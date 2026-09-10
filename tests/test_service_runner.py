"""Power, lock, signal, persistence and privacy tests with synthetic detectors."""

import json
import logging
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from agentplaytime import cli
from agentplaytime.config import Settings
from agentplaytime.service import power, runner
from agentplaytime.service.logging import log_transition, service_logger
from agentplaytime.service.power import MacPowerMonitor, PowerEvent, PowerQueue
from agentplaytime.service.runner import PowerAwareTracker, run_service
from agentplaytime.storage import Database
from agentplaytime.tracker import TrackingEngine, TrackingEvent
from agentplaytime.tracker.runtime import TrackerAlreadyRunning, exclusive_tracker_lock

BASE = datetime(2026, 9, 8, 10, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.seconds = 0

    def __call__(self):
        return BASE + timedelta(seconds=self.seconds)


class Detector:
    def __init__(self, tools=("Claude", "Codex")):
        self.tools = tools
        self.resets = 0
        self.calls = 0
        self.hook = None

    def __call__(self):
        self.calls += 1
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        return self.tools

    def reset(self):
        self.resets += 1


@pytest.fixture
def tracker(tmp_path):
    clock, detector, queue, events = Clock(), Detector(), PowerQueue(), []
    with Database(tmp_path / "sessions.db") as database:
        controller = PowerAwareTracker(TrackingEngine(database), detector, queue,
                                       events.append, logging.getLogger("power-test"), clock)
        yield controller, database, clock, detector, queue, events


def at(seconds):
    return BASE + timedelta(seconds=seconds)


def test_sleep_closes_every_session_at_notification_timestamp(tracker):
    controller, database, clock, detector, queue, events = tracker
    controller.poll()
    queue.put(PowerEvent("sleep", at(20)))
    clock.seconds = 3000  # Cannot execute the state machine until much later.
    controller.poll()
    assert controller.sleeping and not database.open_sessions()
    assert {row.ended_at for row in database.list_sessions()} == {at(20)}
    assert detector.calls == 1
    assert [e.action for e in events] == ["start", "start", "stop", "stop"]


def test_queued_wake_starts_new_sessions_and_excludes_sleep(tracker):
    controller, database, clock, detector, queue, _ = tracker
    controller.poll()
    queue.put(PowerEvent("sleep", at(20)))
    queue.put(PowerEvent("wake", at(3620)))
    clock.seconds = 3640
    controller.poll()
    assert detector.resets == 1
    assert {s.started_at for s in database.open_sessions()} == {at(3620)}
    controller.close()
    rows = database.list_sessions()
    assert len(rows) == 4
    assert sum(s.duration().total_seconds() for s in rows) == 80
    assert not database.open_sessions()


def test_multiple_cycles_are_ordered_even_when_enqueued_out_of_order(tracker):
    controller, database, clock, detector, queue, _ = tracker
    detector.tools = ("Codex",)
    controller.poll()
    for kind, seconds in [("wake", 110), ("sleep", 10), ("wake", 220), ("sleep", 120)]:
        queue.put(PowerEvent(kind, at(seconds)))
    clock.seconds = 230
    controller.poll()
    controller.close()
    rows = database.list_sessions()
    assert [(r.started_at, r.ended_at) for r in rows] == [
        (at(0), at(10)), (at(110), at(120)), (at(220), at(230))]
    assert detector.resets == 2


def test_duplicate_power_events_do_not_duplicate_sessions(tracker):
    controller, database, clock, detector, queue, _ = tracker
    detector.tools = ("Claude", "Claude")
    controller.poll()
    for kind, seconds in [("wake", 1), ("sleep", 10), ("sleep", 11), ("wake", 20), ("wake", 21)]:
        queue.put(PowerEvent(kind, at(seconds)))
    clock.seconds = 25
    controller.poll()
    controller.close()
    assert len(database.list_sessions()) == 2 and detector.resets == 1


def test_wake_redetects_tools_instead_of_reusing_stale_state(tracker):
    controller, database, clock, detector, queue, _ = tracker
    controller.poll()
    queue.put(PowerEvent("sleep", at(10)))
    controller.process_power_events()
    detector.tools = ("ChatGPT",)
    queue.put(PowerEvent("wake", at(50)))
    controller.process_power_events()
    assert [(r.tool, r.started_at) for r in database.open_sessions()] == [("ChatGPT", at(50))]


def test_power_event_during_detection_discards_stale_sample(tracker):
    controller, database, clock, detector, queue, _ = tracker
    controller.poll()
    clock.seconds = 10
    detector.hook = lambda: queue.put(PowerEvent("sleep", at(11)))
    controller.poll()
    assert controller.sleeping
    assert len(database.list_sessions()) == 2 and not database.open_sessions()
    assert all(r.ended_at == at(11) for r in database.list_sessions())


def test_shutdown_while_sleeping_does_not_count_time_after_sleep(tracker):
    controller, database, clock, _, queue, _ = tracker
    controller.poll()
    queue.put(PowerEvent("sleep", at(10)))
    clock.seconds = 1000
    controller.close()
    assert all(r.ended_at == at(10) for r in database.list_sessions())


def test_clock_adjustment_does_not_create_negative_sessions(tracker):
    controller, database, clock, detector, _, _ = tracker
    clock.seconds = 10
    controller.poll()
    clock.seconds = 5
    detector.tools = ()
    controller.poll()
    assert all(r.duration().total_seconds() == 0 for r in database.list_sessions())


def test_sleep_delivered_while_sampling_shutdown_time_is_not_missed(tracker):
    controller, database, _, _, queue, _ = tracker
    controller.poll()
    def shutdown_clock():
        queue.put(PowerEvent("sleep", at(10)))
        return at(1000)
    controller.clock = shutdown_clock
    controller.close()
    assert all(r.ended_at == at(10) for r in database.list_sessions())


@pytest.mark.parametrize("kind,stamp", [("bad", BASE), ("sleep", datetime(2026, 1, 1))])
def test_power_events_validate_type_and_aware_timestamp(kind, stamp):
    with pytest.raises(ValueError):
        PowerEvent(kind, stamp)


class FakeMonitor:
    def __init__(self, clock, on_wait=None, active=True):
        self.clock, self.on_wait, self.active = clock, on_wait, active
        self.waits = 0
        self.closed = False

    def start(self, deliver):
        self.deliver = deliver
        return self.active

    def wait(self, seconds, stop):
        self.waits += 1
        self.clock.seconds += seconds
        if self.on_wait:
            self.on_wait(self, stop)
        else:
            stop.set()

    def close(self):
        self.closed = True
        self.active = False


def test_runner_graceful_close_degraded_diagnostics_and_no_interactive_output(tmp_path, capsys):
    clock = Clock()
    monitor = FakeMonitor(clock, active=False)
    path, log = tmp_path / "sessions.db", tmp_path / "logs/tracker.log"
    assert run_service(Settings(path), log, detector=Detector(), monitor=monitor,
                       clock=clock, monotonic=lambda: clock.seconds, manage_signals=False) == 0
    with Database(path, read_only=True) as database:
        assert len(database.list_sessions()) == 2 and not database.open_sessions()
    content = log.read_text()
    assert "sleep exclusion is degraded" in content
    assert "Service stopped" in content and "START Codex" in content and "STOP Claude" in content
    assert "Ctrl-C" not in content and not capsys.readouterr().out
    state = json.loads(log.with_name("service-state.json").read_text())
    assert state["tracker_state"] == "stopped" and state["power_monitoring"] == "degraded"
    assert monitor.closed


def test_runner_power_monitor_failure_is_reported_at_runtime(tmp_path):
    clock = Clock()
    def fail_then_stop(monitor, stop):
        monitor.active = False
        if monitor.waits == 2:
            stop.set()
    monitor = FakeMonitor(clock, fail_then_stop)
    log = tmp_path / "logs/tracker.log"
    assert run_service(Settings(tmp_path / "data.db"), log, detector=Detector(), monitor=monitor,
                       clock=clock, monotonic=lambda: clock.seconds, manage_signals=False) == 0
    assert "Power monitor failed; sleep exclusion is degraded" in log.read_text()


def test_runner_waits_for_manual_lock_without_touching_sessions(tmp_path):
    path, log, clock = tmp_path / "data.db", tmp_path / "logs/tracker.log", Clock()
    with Database(path) as database:
        database.start_session("Claude", started_at=BASE)
    before = path.read_bytes()
    with exclusive_tracker_lock(path):
        assert run_service(Settings(path), log, detector=Detector(), monitor=FakeMonitor(clock),
                           clock=clock, monotonic=lambda: clock.seconds, manage_signals=False) == 0
    assert path.read_bytes() == before
    assert "waiting for its lock" in log.read_text()


def test_failed_monitor_does_not_leave_runner_permanently_asleep(tmp_path):
    clock = Clock()
    def fail_while_sleeping(monitor, stop):
        if monitor.waits == 1:
            monitor.deliver(PowerEvent("sleep", clock()))
            clock.seconds += 60
            monitor.active = False
        else:
            stop.set()
    path, log = tmp_path / "data.db", tmp_path / "logs/tracker.log"
    assert run_service(Settings(path), log, detector=Detector(("Codex",)),
                       monitor=FakeMonitor(clock, fail_while_sleeping), clock=clock,
                       monotonic=lambda: clock.seconds, manage_signals=False) == 0
    with Database(path, read_only=True) as database:
        rows = database.list_sessions()
        assert len(rows) == 2
        assert rows[0].ended_at == at(0.1) and rows[1].started_at == at(60.1)
    assert "Resuming tracking without power notifications" in log.read_text()


def test_manual_tracker_rejected_while_service_holds_lock_but_stats_work(tmp_path, capsys):
    path, log, clock = tmp_path / "data.db", tmp_path / "logs/tracker.log", Clock()
    def check_lock(monitor, stop):
        assert cli.main(["track", "--database", str(path)]) == 1
        assert "another tracker" in capsys.readouterr().err
        assert cli.main(["stats", "--database", str(path), "--json"]) == 0
        assert len(json.loads(capsys.readouterr().out)["tools"]) == 2
        stop.set()
    assert run_service(Settings(path), log, detector=Detector(), monitor=FakeMonitor(clock, check_lock),
                       clock=clock, monotonic=lambda: clock.seconds, manage_signals=False) == 0
    with exclusive_tracker_lock(path):
        pass  # Shutdown released the lock.


def test_lock_deduplicates_symlink_database_paths(tmp_path):
    path, alias = tmp_path / "data.db", tmp_path / "alias.db"
    alias.symlink_to(path)
    with exclusive_tracker_lock(path):
        with pytest.raises(TrackerAlreadyRunning):
            with exclusive_tracker_lock(alias):
                pytest.fail("second tracker acquired lock")


def test_real_sigterm_closes_sessions_in_isolated_child(tmp_path):
    # A synthetic runner, not a LaunchAgent. Explicit temp paths; no real
    # detectors, power observers, user HOME, or launchctl calls in this child.
    path, log = tmp_path / "signal.db", tmp_path / "logs/tracker.log"
    code = """
import sys
from pathlib import Path
from agentplaytime.config import Settings
from agentplaytime.service.runner import run_service
class Detector:
    def __call__(self): return ('Codex',)
    def reset(self): pass
class Monitor:
    active = False
    def start(self, deliver): return False
    def wait(self, seconds, stop): stop.wait(seconds)
    def close(self): pass
raise SystemExit(run_service(Settings(Path(sys.argv[1]), 0.1), Path(sys.argv[2]),
                             detector=Detector(), monitor=Monitor()))
"""
    with subprocess.Popen([sys.executable, "-c", code, str(path), str(log)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if log.exists() and "START Codex" in log.read_text():
                    break
                assert child.poll() is None, child.communicate()
                time.sleep(0.02)
            else:
                pytest.fail("synthetic service did not become ready")
            child.send_signal(signal.SIGTERM)
            stdout, stderr = child.communicate(timeout=8)
            assert child.returncode == 0 and not stdout and not stderr
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
    with Database(path, read_only=True) as database:
        rows = database.list_sessions()
        assert len(rows) == 1 and rows[0].ended_at is not None
    assert "STOP Codex" in log.read_text()


def test_logs_rotate_to_three_bounded_backups(tmp_path):
    log = tmp_path / "logs/tracker.log"
    with service_logger(log, max_bytes=256) as logger:
        for _ in range(100):
            logger.info("Service starting version=0.3.0")
    logs = sorted(log.parent.iterdir())
    assert [p.name for p in logs] == ["tracker.log", "tracker.log.1", "tracker.log.2", "tracker.log.3"]
    assert all(p.stat().st_size <= 256 for p in logs)
    assert all(p.stat().st_mode & 0o077 == 0 for p in logs)


def test_service_logs_do_not_include_unknown_names_or_exception_content(tmp_path):
    log, clock = tmp_path / "logs/tracker.log", Clock()
    secret = "private-prompt-environment-clipboard-content"
    with service_logger(log) as logger:
        log_transition(logger, TrackingEvent("start", secret, 1, BASE))
    detector = Detector()
    detector.last_snapshot = SimpleNamespace(stale_backends=("native",), errors=(secret,))
    def stop_with_error(monitor, stop):
        raise RuntimeError(secret)
    assert run_service(Settings(tmp_path / "data.db"), log, detector=detector,
                       monitor=FakeMonitor(clock, stop_with_error), clock=clock,
                       monotonic=lambda: clock.seconds, manage_signals=False) == 1
    text = log.read_text()
    assert secret not in text and "unrecognized-tool" in text
    assert "Detection backend degraded" in text and "error_type=RuntimeError" in text
    assert "Traceback" not in text


def test_logging_rejects_symlink_backup_without_touching_target(tmp_path):
    log = tmp_path / "logs/tracker.log"
    log.parent.mkdir()
    victim = tmp_path / "untouched"
    victim.write_text("keep")
    log.with_name("tracker.log.1").symlink_to(victim)
    with pytest.raises(ValueError):
        with service_logger(log):
            pytest.fail("unsafe backup accepted")
    assert victim.read_text() == "keep"


def test_existing_database_schema_and_records_survive_read_only_stats(tmp_path, capsys):
    path = tmp_path / "existing.db"
    with Database(path) as database:
        session = database.start_session("Claude", started_at=BASE)
        database.end_session(session.id, ended_at=at(60))
    original = path.read_bytes()
    with sqlite3.connect(path) as connection:
        schema = connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()
    assert cli.main(["stats", "--database", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total_seconds"] == 60
    assert path.read_bytes() == original
    with Database(path, read_only=True) as database:
        with pytest.raises(sqlite3.OperationalError):
            database.start_session("Codex", started_at=at(100))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == schema
        assert connection.execute("PRAGMA user_version").fetchone() == version


def test_stats_missing_database_does_not_create_directory(tmp_path):
    path = tmp_path / "not-created/empty.db"
    assert cli.main(["stats", "--database", str(path)]) == 0
    assert not path.parent.exists()


def fake_frameworks(fail_registration=False, fail_pump=False):
    class Center:
        def __init__(self):
            self.observers = []
            self.removed = []
        def addObserverForName_object_queue_usingBlock_(self, name, obj, queue, block):
            assert obj is None and queue is None
            if fail_registration and self.observers:
                raise RuntimeError("observer failed")
            self.observers.append((name, block))
            return name
        def removeObserver_(self, token):
            self.removed.append(token)
    center = Center()
    workspace = SimpleNamespace(sharedWorkspace=lambda: SimpleNamespace(notificationCenter=lambda: center))
    date = SimpleNamespace(dateWithTimeIntervalSinceNow_=lambda seconds: seconds)
    def pump(mode, date):
        if fail_pump:
            raise RuntimeError("run loop failed")
        return False
    loop = SimpleNamespace(currentRunLoop=lambda: SimpleNamespace(runMode_beforeDate_=pump))
    return center, (workspace, "sleep-name", "wake-name", date, loop, "default-mode")


def test_native_monitor_subscribes_to_workspace_and_retains_timestamp_only(monkeypatch):
    center, api = fake_frameworks()
    monkeypatch.setattr(power, "_frameworks", lambda: api)
    monkeypatch.setattr(power.sys, "platform", "darwin")
    clock, queue = Clock(), PowerQueue()
    monitor = MacPowerMonitor(clock)
    assert monitor.start(queue.put) and monitor.start(queue.put)
    assert len(center.observers) == 2
    class PrivateNotification:
        def __getattr__(self, key):
            pytest.fail("notification content was inspected")
    for seconds, (_, callback) in enumerate(center.observers):
        clock.seconds = seconds
        callback(PrivateNotification())
    assert queue.drain() == [PowerEvent("sleep", at(0)), PowerEvent("wake", at(1))]
    monitor.close()
    monitor.close()
    assert center.removed == ["sleep-name", "wake-name"] and not monitor.active


@pytest.mark.parametrize("registration,pump", [(True, False), (False, True)])
def test_native_monitor_failures_degrade_and_cleanup(monkeypatch, registration, pump):
    center, api = fake_frameworks(registration, pump)
    monkeypatch.setattr(power, "_frameworks", lambda: api)
    monkeypatch.setattr(power.sys, "platform", "darwin")
    monitor = MacPowerMonitor()
    monitor.start(lambda _: None)
    stop = Event()
    stop.set()
    monitor.wait(0, stop)
    assert not monitor.active
    assert len(center.removed) == len(center.observers)


def test_power_capability_is_not_a_claim_of_active_subscription(monkeypatch):
    monkeypatch.setattr(power.sys, "platform", "darwin")
    monkeypatch.setattr(power, "_frameworks", lambda: ())
    assert power.power_capability()["available"] is True
    def unavailable():
        raise ImportError("private text")
    monkeypatch.setattr(power, "_frameworks", unavailable)
    result = power.power_capability()
    assert not result["available"] and "private text" not in str(result)
