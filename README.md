# NYC Taxi Demand Analytics with DuckDB

Analyze New York City taxi and ride-hailing demand with **Python, DuckDB, FastAPI, and ECharts**. Explore trip patterns, neighborhood flows, fares, and unusual demand through an interactive dashboard and a read-only SQL lab.

Data comes from the monthly Parquet files and taxi zone lookup published by the [NYC Taxi & Limousine Commission (TLC)](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page).

![tests](https://github.com/lexiegao3-cyber/nyc-taxi-duckdb/actions/workflows/ci.yml/badge.svg)

## Highlights

- **Three service types:** yellow taxis, green taxis, and high-volume for-hire vehicles (including Uber and Lyft).
- **Live SQL analytics:** query the full trip table without pre-aggregated reporting tables.
- **One database-owning process:** concurrent reads and a single writer thread keep imports and dashboard queries in the same process.
- **Transparent queries:** inspect chart SQL and its `EXPLAIN ANALYZE` execution plan.
- **Repeatable imports:** reload a service/month transactionally without duplicating trips.
- **Offline tests:** synthetic TLC-shaped Parquet files exercise the download, import, and query pipeline.

## Quick start

Requires **Python 3.10+**. Run commands from the project directory.

```bash
git clone https://github.com/lexiegao3-cyber/nyc-taxi-duckdb.git
cd nyc-taxi-duckdb
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Start with a small, real dataset.
python -m taxi ingest --services green --months 2025-01
python -m taxi serve
```

Open **http://127.0.0.1:8000**. On Windows, activate the environment with `.venv\Scripts\activate`.

Alternatively, start the server with an empty database and import data from the dashboard's Data Import tab. The current dashboard labels are in Chinese; this README uses English descriptions of those tabs.

**Stop the server before running CLI imports or benchmarks.** While the server is running, use the dashboard to import data so that only one process owns the database file.

### Larger datasets

```bash
# Yellow and green taxis, January through March 2025.
python -m taxi ingest --services yellow,green --months 2025-01:2025-03

# Add ride-hailing data: roughly 20 million trips / 500 MB per month.
python -m taxi ingest --services yellow,green,fhvhv --months 2025-01:2025-03

# Download only, without importing.
python -m taxi download --services fhvhv --months 2025-01

# Benchmark the imported table against the original Parquet files.
python -m taxi bench

# Use a different local port.
python -m taxi serve --port 8001
```

## Dashboard

| Tab | What you can explore |
| --- | --- |
| Demand overview | Key metrics, daily trips and a seven-day moving average, weekday/hour heatmap, monthly company shares, and trip-distance distribution |
| Spatial analysis | Top pickup zones, borough-to-borough flows, popular origin/destination pairs, and JFK/LGA/EWR airport trip patterns |
| Operations and efficiency | Fare per mile/minute, tips, driver pay, wait times, congestion-fee coverage, hourly speeds, and ride-hailing wait-time P50/P90 |
| Event detection | Zone/hour demand anomalies against a leave-one-out baseline for the same zone, weekday, and hour |
| Benchmarks | DuckDB table queries compared with queries over the original Parquet files |
| SQL lab | Read-only queries with built-in examples and a 1,000-row response limit |
| Data import | Service/month selection, TLC downloads, and import progress |

Analyses support service, date-range, and pickup-borough filters. Anomaly scores identify unusual demand; they do not by themselves establish which event caused it.

## DuckDB performance

The original project reported the following results for January–March 2025 yellow taxi, green taxi, and ride-hailing data on an **Apple M4 laptop with 10 cores and 16 GB RAM**. These are reference measurements, not performance guarantees.

| Measurement | Reported result |
| --- | --- |
| Source Parquet data | 9 files, approximately 1.6 GB |
| Imported trips after cleaning | **71,402,965** |
| Import time | Approximately 9 seconds, excluding downloads |
| Full-table filtered count | 44 ms |
| Hourly aggregation | 77 ms |
| Zone-by-day aggregation | 159 ms |
| Exact median / P90 / P99 by service | 1.8 seconds |
| Zone/hour anomaly detection | Approximately 0.5 seconds |

Use the Benchmarks tab or `python -m taxi bench` to measure your own dataset. Results depend on hardware, DuckDB version, dataset size, and cache state. Retain the raw Parquet files for the direct-Parquet comparison.

DuckDB features used here include:

- **Native Parquet access:** `read_parquet()` and `parquet_file_metadata()`.
- **Vectorized, multithreaded execution** over the detailed trip table.
- **Analytical SQL:** window functions, `QUALIFY`, `GROUP BY ALL`, `FILTER`, exact `quantile_cont`, and distinct origin/destination counts.
- **MVCC snapshots:** readers can continue querying committed data during imports; see `test_reads_continue_during_writes` in `tests/test_db.py`.
- **Query profiling:** `EXPLAIN ANALYZE` for dashboard analyses.
- **SQL parse-tree validation:** the lab checks queries using `json_serialize_sql` and restricts accessible tables and table functions.

## Architecture

```text
Browser: HTML + JavaScript + ECharts
                  |
                 HTTP
                  |
python -m taxi serve  (single database-owning process)
  |
  +-- FastAPI request threads --> per-request DuckDB cursors
  |                               concurrent reads / MVCC snapshots
  |
  +-- Download pool (default: 3 threads) --> raw Parquet files
  |                                             |
  +-- Single writer thread <--------------------+
        BEGIN; DELETE service/month; INSERT normalized trips; COMMIT
```

- All writes go through `ThreadPoolExecutor(max_workers=1)`.
- Each service/month import is an atomic transaction, so repeated imports replace the previous data.
- Attempting a separate CLI import while the server owns the database produces a `DatabaseLockedError`.
- Successful writes increment `Database.version`; cached analysis results are keyed by this version and become stale automatically.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `TAXI_DATA_DIR` | `data` | Root directory for raw files and the database |
| `TAXI_DB_PATH` | `<TAXI_DATA_DIR>/taxi.duckdb` | Override the database file path |
| `TAXI_THREADS` | `0` (DuckDB chooses) | DuckDB worker thread count |
| `TAXI_MEMORY_LIMIT` | DuckDB default | Memory limit, for example `8GB` |
| `TAXI_DOWNLOAD_WORKERS` | `3` | Number of parallel download workers |
| `TAXI_BASE_URL` | `https://d37ci6vzurychx.cloudfront.net` | TLC source or a compatible mirror; also supports `file://` |

Example:

```bash
TAXI_MEMORY_LIMIT=8GB TAXI_THREADS=4 python -m taxi serve
```

## Data model and cleaning

`taxi/loader.py` normalizes all three service schemas into one `trips` table.

| Normalized field | Yellow / green taxis | High-volume for-hire vehicles |
| --- | --- | --- |
| `pickup_at` / `dropoff_at` | `tpep_*` / `lpep_*` timestamps | `pickup_datetime` / `dropoff_datetime` |
| `company` | Yellow / Green | Uber / Lyft / Via / Juno, mapped from `hvfhs_license_num` |
| `trip_miles` | `trip_distance` | `trip_miles` |
| `fare` | `fare_amount` | `base_passenger_fare` |
| `total` | `total_amount` | Fare, tolls, surcharges, taxes, and tips |
| `wait_min`, `driver_pay`, `shared` | Not available | Request-to-pickup wait, driver earnings, and shared-ride flag |

TLC schemas change across years, including `Airport_fee` versus `airport_fee` and the introduction of `cbd_congestion_fee` in 2025. Import SQL is generated from each file's actual columns, with missing fields represented as `NULL`.

Cleaning retains trips whose pickup falls within the source month, duration is between zero and six hours, location IDs are within 1–265, distance is within 0–200 miles, and total payment is nonnegative. The `ingested_files` table records source rows, loaded rows, file sizes, and import times.

## Project layout

```text
taxi/
  __main__.py    Command-line entry point
  config.py      Environment-based settings
  sources.py     TLC URLs, month parsing, and atomic downloads
  loader.py      Table definitions and schema normalization
  db.py          Single writer and concurrent read cursors
  ingest.py      Parallel download / serial import pipeline
  analytics.py   Analysis SQL and benchmark queries
  server.py      FastAPI routes and query validation
  static/        Dashboard HTML, JavaScript, and styles
tests/           Offline tests using generated Parquet fixtures
.github/         Continuous integration workflow
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

Tests generate TLC-shaped Parquet fixtures, including invalid records, and use `file://` sources to exercise the full pipeline without internet access. Coverage includes cleaning, idempotent imports, reads during writes, filtered analytics, cache invalidation, benchmarks, and SQL-lab restrictions.

## Roadmap

- Interactive taxi-zone maps with GeoJSON boundaries.
- Month-over-month and year-over-year comparisons.
- Weather and calendar joins to provide context for demand anomalies.
- CSV and Parquet exports for filtered analysis results.
- Download retries, duplicate-job protection, and persistent import history.
- English dashboard localization and shareable filter state.

## License

Code is available under the [MIT License](LICENSE). Trip data is provided by NYC TLC; consult the [official TLC data page](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page) for data documentation and applicable terms.
