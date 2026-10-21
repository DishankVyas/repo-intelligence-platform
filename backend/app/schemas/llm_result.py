"""
Standard result object returned by every LLMProvider call, regardless of which
model/vendor served it. The evaluation harness and dashboard only ever talk to this
shape -- they never see vendor-specific response objects.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class LLMTask(str, Enum):
    SUMMARY = "summary"
    ARCHITECTURE_DIAGRAM = "architecture_diagram"
    HALLUCINATION_JUDGE = "hallucination_judge"  # one model grading another's summary
    COVERAGE_JUDGE = "coverage_judge"  # one model checking another's summary for completeness


class ContextVariant(str, Enum):
    """The three repository representations under comparison (this is the core
    independent variable of the research question) -- scoped down from an original
    5-way ablation (raw/AST/dependency_graph/knowledge_graph/combined) to what's
    buildable in a month; see Capstone_Roadmap section 2."""

    RAW = "raw"
    DEPENDENCY_GRAPH = "dependency_graph"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    # Retrieval baseline added for the CICT-2026 revision (reviewer R2): BM25-ranked
    # source chunks at the knowledge graph's own token count, so it isolates
    # "structure vs. selected raw text" at a matched budget. Pack-only -- built by
    # scripts/build_context_pack.py, never by the live API.
    RAG_BM25 = "rag_bm25"


class ProviderName(str, Enum):
    """All three comparison models run locally through Ollama (no paid commercial
    APIs) -- the specific model is captured in LLMResult.model, not here."""

    OLLAMA = "ollama"


class RunStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"  # e.g. output returned but failed schema/mermaid validation
    FAILED = "failed"


class LLMOutput(BaseModel):
    raw_text: str
    parsed: Optional[dict[str, Any]] = None  # structured JSON if task expects it
    valid: bool = False  # did it pass schema validation (summary) or syntax check (diagram)


class LLMMetrics(BaseModel):
    latency_ms: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    retry_count: int = 0


class LLMResult(BaseModel):
    """The single return type for BaseLLMProvider.generate_summary() and
    .generate_architecture_diagram(). Everything downstream (SQLite logging,
    evaluation harness, dashboard) is built against this shape."""

    provider: ProviderName
    model: str  # specific model id actually used, e.g. "claude-sonnet-5"
    task: LLMTask
    context_variant: ContextVariant
    repo_name: str

    output: LLMOutput
    metrics: LLMMetrics

    status: RunStatus
    error: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    prompt_template_version: Optional[str] = None
