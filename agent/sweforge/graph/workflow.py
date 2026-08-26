"""The SWE-Forge LangGraph workflow.

This module is the core SWE-Forge contribution. Upstream Open SWE drives its
SWE loop through ``deepagents.create_deep_agent`` — a single ReAct-style agent
loop wrapped in middleware. That design is excellent for open-ended
interactive work, but the control flow lives inside the model: whether the
agent verifies its work, whether it retries, and when it stops are emergent
properties of a prompt.

SWE-Forge inverts that. The *reasoning* stays with the LLM; the *control flow*
is an explicit, deterministic ``StateGraph``:

    task_intake
      -> repository_analysis
      -> task_complexity_analysis
      -> planning
      -> dynamic_agent_selection
      -> implementation
      -> verification
           |-- passed --> independent_review --> security_analysis --> risk_gate
           |                    |  rejected (budget left) --> recovery
           |                    `- rejected (exhausted)   --> escalation
           `-- failed --> failure_analysis
                              |-- budget left --> recovery --> verification
                              `-- exhausted   --> escalation
      risk_gate
           |-- HIGH  --> human_approval (terminal)
           `-- other --> finalization (terminal)

Properties that follow from doing it this way:

* **Bounded loops.** ``recovery`` is reachable only through a routing function
  that checks ``len(recovery_attempts) < max_recovery_attempts``. An
  autonomous system that can edit code must not be able to loop forever, and
  here that guarantee is structural rather than a prompt instruction.
* **Deterministic control flow.** Every edge condition reads typed state
  (booleans, counters, enum levels) — never free-form model text. Given the
  same structured outputs, the graph takes the same path every time, which is
  what makes the evaluation harness reproducible.
* **Ablatable architecture.** :class:`WorkflowConfig` toggles repository
  intelligence, recovery, review and the risk gate, and the graph is *built
  differently* for each combination. That is what the ablation study in
  ``evaluation/`` actually varies.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from agent.sweforge.agents.roles import Diagnostician, IndependentReviewer
from agent.sweforge.agents.specialized import build_agent
from agent.sweforge.budget import BudgetExceeded, ExecutionBudget
from agent.sweforge.memory.store import ExperienceStore
from agent.sweforge.observability.trace import TraceRecorder
from agent.sweforge.planning.planner import TaskPlanner, select_agents
from agent.sweforge.recovery.classifier import FailureClassifier
from agent.sweforge.repository.analyzer import RepositoryAnalyzer
from agent.sweforge.repository.graph_index import RepositoryGraph
from agent.sweforge.routing.model_router import ModelRouter
from agent.sweforge.schemas import (
    ExecutionMetrics,
    ExperienceRecord,
    FailureDiagnosis,
    RecoveryAttempt,
    ReviewResult,
    RiskScore,
    SecurityFinding,
    TaskPlan,
    VerificationResult,
)
from agent.sweforge.security.risk import ChangeSet, RiskEngine, SecurityScanner
from agent.sweforge.state.graph_state import SWEForgeState
from agent.sweforge.tools.registry import ToolContext, build_tools, tools_by_name
from agent.sweforge.verification.verifier import Verifier

DEFAULT_MAX_RECOVERY_ATTEMPTS = 3
DEFAULT_MAX_REVIEW_CYCLES = 2


@dataclass
class WorkflowConfig:
    """Feature flags defining a workflow variant.

    The ablation study constructs different graphs by toggling these, so each
    flag corresponds to an architectural claim that can be tested.
    """

    enable_repository_intelligence: bool = True
    enable_recovery: bool = True
    enable_review: bool = True
    enable_security_gate: bool = True
    enable_memory: bool = True
    max_recovery_attempts: int = DEFAULT_MAX_RECOVERY_ATTEMPTS
    max_review_cycles: int = DEFAULT_MAX_REVIEW_CYCLES
    full_suite_verification: bool = False
    subtask_workers: int = 1  # >1 executes independent subtasks concurrently
    variant_name: str = "full"

    @classmethod
    def baseline(cls) -> "WorkflowConfig":
        """Single-pass workflow: plan, implement, verify, stop."""
        return cls(
            enable_repository_intelligence=False,
            enable_recovery=False,
            enable_review=False,
            enable_security_gate=False,
            enable_memory=False,
            variant_name="baseline",
        )


@dataclass
class SWEForgeRuntime:
    """Everything the nodes need, injected rather than imported globally.

    Dependency injection here is what makes the graph testable: unit tests and
    the evaluation harness supply a scripted router and a fixture backend, and
    the graph code under test is byte-identical to production.
    """

    repo_root: str
    backend: Any
    router: ModelRouter
    config: WorkflowConfig = field(default_factory=WorkflowConfig)
    memory: ExperienceStore | None = None
    graph_index: RepositoryGraph | None = None
    verifier: Verifier | None = None
    tool_context: ToolContext | None = None
    risk_engine: RiskEngine = field(default_factory=RiskEngine)
    scanner: SecurityScanner = field(default_factory=SecurityScanner)
    classifier: FailureClassifier = field(default_factory=FailureClassifier)
    started_at: float = field(default_factory=time.perf_counter)
    #: Hard execution limits. Enforced in Python; no prompt can raise them.
    budget: ExecutionBudget = field(default_factory=ExecutionBudget)
    budget_error: str | None = None
    #: Local trace, always on. LangSmith is an optional additional sink, so a
    #: run always leaves a durable record even with tracing credentials absent.
    tracer: TraceRecorder = field(default_factory=TraceRecorder)
    #: MCP orchestration. Absent by default: no external calls unless the host
    #: registers adapters and an allowlist.
    mcp_registry: Any | None = None
    mcp_policy: Any | None = None
    #: The SWE-Forge LangChain tool set, keyed by name. Nodes invoke these
    #: rather than calling subsystems directly wherever a structured payload is
    #: the natural interface, so tool usage is real and measurable.
    tools: dict[str, Any] = field(default_factory=dict)
    #: Accumulates the current content of every file the run has written,
    #: which is what the risk engine and reviewer assess.
    written_files: dict[str, str] = field(default_factory=dict)

    def elapsed(self) -> float:
        return round(time.perf_counter() - self.started_at, 6)

    def ensure_tools(self) -> ToolContext:
        if self.tool_context is None:
            self.tool_context = ToolContext(
                repo_root=self.repo_root,
                graph=self.graph_index,
                verifier=self.verifier,
                memory=self.memory,
                risk_engine=self.risk_engine,
                scanner=self.scanner,
                classifier=self.classifier,
            )
        if self.tool_context.tracer is None:
            self.tool_context.tracer = self.tracer
        if not self.tools:
            self.tools = tools_by_name(build_tools(self.tool_context))
        return self.tool_context

    def call_tool(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        node: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Invoke a SWE-Forge LangChain tool by name, with attribution.

        Every call goes through the real ``StructuredTool``, so argument
        validation, error wrapping and ledger accounting all apply. Attribution
        is passed explicitly rather than relying on previously-set context
        state, so a ledger entry can always be traced to the node that caused
        it.
        """
        context = self.ensure_tools()
        tool = self.tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"tool not registered: {name}"}
        context.current_node = node
        context.current_agent = agent
        if self.tool_budget_exceeded():
            return {"ok": False, "error": "tool budget exhausted", "error_category": "budget"}
        try:
            return tool.invoke(payload)
        finally:
            # Tracing happens inside the tool guard so graph-owned and agent
            # bind_tools calls are recorded identically, exactly once.
            self.budget.consume_tool_call()

    def tool_budget_exceeded(self) -> bool:
        """Non-raising budget probe, so a tool call degrades rather than crashes."""
        return self.budget.would_exceed("tool")

    def tool_calls(self) -> int:
        return self.tool_context.ledger.total if self.tool_context else 0


def _metrics(runtime: SWEForgeRuntime, **kwargs: Any) -> ExecutionMetrics:
    """Build a metrics delta; merged additively by the state reducer."""
    kwargs.setdefault("wall_time_seconds", runtime.elapsed())
    kwargs.setdefault("tool_calls", 0)
    return ExecutionMetrics(**kwargs)


# ==========================================================================
# Nodes
# ==========================================================================
def build_nodes(runtime: SWEForgeRuntime) -> dict[str, Any]:
    """Construct the node callables bound to a runtime."""

    config = runtime.config

    # -- 1. task_intake ---------------------------------------------------
    def task_intake(state: SWEForgeState) -> dict[str, Any]:
        task = (state.get("task") or "").strip()
        if not task:
            return {
                "node_trace": ["task_intake"],
                "errors": ["empty task: nothing to do"],
                "final_status": "failed",
                "final_summary": "Task was empty.",
                "execution_metrics": _metrics(runtime),
            }
        return {
            "node_trace": ["task_intake"],
            "repo_root": state.get("repo_root") or runtime.repo_root,
            "execution_metrics": _metrics(runtime),
        }

    # -- 2. repository_analysis -------------------------------------------
    def repository_analysis(state: SWEForgeState) -> dict[str, Any]:
        runtime.ensure_tools()
        if not config.enable_repository_intelligence:
            # Ablation baseline: inventory only, no AST, no import graph. The
            # agent then works from the task text alone, as upstream does.
            analyzer = RepositoryAnalyzer()
            repo_map = analyzer.analyze(state.get("repo_root") or runtime.repo_root)
            return {
                "node_trace": ["repository_analysis(inventory-only)"],
                "repository_map": {**repo_map.to_summary(), "mode": "inventory_only"},
                "relevant_files": [],
                "execution_metrics": _metrics(runtime),
            }

        # Evidence chain, every step through the LangChain tool layer:
        # analyze_repository -> build_repository_graph -> find_relevant_files
        # -> find_dependencies / find_callers -> find_related_tests
        inventory = runtime.call_tool(
            "analyze_repository", {"max_files": 4000}, node="repository_analysis"
        )
        runtime.graph_index = runtime.tool_context.graph  # type: ignore[union-attr]
        if runtime.graph_index is not None:
            runtime.classifier = FailureClassifier(known_files=set(runtime.graph_index.map.files))
            runtime.tool_context.classifier = runtime.classifier  # type: ignore[union-attr]
            if runtime.verifier is not None:
                runtime.verifier.graph = runtime.graph_index

        graph_stats = runtime.call_tool("build_repository_graph", {}, node="repository_analysis")
        ranked = runtime.call_tool(
            "find_relevant_files",
            {"task": state["task"], "limit": 12, "include_tests": True},
            node="repository_analysis",
        )
        hit_paths = [r["path"] for r in ranked.get("results", [])] if ranked.get("ok") else []

        # Lexical ranking can legitimately find nothing (a terse task, or a
        # repository whose identifiers do not overlap the task wording). Falling
        # back to the most-depended-upon source files keeps the evidence chain
        # useful instead of handing the planner an empty list.
        if not hit_paths and runtime.graph_index is not None:
            central = sorted(
                (
                    (len(runtime.graph_index.find_dependents(path)), path)
                    for path, info in runtime.graph_index.map.files.items()
                    if not info.is_test and info.language == "python"
                ),
                key=lambda pair: (-pair[0], pair[1]),
            )
            hit_paths = [path for _count, path in central[:5]]

        # Blast radius and coverage for the top candidates, so planning sees
        # structure rather than a flat file list.
        evidence: dict[str, Any] = {"dependencies": {}, "callers": {}, "related_tests": {}}
        tool_calls = 3
        for path in hit_paths[:3]:
            deps = runtime.call_tool(
                "find_dependencies", {"file": path}, node="repository_analysis"
            )
            tool_calls += 1
            if deps.get("ok"):
                evidence["dependencies"][path] = {
                    "imports": deps.get("imports", [])[:8],
                    "imported_by": deps.get("imported_by", [])[:8],
                }
            tests = runtime.call_tool(
                "find_related_tests", {"file": path}, node="repository_analysis"
            )
            tool_calls += 1
            if tests.get("ok"):
                evidence["related_tests"][path] = tests.get("tests", [])[:6]

        # Callers of the most implicated symbol, when one is discoverable.
        if runtime.graph_index is not None and hit_paths:
            top = runtime.graph_index.map.files.get(hit_paths[0])
            if top is not None and top.symbols:
                symbol = top.symbols[0].name
                callers = runtime.call_tool(
                    "find_callers", {"symbol": symbol}, node="repository_analysis"
                )
                tool_calls += 1
                if callers.get("ok"):
                    evidence["callers"][symbol] = callers.get("referencing_files", [])[:8]

        summary = dict(inventory)
        summary.pop("ok", None)
        summary["graph"] = graph_stats.get("stats", {})
        summary["mode"] = "full_intelligence"
        summary["evidence"] = evidence
        return {
            "node_trace": [f"repository_analysis({len(hit_paths)} candidates)"],
            "repository_map": summary,
            "relevant_files": hit_paths,
            "execution_metrics": _metrics(runtime, tool_calls=tool_calls),
        }

    # -- 2b. external_context (MCP) ----------------------------------------
    def external_context(state: SWEForgeState) -> dict[str, Any]:
        """Consult an external MCP capability when the task references one.

        Deterministic by design: the decision comes from explicit external
        references in the task (an issue number, a URL, a production signal),
        not from asking a model whether it would like to browse.
        """
        if runtime.mcp_registry is None or runtime.mcp_policy is None:
            return {
                "node_trace": ["external_context(skipped: no MCP configured)"],
                "execution_metrics": _metrics(runtime),
            }
        from agent.sweforge.mcp import MCPToolSelector

        selector = MCPToolSelector(runtime.mcp_registry)
        selection = selector.select(task=state["task"], plan=state.get("plan"))
        if not selection.needed or not selection.capability:
            return {
                "node_trace": [f"external_context(not needed: {selection.rationale[:60]})"],
                "execution_metrics": _metrics(runtime),
            }
        result = runtime.mcp_policy.invoke(selection.capability, selection.arguments)
        return {
            "node_trace": [
                f"external_context({selection.capability}: "
                f"{'ok' if result.ok else result.error_category})"
            ],
            "external_context": [{"selection": selection.to_dict(), "result": result.to_dict()}],
            "execution_metrics": _metrics(runtime, tool_calls=1),
        }

    # -- 3. task_complexity_analysis --------------------------------------
    def task_complexity_analysis(state: SWEForgeState) -> dict[str, Any]:
        planner = TaskPlanner(
            router=runtime.router,
            graph=runtime.graph_index if config.enable_repository_intelligence else None,
            memory=runtime.memory if config.enable_memory else None,
        )
        evidence = planner.gather_evidence(state["task"])
        prior = planner.estimate_complexity(state["task"], evidence)
        experience = []
        tool_calls = 0
        if config.enable_memory:
            # Surface retrieval through the tool for trace parity, then use the
            # typed objects for the planner prompt.
            runtime.call_tool(
                "retrieve_similar_tasks",
                {"task": state["task"], "limit": 3},
                node="task_complexity_analysis",
            )
            tool_calls += 1
            experience = planner.retrieve_experience(state["task"], state.get("repository", ""))
        return {
            "node_trace": ["task_complexity_analysis"],
            "complexity": prior,
            "memory_context": [item.record for item in experience],
            "execution_metrics": _metrics(runtime, tool_calls=tool_calls),
        }

    # -- 4. planning -------------------------------------------------------
    def planning(state: SWEForgeState) -> dict[str, Any]:
        planner = TaskPlanner(
            router=runtime.router,
            graph=runtime.graph_index if config.enable_repository_intelligence else None,
            memory=runtime.memory if config.enable_memory else None,
        )
        plan, evidence, reason = planner.plan(state["task"], state.get("repository", ""))
        return {
            "node_trace": ["planning"],
            "plan": plan,
            "complexity": plan.complexity,
            "relevant_files": plan.relevant_files or state.get("relevant_files", []),
            "execution_metrics": _metrics(runtime, model_calls=1),
            "final_summary": f"planner routing: {reason}",
        }

    # -- 5. dynamic_agent_selection ---------------------------------------
    def dynamic_agent_selection(state: SWEForgeState) -> dict[str, Any]:
        plan = state.get("plan")
        if plan is None:
            return {
                "node_trace": ["dynamic_agent_selection"],
                "errors": ["no plan available for agent selection"],
                "execution_metrics": _metrics(runtime),
            }
        roster = select_agents(plan, always_review=config.enable_review)
        return {
            "node_trace": ["dynamic_agent_selection"],
            "selected_agents": list(roster),
            "execution_metrics": _metrics(runtime),
        }

    # -- 6. implementation (plan-driven agent dispatch) --------------------
    def implementation(state: SWEForgeState) -> dict[str, Any]:
        """Dispatch each subtask to the agent its plan names.

        Phase 23: previously every subtask ran through one ImplementationAgent,
        so `subtask.agent` was metadata. Now the validated plan selects the
        concrete agent class, and different plans genuinely execute different
        code paths. The LLM chooses structured intent; this deterministic
        dispatch decides what actually runs.
        """
        plan: TaskPlan | None = state.get("plan")
        if plan is None:
            return {
                "node_trace": ["implementation"],
                "errors": ["no plan to implement"],
                "execution_metrics": _metrics(runtime),
            }

        runtime.ensure_tools()
        results = []
        model_calls = 0
        dispatched: list[str] = []
        budget_stopped = False

        for layer in plan.execution_layers():
            if budget_stopped:
                break
            agents = [
                build_agent(
                    subtask.agent,
                    router=runtime.router,
                    backend=runtime.backend,
                    graph=runtime.graph_index,
                    tools=runtime.tools,
                    budget=runtime.budget,
                )
                for subtask in layer
            ]
            try:
                if config.subtask_workers > 1 and len(layer) > 1:
                    with ThreadPoolExecutor(max_workers=config.subtask_workers) as pool:
                        futures = [
                            pool.submit(agent.run, subtask, plan)
                            for agent, subtask in zip(agents, layer, strict=True)
                        ]
                        layer_results = [f.result() for f in futures]
                else:
                    layer_results = [
                        agent.run(subtask, plan)
                        for agent, subtask in zip(agents, layer, strict=True)
                    ]
            except BudgetExceeded as exc:
                budget_stopped = True
                runtime.budget_error = str(exc)
                break

            layer_results.sort(key=lambda r: r.subtask_id)
            results.extend(layer_results)
            model_calls += len(layer)
            dispatched.extend(f"{s.agent}:{s.id}" for s in layer)

        applied: list[str] = []
        errors: list[str] = []
        for result in results:
            for edit in result.edits:
                try:
                    runtime.backend.write_file(edit.path, edit.content)
                    runtime.written_files[edit.path] = edit.content
                    applied.append(edit.path)
                except Exception as exc:
                    errors.append(f"failed to write {edit.path}: {type(exc).__name__}: {exc}")

        if budget_stopped:
            errors.append(f"implementation halted by budget: {runtime.budget_error}")

        return {
            "node_trace": [f"implementation({', '.join(dispatched) or 'none'})"],
            "implementation_results": results,
            "agents_executed": [r.agent for r in results],
            "errors": errors,
            "execution_metrics": _metrics(runtime, model_calls=model_calls),
        }

    # -- 7. verification ---------------------------------------------------
    def verification(state: SWEForgeState) -> dict[str, Any]:
        if runtime.verifier is None:
            return {
                "node_trace": ["verification(skipped: no backend)"],
                "test_results": None,
                "errors": ["verification unavailable: no execution backend configured"],
                "execution_metrics": _metrics(runtime),
            }
        changed = sorted(runtime.written_files) or list(state.get("relevant_files", []))

        # Verification runs through the run_validation tool so the invocation is
        # validated and ledgered like any other. The Verifier remains the engine
        # underneath, and the sandbox remains the execution boundary.
        payload = runtime.call_tool(
            "run_validation",
            {"changed_files": changed, "full_suite": config.full_suite_verification},
            node="verification",
        )
        if payload.get("ok"):
            result = runtime.verifier.last_result or VerificationResult(
                **{k: v for k, v in payload["verification"].items() if k != "output"}
            )
        else:
            # Tool layer unavailable: fall back to the engine directly rather
            # than losing verification entirely.
            result = runtime.verifier.verify(changed, full_suite=config.full_suite_verification)
        return {
            "node_trace": [f"verification({'PASS' if result.passed else 'FAIL'})"],
            "test_results": result,
            "execution_metrics": _metrics(runtime, verification_runs=1, tool_calls=1),
        }

    # -- 8. failure_analysis ----------------------------------------------
    def failure_analysis(state: SWEForgeState) -> dict[str, Any]:
        result = state.get("test_results")
        if result is None:
            diagnosis = FailureDiagnosis(
                category="environment",
                root_cause="verification produced no result",
                strategy="escalate",
                confidence=0.0,
            )
            return {
                "node_trace": ["failure_analysis(no result)"],
                "failures": [diagnosis],
                "execution_metrics": _metrics(runtime),
            }

        # Deterministic classification first, via the tool: no model call is
        # needed to know that a ModuleNotFoundError is a dependency problem.
        payload = runtime.call_tool(
            "analyze_failure",
            {"output": result.output, "errors": list(result.errors)},
            node="failure_analysis",
        )
        if payload.get("ok"):
            diagnosis = FailureDiagnosis(
                category=payload.get("category", "unknown"),
                root_cause="; ".join(payload.get("evidence", []))[:400]
                or f"{payload.get('category')} failure",
                suspect_files=payload.get("suspect_files", []),
                strategy="",
                confidence=float(payload.get("confidence", 0.0)),
            )
            category = payload.get("category", "unknown")
        else:
            classification = runtime.classifier.classify(result)
            diagnosis = FailureDiagnosis(
                category=classification.category,
                root_cause=classification.describe(),
                suspect_files=classification.suspect_files,
                confidence=classification.confidence,
            )
            category = classification.category
        return {
            "node_trace": [f"failure_analysis({category})"],
            "failures": [diagnosis],
            "execution_metrics": _metrics(runtime, tool_calls=1),
        }

    # -- 9. recovery -------------------------------------------------------
    def recovery(state: SWEForgeState) -> dict[str, Any]:
        result = state.get("test_results")
        attempts: list[RecoveryAttempt] = list(state.get("recovery_attempts", []))
        attempt_number = len(attempts) + 1
        if result is None:
            # Nothing to diagnose; record the attempt so the bounded-loop
            # counter still advances and the graph cannot spin here.
            attempts.append(
                RecoveryAttempt(
                    attempt_number=attempt_number,
                    failure_category="environment",
                    diagnosis="no verification result available to diagnose",
                    strategy="none",
                )
            )
            return {
                "node_trace": ["recovery(aborted: nothing to diagnose)"],
                "recovery_attempts": attempts,
                "execution_metrics": _metrics(runtime, recovery_attempts=attempt_number),
            }
        classification = runtime.classifier.classify(result)

        review = state.get("review_results")
        plan = state.get("plan")
        complexity = state.get("complexity", "moderate")

        diagnostician = Diagnostician(router=runtime.router, backend=runtime.backend)
        candidates = list(state.get("relevant_files", []))[:4]
        # A review rejection is also a failure to repair, so its findings are
        # folded into the repair context.
        if review is not None and not review.approved:
            candidates = [f.file for f in review.findings if f.file] + candidates

        output = diagnostician.diagnose_and_repair(
            task=state["task"],
            classification=classification,
            verification=result,
            complexity=complexity,
            attempt_number=attempt_number,
            previous_strategies=[a.strategy for a in attempts if a.strategy],
            candidate_files=[c for c in candidates if c],
        )

        applied: list[str] = []
        errors: list[str] = []
        for edit in output.edits:
            try:
                runtime.backend.write_file(edit.path, edit.content)
                runtime.written_files[edit.path] = edit.content
                applied.append(edit.path)
            except Exception as exc:
                errors.append(f"recovery failed to write {edit.path}: {exc}")

        attempts.append(
            RecoveryAttempt(
                attempt_number=attempt_number,
                failure_category=output.diagnosis.category,
                diagnosis=output.diagnosis.root_cause,
                strategy=output.strategy or output.diagnosis.strategy,
                edits=output.edits,
                verification=None,  # filled by the next verification pass
            )
        )
        _ = plan  # plan is not needed here; kept for clarity of available state
        return {
            "node_trace": [f"recovery(attempt {attempt_number}, {len(applied)} files)"],
            "recovery_attempts": attempts,
            "errors": errors,
            "execution_metrics": _metrics(runtime, model_calls=1, recovery_attempts=attempt_number),
        }

    # -- 10. independent_review -------------------------------------------
    def independent_review(state: SWEForgeState) -> dict[str, Any]:
        plan = state.get("plan")
        if plan is None:
            return {
                "node_trace": ["independent_review(skipped)"],
                "execution_metrics": _metrics(runtime),
            }
        reviewer = IndependentReviewer(router=runtime.router, graph=runtime.graph_index)

        # The reviewer must inspect the change independently, via the git-diff
        # tool, not receive the implementer's account of it. Fixture repos are
        # not git repositories, so a whole-file rendering is the documented
        # fallback when git is unavailable.
        diff_payload = runtime.call_tool(
            "inspect_git_diff", {"base": "HEAD", "paths": []}, node="independent_review"
        )
        if diff_payload.get("ok") and diff_payload.get("stat"):
            diff = (
                f"git diff --stat vs {diff_payload.get('base')}:\n{diff_payload['stat']}\n\n"
                + _render_change_summary(runtime.written_files)
            )
        else:
            diff = _render_change_summary(runtime.written_files)
        review = reviewer.review(
            task=state["task"],
            plan=plan,
            diff=diff,
            verification=state.get("test_results"),
            changed_files=sorted(runtime.written_files),
        )
        rejections = 0 if review.approved else 1
        return {
            "node_trace": [f"independent_review({'APPROVED' if review.approved else 'REJECTED'})"],
            "review_results": review,
            "execution_metrics": _metrics(runtime, model_calls=1, review_rejections=rejections),
        }

    # -- 11. security_analysis --------------------------------------------
    def security_analysis(state: SWEForgeState) -> dict[str, Any]:
        payload = runtime.call_tool(
            "security_scan", {"files": dict(runtime.written_files)}, node="security_analysis"
        )
        if payload.get("ok"):
            findings = [SecurityFinding.model_validate(f) for f in payload.get("findings", [])]
        else:
            findings = runtime.scanner.scan(ChangeSet(files=dict(runtime.written_files)))
        return {
            "node_trace": [f"security_analysis({len(findings)} findings)"],
            "security_findings": findings,
            "execution_metrics": _metrics(runtime, tool_calls=1),
        }

    # -- 12. risk_gate -----------------------------------------------------
    def risk_gate(state: SWEForgeState) -> dict[str, Any]:
        review = state.get("review_results")
        # The tool path validates the change payload; the typed engine call
        # below is the authoritative one because the gate needs a RiskScore
        # object including factors, and must never depend on a tool failing open.
        runtime.call_tool(
            "calculate_change_risk",
            {
                "files": dict(runtime.written_files),
                "deleted": [],
                "verification_passed": bool(
                    (state.get("test_results") or None) and state["test_results"].passed
                ),
            },
            node="risk_gate",
        )
        score = runtime.risk_engine.assess(
            ChangeSet(files=dict(runtime.written_files)),
            verification=state.get("test_results"),
            findings=list(state.get("security_findings", [])),
            review_rejected=bool(review is not None and not review.approved),
            recovery_attempts=len(state.get("recovery_attempts", [])),
        )
        return {
            "node_trace": [f"risk_gate({score.level}, score={score.score})"],
            "risk_score": score,
            "execution_metrics": _metrics(
                runtime, security_gate_triggered=score.level == "HIGH", tool_calls=1
            ),
        }

    # -- 13. terminal nodes ------------------------------------------------
    def finalization(state: SWEForgeState) -> dict[str, Any]:
        verification = state.get("test_results")
        risk: RiskScore | None = state.get("risk_score")
        review: ReviewResult | None = state.get("review_results")
        findings = state.get("security_findings", [])

        passed = bool(verification and verification.passed)
        status = "completed" if passed else "failed"
        if passed and (findings or (review and review.findings)):
            status = "completed_with_findings"

        summary = (
            f"status={status}; "
            f"verification={'green' if passed else 'red'}; "
            f"risk={risk.level if risk else 'n/a'}; "
            f"review={'approved' if review and review.approved else ('rejected' if review else 'n/a')}; "
            f"recovery_attempts={len(state.get('recovery_attempts', []))}"
        )
        _record_experience(runtime, state, status)
        return {
            "node_trace": ["finalization"],
            "final_status": status,
            "final_summary": summary,
            "execution_metrics": _metrics(runtime, tool_calls=runtime.tool_calls()),
        }

    def human_approval(state: SWEForgeState) -> dict[str, Any]:
        risk = state.get("risk_score")
        _record_experience(runtime, state, "awaiting_human_approval")
        return {
            "node_trace": ["human_approval"],
            "final_status": "awaiting_human_approval",
            "final_summary": (
                f"HIGH risk ({risk.score if risk else 'n/a'}): human approval required. "
                f"{risk.recommendation if risk else ''}"
            ),
            "execution_metrics": _metrics(runtime, tool_calls=runtime.tool_calls()),
        }

    def budget_exhausted(state: SWEForgeState) -> dict[str, Any]:
        """Explicit terminal state for a run that hit a hard resource limit.

        Distinct from escalation: nothing is wrong with the change, the run
        simply ran out of a budget the operator set.
        """
        snapshot = runtime.budget.snapshot()
        _record_experience(runtime, state, "budget_exhausted")
        return {
            "node_trace": [f"budget_exhausted({snapshot.exhausted_reason})"],
            "final_status": "budget_exhausted",
            "final_summary": (
                f"Halted by execution budget: {snapshot.exhausted_reason or runtime.budget_error}. "
                f"Used {snapshot.model_calls_used} model call(s), "
                f"{snapshot.tool_calls_used} tool call(s), "
                f"${snapshot.cost_used_usd:.4f} estimated."
            ),
            "budget_snapshot": snapshot.to_dict(),
            "execution_metrics": _metrics(runtime, tool_calls=runtime.tool_calls()),
        }

    def escalation(state: SWEForgeState) -> dict[str, Any]:
        attempts = state.get("recovery_attempts", [])
        review = state.get("review_results")
        rejected_by_review = bool(review is not None and not review.approved)
        status = (
            "escalated_review_rejected" if rejected_by_review else "escalated_recovery_exhausted"
        )
        last = attempts[-1] if attempts else None
        _record_experience(runtime, state, status)
        return {
            "node_trace": ["escalation"],
            "final_status": status,
            "final_summary": (
                f"Escalated to a human after {len(attempts)} recovery attempt(s). "
                + (f"Last diagnosis: {last.diagnosis[:200]}" if last else "")
                + (
                    f" Reviewer findings: {len(review.blocking_findings)} blocking."
                    if rejected_by_review and review
                    else ""
                )
            ),
            "execution_metrics": _metrics(runtime, tool_calls=runtime.tool_calls()),
        }

    traced: dict[str, Any] = {
        "task_intake": task_intake,
        "repository_analysis": repository_analysis,
        "external_context": external_context,
        "task_complexity_analysis": task_complexity_analysis,
        "planning": planning,
        "dynamic_agent_selection": dynamic_agent_selection,
        "implementation": implementation,
        "verification": verification,
        "failure_analysis": failure_analysis,
        "recovery": recovery,
        "independent_review": independent_review,
        "security_analysis": security_analysis,
        "risk_gate": risk_gate,
        "finalization": finalization,
        "human_approval": human_approval,
        "escalation": escalation,
        "budget_exhausted": budget_exhausted,
    }

    def _wrap(name: str, fn: Any) -> Any:
        """Record node entry/exit locally, without changing node behaviour."""

        def wrapper(state: SWEForgeState) -> dict[str, Any]:
            started = time.perf_counter()
            update = fn(state)
            runtime.tracer.node(
                name,
                task_id=runtime.tracer.task_id,
                status=str(update.get("final_status") or "ok"),
                duration_seconds=round(time.perf_counter() - started, 6),
                recovery_attempt=len(update.get("recovery_attempts", []) or []) or None,
                budget=runtime.budget.snapshot().to_dict(),
                detail={"trace": update.get("node_trace", [])},
            )
            return update

        return wrapper

    return {name: _wrap(name, fn) for name, fn in traced.items()}


# ==========================================================================
# Routing functions — every one reads typed state, never model prose
# ==========================================================================
def route_after_intake(state: SWEForgeState) -> Literal["repository_analysis", "finalization"]:
    return "finalization" if state.get("final_status") == "failed" else "repository_analysis"


def make_route_after_verification(config: WorkflowConfig, budget: Any | None = None):
    """passed -> review/security; failed -> failure_analysis or stop.

    The budget is consulted before any branch that would spend more: a run out
    of budget must terminate explicitly rather than continue on credit.
    """

    def route(
        state: SWEForgeState,
    ) -> Literal[
        "independent_review",
        "security_analysis",
        "failure_analysis",
        "finalization",
        "budget_exhausted",
    ]:
        result = state.get("test_results")
        passed_now = bool(result and result.passed)
        if budget is not None and budget.is_exhausted and not passed_now:
            # Only halt on budget when the work is not already green; a passing
            # run should be allowed to reach its gates and be reported.
            return "budget_exhausted"
        passed = bool(result and result.passed)
        if passed:
            if config.enable_review and _review_budget_left(state, config):
                return "independent_review"
            return "security_analysis" if config.enable_security_gate else "finalization"
        if config.enable_recovery:
            return "failure_analysis"
        # Baseline variant: no recovery, so a red verification ends the run.
        return "finalization"

    return route


def make_route_after_failure_analysis(config: WorkflowConfig, budget: Any | None = None):
    """The bounded-loop guard: recovery is reachable only within budget.

    Two independent bounds apply, and either can stop the loop: the attempt
    count from ``WorkflowConfig``, and the hard resource budget.
    """

    def route(state: SWEForgeState) -> Literal["recovery", "escalation", "budget_exhausted"]:
        if budget is not None and (
            budget.is_exhausted or budget.would_exceed("model") or budget.would_exceed("time")
        ):
            return "budget_exhausted"
        attempts = len(state.get("recovery_attempts", []))
        if attempts < config.max_recovery_attempts:
            return "recovery"
        return "escalation"

    return route


def make_route_after_review(config: WorkflowConfig):
    """approved -> security/finalize; rejected -> recovery within budget."""

    def route(
        state: SWEForgeState,
    ) -> Literal["security_analysis", "finalization", "recovery", "escalation"]:
        review = state.get("review_results")
        if review is None or review.approved:
            return "security_analysis" if config.enable_security_gate else "finalization"
        attempts = len(state.get("recovery_attempts", []))
        if config.enable_recovery and attempts < config.max_recovery_attempts:
            return "recovery"
        return "escalation"

    return route


def route_after_risk_gate(state: SWEForgeState) -> Literal["human_approval", "finalization"]:
    risk = state.get("risk_score")
    if risk is not None and risk.requires_human_approval:
        return "human_approval"
    return "finalization"


def _review_budget_left(state: SWEForgeState, config: WorkflowConfig) -> bool:
    """Prevent an endless implement/review ping-pong."""
    metrics = state.get("execution_metrics")
    rejections = metrics.review_rejections if metrics else 0
    return rejections < config.max_review_cycles


# ==========================================================================
# Graph construction
# ==========================================================================
def build_workflow(runtime: SWEForgeRuntime) -> Any:
    """Build and compile the SWE-Forge StateGraph for a runtime's config."""
    config = runtime.config
    nodes = build_nodes(runtime)

    builder: StateGraph = StateGraph(SWEForgeState)
    for name, fn in nodes.items():
        builder.add_node(name, fn)

    builder.add_edge(START, "task_intake")
    builder.add_conditional_edges(
        "task_intake",
        route_after_intake,
        {"repository_analysis": "repository_analysis", "finalization": "finalization"},
    )
    builder.add_edge("repository_analysis", "external_context")
    builder.add_edge("external_context", "task_complexity_analysis")
    builder.add_edge("task_complexity_analysis", "planning")
    builder.add_edge("planning", "dynamic_agent_selection")
    builder.add_edge("dynamic_agent_selection", "implementation")
    builder.add_edge("implementation", "verification")

    builder.add_conditional_edges(
        "verification",
        make_route_after_verification(config, runtime.budget),
        {
            "independent_review": "independent_review",
            "security_analysis": "security_analysis",
            "failure_analysis": "failure_analysis",
            "finalization": "finalization",
            "budget_exhausted": "budget_exhausted",
        },
    )
    builder.add_conditional_edges(
        "failure_analysis",
        make_route_after_failure_analysis(config, runtime.budget),
        {
            "recovery": "recovery",
            "escalation": "escalation",
            "budget_exhausted": "budget_exhausted",
        },
    )
    # The recovery loop: recovery always re-verifies, and re-entry is gated.
    builder.add_edge("recovery", "verification")

    builder.add_conditional_edges(
        "independent_review",
        make_route_after_review(config),
        {
            "security_analysis": "security_analysis",
            "finalization": "finalization",
            "recovery": "recovery",
            "escalation": "escalation",
        },
    )
    builder.add_edge("security_analysis", "risk_gate")
    builder.add_conditional_edges(
        "risk_gate",
        route_after_risk_gate,
        {"human_approval": "human_approval", "finalization": "finalization"},
    )

    for terminal in ("finalization", "human_approval", "escalation", "budget_exhausted"):
        builder.add_edge(terminal, END)

    return builder.compile()


# ==========================================================================
# Helpers
# ==========================================================================
def _render_change_summary(written: dict[str, str], *, max_chars: int = 6000) -> str:
    """Render written files as a review payload.

    Whole-file content is shown rather than a git diff so review works even in
    a backend without git history (the evaluation fixtures).
    """
    if not written:
        return "(no files were changed)"
    blocks = []
    budget = max_chars
    for path, content in sorted(written.items()):
        body = content if len(content) <= budget else content[:budget] + "\n... [truncated]"
        blocks.append(f"--- {path} ({content.count(chr(10)) + 1} lines) ---\n{body}")
        budget -= len(body)
        if budget <= 0:
            blocks.append("... [remaining files omitted for prompt budget]")
            break
    return "\n\n".join(blocks)


def _record_experience(runtime: SWEForgeRuntime, state: SWEForgeState, status: str) -> None:
    """Persist the run to experience memory. Never fails the run."""
    if runtime.memory is None or not runtime.config.enable_memory:
        return
    try:
        plan = state.get("plan")
        attempts = state.get("recovery_attempts", [])
        verification = state.get("test_results")
        record = ExperienceRecord(
            task=state.get("task", ""),
            repository=state.get("repository", ""),
            complexity=(plan.complexity if plan else state.get("complexity", "moderate")),  # type: ignore[arg-type]
            languages=sorted((state.get("repository_map") or {}).get("languages", {})),
            strategy=(plan.testing_strategy if plan else ""),
            relevant_files=sorted(runtime.written_files) or list(state.get("relevant_files", [])),
            models_used=sorted({r.model for r in state.get("model_usage", [])}),
            tools_used=sorted(
                runtime.tool_context.ledger.by_tool().keys() if runtime.tool_context else []
            ),
            failure_categories=[a.failure_category for a in attempts],
            recovery_strategies=[a.strategy for a in attempts if a.strategy],
            final_status=status,
            recovery_attempts=len(attempts),
            wall_time_seconds=runtime.elapsed(),
            lesson=(
                f"verification {'passed' if verification and verification.passed else 'failed'} "
                f"after {len(attempts)} recovery attempt(s)"
            ),
        )
        runtime.memory.add(record)
    except Exception:
        return
