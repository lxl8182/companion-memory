"""Content-gate readers for the fact extraction window (B04).

Two host state files decide whether extraction runs and which legacy rows it
must skip. Both contain timestamps, plugin ids and class names only -- never
conversation text -- and both are read defensively: a missing, unparsable or
malformed file degrades to "no gate" instead of raising, because the safe
default is to keep extracting ordinary chat.

.fact_suppression.json, two generations:

* schema 1: {suppress, source, reason, pid, updated_at}. An explicit
  suppress=true refreshed within the 90 s TTL means the legacy plugin is
  alive: the whole batch is skipped (mode='legacy').
* schema 2: {schema: 2, plugin_id, suppress_classes, activated_at,
  updated_at}. Within the TTL this is the content gate (mode='content'):
  extraction runs, but plugin-only classes are filtered per message.
  suppress_classes is normalized to the known content classes; unknown names
  are dropped.

.source_gate_state.json, schema 1:
{schema: 1, characters: {<character>: {legacy_cutoff, plugin_id,
updated_at}}}. The host freezes the cutoff the first time it sees the v2
gate; the core only reads it here and never advances it (a plugin reconnect
must not reopen an old window). A missing entry means no barrier.

This reader never writes, creates or repairs a file. Timestamps accept epoch
seconds or ISO-8601 strings. Unlike the host, a gate row without a usable
updated_at is treated as stale instead of falling back to the file mtime:
shape errors read as "no gate", never as a guessed heartbeat.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from ..classification import CONTENT_CLASSES
from ..ports import Clock

GATE_FILENAME = ".fact_suppression.json"
SOURCE_GATE_STATE_FILENAME = ".source_gate_state.json"

#: Host content-gate envelope version.
CONTENT_GATE_SCHEMA = 2
#: Host legacy barrier state version.
SOURCE_GATE_STATE_SCHEMA = 1
#: The plugin refreshes every 20 s; 90 s tolerates a couple of missed beats.
GATE_TTL_SECONDS = 90.0

_GATE_MODES = frozenset({"off", "legacy", "content"})


@dataclass(frozen=True)
class GateState:
    """One character's extraction-gate state, as read from disk.

    mode 'off' means no gate (or a stale/malformed one). 'legacy' means the
    old whole-batch suppression is active. 'content' means the per-class gate
    is active. legacy_barrier is the frozen old-window cutoff, independent of
    the current mode: it stays in force after the content gate expires.
    """

    mode: str = "off"
    suppressed: bool = False
    suppress_classes: frozenset[str] = frozenset()
    legacy_barrier: float | None = None
    plugin_id: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in _GATE_MODES:
            raise ValueError(f"invalid gate mode {self.mode!r}")


class SuppressionGate:
    """Read-only view over the two gate state files under memory_root.

    memory_root is the directory that holds the files (the host's memory_dir;
    in the core demo it is the per-character directory). gate_for() selects
    the character's barrier entry and performs no writes at all, so a poll
    can never create, touch or repair state.
    """

    def __init__(self, *, memory_root: Path, clock: Clock) -> None:
        self._memory_root = Path(memory_root)
        self._clock = clock

    @property
    def gate_path(self) -> Path:
        """Path of .fact_suppression.json (read only)."""
        return self._memory_root / GATE_FILENAME

    @property
    def source_gate_state_path(self) -> Path:
        """Path of .source_gate_state.json (read only)."""
        return self._memory_root / SOURCE_GATE_STATE_FILENAME

    def gate_for(self, character: str) -> GateState:
        """Return the gate state for one character; never raises on bad IO."""
        now = self._now()
        payload = _read_json(self.gate_path)

        mode = "off"
        suppressed = False
        suppress_classes: frozenset[str] = frozenset()
        plugin_id: str | None = None

        if isinstance(payload, Mapping):
            # Legacy suppression wins when both generations are present, the
            # same order the host applies (is_suppressed before the v2 gate).
            if payload.get("suppress") is True:
                stamp = _coerce_epoch(payload.get("updated_at"))
                if stamp is not None and _is_fresh(stamp, now):
                    mode = "legacy"
                    suppressed = True
            if mode == "off" and payload.get("schema") == CONTENT_GATE_SCHEMA:
                stamp = _coerce_epoch(payload.get("updated_at"))
                if stamp is not None and _is_fresh(stamp, now):
                    mode = "content"
                    suppress_classes = _normalize_classes(
                        payload.get("suppress_classes"),
                    )
                    plugin_id = _clean_plugin_id(payload.get("plugin_id"))

        barrier, state_plugin_id = self._read_barrier(character)
        if plugin_id is None:
            plugin_id = state_plugin_id
        return GateState(
            mode=mode,
            suppressed=suppressed,
            suppress_classes=suppress_classes,
            legacy_barrier=barrier,
            plugin_id=plugin_id,
        )

    # -- internals ------------------------------------------------------

    def _now(self) -> float:
        return self._clock.now().timestamp()

    def _read_barrier(self, character: str) -> tuple[float | None, str | None]:
        """The frozen old-window cutoff and its plugin id, per character."""
        if not isinstance(character, str) or not character.strip():
            return None, None
        payload = _read_json(self.source_gate_state_path)
        if not isinstance(payload, Mapping):
            return None, None
        characters = payload.get("characters")
        if not isinstance(characters, Mapping):
            return None, None
        entry = characters.get(character.strip())
        if not isinstance(entry, Mapping):
            return None, None
        barrier = _coerce_epoch(entry.get("legacy_cutoff"))
        plugin_id = _clean_plugin_id(entry.get("plugin_id"))
        return barrier, plugin_id


# -- file and value helpers -------------------------------------------------


def _read_json(path: Path) -> Any | None:
    """Parse a JSON file; None for missing, unreadable or invalid content."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(text)
    except ValueError:  # JSONDecodeError and UnicodeDecodeError
        return None


def _coerce_epoch(value: Any) -> float | None:
    """Epoch seconds from a number or an ISO-8601 string; None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def _is_fresh(stamp: float, now: float) -> bool:
    """Whether a heartbeat is inside the TTL (a future stamp counts as fresh)."""
    return (now - stamp) <= GATE_TTL_SECONDS


def _normalize_classes(value: Any) -> frozenset[str]:
    """Keep known content class names only; duplicates collapse."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    classes: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name in CONTENT_CLASSES:
            classes.add(name)
    return frozenset(classes)


def _clean_plugin_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:64]


__all__ = [
    "CONTENT_GATE_SCHEMA",
    "GATE_FILENAME",
    "GATE_TTL_SECONDS",
    "SOURCE_GATE_STATE_FILENAME",
    "SOURCE_GATE_STATE_SCHEMA",
    "GateState",
    "SuppressionGate",
]
