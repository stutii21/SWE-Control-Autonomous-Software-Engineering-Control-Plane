"""Specialized SWE-Forge agents.

Phase 23 remediation of audit row 3. Previously eight ``AgentRole`` values all
executed one ``ImplementationAgent`` with one prompt, so "multi-agent" was a
Pydantic enum rather than behaviour.

Each agent here differs along four axes that actually change execution:

============  =====================  ==================  ==============  ===================
agent         structured output      model role/tier     tools granted   distinct contract
============  =====================  ==================  ==============  ===================
TestAgent     ``TestChanges``        test_authoring      test discovery  writes failing-first tests
BackendAgent  ``BackendChanges``     implementation      deps + callers  reasons about blast radius
FrontendAgent ``FrontendChanges``    implementation      relevant files  component/state focus
DatabaseAgent ``MigrationChanges``   implementation      deps + callers  migration reversibility
DocumentationAgent ``DocChanges``    documentation       relevant files  behaviour-to-docs consistency
SecurityAgent ``SecurityAssessment`` security_analysis   security scan   findings, not edits
============  =====================  ==================  ==============  ===================

``SecurityAgent`` deliberately returns *findings rather than edits*: an agent
that both flags and silently fixes a security issue removes the human's chance
to see it.

Agents that are granted tools use real LangChain tool-calling
(``bind_tools`` → ``tool_calls`` → ``ToolMessage``) via
:class:`~agent.sweforge.agents.tool_loop.ToolCallingLoop`, not manual
``StructuredTool.invoke`` from a graph node.
"""

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agent.sweforge.agents.roles import FileContext, read_files
from agent.sweforge.agents.tool_loop import ToolCallingLoop
from agent.sweforge.repository.graph_index import RepositoryGraph
from agent.sweforge.routing.model_router import ModelRouter
from agent.sweforge.schemas import (
    AgentRole,
    FileEdit,
    ImplementationResult,
    SecurityFinding,
    Subtask,
    TaskPlan,
)


# --------------------------------------------------------------------------
# Per-agent structured outputs. Distinct shapes, not one reused model.
# --------------------------------------------------------------------------
class TestChanges(BaseModel):
    """A test agent's output: tests, plus what they are meant to catch."""

    # Domain name collides with pytest's collection heuristic; this is a
    # Pydantic model, not a test class.
    __test__ = False

    edits: list[FileEdit] = Field(default_factory=list)
    tests_added: list[str] = Field(
        default_factory=list, description="Names of test functions added."
    )
    behaviour_covered: str = Field(
        default="", description="The behaviour these tests would catch if it regressed."
    )
    fails_before_fix: bool = Field(
        default=True,
        description="True if these tests fail against the unfixed code (they should).",
    )


class BackendChanges(BaseModel):
    """A backend agent's output, including who else is affected."""

    edits: list[FileEdit] = Field(default_factory=list)
    affected_callers: list[str] = Field(
        default_factory=list, description="In-repo files that call the changed code."
    )
    contract_changed: bool = Field(
        default=False, description="True if a public signature or return shape changed."
    )
    notes: str = ""


class FrontendChanges(BaseModel):
    edits: list[FileEdit] = Field(default_factory=list)
    components_touched: list[str] = Field(default_factory=list)
    accessibility_considered: bool = False
    notes: str = ""


class MigrationChanges(BaseModel):
    """A database agent's output. Reversibility is mandatory, not optional."""

    edits: list[FileEdit] = Field(default_factory=list)
    is_reversible: bool = Field(
        default=False, description="True if this migration can be rolled back."
    )
    destructive_operations: list[str] = Field(
        default_factory=list, description="DROP/DELETE/ALTER operations that lose data."
    )
    requires_backfill: bool = False
    notes: str = ""


class DocChanges(BaseModel):
    edits: list[FileEdit] = Field(default_factory=list)
    documents_updated: list[str] = Field(default_factory=list)
    behaviour_documented: str = ""


class SecurityAssessment(BaseModel):
    """A security agent's output: findings, never silent fixes."""

    findings: list[SecurityFinding] = Field(default_factory=list)
    sensitive_paths_touched: list[str] = Field(default_factory=list)
    requires_human_review: bool = False
    summary: str = ""


# --------------------------------------------------------------------------
# System contracts. Genuinely different instructions per role.
# --------------------------------------------------------------------------
TEST_AGENT_SYSTEM = """You are a TEST agent in SWE-Forge. You write tests, not features.

Use your tools to discover existing tests and the files they cover before writing \
anything, so your tests match the project's existing conventions.

Requirements:
- A regression test must FAIL against the unfixed behaviour. A test that passes either \
way is worthless; set fails_before_fix honestly.
- Follow the existing test file's imports, fixtures and naming style.
- Test the behaviour described in the task, plus the boundary either side of it.
- Do not modify production code. That is another agent's subtask.
- Do not weaken or delete an existing test to make a suite green."""

BACKEND_AGENT_SYSTEM = """You are a BACKEND agent in SWE-Forge, responsible for \
service-side logic, APIs and data flow.

Before editing, use your tools to find the callers and dependencies of the code you are \
about to change. A backend change that breaks three call sites is not a fix.

Requirements:
- Report every in-repo caller you found in affected_callers.
- Set contract_changed=true if you altered a public signature, return shape or raised \
exception. That flag drives downstream review.
- Preserve existing error-handling behaviour unless the task requires changing it.
- Return whole files."""

FRONTEND_AGENT_SYSTEM = """You are a FRONTEND agent in SWE-Forge, responsible for UI \
components, state and rendering.

Requirements:
- Follow the existing component structure and styling approach; do not introduce a new \
framework or state library.
- List the components you touched.
- Preserve existing prop contracts unless the task requires changing them.
- Return whole files."""

DATABASE_AGENT_SYSTEM = """You are a DATABASE agent in SWE-Forge, responsible for schema \
and migrations.

Migration safety is your primary concern, ahead of elegance.

Requirements:
- State whether the migration is reversible. If it is not, say so plainly — do not claim \
reversibility you cannot deliver.
- List every destructive operation (DROP, DELETE, data-losing ALTER) in \
destructive_operations. Under-reporting here defeats the risk gate.
- Set requires_backfill=true if existing rows need populating.
- Never combine a destructive change with an unrelated one in a single migration."""

DOCUMENTATION_AGENT_SYSTEM = """You are a DOCUMENTATION agent in SWE-Forge.

Requirements:
- Document what the code now does, not what the task hoped it would do.
- Update only documents whose content is actually made wrong by the change.
- Match the surrounding document's tone and structure.
- Do not add marketing language or invent capabilities.
- Do not edit production code."""

SECURITY_AGENT_SYSTEM = """You are a SECURITY agent in SWE-Forge.

You assess. You do NOT fix. Returning findings rather than edits is deliberate: silently \
patching a security issue removes the human's opportunity to see it.

Use your tools to scan the changed content.

Requirements:
- Report concrete findings tied to a file and, where possible, a line.
- Severity: blocker = must not merge; major = must be fixed; minor = should be fixed; \
info = observation.
- Set requires_human_review=true for credentials, authentication/authorization changes, \
CI/CD edits, or destructive operations.
- Do not invent findings to appear thorough. If the change is clean, say so."""


# --------------------------------------------------------------------------
# Base class
# --------------------------------------------------------------------------
class SpecializedAgent:
    """Common execution shape; subclasses supply the contract that differs."""

    role: AgentRole = "implementation_agent"
    model_role: str = "implementation"
    output_model: type[BaseModel] = TestChanges
    system_prompt: str = ""
    tool_names: tuple[str, ...] = ()
    node_name: str = "implementation"

    def __init__(
        self,
        *,
        router: ModelRouter,
        backend: Any,
        graph: RepositoryGraph | None = None,
        tools: dict[str, Any] | None = None,
        budget: Any | None = None,
    ) -> None:
        self.router = router
        self.backend = backend
        self.graph = graph
        self.tools = tools or {}
        self.budget = budget

    # -- context ----------------------------------------------------------
    def gather_context(self, subtask: Subtask, plan: TaskPlan) -> FileContext:
        paths = list(dict.fromkeys([*subtask.target_files, *plan.relevant_files]))[:8]
        return read_files(self.backend, paths)

    def extra_prompt_context(self, subtask: Subtask, plan: TaskPlan) -> str:
        """Role-specific extra evidence. Overridden by subclasses."""
        return ""

    def granted_tools(self) -> list[Any]:
        return [self.tools[name] for name in self.tool_names if name in self.tools]

    # -- execution --------------------------------------------------------
    def run(self, subtask: Subtask, plan: TaskPlan) -> ImplementationResult:
        context = self.gather_context(subtask, plan)
        decision = self.router.select(self.model_role, complexity=plan.complexity)

        prompt = [
            SystemMessage(self.system_prompt),
            HumanMessage(
                f"Task complexity: {plan.complexity} | risk: {plan.risk_level}\n"
                f"Testing strategy: {plan.testing_strategy}\n\n"
                f"Your subtask ({subtask.id}):\n{subtask.description}\n\n"
                f"Target files: {', '.join(subtask.target_files) or 'you must choose'}\n"
                f"{self.extra_prompt_context(subtask, plan)}\n\n"
                f"Current file contents:\n{context.render()}"
            ),
        ]

        loop = ToolCallingLoop(
            router=self.router,
            node_name=self.node_name,
            agent_role=self.role,
            budget=self.budget,
        )
        try:
            output = loop.run(
                spec=decision.spec,
                messages=prompt,
                tools=self.granted_tools(),
                output_model=self.output_model,
            )
        except Exception as exc:
            return ImplementationResult(
                subtask_id=subtask.id,
                agent=self.role,
                notes=f"{type(self).__name__} failed: {type(exc).__name__}: {exc}",
                succeeded=False,
            )
        return self.to_result(subtask, output)

    def to_result(self, subtask: Subtask, output: Any) -> ImplementationResult:
        edits = list(getattr(output, "edits", []) or [])
        return ImplementationResult(
            subtask_id=subtask.id,
            agent=self.role,
            edits=edits,
            notes=self.summarize(output),
            succeeded=bool(edits),
        )

    def summarize(self, output: Any) -> str:
        return getattr(output, "notes", "") or ""


# --------------------------------------------------------------------------
# Concrete agents
# --------------------------------------------------------------------------
class TestAgent(SpecializedAgent):
    __test__ = False  # not a pytest class despite the domain name

    role: AgentRole = "test_agent"
    model_role = "test_authoring"
    output_model = TestChanges
    system_prompt = TEST_AGENT_SYSTEM
    tool_names = ("find_related_tests", "find_relevant_files")
    node_name = "test_authoring"

    def extra_prompt_context(self, subtask: Subtask, plan: TaskPlan) -> str:
        if self.graph is None:
            return ""
        existing: list[str] = []
        for path in [*subtask.target_files, *plan.relevant_files][:4]:
            existing.extend(self.graph.find_tests_for_file(path))
        unique = sorted(dict.fromkeys(existing))
        if not unique:
            return "\n\nNo existing tests cover these files — you are creating first coverage."
        return f"\n\nExisting tests covering these files: {', '.join(unique[:6])}"

    def summarize(self, output: Any) -> str:
        return (
            f"tests added: {', '.join(output.tests_added) or 'none'}; "
            f"covers: {output.behaviour_covered[:160]}; "
            f"fails_before_fix={output.fails_before_fix}"
        )


class BackendAgent(SpecializedAgent):
    role: AgentRole = "backend_agent"
    model_role = "implementation"
    output_model = BackendChanges
    system_prompt = BACKEND_AGENT_SYSTEM
    tool_names = ("find_dependencies", "find_callers", "find_relevant_files")
    node_name = "backend_implementation"

    def extra_prompt_context(self, subtask: Subtask, plan: TaskPlan) -> str:
        if self.graph is None:
            return ""
        lines: list[str] = []
        for path in subtask.target_files[:4]:
            dependents = self.graph.find_dependents(path)
            if dependents:
                lines.append(f"  {path} is imported by: {', '.join(dependents[:5])}")
        if not lines:
            return ""
        return "\n\nStatic blast radius:\n" + "\n".join(lines)

    def summarize(self, output: Any) -> str:
        return (
            f"contract_changed={output.contract_changed}; "
            f"affected callers: {', '.join(output.affected_callers[:5]) or 'none'}; "
            f"{output.notes[:160]}"
        )


class FrontendAgent(SpecializedAgent):
    role: AgentRole = "frontend_agent"
    model_role = "implementation"
    output_model = FrontendChanges
    system_prompt = FRONTEND_AGENT_SYSTEM
    tool_names = ("find_relevant_files",)
    node_name = "frontend_implementation"

    def summarize(self, output: Any) -> str:
        return (
            f"components: {', '.join(output.components_touched[:5]) or 'none'}; "
            f"a11y_considered={output.accessibility_considered}; {output.notes[:160]}"
        )


class DatabaseAgent(SpecializedAgent):
    role: AgentRole = "database_agent"
    model_role = "implementation"
    output_model = MigrationChanges
    system_prompt = DATABASE_AGENT_SYSTEM
    tool_names = ("find_dependencies", "find_callers")
    node_name = "database_implementation"

    def summarize(self, output: Any) -> str:
        return (
            f"reversible={output.is_reversible}; "
            f"destructive={', '.join(output.destructive_operations[:4]) or 'none'}; "
            f"backfill={output.requires_backfill}; {output.notes[:120]}"
        )


class DocumentationAgent(SpecializedAgent):
    role: AgentRole = "documentation_agent"
    model_role = "documentation"
    output_model = DocChanges
    system_prompt = DOCUMENTATION_AGENT_SYSTEM
    tool_names = ("find_relevant_files",)
    node_name = "documentation"

    def summarize(self, output: Any) -> str:
        return (
            f"docs updated: {', '.join(output.documents_updated[:5]) or 'none'}; "
            f"{output.behaviour_documented[:160]}"
        )


class SecurityAgent(SpecializedAgent):
    """Assesses rather than edits, so its result carries no file edits."""

    role: AgentRole = "security_agent"
    model_role = "security_analysis"
    output_model = SecurityAssessment
    system_prompt = SECURITY_AGENT_SYSTEM
    tool_names = ("security_scan", "calculate_change_risk")
    node_name = "security_agent"

    def to_result(self, subtask: Subtask, output: Any) -> ImplementationResult:
        # No edits by design: findings surface to the reviewer and risk gate.
        return ImplementationResult(
            subtask_id=subtask.id,
            agent=self.role,
            edits=[],
            notes=self.summarize(output),
            succeeded=True,
        )

    def summarize(self, output: Any) -> str:
        return (
            f"{len(output.findings)} finding(s); "
            f"requires_human_review={output.requires_human_review}; "
            f"{output.summary[:160]}"
        )


#: Registry consulted by the graph's dispatch node.
AGENT_CLASSES: dict[str, type[SpecializedAgent]] = {
    "test_agent": TestAgent,
    "backend_agent": BackendAgent,
    "frontend_agent": FrontendAgent,
    "database_agent": DatabaseAgent,
    "documentation_agent": DocumentationAgent,
    "security_agent": SecurityAgent,
}


def build_agent(
    role: str,
    *,
    router: ModelRouter,
    backend: Any,
    graph: RepositoryGraph | None = None,
    tools: dict[str, Any] | None = None,
    budget: Any | None = None,
) -> Any:
    """Resolve a role to a concrete agent.

    Unknown roles and ``implementation_agent`` fall back to the general
    ``ImplementationAgent``, so an unrecognised planner role degrades to
    working generic behaviour rather than failing the run.
    """
    cls = AGENT_CLASSES.get(role)
    if cls is None:
        from agent.sweforge.agents.roles import ImplementationAgent

        return ImplementationAgent(router=router, backend=backend, tools=tools, budget=budget)
    return cls(router=router, backend=backend, graph=graph, tools=tools, budget=budget)
