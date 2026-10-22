#!/usr/bin/env bash
# Secondary-judge rejudging pass. Upload as the "Job Script" in Altair Access
# (Applications -> Batch or Shell Script).
#
# No custom saved image required -- submit with the standard pytorch_pbs image.
# The script bootstraps Ollama and Python deps from /data/<user>/ at start.
#
# Resources (same form fields as run_battery.sh):
#
#   Container Image               pytorch_pbs:25.12-py3   (standard image, NOT custom)
#   Number of Nodes               1
#   Number of Processors per Node 8
#   Number of GPUs per Node       1
#   Amount of Memory (MB)         32000
#   Queue                         workq
#
# Pass the secondary judge in "Job Script Arguments":
#   mistral:7b-instruct    (~5 GB Q4_K_M)
#   gemma2:27b             (~17 GB Q4_K_M — fits alone in the 40 GB slice)
#
# Submit this TWICE — once per secondary judge. Each job re-scores all three
# new battery arms (codellama13b, qwen14b, qwen32b) in sequence with one judge.
#
# The battery jobs (run_battery.sh) MUST have completed and their output DBs
# downloaded into $OUT_DIR before submitting this job.
set -euo pipefail

JUDGE="${1:-${JUDGE:-}}"
# Optional further arguments name the batteries to re-judge (default: the published
# three), e.g. "mistral:7b-instruct rev_codellama13b_s42_t0 rev_qwen14b_s42_t0 rev_qwen32b_s42_t0".
if [[ $# -gt 1 ]]; then BATTERY_STEMS="${*:2}"; fi
: "${JUDGE:?usage: run_rejudge.sh <secondary-judge>   e.g. mistral:7b-instruct}"

# Only these two secondary judges are valid. Using a different model invalidates
# the inter-judge agreement comparison reported in the paper.
case "$JUDGE" in
  mistral:7b-instruct|gemma2:27b) ;;
  *)
    echo "FATAL: '$JUDGE' is not a recognised secondary judge." >&2
    echo "Valid choices: mistral:7b-instruct   gemma2:27b" >&2
    exit 2 ;;
esac

USER_NAME="${USER:-$(id -un)}"
DATA="${DATA:-/data/${USER_NAME}}"
PROJ="${PROJ:-${DATA}/repo-intel-platform}"
OUT_DIR="${OUT_DIR:-${DATA}/evaluation_results}"
# The v2 pack when present (revision runs), else the original.
if [[ -z "${PACK:-}" ]]; then
  PACK="${DATA}/context_pack_v2.json"; [[ -f "$PACK" ]] || PACK="${DATA}/context_pack.json"
fi
JUDGE_SLUG="$(echo "$JUDGE" | tr ':.' '__' | tr '/' '_')"
export PATH="${DATA}/ollama-dist/bin:${DATA}/bin:/usr/local/bin:${PATH}"
export LD_LIBRARY_PATH="${DATA}/ollama-dist/lib/ollama:${LD_LIBRARY_PATH:-}"

# --- Python deps bootstrap ---------------------------------------------------------------
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
export OLLAMA_NUM_CTX="${OLLAMA_NUM_CTX:-8192}"   # pinned invariant — changing invalidates comparability
# One secondary judge, no generator co-resident. OLLAMA_MAX_LOADED_MODELS=1
# prevents accidental co-loading and keeps VRAM budget unambiguous.
export OLLAMA_MAX_LOADED_MODELS=1
# Left at 1 deliberately — same reasoning as run_battery.sh.
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_KEEP_ALIVE=${OLLAMA_KEEP_ALIVE:-30m}

echo "=== configuration ==="
printf '  %-14s %s\n' judge "$JUDGE" out "$OUT_DIR" models "$OLLAMA_MODELS" pack "$PACK"

# --- refuse the mistake that produces plausible-looking wrong numbers ----------
# The secondary judge must not be gemma2:9b (the primary judge). Running it here
# would re-measure the primary judge's agreement with itself, which is trivially 1.0
# and tells us nothing about inter-judge reliability.
if [[ "$JUDGE" == "gemma2:9b" ]]; then
  echo "FATAL: gemma2:9b is the PRIMARY judge and must not be used as a secondary judge." >&2
  echo "Inter-judge agreement measured against itself is trivially 1.0 and meaningless." >&2
  exit 2
fi

# --- source DB check ----------------------------------------------------------
# All three new battery DBs must be present. A missing DB is a warning (we skip
# it), but if NONE are found the job has nothing to do and should fail loudly.
# BATTERY_STEMS overrides which batteries are re-judged, e.g. for the CICT-2026 revision
# runs: BATTERY_STEMS="rev_codellama13b_s42_t0 rev_qwen14b_s42_t0 rev_qwen32b_s42_t0".
SRC_DBS=()
for stem in ${BATTERY_STEMS:-codellama13b qwen14b qwen32b}; do
  p="$OUT_DIR/battery_${stem}.db"
  if [[ -f "$p" ]]; then
    SRC_DBS+=("$p")
  else
    echo "WARNING: $p not found -- this arm will be skipped (run its battery job first)." >&2
  fi
done
[[ ${#SRC_DBS[@]} -eq 0 ]] && {
  echo "FATAL: no battery output DBs found in $OUT_DIR." >&2
  echo "Run the three battery jobs (run_battery.sh) and copy results here first." >&2
  exit 2
}

[[ -d "$PROJ/backend" ]] || { echo "FATAL: project not found at $PROJ" >&2; exit 2; }
mkdir -p "$OUT_DIR"

# --- prove we are on a GPU before spending the allocation ---------------------
echo "=== GPU ==="
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
  nvidia-smi -L
else
  echo "FATAL: no nvidia-smi. This job is not on a GPU." >&2
  echo "gemma2:27b needs ~17 GB VRAM and will not finish on CPU." >&2
  echo "Check 'Number of GPUs per Node' in the submission form." >&2
  exit 3
fi

# --- ollama -------------------------------------------------------------------
echo "=== starting ollama ==="
# Kill any existing process on 11434 to prevent silent bind errors
if command -v fuser >/dev/null 2>&1; then
  fuser -k 11434/tcp || true
elif command -v ss >/dev/null 2>&1; then
  ss -ltnp | grep :11434 | awk '{print $6}' | cut -d, -f2 | cut -d= -f2 | xargs kill -9 2>/dev/null || true
fi

ollama serve > "$OUT_DIR/ollama_rejudge_${JUDGE_SLUG}.log" 2>&1 &
OLLAMA_PID=$!
trap 'kill "$OLLAMA_PID" 2>/dev/null || true' EXIT

for i in $(seq 1 90); do
  ollama list >/dev/null 2>&1 && break
  [[ $i -eq 90 ]] && {
    echo "FATAL: ollama not ready after 90s" >&2
    tail -30 "$OUT_DIR/ollama_rejudge_${JUDGE_SLUG}.log" >&2
    exit 3
  }
  sleep 1
done
echo "ollama ready"; ollama list

# --- model staged check (same FATAL pattern as run_battery.sh) ----------------
ollama list | awk 'NR>1 {print $1}' | grep -qx "$JUDGE" || {
  echo "FATAL: model '$JUDGE' is not in $OLLAMA_MODELS." >&2
  echo "Stage it once (notebook 00_setup_gpu_image.ipynb, section 4) -- this node" >&2
  echo "may have no route out to the Ollama registry." >&2
  exit 4
}

# --- run ----------------------------------------------------------------------
cd "$PROJ/backend"

# Parse phase: load parsed repos from the context pack and cache results to disk,
# falling back to live clones only for repos missing from the pack. One parse run
# populates the cache for all three judge-phase calls that follow.
echo "=== parse phase (build/verify parse cache from ${SRC_DBS[0]}) ==="
time python -m scripts.rejudge \
  --judge "$JUDGE" \
  --src-db "${SRC_DBS[0]}" \
  --context-pack "$PACK" \
  --phase parse

# Judge phase: score each battery arm's stored summaries with $JUDGE.
# Outputs land in rejudge_<judge_slug>_<battery_stem>.{db,csv}.
for src in "${SRC_DBS[@]}"; do
  stem="$(basename "$src" .db)"
  echo "=== judge phase: $JUDGE scoring $stem ==="
  time python -m scripts.rejudge \
    --judge "$JUDGE" \
    --src-db "$src" \
    --phase judge
  echo "=== done -> $OUT_DIR/rejudge_${JUDGE_SLUG}_${stem}.{csv,db} ==="
done

echo "=== rejudge complete for judge=$JUDGE ==="
echo "Download all rejudge_*.{csv,db} files, then run build_paper_report.py locally."
