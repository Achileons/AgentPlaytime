"""Application-wide defaults for AgentPlaytime."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path


APP_NAME = "AgentPlaytime"
DATABASE_FILENAME = "agentplaytime.db"
DEFAULT_POLL_INTERVAL_SECONDS = 2.0


def default_application_support_dir(home: Path | None = None) -> Path:
    """Return AgentPlaytime's conventional per-user data directory on macOS."""

    user_home = Path.home() if home is None else Path(home).expanduser()
    return user_home / "Library" / "Application Support" / APP_NAME


def default_database_path(home: Path | None = None) -> Path:
    """Return the default path of the local SQLite database."""

    return default_application_support_dir(home) / DATABASE_FILENAME


DEFAULT_APPLICATION_SUPPORT_DIR = default_application_support_dir()
DEFAULT_DATABASE_PATH = default_database_path()


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration for the tracker and storage layer."""

    database_path: Path = field(default_factory=default_database_path)
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        database_path = Path(self.database_path).expanduser()
        poll_interval = float(self.poll_interval_seconds)

        if not isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval_seconds must be a finite value greater than zero")

        object.__setattr__(self, "database_path", database_path)
        object.__setattr__(self, "poll_interval_seconds", poll_interval)
