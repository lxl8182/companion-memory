"""B04 recent-store adapters: deletion cleanup and message helpers.

The deletion engine's \`recent\` step is registered with one of these cleaners
(design §17.15.5). Fact-entry deletion prunes derived summaries; an explicit
source or episode deletion removes its original messages too, and the engine deletes
the time-index rows in the same step (both must succeed before the step is
\`completed\`).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..persistence.recent import RecentStore


class RecentDeletionCleaner:
    """Deletion-engine cleaner over \`recent.json\` (B04 registration)."""

    def __init__(self, recent: RecentStore, barrier: Any = None) -> None:
        self._recent = recent
        self._barrier = barrier

    def cleanup(
        self, character: str, resolved: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Apply the deletion scope to recent messages/summaries (idempotent)."""
        scope = str(resolved.get("scope") or "entry")
        episode_ids = [
            str(value) for value in resolved.get("episode_ids") or ()
        ]
        source_keys = [
            str(value) for value in resolved.get("fact_source_keys") or ()
        ]
        texts = [str(value) for value in resolved.get("texts") or ()]
        original_sources = list(resolved.get("original_source_keys") or ())
        barrier_removed = 0
        if scope == "episode" or original_sources:
            if self._barrier is not None:
                current = self._recent.messages(character)
                visible = self._barrier.filter_messages(character, current)
                if visible != current:
                    barrier_removed = len(current) - len(visible)
                    self._recent.replace(character, visible)
            detail = self._recent.delete_episode(
                character,
                episode_ids=episode_ids if scope == "episode" else (),
                source_keys=source_keys if scope == "episode" else original_sources,
                match_texts=texts,
            )
        else:
            detail = self._recent.filter_summaries(
                character, match_texts=texts, deleted_source_keys=source_keys,
            )
        return {"status": "completed", "detail": dict(detail, barrier_removed=barrier_removed)}


__all__ = ["RecentDeletionCleaner"]
