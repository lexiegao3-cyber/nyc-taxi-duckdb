"""Download-then-load pipeline.

Downloads run in parallel (network bound, touch only their own files);
each finished file is handed to the database's single writer thread, so
loading is serialised while the next downloads keep going.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from .config import Settings
from .db import Database
from .sources import ZONE_LOOKUP_PATH, MonthFile, download

log = logging.getLogger(__name__)

_ids = itertools.count(1)


@dataclass
class FileStatus:
    file_name: str
    state: str = "pending"  # pending | downloading | queued | loading | done | error
    downloaded: int = 0
    size: int = 0
    rows: int | None = None
    seconds: float | None = None
    error: str | None = None


@dataclass
class IngestJob:
    files: list[MonthFile]
    id: int = field(default_factory=lambda: next(_ids))
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    status: dict[str, FileStatus] = field(default_factory=dict)
    finished: bool = False
    started: float = field(default_factory=time.perf_counter)
    elapsed: float = 0.0

    def __post_init__(self):
        self.status = {f.file_name: FileStatus(f.file_name) for f in self.files}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "finished": self.finished,
            "elapsed": round(self.elapsed or time.perf_counter() - self.started, 1),
            "files": [vars(s) for s in self.status.values()],
        }


def ensure_zones(db: Database, settings: Settings) -> int:
    path = settings.raw_dir / "taxi_zone_lookup.csv"
    download(f"{settings.base_url}/{ZONE_LOOKUP_PATH}", path)
    return db.load_zones(path).result()


class Ingestor:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.jobs: dict[int, IngestJob] = {}
        self._lock = threading.Lock()
        self._downloads = ThreadPoolExecutor(
            max_workers=settings.download_workers, thread_name_prefix="download"
        )

    def submit(self, files: list[MonthFile]) -> IngestJob:
        job = IngestJob(files)
        with self._lock:
            self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job: IngestJob) -> None:
        try:
            if not self.db.records("SELECT 1 FROM zones LIMIT 1")[0]:
                ensure_zones(self.db, self.settings)
        except Exception as exc:  # zones are optional for loading trips
            log.warning("zone lookup failed: %s", exc)
        downloads = [self._downloads.submit(self._fetch, job, mf) for mf in job.files]
        for d in downloads:
            if (loaded := d.result()) is not None:
                loaded.wait()
        job.finished = True
        job.elapsed = time.perf_counter() - job.started

    def _fetch(self, job: IngestJob, mf: MonthFile) -> threading.Event | None:
        """Download one file, then queue it on the writer.

        Returns an Event that is set once the load finished and its status is final."""
        st = job.status[mf.file_name]
        try:
            st.state = "downloading"

            def progress(done, total):
                st.downloaded, st.size = done, total

            path = download(mf.url(self.settings.base_url), mf.local_path(self.settings.raw_dir),
                            progress)
            st.size = st.downloaded = path.stat().st_size
            st.state = "queued"

            def mark_loading(cur):
                st.state = "loading"
                return load(cur)

            load = self.db.load_month_fn(mf, path)
            loaded = threading.Event()
            fut = self.db.write(mark_loading)
            fut.add_done_callback(lambda f: (self._finish(st, f), loaded.set()))
            return loaded
        except Exception as exc:
            log.exception("download of %s failed", mf.file_name)
            st.state, st.error = "error", str(exc)[:500]
            return None

    @staticmethod
    def _finish(st: FileStatus, load) -> None:
        try:
            result = load.result()
            st.rows, st.seconds, st.state = result["loaded_rows"], result["seconds"], "done"
        except Exception as exc:
            log.exception("load of %s failed", st.file_name)
            st.state, st.error = "error", str(exc)[:500]

    def run_blocking(self, files: list[MonthFile], echo=print) -> IngestJob:
        job = self.submit(files)
        last = None
        while not job.finished:
            line = " | ".join(f"{mf.service}-{mf.month:%Y-%m}:{job.status[mf.file_name].state}"
                              for mf in job.files)
            if line != last:
                echo(line)
                last = line
            time.sleep(0.2)
        for s in job.status.values():
            echo(f"{s.file_name}: {s.state} rows={s.rows} seconds={s.seconds} {s.error or ''}")
        return job

    def shutdown(self):
        self._downloads.shutdown(wait=False, cancel_futures=True)
