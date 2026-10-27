"""Stable source keys shared by ingest, dedup, correction and deletion.

Rewritten from the host contract (design §17.8, `memory/event_sources.py`):
a host event is `host:<character>:<subject>:<event_id>` and a plugin event is
`plugin:<plugin_id>:<character>:<subject>:<session_id>:<event_id>`. Both the
host and every plugin must clean components identically or the same event
would get two keys, so the cleaning rules are frozen here:

* strip control characters and surrounding whitespace;
* fold internal whitespace to a single underscore;
* replace half/full-width colons with underscores (so splitting on ":" is
  unambiguous);
* truncate each component to its own limit; an empty component is invalid.

Pure functions only: no file IO, no event loop, no host imports.
"""

from __future__ import annotations

import re
from typing import Any

HOST_PREFIX = "host"
PLUGIN_PREFIX = "plugin"

MAX_CHARACTER_CHARS = 120
MAX_SUBJECT_CHARS = 256
MAX_EVENT_ID_CHARS = 200
MAX_PLUGIN_ID_CHARS = 64
MAX_SESSION_ID_CHARS = 64
#: Defensive cap for the whole key: component limits plus separators.
MAX_KEY_CHARS = (
    len(PLUGIN_PREFIX) + MAX_PLUGIN_ID_CHARS + MAX_CHARACTER_CHARS
    + MAX_SUBJECT_CHARS + MAX_SESSION_ID_CHARS + MAX_EVENT_ID_CHARS + 8
)

_WHITESPACE_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_COLONS = str.maketrans({":": "_", "\uff1a": "_"})


def sanitize_component(value: Any, *, max_chars: int = MAX_EVENT_ID_CHARS) -> str:
    """Clean one source-key component; an empty result means invalid input."""
    if value is None:
        return ""
    text = _CONTROL_RE.sub("", str(value))
    text = text.strip()
    text = _WHITESPACE_RE.sub("_", text)
    text = text.translate(_COLONS)
    limit = int(max_chars)
    if limit <= 0:
        return ""
    return text[:limit]


def normalize_local_event_id(value: Any) -> str:
    """Normalized local event id; empty when the input has no usable content."""
    return sanitize_component(value, max_chars=MAX_EVENT_ID_CHARS)


def _require_component(value: Any, *, max_chars: int, label: str) -> str:
    component = sanitize_component(value, max_chars=max_chars)
    if not component:
        raise ValueError(f"source key component {label!r} must not be empty")
    return component


def build_host_source_key(character: Any, subject: Any, event_id: Any) -> str:
    """Build `host:<character>:<subject>:<event_id>`."""
    return ":".join((
        HOST_PREFIX,
        _require_component(character, max_chars=MAX_CHARACTER_CHARS, label="character"),
        _require_component(subject, max_chars=MAX_SUBJECT_CHARS, label="subject"),
        _require_component(event_id, max_chars=MAX_EVENT_ID_CHARS, label="event_id"),
    ))


def build_plugin_source_key(
    plugin_id: Any,
    character: Any,
    subject: Any,
    session_id: Any,
    event_id: Any,
) -> str:
    """Build `plugin:<plugin_id>:<character>:<subject>:<session_id>:<event_id>`."""
    return ":".join((
        PLUGIN_PREFIX,
        _require_component(plugin_id, max_chars=MAX_PLUGIN_ID_CHARS, label="plugin_id"),
        _require_component(character, max_chars=MAX_CHARACTER_CHARS, label="character"),
        _require_component(subject, max_chars=MAX_SUBJECT_CHARS, label="subject"),
        _require_component(session_id, max_chars=MAX_SESSION_ID_CHARS, label="session_id"),
        _require_component(event_id, max_chars=MAX_EVENT_ID_CHARS, label="event_id"),
    ))


def _component_ok(component: str, max_chars: int) -> bool:
    if not component or len(component) > max_chars:
        return False
    if ":" in component or "\uff1a" in component:
        return False
    return sanitize_component(component, max_chars=max_chars) == component


def parse_source_key(key: Any) -> dict[str, str | None] | None:
    """Parse a source key into its components; None when the format is invalid.

    Components must already be in cleaned form (no colons, within limits): a
    key that merely looks like a key but was never normalized is rejected, so
    the same event cannot be addressed by two different spellings.
    """
    if not isinstance(key, str):
        return None
    text = key.strip()
    if not text or len(text) > MAX_KEY_CHARS:
        return None
    parts = text.split(":")
    if parts[0] == HOST_PREFIX:
        if len(parts) != 4:
            return None
        character, subject, event_id = parts[1:]
        if not (
            _component_ok(character, MAX_CHARACTER_CHARS)
            and _component_ok(subject, MAX_SUBJECT_CHARS)
            and _component_ok(event_id, MAX_EVENT_ID_CHARS)
        ):
            return None
        return {
            "kind": HOST_PREFIX,
            "plugin_id": None,
            "character": character,
            "subject": subject,
            "session_id": None,
            "event_id": event_id,
        }
    if parts[0] == PLUGIN_PREFIX:
        if len(parts) != 6:
            return None
        plugin_id, character, subject, session_id, event_id = parts[1:]
        if not (
            _component_ok(plugin_id, MAX_PLUGIN_ID_CHARS)
            and _component_ok(character, MAX_CHARACTER_CHARS)
            and _component_ok(subject, MAX_SUBJECT_CHARS)
            and _component_ok(session_id, MAX_SESSION_ID_CHARS)
            and _component_ok(event_id, MAX_EVENT_ID_CHARS)
        ):
            return None
        return {
            "kind": PLUGIN_PREFIX,
            "plugin_id": plugin_id,
            "character": character,
            "subject": subject,
            "session_id": session_id,
            "event_id": event_id,
        }
    return None


def is_valid_source_key(key: Any) -> bool:
    """Whether `key` is a well-formed host/plugin source key."""
    return parse_source_key(key) is not None


def plugin_id_of(key: Any) -> str | None:
    """The plugin id of a plugin source key, or None for host/other keys."""
    parsed = parse_source_key(key)
    if parsed is None or parsed.get("kind") != PLUGIN_PREFIX:
        return None
    return parsed.get("plugin_id")


__all__ = [
    "HOST_PREFIX",
    "MAX_CHARACTER_CHARS",
    "MAX_EVENT_ID_CHARS",
    "MAX_KEY_CHARS",
    "MAX_PLUGIN_ID_CHARS",
    "MAX_SESSION_ID_CHARS",
    "MAX_SUBJECT_CHARS",
    "PLUGIN_PREFIX",
    "build_host_source_key",
    "build_plugin_source_key",
    "is_valid_source_key",
    "normalize_local_event_id",
    "parse_source_key",
    "plugin_id_of",
    "sanitize_component",
]
