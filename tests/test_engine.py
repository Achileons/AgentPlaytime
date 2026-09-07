"""Tests for the detector-independent tracking state machine."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentplaytime.storage import ActiveSessionExistsError, Database
from agentplaytime.tracker import TrackingEngine, TrackingEvent


T0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    store = Database(tmp_path / "sessions.db")
    try:
        yield store
    finally:
        store.close()


def test_tool_appears_starts_session(database: Database) -> None:
    engine = TrackingEngine(database)

    events = engine.reconcile({"ChatGPT"}, observed_at=T0)

    assert [(event.action, event.tool) for event in events] == [("start", "ChatGPT")]
    assert events[0].started_at == T0
    assert events[0].ended_at is None
    assert engine.active_tools == {"ChatGPT"}
    assert len(database.list_sessions()) == 1
    engine.close(observed_at=T0 + timedelta(minutes=1))


def test_same_tool_remains_running_without_duplicate_session(database: Database) -> None:
    engine = TrackingEngine(database)
    engine.reconcile(["Claude"], observed_at=T0)

    events = engine.reconcile(["Claude"], observed_at=T0 + timedelta(seconds=2))

    assert events == []
    assert len(database.list_sessions(tool="Claude")) == 1
    engine.close(observed_at=T0 + timedelta(minutes=1))


def test_tool_disappears_ends_session(database: Database) -> None:
    engine = TrackingEngine(database)
    engine.reconcile(["Cursor"], observed_at=T0)

    events = engine.reconcile([], observed_at=T0 + timedelta(minutes=12, seconds=31))

    assert len(events) == 1
    event = events[0]
    assert event.action == "stop"
    assert event.tool == "Cursor"
    assert event.duration_seconds == 751
    assert engine.active_tools == frozenset()
    assert database.list_sessions(tool="Cursor")[0].ended_at == event.ended_at


def test_tool_returning_later_creates_a_new_session(database: Database) -> None:
    engine = TrackingEngine(database)
    engine.reconcile(["Codex"], observed_at=T0)
    engine.reconcile([], observed_at=T0 + timedelta(minutes=10))

    events = engine.reconcile(["Codex"], observed_at=T0 + timedelta(minutes=20))

    sessions = database.list_sessions(tool="Codex")
    assert [(event.action, event.tool) for event in events] == [("start", "Codex")]
    assert len(sessions) == 2
    assert sessions[0].ended_at == T0 + timedelta(minutes=10)
    assert sessions[1].started_at == T0 + timedelta(minutes=20)
    assert sessions[0].id != sessions[1].id
    engine.close(observed_at=T0 + timedelta(minutes=30))


def test_multiple_instances_of_same_tool_are_one_logical_timer(
    database: Database,
) -> None:
    engine = TrackingEngine(database)

    events = engine.reconcile(
        ["Claude Code", "Claude Code", "Claude Code"],
        observed_at=T0,
    )
    unchanged = engine.reconcile(
        ["Claude Code", "Claude Code"],
        observed_at=T0 + timedelta(seconds=2),
    )

    assert [(event.action, event.tool) for event in events] == [
        ("start", "Claude Code")
    ]
    assert unchanged == []
    assert len(database.list_sessions(tool="Claude Code")) == 1
    engine.close(observed_at=T0 + timedelta(minutes=1))


def test_two_different_tools_have_independent_timers(database: Database) -> None:
    engine = TrackingEngine(database)
    start_events = engine.reconcile(
        ["ChatGPT", "Claude"],
        observed_at=T0,
    )

    stop_claude = engine.reconcile(
        ["ChatGPT"],
        observed_at=T0 + timedelta(minutes=5),
    )
    stop_chatgpt = engine.reconcile(
        [],
        observed_at=T0 + timedelta(minutes=8),
    )

    assert [(event.action, event.tool) for event in start_events] == [
        ("start", "ChatGPT"),
        ("start", "Claude"),
    ]
    assert [(event.action, event.tool) for event in stop_claude] == [
        ("stop", "Claude")
    ]
    assert stop_claude[0].duration_seconds == 300
    assert [(event.action, event.tool) for event in stop_chatgpt] == [
        ("stop", "ChatGPT")
    ]
    assert stop_chatgpt[0].duration_seconds == 480


def test_close_stops_active_sessions_and_is_idempotent(database: Database) -> None:
    engine = TrackingEngine(database)
    engine.reconcile(["Cursor", "Codex"], observed_at=T0)

    events = engine.close(observed_at=T0 + timedelta(seconds=30))

    assert [(event.action, event.tool) for event in events] == [
        ("stop", "Codex"),
        ("stop", "Cursor"),
    ]
    assert all(event.duration_seconds == 30 for event in events)
    assert database.open_sessions() == []
    assert engine.close(observed_at=T0 + timedelta(seconds=40)) == []
    with pytest.raises(RuntimeError, match="closed"):
        engine.reconcile([], observed_at=T0 + timedelta(seconds=40))


def test_run_emits_shutdown_events_when_stop_is_requested(database: Database) -> None:
    moments = iter((T0, T0 + timedelta(seconds=2)))
    callback_events: list[TrackingEvent] = []
    signal = _StopOnFirstWait()
    engine = TrackingEngine(
        database,
        detector=lambda: ["ChatGPT"],
        clock=lambda: next(moments),
    )

    engine.run(stop_event=signal, on_event=callback_events.append)

    assert [(event.action, event.tool) for event in callback_events] == [
        ("start", "ChatGPT"),
        ("stop", "ChatGPT"),
    ]
    assert callback_events[-1].duration_seconds == 2
    assert engine.is_closed


@pytest.mark.parametrize("poll_interval", [0.0, -1.0, float("nan"), float("inf")])
def test_run_rejects_invalid_poll_intervals(
    database: Database,
    poll_interval: float,
) -> None:
    engine = TrackingEngine(database, detector=lambda: [])

    with pytest.raises(ValueError, match="finite and greater than zero"):
        engine.run(poll_interval=poll_interval)


def test_database_normalizes_timestamps_to_utc(database: Database) -> None:
    eastern_europe = timezone(timedelta(hours=2))
    local_start = datetime(2026, 1, 1, 12, 0, tzinfo=eastern_europe)
    session = database.start_session("Claude", started_at=local_start)
    finished = database.end_session(
        session.id,
        ended_at=local_start + timedelta(minutes=3),
    )

    assert finished.started_at == T0
    assert finished.ended_at == T0 + timedelta(minutes=3)
    assert database.runtime_by_tool() == {"Claude": timedelta(minutes=3)}


def test_database_rejects_a_second_open_session_for_the_same_tool(
    database: Database,
) -> None:
    database.start_session("ChatGPT", started_at=T0)

    with pytest.raises(ActiveSessionExistsError):
        database.start_session("ChatGPT", started_at=T0 + timedelta(seconds=1))


class _StopOnFirstWait:
    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> bool:
        return True
