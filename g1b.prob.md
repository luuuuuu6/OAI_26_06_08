# 当前 G1C v7.py SRS 与 GT 提取方式详细分析

---

## 1. 全链路数据流概览

```
                    ┌─────────── Sionna Channel Proxy (v7.py) ───────────┐
                    │                                                     │
  P1B Rays ──→ ChannelCoefficientsGeneratorJIN ──→ h(τ,t)               │
                    │         ↓ FFT                                      │
                    │      H(f,t) [complex128, 2048 bins]                │
                    │         ↓ energy normalization                      │
                    │      IPCRingBuffer (per-UE, per-symbol)            │
                    │         ↓                                          │
                    │  ┌──── _ipc_ul_superposition_slot(ts) ────┐       │
                    │  │                                         │       │
                    │  │  channels_ul = ring_buffer.get(14 sym)  │       │
                    │  │       │                                 │       │
                    │  │       ├──→ process_slot_ipc()           │       │
                    │  │       │     X(f)=FFT(去CP后的UE IQ)     │       │
                    │  │       │     Y(f)=H(f)·X(f)+N(f)        │       │
                    │  │       │     y(t)=IFFT(Y(f))            │       │
                    │  │       │     加CP → 写入 gNB UL buffer   │       │
                    │  │       │                                 │       │
                    │  │       └──→ gt_saver.record_ul_slot()    │       │
                    │  │             保存 H(f)[symbol 12]        │       │
                    │  │             slot_id = ts // 30720       │       │
                    │  └─────────────────────────────────────────┘       │
                    └────────────────────┬──────────────────────────────┘
                                         │ GPU IPC 共享内存
                                         ▼
                    ┌─────────── OAI gNB (nr-softmodem) ────────────────┐
                    │                                                     │
                    │  接收 UL IQ (int16, 含 CP)                         │
                    │       ↓ 去 CP (按 NR 标准 CP 长度)                 │
                    │       ↓ FFT                                        │
                    │    Y_rx(f) = 接收频域信号                           │
                    │       ↓ LS 估计: H_est(k) = Y_rx(k)·P*(k)         │
                    │       ↓ FIR 频域插值 (comb → 全子载波)             │
                    │    srs_estimated_channel_freq[rx][tx][2048]         │
                    │       ↓ 写入 ping-pong 双缓冲                      │
                    │       ↓ 异步写盘线程                               │
                    │    srs_matrix_gNB_2x2_seq{N}.bin                   │
                    └────────────────────────────────────────────────────┘
```

---

## 2. GT 提取细节

### 2.1 GT 保存的是什么？

**GT = Proxy 实际用来对 UE 信号做频域卷积的那个 H(f)**

具体代码路径：

```python
# _ipc_ul_superposition_slot() 中：
channels, n_held = self.channel_buffers[k].get_batch_view(N_SYM)  # 14 symbols
channels_ul = channels.transpose(0, 2, 1, 3)  # → (14, gnb_ant, ue_ant, 2048)

# 同一个 channels_ul 被用于两个目的：
# 目的 1：实际应用到信号
self.pipelines_ul[k].process_slot_ipc(arr_in, channels_ul, ...)

# 目的 2：保存为 GT
gt_captures.append((k, channels_ul))
# ...
self.gt_saver.record_ul_slot(gt_captures, ipc_ts=ts, partial_bypass=...)
```

**关键**：GT 和实际应用到信号的 H(f) 是**同一个对象**，不存在"GT 是一个版本、实际用的是另一个版本"的不一致。

### 2.2 GT 的 OFDM Symbol 选择

```python
# GTBatchSaver.record_ul_slot() 中：
sym_idx = self.symbol_indices  # 默认 [12]（SRS 所在 symbol）
h_full = ch_ul[sym_idx].get().astype(np.complex64)
```

- 默认只保存 **symbol 12**（SRS 位置）
- CLI 参数 `--gt-symbols "12"` 可修改
- SRS symbol 位置推导：`startPosition=1` → `l₀ = 14 - 1 - 1 = 12`

### 2.3 GT 的时间戳

```python
if ipc_ts is not None:
    slot_id = int(ipc_ts) // 30720  # 30720 = sum(SYMBOL_SIZES) = 1 slot 的采样数
```

- `ipc_ts` 是 IPC 层的采样级时间戳（从 0 开始单调递增）
- `slot_id = ts // 30720` = NR 绝对 slot 编号
- 与 SRS 的 `frame_id * 20 + slot_id` 在同一刻度上

### 2.4 GT 的维度与精度

| 属性 | 值 |
|------|-----|
| 物理量 | 频域信道 H(f) |
| 数据类型 | complex64（保存时）/ complex128（GPU 计算时） |
| Shape | `(batch_size, n_sym_saved, gnb_ant, ue_ant, 2048)` |
| 子载波 | 全部 2048 个 FFT bin |
| 归一化 | Sionna 能量归一化（batch 0 参考，全局固定缩放） |

### 2.5 Bypass 标记

当某个 UE 的 ring buffer 超时（`TimeoutError`），该 UE 被跳过，`n_bypassed += 1`。如果有任何 UE 被跳过，整个 slot 的 GT 被标记为 `partial_bypass=True`。分析脚本加载时会跳过这些帧。

---

## 3. SRS 提取细节

### 3.1 SRS 保存的是什么？

**SRS = gNB 对 UL SRS 参考信号做 LS 估计 + FIR 插值后的频域信道估计**

代码路径（`nr_srs_channel_estimation()`）：

```c
// 1. LS 估计（在 SRS comb 位置）
for (int k = 0; k < M_sc_b_SRS; k++) {
    // CDM 合并：多端口相干累加
    srs_ls_estimated_channel[subcarrier] = Y(k) · P*(k);
}

// 2. FIR 频域插值（从 comb 展开到全子载波）
// 使用 filt8_*/filt16_* 固定系数
c16multaddVectRealComplex(filt_coeff, &ls_estimated, srs_estimated_channel16, 16);

// 3. 可选 MMSE 1D 滤波（如果 srs_estimator_mode == NR_SRS_EST_MMSE1D）
// 否则直接 memcpy

// 4. 拷贝到 Digital Twin 双缓冲
memcpy(ctx->srs_buffer[w_idx][ctx->dump_count][rx][tx],
       srs_estimated_channel_freq[rx][tx],
       num_elements * sizeof(c16_t));
```

### 3.2 SRS 的时间戳

```c
ctx->meta_frame[w_idx][ctx->dump_count] = frame;  // NR SFN (0-1023)
ctx->meta_slot[w_idx][ctx->dump_count] = slot;    // 0-19 (μ=1)
```

- `frame` = NR System Frame Number，10-bit，每 10.24s 回绕
- `slot` = slot within frame，0-19
- 绝对 slot = `frame * 20 + slot`（需要 unwrap 处理回绕）

### 3.3 SRS 的维度与精度

| 属性 | 值 |
|------|-----|
| 物理量 | 频域信道估计 H_est(f) |
| 数据类型 | c16_t (int16 I + int16 Q) |
| Shape | `(n_frames, rx_ants, tx_ants, ofdm_symbol_size)` |
| 子载波 | 全部 2048 个 FFT bin（FIR 插值后） |
| 归一化 | OAI 内部定点增益（与 GT 差 ~500-2000 倍） |

### 3.4 SRS 缺失的处理环节（vs DMRS）

| 环节 | DMRS 有 | SRS 有 | 影响 |
|------|---------|--------|------|
| LS 估计 | ✅ | ✅ | — |
| FIR 插值 | ✅ | ✅ | — |
| **时延估计 `nr_est_delay()`** | ✅ | ❌ | SRS 含未补偿的 STO 相位斜率 |
| **`delay_table` 相位补偿** | ✅ | ❌ | 同上 |
| **多符号时域平均** | ✅ | ❌ | SRS 通常只有 1 symbol |
| PTRS 相位跟踪 | ✅ | N/A | SRS 无 PTRS |

→ 分析脚本用 `method_e_align()` 的 per-frame STO 校正来弥补 C 层缺失的 delay 补偿。

---

## 4. GT 与 SRS 的精确物理对应

### 4.1 信号链路中 GT 和 SRS 的位置

```
UE 发送 SRS 导频 P(k)
    ↓
Proxy 频域卷积: Y(k) = H(k) · P(k) + N(k)     ← H(k) 就是 GT
    ↓ IFFT + 加 CP
    ↓ 写入 gNB UL ring buffer (int16)
    ↓
gNB 接收: 去 CP + FFT → Y_rx(k)
    ↓
gNB LS 估计: H_est(k) = Y_rx(k) · P*(k)       ← H_est(k) 就是 SRS
    ↓ FIR 插值
    ↓ 写入 bin 文件
```

理论关系：
```
H_est(k) = H(k) + N(k)/P(k) + quantization_error + interpolation_error
         = GT(k) · α + noise + systematic_bias
```

其中 `α` 是未知复数增益（包含 OAI 内部定点缩放 + Proxy 归一化差异）。

### 4.2 为什么需要 per-frame LS α 对齐？

1. **幅度差异**：GT 是 Sionna 归一化后的值（~1.0 量级），SRS 是 int16（~500-2000 量级）
2. **相位偏移**：STO 导致的频域线性相位 + OAI 内部处理引入的常数相位
3. **帧间变化**：α 的幅度和相位可能帧间波动（AGC、timing drift 等）

### 4.3 活跃子载波

- GT 有全部 2048 bin 的值
- SRS 在 FIR 插值后也有全部 2048 bin，但**边缘子载波**（guard band）的值是插值外推的，质量差
- 分析时只取**活跃子载波**（通常 ~1248 个，由 SRS bandwidth 配置决定）
- 活跃子载波检测：`active = |H_srs_mean| > threshold`

---

## 5. 潜在问题与注意事项

### 5.1 GT symbol index vs SRS 实际调度 symbol

- GT 默认保存 symbol 12（`--gt-symbols "12"`）
- SRS 实际调度的 symbol 由 gNB MAC 层决定（`gNB_scheduler_srs.c`）
- 如果 gNB 把 SRS 调度到了**不是 symbol 12** 的位置，GT 和 SRS 对应的信道就不是同一时刻的
- **验证方法**：检查 `gnb.log` 中 `SRS configured` 的 symbol 位置

### 5.2 GT 保存频率 vs SRS 采集频率

- GT：每 `save_every` 个 UL slot 保存一次（默认 1 = 每个 UL slot）
- SRS：只在 gNB 调度 SRS 的 slot 才有（周期性，通常每 160 slots 一次）
- GT 远多于 SRS → `align_by_slot()` 对每个 SRS 帧找最近的 GT 帧
- 容差 20 slots = 10 ms，正常情况下 gap 应该 ≤ 3 slots

### 5.3 信道时变性对对齐的影响

- **静态信道**（attach-stable 阶段或 speed=0）：GT 帧间完全相同，对齐容差无所谓
- **动态信道**（v7 动态模式）：GT 帧间不同，必须精确对齐到同一 slot
  - 如果 GT `save_every > 1`，可能找不到与 SRS 完全同 slot 的 GT
  - 建议动态模式下 `--gt-save-every 1`

### 5.4 int16 量化路径

Proxy 输出到 gNB 的 IQ 经过了 int16 量化：
```python
# _gpu_compute_core 末尾：
self._buf_iq_out_3d[:, :, 0] = cp.clip(cp.around(self._tmp_out_2d_final.real), -32768, 32767)
self._buf_iq_out_3d[:, :, 1] = cp.clip(cp.around(self._tmp_out_2d_final.imag), -32768, 32767)
self.gpu_iq_out[:] = self._buf_iq_out_3d.ravel().astype(cp.int16)
```

但 GT 保存的是**量化前的 complex64 H(f)**，不是量化后的 IQ。所以 GT 不含量化噪声，而 SRS 的输入信号含有量化噪声。这是 NMSE 的一个固有下限来源。

### 5.5 UL superposition 的 skip_quant 路径

v7 的 UL 路径使用 `skip_quant=True`：
```python
self.pipelines_ul[k].process_slot_ipc(..., skip_quant=True)
self._ul_accum += self.pipelines_ul[k].gpu_out  # complex128，未量化
```

最终量化发生在 `_ul_accum` 求和后的 fused clip+cast kernel：
```python
_fused_clip_cast_kernel((blocks,), (threads,), (accum_f64, self._ul_fused_out, n_elem))
```

所以**多 UE 叠加是在 complex128 精度下完成的**，只在最后写入 gNB buffer 时才量化为 int16。这保证了 UL 信号质量。

---

## 6. 总结：当前方案的正确性

| 检查项 | 结论 |
|--------|------|
| GT 和实际应用的 H(f) 是否一致？ | ✅ 是同一个对象 |
| CP 长度顺序是否正确？ | ✅ `[CP2, CP1×6, CP2, CP1×6]` = NR 标准 |
| GT symbol 是否对应 SRS symbol？ | ✅ 默认都是 symbol 12 |
| 时间戳刻度是否一致？ | ✅ 都是 NR 绝对 slot 编号 |
| SFN 回绕是否处理？ | ✅ `unwrap_srs_abs_slots()` |
| Bypass 帧是否过滤？ | ✅ `bypass_flags` + 加载时 skip |
| 随机种子是否可控？ | ✅ `--seed` 参数 |
| SNR 门限是否会丢帧？ | ✅ 设为 -999，不丢 |

**当前 G1C v7.py 的 GT/SRS 提取方式是正确的。** 两者的物理对应关系清晰：GT = 真实信道 H(f)，SRS = gNB 的 LS 估计 H_est(f)，差异来自估计器噪声 + 系统性偏差（STO、FIR 插值误差、量化）。分析脚本通过 Method E pipeline（STO 校正 + LS α 对齐 + outlier rejection）正确处理了这些差异。
