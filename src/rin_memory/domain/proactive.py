"""M6-A proactive outreach: real causes, the pending-chat intent queue, and
sending that survives a restart.

Design section 11.3 is the specification this module implements:

    a proactive message comes from a real reason -- an item the user cares about
    reaching a good time to follow up, an agreed reminder/check-in/share, a real
    task producing a new result, new information about a sustained interest, or a
    finding that came out of a shared experience. Every candidate carries its
    source, reason, validity time, cooldown and mention restriction. Scheduling
    checks quiet hours, whether the user just spoke, and whether the same item
    was followed up recently. Ordinary proactive messages may start at two a day,
    at least ninety minutes apart, silent 00:00-09:00 in the user timezone; an
    explicitly agreed reminder follows the agreed time.

Three decisions shape this module.

**Where a candidate may come from.** The service derives a candidate only from a
record that already exists (an open state item that carries a next step, an
eligible growth row, a reflection that came out of an episode). It never invents
a reason: a candidate with no source is refused at enqueue time, and there is a
test for it. Two of the five causes -- an agreement and a real task result --
cannot be derived from stored records at all, because whether the two of you
agreed to something, or whether a task really produced a result, is knowledge only
the runtime has. Those are *declared* by the caller together with a real source
reference, so the provenance is never a free-text excuse.

**How maturity is routed.** An explicitly agreed reminder or a real task result is
itself the reason, so it forms a candidate directly. An understanding she inferred
on her own (a growth row or a reflection) must clear the M4 eligibility gate first
(design section 9.3): a raw fact stays fact_not_eligible, an immature row stays
not_mature. It is the same gate automatic evolution uses, so a proactive message
can never surface something the card itself would not be allowed to learn from.

**How a live queue behaves.** Four operations exist because a message that waited
is not a message that was decided: postpone (its moment is not now), rewrite (say
it better), merge (fold it into another reason or into the user own turn) and
cancel. Every one keeps the original source -- a postponed candidate is still the
same reason, not a new one. Then, before any body is generated, the record and the
conversation are re-read: if the user already brought it up (G25) the candidate is
absorbed into the current exchange instead of asking again; if the user moved to an
unrelated topic and this is not an agreed reminder, it waits (design section 10.4:
do not drag an old thread into a new topic); if the source was deleted or the
topic was forbidden, it stops.

Visibility follows M5 exactly. send() creates an output record: the body is durable
but invisible. Only a delivery receipt for a part makes it part of the shared
conversation, so an undelivered line is never something both parties experienced
(design section 12.3). Silence and the passage of time never mark an item done and
never add closeness (design section 11.2); nothing here writes state, growth or
affinity, and no candidate is ever created from the absence of a reply.

Time is expressed as a fixed UTC offset (ProactiveSettings.user_utc_offset_minutes)
rather than an IANA zone name. That is a **current limitation, not a design
choice**: this build has no timezone database available (zoneinfo cannot resolve a
key without the tzdata package, which the environment does not ship), and adding a
data package to the core was rejected. A user in a DST zone must state the offset
they are in; the quiet window is then off by one hour around a shift. Tests cover
the cross-midnight arithmetic explicitly because that is where an off-by-one would
hide.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from ..invalidation import normalize_text, row_matches_subject
from ..persistence.proactive import (
    CAUSES,
    DIRECT_SOURCES,
    INFERRED_SOURCE,
    INTENT_KINDS,
    LIVE_STATUSES,
    MATURITY_SOURCES,
    MAX_TEXT_CHARS,
    STATUS_CANCELLED,
    STATUS_INVALIDATED,
    STATUS_MERGED,
    STATUS_PENDING,
    STATUS_POSTPONED,
    STATUS_SENT,
    STATUS_STALE,
    CandidateRecord,
    ProactiveStore,
    candidate_id_for,
    snapshot_payload,
)
from ..ports import Clock, WritePermit

#: Journal event name for one candidate row. Persisted identifier.
EVT_PROACTIVE_CANDIDATE = "core.proactive_candidate"

#: Stable reasons. They are reported to the caller and asserted by tests, so a
#: refusal is always explainable instead of a silent no-op.
REASON_NO_SOURCE = "no_source"
REASON_UNKNOWN_CAUSE = "unknown_cause"
REASON_UNKNOWN_MATURITY = "unknown_maturity"
REASON_UNKNOWN_INTENT = "unknown_intent"
REASON_EMPTY_TEXT = "empty_text"
REASON_TEXT_TOO_LONG = "text_too_long"
REASON_NOT_MATURE = "not_mature"
REASON_FACT_NOT_ELIGIBLE = "fact_not_eligible"
REASON_SOURCE_DELETED = "source_deleted"
REASON_RESTRICTED = "mention_restricted"
REASON_PAUSED = "topic_paused"
REASON_THREAD_CLOSED = "thread_closed"
REASON_QUIET_HOURS = "quiet_hours"
REASON_DAILY_CAP = "daily_cap"
REASON_MIN_GAP = "min_gap"
REASON_USER_JUST_SPOKE = "user_just_spoke"
REASON_TOPIC_CHANGED = "topic_changed"
REASON_RECENTLY_DISCUSSED = "recently_discussed"
REASON_NOT_DUE = "not_due"
REASON_NO_CANDIDATE = "no_candidate"
REASON_ALREADY_SENT = "already_sent"
REASON_UNKNOWN_CANDIDATE = "unknown_candidate"
REASON_NOT_LIVE = "not_live"
REASON_DISABLED = "disabled"
REASON_OUTPUTS_NOT_WIRED = "outputs_not_wired"
REASON_NO_OUTPUT = "no_output"

#: Dispatch outcomes.
DISPATCH_SEND = "send"
DISPATCH_ABSORBED = "absorbed"
DISPATCH_DEFER = "defer"
DISPATCH_IDLE = "idle"
DISPATCH_REFUSED = "refused"

#: How far back the daily cap and the minimum gap look at sent rows.
SENT_LOOKBACK = 128


@dataclass(frozen=True)
class ProactiveSettings:
    """Scheduling parameters. Defaults are the design trial numbers.

    user_utc_offset_minutes is a fixed offset instead of an IANA zone because no
    timezone database is available in this environment; the docstring of the
    module explains why that is a limitation rather than a choice.
    """

    #: Ordinary proactive messages per local day (agreed items are exempt).
    max_per_day: int = 2
    #: Minimum spacing between two ordinary sends, minutes.
    min_gap_minutes: int = 90
    #: Local quiet window [start, end) in hours; agreed items are exempt.
    quiet_start_hour: int = 0
    quiet_end_hour: int = 9
    #: Local time offset used for quiet hours and the daily cap.
    user_utc_offset_minutes: int = 480
    #: A user message this recent makes the moment unusable for a cold open.
    user_just_spoke_minutes: int = 30
    #: Same-topic cooldown, minutes. Design section 11.3 requires the scheduler to
    #: check "whether the same item was followed up recently"; this is the other
    #: half of that check -- whether the *user* already raised it. Six hours is
    #: deliberate: a topic the user just discussed is deferred independently
    #: of the item's age. The thread floor below is the initial due-time rule;
    #: it does not bypass this pre-send relevance check.
    topic_cooldown_minutes: int = 360
    #: How long an open item waits before its follow-up is due, hours.
    thread_followup_min_hours: int = 12
    #: Default delay applied by postpone when the caller names none, minutes.
    default_postpone_minutes: int = 120
    #: Candidates derived per derivation round (bounded work).
    derive_limit: int = 8
    #: Master switch; off keeps the table readable but refuses every new one.
    enabled: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_per_day", "min_gap_minutes", "user_just_spoke_minutes",
            "topic_cooldown_minutes", "thread_followup_min_hours",
            "default_postpone_minutes", "derive_limit",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in ("quiet_start_hour", "quiet_end_hour"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 24:
                raise ValueError(f"{name} must be an hour in 0..24")
        if isinstance(self.user_utc_offset_minutes, bool) or not isinstance(
            self.user_utc_offset_minutes, int,
        ):
            raise ValueError("user_utc_offset_minutes must be an integer")
        if not -14 * 60 <= self.user_utc_offset_minutes <= 14 * 60:
            raise ValueError("user_utc_offset_minutes must be within -14h..+14h")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")


@dataclass(frozen=True)
class ProactiveReceipt:
    """Result of one enqueue / modify / dispatch / send / deliver call."""

    ok: bool = True
    status: str = ""
    duplicate: bool = False
    reason: str | None = None
    candidate_id: str | None = None
    output_id: str | None = None
    candidate: Mapping[str, Any] | None = None
    detail: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "status": self.status,
            "duplicate": self.duplicate,
            "reason": self.reason,
            "candidate_id": self.candidate_id,
            "output_id": self.output_id,
        }
        if self.candidate is not None:
            payload["candidate"] = dict(self.candidate)
        if self.detail is not None:
            payload["detail"] = dict(self.detail)
        return payload


class ProactiveService:
    """Candidates, scheduling and the send/receipt loop for one data root.

    Collaborators are duck-typed so the service is usable (and testable) without
    the full core. Each missing one degrades a *specific* ability and is reported
    rather than silently assumed: no state service means no derived thread
    candidates, no output service means nothing can be sent, no directive store
    means a mention restriction is refused instead of ignored.
    """

    def __init__(
        self,
        *,
        store: ProactiveStore,
        clock: Clock,
        permit: WritePermit,
        state: Any = None,
        reflections: Any = None,
        facts: Any = None,
        directives: Any = None,
        barrier: Any = None,
        outputs: Any = None,
        journal: Any = None,
        settings: ProactiveSettings | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._permit = permit
        self._state = state
        self._reflections = reflections
        self._facts = facts
        self._directives = directives
        self._barrier = barrier
        self._outputs = outputs
        self._journal = journal
        self._settings = settings or ProactiveSettings()
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        if self._journal is not None:
            # Registered before any write: a journal entry this build writes
            # must be replayable by it.
            self._journal.register_handler(EVT_PROACTIVE_CANDIDATE, self.replay)

    # -- wiring -----------------------------------------------------------

    @property
    def settings(self) -> ProactiveSettings:
        return self._settings

    def _lock(self, character: str) -> threading.RLock:
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

    def _now(self) -> datetime:
        return self._clock.now()

    def _iso(self, moment: datetime | None = None) -> str:
        return (moment or self._now()).isoformat()

    # -- intake -----------------------------------------------------------

    def enqueue(
        self,
        character: str,
        *,
        cause: str,
        text: str,
        why: str = "",
        maturity_source: str = "",
        subject: str = "private",
        scene: str = "",
        intent: str | None = None,
        source_keys: Sequence[str] = (),
        fact_ids: Sequence[str] = (),
        reflection_ids: Sequence[str] = (),
        thread_id: str | None = None,
        thread_revision: str | None = None,
        episode_id: str | None = None,
        due_at: str | None = None,
        agreed_at: str | None = None,
        candidate_id: str | None = None,
        request_id: str | None = None,
    ) -> ProactiveReceipt:
        """Register one candidate; a candidate with no source is refused.

        Both the caller-declared causes and the derived rows come through here,
        so the no-fabricated-reason rule is enforced in exactly one place.
        """
        name = self._character(character)
        if not self._settings.enabled:
            return ProactiveReceipt(ok=False, reason=REASON_DISABLED)
        cause_key = str(cause or "").strip()
        if cause_key not in CAUSES:
            return ProactiveReceipt(ok=False, reason=REASON_UNKNOWN_CAUSE)
        maturity = str(maturity_source or "").strip()
        if maturity not in MATURITY_SOURCES:
            return ProactiveReceipt(ok=False, reason=REASON_UNKNOWN_MATURITY)
        body = str(text or "").strip()
        if not body:
            return ProactiveReceipt(ok=False, reason=REASON_EMPTY_TEXT)
        if len(body) > MAX_TEXT_CHARS:
            return ProactiveReceipt(ok=False, reason=REASON_TEXT_TOO_LONG)
        intent_key = str(intent or "").strip()
        if intent_key and intent_key not in INTENT_KINDS:
            return ProactiveReceipt(ok=False, reason=REASON_UNKNOWN_INTENT)
        keys = [str(item).strip() for item in source_keys
                if isinstance(item, str) and item.strip()]
        facts = [str(item).strip() for item in fact_ids
                 if isinstance(item, str) and item.strip()]
        refs = [str(item).strip() for item in reflection_ids
                if isinstance(item, str) and item.strip()]
        thread = str(thread_id or "").strip() or None
        episode = str(episode_id or "").strip() or None
        if not (keys or facts or refs or thread or episode):
            # Design section 11.3: no source, no message.
            return ProactiveReceipt(ok=False, reason=REASON_NO_SOURCE)
        if self._blocked(name, keys):
            return ProactiveReceipt(ok=False, reason=REASON_SOURCE_DELETED)

        mature, eligibility = self._maturity_gate(
            name, maturity, reflection_ids=refs, fact_ids=facts,
        )
        if not mature:
            return ProactiveReceipt(
                ok=False, reason=eligibility.get("reason", REASON_NOT_MATURE),
                detail={"eligibility": eligibility},
            )
        subject_key = str(subject or "private").strip() or "private"
        if self._restricted(name, body, str(why or "")):
            return ProactiveReceipt(
                ok=False, reason=REASON_RESTRICTED,
                detail={"source": "mention_directive"},
            )
        if self._paused(name, subject_key, body, str(why or "")):
            return ProactiveReceipt(ok=False, reason=REASON_PAUSED)

        stable = (
            str(candidate_id or "").strip()
            or candidate_id_for(
                name, subject_key, cause_key,
                request_id or _source_digest(keys, facts, refs, thread, episode),
            )
        )
        with self._lock(name):
            existing = self._store.get(name, stable)
            if existing is not None and str(existing.get("status")) in LIVE_STATUSES:
                # Same reason, still live: a re-derivation, not a second message.
                return ProactiveReceipt(
                    status=str(existing.get("status")), duplicate=True,
                    candidate_id=stable,
                    candidate=CandidateRecord(existing).to_public(),
                )
            now = self._iso()
            row = {
                "candidate_id": stable,
                "cause": cause_key,
                "intent": intent_key or None,
                "maturity_source": maturity,
                "text": body,
                "why": str(why or "").strip()[:400],
                "subject": subject_key,
                "scene": str(scene or ""),
                "status": STATUS_PENDING,
                "due_at": str(due_at or "") or None,
                "agreed_at": str(agreed_at or "") or None,
                "honor_agreed_time": maturity == "explicit_agreement",
                "source_keys": keys,
                "fact_ids": facts,
                "reflection_ids": refs,
                "thread_id": thread,
                "thread_revision": thread_revision,
                "episode_id": episode,
                "eligibility": dict(eligibility),
                "created_at": now,
                "updated_at": now,
            }
            recorded = self._write(name, row)
        return ProactiveReceipt(
            status=STATUS_PENDING, candidate_id=stable,
            candidate=CandidateRecord(recorded).to_public(),
        )

    # -- the four operations ----------------------------------------------

    def postpone(
        self, character: str, candidate_id: str, *,
        until: str | None = None, minutes: int | None = None, reason: str = "",
    ) -> ProactiveReceipt:
        """Delay one candidate to a later time; its source is unchanged."""
        name = self._character(character)
        with self._lock(name):
            row = self._live(name, candidate_id)
            if row is None:
                return self._not_live(name, candidate_id)
            delay = (
                self._settings.default_postpone_minutes
                if minutes is None else max(0, int(minutes))
            )
            target = str(until or "").strip() or self._iso(
                self._now() + timedelta(minutes=delay),
            )
            updated = dict(row)
            updated.update({
                "status": STATUS_POSTPONED,
                "due_at": target,
            })
            if reason:
                updated["why"] = _append(row.get("why"), reason)
            recorded = self._write(name, updated)
        return ProactiveReceipt(
            status=STATUS_POSTPONED, candidate_id=str(candidate_id),
            candidate=CandidateRecord(recorded).to_public(),
            detail={"until": target},
        )

    def rewrite(
        self, character: str, candidate_id: str, *,
        text: str, why: str | None = None, reason: str = "rewritten",
    ) -> ProactiveReceipt:
        """Say it differently; the previous wording is kept in the history."""
        name = self._character(character)
        body = str(text or "").strip()
        if not body:
            return ProactiveReceipt(ok=False, reason=REASON_EMPTY_TEXT)
        if len(body) > MAX_TEXT_CHARS:
            return ProactiveReceipt(ok=False, reason=REASON_TEXT_TOO_LONG)
        with self._lock(name):
            row = self._live(name, candidate_id)
            if row is None:
                return self._not_live(name, candidate_id)
            haystack_why = str(why if why is not None else row.get("why") or "")
            if self._restricted(name, body, haystack_why):
                return ProactiveReceipt(ok=False, reason=REASON_RESTRICTED)
            history = [dict(item) for item in row.get("rewrite_history") or ()]
            current = str(row.get("text") or "")
            if current:
                history.append({"text": current, "at": self._iso()})
            updated = dict(row)
            updated.update({
                "text": body,
                "rewrite_history": history,
            })
            if why is not None:
                updated["why"] = str(why).strip()[:400]
            recorded = self._write(name, updated)
        return ProactiveReceipt(
            status=str(recorded.get("status") or STATUS_PENDING),
            candidate_id=str(candidate_id),
            candidate=CandidateRecord(recorded).to_public(),
        )

    def merge(
        self, character: str, candidate_id: str, *, into: str, reason: str = "",
    ) -> ProactiveReceipt:
        """Fold one candidate into another: the survivor inherits its source.

        Merging is what keeps two reasons about the same thing from becoming two
        messages, and the merged row keeps its own record so the original cause
        stays auditable.
        """
        name = self._character(character)
        target_id = str(into or "").strip()
        if not target_id or target_id == str(candidate_id or "").strip():
            return ProactiveReceipt(ok=False, reason="invalid_merge_target")
        with self._lock(name):
            row = self._live(name, candidate_id)
            if row is None:
                return self._not_live(name, candidate_id)
            survivor = self._live(name, target_id)
            if survivor is None:
                return ProactiveReceipt(
                    ok=False, reason=REASON_NOT_LIVE, candidate_id=target_id,
                )
            merged = dict(row)
            merged.update({
                "status": STATUS_MERGED,
                "merged_into": target_id,
            })
            if reason:
                merged["why"] = _append(row.get("why"), reason)
            recorded = self._write(name, merged)
            # The survivor keeps both sources, so a deletion of either one still
            # reaches the message that was actually sent.
            updated_target = dict(survivor)
            updated_target.update({
                "source_keys": _union(survivor.get("source_keys"), row.get("source_keys"), 16),
                "fact_ids": _union(survivor.get("fact_ids"), row.get("fact_ids"), 16),
                "reflection_ids": _union(
                    survivor.get("reflection_ids"), row.get("reflection_ids"), 16,
                ),
            })
            self._write(name, updated_target)
        return ProactiveReceipt(
            status=STATUS_MERGED, candidate_id=str(candidate_id),
            candidate=CandidateRecord(recorded).to_public(),
            detail={"merged_into": target_id},
        )

    def cancel(
        self, character: str, candidate_id: str, *, reason: str = "user_declined",
    ) -> ProactiveReceipt:
        """Cancel one candidate: it will never be sent."""
        name = self._character(character)
        with self._lock(name):
            row = self._store.get(name, str(candidate_id or "").strip())
            if row is None:
                return ProactiveReceipt(ok=False, reason=REASON_UNKNOWN_CANDIDATE)
            if str(row.get("status")) not in LIVE_STATUSES:
                return ProactiveReceipt(
                    status=str(row.get("status")), duplicate=True,
                    candidate_id=str(candidate_id),
                    candidate=CandidateRecord(row).to_public(),
                )
            updated = dict(row)
            updated.update({
                "status": STATUS_CANCELLED,
                "cancelled_reason": str(reason or "user_declined")[:200],
            })
            recorded = self._write(name, updated)
        return ProactiveReceipt(
            status=STATUS_CANCELLED, candidate_id=str(candidate_id),
            candidate=CandidateRecord(recorded).to_public(),
        )

    # -- reads -------------------------------------------------------------

    def candidates(
        self, character: str, *, subject: str | None = None,
        statuses: Sequence[str] | None = None, limit: int = 20,
    ) -> list[dict[str, Any]]:
        rows = self._store.list(
            self._character(character), subject=subject,
            statuses=statuses, limit=limit,
        )
        return [CandidateRecord(row).to_public() for row in rows]

    def get(self, character: str, candidate_id: str) -> dict[str, Any] | None:
        row = self._store.get(self._character(character), candidate_id)
        return None if row is None else CandidateRecord(row).to_public()

    def pauses(self, character: str, *, subject: str | None = None) -> list[dict[str, Any]]:
        return self._store.active_pauses(self._character(character), subject=subject)

    # -- derivation (the maintenance step) --------------------------------

    def derive(
        self, character: str, *, subject: str = "private", scene: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Derive candidates from records that already exist; never guesses.

        Three causes are derivable from what the service owns:

        * thread_due -- an open state item carrying a recorded next step that has
          waited at least thread_followup_min_hours;
        * shared_finding -- an *eligible* reflection that came out of an episode,
          i.e. it rests on independent experiences (the M4 gate decides);
        * interest_news -- an *eligible* interest growth row.

        A recorded explicit followup agreement carries its own due time.
        Task results are declared by the runtime through enqueue() with a real
        source reference.

        Returns counts only; the rows are readable through candidates().
        """
        name = self._character(character)
        moment = now or self._now()
        moment_iso = moment.isoformat()
        created: list[str] = []
        duplicates: list[str] = []
        refused: list[dict[str, Any]] = []
        budget = max(0, int(self._settings.derive_limit))

        def _collect(receipt: ProactiveReceipt, origin: str) -> None:
            if receipt.duplicate:
                duplicates.append(str(receipt.candidate_id or ""))
            elif receipt.ok:
                created.append(str(receipt.candidate_id or ""))
            else:
                refused.append({"origin": origin, "reason": receipt.reason})

        for item in self._open_threads(name, subject, moment, scene):
            if len(created) >= budget:
                break
            text = str(item.get("text") or "").strip()
            next_step = str(item.get("next_step") or "").strip()
            if not text or not next_step:
                continue
            age = item.get("_age_hours")
            followup_at = _parse_iso(item.get("followup_at"))
            agreed = bool(item.get("agreed_followup")) and followup_at is not None
            if not followup_at and age is not None and age < self._settings.thread_followup_min_hours:
                continue
            _collect(self.enqueue(
                name, cause="agreement" if agreed else "thread_due",
                text=_thread_sentence(text, next_step),
                why=next_step, maturity_source="explicit_agreement" if agreed else INFERRED_SOURCE,
                subject=subject, scene=scene, intent="continue_topic",
                # Full source keys, resolved through the facts the item cites:
                # otherwise a deletion of the fact could never reach this row.
                source_keys=item.get("_source_keys") or (),
                thread_id=str(item.get("id") or "") or None,
                thread_revision=str(item.get("updated_at") or "") or None,
                episode_id=item.get("_episode_id"),
                due_at=followup_at.isoformat() if followup_at else moment_iso,
                agreed_at=followup_at.isoformat() if agreed else None,
                request_id="thread:" + str(item.get("id") or "") + ":" + str(item.get("updated_at") or ""),
            ), "thread_due")

        for row in self._reflection_rows(name):
            if len(created) >= budget:
                break
            body = str(row.get("text") or "").strip()
            episode = str(row.get("episode_id") or "").strip()
            if not body or not episode:
                continue
            _collect(self.enqueue(
                name, cause="shared_finding",
                text=body, why="从共同经历里想到的",
                maturity_source=INFERRED_SOURCE,
                subject=subject, scene=scene, intent="new_idea",
                reflection_ids=[str(row.get("id") or "")],
                episode_id=episode, due_at=moment_iso,
                request_id="reflection:" + str(row.get("id") or ""),
            ), "shared_finding")

        for row in self._growth_rows(name, kind="interest"):
            if len(created) >= budget:
                break
            body = str(row.get("text") or row.get("claim") or "").strip()
            if not body:
                continue
            _collect(self.enqueue(
                name, cause="interest_news",
                text=body, why="最近一直在聊这个",
                maturity_source=INFERRED_SOURCE,
                subject=subject, scene=scene, intent="interest_share",
                reflection_ids=[str(row.get("id") or "")],
                due_at=moment_iso,
                request_id="interest:" + str(row.get("id") or ""),
            ), "interest_news")

        return {
            "status": "completed",
            "created": [item for item in created if item],
            "duplicates": [item for item in duplicates if item],
            "refused": refused,
            "at": moment_iso,
        }

    # -- scheduling and the pre-send recheck ------------------------------

    def claim(self, character: str, *, subject: str, scene: str, owner: str,
              session_id: str, request_id: str) -> dict[str, Any]:
        """Lease one due topic before a host generates its actual wording."""
        name = self._character(character)
        with self._lock(name):
            if not self._settings.enabled:
                return {"status": "idle", "reason": REASON_DISABLED}
            moment = self._now()
            latest = self.last_user_turn(name, subject)
            rows = self._store.list(name, subject=subject, statuses=sorted(LIVE_STATUSES), limit=64)
            rows.sort(key=lambda row: (not row.get("honor_agreed_time"), str(row.get("due_at") or "~")))
            for row in rows:
                if not _scene_matches(row, scene):
                    continue
                until = _parse_iso(row.get("lease_until"))
                if until and until > moment:
                    if (row.get("lease_request"), row.get("lease_owner"), row.get("lease_session")) == (request_id, owner, session_id):
                        return {"status": "ready", "candidate": dict(row)}
                    continue
                verdict = self._timing_decision(
                    name, row, moment=moment, subject=subject,
                    user_recent_text=str(latest.get("text") or ""),
                    user_spoke_at=latest.get("at"),
                )
                status = verdict.get("status")
                if status == DISPATCH_ABSORBED:
                    self._absorb(name, row, str(latest.get("request_id") or request_id))
                    continue
                if status == DISPATCH_REFUSED:
                    self._mark(name, row, status=STATUS_STALE, stale_reason=verdict.get("reason"))
                    continue
                if status != DISPATCH_SEND:
                    continue
                leased = self._mark(
                    name, row, lease_id="lease_" + uuid.uuid4().hex,
                    lease_owner=owner, lease_session=session_id, lease_request=request_id,
                    lease_scene=scene,
                    lease_until=(moment + timedelta(minutes=3)).isoformat(),
                    lease_user_event=latest.get("request_id"),
                )
                return {"status": "ready", "candidate": dict(leased)}
            return {"status": "idle"}

    def last_user_turn(self, character: str, subject: str) -> dict[str, Any]:
        rows = self._store.recent_utterances(character, subject=subject)
        return dict(rows[-1]) if rows else {}

    def leased_candidate(self, character: str, candidate_id: str, lease_id: str,
                         owner: str, session_id: str) -> dict[str, Any]:
        row = self._store.get(character, candidate_id)
        if not row or (row.get("lease_id"), row.get("lease_owner"), row.get("lease_session")) != (
            lease_id, owner, session_id,
        ):
            raise ValueError("proactive_lease_mismatch")
        return row

    def complete_claim(self, character: str, candidate_id: str, *, lease_id: str,
                       owner: str, session_id: str, text: str) -> ProactiveReceipt:
        with self._lock(character):
            row = self.leased_candidate(character, candidate_id, lease_id, owner, session_id)
            if row.get("status") == STATUS_SENT:
                return self.send(character, candidate_id)
            until = _parse_iso(row.get("lease_until"))
            latest = self.last_user_turn(character, str(row.get("subject") or "private"))
            if not until or until <= self._now() or latest.get("request_id") != row.get("lease_user_event"):
                return ProactiveReceipt(ok=False, reason="proactive_preempted")
            return self.send(
                character, candidate_id, subject=str(row.get("subject") or "private"),
                scene=str(row.get("lease_scene") or row.get("scene") or ""), session_id=session_id,
                request_id="proactive:" + candidate_id, text=text,
            )

    def release_claim(self, character: str, candidate_id: str, *, lease_id: str,
                      owner: str, session_id: str, postpone: bool = False) -> None:
        with self._lock(character):
            row = self.leased_candidate(character, candidate_id, lease_id, owner, session_id)
            until = self._now() + timedelta(minutes=self._settings.default_postpone_minutes) if postpone else self._now()
            self._mark(character, row, lease_until=until.isoformat())

    def check_delivery(self, character: str, candidate_id: str, *, lease_id: str,
                       owner: str, session_id: str) -> dict[str, Any]:
        """Recheck a persisted body immediately before each transport attempt."""
        with self._lock(character):
            row = self.leased_candidate(character, candidate_id, lease_id, owner, session_id)
            latest = self.last_user_turn(character, str(row.get("subject") or "private"))
            if row.get("status") != STATUS_SENT:
                verdict = {"status": DISPATCH_REFUSED, "reason": REASON_NOT_LIVE}
            elif latest.get("request_id") != row.get("lease_user_event"):
                verdict = {"status": DISPATCH_REFUSED, "reason": "proactive_preempted"}
            else:
                verdict = self._timing_decision(
                    character, row, moment=self._now(),
                    subject=str(row.get("subject") or "private"), ignore_limits=True,
                )
            if verdict.get("status") != DISPATCH_SEND and row.get("output_id"):
                self._outputs.interrupt(character, row["output_id"], reason=str(verdict.get("reason")))
            return {"ok": verdict.get("status") == DISPATCH_SEND, **verdict}

    def dispatch(
        self, character: str, *, subject: str = "private", scene: str = "",
        user_recent_text: str = "", user_spoke_at: str | None = None,
        now: datetime | None = None, request_id: str | None = None,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Decide whether anything may be said right now, and say it.

        The order is the design order: is there a real cause at all, is the
        moment usable (the user just spoke, quiet hours, today cap, the gap since
        the last message), then the mandatory recheck of the record the cause
        rests on. Only an agreed reminder is allowed past the timing limits,
        because an agreement carries its own time.
        """
        name = self._character(character)
        moment = now or self._now()
        if not self._settings.enabled:
            return {"status": DISPATCH_IDLE, "reason": REASON_DISABLED}
        subject_key = str(subject or "private").strip() or "private"
        live = [
            row for row in self._store.list(
                name, subject=subject_key, statuses=sorted(LIVE_STATUSES), limit=64,
            )
            if _scene_matches(row, scene)
        ]
        if not live:
            return {"status": DISPATCH_IDLE, "reason": REASON_NO_CANDIDATE}
        # Agreed items first (they carry a time), then the oldest due candidate.
        live.sort(key=lambda row: (
            0 if row.get("honor_agreed_time") else 1,
            str(row.get("due_at") or "~"),
            str(row.get("created_at") or ""),
        ))
        candidate = live[0]
        candidate_id = str(candidate.get("candidate_id") or "")
        verdict = self._timing_decision(
            name, candidate, moment=moment, subject=subject_key,
            user_recent_text=user_recent_text, user_spoke_at=user_spoke_at,
        )
        if verdict.get("status") == DISPATCH_ABSORBED:
            absorbed = self._absorb(name, candidate, request_id or "dispatch")
            return {
                "status": DISPATCH_ABSORBED,
                "reason": REASON_USER_JUST_SPOKE,
                "candidate_id": candidate_id,
                "candidate": CandidateRecord(absorbed).to_public(),
            }
        if verdict.get("status") == DISPATCH_REFUSED:
            stale = self._mark(
                name, candidate, status=STATUS_STALE,
                stale_reason=str(verdict.get("reason") or ""),
            )
            return {
                "status": DISPATCH_REFUSED,
                "reason": verdict.get("reason"),
                "candidate_id": candidate_id,
                "candidate": CandidateRecord(stale).to_public(),
            }
        if verdict.get("status") != DISPATCH_SEND:
            return {
                "status": DISPATCH_DEFER,
                "reason": verdict.get("reason"),
                "candidate_id": candidate_id,
                "candidate": CandidateRecord(candidate).to_public(),
                "detail": dict(verdict.get("detail") or {}),
            }
        receipt = self.send(
            name, candidate_id, subject=subject_key, scene=scene, now=moment,
            user_recent_text=user_recent_text, request_id=request_id,
            session_id=session_id, checked=True,
        )
        return {
            "status": DISPATCH_SEND if receipt.ok else DISPATCH_REFUSED,
            "reason": receipt.reason,
            "candidate_id": receipt.candidate_id,
            "output_id": receipt.output_id,
            "candidate": dict(receipt.candidate or {}),
            "detail": dict(receipt.detail or {}),
        }

    def recheck(
        self, character: str, candidate_id: str, *,
        subject: str = "private", scene: str = "",
        user_recent_text: str = "", user_spoke_at: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Re-read the record and the conversation before a body is generated.

        A candidate can wait for hours, so everything it was built from may have
        moved: the source can be deleted, the item can be closed, the user can
        have forbidden the topic, or the user can have brought it up already.
        This returns the verdict; send() refuses to run without a send verdict.
        """
        name = self._character(character)
        row = self._store.get(name, str(candidate_id or "").strip())
        if row is None:
            return {"status": DISPATCH_REFUSED, "reason": REASON_UNKNOWN_CANDIDATE}
        if str(row.get("status")) not in LIVE_STATUSES:
            return {"status": DISPATCH_REFUSED, "reason": REASON_NOT_LIVE}
        return self._timing_decision(
            name, row, moment=now or self._now(),
            subject=str(subject or row.get("subject") or "private"),
            user_recent_text=user_recent_text, user_spoke_at=user_spoke_at,
        )

    # -- sending -----------------------------------------------------------

    def send(
        self, character: str, candidate_id: str, *,
        subject: str = "private", scene: str = "",
        now: datetime | None = None, user_recent_text: str = "",
        request_id: str | None = None, session_id: str = "",
        checked: bool = False,
        text: str | None = None,
    ) -> ProactiveReceipt:
        """Persist the body for one candidate as an M5 output record.

        The body is not a recent line: nothing both parties experienced has
        happened until a delivery receipt names the parts the platform accepted
        (design section 12.3). The candidate remembers its output id, so a retry
        re-uses the same body instead of generating a second message (G22).

        \`checked=True\` is for dispatch, which has already run the timing
        decision for this exact moment; a direct call re-decides, so a caller
        cannot bypass quiet hours by sending a candidate straight to the wire.
        """
        name = self._character(character)
        if self._outputs is None:
            return ProactiveReceipt(ok=False, reason=REASON_OUTPUTS_NOT_WIRED)
        with self._lock(name):
            row = self._store.get(name, str(candidate_id or "").strip())
            if row is None:
                return ProactiveReceipt(ok=False, reason=REASON_UNKNOWN_CANDIDATE)
            status = str(row.get("status"))
            if status == STATUS_SENT:
                # Idempotent: the same candidate keeps the same body.
                return ProactiveReceipt(
                    status=STATUS_SENT, duplicate=True,
                    candidate_id=str(candidate_id),
                    output_id=row.get("output_id"),
                    candidate=CandidateRecord(row).to_public(),
                    reason=REASON_ALREADY_SENT,
                )
            if status not in LIVE_STATUSES:
                return ProactiveReceipt(
                    ok=False, status=status, reason=REASON_NOT_LIVE,
                    candidate_id=str(candidate_id),
                    candidate=CandidateRecord(row).to_public(),
                )
            verdict = {"status": DISPATCH_SEND}
            if not checked:
                verdict = self._timing_decision(
                    name, row, moment=now or self._now(),
                    subject=str(subject or row.get("subject") or "private"),
                    user_recent_text=user_recent_text, user_spoke_at=None,
                )
            if verdict.get("status") != DISPATCH_SEND:
                # The record moved since it was queued: do not say it anyway.
                self._mark(
                    name, row, status=STATUS_STALE,
                    stale_reason=str(verdict.get("reason") or "recheck_failed"),
                )
                return ProactiveReceipt(
                    ok=False, status=STATUS_STALE,
                    reason=str(verdict.get("reason") or "recheck_failed"),
                    candidate_id=str(candidate_id),
                )
            cause: dict[str, Any] = {
                "kind": str(row.get("cause") or ""),
                "source_keys": [str(item) for item in row.get("source_keys") or ()],
                "candidate_id": str(candidate_id),
                "note": str(row.get("why") or "")[:200],
            }
            if row.get("thread_id"):
                cause["thread_id"] = str(row["thread_id"])
            created = self._outputs.create(
                name,
                text=str(text if text is not None else row.get("text") or ""),
                kind="proactive",
                subject=str(row.get("subject") or "private"),
                scene=str(scene or row.get("scene") or ""),
                session_id=str(session_id or ""),
                episode_id=row.get("episode_id"),
                cause=cause,
                request_id=str(request_id or candidate_id),
            )
            if not created.ok:
                return ProactiveReceipt(
                    ok=False, reason=created.reason,
                    candidate_id=str(candidate_id),
                    candidate=CandidateRecord(row).to_public(),
                )
            output_id = created.record.output_id if created.record else None
            updated = self._mark(
                name, row, status=STATUS_SENT, sent_at=self._iso(now),
                output_id=output_id,
            )
            if row.get("reflection_ids"):
                # M5 existing surface: a proactive mention of a reflection starts
                # its cooldown. Recorded at generation; the delivery receipt is
                # what makes it a real mention.
                self._surface_reflections(name, row.get("reflection_ids") or ())
        return ProactiveReceipt(
            status=STATUS_SENT, candidate_id=str(candidate_id),
            output_id=output_id,
            candidate=CandidateRecord(updated).to_public(),
            detail={"cause": cause},
        )

    def deliver(
        self, character: str, candidate_id: str, *,
        parts: Sequence[Mapping[str, Any]] | None = None,
        platform_id: str = "", kind: str = "sent", reason: str | None = None,
    ) -> ProactiveReceipt:
        """Report what the platform actually accepted for this candidate body.

        Delegates to the M5 output service, so per-part idempotency and the
        delivered-parts-only rule are exactly the ones the rest of the system
        already uses (G22: a retry resends only the missing parts).
        """
        name = self._character(character)
        if self._outputs is None:
            return ProactiveReceipt(ok=False, reason=REASON_OUTPUTS_NOT_WIRED)
        row = self._store.get(name, str(candidate_id or "").strip())
        if row is None:
            return ProactiveReceipt(ok=False, reason=REASON_UNKNOWN_CANDIDATE)
        output_id = str(row.get("output_id") or "").strip()
        if not output_id:
            return ProactiveReceipt(
                ok=False, reason=REASON_NO_OUTPUT, candidate_id=str(candidate_id),
            )
        receipt = self._outputs.deliver(
            name, output_id, parts=parts, platform_id=platform_id,
            kind=kind, reason=reason,
        )
        return ProactiveReceipt(
            ok=receipt.ok, duplicate=receipt.duplicate,
            status=receipt.record.status if receipt.record else "",
            reason=receipt.reason, candidate_id=str(candidate_id),
            output_id=output_id,
            detail={
                "appended_parts": list(receipt.appended_parts),
                "recent_written": int(receipt.recent_written),
            },
        )

    def resumable(self, character: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Sent candidates whose body still has undelivered parts (resume)."""
        name = self._character(character)
        if self._outputs is None:
            return []
        out: list[dict[str, Any]] = []
        for row in self._store.list(
            name, statuses=[STATUS_SENT], limit=max(1, int(limit)) * 4,
        ):
            output_id = str(row.get("output_id") or "").strip()
            if not output_id:
                continue
            record = self._outputs.get(name, output_id, include_text=False)
            if record is None or not record.get("resumable"):
                continue
            pending = list(record.get("pending_indices") or ())
            if not pending:
                continue
            out.append({
                "candidate_id": str(row.get("candidate_id") or ""),
                "output_id": output_id,
                "status": record.get("status"),
                "pending_indices": pending,
                "parts_total": int(record.get("parts_total") or 0),
            })
            if len(out) >= max(0, int(limit)):
                break
        return out

    # -- the user own boundaries (G26) ------------------------------------

    def absorb_user_turn(
        self, character: str, *, subject: str, text: str,
        request_id: str = "", scene: str = "",
    ) -> dict[str, Any]:
        """The user spoke first (G25): fold matching candidates into that turn.

        A candidate whose topic the user just raised must not be asked again
        afterwards. The row keeps the request id it was folded into, so the audit
        trail shows why it was never sent as a separate message.
        """
        name = self._character(character)
        body = str(text or "")
        if not normalize_text(body):
            return {"status": "nothing", "absorbed": []}
        subject_key = str(subject or "private").strip() or "private"
        # Every user turn is logged here first, whether or not it matches a
        # candidate: the topic cooldown has to work in the turn *after* the user
        # raised something, when nothing in this call matches any more.
        self._store.record_utterance(
            name, text=body, at=self._iso(), subject=subject_key,
            request_id=request_id or None,
        )
        absorbed: list[str] = []
        for row in self._store.list(
            name, subject=subject_key,
            statuses=sorted(LIVE_STATUSES), limit=64,
        ):
            if not _scene_matches(row, scene):
                continue
            if not _topics_overlap(body, _candidate_haystack(row)):
                continue
            with self._lock(name):
                current = self._store.get(name, str(row.get("candidate_id") or ""))
                if current is None or str(current.get("status")) not in LIVE_STATUSES:
                    continue
                self._absorb(name, current, request_id or "user_turn")
            absorbed.append(str(row.get("candidate_id") or ""))
        return {
            "status": "absorbed" if absorbed else "nothing",
            "absorbed": [item for item in absorbed if item],
        }

    def pause_topic(
        self, character: str, *, subject: str, text: str = "",
        thread_id: str | None = None, minutes: int | None = None,
        reason: str = "user_paused",
    ) -> dict[str, Any]:
        """The user said "先不聊": stop extending this topic now.

        A pause is neither a mention restriction nor a deletion: it is a
        temporary "not now" that expires on its own, and it cancels the
        candidates about that topic so nothing is queued behind it.
        """
        name = self._character(character)
        subject_key = str(subject or "private").strip() or "private"
        term = str(text or "").strip()
        if not term and thread_id:
            for item in self._open_threads(name, subject_key, self._now()):
                if str(item.get("id") or "") == str(thread_id):
                    term = str(item.get("text") or "")
                    break
        term = term.strip()[:40]
        if not term:
            return {"status": DISPATCH_REFUSED, "reason": "empty_topic"}
        window = (
            self._settings.default_postpone_minutes
            if minutes is None else max(1, int(minutes))
        )
        expires = self._now() + timedelta(minutes=window)
        cancelled: list[str] = []
        with self._lock(name):
            self._store.record_pause(
                name, term=term, subject=subject_key,
                expires_at=expires.isoformat(), reason=reason,
            )
            for row in self._store.list(
                name, subject=subject_key, statuses=sorted(LIVE_STATUSES), limit=64,
            ):
                same_thread = (
                    thread_id and str(row.get("thread_id") or "") == str(thread_id)
                )
                if not same_thread and not _topics_overlap(term, _candidate_haystack(row)):
                    continue
                self._mark(name, row, status=STATUS_CANCELLED,
                           cancelled_reason="topic_paused")
                cancelled.append(str(row.get("candidate_id") or ""))
        return {
            "status": "paused", "term": term, "subject": subject_key,
            "expires_at": expires.isoformat(),
            "cancelled": [candidate_id for candidate_id in cancelled if candidate_id],
        }

    def mention_restriction(
        self, character: str, *, subject: str, text: str,
        reason: str = "user_request",
    ) -> dict[str, Any]:
        """The user said "别再提": a real mention restriction.

        The restriction is the existing user directive (so recall honours it too,
        and it expires by TTL like every other one); this method additionally
        cancels the queued candidates about that topic.
        """
        name = self._character(character)
        if self._directives is None:
            return {"ok": False, "reason": "directives_not_wired"}
        term = str(text or "").strip()[:40]
        if len(term) < 2:
            return {"ok": False, "reason": "term_too_short"}
        recorded = self._directives.record(
            name, term=term, kind="topic", locale="zh", source=reason,
        )
        if not recorded:
            return {"ok": False, "reason": "directive_dropped"}
        cancelled: list[str] = []
        for row in self._store.list(
            name, subject=str(subject or "private"),
            statuses=sorted(LIVE_STATUSES), limit=64,
        ):
            if not _topics_overlap(term, _candidate_haystack(row)):
                continue
            with self._lock(name):
                current = self._store.get(name, str(row.get("candidate_id") or ""))
                if current is None or str(current.get("status")) not in LIVE_STATUSES:
                    continue
                self._mark(name, current, status=STATUS_CANCELLED,
                           cancelled_reason="mention_restricted")
            cancelled.append(str(row.get("candidate_id") or ""))
        return {
            "ok": True, "term": term, "subject": str(subject or "private"),
            "expire_at": recorded.get("expire_at"),
            "cancelled": [candidate_id for candidate_id in cancelled if candidate_id],
        }

    # -- deletion ----------------------------------------------------------

    def invalidate_for_deletion(
        self, character: str, resolved: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Deletion cleaner: a candidate whose source is gone stops existing.

        Not cosmetic: a message whose reason was deleted would be exactly the
        "resurrect deleted content" failure the barrier exists to prevent, so the
        row is invalidated (id kept, text cleared) rather than merely cancelled.
        A candidate that was already sent keeps its output id; the M5 output
        cleaner clears the body the same way.
        """
        name = self._character(character)
        keys = {
            str(value) for value in (
                list(resolved.get("fact_source_keys") or ())
                + list(resolved.get("original_source_keys") or ())
            ) if value
        }
        episodes = {
            str(value) for value in resolved.get("episode_ids") or () if value
        }
        facts = {
            str(value) for value in resolved.get("deleted_fact_ids") or () if value
        }
        if not keys and not episodes and not facts:
            return {"status": "completed", "detail": {"invalidated": 0}}
        invalidated = 0
        for row in self._store.list(name, limit=256):
            if str(row.get("status")) == STATUS_INVALIDATED:
                continue
            row_keys = {str(item) for item in row.get("source_keys") or ()}
            row_facts = {str(item) for item in row.get("fact_ids") or ()}
            if not (
                (row_keys & keys)
                or (episodes and str(row.get("episode_id") or "") in episodes)
                or (row_facts & facts)
            ):
                continue
            with self._lock(name):
                current = self._store.get(name, str(row.get("candidate_id") or ""))
                if current is None or str(current.get("status")) == STATUS_INVALIDATED:
                    continue
                updated = dict(current)
                updated.update({
                    "status": STATUS_INVALIDATED,
                    "text": "",
                    "why": "",
                    "stale_reason": REASON_SOURCE_DELETED,
                })
                self._write(name, updated)
            invalidated += 1
        return {"status": "completed", "detail": {"invalidated": invalidated}}

    # -- journal replay -----------------------------------------------------

    def replay(self, character: str, payload: Any) -> bool:
        if not isinstance(payload, Mapping):
            return False
        row = payload.get("candidate")
        if not isinstance(row, Mapping):
            return False
        return self._store.apply_replay(character, payload)

    # -- internals: gate and recheck ---------------------------------------

    def _maturity_gate(
        self, character: str, maturity: str, *,
        reflection_ids: Sequence[str], fact_ids: Sequence[str],
    ) -> tuple[bool, dict[str, Any]]:
        """Route maturity by source: agreed/task direct, inference through M4.

        A raw fact never qualifies (fact_not_eligible) and an immature
        understanding is not_mature -- the same verdicts automatic evolution
        gives, because a proactive line must not surface what the card itself may
        not learn (design section 9.3).
        """
        if maturity in DIRECT_SOURCES:
            return True, {"source": maturity, "eligible": True}
        if fact_ids:
            return False, {
                "source": maturity, "eligible": False,
                "reason": REASON_FACT_NOT_ELIGIBLE,
            }
        if not reflection_ids:
            # An inference resting on an open state item is the thread family;
            # the state record itself is the provenance.
            return True, {
                "source": maturity, "eligible": True, "basis": "state_thread",
            }
        if self._reflections is None:
            return False, {
                "source": maturity, "eligible": False,
                "reason": REASON_NOT_MATURE, "detail": "reflections_not_wired",
            }
        from .growth_evidence import eligibility_of_row
        from .reflection import PROMOTION_RULES

        min_support = self._min_support()
        rows = {str(row.get("id") or ""): row
                for row in self._reflection_rows(character)}
        growth = {str(row.get("id") or ""): row
                  for row in self._growth_rows(character, kind="interest")}
        for reflection_id in reflection_ids:
            row = rows.get(str(reflection_id)) or growth.get(str(reflection_id))
            if row is None:
                return False, {
                    "source": maturity, "eligible": False,
                    "reason": REASON_SOURCE_DELETED,
                    "reflection_id": str(reflection_id),
                }
            ok, reason = eligibility_of_row(
                row, promotion_rules=PROMOTION_RULES, min_support=min_support,
            )
            if not ok:
                return False, {
                    "source": maturity, "eligible": False, "reason": reason,
                    "reflection_id": str(reflection_id),
                }
        return True, {"source": maturity, "eligible": True}

    def _min_support(self) -> int:
        """The independent-experience threshold M4 uses (default 2).

        Read from the reflection service so the proactive gate can never drift
        from the evolution gate it mirrors.
        """
        settings = getattr(self._reflections, "_settings", None)
        value = getattr(settings, "growth_min_support", None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            return value
        return 2

    def _recent_discussion(
        self, character: str, subject: str, row: Mapping[str, Any], moment: datetime,
    ) -> datetime | None:
        """When the user last raised this candidate topic, inside the cooldown.

        An agreed reminder is exempt: the user asked for it at a stated time, so
        their own earlier mention of the subject is the reason for the message,
        not a reason to suppress it (design section 11.3).
        """
        if bool(row.get("honor_agreed_time")):
            return None
        window = self._settings.topic_cooldown_minutes
        if window <= 0:
            return None
        since = moment - timedelta(minutes=window)
        haystack = _candidate_haystack(row)
        if not normalize_text(haystack):
            return None
        newest: datetime | None = None
        for item in self._store.recent_utterances(
            character, subject=subject, since=since,
        ):
            if not _topics_overlap(item.get("text"), haystack):
                continue
            at = _parse_iso(item.get("at"))
            if at is None or at > moment:
                continue
            if newest is None or at > newest:
                newest = at
        return newest

    def _blocked(self, character: str, keys: Sequence[str]) -> bool:
        if self._barrier is None or not keys:
            return False
        for key in keys:
            try:
                if self._barrier.blocks_source_key(character, key):
                    return True
            except Exception:  # noqa: BLE001 - a damaged barrier must not unlock
                return True
        return False

    def _restricted(self, character: str, text: str, why: str = "") -> bool:
        if self._directives is None:
            return False
        try:
            rows = self._directives.active(character) or ()
        except Exception:  # noqa: BLE001 - fail closed on a damaged store
            return True
        haystack = normalize_text(str(text) + " " + str(why))
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            term = normalize_text(row.get("term"))
            if term and term in haystack:
                return True
        return False

    def _paused(self, character: str, subject: str, text: str, why: str = "") -> bool:
        haystack = normalize_text(str(text) + " " + str(why))
        for row in self._store.active_pauses(character, subject=subject):
            term = normalize_text(row.get("term"))
            if term and term in haystack:
                return True
        return False

    def _timing_decision(
        self, character: str, row: Mapping[str, Any], *,
        moment: datetime, subject: str, user_recent_text: str = "",
        user_spoke_at: str | None = None, ignore_limits: bool = False,
    ) -> dict[str, Any]:
        """The shared verdict for dispatch and for the pre-send recheck."""
        agreed = bool(row.get("honor_agreed_time"))
        text = str(row.get("text") or "")
        why = str(row.get("why") or "")

        # 1) Is it due at all?
        due_at = _parse_iso(row.get("agreed_at") if agreed else row.get("due_at"))
        if due_at is not None and due_at > moment:
            return {
                "status": DISPATCH_DEFER, "reason": REASON_NOT_DUE,
                "detail": {"due_at": due_at.isoformat()},
            }

        # 2) Did the user just speak?
        spoke = _parse_iso(user_spoke_at)
        window = self._settings.user_just_spoke_minutes
        if spoke is not None and window > 0:
            elapsed = (moment - spoke).total_seconds() / 60.0
            if 0 <= elapsed <= window:
                if user_recent_text and _topics_overlap(
                    user_recent_text, _candidate_haystack(row),
                ):
                    # G25: the user opened this topic first; join it instead of
                    # asking the same question again.
                    return {
                        "status": DISPATCH_ABSORBED, "reason": REASON_USER_JUST_SPOKE,
                        "detail": {"matched": "same_topic"},
                    }
                if not agreed:
                    # A cold open right after the user said something unrelated
                    # would drag the conversation back (design section 10.4).
                    return {
                        "status": DISPATCH_DEFER, "reason": REASON_TOPIC_CHANGED,
                    }
                return {
                    "status": DISPATCH_DEFER, "reason": REASON_USER_JUST_SPOKE,
                }

        # 2b) Did the user already raise this very topic a while ago? The
        #     "just spoke" check above only covers the current moment; without
        #     this, a topic the user brought up last turn would be asked about
        #     again as soon as the turn boundary moved.
        discussed_at = self._recent_discussion(character, subject, row, moment)
        if discussed_at is not None:
            return {
                "status": DISPATCH_DEFER, "reason": REASON_RECENTLY_DISCUSSED,
                "detail": {"discussed_at": discussed_at.isoformat()},
            }

        # 3) The record it rests on may have moved since it was queued.
        if self._blocked(
            character, [str(item) for item in row.get("source_keys") or ()],
        ):
            return {"status": DISPATCH_REFUSED, "reason": REASON_SOURCE_DELETED}
        if self._restricted(character, text, why):
            return {"status": DISPATCH_REFUSED, "reason": REASON_RESTRICTED}
        if self._paused(character, subject, text, why):
            return {"status": DISPATCH_REFUSED, "reason": REASON_PAUSED}
        if row.get("thread_id") and not self._thread_open(
            character, subject, str(row["thread_id"]), str(row.get("lease_scene") or row.get("scene") or ""), row.get("thread_revision"),
        ):
            return {"status": DISPATCH_REFUSED, "reason": REASON_THREAD_CLOSED}

        if ignore_limits or agreed:
            # An agreed reminder follows the agreed time (design section 11.3),
            # so the trial caps and quiet hours do not apply to it.
            return {"status": DISPATCH_SEND, "detail": {"agreed": agreed}}

        # 4) Timing limits for ordinary messages.
        local = self._local(moment)
        if self._in_quiet_hours(local):
            return {
                "status": DISPATCH_DEFER, "reason": REASON_QUIET_HOURS,
                "detail": {"local_hour": local.hour},
            }
        sent_today = self._sent_on_local_day(character, local.date())
        if sent_today >= self._settings.max_per_day:
            return {
                "status": DISPATCH_DEFER, "reason": REASON_DAILY_CAP,
                "detail": {
                    "sent_today": sent_today,
                    "max_per_day": self._settings.max_per_day,
                },
            }
        last_sent = _parse_iso(self._last_sent_at(character))
        if last_sent is not None:
            gap = (moment - last_sent).total_seconds() / 60.0
            if gap < self._settings.min_gap_minutes:
                return {
                    "status": DISPATCH_DEFER, "reason": REASON_MIN_GAP,
                    "detail": {"minutes_since_last": round(gap, 1)},
                }
        return {"status": DISPATCH_SEND, "detail": {"agreed": False}}

    # -- internals: record readers -----------------------------------------

    def _fact_index(self, character: str, subject: str) -> dict[str, dict[str, Any]]:
        """Map every local event id and full source key to its fact.

        A state item records only local event ids ('e_interview'), while a
        deletion record and the barrier speak in full source keys
        ('host:rin:private:e_interview'). Without this bridge a derived candidate
        would carry provenance that no deletion could ever match, which would
        leave a queued message about content the user deleted -- the exact
        resurrection the barrier exists to prevent.
        """
        if self._facts is None:
            return {}
        try:
            rows = self._facts.load(character) or ()
        except Exception:  # noqa: BLE001 - an unreadable fact list resolves nothing
            return {}
        index: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if not row_matches_subject(row, subject):
                continue
            entry = {
                "fact_id": str(row.get("id") or ""),
                "episode_id": str(row.get("episode_id") or "").strip() or None,
            }
            for key in row.get("source_event_keys") or ():
                value = str(key or "").strip()
                if not value:
                    continue
                index.setdefault(value, entry)
                local = value.rsplit(":", 1)[-1]
                if local:
                    index.setdefault(local, entry)
        return index

    def _open_threads(
        self, character: str, subject: str, moment: datetime, scene: str = "",
    ) -> list[dict[str, Any]]:
        if self._state is None:
            return []
        fact_index = self._fact_index(character, subject)
        try:
            kind, _, value = str(scene).partition(":")
            scope = {{"game": "game_id", "plugin": "plugin_id"}.get(kind, "session_id"): value} if value else {}
            snapshot = self._state.read(character, subject=subject, scene=scope)
        except Exception:  # noqa: BLE001 - a damaged state is no reason to send
            return []
        state = getattr(snapshot, "state", None)
        if not isinstance(state, Mapping):
            return []
        out: list[dict[str, Any]] = []
        for kind in ("progress", "concerns"):
            for item in state.get(kind) or ():
                if not isinstance(item, Mapping):
                    continue
                if str(item.get("state") or "").lower() in (
                    "done", "cancelled", "canceled", "closed", "ended",
                ):
                    continue
                if not str(item.get("text") or "").strip():
                    continue
                updated = _parse_iso(item.get("updated_at") or item.get("created_at"))
                age_hours = None
                if updated is not None:
                    age_hours = (moment - updated).total_seconds() / 3600.0
                record = dict(item)
                record["kind"] = kind
                record["_age_hours"] = age_hours
                full_keys: list[str] = []
                episode = None
                for value in item.get("source_event_ids") or ():
                    text_key = str(value or "").strip()
                    if not text_key:
                        continue
                    entry = fact_index.get(text_key)
                    if entry is not None:
                        episode = episode or entry.get("episode_id")
                        if ":" in text_key:
                            full_keys.append(text_key)
                        else:
                            full_keys.extend(
                                key for key, item_entry in fact_index.items()
                                if item_entry is entry and ":" in key
                            )
                    elif ":" in text_key:
                        full_keys.append(text_key)
                record["_source_keys"] = list(dict.fromkeys(full_keys))
                record["_episode_id"] = episode
                out.append(record)
        return out

    def _thread_open(self, character: str, subject: str, thread_id: str, scene: str = "", revision: str | None = None) -> bool:
        wanted = str(thread_id)
        for item in self._open_threads(character, subject, self._now(), scene):
            if str(item.get("id") or "") == wanted:
                return not revision or item.get("updated_at") == revision
        return False

    def _reflection_rows(self, character: str) -> list[Mapping[str, Any]]:
        if self._reflections is None:
            return []
        try:
            rows = self._reflections.load(character) or ()
        except Exception:  # noqa: BLE001
            return []
        live = {"pending", "confirmed", "promoted"}
        return [
            row for row in rows
            if isinstance(row, Mapping)
            and str(row.get("status") or "pending") in live
        ]

    def _growth_rows(self, character: str, *, kind: str) -> list[Mapping[str, Any]]:
        if self._reflections is None:
            return []
        try:
            rows = self._reflections.growth_items(character) or ()
        except Exception:  # noqa: BLE001 - an unreadable growth list is no reason
            return []
        return [
            row for row in rows
            if isinstance(row, Mapping)
            and str(row.get("memory_kind") or row.get("kind") or "") == kind
        ]

    def _surface_reflections(
        self, character: str, reflection_ids: Sequence[str],
    ) -> None:
        if self._reflections is None:
            return
        for reflection_id in reflection_ids:
            try:
                self._reflections.record_surfaced(character, str(reflection_id))
            except Exception:  # noqa: BLE001 - cooldown bookkeeping is best effort
                continue

    # -- internals: local time and caps ------------------------------------

    def _local(self, moment: datetime) -> datetime:
        """Local wall time for quiet hours and the daily cap.

        A fixed offset, because no timezone database is available here; the
        caller states where the user is (see ProactiveSettings).
        """
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(
            timezone(timedelta(minutes=self._settings.user_utc_offset_minutes)),
        )

    def _in_quiet_hours(self, local: datetime) -> bool:
        """True inside [quiet_start_hour, quiet_end_hour), wrapping midnight.

        Half-open on purpose: with the default 00:00-09:00 window, 23:59 is quiet
        and 09:00 is not. A window whose end precedes its start is read as
        wrapping past midnight, which is the only reading that makes 23:00-07:00
        behave like the operator meant.
        """
        start = self._settings.quiet_start_hour
        end = self._settings.quiet_end_hour
        if start == end:
            return False
        hour = local.hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _sent_rows(self, character: str) -> list[Mapping[str, Any]]:
        return self._store.list(
            character, statuses=[STATUS_SENT], limit=SENT_LOOKBACK,
        )

    def _sent_on_local_day(self, character: str, day: Any) -> int:
        count = 0
        for row in self._sent_rows(character):
            sent_at = _parse_iso(row.get("sent_at"))
            if sent_at is None:
                continue
            if self._local(sent_at).date() == day:
                count += 1
        return count

    def _last_sent_at(self, character: str) -> str | None:
        stamps = [
            str(row.get("sent_at") or "") for row in self._sent_rows(character)
            if row.get("sent_at")
        ]
        return max(stamps) if stamps else None

    # -- internals: writes --------------------------------------------------

    def _live(self, character: str, candidate_id: str) -> dict[str, Any] | None:
        row = self._store.get(character, str(candidate_id or "").strip())
        if row is None:
            return None
        if str(row.get("status")) not in LIVE_STATUSES:
            return None
        return row

    def _not_live(self, character: str, candidate_id: str) -> ProactiveReceipt:
        row = self._store.get(character, str(candidate_id or "").strip())
        if row is None:
            return ProactiveReceipt(ok=False, reason=REASON_UNKNOWN_CANDIDATE)
        return ProactiveReceipt(
            ok=False, status=str(row.get("status")), reason=REASON_NOT_LIVE,
            candidate_id=str(candidate_id),
            candidate=CandidateRecord(row).to_public(),
        )

    def _absorb(
        self, character: str, row: Mapping[str, Any], request_id: str,
    ) -> dict[str, Any]:
        """Fold a candidate into the user own turn; the reason is recorded."""
        updated = dict(row)
        updated.update({
            "status": STATUS_MERGED,
            "absorbed_into": str(request_id or "user_turn")[:400],
        })
        return self._write(character, updated)

    def _mark(
        self, character: str, row: Mapping[str, Any], **fields: Any,
    ) -> dict[str, Any]:
        updated = dict(row)
        updated.update(fields)
        return self._write(character, updated)

    def _write(self, character: str, row: Mapping[str, Any]) -> dict[str, Any]:
        """Journal then persist: the entry is the repair source for a crash."""
        payload = dict(row)
        payload["updated_at"] = self._iso()
        if self._journal is None:
            return self._store.write(character, payload)
        self._journal.record_and_save(
            character, EVT_PROACTIVE_CANDIDATE, snapshot_payload(payload),
            load_view=self._store.load,
            mutate_view=lambda view: self._store.mutate(view, payload),
            save_view=self._store.save,
        )
        return (
            self._store.get(character, str(payload.get("candidate_id") or ""))
            or payload
        )


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


#: Shortest shared run of characters that means "the same topic" here.
_MIN_TOPIC_RUN = 2


def _topics_overlap(left: Any, right: Any) -> bool:
    """Longest-common-run overlap: a cheap, deterministic "same topic" signal.

    Deliberately not a model call and not a keyword list. The recheck only has to
    answer "did the user just open this same thing", and the two costs are
    asymmetric: a wrong yes joins one sentence she was going to say anyway, a
    wrong no repeats a question the user already answered.

    A shared run of two characters is the shortest meaningful unit in Chinese
    ("面试" in "面试结果出来了" and "面试怎么样了"), while two unrelated sentences
    almost never share one. Bigram-set overlap was rejected: it needs two shared
    bigrams, which a two-character topic can never reach.
    """
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return False
    if a in b or b in a:
        return min(len(a), len(b)) >= _MIN_TOPIC_RUN
    return _longest_common_run(a, b) >= _MIN_TOPIC_RUN


def _longest_common_run(left: str, right: str) -> int:
    """Length of the longest common substring (single rolling row)."""
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    best = 0
    for char in left:
        current = [0] * (len(right) + 1)
        for index, other in enumerate(right, start=1):
            if char == other:
                current[index] = previous[index - 1] + 1
                if current[index] > best:
                    best = current[index]
        previous = current
    return best


def _candidate_haystack(row: Mapping[str, Any]) -> str:
    parts = [
        str(row.get("text") or ""),
        str(row.get("why") or ""),
        str(row.get("intent") or ""),
    ]
    return " ".join(part for part in parts if part)


def _scene_matches(row: Mapping[str, Any], scene: str) -> bool:
    wanted = str(scene or "").strip()
    stored = str(row.get("scene") or "").strip()
    if not wanted or not stored:
        return True
    return wanted == stored


def _append(current: Any, extra: str) -> str:
    base = str(current or "").strip()
    addition = str(extra or "").strip()
    if not addition:
        return base[:400]
    return (base + " " + addition).strip()[:400]


def _union(first: Any, second: Any, cap: int) -> list[str]:
    out: list[str] = []
    for bucket in (first, second):
        for item in bucket or ():
            value = str(item).strip()
            if value and value not in out:
                out.append(value)
            if len(out) >= cap:
                return out
    return out


def _source_digest(
    keys: Sequence[str], facts: Sequence[str], refs: Sequence[str],
    thread: str | None, episode: str | None,
) -> str:
    blob = "|".join([
        ",".join(sorted(keys)), ",".join(sorted(facts)),
        ",".join(sorted(refs)), str(thread or ""), str(episode or ""),
    ])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _thread_sentence(text: str, next_step: str) -> str:
    """A short follow-up line built from the item and its next step.

    Deterministic on purpose: wording a candidate with a model would make every
    scheduling tick a model call, and the design puts candidate maintenance in
    the background pass rather than at the front of a turn.
    """
    body = text.strip().rstrip("。.！!？?")
    step = next_step.strip().rstrip("。.！!？?")
    if not step:
        return body
    return body + "——" + step + "，怎么样了？"


__all__ = [
    "DISPATCH_ABSORBED",
    "DISPATCH_DEFER",
    "DISPATCH_IDLE",
    "DISPATCH_REFUSED",
    "DISPATCH_SEND",
    "EVT_PROACTIVE_CANDIDATE",
    "ProactiveReceipt",
    "ProactiveService",
    "ProactiveSettings",
    "REASON_DAILY_CAP",
    "REASON_FACT_NOT_ELIGIBLE",
    "REASON_MIN_GAP",
    "REASON_NO_SOURCE",
    "REASON_NOT_MATURE",
    "REASON_QUIET_HOURS",
    "REASON_TOPIC_CHANGED",
    "REASON_USER_JUST_SPOKE",
]
