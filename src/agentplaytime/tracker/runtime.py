"""Shared process locking and signal handling for interactive and service use."""

from __future__ import annotations

import fcntl
import os
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Any


class TrackerAlreadyRunning(RuntimeError):
    """Another tracker owns this database's process lock."""


@contextmanager
def exclusive_tracker_lock(database_path: Path) -> Iterator[None]:
    # Resolve the data path, not the Python executable: alternate path spellings
    # must share a lock, whereas resolving venv Python loses the virtualenv.
    database_path = database_path.expanduser().resolve()
    lock_path = database_path.with_name(f"{database_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TrackerAlreadyRunning(
                f"another tracker is already using {database_path}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def install_shutdown_handlers(stop_event: Event) -> dict[signal.Signals, Any]:
    previous: dict[signal.Signals, Any] = {}

    def request_shutdown(_signum: int, _frame: object) -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)
    return previous


def restore_signal_handlers(previous: dict[signal.Signals, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)
