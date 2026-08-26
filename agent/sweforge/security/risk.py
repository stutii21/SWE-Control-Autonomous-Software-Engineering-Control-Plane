"""Security scanning and change-risk scoring.

Scope claim (deliberately modest)
---------------------------------
This is a **defence-in-depth risk layer**, not a security product. It is
pattern-based static screening designed to catch the specific failure mode of
an autonomous agent: quietly committing something nobody would have approved —
a hardcoded credential, an edited CI workflow, a weakened auth check, a
``rm -rf``. It will miss novel or obfuscated issues and will occasionally flag
benign code. It is not a replacement for SAST, dependency scanning, or human
review, and docs/SECURITY.md states so explicitly.

Why the score is deterministic
------------------------------
The risk gate decides whether a change may open a PR automatically or must
wait for a human. Routing that decision through an LLM would make the safety
boundary nondeterministic, so the score is computed from additive, auditable
factors. An LLM security *opinion* can be attached as findings, but it cannot
lower the gate.
"""

import re
from dataclasses import dataclass

from agent.sweforge.schemas import (
    RiskFactor,
    RiskLevel,
    RiskScore,
    SecurityFinding,
    VerificationResult,
)


# --------------------------------------------------------------------------
# Secret / dangerous-pattern rules
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SecurityRule:
    name: str
    pattern: re.Pattern[str]
    severity: str
    message: str


SECRET_RULES: tuple[SecurityRule, ...] = (
    SecurityRule(
        "aws_access_key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "blocker",
        "Possible AWS access key id committed",
    ),
    SecurityRule(
        "anthropic_key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
        "blocker",
        "Possible Anthropic API key committed",
    ),
    SecurityRule(
        "openai_key",
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"),
        "blocker",
        "Possible OpenAI API key committed",
    ),
    SecurityRule(
        "github_token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
        "blocker",
        "Possible GitHub token committed",
    ),
    SecurityRule(
        "slack_token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),
        "blocker",
        "Possible Slack token committed",
    ),
    SecurityRule(
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
        "blocker",
        "Private key material committed",
    ),
    SecurityRule(
        "hardcoded_password",
        re.compile(
            r"""(?i)\b(password|passwd|secret|api_key|apikey|token)\b\s*[:=]\s*['"][^'"\s]{8,}['"]"""
        ),
        "major",
        "Hardcoded credential-looking literal",
    ),
)

DANGEROUS_RULES: tuple[SecurityRule, ...] = (
    SecurityRule(
        "destructive_shell",
        re.compile(r"rm\s+-rf?\s+[/~]|shutil\.rmtree\(\s*['\"]?/|:\s*>\s*/dev/sd"),
        "blocker",
        "Destructive filesystem operation on an absolute path",
    ),
    SecurityRule(
        "force_push",
        re.compile(r"git\s+push\s+(?:--force|-f)\b|git\s+reset\s+--hard\s+origin"),
        "major",
        "Force push or hard reset against a remote",
    ),
    SecurityRule(
        "shell_injection",
        re.compile(r"subprocess\.(?:run|call|Popen)\([^)]*shell\s*=\s*True|os\.system\("),
        "major",
        "Shell execution with shell=True or os.system",
    ),
    SecurityRule(
        "eval_exec",
        re.compile(r"\b(?:eval|exec)\s*\(|pickle\.loads\(|yaml\.load\((?![^)]*Loader)"),
        "major",
        "Dynamic evaluation or unsafe deserialisation",
    ),
    SecurityRule(
        "verify_disabled",
        re.compile(r"verify\s*=\s*False|CURL_CA_BUNDLE\s*=\s*['\"]{2}|ssl\._create_unverified"),
        "major",
        "TLS verification disabled",
    ),
    SecurityRule(
        "auth_weakened",
        re.compile(
            r"(?i)(?:def\s+\w*(?:auth|permission|verify|validate)\w*\s*\([^)]*\)\s*:\s*(?:\n\s+)?return\s+True)"
        ),
        "blocker",
        "Authorisation function unconditionally returns True",
    ),
)

#: Paths where any modification is inherently higher risk.
SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("ci_workflow", re.compile(r"^\.github/(?:workflows|actions)/"), 30),
    (
        "dependency_manifest",
        re.compile(
            r"(?:^|/)(?:requirements[^/]*\.txt|pyproject\.toml|package\.json|"
            r"pnpm-lock\.yaml|uv\.lock|poetry\.lock|Cargo\.toml|go\.mod)$"
        ),
        18,
    ),
    ("container", re.compile(r"(?:^|/)(?:Dockerfile[^/]*|docker-compose[^/]*\.ya?ml)$"), 18),
    (
        "auth_code",
        re.compile(r"(?i)(?:^|/)(?:auth|authn|authz|login|session|permission|rbac)[^/]*\.\w+$"),
        22,
    ),
    (
        "security_code",
        re.compile(r"(?i)(?:^|/)(?:security|crypt|encryption|secret|token|credential)[^/]*\.\w+$"),
        22,
    ),
    (
        "infra_as_code",
        re.compile(r"(?:^|/)(?:.*\.tf|.*\.tfvars|helm/.*|k8s/.*|kustomization\.ya?ml)$"),
        20,
    ),
    ("env_file", re.compile(r"(?:^|/)\.env(?:\..+)?$"), 35),
    ("build_script", re.compile(r"(?:^|/)(?:Makefile|setup\.py|build\.gradle|.*\.sh)$"), 12),
)

BINARY_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".whl",
    ".so",
    ".dylib",
    ".dll",
)

THRESHOLD_MEDIUM = 25
THRESHOLD_HIGH = 55


@dataclass
class ChangeSet:
    """The set of edits being assessed."""

    files: dict[str, str]  # path -> new content
    deleted: list[str] | None = None

    @property
    def paths(self) -> list[str]:
        return sorted(self.files)

    def total_lines(self) -> int:
        return sum(content.count("\n") + 1 for content in self.files.values())


class SecurityScanner:
    """Pattern-based static screening of changed content."""

    def __init__(self, *, extra_rules: tuple[SecurityRule, ...] = ()) -> None:
        self.rules = (*SECRET_RULES, *DANGEROUS_RULES, *extra_rules)

    def scan(self, changes: ChangeSet) -> list[SecurityFinding]:
        findings: list[SecurityFinding] = []
        for path, content in sorted(changes.files.items()):
            if path.endswith(BINARY_SUFFIXES):
                continue
            # Example/placeholder files legitimately contain fake credentials.
            is_example = bool(
                re.search(r"(?:^|/)\.env\.example$|(?:^|/)(?:examples?|fixtures?|docs?)/", path)
            )
            for rule in self.rules:
                for match in rule.pattern.finditer(content):
                    line_no = content.count("\n", 0, match.start()) + 1
                    severity = rule.severity
                    if is_example and rule in SECRET_RULES:
                        severity = "info"
                    findings.append(
                        SecurityFinding(
                            rule=rule.name,
                            severity=severity,  # type: ignore[arg-type]
                            file=path,
                            line=line_no,
                            message=rule.message,
                        )
                    )
                    break  # one finding per rule per file is enough signal
        return findings


class RiskEngine:
    """Computes an additive, auditable risk score for a change set."""

    def __init__(
        self,
        *,
        large_diff_lines: int = 400,
        many_files: int = 15,
        scanner: SecurityScanner | None = None,
    ) -> None:
        self.large_diff_lines = large_diff_lines
        self.many_files = many_files
        self.scanner = scanner or SecurityScanner()

    def assess(
        self,
        changes: ChangeSet,
        *,
        verification: VerificationResult | None = None,
        findings: list[SecurityFinding] | None = None,
        review_rejected: bool = False,
        recovery_attempts: int = 0,
    ) -> RiskScore:
        findings = findings if findings is not None else self.scanner.scan(changes)
        factors: list[RiskFactor] = []

        # 1. Security findings dominate.
        blockers = [f for f in findings if f.severity == "blocker"]
        majors = [f for f in findings if f.severity == "major"]
        if blockers:
            factors.append(
                RiskFactor(
                    code="security_blocker",
                    weight=60,
                    detail=f"{len(blockers)} blocker finding(s): "
                    + ", ".join(sorted({f.rule for f in blockers})),
                )
            )
        if majors:
            factors.append(
                RiskFactor(
                    code="security_major",
                    weight=min(30, 12 * len(majors)),
                    detail=f"{len(majors)} major finding(s): "
                    + ", ".join(sorted({f.rule for f in majors})),
                )
            )

        # 2. Sensitive paths touched.
        for code, pattern, weight in SENSITIVE_PATTERNS:
            hits = [p for p in changes.paths if pattern.search(p)]
            if hits:
                factors.append(
                    RiskFactor(
                        code=f"sensitive_{code}",
                        weight=weight,
                        detail=f"modified {len(hits)} {code} file(s): {', '.join(hits[:3])}",
                    )
                )

        # 3. Deletions of non-test files.
        deletions = [p for p in (changes.deleted or []) if not p.startswith(("test", "tests/"))]
        if deletions:
            factors.append(
                RiskFactor(
                    code="file_deletion",
                    weight=min(25, 8 * len(deletions)),
                    detail=f"deletes {len(deletions)} file(s): {', '.join(deletions[:3])}",
                )
            )

        # 4. Diff size.
        lines = changes.total_lines()
        if lines > self.large_diff_lines:
            factors.append(
                RiskFactor(
                    code="large_diff",
                    weight=min(20, 10 + lines // 400),
                    detail=f"{lines} lines across {len(changes.paths)} files",
                )
            )
        if len(changes.paths) > self.many_files:
            factors.append(
                RiskFactor(
                    code="many_files",
                    weight=12,
                    detail=f"{len(changes.paths)} files touched",
                )
            )

        # 5. Verification state.
        if verification is not None and not verification.passed:
            factors.append(
                RiskFactor(
                    code="verification_failed",
                    weight=40,
                    detail=f"verification not green ({verification.summary()})",
                )
            )
        elif verification is None:
            factors.append(
                RiskFactor(
                    code="unverified",
                    weight=25,
                    detail="no verification result available",
                )
            )

        # 6. Assurance signals.
        if review_rejected:
            factors.append(
                RiskFactor(
                    code="review_rejected",
                    weight=30,
                    detail="independent reviewer withheld approval",
                )
            )
        if recovery_attempts >= 2:
            factors.append(
                RiskFactor(
                    code="repeated_recovery",
                    weight=10 + 5 * (recovery_attempts - 2),
                    detail=f"required {recovery_attempts} recovery attempts",
                )
            )

        score = min(100, sum(f.weight for f in factors))
        level = self._level(score)
        return RiskScore(
            score=score,
            level=level,
            factors=factors,
            recommendation=self._recommendation(level, factors),
        )

    @staticmethod
    def _level(score: int) -> RiskLevel:
        if score >= THRESHOLD_HIGH:
            return "HIGH"
        if score >= THRESHOLD_MEDIUM:
            return "MEDIUM"
        return "LOW"

    @staticmethod
    def _recommendation(level: RiskLevel, factors: list[RiskFactor]) -> str:
        top = ", ".join(f.code for f in sorted(factors, key=lambda f: -f.weight)[:3])
        if level == "HIGH":
            return f"Require human approval before opening a PR. Drivers: {top or 'n/a'}."
        if level == "MEDIUM":
            return f"Open a draft PR with enhanced review and reviewer notes. Drivers: {top}."
        return "Safe to open a PR automatically under the configured policy."
