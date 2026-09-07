"""Deterministic detector tests that do not require installed AI tools."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import agentplaytime.detectors.macos_apps as macos_apps
from agentplaytime.detectors import RuntimeDetector
from agentplaytime.detectors.macos_apps import (
    ApplicationScan,
    RunningApplication,
    candidate_tool_from_name,
    classify_application,
    detect_macos_apps,
    parse_launchctl_applications,
)
from agentplaytime.detectors.processes import (
    ProcessRecord,
    ProcessScan,
    detect_process_tools,
    evaluate_process,
    scan_processes,
)


def test_bundle_identifier_is_authoritative_over_display_name() -> None:
    locally_observed_mismatch = RunningApplication(
        pid=10,
        bundle_identifier="com.openai.codex",
        name="ChatGPT",
        bundle_path="/Applications/ChatGPT.app",
    )

    assert classify_application(locally_observed_mismatch) == "Codex"
    assert detect_macos_apps([locally_observed_mismatch]) == {"Codex"}


def test_unknown_bundle_with_copied_supported_name_is_not_tracked() -> None:
    lookalike = RunningApplication(
        pid=11,
        bundle_identifier="example.unrelated.application",
        name="Claude",
        bundle_path="/Applications/Unrelated.app",
    )

    assert candidate_tool_from_name(lookalike) == "Claude"
    assert classify_application(lookalike) is None
    assert detect_macos_apps([lookalike]) == set()


def test_native_helpers_are_excluded() -> None:
    main = RunningApplication(
        pid=20,
        bundle_identifier="com.anthropic.claudefordesktop",
        name="Claude",
        bundle_path="/Applications/Claude.app",
    )
    renderer = RunningApplication(
        pid=21,
        bundle_identifier="com.anthropic.claudefordesktop.helper",
        name="Claude Helper (Renderer)",
        bundle_path=(
            "/Applications/Claude.app/Contents/Frameworks/"
            "Claude Helper (Renderer).app"
        ),
    )

    assert detect_macos_apps([main, renderer]) == {"Claude"}
    assert classify_application(renderer) is None


def test_launchctl_parser_keeps_only_exact_known_running_app_labels() -> None:
    output = """
         63760      -  application.com.anthropic.claudefordesktop.24829572.24829578
         59832      -  application.com.openai.codex.24757208.24757214
         11111      -  application.com.anthropic.claudefordesktop.helper.1.2
             0   (pe)  application.com.openai.chat.1.2
         22222      -  application.example.Cursor.1.2
    """

    applications = parse_launchctl_applications(output)

    assert [(app.pid, app.bundle_identifier) for app in applications] == [
        (63760, "com.anthropic.claudefordesktop"),
        (59832, "com.openai.codex"),
    ]
    assert detect_macos_apps(applications) == {"Claude", "Codex"}


def test_multiple_cli_instances_collapse_to_one_logical_tool() -> None:
    processes = [
        ProcessRecord(30, 1, "claude", "/Users/test/.local/bin/claude"),
        ProcessRecord(
            31,
            1,
            "2.1.252",
            "/Users/test/.local/share/claude/versions/2.1.252",
        ),
    ]

    assert detect_process_tools(processes) == {"Claude Code"}


def test_cli_shaped_gui_child_is_excluded_but_shell_child_is_detected() -> None:
    gui = ProcessRecord(
        40,
        1,
        "ChatGPT",
        "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
    )
    gui_child = ProcessRecord(
        41,
        40,
        "codex",
        "/Applications/ChatGPT.app/Contents/Resources/codex",
    )
    shell = ProcessRecord(50, 1, "zsh", "/bin/zsh")
    shell_child = ProcessRecord(
        51,
        50,
        "codex",
        "/Applications/ChatGPT.app/Contents/Resources/codex",
    )
    records = [gui, gui_child, shell, shell_child]
    by_pid = {record.pid: record for record in records}

    assert evaluate_process(gui_child, by_pid).decision == "excluded_gui_child"
    assert evaluate_process(shell_child, by_pid).decision == "detected"
    assert detect_process_tools(records) == {"Codex"}


def test_cli_launched_from_cursor_integrated_terminal_is_detected() -> None:
    cursor = ProcessRecord(
        52,
        1,
        "Cursor",
        "/Applications/Cursor.app/Contents/MacOS/Cursor",
    )
    terminal_host = ProcessRecord(
        53,
        52,
        "Cursor Helper (Plugin)",
        "/Applications/Cursor.app/Contents/Frameworks/Cursor Helper (Plugin)",
    )
    shell = ProcessRecord(54, 53, "zsh", "/bin/zsh")
    claude = ProcessRecord(
        55,
        54,
        "claude",
        "/Users/test/.local/share/claude/versions/2.1.252",
    )
    records = [cursor, terminal_host, shell, claude]
    by_pid = {record.pid: record for record in records}

    assert evaluate_process(claude, by_pid).decision == "detected"
    assert detect_process_tools(records) == {"Claude Code"}


def test_runtime_detector_keeps_last_good_state_during_backend_failure() -> None:
    app_results = iter(
        (
            ApplicationScan(
                available=True,
                backend="NSWorkspace",
                applications=(
                    RunningApplication(
                        60,
                        "com.anthropic.claudefordesktop",
                        "Claude",
                        "/Applications/Claude.app",
                    ),
                ),
            ),
            ApplicationScan(
                available=False,
                backend="NSWorkspace",
                applications=(),
                errors=("temporary failure",),
            ),
        )
    )
    process_scan = ProcessScan(
        available=True,
        backend="psutil",
        processes=(),
    )
    detector = RuntimeDetector(
        app_scanner=lambda: next(app_results),
        process_scanner=lambda: process_scan,
    )

    assert detector() == {"Claude"}
    degraded = detector.snapshot()
    assert degraded.tools == {"Claude"}
    assert degraded.degraded
    assert degraded.stale_backends == ("NSWorkspace",)
    assert degraded.errors == ("temporary failure",)


def test_empty_nsworkspace_and_failed_fallback_preserve_last_good_state(
    monkeypatch: object,
) -> None:
    primary_results = iter(
        (
            ApplicationScan(
                available=True,
                backend="NSWorkspace",
                applications=(
                    RunningApplication(
                        61,
                        "com.anthropic.claudefordesktop",
                        "Claude",
                        "/Applications/Claude.app",
                    ),
                ),
            ),
            ApplicationScan(
                available=True,
                backend="NSWorkspace",
                applications=(),
            ),
        )
    )
    failed_fallback = ApplicationScan(
        available=False,
        backend="launchctl-known-services",
        applications=(),
        errors=("launchctl unavailable",),
    )
    monkeypatch.setattr(macos_apps.sys, "platform", "darwin")  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        macos_apps, "_scan_nsworkspace", lambda: next(primary_results)
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        macos_apps, "scan_launchctl_applications", lambda: failed_fallback
    )
    detector = RuntimeDetector(
        app_scanner=macos_apps.scan_running_applications,
        process_scanner=lambda: ProcessScan(True, "psutil", ()),
    )

    assert detector() == {"Claude"}
    degraded = detector.snapshot()

    assert degraded.tools == {"Claude"}
    assert degraded.stale_backends == (
        "NSWorkspace+launchctl-known-services",
    )
    assert degraded.errors == ("launchctl unavailable",)


def test_process_scan_never_requests_command_lines_or_other_content(
    monkeypatch: object,
) -> None:
    requested_attrs: list[tuple[str, ...]] = []

    class AccessDenied(Exception):
        pass

    class NoSuchProcess(Exception):
        pass

    class ZombieProcess(Exception):
        pass

    def process_iter(*, attrs: tuple[str, ...]) -> list[SimpleNamespace]:
        requested_attrs.append(attrs)
        return [
            SimpleNamespace(
                info={
                    "pid": 70,
                    "ppid": 1,
                    "name": "claude",
                    "exe": "/Users/test/.local/bin/claude",
                }
            )
        ]

    fake_psutil = SimpleNamespace(
        AccessDenied=AccessDenied,
        NoSuchProcess=NoSuchProcess,
        ZombieProcess=ZombieProcess,
        process_iter=process_iter,
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)  # type: ignore[attr-defined]

    scan = scan_processes()

    assert scan.available
    assert requested_attrs == [("pid", "ppid", "name", "exe")]
    assert set(scan.processes[0].__dataclass_fields__) == {
        "pid",
        "ppid",
        "name",
        "executable",
    }
