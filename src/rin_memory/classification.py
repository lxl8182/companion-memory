"""Content classification and the extraction window filter (B04).

Migrated from the host's memory/event_classification.py (design §17.15.5):
nine content classes, the metadata priority rules, and the window filter that
decides which messages may enter fact extraction.

This module is pure. Classification reads a message's metadata only; the
filter receives its gate inputs (legacy barrier cutoff, deleted source keys)
as arguments and performs no IO. Reading .fact_suppression.json /
.source_gate_state.json lives in
rin_memory.persistence.gates.SuppressionGate.

Rules kept from the host:

* nine classes; HOST_MEMORY_CLASSES (4) are extractable,
  PLUGIN_ONLY_CLASSES (5) never are, and 'unclassified' is extractable
  (ordinary chat and legacy rows) but is subject to the legacy window barrier;
* explicit metadata.companion_event.content_class wins, then
  metadata.mirror.kind, then metadata.source, then metadata.turn_type /
  metadata.proactive, else 'unclassified';
* a message whose source key was deleted never enters extraction;
* an unclassified message older than the legacy cutoff is skipped, while a
  message without a usable timestamp counts as current and is kept.

Metadata is read from either the direct message['metadata'] (in-memory /
row dicts) or message['data']['metadata'] (the persisted human/ai message
shape). The direct key wins when both are present, matching the host helper.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any

#: Every legal content class. Any other value normalizes to UNCLASSIFIED.
CONTENT_CLASSES = frozenset({
    "chat",
    "life_fact",
    "preference",
    "shared_experience",
    "game_stream",
    "game_requirement",
    "game_technique",
    "plugin_notice",
    "companion_output",
})

#: Only these classes may enter host fact extraction.
HOST_MEMORY_CLASSES = frozenset({
    "chat",
    "life_fact",
    "preference",
    "shared_experience",
})

#: Plugin-owned classes: kept in the plugin's own scope, never extracted.
PLUGIN_ONLY_CLASSES = frozenset({
    "game_stream",
    "game_requirement",
    "game_technique",
    "plugin_notice",
    "companion_output",
})

#: Fallback for ordinary chat and legacy rows; extractable under the barrier.
UNCLASSIFIED = "unclassified"
#: Ordinary conversation. A chat entry point labels an otherwise unlabelled
#: turn with this instead of leaving it 'unclassified' (service plan §5.1).
CHAT = "chat"

#: Host sources that mean "game stream" even without an explicit class.
GAME_MEMORY_SOURCES = frozenset({"sts2_companion", "sts2_autoplay"})
#: Mirror kinds that mean "game stream"; any other nonempty kind is output.
GAME_MIRROR_KINDS = frozenset({"sts2"})
#: Turn types that mark a plugin-initiated (proactive) notice.
PROACTIVE_TURN_TYPES = frozenset({"proactive_instruction", "proactive_reply"})


# -- metadata access --------------------------------------------------------


def metadata_of(message: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a message's metadata mapping from either supported shape.

    Accepts the in-memory / row shape (message['metadata']) and the persisted
    shape (message['data']['metadata']). A missing or non-mapping metadata
    value reads as {}; the direct key wins when both exist.
    """
    if not isinstance(message, Mapping):
        return {}
    direct = message.get("metadata")
    if isinstance(direct, Mapping):
        return direct
    data = message.get("data")
    if isinstance(data, Mapping):
        nested = data.get("metadata")
        if isinstance(nested, Mapping):
            return nested
    return {}


# -- classification ---------------------------------------------------------


def _normalize_class(value: Any) -> str:
    """Collapse any input to one of CONTENT_CLASSES or UNCLASSIFIED."""
    if isinstance(value, str) and value in CONTENT_CLASSES:
        return value
    return UNCLASSIFIED


def classify_message(
    message: Mapping[str, Any], *, default: str = UNCLASSIFIED,
) -> str:
    """Classify one message using the host priority rules.

    Priority: explicit companion_event.content_class (when legal), then
    mirror.kind ('sts2' is a game stream, any other nonempty kind is companion
    output), then source (game sources are streams, 'proactive' and 'plugin:'
    prefixes are notices), then proactive turn types / the proactive flag,
    else `default`. An illegal explicit class is ignored and the remaining
    rules still apply.

    `default` is what an ordinary chat entry point passes (`CHAT`): leaving a
    normal turn 'unclassified' made it subject to the legacy window barrier and
    left the only remaining gate ambiguous, which §5.1 of
    docs/standalone-service.md asks callers to label explicitly instead.
    """
    metadata = metadata_of(message)

    companion = metadata.get("companion_event")
    if isinstance(companion, Mapping):
        explicit = companion.get("content_class")
        if isinstance(explicit, str) and explicit in CONTENT_CLASSES:
            return explicit

    mirror = metadata.get("mirror")
    if isinstance(mirror, Mapping):
        kind = mirror.get("kind")
        if isinstance(kind, str) and kind:
            if kind in GAME_MIRROR_KINDS:
                return "game_stream"
            # Anything an external controller writes into the bubble is not
            # long-term material for the companion's own memory.
            return "companion_output"

    source = metadata.get("source")
    if isinstance(source, str):
        if source in GAME_MEMORY_SOURCES:
            return "game_stream"
        if source == "proactive" or source.startswith("plugin:"):
            return "plugin_notice"

    turn_type = metadata.get("turn_type")
    if (
        isinstance(turn_type, str) and turn_type in PROACTIVE_TURN_TYPES
    ) or metadata.get("proactive") is True:
        return "plugin_notice"

    return _normalize_class(default)


def is_extractable(content_class: Any) -> bool:
    """Whether a class may enter fact extraction.

    Plugin-only classes never do. Host classes and 'unclassified' do; an
    unknown value is treated as 'unclassified'.
    """
    return _normalize_class(content_class) not in PLUGIN_ONLY_CLASSES


# -- source keys ------------------------------------------------------------


def _add_source_key(keys: set[str], value: Any) -> None:
    if isinstance(value, str):
        key = value.strip()
        if key:
            keys.add(key)


def message_source_keys(message: Mapping[str, Any]) -> set[str]:
    """Source keys carried by one message.

    Reads metadata.companion_event.source_key and
    metadata.mirror.event.source_key; empty and non-string values are
    ignored. The filter uses this to honor the deletion barrier.
    """
    metadata = metadata_of(message)
    keys: set[str] = set()
    companion = metadata.get("companion_event")
    if isinstance(companion, Mapping):
        _add_source_key(keys, companion.get("source_key"))
    mirror = metadata.get("mirror")
    if isinstance(mirror, Mapping):
        event = mirror.get("event")
        if isinstance(event, Mapping):
            _add_source_key(keys, event.get("source_key"))
    return keys


# -- extraction window filter ----------------------------------------------


def _empty_meta() -> dict[str, Any]:
    return {
        "gate_mode": "off",
        "skipped_classes": {},
        "legacy_skipped": 0,
        "legacy_barrier": None,
        "plugin_id": "",
        "kept": 0,
        "deleted_skipped": 0,
    }


def _coerce_cutoff(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _coerce_timestamp(value: Any) -> float | None:
    """Epoch seconds from a numeric value, a datetime, or an ISO string."""
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


def _item_timestamp(item: Mapping[str, Any]) -> float | None:
    """A window item's time: 'timestamp' first, then 'created_at'."""
    for field in ("timestamp", "created_at"):
        if field in item:
            stamp = _coerce_timestamp(item.get(field))
            if stamp is not None:
                return stamp
    return None


def _normalize_deleted_keys(value: Collection[str] | None) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, (str, bytes)):
        value = (value,)  # type: ignore[assignment]
    keys: set[str] = set()
    for item in value:
        if isinstance(item, str):
            key = item.strip()
            if key:
                keys.add(key)
    return frozenset(keys)


def filter_messages_for_extraction(
    messages: Collection[Mapping[str, Any]],
    *,
    legacy_barrier_cutoff: float | None = None,
    deleted_source_keys: Collection[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter one extraction window, returning (kept, meta).

    Kept: host-memory classes and 'unclassified' rows that are not blocked by
    the legacy barrier or a deleted source key. Skipped:

    * plugin-only classes, counted per class in skipped_classes;
    * any class whose source key is in deleted_source_keys, counted in
      deleted_skipped;
    * 'unclassified' rows with a timestamp/created_at strictly older than
      legacy_barrier_cutoff, counted in legacy_skipped. A row without a
      usable timestamp is treated as current and kept.

    meta always carries the fixed keys gate_mode, skipped_classes,
    legacy_skipped, legacy_barrier, plugin_id, kept and deleted_skipped. This
    function has no gate state of its own, so it reports gate_mode='off' and
    plugin_id=''; the caller overrides them from a GateState.
    """
    items = list(messages or ())
    meta = _empty_meta()
    cutoff = _coerce_cutoff(legacy_barrier_cutoff)
    deleted = _normalize_deleted_keys(deleted_source_keys)
    meta["legacy_barrier"] = cutoff

    kept: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    legacy_skipped = 0
    deleted_skipped = 0
    for item in items:
        memory_class = classify_message(item)
        if memory_class in PLUGIN_ONLY_CLASSES:
            skipped[memory_class] = skipped.get(memory_class, 0) + 1
            continue
        if deleted and message_source_keys(item) & deleted:
            deleted_skipped += 1
            continue
        if memory_class == UNCLASSIFIED and cutoff is not None:
            stamp = _item_timestamp(item)
            if stamp is not None and stamp < cutoff:
                legacy_skipped += 1
                continue
        kept.append(item)

    meta["skipped_classes"] = skipped
    meta["legacy_skipped"] = legacy_skipped
    meta["deleted_skipped"] = deleted_skipped
    meta["kept"] = len(kept)
    return kept, meta


__all__ = [
    "CONTENT_CLASSES",
    "GAME_MEMORY_SOURCES",
    "GAME_MIRROR_KINDS",
    "HOST_MEMORY_CLASSES",
    "PLUGIN_ONLY_CLASSES",
    "PROACTIVE_TURN_TYPES",
    "UNCLASSIFIED",
    "classify_message",
    "filter_messages_for_extraction",
    "is_extractable",
    "message_source_keys",
    "metadata_of",
]
