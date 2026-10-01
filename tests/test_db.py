import threading
from datetime import date

from conftest import N
from taxi.ingest import Ingestor
from taxi.sources import plan


def ingest(db, settings, services=("yellow", "green", "fhvhv"), months=(date(2025, 1, 1),)):
    job = Ingestor(db, settings).run_blocking(plan(list(services), list(months)), echo=lambda *_: None)
    assert all(s.state == "done" for s in job.status.values()), job.to_dict()
    return job


def test_ingest_normalises_and_cleans(db, settings):
    job = ingest(db, settings)
    assert {s.rows for s in job.status.values()} == {N}  # 3 bad rows dropped per file
    rows, _ = db.records(
        "SELECT service, company, count(*) AS n, min(duration_min) AS min_dur, "
        "count(wait_min) AS waits, sum(total) > 0 AS paid FROM trips GROUP BY ALL ORDER BY ALL")
    by = {(r["service"], r["company"]): r for r in rows}
    assert set(by) == {("fhvhv", "Lyft"), ("fhvhv", "Uber"), ("green", "Green"), ("yellow", "Yellow")}
    assert by[("fhvhv", "Lyft")]["n"] + by[("fhvhv", "Uber")]["n"] == N
    assert by[("yellow", "Yellow")]["waits"] == 0 and by[("fhvhv", "Uber")]["waits"] > 0
    assert all(r["min_dur"] > 0 and r["paid"] for r in rows)
    (meta,), _ = db.records("SELECT raw_rows, loaded_rows FROM ingested_files WHERE service = 'yellow'")
    assert meta == {"raw_rows": N + 3, "loaded_rows": N}
    (z,), _ = db.records("SELECT count(*) AS n FROM zones")
    assert z["n"] == 7


def test_reingest_is_idempotent(db, settings):
    ingest(db, settings, services=("yellow",))
    ingest(db, settings, services=("yellow",))
    (r,), _ = db.records("SELECT count(*) AS n FROM trips")
    assert r["n"] == N


def test_reads_continue_during_writes(db, settings):
    """Readers on cursors keep working (and see consistent snapshots) while the writer loads."""
    ingest(db, settings, services=("yellow",))
    stop, seen, errors = threading.Event(), set(), []

    def reader():
        while not stop.is_set():
            try:
                (r,), _ = db.records("SELECT count(*) AS n FROM trips")
                seen.add(r["n"])
            except Exception as exc:  # pragma: no cover - would fail the test
                errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    ingest(db, settings, services=("green", "fhvhv"), months=(date(2025, 1, 1), date(2025, 2, 1)))
    stop.set()
    for t in threads:
        t.join()
    assert not errors
    # every observed count is a whole number of committed files - never a partial load
    assert all(n % N == 0 for n in seen)
    assert max(seen) <= 5 * N
