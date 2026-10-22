"""RAG_BM25 ContextVariant: BM25-retrieved source chunks at a matched token budget.

The baseline the CICT-2026 reviewers asked for (R2). Raw truncation keeps whichever
files happen to come first; this arm instead *selects* the source text most relevant to
the summarisation task, then stops at exactly the budget the knowledge graph used for
the same repository and model. If the knowledge graph still wins, the advantage is the
structure, not merely a better choice of what to keep under a cap.

Design choices, each deliberate:

- The query is the task, not the repository. A summary request has no user question,
  so the query is a fixed list of the concepts the summary prompt asks for. Building it
  from parser output (endpoint names, class names) would leak the very structure this
  baseline is meant to do without.
- Chunks are fixed line windows over the same files the parser considered
  (`parsed.modules`), so this arm sees no file the other arms could not.
- Selected chunks are emitted in file/line order, not score order, so the model reads
  coherent source rather than a shuffled collage.
- BM25 (Okapi, k1=1.5, b=0.75) is implemented here rather than imported: it is twenty
  lines, and the cluster image then needs no extra dependency.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.context.token_budget import get_encoder
from app.schemas.parser_schema import ParsedRepository

# What SUMMARY_PROMPT_TEMPLATE asks a summary to contain (overview, tech stack, services,
# dependencies, endpoints, components), phrased as retrieval terms.
TASK_QUERY = (
    "application server entry point bootstrap listen port "
    "route router routes endpoint get post put patch delete request response handler "
    "controller service module provider middleware "
    "database model schema entity repository orm query connection "
    "config configuration environment dependency import require "
    "auth authentication user"
)

LINES_PER_CHUNK = 40
K1 = 1.5
B = 0.75

_IDENT = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_CAMEL = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def tokenize(text: str) -> list[str]:
    """Identifiers split on camelCase and snake_case, lowercased: `getUserById` ->
    get, user, by, id. Code vocabulary is compound, and BM25 matches whole terms."""
    out: list[str] = []
    for ident in _IDENT.findall(text):
        out.extend(p.lower() for p in _CAMEL.findall(ident))
    return out


@dataclass
class Chunk:
    path: str
    start_line: int  # 1-based, inclusive
    end_line: int
    text: str

    def render(self) -> str:
        return f"// ---- {self.path} (lines {self.start_line}-{self.end_line}) ----\n{self.text}"


def chunk_repository(repo_path: Path, parsed: ParsedRepository, lines_per_chunk: int = LINES_PER_CHUNK) -> list[Chunk]:
    chunks: list[Chunk] = []
    for module in parsed.modules:
        try:
            lines = (repo_path / module.path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for start in range(0, len(lines), lines_per_chunk):
            window = lines[start : start + lines_per_chunk]
            if any(line.strip() for line in window):
                chunks.append(Chunk(module.path, start + 1, start + len(window), "\n".join(window)))
    return chunks


def bm25_scores(chunks: list[Chunk], query: str = TASK_QUERY) -> list[float]:
    docs = [Counter(tokenize(c.text)) for c in chunks]
    if not docs:
        return []
    lengths = [sum(d.values()) for d in docs]
    avgdl = sum(lengths) / len(lengths) or 1.0
    n = len(docs)
    terms = set(tokenize(query))
    df = {t: sum(1 for d in docs if t in d) for t in terms}
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in terms}
    scores = []
    for d, dl in zip(docs, lengths):
        s = 0.0
        for t in terms:
            tf = d.get(t, 0)
            if tf:
                s += idf[t] * tf * (K1 + 1) / (tf + K1 * (1 - B + B * dl / avgdl))
        scores.append(s)
    return scores


def build_rag_context(
    repo_path: Path,
    parsed: ParsedRepository,
    token_budget: int,
    model: Optional[str] = None,
    query: str = TASK_QUERY,
) -> str:
    """Highest-scoring chunks until `token_budget` (counted in `model`'s tokens) is
    spent; a chunk that would overflow is skipped and smaller ones may still fit.
    Ties break on file order so the selection is deterministic."""
    enc = get_encoder(model)
    chunks = chunk_repository(repo_path, parsed)
    scores = bm25_scores(chunks, query)
    ranked = sorted(range(len(chunks)), key=lambda i: (-scores[i], i))

    separator = len(enc.encode("\n\n"))
    chosen: list[int] = []
    used = 0
    for i in ranked:
        cost = len(enc.encode(chunks[i].render())) + (separator if chosen else 0)
        if used + cost > token_budget:
            continue
        chosen.append(i)
        used += cost
    return "\n\n".join(chunks[i].render() for i in sorted(chosen))
