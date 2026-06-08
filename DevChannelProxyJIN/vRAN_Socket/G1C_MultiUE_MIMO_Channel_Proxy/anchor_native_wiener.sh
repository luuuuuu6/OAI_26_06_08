#!/bin/bash
#
# anchor_native_wiener.sh — pin the LIVE C temporal-Wiener predictor onto the
# offline curve. Captures (same channel + seed):
#   1) one passthru run  -> offline curve baseline (replay computes ZOH/mode0/Wiener
#      at all horizons on the real captured LS)
#   2) one true2d run PER horizon k, with SRS_WIENER_PREDICT_AHEAD=k -> the gNB's
#      C Wiener predictor dumps its k-step prediction; eval_nmse_sto --predict-horizon k
#      scores it vs the FUTURE GT. Each capture = one k (C dumps a fixed horizon).
#
# Usage (needs sudo; recompile nr-softmodem first):
#   sudo bash anchor_native_wiener.sh
# Env overrides: CIR= ND= SEED= DUR= P= KS="1 4 8"
#
# Output: prints each run dir + the live k-step prediction NMSE, then the replay
# command for the offline curve on the SAME channel. Paste the run dirs back.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="/home/dclcom61/OAI_luuuuuu/DevChannelProxyJIN"
CIR="${CIR:-$SCRIPT_DIR/cir/cir_cdl_c_s10.bin}"   # 10 km/h CDL-C
ND="${ND:--6}"; SEED="${SEED:-12345}"; DUR="${DUR:-120}"; P="${P:-10}"
KS="${KS:-1 4 8}"

[ "$(id -u)" -eq 0 ] || { echo "ERROR: need sudo (nr-softmodem)"; exit 1; }
[ -f "$CIR" ] || { echo "ERROR: missing CIR $CIR (run cdl_to_cir.py first)"; exit 1; }
latest() { readlink -f "$PROJ/logs/native_latest"; }

echo "=================================================================="
echo "[anchor] CIR=$CIR seed=$SEED nd=$ND p=$P dur=${DUR}s horizons={$KS}"
echo "=================================================================="

echo ""
echo "### [1/$(( $(echo $KS | wc -w) + 1 ))] passthru baseline (offline curve) ###"
bash "$SCRIPT_DIR/launch_native.sh" -e passthru -cir "$CIR" -nd "$ND" -seed "$SEED" -p "$P" -d "$DUR" --restart-5gc >/dev/null 2>&1
PT="$(latest)"
echo "  passthru run-dir: $PT"

RESULTS=()
i=2
for k in $KS; do
    echo ""
    echo "### [$i/$(( $(echo $KS | wc -w) + 1 ))] true2d + C Wiener predict, k=$k ###"
    SRS_WIENER_PREDICT_AHEAD="$k" bash "$SCRIPT_DIR/launch_native.sh" \
        -e true2d -cir "$CIR" -nd "$ND" -seed "$SEED" -p "$P" -d "$DUR" --restart-5gc >/dev/null 2>&1
    RUN="$(latest)"
    echo "  k=$k run-dir: $RUN"
    LINE=$(python3 "$SCRIPT_DIR/eval_nmse_sto.py" --run-dir "$RUN" --predict-horizon "$k" 2>&1 \
           | grep -E "cplx coherence|NMSE STO\+scalar")
    echo "$LINE" | sed 's/^/    /'
    nmse=$(echo "$LINE" | grep "NMSE STO" | grep -oE "[-+][0-9.]+ dB" | grep -oE "[-+][0-9.]+")
    RESULTS+=("k=$k  liveC_Wiener_NMSE=${nmse}dB  ($RUN)")
    i=$((i+1))
done

echo ""
echo "============== LIVE C Wiener anchor points =============="
for r in "${RESULTS[@]}"; do echo "  $r"; done
echo ""
echo "offline curve on the SAME channel (run, then overlay the live points):"
echo "  python3 $SCRIPT_DIR/replay_true2d.py --run-dir $PT \\"
echo "      --win 16 --predict-horizon $(echo $KS | tr ' ' '\n' | sort -n | tail -1) \\"
echo "      --max-frames 250 --predict-phases 0 --wiener-order 4"
