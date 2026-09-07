"""Public detector facade for AgentPlaytime.

The facade combines native-app and CLI detections into a set of logical tool
names.  That union is the final no-double-counting boundary: multiple helper
processes, multiple CLI instances, and a CLI also visible through a desktop app
still yield one timer for a logical tool.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any

from .constants import (
    CHATGPT,
    CLAUDE,
    CLAUDE_CODE,
    CODEX,
    CURSOR,
    SUPPORTED_TOOLS,
)
from .macos_apps import (
    ApplicationScan,
    InstalledApplication,
    NativeAppRule,
    RunningApplication,
    candidate_tool_from_name,
    classify_application,
    detect_macos_apps,
    detect_native_tools,
    diagnose_macos_apps,
    get_running_applications,
    inspect_installed_applications,
    parse_launchctl_applications,
    scan_launchctl_applications,
    scan_running_applications,
)
from .processes import (
    ProcessEvaluation,
    ProcessRecord,
    ProcessScan,
    classify_process,
    detect_cli_tools,
    detect_process_tools,
    detect_running_processes,
    diagnose_processes,
    evaluate_process,
    get_running_processes,
    scan_processes,
)


@dataclass(frozen=True, slots=True)
class DetectionSnapshot:
    """One combined detection result, including backend health.

    A backend listed in ``stale_backends`` failed during this poll and its last
    successful tool set was retained.  This prevents a transient permission or
    API failure from fabricating STOP/START session boundaries.
    """

    tools: frozenset[str]
    native_tools: frozenset[str]
    process_tools: frozenset[str]
    stale_backends: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def degraded(self) -> bool:
        return bool(self.stale_backends)


class RuntimeDetector:
    """Stateful, failure-resilient detector suitable for the polling engine."""

    def __init__(
        self,
        *,
        app_scanner: Callable[[], ApplicationScan] = scan_running_applications,
        process_scanner: Callable[[], ProcessScan] = scan_processes,
    ) -> None:
        self._app_scanner = app_scanner
        self._process_scanner = process_scanner
        self._last_native: frozenset[str] | None = None
        self._last_process: frozenset[str] | None = None
        self._last_snapshot: DetectionSnapshot | None = None
        self._lock = Lock()

    def snapshot(self) -> DetectionSnapshot:
        """Poll both backends while retaining last-good state on failure."""

        app_scan = self._safe_app_scan()
        process_scan = self._safe_process_scan()

        with self._lock:
            stale: list[str] = []
            errors: list[str] = []

            if app_scan.available:
                native = frozenset(detect_macos_apps(app_scan.applications))
                self._last_native = native
            else:
                native = self._last_native or frozenset()
                stale.append(app_scan.backend)
                errors.extend(app_scan.errors)

            if process_scan.available:
                process = frozenset(detect_process_tools(process_scan.processes))
                self._last_process = process
            else:
                process = self._last_process or frozenset()
                stale.append(process_scan.backend)
                errors.extend(process_scan.errors)

            snapshot = DetectionSnapshot(
                tools=native | process,
                native_tools=native,
                process_tools=process,
                stale_backends=tuple(stale),
                errors=tuple(errors),
            )
            self._last_snapshot = snapshot
            return snapshot

    def __call__(self) -> set[str]:
        return set(self.snapshot().tools)

    @property
    def last_snapshot(self) -> DetectionSnapshot | None:
        with self._lock:
            return self._last_snapshot

    def reset(self) -> None:
        """Forget cached backend state, primarily for tests/reconfiguration."""

        with self._lock:
            self._last_native = None
            self._last_process = None
            self._last_snapshot = None

    def _safe_app_scan(self) -> ApplicationScan:
        try:
            return self._app_scanner()
        except Exception as exc:
            return ApplicationScan(
                available=False,
                backend="NSWorkspace",
                applications=(),
                errors=(f"unexpected native detector failure: {type(exc).__name__}: {exc}",),
            )

    def _safe_process_scan(self) -> ProcessScan:
        try:
            return self._process_scanner()
        except Exception as exc:
            return ProcessScan(
                available=False,
                backend="psutil",
                processes=(),
                errors=(f"unexpected process detector failure: {type(exc).__name__}: {exc}",),
            )


_DEFAULT_DETECTOR = RuntimeDetector()


def detect_running_tools() -> set[str]:
    """Detect running tools, retaining last-good state on backend failures."""

    return _DEFAULT_DETECTOR()


# Concise aliases are useful for callers and keep the CLI wiring obvious.
detect = detect_running_tools


def doctor_diagnostics() -> dict[str, Any]:
    """Return structured, JSON-friendly diagnostics for both backends."""

    native = diagnose_macos_apps()
    processes = diagnose_processes()
    return {
        "detected_tools": sorted(
            set(native["detected_tools"]) | set(processes["detected_tools"])
        ),
        "native_applications": native,
        "processes": processes,
        "privacy": (
            "Runtime metadata only. Detectors do not inspect prompts, command "
            "arguments, conversations, keystrokes, screenshots, clipboard, "
            "windows, environment variables, user files or file contents, or "
            "process memory. Doctor reads only application-bundle metadata."
        ),
    }


diagnose = doctor_diagnostics


__all__ = [
    "ApplicationScan",
    "CHATGPT",
    "CLAUDE",
    "CLAUDE_CODE",
    "CODEX",
    "CURSOR",
    "InstalledApplication",
    "NativeAppRule",
    "DetectionSnapshot",
    "ProcessEvaluation",
    "ProcessRecord",
    "ProcessScan",
    "RunningApplication",
    "RuntimeDetector",
    "SUPPORTED_TOOLS",
    "classify_application",
    "classify_process",
    "candidate_tool_from_name",
    "detect",
    "detect_cli_tools",
    "detect_macos_apps",
    "detect_native_tools",
    "detect_process_tools",
    "detect_running_processes",
    "detect_running_tools",
    "diagnose",
    "diagnose_macos_apps",
    "diagnose_processes",
    "doctor_diagnostics",
    "evaluate_process",
    "get_running_applications",
    "get_running_processes",
    "inspect_installed_applications",
    "parse_launchctl_applications",
    "scan_launchctl_applications",
    "scan_processes",
    "scan_running_applications",
]
