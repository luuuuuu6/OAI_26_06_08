# 突破 -7 dB 评估 Floor：int16-matched GT 方案

| 字段 | 值 |
|---|---|
| **日期** | 2026-05-13 |
| **前置** | `FILT8_OPT_DEBUG_LOG.md` Section 8.14 最终诊断 |
| **目标** | 消除 float64 GT vs int16 SRS 的数值坐标系差异，使 NMSE 评估能反映算法真实精度 |

---

## 1. 问题定义

### 1.1 当前两条链路的数学差异

```
GT 链路 (Proxy, float64):
  Sionna H(f) [complex128]
    → ref_norm 归一化: H_norm = H / √E[|h|²]
    → CuPy FFT (前向无 ÷N)
    → 截断为 complex64
    → 写入 .npz h_matrix

SRS 链路 (OAI, int16):
  UE 发射 SRS: X_ue[k] = round(AMP · ZC[k] / √N_ap) >> 15  [int16]
    → Proxy 信道应用: y(t) = IFFT(H(f)·FFT(x_ue(t)))       [float64]
    → round + clip(-32768, 32767) → int16 写入 shm            ← 量化①
    → OAI int16 FFT (scale=1, 每级 >>1, 总 ÷√2048)            ← 量化②
    → LS: (conj(X_ref)·Y_rx).r >> srs_gen_bits → int16        ← 量化③
    → filt8: int16 overlap-add (adds_epi16 饱和加)             ← 量化④
    → 写入 .bin
```

**数学上**：
- GT = `H(f) / ref_norm` — 归一化的理想频域信道
- SRS = `int16(conj(X) · int16_FFT(int16(IFFT(H·X))) >> 9)` — 经过四层 int16 量化的 LS 估计

**这两个量在数值格式、缩放因子、量化噪声上完全不同。**

### 1.2 为什么全局 alpha 不能修复

LS-aligned NMSE 用全局复数 α 去缩放 GT 对齐 SRS：

```
α = <SRS, GT> / <GT, GT>
NMSE = ||SRS - α·GT||² / ||SRS||²
```

α 能消除全局幅度/相位差异，但 **不能消除 per-subcarrier 的 int16 量化 pattern**。
这些 per-SC distortion 随 STO 变化，导致不同帧有不同的 NMSE（-6.8 到 -0.01 dB）。

### 1.3 Python float 仿真的决定性证据

| 条件 | filt8 NMSE | sinc NMSE | 说明 |
|---|---|---|---|
| float64, SNR=20dB | -22.2 dB | -20.5 dB | noise-limited, 全部 STO 下一致 |
| float64, 无噪声 | -28.9 dB | -85 dB | filt8 系数的理论误差 |
| **实际 OAI int16** | **-6.8 ~ 0 dB** | — | **22+ dB 差距完全来自 int16 链路** |

---

## 2. 三个解决方案

### 2.1 方案 C: SRS 帧平均做 GT（零改动快速验证）

**思路**：不用 Sionna GT，用 OAI 自身 SRS 输出的时间平均做参考。

```python
H_gt_avg = np.mean(H_srs[:N_avg], axis=0)
# 每帧 NMSE = ||SRS[i] - H_gt_avg||² / ||SRS[i]||²
```

| 项目 | 说明 |
|---|---|
| 改动量 | 0 行（纯评估逻辑） |
| 适用场景 | **仅静态信道** (speed=0) |
| 预期 NMSE | noise-limited ~-20 dB @ SNR=20dB |
| 优点 | GT 和 SRS 完全同一坐标系；无 α 对齐问题 |
| 缺点 | 动态信道下帧平均无意义；不是绝对误差评估 |
| 目的 | **5 分钟内确认 -7 dB 是否全部来自坐标系差异** |

**判断标准**：
- NMSE ≈ -20 dB → 确认坐标系差异是唯一原因 → 继续 Phase 2
- NMSE 仍 ≈ -7 dB → 存在其他问题 → 需重新诊断

### 2.2 方案 B: Proxy 端量化模拟（简化版）

**思路**：在 GT 保存前，模拟 Proxy→shm 的 int16 量化（量化①），用 float FFT 回频域。

```python
def generate_quant_matched_gt(H_f, n_fft, pl_linear):
    """只模拟时域 int16 量化 + float FFT 回频域"""
    # H(f) → 时域 (含 path loss 缩放)
    h_time = cp.fft.ifft(H_f, axis=-1) * pl_linear

    # 模拟量化① — 这是最大的精度损失点
    h_q = cp.clip(cp.around(h_time.real), -32768, 32767) + \
          1j * cp.clip(cp.around(h_time.imag), -32768, 32767)

    # float FFT 回频域 (近似 OAI FFT，跳过 int16 FFT 的逐级 >>1)
    H_quant = cp.fft.fft(h_q, axis=-1) / cp.sqrt(n_fft)
    return H_quant
```

| 项目 | 说明 |
|---|---|
| 改动量 | ~20 行 (v8.py GTBatchSaver) |
| 适用场景 | 静态 + 动态信道 |
| 预期 NMSE | 大幅改善，但可能有 ~3-5 dB 残差 |
| 优点 | 实现简单；捕获最大量化损失源 |
| 缺点 | 未模拟 int16 FFT 精度损失 (量化②) 和 LS 除法 (量化③) |

**改动位置**：`v8.py` `GTBatchSaver.stage_for_ue()` 中，把 `channels_ul` 经过量化模拟后再保存。

### 2.3 方案 A: 完整 int16 链路模拟（产品级）

**思路**：在 Proxy 端完整复现 OAI 的 SRS LS 估计链路的 int16 行为。

```python
def generate_int16_matched_gt(H_f, X_ref_freq, srs_gen_bits, n_fft):
    """完整模拟 OAI SRS LS 估计链路"""
    # Step 1: 信道应用 (float64)
    Y_f = H_f * X_ref_freq
    y_time = cp.fft.ifft(Y_f, axis=-1)

    # Step 2: 量化① Proxy→shm int16
    y_q = clip_round_int16(y_time)

    # Step 3: 量化② 模拟 OAI int16 FFT (scale=1)
    Y_rx = simulate_oai_int16_fft(y_q, n_fft)

    # Step 4: 量化③ LS 除法
    # conj(X_ref) · Y_rx, int32 乘积 >> srs_gen_bits → int16
    ls_r = (X_ref_freq.real * Y_rx.real + X_ref_freq.imag * Y_rx.imag)
    ls_i = (X_ref_freq.real * Y_rx.imag - X_ref_freq.imag * Y_rx.real)
    H_LS_r = np.int16(np.int32(ls_r) >> srs_gen_bits)
    H_LS_i = np.int16(np.int32(ls_i) >> srs_gen_bits)

    # Step 5: 量化④ filt8 插值 (可选，取决于要对比 filt8 前还是后)
    H_est = simulate_filt8_comb2(H_LS_r + 1j * H_LS_i, K_TC=2)

    return H_est
```

| 项目 | 说明 |
|---|---|
| 改动量 | ~200 行 (v8.py + 新模块) |
| 适用场景 | 所有场景（静态 + 动态 + 多 SNR） |
| 预期 NMSE | noise-limited (-20 dB)，完全消除坐标系差异 |
| 核心难点 | `simulate_oai_int16_fft` — 见下面技术细节 |
| 次要难点 | Proxy 端生成与 OAI 完全一致的 SRS ZC 参考序列 |
| 风险 | int16 FFT 行为不完全匹配会留下几 dB 残差 |

---

## 3. 关键技术细节

### 3.1 OAI SRS 参考序列

```c
// srs_modulation_nr.c
AMP = 1 << AMP_SHIFT   // AMP_SHIFT=9 (non-BIT8_TX) → AMP=512
srs_generated_signal_bits = log2_approx(AMP) = 9

// 频域参考:
r_amp.r = (int16_t)((int32_t)round((double)amp * r.r / sqrt_N_ap) >> 15);
r_amp.i = (int16_t)((int32_t)round((double)amp * r.i / sqrt_N_ap) >> 15);

// 其中 r 是 ZC 序列 (complex double), sqrt_N_ap = sqrt(N_antenna_ports)
// 结果: |X_ref| ≈ 362 (int16), 占 int16 动态范围的 ~1.1%
```

### 3.2 OAI int16 FFT (DFT2048)

```c
// oai_dfts.c — dft2048, scale=1
// 分解: 2048 = 32 × 64 (或类似 radix 组合)
// 每级 butterfly: mulhrs_epi16 (Q15 乘法) + adds_epi16 (饱和加)
// scale=1 → 每级末尾 >>1

// 总缩放 ≈ ÷√2048 ≈ ÷45.25
// 有效精度: 输入 16 bit → 输出 ~10-11 bit
// per-SC 量化噪声不均匀 → 这就是 STO-dependent distortion 的来源
```

**模拟难度**：需要精确复现 OAI 的 DFT 分解路径（哪些 radix、什么顺序、在哪里插入 >>1）。
替代方案：用 float FFT + 人工添加与实际测量匹配的量化噪声模型。

### 3.3 LS 除法 (int16 乘法 + 右移)

```c
// nr_ul_channel_estimation.c L826-849
// 对 K_TC 个 CDM 内子载波求和:
for (cdm_idx = 0; cdm_idx < fd_cdm; cdm_idx++) {
    ls.r += (int16_t)(((int32_t)gen.r * rx.r + (int32_t)gen.i * rx.i)
                      >> srs_generated_signal_bits);
    ls.i += (int16_t)(((int32_t)gen.r * rx.i - (int32_t)gen.i * rx.r)
                      >> srs_generated_signal_bits);
}

// 数学: H_LS ≈ conj(X_ref) · Y_rx / 2^9
// 精度: int16×int16→int32, >>9, 截断回 int16
// 如果 rx 只有 10 bit 有效 (来自 FFT), gen 有 ~9 bit:
//   乘积 ≈ 19 bit, >>9 后 ≈ 10 bit 有效
```

### 3.4 GT 保存格式（兼容性要求）

```python
# 当前 GTBatchSaver 输出 (.npz):
{
    'h_matrix':       complex64,  (batch, n_sym, n_rx, n_tx, fft_size)
    'slot_ids':       uint32,     (batch,)
    'bypass_flags':   uint8,      (batch,)
    'symbol_indices': uint32,     (n_sym,)
    'gnb_ant':        uint32
    'ue_ant':         uint32
    'fft_size':       uint32
}

# int16-matched GT 应保持相同 key 和 shape
# h_matrix 的数值含义从 "H(f)" 变为 "H_LS_simulated(f)"
# 添加 'gt_mode': 'int16_matched' | 'raw_channel' 标记以区分
# load_gt() 和 eval_pdpR_nmse.py 无需改动（格式相同）
```

---

## 4. 执行计划

```
Phase 1: 方案 C 快速验证 (5 min, 零改动)
  ├─ 用现有 q4_sweep_20260513_000323 数据
  ├─ SRS 100 帧平均做 GT
  ├─ 每帧 SRS vs 帧平均 → NMSE
  ├─ 预期: ~-20 dB (确认坐标系差异是唯一原因)
  └─ 判断: NMSE < -15 dB → 继续 Phase 2
           NMSE > -10 dB → 重新诊断

Phase 2: 方案 B 简化实现 (半天)
  ├─ v8.py GTBatchSaver 添加 int16 量化模拟
  │   - 新增环境变量 GT_MODE=int16_matched | raw (默认 raw)
  │   - int16_matched: H(f) → IFFT → int16 量化 → float FFT → 保存
  ├─ 跑 speed=0, SNR=20 验证
  ├─ 跑 speed=3, SNR=20 验证
  └─ 评估: 与 Sionna GT 对比残差

Phase 3: 方案 A 完整实现 (仅当 Phase 2 残差 > 3 dB)
  ├─ 实现 simulate_oai_int16_fft (Python/CuPy)
  ├─ 移植 OAI SRS ZC 序列生成逻辑到 Python
  ├─ 完整模拟 LS 除法 + filt8
  └─ 验证: 与实际 OAI 输出逐 SC 对比

Phase 0 (并行推进): 2D MMSE 算法开发
  ├─ 使用方案 C 的评估方法 (SRS 帧平均做 GT)
  ├─ 不受 -7 dB floor 限制
  └─ 这是教授 4/29 要求的研究方向，优先级最高
```

---

## 5. 关键代码位置参考

| 功能 | 文件 | 关键行 |
|---|---|---|
| OAI SRS 参考序列生成 | `srs_modulation_nr.c` | L270 (srs_gen_bits), L383 (r_amp) |
| OAI SRS 接收拷贝 | `srs_rx.c` | L121 (srs_received_signal) |
| OAI LS 除法 | `nr_ul_channel_estimation.c` | L826-849 |
| OAI filt8 插值 | `nr_ul_channel_estimation.c` | L875-919 |
| OAI int16 FFT | `oai_dfts.c` (dft2048) | scale=1 |
| OAI AMP 定义 | `impl_defs_top.h` | AMP_SHIFT |
| Proxy GT 保存 | `v8.py` GTBatchSaver | L1265 (savez) |
| Proxy 信道归一化 | `v8.py` ChannelProducer | L2532 (ref_norm) |
| Proxy int16 量化 | `v8.py` _gpu_compute_core | L1582 (clip+around) |
| GT 加载 | `digital_twin_stats.py` load_gt | L131 |
