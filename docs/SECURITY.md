# Security

## Scope of the claim

SWE-Forge's security layer is a **defence-in-depth risk screen**, not a security
product. It exists to catch one specific failure mode: an autonomous agent quietly
committing something no human would have approved.

**This is not** SAST, dependency/CVE scanning, secret rotation, or a substitute for
human review. It is pattern-based static screening. It will miss novel or obfuscated
issues and will sometimes flag benign code. Do not deploy it as your only control.

Upstream Open SWE already provides **access control** — who may run the agent
(`agent/utils/auth.py`, `github_org_membership.py`, `admin_gate.py`) and guards on PR
creation (`pr_creation_guard`, `workflow_push_guard`). SWE-Forge adds the missing
complement: assessment of the *content* the agent produced. The two are not substitutes.

## Threat model

The adversary SWE-Forge is designed against is mostly **the agent itself** — a
well-intentioned model taking a plausible action with bad consequences.

| Threat | Control | Where |
|---|---|---|
| Untrusted repository code executes on the host | All verification runs in the Open SWE sandbox; the host backend is env-gated and refuses by default | `verification/backends.py` |
| Agent commits a credential | Secret pattern scan → `blocker` finding → +60 risk → HIGH → human approval | `security/risk.py` |
| Agent edits CI/CD to run arbitrary code | `.github/workflows/` matched as sensitive path (+30) | `SENSITIVE_PATTERNS` |
| Agent weakens an auth check | Pattern for auth functions that unconditionally `return True` | `DANGEROUS_RULES` |
| Agent runs a destructive command | `rm -rf /`, `shutil.rmtree('/`, force push, hard reset | `DANGEROUS_RULES` |
| Agent introduces unsafe deserialisation / shell injection | `eval`/`exec`, `pickle.loads`, unsafe `yaml.load`, `shell=True`, `os.system` | `DANGEROUS_RULES` |
| Agent disables TLS verification | `verify=False`, `ssl._create_unverified` | `DANGEROUS_RULES` |
| Agent writes outside the repository | `FileEdit` validator rejects absolute paths and `..`; backend re-checks | `schemas.py`, `backends.py` |
| Agent loops forever burning budget | Recovery bound enforced by a routing function, not a prompt | `graph/workflow.py` |
| Agent ships an unverified change | `unverified` (+25) and `verification_failed` (+40) risk factors | `security/risk.py` |
| Credential leaks into logs or traces | Router never reads or logs keys; `describe_configuration` reports presence only | `routing/`, `observability/` |

## Execution isolation

Verification executes repository code, which is untrusted by definition.

**Production path:** `OpenSWESandboxBackend` delegates to upstream's sandbox
infrastructure (Daytona / Modal / E2B / Runloop via the `deepagents`
`SandboxBackendProtocol`). SWE-Forge implements **no sandbox of its own** — reusing
mature isolation is the correct call, and reimplementing it would only weaken it.

**Fixture path:** `LocalSubprocessBackend` runs commands on the host and exists solely
for the evaluation fixtures that SWE-Forge itself ships. It refuses to construct unless
`SWEFORGE_ALLOW_LOCAL_EXEC=1` is set explicitly:

```python
if environ.get(LOCAL_EXEC_ENV) != "1":
    raise LocalExecutionForbidden(
        "Refusing to execute repository code on the host. "
        "Set SWEFORGE_ALLOW_LOCAL_EXEC=1 only for SWE-Forge's own evaluation "
        "fixtures; use OpenSWESandboxBackend for real repositories."
    )
```

It also confines all file operations to the fixture root, rejecting path escapes. Tested
by `test_local_backend_refuses_without_optin` and `test_path_escape_is_blocked`.

## Why the risk score is deterministic

The gate decides whether a change may open a PR automatically or must wait for a human.
Routing that decision through an LLM would make the safety boundary nondeterministic —
the same diff could be approved on one run and blocked on the next.

So the score is computed from additive, auditable factors. An LLM security *opinion* can
attach findings, but it cannot lower the gate. `test_scoring_is_deterministic` asserts
repeatability.

### Scoring

| Factor | Weight |
|---|---|
| Security blocker finding | 60 |
| Verification failed | 40 |
| `.env` file modified | 35 |
| CI workflow modified | 30 |
| Review rejected | 30 |
| Security major finding(s) | 12 per, capped 30 |
| Auth / security module modified | 22 |
| Infrastructure-as-code modified | 20 |
| Unverified change | 25 |
| Dependency manifest / container modified | 18 |
| Non-test file deletion | 8 per, capped 25 |
| Large diff / many files | 12–20 |
| Build script modified | 12 |
| Repeated recovery (≥2 attempts) | 10+ |

Thresholds: **≥55 HIGH** (human approval required), **≥25 MEDIUM** (draft PR, enhanced
review), **<25 LOW** (automatic PR permitted by policy).

Worked example — the `pipeline_secret_risk_gate` scenario, verification green and
reviewer approving:

```
+60  security_blocker         github_token committed in deploy.py
+30  sensitive_ci_workflow    .github/workflows/ci.yml modified
────
 90/100 → HIGH → awaiting_human_approval
```

Every other workflow variant shipped that change. See `docs/EVALUATION.md` §5.4.

## Secret handling

- **No credential is ever committed.** `.env` and `*.pem` are gitignored;
  `.env.example` contains placeholders only.
- **No credential is read by SWE-Forge's own modules.** The router resolves model *ids*
  from env vars; provider SDKs read their own keys. A test asserts a key value never
  reaches a routing decision (`test_no_api_key_is_read_from_env`).
- **No credential appears in diagnostics.** `describe_configuration()` reports
  `api_key_configured: true/false`, never a value
  (`test_describe_configuration_hides_key_value`).
- **Test fixtures contain no real secrets.** Scanner tests use PEM *headers* with no key
  material. The GitHub-token fixture in `evaluation/scenarios.py` is assembled at
  runtime (`"ghp_" + "A" * 36`) specifically so no secret-shaped literal is committed and
  no external scanner is tripped.

A repository-wide sweep for AWS/Anthropic/OpenAI/GitHub/Slack key patterns, private keys
and assigned-secret literals returns only those PEM headers in the scanner's own tests.

## Reporting and known gaps

Follow upstream `SECURITY.md` for vulnerability reporting.

Known gaps, stated rather than hidden:

1. Regex screening is defeatable by obfuscation (string concatenation, encoding).
2. No taint analysis or data-flow tracking.
3. No dependency CVE scanning — a malicious *version bump* is flagged only as a
   sensitive-manifest edit.
4. Non-Python files are pattern-scanned but not parsed.
5. Risk weights are hand-tuned judgement calls, not empirically derived.
6. The human-approval gate is a terminal state; enforcement of the approval itself
   belongs to the surrounding system (upstream PR guards, branch protection).

---

# Phase 23/24 additions

## Execution budgets as a safety control

Budgets are a safety mechanism, not only a cost mechanism: they bound how much
damage a misbehaving autonomous loop can do. All eight limits are enforced in
Python *before* the operation and were each verified to raise `BudgetExceeded`.
The model cannot see, negotiate or raise them, and exhaustion routes to the
explicit `budget_exhausted` terminal state. See [EXECUTION_BUDGETS.md](EXECUTION_BUDGETS.md).

## MCP: deny-by-default

An autonomous agent reaching arbitrary external services is a security problem,
not a feature. `MCPInvocationPolicy` therefore denies by default: a capability
absent from the explicit allowlist is never invoked (`permission_error`).
Additional controls: per-run call cap, timeout, bounded retry, execution-budget
consumption, and structured errors that never raise into the agent loop.

Fixture payloads are labelled `_fixture: true` so test data can never be mistaken
for a real external result.

## Risk-gated pull requests

`agent/sweforge/github/finalization.py` gates PR preparation on the risk score:
LOW → open, MEDIUM → draft with reviewer notes, HIGH → blocked pending human
approval. A failed verification or a withheld review also blocks. Creation
requires `allow_creation=True` **and** an injected upstream creator, so no test or
dry run can open an external PR by accident. Live GitHub creation is UNAVAILABLE
in this environment.

## Secret-scan result (Phase 24)

Repository-wide scan across `agent/sweforge/`, `evaluation/`, `tests_sweforge/`,
`docs/` and root configs (107 files) for AWS/Anthropic/OpenAI/GitHub/Slack/Linear/
LangSmith keys, private keys, OAuth secrets and assigned-secret literals:

* **5 matches, all PEM headers with no key material** — 3 in the scanner's own
  tests, 2 in upstream's `docs/INSTALLATION.md` (GitHub App setup instructions).
* **0 non-PEM matches.** No `.env` file present. `.env.example` contains
  placeholders only. `.gitignore` covers `.env`, `.env.*`, `*.pem`,
  `credentials.json`, `.sweforge/`, with `!.env.example` negated so the template
  is committable.
