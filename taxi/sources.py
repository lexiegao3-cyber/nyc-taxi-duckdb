"""Where the TLC data lives and how to fetch it.

The download page (https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page)
links every monthly Parquet file and the taxi zone lookup table to a CloudFront
bucket with a stable naming scheme, so URLs can be built instead of scraped.
"""

from __future__ import annotations

import re
import shutil
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# service -> file prefix on the TLC bucket
SERVICES = {
    "yellow": "yellow_tripdata",
    "green": "green_tripdata",
    "fhvhv": "fhvhv_tripdata",  # Uber / Lyft / Via (high-volume for-hire)
}

ZONE_LOOKUP_PATH = "misc/taxi_zone_lookup.csv"

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


@dataclass(frozen=True)
class MonthFile:
    service: str
    month: date  # first day of the month

    @property
    def file_name(self) -> str:
        return f"{SERVICES[self.service]}_{self.month:%Y-%m}.parquet"

    def url(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/trip-data/{self.file_name}"

    def local_path(self, raw_dir: Path) -> Path:
        return raw_dir / self.service / self.file_name


def parse_month(text: str) -> date:
    m = _MONTH_RE.match(text.strip())
    if not m:
        raise ValueError(f"month must look like YYYY-MM, got {text!r}")
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        raise ValueError(f"invalid month: {text!r}")
    return date(year, month, 1)


def expand_months(spec: str) -> list[date]:
    """'2025-01:2025-03' or '2025-01,2025-06' -> list of month starts."""
    months: list[date] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            start_s, end_s = part.split(":", 1)
            start, end = parse_month(start_s), parse_month(end_s)
            if end < start:
                raise ValueError(f"range end before start: {part!r}")
            cur = start
            while cur <= end:
                months.append(cur)
                cur = date(cur.year + cur.month // 12, cur.month % 12 + 1, 1)
        else:
            months.append(parse_month(part))
    return sorted(set(months))


def plan(services: list[str], months: list[date]) -> list[MonthFile]:
    unknown = [s for s in services if s not in SERVICES]
    if unknown:
        raise ValueError(f"unknown service(s) {unknown}; choose from {list(SERVICES)}")
    return [MonthFile(s, m) for m in months for s in services]


def download(url: str, dest: Path, progress=None, timeout: int = 60) -> Path:
    """Download url to dest atomically (via a .part file). Skips existing files."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "nyc-taxi-duckdb/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    shutil.move(tmp, dest)
    return dest
