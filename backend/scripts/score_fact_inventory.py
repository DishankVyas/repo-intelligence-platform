"""Score the parser and the stored summaries against an INDEPENDENT fact inventory.

CICT-2026 reviewer R1: the coverage fact list comes from the same parser that builds the
knowledge graph, so the knowledge graph is graded against its own source. This script
replaces that list with one a person wrote from the source code
(backend/fact_inventory/README.md) and reports:

  1. parser precision/recall per category against the inventory, and
  2. deterministic coverage (app/evaluation/oracle.py's matcher) of every stored summary
     against the inventory, with paired Wilcoxon contrasts between arms.

Only inventory files with "verified": true are used.

    python -m scripts.score_fact_inventory                    # score
    python -m scripts.score_fact_inventory --template <repo>  # start a new inventory
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics as st
from pathlib import Path

from scipy.stats import wilcoxon

from app.evaluation.context_pack import ContextPack
from app.evaluation.oracle import match_fact
from app.schemas.parser_schema import ParsedRepository
from scripts.revision_analysis import holm, rank_biserial

BACKEND = Path(__file__).resolve().parent.parent
INVENTORY_DIR = BACKEND / "fact_inventory"
RESULTS = BACKEND / "evaluation_results"
PACK = BACKEND.parent / "context_pack.json"
OUT = RESULTS / "FACT_INVENTORY_RESULTS.txt"

CATEGORIES = ["Framework", "Language", "Dependency", "Endpoint", "Class/Service", "Database entity"]
# The published batteries, restricted to their single consistent pass (see
# scripts/revision_analysis.py for why the 2026-09-02 pass is excluded).
PUBLISHED = {
    "battery_codellama13b.db": "2026-09-03",
    "battery_qwen14b.db": "2026-09-03",
    "battery_qwen32b.db": "2026-09-03",
}
ARM_ORDER = ["raw", "dependency_graph", "knowledge_graph", "rag_bm25"]
CONTRASTS = [("knowledge_graph", "raw"), ("knowledge_graph", "dependency_graph"), ("knowledge_graph", "rag_bm25")]

_PARAM = re.compile(r":[A-Za-z_]\w*|\{[^}]+\}|<[^>]+>")


def _norm(category: str, value: str) -> str:
    v = value.strip()
    if category == "Endpoint":
        method, _, path = v.partition(" ")
        path = _PARAM.sub(":param", path.strip()).rstrip("/") or "/"
        return f"{method.upper()} {path}"
    if category in ("Framework", "Language"):
        return v.lower()
    return v


def parser_facts(parsed: ParsedRepository) -> dict[str, set[str]]:
    """Every parser fact, uncapped -- this measures the parser, not the judge's prompt."""
    meta = parsed.metadata
    raw = {
        "Framework": [meta.detected_framework or ""],
        "Language": [meta.detected_language or ""],
        "Dependency": [d.name for d in parsed.dependencies.external],
        "Endpoint": [f"{e.method.value} {e.path}" for e in parsed.api_endpoints],
        "Class/Service": [c.name for c in parsed.classes],
        "Database entity": [d.name for d in parsed.database_entities],
    }
    return {c: {_norm(c, v) for v in vals if v} for c, vals in raw.items()}


def load_inventories() -> tuple[dict[str, dict], list[str]]:
    verified, skipped = {}, []
    for path in sorted(INVENTORY_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("verified") is True:
            verified[data["repo_name"]] = data
        else:
            skipped.append(path.stem)
    return verified, skipped


def write_template(repo_name: str) -> Path:
    pack = ContextPack.read(PACK)
    match = [r for r in pack.repositories if r.parsed.metadata.name == repo_name]
    if not match:
        raise SystemExit(f"{repo_name!r} is not in {PACK.name}")
    meta = match[0].parsed.metadata
    out = INVENTORY_DIR / f"{repo_name}.json"
    if out.exists():
        raise SystemExit(f"{out} already exists -- not overwriting an inventory in progress")
    out.write_text(json.dumps({
        "repo_name": repo_name,
        "source_url": meta.source_url,
        "commit_sha": meta.commit_sha,
        "annotator": "",
        "verified": False,
        "minutes_spent": None,
        "notes": "",
        # Framework and language are left blank on purpose: copying the parser's
        # detection into the template would seed the inventory from the tool under test.
        "facts": {c: [] for c in CATEGORIES},
    }, indent=2) + "\n", encoding="utf-8")
    return out


def _summary_rows() -> list[dict]:
    rows = []
    sources = [(RESULTS / db, prefix) for db, prefix in PUBLISHED.items()]
    sources += [(p, None) for p in sorted(RESULTS.glob("battery_rev_*.db"))]
    for db, prefix in sources:
        if not db.exists():
            continue
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        where = "run_status='success' AND summary_text != ''"
        if prefix:
            where += f" AND run_at LIKE '{prefix}%'"
        for r in c.execute(f"SELECT repo_name, model, context_variant, summary_text, "
                           f"{'seed' if prefix is None else 'NULL'} AS seed FROM evaluation_runs WHERE {where}"):
            rows.append({**dict(r), "source": db.stem})
        c.close()
    return rows


def coverage_against(inventory: dict, summary: str) -> float | None:
    facts = [f"{c}: {v}" for c in CATEGORIES for v in inventory["facts"].get(c, [])]
    if not facts:
        return None
    return sum(match_fact(f, summary).strict for f in facts) / len(facts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--template", metavar="REPO_NAME", help="write a blank inventory for this repository and exit")
    args = ap.parse_args()
    if args.template:
        print(f"wrote {write_template(args.template)}")
        return

    inventories, skipped = load_inventories()
    lines = ["Independent fact inventory -- generated by scripts/score_fact_inventory.py", ""]
    if skipped:
        lines.append(f"Skipped (not marked verified): {', '.join(skipped)}")
    if not inventories:
        lines.append("No verified inventories yet -- nothing to score. See fact_inventory/README.md.")
        OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines))
        return
    lines += [f"Verified inventories: {len(inventories)} -- {', '.join(sorted(inventories))}", ""]

    # 1. parser vs inventory
    pack = {r.parsed.metadata.name: r.parsed for r in ContextPack.read(PACK).repositories}
    lines.append("== 1. Parser against the independent inventory (precision / recall) ==")
    totals = {c: [0, 0, 0] for c in CATEGORIES}  # true positives, parser total, inventory total
    for name, inv in sorted(inventories.items()):
        got = parser_facts(pack[name])
        cells = []
        for c in CATEGORIES:
            truth = {_norm(c, v) for v in inv["facts"].get(c, [])}
            tp = len(got[c] & truth)
            totals[c][0] += tp
            totals[c][1] += len(got[c])
            totals[c][2] += len(truth)
            p = f"{tp / len(got[c]):.2f}" if got[c] else "  - "
            r = f"{tp / len(truth):.2f}" if truth else "  - "
            cells.append(f"{c.split('/')[0][:8]} {p}/{r}")
        lines.append(f"  {name[:34]:34s} " + "  ".join(cells))
    pooled = []
    for c, (tp, n_parser, n_truth) in totals.items():
        p = tp / n_parser if n_parser else float("nan")
        r = tp / n_truth if n_truth else float("nan")
        pooled.append(f"{c}: P={p:.3f} R={r:.3f} (parser {n_parser}, inventory {n_truth})")
    lines += ["  pooled:", *[f"    {x}" for x in pooled], ""]

    # 2. summaries vs inventory
    lines.append("== 2. Summary coverage against the independent inventory (deterministic, STRICT) ==")
    rows = [r for r in _summary_rows() if r["repo_name"] in inventories]
    for r in rows:
        r["cov"] = coverage_against(inventories[r["repo_name"]], r["summary_text"])
    groups = sorted({(r["source"], r["model"]) for r in rows})
    for source, model in groups:
        g = [r for r in rows if (r["source"], r["model"]) == (source, model) and r["cov"] is not None]
        # Repeated seeds in one DB are averaged per (repo, arm) before testing, so
        # each repository is one paired observation.
        cell: dict[tuple[str, str], list[float]] = {}
        for r in g:
            cell.setdefault((r["repo_name"], r["context_variant"]), []).append(r["cov"])
        by_arm = {a: {k[0]: st.mean(v) for k, v in cell.items() if k[1] == a} for a in ARM_ORDER}
        lines.append(f"  {source} / {model}")
        lines.append("    means: " + "  ".join(
            f"{a[:3]} {st.mean(v.values()):.3f} (n={len(v)})" for a, v in by_arm.items() if v))
        tests = []
        for a, b in CONTRASTS:
            common = sorted(set(by_arm[a]) & set(by_arm[b]))
            if len(common) < 2:
                continue
            xa, xb = [by_arm[a][k] for k in common], [by_arm[b][k] for k in common]
            p = wilcoxon(xa, xb).pvalue if any(x != y for x, y in zip(xa, xb)) else None
            tests.append((a, b, len(common), p, rank_biserial(xa, xb)))
        for (a, b, n, p, r), h in zip(tests, holm([t[3] for t in tests])):
            ps = "n/a" if p is None else f"{p:.4f}"
            hs = "n/a" if h is None else f"{h:.4f}"
            lines.append(f"    {a[:3]} vs {b[:3]}: n={n} p={ps} holm={hs} r={r:+.2f}")
    lines.append("")
    lines.append("Note: with n repositories the smallest attainable two-sided Wilcoxon p is 2/2^n "
                 "(n=6 -> 0.031), so read effect sizes alongside p.")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"wrote {OUT.relative_to(BACKEND)}")


if __name__ == "__main__":
    main()
