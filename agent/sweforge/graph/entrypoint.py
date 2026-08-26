"""LangGraph registration entry point.

Phase 25 remediation: the Phase 24 report stated plainly that "the graph is not
registered in langgraph.json". This module is the factory that registration
points at.

It builds a genuine SWE-Forge graph — the same ``build_workflow`` used by the CLI
and the evaluation harness, not a parallel implementation — so what LangGraph
tooling loads is exactly what the tests exercise.

The five upstream Open SWE entries in ``langgraph.json`` are untouched;
``sweforge`` is added alongside them.

Backend selection is deliberate: registration must not silently execute
repository code on the host. With no sandbox supplied, the graph is built with a
verification backend absent, so it compiles and can be inspected while any real
verification requires an explicitly provided Open SWE sandbox.
"""

from typing import Any

from agent.sweforge.graph.workflow import SWEForgeRuntime, WorkflowConfig, build_workflow
from agent.sweforge.recovery.classifier import FailureClassifier
from agent.sweforge.routing.model_router import ModelRouter
from agent.sweforge.security.risk import RiskEngine, SecurityScanner


def build_runtime_for_registration(
    *,
    repo_root: str = ".",
    backend: Any | None = None,
    router: ModelRouter | None = None,
    config: WorkflowConfig | None = None,
) -> SWEForgeRuntime:
    """Construct a runtime suitable for a registered (deployed) graph."""
    return SWEForgeRuntime(
        repo_root=repo_root,
        backend=backend,
        router=router or ModelRouter(),
        config=config or WorkflowConfig(),
        risk_engine=RiskEngine(),
        scanner=SecurityScanner(),
        classifier=FailureClassifier(),
    )


def get_sweforge_graph() -> Any:
    """LangGraph factory: returns the compiled SWE-Forge StateGraph.

    Referenced from ``langgraph.json`` as
    ``agent.sweforge.graph.entrypoint:get_sweforge_graph``.
    """
    return build_workflow(build_runtime_for_registration())


def get_sweforge_graph_deterministic() -> Any:
    """Compiled graph with the baseline (single-pass) configuration.

    Useful for inspecting the ablation topology through the same tooling.
    """
    return build_workflow(build_runtime_for_registration(config=WorkflowConfig.baseline()))


def describe_graph() -> dict[str, Any]:
    """Inspectable description of the registered graph, for validation."""
    graph = get_sweforge_graph().get_graph()
    return {
        "nodes": sorted(n for n in graph.nodes if not n.startswith("__")),
        "node_count": len([n for n in graph.nodes if not n.startswith("__")]),
        "edge_count": len(graph.edges),
        "terminal_nodes": sorted({e.source for e in graph.edges if e.target == "__end__"}),
    }
