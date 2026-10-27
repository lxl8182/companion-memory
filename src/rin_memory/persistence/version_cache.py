"""Amortised document versions for the deletion/correction stores.

The deletion barrier asks both stores for their document version on *every*
predicate: one `persona_projection_service.status()` read triggers ~950
`version()` calls. Re-parsing and re-normalising the whole document per call
cost 1.12 s per five status reads over a 28 KB `memory_deletions.json`.

This module changes *how often* the document is read, not what a read means:

* the file is stat'ed; the parsed result is reused while the file signature
  `(st_dev, st_ino, st_mtime_ns, st_size)` is unchanged. `os.replace` (the only
  write path in `atomic.py`) always changes the inode or the mtime, so a
  changed file is never served from the cache;
* a cache miss runs the store's own strict parser, the exact same
  `_normalize_doc` used by `load_view`. Error behaviour therefore stays
  identical to the uncached path: unparsable JSON raises
  `CorruptStoreError`, an unknown schema raises `InvalidSchemaError`, and a
  single dirty tombstone/entry also raises instead of being dropped. Nothing is
  cached when the parse fails, so a damaged store keeps raising on every call
  and the barrier can never reopen on it.

The cache is keyed by absolute path and guarded by a lock, so two store
instances or a second character never share an answer, and callers on other
threads see a consistent value.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..ports import AtomicJsonStore

#: `(st_dev, st_ino, st_mtime_ns, st_size)`; `None` when the file is missing.
Signature = tuple[int, int, int, int] | None

#: Sentinel for "could not stat for a reason other than a missing file".
_UNKNOWN_SIGNATURE: tuple[int, int, int, int] = (-1, -1, -1, -1)

#: A filesystem records a modification time at clock-tick granularity (NTFS
#: ~15.6 ms, FAT/exFAT 2 s), so two writes inside one tick can leave *identical*
#: signatures when they also keep the same inode and size. Serving a cached
#: version then would hide a bump and the deletion barrier would not refresh.
#: The cache is therefore only trusted once the file has been quiet for longer
#: than any plausible tick: a later write then necessarily bumps the mtime, so a
#: stale signature cannot survive. The stores' own writes go through
#: `os.replace` and always change the inode, so this only guards a foreign
#: writer (migration tooling, a legacy writer during a switch); those events are
#: rare, so the uncached window right after one never reaches the steady state.
_STABLE_AFTER_NS = 2_000_000_000


def file_signature(path: Path) -> Signature:
    """Cheap identity of the file on disk; `None` when it does not exist.

    Callers for which size is monotonic per write (the append-only journal) may
    use this alone: a larger file can never collide with an older signature.
    """
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError:
        # Unreadable (permissions, transient sharing violation): never trust a
        # cached value; let the store's own load raise the real error.
        return _UNKNOWN_SIGNATURE
    return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)


def settled_signature(path: Path) -> tuple[Signature, bool]:
    """Signature plus whether it is old enough to be trusted as a cache key."""
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        # No file: nothing can be hidden behind a missing one, and creating it
        # changes the signature anyway.
        return None, True
    except OSError:
        return _UNKNOWN_SIGNATURE, False
    signature = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
    quiet = time.time_ns() - stat.st_mtime_ns > _STABLE_AFTER_NS
    return signature, quiet


class VersionReader:
    """Per-store cached document version, parsed by the store's strict parser."""

    def __init__(
        self,
        store: AtomicJsonStore,
        parse: Callable[[Any, Path], int],
    ) -> None:
        self._store = store
        self._parse = parse
        self._cache: dict[str, tuple[Signature, int]] = {}
        self._guard = threading.Lock()

    def version(self, path: Path) -> int:
        """Current document version; 0 for a missing file."""
        key = str(path)
        signature, settled = settled_signature(path)
        with self._guard:
            cached = self._cache.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        raw = self._store.load(path)
        version = 0 if raw is None else self._parse(raw, path)
        if not settled:
            # The file changed too recently for its signature to distinguish two
            # writes (see _STABLE_AFTER_NS); caching now could hide a bump.
            return version
        # Only cache when the file did not change while it was being read: a
        # concurrent writer would otherwise leave (old signature, new version)
        # in the cache. The extra stat costs nothing next to the parse it guards.
        if file_signature(path) != signature:
            return version
        with self._guard:
            self._cache[key] = (signature, version)
        return version

    def invalidate(self, path: Path | None = None) -> None:
        """Drop cached versions (all, or one file); not needed for correctness."""
        with self._guard:
            if path is None:
                self._cache.clear()
            else:
                self._cache.pop(str(path), None)


__all__ = ["VersionReader", "file_signature", "settled_signature"]
