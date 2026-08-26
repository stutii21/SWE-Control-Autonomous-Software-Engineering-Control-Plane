"""SWE-Forge command line interface.

Subcommands:

``demo``     run a scripted scenario end to end and print the full trace
``run``      run a real task against a repository (requires model credentials)
``analyze``  repository intelligence only: index, rank files, show the graph
``doctor``   report configuration: models, tracing, execution backend, memory

The demo exists because the architecture is the point of this project, and a
trace showing plan -> implement -> verify -> diagnose -> repair -> re-verify ->
review -> risk-gate communicates that faster than prose.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from agent.sweforge.graph.workflow import WorkflowConfig
from agent.sweforge.observability.tracing import describe_configuration
from agent.sweforge.repository.analyzer import RepositoryAnalyzer
from agent.sweforge.repository.graph_index import RepositoryGraph
from agent.sweforge.routing.model_router import TIER_ENV_VARS, ModelRouter
from agent.sweforge.runner import RunOutcome, run_task

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"


def _supports_colour() -> bool:
    return sys.stdout.isatty()


def _c(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}" if _supports_colour() else text


def _header(text: str) -> str:
    return _c(f"\n{'=' * 74}\n{text}\n{'=' * 74}", BOLD)


def _print_trace(outcome: RunOutcome) -> None:
    print(_header("GRAPH EXECUTION TRACE"))
    for index, node in enumerate(outcome.node_trace, start=1):
        marker = "->"
        colour = CYAN
        if "PASS" in node or "APPROVED" in node:
            colour = GREEN
        elif "FAIL" in node or "REJECTED" in node:
            colour = RED
        elif "recovery" in node or "risk_gate" in node:
            colour = YELLOW
        print(f"  {index:2d}. {marker} {_c(node, colour)}")


def _print_stage_detail(outcome: RunOutcome) -> None:
    state = outcome.state

    repo_map = state.get("repository_map") or {}
    if repo_map:
        print(_header("1. REPOSITORY INTELLIGENCE"))
        print(f"  files indexed        : {repo_map.get('file_count')}")
        print(f"  symbols extracted    : {repo_map.get('symbol_count')}")
        graph_stats = repo_map.get("graph") or {}
        if graph_stats:
            print(f"  import edges         : {graph_stats.get('import_edges')}")
            print(f"  test files           : {graph_stats.get('test_files')}")
        print(f"  analysis time        : {repo_map.get('analysis_seconds')}s")
        print(f"  mode                 : {repo_map.get('mode')}")
        relevant = state.get("relevant_files") or []
        if relevant:
            print(f"  top relevant files   : {', '.join(relevant[:5])}")

    plan = state.get("plan")
    if plan is not None:
        print(_header("2. PLAN (structured output)"))
        print(f"  complexity           : {plan.complexity}")
        print(f"  risk level           : {plan.risk_level}")
        print(f"  testing strategy     : {plan.testing_strategy}")
        print(f"  subtasks             : {len(plan.subtasks)}")
        for layer_index, layer in enumerate(plan.execution_layers(), start=1):
            names = ", ".join(f"{s.id}({s.agent})" for s in layer)
            print(f"    layer {layer_index} (parallel-safe): {names}")

    agents = state.get("selected_agents") or []
    if agents:
        print(_header("3. DYNAMIC AGENT SELECTION"))
        print(f"  selected: {', '.join(agents)}")

    results = state.get("implementation_results") or []
    if results:
        print(_header("4. IMPLEMENTATION"))
        for result in results:
            files = ", ".join(result.touched_files) or "no files"
            print(f"  [{result.subtask_id}] {result.agent}: {files}")
            if result.notes:
                print(f"      {_c(result.notes[:160], DIM)}")

    verification = state.get("test_results")
    if verification is not None:
        print(_header("5. VERIFICATION"))
        verdict = _c("PASSED", GREEN) if verification.passed else _c("FAILED", RED)
        print(f"  result   : {verdict}")
        print(f"  summary  : {verification.summary()}")
        print(f"  commands : {'; '.join(verification.commands)}")
        print(f"  duration : {verification.duration_seconds}s")
        for error in verification.errors[:5]:
            print(f"    {_c(error[:150], RED)}")

    attempts = state.get("recovery_attempts") or []
    if attempts:
        print(_header("6. FAILURE ANALYSIS & SELF-REPAIR"))
        for attempt in attempts:
            print(f"  attempt {attempt.attempt_number}: category={attempt.failure_category}")
            print(f"      diagnosis: {attempt.diagnosis[:180]}")
            print(f"      strategy : {attempt.strategy[:160]}")
            print(f"      edits    : {', '.join(e.path for e in attempt.edits) or 'none'}")

    review = state.get("review_results")
    if review is not None:
        print(_header("7. INDEPENDENT REVIEW"))
        verdict = _c("APPROVED", GREEN) if review.approved else _c("REJECTED", RED)
        print(f"  verdict  : {verdict} (severity {review.severity})")
        print(f"  summary  : {review.summary}")
        for finding in review.findings:
            print(f"    [{finding.severity}] {finding.file or '-'}: {finding.message[:150]}")

    findings = state.get("security_findings") or []
    print(_header("8. SECURITY ANALYSIS"))
    if findings:
        for finding in findings:
            print(
                f"  [{_c(finding.severity, RED)}] {finding.rule} "
                f"{finding.file}:{finding.line} — {finding.message}"
            )
    else:
        print("  no findings")

    risk = state.get("risk_score")
    if risk is not None:
        print(_header("9. RISK GATE"))
        colour = {"LOW": GREEN, "MEDIUM": YELLOW, "HIGH": RED}[risk.level]
        print(f"  score          : {risk.score}/100 ({_c(risk.level, colour)})")
        for factor in risk.factors:
            print(f"    +{factor.weight:3d} {factor.code}: {factor.detail[:120]}")
        print(f"  recommendation : {risk.recommendation}")

    print(_header("10. OUTCOME"))
    status = outcome.final_status
    colour = (
        GREEN
        if status.startswith("completed")
        else (YELLOW if status == "awaiting_human_approval" else RED)
    )
    print(f"  final status : {_c(status, colour)}")
    print(f"  summary      : {state.get('final_summary')}")

    print(_header("11. METRICS"))
    for key, value in outcome.metrics().items():
        print(f"  {key:28s}: {value}")

    usage = state.get("model_usage") or []
    if usage:
        print(_header("12. MODEL ROUTING LEDGER"))
        print(f"  {'node':22s} {'role':18s} {'tier':10s} {'latency':>9s}")
        for record in usage:
            print(
                f"  {record.node[:22]:22s} {record.role[:18]:18s} "
                f"{record.tier:10s} {record.latency_seconds:9.4f}"
            )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_demo(args: argparse.Namespace) -> int:
    """Run a scripted scenario against a real fixture repository."""
    import shutil
    import tempfile

    from agent.sweforge.models.scripted import ScriptedModelFactory
    from evaluation.scenarios import all_scenarios, scenario_by_id

    if args.list:
        print(_header("AVAILABLE DEMO SCENARIOS"))
        for scenario in all_scenarios():
            print(f"  {_c(scenario.id, BOLD)}")
            print(f"      fixture : {scenario.fixture}")
            print(f"      task    : {scenario.task[:110]}")
            print(f"      shows   : {scenario.description}")
            print(f"      tags    : {', '.join(scenario.tags)}")
        return 0

    scenario = scenario_by_id(args.scenario)
    fixture_source = (
        Path(__file__).resolve().parents[2] / "evaluation" / "fixtures" / scenario.fixture
    )
    if not fixture_source.is_dir():
        print(f"fixture not found: {fixture_source}", file=sys.stderr)
        return 2

    config = WorkflowConfig.baseline() if args.baseline else WorkflowConfig()
    print(_header("SWE-FORGE DEMO"))
    print(f"  scenario   : {scenario.id}")
    print(f"  variant    : {config.variant_name}")
    print(f"  repository : evaluation/fixtures/{scenario.fixture}")
    print(f"  task       : {scenario.task}")
    print(f"  shows      : {scenario.description}")
    print(
        _c(
            "\n  NOTE: model outputs are pinned by evaluation/scenarios.py so the run is "
            "reproducible.\n  The repository, the edits and the pytest results are real.",
            DIM,
        )
    )

    with tempfile.TemporaryDirectory(prefix="sweforge-demo-") as tmp:
        repo_root = Path(tmp) / scenario.fixture
        shutil.copytree(fixture_source, repo_root)
        router = ModelRouter(env={}, model_factory=ScriptedModelFactory(scenario.script))
        outcome = run_task(
            task=scenario.task,
            repo_root=str(repo_root),
            repository=f"fixtures/{scenario.fixture}",
            config=config,
            router=router,
            backend_kind="local",
            memory_path=str(repo_root / ".sweforge" / "experience.jsonl"),
        )
        _print_trace(outcome)
        _print_stage_detail(outcome)

        if args.json:
            print(_header("MACHINE-READABLE METRICS"))
            print(json.dumps(outcome.metrics(), indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run a real task with a live model (requires provider credentials)."""
    config = WorkflowConfig.baseline() if args.baseline else WorkflowConfig()
    config.full_suite_verification = args.full_suite
    config.max_recovery_attempts = args.max_recovery

    print(_header("SWE-FORGE RUN"))
    print(f"  repository : {args.repo}")
    print(f"  task       : {args.task}")
    print(f"  variant    : {config.variant_name}")

    try:
        outcome = run_task(
            task=args.task,
            repo_root=args.repo,
            repository=args.repo,
            config=config,
            backend_kind=args.backend,
        )
    except Exception as exc:
        print(f"\n{_c('run failed', RED)}: {type(exc).__name__}: {exc}", file=sys.stderr)
        if "ALLOW_LOCAL_EXEC" in str(exc):
            print(
                "\nThe local backend executes repository code on this host and is gated. "
                "Use the Open SWE sandbox backend for untrusted repositories.",
                file=sys.stderr,
            )
        return 1

    _print_trace(outcome)
    _print_stage_detail(outcome)
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Repository intelligence only — no model calls, no credentials needed."""
    repo_map = RepositoryAnalyzer(max_files=args.max_files).analyze(args.repo)
    graph = RepositoryGraph(repo_map)

    print(_header("REPOSITORY INTELLIGENCE"))
    print(json.dumps(repo_map.to_summary(), indent=2))
    print("\ngraph:", json.dumps(graph.stats(), indent=2))

    parse_errors = [(p, f.parse_error) for p, f in repo_map.files.items() if f.parse_error]
    if parse_errors:
        print(f"\n{len(parse_errors)} file(s) failed to parse:")
        for path, error in parse_errors[:10]:
            print(f"  {path}: {error}")

    if args.task:
        print(_header(f"RELEVANCE RANKING FOR: {args.task}"))
        for hit in graph.find_related_files(args.task, limit=args.limit):
            print(f"  {hit.score:7.2f}  {hit.path}")
            for reason in hit.reasons[:3]:
                print(f"           {_c(reason, DIM)}")
        print(f"\n  implicated modules: {', '.join(graph.find_relevant_modules(args.task))}")

    if args.file:
        print(_header(f"DEPENDENCY VIEW: {args.file}"))
        print(f"  imports      : {', '.join(graph.find_dependencies(args.file)) or 'none'}")
        print(f"  imported by  : {', '.join(graph.find_dependents(args.file)) or 'none'}")
        print(f"  covering tests: {', '.join(graph.find_tests_for_file(args.file)) or 'none'}")

    if args.symbol:
        print(_header(f"SYMBOL VIEW: {args.symbol}"))
        print(f"  defined in       : {', '.join(graph.find_definition(args.symbol)) or 'unknown'}")
        print(f"  referencing files: {', '.join(graph.find_callers(args.symbol)[:10]) or 'none'}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Show configuration without printing any secret value."""
    import os

    print(_header("SWE-FORGE CONFIGURATION"))
    print("model tiers (env var -> resolved id):")
    for tier, (env_var, default) in TIER_ENV_VARS.items():
        value = os.environ.get(env_var)
        source = "env" if value else "default"
        print(f"  {tier:10s} {env_var:28s} = {value or default}  ({source})")

    print("\nprovider credentials present (names only, values never shown):")
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "FIREWORKS_API_KEY"):
        print(f"  {key:22s}: {'configured' if os.environ.get(key) else 'not set'}")

    print("\nLangSmith tracing:")
    for key, value in describe_configuration().items():
        print(f"  {key:22s}: {value}")

    print("\nexecution:")
    allow_local = os.environ.get("SWEFORGE_ALLOW_LOCAL_EXEC") == "1"
    print(f"  local host execution  : {'ENABLED (fixtures only)' if allow_local else 'disabled'}")
    print(
        f"  memory path           : {os.environ.get('SWEFORGE_MEMORY_PATH', '.sweforge/experience.jsonl')}"
    )
    return 0


def cmd_showcase(args: argparse.Namespace) -> int:
    """One-command deterministic showcase of the full control plane."""
    import shutil
    import tempfile

    from agent.sweforge.graph.workflow import build_workflow
    from agent.sweforge.models.scripted import ScriptedModelFactory
    from agent.sweforge.runner import build_runtime
    from agent.sweforge.state.graph_state import initial_state
    from evaluation.scenarios import scenario_by_id

    scenario = scenario_by_id(args.scenario)
    fixture = Path(__file__).resolve().parents[2] / "evaluation" / "fixtures" / scenario.fixture
    if not fixture.is_dir():
        print(f"fixture not found: {fixture}", file=sys.stderr)
        return 2

    print(_header("SWE-FORGE SHOWCASE"))
    print(f"  scenario : {scenario.id}")
    print(f"  task     : {scenario.task}")
    print(f"  shows    : {scenario.description}")
    print(
        _c(
            "\n  Deterministic: model outputs are pinned by evaluation/scenarios.py.\n"
            "  The repository, the edits and the pytest results are real.",
            DIM,
        )
    )

    with tempfile.TemporaryDirectory(prefix="sweforge-showcase-") as tmp:
        repo = Path(tmp) / scenario.fixture
        shutil.copytree(fixture, repo)
        # Give the implementation agent real tool calls so the bind_tools path
        # appears in the showcase trace, not just graph-owned tool invocation.
        factory = ScriptedModelFactory(
            scenario.script,
            tool_calls={
                "implementation": [
                    [
                        {
                            "name": "find_relevant_files",
                            "args": {"task": scenario.task, "limit": 3},
                        },
                        {"name": "find_dependencies", "args": {"file": scenario.script_target()}},
                    ],
                    [],
                ]
            },
        )
        router = ModelRouter(env={}, model_factory=factory)
        runtime = build_runtime(
            repo_root=str(repo),
            config=WorkflowConfig(),
            router=router,
            backend_kind="local",
            memory_path=str(repo / ".sweforge" / "experience.jsonl"),
        )
        runtime.tracer.task_id = scenario.id
        started = time.perf_counter()
        final = build_workflow(runtime).invoke(
            initial_state(scenario.task, f"fixtures/{scenario.fixture}", str(repo)),
            config={"recursion_limit": 60},
        )
        elapsed = time.perf_counter() - started

        _print_showcase_flow(runtime, final)

        metrics = final.get("execution_metrics")
        budget = runtime.budget.snapshot()
        print(_header("METRICS"))
        print(f"  total nodes          : {len(final.get('node_trace', []))}")
        print(f"  agents executed      : {', '.join(final.get('agents_executed', [])) or 'none'}")
        print(f"  model calls          : {runtime.router.ledger.total_calls}")
        print(f"  tool calls           : {runtime.tool_calls()}")
        print(f"  recovery attempts    : {len(final.get('recovery_attempts', []))}")
        print(f"  latency              : {elapsed:.3f}s")
        print(
            f"  budget consumed      : {budget.model_calls_used} model / "
            f"{budget.tool_calls_used} tool / ${budget.cost_used_usd:.4f} est."
        )
        print(
            f"  budget remaining     : {budget.model_calls_remaining} model / "
            f"{budget.tool_calls_remaining} tool"
        )
        print(f"  final status         : {final.get('final_status')}")

        artifact = {
            "scenario": scenario.id,
            "task": scenario.task,
            "deterministic": True,
            "model_mode": "scripted",
            "final_status": final.get("final_status"),
            "node_trace": final.get("node_trace", []),
            "agents_executed": final.get("agents_executed", []),
            "metrics": {
                "total_nodes": len(final.get("node_trace", [])),
                "model_calls": runtime.router.ledger.total_calls,
                "tool_calls": runtime.tool_calls(),
                "recovery_attempts": len(final.get("recovery_attempts", [])),
                "latency_seconds": round(elapsed, 4),
                "verification_runs": metrics.verification_runs if metrics else 0,
            },
            "budget": budget.to_dict(),
            "risk": (final["risk_score"].model_dump() if final.get("risk_score") else None),
            "tool_sequence": runtime.tracer.summary()["tool_sequence"],
            "trace_summary": runtime.tracer.summary(),
        }
        out = Path(args.output or "evaluation/artifacts/showcase")
        out.mkdir(parents=True, exist_ok=True)
        (out / "showcase.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        runtime.tracer.write(out / "traces.jsonl")
        print(_header("ARTIFACTS"))
        print(f"  {out / 'showcase.json'}")
        print(f"  {out / 'traces.jsonl'}  ({len(runtime.tracer.events)} events)")
    return 0


def _print_showcase_flow(runtime: Any, final: dict[str, Any]) -> None:
    """Render the compact vertical execution flow."""
    print(_header("EXECUTION FLOW"))
    tool_by_node: dict[str, list[str]] = {}
    for event in runtime.tracer.events:
        if event.event == "tool" and event.node:
            tool_by_node.setdefault(event.node, []).append(event.tool or "?")

    print(f"  TASK: {final.get('task', '')[:70]}")
    for entry in final.get("node_trace", []):
        name = entry.split("(")[0]
        colour = CYAN
        if "PASS" in entry or "APPROVED" in entry:
            colour = GREEN
        elif "FAIL" in entry or "REJECTED" in entry:
            colour = RED
        elif "recovery" in entry or "risk_gate" in entry or "human" in entry:
            colour = YELLOW
        print("    |")
        print(f"    v {_c(entry, colour)}")
        for tool in tool_by_node.get(name, []):
            print(f"        - {_c(tool, DIM)}")
    print("    |")
    status = str(final.get("final_status", "unknown"))
    colour = (
        GREEN
        if status.startswith("completed")
        else (YELLOW if status == "awaiting_human_approval" else RED)
    )
    print(f"    v {_c(status.upper(), colour)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sweforge",
        description="SWE-Forge: adaptive, self-verifying autonomous software engineering.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="Run a reproducible scripted scenario.")
    demo.add_argument("--scenario", default="inventory_boundary_recovery")
    demo.add_argument("--list", action="store_true", help="List available scenarios.")
    demo.add_argument("--baseline", action="store_true", help="Run the baseline variant instead.")
    demo.add_argument("--json", action="store_true", help="Also print metrics as JSON.")
    demo.set_defaults(func=cmd_demo)

    run = sub.add_parser("run", help="Run a real task (needs model credentials).")
    run.add_argument("--repo", required=True, help="Path to the repository.")
    run.add_argument("--task", required=True, help="Task or issue description.")
    run.add_argument("--backend", default="local", choices=["local", "sandbox"])
    run.add_argument("--baseline", action="store_true")
    run.add_argument("--full-suite", action="store_true", help="Run the full test suite.")
    run.add_argument("--max-recovery", type=int, default=3)
    run.set_defaults(func=cmd_run)

    analyze = sub.add_parser("analyze", help="Repository intelligence only (no model calls).")
    analyze.add_argument("--repo", required=True)
    analyze.add_argument("--task", default=None, help="Rank files against this task.")
    analyze.add_argument("--file", default=None, help="Show dependencies for one file.")
    analyze.add_argument("--symbol", default=None, help="Show definition/callers of a symbol.")
    analyze.add_argument("--limit", type=int, default=10)
    analyze.add_argument("--max-files", type=int, default=4000)
    analyze.set_defaults(func=cmd_analyze)

    show = sub.add_parser("showcase", help="One-command deterministic showcase.")
    show.add_argument("--scenario", default="inventory_boundary_recovery")
    show.add_argument("--output", default=None, help="Artifact output directory.")
    show.set_defaults(func=cmd_showcase)

    doctor = sub.add_parser("doctor", help="Show configuration and readiness.")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
