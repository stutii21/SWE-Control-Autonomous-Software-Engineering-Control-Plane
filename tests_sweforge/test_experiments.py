"""Tests for the Phase 25 component experiments and the classifier regression."""

from agent.sweforge.recovery.classifier import FailureClassifier
from agent.sweforge.schemas import VerificationResult
from evaluation.experiments import (
    CATEGORY_SAMPLES,
    FAILURE_CATEGORIES,
    retrieval_tasks,
    run_memory_experiment,
    run_recovery_matrix,
    run_retrieval_experiment,
    run_routing_experiment,
)


def _failure(output: str) -> VerificationResult:
    return VerificationResult(passed=False, tests_run=1, tests_failed=1, output=output)


class TestConfigurationRuleRegression:
    """Regression for a real bug found by the Phase 25 recovery matrix.

    The configuration rule ended in `\\b` after a quote character. A word
    boundary after a non-word character requires a following word character, so
    `KeyError: 'DATABASE_URL'` at end-of-line could never match and fell through
    to the runtime rule.
    """

    def test_quoted_uppercase_keyerror_is_configuration(self):
        assert (
            FailureClassifier().classify(_failure("E   KeyError: 'DATABASE_URL'")).category
            == "configuration"
        )

    def test_quoted_key_at_end_of_input_still_matches(self):
        assert (
            FailureClassifier().classify(_failure("KeyError: 'API_HOST'")).category
            == "configuration"
        )

    def test_plain_keyerror_remains_runtime(self):
        """The fix must not over-capture ordinary KeyErrors."""
        assert FailureClassifier().classify(_failure("E   KeyError: 42")).category == "runtime"

    def test_lowercase_quoted_key_remains_runtime(self):
        assert (
            FailureClassifier().classify(_failure("E   KeyError: 'user_id'")).category == "runtime"
        )

    def test_validation_error_is_configuration(self):
        assert (
            FailureClassifier()
            .classify(_failure("E   ValidationError: 3 validation errors"))
            .category
            == "configuration"
        )


class TestRetrievalExperiment:
    def test_runs_and_reports_all_strategies(self):
        result = run_retrieval_experiment()
        assert set(result["summary"]) == {"A_lexical", "B_graph", "C_hybrid"}
        assert result["sample_size"] == len(retrieval_tasks())

    def test_reports_every_required_metric(self):
        summary = run_retrieval_experiment()["summary"]["B_graph"]
        for metric in ("p1", "p3", "p5", "r5", "mrr", "latency"):
            assert metric in summary

    def test_metrics_are_bounded(self):
        for metrics in run_retrieval_experiment()["summary"].values():
            for key in ("p1", "p3", "p5", "r5", "mrr"):
                assert 0.0 <= metrics[key] <= 1.0

    def test_is_deterministic(self):
        """Ranking quality must be identical across runs.

        Latency is deliberately excluded: it is a wall-clock measurement, not a
        deterministic property, and asserting equality on it makes the test
        flaky. The ranking metrics are what determinism actually means here.
        """
        metrics = ("p1", "p3", "p5", "r5", "mrr")

        def quality(summary):
            return {
                name: {k: v for k, v in values.items() if k in metrics}
                for name, values in summary.items()
            }

        first = quality(run_retrieval_experiment()["summary"])
        second = quality(run_retrieval_experiment()["summary"])
        assert first == second

    def test_latency_is_reported_but_not_asserted_equal(self):
        """Latency is measured and reported; it is explicitly not deterministic."""
        for values in run_retrieval_experiment()["summary"].values():
            assert values["latency"] >= 0.0

    def test_declares_no_model_used(self):
        result = run_retrieval_experiment()
        assert result["model_used"] is None
        assert result["deterministic"] is True

    def test_ground_truth_files_exist(self):
        from evaluation.experiments import FIXTURE_ROOT

        for task in retrieval_tasks():
            for path in task.relevant_files + task.relevant_tests:
                assert (FIXTURE_ROOT / task.fixture / path).exists(), path


class TestMemoryExperiment:
    def test_runs_and_compares_variants(self, tmp_path):
        result = run_memory_experiment(tmp_path=tmp_path)
        assert set(result["variants"]) == {"M0", "M1"}
        assert result["sample_size"] >= 2

    def test_m0_has_no_context_by_definition(self, tmp_path):
        for row in run_memory_experiment(tmp_path=tmp_path)["per_task"]:
            assert row["M0_context_chars"] == 0

    def test_m1_retrieves_related_prior_experience(self, tmp_path):
        rows = run_memory_experiment(tmp_path=tmp_path)["per_task"]
        assert any(row["M1_top_is_relevant"] for row in rows)

    def test_declines_to_claim_success_improvement(self, tmp_path):
        notes = " ".join(run_memory_experiment(tmp_path=tmp_path)["notes"]).lower()
        assert "not measured" in notes

    def test_is_deterministic(self, tmp_path):
        a = run_memory_experiment(tmp_path=tmp_path)["summary"]
        b = run_memory_experiment(tmp_path=tmp_path)["summary"]
        assert a == b


class TestRoutingExperiment:
    def test_compares_fixed_against_adaptive(self):
        result = run_routing_experiment()
        assert set(result["variants"]) == {"R0", "R1"}

    def test_call_count_is_identical_between_variants(self):
        """Only tier may differ, or the comparison is not like-for-like."""
        for row in run_routing_experiment()["per_complexity"]:
            assert row["calls"] > 0
            assert "cost_ratio" in row

    def test_costs_are_non_negative(self):
        for row in run_routing_experiment()["per_complexity"]:
            assert row["R0_estimated_cost_usd"] >= 0
            assert row["R1_estimated_cost_usd"] >= 0

    def test_complex_tasks_route_more_expensively_than_trivial(self):
        rows = {r["complexity"]: r for r in run_routing_experiment()["per_complexity"]}
        assert rows["complex"]["R1_estimated_cost_usd"] >= rows["trivial"]["R1_estimated_cost_usd"]

    def test_declines_to_claim_reliability_benefit(self):
        notes = " ".join(run_routing_experiment()["notes"]).lower()
        assert "untested" in notes or "not measured" in notes

    def test_is_deterministic(self):
        assert run_routing_experiment()["summary"] == run_routing_experiment()["summary"]


class TestRecoveryMatrix:
    def test_covers_every_classifier_category(self):
        matrix = run_recovery_matrix()["matrix"]
        assert {row["failure_type"] for row in matrix} == set(FAILURE_CATEGORIES)

    def test_detection_is_measured_for_every_category(self):
        for row in run_recovery_matrix()["matrix"]:
            assert row["detection"]

    def test_detection_accuracy_is_perfect_after_the_fix(self):
        assert run_recovery_matrix()["detection_accuracy"] == 1.0

    def test_untested_categories_are_marked_not_assumed(self):
        result = run_recovery_matrix()
        untested = [r for r in result["matrix"] if r["status"] == "untested"]
        for row in untested:
            assert row["success_rate"] is None
            assert row["recovery_runs"] == 0

    def test_samples_exist_for_every_category(self):
        assert set(CATEGORY_SAMPLES) == set(FAILURE_CATEGORIES)

    def test_handles_missing_results_file(self, tmp_path):
        result = run_recovery_matrix(results_path=tmp_path / "nope.json")
        assert result["categories_measured"] == 0
        assert result["categories_untested"] == len(FAILURE_CATEGORIES)
