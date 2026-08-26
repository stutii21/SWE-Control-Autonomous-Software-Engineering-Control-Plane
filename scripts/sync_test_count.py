"""Propagate the authoritative pytest collection count into documentation.

Adding a test changes the count, which is exactly why hand-maintained numbers
rot. This derives the count from pytest and rewrites every documented
occurrence, so the fix is one command rather than a manual sweep.
"""

import os
import pathlib
import re
import subprocess
import sys

DOCS = (
    "README.md",
    "docs/CUSTOMIZATIONS.md",
    "docs/DEMO.md",
    "docs/INTERVIEW_GUIDE.md",
    "docs/PROJECT_CLAIMS.md",
    "docs/RESUME_CLAIMS.md",
    "docs/EVALUATION.md",
)

PATTERNS = (
    (r"tests-\d+%20passing", "tests-{n}%20passing"),
    (r"(\d+) tests, (no|zero) API key", None),  # handled specially
    (r"(pytest -c pytest-sweforge\.ini\s+#\s*)\d+( tests)", None),
    (r"`tests_sweforge/` — \d+ tests", "`tests_sweforge/` — {n} tests"),
    (r"### `tests_sweforge/` — \d+ tests", "### `tests_sweforge/` — {n} tests"),
    (r"\*\*\d+ passing\*\*", "**{n} passing**"),
    (r"\*\*\d+ passed\*\*", "**{n} passed**"),
    (r"<- \d+ tests, no API key", "<- {n} tests, no API key"),
    (r"\(\d+ tests\)", "({n} tests)"),
    (r"^(TESTS:\s*)\d+$", None),
)


def collected() -> int:
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-c", "pytest-sweforge.ini", "--collect-only", "-q"],
        capture_output=True,
        text=True,
        env={**os.environ, "SWEFORGE_ALLOW_LOCAL_EXEC": "1"},
    )
    match = re.search(r"(\d+)\s+tests?\s+collected", out.stdout)
    if match:
        return int(match.group(1))
    per_file = re.findall(r"^\S+\.py:\s*(\d+)\s*$", out.stdout, re.MULTILINE)
    return sum(int(x) for x in per_file)


def main() -> int:
    n = collected()
    print(f"authoritative collected count: {n}")
    for path in DOCS:
        target = pathlib.Path(path)
        if not target.exists():
            continue
        text = original = target.read_text(encoding="utf-8")
        text = re.sub(r"tests-\d+%20passing", f"tests-{n}%20passing", text)
        text = re.sub(
            r"(\d+) tests, (no|zero) API key", lambda m: f"{n} tests, {m.group(2)} API key", text
        )
        text = re.sub(
            r"(pytest -c pytest-sweforge\.ini\s+#\s*)\d+( tests)", rf"\g<1>{n}\g<2>", text
        )
        text = re.sub(r"`tests_sweforge/` — \d+ tests", f"`tests_sweforge/` — {n} tests", text)
        text = re.sub(
            r"### `tests_sweforge/` — \d+ tests", f"### `tests_sweforge/` — {n} tests", text
        )
        text = re.sub(r"\*\*\d+ passing\*\*", f"**{n} passing**", text)
        text = re.sub(r"<- \d+ tests, no API key", f"<- {n} tests, no API key", text)
        text = re.sub(r"\(\d+ tests\)", f"({n} tests)", text)
        text = re.sub(r"^(TESTS:\s*)\d+$", rf"\g<1>{n}", text, flags=re.MULTILINE)
        if text != original:
            target.write_text(text, encoding="utf-8")
            print(f"  updated {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
