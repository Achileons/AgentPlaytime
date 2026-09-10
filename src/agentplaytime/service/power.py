"""Timestamp-only macOS power events; callbacks never mutate the tracker."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event, Lock
from typing import Any, Literal


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class PowerEvent:
    kind: Literal["sleep", "wake"]
    at: datetime

    def __post_init__(self) -> None:
        if self.kind not in ("sleep", "wake"):
            raise ValueError("unknown power event")
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise ValueError("power timestamps must be timezone-aware")


class PowerQueue:
    def __init__(self) -> None:
        self.lock = Lock()
        self._events: list[PowerEvent] = []

    def put(self, event: PowerEvent) -> None:
        with self.lock:
            self._events.append(event)

    def drain(self) -> list[PowerEvent]:
        with self.lock:
            events, self._events = self._events, []
        return sorted(events, key=lambda event: event.at.astimezone(UTC))


def _frameworks() -> tuple[Any, ...]:
    from AppKit import NSWorkspace, NSWorkspaceWillSleepNotification, NSWorkspaceDidWakeNotification
    from Foundation import NSDate, NSRunLoop, NSDefaultRunLoopMode

    return (NSWorkspace, NSWorkspaceWillSleepNotification,
            NSWorkspaceDidWakeNotification, NSDate, NSRunLoop, NSDefaultRunLoopMode)


def power_capability() -> dict[str, object]:
    """Probe API availability only; this does not subscribe or claim live coverage."""
    if sys.platform != "darwin":
        return {"available": False, "reason": "macOS is required"}
    try:
        _frameworks()
        return {"available": True, "reason": "NSWorkspace API available; see runtime monitoring state"}
    except Exception:
        return {"available": False, "reason": "NSWorkspace power API unavailable"}


class MacPowerMonitor:
    """Pump Cocoa on the main thread, retaining observer tokens until shutdown."""

    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self.clock = clock
        self.active = False
        self._center: Any = None
        self._tokens: list[Any] = []
        self._blocks: list[Callable[..., None]] = []
        self._api: tuple[Any, ...] | None = None

    def start(self, deliver: Callable[[PowerEvent], None]) -> bool:
        if self.active:
            return True
        try:
            if sys.platform != "darwin":
                return False
            self._api = _frameworks()
            workspace, sleep_name, wake_name, *_ = self._api
            self._center = workspace.sharedWorkspace().notificationCenter()
            def callback(kind: Literal["sleep", "wake"]) -> Callable[[object], None]:
                def receive(_notification: object) -> None:
                    # Deliberately ignore notification objects and userInfo.
                    deliver(PowerEvent(kind, self.clock()))
                return receive

            for kind, name in (("sleep", sleep_name), ("wake", wake_name)):
                receive = callback(kind)
                self._blocks.append(receive)
                token = self._center.addObserverForName_object_queue_usingBlock_(
                    name, None, None, receive,
                )
                if token is None:
                    raise RuntimeError("observer registration failed")
                self._tokens.append(token)
            self.active = True
        except Exception:
            self.close()
        return self.active

    def wait(self, seconds: float, stop: Event) -> None:
        if not self.active or self._api is None:
            stop.wait(seconds)
            return
        _, _, _, date, run_loop, mode = self._api
        try:
            handled = run_loop.currentRunLoop().runMode_beforeDate_(
                mode, date.dateWithTimeIntervalSinceNow_(seconds),
            )
            if not handled:
                # Cocoa returns immediately if this run loop has no sources.
                stop.wait(seconds)
        except Exception:
            self.close()
            stop.wait(seconds)

    def close(self) -> None:
        if self._center is not None:
            for token in self._tokens:
                try:
                    self._center.removeObserver_(token)
                except Exception:
                    pass
        self._tokens.clear()
        self._blocks.clear()
        self.active = False
