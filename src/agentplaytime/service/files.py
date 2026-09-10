"""Bounded reads and atomic writes of AgentPlaytime's own configuration files."""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def directory_fd(directory: Path, *, create: bool = False) -> Iterator[int]:
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("service directory must be user-owned and not writable by others")
        yield fd
    finally:
        os.close(fd)


def _check_target(fd: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("service file must be a regular, user-owned file (no symlinks)")
    if info.st_mode & 0o022:
        raise ValueError("service file must not be writable by others")


def read_owned(path: Path, *, limit: int = 65536) -> bytes:
    with directory_fd(path.parent) as folder:
        _check_target(folder, path.name)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=folder)
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("service file exceeds size limit")
        return data


def atomic_write(path: Path, data: bytes) -> None:
    with directory_fd(path.parent, create=True) as folder:
        _check_target(folder, path.name)
        temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
        fd = os.open(
            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600, dir_fd=folder,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _check_target(folder, path.name)
            os.replace(temporary, path.name, src_dir_fd=folder, dst_dir_fd=folder)
        finally:
            try:
                os.unlink(temporary, dir_fd=folder)
            except FileNotFoundError:
                pass


def remove_owned(path: Path) -> None:
    with directory_fd(path.parent) as folder:
        _check_target(folder, path.name)
        os.unlink(path.name, dir_fd=folder)
