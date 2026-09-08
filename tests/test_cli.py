"""Small command-level tests for stable user-facing behavior."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from agentplaytime.cli import _format_event_duration, _format_stats_duration, main
from agentplaytime.statistics import LocalTimezone
from agentplaytime.storage import Database


def test_duration_formatting() -> None:
    assert _format_event_duration(timedelta(minutes=12, seconds=31)) == "12m 31s"
    assert _format_event_duration(timedelta(hours=2, minutes=4, seconds=5)) == (
        "2h 4m 5s"
    )
    assert _format_stats_duration(timedelta(hours=2, minutes=4, seconds=59)) == (
        "2h 4m 59s"
    )
    assert _format_stats_duration(timedelta(seconds=22)) == "22s"


def test_stats_defaults_to_all_and_reads_accumulated_runtime(
    tmp_path: Path,
    capsys: object,
) -> None:
    database_path = tmp_path / "stats.db"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with Database(database_path) as database:
        session = database.start_session("Claude Code", started_at=start)
        database.end_session(session.id, ended_at=start + timedelta(minutes=47))

    assert main(["stats", "--database", str(database_path)]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "AgentPlaytime — All time" in output
    assert "Claude Code" in output
    assert "47m 0s" in output
    assert "stopped" in output


def test_stats_handles_empty_database(tmp_path: Path, capsys: object) -> None:
    assert main(["stats", "--database", str(tmp_path / "empty.db")]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "No runtime recorded for this period." in output


def test_stats_text_uses_deterministic_supported_tool_order(
    tmp_path: Path,
    monkeypatch: object,
    capsys: object,
) -> None:
    database_path = tmp_path / "order.db"
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    with Database(database_path) as database:
        cursor = database.start_session("Codex", started_at=now - timedelta(minutes=4))
        database.end_session(cursor.id, ended_at=now - timedelta(minutes=3))
        chatgpt = database.start_session(
            "ChatGPT", started_at=now - timedelta(minutes=2)
        )
        database.end_session(chatgpt.id, ended_at=now - timedelta(minutes=1))

    _freeze_stats_clock(monkeypatch, now)
    assert main(["stats", "--database", str(database_path)]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]

    assert output.index("ChatGPT") < output.index("Codex")


def test_stats_json_is_valid_and_uses_documented_schema(
    tmp_path: Path,
    monkeypatch: object,
    capsys: object,
) -> None:
    database_path = tmp_path / "json.db"
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    with Database(database_path) as database:
        database.start_session("Claude", started_at=now - timedelta(minutes=10))

    _freeze_stats_clock(monkeypatch, now)
    assert (
        main(
            [
                "stats",
                "--database",
                str(database_path),
                "--period",
                "today",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]

    assert payload == {
        "period": "today",
        "timezone": "Europe/Istanbul",
        "generated_at": "2026-09-08T15:00:00+03:00",
        "range": {
            "start": "2026-09-08T00:00:00+03:00",
            "end": "2026-09-08T15:00:00+03:00",
        },
        "total_seconds": 600,
        "tools": [
            {
                "tool": "Claude",
                "total_seconds": 600,
                "session_count": 1,
                "average_session_seconds": 600,
                "longest_session_seconds": 600,
                "running": True,
            }
        ],
    }


def test_empty_all_json_has_null_start(
    tmp_path: Path,
    monkeypatch: object,
    capsys: object,
) -> None:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    _freeze_stats_clock(monkeypatch, now)

    assert (
        main(
            [
                "stats",
                "--database",
                str(tmp_path / "empty-json.db"),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]

    assert payload["period"] == "all"
    assert payload["range"]["start"] is None
    assert payload["total_seconds"] == 0
    assert payload["tools"] == []


def test_doctor_json_only_includes_full_app_list_with_all(
    monkeypatch: object,
    capsys: object,
) -> None:
    report = {
        "detected_tools": [],
        "native_applications": {"running_applications": [{"name": "Notes"}]},
        "processes": {},
        "privacy": "metadata only",
    }
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "agentplaytime.cli.doctor_diagnostics", lambda: report
    )

    assert main(["doctor", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert "running_applications" not in output["native_applications"]


def test_database_errors_are_reported_without_a_traceback(
    tmp_path: Path,
    capsys: object,
) -> None:
    assert main(["stats", "--database", str(tmp_path)]) == 1

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert captured.out == ""
    assert captured.err.startswith("agentplaytime: error:")
    assert "Traceback" not in captured.err


def _freeze_stats_clock(monkeypatch: object, now: datetime) -> None:
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "agentplaytime.cli._current_utc_time", lambda: now
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "agentplaytime.cli.detect_local_timezone",
        lambda: LocalTimezone(ZoneInfo("Europe/Istanbul"), "Europe/Istanbul"),
    )
