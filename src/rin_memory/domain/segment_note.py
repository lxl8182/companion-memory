"""续聊便条(RO02-CTX):跨段话题易断线时,一张很短的第一人称便条。

为什么需要它:换段之后新会话里没有旧话,参与感靠两条路补 —— 一是带进去的最近
若干轮原文(由插件侧的 carry 负责),二是**只在话题确实容易断线时**留一张便条。
便条不是摘要:它只有一句话,写的是「我们上次说到哪儿」,带时间与来源,让人能
顺着找回原事。

三条硬纪律:

1. **默认不生成。** 没有明确的未完结话题,或跨段时距太短(同一场对话连续说着),
   就不写。便条只在真的隔了很久、话题又没有自然续上的时候有价值。
2. **不新增常规摘要调用。** 这里不做模型调用;便条内容由已有记录派生(未完事项
   的原文片段 + 时间),因此没有额外成本,也不会和摘要层重复计费。
3. **后续更新仍读原始记录,不做摘要压摘要。** 便条只记录**引用**(哪些事项、
   哪个时间、来源键),正文永远从当时的原始记录重新取;这样被更正或删除之后,
   便条不会把旧结论又带回来。

服务端只负责「该不该有、长什么样」;真正的生成与落盘属于插件侧流程,这里给出的
是确定性的判定与正文,便于用纯函数测试固定行为。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

#: 便条正文上限(字符)。一张便条只有一句话的空间,写长了就变成了摘要。
NOTE_CHAR_LIMIT = 160

#: 跨段时距的下限:短于这个间隔说明同一场对话还在继续,带原文就够了,不必留便条。
MIN_GAP_HOURS = 6

#: 最多引用几件未完事项。两件以上就说明话题已经散了,再多也接不上。
MAX_ITEMS = 2


@dataclass(frozen=True)
class SegmentNote:
    """一张续聊便条。

    refs 只放**标识**而不是结论正文:后续读取永远回原始记录取,避免摘要压摘要。
    """

    text: str
    occurred_at: str
    gap_hours: float
    refs: tuple[dict[str, str], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "occurred_at": self.occurred_at,
            "gap_hours": self.gap_hours,
            "refs": [dict(ref) for ref in self.refs],
        }


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _clip(text: str, limit: int) -> str:
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else body[: max(0, limit - 1)] + "…"


def build_segment_note(
    threads: Sequence[Mapping[str, Any]],
    *,
    switched_at: Any,
    last_user_at: Any,
    min_gap_hours: float = MIN_GAP_HOURS,
    char_limit: int = NOTE_CHAR_LIMIT,
) -> SegmentNote | None:
    """跨段时按需给一张续聊便条;不需要时返回 None。

    threads 是服务里已有的未完结事项(来自 working summary 的 open_threads),
    每项至少要有正文与它自己的事件时间。只挑**在换段之前就已经存在**的那些:
    换段之后才出现的话题本来就在新段里,不需要便条。

    判定顺序:
    1. 没有未完结事项 —— 不写。没有可续的话题就没有便条的意义。
    2. 换段时距(最后一次真实用户互动到换段)短于 min_gap_hours —— 不写。
       同一场对话里换段,原文已经带过去了,再加一张便条只是重复。
    3. 其余情况写一张:第一人称、带时间与来源、只引用不结论。
    """
    switched = _parse(switched_at)
    if switched is None:
        return None
    last = _parse(last_user_at) or switched
    gap_hours = max(0.0, (switched - last).total_seconds() / 3600)
    if gap_hours < min_gap_hours:
        return None

    picked: list[tuple[str, str, Mapping[str, Any]]] = []
    for row in threads:
        text = " ".join(str(row.get("text") or "").split())
        if not text:
            continue
        when = _parse(row.get("event_at") or row.get("at") or row.get("updated_at"))
        # 换段之后才出现的话题属于新段,不该进便条。
        if when is not None and when > switched:
            continue
        picked.append((text, (when or switched).isoformat(), row))
    if not picked:
        return None
    # 最近发生的最可能还没接上;同刻按正文排序,保证同样输入得到同样便条。
    picked.sort(key=lambda item: (item[1], item[0]), reverse=True)
    picked = picked[:MAX_ITEMS]

    when_label = switched.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    share = max(24, char_limit // max(1, len(picked)) - 24)
    lines = ["（" + when_label + ",我们上一次聊到这些,接着往下就行:"]
    for text, _, _ in picked:
        lines.append("· " + _clip(text, share))
    lines.append("）")
    body = "\n".join(lines)
    if len(body) > char_limit:
        body = body[: char_limit - 1] + "…"

    refs = tuple(
        {key: str(row[key]) for key in ("id", "source_key", "event_at", "at") if row.get(key)}
        for _, _, row in picked
    )
    return SegmentNote(text=body, occurred_at=switched.isoformat(), gap_hours=gap_hours, refs=refs)


def note_is_stale(note: Mapping[str, Any], deleted_refs: Iterable[str]) -> bool:
    """便条引用的来源已被删除时,这张便条不能再出现。

    便条只存引用,所以这里只比标识:任何一处引用被删,就把整张便条作废而不是
    重写它 —— 重写等于拿旧结论再生一条,正是「摘要压摘要」那种退化。
    """
    gone = {str(item) for item in deleted_refs}
    if not gone:
        return False
    for ref in note.get("refs") or ():
        for value in (ref or {}).values():
            if str(value) in gone:
                return True
    return False


__all__ = [
    "MAX_ITEMS",
    "MIN_GAP_HOURS",
    "NOTE_CHAR_LIMIT",
    "SegmentNote",
    "build_segment_note",
    "note_is_stale",
]
