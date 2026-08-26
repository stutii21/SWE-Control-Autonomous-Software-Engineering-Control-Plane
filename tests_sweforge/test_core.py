"""Unit tests for SWE-Forge schemas, state, routing, memory and risk.

No test in this file requires network access or an API key.
"""

import pytest
from pydantic import ValidationError

from agent.sweforge.memory.store import ExperienceStore
from agent.sweforge.routing.model_router import ModelRouter, ModelUsageLedger
from agent.sweforge.schemas import (
    ExecutionMetrics,
    ExperienceRecord,
    FileEdit,
    ReviewFinding,
    ReviewResult,
    RiskScore,
    Subtask,
    TaskPlan,
    VerificationResult,
)
from agent.sweforge.security.risk import ChangeSet, RiskEngine, SecurityScanner
from agent.sweforge.state.graph_state import _merge_metrics, initial_state


# ==========================================================================
# TaskPlan
# ==========================================================================
def _subtask(sid: str, deps: list[str] | None = None) -> Subtask:
    return Subtask(
        id=sid, description=f"do {sid}", agent="implementation_agent", depends_on=deps or []
    )


class TestTaskPlan:
    def test_requires_at_least_one_subtask(self):
        with pytest.raises(ValidationError):
            TaskPlan(complexity="simple", subtasks=[])

    def test_rejects_duplicate_ids(self):
        with pytest.raises(ValidationError, match="duplicate subtask ids"):
            TaskPlan(complexity="simple", subtasks=[_subtask("a"), _subtask("a")])

    def test_rejects_unknown_dependency(self):
        with pytest.raises(ValidationError, match="unknown ids"):
            TaskPlan(complexity="simple", subtasks=[_subtask("a", ["ghost"])])

    def test_rejects_self_dependency(self):
        with pytest.raises(ValidationError, match="depends on itself"):
            Subtask(id="a", description="x", agent="test_agent", depends_on=["a"])

    def test_rejects_dependency_cycle(self):
        with pytest.raises(ValidationError, match="cycle"):
            TaskPlan(
                complexity="moderate",
                subtasks=[_subtask("a", ["b"]), _subtask("b", ["a"])],
            )

    def test_required_agents_derived_from_subtasks(self):
        plan = TaskPlan(
            complexity="simple",
            subtasks=[
                Subtask(id="a", description="code", agent="implementation_agent"),
                Subtask(id="b", description="test", agent="test_agent"),
            ],
        )
        assert "implementation_agent" in plan.required_agents
        assert "test_agent" in plan.required_agents

    def test_execution_layers_group_independent_work(self):
        plan = TaskPlan(
            complexity="moderate",
            subtasks=[
                _subtask("a"),
                _subtask("b"),
                _subtask("c", ["a", "b"]),
            ],
        )
        layers = plan.execution_layers()
        assert [len(layer) for layer in layers] == [2, 1]
        assert {s.id for s in layers[0]} == {"a", "b"}
        assert layers[1][0].id == "c"

    def test_execution_layers_respect_chain_order(self):
        plan = TaskPlan(
            complexity="moderate",
            subtasks=[_subtask("a"), _subtask("b", ["a"]), _subtask("c", ["b"])],
        )
        assert [[s.id for s in layer] for layer in plan.execution_layers()] == [
            ["a"],
            ["b"],
            ["c"],
        ]


# ==========================================================================
# FileEdit path safety
# ==========================================================================
class TestFileEdit:
    @pytest.mark.parametrize("bad", ["/etc/passwd", "../outside.py", "a/../../b.py"])
    def test_rejects_escaping_paths(self, bad):
        with pytest.raises(ValidationError):
            FileEdit(path=bad, content="x")

    def test_accepts_relative_path(self):
        assert FileEdit(path="pkg/mod.py", content="x").path == "pkg/mod.py"

    def test_rejects_blank_path(self):
        with pytest.raises(ValidationError):
            FileEdit(path="   ", content="x")


# ==========================================================================
# ReviewResult reconciliation
# ==========================================================================
class TestReviewResult:
    def test_major_finding_forces_rejection(self):
        """A model that reports a blocker and approves anyway is corrected."""
        review = ReviewResult(
            approved=True,
            findings=[ReviewFinding(severity="major", message="incomplete")],
        )
        assert review.approved is False
        assert review.severity == "major"

    def test_blocker_finding_forces_rejection(self):
        review = ReviewResult(
            approved=True, findings=[ReviewFinding(severity="blocker", message="unsafe")]
        )
        assert review.approved is False
        assert review.blocking_findings

    def test_minor_findings_allow_approval(self):
        review = ReviewResult(
            approved=True, findings=[ReviewFinding(severity="minor", message="nit")]
        )
        assert review.approved is True
        assert review.severity == "minor"

    def test_clean_review_stays_approved(self):
        assert ReviewResult(approved=True).approved is True


# ==========================================================================
# VerificationResult
# ==========================================================================
class TestVerificationResult:
    def test_repairs_inconsistent_counts(self):
        result = VerificationResult(passed=False, tests_run=1, tests_passed=2, tests_failed=3)
        assert result.tests_run == 5

    def test_summary_includes_gates(self):
        result = VerificationResult(
            passed=True, tests_run=3, tests_passed=3, lint_passed=True, typecheck_passed=False
        )
        summary = result.summary()
        assert "3/3" in summary and "lint ok" in summary and "types fail" in summary

    def test_unavailable_gates_omitted_from_summary(self):
        result = VerificationResult(passed=True, tests_run=1, tests_passed=1)
        assert "lint" not in result.summary()


# ==========================================================================
# State reducers
# ==========================================================================
class TestState:
    def test_initial_state_has_every_key(self):
        state = initial_state("task", "repo", "/tmp/repo")
        for key in (
            "task",
            "repository",
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
        ):
            assert key in state

    def test_metrics_merge_is_additive(self):
        left = ExecutionMetrics(model_calls=2, tool_calls=1, estimated_cost_usd=0.5)
        right = ExecutionMetrics(model_calls=3, tool_calls=4, estimated_cost_usd=0.25)
        merged = _merge_metrics(left, right)
        assert merged.model_calls == 5
        assert merged.tool_calls == 5
        assert merged.estimated_cost_usd == 0.75

    def test_metrics_merge_takes_max_for_counters(self):
        merged = _merge_metrics(
            ExecutionMetrics(recovery_attempts=3, wall_time_seconds=9.0),
            ExecutionMetrics(recovery_attempts=1, wall_time_seconds=2.0),
        )
        assert merged.recovery_attempts == 3
        assert merged.wall_time_seconds == 9.0

    def test_metrics_merge_ors_gate_flag(self):
        merged = _merge_metrics(
            ExecutionMetrics(security_gate_triggered=False),
            ExecutionMetrics(security_gate_triggered=True),
        )
        assert merged.security_gate_triggered is True

    def test_metrics_merge_handles_none(self):
        assert _merge_metrics(None, ExecutionMetrics(model_calls=2)).model_calls == 2
        assert _merge_metrics(ExecutionMetrics(model_calls=2), None).model_calls == 2


# ==========================================================================
# Model routing
# ==========================================================================
class TestModelRouter:
    def test_role_tier_defaults(self):
        router = ModelRouter(env={})
        assert router.resolve_tier("planning")[0] == "reasoning"
        assert router.resolve_tier("summarisation")[0] == "fast"
        assert router.resolve_tier("implementation")[0] == "coding"

    def test_trivial_task_deescalates_planning(self):
        router = ModelRouter(env={})
        assert router.resolve_tier("planning", complexity="trivial")[0] == "balanced"

    def test_complex_task_escalates_implementation(self):
        router = ModelRouter(env={})
        assert router.resolve_tier("implementation", complexity="complex")[0] == "reasoning"

    def test_cheap_roles_do_not_escalate_with_complexity(self):
        router = ModelRouter(env={})
        assert router.resolve_tier("summarisation", complexity="complex")[0] == "fast"

    def test_latency_sensitivity_caps_tier(self):
        router = ModelRouter(env={})
        tier, reason = router.resolve_tier("review", latency_sensitive=True)
        assert tier == "balanced"
        assert "latency" in reason

    def test_env_var_overrides_model_id(self):
        router = ModelRouter(env={"SWEFORGE_MODEL_FAST": "openai:gpt-test"})
        assert router.select("summarisation").spec.model_id == "openai:gpt-test"

    def test_no_api_key_is_read_from_env(self):
        """The router must never surface a credential in its decisions."""
        router = ModelRouter(env={"ANTHROPIC_API_KEY": "secret-value"})
        decision = router.select("planning")
        assert "secret-value" not in decision.spec.model_id
        assert "secret-value" not in decision.spec.reason

    def test_repeated_failures_escalate_tier(self):
        router = ModelRouter(env={})
        spec = router.select("failure_analysis").spec
        for _ in range(2):
            with pytest.raises(RuntimeError):
                with router.track("n", spec):
                    raise RuntimeError("boom")
        assert router.resolve_tier("failure_analysis")[0] == "reasoning"

    def test_cost_estimation_uses_price_table(self):
        router = ModelRouter(env={"SWEFORGE_PRICE_FAST": "2.0,4.0"})
        assert router.estimate_cost("fast", 1_000_000, 0) == 2.0
        assert router.estimate_cost("fast", 0, 1_000_000) == 4.0

    def test_malformed_price_falls_back_to_default(self):
        router = ModelRouter(env={"SWEFORGE_PRICE_FAST": "not-a-price"})
        assert router.price_for("fast") == (1.00, 5.00)

    def test_track_records_ledger_entry(self):
        router = ModelRouter(env={})
        spec = router.select("planning").spec
        with router.track("planning", spec) as usage:
            usage["input_tokens"] = 1000
            usage["output_tokens"] = 500
        assert router.ledger.total_calls == 1
        record = router.ledger.records[0]
        assert record.ok and record.input_tokens == 1000
        assert record.estimated_cost_usd > 0

    def test_ledger_summary_counts_failures(self):
        ledger = ModelUsageLedger()
        router = ModelRouter(env={}, ledger=ledger)
        spec = router.select("planning").spec
        with pytest.raises(ValueError):
            with router.track("planning", spec):
                raise ValueError("nope")
        assert ledger.summary()["failures"] == 1


# ==========================================================================
# Experience memory
# ==========================================================================
def _record(task: str, status: str = "completed", repo: str = "acme/app") -> ExperienceRecord:
    return ExperienceRecord(
        task=task,
        repository=repo,
        final_status=status,
        relevant_files=["billing.py"],
        lesson="validate inputs early",
    )


class TestExperienceStore:
    def test_roundtrip_persistence(self, tmp_path):
        path = tmp_path / "exp.jsonl"
        store = ExperienceStore(path)
        store.add(_record("fix invoice tax rounding"))
        assert len(ExperienceStore(path)) == 1

    def test_retrieval_ranks_lexical_overlap(self, tmp_path):
        store = ExperienceStore(tmp_path / "exp.jsonl")
        store.add(_record("fix invoice tax rounding in billing"))
        store.add(_record("update frontend button colour"))
        hits = store.retrieve("invoice tax rounding bug", limit=2)
        assert hits and "invoice" in hits[0].record.task

    def test_retrieval_returns_empty_for_unrelated_query(self, tmp_path):
        store = ExperienceStore(tmp_path / "exp.jsonl")
        store.add(_record("fix invoice tax rounding"))
        assert store.retrieve("kubernetes ingress certificate") == []

    def test_successful_only_filter(self, tmp_path):
        store = ExperienceStore(tmp_path / "exp.jsonl")
        store.add(_record("invoice rounding", status="failed"))
        assert store.retrieve("invoice rounding", successful_only=True) == []
        assert store.retrieve("invoice rounding", successful_only=False)

    def test_same_repository_is_boosted(self, tmp_path):
        store = ExperienceStore(tmp_path / "exp.jsonl")
        store.add(_record("invoice rounding fix", repo="other/repo"))
        store.add(_record("invoice rounding fix", repo="acme/app"))
        hits = store.retrieve("invoice rounding fix", repository="acme/app", limit=2)
        assert hits[0].record.repository == "acme/app"

    def test_corrupt_line_is_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "exp.jsonl"
        store = ExperienceStore(path)
        store.add(_record("valid entry about billing"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        reloaded = ExperienceStore(path)
        assert len(reloaded) == 1

    def test_missing_file_is_empty_not_error(self, tmp_path):
        store = ExperienceStore(tmp_path / "nope" / "exp.jsonl")
        assert len(store) == 0
        assert store.retrieve("anything") == []

    def test_render_context_empty_when_nothing_retrieved(self):
        assert ExperienceStore.render_context([]) == ""

    def test_render_context_includes_lesson(self, tmp_path):
        store = ExperienceStore(tmp_path / "exp.jsonl")
        store.add(_record("invoice rounding"))
        rendered = ExperienceStore.render_context(store.retrieve("invoice rounding"))
        assert "validate inputs early" in rendered


# ==========================================================================
# Security scanning and risk scoring
# ==========================================================================
class TestSecurityScanner:
    def test_detects_private_key(self):
        findings = SecurityScanner().scan(
            ChangeSet(files={"k.pem": "-----BEGIN RSA PRIVATE KEY-----\nabc\n"})
        )
        assert any(f.rule == "private_key" and f.severity == "blocker" for f in findings)

    def test_detects_destructive_shell(self):
        findings = SecurityScanner().scan(ChangeSet(files={"clean.sh": "rm -rf /var/data\n"}))
        assert any(f.rule == "destructive_shell" for f in findings)

    def test_detects_disabled_tls(self):
        findings = SecurityScanner().scan(
            ChangeSet(files={"http.py": "requests.get(url, verify=False)\n"})
        )
        assert any(f.rule == "verify_disabled" for f in findings)

    def test_detects_auth_returning_true(self):
        code = "def check_permission(user):\n    return True\n"
        findings = SecurityScanner().scan(ChangeSet(files={"auth.py": code}))
        assert any(f.rule == "auth_weakened" for f in findings)

    def test_example_files_downgrade_secret_severity(self):
        """.env.example legitimately contains placeholder credentials."""
        content = 'AWS_KEY = "AKIA' + "A" * 16 + '"\n'
        findings = SecurityScanner().scan(ChangeSet(files={".env.example": content}))
        assert findings
        assert all(f.severity == "info" for f in findings if f.rule == "aws_access_key")

    def test_clean_file_yields_no_findings(self):
        code = "def add(a, b):\n    return a + b\n"
        assert SecurityScanner().scan(ChangeSet(files={"m.py": code})) == []

    def test_binary_files_are_skipped(self):
        assert SecurityScanner().scan(ChangeSet(files={"logo.png": "AKIA" + "A" * 16})) == []


class TestRiskEngine:
    def test_clean_verified_change_is_low(self):
        score = RiskEngine().assess(
            ChangeSet(files={"m.py": "def f():\n    return 1\n"}),
            verification=VerificationResult(passed=True, tests_run=2, tests_passed=2),
        )
        assert score.level == "LOW"
        assert not score.requires_human_approval

    def test_ci_workflow_edit_raises_risk(self):
        score = RiskEngine().assess(
            ChangeSet(files={".github/workflows/ci.yml": "on: [push]\n"}),
            verification=VerificationResult(passed=True, tests_run=1, tests_passed=1),
        )
        assert any(f.code == "sensitive_ci_workflow" for f in score.factors)
        assert score.level in {"MEDIUM", "HIGH"}

    def test_committed_secret_forces_high(self):
        content = 'TOKEN = "ghp_' + "A" * 36 + '"\n'
        score = RiskEngine().assess(
            ChangeSet(files={"deploy.py": content}),
            verification=VerificationResult(passed=True, tests_run=1, tests_passed=1),
        )
        assert score.level == "HIGH"
        assert score.requires_human_approval

    def test_failed_verification_raises_risk(self):
        score = RiskEngine().assess(
            ChangeSet(files={"m.py": "x = 1\n"}),
            verification=VerificationResult(passed=False, tests_run=1, tests_failed=1),
        )
        assert any(f.code == "verification_failed" for f in score.factors)

    def test_unverified_change_is_penalised(self):
        score = RiskEngine().assess(ChangeSet(files={"m.py": "x = 1\n"}), verification=None)
        assert any(f.code == "unverified" for f in score.factors)

    def test_review_rejection_contributes(self):
        score = RiskEngine().assess(
            ChangeSet(files={"m.py": "x = 1\n"}),
            verification=VerificationResult(passed=True, tests_run=1, tests_passed=1),
            review_rejected=True,
        )
        assert any(f.code == "review_rejected" for f in score.factors)

    def test_score_is_bounded_at_100(self):
        content = "-----BEGIN PRIVATE KEY-----\n" + 'TOKEN="ghp_' + "A" * 36 + '"\nrm -rf /\n'
        score = RiskEngine().assess(
            ChangeSet(files={".github/workflows/ci.yml": content, ".env": content}),
            verification=VerificationResult(passed=False, tests_failed=9, tests_run=9),
        )
        assert score.score == 100
        assert score.level == "HIGH"

    def test_scoring_is_deterministic(self):
        changes = ChangeSet(files={"auth.py": "def f():\n    return 1\n"})
        engine = RiskEngine()
        first = engine.assess(changes, verification=VerificationResult(passed=True))
        second = engine.assess(changes, verification=VerificationResult(passed=True))
        assert first.score == second.score
        assert [f.code for f in first.factors] == [f.code for f in second.factors]

    def test_risk_score_rejects_out_of_range(self):
        with pytest.raises(ValidationError):
            RiskScore(score=140, level="HIGH")
