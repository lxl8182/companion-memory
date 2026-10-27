"""Persistent stores (B02): atomic files, schema gate, roots, permits, journal.

Every store here is synchronous (call through asyncio.to_thread), takes its
data root and write permit by injection, and raises the typed errors from
`rin_memory.errors` instead of guessing: corrupt content is never
rewritten into an empty store, and an unsupported schema is rejected before
any write.
"""

from .atomic import FileSystemAtomicStore
from .clock import SystemClock
from .corrections import CorrectionStore, PreparedCorrection
from .cursors import CursorStore
from .deletions import BarrierIndex, DeletionStore, PreparedRegistration
from .directives import UserDirectiveStore
from .facts_view import FactsView
from .gates import GateState, SuppressionGate
from .idempotency import IdempotencyStore, Reservation, key_digest
from .journal import (
    CORE_EVENT_TYPES,
    HOST_EVENT_TYPES,
    KNOWN_EVENT_TYPES,
    EventJournal,
)
from .outbox import OutboxOp, OutboxStore
from .outputs import OutputRecord, OutputStore
from .proactive import CandidateRecord, ProactiveStore
from .recent import RecentStore
from .permits import NullWritePermit, RejectingWritePermit
from .records import (
    canonical_json,
    event_fingerprint,
    event_from_snapshot,
    event_snapshot,
    fingerprint,
)
from .roots import FileSystemRoots, validate_character_name
from .schema import (
    SCHEMA_VERSION,
    load_legacy_list,
    load_versioned,
    require_objects,
    wrap,
)
from .source_ledger import LedgerEntry, SourceLedger
from .timeindex import TimeIndexStore
from .tombstones import ScopedTombstoneStore, TombstoneState
from .version_cache import VersionReader, file_signature
from .working_summary import WorkingSummaryStore

__all__ = [
    "BarrierIndex",
    "CORE_EVENT_TYPES",
    "CorrectionStore",
    "CursorStore",
    "DeletionStore",
    "PreparedCorrection",
    "PreparedRegistration",
    "UserDirectiveStore",
    "EventJournal",
    "FactsView",
    "GateState",
    "FileSystemAtomicStore",
    "FileSystemRoots",
    "HOST_EVENT_TYPES",
    "IdempotencyStore",
    "KNOWN_EVENT_TYPES",
    "LedgerEntry",
    "CandidateRecord",
    "NullWritePermit",
    "OutboxOp",
    "OutboxStore",
    "OutputRecord",
    "OutputStore",
    "ProactiveStore",
    "RecentStore",
    "RejectingWritePermit",
    "Reservation",
    "SCHEMA_VERSION",
    "ScopedTombstoneStore",
    "SourceLedger",
    "SuppressionGate",
    "SystemClock",
    "TimeIndexStore",
    "TombstoneState",
    "VersionReader",
    "WorkingSummaryStore",
    "canonical_json",
    "event_fingerprint",
    "event_from_snapshot",
    "event_snapshot",
    "file_signature",
    "fingerprint",
    "key_digest",
    "load_legacy_list",
    "load_versioned",
    "require_objects",
    "validate_character_name",
    "wrap",
]