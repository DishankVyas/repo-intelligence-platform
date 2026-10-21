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

## Step 4 — PORTAL (optional): different-family judge on the new runs

The deterministic matcher (Step 6) already answers reviewer R3 without any model. To
also have a second, different-family LLM judge on the new main runs, submit
`hpc/run_rejudge.sh` with Job Script Arguments:

```
mistral:7b-instruct rev_codellama13b_s42_t0 rev_qwen14b_s42_t0 rev_qwen32b_s42_t0
```

## Step 5 — LOCAL: download

From `/data/mpstme-dishank/evaluation_results/`, download every `battery_rev_*` and
`rejudge_*_battery_rev_*` file into `backend/evaluation_results/`.

## Step 6 — LOCAL: analyse

The analysis of the new runs (four arms, mean ± SD across the three sampled runs, the
deterministic matcher, and the fact inventory once it exists) runs on the laptop from
the downloaded files. No GPU is needed.

---

## Why the old pack cannot just be reused

- Its contexts were sized with one tokenizer (`cl100k`) for every model. CodeLlama's
  tokenizer needs about 33% more tokens for the same text, so 14 of its prompts
  overflowed and Ollama cut each one to about 4,098 tokens, dropping the task
  instruction. That is the "CodeLlama context anomaly" (see
  `backend/evaluation_results/REVISION_ANALYSIS.txt`).
- It has no `rag_bm25` arm.
