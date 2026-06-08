#!/bin/bash
#
# Q4 SNR Sweep Runner — v7 Channel Proxy Edition
# ================================================
# Uses v7.py (v6 physics fixes + attach-stable improvements).
#
# Runs launch_all_v7.sh repeatedly across a set of input-SNR points,
# collecting at least MAX_FRAMES frames per point, and organizes the
# outputs under a single sweep root directory.
#
# Usage:
#   sudo bash run_q4_snr_sweep_v7.sh [options via env vars]
#
# Env vars:
#   SNR_POINTS       Space-separated SNR(dB) list  (default: "-5 0 5 10 15 20 25")
#   ONLY_SNR         Run ONLY these SNR points (overrides SNR_POINTS).  Use to
#                    resume after an interrupted sweep. e.g. ONLY_SNR="-5 10 15"
#   SWEEP_ROOT       Resume into an existing sweep root (default: new timestamp).
#                    Manifest is appended, not overwritten.
#   MAX_FRAMES       Frames per SNR point          (default: 300)
#   MIN_SRS_BINS     Also require ≥N SRS bin files before stopping a point
#                                                  (default: 2). Set 0 to disable
#                    (GT-only gating, old behaviour).
#                    Note: each SRS bin = MAX_DUMP_FRAMES(100) captures; with
#                    the default OAI SRS period this typically needs a few
#                    minutes per point, hence the raised HARD_CEILING_SEC.
#   HARD_CEILING_SEC Absolute per-point wall-clock ceiling (default: 900s).
#                    Watcher also stops if this is hit, logging `timeout(...)`.
#   GNB_NX GNB_NY    gNB antenna grid              (default: 2 1)
#   UE_NX  UE_NY     UE  antenna grid              (default: 2 1)
#   PROXY_VER        Proxy version                 (default: v7)
#   NUM_UES          Number of UEs                 (default: 1)
#   UE_SPEED         UE speed in m/s               (default: 3, v7's built-in)
#   DEFAULT_DIST_M   BS-UE default distance (m)    (default: 100)
#   ATTACH_STABLE_SEC Max stable time / fallback ceiling (default: 300, 0=off)
#   ATTACH_STABLE_MODE auto,time                  (default: auto)
#   ATTACH_STABLE_TRIGGER rrc_reconfig,srs,off    (default: rrc_reconfig)
#   ATTACH_STABLE_POST_DELAY seconds after trigger before dynamic handoff (default: 15)
#   P1B_DISTANCE_MODE P1B distance mode: default,tau (default: default)
#
# Ctrl+C behavior:
#   First Ctrl+C:  cleans up current SNR point via launch_all's trap, then
#                  aborts the sweep (does NOT proceed to next SNR).
#   Second Ctrl+C: hard-kills any lingering children.
#
# Outputs:
#   logs/q4_sweep_YYYYMMDD_HHMMSS/
#     snr_m10dB/    # per-SNR subdir (m10 = minus 10 dB)
#     snr_m5dB/
#     snr_0dB/
#     ...
#     sweep_manifest.txt
#

set -uo pipefail

# ── Configuration (env-override friendly) ────────────────────────────────
SNR_POINTS_DEFAULT="-5 0 5 10 15 20 25"
SNR_POINTS="${SNR_POINTS:-$SNR_POINTS_DEFAULT}"
ONLY_SNR="${ONLY_SNR:-}"
if [ -n "$ONLY_SNR" ]; then
    SNR_POINTS="$ONLY_SNR"
fi
MAX_FRAMES="${MAX_FRAMES:-300}"
MIN_SRS_BINS="${MIN_SRS_BINS:-2}"
HARD_CEILING_SEC="${HARD_CEILING_SEC:-900}"
GNB_NX="${GNB_NX:-2}"
GNB_NY="${GNB_NY:-1}"
UE_NX="${UE_NX:-2}"
UE_NY="${UE_NY:-1}"
PROXY_VER="${PROXY_VER:-v7}"
NUM_UES="${NUM_UES:-1}"
DIGITAL_AGC="${DIGITAL_AGC:-0}"
P1B_NPZ="${P1B_NPZ:-../P1B_Valid_Results/Area1_7.5GHz_Rays_Valid_RXs.npz}"
UE_RX_INDICES="${UE_RX_INDICES:-98}"
CHANNEL_SEED="${CHANNEL_SEED:-}"
UE_SPEED="${UE_SPEED:-}"
DEFAULT_DIST_M="${DEFAULT_DIST_M:-100}"
ATTACH_STABLE_SEC="${ATTACH_STABLE_SEC:-300}"
ATTACH_STABLE_MODE="${ATTACH_STABLE_MODE:-auto}"
ATTACH_STABLE_TRIGGER="${ATTACH_STABLE_TRIGGER:-rrc_reconfig}"
ATTACH_STABLE_POST_DELAY="${ATTACH_STABLE_POST_DELAY:-15}"
P1B_DISTANCE_MODE="${P1B_DISTANCE_MODE:-default}"
export MIN_SRS_BINS  # inherited by launch_all.sh's internal -mf watcher

if [[ "$ATTACH_STABLE_MODE" != "auto" && "$ATTACH_STABLE_MODE" != "time" ]]; then
    echo "ERROR: ATTACH_STABLE_MODE must be auto or time (got: $ATTACH_STABLE_MODE)"
    exit 1
fi
if [[ "$ATTACH_STABLE_TRIGGER" != "rrc_reconfig" && "$ATTACH_STABLE_TRIGGER" != "srs" && "$ATTACH_STABLE_TRIGGER" != "off" ]]; then
    echo "ERROR: ATTACH_STABLE_TRIGGER must be rrc_reconfig, srs, or off (got: $ATTACH_STABLE_TRIGGER)"
    exit 1
fi
if [[ "$P1B_DISTANCE_MODE" != "default" && "$P1B_DISTANCE_MODE" != "tau" ]]; then
    echo "ERROR: P1B_DISTANCE_MODE must be default or tau (got: $P1B_DISTANCE_MODE)"
    exit 1
fi

# ── Paths ────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ_DIR="/home/dclcom61/OAI_luuuuuu/DevChannelProxyJIN"
LAUNCH_SCRIPT="${SCRIPT_DIR}/launch_all_v7.sh"
LOGS_ROOT="${PROJ_DIR}/logs"
LATEST_LINK="${LOGS_ROOT}/latest"

SWEEP_STAMP="$(date +%Y%m%d_%H%M%S)"
SWEEP_ROOT="${SWEEP_ROOT:-${LOGS_ROOT}/q4_sweep_${SWEEP_STAMP}}"
MANIFEST="${SWEEP_ROOT}/sweep_manifest.txt"
mkdir -p "$SWEEP_ROOT"

# ── Abort handling ───────────────────────────────────────────────────────
ABORT=0
CUR_SNR=""

on_sigint() {
    if [ "$ABORT" -eq 0 ]; then
        ABORT=1
        echo ""
        echo "════════════════════════════════════════════════════════════════"
        echo "[sweep] SIGINT received — aborting sweep after current point"
        echo "        (current point SNR=${CUR_SNR} dB is cleaning up now)"
        echo "        Press Ctrl+C AGAIN to hard-kill everything immediately."
        echo "════════════════════════════════════════════════════════════════"
    else
        echo ""
        echo "[sweep] Second SIGINT — hard-killing children and exiting"
        # Kill everything in our process group except self
        pkill -P $$ 2>/dev/null
        sleep 1
        pkill -9 -P $$ 2>/dev/null
        exit 130
    fi
}
trap on_sigint INT TERM

# ── Banner ───────────────────────────────────────────────────────────────
echo "========================================================================"
echo "  Q4 SNR Sweep — v7 Channel Proxy"
echo "========================================================================"
echo "  SNR points    : ${SNR_POINTS}"
[ -n "$ONLY_SNR" ] && echo "  (resume mode: ONLY_SNR=\"$ONLY_SNR\")"
echo "  Frames/point  : ${MAX_FRAMES}  (GT seq threshold = $(( (MAX_FRAMES + 99) / 100 )))"
echo "  Min SRS bins  : ${MIN_SRS_BINS}  (set 0 to disable)"
echo "  Hard ceiling  : ${HARD_CEILING_SEC} s/point"
echo "  MIMO config   : gNB ${GNB_NX}x${GNB_NY}  UE ${UE_NX}x${UE_NY}"
echo "  Proxy         : ${PROXY_VER}, num_ues=${NUM_UES}"
[ "$DIGITAL_AGC" -eq 1 ] && echo "  Digital AGC   : ON (receiver-side EWMA)"
[ -n "$P1B_NPZ" ] && echo "  P1B npz       : ${P1B_NPZ}"
[ -n "$UE_RX_INDICES" ] && echo "  UE RX index   : ${UE_RX_INDICES}"
[ -n "$CHANNEL_SEED" ] && echo "  Channel seed  : ${CHANNEL_SEED}"
[ -n "$UE_SPEED" ] && echo "  UE speed      : ${UE_SPEED} m/s"
[ -n "$DEFAULT_DIST_M" ] && echo "  Default dist  : ${DEFAULT_DIST_M} m"
[ -n "$ATTACH_STABLE_SEC" ] && echo "  Attach stable : ${ATTACH_STABLE_SEC} s (${ATTACH_STABLE_MODE})"
[ "$ATTACH_STABLE_MODE" = "auto" ] && echo "  Attach trigger: ${ATTACH_STABLE_TRIGGER}, delay=${ATTACH_STABLE_POST_DELAY}s"
[ -n "$P1B_DISTANCE_MODE" ] && echo "  P1B dist mode : ${P1B_DISTANCE_MODE}"
echo "  Sweep root    : ${SWEEP_ROOT}"
echo "========================================================================"
echo ""

# ── Manifest header (only if new) ────────────────────────────────────────
if [ ! -f "$MANIFEST" ]; then
    {
        echo "# Q4 SNR Sweep Manifest"
        echo "# timestamp: ${SWEEP_STAMP}"
        echo "# snr_points: ${SNR_POINTS}"
        echo "# max_frames: ${MAX_FRAMES}"
        echo "# min_srs_bins: ${MIN_SRS_BINS}"
        echo "# hard_ceiling_sec: ${HARD_CEILING_SEC}"
        echo "# mimo: ${GNB_NX}x${GNB_NY}_${UE_NX}x${UE_NY}"
        echo "# proxy_ver: ${PROXY_VER}"
        echo "# attach_stable_sec: ${ATTACH_STABLE_SEC}"
        echo "# attach_stable_mode: ${ATTACH_STABLE_MODE}"
        echo "# attach_stable_trigger: ${ATTACH_STABLE_TRIGGER}"
        echo "# attach_stable_post_delay: ${ATTACH_STABLE_POST_DELAY}"
        echo "# p1b_distance_mode: ${P1B_DISTANCE_MODE}"
        echo "# ----------------------------------------"
        printf "%-10s  %-10s  %-8s  %-8s  %s\n" "snr_dB" "status" "n_gt" "n_srs" "subdir"
    } > "$MANIFEST"
else
    echo "[sweep] Appending to existing manifest: $MANIFEST"
    echo "# ---- resumed ${SWEEP_STAMP} with ONLY_SNR=\"${ONLY_SNR:-ALL}\" ----" >> "$MANIFEST"
fi

# Helper: convert SNR to safe dir slug ("-10" -> "m10", "0" -> "0", "15" -> "15")
snr_to_slug() {
    local s="$1"
    if [[ "$s" == -* ]]; then
        echo "m${s#-}"
    else
        echo "$s"
    fi
}

# ── 5GC restart helper ────────────────────────────────────────────────────
CN5G_COMPOSE="/home/dclcom61/OAI_luuuuuu/DevChannelProxyJIN/openairinterface5g_whan/doc/tutorial_resources/oai-cn5g/docker-compose.yaml"
restart_5gc() {
    # Check if all 5GC containers are already healthy; skip restart if so.
    local unhealthy
    unhealthy=$(docker compose -f "$CN5G_COMPOSE" ps --format json 2>/dev/null \
        | python3 -c "import sys,json; lines=sys.stdin.read().strip().split('\n'); print(sum(1 for l in lines if l and json.loads(l).get('Health','')!='healthy'))" 2>/dev/null || echo "99")
    if [ "$unhealthy" -eq 0 ]; then
        echo "[sweep] 5GC all healthy — skipping restart"
        return
    fi
    echo "[sweep] Restarting 5GC ($unhealthy unhealthy containers)..."
    docker compose -f "$CN5G_COMPOSE" down 2>/dev/null || true
    sleep 3
    docker compose -f "$CN5G_COMPOSE" up -d 2>/dev/null || true
    echo "[sweep] Waiting 30s for 5GC (incl. MySQL) to stabilize..."
    sleep 30
    echo "[sweep] 5GC restart done."
}

# ── Main sweep loop ──────────────────────────────────────────────────────
OVERALL_START="$(date +%s)"
POINT_COUNT=0

for SNR in $SNR_POINTS; do
    # Honor abort flag BEFORE starting a new point
    if [ "$ABORT" -eq 1 ]; then
        echo "[sweep] Abort flag set — skipping SNR=${SNR} dB and exiting loop"
        break
    fi

    # Restart 5GC before every SNR point to avoid AMF SCTP stale association
    restart_5gc
    POINT_COUNT=$((POINT_COUNT + 1))

    CUR_SNR="$SNR"
    SLUG="$(snr_to_slug "$SNR")"
    TARGET_DIR="${SWEEP_ROOT}/snr_${SLUG}dB"

    # Skip-if-complete: target must already satisfy BOTH gating conditions
    # (GT seqs AND SRS bins). Any lesser state → purge and re-run, so the
    # resume path can never accept the old GT-only-complete artifacts.
    if [ -d "$TARGET_DIR/sionna_gt" ]; then
        EXIST_SEQS=$(ls "$TARGET_DIR/sionna_gt"/gt_batch_ue0_seq*.npz 2>/dev/null | wc -l)
        EXIST_SRS=$(ls "$TARGET_DIR"/srs_matrix_gNB_*.bin 2>/dev/null | wc -l)
        REQ_SEQS=$(( (MAX_FRAMES + 99) / 100 ))
        if [ "$EXIST_SEQS" -ge "$REQ_SEQS" ] && [ "$EXIST_SRS" -ge "$MIN_SRS_BINS" ]; then
            echo ""
            echo "[sweep] SNR=${SNR} dB already complete (gt=${EXIST_SEQS} srs=${EXIST_SRS}) in ${TARGET_DIR} — skipping"
            printf "%-10s  %-10s  %-8s  %-8s  %s\n" "$SNR" "SKIP-EXIST" "$EXIST_SEQS" "$EXIST_SRS" \
                "$(basename "$TARGET_DIR")" >> "$MANIFEST"
            continue
        fi
        echo "[sweep] SNR=${SNR} dB has partial data (gt=${EXIST_SEQS} srs=${EXIST_SRS}) — removing and re-running"
        rm -rf "$TARGET_DIR"
    fi

    echo ""
    echo "################################################################"
    echo "#  SNR = ${SNR} dB   →   ${TARGET_DIR}"
    echo "################################################################"
    POINT_START="$(date +%s)"

    # Remember the previous logs/latest target so the external watcher
    # only counts files from the NEW run, not the previous point.
    PREV_LATEST=""
    if [ -L "$LATEST_LINK" ]; then
        PREV_LATEST="$(readlink -f "$LATEST_LINK" 2>/dev/null || echo "")"
    fi

    # Start launch_all in background so we can supervise it.
    # Keep launch_all's internal -mf watcher as a 2nd line of defense.
    AGC_FLAG=""
    [ "$DIGITAL_AGC" -eq 1 ] && AGC_FLAG="-agc"
    P1B_FLAG=""
    [ -n "$P1B_NPZ" ] && P1B_FLAG="-p1b $P1B_NPZ"
    RX_FLAG=""
    [ -n "$UE_RX_INDICES" ] && RX_FLAG="-rx $UE_RX_INDICES"
    SEED_FLAG=""
    [ -n "$CHANNEL_SEED" ] && SEED_FLAG="-seed $CHANNEL_SEED"
    SPEED_FLAG=""
    [ -n "$UE_SPEED" ] && SPEED_FLAG="-speed $UE_SPEED"
    DIST_FLAG=""
    [ -n "$DEFAULT_DIST_M" ] && DIST_FLAG="-dist $DEFAULT_DIST_M"
    STABLE_FLAG=""
    [ -n "$ATTACH_STABLE_SEC" ] && STABLE_FLAG="-stable $ATTACH_STABLE_SEC"
    STABLE_MODE_FLAG=""
    [ -n "$ATTACH_STABLE_MODE" ] && STABLE_MODE_FLAG="-stable-mode $ATTACH_STABLE_MODE"
    STABLE_TRIGGER_FLAG=""
    [ -n "$ATTACH_STABLE_TRIGGER" ] && STABLE_TRIGGER_FLAG="-stable-trigger $ATTACH_STABLE_TRIGGER"
    STABLE_DELAY_FLAG=""
    [ -n "$ATTACH_STABLE_POST_DELAY" ] && STABLE_DELAY_FLAG="-stable-delay $ATTACH_STABLE_POST_DELAY"
    P1BDIST_FLAG=""
    [ -n "$P1B_DISTANCE_MODE" ] && P1BDIST_FLAG="-p1bdist $P1B_DISTANCE_MODE"
    bash "$LAUNCH_SCRIPT" \
        -v  "$PROXY_VER" \
        -n  "$NUM_UES" \
        -ga "$GNB_NX" "$GNB_NY" \
        -ua "$UE_NX"  "$UE_NY" \
        -snr "$SNR" \
        -mf  "$MAX_FRAMES" \
        $P1B_FLAG $RX_FLAG $AGC_FLAG $SEED_FLAG $SPEED_FLAG $DIST_FLAG \
        $STABLE_FLAG $STABLE_MODE_FLAG $STABLE_TRIGGER_FLAG $STABLE_DELAY_FLAG $P1BDIST_FLAG &
    LAUNCH_PID=$!

    # External frame-watcher (runs in wrapper, not in launch_all).
    # Polls the new run's GT dir AND SRS bin files and sends SIGTERM to
    # launch_all when BOTH thresholds are met. Also enforces a hard
    # wall-clock ceiling so a point that can never satisfy (e.g. UE
    # crashed, SRS never scheduled) still terminates.
    MAX_SEQS=$(( (MAX_FRAMES + 99) / 100 ))
    echo "[sweep-watcher] armed: gt≥${MAX_SEQS} ∧ srs≥${MIN_SRS_BINS}, ceiling=${HARD_CEILING_SEC}s, launch_pid=${LAUNCH_PID}"
    (
        # Give launch_all time to refresh logs/latest and create GT dir
        sleep 8
        stop_reason=""
        start_ts=$(date +%s)
        while kill -0 "$LAUNCH_PID" 2>/dev/null; do
            sleep 3
            # Only count if logs/latest has been updated for THIS run
            if [ -L "$LATEST_LINK" ]; then
                cur="$(readlink -f "$LATEST_LINK" 2>/dev/null || echo "")"
                if [ -n "$cur" ] && [ "$cur" != "$PREV_LATEST" ]; then
                    # Glob via -L so we follow sionna_gt (symlink → /tmp/...)
                    n_gt=$(ls -L "$cur/sionna_gt"/gt_batch_ue0_seq*.npz 2>/dev/null | wc -l)
                    # SRS bin files land directly in the run dir (gNB cwd)
                    n_srs=$(ls -L "$cur"/srs_matrix_gNB_*.bin 2>/dev/null | wc -l)
                    if [ "$n_gt" -ge "$MAX_SEQS" ] && [ "$n_srs" -ge "$MIN_SRS_BINS" ]; then
                        stop_reason="threshold(gt=${n_gt}≥${MAX_SEQS} srs=${n_srs}≥${MIN_SRS_BINS})"
                        break
                    fi
                fi
            fi
            # Hard wall-clock ceiling
            now=$(date +%s)
            if [ $((now - start_ts)) -ge "$HARD_CEILING_SEC" ]; then
                stop_reason="timeout(${HARD_CEILING_SEC}s)"
                break
            fi
        done
        if [ -n "$stop_reason" ] && kill -0 "$LAUNCH_PID" 2>/dev/null; then
            echo ""
            echo "[sweep-watcher] stop_reason=${stop_reason} — SIGTERM → launch_all(PID=$LAUNCH_PID)"
            # NOTE: cannot use SIGINT here. When we started launch_all with
            # `bash ... &` in a non-interactive script (no job control), bash
            # force-sets the child's SIGINT to SIG_IGN, and per bash rules an
            # ignored-on-entry signal CANNOT be re-trapped — so
            # `trap cleanup SIGINT` inside launch_all.sh silently does nothing
            # and the child would ignore our kill -INT entirely.
            # SIGTERM is not in that ignored set, so launch_all's
            # `trap cleanup SIGINT SIGTERM` fires on TERM as expected.
            # NOTE 2: launch_all's cleanup() now handles SRS partial-flush
            # by SIGINT-ing nr-softmodem FIRST and waiting up to ~12s for
            # `[SRS Dump] Writer thread exited` before SIGKILL. DO NOT
            # blanket-TERM launch_all's direct children here — that would
            # racily nuke nr-softmodem's subshell before the flush runs.
            # The single SIGTERM below is enough: launch_all's own trap
            # fires cleanup(), which drives the ordered shutdown.
            kill -TERM "$LAUNCH_PID" 2>/dev/null || true
        fi
    ) &
    WATCHER_PID=$!

    # Wait for launch_all to finish (either natural end, watcher SIGINT,
    # launch_all's own -mf watcher, or user Ctrl+C via on_sigint trap).
    wait "$LAUNCH_PID"
    RC=$?

    # Tear down the watcher
    if kill -0 "$WATCHER_PID" 2>/dev/null; then
        kill "$WATCHER_PID" 2>/dev/null || true
        wait "$WATCHER_PID" 2>/dev/null || true
    fi

    # After cleanup(), logs/latest points at this run's log dir
    if [ ! -L "$LATEST_LINK" ]; then
        echo "[sweep] ERROR: logs/latest symlink missing after SNR=${SNR}"
        printf "%-10s  %-10s  %-8s  %-8s  %s\n" "$SNR" "FAIL-NOLINK" "0" "0" "-" >> "$MANIFEST"
        continue
    fi
    LATEST_RUN="$(readlink -f "$LATEST_LINK")"

    # Count GT seqs and SRS bin files collected
    N_SEQS=0
    N_SRS=0
    if [ -d "${LATEST_RUN}/sionna_gt" ]; then
        N_SEQS=$(ls "${LATEST_RUN}/sionna_gt"/gt_batch_ue0_seq*.npz 2>/dev/null | wc -l)
    fi
    N_SRS=$(ls "${LATEST_RUN}"/srs_matrix_gNB_*.bin 2>/dev/null | wc -l)

    # Move run dir into sweep root (rename for clarity)
    if mv "$LATEST_RUN" "$TARGET_DIR" 2>/dev/null; then
        ln -sfn "$TARGET_DIR" "$LATEST_LINK"
        if [ "$ABORT" -eq 1 ]; then
            STATUS="ABORTED"
        elif [ "$N_SRS" -lt "$MIN_SRS_BINS" ]; then
            # launch_all exited without hitting BOTH thresholds → watcher
            # timed out or process died early. Mark distinctly so the
            # analyzer can notice.
            STATUS="PARTIAL"
        else
            STATUS="OK"
        fi
    else
        echo "[sweep] ERROR: mv failed: $LATEST_RUN → $TARGET_DIR"
        STATUS="FAIL-MOVE"
        TARGET_DIR="$LATEST_RUN"
    fi

    POINT_END="$(date +%s)"
    DUR=$(( POINT_END - POINT_START ))
    echo "[sweep] SNR=${SNR} dB  status=${STATUS}  gt=${N_SEQS}  srs=${N_SRS}  dur=${DUR}s"
    printf "%-10s  %-10s  %-8s  %-8s  %s\n" "$SNR" "$STATUS" "$N_SEQS" "$N_SRS" \
        "$(basename "$TARGET_DIR")" >> "$MANIFEST"

    # Small pause between runs to let Docker/GPU settle
    [ "$ABORT" -eq 0 ] && sleep 3
done

OVERALL_END="$(date +%s)"
TOTAL_DUR=$(( OVERALL_END - OVERALL_START ))
echo ""
echo "========================================================================"
if [ "$ABORT" -eq 1 ]; then
    echo "  Sweep ABORTED after ${TOTAL_DUR}s"
else
    echo "  Sweep complete — ${TOTAL_DUR}s total wall-clock"
fi
echo "  Manifest: ${MANIFEST}"
echo "========================================================================"
cat "$MANIFEST"
echo ""
if [ "$ABORT" -eq 0 ]; then
    echo "Next step: run the offline analyzer"
    echo "  python3 ${SCRIPT_DIR}/q4_convergence_sweep.py \\"
    echo "      --sweep-dir ${SWEEP_ROOT} \\"
    echo "      --plot-dir  ${PROJ_DIR}/vRAN_Socket/data_out"
fi
