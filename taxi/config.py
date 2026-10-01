"""Runtime configuration, overridable through environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("TAXI_DATA_DIR", "data")))
    # 0 = let DuckDB pick (all cores)
    threads: int = field(default_factory=lambda: _env_int("TAXI_THREADS", 0))
    memory_limit: str = field(default_factory=lambda: os.environ.get("TAXI_MEMORY_LIMIT", ""))
    download_workers: int = field(default_factory=lambda: _env_int("TAXI_DOWNLOAD_WORKERS", 3))
    base_url: str = field(
        default_factory=lambda: os.environ.get(
            "TAXI_BASE_URL", "https://d37ci6vzurychx.cloudfront.net"
        )
    )

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def db_path(self) -> Path:
        return Path(os.environ.get("TAXI_DB_PATH", self.data_dir / "taxi.duckdb"))


def get_settings() -> Settings:
    return Settings()
