#!/bin/bash
#
# sweep_native_cdl.sh — native CDL 回放下 true2d vs passthru(裸LS) A/B 扫描
#
# 对每个 (CDL 模型 × 噪声档)：用同一信道(同 seed + 同 CIR)各跑一发 passthru 和
# true2d，handoff 后回放该 CDL CIR，收 SRS+GT，eval --ab。每点可 -rep 重复取中位
# (压离群)。汇总同时给出 passthru NMSE(=裸LS质量, 作可靠横轴) 与 true2d 增益。
#
# 用法:
#   sudo bash sweep_native_cdl.sh [-m "cdl_c_s0 cdl_b_s0"] [-nd "-15 -12 -9"] \
#                                 [-d 60] [-seed 12345] [-rep 1]
# 选项:
#   -m  LIST   CDL CIR 名列表(空格; 对应 cir/cir_<m>.bin)  (默认 "cdl_c_s0")
#   -nd LIST   噪声 noise_power_dB 列表                      (默认 "-15 -12 -9")
#   -d  SEC    每发 attach 后采集时长                        (默认 60)
#   -seed N    固定信道种子                                  (默认 12345)
#   -rep N     每工作点重复次数(取中位)                      (默认 1)
#   -A EST     基线估计器(A)                                 (默认 passthru)
#   -B EST     待测估计器(B); delta=B-A(负=B更好)            (默认 true2d)

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="/home/dclcom61/OAI_luuuuuu/DevChannelProxyJIN"
LOGS="$PROJ/logs"

MODELS="cdl_c_s0"; NOISES="-15 -12 -9"; DUR=60; SEED=12345; REP=1
EA="passthru"; EB="true2d"; REST5_EVERY=0; LCNT=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -m) MODELS="$2"; shift 2 ;;
        -nd) NOISES="$2"; shift 2 ;;
        -d) DUR="$2"; shift 2 ;;
        -seed) SEED="$2"; shift 2 ;;
        -rep) REP="$2"; shift 2 ;;
        -A) EA="$2"; shift 2 ;;
        -B) EB="$2"; shift 2 ;;
        -restart5gc) REST5_EVERY=1; shift ;;          # 每发重启 5GC 防 NAS 累积退化(SRS 捕获 0)
        -restart-every) REST5_EVERY="$2"; shift 2 ;;  # 自定义重启周期(发数); 0=从不
        *) echo "unknown: $1"; exit 1 ;;
    esac
done
[ "$(id -u)" -eq 0 ] || { echo "需 root: sudo bash sweep_native_cdl.sh ..."; exit 1; }

latest_run() { ls -dt "$LOGS"/native_"$1"_* 2>/dev/null | head -1; }

# parse "NMSE STO" (1st=passthru,2nd=true2d), coherence, delta from eval --ab output
SUMMARY=()
for m in $MODELS; do
    CIR="$SCRIPT_DIR/cir/cir_${m}.bin"
    if [ ! -f "$CIR" ]; then echo "[sweep] 缺 $CIR"; continue; fi
    for nd in $NOISES; do
        echo "=================================================================="
        echo "[sweep] model=$m noise_dB=$nd seed=$SEED rep=$REP  A=$EA B=$EB"
        echo "=================================================================="
        REPVALS=""   # lines: "delta A_nmse B_nmse A_coh B_coh"
        for r in $(seq 1 "$REP"); do
            R5A=""; if [ "$REST5_EVERY" -gt 0 ] && [ $((LCNT % REST5_EVERY)) -eq 0 ]; then R5A="--restart-5gc"; fi; LCNT=$((LCNT + 1))
            bash "$SCRIPT_DIR/launch_native.sh" -e "$EA" -cir "$CIR" -nd "$nd" -seed "$SEED" -d "$DUR" $R5A >/dev/null 2>&1
            PT=$(latest_run "$EA")
            R5B=""; if [ "$REST5_EVERY" -gt 0 ] && [ $((LCNT % REST5_EVERY)) -eq 0 ]; then R5B="--restart-5gc"; fi; LCNT=$((LCNT + 1))
            bash "$SCRIPT_DIR/launch_native.sh" -e "$EB" -cir "$CIR" -nd "$nd" -seed "$SEED" -d "$DUR" $R5B >/dev/null 2>&1
            T2=$(latest_run "$EB")
            AB=$(python3 "$SCRIPT_DIR/eval_nmse_sto.py" --ab "$PT" "$T2" 2>&1)
            pn=$(echo "$AB" | grep "NMSE STO" | sed -n 1p | grep -oE "[-+]?[0-9.]+ dB" | grep -oE "[-+]?[0-9.]+")
            tn=$(echo "$AB" | grep "NMSE STO" | sed -n 2p | grep -oE "[-+]?[0-9.]+ dB" | grep -oE "[-+]?[0-9.]+")
            # NOTE: grep the exact "cplx coherence" line only. The eval also prints a
            # "⚠ ALIGNMENT SUSPECT: complex coherence ..." line (unsigned) when coh<0.5;
            # a bare `grep coherence` would match it too and shift sed -n 2p -> nan.
            pc=$(echo "$AB" | grep "cplx coherence" | sed -n 1p | grep -oE "[-+][0-9.]+")
            tc=$(echo "$AB" | grep "cplx coherence" | sed -n 2p | grep -oE "[-+][0-9.]+")
            d=$(echo "$AB" | grep "delta NMSE STO" | grep -oE "[-+][0-9.]+ dB" | grep -oE "[-+][0-9.]+")
            echo "  rep$r: $EA=${pn}dB(coh$pc) $EB=${tn}dB(coh$tc) delta=${d}dB"
            REPVALS+="$d $pn $tn $pc $tc"$'\n'
        done
        line=$(printf "%s" "$REPVALS" | python3 -c '
import sys,statistics as st
rows=[l.split() for l in sys.stdin if l.strip()]
def med(j):
    v=[float(r[j]) for r in rows if len(r)>j]
    return st.median(v) if v else float("nan")
print("delta_med=%+.2f A_med=%.2f B_med=%.2f Acoh_med=%.2f Bcoh_med=%.2f n=%d"%(
    med(0),med(1),med(2),med(3),med(4),len(rows)))')
        echo "  [median] $line"
        SUMMARY+=("$m nd=$nd | $line")
    done
done

echo ""
echo "============== SWEEP 汇总 (delta 负=true2d更好; passthru=裸LS横轴) =============="
printf "%-16s %-7s %s\n" "model" "nd" "median(delta/passthru/true2d/coh)"
for s in "${SUMMARY[@]}"; do echo "  $s"; done
