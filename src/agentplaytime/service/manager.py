"""LaunchAgent configuration and explicit, idempotent lifecycle operations."""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from xml.parsers.expat import ExpatError

from agentplaytime.config import DEFAULT_POLL_INTERVAL_SECONDS, default_database_path
from . import LABEL
from .files import atomic_write, read_owned, remove_owned

LAUNCHCTL = "/bin/launchctl"
RUNNER_MODULE = "agentplaytime.service.runner"


@dataclass(frozen=True)
class ServicePaths:
    plist: Path
    database: Path
    log: Path
    python: Path

    @classmethod
    def defaults(cls, home: Path | None = None) -> ServicePaths:
        home = Path.home() if home is None else home
        return cls(
            home / "Library/LaunchAgents" / f"{LABEL}.plist",
            default_database_path(home),
            home / "Library/Logs/AgentPlaytime/tracker.log",
            Path(sys.executable).absolute(),  # Never resolve away the venv symlink.
        )


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def run_command(arguments: list[str]) -> CommandResult:
    """Run a fixed argv; filter service print output before Python receives it.

    `launchctl print` contains inherited environment and argument sections. The
    external filter admits only top-level runtime fields, never those sections.
    """
    if arguments[:2] != [LAUNCHCTL, "print"]:
        result = subprocess.run(
            arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=40, check=False, shell=False,
        )
        return CommandResult(result.returncode)

    with subprocess.Popen(
        arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, shell=False,
    ) as launch:
        assert launch.stdout is not None
        try:
            with subprocess.Popen(
                ["/usr/bin/grep", "-E", "^\t(state|pid|last exit code) = "],
                stdin=launch.stdout, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, shell=False,
            ) as filtered:
                launch.stdout.close()
                launch.stdout = None
                try:
                    output, _ = filtered.communicate(timeout=15)
                    _, errors = launch.communicate(timeout=5)
                except BaseException:
                    filtered.kill()
                    filtered.wait()
                    raise
                if filtered.returncode not in (0, 1):
                    raise RuntimeError("launchctl metadata filter failed")
        except BaseException:
            launch.kill()
            launch.wait()
            raise
    return CommandResult(
        launch.returncode, output.decode("utf-8", errors="replace"),
        errors.decode("utf-8", errors="replace"),
    )


@dataclass
class ServiceStatus:
    installed: bool
    loaded: bool | None
    running: bool | None
    pid: int | None
    label: str
    plist_path: str
    python_path: str
    database_path: str
    log_path: str
    configuration_valid: bool | None
    tracker_state: str = "unknown"
    power_monitoring: str = "unknown"
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.errors:
            return 1
        return 2 if self.configuration_valid is False else 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ServiceManager:
    def __init__(
        self, paths: ServicePaths | None = None, *, uid: int | None = None,
        run: Callable[[list[str]], CommandResult] | None = None,
        platform: str = sys.platform, wait: Callable[[float], None] = time.sleep,
        interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self.paths = paths or ServicePaths.defaults()
        self.uid = os.getuid() if uid is None else uid
        self.run = run if run is not None else run_command
        self.platform = platform
        self.wait = wait
        if not isfinite(interval) or interval <= 0:
            raise ValueError("service interval must be finite and positive")
        self.interval = interval
        if self.paths.plist.name != f"{LABEL}.plist":
            raise ValueError("unexpected LaunchAgent filename")
        if any(not p.is_absolute() for p in asdict(self.paths).values()):
            raise ValueError("service paths must be absolute")

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{LABEL}"

    def plist(self) -> dict[str, object]:
        return {
            "Label": LABEL,
            "ProgramArguments": [str(self.paths.python), "-I", "-m", RUNNER_MODULE,
                                 "--database", str(self.paths.database),
                                 "--log", str(self.paths.log),
                                 "--interval", str(self.interval)],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
            "ThrottleInterval": 30,
            "ExitTimeOut": 30,
            "LimitLoadToSessionType": "Aqua",
            "Umask": 0o077,
        }

    def _call(self, *arguments: str) -> CommandResult:
        if self.platform != "darwin":
            raise RuntimeError("background service requires macOS")
        try:
            return self.run([LAUNCHCTL, *arguments])
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"launchctl {arguments[0]} could not complete") from exc

    def _required(self, *arguments: str) -> None:
        result = self._call(*arguments)
        if result.returncode:
            raise RuntimeError(
                f"launchctl {arguments[0]} failed (exit {result.returncode}); "
                "check service status and macOS Login Items & Extensions"
            )

    def _launch_state(self) -> tuple[bool, bool, int | None]:
        result = self._call("print", self.target)
        if result.returncode:
            # Do not confuse an inaccessible/missing GUI domain with absence.
            if "Could not find service" in result.stderr:
                return False, False, None
            raise RuntimeError(f"launchctl print failed (exit {result.returncode})")
        pid_match = re.search(r"^\s*pid = ([1-9][0-9]*)\s*$", result.stdout, re.M)
        state_match = re.search(r"^\s*state = (.+)$", result.stdout, re.M)
        if state_match is None:
            raise RuntimeError("launchctl print returned no recognizable service state")
        pid = int(pid_match.group(1)) if pid_match else None
        return True, state_match.group(1).strip() == "running" and pid is not None, pid

    def _read_plist(self) -> dict[str, object] | None:
        try:
            data = plistlib.loads(read_owned(self.paths.plist))
        except FileNotFoundError:
            return None
        except (plistlib.InvalidFileException, ExpatError, OverflowError, ValueError) as exc:
            raise ValueError("invalid or unsafe AgentPlaytime plist") from exc
        if not isinstance(data, dict) or data.get("Label") != LABEL:
            raise ValueError("plist does not belong to AgentPlaytime; refusing to replace/remove it")
        return data

    def _validate(self, data: dict[str, object], *, probe: bool = True) -> list[str]:
        issues: list[str] = []
        args = data.get("ProgramArguments")
        if not isinstance(args, list) or len(args) != 10 or not all(isinstance(a, str) for a in args):
            return ["invalid ProgramArguments; reinstall the service"]
        if args[1:4] != ["-I", "-m", RUNNER_MODULE] or args[4::2] != ["--database", "--log", "--interval"]:
            return ["unexpected service command; reinstall the service"]
        if not all(Path(args[i]).is_absolute() for i in (0, 5, 7)):
            issues.append("Python, database and log paths must be absolute")
        try:
            interval = float(args[9])
            if not isfinite(interval) or interval <= 0:
                raise ValueError
        except ValueError:
            issues.append("invalid polling interval")
        expected = self.plist()
        for key in expected.keys() - {"ProgramArguments"}:
            if type(data.get(key)) is not type(expected[key]) or data.get(key) != expected[key]:
                issues.append(f"unexpected {key} setting; reinstall the service")
        if data.keys() - expected.keys():
            issues.append("unrecognized plist settings; reinstall the service")
        executable = Path(args[0])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            issues.append("Python executable is missing or not executable; reinstall from a stable environment")
        elif probe and not issues:
            try:
                result = self.run([str(executable), "-I", "-c", f"import {RUNNER_MODULE}"])
                if result.returncode:
                    issues.append("AgentPlaytime service module cannot be imported by configured Python")
            except (OSError, subprocess.SubprocessError):
                issues.append("configured Python import check failed")
        return issues

    def status(self) -> ServiceStatus:
        status = ServiceStatus(
            self.paths.plist.exists() or self.paths.plist.is_symlink(),
            None, None, None, LABEL, str(self.paths.plist), str(self.paths.python),
            str(self.paths.database), str(self.paths.log), None,
        )
        try:
            data = self._read_plist()
            if data is not None:
                issues = self._validate(data)
                status.configuration_valid = not issues
                status.warnings.extend(issues)
                args = data.get("ProgramArguments")
                if isinstance(args, list) and len(args) == 10 and all(isinstance(a, str) for a in args):
                    status.python_path, status.database_path, status.log_path = args[0], args[5], args[7]
        except (OSError, ValueError) as exc:
            status.configuration_valid = False
            status.warnings.append(str(exc) if isinstance(exc, ValueError) else "cannot read AgentPlaytime plist")
        try:
            status.loaded, status.running, status.pid = self._launch_state()
            if status.loaded and not status.installed:
                status.configuration_valid = False
                status.warnings.append("service is loaded but its plist is missing")
        except RuntimeError as exc:
            status.errors.append(str(exc))
        if status.running and status.configuration_valid:
            self._read_runtime_status(status)
        return status

    def _read_runtime_status(self, status: ServiceStatus) -> None:
        try:
            data = json.loads(read_owned(Path(status.log_path).with_name("service-state.json"), limit=4096))
            age = datetime.now(UTC).timestamp() - data["updated_at"]
            if data["pid"] == status.pid and 0 <= age <= 120:
                if data["tracker_state"] in ("tracking", "sleeping", "waiting_for_lock", "starting", "stopped"):
                    status.tracker_state = data["tracker_state"]
                if data["power_monitoring"] in ("active", "degraded", "inactive"):
                    status.power_monitoring = data["power_monitoring"]
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def install(self) -> ServiceStatus:
        data = self.plist()
        issues = self._validate(data)
        if issues:
            raise ValueError("; ".join(issues))
        old = self._read_plist()  # Reject foreign files before touching launchd.
        loaded, _, _ = self._launch_state()
        if old != data:
            if loaded:
                self.stop()
            atomic_write(self.paths.plist, plistlib.dumps(data, sort_keys=True))
        return self.start()

    def start(self) -> ServiceStatus:
        data = self._read_plist()
        if data is None:
            raise ValueError("service is not installed; run 'agentplaytime service install'")
        issues = self._validate(data)
        if issues:
            raise ValueError("; ".join(issues))
        loaded, running, _ = self._launch_state()
        if not loaded:
            self._required("bootstrap", self.domain, str(self.paths.plist))
        elif not running:
            self._required("kickstart", self.target)  # No -k: do not kill an existing worker.
        return self.status()

    def stop(self) -> ServiceStatus:
        self._read_plist()  # Reject foreign files; allow absent or broken executable.
        loaded, _, _ = self._launch_state()
        if loaded:
            self._required("bootout", self.target)
            for _ in range(100):
                if not self._launch_state()[0]:
                    break
                self.wait(0.1)
            else:
                raise RuntimeError("service is still unloading; retry service status")
        return self.status()

    def restart(self) -> ServiceStatus:
        if self._read_plist() is None:
            raise ValueError("service is not installed")
        self.stop()
        return self.start()

    def uninstall(self) -> ServiceStatus:
        data = self._read_plist()
        self.stop()
        if data is not None:
            remove_owned(self.paths.plist)
        return self.status()
