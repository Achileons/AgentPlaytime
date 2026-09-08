"""Deterministic tests for period-based runtime aggregation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentplaytime.statistics import (
    LocalTimezone,
    StatsPeriod,
    build_statistics,
    detect_local_timezone,
    whole_seconds,
)
from agentplaytime.storage import Session


ISTANBUL = LocalTimezone(ZoneInfo("Europe/Istanbul"), "Europe/Istanbul")
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def _session(
    session_id: int,
    tool: str,
    started_at: datetime,
    ended_at: datetime | None,
) -> Session:
    return Session(session_id, tool, started_at, ended_at)


def test_all_period_aggregates_multiple_tools_and_sessions() -> None:
    sessions = [
        _session(1, "Claude", NOW - timedelta(hours=2), NOW - timedelta(minutes=90)),
        _session(2, "Claude", NOW - timedelta(hours=1), NOW - timedelta(minutes=40)),
        _session(3, "Codex", NOW - timedelta(minutes=10), None),
    ]

    report = build_statistics(
        sessions,
        period="all",
        now=NOW,
        local_timezone=ISTANBUL,
    )

    assert report.period is StatsPeriod.ALL
    assert report.total_duration == timedelta(hours=1)
    assert [tool.tool for tool in report.tools] == ["Claude", "Codex"]
    claude, codex = report.tools
    assert claude.session_count == 2
    assert claude.total_duration == timedelta(minutes=50)
    assert claude.average_session_duration == timedelta(minutes=25)
    assert claude.longest_session_duration == timedelta(minutes=30)
    assert not claude.running
    assert codex.session_count == 1
    assert codex.total_duration == timedelta(minutes=10)
    assert codex.running


def test_today_clips_a_session_crossing_local_midnight() -> None:
    # Europe/Istanbul local midnight is 21:00 UTC on the preceding date.
    local_midnight_utc = datetime(2026, 9, 7, 21, 0, tzinfo=UTC)
    sessions = [
        _session(
            1,
            "Claude",
            local_midnight_utc - timedelta(hours=2),
            local_midnight_utc + timedelta(hours=1),
        )
    ]

    report = build_statistics(
        sessions,
        period="today",
        now=NOW,
        local_timezone=ISTANBUL,
    )

    assert report.range_start == datetime(
        2026, 9, 8, 0, 0, tzinfo=ZoneInfo("Europe/Istanbul")
    )
    assert report.tools[0].total_duration == timedelta(hours=1)
    assert report.tools[0].session_count == 1


def test_today_excludes_sessions_outside_the_period() -> None:
    report = build_statistics(
        [
            _session(
                1,
                "Cursor",
                datetime(2026, 9, 7, 10, 0, tzinfo=UTC),
                datetime(2026, 9, 7, 11, 0, tzinfo=UTC),
            )
        ],
        period="today",
        now=NOW,
        local_timezone=ISTANBUL,
    )

    assert report.tools == ()
    assert report.total_duration == timedelta()


def test_week_starts_at_local_midnight_six_days_before_today() -> None:
    week_start_utc = datetime(2026, 9, 1, 21, 0, tzinfo=UTC)
    report = build_statistics(
        [
            _session(
                1,
                "ChatGPT",
                week_start_utc - timedelta(minutes=20),
                week_start_utc + timedelta(minutes=40),
            )
        ],
        period="week",
        now=NOW,
        local_timezone=ISTANBUL,
    )

    assert report.range_start == datetime(
        2026, 9, 2, 0, 0, tzinfo=ZoneInfo("Europe/Istanbul")
    )
    assert report.tools[0].total_duration == timedelta(minutes=40)


def test_all_includes_sessions_before_the_week_window() -> None:
    old_session = _session(
        1,
        "Claude Code",
        datetime(2025, 1, 1, 8, 0, tzinfo=UTC),
        datetime(2025, 1, 1, 10, 0, tzinfo=UTC),
    )

    report = build_statistics(
        [old_session],
        period="all",
        now=NOW,
        local_timezone=ISTANBUL,
    )

    assert report.total_duration == timedelta(hours=2)
    assert report.range_start == datetime(
        2025, 1, 1, 11, 0, tzinfo=ZoneInfo("Europe/Istanbul")
    )


def test_open_session_is_measured_through_injected_now() -> None:
    report = build_statistics(
        [_session(1, "Codex", NOW - timedelta(seconds=75), None)],
        now=NOW,
        local_timezone=ISTANBUL,
    )

    assert report.tools[0].total_duration == timedelta(seconds=75)
    assert report.tools[0].running


def test_session_touching_period_boundary_has_no_overlap() -> None:
    local_midnight_utc = datetime(2026, 9, 7, 21, 0, tzinfo=UTC)
    report = build_statistics(
        [
            _session(
                1,
                "Claude",
                local_midnight_utc - timedelta(hours=1),
                local_midnight_utc,
            )
        ],
        period="today",
        now=NOW,
        local_timezone=ISTANBUL,
    )

    assert report.tools == ()


def test_empty_all_report_has_no_start_boundary() -> None:
    report = build_statistics([], now=NOW, local_timezone=ISTANBUL)

    assert report.range_start is None
    assert report.range_end == NOW.astimezone(ISTANBUL.zone)
    assert report.tools == ()


def test_spring_dst_day_uses_elapsed_time_not_wall_clock_hours() -> None:
    new_york = LocalTimezone(ZoneInfo("America/New_York"), "America/New_York")
    # DST starts on 2026-03-08: 02:00 is skipped. Midnight to 04:00 is 3 hours.
    start_local = datetime(2026, 3, 8, 0, 0, tzinfo=new_york.zone)
    now_local = datetime(2026, 3, 8, 4, 0, tzinfo=new_york.zone)

    report = build_statistics(
        [_session(1, "Claude", start_local.astimezone(UTC), None)],
        period="today",
        now=now_local,
        local_timezone=new_york,
    )

    assert report.range_start == start_local
    assert report.total_duration == timedelta(hours=3)


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_statistics(
            [],
            now=datetime(2026, 9, 8, 12, 0),
            local_timezone=ISTANBUL,
        )


def test_fractional_seconds_are_truncated_after_aggregation() -> None:
    assert whole_seconds(timedelta(seconds=2, microseconds=999_999)) == 2
    assert whole_seconds(timedelta(seconds=-1)) == 0


def test_local_timezone_detection_returns_an_aware_timezone() -> None:
    detected = detect_local_timezone()

    assert detected.name
    assert NOW.astimezone(detected.zone).utcoffset() is not None
