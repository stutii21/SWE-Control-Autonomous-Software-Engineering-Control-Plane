"""Task planner: structured output, grounded in repository facts and memory.

The planner is the one place where a model is asked to make an open-ended
decision that shapes everything downstream, so three constraints apply:

1. **Grounding.** The prompt carries real repository facts (ranked candidate
   files with the reason each was ranked, module layout, discovered tests) and
   retrieved prior experience — not just the task string. A planner that
   invents ``src/utils/helpers.py`` is worse than useless.
2. **Structure.** The output is a validated :class:`TaskPlan`. Its validator
   rejects dependency cycles and unknown subtask references, so an incoherent
   plan fails loudly at the planning node rather than mid-execution.
3. **Repair, not crash.** If the model hallucinates file paths, the planner
   filters them against the real file list rather than failing the run; if
   structured output fails entirely, a deterministic fallback plan is derived
   from repository intelligence alone.
"""

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agent.sweforge.memory.store import ExperienceStore, RetrievedExperience
from agent.sweforge.repository.graph_index import RepositoryGraph
from agent.sweforge.routing.model_router import ModelRouter
from agent.sweforge.schemas import COMPLEXITY_ORDER, AgentRole, Subtask, TaskPlan

PLANNER_SYSTEM_PROMPT = """You are the planning component of SWE-Forge, an autonomous \
software engineering system.

Produce an execution plan for the given task in the given repository.

Rules you must follow:
- Only reference files that appear in the supplied repository evidence. Never invent paths.
- Choose the SMALLEST set of specialised agents the task genuinely needs. Do not add a \
frontend_agent for a pure backend change, and do not add a documentation_agent unless \
documentation is actually part of the task.
- Decompose into subtasks only where decomposition helps. A one-line fix is one subtask.
- Express real ordering constraints with depends_on. Subtasks with no dependency between \
them will be executed concurrently, so do not serialise work that is independent.
- Set complexity honestly: trivial (one-line/config), simple (single file, clear change), \
moderate (a few files or new tests needed), complex (cross-module, ambiguous, or risky).
- Set risk_level HIGH if the task touches auth, secrets, CI configuration, dependencies, \
or database migrations.
- testing_strategy must name how the change will actually be validated.
"""

AVAILABLE_AGENTS: tuple[AgentRole, ...] = (
    "implementation_agent",
    "test_agent",
    "reviewer_agent",
    "security_agent",
    "documentation_agent",
    "frontend_agent",
    "backend_agent",
    "database_agent",
)


@dataclass
class PlanningEvidence:
    """The repository facts handed to the planner."""

    candidate_files: list[dict[str, Any]]
    modules: list[str]
    languages: dict[str, int]
    file_count: int
    test_files: list[str]

    def render(self, *, max_files: int = 12) -> str:
        lines = [
            f"Repository contains {self.file_count} indexed files.",
            f"Languages: {', '.join(f'{k}={v}' for k, v in list(self.languages.items())[:6])}",
            f"Top-level modules implicated: {', '.join(self.modules) or 'n/a'}",
            "",
            "Candidate files ranked by static relevance to the task:",
        ]
        for entry in self.candidate_files[:max_files]:
            lines.append(
                f"  - {entry['path']} (score {entry['score']}) — {'; '.join(entry['reasons'][:2])}"
            )
        if self.test_files:
            lines.append("")
            lines.append(f"Existing tests near those files: {', '.join(self.test_files[:8])}")
        return "\n".join(lines)


class TaskPlanner:
    """Builds a :class:`TaskPlan` from a task, a repository graph and memory."""

    def __init__(
        self,
        *,
        router: ModelRouter,
        graph: RepositoryGraph | None = None,
        memory: ExperienceStore | None = None,
        node_name: str = "planning",
    ) -> None:
        self.router = router
        self.graph = graph
        self.memory = memory
        self.node_name = node_name

    # -- evidence ----------------------------------------------------------
    def gather_evidence(self, task: str, *, limit: int = 12) -> PlanningEvidence:
        if self.graph is None:
            return PlanningEvidence([], [], {}, 0, [])
        hits = self.graph.find_related_files(task, limit=limit)
        tests: list[str] = []
        for hit in hits[:4]:
            tests.extend(self.graph.find_tests_for_file(hit.path))
        return PlanningEvidence(
            candidate_files=[
                {"path": h.path, "score": h.score, "reasons": h.reasons} for h in hits
            ],
            modules=self.graph.find_relevant_modules(task),
            languages=self.graph.map.languages,
            file_count=self.graph.map.file_count,
            test_files=sorted(dict.fromkeys(tests)),
        )

    def retrieve_experience(self, task: str, repository: str) -> list[RetrievedExperience]:
        if self.memory is None:
            return []
        return self.memory.retrieve(task, limit=3, repository=repository)

    # -- estimation --------------------------------------------------------
    def estimate_complexity(self, task: str, evidence: PlanningEvidence) -> str:
        """Cheap deterministic prior, used for model routing before planning.

        This is a *prior*, not the final answer: the planner's own complexity
        field wins. Its purpose is to pick the planner's model tier without
        first making a model call.
        """
        words = len(task.split())
        candidates = len(evidence.candidate_files)
        signals = 0
        lowered = task.lower()
        if words > 45:
            signals += 1
        if candidates > 8:
            signals += 1
        if any(k in lowered for k in ("refactor", "migrate", "redesign", "across", "all ")):
            signals += 2
        if any(k in lowered for k in ("auth", "security", "migration", "schema", "concurren")):
            signals += 1
        if any(k in lowered for k in ("typo", "rename", "bump", "comment", "docstring")):
            signals -= 2
        if signals <= -1:
            return "trivial"
        if signals == 0:
            return "simple"
        if signals <= 2:
            return "moderate"
        return "complex"

    # -- planning ----------------------------------------------------------
    def plan(self, task: str, repository: str) -> tuple[TaskPlan, PlanningEvidence, str]:
        """Return the plan, the evidence used, and the routing reason."""
        evidence = self.gather_evidence(task)
        experience = self.retrieve_experience(task, repository)
        prior = self.estimate_complexity(task, evidence)

        decision = self.router.select("planning", complexity=prior)
        model = self.router.build_model(decision.spec)

        prompt = [
            SystemMessage(PLANNER_SYSTEM_PROMPT),
            HumanMessage(self._render_prompt(task, repository, evidence, experience, prior)),
        ]

        try:
            with self.router.track(self.node_name, decision.spec) as usage:
                plan = model.with_structured_output(TaskPlan).invoke(prompt)
                last = getattr(model, "last_usage", None)
                if isinstance(last, dict):
                    usage.update(last)
        except Exception:
            # Structured planning failed (validation, provider error). Fall back
            # to a deterministic plan derived from repository intelligence so the
            # workflow degrades instead of dying.
            return self._fallback_plan(task, evidence, prior), evidence, decision.spec.reason

        plan = self._sanitise(plan, evidence)
        return plan, evidence, decision.spec.reason

    def _render_prompt(
        self,
        task: str,
        repository: str,
        evidence: PlanningEvidence,
        experience: list[RetrievedExperience],
        prior: str,
    ) -> str:
        blocks = [
            f"Repository: {repository}",
            f"Task:\n{task}",
            "",
            evidence.render(),
            "",
            f"Static complexity prior (heuristic, you may override): {prior}",
            "",
            f"Available agents: {', '.join(AVAILABLE_AGENTS)}",
        ]
        if experience and self.memory is not None:
            blocks += ["", ExperienceStore.render_context(experience)]
        return "\n".join(blocks)

    # -- post-processing ---------------------------------------------------
    def _sanitise(self, plan: TaskPlan, evidence: PlanningEvidence) -> TaskPlan:
        """Drop hallucinated paths; keep the plan otherwise intact.

        Paths are only filtered when a real file list is available. New files a
        task legitimately creates are preserved: a path is dropped only if it
        looks like a reference to an existing file that does not exist.
        """
        if self.graph is None:
            return plan
        known = set(self.graph.map.files)
        if not known:
            return plan

        def keep(path: str) -> bool:
            if path in known:
                return True
            # A path in a directory that exists is plausibly a new file.
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            return bool(parent) and any(k.startswith(f"{parent}/") for k in known)

        relevant = [p for p in plan.relevant_files if keep(p)]
        if not relevant:
            relevant = [entry["path"] for entry in evidence.candidate_files[:5]]

        subtasks = [
            s.model_copy(update={"target_files": [p for p in s.target_files if keep(p)]})
            for s in plan.subtasks
        ]
        return plan.model_copy(update={"relevant_files": relevant, "subtasks": subtasks})

    def _fallback_plan(self, task: str, evidence: PlanningEvidence, prior: str) -> TaskPlan:
        """Deterministic plan from static analysis alone."""
        files = [entry["path"] for entry in evidence.candidate_files[:3]]
        subtasks = [
            Subtask(
                id="st1",
                description=f"Implement: {task[:300]}",
                agent="implementation_agent",
                target_files=files,
            )
        ]
        if evidence.test_files:
            subtasks.append(
                Subtask(
                    id="st2",
                    description="Update or add tests covering the change",
                    agent="test_agent",
                    target_files=evidence.test_files[:2],
                    depends_on=["st1"],
                )
            )
        return TaskPlan(
            complexity=prior,  # type: ignore[arg-type]
            relevant_files=files,
            subtasks=subtasks,
            testing_strategy="Run tests covering the changed files, then lint.",
            risk_level="MEDIUM",
            rationale="Deterministic fallback plan: structured planning was unavailable.",
        )


def select_agents(plan: TaskPlan, *, always_review: bool = True) -> list[AgentRole]:
    """Resolve the final agent roster from the plan.

    The reviewer is added independently of the planner's opinion when review is
    enabled, because a plan that omits its own reviewer is exactly the case the
    review gate exists to catch. A security agent is added for HIGH-risk or
    complex work.
    """
    roster: list[AgentRole] = list(dict.fromkeys(plan.required_agents))
    if always_review and "reviewer_agent" not in roster:
        roster.append("reviewer_agent")
    needs_security = plan.risk_level == "HIGH" or COMPLEXITY_ORDER[plan.complexity] >= 3
    if needs_security and "security_agent" not in roster:
        roster.append("security_agent")
    return roster
