#!/bin/bash
#
# Q4 Sweep Postprocess Runner (v8)
# =================================
# Runs offline post-processing for one sweep root:
#   1) prepare_2d_mmse_data.py
#   2) test_2d_mmse.py
#
# Can run in foreground or detached background mode.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREP_SCRIPT="${SCRIPT_DIR}/prepare_2d_mmse_data.py"
TEST_SCRIPT="${SCRIPT_DIR}/test_2d_mmse.py"

SWEEP_DIR="${SWEEP_DIR:-${SWEEP_ROOT:-}}"
SPEED_VAL="${UE_SPEED:-0}"
SEED_VAL="${CHANNEL_SEED:-42}"
RUN_IN_BACKGROUND=0

usage() {
  cat <<EOF
Usage:
  bash run_q4_postprocess_v8.sh --sweep-dir <path> [--speed <mps>] [--seed <int>] [--background]

Options:
  --sweep-dir PATH    Sweep root directory (contains snr_*dB subdirs)
  --speed VALUE       UE speed used in this sweep (default: ${SPEED_VAL})
  --seed VALUE        Channel seed used in this sweep (default: ${SEED_VAL})
  --background        Launch detached background job and return immediately
  -h, --help          Show help

Env fallback:
  SWEEP_DIR / SWEEP_ROOT, UE_SPEED, CHANNEL_SEED
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sweep-dir)
      SWEEP_DIR="$2"
      shift 2
      ;;
    --speed)
      SPEED_VAL="$2"
      shift 2
      ;;
    --seed)
      SEED_VAL="$2"
      shift 2
      ;;
    --background)
      RUN_IN_BACKGROUND=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ -z "${SWEEP_DIR}" ]]; then
  echo "ERROR: --sweep-dir is required (or set SWEEP_DIR/SWEEP_ROOT)"
  usage
  exit 1
fi

if [[ ! -d "${SWEEP_DIR}" ]]; then
  echo "ERROR: sweep dir not found: ${SWEEP_DIR}"
  exit 1
fi

if [[ ! -f "${PREP_SCRIPT}" ]]; then
  echo "ERROR: missing script: ${PREP_SCRIPT}"
  exit 1
fi

if [[ ! -f "${TEST_SCRIPT}" ]]; then
  echo "ERROR: missing script: ${TEST_SCRIPT}"
  exit 1
fi

if [[ "${RUN_IN_BACKGROUND}" -eq 1 ]]; then
  STAMP="$(date +%Y%m%d_%H%M%S)"
  LOG_FILE="${SWEEP_DIR}/postprocess_${STAMP}.log"
  nohup bash "$0" \
    --sweep-dir "${SWEEP_DIR}" \
    --speed "${SPEED_VAL}" \
    --seed "${SEED_VAL}" \
    > "${LOG_FILE}" 2>&1 &
  BG_PID=$!
  echo "[post] background postprocess started"
  echo "[post] pid : ${BG_PID}"
  echo "[post] log : ${LOG_FILE}"
  exit 0
fi

echo "========================================================================"
echo "  Q4 Postprocess (v8)"
echo "========================================================================"
echo "  Sweep dir : ${SWEEP_DIR}"
echo "  Speed     : ${SPEED_VAL}"
echo "  Seed      : ${SEED_VAL}"
echo "========================================================================"
echo ""

echo "──────────────────────────────────────────────────────────────"
echo "  Step 1/2: prepare_2d_mmse_data.py"
echo "──────────────────────────────────────────────────────────────"
python3 "${PREP_SCRIPT}" \
  --sweep-dir "${SWEEP_DIR}" \
  --speed "${SPEED_VAL}" \
  --seed "${SEED_VAL}"

echo ""
echo "──────────────────────────────────────────────────────────────"
echo "  Step 2/2: test_2d_mmse.py"
echo "──────────────────────────────────────────────────────────────"
python3 "${TEST_SCRIPT}" --speed "${SPEED_VAL}"

echo ""
echo "[post] Done."
echo "[post] Results: ${SCRIPT_DIR}/data_out/2d_mmse_results/"
