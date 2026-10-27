"""S06 baseline: measure recall quality and latency on the fixed samples.

`docs/standalone-service.md` §6.2 asks for a baseline over the fixed set that
records Recall@K, irrelevant injection, duplicate mentions and p50/p95 latency,
with the data scale, hardware and concurrency noted. It also fixes the pass
conditions: isolation boundaries and deletion rules are hard requirements,
while quality metrics are recorded for comparison (§6.2/§7 of the plan).

What this script does per case:

1. build a throwaway root, write the case's corpus through the real
   `FactsView`, apply deletions through the real `DeletionService`;
2. run `recall_memory` (explicit recall) and `build_turn_context` (passive);
3. compare returned ids against `expect_recalled` / `expect_excluded`;
4. record elapsed_ms.

It never touches user data: every root is a temporary directory, and the corpus
comes from `.scratch/s06_samples.py` (fully synthetic).
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from companion_memory.adapters.composition import build_memory_core
from companion_memory.contracts import DeletionRequest, DeletionTarget, Scope
from s06_samples import FIXED_NOW, RecallCase, build_cases


class FixedClock:
    def __init__(self) -> None:
        self._now = FIXED_NOW

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return time.monotonic()


def _fact_row(fact, case: str) -> dict[str, Any]:
    """A stored fact row; only the fields recall actually reads."""
    row: dict[str, Any] = {
        "id": fact.id,
        "text": fact.text,
        "subject": fact.subject,
        "schema_version": 2,
        "source": "user_observation",
        "importance": 6,
        "created_at": FIXED_NOW.isoformat(),
        # One episode per fact: deleting fact A must not take fact B with it.
        # Sharing an episode would exercise the (correct) whole-episode cleanup
        # instead, and the case would silently stop testing single-entry
        # deletion.
        "episode_id": f"ep:{case}:{fact.id}",
        "source_event_keys": [f"host:demo:{fact.subject}:{fact.id}"],
    }
    if fact.scene:
        row["applies_to"] = dict(fact.scene)
    return row


async def run_case(case: RecallCase, *, repeat: int = 3) -> dict[str, Any]:
    """Execute one case and return its measurements."""
    with tempfile.TemporaryDirectory(prefix="s06-recall-") as tmp:
        root = Path(tmp) / "memory"
        core = build_memory_core(memory_root=root, clock=FixedClock())
        core.recover("demo")

        rows = [_fact_row(fact, case.name) for fact in case.facts]
        core.facts.save("demo", rows)
        if case.deleted_ids:
            await core.deletion_service.forget(DeletionRequest(
                scope=Scope("demo"),
                mode="entry",
                targets=tuple(
                    DeletionTarget(type="fact", id=entry_id)
                    for entry_id in case.deleted_ids
                ),
                reason="s06 sample",
            ))
            core.barrier.refresh("demo")

        explicit_elapsed: list[float] = []
        passive_elapsed: list[float] = []
        recalled: list[str] = []
        duplicate_hits = 0
        passive_ids: list[str] = []
        measured: list[str] = []

        for _ in range(repeat):
            # Both paths are measured for latency, but only the case's own
            # path decides pass/fail: they are different pipelines and a
            # reference case must not be scored against explicit recall.
            started = time.perf_counter()
            result = await core.recall.recall_memory(
                "demo",
                case.query,
                subject=case.subject or None,
                scene=_scene_of(case),
            )
            explicit_elapsed.append((time.perf_counter() - started) * 1000)
            explicit_ids = [
                str(item.get("id")) for item in result.get("items", [])
                if isinstance(item, dict)
            ]

            started = time.perf_counter()
            passive = await core.recall.build_turn_context(
                "demo",
                current_input=case.query,
                recent_turns=list(case.recent_turns) or None,
                subject=case.subject or None,
                scene=_scene_of(case),
            )
            passive_elapsed.append((time.perf_counter() - started) * 1000)
            passive_ids = [str(item) for item in passive.get("memory_ids", [])]

            measured = explicit_ids if case.path == "explicit" else passive_ids
            recalled = measured
            duplicate_hits = len(measured) - len(set(measured))

        other_path_ids = passive_ids if case.path == "explicit" else explicit_ids
        hit = set(recalled) & set(case.expect_recalled)
        missing = sorted(set(case.expect_recalled) - set(recalled))
        injected = sorted(set(recalled) & set(case.expect_excluded))
        deleted_leaked = sorted(set(recalled) & set(case.deleted_ids))

        return {
            "case": case.name,
            "failure_type": case.failure_type,
            "path": case.path,
            "query": case.query,
            "recalled": recalled,
            "other_path_ids": other_path_ids,
            "expected": list(case.expect_recalled),
            "hit": sorted(hit),
            "missing": missing,
            "injected": injected,
            "deleted_leaked": deleted_leaked,
            "duplicate_hits": duplicate_hits,
            "recall_at_k": (
                len(hit) / len(case.expect_recalled)
                if case.expect_recalled else None
            ),
            "empty_as_expected": (
                (not recalled) if case.expect_empty else None
            ),
            "elapsed_ms": explicit_elapsed,
            "passive_ms": passive_elapsed,
            "status": _status(case, hit, injected, deleted_leaked),
        }


def _scene_of(case: RecallCase):
    if not case.scene:
        return None
    from companion_memory.contracts import Scene

    return Scene(**dict(case.scene))


def _status(
    case: RecallCase, hit: set[str], injected: list[str],
    deleted_leaked: list[str],
) -> str:
    """`fail` only for hard contract breaks; `baseline_gap` for known gaps."""
    if deleted_leaked:
        return "fail"
    if injected:
        return "fail"
    if case.expect_empty:
        return "pass" if not hit else "fail"
    if case.expect_recalled:
        if hit:
            return "pass"
        # A declared-expected gap (synonymy) is a documented quality gap, not
        # a contract break; any other miss is a real miss.
        return "baseline_gap" if case.failure_type == "synonym" else "miss"
    return "pass"


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return round(ordered[index], 2)


def _environment() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "concurrency": "sequential (one case at a time)",
        "vector_backend": "none (BM25 baseline; no embedding provider wired)",
        "corpus_scale": {
            "cases": len(build_cases()),
            "facts_per_case_max": max(len(case.facts) for case in build_cases()),
            "total_facts": sum(len(case.facts) for case in build_cases()),
        },
    }


async def main() -> int:
    cases = build_cases()
    results = [await run_case(case) for case in cases]

    explicit_all = [ms for item in results for ms in item["elapsed_ms"]]
    passive_all = [ms for item in results for ms in item["passive_ms"]]
    hard_failures = [item for item in results if item["status"] == "fail"]
    gaps = [item for item in results if item["status"] in ("baseline_gap", "miss")]

    scored = [item for item in results if item["recall_at_k"] is not None]
    mean_recall = (
        round(sum(item["recall_at_k"] for item in scored) / len(scored), 3)
        if scored else None
    )
    injected_total = sum(len(item["injected"]) for item in results)
    duplicates_total = sum(item["duplicate_hits"] for item in results)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": _environment(),
        "summary": {
            "cases": len(results),
            "hard_failures": len(hard_failures),
            "baseline_gaps": len(gaps),
            "recall_at_k_mean": mean_recall,
            "irrelevant_injections": injected_total,
            "duplicate_mentions": duplicates_total,
            "explicit_ms": {
                "p50": _percentile(explicit_all, 0.5),
                "p95": _percentile(explicit_all, 0.95),
                "max": round(max(explicit_all), 2) if explicit_all else 0.0,
            },
            "passive_ms": {
                "p50": _percentile(passive_all, 0.5),
                "p95": _percentile(passive_all, 0.95),
                "max": round(max(passive_all), 2) if passive_all else 0.0,
            },
        },
        "cases": results,
    }

    out = Path(__file__).resolve().parent / "s06-recall-baseline.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for item in results:
        mark = {"pass": "PASS", "baseline_gap": "GAP ", "miss": "MISS",
                "fail": "FAIL"}[item["status"]]
        detail = ""
        if item["injected"]:
            detail += f" injected={item['injected']}"
        if item["deleted_leaked"]:
            detail += f" DELETED_LEAK={item['deleted_leaked']}"
        if item["missing"]:
            detail += f" missing={item['missing']}"
        print(f"{mark} {item['case']:<26} [{item['failure_type']}]{detail}")

    print()
    print(f"cases={len(results)} hard_failures={len(hard_failures)} "
          f"gaps={len(gaps)} injections={injected_total} duplicates={duplicates_total}")
    print(f"recall@k mean={mean_recall}")
    print(f"explicit recall ms p50/p95/max = "
          f"{report['summary']['explicit_ms']['p50']}/"
          f"{report['summary']['explicit_ms']['p95']}/"
          f"{report['summary']['explicit_ms']['max']}")
    print(f"passive context ms p50/p95/max = "
          f"{report['summary']['passive_ms']['p50']}/"
          f"{report['summary']['passive_ms']['p95']}/"
          f"{report['summary']['passive_ms']['max']}")
    print(f"report: {out}")

    if hard_failures:
        print(f"\nS06 BASELINE FAILED: {len(hard_failures)} hard contract break(s)")
        return 1
    print("\nS06 BASELINE OK: isolation and deletion held; quality gaps recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
