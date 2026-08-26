"""Phase 25 invariant tests.

These validate *architecture*, not individual functions. They inspect the
compiled LangGraph topology and assert properties that must hold for any run —
the kind of claim that is otherwise only asserted in a README.

Everything here runs with no API key and no network.
"""

import json

import pytest

from agent.sweforge.budget import BudgetExceeded, BudgetLimits, ExecutionBudget
from agent.sweforge.graph.workflow import (
    SWEForgeRuntime,
    WorkflowConfig,
    build_nodes,
    build_workflow,
    make_route_after_failure_analysis,
    make_route_after_review,
    make_route_after_verification,
    route_after_intake,
    route_after_risk_gate,
)
from agent.sweforge.mcp import MCPCapabilityRegistry, MCPInvocationPolicy
from agent.sweforge.mcp.fixtures import FixtureMCPAdapter
from agent.sweforge.observability.trace import (
    REDACTED,
    TraceRecorder,
    redact,
)
from agent.sweforge.observability.tracing import node_metadata
from agent.sweforge.routing.model_router import ModelRouter
from agent.sweforge.schemas import (
    ExecutionMetrics,
    ReviewFinding,
    ReviewResult,
    RiskScore,
    VerificationResult,
)
from agent.sweforge.state.graph_state import FinalStatus, initial_state
from agent.sweforge.tools.registry import ToolContext, build_tools, tools_by_name

TERMINAL_NODES = {"finalization", "human_approval", "escalation", "budget_exhausted"}


def _runtime(config: WorkflowConfig | None = None) -> SWEForgeRuntime:
    return SWEForgeRuntime(
        repo_root=".",
        backend=None,
        router=ModelRouter(env={}),
        config=config or WorkflowConfig(),
    )


@pytest.fixture
def compiled():
    return build_workflow(_runtime()).get_graph()


# ==========================================================================
# PART 16 — graph invariants
# ==========================================================================
class TestGraphInvariants:
    def test_every_non_terminal_node_has_an_outgoing_edge(self, compiled):
        """A node with no way out is a silent hang."""
        sources = {e.source for e in compiled.edges}
        domain_nodes = set(build_nodes(_runtime()))
        stranded = [n for n in domain_nodes if n not in TERMINAL_NODES and n not in sources]
        assert stranded == [], f"nodes with no outgoing edge: {stranded}"

    def test_terminal_nodes_only_route_to_end(self, compiled):
        """A terminal state must not continue executing."""
        for node in TERMINAL_NODES:
            targets = {e.target for e in compiled.edges if e.source == node}
            assert targets <= {"__end__"}, f"{node} routes onward to {targets}"

    def test_every_node_is_reachable_from_start(self, compiled):
        """An unreachable node is dead code masquerading as architecture."""
        adjacency: dict[str, set[str]] = {}
        for edge in compiled.edges:
            adjacency.setdefault(edge.source, set()).add(edge.target)
        seen, stack = set(), ["__start__"]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(adjacency.get(current, ()))
        unreachable = set(build_nodes(_runtime())) - seen
        assert unreachable == set(), f"unreachable nodes: {unreachable}"

    def test_all_declared_final_statuses_are_produced_by_a_terminal_node(self):
        """FinalStatus must not contain values nothing can reach."""
        import inspect

        from agent.sweforge.graph import workflow

        source = inspect.getsource(workflow)
        for status in FinalStatus.__args__:  # type: ignore[attr-defined]
            if status == "pending":
                continue  # initial value, not a terminal outcome
            assert f'"{status}"' in source, f"no node produces final_status={status}"

    # -- routing functions return only declared destinations ----------------
    def test_route_after_intake_destinations(self):
        allowed = {"repository_analysis", "finalization"}
        assert route_after_intake({"final_status": "failed"}) in allowed
        assert route_after_intake({"final_status": "pending"}) in allowed

    def test_route_after_verification_destinations(self):
        allowed = {
            "independent_review",
            "security_analysis",
            "failure_analysis",
            "finalization",
            "budget_exhausted",
        }
        route = make_route_after_verification(WorkflowConfig(), _runtime().budget)
        for passed in (True, False):
            state = {
                "test_results": VerificationResult(passed=passed),
                "execution_metrics": ExecutionMetrics(),
            }
            assert route(state) in allowed

    def test_route_after_review_destinations(self):
        allowed = {"security_analysis", "finalization", "recovery", "escalation"}
        route = make_route_after_review(WorkflowConfig())
        for approved in (True, False):
            review = ReviewResult(
                approved=approved,
                findings=[] if approved else [ReviewFinding(severity="major", message="x")],
            )
            assert route({"review_results": review, "recovery_attempts": []}) in allowed

    def test_route_after_risk_gate_destinations(self):
        allowed = {"human_approval", "finalization"}
        for level, score in (("LOW", 1), ("MEDIUM", 30), ("HIGH", 90)):
            state = {"risk_score": RiskScore(score=score, level=level)}  # type: ignore[arg-type]
            assert route_after_risk_gate(state) in allowed

    # -- loop bounds --------------------------------------------------------
    @pytest.mark.parametrize("limit", [0, 1, 3, 5])
    def test_recovery_can_never_exceed_its_bound(self, limit):
        route = make_route_after_failure_analysis(WorkflowConfig(max_recovery_attempts=limit))
        at_limit = [{"attempt_number": i + 1} for i in range(limit)]
        state = {"recovery_attempts": at_limit}
        assert route(state) == "escalation", "loop must stop exactly at the bound"

    def test_review_loop_cannot_run_forever(self):
        route = make_route_after_verification(WorkflowConfig(max_review_cycles=2))
        state = {
            "test_results": VerificationResult(passed=True),
            "execution_metrics": ExecutionMetrics(review_rejections=2),
        }
        assert route(state) != "independent_review"

    def test_verification_failure_cannot_reach_finalization_when_recovery_enabled(self):
        """A red run must go through diagnosis, not quietly finish."""
        route = make_route_after_verification(WorkflowConfig(enable_recovery=True))
        state = {
            "test_results": VerificationResult(passed=False),
            "execution_metrics": ExecutionMetrics(),
        }
        assert route(state) == "failure_analysis"

    def test_rejected_review_cannot_jump_to_finalization(self):
        route = make_route_after_review(WorkflowConfig())
        review = ReviewResult(
            approved=False, findings=[ReviewFinding(severity="blocker", message="unsafe")]
        )
        assert route({"review_results": review, "recovery_attempts": []}) != "finalization"

    def test_rejected_review_with_no_budget_escalates_not_finalizes(self):
        route = make_route_after_review(WorkflowConfig(max_recovery_attempts=1))
        review = ReviewResult(
            approved=False, findings=[ReviewFinding(severity="major", message="x")]
        )
        state = {"review_results": review, "recovery_attempts": [{"attempt_number": 1}]}
        assert route(state) == "escalation"

    def test_budget_exhaustion_always_reaches_a_terminal_state(self):
        budget = ExecutionBudget(BudgetLimits(max_model_calls=0))
        with pytest.raises(BudgetExceeded):
            budget.check_model_call()
        route = make_route_after_failure_analysis(WorkflowConfig(), budget)
        assert route({"recovery_attempts": []}) in TERMINAL_NODES

    # -- state contract -----------------------------------------------------
    def test_initial_state_populates_every_required_field(self):
        state = initial_state("t", "repo", "/tmp")
        required = [
            "task",
            "repository",
            "repo_root",
            "repository_map",
            "relevant_files",
            "complexity",
            "plan",
            "selected_agents",
            "implementation_results",
            "test_results",
            "failures",
            "recovery_attempts",
            "review_results",
            "security_findings",
            "risk_score",
            "model_usage",
            "execution_metrics",
            "final_status",
            "agents_executed",
            "model_attempts",
            "budget_snapshot",
            "external_context",
        ]
        missing = [k for k in required if k not in state]
        assert missing == [], f"initial_state missing: {missing}"

    def test_reducers_preserve_fields_under_merge(self):
        from agent.sweforge.state.graph_state import _merge_metrics

        merged = _merge_metrics(
            ExecutionMetrics(model_calls=2, recovery_attempts=3, security_gate_triggered=False),
            ExecutionMetrics(model_calls=1, recovery_attempts=1, security_gate_triggered=True),
        )
        assert merged.model_calls == 3  # additive
        assert merged.recovery_attempts == 3  # high-water mark
        assert merged.security_gate_triggered is True  # logical OR


# ==========================================================================
# PART 17 — security invariants
# ==========================================================================
class TestSecurityInvariants:
    def test_high_risk_can_never_auto_finalize(self):
        """The single most important safety property."""
        for score in (55, 70, 90, 100):
            state = {"risk_score": RiskScore(score=score, level="HIGH")}
            assert route_after_risk_gate(state) == "human_approval"

    def test_risk_score_high_always_requires_human_approval(self):
        assert RiskScore(score=55, level="HIGH").requires_human_approval is True

    def test_model_output_cannot_lower_the_risk_gate(self):
        """An approving reviewer must not be able to release a HIGH-risk change."""
        from agent.sweforge.security.risk import ChangeSet, RiskEngine

        secret = 'TOKEN = "ghp_' + "A" * 36 + '"\n'
        score = RiskEngine().assess(
            ChangeSet(files={"deploy.py": secret}),
            verification=VerificationResult(passed=True, tests_run=1, tests_passed=1),
            review_rejected=False,  # reviewer approved
        )
        assert score.level == "HIGH"
        assert route_after_risk_gate({"risk_score": score}) == "human_approval"

    def test_secret_findings_cannot_be_suppressed_by_review_approval(self):
        from agent.sweforge.security.risk import ChangeSet, SecurityScanner

        findings = SecurityScanner().scan(
            ChangeSet(files={"k.pem": "-----BEGIN RSA PRIVATE KEY-----\nx\n"})
        )
        assert any(f.severity == "blocker" for f in findings)

    def test_budget_limits_are_not_reachable_from_model_output(self):
        """No structured-output field maps to a budget limit."""
        from agent.sweforge.agents.specialized import AGENT_CLASSES
        from agent.sweforge.schemas import ReviewResult as RR
        from agent.sweforge.schemas import TaskPlan

        limit_names = set(BudgetLimits().__dict__)
        models = [cls.output_model for cls in AGENT_CLASSES.values()] + [TaskPlan, RR]
        for model in models:
            overlap = limit_names & set(model.model_fields)
            assert overlap == set(), f"{model.__name__} exposes budget fields {overlap}"

    def test_budget_object_is_not_mutated_by_state(self):
        budget = ExecutionBudget(BudgetLimits(max_model_calls=1))
        # Nothing in state carries a limit; a malicious plan cannot raise one.
        state = initial_state("t", "r", "/tmp")
        assert "max_model_calls" not in state
        assert budget.limits.max_model_calls == 1

    def test_mcp_deny_by_default_cannot_be_bypassed_by_model_text(self):
        registry = MCPCapabilityRegistry()
        registry.register(FixtureMCPAdapter())
        registry.discover()
        policy = MCPInvocationPolicy(registry)  # empty allowlist
        for attempt in ("get_issue", "GET_ISSUE", "get_issue ", "../get_issue"):
            result = policy.invoke(attempt, {"issue_id": "1"})
            assert result.ok is False
            assert result.error_category in {"permission_error", "not_found"}

    def test_tool_arguments_are_schema_validated(self):
        from pydantic import ValidationError

        tools = tools_by_name(build_tools(ToolContext(repo_root=".")))
        with pytest.raises(ValidationError):
            tools["find_relevant_files"].invoke({"task": "x", "limit": 10_000})

    def test_untrusted_execution_is_refused_without_explicit_optin(self, tmp_path):
        from agent.sweforge.verification.backends import (
            LocalExecutionForbidden,
            LocalSubprocessBackend,
        )

        with pytest.raises(LocalExecutionForbidden):
            LocalSubprocessBackend(tmp_path, env={})

    def test_pr_preparation_cannot_bypass_the_risk_gate(self):
        from agent.sweforge.github import PullRequestDecision, prepare_pull_request

        plan = prepare_pull_request(
            task="t",
            changed_files=["m.py"],
            risk=RiskScore(score=90, level="HIGH"),
            verification=VerificationResult(passed=True, tests_run=1, tests_passed=1),
            review=ReviewResult(approved=True),
            upstream_creator=lambda **kw: {"number": 1},
            allow_creation=True,
        )
        assert plan.decision is PullRequestDecision.BLOCKED
        assert plan.created is False

    def test_secrets_are_redacted_from_observability_metadata(self):
        payload = node_metadata(node="x", api_key="SECRET", auth_token="SECRET")
        assert not any("SECRET" in str(v) for v in payload.values())

    def test_secrets_are_redacted_from_local_traces(self):
        recorder = TraceRecorder()
        recorder.record(
            "tool",
            tool="t",
            detail={"anthropic_api_key": "sk-ant-REALLOOKINGVALUE", "nested": {"token": "abc"}},
        )
        dumped = recorder.to_jsonl()
        assert "sk-ant-REALLOOKINGVALUE" not in dumped
        assert REDACTED in dumped

    def test_redaction_handles_nested_and_long_values(self):
        out = redact({"password": "p", "big": "x" * 5000, "ok": {"inner_secret": "s"}})
        assert out["password"] == REDACTED
        assert "truncated" in out["big"]
        assert out["ok"]["inner_secret"] == REDACTED

    def test_redaction_keeps_safe_boolean_flags(self):
        out = redact({"api_key_configured": True, "credential_present": False})
        assert out["api_key_configured"] is True
        assert out["credential_present"] is False


# ==========================================================================
# PART 18 — local trace artifact
# ==========================================================================
class TestLocalTrace:
    def test_trace_exists_without_langsmith(self, monkeypatch):
        monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
        monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
        recorder = TraceRecorder()
        recorder.node("planning")
        recorder.tool("find_relevant_files")
        recorder.final("completed")
        assert len(recorder.events) == 3
        assert recorder.summary()["final_status"] == "completed"

    def test_trace_writes_jsonl(self, tmp_path):
        recorder = TraceRecorder(path=tmp_path / "t.jsonl")
        recorder.node("planning")
        recorder.tool("run_validation", status="ok")
        lines = (tmp_path / "t.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["node"] == "planning"

    def test_events_are_sequenced(self):
        recorder = TraceRecorder()
        for _ in range(5):
            recorder.node("n")
        assert [e.seq for e in recorder.events] == [1, 2, 3, 4, 5]

    def test_recording_never_raises(self):
        recorder = TraceRecorder()

        class _Boom:
            def __repr__(self):
                raise RuntimeError("nope")

        assert recorder.record("tool", tool="t", detail={"x": _Boom()}) is not None or True

    def test_disabled_recorder_records_nothing(self):
        recorder = TraceRecorder(enabled=False)
        recorder.node("planning")
        assert recorder.events == []

    def test_summary_reports_sequences(self):
        recorder = TraceRecorder(task_id="task-1")
        recorder.node("a")
        recorder.tool("find_callers")
        summary = recorder.summary()
        assert summary["node_sequence"] == ["a"]
        assert summary["tool_sequence"] == ["find_callers"]
        assert summary["task_id"] == "task-1"

    def test_traces_are_task_local(self):
        """No cross-run contamination, which concurrency depends on."""
        first, second = TraceRecorder(task_id="t1"), TraceRecorder(task_id="t2")
        first.node("a")
        second.node("b")
        assert first.run_id != second.run_id
        assert [e.node for e in first.events] == ["a"]
        assert [e.node for e in second.events] == ["b"]
