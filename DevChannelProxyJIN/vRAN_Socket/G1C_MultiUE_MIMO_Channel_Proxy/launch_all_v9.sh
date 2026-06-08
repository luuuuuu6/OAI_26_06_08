#!/bin/bash
#
# G1C 통합 런처 — v9 Channel Proxy (v8 + attach diagnostics)
#
# v7 대비 주요 변경:
#   - GT save 의 GPU→CPU 복사를 비동기 stream + pinned pool 로 처리
#   - record_ul_slot 의 staging copy 를 release_batch 이전에 수행 (P0 fix)
#   - --gt-async-copy / --gt-pinned-pool-size / --gt-staging-gpu-buffers 추가
#   - 결과: --gt-save-every=1 에서도 UL pipeline 이 막히지 않음
#
# v4→v7 누적 변경 (참고):
#   - carrier_frequency 3.5e9 Hz, sample_times int64 누적 (v6)
#   - attach-stable window (v7)
#
# 사용법:
#   sudo bash launch_all_v8.sh [옵션]
#
# 옵션:
#   -v VERSION   Proxy 버전                         (기본: v8)
#   -m MODE      통신 모드: socket, gpu-ipc          (기본: gpu-ipc)
#   -n NUM_UES   UE 수: 1 이상                      (기본: 1)
#   -pl dB       경로 손실                           (기본: 0)
#   -snr dB      AWGN 상대 SNR                      (기본: off)
#   -nf dBFS     AWGN 절대 noise floor              (기본: off, 예: -40)
#   -ulg G       UL pre-gain before gNB int16 RX    (기본: 1.0, 예: 4.0)
#   -d SEC       실행 시간 (초)                      (기본: Ctrl+C까지)
#   -mf N        N 프레임 수집 후 자동 종료            (기본: off, 예: 1000)
#   -b           Sionna 채널 바이패스 (IQ 패스스루)
#   -ga Nx Ny    gNB 안테나 (가로 x 세로)            (기본: 1 1)
#   -ua Nx Ny    UE 안테나 (가로 x 세로)             (기본: 1 1)
#   -bs SIZE     채널 생성 배치 크기                  (기본: 2100)
#   -bl LEN      채널 링버퍼 길이                     (기본: 42000)
#   -p1b PATH    P1B npz 파일 경로 (v3/v4, UE별 독립 ray)
#   -rx INDICES  UE RX 인덱스 (콤마구분 또는 "random")
#   -dist METERS BS-UE 기본 거리                    (기본: 100)
#   -stable SEC  attach 안정화 시간                  (기본: 45, 0=off)
#   -p1bdist MODE P1B 거리 모드: default,tau          (기본: default)
#   -h           도움말
#
# 예시:
#   sudo bash launch_all_v8.sh                             # v8, gpu-ipc, 1 UE, SISO
#   sudo bash launch_all_v8.sh -n 1 -ga 2 1 -ua 2 1       # v8, 2x1 MIMO
#   sudo bash launch_all_v8.sh -snr 10 -speed 5 -stable 45 # SNR=10dB, stable attach
#
# 로그 파일:
#   ~/DevChannelProxyJIN/logs/YYYYMMDD_HHMMSS_G1C_v8_ipc_1ue_ga2x1_ua2x1/
#   ~/DevChannelProxyJIN/logs/latest → 최근 실행 심링크
#
# 종료:
#   Ctrl+C → 전체 프로세스 자동 종료
#

set -euo pipefail

# ── 기본값 ────────────────────────────────────────────────────────
# 모든 값은 환경변수로 오버라이드 가능 (sweep 스크립트와 동일한 생산 기본값)
PROXY_VER="${PROXY_VER:-v8}"
MODE="${MODE:-gpu-ipc}"
NUM_UES="${NUM_UES:-1}"
BYPASS_CHANNEL=0
PATH_LOSS_DB=""
SNR_DB=""
NOISE_DBFS=""
UL_PRE_GAIN="${UL_PRE_GAIN:-1.0}"
DURATION=""
GNB_NX="${GNB_NX:-2}"
GNB_NY="${GNB_NY:-1}"
UE_NX="${UE_NX:-2}"
UE_NY="${UE_NY:-1}"
BUF_SYM_SIZE="${BUF_SYM_SIZE:-2100}"
BUF_LEN="${BUF_LEN:-42000}"
P1B_NPZ="${P1B_NPZ-../P1B_Valid_Results/Area1_7.5GHz_Rays_Valid_RXs.npz}"
UE_RX_INDICES="${UE_RX_INDICES-98}"
NPY_DIR="${NPY_DIR:-}"
USE_XLA=0
GPU_IDX="${GPU_IDX:-0}"
GT_SAVE_EVERY="${GT_SAVE_EVERY:-10}"
GT_SYMBOLS="${GT_SYMBOLS:-12}"
# SRS-only CDL: full multipath only on SRS symbol(s), flat elsewhere (reliable attach)
SRS_ONLY_CHANNEL="${SRS_ONLY_CHANNEL:-0}"
SRS_SYMBOLS="${SRS_SYMBOLS:-12}"
SRS_FLAT_MODE="${SRS_FLAT_MODE:-power}"
# Under SRS-only: flatten the whole DL (every symbol) so PBCH/PDSCH/RRC-DL decode
# reliably -> robust attach. DL never needs CDL (only UL SRS does). Default on.
SRS_ONLY_DL_FLAT="${SRS_ONLY_DL_FLAT:-1}"
# v8: async GT D2H pipeline knobs (overridable from env)
GT_ASYNC_COPY="${GT_ASYNC_COPY:-on}"            # on|off
GT_PINNED_POOL_SIZE="${GT_PINNED_POOL_SIZE:-32}"
GT_STAGING_GPU_BUFFERS="${GT_STAGING_GPU_BUFFERS:-32}"
MAX_FRAMES="${MAX_FRAMES:-}"
DIGITAL_AGC="${DIGITAL_AGC:-0}"
CHANNEL_SEED="${CHANNEL_SEED:-}"
UE_SPEED="${UE_SPEED:-}"
DEFAULT_DIST_M="${DEFAULT_DIST_M:-100}"
ATTACH_STABLE_SEC="${ATTACH_STABLE_SEC:-300}"
ATTACH_STABLE_MODE="${ATTACH_STABLE_MODE:-auto}"
ATTACH_STABLE_TRIGGER="${ATTACH_STABLE_TRIGGER:-rrc_reconfig}"
ATTACH_STABLE_POST_DELAY="${ATTACH_STABLE_POST_DELAY:-15}"
ATTACH_STABLE_TRIGGER_FILE="${ATTACH_STABLE_TRIGGER_FILE:-/tmp/oai_gpu_ipc/v8_dynamic_enable}"
P1B_DISTANCE_MODE="${P1B_DISTANCE_MODE:-default}"
# SRS/gNB 환경변수 (nr_ul_channel_estimation.c 에서 읽음)
SRS_ESTIMATOR="${SRS_ESTIMATOR:-legacy}"
SRS_OPTFILT="${SRS_OPTFILT:-0}"
SRS_REF_DUMP_PATH="${SRS_REF_DUMP_PATH:-/tmp/oai_gpu_ipc/srs_ref.bin}"
SRS_PERIOD_SLOTS="${SRS_PERIOD_SLOTS:-10}"
# SRS 2D filter env vars (nr_srs_2d_filter.c — EWMA/IBVSS/Kalman)
SRS_2D_METHOD="${SRS_2D_METHOD:-}"
SRS_2D_DEBUG="${SRS_2D_DEBUG:-}"

# ── 인자 파싱 ─────────────────────────────────────────────────────
usage() {
    echo "G1C 통합 런처 — v8 Channel Proxy (async GT D2H pipeline)"
    echo ""
    echo "사용법: sudo bash launch_all_v8.sh [옵션]"
    echo ""
    echo "옵션:"
    echo "  -v VERSION   Proxy 버전                  (기본: v8)"
    echo "  -m MODE      모드: socket, gpu-ipc       (기본: gpu-ipc)"
    echo "  -n NUM_UES   UE 수: 1 이상              (기본: 1)"
    echo "  -pl dB       경로 손실 (기본: 0, 예: 3)"
    echo "  -snr dB      AWGN 상대 SNR (기본: off, 예: 30)"
    echo "  -nf dBFS     AWGN 절대 noise floor (기본: off, 예: -40)"
    echo "  -ulg G       UL pre-gain before gNB int16 RX (기본: 1.0, 예: 4.0)"
    echo "  -d SEC       실행 시간 (초, 기본: Ctrl+C까지)"
    echo "  -b           Sionna 채널 바이패스 (IQ 패스스루)"
    echo "  -ga Nx Ny    gNB 안테나 배열 (기본: 1 1 = SISO)"
    echo "  -ua Nx Ny    UE 안테나 배열 (기본: 1 1 = SISO)"
    echo "  -bs SIZE     채널 생성 배치 크기 (buffer-symbol-size, 기본: 2100)"
    echo "  -bl LEN      채널 링버퍼 길이 (buffer-len, 기본: 42000)"
    echo "  -p1b PATH    P1B npz 파일 경로 (UE별 독립 ray)"
    echo "  -rx INDICES  UE RX 인덱스 (콤마 구분 또는 'random', 미지정 시 auto random)"
    echo "  -dist METERS BS-UE 기본 거리 (기본: 100)"
    echo "  -stable SEC  attach 안정화 최대 시간 (기본: 300, 0=off)"
    echo "  -stable-mode MODE attach 안정화 모드: auto,time (기본: auto)"
    echo "  -stable-trigger EVENT auto trigger: rrc_reconfig,srs,off (기본: rrc_reconfig)"
    echo "  -stable-delay SEC trigger 이후 dynamic handoff 지연 (기본: 15)"
    echo "  -p1bdist MODE P1B 거리 모드: default,tau (기본: default)"
    echo "  -speed M/S   UE 이동 속도 (기본: v8 내장 3 m/s)"
    echo "  -gpu IDX     사용할 GPU 인덱스 (기본: 0)"
    echo "  --xla        XLA JIT 컴파일 활성화 (커널 퓨전 최적화)"
    echo "  -mf N        N 프레임 수집 후 자동 종료 (GT+SRS 게이트, 기본 SRS 프레임=N)"
    echo "  env SRS_PERIOD_SLOTS=N  SRS 주기 override (기본: 10, OAI 원래 자동값은 보통 160)"
    echo "  -h           도움말"
    echo ""
    echo "버전 설명:"
    echo "  v7  v6 + attach-stable window + configurable P1B distance mode"
    echo "  v8  v7 + async GT D2H pipeline (--gt-async-copy / pinned pool)"
    echo ""
    echo "예시:"
    echo "  sudo bash launch_all_v8.sh                              # v8, 1 UE, SISO"
    echo "  sudo bash launch_all_v8.sh -ga 2 1 -ua 2 1 -snr 10     # 2x1 MIMO, SNR=10"
    echo "  sudo bash launch_all_v8.sh -speed 5 -stable 300         # auto stable attach"
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -v)
            PROXY_VER="$2"
            if [[ ! "$PROXY_VER" =~ ^v[0-9]+$ ]]; then
                echo "ERROR: -v 는 v0 이상이어야 합니다 (입력: $PROXY_VER)"
                exit 1
            fi
            shift 2 ;;
        -m)
            MODE="$2"
            if [[ "$MODE" != "socket" && "$MODE" != "gpu-ipc" ]]; then
                echo "ERROR: -m 은 socket 또는 gpu-ipc 여야 합니다 (입력: $MODE)"
                exit 1
            fi
            shift 2 ;;
        -n)
            NUM_UES="$2"
            if ! [[ "$NUM_UES" =~ ^[0-9]+$ ]] || [ "$NUM_UES" -lt 1 ]; then
                echo "ERROR: -n 은 1 이상 정수여야 합니다 (입력: $NUM_UES)"
                exit 1
            fi
            shift 2 ;;
        -pl)
            _pl_raw="$2"
            _pl_abs="${_pl_raw#-}"
            PATH_LOSS_DB="-${_pl_abs}"
            shift 2 ;;
        -snr)
            SNR_DB="$2"
            shift 2 ;;
        -nf)
            NOISE_DBFS="$2"
            shift 2 ;;
        -ulg)
            UL_PRE_GAIN="$2"
            shift 2 ;;
        -d)
            DURATION="$2"
            shift 2 ;;
        -b) BYPASS_CHANNEL=1; shift ;;
        -ga)
            GNB_NX="$2"; GNB_NY="$3"
            shift 3 ;;
        -ua)
            UE_NX="$2"; UE_NY="$3"
            shift 3 ;;
        -bs)
            BUF_SYM_SIZE="$2"
            shift 2 ;;
        -bl)
            BUF_LEN="$2"
            shift 2 ;;
        -p1b)
            P1B_NPZ="$2"
            shift 2 ;;
        -rx)
            UE_RX_INDICES="$2"
            shift 2 ;;
        -gpu)
            GPU_IDX="$2"
            if ! [[ "$GPU_IDX" =~ ^[0-9]+$ ]]; then
                echo "ERROR: -gpu 는 0 이상 정수여야 합니다 (입력: $GPU_IDX)"
                exit 1
            fi
            shift 2 ;;
        --xla) USE_XLA=1; shift ;;
        -agc) DIGITAL_AGC=1; shift ;;
        -gse)
            GT_SAVE_EVERY="$2"
            shift 2 ;;
        -mf)
            MAX_FRAMES="$2"
            if ! [[ "$MAX_FRAMES" =~ ^[0-9]+$ ]]; then
                echo "ERROR: -mf 는 양의 정수여야 합니다 (입력: $MAX_FRAMES)"
                exit 1
            fi
            shift 2 ;;
        -seed)
            CHANNEL_SEED="$2"
            shift 2 ;;
        -speed)
            UE_SPEED="$2"
            shift 2 ;;
        -dist)
            DEFAULT_DIST_M="$2"
            shift 2 ;;
        -stable)
            ATTACH_STABLE_SEC="$2"
            shift 2 ;;
        -stable-mode)
            ATTACH_STABLE_MODE="$2"
            if [[ "$ATTACH_STABLE_MODE" != "auto" && "$ATTACH_STABLE_MODE" != "time" ]]; then
                echo "ERROR: -stable-mode 는 auto 또는 time 이어야 합니다 (입력: $ATTACH_STABLE_MODE)"
                exit 1
            fi
            shift 2 ;;
        -stable-trigger)
            ATTACH_STABLE_TRIGGER="$2"
            if [[ "$ATTACH_STABLE_TRIGGER" != "rrc_reconfig" && "$ATTACH_STABLE_TRIGGER" != "srs" && "$ATTACH_STABLE_TRIGGER" != "off" ]]; then
                echo "ERROR: -stable-trigger 는 rrc_reconfig, srs 또는 off 여야 합니다 (입력: $ATTACH_STABLE_TRIGGER)"
                exit 1
            fi
            shift 2 ;;
        -stable-delay)
            ATTACH_STABLE_POST_DELAY="$2"
            shift 2 ;;
        -p1bdist)
            P1B_DISTANCE_MODE="$2"
            if [[ "$P1B_DISTANCE_MODE" != "default" && "$P1B_DISTANCE_MODE" != "tau" ]]; then
                echo "ERROR: -p1bdist 는 default 또는 tau 여야 합니다 (입력: $P1B_DISTANCE_MODE)"
                exit 1
            fi
            shift 2 ;;
        -h) usage ;;
        *) echo "ERROR: 알 수 없는 옵션: $1"; usage ;;
    esac
done

# ── 상호 배타 체크 ────────────────────────────────────────────────
if [ -n "$SNR_DB" ] && [ -n "$NOISE_DBFS" ]; then
    echo "ERROR: -snr 과 -nf 는 동시에 사용할 수 없습니다."
    exit 1
fi
if ! python3 - "$UL_PRE_GAIN" <<'PY'
import sys
try:
    value = float(sys.argv[1])
except ValueError:
    sys.exit(1)
sys.exit(0 if value > 0.0 else 1)
PY
then
    echo "ERROR: -ulg/UL_PRE_GAIN must be a positive number (input: $UL_PRE_GAIN)"
    exit 1
fi
if ! [[ "$SRS_PERIOD_SLOTS" =~ ^[0-9]+$ ]] || [ "$SRS_PERIOD_SLOTS" -lt 1 ] || [ "$SRS_PERIOD_SLOTS" -ge 2560 ]; then
    echo "ERROR: SRS_PERIOD_SLOTS must be an integer in [1,2559] (input: $SRS_PERIOD_SLOTS)"
    exit 1
fi
if [[ "$ATTACH_STABLE_MODE" != "auto" && "$ATTACH_STABLE_MODE" != "time" ]]; then
    echo "ERROR: ATTACH_STABLE_MODE must be auto or time (got: $ATTACH_STABLE_MODE)"
    exit 1
fi
if [[ "$ATTACH_STABLE_TRIGGER" != "rrc_reconfig" && "$ATTACH_STABLE_TRIGGER" != "srs" && "$ATTACH_STABLE_TRIGGER" != "off" ]]; then
    echo "ERROR: ATTACH_STABLE_TRIGGER must be rrc_reconfig, srs, or off (got: $ATTACH_STABLE_TRIGGER)"
    exit 1
fi

echo "[launcher] GPU 인덱스: ${GPU_IDX}"

# ── 안테나 계산 ───────────────────────────────────────────────────
GNB_ANT=$((GNB_NX * GNB_NY))
UE_ANT=$((UE_NX * UE_NY))
MAX_ANT=$((GNB_ANT > UE_ANT ? GNB_ANT : UE_ANT))

PROXY_SCRIPT="${PROXY_VER}.py"

# ── 경로 설정 ─────────────────────────────────────────────────────
PROJ_DIR="/home/dclcom61/OAI_luuuuuu/DevChannelProxyJIN"
BUILD_DIR="$PROJ_DIR/openairinterface5g_whan/cmake_targets/ran_build/build"
export LD_LIBRARY_PATH="$BUILD_DIR:${LD_LIBRARY_PATH:-}"
CONF="$PROJ_DIR/openairinterface5g_whan/targets/PROJECTS/GENERIC-NR-5GC/CONF/gnb.sa.band78.fr1.106PRB.usrpb210.conf"
CONF_DIR="$(dirname "$CONF")"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
MODE_SHORT_LOG="ipc"
[ "$MODE" = "socket" ] && MODE_SHORT_LOG="sock"
ANT_TAG=""
[ "$GNB_ANT" -gt 1 ] && ANT_TAG="${ANT_TAG}_ga${GNB_NX}x${GNB_NY}"
[ "$UE_ANT" -gt 1 ] && ANT_TAG="${ANT_TAG}_ua${UE_NX}x${UE_NY}"
LOG_TAG_DIR="G1C_${PROXY_VER}_${MODE_SHORT_LOG}_${NUM_UES}ue${ANT_TAG}"
[ -n "$PATH_LOSS_DB" ] && LOG_TAG_DIR="${LOG_TAG_DIR}_pl${PATH_LOSS_DB#-}"
[ -n "$SNR_DB" ] && LOG_TAG_DIR="${LOG_TAG_DIR}_snr${SNR_DB}"
[ -n "$NOISE_DBFS" ] && LOG_TAG_DIR="${LOG_TAG_DIR}_nf${NOISE_DBFS}"
if [ "$UL_PRE_GAIN" != "1.0" ] && [ "$UL_PRE_GAIN" != "1" ]; then
    LOG_TAG_DIR="${LOG_TAG_DIR}_ulg${UL_PRE_GAIN}"
fi
[ "$USE_XLA" -eq 1 ] && LOG_TAG_DIR="${LOG_TAG_DIR}_xla"
LOG_DIR="$PROJ_DIR/logs/${TIMESTAMP}_${LOG_TAG_DIR}"
mkdir -p "$LOG_DIR"
ln -sfn "$LOG_DIR" "$PROJ_DIR/logs/latest"

GT_DIR_BASE="${GT_DIR_BASE:-/tmp/oai_gpu_ipc/sionna_gt}"
mkdir -p "$GT_DIR_BASE"
chmod 777 "$GT_DIR_BASE" 2>/dev/null || true
GT_DIR="$GT_DIR_BASE/${TIMESTAMP}_${LOG_TAG_DIR}"
mkdir -p "$GT_DIR"
ln -sfn "$GT_DIR" "$LOG_DIR/sionna_gt"

PIDS=()

# Sum SRS frames by reading bin headers (n_frames field).
sum_srs_frames() {
    local run_dir="$1"
    python3 - "$run_dir" <<'PY'
import glob
import os
import struct
import sys

run_dir = sys.argv[1]
total = 0
for fp in sorted(glob.glob(os.path.join(run_dir, "srs_matrix_gNB_*_seq*.bin"))):
    try:
        with open(fp, "rb") as f:
            hdr = f.read(24)
        if len(hdr) >= 24:
            vals = struct.unpack("<6I", hdr[:24])
            total += int(vals[5])  # n_frames in this bin
    except Exception:
        pass
print(total)
PY
}

cleanup() {
    echo ""
    echo "[launcher] Ctrl+C 감지 — 전체 프로세스 종료 중..."

    # 1) Graceful SIGINT to gNB/UE FIRST so OAI runs its full shutdown path
    #    (phy_free_nr_gNB → free_srs_digital_twin_system → partial flush +
    #     writer summary). Without this the SRS pingpong buffer is lost when
    #    MAX_DUMP_FRAMES(=100) is not reached before termination.
    pkill -INT -f "nr-softmodem"   2>/dev/null || true
    pkill -INT -f "nr-uesoftmodem" 2>/dev/null || true

    # 2) Proxy also gets SIGTERM in parallel (its own graceful path)
    docker exec sionna-proxy pkill -TERM -f "v[0-9]\.py" 2>/dev/null || true

    # 3) Wait up to ~12s for gNB to finish SRS partial-flush & writer exit.
    #    Poll for the tell-tale log line, or the process disappearing.
    #    (Typical clean-exit window observed: 1–3s; 12s is comfortable ceiling.)
    SRS_FLUSH_TIMEOUT="${SRS_FLUSH_TIMEOUT:-12}"
    echo "[launcher] gNB graceful shutdown 대기 (SRS partial-flush, 최대 ${SRS_FLUSH_TIMEOUT}s)..."
    for _i in $(seq 1 "$SRS_FLUSH_TIMEOUT"); do
        if ! pgrep -f "nr-softmodem" >/dev/null 2>&1; then
            break
        fi
        if [ -f "$LOG_DIR/gnb.log" ] && \
           grep -q "\[SRS Dump\] Writer thread exited" "$LOG_DIR/gnb.log" 2>/dev/null; then
            # Give the process a beat to finish itti teardown after the flush
            sleep 1
            break
        fi
        sleep 1
    done

    # 4) Hard-kill anything still alive (failsafe)
    docker exec sionna-proxy pkill -9 -f "v[0-9]\.py"      2>/dev/null || true
    docker exec sionna-proxy pkill -9 -f "multiprocessing" 2>/dev/null || true
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
    pkill -9 -f "nr-softmodem"   2>/dev/null || true
    pkill -9 -f "nr-uesoftmodem" 2>/dev/null || true

    # 5) GT 데이터를 /tmp에서 로그 디렉토리로 이동 (심링크 → 실제 데이터)
    if [ -L "$LOG_DIR/sionna_gt" ] && [ -d "$GT_DIR" ]; then
        rm -f "$LOG_DIR/sionna_gt"
        mv "$GT_DIR" "$LOG_DIR/sionna_gt" 2>/dev/null && \
            echo "[launcher] GT 데이터 이동 완료: /tmp → $LOG_DIR/sionna_gt" || \
            echo "[launcher] GT 데이터 이동 실패 (수동으로 이동 필요)"
    fi

    echo "[launcher] 종료 완료. 로그: $LOG_DIR/"
    echo "[launcher] 바로가기: $PROJ_DIR/logs/latest/"
    exit 0
}

trap cleanup SIGINT SIGTERM

start_attach_stable_watcher() {
    if [ "$ATTACH_STABLE_MODE" != "auto" ] || [ "$ATTACH_STABLE_TRIGGER" = "off" ] || [ "${ATTACH_STABLE_SEC:-0}" = "0" ]; then
        echo "[launcher] AttachStable watcher disabled (mode=${ATTACH_STABLE_MODE}, trigger=${ATTACH_STABLE_TRIGGER})"
        return
    fi

    local pattern=""
    case "$ATTACH_STABLE_TRIGGER" in
        rrc_reconfig)
            pattern="Received RRCReconfigurationComplete"
            ;;
        srs)
            pattern="SRS 2D-MMSE|\\[SRS Dump\\] Written|\\[SRS Twin\\]"
            ;;
    esac

    echo "[launcher] AttachStable watcher armed: trigger=${ATTACH_STABLE_TRIGGER}, delay=${ATTACH_STABLE_POST_DELAY}s"
    (
        echo "[attach-stable-watcher] waiting for pattern: ${pattern}"
        while true; do
            if grep -Eq "$pattern" "$LOG_DIR/gnb.log" 2>/dev/null; then
                echo "[attach-stable-watcher] trigger matched (${ATTACH_STABLE_TRIGGER}); sleeping ${ATTACH_STABLE_POST_DELAY}s"
                sleep "$ATTACH_STABLE_POST_DELAY"
                {
                    echo "trigger=${ATTACH_STABLE_TRIGGER}"
                    echo "matched_at=$(date +%s)"
                    echo "post_delay=${ATTACH_STABLE_POST_DELAY}"
                } > "$ATTACH_STABLE_TRIGGER_FILE"
                chmod 666 "$ATTACH_STABLE_TRIGGER_FILE" 2>/dev/null || true
                docker exec sionna-proxy sh -c "test -e '$ATTACH_STABLE_TRIGGER_FILE' || echo trigger=${ATTACH_STABLE_TRIGGER} > '$ATTACH_STABLE_TRIGGER_FILE'" 2>/dev/null || true
                echo "[attach-stable-watcher] trigger file written: ${ATTACH_STABLE_TRIGGER_FILE}"
                break
            fi
            sleep 1
        done
    ) >> "$LOG_DIR/attach_stable_watcher.log" 2>&1 &
    PIDS+=($!)
}

PROXY_EXTRA_ARGS=""
if [ "$BYPASS_CHANNEL" -eq 1 ]; then
    PROXY_EXTRA_ARGS="--no-custom-channel"
fi
if [ -n "$PATH_LOSS_DB" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --path-loss-dB $PATH_LOSS_DB"
fi
if [ -n "$SNR_DB" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --snr-dB $SNR_DB"
fi
if [ -n "$NOISE_DBFS" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --noise-dBFS $NOISE_DBFS"
fi
if [ -n "$UL_PRE_GAIN" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --ul-pre-gain $UL_PRE_GAIN"
fi
if [ -n "$BUF_SYM_SIZE" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --buffer-symbol-size $BUF_SYM_SIZE"
fi
if [ -n "$BUF_LEN" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --buffer-len $BUF_LEN"
fi
if [ -n "$P1B_NPZ" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --p1b-npz $P1B_NPZ"
fi
if [ -n "$UE_RX_INDICES" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --ue-rx-indices $UE_RX_INDICES"
fi
if [ -n "$NPY_DIR" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --npy-dir $NPY_DIR"
fi
if [ "$USE_XLA" -eq 1 ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --xla"
fi
if [ -n "$CHANNEL_SEED" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --seed $CHANNEL_SEED"
fi
if [ -n "$UE_SPEED" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --speed $UE_SPEED"
fi
if [ -n "$DEFAULT_DIST_M" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --default-distance-m $DEFAULT_DIST_M"
fi
if [ -n "$ATTACH_STABLE_SEC" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --attach-stable-sec $ATTACH_STABLE_SEC"
fi
if [ -n "$ATTACH_STABLE_MODE" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --attach-stable-mode $ATTACH_STABLE_MODE"
fi
if [ -n "$ATTACH_STABLE_TRIGGER_FILE" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --attach-stable-trigger-file $ATTACH_STABLE_TRIGGER_FILE"
fi
if [ -n "$P1B_DISTANCE_MODE" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --p1b-distance-mode $P1B_DISTANCE_MODE"
fi
if [ "$SRS_ONLY_CHANNEL" = "1" ]; then
    PROXY_EXTRA_ARGS="$PROXY_EXTRA_ARGS --srs-only-channel --srs-symbols $SRS_SYMBOLS --srs-flat-mode $SRS_FLAT_MODE --srs-only-dl-flat $SRS_ONLY_DL_FLAT"
fi

echo "============================================================"
echo "  G1C 통합 런처 — v8 Channel Proxy (async GT D2H pipeline)"
echo "    Proxy : ${PROXY_SCRIPT}"
echo "    모드  : ${MODE}"
echo "    UE 수 : ${NUM_UES}"
echo "    gNB 안테나 : ${GNB_NX}x${GNB_NY} = ${GNB_ANT}"
echo "    UE 안테나  : ${UE_NX}x${UE_NY} = ${UE_ANT}"
if [ "$BYPASS_CHANNEL" -eq 1 ]; then
echo "    채널  : 바이패스 (IQ 패스스루)"
else
echo "    채널  : Sionna (custom channel)"
fi
[ -n "$PATH_LOSS_DB" ] && echo "    PL    : ${PATH_LOSS_DB#-} dB (internal: ${PATH_LOSS_DB} dB)"
[ -n "$SNR_DB" ] && echo "    Noise : 상대 SNR = ${SNR_DB} dB"
[ -n "$NOISE_DBFS" ] && echo "    Noise : 절대 floor = ${NOISE_DBFS} dBFS"
echo "    UL Gain: pre-gain x${UL_PRE_GAIN} before gNB int16 RX"
[ -n "$BUF_SYM_SIZE" ] && echo "    배치  : buffer-symbol-size = ${BUF_SYM_SIZE}"
[ -n "$BUF_LEN" ] && echo "    버퍼  : buffer-len = ${BUF_LEN}"
[ "$USE_XLA" -eq 1 ] && echo "    XLA   : Enabled (JIT kernel fusion)"
[ "$DIGITAL_AGC" -eq 1 ] && echo "    AGC   : Digital AGC ON (receiver-side EWMA)"
[ -n "$DURATION" ] && echo "    시간  : ${DURATION}초 후 자동 종료"
[ -n "$MAX_FRAMES" ] && echo "    프레임: ${MAX_FRAMES} 프레임 수집 후 자동 종료"
[ -n "$SRS_PERIOD_SLOTS" ] && echo "    SRS   : period override = ${SRS_PERIOD_SLOTS} slots"
[ -n "$DEFAULT_DIST_M" ] && echo "    거리  : ${DEFAULT_DIST_M} m (BS-UE default)"
[ -n "$ATTACH_STABLE_SEC" ] && echo "    Stable: ${ATTACH_STABLE_SEC} s attach-stable (${ATTACH_STABLE_MODE})"
[ "$ATTACH_STABLE_MODE" = "auto" ] && echo "    Stable trigger: ${ATTACH_STABLE_TRIGGER}, delay=${ATTACH_STABLE_POST_DELAY}s"
[ -n "$P1B_DISTANCE_MODE" ] && echo "    P1B 거리 모드: ${P1B_DISTANCE_MODE}"
echo "    로그  : ${LOG_DIR}/"
echo "    GT    : ${GT_DIR}"
if [ -n "$DURATION" ]; then
echo "  종료: ${DURATION}초 후 자동"
else
echo "  종료: Ctrl+C"
fi
echo "============================================================"

# ── Preflight 清理 ─────────────────────────────────────────────────
# 自动检测是否被 sweep 脚本调用 (INSIDE_SWEEP=1 由 sweep 脚本设置)
# sweep 内部调用时跳过 host 进程检查 (避免杀掉 sweep 自己)
PREFLIGHT_SH="${PREFLIGHT_SH:-$(dirname "${BASH_SOURCE[0]}")/preflight.sh}"
if [ "${SKIP_PREFLIGHT:-0}" != "1" ] && [ -f "$PREFLIGHT_SH" ]; then
    PF_ARGS=""
    if [ "${INSIDE_SWEEP:-0}" = "1" ]; then
        PF_ARGS="--inside-sweep"
    fi
    echo "[launcher] preflight 환경 정리${PF_ARGS:+ ($PF_ARGS)}..."
    bash "$PREFLIGHT_SH" $PF_ARGS 2>&1 | sed 's/^/  [preflight] /' || true
    echo "[launcher] preflight 완료"
else
    echo "[launcher] preflight 건너뜀 (SKIP_PREFLIGHT=1 또는 preflight.sh 미발견)"
    # 최소한의 fallback 정리
    pkill -9 -f "nr-softmodem" 2>/dev/null || true
    pkill -9 -f "nr-uesoftmodem" 2>/dev/null || true
    docker exec sionna-proxy pkill -9 -f "v[0-9]\.py" 2>/dev/null || true
    docker exec sionna-proxy pkill -9 -f "multiprocessing" 2>/dev/null || true
fi

rm -f /tmp/oai_gpu_ipc/gpu_ipc_shm 2>/dev/null || true
rm -f /tmp/oai_gpu_ipc/gpu_ipc_shm_ue* 2>/dev/null || true
rm -f "$ATTACH_STABLE_TRIGGER_FILE" 2>/dev/null || true
mkdir -p /tmp/oai_gpu_ipc 2>/dev/null || true
chmod 777 /tmp/oai_gpu_ipc 2>/dev/null || true
sleep 2

# ── Socket 모드: gNB를 먼저 시작 (gNB가 서버 역할) ──────────────
if [ "$MODE" = "socket" ]; then
    echo "[launcher] [socket] gNB 먼저 시작 (서버 port 6013)..."
    (cd "$LOG_DIR" && "$BUILD_DIR/nr-softmodem" \
        -O "$CONF" \
        --gNBs.[0].min_rxtxtime 6 --rfsim \
        --rfsimulator.serverport 6013 \
        > gnb.log 2>&1) &
    PIDS+=($!)
    echo "[launcher] gNB PID: ${PIDS[-1]}"

    echo "[launcher] gNB 서버 대기 (rfsimulator listen)..."
    for i in $(seq 1 30); do
        if grep -q "Running as server" "$LOG_DIR/gnb.log" 2>/dev/null; then
            echo "[launcher] gNB 서버 준비 완료."
            break
        fi
        if [ "$i" -eq 30 ]; then
            echo "[launcher] WARN: gNB 서버 대기 30초 초과, 계속 진행..."
        fi
        sleep 1
    done
fi

# ── 1. Proxy ────────────────────────────────────────────────────
echo "[launcher] Proxy 시작 (${PROXY_SCRIPT}, ${MODE})..."

PROXY_ENV_ARGS="-e PROXY_GPU_IDX=$GPU_IDX -e LD_LIBRARY_PATH=/usr/local/cuda-12.8/lib64"
if [[ "$PROXY_VER" =~ ^v[2-9]$ ]] || [[ "$PROXY_VER" =~ ^v[0-9][0-9]+$ ]]; then
    PROXY_ENV_ARGS="$PROXY_ENV_ARGS -e GPU_IPC_V5_GNB_ANT=$GNB_ANT"
    PROXY_ENV_ARGS="$PROXY_ENV_ARGS -e GPU_IPC_V5_GNB_NX=$GNB_NX"
    PROXY_ENV_ARGS="$PROXY_ENV_ARGS -e GPU_IPC_V5_GNB_NY=$GNB_NY"
    PROXY_ENV_ARGS="$PROXY_ENV_ARGS -e GPU_IPC_V5_UE_ANT=$UE_ANT"
    PROXY_ENV_ARGS="$PROXY_ENV_ARGS -e GPU_IPC_V5_UE_NX=$UE_NX"
    PROXY_ENV_ARGS="$PROXY_ENV_ARGS -e GPU_IPC_V5_UE_NY=$UE_NY"
fi
PROXY_ENV_ARGS="$PROXY_ENV_ARGS -e SIONNA_GT_DIR=$GT_DIR"

docker exec -i $PROXY_ENV_ARGS sionna-proxy python3 -u \
    "/workspace/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/${PROXY_SCRIPT}" \
    --mode="$MODE" $PROXY_EXTRA_ARGS \
    --gnb-ant=$GNB_ANT --ue-ant=$UE_ANT \
    --gnb-nx=$GNB_NX --gnb-ny=$GNB_NY \
    --ue-nx=$UE_NX --ue-ny=$UE_NY \
    --num-ues=$NUM_UES \
    --gt-save-every=$GT_SAVE_EVERY \
    --gt-symbols=$GT_SYMBOLS \
    --gt-async-copy=$GT_ASYNC_COPY \
    --gt-pinned-pool-size=$GT_PINNED_POOL_SIZE \
    --gt-staging-gpu-buffers=$GT_STAGING_GPU_BUFFERS \
    > "$LOG_DIR/proxy.log" 2>&1 &
PIDS+=($!)

echo "[launcher] Proxy 초기화 대기 (출력 실시간 표시)..."
echo "------------------------------------------------------------"
tail -f "$LOG_DIR/proxy.log" 2>/dev/null &
TAIL_WAIT_PID=$!

SHM_FIXED=0
for i in $(seq 1 120); do
    if [ "$SHM_FIXED" -eq 0 ] && [ -f /tmp/oai_gpu_ipc/gpu_ipc_shm ]; then
        docker exec sionna-proxy chmod 666 /tmp/oai_gpu_ipc/gpu_ipc_shm 2>/dev/null || true
        for shm_ue in /tmp/oai_gpu_ipc/gpu_ipc_shm_ue*; do
            [ -f "$shm_ue" ] && docker exec sionna-proxy chmod 666 "$shm_ue" 2>/dev/null || true
        done
        SHM_FIXED=1
        echo "[launcher] GPU IPC SHM 권한 수정 완료 (gNB + ${NUM_UES} UE(s))"
    fi
    if grep -q "Entering main loop\|Pipeline ready" "$LOG_DIR/proxy.log" 2>/dev/null; then
        if [ "$SHM_FIXED" -eq 0 ] && [ -f /tmp/oai_gpu_ipc/gpu_ipc_shm ]; then
            docker exec sionna-proxy chmod 666 /tmp/oai_gpu_ipc/gpu_ipc_shm 2>/dev/null || true
            for shm_ue in /tmp/oai_gpu_ipc/gpu_ipc_shm_ue*; do
                [ -f "$shm_ue" ] && docker exec sionna-proxy chmod 666 "$shm_ue" 2>/dev/null || true
            done
        fi
        kill $TAIL_WAIT_PID 2>/dev/null || true
        wait $TAIL_WAIT_PID 2>/dev/null || true
        echo "------------------------------------------------------------"
        echo "[launcher] Proxy 준비 완료."
        break
    fi
    if ! kill -0 "${PIDS[-1]}" 2>/dev/null; then
        kill $TAIL_WAIT_PID 2>/dev/null || true
        echo "[launcher] ERROR: Proxy 프로세스가 종료됨."
        cleanup
    fi
    if [ "$i" -eq 120 ]; then
        kill $TAIL_WAIT_PID 2>/dev/null || true
        echo "[launcher] ERROR: Proxy가 120초 내에 시작되지 않음."
        cleanup
    fi
    sleep 1
done

# ── 2. gNB (GPU-IPC 모드에서만 여기서 시작) ─────────────────────
if [ "$MODE" != "socket" ]; then
    if [[ "$PROXY_VER" == "v1" ]] || [[ "$PROXY_VER" =~ ^v[2-9]$ ]] || [[ "$PROXY_VER" =~ ^v[0-9][0-9]+$ ]]; then
        GPU_IPC_ENV="RFSIM_GPU_IPC_V8=1"
        echo "[launcher] gNB 시작 (GPU IPC V8, ant=${GNB_ANT})..."
    else
        GPU_IPC_ENV="RFSIM_GPU_IPC_V6=1"
        echo "[launcher] gNB 시작 (GPU IPC V6, ant=${GNB_ANT})..."
    fi

    GNB_ANT_ARGS=""
    if [ "$GNB_ANT" -ge 2 ]; then
        GNB_ANT_ARGS="--RUs.[0].nb_tx $GNB_ANT --RUs.[0].nb_rx $GNB_ANT"
        GNB_ANT_ARGS="$GNB_ANT_ARGS --gNBs.[0].pdsch_AntennaPorts_XP 2"
        GNB_ANT_ARGS="$GNB_ANT_ARGS --gNBs.[0].pusch_AntennaPorts $GNB_ANT"
    fi
    if [ "$GNB_ANT" -ge 4 ]; then
        GNB_ANT_ARGS="$GNB_ANT_ARGS --gNBs.[0].pdsch_AntennaPorts_N1 2"
    fi

    AGC_ENV=""
    if [ "$DIGITAL_AGC" -eq 1 ]; then
        AGC_ENV="NR_DIGITAL_AGC_ENABLED=1"
        AGC_ENV="$AGC_ENV NR_DIGITAL_AGC_BETA=${NR_DIGITAL_AGC_BETA:-0.95}"
        AGC_ENV="$AGC_ENV NR_DIGITAL_AGC_TARGET_RMS=${NR_DIGITAL_AGC_TARGET_RMS:-16384}"
        echo "[launcher] Digital AGC ON for gNB (beta=${NR_DIGITAL_AGC_BETA:-0.95}, target_rms=${NR_DIGITAL_AGC_TARGET_RMS:-16384})"
    fi

    SRS_ENV="SRS_ESTIMATOR=$SRS_ESTIMATOR SRS_OPTFILT=$SRS_OPTFILT SRS_REF_DUMP_PATH=$SRS_REF_DUMP_PATH SRS_PERIOD_SLOTS=$SRS_PERIOD_SLOTS"
    # Forward SRS_2D_* env vars (Kalman Fix1-5, IBVSS, etc.) to nr-softmodem
    [ -n "$SRS_2D_METHOD" ] && SRS_ENV="$SRS_ENV SRS_2D_METHOD=$SRS_2D_METHOD"
    [ -n "$SRS_2D_DEBUG" ]  && SRS_ENV="$SRS_ENV SRS_2D_DEBUG=$SRS_2D_DEBUG"
    [ -n "${SRS_2D_KALMAN_WARMUP_MIN:-}" ]  && SRS_ENV="$SRS_ENV SRS_2D_KALMAN_WARMUP_MIN=$SRS_2D_KALMAN_WARMUP_MIN"
    [ -n "${SRS_2D_KALMAN_WARMUP_MAX:-}" ]  && SRS_ENV="$SRS_ENV SRS_2D_KALMAN_WARMUP_MAX=$SRS_2D_KALMAN_WARMUP_MAX"
    [ -n "${SRS_2D_KALMAN_DEBOUNCE:-}" ]    && SRS_ENV="$SRS_ENV SRS_2D_KALMAN_DEBOUNCE=$SRS_2D_KALMAN_DEBOUNCE"
    [ -n "${SRS_2D_KALMAN_Q_EMA:-}" ]       && SRS_ENV="$SRS_ENV SRS_2D_KALMAN_Q_EMA=$SRS_2D_KALMAN_Q_EMA"
    [ -n "${SRS_2D_KALMAN_INNOV_EMA:-}" ]   && SRS_ENV="$SRS_ENV SRS_2D_KALMAN_INNOV_EMA=$SRS_2D_KALMAN_INNOV_EMA"
    [ -n "${SRS_2D_ALPHA:-}" ]              && SRS_ENV="$SRS_ENV SRS_2D_ALPHA=$SRS_2D_ALPHA"
    # Forward SRS_FREQ_SMOOTH_* env vars (adaptive frequency-domain smoothing)
    [ -n "${SRS_FREQ_SMOOTH_MAX_WIN:-}" ]  && SRS_ENV="$SRS_ENV SRS_FREQ_SMOOTH_MAX_WIN=$SRS_FREQ_SMOOTH_MAX_WIN"
    [ -n "${SRS_FREQ_SMOOTH_DEBUG:-}" ]    && SRS_ENV="$SRS_ENV SRS_FREQ_SMOOTH_DEBUG=$SRS_FREQ_SMOOTH_DEBUG"
    # Forward SRS_MMSE_WINDOW env var (MMSE1D window override)
    [ -n "${SRS_MMSE_WINDOW:-}" ]          && SRS_ENV="$SRS_ENV SRS_MMSE_WINDOW=$SRS_MMSE_WINDOW"
    echo "[launcher] SRS_ENV: $SRS_ENV"

    (cd "$LOG_DIR" && eval "$GPU_IPC_ENV RFSIM_GPU_DEVICE=$GPU_IDX $AGC_ENV $SRS_ENV" \
    "$BUILD_DIR/nr-softmodem" \
        -O "$CONF" \
        --gNBs.[0].min_rxtxtime 6 --rfsim \
        $GNB_ANT_ARGS \
        > gnb.log 2>&1) &
    PIDS+=($!)
    echo "[launcher] gNB PID: ${PIDS[-1]}"
    sleep 3
fi

start_attach_stable_watcher

# ── 3. UE 0 ~ N-1 ──────────────────────────────────────────────
for ue_idx in $(seq 0 $((NUM_UES - 1))); do
    imsi_suffix=$((ue_idx + 1))
    imsi=$(printf "00101000000%04d" "$imsi_suffix")

    echo "[launcher] UE ${ue_idx} 시작 (IMSI=${imsi})..."

    if [ "$MODE" = "socket" ]; then
        (cd "$LOG_DIR" && "$BUILD_DIR/nr-uesoftmodem" \
            -r 106 --numerology 1 --band 78 -C 3619200000 \
            --uicc0.imsi "$imsi" \
            --rfsim --rfsimulator.serverport 6014 \
            --ue-timing-correction-disable \
            > "ue${ue_idx}.log" 2>&1) &
    else
        if [[ "$PROXY_VER" == "v1" ]] || [[ "$PROXY_VER" =~ ^v[2-9]$ ]] || [[ "$PROXY_VER" =~ ^v[0-9][0-9]+$ ]]; then
            UE_GPU_ENV="RFSIM_GPU_IPC_V8=1 RFSIM_GPU_IPC_UE_IDX=$ue_idx RFSIM_GPU_DEVICE=$GPU_IDX"
        else
            UE_GPU_ENV="RFSIM_GPU_IPC_V6=1 RFSIM_GPU_IPC_UE_IDX=$ue_idx RFSIM_GPU_DEVICE=$GPU_IDX"
        fi

        UE_ANT_ARGS=""
        if [ "$UE_ANT" -gt 1 ]; then
            UECAP_FILE="uecap_ports2.xml"
            if [ "$UE_ANT" -gt 2 ]; then
                UECAP_FILE="uecap_ports4.xml"
            fi
            UE_ANT_ARGS="--ue-nb-ant-tx $UE_ANT --ue-nb-ant-rx $UE_ANT"
            UE_ANT_ARGS="$UE_ANT_ARGS --uecap_file $CONF_DIR/$UECAP_FILE"
        fi

        UE_AGC_ENV=""
        if [ "$DIGITAL_AGC" -eq 1 ]; then
            UE_AGC_ENV="NR_DIGITAL_AGC_ENABLED=1"
            UE_AGC_ENV="$UE_AGC_ENV NR_DIGITAL_AGC_BETA=${NR_DIGITAL_AGC_BETA:-0.95}"
            UE_AGC_ENV="$UE_AGC_ENV NR_DIGITAL_AGC_TARGET_RMS=${NR_DIGITAL_AGC_TARGET_RMS:-16384}"
        fi

        (cd "$LOG_DIR" && eval "$UE_GPU_ENV $UE_AGC_ENV" \
        "$BUILD_DIR/nr-uesoftmodem" \
            -r 106 --numerology 1 --band 78 -C 3619200000 \
            --uicc0.imsi "$imsi" \
            --rfsim \
            --ue-timing-correction-disable \
            $UE_ANT_ARGS \
            > "ue${ue_idx}.log" 2>&1) &
    fi
    PIDS+=($!)
    echo "[launcher] UE ${ue_idx} PID: ${PIDS[-1]}"
    sleep 2
done

# ── 3.5 v9 Attach State Machine + ULSCH BLER Diagnostic ─────────
ATTACH_TIMEOUT="${ATTACH_TIMEOUT:-120}"
ATTACH_DIAG_LOG="$LOG_DIR/attach_diag_v9.log"
(
    echo "[v9-attach-diag] started at $(date +%s), timeout=${ATTACH_TIMEOUT}s"
    _start=$(date +%s)
    _state="WAIT_SYNC"
    _bler_high_since=0

    while true; do
        _now=$(date +%s)
        _elapsed=$((_now - _start))

        # State machine
        case "$_state" in
            WAIT_SYNC)
                if grep -q "Got synch" "$LOG_DIR/ue0.log" 2>/dev/null; then
                    _state="WAIT_RA"
                    echo "[v9-attach-diag] ${_elapsed}s: WAIT_SYNC -> WAIT_RA"
                fi
                ;;
            WAIT_RA)
                if grep -q "4-Step RA procedure succeeded" "$LOG_DIR/ue0.log" 2>/dev/null; then
                    _state="WAIT_RRC_RECFG"
                    echo "[v9-attach-diag] ${_elapsed}s: WAIT_RA -> WAIT_RRC_RECFG"
                fi
                ;;
            WAIT_RRC_RECFG)
                if grep -q "Received RRCReconfigurationComplete" "$LOG_DIR/gnb.log" 2>/dev/null; then
                    _state="ATTACHED"
                    echo "[v9-attach-diag] ${_elapsed}s: ATTACHED OK"
                    break
                fi
                # ULSCH BLER diagnostic
                _bler_line=$(grep -o 'ulsch_rounds [0-9]*/[^,]*, ulsch_errors [0-9]*' "$LOG_DIR/gnb.log" 2>/dev/null | tail -1)
                if [ -n "$_bler_line" ]; then
                    _rounds=$(echo "$_bler_line" | grep -oP 'ulsch_rounds \K[0-9]+')
                    _errors=$(echo "$_bler_line" | grep -oP 'ulsch_errors \K[0-9]+')
                    if [ -n "$_rounds" ] && [ -n "$_errors" ] && [ "$_rounds" -gt 5 ]; then
                        _bler_pct=$(( _errors * 100 / _rounds ))
                        echo "[v9-attach-diag] ${_elapsed}s: ULSCH rounds=${_rounds} errors=${_errors} BLER=${_bler_pct}%"
                        if [ "$_bler_pct" -gt 90 ]; then
                            if [ "$_bler_high_since" -eq 0 ]; then
                                _bler_high_since=$_now
                            elif [ $((_now - _bler_high_since)) -ge 30 ]; then
                                echo "[v9-attach-diag] ${_elapsed}s: FAIL-ATTACH-UL (BLER>${_bler_pct}% for 30s+)"
                                echo "FAIL-ATTACH-UL" > "$LOG_DIR/attach_result_v9.txt"
                                break
                            fi
                        else
                            _bler_high_since=0
                        fi
                    fi
                fi
                ;;
        esac

        # Timeout
        if [ "$_elapsed" -ge "$ATTACH_TIMEOUT" ]; then
            echo "[v9-attach-diag] ${_elapsed}s: FAIL-ATTACH-TIMEOUT (state=${_state})"
            echo "FAIL-ATTACH-TIMEOUT(${_state})" > "$LOG_DIR/attach_result_v9.txt"
            break
        fi
        sleep 5
    done

    # Summary
    _all0=$(grep -c "all 0 pdu" "$LOG_DIR/ue0.log" 2>/dev/null || echo 0)
    _contention=$(grep -c "Contention resolution failed" "$LOG_DIR/ue0.log" 2>/dev/null || echo 0)
    _t300=$(grep -c "Timer T300 expired" "$LOG_DIR/ue0.log" 2>/dev/null || echo 0)
    _pbch_err=$(grep -c "Error decoding PBCH" "$LOG_DIR/ue0.log" 2>/dev/null || echo 0)
    echo "[v9-attach-diag] summary: state=${_state} all_0_pdu=${_all0} contention=${_contention} t300=${_t300} pbch_err=${_pbch_err}"

    if [ "$_state" = "ATTACHED" ]; then
        echo "OK" > "$LOG_DIR/attach_result_v9.txt"
    fi
) >> "$ATTACH_DIAG_LOG" 2>&1 &
PIDS+=($!)
echo "[launcher] v9 attach diagnostics PID: ${PIDS[-1]} → ${ATTACH_DIAG_LOG}"

# ── 3.5 시스템 리소스 모니터 (sysmon) ────────────────────────────
SYSMON_CSV="$LOG_DIR/sysmon.csv"
echo "epoch,cpu_pct,ram_used_mb,ram_total_mb,gpu_util_pct,gpu_mem_used_mb,gpu_mem_total_mb" > "$SYSMON_CSV"
(while true; do
  _TS=$(date +%s.%3N)
  _CPU=$(awk '/^cpu / {u=$2+$4; t=$2+$4+$5; printf "%.1f", (u/t)*100}' /proc/stat)
  _RAM=$(free -m | awk '/Mem/{printf "%d,%d", $3, $2}')
  _GPU=$(nvidia-smi -i $GPU_IDX --query-gpu=utilization.gpu,memory.used,memory.total \
         --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')
  echo "${_TS},${_CPU},${_RAM},${_GPU}"
  sleep 1
done >> "$SYSMON_CSV") &
PIDS+=($!)
echo "[launcher] sysmon PID: ${PIDS[-1]} → ${SYSMON_CSV}"

# ── 4. 모니터링 ─────────────────────────────────────────────────
echo ""
echo "============================================================"
echo "  전체 프로세스 실행 중 (${#PIDS[@]}개)"
echo "  메인 터미널: Proxy 로그 실시간 표시"
echo ""
echo "  gNB/UE 로그 확인 (별도 터미널에서):"
echo "    tail -f ${LOG_DIR}/gnb.log"
for ue_idx in $(seq 0 $((NUM_UES - 1))); do
    echo "    tail -f ${LOG_DIR}/ue${ue_idx}.log"
done
echo ""
echo "  시스템 리소스 (CPU/RAM/GPU, 1초 간격):"
echo "    tail -f ${LOG_DIR}/sysmon.csv"
echo ""
echo "  OAI 통계 확인 (별도 터미널에서):"
echo "    cat ${LOG_DIR}/nrMAC_stats.log       # CQI/PMI/RSRP/BLER/MCS"
echo "    cat ${LOG_DIR}/nrRRC_stats.log       # 연결된 DU/UE 목록"
echo "    cat ${LOG_DIR}/nrL1_stats.log        # PRB I0, PRACH I0"
echo ""
echo "  바로가기: $PROJ_DIR/logs/latest/"
if [ -n "$DURATION" ]; then
echo "  ${DURATION}초 후 자동 종료 (또는 Ctrl+C)"
else
echo "  Ctrl+C로 전체 종료"
fi
echo "============================================================"

tail -f "$LOG_DIR/proxy.log" &
TAIL_PID=$!

PARENT_PID=$$
if [ -n "$MAX_FRAMES" ]; then
    MAX_SEQS=$(( (MAX_FRAMES + 99) / 100 ))
    # Gate on both SRS bin count and actual SRS frame count parsed from
    # bin headers. This prevents "partial flush" cases (e.g. 7 frames) from
    # being marked complete just because one SRS file exists.
    MIN_SRS_BINS="${MIN_SRS_BINS:-0}"     # 0 = GT-only (backward compatible)
    MIN_SRS_FRAMES="${MIN_SRS_FRAMES:-$MAX_FRAMES}"  # 0 = disable frame gate
    echo "[launcher] Frame-based auto-stop: ≥${MAX_SEQS} GT seq files (~${MAX_FRAMES} frames)$([ "$MIN_SRS_BINS" -gt 0 ] && echo " AND ≥${MIN_SRS_BINS} SRS bins")$([ "$MIN_SRS_FRAMES" -gt 0 ] && echo " AND ≥${MIN_SRS_FRAMES} SRS frames")"
    (
        while true; do
            sleep 3
            n_gt=$(ls "$GT_DIR"/gt_batch_ue0_seq*.npz 2>/dev/null | wc -l)
            n_srs=0
            n_srs_frames=0
            if [ "$MIN_SRS_BINS" -gt 0 ]; then
                n_srs=$(ls "$LOG_DIR"/srs_matrix_gNB_*.bin 2>/dev/null | wc -l)
            fi
            if [ "$MIN_SRS_FRAMES" -gt 0 ]; then
                if [ "$n_srs" -eq 0 ]; then
                    n_srs=$(ls "$LOG_DIR"/srs_matrix_gNB_*.bin 2>/dev/null | wc -l)
                fi
                if [ "$n_srs" -gt 0 ]; then
                    n_srs_frames=$(sum_srs_frames "$LOG_DIR")
                fi
            fi
            if [ "$n_gt" -ge "$MAX_SEQS" ] \
               && [ "$n_srs" -ge "$MIN_SRS_BINS" ] \
               && [ "$n_srs_frames" -ge "$MIN_SRS_FRAMES" ]; then
                echo ""
                echo "[launcher] Frame threshold reached (gt=${n_gt}/${MAX_SEQS} srs_bins=${n_srs}/${MIN_SRS_BINS} srs_frames=${n_srs_frames}/${MIN_SRS_FRAMES}) — stopping"
                # Use SIGTERM rather than SIGINT: when this script is itself
                # launched via `bash ... &` from a wrapper (e.g. the SNR
                # sweep), bash forces SIGINT=SIG_IGN at startup and the
                # `trap cleanup SIGINT` above is silently disabled. SIGTERM
                # is not in that ignored set and fires the trap correctly.
                kill -TERM "$PARENT_PID" 2>/dev/null || true
                exit 0
            fi
        done
    ) &
    FRAME_WATCHER_PID=$!
fi

if [ -n "$DURATION" ]; then
    echo "[launcher] ${DURATION}초 후 자동 종료 예정..."
    sleep "$DURATION"
    echo ""
    echo "[launcher] ${DURATION}초 경과 — 자동 종료"
    [ -n "${FRAME_WATCHER_PID:-}" ] && kill "$FRAME_WATCHER_PID" 2>/dev/null || true
    kill $TAIL_PID 2>/dev/null || true
    cleanup
else
    wait "${PIDS[0]}" 2>/dev/null || true
    [ -n "${FRAME_WATCHER_PID:-}" ] && kill "$FRAME_WATCHER_PID" 2>/dev/null || true
    kill $TAIL_PID 2>/dev/null || true
    cleanup
fi
