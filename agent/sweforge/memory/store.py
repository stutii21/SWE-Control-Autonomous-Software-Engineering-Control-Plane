"""Experience memory: retrieval-based planning context.

Terminology
-----------
This is **experience memory / historical task retrieval**, used for
**experience-aware planning**. Nothing here trains or fine-tunes a model. A
completed run is written to an append-only JSONL log; before planning a new
task, similar past runs are retrieved and injected into the planner prompt as
context.

Why lexical rather than embeddings
---------------------------------
A vector store was considered and deliberately rejected for the default path.
The corpus is small (tens to thousands of records), the queries are
identifier-heavy ("fix the model fallback middleware retry"), and BM25-style
lexical scoring on identifier tokens outperforms dense similarity on exactly
that kind of query — while adding no service dependency, no embedding cost,
and staying fully deterministic, which the evaluation harness requires.

An embedding backend can be plugged in via :class:`EmbeddingBackend` when a
corpus grows large enough to justify it, but it is opt-in, not the default.
"""

import math
import os
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent.sweforge.repository.graph_index import tokenize
from agent.sweforge.schemas import ExperienceRecord

DEFAULT_MEMORY_PATH = ".sweforge/experience.jsonl"


class EmbeddingBackend(Protocol):
    """Optional dense-retrieval hook. Not used by default."""

    def embed(self, text: str) -> list[float]: ...


@dataclass
class RetrievedExperience:
    record: ExperienceRecord
    score: float
    matched_terms: list[str]


class ExperienceStore:
    """Append-only JSONL experience log with BM25 retrieval.

    Thread-safe for the append path; reads snapshot the in-memory index.
    """

    def __init__(self, path: str | Path | None = None, *, k1: float = 1.5, b: float = 0.75) -> None:
        self.path = Path(path or os.environ.get("SWEFORGE_MEMORY_PATH", DEFAULT_MEMORY_PATH))
        self.k1 = k1
        self.b = b
        self._lock = threading.Lock()
        self._records: list[ExperienceRecord] = []
        self._docs: list[list[str]] = []
        self._loaded = False

    # -- persistence -------------------------------------------------------
    def _document_terms(self, record: ExperienceRecord) -> list[str]:
        """The searchable surface of a record."""
        parts = [
            record.task,
            record.task_type,
            record.repository,
            record.strategy,
            record.lesson,
            " ".join(record.relevant_files),
            " ".join(record.languages),
            " ".join(record.failure_categories),
            " ".join(record.recovery_strategies),
        ]
        return tokenize(" ".join(parts))

    def load(self) -> None:
        """Read the log into memory. Corrupt lines are skipped, not fatal."""
        with self._lock:
            self._records = []
            self._docs = []
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = ExperienceRecord.model_validate_json(line)
                    except Exception:
                        continue  # tolerate a truncated tail; never crash planning
                    self._records.append(record)
                    self._docs.append(self._document_terms(record))
            self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def add(self, record: ExperienceRecord) -> None:
        """Append one completed run."""
        self._ensure_loaded()
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(record.model_dump_json() + "\n")
            self._records.append(record)
            self._docs.append(self._document_terms(record))

    def __len__(self) -> int:
        self._ensure_loaded()
        return len(self._records)

    @property
    def records(self) -> list[ExperienceRecord]:
        self._ensure_loaded()
        return list(self._records)

    # -- retrieval ---------------------------------------------------------
    def retrieve(
        self,
        query: str,
        *,
        limit: int = 3,
        repository: str | None = None,
        successful_only: bool = False,
        min_score: float = 0.0,
    ) -> list[RetrievedExperience]:
        """BM25-ranked similar past tasks.

        ``repository`` applies a same-repo boost (not a hard filter), because a
        similar task in another repository is still useful context.
        """
        self._ensure_loaded()
        query_terms = tokenize(query)
        if not query_terms or not self._records:
            return []

        candidates = [
            (index, record)
            for index, record in enumerate(self._records)
            if not (successful_only and not record.final_status.startswith("completed"))
        ]
        if not candidates:
            return []

        doc_count = len(candidates)
        avg_len = sum(len(self._docs[i]) for i, _ in candidates) / doc_count or 1.0

        # Document frequency across the candidate set.
        df: Counter[str] = Counter()
        for index, _ in candidates:
            for term in set(self._docs[index]):
                df[term] += 1

        results: list[RetrievedExperience] = []
        for index, record in candidates:
            terms = self._docs[index]
            if not terms:
                continue
            freq = Counter(terms)
            score = 0.0
            matched: list[str] = []
            for term in set(query_terms):
                if term not in freq:
                    continue
                idf = math.log(1 + (doc_count - df[term] + 0.5) / (df[term] + 0.5))
                tf = freq[term]
                denom = tf + self.k1 * (1 - self.b + self.b * len(terms) / avg_len)
                score += idf * (tf * (self.k1 + 1)) / denom
                matched.append(term)
            if score <= 0:
                continue
            if repository and record.repository == repository:
                score *= 1.25  # same-repo experience is more transferable
            if record.final_status.startswith("completed"):
                score *= 1.1  # prefer runs that actually worked
            results.append(
                RetrievedExperience(
                    record=record, score=round(score, 5), matched_terms=sorted(matched)
                )
            )

        results.sort(key=lambda r: (-r.score, r.record.task))
        return [r for r in results if r.score >= min_score][:limit]

    # -- prompt rendering --------------------------------------------------
    @staticmethod
    def render_context(retrieved: list[RetrievedExperience], *, max_chars: int = 1800) -> str:
        """Compact planner-prompt block. Empty string when nothing is relevant."""
        if not retrieved:
            return ""
        lines = ["Relevant prior experience from this system's own history:"]
        for item in retrieved:
            record = item.record
            lines.append(
                f"- task: {record.task[:160]}\n"
                f"  repo: {record.repository} | complexity: {record.complexity} | "
                f"outcome: {record.final_status} | recovery_attempts: {record.recovery_attempts}"
            )
            if record.relevant_files:
                lines.append(f"  files that mattered: {', '.join(record.relevant_files[:5])}")
            if record.failure_categories:
                lines.append(
                    f"  failures seen: {', '.join(dict.fromkeys(record.failure_categories))}"
                )
            if record.lesson:
                lines.append(f"  lesson: {record.lesson[:200]}")
        text = "\n".join(lines)
        return text[:max_chars]

    def stats(self) -> dict[str, object]:
        self._ensure_loaded()
        outcomes: Counter[str] = Counter(r.final_status for r in self._records)
        return {
            "records": len(self._records),
            "path": str(self.path),
            "outcomes": dict(outcomes),
            "avg_recovery_attempts": round(
                sum(r.recovery_attempts for r in self._records) / max(1, len(self._records)), 3
            ),
        }
