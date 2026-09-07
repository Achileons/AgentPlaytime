"""SQLite persistence for AgentPlaytime runtime sessions.

Timestamps cross the storage boundary as timezone-aware :class:`datetime` values
and are stored as normalized UTC ISO-8601 strings.  Keeping serialization here
lets the tracker remain a small, independently testable state machine.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock

from agentplaytime.config import default_database_path


class StorageError(RuntimeError):
    """Base class for session persistence errors."""


class ActiveSessionExistsError(StorageError):
    """Raised when a tool already has an unfinished session."""


class SessionAlreadyEndedError(StorageError):
    """Raised when attempting to end an already-finished session."""


@dataclass(frozen=True, slots=True)
class Session:
    """One continuous interval during which a logical tool was running."""

    id: int
    tool: str
    started_at: datetime
    ended_at: datetime | None = None

    def duration(self, *, as_of: datetime | None = None) -> timedelta:
        """Return the session duration.

        ``as_of`` is required for an open session so callers cannot
        accidentally compute its duration using an implicit, hard-to-test
        wall-clock read.
        """

        end = self.ended_at
        if end is None:
            if as_of is None:
                raise ValueError("as_of is required for an open session")
            end = _as_utc(as_of)
        if end < self.started_at:
            raise ValueError("session end cannot be earlier than its start")
        return end - self.started_at


class Database:
    """SQLite-backed store for runtime sessions.

    Passing ``None`` uses the normal macOS Application Support location.
    ``":memory:"`` is supported for tests.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        resolved_path: str | Path = default_database_path() if path is None else path
        if str(resolved_path) != ":memory:":
            resolved_path = Path(resolved_path).expanduser()
            resolved_path.parent.mkdir(parents=True, exist_ok=True)

        self.path = resolved_path
        self._lock = RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            str(resolved_path),
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA foreign_keys = ON")
        if str(resolved_path) != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool TEXT NOT NULL CHECK(length(trim(tool)) > 0),
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    CHECK(ended_at IS NULL OR ended_at >= started_at)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_open_session_per_tool
                    ON sessions(tool)
                    WHERE ended_at IS NULL;

                CREATE INDEX IF NOT EXISTS sessions_tool_started_at
                    ON sessions(tool, started_at);
                """
            )

    def start_session(
        self,
        tool: str,
        *,
        started_at: datetime | None = None,
    ) -> Session:
        """Create and return a new open session for ``tool``."""

        normalized_tool = _normalize_tool(tool)
        start = _as_utc(started_at or datetime.now(UTC))
        with self._lock:
            self._ensure_open()
            try:
                with self._connection:
                    cursor = self._connection.execute(
                        "INSERT INTO sessions(tool, started_at) VALUES (?, ?)",
                        (normalized_tool, _serialize_timestamp(start)),
                    )
            except sqlite3.IntegrityError as error:
                open_session = self.get_open_session(normalized_tool)
                if open_session is not None:
                    raise ActiveSessionExistsError(
                        f"{normalized_tool!r} already has an open session"
                    ) from error
                raise StorageError("could not create session") from error

        return Session(
            id=int(cursor.lastrowid),
            tool=normalized_tool,
            started_at=start,
        )

    def end_session(
        self,
        session_id: int,
        *,
        ended_at: datetime | None = None,
    ) -> Session:
        """Finish ``session_id`` and return its updated value."""

        end = _as_utc(ended_at or datetime.now(UTC))
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT id, tool, started_at, ended_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown session id: {session_id}")

            session = _row_to_session(row)
            if session.ended_at is not None:
                raise SessionAlreadyEndedError(
                    f"session {session_id} has already ended"
                )
            if end < session.started_at:
                raise ValueError("ended_at cannot be earlier than started_at")

            with self._connection:
                self._connection.execute(
                    "UPDATE sessions SET ended_at = ? WHERE id = ?",
                    (_serialize_timestamp(end), session_id),
                )

        return Session(
            id=session.id,
            tool=session.tool,
            started_at=session.started_at,
            ended_at=end,
        )

    def get_open_session(self, tool: str) -> Session | None:
        """Return the open session for ``tool``, if it has one."""

        normalized_tool = _normalize_tool(tool)
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT id, tool, started_at, ended_at
                FROM sessions
                WHERE tool = ? AND ended_at IS NULL
                """,
                (normalized_tool,),
            ).fetchone()
        return None if row is None else _row_to_session(row)

    def open_sessions(self) -> list[Session]:
        """Return all unfinished sessions in deterministic tool order."""

        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT id, tool, started_at, ended_at
                FROM sessions
                WHERE ended_at IS NULL
                ORDER BY tool, id
                """
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    def list_sessions(self, *, tool: str | None = None) -> list[Session]:
        """Return persisted sessions ordered by start time and row id."""

        query = "SELECT id, tool, started_at, ended_at FROM sessions"
        parameters: tuple[str, ...] = ()
        if tool is not None:
            query += " WHERE tool = ?"
            parameters = (_normalize_tool(tool),)
        query += " ORDER BY started_at, id"

        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(query, parameters).fetchall()
        return [_row_to_session(row) for row in rows]

    def runtime_by_tool(
        self,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, timedelta]:
        """Aggregate runtime by tool, including open sessions through ``as_of``.

        If open sessions exist, an explicit ``as_of`` is required.  This keeps
        statistics deterministic for both the CLI and tests.
        """

        sessions = self.list_sessions()
        if any(session.ended_at is None for session in sessions) and as_of is None:
            raise ValueError("as_of is required while sessions are open")
        normalized_as_of = None if as_of is None else _as_utc(as_of)

        totals: dict[str, timedelta] = {}
        for session in sessions:
            totals[session.tool] = totals.get(session.tool, timedelta()) + session.duration(
                as_of=normalized_as_of
            )
        return dict(sorted(totals.items()))

    def close(self) -> None:
        """Close the SQLite connection; repeated calls are harmless."""

        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise StorageError("database is closed")

    def __enter__(self) -> Database:
        self._ensure_open()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def _normalize_tool(tool: str) -> str:
    if not isinstance(tool, str):
        raise TypeError("tool must be a string")
    normalized = tool.strip()
    if not normalized:
        raise ValueError("tool must not be empty")
    return normalized


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _serialize_timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _deserialize_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _row_to_session(row: sqlite3.Row) -> Session:
    ended_at = row["ended_at"]
    return Session(
        id=int(row["id"]),
        tool=str(row["tool"]),
        started_at=_deserialize_timestamp(str(row["started_at"])),
        ended_at=None if ended_at is None else _deserialize_timestamp(str(ended_at)),
    )


def iter_session_seconds(
    sessions: Iterator[Session],
    *,
    as_of: datetime | None = None,
) -> Iterator[tuple[str, float]]:
    """Yield ``(tool, seconds)`` pairs for callers building custom reports."""

    for session in sessions:
        yield session.tool, session.duration(as_of=as_of).total_seconds()
