"""SWE-Forge structured-output schemas.

Every consequential LLM decision in SWE-Forge is expressed as one of these
Pydantic models and obtained through LangChain's ``with_structured_output``.
Free-form text is never parsed to make a control-flow decision.

The validators here are deliberately strict: they turn "the model said
something incoherent" into a loud, catchable error instead of a silently
wrong graph transition. Several invariants (for example "a review with a
blocker cannot be approved", or "a subtask cannot depend on itself") are
enforced here rather than trusted to the model.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Complexity = Literal["trivial", "simple", "moderate", "complex"]
RiskLevel = Literal["LOW", "MEDIUM", "HIGH"]
Severity = Literal["info", "minor", "major", "blocker"]

AgentRole = Literal[
    "implementation_agent",
    "test_agent",
    "reviewer_agent",
    "security_agent",
    "documentation_agent",
    "frontend_agent",
    "backend_agent",
    "database_agent",
]

FailureCategory = Literal[
    "syntax",
    "type",
    "dependency",
    "test_assertion",
    "runtime",
    "configuration",
    "environment",
    "lint",
    "timeout",
    "unknown",
]

COMPLEXITY_ORDER: dict[str, int] = {
    "trivial": 0,
    "simple": 1,
    "moderate": 2,
    "complex": 3,
}

SEVERITY_ORDER: dict[str, int] = {"info": 0, "minor": 1, "major": 2, "blocker": 3}


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
class Subtask(BaseModel):
    """One unit of work assigned to exactly one specialised agent."""

    id: str = Field(description="Short stable identifier, e.g. 'st1'.")
    description: str = Field(description="What must be done, imperative mood.")
    agent: AgentRole = Field(description="Which specialised agent should execute this.")
    target_files: list[str] = Field(
        default_factory=list, description="Repository-relative paths this subtask touches."
    )
    depends_on: list[str] = Field(
        default_factory=list, description="Ids of subtasks that must finish first."
    )

    @field_validator("id")
    @classmethod
    def _id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("subtask id must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def _no_self_dependency(self) -> "Subtask":
        if self.id in self.depends_on:
            raise ValueError(f"subtask {self.id!r} depends on itself")
        return self


class TaskPlan(BaseModel):
    """The planner's structured output.

    ``execution_layers`` turns the declared dependency edges into concrete
    concurrency: every subtask in a layer is independent of its siblings, so
    the graph may execute a layer in parallel and still respect ordering.
    """

    complexity: Complexity
    relevant_files: list[str] = Field(default_factory=list)
    subtasks: list[Subtask] = Field(min_length=1)
    required_agents: list[AgentRole] = Field(default_factory=list)
    testing_strategy: str = ""
    risk_level: RiskLevel = "LOW"
    rationale: str = ""

    @model_validator(mode="after")
    def _validate_dag(self) -> "TaskPlan":
        ids = [s.id for s in self.subtasks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate subtask ids in plan")
        known = set(ids)
        for subtask in self.subtasks:
            unknown = [d for d in subtask.depends_on if d not in known]
            if unknown:
                raise ValueError(f"subtask {subtask.id!r} depends on unknown ids {unknown}")
        # Cycle detection via Kahn's algorithm.
        indegree = {s.id: len(set(s.depends_on)) for s in self.subtasks}
        dependents: dict[str, list[str]] = {s.id: [] for s in self.subtasks}
        for subtask in self.subtasks:
            for dep in set(subtask.depends_on):
                dependents[dep].append(subtask.id)
        ready = [i for i, d in indegree.items() if d == 0]
        seen = 0
        while ready:
            current = ready.pop()
            seen += 1
            for nxt in dependents[current]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
        if seen != len(self.subtasks):
            raise ValueError("subtask dependency graph contains a cycle")
        # Keep required_agents consistent with what the subtasks actually need.
        needed = {s.agent for s in self.subtasks}
        merged = sorted(needed.union(self.required_agents))
        object.__setattr__(self, "required_agents", merged)  # type: ignore[arg-type]
        return self

    def execution_layers(self) -> list[list[Subtask]]:
        """Topologically sorted layers of independent subtasks."""
        by_id = {s.id: s for s in self.subtasks}
        remaining = dict(by_id)
        done: set[str] = set()
        layers: list[list[Subtask]] = []
        while remaining:
            layer = [s for s in remaining.values() if all(dep in done for dep in s.depends_on)]
            if not layer:  # pragma: no cover - guarded by _validate_dag
                raise ValueError("cycle detected while layering subtasks")
            layer.sort(key=lambda s: s.id)
            layers.append(layer)
            for s in layer:
                done.add(s.id)
                remaining.pop(s.id)
        return layers


# --------------------------------------------------------------------------
# Implementation / verification
# --------------------------------------------------------------------------
class FileEdit(BaseModel):
    """A whole-file replacement produced by an implementation or repair step.

    Whole-file writes are used instead of unified diffs on purpose: a diff
    produced by a model frequently fails to apply against a moved line, and a
    failed patch application is a much worse failure mode than a slightly
    larger payload.
    """

    path: str
    content: str
    summary: str = ""

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("edit path must not be blank")
        if cleaned.startswith("/") or ".." in cleaned.split("/"):
            raise ValueError(f"edit path must be repository-relative: {value!r}")
        return cleaned


class ImplementationResult(BaseModel):
    subtask_id: str
    agent: AgentRole
    edits: list[FileEdit] = Field(default_factory=list)
    notes: str = ""
    succeeded: bool = True

    @property
    def touched_files(self) -> list[str]:
        return [e.path for e in self.edits]


class VerificationResult(BaseModel):
    passed: bool
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    lint_passed: bool | None = None
    typecheck_passed: bool | None = None
    errors: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    output: str = ""
    duration_seconds: float = 0.0

    @model_validator(mode="after")
    def _consistent_counts(self) -> "VerificationResult":
        if self.tests_passed + self.tests_failed > self.tests_run:
            object.__setattr__(self, "tests_run", self.tests_passed + self.tests_failed)
        return self

    def summary(self) -> str:
        bits = [f"tests {self.tests_passed}/{self.tests_run}"]
        if self.lint_passed is not None:
            bits.append(f"lint {'ok' if self.lint_passed else 'fail'}")
        if self.typecheck_passed is not None:
            bits.append(f"types {'ok' if self.typecheck_passed else 'fail'}")
        return ", ".join(bits)


# --------------------------------------------------------------------------
# Failure analysis / recovery
# --------------------------------------------------------------------------
class FailureDiagnosis(BaseModel):
    category: FailureCategory
    root_cause: str
    suspect_files: list[str] = Field(default_factory=list)
    strategy: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class RecoveryAttempt(BaseModel):
    attempt_number: int = Field(ge=1)
    failure_category: FailureCategory
    diagnosis: str
    strategy: str = ""
    edits: list[FileEdit] = Field(default_factory=list)
    verification: VerificationResult | None = None

    @property
    def succeeded(self) -> bool:
        return bool(self.verification and self.verification.passed)


# --------------------------------------------------------------------------
# Review
# --------------------------------------------------------------------------
class ReviewFinding(BaseModel):
    severity: Severity
    category: str = "correctness"
    file: str | None = None
    message: str
    recommendation: str = ""


class ReviewResult(BaseModel):
    """Independent reviewer verdict.

    The ``approved`` flag is reconciled against the findings: a model that
    reports a blocker and still approves is corrected here, because the risk
    gate downstream trusts this boolean.
    """

    approved: bool
    severity: Severity = "info"
    findings: list[ReviewFinding] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    summary: str = ""

    @model_validator(mode="after")
    def _reconcile(self) -> "ReviewResult":
        if self.findings:
            worst = max(SEVERITY_ORDER[f.severity] for f in self.findings)
            worst_name = next(k for k, v in SEVERITY_ORDER.items() if v == worst)
            object.__setattr__(self, "severity", worst_name)
            if worst >= SEVERITY_ORDER["major"]:
                object.__setattr__(self, "approved", False)
        return self

    @property
    def blocking_findings(self) -> list[ReviewFinding]:
        return [f for f in self.findings if SEVERITY_ORDER[f.severity] >= SEVERITY_ORDER["major"]]


# --------------------------------------------------------------------------
# Security / risk
# --------------------------------------------------------------------------
class RiskFactor(BaseModel):
    code: str
    weight: int
    detail: str


class SecurityFinding(BaseModel):
    rule: str
    severity: Severity
    file: str | None = None
    line: int | None = None
    message: str


class RiskScore(BaseModel):
    score: int = Field(ge=0, le=100)
    level: RiskLevel
    factors: list[RiskFactor] = Field(default_factory=list)
    recommendation: str = ""

    @property
    def requires_human_approval(self) -> bool:
        return self.level == "HIGH"


# --------------------------------------------------------------------------
# Routing / metrics / memory
# --------------------------------------------------------------------------
class ModelCallRecord(BaseModel):
    node: str
    role: str
    tier: str
    model: str
    latency_seconds: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float = 0.0
    ok: bool = True
    error: str | None = None


class ExecutionMetrics(BaseModel):
    node_transitions: list[str] = Field(default_factory=list)
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    wall_time_seconds: float = 0.0
    recovery_attempts: int = 0
    review_rejections: int = 0
    security_gate_triggered: bool = False
    verification_runs: int = 0


class ExperienceRecord(BaseModel):
    """One completed task, stored for retrieval-based planning context.

    This is retrieval, not learning: nothing about the model weights changes.
    """

    task: str
    repository: str
    task_type: str = "general"
    complexity: Complexity = "moderate"
    languages: list[str] = Field(default_factory=list)
    strategy: str = ""
    relevant_files: list[str] = Field(default_factory=list)
    models_used: list[str] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    failure_categories: list[FailureCategory] = Field(default_factory=list)
    recovery_strategies: list[str] = Field(default_factory=list)
    final_status: str = "unknown"
    recovery_attempts: int = 0
    wall_time_seconds: float = 0.0
    lesson: str = ""
