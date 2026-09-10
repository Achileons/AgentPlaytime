"""Bounded service logs containing allowlisted operational fields only."""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

from agentplaytime.detectors.constants import SUPPORTED_TOOLS
from agentplaytime.tracker import TrackingEvent
from .files import directory_fd, _check_target

MAX_BYTES = 1024 * 1024
BACKUPS = 3


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):  # type: ignore[no-untyped-def]
        fd = os.open(self.baseFilename, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")


@contextmanager
def service_logger(path: Path, *, max_bytes: int = MAX_BYTES) -> Iterator[logging.Logger]:
    with directory_fd(path.parent, create=True) as folder:
        for suffix in ("", ".1", ".2", ".3"):
            _check_target(folder, path.name + suffix)
    handler = PrivateRotatingHandler(path, maxBytes=max_bytes, backupCount=BACKUPS, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logger = logging.Logger("agentplaytime.service", level=logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield logger
    finally:
        handler.close()
        logger.removeHandler(handler)


def log_transition(logger: logging.Logger, event: TrackingEvent) -> None:
    # Unknown names could be user-controlled data in an imported database.
    tool = event.tool if event.tool in SUPPORTED_TOOLS else "unrecognized-tool"
    if event.action == "start":
        logger.info("START %s at=%s", tool, event.started_at.isoformat())
    else:
        logger.info("STOP %s at=%s seconds=%d", tool, event.ended_at.isoformat(), int(event.duration_seconds or 0))
