"""
Tests for the CICT-2026 revision changes: per-model token budgets (the CodeLlama
overflow fix), the rag_bm25 baseline, v2 context packs, seeded/repeatable harness runs
with a fixed judge, and judge verdict parsing of string booleans. No network, no
Neo4j, no Ollama: tokenizers are replaced with a deterministic fake.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.context import token_budget
from app.context.rag import bm25_scores, build_rag_context, chunk_repository, tokenize
from app.evaluation.context_pack import ContextPack, PackedRepository
from app.evaluation.hallucination import _parse_verdict
from app.evaluation.harness import run_evaluation
from app.parsers.express_parser import ExpressParser
from app.schemas.llm_result import ContextVariant, LLMMetrics, LLMOutput, LLMResult, LLMTask, ProviderName, RunStatus
from app.schemas.parser_schema import ModuleNode, ParsedRepository, RepoMetadata

FIXTURES = Path(__file__).parent / "fixtures"


class _WordEncoder:
    """One token per whitespace-separated word -- predictable budget arithmetic."""

    def encode(self, text):
        return list(range(len(text.split())))

    def decode(self, ids):  # pragma: no cover - not exercised by these tests
        raise NotImplementedError


class _DoubleEncoder(_WordEncoder):
    """Twice as many tokens per word: stands in for a less efficient tokenizer."""

    def encode(self, text):
        return list(range(2 * len(text.split())))


@pytest.fixture
def fake_encoders(monkeypatch):
    monkeypatch.setattr(token_budget, "_encoder_cache", {"small-tok": _WordEncoder(), "big-tok": _DoubleEncoder()})
    monkeypatch.setattr(token_budget, "_budget_cache", {})
    monkeypatch.setitem(token_budget.MODEL_TOKENIZERS, "small-tok", "unused")
    monkeypatch.setitem(token_budget.MODEL_TOKENIZERS, "big-tok", "unused")
    monkeypatch.setenv("OLLAMA_NUM_CTX", "8192")


# ---------------------------------------------------------------- token budgets


def test_budget_is_counted_in_the_models_own_tokens(fake_encoders):
    small = token_budget.compute_context_token_budget(model="small-tok")
    big = token_budget.compute_context_token_budget(model="big-tok")
    # The prompt template costs twice as much under the less efficient tokenizer,
    # so less of the window is left for context.
    assert big < small < 8192 - 1200


def test_unknown_model_refuses_rather_than_borrowing_a_tokenizer(monkeypatch):
    monkeypatch.setattr(token_budget, "_encoder_cache", {})
    with pytest.raises(KeyError, match="No tokenizer registered"):
        token_budget.get_encoder("some-unlisted-model:7b")


def test_prompt_tokens_includes_template_and_margin(fake_encoders):
    n = token_budget.prompt_tokens("one two three", "small-tok")
    assert n > 3 + token_budget.CHAT_TEMPLATE_MARGIN


# --------------------------------------------------------------------- rag_bm25


def _express_fixture() -> tuple[Path, ParsedRepository]:
    root = FIXTURES / "express-app"
    return root, ExpressParser().parse(root)


def test_tokenize_splits_code_identifiers():
    assert tokenize("getUserById snake_case HTTPServer") == ["get", "user", "by", "id", "snake", "case", "http", "server"]


def test_chunks_cover_only_parsed_modules():
    root, parsed = _express_fixture()
    chunks = chunk_repository(root, parsed)
    assert chunks
    assert {c.path for c in chunks} <= {m.path for m in parsed.modules}


def test_bm25_prefers_routing_code_for_the_task_query():
    root, parsed = _express_fixture()
    chunks = chunk_repository(root, parsed, lines_per_chunk=5)
    scores = bm25_scores(chunks)
    best = chunks[max(range(len(chunks)), key=scores.__getitem__)]
    assert any(word in best.text for word in ("router", "app.get", "app.use", "require"))


def test_rag_context_respects_the_matched_budget(fake_encoders):
    root, parsed = _express_fixture()
    budget = 40
    ctx = build_rag_context(root, parsed, budget, model="small-tok")
    assert ctx
    assert len(ctx.split()) <= budget


def test_rag_context_is_in_file_order_and_deterministic(fake_encoders):
    root, parsed = _express_fixture()
    a = build_rag_context(root, parsed, 10_000, model="small-tok")
    b = build_rag_context(root, parsed, 10_000, model="small-tok")
    assert a == b
    headers = [line for line in a.splitlines() if line.startswith("// ---- ")]
    assert headers == sorted(headers, key=lambda h: (h.split(" (lines")[0], int(h.split("lines ")[1].split("-")[0])))


# --------------------------------------------------------------- v2 context pack


def _parsed(name="repo-a") -> ParsedRepository:
    return ParsedRepository(
        metadata=RepoMetadata(name=name, source_url=f"https://github.com/t/{name}", detected_language="javascript", detected_framework="express"),
        modules=[ModuleNode(id="mod_0", path="app.js")],
    )


def _packed(model_contexts=None) -> PackedRepository:
    return PackedRepository(
        source_url="https://github.com/t/repo-a",
        parsed=_parsed(),
        contexts={ContextVariant.RAW: "shared raw", ContextVariant.KNOWLEDGE_GRAPH: "shared kg"},
        model_contexts=model_contexts or {},
    )


def test_v2_pack_hands_each_model_its_own_contexts():
    packed = _packed({
        "m-a": {ContextVariant.RAW: "a raw", ContextVariant.RAG_BM25: "a rag"},
        "m-b": {ContextVariant.RAW: "b raw"},
    })
    assert packed.context_map("m-a")[ContextVariant.RAG_BM25] == "a rag"
    assert packed.context_map("m-b") == {ContextVariant.RAW: "b raw"}


def test_v2_pack_refuses_a_model_it_was_not_built_for():
    with pytest.raises(KeyError, match="no contexts for 'm-c'"):
        _packed({"m-a": {ContextVariant.RAW: "a"}}).context_map("m-c")


def test_v1_pack_still_reads_with_shared_contexts(tmp_path):
    path = tmp_path / "pack.json"
    pack = ContextPack(version=1, built_at="2026-09-03", repositories=[_packed()])
    path.write_text(pack.model_dump_json(), encoding="utf-8")
    read = ContextPack.read(path)
    assert read.repositories[0].context_map("any-model")[ContextVariant.RAW] == "shared raw"


def test_pack_json_round_trip_keeps_model_contexts(tmp_path):
    pack = ContextPack(built_at="now", repositories=[_packed({"m-a": {ContextVariant.RAG_BM25: "a rag"}})])
    pack.write(tmp_path / "p.json")
    read = ContextPack.read(tmp_path / "p.json")
    assert read.version == 2
    assert read.repositories[0].context_map("m-a") == {ContextVariant.RAG_BM25: "a rag"}


# ----------------------------------------------------------- harness, v2 + seeds


def _ok_result(model, variant, prompt_context) -> LLMResult:
    return LLMResult(
        provider=ProviderName.OLLAMA, model=model, task=LLMTask.SUMMARY, context_variant=variant,
        repo_name="repo-a", output=LLMOutput(raw_text=prompt_context),
        metrics=LLMMetrics(latency_ms=1, input_tokens=10, output_tokens=5, estimated_cost_usd=0.0),
        status=RunStatus.SUCCESS,
    )


class _GenProvider:
    """Echoes the context it was handed, so each row shows which context reached it."""

    seed = 7
    temperature = 0.7
    default_model = "m-a"

    def generate_summary(self, context, repo_name, context_variant, prompt_template, model):
        return _ok_result(model, context_variant, context)


def _run(pack, **kwargs):
    judge = MagicMock()
    with patch("app.evaluation.harness.score_summary") as hal, patch("app.evaluation.harness.score_coverage") as cov:
        hal.return_value = MagicMock(judged=True, hallucination_score=0.0, total_claims=1, unsupported_claims=[])
        cov.return_value = MagicMock(judged=True, coverage_score=1.0, total_facts=1, missing_facts=[])
        rows = run_evaluation([], judge_model="judge-x", provider=_GenProvider(), judge_provider=judge, context_pack=pack, **kwargs)
    return rows, judge, hal, cov


def _pack():
    return ContextPack(built_at="now", repositories=[_packed({
        "m-a": {ContextVariant.RAW: "a raw", ContextVariant.RAG_BM25: "a rag"},
        "m-b": {ContextVariant.RAW: "b raw", ContextVariant.RAG_BM25: "b rag"},
    })])


def test_each_model_is_generated_from_its_own_pack_contexts():
    rows, *_ = _run(_pack(), models=["m-a", "m-b"])
    got = {(r.model, r.context_variant): r.summary_text for r in rows}
    assert got == {
        ("m-a", "raw"): "a raw", ("m-a", "rag_bm25"): "a rag",
        ("m-b", "raw"): "b raw", ("m-b", "rag_bm25"): "b rag",
    }


def test_judging_goes_to_the_judge_provider_not_the_generator():
    rows, judge, hal, cov = _run(_pack(), models=["m-a"])
    assert hal.call_count == cov.call_count == len(rows) == 2
    assert all(call.args[0] is judge for call in hal.call_args_list + cov.call_args_list)


def test_generator_seed_and_temperature_are_recorded_per_row():
    rows, *_ = _run(_pack(), models=["m-a"])
    assert {(r.seed, r.temperature) for r in rows} == {(7, 0.7)}


def test_variants_filter_generates_only_the_requested_arms():
    rows, *_ = _run(_pack(), models=["m-a", "m-b"], variants=[ContextVariant.RAG_BM25])
    assert {r.context_variant for r in rows} == {"rag_bm25"}
    assert len(rows) == 2


# ------------------------------------------------------------ verdict parsing


@pytest.mark.parametrize("value,expected_unsupported", [
    (False, 1), ("false", 1), ("False", 1), ("no", 1), (0, 1),
    (True, 0), ("true", 0), ("yes", 0), (1, 0),
])
def test_string_booleans_are_not_truthy_strings(value, expected_unsupported):
    raw = '{"all_claims": [{"text": "uses MongoDB", "supported": %s}]}' % (
        f'"{value}"' if isinstance(value, str) else str(value).lower()
    )
    total, unsupported, _ = _parse_verdict(raw)
    assert total == 1
    assert len(unsupported) == expected_unsupported


def test_unreadable_supported_value_drops_the_claim():
    total, unsupported, _ = _parse_verdict('{"all_claims": [{"text": "x", "supported": "maybe"}]}')
    assert (total, unsupported) == (0, [])
