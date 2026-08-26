"""Tests for evaluation metrics aggregation and LangSmith observability config."""

from agent.sweforge.observability.tracing import (
    describe_configuration,
    project_name,
    run_metadata,
    trace_run,
    tracing_enabled,
)
from evaluation.evaluator import Report
from evaluation.metrics import aggregate, expectation_check
from evaluation.runner import variant_configs
from evaluation.scenarios import all_scenarios, scenario_by_id


def _record(
    scenario_id="s1",
    variant="E_full",
    status="completed",
    available=True,
    **metrics,
):
    payload = {
        "verification_passed": True,
        "first_attempt_success": True,
        "recovery_attempts": 0,
        "tests_run": 2,
        "tests_passed": 2,
        "tests_failed": 0,
        "model_calls": 3,
        "tool_calls": 5,
        "wall_time_seconds": 1.0,
        "verification_runs": 1,
        "review_approved": True,
        "review_rejections": 0,
        "security_findings": 0,
        "security_gate_triggered": False,
        "risk_level": "LOW",
        "node_count": 11,
        "estimated_cost_usd": 0.01,
        "input_tokens": 100,
        "output_tokens": 50,
    }
    payload.update(metrics)
    return {
        "scenario_id": scenario_id,
        "variant": variant,
        "status": status,
        "available": available,
        "error": None if available else "fixture missing",
        "metrics": payload if available else {},
        "node_trace": [],
        "expectations": {
            "expected_status": "completed",
            "expected_verification": True,
            "expects_recovery": False,
            "expects_review_rejection": False,
            "expects_high_risk": False,
        },
        "tags": [],
    }


class TestMetrics:
    def test_counts_available_runs(self):
        metrics = aggregate([_record(), _record(scenario_id="s2")])["E_full"]
        assert metrics.runs_available == 2
        assert metrics.task_success_rate == 1.0

    def test_unavailable_excluded_from_rates(self):
        """An infrastructure gap must not be scored as a task failure."""
        metrics = aggregate([_record(), _record(scenario_id="s2", available=False)])["E_full"]
        assert metrics.runs_attempted == 2
        assert metrics.runs_available == 1
        assert metrics.runs_unavailable == 1
        assert metrics.task_success_rate == 1.0
        assert metrics.unavailable_reasons

    def test_undefined_rate_is_none_not_zero(self):
        """Reporting 0% for a rate with no denominator would be misleading."""
        metrics = aggregate([_record()])["E_full"]
        assert metrics.recovery_success_rate is None
        assert metrics.avg_recovery_attempts is None

    def test_recovery_rates_computed_over_recovery_runs_only(self):
        records = [
            _record(scenario_id="a", recovery_attempts=0),
            _record(scenario_id="b", recovery_attempts=2, verification_passed=True),
            _record(
                scenario_id="c",
                recovery_attempts=3,
                verification_passed=False,
                status="escalated_recovery_exhausted",
            ),
        ]
        metrics = aggregate(records)["E_full"]
        assert metrics.runs_entering_recovery == 2
        assert metrics.recovery_success_rate == 0.5
        assert metrics.avg_recovery_attempts == 2.5

    def test_human_approval_is_not_counted_as_success(self):
        metrics = aggregate([_record(status="awaiting_human_approval")])["E_full"]
        assert metrics.awaiting_human_approval == 1
        assert metrics.task_success_rate == 0.0

    def test_escalation_counted_separately(self):
        metrics = aggregate([_record(status="escalated_recovery_exhausted")])["E_full"]
        assert metrics.escalated == 1

    def test_completed_with_findings_counts_as_success(self):
        metrics = aggregate([_record(status="completed_with_findings")])["E_full"]
        assert metrics.task_success_rate == 1.0

    def test_review_interventions_counted_even_when_resolved(self):
        metrics = aggregate([_record(review_rejections=1, review_approved=True)])["E_full"]
        assert metrics.review_rejections == 1

    def test_security_gate_interventions_counted(self):
        metrics = aggregate([_record(security_gate_triggered=True)])["E_full"]
        assert metrics.security_gate_interventions == 1

    def test_test_pass_rate(self):
        metrics = aggregate([_record(tests_run=4, tests_passed=3, tests_failed=1)])["E_full"]
        assert metrics.test_pass_rate == 0.75

    def test_variants_are_isolated(self):
        grouped = aggregate([_record(variant="A_baseline"), _record(variant="E_full")])
        assert set(grouped) == {"A_baseline", "E_full"}

    def test_to_dict_labels_synthetic_fields(self):
        payload = aggregate([_record()])["E_full"].to_dict()
        assert "estimated_cost_usd_synthetic" in payload
        assert "input_tokens_synthetic" in payload

    def test_expectation_check_only_scores_full_variant(self):
        result = expectation_check([_record(variant="A_baseline", status="failed")])
        assert result["checked"] == 0

    def test_expectation_check_detects_mismatch(self):
        result = expectation_check([_record(status="failed", verification_passed=False)])
        assert result["checked"] == 1
        assert result["passed"] == 0

    def test_expectation_check_passes_on_match(self):
        result = expectation_check([_record()])
        assert result["passed"] == 1
        assert result["rate"] == 1.0


class TestReport:
    def _payload(self, records):
        return {
            "generated_at": "2026-01-01T00:00:00Z",
            "duration_seconds": 1.0,
            "model_mode": "scripted-deterministic",
            "notes": ["note one"],
            "variants": sorted({r["variant"] for r in records}),
            "scenarios": sorted({r["scenario_id"] for r in records}),
            "records": records,
        }

    def _report(self, records):
        return Report(
            payload=self._payload(records),
            variants=aggregate(records),
            expectations=expectation_check(records),
        )

    def test_markdown_renders_sections(self):
        markdown = self._report([_record()]).markdown()
        for heading in (
            "# SWE-Forge Evaluation Report",
            "## Ablation results",
            "## Graph routing correctness",
            "## Unavailable runs",
        ):
            assert heading in markdown

    def test_markdown_labels_synthetic_cost(self):
        markdown = self._report([_record()]).markdown()
        assert "synthetic" in markdown.lower()

    def test_undefined_rates_render_as_na(self):
        markdown = self._report([_record()]).markdown()
        assert "n/a" in markdown

    def test_unavailable_runs_are_reported(self):
        markdown = self._report([_record(available=False)]).markdown()
        assert "unavailable" in markdown.lower()

    def test_csv_written(self, tmp_path):
        path = self._report([_record()]).write_csv(tmp_path / "v.csv")
        assert path.exists() and "variant" in path.read_text()

    def test_run_csv_written(self, tmp_path):
        path = self._report([_record()]).write_run_csv(tmp_path / "r.csv")
        assert path.exists() and "scenario_id" in path.read_text()

    def test_json_written(self, tmp_path):
        import json

        path = self._report([_record()]).write_json(tmp_path / "s.json")
        assert json.loads(path.read_text())["variants"]


class TestScenarios:
    def test_scenarios_have_unique_ids(self):
        ids = [s.id for s in all_scenarios()]
        assert len(ids) == len(set(ids))

    def test_every_scenario_declares_expectations(self):
        for scenario in all_scenarios():
            assert scenario.expected_status
            assert scenario.task and scenario.fixture
            assert scenario.script.get("planning")

    def test_lookup_by_id(self):
        assert scenario_by_id("billing_validation_first_try").fixture == "billing"

    def test_unknown_id_raises(self):
        import pytest

        with pytest.raises(KeyError):
            scenario_by_id("nope")

    def test_scenarios_cover_every_terminal_state(self):
        statuses = {s.expected_status for s in all_scenarios()}
        assert "completed" in statuses
        assert "escalated_recovery_exhausted" in statuses
        assert "awaiting_human_approval" in statuses

    def test_variants_are_cumulative(self):
        configs = variant_configs()
        assert configs["A_baseline"].enable_recovery is False
        assert configs["C_recovery"].enable_recovery is True
        assert configs["D_reviewer"].enable_review is True
        assert configs["E_full"].enable_security_gate is True

    def test_baseline_disables_everything(self):
        config = variant_configs()["A_baseline"]
        assert not any(
            [
                config.enable_repository_intelligence,
                config.enable_recovery,
                config.enable_review,
                config.enable_security_gate,
                config.enable_memory,
            ]
        )

    def test_evaluation_runs_sequentially_for_reproducibility(self):
        for config in variant_configs().values():
            assert config.subtask_workers == 1


class TestObservability:
    def test_disabled_without_env(self):
        assert tracing_enabled({}) is False

    def test_disabled_without_api_key(self):
        assert tracing_enabled({"LANGSMITH_TRACING": "true"}) is False

    def test_disabled_when_switched_off(self):
        assert tracing_enabled({"LANGSMITH_API_KEY": "k"}) is False

    def test_enabled_with_both(self):
        assert tracing_enabled({"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "k"}) is True

    def test_legacy_variable_supported(self):
        assert tracing_enabled({"LANGCHAIN_TRACING_V2": "1", "LANGCHAIN_API_KEY": "k"}) is True

    def test_project_name_default(self):
        assert project_name({}) == "sweforge"

    def test_project_name_override(self):
        assert project_name({"LANGSMITH_PROJECT": "custom"}) == "custom"

    def test_trace_run_is_noop_when_disabled(self):
        with trace_run(name="x", metadata={"a": 1}):
            pass  # must not raise

    def test_describe_configuration_hides_key_value(self):
        config = describe_configuration({"LANGSMITH_API_KEY": "super-secret"})
        assert config["api_key_configured"] is True
        assert "super-secret" not in str(config)

    def test_run_metadata_is_namespaced(self):
        metadata = run_metadata("full", "acme/app", recovery=True)
        assert metadata["sweforge.variant"] == "full"
        assert metadata["sweforge.recovery"] is True
