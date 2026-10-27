"""B07 outbox dispatch: op-type registry, at-least-once replay, dead letters.

B02's OutboxStore only persists and scans (persistence/outbox.py:1-18); the
host keeps dispatch in app/memory_server/outbox_infra.py. This module is the
core equivalent: a registry from op type to an async handler, plus one
bounded run over a character's pending queue.

Semantics (B07 spec section 3.2)
--------------------------------
* At-least-once: a handler must be idempotent; a successful handler marks the
  op done, a raising handler records one failed attempt via OutboxStore.fail.
* Dead letters: once an op reaches max_attempts it is never executed again;
  fail() appends a terminal dead_letter record (the op is kept on disk so an
  operator or host can still inspect/retry it).
* Unknown op types stay pending and are counted as skipped: the host keeps
  forward-compatible ops for adapters that may register later
  (outbox_infra.py:75-78).
* Bulk handlers: some op families are cheaper to drain in one service call
  (extract_facts -> ExtractionService.process_outbox). A bulk handler is
  called once per run with the character and is responsible for
  completing/failing its own ops; the dispatcher then re-scans the queue to
  report processed/failed/pending.
* Locale-style ops (persist_prompt_locale) have no core handler; the host
  adapter registers them through register(), otherwise they are skipped.

No model, storage or host imports live here: the dispatcher only talks to the
injected outbox store, so maintenance can run it in a temporary root.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

#: Handler result statuses that count as a failed attempt (instead of raising).
_FAILURE_STATUSES = frozenset({"failed", "error", "blocked", "rejected"})

BulkHandler = Callable[[str], Awaitable[Mapping[str, Any]]]
OpHandler = Callable[[str, Any], Awaitable[Mapping[str, Any]]]


class OutboxDispatcher:
    """Registered, bounded dispatch over one character's pending outbox.

    Constructor accepts optional initial registries so composition can wire
    everything in one call; register()/register_bulk() add or replace entries.
    A bulk handler wins over an op handler for the same op type.
    """

    def __init__(
        self,
        *,
        outbox: Any,
        bulk_handlers: Mapping[str, BulkHandler] | None = None,
        op_handlers: Mapping[str, OpHandler] | None = None,
        max_attempts: int = 5,
    ) -> None:
        if outbox is None:
            raise ValueError("outbox is required")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise ValueError("max_attempts must be an integer")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._outbox = outbox
        self._max_attempts = max_attempts
        self._bulk_handlers: dict[str, BulkHandler] = {}
        self._op_handlers: dict[str, OpHandler] = {}
        for op_type, handler in (bulk_handlers or {}).items():
            self.register_bulk(op_type, handler)
        for op_type, handler in (op_handlers or {}).items():
            self.register(op_type, handler)

    # -- registry -------------------------------------------------------

    def register_bulk(self, op_type: str, handler: BulkHandler) -> None:
        """Register a handler that drains every pending op of this type."""
        _validate_registration(op_type, handler)
        self._op_handlers.pop(op_type, None)
        self._bulk_handlers[op_type] = handler

    def register(self, op_type: str, handler: OpHandler) -> None:
        """Register a per-op handler (character, payload) -> result mapping."""
        _validate_registration(op_type, handler)
        self._bulk_handlers.pop(op_type, None)
        self._op_handlers[op_type] = handler

    @property
    def bulk_types(self) -> tuple[str, ...]:
        return tuple(self._bulk_handlers)

    @property
    def op_types(self) -> tuple[str, ...]:
        return tuple(self._op_handlers)

    def has_handler(self, op_type: str) -> bool:
        return op_type in self._bulk_handlers or op_type in self._op_handlers

    # -- dispatch -------------------------------------------------------

    async def run_pending(
        self, character: str, limit: int = 20,
    ) -> dict[str, int]:
        """Process up to limit pending ops; never raises for handler failures.

        Returns {processed, failed, pending, skipped, dead_letter}:
        processed = ops completed this run, failed = attempt records written
        this run, pending = pending ops still observed afterwards, skipped =
        unknown op types left pending, dead_letter = ops newly terminal.
        """
        character = _character(character)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if limit < 0:
            raise ValueError("limit must be nonnegative")
        stats = {
            "processed": 0, "failed": 0, "pending": 0,
            "skipped": 0, "dead_letter": 0,
        }
        if limit == 0:
            return stats

        snapshot = list(self._outbox.pending(character, limit=limit))
        active: list[Any] = []
        for op in snapshot:
            if op.attempts >= self._max_attempts:
                # A crash after the last failed attempt but before the dead
                # letter record: finalize without executing the handler.
                stats["failed"] += 1
                if self._fail(character, op.op_id, "max_attempts_exhausted"):
                    stats["dead_letter"] += 1
                    self._ensure_terminal(character, op.op_id)
                continue
            active.append(op)

        bulk_groups: dict[str, list[Any]] = {}
        for op in active:
            if op.op_type in self._bulk_handlers:
                bulk_groups.setdefault(op.op_type, []).append(op)

        for op_type, ops in bulk_groups.items():
            handler = self._bulk_handlers[op_type]
            try:
                result = await _invoke(handler, character)
            except Exception as exc:  # noqa: BLE001 - handler failure is per-op
                for op in ops:
                    stats["failed"] += 1
                    count = self._fail(character, op.op_id, _error_text(exc))
                    if count is not None and count >= self._max_attempts:
                        stats["dead_letter"] += 1
                        self._ensure_terminal(character, op.op_id)
                continue
            after_ids = {
                op.op_id for op in self._outbox.pending(character, limit=limit)
            }
            removed = [op for op in ops if op.op_id not in after_ids]
            still = [op for op in ops if op.op_id in after_ids]
            dead = 0
            reported_failed = 0
            if isinstance(result, Mapping):
                dead = _nonnegative_int(result.get("dead_letter"))
                reported_failed = _nonnegative_int(result.get("failed"))
            dead = min(dead, len(removed))
            stats["processed"] += len(removed) - dead
            stats["failed"] += dead
            stats["dead_letter"] += dead
            stats["failed"] += min(reported_failed, len(still))

        for op in active:
            if op.op_type in self._bulk_handlers:
                continue
            handler = self._op_handlers.get(op.op_type)
            if handler is None:
                stats["skipped"] += 1
                continue
            try:
                result = await _invoke(handler, character, op.payload)
                if (
                    isinstance(result, Mapping)
                    and str(result.get("status") or "").strip().lower()
                    in _FAILURE_STATUSES
                ):
                    raise RuntimeError(
                        str(result.get("reason") or result.get("status"))
                    )
            except Exception as exc:  # noqa: BLE001 - failed attempt
                stats["failed"] += 1
                count = self._fail(character, op.op_id, _error_text(exc))
                if count is not None and count >= self._max_attempts:
                    stats["dead_letter"] += 1
                    self._ensure_terminal(character, op.op_id)
                continue
            try:
                self._outbox.complete(character, op.op_id)
            except Exception as exc:  # noqa: BLE001 - completion failure
                stats["failed"] += 1
                count = self._fail(character, op.op_id, _error_text(exc))
                if count is not None and count >= self._max_attempts:
                    stats["dead_letter"] += 1
                    self._ensure_terminal(character, op.op_id)
                continue
            stats["processed"] += 1

        stats["pending"] = len(
            self._outbox.pending(character, limit=max(limit, 1))
        )
        return stats

    # -- internals ------------------------------------------------------

    def _fail(self, character: str, op_id: str, error: str) -> int | None:
        try:
            return int(self._outbox.fail(character, op_id, error=error))
        except Exception:  # noqa: BLE001 - store failure must not hide the run
            return None

    def _ensure_terminal(self, character: str, op_id: str) -> None:
        """Force the store to write its terminal record at our threshold.

        OutboxStore.fail() dead-letters at the store's own max_attempts. When
        the dispatcher threshold is lower (a test/wiring mismatch), keep
        failing the op until the store marks it terminal so it cannot replay.
        Bounded: a broken store leaves the op pending instead of looping.
        """
        for _ in range(64):
            try:
                pending = {
                    op.op_id
                    for op in self._outbox.pending(character, limit=1000)
                }
            except Exception:  # noqa: BLE001
                return
            if op_id not in pending:
                return
            try:
                self._outbox.fail(
                    character, op_id, error="max_attempts_exhausted",
                )
            except Exception:  # noqa: BLE001
                return


def _character(character: Any) -> str:
    value = str(character or "").strip()
    if not value:
        raise ValueError("character must be nonempty")
    return value


def _validate_registration(op_type: Any, handler: Any) -> None:
    if not isinstance(op_type, str) or not op_type.strip():
        raise ValueError("op_type must be a nonempty string")
    if not callable(handler):
        raise ValueError(f"handler for {op_type!r} must be callable")


async def _invoke(handler: Any, *args: Any) -> Any:
    """Call a sync or async handler and await only when needed."""
    result = handler(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def _error_text(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".strip()
    return text[:200]


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "BulkHandler",
    "OpHandler",
    "OutboxDispatcher",
]
