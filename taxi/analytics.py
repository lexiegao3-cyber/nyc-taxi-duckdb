"""Travel-demand analyses, each a single DuckDB SQL query over the full `trips` table.

Every analysis takes the same `Filters` and returns (sql, params). Nothing is
pre-aggregated: the point is to let DuckDB's vectorised, multi-threaded engine
scan tens or hundreds of millions of rows on every request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .sources import SERVICES

AIRPORTS = {"JFK": 132, "LGA": 138, "EWR": 1}
BOROUGHS = ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island", "EWR"]


@dataclass(frozen=True)
class Filters:
    services: tuple[str, ...] = tuple(SERVICES)
    start: date | None = None
    end: date | None = None  # inclusive
    borough: str | None = None  # pickup borough

    def __post_init__(self):
        bad = [s for s in self.services if s not in SERVICES]
        if bad or not self.services:
            raise ValueError(f"services must be a non-empty subset of {list(SERVICES)}")
        if self.borough and self.borough not in BOROUGHS:
            raise ValueError(f"borough must be one of {BOROUGHS}")

    def where(self, alias: str = "t") -> tuple[str, list]:
        a = alias
        clauses = [f"{a}.service IN ({', '.join('?' for _ in self.services)})"]
        params: list = list(self.services)
        if self.start:
            clauses.append(f"{a}.pickup_at >= ?")
            params.append(self.start)
        if self.end:
            clauses.append(f"{a}.pickup_at < ?")
            params.append(self.end + timedelta(days=1))
        if self.borough:
            clauses.append(
                f"{a}.pu_location_id IN (SELECT location_id FROM zones WHERE borough = ?)"
            )
            params.append(self.borough)
        return " AND ".join(clauses), params


ANALYSES: dict[str, dict] = {}


def analysis(title: str):
    def register(fn):
        ANALYSES[fn.__name__] = {"title": title, "build": fn, "doc": (fn.__doc__ or "").strip()}
        return fn

    return register


@analysis("总体指标")
def overview(f: Filters):
    """行程量、营收、时长分位数与平均车速。"""
    w, p = f.where()
    return f"""
SELECT
    count(*)                                          AS trips,
    round(sum(total))                                 AS revenue,
    round(avg(trip_miles), 2)                         AS avg_miles,
    round(avg(duration_min), 1)                       AS avg_min,
    round(quantile_cont(duration_min, 0.5), 1)        AS median_min,
    round(quantile_cont(duration_min, 0.9), 1)        AS p90_min,
    round(sum(trip_miles) / nullif(sum(duration_min) / 60, 0), 1) AS avg_mph,
    count(DISTINCT pickup_at::DATE)                   AS days,
    round(count(*) / nullif(count(DISTINCT pickup_at::DATE), 0)) AS trips_per_day,
    round(100 * sum(tip) FILTER (WHERE payment_type = 1 OR service = 'fhvhv')
        / nullif(sum(fare) FILTER (WHERE payment_type = 1 OR service = 'fhvhv'), 0), 1) AS tip_pct,
    min(pickup_at)::DATE                              AS first_day,
    max(pickup_at)::DATE                              AS last_day
FROM trips t WHERE {w}
""", p


@analysis("每日出行量与 7 日移动平均")
def daily(f: Filters):
    """按服务类型统计每日行程，并用窗口函数计算 7 日移动平均。"""
    w, p = f.where()
    return f"""
WITH d AS (
    SELECT pickup_at::DATE AS day, service, count(*) AS trips
    FROM trips t WHERE {w}
    GROUP BY ALL
)
SELECT day, service, trips,
       round(avg(trips) OVER (PARTITION BY service ORDER BY day
                              ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)) AS ma7
FROM d ORDER BY day, service
""", p


@analysis("星期 × 小时 需求热力图")
def heatmap(f: Filters):
    """每个(星期, 小时)时段的平均每小时上车量。"""
    w, p = f.where()
    return f"""
WITH h AS (
    SELECT date_trunc('hour', pickup_at) AS hr, count(*) AS n
    FROM trips t WHERE {w}
    GROUP BY 1
)
SELECT isodow(hr) AS dow, hour(hr) AS hour, round(avg(n)) AS avg_trips
FROM h GROUP BY ALL ORDER BY dow, hour
""", p


@analysis("热门上车区域")
def top_zones(f: Filters):
    """上车量 Top 15 区域、占比与客单价。"""
    w, p = f.where()
    return f"""
SELECT z.zone, z.borough,
       count(*)                                         AS trips,
       round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS share_pct,
       round(avg(t.total), 2)                           AS avg_total,
       round(avg(t.trip_miles), 2)                      AS avg_miles
FROM trips t JOIN zones z ON z.location_id = t.pu_location_id
WHERE {w}
GROUP BY ALL
ORDER BY trips DESC
LIMIT 15
""", p


@analysis("热门 OD（起点→终点）")
def od_pairs(f: Filters):
    """出行最多的 20 条起终点组合，含中位时长与平均车速。"""
    w, p = f.where()
    # rank pairs with a cheap count first, then compute the exact median only for the top 20
    return f"""
WITH top AS (
    SELECT pu_location_id, do_location_id, count(*) AS trips
    FROM trips t WHERE {w} AND pu_location_id <> do_location_id
    GROUP BY ALL ORDER BY trips DESC LIMIT 20
)
SELECT pz.zone AS origin, dz.zone AS destination,
       any_value(top.trips)                              AS trips,
       round(quantile_cont(t.duration_min, 0.5), 1)      AS median_min,
       round(avg(t.trip_miles), 2)                       AS avg_miles,
       round(sum(t.trip_miles) / nullif(sum(t.duration_min) / 60, 0), 1) AS mph,
       round(avg(t.total), 2)                            AS avg_total
FROM trips t
JOIN top USING (pu_location_id, do_location_id)
JOIN zones pz ON pz.location_id = t.pu_location_id
JOIN zones dz ON dz.location_id = t.do_location_id
WHERE {w}
GROUP BY ALL
ORDER BY trips DESC
""", p + p


@analysis("行政区间出行流向")
def borough_flows(f: Filters):
    """行政区 → 行政区 的行程数（桑基图）。"""
    w, p = f.where()
    return f"""
SELECT pz.borough AS source, dz.borough AS target, count(*) AS trips
FROM trips t
JOIN zones pz ON pz.location_id = t.pu_location_id
JOIN zones dz ON dz.location_id = t.do_location_id
WHERE {w}
  AND pz.borough NOT IN ('Unknown', 'N/A') AND dz.borough NOT IN ('Unknown', 'N/A')
GROUP BY ALL
ORDER BY trips DESC
""", p


@analysis("各公司月度市场份额")
def market_share(f: Filters):
    """黄车 / 绿车 / Uber / Lyft 每月行程量与份额。"""
    w, p = f.where()
    return f"""
WITH m AS (
    SELECT date_trunc('month', pickup_at)::DATE AS month, company, count(*) AS trips
    FROM trips t WHERE {w}
    GROUP BY ALL
)
SELECT month, company, trips,
       round(100.0 * trips / sum(trips) OVER (PARTITION BY month), 2) AS share_pct
FROM m ORDER BY month, trips DESC
""", p


@analysis("机场出行")
def airports(f: Filters):
    """JFK / LGA / EWR 进出机场行程按小时分布。"""
    w, p = f.where()
    values = ", ".join(f"({lid}, '{name}')" for name, lid in AIRPORTS.items())
    return f"""
WITH a(id, airport) AS (VALUES {values}),
legs AS (
    SELECT a.airport, '离开机场' AS direction, t.* FROM trips t JOIN a ON t.pu_location_id = a.id
    WHERE {w}
    UNION ALL
    SELECT a.airport, '前往机场' AS direction, t.* FROM trips t JOIN a ON t.do_location_id = a.id
    WHERE {w}
)
SELECT airport, direction, hour(pickup_at) AS hour,
       count(*) AS trips, round(avg(total), 2) AS avg_total, round(avg(duration_min), 1) AS avg_min
FROM legs GROUP BY ALL ORDER BY airport, direction, hour
""", p + p


@analysis("各时段平均车速（拥堵）")
def speed_by_hour(f: Filters):
    """工作日 / 周末各小时平均车速，反映道路拥堵。"""
    w, p = f.where()
    return f"""
SELECT hour(pickup_at) AS hour,
       CASE WHEN isodow(pickup_at) <= 5 THEN '工作日' ELSE '周末' END AS day_type,
       round(sum(trip_miles) / nullif(sum(duration_min) / 60, 0), 2) AS mph,
       round(avg(duration_min), 1) AS avg_min
FROM trips t
WHERE {w} AND trip_miles > 0 AND duration_min >= 1
GROUP BY ALL ORDER BY day_type, hour
""", p


@analysis("行程距离分布")
def distance_hist(f: Filters):
    """按 1 英里分箱的行程距离分布（30 英里以上合并）。"""
    w, p = f.where()
    return f"""
SELECT least(floor(trip_miles), 30)::INT AS miles, service, count(*) AS trips
FROM trips t WHERE {w} AND trip_miles IS NOT NULL
GROUP BY ALL ORDER BY miles, service
""", p


@analysis("运营经济性")
def economics(f: Filters):
    """各公司每英里/每分钟票价、小费率、司机分成、等车时间与拥堵费覆盖率。"""
    w, p = f.where()
    return f"""
SELECT company,
       count(*)                                                   AS trips,
       round(avg(fare), 2)                                        AS avg_fare,
       round(sum(fare) / nullif(sum(trip_miles), 0), 2)           AS fare_per_mile,
       round(sum(fare) / nullif(sum(duration_min), 0), 2)         AS fare_per_min,
       round(100 * sum(tip) FILTER (WHERE payment_type = 1 OR service = 'fhvhv')
           / nullif(sum(fare) FILTER (WHERE payment_type = 1 OR service = 'fhvhv'), 0), 1) AS tip_pct,
       round(100 * sum(driver_pay) / nullif(sum(fare), 0), 1)    AS driver_pay_pct,
       round(avg(wait_min), 1)                                    AS avg_wait_min,
       round(quantile_cont(wait_min, 0.9), 1)                     AS p90_wait_min,
       round(100 * avg((cbd_fee > 0)::INT), 1)                    AS cbd_fee_pct,
       round(100 * avg(shared::INT), 2)                           AS shared_pct
FROM trips t WHERE {w}
GROUP BY ALL ORDER BY trips DESC
""", p


@analysis("网约车等车时间")
def wait_times(f: Filters):
    """HVFHV 各小时从叫车到上车的中位 / P90 等待时间。"""
    w, p = f.where()
    return f"""
SELECT hour(pickup_at) AS hour, company,
       round(quantile_cont(wait_min, 0.5), 2) AS p50_wait,
       round(quantile_cont(wait_min, 0.9), 2) AS p90_wait,
       count(*) AS trips
FROM trips t
WHERE {w} AND service = 'fhvhv' AND wait_min BETWEEN 0 AND 60
GROUP BY ALL ORDER BY company, hour
""", p


@analysis("异常需求峰值（事件检测）")
def anomalies(f: Filters):
    """区域-小时上车量相对“同区域·同星期·同小时”留一法基线的 Z 分数，每个区域取最异常的一小时、每天最多 3 条，常对应演唱会、球赛、节假日、恶劣天气。"""
    w, p = f.where()
    # Leave-one-out baseline: the spike itself is excluded from its own mean / stddev,
    # otherwise z can never exceed (k-1)/sqrt(k) for k weeks of history.
    return f"""
WITH zh AS (
    SELECT pu_location_id AS zone_id, date_trunc('hour', pickup_at) AS hr, count(*) AS n
    FROM trips t WHERE {w}
    GROUP BY ALL
), stats AS (
    SELECT *,
           sum(n)     OVER slot AS s1,
           sum(n * n) OVER slot AS s2,
           count(*)   OVER slot AS k
    FROM zh
    WINDOW slot AS (PARTITION BY zone_id, isodow(hr), hour(hr))
), loo AS (
    SELECT *, (s1 - n) / (k - 1) AS mu,
           sqrt(greatest((s2 - n * n - (s1 - n) * (s1 - n) / (k - 1)) / (k - 2), 0)) AS sd
    FROM stats WHERE k >= 4
), best AS (  -- each zone's single most anomalous hour
    SELECT hr, zone_id, n, mu, (n - mu) / sd AS z
    FROM loo WHERE mu >= 10 AND sd > 0
    QUALIFY row_number() OVER (PARTITION BY zone_id ORDER BY z DESC) = 1
)
SELECT hr AS hour, z.zone, z.borough, n AS trips,
       round(mu, 1)      AS expected,
       round(n / mu, 1)  AS ratio,
       round(best.z, 1)  AS z_score
FROM best JOIN zones z ON z.location_id = best.zone_id
QUALIFY row_number() OVER (PARTITION BY hr::DATE ORDER BY best.z DESC) <= 3  -- max 3 per day
ORDER BY z_score DESC
LIMIT 20
""", p


def build(name: str, f: Filters) -> tuple[str, list]:
    if name not in ANALYSES:
        raise KeyError(name)
    return ANALYSES[name]["build"](f)


# Benchmark suite: (label, sql). {trips} is replaced by the relation under test,
# so the same SQL can run on the DuckDB table and directly on raw Parquet files.
BENCHMARKS = [
    ("条件计数（全表扫描）", "SELECT count(*) FILTER (WHERE trip_miles > 10) FROM {trips}"),
    ("按小时聚合", "SELECT hour(pickup_at) h, count(*), avg(trip_miles) FROM {trips} GROUP BY 1"),
    ("区域 × 日 聚合", "SELECT pu_location_id, pickup_at::DATE d, count(*) FROM {trips} GROUP BY ALL"),
    ("时长分位数", "SELECT service, quantile_cont(duration_min, [0.5, 0.9, 0.99]) FROM {trips} GROUP BY 1"),
    ("OD 精确去重计数", "SELECT count(DISTINCT (pu_location_id, do_location_id)) FROM {trips}"),
]
