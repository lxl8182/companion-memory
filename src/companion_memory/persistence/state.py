"""Host-compatible companion_state.json storage (B07).

The host keeps one short-term state document per character, split by subject
(private or a scoped subject key). This adapter owns the on-disk shape, the
pure normalization/apply/render rules and the journal replay handler; the
commit/lease/version/idempotency *orchestration* lives in
\`companion_memory.domain.state\`.

On-disk contract (host \`memory/companion_state.py:247-365\`)
-------------------------------------------------------------
* Path \`<character_dir>/companion_state.json\`.
* Top level \`{"schema": 1, "character": str, "subjects": {<subject>: row}}\`.
* Subject row:
  \`{"schema": 1, "version": int, "updated_at": ISO|null,
    "state": {"thoughts": [], "moods": [], "concerns": [], "progress": []},
    "processed_events": [], "requests": {}, "lease": null}\`
* Item fields mirror the host: \`id/source_event_ids/scene/created_at/
  updated_at/expires_at\`, plus \`text\` or mood \`label/intensity/cause\`;
  concern/progress may carry \`why/next_step\`.
* Reads normalize unknown fields away (host behavior) and default missing
  values; writes emit the same shape.

Strict reads / writes
---------------------
A missing file is the empty document. Invalid JSON, a non-object root, an
unsupported \`schema\`, a non-object subject row, a non-list state/processed
field or a non-object request row raises \`CorruptStoreError\` /
\`InvalidSchemaError\` and the bytes are left exactly as found. \`save()\` also
refuses to replace a file its own reader rejects, so a damaged state file must
be inspected instead of silently overwritten (B02 fail-closed rule).

Every write runs under one process-wide per-file RLock and follows
\`WritePermit.assert_writable -> write_guard -> AtomicJsonStore.save\`.
\`write_guard(character, document)\` is optional and lets the composition root
apply a final document-level fence (state itself does not participate in
per-item deletion; the domain service does a defensive item check).

All methods are synchronous blocking I/O; call them through
\`asyncio.to_thread\` from async code, like the other stores in this package.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..errors import CorruptStoreError, InvalidSchemaError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit

#: File name inside <character_dir>; the host uses this exact name.
STATE_FILE = "companion_state.json"
STATE_FILENAME = STATE_FILE

SCHEMA_VERSION = 1
DEFAULT_SUBJECT = "private"

STATE_KINDS: tuple[str, ...] = ("thoughts", "moods", "concerns", "progress")
ITEM_TYPE_BY_KIND: dict[str, str] = {
    "thoughts": "thought",
    "moods": "mood",
    "concerns": "concern",
    "progress": "progress",
}

OPS: frozenset[str] = frozenset({
    "set_thought", "set_mood", "set_concern", "close_concern",
    "set_progress", "close_thought", "close_progress", "clear_scope",
})

MAX_OPS = 12
MAX_ITEMS_PER_KIND = 8
MAX_RENDER_ITEMS = 3
OPEN_THREAD_MAX_AGE_SECONDS = 20 * 60
MAX_TEXT_CHARS = 600
MAX_LABEL_CHARS = 120
MAX_ID_CHARS = 120
MAX_SOURCE_EVENT_IDS = 16
MAX_SOURCE_ID_CHARS = 160
MAX_PROCESSED_EVENTS = 128
MAX_REQUESTS = 64
MAX_SUBJECT_KEY_CHARS = 256
STATE_BLOCK_MAX_TOKENS = 300

MOOD_HALF_LIFE_SECONDS = 6 * 3600
MOOD_MIN_RENDER_INTENSITY = 0.15

LEASE_DEFAULT_TTL_SECONDS = 90
LEASE_MAX_TTL_SECONDS = 3600
LEASE_RENEW_MIN_REMAINING_SECONDS = 45

#: Conflict reasons (host memory/companion_state.py:117-120).
REASON_INVALID_REQUEST = "invalid_request"
REASON_VERSION_CONFLICT = "version_conflict"
REASON_REQUEST_ID_CONFLICT = "request_id_conflict"
REASON_LEASE_STALE = "lease_stale"

#: Journal event name (host memory/event_log.py:96); persisted identifier.
EVT_COMPANION_STATE_COMMITTED = "companion.state_committed"

_CONTROL_CHARS = frozenset(chr(code) for code in range(0x20))
_SCENE_KEYS = ("plugin_id", "game_id", "session_id")
_SHARED_SCOPE_VALUES = frozenset({"shared", "global", "all"})

#: One lock per physical state file, shared process-wide (single-writer rule
#: makes this belt-and-braces, but hot reload may briefly hold two instances).
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()

#: Called with (character, normalized document) right before the replace.
WriteGuard = Callable[[str, Mapping[str, Any]], None]


class StateRequestError(ValueError):
    """Invalid commit/read/lease payload; \`reason\` is the host error code."""

    def __init__(self, reason: str = REASON_INVALID_REQUEST, message: str = "") -> None:
        self.reason = str(reason or REASON_INVALID_REQUEST)
        self.message = message or self.reason
        super().__init__(self.message)


def _lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


# -- time / text helpers -----------------------------------------------------


def _bounded_text(value: Any, limit: int, *, strip: bool = True) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip() if strip else value
    return text[:limit]


def _has_control_chars(value: str) -> bool:
    return any(char in _CONTROL_CHARS for char in value)


def _parse_iso(value: Any, *, now: datetime | None = None) -> datetime | None:
    """Parse an ISO timestamp into an aware datetime; naive input is local time.

    Host files store naive local ISO strings (host \`_coerce_now\`), while the
    core clock is tz-aware UTC. Attaching the machine-local zone to naive input
    keeps host-written rows comparable with the injected clock.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None:
        local_tz = (now.tzinfo if now is not None and now.tzinfo is not None
                    else datetime.now().astimezone().tzinfo)
        parsed = parsed.replace(tzinfo=local_tz or timezone.utc)
    return parsed


def _iso(now: datetime) -> str:
    return now.isoformat()


def _normalize_language(language: Any) -> str:
    raw = str(language or "").strip().replace("_", "-").lower()
    if not raw:
        return "zh"
    base = raw.split("-", 1)[0]
    if base == "zh":
        return "zh-TW" if ("tw" in raw or "hk" in raw or "hant" in raw) else "zh"
    if base in _STATE_LABELS:
        return base
    return "en"


def _estimate_tokens(text: str) -> int:
    """CJK-aware estimate used only when no TokenCounter is injected."""
    return max(1, len(text) // 2)


# -- scene helpers -----------------------------------------------------------


def normalize_scene(value: Any) -> dict[str, str]:
    """Keep the three scene keys (≤64 chars); non-mapping means no restriction."""
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for key in _SCENE_KEYS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            out[key] = item.strip()[:64]
    return out


def item_applies_to_scene(item: Any, scene: Any) -> bool:
    """Host rule: empty item.scene is cross-scene; otherwise exact match.

    Unlike \`invalidation.scene_applies\`, a missing current scene does NOT match
    a scene-restricted item (host \`entry_applies_to_scene(entry, None)\`).
    """
    if not isinstance(item, Mapping):
        return False
    restriction = item.get("scene")
    if not isinstance(restriction, Mapping) or not restriction:
        return True
    scope = restriction.get("scope")
    if isinstance(scope, str) and scope.strip().lower() in _SHARED_SCOPE_VALUES:
        return True
    current = normalize_scene(scene)
    if not current:
        return False
    for key in _SCENE_KEYS:
        wanted = restriction.get(key)
        if not isinstance(wanted, str) or not wanted.strip():
            continue
        if current.get(key) != wanted.strip()[:64]:
            return False
    return True


# -- document / row normalization -------------------------------------------


def empty_document(character: str = "") -> dict[str, Any]:
    """A brand-new empty document (never written unless a caller saves it)."""
    return {
        "schema": SCHEMA_VERSION,
        "character": str(character or ""),
        "subjects": {},
    }


def new_subject_row() -> dict[str, Any]:
    """A fresh subject row with host defaults."""
    return {
        "schema": SCHEMA_VERSION,
        "version": 0,
        "updated_at": None,
        "state": {kind: [] for kind in STATE_KINDS},
        "processed_events": [],
        "requests": {},
        "lease": None,
    }


def normalize_subject_key(value: Any) -> str:
    """Normalize a subject key; empty means private (host rule, no kind check)."""
    if value is None:
        return DEFAULT_SUBJECT
    if not isinstance(value, str):
        raise StateRequestError(message="subject must be a string")
    key = value.strip()
    if not key:
        return DEFAULT_SUBJECT
    if len(key) > MAX_SUBJECT_KEY_CHARS or _has_control_chars(key):
        raise StateRequestError(message="subject is malformed")
    return key


def _normalize_source_event_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise StateRequestError(message="source_event_ids must be a list")
    ids: list[str] = []
    for raw in value[:MAX_SOURCE_EVENT_IDS]:
        source_id = _bounded_text(raw, MAX_SOURCE_ID_CHARS)
        if source_id and source_id not in ids:
            ids.append(source_id)
    return ids


def _normalize_expires_at(value: Any) -> str | None:
    if value is None or value == "":
        return None
    parsed = _parse_iso(value)
    if parsed is None:
        raise StateRequestError(message="expires_at is not ISO time")
    return _iso(parsed)


def _normalize_lease(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    lease_id = _bounded_text(raw.get("lease_id"), 128)
    instance = _bounded_text(raw.get("instance"), 128)
    if not lease_id or not instance:
        return None
    return {
        "lease_id": lease_id,
        "instance": instance,
        "scene": normalize_scene(raw.get("scene")),
        "acquired_at": _bounded_text(raw.get("acquired_at"), 40) or None,
        "expires_at": _bounded_text(raw.get("expires_at"), 40) or None,
    }


def _normalize_item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    item: dict[str, Any] = {
        "id": _bounded_text(raw.get("id"), MAX_ID_CHARS) or uuid.uuid4().hex[:16],
        "source_event_ids": _normalize_source_event_ids(raw.get("source_event_ids")),
        "scene": normalize_scene(raw.get("scene")),
        "created_at": _bounded_text(raw.get("created_at"), 40) or None,
        "updated_at": _bounded_text(raw.get("updated_at"), 40) or None,
    }
    if raw.get("expires_at"):
        item["expires_at"] = _bounded_text(raw.get("expires_at"), 40)
    if "label" in raw or "intensity" in raw:
        item["label"] = _bounded_text(raw.get("label"), MAX_LABEL_CHARS)
        try:
            intensity = float(raw.get("intensity") or 0.0)
        except (TypeError, ValueError):
            intensity = 0.0
        item["intensity"] = max(0.0, min(1.0, intensity))
        cause = _bounded_text(raw.get("cause"), MAX_TEXT_CHARS)
        if cause:
            item["cause"] = cause
    else:
        item["text"] = _bounded_text(raw.get("text"), MAX_TEXT_CHARS)
    for key in ("why", "next_step"):
        value = _bounded_text(raw.get(key), MAX_TEXT_CHARS)
        if value:
            item[key] = value
    for key in ("followup_at", "event_at"):
        if raw.get(key):
            item[key] = _normalize_expires_at(raw[key])
    if "agreed_followup" in raw:
        item["agreed_followup"] = bool(raw["agreed_followup"])
    return item


def _normalize_requests(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return {}
    kept: dict[str, dict[str, Any]] = {}
    for key, value in list(raw.items())[:MAX_REQUESTS]:
        if not isinstance(key, str) or not key or not isinstance(value, Mapping):
            continue
        version = value.get("version")
        kept[key] = {
            "fingerprint": _bounded_text(value.get("fingerprint"), 64),
            "at": _bounded_text(value.get("at"), 40) or None,
            "version": version if isinstance(version, int) and not isinstance(version, bool) else 0,
            "applied": bool(value.get("applied", True)),
        }
    return kept


def normalize_subject_row(raw: Any) -> dict[str, Any]:
    """Tolerant row normalization (disk and replay payloads share this)."""
    row = new_subject_row()
    if not isinstance(raw, Mapping):
        return row
    try:
        row["version"] = max(0, int(raw.get("version") or 0))
    except (TypeError, ValueError):
        row["version"] = 0
    row["updated_at"] = _bounded_text(raw.get("updated_at"), 40) or None
    state = raw.get("state")
    if isinstance(state, Mapping):
        for kind in STATE_KINDS:
            rows = state.get(kind)
            if not isinstance(rows, (list, tuple)):
                continue
            normalized = [
                item for item in (_normalize_item(entry) for entry in rows)
                if item is not None
            ]
            row["state"][kind] = normalized[:MAX_ITEMS_PER_KIND]
    row["processed_events"] = _normalize_source_event_ids(
        list(raw.get("processed_events") or [])[:MAX_PROCESSED_EVENTS],
    )
    row["requests"] = _normalize_requests(raw.get("requests"))
    row["lease"] = _normalize_lease(raw.get("lease"))
    return row


def normalize_document(raw: Any, character: str | None = None) -> dict[str, Any]:
    """Normalize a document; top-level shape errors are corruption, not empty."""
    document = empty_document(
        character if character is not None else (
            _bounded_text(raw.get("character"), 200) if isinstance(raw, Mapping) else ""
        ),
    )
    if raw is None:
        return document
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"companion_state: expected a JSON object, found {type(raw).__name__}",
        )
    schema = raw.get("schema", SCHEMA_VERSION)
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise InvalidSchemaError(
            f"companion_state: missing or non-integer schema: {schema!r}",
        )
    if schema != SCHEMA_VERSION:
        raise InvalidSchemaError(
            f"unsupported companion_state schema {schema}; this build supports "
            f"{SCHEMA_VERSION}",
        )
    subjects = raw.get("subjects")
    if subjects is None:
        return document
    if not isinstance(subjects, Mapping):
        raise CorruptStoreError(
            f"companion_state.subjects: expected an object, found "
            f"{type(subjects).__name__}",
        )
    for key, value in subjects.items():
        if not isinstance(key, str) or not key:
            raise CorruptStoreError("companion_state.subjects: empty subject key")
        document["subjects"][key] = normalize_subject_row(value)
    return document


def _validated_document(document: Any, character: str) -> dict[str, Any]:
    """Validate a caller payload (caller bug -> ValueError, disk untouched)."""
    if not isinstance(document, Mapping):
        raise ValueError("companion_state: document must be a mapping")
    subjects = document.get("subjects", {})
    if not isinstance(subjects, Mapping):
        raise ValueError("companion_state.subjects must be a mapping")
    for key, value in subjects.items():
        if not isinstance(key, str) or not key:
            raise ValueError("companion_state: subject keys must be nonempty strings")
        if not isinstance(value, Mapping):
            raise ValueError(
                f"companion_state.subjects[{key!r}] must be a mapping",
            )
    return normalize_document(document, character)


# -- request normalization ---------------------------------------------------


def normalize_op(op: Any, *, internal: bool) -> dict[str, Any]:
    """Normalize one op (host \`_normalize_op\`); raises StateRequestError."""
    if not isinstance(op, Mapping):
        raise StateRequestError(message="op must be an object")
    op_type = op.get("op")
    if op_type not in OPS:
        raise StateRequestError(message="unsupported op")
    normalized: dict[str, Any] = {"op": op_type}
    source_ids = _normalize_source_event_ids(op.get("source_event_ids"))
    scene = normalize_scene(op.get("scene"))
    if op_type == "clear_scope":
        all_scopes = bool(op.get("all"))
        if not scene and not all_scopes:
            raise StateRequestError(message="clear_scope requires scene or all=true")
        normalized["scene"] = scene
        normalized["all"] = all_scopes
        normalized["source_event_ids"] = source_ids
        return normalized
    if not internal and not source_ids:
        raise StateRequestError(message="external set_* op requires source_event_ids")
    normalized["source_event_ids"] = source_ids
    normalized["scene"] = scene
    expires_at = _normalize_expires_at(op.get("expires_at"))
    if expires_at:
        normalized["expires_at"] = expires_at
    item_id = _bounded_text(op.get("id"), MAX_ID_CHARS)
    if item_id:
        normalized["id"] = item_id
    if op_type == "set_mood":
        normalized["label"] = _require_non_empty(op.get("label"), "label", MAX_LABEL_CHARS)
        intensity = op.get("intensity")
        if isinstance(intensity, bool) or not isinstance(intensity, (int, float)):
            raise StateRequestError(message="intensity must be a number")
        normalized["intensity"] = max(0.0, min(1.0, float(intensity)))
        cause = _bounded_text(op.get("cause"), MAX_TEXT_CHARS)
        if cause:
            normalized["cause"] = cause
        return normalized
    if op_type in {"close_concern", "close_thought", "close_progress"}:
        text = _bounded_text(op.get("text"), MAX_TEXT_CHARS)
        if text:
            normalized["text"] = text
        if not item_id and not text and not source_ids:
            raise StateRequestError(
                message=f"{op_type} requires id, text or source_event_ids",
            )
        return normalized
    normalized["text"] = _require_non_empty(op.get("text"), "text", MAX_TEXT_CHARS)
    for key in ("followup_at", "event_at"):
        if key in op:
            normalized[key] = _normalize_expires_at(op[key])
    if "agreed_followup" in op:
        normalized["agreed_followup"] = bool(op["agreed_followup"])
    if op_type == "set_concern":
        for key in ("why", "next_step"):
            value = _bounded_text(op.get(key), MAX_TEXT_CHARS)
            if value:
                normalized[key] = value
    elif op_type == "set_progress":
        next_step = _bounded_text(op.get("next_step"), MAX_TEXT_CHARS)
        if next_step:
            normalized["next_step"] = next_step
    return normalized


def _require_non_empty(value: Any, field: str, limit: int) -> str:
    text = _bounded_text(value, limit)
    if not text:
        raise StateRequestError(message=f"{field} is required")
    return text


def fingerprint_ops(
    ops: Sequence[Mapping[str, Any]], processed_event_ids: Sequence[str],
) -> str:
    """Idempotency fingerprint (host rule: ops + processed events only)."""
    payload = json.dumps(
        {"ops": list(ops), "processed_event_ids": list(processed_event_ids)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def normalize_commit_request(
    request: Any, *, language: str | None = None,
) -> dict[str, Any]:
    """Normalize a commit request; raises StateRequestError on invalid input."""
    if not isinstance(request, Mapping):
        raise StateRequestError(message="request must be an object")
    subject = normalize_subject_key(request.get("subject"))
    scene = normalize_scene(request.get("scene"))
    producer = _bounded_text(request.get("producer"), 64) or "external"
    instance = _bounded_text(request.get("instance"), 128) or None
    lease_id = _bounded_text(request.get("lease_id"), 128) or None
    request_id = _bounded_text(request.get("request_id"), 128) or None
    internal = bool(request.get("internal"))
    base_version = request.get("base_version")
    if base_version is not None:
        if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 0:
            raise StateRequestError(message="base_version must be a non-negative int")
    ops_raw = request.get("ops") or []
    if not isinstance(ops_raw, (list, tuple)):
        raise StateRequestError(message="ops must be a list")
    if len(ops_raw) > MAX_OPS:
        raise StateRequestError(message="too many ops")
    ops = [normalize_op(op, internal=internal) for op in ops_raw]
    processed_raw = request.get("processed_event_ids") or []
    if not isinstance(processed_raw, (list, tuple)):
        raise StateRequestError(message="processed_event_ids must be a list")
    if len(processed_raw) > MAX_PROCESSED_EVENTS:
        raise StateRequestError(message="too many processed_event_ids")
    processed = _normalize_source_event_ids(processed_raw)
    return {
        "subject": subject,
        "scene": scene,
        "producer": producer,
        "instance": instance,
        "lease_id": lease_id,
        "request_id": request_id,
        "internal": internal,
        "base_version": base_version,
        "ops": ops,
        "processed_event_ids": processed,
        "fingerprint": fingerprint_ops(ops, processed),
        "language": _bounded_text(language or request.get("language"), 16) or None,
    }


# -- state application (pure) ------------------------------------------------


def _set_item(
    rows: list[dict], op: Mapping[str, Any], now: datetime,
    extra_fields: tuple[str, ...] = (),
) -> bool:
    item_id = op.get("id")
    for item in rows:
        if item_id and item.get("id") == item_id:
            changed = False
            text = op.get("text")
            if isinstance(text, str) and item.get("text") != text:
                item["text"] = text
                changed = True
            for key in extra_fields:
                if key in op and item.get(key) != op[key]:
                    item[key] = op[key]
                    changed = True
            if op.get("scene") and item.get("scene") != dict(op["scene"]):
                item["scene"] = dict(op["scene"])
                changed = True
            if op.get("expires_at") and item.get("expires_at") != op["expires_at"]:
                item["expires_at"] = op["expires_at"]
                changed = True
            source_ids = op.get("source_event_ids") or []
            if source_ids:
                merged = list(item.get("source_event_ids") or [])
                for source_id in source_ids:
                    if source_id not in merged:
                        merged.append(source_id)
                if merged != item.get("source_event_ids"):
                    item["source_event_ids"] = merged
                    changed = True
            if changed:
                item["updated_at"] = _iso(now)
            return changed
    row: dict[str, Any] = {
        "id": str(item_id or uuid.uuid4().hex[:16]),
        "text": op.get("text") or "",
        "source_event_ids": list(op.get("source_event_ids") or []),
        "scene": dict(op.get("scene") or {}),
        "created_at": _iso(now),
        "updated_at": _iso(now),
    }
    for key in extra_fields:
        if op.get(key):
            row[key] = op[key]
    if op.get("expires_at"):
        row["expires_at"] = op["expires_at"]
    rows.append(row)
    return True


def _set_mood(rows: list[dict], op: Mapping[str, Any], now: datetime) -> bool:
    item_id = op.get("id")
    for item in rows:
        if item_id and item.get("id") == item_id:
            changed = False
            for key in ("label", "intensity", "cause"):
                if key in op and item.get(key) != op.get(key):
                    item[key] = op[key]
                    changed = True
            if op.get("scene") and item.get("scene") != dict(op["scene"]):
                item["scene"] = dict(op["scene"])
                changed = True
            if op.get("expires_at") and item.get("expires_at") != op["expires_at"]:
                item["expires_at"] = op["expires_at"]
                changed = True
            source_ids = op.get("source_event_ids") or []
            merged = list(item.get("source_event_ids") or [])
            for source_id in source_ids:
                if source_id not in merged:
                    merged.append(source_id)
            if merged != item.get("source_event_ids"):
                item["source_event_ids"] = merged
                changed = True
            if changed:
                item["updated_at"] = _iso(now)
            return changed
    row: dict[str, Any] = {
        "id": str(item_id or uuid.uuid4().hex[:16]),
        "label": op.get("label") or "",
        "intensity": float(op.get("intensity") or 0.0),
        "source_event_ids": list(op.get("source_event_ids") or []),
        "scene": dict(op.get("scene") or {}),
        "created_at": _iso(now),
        "updated_at": _iso(now),
    }
    if op.get("cause"):
        row["cause"] = op["cause"]
    if op.get("expires_at"):
        row["expires_at"] = op["expires_at"]
    rows.append(row)
    return True


def _close_item(rows: list[dict], op: Mapping[str, Any]) -> bool:
    item_id = op.get("id")
    text = op.get("text")
    source_ids = set(op.get("source_event_ids") or ())
    for index, item in enumerate(rows):
        matched = False
        if item_id and item.get("id") == item_id:
            matched = True
        elif text and str(item.get("text") or "").strip() == text:
            matched = True
        elif source_ids and source_ids & set(item.get("source_event_ids") or ()):
            matched = True
        if matched:
            rows.pop(index)
            return True
    return False


def _clear_scope(state: dict[str, Any], op: Mapping[str, Any]) -> bool:
    scene = normalize_scene(op.get("scene"))
    clear_all = bool(op.get("all"))
    changed = False
    for kind in STATE_KINDS:
        rows = state.get(kind)
        if not isinstance(rows, list):
            continue
        kept: list[dict[str, Any]] = []
        for item in rows:
            item_scene = normalize_scene(item.get("scene"))
            matched = clear_all or (bool(scene) and item_scene == scene)
            if matched:
                changed = True
            else:
                kept.append(item)
        state[kind] = kept
    return changed


def apply_ops(state: dict[str, Any], ops: Sequence[Mapping[str, Any]], now: datetime) -> int:
    """Apply normalized ops to a state copy; returns the changed-op count."""
    applied = 0
    for op in ops:
        op_type = op.get("op")
        if op_type == "set_thought":
            changed = _set_item(state.setdefault("thoughts", []), op, now)
        elif op_type == "set_concern":
            changed = _set_item(
                state.setdefault("concerns", []), op, now,
                extra_fields=("why", "next_step", "followup_at", "event_at", "agreed_followup"),
            )
        elif op_type == "set_progress":
            changed = _set_item(
                state.setdefault("progress", []), op, now,
                extra_fields=("next_step", "followup_at", "event_at", "agreed_followup"),
            )
        elif op_type == "set_mood":
            changed = _set_mood(state.setdefault("moods", []), op, now)
        elif op_type in {"close_concern", "close_thought", "close_progress"}:
            kind = {
                "close_concern": "concerns",
                "close_thought": "thoughts",
                "close_progress": "progress",
            }[op_type]
            changed = _close_item(state.setdefault(kind, []), op)
        else:  # clear_scope
            changed = _clear_scope(state, op)
        if changed:
            applied += 1
    return applied


def op_already_processed(op: Mapping[str, Any], processed: set[str]) -> bool:
    """Host rule: clear_scope/close_concern source ids locate targets, not replay."""
    if op.get("op") in ("clear_scope", "close_concern"):
        return False
    source_ids = op.get("source_event_ids") or ()
    return bool(source_ids) and all(source_id in processed for source_id in source_ids)


def _is_expired(item: Mapping[str, Any], now: datetime) -> bool:
    expires_at = _parse_iso(item.get("expires_at"), now=now)
    if expires_at is not None and expires_at <= now:
        return True
    if str(item.get("id") or "").startswith("open_threads:"):
        updated = _parse_iso(item.get("updated_at") or item.get("created_at"), now=now)
        return updated is None or (now - updated).total_seconds() >= OPEN_THREAD_MAX_AGE_SECONDS
    return False


def _item_sort_key(item: Mapping[str, Any]) -> str:
    return str(item.get("updated_at") or item.get("created_at") or "")


def mood_effective_intensity(item: Mapping[str, Any], now: Any = None) -> float:
    """Read-only linear decay: half at 6h, zero at 12h (host rule)."""
    try:
        intensity = float(item.get("intensity") or 0.0)
    except (TypeError, ValueError):
        intensity = 0.0
    intensity = max(0.0, min(1.0, intensity))
    if intensity <= 0.0:
        return 0.0
    now_dt = now if isinstance(now, datetime) else datetime.now().astimezone()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    updated_at = _parse_iso(item.get("updated_at") or item.get("created_at"), now=now_dt)
    if updated_at is None:
        return intensity
    age_seconds = (now_dt - updated_at).total_seconds()
    if age_seconds <= 0:
        return intensity
    factor = 1.0 - age_seconds / (2.0 * MOOD_HALF_LIFE_SECONDS)
    return intensity * max(0.0, factor)


def decay_moods(rows: Any, now: Any = None) -> list[dict[str, Any]]:
    """Visible mood rows with \`effective_intensity\` (below threshold dropped)."""
    now_dt = now if isinstance(now, datetime) else datetime.now().astimezone()
    out: list[dict[str, Any]] = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        intensity = mood_effective_intensity(row, now_dt)
        if intensity < MOOD_MIN_RENDER_INTENSITY:
            continue
        item = dict(row)
        item["effective_intensity"] = round(intensity, 4)
        out.append(item)
    return out


def _visible_items(
    rows: Any, kind: str, scene: Any, now: datetime,
) -> list[dict[str, Any]]:
    if not isinstance(rows, (list, tuple)):
        return []
    visible: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or not item_applies_to_scene(row, scene):
            continue
        if _is_expired(row, now):
            continue
        if kind == "moods" and mood_effective_intensity(row, now) < MOOD_MIN_RENDER_INTENSITY:
            continue
        visible.append(dict(row))
    visible.sort(key=_item_sort_key, reverse=True)
    return visible


def _public_item(kind: str, item: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    public: dict[str, Any] = {
        "id": str(item.get("id") or ""),
        "source_event_ids": list(item.get("source_event_ids") or []),
        "scene": normalize_scene(item.get("scene")),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }
    if item.get("expires_at"):
        public["expires_at"] = item.get("expires_at")
    if kind == "moods":
        public["label"] = str(item.get("label") or "")
        try:
            public["intensity"] = round(float(item.get("intensity") or 0.0), 4)
        except (TypeError, ValueError):
            public["intensity"] = 0.0
        public["effective_intensity"] = round(mood_effective_intensity(item, now), 4)
        if item.get("cause"):
            public["cause"] = str(item.get("cause"))
    else:
        public["text"] = str(item.get("text") or "")
        for key in ("why", "next_step"):
            if item.get(key):
                public[key] = str(item[key])
    for key in ("followup_at", "event_at", "agreed_followup"):
        if key in item:
            public[key] = item[key]
    return public


def public_state(
    document: Any,
    *,
    subject: str = DEFAULT_SUBJECT,
    scene: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    """Public subject view with scene/expiry/mood filtering applied (no writes)."""
    now_dt = now if isinstance(now, datetime) else datetime.now().astimezone()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    subject_key = normalize_subject_key(subject)
    subjects = document.get("subjects") if isinstance(document, Mapping) else None
    raw_row = subjects.get(subject_key) if isinstance(subjects, Mapping) else None
    row = normalize_subject_row(raw_row)
    visible_state: dict[str, list[dict[str, Any]]] = {}
    items: list[dict[str, Any]] = []
    for kind in STATE_KINDS:
        rows = _visible_items(row["state"].get(kind), kind, scene, now_dt)
        public_rows = [_public_item(kind, item, now_dt) for item in rows]
        visible_state[kind] = public_rows
        for item in public_rows:
            items.append({"type": ITEM_TYPE_BY_KIND[kind], **item})
    return {
        "subject": subject_key,
        "version": int(row.get("version") or 0),
        "updated_at": row.get("updated_at"),
        "state": visible_state,
        "items": items,
        "lease": dict(row.get("lease") or {}) or None,
        "processed_events": list(row.get("processed_events") or []),
    }


def _prune_items(rows: Any, now: datetime) -> list[dict[str, Any]]:
    live: list[dict[str, Any]] = []
    for row in rows or ():
        if not isinstance(row, dict) or _is_expired(row, now):
            continue
        live.append(row)
    live.sort(key=_item_sort_key, reverse=True)
    return live[:MAX_ITEMS_PER_KIND]


def prune_state(state: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Drop expired rows and keep at most MAX_ITEMS_PER_KIND per kind."""
    return {kind: _prune_items(state.get(kind), now) for kind in STATE_KINDS}


def cap_tail(values: Sequence[str], limit: int) -> list[str]:
    return list(values[-limit:]) if len(values) > limit else list(values)


def cap_requests(requests: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if len(requests) <= MAX_REQUESTS:
        return {str(key): dict(value) for key, value in requests.items()
                if isinstance(value, Mapping)}
    ordered = sorted(
        requests.items(),
        key=lambda pair: str((pair[1] or {}).get("at") or ""),
    )
    return {str(key): dict(value) for key, value in ordered[-MAX_REQUESTS:]}


def document_with_subject(subject: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap one subject row into a single-subject document for public/render."""
    return {
        "schema": SCHEMA_VERSION,
        "character": "",
        "subjects": {normalize_subject_key(subject): normalize_subject_row(row)},
    }


# -- rendering ---------------------------------------------------------------

_STATE_LABELS: dict[str, dict[str, str]] = {
    "zh": {
        "thought": "想法", "mood": "情绪", "concern": "关注", "progress": "进展",
        "intensity": "强度", "cause": "因为", "sep": "：", "comma": "，",
    },
    "zh-TW": {
        "thought": "想法", "mood": "情緒", "concern": "關注", "progress": "進展",
        "intensity": "強度", "cause": "因為", "sep": "：", "comma": "，",
    },
    "en": {
        "thought": "Thought", "mood": "Mood", "concern": "Concern", "progress": "Progress",
        "intensity": "intensity", "cause": "because", "sep": ": ", "comma": ", ",
    },
    "ja": {
        "thought": "考え", "mood": "気分", "concern": "気がかり", "progress": "進み具合",
        "intensity": "強さ", "cause": "理由", "sep": "：", "comma": "、",
    },
    "ko": {
        "thought": "생각", "mood": "기분", "concern": "관심", "progress": "진행",
        "intensity": "강도", "cause": "이유", "sep": ": ", "comma": ", ",
    },
    "ru": {
        "thought": "Мысль", "mood": "Настроение", "concern": "Забота",
        "progress": "Прогресс", "intensity": "интенсивность", "cause": "потому что",
        "sep": ": ", "comma": ", ",
    },
    "es": {
        "thought": "Idea", "mood": "Ánimo", "concern": "Preocupación",
        "progress": "Progreso", "intensity": "intensidad", "cause": "porque",
        "sep": ": ", "comma": ", ",
    },
    "pt": {
        "thought": "Ideia", "mood": "Humor", "concern": "Preocupação",
        "progress": "Progresso", "intensity": "intensidade", "cause": "porque",
        "sep": ": ", "comma": ", ",
    },
}


def _render_line(
    kind: str, item: Mapping[str, Any], labels: Mapping[str, str], now: datetime,
) -> str:
    sep = labels["sep"]
    if kind == "moods":
        intensity = mood_effective_intensity(item, now)
        line = (
            f"{labels['mood']}{sep}{item.get('label') or ''}"
            f" ({labels['intensity']} {intensity:.2f})"
        )
        cause = str(item.get("cause") or "").strip()
        if cause:
            line = f"{line}{labels['comma']}{labels['cause']} {cause}"
        return line
    return f"{labels[ITEM_TYPE_BY_KIND[kind]]}{sep}{item.get('text') or ''}"


def render_state_block(
    document: Any,
    *,
    subject: str = DEFAULT_SUBJECT,
    scene: Any = None,
    max_tokens: int = STATE_BLOCK_MAX_TOKENS,
    now: Any = None,
    language: str | None = None,
) -> str:
    """Render the public state as a compact prompt block (≤ max_tokens)."""
    budget = int(max_tokens or 0)
    if budget <= 0:
        return ""
    now_dt = now if isinstance(now, datetime) else datetime.now().astimezone()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    public = public_state(document, subject=subject, scene=scene, now=now_dt)
    labels = _STATE_LABELS[_normalize_language(language)]
    lines: list[str] = []
    for kind in STATE_KINDS:
        for item in public["state"].get(kind, [])[:MAX_RENDER_ITEMS]:
            line = _render_line(kind, item, labels, now_dt).strip()
            if line:
                lines.append(line)
    if not lines:
        return ""
    used = 0
    kept: list[str] = []
    for line in lines:
        cost = _estimate_tokens(line)
        if used + cost > budget:
            if not kept:
                kept.append(line[: max(1, budget * 2)])
            break
        kept.append(line)
        used += cost
    return "\n".join(kept).strip()


# -- store -------------------------------------------------------------------


class StateStore:
    """Per-character companion_state.json with strict reads and journal replay."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
        write_guard: WriteGuard | None = None,
    ) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        self._write_guard = write_guard

    # -- paths ----------------------------------------------------------

    def _path(self, character: str, *, create: bool = False) -> Path:
        return self._roots.character_dir(character, create=create) / STATE_FILE

    # -- reads ----------------------------------------------------------

    def _load_document(self, character: str) -> dict[str, Any]:
        path = self._path(character)
        raw = self._store.load(path)
        if raw is None:
            return empty_document(character)
        return normalize_document(raw, character)

    def load(self, character: str) -> dict[str, Any]:
        """Return the normalized document; corruption raises, never empty."""
        path = self._path(character)
        with _lock_for(path):
            return self._load_document(character)

    def subject(self, character: str, subject: str = DEFAULT_SUBJECT) -> dict[str, Any]:
        """Return one normalized subject row (fresh empty row when absent)."""
        document = self.load(character)
        key = normalize_subject_key(subject)
        return normalize_subject_row(document["subjects"].get(key))

    def all_subjects(self, character: str) -> dict[str, dict[str, Any]]:
        """Return every normalized subject row, keyed by subject."""
        document = self.load(character)
        return {
            str(key): normalize_subject_row(value)
            for key, value in document["subjects"].items()
        }

    # -- writes ---------------------------------------------------------

    def save(self, character: str, document: Mapping[str, Any]) -> None:
        """Atomically replace the state file after permit/guard validation."""
        payload = _validated_document(document, character)
        path = self._path(character)
        with _lock_for(path):
            # Refuse to replace a file this reader would reject (fail closed).
            self._load_document(character)
            self._permit.assert_writable("save_companion_state", str(path))
            if self._write_guard is not None:
                self._write_guard(character, payload)
            self._store.save(path, payload)

    def upsert_subject(
        self, character: str, subject: str, row: Mapping[str, Any],
    ) -> bool:
        """Insert/replace one subject row; returns True when the row changed."""
        key = normalize_subject_key(subject)
        if not isinstance(row, Mapping):
            raise ValueError("subject row must be a mapping")
        normalized = normalize_subject_row(row)
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            if document["subjects"].get(key) == normalized:
                return False
            document["subjects"][key] = normalized
            self.save(character, document)
            return True

    # -- journal replay -------------------------------------------------

    def replay_snapshot(self, character: str, payload: Any) -> bool:
        """Idempotent \`companion.state_committed\` handler.

        Accepts the core wrapper \`{character, subject, snapshot}\` and the host
        legacy shape (the subject snapshot itself). A payload without an integer
        version is unapplicable and returns False without blocking replay (host
        behavior); a damaged state file raises so replay pauses instead of
        silently dropping the event.
        """
        if not isinstance(payload, Mapping):
            return False
        wrapper = payload.get("snapshot")
        if isinstance(wrapper, Mapping):
            snapshot: Mapping[str, Any] = wrapper
            raw_subject = payload.get("subject", snapshot.get("subject"))
        else:
            snapshot = payload
            raw_subject = payload.get("subject")
        version = snapshot.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            return False
        subject_key = normalize_subject_key(raw_subject)
        row = normalize_subject_row(snapshot)
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            if document["subjects"].get(subject_key) == row:
                return False
            document["subjects"][subject_key] = row
            self.save(character, document)
            return True


__all__ = [
    "DEFAULT_SUBJECT",
    "EVT_COMPANION_STATE_COMMITTED",
    "ITEM_TYPE_BY_KIND",
    "LEASE_DEFAULT_TTL_SECONDS",
    "LEASE_MAX_TTL_SECONDS",
    "LEASE_RENEW_MIN_REMAINING_SECONDS",
    "MAX_ID_CHARS",
    "MAX_ITEMS_PER_KIND",
    "MAX_LABEL_CHARS",
    "MAX_OPS",
    "MAX_PROCESSED_EVENTS",
    "MAX_REQUESTS",
    "MAX_RENDER_ITEMS",
    "MAX_SOURCE_EVENT_IDS",
    "MAX_SOURCE_ID_CHARS",
    "MAX_SUBJECT_KEY_CHARS",
    "MAX_TEXT_CHARS",
    "MOOD_HALF_LIFE_SECONDS",
    "MOOD_MIN_RENDER_INTENSITY",
    "OPEN_THREAD_MAX_AGE_SECONDS",
    "OPS",
    "REASON_INVALID_REQUEST",
    "REASON_LEASE_STALE",
    "REASON_REQUEST_ID_CONFLICT",
    "REASON_VERSION_CONFLICT",
    "SCHEMA_VERSION",
    "STATE_BLOCK_MAX_TOKENS",
    "STATE_FILE",
    "STATE_FILENAME",
    "STATE_KINDS",
    "StateRequestError",
    "StateStore",
    "WriteGuard",
    "apply_ops",
    "cap_requests",
    "cap_tail",
    "decay_moods",
    "document_with_subject",
    "empty_document",
    "fingerprint_ops",
    "item_applies_to_scene",
    "mood_effective_intensity",
    "new_subject_row",
    "normalize_commit_request",
    "normalize_document",
    "normalize_op",
    "normalize_scene",
    "normalize_subject_key",
    "normalize_subject_row",
    "op_already_processed",
    "prune_state",
    "public_state",
    "render_state_block",
]
