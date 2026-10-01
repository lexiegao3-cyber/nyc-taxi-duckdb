from datetime import date

import pytest
from fastapi.testclient import TestClient

from taxi import analytics
from taxi.server import create_app
from test_db import ingest


@pytest.fixture
def client(db, settings):
    ingest(db, settings, months=(date(2025, 1, 1), date(2025, 2, 1)))
    with TestClient(create_app(settings, db)) as c:
        yield c


def test_status(client):
    s = client.get("/api/status").json()
    assert s["trips"] > 0 and len(s["files"]) == 6 and s["zones"] == 7


@pytest.mark.parametrize("name", list(analytics.ANALYSES))
@pytest.mark.parametrize("qs", ["", "services=yellow,green&borough=Manhattan&start=2025-01-05&end=2025-02-10"])
def test_every_analysis_runs(client, name, qs):
    r = client.get(f"/api/analysis/{name}?{qs}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["rows"], list) and body["sql"]


def test_overview_respects_filters(client):
    total = client.get("/api/analysis/overview").json()["rows"][0]["trips"]
    yellow = client.get("/api/analysis/overview?services=yellow").json()["rows"][0]["trips"]
    jan = client.get("/api/analysis/overview?start=2025-01-01&end=2025-01-31").json()["rows"][0]["trips"]
    assert 0 < yellow < total and 0 < jan < total


def test_cache_invalidates_after_write(client, db, settings):
    first = client.get("/api/analysis/overview").json()
    assert client.get("/api/analysis/overview").json()["cached"] is True
    db.write(lambda cur: cur.execute("DELETE FROM trips WHERE service = 'green'")).result()
    after = client.get("/api/analysis/overview").json()
    assert after["cached"] is False and after["rows"][0]["trips"] < first["rows"][0]["trips"]


@pytest.mark.parametrize("bad", [
    "DELETE FROM trips",
    "SELECT 1; SELECT 2",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM '/etc/passwd'",
    "SELECT * FROM (SELECT * FROM read_text('/etc/hosts'))",
    "WITH x AS (SELECT * FROM read_parquet('a.parquet')) SELECT * FROM x",
])
def test_sql_lab_rejects(client, bad):
    assert client.post("/api/sql", json={"sql": bad}).status_code == 400


def test_sql_lab_allows_selects(client):
    r = client.post("/api/sql", json={"sql": "WITH b AS (SELECT borough FROM zones) "
                                             "SELECT b.borough, count(*) FROM trips t JOIN zones z "
                                             "ON z.location_id = t.pu_location_id JOIN b USING (borough) "
                                             "GROUP BY ALL"})
    assert r.status_code == 200, r.text
    assert r.json()["rows"]


def test_bench_and_explain(client):
    b = client.get("/api/bench").json()
    assert len(b["results"]) == len(analytics.BENCHMARKS)
    assert all("parquet_ms" in r for r in b["results"])
    plan = client.get("/api/explain/heatmap").json()["plan"]
    assert "Total Time" in plan and "TABLE_SCAN" in plan


def test_ingest_endpoint_validates(client):
    assert client.post("/api/ingest", json={"services": ["bus"], "months": "2025-01"}).status_code == 400
    assert client.post("/api/ingest", json={"services": ["yellow"], "months": "2025-13"}).status_code == 400


def test_index_served(client):
    assert "DuckDB" in client.get("/").text
