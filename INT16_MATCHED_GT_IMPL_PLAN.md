# int16-matched GT 实施计划：通过 `libdfts.so` 实现 bit-exact 对齐

| 字段 | 值 |
|---|---|
| **日期** | 2026-05-13 |
| **前置** | `INT16_MATCHED_GT_PLAN.md` 方案 A |
| **目标** | 让 GT 完整经过 OAI 的 int16 SRS 链路，与 SRS 输出处于同一坐标系 |
| **关键发现** | `libdfts.so` 已在 OAI 构建目录中存在，可直接从 Python 调用 `dft2048` |

---

## 0. 关键发现：`libdfts.so` 已可用

OAI 的构建系统已经编译了独立的 DFT 共享库：

```
路径: DevChannelProxyJIN/openairinterface5g_whan/cmake_targets/ran_build/build/libdfts.so
类型: ELF 64-bit x86_64 shared object
依赖: 仅 libc.so.6 (+ 少量 OAI 符号需 stub)
导出: dft2048, dft1024, dft256, dft64, dfts_autoinit, init_rad2, init_rad4, ...
```

**已验证可从 Python ctypes 调用**（需加载一个 stub .so 提供 6 个日志/追踪符号）。

DC 测试：`dft2048(100+0j × 2048)` → DC bin = 4495，vs float FFT = 4525.5，比例 0.9933。
这 0.67% 的差异就是 int16 量化精度的直观体现。

---

## 1. 整体架构

```
                      Sionna H(f)
                          │
                          ▼
            ┌─────────────────────────┐
            │   GT 生成 (Python/GPU)   │
            │                         │
            │  1. X_ref ← 加载 OAI    │
            │     SRS ZC 参考序列      │
            │                         │
            │  2. Y(f) = H(f)·X_ref   │
            │     频域信道应用         │
            │                         │
            │  3. y(t) = IFFT(Y(f))   │
            │     CuPy float64 IFFT   │
            │                         │
            │  4. y_q = int16(y(t))   │
            │     量化① (同 Proxy)    │
            │                         │
            │  5. Y_rx = dft2048(y_q) │  ← libdfts.so (bit-exact)
            │     量化② (int16 FFT)   │
            │                         │
            │  6. H_LS = LS_div(Y_rx) │
            │     量化③ (>>9 截断)    │
            │                         │
            │  7. H_est = filt8(H_LS) │
            │     量化④ (饱和加)      │
            │                         │
            │  8. 保存 H_est → .npz   │
            └─────────────────────────┘
                          │
                 格式与现有 GT 兼容
                 eval_pdpR_nmse.py 无需改动
```

---

## 2. 需要的组件与状态

| # | 组件 | 状态 | 说明 |
|---|------|------|------|
| 1 | `libdfts.so` | ✅ 已有 | OAI build 目录已编译 |
| 2 | `liboai_stub.so` | ⚠️ 需创建 | 提供 6 个符号 stub (~30 行 C) |
| 3 | `OAIDftWrapper` | ⚠️ 需创建 | Python ctypes 封装类 (~60 行) |
| 4 | X_ref 获取 | ⚠️ 需实现 | OAI 侧 dump 一次 + Python 加载 |
| 5 | LS 除法 (Python) | ⚠️ 需实现 | int16 算术 (~15 行) |
| 6 | filt8 插值 (Python) | ⚠️ 需实现 | `c16multaddVectRealComplex` 仿真 (~40 行) |
| 7 | v8.py GTBatchSaver 集成 | ⚠️ 需修改 | GT_MODE 环境变量控制 (~30 行) |

---

## 3. 各组件详细设计

### 3.1 `liboai_stub.so` — OAI 符号 stub

`libdfts.so` 有 6 个非 glibc 的未解析符号：

| 符号 | 类型 | 用途 | stub |
|------|------|------|------|
| `exit_function` | 函数 | AssertFatal 使用 | `abort()` |
| `g_log` | 全局变量 | 日志级别控制 | 零初始化 struct |
| `logRecord_mt` | 函数 | 日志输出 | no-op |
| `T_active`/`T_cache`/`T_freelist_head`/`T_stdout` | 全局变量 | T tracer | 零初始化 |
| `write_file_matlab` | 函数 | Matlab dump | no-op |

**代码位置**: `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/oai_stub.c`
**编译**: `gcc -shared -fPIC -o liboai_stub.so oai_stub.c -lm`

### 3.2 `OAIDftWrapper` — Python ctypes 封装

```python
class OAIDftWrapper:
    """Thin ctypes wrapper around OAI's libdfts.so for bit-exact int16 FFT."""

    def __init__(self, libdfts_path, stub_path):
        self._stub = ctypes.CDLL(stub_path, mode=ctypes.RTLD_GLOBAL)
        self._lib = ctypes.CDLL(libdfts_path)
        self._lib.dfts_autoinit()

    def dft2048(self, x_int16_iq):
        """
        Parameters
        ----------
        x_int16_iq : ndarray, shape (4096,), dtype=int16
            Interleaved I/Q: [Re0, Im0, Re1, Im1, ...]

        Returns
        -------
        y_int16_iq : ndarray, shape (4096,), dtype=int16
            DFT output in same layout.
        """
        # 确保 32 字节对齐 (AVX2 要求)
        x_aligned = self._ensure_aligned(x_int16_iq, 32)
        y_aligned = np.zeros_like(x_aligned)

        self._lib.dft2048(
            x_aligned.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            y_aligned.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            ctypes.c_ubyte(1)  # scale=1, 与 OAI nr_slot_fep_ul 一致
        )
        return y_aligned[:len(x_int16_iq)]
```

**关键约束**:
- 输入/输出 buffer 必须 **32 字节对齐** (AVX2)
- `scale=1` — 与 OAI 的 `nr_slot_fep_ul → dft(..., 1)` 一致
- 数据布局: int16 交错 I/Q `[Re0, Im0, Re1, Im1, ...]`

### 3.3 SRS ZC 参考序列 (X_ref) 获取

**推荐方案: OAI 侧 dump 一次**

在 `phy_procedures_nr_gNB.c` 的 SRS 处理函数中添加:

```c
// 在 generate_srs_nr() 调用之后，仅执行一次
static int srs_ref_dumped = 0;
if (!srs_ref_dumped) {
    const char *dump_path = getenv("SRS_REF_DUMP_PATH");
    if (dump_path) {
        FILE *f = fopen(dump_path, "wb");
        if (f) {
            // header: ofdm_symbol_size (uint32), N_ap (uint32), M_sc_b_SRS (uint32)
            uint32_t hdr[3] = {fp->ofdm_symbol_size, N_ap, M_sc_b_SRS};
            fwrite(hdr, sizeof(uint32_t), 3, f);
            // per-port: ofdm_symbol_size * sizeof(c16_t) bytes
            for (int p = 0; p < N_ap; p++) {
                fwrite(srs_generated_signal[p], sizeof(c16_t),
                       fp->ofdm_symbol_size, f);
            }
            fclose(f);
            printf("[SRS dump] reference saved to %s\n", dump_path);
        }
        srs_ref_dumped = 1;
    }
}
```

**Python 加载**:
```python
def load_srs_ref(path):
    with open(path, 'rb') as f:
        ofdm_size, n_ap, m_sc = np.fromfile(f, dtype=np.uint32, count=3)
        data = np.fromfile(f, dtype=np.int16)
        # reshape to (n_ap, ofdm_size, 2) → complex
        data = data.reshape(n_ap, ofdm_size, 2)
        return data[..., 0] + 1j * data[..., 1]  # (n_ap, ofdm_size) complex
```

**此序列在同一 SRS 配置下不变**, dump 一次即可复用。

### 3.4 LS 除法 (Python, int16 算术)

```python
def srs_ls_estimate_int16(gen_signal, rx_signal, srs_gen_bits, K_TC, M_sc_b_SRS):
    """
    Parameters
    ----------
    gen_signal : ndarray (ofdm_size,) complex — X_ref int16 as complex
    rx_signal  : ndarray (ofdm_size,) complex — dft2048 output as complex
    srs_gen_bits : int — typically 9
    K_TC : int — comb size (2)
    M_sc_b_SRS : int — number of SRS subcarriers (624)

    Returns
    -------
    ls_out : ndarray (ofdm_size,) complex — LS estimates at pilot positions
    """
    ls_out = np.zeros(ofdm_size, dtype=np.complex64)
    # 遍历 SRS 导频位置
    for k in range(0, M_sc_b_SRS, K_TC // K_TC):  # 每 K_TC 个子载波一个导频
        # 梳齿内 CDM 累加 (fd_cdm = K_TC for comb_size=0 → K_TC=2)
        ls_r, ls_i = np.int16(0), np.int16(0)
        for cdm in range(K_TC):
            sc = pilot_subcarriers[k * K_TC + cdm]
            gr = np.int16(gen_signal[sc].real)
            gi = np.int16(gen_signal[sc].imag)
            rr = np.int16(rx_signal[sc].real)
            ri = np.int16(rx_signal[sc].imag)

            ls_r += np.int16(np.int32(
                np.int32(gr) * np.int32(rr) + np.int32(gi) * np.int32(ri)
            ) >> srs_gen_bits)
            ls_i += np.int16(np.int32(
                np.int32(gr) * np.int32(ri) - np.int32(gi) * np.int32(rr)
            ) >> srs_gen_bits)
        ls_out[pilot_subcarriers[k * K_TC]] = ls_r + 1j * ls_i
    return ls_out
```

### 3.5 filt8 插值 (Python, 仿真 `c16multaddVectRealComplex`)

```python
# OAI filt8 系数 (来自 filt16a_32.h)
FILT8_OPT = {
    'start':      np.array([16384, 8192,     0,     0, 0, 0, 0, 0], dtype=np.int16),
    'middle2':    np.array([    0, 8192, 16384, 8192, 0, 0, 0, 0], dtype=np.int16),
    'middle4':    np.array([    0,    0,     0, 8192, 16384, 8192, 0, 0], dtype=np.int16),
    'end_odd':    np.array([    0, 8192, 16384, 16384, 0, 0, 0, 0], dtype=np.int16),
    'end_even':   np.array([    0,    0,     0, 8192, 16384, 16384, 0, 0], dtype=np.int16),
}

def c16multadd(filt_coeffs, alpha_r, alpha_i, y_r, y_i):
    """Bit-exact simulation of c16multaddVectRealComplex.

    y += mulhrs(alpha, filt) * 2   (per-element, saturating)

    mulhrs(a,b) = (a*b + 0x4000) >> 15
    *2 via saturating add: adds(x, x)
    accumulate via saturating add: adds(y, prod)
    """
    for i in range(len(filt_coeffs)):
        # mulhrs
        prod_r = np.int16((np.int32(alpha_r) * np.int32(filt_coeffs[i]) + 0x4000) >> 15)
        prod_i = np.int16((np.int32(alpha_i) * np.int32(filt_coeffs[i]) + 0x4000) >> 15)
        # *2 saturating
        prod_r = np.int16(np.clip(np.int32(prod_r) * 2, -32768, 32767))
        prod_i = np.int16(np.clip(np.int32(prod_i) * 2, -32768, 32767))
        # accumulate saturating
        y_r[i] = np.int16(np.clip(np.int32(y_r[i]) + np.int32(prod_r), -32768, 32767))
        y_i[i] = np.int16(np.clip(np.int32(y_i[i]) + np.int32(prod_i), -32768, 32767))
```

### 3.6 v8.py GTBatchSaver 集成

```python
# stage_for_ue 修改 (概念)
def stage_for_ue(self, ue_idx, src_gpu):
    sym_idx = self.symbol_indices
    if self.gt_mode == 'int16_matched':
        # 取 H(f) 并通过 int16 SRS 链路
        h_freq = src_gpu[sym_idx].get()  # (n_sym, n_rx, n_tx, fft_size) complex128
        h_matched = self._apply_int16_pipeline(h_freq)
        return _GTToken(kind='sync', ue_idx=ue_idx, h_full=h_matched.astype(np.complex64))
    else:
        # 原有路径 (raw channel)
        ...
```

**环境变量控制**:
```
GT_MODE=int16_matched   # 新模式: GT 经过 int16 链路
GT_MODE=raw             # 默认: 保存原始 H(f)
SRS_REF_PATH=...        # X_ref dump 文件路径
```

---

## 4. 实施阶段

### Phase 0: 快速验证 (10 分钟, 零改动)

用方案 C (SRS 帧平均做 GT) 确认坐标系差异是唯一原因。

```bash
# 用现有数据, 纯评估逻辑
python3 -c "
import numpy as np
from digital_twin_stats import load_srs_v2
H_srs, meta = load_srs_v2('/path/to/snr_20dB')
H_avg = np.mean(H_srs[:100], axis=0, keepdims=True)
diff = H_srs[100:] - H_avg
nmse = 10*np.log10(np.mean(np.abs(diff)**2) / np.mean(np.abs(H_avg)**2))
print(f'NMSE (SRS vs SRS-avg): {nmse:.1f} dB')
# 预期: ~-20 dB (noise-limited)
"
```

**判断**: NMSE < -15 dB → 确认坐标系差异 → 继续 Phase 1

### Phase 1: libdfts.so 桥接 (2 小时)

| 步骤 | 做什么 | 产出 |
|------|--------|------|
| 1a | 创建 `oai_stub.c` + 编译 `liboai_stub.so` | stub 库 |
| 1b | 创建 `oai_dft_wrapper.py` (OAIDftWrapper 类) | Python DFT 封装 |
| 1c | 单元测试: DC / 纯音 / 随机输入，对比 float FFT | 验证 bit-exact |
| 1d | 性能测试: 2048 点 DFT × 1000 次的吞吐 | 确认不是瓶颈 |

### Phase 2: X_ref 获取 (1 小时)

| 步骤 | 做什么 | 产出 |
|------|--------|------|
| 2a | OAI 代码添加 X_ref dump (3 行 C, 环境变量控制) | 修改 `phy_procedures_nr_gNB.c` |
| 2b | 运行一次 OAI → 得到 `srs_ref_dump.bin` | X_ref 数据文件 |
| 2c | Python 加载 + 验证 (检查 \|X_ref\| ≈ 362, 非零位数 = M_sc_b_SRS) | 加载器 |

### Phase 3: 完整 pipeline (半天)

| 步骤 | 做什么 | 产出 |
|------|--------|------|
| 3a | 实现 LS 除法 Python 函数 | `srs_ls_estimate_int16()` |
| 3b | 实现 filt8 Python 函数 | `srs_filt8_interp_int16()` |
| 3c | 集成: H(f) → 量化① → dft2048 → LS → filt8 | `int16_matched_pipeline()` |
| 3d | **端到端验证**: pipeline 输出 vs 实际 OAI SRS 输出逐 SC 对比 | NMSE < -30 dB |

**验证方法 (Phase 3d)**:
- 用无噪声静态信道运行一次完整系统
- 保存 OAI 的 SRS 估计输出 (.bin)
- 用相同 H(f) 通过 Python pipeline 生成 GT
- 逐子载波对比 → 应该 bit-exact 或差异 < 1 LSB

### Phase 4: GTBatchSaver 集成 (2 小时)

| 步骤 | 做什么 | 产出 |
|------|--------|------|
| 4a | v8.py 添加 `GT_MODE` 环境变量处理 | 条件分支 |
| 4b | `stage_for_ue` 添加 int16_matched 路径 | GPU→CPU→pipeline→保存 |
| 4c | 完整 sweep 运行: SNR=20dB, speed=0 | NMSE 应降至 ~-20 dB |
| 4d | 完整 sweep 运行: SNR=20dB, speed=3 | 验证动态信道 |

---

## 5. 文件结构

```
DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/
├── oai_stub.c                 # [新] 6 个符号的 stub (30 行)
├── liboai_stub.so             # [新] 编译产物
├── oai_dft_wrapper.py         # [新] OAIDftWrapper + SRS pipeline (200 行)
├── test_oai_dft.py            # [新] 单元测试
├── v8.py                      # [改] GTBatchSaver 添加 int16_matched 模式
├── eval_pdpR_nmse.py          # [不变]
├── digital_twin_stats.py      # [不变]
└── data/
    └── srs_ref_dump.bin       # [新] X_ref (一次性 dump)
```

---

## 6. 当前系统参数 (已确认)

| 参数 | 值 | 来源 |
|------|-----|------|
| FFT_SIZE | 2048 | v8.py |
| K_TC | 2 | nr_radio_config.c:964 (transmissionComb_PR_n2) |
| M_SRS_PRB | 104 | nr_radio_config.c:930 (c_SRS=25) |
| M_SC_B_SRS | 624 | 104 × 12 / 2 |
| AMP | 512 | impl_defs_top.h (AMP_SHIFT=9) |
| srs_generated_signal_bits | 9 | log2_approx(512) |
| dft scale | 1 | slot_fep_nr.c:189 |
| N_ap | 1 or 2 | 取决于 UE 配置 |
| filt8 模式 | opt / legacy | 由 SRS_OPTFILT 环境变量控制 |

---

## 7. 风险与应对

| 风险 | 概率 | 影响 | 应对 |
|------|------|------|------|
| libdfts.so 版本与运行中 OAI 不一致 | 低 | dft 行为差异 | 确保同一 commit 编译 |
| X_ref subcarrier 映射 (k_0_p) 不匹配 | 中 | LS 结果错位 | Phase 3d 逐 SC 对比验证 |
| filt8 Python 仿真 rounding 差异 | 低 | 几 LSB 偏差 | filt8 可选择放入 C wrapper |
| GPU→CPU 传输延迟 (int16_matched 比 raw 慢) | 中 | GT 保存吞吐下降 | dft2048 ~μs 级, 不是瓶颈; 整体链路主要受 IO 限制 |

---

## 8. 预期结果

| 指标 | 当前 (float GT) | int16-matched GT |
|------|-----------------|------------------|
| NMSE (SNR=20, speed=0) | -6.8 ~ 0 dB | **~-20 dB** (noise-limited) |
| NMSE per-frame 方差 | 巨大 (STO-dependent) | **< 2 dB** (STO-invariant) |
| GT-SRS 坐标系差异 | 完全不同 | **bit-exact** |
| 全局 alpha 依赖 | ls_aligned 必须用 alpha | **raw NMSE 即可用** |
