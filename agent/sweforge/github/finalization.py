"""Risk-gated pull-request preparation.

Phase 23 remediation of audit row 17. SWE-Forge does **not** rebuild GitHub
integration: upstream owns App auth, proxy tokens and PR creation
(``agent/tools/``, ``agent/integrations/``). This module contributes the one
decision upstream does not make — whether a change SWE-Forge produced is
*allowed* to become a PR — and defers the mechanics to an injected upstream
creator.

Policy, driven by the risk gate:

=========  ==============================================================
LOW        prepare and open a PR automatically
MEDIUM     prepare a DRAFT PR with reviewer notes; needs approval to promote
HIGH       do not prepare a PR; require human approval first
=========  ==============================================================

Tests never create an external PR: the upstream creator is injected as a mock.
Live GitHub integration is UNAVAILABLE in this environment (no App
installation), and is reported as such rather than simulated.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class PullRequestDecision(StrEnum):
    OPEN = "open"
    DRAFT = "draft"
    BLOCKED = "blocked"


@dataclass
class PullRequestPlan:
    """What would be submitted, and whether submission is permitted."""

    decision: PullRequestDecision
    title: str = ""
    body: str = ""
    draft: bool = False
    changed_files: list[str] = field(default_factory=list)
    reason: str = ""
    created: bool = False
    creation_unavailable_reason: str | None = None
    upstream_result: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": str(self.decision),
            "title": self.title,
            "draft": self.draft,
            "changed_files": self.changed_files,
            "reason": self.reason,
            "created": self.created,
            "creation_unavailable_reason": self.creation_unavailable_reason,
        }


def _render_body(
    *, task: str, verification: Any, risk: Any, review: Any, recovery_attempts: int
) -> str:
    lines = [
        "## Task",
        task.strip() or "(none)",
        "",
        "## Verification",
        (
            f"- passed: **{verification.passed}** ({verification.summary()})"
            if verification is not None
            else "- no verification result available"
        ),
        "",
        "## Independent review",
        (
            f"- approved: **{review.approved}** (severity: {review.severity})"
            if review is not None
            else "- review not run"
        ),
    ]
    if review is not None and review.findings:
        for finding in review.findings[:6]:
            lines.append(f"  - [{finding.severity}] {finding.file or '-'}: {finding.message}")
    lines += [
        "",
        "## Risk assessment",
        (
            f"- score: **{risk.score}/100 ({risk.level})**"
            if risk is not None
            else "- risk not assessed"
        ),
    ]
    if risk is not None:
        for factor in risk.factors[:6]:
            lines.append(f"  - +{factor.weight} {factor.code}: {factor.detail}")
    lines += [
        "",
        f"Recovery attempts required: {recovery_attempts}",
        "",
        "---",
        "_Prepared by SWE-Forge. Risk assessment is pattern-based screening, not a "
        "substitute for human review._",
    ]
    return "\n".join(lines)


def prepare_pull_request(
    *,
    task: str,
    changed_files: list[str],
    risk: Any,
    verification: Any = None,
    review: Any = None,
    recovery_attempts: int = 0,
    upstream_creator: Any = None,
    allow_creation: bool = False,
) -> PullRequestPlan:
    """Decide whether a PR may be prepared, and optionally delegate creation.

    ``upstream_creator`` is the injected upstream PR-creation callable. It is
    only invoked for a permitted decision and only when ``allow_creation`` is
    explicitly true, so no test or dry run can open an external PR by accident.
    """
    level = getattr(risk, "level", None)
    if level == "HIGH":
        return PullRequestPlan(
            decision=PullRequestDecision.BLOCKED,
            changed_files=sorted(changed_files),
            reason=(
                f"risk gate returned HIGH ({getattr(risk, 'score', '?')}/100); "
                "human approval required before a PR may be opened"
            ),
        )
    if verification is not None and not verification.passed:
        return PullRequestPlan(
            decision=PullRequestDecision.BLOCKED,
            changed_files=sorted(changed_files),
            reason="verification is not green; a red change must not become a PR",
        )
    if review is not None and not review.approved:
        return PullRequestPlan(
            decision=PullRequestDecision.BLOCKED,
            changed_files=sorted(changed_files),
            reason="independent reviewer withheld approval",
        )

    draft = level == "MEDIUM"
    decision = PullRequestDecision.DRAFT if draft else PullRequestDecision.OPEN
    summary = task.strip().splitlines()[0] if task.strip() else "SWE-Forge change"
    plan = PullRequestPlan(
        decision=decision,
        title=f"{summary[:70]}",
        body=_render_body(
            task=task,
            verification=verification,
            risk=risk,
            review=review,
            recovery_attempts=recovery_attempts,
        ),
        draft=draft,
        changed_files=sorted(changed_files),
        reason=(
            "MEDIUM risk: draft PR with enhanced review notes"
            if draft
            else "LOW risk: eligible for automatic PR"
        ),
    )

    if not allow_creation:
        plan.creation_unavailable_reason = (
            "creation not requested (allow_creation=False); plan prepared only"
        )
        return plan
    if upstream_creator is None:
        plan.creation_unavailable_reason = (
            "no upstream GitHub creator supplied; live GitHub integration requires an "
            "Open SWE App installation and is UNAVAILABLE in this environment"
        )
        return plan

    try:
        plan.upstream_result = upstream_creator(
            title=plan.title, body=plan.body, draft=plan.draft, files=plan.changed_files
        )
        plan.created = True
    except Exception as exc:
        plan.created = False
        plan.creation_unavailable_reason = f"{type(exc).__name__}: {exc}"
    return plan
