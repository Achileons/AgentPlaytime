"""Command-line interface for AgentPlaytime."""

from __future__ import annotations

import argparse
import json
import platform
import signal
import sqlite3
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

from agentplaytime import __version__
from agentplaytime.config import DEFAULT_POLL_INTERVAL_SECONDS, default_database_path
from agentplaytime.detectors import (
    CLAUDE_CODE,
    CODEX,
    RuntimeDetector,
    SUPPORTED_TOOLS,
    doctor_diagnostics,
    scan_processes,
)
from agentplaytime.storage import Database
from agentplaytime.statistics import (
    StatisticsReport,
    StatsPeriod,
    ToolStatistics,
    build_statistics,
    detect_local_timezone,
    whole_seconds,
)
from agentplaytime.tracker import TrackingEngine, TrackingEvent


def build_parser() -> argparse.ArgumentParser:
    """Build the public command-line parser."""

    parser = argparse.ArgumentParser(
        prog="agentplaytime",
        description="Track how long supported AI tools are running on this Mac.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser(
        "doctor",
        help="inspect privacy-safe app and process metadata used for detection",
    )
    doctor.add_argument(
        "--all",
        action="store_true",
        help="include the full available app/process metadata snapshot",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable diagnostics",
    )
    doctor.set_defaults(handler=_doctor_command)

    track = commands.add_parser("track", help="start polling for supported AI tools")
    _add_database_argument(track)
    track.add_argument(
        "--interval",
        metavar="SECONDS",
        type=_positive_float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"polling interval (default: {DEFAULT_POLL_INTERVAL_SECONDS:g})",
    )
    track.set_defaults(handler=_track_command)

    stats = commands.add_parser("stats", help="show accumulated runtime by tool")
    _add_database_argument(stats)
    stats.add_argument(
        "--period",
        choices=[period.value for period in StatsPeriod],
        default=StatsPeriod.ALL.value,
        help="report period: today, week, or all (default: all)",
    )
    stats.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable statistics",
    )
    stats.set_defaults(handler=_stats_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        # A fallback for interrupts received outside the track command's
        # signal-managed polling window.
        print("\nStopped.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"agentplaytime: error: {exc}", file=sys.stderr)
        return 1


def _doctor_command(args: argparse.Namespace) -> int:
    report = doctor_diagnostics()
    if args.all:
        process_scan = scan_processes()
        report["all_processes"] = [asdict(process) for process in process_scan.processes]
    else:
        # Keep JSON and text mode consistent: the default report contains only
        # relevant candidates, while --all opts into the full metadata list.
        report["native_applications"].pop("running_applications", None)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_doctor_report(report, show_all=args.all)
    return 0


def _track_command(args: argparse.Namespace) -> int:
    database_path = Path(args.database).expanduser()
    stop_event = Event()
    runtime_detector = RuntimeDetector()
    last_health: tuple[tuple[str, ...], tuple[str, ...]] | None = None

    def detect_with_health_report() -> set[str]:
        nonlocal last_health
        snapshot = runtime_detector.snapshot()
        health = (snapshot.stale_backends, snapshot.errors)
        if health != last_health:
            if snapshot.degraded:
                backends = ", ".join(snapshot.stale_backends)
                print(f"[WARN] Detection degraded: {backends}", file=sys.stderr)
                for error in snapshot.errors:
                    print(f"       {error}", file=sys.stderr)
            elif last_health is not None and last_health[0]:
                print("[INFO] Detection backends recovered.", file=sys.stderr)
            last_health = health
        return set(snapshot.tools)

    with _exclusive_tracker_lock(database_path):
        with Database(database_path) as database:
            engine = TrackingEngine(database, detector=detect_with_health_report)
            print("AgentPlaytime")
            print(f"Database: {database_path}")
            print(f"Polling every {args.interval:g}s. Press Ctrl-C to stop.")

            previous_handlers = _install_shutdown_handlers(stop_event)
            try:
                engine.run(
                    poll_interval=args.interval,
                    on_event=_print_tracking_event,
                    stop_event=stop_event,
                )
            finally:
                _restore_signal_handlers(previous_handlers)
    print("Stopped.")
    return 0


def _stats_command(args: argparse.Namespace) -> int:
    database_path = Path(args.database).expanduser()
    now = _current_utc_time()
    local_timezone = detect_local_timezone()
    with Database(database_path) as database:
        sessions = database.list_sessions()

    report = build_statistics(
        sessions,
        period=args.period,
        now=now,
        local_timezone=local_timezone,
    )
    if args.json:
        print(json.dumps(_statistics_payload(report), indent=2))
    else:
        _print_statistics_report(report)
    return 0


def _print_statistics_report(report: StatisticsReport) -> None:
    period_title = {
        StatsPeriod.TODAY: "Today",
        StatsPeriod.WEEK: "Last 7 days",
        StatsPeriod.ALL: "All time",
    }[report.period]
    print(f"AgentPlaytime — {period_title}")
    print(f"Timezone: {report.timezone_name}")
    print(f"Total runtime: {_format_stats_duration(report.total_duration)}")
    print()

    if not report.tools:
        print("No runtime recorded for this period.")
        return

    rows = [
        (
            item.tool,
            _format_stats_duration(item.total_duration),
            str(item.session_count),
            _format_stats_duration(item.average_session_duration),
            _format_stats_duration(item.longest_session_duration),
            "running" if item.running else "stopped",
        )
        for item in _ordered_tool_statistics(report.tools)
    ]
    headers = ("Tool", "Total", "Sessions", "Average", "Longest", "Status")
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def format_row(row: tuple[str, ...]) -> str:
        return (
            f"{row[0]:<{widths[0]}}  "
            f"{row[1]:>{widths[1]}}  "
            f"{row[2]:>{widths[2]}}  "
            f"{row[3]:>{widths[3]}}  "
            f"{row[4]:>{widths[4]}}  "
            f"{row[5]:<{widths[5]}}"
        ).rstrip()

    print(format_row(headers))
    for row in rows:
        print(format_row(row))


def _statistics_payload(report: StatisticsReport) -> dict[str, Any]:
    return {
        "period": report.period.value,
        "timezone": report.timezone_name,
        "generated_at": _iso_seconds(report.generated_at),
        "range": {
            "start": (
                None
                if report.range_start is None
                else _iso_seconds(report.range_start)
            ),
            "end": _iso_seconds(report.range_end),
        },
        "total_seconds": whole_seconds(report.total_duration),
        "tools": [
            {
                "tool": item.tool,
                "total_seconds": whole_seconds(item.total_duration),
                "session_count": item.session_count,
                "average_session_seconds": whole_seconds(
                    item.average_session_duration
                ),
                "longest_session_seconds": whole_seconds(
                    item.longest_session_duration
                ),
                "running": item.running,
            }
            for item in _ordered_tool_statistics(report.tools)
        ],
    }


def _iso_seconds(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _print_tracking_event(event: TrackingEvent) -> None:
    if event.action == "start":
        print(f"[START] {event.tool}", flush=True)
        return

    duration = event.duration or timedelta()
    print(f"[STOP] {event.tool} — {_format_event_duration(duration)}", flush=True)


def _print_doctor_report(report: dict[str, Any], *, show_all: bool = False) -> None:
    native = report["native_applications"]
    processes = report["processes"]
    detected = set(report["detected_tools"])

    print("AgentPlaytime doctor")
    print(f"Version: {__version__}")
    print(f"System: {platform.platform()}")
    print(f"Python: {platform.python_version()}")
    print(f"Database: {default_database_path()}")
    print()
    print("Supported tools")
    for tool in SUPPORTED_TOOLS:
        if tool in detected:
            print(f"  [RUNNING] {tool}")
            continue
        unknown_reason = _unknown_detection_reason(tool, native, processes)
        if unknown_reason is None:
            print(f"  [not detected] {tool}")
        else:
            print(f"  [UNKNOWN] {tool} — {unknown_reason}")

    print()
    print(
        f"Native applications: {native['backend']} "
        f"({'available' if native['available'] else 'unavailable'})"
    )
    supported_apps = native["supported_running_applications"]
    if supported_apps:
        for app in supported_apps:
            print(
                "  "
                f"{app['detected_tool']}: name={_display(app.get('name'))}, "
                f"bundle={_display(app.get('bundle_identifier'))}, "
                f"pid={app['pid']}, path={_display(app.get('bundle_path'))}"
            )
    else:
        print("  No supported native applications found.")

    installed = native["installed_candidate_applications"]
    if installed:
        print("  Installed candidate bundles:")
        for app in installed:
            print(
                "    "
                f"{app.get('display_name') or app.get('name') or '(unnamed)'}: "
                f"bundle={_display(app.get('bundle_identifier'))}, "
                f"executable={_display(app.get('executable'))}, "
                f"version={_display(app.get('version'))}, "
                f"classified={_display(app.get('detected_tool'))}, "
                f"path={app['bundle_path']}"
            )

    print("  Bundle rules:")
    for rule in native["bundle_rules"]:
        print(
            f"    {rule['tool']}: {rule['bundle_identifier']} "
            f"[{rule['verification']}]"
        )
        print(f"      {rule['note']}")

    print()
    print(
        f"CLI processes: {processes['backend']} "
        f"({'available' if processes['available'] else 'unavailable'}; "
        f"scanned {processes['scanned_process_count']}, "
        f"access denied {processes['access_denied_count']})"
    )
    candidates = processes["candidate_processes"]
    if candidates:
        for evaluation in candidates:
            process = evaluation["process"]
            label = evaluation.get("detected_tool") or evaluation.get("candidate_tool")
            print(
                "  "
                f"{label or 'relevant'}: pid={process['pid']}, "
                f"ppid={_display(process.get('ppid'))}, "
                f"name={_display(process.get('name'))}, "
                f"executable={_display(process.get('executable'))}"
            )
            print(
                f"    {evaluation['decision']}: {evaluation['reason']}"
            )
    elif processes["available"]:
        print("  No supported or similarly named CLI processes found.")
    else:
        print("  CLI process state could not be determined in this environment.")

    if show_all:
        print()
        print(f"Native application snapshot ({len(native['running_applications'])})")
        for app in native["running_applications"]:
            print(
                "  "
                f"pid={app['pid']}, name={_display(app.get('name'))}, "
                f"bundle={_display(app.get('bundle_identifier'))}, "
                f"path={_display(app.get('bundle_path'))}"
            )

        all_processes = report.get("all_processes", [])
        print()
        print(f"Process snapshot ({len(all_processes)})")
        for process in all_processes:
            print(
                "  "
                f"pid={process['pid']}, ppid={_display(process.get('ppid'))}, "
                f"name={_display(process.get('name'))}, "
                f"executable={_display(process.get('executable'))}"
            )

    errors = [*native["errors"], *processes["errors"]]
    if errors:
        print()
        print("Warnings")
        for error in errors:
            print(f"  - {error}")

    print()
    print(f"Privacy: {report['privacy']}")
    if not show_all:
        print("Tip: use `agentplaytime doctor --all` for the full metadata snapshot.")


def _format_event_duration(duration: timedelta) -> str:
    return _format_duration(duration)


def _format_stats_duration(duration: timedelta) -> str:
    return _format_duration(duration)


def _format_duration(duration: timedelta) -> str:
    seconds = whole_seconds(duration)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def _ordered_tools(totals: Mapping[str, object]) -> list[str]:
    supported = [tool for tool in SUPPORTED_TOOLS if tool in totals]
    extras = sorted(set(totals) - set(supported))
    return [*supported, *extras]


def _ordered_tool_statistics(
    statistics: Sequence[ToolStatistics],
) -> list[ToolStatistics]:
    by_tool = {item.tool: item for item in statistics}
    return [by_tool[tool] for tool in _ordered_tools(by_tool)]


def _current_utc_time() -> datetime:
    return datetime.now(UTC)


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    if number == float("inf") or number != number:
        raise argparse.ArgumentTypeError("must be finite")
    return number


def _add_database_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database",
        type=Path,
        default=default_database_path(),
        metavar="PATH",
        help="SQLite database path (default: macOS Application Support)",
    )


def _display(value: object) -> str:
    if value is None or value == "":
        return "(unknown)"
    return str(value)


def _unknown_detection_reason(
    tool: str,
    native: dict[str, Any],
    processes: dict[str, Any],
) -> str | None:
    native_available = bool(native["available"])
    process_available = bool(processes["available"])
    if tool == CLAUDE_CODE:
        return None if process_available else "process detector unavailable"
    if tool == CODEX:
        if native_available and process_available:
            return None
        unavailable = []
        if not native_available:
            unavailable.append("native-app")
        if not process_available:
            unavailable.append("process")
        return f"{' and '.join(unavailable)} detector unavailable"
    return None if native_available else "native-app detector unavailable"


def _install_shutdown_handlers(stop_event: Event) -> dict[signal.Signals, Any]:
    previous: dict[signal.Signals, Any] = {}

    def request_shutdown(_signum: int, _frame: object) -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)
    return previous


def _restore_signal_handlers(previous: dict[signal.Signals, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


@contextmanager
def _exclusive_tracker_lock(database_path: Path) -> Iterator[None]:
    """Prevent two tracker processes from adopting the same open sessions."""

    import fcntl

    lock_path = database_path.with_name(f"{database_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"another tracker is already using {database_path}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":  # pragma: no cover - exercised by the console script.
    raise SystemExit(main())
