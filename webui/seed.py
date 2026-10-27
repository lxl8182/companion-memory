"""Demo data for the RIN WebUI: character suzume / display name 铃芽.

The seed is built through the real core services wherever possible:

* 68 journal events are appended through DurableEventStore.append (so they get
  durable receipts, source ledger entries and replayable journal rows);
* facts are written through FactsView (the real host-shaped store);
* two deletions go through DeletionService.forget (barrier + tombstones);
* one correction goes through CorrectionService.correct (it invalidates a fact
  and records the replacement understanding);
* reflections/growth rows are written through ReflectionStore in the host row
  shape the B05 reader accepts;
* the persona card is imported/revised/frozen through PersonaCardService;
* three published versions go through PersonaPublishService.publish;
* three evolution proposals go through EvolutionService.propose with a
  deterministic offline model; one is accepted and one rejected through
  PersonaAdminService;
* companion state is committed through StateService (lease -> commit).

Nothing here reads NEKO user data, the network or anything outside the
explicit --root. python webui/seed.py --root <dir> builds a complete demo root
and prints the counts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, TypeVar

from rin_memory.adapters.composition import build_memory_core as build_core
from rin_memory.contracts import (
    CorrectionRequest,
    DeletionRequest,
    DeletionTarget,
    MemoryEvent,
    Scene,
    Scope,
)
from rin_memory.domain.evolution import EvolutionSettings
from rin_memory.event_sources import (
    build_host_source_key,
    build_plugin_source_key,
)
from rin_memory.persistence.permits import NullWritePermit, RejectingWritePermit

CHARACTER = "suzume"
DISPLAY_NAME = "铃芽"
ABBREVIATION = "RIN"
SEED_VERSION = 1
MARKER_FILENAME = "webui_seed.json"
DEMO_INSTANCE = "rin-webui-demo"
DEMO_LEASE_ID = "rin-webui-demo-lease"

#: The deterministic offline proposal model. Only the evolution purpose is
#: exercised by the WebUI; every other purpose returns an empty JSON object.
EVOLUTION_TEXT = (
    "用户喜欢温柔、具体的语气；先给结论，再补理由，必要时直接问一次。"
)
EVOLUTION_REASON = "证据显示用户偏好具体的温柔表达，并且不喜欢被反复追问"


class DemoModel:
    """Offline model stand-in; no network and no provider configuration."""

    async def invoke(
        self, tier, messages, *, max_output_tokens, timeout_s, purpose,
    ) -> str:
        del tier, messages, max_output_tokens, timeout_s
        if purpose == "persona_evolution_proposal":
            return json.dumps(
                {
                    "operations": [{
                        "op": "replace",
                        "section_id": "tone",
                        "text": EVOLUTION_TEXT,
                    }],
                    "reason": EVOLUTION_REASON,
                },
                ensure_ascii=False,
            )
        return "{}"


T = TypeVar("T")


def _run_async(coro: Awaitable[T]) -> T:
    """Run a coroutine from sync code (also when a loop is already running)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised in caller thread
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    error = result.get("error")
    if error is not None:
        raise error
    return result["value"]


# -- paths and safety --------------------------------------------------------


def _resolve_root(memory_root: Path | str, *, allow_nekodata: bool = False) -> Path:
    """Resolve and sanity-check a data root.

    Demo builders keep the original guard so a throwaway seed can never land in
    the real user data root. Live builders pass allow_nekodata=True: reading the
    real root read-only is the documented C06 path.
    """
    root = Path(memory_root).expanduser().resolve()
    if not allow_nekodata and "nekodata" in str(root).lower():
        raise ValueError("refusing a data root whose path contains 'nekodata'")
    if root == Path(root.anchor):
        raise ValueError(f"refusing the filesystem root as a data root: {root}")
    return root


def _marker_path(root: Path) -> Path:
    return root / CHARACTER / MARKER_FILENAME


def _marker_ok(root: Path) -> bool:
    try:
        payload = json.loads(_marker_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("seed_version") == SEED_VERSION


def _write_marker(root: Path) -> None:
    path = _marker_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"seed_version": SEED_VERSION, "character": CHARACTER},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _remove_character(root: Path) -> None:
    character_dir = root / CHARACTER
    if character_dir.exists():
        shutil.rmtree(character_dir)
    local_state = root / "local_state"
    if local_state.exists():
        shutil.rmtree(local_state)


# -- core builders -----------------------------------------------------------


def _new_core(root: Path, *, model: Any = None, writable: bool = True):
    permit = NullWritePermit() if writable else RejectingWritePermit(
        code="read_only", reason="memory root is read-only",
    )
    return build_core(
        memory_root=root,
        local_state_root=root / "local_state",
        permit=permit,
        model=model,
        evolution_settings=EvolutionSettings(enabled=True),
    )


def build_memory_core(memory_root: Path | str, *, reset: bool = False):
    """Build (and, when needed, seed) the suzume demo root; returns the core.

    A completed seed is reused. An existing but unmarked character directory is
    never deleted implicitly (it may be real data): pass reset=True (the server
    demo-reset path and the CLI --reset flag do) to rebuild it.
    """
    root = _resolve_root(memory_root)
    if reset:
        _remove_character(root)
    elif (root / CHARACTER).exists() and not _marker_ok(root):
        raise RuntimeError(
            f"{root / CHARACTER} exists but is not a completed RIN demo seed; "
            "pass --reset to rebuild it",
        )
    core = _new_core(root, model=DemoModel())
    core.recover(CHARACTER)
    if not _marker_ok(root):
        _run_async(_seed(core))
        _write_marker(root)
    return core


#: Root ownership locks held by writable live consoles, keyed by `id(core)`.
#: `MemoryCore` is a frozen dataclass, so the lock is tracked here instead of
#: being attached to the core; `release_live_lock` is what `RinApp.close()`
#: calls.
_LIVE_LOCKS: dict[int, Any] = {}


def release_live_lock(core: Any) -> None:
    """Release the root ownership lock a writable live console holds."""
    lock = _LIVE_LOCKS.pop(id(core), None)
    if lock is not None:
        lock.release()


def build_live_core(
    memory_root: Path | str, *, writable: bool, character: str | None = None,
):
    """Build a core over an existing root; read-only roots get a rejecting permit.

    A writable live console is one of the entry points §2.2 of
    docs/standalone-service.md names: it takes the root ownership lock before
    assembling its core, so it can never become a second writer next to the
    standalone service, the embedded host or a `run --loop` maintenance
    process. `release_live_lock` drops it when the console closes.
    """
    root = _resolve_root(memory_root, allow_nekodata=True)
    lock = None
    if writable:
        from rin_memory.persistence.ownership import acquire_root_lock

        lock = acquire_root_lock(root, mode="console")
    try:
        core = _new_core(root, model=None, writable=writable)
        if writable:
            core.recover(character or CHARACTER)
    except BaseException:
        if lock is not None:
            lock.release()
        raise
    if lock is not None:
        _LIVE_LOCKS[id(core)] = lock
    return core


# -- event corpus ------------------------------------------------------------

#: (theme title, user message, assistant message) -- one triple per day, oldest
#: first. The third event of each day is derived from the theme and the scene
#: rotation below.
THEMES: tuple[tuple[str, str, str], ...] = (
    ("初次见面", "你好呀，我是铃芽。以后请多关照。", "嗯，我记住了你的声音。有什么想聊的随时叫我。"),
    ("整理桌面", "今天把桌面收拾了一遍，舒服多了。", "整洁的桌面会让思路也清楚一点，我帮你记下这个小习惯。"),
    ("雨天音乐", "下雨了，我在听一首城市流行。", "雨声和城市流行很配，下次我可以推荐几首。"),
    ("跑步计划", "周末想去公园跑五公里。", "记得热身和补水，我会替你记着这个计划。"),
    ("工作烦躁", "今天被临时需求打断了好几次，有点烦。", "先深呼吸，把最重要的一件事挑出来，其他的我陪你慢慢理。"),
    ("做饭", "晚上做了番茄牛腩，味道还不错。", "听起来很香，下次可以试试少放一点糖。"),
    ("猫团子", "团子今天又把纸箱占了。", "猫对纸箱的执念真是可爱，团子今天心情不错。"),
    ("游戏第一局", "STS2 第一局又贪了一刀。", "保守一点更稳，下一局我们试试先防后攻。"),
    ("语音通话", "今天语音里你笑得很开心。", "和你聊天的时候，我也觉得很轻松。"),
    ("读书", "在读一本讲记忆的书，里面说记忆会被重写。", "那我们就把重要的部分一起记住，别让它被改坏。"),
    ("生日", "十月中旬的生日快到了，有点期待。", "我会记得这个日子，到时候提醒你好好庆祝。"),
    ("插件任务", "插件那边今天的任务列表清空了。", "辛苦了，清空任务列表的感觉很好吧。"),
    ("旅行计划", "以前住在东京的时候常去海边，最近又想找个周末去看日出。", "海边日出要早起，我可以帮你把路线和时间记下来。"),
    ("失眠", "昨晚又失眠到两点。", "今晚早点放下手机，热牛奶对你会不会有帮助？"),
    ("咖啡", "早上还是得靠手冲咖啡。", "记得别空腹喝，下午之后少一点。"),
    ("雨天散步", "傍晚雨停了，出去走了一圈。", "雨后的空气很干净，散步是个好选择。"),
    ("项目上线", "项目终于上线了，虽然还有一堆小问题。", "先让自己松一口气，剩下的问题明天再排优先级。"),
    ("感冒", "有点感冒，嗓子不太舒服。", "多喝温水，今天别熬太晚，我会提醒你休息。"),
    ("拍照", "今天拍到了很好看的晚霞。", "晚霞这种瞬间值得留住，发我看看？"),
    ("年末总结", "这一年过得比想象中快。", "我们一起把这一年里值得记住的事都收好。"),
    ("今天", "今天想和你安静地待一会儿。", "好，我就在这里。你想说话的时候我听着。"),
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _scene_label(scene: Scene) -> str:
    if scene.game_id:
        return f"game:{scene.game_id}"
    if scene.plugin_id:
        return f"plugin:{scene.plugin_id}"
    return f"host:{scene.session_id or 'chat'}"


def _event_rows(now: datetime) -> list[tuple[str, MemoryEvent]]:
    rows: list[tuple[str, MemoryEvent]] = []

    def add(
        slug: str,
        text: str,
        event_type: str,
        occurred_at: datetime,
        scene: Scene,
        *,
        subject: str = "private",
        content_class: str = "chat",
    ) -> None:
        event_id = f"evt_{len(rows) + 1:04d}"
        if scene.plugin_id:
            source_key = build_plugin_source_key(
                scene.plugin_id, CHARACTER, subject,
                scene.session_id or "session", event_id,
            )
        else:
            source_key = build_host_source_key(CHARACTER, subject, event_id)
        rows.append((slug, MemoryEvent(
            scope=Scope(CHARACTER, subject),
            source_key=source_key,
            event_type=event_type,
            text=text,
            occurred_at=occurred_at,
            scene=scene,
            content_class=content_class,
            event_id=event_id,
        )))

    for day, (title, user_text, assistant_text) in enumerate(THEMES):
        day_base = now - timedelta(days=(20 - day))
        subject = "user" if day in (0, 10, 20) else "private"
        host_scene = Scene(session_id=f"chat-{day + 1:02d}")
        add(
            f"day{day + 1:02d}-user", user_text, "user_message",
            day_base - timedelta(hours=9), host_scene, subject=subject,
        )
        add(
            f"day{day + 1:02d}-ai", assistant_text, "assistant_message",
            day_base - timedelta(hours=6), host_scene,
        )
        if day % 5 == 3:
            game_scene = Scene(
                plugin_id="nekospire", game_id="sts2",
                session_id=f"game-{day + 1:02d}",
            )
            add(
                f"day{day + 1:02d}-game",
                f"STS2 对局记录：{title}之后选择保守路线，剩余生命 42。",
                "game_event", day_base - timedelta(hours=2), game_scene,
                content_class="game",
            )
        elif day % 5 == 1:
            plugin_scene = Scene(
                plugin_id="nekospire", session_id=f"plugin-{day + 1:02d}",
            )
            add(
                f"day{day + 1:02d}-plugin",
                f"nekospire 会话摘要：{title}相关任务已完成，用户偏好短句回复。",
                "plugin_event", day_base - timedelta(hours=2), plugin_scene,
                content_class="plugin",
            )
        else:
            add(
                f"day{day + 1:02d}-note",
                f"我把「{title}」这件事记在小本子上了。",
                "assistant_message", day_base - timedelta(hours=2), host_scene,
            )

    # Extra events: one deleted source, one address fact, two recent turns and
    # a plugin reminder (68 events in total).
    add(
        "quarrel", "那天和同事争执其实是因为排期，不是针对我。",
        "user_message", now - timedelta(days=12, hours=5),
        Scene(session_id="chat-extra-1"), subject="user",
    )
    add(
        "old_address", "刚搬来的时候住在东京都涩谷区，后来换了个安静的地方。",
        "user_message", now - timedelta(days=19, hours=4),
        Scene(session_id="chat-extra-2"),
    )
    add(
        "reminder", "nekospire 提醒：明日 09:00 有晨跑计划，记得带水。",
        "plugin_event", now - timedelta(hours=3),
        Scene(plugin_id="nekospire", session_id="plugin-reminder"),
        content_class="plugin",
    )
    add(
        "recent-user", "对了，我不太喜欢被反复追问同一件事。",
        "user_message", now - timedelta(minutes=40),
        Scene(session_id="chat-today"), subject="user",
    )
    add(
        "recent-ai", "明白，我会直接问一次，然后记住答案。",
        "assistant_message", now - timedelta(minutes=35),
        Scene(session_id="chat-today"),
    )
    return rows


# -- seed sections -----------------------------------------------------------

INITIAL_SECTIONS: tuple[dict[str, Any], ...] = (
    {
        "section_id": "identity", "kind": "identity",
        "text": "你是铃芽（RIN），一个会认真听人说话、也愿意表达自己偏好的陪伴角色。",
        "scope": {"subject": "private"},
        "editable_by": {"user": True, "evolution": False},
        "frozen": False, "source_refs": [],
    },
    {
        "section_id": "tone", "kind": "style",
        "text": "说话温柔、具体，不绕弯子；用户需要时先给结论，再补理由。",
        "scope": {"subject": "private"},
        "editable_by": {"user": True, "evolution": True},
        "frozen": False, "source_refs": ["fact_tone"],
    },
    {
        "section_id": "relationship", "kind": "relationship",
        "text": "你和用户是长期相处的搭档，记得共同经历的小事，也会在对方疲惫时主动放缓节奏。",
        "scope": {"subject": "private"},
        "editable_by": {"user": True, "evolution": True},
        "frozen": False, "source_refs": ["fact_run"],
    },
    {
        "section_id": "habits", "kind": "habit",
        "text": "用户周末有晨跑习惯，你会在周五晚上提醒准备跑鞋和补水。",
        "scope": {"subject": "private"},
        "editable_by": {"user": True, "evolution": True},
        "frozen": False, "source_refs": ["fact_run"],
    },
    {
        "section_id": "boundaries", "kind": "boundary",
        "text": "不替用户做重大决定，不编造没有依据的记忆，不主动触碰用户不愿提的话题。",
        "scope": {"subject": "private"},
        "editable_by": {"user": True, "evolution": False},
        "frozen": False, "source_refs": [],
    },
)

AESTHETIC_SECTION = {
    "op": "add", "section_id": "aesthetic", "kind": "aesthetic",
    "text": "偏爱雨后的空气、旧书页和城市夜景；表达上喜欢短句和留白。",
    "scope": {"subject": "private"},
    "editable_by": {"user": True, "evolution": True},
    "source_refs": ["fact_music"],
}


def _reflection_row(
    row_id: str,
    text: str,
    status: str,
    reinforcement: float,
    fact_ids: list[str],
    event_keys: list[str],
    created_at: str,
    *,
    needs_rebuild: bool = False,
    absorbed_into: str | None = None,
    entity: str = "relationship",
    experiences: list[tuple[str, list[str]]] | None = None,
) -> dict[str, Any]:
    """One demo reflection row in the host shape, plus the M4 support basis.

    `experiences` names the *independent* experiences (one episode each) the
    row rests on, so the demo data follows the same rule real synthesis does
    instead of leaving the maturing gate to guess. When it is omitted each event
    key is treated as its own experience, which is what a conversation-per-key
    seed naturally is.
    """
    evidence = [
        {"source_key": key, "weight": round(1.0 + index * 0.1, 2)}
        for index, key in enumerate(event_keys)
    ]
    groups = experiences
    if groups is None:
        groups = [("episode:" + key, [key]) for key in event_keys]
    basis = [
        {
            "experience": experience,
            "facts": [],
            "support": [
                {"key": key, "fact_id": "", "kind": "",
                 "flags": ["actual_result"]}
                for key in keys
            ],
        }
        for experience, keys in groups
    ]
    row: dict[str, Any] = {
        "id": row_id,
        "text": text,
        "entity": entity,
        "status": status,
        "source_fact_ids": list(fact_ids),
        "source_event_keys": list(event_keys),
        "source_refs": list(fact_ids),
        "created_at": created_at,
        "feedback": None,
        "reinforcement": reinforcement,
        "rein_last_signal_at": created_at,
        "disputation": 0.0,
        "disp_last_signal_at": None,
        "sub_zero_days": 0,
        "sub_zero_last_increment_date": None,
        "user_fact_reinforce_count": 0,
        "absorbed_into": absorbed_into,
        "needs_rebuild": needs_rebuild,
        "invalidated_at": None,
        "protected": False,
        "schema_version": 2,
        "evidence": evidence,
        # M4: independent experiences + derived count/flag buckets. Written on
        # new demo rows so they go through the strict rule, not the legacy one.
        "support_basis": basis,
        "support_event_keys": list(event_keys),
        "support_count": len(basis),
        "support_evidence": {
            "actual_result": list(event_keys),
            "user_feedback": [], "both_party": [], "real_contact": [],
        },
        "persona_ids": [absorbed_into] if absorbed_into else [],
    }
    if status == "promoted":
        row["promoted_at"] = created_at
    return row


def _growth_row(
    row_id: str,
    kind: str,
    claim: str,
    situation: str,
    status: str,
    support_keys: list[str],
    created_at: str,
    *,
    experiences: list[tuple[str, list[str]]] | None = None,
) -> dict[str, Any]:
    """One demo growth row with the M4 support basis (see _reflection_row)."""
    groups = experiences
    if groups is None:
        groups = [("episode:" + key, [key]) for key in support_keys]
    basis = [
        {
            "experience": experience,
            "facts": [],
            "support": [
                {"key": key, "fact_id": "", "kind": "",
                 "flags": ["actual_result"]}
                for key in keys
            ],
        }
        for experience, keys in groups
    ]
    return {
        "id": row_id,
        "text": claim,
        "claim": claim,
        "situation": situation,
        "entity": "relationship",
        "status": status,
        "memory_kind": kind,
        "growth_key": f"{kind}:{claim}",
        "support_event_keys": list(support_keys),
        "support_count": len(basis),
        "support_basis": basis,
        "support_evidence": {
            "actual_result": list(support_keys),
            "user_feedback": [],
            "both_party": [],
            "real_contact": [],
        },
        "source_event_keys": list(support_keys),
        "source_refs": list(support_keys),
        "applies_to": {},
        "created_at": created_at,
        "updated_at": created_at,
        "valid_from": created_at,
        "superseded_by": None,
        "needs_rebuild": False,
        "invalidated_at": None,
        "schema_version": 2,
    }


# -- the seed itself ---------------------------------------------------------


async def _seed(core) -> dict[str, int]:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    event_rows = _event_rows(now)
    keys = {slug: event.source_key for slug, event in event_rows}
    event_ids = {slug: str(event.event_id) for slug, event in event_rows}
    occurred_at = {slug: _iso(event.occurred_at) for slug, event in event_rows}

    # 1) Durable events through the real sink (journal + source ledger).
    for _slug, event in event_rows:
        receipt = await core.sink.append(event)
        if receipt.status != "accepted":
            raise RuntimeError(
                f"demo event {event.source_key!r} was not accepted: {receipt.status}",
            )

    def at(slug: str) -> str:
        return occurred_at.get(slug, _iso(now))

    # 2) Facts in the host-shaped facts.json (15 rows; 2 deleted later).
    facts = [
        _fact("fact_tone", "用户喜欢温柔、具体的语气", 8, [keys["day01-user"]], at("day01-user"), confidence=0.9),
        _fact("fact_run", "用户周末在公园跑步", 7, [keys["day04-user"]], at("day04-user"), absorbed=True),
        _fact("fact_cat", "用户养了一只叫团子的猫", 7, [keys["day07-user"]], at("day07-user"), absorbed=True),
        _fact("fact_coffee", "用户早上靠手冲咖啡提神", 5, [keys["day15-user"]], at("day15-user")),
        _fact("fact_music", "用户雨天喜欢听城市流行", 6, [keys["day03-user"]], at("day03-user")),
        _fact("fact_game", "用户在 STS2 里偏好保守打法", 6, [keys["day09-game"]], at("day09-game")),
        _fact("fact_work", "用户最近在赶一个上线项目", 9, [keys["day05-user"]], at("day05-user")),
        _fact("fact_sleep", "用户熬夜后会补一杯热牛奶", 4, [keys["day14-user"]], at("day14-user")),
        _fact("fact_book", "用户在读一本关于记忆的书", 5, [keys["day10-user"]], at("day10-user")),
        _fact("fact_tokyo", "用户以前住在东京，最近又想去看海", 6, [keys["day13-user"]], at("day13-user")),
        _fact("fact_birthday", "用户的生日在十月中旬", 10, [keys["day11-user"]], at("day11-user")),
        _fact("fact_trip", "用户想去海边看日出", 7, [keys["day13-user"]], at("day13-user")),
        _fact("fact_plugin", "插件 nekospire 记录用户偏爱短句回复", 5, [keys["day12-plugin"]], at("day12-plugin")),
        _fact("fact_old_address", "用户的旧住址在东京都涩谷区", 3, [keys["old_address"]], at("old_address")),
        _fact("fact_quarrel", "用户提到和同事有过一次争执", 4, [keys["quarrel"]], at("quarrel")),
    ]
    core.facts.save(CHARACTER, facts)

    # 3) Two deletions through the real deletion service (barrier + tombstones).
    first = await core.deletion_service.forget(DeletionRequest(
        scope=Scope(CHARACTER),
        targets=(DeletionTarget("fact", "fact_old_address"),),
        reason="用户要求删除旧住址",
        request_id="seed-deletion-1",
    ))
    if first.status != "completed":
        raise RuntimeError(f"demo deletion 1 not completed: {first.status}")
    second = await core.deletion_service.forget(DeletionRequest(
        scope=Scope(CHARACTER),
        source_keys=(keys["quarrel"],),
        reason="用户不想保留那次争执",
        request_id="seed-deletion-2",
    ))
    if second.status != "completed":
        raise RuntimeError(f"demo deletion 2 not completed: {second.status}")

    # 4) One correction: replaces the Tokyo fact (it becomes invalidated).
    correction = await core.correction_service.correct(CorrectionRequest(
        scope=Scope(CHARACTER),
        action="replace",
        old_text="以前住在东京",
        new_text="用户现在住在大阪",
        source_key=build_host_source_key(CHARACTER, "private", "correction-seed-1"),
        request_id="seed-correction-1",
    ))
    if "fact_tokyo" not in correction.invalidated_fact_ids:
        raise RuntimeError("demo correction did not invalidate fact_tokyo")

    # 5) Reflections (6) and growth (4) in the host row shape.
    reflection_rows = [
        _reflection_row(
            "ref_run", "用户喜欢周末在公园跑步", "promoted", 2.6,
            ["fact_run", "fact_trip"], [keys["day04-user"], keys["day13-user"]],
            at("day04-user"), absorbed_into="prom_ref_run",
        ),
        # The tone understanding the demo evolution proposals rest on: two
        # independent experiences (day one and the recent turn), so it is a
        # mature candidate under M4's eligibility gate. `fact_tone` stays in the
        # pool as its explanation source.
        _reflection_row(
            "ref_tone", "用户喜欢温柔、具体的表达，不喜欢被反复追问", "confirmed", 1.8,
            ["fact_tone"], [keys["day01-user"], keys["recent-user"]],
            at("day01-user"),
        ),
        _reflection_row(
            "ref_work", "用户在意项目上线的节奏，也怕被临时需求打断", "confirmed", 1.4,
            ["fact_work"], [keys["day05-user"]], at("day05-user"),
        ),
        _reflection_row(
            "ref_cat", "团子是用户生活里很重要的陪伴", "confirmed", 1.0,
            ["fact_cat"], [keys["day07-user"]], at("day07-user"),
        ),
        _reflection_row(
            "ref_coffee", "用户用一杯手冲咖啡开始一天", "pending", 0.8,
            ["fact_coffee"], [keys["day15-user"]], at("day15-user"),
        ),
        _reflection_row(
            "ref_sleep_rebuild", "用户失眠时希望被轻声安抚", "confirmed", 0.6,
            ["fact_sleep"], [keys["day14-user"]], at("day14-user"),
            needs_rebuild=True,
        ),
        _reflection_row(
            "ref_old_archive", "用户以前常去东京的海边", "archived", 0.4,
            ["fact_tokyo"], [keys["day13-user"]], at("day13-user"),
        ),
    ]
    growth_rows = [
        _growth_row(
            "grow_run", "habit", "用户周末有晨跑习惯", "周末早晨",
            "promoted", [keys["day04-user"], keys["reminder"], keys["day13-user"]],
            at("day04-user"),
        ),
        _growth_row(
            "grow_coffee", "interest", "用户喜欢手冲咖啡", "工作日的早晨",
            "confirmed", [keys["day15-user"], keys["day16-note"]], at("day15-user"),
        ),
        _growth_row(
            "grow_game", "experience", "用户在 STS2 里倾向先防后攻", "游戏对局",
            "confirmed", [keys["day09-game"]], at("day09-game"),
        ),
        _growth_row(
            "grow_archive", "habit", "用户以前有深夜散步的习惯", "深夜",
            "archived", [keys["day16-user"]], at("day16-user"),
        ),
    ]
    core.reflections.save(CHARACTER, reflection_rows + growth_rows)

    # 6) Persona card: import, three revisions, one frozen section.
    imported = core.persona_card_service.import_initial(
        CHARACTER, INITIAL_SECTIONS,
    )
    if imported.status != "completed":
        raise RuntimeError(f"demo card import failed: {imported.status}")
    revision = imported.revision
    revision_steps = (
        (
            [{"op": "replace", "section_id": "tone",
              "text": "说话温柔、具体，不绕弯子；先给结论，再补理由，偶尔开个小玩笑。"}],
            "用户说喜欢直接一点的表达",
        ),
        (
            [{"op": "merge", "section_id": "relationship",
              "text": "也会记得用户不喜欢被反复追问。"}],
            "补充相处边界",
        ),
        ([dict(AESTHETIC_SECTION)], "把审美偏好写进卡里"),
    )
    for operations, reason in revision_steps:
        receipt = core.persona_card_service.revise(CHARACTER, {
            "base_revision": revision,
            "operations": operations,
            "reason": reason,
        })
        if receipt.status != "completed":
            raise RuntimeError(f"demo card revision failed: {receipt.status}")
        revision = receipt.revision
    frozen = core.persona_card_service.freeze(CHARACTER, "boundaries", True)
    if frozen.status != "completed":
        raise RuntimeError(f"demo card freeze failed: {frozen.status}")
    revision = frozen.revision

    # 7) Publish next-turn and realtime at the frozen revision. (Publishing
    # both before the accepted proposal keeps each version token distinct from
    # the later game token, so journal replay stays duplicate-free.)
    for target in ("next_turn", "realtime"):
        first_publish = core.persona_publish.publish(
            CHARACTER, target=target, revision=revision,
        )
        if first_publish.status != "published":
            raise RuntimeError(f"demo publish {target} failed: {first_publish.status}")

    # 8) Three evolution proposals: one accepted, one rejected, one pending.
    proposal_one = await core.evolution_service.propose(CHARACTER, {
        "base_revision": revision,
        "source_refs": ["ref_tone", "fact_tone"],
        "focus_sections": ["tone"],
        "reason": EVOLUTION_REASON,
        "request_id": "seed-evolution-1",
    })
    if proposal_one.status != "proposed":
        raise RuntimeError(f"demo proposal 1 failed: {proposal_one.status}")
    accepted = core.persona_admin_service.accept_proposal(
        CHARACTER, proposal_one.proposal_id, reason="审核通过",
    )
    if accepted.status != "accepted":
        raise RuntimeError(f"demo proposal accept failed: {accepted.status}")
    revision = accepted.revision_after

    game_publish = core.persona_publish.publish(
        CHARACTER, target="game", revision=revision,
    )
    if game_publish.status != "published":
        raise RuntimeError(f"demo publish game failed: {game_publish.status}")

    proposal_two = await core.evolution_service.propose(CHARACTER, {
        "base_revision": revision,
        "source_refs": ["ref_tone", "fact_tone"],
        "focus_sections": ["tone"],
        "reason": "这次提案与当前语气重复",
        "request_id": "seed-evolution-2",
    })
    if proposal_two.status != "proposed":
        raise RuntimeError(f"demo proposal 2 failed: {proposal_two.status}")
    rejected = core.persona_admin_service.reject_proposal(
        CHARACTER, proposal_two.proposal_id, reason="暂时不需要调整",
    )
    if rejected.status != "rejected":
        raise RuntimeError(f"demo proposal reject failed: {rejected.status}")

    proposal_three = await core.evolution_service.propose(CHARACTER, {
        "base_revision": revision,
        "source_refs": ["ref_tone", "fact_tone"],
        "focus_sections": ["tone"],
        "reason": "等待人工审核的候选提案",
        "request_id": "seed-evolution-3",
    })
    if proposal_three.status != "proposed":
        raise RuntimeError(f"demo proposal 3 failed: {proposal_three.status}")

    # 9) Companion state: lease -> commit (version 1) -> release.
    lease = core.state_service.claim_lease(
        CHARACTER, instance=DEMO_INSTANCE, lease_id=DEMO_LEASE_ID,
        ttl_seconds=300,
    )
    if not lease.get("ok"):
        raise RuntimeError("demo state lease was not claimed")
    committed = core.state_service.commit(CHARACTER, {
        "ops": [
            {"op": "set_thought", "text": "用户在准备周末的晨跑",
             "source_event_ids": [event_ids["recent-user"]]},
            {"op": "set_mood", "label": "温柔", "intensity": 0.9,
             "cause": "和用户安静地聊了一会儿",
             "source_event_ids": [event_ids["recent-user"]]},
        ],
        "producer": "webui-demo",
        "instance": DEMO_INSTANCE,
        "lease_id": DEMO_LEASE_ID,
        "request_id": "seed-state-1",
        "base_version": 0,
    })
    if not committed.ok or committed.version < 1:
        raise RuntimeError(f"demo state commit failed: {committed.reason}")
    core.state_service.release_lease(
        CHARACTER, lease_id=DEMO_LEASE_ID, instance=DEMO_INSTANCE,
    )
    # The evolution proposal journal entries are appended without advancing the
    # replay marker, so reconcile once here: handlers are idempotent and the
    # demo root then opens with no unapplied tail.
    core.recover(CHARACTER)
    return seed_counts(core)


def _fact(
    fact_id: str,
    text: str,
    importance: int,
    event_keys: list[str],
    created_at: str,
    *,
    absorbed: bool = False,
    confidence: float = 0.8,
) -> dict[str, Any]:
    return {
        "id": fact_id,
        "text": text,
        "importance": importance,
        "absorbed": absorbed,
        "confidence": confidence,
        "source": "user_observation",
        "schema_version": 2,
        "created_at": created_at,
        "source_event_keys": list(event_keys),
        "invalidated_at": None,
    }


# -- counts / CLI ------------------------------------------------------------


def seed_counts(core) -> dict[str, int]:
    character = CHARACTER
    journal_entries = core.journal.read_since(character, -1)
    rows = core.reflections.load(character)
    reflections = [row for row in rows if not row.get("memory_kind")]
    growth = [row for row in rows if row.get("memory_kind")]
    card = core.persona_card_service.get(character)
    versions = core.persona_versions.load(character)
    proposals = core.evolution_service.list(character)
    deletion_doc = core.deletions.snapshot(character)
    corrections = core.corrections.all_entries(character)
    return {
        "events": sum(
            1 for entry in journal_entries
            if entry.event_type == "core.source_recorded"
        ),
        "facts": len(core.facts.load(character)),
        "reflections": len(reflections),
        "growth": len(growth),
        "card_revision": card.revision,
        "revisions": len(card.revisions),
        "versions": len(versions.get("published") or []),
        "proposals": len(proposals),
        "deletions": len(deletion_doc.get("deletions") or []),
        "tombstones": len(deletion_doc.get("tombstones") or []),
        "corrections": len(corrections),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the 铃芽 RIN demo memory root.",
    )
    parser.add_argument("--root", required=True, help="explicit demo memory root")
    parser.add_argument(
        "--reset", action="store_true", help="rebuild even if the root is seeded",
    )
    args = parser.parse_args(argv)
    try:
        core = build_memory_core(args.root, reset=args.reset)
    except Exception as exc:  # noqa: BLE001 - CLI reports the failure
        print(f"[seed] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "ok": True,
        "root": str(Path(args.root).expanduser().resolve()),
        "character": CHARACTER,
        "display_name": DISPLAY_NAME,
        "counts": seed_counts(core),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
