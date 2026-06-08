#!/bin/bash
#
# complexity_native.sh — per-TTI CPU of the SRS estimator blocks (professor 6-04
# ask A: complexity vs legacy, must not exceed the TTI). Runs several estimator
# scenarios with SRS_TIMING=1 on the SAME channel and prints each run's
# [SRS timing] summary (per-stage avg/max us + ADDED-per-SRS vs 500us @mu=1).
#
# Note: the timing instruments OUR added blocks only (freq_wiener / pkf_time /
# wiener_*). legacy runs them 0 times -> "ADDED = 0" is the baseline; true2d's
# ADDED is the increment over legacy (the legacy filt8/16 is the common cheap
# base present in both). CPU is ~channel/speed-independent (same matrix ops).
#
# Usage (sudo; recompile nr-softmodem first):
#   sudo bash complexity_native.sh
# Env overrides: CIR= ND= SEED= DUR= P= K=(wiener horizon for the +predict run)

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="/home/dclcom61/OAI_luuuuuu/DevChannelProxyJIN"
CIR="${CIR:-$SCRIPT_DIR/cir/cir_cdl_c_s10.bin}"
ND="${ND:--6}"; SEED="${SEED:-12345}"; DUR="${DUR:-60}"; P="${P:-10}"; K="${K:-4}"

[ "$(id -u)" -eq 0 ] || { echo "ERROR: need sudo"; exit 1; }
[ -f "$CIR" ] || { echo "ERROR: missing CIR $CIR"; exit 1; }
latest() { readlink -f "$PROJ/logs/native_latest"; }

run() {   # $1=label  $2=estimator  $3=extra-env
    echo ""
    echo "######## $1 ########"
    eval "$3" SRS_TIMING=1 bash "$SCRIPT_DIR/launch_native.sh" \
        -e "$2" -cir "$CIR" -nd "$ND" -seed "$SEED" -p "$P" -d "$DUR" --restart-5gc \
        >/dev/null 2>&1
    local RUN; RUN="$(latest)"
    echo "  run: $RUN"
    local T; T=$(grep "SRS timing" "$RUN/gnb.log" 2>/dev/null | tail -7)
    if [ -n "$T" ]; then echo "$T" | sed 's/^/  /'
    else echo "  (no added-block timing -> baseline, ADDED=0)"; fi
}

echo "=================================================================="
echo "[complexity] CIR=$CIR nd=$ND p=$P dur=${DUR}s  (per-TTI budget 500us @mu=1)"
echo "=================================================================="
run "1) legacy (baseline)"              legacy ""
run "2) true2d (freq Wiener + PKF)"     true2d ""
run "3) true2d + Wiener predict k=$K"   true2d "SRS_WIENER_PREDICT_AHEAD=$K"
echo ""
echo "Compare each run's 'ADDED(freq+pkf+wiener) per-SRS avg' to 500us."
echo "legacy=0 (our blocks not called); true2d=freq+pkf; +predict adds wiener_*."
