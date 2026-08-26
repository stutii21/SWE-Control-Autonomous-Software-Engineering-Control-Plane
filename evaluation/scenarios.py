"""Deterministic evaluation scenarios.

Each scenario pins the LLM's behaviour so the *orchestration* can be measured.
The code in every ``FileEdit`` below is real: applying it and running pytest in
the fixture repository genuinely passes or genuinely fails. Nothing is
simulated — the harness writes these files to disk and runs the real test
runner, so "recovery succeeded" means a real failing suite really went green.

What this does and does not measure
-----------------------------------
MEASURED: which graph path executed, how many recovery attempts ran, whether
the review gate fired, which terminal state was reached, wall-clock time, tool
calls, model call counts, real pytest results.

NOT MEASURED: whether a frontier model would produce these edits unaided.
Scripted runs say nothing about model capability, and no such claim is made.
Token counts under scripted models are synthetic, so cost columns are labelled
accordingly.
"""

from dataclasses import dataclass, field
from typing import Any

from agent.sweforge.agents.roles import ImplementationOutput, RepairOutput
from agent.sweforge.schemas import (
    FailureDiagnosis,
    FileEdit,
    ReviewFinding,
    ReviewResult,
    Subtask,
    TaskPlan,
)

# A deliberately fake credential, assembled at runtime so no secret-shaped
# literal is ever committed to this repository. Used only to prove the risk
# gate blocks a change that would leak one.
_FAKE_TOKEN = "ghp_" + ("A" * 36)


@dataclass
class Scenario:
    """One benchmark case: a task, a fixture repo, and a pinned model script."""

    id: str
    fixture: str
    task: str
    script: dict[str, list[Any]]
    expected_status: str
    expected_verification: bool
    description: str = ""
    expects_recovery: bool = False
    expects_review_rejection: bool = False
    expects_high_risk: bool = False
    timeout_seconds: int = 120
    tags: list[str] = field(default_factory=list)

    def script_target(self) -> str:
        """Primary file this scenario edits, for tool-call demonstrations."""
        plans = self.script.get("planning") or []
        if plans and getattr(plans[0], "relevant_files", None):
            return plans[0].relevant_files[0]
        return "."


def _plan(
    description: str,
    files: list[str],
    *,
    complexity: str = "simple",
    risk: str = "LOW",
    strategy: str = "Run the tests covering the changed file.",
) -> TaskPlan:
    return TaskPlan(
        complexity=complexity,  # type: ignore[arg-type]
        relevant_files=files,
        subtasks=[
            Subtask(
                id="st1",
                description=description,
                agent="implementation_agent",
                target_files=files,
            )
        ],
        testing_strategy=strategy,
        risk_level=risk,  # type: ignore[arg-type]
        rationale="Scripted plan for reproducible orchestration evaluation.",
    )


# --------------------------------------------------------------------------
# Fixture file contents (real, runnable code)
# --------------------------------------------------------------------------
INVENTORY_WRONG = '''"""Simple inventory ledger."""


def total_value(items):
    """Return the total value of all items."""
    return sum(item["qty"] * item["price"] for item in items)


def restock_needed(items, threshold=5):
    """Return names of items at or below the reorder threshold."""
    return [item["name"] for item in items if item["qty"] < threshold - 1]
'''

INVENTORY_FIXED = '''"""Simple inventory ledger."""


def total_value(items):
    """Return the total value of all items."""
    return sum(item["qty"] * item["price"] for item in items)


def restock_needed(items, threshold=5):
    """Return names of items at or below the reorder threshold.

    The threshold is inclusive: an item sitting exactly at the reorder point
    still needs restocking.
    """
    return [item["name"] for item in items if item["qty"] <= threshold]
'''

BILLING_FIXED = '''"""Invoice totals with tax and discount handling."""


def apply_discount(amount, percent):
    """Reduce amount by percent (0-100)."""
    return amount - (amount * percent / 100)


def invoice_total(subtotal, tax_rate=0.0, discount_percent=0.0):
    """Discount first, then tax.

    Raises:
        ValueError: if subtotal is negative.
    """
    if subtotal < 0:
        raise ValueError("subtotal must not be negative")
    discounted = apply_discount(subtotal, discount_percent)
    return round(discounted * (1 + tax_rate), 2)
'''

BILLING_FULLY_VALIDATED = '''"""Invoice totals with tax and discount handling."""


def apply_discount(amount, percent):
    """Reduce amount by percent (0-100)."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return amount - (amount * percent / 100)


def invoice_total(subtotal, tax_rate=0.0, discount_percent=0.0):
    """Discount first, then tax.

    Raises:
        ValueError: if subtotal is negative, tax_rate is negative, or
            discount_percent falls outside 0-100.
    """
    if subtotal < 0:
        raise ValueError("subtotal must not be negative")
    if tax_rate < 0:
        raise ValueError("tax_rate must not be negative")
    discounted = apply_discount(subtotal, discount_percent)
    return round(discounted * (1 + tax_rate), 2)
'''

TEXTUTIL_SYNTAX_ERROR = '''"""Text normalisation helpers."""

import re


def slugify(text)
    return re.sub(r"\\s+", "-", text.strip().lower())


def truncate(text, limit):
    """Truncate to limit characters, appending an ellipsis when cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."
'''

TEXTUTIL_FIXED = '''"""Text normalisation helpers."""

import re


def slugify(text):
    """Lowercase and hyphenate, collapsing runs of whitespace."""
    return re.sub(r"\\s+", "-", text.strip().lower())


def truncate(text, limit):
    """Truncate to limit characters, appending an ellipsis when cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."
'''

DEPLOY_WITH_SECRET = f'''"""Deployment helper."""

import os

DEPLOY_TOKEN = "{_FAKE_TOKEN}"


def target_environment():
    return os.environ.get("DEPLOY_ENV", "staging")


def is_production():
    return target_environment() == "production"
'''

CI_WORKFLOW_EDITED = """name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python -m pytest -q
      - run: ./scripts/deploy.sh
"""


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------
def all_scenarios() -> list[Scenario]:
    """The benchmark suite. Each case exercises a distinct graph path."""
    return [
        # 1. Clean first-attempt success.
        Scenario(
            id="billing_validation_first_try",
            fixture="billing",
            task=(
                "invoice_total should reject a negative subtotal by raising ValueError "
                "instead of silently returning a negative invoice."
            ),
            description="Correct on the first attempt; no recovery expected.",
            script={
                "planning": [_plan("Add negative-subtotal validation", ["billing.py"])],
                "implementation": [
                    ImplementationOutput(
                        edits=[
                            FileEdit(
                                path="billing.py",
                                content=BILLING_FIXED,
                                summary="raise ValueError on negative subtotal",
                            )
                        ],
                        notes="Added guard clause.",
                    )
                ],
                "review": [
                    ReviewResult(approved=True, summary="Correct, minimal, covered by the test.")
                ],
            },
            expected_status="completed",
            expected_verification=True,
            tags=["happy_path"],
        ),
        # 2. Wrong first attempt, recovered from a test assertion failure.
        Scenario(
            id="inventory_boundary_recovery",
            fixture="inventory",
            task=(
                "restock_needed must treat the threshold as inclusive: an item whose "
                "quantity equals the threshold still needs restocking."
            ),
            description="First attempt off-by-one; recovery fixes the boundary.",
            script={
                "planning": [_plan("Make the reorder threshold inclusive", ["inventory.py"])],
                "implementation": [
                    ImplementationOutput(
                        edits=[
                            FileEdit(
                                path="inventory.py",
                                content=INVENTORY_WRONG,
                                summary="adjust threshold comparison",
                            )
                        ],
                        notes="Changed the comparison.",
                    )
                ],
                "recovery": [
                    RepairOutput(
                        diagnosis=FailureDiagnosis(
                            category="test_assertion",
                            root_cause=(
                                "threshold - 1 excludes the boundary value; the test expects "
                                "qty == threshold to be included"
                            ),
                            suspect_files=["inventory.py"],
                            strategy="use an inclusive <= comparison",
                            confidence=0.9,
                        ),
                        edits=[
                            FileEdit(
                                path="inventory.py",
                                content=INVENTORY_FIXED,
                                summary="inclusive comparison",
                            )
                        ],
                        strategy="use an inclusive <= comparison",
                    )
                ],
                "review": [ReviewResult(approved=True, summary="Boundary now correct.")],
            },
            expected_status="completed",
            expected_verification=True,
            expects_recovery=True,
            tags=["recovery", "test_assertion"],
        ),
        # 3. Syntax error introduced then repaired.
        Scenario(
            id="textutil_syntax_recovery",
            fixture="textutil",
            task="slugify should collapse runs of whitespace into a single hyphen.",
            description="First attempt has a syntax error; recovery repairs it.",
            script={
                "planning": [_plan("Collapse whitespace runs in slugify", ["textutil.py"])],
                "implementation": [
                    ImplementationOutput(
                        edits=[
                            FileEdit(
                                path="textutil.py",
                                content=TEXTUTIL_SYNTAX_ERROR,
                                summary="use regex to collapse whitespace",
                            )
                        ],
                        notes="Switched to a regex.",
                    )
                ],
                "recovery": [
                    RepairOutput(
                        diagnosis=FailureDiagnosis(
                            category="syntax",
                            root_cause="missing colon after the slugify function signature",
                            suspect_files=["textutil.py"],
                            strategy="restore the function signature colon",
                            confidence=0.95,
                        ),
                        edits=[
                            FileEdit(
                                path="textutil.py", content=TEXTUTIL_FIXED, summary="fix signature"
                            )
                        ],
                        strategy="restore the function signature colon",
                    )
                ],
                "review": [ReviewResult(approved=True, summary="Regex collapse is correct.")],
            },
            expected_status="completed",
            expected_verification=True,
            expects_recovery=True,
            tags=["recovery", "syntax"],
        ),
        # 4. Recovery budget exhausted -> escalation (bounded-loop proof).
        Scenario(
            id="inventory_recovery_exhausted",
            fixture="inventory",
            task=(
                "restock_needed must treat the threshold as inclusive, but every repair "
                "attempt in this scenario is deliberately wrong."
            ),
            description=(
                "Proves the bounded recovery loop terminates: all attempts fail, so the "
                "graph escalates instead of looping forever."
            ),
            script={
                "planning": [_plan("Make the reorder threshold inclusive", ["inventory.py"])],
                "implementation": [
                    ImplementationOutput(
                        edits=[
                            FileEdit(
                                path="inventory.py",
                                content=INVENTORY_WRONG,
                                summary="adjust threshold comparison",
                            )
                        ],
                        notes="Changed the comparison.",
                    )
                ],
                # repeat_last means every subsequent attempt reapplies this same
                # wrong edit, which is exactly the pathological case the bound exists for.
                "recovery": [
                    RepairOutput(
                        diagnosis=FailureDiagnosis(
                            category="test_assertion",
                            root_cause="incorrect diagnosis (scripted failure case)",
                            suspect_files=["inventory.py"],
                            strategy="reapply the same wrong comparison",
                            confidence=0.4,
                        ),
                        edits=[
                            FileEdit(
                                path="inventory.py",
                                content=INVENTORY_WRONG,
                                summary="no effective change",
                            )
                        ],
                        strategy="reapply the same wrong comparison",
                    )
                ],
                "review": [ReviewResult(approved=False, summary="Not reached.")],
            },
            expected_status="escalated_recovery_exhausted",
            expected_verification=False,
            expects_recovery=True,
            tags=["bounded_loop", "escalation"],
        ),
        # 5. Tests pass but the independent reviewer rejects; recovery then approval.
        Scenario(
            id="billing_review_rejection",
            fixture="billing",
            task=(
                "Validate invoice_total inputs: reject a negative subtotal, a negative "
                "tax_rate, and a discount percentage outside 0-100."
            ),
            description=(
                "Green tests are not sufficient: the reviewer catches incomplete "
                "validation, drives a recovery cycle, then approves."
            ),
            script={
                "planning": [
                    _plan(
                        "Validate all invoice_total inputs",
                        ["billing.py"],
                        complexity="moderate",
                    )
                ],
                "implementation": [
                    ImplementationOutput(
                        edits=[
                            FileEdit(
                                path="billing.py",
                                content=BILLING_FIXED,
                                summary="validate subtotal only",
                            )
                        ],
                        notes="Added subtotal validation.",
                    )
                ],
                "review": [
                    ReviewResult(
                        approved=False,
                        findings=[
                            ReviewFinding(
                                severity="major",
                                category="completeness",
                                file="billing.py",
                                message=(
                                    "Only subtotal is validated. The task also requires "
                                    "rejecting a negative tax_rate and an out-of-range discount."
                                ),
                                recommendation="Add the two remaining guard clauses.",
                            )
                        ],
                        summary="Tests pass but the change is incomplete against the task.",
                    ),
                    ReviewResult(approved=True, summary="All three inputs now validated."),
                ],
                "recovery": [
                    RepairOutput(
                        diagnosis=FailureDiagnosis(
                            category="test_assertion",
                            root_cause="reviewer found incomplete input validation",
                            suspect_files=["billing.py"],
                            strategy="add tax_rate and discount_percent validation",
                            confidence=0.85,
                        ),
                        edits=[
                            FileEdit(
                                path="billing.py",
                                content=BILLING_FULLY_VALIDATED,
                                summary="validate all inputs",
                            )
                        ],
                        strategy="add tax_rate and discount_percent validation",
                    )
                ],
            },
            expected_status="completed",
            expected_verification=True,
            expects_recovery=True,
            expects_review_rejection=True,
            tags=["review_gate", "recovery"],
        ),
        # 6. Risk gate blocks an otherwise-green change.
        Scenario(
            id="pipeline_secret_risk_gate",
            fixture="pipeline",
            task=(
                "Add a deploy token to the deployment helper and make CI also run on pull requests."
            ),
            description=(
                "Tests stay green, but the change commits a credential and edits a CI "
                "workflow. The risk gate must withhold the automatic PR and require a human."
            ),
            script={
                "planning": [
                    _plan(
                        "Add deploy token and update CI triggers",
                        ["deploy.py", ".github/workflows/ci.yml"],
                        complexity="moderate",
                        risk="HIGH",
                    )
                ],
                "implementation": [
                    ImplementationOutput(
                        edits=[
                            FileEdit(
                                path="deploy.py",
                                content=DEPLOY_WITH_SECRET,
                                summary="add deploy token",
                            ),
                            FileEdit(
                                path=".github/workflows/ci.yml",
                                content=CI_WORKFLOW_EDITED,
                                summary="run on pull requests",
                            ),
                        ],
                        notes="Added the token constant and extended CI triggers.",
                    )
                ],
                "review": [
                    ReviewResult(approved=True, summary="Functionally does what was asked.")
                ],
            },
            expected_status="awaiting_human_approval",
            expected_verification=True,
            expects_high_risk=True,
            tags=["security", "risk_gate"],
        ),
    ]


def scenario_by_id(scenario_id: str) -> Scenario:
    for scenario in all_scenarios():
        if scenario.id == scenario_id:
            return scenario
    raise KeyError(f"unknown scenario: {scenario_id}")
