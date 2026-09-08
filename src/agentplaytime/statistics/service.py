"""Deterministic aggregation of locally stored runtime sessions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agentplaytime.storage import Session


class StatsPeriod(str, Enum):
    """User-facing time ranges supported by the stats command."""

    TODAY = "today"
    WEEK = "week"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class LocalTimezone:
    """The resolved local timezone and the name displayed to users."""

    zone: tzinfo
    name: str
    used_fixed_offset_fallback: bool = False


@dataclass(frozen=True, slots=True)
class ToolStatistics:
    """Aggregated metrics for one logical tool within a report period."""

    tool: str
    total_duration: timedelta
    session_count: int
    average_session_duration: timedelta
    longest_session_duration: timedelta
    running: bool


@dataclass(frozen=True, slots=True)
class StatisticsReport:
    """Complete statistics result, including its exact local time range."""

    period: StatsPeriod
    timezone_name: str
    generated_at: datetime
    range_start: datetime | None
    range_end: datetime
    total_duration: timedelta
    tools: tuple[ToolStatistics, ...]


@dataclass(slots=True)
class _ToolAccumulator:
    durations: list[timedelta]
    running: bool = False


def build_statistics(
    sessions: Iterable[Session],
    *,
    period: StatsPeriod | str = StatsPeriod.ALL,
    now: datetime,
    local_timezone: LocalTimezone,
) -> StatisticsReport:
    """Aggregate sessions over ``period`` using local calendar boundaries.

    Session timestamps remain UTC. Only period boundaries and report timestamps
    are represented in the local timezone. Open sessions are measured through
    the injected ``now`` so callers and tests never depend on an implicit clock.
    """

    selected_period = StatsPeriod(period)
    now_utc = _as_utc(now)
    local_now = now_utc.astimezone(local_timezone.zone)
    sessions_list = list(sessions)
    period_start_local = _period_start(selected_period, local_now)
    period_start_utc = (
        None if period_start_local is None else period_start_local.astimezone(UTC)
    )

    accumulators: dict[str, _ToolAccumulator] = defaultdict(
        lambda: _ToolAccumulator(durations=[])
    )
    for session in sessions_list:
        session_start = _as_utc(session.started_at)
        session_end = (
            now_utc if session.ended_at is None else _as_utc(session.ended_at)
        )
        if session_end < session_start:
            raise ValueError("session end cannot be earlier than its start")

        overlap_start = session_start
        if period_start_utc is not None:
            overlap_start = max(overlap_start, period_start_utc)
        overlap_end = min(session_end, now_utc)

        # Treat intervals as half-open. Merely touching a period boundary, or a
        # zero-duration session, does not constitute runtime in that period.
        if overlap_end <= overlap_start:
            continue

        accumulator = accumulators[session.tool]
        accumulator.durations.append(overlap_end - overlap_start)
        if session.ended_at is None and session_start <= now_utc:
            accumulator.running = True

    tools: list[ToolStatistics] = []
    for tool in sorted(accumulators):
        accumulator = accumulators[tool]
        total = sum(accumulator.durations, start=timedelta())
        count = len(accumulator.durations)
        tools.append(
            ToolStatistics(
                tool=tool,
                total_duration=total,
                session_count=count,
                average_session_duration=total / count,
                longest_session_duration=max(accumulator.durations),
                running=accumulator.running,
            )
        )

    all_time_start = _all_time_start(
        sessions_list,
        local_timezone.zone,
        through=now_utc,
    )
    range_start = (
        all_time_start if selected_period is StatsPeriod.ALL else period_start_local
    )
    return StatisticsReport(
        period=selected_period,
        timezone_name=local_timezone.name,
        generated_at=local_now,
        range_start=range_start,
        range_end=local_now,
        total_duration=sum(
            (tool.total_duration for tool in tools),
            start=timedelta(),
        ),
        tools=tuple(tools),
    )


def detect_local_timezone() -> LocalTimezone:
    """Resolve the Mac's IANA timezone, with a safe fixed-offset fallback.

    macOS normally exposes an IANA zone through ``/etc/localtime`` and through
    Foundation. Neither fallback reads user files or environment variables.
    """

    localtime_name = _localtime_zone_name(Path("/etc/localtime"))
    if localtime_name is not None:
        resolved = _iana_timezone(localtime_name)
        if resolved is not None:
            return LocalTimezone(zone=resolved, name=localtime_name)

    foundation_name = _foundation_timezone_name()
    if foundation_name is not None:
        resolved = _iana_timezone(foundation_name)
        if resolved is not None:
            return LocalTimezone(zone=resolved, name=foundation_name)

    local_now = datetime.now().astimezone()
    fallback = local_now.tzinfo or UTC
    fallback_name = local_now.tzname() or str(fallback) or "UTC"
    return LocalTimezone(
        zone=fallback,
        name=fallback_name,
        used_fixed_offset_fallback=True,
    )


def whole_seconds(duration: timedelta) -> int:
    """Return non-negative whole seconds, truncating sub-second fractions."""

    return max(0, int(duration.total_seconds()))


def _period_start(period: StatsPeriod, local_now: datetime) -> datetime | None:
    if period is StatsPeriod.ALL:
        return None
    days_back = 0 if period is StatsPeriod.TODAY else 6
    start_date = local_now.date() - timedelta(days=days_back)
    return datetime.combine(start_date, time.min, tzinfo=local_now.tzinfo)


def _all_time_start(
    sessions: list[Session],
    zone: tzinfo,
    *,
    through: datetime,
) -> datetime | None:
    starts: list[datetime] = []
    for session in sessions:
        start = _as_utc(session.started_at)
        if start <= through:
            starts.append(start)
    if not starts:
        return None
    return min(starts).astimezone(zone)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _localtime_zone_name(localtime_path: Path) -> str | None:
    try:
        resolved = localtime_path.resolve(strict=True)
    except OSError:
        return None

    marker = "zoneinfo/"
    _prefix, found, suffix = str(resolved).partition(marker)
    return suffix if found and suffix else None


def _foundation_timezone_name() -> str | None:
    try:
        from Foundation import NSTimeZone

        name = str(NSTimeZone.localTimeZone().name()).strip()
    # Foundation is an optional discovery backend. Any bridge or framework
    # failure must fall through to the safe fixed-offset result.
    except Exception:
        return None
    return name or None


def _iana_timezone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
