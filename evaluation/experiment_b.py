"""Experiment B — system-level baseline: actual Open SWE vs full SWE-Forge.

Kept strictly separate from Experiment A (the architectural ablation in
``evaluation/runner.py``), because they answer different questions:

* **Experiment A** — "does each SWE-Forge component help?" All five variants are
  SWE-Forge; the baseline is a *stripped SWE-Forge graph*.
* **Experiment B** — "does SWE-Forge improve an existing autonomous SWE system?"
  The baseline is the *real upstream Open SWE agent*.

Conflating the two would misrepresent Experiment A's `A_baseline` as an Open SWE
comparison, which it is not. That conflation existed before Phase 23 and is
corrected here and in ``docs/EVALUATION.md``.

If upstream cannot execute (missing dependencies, no model credential, no
sandbox provider) the baseline side is reported ``unavailable`` with the precise
reason. No baseline number is ever synthesised.
"""

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from agent.sweforge.graph.workflow import WorkflowConfig
from agent.sweforge.models.scripted import ScriptedModelFactory
from agent.sweforge.routing.model_router import ModelRouter
from agent.sweforge.runner import run_task
from evaluation.baselines import OpenSWEBaseline, describe_baseline_availability
from evaluation.scenarios import all_scenarios

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
RESULTS_ROOT = Path(__file__).parent / "results"


def run_pair(scenario: Any, *, model: str | None = None) -> dict[str, Any]:
    """Run one scenario against both systems, on identical starting code."""
    record: dict[str, Any] = {"scenario_id": scenario.id, "task": scenario.task}

    # --- Side 1: actual upstream Open SWE ------------------------------------
    with tempfile.TemporaryDirectory(prefix="expb-baseline-") as tmp:
        repo = Path(tmp) / scenario.fixture
        shutil.copytree(FIXTURE_ROOT / scenario.fixture, repo)
        baseline = OpenSWEBaseline()
        outcome = baseline.run(
            task=scenario.task,
            repo_root=str(repo),
            repository=f"fixtures/{scenario.fixture}",
            model=model,
            timeout_seconds=scenario.timeout_seconds,
        )
        record["open_swe"] = outcome.to_dict()

    # --- Side 2: SWE-Forge, same fixture, same starting code ----------------
    with tempfile.TemporaryDirectory(prefix="expb-sweforge-") as tmp:
        repo = Path(tmp) / scenario.fixture
        shutil.copytree(FIXTURE_ROOT / scenario.fixture, repo)
        router = ModelRouter(env={}, model_factory=ScriptedModelFactory(scenario.script))
        started = time.perf_counter()
        try:
            result = run_task(
                task=scenario.task,
                repo_root=str(repo),
                repository=f"fixtures/{scenario.fixture}",
                config=WorkflowConfig(),
                router=router,
                backend_kind="local",
                memory_path=str(repo / ".sweforge" / "experience.jsonl"),
            )
            record["sweforge"] = {
                "available": True,
                "status": result.final_status,
                "metrics": result.metrics(),
                "wall_time_seconds": round(time.perf_counter() - started, 4),
                "model_mode": "scripted-deterministic",
            }
        except Exception as exc:
            record["sweforge"] = {
                "available": False,
                "status": "unavailable",
                "unavailable_reason": f"{type(exc).__name__}: {exc}",
            }

    record["comparable"] = bool(
        record["open_swe"].get("available") and record["sweforge"].get("available")
    )
    return record


def run_experiment_b(
    *, model: str | None = None, scenario_ids: list[str] | None = None
) -> dict[str, Any]:
    scenarios = [s for s in all_scenarios() if not scenario_ids or s.id in scenario_ids]
    started = time.perf_counter()
    records = [run_pair(s, model=model) for s in scenarios]
    availability = describe_baseline_availability()
    comparable = sum(1 for r in records if r["comparable"])
    return {
        "experiment": "B_system_baseline",
        "question": "Does SWE-Forge improve on the actual Open SWE system?",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_seconds": round(time.perf_counter() - started, 4),
        "baseline_availability": availability,
        "comparable_pairs": comparable,
        "notes": [
            "The baseline side invokes the real upstream agent.server.get_agent path.",
            "When upstream cannot execute, the pair is reported UNAVAILABLE with the "
            "precise missing dependency or credential. No baseline result is synthesised.",
            "A head-to-head verdict requires comparable_pairs > 0.",
        ]
        + (
            []
            if comparable
            else ["comparable_pairs == 0: NO head-to-head conclusion may be drawn from this run."]
        ),
        "records": records,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Experiment B: Open SWE vs SWE-Forge.")
    parser.add_argument("--model", default=None, help="Model id to use for BOTH systems.")
    parser.add_argument("--scenarios", nargs="*", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    payload = run_experiment_b(model=args.model, scenario_ids=args.scenarios)
    target = Path(args.out or RESULTS_ROOT / "experiment_b.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"Experiment B: {len(payload['records'])} scenario pair(s)")
    print(f"  comparable pairs: {payload['comparable_pairs']}")
    if not payload["comparable_pairs"]:
        print(f"  baseline UNAVAILABLE: {payload['baseline_availability']['reason']}")
        print("  -> no head-to-head conclusion is drawn (by design)")
    print(f"  written to {target}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
