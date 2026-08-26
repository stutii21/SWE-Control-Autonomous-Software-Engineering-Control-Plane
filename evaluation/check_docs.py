"""Documentation consistency checker.

The Final Freeze audit found four stale numbers in the docs — a test count that
had been wrong since Phase 23, a file count, and repository statistics that no
longer reproduced. Every one was a hand-maintained figure that rotted silently
while the code moved.

This script makes that failure mode detectable. It extracts ground truth from
the *source* (graph topology, tool registry, agent registry, pytest collection)
and asserts the documentation agrees. It is wired into CI, so documentation
drift fails the build instead of surviving until someone reads carefully.

Deliberately narrow: it checks facts that are cheap to derive and expensive to
notice when wrong. It does not try to validate prose.

Usage:
    python -m evaluation.check_docs          # report and exit non-zero on drift
    python -m evaluation.check_docs --json   # machine-readable
"""

import json
import os
import re
import subprocess
import sys
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
#: Documentation that must stay accurate. Historical phase reports are excluded:
#: they are point-in-time evidence and are labelled as such.
LIVING_DOCS = (
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/LANGCHAIN_LANGGRAPH.md",
    "docs/CUSTOMIZATIONS.md",
    "docs/EVALUATION.md",
    "docs/SECURITY.md",
    "docs/DEMO.md",
    "docs/PROJECT_CLAIMS.md",
    "docs/INTERVIEW_GUIDE.md",
    "docs/EXECUTION_BUDGETS.md",
    "docs/LIVE_EVALUATION.md",
)


@dataclass
class Check:
    name: str
    expected: Any
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    ground_truth: dict[str, Any] = field(default_factory=dict)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ground_truth": self.ground_truth,
            "checks": [
                {"name": c.name, "expected": c.expected, "ok": c.ok, "detail": c.detail}
                for c in self.checks
            ],
            "passed": len(self.checks) - len(self.failures),
            "failed": len(self.failures),
        }


def collect_ground_truth() -> dict[str, Any]:
    """Derive every checkable quantity from source, never from prose."""
    from agent.sweforge.agents.specialized import AGENT_CLASSES
    from agent.sweforge.graph.workflow import (
        SWEForgeRuntime,
        WorkflowConfig,
        build_nodes,
        build_workflow,
    )
    from agent.sweforge.routing.model_router import ModelRouter
    from agent.sweforge.state.graph_state import FinalStatus
    from agent.sweforge.tools.registry import ToolContext, build_tools

    runtime = SWEForgeRuntime(
        repo_root=".", backend=None, router=ModelRouter(env={}), config=WorkflowConfig()
    )
    nodes = build_nodes(runtime)
    graph = build_workflow(runtime).get_graph()
    terminals = sorted({e.source for e in graph.edges if e.target == "__end__"})
    routers = sorted({e.source for e in graph.edges if getattr(e, "conditional", False)})
    statuses = list(typing.get_args(FinalStatus))

    return {
        "domain_nodes": len(nodes),
        "routers": len(routers),
        "terminal_nodes": len(terminals),
        "terminal_statuses": len([s for s in statuses if s != "pending"]),
        "tools": len(build_tools(ToolContext(repo_root="."))),
        "specialized_agents": len(AGENT_CLASSES),
        "total_agent_classes": len(AGENT_CLASSES) + 3,
        "sweforge_py_files": len(list((REPO_ROOT / "agent/sweforge").rglob("*.py"))),
        "tests": collect_test_count(),
    }


def collect_test_count() -> int:
    """Actual pytest collection count. Never hard-coded."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv
            [sys.executable, "-m", "pytest", "-c", "pytest-sweforge.ini", "--collect-only", "-q"],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=REPO_ROOT,
            # Inherit the environment: stripping it breaks import resolution and
            # would silently report zero tests, which is worse than no check.
            env={**os.environ, "SWEFORGE_ALLOW_LOCAL_EXEC": "1"},
        )
        # pytest emits one of two shapes depending on version and -q:
        #   "466 tests collected"           (summary line), or
        #   "tests_sweforge/test_x.py: 73"  (per-file counts)
        match = re.search(r"(\d+)\s+tests?\s+collected", result.stdout)
        if match:
            return int(match.group(1))
        per_file = re.findall(r"^\S+\.py:\s*(\d+)\s*$", result.stdout, re.MULTILINE)
        if per_file:
            return sum(int(n) for n in per_file)
        return len(re.findall(r"^tests_sweforge/\S+::", result.stdout, re.MULTILINE))
    except Exception:
        return -1


def _read(path: str) -> str:
    target = REPO_ROOT / path
    return target.read_text(encoding="utf-8") if target.exists() else ""


def _stale_numbers(text: str, pattern: str, correct: int) -> list[str]:
    """Find occurrences of `pattern` whose number is not `correct`."""
    bad: list[str] = []
    for match in re.finditer(pattern, text):
        value = int(match.group(1).replace(",", ""))
        if value != correct:
            bad.append(match.group(0))
    return bad


def run_checks() -> Report:
    truth = collect_ground_truth()
    report = Report(ground_truth=truth)

    # Each rule: a regex whose first group is a number that must equal truth.
    rules: list[tuple[str, str, int]] = [
        ("test count", r"(\d+)\s+tests?,\s+(?:no|zero) API key", truth["tests"]),
        ("test count (badge)", r"tests-(\d+)%20passing", truth["tests"]),
        ("test count (comment)", r"pytest-sweforge\.ini\s+#\s*(\d+)\s+tests", truth["tests"]),
        (
            "node count",
            r"(\d+)\s+(?:domain\s+)?nodes,\s+\d+\s+(?:conditional\s+)?routers",
            truth["domain_nodes"],
        ),
        ("router count", r"\d+\s+nodes,\s+(\d+)\s+conditional routers", truth["routers"]),
        ("tool count", r"(\d+)\s+load-bearing LangChain tools", truth["tools"]),
        (
            "sweforge file count",
            r"`agent/sweforge/`\s*\((\d+)\s+files\)",
            truth["sweforge_py_files"],
        ),
    ]

    for name, pattern, correct in rules:
        offenders: dict[str, list[str]] = {}
        for doc in LIVING_DOCS:
            bad = _stale_numbers(_read(doc), pattern, correct)
            if bad:
                offenders[doc] = bad
        report.checks.append(
            Check(
                name=name,
                expected=correct,
                ok=not offenders,
                detail=(
                    "; ".join(f"{d}: {', '.join(v)}" for d, v in offenders.items())
                    if offenders
                    else "consistent"
                ),
            )
        )

    # Claim-safety: forbidden implications must not appear in the README.
    readme = _read("README.md")
    forbidden = {
        "open-swe superiority": r"SWE-Forge (?:outperform|beats|is faster than) Open SWE",
        "benchmark performance": r"SWE-bench (?:score|result|performance) of",
        "live-model result": r"live[- ]model (?:results?|benchmark) show",
    }
    for name, pattern in forbidden.items():
        hit = re.search(pattern, readme, re.IGNORECASE)
        report.checks.append(
            Check(
                name=f"claim safety: {name}",
                expected="absent",
                ok=hit is None,
                detail=hit.group(0) if hit else "absent",
            )
        )

    # Unavailable capabilities must remain explicitly marked.
    for name, needle in (
        ("real benchmark marked unavailable", "NOT_AVAILABLE"),
        ("open swe head-to-head marked unavailable", "comparable_pairs = 0"),
        ("live model marked unavailable", "UNAVAILABLE"),
    ):
        report.checks.append(
            Check(
                name=name,
                expected="present",
                ok=needle in readme,
                detail="present" if needle in readme else f"missing marker: {needle}",
            )
        )

    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Verify documentation against source.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = run_checks()
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print("Ground truth from source:")
        for key, value in report.ground_truth.items():
            print(f"  {key:24s} {value}")
        print("\nDocumentation checks:")
        for check in report.checks:
            mark = "PASS" if check.ok else "FAIL"
            print(f"  [{mark}] {check.name:42s} {check.detail[:90]}")
        print(f"\n{len(report.checks) - len(report.failures)}/{len(report.checks)} passed")
    return 1 if report.failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
