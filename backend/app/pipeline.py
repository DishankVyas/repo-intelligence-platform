"""
Top-level orchestration: URL in, ParsedRepository out (analyze_repository), or URL in,
LLM-generated summary out (generate_repository_summary). This is what the FastAPI
endpoint (and later, the evaluation harness) calls -- it doesn't know about
GitPython, tree-sitter, Neo4j, or Ollama directly, just these two functions.
"""

from __future__ import annotations
import re 
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # never imported at runtime on the cluster
    from neo4j import Driver
    from neo4j.exceptions import DriverError, Neo4jError

from pydantic import BaseModel

from app.acquisition.clone import AcquiredRepo, CloneFailedError, InvalidRepoUrlError, RepoTooLargeError, clone_repository
from app.evaluation.coverage import CoverageResult, score_coverage
from app.evaluation.failure_analysis import FailureTag, analyze_failures
from app.evaluation.hallucination import HallucinationResult, score_summary
from app.evaluation.oracle import (
    OracleCoverage,
    OracleHallucination,
    score_coverage_oracle,
    score_hallucination_oracle,
)
from app.evaluation.text_overlap import TextOverlapResult, extract_overview, score_text_overlap
from app.parsers.base import UnsupportedFrameworkError
from app.providers.base import BaseLLMProvider
from app.providers.ollama_provider import OllamaProvider
from app.providers.prompts import SUMMARY_PROMPT_TEMPLATE
from app.schemas.llm_result import ContextVariant, LLMResult, RunStatus
from app.schemas.parser_schema import ParsedRepository

# Heavy infrastructure imports (neo4j, tree-sitter, graph builders, context builder)
# are deferred to inside the functions that need them.  generate_across_contexts() --
# the only function called on the cluster -- never touches any of them.

REQUIRED_SUMMARY_KEYS = {"overview", "tech_stack", "services", "dependencies"}
# Asked for by the prompt and scored, but NOT required for the output to count as valid.
# Endpoints and components were added so that coverage can score the ~46% of ground-truth
# facts (endpoints, classes/services) the earlier four-key shape had nowhere to express --
# see docs/REPORT.md 5.7. Keeping them optional means an older model that omits them
# still parses instead of being recorded as a failed run, so the two changes stay
# separable when reading results.
OPTIONAL_SUMMARY_KEYS = {"endpoints", "components"}

class AnalysisError(Exception):
    """Wraps all recoverable failure modes with a consistent message for the API layer."""


class PipelineInfrastructureError(Exception):
    """Neo4j (or another required service) is unreachable -- distinct from
    AnalysisError (bad input) because it's a 503-shaped problem (retry later, or
    start the service), not a 400-shaped one (fix your request)."""


def analyze_repository(url: str, max_size_mb: int = 200) -> ParsedRepository:
    acquired: AcquiredRepo | None = None
    try:
        acquired = clone_repository(url, max_size_mb=max_size_mb)
        from app.parsers.registry import parse_repository  # deferred: not needed on cluster
        parsed = parse_repository(acquired.local_path)
        parsed.metadata.source_url = url
        parsed.metadata.commit_sha = acquired.commit_sha
        return parsed
    except (InvalidRepoUrlError, CloneFailedError, RepoTooLargeError, UnsupportedFrameworkError) as e:
        raise AnalysisError(str(e)) from e
    finally:
        if acquired is not None:
            acquired.cleanup()


def generate_repository_summary(
    url: str,
    model: str | None = None,
    max_size_mb: int = 200,
    driver: Driver | None = None,
    provider: BaseLLMProvider | None = None,
) -> tuple[ParsedRepository, LLMResult]:
    """Week 2's real, no-mocks pipeline: GitHub URL -> parser -> Neo4j -> context ->
    Ollama -> summary. Uses the knowledge_graph representation (the richest) -- the
    3-way ablation across all representations is Week 3 work.

    `driver`/`provider` are injectable so tests don't need a live Neo4j/Ollama; when
    omitted, real connections are created and (for an owned driver) closed here.
    """
    parsed = analyze_repository(url, max_size_mb=max_size_mb)

    owns_driver = driver is None
    # Deferred: these are never called on the cluster (context_pack path bypasses this fn)
    from app.db.neo4j_client import get_driver
    from app.graph.builder import ensure_constraints, write_parsed_repository
    from app.context.builder import build_context
    from neo4j.exceptions import DriverError, Neo4jError
    driver = driver or get_driver()
    provider = provider or OllamaProvider()
    try:
        try:
            ensure_constraints(driver)
            write_parsed_repository(driver, parsed)
            context = build_context(driver, parsed.metadata.name, ContextVariant.KNOWLEDGE_GRAPH)
        except (Neo4jError, DriverError, OSError) as e:
            raise PipelineInfrastructureError(
                f"Couldn't reach Neo4j -- is it running? ({e})"
            ) from e
    finally:
        if owns_driver:
            driver.close()

    result = provider.generate_summary(
        context=context,
        repo_name=parsed.metadata.name,
        context_variant=ContextVariant.KNOWLEDGE_GRAPH,
        prompt_template=SUMMARY_PROMPT_TEMPLATE,
        model=model or provider.default_model,
    )

    if result.status == RunStatus.SUCCESS:
        result.output.parsed, result.output.valid = _try_parse_summary_json(result.output.raw_text)

    return parsed, result


def generate_repository_diagram(
    url: str,
    max_size_mb: int = 200,
    driver: Driver | None = None,
) -> tuple[ParsedRepository, str]:
    """Week 3's diagram generation: GitHub URL -> parser -> Neo4j -> Mermaid
    flowchart. No LLM call anywhere in this path -- the diagram is generated
    directly from the graph (see graph/diagram.py's docstring), deterministic and
    free, unlike the summary pipeline."""
    parsed = analyze_repository(url, max_size_mb=max_size_mb)

    owns_driver = driver is None
    from app.db.neo4j_client import get_driver  # deferred: not needed on cluster
    from app.graph.builder import ensure_constraints, write_parsed_repository
    from app.graph.diagram import generate_architecture_diagram
    from neo4j.exceptions import DriverError, Neo4jError
    driver = driver or get_driver()
    try:
        try:
            ensure_constraints(driver)
            write_parsed_repository(driver, parsed)
            diagram = generate_architecture_diagram(driver, parsed.metadata.name)
        except (Neo4jError, DriverError, OSError) as e:
            raise PipelineInfrastructureError(
                f"Couldn't reach Neo4j -- is it running? ({e})"
            ) from e
    finally:
        if owns_driver:
            driver.close()

    return parsed, diagram


def _strip_code_fence(raw_text: str) -> str:
    """Local models often wrap JSON output in a markdown code fence (```json ... ```)
    even when the prompt asks for raw JSON. Strip that fence before parsing, since
    json.loads() otherwise fails on the leading/trailing backticks."""
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw_text, re.DOTALL)
    if match:
        return match.group(1)
    return raw_text


def _try_parse_summary_json(raw_text: str) -> tuple[dict | None, bool]:
    """Downstream parsing of the model's raw_text into the structured shape the
    prompt asked for (see providers/base.py: this is deliberately not the
    provider's job). Malformed or off-schema output is a real possibility with
    local models, not just a hypothetical -- treated as invalid, not raised."""
    try:
        parsed = json.loads(_strip_code_fence(raw_text))
    except json.JSONDecodeError:
        return None, False
    if not isinstance(parsed, dict) or not REQUIRED_SUMMARY_KEYS.issubset(parsed.keys()):
        return None, False
    return parsed, True


def _default_comparison_models() -> list[str]:
    """Read lazily (not a module-level constant) so this reflects .env even if
    load_dotenv() runs after this module is first imported -- see main.py's
    load_dotenv() placement note for why that ordering isn't actually guaranteed
    to matter here, but this avoids depending on it regardless."""
    return [
        os.environ.get("OLLAMA_MODEL_PRIMARY", "qwen2.5-coder:7b"),
        os.environ.get("OLLAMA_MODEL_FALLBACK", "llama3.1:8b"),
        os.environ.get("OLLAMA_MODEL_DIVERSITY", "gpt-oss:20b"),
    ]


def _read_raw_source(
    repo_path: Path, parsed: ParsedRepository, model: str | None = None
) -> str:
    """RAW ContextVariant: the repo's own source text, concatenated per-module and
    truncated to fit within the token budget -- OLLAMA_NUM_CTX minus template overhead,
    counted in `model`'s own tokens (cl100k proxy when None; see token_budget.py)."""
    import logging
    from app.context.token_budget import compute_context_token_budget, get_encoder
    
    logger = logging.getLogger(__name__)
    enc = get_encoder(model)
    budget = compute_context_token_budget(model=model)
    
    chunks: list[str] = []
    total_tokens = 0
    separator_tokens = len(enc.encode("\n\n"))
    
    # Calculate the full raw source tokens to fast-fail if exceeded by rounding margin
    full_raw_text = ""
    for module in parsed.modules:
        try:
            text = (repo_path / module.path).read_text(encoding="utf-8", errors="replace")
            full_raw_text += f"// ---- {module.path} ----\n{text}\n\n"
        except OSError:
            continue
            
    total_raw_tokens = len(enc.encode(full_raw_text))
    margin = 50
    if total_raw_tokens > (budget + margin):
        warning_msg = f"WARNING: Repo {parsed.metadata.name} raw source exceeds token budget! ({total_raw_tokens} > {budget} tokens). It will be truncated."
        logger.warning(warning_msg)
        print(warning_msg)

    for module in parsed.modules:
        try:
            text = (repo_path / module.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
            
        chunk = f"// ---- {module.path} ----\n{text}"
        chunk_tokens = len(enc.encode(chunk))
        
        cost = chunk_tokens + (separator_tokens if chunks else 0)
        
        if total_tokens + cost > budget:
            remaining_tokens = budget - total_tokens - (separator_tokens if chunks else 0)
            if remaining_tokens > 0:
                # Truncate by tokens, not characters
                encoded_chunk = enc.encode(chunk)
                truncated_chunk = enc.decode(encoded_chunk[:remaining_tokens])
                chunks.append(truncated_chunk)
            break
            
        chunks.append(chunk)
        total_tokens += cost
        
    return "\n\n".join(chunks)


def run_representation_ablation(
    url: str,
    models: list[str] | None = None,
    max_size_mb: int = 200,
    driver: Driver | None = None,
    provider: BaseLLMProvider | None = None,
) -> tuple[ParsedRepository, list[LLMResult]]:
    """Week 3's 3-way representation ablation: the same repo, the same models, three
    different context representations (raw / dependency_graph / knowledge_graph) --
    the core independent variable of the research question. Returns one LLMResult
    per (model, representation) pair -- len(models) * 3 total.

    Clones and parses once (not reusing analyze_repository(), which cleans up its
    checkout before returning -- raw source has to be read before that happens, see
    _read_raw_source), writes to the graph once, then reuses that single context
    build across every model for dependency_graph/knowledge_graph.
    """
    models = models or _default_comparison_models()

    acquired: AcquiredRepo | None = None
    try:
        acquired = clone_repository(url, max_size_mb=max_size_mb)
        from app.parsers.registry import parse_repository  # deferred: not needed on cluster
        parsed = parse_repository(acquired.local_path)
        parsed.metadata.source_url = url
        parsed.metadata.commit_sha = acquired.commit_sha
        raw_context = _read_raw_source(acquired.local_path, parsed)
    except (InvalidRepoUrlError, CloneFailedError, RepoTooLargeError, UnsupportedFrameworkError) as e:
        raise AnalysisError(str(e)) from e
    finally:
        if acquired is not None:
            acquired.cleanup()

    owns_driver = driver is None
    from app.db.neo4j_client import get_driver  # deferred: not needed on cluster
    from app.graph.builder import ensure_constraints, write_parsed_repository
    from app.context.builder import build_context
    from neo4j.exceptions import DriverError, Neo4jError
    driver = driver or get_driver()
    provider = provider or OllamaProvider()
    try:
        try:
            ensure_constraints(driver)
            write_parsed_repository(driver, parsed)
            dependency_graph_context = build_context(
                driver, parsed.metadata.name, ContextVariant.DEPENDENCY_GRAPH
            )
            knowledge_graph_context = build_context(
                driver, parsed.metadata.name, ContextVariant.KNOWLEDGE_GRAPH
            )
        except (Neo4jError, DriverError, OSError) as e:
            raise PipelineInfrastructureError(
                f"Couldn't reach Neo4j -- is it running? ({e})"
            ) from e
    finally:
        if owns_driver:
            driver.close()

    contexts = {
        ContextVariant.RAW: raw_context,
        ContextVariant.DEPENDENCY_GRAPH: dependency_graph_context,
        ContextVariant.KNOWLEDGE_GRAPH: knowledge_graph_context,
    }

    return parsed, generate_across_contexts(parsed, contexts, models, provider)


def generate_across_contexts(
    parsed: ParsedRepository,
    contexts: dict[ContextVariant, str],
    models: list[str] | None = None,
    provider: BaseLLMProvider | None = None,
) -> list[LLMResult]:
    """The generation half of the ablation: every model x every already-built context.

    Split out from run_representation_ablation() because this is the only part that
    needs a GPU. Acquisition, parsing and the Neo4j round-trip that produce `contexts`
    are CPU work and can happen anywhere -- which is what lets the batch run on a
    job-scheduled GPU node that has neither a Neo4j nor a route to github.com. See
    scripts/build_context_pack.py.
    """
    models = models or _default_comparison_models()
    provider = provider or OllamaProvider()

    results: list[LLMResult] = []
    for model in models:
        for variant, context in contexts.items():
            result = provider.generate_summary(
                context=context,
                repo_name=parsed.metadata.name,
                context_variant=variant,
                prompt_template=SUMMARY_PROMPT_TEMPLATE,
                model=model,
            )
            if result.status == RunStatus.SUCCESS:
                result.output.parsed, result.output.valid = _try_parse_summary_json(
                    result.output.raw_text
                )
            results.append(result)

    return results


class ScoredResult(BaseModel):
    """One ablation result paired with its hallucination (precision-like: are the
    claims it made true) and coverage (recall-like: how much of the ground truth did
    it mention) scores. Both are None for a run that failed (nothing to grade).

    `text_overlap` is None whenever no reference_summaries/<repo>.json exists for
    this repo (BLEU/ROUGE/METEOR need a reference text to compare against) -- see
    reference_summaries/README.md. BERTScore is deliberately left out of the live
    endpoint (unlike the batch harness, which can enable it via --reference-summaries-dir
    without --no-bertscore): it downloads and loads a model on first use, which would
    make an interactive dashboard comparison unpredictably slow. `failure_tags` is
    always populated when the run succeeded -- it costs no extra judge call, since it's
    derived entirely from the hallucination/coverage signals already computed above.

    `oracle_coverage`/`oracle_hallucination` are the deterministic, parser-grounded
    scores for the same summary -- no model involved, so they cost no judge call and
    cannot vary between runs. They are reported ALONGSIDE the judge scores rather than
    instead of them: the oracle cannot credit paraphrase (it matches identifiers), while
    the judge can but is unreliable in both directions. Showing both is what makes the
    disagreement visible, which is the point (see docs/REPORT.md 5.8)."""

    result: LLMResult
    hallucination: HallucinationResult | None = None
    coverage: CoverageResult | None = None
    oracle_coverage: OracleCoverage | None = None
    oracle_hallucination: OracleHallucination | None = None
    text_overlap: TextOverlapResult | None = None
    failure_tags: list[FailureTag] = []


# OFF by default. Reference summaries feed only BLEU-4/ROUGE-L/METEOR, and those were
# measured at ~0.015 correlation with no discriminative power (REPORT.md 5.5.4) -- a
# negative result already reported, and one that does not need re-collecting per repo.
# They were also the only part of the ground truth requiring a human to write prose, so
# dropping them removes the annotation burden that scaled worst with dataset size.
# Pass --reference-summaries-dir explicitly to re-enable for a specific run.
DEFAULT_REFERENCE_SUMMARIES_DIR = None


def run_scored_ablation(
    url: str,
    models: list[str] | None = None,
    judge_model: str | None = None,
    max_size_mb: int = 200,
    driver: Driver | None = None,
    provider: BaseLLMProvider | None = None,
    reference_summaries_dir: str | Path | None = DEFAULT_REFERENCE_SUMMARIES_DIR,
) -> tuple[ParsedRepository, list[ScoredResult]]:
    """The ablation plus hallucination + coverage scores per successful result --
    what the /compare endpoint and dashboard use, so the comparison shows quality
    (not just latency/tokens) along both the precision axis (hallucination) and the
    recall axis (coverage). One shared provider does generation and both judging
    passes. The judge reads ground-truth facts from the ParsedRepository, so scoring
    needs no Neo4j and happens after the driver is released."""
    owns_driver = driver is None
    from app.db.neo4j_client import get_driver  # deferred: not needed on cluster
    driver = driver or get_driver()
    provider = provider or OllamaProvider()
    judge_model = judge_model or provider.default_model

    # Refuse to grade a generator with itself. REPORT.md 6.2 records that self-judging
    # reverses the ranking of representations, so a run where the judge is also a writer
    # produces numbers that look fine and are wrong -- the failure mode this project
    # exists to document. Fail loudly rather than emit them: the per-row `self_judged`
    # flag marks the condition after the fact, but by then the run has already cost
    # minutes and the reader has a plausible table in front of them.
    requested_models = models if models is not None else _default_comparison_models()
    if judge_model in requested_models:
        raise AnalysisError(
            f"Judge model {judge_model!r} is also one of the generators "
            f"({', '.join(requested_models)}), so that arm would grade its own output. "
            "Pass a judge_model that is not in the generator list, or change "
            "OLLAMA_MODEL_PRIMARY/FALLBACK/DIVERSITY so they do not include it."
        )

    try:
        parsed, results = run_representation_ablation(
            url, models=models, max_size_mb=max_size_mb, driver=driver, provider=provider
        )
    finally:
        if owns_driver:
            driver.close()

    reference_overview = _load_reference_overview(parsed.metadata.name, reference_summaries_dir)
    known_terms = [parsed.metadata.detected_framework or "", parsed.metadata.detected_language or ""]

    scored: list[ScoredResult] = []
    for result in results:
        hallucination = None
        coverage = None
        oracle_coverage = None
        oracle_hallucination = None
        text_overlap = None
        failure_tags: list[FailureTag] = []
        if result.status == RunStatus.SUCCESS:
            # Deterministic scores first: pure CPU, no model call, so they are always
            # present even if a judge call later fails or its verdict won't parse.
            oracle_coverage = score_coverage_oracle(parsed, result.output.raw_text)
            oracle_hallucination = score_hallucination_oracle(parsed, result.output.raw_text)
            hallucination = score_summary(
                provider,
                parsed,
                result.output.raw_text,
                result.context_variant,
                judge_model=judge_model,
            )
            coverage = score_coverage(
                provider,
                parsed,
                result.output.raw_text,
                result.context_variant,
                judge_model=judge_model,
            )
            if reference_overview is not None:
                text_overlap = score_text_overlap(
                    repo_name=parsed.metadata.name,
                    context_variant=result.context_variant,
                    summary_text=result.output.raw_text,
                    reference_overview=reference_overview,
                )
            generated_overview, _ = extract_overview(result.output.raw_text)
            failure_tags = analyze_failures(
                repo_name=parsed.metadata.name,
                model=result.model,
                context_variant=result.context_variant.value,
                unsupported_claims=hallucination.unsupported_claims,
                missing_facts=coverage.missing_facts,
                generated_overview=generated_overview,
                known_terms=known_terms,
                hallucination_judged=hallucination.judged,
                coverage_judged=coverage.judged,
            ).tags
        scored.append(
            ScoredResult(
                result=result,
                hallucination=hallucination,
                coverage=coverage,
                oracle_coverage=oracle_coverage,
                oracle_hallucination=oracle_hallucination,
                text_overlap=text_overlap,
                failure_tags=failure_tags,
            )
        )

    return parsed, scored


def _load_reference_overview(repo_name: str, reference_summaries_dir: str | Path | None) -> str | None:
    if reference_summaries_dir is None:
        return None
    ref_path = Path(reference_summaries_dir) / f"{repo_name}.json"
    if not ref_path.exists():
        return None
    try:
        data = json.loads(ref_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    overview = data.get("overview")
    return overview if isinstance(overview, str) else None
