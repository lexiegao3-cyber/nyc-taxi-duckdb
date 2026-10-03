"""FastAPI app: the one process that owns the DuckDB file.

Reads run on FastAPI's thread pool, each on its own DuckDB cursor; writes
(ingest) go through Database.write -> the single writer thread.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analytics, loader
from .agent import AgentBusy, AgentUnavailable, BusinessAgent
from .config import Settings, get_settings
from .db import Database
from .ingest import Ingestor
from .localization import translate
from .sources import SERVICES, MonthFile, expand_months, plan

STATIC = Path(__file__).parent / "static"
MAX_LAB_ROWS = 1000


class IngestRequest(BaseModel):
    services: list[str]
    months: str  # "2025-01:2025-03"


class AgentRequest(BaseModel):
    end_month: str = Field(pattern=r"^\d{4}-\d{2}$")
    services: list[str] = Field(default_factory=lambda: list(SERVICES), min_length=1, max_length=3)
    borough: str | None = None
    mode: Literal["brief", "ai"] = "brief"
    question: str = Field(default="", max_length=2000)
    language: Literal["zh", "en"] = "zh"


class SqlRequest(BaseModel):
    sql: str


LAB_TABLES = {"trips", "zones", "ingested_files"}
LAB_TABLE_FUNCTIONS = {"range", "generate_series", "unnest"}


def disallowed_sources(db: Database, sql: str) -> list[str]:
    """Walk DuckDB's own parse tree and list every table / table function that the
    query lab must not touch (files via replacement scans, read_csv, ...)."""
    (tree,), _ = db.records("SELECT json_serialize_sql(?::VARCHAR) AS t", [sql])
    tree = json.loads(tree["t"])
    tables, funcs, ctes = [], [], set()

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "BASE_TABLE":
                tables.append((node.get("schema_name") or "", node.get("table_name")))
            elif node.get("type") == "TABLE_FUNCTION":
                funcs.append((node.get("function") or {}).get("function_name"))
            for entry in (node.get("cte_map") or {}).get("map", []):
                ctes.add(entry.get("key", "").lower())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(tree)
    bad = [f"{s}.{t}" if s else t for s, t in tables
           if (t or "").lower() not in LAB_TABLES | ctes or s not in ("", "main")]
    bad += [f + "()" for f in funcs if (f or "").lower() not in LAB_TABLE_FUNCTIONS]
    return bad


class _Cache:
    """Tiny LRU keyed on the DB write version, so results invalidate after ingest."""

    def __init__(self, size: int = 256):
        self.size, self.data, self.lock = size, OrderedDict(), threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.data:
                self.data.move_to_end(key)
                return self.data[key]
        return None

    def put(self, key, value):
        with self.lock:
            self.data[key] = value
            self.data.move_to_end(key)
            while len(self.data) > self.size:
                self.data.popitem(last=False)


def parquet_relation(db: Database, settings: Settings) -> str | None:
    """All ingested raw files, normalised on the fly - queried in place, no loading."""
    files, _ = db.records("SELECT service, month FROM ingested_files ORDER BY month")
    parts = []
    for r in files:
        mf = MonthFile(r["service"], r["month"])
        path = mf.local_path(settings.raw_dir)
        if not path.exists():
            continue
        names = {row[0] for row in db.query(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)])[1]}
        src = "read_parquet('" + str(path).replace("'", "''") + "')"
        parts.append("(" + loader.select_sql(mf.service, names, src, mf.month) + ")")
    return "(" + "\nUNION ALL\n".join(parts) + ")" if parts else None


def run_bench(db: Database, settings: Settings, parquet: bool = True) -> dict:
    (n,), _ = db.records("SELECT count(*) AS n FROM trips")
    rel_pq = parquet_relation(db, settings) if parquet else None
    out = []
    for label, tmpl in analytics.BENCHMARKS:
        _, _, ms_table = db.query(tmpl.format(trips="trips"))
        row = {"query": label, "sql": tmpl.format(trips="trips"),
               "table_ms": round(ms_table, 1),
               "rows_per_sec": round(n["n"] / (ms_table / 1000)) if ms_table else None}
        if rel_pq:
            _, _, ms_pq = db.query(tmpl.format(trips=rel_pq))
            row["parquet_ms"] = round(ms_pq, 1)
        out.append(row)
    return {"rows": n["n"], "results": out}


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    owns_db = db is None
    db = db or Database(settings.db_path, settings.threads, settings.memory_limit)
    ingestor = Ingestor(db, settings)
    cache = _Cache()
    business_agent = BusinessAgent(db)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        ingestor.shutdown()
        if owns_db:
            db.close()

    app = FastAPI(title="NYC Taxi Demand · DuckDB", lifespan=lifespan)
    app.state.db, app.state.ingestor = db, ingestor

    def filters(services: str | None, start: date | None, end: date | None,
                borough: str | None) -> analytics.Filters:
        try:
            svc = tuple(s for s in (services or ",".join(SERVICES)).split(",") if s)
            return analytics.Filters(svc, start, end, borough or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/agent/status")
    def agent_status():
        return business_agent.status()

    @app.post("/api/agent/run")
    def agent_run(req: AgentRequest):
        try:
            return business_agent.run(req.end_month, req.services, req.borough or None,
                                      req.mode, req.question, req.language)
        except AgentBusy as exc:
            raise HTTPException(409, translate(str(exc), req.language)) from exc
        except AgentUnavailable as exc:
            raise HTTPException(503, translate(str(exc), req.language)) from exc
        except TimeoutError as exc:
            raise HTTPException(504, translate(str(exc), req.language)) from exc
        except ValueError as exc:
            raise HTTPException(400, translate(str(exc), req.language)) from exc

    @app.get("/api/status")
    def status():
        files, _ = db.records(
            "SELECT service, strftime(month, '%Y-%m') AS month, raw_rows, loaded_rows, "
            "round(file_bytes / 1e6, 1) AS mb, round(seconds, 2) AS seconds, "
            "round(loaded_rows / nullif(seconds, 0)) AS rows_per_sec "
            "FROM ingested_files ORDER BY month, service"
        )
        (info,), _ = db.records(
            "SELECT (SELECT count(*) FROM trips) AS trips, "
            "(SELECT min(pickup_at)::DATE FROM trips) AS first_day, "
            "(SELECT max(pickup_at)::DATE FROM trips) AS last_day, "
            "(SELECT count(*) FROM zones) AS zones, "
            "version() AS duckdb_version, current_setting('threads') AS threads, "
            "current_setting('memory_limit') AS memory_limit"
        )
        size = db.path.stat().st_size if db.path.exists() else 0
        return {**info, "db_mb": round(size / 1e6, 1), "files": files,
                "services": list(SERVICES), "boroughs": analytics.BOROUGHS,
                "analyses": {k: {"title": v["title"], "doc": v["doc"]}
                             for k, v in analytics.ANALYSES.items()}}

    @app.get("/api/analysis/{name}")
    def run_analysis(name: str, services: str | None = None, start: date | None = None,
                     end: date | None = None, borough: str | None = None):
        f = filters(services, start, end, borough)
        try:
            sql, params = analytics.build(name, f)
        except KeyError:
            raise HTTPException(404, f"unknown analysis {name!r}")
        key = (name, f, db.version)
        if (hit := cache.get(key)) is not None:
            return {**hit, "cached": True}
        rows, ms = db.records(sql, params)
        result = {"name": name, "rows": rows, "ms": round(ms, 1), "sql": sql.strip()}
        cache.put(key, result)
        return {**result, "cached": False}

    @app.post("/api/sql")
    def run_sql(req: SqlRequest):
        """Query lab: one read-only SELECT statement, capped at MAX_LAB_ROWS rows."""
        try:
            stmts = duckdb.extract_statements(req.sql)
        except duckdb.Error as exc:
            raise HTTPException(400, str(exc)) from exc
        if len(stmts) != 1 or stmts[0].type != duckdb.StatementType.SELECT:
            raise HTTPException(400, "只允许单条 SELECT 语句")
        if bad := disallowed_sources(db, req.sql):
            raise HTTPException(400, f"查询实验室只能读取 trips / zones / ingested_files，不允许：{bad}")
        cur = db.cursor()
        try:
            t0 = time.perf_counter()
            rel = cur.execute(stmts[0].query)
            rows = rel.fetchmany(MAX_LAB_ROWS + 1)
            ms = (time.perf_counter() - t0) * 1000
            cols = [d[0] for d in rel.description]
        except duckdb.Error as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            cur.close()
        return {"columns": cols, "rows": [list(r) for r in rows[:MAX_LAB_ROWS]],
                "truncated": len(rows) > MAX_LAB_ROWS, "ms": round(ms, 1)}

    @app.get("/api/explain/{name}")
    def explain(name: str, services: str | None = None, start: date | None = None,
                end: date | None = None, borough: str | None = None):
        f = filters(services, start, end, borough)
        try:
            sql, params = analytics.build(name, f)
        except KeyError:
            raise HTTPException(404, f"unknown analysis {name!r}")
        cols, rows, ms = db.query("EXPLAIN ANALYZE " + sql, params)
        return {"plan": rows[0][-1], "ms": round(ms, 1)}

    @app.get("/api/bench")
    def bench(parquet: bool = Query(True, description="also run directly on raw Parquet")):
        return run_bench(db, settings, parquet)

    @app.post("/api/ingest")
    def ingest(req: IngestRequest):
        try:
            files = plan(req.services, expand_months(req.months))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not files or len(files) > 60:
            raise HTTPException(400, "一次最多导入 60 个文件")
        return ingestor.submit(files).to_dict()

    @app.get("/api/jobs")
    def jobs():
        return [j.to_dict() for j in sorted(ingestor.jobs.values(), key=lambda j: -j.id)]

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
