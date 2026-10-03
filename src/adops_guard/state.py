"""The journal, the state file and the single-writer lock.

* ``journal.jsonl``: append-only, one JSON object per line, one line per step
  of every applied write (intent, applied or error, verified or mismatch).
* ``state.json``: small idempotency records, e.g. "this spend watch already
  fired". Written atomically (temp file, fsync, rename).
* ``lock``: an exclusive, non-blocking ``flock`` held while a write runs, so
  two commands can never write at the same time.

The directory is created 0700 and every file 0600. An existing directory is
used only if it is a real directory (not a symlink), owned by you and not
writable by other users; the journal and the lock are opened without
following symlinks and refused if they have extra hard links. Everything
written goes through :func:`adops_guard.redact.redact_data` first.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

from adops_guard.errors import AdopsError, LockBusy
from adops_guard.redact import redact, redact_data

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

# Never follow a symlink when opening the journal or the lock (0 where the flag does not exist).
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _json_default(value: Any) -> Any:
    # json.dumps calls this *after* redact_data has run, so every string made here is redacted too.
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(redact_data(list(value)), key=str)
    if isinstance(value, Path):
        return redact(str(value))
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return "***" if type(value).__name__ == "Secret" else redact(repr(value))


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (0700) if needed, or check that an existing one is private to you.

    An existing state directory must be a real directory (not a symlink), owned
    by you, and not writable by other users: otherwise someone else could plant
    a symlink named ``lock`` or ``journal.jsonl`` in it and make a write land in
    one of your files.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700)
        if os.name != "nt":
            os.chmod(path, 0o700)
        return path
    if stat.S_ISLNK(info.st_mode):
        raise AdopsError(
            f"state directory {path} is a symlink", hint="point --state-dir (or state_dir) at the real directory"
        )
    if not stat.S_ISDIR(info.st_mode):
        raise AdopsError(f"state directory {path} exists but is not a directory")
    if os.name != "nt":
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise AdopsError(
                f"state directory {path} belongs to another user", hint="use a state directory that you own"
            )
        if info.st_mode & 0o022:
            raise AdopsError(
                f"state directory {path} can be changed by other users (mode {oct(info.st_mode & 0o777)})",
                hint=f"run: chmod 700 {path}",
            )
    return path


def _open_private(path: Path, flags: int) -> int:
    """Open (or create, 0600) a state file without following symlinks; refuse extra hard links."""
    try:
        fd = os.open(path, flags | os.O_CREAT | O_NOFOLLOW, 0o600)
    except OSError as exc:
        if path.is_symlink():
            raise AdopsError(f"{path} is a symlink; refusing to write through it") from None
        raise AdopsError(f"cannot open {path}: {exc.strerror or exc}") from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
        os.close(fd)
        raise AdopsError(f"{path} is not a plain private file (it is linked elsewhere); refusing to write to it")
    return fd


class Journal:
    """Append-only JSON lines. Each record gets a UTC timestamp and the run id."""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id

    def record(self, **fields: Any) -> dict[str, Any]:
        entry = {"ts": utc_now_iso(), "run": self.run_id, **fields}
        line = json.dumps(redact_data(entry), ensure_ascii=False, default=_json_default)
        fd = _open_private(self.path, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return entry

    def entries(self) -> list[dict[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in text.splitlines() if line.strip()]


class StateStore:
    """A small JSON document, replaced atomically on every save."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise AdopsError(
                f"state file {self.path} is not valid JSON ({exc})",
                hint="move it aside and re-run; the journal keeps the full history",
            ) from None
        if not isinstance(data, dict):
            raise AdopsError(f"state file {self.path} does not hold a JSON object")
        return data

    def save(self, data: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    redact_data(data), handle, indent=2, ensure_ascii=False, sort_keys=True, default=_json_default
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def update(self, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        data = self.load()
        change(data)
        self.save(data)
        return data


def _try_lock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:  # pragma: no cover - Windows
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def _unlock(fd: int) -> None:
    with contextlib.suppress(OSError):
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:  # pragma: no cover - Windows
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class WriteLock:
    """Exclusive, non-blocking, re-entrant within one process."""

    _depth: ClassVar[dict[str, int]] = {}
    _fds: ClassVar[dict[str, int]] = {}

    def __init__(self, path: Path, what: str) -> None:
        self.path = path
        self.what = what

    def __enter__(self) -> WriteLock:
        key = str(self.path.resolve())
        if WriteLock._depth.get(key):
            WriteLock._depth[key] += 1
            return self
        fd = _open_private(self.path, os.O_RDWR)
        try:
            _try_lock(fd)
        except OSError:
            try:
                holder = os.read(fd, 300).decode("utf-8", errors="replace").strip()
            finally:
                os.close(fd)
            raise LockBusy(
                f"another adops-guard write is running ({holder or 'the lock is held'})",
                hint=f"wait for it to finish, then re-run. Lock file: {self.path}",
            ) from None
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"pid {os.getpid()}: {self.what} (since {utc_now_iso()})\n".encode())
        WriteLock._depth[key] = 1
        WriteLock._fds[key] = fd
        return self

    def __exit__(self, *exc: object) -> None:
        key = str(self.path.resolve())
        WriteLock._depth[key] -= 1
        if WriteLock._depth[key] == 0:
            del WriteLock._depth[key]
            fd = WriteLock._fds.pop(key)
            _unlock(fd)
            os.close(fd)
