# D0:Floor 来源诊断实施方案

| 字段 | 值 |
|------|----|
| **创建日期** | 2026-05-11 |
| **承接** | `SRS_2D_LRLS_MASTERPLAN.md` v2 §7 D0 |
| **状态** | DRAFT — 等待 sign-off |
| **预期工作量** | 0.5–1 天 |
| **Go/No-Go 输出** | `D0_RESULT_YYYYMMDD.md`(决策记录) |

---

## 0. 一页纸版

D0 通过实证回答**一个根本问题**:
> "PDP-R 撞的 -7 dB NMSE floor,到底来自 SRS estimator 内部、还是 OAI 下游处理 / 评估管线?"

**实施手段**:在 dispatcher 加 2 个新模式:
- `SRS_ESTIMATOR=passthru` — LS 直接输出,跳过 frequency interpolation(测 filt 贡献)
- `SRS_ESTIMATOR=oracle` — 注入 Sionna GT,绕过整个 SRS estimation(测下游 floor)

跑 SNR=20 sweep × 3 配置(legacy / passthru / oracle),NMSE 三个数字组合得出 §4 决策矩阵的结论。

**预期成本**:
- C 改 ≈ 80 行(2 个 dispatcher case + 1 个全局 mmap)
- Python 工具 ≈ 80 行(`gen_oracle_gt_bin.py`)
- Sweep 时间 ≈ 30 分钟(单 SNR × 3 算法)
- 离线分析 ≈ 30 分钟

---

## 1. 数据流诊断模型

```
Sionna GT  H_true(t,f)
      │
      ▼
┌─────────────────────────────────────────────────────────────────┐
│  OAI 接收链路:                                                  │
│   ADC → fixed-point IDFT → SRS pilot extract → LS estimate      │
│                                                                  │
│  ▶ 引入: ADC 量化噪声 + 位宽截断 + LS 1/N_pilot 噪声           │
│                                                                  │
│  输出: srs_ls_estimated_channel[]  (c16, K_TC=4 sparse)         │
└─────────────────────────────────────────────────────────────────┘
      │
      ▼ (这里 SRS_ESTIMATOR=passthru 直接输出, 不做 freq interp)
┌─────────────────────────────────────────────────────────────────┐
│  Legacy filt8/16 frequency interpolation                        │
│   ▶ 引入: 固定 FIR 截断误差 + interpolation 假设误差           │
│                                                                  │
│  输出: srs_estimated_channel_freq[]  (c16, dense)               │
└─────────────────────────────────────────────────────────────────┘
      │
      ▼ (这里 SRS_ESTIMATOR=oracle 注入 GT, 跳过上面两步)
┌─────────────────────────────────────────────────────────────────┐
│  OAI 下游处理:                                                  │
│   freq2time → time domain shift → bin dump → 评估管线          │
│                                                                  │
│  ▶ 引入: c16 量化 + IDFT 截断 + bin write 量化 + STO/CFO bias  │
└─────────────────────────────────────────────────────────────────┘
      │
      ▼
SRS bin file (per-frame, c16)
      │
      ▼
eval_pdpR_nmse.py (Python, complex128)
   ▶ 引入: STO 校正残差 + slot pairing 误差
      │
      ▼
NMSE p50 dB
```

→ **3 段噪声来源**:
- 段 A:LS estimate(ADC + 位宽截断 + pilot 稀疏 LS)
- 段 B:Legacy frequency interpolation(filt8/16 截断)
- 段 C:OAI 下游处理 + 评估管线(IDFT + c16 + STO + bin)

→ **3 个测量值**唯一映射 3 段噪声:

| 测量 | 公式 | 隔离的噪声段 |
|------|------|------------|
| `NMSE_legacy` | NMSE(GT, OAI(legacy filt 输出)) | A + B + C(全部) |
| `NMSE_passthru` | NMSE(GT, OAI(LS 直接输出)) | A + C(没 B) |
| `NMSE_oracle` | NMSE(GT, OAI(GT 注入)) | C(只下游) |

→ **段贡献分解**:
- 段 A(LS 阶段):`A = NMSE_passthru - NMSE_oracle`
- 段 B(filt 阶段):`B = NMSE_legacy - NMSE_passthru`(可正可负)
- 段 C(下游):`C = NMSE_oracle`

---

## 2. 决策矩阵(§7.1 详细版)

### 2.1 主决策(基于 oracle NMSE)

```
NMSE_oracle p50 @ SNR=20 dB
    │
    ├─ < -15 dB  ──→  ✅ GO LSL
    │                  下游噪声可忽略, floor 在 estimator 内,
    │                  时域+频域优化都有空间, 进 D1
    │
    ├─ -10 ~ -15 ──→  ⚠️ GO with reduced target
    │                  下游有 contribution 但不主导,
    │                  LSL target 调整为 "接近 oracle",
    │                  最大改进 ≈ |NMSE_oracle - (-7)|
    │
    ├─ -7 ~ -10  ──→  🟡 MARGINAL
    │                  下游主导, LSL 价值有限,
    │                  仍可继续但 expected gain ≤ 1 dB,
    │                  跟 LIULU 商量是否值得做
    │
    └─ ≈ -7 dB   ──→  ❌ STOP
                       下游完全主导, 改任何 estimator 都没用,
                       改去 audit OAI fixed-point pipeline
                       或直接 negative result 上报教授
```

### 2.2 副决策(基于 passthru vs legacy)

```
段 B = NMSE_legacy - NMSE_passthru
    │
    ├─ B < -2 dB     ──→  freq filt 是主要去噪手段, 频域改进有空间
    │                      (例如 EWMA-在-频域 也值得试)
    │
    ├─ B ≈ 0 ± 1     ──→  freq filt 接近 oracle 在 freq dim 的极限,
    │                      继续优化频域无用, 必须靠时域
    │
    └─ B > 0         ──→  ⚠️ filt 反而引入误差?
                            可能是 K_TC alias 或 OAI 实现 bug,
                            需要先 debug 再做任何改进
```

### 2.3 三测组合 → 综合结论

| 段 A | 段 B | 段 C | 推断 | 行动 |
|------|------|------|------|------|
| 大 | 中 | 小 | LS 是主要噪源(SRS pilot SNR 不够)| 提高 SRS power 或 长 pilot;时域 EWMA/LSL 也能压(因为 LS noise 是 i.i.d.)→ **GO LSL** |
| 中 | 大 | 小 | filt 截断误差大 | 改 filt 形状或加自适应 → 与 LSL 路线**正交**,可两者都做 |
| 小 | 小 | 大 | 下游 fixed-point 主导 | **STOP estimator 优化**,改方向 |
| 大 | 大 | 大 | 全部都贡献 | 优先压最大的;LSL 仍有部分价值 |

---

## 3. C 实现 patch

### 3.1 `nr_srs_mmse.h` 增加枚举

位置:第 13-18 行附近的 enum 块。

```c
typedef enum {
  NR_SRS_EST_LEGACY        = 0,
  NR_SRS_EST_MMSE1D        = 1,
  NR_SRS_EST_MMSE2D        = 2,   /* reserved for v2 LRLS */
  NR_SRS_EST_MMSE1D_ADAPT  = 3,
  /* D0 diagnostic modes ↓ */
  NR_SRS_EST_PASSTHRU      = 5,   /* LS 直接输出, 跳过 freq filt */
  NR_SRS_EST_ORACLE        = 6,   /* 注入 Sionna GT */
} nr_srs_estimator_mode_t;
```

### 3.2 `nr_srs_mmse.c` 加 oracle helper

新增一个 module-local oracle loader(用 mmap,启动一次):

```c
/* ── D0 Oracle GT injection ────────────────────────────────── */
#include <sys/mman.h>
#include <fcntl.h>
#include <unistd.h>

#define ORACLE_MAGIC 0xDA7AABCDu

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint32_t version;
    uint32_t n_entries;
    uint8_t  n_rx;
    uint8_t  n_tx;
    uint16_t n_sc;
    uint64_t _pad;
} oracle_header_t;

typedef struct __attribute__((packed)) {
    int64_t  slot_id;     /* abs slot index, matches SFN-unwrapped */
    uint32_t offset_c16;  /* offset in c16 units into payload */
} oracle_index_t;

static const oracle_header_t *g_oracle_hdr = NULL;
static const oracle_index_t  *g_oracle_idx = NULL;
static const c16_t           *g_oracle_payload = NULL;
static int g_oracle_inited = 0;

static int oracle_init_once(void) {
    if (g_oracle_inited) return g_oracle_hdr != NULL ? 0 : -1;
    g_oracle_inited = 1;

    const char *fp = getenv("SRS_ORACLE_GT_FILE");
    if (!fp || !*fp) {
        LOG_E(NR_PHY, "[D0/oracle] SRS_ORACLE_GT_FILE not set\n");
        return -1;
    }
    int fd = open(fp, O_RDONLY);
    if (fd < 0) { LOG_E(NR_PHY, "[D0/oracle] open(%s) failed\n", fp); return -1; }

    off_t sz = lseek(fd, 0, SEEK_END);
    void *p = mmap(NULL, sz, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (p == MAP_FAILED) { LOG_E(NR_PHY, "[D0/oracle] mmap failed\n"); return -1; }

    g_oracle_hdr = (oracle_header_t *)p;
    if (g_oracle_hdr->magic != ORACLE_MAGIC) {
        LOG_E(NR_PHY, "[D0/oracle] bad magic 0x%x\n", g_oracle_hdr->magic);
        return -1;
    }
    g_oracle_idx = (const oracle_index_t *)((const uint8_t *)p + sizeof(oracle_header_t));
    g_oracle_payload = (const c16_t *)(g_oracle_idx + g_oracle_hdr->n_entries);
    LOG_I(NR_PHY, "[D0/oracle] loaded %u entries, %ux%u ant, %u SC\n",
          g_oracle_hdr->n_entries, g_oracle_hdr->n_rx, g_oracle_hdr->n_tx, g_oracle_hdr->n_sc);
    return 0;
}

/* Binary-search for abs_slot. Returns offset_c16 or UINT32_MAX if not found. */
static uint32_t oracle_lookup(int64_t abs_slot) {
    if (!g_oracle_idx || !g_oracle_hdr) return UINT32_MAX;
    int lo = 0, hi = (int)g_oracle_hdr->n_entries - 1;
    while (lo <= hi) {
        int mid = (lo + hi) >> 1;
        if (g_oracle_idx[mid].slot_id == abs_slot) return g_oracle_idx[mid].offset_c16;
        if (g_oracle_idx[mid].slot_id < abs_slot) lo = mid + 1; else hi = mid - 1;
    }
    return UINT32_MAX;
}

/* Public API exposed to dispatcher */
int nr_srs_oracle_inject(c16_t *out, int n_sc, int ant, int port,
                          int64_t abs_slot) {
    if (oracle_init_once() != 0) return -1;
    uint32_t off = oracle_lookup(abs_slot);
    if (off == UINT32_MAX) {
        LOG_W(NR_PHY, "[D0/oracle] slot %ld not in GT, fallback Legacy\n",
              (long)abs_slot);
        return -1;
    }
    /* Layout in payload: [n_rx][n_tx][n_sc] of c16 */
    int per_entry = g_oracle_hdr->n_rx * g_oracle_hdr->n_tx * g_oracle_hdr->n_sc;
    int per_ant_port = g_oracle_hdr->n_sc;
    uint32_t cell_off = off + (uint32_t)(ant * g_oracle_hdr->n_tx + port) * per_ant_port;
    int copy_n = (n_sc < g_oracle_hdr->n_sc) ? n_sc : g_oracle_hdr->n_sc;
    memcpy(out, &g_oracle_payload[cell_off], copy_n * sizeof(c16_t));
    return 0;
}
```

### 3.3 `nr_srs_mmse.h` 公开 API

```c
/* D0 oracle injection: copy GT for (abs_slot, ant, port) into out[].
 * Returns 0 on success, -1 if not found (caller should fallback). */
int nr_srs_oracle_inject(c16_t *out, int n_sc, int ant, int port,
                          int64_t abs_slot);
```

### 3.4 `nr_ul_channel_estimation.c` dispatcher 加 case

位置:line 913 的 `if (...MMSE1D...)` 那个 if/else 块,改成 switch:

```c
const int64_t abs_slot = (int64_t)frame * 20 + slot;  /* assuming SCS-30k, 20 slots/frame */

switch (srs_estimator_mode) {

  case NR_SRS_EST_LEGACY:
    memcpy(srs_estimated_channel_freq[ant][p_index],
           &srs_est[mem_offset],
           (frame_parms->ofdm_symbol_size * (1 << srs_pdu->num_symbols)) * sizeof(c16_t));
    break;

  case NR_SRS_EST_MMSE1D:
  case NR_SRS_EST_MMSE1D_ADAPT: {
    /* ... existing PDP-R path, unchanged ... */
  } break;

  case NR_SRS_EST_PASSTHRU:
    /* ── D0.A: LS 直接输出, 跳过 freq filt ── */
    memset(srs_estimated_channel_freq[ant][p_index], 0,
           (frame_parms->ofdm_symbol_size * (1 << srs_pdu->num_symbols)) * sizeof(c16_t));
    memcpy(srs_estimated_channel_freq[ant][p_index],
           srs_ls_estimated_channel,
           frame_parms->ofdm_symbol_size * sizeof(c16_t));
    break;

  case NR_SRS_EST_ORACLE:
    /* ── D0.B: 注入 Sionna GT, fallback Legacy 若找不到 ── */
    memset(srs_estimated_channel_freq[ant][p_index], 0,
           (frame_parms->ofdm_symbol_size * (1 << srs_pdu->num_symbols)) * sizeof(c16_t));
    if (nr_srs_oracle_inject(srs_estimated_channel_freq[ant][p_index],
                              frame_parms->ofdm_symbol_size,
                              ant, p_index, abs_slot) != 0) {
      memcpy(srs_estimated_channel_freq[ant][p_index],
             &srs_est[mem_offset],
             (frame_parms->ofdm_symbol_size * (1 << srs_pdu->num_symbols)) * sizeof(c16_t));
    }
    break;

  default:
    /* fallback Legacy */
    memcpy(srs_estimated_channel_freq[ant][p_index],
           &srs_est[mem_offset],
           (frame_parms->ofdm_symbol_size * (1 << srs_pdu->num_symbols)) * sizeof(c16_t));
    break;
}
```

> ⚠️ 注意:`abs_slot` 计算方式必须跟 GT npz 的 `slot_ids` 对齐。Sionna 那边一般是单调累计,不会 SFN wrap;OAI 这边 `frame * 20 + slot` 在 SFN 翻 1024 处会 wrap。**首次集成跑 < 10s 数据没问题,长跑要加 SFN unwrap**(参考 `digital_twin_stats.py:unwrap_srs_abs_slots()`)。

### 3.5 `nr_srs_get_estimator_mode()` 解析新 env 值

位置:`nr_srs_mmse.c` 文件头部已有的 env getter,加分支:

```c
nr_srs_estimator_mode_t nr_srs_get_estimator_mode(void) {
  const char *e = getenv("SRS_ESTIMATOR");
  if (!e) return NR_SRS_EST_LEGACY;
  if (!strcmp(e, "legacy"))   return NR_SRS_EST_LEGACY;
  if (!strcmp(e, "mmse1d"))   return NR_SRS_EST_MMSE1D;
  if (!strcmp(e, "passthru")) return NR_SRS_EST_PASSTHRU;
  if (!strcmp(e, "oracle"))   return NR_SRS_EST_ORACLE;
  /* ... 其他 mode ... */
  return NR_SRS_EST_LEGACY;
}
```

---

## 4. Python 工具:`gen_oracle_gt_bin.py`

放在 `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/` 跟其他 eval 脚本一起。

```python
#!/usr/bin/env python3
"""
gen_oracle_gt_bin.py — convert Sionna GT npz directory into the binary
                        lookup file consumed by D0 oracle injection.

Output format:
  header  (24 bytes): magic, version, n_entries, n_rx, n_tx, n_sc, _pad
  index   (n_entries * 12 bytes): slot_id (i64), offset_c16 (u32)
  payload (n_entries * n_rx * n_tx * n_sc * 4 bytes): c16 (int16 r, int16 i)

Usage:
  # GT npz lives at <sweep_run_dir>/sionna_gt/gt_batch_ue0_seq*.npz
  python3 gen_oracle_gt_bin.py \\
      --gt-dir <sweep_run_dir>/sionna_gt \\
      --out    /tmp/oracle_gt.bin \\
      --srs-symbol 12 --ue 0 --scale 32767
"""
import argparse, os, sys, struct
import numpy as np
from pathlib import Path

ORACLE_MAGIC = 0xDA7AABCD

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt-dir", required=True)
    ap.add_argument("--out",    required=True)
    ap.add_argument("--srs-symbol", type=int, default=12)
    ap.add_argument("--ue", type=int, default=0)
    ap.add_argument("--scale", type=float, default=32767.0,
                    help="multiplier before int16 quantization")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    from digital_twin_stats import load_gt

    H, slot_ids = load_gt(args.gt_dir, srs_symbol=args.srs_symbol,
                          ue_idx=args.ue, return_slot_ids=True)
    if H.size == 0:
        sys.exit("no GT loaded")
    if slot_ids.size == 0:
        sys.exit("GT npz lacks slot_ids — required for oracle alignment")

    n_frames, n_rx, n_tx, n_sc = H.shape
    print(f"loaded {n_frames} frames, shape ({n_rx},{n_tx},{n_sc})")

    # Sort by slot_id (binary search assumes sorted)
    order = np.argsort(slot_ids)
    slot_ids = slot_ids[order]
    H        = H[order]

    # Quantize complex128 → int16 c16
    abs_max = float(np.max(np.abs(H)))
    if abs_max == 0:
        sys.exit("GT all zeros")
    norm = args.scale / abs_max
    H_q = np.empty((*H.shape, 2), dtype=np.int16)
    H_q[..., 0] = np.clip(np.round(H.real * norm), -32768, 32767)
    H_q[..., 1] = np.clip(np.round(H.imag * norm), -32768, 32767)
    print(f"abs(H) max={abs_max:.3e}, norm scale={norm:.3e}")

    # Layout per-entry payload as [rx][tx][sc] c16
    per_entry = n_rx * n_tx * n_sc

    header = struct.pack("<IIIBBHQ",
                         ORACLE_MAGIC, 1, n_frames,
                         n_rx, n_tx, n_sc, 0)
    index_bytes = bytearray()
    for i in range(n_frames):
        offset_c16 = i * per_entry
        index_bytes += struct.pack("<qI", int(slot_ids[i]), offset_c16)

    with open(args.out, "wb") as f:
        f.write(header)
        f.write(bytes(index_bytes))
        f.write(H_q.tobytes())

    sz = os.path.getsize(args.out)
    print(f"wrote {args.out}: {sz:,} bytes ({sz/1e6:.1f} MB)")
    print(f"  scale used = {norm:.6e} (must match OAI rxdataF scale!)")

if __name__ == "__main__":
    main()
```

> ⚠️ **量化 scale 必须跟 OAI rxdataF 的 c16 scale 一致**,否则注入的 GT 跟 LS estimate 量级不匹配,oracle 会得到莫名奇妙的高 NMSE。`--scale 32767` 是 conservative 起点;首跑后看 OAI LOG 中 `srs_ls_estimated_channel` 的 max abs 值校准。

---

## 5. 测试脚本:`d0_run.sh`

放在跟其他 sweep 一起的目录。

> **GT 数据流**(v2 修正):
> - GT npz 跟 SRS bin **共置**在每次 sweep run dir 的 `sionna_gt/` 子目录下,所以**不需要单独维护 GT 目录**。
> - 流程是:跑 legacy sweep → 用 legacy 的 GT 生成 oracle bin → 跑 passthru / oracle sweep。
> - 同 `CHANNEL_SEED=42` 下 Sionna 输出 deterministic,3 次 sweep 的 GT 应一致。

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT=/home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy
cd "$ROOT"

# Sweep 输出根(v8 sweep 默认 logs/q4_sweep_<TS>;这里用自定义 SWEEP_ROOT)
TS="$(date +%Y%m%d_%H%M)"
LOG_BASE="$ROOT/../../logs/d0_$TS"
mkdir -p "$LOG_BASE"

# D0 通用 sweep 参数:单 SNR=20,缩短 frames 加速
export CHANNEL_SEED=42
export ONLY_SNR=20
export MAX_FRAMES=100   # 够 NMSE p10/p50/p90;原 default 300 太慢
export MIN_SRS_BINS=1   # 单 bin 够 100 frame

# Step 1: 跑 legacy sweep(同时拿到 SRS bin + GT)
echo "=========== D0 step 1: legacy ==========="
bash preflight.sh
SWEEP_ROOT="$LOG_BASE/legacy" SRS_ESTIMATOR=legacy \
    bash run_q4_snr_sweep_v8.sh
LEGACY_RUN="$(ls -d "$LOG_BASE/legacy"/snr_20dB 2>/dev/null | head -1)"
GT_DIR="$LEGACY_RUN/sionna_gt"
[ -d "$GT_DIR" ] || { echo "ERROR: GT dir not found: $GT_DIR"; exit 1; }

# Step 2: 从 legacy GT 生成 oracle bin(单次)
ORACLE_BIN="$LOG_BASE/oracle_gt.bin"
echo "=========== D0 step 2: build oracle bin from $GT_DIR ==========="
python3 gen_oracle_gt_bin.py --gt-dir "$GT_DIR" --out "$ORACLE_BIN"

# Step 3: 跑 passthru + oracle sweep
for ALG in passthru oracle; do
  echo "=========== D0 step 3: SRS_ESTIMATOR=$ALG ==========="
  bash preflight.sh
  EXTRA=""
  if [ "$ALG" = "oracle" ]; then
    export SRS_ORACLE_GT_FILE="$ORACLE_BIN"
  fi
  SWEEP_ROOT="$LOG_BASE/$ALG" SRS_ESTIMATOR=$ALG \
      bash run_q4_snr_sweep_v8.sh
  unset SRS_ORACLE_GT_FILE
done

# Step 4: 评估
for ALG in legacy passthru oracle; do
  RUN_DIR="$(ls -d "$LOG_BASE/$ALG"/snr_20dB 2>/dev/null | head -1)"
  echo "=========== NMSE: $ALG ($RUN_DIR) ==========="
  python3 eval_pdpR_nmse.py \
      --run-dir "$RUN_DIR" \
      --sto-correct per-frame \
      | tee "$LOG_BASE/${ALG}_nmse.txt"
done

# Step 4: 决策
python3 - <<EOF
import re, sys, pathlib
def grab(name):
    p = pathlib.Path("$LOG_BASE") / f"{name}_nmse.txt"
    if not p.exists(): return None
    m = re.search(r"NMSE.*p50.*=\s*([-0-9.]+)\s*dB", p.read_text())
    return float(m.group(1)) if m else None

L = grab("legacy"); P = grab("passthru"); O = grab("oracle")
print(f"\nNMSE p50 @ SNR=20:")
print(f"  legacy   = {L}")
print(f"  passthru = {P}")
print(f"  oracle   = {O}")
if O is None:
    print("ORACLE failed — check GT bin / slot alignment")
    sys.exit(2)

print(f"\nDecomposition:")
if O is not None and P is not None:
    print(f"  Section A (LS)        = {P - O:+.2f} dB")
if L is not None and P is not None:
    print(f"  Section B (filt)      = {L - P:+.2f} dB")
print(f"  Section C (downstream) = {O:+.2f} dB")

print(f"\nDecision:")
if O > -10:
    print("  ❌ STOP — downstream dominates, don't proceed to LSL")
elif O > -15:
    print("  ⚠️  GO with reduced target ≈ {:.1f} dB".format(O))
else:
    print("  ✅ GO LSL — floor is in estimator")
EOF
```

---

## 6. 决策记录模板:`D0_RESULT_YYYYMMDD.md`

D0 跑完后填这个模板,作为 sign-off 进 D1 的依据。

```markdown
# D0 Floor Diagnosis — Result Record

| Field | Value |
|-------|-------|
| Date | YYYY-MM-DD |
| GT source dir | logs/... |
| Oracle bin path | /tmp/... |
| Oracle bin size | XX MB |
| OAI rxdataF scale used | XXX (compared to default Y) |
| Sionna config Doppler | Z Hz (R8) |
| Empirical T_c | 1/Z = ZZ ms |
| SRS period T_SRS | XX ms |
| T_c / T_SRS ratio | NN (LSL viable iff > 5) |

## Measurements (SNR = 20 dB, seed = 42)

| Mode | NMSE p50 (dB) | NMSE p10 / p90 |
|------|---------------|----------------|
| legacy   | -X.X | -Y.Y / -Z.Z |
| passthru | -X.X | -Y.Y / -Z.Z |
| oracle   | -X.X | -Y.Y / -Z.Z |

## Decomposition

- Section A (LS estimate) = passthru - oracle = X.X dB
- Section B (filt)        = legacy - passthru = X.X dB
- Section C (downstream)  = oracle             = X.X dB

## Decision

- [ ] ✅ GO LSL (oracle < -15)
- [ ] ⚠️ GO reduced target (-15 ≤ oracle < -10)
- [ ] 🟡 MARGINAL (oracle in -10 ~ -7), discuss with LIULU
- [ ] ❌ STOP (oracle ≈ -7), audit fixed-point pipeline instead

## Notes

(Anything anomalous: oracle higher than expected, GT alignment issues,
 SFN wrap, scale mismatch warning, etc.)
```

---

## 7. 额外:R8 Sionna config 检查 sub-task

D0 期间顺便完成,15 分钟工作量。

```bash
# 找 sionna config
find DevChannelProxyJIN -name "*.yml" -o -name "*.yaml" -o -name "sionna*config*" \
    | xargs -I{} grep -l -i "doppler\|velocity\|mobility" {} 2>/dev/null
```

输出:
- 如果找到 `velocity = 0` 或 `mobility = static` → T_c → ∞,LSL 完美场景
- 如果找到 `velocity = X km/h` → 算 f_d = X/3.6 × f_c/3e8(典型 3.5 GHz),T_c = 1/f_d
- 把结果填到 §6 的 R8 表格里

**T_c < 5 × T_SRS** → LSL λ 必须设小(0.5-0.7),不要默认 0.97。

---

## 8. 时间预算细分

| Sub-task | 估算 |
|----------|------|
| D0.1 加 enum (1 行编辑) | 5 min |
| D0.2 dispatcher case (~30 行 C) | 1 h |
| 写 oracle helper (~80 行 C) | 1 h |
| 写 `gen_oracle_gt_bin.py` (~80 行) | 30 min |
| Build + smoke test (单 frame) | 30 min |
| 跑 d0_run.sh (3 sweep + eval) | 30 min |
| 决策 + 写 D0_RESULT.md | 30 min |
| R8 Sionna config 检查 | 15 min |
| **合计** | **~4-5 hours** |

理论上半天就能拿到决策,留半天做 buffer。

---

## 9. 已知风险

| 风险 | 应对 |
|------|------|
| GT slot_id 跟 OAI abs_slot 算法不一致 | 先打 LOG 看一组对比,若错位 ± offset,用 `SRS_ORACLE_GT_OFFSET` env 调 |
| OAI rxdataF c16 scale 跟 GT 量级不一致 | 首跑看 oracle NMSE 是否 > 0 dB(异常高);如果是,调 `--scale` |
| GT 文件没 slot_ids key(老 sionna-proxy 输出) | `gen_oracle_gt_bin.py` 直接报错;需要重跑一次 sionna-proxy 拿带 slot_ids 的 GT |
| oracle case 让 OAI crash(memcpy 越界) | 加 assert + LOG_W,用 `n_sc < g_oracle_hdr->n_sc` 兜底 |
| sweep 跑不到 SRS 调度(OAI 不发 SRS) | 跟现有 sweep 一样,看 SRS Dump captured rate;< 1% 则 sweep 数据不可信 |

---

## 10. Sign-off 区(等 LIULU 确认)

- [ ] §1 数据流模型 — 接受 3 段噪声分解逻辑
- [ ] §2 决策矩阵 — 接受决策门槛(oracle < -15 GO,> -10 STOP)
- [ ] §3 C 实现 patch — 接受改动范围(2 个 case + 1 个 oracle helper)
- [ ] §4 Python 工具 quantization scale 默认 32767 — 接受首跑校准方式
- [ ] §5 测试脚本 — 接受单 SNR=20 是否够(可选:加 SNR=10/15)
- [ ] §6 决策记录模板 — 接受
- [ ] §7 R8 Sionna config 检查 — 接受顺带做
- [ ] §8 时间预算 — 接受半天工作量

---

**完。等 sign-off 后从 §3 开始编码。**
