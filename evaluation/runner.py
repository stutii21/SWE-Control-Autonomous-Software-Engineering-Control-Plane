"""Evaluation runner.

Executes scenarios against real fixture repositories. Each run gets a pristine
copy of the fixture in a temporary directory, so a run that mutates code cannot
contaminate the next one — a property the ablation study depends on, since the
same scenario is executed once per variant.

Workflow variants (the ablation axis)
-------------------------------------
A. ``baseline``          single pass: plan -> implement -> verify -> stop
B. ``repo_intel``        + repository intelligence (AST + import graph)
C. ``recovery``          + bounded self-repair loop
D. ``reviewer``          + independent review gate
E. ``full``              + security scan and risk gate (complete SWE-Forge)

Variant A is the *fixed single-agent* comparison required by the
technology-level evaluation: one linear pass, no adaptive routing.
"""

import json
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent.sweforge.graph.workflow import WorkflowConfig
from agent.sweforge.models.scripted import ScriptedModelFactory
from agent.sweforge.routing.model_router import ModelRouter
from agent.sweforge.runner import run_task
from evaluation.scenarios import Scenario, all_scenarios

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
RESULTS_ROOT = Path(__file__).parent / "results"


def variant_configs() -> dict[str, WorkflowConfig]:
    """The five ablation variants, cumulative from baseline to full."""
    return {
        "A_baseline": WorkflowConfig(
            enable_repository_intelligence=False,
            enable_recovery=False,
            enable_review=False,
            enable_security_gate=False,
            enable_memory=False,
            variant_name="A_baseline",
        ),
        "B_repo_intel": WorkflowConfig(
            enable_repository_intelligence=True,
            enable_recovery=False,
            enable_review=False,
            enable_security_gate=False,
            enable_memory=False,
            variant_name="B_repo_intel",
        ),
        "C_recovery": WorkflowConfig(
            enable_repository_intelligence=True,
            enable_recovery=True,
            enable_review=False,
            enable_security_gate=False,
            enable_memory=False,
            variant_name="C_recovery",
        ),
        "D_reviewer": WorkflowConfig(
            enable_repository_intelligence=True,
            enable_recovery=True,
            enable_review=True,
            enable_security_gate=False,
            enable_memory=False,
            variant_name="D_reviewer",
        ),
        "E_full": WorkflowConfig(
            enable_repository_intelligence=True,
            enable_recovery=True,
            enable_review=True,
            enable_security_gate=True,
            enable_memory=True,
            variant_name="E_full",
        ),
    }


@dataclass
class RunRecord:
    """One (scenario, variant) execution."""

    scenario_id: str
    variant: str
    status: str = "unavailable"
    available: bool = True
    error: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    node_trace: list[str] = field(default_factory=list)
    expectations: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


def _prepare_fixture(scenario: Scenario, workdir: Path) -> Path:
    """Copy the fixture into an isolated working directory."""
    source = FIXTURE_ROOT / scenario.fixture
    if not source.is_dir():
        raise FileNotFoundError(f"fixture not found: {source}")
    target = workdir / scenario.fixture
    shutil.copytree(source, target)
    return target


def run_scenario(scenario: Scenario, variant: str, config: WorkflowConfig) -> RunRecord:
    """Execute one scenario under one variant in an isolated fixture copy."""
    record = RunRecord(
        scenario_id=scenario.id,
        variant=variant,
        tags=list(scenario.tags),
        expectations={
            "expected_status": scenario.expected_status,
            "expected_verification": scenario.expected_verification,
            "expects_recovery": scenario.expects_recovery,
            "expects_review_rejection": scenario.expects_review_rejection,
            "expects_high_risk": scenario.expects_high_risk,
        },
    )
    with tempfile.TemporaryDirectory(prefix="sweforge-eval-") as tmp:
        try:
            repo_root = _prepare_fixture(scenario, Path(tmp))
        except FileNotFoundError as exc:
            record.available = False
            record.error = str(exc)
            return record

        # A fresh factory per run: scripts must not leak across variants.
        factory = ScriptedModelFactory(scenario.script)
        router = ModelRouter(env={}, model_factory=factory)

        try:
            outcome = run_task(
                task=scenario.task,
                repo_root=str(repo_root),
                repository=f"fixtures/{scenario.fixture}",
                config=config,
                router=router,
                backend_kind="local",
                memory_path=str(repo_root / ".sweforge" / "experience.jsonl"),
            )
        except Exception as exc:
            record.available = False
            record.error = f"{type(exc).__name__}: {exc}"
            return record

        record.status = outcome.final_status
        record.metrics = outcome.metrics()
        record.metrics["scripted_model_calls"] = factory.call_count()
        record.node_trace = outcome.node_trace
    return record


def run_suite(
    *,
    variants: list[str] | None = None,
    scenario_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Run the full matrix of scenarios x variants."""
    configs = variant_configs()
    chosen_variants = variants or list(configs)
    scenarios = [s for s in all_scenarios() if not scenario_ids or s.id in scenario_ids]

    started = time.perf_counter()
    records: list[RunRecord] = []
    for variant in chosen_variants:
        if variant not in configs:
            raise KeyError(f"unknown variant {variant!r}; known: {sorted(configs)}")
        for scenario in scenarios:
            records.append(run_scenario(scenario, variant, configs[variant]))

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_seconds": round(time.perf_counter() - started, 4),
        "model_mode": "scripted-deterministic",
        "notes": [
            "Model behaviour is pinned by evaluation/scenarios.py so that graph "
            "orchestration is the only variable.",
            "Token counts and therefore cost figures are SYNTHETIC under scripted "
            "models; they demonstrate ledger accounting, not provider billing.",
            "Test results, recovery counts, routing paths, gate decisions and "
            "wall-clock times are REAL measurements from executed runs.",
        ],
        "variants": chosen_variants,
        "scenarios": [s.id for s in scenarios],
        "records": [asdict(r) for r in records],
    }


def _signature(payload: dict[str, Any]) -> str:
    """Stable fingerprint of a run's architectural outcome.

    Covers terminal state, routing path, recovery count and tool sequence —
    the properties deterministic fixtures are supposed to reproduce exactly.
    """
    import hashlib

    parts: list[str] = []
    for record in sorted(
        payload.get("records", []), key=lambda r: (r["variant"], r["scenario_id"])
    ):
        metrics = record.get("metrics") or {}
        parts.append(
            f"{record['variant']}|{record['scenario_id']}|{record.get('status')}|"
            f"{metrics.get('recovery_attempts')}|{metrics.get('tool_calls')}|"
            f"{'>'.join(record.get('node_trace', []))}"
        )
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def save_results(payload: dict[str, Any], path: str | Path | None = None) -> Path:
    target = Path(path or RESULTS_ROOT / "results.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    import argparse

    from evaluation.reproducibility import BENCHMARK_VERSION, RunArtifacts, RunManifest

    parser = argparse.ArgumentParser(description="Run the SWE-Forge evaluation suite.")
    parser.add_argument("--variants", nargs="*", default=None, help="Subset of variants to run.")
    parser.add_argument("--scenarios", nargs="*", default=None, help="Subset of scenario ids.")
    parser.add_argument("--out", default=None, help="Where to write results JSON.")
    parser.add_argument(
        "--seed", type=int, default=0, help="Seed (scripted runs are deterministic)."
    )
    parser.add_argument("--benchmark-version", default=BENCHMARK_VERSION)
    parser.add_argument("--output-dir", default=None, help="Per-run artifact directory root.")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat runs to verify determinism.")
    args = parser.parse_args(argv)

    payload = run_suite(variants=args.variants, scenario_ids=args.scenarios)

    # Determinism verification: repeated scripted runs must be identical.
    if args.repeat > 1:
        signatures = [_signature(payload)]
        for _ in range(args.repeat - 1):
            signatures.append(
                _signature(run_suite(variants=args.variants, scenario_ids=args.scenarios))
            )
        payload["determinism"] = {
            "repeats": args.repeat,
            "identical": len(set(signatures)) == 1,
            "signature": signatures[0],
        }
        print(
            f"determinism over {args.repeat} repeats: "
            f"{'IDENTICAL' if len(set(signatures)) == 1 else 'DIVERGED'}"
        )

    manifest = RunManifest(
        experiment="A_architectural_ablation",
        seed=args.seed,
        benchmark_version=args.benchmark_version,
        scenarios=payload.get("scenarios", []),
        notes=payload.get("notes", []),
    )
    artifacts = RunArtifacts.create(manifest, base=args.output_dir)
    payload["run_id"] = manifest.run_id
    payload["benchmark_version"] = manifest.benchmark_version
    payload["seed"] = manifest.seed
    artifacts.write_json("results.json", payload)
    artifacts.write_csv(
        "metrics.csv",
        [
            {
                "scenario_id": r["scenario_id"],
                "variant": r["variant"],
                "status": r["status"],
                **(r.get("metrics") or {}),
            }
            for r in payload["records"]
        ],
    )
    print(f"run artifacts: {artifacts.root}")

    path = save_results(payload, args.out)
    total = len(payload["records"])
    unavailable = sum(1 for r in payload["records"] if not r["available"])
    print(f"ran {total} (scenario x variant) executions in {payload['duration_seconds']}s")
    if unavailable:
        print(f"  {unavailable} marked UNAVAILABLE")
    print(f"results written to {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
