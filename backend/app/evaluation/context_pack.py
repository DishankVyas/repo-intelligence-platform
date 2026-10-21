"""Context packs: the frozen inputs to a generation run, so generation can happen
somewhere that cannot clone a repo or reach a Neo4j.

The battery has two halves with very different requirements. Cloning, tree-sitter
parsing, the Neo4j round-trip and context rendering are CPU-only, need network access
and a database, and take seconds. Generation needs a GPU, needs neither network nor
database, and takes hours. A job-scheduled GPU node typically offers the opposite of
what the first half wants: no inbound services, often no route to github.com, and a
wall-clock budget you do not want to spend on `git clone`.

A pack is that seam. Build it once where the network and the database live; ship the
file; generate from it on the GPU. Because the pack stores the *rendered* context
strings rather than re-deriving them, a run from a pack is also exactly reproducible
in a way a run from URLs is not -- upstream repos move, and `main` today is not `main`
last week.

The parsed repository travels with the contexts because scoring needs it: both judges
and the deterministic oracle score against parser-extracted ground truth, not against
the context text.

Format is one JSON file, `version` gated so a stale pack fails loudly rather than
silently scoring against contexts that no longer match the code that reads them.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from app.schemas.llm_result import ContextVariant
from app.schemas.parser_schema import ParsedRepository

# Bump when the meaning of a field changes (e.g. a context variant starts including
# something it did not before). An older pack is not "probably fine", it was rendered by
# different code -- which is why each version this code can read is listed explicitly.
#
# v2 (CICT-2026 revision): adds `model_contexts`, one set of contexts per generator,
# each budgeted in that generator's own tokenizer, plus the rag_bm25 arm. v1 packs stay
# readable so the published battery's inputs can still be re-scored, but a v1 pack
# carries only the shared cl100k-budgeted contexts that overflowed CodeLlama's window.
PACK_VERSION = 2
READABLE_VERSIONS = (1, 2)


class PackedRepository(BaseModel):
    """One repository: its parse, and its three rendered contexts."""

    source_url: str
    parsed: ParsedRepository
    contexts: dict[ContextVariant, str]
    # Generator model -> its own contexts. Empty in a v1 pack.
    model_contexts: dict[str, dict[ContextVariant, str]] = Field(default_factory=dict)

    def context_map(self, model: str | None = None) -> dict[ContextVariant, str]:
        """Keys come back as ContextVariant even after a JSON round-trip, where they
        arrive as bare strings.

        With `model`, returns that generator's own contexts. A pack that has
        per-model contexts but none for `model` raises rather than falling back to
        the shared set: those were budgeted in a different tokenizer, and silently
        using them is how CodeLlama's prompts overflowed in the first place."""
        source = self.contexts
        if model is not None and self.model_contexts:
            if model not in self.model_contexts:
                raise KeyError(
                    f"Context pack has no contexts for {model!r} (it has: "
                    f"{', '.join(sorted(self.model_contexts))}). Rebuild it with "
                    f"--models including {model!r}."
                )
            source = self.model_contexts[model]
        return {ContextVariant(k): v for k, v in source.items()}


class ContextPack(BaseModel):
    version: int = PACK_VERSION
    built_at: str
    notes: str = ""
    repositories: list[PackedRepository] = Field(default_factory=list)

    def write(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def read(cls, path: str | Path) -> "ContextPack":
        pack = cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
        if pack.version not in READABLE_VERSIONS:
            raise ValueError(
                f"Context pack at {path} is version {pack.version}, but this code reads "
                f"versions {READABLE_VERSIONS}. Rebuild it with scripts/build_context_pack.py "
                "-- the contexts in an older pack were rendered by different code, so "
                "scoring against them would compare arms that were never built the same way."
            )
        return pack
