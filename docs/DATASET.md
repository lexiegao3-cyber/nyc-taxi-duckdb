# Recommended dataset: NYC TLC, January–March 2025

Use the [official NYC TLC Trip Record Data page](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page). Select **2025**, then January, February and March. For each month download:

1. Yellow Taxi Trip Records (`yellow`).
2. Green Taxi Trip Records (`green`).
3. **High Volume For-Hire Vehicle Trip Records** (`fhvhv`), which includes Uber/Lyft. Do not substitute the separate, less detailed FHV dataset.

The application also uses the [official taxi-zone lookup CSV](https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv).

## Download links

| Month | Yellow | Green | High-volume FHV |
| --- | --- | --- | --- |
| 2025-01 | [Parquet](https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2025-01.parquet) | [Parquet](https://d37ci6vzurychx.cloudfront.net/trip-data/green_tripdata_2025-01.parquet) | [Parquet](https://d37ci6vzurychx.cloudfront.net/trip-data/fhvhv_tripdata_2025-01.parquet) |
| 2025-02 | [Parquet](https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2025-02.parquet) | [Parquet](https://d37ci6vzurychx.cloudfront.net/trip-data/green_tripdata_2025-02.parquet) | [Parquet](https://d37ci6vzurychx.cloudfront.net/trip-data/fhvhv_tripdata_2025-02.parquet) |
| 2025-03 | [Parquet](https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2025-03.parquet) | [Parquet](https://d37ci6vzurychx.cloudfront.net/trip-data/green_tripdata_2025-03.parquet) | [Parquet](https://d37ci6vzurychx.cloudfront.net/trip-data/fhvhv_tripdata_2025-03.parquet) |

This quarter is a reproducible historical demo, not a live dispatch dataset or a claim to be the latest release. It is compatible with the existing loader and provides a common three-month window across all three services. Comparing 2025 Q1 months alone cannot establish the effect of congestion pricing or support year-over-year conclusions.

## Import automatically

With the server stopped:

```bash
python -m taxi ingest --services yellow,green,fhvhv --months 2025-01:2025-03
python -m taxi serve --port 8001
```

If the server is already running, use **Data Import** or its API instead:

```bash
curl -X POST http://127.0.0.1:8001/api/ingest \
  -H 'Content-Type: application/json' \
  -d '{"services":["yellow","green","fhvhv"],"months":"2025-01:2025-03"}'
```

Imports replace each service/month atomically. The original January green-taxi slice is updated without duplicate rows; unrelated months are retained. The nine raw files require approximately 1.65 GB; the locally verified database is approximately 3.17 GB. Allow additional disk space for temporary files and database growth. Raw files and the database are ignored by Git.

## Verified local import

| Month | Yellow retained trips | Green retained trips | High-volume FHV retained trips |
| --- | ---: | ---: | ---: |
| 2025-01 | 3,408,810 | 47,829 | 20,405,004 |
| 2025-02 | 3,515,897 | 45,935 | 19,339,316 |
| 2025-03 | 4,052,722 | 50,754 | 20,536,698 |
| **Total** | **10,977,429** | **144,518** | **60,281,018** |

**71,402,965 retained trips in total**, with 265 zone lookup entries. All nine service/month slices passed calendar-date coverage and manifest-count checks. This is an application validation result, not a guarantee that the source captures every real trip.

[dataset-2025-q1.json](dataset-2025-q1.json) records each downloaded URL, byte size, SHA-256 hash, raw rows and retained rows. Upstream files can be revised; compare hashes when reproducing results.
