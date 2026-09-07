"""Polling tracker state machine.

The engine only consumes logical tool names.  Process and application
deduplication belongs to detectors, while the conversion from an iterable to a
set here provides a final guard against duplicate instances.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Literal, Protocol, TypeAlias

from agentplaytime.config import DEFAULT_POLL_INTERVAL_SECONDS
from agentplaytime.storage import Database, Session


EventAction: TypeAlias = Literal["start", "stop"]
ToolDetector: TypeAlias = Callable[[], Iterable[str]]
EventCallback: TypeAlias = Callable[["TrackingEvent"], None]


class StopSignal(Protocol):
    """Subset of :class:`threading.Event` used by :meth:`TrackingEngine.run`."""

    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


@dataclass(frozen=True, slots=True)
class TrackingEvent:
    """A persisted transition suitable for CLI event output."""

    action: EventAction
    tool: str
    session_id: int
    started_at: datetime
    ended_at: datetime | None = None

    @property
    def duration(self) -> timedelta | None:
        """Return the completed duration for a STOP event."""

        if self.ended_at is None:
            return None
        return self.ended_at - self.started_at

    @property
    def duration_seconds(self) -> float | None:
        """Return completed seconds for a STOP event, otherwise ``None``."""

        duration = self.duration
        return None if duration is None else duration.total_seconds()


class TrackingEngine:
    """Reconcile detector snapshots with persisted runtime sessions."""

    def __init__(
        self,
        database: Database,
        detector: ToolDetector | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._detector = detector
        self._clock = clock or (lambda: datetime.now(UTC))
        self._active: dict[str, Session] = {
            session.tool: session for session in database.open_sessions()
        }
        self._closed = False

    @property
    def active_tools(self) -> frozenset[str]:
        """Logical tools that currently have an open session."""

        return frozenset(self._active)

    @property
    def is_closed(self) -> bool:
        return self._closed

    def reconcile(
        self,
        running_tools: Iterable[str],
        *,
        observed_at: datetime | None = None,
    ) -> list[TrackingEvent]:
        """Persist changes from one detector snapshot.

        STOP events are emitted first, followed by START events.  Tools within
        each group are sorted by name, making output and tests deterministic.
        Duplicate names in ``running_tools`` collapse to one logical timer.
        """

        if self._closed:
            raise RuntimeError("tracking engine is closed")

        observed = _observed_time(observed_at or self._clock())
        running = _logical_tool_set(running_tools)
        previously_running = set(self._active)
        stopped_tools = sorted(previously_running - running)
        started_tools = sorted(running - previously_running)
        events: list[TrackingEvent] = []

        for tool in stopped_tools:
            session = self._database.end_session(
                self._active[tool].id,
                ended_at=observed,
            )
            del self._active[tool]
            events.append(_stop_event(session))

        for tool in started_tools:
            session = self._database.start_session(tool, started_at=observed)
            self._active[tool] = session
            events.append(_start_event(session))

        return events

    def poll_once(self, *, observed_at: datetime | None = None) -> list[TrackingEvent]:
        """Run the configured detector once and reconcile its snapshot."""

        if self._detector is None:
            raise RuntimeError("no detector was configured")
        return self.reconcile(self._detector(), observed_at=observed_at)

    def run(
        self,
        detector: ToolDetector | None = None,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        on_event: EventCallback | None = None,
        stop_event: StopSignal | None = None,
    ) -> None:
        """Poll until interrupted or ``stop_event`` is set.

        Active sessions are always stopped in ``finally``.  A
        :class:`threading.Event` is a suitable signal and lets callers request
        shutdown without waiting for the full polling interval.
        """

        selected_detector = detector or self._detector
        if selected_detector is None:
            raise RuntimeError("no detector was configured")
        if not isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval must be finite and greater than zero")
        if self._closed:
            raise RuntimeError("tracking engine is closed")

        try:
            while stop_event is None or not stop_event.is_set():
                events = self.reconcile(selected_detector())
                _emit(events, on_event)

                if stop_event is None:
                    time.sleep(poll_interval)
                elif stop_event.wait(poll_interval):
                    break
        finally:
            _emit(self.close(), on_event)

    def close(self, *, observed_at: datetime | None = None) -> list[TrackingEvent]:
        """Stop every active session and return its STOP events.

        The method is idempotent and intentionally leaves the shared database
        connection open; the owner of :class:`Database` controls its lifetime.
        """

        if self._closed:
            return []
        events = self.reconcile((), observed_at=observed_at)
        self._closed = True
        return events

    def __enter__(self) -> TrackingEngine:
        if self._closed:
            raise RuntimeError("tracking engine is closed")
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def _logical_tool_set(tools: Iterable[str]) -> set[str]:
    logical_tools: set[str] = set()
    for tool in tools:
        if not isinstance(tool, str):
            raise TypeError("detectors must yield tool names as strings")
        normalized = tool.strip()
        if not normalized:
            raise ValueError("detectors must not yield empty tool names")
        logical_tools.add(normalized)
    return logical_tools


def _observed_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    return value.astimezone(UTC)


def _start_event(session: Session) -> TrackingEvent:
    return TrackingEvent(
        action="start",
        tool=session.tool,
        session_id=session.id,
        started_at=session.started_at,
    )


def _stop_event(session: Session) -> TrackingEvent:
    if session.ended_at is None:  # Defensive: Database.end_session guarantees this.
        raise ValueError("cannot create a STOP event from an open session")
    return TrackingEvent(
        action="stop",
        tool=session.tool,
        session_id=session.id,
        started_at=session.started_at,
        ended_at=session.ended_at,
    )


def _emit(events: Iterable[TrackingEvent], callback: EventCallback | None) -> None:
    if callback is None:
        return
    for event in events:
        callback(event)
