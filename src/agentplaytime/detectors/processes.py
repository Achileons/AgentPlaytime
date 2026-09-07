"""Privacy-preserving detection for Claude Code and Codex CLI processes.

This module intentionally never requests a process command line.  Prompts can
be supplied directly in CLI arguments (for example, ``claude -p ...`` or
``codex exec ...``), so even diagnostic code is limited to PID, parent PID,
process name, and executable path.  It also never reads environments, current
working directories, open files, terminal text, or process memory.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import PurePath
from typing import Any, Final

from .constants import CLAUDE_CODE, CODEX


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    """The complete, deliberately small process metadata AgentPlaytime reads."""

    pid: int
    ppid: int | None
    name: str | None
    executable: str | None


@dataclass(frozen=True, slots=True)
class ProcessScan:
    """Result of a best-effort psutil process-table snapshot."""

    available: bool
    backend: str
    processes: tuple[ProcessRecord, ...]
    access_denied_count: int = 0
    vanished_count: int = 0
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProcessEvaluation:
    """Explain why a candidate process was included or excluded."""

    process: ProcessRecord
    candidate_tool: str | None
    detected_tool: str | None
    decision: str
    reason: str


_CLAUDE_PROCESS_NAMES: Final = frozenset({"claude", "claude-code"})
_CODEX_PROCESS_NAMES: Final = frozenset({"codex", "codex-cli"})

_HELPER_NAME_MARKERS: Final = (
    " helper",
    "renderer",
    "gpu process",
    "plugin",
    "plug-in",
    "crashpad",
    "crash reporter",
    "web content",
    "ios sim",
)

_SUPPORTED_GUI_NAMES: Final = frozenset(
    {
        "ChatGPT",
        "Claude",
        "Codex",
        "Cursor",
        "ChatGPT Helper",
        "Claude Helper",
        "Codex Helper",
        "Cursor Helper",
    }
)

_SUPPORTED_GUI_BUNDLE_PATH_PARTS: Final = (
    "/chatgpt.app/contents/",
    "/claude.app/contents/",
    "/codex.app/contents/",
    "/cursor.app/contents/",
)


def scan_processes() -> ProcessScan:
    """Read a privacy-safe process snapshot with psutil.

    The requested psutil attribute list is an important privacy boundary.  Do
    not add ``cmdline``, ``environ``, ``cwd``, or other content-bearing fields.
    Per-process permission errors and races are expected and do not abort the
    snapshot.
    """

    try:
        import psutil  # type: ignore[import-not-found]
    except (ImportError, OSError) as exc:
        return ProcessScan(
            available=False,
            backend="psutil",
            processes=(),
            errors=(f"psutil is unavailable: {type(exc).__name__}: {exc}",),
        )

    records: list[ProcessRecord] = []
    access_denied = 0
    vanished = 0
    errors: list[str] = []

    try:
        iterator = psutil.process_iter(attrs=("pid", "ppid", "name", "exe"))
        for process in iterator:
            try:
                info = process.info
                records.append(
                    ProcessRecord(
                        pid=int(info["pid"]),
                        ppid=_optional_int(info.get("ppid")),
                        name=_optional_string(info.get("name")),
                        executable=_optional_string(info.get("exe")),
                    )
                )
            except psutil.AccessDenied:
                access_denied += 1
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                vanished += 1
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(
                    f"one process record was invalid: {type(exc).__name__}: {exc}"
                )
    except (psutil.AccessDenied, OSError) as exc:
        return ProcessScan(
            available=False,
            backend="psutil",
            processes=tuple(records),
            access_denied_count=access_denied,
            vanished_count=vanished,
            errors=(f"process-table query failed: {type(exc).__name__}: {exc}",),
        )

    return ProcessScan(
        available=True,
        backend="psutil",
        processes=tuple(records),
        access_denied_count=access_denied,
        vanished_count=vanished,
        errors=tuple(errors),
    )


def get_running_processes() -> tuple[ProcessRecord, ...]:
    """Compatibility helper returning only privacy-safe process records."""

    return scan_processes().processes


def evaluate_process(
    process: ProcessRecord,
    process_by_pid: Mapping[int, ProcessRecord] | None = None,
) -> ProcessEvaluation:
    """Classify one process and retain a human-readable decision reason."""

    candidate_tool, match_reason = _candidate_match(process)
    if candidate_tool is None:
        return ProcessEvaluation(
            process=process,
            candidate_tool=None,
            detected_tool=None,
            decision="not_a_supported_cli",
            reason=match_reason,
        )

    exclusion_reason = _self_exclusion_reason(process)
    if exclusion_reason is not None:
        return ProcessEvaluation(
            process=process,
            candidate_tool=candidate_tool,
            detected_tool=None,
            decision="excluded_gui_or_helper",
            reason=exclusion_reason,
        )

    if process_by_pid is not None:
        ancestor = _supported_gui_ancestor(process, process_by_pid)
        if ancestor is not None:
            ancestor_label = ancestor.name or ancestor.executable or str(ancestor.pid)
            return ProcessEvaluation(
                process=process,
                candidate_tool=candidate_tool,
                detected_tool=None,
                decision="excluded_gui_child",
                reason=f"descendant of supported native application {ancestor_label!r}",
            )

    return ProcessEvaluation(
        process=process,
        candidate_tool=candidate_tool,
        detected_tool=candidate_tool,
        decision="detected",
        reason=match_reason,
    )


def classify_process(
    process: ProcessRecord,
    process_by_pid: Mapping[int, ProcessRecord] | None = None,
) -> str | None:
    """Return the logical CLI tool for one process, if detected."""

    return evaluate_process(process, process_by_pid).detected_tool


def detect_process_tools(
    processes: Iterable[ProcessRecord] | None = None,
) -> set[str]:
    """Return a set, ensuring multiple CLI instances produce one timer."""

    records = get_running_processes() if processes is None else tuple(processes)
    process_by_pid = {process.pid: process for process in records}
    return {
        tool
        for process in records
        if (tool := classify_process(process, process_by_pid)) is not None
    }


# Integration-friendly aliases.
detect_cli_tools = detect_process_tools
detect_running_processes = detect_process_tools


def diagnose_processes() -> dict[str, Any]:
    """Return JSON-friendly, prompt-safe diagnostics for ``doctor``."""

    scan = scan_processes()
    process_by_pid = {process.pid: process for process in scan.processes}
    evaluations = [
        evaluate_process(process, process_by_pid) for process in scan.processes
    ]
    relevant = [
        evaluation
        for evaluation in evaluations
        if evaluation.candidate_tool is not None or _looks_relevant(evaluation.process)
    ]

    return {
        "platform": sys.platform,
        "backend": scan.backend,
        "available": scan.available,
        "errors": list(scan.errors),
        "scanned_process_count": len(scan.processes),
        "access_denied_count": scan.access_denied_count,
        "vanished_count": scan.vanished_count,
        "detected_tools": sorted(detect_process_tools(scan.processes)),
        "candidate_processes": [_evaluation_dict(item) for item in relevant],
        "known_patterns": [
            {
                "tool": CLAUDE_CODE,
                "process_names": sorted(_CLAUDE_PROCESS_NAMES),
                "executable_path_patterns": [
                    "*/.local/share/claude/versions/*",
                    "*/claude-code/*",
                ],
                "verification": (
                    "native Claude Code 2.1.252 verified locally through "
                    "~/.local/bin/claude"
                ),
            },
            {
                "tool": CODEX,
                "process_names": sorted(_CODEX_PROCESS_NAMES),
                "executable_path_patterns": ["*/codex", "*/codex-cli", "*/codex-*"],
                "verification": (
                    "codex-cli 0.153.4 binary verified locally at "
                    "/Applications/ChatGPT.app/Contents/Resources/codex"
                ),
            },
        ],
        "privacy": (
            "Only pid, parent pid, process name, and executable path were read. "
            "Command-line arguments, prompts, environment, cwd, files, and "
            "process memory were not requested."
        ),
    }


def _candidate_match(process: ProcessRecord) -> tuple[str | None, str]:
    name = (process.name or "").strip()
    executable = (process.executable or "").strip()
    folded_executable = executable.casefold().replace("\\", "/")
    executable_name = PurePath(executable).name.casefold() if executable else ""

    # Preserve case as a useful macOS distinction: native application names
    # are normally "Claude"/"Codex" while CLI process names are lowercase.
    if name in _CLAUDE_PROCESS_NAMES:
        return CLAUDE_CODE, f"exact CLI process name {name!r}"
    if executable_name in _CLAUDE_PROCESS_NAMES:
        return CLAUDE_CODE, f"exact executable basename {executable_name!r}"
    if "/.local/share/claude/versions/" in folded_executable:
        return CLAUDE_CODE, "executable is in Claude Code's native version directory"
    if "/claude-code/" in folded_executable:
        return CLAUDE_CODE, "executable path contains a Claude Code directory"

    if name in _CODEX_PROCESS_NAMES:
        return CODEX, f"exact CLI process name {name!r}"
    if executable_name in _CODEX_PROCESS_NAMES:
        return CODEX, f"exact executable basename {executable_name!r}"
    if executable_name.startswith("codex-"):
        return CODEX, f"version/platform Codex executable {executable_name!r}"

    return None, "name and executable do not match a supported CLI pattern"


def _self_exclusion_reason(process: ProcessRecord) -> str | None:
    name = (process.name or "").strip()
    folded_name = name.casefold()
    executable = (process.executable or "").casefold().replace("\\", "/")

    if any(marker in folded_name for marker in _HELPER_NAME_MARKERS):
        return "helper/renderer-style process name"
    if "/contents/frameworks/" in executable and ".app/contents/" in executable:
        return "executable belongs to an application framework/helper bundle"
    if ".app/contents/macos/" in executable:
        return "executable is the main binary of a native application bundle"
    return None


def _supported_gui_ancestor(
    process: ProcessRecord,
    process_by_pid: Mapping[int, ProcessRecord],
) -> ProcessRecord | None:
    """Find an ancestor from the same supported application bundle.

    A CLI launched from an IDE's integrated terminal is also a descendant of
    that IDE.  GUI ancestry alone therefore cannot distinguish a bundled app
    backend from an intentional CLI session.  Require the candidate executable
    and the ancestor executable to live in the same ``.app`` bundle before
    excluding it.
    """

    visited = {process.pid}
    parent_pid = process.ppid
    process_bundle = _application_bundle_root(process.executable)
    if process_bundle is None:
        return None
    for _ in range(32):
        if parent_pid is None or parent_pid <= 0 or parent_pid in visited:
            return None
        visited.add(parent_pid)
        parent = process_by_pid.get(parent_pid)
        if parent is None:
            return None

        executable = (parent.executable or "").casefold().replace("\\", "/")
        if parent.name in _SUPPORTED_GUI_NAMES or any(
            marker in executable for marker in _SUPPORTED_GUI_BUNDLE_PATH_PARTS
        ):
            if _application_bundle_root(parent.executable) == process_bundle:
                return parent
        parent_pid = parent.ppid
    return None


def _application_bundle_root(executable: str | None) -> str | None:
    """Return a normalized path through the containing ``.app`` component."""

    folded = (executable or "").casefold().replace("\\", "/")
    marker = ".app/contents/"
    marker_index = folded.find(marker)
    if marker_index < 0:
        return None
    return folded[: marker_index + len(".app")]


def _looks_relevant(process: ProcessRecord) -> bool:
    haystack = " ".join((process.name or "", process.executable or "")).casefold()
    return any(token in haystack for token in ("chatgpt", "claude", "codex", "cursor"))


def _evaluation_dict(evaluation: ProcessEvaluation) -> dict[str, Any]:
    result = asdict(evaluation)
    # dataclasses.asdict nests ProcessRecord as desired and never introduces
    # fields outside the explicit privacy-safe schema above.
    return result


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)
