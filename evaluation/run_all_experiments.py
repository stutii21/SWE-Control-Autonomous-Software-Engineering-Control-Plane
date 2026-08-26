"""Run every component experiment and write reproducible artifacts."""

import json

from evaluation.experiments import (
    run_memory_experiment,
    run_recovery_matrix,
    run_retrieval_experiment,
    run_routing_experiment,
)
from evaluation.reproducibility import RunArtifacts, RunManifest


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run SWE-Forge component experiments.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    experiments = {
        "retrieval": run_retrieval_experiment(),
        "memory": run_memory_experiment(),
        "routing": run_routing_experiment(),
        "recovery_matrix": run_recovery_matrix(),
    }
    manifest = RunManifest(
        experiment="component_experiments",
        seed=args.seed,
        scenarios=sorted(experiments),
        notes=["Deterministic; no LLM in the causal path for retrieval or routing."],
    )
    artifacts = RunArtifacts.create(manifest, base=args.output_dir)
    artifacts.write_json("results.json", experiments)

    artifacts.write_csv(
        "retrieval_metrics.csv",
        [{"strategy": k, **v} for k, v in experiments["retrieval"]["summary"].items()],
    )
    artifacts.write_csv("routing_metrics.csv", experiments["routing"]["per_complexity"])
    artifacts.write_csv("recovery_matrix.csv", experiments["recovery_matrix"]["matrix"])

    lines = [
        "# Component Experiments",
        "",
        f"Run id: `{manifest.run_id}`",
        f"Commit: `{manifest.git_commit}`",
        f"Benchmark version: {manifest.benchmark_version}",
        "",
        "## Retrieval (deterministic, no LLM)",
        "",
        "| Strategy | P@1 | P@3 | P@5 | R@5 | MRR |",
        "|---|---|---|---|---|---|",
    ]
    for name, metrics in experiments["retrieval"]["summary"].items():
        lines.append(
            f"| {name} | {metrics['p1']} | {metrics['p3']} | {metrics['p5']} "
            f"| {metrics['r5']} | {metrics['mrr']} |"
        )
    lines += [
        "",
        "## Routing (estimated cost, identical call pattern)",
        "",
        "| Complexity | R0 fixed | R1 adaptive | Ratio |",
        "|---|---|---|---|",
    ]
    for row in experiments["routing"]["per_complexity"]:
        lines.append(
            f"| {row['complexity']} | ${row['R0_estimated_cost_usd']} "
            f"| ${row['R1_estimated_cost_usd']} | {row['cost_ratio']} |"
        )
    lines += [
        "",
        "## Recovery matrix",
        "",
        "| Failure type | Detection | Status | Success rate |",
        "|---|---|---|---|",
    ]
    for row in experiments["recovery_matrix"]["matrix"]:
        lines.append(
            f"| {row['failure_type']} | {row['detection']} | {row['status']} "
            f"| {row['success_rate'] if row['success_rate'] is not None else 'n/a'} |"
        )
    artifacts.write_text("summary.md", "\n".join(lines) + "\n")

    print(f"component experiment artifacts: {artifacts.root}")
    print(
        json.dumps(
            {k: v.get("summary", v.get("detection_accuracy")) for k, v in experiments.items()},
            indent=2,
            default=str,
        )[:600]
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
