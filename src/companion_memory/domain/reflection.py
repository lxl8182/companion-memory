"""B05 reflection, evidence, promotion and growth domain service.

This module owns the host's reflection lifecycle on top of the B05 stores
(`reflections.json`, `surfaced.json`, `synth_backoff.json`,
`growth_uses.json`, `reflection_archive/`) and the B04 fact view:

* synthesis (facts -> pending reflection, optional growth candidate) through
  the injected `ModelInvoker` with explicit tier/budget/timeout;
* read-time evidence decay (reinforcement half-life 30 days, disputation 180
  days, `protected=True` -> `inf`) and daily `sub_zero_days` accounting;
* promotion: score-driven (1.0 / 2.0), time-driven when `powerful_memory`
  is off (7 + 7 days, no model) and growth (min support + evidence flag);
* surfaced/feedback recording with the host's 30 minute cooldown;
* archive sweep (terminal age + score-driven sub-zero days);
* the B03 deletion cleaner and correction invalidator (registered by the
  composition root);
* replay handlers for `reflection.evidence_updated` (full-snapshot whitelist)
  and `reflection.state_changed` (CAS on `from`); a raising handler pauses
  the reconciler so the sentinel cannot skip a lost mutation.

Safety rules kept from the host spec (B05 spec section 6.1):

* reflection ids are deterministic: `ref_<sha256(sorted(source_fact_ids))[:16]>`
  (the host appends `\\x00` between ids), so re-synthesizing the same batch is
  idempotent;
* the synthesis backoff key is the *full* unabsorbed id set, not the capped
  batch id, so a poisoned batch cannot starve new facts;
* `withdrawn` is not a terminal/hidden status: a corrected reflection stays
  readable as a row but no longer renders or promotes;
* before any model-backed write the barrier snapshot is re-checked and a stale
  result is discarded (never written);
* every reflection write passes the deletion barrier first.

Deliberate deviations from the host, recorded for B05 hand-off:

* promotion does not call the host merge-decision LLM. The frozen
  `PersonaService` surface is `add_reflection/add_growth` only, so a
  deterministic write is used; the model-failure "never downgrade to
  promote_fresh" rule is therefore trivially held (no model is consulted);
* a growth candidate is stored on its own reflection row (id = `ref_...`),
  merged across batches by `growth_key`; the host stores a separate
  `grow_...` row. Field names and merge semantics are the host's.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from ..contracts import MemoryBlock
from ..domain.barrier import DeletionBarrier, episode_ids_of
from ..errors import BarrierStaleError, ContentBlockedError
from ..invalidation import reflection_invalidated, row_matches_subject, text_hash
from ..persistence.facts_view import FactsView
from ..persistence.journal import EventJournal
from ..persistence.reflections import ReflectionStore
from ..ports import Clock, ModelInvoker, StorageRoots, WritePermit

# -- event vocabulary (host memory/event_log.py names, persisted identifiers) --

EVT_REFLECTION_EVIDENCE_UPDATED = "reflection.evidence_updated"
EVT_REFLECTION_STATE_CHANGED = "reflection.state_changed"
REFLECTION_EVENT_TYPES: tuple[str, ...] = (
    EVT_REFLECTION_EVIDENCE_UPDATED,
    EVT_REFLECTION_STATE_CHANGED,
)

#: Full-snapshot keys copied by the evidence replay handler (host
#: memory/evidence_handlers.py:48-68). Kept verbatim: replaying a subset would
#: desynchronise the evidence view from the audit log.
EVIDENCE_SNAPSHOT_KEYS: tuple[str, ...] = (
    "reinforcement",
    "disputation",
    "rein_last_signal_at",
    "disp_last_signal_at",
    "sub_zero_days",
    "sub_zero_last_increment_date",
    "user_fact_reinforce_count",
    "last_promote_attempt_at",
    "promote_attempt_count",
)

#: Hidden statuses (host _shared.REFLECTION_TERMINAL_STATUSES). `withdrawn`
#: is deliberately absent: a withdrawn row is still stored but not rendered.
REFLECTION_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "promoted", "denied", "archived", "merged", "promote_blocked",
})
REFLECTION_ACTIVE_STATUSES: tuple[str, ...] = ("pending", "confirmed")
REFLECTION_STATUSES: frozenset[str] = frozenset({
    "pending", "confirmed", "promoted", "denied", "archived", "merged",
    "promote_blocked", "withdrawn",
})

MEMORY_KINDS: tuple[str, ...] = ("habit", "interest", "experience", "relationship")
EVIDENCE_FLAGS: tuple[str, ...] = (
    "actual_result", "user_feedback", "both_party", "real_contact",
)
GROWTH_TERMINAL_STATUSES: frozenset[str] = frozenset({"withdrawn", "superseded"})
GROWTH_ACTIVE_STATUSES: frozenset[str] = frozenset({"pending", "confirmed", "promoted"})
USAGE_TYPES: tuple[str, ...] = ("read", "adopted", "result", "feedback")

#: Growth promotion gate: independent experiences + at least one evidence flag
#: of the matching class (host memory/growth.py:122-128).
PROMOTION_RULES: dict[str, tuple[str, ...]] = {
    "habit": ("actual_result", "user_feedback"),
    "experience": ("actual_result", "user_feedback"),
    "interest": ("real_contact",),
    "relationship": ("both_party",),
}

#: Host evidence constants (config/memory_settings.py:30-56,1014-1021).
EVIDENCE_REIN_HALF_LIFE_DAYS = 30.0
EVIDENCE_DISP_HALF_LIFE_DAYS = 180.0
USER_CONFIRM_DELTA = 1.0
USER_REBUT_DELTA = 1.0
IGNORED_REINFORCEMENT_DELTA = -0.2
#: Host initial reinforcement seeds by MAX source-fact importance.
INITIAL_REINFORCEMENT: tuple[tuple[int, float], ...] = (
    (10, 0.8), (9, 0.6), (8, 0.4), (7, 0.2),
)

MAX_REFLECTION_TEXT = 600
MAX_CLAIM_CHARS = 200
MAX_SITUATION_CHARS = 240
MAX_SOURCE_KEYS = 32
MAX_GROWTH_ITEMS = 6
GROWTH_BLOCK_MAX_TOKENS = 200
MAX_GROWTH_USES = 2000
SHARED_SCOPE_VALUES = frozenset({"shared", "global", "all"})
_SCENE_KEYS = ("plugin_id", "game_id", "session_id")

_FENCE = chr(96) * 3


# -- settings and outcomes ---------------------------------------------------


@dataclass(frozen=True)
class ReflectionSettings:
    """B05 policy; defaults mirror the host (B05 spec sections 1, 3, 6.1)."""

    min_facts_for_reflection: int = 5
    synthesis_facts_max: int = 20
    cooldown_minutes: int = 30
    confirmed_threshold: float = 1.0
    promoted_threshold: float = 2.0
    time_confirm_days: float = 7.0
    time_promote_days: float = 7.0
    max_synthesis_attempts: int = 5
    dead_letter_self_heal_seconds: float = 5 * 3600.0
    archive_after_days: float = 30.0
    sub_zero_archive_days: int = 14
    render_max_tokens: int = 2000
    growth_min_support: int = 2
    synthesis_output_tokens: int = 4096
    synthesis_timeout_s: float = 110.0
    model_tier: str = "summary"
    powerful_memory: bool = True


@dataclass(frozen=True)
class SynthesisOutcome:
    """One synthesis attempt.

    `status` is one of `completed`, `duplicate`, `skipped`, `failed`,
    `stale`. A failed attempt increments the full-set backoff and never writes
    a reflection; `stale` means the deletion/correction barrier moved while the
    model was running and the result was discarded.
    """

    status: str
    reflection_ids: tuple[str, ...] = ()
    absorbed_fact_ids: tuple[str, ...] = ()
    growth_item_ids: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class PromotionOutcome:
    """One promotion pass (score/time/growth paths share it)."""

    status: str
    promoted_ids: tuple[str, ...] = ()
    confirmed_ids: tuple[str, ...] = ()
    persona_ids: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class GrowthReceipt:
    """Result of recording one growth usage; version is the growth version."""

    item_id: str
    version: int
    duplicate: bool


# -- pure helpers ------------------------------------------------------------


def _now_iso(now: datetime) -> str:
    return now.isoformat()


def _parse_iso(value: Any, now: datetime | None = None) -> datetime | None:
    """Parse an ISO timestamp; naive values inherit `now`'s timezone."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        tz = now.tzinfo if now is not None and now.tzinfo is not None else timezone.utc
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def _age_days(value: Any, now: datetime) -> float:
    parsed = _parse_iso(value, now)
    if parsed is None:
        return 0.0
    delta = (now - parsed).total_seconds()
    if delta <= 0:
        return 0.0
    return delta / 86400.0


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bounded(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _normalize_claim(value: Any) -> str:
    text = _bounded(value, MAX_CLAIM_CHARS)
    return " ".join(text.split())


def _normalize_situation(value: Any) -> str:
    text = _bounded(value, MAX_SITUATION_CHARS)
    return " ".join(text.split())


def _normalize_scene(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for key in _SCENE_KEYS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            out[key] = item.strip()[:64]
    scope = value.get("scope")
    if isinstance(scope, str) and scope.strip().lower() in SHARED_SCOPE_VALUES:
        out["scope"] = scope.strip().lower()
    return out


def _entry_applies_to_scene(entry: Any, scene: Any) -> bool:
    """Host `entry_applies_to_scene`: empty/shared applies everywhere."""
    if not isinstance(entry, Mapping):
        return False
    restriction = entry.get("applies_to")
    if not isinstance(restriction, Mapping):
        restriction = entry.get("source_scene")
    if not isinstance(restriction, Mapping) or not restriction:
        return True
    scope = restriction.get("scope")
    if isinstance(scope, str) and scope.strip().lower() in SHARED_SCOPE_VALUES:
        return True
    entry_scope = entry.get("scope")
    if isinstance(entry_scope, str) and entry_scope.strip().lower() in SHARED_SCOPE_VALUES:
        return True
    current = _normalize_scene(scene)
    if not current:
        return False
    for key in _SCENE_KEYS:
        wanted = restriction.get(key)
        if not isinstance(wanted, str) or not wanted.strip():
            continue
        if current.get(key) != wanted.strip()[:64]:
            return False
    return True


def reflection_id_from_facts(source_fact_ids: Sequence[str]) -> str:
    """Deterministic host reflection id: `ref_<sha256(sorted ids + NUL)>[:16]`."""
    digest = hashlib.sha256()
    for fact_id in sorted(str(item) for item in source_fact_ids):
        digest.update(fact_id.encode("utf-8"))
        digest.update(b"\x00")
    return "ref_" + digest.hexdigest()[:16]


def growth_key(
    kind: Any, claim: Any, situation: Any = "", applies_to: Any = None,
) -> str:
    """Host growth key: sha1(kind | normalized claim | situation | scene)."""
    scene = _normalize_scene(applies_to)
    seed = json.dumps(
        {
            "kind": _bounded(kind, 32).lower(),
            "claim": _normalize_claim(claim),
            "situation": _normalize_situation(situation),
            "scene": {key: scene[key] for key in sorted(scene)},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "grow_" + hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:16]


def _initial_reinforcement(importance: Any) -> float:
    value = _as_int(importance, 5)
    for threshold, seed in INITIAL_REINFORCEMENT:
        if value >= threshold:
            return seed
    return 0.0


def _importance_of(fact: Mapping[str, Any]) -> int:
    value = fact.get("importance")
    if isinstance(value, bool) or not isinstance(value, int):
        value = 5
    return max(1, min(10, value))


def _parse_json_object(raw: Any) -> dict[str, Any] | None:
    """Parse a model JSON object; tolerate a fenced code block."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith(_FENCE):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith(_FENCE):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return dict(data) if isinstance(data, Mapping) else None


def _find_row(rows: Sequence[Mapping[str, Any]], row_id: Any) -> dict[str, Any] | None:
    if not isinstance(row_id, str) or not row_id:
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("id") == row_id:
            return row
    return None


def _row_source_keys(row: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field_name in ("source_event_keys", "source_keys"):
        value = row.get(field_name)
        if isinstance(value, str) and value.strip():
            keys.add(value.strip())
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    keys.add(item.strip())
    return keys


def _apply_support_removal(
    row: dict[str, Any], invalidated_keys: set[str], *, now_iso: str,
) -> bool:
    """Host `apply_support_removal`: drop lost support, withdraw/narrow."""
    if not invalidated_keys:
        return False
    support = [
        str(key) for key in row.get("support_event_keys") or ()
        if isinstance(key, str) and key
    ]
    kept = [key for key in support if key not in invalidated_keys]
    if len(kept) == len(support):
        return False
    removed = {key for key in support if key in invalidated_keys}
    row["support_event_keys"] = kept
    row["support_count"] = len(kept)
    evidence = row.get("support_evidence")
    buckets: dict[str, list[str]] = {}
    if isinstance(evidence, Mapping):
        for flag in EVIDENCE_FLAGS:
            buckets[flag] = [
                key for key in evidence.get(flag) or ()
                if isinstance(key, str) and key not in removed
            ]
    else:
        buckets = {flag: [] for flag in EVIDENCE_FLAGS}
    row["support_evidence"] = buckets
    row["source_event_keys"] = [
        key for key in row.get("source_event_keys") or ()
        if isinstance(key, str) and key not in removed
    ]
    row["updated_at"] = now_iso
    min_support = 2
    status = str(row.get("status") or "pending")
    if not kept:
        row["status"] = "withdrawn"
        row["withdrawn_at"] = now_iso
        row["withdrawn_reason"] = "sources_deleted"
    elif len(kept) < min_support and status in ("confirmed", "promoted", "narrowed"):
        row["status"] = "narrowed"
        row["narrowed_at"] = now_iso
        row["narrowed_reason"] = "sources_reduced"
    return True


# -- service -----------------------------------------------------------------


class ReflectionService:
    """Reflection/evidence/growth entry points over one character directory.

    The constructor is the frozen composition contract (adapters/composition.py);
    `persona` is the B05 PersonaService or `None` when the persona store is not
    wired (promotion then reports `blocked/persona_unavailable` instead of
    pretending success).
    """

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: ReflectionStore,
        facts: FactsView,
        barrier: DeletionBarrier,
        journal: EventJournal,
        clock: Clock,
        permit: WritePermit,
        model: ModelInvoker | None = None,
        settings: ReflectionSettings | None = None,
        persona: Any = None,
    ) -> None:
        self._roots = roots
        self._store = store
        self._facts = facts
        self._barrier = barrier
        self._journal = journal
        self._clock = clock
        self._permit = permit
        self._model = model
        self._settings = settings or ReflectionSettings()
        self._persona = persona
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        # Replay handlers must be registered before any business write
        # (B05 spec section 5.3); the journal refuses unhandled event types.
        self._journal.register_handler(
            EVT_REFLECTION_EVIDENCE_UPDATED, self._apply_evidence_updated,
        )
        self._journal.register_handler(
            EVT_REFLECTION_STATE_CHANGED, self._apply_state_changed,
        )

    # -- wiring ---------------------------------------------------------

    @property
    def settings(self) -> ReflectionSettings:
        return self._settings

    def load(self, character: str) -> list[dict[str, Any]]:
        """Every stored row, terminal statuses included (test/inspection hook)."""
        return self._store.load(self._character(character))

    def _lock_for(self, character: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(character)
            if lock is None:
                lock = threading.RLock()
                self._locks[character] = lock
            return lock

    @staticmethod
    def _character(character: Any) -> str:
        value = str(character or "").strip()
        if not value:
            raise ValueError("character must be nonempty")
        return value

    # -- evidence -------------------------------------------------------

    def evidence_score(self, character: str, entry: Mapping[str, Any]) -> float:
        """Read-time decayed score (+rein -disp); `protected` returns inf.

        `sub_zero_days` is maintained by :meth:`archive` (at most one
        increment per calendar day), not computed here.
        """
        self._character(character)
        if not isinstance(entry, Mapping):
            return 0.0
        if entry.get("protected"):
            return float("inf")
        now = self._clock.now()
        reinforcement = _as_float(entry.get("reinforcement"), 0.0)
        disputation = _as_float(entry.get("disputation"), 0.0)
        if reinforcement != 0.0:
            reinforcement *= 0.5 ** (
                _age_days(entry.get("rein_last_signal_at"), now)
                / EVIDENCE_REIN_HALF_LIFE_DAYS
            )
        if disputation != 0.0:
            disputation *= 0.5 ** (
                _age_days(entry.get("disp_last_signal_at"), now)
                / EVIDENCE_DISP_HALF_LIFE_DAYS
            )
        return reinforcement - disputation

    # -- visible / block ------------------------------------------------

    def visible(
        self,
        character: str,
        *,
        subject: Any = None,
        scene: Any = None,
    ) -> list[dict[str, Any]]:
        """Renderable rows: hidden terminal states, barrier and invalid rows out."""
        character = self._character(character)
        rows = self._store.load(character)
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("status") or "") in REFLECTION_TERMINAL_STATUSES:
                continue
            if row.get("needs_rebuild"):
                continue
            if subject is not None and not row_matches_subject(row, subject):
                continue
            if scene is not None and not _entry_applies_to_scene(row, scene):
                continue
            if self._barrier.blocks_row(character, row):
                continue
            if reflection_invalidated(row):
                continue
            out.append(dict(row))
        return out

    def block(
        self,
        character: str,
        *,
        subject: Any = None,
        scene: Any = None,
        language: str = "zh",
        limit_tokens: int | None = None,
    ) -> MemoryBlock:
        """Pending+confirmed rows rendered to the host reflection budget."""
        rows = [
            row for row in self.visible(character, subject=subject, scene=scene)
            if str(row.get("status") or "") in REFLECTION_ACTIVE_STATUSES
        ]
        now = self._clock.now()
        rows.sort(
            key=lambda row: (
                -self.evidence_score(character, row),
                str(row.get("created_at") or ""),
            ),
        )
        budget = self._settings.render_max_tokens if limit_tokens is None else int(limit_tokens)
        lines: list[str] = []
        used = 0
        for row in rows:
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            cost = max(1, len(text) // 2)
            if used + cost > budget:
                break
            lines.append("- " + text)
            used += cost
        del language  # host rendering is plain text lines; label stays external
        return MemoryBlock(block="\n".join(lines), items=tuple(rows), version=0)

    # -- synthesis ------------------------------------------------------

    async def synthesize(self, character: str) -> SynthesisOutcome:
        """Synthesize one reflection from the unabsorbed facts, if due."""
        character = self._character(character)
        settings = self._settings
        with self._lock_for(character):
            unabsorbed = self._unabsorbed_facts(character)
            if len(unabsorbed) < settings.min_facts_for_reflection:
                return SynthesisOutcome("skipped", reason="insufficient_facts")
            all_ids = sorted(str(fact["id"]) for fact in unabsorbed)
            backoff_key = reflection_id_from_facts(all_ids)
            backoff = self._store.load_backoff(character)
            entry = backoff.get(backoff_key) or {}
            attempts = _as_int(entry.get("n"), 0)
            if (
                attempts >= settings.max_synthesis_attempts
                and not self._dead_letter_healed(entry)
            ):
                return SynthesisOutcome("skipped", reason="dead_letter")
            capped = self._cap_facts(unabsorbed)
            source_fact_ids = sorted(str(fact["id"]) for fact in capped)
            reflection_id = reflection_id_from_facts(source_fact_ids)
            rows = self._store.load(character)
            existing = _find_row(rows, reflection_id)
            if existing is not None:
                absorbed = self._mark_absorbed(character, source_fact_ids)
                self._clear_backoff(character, backoff_key)
                growth_ids = (
                    (str(existing.get("id")),)
                    if existing.get("memory_kind") in MEMORY_KINDS else ()
                )
                return SynthesisOutcome(
                    "duplicate", reflection_ids=(reflection_id,),
                    absorbed_fact_ids=tuple(absorbed),
                    growth_item_ids=growth_ids,
                    reason="already_synthesized",
                )
            if self._model is None:
                self._bump_backoff(character, backoff_key)
                return SynthesisOutcome("failed", reason="model_unavailable")
            snapshot = self._barrier.snapshot(character)
            prompt = _build_synthesis_prompt(capped)
        try:
            raw = await self._model.invoke(
                settings.model_tier,
                [{"role": "system", "content": prompt}],
                max_output_tokens=settings.synthesis_output_tokens,
                timeout_s=settings.synthesis_timeout_s,
                purpose="reflection_synthesis",
            )
        except Exception as exc:  # noqa: BLE001 - transport failure is retryable
            self._bump_backoff(character, backoff_key)
            return SynthesisOutcome(
                "failed", reason=f"transport:{type(exc).__name__}",
            )
        parsed = _parse_json_object(raw)
        if parsed is None:
            self._bump_backoff(character, backoff_key)
            return SynthesisOutcome("failed", reason="parse")
        text = parsed.get("reflection")
        if not isinstance(text, str) or not text.strip():
            self._bump_backoff(character, backoff_key)
            return SynthesisOutcome("failed", reason="empty_reflection")
        text = text.strip()[:MAX_REFLECTION_TEXT]

        with self._lock_for(character):
            try:
                self._barrier.assert_fresh(character, snapshot)
            except BarrierStaleError:
                return SynthesisOutcome("stale", reason="barrier_stale")
            rows = self._store.load(character)
            existing = _find_row(rows, reflection_id)
            if existing is not None:
                absorbed = self._mark_absorbed(character, source_fact_ids)
                self._clear_backoff(character, backoff_key)
                return SynthesisOutcome(
                    "duplicate", reflection_ids=(reflection_id,),
                    absorbed_fact_ids=tuple(absorbed), reason="already_synthesized",
                )
            # The model may have rewritten the batch; only facts that still
            # exist and are not tombstoned may become sources.
            current = {
                str(fact.get("id")): fact for fact in self._facts.load(character)
                if isinstance(fact, dict) and isinstance(fact.get("id"), str)
            }
            usable = [
                fact_id for fact_id in source_fact_ids
                if fact_id in current
                and not self._barrier.blocks_row(character, current[fact_id])
            ]
            if len(usable) < settings.min_facts_for_reflection:
                return SynthesisOutcome("skipped", reason="sources_deleted")
            row = self._new_reflection_row(
                reflection_id, usable, capped, parsed, text,
            )
            try:
                self._barrier.assert_row_allowed(character, row)
            except ContentBlockedError:
                return SynthesisOutcome("stale", reason="blocked_row")
            growth_ids: tuple[str, ...] = ()
            candidate = self._growth_candidate(parsed, usable, capped, text)
            appended = False
            if candidate is not None:
                growth_ids, appended = self._merge_or_append_growth(
                    character, rows, row, candidate,
                )
            else:
                rows.append(row)
                appended = True
            self._store.save(character, rows)
            absorbed = self._mark_absorbed(character, usable)
            self._clear_backoff(character, backoff_key)
            return SynthesisOutcome(
                "completed",
                reflection_ids=(reflection_id,) if appended else (),
                absorbed_fact_ids=tuple(absorbed),
                growth_item_ids=growth_ids,
            )

    def _unabsorbed_facts(self, character: str) -> list[dict[str, Any]]:
        facts = self._facts.load(character)
        out: list[dict[str, Any]] = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            fact_id = fact.get("id")
            if not isinstance(fact_id, str) or not fact_id:
                continue
            if fact.get("absorbed"):
                continue
            if self._barrier.blocks_row(character, fact):
                continue
            if self._barrier.blocks_target(character, "fact", fact_id):
                continue
            out.append(fact)
        return out

    def _cap_facts(self, facts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(
            facts,
            key=lambda fact: (
                -_importance_of(fact),
                str(fact.get("created_at") or ""),
            ),
        )
        return [dict(fact) for fact in ordered[: self._settings.synthesis_facts_max]]

    def _new_reflection_row(
        self,
        reflection_id: str,
        source_fact_ids: Sequence[str],
        facts: Sequence[Mapping[str, Any]],
        parsed: Mapping[str, Any],
        text: str,
    ) -> dict[str, Any]:
        now = self._clock.now()
        now_iso = _now_iso(now)
        max_importance = max(
            (_importance_of(fact) for fact in facts), default=5,
        )
        initial = _initial_reinforcement(max_importance)
        entity = parsed.get("entity")
        if entity not in ("master", "neko", "relationship"):
            entity = "relationship"
        relation_type = parsed.get("relation_type")
        if not isinstance(relation_type, str):
            relation_type = None
        temporal_scope = parsed.get("temporal_scope")
        if not isinstance(temporal_scope, str):
            temporal_scope = None
        subject = parsed.get("subject")
        if not isinstance(subject, str):
            subject = None
        row: dict[str, Any] = {
            "id": reflection_id,
            "text": text,
            "entity": entity,
            "status": "pending",
            "source_fact_ids": list(source_fact_ids),
            "created_at": now_iso,
            "feedback": None,
            "next_eligible_at": (
                now + timedelta(minutes=self._settings.cooldown_minutes)
            ).isoformat(),
            "reinforcement": initial,
            "rein_last_signal_at": now_iso if initial > 0 else None,
            "disputation": 0.0,
            "disp_last_signal_at": None,
            "sub_zero_days": 0,
            "sub_zero_last_increment_date": None,
            "user_fact_reinforce_count": 0,
            "absorbed_into": None,
            "last_promote_attempt_at": None,
            "promote_attempt_count": 0,
            "promote_blocked_reason": None,
            "recent_mentions": [],
            "suppress": False,
            "suppressed_at": None,
            "protected": False,
            "relation_type": relation_type,
            "temporal_scope": temporal_scope,
            "subject": subject,
            "event_when_raw": None,
            "event_start_at": now_iso,
            "event_end_at": None,
            "schema_version": 2,
            "embedding": None,
            "embedding_text_sha256": None,
            "embedding_model_id": None,
            "last_refine_cluster_hash": None,
            "last_refine_at": None,
        }
        for key in (
            "speaker_id", "speaker_trust", "speaker_label",
            "speaker_provenance_mixed",
        ):
            for fact in facts:
                value = fact.get(key)
                if value is not None:
                    row[key] = value
                    break
        return row

    def _growth_candidate(
        self,
        parsed: Mapping[str, Any],
        source_fact_ids: Sequence[str],
        facts: Sequence[Mapping[str, Any]],
        text: str,
    ) -> dict[str, Any] | None:
        raw_kind = parsed.get("memory_kind")
        kind = raw_kind.strip().lower() if isinstance(raw_kind, str) else ""
        if kind not in MEMORY_KINDS:
            return None
        claim = _normalize_claim(parsed.get("claim")) or _normalize_claim(text)
        if not claim:
            return None
        situation = _normalize_situation(parsed.get("situation"))
        raw_scene = parsed.get("applies_to")
        if isinstance(raw_scene, Mapping):
            applies_to = _normalize_scene(raw_scene)
        else:
            applies_to = {}
            for fact in facts:
                scene = fact.get("applies_to")
                if isinstance(scene, Mapping) and scene:
                    applies_to = _normalize_scene(scene)
                    break
        keys: list[str] = []
        for fact in facts:
            for key in fact.get("source_event_keys") or ():
                if isinstance(key, str) and key.strip():
                    key = key.strip()[:600]
                    if key not in keys and len(keys) < MAX_SOURCE_KEYS:
                        keys.append(key)
        return {
            "kind": kind,
            "claim": claim,
            "situation": situation,
            "applies_to": applies_to,
            "growth_key": growth_key(kind, claim, situation, applies_to),
            "source_event_keys": keys,
            "source_fact_ids": [str(item) for item in source_fact_ids],
        }

    def _merge_or_append_growth(
        self,
        character: str,
        rows: list[dict[str, Any]],
        row: dict[str, Any],
        candidate: Mapping[str, Any],
    ) -> tuple[tuple[str, ...], bool]:
        """Merge into an existing growth row (same key) or append the new row."""
        key = str(candidate["growth_key"])
        target: dict[str, Any] | None = None
        for item in rows:
            if (
                isinstance(item, dict)
                and item.get("memory_kind") in MEMORY_KINDS
                and item.get("growth_key") == key
                and str(item.get("status") or "") not in GROWTH_TERMINAL_STATUSES
            ):
                target = item
                break
        now_iso = _now_iso(self._clock.now())
        keys = list(candidate.get("source_event_keys") or ())
        if target is None:
            support = list(keys)
            row["memory_kind"] = candidate["kind"]
            row["claim"] = candidate["claim"]
            row["situation"] = candidate["situation"]
            row["applies_to"] = dict(candidate.get("applies_to") or {})
            row["growth_key"] = key
            row["support_event_keys"] = support
            row["support_count"] = len(support)
            row["support_evidence"] = {
                flag: (list(support) if flag == "actual_result" else [])
                for flag in EVIDENCE_FLAGS
            }
            row["source_event_keys"] = list(support)
            row["valid_from"] = now_iso
            row["superseded_by"] = None
            rows.append(row)
            return (str(row["id"]),), True
        # Existing independent experience: only new source keys add support.
        support = [
            str(item) for item in target.get("support_event_keys") or ()
            if isinstance(item, str)
        ]
        added = [key for key in keys if key not in support][: MAX_SOURCE_KEYS - len(support)]
        if added:
            support.extend(added)
            target["support_event_keys"] = support[:MAX_SOURCE_KEYS]
            target["support_count"] = len(target["support_event_keys"])
            evidence = target.get("support_evidence")
            buckets: dict[str, list[str]] = {}
            if isinstance(evidence, Mapping):
                for flag in EVIDENCE_FLAGS:
                    buckets[flag] = [
                        str(item) for item in evidence.get(flag) or ()
                        if isinstance(item, str)
                    ]
            else:
                buckets = {flag: [] for flag in EVIDENCE_FLAGS}
            for key in added:
                if key not in buckets["actual_result"]:
                    buckets["actual_result"].append(key)
            target["support_evidence"] = buckets
            source_keys = [
                str(item) for item in target.get("source_event_keys") or ()
                if isinstance(item, str)
            ]
            for key in added:
                if key not in source_keys and len(source_keys) < MAX_SOURCE_KEYS:
                    source_keys.append(key)
            target["source_event_keys"] = source_keys
            source_facts = [
                str(item) for item in target.get("source_fact_ids") or ()
                if isinstance(item, str)
            ]
            for fact_id in candidate.get("source_fact_ids") or ():
                if fact_id not in source_facts and len(source_facts) < MAX_SOURCE_KEYS:
                    source_facts.append(str(fact_id))
            target["source_fact_ids"] = source_facts
            target["updated_at"] = now_iso
            if (
                str(target.get("status") or "") in ("pending", "narrowed")
                and self._growth_promotion_ready(target)
            ):
                target["status"] = "confirmed"
                target["confirmed_at"] = now_iso
        del character
        return (str(target.get("id")),), False

    # -- promotion ------------------------------------------------------

    async def promote(self, character: str) -> PromotionOutcome:
        """Advance pending/confirmed rows via score, time or growth rules."""
        character = self._character(character)
        settings = self._settings
        confirmed: list[str] = []
        promoted: list[str] = []
        persona_ids: list[str] = []
        errors = 0
        persona_needed = False
        with self._lock_for(character):
            rows = self._store.load(character)
            now = self._clock.now()
            now_iso = _now_iso(now)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if row.get("memory_kind") not in MEMORY_KINDS:
                    continue
                status = str(row.get("status") or "")
                row_id = row.get("id")
                if not isinstance(row_id, str) or not row_id:
                    continue
                if status in ("pending", "narrowed"):
                    if self._growth_promotion_ready(row) and self._change_status(
                        character, row_id, status, "confirmed",
                        reason="growth_threshold",
                    ):
                        confirmed.append(row_id)
                        status = "confirmed"
                if status != "confirmed":
                    continue
                if not _entry_applies_to_scene(row, None):
                    continue
                persona_needed = True
                if self._persona is None:
                    continue
                if self._barrier.blocks_row(character, row):
                    continue
                try:
                    persona_id = self._persona.add_growth(
                        character, self._growth_persona_row(row, now_iso),
                    )
                except Exception:  # noqa: BLE001 - keep confirmed for a retry
                    errors += 1
                    continue
                if not isinstance(persona_id, str) or not persona_id:
                    errors += 1
                    continue
                if self._change_status(
                    character, row_id, "confirmed", "promoted",
                    absorbed_into=persona_id,
                ):
                    promoted.append(row_id)
                    persona_ids.append(persona_id)

            # Non-growth rows: score-driven when powerful memory is on, the
            # host's zero-LLM time-driven fallback when it is off.
            rows = self._store.load(character)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if row.get("memory_kind") in MEMORY_KINDS:
                    continue
                status = str(row.get("status") or "")
                row_id = row.get("id")
                if not isinstance(row_id, str) or not row_id:
                    continue
                if self._barrier.blocks_row(character, row):
                    continue
                if settings.powerful_memory:
                    if status == "pending":
                        if self.evidence_score(character, row) >= settings.confirmed_threshold:
                            if self._change_status(
                                character, row_id, "pending", "confirmed",
                                reason="evidence_confirmed",
                            ):
                                confirmed.append(row_id)
                    elif status == "confirmed":
                        if self.evidence_score(character, row) >= settings.promoted_threshold:
                            persona_needed = True
                            if self._persona is None:
                                continue
                            try:
                                persona_id = self._persona.add_reflection(
                                    character,
                                    self._reflection_persona_row(row, now_iso),
                                )
                            except Exception:  # noqa: BLE001
                                errors += 1
                                continue
                            if not isinstance(persona_id, str) or not persona_id:
                                errors += 1
                                continue
                            if self._change_status(
                                character, row_id, "confirmed", "promoted",
                                absorbed_into=persona_id,
                            ):
                                promoted.append(row_id)
                                persona_ids.append(persona_id)
                else:
                    if status == "pending":
                        age = _age_days(row.get("created_at"), now)
                        if age >= settings.time_confirm_days:
                            if self._change_status(
                                character, row_id, "pending", "confirmed",
                                reason="time_driven",
                            ):
                                confirmed.append(row_id)
                    elif status == "confirmed":
                        age = _age_days(row.get("confirmed_at"), now)
                        if age >= settings.time_promote_days:
                            persona_needed = True
                            if self._persona is None:
                                continue
                            try:
                                persona_id = self._persona.add_reflection(
                                    character,
                                    self._reflection_persona_row(row, now_iso),
                                )
                            except Exception:  # noqa: BLE001
                                errors += 1
                                continue
                            if not isinstance(persona_id, str) or not persona_id:
                                errors += 1
                                continue
                            if self._change_status(
                                character, row_id, "confirmed", "promoted",
                                absorbed_into=persona_id,
                            ):
                                promoted.append(row_id)
                                persona_ids.append(persona_id)
        if persona_needed and self._persona is None:
            status, reason = "blocked", "persona_unavailable"
        elif errors:
            status, reason = "failed", "persona_error"
        else:
            status, reason = "completed", None
        return PromotionOutcome(
            status,
            promoted_ids=tuple(promoted),
            confirmed_ids=tuple(confirmed),
            persona_ids=tuple(persona_ids),
            reason=reason,
        )

    def _growth_promotion_ready(self, row: Mapping[str, Any]) -> bool:
        kind = str(row.get("memory_kind") or "")
        required = PROMOTION_RULES.get(kind)
        if required is None:
            return False
        support = _as_int(row.get("support_count"), 0)
        if support < self._settings.growth_min_support:
            return False
        evidence = row.get("support_evidence")
        if not isinstance(evidence, Mapping):
            return False
        for flag in required:
            bucket = evidence.get(flag)
            if isinstance(bucket, (list, tuple)) and bucket:
                return True
        return False

    @staticmethod
    def _reflection_persona_row(
        row: Mapping[str, Any], now_iso: str,
    ) -> dict[str, Any]:
        reflection_id = str(row.get("id") or "")
        # PersonaService.add_reflection owns the prom_<reflection_id> id and
        # derives source_id from this row; passing a prefixed id would produce
        # prom_prom_ref_... (integration defect found by the Lead).
        return {
            "id": reflection_id,
            "entity": row.get("entity") or "relationship",
            "text": str(row.get("text") or ""),
            "source": "reflection",
            "source_id": reflection_id,
            "merged_from_ids": [],
            "protected": False,
            "suppress": False,
            "reinforcement": _as_float(row.get("reinforcement"), 0.0),
            "disputation": _as_float(row.get("disputation"), 0.0),
            "created_at": now_iso,
            "updated_at": now_iso,
            "version_history": [],
        }

    @staticmethod
    def _growth_persona_row(
        row: Mapping[str, Any], now_iso: str,
    ) -> dict[str, Any]:
        item_id = str(row.get("id") or "")
        claim = _normalize_claim(row.get("claim") or row.get("text"))
        kind = str(row.get("memory_kind") or "")
        entity = "relationship" if kind == "relationship" else "neko"
        # PersonaService.add_growth derives
        # prom_growth_<sha1(item_id|claim)[:16]> from this row; it reads the
        # item id from "id"/"item_id", so the raw reflection id must be passed.
        return {
            "id": item_id,
            "entity": entity,
            "text": claim,
            "source": "growth",
            "source_id": item_id,
            "merged_from_ids": [],
            "applies_to": dict(row.get("applies_to") or {}),
            "created_at": now_iso,
            "updated_at": now_iso,
            "version_history": [{
                "text": claim,
                "replaced_at": now_iso,
                "reason": "growth_promotion",
                "source_reflection_ids": [item_id],
            }],
            "growth_key": row.get("growth_key"),
        }

    # -- surfaced / feedback -------------------------------------------

    async def record_surfaced(
        self, character: str, reflection_id: str,
    ) -> dict[str, Any]:
        """Record a delivered proactive mention and refresh its cooldown."""
        character = self._character(character)
        reflection_id = str(reflection_id or "").strip()
        if not reflection_id:
            raise ValueError("reflection_id must be nonempty")
        with self._lock_for(character):
            rows = self._store.load(character)
            row = _find_row(rows, reflection_id)
            if row is None:
                return {"status": "skipped", "reason": "unknown_reflection"}
            if self._barrier.blocks_row(character, row):
                return {"status": "skipped", "reason": "deleted"}
            if str(row.get("status") or "") in REFLECTION_TERMINAL_STATUSES:
                return {"status": "skipped", "reason": "terminal"}
            now = self._clock.now()
            now_iso = _now_iso(now)
            next_eligible = (
                now + timedelta(minutes=self._settings.cooldown_minutes)
            ).isoformat()
            row["next_eligible_at"] = next_eligible
            self._store.save(character, rows)
            surfaced = self._store.load_surfaced(character)
            text = str(row.get("text") or "")
            existing = next(
                (item for item in surfaced
                 if item.get("reflection_id") == reflection_id),
                None,
            )
            if existing is None:
                surfaced.append({
                    "reflection_id": reflection_id,
                    "text": text,
                    "surfaced_at": now_iso,
                    "feedback": None,
                })
            else:
                existing["surfaced_at"] = now_iso
                existing["text"] = text
                existing["feedback"] = None
            self._store.save_surfaced(character, surfaced)
            return {
                "status": "completed",
                "reflection_id": reflection_id,
                "next_eligible_at": next_eligible,
                "surfaced": True,
            }

    async def record_feedback(
        self, character: str, reflection_id: str, feedback: str,
    ) -> dict[str, Any]:
        """Write back confirmed/denied/ignored and dispatch the evidence delta."""
        character = self._character(character)
        reflection_id = str(reflection_id or "").strip()
        if not reflection_id:
            raise ValueError("reflection_id must be nonempty")
        value = str(feedback or "").strip().lower()
        if value not in ("confirmed", "denied", "ignored"):
            raise ValueError(f"invalid feedback {feedback!r}")
        with self._lock_for(character):
            rows = self._store.load(character)
            row = _find_row(rows, reflection_id)
            if row is None:
                return {"status": "skipped", "reason": "unknown_reflection"}
            if self._barrier.blocks_row(character, row):
                return {"status": "skipped", "reason": "deleted"}
            now = self._clock.now()
            now_iso = _now_iso(now)
            surfaced = self._store.load_surfaced(character)
            target = next(
                (item for item in surfaced
                 if item.get("reflection_id") == reflection_id),
                None,
            )
            if target is None:
                surfaced.append({
                    "reflection_id": reflection_id,
                    "text": str(row.get("text") or ""),
                    "surfaced_at": now_iso,
                    "feedback": value,
                    "feedback_at": now_iso,
                })
            else:
                target["feedback"] = value
                target["feedback_at"] = now_iso
            self._store.save_surfaced(character, surfaced)
            if value == "confirmed":
                updates = {
                    "reinforcement": _as_float(row.get("reinforcement"), 0.0)
                    + USER_CONFIRM_DELTA,
                    "rein_last_signal_at": now_iso,
                }
            elif value == "denied":
                updates = {
                    "disputation": _as_float(row.get("disputation"), 0.0)
                    + USER_REBUT_DELTA,
                    "disp_last_signal_at": now_iso,
                }
            else:
                updates = {
                    "reinforcement": _as_float(row.get("reinforcement"), 0.0)
                    + IGNORED_REINFORCEMENT_DELTA,
                    "rein_last_signal_at": now_iso,
                }
            self._record_evidence(character, reflection_id, updates)
            if value == "denied":
                current = str(row.get("status") or "")
                if current in REFLECTION_ACTIVE_STATUSES:
                    self._change_status(
                        character, reflection_id, current, "denied",
                        reason="user_denied",
                    )
            return {
                "status": "completed",
                "reflection_id": reflection_id,
                "feedback": value,
            }

    # -- archive --------------------------------------------------------

    async def archive(self, character: str) -> dict[str, Any]:
        """Daily sub-zero accounting plus terminal/score-driven archival."""
        character = self._character(character)
        settings = self._settings
        archived = 0
        incremented = 0
        protected = 0
        with self._lock_for(character):
            now = self._clock.now()
            today = now.date().isoformat()
            rows = self._store.load(character)
            for row in list(rows):
                if not isinstance(row, dict):
                    continue
                row_id = row.get("id")
                if not isinstance(row_id, str) or not row_id:
                    continue
                if row.get("protected"):
                    protected += 1
                    continue
                if self._barrier.blocks_row(character, row):
                    continue
                effective = _as_int(row.get("sub_zero_days"), 0)
                if (
                    self.evidence_score(character, row) < 0.0
                    and row.get("sub_zero_last_increment_date") != today
                ):
                    if self._record_evidence(
                        character, row_id,
                        {
                            "sub_zero_days": effective + 1,
                            "sub_zero_last_increment_date": today,
                        },
                    ):
                        incremented += 1
                        effective += 1
                if effective >= settings.sub_zero_archive_days:
                    if self._archive_row(character, row_id, reason="sub_zero"):
                        archived += 1
            # Age-based archival of terminal rows (host prepare_save_reflections).
            rows = self._store.load(character)
            cutoff = now - timedelta(days=settings.archive_after_days)
            for row in list(rows):
                if not isinstance(row, dict):
                    continue
                if str(row.get("status") or "") not in ("promoted", "denied"):
                    continue
                row_id = row.get("id")
                if not isinstance(row_id, str) or not row_id:
                    continue
                if row.get("protected"):
                    continue
                if self._barrier.blocks_row(character, row):
                    continue
                timestamp = (
                    row.get("promoted_at")
                    or row.get("denied_at")
                    or row.get("created_at")
                )
                stamp = _parse_iso(timestamp, now)
                if stamp is None or stamp >= cutoff:
                    continue
                if self._archive_row(character, row_id, reason="age"):
                    archived += 1
        return {
            "status": "completed",
            "archived": archived,
            "sub_zero_incremented": incremented,
            "protected_skipped": protected,
        }

    def _archive_row(self, character: str, reflection_id: str, *, reason: str) -> bool:
        rows = self._store.load(character)
        row = _find_row(rows, reflection_id)
        if row is None or row.get("protected"):
            return False
        if self._barrier.blocks_row(character, row):
            return False
        now_iso = _now_iso(self._clock.now())
        archive_entry = dict(row)
        archive_entry["status"] = "archived"
        archive_entry["archived_at"] = now_iso
        archive_entry["archive_shard_path"] = ""
        archive_ids = {
            item.get("id") for item in self._store.load_archive(character)
            if isinstance(item, dict)
        }
        shard = ""
        if reflection_id not in archive_ids:
            shard = self._store.append_archive(character, [archive_entry])
        archive_entry["archive_shard_path"] = shard or None
        payload = {
            "reflection_id": reflection_id,
            "from": row.get("status"),
            "to": "archived",
            "ts": now_iso,
            "archived_at": now_iso,
            "archive_shard_path": shard or None,
            "entry_snapshot": archive_entry,
            "reason": reason,
        }
        def _mutate(view: Any) -> None:
            view[:] = [
                item for item in view
                if not (isinstance(item, dict) and item.get("id") == reflection_id)
            ]

        try:
            self._journal.record_and_save(
                character,
                EVT_REFLECTION_STATE_CHANGED,
                payload,
                load_view=self._store.load,
                mutate_view=_mutate,
                save_view=self._store.save,
            )
        except Exception:
            # The shard already holds the row: restore the active copy so the
            # next sweep retries the removal instead of losing the row.
            if reflection_id not in archive_ids:
                rows = self._store.load(character)
                if _find_row(rows, reflection_id) is None:
                    rows.append(dict(row))
                    self._store.save(character, rows)
            raise
        return True

    # -- growth ---------------------------------------------------------

    def growth_items(
        self, character: str, *, scene: Any = None,
    ) -> list[dict[str, Any]]:
        """Confirmed/promoted growth rows usable in this scene."""
        character = self._character(character)
        rows = self._store.load(character)
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("memory_kind") not in MEMORY_KINDS:
                continue
            if str(row.get("status") or "") not in ("confirmed", "promoted"):
                continue
            if row.get("needs_rebuild"):
                continue
            if not _entry_applies_to_scene(row, scene):
                continue
            if self._barrier.blocks_row(character, row):
                continue
            if reflection_invalidated(row):
                continue
            out.append(dict(row))
        out.sort(
            key=lambda item: (
                _as_int(item.get("support_count"), 0),
                str(item.get("updated_at") or ""),
            ),
            reverse=True,
        )
        return out[:MAX_GROWTH_ITEMS]

    def growth_block(
        self,
        character: str,
        *,
        scene: Any = None,
        language: str = "zh",
        limit_tokens: int | None = None,
    ) -> MemoryBlock:
        """Render the growth block; `version` is the growth_uses version."""
        character = self._character(character)
        items = self.growth_items(character, scene=scene)
        version = _as_int(
            self._store.load_growth_uses(character).get("version"), 0,
        )
        budget = GROWTH_BLOCK_MAX_TOKENS if limit_tokens is None else int(limit_tokens)
        block, used = _render_growth(items, language=language, limit_tokens=budget)
        return MemoryBlock(block=block, items=tuple(used), version=version)

    async def record_growth_usage(
        self,
        character: str,
        item_id: str,
        usage: str,
        *,
        source_key: str = "",
        scene: Any = None,
        outcome: str | None = None,
        feedback: str | None = None,
    ) -> GrowthReceipt:
        """Record one growth use; identity (item, version, usage, source_key)."""
        character = self._character(character)
        item_id = _bounded(item_id, 200)
        if not item_id:
            raise ValueError("item_id must be nonempty")
        value = _bounded(usage, 16).lower()
        if value not in USAGE_TYPES:
            raise ValueError(f"invalid growth usage {usage!r}")
        source = _bounded(source_key, 600)
        with self._lock_for(character):
            doc = self._store.load_growth_uses(character)
            version = _as_int(doc.get("version"), 0)
            uses = [dict(row) for row in doc.get("uses") or ()
                    if isinstance(row, Mapping)]
            # Identity is (item, version, usage, source_key); the version is the
            # growth version at the time of the use. Because every new use
            # bumps the version, the same (item, usage, source_key) tuple is
            # recognised regardless of the row's historical version, which
            # keeps a retried call idempotent (host growth.py:800-863).
            for row in uses:
                if (
                    row.get("item_id") == item_id
                    and row.get("usage") == value
                    and str(row.get("source_key") or "") == source
                ):
                    return GrowthReceipt(item_id, version, True)
            entry: dict[str, Any] = {
                "item_id": item_id,
                "version": version,
                "usage": value,
                "source_key": source,
                "scene": _normalize_scene(scene),
                "at": _now_iso(self._clock.now()),
            }
            if outcome:
                entry["outcome"] = _bounded(outcome, 200)
            if feedback:
                entry["feedback"] = _bounded(feedback, 200)
            uses.append(entry)
            if len(uses) > MAX_GROWTH_USES:
                uses = uses[-MAX_GROWTH_USES:]
            doc["uses"] = uses
            doc["version"] = version + 1
            self._store.save_growth_uses(character, doc)
            return GrowthReceipt(item_id, version + 1, False)

    async def retract_growth(
        self, character: str, item_id: str, *, reason: str = "user_retract",
    ) -> dict[str, Any]:
        """Withdraw a growth row and bump the growth version."""
        character = self._character(character)
        item_id = _bounded(item_id, 200)
        if not item_id:
            raise ValueError("item_id must be nonempty")
        with self._lock_for(character):
            rows = self._store.load(character)
            row = _find_row(rows, item_id)
            if row is None or row.get("memory_kind") not in MEMORY_KINDS:
                return {"ok": False, "reason": "unknown_growth_item"}
            version = _as_int(
                self._store.load_growth_uses(character).get("version"), 0,
            )
            if str(row.get("status") or "") == "withdrawn":
                # Idempotent retry: the persona walk is itself idempotent and
                # returns only entries it newly removed, so a crash between the
                # row write and the persona write is repaired here.
                return {
                    "ok": True,
                    "duplicate": True,
                    "item": _growth_explanation(row, version),
                    "version": version,
                    "persona_retracted": self._retract_persona_for_growth(
                        character, item_id, "user_retract",
                    ),
                }
            now_iso = _now_iso(self._clock.now())
            row["status"] = "withdrawn"
            row["withdrawn_at"] = now_iso
            row["withdrawn_reason"] = _bounded(reason, 200) or "user_retract"
            row["updated_at"] = now_iso
            self._store.save(character, rows)
            doc = self._store.load_growth_uses(character)
            doc["version"] = version + 1
            self._store.save_growth_uses(character, doc)
            return {
                "ok": True,
                "duplicate": False,
                "item": _growth_explanation(row, version + 1),
                "version": version + 1,
                "persona_retracted": self._retract_persona_for_growth(
                    character, item_id, row["withdrawn_reason"],
                ),
            }

    async def reconcile_growth_support(self, character: str) -> dict[str, Any]:
        """Recompute growth support after corrections/deletions."""
        character = self._character(character)
        with self._lock_for(character):
            rows = self._store.load(character)
            invalidated_keys = set(self._barrier.state(character).index.source_keys)
            for fact in self._facts.load(character):
                if not isinstance(fact, dict) or not fact.get("invalidated_at"):
                    continue
                for key in fact.get("source_event_keys") or ():
                    if isinstance(key, str) and key:
                        invalidated_keys.add(key)
            if not invalidated_keys:
                return {
                    "ok": True, "changed": 0, "narrowed": 0,
                    "withdrawn": 0, "persona_retracted": [],
                }
            now_iso = _now_iso(self._clock.now())
            changed = 0
            narrowed: list[str] = []
            withdrawn: list[str] = []
            persona_retracted: list[str] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if row.get("memory_kind") not in MEMORY_KINDS:
                    continue
                if str(row.get("status") or "") in GROWTH_TERMINAL_STATUSES:
                    continue
                before_status = str(row.get("status") or "")
                if not _apply_support_removal(row, invalidated_keys, now_iso=now_iso):
                    continue
                changed += 1
                row_id = str(row.get("id") or "")
                if row.get("status") == "withdrawn":
                    withdrawn.append(row_id)
                elif row.get("status") == "narrowed":
                    narrowed.append(row_id)
                if before_status == "promoted" and row.get("status") in (
                    "withdrawn", "narrowed",
                ):
                    persona_retracted.extend(
                        self._retract_persona_for_growth(
                            character, row_id,
                            str(row.get("withdrawn_reason")
                                or row.get("narrowed_reason")
                                or "sources_reduced"),
                        ),
                    )
            if changed:
                self._store.save(character, rows)
                doc = self._store.load_growth_uses(character)
                doc["version"] = _as_int(doc.get("version"), 0) + 1
                self._store.save_growth_uses(character, doc)
            return {
                "ok": True,
                "changed": changed,
                "narrowed": len(narrowed),
                "withdrawn": len(withdrawn),
                "persona_retracted": persona_retracted,
            }

    def _retract_persona_for_growth(
        self, character: str, item_id: str, reason: str,
    ) -> list[str]:
        """Retract persona entries derived from one growth item (idempotent)."""
        if self._persona is None:
            return []
        method = getattr(self._persona, "retract_source", None)
        if not callable(method):
            return []
        try:
            result = method(
                character, source="growth", source_id=item_id,
                reason=_bounded(reason, 200) or "user_retract",
            )
        except Exception:  # noqa: BLE001 - row withdrawal is the durable part
            return []
        if not isinstance(result, (list, tuple)):
            return []
        return [str(item) for item in result if isinstance(item, str) and item]

    # -- B03 integration: deletion cleaner / correction invalidator -------

    def resolve_ids(
        self, character: str, resolved: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Deletion-engine resolve delegate: every row the request covers.

        The engine resolves before the fact step so the persona step gets
        explicit `persona_ids` even after the reflection rows it would walk are
        gone. All statuses are returned, terminal rows included: a deletion
        must cover archived/hidden rows too. Matching rules: explicit
        `reflection_ids`; `source_fact_ids` against `fact_ids`; row source
        keys against `fact_source_keys`/`source_keys`; normalized text hash;
        episode ids; and the subject when it is not private.
        """
        character = self._character(character)
        explicit = {
            str(item) for item in resolved.get("reflection_ids") or ()
            if isinstance(item, str) and item
        }
        fact_ids = {
            str(item) for item in resolved.get("fact_ids") or ()
            if isinstance(item, str) and item
        }
        source_keys = {
            str(item)
            for item in (
                resolved.get("fact_source_keys")
                or resolved.get("source_keys")
                or ()
            )
            if isinstance(item, str) and item
        }
        text_hashes = {
            str(item) for item in resolved.get("text_hashes") or ()
            if isinstance(item, str) and item
        }
        episode_ids = {
            str(item) for item in resolved.get("episode_ids") or ()
            if isinstance(item, str) and item
        }
        subject = str(resolved.get("subject") or "")
        subject_scope = subject not in ("", "private")
        matched: list[str] = []
        narrowed: list[str] = []
        for row in self._store.load(character):
            if not isinstance(row, dict):
                continue
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id:
                continue
            if row_id in explicit:
                matched.append(row_id)
                continue
            if text_hashes and text_hash(row.get("text")) in text_hashes:
                matched.append(row_id)
                continue
            if episode_ids and (episode_ids_of(row) & episode_ids):
                matched.append(row_id)
                continue
            if subject_scope and row_matches_subject(row, subject):
                matched.append(row_id)
                continue
            if self._barrier is not None and (
                self._barrier.blocks_target(character, "reflection", row_id)
                or self._barrier.blocks_row(character, row)
            ):
                matched.append(row_id)
                continue
            # A row only counts as removed when every dependency is gone; a
            # partially covered row is narrowed by the cleaner instead (its
            # derived persona entry is retracted there, not resolved here).
            sources = {
                str(item) for item in row.get("source_fact_ids") or ()
                if isinstance(item, str) and item
            }
            row_keys = _row_source_keys(row)
            deleted_sources = sources & fact_ids
            deleted_keys = row_keys & source_keys
            if not deleted_sources and not deleted_keys:
                continue
            remaining_sources = sources - fact_ids
            support = {
                str(item) for item in row.get("support_event_keys") or ()
                if isinstance(item, str) and item
            }
            remaining_support = support - source_keys
            is_growth = row.get("memory_kind") in MEMORY_KINDS
            fully_dependent = bool(sources) and not remaining_sources
            if is_growth and support and not remaining_support:
                fully_dependent = True
            if not sources and not support:
                fully_dependent = True
            if fully_dependent:
                if row_id not in matched:
                    matched.append(row_id)
            elif row_id not in narrowed:
                narrowed.append(row_id)
        return {
            "reflection_ids": matched,
            "narrowed_reflection_ids": narrowed,
        }

    def cleanup(self, character: str, resolved: Mapping[str, Any]) -> Mapping[str, Any]:
        """Deletion-engine cleaner delegate (B03 `DeletionServices.reflections`)."""
        return self._cleaner().cleanup(character, resolved)

    def invalidate(
        self,
        character: str,
        fact_ids: Sequence[str],
        *,
        invalidated_at: str,
        reason: str,
        correction_id: str,
    ) -> Mapping[str, Any]:
        """Correction-service invalidator delegate (derived invalidation walker)."""
        return self._invalidator().invalidate(
            character,
            fact_ids,
            invalidated_at=invalidated_at,
            reason=reason,
            correction_id=correction_id,
        )

    def _cleaner(self) -> "ReflectionDeletionCleaner":
        return ReflectionDeletionCleaner(
            store=self._store, barrier=self._barrier, persona=self._persona,
        )

    def _invalidator(self) -> "ReflectionInvalidator":
        return ReflectionInvalidator(store=self._store, barrier=self._barrier)

    # -- evidence/state write helpers ------------------------------------

    def _record_evidence(
        self,
        character: str,
        reflection_id: str,
        updates: Mapping[str, Any],
    ) -> bool:
        rows = self._store.load(character)
        row = _find_row(rows, reflection_id)
        if row is None:
            return False
        if self._barrier.blocks_row(character, row):
            return False
        snapshot = {
            "reinforcement": _as_float(row.get("reinforcement"), 0.0),
            "disputation": _as_float(row.get("disputation"), 0.0),
            "rein_last_signal_at": row.get("rein_last_signal_at"),
            "disp_last_signal_at": row.get("disp_last_signal_at"),
            "sub_zero_days": _as_int(row.get("sub_zero_days"), 0),
            "sub_zero_last_increment_date": row.get(
                "sub_zero_last_increment_date",
            ),
            "user_fact_reinforce_count": _as_int(
                row.get("user_fact_reinforce_count"), 0,
            ),
            "last_promote_attempt_at": row.get("last_promote_attempt_at"),
            "promote_attempt_count": _as_int(row.get("promote_attempt_count"), 0),
        }
        for key, value in updates.items():
            if key in EVIDENCE_SNAPSHOT_KEYS:
                snapshot[key] = value
        payload = {"reflection_id": reflection_id, **snapshot}
        payload["source"] = "reflection_service"

        def _mutate(view: Any) -> None:
            target = _find_row(view, reflection_id)
            if target is None:
                return
            for key in EVIDENCE_SNAPSHOT_KEYS:
                target[key] = snapshot[key]

        self._journal.record_and_save(
            character,
            EVT_REFLECTION_EVIDENCE_UPDATED,
            payload,
            load_view=self._store.load,
            mutate_view=_mutate,
            save_view=self._store.save,
        )
        return True

    def _change_status(
        self,
        character: str,
        reflection_id: str,
        from_status: str,
        to_status: str,
        *,
        absorbed_into: str | None = None,
        reason: str | None = None,
        reject_explanation: str | None = None,
    ) -> bool:
        rows = self._store.load(character)
        row = _find_row(rows, reflection_id)
        if row is None or str(row.get("status") or "") != from_status:
            return False
        if self._barrier.blocks_row(character, row):
            return False
        now_iso = _now_iso(self._clock.now())
        payload: dict[str, Any] = {
            "reflection_id": reflection_id,
            "from": from_status,
            "to": to_status,
            "ts": now_iso,
        }
        if absorbed_into is not None:
            payload["absorbed_into"] = absorbed_into
        if reason is not None:
            payload["reason"] = reason
        if reject_explanation is not None:
            payload["reject_explanation"] = reject_explanation
        applied = {"ok": False}

        def _mutate(view: Any) -> None:
            target = _find_row(view, reflection_id)
            if target is None or str(target.get("status") or "") != from_status:
                return
            target["status"] = to_status
            target[f"{to_status}_at"] = now_iso
            if absorbed_into is not None:
                target["absorbed_into"] = absorbed_into
            if reason is not None:
                if to_status == "promote_blocked":
                    target["promote_blocked_reason"] = reason
                elif to_status == "denied":
                    target["denied_reason"] = reason
            if reject_explanation is not None:
                target["reject_reason"] = reject_explanation
            applied["ok"] = True

        self._journal.record_and_save(
            character,
            EVT_REFLECTION_STATE_CHANGED,
            payload,
            load_view=self._store.load,
            mutate_view=_mutate,
            save_view=self._store.save,
        )
        return applied["ok"]

    # -- backoff --------------------------------------------------------

    def _bump_backoff(self, character: str, key: str) -> None:
        with self._lock_for(character):
            doc = self._store.load_backoff(character)
            entry = doc.get(key) or {}
            doc[key] = {
                "n": _as_int(entry.get("n"), 0) + 1,
                "at": _now_iso(self._clock.now()),
            }
            self._store.save_backoff(character, doc)

    def _clear_backoff(self, character: str, key: str) -> None:
        doc = self._store.load_backoff(character)
        if key in doc:
            doc.pop(key, None)
            self._store.save_backoff(character, doc)

    def _dead_letter_healed(self, entry: Mapping[str, Any]) -> bool:
        last = _parse_iso(entry.get("at"), self._clock.now())
        if last is None:
            return False
        elapsed = (self._clock.now() - last).total_seconds()
        return elapsed >= self._settings.dead_letter_self_heal_seconds

    def _mark_absorbed(
        self, character: str, source_fact_ids: Sequence[str],
    ) -> list[str]:
        wanted = {str(item) for item in source_fact_ids if str(item or "").strip()}
        facts = self._facts.load(character)
        marked: list[str] = []
        changed = False
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            fact_id = fact.get("id")
            if not isinstance(fact_id, str) or fact_id not in wanted:
                continue
            if self._barrier.blocks_row(character, fact):
                continue
            marked.append(fact_id)
            if fact.get("absorbed") is not True:
                fact["absorbed"] = True
                changed = True
        if changed:
            self._facts.save(character, facts)
        return sorted(marked)

    # -- journal replay handlers ----------------------------------------

    def _apply_evidence_updated(
        self, character: str, payload: Mapping[str, Any],
    ) -> bool:
        """Full-snapshot whitelist replay; a load failure propagates (pause)."""
        reflection_id = payload.get("reflection_id")
        if not isinstance(reflection_id, str) or not reflection_id:
            return False
        rows = self._store.load(character)
        row = _find_row(rows, reflection_id)
        if row is None:
            return False
        if self._barrier.blocks_row(character, row):
            return True
        changed = False
        for key in EVIDENCE_SNAPSHOT_KEYS:
            if key in payload and row.get(key) != payload[key]:
                row[key] = payload[key]
                changed = True
        if changed:
            self._store.save(character, rows)
        return changed

    def _apply_state_changed(
        self, character: str, payload: Mapping[str, Any],
    ) -> bool:
        """CAS on `from`; archived rows self-heal their shard."""
        reflection_id = payload.get("reflection_id")
        to_status = payload.get("to")
        if not isinstance(reflection_id, str) or not reflection_id:
            return False
        if not isinstance(to_status, str) or not to_status:
            return False
        from_status = payload.get("from")
        rows = self._store.load(character)
        row = _find_row(rows, reflection_id)
        if row is not None and isinstance(from_status, str) and from_status:
            if str(row.get("status") or "") != from_status:
                # A newer signal won; drop the stale transition but advance.
                return True
        if to_status == "archived":
            return self._replay_archived(
                character, reflection_id, payload, rows, row,
            )
        if row is None:
            return False
        if self._barrier.blocks_row(character, row):
            return True
        changed = False
        if row.get("status") != to_status:
            row["status"] = to_status
            changed = True
        timestamp = payload.get("ts")
        if isinstance(timestamp, str) and timestamp:
            key = f"{to_status}_at"
            if row.get(key) != timestamp:
                row[key] = timestamp
                changed = True
        absorbed_into = payload.get("absorbed_into")
        if absorbed_into is not None and row.get("absorbed_into") != absorbed_into:
            row["absorbed_into"] = absorbed_into
            changed = True
        reason = payload.get("reason")
        if isinstance(reason, str) and reason:
            if to_status == "promote_blocked" and row.get("promote_blocked_reason") != reason:
                row["promote_blocked_reason"] = reason
                changed = True
            elif to_status == "denied" and row.get("denied_reason") != reason:
                row["denied_reason"] = reason
                changed = True
        explanation = payload.get("reject_explanation")
        if isinstance(explanation, str) and row.get("reject_reason") != explanation:
            row["reject_reason"] = explanation
            changed = True
        if changed:
            self._store.save(character, rows)
        return changed

    def _replay_archived(
        self,
        character: str,
        reflection_id: str,
        payload: Mapping[str, Any],
        rows: list[dict[str, Any]],
        row: dict[str, Any] | None,
    ) -> bool:
        changed = False
        snapshot = payload.get("entry_snapshot")
        if isinstance(snapshot, Mapping) and not self._barrier.blocks_row(
            character, snapshot,
        ):
            archived_ids = {
                item.get("id") for item in self._store.load_archive(character)
                if isinstance(item, dict)
            }
            if snapshot.get("id") not in archived_ids:
                shard = payload.get("archive_shard_path")
                if not isinstance(shard, str) or not shard:
                    shard = snapshot.get("archive_shard_path")
                if isinstance(shard, str) and shard:
                    self._store.ensure_archive_entry(
                        character, dict(snapshot), shard,
                    )
                else:
                    self._store.append_archive(character, [dict(snapshot)])
                changed = True
        if row is not None:
            kept = [item for item in rows if item.get("id") != reflection_id]
            self._store.save(character, kept)
            changed = True
        return changed


# -- deletion cleaner --------------------------------------------------------


class ReflectionDeletionCleaner:
    """B03 `DeletionServices.reflections` cleaner over reflections/surfaced.

    Full dependency (every source fact deleted, or the row itself is a
    tombstone target) discards the row; a partial dependency narrows
    `source_fact_ids`, records `invalidated_sources` and sets
    `needs_rebuild=True`. Archived shards are rewritten by id.
    """

    def __init__(
        self,
        *,
        store: ReflectionStore,
        barrier: DeletionBarrier | None = None,
        persona: Any = None,
    ) -> None:
        self._store = store
        self._barrier = barrier
        self._persona = persona

    def cleanup(
        self, character: str, resolved: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        character = str(character or "").strip()
        if not character:
            raise ValueError("character must be nonempty")
        reflection_ids = {
            str(item) for item in resolved.get("reflection_ids") or ()
            if isinstance(item, str) and item
        }
        fact_ids = {
            str(item) for item in resolved.get("fact_ids") or ()
            if isinstance(item, str) and item
        }
        source_keys = {
            str(item)
            for item in (
                resolved.get("source_keys")
                or resolved.get("fact_source_keys")
                or ()
            )
            if isinstance(item, str) and item
        }
        episode_ids = {
            str(item) for item in resolved.get("episode_ids") or ()
            if isinstance(item, str) and item
        }
        text_hashes = {
            str(item) for item in resolved.get("text_hashes") or ()
            if isinstance(item, str) and item
        }
        subject = str(resolved.get("subject") or "")
        subject_scope = subject not in ("", "private")
        rows = self._store.load(character)
        kept: list[dict[str, Any]] = []
        removed: list[str] = []
        narrowed = 0
        for row in rows:
            if not isinstance(row, dict):
                kept.append(row)
                continue
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id:
                kept.append(row)
                continue
            if row_id in reflection_ids:
                removed.append(row_id)
                continue
            if self._barrier is not None:
                if self._barrier.blocks_target(character, "reflection", row_id):
                    removed.append(row_id)
                    continue
                if self._barrier.blocks_row(character, row):
                    removed.append(row_id)
                    continue
            if episode_ids and (episode_ids_of(row) & episode_ids):
                removed.append(row_id)
                continue
            if text_hashes and text_hash(row.get("text")) in text_hashes:
                removed.append(row_id)
                continue
            if subject_scope and row_matches_subject(row, subject):
                removed.append(row_id)
                continue
            sources = [
                str(item) for item in row.get("source_fact_ids") or ()
                if isinstance(item, str) and item
            ]
            deleted_sources = [item for item in sources if item in fact_ids]
            row_keys = _row_source_keys(row)
            deleted_keys = row_keys & source_keys
            if not deleted_sources and not deleted_keys:
                kept.append(row)
                continue
            remaining_sources = [item for item in sources if item not in fact_ids]
            is_growth = row.get("memory_kind") in MEMORY_KINDS
            support = [
                str(item) for item in row.get("support_event_keys") or ()
                if isinstance(item, str) and item
            ]
            remaining_support = [item for item in support if item not in source_keys]
            if deleted_keys:
                row["support_event_keys"] = remaining_support
                row["support_count"] = len(remaining_support)
                evidence = row.get("support_evidence")
                buckets: dict[str, list[str]] = {}
                if isinstance(evidence, Mapping):
                    for flag in EVIDENCE_FLAGS:
                        buckets[flag] = [
                            str(item) for item in evidence.get(flag) or ()
                            if isinstance(item, str)
                            and item not in source_keys
                        ]
                else:
                    buckets = {flag: [] for flag in EVIDENCE_FLAGS}
                row["support_evidence"] = buckets
                row["source_event_keys"] = [
                    str(item) for item in row.get("source_event_keys") or ()
                    if isinstance(item, str) and item not in source_keys
                ]
            fully_dependent = bool(sources) and not remaining_sources
            if is_growth and support and not remaining_support:
                fully_dependent = True
            if not sources and not support and (deleted_sources or deleted_keys):
                fully_dependent = True
            if fully_dependent:
                removed.append(row_id)
                continue
            invalidated = {
                str(item) for item in row.get("invalidated_sources") or ()
                if isinstance(item, str)
            }
            invalidated.update(deleted_sources)
            invalidated.update(deleted_keys)
            row["source_fact_ids"] = remaining_sources
            row["invalidated_sources"] = sorted(invalidated)
            row["needs_rebuild"] = True
            narrowed += 1
            kept.append(row)
        if removed or narrowed:
            self._store.save(character, kept)

        surfaced = self._store.load_surfaced(character)
        removed_set = set(removed)
        kept_surfaced: list[dict[str, Any]] = []
        for item in surfaced:
            if not isinstance(item, dict):
                continue
            if item.get("reflection_id") in removed_set:
                continue
            text = item.get("text")
            if (
                self._barrier is not None
                and isinstance(text, str)
                and self._barrier.blocks_text(character, text)
            ):
                continue
            kept_surfaced.append(item)
        surfaced_removed = len(surfaced) - len(kept_surfaced)
        if surfaced_removed:
            self._store.save_surfaced(character, kept_surfaced)

        archive_removed = 0
        if removed:
            archive_removed = self._store.delete_from_archive(
                character, sorted(removed),
            )
        # A derived persona entry must stop rendering as soon as its reflection
        # is removed or narrowed (the persona cleaner also drops entries when
        # the deletion engine resolved explicit persona_ids first).
        persona_retracted: list[str] = []
        for row_id in removed:
            persona_retracted.extend(
                self._retract_persona(character, row_id, "deletion"),
            )
        for row in kept:
            if not isinstance(row, dict) or not row.get("needs_rebuild"):
                continue
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id:
                continue
            if not (
                row.get("absorbed_into")
                or str(row.get("status") or "") in ("confirmed", "promoted")
            ):
                continue
            persona_retracted.extend(
                self._retract_persona(character, row_id, "deletion_narrowed"),
            )
        return {
            "status": "completed",
            "detail": {
                "reflections_removed": len(removed),
                "reflections_narrowed": narrowed,
                "surfaced_removed": surfaced_removed,
                "archive_removed": archive_removed,
                "persona_retracted": persona_retracted,
            },
        }

    def _retract_persona(
        self, character: str, reflection_id: str, reason: str,
    ) -> list[str]:
        """Idempotently retract persona entries derived from one reflection."""
        if self._persona is None:
            return []
        method = getattr(self._persona, "retract_source", None)
        if not callable(method):
            return []
        try:
            result = method(
                character, source="reflection", source_id=reflection_id,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 - the reflection row write is durable
            return []
        if not isinstance(result, (list, tuple)):
            return []
        return [str(item) for item in result if isinstance(item, str) and item]


# -- correction invalidator --------------------------------------------------


class ReflectionInvalidator:
    """Correction-service derived walker for reflections.json (main view only).

    A fully invalidated row becomes `withdrawn` (and `needs_rebuild` is
    cleared); a partially invalidated row keeps its remaining sources, records
    `invalidated_sources` and `needs_rebuild=True`. Only rows changed by this
    call are returned, so a retried correction is idempotent.
    """

    def __init__(
        self,
        *,
        store: ReflectionStore,
        barrier: DeletionBarrier | None = None,
    ) -> None:
        self._store = store
        self._barrier = barrier

    def invalidate(
        self,
        character: str,
        fact_ids: Sequence[str],
        *,
        invalidated_at: str,
        reason: str,
        correction_id: str,
    ) -> Mapping[str, Any]:
        character = str(character or "").strip()
        if not character:
            raise ValueError("character must be nonempty")
        targets = {
            str(item) for item in fact_ids or ()
            if isinstance(item, str) and item
        }
        if not targets:
            return {"reflection_ids": []}
        rows = self._store.load(character)
        changed_ids: list[str] = []
        changed = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id:
                continue
            sources = {
                str(item) for item in row.get("source_fact_ids") or ()
                if isinstance(item, str) and item
            }
            hit = sources & targets
            if not hit:
                continue
            if self._barrier is not None and self._barrier.blocks_row(
                character, row,
            ):
                continue
            if sources and sources <= targets:
                if (
                    str(row.get("status") or "") == "withdrawn"
                    and not row.get("needs_rebuild")
                ):
                    continue
                row["status"] = "withdrawn"
                row["withdrawn_at"] = invalidated_at
                row["withdrawn_reason"] = reason
                row["correction_id"] = correction_id
                row["needs_rebuild"] = False
                changed_ids.append(row_id)
                changed = True
                continue
            existing = {
                str(item) for item in row.get("invalidated_sources") or ()
                if isinstance(item, str)
            }
            added = hit - existing
            if not added:
                continue
            row["invalidated_sources"] = sorted(existing | hit)
            row["needs_rebuild"] = True
            changed_ids.append(row_id)
            changed = True
        if changed:
            self._store.save(character, rows)
        return {"reflection_ids": changed_ids}


# -- growth helpers ----------------------------------------------------------


def _growth_explanation(row: Mapping[str, Any], version: int) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "kind": row.get("memory_kind"),
        "claim": row.get("claim") or row.get("text"),
        "situation": row.get("situation") or "",
        "applies_to": dict(row.get("applies_to") or {}),
        "status": row.get("status"),
        "support_count": _as_int(row.get("support_count"), 0),
        "source_event_keys": list(row.get("source_event_keys") or []),
        "support_event_keys": list(row.get("support_event_keys") or []),
        "source_fact_ids": list(row.get("source_fact_ids") or []),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "valid_from": row.get("valid_from"),
        "superseded_by": row.get("superseded_by"),
        "version": int(version or 0),
    }


_GROWTH_LABELS: dict[str, dict[str, str]] = {
    "zh": {
        "header": "[成长与经验]",
        "habit": "习惯",
        "interest": "兴趣",
        "experience": "经历",
        "relationship": "关系",
        "situation": "情境",
        "support": "支持",
    },
    "en": {
        "header": "[Growth and experience]",
        "habit": "Habit",
        "interest": "Interest",
        "experience": "Experience",
        "relationship": "Relationship",
        "situation": "When",
        "support": "support",
    },
}


def _normalize_language(language: Any) -> str:
    raw = str(language or "").strip().replace("_", "-").lower()
    if raw.startswith("zh"):
        return "zh"
    if raw.startswith("en"):
        return "en"
    return "zh"


def _render_growth(
    items: Sequence[Mapping[str, Any]],
    *,
    language: Any,
    limit_tokens: int,
) -> tuple[str, list[dict[str, Any]]]:
    labels = _GROWTH_LABELS[_normalize_language(language)]
    budget = max(0, int(limit_tokens or 0))
    if not items or budget <= 0:
        return "", []
    lines = [labels["header"]]
    used = max(1, len(lines[0]) // 2)
    rendered: list[dict[str, Any]] = []
    for item in items:
        kind = str(item.get("memory_kind") or "")
        claim = _normalize_claim(item.get("claim") or item.get("text"))
        if not claim:
            continue
        situation = _normalize_situation(item.get("situation"))
        kind_label = labels.get(kind) or labels["experience"]
        line = f"- {kind_label}: {claim}"
        if situation:
            line += f" ({labels['situation']}: {situation})"
        support = _as_int(item.get("support_count"), 0)
        if support > 1:
            line += f" [{labels['support']} {support}]"
        cost = max(1, len(line) // 2)
        if used + cost > budget:
            break
        lines.append(line)
        used += cost
        rendered.append(dict(item))
    if len(lines) <= 1:
        return "", []
    return "\n".join(lines), rendered


# -- prompt ------------------------------------------------------------------


def _build_synthesis_prompt(facts: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        f"- {str(fact.get('text') or '').strip()} "
        f"(importance: {_importance_of(fact)})"
        for fact in facts
        if str(fact.get("text") or "").strip()
    ]
    body = "\n".join(lines)
    return (
        "你是长期记忆反思器。根据下面的用户事实，归纳一条稳定的认识。\n"
        "只输出一个 JSON 对象：{\"reflection\": \"一句独立陈述\", "
        "\"entity\": \"master|neko|relationship\", "
        "\"relation_type\": null, \"temporal_scope\": null, "
        "\"memory_kind\": null|habit|interest|experience|relationship, "
        "\"claim\": null, \"situation\": null}。\n"
        "不要重复原文，不要编造；没有稳定认识时也要给出最保守的一条。"
        "只输出 JSON。\n\n"
        f"事实：\n{body}"
    )


__all__ = [
    "EVIDENCE_SNAPSHOT_KEYS",
    "EVT_REFLECTION_EVIDENCE_UPDATED",
    "EVT_REFLECTION_STATE_CHANGED",
    "GrowthReceipt",
    "MEMORY_KINDS",
    "PROMOTION_RULES",
    "PromotionOutcome",
    "REFLECTION_EVENT_TYPES",
    "REFLECTION_TERMINAL_STATUSES",
    "ReflectionDeletionCleaner",
    "ReflectionInvalidator",
    "ReflectionService",
    "ReflectionSettings",
    "SynthesisOutcome",
    "growth_key",
    "reflection_id_from_facts",
]
