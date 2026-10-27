"""Root-scoped ownership lock shared by every writable entry point (S01).

`docs/standalone-service.md` §2.2: the standalone service, the embedded host,
CLI maintenance, migration/rollback and an offline writable console all take
ONE lock on the canonical data root before assembling a writable core. The
per-character lock and the injected write permits still govern transactions
*inside* one process; this lock decides which process may write at all.

Rules taken from the service plan, and why they are implemented this way:

* the root is identified by its canonical path, so a different drive-letter
  case, a ``.``/``..`` segment or a directory alias cannot create a second,
  unrelated lock for one directory;
* mutual exclusion is an OS file lock held while the owner needs the root. A
  pid is recorded for diagnostics only and is never used to decide whether a
  lock is stale: a process that dies releases its OS lock automatically, so
  there is no "abandoned lock file" to infer;
* because of that, the lock file is never unlinked by an entrant. The previous
  behaviour -- treat an unreadable lock as stale and delete it -- had a real
  race: an owner created the file with ``O_CREAT|O_EXCL`` and wrote its payload
  afterwards, so a second process reading the still-empty file concluded
  "stale" and removed a lock that was live;
* the descriptive payload is written only after the lock is held, so two
  contenders can never leave one process's pid describing another's lock.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..errors import RootLockConflict

#: One lock per data root, in the root itself (never inside a character dir).
ROOT_LOCK_FILENAME = ".companion_memory.root.lock"
#: The descriptive owner record lives in bytes [0, _HEADER_BYTES). The OS lock
#: is a single byte *after* it, so a diagnostic read by another process is not
#: blocked by the lock itself (Windows byte-range locks deny reads of the
#: locked range to every other handle, and a payload that cannot be read while
#: held is useless in a conflict message).
_HEADER_BYTES = 4096
_LOCK_OFFSET = _HEADER_BYTES
#: Modes recorded in the lock payload; diagnostics only, never authority.
LOCK_MODES = frozenset({"standalone", "host", "cli", "migration", "console"})


def canonical_root(path: Path | str) -> Path:
    """Resolve one data root to the single path that identifies it.

    ``resolve`` collapses ``.``/``..``, drive-letter case and junctions or
    symlinks, which is what makes a directory alias and its real path share one
    lock. The path need not exist yet (``strict=False``).
    """
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("data root must not be empty")
    try:
        return Path(raw).expanduser().resolve()
    except OSError as exc:  # pragma: no cover - unreadable parent/loop
        raise ValueError(f"data root cannot be resolved: {exc}") from exc


def root_lock_path(root: Path | str) -> Path:
    """Return `<canonical root>/.companion_memory.root.lock`."""
    return canonical_root(root) / ROOT_LOCK_FILENAME


# -- OS locking --------------------------------------------------------------


def lock_descriptor(fd: int, *, offset: int = 0) -> None:
    """Take a non-blocking exclusive OS lock on an open descriptor.

    Raises `OSError` when another owner (in this process or another) holds it.
    Shared with the CLI's per-character guard so both locks use one mechanism.
    `offset` selects the locked byte; callers that keep readable data in the
    same file lock a byte beyond it.
    """
    try:
        import fcntl
    except ImportError:  # Windows
        fcntl = None  # type: ignore[assignment]
    if fcntl is not None:  # pragma: no cover - POSIX branch
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    import msvcrt  # pragma: no cover - Windows branch

    os.lseek(fd, offset, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def unlock_descriptor(fd: int, *, offset: int = 0) -> None:
    """Release a lock taken by `lock_descriptor`; errors are not worth raising."""
    try:
        import fcntl
    except ImportError:  # Windows
        fcntl = None  # type: ignore[assignment]
    if fcntl is not None:  # pragma: no cover - POSIX branch
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return
    import msvcrt  # pragma: no cover - Windows branch

    try:
        os.lseek(fd, offset, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _lock_file(fd: int, path: Path, mode: str) -> None:
    """Take the root lock on `fd` or raise RootLockConflict."""
    try:
        lock_descriptor(fd, offset=_LOCK_OFFSET)
    except OSError as exc:
        raise RootLockConflict(
            f"another writer owns the data root {str(path.parent)!r}",
            path=path, cause=exc,
            detail={
                "lock_path": str(path),
                "root": str(path.parent),
                "owner": read_root_lock(path.parent),
            },
        ) from exc


def _unlock_file(fd: int) -> None:
    unlock_descriptor(fd, offset=_LOCK_OFFSET)


#: One held lock per canonical root inside this process, so a second component
#: (CLI + console in one test) fails fast instead of locking against itself.
_HELD: dict[str, str] = {}
_HELD_GUARD = threading.Lock()


def _held_key(root: Path) -> str:
    return os.path.normcase(str(root))


@dataclass
class RootLock:
    """A held root lock; release it (or use the context manager) when done.

    `release` is idempotent and never removes the lock file: another process
    may already be waiting on the same inode, and unlinking it would hand a
    fresh lock file to the next contender while the old descriptor lives on.
    """

    root: Path
    path: Path
    mode: str
    _fd: int | None = field(default=None, repr=False)
    _released: bool = field(default=False, repr=False)
    _key: str = field(default="", repr=False)

    # -- diagnostics ----------------------------------------------------

    def owner_info(self) -> dict[str, Any]:
        """The descriptive payload this process wrote; empty when unreadable."""
        payload = read_root_lock(self.root)
        return dict(payload) if payload else {}

    # -- lifecycle ------------------------------------------------------

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            _unlock_file(fd)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            with _HELD_GUARD:
                if _HELD.get(self._key) == self.mode:
                    _HELD.pop(self._key, None)

    def __enter__(self) -> "RootLock":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def held_root_owner(root: Path | str) -> str | None:
    """The mode this process records as the owner of `root`, if any."""
    with _HELD_GUARD:
        return _HELD.get(_held_key(canonical_root(root)))


def acquire_root_lock(
    root: Path | str, *, mode: str = "standalone", create: bool = True,
) -> RootLock:
    """Take the one writable-owner lock on `root`; raise RootLockConflict.

    Callers take this *before* assembling a writable core, so a second writable
    entry point is refused before it can touch any file.
    """
    if mode not in LOCK_MODES:
        raise ValueError(f"unknown lock mode {mode!r}; expected {sorted(LOCK_MODES)}")
    resolved = canonical_root(root)
    if not resolved.exists():
        if not create:
            raise RootLockConflict(
                f"data root does not exist: {str(resolved)!r}",
                path=resolved, detail={"root": str(resolved)},
            )
        resolved.mkdir(parents=True, exist_ok=True)
    if not resolved.is_dir():
        raise ValueError(f"data root is not a directory: {str(resolved)!r}")
    path = resolved / ROOT_LOCK_FILENAME
    key = _held_key(resolved)
    with _HELD_GUARD:
        if key in _HELD:
            raise RootLockConflict(
                f"this process already owns the data root {str(resolved)!r}",
                path=path,
                detail={"root": str(resolved), "mode": _HELD[key]},
            )
        _HELD[key] = mode
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        with _HELD_GUARD:
            _HELD.pop(key, None)
        raise RootLockConflict(
            f"cannot open the root lock {str(path)!r}: {exc}",
            path=path, cause=exc, detail={"root": str(resolved)},
        ) from exc
    try:
        _lock_file(fd, path, mode)
    except RootLockConflict:
        os.close(fd)
        with _HELD_GUARD:
            _HELD.pop(key, None)
        raise
    lock = RootLock(root=resolved, path=path, mode=mode, _fd=fd, _key=key)
    _write_payload(fd, resolved, mode)
    return lock


def _write_payload(fd: int, root: Path, mode: str) -> None:
    """Describe the owner *after* the lock is held (diagnostics only).

    The record goes in the header bytes *below* the locked byte, so another
    process can read it while this owner holds the lock. A failure here must
    not release a lock the caller believes it owns, so it is swallowed: the
    payload is never consulted for mutual exclusion.
    """
    payload = json.dumps({
        "pid": os.getpid(),
        "mode": mode,
        "root": str(root),
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False).encode("utf-8")
    if len(payload) > _HEADER_BYTES - 1:
        return
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        os.ftruncate(fd, len(payload))
        os.fsync(fd)
    except OSError:
        pass


def read_root_lock(root: Path | str) -> dict[str, Any] | None:
    """Best-effort read of the owner payload for status/error messages.

    Safe to call while the lock is held: the record is in the header bytes and
    the lock covers only the byte after it. Never used to decide ownership: a
    readable payload may be stale and an unreadable one may belong to a live
    owner.
    """
    path = root_lock_path(root)
    try:
        raw = path.read_bytes()
    except (OSError, ValueError):
        return None
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace").strip() or "null")
    except ValueError:
        # A record with trailing bytes after valid JSON still parses below.
        head = raw.split(b"\x00", 1)[0]
        try:
            payload = json.loads(head.decode("utf-8", errors="replace"))
        except (UnicodeDecodeError, ValueError):
            return None
    if not isinstance(payload, Mapping):
        return None
    return dict(payload)
