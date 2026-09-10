"""LaunchAgent lifecycle tests: all paths and launchctl state are synthetic."""

import json
import os
import plistlib
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentplaytime import cli
from agentplaytime.service import LABEL, files, manager
from agentplaytime.service.manager import CommandResult, ServiceManager, ServicePaths

# Preserve the implementation before the safety fixture replaces its default.
execute_command = manager.run_command


class FakeLaunchctl:
    def __init__(self):
        self.loaded = False
        self.running = False
        self.calls = []
        self.fail = None
        self.missing_module = False

    def __call__(self, arguments):
        self.calls.append(arguments)
        if arguments[0] != manager.LAUNCHCTL:
            assert arguments[1:] == ["-I", "-c", "import agentplaytime.service.runner"]
            return CommandResult(int(self.missing_module))
        action = arguments[1]
        if self.fail == action:
            return CommandResult(5, stderr="private-error-text-must-not-leak")
        if action == "print":
            if not self.loaded:
                return CommandResult(113, stderr="Could not find service")
            return CommandResult(0, "\tstate = running\n\tpid = 4242\n" if self.running else "\tstate = waiting\n")
        if action == "bootstrap":
            assert arguments[2] == "gui/501"
            assert Path(arguments[3]).is_file()
            self.loaded = self.running = True
        elif action == "bootout":
            assert arguments[2] == f"gui/501/{LABEL}"
            self.loaded = self.running = False
        elif action == "kickstart":
            assert arguments[2:] == [f"gui/501/{LABEL}"]
            self.running = True
        else:
            pytest.fail(f"Unexpected command: {action}")
        return CommandResult(0)

    @property
    def mutations(self):
        return [call[1] for call in self.calls if call[0] == manager.LAUNCHCTL and call[1] != "print"]


@pytest.fixture
def service(tmp_path):
    root = tmp_path / "project with spaces"
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    paths = replace(ServicePaths.defaults(tmp_path / "user home"), python=python)
    fake = FakeLaunchctl()
    return ServiceManager(paths, uid=501, run=fake, platform="darwin", wait=lambda _: None), fake


def test_plist_roundtrip_label_paths_and_safe_settings(service):
    service, _ = service
    data = plistlib.loads(plistlib.dumps(service.plist()))
    assert data["Label"] == LABEL
    args = data["ProgramArguments"]
    assert args[0] == str(service.paths.python)
    assert "project with spaces" in args[0]
    assert args[1:4] == ["-I", "-m", "agentplaytime.service.runner"]
    assert all(Path(args[i]).is_absolute() for i in (0, 5, 7))
    assert data["RunAtLoad"] is True and data["KeepAlive"] is True
    assert data["ThrottleInterval"] == 30 and data["ProcessType"] == "Background"
    assert "EnvironmentVariables" not in data and "StandardOutPath" not in data


def test_install_is_atomic_and_idempotent(service, monkeypatch):
    service, fake = service
    original_replace = os.replace
    replacements = []

    def checked_replace(source, target, **kwargs):
        assert not service.paths.plist.exists()
        assert target == f"{LABEL}.plist"
        staged = service.paths.plist.parent / source
        assert plistlib.loads(staged.read_bytes()) == service.plist()
        replacements.append((source, target))
        return original_replace(source, target, **kwargs)

    monkeypatch.setattr(files.os, "replace", checked_replace)
    assert service.install().running
    modified = service.paths.plist.stat().st_mtime_ns
    assert service.install().running
    assert service.paths.plist.stat().st_mtime_ns == modified
    assert len(replacements) == 1 and fake.mutations == ["bootstrap"]
    assert service.paths.plist.stat().st_mode & 0o777 == 0o600
    assert list(service.paths.plist.parent.iterdir()) == [service.paths.plist]


def test_install_updates_configuration_after_graceful_stop(service):
    service, fake = service
    service.install()
    updated = ServiceManager(service.paths, uid=501, run=fake, platform="darwin", interval=5)
    assert updated.install().running
    assert fake.mutations == ["bootstrap", "bootout", "bootstrap"]
    assert plistlib.loads(service.paths.plist.read_bytes())["ProgramArguments"][-1] == "5"


def test_install_does_not_overwrite_after_stop_failure(service):
    service, fake = service
    service.install()
    original = service.paths.plist.read_bytes()
    fake.fail = "bootout"
    service.interval = 6
    with pytest.raises(RuntimeError, match="bootout failed"):
        service.install()
    assert service.paths.plist.read_bytes() == original


def test_start_stop_restart_sequences_are_idempotent(service):
    service, fake = service
    service.install()
    service.start()
    service.stop()
    assert not service.stop().loaded
    service.start()
    service.restart()
    assert fake.mutations == ["bootstrap", "bootout", "bootstrap", "bootout", "bootstrap"]
    fake.running = False
    assert service.start().running
    assert fake.mutations[-1] == "kickstart"


@pytest.mark.parametrize("action", ["start", "restart"])
def test_start_restart_require_install(service, action):
    service, fake = service
    with pytest.raises(ValueError, match="not installed"):
        getattr(service, action)()
    assert not fake.mutations


def test_uninstall_preserves_database_logs_and_neighbor_plists(service):
    service, fake = service
    service.install()
    service.paths.database.parent.mkdir(parents=True)
    service.paths.database.write_bytes(b"existing sessions")
    service.paths.log.parent.mkdir(parents=True)
    service.paths.log.write_text("existing logs")
    neighbor = service.paths.plist.with_name("unrelated.plist")
    neighbor.write_bytes(b"unrelated")
    assert not service.uninstall().installed
    assert not service.uninstall().loaded
    assert not service.paths.plist.exists()
    assert service.paths.database.read_bytes() == b"existing sessions"
    assert service.paths.log.read_text() == "existing logs"
    assert neighbor.read_bytes() == b"unrelated"
    assert fake.mutations == ["bootstrap", "bootout"]


def test_status_absent_stopped_running_is_read_only(service):
    service, fake = service
    absent = service.status()
    assert not absent.installed and not absent.loaded and not absent.running
    assert absent.exit_code == 0 and absent.configuration_valid is None
    assert not service.paths.plist.parent.exists()
    service.install()
    running = service.status()
    assert running.pid == 4242 and running.running and running.configuration_valid
    service.stop()
    stopped = service.status()
    assert stopped.installed and not stopped.loaded and not stopped.running
    assert stopped.exit_code == 0 and stopped.configuration_valid
    before = list(fake.mutations)
    service.status()
    assert fake.mutations == before


@pytest.mark.parametrize("payload", [b"not a plist", b"<plist><dict><key>Label</key>", plistlib.dumps([])])
def test_broken_plist_is_reported_without_traceback(service, payload, monkeypatch, capsys):
    service, _ = service
    files.atomic_write(service.paths.plist, payload)
    monkeypatch.setattr(cli, "ServiceManager", lambda: service)
    assert cli.main(["service", "status"]) == 2
    output = capsys.readouterr()
    assert "Configuration: invalid" in output.out
    assert "Traceback" not in output.out + output.err


@pytest.mark.parametrize("fault", ["missing_python", "missing_module", "relative_path", "wrong_command", "unexpected_key", "bad_interval"])
def test_invalid_configuration_warnings(service, fault):
    service, fake = service
    data = service.plist()
    if fault == "missing_python":
        data["ProgramArguments"][0] = "/nonexistent-python"
    elif fault == "missing_module":
        fake.missing_module = True
    elif fault == "relative_path":
        data["ProgramArguments"][5] = "relative.db"
    elif fault == "wrong_command":
        data["ProgramArguments"][3] = "other.module"
    elif fault == "unexpected_key":
        data["EnvironmentVariables"] = {"SECRET": "do-not-output-this"}
    else:
        data["ProgramArguments"][-1] = "nan"
    files.atomic_write(service.paths.plist, plistlib.dumps(data))
    result = service.status()
    assert result.configuration_valid is False and result.exit_code == 2
    assert result.warnings and "do-not-output-this" not in str(result.to_dict())


def test_install_fails_before_creating_files_for_missing_module(service):
    service, fake = service
    fake.missing_module = True
    with pytest.raises(ValueError, match="cannot be imported"):
        service.install()
    assert not service.paths.plist.parent.exists() and not fake.mutations


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_foreign_label_is_never_overwritten_or_removed(service, action):
    service, fake = service
    data = plistlib.dumps({"Label": "another.service"})
    files.atomic_write(service.paths.plist, data)
    with pytest.raises(ValueError, match="does not belong"):
        getattr(service, action)()
    assert service.paths.plist.read_bytes() == data and not fake.mutations


def test_symlink_plist_and_directory_are_rejected(service, tmp_path):
    service, fake = service
    victim = tmp_path / "do-not-change"
    victim.write_bytes(b"keep")
    service.paths.plist.parent.mkdir(parents=True)
    service.paths.plist.symlink_to(victim)
    with pytest.raises(ValueError, match="unsafe"):
        service.install()
    with pytest.raises(ValueError):
        service.uninstall()
    assert victim.read_bytes() == b"keep" and not fake.mutations
    service.paths.plist.unlink()
    service.paths.plist.parent.rmdir()
    service.paths.plist.parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        service.install()
    assert not (tmp_path / f"{LABEL}.plist").exists()


def test_failed_atomic_replace_leaves_old_contents_and_no_temp_files(tmp_path, monkeypatch):
    target = tmp_path / "owned.plist"
    files.atomic_write(target, b"old")
    def fail(*args, **kwargs):
        raise OSError("simulated replace failure")
    monkeypatch.setattr(files.os, "replace", fail)
    with pytest.raises(OSError):
        files.atomic_write(target, b"new")
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_status_does_not_confuse_launchctl_failure_with_absence(service):
    service, fake = service
    fake.fail = "print"
    result = service.status()
    assert result.loaded is None and result.running is None
    assert result.exit_code == 1 and result.errors
    assert "private-error" not in str(result.to_dict())


def test_launchctl_failure_is_clean_at_cli(service, monkeypatch, capsys):
    service, fake = service
    fake.fail = "bootstrap"
    monkeypatch.setattr(cli, "ServiceManager", lambda: service)
    assert cli.main(["service", "install"]) == 1
    output = capsys.readouterr()
    assert "bootstrap failed" in output.err
    assert "Traceback" not in output.err and "private-error" not in output.err


def test_runtime_status_requires_matching_fresh_pid(service):
    service, _ = service
    service.install()
    path = service.paths.log.with_name("service-state.json")
    payload = {"pid": 4242, "updated_at": datetime.now(UTC).timestamp(),
               "tracker_state": "waiting_for_lock", "power_monitoring": "active"}
    files.atomic_write(path, json.dumps(payload).encode())
    status = service.status()
    assert status.tracker_state == "waiting_for_lock" and status.power_monitoring == "active"
    payload["updated_at"] -= 121
    files.atomic_write(path, json.dumps(payload).encode())
    assert service.status().power_monitoring == "unknown"
    payload["updated_at"] += 121
    payload["pid"] = 999
    files.atomic_write(path, json.dumps(payload).encode())
    assert service.status().tracker_state == "unknown"


def test_non_macos_status_is_clear(service):
    service, fake = service
    service.platform = "linux"
    assert service.status().errors == ["background service requires macOS"]
    assert not fake.mutations


def test_subprocess_mutations_use_argv_no_shell_and_discard_output(monkeypatch):
    seen = []
    def run(arguments, **kwargs):
        seen.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0)
    monkeypatch.setattr(manager.subprocess, "run", run)
    for action in ("bootstrap", "bootout", "kickstart"):
        assert execute_command([manager.LAUNCHCTL, action, "path with spaces"]).returncode == 0
    for arguments, kwargs in seen:
        assert isinstance(arguments, list) and arguments[-1] == "path with spaces"
        assert kwargs["shell"] is False
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL


def test_launchctl_print_is_filtered_before_python_receives_private_sections(monkeypatch):
    # Replace only the launchctl child with a synthetic output producer. The
    # fixed system grep pipeline is real; no real launchctl is called.
    real_popen = subprocess.Popen
    raw = "\tstate = running\n\tpid = 4242\n\tenvironment = {\n\t\tSECRET = hidden\n\t}\n\targuments = {\n\t\tprivate-prompt\n\t}\n"
    seen = []
    def popen(arguments, **kwargs):
        seen.append((arguments, kwargs))
        if arguments[0] == manager.LAUNCHCTL:
            arguments = [sys.executable, "-c", f"print({raw!r})"]
        return real_popen(arguments, **kwargs)
    monkeypatch.setattr(manager.subprocess, "Popen", popen)
    result = execute_command([manager.LAUNCHCTL, "print", f"gui/501/{LABEL}"])
    assert result.returncode == 0
    assert result.stdout == "\tstate = running\n\tpid = 4242\n"
    assert all(kwargs["shell"] is False for _, kwargs in seen)


def test_cli_status_and_doctor_json_have_equivalent_service_information(service, monkeypatch, capsys):
    service, fake = service
    service.install()
    monkeypatch.setattr(cli, "ServiceManager", lambda: service)
    monkeypatch.setattr(cli, "doctor_diagnostics", lambda: {
        "native_applications": {}, "processes": {}, "detected_tools": []})
    monkeypatch.setattr(cli, "power_capability", lambda: {"available": True, "reason": "test API"})
    before = list(fake.mutations)
    assert cli.main(["service", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert cli.main(["doctor", "--json"]) == 0
    doctor = json.loads(capsys.readouterr().out)["service"]
    assert doctor.pop("power_api")["available"] is True
    assert doctor == status and fake.mutations == before
