"""Tests for graph orchestration: routing functions, bounded loops, planner, e2e.

These are the tests that matter most: they assert the *control-flow guarantees*
the project claims. Everything uses scripted models, so no API key is needed.
"""

import pytest

from agent.sweforge.agents.roles import (
    Diagnostician,
    ImplementationAgent,
    ImplementationOutput,
    IndependentReviewer,
    RepairOutput,
)
from agent.sweforge.graph.workflow import (
    WorkflowConfig,
    make_route_after_failure_analysis,
    make_route_after_review,
    make_route_after_verification,
    route_after_intake,
    route_after_risk_gate,
)
from agent.sweforge.models.scripted import (
    ScriptedChatModel,
    ScriptedModelFactory,
    ScriptExhausted,
)
from agent.sweforge.planning.planner import TaskPlanner, select_agents
from agent.sweforge.repository.analyzer import RepositoryAnalyzer
from agent.sweforge.repository.graph_index import RepositoryGraph
from agent.sweforge.routing.model_router import ModelRouter
from agent.sweforge.runner import run_task
from agent.sweforge.schemas import (
    ExecutionMetrics,
    FailureDiagnosis,
    FileEdit,
    RecoveryAttempt,
    ReviewFinding,
    ReviewResult,
    RiskScore,
    Subtask,
    TaskPlan,
    VerificationResult,
)


def _plan(complexity: str = "simple", risk: str = "LOW", agent: str = "implementation_agent"):
    return TaskPlan(
        complexity=complexity,  # type: ignore[arg-type]
        relevant_files=["billing.py"],
        subtasks=[Subtask(id="st1", description="do it", agent=agent)],  # type: ignore[arg-type]
        testing_strategy="run targeted tests",
        risk_level=risk,  # type: ignore[arg-type]
    )


def _attempts(n: int) -> list[RecoveryAttempt]:
    return [
        RecoveryAttempt(attempt_number=i + 1, failure_category="test_assertion", diagnosis="d")
        for i in range(n)
    ]


# ==========================================================================
# Scripted model
# ==========================================================================
class TestScriptedModel:
    def test_returns_validated_schema_instance(self):
        model = ScriptedChatModel(role="planning", outputs=[_plan()], calls=[])
        result = model.with_structured_output(TaskPlan).invoke("go")
        assert isinstance(result, TaskPlan)

    def test_validates_dict_payloads(self):
        payload = _plan().model_dump()
        model = ScriptedChatModel(role="planning", outputs=[payload], calls=[])
        assert isinstance(model.with_structured_output(TaskPlan).invoke("go"), TaskPlan)

    def test_advances_through_script(self):
        first, second = _plan("simple"), _plan("complex")
        model = ScriptedChatModel(role="planning", outputs=[first, second], calls=[])
        runnable = model.with_structured_output(TaskPlan)
        assert runnable.invoke("a").complexity == "simple"
        assert runnable.invoke("b").complexity == "complex"

    def test_repeats_last_by_default(self):
        model = ScriptedChatModel(role="planning", outputs=[_plan()], calls=[])
        runnable = model.with_structured_output(TaskPlan)
        runnable.invoke("a")
        assert runnable.invoke("b") is not None

    def test_raises_when_exhausted_and_not_repeating(self):
        model = ScriptedChatModel(role="planning", outputs=[_plan()], repeat_last=False, calls=[])
        runnable = model.with_structured_output(TaskPlan)
        runnable.invoke("a")
        with pytest.raises(ScriptExhausted):
            runnable.invoke("b")

    def test_empty_script_raises(self):
        model = ScriptedChatModel(role="planning", outputs=[], calls=[])
        with pytest.raises(ScriptExhausted):
            model.with_structured_output(TaskPlan).invoke("a")

    def test_factory_reuses_instance_per_role(self):
        factory = ScriptedModelFactory({"planning": [_plan(), _plan("complex")]})

        class _Spec:
            role = "planning"

        first = factory(_Spec())
        second = factory(_Spec())
        assert first is second

    def test_reports_synthetic_usage(self):
        model = ScriptedChatModel(role="planning", outputs=[_plan()], calls=[])
        model.with_structured_output(TaskPlan).invoke("go")
        assert model.last_usage["output_tokens"] > 0


# ==========================================================================
# Routing functions
# ==========================================================================
class TestRouting:
    def test_intake_routes_empty_task_to_terminal(self):
        assert route_after_intake({"final_status": "failed"}) == "finalization"

    def test_intake_routes_valid_task_forward(self):
        assert route_after_intake({"final_status": "pending"}) == "repository_analysis"

    def test_pass_routes_to_review_when_enabled(self):
        route = make_route_after_verification(WorkflowConfig())
        state = {
            "test_results": VerificationResult(passed=True),
            "execution_metrics": ExecutionMetrics(),
        }
        assert route(state) == "independent_review"

    def test_pass_skips_review_when_disabled(self):
        config = WorkflowConfig(enable_review=False)
        route = make_route_after_verification(config)
        assert route({"test_results": VerificationResult(passed=True)}) == "security_analysis"

    def test_pass_goes_straight_to_finalization_in_baseline(self):
        route = make_route_after_verification(WorkflowConfig.baseline())
        assert route({"test_results": VerificationResult(passed=True)}) == "finalization"

    def test_failure_routes_to_analysis_when_recovery_enabled(self):
        route = make_route_after_verification(WorkflowConfig())
        assert route({"test_results": VerificationResult(passed=False)}) == "failure_analysis"

    def test_failure_terminates_in_baseline(self):
        route = make_route_after_verification(WorkflowConfig.baseline())
        assert route({"test_results": VerificationResult(passed=False)}) == "finalization"

    def test_review_budget_exhaustion_skips_further_review(self):
        route = make_route_after_verification(WorkflowConfig(max_review_cycles=1))
        state = {
            "test_results": VerificationResult(passed=True),
            "execution_metrics": ExecutionMetrics(review_rejections=1),
        }
        assert state["execution_metrics"].review_rejections == 1
        assert route(state) == "security_analysis"

    # -- the bounded loop guarantee ------------------------------------------
    @pytest.mark.parametrize(
        "attempts,expected", [(0, "recovery"), (2, "recovery"), (3, "escalation")]
    )
    def test_recovery_is_bounded(self, attempts, expected):
        route = make_route_after_failure_analysis(WorkflowConfig(max_recovery_attempts=3))
        assert route({"recovery_attempts": _attempts(attempts)}) == expected

    def test_recovery_bound_is_configurable(self):
        route = make_route_after_failure_analysis(WorkflowConfig(max_recovery_attempts=1))
        assert route({"recovery_attempts": _attempts(1)}) == "escalation"

    def test_zero_budget_escalates_immediately(self):
        route = make_route_after_failure_analysis(WorkflowConfig(max_recovery_attempts=0))
        assert route({"recovery_attempts": []}) == "escalation"

    def test_approved_review_proceeds(self):
        route = make_route_after_review(WorkflowConfig())
        state = {"review_results": ReviewResult(approved=True), "recovery_attempts": []}
        assert route(state) == "security_analysis"

    def test_rejected_review_routes_to_recovery(self):
        route = make_route_after_review(WorkflowConfig())
        review = ReviewResult(
            approved=False, findings=[ReviewFinding(severity="major", message="incomplete")]
        )
        assert route({"review_results": review, "recovery_attempts": []}) == "recovery"

    def test_rejected_review_escalates_when_budget_spent(self):
        route = make_route_after_review(WorkflowConfig(max_recovery_attempts=2))
        review = ReviewResult(
            approved=False, findings=[ReviewFinding(severity="major", message="bad")]
        )
        state = {"review_results": review, "recovery_attempts": _attempts(2)}
        assert route(state) == "escalation"

    def test_high_risk_requires_human(self):
        state = {"risk_score": RiskScore(score=80, level="HIGH")}
        assert route_after_risk_gate(state) == "human_approval"

    @pytest.mark.parametrize("level,score", [("LOW", 5), ("MEDIUM", 35)])
    def test_non_high_risk_finalizes(self, level, score):
        state = {"risk_score": RiskScore(score=score, level=level)}
        assert route_after_risk_gate(state) == "finalization"

    def test_missing_risk_score_finalizes(self):
        assert route_after_risk_gate({}) == "finalization"


# ==========================================================================
# Planner
# ==========================================================================
@pytest.fixture
def planner_graph(tmp_path):
    (tmp_path / "billing.py").write_text(
        '"""Invoice billing."""\n\n\ndef invoice_total(subtotal):\n    return subtotal\n'
    )
    (tmp_path / "test_billing.py").write_text("def test_x():\n    assert True\n")
    return RepositoryGraph(RepositoryAnalyzer().analyze(tmp_path))


class TestPlanner:
    def test_produces_structured_plan(self, planner_graph):
        router = ModelRouter(env={}, model_factory=ScriptedModelFactory({"planning": [_plan()]}))
        planner = TaskPlanner(router=router, graph=planner_graph)
        plan, evidence, reason = planner.plan("fix invoice total", "acme/app")
        assert isinstance(plan, TaskPlan)
        assert evidence.file_count >= 2
        assert "planning" in reason

    def test_evidence_ranks_real_files(self, planner_graph):
        router = ModelRouter(env={}, model_factory=ScriptedModelFactory({"planning": [_plan()]}))
        planner = TaskPlanner(router=router, graph=planner_graph)
        evidence = planner.gather_evidence("invoice total calculation")
        assert evidence.candidate_files
        assert evidence.candidate_files[0]["path"] == "billing.py"

    def test_evidence_render_lists_candidates(self, planner_graph):
        router = ModelRouter(env={}, model_factory=ScriptedModelFactory({"planning": [_plan()]}))
        planner = TaskPlanner(router=router, graph=planner_graph)
        rendered = planner.gather_evidence("invoice total").render()
        assert "billing.py" in rendered

    def test_hallucinated_paths_are_filtered(self, planner_graph):
        bad = TaskPlan(
            complexity="simple",
            relevant_files=["src/utils/imaginary.py"],
            subtasks=[
                Subtask(
                    id="st1",
                    description="edit",
                    agent="implementation_agent",
                    target_files=["src/utils/imaginary.py"],
                )
            ],
        )
        router = ModelRouter(env={}, model_factory=ScriptedModelFactory({"planning": [bad]}))
        planner = TaskPlanner(router=router, graph=planner_graph)
        plan, _, _ = planner.plan("invoice total", "acme/app")
        assert "src/utils/imaginary.py" not in plan.relevant_files
        assert plan.relevant_files  # replaced with real candidates

    def test_fallback_plan_when_structured_output_fails(self, planner_graph):
        """A planner failure must degrade, not kill the run."""
        router = ModelRouter(env={}, model_factory=ScriptedModelFactory({"planning": []}))
        planner = TaskPlanner(router=router, graph=planner_graph)
        plan, _, _ = planner.plan("invoice total", "acme/app")
        assert isinstance(plan, TaskPlan)
        assert "fallback" in plan.rationale.lower()

    @pytest.mark.parametrize(
        "task,expected",
        [
            ("fix a typo in the docstring", "trivial"),
            ("refactor authentication across all modules and migrate the schema", "complex"),
        ],
    )
    def test_complexity_prior(self, planner_graph, task, expected):
        router = ModelRouter(env={}, model_factory=ScriptedModelFactory({"planning": [_plan()]}))
        planner = TaskPlanner(router=router, graph=planner_graph)
        evidence = planner.gather_evidence(task)
        assert planner.estimate_complexity(task, evidence) == expected

    def test_agent_selection_adds_reviewer(self):
        roster = select_agents(_plan(), always_review=True)
        assert "reviewer_agent" in roster

    def test_agent_selection_respects_disabled_review(self):
        roster = select_agents(_plan(), always_review=False)
        assert "reviewer_agent" not in roster

    def test_agent_selection_adds_security_for_high_risk(self):
        roster = select_agents(_plan(risk="HIGH"))
        assert "security_agent" in roster

    def test_agent_selection_adds_security_for_complex(self):
        roster = select_agents(_plan(complexity="complex"))
        assert "security_agent" in roster

    def test_agent_selection_is_not_fixed(self):
        """Different plans must yield different rosters."""
        simple = select_agents(_plan())
        risky = select_agents(_plan(complexity="complex", risk="HIGH"))
        assert set(simple) != set(risky)

    def test_frontend_agent_only_when_planned(self):
        roster = select_agents(_plan(agent="frontend_agent"))
        assert "frontend_agent" in roster
        assert "database_agent" not in roster


# ==========================================================================
# Agents
# ==========================================================================
class _MemBackend:
    name = "mem"

    def __init__(self, files=None):
        self.files = dict(files or {})

    def read_file(self, path):
        return self.files[path]

    def write_file(self, path, content):
        self.files[path] = content

    def run(self, command, *, timeout=300):
        raise AssertionError("agents must not execute commands directly")


class TestAgents:
    def test_implementation_returns_edits(self):
        output = ImplementationOutput(
            edits=[FileEdit(path="billing.py", content="x = 1\n")], notes="done"
        )
        router = ModelRouter(
            env={}, model_factory=ScriptedModelFactory({"implementation": [output]})
        )
        agent = ImplementationAgent(router=router, backend=_MemBackend({"billing.py": "old"}))
        result = agent.run(
            Subtask(id="st1", description="d", agent="implementation_agent"), _plan()
        )
        assert result.succeeded and result.touched_files == ["billing.py"]

    def test_implementation_failure_is_captured(self):
        router = ModelRouter(env={}, model_factory=ScriptedModelFactory({"implementation": []}))
        agent = ImplementationAgent(router=router, backend=_MemBackend())
        result = agent.run(
            Subtask(id="st1", description="d", agent="implementation_agent"), _plan()
        )
        assert result.succeeded is False
        assert "failed" in result.notes

    def test_reviewer_returns_verdict(self):
        router = ModelRouter(
            env={},
            model_factory=ScriptedModelFactory(
                {"review": [ReviewResult(approved=True, summary="ok")]}
            ),
        )
        result = IndependentReviewer(router=router).review(
            task="t",
            plan=_plan(),
            diff="d",
            verification=VerificationResult(passed=True),
            changed_files=["billing.py"],
        )
        assert result.approved

    def test_reviewer_failure_does_not_approve(self):
        """A reviewer that cannot run must never silently approve."""
        router = ModelRouter(env={}, model_factory=ScriptedModelFactory({"review": []}))
        result = IndependentReviewer(router=router).review(
            task="t", plan=_plan(), diff="d", verification=None, changed_files=[]
        )
        assert result.approved is False

    def test_diagnostician_returns_repair(self):
        from agent.sweforge.recovery.classifier import FailureClassifier

        repair = RepairOutput(
            diagnosis=FailureDiagnosis(category="syntax", root_cause="missing colon"),
            edits=[FileEdit(path="billing.py", content="fixed\n")],
            strategy="add colon",
        )
        router = ModelRouter(env={}, model_factory=ScriptedModelFactory({"recovery": [repair]}))
        verification = VerificationResult(passed=False, output="E SyntaxError: x", tests_failed=1)
        classification = FailureClassifier().classify(verification)
        out = Diagnostician(
            router=router, backend=_MemBackend({"billing.py": "b"})
        ).diagnose_and_repair(
            task="t",
            classification=classification,
            verification=verification,
            complexity="simple",
            attempt_number=1,
            previous_strategies=[],
            candidate_files=["billing.py"],
        )
        assert out.edits and out.diagnosis.category == "syntax"

    def test_diagnostician_failure_yields_no_edits(self):
        from agent.sweforge.recovery.classifier import FailureClassifier

        router = ModelRouter(env={}, model_factory=ScriptedModelFactory({"recovery": []}))
        verification = VerificationResult(passed=False, output="E ValueError: x", tests_failed=1)
        out = Diagnostician(router=router, backend=_MemBackend()).diagnose_and_repair(
            task="t",
            classification=FailureClassifier().classify(verification),
            verification=verification,
            complexity="simple",
            attempt_number=1,
            previous_strategies=[],
            candidate_files=[],
        )
        assert out.edits == []
        assert out.diagnosis.confidence == 0.0


# ==========================================================================
# End-to-end graph execution against real fixtures
# ==========================================================================
@pytest.fixture
def fixture_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("SWEFORGE_ALLOW_LOCAL_EXEC", "1")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\npythonpath = .\n")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    return tmp_path


GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a * b\n"


def _script(impl_content: str, repair_content: str | None = None, approve: bool = True):
    script = {
        "planning": [
            TaskPlan(
                complexity="simple",
                relevant_files=["calc.py"],
                subtasks=[Subtask(id="st1", description="fix add", agent="implementation_agent")],
                testing_strategy="run tests",
            )
        ],
        "implementation": [
            ImplementationOutput(edits=[FileEdit(path="calc.py", content=impl_content)])
        ],
        "review": [ReviewResult(approved=approve, summary="scripted")],
    }
    if repair_content is not None:
        script["recovery"] = [
            RepairOutput(
                diagnosis=FailureDiagnosis(category="test_assertion", root_cause="wrong operator"),
                edits=[FileEdit(path="calc.py", content=repair_content)],
                strategy="use addition",
            )
        ]
    return script


class TestEndToEnd:
    def _run(self, repo, script, config=None):
        router = ModelRouter(env={}, model_factory=ScriptedModelFactory(script))
        return run_task(
            task="add() must return the sum of its arguments",
            repo_root=str(repo),
            repository="test/calc",
            config=config or WorkflowConfig(),
            router=router,
            backend_kind="local",
            memory_path=str(repo / ".sweforge" / "exp.jsonl"),
        )

    def test_first_attempt_success(self, fixture_repo):
        outcome = self._run(fixture_repo, _script(GOOD))
        assert outcome.final_status == "completed"
        assert outcome.verification_passed
        assert outcome.recovery_attempts == 0
        assert outcome.metrics()["first_attempt_success"] is True

    def test_edits_are_really_written_to_disk(self, fixture_repo):
        self._run(fixture_repo, _script(GOOD))
        assert (fixture_repo / "calc.py").read_text() == GOOD

    def test_recovery_fixes_failing_tests(self, fixture_repo):
        outcome = self._run(fixture_repo, _script(BAD, repair_content=GOOD))
        assert outcome.verification_passed
        assert outcome.recovery_attempts == 1
        assert outcome.recovered
        assert outcome.metrics()["verification_runs"] == 2

    def test_trace_shows_expected_node_sequence(self, fixture_repo):
        outcome = self._run(fixture_repo, _script(BAD, repair_content=GOOD))
        trace = " -> ".join(outcome.node_trace)
        for expected in (
            "verification(FAIL)",
            "failure_analysis",
            "recovery",
            "verification(PASS)",
        ):
            assert expected in trace

    def test_recovery_loop_terminates_when_repair_never_works(self, fixture_repo):
        """The central safety guarantee: an always-wrong repair still halts."""
        outcome = self._run(
            fixture_repo,
            _script(BAD, repair_content=BAD),
            WorkflowConfig(max_recovery_attempts=3),
        )
        assert outcome.final_status == "escalated_recovery_exhausted"
        assert outcome.recovery_attempts == 3

    def test_recovery_bound_is_respected_at_one(self, fixture_repo):
        outcome = self._run(
            fixture_repo,
            _script(BAD, repair_content=BAD),
            WorkflowConfig(max_recovery_attempts=1),
        )
        assert outcome.recovery_attempts == 1
        assert outcome.final_status == "escalated_recovery_exhausted"

    def test_baseline_variant_does_not_recover(self, fixture_repo):
        outcome = self._run(fixture_repo, _script(BAD), WorkflowConfig.baseline())
        assert outcome.final_status == "failed"
        assert outcome.recovery_attempts == 0

    def test_baseline_executes_fewer_nodes(self, fixture_repo):
        full = self._run(fixture_repo, _script(GOOD))
        baseline = self._run(fixture_repo, _script(GOOD), WorkflowConfig.baseline())
        assert len(baseline.node_trace) < len(full.node_trace)

    def test_review_rejection_triggers_recovery(self, fixture_repo):
        script = _script(GOOD, repair_content=GOOD, approve=False)
        script["review"] = [
            ReviewResult(
                approved=False,
                findings=[ReviewFinding(severity="major", message="incomplete", file="calc.py")],
            ),
            ReviewResult(approved=True, summary="now fine"),
        ]
        outcome = self._run(fixture_repo, script)
        assert outcome.recovery_attempts >= 1
        assert outcome.metrics()["review_rejections"] >= 1

    def test_high_risk_change_awaits_human(self, fixture_repo):
        secret = 'TOKEN = "ghp_' + "A" * 36 + '"\n' + GOOD
        outcome = self._run(fixture_repo, _script(secret))
        assert outcome.final_status == "awaiting_human_approval"
        assert outcome.state["risk_score"].level == "HIGH"

    def test_security_gate_disabled_ships_the_same_change(self, fixture_repo):
        """Demonstrates the gate is what makes the difference, not luck."""
        secret = 'TOKEN = "ghp_' + "A" * 36 + '"\n' + GOOD
        config = WorkflowConfig(enable_security_gate=False)
        outcome = self._run(fixture_repo, _script(secret), config)
        assert outcome.final_status.startswith("completed")

    def test_empty_task_terminates_cleanly(self, fixture_repo):
        router = ModelRouter(env={}, model_factory=ScriptedModelFactory(_script(GOOD)))
        outcome = run_task(
            task="   ",
            repo_root=str(fixture_repo),
            repository="t",
            router=router,
            backend_kind="local",
            memory_path=str(fixture_repo / ".sweforge" / "e.jsonl"),
        )
        assert outcome.final_status == "failed"
        assert outcome.node_trace == ["task_intake", "finalization"]

    def test_metrics_are_populated(self, fixture_repo):
        metrics = self._run(fixture_repo, _script(GOOD)).metrics()
        assert metrics["model_calls"] >= 1
        assert metrics["tool_calls"] >= 1
        assert metrics["wall_time_seconds"] > 0
        assert metrics["tests_run"] >= 1

    def test_experience_is_recorded(self, fixture_repo):
        from agent.sweforge.memory.store import ExperienceStore

        self._run(fixture_repo, _script(GOOD))
        store = ExperienceStore(fixture_repo / ".sweforge" / "exp.jsonl")
        assert len(store) == 1
        assert store.records[0].final_status == "completed"

    def test_run_is_reproducible(self, fixture_repo, tmp_path):
        """Same script, same fixture, same terminal state and node count."""
        first = self._run(fixture_repo, _script(BAD, repair_content=GOOD))
        (fixture_repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        second = self._run(fixture_repo, _script(BAD, repair_content=GOOD))
        assert first.final_status == second.final_status
        assert len(first.node_trace) == len(second.node_trace)
