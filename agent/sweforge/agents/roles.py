"""Specialised agents: implementer, independent reviewer, diagnostician.

Each agent owns one structured decision and one model tier. They are separate
classes rather than one prompt-switching helper because they have genuinely
different contracts, and because the reviewer must not share context with the
implementer — see :class:`IndependentReviewer`.
"""

from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agent.sweforge.recovery.classifier import Classification
from agent.sweforge.repository.graph_index import RepositoryGraph
from agent.sweforge.routing.model_router import ModelRouter
from agent.sweforge.schemas import (
    FailureDiagnosis,
    FileEdit,
    ImplementationResult,
    ReviewResult,
    Subtask,
    TaskPlan,
    VerificationResult,
)

_MAX_FILE_CHARS = 8000


class ImplementationOutput(BaseModel):
    """What an implementation agent returns."""

    edits: list[FileEdit] = Field(
        default_factory=list,
        description="Full new content for each file to write. Include the entire file.",
    )
    notes: str = Field(default="", description="What was changed and why, briefly.")
    confident: bool = Field(default=True, description="False if the change is a guess.")


class RepairOutput(BaseModel):
    """What a recovery step returns: a diagnosis plus the fix."""

    diagnosis: FailureDiagnosis
    edits: list[FileEdit] = Field(default_factory=list)
    strategy: str = ""


IMPLEMENTER_SYSTEM = """You are an implementation agent in SWE-Forge.

You will be given a subtask, the relevant repository files with their current contents, \
and the overall plan. Return the complete new content for every file you change.

Requirements:
- Return WHOLE files, not diffs or fragments. Content you omit will be lost.
- Preserve existing style, imports and structure. Change only what the subtask requires.
- Do not add dependencies unless the subtask requires it.
- If the subtask needs a test, write a real test that would fail before your change.
- Never write credentials, tokens or keys into files.
"""

REVIEWER_SYSTEM = """You are an INDEPENDENT code reviewer in SWE-Forge.

You did not write this code. Your job is to find what is wrong with it, then decide \
whether it should be approved.

Evaluate: correctness, completeness against the stated task, regressions in callers, \
test coverage of the new behaviour, maintainability, and security.

Rules:
- Judge only the change and the evidence given.
- Passing tests are necessary but not sufficient: a change can pass and still be wrong \
(wrong requirement, missing edge case, silently broken caller).
- Severity: blocker = must not merge; major = must be fixed; minor = should be fixed; \
info = observation.
- Do not approve if you record any major or blocker finding.
- Do not invent findings to appear thorough. If the change is correct, approve it.
"""

DIAGNOSTICIAN_SYSTEM = """You are the failure-analysis and repair agent in SWE-Forge.

Verification has failed. You are given the deterministic failure classification, the \
failing output, and the current contents of the suspect files.

Produce a diagnosis and the minimal fix. Requirements:
- Address the ROOT CAUSE, not the symptom. Do not delete or weaken a test to make it pass.
- Do not loosen an assertion unless the test itself is demonstrably wrong.
- Return whole files for every file you change.
- If the previous attempt's approach was wrong, say so and change approach rather than \
making the same edit again.
"""


@dataclass
class FileContext:
    """File contents supplied to an agent, truncated for prompt budget."""

    files: dict[str, str] = field(default_factory=dict)

    def render(self, *, max_chars: int = _MAX_FILE_CHARS) -> str:
        if not self.files:
            return "(no file contents available)"
        blocks: list[str] = []
        for path, content in sorted(self.files.items()):
            body = content
            if len(body) > max_chars:
                body = body[:max_chars] + f"\n... [truncated, {len(content)} chars total]"
            blocks.append(f"--- {path} ---\n{body}")
        return "\n\n".join(blocks)


def read_files(backend: Any, paths: list[str]) -> FileContext:
    """Read files through an execution backend, skipping unreadable paths."""
    contents: dict[str, str] = {}
    for path in dict.fromkeys(paths):
        try:
            contents[path] = backend.read_file(path)
        except Exception:
            continue
    return FileContext(files=contents)


class ImplementationAgent:
    """Executes one subtask and returns whole-file edits.

    The general-purpose agent, used for ``implementation_agent`` subtasks and as
    the graceful fallback for any planner role without a specialised class.
    Accepts ``tools`` and ``budget`` so it participates in real tool-calling
    (see :mod:`agent.sweforge.agents.tool_loop`) on the same terms as the
    specialised agents.
    """

    role = "implementation_agent"
    tool_names: tuple[str, ...] = ("find_relevant_files", "find_dependencies")

    def __init__(
        self,
        *,
        router: ModelRouter,
        backend: Any,
        node_name: str = "implementation",
        tools: dict[str, Any] | None = None,
        budget: Any | None = None,
        graph: Any | None = None,
    ) -> None:
        self.router = router
        self.backend = backend
        self.node_name = node_name
        self.tools = tools or {}
        self.budget = budget
        self.graph = graph

    def run(
        self, subtask: Subtask, plan: TaskPlan, *, extra_files: list[str] | None = None
    ) -> ImplementationResult:
        paths = list(dict.fromkeys([*subtask.target_files, *(extra_files or [])]))
        context = read_files(self.backend, paths)

        decision = self.router.select("implementation", complexity=plan.complexity)
        prompt = [
            SystemMessage(IMPLEMENTER_SYSTEM),
            HumanMessage(
                f"Overall task complexity: {plan.complexity}\n"
                f"Testing strategy: {plan.testing_strategy}\n\n"
                f"Your subtask ({subtask.id}, agent={subtask.agent}):\n{subtask.description}\n\n"
                f"Target files: {', '.join(subtask.target_files) or 'you must choose'}\n\n"
                f"Current file contents:\n{context.render()}"
            ),
        ]
        try:
            from agent.sweforge.agents.tool_loop import ToolCallingLoop

            loop = ToolCallingLoop(
                router=self.router,
                node_name=self.node_name,
                agent_role=self.role,
                budget=self.budget,
            )
            output = loop.run(
                spec=decision.spec,
                messages=prompt,
                tools=[self.tools[n] for n in self.tool_names if n in self.tools],
                output_model=ImplementationOutput,
            )
        except Exception as exc:
            return ImplementationResult(
                subtask_id=subtask.id,
                agent=subtask.agent,
                notes=f"implementation failed: {type(exc).__name__}: {exc}",
                succeeded=False,
            )

        return ImplementationResult(
            subtask_id=subtask.id,
            agent=subtask.agent,
            edits=output.edits,
            notes=output.notes,
            succeeded=bool(output.edits),
        )


class IndependentReviewer:
    """Reviews a change without the implementer's reasoning in context.

    Independence is structural, not just prompt-level: the reviewer receives
    the original task, the plan, the diff, the verification result and fresh
    repository context — but never the implementation agent's notes or
    self-justification. A reviewer told "I fixed this by X for reason Y" tends
    to grade the reasoning rather than the code.
    """

    def __init__(
        self,
        *,
        router: ModelRouter,
        graph: RepositoryGraph | None = None,
        node_name: str = "independent_review",
    ) -> None:
        self.router = router
        self.graph = graph
        self.node_name = node_name

    def _blast_radius(self, paths: list[str]) -> str:
        if self.graph is None:
            return ""
        lines: list[str] = []
        for path in paths[:6]:
            dependents = self.graph.find_dependents(path)
            tests = self.graph.find_tests_for_file(path)
            if dependents or tests:
                lines.append(
                    f"  {path}: imported by {len(dependents)} file(s)"
                    + (f" (e.g. {', '.join(dependents[:3])})" if dependents else "")
                    + (
                        f"; covering tests: {', '.join(tests[:3])}"
                        if tests
                        else "; NO covering tests found"
                    )
                )
        return "\n".join(lines)

    def review(
        self,
        *,
        task: str,
        plan: TaskPlan,
        diff: str,
        verification: VerificationResult | None,
        changed_files: list[str],
    ) -> ReviewResult:
        decision = self.router.select("review", complexity=plan.complexity)
        model = self.router.build_model(decision.spec)

        verification_block = (
            f"passed={verification.passed}; {verification.summary()}\n"
            f"errors: {'; '.join(verification.errors[:5]) or 'none'}"
            if verification
            else "no verification result available"
        )
        radius = self._blast_radius(changed_files)

        prompt = [
            SystemMessage(REVIEWER_SYSTEM),
            HumanMessage(
                f"Original task:\n{task}\n\n"
                f"Plan complexity: {plan.complexity}; risk level: {plan.risk_level}\n"
                f"Stated testing strategy: {plan.testing_strategy}\n\n"
                f"Files changed: {', '.join(changed_files) or 'none'}\n\n"
                f"Static blast radius:\n{radius or '  (unavailable)'}\n\n"
                f"Verification:\n{verification_block}\n\n"
                f"Change under review:\n{diff}"
            ),
        ]
        try:
            with self.router.track(self.node_name, decision.spec) as usage:
                result = model.with_structured_output(ReviewResult).invoke(prompt)
                last = getattr(model, "last_usage", None)
                if isinstance(last, dict):
                    usage.update(last)
            return result
        except Exception as exc:
            # A reviewer that cannot run must not silently approve.
            return ReviewResult(
                approved=False,
                severity="major",
                summary=f"review could not be completed: {type(exc).__name__}: {exc}",
                recommendations=["Re-run review or escalate to a human reviewer."],
            )


class Diagnostician:
    """Produces a root-cause diagnosis and a repair for a failed verification."""

    def __init__(self, *, router: ModelRouter, backend: Any, node_name: str = "recovery") -> None:
        self.router = router
        self.backend = backend
        self.node_name = node_name

    def diagnose_and_repair(
        self,
        *,
        task: str,
        classification: Classification,
        verification: VerificationResult,
        complexity: str,
        attempt_number: int,
        previous_strategies: list[str],
        candidate_files: list[str],
    ) -> RepairOutput:
        paths = list(dict.fromkeys([*classification.suspect_files, *candidate_files]))[:6]
        context = read_files(self.backend, paths)

        decision = self.router.select("recovery", complexity=complexity)
        model = self.router.build_model(decision.spec)

        history = (
            "Approaches already tried (do not repeat them):\n"
            + "\n".join(f"  attempt {i + 1}: {s}" for i, s in enumerate(previous_strategies))
            if previous_strategies
            else "This is the first repair attempt."
        )

        prompt = [
            SystemMessage(DIAGNOSTICIAN_SYSTEM),
            HumanMessage(
                f"Original task:\n{task}\n\n"
                f"Repair attempt number: {attempt_number}\n{history}\n\n"
                f"Deterministic classification: {classification.category} "
                f"(confidence {classification.confidence})\n"
                f"Matched rules: {', '.join(classification.matched_rules) or 'none'}\n"
                f"Failing tests: {', '.join(classification.failing_tests) or 'unknown'}\n\n"
                f"Verification summary: {verification.summary()}\n"
                f"Error lines:\n" + "\n".join(f"  {e}" for e in verification.errors[:12]) + "\n\n"
                f"Failing output (tail):\n{verification.output[-3000:]}\n\n"
                f"Current contents of suspect files:\n{context.render()}"
            ),
        ]
        try:
            with self.router.track(self.node_name, decision.spec) as usage:
                output = model.with_structured_output(RepairOutput).invoke(prompt)
                last = getattr(model, "last_usage", None)
                if isinstance(last, dict):
                    usage.update(last)
            return output
        except Exception as exc:
            return RepairOutput(
                diagnosis=FailureDiagnosis(
                    category=classification.category,
                    root_cause=f"diagnosis failed: {type(exc).__name__}: {exc}",
                    suspect_files=classification.suspect_files,
                    strategy="none",
                    confidence=0.0,
                ),
                edits=[],
                strategy="none",
            )
