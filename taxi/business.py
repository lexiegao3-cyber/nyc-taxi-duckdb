"""Auditable, read-only business evidence for one three-month analysis window."""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date
import threading
import time

import duckdb

from .analytics import BOROUGHS
from .sources import SERVICES, parse_month

SOURCE = "https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page"
BANDS = ("00:00–05:59", "06:00–11:59", "12:00–17:59", "18:00–23:59")
LIMITATIONS = [
    "已完成行程不等于全部叫车需求；不能据此确认缺车或未满足需求。",
    "乘客支付金额不是平台营收或利润；缺少成本、取消率、接单率和在线司机数据。",
    "结果描述历史关联，不证明天气、活动或政策的因果影响，也不是未来预测。",
    "市场份额仅指所选服务的已导入有效行程；清洗规则和原始数据完整性会影响结果。",
    "机会按日均行程绝对增量排序，不是利润排序；工作日/周末分开，未进一步控制节假日。",
]


def month_shift(month: date, offset: int) -> date:
    n = month.year * 12 + month.month - 1 + offset
    return date(n // 12, n % 12 + 1, 1)


def window(end_month: str) -> list[date]:
    end = parse_month(end_month)
    return [month_shift(end, n) for n in (-2, -1, 0)]


def calendar_days(month: date, weekend: bool | None = None) -> int:
    days = range(1, calendar.monthrange(month.year, month.month)[1] + 1)
    return sum(1 for d in days if weekend is None or (date(month.year, month.month, d).weekday() >= 5) == weekend)


def catalog(db) -> dict:
    rows, _ = db.records("SELECT strftime(month, '%Y-%m') AS month, service, loaded_rows "
                         "FROM ingested_files ORDER BY month, service")
    months = sorted({r["month"] for r in rows})
    return {"months": months, "files": rows, "default_month": months[-1] if months else None,
            "services": list(SERVICES), "boroughs": BOROUGHS, "source": SOURCE}


def build_evidence(db, end_month: str, services: list[str], borough: str | None = None,
                   timeout: float = 25) -> dict:
    """One MVCC snapshot across coverage and aggregates; fixed SQL, no model SQL."""
    if not services or len(services) != len(set(services)) or any(s not in SERVICES for s in services):
        raise ValueError("请选择不重复的有效服务类型。")
    if borough and borough not in BOROUGHS:
        raise ValueError("无效的行政区。")
    months = window(end_month)
    end = month_shift(months[-1], 1)
    if end > date.today().replace(day=1):
        raise ValueError("请选择已经结束的完整月份。")
    marks = ",".join("?" for _ in services)
    params = [months[0], end, *services]
    where = f"source_month >= ? AND source_month < ? AND service IN ({marks})"
    queries = []
    cur = db.cursor()

    def query(label, sql, args):
        started = time.perf_counter()
        timer = threading.Timer(timeout, cur.interrupt)
        timer.daemon = True
        timer.start()
        try:
            result = cur.execute(sql, args)
            names = [d[0] for d in result.description]
            rows = [dict(zip(names, r)) for r in result.fetchall()]
        except duckdb.InterruptException as exc:
            raise TimeoutError("分析查询超时，请减少所选服务或稍后重试。") from exc
        finally:
            timer.cancel()
            timer.join()
        queries.append({"label": label, "sql": sql.strip(), "parameters": [str(x) for x in args],
                        "ms": round((time.perf_counter() - started) * 1000, 1)})
        return rows

    try:
        cur.execute("BEGIN TRANSACTION")
        files = query("Import manifest", f"""
            SELECT service, month, file_name, raw_rows, loaded_rows
            FROM ingested_files WHERE month >= ? AND month < ? AND service IN ({marks})
            ORDER BY month, service
        """, params)
        observed = query("Service / month coverage", f"""
            SELECT service, source_month AS month, count(*) AS trips,
                   count(DISTINCT pickup_at::DATE) AS observed_days,
                   min(pickup_at)::DATE AS first_day, max(pickup_at)::DATE AS last_day
            FROM trips WHERE {where} GROUP BY ALL ORDER BY month, service
        """, params)
        scope = " AND z.borough = ?" if borough else ""
        groups = query("Zone / time-band evidence", f"""
            SELECT t.service, t.source_month AS month, t.pu_location_id AS zone_id,
                   coalesce(z.zone, 'Unknown') AS zone, coalesce(z.borough, 'Unknown') AS borough,
                   isodow(t.pickup_at) >= 6 AS weekend,
                   (hour(t.pickup_at) // 6)::INTEGER AS band,
                   count(*) AS trips, sum(t.total) AS passenger_payments,
                   sum(t.wait_min) FILTER (WHERE t.wait_min BETWEEN 0 AND 180) AS wait_sum,
                   count(t.wait_min) FILTER (WHERE t.wait_min BETWEEN 0 AND 180) AS wait_count
            FROM trips t LEFT JOIN zones z ON z.location_id = t.pu_location_id
            WHERE {where}{scope} GROUP BY ALL
            ORDER BY month, service, zone_id, weekend, band
        """, params + ([borough] if borough else []))
        cur.execute("COMMIT")
    finally:
        cur.close()

    by_file = {(r["month"], r["service"]): r for r in files}
    by_seen = {(r["month"], r["service"]): r for r in observed}
    coverage = []
    for month in months:
        for svc in services:
            f = by_file.get((month, svc), {})
            seen = by_seen.get((month, svc), {})
            expected = calendar_days(month)
            raw, loaded = f.get("raw_rows", 0), f.get("loaded_rows", 0)
            complete = bool(loaded and seen.get("observed_days") == expected and seen.get("trips") == loaded)
            coverage.append({"month": month.strftime("%Y-%m"), "service": svc,
                             "imported": bool(f), "raw_rows": raw, "loaded_rows": loaded,
                             "observed_days": seen.get("observed_days", 0), "expected_days": expected,
                             "excluded_pct": round(100 * (raw - loaded) / raw, 2) if raw else None,
                             "complete": complete})
    ready = all(r["complete"] for r in coverage)
    warnings = list(LIMITATIONS)
    if not ready:
        warnings.insert(0, "三个月的部分服务缺文件、缺日期或行数不一致，暂停机会排名；请先补齐或检查数据。")
    if any((r["excluded_pct"] or 0) > 5 for r in coverage):
        warnings.insert(0, "部分文件清洗排除比例超过 5%，请检查数据质量后再解释业务变化。")

    monthly_totals = defaultdict(lambda: {"trips": 0, "passenger_payments": 0.0})
    cells = defaultdict(dict)
    for r in groups:
        key = (r["month"], r["service"])
        monthly_totals[key]["trips"] += r["trips"]
        monthly_totals[key]["passenger_payments"] += r["passenger_payments"] or 0
        if r["zone_id"] and r["zone_id"] <= 263 and r["borough"] in BOROUGHS:
            cells[(r["service"], r["zone_id"], r["zone"], r["borough"], r["weekend"], r["band"])][r["month"]] = r
    monthly = []
    for month in months:
        total = sum(monthly_totals[(month, s)]["trips"] for s in services)
        for svc in services:
            v = monthly_totals[(month, svc)]
            prev = monthly_totals.get((month_shift(month, -1), svc))
            per_day = v["trips"] / calendar_days(month)
            prev_per_day = prev["trips"] / calendar_days(month_shift(month, -1)) if prev else 0
            monthly.append({"month": month.strftime("%Y-%m"), "service": svc, "trips": v["trips"],
                            "calendar_days": calendar_days(month), "trips_per_day": round(per_day, 2),
                            "daily_change_pct": round(100 * (per_day / prev_per_day - 1), 2) if prev_per_day else None,
                            "share_pct": round(100 * v["trips"] / total, 2) if total else None,
                            "passenger_payments": round(v["passenger_payments"], 2)})
    candidates = []
    if ready:
        for key, history in cells.items():
            svc, zone_id, zone, area, weekend, band = key
            previous = history.get(months[-2], {})
            current = history.get(months[-1], {})
            before, after = previous.get("trips", 0), current.get("trips", 0)
            # Avoid tiny denominators; zero-baseline/new and vanished segments are not ranked.
            if min(before, after) < 100:
                continue
            prev_day = before / calendar_days(months[-2], weekend)
            curr_day = after / calendar_days(months[-1], weekend)
            first = history.get(months[0], {}).get("trips", 0) / calendar_days(months[0], weekend)
            wait_n = current.get("wait_count", 0)
            candidates.append({"service": svc, "zone_id": zone_id, "zone": zone, "borough": area,
                               "day_type": "周末" if weekend else "工作日", "time_band": BANDS[band],
                               "previous_trips": before, "current_trips": after,
                               "first_daily": round(first, 2), "previous_daily": round(prev_day, 2),
                               "current_daily": round(curr_day, 2), "daily_delta": round(curr_day - prev_day, 2),
                               "change_pct": round(100 * (curr_day / prev_day - 1), 2),
                               "avg_wait_min": round(current["wait_sum"] / wait_n, 2) if wait_n else None})
    opportunities = sorted((r for r in candidates if r["daily_delta"] > 0),
                           key=lambda r: (-r["daily_delta"], r["service"], r["zone_id"], r["time_band"]))[:10]
    declines = sorted((r for r in candidates if r["daily_delta"] < 0),
                      key=lambda r: (r["daily_delta"], r["service"], r["zone_id"], r["time_band"]))[:10]
    scope = {"months": [m.strftime("%Y-%m") for m in months], "services": services,
             "borough": borough or "All", "comparison": "latest month vs previous month",
             "normalization": "calendar days; zone/time-band comparisons use matching weekday/weekend day counts"}
    return {"scope": scope, "ready": ready, "source": SOURCE, "warnings": warnings,
            "coverage": coverage, "monthly": monthly, "opportunities": opportunities, "declines": declines,
            "methodology": "三个月内最新月对比上月；按日历天归一化。区域×服务×工作日/周末×6小时时段；"
                           "前后月均至少100条行程；排除未知区域；按日均行程绝对变化排名，未进行显著性检验。"
                           "新出现、消失及低样本分组不进入排名。完整性检查能发现缺日期，不能证明源数据完全无遗漏。",
            "queries": queries}


def fixed_brief(pack: dict) -> str:
    """Explicitly a deterministic report, never represented as a model answer."""
    scope = pack["scope"]
    lines = ["运营决策简报（固定分析流程，非 AI 生成）",
             f"范围：{scope['months'][0]} 至 {scope['months'][-1]}；服务：{', '.join(scope['services'])}；行政区：{scope['borough']}。",
             "\n1. 数据与市场变化 [E1] [E2]"]
    if not pack["ready"]:
        lines += ["数据完整性检查未通过，暂停运营机会建议。请先补齐以下服务与月份："]
        lines += [f"- {r['month']} / {r['service']}：{r['observed_days']}/{r['expected_days']} 天。"
                  for r in pack["coverage"] if not r["complete"]]
    else:
        for r in pack["monthly"]:
            if r["month"] == scope["months"][-1]:
                change = f"{r['daily_change_pct']:+.2f}%" if r["daily_change_pct"] is not None else "无法计算"
                lines.append(f"- {r['service']}：{r['trips']:,} 条有效行程，日均 {r['trips_per_day']:,.2f}，较上月 {change}。")
        lines += ["\n2. 值得调查的区域与时段 [E3]"]
        for r in pack["opportunities"][:3]:
            lines.append(f"- {r['zone']} / {r['service']} / {r['day_type']} {r['time_band']}："
                         f"日均 {r['previous_daily']:,.2f} → {r['current_daily']:,.2f}（{r['change_pct']:+.2f}%）。")
        if not pack["opportunities"]:
            lines.append("- 没有满足样本门槛的正增长分组，不强行推荐三个机会。")
        lines += ["\n3. 需要复盘的下降 [E4]"]
        for r in pack["declines"][:3]:
            lines.append(f"- {r['zone']} / {r['service']} / {r['day_type']} {r['time_band']}：日均变化 {r['daily_delta']:+,.2f}。")
        if not pack["declines"]:
            lines.append("- 没有满足样本门槛的下降分组。")
        lines += ["\n4. 建议行动与验证",
                  "- 对排名靠前的候选区域，先核对内部接单率、取消率、在线司机数及每单贡献利润，再决定是否试点调整运力。",
                  "- 与相似的对照区域开展有限运营试验，记录履约率、接驾时长和每单贡献利润；预先约定成本上限与停止条件。",
                  "- 对下降分组先排查数据、服务覆盖及运营变更；当前证据不能确认下降原因，也不能承诺调度或补贴的收益。"]
    lines += ["\n5. 分析局限", *[f"- {w}" for w in pack["warnings"]]]
    return "\n".join(lines)


def english_brief(pack: dict) -> str:
    """English version of the same deterministic analysis; no model call."""
    from .localization import translate
    scope = pack['scope']
    lines = ['Operating decision brief (fixed workflow, not AI-generated)',
             f"Scope: {scope['months'][0]} to {scope['months'][-1]}; services: {', '.join(scope['services'])}; borough: {scope['borough']}.",
             '\n1. Data and market changes [E1] [E2]']
    if not pack['ready']:
        lines.append('Data checks failed. Operating recommendations are paused. Check these service/month slices:')
        lines += [f"- {r['month']} / {r['service']}: {r['observed_days']}/{r['expected_days']} observed days."
                  for r in pack['coverage'] if not r['complete']]
    else:
        for r in pack['monthly']:
            if r['month'] == scope['months'][-1]:
                change = f"{r['daily_change_pct']:+.2f}%" if r['daily_change_pct'] is not None else 'unavailable'
                lines.append(f"- {r['service']}: {r['trips']:,} retained trips; {r['trips_per_day']:,.2f} per calendar day; daily volume change vs previous month: {change}.")
        lines.append('\n2. Zones and time bands to investigate [E3]')
        for r in pack['opportunities'][:3]:
            lines.append(f"- {r['zone']} / {r['service']} / {translate(r['day_type'], 'en')} {r['time_band']}: "
                         f"daily average {r['previous_daily']:,.2f} → {r['current_daily']:,.2f} ({r['change_pct']:+.2f}%).")
        if not pack['opportunities']:
            lines.append('- No positive-growth groups meet the sample threshold. No opportunities are invented to fill a quota.')
        lines.append('\n3. Declines to review [E4]')
        for r in pack['declines'][:3]:
            lines.append(f"- {r['zone']} / {r['service']} / {translate(r['day_type'], 'en')} {r['time_band']}: daily change {r['daily_delta']:+,.2f}.")
        if not pack['declines']:
            lines.append('- No declining groups meet the sample threshold.')
        lines += ['\n4. Proposed actions and validation',
                  '- For the top candidates, check internal acceptance rates, cancellations, available drivers, and contribution margin per trip before considering a capacity experiment.',
                  '- Run a limited operating experiment with comparable control zones. Track fulfillment, pickup waits, and contribution margin; agree on a cost cap and stopping rules in advance.',
                  '- For declining groups, check data, service coverage, and operating changes first. The evidence does not establish causes or guarantee returns from dispatch changes or incentives.']
    lines += ['\n5. Limitations', *['- ' + translate(w, 'en') for w in pack['warnings']]]
    return '\n'.join(lines)
