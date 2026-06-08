# SRS -7 dB Floor 根因定位与修复日志

| 字段 | 值 |
|------|----|
| **日期** | 2026-05-14 |
| **耗时** | 10:13 ~ 13:45 (约 3.5 小时) |
| **目标** | 定位并修复 SRS 信道估计 -7 dB NMSE floor 的真正根因 |
| **结论** | **帧间 STO 抖动 (±0.5 样本)** 是根因；5 行 phase slope 补偿消除 floor |
| **前序工作** | 05-09 ~ 05-13 共 5 天诊断（见 SRS_MMSE_FULL_LOG.md / FILT8_OPT_DEBUG_LOG.md） |

---

## 最终结果

| 指标 | 修前 | Fix 1 后 | 改善 |
|------|------|----------|------|
| 帧间 correlation mean | 0.674 | **0.986** | +0.31 |
| 帧间 correlation min | 0.146 | 0.749 | +0.60 |
| 帧平均 SNR | 5.1 dB | **16.5 dB** | **+11.4 dB** |
| NMSE vs 帧平均 p50 | -8.6 dB | **-20.2 dB** | **+11.6 dB** |
| -7 dB floor | 存在 | 消除 | — |

**残留观察**：
- empirical SNR 在 SNR=30/40 被 cap 在 17-21 dB（疑为 OAI int16 pipeline 精度上限，不影响 2D MMSE 工作）
- Fix 1 后 corr min=0.749（tail 偏重），可能来自深衰落 SC 上加权拟合精度不足或偶发大 STO 跳变
- STO 抖动的物理来源（"为什么会抖"）未追到机制层，定位到现象层为止

---

## 诊断过程（按时间线）

### 阶段 1：Proxy AGC 尝试 (10:25 ~ 11:10)

**假说**：UE IFFT 输出 RMS=200 (0.6% of int16)，int16 FFT 量化失真是根因。

**行动**：在 v8.py 中实现自适应 AGC（target_peak=30000），在 int16 量化前放大信号填满动态范围。

**结果**：AGC 导致 OAI FFT 内部溢出（butterfly 加法饱和），NMSE 反而从 -6.5 恶化到 -4.55 dB。SRS 检测频繁失败（"No SRS signal"）。

**结论**：
- target_peak=30000 导致 FFT 内部 butterfly overflow
- AGC 方向错误：即使不溢出，精度损失发生在 UE→Proxy 的 int16 输入端，Proxy 输出端放大无法恢复已丢失的 LSB
- **AGC 代码已全部回退**

---

### 阶段 2：E1 Float FFT 假说 (11:35 ~ 11:46)

**假说**：OAI 的 int16 FFT (dft2048) 在低幅度输入下量化失真严重，用 float FFT (FFTW) 旁路可消除。

**关键对照实验**：用 `oai_dft_wrapper.py`（bit-exact libdfts.so）在 Python 中模拟完整 int16 链路。

```
结果:
  Path A (OAI int16 FFT + int16 LS):  帧间 corr = 0.9964, 有效 SNR = 23.4 dB
  Path B (float FFT + float LS):      帧间 corr = 0.9968, 有效 SNR = 21.9 dB
  Path C (float FFT → int16 → LS):    帧间 corr = 0.9975, 有效 SNR = 24.8 dB
```

**结论**：三条路径 corr 都是 0.996+。**int16 FFT 在仿真中完全没问题。** 退化发生在 Python 仿真未覆盖的环节（UE→Proxy→gNB 的实际 IPC 传输路径）。

**E1 方案取消。** 此对照实验是整个 debug 的转折点——30 分钟省去 1-2 天无效编码。

---

### 阶段 3：STO 诊断 — 初次错误排除 (11:46 ~ 11:55)

**行动**：对已有 SRS bin 数据做相位 slope 估计和 STO 补偿测试。

**错误结论**：STO 补偿后 corr 从 0.72 降到 0.29，"STO 不是根因"。

**错误原因**：补偿算法实现有 bug——去除了每帧各自的 slope 后，没有对齐到共同参考帧。线性相位（STO 的体现）恰好是帧间最一致的成分，错误地去除它反而暴露了 AWGN 随机性。

**教训**：**这是整个 debug 里最危险的一步——差点永久排除正确答案。** 验证实验脚本本身可能有 bug，当假说被"否定"时，先审查实验代码再否定假说。（见 Lesson 6）

---

### 阶段 4：根因候选排查 (11:55 ~ 12:13)

并行调查三个方向：

**候选 1: Proxy UL slot 时序未对齐**
- 发现 157 次 `[IPC DIAG] UL start_ts not slot-aligned (offset=29917)`
- offset = 30720 - 29917 = 803 ≈ N_TA_offset (800 样本)
- 但 offset 帧间变化极小（29916/29917/29918，±1 样本）
- 初步判断：±1 样本太小（基于 per-SC 相位差 0.003 rad），不足以解释 corr=0.54
- **此判断后被证伪**——per-SC 差异虽小，但跨 1200 SC 带宽的累积相位差约 1.2π，足以严重降低宽带 correlation。见阶段 6 和 Lesson 4。

**候选 2: GT symbol index vs SRS symbol 不匹配**
- GT 保存 symbol 12，SRS 也用 symbol 12 → **排除**

**候选 3: OAI TA / FFT 窗动态变化**
- N_TA_offset=800 固定，不随帧变化
- UE 无 TA command（静态 channel rfsim）→ **排除**

---

### 阶段 5：UE int16 量化假说 (12:13 ~ 12:58)

**假说**：UE IFFT 输出 RMS=200，int16 量化后每帧的量化 pattern 不同。

**经审视后反驳**：
- 量化噪声 SQNR = 200² / (1/12) = 57 dB
- 实测退化只有 ~12 dB (|α|=0.93 → SNR≈12 dB)
- 量化噪声比实测退化低 45 dB，不可能是根因

**结论**：AMP 修改预测无效。量化噪声与实际退化机制正交。

---

### 阶段 6：SNR 标定实验 (13:04 ~ 13:24)

**实验**：4 个 SNR 点 (10/20/30/40 dB)，静态 channel，每点 100 帧 SRS。

**按 Δδ=0 分组（帧间 STO 差 < 0.25 样本）的关键结果**：

| SNR 设定 | corr (Δδ=0 组) | corr (all pairs) | Δδ=0 组帧对数 |
|----------|----------------|------------------|---------------|
| 10 dB | 0.9853 | 0.74 | 182 |
| 20 dB | 0.9949 | 0.78 | 223 |
| 30 dB | 0.9972 | 0.82 | 246 |
| 40 dB | **0.9999** | 0.75 | 200 |

**此实验证明**：同 STO 的帧对之间，唯一不一致性来源是 AWGN（随 SNR 线性消失到 0.9999）。**不存在任何其他系统性退化机制。**

剩余的跨 STO 帧退化（corr 0.74-0.82）由阶段 7 验证是否能通过 STO 补偿消除。

---

### 阶段 7：Fix 1 验证 — 根因确认 (13:38 ~ 13:45)

**修复方法**：per-frame weighted phase slope compensation

```python
for i in range(N_frames):
    phase = np.unwrap(np.angle(H[i, active_sc]))
    w = np.abs(H[i, active_sc])**2
    w /= np.sum(w)
    k_c = k_indices - np.sum(w * k_indices)
    slope = np.sum(w * k_c * (phase - np.sum(w * phase))) / np.sum(w * k_c**2)
    H[i, active_sc] *= np.exp(-1j * slope * k_indices)
```

**验证结果 (SNR=20 dB, 100 frames)**：

| 指标 | 修前 | Fix 1 后 | 改善 |
|------|------|----------|------|
| 帧间 corr mean | 0.674 | 0.986 | +0.31 |
| 帧平均 SNR | 5.1 dB | 16.5 dB | +11.4 dB |
| NMSE vs avg p50 | -8.6 dB | -20.2 dB | +11.6 dB |

**阶段 6 + 阶段 7 共同闭合因果链**：
- 阶段 6 证明：同 STO 帧无其他退化（corr→1.0 at high SNR）
- 阶段 7 证明：STO 补偿后全部帧 corr 恢复到 0.986
- 两者共同 → **帧间 STO 抖动是 -7 dB floor 的全部根因**

---

## 根因完整因果链

```
OAI/Proxy 时钟域接合（rfsimulator GPU IPC 路径）
  → UL start_ts 在帧间有 ±1 整样本抖动 (29916/29917/29918)
    → Proxy 信道应用 / OAI gNB FFT 窗起点相对信号有亚样本偏移
      → SRS 信道估计 H(f) 包含帧间变化的线性相位斜率
        → H_obs[k] = H_true[k] × exp(-j2π k δ/N), δ 帧间变化
          → 跨 ~1200 SC 带宽累积相位差约 1.2π (per Δδ=1 sample)
            → 帧间 correlation 降到 0.5-0.8
              → NMSE vs 任何参考 ≈ -7 dB (floor)
```

**注**：根因定位到"±1 样本 timing 抖动"现象层。具体机制（为什么 start_ts 会帧间抖动 ±1）未深入追查，留作 Fix 2 的未来工作。可能来源：rfsimulator 时间戳量化、GPU IPC futex 唤醒延迟、OAI 调度器 slot 边界计算的舍入。

---

## 排除假说表

| 假说 | 证据 | 状态 |
|------|------|------|
| int16 FFT 精度不足 | Python bit-exact 仿真 corr=0.996 | **排除** |
| UE int16 量化 (RMS=200) | SQNR=57dB, 比实测退化低 45 dB | **排除** |
| Proxy AGC 可修复 | 实验恶化 2 dB (FFT overflow) | **排除** |
| filt8 插值误差 | passthru ≈ legacy (差 0.15 dB) | **排除（05-12 确认）** |
| GT-SRS symbol 不匹配 | 两者都用 symbol 12 | **排除** |
| OAI TA 动态调整 FFT 窗 | N_TA_offset 固定，无 TA command | **排除** |
| **帧间 STO 抖动 (±0.5 样本)** | **Δδ=0 组 corr=0.9999@40dB; Fix 1 后 corr=0.986@20dB** | **确认** |

---

## 教训

1. **"对照实验先行"比"假说驱动开发"高效 10 倍**
   - Python int16 FFT 对照实验（30 分钟）直接排除了 E1，省去 1-2 天
   - SNR 标定实验（15 分钟）直接锁定 STO，省去 AMP 修改方向
   - 回看前 5 天：如果第一天就做"同 STO 帧 vs 不同 STO 帧"的分组对比，可能 1 小时就锁定根因

2. **sinc 模型 / 物理直觉要用对参数**
   - 首次 STO 分析直觉"±1 样本太小"是基于 per-SC 0.003 rad，没考虑宽带累积
   - 正确度量：1 样本 STO 在 1200 SC 跨度上累积 2π×1200/2048 ≈ 3.7 rad ≈ 1.2π
   - 按 Δδ 分组实测 |α| 直接绕过了模型依赖——实验 > 计算

3. **"量化噪声 SQNR" ≠ "系统有效 SNR"**
   - SQNR=57 dB 但有效 SNR=5 dB 的差距曾让人误以为量化是瓶颈
   - 实际退化机制（STO）与量化完全正交——两者互相遮蔽了对方的存在

4. **±1 样本看似微小，但宽带累积效应巨大**
   - 1 样本 STO 在 2048 点 FFT + 1200 SC 跨度下累积约 1.2π 相位差
   - 窄带分段 (300 SC) corr=0.97，但宽带 corr=0.54——宽带信号对 STO 极其敏感
   - 这解释了"分段看一切正常，整体看一塌糊涂"的矛盾

5. **分组对比是最有力的诊断工具**
   - 按 Δδ=0/1/2 分组直接给出因果关系，5 行 numpy
   - 比回归分析、相位拟合、频域滤波等间接方法可靠得多
   - 前提条件：你需要知道"按什么变量分组"——这一步需要对系统有足够理解

6. **验证实验脚本本身可能有 bug——当假说被"否定"时，先审查实验代码**
   - 阶段 3 的 STO 补偿脚本有实现错误（没对齐到参考帧），导致正确假说被错误排除
   - 这是整个 debug 最危险的一步——如果没有后续的 SNR 标定实验"碰巧"重新揭示 STO，可能永远找不到根因
   - 规则：当实验结果"违反直觉"（STO 补偿不应该让 corr 变差），优先怀疑脚本而非物理

---

## 遗留问题（明确不修，附决策依据）

### 遗留 1：二级 ceiling（empirical SNR 被 cap 在 ~20 dB）

**现象**：
- SNR=30 dB 设定 → Fix 1 后 empirical SNR = 17.2 dB（损失 12.8 dB）
- SNR=40 dB 设定 → Fix 1 后 empirical SNR = 9.7 dB（损失 30.3 dB，但此数据 STO std 异常大）
- NMSE vs 帧平均在 SNR=30/40 dB 收敛到 -21~-22 dB，不再随 SNR 提升

**可能来源**：OAI int16 pipeline 精度上限（LS 乘法的 int32>>9→int16 截断、filt8 乘加的饱和运算），或 Python 仿真中 libdfts.so 给出的 23.4 dB 就是该 pipeline 的理论上限。

**05-14 后续实验澄清（重要）**：

2D MMSE Phase 1 实验中发现：之前误判的 -31 dB "ceiling" 实为 **split-set 帧数限制的假象**，不是 int16 硬 ceiling。

- 50 帧 eval 窗口 → 平均增益 10·log₁₀(50) ≈ 17 dB
- single-frame ~-14 dB + 17 dB ≈ -31 dB ← 恰好对上之前看到的"ceiling"
- 验证：SNR=20 cumulative oracle 在 90 帧时达 **-50.38 dB**，远低于 -31 dB
- SNR=20 理论 100 帧 oracle = -36.8 dB

**修正后的判断**：
- per-frame 层面的 ~17 dB cap 确实存在（int16 量化限制）
- 但**多帧平均没有硬 ceiling**，oracle NMSE 持续随帧数下降
- 之前 EMA 和 Oracle "撞墙对齐" 是两者都撞 eval 窗口限制的假象
- e-MIMO 管线中的统计聚合（PDP/协方差）理论上可无限积累精度，遗留 1 的实际影响比原先判断的**更小**

**为什么不修**：
- e-MIMO 孪生管线走的是 H → PDP/协方差 → State Sample → AI 特征路线
- PDP 是 IFFT 后的统计量，协方差是多帧平均——两者天然对 per-frame 量化噪声有 N 倍压制
- 21 dB per-frame SNR 对应 N=10 帧平均后 31 dB、N=100 帧后 41 dB，完全满足特征提取需求
- 只有 V-RAN 完整解调路线（per-frame BLER 敏感）才需要突破此 ceiling
- 教授明确当前走 e-MIMO 孪生路线，不走 V-RAN
- ⬆ 后续实验进一步确认：多帧平均可穿透 per-frame ceiling，e-MIMO 路线完全不受影响

**何时重开**：路线切换到 V-RAN（需完整解码），或高 SNR 场景下 AI 特征异常时。

---

### 遗留 2：STO 抖动的物理机制未定位

**现象**：
- UL start_ts 在帧间有 ±1 整样本抖动（29916/29917/29918，std≈0.5 样本）
- 这是通过 phase slope 估计 + SNR 标定实验确认的 -7 dB floor 全部根因
- Fix 1 (per-frame STO 补偿) 已将其对下游的影响完全消除

**根因定位深度**：到"什么在抖"为止，未追到"为什么在抖"。

**可能机制**（未验证）：
- rfsimulator GPU IPC 的 futex 唤醒延迟导致 timestamp 量化到不同整数
- OAI 调度器 slot 边界计算的舍入（`get_samples_slot_timestamp` 累加路径）
- Proxy `proxy_ul_head_combined` 初始化/追赶逻辑中的边界条件

**05-14 后续实验新现象**：

2D MMSE Phase 1 数据分析中发现 STO 存在**偶发大幅跳变**，比常规 ±1 样本抖动大一个数量级：

- SNR=40 最后 4 帧 (96-99) STO 从 ~4 跳到 **~20 样本**
- 根因为采集时 timing sync 异常，非 int16 问题
- 处理方式：`test_2d_mmse.py` 自动检测并剔除 STO > 6 样本的帧
- 去除后 SNR=40 经验 SNR = 17.3 dB，与 SNR=30 (17.2 dB) 一致

**影响评估**：
- 常规 ±1 样本抖动已被 Fix 1 (phase slope 补偿) 完全消除
- 偶发大幅跳变 (~20 样本) 超出 Fix 1 的设计范围，需要在数据预处理层检测并剔除
- 当前 STO > 6 样本阈值过滤策略有效，但若此类事件频率升高，需要重新评估

**为什么不修**：
- Fix 1 已在信号处理层完全消除了常规 STO 抖动对下游的影响（corr 0.54→0.986）
- 偶发大幅跳变通过阈值过滤处理，不影响下游数据质量
- 2D MMSE / PDP / 协方差提取的输入是 Fix 1 补偿后的 H，不受 STO 影响
- rfsim 仿真中的 STO 机制和真实硬件 timing 行为无关（一个是 IPC 时序，一个是 RF 链路 propagation + AGC + ADC timing），追查仿真里的机制不产生可迁移知识
- 修复需要深入 OAI rfsimulator / GPU IPC 底层，工作量大（预估 2-5 天），收益为零

**何时重开**：Fix 1 在某些场景失效（如动态 channel 下 STO 估计不可靠），或 STO 大幅跳变频率显著升高（>5% 帧），或 STO 抖动幅度增大到影响 UE attach 稳定性。

---

### 遗留问题与 e-MIMO 管线的关系

```
教授的管线:
  SRS → 信道估计 → [Fix 1: STO补偿] → 2D滤波 → PDP/协方差 → State Sample → AI

遗留 1 (21dB ceiling):
  影响位置: "信道估计"的 per-frame 精度
  被吸收处: "2D滤波"(多帧平均) + "PDP/协方差"(统计聚合)
  → 不传递到 State Sample

遗留 2 (STO 机制):
  影响位置: "SRS → 信道估计"之间
  被消除处: [Fix 1: STO补偿]
  → 不传递到 2D滤波及之后
```

**结论**：两个遗留都不在 e-MIMO 孪生管线的关键路径上。明确归类为"路线决定不需要解决"，记录在案，继续主线工作。

---

## 后续方向

| 方向 | 优先级 | 说明 |
|------|--------|------|
| **2D MMSE 算法开发** | **高** | 教授要求的研究方向，用 STO 补偿后的 H 作为输入 |
| 动态 channel 验证 | 中 | 确认 Fix 1 在 speed>0 下仍有效（STO 估计是否受 Doppler 影响） |
| STO 补偿 C 化 | 低 | 等算法定型后再移植到 OAI |
| 遗留 1: 二级 ceiling | 不修 | 见上文决策依据 |
| 遗留 2: STO 机制 | 不修 | 见上文决策依据 |

---

## 如何复现本次诊断

### 关键脚本

| 文件 | 用途 |
|------|------|
| `verify_float_fft_gain.py` | E1 对照实验：int16 vs float FFT 帧间一致性比较 |
| `oai_dft_wrapper.py` | bit-exact OAI int16 DFT Python 封装 |
| `digital_twin_stats.py` | SRS bin 数据加载器 (`load_srs_v2`) |
| `run_q4_snr_sweep_v8.sh` | SNR 扫描 runner |

### 关键数据集

| 数据 | 路径 | 说明 |
|------|------|------|
| SNR 标定 4 点 | `logs/q4_sweep_20260514_130516/snr_{10,20,30,40}dB/` | 每点 100 帧 SRS + GT |
| Doppler 修复后 baseline | `logs/q4_sweep_20260513_000323/snr_20dB/` | 之前的 static channel 数据 |

### 复现命令

```bash
# 1. 跑 SNR 扫描（需要 OAI + Proxy 环境）
sudo UE_SPEED=0 CHANNEL_SEED=42 SRS_ESTIMATOR=legacy GT_SAVE_EVERY=10 \
     SNR_POINTS="10 20 30 40" MAX_FRAMES=300 ATTACH_STABLE_SEC=300 \
     bash run_q4_snr_sweep_v8.sh

# 2. 分析（纯 Python，无需 OAI）
python3 -c "
import numpy as np
from digital_twin_stats import load_srs_v2
srs, meta = load_srs_v2('path/to/snr_20dB')
# ... 分组分析和 Fix 1 代码见阶段 6/7
"
```
