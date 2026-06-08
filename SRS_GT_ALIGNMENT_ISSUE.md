# SRS ↔ GT 对齐问题记录

> 创建日期：2026-05-07  
> 状态：**未解决，持续跟踪**  
> 优先级：**最高**（阻塞 1D/2D MMSE 的公平评估）

---

## 1. 问题描述

在 Digital Twin 验证系统中，SRS（gNB 信道估计）和 GT（Sionna Proxy 真实信道）的对齐存在不确定性，导致 NMSE 比较结果中混入了"对齐噪声"，无法区分：
- 估计器本身的误差（我们想测量的）
- 对齐错误导致的虚假误差（我们想消除的）

这个问题在准静态信道下被掩盖（所有帧的 H(f) 几乎相同，对齐错了也看不出来），但在动态信道 + 严格 Signature Gate 下暴露。

---

## 2. 对齐涉及的两个维度

### 2.1 时间维度（slot 对齐）

**当前状态**：
- GT 每 10 个 UL slot 保存一次（`GT_SAVE_EVERY=10`）
- SRS 由 gNB 调度器周期性触发（通常每 40-160 slots）
- 分析脚本用 `align_by_slot(tol_slots=20)` 做最近邻匹配

**问题**：
- TDD 配置 `7DL + 1Special + 2UL`：每 10 slots 有 2 个 UL slot
- GT 实际保存间隔 = 每 50 NR slots（= 25 ms）
- 如果 SRS 落在 GT 没保存的 slot 上，最近邻匹配的 GT 可能差 1-25 个 slot
- 动态信道下，即使差 1 个 slot，H(f) 已经变化 → NMSE 虚高

**量化**：
- UE 速度 3 m/s，载频 3.5 GHz → Doppler ≈ 35 Hz → 相干时间 ≈ 28 ms
- 1 slot = 0.5 ms，25 slots = 12.5 ms ≈ 相干时间的 45%
- 在相干时间 45% 的间隔内，信道相关性已显著下降

### 2.2 频率维度（OFDM symbol 对齐）

**当前状态**：
- GT 保存 symbol 12（`GT_SYMBOLS="12"`）
- SRS 在 symbol 12 接收（`startPosition=1` → `l₀ = 14-1-1 = 12`）

**确认链路**：
```
nr_radio_config.c L923:
  srs_res->resourceMapping.startPosition = 1;        ← 硬编码

gNB_scheduler_srs.c L431:
  srs_pdu->time_start_position = 14 - 1 - 1 = 12;   ← 计算

srs_rx.c L89:
  const uint8_t l0 = srs_pdu->time_start_position;   ← 使用
  symbol_offset = (n_symbols + l0) * ofdm_symbol_size;
```

**风险**：
- 当前配置下 symbol 12 是正确的
- 但 `startPosition` 是可配置的（0~5 → symbol 13~8）
- 未来做 2D MMSE 需要 `nrofSymbols=n2/n4`，SRS 会占多个 symbol
- SRS bin 文件**不记录** `time_start_position`，分析脚本无法自动验证

---

## 3. 当前系统的具体配置

| 参数 | 值 | 来源 |
|------|-----|------|
| GT 保存间隔 | 每 10 UL slot | `launch_all_v7.sh: GT_SAVE_EVERY=10` |
| GT 保存 symbol | 12 | `launch_all_v7.sh: GT_SYMBOLS="12"` |
| GT slot_id 计算 | `ipc_ts // 30720` | `v7.py: GTBatchSaver.record_ul_slot()` |
| SRS symbol 位置 | 12 | `nr_radio_config.c: startPosition=1` |
| SRS slot_id 计算 | `frame_id * 20 + slot_id` (unwrapped) | `digital_twin_stats.py: unwrap_srs_abs_slots()` |
| 对齐容差 | ±20 slots | `digital_twin_stats.py: align_by_slot(tol_slots=20)` |
| SRS 周期 | ~40-160 slots（由调度器决定） | `gNB_scheduler_srs.c` |
| TDD 配置 | 7DL + 1Sp + 2UL | `gnb.sa.band78.fr1.106PRB.usrpb210.conf` |

---

## 4. 已确认不是问题的方面

| 检查项 | 状态 | 证据 |
|--------|------|------|
| CP 长度顺序 | ✅ 正确 | G1C v7.py: `[CP2, CP1×6, CP2, CP1×6]` |
| GT 和实际应用的 H(f) 一致性 | ✅ 同一对象 | `_ipc_ul_superposition_slot` 中同一个 `channels_ul` |
| SFN 回绕处理 | ✅ 已修复 | `unwrap_srs_abs_slots()` |
| Bypass 帧过滤 | ✅ 已实现 | `bypass_flags` + 加载时 skip |
| SNR 门限丢帧 | ✅ 已修复 | `SRS_TWIN_SNR_THRESHOLD = -999` |
| 随机种子控制 | ✅ 已实现 | `--seed` 参数 |

---

## 5. 解决方案路线图

### Phase 1：消除时间维度不确定性（最高优先）

**方案 A：GT 每个 UL slot 都保存**
```bash
GT_SAVE_EVERY=1   # 改 launch_all_v7.sh
```
- 优点：零代码改动，立即生效，保证同 slot 匹配
- 代价：磁盘写入 ×10（每 100 帧 ~6 MB → 每秒 ~12 MB）
- 适用：短时间实验（< 30 分钟）

**方案 B：收紧对齐容差**
```python
align_by_slot(srs_abs, gt_slots, tol_slots=1)  # 从 20 收紧到 1
```
- 如果配对数大幅下降 → 说明 GT 采样频率不够（需要方案 A）
- 如果配对数不变 → 说明当前 `save_every=10` 已经足够

**方案 C：GT 只在 SRS slot 保存（精确匹配）**
- 需要 Proxy 知道 SRS 的调度周期
- 实现复杂，但磁盘开销最小
- 可通过监听 gNB log 中的 SRS 调度信息实现

### Phase 2：消除 symbol 维度不确定性

**方案 D：SRS bin 文件记录 symbol 位置**
```c
// nr_ul_channel_estimation.c, dump 注入点
ctx->meta_symbol[w_idx][ctx->dump_count] = srs_pdu->time_start_position;
```
- bin 格式升级为 V3
- 分析脚本自动选择对应 GT symbol

**方案 E：GT 保存多个 symbol 做交叉验证**
```bash
GT_SYMBOLS="11,12,13"
```
- 分析时对比每个 symbol 与 SRS 的相关性
- 确认 12 是最佳匹配（或发现 off-by-one）

**方案 F：运行时从 gnb.log 提取 SRS 配置**
```bash
grep "time_start_position\|SRS configured" gnb.log
```
- 传给分析脚本 `--srs-symbol <N>`

### Phase 3：为 2D MMSE 做准备

**方案 G：多 symbol GT + 多 symbol SRS**
- SRS 配置改为 `nrofSymbols=n2`（symbol 11+12）
- GT 保存 `--gt-symbols "11,12"`
- bin 文件记录 `num_symbols` 和 `time_start_position`
- 分析脚本支持多 symbol 对齐

**方案 H：slot 间 history buffer**
- 保持 `nrofSymbols=n1`
- GT 保存连续多个 slot 的 symbol 12
- 分析脚本做跨 slot 时域对齐
- 对应 2D MMSE 的"经路 B"（tex 文件中描述）

---

## 6. 诊断工具

### 6.1 快速检查 SRS symbol 位置
```bash
grep -i "time_start_position\|SRS.*symbol\|SRS configured" logs/latest/gnb.log | head -5
```

### 6.2 检查 GT-SRS slot gap 分布
```python
from digital_twin_stats import load_srs_v2, load_gt, align_by_slot
H_srs, meta = load_srs_v2("logs/latest")
H_gt, gt_slots = load_gt("logs/latest/sionna_gt", return_slot_ids=True)
srs_idx, gt_idx, median_gap = align_by_slot(meta["abs_slots"], gt_slots, tol_slots=20)
print(f"Paired: {len(srs_idx)}/{len(meta['abs_slots'])}")
print(f"Median gap: {median_gap} slots")
print(f"Max gap: {max(abs(meta['abs_slots'][srs_idx] - gt_slots[gt_idx]))} slots")
```

### 6.3 GT 保存多 symbol 后的交叉验证
```python
# 假设 GT 保存了 symbol 11,12,13
for sym in [11, 12, 13]:
    H_gt_sym = load_gt("logs/latest/sionna_gt", srs_symbol=sym, return_slot_ids=True)
    # 计算与 SRS 的相关性
    corr = np.mean(np.abs(np.sum(H_srs * np.conj(H_gt_sym), axis=-1)))
    print(f"Symbol {sym}: correlation = {corr:.6f}")
```

---

## 7. 与 1D/2D MMSE 工作的关系

| MMSE 阶段 | 对齐要求 | 当前状态 |
|-----------|---------|---------|
| 1D MMSE 评估 | 同 slot 同 symbol 的 GT | ⚠️ `save_every=10` 可能不够 |
| 1D vs Legacy 公平对比 | 两者用完全相同的 GT 帧 | ✅ 同一 bin 文件 |
| 2D MMSE（多 symbol） | 多 symbol GT + symbol 位置元数据 | ❌ 未实现 |
| 2D MMSE（跨 slot） | 连续 slot 的 GT | ❌ 需要 `save_every=1` |

**结论**：无论走哪条 2D MMSE 路径，都需要先解决 Phase 1（时间对齐）。建议立即将 `GT_SAVE_EVERY` 改为 1，作为所有后续工作的基础。

---

## 8. 深层问题分析：为什么 `save_every=1` 不一定能解决对齐？

### 8.1 表面理解（不完整）

"GT 每 10 个 UL slot 保存一次，SRS 可能落在中间没保存的 slot 上，所以改成每个 UL slot 都保存就能精确匹配。"

这个理解**只对了一半**。

### 8.2 更深层的问题

即使 `save_every=1`，对齐仍然依赖以下**三个前提条件全部成立**：

#### 前提 1：GT slot_id 和 SRS slot_id 在同一刻度上

- GT 的 slot_id = `ipc_ts // 30720`（Proxy 侧 IPC 采样时间戳）
- SRS 的 slot_id = `frame_id * 20 + slot_id`（gNB 侧 NR SFN）
- §49 BUG 1 修复后，两者**理论上**在同一刻度
- **但**：如果 Proxy 的 `ipc_ts` 起始值与 gNB 的 SFN 起始值有固定偏移，所有 GT slot_id 都会偏移一个常数

**验证方法**：对比 GT 的 slot_id 范围和 SRS 的 abs_slot 范围，看是否有系统性偏移。

#### 前提 2：GT 保存的 H(f) 对应的物理时刻 = SRS 接收的物理时刻

信号链路有延迟：
```
Proxy 生成 H(f) 并保存 GT [时刻 T]
    ↓ 用 H(f) 卷积 UE IQ
    ↓ 写入 gNB UL ring buffer
    ↓ gNB 从 ring buffer 读取（可能有 1-2 slot 延迟）
    ↓ gNB 做 FFT + SRS 估计
    ↓ 记录 frame_id/slot_id [时刻 T + Δ]
```

如果 gNB 读取 ring buffer 有延迟（比如 IPC polling 周期），SRS 记录的 slot_id 可能比 GT 的 slot_id **晚 1-2 个 slot**。

**验证方法**：检查 `align_by_slot` 的 median gap。如果稳定为 +1 或 +2（而非 0），说明有固定管道延迟。

#### 前提 3：SRS 发生在 GT 保存的那个 UL slot 中

- GT 在每个 UL slot 的 `_ipc_ul_superposition_slot()` 中保存
- SRS 只在 gNB 调度了 SRS 的那个 slot 才产生
- 如果 SRS 的调度 slot 恰好是 UL slot → GT 有对应条目 ✅
- 如果 SRS 被调度到 Special slot 的 UL 部分 → GT 可能没有（取决于 Proxy 是否处理了那个 slot）

**验证方法**：检查 SRS 的 slot_id 是否总是落在 TDD 配置的 UL slot 位置（slot 8 或 9）。

### 8.3 当前数据的实际诊断结果（2026-05-07 测量）

数据来源：`logs/latest`（= `mmse_sigma2_abcd_seed8201_20260506_115206/B_try2/snr_10dB`）

```
GT Analysis:
  Files: 53, Total frames: 5274
  slot_id range: [94, 52950]
  Gap: mean=10.0, median=10, min=10, max=11 (97.6% 是精确的 10)

SRS Analysis:
  Files: 1, Total frames: 79
  slot_id unique values: [8]  ← SRS 全部在 slot 8！
  abs_slot range: [10728, 46408]
  SRS period: mean=457.4, median=160, min=160, max=2240

Alignment (nearest-neighbor):
  Paired (tol=20): 79/79 (100.0%)  ← 全部配对成功
  Paired (tol=5):  79/79 (100.0%)
  Paired (tol=1):  17/79 (21.5%)   ← 只有 21% 精确匹配

  Gap distribution:
    gap=0:  6 frames ( 7.6%)
    gap=1: 11 frames (13.9%)
    gap=2: 15 frames (19.0%)
    gap=3: 29 frames (36.7%)  ← 最多
    gap=4: 14 frames (17.7%)
    gap=5:  4 frames ( 5.1%)
  
  Mean gap: 2.58 slots, Median gap: 3 slots, Max gap: 5 slots
```

### 8.4 诊断结论

**关键发现**：

1. **SRS 全部在 slot 8**（TDD 配置的第一个 UL slot）。这与 §47 的发现一致。

2. **GT 每 10 个 UL slot 保存一次**，GT 的 slot_id 是 `..., 94, 104, 114, ...`（间隔 10）。

3. **Median gap = 3 slots**。这意味着 SRS@slot_X 被匹配到 GT@slot_(X±3)。

4. **在静态信道下 gap=3 无影响**（H(f) 不变）。**在动态信道下 gap=3 = 1.5 ms 的信道变化**。

5. **配对率 100%（tol=20）**，说明 slot_id 刻度没有系统性偏移，两者确实在同一时间范围内。

6. **如果改成 `save_every=1`**：GT 的 slot_id 会变成 `..., 94, 95, 96, 97, 98, ...`，SRS@slot_X 就能精确匹配到 gap=0 的 GT。

### 8.5 最终判断

**`save_every=10` 在动态信道下确实是问题**：
- 当前 median gap = 3 slots = 1.5 ms
- UE 3 m/s + 3.5 GHz → Doppler 35 Hz → 相干时间 ~28 ms
- 1.5 ms / 28 ms ≈ 5% 相干时间 → 信道相关性 ~0.97
- 这会在 NMSE 中引入 ~0.03 的底噪（约 -15 dB）

**对 1D MMSE 评估的影响**：
- 如果 MMSE 改善量 < 3 dB，gap=3 的对齐噪声可能掩盖真实改善
- 如果 MMSE 改善量 > 5 dB，gap=3 影响可忽略

**结论：改 `save_every=1` 是正确的做法**，可以将 gap 从 3 降到 0，消除这层不确定性。

### 8.4 结论

`save_every=1` 是**必要条件但不充分条件**。它消除了"GT 采样率不够"这一层不确定性，但不能解决：
- slot_id 刻度偏移（需要验证）
- IPC 管道延迟（需要测量）
- SRS 调度 slot 与 UL slot 的对应关系（需要确认）

**正确的诊断顺序**：
1. 先用现有数据（`save_every=10`）跑 `align_by_slot`，看 median gap
2. 如果 median gap > 5 → 说明采样率确实不够，改 `save_every=1`
3. 如果 median gap = 0~3 → 说明采样率够用，问题在别处（刻度偏移/管道延迟）
4. 如果配对率很低（< 50%）→ 说明 slot_id 刻度有系统性问题

---

## 9. 行动项（修正后）

### 诊断优先（先搞清楚问题在哪）

- [x] **立即**：用诊断工具 6.2 检查现有数据的 slot gap 分布和配对率 — 已复核 §10.1，与 §8.3 完全一致
- [x] **立即**：对比 GT slot_id 范围 vs SRS abs_slot 范围，检查是否有系统性偏移 — 无系统偏移，overlap 完整覆盖（§10.2）
- [x] **立即**：确认 SRS 的 slot_id 是否总是落在 TDD UL slot 位置（slot 8 或 9）— **100% 落在 slot 8**（§10.2）

### 根据诊断结果决定

- [ ] 如果 median gap > 5：将 `GT_SAVE_EVERY` 改为 1 — 实测 median=3，**未触发该条件**，但仍建议改 `=1` 以排除采样率层面影响（§10.4）
- [ ] 如果有固定偏移 N：在 `align_by_slot` 中加入 `offset` 参数补偿 — **gap 分布是单峰众数=3，疑似 +3 IPC 管道延迟**，需先做 `save_every=1` 复测才能确诊（§10.3）
- [ ] 如果配对率低：排查 slot_id 刻度计算逻辑 — 配对率 100%（tol=20 / tol=5 都满），刻度逻辑无误

### 确认 symbol 对齐

- [ ] **短期**：实现方案 E（GT 保存多 symbol "11,12,13"），交叉验证确认 12 正确
- [ ] **短期**：从 gnb.log 提取 `time_start_position` 确认运行时值

### 长期基础设施

- [ ] **中期**：SRS bin V3 格式，记录 symbol 位置 + 估计器模式
- [ ] **长期**：为 2D MMSE 准备多 symbol / 跨 slot GT 基础设施

---

## 10. 复核诊断（2026-05-07 17:30）

> 数据来源：`/home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/logs/latest`
> （= `mmse_sigma2_abcd_seed8201_20260506_115206/B_try2/snr_10dB`，与 §8.3 同一份数据）
> 复核脚本：在 `G1C_MultiUE_MIMO_Channel_Proxy/` 目录下用 `digital_twin_stats` 直接调 `load_srs_v2 / load_gt / align_by_slot`
> 目的：用同一份数据独立复核 §8.3 的数字是否准确，并补充 §8 中未量化的几个细节

### 10.1 §8.3 数字逐项复核（完全吻合）

| 指标 | §8.3 声称 | §10 实测 | 是否吻合 |
|------|----------|---------|---------|
| SRS frames | 79 | **79** | ✅ |
| SRS unique slot_id | [8] | **[8]** | ✅ |
| SRS abs_slot range | [10728, 46408] | **[10728, 46408]** | ✅ |
| SRS period median | 160 | **160** | ✅ |
| SRS period mean | 457.4 | **457.4** | ✅ |
| SRS period max | 2240 | **2240** | ✅ |
| GT frames | 5274 | **5274** | ✅ |
| GT abs_slot range | [94, 52950] | **[94, 52950]** | ✅ |
| GT gap median | 10 | **10** | ✅ |
| 配对率 (tol=20) | 100% | **100% (79/79)** | ✅ |
| 配对率 (tol=5) | 100% | **100% (79/79)** | ✅ |
| 配对率 (tol=1) | 21.5% | **21.5% (17/79)** | ✅ |
| Median gap | 3 | **3** | ✅ |
| Max gap | 5 | **5** | ✅ |
| Gap=0 | 6 (7.6%) | **6 (7.6%)** | ✅ |
| Gap=1 | 11 (13.9%) | **11 (13.9%)** | ✅ |
| Gap=2 | 15 (19.0%) | **15 (19.0%)** | ✅ |
| Gap=3 | 29 (36.7%) | **29 (36.7%)** | ✅ |
| Gap=4 | 14 (17.7%) | **14 (17.7%)** | ✅ |
| Gap=5 | 4 (5.1%) | **4 (5.1%)** | ✅ |

**结论**：§8.3 的所有数字都是真的，问题是真实的，不是误读。

### 10.2 §8 中未明确量化、本次新发现的两点

#### (1) SRS slot_id 100% 落在 TDD UL slot

```
SRS slot_id mod 10 = [8]   (期望: 8 或 9)
```

- §8.2 前提 3"SRS 调度 slot 与 UL slot 的对应关系"在当前数据上 **100% 满足**
- 也就是说 §47 已确认的"SRS 全在 slot 8"在 v7 + 当前 TDD 配置下依然成立
- 这条**不是问题来源**，可以从待排查列表里划掉

#### (2) tol=3 时配对率 = 77.2%（介于 tol=5 和 tol=1 之间）

| tol | paired/total | rate | median_gap | max_gap |
|----:|-------------:|-----:|-----------:|--------:|
| 20  | 79/79  | 100.0% | 3 | 5 |
|  5  | 79/79  | 100.0% | 3 | 5 |
|  3  | 61/79  |  77.2% | 2 | 3 |
|  1  | 17/79  |  21.5% | 1 | 1 |
|  0  |  6/79  |   7.6% | 0 | 0 |

- 即使把 tol 从 20 收紧到 5，**一帧也没丢**，说明 max gap 真的就是 5 而不是更大
- tol=3 时还能保住 77% 的帧，但 median gap 退化到 2
- tol=1 时 79% 的帧被丢弃，统计性会崩 → **目前不能直接收紧 tol，需要先解决采样率/延迟问题**

#### (3) SRS 采样数据量本身偏少

- 79 帧分布在 35680 slots 跨度（≈ 17.84 秒）
- 收敛分析做 N→∞ 曲线时，N 上限受这 79 帧约束
- 这是另一个独立的问题（**采样数量** ≠ **对齐质量**），但建议在 sweep 时同时关注

### 10.3 Gap 分布的关键观察 —— 不是均匀错位，是单峰偏移

如果 gap 是因为采样率不够导致的纯随机错位，理论上 gap 在 [0, save_every/2] 内应该接近**均匀分布**。但实测：

```
gap=3 一项就占 36.7%（众数）
分布形状：6 → 11 → 15 → 29 → 14 → 4   （单峰）
```

这看起来像是 **+3 slot 左右的固定偏移叠加 ±2 抖动**，而**不是**均匀的采样率错位。

**最可能的物理来源**：IPC 管道延迟

```
Proxy 在 slot T 生成 H(f) 并保存 GT     [GT 时间戳 = T]
   ↓ 用 H(f) 卷积 UE IQ
   ↓ 写入 gNB UL ring buffer
   ↓ gNB 在 slot T+Δ 从 ring buffer 读取（Δ ≈ 3 slot）
   ↓ gNB 做 FFT + SRS 估计
   ↓ 记录 frame_id/slot_id   [SRS 时间戳 = T+Δ]
```

`align_by_slot` 是绝对值最近邻匹配，所以会把 SRS@(T+3) 误配到 GT@T 之外的某个邻居，得到 gap=3 这个众数。

### 10.4 对每个前提的最终判断（更新 §8.2）

| 前提 | 结论 | 证据 |
|------|------|------|
| **前提 1**：GT/SRS slot_id 同刻度 | ✅ **没问题** | tol=20 和 tol=5 配对率都是 100%；overlap 范围完整覆盖 SRS 的 [10728, 46408] |
| **前提 2**：GT 保存时刻 = SRS 接收时刻 | ⚠️ **疑似 +3 slot 固定偏移**（IPC 管道延迟） | gap 分布单峰众数=3，不是均匀分布 |
| **前提 3**：SRS 落在 GT 保存的 UL slot | ✅ **没问题** | SRS slot_id 100% = 8，是 TDD UL slot |

### 10.5 决策建议（2 步）

#### Step A：消除"采样率"这一层（5 分钟）

```bash
cd /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy
sed -i 's/^GT_SAVE_EVERY=10$/GT_SAVE_EVERY=1/' launch_all_v7.sh
grep -n '^GT_SAVE_EVERY' launch_all_v7.sh   # 确认改了
```

跑一次同样配置的最短 sweep（`MAX_FRAMES=20` 即可），然后**重新跑 §10 的诊断脚本**，看 gap 分布会变成什么样：

| 情况 | gap 分布 | 推断 | 下一步 |
|------|---------|------|--------|
| **A** | 几乎全部 = 0 | 仅采样率问题 | 保留 `save_every=1` 收工 |
| **B** | 几乎全部 = 3（窄峰） | **存在 +3 固定 IPC 延迟** | 在 `align_by_slot` 加 `offset=-3` 参数补偿，或修 v7.py 的 `record_ul_slot` 时间戳定义 |
| **C** | 仍然 0~5 散布 | 采样率 + 抖动两层都有 | 两层都修：`save_every=1` + offset 补偿 |

#### Step B：量化"对齐误差污染了多少 NMSE"（可选）

用现有数据做控制实验：把 SRS 和故意错位 ±k slot 的 GT 配对（k = 0, 1, 2, 3, 5），跑 NMSE 曲线。`NMSE(k) - NMSE(0)` 就是**对齐误差贡献的上界**。

- 若 k=3 时 NMSE 退化 < 0.5 dB → 说明 §8.5 的"-15 dB 底噪"估算偏悲观，对齐误差影响可忽略，1D MMSE 工作可继续推进
- 若 k=3 时 NMSE 退化 > 2 dB → 必须先解决对齐再谈 1D MMSE 的 A/B 公平性

### 10.6 §8.5 的"NMSE 底噪 -15 dB"估算复审

§8.5 写的是：

> 1.5 ms / 28 ms ≈ 5% 相干时间 → 信道相关性 ~0.97 → NMSE 底噪 ~0.03（约 -15 dB）

这个估算偏悲观。在 Jakes 模型下：

```
ρ(τ) = J₀(2π · f_d · τ)    (Jakes / Clarke 经典模型)
f_d = 35 Hz, τ = 1.5 ms
2π · 35 · 0.0015 ≈ 0.330
J₀(0.330) ≈ 0.9728
```

也就是相关性确实约 0.97，**§8.5 这一步是对的**。但 NMSE 底噪不应该直接等于 `1 - ρ`，正确表达式是：

```
NMSE_align ≈ 2(1 - ρ) = 2 × 0.027 = 0.054   →  约 -12.7 dB
```

或者考虑两侧都有信道演化的极端：

```
NMSE_align ≈ 2(1 - ρ²) = 2 × (1 - 0.946) = 0.108   →  约 -9.7 dB
```

所以 §8.5 的"-15 dB"估算其实**比真实情况乐观**，对齐误差**可能比文档说的更严重**（落在 -10 ~ -13 dB 区间）。这进一步支持"必须修对齐"的结论。

### 10.7 复核脚本

下面这段脚本可以复用，作为以后任何 sweep 完成后的标准 sanity check：

```python
import sys, numpy as np
sys.path.insert(0, '/home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy')
from digital_twin_stats import load_srs_v2, load_gt, align_by_slot

run_dir = '/home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/logs/latest'
H_srs, meta = load_srs_v2(run_dir)
H_gt, gt_slots = load_gt(f'{run_dir}/sionna_gt', return_slot_ids=True)
srs_abs = np.asarray(meta['abs_slots'], dtype=np.int64)
srs_slot_ids = np.asarray(meta['slot_ids'], dtype=np.int64)

print(f'SRS  : {len(srs_abs)} frames, unique slot_id={sorted(set(srs_slot_ids.tolist()))}')
print(f'GT   : {len(gt_slots)} frames, gap_median={int(np.median(np.diff(np.sort(gt_slots))))}')
for tol in (20, 5, 3, 1, 0):
    s, g, _ = align_by_slot(srs_abs, gt_slots, tol_slots=tol)
    if s.size:
        gaps = np.abs(srs_abs[s] - gt_slots[g])
        print(f'tol={tol:>2}: paired={s.size}/{srs_abs.size} '
              f'({100*s.size/srs_abs.size:.1f}%)  median={int(np.median(gaps))}  max={int(gaps.max())}')
s, g, _ = align_by_slot(srs_abs, gt_slots, tol_slots=20)
gaps = np.abs(srs_abs[s] - gt_slots[g])
u, c = np.unique(gaps, return_counts=True)
print('gap dist:', dict(zip(u.tolist(), c.tolist())))
```
