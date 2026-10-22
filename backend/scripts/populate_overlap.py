#!/usr/bin/env python3
import csv
import json
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _BACKEND_DIR)

import nltk
for pkg in ("wordnet", "punkt", "punkt_tab", "omw-1.4"):
    try:
        nltk.download(pkg, quiet=True)
    except Exception:
        pass

from app.schemas.llm_result import ContextVariant
from app.evaluation.text_overlap import (
    extract_overview,
    _bleu4,
    _rouge_l,
    _meteor,
)

REF_DIR = os.path.join(_BACKEND_DIR, "reference_summaries")
EVAL_DIR = os.path.join(_BACKEND_DIR, "evaluation_results")

def get_reference_overview(repo_name: str) -> str:
    path = os.path.join(REF_DIR, f"{repo_name}.json")
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
        return data.get("overview", "")

def process_file(csv_path: str):
    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        return

    print(f"Processing {csv_path}...")
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    updated = 0
    skipped = 0

    for r in rows:
        if r.get("run_status") != "success":
            continue
        summary_text = r.get("summary_text", "")
        if not summary_text:
            continue
        repo_name = r.get("repo_name", "")
        ref = get_reference_overview(repo_name)
        if not ref:
            skipped += 1
            continue

        gen_overview, _ = extract_overview(summary_text)

        # Compute lexical overlap metrics
        b4 = _bleu4(ref, gen_overview)
        rl = _rouge_l(ref, gen_overview)
        met = _meteor(ref, gen_overview)

        r["text_overlap_scored"] = "True"
        r["bleu4"] = f"{b4:.4f}"
        r["rouge_l"] = f"{rl:.4f}"
        r["meteor"] = f"{met:.4f}"
        updated += 1

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Done {csv_path}: {updated} rows scored with BLEU-4, ROUGE-L, and METEOR ({skipped} skipped).")

if __name__ == "__main__":
    # CSV names (in evaluation_results/) may be given as arguments, e.g. the revision
    # runs' battery_rev_*.csv; with none, the three published batteries as before.
    stems = sys.argv[1:] or ["battery_codellama13b.csv", "battery_qwen14b.csv", "battery_qwen32b.csv"]
    for stem in stems:
        target = os.path.join(EVAL_DIR, os.path.basename(stem))
        process_file(target)
