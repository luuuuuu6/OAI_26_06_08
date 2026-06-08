#!/bin/bash
#
# launch_native.sh — 一键启动 native rfsimulator + channelmod 真实频选测试床
#
# 与 launch_all_v9.sh(proxy/GPU-IPC) 的区别:
#   - 不走 Sionna/GPU/docker proxy; 纯 native OAI rfsim socket (gNB<->UE 直连 4043)
#   - 信道用 OAI 内建 channelmod(TDL_A, conf 里配), 上行频选
#   - 1T1R SISO (2T2R/2端口SSB 在原生路径不工作, 见日志 §12.3)
#   - "先平坦 attach -> handoff 切 TDL" (触发文件 /tmp/rfsim_chan_on)
#   - GT = rfsim channelDesc->ch[] 时域抽头 dump (NATIVE_GT_DUMP=1)
#
# 用法:
#   sudo bash launch_native.sh [选项]
#
# 选项:
#   -e EST        SRS_ESTIMATOR: legacy|true2d|mmse1d|...  (默认 legacy)
#   -d SEC        attach 后自动跑 SEC 秒再退出   (默认 180s; -d 0 = Ctrl+C 为止)
#   -p N          SRS_PERIOD_SLOTS                          (默认 10)
#   -nd DB        上行噪声 ue0.noise_power_dB (逐信道, 仅 handoff 后加 -> attach 干净)
#                 (默认 off=无噪; 越大越吵, 如 -20 轻 / -10 重; 看 gNB 报的 SNR 调)
#   -seed N       固定信道随机种子 OAI_RNGSEED (同信道 A/B)  (默认 时间随机)
#   -cir FILE     回放 CDL CIR 序列(cdl_to_cir.py 生成)，handoff 后施加；自动把 GT dump 设为每slot
#   -gt-every N   NATIVE_GT_EVERY_SAMPLES (GT dump 周期, 样本)  (默认 C 端 307200; -cir 时自动 30720)
#   --handoff-delay SEC  in-sync 后等 SEC 秒再 handoff 切 TDL (默认 5)
#   --no-handoff  不自动 handoff (上行保持平坦, 用于纯 attach 验证)
#   --no-gt       不 dump GT (NATIVE_GT_DUMP=0)
#   --no-cn5g-check  跳过 5GC 容器检查
#   --restart-5gc 即使 5GC 全 Up 也 docker compose restart 一次 (清 AMF/NAS 累积状态;
#                 连续多次 attach 后会出现 NAS 不 accept / RRCReconfig 不完成 / SRS 捕获 0)
#   -h            帮助
#
# 退出时:
#   - 优雅 SIGINT gNB/UE (让 SRS 乒乓缓冲 flush), 再强杀兜底
#   - 若开了 GT dump: 自动跑 native_gt_to_npz.py 生成 sionna_gt/gt_batch_ue0_seq0.npz
#   - 打印 eval 命令
#
# 日志/产物:  logs/native_<EST>_<时间戳>/  (gnb.log, ue.log, srs_matrix_*.bin, sionna_gt/)

set -u

# ── 默认值 ────────────────────────────────────────────────────────
EST="${SRS_ESTIMATOR:-legacy}"
# Auto-stop after this many seconds so a forgotten run can't hang indefinitely.
# Override with -d SEC or env LAUNCH_DURATION; use -d 0 to run until Ctrl+C.
DURATION="${LAUNCH_DURATION:-180}"
SRS_PERIOD="${SRS_PERIOD_SLOTS:-10}"
HANDOFF_DELAY=5
DO_HANDOFF=1
DO_GT=1
CHECK_CN5G=1
RESTART_CN5G="${RESTART_CN5G:-0}"   # 1 = 即使全 Up 也 restart 5GC (清 NAS/UE 累积态)
CN5G_RESTART_WAIT="${CN5G_RESTART_WAIT:-5}"   # restart 后等待秒数 (restart 比 cold-up 快, 默认 5s)
NOISE_DB=""               # ue0 noise_power_dB (逐信道噪声, 仅 handoff 后施加 -> attach 干净).
                          # 空=无噪. 越大噪声越强 (e.g. -20 较轻, -10 较重); 看 gNB 报的 SNR 调.
SEED=""                   # OAI_RNGSEED: 固定信道实现 (同信道 A/B). 空=时间随机
CIR_FILE=""               # RFSIM_CIR_REPLAY: 回放 cdl_to_cir.py 生成的 CDL CIR 序列 (handoff 后). 空=用 conf 的 TDL_A
GT_EVERY=""               # NATIVE_GT_EVERY_SAMPLES: GT dump 周期(样本). 动态信道建议 30720(每slot). 空=C 默认(307200)

# ── 路径 ──────────────────────────────────────────────────────────
PROJ="/home/dclcom61/OAI_luuuuuu/DevChannelProxyJIN"
BUILD="$PROJ/openairinterface5g_whan/cmake_targets/ran_build/build"
CONF="$PROJ/openairinterface5g_whan/targets/PROJECTS/GENERIC-NR-5GC/CONF/gnb.sa.band78.fr1.106PRB.usrpb210.conf"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CN5G_COMPOSE="$PROJ/openairinterface5g_whan/doc/tutorial_resources/oai-cn5g/docker-compose.yaml"
TRIGGER="/tmp/rfsim_chan_on"
GT_BIN="/tmp/oai_gpu_ipc/native_gt.bin"

# ── 参数解析 ──────────────────────────────────────────────────────
usage() { sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        -e) EST="$2"; shift 2 ;;
        -d) DURATION="$2"; shift 2 ;;
        -p) SRS_PERIOD="$2"; shift 2 ;;
        --handoff-delay) HANDOFF_DELAY="$2"; shift 2 ;;
        -nd) NOISE_DB="$2"; shift 2 ;;
        -seed) SEED="$2"; shift 2 ;;
        -cir) CIR_FILE="$2"; shift 2 ;;
        -gt-every) GT_EVERY="$2"; shift 2 ;;
        --no-handoff) DO_HANDOFF=0; shift ;;
        --no-gt) DO_GT=0; shift ;;
        --no-cn5g-check) CHECK_CN5G=0; shift ;;
        --restart-5gc) RESTART_CN5G=1; shift ;;
        -h|--help) usage ;;
        *) echo "未知选项: $1"; usage ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: 需要 root (nr-softmodem 要 sudo)。请用: sudo bash launch_native.sh ..."
    exit 1
fi

# gNB runs from $RUN (cd), so a relative -cir path would break -> make absolute.
if [ -n "$CIR_FILE" ]; then
    if [ ! -f "$CIR_FILE" ]; then echo "ERROR: -cir 文件不存在: $CIR_FILE"; exit 1; fi
    CIR_FILE="$(realpath "$CIR_FILE")"
fi

TS=$(date +%Y%m%d_%H%M%S)
RUN="$PROJ/logs/native_${EST}_${TS}"
mkdir -p "$RUN/sionna_gt"
ln -sfn "$RUN" "$PROJ/logs/native_latest"

PIDS=()

# ── Preflight ─────────────────────────────────────────────────────
preflight() {
    echo "[preflight] 清场 host 进程..."
    pkill -9 -f nr-softmodem   2>/dev/null || true
    pkill -9 -f nr-uesoftmodem 2>/dev/null || true
    sleep 2
    echo "[preflight] 清理 /tmp 残留 (trigger / GT / rfsim shm)..."
    rm -f "$TRIGGER" "$GT_BIN" 2>/dev/null || true
    rm -f /tmp/oai_gpu_ipc/gpu_ipc_shm* 2>/dev/null || true
    mkdir -p /tmp/oai_gpu_ipc && chmod 777 /tmp/oai_gpu_ipc 2>/dev/null || true

    if [ "$CHECK_CN5G" -eq 1 ]; then
        echo "[preflight] 检查 5GC (SA attach 必需)..."
        local core=("oai-amf" "oai-smf" "oai-upf" "oai-nrf" "oai-udm" "oai-ausf" "oai-udr" "mysql")
        local down=""
        for c in "${core[@]}"; do
            local st
            st=$(docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | awk -v c="$c" '$1==c{print $2}')
            [[ "$st" == Up* ]] || down+="$c "
        done
        if [ -n "$down" ]; then
            echo "[preflight]   5GC 未全 Up ($down) → docker compose up -d ..."
            if [ -f "$CN5G_COMPOSE" ]; then
                docker compose -f "$CN5G_COMPOSE" up -d 2>/dev/null || true
                echo "[preflight]   等 25s 让 5GC 稳定..."
                sleep 25
            else
                echo "[preflight]   ⚠ 未找到 compose 文件, 请手动确认 5GC 在跑"
            fi
        elif [ "$RESTART_CN5G" -eq 1 ]; then
            echo "[preflight]   5GC 全 Up 但 --restart-5gc 指定 → docker compose restart (清 NAS/UE 累积态)..."
            if [ -f "$CN5G_COMPOSE" ]; then
                docker compose -f "$CN5G_COMPOSE" restart 2>/dev/null || true
                echo "[preflight]   等 ${CN5G_RESTART_WAIT}s 让 5GC 稳定..."
                sleep "$CN5G_RESTART_WAIT"
            else
                echo "[preflight]   ⚠ 未找到 compose 文件, 无法 restart"
            fi
        else
            echo "[preflight]   ✓ 5GC 全 Up (若多次 attach 后 SRS 捕获 0, 加 --restart-5gc)"
        fi
    fi
    echo "[preflight] 完成。run 目录: $RUN"
}

# ── cleanup (退出时) ──────────────────────────────────────────────
cleanup() {
    echo ""
    echo "[launch] 收尾中..."
    # 优雅 SIGINT 让 SRS 乒乓缓冲 flush
    pkill -INT -f nr-softmodem   2>/dev/null || true
    pkill -INT -f nr-uesoftmodem 2>/dev/null || true
    for _i in $(seq 1 10); do
        pgrep -f nr-softmodem >/dev/null 2>&1 || break
        grep -q "Writer thread exited" "$RUN/gnb.log" 2>/dev/null && { sleep 1; break; }
        sleep 1
    done
    pkill -9 -f nr-softmodem   2>/dev/null || true
    pkill -9 -f nr-uesoftmodem 2>/dev/null || true

    # 自动转换 GT
    local n_srs
    n_srs=$(ls "$RUN"/srs_matrix_gNB_*.bin 2>/dev/null | wc -l)
    echo "[launch] SRS bin 数: $n_srs"
    if [ "$DO_GT" -eq 1 ] && [ -f "$GT_BIN" ] && [ "$n_srs" -gt 0 ]; then
        echo "[launch] 转换 native GT -> sionna_gt/ ..."
        cp -f "$GT_BIN" "$RUN/native_gt.bin" 2>/dev/null || true
        python3 "$SCRIPT_DIR/native_gt_to_npz.py" "$GT_BIN" "$RUN" 2>&1 | sed 's/^/  /'
        # make run artifacts re-convertible/eval-able by non-root (script runs as root)
        chmod -R a+rwX "$RUN" 2>/dev/null || true
    fi

    echo ""
    echo "========================================================"
    echo "  run 目录: $RUN"
    echo "  评估 (单发 NMSE):"
    echo "    cd $SCRIPT_DIR && python3 eval_nmse_sto.py --run-dir $RUN"
    echo "  A/B (需另一发不同 estimator):"
    echo "    python3 eval_nmse_sto.py --ab <legacy_run> <true2d_run>"
    echo "========================================================"
    exit 0
}
trap cleanup SIGINT SIGTERM

# ── 启动 ──────────────────────────────────────────────────────────
preflight

GNB_ENV="SRS_ESTIMATOR=$EST SRS_PERIOD_SLOTS=$SRS_PERIOD"
[ "$DO_GT" -eq 1 ] && GNB_ENV="$GNB_ENV NATIVE_GT_DUMP=1 NATIVE_GT_PATH=$GT_BIN"
[ -n "$SEED" ]    && GNB_ENV="$GNB_ENV OAI_RNGSEED=$SEED"
# CDL CIR replay: enable + default GT dump to per-slot (dynamic needs per-slot GT)
if [ -n "$CIR_FILE" ]; then
    GNB_ENV="$GNB_ENV RFSIM_CIR_REPLAY=$CIR_FILE"
    [ -z "$GT_EVERY" ] && GT_EVERY=30720
fi
[ -n "$GT_EVERY" ] && GNB_ENV="$GNB_ENV NATIVE_GT_EVERY_SAMPLES=$GT_EVERY"
# Forward SRS 2D / PKF tuning (E1: phase-prediction mode) if set in the env, so
# `SRS_2D_PKF_PHASE=2 ./launch_native.sh ...` is explicit and shows in the log.
for _v in SRS_2D_DEBUG SRS_2D_PKF_PHASE SRS_2D_PKF_OMEGA_BANDS SRS_2D_PKF_BAND_MIN \
          SRS_2D_PKF_SLOPE_MAX SRS_2D_PKF_RAMP_GATE SRS_2D_PKF_KW SRS_PKF_PREDICT_AHEAD \
          SRS_WIENER_PREDICT_AHEAD SRS_WIENER_ORDER SRS_WIENER_KMAX \
          SRS_WIENER_R_EMA SRS_WIENER_DIAG_LOAD SRS_TIMING SRS_PARALLEL SRS_SHARE_R; do
    eval "_val=\${$_v:-}"
    [ -n "$_val" ] && GNB_ENV="$GNB_ENV $_v=$_val"
done

# Noise: use a per-run conf copy with ue0.noise_power_dB patched. This is the
# PER-CHANNEL noise path (applied inside rxAddInput's channel_mode==1 branch),
# so it only kicks in AFTER the handoff -> the flat attach phase stays clean
# (avoids the global --channelmod.noise_power_dBFS which would corrupt PRACH/RA).
CONF_USE="$CONF"
if [ -n "$NOISE_DB" ]; then
    CONF_USE="$RUN/gnb.conf"
    cp -f "$CONF" "$CONF_USE"
    sed -i -E "s/noise_power_dB[[:space:]]*=[[:space:]]*-?[0-9.]+/noise_power_dB = $NOISE_DB/g" "$CONF_USE"
    echo "[launch] noise: ue0.noise_power_dB = $NOISE_DB (per-run conf, post-handoff only)"
fi

echo "[launch] 启动 gNB (EST=$EST, SRS_PERIOD=$SRS_PERIOD, GT_DUMP=$DO_GT, "\
"noise_dB=${NOISE_DB:-off}, seed=${SEED:-rand}, cir=${CIR_FILE:-off}, gt_every=${GT_EVERY:-default}, DL ${NB_TX:-1}T×${NB_RX:-1}R+chanmod)..."
# Thread pool: TPOOL="n" (default) = no pool -> SRS estimation runs inline (serial,
# = legacy behaviour). Set TPOOL="0,1,2,3" (comma core list) to enable the L1 pool;
# the per-(ant,port) SRS channel estimation then fans out one worker per RX antenna
# (near-linear speedup for MIMO). NB_RX/NB_TX expose the RU antenna counts (default
# 1T1R); >4 RX is now supported by the runtime-dynamic per-link state.
( cd "$RUN" && eval "$GNB_ENV" "$BUILD/nr-softmodem" \
    -O "$CONF_USE" --gNBs.[0].min_rxtxtime 6 \
    --rfsim --rfsimulator.serveraddr server --rfsimulator.options chanmod \
    --thread-pool "${TPOOL:-n}" \
    --RUs.[0].nb_tx "${NB_TX:-1}" --RUs.[0].nb_rx "${NB_RX:-1}" \
    --gNBs.[0].pdsch_AntennaPorts_XP 1 --gNBs.[0].pusch_AntennaPorts "${NB_RX:-1}" \
    > "$RUN/gnb.log" 2>&1 ) &
PIDS+=($!)

echo "[launch] 等 gNB rfsim server 就绪..."
for i in $(seq 1 30); do
    grep -q "Running as server" "$RUN/gnb.log" 2>/dev/null && { echo "[launch]   ✓ server 就绪"; break; }
    pgrep -f nr-softmodem >/dev/null 2>&1 || { echo "[launch] ERROR: gNB 启动失败, 看 $RUN/gnb.log"; cleanup; }
    [ "$i" -eq 30 ] && echo "[launch]   ⚠ 30s 超时, 继续..."
    sleep 1
done

# ── Phase 1 上行 SRS MIMO: UE 多端口 SRS 发射 ─────────────────────────
# UE_TX = UE SRS 天线端口数 (1/2/4)。>1 时上行变 N_tx 端口 SRS,gNB 多 RX(NB_RX)
# 接收 -> 真 N_rx×N_tx SRS 信道。下行保持 SISO(gNB nb_tx=1,见上),不触发 rfsim
# 多天线 DL 读路径 bug,attach 不受影响。UECAP 自动按 UE_TX 选 uecap_portsN.xml
# (放开 maxNumberSRS-Ports-PerResource);也可用 env UECAP 覆盖。
UE_TX="${UE_TX:-1}"
CONF_DIR="$(dirname "$CONF")"
UECAP="${UECAP:-$CONF_DIR/uecap_ports${UE_TX}.xml}"
UE_ANT_ARGS=()
if [ "$UE_TX" -gt 1 ]; then
    if [ ! -f "$UECAP" ]; then
        echo "[launch] ERROR: UE_TX=$UE_TX 需要能力文件 $UECAP,未找到"; cleanup
    fi
    UE_ANT_ARGS=(--ue-nb-ant-tx "$UE_TX" --uecap_file "$UECAP")
    echo "[launch]   UE 多端口 SRS: ue-nb-ant-tx=$UE_TX, uecap=$(basename "$UECAP")"
fi

echo "[launch] 启动 UE (UE_TX=${UE_TX}T × DL 1R, 连 127.0.0.1:4043)..."
( cd "$RUN" && "$BUILD/nr-uesoftmodem" \
    -r 106 --numerology 1 --band 78 -C 3619200000 \
    --uicc0.imsi 001010000000001 \
    --rfsim --rfsimulator.serveraddr 127.0.0.1 --rfsimulator.serverport 4043 \
    --ue-timing-correction-disable \
    "${UE_ANT_ARGS[@]}" \
    > "$RUN/ue.log" 2>&1 ) &
PIDS+=($!)

# ── attach 监控 + 自动 handoff ────────────────────────────────────
echo "[launch] 等 attach (gNB 'UE RNTI ... in-sync')..."
ATTACHED=0
for i in $(seq 1 120); do
    if grep -q "in-sync" "$RUN/gnb.log" 2>/dev/null; then
        echo "[launch]   ✓ attach 成功 ($(grep -o 'UE RNTI [0-9a-fx]*' "$RUN/gnb.log" | tail -1))"
        ATTACHED=1
        break
    fi
    sleep 1
done
[ "$ATTACHED" -eq 0 ] && echo "[launch]   ⚠ 120s 未见 in-sync (看 $RUN/gnb.log / ue.log)"

if [ "$ATTACHED" -eq 1 ] && [ "$DO_HANDOFF" -eq 1 ]; then
    echo "[launch] handoff 前等 ${HANDOFF_DELAY}s (让链路稳定)..."
    sleep "$HANDOFF_DELAY"
    touch "$TRIGGER"
    echo "[launch]   ✓ 已 touch $TRIGGER → 上行切 TDL_A 频选"
    sleep 2
    if grep -q "channel handoff" "$RUN/gnb.log" 2>/dev/null; then
        echo "[launch]   ✓ gNB 确认 handoff: $(grep 'channel handoff' "$RUN/gnb.log" | tail -1)"
    else
        echo "[launch]   ⚠ 暂未见 handoff 日志 (稍后会出现)"
    fi
elif [ "$DO_HANDOFF" -eq 0 ]; then
    echo "[launch] --no-handoff: 上行保持平坦"
fi

# ── 运行 / 等待 ───────────────────────────────────────────────────
echo ""
echo "[launch] 运行中。监控:"
echo "    tail -f $RUN/gnb.log"
echo "    tail -f $RUN/ue.log"
echo "    ls -la $GT_BIN $RUN/srs_matrix_gNB_*.bin"
if [ -n "$DURATION" ] && [ "$DURATION" != "0" ]; then
    echo "[launch] ${DURATION}s 后自动退出 (或 Ctrl+C; 用 -d 0 / LAUNCH_DURATION=0 禁用自动退出)..."
    sleep "$DURATION"
    echo "[launch] 到时自动收尾..."
    cleanup
else
    echo "[launch] Ctrl+C 退出 (会自动 flush SRS + 转换 GT)。"
    wait "${PIDS[0]}" 2>/dev/null || true
    cleanup
fi
