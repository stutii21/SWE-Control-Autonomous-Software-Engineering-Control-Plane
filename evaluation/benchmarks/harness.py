"""Real-world benchmark harness (Phase 25, Part 12).

Implements the *infrastructure* for a genuine public benchmark
(SWE-bench Lite format) so that a real run becomes a matter of supplying
credentials rather than writing code.

Three rules govern this module:

1. **Nothing is downloaded or executed automatically.** A benchmark run must be
   explicitly configured; the default path is a dry run that validates parsing
   and planning only.
2. **Both systems run the same task.** The harness drives Open SWE and SWE-Forge
   against the same repository, commit, task text, model and timeout.
3. **No result is claimed until a run occurs.** `REAL_WORLD_BENCHMARK` is
   `NOT_AVAILABLE` in this environment, and the scorer refuses to emit a verdict
   from zero completed tasks.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"
REAL_WORLD_BENCHMARK_STATUS = "NOT_AVAILABLE"

#: Minimum completed tasks before any comparative verdict is permitted.
MIN_TASKS_FOR_VERDICT = 20


@dataclass
class BenchmarkTask:
    """One real-world benchmark task (SWE-bench Lite compatible fields)."""

    task_id: str
    repository: str
    base_commit: str
    task_description: str
    test_command: str
    timeout_seconds: int = 900
    language: str = "python"
    difficulty: str = "unknown"
    category: str = "bugfix"
    expected_patch_source: str | None = None
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BenchmarkSchemaError(ValueError):
    """Raised when a task record does not satisfy the schema."""


REQUIRED_FIELDS = ("task_id", "repository", "base_commit", "task_description", "test_command")


def validate_task(record: dict[str, Any]) -> BenchmarkTask:
    """Validate one record and construct a task. Raises on malformed input."""
    missing = [f for f in REQUIRED_FIELDS if not str(record.get(f, "")).strip()]
    if missing:
        raise BenchmarkSchemaError(f"missing required field(s): {', '.join(missing)}")
    timeout = record.get("timeout_seconds", 900)
    try:
        timeout = int(timeout)
    except (TypeError, ValueError) as exc:
        raise BenchmarkSchemaError(f"timeout_seconds must be an integer: {timeout!r}") from exc
    if timeout <= 0:
        raise BenchmarkSchemaError("timeout_seconds must be positive")
    return BenchmarkTask(
        task_id=str(record["task_id"]),
        repository=str(record["repository"]),
        base_commit=str(record["base_commit"]),
        task_description=str(record["task_description"]),
        test_command=str(record["test_command"]),
        timeout_seconds=timeout,
        language=str(record.get("language", "python")),
        difficulty=str(record.get("difficulty", "unknown")),
        category=str(record.get("category", "bugfix")),
        expected_patch_source=record.get("expected_patch_source"),
        fail_to_pass=list(record.get("fail_to_pass") or record.get("FAIL_TO_PASS") or []),
        pass_to_pass=list(record.get("pass_to_pass") or record.get("PASS_TO_PASS") or []),
    )


def load_tasks(path: str | Path) -> list[BenchmarkTask]:
    """Load tasks from JSON or JSON Lines. Malformed records raise, not skip."""
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"benchmark file not found: {source}")
    text = source.read_text(encoding="utf-8").strip()
    if not text:
        return []
    records: list[dict[str, Any]]
    if source.suffix == ".jsonl":
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        loaded = json.loads(text)
        records = loaded if isinstance(loaded, list) else loaded.get("tasks", [])
    return [validate_task(r) for r in records]


@dataclass
class BenchmarkRunResult:
    """Outcome of one system on one task."""

    task_id: str
    system: str  # "open_swe" | "sweforge"
    available: bool = False
    status: str = "unavailable"
    unavailable_reason: str | None = None
    resolved: bool | None = None
    tests_passed: int | None = None
    tests_failed: int | None = None
    model_calls: int | None = None
    tool_calls: int | None = None
    recovery_attempts: int | None = None
    latency_seconds: float | None = None
    estimated_cost_usd: float | None = None
    human_intervention: bool | None = None
    security_intervention: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def dry_run(tasks: list[BenchmarkTask]) -> dict[str, Any]:
    """Validate the harness end-to-end without executing anything.

    Confirms tasks parse, both system adapters are reachable, and reports which
    prerequisites are missing — the maximum that can honestly be done without
    credentials.
    """
    from evaluation.baselines import describe_baseline_availability
    from evaluation.live import describe_live_availability

    baseline = describe_baseline_availability()
    live = describe_live_availability()
    blockers: list[str] = []
    if not baseline["can_run"]:
        blockers.append(f"open_swe: {baseline['reason']}")
    if not live["available"]:
        blockers.append(f"live_model: {live['reason']}")

    return {
        "mode": "dry_run",
        "schema_version": SCHEMA_VERSION,
        "real_world_benchmark": REAL_WORLD_BENCHMARK_STATUS,
        "tasks_parsed": len(tasks),
        "task_ids": [t.task_id for t in tasks],
        "languages": sorted({t.language for t in tasks}),
        "executed": False,
        "open_swe_available": baseline["can_run"],
        "live_model_available": live["available"],
        "blockers": blockers,
        "notes": [
            "Dry run validates parsing and adapter reachability only.",
            "NOTHING was downloaded or executed.",
            "No benchmark performance is claimed. REAL_WORLD_BENCHMARK = NOT_AVAILABLE.",
        ],
    }


def score_results(results: list[BenchmarkRunResult]) -> dict[str, Any]:
    """Score paired results, refusing to conclude from an insufficient sample."""
    by_system: dict[str, list[BenchmarkRunResult]] = {}
    for result in results:
        by_system.setdefault(result.system, []).append(result)

    summary: dict[str, Any] = {"systems": {}}
    for system, rows in by_system.items():
        completed = [r for r in rows if r.available]
        resolved = [r for r in completed if r.resolved]
        summary["systems"][system] = {
            "attempted": len(rows),
            "completed": len(completed),
            "unavailable": len(rows) - len(completed),
            "resolved": len(resolved),
            "resolve_rate": (round(len(resolved) / len(completed), 4) if completed else None),
        }

    paired = _pair(by_system)
    summary["paired_tasks"] = len(paired)
    if len(paired) < MIN_TASKS_FOR_VERDICT:
        summary["verdict"] = "INSUFFICIENT_SAMPLE"
        summary["verdict_reason"] = (
            f"{len(paired)} paired task(s) completed; a comparative claim requires at "
            f"least {MIN_TASKS_FOR_VERDICT}. No conclusion is drawn."
        )
        return summary

    summary.update(_paired_statistics(paired))
    summary["verdict"] = "PILOT" if len(paired) < 50 else "REPORTED"
    return summary


def _pair(
    by_system: dict[str, list[BenchmarkRunResult]],
) -> list[tuple[BenchmarkRunResult, BenchmarkRunResult]]:
    """Pair results by task_id; only tasks both systems completed count."""
    forge = {r.task_id: r for r in by_system.get("sweforge", []) if r.available}
    swe = {r.task_id: r for r in by_system.get("open_swe", []) if r.available}
    return [(swe[t], forge[t]) for t in sorted(set(forge) & set(swe))]


def _paired_statistics(
    paired: list[tuple[BenchmarkRunResult, BenchmarkRunResult]],
) -> dict[str, Any]:
    """McNemar counts and a bootstrap CI on the paired success difference."""
    import random

    both = wins_forge = wins_swe = neither = 0
    diffs: list[int] = []
    for swe, forge in paired:
        a, b = bool(swe.resolved), bool(forge.resolved)
        if a and b:
            both += 1
        elif b and not a:
            wins_forge += 1
        elif a and not b:
            wins_swe += 1
        else:
            neither += 1
        diffs.append(int(b) - int(a))

    # McNemar on discordant pairs only; exact binomial two-sided.
    discordant = wins_forge + wins_swe
    p_value: float | None = None
    if discordant:
        from math import comb

        k = min(wins_forge, wins_swe)
        tail = sum(comb(discordant, i) for i in range(k + 1)) / (2**discordant)
        p_value = round(min(1.0, 2 * tail), 6)

    rng = random.Random(0)  # fixed seed: reproducible CI
    samples = []
    for _ in range(2000):
        draw = [diffs[rng.randrange(len(diffs))] for _ in diffs]
        samples.append(sum(draw) / len(draw))
    samples.sort()
    lower = samples[int(0.025 * len(samples))]
    upper = samples[int(0.975 * len(samples))]

    return {
        "mcnemar": {
            "both_resolved": both,
            "sweforge_only": wins_forge,
            "open_swe_only": wins_swe,
            "neither": neither,
            "discordant": discordant,
            "p_value": p_value,
        },
        "paired_success_difference": round(sum(diffs) / len(diffs), 4),
        "bootstrap_95ci": [round(lower, 4), round(upper, 4)],
        "bootstrap_resamples": 2000,
        "bootstrap_seed": 0,
    }
