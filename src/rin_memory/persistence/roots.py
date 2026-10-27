"""Resolved storage roots with path-containment validation.

The core never guesses Documents, global config or the current character: the
host resolves `memory_dir` / `local_state_dir` and injects them (design
§17.2). Everything the core opens goes through `character_dir`, which rejects
names that could escape the memory root.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

#: Characters are host-supplied names; reject anything that is not one path segment.
_INVALID_NAME_CHARS = frozenset('<>:"/\\|?*') | frozenset(chr(code) for code in range(32))
_RESERVED_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{index}" for index in range(1, 10)]
    + [f"LPT{index}" for index in range(1, 10)]
)
_MAX_NAME_LENGTH = 128


def validate_character_name(character: str) -> str:
    """Return the name if it is exactly one safe path segment, else raise ValueError."""
    if not isinstance(character, str):
        raise ValueError("character name must be a string")
    if not character.strip():
        raise ValueError("character name must not be empty")
    if character != character.strip():
        raise ValueError("character name must not have leading or trailing whitespace")
    if character in {".", ".."}:
        raise ValueError(f"invalid character name {character!r}")
    if len(character) > _MAX_NAME_LENGTH:
        raise ValueError(f"character name longer than {_MAX_NAME_LENGTH} characters")
    bad = sorted({char for char in character if char in _INVALID_NAME_CHARS})
    if bad:
        raise ValueError(f"character name contains invalid characters {bad}")
    if character.split(".")[0].upper() in _RESERVED_DEVICE_NAMES:
        raise ValueError(f"character name {character!r} is a reserved device name")
    return character


def _is_within(path: Path, root: Path) -> bool:
    """Resolve-based containment check.

    Kept as the one-time defensive check per character name: it is the only
    place that follows junctions/symlinks. `root` is already resolved when it
    reaches here, so only the candidate path costs a system call.
    """
    try:
        resolved = os.path.normcase(str(path.resolve()))
        root_resolved = os.path.normcase(str(root))
    except OSError:
        return False
    return resolved == root_resolved or resolved.startswith(root_resolved + os.sep)


@dataclass(frozen=True)
class FileSystemRoots:
    """StorageRoots port on a local filesystem.

    `memory_root` holds one directory per character. `local_state_root`
    defaults to `<memory_root>/../local_state` for tests and the demo; the host
    adapter (B08) injects the real `local_state_dir`.
    """

    _memory_root: Path
    _local_state_root: Path
    #: Character name -> directory already built and proven contained. Excluded
    #: from equality/repr so caching never changes how a roots value compares.
    _dirs: Mapping[str, Path] = field(
        default_factory=dict, compare=False, repr=False,
    )

    def __init__(self, memory_root: Path | str, local_state_root: Path | str | None = None) -> None:
        memory_root = Path(memory_root).expanduser()
        if not memory_root.is_absolute():
            memory_root = memory_root.resolve()
        state_root = (
            memory_root.parent / "local_state"
            if local_state_root is None
            else Path(local_state_root).expanduser()
        )
        if not state_root.is_absolute():
            state_root = state_root.resolve()
        object.__setattr__(self, "_memory_root", memory_root.resolve())
        object.__setattr__(self, "_local_state_root", state_root.resolve())
        object.__setattr__(self, "_dirs", {})
        object.__setattr__(self, "_dirs_guard", threading.Lock())

    @property
    def memory_root(self) -> Path:
        """Root that holds one directory per character."""
        return self._memory_root

    @property
    def local_state_root(self) -> Path:
        """Root for state that must not travel with a cloud snapshot."""
        return self._local_state_root

    def character_dir(self, character: str, *, create: bool = True) -> Path:
        name = validate_character_name(character)
        directory = self._dirs.get(name)
        if directory is None:
            directory = self._resolve_dir(name)
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _resolve_dir(self, name: str) -> Path:
        """Build and prove one character directory, then cache it.

        `validate_character_name` guarantees a single plain segment, so the join
        cannot leave `_memory_root` lexically. The resolve-based containment
        check (the only place that follows a junction/symlink) still runs, but
        once per name: the barrier alone asks for this path ~2k times per
        projection read, and each `resolve()` is a `_getfinalpathname` syscall
        on NTFS.
        """
        directory = self._memory_root / name
        if directory.parent != self._memory_root:
            raise ValueError(f"character directory escapes the memory root: {name!r}")
        if not _is_within(directory, self._memory_root):
            raise ValueError(f"character directory escapes the memory root: {name!r}")
        with self._dirs_guard:
            return self._dirs.setdefault(name, directory)

    def character_file(self, character: str, *parts: str) -> Path:
        """Return `<character_dir>/<parts...>` after validating every segment."""
        if not parts:
            raise ValueError("character_file requires at least one path part")
        for part in parts:
            if not isinstance(part, str) or not part.strip():
                raise ValueError("path parts must be nonempty strings")
            if part in {".", ".."} or "/" in part or "\\" in part:
                raise ValueError(f"invalid path part {part!r}")
        return self.character_dir(character, create=True).joinpath(*parts)
