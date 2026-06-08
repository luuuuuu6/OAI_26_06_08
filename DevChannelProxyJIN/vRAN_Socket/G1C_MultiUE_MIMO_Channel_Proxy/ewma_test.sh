#!/usr/bin/env bash
#
# ewma_test.sh — Quick A/B test: Legacy vs 2D-EWMA
#
# Usage:  sudo bash ewma_test.sh
#
# Runs two sweeps at SNR=20:
#   1. SRS_ESTIMATOR=legacy   (baseline)
#   2. SRS_ESTIMATOR=2dmmse   (EWMA α=0.9)
#
# Then evaluates NMSE for both and prints comparison.
#

set -uo pipefail

if [ "$(id -u)" != "0" ]; then
  echo "ERROR: must run as root (sudo bash ewma_test.sh)"
  exit 2
fi

PROJ="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJ"

TS="$(date +%Y%m%d_%H%M)"
LOG_BASE="${PROJ}/../../logs/ewma_test_${TS}"
SNR="${EWMA_SNR:-20}"
MAX_FRAMES="${EWMA_MAX_FRAMES:-100}"
CHANNEL_SEED="${EWMA_CHANNEL_SEED:-42}"
ALPHA="${EWMA_ALPHA:-0.9}"
SIONNA_WAIT="${EWMA_SIONNA_WAIT:-30}"

mkdir -p "$LOG_BASE"

echo "============================================================"
echo " EWMA A/B Test   ($(date))"
echo "============================================================"
echo "  LOG_BASE     = $LOG_BASE"
echo "  SNR          = $SNR"
echo "  MAX_FRAMES   = $MAX_FRAMES"
echo "  CHANNEL_SEED = $CHANNEL_SEED"
echo "  ALPHA        = $ALPHA"
echo "============================================================"
echo

restart_sionna() {
  echo ">>> docker restart sionna-proxy"
  docker restart sionna-proxy >/dev/null 2>&1 || true
  echo ">>> wait ${SIONNA_WAIT}s"
  sleep "$SIONNA_WAIT"
}

run_one() {
  local algo="$1"
  local extra_env="$2"
  local sweep_root="$LOG_BASE/$algo"

  restart_sionna
  bash preflight.sh 2>/dev/null || true

  echo ">>> [$algo] sweep START"
  env CHANNEL_SEED="$CHANNEL_SEED" \
      SRS_ESTIMATOR="$algo" \
      $extra_env \
      ONLY_SNR="$SNR" \
      MAX_FRAMES="$MAX_FRAMES" \
      SWEEP_ROOT="$sweep_root" \
      bash run_q4_snr_sweep_v8.sh \
      > "$LOG_BASE/${algo}_sweep.log" 2>&1

  local snr_dir="$sweep_root/snr_${SNR}dB"
  local n_bins=$(ls "$snr_dir"/srs_matrix_*.bin 2>/dev/null | wc -l)
  echo ">>> [$algo] done: $n_bins SRS bins captured"
  if [ "$n_bins" -lt 1 ]; then
    echo ">>> [$algo] FAILED: 0 bins"
    return 1
  fi
  return 0
}

eval_one() {
  local algo="$1"
  local snr_dir="$LOG_BASE/$algo/snr_${SNR}dB"
  echo ">>> [$algo] eval"
  python3 eval_pdpR_nmse.py \
      --run-dir "$snr_dir" \
      --sto-correct per-frame \
      2>&1 | tee "$LOG_BASE/${algo}_nmse.txt"
}

# ── Run sweeps ──
echo "================ SWEEP 1: legacy ================"
if ! run_one "legacy" ""; then
  echo "ABORT: legacy sweep failed"
  exit 1
fi

echo
echo "================ SWEEP 2: 2dmmse (EWMA α=$ALPHA) ================"
if ! run_one "2dmmse" "SRS_2D_ALPHA=$ALPHA SRS_2D_DEBUG=1"; then
  echo "ABORT: 2dmmse sweep failed"
  exit 1
fi

# ── Evaluate ──
echo
echo "================ EVALUATION ================"
echo
echo "--- legacy ---"
eval_one "legacy"
echo
echo "--- 2dmmse (EWMA α=$ALPHA) ---"
eval_one "2dmmse"

# ── Summary ──
echo
echo "============================================================"
echo " COMPARISON SUMMARY"
echo "============================================================"
echo " Check $LOG_BASE/{legacy,2dmmse}_nmse.txt for p50 NMSE values"
echo " Expected: 2dmmse should show lower (better) NMSE than legacy"
echo "============================================================"
