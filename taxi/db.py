"""The single-writer database owner.

DuckDB allows one process to open a database file read-write. Inside that
process, many threads may read concurrently through `connection.cursor()`
(each gets its own MVCC snapshot), but we funnel *all* writes through one
dedicated writer thread. That gives us:

* no cross-process file-lock conflicts (only the server process opens the file),
* no write-write transaction conflicts (writes are serialised),
* readers never block: they keep seeing the last committed snapshot while a
  month of data is being loaded.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import duckdb

from . import loader
from .sources import MonthFile

log = logging.getLogger(__name__)


class DatabaseLockedError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path | str, threads: int = 0, memory_limit: str = ""):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        config: dict[str, Any] = {"memory_limit": memory_limit} if memory_limit else {}
        if threads:
            config["threads"] = threads
        try:
            self._con = duckdb.connect(str(path), config=config)
        except duckdb.IOException as exc:  # another process holds the file lock
            raise DatabaseLockedError(
                f"{path} is opened by another process (is `python -m taxi serve` running?). "
                "Send writes to the running server instead, e.g. POST /api/ingest."
            ) from exc
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="duckdb-writer")
        self._version_lock = threading.Lock()
        self.version = 0  # bumped after every committed write; used as a cache key
        self.write(self._init_schema).result()

    # ------------------------------------------------------------------ writes
    def write(self, fn: Callable[[duckdb.DuckDBPyConnection], Any]) -> Future:
        """Run fn(cursor) on the writer thread. Returns a Future."""

        def run():
            cur = self._con.cursor()
            try:
                result = fn(cur)
            finally:
                cur.close()
            with self._version_lock:
                self.version += 1
            return result

        return self._writer.submit(run)

    @staticmethod
    def _init_schema(cur: duckdb.DuckDBPyConnection) -> None:
        cur.execute(loader.TRIPS_DDL)
        cur.execute(loader.META_DDL)

    def load_zones(self, csv_path: Path) -> Future:
        def fn(cur):
            cur.execute("BEGIN")
            cur.execute("DELETE FROM zones")
            cur.execute(
                """
                INSERT INTO zones
                SELECT LocationID, Borough, Zone, service_zone
                FROM read_csv(?, header = true)
                """,
                [str(csv_path)],
            )
            cur.execute("COMMIT")
            return cur.execute("SELECT count(*) FROM zones").fetchone()[0]

        return self.write(fn)

    def load_month(self, mf: MonthFile, parquet_path: Path) -> Future:
        """Replace one (service, month) slice atomically from a local Parquet file."""
        return self.write(self.load_month_fn(mf, parquet_path))

    @staticmethod
    def load_month_fn(mf: MonthFile, parquet_path: Path) -> Callable:
        """The writer-thread function behind load_month (exposed for job tracking)."""

        def fn(cur):
            t0 = time.perf_counter()
            path = str(parquet_path)
            cols = {
                r[0].lower()
                for r in cur.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)", [path]
                ).fetchall()
            }
            raw_rows = cur.execute(
                "SELECT sum(num_rows) FROM parquet_file_metadata(?)",
                [path],
            ).fetchone()[0]
            source = "read_parquet('" + path.replace("'", "''") + "')"
            select = loader.select_sql(mf.service, cols, source, mf.month)
            cur.execute("BEGIN")
            try:
                cur.execute(
                    "DELETE FROM trips WHERE service = ? AND source_month = ?",
                    [mf.service, mf.month],
                )
                cur.execute(f"INSERT INTO trips {select}")
                loaded = cur.execute(
                    "SELECT count(*) FROM trips WHERE service = ? AND source_month = ?",
                    [mf.service, mf.month],
                ).fetchone()[0]
                secs = time.perf_counter() - t0
                cur.execute(
                    """
                    INSERT OR REPLACE INTO ingested_files
                        (service, month, file_name, file_bytes, raw_rows, loaded_rows, seconds)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [mf.service, mf.month, mf.file_name, parquet_path.stat().st_size,
                     raw_rows, loaded, secs],
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
            log.info("loaded %s: %s/%s rows in %.1fs", mf.file_name, loaded, raw_rows, secs)
            return {"raw_rows": raw_rows, "loaded_rows": loaded, "seconds": round(secs, 2)}

        return fn

    # ------------------------------------------------------------------- reads
    def query(self, sql: str, params: list | None = None) -> tuple[list[str], list[tuple], float]:
        """Run a read query on a fresh cursor (safe from any thread)."""
        cur = self._con.cursor()
        try:
            t0 = time.perf_counter()
            rel = cur.execute(sql, params or [])
            rows = rel.fetchall()
            ms = (time.perf_counter() - t0) * 1000
            cols = [d[0] for d in rel.description] if rel.description else []
            return cols, rows, ms
        finally:
            cur.close()

    def records(self, sql: str, params: list | None = None) -> tuple[list[dict], float]:
        cols, rows, ms = self.query(sql, params)
        return [dict(zip(cols, r)) for r in rows], ms

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self._con.cursor()

    def close(self) -> None:
        self._writer.shutdown(wait=True)
        self._con.close()
