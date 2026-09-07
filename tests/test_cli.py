"""Small command-level tests for stable user-facing behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from agentplaytime.cli import _format_event_duration, _format_stats_duration, main
from agentplaytime.storage import Database


def test_duration_formatting() -> None:
    assert _format_event_duration(timedelta(minutes=12, seconds=31)) == "12m 31s"
    assert _format_event_duration(timedelta(hours=2, minutes=4, seconds=5)) == (
        "2h 4m 5s"
    )
    assert _format_stats_duration(timedelta(hours=2, minutes=4, seconds=59)) == (
        "2h 04m"
    )
    assert _format_stats_duration(timedelta(seconds=22)) == "22s"


def test_stats_reads_accumulated_runtime(tmp_path: Path, capsys: object) -> None:
    database_path = tmp_path / "stats.db"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with Database(database_path) as database:
        session = database.start_session("Claude Code", started_at=start)
        database.end_session(session.id, ended_at=start + timedelta(minutes=47))

    assert main(["stats", "--database", str(database_path)]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "AgentPlaytime" in output
    assert "Claude Code  47m" in output


def test_stats_handles_empty_database(tmp_path: Path, capsys: object) -> None:
    assert main(["stats", "--database", str(tmp_path / "empty.db")]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "No runtime recorded yet." in output


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
