"""Real-world benchmark harness (schema, loader, dry-run, scorer).

Infrastructure only. No benchmark has been executed and no benchmark
performance is claimed anywhere in this repository.
"""

from evaluation.benchmarks.harness import (
    BenchmarkRunResult,
    BenchmarkTask,
    dry_run,
    load_tasks,
    score_results,
    validate_task,
)

__all__ = [
    "BenchmarkTask",
    "BenchmarkRunResult",
    "dry_run",
    "load_tasks",
    "score_results",
    "validate_task",
]
