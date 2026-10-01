"""Normalise yellow / green / HVFHV Parquet files into one `trips` table.

TLC schemas drift between services and years (e.g. `Airport_fee` vs
`airport_fee`, `cbd_congestion_fee` only from 2025, int vs double passenger
counts), so the SELECT is generated per file from the columns it actually has.
"""

from __future__ import annotations

from datetime import date

TRIPS_DDL = """
CREATE TABLE IF NOT EXISTS trips (
    service         VARCHAR NOT NULL,   -- yellow | green | fhvhv
    company         VARCHAR NOT NULL,   -- Yellow | Green | Uber | Lyft | Via | Juno
    source_month    DATE NOT NULL,
    pickup_at       TIMESTAMP NOT NULL,
    dropoff_at      TIMESTAMP NOT NULL,
    pu_location_id  SMALLINT,
    do_location_id  SMALLINT,
    passenger_count TINYINT,
    trip_miles      DOUBLE,
    duration_min    DOUBLE,
    fare            DOUBLE,             -- metered fare / base passenger fare
    tip             DOUBLE,
    tolls           DOUBLE,
    total           DOUBLE,             -- what the rider paid
    congestion_fee  DOUBLE,
    airport_fee     DOUBLE,
    cbd_fee         DOUBLE,             -- Manhattan congestion pricing (2025-01-05+)
    payment_type    TINYINT,            -- taxis only: 1 card, 2 cash, ...
    wait_min        DOUBLE,             -- HVFHV only: request -> pickup
    driver_pay      DOUBLE,             -- HVFHV only
    shared          BOOLEAN             -- HVFHV only
);
"""

META_DDL = """
CREATE TABLE IF NOT EXISTS ingested_files (
    service      VARCHAR NOT NULL,
    month        DATE NOT NULL,
    file_name    VARCHAR NOT NULL,
    file_bytes   BIGINT,
    raw_rows     BIGINT,
    loaded_rows  BIGINT,
    seconds      DOUBLE,
    loaded_at    TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (service, month)
);
CREATE TABLE IF NOT EXISTS zones (
    location_id  SMALLINT PRIMARY KEY,
    borough      VARCHAR,
    zone         VARCHAR,
    service_zone VARCHAR
);
"""

HVFHS_COMPANIES = {"HV0002": "Juno", "HV0003": "Uber", "HV0004": "Via", "HV0005": "Lyft"}

# column name (lower-case) per service for each unified field
_COLUMN_MAP = {
    "yellow": {
        "pickup": "tpep_pickup_datetime",
        "dropoff": "tpep_dropoff_datetime",
        "miles": "trip_distance",
        "fare": "fare_amount",
        "tip": "tip_amount",
        "tolls": "tolls_amount",
        "total": "total_amount",
    },
    "green": {
        "pickup": "lpep_pickup_datetime",
        "dropoff": "lpep_dropoff_datetime",
        "miles": "trip_distance",
        "fare": "fare_amount",
        "tip": "tip_amount",
        "tolls": "tolls_amount",
        "total": "total_amount",
    },
    "fhvhv": {
        "pickup": "pickup_datetime",
        "dropoff": "dropoff_datetime",
        "miles": "trip_miles",
        "fare": "base_passenger_fare",
        "tip": "tips",
        "tolls": "tolls",
    },
}

MAX_TRIP_HOURS = 6
MAX_TRIP_MILES = 200


def _col(cols: set[str], name: str, sql_type: str) -> str:
    if name.lower() in cols:
        return f'TRY_CAST("{name}" AS {sql_type})'
    return f"CAST(NULL AS {sql_type})"


def select_sql(service: str, cols: set[str], source: str, month: date) -> str:
    """Build the normalising SELECT for one Parquet file.

    `cols` are the file's column names, lower-cased. `source` is a SQL
    expression for the relation (e.g. read_parquet('...')).
    """
    if service not in _COLUMN_MAP:
        raise ValueError(f"unknown service {service!r}")
    cols = {c.lower() for c in cols}
    m = _COLUMN_MAP[service]
    c = lambda name, t="DOUBLE": _col(cols, name, t)  # noqa: E731

    if service == "fhvhv":
        company = "CASE hvfhs_license_num " + " ".join(
            f"WHEN '{k}' THEN '{v}'" for k, v in HVFHS_COMPANIES.items()
        ) + " ELSE 'Other HVFHV' END"
        fee_parts = [
            "base_passenger_fare", "tolls", "bcf", "sales_tax",
            "congestion_surcharge", "airport_fee", "tips", "cbd_congestion_fee",
        ]
        total = " + ".join(f"coalesce({c(p)}, 0)" for p in fee_parts)
        passenger = "CAST(NULL AS TINYINT)"
        payment = "CAST(NULL AS TINYINT)"
        wait = (
            f"date_diff('second', {c('request_datetime', 'TIMESTAMP')}, pickup_at) / 60.0"
        )
        driver_pay = c("driver_pay")
        shared = (
            "(shared_match_flag = 'Y')" if "shared_match_flag" in cols else "CAST(NULL AS BOOLEAN)"
        )
    else:
        company = f"'{service.capitalize()}'"
        total = c(m["total"])
        passenger = c("passenger_count", "TINYINT")
        payment = c("payment_type", "TINYINT")
        wait = "CAST(NULL AS DOUBLE)"
        driver_pay = "CAST(NULL AS DOUBLE)"
        shared = "CAST(NULL AS BOOLEAN)"

    # two stages: cast/rename first, then filter on the clean names
    return f"""
WITH src AS (
    SELECT
        *,
        {c(m['pickup'], 'TIMESTAMP')}  AS pickup_at,
        {c(m['dropoff'], 'TIMESTAMP')} AS dropoff_at
    FROM {source}
), norm AS (
    SELECT
        '{service}'                                  AS service,
        {company}                                    AS company,
        DATE '{month:%Y-%m-%d}'                      AS source_month,
        pickup_at,
        dropoff_at,
        {c('PULocationID', 'SMALLINT')}              AS pu_location_id,
        {c('DOLocationID', 'SMALLINT')}              AS do_location_id,
        {passenger}                                  AS passenger_count,
        {c(m['miles'])}                              AS trip_miles,
        date_diff('second', pickup_at, dropoff_at) / 60.0 AS duration_min,
        {c(m['fare'])}                               AS fare,
        {c(m['tip'])}                                AS tip,
        {c(m['tolls'])}                              AS tolls,
        {total}                                      AS total,
        {c('congestion_surcharge')}                  AS congestion_fee,
        {c('airport_fee')}                           AS airport_fee,
        {c('cbd_congestion_fee')}                    AS cbd_fee,
        {payment}                                    AS payment_type,
        {wait}                                       AS wait_min,
        {driver_pay}                                 AS driver_pay,
        {shared}                                     AS shared
    FROM src
)
SELECT * FROM norm
WHERE pickup_at >= DATE '{month:%Y-%m-%d}'
  AND pickup_at <  DATE '{month:%Y-%m-%d}' + INTERVAL 1 MONTH
  AND dropoff_at > pickup_at
  AND dropoff_at < pickup_at + INTERVAL {MAX_TRIP_HOURS} HOUR
  AND pu_location_id BETWEEN 1 AND 265
  AND do_location_id BETWEEN 1 AND 265
  AND coalesce(trip_miles, 0) BETWEEN 0 AND {MAX_TRIP_MILES}
  AND coalesce(total, 0) >= 0
"""
