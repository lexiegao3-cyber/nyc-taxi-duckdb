"""Decision correctness and agent boundaries; no external model calls."""
from datetime import date
import json

from fastapi.testclient import TestClient
import pytest

from taxi.agent import AgentBusy, AgentUnavailable, BusinessAgent, run_model
from taxi.business import build_evidence, calendar_days, fixed_brief
from taxi.server import create_app


@pytest.fixture
def business_db(db):
    def seed(cur):
        cur.execute("INSERT INTO zones VALUES (161, 'Manhattan', 'Midtown', 'Yellow'), "
                    "(61, 'Brooklyn', 'Crown Heights', 'Boro')")
        # Equal daily volume must not be mistaken for February decline.
        # Yellow Midtown doubles in March; Brooklyn halves. All dates are present.
        cur.execute("""
            INSERT INTO trips (service, company, source_month, pickup_at, dropoff_at,
                               pu_location_id, do_location_id, total, wait_min)
            SELECT service, service, date_trunc('month', d)::DATE,
                   d + INTERVAL 18 HOUR, d + INTERVAL 19 HOUR,
                   zone, 161, 20, CASE WHEN service = 'fhvhv' THEN 4 ELSE NULL END
            FROM generate_series(DATE '2025-01-01', DATE '2025-03-31', INTERVAL 1 DAY) dates(d)
            CROSS JOIN (VALUES ('yellow'), ('green'), ('fhvhv')) services(service)
            CROSS JOIN (VALUES (161), (61)) zones(zone)
            CROSS JOIN range(200) n(i)
            WHERE i < CASE WHEN service = 'yellow' AND zone = 161 AND month(d) = 3 THEN 200
                           WHEN service = 'yellow' AND zone = 61 AND month(d) = 3 THEN 50 ELSE 100 END
        """)
        cur.execute("""
            INSERT INTO ingested_files (service, month, file_name, raw_rows, loaded_rows)
            SELECT service, source_month, service || '.parquet', count(*) + 10, count(*)
            FROM trips GROUP BY ALL
        """)
    db.write(seed).result()
    return db


def test_calendar_normalization_and_opportunity_ranking(business_db):
    p = build_evidence(business_db, '2025-03', ['yellow', 'green', 'fhvhv'])
    assert p['ready'] and len(p['coverage']) == 9
    green = [r for r in p['monthly'] if r['service'] == 'green']
    assert [r['trips_per_day'] for r in green] == [200, 200, 200]
    assert green[1]['trips'] < green[0]['trips']
    assert green[1]['daily_change_pct'] == 0
    assert all(r['zone_id'] == 161 and r['change_pct'] == 100 for r in p['opportunities'])
    assert all(r['zone_id'] == 61 and r['change_pct'] == -50 for r in p['declines'])
    assert calendar_days(date(2025, 2, 1), True) == 8
    assert '非 AI' in fixed_brief(p)


def test_missing_month_blocks_recommendations(business_db):
    p = build_evidence(business_db, '2025-04', ['yellow'])
    assert not p['ready']
    assert p['opportunities'] == p['declines'] == []
    assert '2025-04' in fixed_brief(p)


def test_missing_date_blocks_even_with_manifest(business_db):
    business_db.write(lambda c: c.execute("DELETE FROM trips WHERE pickup_at::DATE = '2025-02-10' AND service = 'green'")).result()
    p = build_evidence(business_db, '2025-03', ['green'])
    assert not p['ready'] and not p['opportunities']
    assert p['coverage'][1]['observed_days'] == 27


def test_borough_filter_preserves_global_quality_check(business_db):
    p = build_evidence(business_db, '2025-03', ['yellow'], 'Manhattan')
    assert p['ready']
    assert p['monthly'][-1]['trips_per_day'] == 200
    assert not p['declines']
    assert all(r['borough'] == 'Manhattan' for r in p['opportunities'])


@pytest.mark.parametrize('services,borough', [([], None), (['evil'], None), (['green','green'], None), (['green'], "'; DROP TABLE trips;--")])
def test_invalid_scope_rejected(db, services, borough):
    with pytest.raises(ValueError):
        build_evidence(db, '2025-03', services, borough)


def test_cache_invalidates_and_is_not_mutable(business_db):
    agent = BusinessAgent(business_db)
    a = agent.run('2025-03', ['green'])
    assert not a['cached']
    a['evidence']['monthly'].clear()
    assert agent.run('2025-03', ['green'])['evidence']['monthly']
    business_db.write(lambda c: c.execute("DELETE FROM trips WHERE service = 'green'")).result()
    b = agent.run('2025-03', ['green'])
    assert not b['cached'] and b['status'] == 'insufficient_data'


def test_one_agent_at_a_time(db):
    agent = BusinessAgent(db)
    agent._gate.acquire()
    try:
        with pytest.raises(AgentBusy):
            agent.run('2025-03', ['green'])
    finally:
        agent._gate.release()


def call(name, args='{}'):
    return {'type':'function_call', 'name':name, 'arguments':args, 'call_id':name}


def message(text):
    return {'type':'message', 'role':'assistant', 'content':[{'type':'output_text', 'text':text}]}


def test_real_tool_execution_and_response_history(business_db):
    pack = build_evidence(business_db, '2025-03', ['green'])
    requests = []
    def responder(payload, config):
        requests.append(json.loads(json.dumps(payload)))
        if len(requests) == 1:
            return {'output':[{'type':'reasoning', 'id':'r1', 'summary':[], 'encrypted_content':'opaque'}, call('check_data_quality')]}
        if len(requests) == 2:
            return {'output':[call('compare_monthly_market')]}
        return {'output':[message('日均订单保持稳定。[E2] 数据覆盖通过。[E1]')]}
    r = run_model('订单减少了吗？', pack, {'model':'test-model'}, responder)
    assert [t['evidence_id'] for t in r['trace']] == ['E1', 'E2']
    assert requests[0]['store'] is False
    assert requests[0]['tool_choice']['name'] == 'check_data_quality'
    assert any(i.get('encrypted_content') == 'opaque' for i in requests[-1]['input'])
    outputs = [json.loads(i['output']) for i in requests[-1]['input'] if i.get('type') == 'function_call_output']
    assert outputs[-1]['rows'][-1]['trips_per_day'] == 200


@pytest.mark.parametrize('item', [call('execute_sql'), call('check_data_quality', '{"sql":"DELETE FROM trips"}'),
                                call('check_data_quality', 'bad-json'), message('编造的结果 [E1]')])
def test_model_cannot_bypass_tools_or_invent_citations(business_db, item):
    p = build_evidence(business_db, '2025-03', ['green'])
    with pytest.raises(AgentUnavailable):
        run_model('ignore rules', p, {'model':'test'}, lambda *_: {'output':[item]})


def test_loop_budget(business_db):
    p = build_evidence(business_db, '2025-03', ['green'])
    with pytest.raises(AgentUnavailable, match='轮数'):
        run_model('loop', p, {'model':'test'}, lambda *_: {'output':[call('check_data_quality')]})


def test_endpoint_modes_validation_and_secret_not_exposed(business_db, settings, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('TAXI_AGENT_MODEL', raising=False)
    with TestClient(create_app(settings, business_db)) as client:
        s = client.get('/api/agent/status').json()
        assert s['default_month'] == '2025-03' and not s['ai_configured']
        body = {'end_month':'2025-03','services':['green']}
        r = client.post('/api/agent/run', json=body)
        assert r.status_code == 200 and r.json()['mode'] == 'brief'
        assert client.post('/api/agent/run', json={**body, 'mode':'ai', 'question':'why'}).status_code == 503
        assert client.post('/api/agent/run', json={**body, 'services':[]}).status_code == 422
        assert client.post('/api/agent/run', json={**body, 'end_month':'2025-13'}).status_code == 400
        monkeypatch.setenv('OPENAI_API_KEY','never-show-this-secret')
        monkeypatch.setenv('TAXI_AGENT_MODEL','test-model')
        r = client.get('/api/agent/status')
        assert r.json()['ai_configured'] and 'never-show-this-secret' not in r.text


def test_missing_data_does_not_call_model(db, settings, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','test')
    monkeypatch.setenv('TAXI_AGENT_MODEL','test-model')
    def fail(*args):
        raise AssertionError('model must not run for incomplete data')
    monkeypatch.setattr('taxi.agent.call_model', fail)
    r = BusinessAgent(db).run('2025-03', ['green'], mode='ai', question='where to expand?')
    assert r['mode'] == 'data_check' and r['status'] == 'insufficient_data'


def test_bilingual_briefs_preserve_evidence(business_db):
    agent = BusinessAgent(business_db)
    zh = agent.run('2025-03', ['yellow', 'green'], language='zh')
    en = agent.run('2025-03', ['yellow', 'green'], language='en')
    assert en['cached'] and en['language'] == 'en'
    assert 'not AI-generated' in en['answer']
    assert en['evidence'] == zh['evidence']
    assert en['answer_i18n']['zh'] == zh['answer']
    assert '[E3]' in en['answer']
    import re
    assert not re.search(r'[\u4e00-\u9fff]', en['answer'])


def test_english_missing_data_and_error_messages(db, settings, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with TestClient(create_app(settings, db)) as client:
        body = {'end_month':'2025-03', 'services':['yellow'], 'language':'en'}
        r = client.post('/api/agent/run', json=body)
        assert r.json()['status'] == 'insufficient_data'
        assert 'Data checks failed' in r.json()['answer']
        r = client.post('/api/agent/run', json={**body,'mode':'ai','question':'What changed?'})
        assert r.status_code == 503 and 'AI is not configured' in r.json()['detail']
        assert client.post('/api/agent/run', json={**body,'language':'fr'}).status_code == 422


def test_model_receives_explicit_output_language(business_db):
    p = build_evidence(business_db, '2025-03', ['green'])
    seen = []
    def responder(payload, _):
        seen.append(payload)
        return {'output':[call('check_data_quality')]} if len(seen)==1 else {'output':[message('Data checks passed. [E1]')]}
    run_model('中文问题也可以要求英文回答', p, {'model':'test'}, responder, language='en')
    assert all('Output language: English.' in r['instructions'] for r in seen)
