# GT↔SRS Alignment Fix Bugfix Design

## Overview

SRS 信道估计的端到端 NMSE 评估存在约 +5 dB 的正地板，淹没了估计器算法 1–3 dB 的真实增益。需求文档已把根因从"单一 IPC 管道延迟 δ"修正为多因素问题，**主因是 SRS dump 的 frame/slot 编号与 GT 的 `slot_id`（= ts // 30720）不同源 / 不同参考系**，叠加未补偿的恒定 +4 样本 IPC STO、超相干时间的 GT 保存节奏、以及 `UL_PRE_GAIN=2.0` 的 int16 削顶。

本设计的修复策略遵循三条原则：

1. **分两类落点，优先非侵入侧**：修复分为 (A) **评估管线侧**（`eval_nmse_clean.py` / `eval_nmse_sto.py` / `digital_twin_stats.py` 的对齐逻辑）和 (B) **数据采集侧**（`v8.py::GTBatchSaver` 与 SRS bin dump 之间的编号映射、采集配置）。我们**优先做评估侧 + 采集侧打通编号映射**，**不改动 OAI 实时信号链路**（保护不变行为 3.1–3.4）。
2. **决定性诊断先行**：以 `SRS_ESTIMATOR=passthru`（raw LS）+ STO 对齐作为设计的第一步分诊，切分问题域（GT 参考系问题 vs 估计器侧问题）。
3. **最小改动 + 向后兼容**：评估脚本改动必须保留 `eval_nmse_clean` 的 scalar-only 口径（回归防护 3.5），新对齐逻辑可分离、可开关。

核心思路：把当前 `align_frames` 中"用中位数硬凑一个常数偏移 `C`"的对齐方式，替换为**基于同源映射的物理 slot 对齐**——要么让 GT 与 SRS 各自记录一个可换算到同一参考系的量（采集侧打通），要么在评估侧用一个经诊断验证的确定性映射函数把两套编号归一，并强制启用已有的 STO 网格搜索补偿。

## Glossary

- **Bug_Condition (C)**: 触发缺陷的条件——一对 (GT_frame, SRS_frame) 未对应同一物理信道快照，即编号参考系错位 OR 存在未补偿的恒定 STO。
- **Property (P)**: 期望行为——配对后的 GT/SRS 对应同一物理 slot，且补偿 STO 后静态 NMSE 转负、动态 |alpha| 波动显著下降、gap=0 强相关。
- **Preservation**: 必须保持不变的现有行为——OAI 实时信号处理链路、gNB SRS 估计输出、IPC 时序、非 SRS slot 处理、旧 bin 格式兼容、`eval_nmse_clean` 的 scalar-only 口径。
- **GT slot_id**: `v8.py::GTBatchSaver.try_begin_slot`（约 L1009–1033）中 `slot_id = ts_int // _SLOT_SAMPLES`，`_SLOT_SAMPLES = sum(SYMBOL_SIZES) = 30720`（mu=1）。这是从数据流起点单调递增的**绝对采样 slot**，写进 npz 的 `slot_ids` 字段。`ipc_ts` 不可用时 fallback 用 `self._ul_slot_counter`（又一套不同源编号）。
- **SRS abs_slot**: `eval_nmse_clean.py::load_srs` 从 bin 头逐帧读 `fid`(frame=SFN) / `sid`(slot)，由 gNB 写。`_unwrap_abs_slots` 计算 `abs_slot = frame*20 + slot`，并用 `SFN_WRAP = 20480` 检测 SFN（0..1023）回绕。这是 **SFN 域编号**，与 GT 的 `ts//30720` **没有共同原点**。
- **C_rough**: `align_frames` 中 `median(gt_slots) - median(srs_abs)` 估出的常数偏移，再在 ±2·tol 内扫 delta 找最大配对数——**症结所在**。
- **STO**: Sample Timing Offset，IPC 管道引入的恒定 ≈ +4 样本时延，在 FFT 域表现为逐子载波线性相位斜坡，单标量 alpha 无法消除。
- **passthru**: `SRS_ESTIMATOR=passthru`，输出 raw LS 估计，用于分诊缺陷是否在估计器侧。

## Bug Details

### Bug Condition

缺陷发生在评估脚本按 slot 编号配对 GT 帧与 SRS 帧时。`align_frames`（`eval_nmse_clean.py`，约 L150–190）用 `C_rough = median(gt_slots) - median(srs_abs)` 估一个**常数偏移**，再在 `±2*tol` 窗口内扫 delta 最大化配对数。由于 GT 的 `ts//30720` 绝对采样 slot 与 SRS 的 `frame*20+slot` SFN 域编号**不同源**，这个常数偏移从未保证把一对 GT/SRS 配到同一物理信道快照。叠加未补偿的恒定 +4 样本 IPC STO，单标量 alpha 对齐无法消除逐子载波线性相位错位。

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X = (GT_frame, SRS_frame) 配对，由当前 align_frames 产出
  OUTPUT: boolean

  // 参考系错位：两套编号无共同原点，常数偏移无法保证同一物理快照
  reference_mismatch := (GT_frame.coordinate_system != SRS_frame.coordinate_system)
                        OR NOT mapsToSamePhysicalSlot(GT_frame.slot_id,
                                                      SRS_frame.abs_slot)

  // STO 未补偿：FFT 域存在未消除的线性相位斜坡
  sto_uncompensated := (estimatedSTO(GT_frame, SRS_frame) != 0)

  RETURN reference_mismatch OR sto_uncompensated
END FUNCTION
```

### Examples

- **参考系错位（实锤）**: GT 写 `slot_id = ipc_ts // 30720`（数据流起点单调递增，量级很大且不回绕）；SRS 写 `abs_slot = SFN*20 + slot`（SFN 0..1023 回绕，量级很小）。`align_frames` 只用 `median(gt)-median(srs)` 凑一个常数 C —— 期望：配到同一物理快照；实际：配到任意时间窗的两个独立快照。
- **gap=0 仍零相关**: 用 `gap=0`（同 slot 直接配对）评估 cdl_c 幅度包络相关，仅 +0.08（频域 mavg101 平滑后仍 0.08）。期望：同一物理快照应强相关；实际：零相关 —— 证明不是单纯 δ 延迟。
- **STO 未补偿**: IPC 管道恒定 +4 样本 STO（p10/p90 都 +4），`eval_nmse_clean` 仅用单标量 alpha。期望：补偿后线性相位斜坡 ≈ 0；实际：scalar-only 在 cdl_a 上 +12..+14 dB。
- **静态场景可对齐（边界，期望行为）**: UE_SPEED=0 + UL_PRE_GAIN=1.0 + SRS_ONLY_CHANNEL=1 + cdl_c 下，STO 测得恒定 +4，NMSE 从 scalar-only +14.91 dB → STO 对齐 -6.37 dB（rx1tx1 -9.49 dB）。证明编号在无 Doppler 去相关时能对上、且 STO 补偿必要且正确。

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- **OAI 实时 UL 信号处理链路（3.1）**：Proxy 仍用相同的 H(f) 对 UE 信号做频域卷积，本修复不触碰该路径。
- **gNB SRS 估计核心输出（3.2）**：`srs_estimated_channel_freq` 的 LS + FIR 插值输出与修复前完全相同（位级一致）。
- **IPC ring buffer 时序与吞吐（3.3）**：UL IQ 数据传输时序/吞吐不变，不引入额外延迟。
- **非 SRS slot 处理（3.4）**：PUSCH/PUCCH 等其他 UL 信道处理不受影响。
- **旧 bin 格式与 scalar-only 口径（3.5）**：`eval_nmse_clean` 对旧格式 bin 文件的解析与 scalar-only NMSE 数值保持不变（向后兼容）。

**Scope:**
所有不涉及缺陷条件的输入应完全不受本修复影响，包括：
- OAI 实时信号链路上的所有数据流（仅读 GT/SRS dump 做离线评估，不回写）
- gNB SRS 估计器内部计算（passthru 与各估计器的原始输出）
- IPC 管道的数据搬运与时序
- 旧格式 SRS bin 文件的 scalar-only 评估路径

**Note:** 期望的"正确对齐行为"定义在下方 Correctness Properties（Property 1）；本节聚焦"什么必须不变"。

## Hypothesized Root Cause

基于需求分析与代码定位，最可能的问题按优先级：

1. **编号参考系不同源（主因）**: GT `slot_id = ts//30720`（绝对采样域，`v8.py` L1031）与 SRS `abs_slot = SFN*20+slot`（SFN 域，`eval_nmse_clean.py::_unwrap_abs_slots`）无共同原点。
   - `align_frames` 用 `C_rough = median(gt)-median(srs)` 凑常数偏移（`eval_nmse_clean.py` L168），从未锚定到同一物理时刻。
   - `try_begin_slot` 在 `ipc_ts` 不可用时 fallback 到 `self._ul_slot_counter`（第三套编号），进一步破坏可换算性。
   - 后果：gap=0 配对仍零相关（+0.08）。

2. **恒定 +4 样本 IPC STO 未补偿（叠加因素）**: `eval_nmse_clean` 仅单标量 alpha，FFT 域线性相位斜坡未消除。`eval_nmse_sto.py` 已有逐帧/逐天线 STO 网格搜索（`tau_range=30, step=0.5`），但 clean 口径默认不启用。

3. **GT 保存节奏超相干时间（叠加因素）**: `GT_SAVE_EVERY=100`（= 50 ms）远超信道相干时间（~10–28 ms），动态场景下每次保存的 GT 是独立衰落快照（帧间自相关 ≈ 0），即使编号对上也无法跨 slot 内插对应。

4. **`UL_PRE_GAIN=2.0` 触发 int16 削顶（数据污染）**: ULSCH BLER ~65%、attach 掷硬币，污染 SRS/GT 采集，使对齐诊断在脏数据上不可信。

## Correctness Properties

Property 1: Bug Condition - 统一参考系 + STO 补偿后配对对应同一物理快照

_For any_ 满足缺陷条件的输入（`isBugCondition` 返回 true，即参考系错位或 STO 未补偿），修复后的评估/对应链路 SHALL 在统一同源编号参考系下把 GT/SRS 配到同一物理 slot 并补偿恒定 STO，使得：(a) 静态场景（UE_SPEED=0, UL_PRE_GAIN=1.0, SRS_ONLY_CHANNEL=1, cdl_c）STO 对齐后 NMSE < 0 dB；(b) 动态场景 per-frame |alpha| 的 std/median 相对 scalar-only 基线显著下降、正地板消除；(c) gap=0（同源同 slot）配对的 corr(|GT|, |SRS|) 强相关（≫ 0.08）。

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - 非缺陷输入行为不变

_For any_ 不满足缺陷条件的输入（`isBugCondition` 返回 false，包括 OAI 实时信号链路数据、gNB SRS 估计器原始输出、IPC 时序、非 SRS slot、旧格式 bin 的 scalar-only 评估），修复后的代码 SHALL 产生与修复前完全相同的结果，保持信号处理链路、估计输出、IPC 时序与向后兼容性不变（F(X) = F'(X)）。

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

## Fix Implementation

修复分为**评估侧（A）**与**采集侧（B）**，并以**诊断步骤（D0）**作为前置分诊。所有改动均不触碰 OAI 实时信号链路（C 端 / IPC）。

### D0. 决定性诊断（前置，分诊问题域）

**File**: `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/eval_nmse_sto.py`（复用，不重造）

以 `SRS_ESTIMATOR=passthru`（raw LS）+ STO 对齐在动态场景跑 `eval_nmse_sto.py`：
- 若 raw LS 仍残留约 +5 dB ⇒ **纯 GT 参考系问题**（进入 A1/B 编号映射修复，查 GT 符号/时延/CP 参考系）。
- 若转负 ⇒ 缺陷在估计器侧（超出本 spec 范围，记录后停止）。

此步骤确认或推翻根因 1（参考系）vs 估计器侧，决定后续改动落点。

### A. 评估管线侧

**File**: `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/eval_nmse_clean.py`、`eval_nmse_sto.py`、`digital_twin_stats.py`

**A1. 统一编号参考系（核心，对应根因 1）**:
- 新增一个**确定性映射函数** `unify_reference_frame(gt_slots, srs_abs, mapping)`，把两套编号归一到同一物理 slot 域。映射来源优先级：
  1. 若采集侧（B）已打通——直接用 GT/SRS 各自记录的同源量（见 B），映射为恒等/已知线性换算。
  2. 否则——用经 D0 诊断验证的换算关系（例如把 SRS 的 SFN 域通过已知锚点换算到 `ts//30720` 域），并**断言映射后 gap=0 可达成强相关**，否则报告映射失败而非静默凑数。
- 保留 `align_frames` 的现有签名与 scalar-only 行为，新逻辑通过**新增可选参数**（如 `unified=False` 默认关闭）启用，确保 3.5 向后兼容。

**A2. 强制 STO 补偿（对应根因 2）**:
- 评估默认走 `eval_nmse_sto.py` 的逐帧/逐天线 STO 网格搜索（`tau_range`, `tau_step` 复用现有实现），并同时报告 scalar-only 与 STO 对齐两列（现有 `_print_one` 已支持），不破坏 clean 口径。

**A3. gap=0 同源验证（对应根因 1 验证）**:
- 新增一个验证模式：在统一参考系下做 gap=0 配对，计算 `corr(|GT|, |SRS|)`，作为对齐成功的硬判据（阈值远高于 0.08）。

### B. 数据采集侧（编号映射打通）

**File**: `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/v8.py`（`GTBatchSaver`）+ SRS bin dump 头

确认 GT `slot_id` 与 SRS `fid/sid` 能映射到同一物理 slot 的方案（二选一，倾向最小侵入）：
- **方案 B1（GT 侧记录 SFN 可换算量）**: 在 `try_begin_slot` 写 npz 时，除 `slot_id = ts//30720` 外，额外记录可换算到 SFN 域的量（如对应的 frame/slot 或一个共享锚点 timestamp）。
- **方案 B2（SRS dump 侧记录采样域可换算量）**: 在 SRS bin 头额外记录可换算到 `ts//30720` 的量（如该 SRS 估计对应的 IPC 采样计数）。

两方案均为**新增旁路元数据**，不改实时信号链路、不改 IPC 时序、不改估计输出（保护 3.1–3.4）。读侧对缺少新字段的旧 bin 退回 scalar-only 路径（保护 3.5）。

### 采集配置（对应根因 4）

- 数据采集统一用 `UL_PRE_GAIN=1.0` 避免 int16 削顶（对应需求 2.7）。这是采集运行参数约束，不改源码逻辑。

### 改动落点与不变行为映射

| 改动 | 文件/位置 | 保护的不变行为 |
|------|-----------|----------------|
| A1 统一参考系（可选参数） | `eval_nmse_clean.py::align_frames` | 3.5（默认关闭，scalar-only 口径不变） |
| A2 强制 STO 补偿 | `eval_nmse_sto.py`（复用） | 3.2（只读估计输出，不改） |
| A3 gap=0 验证 | 评估侧新增 | 3.5 |
| B1/B2 旁路元数据 | `v8.py` / SRS bin 头 | 3.1, 3.3, 3.4（不碰信号链路/IPC/非SRS slot） |
| 采集配置 gain=1.0 | 运行参数 | 3.1（不改源码） |

## Testing Strategy

### Validation Approach

两阶段：先在**未修复代码**上用 D0 诊断 + 现有 eval surface 出反例（demonstrate 缺陷），确认/推翻根因；再验证修复后 (a) 缺陷输入产生期望行为（Fix Checking）、(b) 非缺陷输入行为不变（Preservation Checking）。STO 网格搜索复用 `eval_nmse_sto.py`，不重造。

### Exploratory Bug Condition Checking

**Goal**: 在实现修复前 surface 反例以 demonstrate 缺陷，确认或推翻根因分析。若推翻则需重新假设。

**Test Plan**: 在 UNFIXED 代码上运行诊断与对齐评估，观察失败模式。

**Test Cases**:
1. **参考系错位反例**: 在 UNFIXED `align_frames` 上断言 gap=0 配对的 `corr(|GT|,|SRS|)`，观察其 ≈ 0.08 零相关（will fail on unfixed code 对"应强相关"的期望）。
2. **passthru 分诊**: `SRS_ESTIMATOR=passthru` + STO 对齐跑动态场景，观察 raw LS 仍残留约 +5 dB（确认 GT 参考系问题而非估计器侧）。
3. **STO 未补偿反例**: scalar-only 口径在 cdl_a 上观察 +12..+14 dB；STO 网格搜索观察 median STO ≈ +4（demonstrate STO 未补偿）。
4. **静态可对齐边界**: 静态场景跑 `eval_nmse_sto.py`，观察 scalar-only +14.91 dB → STO 对齐 -6.37 dB（may pass —— 证明静态下编号能对上、STO 补偿正确）。

**Expected Counterexamples**:
- gap=0 配对零相关、scalar-only 正地板、median STO ≈ +4。
- Possible causes: 编号参考系不同源、STO 未补偿、GT 超相干时间保存。

### Fix Checking

**Goal**: 验证对所有满足缺陷条件的输入，修复后函数产生期望行为。

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := evaluate_fixed(input)   // 统一参考系 + STO 补偿
  ASSERT (静态 NMSE(result) < 0 dB)
     AND (动态 std(|alpha|)/median(|alpha|) 相对 baseline 显著下降)
     AND (gap=0 corr(|GT|,|SRS|) >> 0.08)
END FOR
```

### Preservation Checking

**Goal**: 验证对所有不满足缺陷条件的输入，修复后函数产生与修复前相同的结果。

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT evaluate_original(input) = evaluate_fixed(input)
END FOR
```

**Testing Approach**: 推荐 property-based testing 做 preservation checking，因为：
- 自动在输入域上生成大量用例
- 捕获手写单测漏掉的边界
- 对"所有非缺陷输入行为不变"提供强保证

**Test Plan**: 先在 UNFIXED 代码上观察 scalar-only 口径、旧 bin 解析、估计器原始输出的行为，再写 property-based 测试固化该行为。

**Test Cases**:
1. **scalar-only 口径保持**: 观察 `eval_nmse_clean` 在旧 bin 上的 scalar-only NMSE，写测试断言修复后（默认关闭 unified 参数时）数值位级一致（3.5）。
2. **旧 bin 向后兼容**: 观察缺少新字段的旧格式 bin 仍可被 `load_srs` 解析并退回 scalar-only 路径，写测试断言行为不变（3.5）。
3. **估计器原始输出不变**: 观察 passthru / 各估计器的 `srs_estimated_channel_freq`，断言本修复不改变其输出（3.2）。

### Unit Tests

- `unify_reference_frame`: 已知映射下，归一后同一物理 slot 的 GT/SRS 索引正确配对、gap=0。
- STO 估计器（复用 `_sto_es`）: 注入已知 tau 的线性相位斜坡，恢复 tau 在 `tau_step` 分辨率内。
- 旧 bin 解析边界: 缺 `slot_ids` / 缺新字段 / SFN 回绕（`_unwrap_abs_slots` 的 `SFN_WRAP`）。

### Property-Based Tests

- **Property 1（Fix）**: 生成随机但可控的 GT/SRS slot 序列 + 已知映射 + 注入已知 STO，断言修复后配对全部命中同一物理 slot（gap=0）且补偿后残留线性相位斜坡 ≈ 0。
- **Property 1（STO 恢复）**: 对任意注入 tau（|tau| ≤ tau_range），STO 网格搜索恢复值与注入值在 step 分辨率内一致。
- **Property 2（Preservation）**: 生成随机旧格式 bin 输入，断言 `align_frames(unified=False)` 与修复前 scalar-only 输出位级一致；断言估计器输出 F(X)=F'(X)。

### Integration Tests

- 静态场景全流程：UE_SPEED=0 + UL_PRE_GAIN=1.0 + SRS_ONLY_CHANNEL=1 + cdl_c，跑统一参考系 + STO 对齐，断言 NMSE < 0 dB（复现 -6.37 dB 量级）。
- 动态场景全流程：统一参考系 + STO 补偿后，断言 per-frame |alpha| std/median 相对 scalar-only baseline 显著下降、正地板消除。
- gap=0 同源验证：统一参考系后断言 `corr(|GT|,|SRS|) >> 0.08`。
- 向后兼容回归：对旧格式 bin 跑 scalar-only，断言与历史数值一致。
