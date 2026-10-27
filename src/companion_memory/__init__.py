"""Public entry point; importing the package starts no services or workers."""

from .adapters.composition import MemoryCore, build_memory_core
from .adapters.durable import DurableEventStore, build_durable_event_store
from .api import MemoryEngine
from .contracts import (
    ContextBundle,
    ContextItem,
    ContextRequest,
    CorrectionReceipt,
    CorrectionRequest,
    DeletionReceipt,
    DeletionRequest,
    DeletionTarget,
    EventReceipt,
    JournalEntry,
    MemoryBlock,
    MemoryEvent,
    MentionDirective,
    PersonaProfile,
    PersonaSection,
    RecoveryResult,
    Scene,
    Scope,
    SentinelState,
)
from .domain.barrier import BarrierSnapshot, DeletionBarrier
from .domain.compression import CompressionService, CompressionSettings
from .domain.corrections import CorrectionService
from .domain.deletions import DeletionService, DeletionServices
from .domain.extraction import (
    ExtractionOutcome,
    ExtractionService,
    ExtractionSettings,
    IngestResult,
)
from .domain.maintenance import MaintenanceResult, MaintenanceService, MaintenanceSettings
from .domain.outbox_dispatch import OutboxDispatcher
from .domain.persona import PersonaDeletionCleaner, PersonaInvalidator, PersonaService
from .domain.recall import (
    RecallService,
    RecallSettings,
    bm25_scores,
    render_recall_block,
    rrf_fuse,
    tokenize,
)
from .domain.state import (
    StateCommitRequest,
    StateReadRequest,
    StateReceipt,
    StateService,
    StateSnapshot,
)
from .domain.reflection import (
    GrowthReceipt,
    PromotionOutcome,
    ReflectionDeletionCleaner,
    ReflectionInvalidator,
    ReflectionService,
    ReflectionSettings,
    SynthesisOutcome,
)
from .persistence.persona import PersonaStore
from .persistence.reflections import ReflectionStore
from .persistence.state import StateStore
from .ports import EmbeddingProvider, TokenCounter

__all__ = [
    "BarrierSnapshot",
    "ContextBundle",
    "ContextItem",
    "CompressionService",
    "CompressionSettings",
    "ContextRequest",
    "CorrectionReceipt",
    "CorrectionRequest",
    "CorrectionService",
    "DeletionBarrier",
    "DeletionReceipt",
    "DeletionRequest",
    "DeletionService",
    "DeletionServices",
    "DeletionTarget",
    "DurableEventStore",
    "EventReceipt",
    "ExtractionOutcome",
    "ExtractionService",
    "ExtractionSettings",
    "GrowthReceipt",
    "IngestResult",
    "PersonaDeletionCleaner",
    "PersonaInvalidator",
    "PersonaService",
    "PersonaStore",
    "PromotionOutcome",
    "RecallService",
    "RecallSettings",
    "ReflectionDeletionCleaner",
    "ReflectionInvalidator",
    "ReflectionService",
    "ReflectionSettings",
    "ReflectionStore",
    "SynthesisOutcome",
    "StateCommitRequest",
    "StateReadRequest",
    "StateReceipt",
    "StateService",
    "StateSnapshot",
    "StateStore",
    "MaintenanceResult",
    "MaintenanceService",
    "MaintenanceSettings",
    "OutboxDispatcher",
    "EmbeddingProvider",
    "TokenCounter",
    "bm25_scores",
    "JournalEntry",
    "MemoryBlock",
    "MemoryCore",
    "MemoryEngine",
    "MemoryEvent",
    "MentionDirective",
    "PersonaProfile",
    "PersonaSection",
    "RecoveryResult",
    "Scene",
    "Scope",
    "SentinelState",
    "build_durable_event_store",
    "build_memory_core",
    "render_recall_block",
    "rrf_fuse",
    "tokenize",
]