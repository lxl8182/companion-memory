"""Ports the core needs from its host (design §17.9.2).

B02 freezes the storage-side ports. Each Protocol is the binding contract:
concrete implementations live in `companion_memory.persistence`, host
adapters live in the N.E.K.O repository, and the core never imports the host.
"""

from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Mapping, Protocol, Sequence

from .contracts import (
    ContextItem, ContextRequest, EventReceipt, JournalEntry, MemoryEvent,
    PersonaProfile, RecoveryResult, SentinelState,
)

#: Sync replay handler: `(character, payload) -> changed?`. It must load, apply
#: and persist its own view before returning; the sentinel advances only after
#: a successful return, and a crash in between replays the entry again.
JournalApplyHandler = Callable[[str, Mapping[str, Any]], bool]


class EventSink(Protocol):
    async def append(self, event: MemoryEvent) -> EventReceipt:
        """Accept once per scope/source; reject changed payloads; report durability."""
        ...


class ProfileReader(Protocol):
    async def get(self, character: str) -> PersonaProfile | None:
        """Return a complete immutable profile snapshot or an absent profile."""
        ...


class ContextProvider(Protocol):
    async def recall(self, request: ContextRequest) -> tuple[ContextItem, ...]:
        """Return scoped candidates; cancellation must release retrieval resources."""
        ...


class ModelInvoker(Protocol):
    """B04 model port: one bounded text completion, host owns the provider.

    `messages` is the OpenAI-style list the adapter maps onto the host's
    layered model config (tier + credentials stay outside the core). The core
    passes explicit budgets and a timeout on every call; cancellation must
    close the underlying client. A malformed provider reply is a normal
    `Exception` the caller classifies, not a return value.
    """

    async def invoke(
        self,
        tier: str,
        messages: Sequence[Mapping[str, str]],
        *,
        max_output_tokens: int,
        timeout_s: float,
        purpose: str,
    ) -> str:
        """Return the completion text; raise on transport/parse-adjacent errors."""
        ...


class Clock(Protocol):
    """Time source; tests inject a fake so timestamps and deadlines are exact."""

    def now(self) -> datetime:
        """Timezone-aware current instant (UTC)."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds, for budgets and deadlines."""
        ...


class StorageRoots(Protocol):
    """Resolved data roots. The core never guesses Documents or global config."""

    @property
    def memory_root(self) -> Path:
        """Root that holds one directory per character."""
        ...

    @property
    def local_state_root(self) -> Path:
        """Root for state that must not travel with a cloud snapshot."""
        ...

    def character_dir(self, character: str, *, create: bool = True) -> Path:
        """Return (and optionally create) the validated per-character directory."""
        ...


class AtomicJsonStore(Protocol):
    """Crash-safe file primitives: same-directory temp file + fsync + replace."""

    def load(self, path: Path) -> Any | None:
        """Parse a JSON file; None when absent; raise CorruptStoreError on bad content."""
        ...

    def save(self, path: Path, value: Any) -> None:
        """Atomically replace a JSON file; durable when this returns."""
        ...

    def save_text(self, path: Path, text: str) -> None:
        """Atomically replace a whole text file (journal compaction)."""
        ...

    def append_line(self, path: Path, line: str) -> None:
        """Append one newline-terminated line, flushed and fsynced."""
        ...


class WritePermit(Protocol):
    """Final fence before any persistent mutation (maintenance mode / save fence)."""

    def assert_writable(self, operation: str, target: str) -> None:
        """Raise WriteRejected when the write must not happen."""
        ...

    def transaction(self, operation: str, target: str) -> Any:
        """Context manager wrapping a multi-file commit under the same permit."""
        ...


class EventJournal(Protocol):
    """Per-character append-only journal plus replay-into-views recovery."""

    def register_handler(self, event_type: str, handler: JournalApplyHandler) -> None:
        """Register the idempotent replay handler; must happen before writes."""
        ...

    def append(
        self, character: str, event_type: str, payload: Mapping[str, Any],
    ) -> JournalEntry:
        """Append + fsync one entry. Does not advance the sentinel by itself."""
        ...

    def record_and_save(
        self,
        character: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        load_view: Callable[[str], Any],
        mutate_view: Callable[[Any], None],
        save_view: Callable[[str, Any], None],
        validate_view: Callable[[Any], None] | None = None,
    ) -> JournalEntry:
        """load view -> validate -> append -> mutate -> save -> advance sentinel."""
        ...

    def read_since(self, character: str, position: int) -> tuple[JournalEntry, ...]:
        """Entries with `position` greater than the argument, in journal order."""
        ...

    def read_sentinel(self, character: str) -> SentinelState:
        """Last applied position; -1 (replay everything) when unknown."""
        ...

    def reconcile(self, character: str) -> RecoveryResult:
        """Replay the unapplied tail; stop at an unregistered type or a failing handler."""
        ...

    def compact_if_needed(
        self,
        character: str,
        seed_events: Callable[[], Sequence[tuple[str, Mapping[str, Any]]]],
    ) -> int:
        """Rewrite the body from snapshot seeds when thresholds are exceeded."""
        ...


class OutboxStore(Protocol):
    """At-least-once queue for work that must survive a restart (ops are idempotent)."""

    def enqueue(self, character: str, op_type: str, payload: Mapping[str, Any]) -> str:
        """Append a pending op and return its op_id."""
        ...

    def pending(self, character: str, *, limit: int = 20) -> tuple[Any, ...]:
        """Pending ops in append order, oldest first."""
        ...

    def complete(self, character: str, op_id: str) -> None:
        """Mark done; the op never replays again."""
        ...

    def fail(self, character: str, op_id: str, *, error: str) -> int:
        """Record a failed attempt and return the new attempt count."""
        ...


class CursorStore(Protocol):
    """Per-character named ISO-8601 cursors; a missing key means never processed."""

    def get(self, character: str, key: str) -> datetime | None:
        ...

    def set(self, character: str, key: str, value: datetime) -> None:
        ...

    def snapshot(self, character: str) -> Mapping[str, datetime]:
        ...


# -- B06: optional vector retrieval and token accounting ----------------------


class TokenCounter(Protocol):
    """Token accounting injected into render/recall budgets (B06).

    The core has no tokenizer dependency of its own: the host adapter wraps
    tiktoken (or any compatible counter) and must return the same identity
    string for the lifetime of one encoding. `truncate` never returns more
    than `max_tokens` tokens and may return a shorter string when a BPE merge
    would split a character.
    """

    def count(self, text: str) -> int:
        """Number of tokens in `text` (0 for empty)."""
        ...

    async def acount(self, text: str) -> int:
        """Async twin of :meth:`count` for the render path."""
        ...

    def truncate(self, text: str, max_tokens: int) -> str:
        """Return `text` truncated to at most `max_tokens` tokens."""
        ...

    def identity(self) -> str:
        """Stable counter fingerprint used in token-cache keys."""
        ...


class EmbeddingProvider(Protocol):
    """Optional local embedding adapter used by recall (B06).

    No implementation lives in the core. `is_available()` is the only gate:
    when it returns False, or `model_id()` is None, recall runs BM25-only and
    never raises. `embed_batch` preserves positional alignment with the input
    list; a failed slot is `None` rather than an exception.
    """

    def is_available(self) -> bool:
        """True only when `embed_batch` would return at least one vector."""
        ...

    def model_id(self) -> str | None:
        """Opaque embedding-space id (dimension/quantization included)."""
        ...

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float] | None]:
        """One vector (or None) per input text, in the same order."""
        ...
