#!/usr/bin/env bash
# Single-entrypoint runner for the whole remaining evaluation. Upload as the
# "Job Script" in Altair Access (Applications -> Batch or Shell Script).
#
# No custom saved image required -- submit with the standard pytorch_pbs image.
# The script bootstraps Ollama and Python deps from /data/<user>/ at start.
#
# Resources (same form fields as run_battery.sh / run_rejudge.sh):
#
#   Container Image               pytorch_pbs:25.12-py3   (standard image, NOT custom)
#   Number of Nodes               1
#   Number of Processors per Node 8
#   Number of GPUs per Node       1
#   Amount of Memory (MB)         32000
#   Queue                         workq
#
# Pass an optional phase in "Job Script Arguments":
#
#   revision        CICT-2026 revision runs (docs/REVISION_RUNS.md): the 3 main
#                   batteries (4 arms incl. rag_bm25, temperature 0), the secondary
#                   JUDGES on those, then 9 variance batteries (seeds 1-3, t=0.7).
#                   Needs the v2 pack at $PACK and, from the setup notebook,
#                   $DATA/gpu_env.sh. Does not need the September battery DBs.
#   (no argument)   everything, in order: judges -> human -> quality
#   judges          only the secondary judge(s) -- gemma2:27b by default; set
#                   JUDGES="gemma2:27b mistral:7b-instruct" to run both
#   human           only sample + full-claims rejudge + review sheet
#   quality         only G-Eval + text overlap (BLEU-4, ROUGE-L, METEOR;
#                   BERTScore is off unless NO_BERTSCORE=0)
#
# SAFE TO RE-RUN. Every step dedupes on (repo, model, variant) against its own
# output file, so if the walltime kills the job you resubmit the identical line
# and it resumes from where it stopped -- at most the row in flight is lost.
#
# A phase that fails is logged and the remaining phases still run; the script
# exits non-zero if any phase failed, and prints a summary table of outcomes.
#
# The battery jobs (run_battery.sh) MUST have completed and all three output DBs
# downloaded into $OUT_DIR before submitting this job.
#
# Deliberately NOT set -e: a failing phase must not abort the phases after it.
set -uo pipefail

usage() {
  cat <<'EOF'
usage: run_all.sh [all|judges|human|quality|revision]

  revision              CICT-2026 revision: 3 main batteries -> JUDGES on them
                        -> 9 variance batteries (resumable; see docs/REVISION_RUNS.md)
  (no argument) / all   run every phase in order: judges -> human -> quality
  judges                secondary judges only: gemma2:27b then mistral:7b-instruct
  human                 human-validation sample + full-claims rejudge + review sheet
  quality               G-Eval quality + text-overlap rejudge

environment overrides:
  DATA          persistent root                (default /data/$USER)
  PROJ          repo checkout                  (default $DATA/repo-intel-platform)
  OUT_DIR       results directory              (default $DATA/evaluation_results)
  PACK          context pack                   (default $DATA/context_pack.json)
  JUDGES        secondary judges, space-sep    (default "gemma2:27b"; use
                "gemma2:27b mistral:7b-instruct" for both)
  NO_BERTSCORE  1 = skip BERTScore in quality  (default 1 -- bert-score is not
                installed on the cluster and its model (distilbert-base-uncased)
                cannot be downloaded from a network-isolated compute node)
EOF
}

case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
esac

PHASE="${1:-all}"
case "$PHASE" in
  all|judges|human|quality|revision) ;;
  *)
    echo "FATAL: unknown phase '$PHASE'." >&2
    usage >&2
    exit 2 ;;
esac

USER_NAME="${USER:-$(id -un)}"
# GPU settings written by the setup notebook (section 3): driver library path, MIG
# slice, Ollama env. Loaded before the PATH/LD_LIBRARY_PATH exports below, which
# prepend to (not replace) what it sets, so every job's Ollama finds the GPU.
GPU_ENV="${GPU_ENV:-/data/${USER_NAME}/gpu_env.sh}"
if [[ -f "$GPU_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$GPU_ENV"
fi
# Exported, not just set: the judges phase shells out to run_rejudge.sh, which
# recomputes these from the environment. Without export, an override passed to
# run_all.sh would be silently ignored by that script and it would write its
# results somewhere else.
export DATA="${DATA:-/data/${USER_NAME}}"
export PROJ="${PROJ:-${DATA}/repo-intel-platform}"
export OUT_DIR="${OUT_DIR:-${DATA}/evaluation_results}"
export PACK="${PACK:-${DATA}/context_pack.json}"
export PATH="${DATA}/ollama-dist/bin:${DATA}/bin:/usr/local/bin:${PATH}"
export LD_LIBRARY_PATH="${DATA}/ollama-dist/lib/ollama:${LD_LIBRARY_PATH:-}"

# METEOR's wordnet corpus must not land in $HOME -- home is volatile here, so it
# would be re-downloaded every job and lost if the node has no route out.
export NLTK_DATA="${NLTK_DATA:-${DATA}/nltk_data}"
mkdir -p "$NLTK_DATA" 2>/dev/null || true

# The judge being validated against the human reviewer. Pinned explicitly rather
# than left to the script default -- the primary judge under test must be visible
# in the command that produced the numbers.
PRIMARY_JUDGE="gemma2:9b"
# Default is gemma2:27b alone. mistral:7b-instruct is a valid second judge but
# doubles the phase's runtime; add it with JUDGES="gemma2:27b mistral:7b-instruct".
# The revision runs need both: mistral is the different-family judge reviewer R3 asks
# for, and gemma2:27b is the second judge the paper already reports.
if [[ "$PHASE" == "revision" ]]; then
  read -r -a SECONDARY_JUDGES <<< "${JUDGES:-gemma2:27b mistral:7b-instruct}"
else
  read -r -a SECONDARY_JUDGES <<< "${JUDGES:-gemma2:27b}"
fi
DB_STEMS=(codellama13b qwen14b qwen32b)

# --- logging ------------------------------------------------------------------
# Nothing is written to $HOME: home directories on this cluster are volatile.
mkdir -p "$OUT_DIR" || { echo "FATAL: cannot create $OUT_DIR" >&2; exit 2; }
LOG="${OUT_DIR}/run_all.log"

_stamp() {
  while IFS= read -r line; do
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$line"
  done
}
exec > >(_stamp | tee -a "$LOG") 2>&1

echo $$ > "${OUT_DIR}/run_all.pid"

# --- single instance ----------------------------------------------------------
# Two concurrent runs judge the same rows twice and append both copies, so any
# average taken from the output is weighted by how often each row was re-judged.
# One earlier codellama arm ended up with 116 rows for 54 combos this way.
LOCK="${OUT_DIR}/run_all.lock"
OLLAMA_PID=""
cleanup() {
  [[ -n "${OLLAMA_PID:-}" ]] && kill "$OLLAMA_PID" 2>/dev/null
  [[ -f "$LOCK" && "$(cat "$LOCK" 2>/dev/null)" == "$$" ]] && rm -f "$LOCK"
  return 0
}
trap cleanup EXIT

if [[ -f "$LOCK" ]]; then
  OLD_PID="$(cat "$LOCK" 2>/dev/null || true)"
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "FATAL: run_all.sh is already running as pid $OLD_PID." >&2
    echo "Two instances would judge the same rows twice and append both copies." >&2
    echo "Watch the running one:  tail -f $LOG" >&2
    echo "Or stop it:             kill $OLD_PID   (then delete $LOCK)" >&2
    exit 6
  fi
  echo "WARNING: stale lock from pid ${OLD_PID:-unknown} (not running); taking over."
fi
echo $$ > "$LOCK"

echo "=== run_all.sh phase=$PHASE pid=$$ ==="
echo "=== configuration ==="
printf '  %-14s %s\n' \
  phase "$PHASE" proj "$PROJ" out "$OUT_DIR" pack "$PACK" \
  log "$LOG" no_bertscore "${NO_BERTSCORE:-1}"

# --- preflight: everything this job depends on must already be on disk --------
[[ -d "$PROJ/backend" ]] || { echo "FATAL: project not found at $PROJ" >&2; exit 2; }

[[ -f "$PACK" ]] || {
  echo "FATAL: context pack not found at $PACK." >&2
  echo "The compute nodes have no route to GitHub, so the parse phases read repos" >&2
  echo "from the pack instead of cloning. Copy context_pack.json to \$DATA first," >&2
  echo "or point PACK= at it." >&2
  exit 2
}

MISSING_DBS=()
# The revision phase produces its own batteries, so the September DBs are not needed.
[[ "$PHASE" == "revision" ]] && DB_STEMS_REQUIRED=() || DB_STEMS_REQUIRED=("${DB_STEMS[@]}")
for stem in ${DB_STEMS_REQUIRED[@]+"${DB_STEMS_REQUIRED[@]}"}; do
  [[ -f "$OUT_DIR/battery_${stem}.db" ]] || MISSING_DBS+=("battery_${stem}.db")
done
[[ ${#MISSING_DBS[@]} -eq 0 ]] || {
  echo "FATAL: missing battery output DB(s) in $OUT_DIR: ${MISSING_DBS[*]}" >&2
  echo "All three arms are required. Run the battery jobs (run_battery.sh) and" >&2
  echo "copy their results into $OUT_DIR first." >&2
  exit 2
}

# --- Python deps bootstrap ----------------------------------------------------
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
# One judge at a time, no generator co-resident.
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_KEEP_ALIVE=${OLLAMA_KEEP_ALIVE:-30m}

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
if [[ "$PHASE" == "revision" ]]; then
  [[ -f "$GPU_ENV" ]] && echo "GPU settings loaded from $GPU_ENV" \
    || echo "WARNING: $GPU_ENV not found -- run the setup notebook first, or Ollama may not find the GPU."
  python - "$PACK" <<'PY' || exit 2
import json, sys
pack = json.load(open(sys.argv[1], encoding="utf-8"))
if pack.get("version", 1) < 2:
    sys.exit("FATAL: %s is a v%s pack; the revision runs need the v2 pack (per-model contexts + rag_bm25)." % (sys.argv[1], pack.get("version", 1)))
print("context pack: v%s, %d repositories" % (pack["version"], len(pack["repositories"])))
PY
fi

# --- ollama -------------------------------------------------------------------
# The judges phase delegates to run_rejudge.sh, which starts and tears down its
# own server. So the human/quality phases call ensure_ollama, which reuses a live
# server if there is one and otherwise starts a fresh one. OLLAMA_PID is declared
# with the lock above and torn down by cleanup().

ensure_ollama() {
  if ollama list >/dev/null 2>&1; then
    echo "ollama already responding on 11434"
    return 0
  fi

  echo "=== starting ollama ==="
  # Kill any existing process on 11434 to prevent silent bind errors. A stale
  # binder is exactly what turned the last rejudge run into 658 empty rows.
  if command -v fuser >/dev/null 2>&1; then
    fuser -k 11434/tcp || true
  elif command -v ss >/dev/null 2>&1; then
    ss -ltnp | grep :11434 | awk '{print $6}' | cut -d, -f2 | cut -d= -f2 | xargs kill -9 2>/dev/null || true
  fi

  ollama serve > "${OUT_DIR}/ollama_run_all.log" 2>&1 &
  OLLAMA_PID=$!

  local i
  for i in $(seq 1 90); do
    ollama list >/dev/null 2>&1 && break
    if [[ $i -eq 90 ]]; then
      echo "FATAL: ollama not ready after 90s" >&2
      tail -30 "${OUT_DIR}/ollama_run_all.log" >&2
      return 3
    fi
    sleep 1
  done
  echo "ollama ready"; ollama list
  return 0
}

require_model() {
  local m="$1"
  if ollama list | awk 'NR>1 {print $1}' | grep -qx "$m"; then
    return 0
  fi
  echo "FATAL: model '$m' is not in $OLLAMA_MODELS." >&2
  echo "Stage it once (notebook 00_setup_gpu_image.ipynb, section 4) -- this node" >&2
  echo "may have no route out to the Ollama registry." >&2
  return 4
}

# --- phase bookkeeping --------------------------------------------------------
PHASE_NAMES=()
PHASE_RCS=()

run_phase() {
  local name="$1"; shift
  echo ""
  echo "############################################################"
  echo "### phase $name: START"
  echo "############################################################"
  "$@"
  local rc=$?
  if [[ $rc -eq 0 ]]; then
    echo "### phase $name: OK"
  else
    echo "### phase $name: FAILED (rc=$rc)"
  fi
  PHASE_NAMES+=("$name")
  PHASE_RCS+=("$rc")
  return 0
}

# --- phase 1: secondary judges ------------------------------------------------
# Strictly sequential, never parallel: gemma2:27b is ~17 GB and will not
# co-reside with a second judge on one MIG slice. A failing judge is logged and
# the other one still runs.
prune_parse_cache() {
  # A parse cache left half-written by a killed job (or by the old pre-context-pack
  # version that tried to git clone) makes the judge phase emit
  # error="no cached parse" for every row -- 658 of them, last time, all scored
  # null. Drop anything that is not valid JSON so the parse phase rebuilds it from
  # the context pack.
  local cache="$OUT_DIR/parse_cache"
  [[ -d "$cache" ]] || return 0
  python - "$cache" <<'PY'
import json, os, sys
cache = sys.argv[1]
bad = []
for name in sorted(os.listdir(cache)):
    if not name.endswith(".json"):
        continue
    path = os.path.join(cache, name)
    try:
        if os.path.getsize(path) == 0:
            raise ValueError("empty file")
        with open(path, encoding="utf-8") as fh:
            json.load(fh)
    except Exception as e:
        bad.append((name, str(e)))
        os.remove(path)
for name, err in bad:
    print(f"  pruned unusable parse cache entry {name}: {err}")
good = len([f for f in os.listdir(cache) if f.endswith('.json')])
print(f"  parse cache: {good} valid entr{'y' if good == 1 else 'ies'}, {len(bad)} pruned")
PY
}

verify_rejudge_output() {
  # The judge phase writes a row per summary even when it fails, so a "successful"
  # run can still be 100% useless. Check the CSVs it just produced.
  local judge="$1" slug rc=0 csv rows bad
  slug="$(echo "$judge" | tr ':.' '__' | tr '/' '_')"
  shopt -s nullglob
  local csvs=("$OUT_DIR"/rejudge_"${slug}"_*.csv)
  shopt -u nullglob
  if [[ ${#csvs[@]} -eq 0 ]]; then
    echo "WARNING: judge $judge produced no rejudge_${slug}_*.csv at all." >&2
    return 1
  fi
  for csv in "${csvs[@]}"; do
    rows=$(( $(wc -l < "$csv") - 1 ))
    bad=$(grep -c "no cached parse" "$csv" || true)
    printf '  %-52s %4s rows, %s with "no cached parse"\n' "$(basename "$csv")" "$rows" "$bad"
    if [[ "$rows" -le 0 ]]; then
      echo "WARNING: $(basename "$csv") has no data rows." >&2
      rc=1
    elif [[ "$bad" -gt 0 ]]; then
      echo "WARNING: $(basename "$csv") has $bad row(s) with no cached parse -- the" >&2
      echo "         parse phase did not populate $OUT_DIR/parse_cache from \$PACK." >&2
      rc=1
    fi
  done
  return $rc
}

phase_judges() {
  local rc_all=0 J rc
  echo "--- secondary judges to run: ${SECONDARY_JUDGES[*]} ---"
  prune_parse_cache
  for J in "${SECONDARY_JUDGES[@]}"; do
    echo "--- secondary judge: $J ---"
    bash "$PROJ/hpc/run_rejudge.sh" "$J"
    rc=$?
    if [[ $rc -ne 0 ]]; then
      echo "WARNING: secondary judge $J failed (rc=$rc). Continuing with the next judge." >&2
      rc_all=1
      continue
    fi
    echo "--- de-duplicating $J output ---"
    # Cheap insurance: if a previous run's rows were appended without the .db
    # resume engaging, the same combo appears more than once and every average
    # taken from the file is silently mis-weighted.
    python -m scripts.dedupe_rejudge --results-dir "$OUT_DIR" \
      || echo "WARNING: dedupe_rejudge failed; check for duplicate rows by hand." >&2
    echo "--- verifying $J output ---"
    verify_rejudge_output "$J" || rc_all=1
    echo "--- secondary judge $J done ---"
  done
  return $rc_all
}

# --- phase 2: human validation ------------------------------------------------
phase_human() {
  ensure_ollama || return $?
  require_model "$PRIMARY_JUDGE" || return $?

  local rc_all=0 stem rc
  local sample="evaluation_results/human_validation_sample_final.csv"
  local claims="evaluation_results/human_validation_claims.csv"

  if [[ ! -f "$sample" ]]; then
    echo "--- $sample absent: drawing the fixed-seed N=40 sample ---"
    python -m scripts.export_human_validation
    rc=$?
    if [[ $rc -ne 0 || ! -f "$sample" ]]; then
      echo "FATAL: could not create $sample (rc=$rc). Nothing downstream can run." >&2
      return ${rc:-1}
    fi
  else
    echo "--- reusing existing $sample ---"
  fi

  # All three arms append to the SAME claims CSV; each run skips rows already in
  # it, so this is resumable and must stay sequential.
  for stem in "${DB_STEMS[@]}"; do
    echo "--- full-claims rejudge: battery_${stem}.db (judge $PRIMARY_JUDGE) ---"
    python -m scripts.rejudge_full_claims \
      --src-db "evaluation_results/battery_${stem}.db" \
      --sample-csv "$sample" \
      --context-pack "$PACK" \
      --judge "$PRIMARY_JUDGE" \
      --out "$claims"
    rc=$?
    if [[ $rc -ne 0 ]]; then
      echo "WARNING: full-claims rejudge failed for battery_${stem}.db (rc=$rc)." >&2
      rc_all=1
    fi
  done

  if [[ ! -s "$claims" ]]; then
    echo "FATAL: $claims is missing or empty after all three arms -- not building a" >&2
    echo "review sheet from nothing. Check the per-arm counts above." >&2
    return 1
  fi
  echo "--- claims file: $(( $(wc -l < "$claims") - 1 )) summary rows ---"

  echo "--- building blinded review sheet ---"
  python -m scripts.build_review_sheet --context-pack "$PACK"
  rc=$?
  [[ $rc -ne 0 ]] && { echo "WARNING: build_review_sheet failed (rc=$rc)." >&2; rc_all=1; }

  return $rc_all
}

# --- phase 3: G-Eval quality + text overlap -----------------------------------
phase_quality() {
  ensure_ollama || return $?
  require_model "$PRIMARY_JUDGE" || return $?

  local flags=()
  if [[ "${NO_BERTSCORE:-1}" == "1" ]]; then
    # bert-score is excluded from requirements-cluster.txt and its model
    # (distilbert-base-uncased) cannot be fetched from a network-isolated node
    # unless the HuggingFace cache is pre-staged. BLEU-4, ROUGE-L and METEOR
    # still run; bertscore_f1 is left empty.
    flags+=(--no-bertscore)
    echo "--- BERTScore disabled (NO_BERTSCORE=1) ---"
  else
    echo "--- BERTScore ENABLED (NO_BERTSCORE=0) ---"
    # Fail loudly here rather than per-row: with bert-score missing, every row
    # would take the overlap `except` path and land with an empty bertscore_f1
    # while still burning its G-Eval call, and the CSV would look complete.
    if ! python -c "import bert_score" >/dev/null 2>&1; then
      echo "FATAL: NO_BERTSCORE=0 but the bert-score package is not importable." >&2
      echo "It is deliberately absent from requirements-cluster.txt. Install it into" >&2
      echo "the same --target as the other deps, from a node with a network route:" >&2
      echo "  pip install --target \"$PIP_TARGET\" --cache-dir \"$PIP_CACHE\" bert-score==0.3.13" >&2
      echo "Also pre-stage its model so the run does not need the network:" >&2
      echo "  HF_HOME=$DATA/hf-cache python -c \"from transformers import AutoModel, AutoTokenizer;\\" >&2
      echo "    AutoTokenizer.from_pretrained('distilbert-base-uncased');\\" >&2
      echo "    AutoModel.from_pretrained('distilbert-base-uncased')\"" >&2
      echo "then submit with HF_HOME=$DATA/hf-cache (add TRANSFORMERS_OFFLINE=1 to be sure)." >&2
      echo "Or leave NO_BERTSCORE=1 and report BLEU-4/ROUGE-L/METEOR only." >&2
      return 5
    fi
    echo "bert-score importable; HF_HOME=${HF_HOME:-<unset, will use the default cache>}"
  fi

  python -m scripts.rejudge_quality_and_overlap ${flags[@]+"${flags[@]}"}
  local rc=$?

  # This script's whole failure history is "exits 0, writes an empty CSV", so
  # count what it actually produced.
  local csv rows total=0
  shopt -s nullglob
  local csvs=("$OUT_DIR"/quality_and_overlap_*.csv)
  shopt -u nullglob
  if [[ ${#csvs[@]} -eq 0 ]]; then
    echo "WARNING: no quality_and_overlap_*.csv was produced." >&2
    return 1
  fi
  for csv in "${csvs[@]}"; do
    rows=$(( $(wc -l < "$csv") - 1 ))
    printf '  %-52s %4s rows\n' "$(basename "$csv")" "$rows"
    total=$(( total + rows ))
  done
  echo "  total G-Eval/overlap rows: $total"
  if [[ $total -le 0 ]]; then
    echo "WARNING: every quality CSV is header-only. G-Eval produced nothing." >&2
    return 1
  fi
  return $rc
}

# --- phase 4: CICT-2026 revision runs -----------------------------------------
REV_GENERATORS=(codellama:13b-instruct qwen2.5-coder:14b qwen2.5-coder:32b)
REV_ROWS=72   # 18 repositories x 4 arms per battery

rev_tag() {
  case "$1" in
    codellama:13b-instruct) echo codellama13b ;;
    qwen2.5-coder:14b)      echo qwen14b ;;
    qwen2.5-coder:32b)      echo qwen32b ;;
    *)                      echo "$1" | tr ':/' '__' ;;
  esac
}

free_ollama_port() {
  # Each battery/rejudge script starts its own server; a leftover one (the setup
  # notebook's, or a killed job's) would hold 11434 and the new one would not bind.
  pkill -f 'ollama serve' 2>/dev/null || true
  if command -v fuser >/dev/null 2>&1; then fuser -k 11434/tcp >/dev/null 2>&1 || true; fi
  sleep 3
}

gpu_check_log() {
  local log="$1"
  [[ -f "$log" ]] || return 0
  if grep -qiE 'no compatible gpu|library=cpu' "$log"; then
    echo "WARNING: $(basename "$log") shows Ollama did NOT use the GPU for this run." >&2
    return 1
  fi
  return 0
}

run_rev_battery() {
  local model="$1" seed="$2" temp="$3" tag db rows rc
  tag="rev_$(rev_tag "$model")_s${seed}_t${temp}"
  db="$OUT_DIR/battery_${tag}.db"
  # The harness writes its DB once, at the end, and appends. So a complete DB means
  # this battery is done (skip it: re-running would duplicate every row), and an
  # interrupted battery leaves no DB and simply runs again.
  if [[ -f "$db" ]]; then
    rows=$(python - "$db" <<'PY'
import sqlite3, sys
try:
    print(sqlite3.connect(sys.argv[1]).execute('select count(*) from evaluation_runs').fetchone()[0])
except Exception:
    print(0)
PY
)
    if [[ "$rows" -ge "$REV_ROWS" ]]; then
      echo "--- skip battery_${tag}: already complete ($rows rows) ---"
      return 0
    fi
    local aside="${db}.partial.$(date '+%Y%m%d%H%M%S')"
    echo "WARNING: battery_${tag}.db has only $rows of $REV_ROWS rows; moved aside to $aside" >&2
    mv "$db" "$aside"
  fi
  echo "--- battery $model seed=$seed temperature=$temp -> battery_${tag} ---"
  free_ollama_port
  bash "$PROJ/hpc/run_battery.sh" "$model" "$seed" "$temp"
  rc=$?
  gpu_check_log "$OUT_DIR/ollama_${tag}.log" || rc=1
  [[ $rc -ne 0 ]] && echo "WARNING: battery_${tag} failed (rc=$rc); continuing." >&2
  return $rc
}

fresh_parse_cache() {
  # rejudge.py reuses $OUT_DIR/parse_cache when present. A cache from the September
  # (v1) pack would score the new summaries against the old parse, so rebuild it from
  # the v2 pack once. The old cache is renamed, never deleted.
  local cache="$OUT_DIR/parse_cache" marker="$OUT_DIR/parse_cache/.from_v2_pack"
  [[ -f "$marker" ]] && return 0
  if [[ -d "$cache" ]] && [[ -n "$(ls -A "$cache" 2>/dev/null)" ]]; then
    local aside="${cache}.v1.$(date '+%Y%m%d%H%M%S')"
    mv "$cache" "$aside" && echo "moved the earlier parse cache aside to $aside"
  fi
  mkdir -p "$cache" && touch "$marker"
}

phase_revision() {
  local rc_all=0 m s J rc
  local main_stems=()
  for m in "${REV_GENERATORS[@]}"; do main_stems+=("rev_$(rev_tag "$m")_s42_t0"); done

  echo "=== revision 1/3: main batteries (temperature 0, 4 arms incl. rag_bm25) ==="
  for m in "${REV_GENERATORS[@]}"; do
    run_rev_battery "$m" 42 0 || rc_all=1
  done

  echo "=== revision 2/3: secondary judges on the new main runs: ${SECONDARY_JUDGES[*]} ==="
  fresh_parse_cache
  for J in "${SECONDARY_JUDGES[@]}"; do
    echo "--- secondary judge: $J ---"
    free_ollama_port
    bash "$PROJ/hpc/run_rejudge.sh" "$J" "${main_stems[@]}"
    rc=$?
    gpu_check_log "$OUT_DIR/ollama_rejudge_$(echo "$J" | tr ':.' '__' | tr '/' '_').log" || rc=1
    if [[ $rc -ne 0 ]]; then
      echo "WARNING: secondary judge $J failed (rc=$rc). Continuing." >&2
      rc_all=1
      continue
    fi
    python -m scripts.dedupe_rejudge --results-dir "$OUT_DIR" \
      || echo "WARNING: dedupe_rejudge failed; check for duplicate rows by hand." >&2
    verify_rejudge_output "$J" || rc_all=1
  done

  echo "=== revision 3/3: variance batteries (seeds 1-3, temperature 0.7) ==="
  for m in "${REV_GENERATORS[@]}"; do
    for s in 1 2 3; do
      run_rev_battery "$m" "$s" 0.7 || rc_all=1
    done
  done

  echo "=== revision outputs ==="
  ls -lh "$OUT_DIR"/battery_rev_* "$OUT_DIR"/rejudge_*_battery_rev_* 2>/dev/null
  return $rc_all
}

# --- run ----------------------------------------------------------------------
cd "$PROJ/backend" || { echo "FATAL: cannot cd to $PROJ/backend" >&2; exit 2; }

# rejudge_quality_and_overlap.py and rejudge_full_claims.py write to relative
# evaluation_results/ paths, so that name must resolve to the persistent $OUT_DIR.
# The repo checkout ships its own backend/evaluation_results/ (old committed
# results). Left in place it shadows $OUT_DIR, and every --src-db lookup fails on
# a directory that has none of the battery DBs -- so it is moved aside, not
# written into.
link_results() {
  local here="$PROJ/backend/evaluation_results"
  local resolved_here="" resolved_out=""
  resolved_out="$(cd "$OUT_DIR" 2>/dev/null && pwd -P)" || resolved_out=""

  if [[ -e "$here" || -L "$here" ]]; then
    resolved_here="$(cd "$here" 2>/dev/null && pwd -P)" || resolved_here=""
    if [[ -n "$resolved_here" && "$resolved_here" == "$resolved_out" ]]; then
      echo "evaluation_results already resolves to $OUT_DIR"
      return 0
    fi
    local aside="${here}.shipped.$(date '+%Y%m%d%H%M%S')"
    mv "$here" "$aside" || {
      echo "FATAL: $here shadows $OUT_DIR and could not be moved aside." >&2
      return 2
    }
    echo "WARNING: $here shadowed $OUT_DIR (the battery DBs live in $OUT_DIR)."
    echo "WARNING: moved it aside to $aside -- nothing was deleted."
  fi

  ln -s "$OUT_DIR" "$here" || {
    echo "FATAL: could not symlink $here -> $OUT_DIR" >&2
    return 2
  }
  echo "symlinked $here -> $OUT_DIR"
  return 0
}

link_results || exit 2

case "$PHASE" in
  all)
    run_phase judges  phase_judges
    run_phase human   phase_human
    run_phase quality phase_quality
    ;;
  judges)  run_phase judges  phase_judges  ;;
  human)   run_phase human   phase_human   ;;
  quality) run_phase quality phase_quality ;;
  revision) run_phase revision phase_revision ;;
esac

# --- summary ------------------------------------------------------------------
echo ""
echo "############################################################"
echo "### phase summary"
echo "############################################################"
printf '  %-10s %-8s %s\n' PHASE RC RESULT
FAILED=0
for i in "${!PHASE_NAMES[@]}"; do
  rc="${PHASE_RCS[$i]}"
  if [[ "$rc" -eq 0 ]]; then
    printf '  %-10s %-8s %s\n' "${PHASE_NAMES[$i]}" "$rc" OK
  else
    printf '  %-10s %-8s %s\n' "${PHASE_NAMES[$i]}" "$rc" FAILED
    FAILED=1
  fi
done

echo ""
echo "=== files in $OUT_DIR ==="
ls -lh "$OUT_DIR" 2>/dev/null | tail -n +2

echo ""
echo "=== next step (run locally, off-cluster) ==="
echo "  1. Download $OUT_DIR/human_review.xlsx"
echo "  2. A reviewer fills the 'human_verdict' column on the 'Review' worksheet."
echo "     Model and context variant are hidden in the 'Answer Key' worksheet --"
echo "     leave it hidden, the sheet is blinded on purpose."
echo "  3. Then, from backend/:"
echo ""
echo "       python -m scripts.score_human_validation"
echo ""
echo "  Also download rejudge_*.{csv,db} and quality_and_overlap_*.csv, then run"
echo "  build_paper_report.py locally."

if [[ $FAILED -ne 0 ]]; then
  echo "=== run_all.sh finished WITH FAILURES (see summary above) ==="
  exit 1
fi
echo "=== run_all.sh complete: all phases OK ==="
exit 0
