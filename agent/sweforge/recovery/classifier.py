"""Deterministic failure classification.

Design decision: classification happens **before** any LLM sees the failure.

A stack trace has an unambiguous category — ``ModuleNotFoundError`` is a
dependency problem, ``SyntaxError`` is a syntax problem — and asking a model to
label it wastes a call, adds latency, and introduces nondeterminism into
control flow. So SWE-Forge classifies with ordered regex rules and then uses
the LLM only for the genuinely open question: *what should we change?*

Rule order matters. An ``ImportError`` raised inside a test file is a
dependency failure even though pytest reports it as a test error, so
import/syntax rules are evaluated before assertion rules.
"""

import re
from dataclasses import dataclass, field

from agent.sweforge.schemas import FailureCategory, VerificationResult


@dataclass(frozen=True)
class ClassificationRule:
    category: FailureCategory
    pattern: re.Pattern[str]
    description: str
    weight: int = 1


#: Evaluated in order; first matching category wins the primary label.
RULES: tuple[ClassificationRule, ...] = (
    ClassificationRule(
        "syntax",
        re.compile(r"\b(SyntaxError|IndentationError|TabError)\b"),
        "Python failed to parse the source",
        weight=5,
    ),
    ClassificationRule(
        "dependency",
        re.compile(r"\b(ModuleNotFoundError|ImportError)\b|No module named"),
        "A module could not be imported",
        weight=4,
    ),
    ClassificationRule(
        "timeout",
        re.compile(r"timed out after|\bTimeoutError\b|Timeout >\d+", re.IGNORECASE),
        "Execution exceeded its time budget",
        weight=4,
    ),
    ClassificationRule(
        "type",
        re.compile(r"\bTypeError\b|\bAttributeError\b|error: Argument .* has incompatible type"),
        "Type or attribute contract violated",
        weight=3,
    ),
    ClassificationRule(
        "configuration",
        # NOTE: the quoted-key alternative must NOT carry a trailing \b — the
        # match ends on an apostrophe, and a word boundary after a non-word
        # character requires a following word character, so `KeyError: 'X'` at
        # end-of-line could never match. Found by the Phase 25 recovery matrix.
        re.compile(
            r"KeyError:\s*'[A-Z_]{3,}'"
            r"|\b(?:ConfigError|ValidationError)\b"
            r"|pydantic_core\._pydantic_core\.ValidationError"
            r"|missing required (?:environment|config)",
        ),
        "Configuration or schema validation problem",
        weight=3,
    ),
    ClassificationRule(
        "environment",
        re.compile(
            r"\b(PermissionError|FileNotFoundError|OSError|ConnectionError|executable not found)\b"
            r"|command not found",
        ),
        "Environment, filesystem or network problem",
        weight=3,
    ),
    # Runtime is checked BEFORE test_assertion on purpose: pytest echoes the
    # failing source line (`>  assert add(2, 3) == 5`) even when the real cause
    # is an exception raised inside the call, so an explicitly named exception
    # is stronger evidence than the presence of the word "assert".
    ClassificationRule(
        "runtime",
        re.compile(
            r"\b(ValueError|IndexError|ZeroDivisionError|RuntimeError|RecursionError|KeyError)\b"
        ),
        "Unhandled runtime exception",
        weight=2,
    ),
    ClassificationRule(
        "test_assertion",
        # Only genuine assertion evidence: the exception itself, or pytest's
        # error-prefixed assertion line. Not a bare "assert x == y" source echo.
        re.compile(r"\bAssertionError\b|^E\s+assert\b", re.MULTILINE),
        "A test assertion did not hold",
        weight=2,
    ),
    ClassificationRule(
        "lint",
        re.compile(r"^\S+\.py:\d+:\d+: [A-Z]+\d+ ", re.MULTILINE),
        "Lint rule violation",
        weight=1,
    ),
)

#: Files named in a traceback line: `  File "pkg/mod.py", line 12, in fn`
_TRACEBACK_FILE_RE = re.compile(r'File "([^"]+\.py)", line (\d+)')
#: pytest failure headers: `FAILED tests/test_x.py::test_y - AssertionError`
_PYTEST_FAILED_RE = re.compile(r"FAILED\s+([^\s:]+\.py)(::\S+)?")
#: ruff / mypy style `path.py:12:3: CODE msg`
_TOOL_FILE_RE = re.compile(r"^([\w./\-]+\.py):(\d+)", re.MULTILINE)


@dataclass
class Classification:
    """Result of deterministic triage."""

    category: FailureCategory
    evidence: list[str] = field(default_factory=list)
    matched_rules: list[str] = field(default_factory=list)
    suspect_files: list[str] = field(default_factory=list)
    failing_tests: list[str] = field(default_factory=list)
    confidence: float = 0.5

    def describe(self) -> str:
        head = f"{self.category} failure"
        if self.evidence:
            head += f": {self.evidence[0][:200]}"
        return head


class FailureClassifier:
    """Classifies a :class:`VerificationResult` without any model call."""

    def __init__(self, *, known_files: set[str] | None = None) -> None:
        # When the repository map is available, extracted paths are filtered to
        # real repository files, which removes site-packages frames from noise.
        self.known_files = known_files

    def classify(self, result: VerificationResult) -> Classification:
        text = "\n".join([*result.errors, result.output])
        if result.passed:
            return Classification(
                category="unknown", confidence=0.0, evidence=["verification passed"]
            )

        matched: list[ClassificationRule] = [r for r in RULES if r.pattern.search(text)]

        if not matched:
            category: FailureCategory = "unknown"
            confidence = 0.2
        else:
            category = matched[0].category
            # Confidence rises with rule specificity and falls when several
            # different categories match (ambiguous output).
            distinct = {r.category for r in matched}
            confidence = min(0.95, 0.5 + 0.1 * matched[0].weight - 0.1 * (len(distinct) - 1))
            confidence = max(0.25, round(confidence, 3))

        return Classification(
            category=category,
            evidence=self._evidence(text, matched),
            matched_rules=[r.category for r in matched],
            suspect_files=self._suspect_files(text),
            failing_tests=self._failing_tests(text),
            confidence=confidence,
        )

    # -- extraction --------------------------------------------------------
    @staticmethod
    def _evidence(text: str, matched: list[ClassificationRule]) -> list[str]:
        evidence: list[str] = []
        for rule in matched[:3]:
            hit = rule.pattern.search(text)
            if not hit:
                continue
            start = max(0, hit.start() - 60)
            snippet = text[start : hit.end() + 140].strip().replace("\n", " | ")
            evidence.append(snippet[:300])
        return evidence

    def _suspect_files(self, text: str) -> list[str]:
        candidates: list[str] = []
        # Deepest traceback frames are most informative, so reverse them.
        for path, _line in reversed(_TRACEBACK_FILE_RE.findall(text)):
            candidates.append(path)
        for path, _sel in _PYTEST_FAILED_RE.findall(text):
            candidates.append(path)
        for path, _line in _TOOL_FILE_RE.findall(text):
            candidates.append(path)

        cleaned: list[str] = []
        for raw in candidates:
            # Filter on the ORIGINAL path: normalising first would strip the
            # leading slash and let /usr/lib frames slip through.
            if (
                "site-packages" in raw
                or "dist-packages" in raw
                or raw.startswith(("/usr/", "/lib/", "/opt/"))
                or "/python3." in raw
            ):
                continue
            path = raw.lstrip("./")
            if self.known_files is not None and path not in self.known_files:
                continue
            if path not in cleaned:
                cleaned.append(path)
        return cleaned[:8]

    @staticmethod
    def _failing_tests(text: str) -> list[str]:
        tests: list[str] = []
        for path, selector in _PYTEST_FAILED_RE.findall(text):
            name = f"{path}{selector}" if selector else path
            if name not in tests:
                tests.append(name)
        return tests[:10]
