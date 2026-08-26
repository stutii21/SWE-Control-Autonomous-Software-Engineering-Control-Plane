"""Component-level experiments (Phase 25, Parts 8-11).

Experiment A measures orchestration end-to-end, but it is structurally blind to
repository intelligence, memory and model routing: the scripted model's output
is pinned, so richer planning evidence cannot change the plan. Phase 24 reported
that as a negative result rather than hiding it.

These experiments measure those components **directly**, where a scripted model
is not in the causal path:

* :func:`run_retrieval_experiment` — ranking quality against deterministic
  ground truth (no LLM involved at all).
* :func:`run_memory_experiment` — does retrieved prior experience change the
  evidence a planner would see?
* :func:`run_routing_experiment` — what does adaptive tier selection cost
  relative to a fixed model?
* :func:`run_recovery_matrix` — recovery outcomes broken out by failure category.

Every function returns machine-readable results and states its own limits. Where
an effect is neutral or negative, that is the reported finding.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.sweforge.memory.store import ExperienceStore
from agent.sweforge.recovery.classifier import FailureClassifier
from agent.sweforge.repository.analyzer import RepositoryAnalyzer
from agent.sweforge.repository.graph_index import RepositoryGraph, tokenize
from agent.sweforge.routing.model_router import ModelRouter
from agent.sweforge.schemas import ExperienceRecord, VerificationResult

FIXTURE_ROOT = Path(__file__).parent / "fixtures"


# ==========================================================================
# PART 8 — repository retrieval benchmark
# ==========================================================================
@dataclass
class RetrievalTask:
    """One retrieval query with deterministic ground truth.

    Ground truth is asserted by construction: these are small fixtures whose
    correct answer is unambiguous. That is the point — a retrieval metric is
    only meaningful when the gold set is not itself a judgement call.
    """

    task_id: str
    fixture: str
    query: str
    relevant_files: list[str]
    relevant_tests: list[str] = field(default_factory=list)


def retrieval_tasks() -> list[RetrievalTask]:
    return [
        RetrievalTask(
            task_id="ret-inventory-threshold",
            fixture="inventory",
            query="restock threshold inclusive boundary for items",
            relevant_files=["inventory.py"],
            relevant_tests=["tests/test_inventory.py"],
        ),
        RetrievalTask(
            task_id="ret-billing-discount",
            fixture="billing",
            query="apply discount percent to invoice total",
            relevant_files=["billing.py"],
            relevant_tests=["tests/test_billing.py"],
        ),
        RetrievalTask(
            task_id="ret-textutil-slugify",
            fixture="textutil",
            query="slugify collapse whitespace into hyphen",
            relevant_files=["textutil.py"],
            relevant_tests=["tests/test_textutil.py"],
        ),
        RetrievalTask(
            task_id="ret-pipeline-deploy",
            fixture="pipeline",
            query="deployment target environment production check",
            relevant_files=["deploy.py"],
            relevant_tests=["tests/test_deploy.py"],
        ),
    ]


def _lexical_rank(graph: RepositoryGraph, query: str, limit: int) -> list[str]:
    """Strategy A: path/identifier overlap only, no graph expansion.

    This is the ablation control for graph-aware retrieval.
    """
    terms = set(tokenize(query))
    scored: list[tuple[float, str]] = []
    for path, info in graph.map.files.items():
        tokens = set(tokenize(path.replace("/", " ")))
        tokens.update(t for s in info.symbols for t in tokenize(s.name))
        overlap = len(terms & tokens)
        if overlap:
            scored.append((float(overlap), path))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [p for _s, p in scored[:limit]]


def _graph_rank(graph: RepositoryGraph, query: str, limit: int) -> list[str]:
    """Strategy B: full SWE-Forge ranking (symbols, docstrings, import expansion)."""
    return [h.path for h in graph.find_related_files(query, limit=limit)]


def _hybrid_rank(graph: RepositoryGraph, query: str, limit: int) -> list[str]:
    """Strategy C: lexical hits first, then graph results appended."""
    seen: list[str] = []
    for path in _lexical_rank(graph, query, limit) + _graph_rank(graph, query, limit):
        if path not in seen:
            seen.append(path)
    return seen[:limit]


def _precision_at_k(ranked: list[str], gold: set[str], k: int) -> float:
    top = ranked[:k]
    if not top:
        return 0.0
    return round(len([p for p in top if p in gold]) / len(top), 4)


def _recall_at_k(ranked: list[str], gold: set[str], k: int) -> float:
    if not gold:
        return 0.0
    return round(len([p for p in ranked[:k] if p in gold]) / len(gold), 4)


def _mrr(ranked: list[str], gold: set[str]) -> float:
    for index, path in enumerate(ranked, start=1):
        if path in gold:
            return round(1.0 / index, 4)
    return 0.0


def run_retrieval_experiment(*, limit: int = 5) -> dict[str, Any]:
    """Compare lexical vs graph-aware vs hybrid retrieval on ground truth."""
    strategies = {
        "A_lexical": _lexical_rank,
        "B_graph": _graph_rank,
        "C_hybrid": _hybrid_rank,
    }
    per_task: list[dict[str, Any]] = []
    totals: dict[str, dict[str, list[float]]] = {
        name: {"p1": [], "p3": [], "p5": [], "r5": [], "mrr": [], "latency": []}
        for name in strategies
    }

    for task in retrieval_tasks():
        root = FIXTURE_ROOT / task.fixture
        graph = RepositoryGraph(RepositoryAnalyzer().analyze(root))
        gold = set(task.relevant_files) | set(task.relevant_tests)
        row: dict[str, Any] = {"task_id": task.task_id, "gold": sorted(gold)}
        for name, fn in strategies.items():
            started = time.perf_counter()
            ranked = fn(graph, task.query, limit)
            latency = time.perf_counter() - started
            metrics = {
                "p1": _precision_at_k(ranked, gold, 1),
                "p3": _precision_at_k(ranked, gold, 3),
                "p5": _precision_at_k(ranked, gold, 5),
                "r5": _recall_at_k(ranked, gold, 5),
                "mrr": _mrr(ranked, gold),
                "latency": round(latency, 6),
            }
            for key, value in metrics.items():
                totals[name][key].append(value)
            row[name] = {**metrics, "ranked": ranked[:5]}
        per_task.append(row)

    def _mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    summary = {
        name: {key: _mean(values) for key, values in metrics.items()}
        for name, metrics in totals.items()
    }
    return {
        "experiment": "repository_retrieval",
        "sample_size": len(per_task),
        "deterministic": True,
        "model_used": None,
        "notes": [
            "No LLM is involved: this measures the ranking function directly.",
            "Ground truth is deterministic by construction on small fixtures.",
            f"n={len(per_task)} is far too small for statistical claims; these are "
            "directional measurements of ranking behaviour, not a benchmark result.",
        ],
        "summary": summary,
        "per_task": per_task,
    }


# ==========================================================================
# PART 9 — memory experiment
# ==========================================================================
def run_memory_experiment(*, tmp_path: Path | None = None) -> dict[str, Any]:
    """M0 (no retrieval) vs M1 (BM25 experience retrieval).

    Measures whether prior experience is *retrievable and relevant* for a
    related follow-up task — i.e. whether the planner's context differs at all.
    It deliberately does **not** claim a task-success improvement: with scripted
    models the plan is pinned, so no end-to-end effect is measurable here.
    """
    base = Path(tmp_path or ".sweforge/experiments")
    base.mkdir(parents=True, exist_ok=True)
    store_path = base / "memory_experiment.jsonl"
    if store_path.exists():
        store_path.unlink()

    prior = [
        ExperienceRecord(
            task="fix restock_needed threshold boundary in inventory",
            repository="fixtures/inventory",
            final_status="completed",
            relevant_files=["inventory.py"],
            failure_categories=["test_assertion"],
            recovery_strategies=["use an inclusive <= comparison"],
            recovery_attempts=1,
            lesson="threshold comparisons are inclusive at the reorder point",
        ),
        ExperienceRecord(
            task="validate invoice_total inputs in billing",
            repository="fixtures/billing",
            final_status="completed",
            relevant_files=["billing.py"],
            lesson="validate all inputs, not just the first",
        ),
        ExperienceRecord(
            task="update frontend button colour",
            repository="fixtures/other",
            final_status="completed",
            relevant_files=["button.tsx"],
            lesson="unrelated",
        ),
    ]
    store = ExperienceStore(store_path)
    for record in prior:
        store.add(record)

    follow_ups = [
        (
            "mem-inventory",
            "restock_needed boundary is still wrong for inventory items",
            "fixtures/inventory",
            "inventory.py",
        ),
        (
            "mem-billing",
            "invoice_total should validate the tax rate in billing",
            "fixtures/billing",
            "billing.py",
        ),
    ]

    rows: list[dict[str, Any]] = []
    for task_id, query, repo, expected_file in follow_ups:
        started = time.perf_counter()
        retrieved = store.retrieve(query, limit=3, repository=repo)
        latency = time.perf_counter() - started
        m1_context = ExperienceStore.render_context(retrieved)
        top = retrieved[0].record if retrieved else None
        rows.append(
            {
                "task_id": task_id,
                "M0_context_chars": 0,
                "M1_context_chars": len(m1_context),
                "M1_retrieved": [r.record.task[:60] for r in retrieved],
                "M1_top_is_relevant": bool(top and expected_file in top.relevant_files),
                "M1_surfaced_prior_files": sorted(
                    {f for r in retrieved for f in r.record.relevant_files}
                ),
                "M1_retrieval_latency_s": round(latency, 6),
            }
        )

    relevant_hits = sum(1 for r in rows if r["M1_top_is_relevant"])
    return {
        "experiment": "experience_memory",
        "sample_size": len(rows),
        "deterministic": True,
        "corpus_size": len(store),
        "variants": {
            "M0": "no experience retrieval (planner sees repository evidence only)",
            "M1": "BM25 experience retrieval injected into planner context",
        },
        "summary": {
            "top1_relevant_rate": round(relevant_hits / len(rows), 4) if rows else None,
            "mean_context_chars_M0": 0,
            "mean_context_chars_M1": round(sum(r["M1_context_chars"] for r in rows) / len(rows), 1)
            if rows
            else 0,
        },
        "notes": [
            "MEASURED: whether relevant prior experience is retrieved for a related task.",
            "NOT MEASURED: whether that context improves task success. Under scripted "
            "models the plan is pinned, so no end-to-end effect is observable. Any "
            "success claim requires the live-model track.",
            f"n={len(rows)}: directional only.",
        ],
        "per_task": rows,
    }


# ==========================================================================
# PART 10 — model routing experiment
# ==========================================================================
#: Representative role sequence for one task, used by both variants so the
#: comparison is like-for-like.
ROLE_SEQUENCE = [
    ("repository_analysis", 1),
    ("complexity_analysis", 1),
    ("planning", 1),
    ("implementation", 2),
    ("failure_analysis", 1),
    ("recovery", 1),
    ("review", 1),
    ("security_analysis", 1),
    ("summarisation", 1),
]

#: Nominal tokens per call, held constant across variants so only tier differs.
TOKENS_PER_CALL = (4000, 800)


def run_routing_experiment() -> dict[str, Any]:
    """R0 fixed model vs R1 adaptive tier routing, on identical workloads.

    Measures the *cost consequence* of routing under an identical call pattern.
    It does not claim a success-rate benefit: task success is unchanged by
    construction here, and asserting otherwise would require a live model.
    """
    complexities = ["trivial", "simple", "moderate", "complex"]
    rows: list[dict[str, Any]] = []

    for complexity in complexities:
        # R0: one fixed model for everything (the common baseline design).
        r0 = ModelRouter(env={})
        fixed_tier = "reasoning"
        r0_cost = 0.0
        r0_calls = 0
        for _role, count in ROLE_SEQUENCE:
            for _ in range(count):
                r0_cost += r0.estimate_cost(fixed_tier, *TOKENS_PER_CALL)
                r0_calls += 1

        # R1: SWE-Forge adaptive routing.
        r1 = ModelRouter(env={})
        r1_cost = 0.0
        r1_calls = 0
        tiers: dict[str, int] = {}
        for role, count in ROLE_SEQUENCE:
            tier, _reason = r1.resolve_tier(role, complexity=complexity)
            for _ in range(count):
                r1_cost += r1.estimate_cost(tier, *TOKENS_PER_CALL)
                r1_calls += 1
                tiers[tier] = tiers.get(tier, 0) + 1

        rows.append(
            {
                "complexity": complexity,
                "calls": r0_calls,
                "R0_fixed_tier": fixed_tier,
                "R0_estimated_cost_usd": round(r0_cost, 6),
                "R1_tier_distribution": dict(sorted(tiers.items())),
                "R1_estimated_cost_usd": round(r1_cost, 6),
                "cost_delta_usd": round(r1_cost - r0_cost, 6),
                "cost_ratio": round(r1_cost / r0_cost, 4) if r0_cost else None,
            }
        )

    mean_ratio = round(sum(r["cost_ratio"] for r in rows) / len(rows), 4)
    return {
        "experiment": "model_routing",
        "sample_size": len(rows),
        "deterministic": True,
        "variants": {
            "R0": "fixed reasoning-tier model for every role",
            "R1": "SWE-Forge adaptive per-role tier routing",
        },
        "summary": {
            "mean_cost_ratio_R1_over_R0": mean_ratio,
            "calls_per_task": rows[0]["calls"],
            "note": "call count is identical by construction; only tier differs",
        },
        "notes": [
            "MEASURED: estimated cost consequence of tier selection under an "
            "identical call pattern and identical nominal token counts.",
            "NOT MEASURED: whether routing preserves task success. Under scripted "
            "models success is fixed by the fixture, so the reliability half of the "
            "cost/reliability trade-off is UNTESTED and no such claim is made.",
            "Costs use the configurable price table; they are estimates, not invoices.",
        ],
        "per_complexity": rows,
    }


# ==========================================================================
# PART 11 — recovery matrix
# ==========================================================================
#: Every category the deterministic classifier can emit.
FAILURE_CATEGORIES = [
    "syntax",
    "dependency",
    "type",
    "test_assertion",
    "runtime",
    "configuration",
    "environment",
    "lint",
    "timeout",
    "unknown",
]

#: Real runner output samples, used to verify detection per category.
CATEGORY_SAMPLES: dict[str, str] = {
    "syntax": "E   SyntaxError: expected ':'",
    "dependency": "E   ModuleNotFoundError: No module named 'foo'",
    "type": "E   TypeError: unsupported operand type(s)",
    "test_assertion": "E       assert -1 == 5\nE   AssertionError",
    "runtime": "E   ValueError: boom",
    "configuration": "E   KeyError: 'DATABASE_URL'",
    "environment": "E   PermissionError: denied",
    "lint": "mod.py:1:1: F401 unused import",
    "timeout": "command timed out after 300s",
    "unknown": "something entirely inscrutable",
}


def run_recovery_matrix(results_path: str | Path | None = None) -> dict[str, Any]:
    """Recovery outcomes broken out by failure category.

    Detection and diagnosis are measured directly from the classifier. Recovery
    and success columns are populated only from **actual benchmark runs**;
    categories with no benchmark example are marked ``untested`` rather than
    assumed to work.
    """
    classifier = FailureClassifier()

    # Detection/diagnosis: measured from real runner output.
    detection: dict[str, dict[str, Any]] = {}
    for category, sample in CATEGORY_SAMPLES.items():
        result = VerificationResult(passed=False, tests_run=1, tests_failed=1, output=sample)
        classification = classifier.classify(result)
        detection[category] = {
            "detected_as": classification.category,
            "correct": classification.category == category,
            "confidence": classification.confidence,
        }

    # Recovery: read from the deterministic evaluation results if present.
    observed: dict[str, dict[str, Any]] = {}
    path = Path(results_path or Path(__file__).parent / "results" / "results.json")
    if path.exists():
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        for record in payload.get("records", []):
            if record.get("variant") != "E_full" or not record.get("available"):
                continue
            metrics = record.get("metrics", {})
            attempts = int(metrics.get("recovery_attempts") or 0)
            if attempts <= 0:
                continue
            # Category comes from the scenario tags, which name the seeded failure.
            tags = [t for t in record.get("tags", []) if t in FAILURE_CATEGORIES]
            category = tags[0] if tags else "unknown"
            bucket = observed.setdefault(category, {"runs": 0, "successes": 0, "attempts": 0})
            bucket["runs"] += 1
            bucket["attempts"] += attempts
            if metrics.get("verification_passed"):
                bucket["successes"] += 1

    matrix: list[dict[str, Any]] = []
    for category in FAILURE_CATEGORIES:
        det = detection[category]
        run = observed.get(category)
        matrix.append(
            {
                "failure_type": category,
                "detection": "correct"
                if det["correct"]
                else f"misdetected as {det['detected_as']}",
                "diagnosis_confidence": det["confidence"],
                "recovery_attempted": bool(run),
                "recovery_runs": run["runs"] if run else 0,
                "recovery_successes": run["successes"] if run else 0,
                "success_rate": (
                    round(run["successes"] / run["runs"], 4) if run and run["runs"] else None
                ),
                "avg_attempts": (
                    round(run["attempts"] / run["runs"], 3) if run and run["runs"] else None
                ),
                "status": "measured" if run else "untested",
            }
        )

    tested = [row for row in matrix if row["status"] == "measured"]
    return {
        "experiment": "recovery_matrix",
        "deterministic": True,
        "detection_accuracy": round(
            sum(1 for d in detection.values() if d["correct"]) / len(detection), 4
        ),
        "categories_total": len(matrix),
        "categories_measured": len(tested),
        "categories_untested": len(matrix) - len(tested),
        "notes": [
            "Detection/diagnosis measured directly from real runner output samples.",
            "Recovery columns populated only from executed benchmark runs; categories "
            "with no benchmark example are marked 'untested', never assumed.",
        ],
        "matrix": matrix,
    }
