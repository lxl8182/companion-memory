"""Frozen v1 contracts (design §17.9.3). Host wire formats stay in adapters."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping


@dataclass(frozen=True)
class Scene:
    plugin_id: str = ""
    game_id: str = ""
    session_id: str = ""


@dataclass(frozen=True)
class Scope:
    character: str
    subject: str = "private"

    def __post_init__(self) -> None:
        if not self.character.strip() or not self.subject.strip():
            raise ValueError("character and subject must not be empty")


@dataclass(frozen=True)
class MemoryEvent:
    scope: Scope
    source_key: str
    event_type: str
    text: str
    occurred_at: datetime
    scene: Scene = field(default_factory=Scene)
    applies_to: Scene = field(default_factory=Scene)
    # B02 additions (design §17.9.3). Defaults keep the B00 call sites valid.
    schema_version: int = 1
    received_at: datetime | None = None
    content_class: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    event_id: str | None = None
    episode_id: str | None = None

    def __post_init__(self) -> None:
        if not self.source_key.strip() or not self.event_type.strip():
            raise ValueError("source_key and event_type must not be empty")
        if self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        if self.schema_version < 1:
            raise ValueError("schema_version must be >= 1")
        if self.received_at is not None and self.received_at.utcoffset() is None:
            raise ValueError("received_at must include a timezone")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a mapping")


@dataclass(frozen=True)
class EventReceipt:
    """Result of `observe`. `durable` is true only after a persistent commit."""

    source_key: str
    status: Literal["accepted", "duplicate", "rejected", "conflict"]
    durable: bool
    event_id: str | None = None
    processing_position: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class JournalEntry:
    """One recovered journal record; `position` is its journal order."""

    position: int
    event_id: str
    event_type: str
    payload: Mapping[str, Any]
    recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.position < 0:
            raise ValueError("journal position must be nonnegative")
        if not self.event_id.strip() or not self.event_type.strip():
            raise ValueError("journal entry requires event_id and event_type")


@dataclass(frozen=True)
class SentinelState:
    """How far the journal has been replayed into views.

    `position == -1` means nothing has been applied yet (or the marker was
    lost), which replays the whole journal: handlers are idempotent, so
    over-replay is safe while under-replay would lose a transition.
    """

    position: int = -1
    event_id: str | None = None


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of one replay round for one character."""

    applied: int
    position: int
    paused: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class PersonaSection:
    section_id: str
    text: str
    subject: str | None = None
    applies_to: Scene = field(default_factory=Scene)
    evolution_enabled: bool = False


@dataclass(frozen=True)
class PersonaProfile:
    character: str
    revision: int
    sections: tuple[PersonaSection, ...]

    def __post_init__(self) -> None:
        if not self.character.strip() or self.revision < 1:
            raise ValueError("profile requires a character and revision >= 1")
        ids = [section.section_id for section in self.sections]
        if any(not item.strip() for item in ids) or len(ids) != len(set(ids)):
            raise ValueError("profile section IDs must be nonempty and unique")


@dataclass(frozen=True)
class ContextRequest:
    scope: Scope
    current_input: str
    scene: Scene = field(default_factory=Scene)
    purpose: Literal["chat", "decision", "proactive"] = "chat"
    budget_ms: int = 1000
    max_items: int = 4

    def __post_init__(self) -> None:
        if self.budget_ms <= 0 or self.max_items < 0:
            raise ValueError("budget_ms must be positive; max_items must be nonnegative")


@dataclass(frozen=True)
class ContextItem:
    scope: Scope
    source_key: str
    text: str
    kind: str
    applies_to: Scene = field(default_factory=Scene)


@dataclass(frozen=True)
class ContextBundle:
    scope: Scope
    persona: str
    persona_revision: int | None
    items: tuple[ContextItem, ...]
    recall_status: Literal["ready", "timed_out"] = "ready"


# -- B03: deletion barrier and corrections (design §17.9.1, §17.5.4) ----------


@dataclass(frozen=True)
class MemoryBlock:
    """The `{block, items, version}` triple shared by corrections/growth/state.

    A block is rendered text for prompt injection, `items` is the structured
    form a UI or adapter may consume, and `version` is the monotonic store
    version used for in-flight invalidation (a caller that read at version N
    must discard its result once the store moved past N).
    """

    block: str = ""
    items: tuple[Mapping[str, Any], ...] = ()
    version: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.block, str):
            raise ValueError("block must be a string")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValueError("block version must be a nonnegative integer")
        if not all(isinstance(item, Mapping) for item in self.items):
            raise ValueError("block items must be mappings")


#: Deletion target kinds; "episode" removes a whole experience.
DELETION_TARGET_TYPES: frozenset[str] = frozenset({"fact", "reflection", "persona", "episode"})
#: Host limits (memory/deletion_store.py): bounded request and receipt fields.
MAX_DELETION_TARGETS = 64
MAX_DELETION_SOURCE_KEYS = 64
MAX_DELETION_EPISODES = 16


@dataclass(frozen=True)
class DeletionTarget:
    """One entry the user asked to delete; `text_hash` adds content matching."""

    type: str
    id: str
    text_hash: str | None = None

    def __post_init__(self) -> None:
        if self.type not in DELETION_TARGET_TYPES:
            raise ValueError(f"unsupported deletion target type {self.type!r}")
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("deletion target id must be a nonempty string")
        if self.text_hash is not None:
            if not isinstance(self.text_hash, str) or len(self.text_hash) != 64:
                raise ValueError("text_hash must be a 64-character digest")

    def as_payload(self) -> dict[str, Any]:
        """Host-shaped target row (design §17.5.4 receipt contract)."""
        row: dict[str, Any] = {"type": self.type, "id": self.id}
        if self.text_hash:
            row["text_hash"] = self.text_hash
        return row


@dataclass(frozen=True)
class DeletionRequest:
    """User-initiated forget of entries (`entry`) or a whole experience (`episode`).

    The barrier is registered before any cleanup; the receipt reports
    `stopped_using` as soon as the barrier is durable and `content_cleaned`
    only after every host-side step finished. A missing plugin receipt keeps
    the whole deletion `pending` (design §17.5.4).
    """

    scope: Scope
    mode: Literal["entry", "episode"] = "entry"
    targets: tuple[DeletionTarget, ...] = ()
    source_keys: tuple[str, ...] = ()
    episode_ids: tuple[str, ...] = ()
    reason: str = ""
    request_id: str | None = None
    deletion_id: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in ("entry", "episode"):
            raise ValueError(f"unsupported deletion mode {self.mode!r}")
        if self.mode == "entry" and self.episode_ids:
            raise ValueError("entry deletion does not accept episode_ids; use mode='episode'")
        if not (self.targets or self.source_keys or self.episode_ids):
            raise ValueError("deletion needs at least one target, source key or episode")
        if len(self.targets) > MAX_DELETION_TARGETS:
            raise ValueError(f"at most {MAX_DELETION_TARGETS} targets per deletion")
        if len(self.source_keys) > MAX_DELETION_SOURCE_KEYS:
            raise ValueError(f"at most {MAX_DELETION_SOURCE_KEYS} source keys per deletion")
        if len(self.episode_ids) > MAX_DELETION_EPISODES:
            raise ValueError(f"at most {MAX_DELETION_EPISODES} episodes per deletion")
        for key in self.source_keys:
            if not isinstance(key, str) or not key.strip():
                raise ValueError("source keys must be nonempty strings")
        for episode_id in self.episode_ids:
            if not isinstance(episode_id, str) or not episode_id.strip():
                raise ValueError("episode ids must be nonempty strings")
        if self.deletion_id is not None and not self.deletion_id.strip():
            raise ValueError("deletion_id must be a nonempty string when given")


def _target_tuple(values: Any) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for item in values or ():
        if isinstance(item, Mapping):
            rows.append(dict(item))
    return tuple(rows)


@dataclass(frozen=True)
class DeletionReceipt:
    """Three-stage deletion receipt (design §17.5.4, host wire fields kept).

    `status` is pending/completed/failed; `completed`/`pending`/`failed`
    are step names, not contents. A plugin that was offline at deletion time
    keeps `plugin_receipts` empty and the deletion pending.
    """

    deletion_id: str
    version: int
    scope: Literal["entry", "episode"]
    status: Literal["pending", "completed", "failed"]
    stopped_using: bool
    content_cleaned: bool
    targets: tuple[Mapping[str, Any], ...] = ()
    resolved_targets: tuple[Mapping[str, Any], ...] = ()
    episode_ids: tuple[str, ...] = ()
    source_keys: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    created_at: str | None = None
    updated_at: str | None = None
    steps: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    plugin_receipts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    duplicate: bool = False
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not self.deletion_id.strip():
            raise ValueError("receipt requires a deletion_id")
        if self.scope not in ("entry", "episode"):
            raise ValueError(f"unsupported receipt scope {self.scope!r}")
        if self.status not in ("pending", "completed", "failed"):
            raise ValueError(f"unsupported receipt status {self.status!r}")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValueError("receipt version must be a nonnegative integer")

    @classmethod
    def from_mapping(
        cls, receipt: Mapping[str, Any], *, request_id: str | None = None,
    ) -> "DeletionReceipt":
        """Build a typed receipt from the host-shaped store projection."""
        return cls(
            deletion_id=str(receipt.get("deletion_id") or ""),
            version=int(receipt.get("version") or 0),
            scope=str(receipt.get("scope") or "entry"),
            status=str(receipt.get("status") or "pending"),
            stopped_using=bool(receipt.get("stopped_using")),
            content_cleaned=bool(receipt.get("content_cleaned")),
            targets=_target_tuple(receipt.get("targets")),
            resolved_targets=_target_tuple(receipt.get("resolved_targets")),
            episode_ids=tuple(receipt.get("episode_ids") or ()),
            source_keys=tuple(receipt.get("source_keys") or ()),
            completed=tuple(receipt.get("completed") or ()),
            pending=tuple(receipt.get("pending") or ()),
            failed=tuple(receipt.get("failed") or ()),
            created_at=receipt.get("created_at"),
            updated_at=receipt.get("updated_at"),
            steps={
                str(name): dict(step)
                for name, step in (receipt.get("steps") or {}).items()
                if isinstance(step, Mapping)
            },
            plugin_receipts={
                str(plugin_id): dict(value)
                for plugin_id, value in (receipt.get("plugin_receipts") or {}).items()
                if isinstance(value, Mapping)
            },
            duplicate=bool(receipt.get("duplicate")),
            request_id=request_id,
        )


@dataclass(frozen=True)
class CorrectionRequest:
    """Explicit user correction/preference; `replace` records a new belief.

    A `retract` only withdraws the correction block entry: invalidations that
    already stopped derived content keep applying (host behaviour, design
    §9.3/§17.9.1).
    """

    scope: Scope
    action: Literal["replace", "retract"] = "replace"
    target_fact_ids: tuple[str, ...] = ()
    target_source_keys: tuple[str, ...] = ()
    old_text: str = ""
    new_text: str = ""
    source_key: str = ""
    applies_to: Scene = field(default_factory=Scene)
    expires_at: str | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        if self.action not in ("replace", "retract"):
            raise ValueError(f"unsupported correction action {self.action!r}")
        if not (self.target_fact_ids or self.target_source_keys or self.old_text or self.new_text):
            raise ValueError("correction needs a target, old text or new text")
        if self.action == "retract" and not (
            self.target_fact_ids or self.target_source_keys or self.old_text
        ):
            raise ValueError("retract needs a target or the old text")
        if len(self.target_fact_ids) > 32 or len(self.target_source_keys) > 16:
            raise ValueError("too many correction targets")
        for value in self.target_fact_ids + self.target_source_keys:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("correction targets must be nonempty strings")
        if self.source_key and not self.source_key.strip():
            raise ValueError("source_key must be a nonempty string when given")


@dataclass(frozen=True)
class CorrectionReceipt:
    """Result of `correct`; the correction store version is monotonic."""

    correction_id: str
    version: int
    applied: bool
    duplicate: bool = False
    status: Literal["active", "retracted"] = "active"
    invalidated_fact_ids: tuple[str, ...] = ()
    invalidated_source_keys: tuple[str, ...] = ()
    invalidated_reflection_ids: tuple[str, ...] = ()
    invalidated_persona_ids: tuple[str, ...] = ()
    replacement_fact_id: str | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not self.correction_id.strip():
            raise ValueError("receipt requires a correction_id")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValueError("correction receipt version must be a nonnegative integer")


@dataclass(frozen=True)
class MentionDirective:
    """One persisted "do not mention X" directive (host `user_directives.json`)."""

    term: str
    kind: str = "ban_topic"
    locale: str = "und"
    created_at: float = 0.0
    last_seen_at: float = 0.0
    expire_at: float = 0.0
    hit_count: int = 1
    source: str = "regex"

    def is_active(self, now: float) -> bool:
        return self.expire_at > now
