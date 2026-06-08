#!/usr/bin/env bash
#
# d0_run.sh — one-shot D0 floor diagnosis runner.
#
# !!! Must be invoked as ROOT  (sudo bash d0_run.sh)  !!!
#
# Why root:
#   - sweep / launch_all / preflight all use `sudo docker exec` and
#     `sudo pkill`.  When invoked as a normal user, sudo's 5-min credit
#     timestamp expires mid-sweep and any subsequent sudo call HANGS
#     waiting for a password — silently breaking sionna IPC and producing
#     "No SRS signal" + 0 captured frames (observed 17:17 D0 attempt).
#   - Running the wrapper as root makes every nested sudo a no-op.
#
# Drives the full D0 protocol per ceshiliucheng.md (the proven template):
#   step 1  : restart sionna-proxy (clear IPC ringbuffer state)  +  preflight
#   step 2  : sweep SRS_ESTIMATOR=legacy   (single SNR=20)        — captures GT + Legacy SRS
#   step 3  : gen_oracle_gt_bin.py from legacy's GT
#   step 4  : restart sionna-proxy + preflight
#   step 5  : sweep SRS_ESTIMATOR=passthru
#   step 6  : restart sionna-proxy + preflight
#   step 7  : sweep SRS_ESTIMATOR=oracle
#   step 8  : eval_pdpR_nmse.py × 3 + decision matrix
#
# Each sweep is **idempotent + 0-bin gate**: if a previous sweep dir already
# has SRS bins, that step is skipped.  If a fresh sweep finishes with 0
# captured SRS bins, the wrapper aborts the whole D0 run early instead of
# wasting time on doomed downstream sweeps.
#
# Env override knobs (all optional):
#   D0_LOG_BASE          override LOG_BASE (default: $PROJ/../../logs/d0_<TS>)
#   D0_SNR               single SNR point (default 20)
#   D0_MAX_FRAMES        frames per sweep (default 100)
#   D0_MIN_SRS_BINS      min SRS bin files per sweep (default 1)
#   D0_HARD_CEILING_SEC  per-sweep wall-clock ceiling (default 900)
#   D0_CHANNEL_SEED      Sionna channel seed (default 42)
#   D0_SCALE             gen_oracle_gt_bin.py --scale arg (default auto)
#   D0_SIONNA_WAIT       seconds to wait after `docker restart sionna-proxy`
#                        before next sweep (default 30; bump if init slow)
#   D0_SKIP_RESTART      if non-empty, do not restart sionna-proxy
#                        (use only when you're sure prior state is clean)
#

set -uo pipefail

if [ "$(id -u)" != "0" ]; then
  echo "ERROR: d0_run.sh must be invoked as ROOT."
  echo "  Run:  sudo bash $(basename "$0")"
  exit 2
fi

PROJ="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJ"

TS="$(date +%Y%m%d_%H%M)"
LOG_BASE="${D0_LOG_BASE:-${PROJ}/../../logs/d0_${TS}}"
SNR="${D0_SNR:-20}"
MAX_FRAMES="${D0_MAX_FRAMES:-100}"
MIN_SRS_BINS="${D0_MIN_SRS_BINS:-1}"
HARD_CEILING_SEC="${D0_HARD_CEILING_SEC:-900}"
CHANNEL_SEED="${D0_CHANNEL_SEED:-42}"
SCALE="${D0_SCALE:-auto}"
SIONNA_WAIT="${D0_SIONNA_WAIT:-30}"
SKIP_RESTART="${D0_SKIP_RESTART:-}"

mkdir -p "$LOG_BASE"

echo "============================================================"
echo " D0 Floor Diagnosis Runner   (uid=$(id -u), pid=$$)"
echo "============================================================"
echo "  LOG_BASE          = $LOG_BASE"
echo "  SNR               = $SNR"
echo "  MAX_FRAMES        = $MAX_FRAMES   per sweep"
echo "  MIN_SRS_BINS      = $MIN_SRS_BINS"
echo "  HARD_CEILING_SEC  = $HARD_CEILING_SEC"
echo "  CHANNEL_SEED      = $CHANNEL_SEED"
echo "  SCALE             = $SCALE"
echo "  SIONNA_WAIT       = ${SIONNA_WAIT}s   (post docker restart)"
echo "  SKIP_RESTART      = ${SKIP_RESTART:-(false)}"
echo "============================================================"
echo

# ── helpers ────────────────────────────────────────────────────────────────
restart_sionna() {
  if [ -n "$SKIP_RESTART" ]; then
    echo ">>> docker restart sionna-proxy SKIPPED (D0_SKIP_RESTART set)"
    return 0
  fi
  echo ">>> docker restart sionna-proxy"
  docker restart sionna-proxy >/dev/null || {
    echo "  ERROR: docker restart failed"; return 1; }
  echo ">>> wait ${SIONNA_WAIT}s for sionna GPU init"
  sleep "$SIONNA_WAIT"
}

preflight() {
  echo ">>> preflight"
  bash preflight.sh
}

# Run a sweep with given SRS_ESTIMATOR, into LOG_BASE/<algo>/.
# Skips if SRS bins already present.  Returns 1 if a fresh sweep
# finishes with 0 captured SRS bins (cause downstream eval to fail).
run_sweep() {
  local algo="$1"
  local sweep_root="$LOG_BASE/$algo"
  local snr_dir="$sweep_root/snr_${SNR}dB"

  # idempotency check
  local existing_bins=$(ls "$snr_dir"/srs_matrix_*.bin 2>/dev/null | wc -l)
  if [ "$existing_bins" -ge 1 ]; then
    echo ">>> [$algo] sweep already has $existing_bins SRS bin(s) at $snr_dir (skipping)"
    return 0
  fi

  restart_sionna
  preflight
  echo ">>> [$algo] sweep START  -> $sweep_root"

  if [ "$algo" = "oracle" ]; then
    export SRS_ORACLE_GT_FILE="$LOG_BASE/oracle_gt.bin"
  else
    unset SRS_ORACLE_GT_FILE 2>/dev/null || true
  fi

  # NOTE: redirect to file instead of `| tee` because launch_all_v8 spawns
  # background processes (watcher / tail -f) that inherit stdout fd and
  # never close it.  `tee` would block forever waiting for stdin EOF
  # (observed deadlock 17:45 D0 attempt).  We tail in background instead
  # for live progress, then SIGTERM the tail when sweep returns.
  local sweep_log="$LOG_BASE/${algo}_sweep.log"
  : > "$sweep_log"
  ( tail -F "$sweep_log" 2>/dev/null & echo $! > "$LOG_BASE/.tail_pid" ) >&2

  env CHANNEL_SEED="$CHANNEL_SEED" \
      SRS_ESTIMATOR="$algo" \
      ONLY_SNR="$SNR" \
      MAX_FRAMES="$MAX_FRAMES" \
      MIN_SRS_BINS="$MIN_SRS_BINS" \
      HARD_CEILING_SEC="$HARD_CEILING_SEC" \
      SWEEP_ROOT="$sweep_root" \
      ${SRS_ORACLE_GT_FILE:+SRS_ORACLE_GT_FILE="$SRS_ORACLE_GT_FILE"} \
      bash run_q4_snr_sweep_v8.sh \
      > "$sweep_log" 2>&1

  # stop tail
  if [ -f "$LOG_BASE/.tail_pid" ]; then
    kill -TERM "$(cat "$LOG_BASE/.tail_pid")" 2>/dev/null
    rm -f "$LOG_BASE/.tail_pid"
  fi

  # sanity: did we actually capture any SRS?
  local n_bins=$(ls "$snr_dir"/srs_matrix_*.bin 2>/dev/null | wc -l)
  if [ "$n_bins" -lt 1 ]; then
    echo ">>> [$algo] FAILED: 0 SRS bins captured at $snr_dir"
    echo "    Likely cause: sionna IPC state broken; bumping D0_SIONNA_WAIT may help"
    return 1
  fi
  local n_gt=$(ls "$snr_dir"/sionna_gt/gt_batch_*.npz 2>/dev/null | wc -l)
  echo ">>> [$algo] OK: $n_bins SRS bin(s) + $n_gt GT npz(s) at $snr_dir"
}

run_eval() {
  local algo="$1"
  local snr_dir="$LOG_BASE/$algo/snr_${SNR}dB"
  local out="$LOG_BASE/${algo}_nmse.txt"
  if [ ! -d "$snr_dir" ]; then
    echo ">>> [$algo] eval SKIPPED — $snr_dir not found"; return 1
  fi
  echo ">>> [$algo] eval $snr_dir"
  python3 eval_pdpR_nmse.py \
      --run-dir "$snr_dir" \
      --sto-correct per-frame \
      2>&1 | tee "$out"
}

extract_p50() {
  local f="$1"
  [ -f "$f" ] || { echo "NA"; return; }
  python3 - "$f" <<'PY'
import re, sys
txt = open(sys.argv[1]).read()
m = re.search(r"NMSE LS-aligned\s*:\s*([-+0-9.]+)\s*dB.*?p50.*?\[\s*[-+0-9.]+\s*,\s*([-+0-9.]+)", txt, re.S)
if not m:
    m = re.search(r"NMSE raw.*?p50.*?\[\s*[-+0-9.]+\s*,\s*([-+0-9.]+)", txt, re.S)
    if not m:
        print("NA"); sys.exit(0)
    print(m.group(1))
else:
    print(m.group(2))
PY
}

# ── Step 1+2: legacy ──────────────────────────────────────────────────────
echo "================ STEP 1+2: legacy sweep ================"
if ! run_sweep legacy; then
  echo "ABORT at legacy step.  Diagnose sionna/OAI before re-running."
  exit 1
fi

# ── Step 3: build oracle bin ──────────────────────────────────────────────
echo "================ STEP 3: build oracle bin ================"
ORACLE_BIN="$LOG_BASE/oracle_gt.bin"
GT_DIR="$LOG_BASE/legacy/snr_${SNR}dB/sionna_gt"
if [ -s "$ORACLE_BIN" ]; then
  echo ">>> oracle bin already exists ($ORACLE_BIN), skipping"
else
  if [ ! -d "$GT_DIR" ]; then
    echo "ABORT: GT dir not found at $GT_DIR (legacy step did not produce GT)"
    exit 1
  fi
  echo ">>> building oracle bin from $GT_DIR"
  python3 gen_oracle_gt_bin.py \
      --gt-dir "$GT_DIR" \
      --out    "$ORACLE_BIN" \
      --scale  "$SCALE" \
      2>&1 | tee "$LOG_BASE/oracle_bin.log"
  if [ ! -s "$ORACLE_BIN" ]; then
    echo "ABORT: gen_oracle_gt_bin.py produced empty bin"; exit 1
  fi
fi

# ── Step 4+5: passthru ────────────────────────────────────────────────────
echo "================ STEP 4+5: passthru sweep ================"
if ! run_sweep passthru; then
  echo "passthru step failed; oracle step also likely to fail.  Aborting."
  exit 1
fi

# ── Step 6+7: oracle ──────────────────────────────────────────────────────
echo "================ STEP 6+7: oracle sweep ================"
if ! run_sweep oracle; then
  echo "oracle step failed.  legacy + passthru data still usable."
fi

# ── Step 8: eval ──────────────────────────────────────────────────────────
echo "================ STEP 8: NMSE evaluation ================"
for ALG in legacy passthru oracle; do
  echo
  echo "--- $ALG ---"
  run_eval "$ALG" || true
done

# ── Step 9: decision summary ──────────────────────────────────────────────
LP="$(extract_p50 "$LOG_BASE/legacy_nmse.txt")"
PP="$(extract_p50 "$LOG_BASE/passthru_nmse.txt")"
OP="$(extract_p50 "$LOG_BASE/oracle_nmse.txt")"

SUMMARY="$LOG_BASE/d0_summary.txt"
{
  echo "D0 Floor Diagnosis @ SNR=$SNR (seed=$CHANNEL_SEED, MAX_FRAMES=$MAX_FRAMES)"
  echo "------------------------------------------------------------"
  echo "  legacy   p50 = $LP dB"
  echo "  passthru p50 = $PP dB"
  echo "  oracle   p50 = $OP dB"
  echo
  python3 - <<PY
LP, PP, OP = "$LP", "$PP", "$OP"
def f(x):
    try: return float(x)
    except: return None
L, P, O = f(LP), f(PP), f(OP)

print("Section decomposition (NMSE p50 dB):")
if O is not None and P is not None:
    print(f"  A (LS noise)          = passthru - oracle  = {P-O:+.2f}")
if L is not None and P is not None:
    print(f"  B (filt contribution) = legacy - passthru  = {L-P:+.2f}")
if O is not None:
    print(f"  C (downstream floor)  = oracle             = {O:+.2f}")

print()
print("Decision (based on oracle p50):")
if O is None:
    print("  ?? oracle eval failed; cannot decide")
elif O <= -15:
    print(f"  GO LSL  -- oracle {O:+.2f} <= -15  : floor in estimator, full path open")
elif O <= -10:
    print(f"  GO with reduced target  -- oracle in (-15, -10]  : LSL ceiling ~ {O:+.2f}")
elif O <= -7:
    print(f"  MARGINAL -- oracle in (-10, -7]  : downstream dominates, talk to LIULU")
else:
    print(f"  STOP -- oracle {O:+.2f} > -7  : downstream is the floor, do not pursue LSL")
PY
} | tee "$SUMMARY"

echo
echo ">>> D0 done."
echo ">>> Summary       : $SUMMARY"
echo ">>> Per-algo NMSE : $LOG_BASE/{legacy,passthru,oracle}_nmse.txt"
echo ">>> Now fill out  : $PROJ/../../D0_RESULT_TEMPLATE.md  →  D0_RESULT_<date>.md"
