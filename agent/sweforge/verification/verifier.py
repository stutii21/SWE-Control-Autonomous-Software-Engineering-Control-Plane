"""Self-verification engine.

Selects and runs the narrowest useful validation for a change, then converts
raw tool output into a :class:`VerificationResult`. Two things matter here:

1. **Command selection is driven by repository intelligence.** Given the files
   a change touched, the import graph supplies the tests that actually cover
   them, so verification runs targeted tests before (optionally) the full
   suite. That is the difference between a 4-second signal and a 6-minute one.

2. **Parsing is structural, not vibes.** pytest's summary line is parsed with
   anchored regexes and cross-checked against the exit code, so "passed" is a
   fact derived from the runner, never an LLM's opinion about its own work.
"""

import re
import time
from dataclasses import dataclass

from agent.sweforge.repository.graph_index import RepositoryGraph
from agent.sweforge.schemas import VerificationResult
from agent.sweforge.verification.backends import ExecResult, ExecutionBackend

# The final pytest summary line, e.g. "===== 3 failed, 5 passed in 0.42s =====".
# Counts are parsed ONLY from this line: strings like "Interrupted: 1 error
# during collection" also contain "1 error" and would double-count.
_PYTEST_SUMMARY_LINE_RE = re.compile(
    r"^=*\s*(?:\d+\s+\w+(?:,\s*)?)+\s+in\s+[\d.]+s.*$", re.MULTILINE
)
_PYTEST_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)")
_PYTEST_NO_TESTS_RE = re.compile(r"no tests ran", re.IGNORECASE)
# A tool invoked via `python -m X` that isn't installed. This is a missing
# linter, not a defect in the repository, and must not fail verification.
_MODULE_MISSING_RE = re.compile(r"No module named (\S+)")
_COLLECT_ERROR_RE = re.compile(r"(ERROR collecting|INTERNALERROR|ImportError while importing)")
_MAX_OUTPUT_CHARS = 12_000


@dataclass
class VerificationPlan:
    """The concrete commands verification will run, in order."""

    test_commands: list[str]
    lint_command: str | None = None
    typecheck_command: str | None = None
    targeted: bool = True

    @property
    def all_commands(self) -> list[str]:
        out = list(self.test_commands)
        if self.lint_command:
            out.append(self.lint_command)
        if self.typecheck_command:
            out.append(self.typecheck_command)
        return out


class Verifier:
    """Runs validation inside an execution backend and structures the result."""

    def __init__(
        self,
        backend: ExecutionBackend,
        *,
        graph: RepositoryGraph | None = None,
        timeout: int = 300,
        enable_lint: bool = True,
        enable_typecheck: bool = False,
    ) -> None:
        self.backend = backend
        self.graph = graph
        self.timeout = timeout
        self.enable_lint = enable_lint
        self.enable_typecheck = enable_typecheck
        #: The most recent full result, including raw output. The run_validation
        #: tool returns a trimmed payload (output excluded, to keep tool results
        #: prompt-sized); the graph reads the full object from here so failure
        #: classification still sees the runner output it needs.
        self.last_result: VerificationResult | None = None

    # -- planning ----------------------------------------------------------
    def build_plan(self, changed_files: list[str], *, full_suite: bool = False) -> VerificationPlan:
        """Pick the narrowest validation that still covers the change."""
        python_changes = [f for f in changed_files if f.endswith(".py")]
        tests: list[str] = []

        if not full_suite and self.graph is not None and python_changes:
            related: list[str] = []
            for path in python_changes:
                if path in self.graph.map.files and self.graph.map.files[path].is_test:
                    related.append(path)
                related.extend(self.graph.find_tests_for_file(path))
            unique = sorted(dict.fromkeys(related))
            if unique:
                tests.append(f"python -m pytest {' '.join(unique)} -q --no-header")

        if not tests:
            tests.append("python -m pytest -q --no-header")
            targeted = False
        else:
            targeted = True

        lint = None
        if self.enable_lint and python_changes:
            lint = f"python -m ruff check {' '.join(python_changes)}"

        typecheck = None
        if self.enable_typecheck and python_changes:
            typecheck = f"python -m mypy {' '.join(python_changes)}"

        return VerificationPlan(
            test_commands=tests,
            lint_command=lint,
            typecheck_command=typecheck,
            targeted=targeted,
        )

    # -- execution ---------------------------------------------------------
    def verify(self, changed_files: list[str], *, full_suite: bool = False) -> VerificationResult:
        plan = self.build_plan(changed_files, full_suite=full_suite)
        started = time.perf_counter()
        outputs: list[str] = []
        errors: list[str] = []
        tests_run = tests_passed = tests_failed = 0
        tests_ok = True

        for command in plan.test_commands:
            result = self.backend.run(command, timeout=self.timeout)
            outputs.append(f"$ {command}\n{result.combined_output}")
            counts = self._parse_pytest(result)
            tests_run += counts["run"]
            tests_passed += counts["passed"]
            tests_failed += counts["failed"]
            if not result.ok:
                tests_ok = False
                errors.extend(self._extract_errors(result))

        lint_passed = self._run_gate(plan.lint_command, outputs, errors)
        typecheck_passed = self._run_gate(plan.typecheck_command, outputs, errors)

        # A change is verified only when tests pass AND no enabled gate failed.
        passed = tests_ok and (lint_passed is not False) and (typecheck_passed is not False)

        self.last_result = VerificationResult(
            passed=passed,
            tests_run=tests_run,
            tests_passed=tests_passed,
            tests_failed=tests_failed,
            lint_passed=lint_passed,
            typecheck_passed=typecheck_passed,
            errors=errors[:20],
            commands=plan.all_commands,
            output="\n\n".join(outputs)[-_MAX_OUTPUT_CHARS:],
            duration_seconds=round(time.perf_counter() - started, 4),
        )
        return self.last_result

    def _run_gate(self, command: str | None, outputs: list[str], errors: list[str]) -> bool | None:
        """Run an optional lint/typecheck gate.

        Returns ``None`` when the tool is not installed in the execution
        environment. A missing linter is an environment gap, not a defect in
        the change under test, so it must not turn a passing change red.
        """
        if not command:
            return None
        result = self.backend.run(command, timeout=self.timeout)
        missing = _MODULE_MISSING_RE.search(result.combined_output)
        # When the tool is absent, record ONLY the skip note. Its raw stderr
        # ("No module named ruff") otherwise pollutes failure classification,
        # which reads this output and would label the run a dependency failure.
        if result.exit_code == 127 or (result.exit_code == 1 and missing):
            detail = f"{missing.group(1)} not installed" if missing else "executable not found"
            outputs.append(f"$ {command}\n[sweforge] gate skipped: {detail}")
            return None
        outputs.append(f"$ {command}\n{result.combined_output}")
        if not result.ok:
            errors.extend(self._extract_errors(result, limit=5))
        return result.ok

    # -- parsing -----------------------------------------------------------
    @staticmethod
    def _parse_pytest(result: ExecResult) -> dict[str, int]:
        """Extract test counts from pytest output.

        Exit code alone is insufficient (exit 1 covers both "assertion failed"
        and "collection error"), and the summary line alone is insufficient
        (absent on a crash), so both are used.
        """
        text = result.combined_output
        counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
        summary_lines = _PYTEST_SUMMARY_LINE_RE.findall(text)
        # Use the last summary line only; earlier lines repeat the counts.
        scope = summary_lines[-1] if summary_lines else ""
        for number, label in _PYTEST_COUNT_RE.findall(scope):
            key = "error" if label.startswith("error") else label
            if key in counts:
                counts[key] += int(number)
            elif key in {"xfailed", "xpassed"}:
                counts["passed"] += int(number)

        failed = counts["failed"] + counts["error"]
        passed = counts["passed"]

        if passed == 0 and failed == 0 and not result.ok:
            # Crash or collection error with no parsable summary.
            failed = 1
        return {
            "run": passed + failed + counts["skipped"],
            "passed": passed,
            "failed": failed,
        }

    @staticmethod
    def _extract_errors(result: ExecResult, *, limit: int = 12) -> list[str]:
        """Pull the lines a diagnostician would actually read."""
        interesting: list[str] = []
        for line in result.combined_output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if (
                stripped.startswith(("E   ", "FAILED", "ERROR"))
                or _COLLECT_ERROR_RE.search(stripped)
                or re.match(r"^\S+\.py:\d+:\d+: [A-Z]\d+", stripped)  # ruff
                or re.match(r"^\S+\.py:\d+: error:", stripped)  # mypy
            ):
                interesting.append(stripped[:400])
            if len(interesting) >= limit:
                break
        if not interesting and not result.ok:
            tail = result.combined_output.strip().splitlines()[-3:]
            interesting = [line.strip()[:400] for line in tail if line.strip()]
        return interesting
