#!/usr/bin/env bash
# One generator's battery, on one MIG slice. Upload as the "Job Script" in Altair
# Access (Applications -> Batch or Shell Script).
#
# No custom saved image required -- submit with the standard pytorch_pbs image.
# The script bootstraps Ollama and Python deps from /data/<user>/ at start.
#
# Resources come from the submission form, not from here. Set them to:
#
#   Container Image               pytorch_pbs:25.12-py3   (standard image, NOT custom)
#   Number of Nodes               1
#   Number of Processors per Node 8          <- the form defaults to 1
#   Number of GPUs per Node       1
#   Amount of Memory (MB)         32000      <- the form defaults to 10, which is 10 MB
#   Queue                         workq
#
# Final generator roster (pass one of these in "Job Script Arguments"):
#   codellama:13b-instruct   -- cross-org comparison anchor (~8 GB Q4_K_M)
#   qwen2.5-coder:14b        -- scale-matched vs codellama:13b (~9 GB Q4_K_M)
#   qwen2.5-coder:32b        -- two-point within-family check (~20 GB Q4_K_M)
#
# CICT-2026 revision: pass a seed as the second argument to repeat a battery (reviewer
# R4 asks for variance across runs), e.g. "qwen2.5-coder:14b 2"; an optional third
# argument sets the generator temperature (default 0). Outputs are written to
# battery_rev_<model>_s<seed>_t<temp>.{csv,db} and never touch the published battery_<model>.db
# files. The pack must be a v2 pack (per-model contexts + rag_bm25) -- see
# docs/REVISION_RUNS.md. The judge always runs at temperature 0.
#
# Submit this three times, once per generator, rather than looping over models here.
# Three jobs run on three of the four MIG slices at once and finish in roughly the time
# of one; a loop would take three times as long on a single slice. It also means one
# model crashing does not take the other two down with it.
set -euo pipefail

MODEL="${1:-${MODEL:-}}"
: "${MODEL:?usage: run_battery.sh <generator-model> [seed] [temperature]   e.g. qwen2.5-coder:14b 1 0}"
SEED="${2:-${SEED:-42}}"
GEN_TEMPERATURE="${3:-${GEN_TEMPERATURE:-0}}"
JUDGE_MODEL="${JUDGE_MODEL:-gemma2:9b}"

USER_NAME="${USER:-$(id -un)}"
DATA="${DATA:-/data/${USER_NAME}}"
PROJ="${PROJ:-${DATA}/repo-intel-platform}"
PACK="${PACK:-${DATA}/context_pack_v2.json}"
OUT_DIR="${OUT_DIR:-${DATA}/evaluation_results}"
# Ollama may be in /usr/local/bin (root install) or in one of two non-root locations:
# - $DATA/bin/ollama        (legacy single-binary install)
# - $DATA/ollama-dist/bin/  (current tarball install, includes GPU libs)
export PATH="${DATA}/ollama-dist/bin:${DATA}/bin:/usr/local/bin:${PATH}"
export LD_LIBRARY_PATH="${DATA}/ollama-dist/lib/ollama:${LD_LIBRARY_PATH:-}"
# Map canonical model names to clean output-file slugs so battery outputs land in
# battery_codellama13b.{csv,db}, battery_qwen14b.{csv,db}, battery_qwen32b.{csv,db}.
# Unknown model names fall back to the tr-escaped form for forward compatibility.
case "$MODEL" in
  codellama:13b-instruct) RUN_TAG="codellama13b" ;;
  qwen2.5-coder:14b)      RUN_TAG="qwen14b" ;;
  qwen2.5-coder:32b)      RUN_TAG="qwen32b" ;;
  *)                      RUN_TAG="$(echo "$MODEL" | tr ':/' '__')" ;;
esac
RUN_TAG="rev_${RUN_TAG}_s${SEED}_t${GEN_TEMPERATURE}"

# --- Python deps bootstrap ---------------------------------------------------------------
# The pytorch_pbs base image does not have the project's Python deps pre-installed.
# Install them to a persistent directory on /data/ so only the first job ever downloads
# from PyPI; every subsequent job finds them already on disk and installs in ~10 seconds.
PIP_TARGET="${DATA}/site-packages"
PIP_CACHE="${DATA}/pip-cache"
REQ="${PROJ}/backend/requirements-cluster.txt"
mkdir -p "$PIP_TARGET" "$PIP_CACHE"
echo "=== installing Python deps to $PIP_TARGET ==="
pip install --quiet --target "$PIP_TARGET" --cache-dir "$PIP_CACHE" -r "$REQ" \
  >> /tmp/pip_install.log 2>&1 \
  || { echo "FATAL: pip install failed. See /tmp/pip_install.log" >&2; exit 5; }
export PYTHONPATH="${PIP_TARGET}:${PYTHONPATH:-}"
echo "Python deps ready."

export OLLAMA_MODELS="${OLLAMA_MODELS:-${DATA}/ollama}"
export OLLAMA_HOST="http://127.0.0.1:11434"
export OLLAMA_NUM_CTX="${OLLAMA_NUM_CTX:-8192}"   # pinned: changing it invalidates comparability
# Keep the generator AND the judge resident at once. This is the single biggest
# speedup available here and it is purely a scheduling change -- same weights, same
# num_ctx, same deterministic decoding, so outputs are unaffected.
#
# On the 16GB laptop this had to be 1: every generation->judge transition evicted one
# model and loaded the other, moving ~15GB through a 9.7GB budget for EVERY row. The
# battery makes two judge calls per generation, so that thrash dominated the run.
# A 40GB MIG slice holds all three generators alongside the judge comfortably:
#   qwen2.5-coder:32b (~20 GB) + gemma2:9b (~6 GB) = ~26 GB  <-- worst case, still <40 GB
#   codellama:13b-instruct (~8 GB) + gemma2:9b (~6 GB) = ~14 GB
export OLLAMA_MAX_LOADED_MODELS=2
# Left at 1 deliberately. Raising it batches concurrent requests, which changes the
# order of floating-point reductions and so can change generated text. Throughput is
# already won by running the three generators as three parallel jobs; buying more of it
# at the cost of reproducibility is a bad trade for a study.
export OLLAMA_NUM_PARALLEL=1
# Do not let a model be evicted between rows just because it went briefly idle.
export OLLAMA_KEEP_ALIVE=${OLLAMA_KEEP_ALIVE:-30m}

echo "=== configuration ==="
printf '  %-14s %s\n' generator "$MODEL" judge "$JUDGE_MODEL" seed "$SEED" temperature "$GEN_TEMPERATURE" \
  pack "$PACK" out "$OUT_DIR" models "$OLLAMA_MODELS"

# --- refuse the two mistakes that produce plausible-looking wrong numbers -------------
if [[ "$MODEL" == "$JUDGE_MODEL" ]]; then
  echo "FATAL: generator and judge are both '$MODEL'." >&2
  echo "An arm grading its own output inverts the result this study reports (REPORT.md 6.2)." >&2
  exit 2
fi
[[ -f "$PACK" ]] || { echo "FATAL: no context pack at $PACK. Build it off-cluster first:" >&2
                      echo "  python -m scripts.build_context_pack \$(cat 18_repo_urls.txt) --out context_pack.json" >&2
                      exit 2; }
[[ -d "$PROJ/backend" ]] || { echo "FATAL: project not found at $PROJ" >&2; exit 2; }
mkdir -p "$OUT_DIR"
# A v1 pack has one cl100k-budgeted context set for every model -- the inputs that
# overflowed CodeLlama's window. Refuse it rather than reproduce that failure.
python3 - "$PACK" "$MODEL" <<'PY' || exit 2
import json, sys
pack = json.load(open(sys.argv[1], encoding="utf-8"))
if pack.get("version", 1) < 2:
    sys.exit("FATAL: %s is a v%s pack; rebuild it as v2 (docs/REVISION_RUNS.md)." % (sys.argv[1], pack.get("version", 1)))
missing = [r["parsed"]["metadata"]["name"] for r in pack["repositories"] if sys.argv[2] not in r.get("model_contexts", {})]
if missing:
    sys.exit("FATAL: pack has no contexts for %s (e.g. %s); rebuild with --models including it." % (sys.argv[2], missing[0]))
PY

# --- prove we are on a GPU before spending the allocation ------------------------------
echo "=== GPU ==="
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
  nvidia-smi -L
else
  echo "FATAL: no nvidia-smi. This job is not on a GPU; every latency figure would be" >&2
  echo "meaningless and generation would take days. Check 'Number of GPUs per Node'." >&2
  exit 3
fi

# --- ollama ----------------------------------------------------------------------------
echo "=== starting ollama ==="
ollama serve > "$OUT_DIR/ollama_${RUN_TAG}.log" 2>&1 &
OLLAMA_PID=$!
trap 'kill "$OLLAMA_PID" 2>/dev/null || true' EXIT

for i in $(seq 1 90); do
  ollama list >/dev/null 2>&1 && break
  [[ $i -eq 90 ]] && { echo "FATAL: ollama not ready after 90s" >&2; tail -30 "$OUT_DIR/ollama_${RUN_TAG}.log" >&2; exit 3; }
  sleep 1
done
echo "ollama ready"; ollama list

for m in "$MODEL" "$JUDGE_MODEL"; do
  ollama list | awk 'NR>1 {print $1}' | grep -qx "$m" || {
    echo "FATAL: model '$m' is not in $OLLAMA_MODELS." >&2
    echo "Stage it once (notebook 00_setup_gpu_image.ipynb, section 4) -- this node may have no route out." >&2
    exit 4; }
done

# --- run --------------------------------------------------------------------------------
cd "$PROJ/backend"
ARGS=(--context-pack "$PACK"
      --models "$MODEL"
      --judge-model "$JUDGE_MODEL"
      --seed "$SEED"
      --temperature "$GEN_TEMPERATURE"
      --no-bertscore
      --out "$OUT_DIR/battery_${RUN_TAG}.csv"
      --sqlite "$OUT_DIR/battery_${RUN_TAG}.db")
# Reference summaries are deliberately not passed: they feed only BLEU/ROUGE/METEOR,
# measured at ~0.015 correlation with no discriminative power (REPORT.md 5.5.4).
# --no-bertscore above additionally avoids a model download on first use.
[[ -d "$PROJ/backend/annotations" ]] && ARGS+=(--annotations-dir "$PROJ/backend/annotations")

echo "=== running: $MODEL judged by $JUDGE_MODEL ==="
# `time` so the log records wall clock beside the per-row latencies: the gap between
# them is scheduler and model-load overhead, which must not be reported as generation time.
time python -m app.evaluation.harness "${ARGS[@]}"

echo "=== done -> $OUT_DIR/battery_${RUN_TAG}.{csv,db} ==="
echo "Score locally: the oracle is pure CPU and needs no GPU."
