"""Single-process demo storage; no persistence, extraction, or production recovery."""

from ..contracts import (
    ContextItem, ContextRequest, EventReceipt, MemoryEvent, PersonaProfile, Scope,
)
from ..errors import SourceConflictError
from ..scope import scene_matches


class InMemoryStore:
    def __init__(self, profiles: tuple[PersonaProfile, ...] = ()) -> None:
        self._events: dict[tuple[Scope, str], MemoryEvent] = {}
        self._profiles = {profile.character: profile for profile in profiles}

    async def append(self, event: MemoryEvent) -> EventReceipt:
        key = (event.scope, event.source_key)
        previous = self._events.get(key)
        if previous is not None:
            if previous != event:
                raise SourceConflictError("source key already belongs to a different event")
            return EventReceipt(event.source_key, "duplicate", durable=False)
        self._events[key] = event
        return EventReceipt(event.source_key, "accepted", durable=False)

    async def get(self, character: str) -> PersonaProfile | None:
        return self._profiles.get(character)

    async def list_events(self, scope: Scope) -> tuple[MemoryEvent, ...]:
        return tuple(event for event in self._events.values() if event.scope == scope)


class DemoEventContext:
    """Shows recent raw events to exercise wiring; not a memory recall algorithm.

    current_input is intentionally not ranked. Replace this with extracted NEKO
    recall before integration; raw events must not be treated as confirmed facts.
    """

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def recall(self, request: ContextRequest) -> tuple[ContextItem, ...]:
        events = await self._store.list_events(request.scope)
        return tuple(
            ContextItem(
                scope=event.scope, source_key=event.source_key, text=event.text,
                kind="demo_event", applies_to=event.applies_to,
            )
            for event in reversed(events)
            if scene_matches(event.applies_to, request.scene)
        )[:request.max_items]

