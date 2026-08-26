"""SWE-Forge GitHub finalization adapter.

Reuses upstream Open SWE GitHub infrastructure; contributes only the
risk-gated decision about whether a PR may be prepared.
"""

from agent.sweforge.github.finalization import (
    PullRequestDecision,
    PullRequestPlan,
    prepare_pull_request,
)

__all__ = ["PullRequestDecision", "PullRequestPlan", "prepare_pull_request"]
