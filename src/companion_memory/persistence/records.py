"""JSON-safe event snapshots shared by the journal and the source ledger.

A journal entry carries a *full snapshot* of the observed event so a replay
handler can rebuild its view from the entry alone. Snapshots are JSON scalars
only, keep ISO-8601 timestamps with an offset, and never contain host wire
objects.

Malformed snapshots raise ValueError; persistence callers wrap that into
CorruptStoreError with the file path, so this module stays path-agnostic.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from ..contracts import MemoryEvent, Scene, Scope

#: Fields that may change without changing the event's meaning (retries).
_VOLATILE_FIELDS = ("event_id", "received_at", "request_id")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    """Stable short digest used for idempotency and conflict detection."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:32]


def _scene_snapshot(scene: Scene) -> dict[str, str]:
    return {
        "plugin_id": scene.plugin_id,
        "game_id": scene.game_id,
        "session_id": scene.session_id,
    }


def _scene_from_snapshot(raw: Any) -> Scene:
    if raw is None:
        return Scene()
    if not isinstance(raw, Mapping):
        raise ValueError("scene snapshot must be an object")
    values = {}
    for key in ("plugin_id", "game_id", "session_id"):
        value = raw.get(key, "")
        if not isinstance(value, str):
            raise ValueError(f"scene field {key} must be a string")
        values[key] = value
    return Scene(**values)


def event_snapshot(event: MemoryEvent) -> dict[str, Any]:
    """Return the journal/ledger snapshot of `event` (JSON-safe, complete)."""
    return {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "source_key": event.source_key,
        "scope": {"character": event.scope.character, "subject": event.scope.subject},
        "event_type": event.event_type,
        "content_class": event.content_class,
        "text": event.text,
        "payload": dict(event.payload),
        "occurred_at": event.occurred_at.isoformat(),
        "received_at": None if event.received_at is None else event.received_at.isoformat(),
        "scene": _scene_snapshot(event.scene),
        "applies_to": _scene_snapshot(event.applies_to),
        "request_id": event.request_id,
        "episode_id": event.episode_id,
    }


def event_from_snapshot(snapshot: Mapping[str, Any]) -> MemoryEvent:
    """Rebuild a MemoryEvent from `event_snapshot` output."""
    if not isinstance(snapshot, Mapping):
        raise ValueError("event snapshot must be an object")
    scope_raw = snapshot.get("scope")
    if not isinstance(scope_raw, Mapping):
        raise ValueError("event snapshot is missing its scope")
    character = scope_raw.get("character")
    subject = scope_raw.get("subject", "private")
    if not isinstance(character, str) or not isinstance(subject, str):
        raise ValueError("event snapshot scope must hold strings")
    occurred_at = _parse_datetime(snapshot.get("occurred_at"), "occurred_at")
    received_raw = snapshot.get("received_at")
    payload = snapshot.get("payload") or {}
    if not isinstance(payload, Mapping):
        raise ValueError("event snapshot payload must be an object")
    return MemoryEvent(
        scope=Scope(character=character, subject=subject),
        source_key=_require_str(snapshot, "source_key"),
        event_type=_require_str(snapshot, "event_type"),
        text=_require_str(snapshot, "text"),
        occurred_at=occurred_at,
        scene=_scene_from_snapshot(snapshot.get("scene")),
        applies_to=_scene_from_snapshot(snapshot.get("applies_to")),
        schema_version=int(snapshot.get("schema_version") or 1),
        received_at=None if received_raw is None else _parse_datetime(received_raw, "received_at"),
        content_class=snapshot.get("content_class"),
        payload=dict(payload),
        request_id=snapshot.get("request_id"),
        event_id=snapshot.get("event_id"),
        episode_id=snapshot.get("episode_id"),
    )


def event_fingerprint(event: MemoryEvent) -> str:
    """Digest of the event's meaning, ignoring retry-only fields.

    A retried source key with the same meaning is a duplicate; a different
    digest under the same source key is a conflict.
    """
    snapshot = event_snapshot(event)
    for field_name in _VOLATILE_FIELDS:
        snapshot.pop(field_name, None)
    return fingerprint(snapshot)


def _require_str(snapshot: Mapping[str, Any], key: str) -> str:
    value = snapshot.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"event snapshot field {key} must be a nonempty string")
    return value


def _parse_datetime(raw: Any, field_name: str) -> datetime:
    if not isinstance(raw, str):
        raise ValueError(f"{field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not ISO-8601: {raw!r}") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed
