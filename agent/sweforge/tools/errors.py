"""Tool error classification and response policy.

Phase 23 remediation of audit row 10. Previously every tool failure collapsed
into ``{"ok": false, "error": "..."}``, which made a malformed-argument bug and
a transient network blip indistinguishable — so neither could be handled
correctly.

Classification drives a decision:

===================  ==========================================================
category             action
===================  ==========================================================
validation_error     return to the agent (it must fix its own arguments)
timeout              retry with backoff, bounded
transient_error      retry with backoff, bounded
permission_error     escalate; retrying a denial wastes budget and may lock out
not_found            skip; the resource will not appear on a retry
permanent_error      escalate
===================  ==========================================================

Retries are always bounded. An autonomous system that retries indefinitely on a
permission error is a denial-of-service against its own credentials.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

ToolErrorCategory = str


class ToolErrorAction(StrEnum):
    NONE = "none"
    RETURN_TO_AGENT = "return_to_agent"
    RETRY = "retry"
    FALLBACK = "fallback"
    SKIP = "skip"
    ESCALATE = "escalate"


@dataclass
class ToolErrorClassification:
    category: ToolErrorCategory
    message: str
    retryable: bool


@dataclass
class ToolErrorDecision:
    action: ToolErrorAction
    reason: str
    backoff_seconds: float = 0.0


#: Ordered (category, pattern, retryable) rules matched against the error text.
_PATTERNS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    (
        "validation_error",
        re.compile(
            r"ValidationError|validation error|invalid argument|"
            r"TypeError|missing \d+ required",
            re.IGNORECASE,
        ),
        False,
    ),
    (
        "timeout",
        re.compile(r"\btimeout\b|timed out|TimeoutError|deadline exceeded", re.IGNORECASE),
        True,
    ),
    (
        "permission_error",
        re.compile(
            r"PermissionError|permission denied|forbidden|401|403|"
            r"unauthorized|not authorized",
            re.IGNORECASE,
        ),
        False,
    ),
    (
        "not_found",
        re.compile(r"FileNotFoundError|not found|404|does not exist|no such file", re.IGNORECASE),
        False,
    ),
    (
        "transient_error",
        re.compile(
            r"ConnectionError|connection reset|temporarily unavailable|"
            r"rate limit|429|50[23]|EAGAIN|try again",
            re.IGNORECASE,
        ),
        True,
    ),
)

_ACTIONS: dict[str, ToolErrorAction] = {
    "validation_error": ToolErrorAction.RETURN_TO_AGENT,
    "timeout": ToolErrorAction.RETRY,
    "transient_error": ToolErrorAction.RETRY,
    "permission_error": ToolErrorAction.ESCALATE,
    "not_found": ToolErrorAction.SKIP,
    "permanent_error": ToolErrorAction.ESCALATE,
}


class ToolErrorPolicy:
    """Classifies tool failures and decides what to do about them."""

    def __init__(self, *, max_retries: int = 2, base_backoff_seconds: float = 0.0) -> None:
        # Backoff defaults to 0 so unit tests do not sleep; production callers
        # set a real value.
        self.max_retries = max_retries
        self.base_backoff_seconds = base_backoff_seconds

    def classify_text(self, text: str) -> ToolErrorClassification:
        for category, pattern, retryable in _PATTERNS:
            if pattern.search(text):
                return ToolErrorClassification(category, text[:400], retryable)
        return ToolErrorClassification("permanent_error", text[:400], False)

    def classify_exception(self, exc: BaseException) -> ToolErrorClassification:
        return self.classify_text(f"{type(exc).__name__}: {exc}")

    def classify_payload(self, payload: Any) -> ToolErrorClassification:
        if isinstance(payload, dict):
            return self.classify_text(str(payload.get("error") or payload))
        return self.classify_text(str(payload))

    def decide(self, classification: ToolErrorClassification, attempt: int) -> ToolErrorDecision:
        """Choose an action, honouring the retry bound."""
        action = _ACTIONS.get(classification.category, ToolErrorAction.ESCALATE)
        if action is ToolErrorAction.RETRY:
            if attempt > self.max_retries:
                return ToolErrorDecision(
                    ToolErrorAction.ESCALATE,
                    f"{classification.category} persisted after {attempt} attempts",
                )
            return ToolErrorDecision(
                ToolErrorAction.RETRY,
                f"{classification.category} is retryable (attempt {attempt})",
                backoff_seconds=self.base_backoff_seconds * (2 ** (attempt - 1)),
            )
        return ToolErrorDecision(action, f"{classification.category} is not retryable")
