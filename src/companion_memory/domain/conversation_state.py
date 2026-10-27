"""Apply source-bound conversation updates to the existing companion state."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .extraction import _parse_iso, _resolve_event_when


class ConversationState:
    def __init__(self, state: Any, barrier: Any, clock: Any) -> None:
        self.state = state
        self.barrier = barrier
        self.clock = clock

    def snapshot(self, character: str, metadata: Mapping) -> Any:
        return self.state.read(
            character, subject=str(metadata.get("subject") or "private"),
            scene=dict(metadata.get("applies_to") or {}),
        )

    def apply(
        self, character: str, updates: Sequence[Mapping], *,
        metadata: Mapping, source_keys: Sequence[str], occurred_at: Any,
    ) -> int:
        if not updates:
            return 0
        snapshot = self.snapshot(character, metadata)
        if set(source_keys).issubset(set(snapshot.processed_events)):
            return 0
        current = {str(item["id"]): item for item in snapshot.items}
        scene = dict(metadata.get("applies_to") or {})
        subject = str(metadata.get("subject") or "private")
        anchor = occurred_at or self.clock.now()
        ops = []
        for update in updates[:8]:
            op = str(update.get("op") or "")
            if op not in ("set_progress", "set_concern", "close_progress", "close_concern"):
                raise ValueError("unsupported conversation state operation")
            item_id = str(update.get("id") or "")
            existing = current.get(item_id)
            if item_id and existing is None:
                raise ValueError("conversation state target is outside the current scope")
            if existing:
                if str(existing.get("type")) != op.split("_", 1)[1]:
                    raise ValueError("conversation state target kind differs")
                last = _parse_iso(existing.get("event_at"))
                if last and anchor < last:
                    continue
            text = str(update.get("text") or (existing or {}).get("text") or "").strip()
            if not text:
                raise ValueError("conversation state text is required")
            if op.startswith("close_") and existing is None:
                raise ValueError("closing an item requires its current id")
            if not item_id:
                seed = json.dumps([subject, scene, op, text], ensure_ascii=False, sort_keys=True)
                item_id = "thread_" + hashlib.sha256(seed.encode()).hexdigest()[:16]
            row = {
                "op": op, "id": item_id, "text": text[:400], "scene": scene,
                "source_event_ids": list(source_keys),
            }
            if op.startswith("set_"):
                row.update(
                    next_step=str(update.get("next_step") or "")[:400],
                    why=str(update.get("why") or "")[:400],
                    event_at=anchor.isoformat(),
                )
                when = update.get("followup_when")
                if isinstance(when, Mapping):
                    row["followup_at"] = _resolve_event_when({"start": when}, anchor)[0]
                    row["agreed_followup"] = bool(update.get("agreed_followup"))
            self.barrier.assert_row_allowed(character, {
                "subject": subject, "applies_to": scene,
                "source_event_keys": list(source_keys),
            })
            ops.append(row)
        receipt = self.state.commit(character, {
            "subject": subject, "scene": scene, "internal": True,
            "producer": "conversation", "base_version": snapshot.version,
            "request_id": hashlib.sha256("|".join(sorted(source_keys)).encode()).hexdigest(),
            "processed_event_ids": list(source_keys), "ops": ops,
        })
        if not receipt.ok:
            raise ValueError("conversation state commit: " + str(receipt.reason))
        return len(ops) if receipt.applied else 0
