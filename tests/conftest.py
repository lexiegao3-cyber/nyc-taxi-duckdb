"""Synthetic TLC-shaped Parquet files, so tests run offline in seconds."""

from datetime import date
from pathlib import Path

import duckdb
import pytest

from taxi.config import Settings
from taxi.db import Database

ZONES_CSV = """LocationID,Borough,Zone,service_zone
1,EWR,Newark Airport,EWR
132,Queens,JFK Airport,Airports
138,Queens,LaGuardia Airport,Airports
161,Manhattan,Midtown Center,Yellow Zone
236,Manhattan,Upper East Side North,Yellow Zone
61,Brooklyn,Crown Heights North,Boro Zone
264,Unknown,N/A,N/A
"""

ZONE_IDS = [1, 132, 138, 161, 236, 61]
N = 3000  # valid rows per file; each file also gets 3 rows that must be dropped


def _write(con, sql: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet)")


def make_month(base: Path, month: date) -> None:
    """Write yellow/green/fhvhv files for one month under base/trip-data/."""
    con = duckdb.connect()
    m = f"DATE '{month:%Y-%m-%d}'"
    zones = "[" + ",".join(map(str, ZONE_IDS)) + "]"
    common = f"""
        {m} + to_minutes(i * 13 % (27 * 24 * 60)) AS pu,
        {zones}[1 + i % {len(ZONE_IDS)}] AS PULocationID,
        {zones}[1 + (i * 7) % {len(ZONE_IDS)}] AS DOLocationID,
        1 + i % 15 AS mins, (i % 90) / 10.0 AS miles
    """
    bad = f"""
        UNION ALL SELECT -1, {m} - INTERVAL 40 DAY, 161, 236, 10, 1.0   -- previous month
        UNION ALL SELECT -2, {m} + INTERVAL 1 DAY, 161, 236, -5, 1.0    -- negative duration
        UNION ALL SELECT -3, {m} + INTERVAL 1 DAY, 999, 236, 10, 1.0    -- bad zone
    """
    gen = f"SELECT i, {common} FROM range({N}) t(i) {bad}"
    for svc, prefix in (("yellow", "tpep"), ("green", "lpep")):
        _write(con, f"""
            SELECT 2 AS VendorID, pu AS {prefix}_pickup_datetime,
                   pu + to_minutes(mins) AS {prefix}_dropoff_datetime,
                   (1 + i % 3)::DOUBLE AS passenger_count, miles AS trip_distance,
                   PULocationID, DOLocationID, (1 + i % 2) AS payment_type,
                   5 + miles * 3 AS fare_amount, CASE WHEN i % 2 = 0 THEN 2.0 ELSE 0 END AS tip_amount,
                   0.0 AS tolls_amount, 10 + miles * 3 AS total_amount,
                   2.5 AS congestion_surcharge, 0.0 AS Airport_fee, 0.75 AS cbd_congestion_fee
            FROM ({gen})""", base / "trip-data" / f"{svc}_tripdata_{month:%Y-%m}.parquet")
    _write(con, f"""
        SELECT CASE WHEN i % 3 = 0 THEN 'HV0005' ELSE 'HV0003' END AS hvfhs_license_num,
               pu - to_minutes(3) AS request_datetime, pu AS pickup_datetime,
               pu + to_minutes(mins) AS dropoff_datetime, PULocationID, DOLocationID,
               miles AS trip_miles, mins * 60 AS trip_time, 8 + miles * 2 AS base_passenger_fare,
               0.0 AS tolls, 0.5 AS bcf, 0.7 AS sales_tax, 2.75 AS congestion_surcharge,
               0.0 AS airport_fee, 1.0 AS tips, 6 + miles AS driver_pay, 'N' AS shared_match_flag
        FROM ({gen})""", base / "trip-data" / f"fhvhv_tripdata_{month:%Y-%m}.parquet")
    (base / "misc").mkdir(parents=True, exist_ok=True)
    (base / "misc" / "taxi_zone_lookup.csv").write_text(ZONES_CSV)


@pytest.fixture(scope="session")
def remote(tmp_path_factory) -> Path:
    """A local directory laid out like the TLC CloudFront bucket."""
    base = tmp_path_factory.mktemp("tlc")
    for month in (date(2025, 1, 1), date(2025, 2, 1)):
        make_month(base, month)
    return base


@pytest.fixture
def settings(tmp_path, remote, monkeypatch) -> Settings:
    monkeypatch.setenv("TAXI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TAXI_BASE_URL", remote.as_uri())  # urllib handles file:// URLs
    monkeypatch.setenv("TAXI_THREADS", "2")
    monkeypatch.delenv("TAXI_DB_PATH", raising=False)
    return Settings()


@pytest.fixture
def db(settings):
    database = Database(settings.db_path, settings.threads)
    yield database
    database.close()
