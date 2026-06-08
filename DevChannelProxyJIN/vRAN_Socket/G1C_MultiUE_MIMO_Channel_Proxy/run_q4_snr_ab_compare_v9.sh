#!/bin/bash
#
# Q4 SNR Sweep AB Comparator (v9)
# ===============================
# Runs the full v8 sweep pipeline twice (or custom METHODS list),
# switching only OAI SRS 2D method:
#   - ewma
#   - adaptive
#
# It wraps run_q4_snr_sweep_v8.sh in capture-only mode:
#   launch_all_v8.sh loop -> data capture
# Offline post-processing is decoupled via run_q4_postprocess_v8.sh.
#
# Usage:
#   sudo bash run_q4_snr_ab_compare_v8.sh
#
# Key env vars:
#   METHODS            Space-separated list (default: "ewma adaptive")
#   SWEEP_ROOT_BASE    Parent dir for AB outputs
#                      (default: logs/q4_ab_compare_YYYYMMDD_HHMMSS)
#   STOP_ON_FAIL       1=stop immediately, 0=continue next method (default: 1)
#   AUTO_POSTPROCESS_BG 1=enqueue per-method postprocess in background (default: 0)
#   SKIP_PRESWEEP_PREFLIGHT 1=skip pre-sweep preflight (default: 0)
#   SKIP_PREFLIGHT          1=skip per-point preflight in launch_all (default: 0)
#
# Forwarded env vars:
#   SNR_POINTS, ONLY_SNR, MAX_FRAMES, MIN_SRS_BINS, MIN_SRS_FRAMES, HARD_CEILING_SEC, ...
#   (all variables accepted by run_q4_snr_sweep_v8.sh are honored)
#
# CDL mode (bypass P1B, use CDL ray data):
#   P1B_NPZ=""  NPY_DIR=data_out/cdl_c_30kmh  UE_SPEED=0.83  (3 km/h)
#   P1B_NPZ=""  NPY_DIR=data_out/cdl_c_30kmh  UE_SPEED=8.33  (30 km/h)
#
# Adaptive coefficient env (optional override):
#   SRS_2D_ADAPT_A1, SRS_2D_ADAPT_A2, SRS_2D_ADAPT_A3,
#   SRS_2D_ADAPT_CMODEL, SRS_2D_ADAPT_ALPHA_INIT, SRS_2D_ADAPT_ALPHA_MIN,
#   SRS_2D_ADAPT_ALPHA_MAX, SRS_2D_ADAPT_RR_EMA, SRS_2D_ADAPT_SNR_EMA

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ_DIR="/home/dclcom61/OAI_luuuuuu/DevChannelProxyJIN"
RUNNER="${SCRIPT_DIR}/run_q4_snr_sweep_v9.sh"
POSTPROC_SCRIPT="${SCRIPT_DIR}/run_q4_postprocess_v8.sh"

METHODS="${METHODS:-ewma ibvss kalman}"
STOP_ON_FAIL="${STOP_ON_FAIL:-1}"
AUTO_POSTPROCESS_BG="${AUTO_POSTPROCESS_BG:-0}"

# Same-channel seed: critical for fair A/B comparison
export CHANNEL_SEED="${CHANNEL_SEED:-42}"
export MIN_SRS_FRAMES="${MIN_SRS_FRAMES:-100}"

POSTPROCESS_SPEED="${POSTPROCESS_SPEED:-${UE_SPEED:-0}}"
POSTPROCESS_SEED="${POSTPROCESS_SEED:-${CHANNEL_SEED}}"
STAMP="$(date +%Y%m%d_%H%M%S)"
SWEEP_ROOT_BASE="${SWEEP_ROOT_BASE:-${PROJ_DIR}/logs/q4_ab_compare_${STAMP}}"
SUMMARY_FILE="${SWEEP_ROOT_BASE}/ab_compare_manifest.txt"

if [ ! -f "$RUNNER" ]; then
  echo "ERROR: runner not found: $RUNNER"
  exit 1
fi

mkdir -p "$SWEEP_ROOT_BASE"

{
  echo "# Q4 AB compare manifest"
  echo "# timestamp: ${STAMP}"
  echo "# methods: ${METHODS}"
  echo "# stop_on_fail: ${STOP_ON_FAIL}"
  echo "# channel_seed: ${CHANNEL_SEED} (same for all methods)"
  echo "# min_srs_frames: ${MIN_SRS_FRAMES}"
  echo "# runner: ${RUNNER}"
  echo "# snr_points: ${SNR_POINTS:-<runner_default>}"
  echo "# only_snr: ${ONLY_SNR:-<none>}"
  echo "# max_frames: ${MAX_FRAMES:-<runner_default>}"
  echo "# min_srs_bins: ${MIN_SRS_BINS:-<runner_default>}"
  echo "# hard_ceiling_sec: ${HARD_CEILING_SEC:-<runner_default>}"
  echo "# auto_postprocess_bg: ${AUTO_POSTPROCESS_BG}"
  echo "# postprocess_speed: ${POSTPROCESS_SPEED}"
  echo "# postprocess_seed: ${POSTPROCESS_SEED}"
  echo "# ----------------------------------------"
  printf "%-10s  %-8s  %s\n" "method" "status" "sweep_root"
} > "$SUMMARY_FILE"

print_adaptive_env() {
  echo "  SRS_2D_ADAPT_A1=${SRS_2D_ADAPT_A1:-<default_in_c>}"
  echo "  SRS_2D_ADAPT_A2=${SRS_2D_ADAPT_A2:-<default_in_c>}"
  echo "  SRS_2D_ADAPT_A3=${SRS_2D_ADAPT_A3:-<default_in_c>}"
  echo "  SRS_2D_ADAPT_CMODEL=${SRS_2D_ADAPT_CMODEL:-<default_in_c>}"
  echo "  SRS_2D_ADAPT_ALPHA_INIT=${SRS_2D_ADAPT_ALPHA_INIT:-<default_in_c>}"
  echo "  SRS_2D_ADAPT_ALPHA_MIN=${SRS_2D_ADAPT_ALPHA_MIN:-<default_in_c>}"
  echo "  SRS_2D_ADAPT_ALPHA_MAX=${SRS_2D_ADAPT_ALPHA_MAX:-<default_in_c>}"
  echo "  SRS_2D_ADAPT_RR_EMA=${SRS_2D_ADAPT_RR_EMA:-<default_in_c>}"
  echo "  SRS_2D_ADAPT_SNR_EMA=${SRS_2D_ADAPT_SNR_EMA:-<default_in_c>}"
}

echo "========================================================================"
echo "  Q4 AB Compare Runner (v8)"
echo "========================================================================"
echo "  methods        : ${METHODS}"
echo "  channel seed   : ${CHANNEL_SEED} (same-channel A/B)"
echo "  min SRS frames : ${MIN_SRS_FRAMES}"
echo "  sweep root base: ${SWEEP_ROOT_BASE}"
echo "  stop on fail   : ${STOP_ON_FAIL}"
echo "  auto post bg   : ${AUTO_POSTPROCESS_BG}"
echo "========================================================================"
echo ""

for method in $METHODS; do
  case "$method" in
    ewma|adaptive|ibvss|kalman|scalarkalman)
      ;;
    *)
      echo "ERROR: unsupported method '${method}' (allowed: ewma adaptive ibvss kalman)"
      exit 1
      ;;
  esac

  method_root="${SWEEP_ROOT_BASE}/${method}"
  mkdir -p "$method_root"
  export SWEEP_ROOT="$method_root"
  export GT_DIR_BASE="/tmp/oai_gpu_ipc/sionna_gt"
  export SRS_ESTIMATOR="${SRS_ESTIMATOR:-2d-mmse}"
  export SRS_OPTFILT="${SRS_OPTFILT:-0}"
  export SRS_2D_METHOD="$method"
  export SKIP_PRESWEEP_PREFLIGHT="${SKIP_PRESWEEP_PREFLIGHT:-0}"
  export SKIP_PREFLIGHT="${SKIP_PREFLIGHT:-0}"

  echo ""
  echo "################################################################"
  echo "# Method: ${method}"
  echo "# Sweep root: ${method_root}"
  echo "# SRS_ESTIMATOR=${SRS_ESTIMATOR}, SRS_OPTFILT=${SRS_OPTFILT}, SRS_2D_METHOD=${SRS_2D_METHOD}"
  if [ "$method" = "adaptive" ]; then
    print_adaptive_env
  elif [ "$method" = "ibvss" ]; then
    echo "  SRS_2D_IBVSS_ALPHA_INIT=${SRS_2D_IBVSS_ALPHA_INIT:-0.5}"
    echo "  SRS_2D_IBVSS_ALPHA_MIN=${SRS_2D_IBVSS_ALPHA_MIN:-0.02}"
    echo "  SRS_2D_IBVSS_ALPHA_MAX=${SRS_2D_IBVSS_ALPHA_MAX:-0.98}"
    echo "  SRS_2D_IBVSS_INNOV_EMA=${SRS_2D_IBVSS_INNOV_EMA:-0.15}"
    echo "  SRS_2D_IBVSS_WARMUP=${SRS_2D_IBVSS_WARMUP:-20}"
    echo "  SRS_2D_IBVSS_ADAPTIVE_CMODEL=${SRS_2D_IBVSS_ADAPTIVE_CMODEL:-1}"
  elif [ "$method" = "kalman" ] || [ "$method" = "scalarkalman" ]; then
    echo "  SRS_2D_KALMAN_ALPHA_MIN=${SRS_2D_KALMAN_ALPHA_MIN:-0.02}"
    echo "  SRS_2D_KALMAN_ALPHA_MAX=${SRS_2D_KALMAN_ALPHA_MAX:-0.98}"
    echo "  SRS_2D_KALMAN_INNOV_EMA=${SRS_2D_KALMAN_INNOV_EMA:-0.15}"
    echo "  SRS_2D_KALMAN_Q_EMA=${SRS_2D_KALMAN_Q_EMA:-0.10}"
    echo "  SRS_2D_KALMAN_WARMUP=${SRS_2D_KALMAN_WARMUP:-30}"
  fi
  echo "################################################################"
  echo ""

  if bash "$RUNNER"; then
    status="OK"
  else
    status="FAIL"
  fi

  if [ "$status" = "OK" ]; then
    if [ "$AUTO_POSTPROCESS_BG" = "1" ]; then
      if [ -x "$POSTPROC_SCRIPT" ]; then
        echo "[ab] enqueue postprocess for ${method} (background)"
        bash "$POSTPROC_SCRIPT" \
          --sweep-dir "$method_root" \
          --speed "$POSTPROCESS_SPEED" \
          --seed "$POSTPROCESS_SEED" \
          --background
      else
        echo "[ab] WARN: postprocess script missing/executable bit not set: $POSTPROC_SCRIPT"
      fi
    else
      echo "[ab] postprocess command for ${method}:"
      echo "     bash \"$POSTPROC_SCRIPT\" --sweep-dir \"$method_root\" --speed \"$POSTPROCESS_SPEED\" --seed \"$POSTPROCESS_SEED\" --background"
    fi
  fi

  printf "%-10s  %-8s  %s\n" "$method" "$status" "$method_root" >> "$SUMMARY_FILE"

  if [ "$status" != "OK" ] && [ "$STOP_ON_FAIL" = "1" ]; then
    echo "[ab] ${method} failed and STOP_ON_FAIL=1, aborting."
    break
  fi
done

echo ""
echo "========================================================================"
echo "  AB compare finished"
echo "  Summary: ${SUMMARY_FILE}"
echo "========================================================================"
cat "$SUMMARY_FILE"
echo ""

