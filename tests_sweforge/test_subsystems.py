"""Tests for repository intelligence, verification, failure classification and tools."""

import pytest

from agent.sweforge.recovery.classifier import FailureClassifier
from agent.sweforge.repository.analyzer import RepositoryAnalyzer
from agent.sweforge.repository.graph_index import RepositoryGraph, tokenize
from agent.sweforge.schemas import VerificationResult
from agent.sweforge.tools.registry import ToolContext, build_tools, tools_by_name
from agent.sweforge.verification.backends import (
    ExecResult,
    LocalExecutionForbidden,
    LocalSubprocessBackend,
)
from agent.sweforge.verification.verifier import Verifier


# ==========================================================================
# Fixtures: a small real repository on disk
# ==========================================================================
@pytest.fixture
def sample_repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "billing.py").write_text(
        '"""Billing calculations for invoices."""\n'
        "\n"
        "\n"
        "class InvoiceCalculator:\n"
        '    """Computes invoice totals."""\n'
        "\n"
        "    def total(self, subtotal, tax_rate):\n"
        "        return subtotal * (1 + tax_rate)\n"
        "\n"
        "\n"
        "def apply_discount(amount, percent):\n"
        "    return amount - amount * percent / 100\n"
    )
    (tmp_path / "pkg" / "reporting.py").write_text(
        '"""Reporting built on billing."""\n'
        "from pkg.billing import InvoiceCalculator\n"
        "\n"
        "\n"
        "def monthly_report(rows):\n"
        "    return [InvoiceCalculator().total(r, 0.2) for r in rows]\n"
    )
    (tmp_path / "tests" / "test_billing.py").write_text(
        "from pkg.billing import apply_discount\n"
        "\n"
        "\n"
        "def test_apply_discount():\n"
        "    assert apply_discount(100, 10) == 90\n"
    )
    (tmp_path / "broken.py").write_text("def oops(\n")
    (tmp_path / "notes.md").write_text("# notes\n")
    return tmp_path


@pytest.fixture
def repo_graph(sample_repo):
    return RepositoryGraph(RepositoryAnalyzer().analyze(sample_repo))


# ==========================================================================
# Tokenizer
# ==========================================================================
class TestTokenize:
    def test_splits_snake_case(self):
        assert "invoice" in tokenize("invoice_total")
        assert "total" in tokenize("invoice_total")

    def test_splits_camel_case(self):
        tokens = tokenize("InvoiceCalculator")
        assert "invoice" in tokens and "calculator" in tokens

    def test_removes_stopwords_and_short_tokens(self):
        tokens = tokenize("the and for a an")
        assert tokens == []

    def test_is_deterministic(self):
        text = "fix the InvoiceCalculator tax_rate rounding"
        assert tokenize(text) == tokenize(text)


# ==========================================================================
# Analyzer
# ==========================================================================
class TestRepositoryAnalyzer:
    def test_indexes_files_and_languages(self, sample_repo):
        repo_map = RepositoryAnalyzer().analyze(sample_repo)
        assert repo_map.languages["python"] >= 4
        assert repo_map.languages["markdown"] == 1

    def test_extracts_classes_and_functions(self, sample_repo):
        repo_map = RepositoryAnalyzer().analyze(sample_repo)
        billing = repo_map.files["pkg/billing.py"]
        names = {s.name for s in billing.symbols}
        assert {"InvoiceCalculator", "total", "apply_discount"} <= names

    def test_records_method_parentage(self, sample_repo):
        repo_map = RepositoryAnalyzer().analyze(sample_repo)
        billing = repo_map.files["pkg/billing.py"]
        total = next(s for s in billing.symbols if s.name == "total")
        assert total.kind == "method"
        assert total.qualified_name == "InvoiceCalculator.total"

    def test_captures_module_docstring(self, sample_repo):
        repo_map = RepositoryAnalyzer().analyze(sample_repo)
        assert "Billing" in repo_map.files["pkg/billing.py"].docstring

    def test_syntax_error_recorded_not_raised(self, sample_repo):
        repo_map = RepositoryAnalyzer().analyze(sample_repo)
        broken = repo_map.files["broken.py"]
        assert broken.parse_error is not None
        assert "SyntaxError" in broken.parse_error

    def test_identifies_test_files(self, sample_repo):
        repo_map = RepositoryAnalyzer().analyze(sample_repo)
        assert "tests/test_billing.py" in repo_map.test_files
        assert "pkg/billing.py" not in repo_map.test_files

    def test_flags_sensitive_paths(self, tmp_path):
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        (tmp_path / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        repo_map = RepositoryAnalyzer().analyze(tmp_path)
        assert repo_map.files[".github/workflows/ci.yml"].is_sensitive

    def test_max_files_truncates(self, sample_repo):
        repo_map = RepositoryAnalyzer(max_files=2).analyze(sample_repo)
        assert repo_map.truncated
        assert repo_map.file_count == 2

    def test_missing_root_raises(self, tmp_path):
        with pytest.raises(NotADirectoryError):
            RepositoryAnalyzer().analyze(tmp_path / "ghost")

    def test_summary_is_json_safe(self, sample_repo):
        import json

        summary = RepositoryAnalyzer().analyze(sample_repo).to_summary()
        json.dumps(summary)  # must not raise


# ==========================================================================
# Graph queries
# ==========================================================================
class TestRepositoryGraph:
    def test_resolves_absolute_import_edge(self, repo_graph):
        assert "pkg/billing.py" in repo_graph.find_dependencies("pkg/reporting.py")

    def test_reverse_edges(self, repo_graph):
        assert "pkg/reporting.py" in repo_graph.find_dependents("pkg/billing.py")

    def test_finds_symbol_definition(self, repo_graph):
        assert repo_graph.find_definition("InvoiceCalculator") == ["pkg/billing.py"]

    def test_finds_callers_through_imports(self, repo_graph):
        callers = repo_graph.find_callers("InvoiceCalculator")
        assert "pkg/reporting.py" in callers

    def test_finds_tests_for_file(self, repo_graph):
        assert "tests/test_billing.py" in repo_graph.find_tests_for_file("pkg/billing.py")

    def test_unknown_file_returns_empty(self, repo_graph):
        assert repo_graph.find_dependencies("does/not/exist.py") == []

    def test_ranking_prefers_matching_file(self, repo_graph):
        hits = repo_graph.find_related_files("invoice discount calculation", limit=5)
        assert hits
        assert hits[0].path == "pkg/billing.py"

    def test_ranking_explains_itself(self, repo_graph):
        hits = repo_graph.find_related_files("invoice discount", limit=3)
        assert all(hit.reasons for hit in hits)

    def test_ranking_is_deterministic(self, repo_graph):
        first = [h.path for h in repo_graph.find_related_files("invoice tax", limit=5)]
        second = [h.path for h in repo_graph.find_related_files("invoice tax", limit=5)]
        assert first == second

    def test_ranking_can_exclude_tests(self, repo_graph):
        hits = repo_graph.find_related_files("billing", limit=10, include_tests=False)
        assert all("test" not in h.path for h in hits)

    def test_empty_task_returns_nothing(self, repo_graph):
        assert repo_graph.find_related_files("") == []

    def test_relevant_modules(self, repo_graph):
        assert "pkg" in repo_graph.find_relevant_modules("invoice discount")

    def test_stats_report_graph_size(self, repo_graph):
        stats = repo_graph.stats()
        assert stats["files"] >= 4
        assert stats["import_edges"] >= 1

    def test_relative_import_resolution(self, tmp_path):
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "__init__.py").write_text("")
        (tmp_path / "app" / "core.py").write_text("VALUE = 1\n")
        (tmp_path / "app" / "api.py").write_text("from .core import VALUE\n")
        graph = RepositoryGraph(RepositoryAnalyzer().analyze(tmp_path))
        assert "app/core.py" in graph.find_dependencies("app/api.py")


# ==========================================================================
# Execution backends
# ==========================================================================
class TestBackends:
    def test_local_backend_refuses_without_optin(self, tmp_path):
        with pytest.raises(LocalExecutionForbidden):
            LocalSubprocessBackend(tmp_path, env={})

    def test_local_backend_runs_when_enabled(self, tmp_path):
        backend = LocalSubprocessBackend(tmp_path, env={"SWEFORGE_ALLOW_LOCAL_EXEC": "1"})
        result = backend.run("python3 -c print(1)")
        assert isinstance(result, ExecResult)

    def test_write_then_read_roundtrip(self, tmp_path):
        backend = LocalSubprocessBackend(tmp_path, env={"SWEFORGE_ALLOW_LOCAL_EXEC": "1"})
        backend.write_file("a/b.py", "x = 1\n")
        assert backend.read_file("a/b.py") == "x = 1\n"

    def test_path_escape_is_blocked(self, tmp_path):
        backend = LocalSubprocessBackend(tmp_path, env={"SWEFORGE_ALLOW_LOCAL_EXEC": "1"})
        with pytest.raises(ValueError, match="escapes"):
            backend.write_file("../escaped.py", "x = 1\n")

    def test_missing_executable_reported_not_raised(self, tmp_path):
        backend = LocalSubprocessBackend(tmp_path, env={"SWEFORGE_ALLOW_LOCAL_EXEC": "1"})
        result = backend.run("definitely-not-a-real-binary-xyz")
        assert result.exit_code == 127
        assert not result.ok


# ==========================================================================
# Verifier
# ==========================================================================
class _StubBackend:
    """Records commands and replays canned results."""

    name = "stub"

    def __init__(self, responses: dict[str, ExecResult]):
        self.responses = responses
        self.commands: list[str] = []
        self.files: dict[str, str] = {}

    def run(self, command: str, *, timeout: int = 300) -> ExecResult:
        self.commands.append(command)
        for key, response in self.responses.items():
            if key in command:
                return response
        return ExecResult(command, 0, "", "", 0.01)

    def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    def read_file(self, path: str) -> str:
        return self.files[path]


def _exec(stdout: str, code: int = 0) -> ExecResult:
    return ExecResult("cmd", code, stdout, "", 0.01)


class TestVerifier:
    def test_targeted_tests_selected_from_graph(self, repo_graph):
        backend = _StubBackend({})
        plan = Verifier(backend, graph=repo_graph).build_plan(["pkg/billing.py"])
        assert plan.targeted
        assert "tests/test_billing.py" in plan.test_commands[0]

    def test_falls_back_to_full_suite_without_graph(self):
        plan = Verifier(_StubBackend({})).build_plan(["pkg/billing.py"])
        assert not plan.targeted
        assert plan.test_commands == ["python -m pytest -q --no-header"]

    def test_full_suite_flag_overrides_targeting(self, repo_graph):
        plan = Verifier(_StubBackend({}), graph=repo_graph).build_plan(
            ["pkg/billing.py"], full_suite=True
        )
        assert not plan.targeted

    def test_parses_pass_counts(self):
        backend = _StubBackend({"pytest": _exec("===== 3 passed in 0.10s =====")})
        result = Verifier(backend, enable_lint=False).verify(["m.py"])
        assert result.passed and result.tests_passed == 3 and result.tests_failed == 0

    def test_parses_mixed_counts(self):
        backend = _StubBackend({"pytest": _exec("===== 2 failed, 3 passed in 0.4s =====", code=1)})
        result = Verifier(backend, enable_lint=False).verify(["m.py"])
        assert not result.passed
        assert (result.tests_passed, result.tests_failed) == (3, 2)

    def test_does_not_double_count_interrupted_summary(self):
        """'Interrupted: 1 error' must not be counted alongside the summary line."""
        output = (
            "!!!!! Interrupted: 1 error during collection !!!!!\n===== 1 error in 0.12s =====\n"
        )
        backend = _StubBackend({"pytest": _exec(output, code=2)})
        result = Verifier(backend, enable_lint=False).verify(["m.py"])
        assert result.tests_failed == 1

    def test_crash_without_summary_counts_as_failure(self):
        backend = _StubBackend({"pytest": _exec("Traceback ...", code=1)})
        result = Verifier(backend, enable_lint=False).verify(["m.py"])
        assert not result.passed and result.tests_failed == 1

    def test_missing_linter_does_not_fail_the_change(self):
        """A linter that is not installed is an environment gap, not a defect."""
        backend = _StubBackend(
            {
                "pytest": _exec("===== 1 passed in 0.1s ====="),
                "ruff": _exec("No module named ruff", code=1),
            }
        )
        result = Verifier(backend, enable_lint=True).verify(["m.py"])
        assert result.passed
        assert result.lint_passed is None

    def test_missing_linter_output_not_in_diagnostics(self):
        """Regression: 'No module named ruff' once poisoned failure classification."""
        backend = _StubBackend(
            {
                "pytest": _exec("===== 1 passed in 0.1s ====="),
                "ruff": _exec("No module named ruff", code=1),
            }
        )
        result = Verifier(backend, enable_lint=True).verify(["m.py"])
        assert "No module named ruff" not in result.output

    def test_real_lint_failure_fails_verification(self):
        backend = _StubBackend(
            {
                "pytest": _exec("===== 1 passed in 0.1s ====="),
                "ruff": _exec("m.py:1:1: F401 unused import", code=1),
            }
        )
        result = Verifier(backend, enable_lint=True).verify(["m.py"])
        assert result.lint_passed is False
        assert not result.passed


# ==========================================================================
# Failure classifier
# ==========================================================================
def _failure(output: str) -> VerificationResult:
    return VerificationResult(passed=False, tests_run=1, tests_failed=1, output=output)


class TestFailureClassifier:
    @pytest.mark.parametrize(
        "output,expected",
        [
            ("E   SyntaxError: expected ':'", "syntax"),
            ("E   IndentationError: unexpected indent", "syntax"),
            ("E   ModuleNotFoundError: No module named 'foo'", "dependency"),
            ("E   ImportError: cannot import name 'x'", "dependency"),
            ("E   TypeError: unsupported operand type(s)", "type"),
            ("E   AttributeError: 'NoneType' has no attribute 'x'", "type"),
            ("E   AssertionError: assert 1 == 2", "test_assertion"),
            ("E   ValueError: boom", "runtime"),
            ("E   IndexError: list index out of range", "runtime"),
            ("E   PermissionError: denied", "environment"),
            ("command timed out after 300s", "timeout"),
        ],
    )
    def test_categories(self, output, expected):
        assert FailureClassifier().classify(_failure(output)).category == expected

    def test_explicit_exception_beats_assert_source_echo(self):
        """pytest echoes the source line; a named exception is stronger evidence."""
        output = ">       assert add(2, 3) == 5\nE       ValueError: boom\n"
        assert FailureClassifier().classify(_failure(output)).category == "runtime"

    def test_passing_result_is_not_classified(self):
        result = VerificationResult(passed=True, tests_run=1, tests_passed=1)
        classification = FailureClassifier().classify(result)
        assert classification.confidence == 0.0

    def test_unknown_output_has_low_confidence(self):
        classification = FailureClassifier().classify(_failure("something inscrutable"))
        assert classification.category == "unknown"
        assert classification.confidence <= 0.25

    def test_extracts_failing_test_names(self):
        output = "FAILED tests/test_billing.py::test_total - AssertionError\n"
        classification = FailureClassifier().classify(_failure(output))
        assert "tests/test_billing.py::test_total" in classification.failing_tests

    def test_filters_stdlib_frames_from_suspects(self):
        output = (
            'File "/usr/lib/python3.12/importlib/__init__.py", line 90, in import_module\n'
            'File "pkg/billing.py", line 12, in total\n'
            "E   ValueError: boom\n"
        )
        suspects = FailureClassifier().classify(_failure(output)).suspect_files
        assert "pkg/billing.py" in suspects
        assert not any("importlib" in s for s in suspects)

    def test_filters_site_packages(self):
        output = 'File "/x/site-packages/_pytest/python.py", line 1, in f\nE   ValueError: b\n'
        assert FailureClassifier().classify(_failure(output)).suspect_files == []

    def test_known_files_filter_restricts_suspects(self):
        output = 'File "pkg/billing.py", line 1, in f\nFile "ghost.py", line 2, in g\nE   ValueError: b\n'
        classifier = FailureClassifier(known_files={"pkg/billing.py"})
        assert classifier.classify(_failure(output)).suspect_files == ["pkg/billing.py"]

    def test_classification_is_deterministic(self):
        output = "E   AssertionError: assert 1 == 2"
        first = FailureClassifier().classify(_failure(output))
        second = FailureClassifier().classify(_failure(output))
        assert (first.category, first.confidence) == (second.category, second.confidence)


# ==========================================================================
# Tool registry
# ==========================================================================
class TestTools:
    def test_expected_tools_registered(self, sample_repo):
        tools = tools_by_name(build_tools(ToolContext(repo_root=str(sample_repo))))
        for name in (
            "analyze_repository",
            "find_relevant_files",
            "find_dependencies",
            "find_callers",
            "find_related_tests",
            "run_validation",
            "analyze_failure",
            "inspect_git_diff",
            "calculate_change_risk",
            "security_scan",
            "retrieve_similar_tasks",
            "build_repository_graph",
        ):
            assert name in tools

    def test_every_tool_has_description_and_schema(self, sample_repo):
        for tool in build_tools(ToolContext(repo_root=str(sample_repo))):
            assert tool.description and len(tool.description) > 30
            assert tool.args_schema is not None

    def test_tool_returns_structured_error_not_raise(self, sample_repo):
        tools = tools_by_name(build_tools(ToolContext(repo_root=str(sample_repo))))
        result = tools["run_validation"].invoke({"changed_files": ["x.py"]})
        assert result["ok"] is False
        assert "error" in result

    def test_ledger_counts_calls(self, sample_repo):
        context = ToolContext(repo_root=str(sample_repo))
        tools = tools_by_name(build_tools(context))
        tools["find_relevant_files"].invoke({"task": "invoice discount", "limit": 3})
        tools["security_scan"].invoke({"files": {"m.py": "x = 1\n"}})
        assert context.ledger.total == 2
        assert context.ledger.by_tool()["find_relevant_files"] == 1

    def test_failed_call_recorded_as_failure(self, sample_repo):
        context = ToolContext(repo_root=str(sample_repo))
        tools = tools_by_name(build_tools(context))
        tools["run_validation"].invoke({"changed_files": []})
        assert context.ledger.calls[0].ok is False

    def test_argument_validation_rejects_bad_input(self, sample_repo):
        tools = tools_by_name(build_tools(ToolContext(repo_root=str(sample_repo))))
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            tools["find_relevant_files"].invoke({"task": "x", "limit": 9999})

    def test_analyze_repository_builds_graph(self, sample_repo):
        context = ToolContext(repo_root=str(sample_repo))
        tools = tools_by_name(build_tools(context))
        result = tools["analyze_repository"].invoke({})
        assert result["ok"] and result["file_count"] >= 4
        assert context.graph is not None

    def test_analyze_repository_reports_parse_errors(self, sample_repo):
        tools = tools_by_name(build_tools(ToolContext(repo_root=str(sample_repo))))
        result = tools["analyze_repository"].invoke({})
        assert any(e["file"] == "broken.py" for e in result["parse_errors"])

    def test_security_scan_flags_secret(self, sample_repo):
        tools = tools_by_name(build_tools(ToolContext(repo_root=str(sample_repo))))
        content = 'TOKEN = "ghp_' + "A" * 36 + '"\n'
        result = tools["security_scan"].invoke({"files": {"d.py": content}})
        assert result["finding_count"] >= 1

    def test_change_risk_flags_high(self, sample_repo):
        tools = tools_by_name(build_tools(ToolContext(repo_root=str(sample_repo))))
        content = "-----BEGIN PRIVATE KEY-----\n"
        result = tools["calculate_change_risk"].invoke(
            {"files": {"k.pem": content}, "verification_passed": True}
        )
        assert result["requires_human_approval"] is True

    def test_memory_tool_without_store_is_graceful(self, sample_repo):
        tools = tools_by_name(build_tools(ToolContext(repo_root=str(sample_repo))))
        result = tools["retrieve_similar_tasks"].invoke({"task": "anything"})
        assert result["ok"] and result["results"] == []
