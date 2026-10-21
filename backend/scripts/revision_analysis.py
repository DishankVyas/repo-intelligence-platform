"""CICT-2026 revision analyses -- local, CPU only, no model calls.

Reads the three scaled batteries plus their gemma2:27b and mistral:7b-instruct rejudge
passes, and re-scores every summary with the deterministic oracle (app/evaluation/
oracle.py). Everything is restricted to ONE pipeline version: the 2026-09-03 pass.
The 2026-09-02 pass predates commit de2b83f (raw token-budget fix) and its prompts
differ in nearly every cell, so mixing the two is mixing two experimental conditions.

Answers, per reviewer item:
  R3  KG-vs-raw under four graders: gemma2:9b, gemma2:27b, mistral:7b-instruct (a
      different model family), and the deterministic oracle (no model at all).
  W2  the same contrasts restricted to repositories whose raw source was never
      truncated, so representation is not confounded with information loss.
  R4  CodeLlama prompt overflow, measured with CodeLlama's own tokenizer.

    python -m scripts.revision_analysis            # from backend/
"""
from __future__ import annotations

import csv
import sqlite3
import statistics as st
from pathlib import Path

from scipy.stats import wilcoxon

from app.evaluation.context_pack import ContextPack
from app.evaluation.oracle import score_coverage_oracle, score_hallucination_oracle

BACKEND = Path(__file__).resolve().parent.parent
RESULTS = BACKEND / "evaluation_results"
PACK = BACKEND.parent / "context_pack.json"
OUT_TXT = RESULTS / "REVISION_ANALYSIS.txt"
OUT_CSV = RESULTS / "revision_rows.csv"

MODELS = {
    "codellama:13b-instruct": "codellama13b",
    "qwen2.5-coder:14b": "qwen14b",
    "qwen2.5-coder:32b": "qwen32b",
}
PASS2 = "2026-09-03"
ARMS = ["raw", "dependency_graph", "knowledge_graph"]
COMPARISONS = [("dependency_graph", "raw"), ("knowledge_graph", "raw"), ("knowledge_graph", "dependency_graph")]
SECOND_JUDGES = {"gemma27b": "gemma2_27b", "mistral7b": "mistral_7b-instruct"}

# (column, higher_is_better, label)
METRICS = [
    ("cov_gemma9b", True, "coverage, judge gemma2:9b (primary)"),
    ("cov_gemma27b", True, "coverage, judge gemma2:27b"),
    ("cov_mistral7b", True, "coverage, judge mistral:7b-instruct"),
    ("cov_oracle_strict", True, "coverage, deterministic oracle STRICT"),
    ("cov_oracle_lenient", True, "coverage, deterministic oracle LENIENT"),
    ("hal_gemma9b", False, "hallucination, judge gemma2:9b (primary)"),
    ("hal_gemma27b", False, "hallucination, judge gemma2:27b"),
    ("hal_mistral7b", False, "hallucination, judge mistral:7b-instruct"),
    ("hal_oracle", False, "unsupported-identifier rate, deterministic oracle"),
]


def _truthy(x) -> bool:
    return str(x) in ("1", "True", "true")


def _float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_battery(stem: str) -> list[dict]:
    """Successful rows in rowid order -- the order rejudge.py consumed them in."""
    c = sqlite3.connect(RESULTS / f"battery_{stem}.db")
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM evaluation_runs WHERE run_status='success' "
        "AND summary_text IS NOT NULL AND summary_text != '' ORDER BY rowid"
    )]
    c.close()
    return rows


def attach_rejudge(rows: list[dict], stem: str, key: str, judge_slug: str) -> None:
    """rejudge.py judged every successful source row once, in source order, so the
    k-th rejudged row is the k-th source row. Verified per row on (repo, model, arm)
    rather than assumed -- a misalignment raises instead of silently mispairing."""
    c = sqlite3.connect(RESULTS / f"rejudge_{judge_slug}_battery_{stem}.db")
    c.row_factory = sqlite3.Row
    rj = [dict(r) for r in c.execute("SELECT * FROM rejudged ORDER BY rowid")]
    c.close()
    if len(rj) != len(rows):
        raise SystemExit(f"{stem}/{key}: {len(rj)} rejudged rows vs {len(rows)} source rows")
    for src, r in zip(rows, rj):
        if (src["repo_name"], src["model"], src["context_variant"]) != (
            r["repo_name"], r["model"], r["context_variant"]
        ):
            raise SystemExit(f"{stem}/{key}: rejudge rows are not aligned with the source DB")
        src[f"cov_{key}"] = _float(r["coverage_score"]) if _truthy(r["coverage_judged"]) else None
        src[f"hal_{key}"] = _float(r["hallucination_score"]) if _truthy(r["hallucination_judged"]) else None


def raw_fits(pack: ContextPack) -> dict[str, bool]:
    """True when the packed raw context holds every module in full. The builder writes
    one '// ---- <path> ----' header per module and stops at the token budget, so a
    missing header means at least one file never reached the model."""
    fits = {}
    for repo in pack.repositories:
        raw = _contexts(repo).get("raw", "")
        headers = {line[8:-5] for line in raw.splitlines() if line.startswith("// ---- ") and line.endswith(" ----")}
        modules = {m.path for m in repo.parsed.modules}
        fits[repo.parsed.metadata.name] = modules <= headers and not _last_chunk_cut(repo)
    return fits


def _last_chunk_cut(repo) -> bool:
    from app.context.token_budget import compute_context_token_budget, get_encoder

    return len(get_encoder().encode(_contexts(repo).get("raw", ""))) >= compute_context_token_budget()


def _contexts(repo) -> dict[str, str]:
    """Plain-string keys: ContextVariant hashes by member name, not by value."""
    return {k.value: v for k, v in repo.context_map().items()}


def codellama_overflow(pack: ContextPack) -> dict[tuple[str, str], int] | None:
    """Prompt length in CodeLlama's own tokens, or None if the tokenizer is unavailable
    (it is a ~2MB download from the Hugging Face hub, not model weights)."""
    try:
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer
    except ImportError:
        return None
    from app.providers.prompts import SUMMARY_PROMPT_TEMPLATE

    tok = Tokenizer.from_file(hf_hub_download("codellama/CodeLlama-13b-Instruct-hf", "tokenizer.json"))
    system = "You are a repository analysis assistant. Respond only with valid JSON."
    return {
        (r.parsed.metadata.name, arm): len(tok.encode(system + "\n" + SUMMARY_PROMPT_TEMPLATE.format(context=ctx)).ids)
        for r in pack.repositories
        for arm, ctx in _contexts(r).items()
    }


def rank_biserial(a: list[float], b: list[float]) -> float:
    """Matched-pairs rank-biserial correlation for a - b; +1 means every pair favours a."""
    diffs = [x - y for x, y in zip(a, b) if x != y]
    if not diffs:
        return 0.0
    order = sorted(range(len(diffs)), key=lambda i: abs(diffs[i]))
    ranks = [0.0] * len(diffs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and abs(diffs[order[j + 1]]) == abs(diffs[order[i]]):
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    plus = sum(r for r, d in zip(ranks, diffs) if d > 0)
    minus = sum(r for r, d in zip(ranks, diffs) if d < 0)
    return (plus - minus) / (plus + minus)


def holm(pvalues: list[float | None]) -> list[float | None]:
    present = sorted((p, i) for i, p in enumerate(pvalues) if p is not None)
    adjusted: list[float | None] = [None] * len(pvalues)
    running = 0.0
    m = len(present)
    for rank, (p, i) in enumerate(present):
        running = max(running, min(1.0, (m - rank) * p))
        adjusted[i] = running
    return adjusted


def contrast_block(rows: list[dict], metric: str, repos: set[str] | None = None) -> list[str]:
    cells = {arm: {r["repo_name"]: r[metric] for r in rows
                   if r["context_variant"] == arm and r.get(metric) is not None
                   and (repos is None or r["repo_name"] in repos)}
             for arm in ARMS}
    means = "  ".join(
        f"{arm[:3]} {st.mean(v.values()):.3f}±{st.pstdev(v.values()):.3f} (n={len(v)})" if v else f"{arm[:3]} --"
        for arm, v in cells.items()
    )
    tests = []
    for a, b in COMPARISONS:
        common = sorted(set(cells[a]) & set(cells[b]))
        xa, xb = [cells[a][k] for k in common], [cells[b][k] for k in common]
        ties = sum(x == y for x, y in zip(xa, xb))
        p = wilcoxon(xa, xb).pvalue if len(common) >= 2 and ties < len(common) else None
        tests.append((a, b, len(common), ties, p, rank_biserial(xa, xb)))
    adj = holm([t[4] for t in tests])
    out = [f"    means (±SD across repos): {means}"]
    for (a, b, n, ties, p, r), h in zip(tests, adj):
        ps = "  n/a " if p is None else f"{p:.4f}"
        hs = "  n/a " if h is None else f"{h:.4f}"
        out.append(f"    {a[:3]} vs {b[:3]}: n={n:2d} ties={ties:2d} p={ps} holm={hs} r={r:+.2f}")
    return out


def main() -> None:
    pack = ContextPack.read(PACK)
    parsed_by_name = {r.parsed.metadata.name: r.parsed for r in pack.repositories}
    fits = raw_fits(pack)
    overflow = codellama_overflow(pack)

    all_rows: list[dict] = []
    for model, stem in MODELS.items():
        rows = load_battery(stem)
        for key, slug in SECOND_JUDGES.items():
            attach_rejudge(rows, stem, key, slug)
        rows = [r for r in rows if r["run_at"].startswith(PASS2)]
        for r in rows:
            parsed = parsed_by_name[r["repo_name"]]
            oc = score_coverage_oracle(parsed, r["summary_text"])
            oh = score_hallucination_oracle(parsed, r["summary_text"])
            r["cov_gemma9b"] = r["coverage_score"] if r["coverage_judged"] else None
            r["hal_gemma9b"] = r["hallucination_score"] if r["hallucination_judged"] else None
            r["cov_oracle_strict"] = oc.coverage_strict
            r["cov_oracle_lenient"] = oc.coverage_lenient
            # Undefined (0/0) when a summary contains no checkable identifier; excluded
            # rather than scored 0.0 so it cannot flatter whichever arm says least.
            r["hal_oracle"] = oh.unsupported_identifier_rate if oh.total_candidates else None
            r["raw_fits"] = fits[r["repo_name"]]
            r["codellama_tokens"] = overflow.get((r["repo_name"], r["context_variant"])) if overflow else None
        all_rows.extend(rows)

    full_fit = sorted(n for n, ok in fits.items() if ok)
    lines = [
        "CICT-2026 revision analyses -- generated by scripts/revision_analysis.py",
        f"Rows: the {PASS2} pass of each battery only (one pipeline version), successful generations.",
        "Tests: paired Wilcoxon per (model, metric); Holm within the family of three comparisons;",
        "r = matched-pairs rank-biserial, positive = first-named arm higher. For hallucination,",
        "lower is better, so a NEGATIVE r favours the first-named arm.",
        "",
    ]

    if overflow:
        cl = [r for r in all_rows if r["model"] == "codellama:13b-instruct"]
        over = [r for r in cl if r["codellama_tokens"] and r["codellama_tokens"] > 8192]
        at_4098 = [r for r in cl if r["input_tokens"] == 4098]
        both = [r for r in over if r["input_tokens"] == 4098]
        lines += [
            "== R4: CodeLlama context anomaly ==",
            f"  prompts over 8,192 CodeLlama tokens: {len(over)} of {len(cl)}",
            f"  rows recorded at exactly 4,098 input tokens: {len(at_4098)}",
            f"  overlap: {len(both)} -- every 4,098 row is an overflowing prompt"
            if len(both) == len(at_4098) == len(over) else f"  overlap: {len(both)} (NOT a perfect match)",
            "",
        ]

    lines += [
        f"== W2: repositories whose raw source reached the model in full: {len(full_fit)} of {len(fits)} ==",
        "  " + ", ".join(full_fit),
        "",
    ]

    for model in MODELS:
        rows = [r for r in all_rows if r["model"] == model]
        lines.append(f"######## {model} ########")
        for metric, _, label in METRICS:
            lines.append(f"  {label}")
            lines += contrast_block(rows, metric)
        subset = set(full_fit)
        if model == "codellama:13b-instruct" and overflow:
            subset = {n for n in subset if all(
                (overflow.get((n, a)) or 0) <= 8192 for a in ARMS)}
        lines.append(f"  -- W2 subset: raw untruncated{' and no CodeLlama overflow' if model.startswith('codellama') else ''} (n={len(subset)} repos) --")
        for metric, _, label in METRICS:
            if metric in ("cov_gemma9b", "hal_gemma9b", "cov_oracle_strict", "hal_oracle"):
                lines.append(f"  {label}")
                lines += contrast_block(rows, metric, repos=subset)
        lines.append("")

    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cols = ["repo_name", "model", "context_variant", "run_at", "input_tokens", "codellama_tokens", "raw_fits"] + [m for m, _, _ in METRICS]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)
    print("\n".join(lines))
    print(f"wrote {OUT_TXT.name} and {OUT_CSV.name}")


if __name__ == "__main__":
    main()
