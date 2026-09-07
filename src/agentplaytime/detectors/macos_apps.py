"""Detect supported native applications through macOS Launch Services.

Only the main application returned by :class:`NSWorkspace` is considered.
Electron helper, renderer, GPU, plug-in, and crash-reporting applications are
explicitly ignored, so a desktop tool contributes at most one logical timer.

PyObjC is imported lazily.  Importing this module is therefore harmless on
non-macOS systems and in minimal test environments.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from .constants import CHATGPT, CLAUDE, CODEX, CURSOR


@dataclass(frozen=True, slots=True)
class NativeAppRule:
    """A bundle identifier that maps one native application to one tool.

    ``verification`` distinguishes identifiers inspected on the development
    Mac from compatibility candidates that were not installed there.  Doctor
    output exposes this field instead of presenting every rule as locally
    verified fact.
    """

    tool: str
    bundle_identifier: str
    verification: str
    note: str


NATIVE_APP_RULES: Final[tuple[NativeAppRule, ...]] = (
    NativeAppRule(
        tool=CLAUDE,
        bundle_identifier="com.anthropic.claudefordesktop",
        verification="verified_on_development_mac",
        note=(
            "Verified from /Applications/Claude.app. Helper bundles use the "
            "distinct com.anthropic.claudefordesktop.helper identifier."
        ),
    ),
    NativeAppRule(
        tool=CODEX,
        bundle_identifier="com.openai.codex",
        verification="verified_on_development_mac",
        note=(
            "Verified locally. The inspected bundle was named ChatGPT.app and "
            "displayed 'ChatGPT'; the bundle identifier is treated as "
            "authoritative and the mismatch is shown by doctor."
        ),
    ),
    NativeAppRule(
        tool=CHATGPT,
        bundle_identifier="com.openai.chat",
        verification="compatibility_candidate_not_installed_locally",
        note=(
            "Compatibility identifier for ChatGPT Desktop; it was not present "
            "on the development Mac and remains visibly marked as a candidate."
        ),
    ),
    NativeAppRule(
        tool=CURSOR,
        bundle_identifier="com.todesktop.230313mzl4w4u92",
        verification="compatibility_candidate_not_installed_locally",
        note=(
            "Compatibility identifier reported by Cursor distributions; "
            "Cursor was not installed on the development Mac."
        ),
    ),
)

_RULE_BY_BUNDLE_ID: Final = {
    rule.bundle_identifier.casefold(): rule for rule in NATIVE_APP_RULES
}

# Names are diagnostic hints only.  They are intentionally not used for live
# tracking: another application can choose the same display name, while a
# signed bundle identifier is the stable identity supplied by Launch Services.
_NAME_FALLBACKS: Final = {
    "chatgpt": CHATGPT,
    "claude": CLAUDE,
    "codex": CODEX,
    "cursor": CURSOR,
}

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

_HELPER_BUNDLE_MARKERS: Final = (
    ".helper",
    ".renderer",
    ".gpu",
    ".plugin",
    ".crashpad",
    ".webcontent",
    ".ios-sim",
)


@dataclass(frozen=True, slots=True)
class RunningApplication:
    """Privacy-safe metadata for one application known to Launch Services."""

    pid: int
    bundle_identifier: str | None
    name: str | None
    bundle_path: str | None


@dataclass(frozen=True, slots=True)
class ApplicationScan:
    """Result of querying NSWorkspace without making failures fatal."""

    available: bool
    backend: str
    applications: tuple[RunningApplication, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InstalledApplication:
    """Selected non-sensitive fields read from an installed app Info.plist."""

    bundle_path: str
    bundle_identifier: str | None
    name: str | None
    display_name: str | None
    executable: str | None
    version: str | None
    detected_tool: str | None


def scan_running_applications() -> ApplicationScan:
    """Return running applications using AppKit's ``NSWorkspace``.

    No Accessibility permission is required, and no window titles or window
    contents are requested.  If AppKit cannot be imported, the result explains
    why and contains no applications.
    """

    if sys.platform != "darwin":
        return ApplicationScan(
            available=False,
            backend="NSWorkspace",
            applications=(),
            errors=("native application detection is only available on macOS",),
        )

    primary = _scan_nsworkspace()

    # Some sandbox profiles allow Launch Services to enumerate no applications
    # even though the query itself succeeds.  Fall back only for that empty (or
    # unavailable) result.  A normal NSWorkspace result always includes system
    # apps such as Finder, so ordinary polling never launches the fallback.
    if primary.available and primary.applications:
        return primary

    fallback = scan_launchctl_applications()
    if not fallback.available:
        return ApplicationScan(
            # An empty NSWorkspace result caused us to invoke the fallback, so
            # it is not a trustworthy successful empty snapshot.  Mark the
            # combined backend unavailable to let RuntimeDetector retain its
            # last-good state instead of emitting a false STOP transition.
            available=False,
            backend=f"{primary.backend}+{fallback.backend}",
            applications=(),
            errors=(*primary.errors, *fallback.errors),
        )

    combined = _deduplicate_applications(
        (*primary.applications, *fallback.applications)
    )
    backend = fallback.backend
    if primary.available and primary.applications:
        backend = f"{primary.backend}+{fallback.backend}"
    return ApplicationScan(
        available=True,
        backend=backend,
        applications=combined,
        errors=primary.errors,
    )


def _scan_nsworkspace() -> ApplicationScan:
    """Query NSWorkspace once; caller decides whether a fallback is needed."""

    try:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
    except (ImportError, OSError) as exc:
        return ApplicationScan(
            available=False,
            backend="NSWorkspace",
            applications=(),
            errors=(f"AppKit/PyObjC is unavailable: {type(exc).__name__}: {exc}",),
        )

    applications: list[RunningApplication] = []
    errors: list[str] = []
    try:
        raw_applications = NSWorkspace.sharedWorkspace().runningApplications()
    except Exception as exc:  # PyObjC raises Objective-C exception wrappers.
        return ApplicationScan(
            available=False,
            backend="NSWorkspace",
            applications=(),
            errors=(f"NSWorkspace query failed: {type(exc).__name__}: {exc}",),
        )

    for raw_application in raw_applications:
        try:
            bundle_url = raw_application.bundleURL()
            bundle_path = None if bundle_url is None else str(bundle_url.path())
            applications.append(
                RunningApplication(
                    pid=int(raw_application.processIdentifier()),
                    bundle_identifier=_optional_string(
                        raw_application.bundleIdentifier()
                    ),
                    name=_optional_string(raw_application.localizedName()),
                    bundle_path=bundle_path,
                )
            )
        except Exception as exc:
            # A single disappearing application should not invalidate an
            # otherwise useful snapshot.
            errors.append(
                f"one NSWorkspace application could not be read: "
                f"{type(exc).__name__}: {exc}"
            )

    return ApplicationScan(
        available=True,
        backend="NSWorkspace",
        applications=tuple(applications),
        errors=tuple(errors),
    )


def scan_launchctl_applications() -> ApplicationScan:
    """Find known main-app service labels in the current launchd GUI domain.

    ``launchctl print`` can emit unrelated service and domain-environment data.
    To keep that data outside the Python process, launchctl is connected
    directly to a fixed-pattern grep subprocess.  Python receives and retains
    only lines mentioning configured application bundle identifiers; the
    parser then requires an exact numeric-suffixed service label.
    """

    if sys.platform != "darwin":
        return ApplicationScan(
            available=False,
            backend="launchctl-known-services",
            applications=(),
            errors=("launchctl application detection is only available on macOS",),
        )

    command = ("/bin/launchctl", "print", f"gui/{os.getuid()}")
    escaped_ids = "|".join(
        re.escape(rule.bundle_identifier) for rule in NATIVE_APP_RULES
    )
    label_filter = (
        rf"^[[:space:]]*[1-9][0-9]*[[:space:]]+"
        rf"[^[:space:]]+[[:space:]]+"
        rf"application\.({escaped_ids})(\.[0-9]+)+[[:space:]]*$"
    )
    try:
        launchctl_process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert launchctl_process.stdout is not None
        filter_process = subprocess.Popen(
            ("/usr/bin/grep", "-E", label_filter),
            stdin=launchctl_process.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        # The filter owns the duplicated read end; closing ours is necessary
        # for correct SIGPIPE/EOF behavior if either process exits early.
        launchctl_process.stdout.close()
    except OSError as exc:
        if "launchctl_process" in locals():
            launchctl_process.kill()
            launchctl_process.wait()
        return ApplicationScan(
            available=False,
            backend="launchctl-known-services",
            applications=(),
            errors=(
                f"launchctl label filter could not start: "
                f"{type(exc).__name__}: {exc}",
            ),
        )

    try:
        filtered_output, filter_stderr = filter_process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        launchctl_process.kill()
        filter_process.kill()
        launchctl_process.wait()
        filter_process.communicate()
        return ApplicationScan(
            available=False,
            backend="launchctl-known-services",
            applications=(),
            errors=("launchctl query timed out",),
        )

    try:
        launchctl_return_code = launchctl_process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        launchctl_process.kill()
        launchctl_return_code = launchctl_process.wait()

    applications = parse_launchctl_applications(filtered_output)
    # grep returns 1 for a valid scan with no matching lines.
    filter_failed = filter_process.returncode not in (0, 1)
    if launchctl_return_code != 0 or filter_failed:
        details: list[str] = []
        if launchctl_return_code != 0:
            details.append(f"launchctl status {launchctl_return_code}")
        if filter_failed:
            safe_filter_error = filter_stderr.strip()[:500]
            details.append(
                f"label-filter status {filter_process.returncode}"
                + (f": {safe_filter_error}" if safe_filter_error else "")
            )
        return ApplicationScan(
            available=False,
            backend="launchctl-known-services",
            applications=applications,
            errors=("; ".join(details),),
        )

    return ApplicationScan(
        available=True,
        backend="launchctl-known-services",
        applications=applications,
    )


def parse_launchctl_applications(
    lines: Iterable[str] | str,
) -> tuple[RunningApplication, ...]:
    """Parse only known running app service records from launchctl text."""

    source_lines = lines.splitlines() if isinstance(lines, str) else lines
    applications: list[RunningApplication] = []
    for line in source_lines:
        fields = line.split()
        if len(fields) < 2 or not fields[0].isdigit():
            continue
        pid = int(fields[0])
        label = fields[-1]
        if pid <= 0:
            continue
        for rule in NATIVE_APP_RULES:
            base_label = f"application.{rule.bundle_identifier}"
            if label == base_label or _is_number_suffixed_label(label, base_label):
                applications.append(
                    RunningApplication(
                        pid=pid,
                        bundle_identifier=rule.bundle_identifier,
                        name=None,
                        bundle_path=None,
                    )
                )
                break
    return _deduplicate_applications(applications)


def get_running_applications() -> tuple[RunningApplication, ...]:
    """Compatibility helper returning only the current application records."""

    return scan_running_applications().applications


def classify_application(application: RunningApplication) -> str | None:
    """Map an NSWorkspace record to a supported logical tool, if any."""

    if is_helper_application(application):
        return None

    bundle_identifier = (application.bundle_identifier or "").strip().casefold()
    if bundle_identifier:
        rule = _RULE_BY_BUNDLE_ID.get(bundle_identifier)
        if rule is not None:
            return rule.tool
    return None


def candidate_tool_from_name(application: RunningApplication) -> str | None:
    """Return an untrusted exact-name hint for doctor output only.

    Keeping this separate from :func:`classify_application` prevents an
    unrelated app with a copied name and unknown bundle identifier from
    silently starting a timer.
    """

    if is_helper_application(application):
        return None
    name = (application.name or "").strip().casefold()
    return _NAME_FALLBACKS.get(name)


def is_helper_application(application: RunningApplication) -> bool:
    """Return whether an application record is a helper rather than a main app."""

    name = (application.name or "").strip().casefold()
    bundle_identifier = (application.bundle_identifier or "").strip().casefold()
    bundle_path = (application.bundle_path or "").casefold()

    if any(marker in bundle_identifier for marker in _HELPER_BUNDLE_MARKERS):
        return True
    if any(marker in name for marker in _HELPER_NAME_MARKERS):
        return True
    if "/contents/frameworks/" in bundle_path and ".app/contents/" in bundle_path:
        return True
    return False


def detect_macos_apps(
    applications: Iterable[RunningApplication] | None = None,
) -> set[str]:
    """Return one logical tool name per supported running native app."""

    records = get_running_applications() if applications is None else applications
    return {
        tool
        for application in records
        if (tool := classify_application(application)) is not None
    }


# A descriptive alias reads naturally at integration call sites.
detect_native_tools = detect_macos_apps


def inspect_installed_applications(
    bundle_paths: Sequence[Path | str] | None = None,
) -> tuple[InstalledApplication, ...]:
    """Inspect known app bundle paths without launching any application."""

    paths = _default_bundle_paths() if bundle_paths is None else bundle_paths
    installed: list[InstalledApplication] = []
    seen: set[str] = set()

    for raw_path in paths:
        path = Path(raw_path).expanduser()
        path_key = str(path)
        if path_key in seen:
            continue
        seen.add(path_key)

        info_path = path / "Contents" / "Info.plist"
        if not info_path.is_file():
            continue

        try:
            with info_path.open("rb") as plist_file:
                info = plistlib.load(plist_file)
        except (OSError, plistlib.InvalidFileException):
            continue

        bundle_identifier = _plist_string(info, "CFBundleIdentifier")
        name = _plist_string(info, "CFBundleName")
        display_name = _plist_string(info, "CFBundleDisplayName")
        record = RunningApplication(
            pid=0,
            bundle_identifier=bundle_identifier,
            name=display_name or name or path.stem,
            bundle_path=str(path),
        )
        installed.append(
            InstalledApplication(
                bundle_path=str(path),
                bundle_identifier=bundle_identifier,
                name=name,
                display_name=display_name,
                executable=_plist_string(info, "CFBundleExecutable"),
                version=_plist_string(info, "CFBundleShortVersionString"),
                detected_tool=classify_application(record),
            )
        )

    return tuple(installed)


def diagnose_macos_apps() -> dict[str, Any]:
    """Return JSON-friendly native-app diagnostics for ``doctor``.

    The report deliberately contains bundle/runtime metadata only.  It never
    queries Accessibility, windows, screenshots, or application content.
    """

    scan = scan_running_applications()
    supported_running = []
    relevant_running = []
    for application in scan.applications:
        detected_tool = classify_application(application)
        candidate_tool = candidate_tool_from_name(application)
        if detected_tool is not None:
            decision = "detected_by_bundle_identifier"
            reason = "bundle identifier exactly matches a configured rule"
        elif candidate_tool is not None:
            decision = "name_only_candidate_not_tracked"
            reason = "name resembles a supported app but bundle identifier is unverified"
        elif is_helper_application(application) and _looks_relevant(application):
            decision = "helper_excluded"
            reason = "helper/renderer application is never tracked separately"
        else:
            continue

        evaluated = {
            **asdict(application),
            "candidate_tool": candidate_tool,
            "detected_tool": detected_tool,
            "decision": decision,
            "reason": reason,
        }
        relevant_running.append(evaluated)
        if detected_tool is not None:
            supported_running.append(evaluated)

    return {
        "platform": sys.platform,
        "backend": scan.backend,
        "available": scan.available,
        "errors": list(scan.errors),
        "detected_tools": sorted(detect_macos_apps(scan.applications)),
        "supported_running_applications": supported_running,
        "candidate_running_applications": relevant_running,
        "running_application_count": len(scan.applications),
        "running_applications": [asdict(app) for app in scan.applications],
        "installed_candidate_applications": [
            asdict(app) for app in inspect_installed_applications()
        ],
        "bundle_rules": [asdict(rule) for rule in NATIVE_APP_RULES],
        "privacy": (
            "Native runtime metadata only: pid, bundle identifier, application "
            "name, and bundle path from NSWorkspace or exact known launchctl "
            "service labels; no windows or application content."
        ),
    }


def _default_bundle_paths() -> tuple[Path, ...]:
    names = ("ChatGPT.app", "Claude.app", "Codex.app", "Cursor.app")
    roots = (Path("/Applications"), Path.home() / "Applications")
    return tuple(root / name for root in roots for name in names)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _plist_string(info: dict[str, object], key: str) -> str | None:
    value = info.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _looks_relevant(application: RunningApplication) -> bool:
    haystack = " ".join(
        (
            application.name or "",
            application.bundle_identifier or "",
            application.bundle_path or "",
        )
    ).casefold()
    return any(token in haystack for token in ("chatgpt", "claude", "codex", "cursor"))


def _is_number_suffixed_label(label: str, base_label: str) -> bool:
    """Accept launchd's numeric ASID-style suffix, never helper identifiers."""

    prefix = f"{base_label}."
    if not label.startswith(prefix):
        return False
    suffix_parts = label[len(prefix) :].split(".")
    return bool(suffix_parts) and all(part.isdigit() for part in suffix_parts)


def _deduplicate_applications(
    applications: Iterable[RunningApplication],
) -> tuple[RunningApplication, ...]:
    """Keep the richer first record when NSWorkspace and launchctl overlap."""

    unique: list[RunningApplication] = []
    seen: set[tuple[int, str | None]] = set()
    for application in applications:
        key = (application.pid, application.bundle_identifier)
        if key in seen:
            continue
        seen.add(key)
        unique.append(application)
    return tuple(unique)
