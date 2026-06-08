"""
================================================================================
v8.py - G1C Multi-UE MIMO Channel Proxy (v7 + async GT D2H pipeline)

[v8 → v8 변경사항]

  Async GT D2H pipeline (highest priority):
  -----------------------------------------
  v7 GTBatchSaver wrote files asynchronously, but the GPU→CPU copy
  (cupy `.get()`) inside `record_ul_slot` was synchronous and forced a
  cuda default-stream sync. With `--gt-save-every=1` this stalled the UL
  pipeline (~5 ms per UL slot), starving the gNB UL ringbuffer and
  blocking RA / RRC / SRS attach progress.

  v8 fixes this end-to-end:
    A. Per-UE GPU→GPU staging copy is enqueued on the default cuda stream
       BEFORE `channel_buffers.release_batch()` in
       `_ipc_ul_superposition_slot`. Default-stream serial execution
       guarantees the staging copy runs after the process_slot_ipc kernels
       that READ channels_ul, so no race vs ChannelProducer's next-slot
       writes can corrupt the captured data.
    B. `stage_for_ue()` records (does NOT synchronize) a per-token cuda
       Event on the default stream and returns IMMEDIATELY to the main
       thread. CPU never blocks on a default-stream event — that would
       defeat async-ness by serializing against process_slot_ipc.
    C. The actual GPU→CPU memcpy is enqueued on a dedicated cupy Stream
       (`_copy_stream`, non_blocking=True) and runs in parallel with the
       next UL slot. copy_stream.wait_event(stage_event) ensures GPU-side
       (not CPU-side) ordering between the two streams.
    D. A new `_copy_drain_loop` thread waits on each token's Event in the
       background, then hands the host buffer off to the existing batch
       buffer / `_writer_loop` chain.
    E. Pinned (page-locked) host pool + GPU staging pool are pre-allocated
       at startup to avoid per-slot allocations.
    F. Pool exhaustion falls back to v7-style synchronous `.get()` instead
       of dropping data (back-pressure, with overflow counter).
    G. `_buffers / _seq` access is now thread-safe via per-UE locks
       (writer thread + main thread + sync-fallback path can all touch).
    H. Shutdown waits for in-flight copies (`_copy_q.join()`) before
       flushing, so no GT data is lost.
    I. Slot-level `should_capture` decision is taken once per UL slot in
       `_ipc_ul_superposition_slot`, then reused for every UE in that
       slot — guarantees per-slot consistency.

  CLI additions:
    --gt-async-copy {on,off}        (default on)
    --gt-pinned-pool-size N         (default 32, ~1 MB total at 32 KB/buf)
    --gt-staging-gpu-buffers N      (default 32, ~1 MB GPU)

  Behavioral compatibility with v7:
    - Output .npz format is byte-equivalent (h_matrix / slot_ids /
      bypass_flags / symbol_indices / gnb_ant / ue_ant / fft_size).
    - With --gt-async-copy=off the path is identical to v7 (for A/B).
    - With --gt-save-every=10 the speedup is invisible; v8 is only a
      strict win at low save_every.

[v6 →  변경사항] (inherited from v7)

  Physics correctness (highest priority):
  --------------------------------------
  1. velocities: scalar speed magnitude + random horizontal direction
     (was: 3D component-wise normal, total |v| = sqrt(3)*Speed)
  2. los_aoa/aod/zoa/zod: random samples (was: all zeros)
  3. distance_3d: configurable for P1B (fixed default or tau-derived),
     else configurable fallback (was: hardcoded ones)
  4. Energy normalization uses first-batch reference, preserving Doppler
     power fluctuations across batches (was: per-batch normalization)
  5. sample_times use integer base to avoid float32 precision loss after
     long runs (was: batch_idx * batch_duration accumulation)
  6. _validate_physics_params() raises ValueError instead of assert,
     called after CLI args resolved so Speed override is validated.

  Robustness:
  -----------
  7. batch_idx incremented immediately after generate_fn() to prevent
     duplicate batches on exception retry.
  8. Producer alive-check in main loop; clean shutdown on producer death.
  9. Producer exception loop has consecutive-failure limit (prevents
     infinite spam).
  10. Socket mode rejects num_ues > 1 (multi-UE unimplemented for socket).
  11. CUDA Graph capture uses Relaxed mode (avoids global stream conflicts).
  12. CUDA Graph failure is recoverable (retry per slot, not permanent).
  13. _stalled_ue_logged is cleared when UE rejoins active_set.

  Performance / memory:
  ---------------------
  14. process_slot_ipc(skip_quant=True) avoids dummy clip+cast in UL
      superposition path.
  15. NoiseProducer maxlen 256→64, dtype float64→float32 (~16x VRAM save).
  16. GTBatchSaver writes asynchronously in a background thread.
  17. bypass_copy uses preallocated buffer (no per-call cp.zeros).
  18. UL ipc_gnb.set_last_ul_rx_ts called once per slot (was: per-UE).
  19. IPCRingBufferProducer.try_put_batch syncs outside the lock.
  20. Antenna consistency check: gnb_ant == gnb_nx * gnb_ny.

  Misc:
  -----
  21. CPU-mode noise_dBFS support (was: silently ignored).
  22. random.sample size check (was: cryptic ValueError).
  23. Removed dead _ul_clip_3d allocation.
  24. Version strings normalized to [v7].   (v8: re-normalized to [v8])

  Attach stability:
  -----------------
  25. Optional attach-stable window freezes inter-batch sample_times for the
      first N seconds so RRC/NAS/SRS setup can complete before Doppler dynamics.
  26. P1B distance can use a fixed default fallback instead of tau-derived
      distance, avoiding degenerate 1m floors seen with some P1B ray dumps.

[아키텍처 (unchanged from v4/v6/v7)]
  P1B npz → load_p1b_stacked() → 스택된 ray_data (N_UE 차원)
  UnifiedChannelProducerProcess (단일 프로세스: TF+CuPy, N_UE 통합)
    → UE별 슬라이싱 → try_put_batch() → N개 IPCRingBuffer
  Proxy Main Process (CuPy + CUDA Graph):
    → active_set 기반 UL superposition
    → gNB/UE IPC V7 polling loop
================================================================================
"""
import argparse, selectors, socket, struct, numpy as np
import bisect
import ctypes
import ctypes.util
import mmap
import multiprocessing as _mp
import signal
import random as _random
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Tuple, Optional
import logging
import threading
import json
import time

_mp_ctx = _mp.get_context('spawn')
from sionna.phy import PI, SPEED_OF_LIGHT
from datetime import datetime
import os
from channel_coefficients_JIN import ChannelCoefficientsGeneratorJIN, random_binary_mask_tf_complex64

try:
    from sionna.phy.channel.tr38901 import PanelArray, Topology, Rays
    print("[Sionna Init] sionna.phy.channel.tr38901 모듈 로드 성공")
except ModuleNotFoundError as e:
    print(f"[Sionna Init] sionna 모듈 로드 실패: {e}")
    import sys
    sys.exit(1)

try:
    import cupy as cp
    GPU_AVAILABLE = True
    print("[GPU Init] CuPy 로드 성공 - GPU 가속 활성화")
except ImportError:
    cp = None
    GPU_AVAILABLE = False
    print("[GPU Init] CuPy 없음 - CPU 모드로 실행")

SIONNA_API_IP = "127.0.0.1"
SIONNA_API_PORT = 7000

try:
    import tensorflow as tf
except ImportError:
    tf = None

gpu_num = int(os.environ.get('PROXY_GPU_IDX', '0'))
os.environ['TF_GPU_ALLOCATOR'] = 'cuda_malloc_async'
os.environ["CUDA_VISIBLE_DEVICES"] = f"{gpu_num}"
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "0"

HDR_FMT_LE = "<I I Q I I"
HDR_LEN = struct.calcsize(HDR_FMT_LE)

def unpack_header(b):
    size, nb, ts, frame, subframe = struct.unpack(HDR_FMT_LE, b)
    return size, nb, ts, frame, subframe

MAX_LOG = 300
LOG_LINES = []
DL_LOG_CNT = 0
LAST_TS = None
LAST_WALLTIME = None
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
directory = os.path.join(os.path.dirname(_SCRIPT_DIR), "saved_rays_data")


def validate_rx_indices(npz_path, requested_indices):
    """P1B npz의 유효 RX 인덱스와 대조.
    무효 인덱스마다 가장 가까운 유효 인덱스를 안내하고 sys.exit(1)."""
    data = np.load(npz_path, allow_pickle=True)
    valid_set = set(data['rx_indices'].tolist())
    sorted_valid = sorted(valid_set)
    data.close()

    errors = []
    for rx in requested_indices:
        if rx not in valid_set:
            pos = bisect.bisect_left(sorted_valid, rx)
            candidates = []
            if pos > 0:
                candidates.append(sorted_valid[pos - 1])
            if pos < len(sorted_valid):
                candidates.append(sorted_valid[pos])
            nearest = min(candidates, key=lambda x: abs(x - rx))
            errors.append(
                f"  RX{rx}는 존재하지 않습니다. "
                f"가장 가까운 유효 인덱스: RX{nearest}")

    if errors:
        print(f"[ERROR] --ue-rx-indices 검증 실패 ({len(errors)}건):")
        for e in errors:
            print(e)
        print(f"  유효 범위: RX{sorted_valid[0]}~RX{sorted_valid[-1]} "
              f"(총 {len(sorted_valid)}개)")
        import sys; sys.exit(1)


def pick_random_rx_indices(npz_path, num_ues):
    """유효 RX 중 num_ues개 무작위 선택 (중복 없음)."""
    data = np.load(npz_path, allow_pickle=True)
    all_indices = data['rx_indices'].tolist()
    data.close()
    if num_ues > len(all_indices):
        raise ValueError(
            f"--num-ues={num_ues} exceeds number of available RX "
            f"indices in P1B npz ({len(all_indices)} available). "
            f"Reduce num_ues or use a richer P1B file.")
    selected = _random.sample(all_indices, num_ues)
    parts = ", ".join(f"UE{i}=RX{rx}" for i, rx in enumerate(selected))
    print(f"[P1B] 랜덤 RX 선택: {parts}")
    return selected


def load_p1b_per_ue(npz_path, rx_index):
    """P1B npz에서 특정 RX의 ray 데이터를 추출, degree→radian 변환.
    Returns: dict with 6 numpy arrays, each shape (1,1,1,1,400)"""
    data = np.load(npz_path, allow_pickle=True)
    rx_list = data['rx_indices'].tolist()
    pos = rx_list.index(rx_index)
    rad = np.pi / 180.0
    result = {
        'tau':     data['tau'][pos],
        'power':   data['power'][pos],
        'phi_r':   data['phi_r_deg'][pos] * rad,
        'phi_t':   data['phi_t_deg'][pos] * rad,
        'theta_r': data['theta_r_deg'][pos] * rad,
        'theta_t': data['theta_t_deg'][pos] * rad,
    }
    data.close()
    return result


def load_p1b_stacked(npz_path, rx_indices):
    """N개 RX의 ray 데이터를 N_UE 차원으로 스택.
    Returns: dict, each value shape (1, 1, N_UE, 1, 400)"""
    per_ue = [load_p1b_per_ue(npz_path, rx) for rx in rx_indices]
    return {
        key: np.concatenate([d[key] for d in per_ue], axis=2)
        for key in per_ue[0].keys()
    }


def log(direction, size, nb, ts, frame, subframe, samples, note=""):
    global LOG_LINES, DL_LOG_CNT, LAST_TS, LAST_WALLTIME
    if len(LOG_LINES) >= MAX_LOG:
        return
    msg = f"{direction:<17}| size={size:<7} nbAnt={nb:<2} ts={ts:<12} samples={samples:<7} {note}"
    LOG_LINES.append(msg)
    print(msg)
    if direction.strip() == "gNB → Proxy":
        DL_LOG_CNT += 1
        if DL_LOG_CNT % 10 == 0:
            if DL_LOG_CNT == 10:
                print(f"   --- [DL {DL_LOG_CNT}회] ts={ts}")
                LAST_TS = ts
                LAST_WALLTIME = time.time()
            else:
                now = time.time()
                ts_delta = ts - (LAST_TS if LAST_TS is not None else ts)
                wall_delta = now - (LAST_WALLTIME if LAST_WALLTIME is not None else now)
                print(f"   --- [DL {DL_LOG_CNT}회] ts={ts}, Δts={ts_delta}, Δwall={wall_delta:.6f} sec")
                LAST_TS = ts
                LAST_WALLTIME = now

# OFDM NR numerology=1
carrier_frequency = 3.5e9
FFT_SIZE = 2048
N_FFT = FFT_SIZE
CP1 = 144
CP2 = 160
N_SYM = 14
SYMBOL_SIZES = ([CP2 + FFT_SIZE] + [CP1 + FFT_SIZE] * 6
                + [CP2 + FFT_SIZE] + [CP1 + FFT_SIZE] * 6)
CP_LENGTHS = [CP2] + [CP1]*6 + [CP2] + [CP1]*6
scs = 30*1e3
Fs = FFT_SIZE*scs

path_loss_dB = 0
pathLossLinear = 10**(path_loss_dB / 20.0)
snr_dB = None
noise_dBFS = None
noise_mode = "none"
noise_enabled = False
noise_std_abs = None

Speed = 3


def _validate_physics_params(cf=None, sc=None, sp=None):
    """Validate physics parameters. Uses module globals if args omitted.
    Raises ValueError (not assert) so it survives `python -O`."""
    cf = carrier_frequency if cf is None else cf
    sc = scs if sc is None else sc
    sp = Speed if sp is None else sp
    _wavelength = 3e8 / cf
    if not (1e8 < cf < 1e12):
        raise ValueError(
            f"carrier_frequency={cf} Hz — "
            f"expected 100 MHz–1 THz for 5G/sub-THz")
    if not (1e-4 < _wavelength < 10):
        raise ValueError(
            f"wavelength={_wavelength} m — "
            f"implausible for 5G (expected mm–cm range)")
    if sc not in (15e3, 30e3, 60e3, 120e3, 240e3):
        raise ValueError(
            f"scs={sc} Hz — not a standard NR numerology")
    if not (0 <= sp < 500):
        raise ValueError(
            f"Speed={sp} m/s — out of range "
            f"(0=static, ~140=high-speed train)")
    print(f"[v8 Physics] OK: fc={cf/1e9:.2f} GHz, λ={_wavelength*100:.2f} cm, "
          f"scs={sc/1e3:.0f} kHz, v={sp} m/s")


def radian_to_degree(radian):
    return radian * (180.0 / PI)

def degree_to_radian(degree):
    return degree * (PI / 180.0)

def set_BS(location=[0,0,0], rotation=[0,0], num_rows_per_panel=1, num_cols_per_panel=1, num_rows=1, num_cols=1,
           polarization="single", polarization_type="V", antenna_pattern="38.901",
           panel_vertical_spacing=2.5, panel_horizontal_spacing=2.5):
    BSexample = {
        "location": location, "rotation": rotation,
        "num_rows_per_panel": num_rows_per_panel, "num_cols_per_panel": num_cols_per_panel,
        "num_rows": num_rows, "num_cols": num_cols,
        "polarization": polarization, "polarization_type": polarization_type,
        "antenna_pattern": antenna_pattern,
        "panel_vertical_spacing": panel_vertical_spacing,
        "panel_horizontal_spacing": panel_horizontal_spacing
    }
    tx_antennas = int(BSexample["num_rows_per_panel"] * BSexample["num_cols_per_panel"] *
                      BSexample["num_rows"] * BSexample["num_cols"])
    return BSexample, tx_antennas

def get_ofdm_symbol_indices(total_samples):
    indices = []
    idx = 0
    for s in SYMBOL_SIZES:
        if idx + s > total_samples:
            break
        indices.append((idx, idx + s))
        idx += s
    return indices


# ============================================================================
# WindowProfiler (ported from v11)
# ============================================================================

class WindowProfiler:
    """Rolling-window latency statistics (avg / p95 / p99 / max)."""
    def __init__(self, name, metrics, window=500, report_interval=100):
        self.name = name
        self.metrics = metrics
        self.window = int(max(10, window))
        self.report_interval = int(max(1, report_interval))
        self.samples = 0
        self.buffers = {m: deque(maxlen=self.window) for m in self.metrics}

    def add(self, tag="", **kwargs):
        self.samples += 1
        for key in self.metrics:
            if key in kwargs and kwargs[key] is not None:
                self.buffers[key].append(float(kwargs[key]))
        if self.samples % self.report_interval == 0:
            self.print_report(tag=tag)

    def _fmt(self, values):
        if not values:
            return "n/a"
        arr = np.asarray(values, dtype=np.float64)
        return (f"avg={arr.mean():.3f} p95={np.percentile(arr,95):.3f} "
                f"p99={np.percentile(arr,99):.3f} max={arr.max():.3f} ms")

    def print_report(self, tag=""):
        tag_txt = f" {tag}" if tag else ""
        print(f"\n[PROFILE {self.name}#{self.samples}{tag_txt}] window={self.window}")
        for key in self.metrics:
            print(f"  - {key:<14} {self._fmt(self.buffers[key])}")


# ============================================================================
# GPU IPC Interface
# ============================================================================

GPU_IPC_V6_SHM_PATH = "/tmp/oai_gpu_ipc/gpu_ipc_shm"
GPU_IPC_V6_MAGIC = 0x47505537
GPU_IPC_V6_VERSION = 1
GPU_IPC_V6_HANDLE_SIZE = 64
GPU_IPC_V6_SHM_SIZE = 4096
GPU_IPC_V6_CIR_TIME = 460800
GPU_IPC_V6_SAMPLE_SIZE = 4

GPU_IPC_V7_SHM_PATH = "/tmp/oai_gpu_ipc/gpu_ipc_shm"
GPU_IPC_V7_MAGIC = 0x47505538
GPU_IPC_V7_VERSION = 1
GPU_IPC_V7_HANDLE_SIZE = 64
GPU_IPC_V7_SHM_SIZE = 4096
GPU_IPC_V7_CIR_TIME = 460800
GPU_IPC_V7_SAMPLE_SIZE = 4
GPU_IPC_V7_OFF_DL_TX_SEQ = 368
GPU_IPC_V7_OFF_DL_RX_SEQ = 372
GPU_IPC_V7_OFF_UL_TX_SEQ = 376
GPU_IPC_V7_OFF_UL_RX_SEQ = 380

GPU_IPC_V8_SHM_PATH = GPU_IPC_V7_SHM_PATH
GPU_IPC_V8_MAGIC = GPU_IPC_V7_MAGIC
GPU_IPC_V8_VERSION = GPU_IPC_V7_VERSION
GPU_IPC_V8_HANDLE_SIZE = GPU_IPC_V7_HANDLE_SIZE
GPU_IPC_V8_SHM_SIZE = GPU_IPC_V7_SHM_SIZE
GPU_IPC_V8_CIR_TIME = GPU_IPC_V7_CIR_TIME
GPU_IPC_V8_SAMPLE_SIZE = GPU_IPC_V7_SAMPLE_SIZE
GPU_IPC_V8_OFF_DL_TX_SEQ = GPU_IPC_V7_OFF_DL_TX_SEQ
GPU_IPC_V8_OFF_DL_RX_SEQ = GPU_IPC_V7_OFF_DL_RX_SEQ
GPU_IPC_V8_OFF_UL_TX_SEQ = GPU_IPC_V7_OFF_UL_TX_SEQ
GPU_IPC_V8_OFF_UL_RX_SEQ = GPU_IPC_V7_OFF_UL_RX_SEQ

import sys as _sys
SYS_futex = 202 if _sys.maxsize > 2**32 else 240
FUTEX_WAKE = 1
_libc = ctypes.CDLL("libc.so.6", use_errno=True)

GPU_IPC_SHM_PATH = GPU_IPC_V7_SHM_PATH

GNB_ANT = int(os.environ.get('GPU_IPC_V5_GNB_ANT', '1'))
UE_ANT = int(os.environ.get('GPU_IPC_V5_UE_ANT', '1'))
GNB_NX = int(os.environ.get('GPU_IPC_V5_GNB_NX', '1'))
GNB_NY = int(os.environ.get('GPU_IPC_V5_GNB_NY', '1'))
UE_NX = int(os.environ.get('GPU_IPC_V5_UE_NX', '1'))
UE_NY = int(os.environ.get('GPU_IPC_V5_UE_NY', '1'))


class GPUIpcV6Interface:
    """
    GPU IPC V6 Per-Buffer Antenna interface — SERVER role (Proxy).

    Each of the 4 buffers has its own nbAnt and cir_size, enabling
    asymmetric MIMO (gNB and UE with different antenna counts).

    Buffer mapping:
      dl_tx: gNB writes (nbAnt=GNB_ANT) → Proxy reads
      dl_rx: Proxy writes → UE reads (nbAnt=UE_ANT)
      ul_tx: UE writes (nbAnt=UE_ANT) → Proxy reads
      ul_rx: Proxy writes → gNB reads (nbAnt=GNB_ANT)
    """

    def __init__(self, gnb_ant=1, ue_ant=1, cir_time=GPU_IPC_V6_CIR_TIME,
                 shm_path=GPU_IPC_V6_SHM_PATH):
        self.shm_path = shm_path
        self.shm_fd = None
        self.shm_mm = None
        self.gpu_dl_tx_ptr = 0
        self.gpu_dl_rx_ptr = 0
        self.gpu_ul_tx_ptr = 0
        self.gpu_ul_rx_ptr = 0
        self._gpu_mem = []
        self.gnb_ant = gnb_ant
        self.ue_ant = ue_ant
        self.cir_time = cir_time
        self.dl_tx_nbAnt = gnb_ant
        self.dl_tx_cir_size = cir_time * gnb_ant
        self.dl_rx_nbAnt = ue_ant
        self.dl_rx_cir_size = cir_time * ue_ant
        self.ul_tx_nbAnt = ue_ant
        self.ul_tx_cir_size = cir_time * ue_ant
        self.ul_rx_nbAnt = gnb_ant
        self.ul_rx_cir_size = cir_time * gnb_ant
        self.initialized = False

    def init(self):
        """Allocate 4 GPU circular buffers with per-buffer sizes."""
        shm_dir = os.path.dirname(self.shm_path)
        os.makedirs(shm_dir, mode=0o777, exist_ok=True)

        self.shm_fd = os.open(self.shm_path,
                              os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o666)
        os.ftruncate(self.shm_fd, GPU_IPC_V6_SHM_SIZE)
        self.shm_mm = mmap.mmap(self.shm_fd, GPU_IPC_V6_SHM_SIZE)
        self.shm_mm[:] = b'\x00' * GPU_IPC_V6_SHM_SIZE

        buf_configs = [
            ('dl_tx', 0,   self.dl_tx_cir_size),
            ('dl_rx', 64,  self.dl_rx_cir_size),
            ('ul_tx', 128, self.ul_tx_cir_size),
            ('ul_rx', 192, self.ul_rx_cir_size),
        ]
        ptrs = []
        for name, handle_off, cir_sz in buf_configs:
            buf_bytes = cir_sz * GPU_IPC_V6_SAMPLE_SIZE
            mem = cp.cuda.alloc(buf_bytes)
            self._gpu_mem.append(mem)
            ptr = mem.ptr
            ptrs.append(ptr)
            cp.cuda.runtime.memset(ptr, 0, buf_bytes)
            handle_bytes = cp.cuda.runtime.ipcGetMemHandle(ptr)
            self.shm_mm[handle_off:handle_off + GPU_IPC_V6_HANDLE_SIZE] = handle_bytes
            print(f"[GPU IPC V6] SERVER: allocated {name} "
                  f"({buf_bytes} bytes, cir_size={cir_sz}, ptr=0x{ptr:x})")

        self.gpu_dl_tx_ptr = ptrs[0]
        self.gpu_dl_rx_ptr = ptrs[1]
        self.gpu_ul_tx_ptr = ptrs[2]
        self.gpu_ul_rx_ptr = ptrs[3]

        struct.pack_into('<I', self.shm_mm, 264, self.cir_time)
        struct.pack_into('<I', self.shm_mm, 268, 1)
        struct.pack_into('<I', self.shm_mm, 272, self.dl_tx_nbAnt)
        struct.pack_into('<I', self.shm_mm, 276, self.dl_tx_cir_size)
        struct.pack_into('<I', self.shm_mm, 280, self.dl_rx_nbAnt)
        struct.pack_into('<I', self.shm_mm, 284, self.dl_rx_cir_size)
        struct.pack_into('<I', self.shm_mm, 288, self.ul_tx_nbAnt)
        struct.pack_into('<I', self.shm_mm, 292, self.ul_tx_cir_size)
        struct.pack_into('<I', self.shm_mm, 296, self.ul_rx_nbAnt)
        struct.pack_into('<I', self.shm_mm, 300, self.ul_rx_cir_size)
        struct.pack_into('<I', self.shm_mm, 260, GPU_IPC_V6_VERSION)
        self.shm_mm.flush()
        struct.pack_into('<I', self.shm_mm, 256, GPU_IPC_V6_MAGIC)
        self.shm_mm.flush()

        self.initialized = True
        print(f"[GPU IPC V6] SERVER: ready (magic=0x{GPU_IPC_V6_MAGIC:08X}, "
              f"version={GPU_IPC_V6_VERSION}, gnb_ant={self.gnb_ant}, ue_ant={self.ue_ant}, "
              f"cir_time={self.cir_time})")
        return True

    def circ_offset(self, ts, nbAnt, cir_size):
        return int((ts * nbAnt) % cir_size)

    def get_gpu_array_at(self, base_ptr, ts, nsamps, nbAnt, cir_size, dtype=cp.int16):
        off = self.circ_offset(ts, nbAnt, cir_size)
        total = nsamps * nbAnt
        elem_size = dtype().itemsize
        if off + total <= cir_size:
            byte_off = off * GPU_IPC_V6_SAMPLE_SIZE
            n_elem = (total * GPU_IPC_V6_SAMPLE_SIZE) // elem_size
            mem = cp.cuda.UnownedMemory(base_ptr + byte_off,
                                        total * GPU_IPC_V6_SAMPLE_SIZE, owner=None)
            return cp.ndarray(n_elem, dtype=dtype, memptr=cp.cuda.MemoryPointer(mem, 0)), False
        else:
            return None, True

    def gpu_circ_copy(self, dst_ptr, src_ptr, ts, nsamps, nbAnt, cir_size):
        off = self.circ_offset(ts, nbAnt, cir_size)
        total = nsamps * nbAnt
        sample_sz = GPU_IPC_V6_SAMPLE_SIZE
        if off + total <= cir_size:
            n_int16 = (total * sample_sz) // 2
            dst = self._make_gpu_array(dst_ptr, off * sample_sz, n_int16)
            src = self._make_gpu_array(src_ptr, off * sample_sz, n_int16)
            dst[:] = src[:]
        else:
            tail = cir_size - off
            head = total - tail
            tail_n = (tail * sample_sz) // 2
            head_n = (head * sample_sz) // 2
            dst_t = self._make_gpu_array(dst_ptr, off * sample_sz, tail_n)
            src_t = self._make_gpu_array(src_ptr, off * sample_sz, tail_n)
            dst_t[:] = src_t[:]
            dst_h = self._make_gpu_array(dst_ptr, 0, head_n)
            src_h = self._make_gpu_array(src_ptr, 0, head_n)
            dst_h[:] = src_h[:]
        cp.cuda.Stream.null.synchronize()

    def bypass_copy(self, dst_ptr, src_ptr, ts, nsamps,
                    src_nbAnt, src_cir_size, dst_nbAnt, dst_cir_size):
        """Bypass copy with antenna mapping. Symmetric=direct copy, asymmetric=truncate/pad."""
        if src_nbAnt == dst_nbAnt:
            self.gpu_circ_copy(dst_ptr, src_ptr, ts, nsamps, src_nbAnt, src_cir_size)
        else:
            src_off = self.circ_offset(ts, src_nbAnt, src_cir_size)
            src_total = nsamps * src_nbAnt
            sample_sz = GPU_IPC_V6_SAMPLE_SIZE
            min_ant = min(src_nbAnt, dst_nbAnt)
            if src_off + src_total <= src_cir_size:
                src_arr = self._make_gpu_array(src_ptr, src_off * sample_sz,
                                               (src_total * sample_sz) // 2)
                src_2d = src_arr.view(cp.int16).reshape(nsamps, src_nbAnt * 2)
            else:
                tail = src_cir_size - src_off
                head = src_total - tail
                tail_arr = self._make_gpu_array(src_ptr, src_off * sample_sz, (tail * sample_sz) // 2)
                head_arr = self._make_gpu_array(src_ptr, 0, (head * sample_sz) // 2)
                src_arr = cp.concatenate([tail_arr, head_arr])
                src_2d = src_arr.view(cp.int16).reshape(nsamps, src_nbAnt * 2)

            # v7 #7: lazily allocate (or grow) a reusable destination buffer
            # instead of cp.zeros() per call.
            _key = (nsamps, dst_nbAnt)
            _existing = getattr(self, '_bypass_dst_2d', None)
            if _existing is None or self._bypass_dst_key != _key:
                self._bypass_dst_2d = cp.zeros((nsamps, dst_nbAnt * 2), dtype=cp.int16)
                self._bypass_dst_key = _key
            dst_2d = self._bypass_dst_2d
            dst_2d[:, :min_ant * 2] = src_2d[:, :min_ant * 2]
            if min_ant < dst_nbAnt:
                dst_2d[:, min_ant * 2:] = 0   # zero-pad extra antennas
            dst_flat = dst_2d.ravel()

            dst_off = self.circ_offset(ts, dst_nbAnt, dst_cir_size)
            dst_total = nsamps * dst_nbAnt
            if dst_off + dst_total <= dst_cir_size:
                dst_arr = self._make_gpu_array(dst_ptr, dst_off * sample_sz,
                                               (dst_total * sample_sz) // 2)
                dst_arr[:] = dst_flat
            else:
                tail_d = dst_cir_size - dst_off
                head_d = dst_total - tail_d
                dst_t = self._make_gpu_array(dst_ptr, dst_off * sample_sz, (tail_d * sample_sz) // 2)
                dst_t[:] = dst_flat[:(tail_d * sample_sz) // 2]
                dst_h = self._make_gpu_array(dst_ptr, 0, (head_d * sample_sz) // 2)
                dst_h[:] = dst_flat[(tail_d * sample_sz) // 2:]
            cp.cuda.Stream.null.synchronize()

    def _make_gpu_array(self, base_ptr, byte_offset, n_int16):
        mem = cp.cuda.UnownedMemory(base_ptr + byte_offset,
                                    n_int16 * 2, owner=None)
        return cp.ndarray(n_int16, dtype=cp.int16,
                          memptr=cp.cuda.MemoryPointer(mem, 0))

    def read_circ_to_linear(self, base_ptr, ts, nsamps, nbAnt, cir_size,
                            dtype=cp.int16):
        """Read from circular GPU buffer into a contiguous linear array.
        Handles wrap-around transparently — never returns None."""
        off = self.circ_offset(ts, nbAnt, cir_size)
        total = nsamps * nbAnt
        sample_sz = GPU_IPC_V6_SAMPLE_SIZE
        elem_size = dtype().itemsize
        n_elem = (total * sample_sz) // elem_size

        if off + total <= cir_size:
            byte_off = off * sample_sz
            mem = cp.cuda.UnownedMemory(base_ptr + byte_off,
                                        total * sample_sz, owner=None)
            return cp.ndarray(n_elem, dtype=dtype,
                              memptr=cp.cuda.MemoryPointer(mem, 0))

        tail = cir_size - off
        head = total - tail
        tail_n = (tail * sample_sz) // elem_size
        head_n = (head * sample_sz) // elem_size
        buf = cp.empty(n_elem, dtype=dtype)
        tail_arr = self._make_gpu_array(base_ptr, off * sample_sz,
                                        (tail * sample_sz) // 2)
        head_arr = self._make_gpu_array(base_ptr, 0,
                                        (head * sample_sz) // 2)
        buf_i16 = buf.view(cp.int16)
        tail_n16 = (tail * sample_sz) // 2
        head_n16 = (head * sample_sz) // 2
        buf_i16[:tail_n16] = tail_arr[:]
        buf_i16[tail_n16:tail_n16 + head_n16] = head_arr[:]
        return buf

    def write_linear_to_circ(self, base_ptr, ts, nsamps, nbAnt, cir_size,
                             data):
        """Write a contiguous linear array into a circular GPU buffer.
        Handles wrap-around transparently."""
        off = self.circ_offset(ts, nbAnt, cir_size)
        total = nsamps * nbAnt
        sample_sz = GPU_IPC_V6_SAMPLE_SIZE

        if off + total <= cir_size:
            n_int16 = (total * sample_sz) // 2
            dst = self._make_gpu_array(base_ptr, off * sample_sz, n_int16)
            dst[:] = data[:n_int16]
        else:
            tail = cir_size - off
            head = total - tail
            tail_n = (tail * sample_sz) // 2
            head_n = (head * sample_sz) // 2
            dst_tail = self._make_gpu_array(base_ptr, off * sample_sz, tail_n)
            dst_tail[:] = data[:tail_n]
            dst_head = self._make_gpu_array(base_ptr, 0, head_n)
            dst_head[:] = data[tail_n:tail_n + head_n]
        cp.cuda.Stream.null.synchronize()

    def read_shm_field(self, offset, fmt):
        return struct.unpack_from(fmt, self.shm_mm, offset)[0]

    def write_shm_field(self, offset, fmt, value):
        struct.pack_into(fmt, self.shm_mm, offset, value)
        self.shm_mm.flush()

    def get_last_dl_tx_ts(self):
        return self.read_shm_field(304, '<Q')

    def get_last_dl_tx_nsamps(self):
        return self.read_shm_field(312, '<I')

    def set_last_dl_rx_ts(self, ts):
        self.write_shm_field(320, '<Q', ts)

    def get_last_ul_tx_ts(self):
        return self.read_shm_field(328, '<Q')

    def get_last_ul_tx_nsamps(self):
        return self.read_shm_field(336, '<I')

    def set_last_ul_rx_ts(self, ts):
        self.write_shm_field(344, '<Q', ts)

    def cleanup(self):
        if not self.initialized:
            return
        self._gpu_mem.clear()
        self.gpu_dl_tx_ptr = 0
        self.gpu_dl_rx_ptr = 0
        self.gpu_ul_tx_ptr = 0
        self.gpu_ul_rx_ptr = 0
        try:
            if self.shm_mm:
                self.shm_mm.close()
        except BufferError:
            pass
        if self.shm_fd is not None:
            os.close(self.shm_fd)
        try:
            os.unlink(self.shm_path)
        except OSError:
            pass
        self.initialized = False
        print("[GPU IPC V6] Cleanup done")


class GPUIpcV7Interface(GPUIpcV6Interface):
    """GPU IPC V7 — V6 + futex sequence counters for wake notification.
    Proxy only calls futex_wake (never futex_wait)."""

    def __init__(self, gnb_ant=1, ue_ant=1, cir_time=GPU_IPC_V7_CIR_TIME,
                 shm_path=GPU_IPC_V7_SHM_PATH):
        super().__init__(gnb_ant=gnb_ant, ue_ant=ue_ant,
                         cir_time=cir_time, shm_path=shm_path)

    def init(self):
        shm_dir = os.path.dirname(self.shm_path)
        os.makedirs(shm_dir, mode=0o777, exist_ok=True)

        self.shm_fd = os.open(self.shm_path,
                              os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o666)
        os.ftruncate(self.shm_fd, GPU_IPC_V7_SHM_SIZE)
        self.shm_mm = mmap.mmap(self.shm_fd, GPU_IPC_V7_SHM_SIZE)
        self.shm_mm[:] = b'\x00' * GPU_IPC_V7_SHM_SIZE

        buf_configs = [
            ('dl_tx', 0,   self.dl_tx_cir_size),
            ('dl_rx', 64,  self.dl_rx_cir_size),
            ('ul_tx', 128, self.ul_tx_cir_size),
            ('ul_rx', 192, self.ul_rx_cir_size),
        ]
        ptrs = []
        for name, handle_off, cir_sz in buf_configs:
            buf_bytes = cir_sz * GPU_IPC_V7_SAMPLE_SIZE
            mem = cp.cuda.alloc(buf_bytes)
            self._gpu_mem.append(mem)
            ptr = mem.ptr
            ptrs.append(ptr)
            cp.cuda.runtime.memset(ptr, 0, buf_bytes)
            handle_bytes = cp.cuda.runtime.ipcGetMemHandle(ptr)
            self.shm_mm[handle_off:handle_off + GPU_IPC_V7_HANDLE_SIZE] = handle_bytes
            print(f"[GPU IPC V7] SERVER: allocated {name} "
                  f"({buf_bytes} bytes, cir_size={cir_sz}, ptr=0x{ptr:x})")

        self.gpu_dl_tx_ptr = ptrs[0]
        self.gpu_dl_rx_ptr = ptrs[1]
        self.gpu_ul_tx_ptr = ptrs[2]
        self.gpu_ul_rx_ptr = ptrs[3]

        struct.pack_into('<I', self.shm_mm, 264, self.cir_time)
        struct.pack_into('<I', self.shm_mm, 268, 1)
        struct.pack_into('<I', self.shm_mm, 272, self.dl_tx_nbAnt)
        struct.pack_into('<I', self.shm_mm, 276, self.dl_tx_cir_size)
        struct.pack_into('<I', self.shm_mm, 280, self.dl_rx_nbAnt)
        struct.pack_into('<I', self.shm_mm, 284, self.dl_rx_cir_size)
        struct.pack_into('<I', self.shm_mm, 288, self.ul_tx_nbAnt)
        struct.pack_into('<I', self.shm_mm, 292, self.ul_tx_cir_size)
        struct.pack_into('<I', self.shm_mm, 296, self.ul_rx_nbAnt)
        struct.pack_into('<I', self.shm_mm, 300, self.ul_rx_cir_size)
        # Zero out seq counters
        for off in (GPU_IPC_V7_OFF_DL_TX_SEQ, GPU_IPC_V7_OFF_DL_RX_SEQ,
                    GPU_IPC_V7_OFF_UL_TX_SEQ, GPU_IPC_V7_OFF_UL_RX_SEQ):
            struct.pack_into('<I', self.shm_mm, off, 0)
        struct.pack_into('<I', self.shm_mm, 260, GPU_IPC_V7_VERSION)
        self.shm_mm.flush()
        struct.pack_into('<I', self.shm_mm, 256, GPU_IPC_V7_MAGIC)
        self.shm_mm.flush()

        self.initialized = True
        print(f"[GPU IPC V7] SERVER: ready (magic=0x{GPU_IPC_V7_MAGIC:08X}, "
              f"version={GPU_IPC_V7_VERSION}, gnb_ant={self.gnb_ant}, ue_ant={self.ue_ant}, "
              f"cir_time={self.cir_time}, futex=enabled)")
        return True

    def _futex_wake(self, seq_offset):
        """Increment seq counter in SHM and issue futex WAKE."""
        cur = struct.unpack_from('<I', self.shm_mm, seq_offset)[0]
        struct.pack_into('<I', self.shm_mm, seq_offset, cur + 1)
        self.shm_mm.flush()
        addr = ctypes.c_void_p(ctypes.addressof(
            ctypes.c_char.from_buffer(self.shm_mm, seq_offset)))
        _libc.syscall(ctypes.c_long(SYS_futex),
                      addr, ctypes.c_int(FUTEX_WAKE),
                      ctypes.c_int(1),
                      ctypes.c_void_p(0), ctypes.c_void_p(0), ctypes.c_int(0))

    def set_last_dl_rx_ts(self, ts):
        """GPU write must be complete before calling this."""
        self.write_shm_field(320, '<Q', ts)
        self._futex_wake(GPU_IPC_V7_OFF_DL_RX_SEQ)

    def set_last_ul_rx_ts(self, ts):
        """GPU write must be complete before calling this."""
        self.write_shm_field(344, '<Q', ts)
        self._futex_wake(GPU_IPC_V7_OFF_UL_RX_SEQ)


# ============================================================================
# Fused clip+cast RawKernel for UL superposition output
# ============================================================================
if GPU_AVAILABLE:
    _fused_clip_cast_kernel = cp.RawKernel(r'''
    extern "C" __global__
    void fused_clip_cast(
        const double* accum_f64,
        short* out,
        int n_elem)
    {
        int idx = blockIdx.x * blockDim.x + threadIdx.x;
        if (idx >= n_elem) return;
        double r = round(accum_f64[2 * idx]);
        double i = round(accum_f64[2 * idx + 1]);
        r = fmin(fmax(r, -32768.0), 32767.0);
        i = fmin(fmax(i, -32768.0), 32767.0);
        out[2 * idx]     = (short)r;
        out[2 * idx + 1] = (short)i;
    }
    ''', 'fused_clip_cast')


# ============================================================================
# GT (Ground Truth) Batch Saver — saves Sionna H(f) for offline validation
# ============================================================================

@dataclass
class _GTToken:
    """Per-UE per-slot capture token created by stage_for_ue, consumed by commit_slot.

    Two flavours:
      kind='async': GPU→GPU staging done, awaiting copy_stream D2H + drain.
      kind='sync' : already a host-side numpy array (v7 behaviour or fallback).
    """
    kind: str
    ue_idx: int
    # async fields (kind='async' only)
    staging_gpu: object = None     # cp.ndarray, owned from GPU staging pool
    pinned_host: object = None     # np.ndarray view of pinned host mem, from pinned pool
    stage_event: object = None     # cp.cuda.Event recorded on default stream after GPU→GPU
    # sync fields (kind='sync' only)
    h_full: object = None          # np.ndarray complex64, ready to push to writer buffer


@dataclass
class _PendingD2H:
    """In-flight D2H task drained by _copy_drain_loop."""
    copy_event: object             # cp.cuda.Event recorded on copy_stream
    pinned_host: object            # np.ndarray (pinned)
    staging_gpu: object            # cp.ndarray (to be returned to pool)
    ue_idx: int
    slot_id: int
    partial_bypass: bool


class GTBatchSaver:
    """Saves Sionna UL channel H(f) matrices in batches for offline analysis.

    v8: end-to-end async pipeline.

      Main thread (in _ipc_ul_superposition_slot):
        try_begin_slot(ts) → (decision, slot_id)        # slot-level decision, taken once
        for k in ues:
            stage_for_ue(k, channels_ul) → _GTToken     # GPU→GPU + event sync (~10–30 μs)
            release_batch(...)                          # safe now (data is in staging_gpu)
        commit_slot(tokens, slot_id, partial_bypass)    # enqueue D2H, returns immediately

      Background copy_drain_loop:
        on each token: event.synchronize() → np.copy → return pool → push to writer buffer

      Background writer_loop (unchanged from v7):
        np.savez_compressed → disk

    Each batch file is a compressed .npz containing:
      h_matrix : complex64 array (BATCH_SIZE, n_sym_saved, gnb_ant, ue_ant, fft_size)
      slot_ids : uint32 array (BATCH_SIZE,) — slot id per frame
      bypass_flags : uint8 array (BATCH_SIZE,) — per-slot partial-bypass marker
      symbol_indices : uint32 array — which OFDM symbol indices were saved
      gnb_ant, ue_ant, fft_size : scalar metadata
    """
    BATCH_SIZE = 100
    _WRITE_QUEUE_MAX = 16   # bounded; if overrun, switch to sync to apply backpressure

    def __init__(self, gt_dir, num_ues, gnb_ant, ue_ant, fft_size=2048,
                 save_every=1, symbol_indices=None,
                 async_copy=True, pinned_pool_size=32, gpu_staging_pool_size=32):
        self.gt_dir = gt_dir
        self.num_ues = num_ues
        self.gnb_ant = gnb_ant
        self.ue_ant = ue_ant
        self.fft_size = fft_size
        self.save_every = max(1, int(save_every))
        self.symbol_indices = symbol_indices if symbol_indices is not None else [N_SYM - 1]
        self.enabled = bool(gt_dir)
        self.async_copy = bool(async_copy) and GPU_AVAILABLE
        self.pinned_pool_size = int(pinned_pool_size)
        self.gpu_staging_pool_size = int(gpu_staging_pool_size)

        if not self.enabled:
            return

        os.makedirs(gt_dir, exist_ok=True)
        self._buffers = {k: [] for k in range(num_ues)}
        self._seq = {k: 0 for k in range(num_ues)}
        # P1: per-UE locks protect _buffers / _seq mutation
        self._buffer_locks = {k: threading.Lock() for k in range(num_ues)}
        self._ul_slot_counter = 0

        # Writer queue (always present)
        import queue as _queue
        self._queue_mod = _queue
        self._write_q = _queue.Queue(maxsize=self._WRITE_QUEUE_MAX)
        self._writer_stop = threading.Event()
        self._writer = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer.start()
        self._sync_fallback_count = 0
        self._async_overflow_count = 0

        # Async-copy resources (only if async_copy=True and GPU available)
        if self.async_copy:
            sym_count = len(self.symbol_indices)
            self._capture_shape = (sym_count, gnb_ant, ue_ant, fft_size)
            self._capture_nbytes = int(np.prod(self._capture_shape)) * 8  # complex64 = 8 B

            # Dedicated cuda stream for D2H, runs in parallel with default stream
            self._copy_stream = cp.cuda.Stream(non_blocking=True)

            # Pre-allocate pinned host buffers (pool)
            self._pinned_pool = _queue.Queue(maxsize=self.pinned_pool_size)
            self._pinned_keepalive = []  # prevent GC of pinned mem objects
            for _ in range(self.pinned_pool_size):
                pmem = cp.cuda.alloc_pinned_memory(self._capture_nbytes)
                self._pinned_keepalive.append(pmem)
                arr = np.frombuffer(pmem, dtype=np.complex64,
                                    count=int(np.prod(self._capture_shape))).reshape(self._capture_shape)
                self._pinned_pool.put_nowait(arr)

            # Pre-allocate GPU staging buffers (pool)
            self._gpu_staging_pool = _queue.Queue(maxsize=self.gpu_staging_pool_size)
            for _ in range(self.gpu_staging_pool_size):
                self._gpu_staging_pool.put_nowait(
                    cp.empty(self._capture_shape, dtype=cp.complex64))

            # Drain queue: capacity slightly larger than pinned pool
            self._copy_q = _queue.Queue(maxsize=self.pinned_pool_size + 4)
            self._copy_drain_stop = threading.Event()
            self._copy_drain_thread = threading.Thread(
                target=self._copy_drain_loop, daemon=True)
            self._copy_drain_thread.start()

            # v8 diagnostic timing (ns); printed every _DIAG_PRINT_EVERY calls
            self._diag_stage_total_ns = []
            self._diag_stage_pool_ns = []
            self._diag_stage_copy_ns = []
            self._diag_stage_event_ns = []
            self._diag_commit_total_ns = []
            self._DIAG_PRINT_EVERY = 200
            self._diag_call_count = 0

            print(f"[GT Saver v8] Initialized (ASYNC): dir={gt_dir}, save_every={self.save_every}, "
                  f"batch={self.BATCH_SIZE}, symbols={self.symbol_indices}, "
                  f"pinned_pool={self.pinned_pool_size}, gpu_staging_pool={self.gpu_staging_pool_size}, "
                  f"writer cap={self._WRITE_QUEUE_MAX}")
        else:
            print(f"[GT Saver v8] Initialized (SYNC fallback): dir={gt_dir}, save_every={self.save_every}, "
                  f"batch={self.BATCH_SIZE}, symbols={self.symbol_indices}, "
                  f"writer cap={self._WRITE_QUEUE_MAX}")

    _SLOT_SAMPLES = sum(SYMBOL_SIZES)   # 30720 for mu=1

    # ────────────────────────── Slot-level decision ──────────────────────────

    def try_begin_slot(self, ipc_ts=None):
        """Increment UL-slot counter and decide whether to capture this slot.

        Returns (decision: bool, slot_id: int). slot_id is meaningful only if
        decision is True; otherwise 0.

        Must be called exactly once per UL slot, BEFORE any per-UE stage_for_ue.
        """
        if not self.enabled:
            return False, 0

        self._ul_slot_counter += 1
        if self._ul_slot_counter % self.save_every != 0:
            return False, 0

        if ipc_ts is not None:
            ts_int = int(ipc_ts)
            misalign = ts_int % self._SLOT_SAMPLES
            slot_id = ts_int // self._SLOT_SAMPLES
            if misalign != 0 and self._ul_slot_counter <= 20:
                print(f"[GT DIAG] ipc_ts={ts_int} not slot-aligned "
                      f"(misalign={misalign}, slot_id={slot_id})")
        else:
            slot_id = self._ul_slot_counter
        return True, slot_id

    # ────────────────────────── Stage (called BEFORE release_batch) ──────────

    def stage_for_ue(self, ue_idx, src_gpu):
        """Capture src_gpu[symbol_indices] into a per-token buffer.

        MUST be called BEFORE channel_buffers[ue_idx].release_batch(...) so that
        ChannelProducer cannot overwrite the source while we read it.

        Returns _GTToken (kind='async' or 'sync'). Never returns None on the
        happy path: if the async pool is exhausted, falls back to sync .get().
        """
        sym_idx = self.symbol_indices

        # CPU path or async disabled → sync
        if not self.async_copy or not hasattr(src_gpu, 'data'):
            if hasattr(src_gpu, 'get'):
                # cupy → host. WARNING: this synchronises the default stream
                # (v7 behaviour, used here as fallback only).
                h_full = src_gpu[sym_idx].get().astype(np.complex64)
            else:
                h_full = np.asarray(src_gpu[sym_idx], dtype=np.complex64)
            return _GTToken(kind='sync', ue_idx=ue_idx, h_full=h_full)

        # Async path
        _t0 = time.perf_counter_ns()
        try:
            staging_gpu = self._gpu_staging_pool.get_nowait()
        except self._queue_mod.Empty:
            staging_gpu = None
        try:
            pinned_host = self._pinned_pool.get_nowait()
        except self._queue_mod.Empty:
            pinned_host = None
        _t_pool = time.perf_counter_ns()

        if staging_gpu is None or pinned_host is None:
            # Pool exhausted → return whatever we did pull, fallback to sync
            if staging_gpu is not None:
                self._return_gpu_staging(staging_gpu)
            if pinned_host is not None:
                self._return_pinned(pinned_host)
            self._async_overflow_count += 1
            if self._async_overflow_count <= 3 or self._async_overflow_count % 100 == 0:
                print(f"[GT Saver v8] async pool exhausted, sync fallback "
                      f"(count={self._async_overflow_count}; consider larger "
                      f"--gt-pinned-pool-size / --gt-staging-gpu-buffers)")
            h_full = src_gpu[sym_idx].get().astype(np.complex64)
            return _GTToken(kind='sync', ue_idx=ue_idx, h_full=h_full)

        # 1) GPU→GPU on default stream (in-order with prior process_slot_ipc
        #    kernels). cupy enqueues the copy and returns immediately to CPU.
        # 2) Record an event on the default stream so the copy_stream can wait
        #    for it later via wait_event() (GPU-side sync only — CPU does NOT
        #    block here). This is the entire point of v8: the main thread is
        #    NEVER allowed to wait on a default-stream event, otherwise we'd
        #    serialize against the in-flight process_slot_ipc kernels (the
        #    exact bug we are fixing in v7).
        #
        # Safety vs release_batch():
        #   - Default-stream serial execution guarantees staging_gpu[:] runs
        #     AFTER process_slot_ipc kernels finish reading channels_ul.
        #   - release_batch() is a CPU-only marker; it does not preempt or
        #     reorder GPU kernels already enqueued on the default stream.
        #   - ChannelProducer (other process) writes the NEXT ring-buffer slot,
        #     not the same slot we just read; with buffer_len=42000 the wrap
        #     window is many seconds, far longer than this default-stream
        #     copy needs.
        try:
            staging_gpu[:] = src_gpu[sym_idx]
            _t_copy = time.perf_counter_ns()
            stage_event = cp.cuda.Event()
            stage_event.record()
            _t_event = time.perf_counter_ns()
        except Exception as e:
            # Anything goes wrong → return resources, fallback to sync
            self._return_gpu_staging(staging_gpu)
            self._return_pinned(pinned_host)
            self._async_overflow_count += 1
            print(f"[GT Saver v8] stage_for_ue failed ({type(e).__name__}: {e}), "
                  "falling back to sync .get()")
            h_full = src_gpu[sym_idx].get().astype(np.complex64)
            return _GTToken(kind='sync', ue_idx=ue_idx, h_full=h_full)

        # diagnostic timing accumulation (very cheap append)
        self._diag_stage_total_ns.append(_t_event - _t0)
        self._diag_stage_pool_ns.append(_t_pool - _t0)
        self._diag_stage_copy_ns.append(_t_copy - _t_pool)
        self._diag_stage_event_ns.append(_t_event - _t_copy)
        self._diag_call_count += 1
        if self._diag_call_count % self._DIAG_PRINT_EVERY == 0:
            self._print_diag()

        return _GTToken(kind='async', ue_idx=ue_idx,
                        staging_gpu=staging_gpu, pinned_host=pinned_host,
                        stage_event=stage_event)

    def _print_diag(self):
        """Print rolling timing stats for the last DIAG_PRINT_EVERY stage calls."""
        def _stats_us(ns_list):
            n = self._DIAG_PRINT_EVERY
            arr = ns_list[-n:]
            arr_sorted = sorted(arr)
            mean = sum(arr) / len(arr) / 1000.0  # → μs
            p50 = arr_sorted[len(arr_sorted) // 2] / 1000.0
            p99 = arr_sorted[int(len(arr_sorted) * 0.99)] / 1000.0
            mx = max(arr) / 1000.0
            return f"mean={mean:6.1f} p50={p50:6.1f} p99={p99:6.1f} max={mx:6.1f}"
        commit_str = ""
        if self._diag_commit_total_ns:
            commit_str = f"  commit_total: {_stats_us(self._diag_commit_total_ns)}"
        print(f"[GT Saver v8 DIAG] last {self._DIAG_PRINT_EVERY} stages (μs):  "
              f"total: {_stats_us(self._diag_stage_total_ns)}  |  "
              f"pool: {_stats_us(self._diag_stage_pool_ns)}  |  "
              f"copy: {_stats_us(self._diag_stage_copy_ns)}  |  "
              f"event: {_stats_us(self._diag_stage_event_ns)}{commit_str}")

    # ────────────────────────── Commit (called AFTER all UE release_batch) ───

    def commit_slot(self, tokens, slot_id, partial_bypass=False):
        """Hand off all tokens for this slot.

        async tokens → enqueue D2H on copy_stream + put on copy_drain queue.
        sync tokens  → push directly to per-UE writer buffer.
        """
        if not self.enabled:
            return

        _t0 = time.perf_counter_ns()
        for tok in tokens:
            if tok.kind == 'sync':
                self._enqueue_to_writer_buffer(
                    tok.ue_idx, slot_id, tok.h_full, partial_bypass)
                continue

            # async path
            try:
                with self._copy_stream:
                    # Make copy_stream wait (GPU-side, no CPU stall) for the
                    # default-stream staging copy to actually finish.  This is
                    # the ONLY synchronisation between the two streams; without
                    # it the D2H could race the GPU→GPU copy and read garbage.
                    self._copy_stream.wait_event(tok.stage_event)
                    cp.cuda.runtime.memcpyAsync(
                        tok.pinned_host.ctypes.data,
                        tok.staging_gpu.data.ptr,
                        tok.staging_gpu.nbytes,
                        cp.cuda.runtime.memcpyDeviceToHost,
                        self._copy_stream.ptr,
                    )
                    copy_event = cp.cuda.Event()
                    copy_event.record(self._copy_stream)
            except Exception as e:
                # Async D2H setup failed → return resources, sync fallback
                print(f"[GT Saver v8] async D2H enqueue failed "
                      f"({type(e).__name__}: {e}), sync fallback for ue{tok.ue_idx}")
                self._return_gpu_staging(tok.staging_gpu)
                self._return_pinned(tok.pinned_host)
                # Worst case: read from staging_gpu... but we already gave it
                # back. Use src is unsafe (may be overwritten). Best-effort:
                # mark this slot as bypassed for this UE.
                self._async_overflow_count += 1
                continue

            try:
                self._copy_q.put_nowait(_PendingD2H(
                    copy_event=copy_event,
                    pinned_host=tok.pinned_host,
                    staging_gpu=tok.staging_gpu,
                    ue_idx=tok.ue_idx,
                    slot_id=slot_id,
                    partial_bypass=partial_bypass,
                ))
            except self._queue_mod.Full:
                # Drain queue full → sync wait + push directly + return resources
                self._async_overflow_count += 1
                copy_event.synchronize()
                h_full = np.array(tok.pinned_host, dtype=np.complex64, copy=True)
                self._enqueue_to_writer_buffer(
                    tok.ue_idx, slot_id, h_full, partial_bypass)
                self._return_gpu_staging(tok.staging_gpu)
                self._return_pinned(tok.pinned_host)
        # diagnostic: total commit_slot wall-clock for this slot
        if hasattr(self, '_diag_commit_total_ns'):
            self._diag_commit_total_ns.append(time.perf_counter_ns() - _t0)

    # ────────────────────────── v7-compatible high-level API ────────────────

    def record_ul_slot(self, ue_channels, ipc_ts=None, partial_bypass=False):
        """v7-compatible single-call API: try_begin + stage_each + commit.

        NOTE: this convenience wrapper does NOT enforce stage-before-release
        ordering, so when the async pipeline is in use, the caller in
        _ipc_ul_superposition_slot should use try_begin_slot / stage_for_ue /
        commit_slot directly.  Provided here for backwards compatibility and
        for code paths that don't have a release_batch step.
        """
        if not self.enabled:
            return
        decision, slot_id = self.try_begin_slot(ipc_ts=ipc_ts)
        if not decision:
            return
        tokens = []
        for ue_idx, ch_ul in ue_channels:
            tokens.append(self.stage_for_ue(ue_idx, ch_ul))
        self.commit_slot(tokens, slot_id, partial_bypass=partial_bypass)

    # ────────────────────────── Pool helpers ─────────────────────────────────

    def _return_gpu_staging(self, staging_gpu):
        try:
            self._gpu_staging_pool.put_nowait(staging_gpu)
        except self._queue_mod.Full:
            pass  # let GC reclaim (rare, only if pool grew elsewhere)

    def _return_pinned(self, pinned_host):
        try:
            self._pinned_pool.put_nowait(pinned_host)
        except self._queue_mod.Full:
            pass

    # ────────────────────────── Writer buffer (thread-safe) ──────────────────

    def _enqueue_to_writer_buffer(self, ue_idx, slot_id, h_full, partial_bypass):
        """Append to per-UE batch buffer; flush if full. Thread-safe."""
        with self._buffer_locks[ue_idx]:
            self._buffers[ue_idx].append((slot_id, h_full, partial_bypass))
            if len(self._buffers[ue_idx]) >= self.BATCH_SIZE:
                # Build payload while holding the lock, but enqueue outside.
                payload, fname = self._build_payload_and_swap_locked(ue_idx)
        if 'payload' in locals() and payload is not None:
            self._enqueue_writer_payload(fname, payload)

    def _build_payload_and_swap_locked(self, ue_idx):
        """Build npz payload from current buffer + bump seq. MUST hold lock."""
        buf = self._buffers[ue_idx]
        if not buf:
            return None, None
        slot_ids = np.array([b[0] for b in buf], dtype=np.uint32)
        h_matrices = np.stack([b[1] for b in buf])
        bypass_flags = np.array([b[2] if len(b) > 2 else False
                                 for b in buf], dtype=np.uint8)
        seq = self._seq[ue_idx]
        fname = os.path.join(self.gt_dir, f"gt_batch_ue{ue_idx}_seq{seq}.npz")
        payload = dict(h_matrix=h_matrices,
                       slot_ids=slot_ids,
                       bypass_flags=bypass_flags,
                       symbol_indices=np.array(self.symbol_indices, dtype=np.uint32),
                       gnb_ant=np.uint32(self.gnb_ant),
                       ue_ant=np.uint32(self.ue_ant),
                       fft_size=np.uint32(self.fft_size))
        # Reset buffer + bump seq under the lock
        self._seq[ue_idx] = seq + 1
        self._buffers[ue_idx] = []
        return payload, fname

    def _enqueue_writer_payload(self, fname, payload):
        """Enqueue payload to writer; sync-fallback on overflow."""
        try:
            self._write_q.put_nowait((fname, payload))
        except self._queue_mod.Full:
            self._sync_fallback_count += 1
            if self._sync_fallback_count <= 3 or self._sync_fallback_count % 50 == 0:
                print(f"[GT Saver v8] write queue full, falling back to sync write "
                      f"(count={self._sync_fallback_count}). Consider larger "
                      f"--gt-save-every to reduce write rate.")
            np.savez_compressed(fname, **payload)

    # ────────────────────────── Background threads ───────────────────────────

    def _copy_drain_loop(self):
        """Background: wait for D2H events, push host data into writer buffer."""
        if GPU_AVAILABLE:
            try:
                cp.cuda.Device(0).use()
            except Exception:
                pass
        while not (self._copy_drain_stop.is_set() and self._copy_q.empty()):
            try:
                pending = self._copy_q.get(timeout=0.5)
            except self._queue_mod.Empty:
                continue
            if pending is None:
                self._copy_q.task_done()
                break
            try:
                # Block in background thread (does not stall main loop)
                pending.copy_event.synchronize()
                # Copy out of the pinned buffer so we can return it to the pool
                h_full = np.array(pending.pinned_host, dtype=np.complex64, copy=True)
                self._enqueue_to_writer_buffer(
                    pending.ue_idx, pending.slot_id, h_full, pending.partial_bypass)
            except Exception as e:
                print(f"[GT Saver v8] copy_drain error: {type(e).__name__}: {e}")
            finally:
                # Always return the resources to the pool, even on error
                self._return_gpu_staging(pending.staging_gpu)
                self._return_pinned(pending.pinned_host)
                self._copy_q.task_done()

    def _writer_loop(self):
        """Background: drains write queue, performs np.savez_compressed."""
        while not (self._writer_stop.is_set() and self._write_q.empty()):
            try:
                item = self._write_q.get(timeout=0.5)
            except Exception:
                continue
            if item is None:
                self._write_q.task_done()
                break
            fname, payload = item
            try:
                np.savez_compressed(fname, **payload)
                print(f"[GT Saver v8] async wrote {os.path.basename(fname)} "
                      f"(shape={payload['h_matrix'].shape})")
            except Exception as e:
                print(f"[GT Saver v8] async write failed: {fname} → {e}")
            finally:
                self._write_q.task_done()

    # ────────────────────────── Shutdown ─────────────────────────────────────

    def flush_all(self):
        """Drain all in-flight work and stop threads, in safe order."""
        if not self.enabled:
            return

        # 1) Wait for in-flight D2H drains to finish (they push to writer buffer)
        if self.async_copy:
            try:
                self._copy_q.join()
            except Exception:
                pass

        # 2) Flush remaining partial buffers (stage data → writer queue)
        for ue_idx in range(self.num_ues):
            with self._buffer_locks[ue_idx]:
                if self._buffers[ue_idx]:
                    payload, fname = self._build_payload_and_swap_locked(ue_idx)
            if 'payload' in locals() and payload is not None:
                self._enqueue_writer_payload(fname, payload)

        # 3) Wait for writer queue to fully drain (no timeout — disk may be slow)
        try:
            self._write_q.join()
        except Exception:
            pass

        # 4) Stop background threads
        if self.async_copy:
            self._copy_drain_stop.set()
            try:
                self._copy_q.put_nowait(None)
            except Exception:
                pass
            if self._copy_drain_thread.is_alive():
                self._copy_drain_thread.join(timeout=10.0)

        self._writer_stop.set()
        try:
            self._write_q.put_nowait(None)
        except Exception:
            pass
        if self._writer.is_alive():
            self._writer.join(timeout=30.0)

        total = sum(self._seq.values())
        if total > 0:
            print(f"[GT Saver v8] Final flush complete (total files: {total}, "
                  f"sync fallbacks: {self._sync_fallback_count}, "
                  f"async overflows: {self._async_overflow_count})")


# ============================================================================
# GPU Slot Pipeline (from v10, with IPC extensions)
# ============================================================================

class GPUSlotPipeline:
    """
    GPU Full Pipeline: v10 CUDA Graph + v12 GPU IPC mode

    Socket mode: int16 bytes in → GPU process → int16 bytes out (v10 behavior)
    IPC mode:    GPU int16 in → GPU process → GPU int16 out (zero-copy)
    """
    WARMUP_SLOTS = 3

    def __init__(self, fft_size=2048, enable_gpu=True, use_pinned_memory=True,
                 use_cuda_graph=True, profile_interval=100, profile_window=500,
                 dual_timer_compare=True, n_tx_in=1, n_rx_out=1,
                 noise_buffer=None):
        self.fft_size = fft_size
        self.n_tx = n_tx_in
        self.n_rx = n_rx_out
        self.noise_buffer = noise_buffer
        self.enable_gpu = enable_gpu and GPU_AVAILABLE
        self.use_pinned_memory = use_pinned_memory
        self.slot_counter = 0
        self.profile_interval = max(1, int(profile_interval))
        self.profile_window = max(10, int(profile_window))
        self.dual_timer_compare = bool(dual_timer_compare)

        _sock_metrics = ["H2D", "CH_COPY", "NOISE_PREP", "GPU_COMPUTE", "D2H", "TOTAL"]
        self.profile_gpu = WindowProfiler(
            "GPU_SLOT", _sock_metrics,
            window=self.profile_window, report_interval=self.profile_interval)
        self.profile_gpu_evt = WindowProfiler(
            "GPU_SLOT_EVT", _sock_metrics,
            window=self.profile_window, report_interval=self.profile_interval)
        self.profile_gpu_diff = WindowProfiler(
            "GPU_SLOT_CPU-EVT", _sock_metrics,
            window=self.profile_window, report_interval=self.profile_interval)

        _ipc_metrics = ["GPU_COPY_IN", "CH_COPY", "NOISE_PREP", "GPU_COMPUTE", "GPU_COPY_OUT", "TOTAL"]
        self.profile_ipc = WindowProfiler(
            "IPC_SLOT", _ipc_metrics,
            window=self.profile_window, report_interval=self.profile_interval)
        self.profile_ipc_evt = WindowProfiler(
            "IPC_SLOT_EVT", _ipc_metrics,
            window=self.profile_window, report_interval=self.profile_interval)
        self.profile_ipc_diff = WindowProfiler(
            "IPC_SLOT_CPU-EVT", _ipc_metrics,
            window=self.profile_window, report_interval=self.profile_interval)

        self.n_sym = N_SYM
        self.total_cpx = sum(SYMBOL_SIZES)
        self.total_int16_in = self.total_cpx * n_tx_in * 2
        self.total_int16_out = self.total_cpx * n_rx_out * 2
        self.total_int16 = self.total_int16_in

        if not self.enable_gpu:
            print("[GPU Pipeline] GPU disabled - CPU numpy mode")
            return

        print(f"[GPU Pipeline v6] MIMO CUDA Graph Pipeline + Dual Noise initializing (n_tx={n_tx_in}, n_rx={n_rx_out})...")
        print(f"[GPU Pipeline v6] Precision: complex128 (float64, PSS stability)")
        print(f"[GPU Pipeline v6] Profiling: interval={self.profile_interval}, "
              f"window={self.profile_window}, dual_timer={'ON' if self.dual_timer_compare else 'OFF'}")

        self.stream = cp.cuda.Stream(non_blocking=True)

        self.sym_bounds = get_ofdm_symbol_indices(self.total_cpx)

        if self.use_pinned_memory:
            self.pinned_iq_in_buf = cp.cuda.alloc_pinned_memory(self.total_int16_in * 2)
            self.pinned_iq_out_buf = cp.cuda.alloc_pinned_memory(self.total_int16_out * 2)
            self.pinned_iq_in = np.frombuffer(self.pinned_iq_in_buf, dtype=np.int16,
                                              count=self.total_int16_in)
            self.pinned_iq_out = np.frombuffer(self.pinned_iq_out_buf, dtype=np.int16,
                                               count=self.total_int16_out)

        self.gpu_iq_in = cp.zeros(self.total_int16_in, dtype=cp.int16)
        self.gpu_iq_out = cp.zeros(self.total_int16_out, dtype=cp.int16)
        self.gpu_x = cp.zeros((self.n_sym, n_tx_in, fft_size), dtype=cp.complex128)
        self.gpu_H = cp.zeros((self.n_sym, n_rx_out, n_tx_in, fft_size), dtype=cp.complex128)
        self.gpu_out = cp.zeros(self.total_cpx * n_rx_out, dtype=cp.complex128)
        self.gpu_noise_r = cp.zeros(self.total_cpx * n_rx_out, dtype=cp.float64)
        self.gpu_noise_i = cp.zeros(self.total_cpx * n_rx_out, dtype=cp.float64)

        self._buf_HX = cp.zeros((self.n_sym, n_rx_out, n_tx_in, fft_size), dtype=cp.complex128)
        self._buf_Yf = cp.zeros((self.n_sym, n_rx_out, fft_size), dtype=cp.complex128)
        self._buf_out_2d = cp.zeros((self.total_cpx, n_rx_out), dtype=cp.complex128)
        self._buf_iq_out_3d = cp.zeros((self.total_cpx, n_rx_out, 2), dtype=cp.float64)

        ext_idx = cp.zeros((N_SYM, fft_size), dtype=cp.int64)
        data_dst = []
        cp_dst_list = []
        cp_src_list = []
        for i, (s, e) in enumerate(self.sym_bounds):
            cp_l = CP_LENGTHS[i]
            ext_idx[i] = cp.arange(s + cp_l, e)
            data_dst.append(cp.arange(s + cp_l, e, dtype=cp.int64))
            cp_dst_list.append(cp.arange(s, s + cp_l, dtype=cp.int64))
            cp_src_list.append(cp.arange(
                i * fft_size + fft_size - cp_l,
                i * fft_size + fft_size, dtype=cp.int64))
        self.gpu_ext_idx = ext_idx
        self.gpu_data_dst = cp.concatenate(data_dst)
        self.gpu_cp_dst = cp.concatenate(cp_dst_list)
        self.gpu_cp_src = cp.concatenate(cp_src_list)

        self.use_cuda_graph = use_cuda_graph
        self.cuda_graph = None
        self.graph_captured = False
        self.warmup_count = 0
        self._graph_pl_linear = None
        self._graph_noise_on = None
        self._graph_snr_db = None
        self._graph_noise_std_abs = None
        # v7 #19: capture failure tracking (replaces permanent disable)
        self._capture_failure_count = 0
        self._MAX_CAPTURE_FAILURES = 5
        self._capture_disabled_logged = False

        print(f"[GPU Pipeline v7] Initialization complete (n_tx={self.n_tx}, n_rx={self.n_rx})")

    def _regenerate_noise(self):
        if self.noise_buffer is not None:
            noise_view, n_held = self.noise_buffer.get_batch_view(1)
            self.gpu_noise_r[:] = noise_view[0, 0]
            self.gpu_noise_i[:] = noise_view[0, 1]
            self.noise_buffer.release_batch(n_held)
        else:
            n = self.total_cpx * self.n_rx
            self.gpu_noise_r[:] = cp.random.randn(n).astype(cp.float64)
            self.gpu_noise_i[:] = cp.random.randn(n).astype(cp.float64)

    def _gpu_compute_core(self, pl_linear, snr_db, noise_on, noise_std_abs=None,
                           skip_quant=False):
        """MIMO channel application: de-interleave → broadcast*sum → re-interleave.
        All ops are CUDA Graph capturable (no cuBLAS, no dynamic alloc, no Python loops).
        gpu_H is already in frequency domain (FFT pre-computed by ChannelProducer).
        noise_std_abs: pre-computed absolute noise std (cp.float64) for dBFS mode, None for relative SNR.
        skip_quant: if True, skip the trailing clip+cast→int16 step (UL fast path; v7 #14).
        """
        n_tx = self.n_tx
        n_rx = self.n_rx

        # 1. De-interleave: flat int16 (sample-major, ant-minor) → complex per antenna
        self._tmp_f64 = self.gpu_iq_in.astype(cp.float64)
        self._tmp_iq_3d = self._tmp_f64.reshape(self.total_cpx, n_tx, 2)
        self._tmp_cpx_2d = self._tmp_iq_3d[:, :, 0] + 1j * self._tmp_iq_3d[:, :, 1]

        # 2. OFDM symbol extraction — GPU index array (no Python for-loop)
        self.gpu_x[:] = self._tmp_cpx_2d[self.gpu_ext_idx].transpose(0, 2, 1)

        # 3. FFT(signal) + broadcast multiply with Hf + sum (Hf pre-computed, no cuBLAS)
        self._tmp_Xf = cp.fft.fft(self.gpu_x, axis=-1)
        cp.multiply(self.gpu_H, self._tmp_Xf[:, cp.newaxis, :, :], out=self._buf_HX)
        cp.sum(self._buf_HX, axis=2, out=self._buf_Yf)
        self._tmp_y = cp.fft.ifft(self._buf_Yf, axis=-1)

        # 4. Reconstruct OFDM — GPU index scatter (no Python for-loop)
        self._buf_out_2d[:] = 0
        self._tmp_y_t = self._tmp_y.transpose(0, 2, 1)
        self._tmp_y_flat = self._tmp_y_t.reshape(-1, n_rx)
        self._buf_out_2d[self.gpu_data_dst] = self._tmp_y_flat
        self._buf_out_2d[self.gpu_cp_dst] = self._tmp_y_flat[self.gpu_cp_src]

        # 5. Path Loss + AWGN
        self.gpu_out[:] = self._buf_out_2d.ravel()
        # Always multiply (even if pl_linear==1.0) — no Python branch in graph;
        # cost of one multiply is negligible.
        self.gpu_out *= cp.float64(pl_linear)

        if noise_on:
            if noise_std_abs is not None:
                self._tmp_noise = noise_std_abs * (
                    self.gpu_noise_r + 1j * self.gpu_noise_i
                )
            else:
                self._tmp_abs_sq = cp.abs(self.gpu_out) ** 2
                self._tmp_sig_pwr = cp.mean(self._tmp_abs_sq)
                snr_linear = cp.float64(10.0 ** (snr_db / 10.0))
                self._tmp_n_pwr = self._tmp_sig_pwr / snr_linear
                self._tmp_n_std = cp.sqrt(self._tmp_n_pwr / cp.float64(2.0))
                self._tmp_noise = self._tmp_n_std * (
                    self.gpu_noise_r + 1j * self.gpu_noise_i
                )
            self.gpu_out += self._tmp_noise

        # 6. Re-interleave: (total_cpx, n_rx) → flat int16 (pre-allocated buffer)
        # v7 #14: skip when caller will discard quantized output (UL superposition).
        if not skip_quant:
            self._tmp_out_2d_final = self.gpu_out.reshape(self.total_cpx, n_rx)
            self._buf_iq_out_3d[:, :, 0] = cp.clip(cp.around(self._tmp_out_2d_final.real), -32768, 32767)
            self._buf_iq_out_3d[:, :, 1] = cp.clip(cp.around(self._tmp_out_2d_final.imag), -32768, 32767)
            self.gpu_iq_out[:] = self._buf_iq_out_3d.ravel().astype(cp.int16)

    def _try_capture_graph(self, pl_linear, snr_db, noise_on, noise_std_abs=None):
        # v7 #19: cap retry attempts; allow per-slot retries (not permanent disable)
        if self._capture_failure_count >= self._MAX_CAPTURE_FAILURES:
            if not self._capture_disabled_logged:
                print(f"[CUDA Graph] Disabled after {self._capture_failure_count} "
                      f"consecutive capture failures.")
                self._capture_disabled_logged = True
            return
        try:
            # v7 #3: Relaxed mode avoids errors when other streams (e.g.
            # NoiseProducer in default stream) issue CUDA work concurrently.
            try:
                _mode = cp.cuda.runtime.streamCaptureModeRelaxed
            except AttributeError:
                _mode = 2  # Relaxed=2 in CUDA runtime (fallback)
            try:
                self.stream.begin_capture(mode=_mode)
            except TypeError:
                # Older CuPy without mode arg
                self.stream.begin_capture()
            self._gpu_compute_core(pl_linear, snr_db, noise_on, noise_std_abs)
            self.cuda_graph = self.stream.end_capture()

            self.graph_captured = True
            self._graph_pl_linear = pl_linear
            self._graph_noise_on = noise_on
            self._graph_snr_db = snr_db
            self._graph_noise_std_abs = noise_std_abs
            self._capture_failure_count = 0   # reset on success

            _noise_info = "OFF"
            if noise_on:
                _noise_info = f"abs={noise_std_abs}" if noise_std_abs is not None else f"SNR={snr_db}dB"
            print(f"[CUDA Graph] Capture success (PL={pl_linear}, noise={_noise_info})")

        except Exception as e:
            try:
                self.stream.end_capture()
            except:
                pass
            try:
                cp.cuda.Device(0).synchronize()
            except:
                pass
            self._capture_failure_count += 1
            print(f"[CUDA Graph] Capture failed (attempt "
                  f"{self._capture_failure_count}/{self._MAX_CAPTURE_FAILURES}) "
                  f"- fallback to normal: {e}")
            self.graph_captured = False
            # NB: do NOT set self.use_cuda_graph = False — let next slot retry
            # until _MAX_CAPTURE_FAILURES is reached.

    def _need_recapture(self, pl_linear, snr_db, noise_on, noise_std_abs=None):
        if not self.graph_captured:
            return False
        return (self._graph_pl_linear != pl_linear or
                self._graph_noise_on != noise_on or
                self._graph_snr_db != snr_db or
                self._graph_noise_std_abs != noise_std_abs)

    def process_slot(self, iq_bytes, channels_gpu, pl_linear, snr_db, noise_on, noise_std_abs=None):
        """Socket mode: raw bytes in -> int16 bytes out (v10 compatible)"""
        n_iq = len(iq_bytes) // 2
        n_cpx = n_iq // 2

        if not self.enable_gpu or n_cpx != self.total_cpx:
            return self._cpu_fallback(iq_bytes, channels_gpu, pl_linear, snr_db, noise_on)

        do_profile = (self.slot_counter > 0
                      and self.slot_counter % self.profile_interval == 0)
        do_dual = do_profile and self.dual_timer_compare
        if do_profile:
            cp.cuda.Stream.null.synchronize()
        t0 = time.perf_counter()
        if do_dual:
            e_h2d_s, e_h2d_e = cp.cuda.Event(), cp.cuda.Event()
            e_ch_s, e_ch_e = cp.cuda.Event(), cp.cuda.Event()
            e_noise_s, e_noise_e = cp.cuda.Event(), cp.cuda.Event()
            e_gpu_s, e_gpu_e = cp.cuda.Event(), cp.cuda.Event()
            e_d2h_s, e_d2h_e = cp.cuda.Event(), cp.cuda.Event()

        with self.stream:
            if do_dual:
                e_h2d_s.record(self.stream)
            if self.use_pinned_memory:
                ctypes.memmove(self.pinned_iq_in.ctypes.data, iq_bytes, len(iq_bytes))
                self.gpu_iq_in.set(self.pinned_iq_in, stream=self.stream)
            else:
                iq_int16 = np.frombuffer(iq_bytes, dtype='<i2')
                self.gpu_iq_in[:] = cp.asarray(iq_int16)
            if do_dual:
                e_h2d_e.record(self.stream)

            if do_profile:
                self.stream.synchronize(); t1 = time.perf_counter()

            if do_dual:
                e_ch_s.record(self.stream)
            n_ch = min(channels_gpu.shape[0], self.n_sym)
            if n_ch < self.n_sym:
                self.gpu_H[n_ch:] = 0
            if channels_gpu.ndim == 4:
                n_w = min(channels_gpu.shape[3], self.fft_size)
                ch_slice = channels_gpu[:n_ch, :self.n_rx, :self.n_tx, :n_w]
            elif channels_gpu.ndim == 2:
                n_w = min(channels_gpu.shape[1], self.fft_size)
                ch_slice = channels_gpu[:n_ch, :n_w].reshape(n_ch, 1, 1, n_w)
            else:
                n_w = self.fft_size
                ch_slice = channels_gpu[:n_ch].reshape(n_ch, 1, 1, -1)[:, :, :, :n_w]
            self.gpu_H[:n_ch, :ch_slice.shape[1], :ch_slice.shape[2], :n_w] = ch_slice
            if do_dual:
                e_ch_e.record(self.stream)

            if do_profile:
                self.stream.synchronize(); t2 = time.perf_counter()

            if do_dual:
                e_noise_s.record(self.stream)
            if noise_on:
                self._regenerate_noise()
            if do_dual:
                e_noise_e.record(self.stream)

            if do_profile:
                self.stream.synchronize(); t_noise1 = time.perf_counter()

            if self._need_recapture(pl_linear, snr_db, noise_on, noise_std_abs):
                self.graph_captured = False
                self.warmup_count = self.WARMUP_SLOTS

            if do_dual:
                e_gpu_s.record(self.stream)
            if self.graph_captured:
                self.cuda_graph.launch(self.stream)
            elif self.use_cuda_graph and self.warmup_count >= self.WARMUP_SLOTS and self._capture_failure_count < self._MAX_CAPTURE_FAILURES:
                self._try_capture_graph(pl_linear, snr_db, noise_on, noise_std_abs)
                if self.graph_captured:
                    self.cuda_graph.launch(self.stream)
                else:
                    self._gpu_compute_core(pl_linear, snr_db, noise_on, noise_std_abs)
            else:
                self._gpu_compute_core(pl_linear, snr_db, noise_on, noise_std_abs)
                self.warmup_count += 1
            if do_dual:
                e_gpu_e.record(self.stream)

            if do_profile:
                self.stream.synchronize(); t3 = time.perf_counter()

            if do_dual:
                e_d2h_s.record(self.stream)
            if self.use_pinned_memory:
                self.gpu_iq_out.get(out=self.pinned_iq_out, stream=self.stream)
                if do_dual:
                    e_d2h_e.record(self.stream)
                self.stream.synchronize()
                result = self.pinned_iq_out.tobytes()
            else:
                out_host = self.gpu_iq_out.get(stream=self.stream)
                if do_dual:
                    e_d2h_e.record(self.stream)
                self.stream.synchronize()
                result = out_host.tobytes()

            if do_profile:
                t4 = time.perf_counter()

        self.slot_counter += 1
        if do_profile:
            mode = "GRAPH" if self.graph_captured else "NORMAL"
            cpu_h2d = 1000 * (t1 - t0)
            cpu_ch = 1000 * (t2 - t1)
            cpu_noise = 1000 * (t_noise1 - t2)
            cpu_gpu = 1000 * (t3 - t_noise1)
            cpu_d2h = 1000 * (t4 - t3)
            cpu_total = 1000 * (t4 - t0)
            self.profile_gpu.add(
                tag=f"mode={mode}",
                H2D=cpu_h2d, CH_COPY=cpu_ch, NOISE_PREP=cpu_noise,
                GPU_COMPUTE=cpu_gpu, D2H=cpu_d2h, TOTAL=cpu_total)
            if do_dual:
                evt_h2d = cp.cuda.get_elapsed_time(e_h2d_s, e_h2d_e)
                evt_ch = cp.cuda.get_elapsed_time(e_ch_s, e_ch_e)
                evt_noise = cp.cuda.get_elapsed_time(e_noise_s, e_noise_e)
                evt_gpu = cp.cuda.get_elapsed_time(e_gpu_s, e_gpu_e)
                evt_d2h = cp.cuda.get_elapsed_time(e_d2h_s, e_d2h_e)
                evt_total = evt_h2d + evt_ch + evt_noise + evt_gpu + evt_d2h
                self.profile_gpu_evt.add(
                    tag=f"mode={mode}",
                    H2D=evt_h2d, CH_COPY=evt_ch, NOISE_PREP=evt_noise,
                    GPU_COMPUTE=evt_gpu, D2H=evt_d2h, TOTAL=evt_total)
                self.profile_gpu_diff.add(
                    tag=f"mode={mode}",
                    H2D=cpu_h2d - evt_h2d, CH_COPY=cpu_ch - evt_ch,
                    NOISE_PREP=cpu_noise - evt_noise,
                    GPU_COMPUTE=cpu_gpu - evt_gpu,
                    D2H=cpu_d2h - evt_d2h, TOTAL=cpu_total - evt_total)
        return result

    def process_slot_ipc(self, gpu_iq_in_arr, channels_gpu, pl_linear, snr_db,
                         noise_on, gpu_iq_out_arr, noise_std_abs=None,
                         skip_quant=False):
        """
        GPU IPC mode: GPU array in -> GPU process -> GPU array out
        MIMO: channels_gpu shape = (N_SYM, N_r, N_t, FFT) or (N_SYM, FFT) for SISO compat.

        v7 #14: When ``skip_quant=True`` the trailing clip+cast→int16 step in
        ``_gpu_compute_core`` is bypassed and ``gpu_iq_out_arr`` is NOT
        written.  The caller should read ``self.gpu_out`` (complex128) directly
        — this is what UL superposition does, where re-quantization in each
        per-UE pass would be discarded anyway.
        """
        if not self.enable_gpu:
            return

        do_profile = (self.slot_counter > 0
                      and self.slot_counter % self.profile_interval == 0)
        do_dual = do_profile and self.dual_timer_compare
        if do_profile:
            cp.cuda.Stream.null.synchronize()
        t0 = time.perf_counter()
        if do_dual:
            e_cin_s, e_cin_e = cp.cuda.Event(), cp.cuda.Event()
            e_ch_s, e_ch_e = cp.cuda.Event(), cp.cuda.Event()
            e_noise_s, e_noise_e = cp.cuda.Event(), cp.cuda.Event()
            e_gpu_s, e_gpu_e = cp.cuda.Event(), cp.cuda.Event()
            e_cout_s, e_cout_e = cp.cuda.Event(), cp.cuda.Event()

        # When skip_quant, we bypass CUDA Graph (graph captures full pipeline
        # including quant; recapturing every time skip_quant flips would
        # thrash the warmup counter). Skip-quant path is the UL fast path.
        _use_graph_this_call = (not skip_quant)

        with self.stream:
            if do_dual:
                e_cin_s.record(self.stream)
            self.gpu_iq_in[:] = gpu_iq_in_arr[:self.total_int16_in]
            if do_dual:
                e_cin_e.record(self.stream)

            if do_profile:
                self.stream.synchronize(); t1 = time.perf_counter()

            if do_dual:
                e_ch_s.record(self.stream)
            n_ch = min(channels_gpu.shape[0], self.n_sym)
            if n_ch < self.n_sym:
                self.gpu_H[n_ch:] = 0
            if channels_gpu.ndim == 4:
                n_w = min(channels_gpu.shape[3], self.fft_size)
                ch_slice = channels_gpu[:n_ch, :self.n_rx, :self.n_tx, :n_w]
            elif channels_gpu.ndim == 2:
                n_w = min(channels_gpu.shape[1], self.fft_size)
                ch_slice = channels_gpu[:n_ch, :n_w].reshape(n_ch, 1, 1, n_w)
            else:
                n_w = self.fft_size
                ch_slice = channels_gpu[:n_ch].reshape(n_ch, 1, 1, -1)[:, :, :, :n_w]
            self.gpu_H[:n_ch, :ch_slice.shape[1], :ch_slice.shape[2], :n_w] = ch_slice
            if do_dual:
                e_ch_e.record(self.stream)

            if do_profile:
                self.stream.synchronize(); t2 = time.perf_counter()

            if do_dual:
                e_noise_s.record(self.stream)
            if noise_on:
                self._regenerate_noise()
            if do_dual:
                e_noise_e.record(self.stream)

            if do_profile:
                self.stream.synchronize(); t_noise1 = time.perf_counter()

            if _use_graph_this_call and self._need_recapture(pl_linear, snr_db, noise_on, noise_std_abs):
                self.graph_captured = False
                self.warmup_count = self.WARMUP_SLOTS

            if do_dual:
                e_gpu_s.record(self.stream)
            if _use_graph_this_call and self.graph_captured:
                self.cuda_graph.launch(self.stream)
            elif _use_graph_this_call and self.use_cuda_graph and self.warmup_count >= self.WARMUP_SLOTS and self._capture_failure_count < self._MAX_CAPTURE_FAILURES:
                self._try_capture_graph(pl_linear, snr_db, noise_on, noise_std_abs)
                if self.graph_captured:
                    self.cuda_graph.launch(self.stream)
                else:
                    self._gpu_compute_core(pl_linear, snr_db, noise_on, noise_std_abs,
                                            skip_quant=skip_quant)
            else:
                self._gpu_compute_core(pl_linear, snr_db, noise_on, noise_std_abs,
                                        skip_quant=skip_quant)
                if _use_graph_this_call:
                    self.warmup_count += 1
            if do_dual:
                e_gpu_e.record(self.stream)

            if do_profile:
                self.stream.synchronize(); t3 = time.perf_counter()

            if do_dual:
                e_cout_s.record(self.stream)
            if not skip_quant:
                gpu_iq_out_arr[:self.total_int16_out] = self.gpu_iq_out[:]
            if do_dual:
                e_cout_e.record(self.stream)
            self.stream.synchronize()

            if do_profile:
                t4 = time.perf_counter()

        self.slot_counter += 1
        if do_profile:
            mode = "GRAPH" if self.graph_captured else "NORMAL"
            cpu_cin = 1000 * (t1 - t0)
            cpu_ch = 1000 * (t2 - t1)
            cpu_noise = 1000 * (t_noise1 - t2)
            cpu_gpu = 1000 * (t3 - t_noise1)
            cpu_cout = 1000 * (t4 - t3)
            cpu_total = 1000 * (t4 - t0)
            self.profile_ipc.add(
                tag=f"mode={mode}",
                GPU_COPY_IN=cpu_cin, CH_COPY=cpu_ch, NOISE_PREP=cpu_noise,
                GPU_COMPUTE=cpu_gpu, GPU_COPY_OUT=cpu_cout, TOTAL=cpu_total)
            if do_dual:
                evt_cin = cp.cuda.get_elapsed_time(e_cin_s, e_cin_e)
                evt_ch = cp.cuda.get_elapsed_time(e_ch_s, e_ch_e)
                evt_noise = cp.cuda.get_elapsed_time(e_noise_s, e_noise_e)
                evt_gpu = cp.cuda.get_elapsed_time(e_gpu_s, e_gpu_e)
                evt_cout = cp.cuda.get_elapsed_time(e_cout_s, e_cout_e)
                evt_total = evt_cin + evt_ch + evt_noise + evt_gpu + evt_cout
                self.profile_ipc_evt.add(
                    tag=f"mode={mode}",
                    GPU_COPY_IN=evt_cin, CH_COPY=evt_ch, NOISE_PREP=evt_noise,
                    GPU_COMPUTE=evt_gpu, GPU_COPY_OUT=evt_cout, TOTAL=evt_total)
                self.profile_ipc_diff.add(
                    tag=f"mode={mode}",
                    GPU_COPY_IN=cpu_cin - evt_cin, CH_COPY=cpu_ch - evt_ch,
                    NOISE_PREP=cpu_noise - evt_noise,
                    GPU_COMPUTE=cpu_gpu - evt_gpu,
                    GPU_COPY_OUT=cpu_cout - evt_cout,
                    TOTAL=cpu_total - evt_total)

    def _cpu_fallback(self, iq_bytes, channels_gpu, pl_linear, snr_db, noise_on):
        iq_int16 = np.frombuffer(iq_bytes, dtype='<i2')
        x_cpx = iq_int16[::2].astype(np.float64) + 1j * iq_int16[1::2].astype(np.float64)
        n_cpx = len(x_cpx)
        sym_idx = get_ofdm_symbol_indices(n_cpx)

        if GPU_AVAILABLE and hasattr(channels_gpu, 'get'):
            ch_np = cp.asnumpy(channels_gpu)
        else:
            ch_np = np.asarray(channels_gpu)

        out = np.zeros(n_cpx, dtype=np.complex128)
        for i, (s, e) in enumerate(sym_idx):
            cp_l = CP_LENGTHS[i]
            sym = x_cpx[s + cp_l : e]
            h = ch_np[i] if i < ch_np.shape[0] else np.ones(self.fft_size, dtype=np.complex64)
            Xf = np.fft.fft(sym)
            Hf = np.fft.fft(h, self.fft_size)
            y = np.fft.ifft(Xf * Hf)
            out[s + cp_l : e] = y[:self.fft_size]
            out[s : s + cp_l] = y[self.fft_size - cp_l : self.fft_size]

        out *= pl_linear
        if noise_on and snr_db is not None:
            sp = np.mean(np.abs(out) ** 2)
            if sp > 0:
                ns = np.sqrt(sp / (10.0 ** (snr_db / 10.0)) / 2.0)
                out += ns * (np.random.randn(n_cpx) + 1j * np.random.randn(n_cpx))

        y16 = np.empty(n_cpx * 2, dtype='<i2')
        y16[::2] = np.clip(np.round(out.real), -32768, 32767).astype('<i2')
        y16[1::2] = np.clip(np.round(out.imag), -32768, 32767).astype('<i2')
        return y16.tobytes()


# ============================================================================
# Socket-mode support classes (v10 backward compatibility)
# ============================================================================

@dataclass
class Endpoint:
    sock:  socket.socket
    role:  str
    rx:    bytearray = field(default_factory=bytearray)
    stage: str = "hdr"
    pay_len: int = 0
    hdr_raw: bytes = b""
    hdr_vals: Optional[Tuple[int,int,int,int,int]] = None
    closed: bool = False

    def fileno(self):
        return self.sock.fileno()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.close()
        finally:
            pass

    def read_blocks(self):
        blocks = []
        try:
            chunk = self.sock.recv(65536)
        except BlockingIOError:
            return blocks
        except OSError:
            self.close()
            return blocks
        if not chunk:
            self.close()
            return blocks
        self.rx.extend(chunk)

        while True:
            if self.stage == "hdr":
                if len(self.rx) < HDR_LEN:
                    break
                self.hdr_raw = bytes(self.rx[:HDR_LEN])
                del self.rx[:HDR_LEN]
                size, nb, ts, frame, subframe = unpack_header(self.hdr_raw)
                self.hdr_vals = (size, nb, ts, frame, subframe)
                self.pay_len = size * nb * 4
                self.stage = "pay"

            if self.stage == "pay":
                if len(self.rx) < self.pay_len:
                    break
                payload = bytes(self.rx[:self.pay_len])
                del self.rx[:self.pay_len]
                blocks.append((self.hdr_raw, self.hdr_vals, payload))
                self.stage = "hdr"
                self.hdr_raw = b""
                self.hdr_vals = None
                self.pay_len = 0
        return blocks

    def send(self, h, p):
        try:
            self.sock.sendall(h+p)
        except OSError:
            self.close()


# ============================================================================
# RingBuffer (threading, for NoiseProducer — unchanged from v1)
# ============================================================================

class RingBuffer:
    def __init__(self, shape, dtype=cp.complex128, maxlen=1024):
        if GPU_AVAILABLE:
            self.buffer = cp.zeros((maxlen,) + shape, dtype=dtype)
            self.is_gpu = True
        else:
            _dtype_map = {
                cp.complex128: np.complex128, np.complex128: np.complex128,
                cp.complex64: np.complex64, np.complex64: np.complex64,
                cp.float64: np.float64, np.float64: np.float64,
                cp.float32: np.float32, np.float32: np.float32,
            }
            np_dtype = _dtype_map.get(dtype, np.complex128)
            self.buffer = np.zeros((maxlen,) + shape, dtype=np_dtype)
            self.is_gpu = False
        self.maxlen = maxlen
        self.write_idx = 0
        self.read_idx = 0
        self.count = 0
        self.lock = threading.Lock()
        self.not_empty = threading.Condition(self.lock)
        self.not_full = threading.Condition(self.lock)

    def put(self, data):
        with self.not_full:
            while self.count == self.maxlen:
                self.not_full.wait()
            self.buffer[self.write_idx] = data
            self.write_idx = (self.write_idx + 1) % self.maxlen
            self.count += 1
            self.not_empty.notify()

    def put_batch(self, data_batch):
        n = data_batch.shape[0]
        with self.not_full:
            for i in range(n):
                while self.count == self.maxlen:
                    self.not_full.wait()
                self.buffer[self.write_idx] = data_batch[i]
                self.write_idx = (self.write_idx + 1) % self.maxlen
                self.count += 1
            self.not_empty.notify_all()

    def get(self):
        with self.not_empty:
            while self.count == 0:
                self.not_empty.wait()
            data = self.buffer[self.read_idx].copy()
            self.read_idx = (self.read_idx + 1) % self.maxlen
            self.count -= 1
            self.not_full.notify()
        return data

    def get_batch(self, n):
        with self.not_empty:
            while self.count < n:
                self.not_empty.wait()
            end = self.read_idx + n
            if end <= self.maxlen:
                batch = self.buffer[self.read_idx:end].copy()
            else:
                lib = cp if self.is_gpu else np
                batch = lib.concatenate([
                    self.buffer[self.read_idx:],
                    self.buffer[:end - self.maxlen]
                ])
            self.read_idx = end % self.maxlen
            self.count -= n
            self.not_full.notify_all()
        return batch

    def get_batch_view(self, n):
        """Return a view (no copy) of n entries. count is NOT decremented,
        so the Producer cannot overwrite this region until release_batch()."""
        with self.not_empty:
            while self.count < n:
                self.not_empty.wait()
            end = self.read_idx + n
            if end <= self.maxlen:
                batch = self.buffer[self.read_idx:end]
            else:
                lib = cp if self.is_gpu else np
                batch = lib.concatenate([
                    self.buffer[self.read_idx:],
                    self.buffer[:end - self.maxlen]
                ])
            self.read_idx = end % self.maxlen
        return batch, n

    def release_batch(self, n):
        """Call after finishing with the view from get_batch_view().
        Decrements count and notifies the Producer."""
        with self.not_full:
            self.count -= n
            self.not_full.notify_all()


# ============================================================================
# IPCRingBuffer — Cross-process GPU ring buffer via CUDA IPC
# ============================================================================

class IPCRingBufferSync:
    """Shared synchronization primitives for cross-process ring buffer.
    Created in the main process; passed to child via mp.Process args."""

    def __init__(self, maxlen, ctx=None):
        c = ctx or _mp_ctx
        self.write_idx = c.Value('i', 0)
        self.read_idx = c.Value('i', 0)
        self.count = c.Value('i', 0)
        self.maxlen = maxlen
        self.lock = c.Lock()
        self.not_empty = c.Condition(self.lock)
        self.not_full = c.Condition(self.lock)


class IPCRingBufferProducer:
    """Producer side — lives inside ChannelProducerProcess.
    Allocates GPU buffer and exposes CUDA IPC handle."""

    def __init__(self, shape, dtype, sync: IPCRingBufferSync):
        import cupy as _cp
        self.buffer = _cp.zeros((sync.maxlen,) + shape, dtype=dtype)
        self.ipc_handle = _cp.cuda.runtime.ipcGetMemHandle(self.buffer.data.ptr)
        self.sync = sync
        self.nbytes = self.buffer.nbytes

    def put_batch(self, data_batch):
        """v7 #22: GPU sync moved outside the lock to avoid blocking peers."""
        import cupy as _cp
        n = data_batch.shape[0]
        s = self.sync
        # 1. Stage all writes (still need lock for indices), but no GPU sync inside
        with s.not_full:
            for i in range(n):
                while s.count.value == s.maxlen:
                    s.not_full.wait()
                self.buffer[s.write_idx.value] = data_batch[i]
                s.write_idx.value = (s.write_idx.value + 1) % s.maxlen
                s.count.value += 1
            # Notify under lock (cheap), but do NOT sync yet
            s.not_empty.notify_all()
        # 2. Sync after releasing lock — peers can now acquire it
        _cp.cuda.Device(0).synchronize()
        # 3. Re-notify in case consumer raced past notify before sync completed
        with s.not_empty:
            s.not_empty.notify_all()

    def try_put_batch(self, data_batch):
        """Non-blocking put. full 시 남은 데이터는 drop.
        Returns: 성공적으로 삽입된 아이템 수.
        v7 #22: sync moved outside the lock."""
        import cupy as _cp
        n = data_batch.shape[0]
        s = self.sync
        inserted = 0
        with s.not_full:
            for i in range(n):
                if s.count.value == s.maxlen:
                    break
                self.buffer[s.write_idx.value] = data_batch[i]
                s.write_idx.value = (s.write_idx.value + 1) % s.maxlen
                s.count.value += 1
                inserted += 1
            if inserted > 0:
                s.not_empty.notify_all()
        if inserted > 0:
            _cp.cuda.Device(0).synchronize()
            with s.not_empty:
                s.not_empty.notify_all()
        return inserted


class IPCRingBufferConsumer:
    """Consumer side — lives in the main (Proxy) process.
    Opens CUDA IPC handle to access Producer's GPU buffer."""

    def __init__(self, ipc_handle, shape, dtype, sync: IPCRingBufferSync):
        ptr = cp.cuda.runtime.ipcOpenMemHandle(ipc_handle)
        full_shape = (sync.maxlen,) + shape
        nbytes = int(np.prod(full_shape)) * cp.dtype(dtype).itemsize
        mem = cp.cuda.UnownedMemory(ptr, nbytes, owner=None)
        self.buffer = cp.ndarray(
            full_shape, dtype=dtype,
            memptr=cp.cuda.MemoryPointer(mem, 0))
        self.sync = sync
        self._ipc_ptr = ptr

    def get_batch_view(self, n, timeout=30.0):
        """Return a view of n entries. Count is NOT decremented until release_batch().
        Returns (batch, n_held) or raises TimeoutError."""
        s = self.sync
        with s.not_empty:
            deadline = time.monotonic() + timeout
            while s.count.value < n:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"IPCRingBufferConsumer.get_batch_view timed out "
                        f"(need={n}, have={s.count.value})")
                s.not_empty.wait(timeout=remaining)
            end = s.read_idx.value + n
            if end <= s.maxlen:
                batch = self.buffer[s.read_idx.value:end]
            else:
                batch = cp.concatenate([
                    self.buffer[s.read_idx.value:],
                    self.buffer[:end - s.maxlen]])
            s.read_idx.value = end % s.maxlen
        return batch, n

    def release_batch(self, n):
        s = self.sync
        with s.not_full:
            s.count.value -= n
            s.not_full.notify_all()

    def cleanup(self):
        try:
            cp.cuda.runtime.ipcCloseMemHandle(self._ipc_ptr)
        except Exception:
            pass


# ============================================================================
# UnifiedChannelProducerProcess — single TF context for all UEs (v4)
# ============================================================================

class UnifiedChannelProducerProcess(_mp_ctx.Process):
    """단일 프로세스에서 N_UE개 UE의 채널을 통합 생성.
    N_UE 배치 차원을 활용하여 Sionna 한 번 호출로 전체 UE 채널을 생성하고,
    UE별로 슬라이싱하여 각 링버퍼에 non-blocking으로 분배한다."""

    def __init__(self, config, syncs, handle_queue, stop_event):
        super().__init__(daemon=True)
        self.config = config
        self.syncs = syncs
        self.handle_queue = handle_queue
        self.stop_event = stop_event

    def run(self):
        import os as _os
        _os.environ['TF_GPU_ALLOCATOR'] = 'cuda_malloc_async'
        _os.environ["CUDA_VISIBLE_DEVICES"] = str(self.config.get('gpu_num', 0))
        _os.environ["TF_CPP_MIN_LOG_LEVEL"] = "0"

        import tensorflow as _tf
        for _gpu in _tf.config.list_physical_devices('GPU'):
            _tf.config.experimental.set_memory_growth(_gpu, True)

        import cupy as _cp
        _cp.cuda.Device(0).use()

        import numpy as _np

        _child_seed = self.config.get('seed')
        if _child_seed is not None:
            _tf.random.set_seed(_child_seed)
            _np.random.seed(_child_seed)
            _cp.random.seed(_child_seed)
            import random as _rnd
            _rnd.seed(_child_seed)
            print(f"[v8 ChannelProducer] RNG seed={_child_seed} "
                  f"(tf+np+cp+random in child pid={_os.getpid()})")

        from sionna.phy import PI, SPEED_OF_LIGHT, config as _sionna_cfg
        from sionna.phy.channel.tr38901 import PanelArray, Topology, Rays
        from channel_coefficients_JIN import (
            ChannelCoefficientsGeneratorJIN,
            random_binary_mask_tf_complex64,
        )

        if _child_seed is not None:
            _sionna_cfg.seed = _child_seed
            print(f"[v8 ChannelProducer] sionna config.seed={_child_seed}")

        cfg = self.config
        num_ues = cfg['num_ues']

        if 'ray_data_stacked' in cfg:
            rd = cfg['ray_data_stacked']
            phi_r_rays = _tf.convert_to_tensor(rd['phi_r'])
            phi_t_rays = _tf.convert_to_tensor(rd['phi_t'])
            theta_r_rays = _tf.convert_to_tensor(rd['theta_r'])
            theta_t_rays = _tf.convert_to_tensor(rd['theta_t'])
            power_rays = _tf.convert_to_tensor(rd['power'])
            tau_rays = _tf.convert_to_tensor(rd['tau'])
            print(f"[v8 UnifiedChannelProducerProcess] Stacked P1B ray_data loaded (N_UE={num_ues})")
        elif 'ray_data' in cfg:
            rd = cfg['ray_data']
            phi_r_rays = _tf.convert_to_tensor(rd['phi_r'])
            phi_t_rays = _tf.convert_to_tensor(rd['phi_t'])
            theta_r_rays = _tf.convert_to_tensor(rd['theta_r'])
            theta_t_rays = _tf.convert_to_tensor(rd['theta_t'])
            power_rays = _tf.convert_to_tensor(rd['power'])
            tau_rays = _tf.convert_to_tensor(rd['tau'])
            print(f"[v8 UnifiedChannelProducerProcess] P1B ray_data loaded (N_UE={num_ues})")
        else:
            npy_dir = cfg['npy_directory']
            phi_r_rays = _tf.convert_to_tensor(_np.load(npy_dir + "/phi_r_rays_for_ChannelBlock.npy"))
            phi_t_rays = _tf.convert_to_tensor(_np.load(npy_dir + "/phi_t_rays_for_ChannelBlock.npy"))
            theta_r_rays = _tf.convert_to_tensor(_np.load(npy_dir + "/theta_r_rays_for_ChannelBlock.npy"))
            theta_t_rays = _tf.convert_to_tensor(_np.load(npy_dir + "/theta_t_rays_for_ChannelBlock.npy"))
            power_rays = _tf.convert_to_tensor(_np.load(npy_dir + "/power_rays_for_ChannelBlock.npy"))
            tau_rays = _tf.convert_to_tensor(_np.load(npy_dir + "/tau_rays_for_ChannelBlock.npy"))

        batch_size = 1
        N_UE = cfg['N_UE']
        N_BS = cfg['N_BS']
        N_FFT_local = cfg['N_FFT']
        scs_local = cfg['scs']
        Fs_local = cfg['Fs']
        carrier_freq = cfg['carrier_frequency']
        buffer_symbol_size = cfg['buffer_symbol_size']
        gnb_nx, gnb_ny = cfg['gnb_nx'], cfg['gnb_ny']
        ue_nx, ue_ny = cfg['ue_nx'], cfg['ue_ny']
        Speed_local = cfg['Speed']
        mean_xpr = cfg['mean_xpr']
        stddev_xpr = cfg['stddev_xpr']

        BSexample = {
            "num_rows_per_panel": gnb_ny, "num_cols_per_panel": gnb_nx,
            "num_rows": 1, "num_cols": 1,
            "polarization": "single", "polarization_type": "V",
        }
        ArrayTX = PanelArray(
            num_rows_per_panel=BSexample["num_rows_per_panel"],
            num_cols_per_panel=BSexample["num_cols_per_panel"],
            num_rows=BSexample["num_rows"], num_cols=BSexample["num_cols"],
            polarization=BSexample["polarization"],
            polarization_type=BSexample["polarization_type"],
            antenna_pattern='omni',
            carrier_frequency=carrier_freq)
        ArrayRX = PanelArray(
            num_rows_per_panel=ue_ny, num_cols_per_panel=ue_nx,
            num_rows=1, num_cols=1,
            polarization='single', polarization_type='V', antenna_pattern='omni',
            carrier_frequency=carrier_freq)

        print(f"[v8 UnifiedChannelProducerProcess] Sionna init start (N_UE={N_UE})")

        xpr_pdp = 10**(_tf.random.normal(
            shape=[batch_size, N_BS, N_UE, 1, phi_r_rays.shape[-1]],
            mean=mean_xpr, stddev=stddev_xpr
        )/10)
        PDP = Rays(
            delays=tau_rays, powers=power_rays, aoa=phi_r_rays, aod=phi_t_rays,
            zoa=theta_r_rays, zod=theta_t_rays, xpr=xpr_pdp)

        # ─────────────────────────────────────────────────────────────────
        # v7 #V6-8: velocities — scalar speed magnitude with random horizontal
        # direction.  v6 generated [Vx, Vy, Vz] each with mean=Speed_local,
        # giving |v| = sqrt(3)·Speed_local.  Here we generate a unit direction
        # in the horizontal plane and scale by a (nearly-)scalar speed.
        # ─────────────────────────────────────────────────────────────────
        if Speed_local == 0 or Speed_local == 0.0:
            velocities = _tf.zeros([batch_size, N_UE, 3], dtype=_tf.float32)
            print(f"[v8 ChannelProducer] velocities: EXACTLY zero (static channel)")
        else:
            _phi = _tf.random.uniform(
                shape=[batch_size, N_UE], minval=0.0, maxval=2 * float(PI),
                dtype=_tf.float32)
            _speed_mag = _tf.abs(_tf.random.normal(
                shape=[batch_size, N_UE], mean=Speed_local, stddev=0.1,
                dtype=_tf.float32))
            velocities = _tf.stack([
                _speed_mag * _tf.cos(_phi),                       # Vx
                _speed_mag * _tf.sin(_phi),                       # Vy
                _tf.zeros_like(_speed_mag),                       # Vz=0 (planar)
            ], axis=-1)
            print(f"[v8 ChannelProducer] velocities: scalar |v|={Speed_local} m/s, "
                  f"horizontal random direction (Vz=0)")

        # ─────────────────────────────────────────────────────────────────
        # v7 #V6-9: LOS path angles — random within physically reasonable
        # ranges instead of all zeros.  Range matches 3GPP TR38.901 conventions
        # (azimuth: full 2π, zenith: π/2 ± 30° around horizon).
        # ─────────────────────────────────────────────────────────────────
        los_aoa = _tf.random.uniform(
            shape=[batch_size, N_BS, N_UE], minval=0.0, maxval=2 * float(PI),
            dtype=_tf.float32)
        los_aod = _tf.random.uniform(
            shape=[batch_size, N_BS, N_UE], minval=0.0, maxval=2 * float(PI),
            dtype=_tf.float32)
        los_zoa = _tf.random.uniform(
            shape=[batch_size, N_BS, N_UE],
            minval=float(PI) / 3, maxval=2 * float(PI) / 3, dtype=_tf.float32)
        los_zod = _tf.random.uniform(
            shape=[batch_size, N_BS, N_UE],
            minval=float(PI) / 3, maxval=2 * float(PI) / 3, dtype=_tf.float32)
        los = _tf.random.uniform(
            shape=[batch_size, N_BS, N_UE], minval=0, maxval=2, dtype=_tf.int32) > 0

        # ─────────────────────────────────────────────────────────────────
        # v7 #V6-7: distance_3d — keep P1B tau-derived distance optional.
        # The fixed default mode is more stable for attach when P1B contains
        # very small first-tap delays that collapse topology distance to 1m.
        # tau_rays shape: (batch, N_BS, N_UE, 1, n_paths)
        # ─────────────────────────────────────────────────────────────────
        _default_d = float(cfg.get('default_distance_m', 100.0))
        _p1b_distance_mode = str(cfg.get('p1b_distance_mode', 'default')).lower()
        _has_p1b = 'ray_data_stacked' in cfg or 'ray_data' in cfg
        if _has_p1b and _p1b_distance_mode == 'tau':
            # Use earliest tap delay across paths to estimate distance
            _tau_first = _tf.reduce_min(tau_rays, axis=-1)              # (b, N_BS, N_UE, 1)
            _tau_first = _tf.squeeze(_tau_first, axis=-1)               # (b, N_BS, N_UE)
            distance_3d = _tau_first * _tf.constant(SPEED_OF_LIGHT, _tau_first.dtype)
            distance_3d = _tf.maximum(distance_3d, 1.0)                 # floor at 1m
            print(f"[v8 ChannelProducer] distance_3d derived from P1B tau, "
                  f"range=[{float(_tf.reduce_min(distance_3d))}, "
                  f"{float(_tf.reduce_max(distance_3d))}] m")
        else:
            distance_3d = _tf.ones([1, N_BS, N_UE]) * _default_d
            if _has_p1b:
                print(f"[v8 ChannelProducer] distance_3d=fixed {_default_d} m "
                      f"(P1B distance mode={_p1b_distance_mode})")
            else:
                print(f"[v8 ChannelProducer] distance_3d=fallback {_default_d} m "
                      f"(no P1B; configure via --default-distance-m)")

        tx_orientations = _tf.random.normal(
            shape=[batch_size, N_BS, 3], mean=0, stddev=PI/5, dtype=_tf.float32)
        rx_orientations = _tf.random.normal(
            shape=[batch_size, N_UE, 3], mean=0, stddev=PI/5, dtype=_tf.float32)

        topology = Topology(
            velocities, "rx", los_aoa, los_aod, los_zoa, los_zod,
            los, distance_3d, tx_orientations, rx_orientations)

        gen = ChannelCoefficientsGeneratorJIN(
            carrier_freq, scs_local, ArrayTX, ArrayRX, False)
        h_field, aoa, zoa = gen._H_PDP_FIX(topology, PDP, N_FFT_local, scs_local)
        h_field = _tf.transpose(h_field, [0, 3, 5, 6, 1, 2, 7, 4])
        aoa = _tf.transpose(aoa, [0, 3, 1, 2, 4])
        zoa = _tf.transpose(zoa, [0, 3, 1, 2, 4])

        print(f"[v8 UnifiedChannelProducerProcess] Sionna init done, allocating {num_ues} GPU ring buffers")

        shape = cfg['shape']
        ring_buffers = []
        for k in range(num_ues):
            rb_k = IPCRingBufferProducer(shape, _cp.complex128, self.syncs[k])
            self.handle_queue.put(rb_k.ipc_handle)
            ring_buffers.append(rb_k)
            print(f"[v8 UnifiedChannelProducerProcess] UE[{k}] IPC handle sent")

        use_xla = cfg.get('use_xla', False)
        xla_tag = " +XLA" if use_xla else ""
        print(f"[v8 UnifiedChannelProducerProcess] Entering generation loop (pid={_os.getpid()}, N_UE={num_ues}{xla_tag})")

        params = dict(Fs=Fs_local, scs=scs_local,
                      N_UE=N_UE, N_BS=N_BS,
                      N_UE_active=cfg['num_rx'], N_BS_serving=cfg['num_tx'])

        # v7 #V6-6: build sample_times from integer base to avoid float32
        # precision loss after long runs.  Using tf.range over int64 first,
        # then dividing by scs, keeps full int precision until the final cast.
        _scs_tf = _tf.constant(params['scs'], gen.rdtype)
        _bss = int(buffer_symbol_size)

        def _build_sample_times(b_idx):
            # absolute symbol indices: [b_idx*B, b_idx*B+1, ..., b_idx*B+B-1]
            start = _tf.constant(b_idx * _bss, dtype=_tf.int64)
            idx_i64 = start + _tf.range(_bss, dtype=_tf.int64)
            return _tf.cast(idx_i64, gen.rdtype) / _scs_tf

        ActiveUE_fixed = _tf.constant(
            random_binary_mask_tf_complex64(params['N_UE'], k=params['N_UE_active']),
            dtype=_tf.complex64)
        ServingBS_fixed = _tf.constant(
            random_binary_mask_tf_complex64(params['N_BS'], k=params['N_BS_serving']),
            dtype=_tf.complex64)

        # ─────────────────────────────────────────────────────────────────
        # v7 #V6-5: energy normalization uses a FIXED reference (computed on
        # batch 0) so that Doppler-induced power fluctuations are preserved
        # across batches.  v6 normalized per-batch, which strips out Rayleigh
        # fading.  Using a fixed reference also removes the inter-batch
        # discontinuity (every batch divides by the SAME constant).
        # ─────────────────────────────────────────────────────────────────
        def _generate_unnorm(sample_times):
            h_delay, _, _, _ = gen._H_TTI_sequential_fft_o_ELW2_noProfile(
                topology, ActiveUE_fixed, ServingBS_fixed, sample_times,
                h_field, aoa, zoa)
            h_delay = h_delay[0, :, :, 0, :, :, :]
            h_delay = _tf.transpose(h_delay, [3, 2, 0, 1, 4])
            return _tf.cast(h_delay, _tf.complex128)

        # Compute reference normalization scalar from batch 0
        _h0 = _generate_unnorm(_build_sample_times(0))
        _ref_energy = _tf.reduce_mean(_tf.abs(_h0) ** 2, keepdims=False)
        _ref_norm = _tf.cast(_tf.sqrt(_ref_energy + 1e-30), _h0.dtype)
        print(f"[v8 ChannelProducer] reference energy normalization: "
              f"|H|_rms = {float(_tf.abs(_ref_norm)):.4e}")

        def _generate_eager(sample_times):
            h_c128 = _generate_unnorm(sample_times)
            return h_c128 / _ref_norm   # global, time-invariant scaling

        if use_xla:
            _st_spec = _tf.TensorSpec([_bss], gen.rdtype)
            generate_fn = _tf.function(
                _generate_eager, jit_compile=True,
                input_signature=[_st_spec])
            print(f"[v8 XLA] Compiling XLA graph (first call will be slow)...")
            _t_xla = time.time()
            _ = generate_fn(_build_sample_times(0))
            print(f"[v8 XLA] Compilation done ({time.time()-_t_xla:.1f}s)")
        else:
            generate_fn = _generate_eager

        attach_stable_sec = float(cfg.get('attach_stable_sec', 0.0) or 0.0)
        attach_stable_mode = str(cfg.get('attach_stable_mode', 'auto')).lower()
        attach_stable_trigger_file = str(cfg.get(
            'attach_stable_trigger_file',
            '/tmp/oai_gpu_ipc/v8_dynamic_enable')).strip()
        attach_stable_batches = 0
        dynamic_start_batch = 0
        attach_stable_active = False
        attach_stable_handoff_logged = False
        if attach_stable_sec > 0.0:
            attach_stable_batches = int(np.ceil(attach_stable_sec * params['scs'] / _bss))
            attach_stable_batches = max(1, attach_stable_batches)
            attach_stable_active = True
            dynamic_start_batch = attach_stable_batches
            trigger_desc = ""
            if attach_stable_mode == "auto" and attach_stable_trigger_file:
                trigger_desc = f", trigger_file={attach_stable_trigger_file}"
            print(f"[v8 AttachStable] enabled: mode={attach_stable_mode}, "
                  f"max={attach_stable_sec:.1f}s "
                  f"≈ {attach_stable_batches} batches; sample_times frozen "
                  f"until dynamic handoff{trigger_desc}")

        symbol_counter = 0
        batch_idx = 0
        drop_count = [0] * num_ues
        # v7 #5: bounded retry on non-OOM exceptions to prevent infinite spam.
        consecutive_errors = 0
        MAX_CONSECUTIVE_ERRORS = 20

        while not self.stop_event.is_set():
            try:
                if attach_stable_active:
                    if (attach_stable_mode == "auto" and attach_stable_trigger_file and
                            (batch_idx % 10 == 0) and
                            _os.path.exists(attach_stable_trigger_file)):
                        attach_stable_active = False
                        dynamic_start_batch = batch_idx
                        attach_stable_handoff_logged = True
                        print("[v8 AttachStable] dynamic channel handoff "
                              f"triggered by file: {attach_stable_trigger_file}")
                    elif batch_idx >= attach_stable_batches:
                        attach_stable_active = False
                        dynamic_start_batch = batch_idx
                        attach_stable_handoff_logged = True
                        print("[v8 AttachStable] dynamic channel handoff "
                              f"after max stable window ({attach_stable_sec:.1f}s)")

                if attach_stable_active:
                    # Freeze inter-batch time during attach so RRC/NAS/SRS setup
                    # sees a quasi-static channel, then restart dynamic time at 0.
                    sample_times_now = _build_sample_times(0)
                else:
                    if (attach_stable_batches > 0 and
                            batch_idx == attach_stable_batches and
                            not attach_stable_handoff_logged):
                        print("[v8 AttachStable] dynamic channel handoff")
                    dyn_batch_idx = batch_idx - dynamic_start_batch
                    sample_times_now = _build_sample_times(dyn_batch_idx)
                h_c128_norm = generate_fn(sample_times_now)

                # v7 #V6-4: increment batch_idx IMMEDIATELY after successful
                # generate_fn() so a downstream exception does not trigger a
                # duplicate batch on retry.
                batch_idx += 1

                try:
                    h_cp = _cp.from_dlpack(
                        _tf.experimental.dlpack.to_dlpack(h_c128_norm)).copy()
                except Exception:
                    h_cp = _cp.asarray(h_c128_norm.numpy())

                h_cp = _cp.fft.fft(h_cp, axis=-1)

                for ue_k in range(num_ues):
                    h_ue_k = h_cp[:, ue_k, :, :, :]
                    rb = ring_buffers[ue_k]
                    s = rb.sync
                    bp_limit = int(s.maxlen * 0.75)
                    with s.not_full:
                        while s.count.value >= bp_limit and not self.stop_event.is_set():
                            s.not_full.wait(timeout=0.1)
                    if self.stop_event.is_set():
                        break
                    inserted = rb.try_put_batch(h_ue_k)
                    dropped = h_ue_k.shape[0] - inserted
                    if dropped > 0:
                        drop_count[ue_k] += dropped
                        if drop_count[ue_k] <= 10 or drop_count[ue_k] % 500 == 0:
                            avail = s.count.value
                            cap = s.maxlen
                            print(f"[CH DIAG] UE[{ue_k}] ring buffer full: "
                                  f"dropped={dropped}, total_dropped={drop_count[ue_k]}, "
                                  f"buf={avail}/{cap}")

                symbol_counter += _bss
                consecutive_errors = 0   # successful loop resets counter

            except _tf.errors.ResourceExhaustedError as e:
                print(f"[v8 UnifiedChannelProducerProcess] GPU OOM — stopping: {e}")
                break
            except Exception as e:
                consecutive_errors += 1
                print(f"[v8 UnifiedChannelProducerProcess] ERROR "
                      f"({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}")
                import traceback
                traceback.print_exc()
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    print(f"[v8 UnifiedChannelProducerProcess] Too many "
                          f"consecutive errors — exiting.")
                    break
                time.sleep(1.0)

        try:
            _cp.get_default_memory_pool().free_all_blocks()
            _cp.get_default_pinned_memory_pool().free_all_blocks()
        except Exception:
            pass
        drop_str = ", ".join(f"UE{k}={drop_count[k]}" for k in range(num_ues))
        print(f"[v8 UnifiedChannelProducerProcess] Stopped (symbols={symbol_counter}, batches={batch_idx}, dropped: {drop_str})")


class NoiseProducer(threading.Thread):
    """Pre-generate AWGN noise vectors in a background thread.
    Each entry: (2, noise_len) float32 — [0]=real, [1]=imag, standard normal.

    v7 #16: dtype downgraded to float32 (noise statistics need ~5 bits of
    precision, not 52); BATCH_SIZE kept moderate.  The consumer
    (GPUSlotPipeline._regenerate_noise) casts to float64 on copy, so
    physics is unchanged but VRAM footprint shrinks ~16× vs v6.
    """
    BATCH_SIZE = 32           # was 64; consumer rate is ~1/slot anyway
    NOISE_DTYPE = cp.float32 if GPU_AVAILABLE else np.float32

    def __init__(self, buffer, noise_len):
        super().__init__()
        self.buffer = buffer
        self.noise_len = noise_len
        self.stop_event = threading.Event()
        self.daemon = True

    def run(self):
        if GPU_AVAILABLE:
            cp.cuda.Device(0).use()
        while not self.stop_event.is_set():
            batch = cp.random.randn(
                self.BATCH_SIZE, 2, self.noise_len).astype(self.NOISE_DTYPE)
            self.buffer.put_batch(batch)


# ============================================================================
# Proxy (dual-mode: socket / gpu-ipc)
# ============================================================================

class Proxy:
    def __init__(self, mode="socket", ue_port=6018, gnb_host="127.0.0.1", gnb_port=6017,
                 log_level="info", ch_en=True, ch_L=32, ch_dd=0, log_plot=False,
                 conv_mode="fft", block_size=4096, num_blocks=None, fft_lib="np",
                 custom_channel=False, buffer_len=1024, buffer_symbol_size=42,
                 enable_gpu=True, use_pinned_memory=True, use_cuda_graph=True,
                 ipc_shm_path=GPU_IPC_SHM_PATH,
                 profile_interval=100, profile_window=500, dual_timer_compare=True,
                 gnb_ant=1, ue_ant=1,
                 gnb_nx=1, gnb_ny=1, ue_nx=1, ue_ny=1,
                 num_ues=1,
                 p1b_npz=None, ue_rx_indices=None,
                 use_xla=False,
                 gt_dir=None, gt_save_every=1, gt_symbol_indices=None,
                 gt_async_copy=True, gt_pinned_pool_size=32, gt_staging_gpu_buffers=32,
                 seed=None,
                 default_distance_m=100.0,
                 attach_stable_sec=300.0,
                 attach_stable_mode="auto",
                 attach_stable_trigger_file="/tmp/oai_gpu_ipc/v8_dynamic_enable",
                 p1b_distance_mode="default",
                 ul_pre_gain=1.0,
                 srs_only_channel=False,
                 srs_symbols=None,
                 srs_flat_mode="power",
                 srs_only_dl_flat=True):
        self.mode = mode
        self.num_ues = num_ues
        self.p1b_npz = p1b_npz
        self.ue_rx_indices = ue_rx_indices
        self.use_xla = use_xla
        self.gt_dir = gt_dir
        self.gt_save_every = gt_save_every
        self.gt_symbol_indices = gt_symbol_indices
        self.gt_async_copy = gt_async_copy
        self.gt_pinned_pool_size = gt_pinned_pool_size
        self.gt_staging_gpu_buffers = gt_staging_gpu_buffers
        self.seed = seed
        self.default_distance_m = default_distance_m   # v7 V6-7
        self.attach_stable_sec = attach_stable_sec
        self.attach_stable_mode = attach_stable_mode
        self.attach_stable_trigger_file = attach_stable_trigger_file
        self.p1b_distance_mode = p1b_distance_mode
        self.ul_pre_gain = float(ul_pre_gain)
        # SRS-only CDL: keep full multipath only on SRS symbol(s), flatten the rest
        self.srs_only_channel = bool(srs_only_channel)
        self._srs_symbols_set = set(int(s) for s in srs_symbols) if srs_symbols else {12}
        self._srs_flat_mode = str(srs_flat_mode) if srs_flat_mode else "power"
        # DL flat (under SRS-only): flatten EVERY DL symbol the whole run so
        # PBCH/PDSCH/RRC-DL decode reliably (DL never needs CDL). Default on.
        self.srs_only_dl_flat = bool(srs_only_dl_flat)
        # latched once the dynamic-channel handoff (= UE attached) fires: before
        # that we flatten ALL symbols so PRACH/RA see a flat channel (reliable
        # attach); after, we keep the SRS symbol(s) on full CDL for measurement.
        self._srs_handoff_done = False
        self._stalled_ue_logged = set()
        self.gnb_ant = gnb_ant
        self.ue_ant = ue_ant
        self.gnb_nx = gnb_nx
        self.gnb_ny = gnb_ny
        self.ue_nx = ue_nx
        self.ue_ny = ue_ny
        self.prev_ts = None
        self.global_symbol_count = 0
        self.slot_sample_accum = 0
        self.ch_en = ch_en
        self.ch_L = ch_L
        self.ch_dd = ch_dd
        self.log_plot = log_plot
        self.conv_mode = conv_mode
        self.block_size = block_size
        self.num_blocks = num_blocks
        self.fft_lib = fft_lib
        self.custom_channel = custom_channel
        self.buffer_len = buffer_len
        self.buffer_symbol_size = buffer_symbol_size
        self.enable_gpu = enable_gpu
        self.use_pinned_memory = use_pinned_memory
        self.use_cuda_graph = use_cuda_graph
        self.ipc_shm_path = ipc_shm_path
        self.profile_interval = max(1, int(profile_interval))
        self.profile_window = max(10, int(profile_window))
        self.dual_timer_compare = bool(dual_timer_compare)

        # GPU IPC interface (only used in gpu-ipc mode)
        self.ipc = None

        # Proxy-level profilers (OFDM wrapper + E2E)
        self.profile_ofdm = WindowProfiler(
            "OFDM_SLOT",
            ["CH_GET", "CH_PAD", "GPU_PROC", "TOTAL"],
            window=self.profile_window,
            report_interval=self.profile_interval)
        self.profile_proxy = WindowProfiler(
            "PROXY_E2E",
            ["PROC", "SEND", "TOTAL"],
            window=self.profile_window,
            report_interval=self.profile_interval)

        # E2E TDD frame statistics
        self._e2e_slot_count = 0
        self._e2e_frame_slots = 10
        self._e2e_next_frame_boundary = 10
        self._e2e_last_wall = None
        self._e2e_proxy_dl_accum_ms = 0.0
        self._e2e_proxy_ul_accum_ms = 0.0
        self._e2e_dl_in_frame = 0
        self._e2e_ul_in_frame = 0
        self._e2e_dl_per_ue = [0] * num_ues
        self._e2e_ul_per_ue = [0] * num_ues

        # Socket mode setup
        if mode == "socket":
            # v7 #6: socket mode does not implement multi-UE correctly
            # (it shares UE0's pipeline/channel and broadcasts identical
            # processed signal to all UEs). Reject early instead of letting
            # users get silently-wrong results.
            if num_ues > 1:
                raise ValueError(
                    f"Socket mode does not support num_ues>1 (got {num_ues}). "
                    f"Use --mode gpu-ipc for multi-UE simulation, or set "
                    f"--num-ues 1 for socket mode.")
            self.sel = selectors.DefaultSelector()
            self.lis = socket.socket()
            self.lis.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.lis.bind(("0.0.0.0", ue_port))
            self.lis.listen()
            self.lis.setblocking(False)
            self.sel.register(self.lis, selectors.EVENT_READ, data="UE_LIS")
            print(f"[INFO] Socket mode: UE listen 0.0.0.0:{ue_port}")
            self.gnb_host, self.gnb_port = gnb_host, gnb_port
            self.gnb_ep: Optional[Endpoint] = None
            self.ues: Dict[int, Endpoint] = {}
            self.gnb_hshake: Optional[Tuple[bytes, bytes]] = None
        else:
            print(f"[INFO] GPU IPC mode: shm_path={ipc_shm_path}")

        self._init_channel(buffer_len, buffer_symbol_size)

    def _init_channel(self, buffer_len, buffer_symbol_size):
        """Initialize per-UE channel producer processes and GPU pipelines (Multi-UE MIMO v2).
        Channel generation runs in independent processes (multiprocessing.Process).
        Pipelines and noise producers remain in the main process."""
        pipeline_common = dict(
            enable_gpu=self.enable_gpu,
            use_pinned_memory=self.use_pinned_memory,
            use_cuda_graph=self.use_cuda_graph,
            profile_interval=self.profile_interval,
            profile_window=self.profile_window,
            dual_timer_compare=self.dual_timer_compare)

        N = self.num_ues
        self.noise_producers = []
        self._noise_buffers_dl_list = []
        self._noise_buffers_ul_list = []
        self.pipelines_dl = []
        self.pipelines_ul = []
        self.channel_buffers = []
        self.channel_producers = []
        self._channel_stop_events = []

        total_cpx = sum(SYMBOL_SIZES)
        noise_len_dl = total_cpx * self.ue_ant
        noise_len_ul = total_cpx * self.gnb_ant

        for k in range(N):
            nb_dl = None
            nb_ul = None
            if noise_enabled and GPU_AVAILABLE:
                # v7 #16: float64→float32 (16x VRAM save), maxlen 256→64
                # (consumer takes 1 batch/slot, producer makes 32/loop, so 64
                # slots of headroom is plenty).
                nb_dl = RingBuffer(shape=(2, noise_len_dl), dtype=cp.float32, maxlen=64)
                np_dl = NoiseProducer(nb_dl, noise_len_dl)
                self.noise_producers.append(np_dl)
                nb_ul = RingBuffer(shape=(2, noise_len_ul), dtype=cp.float32, maxlen=64)
                np_ul = NoiseProducer(nb_ul, noise_len_ul)
                self.noise_producers.append(np_ul)
            self._noise_buffers_dl_list.append(nb_dl)
            self._noise_buffers_ul_list.append(nb_ul)

            pdl = GPUSlotPipeline(
                FFT_SIZE, n_tx_in=self.gnb_ant, n_rx_out=self.ue_ant,
                noise_buffer=nb_dl, **pipeline_common)
            pul = GPUSlotPipeline(
                FFT_SIZE, n_tx_in=self.ue_ant, n_rx_out=self.gnb_ant,
                noise_buffer=nb_ul, **pipeline_common)
            self.pipelines_dl.append(pdl)
            self.pipelines_ul.append(pul)
            print(f"[v7] UE[{k}] pipelines created (DL: {self.gnb_ant}tx→{self.ue_ant}rx, UL: {self.ue_ant}tx→{self.gnb_ant}rx)")

        self.pipeline_dl = self.pipelines_dl[0]
        self.pipeline_ul = self.pipelines_ul[0]
        self.gpu_slot_pipeline = self.pipeline_dl
        self._noise_buffers_dl = self._noise_buffers_dl_list[0]
        self._noise_buffers_ul = self._noise_buffers_ul_list[0]

        self.gt_saver = GTBatchSaver(
            gt_dir=self.gt_dir,
            num_ues=N,
            gnb_ant=self.gnb_ant,
            ue_ant=self.ue_ant,
            fft_size=FFT_SIZE,
            save_every=self.gt_save_every,
            symbol_indices=self.gt_symbol_indices,
            async_copy=self.gt_async_copy,
            pinned_pool_size=self.gt_pinned_pool_size,
            gpu_staging_pool_size=self.gt_staging_gpu_buffers)

        if not self.custom_channel:
            print(f"[v7] Bypass mode — {N} UE(s), no channel")
            return

        self.N_UE = N
        self.N_BS = 1
        self.num_rx = N
        self.num_tx = 1

        mean_xpr_list = {"UMi-LOS": 9, "UMi-NLOS": 8, "UMa-LOS": 8, "UMa-NLOS": 7}
        stddev_xpr_list = {"UMi-LOS": 3, "UMi-NLOS": 3, "UMa-LOS": 4, "UMa-NLOS": 4}

        print(f"[v7] Starting UnifiedChannelProducerProcess via spawn (N_UE={N})...")

        syncs = [IPCRingBufferSync(maxlen=buffer_len, ctx=_mp_ctx) for _ in range(N)]
        handle_q = _mp_ctx.Queue()
        stop_ev = _mp_ctx.Event()

        config = {
            'gnb_nx': self.gnb_nx, 'gnb_ny': self.gnb_ny,
            'ue_nx': self.ue_nx, 'ue_ny': self.ue_ny,
            'carrier_frequency': carrier_frequency,
            'scs': scs, 'N_FFT': N_FFT, 'Fs': Fs,
            'buffer_symbol_size': buffer_symbol_size,
            'buffer_len': buffer_len,
            'npy_directory': directory,
            'shape': (self.ue_ant, self.gnb_ant, FFT_SIZE),
            'N_UE': self.N_UE, 'N_BS': self.N_BS,
            'num_rx': self.num_rx, 'num_tx': self.num_tx,
            'num_ues': N,
            'Speed': Speed,
            'mean_xpr': mean_xpr_list["UMa-NLOS"],
            'stddev_xpr': stddev_xpr_list["UMa-NLOS"],
            'gpu_num': gpu_num,
            'use_xla': self.use_xla,
            'seed': self.seed,
            'default_distance_m': self.default_distance_m,   # v7 V6-7
            'attach_stable_sec': self.attach_stable_sec,
            'attach_stable_mode': self.attach_stable_mode,
            'attach_stable_trigger_file': self.attach_stable_trigger_file,
            'p1b_distance_mode': self.p1b_distance_mode,
        }

        if self.p1b_npz and self.ue_rx_indices:
            config['ray_data_stacked'] = load_p1b_stacked(
                self.p1b_npz, self.ue_rx_indices)
            rx_str = ",".join(str(r) for r in self.ue_rx_indices)
            print(f"[v7] P1B stacked ray loaded: RX=[{rx_str}], N_UE={N}")

        proc = UnifiedChannelProducerProcess(config, syncs, handle_q, stop_ev)
        proc.start()
        print(f"[v7] UnifiedChannelProducerProcess started (pid={proc.pid}, N_UE={N})")

        for k in range(N):
            try:
                ipc_handle = handle_q.get(timeout=120)
            except Exception as e:
                print(f"[v7] ERROR: UE[{k}] IPC handle not received within 120s: {e}")
                if proc.is_alive():
                    proc.terminate()
                raise RuntimeError(f"UnifiedChannelProducerProcess failed to initialize (UE[{k}] handle)") from e

            consumer_k = IPCRingBufferConsumer(
                ipc_handle,
                config['shape'],
                cp.complex128,
                syncs[k])

            self.channel_buffers.append(consumer_k)
            print(f"[v7] UE[{k}] IPCRingBufferConsumer connected (CUDA IPC)")

        self.channel_producers.append(proc)
        self._channel_stop_events.append(stop_ev)

        self.channel_buffer = self.channel_buffers[0]
        self.producer = self.channel_producers[0]

        print(f"[v7] Unified Channel Proxy initialized: {N} UE(s), "
              f"DL: {self.gnb_ant}tx→{self.ue_ant}rx, UL: {self.ue_ant}tx→{self.gnb_ant}rx")

    # ── Socket mode methods (v10 compatible) ──

    def connect_gnb(self):
        try:
            s = socket.create_connection((self.gnb_host, self.gnb_port), timeout=5)
            s.setblocking(False)
            self.gnb_ep = Endpoint(s, "gNB")
            self.sel.register(s, selectors.EVENT_READ, data=self.gnb_ep)
            print(f"[INFO] gNB connected {self.gnb_host}:{self.gnb_port}")
        except OSError as e:
            print(f"[WARN] gNB connect fail: {e}")

    def _reconnect_gnb_if_needed(self):
        if self.gnb_ep and not self.gnb_ep.closed:
            return
        if self.gnb_ep:
            try:
                self.sel.unregister(self.gnb_ep.sock)
            except:
                pass
            self.gnb_ep = None
        self.connect_gnb()

    def _accept_ue(self):
        try:
            c, addr = self.lis.accept()
            c.setblocking(False)
        except OSError:
            return
        ue = Endpoint(c, "UE")
        fd = ue.fileno()
        if fd in self.ues:
            old = self.ues[fd]
            try: self.sel.unregister(old.sock)
            except: pass
            try: old.sock.close()
            except: pass
        self.ues[fd] = ue
        self.sel.register(c, selectors.EVENT_READ, data=ue)
        print(f"[INFO] UE joined {addr}")
        if self.gnb_hshake:
            h, p = self.gnb_hshake
            ue.send(h, p)

    def _handle_ep(self, ep: Endpoint):
        for hdr_raw, hdr_vals, payload in ep.read_blocks():
            t_blk0 = time.perf_counter()
            size, nb, ts, frame, subframe = hdr_vals
            sample_cnt = size * nb

            if size > 1 and self.ch_en and self.custom_channel:
                processed = self._process_ofdm_slot(payload, ts)
                ch_note = " (GPU slot pipeline v12)"
            else:
                processed = payload
                ch_note = ""

            t_proc1 = time.perf_counter()
            send_ms = 0.0

            if ep.role == "gNB":
                log("gNB -> Proxy", size, nb, ts, frame, subframe, sample_cnt,
                    "(handshake)" if size == 1 else ch_note)
                if size == 1:
                    self.gnb_hshake = (hdr_raw, payload)
                for u in list(self.ues.values()):
                    if u.closed:
                        continue
                    t_send0 = time.perf_counter()
                    u.send(hdr_raw, processed)
                    send_ms += 1000 * (time.perf_counter() - t_send0)
            else:
                log("UE -> Proxy", size, nb, ts, frame, subframe, sample_cnt, ch_note)
                if self.gnb_ep and not self.gnb_ep.closed:
                    t_send0 = time.perf_counter()
                    self.gnb_ep.send(hdr_raw, processed)
                    send_ms += 1000 * (time.perf_counter() - t_send0)

            if size > 1 and self.ch_en and self.custom_channel:
                total_ms = 1000 * (time.perf_counter() - t_blk0)
                proc_ms = 1000 * (t_proc1 - t_blk0)
                self.profile_proxy.add(
                    tag=f"dir={ep.role}",
                    PROC=proc_ms, SEND=send_ms, TOTAL=total_ms)

                self._e2e_slot_count += 1
                if ep.role == "gNB":
                    self._e2e_proxy_dl_accum_ms += total_ms
                    self._e2e_dl_in_frame += 1
                    self._e2e_dl_per_ue[0] += 1
                else:
                    self._e2e_proxy_ul_accum_ms += total_ms
                    self._e2e_ul_in_frame += 1
                    self._e2e_ul_per_ue[0] += 1
                self._check_e2e_frame("Socket+OAI")

    def _process_ofdm_slot(self, iq_bytes, ts):
        """Process one OFDM slot through GPU pipeline."""
        t_start = time.perf_counter()

        n_int16 = len(iq_bytes) // 2
        n_cpx = n_int16 // 2
        sym_idx = get_ofdm_symbol_indices(n_cpx)
        n_sym = len(sym_idx)

        t_ch0 = time.perf_counter()
        try:
            channels, n_held = self.channel_buffer.get_batch_view(n_sym)
        except TimeoutError:
            return iq_bytes
        t_ch1 = time.perf_counter()

        t_pad0 = t_ch1
        if n_sym < N_SYM:
            lib = cp if GPU_AVAILABLE else np
            pad = lib.ones((N_SYM - n_sym, FFT_SIZE), dtype=channels.dtype)
            channels = lib.concatenate([channels, pad])
        t_pad1 = time.perf_counter()

        t_gpu0 = t_pad1
        result = self.gpu_slot_pipeline.process_slot(
            iq_bytes, channels, pathLossLinear, snr_dB, noise_enabled, noise_std_abs
        )
        self.channel_buffer.release_batch(n_held)
        t_end = time.perf_counter()

        sc = self.gpu_slot_pipeline.slot_counter
        if sc > 0 and sc % self.profile_interval == 0:
            mode = "GRAPH" if self.gpu_slot_pipeline.graph_captured else "NORMAL"
            self.profile_ofdm.add(
                tag=f"mode={mode}",
                CH_GET=1000 * (t_ch1 - t_ch0),
                CH_PAD=1000 * (t_pad1 - t_pad0),
                GPU_PROC=1000 * (t_end - t_gpu0),
                TOTAL=1000 * (t_end - t_start))

        return result

    # ── GPU IPC mode methods ──

    def _apply_srs_only_mask(self, channels_ul, keep_override=None):
        """SRS-only CDL: keep the full multipath H(f) on the SRS OFDM symbol(s)
        (self._srs_symbols_set, default {12}) and replace every other symbol with
        a frequency-FLAT (zero-delay-spread) channel so PRACH/Msg3/PUSCH stay
        benign and attach reliably, while the SRS symbol still sees the full CDL.

        Flat mode (self._srs_flat_mode):
          'power'    : |H_flat| = sqrt(mean_k|H(f)|^2) (per-link RMS power), phase
                       = angle(mean_k H(f)). Preserves full link power -> PUSCH SNR
                       ~ a flat channel -> reliable attach. DEFAULT (mean was too
                       weak on NLOS: the DC/mean tap is small -> ~67% UL BLER).
          'mean'     : H_flat = mean_k H(f) (DC tap; low gain on NLOS).
          'firsttap' : strongest delay tap only (LOS-like flat).

        channels_ul: (N_SYM, n_rx, n_tx, FFT) UL channel for one slot. Returns a
        masked COPY — it NEVER mutates the shared channel ring-buffer view, and
        the same returned tensor is fed to both process_slot_ipc (IQ) and
        stage_for_ue (GT), so GT[srs_symbol] equals what the SRS sees.

        keep_override: if not None, use this set of symbol indices to keep on
        full CDL (bypassing the handoff-gated default). The DL path passes
        set() to flatten EVERY symbol for the whole run (DL never needs CDL;
        leaving it on multipath breaks PBCH/PDSCH/RRC-DL -> flaky attach).
        """
        lib = cp if GPU_AVAILABLE else np
        out = channels_ul.copy()
        mode = self._srs_flat_mode
        n_sym = out.shape[0]

        if keep_override is not None:
            keep = keep_override
        else:
            # Attach-phase gating: PRACH/RA timing is corrupted by CDL multipath
            # (timing_offset scatters 0~30 samples -> TA loop fails -> RA never
            # completes on high-delay-spread channels like CDL-C). So until the
            # dynamic-channel handoff fires (= UE attached, signalled by the
            # attach_stable_trigger_file), flatten EVERY symbol (keep = {}) so the
            # whole attach (PRACH/Msg3/RRC) sees a flat channel. Only after handoff
            # do we keep the SRS symbol(s) on full CDL for the measurement.
            if not self._srs_handoff_done:
                tf = getattr(self, "attach_stable_trigger_file", "")
                if (not getattr(self, "attach_stable_sec", 0.0)) or (tf and os.path.exists(tf)):
                    self._srs_handoff_done = True
            keep = self._srs_symbols_set if self._srs_handoff_done else set()
        for s in range(n_sym):
            if s in keep:
                continue
            Hs = out[s]                                   # (n_rx, n_tx, FFT)
            if mode == "mean":
                flat = Hs.mean(axis=-1, keepdims=True)
            elif mode == "firsttap":
                ht = lib.fft.ifft(Hs, axis=-1)            # delay domain
                p = lib.abs(ht) ** 2
                # zero all but the per-link strongest tap, back to freq = flat-ish
                kmax = lib.argmax(p, axis=-1)             # (n_rx, n_tx)
                ht_keep = lib.zeros_like(ht)
                for a in range(ht.shape[0]):
                    for b in range(ht.shape[1]):
                        ht_keep[a, b, int(kmax[a, b])] = ht[a, b, int(kmax[a, b])]
                flat = lib.fft.fft(ht_keep, axis=-1).mean(axis=-1, keepdims=True)
            else:  # 'power' (default): RMS magnitude + mean phase
                amp = lib.sqrt(lib.mean(lib.abs(Hs) ** 2, axis=-1, keepdims=True))
                m = Hs.mean(axis=-1, keepdims=True)
                mabs = lib.abs(m)
                # mean direction where well-defined, else real-positive
                phase = lib.where(mabs > 1e-20, m / (mabs + 1e-30), 1.0 + 0.0j)
                flat = amp * phase
            out[s, :, :, :] = flat
        return out

    def _ipc_apply_channel(self, src_ptr, dst_ptr, ts, nsamps,
                           src_nbAnt, src_cir_size, dst_nbAnt, dst_cir_size,
                           direction):
        """Apply Sionna MIMO channel to one full slot at ts.
        direction: 'DL' uses H, 'UL' uses H^T.
        Wrap-safe: handles circular buffer wrap for both input and output."""
        arr_in = self.ipc.read_circ_to_linear(
            src_ptr, ts, nsamps, src_nbAnt, src_cir_size, cp.int16)

        arr_out, wraps_out = self.ipc.get_gpu_array_at(
            dst_ptr, ts, nsamps, dst_nbAnt, dst_cir_size, cp.int16)
        need_circ_write = wraps_out
        if need_circ_write:
            total_out = nsamps * dst_nbAnt
            n_out_i16 = (total_out * GPU_IPC_V6_SAMPLE_SIZE) // 2
            arr_out = cp.empty(n_out_i16, dtype=cp.int16)

        try:
            channels, n_held = self.channel_buffer.get_batch_view(N_SYM)
        except TimeoutError:
            self.ipc.bypass_copy(dst_ptr, src_ptr, ts, nsamps,
                                 src_nbAnt, src_cir_size, dst_nbAnt, dst_cir_size)
            return

        if channels.shape[0] < N_SYM:
            lib = cp if GPU_AVAILABLE else np
            n_r, n_t = channels.shape[1], channels.shape[2]
            n_pad = N_SYM - channels.shape[0]
            pad = lib.zeros((n_pad, n_r, n_t, FFT_SIZE), dtype=channels.dtype)
            for k in range(min(n_r, n_t)):
                pad[:, k, k, :] = 1.0
            channels = lib.concatenate([channels, pad])

        if direction == "UL":
            channels = channels.transpose(0, 2, 1, 3)
            if self.srs_only_channel:
                channels = self._apply_srs_only_mask(channels)
        elif direction == "DL" and self.srs_only_channel and self.srs_only_dl_flat:
            # DL never needs CDL (we only evaluate UL SRS vs GT[12]); leaving DL
            # on multipath breaks PBCH/PDSCH/RRC-DL decode -> flaky attach. Flatten
            # EVERY DL symbol (keep_override=set()) for the whole run.
            channels = self._apply_srs_only_mask(channels, keep_override=set())

        pipeline = self.pipeline_dl if direction == "DL" else self.pipeline_ul
        pipeline.process_slot_ipc(
            arr_in, channels, pathLossLinear, snr_dB, noise_enabled, arr_out, noise_std_abs
        )
        self.channel_buffer.release_batch(n_held)

        if need_circ_write:
            self.ipc.write_linear_to_circ(
                dst_ptr, ts, nsamps, dst_nbAnt, dst_cir_size, arr_out)

    def _ipc_process_range(self, src_ptr, dst_ptr, set_ts_fn,
                           start_ts, delta,
                           src_nbAnt, src_cir_size,
                           dst_nbAnt, dst_cir_size, direction):
        """Process a range [start_ts, start_ts+delta) from src to dst circular buffer.

        V6: supports per-buffer nbAnt/cir_size for asymmetric MIMO bypass.
        Full slot-aligned chunks get channel processing (if enabled).
        Remaining partial data gets channel processing too (wrap-safe).
        Returns (processing_time_ms, num_slots_processed).
        """
        t0 = time.perf_counter()
        slot_samples = self.gpu_slot_pipeline.total_cpx  # 30720
        apply_ch = self.ch_en and self.custom_channel
        pos = int(start_ts)
        remaining = int(delta)
        slots = 0

        while remaining >= slot_samples and apply_ch:
            self._ipc_apply_channel(src_ptr, dst_ptr, pos, slot_samples,
                                    src_nbAnt, src_cir_size,
                                    dst_nbAnt, dst_cir_size, direction)
            pos += slot_samples
            remaining -= slot_samples
            slots += 1

        if remaining > 0:
            self.ipc.bypass_copy(dst_ptr, src_ptr, pos, remaining,
                                 src_nbAnt, src_cir_size,
                                 dst_nbAnt, dst_cir_size)
            pos += remaining

        set_ts_fn(int(start_ts + delta - 1))
        ms = 1000 * (time.perf_counter() - t0)
        return ms, max(slots, 1)

    # ── Multi-UE IPC methods (G1C) ──

    def _ipc_apply_channel_for_ue(self, src_ptr, dst_ptr, ts, nsamps,
                                   src_nbAnt, src_cir_size, dst_nbAnt, dst_cir_size,
                                   src_ipc, dst_ipc, direction, ue_idx):
        """Apply channel for a specific UE. Uses per-UE channel buffer and pipeline.
        Wrap-safe: handles circular buffer wrap for both input and output.
        Falls back to bypass on timeout (ChannelProducerProcess may be slow/dead)."""
        arr_in = src_ipc.read_circ_to_linear(
            src_ptr, ts, nsamps, src_nbAnt, src_cir_size, cp.int16)

        arr_out, wraps_out = dst_ipc.get_gpu_array_at(
            dst_ptr, ts, nsamps, dst_nbAnt, dst_cir_size, cp.int16)
        need_circ_write = wraps_out
        if need_circ_write:
            total_out = nsamps * dst_nbAnt
            n_out_i16 = (total_out * GPU_IPC_V6_SAMPLE_SIZE) // 2
            arr_out = cp.empty(n_out_i16, dtype=cp.int16)

        try:
            channels, n_held = self.channel_buffers[ue_idx].get_batch_view(N_SYM)
        except TimeoutError:
            print(f"[v8 WARN] UE[{ue_idx}] channel buffer timeout — bypass copy")
            src_ipc.bypass_copy(dst_ptr, src_ptr, ts, nsamps,
                                src_nbAnt, src_cir_size, dst_nbAnt, dst_cir_size)
            return

        if channels.shape[0] < N_SYM:
            lib = cp if GPU_AVAILABLE else np
            n_r, n_t = channels.shape[1], channels.shape[2]
            n_pad = N_SYM - channels.shape[0]
            pad = lib.zeros((n_pad, n_r, n_t, FFT_SIZE), dtype=channels.dtype)
            for j in range(min(n_r, n_t)):
                pad[:, j, j, :] = 1.0
            channels = lib.concatenate([channels, pad])

        if direction == "UL":
            channels = channels.transpose(0, 2, 1, 3)
            if self.srs_only_channel:
                channels = self._apply_srs_only_mask(channels)
        elif direction == "DL" and self.srs_only_channel and self.srs_only_dl_flat:
            # DL never needs CDL (we only evaluate UL SRS vs GT[12]); leaving DL
            # on multipath breaks PBCH/PDSCH/RRC-DL decode -> flaky attach. Flatten
            # EVERY DL symbol (keep_override=set()) for the whole run.
            channels = self._apply_srs_only_mask(channels, keep_override=set())

        pipeline = self.pipelines_dl[ue_idx] if direction == "DL" else self.pipelines_ul[ue_idx]
        pipeline.process_slot_ipc(
            arr_in, channels, pathLossLinear, snr_dB, noise_enabled, arr_out, noise_std_abs
        )
        self.channel_buffers[ue_idx].release_batch(n_held)

        if need_circ_write:
            dst_ipc.write_linear_to_circ(
                dst_ptr, ts, nsamps, dst_nbAnt, dst_cir_size, arr_out)

    _dl_remainder_count = 0

    def _ipc_dl_broadcast(self, start_ts, delta):
        """DL Broadcast: gNB dl_tx → per-UE channel → UE[k] dl_rx."""
        t0 = time.perf_counter()
        slot_samples = self.pipelines_dl[0].total_cpx
        apply_ch = self.ch_en and self.custom_channel
        slots = 0

        remainder = int(delta) % slot_samples
        if remainder != 0:
            Proxy._dl_remainder_count += 1
            if Proxy._dl_remainder_count <= 20 or Proxy._dl_remainder_count % 100 == 0:
                print(f"[IPC DIAG] DL delta={delta} not slot-aligned "
                      f"(remainder={remainder}/{slot_samples}, "
                      f"count={Proxy._dl_remainder_count})")

        for k in range(self.num_ues):
            pos = int(start_ts)
            remaining = int(delta)

            while remaining >= slot_samples and apply_ch:
                self._ipc_apply_channel_for_ue(
                    self.ipc_gnb.gpu_dl_tx_ptr, self.ipc_ues[k].gpu_dl_rx_ptr,
                    pos, slot_samples,
                    self.ipc_gnb.dl_tx_nbAnt, self.ipc_gnb.dl_tx_cir_size,
                    self.ipc_ues[k].dl_rx_nbAnt, self.ipc_ues[k].dl_rx_cir_size,
                    self.ipc_gnb, self.ipc_ues[k], "DL", k)
                pos += slot_samples
                remaining -= slot_samples
                if k == 0:
                    slots += 1

            if remaining > 0:
                self.ipc_gnb.bypass_copy(
                    self.ipc_ues[k].gpu_dl_rx_ptr, self.ipc_gnb.gpu_dl_tx_ptr,
                    pos, remaining,
                    self.ipc_gnb.dl_tx_nbAnt, self.ipc_gnb.dl_tx_cir_size,
                    self.ipc_ues[k].dl_rx_nbAnt, self.ipc_ues[k].dl_rx_cir_size)

            self.ipc_ues[k].set_last_dl_rx_ts(int(start_ts + delta - 1))

        ms = 1000 * (time.perf_counter() - t0)
        return ms, max(slots, 1)

    _ul_misalign_count = 0
    _ul_remainder_count = 0
    _ul_combine_diag_count = 0

    def _ipc_ul_combine(self, start_ts, delta, active_ues=None):
        """UL Combine: UE[k] ul_tx → per-UE channel → superposition → gNB ul_rx.
        Bypass mode: sequential copy (last UE wins, no superposition)."""
        Proxy._ul_combine_diag_count += 1
        _dc = Proxy._ul_combine_diag_count
        if _dc <= 10 or (_dc <= 200 and _dc % 50 == 0) or _dc % 5000 == 0:
            _cir = self.ipc_gnb.ul_rx_cir_size
            _nb = self.ipc_gnb.ul_rx_nbAnt
            _co = (int(start_ts) * _nb) % _cir
            print(f"[UL DIAG] proxy_combine #{_dc}: start_ts={int(start_ts)} "
                  f"delta={int(delta)} circ_off={_co} cir={_cir} nbAnt={_nb}")

        t0 = time.perf_counter()
        slot_samples = self.pipelines_ul[0].total_cpx
        apply_ch = self.ch_en and self.custom_channel
        slots = 0
        ues = active_ues if active_ues is not None else range(self.num_ues)
        first_ue = True

        remainder = int(delta) % slot_samples
        if remainder != 0:
            Proxy._ul_remainder_count += 1
            if Proxy._ul_remainder_count <= 20 or Proxy._ul_remainder_count % 100 == 0:
                print(f"[IPC DIAG] UL delta={delta} not slot-aligned "
                      f"(remainder={remainder}/{slot_samples}, "
                      f"count={Proxy._ul_remainder_count})")
        if int(start_ts) % slot_samples != 0:
            Proxy._ul_misalign_count += 1
            if Proxy._ul_misalign_count <= 20 or Proxy._ul_misalign_count % 100 == 0:
                print(f"[IPC DIAG] UL start_ts={start_ts} not slot-aligned "
                      f"(offset={int(start_ts) % slot_samples}, "
                      f"count={Proxy._ul_misalign_count})")

        if not apply_ch:
            for k in ues:
                pos = int(start_ts)
                remaining = int(delta)
                while remaining > 0:
                    n = min(remaining, slot_samples)
                    self.ipc_ues[k].bypass_copy(
                        self.ipc_gnb.gpu_ul_rx_ptr, self.ipc_ues[k].gpu_ul_tx_ptr,
                        pos, n,
                        self.ipc_ues[k].ul_tx_nbAnt, self.ipc_ues[k].ul_tx_cir_size,
                        self.ipc_gnb.ul_rx_nbAnt, self.ipc_gnb.ul_rx_cir_size)
                    pos += n
                    remaining -= n
                    if first_ue:
                        slots += 1
                first_ue = False
            self.ipc_gnb.set_last_ul_rx_ts(int(start_ts + delta - 1))
        else:
            pos = int(start_ts)
            remaining = int(delta)

            while remaining >= slot_samples:
                self._ipc_ul_superposition_slot(pos, slot_samples, active_ues=active_ues)
                pos += slot_samples
                remaining -= slot_samples
                slots += 1

            if remaining > 0:
                for k_r in (active_ues if active_ues is not None else range(self.num_ues)):
                    self.ipc_ues[k_r].bypass_copy(
                        self.ipc_gnb.gpu_ul_rx_ptr, self.ipc_ues[k_r].gpu_ul_tx_ptr,
                        pos, remaining,
                        self.ipc_ues[k_r].ul_tx_nbAnt, self.ipc_ues[k_r].ul_tx_cir_size,
                        self.ipc_gnb.ul_rx_nbAnt, self.ipc_gnb.ul_rx_cir_size)

            self.ipc_gnb.set_last_ul_rx_ts(int(start_ts + delta - 1))

        ms = 1000 * (time.perf_counter() - t0)
        return ms, max(slots, 1)

    def _ipc_ul_superposition_slot(self, ts, nsamps, active_ues=None):
        """Apply per-UE UL channels and sum into gNB ul_rx for one slot.

        v8 GT capture ordering (P0 fix vs v7):
          1. try_begin_slot once per slot — slot-level decision is consistent
             across all UEs (avoids per-UE divergence).
          2. stage_for_ue is called BEFORE release_batch — guarantees the
             per-UE channel data is captured into a private staging buffer
             before ChannelProducer (separate process) can overwrite it.
          3. commit_slot is called AFTER all UEs have been processed and
             released — submits async D2H to the copy_stream + drain thread.
        """
        self._ul_accum[:] = 0
        ues = active_ues if active_ues is not None else range(self.num_ues)
        n_bypassed = 0

        # v8: slot-level GT decision taken ONCE (not per-UE). Even when the
        # decision is False this still increments the internal counter so the
        # save_every cadence is preserved.
        gt_decision = False
        gt_slot_id = 0
        gt_tokens = []
        if hasattr(self, 'gt_saver') and self.gt_saver.enabled:
            gt_decision, gt_slot_id = self.gt_saver.try_begin_slot(ipc_ts=ts)

        for k in ues:
            arr_in = self.ipc_ues[k].read_circ_to_linear(
                self.ipc_ues[k].gpu_ul_tx_ptr, ts, nsamps,
                self.ipc_ues[k].ul_tx_nbAnt, self.ipc_ues[k].ul_tx_cir_size,
                cp.int16)

            try:
                channels, n_held = self.channel_buffers[k].get_batch_view(N_SYM)
            except TimeoutError:
                print(f"[v8 WARN] UE[{k}] UL channel buffer timeout — skip this UE")
                n_bypassed += 1
                continue
            if channels.shape[0] < N_SYM:
                lib = cp if GPU_AVAILABLE else np
                n_r, n_t = channels.shape[1], channels.shape[2]
                n_pad = N_SYM - channels.shape[0]
                pad = lib.zeros((n_pad, n_r, n_t, FFT_SIZE), dtype=channels.dtype)
                for j in range(min(n_r, n_t)):
                    pad[:, j, j, :] = 1.0
                channels = lib.concatenate([channels, pad])

            channels_ul = channels.transpose(0, 2, 1, 3)

            # SRS-only CDL: flatten all but the SRS symbol(s) BEFORE both the IQ
            # application and the GT staging below, so GT[srs_symbol] stays equal
            # to what the SRS sees. _apply_srs_only_mask returns a copy (never
            # mutates the shared ring-buffer view).
            if self.srs_only_channel:
                channels_ul = self._apply_srs_only_mask(channels_ul)

            # v7 #14: skip_quant=True — UL superposition reads gpu_out (complex128)
            # directly; the int16 quantization output would be discarded anyway.
            # We pass _ul_dummy_out for ABI compatibility but it is NOT written.
            self.pipelines_ul[k].process_slot_ipc(
                arr_in, channels_ul, pathLossLinear, snr_dB, noise_enabled,
                self._ul_dummy_out, noise_std_abs, skip_quant=True)

            # v8 P0 fix: stage GT capture BEFORE release_batch so that
            # ChannelProducer (separate _mp.Process) cannot overwrite
            # `channels` while we are still reading it.  stage_for_ue does
            # GPU→GPU staging copy + event.synchronize() (~10–30 μs).
            if gt_decision:
                gt_tokens.append(self.gt_saver.stage_for_ue(k, channels_ul))

            self.channel_buffers[k].release_batch(n_held)

            self._ul_accum += self.pipelines_ul[k].gpu_out

        # v8: commit ALL tokens once after the per-UE loop so partial_bypass
        # reflects the final per-slot state (matches v7 semantics).  For the
        # async path this is where D2H is enqueued on _copy_stream — it is
        # non-blocking and returns to the caller immediately.
        if gt_decision and gt_tokens:
            self.gt_saver.commit_slot(
                gt_tokens, slot_id=gt_slot_id,
                partial_bypass=(n_bypassed > 0))

        n_rx = self.gnb_ant
        total = self.pipelines_ul[0].total_cpx
        n_elem = total * n_rx
        if self.ul_pre_gain != 1.0:
            if not hasattr(self, '_ul_gain_work') or self._ul_gain_work.shape != self._ul_accum.shape:
                self._ul_gain_work = cp.empty_like(self._ul_accum)
            cp.multiply(self._ul_accum, cp.float64(self.ul_pre_gain), out=self._ul_gain_work)
            accum_f64 = self._ul_gain_work.view(cp.float64)
        else:
            accum_f64 = self._ul_accum.view(cp.float64)
        if not hasattr(self, '_ul_fused_out') or self._ul_fused_out.shape[0] != n_elem * 2:
            self._ul_fused_out = cp.zeros(n_elem * 2, dtype=cp.int16)
        threads = 256
        blocks = (n_elem + threads - 1) // threads
        _fused_clip_cast_kernel((blocks,), (threads,),
                                (accum_f64, self._ul_fused_out, n_elem))

        self.ipc_gnb.write_linear_to_circ(
            self.ipc_gnb.gpu_ul_rx_ptr, ts, nsamps,
            self.ipc_gnb.ul_rx_nbAnt, self.ipc_gnb.ul_rx_cir_size,
            self._ul_fused_out)

    def _check_e2e_frame(self, overhead_label="Socket+OAI"):
        """Print E2E TDD frame stats when a frame boundary is crossed."""
        if self._e2e_slot_count < self._e2e_next_frame_boundary:
            return
        now = time.perf_counter()
        if self._e2e_last_wall is not None:
            wall_ms = 1000 * (now - self._e2e_last_wall)
            dl_acc = self._e2e_proxy_dl_accum_ms
            ul_acc = self._e2e_proxy_ul_accum_ms
            proxy_ms = dl_acc + ul_acc
            overhead_ms = wall_ms - proxy_ms
            nd = self._e2e_dl_in_frame
            nu = self._e2e_ul_in_frame
            ns = nd + nu
            if ns <= 0:
                ns = 1
            N = self.num_ues

            ue_parts = []
            for k in range(N):
                dk = self._e2e_dl_per_ue[k]
                uk = self._e2e_ul_per_ue[k]
                ue_parts.append(f"UE{k}({dk}D+{uk}U)")
            ue_str = " ".join(ue_parts)

            print(f"\n[E2E frame#{self._e2e_next_frame_boundary} "
                  f"{N}UE ({nd}D+{nu}U) {ue_str}] "
                  f"wall={wall_ms:.2f}ms  "
                  f"Proxy(DL={dl_acc:.1f}+UL={ul_acc:.1f})"
                  f"={proxy_ms:.2f}ms  "
                  f"{overhead_label}={overhead_ms:.2f}ms  "
                  f"| per slot({nd}+{nu}={ns}): "
                  f"wall={wall_ms/ns:.2f}  "
                  f"proxy={proxy_ms/ns:.2f}  "
                  f"{overhead_label.lower()}={overhead_ms/ns:.2f} ms"
                  f" ts={now:.3f}")
        else:
            print(f"\n[E2E frame#{self._e2e_next_frame_boundary} {self.num_ues}UE] "
                  f"baseline set ts={now:.3f}")
        self._e2e_next_frame_boundary += self._e2e_frame_slots
        self._e2e_last_wall = now
        self._e2e_proxy_dl_accum_ms = 0.0
        self._e2e_proxy_ul_accum_ms = 0.0
        self._e2e_dl_in_frame = 0
        self._e2e_ul_in_frame = 0
        for k in range(self.num_ues):
            self._e2e_dl_per_ue[k] = 0
            self._e2e_ul_per_ue[k] = 0

    def _warmup_pipeline(self):
        """Pre-warm TensorFlow XLA + CUDA Graph with dummy data for all UE pipelines."""
        if not self.pipelines_dl or not self.pipelines_dl[0].enable_gpu:
            return

        for np_thread in self.noise_producers:
            np_thread.start()
            print(f"[v7] NoiseProducer started (noise_len={np_thread.noise_len}, batch={np_thread.BATCH_SIZE})")

        if not self.custom_channel:
            print("[v7] Bypass mode — channel warmup skipped")
            return
        print(f"[v7] Pre-warming {self.num_ues} UE(s) DL/UL pipelines...")
        t0 = time.time()
        passes = GPUSlotPipeline.WARMUP_SLOTS + 1
        if self.channel_producers and hasattr(self.channel_producers[0], 'is_alive'):
            if not self.channel_producers[0].is_alive():
                print(f"[v8 WARN] UnifiedChannelProducerProcess died during warmup")
        for k in range(self.num_ues):
            for label, pipeline in [("DL", self.pipelines_dl[k]), ("UL", self.pipelines_ul[k])]:
                dummy_in = cp.zeros(pipeline.total_int16_in, dtype=cp.int16)
                dummy_out = cp.zeros(pipeline.total_int16_out, dtype=cp.int16)
                n_r, n_t = pipeline.n_rx, pipeline.n_tx
                dummy_ch = cp.zeros((N_SYM, n_r, n_t, FFT_SIZE),
                                    dtype=cp.complex128 if GPU_AVAILABLE else np.complex128)
                for j in range(min(n_r, n_t)):
                    dummy_ch[:, j, j, :] = 1.0
                for i in range(passes):
                    pipeline.process_slot_ipc(
                        dummy_in, dummy_ch, pathLossLinear, snr_dB, noise_enabled, dummy_out, noise_std_abs
                    )
                print(f"  UE[{k}] {label} warmup done ({time.time()-t0:.1f}s)")
        print(f"[v7] All {self.num_ues} UE(s) pipelines ready ({time.time()-t0:.1f}s)")

    def run_ipc(self):
        """Main loop for GPU IPC V6 Multi-UE — DL broadcast + UL combine.

        Architecture:
          ipc_gnb: shared gNB SHM (dl_tx write by gNB, ul_rx read by gNB)
          ipc_ues[k]: per-UE SHM (dl_rx read by UE k, ul_tx write by UE k)

        DL: gNB dl_tx → per-UE channel → UE[k] dl_rx (broadcast)
        UL bypass: UE[k] ul_tx → sequential copy → gNB ul_rx (last wins)
        UL channel: UE[k] ul_tx → per-UE channel → sum → gNB ul_rx (superposition)
        """
        N = self.num_ues

        self.ipc_gnb = GPUIpcV7Interface(
            gnb_ant=self.gnb_ant, ue_ant=self.ue_ant,
            shm_path=self.ipc_shm_path)
        if not self.ipc_gnb.init():
            print("[ERROR] GPU IPC V7 gNB initialization failed")
            return

        self.ipc_ues = []
        for k in range(N):
            shm_path_ue = f"/tmp/oai_gpu_ipc/gpu_ipc_shm_ue{k}"
            ipc_ue = GPUIpcV7Interface(
                gnb_ant=self.gnb_ant, ue_ant=self.ue_ant,
                shm_path=shm_path_ue)
            if not ipc_ue.init():
                print(f"[ERROR] GPU IPC V7 UE[{k}] initialization failed")
                return
            self.ipc_ues.append(ipc_ue)
            print(f"[G1C] UE[{k}] IPC V7 ready: {shm_path_ue}")

        self.ipc = self.ipc_gnb

        total_cpx = sum(SYMBOL_SIZES)
        if GPU_AVAILABLE:
            self._ul_accum = cp.zeros(total_cpx * self.gnb_ant, dtype=cp.complex128)
            # v7 #27: removed dead _ul_clip_3d allocation (replaced by
            # _fused_clip_cast_kernel writing directly to _ul_fused_out).
            self._ul_dummy_out = cp.zeros(
                self.pipelines_ul[0].total_int16_out, dtype=cp.int16)

        self._warmup_pipeline()
        print(f"[v7] Entering main loop ({N} UE(s), gnb_ant={self.gnb_ant}, ue_ant={self.ue_ant})...")
        dl_count = 0
        ul_count = 0
        t_start = time.time()
        proxy_dl_head = 0
        apply_ch = self.ch_en and self.custom_channel

        if apply_ch:
            proxy_ul_head_combined = 0
        else:
            proxy_ul_heads = [0] * N

        # v7 #4: periodic producer-alive check (every N iterations to avoid
        # syscall overhead; if dead, drop out of main loop cleanly).
        _producer_check_interval = 1000
        _iter_count = 0
        _producer_dead = False

        try:
            while True:
                processed = False
                _iter_count += 1

                # v7 #4: periodic producer health check
                if (_iter_count % _producer_check_interval == 0
                        and apply_ch
                        and self.channel_producers):
                    for _proc in self.channel_producers:
                        if hasattr(_proc, 'is_alive') and not _proc.is_alive():
                            print(f"[v8 ERROR] UnifiedChannelProducerProcess "
                                  f"died (exitcode={getattr(_proc, 'exitcode', 'unknown')})"
                                  f" — exiting main loop.")
                            _producer_dead = True
                            break
                    if _producer_dead:
                        break

                # --- DL Broadcast ---
                cur_dl_ts = self.ipc_gnb.get_last_dl_tx_ts()
                dl_nsamps = self.ipc_gnb.get_last_dl_tx_nsamps()
                if cur_dl_ts > 0 and dl_nsamps > 0:
                    gnb_dl_head = cur_dl_ts + dl_nsamps
                    if gnb_dl_head > proxy_dl_head:
                        if proxy_dl_head == 0:
                            proxy_dl_head = max(0, gnb_dl_head - self.ipc_gnb.cir_time)
                        delta = int(gnb_dl_head - proxy_dl_head)
                        dl_ms, n_slots = self._ipc_dl_broadcast(proxy_dl_head, delta)
                        proxy_dl_head = gnb_dl_head
                        dl_count += n_slots
                        processed = True

                        self._e2e_proxy_dl_accum_ms += dl_ms
                        self._e2e_dl_in_frame += n_slots
                        for _k in range(N):
                            self._e2e_dl_per_ue[_k] += n_slots
                        self._e2e_slot_count += n_slots
                        self._check_e2e_frame("IPC_G1C+OAI")

                # --- UL Processing (active_set based stall detection) ---
                if apply_ch:
                    ue_heads = {}
                    for k in range(N):
                        cur_ul = self.ipc_ues[k].get_last_ul_tx_ts()
                        ul_ns = self.ipc_ues[k].get_last_ul_tx_nsamps()
                        if cur_ul > 0 and ul_ns > 0:
                            ue_heads[k] = cur_ul + ul_ns

                    if ue_heads:
                        max_head = max(ue_heads.values())
                        active_set = {k for k, h in ue_heads.items()
                                      if max_head - h < self.ipc_gnb.cir_time}
                        stalled = set(ue_heads.keys()) - active_set

                        # v7 #15: clear log set for UEs that rejoined active.
                        # Without this, a UE can only ever be logged-as-stalled
                        # once for its lifetime even after recovering & re-stalling.
                        _recovered = self._stalled_ue_logged & active_set
                        if _recovered:
                            for rk in _recovered:
                                print(f"[v8 INFO] UE[{rk}] recovered from stall "
                                      f"(head={ue_heads[rk]}, max={max_head})")
                            self._stalled_ue_logged -= _recovered

                        for sk in stalled:
                            if sk not in self._stalled_ue_logged:
                                self._stalled_ue_logged.add(sk)
                                print(f"[v8 WARN] UE[{sk}] stall detected — "
                                      f"head={ue_heads[sk]}, max_head={max_head}, "
                                      f"gap={max_head - ue_heads[sk]}, "
                                      f"cir_time={self.ipc_gnb.cir_time}")

                        min_active_head = min(ue_heads[k] for k in active_set)

                        if min_active_head > proxy_ul_head_combined:
                            if proxy_ul_head_combined == 0:
                                proxy_ul_head_combined = max(0, min_active_head - self.ipc_gnb.cir_time)
                            delta = int(min_active_head - proxy_ul_head_combined)
                            ul_ms, n_slots = self._ipc_ul_combine(
                                proxy_ul_head_combined, delta, active_ues=active_set)
                            proxy_ul_head_combined = min_active_head
                            ul_count += n_slots
                            processed = True

                            self._e2e_proxy_ul_accum_ms += ul_ms
                            self._e2e_ul_in_frame += n_slots
                            for _k in active_set:
                                self._e2e_ul_per_ue[_k] += n_slots
                            self._e2e_slot_count += n_slots
                            self._check_e2e_frame("IPC_G1C+OAI")
                else:
                    # v7 #17: collect all per-UE writes, then call
                    # set_last_ul_rx_ts (futex_wake) ONCE per scan to avoid
                    # waking gNB N times per cycle.
                    _max_new_head = 0
                    _any_bypass = False
                    for k in range(N):
                        cur_ul = self.ipc_ues[k].get_last_ul_tx_ts()
                        ul_ns = self.ipc_ues[k].get_last_ul_tx_nsamps()
                        if cur_ul > 0 and ul_ns > 0:
                            ue_head = cur_ul + ul_ns
                            if ue_head > proxy_ul_heads[k]:
                                if proxy_ul_heads[k] == 0:
                                    proxy_ul_heads[k] = max(0, ue_head - self.ipc_ues[k].cir_time)
                                delta = int(ue_head - proxy_ul_heads[k])
                                self.ipc_ues[k].bypass_copy(
                                    self.ipc_gnb.gpu_ul_rx_ptr,
                                    self.ipc_ues[k].gpu_ul_tx_ptr,
                                    proxy_ul_heads[k], delta,
                                    self.ipc_ues[k].ul_tx_nbAnt,
                                    self.ipc_ues[k].ul_tx_cir_size,
                                    self.ipc_gnb.ul_rx_nbAnt,
                                    self.ipc_gnb.ul_rx_cir_size)
                                proxy_ul_heads[k] = ue_head
                                if ue_head > _max_new_head:
                                    _max_new_head = ue_head
                                _any_bypass = True
                                n_bypass_slots = delta // self.pipelines_ul[0].total_cpx
                                ul_count += n_bypass_slots
                                self._e2e_proxy_ul_accum_ms += 0.0
                                self._e2e_ul_in_frame += n_bypass_slots
                                self._e2e_ul_per_ue[k] += n_bypass_slots
                                self._e2e_slot_count += n_bypass_slots
                                self._check_e2e_frame("IPC_G1C+OAI")
                                processed = True
                    # Single futex_wake at the end
                    if _any_bypass:
                        self.ipc_gnb.set_last_ul_rx_ts(int(_max_new_head - 1))

                if not processed:
                    time.sleep(0.0001)

        except KeyboardInterrupt:
            print(f"\n[v7] Terminated by Ctrl-C (DL: {dl_count}, UL: {ul_count}, UEs: {N})")
        finally:
            if hasattr(self, 'gt_saver'):
                self.gt_saver.flush_all()
            self._cleanup_channel_producers()
            self.ipc_gnb.cleanup()
            for ipc in self.ipc_ues:
                ipc.cleanup()

    def _cleanup_channel_producers(self):
        """Gracefully stop UnifiedChannelProducerProcess and release CUDA IPC resources.
        v7 #48: longer initial join window (10s) so an in-flight generate_fn
        call (especially in XLA mode) has time to finish; falls back to
        SIGTERM, then SIGKILL."""
        for i, evt in enumerate(getattr(self, '_channel_stop_events', [])):
            evt.set()

        for i, proc in enumerate(getattr(self, 'channel_producers', [])):
            if not hasattr(proc, 'join'):
                continue
            # First: graceful exit (stop_event observed at top of generate loop)
            proc.join(timeout=10)
            if proc.is_alive():
                print(f"[v7] UnifiedChannelProducerProcess[{i}] still alive "
                      f"after 10s — sending SIGTERM")
                try:
                    proc.terminate()    # POSIX: sends SIGTERM
                except Exception:
                    pass
                proc.join(timeout=3)
            if proc.is_alive():
                print(f"[v7] UnifiedChannelProducerProcess[{i}] did not exit, killing")
                try:
                    proc.kill()
                except Exception:
                    pass
                proc.join(timeout=2)

        for i, buf in enumerate(getattr(self, 'channel_buffers', [])):
            if hasattr(buf, 'cleanup'):
                try:
                    buf.cleanup()
                except Exception:
                    pass

        print("[v7] UnifiedChannelProducerProcess cleaned up")

    def run_socket(self):
        """Main loop for socket mode (v10 compatible)."""
        for np_thread in self.noise_producers:
            if not np_thread.is_alive():
                np_thread.start()
                print(f"[v7] NoiseProducer started (noise_len={np_thread.noise_len}, batch={np_thread.BATCH_SIZE})")
        self.connect_gnb()
        try:
            while True:
                for key, _ in self.sel.select(0.5):
                    if key.data == "UE_LIS":
                        self._accept_ue()
                    else:
                        self._handle_ep(key.data)
                self._reconnect_gnb_if_needed()
        except KeyboardInterrupt:
            print("[INFO] terminated by Ctrl-C")

    def run(self):
        if self.mode == "gpu-ipc":
            self.run_ipc()
        else:
            self.run_socket()


# ============================================================================
# Main
# ============================================================================

def main():
    ap = argparse.ArgumentParser(
        description="G1C v3 Multi-UE MIMO Channel Proxy (P1B ray + stall detection)")

    ap.add_argument("--mode", choices=["socket", "gpu-ipc"], default="socket",
                    help="Communication mode: socket (v10 compat) or gpu-ipc (CUDA IPC)")
    ap.add_argument("--ipc-shm-path", default=GPU_IPC_SHM_PATH,
                    help=f"GPU IPC shared memory file path (default: {GPU_IPC_SHM_PATH})")

    ap.add_argument("--ue-port", type=int, default=6018)
    ap.add_argument("--gnb-host", default="127.0.0.1")
    ap.add_argument("--gnb-port", type=int, default=6017)
    ap.add_argument("--log", choices=["error", "warn", "info", "debug"], default="info")

    ap.add_argument("--ch-en", dest='ch_en', action="store_true")
    ap.add_argument("--no-ch-en", dest='ch_en', action="store_false")
    ap.set_defaults(ch_en=True)
    ap.add_argument("--ch-dd", type=int, default=0)
    ap.add_argument("--ch-L", type=int, default=32)
    ap.add_argument("--log-plot", action="store_true", default=False)
    ap.add_argument("--conv-mode", type=str, default="fft", choices=["fft", "oa", "os"])
    ap.add_argument("--block-size", type=int, default=4096)
    ap.add_argument("--num-blocks", type=int, default=None)
    ap.add_argument("--fft-lib", type=str, default="np", choices=["np", "tf"])

    ap.add_argument("--custom-channel", dest='custom_channel', action="store_true")
    ap.add_argument("--no-custom-channel", dest='custom_channel', action="store_false")
    ap.set_defaults(custom_channel=True)
    ap.add_argument("--buffer-len", type=int, default=10500,
                    help="Channel IPC ring buffer depth (v2 default 10500 = 42000/4)")
    ap.add_argument("--buffer-symbol-size", type=int, default=4200)

    ap.add_argument("--enable-gpu", dest='enable_gpu', action="store_true")
    ap.add_argument("--disable-gpu", dest='enable_gpu', action="store_false")
    ap.set_defaults(enable_gpu=True)
    ap.add_argument("--use-pinned-memory", dest='use_pinned_memory', action="store_true")
    ap.add_argument("--no-pinned-memory", dest='use_pinned_memory', action="store_false")
    ap.set_defaults(use_pinned_memory=True)
    ap.add_argument("--use-cuda-graph", dest='use_cuda_graph', action="store_true")
    ap.add_argument("--no-cuda-graph", dest='use_cuda_graph', action="store_false")
    ap.set_defaults(use_cuda_graph=True)

    ap.add_argument("--path-loss-dB", type=float, default=0.0)
    ap.add_argument("--snr-dB", type=float, default=None,
                    help="Relative SNR in dB (noise scales with signal power after PL)")
    ap.add_argument("--noise-dBFS", type=float, default=None,
                    help="Absolute noise floor in dBFS (0=full scale 32767). Mutually exclusive with --snr-dB")
    ap.add_argument("--ul-pre-gain", type=float, default=1.0,
                    help="Fixed UL time-domain pre-gain before int16 quantization "
                         "to emulate RF AGC in rfsim/Channel Proxy (default: 1.0)")

    ap.add_argument("--profile-interval", type=int, default=100,
                    help="Profiling report interval in slots (default: 100)")
    ap.add_argument("--profile-window", type=int, default=500,
                    help="Rolling window size for avg/p95/p99/max stats (default: 500)")
    ap.add_argument("--dual-timer-compare", dest='dual_timer_compare', action="store_true",
                    help="Profile with both CPU+sync and CUDA Event timers")
    ap.add_argument("--no-dual-timer-compare", dest='dual_timer_compare', action="store_false",
                    help="Disable CUDA Event comparison (CPU+sync only)")
    ap.set_defaults(dual_timer_compare=True)

    ap.add_argument("--gnb-ant", type=int, default=GNB_ANT,
                    help=f"gNB total antenna count (default: {GNB_ANT} from env)")
    ap.add_argument("--ue-ant", type=int, default=UE_ANT,
                    help=f"UE total antenna count (default: {UE_ANT} from env)")
    ap.add_argument("--gnb-nx", type=int, default=GNB_NX,
                    help=f"gNB antenna cols per panel (default: {GNB_NX} from env)")
    ap.add_argument("--gnb-ny", type=int, default=GNB_NY,
                    help=f"gNB antenna rows per panel (default: {GNB_NY} from env)")
    ap.add_argument("--ue-nx", type=int, default=UE_NX,
                    help=f"UE antenna cols per panel (default: {UE_NX} from env)")
    ap.add_argument("--ue-ny", type=int, default=UE_NY,
                    help=f"UE antenna rows per panel (default: {UE_NY} from env)")
    ap.add_argument("--num-ues", type=int, default=1,
                    help="Number of UEs to support (default: 1)")

    ap.add_argument("--xla", dest='use_xla', action="store_true",
                    help="Enable XLA JIT compilation for channel generation (kernel fusion)")
    ap.add_argument("--no-xla", dest='use_xla', action="store_false")
    ap.set_defaults(use_xla=False)

    ap.add_argument("--npy-dir", type=str, default=None,
                    help="Override default ray .npy directory (e.g. for CDL data). "
                         "Falls through only when --p1b-npz is NOT given.")
    ap.add_argument("--p1b-npz", type=str, default=None,
                    help="P1B npz file path for per-UE independent ray data")
    ap.add_argument("--ue-rx-indices", type=str, default=None,
                    help="Per-UE RX indices (comma-separated, e.g. '100,500') "
                         "or 'random'. Auto-random if --p1b-npz given without this.")

    ap.add_argument("--gt-dir", type=str, default=os.environ.get("SIONNA_GT_DIR", ""),
                    help="Directory to save Sionna GT H(f) batches (default: $SIONNA_GT_DIR)")
    ap.add_argument("--gt-save-every", type=int, default=1,
                    help="Save GT every N-th UL slot (default: 1 = every slot)")
    ap.add_argument("--gt-symbols", type=str, default="12",
                    help="OFDM symbol indices to save, comma-separated (default: 12 = SRS symbol, "
                         "per startPosition=1 in nr_radio_config.c)")
    ap.add_argument("--srs-only-channel", dest="srs_only_channel", action="store_true",
                    help="Apply full CDL multipath ONLY on the SRS OFDM symbol(s) "
                         "(see --srs-symbols); all other symbols get a frequency-flat "
                         "(per-symbol mean) channel so PRACH/Msg3/PUSCH stay benign and "
                         "attach reliably. GT stays consistent (it is staged from the "
                         "same masked tensor). Default off.")
    ap.set_defaults(srs_only_channel=False)
    ap.add_argument("--srs-symbols", type=str, default="12",
                    help="OFDM symbol indices that keep full CDL when --srs-only-channel "
                         "is set, comma-separated (default: 12, aligned with --gt-symbols).")
    ap.add_argument("--srs-flat-mode", type=str, default="power",
                    choices=("power", "mean", "firsttap"),
                    help="Flat-channel model for non-SRS symbols under --srs-only-channel: "
                         "'power'=RMS-power-preserving flat (default, reliable attach), "
                         "'mean'=DC/mean tap (weak on NLOS), 'firsttap'=strongest tap.")
    ap.add_argument("--srs-only-dl-flat", type=int, default=1, choices=(0, 1),
                    help="Under --srs-only-channel: flatten the WHOLE downlink (every DL "
                         "symbol, whole run) so PBCH/PDSCH/RRC-DL decode reliably and attach "
                         "is robust. DL never needs CDL (we only evaluate UL SRS vs GT[12]). "
                         "Default 1 (on); set 0 to keep DL on full CDL multipath.")
    ap.add_argument("--gt-async-copy", choices=("on", "off"), default="on",
                    help="v8: async GPU→CPU copy for GT (default: on). "
                         "Set 'off' to fall back to v7-style sync .get() for A/B comparison.")
    ap.add_argument("--gt-pinned-pool-size", type=int, default=32,
                    help="v8: pre-allocated pinned host buffers for async D2H "
                         "(default: 32). Each ~32 KB, total ~1 MB at default shape.")
    ap.add_argument("--gt-staging-gpu-buffers", type=int, default=32,
                    help="v8: pre-allocated GPU staging buffers (default: 32). "
                         "Each ~32 KB GPU memory.")

    ap.add_argument("--speed", type=float, default=None,
                    help="UE speed in m/s (overrides default 3 m/s). "
                         "Set to 0 for a fully static channel.")

    ap.add_argument("--default-distance-m", type=float, default=100.0,
                    help="Default UE-BS distance (meters). Used without P1B, "
                         "and also with P1B when --p1b-distance-mode=default.")

    ap.add_argument("--attach-stable-sec", type=float, default=300.0,
                    help="Maximum attach-stable duration in seconds. In auto "
                         "mode this is a fallback ceiling; in time mode this is "
                         "the fixed freeze duration. Set 0 to disable.")

    ap.add_argument("--attach-stable-mode", choices=("auto", "time"), default="auto",
                    help="auto: freeze until trigger file appears or max seconds "
                         "expires. time: freeze for --attach-stable-sec only.")

    ap.add_argument("--attach-stable-trigger-file", type=str,
                    default="/tmp/oai_gpu_ipc/v8_dynamic_enable",
                    help="Trigger file watched in auto mode. launch_all_v8.sh "
                         "creates this after gNB reaches the configured attach "
                         "milestone.")

    ap.add_argument("--p1b-distance-mode", choices=("default", "tau"), default="default",
                    help="When P1B ray data is supplied, choose whether topology "
                         "distance_3d uses --default-distance-m or is derived from "
                         "earliest path tau. Default: default.")

    ap.add_argument("--seed", type=int, default=None,
                    help="Global RNG seed for reproducible channel realizations. "
                         "Sets tf/np/cp/python random seeds so that the same channel "
                         "is generated across different SNR points in a sweep.")

    args = ap.parse_args()

    # ── Global RNG seed (reproducible channel realizations) ──
    if args.seed is not None:
        import tensorflow as _tf_seed
        _tf_seed.random.set_seed(args.seed)
        np.random.seed(args.seed)
        _random.seed(args.seed)
        if GPU_AVAILABLE:
            cp.random.seed(args.seed)
        print(f"[v7] Global RNG seed set to {args.seed} "
              f"(tf + np + cp + python random)")

    global Speed
    if args.speed is not None:
        Speed = args.speed
        print(f"[v7] UE speed overridden to {Speed} m/s")

    if args.attach_stable_sec < 0:
        print("[ERROR] --attach-stable-sec must be >= 0")
        sys.exit(1)

    # --npy-dir override for CDL or custom ray data
    global directory
    if args.npy_dir is not None:
        if not os.path.isabs(args.npy_dir):
            args.npy_dir = os.path.normpath(os.path.join(_SCRIPT_DIR, args.npy_dir))
        directory = args.npy_dir
        print(f"[v8] npy_directory overridden: {directory}")

    # v7 V6-1: validate physics AFTER args parsing so user overrides are checked.
    _validate_physics_params(cf=carrier_frequency, sc=scs, sp=Speed)

    # v7 #9: antenna count consistency with panel layout.
    if args.gnb_ant != args.gnb_nx * args.gnb_ny:
        raise ValueError(
            f"--gnb-ant ({args.gnb_ant}) != --gnb-nx*--gnb-ny "
            f"({args.gnb_nx}*{args.gnb_ny}={args.gnb_nx*args.gnb_ny}). "
            f"Antenna total must equal panel rows × cols.")
    if args.ue_ant != args.ue_nx * args.ue_ny:
        raise ValueError(
            f"--ue-ant ({args.ue_ant}) != --ue-nx*--ue-ny "
            f"({args.ue_nx}*{args.ue_ny}={args.ue_nx*args.ue_ny}). "
            f"Antenna total must equal panel rows × cols.")

    # ── P1B 경로 해석 (상대경로 → 스크립트 기준 절대경로) ──
    if args.p1b_npz and not os.path.isabs(args.p1b_npz):
        args.p1b_npz = os.path.normpath(os.path.join(_SCRIPT_DIR, args.p1b_npz))
        print(f"[v7] P1B npz path resolved: {args.p1b_npz}")

    # ── P1B RX 인덱스 해석 ──
    resolved_rx_indices = None
    if args.p1b_npz:
        if args.ue_rx_indices is None or args.ue_rx_indices == "random":
            resolved_rx_indices = pick_random_rx_indices(args.p1b_npz, args.num_ues)
        else:
            resolved_rx_indices = [int(x) for x in args.ue_rx_indices.split(",")]
            if len(resolved_rx_indices) != args.num_ues:
                print(f"[ERROR] --ue-rx-indices 개수({len(resolved_rx_indices)})가 "
                      f"--num-ues({args.num_ues})와 불일치")
                sys.exit(1)
            validate_rx_indices(args.p1b_npz, resolved_rx_indices)

    global path_loss_dB, pathLossLinear, snr_dB, noise_enabled, noise_mode, noise_dBFS, noise_std_abs
    path_loss_dB = args.path_loss_dB
    pathLossLinear = 10**(path_loss_dB / 20.0)
    snr_dB = args.snr_dB
    noise_dBFS = args.noise_dBFS

    if snr_dB is not None and noise_dBFS is not None:
        print("[ERROR] --snr-dB and --noise-dBFS are mutually exclusive")
        sys.exit(1)
    if args.ul_pre_gain <= 0.0:
        print("[ERROR] --ul-pre-gain must be > 0")
        sys.exit(1)

    noise_mode = "none"
    if snr_dB is not None:
        noise_mode = "relative"
    elif noise_dBFS is not None:
        noise_mode = "absolute"
    noise_enabled = (noise_mode != "none")

    if noise_mode == "absolute":
        import math as _math
        _noise_rms = 32767.0 * (10.0 ** (noise_dBFS / 20.0))
        # v7 #34: CPU mode previously got noise_std_abs=None, silently
        # downgrading absolute-noise mode to no-noise.  Now we use a numeric
        # scalar that works in both CPU fallback (numpy) and GPU paths.
        if GPU_AVAILABLE:
            noise_std_abs = cp.float64(_noise_rms / _math.sqrt(2.0))
        else:
            noise_std_abs = float(_noise_rms / _math.sqrt(2.0))
    else:
        noise_std_abs = None

    print("=" * 80)
    xla_str = " +XLA" if args.use_xla else ""
    print(f"G1C v7 Unified Multi-UE MIMO Channel Proxy{xla_str}")
    print("=" * 80)
    print(f"Mode: {args.mode.upper()}")
    print(f"UEs: {args.num_ues}")
    if args.p1b_npz:
        rx_str = ", ".join(f"UE{i}=RX{rx}" for i, rx in enumerate(resolved_rx_indices))
        print(f"P1B Ray Data: {args.p1b_npz}")
        print(f"  RX Indices: {rx_str}")
    else:
        print(f"Ray Data: npy_directory={directory}")
    if args.mode == "gpu-ipc":
        print(f"IPC SHM Path (gNB): {args.ipc_shm_path}")
        for k in range(args.num_ues):
            print(f"IPC SHM Path (UE{k}): /tmp/oai_gpu_ipc/gpu_ipc_shm_ue{k}")
        print(f"  >> Socket ports NOT used (direct GPU shared memory)")
    else:
        print(f"UE Port: {args.ue_port}, gNB: {args.gnb_host}:{args.gnb_port}")
    print(f"GPU Acceleration: {'Enabled' if args.enable_gpu and GPU_AVAILABLE else 'Disabled'}")
    print(f"CUDA Graph: {'Enabled' if args.use_cuda_graph else 'Disabled'}")
    print(f"Pinned Memory: {'Enabled' if args.use_pinned_memory else 'Disabled'}")
    print(f"Precision: complex128 (float64, PSS stability)")
    print(f"Profiling: interval={args.profile_interval} slots, "
          f"window={args.profile_window} samples, "
          f"dual_timer={'ON' if args.dual_timer_compare else 'OFF'}")
    print(f"Custom Channel: {'Enabled' if args.custom_channel else 'Disabled'}")
    if args.srs_only_channel:
        print(f"SRS-only CDL: ENABLED (full multipath only on symbol(s) "
              f"{args.srs_symbols}; other symbols flat mode={args.srs_flat_mode})")
        print(f"SRS-only DL flat: {'ENABLED (whole DL flattened for reliable attach)' if args.srs_only_dl_flat else 'DISABLED (DL on full CDL)'}")
    print(f"Path Loss: {path_loss_dB} dB (linear={pathLossLinear:.6f})")
    print(f"UL Pre-Gain: x{args.ul_pre_gain:g} "
          f"({'disabled' if args.ul_pre_gain == 1.0 else 'FFT-pre AGC emulation'})")
    print(f"Attach Stable: {args.attach_stable_sec:.1f} s "
          f"({'disabled' if args.attach_stable_sec == 0 else args.attach_stable_mode})")
    if args.attach_stable_mode == "auto" and args.attach_stable_sec > 0:
        print(f"Attach Trigger File: {args.attach_stable_trigger_file}")
    print(f"P1B Distance Mode: {args.p1b_distance_mode} "
          f"(default_distance={args.default_distance_m} m)")
    if noise_mode == "relative":
        print(f"AWGN Noise: Relative SNR mode (SNR={snr_dB} dB)")
    elif noise_mode == "absolute":
        _noise_rms = 32767.0 * (10.0 ** (noise_dBFS / 20.0))
        print(f"AWGN Noise: Absolute mode (floor={noise_dBFS} dBFS, rms={_noise_rms:.1f})")
    else:
        print(f"AWGN Noise: Disabled")
    gt_symbol_indices = [int(x) for x in args.gt_symbols.split(",")]
    srs_symbol_indices = [int(x) for x in args.srs_symbols.split(",")]
    gt_async_copy_bool = (args.gt_async_copy == "on")
    if args.gt_dir:
        print(f"GT Saver: {args.gt_dir} (every {args.gt_save_every} UL slot(s), "
              f"symbols={gt_symbol_indices}, async_copy={args.gt_async_copy}, "
              f"pinned_pool={args.gt_pinned_pool_size}, gpu_staging={args.gt_staging_gpu_buffers})")
    else:
        print(f"GT Saver: Disabled")
    print("=" * 80)

    print("\n[v8 Architecture]")
    if args.enable_gpu and GPU_AVAILABLE:
        print(f"  + Multi-UE: {args.num_ues} UE(s), per-UE IPC/pipeline/channel/noise")
        print("  + ChannelProducer: UnifiedChannelProducerProcess (single TF context, N_UE batch)")
        print("  + RingBuffer: CUDA IPC cross-process GPU ring buffer + non-blocking try_put_batch")
        if args.use_xla:
            print("  + XLA: JIT compilation enabled (kernel fusion)")
        else:
            print("  + XLA: Disabled (eager mode)")
        print("  + DL Broadcast: gNB dl_tx → per-UE channel → UE[k] dl_rx")
        if args.custom_channel:
            print("  + UL Superposition: UE[k] ul_tx → per-UE channel → sum → gNB ul_rx")
        else:
            print("  + UL Bypass: UE[k] ul_tx → sequential copy → gNB ul_rx")
        if args.ul_pre_gain != 1.0:
            print(f"  + UL Pre-Gain: fixed x{args.ul_pre_gain:g} before clip/cast to gNB ul_rx")
        print(f"  + GPU IPC V7 futex per-buffer antenna (gNB={args.gnb_ant}, UE={args.ue_ant})")
        print(f"  + gNB array: {args.gnb_ny}x{args.gnb_nx}, UE array: {args.ue_ny}x{args.ue_nx}")
        if args.use_cuda_graph:
            print(f"  + CUDA Graph (warmup {GPUSlotPipeline.WARMUP_SLOTS} slots, per-UE)")
        print("  + G1B v8 optimizations: CH_COPY view+release, NoiseProducer, pre-computed FFT")
        print(f"  + Noise model: {noise_mode} " +
              (f"(SNR={snr_dB}dB)" if noise_mode == "relative" else
               f"(floor={noise_dBFS}dBFS)" if noise_mode == "absolute" else "(off)"))
        print(f"  + WindowProfiler (interval={args.profile_interval}, window={args.profile_window})")
    print("=" * 80)
    print()

    proxy = Proxy(
        mode=args.mode,
        ue_port=args.ue_port, gnb_host=args.gnb_host, gnb_port=args.gnb_port,
        log_level=args.log, ch_en=args.ch_en, ch_dd=args.ch_dd, ch_L=args.ch_L,
        log_plot=args.log_plot, conv_mode=args.conv_mode, block_size=args.block_size,
        num_blocks=args.num_blocks, fft_lib=args.fft_lib,
        custom_channel=args.custom_channel,
        buffer_len=args.buffer_len, buffer_symbol_size=args.buffer_symbol_size,
        enable_gpu=args.enable_gpu, use_pinned_memory=args.use_pinned_memory,
        use_cuda_graph=args.use_cuda_graph,
        ipc_shm_path=args.ipc_shm_path,
        profile_interval=args.profile_interval,
        profile_window=args.profile_window,
        dual_timer_compare=args.dual_timer_compare,
        gnb_ant=args.gnb_ant,
        ue_ant=args.ue_ant,
        gnb_nx=args.gnb_nx,
        gnb_ny=args.gnb_ny,
        ue_nx=args.ue_nx,
        ue_ny=args.ue_ny,
        num_ues=args.num_ues,
        p1b_npz=args.p1b_npz,
        ue_rx_indices=resolved_rx_indices,
        use_xla=args.use_xla,
        gt_dir=args.gt_dir,
        gt_save_every=args.gt_save_every,
        gt_symbol_indices=gt_symbol_indices,
        gt_async_copy=gt_async_copy_bool,
        gt_pinned_pool_size=args.gt_pinned_pool_size,
        gt_staging_gpu_buffers=args.gt_staging_gpu_buffers,
        seed=args.seed,
        default_distance_m=args.default_distance_m,   # v7 V6-7
        attach_stable_sec=args.attach_stable_sec,
        attach_stable_mode=args.attach_stable_mode,
        attach_stable_trigger_file=args.attach_stable_trigger_file,
        p1b_distance_mode=args.p1b_distance_mode,
        ul_pre_gain=args.ul_pre_gain,
        srs_only_channel=args.srs_only_channel,
        srs_symbols=srs_symbol_indices,
        srs_flat_mode=args.srs_flat_mode,
        srs_only_dl_flat=bool(args.srs_only_dl_flat),
    )

    def _sigterm_handler(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm_handler)

    proxy.run()


if __name__ == "__main__":
    main()