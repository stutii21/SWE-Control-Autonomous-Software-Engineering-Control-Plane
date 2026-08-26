"""Tests for Phase 23 remediation features.

Covers: specialized agents, plan-driven dispatch, real bind_tools tool-calling,
execution budgets as hard limits, model retry/fallback, tool error policy, MCP
orchestration, the Open SWE baseline adapter, and LangSmith node metadata.

No test here requires an API key or network access.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from agent.sweforge.agents.roles import ImplementationAgent
from agent.sweforge.agents.specialized import (
    AGENT_CLASSES,
    BackendAgent,
    BackendChanges,
    DatabaseAgent,
    DocChanges,
    DocumentationAgent,
    FrontendAgent,
    MigrationChanges,
    SecurityAgent,
    SecurityAssessment,
    TestAgent,
    TestChanges,
    build_agent,
)
from agent.sweforge.agents.tool_loop import ToolCallingLoop
from agent.sweforge.budget import BudgetExceeded, BudgetLimits, ExecutionBudget
from agent.sweforge.graph.workflow import (
    WorkflowConfig,
    make_route_after_failure_analysis,
    make_route_after_verification,
)
from agent.sweforge.mcp import (
    MCPCapabilityKind,
    MCPCapabilityRegistry,
    MCPInvocationPolicy,
    MCPToolSelector,
)
from agent.sweforge.mcp.fixtures import (
    FailingMCPAdapter,
    FixtureMCPAdapter,
    RecoveringMCPAdapter,
)
from agent.sweforge.models.scripted import ScriptedModelFactory
from agent.sweforge.observability.tracing import node_metadata, trace_node
from agent.sweforge.routing.execution_policy import (
    AllModelsFailed,
    ModelExecutionPolicy,
    fallback_chain,
)
from agent.sweforge.routing.model_router import ModelRouter
from agent.sweforge.schemas import FileEdit, Subtask, TaskPlan, VerificationResult
from agent.sweforge.tools.errors import ToolErrorAction, ToolErrorPolicy
from agent.sweforge.tools.registry import (
    ToolContext,
    build_tools,
    summarize_args,
    tools_by_name,
)
from evaluation.baselines import OpenSWEBaseline, describe_baseline_availability, preflight


def _plan(agent: str = "implementation_agent", complexity: str = "simple") -> TaskPlan:
    return TaskPlan(
        complexity=complexity,  # type: ignore[arg-type]
        relevant_files=["mod.py"],
        subtasks=[
            Subtask(id="st1", description="do work", agent=agent, target_files=["mod.py"])  # type: ignore[arg-type]
        ],
        testing_strategy="run tests",
    )


class _MemBackend:
    name = "mem"

    def __init__(self, files=None):
        self.files = dict(files or {"mod.py": "x = 1\n"})

    def read_file(self, path):
        return self.files[path]

    def write_file(self, path, content):
        self.files[path] = content


# ==========================================================================
# PART 3 — specialized agents are genuinely distinct
# ==========================================================================
class TestSpecializedAgents:
    def test_registry_covers_required_roles(self):
        for role in (
            "test_agent",
            "backend_agent",
            "frontend_agent",
            "database_agent",
            "documentation_agent",
            "security_agent",
        ):
            assert role in AGENT_CLASSES

    def test_at_least_five_specialized_plus_general(self):
        assert len(AGENT_CLASSES) >= 5

    def test_each_agent_has_a_distinct_output_model(self):
        models = {cls.output_model for cls in AGENT_CLASSES.values()}
        assert len(models) == len(AGENT_CLASSES), "agents must not share output shapes"

    def test_each_agent_has_a_distinct_system_prompt(self):
        prompts = {cls.system_prompt for cls in AGENT_CLASSES.values()}
        assert len(prompts) == len(AGENT_CLASSES), "agents must not share one prompt"

    def test_agents_use_different_model_roles(self):
        roles = {cls.model_role for cls in AGENT_CLASSES.values()}
        assert len(roles) >= 3, "specialisation should reach the routing layer"

    def test_agents_have_different_tool_grants(self):
        grants = {cls.tool_names for cls in AGENT_CLASSES.values()}
        assert len(grants) >= 3

    @pytest.mark.parametrize(
        "role,cls",
        [
            ("test_agent", TestAgent),
            ("backend_agent", BackendAgent),
            ("frontend_agent", FrontendAgent),
            ("database_agent", DatabaseAgent),
            ("documentation_agent", DocumentationAgent),
            ("security_agent", SecurityAgent),
        ],
    )
    def test_build_agent_resolves_role(self, role, cls):
        agent = build_agent(role, router=ModelRouter(env={}), backend=_MemBackend())
        assert isinstance(agent, cls)

    def test_unknown_role_falls_back_to_general_agent(self):
        agent = build_agent("nonexistent_agent", router=ModelRouter(env={}), backend=_MemBackend())
        assert isinstance(agent, ImplementationAgent)

    def test_test_agent_produces_test_changes(self):
        output = TestChanges(
            edits=[FileEdit(path="tests/test_mod.py", content="def test_x(): pass\n")],
            tests_added=["test_x"],
            behaviour_covered="boundary",
            fails_before_fix=True,
        )
        router = ModelRouter(
            env={}, model_factory=ScriptedModelFactory({"test_authoring": [output]})
        )
        result = TestAgent(router=router, backend=_MemBackend()).run(
            _plan("test_agent").subtasks[0], _plan("test_agent")
        )
        assert result.agent == "test_agent"
        assert "test_x" in result.notes
        assert result.succeeded

    def test_database_agent_reports_reversibility(self):
        output = MigrationChanges(
            edits=[FileEdit(path="migrations/001.py", content="# up\n")],
            is_reversible=False,
            destructive_operations=["DROP COLUMN legacy_id"],
        )
        router = ModelRouter(
            env={}, model_factory=ScriptedModelFactory({"implementation": [output]})
        )
        result = DatabaseAgent(router=router, backend=_MemBackend()).run(
            _plan("database_agent").subtasks[0], _plan("database_agent")
        )
        assert "reversible=False" in result.notes
        assert "DROP COLUMN legacy_id" in result.notes

    def test_backend_agent_reports_contract_change(self):
        output = BackendChanges(
            edits=[FileEdit(path="mod.py", content="def f(a, b): return a\n")],
            affected_callers=["api.py"],
            contract_changed=True,
        )
        router = ModelRouter(
            env={}, model_factory=ScriptedModelFactory({"implementation": [output]})
        )
        result = BackendAgent(router=router, backend=_MemBackend()).run(
            _plan("backend_agent").subtasks[0], _plan("backend_agent")
        )
        assert "contract_changed=True" in result.notes
        assert "api.py" in result.notes

    def test_security_agent_returns_findings_not_edits(self):
        """Assessing rather than fixing is the whole point of this agent."""
        output = SecurityAssessment(
            findings=[], requires_human_review=True, summary="credential risk"
        )
        router = ModelRouter(
            env={}, model_factory=ScriptedModelFactory({"security_analysis": [output]})
        )
        result = SecurityAgent(router=router, backend=_MemBackend()).run(
            _plan("security_agent").subtasks[0], _plan("security_agent")
        )
        assert result.edits == []
        assert result.succeeded is True
        assert "requires_human_review=True" in result.notes

    def test_documentation_agent_lists_updated_docs(self):
        output = DocChanges(
            edits=[FileEdit(path="README.md", content="# hi\n")],
            documents_updated=["README.md"],
            behaviour_documented="inclusive threshold",
        )
        router = ModelRouter(
            env={}, model_factory=ScriptedModelFactory({"documentation": [output]})
        )
        result = DocumentationAgent(router=router, backend=_MemBackend()).run(
            _plan("documentation_agent").subtasks[0], _plan("documentation_agent")
        )
        assert "README.md" in result.notes

    def test_agent_failure_is_captured_not_raised(self):
        router = ModelRouter(env={}, model_factory=ScriptedModelFactory({"test_authoring": []}))
        result = TestAgent(router=router, backend=_MemBackend()).run(
            _plan("test_agent").subtasks[0], _plan("test_agent")
        )
        assert result.succeeded is False
        assert "failed" in result.notes.lower()


# ==========================================================================
# PART 5 — real LangChain tool-calling semantics
# ==========================================================================
class _Out(BaseModel):
    value: str = "done"


def _echo_tool(name: str = "echo") -> StructuredTool:
    class Args(BaseModel):
        text: str

    return StructuredTool.from_function(
        func=lambda text: {"ok": True, "echo": text},
        name=name,
        description="Echo the supplied text back.",
        args_schema=Args,
    )


class TestToolCallingLoop:
    def _router(self, tool_rounds, outputs=None):
        return ModelRouter(
            env={},
            model_factory=ScriptedModelFactory(
                {"implementation": outputs or [_Out()]},
                tool_calls={"implementation": tool_rounds},
            ),
        )

    def test_bind_tools_path_executes(self):
        router = self._router([[{"name": "echo", "args": {"text": "hi"}}], []])
        loop = ToolCallingLoop(router=router, node_name="implementation")
        out = loop.run(
            spec=router.select("implementation").spec,
            messages=[HumanMessage("go")],
            tools=[_echo_tool()],
            output_model=_Out,
        )
        assert loop.tool_phase_ran is True
        assert isinstance(out, _Out)
        assert any(inv["tool"] == "echo" and inv["status"] == "ok" for inv in loop.tool_invocations)

    def test_records_agent_and_node_provenance(self):
        router = self._router([[{"name": "echo", "args": {"text": "hi"}}], []])
        loop = ToolCallingLoop(
            router=router, node_name="backend_implementation", agent_role="backend_agent"
        )
        loop.run(
            spec=router.select("implementation").spec,
            messages=[HumanMessage("go")],
            tools=[_echo_tool()],
            output_model=_Out,
        )
        assert loop.tool_invocations[0]["node"] == "backend_implementation"
        assert loop.tool_invocations[0]["agent"] == "backend_agent"

    def test_unknown_tool_name_is_handled_not_fatal(self):
        router = self._router([[{"name": "nope", "args": {}}], []])
        loop = ToolCallingLoop(router=router, node_name="implementation")
        out = loop.run(
            spec=router.select("implementation").spec,
            messages=[HumanMessage("go")],
            tools=[_echo_tool()],
            output_model=_Out,
        )
        assert isinstance(out, _Out)

    def test_loop_is_bounded_by_max_iterations(self):
        # A model that asks for a tool forever must still terminate.
        rounds = [[{"name": "echo", "args": {"text": "again"}}]] * 50
        router = self._router(rounds)
        loop = ToolCallingLoop(router=router, node_name="implementation", max_iterations=3)
        loop.run(
            spec=router.select("implementation").spec,
            messages=[HumanMessage("go")],
            tools=[_echo_tool()],
            output_model=_Out,
        )
        assert loop.iterations_used <= 3

    def test_no_tools_skips_tool_phase(self):
        router = self._router([])
        loop = ToolCallingLoop(router=router, node_name="implementation")
        loop.run(
            spec=router.select("implementation").spec,
            messages=[HumanMessage("go")],
            tools=[],
            output_model=_Out,
        )
        assert loop.tool_phase_ran is False

    def test_tool_budget_stops_tool_calls(self):
        router = self._router([[{"name": "echo", "args": {"text": "hi"}}], []])
        budget = ExecutionBudget(BudgetLimits(max_tool_calls=0))
        loop = ToolCallingLoop(router=router, node_name="implementation", budget=budget)
        out = loop.run(
            spec=router.select("implementation").spec,
            messages=[HumanMessage("go")],
            tools=[_echo_tool()],
            output_model=_Out,
        )
        assert isinstance(out, _Out)  # still produces a result
        assert budget.tool_calls == 0

    def test_tool_message_contract_is_used(self):
        """A ToolMessage must be appended for each executed tool call."""
        seen: list[str] = []

        class _Recorder(StructuredTool):
            pass

        router = self._router([[{"name": "echo", "args": {"text": "hi"}}], []])
        loop = ToolCallingLoop(router=router, node_name="implementation")
        original = ToolMessage.__init__

        def patched(self, *args, **kwargs):
            seen.append(str(kwargs.get("content", args[0] if args else "")))
            original(self, *args, **kwargs)

        ToolMessage.__init__ = patched  # type: ignore[method-assign]
        try:
            loop.run(
                spec=router.select("implementation").spec,
                messages=[HumanMessage("go")],
                tools=[_echo_tool()],
                output_model=_Out,
            )
        finally:
            ToolMessage.__init__ = original  # type: ignore[method-assign]
        assert seen, "no ToolMessage was constructed"
        assert any("echo" in s for s in seen)

    def test_scripted_model_emits_real_ai_message_tool_calls(self):
        factory = ScriptedModelFactory(
            {"implementation": [_Out()]},
            tool_calls={"implementation": [[{"name": "echo", "args": {"text": "x"}}]]},
        )
        model = factory(type("S", (), {"role": "implementation"})())
        bound = model.bind_tools([_echo_tool()])
        message = bound.invoke([HumanMessage("go")])
        assert isinstance(message, AIMessage)
        assert message.tool_calls
        assert message.tool_calls[0]["name"] == "echo"


class TestToolLedgerProvenance:
    def test_summarize_args_redacts_values(self):
        summary = summarize_args({"content": "s" * 5000, "files": {"a": "b"}})
        assert "str[5000]" in summary
        assert "sssss" not in summary

    def test_ledger_records_node_and_error_category(self, tmp_path):
        (tmp_path / "m.py").write_text("x = 1\n")
        context = ToolContext(repo_root=str(tmp_path))
        tools = tools_by_name(build_tools(context))
        context.current_node = "verification"
        result = tools["run_validation"].invoke({"changed_files": ["m.py"]})
        assert result["ok"] is False
        assert "error_category" in result
        call = context.ledger.calls[0]
        assert call.node == "verification"
        assert call.status != "ok"


# ==========================================================================
# PART 8 — execution budgets are hard limits
# ==========================================================================
class TestExecutionBudget:
    def test_model_budget_exceeded_raises(self):
        budget = ExecutionBudget(BudgetLimits(max_model_calls=2))
        budget.consume_model_call()
        budget.consume_model_call()
        with pytest.raises(BudgetExceeded) as excinfo:
            budget.check_model_call()
        assert excinfo.value.limit_name == "max_model_calls"

    def test_tool_budget_exceeded_raises(self):
        budget = ExecutionBudget(BudgetLimits(max_tool_calls=1))
        budget.consume_tool_call()
        with pytest.raises(BudgetExceeded):
            budget.check_tool_call()

    def test_cost_budget_exceeded_raises(self):
        budget = ExecutionBudget(BudgetLimits(max_estimated_cost_usd=0.01))
        budget.consume_model_call(cost_usd=0.02)
        with pytest.raises(BudgetExceeded) as excinfo:
            budget.check_cost()
        assert excinfo.value.limit_name == "max_estimated_cost_usd"

    def test_wall_time_budget_exceeded_raises(self):
        clock = iter([0.0, 100.0, 100.0, 100.0])
        budget = ExecutionBudget(
            BudgetLimits(max_wall_time_seconds=10.0), clock=lambda: next(clock)
        )
        with pytest.raises(BudgetExceeded) as excinfo:
            budget.check_wall_time()
        assert excinfo.value.limit_name == "max_wall_time_seconds"

    def test_recovery_budget_exceeded_raises(self):
        budget = ExecutionBudget(BudgetLimits(max_recovery_attempts=3))
        with pytest.raises(BudgetExceeded):
            budget.check_recovery(3)

    def test_token_budget_exceeded_raises(self):
        budget = ExecutionBudget(BudgetLimits(max_input_tokens=100))
        budget.consume_model_call(input_tokens=500)
        with pytest.raises(BudgetExceeded):
            budget.check_tokens()

    def test_none_disables_an_individual_limit(self):
        budget = ExecutionBudget(BudgetLimits(max_model_calls=None))
        for _ in range(50):
            budget.consume_model_call()
        budget.check_model_call()  # must not raise

    def test_would_exceed_is_non_raising(self):
        budget = ExecutionBudget(BudgetLimits(max_tool_calls=0))
        assert budget.would_exceed("tool") is True
        assert budget.would_exceed("nonsense") is False

    def test_snapshot_reports_headroom(self):
        budget = ExecutionBudget(BudgetLimits(max_model_calls=10))
        budget.consume_model_call()
        snapshot = budget.snapshot()
        assert snapshot.model_calls_used == 1
        assert snapshot.model_calls_remaining == 9
        assert snapshot.exhausted is False

    def test_exhaustion_is_recorded_for_routing(self):
        budget = ExecutionBudget(BudgetLimits(max_model_calls=0))
        with pytest.raises(BudgetExceeded):
            budget.check_model_call()
        assert budget.is_exhausted
        assert budget.snapshot().exhausted_reason

    def test_sync_from_ledger_adopts_totals(self):
        router = ModelRouter(env={})
        spec = router.select("planning").spec
        with router.track("planning", spec) as usage:
            usage["input_tokens"] = 1000
            usage["output_tokens"] = 200
        budget = ExecutionBudget()
        budget.sync_from_ledger(router.ledger)
        assert budget.input_tokens == 1000
        assert budget.cost_usd > 0

    def test_routing_sends_exhausted_run_to_terminal(self):
        budget = ExecutionBudget(BudgetLimits(max_model_calls=0))
        with pytest.raises(BudgetExceeded):
            budget.check_model_call()
        route = make_route_after_failure_analysis(WorkflowConfig(), budget)
        assert route({"recovery_attempts": []}) == "budget_exhausted"

    def test_passing_run_not_diverted_by_budget(self):
        """A green run should still reach its gates and be reported."""
        budget = ExecutionBudget(BudgetLimits(max_model_calls=0))
        with pytest.raises(BudgetExceeded):
            budget.check_model_call()
        route = make_route_after_verification(WorkflowConfig(), budget)
        state = {"test_results": VerificationResult(passed=True), "execution_metrics": None}
        assert route(state) != "budget_exhausted"


# ==========================================================================
# PART 7 / 9 — model retry and fallback
# ==========================================================================
class TestModelExecutionPolicy:
    def _specs(self):
        return fallback_chain(ModelRouter(env={}), "planning")

    def test_fallback_chain_is_ordered_and_deduped(self):
        chain = self._specs()
        assert len(chain) >= 2
        assert len({s.model_id for s in chain}) == len(chain)

    def test_success_on_first_attempt_records_one_attempt(self):
        policy = ModelExecutionPolicy()
        result, attempts = policy.execute(specs=self._specs(), operation=lambda spec: "ok")
        assert result == "ok"
        assert len(attempts) == 1
        assert attempts[0].status == "success"
        assert policy.fallback_used is False

    def test_retries_the_same_model_on_timeout(self):
        calls = {"n": 0}

        def operation(spec):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("request timed out")
            return "recovered"

        policy = ModelExecutionPolicy()
        result, attempts = policy.execute(specs=self._specs(), operation=operation)
        assert result == "recovered"
        assert [a.status for a in attempts] == ["timeout", "success"]
        assert attempts[1].was_fallback is False
        assert policy.retry_count == 1

    def test_falls_back_to_a_different_model(self):
        seen: list[str] = []

        def operation(spec):
            seen.append(spec.model_id)
            if len(seen) <= 3:  # exhaust retries on the primary
                raise RuntimeError("model overloaded")
            return "fallback-ok"

        policy = ModelExecutionPolicy(max_retries_per_model=2)
        result, attempts = policy.execute(specs=self._specs(), operation=operation)
        assert result == "fallback-ok"
        assert policy.fallback_used is True
        assert seen[0] != seen[-1], "fallback must use a different model"

    def test_fallback_counted_as_separate_attempts(self):
        def operation(spec):
            raise RuntimeError("overloaded")

        policy = ModelExecutionPolicy(max_retries_per_model=1)
        with pytest.raises(AllModelsFailed) as excinfo:
            policy.execute(specs=self._specs(), operation=operation)
        assert len(excinfo.value.attempts) > 2
        assert any(a.was_fallback for a in excinfo.value.attempts)

    def test_non_retryable_error_does_not_spend_fallbacks(self):
        seen: list[str] = []

        def operation(spec):
            seen.append(spec.model_id)
            raise ValueError("prompt is malformed")

        policy = ModelExecutionPolicy()
        with pytest.raises(AllModelsFailed):
            policy.execute(specs=self._specs(), operation=operation)
        assert len(set(seen)) == 1, "a deterministic failure must not try other models"

    def test_budget_error_propagates_rather_than_falling_back(self):
        budget = ExecutionBudget(BudgetLimits(max_model_calls=0))

        def operation(spec):
            return "never reached"

        with pytest.raises(BudgetExceeded):
            ModelExecutionPolicy().execute(specs=self._specs(), operation=operation, budget=budget)

    def test_summary_is_machine_readable(self):
        policy = ModelExecutionPolicy()
        policy.execute(specs=self._specs(), operation=lambda spec: "ok")
        summary = policy.summary()
        assert summary["succeeded"] is True
        assert summary["total_attempts"] == 1
        assert isinstance(summary["attempts"], list)

    @pytest.mark.parametrize(
        "exc,expected",
        [
            (TimeoutError("timed out"), "timeout"),
            (RuntimeError("rate limit exceeded (429)"), "rate_limit"),
            (RuntimeError("503 service unavailable"), "transient"),
            (ValueError("bad prompt"), "error"),
        ],
    )
    def test_classification(self, exc, expected):
        assert ModelExecutionPolicy.classify(exc) == expected


# ==========================================================================
# PART 9 — tool error policy
# ==========================================================================
class TestToolErrorPolicy:
    @pytest.mark.parametrize(
        "text,category",
        [
            ("ValidationError: limit must be <= 50", "validation_error"),
            ("TimeoutError: timed out after 30s", "timeout"),
            ("PermissionError: permission denied", "permission_error"),
            ("FileNotFoundError: no such file", "not_found"),
            ("ConnectionError: connection reset by peer", "transient_error"),
            ("something unexplained", "permanent_error"),
        ],
    )
    def test_classification(self, text, category):
        assert ToolErrorPolicy().classify_text(text).category == category

    def test_validation_error_returns_to_agent(self):
        policy = ToolErrorPolicy()
        decision = policy.decide(policy.classify_text("ValidationError: nope"), 1)
        assert decision.action is ToolErrorAction.RETURN_TO_AGENT

    def test_transient_error_retries_then_escalates(self):
        policy = ToolErrorPolicy(max_retries=2)
        classification = policy.classify_text("ConnectionError: connection reset")
        assert policy.decide(classification, 1).action is ToolErrorAction.RETRY
        assert policy.decide(classification, 3).action is ToolErrorAction.ESCALATE

    def test_permission_error_never_retries(self):
        policy = ToolErrorPolicy()
        decision = policy.decide(policy.classify_text("403 forbidden"), 1)
        assert decision.action is ToolErrorAction.ESCALATE

    def test_not_found_is_skipped(self):
        policy = ToolErrorPolicy()
        assert (
            policy.decide(policy.classify_text("404 not found"), 1).action is ToolErrorAction.SKIP
        )

    def test_backoff_grows(self):
        policy = ToolErrorPolicy(base_backoff_seconds=1.0)
        classification = policy.classify_text("timed out")
        assert (
            policy.decide(classification, 2).backoff_seconds
            > policy.decide(classification, 1).backoff_seconds
        )

    def test_classify_payload_reads_error_field(self):
        classification = ToolErrorPolicy().classify_payload(
            {"ok": False, "error": "PermissionError: denied"}
        )
        assert classification.category == "permission_error"


# ==========================================================================
# PART 6 — MCP orchestration
# ==========================================================================
class TestMCPOrchestration:
    def _registry(self):
        registry = MCPCapabilityRegistry()
        registry.register(FixtureMCPAdapter())
        registry.register(RecoveringMCPAdapter())
        registry.discover()
        return registry

    def test_discovery_exposes_capabilities_and_schemas(self):
        registry = self._registry()
        capability = registry.get("get_issue")
        assert capability is not None
        assert capability.kind is MCPCapabilityKind.ISSUE_TRACKER
        assert "issue_id" in capability.input_schema.get("properties", {})

    def test_discovery_survives_a_failing_server(self):
        registry = MCPCapabilityRegistry()

        class _Broken:
            name = "broken"

            def list_tools(self):
                raise ConnectionError("server down")

            def call_tool(self, name, arguments):
                raise ConnectionError("server down")

        registry.register(_Broken())
        registry.register(FixtureMCPAdapter())
        capabilities = registry.discover()
        assert any(c.name == "get_issue" for c in capabilities)
        assert "broken" in registry.discovery_errors

    def test_selector_detects_issue_reference(self):
        selection = MCPToolSelector(self._registry()).select(
            task="Investigate the issue linked to #412 and fix it"
        )
        assert selection.needed is True
        assert selection.capability == "get_issue"
        assert selection.arguments["issue_id"] == "412"

    def test_selector_declines_when_no_external_reference(self):
        selection = MCPToolSelector(self._registry()).select(task="Rename a local variable")
        assert selection.needed is False
        assert selection.capability is None

    def test_selector_is_deterministic(self):
        selector = MCPToolSelector(self._registry())
        first = selector.select(task="fix issue #7")
        second = selector.select(task="fix issue #7")
        assert first.to_dict() == second.to_dict()

    def test_deny_by_default(self):
        registry = self._registry()
        policy = MCPInvocationPolicy(registry)  # no allowlist
        result = policy.invoke("get_issue", {"issue_id": "412"})
        assert result.ok is False
        assert result.error_category == "permission_error"

    def test_allowlisted_call_succeeds(self):
        registry = self._registry()
        policy = MCPInvocationPolicy(registry, allowlist={"get_issue"})
        result = policy.invoke("get_issue", {"issue_id": "412"})
        assert result.ok is True
        assert result.content["_fixture"] is True  # fixture data, clearly labelled

    def test_retry_then_success(self):
        registry = MCPCapabilityRegistry()
        adapter = RecoveringMCPAdapter(failures_before_success=1)
        registry.register(adapter)
        registry.discover()
        policy = MCPInvocationPolicy(registry, allowlist={"doc_lookup"})
        result = policy.invoke("doc_lookup", {})
        assert result.ok is True
        assert result.attempts == 2

    def test_permanent_failure_is_structured_not_raised(self):
        registry = MCPCapabilityRegistry()
        registry.register(FailingMCPAdapter(error=PermissionError("permission denied")))
        registry.discover()
        policy = MCPInvocationPolicy(registry, allowlist={"flaky_lookup"})
        result = policy.invoke("flaky_lookup", {})
        assert result.ok is False
        assert result.error_category == "permission_error"

    def test_call_budget_per_run_enforced(self):
        registry = self._registry()
        policy = MCPInvocationPolicy(registry, allowlist={"get_issue"}, max_calls_per_run=1)
        policy.invoke("get_issue", {"issue_id": "1"})
        blocked = policy.invoke("get_issue", {"issue_id": "2"})
        assert blocked.ok is False
        assert blocked.error_category == "budget"

    def test_unknown_capability_reports_not_found(self):
        policy = MCPInvocationPolicy(self._registry(), allowlist={"ghost"})
        result = policy.invoke("ghost", {})
        assert result.error_category == "not_found"

    def test_execution_budget_blocks_mcp_call(self):
        budget = ExecutionBudget(BudgetLimits(max_tool_calls=0))
        policy = MCPInvocationPolicy(self._registry(), allowlist={"get_issue"}, budget=budget)
        result = policy.invoke("get_issue", {"issue_id": "1"})
        assert result.ok is False
        assert result.error_category == "budget"

    def test_summary_is_machine_readable(self):
        policy = MCPInvocationPolicy(self._registry(), allowlist={"get_issue"})
        policy.invoke("get_issue", {"issue_id": "1"})
        summary = policy.summary()
        assert summary["total"] == 1
        assert summary["successes"] == 1


# ==========================================================================
# PART 2 — Open SWE baseline adapter
# ==========================================================================
class TestOpenSWEBaseline:
    def test_preflight_reports_missing_modules(self):
        check = preflight({})
        assert isinstance(check.missing_modules, list)
        assert check.can_run is False
        assert check.reason()

    def test_preflight_names_missing_credentials(self):
        reason = preflight({}).reason()
        assert "credential" in reason

    def test_availability_description_is_machine_readable(self):
        payload = describe_baseline_availability({})
        assert payload["baseline"] == "open_swe_upstream"
        assert payload["can_run"] is False
        assert "details" in payload

    def test_unavailable_run_is_not_fabricated(self):
        """With no upstream present the run must report unavailable, not a score."""
        result = OpenSWEBaseline(env={}).run(task="fix a bug", repo_root="/tmp")
        assert result.available is False
        assert result.status == "unavailable"
        assert result.unavailable_reason
        assert result.upstream_invoked is False
        assert result.metrics == {}

    def test_adapter_really_invokes_the_upstream_entry_point(self):
        """Proves the adapter calls upstream rather than reimplementing it."""
        calls: list[str] = []

        class _FakeUpstreamAgent:
            def invoke(self, payload, config=None):
                calls.append("invoked")
                return {
                    "messages": [
                        {"role": "user", "content": "task"},
                        {"role": "assistant", "content": "done", "tool_calls": [{"name": "x"}]},
                    ]
                }

        def factory():
            calls.append("factory")
            return _FakeUpstreamAgent()

        baseline = OpenSWEBaseline(env={}, agent_factory=factory)
        result = baseline.run(task="fix a bug", repo_root="/tmp", repository="acme/app")
        assert calls == ["factory", "invoked"]
        assert result.upstream_invoked is True
        assert result.available is True
        assert baseline.invocations

    def test_resolve_agent_factory_targets_real_upstream_symbol(self):
        """It must resolve upstream's own symbol, never a SWE-Forge substitute.

        Environment-independent by design: whether upstream's dependencies are
        installed varies, but the invariant does not. Either the import fails
        (deps absent) or it returns the genuine ``agent.server.get_agent`` —
        never a SWE-Forge fallback masquerading as a baseline.
        """
        baseline = OpenSWEBaseline(env={})
        try:
            factory = baseline.resolve_agent_factory()
        except (ImportError, ModuleNotFoundError):
            return  # upstream deps absent; nothing was substituted
        assert factory.__module__ == "agent.server"
        assert factory.__name__ == "get_agent"
        assert "sweforge" not in factory.__module__

    def test_run_is_unavailable_without_credentials_even_when_importable(self):
        """Reaching the upstream symbol is not the same as executing it."""
        result = OpenSWEBaseline(env={}).run(task="t", repo_root="/tmp")
        assert result.available is False
        assert result.upstream_invoked is False
        assert "credential" in (result.unavailable_reason or "")

    def test_absent_metrics_are_none_not_zero(self):
        """Upstream exposes no verification result; None must not become 0."""

        class _Agent:
            def invoke(self, payload, config=None):
                return {"messages": []}

        result = OpenSWEBaseline(env={}, agent_factory=lambda: _Agent()).run(
            task="t", repo_root="/tmp"
        )
        assert result.metrics["verification_passed"] is None
        assert result.metrics["recovery_attempts"] is None

    def test_upstream_exception_marks_unavailable(self):
        def factory():
            raise RuntimeError("sandbox provider not configured")

        result = OpenSWEBaseline(env={}, agent_factory=factory).run(task="t", repo_root="/tmp")
        assert result.available is False
        assert "sandbox provider" in (result.unavailable_reason or "")


# ==========================================================================
# PART 10 — LangSmith node metadata
# ==========================================================================
class TestTraceMetadata:
    def test_metadata_is_namespaced(self):
        payload = node_metadata(node="recovery", agent="backend_agent")
        assert payload["sweforge.node"] == "recovery"
        assert payload["sweforge.agent"] == "backend_agent"

    def test_none_values_are_dropped(self):
        payload = node_metadata(node="x", agent=None)
        assert "sweforge.agent" not in payload

    def test_secret_like_fields_are_filtered(self):
        payload = node_metadata(node="x", api_key="SECRET", auth_token="SECRET")
        assert not any("SECRET" in str(v) for v in payload.values())

    def test_rich_fields_supported(self):
        payload = node_metadata(
            node="risk_gate",
            task_id="t1",
            model="anthropic:x",
            tier="reasoning",
            attempt=2,
            recovery_attempt=1,
            tool="security_scan",
            risk_score=90,
            final_status="awaiting_human_approval",
            budget_remaining={"model_calls_remaining": 5},
        )
        for key in (
            "sweforge.task_id",
            "sweforge.tier",
            "sweforge.recovery_attempt",
            "sweforge.risk_score",
            "sweforge.budget_remaining",
        ):
            assert key in payload

    def test_trace_node_is_noop_when_disabled(self):
        with trace_node(node="x", agent="y"):
            pass  # must not raise

    def test_trace_node_runs_with_tracing_enabled(self, monkeypatch):
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "fake-key-not-used-for-network")
        with trace_node(node="planning", agent="planner"):
            pass  # exercises the enabled branch without asserting on remote state


# ==========================================================================
# PART 4 — agent selection actually changes execution
# ==========================================================================
def _dispatch_plan(*specs: tuple[str, str]) -> TaskPlan:
    """Build a plan whose subtasks name specific agents."""
    subtasks = [
        Subtask(id=f"st{i + 1}", description=desc, agent=role, target_files=["calc.py"])  # type: ignore[arg-type]
        for i, (role, desc) in enumerate(specs)
    ]
    return TaskPlan(
        complexity="moderate",
        relevant_files=["calc.py"],
        subtasks=subtasks,
        testing_strategy="run tests",
    )


GOOD_CALC = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def calc_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("SWEFORGE_ALLOW_LOCAL_EXEC", "1")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\npythonpath = .\n")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    return tmp_path


class TestDynamicDispatch:
    """Different plans must execute different agents, not just record names."""

    def _run(self, repo, plan, script):
        from agent.sweforge.runner import run_task

        full_script = {"planning": [plan], **script}
        router = ModelRouter(env={}, model_factory=ScriptedModelFactory(full_script))
        return run_task(
            task="make add() correct",
            repo_root=str(repo),
            repository="test/calc",
            config=WorkflowConfig(),
            router=router,
            backend_kind="local",
            memory_path=str(repo / ".sweforge" / "e.jsonl"),
        )

    def test_backend_plan_dispatches_backend_agent(self, calc_repo):
        outcome = self._run(
            calc_repo,
            _dispatch_plan(("backend_agent", "fix the service logic")),
            {
                "implementation": [
                    BackendChanges(
                        edits=[FileEdit(path="calc.py", content=GOOD_CALC)],
                        affected_callers=["api.py"],
                        contract_changed=False,
                    )
                ],
                "review": [
                    __import__("agent.sweforge.schemas", fromlist=["ReviewResult"]).ReviewResult(
                        approved=True
                    )
                ],
            },
        )
        assert "backend_agent" in outcome.state.get("agents_executed", [])
        assert "backend_agent" in " ".join(outcome.node_trace)

    def test_documentation_plan_dispatches_documentation_agent(self, calc_repo):
        outcome = self._run(
            calc_repo,
            _dispatch_plan(("documentation_agent", "update the README wording")),
            {
                "documentation": [
                    DocChanges(
                        edits=[FileEdit(path="calc.py", content=GOOD_CALC)],
                        documents_updated=["README.md"],
                    )
                ],
                "review": [
                    __import__("agent.sweforge.schemas", fromlist=["ReviewResult"]).ReviewResult(
                        approved=True
                    )
                ],
            },
        )
        assert "documentation_agent" in outcome.state.get("agents_executed", [])
        assert "backend_agent" not in outcome.state.get("agents_executed", [])

    def test_database_plan_dispatches_database_agent(self, calc_repo):
        outcome = self._run(
            calc_repo,
            _dispatch_plan(("database_agent", "add a migration")),
            {
                "implementation": [
                    MigrationChanges(
                        edits=[FileEdit(path="calc.py", content=GOOD_CALC)],
                        is_reversible=True,
                    )
                ],
                "review": [
                    __import__("agent.sweforge.schemas", fromlist=["ReviewResult"]).ReviewResult(
                        approved=True
                    )
                ],
            },
        )
        assert "database_agent" in outcome.state.get("agents_executed", [])

    def test_multi_agent_plan_dispatches_each_in_order(self, calc_repo):
        plan = TaskPlan(
            complexity="moderate",
            relevant_files=["calc.py"],
            subtasks=[
                Subtask(
                    id="st1",
                    description="fix backend",
                    agent="backend_agent",
                    target_files=["calc.py"],
                ),
                Subtask(
                    id="st2",
                    description="add tests",
                    agent="test_agent",
                    target_files=["tests/test_calc.py"],
                    depends_on=["st1"],
                ),
            ],
            testing_strategy="run tests",
        )
        outcome = self._run(
            calc_repo,
            plan,
            {
                "implementation": [
                    BackendChanges(edits=[FileEdit(path="calc.py", content=GOOD_CALC)])
                ],
                "test_authoring": [
                    TestChanges(
                        edits=[
                            FileEdit(
                                path="tests/test_calc.py",
                                content=(
                                    "from calc import add\n\n\n"
                                    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
                                    "def test_add_zero():\n    assert add(0, 0) == 0\n"
                                ),
                            )
                        ],
                        tests_added=["test_add_zero"],
                    )
                ],
                "review": [
                    __import__("agent.sweforge.schemas", fromlist=["ReviewResult"]).ReviewResult(
                        approved=True
                    )
                ],
            },
        )
        executed = outcome.state.get("agents_executed", [])
        assert executed == ["backend_agent", "test_agent"]
        assert outcome.verification_passed

    def test_different_plans_produce_different_execution_paths(self, calc_repo):
        """The core Phase 23 claim, asserted directly."""
        from agent.sweforge.schemas import ReviewResult

        backend = self._run(
            calc_repo,
            _dispatch_plan(("backend_agent", "fix service logic")),
            {
                "implementation": [
                    BackendChanges(edits=[FileEdit(path="calc.py", content=GOOD_CALC)])
                ],
                "review": [ReviewResult(approved=True)],
            },
        )
        (calc_repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        docs = self._run(
            calc_repo,
            _dispatch_plan(("documentation_agent", "update docs")),
            {
                "documentation": [DocChanges(edits=[FileEdit(path="calc.py", content=GOOD_CALC)])],
                "review": [ReviewResult(approved=True)],
            },
        )
        assert backend.state["agents_executed"] != docs.state["agents_executed"]

    def test_all_twelve_tools_exercised_end_to_end(self, calc_repo):
        """Integration proof that no tool is a dead demonstration."""
        from agent.sweforge.graph.workflow import build_workflow
        from agent.sweforge.runner import build_runtime
        from agent.sweforge.schemas import ReviewResult
        from agent.sweforge.state.graph_state import initial_state

        plan = _dispatch_plan(("backend_agent", "fix service logic"))
        factory = ScriptedModelFactory(
            {
                "planning": [plan],
                "implementation": [
                    BackendChanges(edits=[FileEdit(path="calc.py", content=GOOD_CALC)])
                ],
                "review": [ReviewResult(approved=True)],
            },
            tool_calls={
                "implementation": [
                    [{"name": "find_relevant_files", "args": {"task": "add", "limit": 2}}],
                    [],
                ]
            },
        )
        router = ModelRouter(env={}, model_factory=factory)
        runtime = build_runtime(
            repo_root=str(calc_repo),
            config=WorkflowConfig(),
            router=router,
            backend_kind="local",
            memory_path=str(calc_repo / ".sweforge" / "e.jsonl"),
        )
        graph = build_workflow(runtime)
        graph.invoke(
            initial_state("make add() correct", "test/calc", str(calc_repo)),
            config={"recursion_limit": 60},
        )
        used = runtime.tool_context.ledger.by_tool()
        required = [
            "analyze_repository",
            "build_repository_graph",
            "find_relevant_files",
            "find_dependencies",
            "find_callers",
            "find_related_tests",
            "run_validation",
            "inspect_git_diff",
            "retrieve_similar_tasks",
            "calculate_change_risk",
            "security_scan",
        ]
        missing = [name for name in required if not used.get(name)]
        assert missing == [], f"tools never exercised: {missing}"

    def test_analyze_failure_tool_exercised_on_failure_path(self, calc_repo):
        from agent.sweforge.agents.roles import RepairOutput
        from agent.sweforge.graph.workflow import build_workflow
        from agent.sweforge.runner import build_runtime
        from agent.sweforge.schemas import FailureDiagnosis, ReviewResult
        from agent.sweforge.state.graph_state import initial_state

        plan = _dispatch_plan(("backend_agent", "fix service logic"))
        factory = ScriptedModelFactory(
            {
                "planning": [plan],
                "implementation": [
                    BackendChanges(
                        edits=[
                            FileEdit(path="calc.py", content="def add(a, b):\n    return a * b\n")
                        ]
                    )
                ],
                "recovery": [
                    RepairOutput(
                        diagnosis=FailureDiagnosis(
                            category="test_assertion", root_cause="wrong operator"
                        ),
                        edits=[FileEdit(path="calc.py", content=GOOD_CALC)],
                        strategy="use addition",
                    )
                ],
                "review": [ReviewResult(approved=True)],
            }
        )
        router = ModelRouter(env={}, model_factory=factory)
        runtime = build_runtime(
            repo_root=str(calc_repo),
            config=WorkflowConfig(),
            router=router,
            backend_kind="local",
            memory_path=str(calc_repo / ".sweforge" / "e.jsonl"),
        )
        graph = build_workflow(runtime)
        graph.invoke(
            initial_state("make add() correct", "test/calc", str(calc_repo)),
            config={"recursion_limit": 60},
        )
        assert runtime.tool_context.ledger.by_tool().get("analyze_failure")


# ==========================================================================
# PART 14 — risk-gated GitHub PR finalization
# ==========================================================================
class TestPullRequestFinalization:
    def _risk(self, level: str, score: int):
        from agent.sweforge.schemas import RiskScore

        return RiskScore(score=score, level=level)  # type: ignore[arg-type]

    def _green(self):
        return VerificationResult(passed=True, tests_run=2, tests_passed=2)

    def test_low_risk_is_eligible_for_open_pr(self):
        from agent.sweforge.github import PullRequestDecision, prepare_pull_request

        plan = prepare_pull_request(
            task="Fix the boundary",
            changed_files=["m.py"],
            risk=self._risk("LOW", 5),
            verification=self._green(),
        )
        assert plan.decision is PullRequestDecision.OPEN
        assert plan.draft is False

    def test_medium_risk_produces_draft(self):
        from agent.sweforge.github import PullRequestDecision, prepare_pull_request

        plan = prepare_pull_request(
            task="Bump a dependency",
            changed_files=["pyproject.toml"],
            risk=self._risk("MEDIUM", 35),
            verification=self._green(),
        )
        assert plan.decision is PullRequestDecision.DRAFT
        assert plan.draft is True

    def test_high_risk_is_blocked(self):
        from agent.sweforge.github import PullRequestDecision, prepare_pull_request

        plan = prepare_pull_request(
            task="Add deploy token",
            changed_files=["deploy.py"],
            risk=self._risk("HIGH", 90),
            verification=self._green(),
        )
        assert plan.decision is PullRequestDecision.BLOCKED
        assert "human approval" in plan.reason

    def test_failed_verification_blocks_pr(self):
        from agent.sweforge.github import PullRequestDecision, prepare_pull_request

        plan = prepare_pull_request(
            task="t",
            changed_files=["m.py"],
            risk=self._risk("LOW", 0),
            verification=VerificationResult(passed=False, tests_run=1, tests_failed=1),
        )
        assert plan.decision is PullRequestDecision.BLOCKED

    def test_review_rejection_blocks_pr(self):
        from agent.sweforge.github import PullRequestDecision, prepare_pull_request
        from agent.sweforge.schemas import ReviewFinding, ReviewResult

        review = ReviewResult(
            approved=False, findings=[ReviewFinding(severity="major", message="incomplete")]
        )
        plan = prepare_pull_request(
            task="t",
            changed_files=["m.py"],
            risk=self._risk("LOW", 0),
            verification=self._green(),
            review=review,
        )
        assert plan.decision is PullRequestDecision.BLOCKED

    def test_no_pr_is_created_without_explicit_permission(self):
        """A dry run must never open an external PR."""
        from agent.sweforge.github import prepare_pull_request

        called = []
        plan = prepare_pull_request(
            task="t",
            changed_files=["m.py"],
            risk=self._risk("LOW", 0),
            verification=self._green(),
            upstream_creator=lambda **kw: called.append(kw),
        )
        assert plan.created is False
        assert called == []
        assert "allow_creation=False" in (plan.creation_unavailable_reason or "")

    def test_creation_delegates_to_injected_upstream(self):
        """SWE-Forge must delegate PR mechanics, not reimplement them."""
        from agent.sweforge.github import prepare_pull_request

        received: dict = {}

        def creator(**kwargs):
            received.update(kwargs)
            return {"number": 7, "url": "https://example.invalid/pr/7"}

        plan = prepare_pull_request(
            task="Fix boundary",
            changed_files=["m.py"],
            risk=self._risk("LOW", 0),
            verification=self._green(),
            upstream_creator=creator,
            allow_creation=True,
        )
        assert plan.created is True
        assert plan.upstream_result["number"] == 7
        assert received["title"]

    def test_live_integration_reported_unavailable_without_creator(self):
        from agent.sweforge.github import prepare_pull_request

        plan = prepare_pull_request(
            task="t",
            changed_files=["m.py"],
            risk=self._risk("LOW", 0),
            verification=self._green(),
            allow_creation=True,
        )
        assert plan.created is False
        assert "UNAVAILABLE" in (plan.creation_unavailable_reason or "")

    def test_body_includes_risk_and_verification_evidence(self):
        from agent.sweforge.github import prepare_pull_request
        from agent.sweforge.schemas import RiskFactor, RiskScore

        risk = RiskScore(
            score=30,
            level="MEDIUM",
            factors=[RiskFactor(code="dependency_manifest", weight=18, detail="pyproject.toml")],
        )
        plan = prepare_pull_request(
            task="Bump dep",
            changed_files=["pyproject.toml"],
            risk=risk,
            verification=self._green(),
            recovery_attempts=1,
        )
        assert "Risk assessment" in plan.body
        assert "dependency_manifest" in plan.body
        assert "Recovery attempts required: 1" in plan.body


# ==========================================================================
# PART 15/16 — live-model track and experiment separation
# ==========================================================================
class TestLiveEvaluationConfig:
    def test_unavailable_without_credentials(self):
        from evaluation.live import LiveEvalConfig

        config = LiveEvalConfig.from_env({})
        assert config.available is False
        assert "ANTHROPIC_API_KEY" in config.unavailable_reason()

    def test_available_when_fully_configured(self):
        from evaluation.live import LiveEvalConfig

        config = LiveEvalConfig.from_env(
            {
                "SWEFORGE_EVAL_PROVIDER": "anthropic",
                "SWEFORGE_EVAL_MODEL": "claude-sonnet-4-5",
                "ANTHROPIC_API_KEY": "not-a-real-key",
            }
        )
        assert config.available is True
        assert config.model_id == "anthropic:claude-sonnet-4-5"

    def test_config_never_exposes_the_key_value(self):
        from evaluation.live import LiveEvalConfig

        config = LiveEvalConfig.from_env(
            {
                "SWEFORGE_EVAL_MODEL": "m",
                "ANTHROPIC_API_KEY": "SUPER-SECRET-VALUE",
            }
        )
        assert "SUPER-SECRET-VALUE" not in str(config.to_dict())
        assert config.to_dict()["credential_present"] is True

    def test_unknown_provider_reported(self):
        from evaluation.live import LiveEvalConfig

        config = LiveEvalConfig.from_env(
            {"SWEFORGE_EVAL_PROVIDER": "nope", "SWEFORGE_EVAL_MODEL": "m"}
        )
        assert config.available is False
        assert "unknown provider" in config.unavailable_reason()

    def test_malformed_number_reported_not_crashed(self):
        from evaluation.live import LiveEvalConfig

        config = LiveEvalConfig.from_env(
            {"SWEFORGE_EVAL_MODEL": "m", "SWEFORGE_EVAL_MAX_COST_USD": "lots"}
        )
        assert "is not a number" in config.unavailable_reason()

    def test_cost_ceiling_becomes_a_budget(self):
        from evaluation.live import LiveEvalConfig

        limits = LiveEvalConfig.from_env(
            {"SWEFORGE_EVAL_MODEL": "m", "SWEFORGE_EVAL_MAX_COST_USD": "0.5"}
        ).budget_limits()
        assert limits.max_estimated_cost_usd == 0.5

    def test_describe_live_availability_is_machine_readable(self):
        from evaluation.live import describe_live_availability

        payload = describe_live_availability({})
        assert payload["track"] == "C_live_model"
        assert payload["available"] is False


class TestExperimentSeparation:
    def test_experiment_b_marks_unavailable_without_upstream(self):
        from evaluation.experiment_b import run_experiment_b

        payload = run_experiment_b(scenario_ids=["billing_validation_first_try"])
        assert payload["experiment"] == "B_system_baseline"
        assert payload["comparable_pairs"] == 0
        assert any("NO head-to-head" in n for n in payload["notes"])

    def test_experiment_b_still_runs_the_sweforge_side(self, monkeypatch):
        monkeypatch.setenv("SWEFORGE_ALLOW_LOCAL_EXEC", "1")
        from evaluation.experiment_b import run_experiment_b

        payload = run_experiment_b(scenario_ids=["billing_validation_first_try"])
        record = payload["records"][0]
        assert record["sweforge"]["available"] is True
        assert record["open_swe"]["available"] is False

    def test_experiment_a_variants_are_all_sweforge(self):
        """Guards against re-conflating the ablation with a system baseline."""
        from evaluation.runner import variant_configs

        configs = variant_configs()
        assert "A_baseline" in configs
        # Every Experiment A variant is a WorkflowConfig, i.e. SWE-Forge.
        assert all(isinstance(c, WorkflowConfig) for c in configs.values())

    def test_experiment_b_records_are_machine_readable(self, monkeypatch):
        monkeypatch.setenv("SWEFORGE_ALLOW_LOCAL_EXEC", "1")
        import json

        from evaluation.experiment_b import run_experiment_b

        payload = run_experiment_b(scenario_ids=["billing_validation_first_try"])
        json.dumps(payload)  # must serialise
        assert "baseline_availability" in payload
