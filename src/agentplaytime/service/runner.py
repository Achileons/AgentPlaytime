"""Dedicated launchd entry point and serialized sleep-aware tracking loop."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Protocol

from agentplaytime import __version__
from agentplaytime.config import DEFAULT_POLL_INTERVAL_SECONDS, Settings
from agentplaytime.detectors import RuntimeDetector
from agentplaytime.storage import Database
from agentplaytime.tracker import TrackingEngine, TrackingEvent
from agentplaytime.tracker.runtime import (
    TrackerAlreadyRunning, exclusive_tracker_lock,
    install_shutdown_handlers, restore_signal_handlers,
)
from .files import atomic_write
from .logging import log_transition, service_logger
from .manager import ServicePaths
from .power import MacPowerMonitor, PowerEvent, PowerQueue, utc_now


class Detector(Protocol):
    def __call__(self) -> Iterable[str]: ...
    def reset(self) -> None: ...


class PowerMonitor(Protocol):
    active: bool
    def start(self, deliver: Callable[[PowerEvent], None]) -> bool: ...
    def wait(self, seconds: float, stop: Event) -> None: ...
    def close(self) -> None: ...


class PowerAwareTracker:
    """Only the owning runner thread calls this state machine.

    Notification callbacks merely enqueue timestamps. Sampling uses a timestamp
    taken before detection; a notification arriving during detection cannot end
    a newly created session before it began. Queued events are processed before
    any new sample is committed, and the pre-sleep sample is discarded.
    """

    def __init__(self, engine: TrackingEngine, detector: Detector, queue: PowerQueue,
                 emit: Callable[[TrackingEvent], None], logger: logging.Logger,
                 clock: Callable[[], datetime] = utc_now) -> None:
        self.engine, self.detector, self.queue = engine, detector, queue
        self.emit, self.logger, self.clock = emit, logger, clock
        self.sleeping = False
        self.last_observation: datetime | None = None

    def _reconcile(self, tools: Iterable[str], at: datetime) -> None:
        if self.last_observation is not None and at < self.last_observation:
            # Wall clock adjustments should not create negative sessions.
            self.logger.warning("Clock moved backward; clamping observation timestamp")
            at = self.last_observation
        for event in self.engine.reconcile(tools, observed_at=at):
            self.emit(event)
        self.last_observation = at

    def process_power_events(self) -> bool:
        events = self.queue.drain()
        for event in events:
            self.logger.info("POWER %s at=%s", event.kind, event.at.isoformat())
            if event.kind == "sleep" and not self.sleeping:
                self._reconcile((), event.at)
                self.sleeping = True
            elif event.kind == "wake" and self.sleeping:
                self.detector.reset()
                self.sleeping = False
                self._reconcile(self.detector(), event.at)
        return bool(events)

    def poll(self) -> None:
        self.process_power_events()
        if self.sleeping:
            return
        observed_at = self.clock()
        tools = self.detector()
        if self.process_power_events():
            return  # Discard a detection spanning a sleep/wake boundary.
        self._reconcile(tools, observed_at)

    def close(self) -> None:
        # Sample before draining: suspension between the drain and a later
        # timestamp must not stretch shutdown across an unprocessed sleep.
        at = self.clock()
        self.process_power_events()
        if self.last_observation is not None:
            at = max(at, self.last_observation)
        for event in self.engine.close(observed_at=at):
            self.emit(event)


def run_service(
    settings: Settings, log_path: Path, *, stop: Event | None = None,
    detector: Detector | None = None, monitor: PowerMonitor | None = None,
    clock: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    manage_signals: bool = True,
) -> int:
    stop = stop if stop is not None else Event()
    detector = detector if detector is not None else RuntimeDetector()
    monitor = monitor if monitor is not None else MacPowerMonitor(clock)
    queue = PowerQueue()
    previous = install_shutdown_handlers(stop) if manage_signals else {}
    try:
        with service_logger(log_path) as logger:
            logger.info("Service starting version=%s", __version__)
            state = "starting"
            last_health: object = None

            def write_health() -> None:
                atomic_write(log_path.with_name("service-state.json"), json.dumps({
                    "pid": os.getpid(), "updated_at": clock().timestamp(),
                    "tracker_state": state,
                    "power_monitoring": "active" if monitor.active else "degraded",
                }).encode())

            monitor.start(queue.put)
            if not monitor.active:
                logger.warning("Power monitoring unavailable; sleep exclusion is degraded")
            write_health()
            waiting_logged = False
            try:
                while not stop.is_set():
                    try:
                        with exclusive_tracker_lock(settings.database_path):
                            with Database(settings.database_path) as database:
                                controller = PowerAwareTracker(
                                    TrackingEngine(database), detector, queue,
                                    lambda event: log_transition(logger, event), logger, clock,
                                )
                                # Power events while waiting for another tracker
                                # must not retroactively end that tracker's rows.
                                pending = queue.drain()
                                if pending:
                                    controller.sleeping = pending[-1].kind == "sleep"
                                next_poll = monotonic()
                                next_health = next_poll
                                was_active = monitor.active
                                try:
                                    while not stop.is_set():
                                        controller.process_power_events()
                                        tick = monotonic()
                                        if tick >= next_poll:
                                            controller.poll()
                                            next_poll = monotonic() + settings.poll_interval_seconds
                                            snapshot = getattr(detector, "last_snapshot", None)
                                            health = getattr(snapshot, "stale_backends", ())
                                            if health != last_health:
                                                if health:
                                                    # Never log arbitrary detector exception text.
                                                    logger.warning("Detection backend degraded; retaining last known state")
                                                elif last_health:
                                                    logger.info("Detection backends recovered")
                                                last_health = health
                                        if was_active and not monitor.active:
                                            logger.warning("Power monitor failed; sleep exclusion is degraded")
                                            if controller.sleeping:
                                                # There may never be a wake callback now. Resume
                                                # at this observation, without filling the gap.
                                                logger.warning("Resuming tracking without power notifications")
                                                controller.sleeping = False
                                                detector.reset()
                                                controller.poll()
                                        current = "sleeping" if controller.sleeping else "tracking"
                                        if tick >= next_health or current != state or was_active != monitor.active:
                                            state = current
                                            write_health()
                                            next_health = tick + 30
                                        was_active = monitor.active
                                        monitor.wait(0.1, stop)
                                finally:
                                    controller.close()
                            break
                    except TrackerAlreadyRunning:
                        state = "waiting_for_lock"
                        if not waiting_logged:
                            logger.warning("Another tracker owns the database; waiting for its lock")
                            waiting_logged = True
                        write_health()
                        retry_at = monotonic() + min(settings.poll_interval_seconds, 2)
                        while not stop.is_set():
                            remaining = retry_at - monotonic()
                            if remaining <= 0:
                                break
                            monitor.wait(min(remaining, 0.1), stop)
            except Exception as exc:
                logger.error("Service failed error_type=%s", type(exc).__name__)
                return 1
            finally:
                state = "stopped"
                write_health()
                monitor.close()
                logger.info("Service stopped")
    finally:
        monitor.close()
        if manage_signals:
            restore_signal_handlers(previous)
    return 0


def main(argv: list[str] | None = None) -> int:
    defaults = ServicePaths.defaults()
    parser = argparse.ArgumentParser(description="AgentPlaytime background runner")
    parser.add_argument("--database", type=Path, default=defaults.database)
    parser.add_argument("--log", type=Path, default=defaults.log)
    parser.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    args = parser.parse_args(argv)
    try:
        return run_service(Settings(args.database, args.interval), args.log)
    except Exception as exc:
        # launchd drops standard output by default; do not create an unbounded
        # stderr file or expose exception contents if log setup itself fails.
        print(f"AgentPlaytime service startup failed ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
