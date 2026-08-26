"""Real system baselines for Experiment B (system-level comparison).

Distinct from the architectural ablation in ``evaluation/runner.py``: those
variants are all SWE-Forge. This package invokes the genuine upstream Open SWE
execution path, or reports precisely why it cannot.
"""

from evaluation.baselines.open_swe_baseline import (
    BaselineRunResult,
    OpenSWEBaseline,
    PreflightResult,
    describe_baseline_availability,
    preflight,
)

__all__ = [
    "BaselineRunResult",
    "OpenSWEBaseline",
    "PreflightResult",
    "describe_baseline_availability",
    "preflight",
]
