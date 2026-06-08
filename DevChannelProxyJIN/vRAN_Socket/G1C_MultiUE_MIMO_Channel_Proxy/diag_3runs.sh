#!/bin/bash
# 连续跑 3 次 adaptive SNR=10 诊断 run
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="/home/dclcom61/OAI_luuuuuu/DevChannelProxyJIN/logs/diag_3runs_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULTS_DIR"

for i in 1 2 3; do
    echo ""
    echo "================================================================"
    echo "  DIAGNOSTIC RUN $i / 3"
    echo "================================================================"
    
    export P1B_NPZ="" NPY_DIR=data_out/cdl_c UE_SPEED=0.83
    export MAX_FRAMES=100 MIN_SRS_FRAMES=100 ONLY_SNR="10"
    export SRS_ESTIMATOR=2d-mmse SRS_2D_METHOD=adaptive
    export HARD_CEILING_SEC=900

    bash "$SCRIPT_DIR/run_q4_snr_sweep_v9.sh"
    
    # 收集结果
    LATEST=$(readlink -f /home/dclcom61/OAI_luuuuuu/DevChannelProxyJIN/logs/latest 2>/dev/null)
    if [ -d "$LATEST" ]; then
        ATTACH_RESULT=$(cat "$LATEST/attach_result_v9.txt" 2>/dev/null || echo "N/A")
        ULSCH=$(grep -o 'ulsch_errors [0-9]*' "$LATEST/gnb.log" 2>/dev/null | tail -1 || echo "N/A")
        SRS=$(grep -o 'Captured : [0-9]* frames' "$LATEST/gnb.log" 2>/dev/null || echo "N/A")
        echo "[DIAG] Run $i: attach=$ATTACH_RESULT $ULSCH $SRS"
        echo "run=$i attach=$ATTACH_RESULT $ULSCH $SRS latest=$LATEST" >> "$RESULTS_DIR/summary.txt"
        
        # 提取关键诊断行
        grep "UL DIAG" "$LATEST/gnb.log" 2>/dev/null | head -15 >> "$RESULTS_DIR/run${i}_gnb_ul_diag.txt"
        grep "UL DIAG" "$LATEST/proxy.log" 2>/dev/null | head -10 >> "$RESULTS_DIR/run${i}_proxy_ul_diag.txt"
        cat "$LATEST/attach_diag_v9.log" 2>/dev/null >> "$RESULTS_DIR/run${i}_attach_diag.txt"
    fi
done

echo ""
echo "================================================================"
echo "  ALL 3 RUNS COMPLETE — Summary:"
echo "================================================================"
cat "$RESULTS_DIR/summary.txt" 2>/dev/null
echo ""
echo "Full results: $RESULTS_DIR"
