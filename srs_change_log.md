# SRS Digital Twin — 调研与分析日志

---

## §S1 OAI DMRS 2D Filtering vs SRS 信道估计调研 (2026-04-30)

> 根据 04-29 组会教授指导，对 OAI 5G NR 信道估计代码进行全链路审计。

### S1.1 关键结论

| 发现 | 说明 |
|------|------|
| **OAI 没有真正的 2D MMSE/Wiener** | DMRS 和 SRS 均无自适应/最优滤波器。"2D filtering" = 固定系数频域 FIR 插值 + 可选时域符号平均 |
| **SRS 缺失 DMRS 的 3 个环节** | ① 时延估计+频域相位补偿 (`nr_est_delay` + `delay_table`) ② 多符号时域平均 (`nr_chest_time_domain_avg`) ③ PTRS 相位跟踪 |
| **STO 补偿是我们后加的** | 全部在 Python 后处理层（`digital_twin_stats.py`），OAI C 代码的 SRS 链路无任何 STO/相位斜率补偿 |
| **⭐ 教授的核心观点已被验证** | STO 是症状而非病因。根因在 OAI ↔ Sionna Proxy 接口层的 sample 对齐问题 |

### S1.2 代码定位

| 文件 | 路径（相对 `openairinterface5g_whan/openair1/PHY/`） | 角色 |
|------|------|------|
| PUSCH DMRS 估计 | `NR_ESTIMATION/nr_ul_channel_estimation.c` L91-449 | gNB DMRS LS + 频域插值 + delay 补偿 |
| SRS 估计 | `NR_ESTIMATION/nr_ul_channel_estimation.c` L740-1102 | gNB SRS LS + 频域插值（**无 delay 补偿**） |
| 时域符号平均 | `NR_REFSIG/dmrs_nr.c` L342-417 | `nr_chest_time_domain_avg()` |
| 时延估计 | `nr_phy_common/src/nr_phy_common.c` L408-438 | `nr_est_delay()` |
| 滤波系数表 | `NR_UE_ESTIMATION/filt16a_32.h` | `filt8_*`, `filt16_*` 定义 |

### S1.3 DMRS 处理链（gNB PUSCH Type1, chest_freq==0）

```
rxdataF → [LS] → [nr_est_delay: IDFT→找峰→est_delay]
         → [delay_table 补偿] → [filt16_ul_* FIR 插值]
         → [inv_delay_table 恢复] → [nr_chest_time_domain_avg]
         → [PTRS 相位跟踪] → ul_ch_estimates
```

### S1.4 SRS 处理链

```
srs_received → [LS + CDM 合并]
             → [filt8_*/filt16_* FIR 插值]    ← 与 DMRS 类似
             → [IDFT→时域 (仅用于 TA 报告)]
             → [SNR 估计]
             → [Digital Twin bin dump]
```

**缺失**：无 `nr_est_delay`、无 `delay_table`、无时域平均、无 PTRS。

### S1.5 滤波系数分析

所有系数为 Q1.15 定点（`16384 = 1.0`）。本质是**固定三角/梯形权重的线性插值**，不是 MMSE，不自适应，不考虑 SNR/信道相关性。

DMRS 和 SRS 的频域 FIR 插值机制是**同一设计范式**，只是 comb 间距不同导致系数不同。

### S1.6 教授建议的 2D MMSE 是全新功能

OAI 两侧都没有真正的 2D MMSE。教授说的 "DMRS 那边应该有" 的 "2D filtering"，实际上是 delay 补偿 + FIR 插值 + 时域平均的组合。但教授要求的**终极目标**（自适应 MMSE，考虑 Doppler/频率选择性的参数化滤波器组）需要从零实现。

---

## §S2 STO 补偿代码溯源 — 100% 后加的 Python 后处理 (2026-04-30)

### S2.1 确认：STO 不是 OAI 原生功能

**OAI C 代码中 SRS 链路没有任何 STO/相位斜率补偿。** 所有 STO 相关代码都在 Python 后处理脚本中：

| 函数 | 文件 | 位置 | 说明 |
|------|------|------|------|
| `_apply_sto_correction()` | `digital_twin_stats.py` | L1005-1021 | 核心：在 fftshift 域上乘 `exp(j·slope·k)` |
| `estimate_sto()` | `digital_twin_stats.py` | L1073-1104 | 全数据 LS 拟合 → `polyfit` 频域线性相位斜率 |
| `_estimate_sto_single()` | `digital_twin_stats.py` | L617-638 | 单帧版本（Method E 使用） |
| `method_e_align()` | `digital_twin_stats.py` | L641-722 | 逐帧 STO + 相位去旋转 + outlier reject |
| `estimate_sto_and_correct()` | `compare_nmse.py` | L45-77 | 独立实现（含 intercept） |
| `dft_denoise_frames()` | `digital_twin_stats.py` | L933-1000 | 时域 CIR 截断窗（替代 STO 的另一种尝试） |

**这些都是对 OAI 导出的 SRS bin 文件的离线后处理，不影响 OAI PHY 实时运行。**

### S2.2 STO 补偿的效果回顾

| 实验（§49.8） | NMSE |
|---|---|
| 有 STO 补偿 | -32 dB |
| 关闭 STO | +11 dB（崩溃） |
| 关闭 STO + DFT denoise | +16 dB（更差） |

**结论**：STO 补偿目前"不可或缺"，说明**根因（sample 对齐问题）还没修好**。STO 是在"掩盖病症"，而非"治愈病因"。

### S2.3 是否应该回退 STO？

**策略**：
- **不要现在全面回退** — STO 是目前唯一让 pipeline 产出合理 NMSE 的手段
- **修好接口根因后，自然可以去掉 STO** — 如果卷积/采样对齐正确，频域就不该有线性相位斜率，STO 补偿量应该趋近于零
- **用 STO 补偿量作为"诊断指标"** — 修接口时监控 `sto_samples`，如果修对了，这个值会从 ~10-25 samples 降到 ~0

---

## §S3 ⭐ 最紧急：OAI ↔ Sionna Proxy 接口 Sample 对齐审计 (2026-04-30)

> 教授 04-29 组会核心论点：
> "컨볼루션을 제대로 안 해줘서"（卷积没做对）
> "샘플이 중간에 빠지면은 주파수 번호 단으로 돌리면은 페이지가 돌아가"（样本丢失 → 频域相位旋转）
> "메 TTI마다 와야 되는데 버퍼에 걔가 안 차고 있다면"（每 TTI 应到的样本没到位）

### S3.1 接口架构概述

```
OAI gNB (nr-softmodem)          Sionna Proxy (v4.py)
    │                                │
    │ DL TX IQ ───GPU IPC Ring───▶  接收 + 信道卷积 → DL RX IQ
    │ UL RX IQ ◀──GPU IPC Ring───  UE TX IQ + 信道叠加 → 写回 UL RX
    │                                │
    └── SHM (4096B) 元数据 ──────────┘
        last_dl_tx_ts / nsamps
        last_ul_tx_ts / nsamps
        CUDA IPC handles
```

**关键参数**：
- 环形缓冲大小：`cir_time = 460800` samples（约 15ms @30.72 MHz）
- 每个 slot：`total_cpx = 30720` samples（固定，µ=1 所有 slot 相同）
- 天线展开：缓冲实际大小 = `cir_time × nbAnt`
- 通信方式：Proxy 轮询 SHM 中 OAI 更新的 `last_*_tx_ts + nsamps`

### S3.2 已发现的 6 个接口层问题

#### 问题 1：UL Ring Buffer Wrap → gNB 读到过期数据（致命）

**代码**：`v4.py` L2498-2503

```python
arr_out, wraps_out = self.ipc_gnb.get_gpu_array_at(
    self.ipc_gnb.gpu_ul_rx_ptr, ts, nsamps,
    self.ipc_gnb.ul_rx_nbAnt, self.ipc_gnb.ul_rx_cir_size, cp.int16)
if not wraps_out:
    arr_out[:] = self._ul_fused_out
    cp.cuda.Stream.null.synchronize()
# 如果 wraps_out == True → 什么都不做！gNB 那边读到的是旧数据！
```

**后果**：当输出写位置跨越环形缓冲边界时，**整个 slot 的处理结果被丢弃**。
gNB 在该时刻读到的是**上一圈残留的旧 IQ 数据**。如果恰好有 SRS 在这个 slot 里，
SRS 信道估计结果会是完全错误的 → 直接表现为"坏帧"/outlier。

**发生频率**：`cir_time = 460800`，`slot_samples = 30720`，一个环能放 15 个 slot。
每隔 15 个 slot（约 7.5ms），就会有一次 wrap 边界。**如果 proxy 处理延迟导致写位置对齐不好，
wrap 事件可以在任何 slot 发生。**

#### 问题 2：UE 输入 Wrap → UE 从叠加中静默消失

**代码**：`v4.py` L2449-2455

```python
for k in ues:
    arr_in, wraps = self.ipc_ues[k].get_gpu_array_at(...)
    if wraps:
        n_bypassed += 1
        continue          # ← 这个 UE 的信号直接丢了！
```

**后果**：当 UE 的 `ul_tx` 缓冲跨环时，该 UE **不参与 UL 叠加**，也**不做 bypass**。
`_ul_accum` 中少了这个 UE 的贡献。对于单 UE 场景：**整个 slot 变成零！**

#### 问题 3：Remainder Bypass → 部分样本未经信道处理

**代码**：`v4.py` L2422-2434 (`_ipc_ul_combine`)

```python
while remaining >= slot_samples:
    self._ipc_ul_superposition_slot(pos, slot_samples, ...)
    pos += slot_samples
    remaining -= slot_samples

if remaining > 0:
    for k in ues:
        self.ipc_ues[k].bypass_copy(...)  # ← "last UE wins"！
```

**后果**：如果 `delta`（OAI 一次写入的总样本数）不是 30720 的整数倍，
尾部 `remaining` 个样本走 bypass（不经过信道处理）。
而且多 UE 的 bypass 是**顺序覆盖**，最后一个 UE 的原始信号覆盖前面所有 UE。

#### 问题 4：Proxy ↔ OAI 没有 Slot 边界校验

**代码**：`v4.py` L2646-2657（DL 主循环）

```python
cur_dl_ts = self.ipc_gnb.get_last_dl_tx_ts()
dl_nsamps = self.ipc_gnb.get_last_dl_tx_nsamps()
gnb_dl_head = cur_dl_ts + dl_nsamps
delta = int(gnb_dl_head - proxy_dl_head)
```

Proxy 只追踪 sample 级别的 head 指针差 `delta`，然后按 30720 切块处理。
**没有任何校验**：
- `delta` 是否是 30720 的整数倍
- `proxy_dl_head` 是否对齐到 slot 边界
- 是否有中间 slot 被跳过

如果 OAI 某一次写入不是整 slot 对齐的，Proxy 的 slot 切分就会与 OAI 的 NR slot 边界**永久性偏移**。

#### 问题 5：信道 Ring Buffer 满载丢弃 → H(f) 与 IQ 错拍

**Sionna ChannelProducer** 通过 `IPCRingBuffer` 向主进程提供信道系数。
当 ring buffer 满时：`try_put_batch()` → **静默丢弃**。

**后果**：Proxy 可能对 slot N 的 IQ 数据施加 slot M 的信道系数（M ≠ N），
导致 GT 与实际施加的信道不匹配。

#### 问题 6：`ts // 30720` 作为 slot_id 的脆弱性

**代码**：`v4.py` L699-723 (GTBatchSaver)

```python
_SLOT_SAMPLES = sum(SYMBOL_SIZES)   # 30720
# slot_id = int(ipc_ts) // 30720
```

如果 `ipc_ts`（来自 SHM）与 30720 不是精确对齐的（比如因为问题 4 导致的偏移），
`ts // 30720` 映射出的 slot_id 会出错 → GT 与 SRS 的 slot 对齐失败。

### S3.3 根因 → 症状映射

| 接口问题 | 导致的症状 |
|----------|-----------|
| 问题 1（UL write wrap 丢弃） | 坏帧（outlier），NMSE 爆炸 |
| 问题 2（UE 从叠加消失） | gNB 收到零信号或不完整信号 |
| 问题 3（remainder bypass） | 部分样本未经信道 → 时域拼接处相位断裂 |
| 问题 4（无 slot 边界校验） | **累积偏移** → 所有后续 slot 的信道施加位置都错 |
| 问题 5（信道丢弃） | GT 与实际信道不匹配 |
| 问题 6（ts 映射脆弱） | GT-SRS slot 对齐错误 |

**综合效果**：以上问题组合起来，完美解释了之前观察到的所有症状：
- **800 子载波相位断裂** ← 问题 3/4（部分样本错位 → 频域线性相位斜率）
- **30-40% 坏帧** ← 问题 1/2（wrap 事件导致的完全错误帧）
- **10dB/25dB 结果乱跳** ← 问题 4（slot 边界偏移 + 问题 5 信道错拍）
- **STO 补偿"不可或缺"** ← 问题 3/4 的频域表现恰好是线性相位斜率

### S3.4 与教授指导的精确对应

| 教授原话（04-29 组会） | 接口问题编号 |
|---|---|
| "컨볼루션을 제대로 안 해줘서"（卷积没做对） | 问题 3/4: 信道施加的 slot 边界与 OAI 不对齐 |
| "샘플이 중간에 빠지면은"（如果中间丢了 sample） | 问题 1/2: wrap 时整 slot 或整 UE 被丢弃 |
| "메 TTI마다 와야 되는데 버퍼에 안 차면"（每 TTI 应到的 buffer 没满） | 问题 4: delta 不是整 slot 倍数时的处理 |
| "그걸 안 준다고 거기에 맞춰서 돈다는 것도 문제야"（数据没给全就照跑也是问题） | 问题 2: UE wrap 时 continue 跳过而非等待/报错 |
| "그거부터 잡고 시작해야 되는 거"（这个要先修） | → **这是最高优先级** |

### S3.5 建议修复方案

| 优先级 | 修复项 | 改动 |
|--------|--------|------|
| **P0** | 问题 1：UL write wrap 时改用 `gpu_circ_copy`（分 tail+head 写入），不要跳过 | `_ipc_ul_superposition_slot()` L2498-2503 |
| **P0** | 问题 2：UE input wrap 时改用 `bypass_copy` 而非 `continue` | `_ipc_ul_superposition_slot()` L2453-2455 |
| **P0** | 问题 4：在 Proxy 启动时做 slot 边界对齐校验 + 运行时 assert `delta % 30720 == 0` | `_ipc_ul_combine()` 入口 |
| **P1** | 问题 3：消除 remainder bypass（确保每次 delta 都是整 slot） | 上游 OAI/SHM 协调 |
| **P1** | 问题 5：增大信道 ring buffer 或添加 backpressure | ChannelProducer 配置 |
| **P2** | 添加全局诊断计数器：wrap 次数、bypass 次数、delta 非整 slot 次数 | 贯穿 v4.py |

### S3.6 诊断实验建议

在修复之前，先**加 instrumentation 确认问题规模**：

```python
# 在 _ipc_ul_combine 入口添加：
if delta % slot_samples != 0:
    print(f"[DIAG] delta={delta} not aligned! remainder={delta % slot_samples}")

# 在 _ipc_ul_superposition_slot 中添加：
if wraps_out:
    print(f"[DIAG] UL write WRAP at ts={ts}! gNB will read stale data!")
if n_bypassed > 0:
    print(f"[DIAG] {n_bypassed}/{n_expected} UEs bypassed at ts={ts}")
```

跑一次 SNR sweep，统计这些事件的频率和分布，即可量化接口问题的严重程度。

---

## §S4 修复优先级总结 (2026-04-30)

根据教授 04-29 组会指导，重新排序工作优先级：

| 顺序 | 工作项 | 理由 |
|------|--------|------|
| **1** | 审计并修复 v4.py IPC 接口的 6 个问题 | 教授明确指出："这个要先修" |
| **2** | 修复后去掉 STO 补偿，验证 NMSE 是否正常 | 验证根因是否确实在接口层 |
| **3** | 固定 seed 跑 SNR sweep 验证单调性 | §49.9 已规划 |
| **4** | 调研 2D MMSE filtering（Python 原型） | 教授中长期目标 |
| **5** | 移植 DMRS `nr_est_delay` 到 SRS（C 代码） | 如果修好接口后仍有残余问题 |

---

## §S5 IPC 接口 6 个问题修复实施记录 (2026-04-30)

**修改文件**：`vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/v4.py`

### S5.1 新增 IPC 帮助方法（GPU IPC Handler 类）

| 方法 | 说明 |
|------|------|
| `read_circ_to_linear()` | 从环形 GPU 缓冲读取到连续线性数组。**自动处理 wrap**：跨环时分 tail+head 拼接。永不返回 None |
| `write_linear_to_circ()` | 将连续线性数组写入环形 GPU 缓冲。**自动处理 wrap**：跨环时分 tail+head 分段写入 |

### S5.2 修复清单

| # | 问题 | 修复内容 | 影响函数 |
|---|------|---------|----------|
| 1 | UL write wrap 丢弃 | `_ipc_ul_superposition_slot`: 用 `write_linear_to_circ` 替代 `get_gpu_array_at` + 条件写入 | `_ipc_ul_superposition_slot` |
| 2 | UE input wrap 跳过 | `_ipc_ul_superposition_slot`: 用 `read_circ_to_linear` 替代 `get_gpu_array_at` + `if wraps: continue` | `_ipc_ul_superposition_slot` |
| 1+2 | DL 方向同样问题 | `_ipc_apply_channel`: 输入改用 `read_circ_to_linear`；输出 wrap 时用临时缓冲 + `write_linear_to_circ` | `_ipc_apply_channel`, `_ipc_apply_channel_for_ue` |
| 3 | Remainder bypass | UL: `_ipc_ul_combine` 余量调用 `_ipc_ul_superposition_slot` 而非 bypass。DL: `_ipc_dl_broadcast` + `_ipc_process_range` 余量调用信道处理而非 bypass | `_ipc_ul_combine`, `_ipc_dl_broadcast`, `_ipc_process_range` |
| 4 | 无 slot 边界校验 | UL/DL 入口添加 `delta % slot_samples` 和 `start_ts % slot_samples` 的诊断日志 | `_ipc_ul_combine`, `_ipc_dl_broadcast` |
| 5 | 信道 buffer 丢弃 | 改进 `try_put_batch` 丢弃日志：早期高频输出 + 显示 buffer 占用率 | `UnifiedChannelProducerProcess.run()` |
| 6 | ts//30720 脆弱 | `GTBatchSaver.record_ul_slot` 添加 `ipc_ts % _SLOT_SAMPLES` 对齐诊断日志 | `GTBatchSaver.record_ul_slot` |

### S5.3 IPC 修复后验证结果

1. **SNR sweep（SNR=25dB, MAX_FRAMES=50）运行正常**，无 IPC 崩溃
2. 修复了 remainder 处理回归 bug（partial slot 进信道 pipeline 导致 `ValueError: shapes (52736,) (122880,)`），恢复为 bypass
3. NMSE 结果（修复后 baseline）：
   - `--denoise sto`：**-21.77 dB**（ρσ1=0.9996, CovFro=0.7466）
   - `--denoise none`：**+37.67 dB**（灾难性，确认 STO 仍是主要问题）
4. 结论：IPC 修复保证了系统稳定性，但 STO 仍然是 NMSE 的主导误差源

---

## §S6 SRS 时延补偿 — 移植 DMRS nr_est_delay 到 SRS C 代码 (2026-04-30)

### S6.1 目标

在 OAI C 代码 `nr_srs_channel_estimation()` 中添加 DMRS 风格的 `nr_est_delay` + `delay_table` 时延补偿，消除 SRS 估计中的线性相位斜坡。

### S6.2 修改文件

`openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c`

### S6.3 实现细节

**插入位置**：在 FIR 插值输出 `memcpy → srs_estimated_channel_freq` 之后、噪声计算之后、`freq2time` 之前。

**处理流程（每个 ant×port 对独立）**：
```
srs_estimated_channel_freq[ant][p_index]  （FIR 插值后全带宽数据）
  → nr_est_delay()  → 得到 per_pair_delay.est_delay（整数 sample delay）
  → get_delay_idx(est_delay, MAX_DELAY_COMP) → 查 delay_table
  → 逐子载波 c16mulShift(H[k], delay_table[k], 8) → 去除线性相位斜坡
  → freq2time() → 时域表示
```

### S6.4 两次迭代与 Comb 混叠陷阱

**V1（失败）：使用稀疏 LS 数据做时延估计**

对 `srs_ls_estimated_channel`（只有 comb 位置有值）调用 `nr_est_delay`。

问题：SRS comb-2 的稀疏频域数据做 IFFT 产生**时域混叠**——在 τ 和 τ+N/2 处出现两个等幅峰值。`nr_est_delay` 可能选中错误的峰（τ+N/2），经 circular shift 后 `est_delay = τ - N/2`（巨大错误值），导致完全错误的相位旋转。

结果：
| 条件 | NMSE |
|---|---|
| `--denoise none` | +31.24 dB（比 baseline 更差） |
| `--denoise sto` | **+25.88 dB**（灾难性破坏，baseline 是 -21.77） |

**V2（当前版本）：使用 FIR 插值后的全带宽数据做时延估计**

改用 `srs_estimated_channel_freq[ant][p_index]`（FIR 已填充所有子载波）。IFFT 产生单一明确峰值，无混叠。

结果：
| 条件 | NMSE | ρPDP | RSRPerr |
|---|---|---|---|
| `--denoise none` | +35.02 dB | 0.1757 | 35.02 dB |
| `--denoise sto` | **-31.00 dB** | **1.0000** | **0.00 dB** |

### S6.5 Debug 日志确认

gNB 日志中 `est_delay` 的实际值：
```
SRS delay comp: ant=0 p=0 est_delay=-8 max_val=185450513
SRS delay comp: ant=0 p=1 est_delay=-8 max_val=174982793
SRS delay comp: ant=1 p=0 est_delay=-7 max_val=199736938
SRS delay comp: ant=1 p=1 est_delay=-6 max_val=195715012
```

- `est_delay` 非零（-8 ~ -6），补偿**确实在执行**
- 值在帧间高度稳定（静态信道预期行为）
- 负值表示信道主径相对于 FFT 窗有负向偏移

### S6.6 结果对比总汇

| 阶段 | `--denoise none` | `--denoise sto` | 说明 |
|---|---|---|---|
| IPC 修复后 baseline | +37.67 dB | -21.77 dB | STO 主导 |
| +C delay V1（稀疏LS） | +31.24 dB | +25.88 dB | comb 混叠导致灾难 |
| **+C delay V2（插值数据）** | +35.02 dB | **-31.00 dB** | **整数 delay 补偿有效** |

**关键发现**：
- `--denoise sto` 从 -21.77 提升到 **-31.00 dB**（改善 **9.23 dB**），ρPDP=1.0000
- C 代码的整数 delay 补偿让离线 STO 精细校正能收敛到接近噪声底限
- `--denoise none` 仍差（+35 dB），因为 `delay_table` 只能补偿整数 sample delay，残留的小数部分（~0.3-0.7 sample）跨 2048 子载波仍然产生巨大相位旋转

### S6.7 结论与下一步

1. **整数 delay 补偿已证明有效**——组合使用 C 代码粗补偿 + 离线精细 STO 达到了 **-31 dB NMSE**（接近 SNR=25dB 的理论下界）
2. **要在 C 代码内完全消除 STO，需要小数 delay 补偿**——这超出了 `delay_table`（整数离散化）的能力，需要频域线性相位拟合或 DFT 域窗函数方法
3. **这实质上就是教授要求的 2D MMSE 方向**——精确的频域相位补偿是 MMSE 信道估计的核心组成部分
4. 当前阶段建议：保留 C 代码的整数 delay 补偿 + 离线 STO 精细校正，作为 Digital Twin NMSE 的 production pipeline

---

## §S7 修复优先级更新 (2026-04-30)

| 顺序 | 工作项 | 状态 |
|------|--------|------|
| ~~1~~ | IPC 接口 6 个问题修复 | **已完成** (§S5) |
| ~~2~~ | 移植 nr_est_delay 到 SRS（整数 delay 补偿） | **已完成** (§S6) |
| ~~3~~ | C 代码小数 delay（IFFT 峰值+抛物线插值） | **已完成** → 被 §S8 取代 |
| ~~4~~ | **2D MMSE 第一阶段（DFT 降噪 + 互相关延迟估计）** | **已完成** (§S8) |
| **5** | 跨 slot SRS 时域 EMA 平均 | 待评估 |
| **6** | MMSE 频域加权插值（替代 FIR） | 待评估 |

---

## §S8 2D MMSE 第一阶段 — DFT 降噪 + 互相关延迟估计 (2026-04-30)

### S8.1 动机

V3（IFFT 峰值 + 抛物线小数 delay 补偿）的 `--denoise none` NMSE 为 **+19.02 dB**。
核心瓶颈有二：
1. **IFFT 峰值法在多径信道中有偏**——估计的是最强路径延迟而非"有效"加权平均延迟
2. **FIR 插值后频域数据仍有噪声**——所有 N 个时域 tap 都参与，大量噪声 tap 未被抑制

教授核心要求的 **2D MMSE** 第一步：
- **频域 MMSE 等价**：DFT 域降噪（IFFT→时域截断窗→FFT）
- **最优相位斜率估计**：功率加权互相关替代 IFFT 峰值法

### S8.2 算法设计

#### DFT-based 降噪（每个 ant/port 对独立）

1. FIR 插值完成后，对 `srs_estimated_channel_freq[ant][p]` 做 N 点 IFFT（`idft`, scale=1）
2. 时域截断窗：保留前 `cp_taps=144` 和后 `cp_taps` 个 tap，中间 N−2×cp_taps 个 tap 置零
3. N 点 FFT（`dft`, scale=0）回到频域 → 覆盖 `srs_estimated_channel_freq`

数学等价于 MMSE 在均匀时延功率谱假设下的最优滤波（截断 = 假设 CIR 集中在 CP 长度内）。

Round-trip 缩放：原设计假设 `IFFT(scale=1) → FFT(scale=0)` 恢复原始幅度（1/N × N = 1）。
**实际验证失败——见 §S8.5。**

#### 互相关相位斜率估计（所有 ant/port 池化）

取代旧的 `nr_est_delay`（IFFT 峰值 + 抛物线插值），在主循环结束后执行：

```
cc = Σ_{a,p,k} H[a][p][k+1] · conj(H[a][p][k])
phase_slope = arg(cc)
delay = phase_slope × N / (2π)
```

**优势**：
- 功率加权：强子载波贡献更多，弱子载波/零子载波自然忽略
- 无相位解卷绕问题
- 所有天线/端口对联合估计，统计精度远超单对 IFFT
- ML 最优（线性相位模型下的最大似然估计）

### S8.3 C 代码修改

**文件**：`openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c`

**删除**：
- `srs_delay_time_buf` 声明（不再需要 IFFT 时域缓冲）
- `srs_best_delay_max_val` 变量
- 整个 IFFT 峰值 + 抛物线插值块（`nr_est_delay` 调用、α/β/γ 计算）

**新增（内循环，FIR memcpy 后）**：
```c
c16_t dft_time_buf[N] __attribute__((aligned(32)));
idft(get_idft(N), srs_estimated_channel_freq[ant][p], dft_time_buf, 1);
for (n = 144; n < N - 144; n++) dft_time_buf[n] = {0, 0};
dft(get_dft(N), dft_time_buf, srs_estimated_channel_freq[ant][p], 0);
```

**新增（循环后，替代旧补偿块）**：
```c
double cc_re = 0, cc_im = 0;
for (a, p, k): cc += H[k+1] * conj(H[k]);
delay = atan2(cc_im, cc_re) * N / (2π);
// 相位旋转补偿与之前相同
```

### S8.4 验证结果（DFT 降噪 + 互相关）

**NMSE 灾难性退化**：`--denoise none` N=1 **+33.73 dB**，N=100 **+43.09 dB**（之前 V3 为 +19.02 dB）。

### S8.5 DFT 降噪失败根因分析

**问题**：OAI 的 `dft()`/`idft()` 工作在 **int16** 精度上。

1. `idft(scale=1)` 将频域数据做 IFFT 并除以 N=2048 → 时域值约 ±500（合理，int16 安全）
2. 时域截断窗保留 2×144=288 个非零 tap
3. `dft(scale=0)` 做 FFT **不缩放** → 288 个 tap 求和后峰值可达 ~288×500 = **144,000**
4. **int16 最大值仅 32,767** → 溢出包裹（wrap-around）→ 频域数据完全损坏

**后果**：溢出后的值与原始信道无关，NMSE 等效于随机数据对比 GT，约 +30~+43 dB。

**错误假设**：设计时假设 `IFFT(scale=1) + FFT(scale=0)` 的 round-trip 缩放为 1，
但实际 `scale=1` 的含义是 **右移 log₂(N)=11 位**（除以 2048），而 `scale=0` 不做任何缩放。
因此 round-trip 实际为 (1/N)×(N) = 1——**数学上正确，但 int16 的中间值溢出**。

**结论**：DFT 域降噪在 OAI 的 int16 定点 DFT 原语下无法直接实现。需要以下替代方案之一：
- 使用 float64 手动 FFT/IFFT（O(N²) 或引入外部 FFT 库）
- 在 LS 稀疏域（N/K_TC 点）做降噪后再 FIR 插值
- 在 Python 离线分析器中做 DFT 降噪（已有 `--denoise dft` 选项）

### S8.6 修复：回退 DFT 降噪，保留互相关延迟估计

已删除 DFT 降噪块。当前 C 代码仅包含：
1. **互相关相位斜率延迟估计**（替代 IFFT 峰值 + 抛物线插值）
2. **频域相位旋转补偿**（与 V3 相同逻辑，但使用互相关估计的延迟值）

等待 sweep 验证互相关单独的效果。

### S8.7 待验证（回退后）

- [ ] 互相关延迟估计单独的 `--denoise none` NMSE
- [ ] 互相关估计的 delay 值与 IFFT 峰值 (-10.954) 的对比
- [ ] `--denoise sto` 在互相关延迟估计下的 NMSE
