"""
Batch evaluation harness (Week 4 / Phase 7).

Runs the full battery -- every repo x every model x all 3 representations -- scoring
each generated summary with two LLM-as-judge metrics (hallucination: are the claims
it made true; coverage: how much of the parser's ground truth did it mention), and
writes one flat CSV row per (repo, model, representation). That CSV is the results
table the report is built from; each row carries the efficiency metrics (latency,
tokens) alongside both quality metrics, so the representation ablation can be read
straight off it -- and so a summary that's faithful only because it said almost
nothing (high hallucination score, but also low coverage) doesn't look identical to
one that's faithful AND complete.

Runs as a script: `python -m app.evaluation.harness <url> [<url> ...] --out results.csv`.
Meant to run on the single designated evaluation machine (roadmap Section 1) so the
latency numbers are comparable -- running arms on different laptops makes them
meaningless.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # never imported at runtime -- neo4j is not installed on the cluster
    from neo4j import Driver

from pydantic import BaseModel

from app.evaluation.context_pack import ContextPack, PackedRepository
from app.evaluation.coverage import build_coverable_facts, categorize_facts, score_coverage
from app.evaluation.hallucination import score_summary
from app.evaluation.diagram_score import (
    GraphDiffScore,
    extract_actual_structure,
    load_expected,
    score_diagram,
)
from app.evaluation.failure_analysis import analyze_failures
from app.evaluation.quality_judge import QualityJudgeResult, score_summary_quality
from app.evaluation.text_overlap import TextOverlapResult, extract_overview, score_text_overlap
from app.pipeline import (
    AnalysisError,
    PipelineInfrastructureError,
    _default_comparison_models,
    generate_across_contexts,
    run_representation_ablation,
)
from app.providers.base import BaseLLMProvider
from app.providers.ollama_provider import OllamaProvider
from app.schemas.llm_result import ContextVariant, LLMResult, RunStatus
from app.schemas.parser_schema import ParsedRepository


class EvaluationRow(BaseModel):
    repo_name: str
    source_url: str
    framework: Optional[str]
    model: str
    context_variant: str
    run_status: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    judge_model: str
    # Generator decoding, recorded per row so repeated runs (CICT-2026 reviewer R4)
    # are distinguishable in one accumulated SQLite table. Judges always decode at
    # temperature 0 with a fixed seed, whatever these say -- see run_evaluation.
    seed: Optional[int] = None
    temperature: Optional[float] = None
    self_judged: bool  # generator == judge; filter these out when analysing, if desired
    hallucination_judged: bool  # did the judge's own output parse
    hallucination_score: float
    total_claims: int
    unsupported_claims: int
    # Coverage (recall) -- companion to hallucination (precision): how much of the
    # parser's ground truth the summary actually mentioned, vs. how much of what it
    # said was true. A summary can score 0.0 hallucination by saying almost nothing;
    # coverage is what would catch that.
    coverage_judged: bool
    coverage_score: float
    total_facts: int
    missing_facts: int
    # Diagram graph-diff (per repo, same across a repo's rows -- populated only when a
    # `<repo_name>.json` expected-structure annotation is found). None when unscored.
    diagram_scored: bool = False
    diagram_module_f1: Optional[float] = None
    diagram_import_f1: Optional[float] = None
    diagram_endpoint_f1: Optional[float] = None
    diagram_overall_f1: Optional[float] = None
    # Text-overlap metrics against reference_summaries/<repo_name>.json's `overview`
    # (see reference_summaries/README.md) -- populated only when --reference-summaries-dir
    # is given and a reference file exists for this repo.
    text_overlap_scored: bool = False
    bleu4: Optional[float] = None
    rouge_l: Optional[float] = None
    meteor: Optional[float] = None
    bertscore_f1: Optional[float] = None
    # G-Eval quality-judge scores (fixed rubric, see quality_judge.py) -- opt-in via
    # --quality-judge since this multiplies judge calls by the number of criteria (5).
    quality_judged: bool = False
    quality_mean_score: Optional[float] = None
    quality_completeness: Optional[float] = None
    quality_conciseness: Optional[float] = None
    quality_correctness: Optional[float] = None
    quality_cohesiveness: Optional[float] = None
    quality_domain_specificity: Optional[float] = None
    # Failure taxonomy tags (see failure_analysis.py), comma-joined for a flat CSV
    # cell -- empty string when nothing was tagged.
    failure_tags: str = ""
    # The generated summary's raw text -- previously computed and judged but never
    # persisted, so re-scoring an existing battery with a different judge model (or
    # exporting a human-validation sample) required regenerating every summary from
    # scratch. Storing it here means a judge swap only needs fresh judge calls, not
    # fresh generator calls, roughly a 3x reduction for that specific case (one
    # generation vs. generation+hallucination-judge+coverage-judge). Empty for a
    # failed/empty run -- nothing was generated to store.
    summary_text: str = ""
    # Missing/unsupported fact and claim lists, JSON-encoded -- like summary_text,
    # previously computed and used for judging (and for the failure-tag analysis
    # just below) but never persisted, so a human-validation export or a category
    # breakdown of coverage (dependencies vs. endpoints vs. classes vs. database
    # entities) required re-deriving them from scratch. "[]" rather than "" when
    # judged-but-nothing-was-missing/unsupported, so a downstream reader can always
    # json.loads() this column without a special case for the empty state.
    missing_facts_list: str = "[]"
    unsupported_claims_list: str = "[]"
    # Total coverage-checkable facts, broken out by category (Dependency/Endpoint/
    # Class-Service/Database entity/Framework/Language) -- JSON-encoded dict, e.g.
    # '{"Dependency": 12, "Endpoint": 8}'. The per-category MISSING count is
    # derivable from missing_facts_list above; this is the per-category
    # denominator, which isn't otherwise recoverable after the fact.
    facts_by_category: str = "{}"
    error: Optional[str] = None


def run_evaluation(
    repo_urls: list[str],
    models: Optional[list[str]] = None,
    judge_model: Optional[str] = None,
    max_size_mb: int = 200,
    annotations_dir: Optional[str | Path] = None,
    reference_summaries_dir: Optional[str | Path] = None,
    enable_quality_judge: bool = False,
    quality_judge_host: Optional[str] = None,
    bert_scorer=None,
    driver: Optional[Driver] = None,
    provider: Optional[BaseLLMProvider] = None,
    context_pack: Optional["ContextPack"] = None,
    judge_provider: Optional[BaseLLMProvider] = None,
    variants: Optional[list[ContextVariant]] = None,
) -> list[EvaluationRow]:
    """One shared Neo4j driver and provider across the whole batch (created here if
    not injected). A single repo failing -- bad URL, unsupported framework, Neo4j
    blip -- records a failure row and moves on rather than aborting the batch;
    'something always breaks on repos you didn't build against' (roadmap Week 4).

    If `annotations_dir` is given, each repo is also diagram-scored against
    `<annotations_dir>/<repo_name>.json` when that file exists (repos without an
    annotation just leave the diagram columns empty -- no error).

    If `reference_summaries_dir` is given, text-overlap metrics (BLEU-4/ROUGE-L/
    METEOR/BERTScore) are scored against `<reference_summaries_dir>/<repo_name>.json`'s
    `overview` field when that file exists. `bert_scorer` defaults to a real (lazily
    imported, model-downloading) BERTScore backend -- inject
    `text_overlap.score_text_overlap`'s `bert_scorer=None` upstream, or your own
    batched scorer, to skip the model download entirely.

    `enable_quality_judge` runs the G-Eval rubric scorer (quality_judge.py) on every
    successful summary -- OFF by default because it multiplies judge calls by 5 (one
    per criterion) on top of the hallucination/coverage judges already running.
    `quality_judge_host` defaults to OLLAMA_HOST (see quality_judge.py) and is only
    used when this is enabled.

    If `context_pack` is given, `repo_urls` is ignored and no Neo4j driver is opened:
    contexts and parses are read from the pack instead of being rebuilt. This is what
    lets the batch run on a job-scheduled GPU node with no database and no route to
    github.com -- see scripts/build_context_pack.py. Scoring is unchanged, because it
    scores against the parsed repository, which the pack carries. A v2 pack holds
    contexts per generator, budgeted in that generator's tokenizer; each model is
    handed its own.

    `judge_provider` does all judging; it defaults to `provider`. Pass a separate one
    whenever the generator samples (temperature > 0) so that repeated runs vary only
    the summaries, never the grading. `variants` restricts which arms are generated
    (e.g. only rag_bm25 when adding the baseline to an existing battery)."""
    # A packed run must not open a driver: the whole point is that no Neo4j exists on
    # the node. get_driver() is lazy about connecting, but constructing it here would
    # still fail on a host with no bolt route configured at all.
    owns_driver = driver is None and context_pack is None
    if not context_pack:
        from app.db.neo4j_client import get_driver
        driver = driver or get_driver()
    provider = provider or OllamaProvider()
    judge_provider = judge_provider or provider
    # Recorded only when the provider actually exposes them (an injected test double or
    # a non-Ollama provider may not).
    gen_seed = getattr(provider, "seed", None)
    gen_seed = gen_seed if isinstance(gen_seed, int) else None
    gen_temperature = getattr(provider, "temperature", None)
    gen_temperature = float(gen_temperature) if isinstance(gen_temperature, (int, float)) else None
    judge_model = judge_model or provider.default_model
    if reference_summaries_dir is not None and bert_scorer is None:
        from app.evaluation.text_overlap import default_bert_scorer

        bert_scorer = default_bert_scorer

    rows: list[EvaluationRow] = []
    work: list[tuple[str, Optional["PackedRepository"]]] = (
        [(r.source_url, r) for r in context_pack.repositories]
        if context_pack is not None
        else [(u, None) for u in repo_urls]
    )
    try:
        for url, packed in work:
            try:
                if packed is not None:
                    parsed = packed.parsed
                    results = []
                    for model in models or _default_comparison_models():
                        contexts = packed.context_map(model)
                        if variants is not None:
                            contexts = {v: c for v, c in contexts.items() if v in variants}
                        results.extend(
                            generate_across_contexts(parsed, contexts, models=[model], provider=provider)
                        )
                else:
                    parsed, results = run_representation_ablation(
                        url,
                        models=models,
                        max_size_mb=max_size_mb,
                        driver=driver,
                        provider=provider,
                    )
            except (AnalysisError, PipelineInfrastructureError) as e:
                rows.append(_failure_row(url, judge_model, str(e)))
                continue

            diagram = _diagram_score(parsed, annotations_dir)
            reference_overview = _reference_overview(parsed, reference_summaries_dir)
            for result in results:
                rows.append(
                    _row_for_result(
                        judge_provider,
                        parsed,
                        result,
                        judge_model,
                        diagram,
                        reference_overview=reference_overview,
                        bert_scorer=bert_scorer,
                        enable_quality_judge=enable_quality_judge,
                        quality_judge_host=quality_judge_host,
                        seed=gen_seed,
                        temperature=gen_temperature,
                    )
                )
    finally:
        if owns_driver and driver is not None:
            driver.close()
    return rows


def _reference_overview(parsed: ParsedRepository, reference_summaries_dir: Optional[str | Path]) -> Optional[str]:
    if reference_summaries_dir is None:
        return None
    ref_path = Path(reference_summaries_dir) / f"{parsed.metadata.name}.json"
    if not ref_path.exists():
        return None
    data = json.loads(ref_path.read_text(encoding="utf-8"))
    overview = data.get("overview")
    return overview if isinstance(overview, str) else None


def _diagram_score(
    parsed: ParsedRepository, annotations_dir: Optional[str | Path]
) -> Optional[GraphDiffScore]:
    """Score the extracted structure against an annotation file if one exists for
    this repo. Per-repo (the diagram is deterministic from the graph), so it's
    computed once and repeated onto each of the repo's rows."""
    if annotations_dir is None:
        return None
    annotation_path = Path(annotations_dir) / f"{parsed.metadata.name}.json"
    if not annotation_path.exists():
        return None
    expected = load_expected(annotation_path)
    actual = extract_actual_structure(parsed)
    return score_diagram(actual, expected, parsed.metadata.name)


def _row_for_result(
    provider: BaseLLMProvider,
    parsed: ParsedRepository,
    result: LLMResult,
    judge_model: str,
    diagram: Optional[GraphDiffScore] = None,
    reference_overview: Optional[str] = None,
    bert_scorer=None,
    enable_quality_judge: bool = False,
    quality_judge_host: Optional[str] = None,
    seed: Optional[int] = None,
    temperature: Optional[float] = None,
) -> EvaluationRow:
    self_judged = result.model == judge_model
    text_overlap: Optional[TextOverlapResult] = None
    quality: Optional[QualityJudgeResult] = None
    unsupported_claims_list: list[str] = []
    missing_facts_list: list[str] = []
    facts_by_category: dict[str, int] = {}

    if result.status == RunStatus.SUCCESS:
        scored = score_summary(
            provider,
            parsed,
            result.output.raw_text,
            result.context_variant,
            judge_model=judge_model,
        )
        hallucination_judged = scored.judged
        hallucination_score = scored.hallucination_score
        total_claims = scored.total_claims
        unsupported_claims_list = scored.unsupported_claims
        unsupported = len(unsupported_claims_list)

        coverage = score_coverage(
            provider,
            parsed,
            result.output.raw_text,
            result.context_variant,
            judge_model=judge_model,
        )
        coverage_judged = coverage.judged
        coverage_score = coverage.coverage_score
        total_facts = coverage.total_facts
        missing_facts_list = coverage.missing_facts
        missing = len(missing_facts_list)
        # Recomputes the same deterministic fact list score_coverage() builds
        # internally, purely to get per-category totals -- cheap (a list
        # comprehension over already-in-memory parser output), not a second judge
        # call, so this doesn't add to the run's LLM-call count.
        facts_by_category = categorize_facts(build_coverable_facts(parsed))

        if reference_overview is not None and bert_scorer is not None:
            text_overlap = score_text_overlap(
                repo_name=parsed.metadata.name,
                context_variant=result.context_variant,
                summary_text=result.output.raw_text,
                reference_overview=reference_overview,
                bert_scorer=bert_scorer,
            )

        if enable_quality_judge:
            quality = score_summary_quality(
                repo_name=parsed.metadata.name,
                context_variant=result.context_variant,
                summary_text=result.output.raw_text,
                judge_model=judge_model,
                host=quality_judge_host,
            )
    else:
        # A failed/empty generation has nothing to judge -- don't spend a judge call.
        hallucination_judged = False
        hallucination_score = 0.0
        total_claims = 0
        unsupported = 0
        coverage_judged = False
        coverage_score = 0.0
        total_facts = 0
        missing = 0

    known_terms = [parsed.metadata.detected_framework or "", parsed.metadata.detected_language or ""]
    generated_overview, _ = extract_overview(result.output.raw_text) if result.status == RunStatus.SUCCESS else ("", False)
    failure_tags = analyze_failures(
        repo_name=parsed.metadata.name,
        model=result.model,
        context_variant=result.context_variant.value,
        unsupported_claims=unsupported_claims_list,
        missing_facts=missing_facts_list,
        generated_overview=generated_overview,
        known_terms=known_terms,
        hallucination_judged=hallucination_judged,
        coverage_judged=coverage_judged,
    ).tags

    return EvaluationRow(
        repo_name=parsed.metadata.name,
        source_url=parsed.metadata.source_url,
        framework=parsed.metadata.detected_framework,
        model=result.model,
        context_variant=result.context_variant.value,
        run_status=result.status.value,
        latency_ms=result.metrics.latency_ms,
        input_tokens=result.metrics.input_tokens,
        output_tokens=result.metrics.output_tokens,
        estimated_cost_usd=result.metrics.estimated_cost_usd,
        judge_model=judge_model,
        seed=seed,
        temperature=temperature,
        self_judged=self_judged,
        hallucination_judged=hallucination_judged,
        hallucination_score=hallucination_score,
        total_claims=total_claims,
        unsupported_claims=unsupported,
        coverage_judged=coverage_judged,
        coverage_score=coverage_score,
        total_facts=total_facts,
        missing_facts=missing,
        diagram_scored=diagram is not None,
        diagram_module_f1=diagram.modules.f1 if diagram else None,
        diagram_import_f1=diagram.imports.f1 if diagram else None,
        diagram_endpoint_f1=diagram.endpoints.f1 if diagram else None,
        diagram_overall_f1=diagram.overall_f1 if diagram else None,
        text_overlap_scored=text_overlap is not None,
        bleu4=text_overlap.bleu4 if text_overlap else None,
        rouge_l=text_overlap.rouge_l if text_overlap else None,
        meteor=text_overlap.meteor if text_overlap else None,
        bertscore_f1=text_overlap.bertscore_f1 if text_overlap else None,
        quality_judged=quality is not None,
        quality_mean_score=quality.mean_score if quality else None,
        quality_completeness=quality.scores["completeness"].score if quality else None,
        quality_conciseness=quality.scores["conciseness"].score if quality else None,
        quality_correctness=quality.scores["correctness"].score if quality else None,
        quality_cohesiveness=quality.scores["cohesiveness"].score if quality else None,
        quality_domain_specificity=quality.scores["domain_specificity"].score if quality else None,
        failure_tags=",".join(tag.value for tag in failure_tags),
        summary_text=result.output.raw_text if result.status == RunStatus.SUCCESS else "",
        missing_facts_list=json.dumps(missing_facts_list),
        unsupported_claims_list=json.dumps(unsupported_claims_list),
        facts_by_category=json.dumps(facts_by_category),
    )


def _failure_row(url: str, judge_model: str, error: str) -> EvaluationRow:
    return EvaluationRow(
        repo_name=url,
        source_url=url,
        framework=None,
        model="",
        context_variant="",
        run_status="failed",
        latency_ms=0,
        input_tokens=0,
        output_tokens=0,
        estimated_cost_usd=0.0,
        judge_model=judge_model,
        self_judged=False,
        hallucination_judged=False,
        hallucination_score=0.0,
        total_claims=0,
        unsupported_claims=0,
        coverage_judged=False,
        coverage_score=0.0,
        total_facts=0,
        missing_facts=0,
        error=error,
    )


def write_csv(rows: list[EvaluationRow], path: str | Path) -> None:
    path = Path(path)
    fieldnames = list(EvaluationRow.model_fields.keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.model_dump())


def write_sqlite(rows: list[EvaluationRow], path: str | Path, table: str = "evaluation_runs") -> None:
    """Append rows into a SQLite table (created if absent). Unlike the CSV -- which is
    overwritten each run -- this accumulates across runs, so results from multiple
    batches (or the same battery re-run) build up in one queryable place. A run_at
    timestamp column distinguishes them."""
    fields = list(EvaluationRow.model_fields.keys())
    columns = ", ".join(f'"{f}"' for f in fields)
    placeholders = ", ".join("?" for _ in fields)
    conn = sqlite3.connect(str(path))
    try:
        # Columns declared with no type affinity so each value keeps its native
        # storage class (int/float/text) for analysis, instead of everything becoming
        # TEXT. run_at keeps a real type for its default.
        col_defs = ", ".join(f'"{f}"' for f in fields)
        conn.execute(
            f'CREATE TABLE IF NOT EXISTS "{table}" '
            f'(run_at TEXT DEFAULT CURRENT_TIMESTAMP, {col_defs})'
        )
        conn.executemany(
            f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})',
            [tuple(_sqlite_value(v) for v in row.model_dump().values()) for row in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _sqlite_value(value: object) -> object:
    # sqlite3 handles str/int/float/None natively; coerce bools to 0/1 and anything
    # else (shouldn't occur) to str, so the insert never raises on an odd type.
    if isinstance(value, bool):
        return int(value)
    if value is None or isinstance(value, (str, int, float)):
        return value
    return str(value)


def main() -> None:
    import argparse

    from dotenv import load_dotenv

    load_dotenv()  # standalone script -- pick up .env (Neo4j creds, Ollama host, models)

    parser = argparse.ArgumentParser(description="Run the evaluation battery over repos.")
    parser.add_argument(
        "urls", nargs="*", help="GitHub repo URLs to evaluate (omit when using --context-pack)"
    )
    parser.add_argument(
        "--context-pack",
        default=None,
        help="Read contexts and parses from a pack built by scripts/build_context_pack.py "
        "instead of cloning and querying Neo4j. Required on a GPU node with no database "
        "or no network; URLs are ignored when this is set.",
    )
    parser.add_argument(
        "--models", nargs="*", default=None, help="Models to compare (default: the .env comparison models)"
    )
    parser.add_argument("--judge-model", default=None, help="Model used as the hallucination judge")
    parser.add_argument(
        "--annotations-dir",
        default=None,
        help="Directory of <repo_name>.json expected-structure annotations for diagram scoring",
    )
    parser.add_argument(
        "--reference-summaries-dir",
        default=None,
        help="Directory of <repo_name>.json reference summaries for text-overlap metrics "
        "(BLEU-4/ROUGE-L/METEOR/BERTScore) -- see reference_summaries/README.md. Downloads "
        "a BERTScore model on first use unless --no-bertscore is also given.",
    )
    parser.add_argument(
        "--no-bertscore",
        action="store_true",
        help="Skip BERTScore specifically (still computes BLEU-4/ROUGE-L/METEOR) -- use this "
        "to avoid the model download/load cost if BERTScore isn't needed yet.",
    )
    parser.add_argument(
        "--quality-judge",
        action="store_true",
        help="Also run the G-Eval rubric scorer (quality_judge.py) on every successful summary. "
        "OFF by default: multiplies judge calls by 5 (one per criterion) on top of the "
        "hallucination/coverage judges that always run.",
    )
    parser.add_argument(
        "--quality-judge-host",
        default=None,
        help="Ollama host for the G-Eval judge's raw /api/generate calls (default: $OLLAMA_HOST)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Generator sampling seed. Repeat a battery with different seeds to measure "
        "run-to-run variance; the judge's seed is fixed regardless.",
    )
    parser.add_argument(
        "--temperature", type=float, default=0.0,
        help="Generator temperature (default 0, greedy -- the published condition). "
        "The judge always runs at 0.",
    )
    parser.add_argument(
        "--variants", nargs="*", default=None, choices=[v.value for v in ContextVariant],
        help="Only generate these arms (default: every arm in the pack)",
    )
    parser.add_argument("--out", default="evaluation_results.csv", help="Output CSV path")
    parser.add_argument(
        "--sqlite", default=None, help="Optional SQLite DB path to also append results to"
    )
    args = parser.parse_args()

    bert_scorer = None
    if args.reference_summaries_dir and args.no_bertscore:
        from app.evaluation.text_overlap import _bleu4, _meteor, _rouge_l  # noqa: F401 -- documents what still runs

        def bert_scorer(references: list[str], hypotheses: list[str]) -> list[float]:
            return [0.0] * len(hypotheses)  # placeholder -- BLEU/ROUGE/METEOR still score normally

    pack = None
    if args.context_pack:
        pack = ContextPack.read(args.context_pack)
        print(
            f"Loaded context pack: {len(pack.repositories)} repositories, built "
            f"{pack.built_at}, max_raw_chars={pack.max_raw_chars}"
            + (f" -- {pack.notes}" if pack.notes else "")
        )
    elif not args.urls:
        parser.error("give repo URLs, or --context-pack")

    provider = OllamaProvider(seed=args.seed, temperature=args.temperature)
    # Judging is never sampled: a fixed grader is what makes repeated runs comparable.
    judge_provider = OllamaProvider()
    rows = run_evaluation(
        args.urls,
        provider=provider,
        judge_provider=judge_provider,
        variants=[ContextVariant(v) for v in args.variants] if args.variants else None,
        models=args.models,
        judge_model=args.judge_model,
        annotations_dir=args.annotations_dir,
        reference_summaries_dir=args.reference_summaries_dir,
        bert_scorer=bert_scorer,
        enable_quality_judge=args.quality_judge,
        quality_judge_host=args.quality_judge_host,
        context_pack=pack,
    )
    write_csv(rows, args.out)
    if args.sqlite:
        write_sqlite(rows, args.sqlite)
    failures = sum(1 for r in rows if r.run_status == "failed")
    sqlite_note = f" and appended to {args.sqlite}" if args.sqlite else ""
    print(f"Wrote {len(rows)} rows to {args.out}{sqlite_note} ({failures} repo-level failures).")


if __name__ == "__main__":
    main()
