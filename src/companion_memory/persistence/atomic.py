"""Crash-safe file primitives (core replacement for host `utils.file_utils`).

Write order is always: same-directory temp file -> write -> flush -> fsync ->
`os.replace` -> fsync the directory. A reader therefore sees either the old
file or the new one, never a truncated mix. Reads tolerate a concurrent
replace (bounded retry) and never "repair" content: unparsable data raises
CorruptStoreError and leaves the file exactly as found.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from ..errors import CorruptStoreError

#: A concurrent os.replace on Windows can briefly make the path unreadable.
_IO_RETRIES = 5
_RETRY_SLEEP_SECONDS = 0.01


class FileSystemAtomicStore:
    """AtomicJsonStore port on a local filesystem."""

    def load(self, path: Path) -> Any | None:
        """Parse JSON; None when the file does not exist.

        Raises CorruptStoreError for unparsable content, including a file that
        another writer is replacing right now (after bounded retries).
        """
        text = self._read_text(Path(path))
        if text is None:
            return None
        if not text.strip():
            raise CorruptStoreError("empty file is not valid JSON", path=path)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise CorruptStoreError(
                f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}",
                path=path, cause=exc,
            ) from exc

    def save(self, path: Path, value: Any) -> None:
        """Atomically replace a JSON document; durable when this returns."""
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"value is not JSON serialisable: {exc}") from exc
        self.save_text(path, text)

    def save_text(self, path: Path, text: str) -> None:
        """Atomically replace a whole text file (journal compaction, ndjson rewrite)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            self._replace_with_retry(tmp, path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        self._fsync_dir(path.parent)

    def append_line(self, path: Path, line: str) -> None:
        """Append one newline-terminated line, flushed and fsynced.

        An fsync failure must propagate: callers treat "append returned" as the
        durability point that lets them advance a view.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # -- internals ------------------------------------------------------

    def _read_text(self, path: Path) -> str | None:
        last_error: OSError | None = None
        for attempt in range(_IO_RETRIES):
            try:
                return path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            except OSError as exc:  # sharing violation while another writer replaces
                last_error = exc
                time.sleep(_RETRY_SLEEP_SECONDS * (attempt + 1))
        raise CorruptStoreError(
            f"unreadable after {_IO_RETRIES} attempts: {last_error}",
            path=path, cause=last_error,
        )

    @staticmethod
    def _replace_with_retry(tmp: Path, path: Path) -> None:
        last_error: OSError | None = None
        for attempt in range(_IO_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:  # Windows: target momentarily open
                last_error = exc
                time.sleep(_RETRY_SLEEP_SECONDS * (attempt + 1))
        raise CorruptStoreError(
            f"could not replace target after {_IO_RETRIES} attempts: {last_error}",
            path=path, cause=last_error,
        )

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Persist the rename itself where the platform supports it."""
        if os.name == "nt":
            return
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
