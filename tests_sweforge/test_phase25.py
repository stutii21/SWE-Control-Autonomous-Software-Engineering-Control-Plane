"""Tests for Phase 25 infrastructure: packaging, registration, reproducibility,
benchmark harness, showcase and concurrency isolation."""

import json

import pytest

from agent.sweforge.graph.entrypoint import (
    build_runtime_for_registration,
    describe_graph,
    get_sweforge_graph,
    get_sweforge_graph_deterministic,
)
from evaluation.benchmarks import (
    BenchmarkRunResult,
    dry_run,
    load_tasks,
    score_results,
    validate_task,
)
from evaluation.benchmarks.harness import (
    MIN_TASKS_FOR_VERDICT,
    REAL_WORLD_BENCHMARK_STATUS,
    BenchmarkSchemaError,
)
from evaluation.reproducibility import (
    BENCHMARK_VERSION,
    RunArtifacts,
    RunManifest,
    credential_presence,
    package_versions,
)


# ==========================================================================
# PART 3 — LangGraph registration
# ==========================================================================
class TestGraphRegistration:
    def test_langgraph_json_registers_sweforge(self):
        config = json.loads(open("langgraph.json").read())
        assert "sweforge" in config["graphs"]
        assert "entrypoint" in config["graphs"]["sweforge"]

    def test_upstream_graphs_are_preserved(self):
        """Registration must not displace Open SWE's own entries."""
        config = json.loads(open("langgraph.json").read())
        for name in ("agent", "reviewer", "analyzer", "chat", "scheduler"):
            assert name in config["graphs"], f"upstream graph {name} was removed"

    def test_registered_target_resolves_and_compiles(self):
        graph = get_sweforge_graph()
        assert type(graph).__name__ == "CompiledStateGraph"

    def test_registered_graph_is_inspectable(self):
        info = describe_graph()
        assert info["node_count"] >= 15
        assert "risk_gate" in info["nodes"]
        assert set(info["terminal_nodes"]) >= {"finalization", "human_approval"}

    def test_baseline_variant_registers_too(self):
        assert type(get_sweforge_graph_deterministic()).__name__ == "CompiledStateGraph"

    def test_registration_does_not_enable_host_execution(self):
        """A registered graph must not silently execute repo code on the host."""
        runtime = build_runtime_for_registration()
        assert runtime.backend is None
        assert runtime.verifier is None

    def test_entrypoint_path_in_config_matches_module(self):
        config = json.loads(open("langgraph.json").read())
        target = config["graphs"]["sweforge"]
        assert target.endswith(":get_sweforge_graph")
        assert "agent/sweforge/graph/entrypoint.py" in target


# ==========================================================================
# PART 2 — packaging
# ==========================================================================
class TestPackaging:
    def test_packaging_metadata_parses(self):
        import tomllib

        data = tomllib.load(open("sweforge-pyproject.toml", "rb"))
        assert data["project"]["name"] == "sweforge"
        assert data["project"]["requires-python"] == ">=3.11"

    def test_console_entry_point_declared(self):
        import tomllib

        data = tomllib.load(open("sweforge-pyproject.toml", "rb"))
        assert data["project"]["scripts"]["sweforge"] == "agent.sweforge.cli:main"

    def test_does_not_vendor_open_swe(self):
        """Upstream must be an optional integration, never a hard dependency."""
        import tomllib

        data = tomllib.load(open("sweforge-pyproject.toml", "rb"))
        core = " ".join(data["project"]["dependencies"]).lower()
        assert "deepagents" not in core
        assert "fastapi" not in core
        assert "deepagents" in " ".join(data["project"]["optional-dependencies"]["openswe"])

    def test_module_entry_point_exists(self):
        from agent.sweforge import __main__

        assert hasattr(__main__, "main")

    def test_cli_main_is_importable_for_console_script(self):
        from agent.sweforge.cli import main

        assert callable(main)


# ==========================================================================
# PART 5 — reproducibility
# ==========================================================================
class TestReproducibility:
    def test_manifest_records_provenance(self):
        manifest = RunManifest(experiment="A", seed=7)
        data = manifest.to_dict()
        for key in ("git_commit", "python_version", "packages", "benchmark_version", "seed"):
            assert key in data
        assert data["seed"] == 7

    def test_manifest_records_no_secret_values(self):
        data = RunManifest().to_dict()
        for present in data["credentials_present"].values():
            assert isinstance(present, bool)

    def test_package_versions_are_tracked(self):
        versions = package_versions()
        assert "langgraph" in versions and "langchain" in versions

    def test_credential_presence_is_boolean_only(self):
        assert all(isinstance(v, bool) for v in credential_presence().values())

    def test_artifacts_directory_written(self, tmp_path):
        artifacts = RunArtifacts.create(RunManifest(experiment="A"), base=tmp_path)
        assert (artifacts.root / "manifest.json").exists()
        artifacts.write_json("results.json", {"ok": True})
        artifacts.write_csv("metrics.csv", [{"a": 1, "b": 2}])
        artifacts.write_text("summary.md", "# summary")
        artifacts.append_traces('{"seq": 1}')
        for name in ("results.json", "metrics.csv", "summary.md", "traces.jsonl"):
            assert (artifacts.root / name).exists()

    def test_benchmark_version_is_pinned(self):
        assert BENCHMARK_VERSION
        manifest = json.loads(open("evaluation/benchmarks/manifest.json").read())
        assert manifest["benchmark_version"] == BENCHMARK_VERSION

    def test_benchmark_manifest_declares_it_is_not_real_world(self):
        manifest = json.loads(open("evaluation/benchmarks/manifest.json").read())
        assert manifest["not_a_real_world_benchmark"] is True
        assert manifest["kind"] == "deterministic_architectural_fixtures"

    def test_experiment_config_lists_variants_and_scenarios(self):
        config = json.loads(open("evaluation/configs/experiment_a.json").read())
        assert len(config["variants"]) == 5
        assert len(config["scenarios"]) == 6

    def test_run_signature_is_stable(self):
        from evaluation.runner import _signature

        payload = {
            "records": [
                {
                    "variant": "E_full",
                    "scenario_id": "s1",
                    "status": "completed",
                    "metrics": {"recovery_attempts": 1, "tool_calls": 5},
                    "node_trace": ["a", "b"],
                }
            ]
        }
        assert _signature(payload) == _signature(payload)

    def test_run_signature_detects_divergence(self):
        from evaluation.runner import _signature

        base = {
            "records": [
                {
                    "variant": "E",
                    "scenario_id": "s",
                    "status": "completed",
                    "metrics": {"recovery_attempts": 1, "tool_calls": 5},
                    "node_trace": ["a"],
                }
            ]
        }
        changed = json.loads(json.dumps(base))
        changed["records"][0]["status"] = "escalated_recovery_exhausted"
        assert _signature(base) != _signature(changed)


# ==========================================================================
# PART 12 — benchmark harness
# ==========================================================================
class TestBenchmarkHarness:
    def _record(self, **overrides):
        base = {
            "task_id": "t1",
            "repository": "example/repo",
            "base_commit": "abc123",
            "task_description": "fix it",
            "test_command": "pytest -q",
        }
        base.update(overrides)
        return base

    def test_valid_record_parses(self):
        task = validate_task(self._record())
        assert task.task_id == "t1"
        assert task.timeout_seconds == 900

    @pytest.mark.parametrize("field", ["task_id", "repository", "base_commit", "test_command"])
    def test_missing_required_field_raises(self, field):
        record = self._record()
        record[field] = ""
        with pytest.raises(BenchmarkSchemaError):
            validate_task(record)

    def test_invalid_timeout_raises(self):
        with pytest.raises(BenchmarkSchemaError):
            validate_task(self._record(timeout_seconds=-1))

    def test_loads_jsonl_example(self):
        tasks = load_tasks("evaluation/benchmarks/example_swebench_lite.jsonl")
        assert len(tasks) == 1
        assert tasks[0].language == "python"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_tasks(tmp_path / "nope.jsonl")

    def test_dry_run_executes_nothing(self):
        result = dry_run(load_tasks("evaluation/benchmarks/example_swebench_lite.jsonl"))
        assert result["executed"] is False
        assert result["real_world_benchmark"] == REAL_WORLD_BENCHMARK_STATUS == "NOT_AVAILABLE"

    def test_dry_run_names_blockers(self):
        result = dry_run(load_tasks("evaluation/benchmarks/example_swebench_lite.jsonl"))
        assert result["blockers"], "dry run must name what is missing"

    def test_scorer_refuses_insufficient_sample(self):
        results = [
            BenchmarkRunResult(task_id="a", system="sweforge", available=True, resolved=True),
            BenchmarkRunResult(task_id="a", system="open_swe", available=True, resolved=False),
        ]
        summary = score_results(results)
        assert summary["verdict"] == "INSUFFICIENT_SAMPLE"
        assert str(MIN_TASKS_FOR_VERDICT) in summary["verdict_reason"]

    def test_scorer_counts_unavailable_separately(self):
        results = [
            BenchmarkRunResult(task_id="a", system="sweforge", available=True, resolved=True),
            BenchmarkRunResult(task_id="a", system="open_swe", available=False),
        ]
        summary = score_results(results)
        assert summary["systems"]["open_swe"]["unavailable"] == 1
        assert summary["paired_tasks"] == 0

    def test_paired_statistics_computed_with_enough_samples(self):
        results = []
        for i in range(MIN_TASKS_FOR_VERDICT + 5):
            results.append(
                BenchmarkRunResult(
                    task_id=f"t{i}", system="sweforge", available=True, resolved=i % 2 == 0
                )
            )
            results.append(
                BenchmarkRunResult(
                    task_id=f"t{i}", system="open_swe", available=True, resolved=i % 3 == 0
                )
            )
        summary = score_results(results)
        assert summary["verdict"] in {"PILOT", "REPORTED"}
        assert "mcnemar" in summary
        assert "bootstrap_95ci" in summary
        assert summary["bootstrap_seed"] == 0

    def test_bootstrap_is_reproducible(self):
        results = []
        for i in range(MIN_TASKS_FOR_VERDICT + 1):
            results.append(
                BenchmarkRunResult(
                    task_id=f"t{i}", system="sweforge", available=True, resolved=True
                )
            )
            results.append(
                BenchmarkRunResult(
                    task_id=f"t{i}", system="open_swe", available=True, resolved=i % 2 == 0
                )
            )
        assert score_results(results)["bootstrap_95ci"] == score_results(results)["bootstrap_95ci"]


# ==========================================================================
# PART 22 — concurrency isolation
# ==========================================================================
class TestConcurrencyIsolation:
    def test_budgets_are_task_local(self):
        from agent.sweforge.budget import BudgetLimits, ExecutionBudget

        first = ExecutionBudget(BudgetLimits(max_model_calls=5))
        second = ExecutionBudget(BudgetLimits(max_model_calls=5))
        first.consume_model_call()
        assert first.model_calls == 1
        assert second.model_calls == 0, "budgets must not be shared across tasks"

    def test_traces_are_task_local(self):
        from agent.sweforge.observability.trace import TraceRecorder

        first, second = TraceRecorder(task_id="a"), TraceRecorder(task_id="b")
        first.node("x")
        assert second.events == []

    def test_concurrent_runs_do_not_contaminate_each_other(self, tmp_path, monkeypatch):
        """Two graphs built concurrently must keep separate state."""
        import shutil
        from concurrent.futures import ThreadPoolExecutor

        from agent.sweforge.graph.workflow import WorkflowConfig, build_workflow
        from agent.sweforge.models.scripted import ScriptedModelFactory
        from agent.sweforge.routing.model_router import ModelRouter
        from agent.sweforge.runner import build_runtime
        from agent.sweforge.state.graph_state import initial_state
        from evaluation.scenarios import scenario_by_id

        monkeypatch.setenv("SWEFORGE_ALLOW_LOCAL_EXEC", "1")
        scenarios = ["billing_validation_first_try", "inventory_boundary_recovery"]

        def _run(name: str):
            scenario = scenario_by_id(name)
            repo = tmp_path / name / scenario.fixture
            repo.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(f"evaluation/fixtures/{scenario.fixture}", repo)
            runtime = build_runtime(
                repo_root=str(repo),
                config=WorkflowConfig(),
                router=ModelRouter(env={}, model_factory=ScriptedModelFactory(scenario.script)),
                backend_kind="local",
                memory_path=str(repo / ".sweforge" / "e.jsonl"),
            )
            final = build_workflow(runtime).invoke(
                initial_state(scenario.task, name, str(repo)), config={"recursion_limit": 60}
            )
            return name, runtime, final

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(_run, scenarios))

        run_ids = {runtime.tracer.run_id for _n, runtime, _f in outcomes}
        assert len(run_ids) == 2, "trace run ids must be unique per task"
        for _name, runtime, final in outcomes:
            assert final["final_status"] == "completed"
            # Every traced event belongs to this run only.
            assert {e.run_id for e in runtime.tracer.events} == {runtime.tracer.run_id}

    def test_evaluation_documents_sequential_as_reproducible_mode(self):
        from evaluation.runner import variant_configs

        for config in variant_configs().values():
            assert config.subtask_workers == 1, (
                "evaluation runs sequentially for reproducibility; concurrency is opt-in"
            )
