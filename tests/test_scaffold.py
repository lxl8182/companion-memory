import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from rin_memory import (
    ContextRequest, MemoryEngine, MemoryEvent, PersonaProfile, PersonaSection, Scene, Scope,
)
from rin_memory.adapters.in_memory import DemoEventContext, InMemoryStore
from rin_memory.errors import SourceConflictError


class ScaffoldTests(unittest.IsolatedAsyncioTestCase):
    def event(self, scope: Scope, key: str, **overrides) -> MemoryEvent:
        return MemoryEvent(
            scope, key, "user_message", key,
            datetime(2026, 10, 22, tzinfo=timezone.utc), **overrides,
        )

    async def test_retry_is_idempotent_but_changed_source_is_rejected(self):
        store = InMemoryStore()
        event = self.event(Scope("one"), "same-source")
        self.assertEqual((await store.append(event)).status, "accepted")
        self.assertEqual((await store.append(event)).status, "duplicate")
        with self.assertRaises(SourceConflictError):
            await store.append(replace(event, text="changed"))
        other = replace(event, scope=Scope("two"))
        self.assertEqual((await store.append(other)).status, "accepted")
        self.assertEqual(await store.list_events(event.scope), (event,))

    async def test_context_keeps_subject_and_scene_boundaries(self):
        scope = Scope("one", "alice")
        game = Scene(plugin_id="game-adapter", game_id="game-a")
        run = replace(game, session_id="run-1")
        profile = PersonaProfile("one", 1, (
            PersonaSection("identity", "identity"),
            PersonaSection("alice", "alice-only", subject="alice"),
            PersonaSection("bob", "bob-only", subject="bob"),
            PersonaSection("game", "game-only", applies_to=game),
        ))
        store = InMemoryStore((profile,))
        for event in (
            self.event(scope, "shared-from-game", scene=game),
            self.event(scope, "game", scene=game, applies_to=game),
            self.event(scope, "run", scene=run, applies_to=run),
            self.event(Scope("one", "bob"), "bob"),
            self.event(Scope("two", "alice"), "other-character"),
        ):
            await store.append(event)
        engine = MemoryEngine(events=store, profiles=store, context=DemoEventContext(store))
        chat = await engine.build_context(ContextRequest(scope, "hello"))
        self.assertEqual([item.source_key for item in chat.items], ["shared-from-game"])
        self.assertEqual(chat.persona, "identity\nalice-only")
        next_run = await engine.build_context(ContextRequest(
            scope, "hello", scene=replace(game, session_id="run-2"),
        ))
        self.assertEqual([item.source_key for item in next_run.items], ["game", "shared-from-game"])
        self.assertEqual(next_run.persona, "identity\nalice-only\ngame-only")

    async def test_optional_recall_timeout_preserves_persona_and_cancels_provider(self):
        cancelled = asyncio.Event()

        class WaitingContext:
            async def recall(self, request):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        profile = PersonaProfile("one", 3, (PersonaSection("identity", "identity"),))
        store = InMemoryStore((profile,))
        engine = MemoryEngine(events=store, profiles=store, context=WaitingContext())
        bundle = await engine.build_context(ContextRequest(Scope("one"), "hi", budget_ms=20))
        self.assertTrue(cancelled.is_set())
        self.assertEqual(bundle.recall_status, "timed_out")
        self.assertEqual(bundle.persona, "identity")
        self.assertEqual(bundle.persona_revision, 3)
        self.assertEqual(bundle.items, ())


if __name__ == "__main__":
    unittest.main()
