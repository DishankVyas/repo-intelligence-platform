# CICT-2026 revision runs — step by step

What this produces for the reviewers:

| Reviewer item | What the runs add |
|---|---|
| R2 RAG baseline at matched budget | a 4th arm, `rag_bm25`, sized to each repo's knowledge-graph token count |
| R4 CodeLlama anomaly | contexts budgeted in each model's own tokenizer, so no prompt overflows |
| R4 variance across runs | 3 sampled repeats per model (temperature 0.7, seeds 1–3) |

The published results (`battery_codellama13b.db` etc.) are never touched. Every new
output is named `battery_rev_<model>_s<seed>_t<temperature>.{db,csv}`.

Where each step runs:

- **LOCAL** = your laptop (or any machine with Docker and internet). No model runs here.
- **PORTAL** = the college GPU through Altair Access, as in `hpc/INSTRUCTIONS.md`.

---

## Step 1 — LOCAL: build the new context pack (about 10–15 minutes)

This parses the 18 repos and renders all four arms for each of the three models. It
needs Neo4j (via Docker) and internet. It downloads each model's *tokenizer file*
(a few MB), never the model itself.

Needs **Docker Desktop** running and **Python 3.12+** (the pinned `scipy` requires it).

1. One-time: create `backend/.env` from the example and set the Neo4j password to the
   one Docker Compose uses (`devpassword123` unless you changed it):

   ```
   copy backend\.env.example backend\.env      (PowerShell / cmd)
   ```
   then edit `backend/.env` so it has `NEO4J_PASSWORD=devpassword123` and
   `OLLAMA_NUM_CTX=8192`.

2. Start Neo4j and build the pack. **PowerShell** (the VS Code default on Windows):

   ```powershell
   docker compose up -d neo4j                    # from the repo root
   cd backend
   py -3.12 -m venv venv                         # plain `python` is 3.11 on this laptop
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python -m scripts.build_context_pack (Get-Content ..\18_repo_urls.txt) `
       --pin-commits ..\context_pack.json `
       --notes "CICT-2026 revision: per-model budgets + rag_bm25" `
       --out ..\context_pack_v2.json
   ```

   Git Bash / Linux / macOS: same command with `$(cat ../18_repo_urls.txt)` and `\`
   line continuations.

- `--pin-commits` checks every repo out at the exact commit the published study used.
  This matters: `ack-nestjs-boilerplate` has had new commits since September.
- The build **refuses to write a pack** if any prompt would overflow the 8,192-token
  window. If it stops with that message, report the listed repos rather than working
  around it.

## Step 2 — PORTAL: upload

Upload to `/data/mpstme-dishank/`:

- `context_pack_v2.json` (from Step 1)
- the updated repository (zip of the repo root, unzipped over the old copy), so the
  cluster runs the new harness and job scripts

## Step 3 — PORTAL: submit 12 battery jobs

**Applications → Batch or Shell Script**, Job Script `hpc/run_battery.sh`, same form
fields as before (8 processors, 1 GPU, 32000 MB, image `pytorch_pbs:25.12-py3`).
The only thing that changes between jobs is **Job Script Arguments**:

| # | Job Script Arguments | Purpose |
|---|---|---|
| 1 | `codellama:13b-instruct 42 0` | main result (temperature 0, as in the paper) |
| 2 | `qwen2.5-coder:14b 42 0` | main result |
| 3 | `qwen2.5-coder:32b 42 0` | main result |
| 4 | `codellama:13b-instruct 1 0.7` | variance, run 1 |
| 5 | `codellama:13b-instruct 2 0.7` | variance, run 2 |
| 6 | `codellama:13b-instruct 3 0.7` | variance, run 3 |
| 7 | `qwen2.5-coder:14b 1 0.7` | variance, run 1 |
| 8 | `qwen2.5-coder:14b 2 0.7` | variance, run 2 |
| 9 | `qwen2.5-coder:14b 3 0.7` | variance, run 3 |
| 10 | `qwen2.5-coder:32b 1 0.7` | variance, run 1 |
| 11 | `qwen2.5-coder:32b 2 0.7` | variance, run 2 |
| 12 | `qwen2.5-coder:32b 3 0.7` | variance, run 3 |

Submit jobs 1–3 first: they are the ones the paper's main tables need. Each job
generates 72 summaries (18 repos × 4 arms) and judges each twice with `gemma2:9b`. The
judge always runs at temperature 0, so only the summaries vary between repeats.

The job stops immediately with `FATAL` if the pack is the old v1 file or lacks contexts
for the model, so a wrong upload cannot silently produce a bad run.

## Step 4 — PORTAL: second judges on the new main runs (2 jobs, recommended)

Submit only after jobs 1–3 have finished: these re-score the three new main runs
(temperature 0) without regenerating anything. Same form fields as the battery jobs,
Job Script `hpc/run_rejudge.sh`:

| # | Job Script Arguments | Why |
|---|---|---|
| 13 | `gemma2:27b rev_codellama13b_s42_t0 rev_qwen14b_s42_t0 rev_qwen32b_s42_t0` | the paper already reports gemma2:27b as its second judge; without this, that table would describe the old summaries |
| 14 | `mistral:7b-instruct rev_codellama13b_s42_t0 rev_qwen14b_s42_t0 rev_qwen32b_s42_t0` | a judge from a different model family (reviewer R3) |

The deterministic matcher (Step 6) answers R3 without any model, so these strengthen the
result rather than being the only evidence. The variance runs (jobs 4–12) are not
re-judged: their purpose is the spread under the primary judge.

## Step 5 — LOCAL: download

From `/data/mpstme-dishank/evaluation_results/`, download every `battery_rev_*` and
`rejudge_*_battery_rev_*` file into `backend/evaluation_results/`.

## Step 6 — LOCAL: analyse (no GPU)

1. **BLEU-4 / ROUGE-L / METEOR** for the new main runs (the paper reports these), from
   `backend/` with the venv active:

   ```powershell
   python -m scripts.populate_overlap battery_rev_codellama13b_s42_t0.csv battery_rev_qwen14b_s42_t0.csv battery_rev_qwen32b_s42_t0.csv
   ```

2. **The revision analysis** (four arms, mean ± SD across the three sampled runs, all
   judges, the deterministic matcher): Claude extends `scripts/revision_analysis.py` to
   the downloaded `battery_rev_*` files once they exist.

3. **Fact inventory** (reviewer R1), once the annotations are written and marked
   verified: `python -m scripts.score_fact_inventory`. It picks up the new runs
   automatically.

## Summary — everything that has to run

| Where | What | Count |
|---|---|---|
| LOCAL | build `context_pack_v2.json` (Step 1) | once |
| PORTAL | battery jobs 1–3: main results, temperature 0 | 3 (required) |
| PORTAL | battery jobs 4–12: variance, temperature 0.7, seeds 1–3 | 9 (required) |
| PORTAL | rejudge jobs 13–14: gemma2:27b and mistral on jobs 1–3 | 2 (recommended) |
| LOCAL | text overlap, revision analysis, fact-inventory scoring (Step 6) | after download |

Nothing else needs re-running: the published September batteries stay as reported,
diagram accuracy is unchanged (the parse content is identical; see the parse
comparison in the revision notes), and the human validation stays tied to the summaries
it was done on. G-Eval rubric scores for the new runs are not set up; they would need
one more GPU job.

---

## Why the old pack cannot just be reused

- Its contexts were sized with one tokenizer (`cl100k`) for every model. CodeLlama's
  tokenizer needs about 33% more tokens for the same text, so 14 of its prompts
  overflowed and Ollama cut each one to about 4,098 tokens, dropping the task
  instruction. That is the "CodeLlama context anomaly" (see
  `backend/evaluation_results/REVISION_ANALYSIS.txt`).
- It has no `rag_bm25` arm.
