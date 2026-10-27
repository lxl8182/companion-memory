"""Run isolated demonstrations. Never reads NEKO user data or starts services."""

import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import ContextRequest, MemoryEngine, MemoryEvent, PersonaProfile, PersonaSection, Scope
from .adapters.composition import build_memory_core
from .adapters.durable import build_durable_event_store
from .adapters.in_memory import DemoEventContext, InMemoryStore
from .contracts import CorrectionRequest, DeletionRequest, DeletionTarget
from .domain.evolution import EvolutionSettings
from .domain.extraction import ExtractionSettings


async def demo() -> dict:
    profile = PersonaProfile(
        character="demo", revision=1,
        sections=(PersonaSection("identity", "我是一个喜欢一起解决问题的伙伴。"),),
    )
    store = InMemoryStore((profile,))
    engine = MemoryEngine(events=store, profiles=store, context=DemoEventContext(store))
    scope = Scope("demo")
    event = MemoryEvent(
        scope=scope, source_key="host:demo:private:example-1",
        event_type="user_message", text="今天一起把项目框架搭起来了。",
        occurred_at=datetime.now(timezone.utc),
    )
    receipt = await engine.observe(event)
    retry = await engine.observe(event)
    context = await engine.build_context(ContextRequest(scope, "刚才做了什么？"))
    return {
        "mode": "in_memory_scaffold", "durable": receipt.durable,
        "receipt": receipt.status, "retry": retry.status,
        "persona": context.persona, "persona_revision": context.persona_revision,
        "items": [{"kind": item.kind, "text": item.text} for item in context.items],
        "recall_status": context.recall_status,
    }


async def durable_demo() -> dict:
    """Prove the B02 receipt path on a throwaway data root.

    Shows the three properties the batch is judged on: the receipt is durable
    only after the journal+ledger commit, a retried commit lands once, and a
    fresh process reading the same root still recognises the duplicate.
    """
    event = MemoryEvent(
        scope=Scope("demo"), source_key="host:demo:private:durable-1",
        event_type="user_message", text="这条事件会真正落盘。",
        occurred_at=datetime.now(timezone.utc), content_class="chat",
    )
    with tempfile.TemporaryDirectory(prefix="rin-memory-demo-") as tmp:
        root = Path(tmp) / "memory"
        sink = build_durable_event_store(memory_root=root)
        first = await sink.append(event)
        retry = await sink.append(event)
        # A new stack on the same root stands in for "restart the process".
        reopened = build_durable_event_store(memory_root=root)
        after_reopen = await reopened.append(event)
        entries = reopened.recovery_state("demo")
        files = sorted(path.name for path in (root / "demo").iterdir())
        return {
            "mode": "durable_temp_root",
            "data_root": str(root),
            "first": {"status": first.status, "durable": first.durable,
                      "position": first.processing_position},
            "retry": {"status": retry.status, "durable": retry.durable},
            "after_reopen": {"status": after_reopen.status, "durable": after_reopen.durable},
            "recovered": None if entries is None else entries.applied,
            "files": files,
        }


async def correction_deletion_demo() -> dict:
    """Prove the B03 loop on a throwaway data root.

    Correction records the new understanding and invalidates the replaced
    fact; deletion registers its barrier before cleanup and a retried ingest
    of the deleted source is rejected instead of resurrecting it. Everything
    is re-read from a fresh stack on the same root.
    """
    with tempfile.TemporaryDirectory(prefix="rin-memory-demo-") as tmp:
        root = Path(tmp) / "memory"
        core = build_memory_core(memory_root=root)
        core.recover("demo")
        event = MemoryEvent(
            scope=Scope("demo"), source_key="host:demo:private:b03-1",
            event_type="user_message", text="我以前住在东京。",
            occurred_at=datetime.now(timezone.utc), content_class="chat",
        )
        accepted = await core.sink.append(event)
        core.facts.save("demo", [
            {"id": "fact_tokyo", "text": "用户以前住在东京",
             "source_event_keys": ["host:demo:private:b03-1"]},
            {"id": "fact_cat", "text": "用户喜欢猫"},
        ])
        correction = await core.correction_service.correct(CorrectionRequest(
            scope=Scope("demo"), action="replace",
            old_text="以前住在东京", new_text="现在住在大阪",
            source_key="host:demo:private:correction-1",
        ))
        deleted = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), source_keys=("host:demo:private:b03-1",),
            reason="demo deletion",
        ))
        blocked = await core.sink.append(event)
        reopened = build_memory_core(memory_root=root)
        receipt_after_reopen = reopened.deletion_service.get(
            "demo", deleted.deletion_id,
        )
        block = reopened.correction_service.block("demo", language="zh")
        return {
            "mode": "correction_and_deletion_temp_root",
            "data_root": str(root),
            "accepted": accepted.status,
            "correction": {
                "correction_id": correction.correction_id,
                "invalidated": list(correction.invalidated_fact_ids),
                "version": correction.version,
                "block": block.block,
            },
            "deletion": {
                "status": deleted.status,
                "stopped_using": deleted.stopped_using,
                "content_cleaned": deleted.content_cleaned,
                "steps_completed": list(deleted.completed),
                "files": sorted(path.name for path in (root / "demo").iterdir()),
            },
            "reingest_after_delete": {
                "status": blocked.status, "reason": blocked.reason,
                "durable": blocked.durable,
            },
            "after_reopen": {
                "status": receipt_after_reopen.status,
                "stopped_using": receipt_after_reopen.stopped_using,
                "facts": [row["id"] for row in reopened.facts.load("demo")],
            },
        }


class _DemoModel:
    """Deterministic offline model stand-in for the B04 demo.

    No network or provider config: the demo only proves the pipeline contract
    (prompt -> JSON candidates -> facts; summary -> memo message), exactly what
    the automated tests do with a fake model.
    """

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        if purpose == "memory_fact_extraction":
            return json.dumps([{
                "text": "用户喜欢在周末去公园跑步",
                "importance": 7,
                "source": "user_observation",
                "event_when": {"start": {"offset": 2, "unit": "day"}, "end": None},
            }], ensure_ascii=False)
        if purpose == "recent_compression":
            return "用户和角色约好周末一起去公园跑步，用户最近在准备一次长跑。"
        return "[]"


async def extraction_demo() -> dict:
    """Prove the B04 loop on a throwaway data root with a stub model.

    A turn reaches recent.json + time_indexed.db, the queued extract_facts op
    runs inline, facts get host field names and a content hash, and the recent
    window compresses into a head memo message (the summarized prefix moves out
    of recent but stays in the time index).
    """
    with tempfile.TemporaryDirectory(prefix="rin-memory-demo-") as tmp:
        root = Path(tmp) / "memory"
        core = build_memory_core(memory_root=root, model=_DemoModel())
        core.recover("demo")
        turn = [
            {"type": "human", "data": {"content": "周末一般去公园跑步"}},
            {"type": "ai", "data": {"content": "跑步的时候记得补水。"}},
        ]
        ingested = await core.extraction.ingest_turn(
            "demo", turn,
            source_key="host:demo:private:b04-1",
            episode_id="ep:host:b04-1:2026-10-22",
            session_id="s1",
        )
        facts = core.facts.load("demo")
        for index in range(22):
            core.recent.append("demo", [{
                "type": "human",
                "data": {"content": f"第 {index + 1} 条日常对话"},
            }])
        compression = await core.compression.compress("demo")
        recent = core.recent.load("demo")
        index = core.timeindex("demo").open()
        try:
            original_rows = index.count_original()
        finally:
            index.close()
        return {
            "mode": "ingest_extract_compress_temp_root",
            "data_root": str(root),
            "ingest": {
                "status": ingested.status,
                "content_class": ingested.content_class,
                "facts_written": ingested.facts_written,
                "op_id": ingested.op_id,
            },
            "facts": [
                {"text": row["text"], "hash": row["hash"],
                 "source": row["source"], "schema_version": row["schema_version"]}
                for row in facts
            ],
            "compression": {
                "status": compression.status,
                "summarized": compression.summarized,
            },
            "recent_head": recent[0]["data"]["content"] if recent else "",
            "recent_count": len(recent),
            "time_index_rows": original_rows,
            "outbox_pending": len(core.outbox.pending("demo")),
        }


class _DemoClock:
    """Fixed clock for the B05-B07 demos (no wall-clock drift in the output)."""

    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
        self._monotonic = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, **kwargs) -> None:
        delta = timedelta(**kwargs)
        self._now += delta
        self._monotonic += delta.total_seconds()


class _DemoReflectionModel:
    """Offline B05 model: one plain reflection, then one growth candidate."""

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        if purpose != "reflection_synthesis":
            return "{}"
        self.calls += 1
        if self.calls == 1:
            return json.dumps({
                "operation": "add",
                "reflection": "主人更喜欢被直接问，而不是被反复试探",
                "topic": "沟通方式",
                "entity": "relationship",
            }, ensure_ascii=False)
        return json.dumps({
            "operation": "add",
            "reflection": "用户周末早晨有晨跑习惯",
            "topic": "运动",
            "entity": "relationship",
            "memory_kind": "habit",
            "claim": "用户周末早晨会晨跑",
            "situation": "周末早晨",
        }, ensure_ascii=False)


async def reflection_growth_demo() -> dict:
    """Prove the B05 loop on a throwaway data root with a stub model.

    Facts form a grounded reflection (deterministic id, absorbed once), delivered
    feedback moves its evidence across the 1.0/2.0 thresholds into a persona
    entry; a second batch forms a growth item that is promoted after independent
    support; a deletion narrows the reflection and retracting the growth stops
    serving its derived persona row. Everything is re-read from a fresh stack.
    """
    with tempfile.TemporaryDirectory(prefix="rin-memory-demo-") as tmp:
        root = Path(tmp) / "memory"
        clock = _DemoClock()
        model = _DemoReflectionModel()
        core = build_memory_core(memory_root=root, clock=clock, model=model)
        core.recover("demo")

        def seed(prefix: str, count: int) -> None:
            existing = core.facts.load("demo")
            existing.extend({
                "id": f"fact_{prefix}_{index}",
                "text": f"用户第 {index + 1} 次聊到{prefix}这件事",
                "importance": 7,
                "source": "user_observation",
                "schema_version": 2,
                "absorbed": False,
                "created_at": "2026-10-20T00:00:00+00:00",
                "source_event_keys": [f"host:demo:private:{prefix}{index}"],
            } for index in range(count))
            core.facts.save("demo", existing)

        seed("a", 5)
        synthesis = await core.reflection_service.synthesize("demo")
        reflection_id = synthesis.reflection_ids[0]
        await core.reflection_service.record_surfaced("demo", reflection_id)
        await core.reflection_service.record_feedback("demo", reflection_id, "confirmed")
        await core.reflection_service.record_feedback("demo", reflection_id, "confirmed")
        confirmed = await core.reflection_service.promote("demo")
        promoted = await core.reflection_service.promote("demo")
        persona_after_reflection = [
            entry["id"] for entry in core.persona_service.entries("demo")
        ]

        seed("b", 5)
        growth_synthesis = await core.reflection_service.synthesize("demo")
        growth_id = growth_synthesis.growth_item_ids[0]
        growth_promotion = await core.reflection_service.promote("demo")
        usage = await core.reflection_service.record_growth_usage(
            "demo", growth_id, "read", source_key="demo:chat:1",
        )
        growth_block = core.reflection_service.growth_block("demo")

        deletion = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"),
            targets=(DeletionTarget("fact", "fact_a_0"),),
            reason="demo partial deletion",
        ))
        retraction = await core.reflection_service.retract_growth("demo", growth_id)

        reopened = build_memory_core(memory_root=root, clock=clock, model=model)
        reopened.recover("demo")
        rows_after = {
            str(row.get("id")): {
                "status": row.get("status"),
                "sources": len(row.get("source_fact_ids") or ()),
                "needs_rebuild": bool(row.get("needs_rebuild")),
            }
            for row in reopened.reflections.load("demo")
        }
        return {
            "mode": "reflection_growth_temp_root",
            "data_root": str(root),
            "synthesis": {
                "status": synthesis.status,
                "reflection_id": reflection_id,
                "absorbed": len(synthesis.absorbed_fact_ids),
            },
            "promotion": {
                "confirmed": list(confirmed.confirmed_ids),
                "promoted": list(promoted.promoted_ids),
                "persona_ids": persona_after_reflection,
            },
            "growth": {
                "item_id": growth_id,
                "status": growth_promotion.status,
                "persona_ids": list(growth_promotion.persona_ids),
                "block": growth_block.block,
                "usage_version": usage.version,
            },
            "deletion": {
                "status": deletion.status,
                "steps": {
                    name: step.get("status")
                    for name, step in (deletion.steps or {}).items()
                },
                "reflections": (deletion.steps or {}).get(
                    "reflections", {},
                ).get("detail"),
                "persona": (deletion.steps or {}).get(
                    "persona", {},
                ).get("detail"),
            },
            "growth_retract": {
                "ok": retraction.get("ok"),
                "persona_retracted": list(retraction.get("persona_retracted") or ()),
            },
            "after_reopen": {
                "reflections": rows_after,
                "persona_entries": [
                    entry["id"] for entry in reopened.persona_service.entries("demo")
                ],
                "growth_items": [
                    row["id"] for row in reopened.reflection_service.growth_items("demo")
                ],
            },
        }


class _DemoEmbedding:
    """Deterministic offline vectors: one axis per demo topic."""

    def is_available(self) -> bool:
        return True

    def model_id(self):
        return "demo-embedding-v1"

    async def embed_batch(self, texts):
        out = []
        for text in texts:
            if "跑步" in text:
                out.append([1.0, 0.0, 0.0])
            elif "猫" in text:
                out.append([0.0, 1.0, 0.0])
            else:
                out.append([0.0, 0.0, 1.0])
        return out


class _DemoTokenCounter:
    """Cheap deterministic token counter for the demo (no host tokenizer)."""

    def count(self, text: str) -> int:
        return max(1, len(text or "") // 2)

    async def acount(self, text: str) -> int:
        return self.count(text)

    def truncate(self, text: str, max_tokens: int) -> str:
        return (text or "")[: max(0, int(max_tokens)) * 2]

    def identity(self) -> str:
        return "demo-tokenizer-v1"


async def recall_demo() -> dict:
    """Prove the B06 recall loop on a throwaway data root.

    Explicit recall ranks BM25/RRF candidates with source keys; the passive turn
    context returns a bounded block for a matching topic and stays empty for a
    new one; an anaphora with recent turns continues the running episode; an
    explicit correction hides the replaced fact while keeping the correction
    block visible.
    """
    with tempfile.TemporaryDirectory(prefix="rin-memory-demo-") as tmp:
        root = Path(tmp) / "memory"
        core = build_memory_core(
            memory_root=root, clock=_DemoClock(),
            embedding_provider=_DemoEmbedding(),
            token_counter=_DemoTokenCounter(),
        )
        core.recover("demo")
        core.facts.save("demo", [
            {
                "id": "fact_run", "text": "用户周末在公园跑步",
                "importance": 8, "source": "user_observation",
                "schema_version": 2, "absorbed": True,
                "created_at": "2026-10-20T08:00:00+00:00",
                "event_start_at": "2026-10-20T08:00:00+00:00",
                "episode_id": "ep:host:e1:2026-10-20",
                "source_event_keys": ["host:demo:private:e1"],
                "embedding": [1.0, 0.0, 0.0],
                "embedding_model_id": "demo-embedding-v1",
            },
            {
                "id": "fact_cat", "text": "用户养了一只叫团子的猫",
                "importance": 7, "source": "user_observation",
                "schema_version": 2, "absorbed": True,
                "created_at": "2026-10-20T08:00:00+00:00",
                "event_start_at": "2026-10-20T08:00:00+00:00",
                "source_event_keys": ["host:demo:private:e2"],
                "embedding": [0.0, 1.0, 0.0],
                "embedding_model_id": "demo-embedding-v1",
            },
        ])
        core.reflections.save("demo", [{
            "id": "ref_run", "text": "用户喜欢周末户外跑步",
            "status": "confirmed", "entity": "relationship",
            "source_fact_ids": ["fact_run"], "reinforcement": 1.2,
            "created_at": "2026-10-21T00:00:00+00:00",
            "source_event_keys": ["host:demo:private:e1"],
            "embedding": [1.0, 0.0, 0.0],
            "embedding_model_id": "demo-embedding-v1",
        }])

        explicit = await core.recall.recall_memory("demo", "团子 猫", purpose="query")
        matched = await core.recall.build_turn_context("demo", current_input="周末跑步")
        new_topic = await core.recall.build_turn_context("demo", current_input="今天天气怎么样")
        continued = await core.recall.build_turn_context(
            "demo", current_input="后来呢？",
            recent_turns=["我们之前聊到周末在公园跑步"],
        )
        await core.correction_service.correct(CorrectionRequest(
            scope=Scope("demo"), old_text="在公园跑步", new_text="在河边跑步",
        ))
        after_correction = await core.recall.build_turn_context(
            "demo", current_input="跑步",
        )
        by_time = await core.recall.recall_by_time(
            "demo",
            time_range=("2026-10-01T00:00:00+00:00", "2026-10-31T00:00:00+00:00"),
        )
        return {
            "mode": "recall_temp_root",
            "data_root": str(root),
            "explicit_recall": {
                "status": explicit["status"],
                "retrieval_mode": explicit["retrieval_mode"],
                "ids": [item["id"] for item in explicit["items"]],
                "source_keys": explicit["items"][0]["source_keys"] if explicit["items"] else [],
            },
            "turn_context": {
                "result": matched["result"],
                "retrieval_mode": matched["retrieval_mode"],
                "memory_ids": matched["memory_ids"],
                "tokens": matched["tokens"],
                "context": matched["context"],
            },
            "new_topic": {
                "result": new_topic["result"],
                "memory_ids": new_topic["memory_ids"],
            },
            "anaphora": {
                "result": continued["result"],
                "memory_ids": continued["memory_ids"],
                "episode_id": continued["episode"].get("episode_id"),
            },
            "after_correction": {
                "result": after_correction["result"],
                "memory_ids": after_correction["memory_ids"],
                "corrections": after_correction["corrections"]["block"],
            },
            "by_time": {
                "status": by_time["status"],
                "ids": [item["id"] for item in by_time["items"]],
            },
        }


async def standalone_closed_loop_demo() -> dict:
    """Prove the B07 closed loop on a throwaway data root.

    One explicit data directory drives the whole core without the host: a lease
    + state commit/read (version, rendered block, mood decay), a queued fact
    extraction drained by the outbox dispatcher, one ordered maintenance pass
    (reconcile/outbox/derive/delete-retry), an entry deletion, and a restart
    that re-reads the same state version.
    """
    with tempfile.TemporaryDirectory(prefix="rin-memory-demo-") as tmp:
        root = Path(tmp) / "memory"
        clock = _DemoClock()
        core = build_memory_core(
            memory_root=root, clock=clock, model=_DemoModel(),
            extraction_settings=ExtractionSettings(inline=False),
        )
        core.recover("demo")
        lease = core.state_service.claim_lease(
            "demo", instance="demo-host", lease_id="demo-lease", ttl_seconds=90,
        )
        committed = core.state_service.commit("demo", {
            "ops": [
                {"op": "set_thought", "text": "用户在准备长跑",
                 "source_event_ids": ["host:demo:private:e1"]},
                {"op": "set_mood", "label": "开心", "intensity": 1.0,
                 "cause": "约好周末一起跑步",
                 "source_event_ids": ["host:demo:private:e1"]},
            ],
            "producer": "host", "instance": "demo-host",
            "lease_id": lease["lease"]["lease_id"],
            "request_id": "demo-state-1", "base_version": 0,
        })
        clock.advance(hours=6)
        decayed = core.state_service.read("demo")
        await core.extraction.ingest_turn(
            "demo",
            [{"type": "human", "data": {"content": "我最近开始晨跑"}}],
            source_key="host:demo:private:e2", session_id="s1",
        )
        dispatched = await core.outbox_dispatcher.run_pending("demo")
        maintenance = await core.maintenance.run_once("demo")
        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"),
            source_keys=("host:demo:private:e2",),
            reason="demo cleanup",
        ))
        after_deletion = await core.maintenance.run_once("demo")
        reopened = build_memory_core(
            memory_root=root, clock=clock, model=_DemoModel(),
            extraction_settings=ExtractionSettings(inline=False),
        )
        recovered = reopened.recover("demo")
        state_after = reopened.state_service.read("demo")
        return {
            "mode": "standalone_closed_loop_temp_root",
            "data_root": str(root),
            "state": {
                "committed_version": committed.version,
                "block": committed.block,
                "mood_after_6h": decayed.block,
                "version_after_decay": decayed.version,
            },
            "outbox": {
                "dispatched": dispatched,
                "facts": [row["text"] for row in core.facts.load("demo")],
            },
            "maintenance": {
                "status": maintenance.status,
                "steps": {
                    name: step.get("status")
                    for name, step in maintenance.steps.items()
                },
                "deletion_retry_after_delete": after_deletion.steps.get(
                    "deletion_retry", {},
                ).get("status"),
            },
            "after_reopen": {
                "recovered": recovered.applied,
                "state_version": state_after.version,
                "state_block": state_after.block,
            },
        }


async def persona_card_demo() -> dict:
    """Prove the B10 card loop on a throwaway data root.

    One-time import, a local revision under base_revision CAS, a freeze that
    only blocks automatic edits, the characters.json-shaped projection, a
    source deletion that removes the dependent section, and a restart that
    keeps the current revision.
    """
    with tempfile.TemporaryDirectory(prefix="rin-memory-demo-") as tmp:
        root = Path(tmp) / "memory"
        clock = _DemoClock()
        core = build_memory_core(memory_root=root, clock=clock)
        core.recover("demo")
        card_sections = [
            {"section_id": "identity", "kind": "identity",
             "text": "你是一只喜欢一起解决问题的猫娘。",
             "editable_by": {"user": True, "evolution": False}},
            {"section_id": "tone", "kind": "style", "text": "说话温柔，偶尔开玩笑。",
             "source_refs": ["fact_tone"],
             "editable_by": {"user": True, "evolution": True}},
        ]
        imported = core.persona_card_service.import_initial("demo", card_sections)
        revised = core.persona_card_service.revise("demo", {
            "base_revision": imported.revision,
            "operations": [{"op": "replace", "section_id": "identity",
                            "text": "你是一只喜欢被直接问的猫娘。"}],
            "reason": "user edit",
        })
        frozen = core.persona_card_service.freeze("demo", "identity", True)
        auto = core.persona_card_service.revise("demo", {
            "base_revision": frozen.revision, "origin": "evolution",
            "operations": [{"op": "replace", "section_id": "identity",
                            "text": "自动改写"}],
        })
        projection = core.persona_card_service.render_projection("demo")
        core.facts.save("demo", [{
            "id": "fact_tone", "text": "用户喜欢温柔的语气", "importance": 7,
            "schema_version": 2, "absorbed": False,
            "source_event_keys": ["host:demo:private:tone"],
        }])
        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), targets=(DeletionTarget("fact", "fact_tone"),),
            reason="demo cleanup",
        ))
        after_delete = core.persona_card_service.render_projection("demo")
        reopened = build_memory_core(memory_root=root, clock=clock)
        reopened.recover("demo")
        return {
            "mode": "persona_card_temp_root",
            "data_root": str(root),
            "imported": {
                "status": imported.status,
                "revision": imported.revision,
                "sections": [section["section_id"] for section in card_sections],
            },
            "revised": {
                "status": revised.status,
                "revision": revised.revision,
                "projection": projection,
            },
            "freeze": {
                "status": frozen.status,
                "auto_revision_status": auto.status,
                "auto_revision_reason": auto.reason,
            },
            "after_source_delete": after_delete,
            "after_reopen": {
                "revision": reopened.persona_card_service.get("demo").revision,
                "projection": reopened.persona_card_service.render_projection("demo"),
            },
        }


async def persona_publish_demo() -> dict:
    """Prove the B11 publish loop on a throwaway data root.

    A revision is published as a persisted version token per target; consumers
    report what they actually rendered, a realtime republish supersedes the old
    session, the game planner poll pairs on the version, and rollback restores
    an earlier version only when explicitly allowed.
    """
    with tempfile.TemporaryDirectory(prefix="rin-memory-demo-") as tmp:
        root = Path(tmp) / "memory"
        clock = _DemoClock()
        core = build_memory_core(memory_root=root, clock=clock)
        core.recover("demo")
        imported = core.persona_card_service.import_initial("demo", [
            {"section_id": "identity", "text": "你是一只猫娘。",
             "editable_by": {"user": True, "evolution": False}},
            {"section_id": "tone", "text": "说话温柔。",
             "editable_by": {"user": True, "evolution": True}},
        ])
        revised = core.persona_card_service.revise("demo", {
            "base_revision": imported.revision,
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": "说话温柔，偶尔开玩笑。"}],
        })
        next_turn = core.persona_publish.publish(
            "demo", target="next_turn", revision=revised.revision,
        )
        realtime = core.persona_publish.publish(
            "demo", target="realtime", revision=revised.revision,
        )
        matched = core.persona_publish.consume(
            "demo", "realtime:session-a", expected_version=realtime.version,
        )
        newer = core.persona_card_service.revise("demo", {
            "base_revision": revised.revision,
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": "说话非常温柔。"}],
        })
        republished = core.persona_publish.publish(
            "demo", target="realtime", revision=newer.revision,
        )
        superseded = core.persona_publish.consume(
            "demo", "realtime:session-a", expected_version=realtime.version,
        )
        game = core.persona_publish.publish(
            "demo", target="game", revision=newer.revision,
        )
        planner_match = core.persona_publish.pair_planner(
            "demo", game.snapshot.planner_version,
        )
        planner_stale = core.persona_publish.pair_planner("demo", "planner:stale")
        rollback = core.persona_publish.rollback(
            "demo", next_turn.version, allow_consumed=True,
        )
        reopened = build_memory_core(memory_root=root, clock=clock)
        reopened.recover("demo")
        current = reopened.persona_publish.current("demo")
        return {
            "mode": "persona_publish_temp_root",
            "data_root": str(root),
            "next_turn": {
                "status": next_turn.status,
                "version": next_turn.version,
                "revision": next_turn.revision,
                "safe_boundary": next_turn.boundary.get("safe_boundary"),
            },
            "realtime": {
                "status": realtime.status,
                "safe_boundary": realtime.boundary.get("safe_boundary"),
                "consumer_match": matched.status,
                "consumer_superseded": superseded.status,
                "republished_version": republished.version,
            },
            "game": {
                "status": game.status,
                "planner_match": planner_match["status"],
                "planner_stale": planner_stale["status"],
                "planner_version": game.snapshot.planner_version,
            },
            "rollback": {
                "status": rollback.status,
                "version": rollback.version,
                "revision": rollback.revision,
            },
            "after_reopen": {
                "available": current.available,
                "version": current.version,
                "revision": current.revision,
            },
        }


class _EvolutionModel:
    """Deterministic stand-in for the host model: returns one JSON proposal."""

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls += 1
        return (
            '{"operations":[{"op":"replace","section_id":"tone",'
            '"text":"用户喜欢温柔、具体的语气。"}],'
            '"reason":"证据显示用户偏好具体的温柔表达"}'
        )

def _demo_understanding(fact_id: str) -> dict:
    """A matured understanding for the demo trajectories.

    M4 (design 9.3 / G15): a raw fact explains a proposal but can never qualify
    for one (`fact_not_eligible`). A real conversation reaches this row through
    the growth gate, so the demo seeds it directly -- two independent
    experiences, status `confirmed`, with the fact kept as provenance.
    """
    return {
        "id": "ref_tone",
        "text": "用户在沟通上偏好温柔、具体的语气",
        "status": "confirmed",
        "entity": "relationship",
        "source_fact_ids": [fact_id],
        "source_event_keys": ["host:demo:private:tone", "host:demo:private:tone2"],
        "support_event_keys": ["host:demo:private:tone", "host:demo:private:tone2"],
        "support_basis": [
            {"experience": "episode:demo-tone-1", "facts": [fact_id],
             "support": [{"key": "host:demo:private:tone", "fact_id": fact_id,
                         "kind": "explicit_feedback",
                         "flags": ["user_feedback"]}]},
            {"experience": "episode:demo-tone-2", "facts": [fact_id],
             "support": [{"key": "host:demo:private:tone2", "fact_id": fact_id,
                         "kind": "explicit_feedback",
                         "flags": ["user_feedback"]}]},
        ],
        "support_count": 2,
        "created_at": "2026-10-20T00:00:00+00:00",
        "updated_at": "2026-10-20T00:00:00+00:00",
    }


async def persona_evolution_demo() -> dict:
    """Prove the B12 proposal loop: the model proposes, validation decides.

    The card is never written by this path; a second identical request is a
    duplicate, and a frozen section rejects automatic proposals.
    """
    with tempfile.TemporaryDirectory(prefix="rin-memory-demo-") as tmp:
        root = Path(tmp) / "memory"
        clock = _DemoClock()
        model = _EvolutionModel()
        core = build_memory_core(
            memory_root=root, clock=clock, model=model,
            evolution_settings=EvolutionSettings(enabled=True, tier="summary"),
        )
        core.recover("demo")
        core.persona_card_service.import_initial("demo", [
            {"section_id": "identity", "text": "你是一只猫娘。",
             "editable_by": {"user": True, "evolution": False}},
            {"section_id": "tone", "text": "说话温柔。",
             "source_refs": ["fact_tone"],
             "editable_by": {"user": True, "evolution": True}},
        ])
        core.facts.save("demo", [{
            "id": "fact_tone", "text": "用户喜欢温柔的语气", "importance": 7,
            "schema_version": 2, "absorbed": False,
            "source_event_keys": ["host:demo:private:tone"],
        }])
        core.reflections.save("demo", [_demo_understanding("fact_tone")])
        refs = ["ref_tone", "fact_tone"]
        proposed = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": refs,
            "reason": "demo proposal",
        })
        duplicate = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": refs,
            "reason": "demo proposal", "request_id": "demo-request",
        })
        again = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": refs,
            "reason": "demo proposal", "request_id": "demo-request",
        })
        frozen = core.persona_card_service.freeze("demo", "tone", True)
        blocked = await core.evolution_service.propose("demo", {
            "base_revision": frozen.revision, "source_refs": refs,
        })
        return {
            "mode": "persona_evolution_temp_root",
            "data_root": str(root),
            "model_calls": model.calls,
            "proposed": {
                "status": proposed.status,
                "proposal_id": proposed.proposal_id,
                "card_revision": core.persona_card_service.get("demo").revision,
            },
            "request_id_duplicate": {
                "first": duplicate.status,
                "second": again.status,
                "same_proposal": duplicate.proposal_id == again.proposal_id,
            },
            "frozen_gate": {
                "status": blocked.status,
                "reason": blocked.reason,
            },
            "stored_proposals": [
                item["status"] for item in core.evolution_service.list("demo")
            ],
        }


async def persona_admin_demo() -> dict:
    """Prove the B13 review loop: accept, reject, diff, rollback and switch.

    Acceptance goes through the card service (base_revision/frozen checks), the
    card is only changed by that explicit action, and the switch is persisted
    separately from the card.
    """
    with tempfile.TemporaryDirectory(prefix="rin-memory-demo-") as tmp:
        root = Path(tmp) / "memory"
        clock = _DemoClock()
        model = _EvolutionModel()
        core = build_memory_core(
            memory_root=root, clock=clock, model=model,
            evolution_settings=EvolutionSettings(enabled=True),
        )
        core.recover("demo")
        core.persona_card_service.import_initial("demo", [
            {"section_id": "identity", "text": "你是一只猫娘。",
             "editable_by": {"user": True, "evolution": False}},
            {"section_id": "tone", "text": "说话温柔。",
             "source_refs": ["fact_tone"],
             "editable_by": {"user": True, "evolution": True}},
        ])
        core.facts.save("demo", [{
            "id": "fact_tone", "text": "用户喜欢温柔的语气", "importance": 7,
            "schema_version": 2, "absorbed": False,
            "source_event_keys": ["host:demo:private:tone"],
        }])
        # M4 (design 9.3 / G15): a raw fact explains a proposal but may never
        # qualify for one, so this demo has to carry the understanding a real
        # conversation would have produced through the growth gate. The fact
        # stays in the pool as the explanation source.
        core.reflections.save("demo", [
            _demo_understanding("fact_tone"),
        ])
        proposal = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["ref_tone", "fact_tone"],
        })
        accepted = core.persona_admin_service.accept_proposal(
            "demo", proposal.proposal_id, reason="demo accept",
        )
        accepted_projection = dict(
            core.persona_card_service.render_projection("demo"),
        )
        second = await core.evolution_service.propose("demo", {
            "base_revision": accepted.revision_after,
            "source_refs": ["ref_tone", "fact_tone"],
        })
        rejected = core.persona_admin_service.reject_proposal(
            "demo", second.proposal_id, reason="demo reject",
        )
        diff = core.persona_admin_service.diff("demo", 1, accepted.revision_after)
        rolled = core.persona_admin_service.rollback_to_revision("demo", 1)
        switched = core.persona_admin_service.set_auto_evolution("demo", True)
        reopened = build_memory_core(memory_root=root, clock=clock)
        reopened.recover("demo")
        return {
            "mode": "persona_admin_temp_root",
            "data_root": str(root),
            "accepted": {
                "status": accepted.status,
                "revision_before": accepted.revision_before,
                "revision_after": accepted.revision_after,
                "projection": accepted_projection,
            },
            "rejected": {
                "status": rejected.status,
                "proposal_status": core.evolution_service.get(
                    "demo", second.proposal_id,
                )["status"],
            },
            "diff": {
                "available": diff.available,
                "changed": [item["section_id"] for item in diff.changed],
            },
            "rollback": {
                "status": rolled.status,
                "revision_after": rolled.revision_after,
                "tone": core.persona_card_service.render_projection("demo")["tone"],
            },
            "auto_evolution": {
                "enabled": switched.enabled,
                "after_reopen": reopened.persona_admin_service.auto_evolution(
                    "demo",
                ).enabled,
            },
        }


async def run() -> list[dict]:
    return [
        await demo(), await durable_demo(), await correction_deletion_demo(),
        await extraction_demo(), await reflection_growth_demo(),
        await recall_demo(), await standalone_closed_loop_demo(),
        await persona_card_demo(), await persona_publish_demo(),
        await persona_evolution_demo(), await persona_admin_demo(),
    ]


def main() -> None:
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()