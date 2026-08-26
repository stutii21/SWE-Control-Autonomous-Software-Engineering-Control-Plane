"""Documentation-consistency tests.

The Final Freeze audit found four stale numbers that had survived multiple
"final validation" passes — including a test-count badge wrong since Phase 23.
Every one was hand-maintained prose that rotted while the code moved.

These tests make documentation drift a build failure rather than something a
careful reader might eventually notice.
"""

import pytest

from evaluation.check_docs import collect_ground_truth, collect_test_count, run_checks


@pytest.fixture(scope="module")
def report():
    return run_checks()


class TestDocumentationConsistency:
    def test_all_documented_numbers_match_source(self, report):
        failures = [f"{c.name}: {c.detail}" for c in report.failures]
        assert failures == [], "documentation drifted from source:\n" + "\n".join(failures)

    def test_ground_truth_is_derived_not_hardcoded(self):
        """The checker must read the graph, not a constant."""
        truth = collect_ground_truth()
        assert truth["domain_nodes"] > 0
        assert truth["tools"] > 0
        assert truth["specialized_agents"] > 0

    def test_test_count_is_collected_from_pytest(self):
        count = collect_test_count()
        assert count > 100, f"expected a real collection count, got {count}"

    def test_ground_truth_matches_known_architecture(self):
        """Frozen architecture: these must not change without a deliberate decision."""
        truth = collect_ground_truth()
        assert truth["domain_nodes"] == 17
        assert truth["routers"] == 5
        assert truth["terminal_nodes"] == 4
        assert truth["tools"] == 12
        assert truth["specialized_agents"] == 6
        assert truth["total_agent_classes"] == 9


class TestClaimSafety:
    """The README must never imply a result that does not exist."""

    def test_no_open_swe_superiority_claim(self, report):
        check = next(c for c in report.checks if "open-swe superiority" in c.name)
        assert check.ok, f"README implies an unproven comparison: {check.detail}"

    def test_no_real_benchmark_performance_claim(self, report):
        check = next(c for c in report.checks if "benchmark performance" in c.name)
        assert check.ok, check.detail

    def test_no_live_model_result_claim(self, report):
        check = next(c for c in report.checks if "live-model result" in c.name)
        assert check.ok, check.detail

    @pytest.mark.parametrize(
        "marker",
        [
            "real benchmark marked unavailable",
            "open swe head-to-head marked unavailable",
            "live model marked unavailable",
        ],
    )
    def test_unavailable_capabilities_stay_marked(self, report, marker):
        check = next(c for c in report.checks if c.name == marker)
        assert check.ok, f"unavailability marker removed: {check.detail}"

    def test_negative_results_are_retained(self):
        """Negative findings must not be quietly deleted."""
        readme = open("README.md").read()
        assert "negative result" in readme.lower()
        evaluation = open("docs/EVALUATION.md").read()
        assert "no measurable end-to-end effect" in evaluation.lower() or (
            "negative result" in evaluation.lower()
        )
