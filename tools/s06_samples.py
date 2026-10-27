"""S06 fixed recall samples: the failure corpus and its expectations.

`docs/standalone-service.md` §6.2 requires a fixed, desensitised set covering
Chinese entities and short words, synonymy, reference ("那个"), topic shifts,
irrelevant-content suppression, cross-subject/scene isolation and deleted
content. Every case names what must be recalled and what must be excluded, so
the metrics in `.scratch/check_s06_recall.py` measure a stated contract rather
than "some results came back".

Design rules:

* all content here is **synthetic**. No user text, no real character data;
* each case declares its own corpus, so a case can never pass by borrowing a
  fact that belongs to another case;
* `expect_excluded` is as important as `expect_recalled`: "injects unrelated
  memories" is a failure the plan tracks next to Recall@K;
* ids are stable (`fact_<case>_<n>`) so a report diff shows which item moved.

Adding a case means adding it here; the runner discovers cases by name and
never hard-codes one, so the corpus stays the single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


@dataclass(frozen=True)
class SampleFact:
    """One corpus row, shaped like the stored fact records the recall reads.

    `scene` is written as the *scene restriction* the recall actually compares
    (`applies_to` with a `game_id`/`session_id`/`plugin_id` key), not as an
    opaque label: `entry_applies_to_scene` matches field by field, so storing a
    label like "game:sts2" would restrict nothing and quietly make every
    isolation case vacuous.
    """

    id: str
    text: str
    subject: str = "private"
    #: Raw `applies_to` restriction dict, e.g. {"game_id": "sts2"}.
    scene: Mapping[str, str] = field(default_factory=dict)
    deleted: bool = False


@dataclass(frozen=True)
class RecallCase:
    """One query with its corpus and its pass conditions."""

    name: str
    #: Which §6.2 failure type this case pins (reported, not scored).
    failure_type: str
    query: str
    facts: tuple[SampleFact, ...]
    expect_recalled: tuple[str, ...] = ()
    expect_excluded: tuple[str, ...] = ()
    #: Subject/scene the query is asked from (isolation cases set these).
    subject: str = "private"
    #: Query scene as a scene dict, e.g. {"game_id": "sts2"}.
    scene: Mapping[str, str] = field(default_factory=dict)
    #: Recent turns handed to passive context (reference/continuation cases).
    recent_turns: tuple[str, ...] = ()
    #: Which recall path this case measures: "explicit" (the recall tool /
    #: `/query_memory`) or "passive" (per-turn `build_turn_context`).
    #: They are deliberately different pipelines (spec §0.2): anaphora
    #: expansion exists only on the passive path, so a reference case must say
    #: which one it pins instead of silently measuring the other.
    path: str = "explicit"
    #: A memory that was deleted before the query ran.
    deleted_ids: tuple[str, ...] = ()
    #: Expect the query to return nothing at all (topic switch / suppression).
    expect_empty: bool = False
    notes: str = ""


FIXED_NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)


def _fact(
    case: str, index: int, text: str, *,
    subject: str = "private", scene: Mapping[str, str] | None = None,
) -> SampleFact:
    return SampleFact(
        id=f"fact_{case}_{index}", text=text, subject=subject,
        scene=dict(scene or {}),
    )


def build_cases() -> tuple[RecallCase, ...]:
    """The frozen corpus. Order is stable so reports diff cleanly."""
    cases: list[RecallCase] = []

    # -- 6.2 bullet 1: Chinese entities and short words ------------------
    cases.append(RecallCase(
        name="cjk_entity_drink",
        failure_type="cjk_entity",
        query="乌龙茶",
        facts=(
            _fact("cjk_entity_drink", 1, "用户喜欢喝乌龙茶"),
            _fact("cjk_entity_drink", 2, "用户周末会去公园跑步"),
            _fact("cjk_entity_drink", 3, "用户在用一台旧笔记本写代码"),
        ),
        expect_recalled=("fact_cjk_entity_drink_1",),
        expect_excluded=("fact_cjk_entity_drink_2", "fact_cjk_entity_drink_3"),
        notes="A two-character CJK entity must not be drowned by other facts.",
    ))
    cases.append(RecallCase(
        name="short_latin_term",
        failure_type="cjk_entity",
        query="Live2D 模型",
        facts=(
            _fact("short_latin_term", 1, "用户自己做了 Live2D 模型"),
            _fact("short_latin_term", 2, "用户最近在整理照片"),
        ),
        expect_recalled=("fact_short_latin_term_1",),
        expect_excluded=("fact_short_latin_term_2",),
        notes="Mixed CJK/Latin short terms keep their Latin fragment.",
    ))

    # -- 6.2 bullet 1: synonymy -----------------------------------------
    cases.append(RecallCase(
        name="synonym_paraphrase",
        failure_type="synonym",
        query="失眠",
        facts=(
            _fact("synonym_paraphrase", 1, "用户经常熬夜到凌晨"),
            _fact("synonym_paraphrase", 2, "用户养了一只猫叫团子"),
        ),
        # The wanted fact is stated, so the gap is measured rather than hidden
        # behind an empty expectation: BM25 cannot match 失眠 against 熬夜, and
        # only query expansion or vectors would close it (§6.3/§6.4). This case
        # is a recorded `baseline_gap`, not a contract failure.
        expect_recalled=("fact_synonym_paraphrase_1",),
        expect_excluded=("fact_synonym_paraphrase_2",),
        notes="Known baseline failure: records the synonym gap, not a bug.",
    ))

    # -- 6.2 bullet 2: reference and topic continuity -------------------
    cases.append(RecallCase(
        name="reference_continuation",
        failure_type="reference",
        query="你刚才说的那个",
        facts=(
            _fact("reference_continuation", 1, "用户打算周末去公园跑五公里"),
            _fact("reference_continuation", 2, "用户不喜欢吵闹的地方"),
        ),
        recent_turns=("我周末想去公园跑五公里",),
        path="passive",
        expect_recalled=("fact_reference_continuation_1",),
        expect_excluded=("fact_reference_continuation_2",),
        notes=(
            "'那个' must resolve through the recent turn. Only the passive "
            "path expands anaphora (spec §0.2), so this case measures passive."
        ),
    ))

    # -- 6.2 bullet 3: topic switch / irrelevant suppression ------------
    cases.append(RecallCase(
        name="topic_switch_suppression",
        failure_type="topic_switch",
        query="明天的天气预报",
        facts=(
            _fact("topic_switch_suppression", 1, "用户喜欢喝乌龙茶"),
            _fact("topic_switch_suppression", 2, "用户养了一只猫叫团子"),
        ),
        expect_recalled=(),
        expect_empty=True,
        notes="A brand-new topic must return nothing rather than force a match.",
    ))

    # -- 6.2 bullet 4: cross-subject isolation --------------------------
    cases.append(RecallCase(
        name="subject_isolation",
        failure_type="isolation",
        query="乌龙茶",
        facts=(
            _fact("subject_isolation", 1, "用户喜欢喝乌龙茶"),
            _fact("subject_isolation", 2, "另一个人也喜欢喝乌龙茶", subject="other"),
        ),
        expect_recalled=("fact_subject_isolation_1",),
        expect_excluded=("fact_subject_isolation_2",),
        notes="Another subject's identical fact must never leak in.",
    ))

    # -- 6.2 bullet 4: cross-scene isolation ----------------------------
    cases.append(RecallCase(
        name="scene_isolation",
        failure_type="isolation",
        query="先防后攻",
        facts=(
            _fact("scene_isolation", 1, "第三层先防后攻比较稳",
                  scene={"game_id": "sts2"}),
            _fact("scene_isolation", 2, "用户喜欢在家做饭",
                  scene={"session_id": "chat-1"}),
        ),
        # The query is asked FROM the game scene, which is the case that
        # matters: the host-scene row must be filtered out, and the game row
        # must survive. Without an explicit scene the recall correctly drops
        # every scene-restricted row, which is a different (also correct) path.
        scene={"game_id": "sts2"},
        expect_recalled=("fact_scene_isolation_1",),
        expect_excluded=("fact_scene_isolation_2",),
        notes="A game-scoped tactic is recalled inside its scene and not outside.",
    ))

    # -- 6.2 bullet 4: deleted content ----------------------------------
    cases.append(RecallCase(
        name="deleted_content",
        failure_type="deletion",
        query="咖啡",
        facts=(
            _fact("deleted_content", 1, "用户喜欢喝乌龙茶"),
            _fact("deleted_content", 2, "用户喜欢喝咖啡"),
        ),
        deleted_ids=("fact_deleted_content_1",),
        # The surviving fact is the coffee one, so the query must be about
        # coffee: asking "乌龙茶" after deleting the tea fact correctly returns
        # nothing, which would not prove deletion works.
        expect_recalled=("fact_deleted_content_2",),
        expect_excluded=("fact_deleted_content_1",),
        notes="A deleted fact must not come back through the corpus.",
    ))

    return tuple(cases)


#: Quick lookup used by the report and by tests.
CASE_NAMES: tuple[str, ...] = tuple(case.name for case in build_cases())


def all_fact_ids(case: RecallCase) -> set[str]:
    """Every fact id in one case's corpus (for coverage accounting)."""
    return {fact.id for fact in case.facts}
