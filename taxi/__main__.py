"""Command line: python -m taxi {serve,ingest,download,bench}."""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor

from .config import get_settings
from .sources import SERVICES, download, expand_months, plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m taxi", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the web UI (the single DuckDB writer process)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    for name, help_ in [("ingest", "download + load into DuckDB (server must be stopped)"),
                        ("download", "only download Parquet files")]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--services", default="yellow,green",
                       help=f"comma separated, from {','.join(SERVICES)}")
        p.add_argument("--months", required=True, help="e.g. 2025-01:2025-06 or 2025-01,2025-03")

    sub.add_parser("bench", help="run the benchmark suite against the local database")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()

    if args.cmd == "serve":
        import uvicorn

        from .server import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return 0

    if args.cmd in ("ingest", "download"):
        files = plan([x for x in args.services.split(",") if x], expand_months(args.months))
        if args.cmd == "download":
            with ThreadPoolExecutor(settings.download_workers) as pool:
                for path in pool.map(
                    lambda mf: download(mf.url(settings.base_url), mf.local_path(settings.raw_dir)),
                    files,
                ):
                    print(path)
            return 0

        from .db import Database, DatabaseLockedError
        from .ingest import Ingestor

        try:
            db = Database(settings.db_path, settings.threads, settings.memory_limit)
        except DatabaseLockedError as exc:
            print(exc, file=sys.stderr)
            return 2
        ingestor = Ingestor(db, settings)
        job = ingestor.run_blocking(files)
        ingestor.shutdown()
        db.close()
        return 0 if all(s.state == "done" for s in job.status.values()) else 1

    if args.cmd == "bench":
        from .db import Database
        from .server import run_bench

        db = Database(settings.db_path, settings.threads, settings.memory_limit)
        result = run_bench(db, settings)
        db.close()
        print(f"rows: {result['rows']:,}")
        print(f"{'query':<16}{'table ms':>12}{'parquet ms':>12}{'rows/s':>16}")
        for r in result["results"]:
            print(f"{r['query']:<16}{r['table_ms']:>12}{r.get('parquet_ms', '-'):>12}"
                  f"{r['rows_per_sec'] or 0:>16,}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
