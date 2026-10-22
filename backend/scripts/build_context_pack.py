#!/usr/bin/env python3
"""Build a context pack: clone, parse, write to Neo4j, render all three contexts, and
freeze the result to one JSON file.

Run this where the network and Neo4j live -- a laptop, or an HPC login node with a
Neo4j container. The pack it writes is then the only input the GPU job needs, so the
compute node never clones a repo, never talks to a database, and spends its whole
allocation generating.

    python -m scripts.build_context_pack $(cat ../18_repo_urls.txt) \
        --out evaluation_results/context_pack.json

Then on the GPU node:

    python -m app.evaluation.harness --context-pack evaluation_results/context_pack.json \
        --models qwen2.5-coder:14b --judge-model gemma2:9b ...

Each generator gets its own contexts (raw, dependency_graph, knowledge_graph and the
rag_bm25 baseline), budgeted in its own tokenizer, and the build refuses to write a pack
in which any prompt would overflow OLLAMA_NUM_CTX (default 8192).

A repo that fails to clone or parse is reported and skipped rather than aborting the
build -- the pack records what it contains, and the harness scores what it is given.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.acquisition.clone import (  # noqa: E402
    AcquiredRepo,
    CloneFailedError,
    InvalidRepoUrlError,
    RepoTooLargeError,
    clone_repository,
)
from app.context.builder import build_context  # noqa: E402
from app.context.rag import build_rag_context  # noqa: E402
from app.context.token_budget import MODEL_TOKENIZERS, get_encoder, prompt_tokens  # noqa: E402
from app.db.neo4j_client import get_driver  # noqa: E402
from app.evaluation.context_pack import ContextPack, PackedRepository  # noqa: E402
from app.graph.builder import ensure_constraints, write_parsed_repository  # noqa: E402
from app.parsers.base import UnsupportedFrameworkError  # noqa: E402
from app.parsers.registry import parse_repository  # noqa: E402
from app.pipeline import _read_raw_source  # noqa: E402
from app.schemas.llm_result import ContextVariant  # noqa: E402


def build(
    urls: list[str], max_size_mb: int, notes: str, models: list[str], pins: dict[str, str] | None = None
) -> ContextPack:
    driver = get_driver()
    pack = ContextPack(
        built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        notes=notes,
    )
    num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
    overflows: list[str] = []
    try:
        ensure_constraints(driver)
        for i, url in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}] {url}", flush=True)
            acquired: AcquiredRepo | None = None
            try:
                acquired = clone_repository(url, max_size_mb=max_size_mb, commit_sha=(pins or {}).get(url))
                parsed = parse_repository(acquired.local_path)
                parsed.metadata.source_url = url
                parsed.metadata.commit_sha = acquired.commit_sha
                write_parsed_repository(driver, parsed)
                name = parsed.metadata.name
                # Every context is rendered while the checkout still exists: raw and
                # rag_bm25 read source files, which cleanup deletes.
                contexts = {
                    ContextVariant.RAW: _read_raw_source(acquired.local_path, parsed),
                    ContextVariant.DEPENDENCY_GRAPH: build_context(driver, name, ContextVariant.DEPENDENCY_GRAPH),
                    ContextVariant.KNOWLEDGE_GRAPH: build_context(driver, name, ContextVariant.KNOWLEDGE_GRAPH),
                }
                model_contexts = {
                    model: _contexts_for_model(driver, acquired.local_path, parsed, model)
                    for model in models
                }
            except (
                InvalidRepoUrlError,
                CloneFailedError,
                RepoTooLargeError,
                UnsupportedFrameworkError,
            ) as e:
                print(f"    SKIPPED: {e}", flush=True)
                continue
            finally:
                if acquired is not None:
                    acquired.cleanup()

            for model, ctxs in model_contexts.items():
                sizes = " ".join(
                    f"{v.value}={prompt_tokens(c, model)}t" for v, c in ctxs.items()
                )
                print(f"    {name} [{model}] prompt tokens incl. template: {sizes}", flush=True)
                for v, c in ctxs.items():
                    # The generator also needs room to answer.
                    if prompt_tokens(c, model) + RESERVED_OUTPUT_TOKENS > num_ctx:
                        overflows.append(f"{name}/{model}/{v.value}")
            pack.repositories.append(
                PackedRepository(source_url=url, parsed=parsed, contexts=contexts, model_contexts=model_contexts)
            )
    finally:
        driver.close()
    if overflows:
        raise SystemExit(
            "These prompts would not fit the window with room for output, so Ollama "
            "would truncate them -- the CodeLlama failure this pack format exists to "
            "prevent. Nothing written.\n  " + "\n  ".join(overflows)
        )
    return pack


RESERVED_OUTPUT_TOKENS = 1200  # matches compute_context_token_budget's default


def _contexts_for_model(driver, repo_path: Path, parsed, model: str) -> dict[ContextVariant, str]:
    """All four arms, each budgeted in `model`'s own tokens. rag_bm25 gets exactly the
    knowledge graph's token count, so the two differ in form, not in size."""
    name = parsed.metadata.name
    kg = build_context(driver, name, ContextVariant.KNOWLEDGE_GRAPH, model=model)
    kg_tokens = len(get_encoder(model).encode(kg))
    return {
        ContextVariant.RAW: _read_raw_source(repo_path, parsed, model=model),
        ContextVariant.DEPENDENCY_GRAPH: build_context(driver, name, ContextVariant.DEPENDENCY_GRAPH, model=model),
        ContextVariant.KNOWLEDGE_GRAPH: kg,
        ContextVariant.RAG_BM25: build_rag_context(repo_path, parsed, kg_tokens, model=model),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+", help="GitHub repo URLs to freeze into the pack")
    ap.add_argument("--out", default="evaluation_results/context_pack.json")
    ap.add_argument("--max-size-mb", type=int, default=200)
    ap.add_argument("--notes", default="", help="Free text recorded in the pack (e.g. which study this is for)")
    ap.add_argument(
        "--models", nargs="+", default=list(MODEL_TOKENIZERS),
        help="Generators to render per-model contexts for, each budgeted in its own "
        "tokenizer (default: the CICT-2026 roster). Downloads tokenizer.json only.",
    )
    ap.add_argument(
        "--pin-commits", metavar="PACK",
        help="Check out each URL at the commit recorded in this existing pack, so a rebuild "
        "parses the same code the earlier battery saw even if upstream has moved since.",
    )
    args = ap.parse_args()
    from dotenv import load_dotenv

    load_dotenv()  # NEO4J_PASSWORD / OLLAMA_NUM_CTX from backend/.env, as the harness does

    pins = None
    if args.pin_commits:
        old = ContextPack.read(args.pin_commits)
        pins = {r.source_url: r.parsed.metadata.commit_sha for r in old.repositories if r.parsed.metadata.commit_sha}
        unpinned = [u for u in args.urls if u not in pins]
        if unpinned:
            print(f"NOTE: no pinned commit for {len(unpinned)} URL(s); using their current head: {unpinned}")

    pack = build(args.urls, args.max_size_mb, args.notes, args.models, pins)
    if not pack.repositories:
        print("No repositories were packed -- nothing written.", file=sys.stderr)
        return 1

    out = pack.write(args.out)
    mb = out.stat().st_size / 1_048_576
    print(f"\nWrote {len(pack.repositories)} repositories to {out} ({mb:.1f} MB)")
    if len(pack.repositories) != len(args.urls):
        print(f"NOTE: {len(args.urls) - len(pack.repositories)} of {len(args.urls)} URLs were skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
