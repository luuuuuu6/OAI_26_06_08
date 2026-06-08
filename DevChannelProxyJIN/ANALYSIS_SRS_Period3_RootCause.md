# SRS LS 估计器 Period-3 质量退化：根因分析

**日期**：2026-04-27
**数据来源**：`logs/q4_sweep_20260427_183347`（1000 帧 sweep, SNR = -5 ~ 25 dB, DIGITAL_AGC=0）
**分析人**：luuuuuu + AI assistant

---

## 1. 现象

在 SNR sweep 实验中，NMSE（Normalized MSE，SRS vs Sionna GT）呈现严重的 **非单调** 行为：

| SNR (dB) | slot_id | Median NMSE (dB) | |α| CV | sim_gt (mean) |
|----------|---------|-------------------|--------|---------------|
| -5       | 8       | +1.2              | 39%    | 0.70          |
| 0        | 8       | +2.1              | 47%    | 0.63          |
| 5        | 8       | +3.5              | 42%    | 0.67          |
| 10       | 8       | -2.2              | 34%    | 0.77          |
| 15       | 8       | -1.3              | 73%    | 0.50          |
| 20       | 8       | -2.0              | 75%    | 0.51          |
| **25**   | **9**   | **-10.1**         | **11%**| **0.88**      |

- SNR=25 dB 独占好表现，其余所有点 NMSE 分布混乱
- `|α|` CV 在 15/20 dB 处暴增至 73-75%
- Sionna GT 在所有帧间完全恒定（`gt_self_sim = 1.0000`），静态信道确认无误

---

## 2. 排查过程

### 2.1 排除 Proxy 侧原因

| 嫌疑项 | 验证方法 | 结论 |
|--------|----------|------|
| relative SNR 噪声注入 | 读 `v4.py` L916-941，确认 `P_sig=mean(\|y\|²)` → `P_n=P_sig/SNR` 逻辑正确 | ✅ 排除 |
| int16 量化饱和 | 测 SRS bin RMS: 980-1176（远未饱和 32767） | ✅ 排除 |
| SRS 频率跳频 | conf 确认 `freq_hopping_b_srs=0, b_hop=0, c_srs=0` | ✅ 排除 |
| 帧间 SRS RMS 不稳定 | 各 SNR 点 RMS CV ≈ 0%（除 SNR=15 有 1 个 outlier） | ✅ 排除 |
| CUDA Graph 冻结噪声功率 | 逻辑分析: `cp.mean()` 在 graph replay 时会重新计算 | ✅ 排除 |

### 2.2 发现 Period-3 确定性模式

对 SNR=20 dB 的前 9 帧做逐帧分析，发现完美的周期=3 循环：

```
Frame 0 (fid%3=2): sim_gt=0.78, STO=2.57 samp, NMSE= -2.0 dB, |α|=398  ← OK
Frame 1 (fid%3=1): sim_gt=0.94, STO=1.57 samp, NMSE= -8.7 dB, |α|=478  ← GOOD
Frame 2 (fid%3=0): sim_gt=0.08, STO=-0.08 samp, NMSE=+20.8 dB, |α|= 46  ← GARBAGE!
Frame 3 (fid%3=2): sim_gt=0.78, STO=2.57 samp, NMSE= -2.0 dB, |α|=398  ← 完全重复
Frame 4 (fid%3=1): sim_gt=0.94, STO=1.57 samp, NMSE= -8.7 dB, |α|=478  ← 完全重复
Frame 5 (fid%3=0): sim_gt=0.08, STO=-0.08 samp, NMSE=+20.8 dB, |α|= 46  ← 完全重复
```

关键指标：
- **sim_gt**：SRS 帧与 GT（静态信道）的归一化互相关。0.08 意味着该帧的信道估计与真实信道几乎无关
- **STO**：采样时间偏移。三组帧分别为 2.57/1.57/-0.08 sample，循环不变
- 同一 `fid%3` 的所有帧，上述指标完全一致（确定性，非随机噪声）

### 2.3 发现 Slot 8 vs Slot 9 的质量鸿沟

| 指标 | Slot 8 (SNR -5~20) | Slot 9 (SNR=25) |
|------|---------------------|-----------------|
| 好帧 sim_gt | 0.50 ~ 0.94 | 0.88 ~ 0.95 |
| 坏帧 sim_gt | **0.08**（完全错误） | **0.73**（仍可用） |
| |α| CV | 34 ~ 75% | 11% |
| 帧间相位旋转 std | 21 ~ 119° | **6.9°** |
| NMSE median | +3.5 ~ -2.2 dB | **-10.1 dB** |

TDD 配置（`gnb.sa.band78.fr1.106PRB.usrpb210.conf`）：

```
dl_UL_TransmissionPeriodicity = 6   (5 ms)
nrofDownlinkSlots = 7
nrofDownlinkSymbols = 6
nrofUplinkSlots = 2
nrofUplinkSymbols = 4
```

帧结构：`[DL×7 | Special(6DL+4UL) | UL(slot8) | UL(slot9)]`

**Slot 8 = DL→UL 切换后的第一个完整 UL slot**，紧邻 Special slot。
Slot 9 = 第二个 UL slot，远离切换点。

---

## 3. 根因总结

### Root Cause 1：OAI SRS LS 估计器存在 Period-3 确定性 Bug

OAI gNB 的 SRS 信道估计输出按 `frame_id % 3` 循环，产生 3 种不同的信道形状。
对于 slot 8，其中 1 种与真实信道几乎无关（sim_gt ≈ 0.08）。

这不是噪声引起的随机波动（SRS RMS 在所有帧间 CV≈0%），而是 gNB PHY 层
SRS 估计代码中的系统性 bug：
- 可能是 FFT 窗口起始位置在 3 个值之间循环
- 或 SRS LS 除法中使用的参考序列索引有误
- 或某个内部缓冲区/计数器以 3 为周期回绕

### Root Cause 2：Slot 8 SRS 质量显著劣于 Slot 9

- Slot 8 紧接 DL→UL 切换（Special slot），可能受 guard period / timing advance
  过渡影响
- 即便是 slot 8 的"好帧"（sim_gt=0.78~0.94），也远不及 slot 9 的 0.88~0.95
- 帧间相位稳定性：slot 8 std=21~119° vs slot 9 std=6.9°

### Root Cause 3：gNB MAC 调度器在不同 SNR 下分配不同的 SRS slot

- SNR = -5 ~ 20 dB → SRS on slot 8 (差)
- SNR = 25 dB → SRS on slot 9 (好)
- 导致 SNR=25 与其他点不可公平比较
- 调度差异可能来自 CQI/MCS 联动的 UL 资源分配逻辑

---

## 4. 定量验证

### 4.1 帧间相位旋转分布

| SNR | slot | Phase Std | 模式（前 8 帧中心 SC 相位） |
|-----|------|-----------|-----------------------------|
| -5  | 8    | 51.1°     | 79 -73 26 133 25 19 149 -65 |
| 0   | 8    | 21.6°     | 98 -149 -46 102 -149 -43 104 -148 |
| 5   | 8    | 89.2°     | -147 -35 71 -145 -37 77 -141 -31 |
| 10  | 8    | 51.0°     | -115 25 136 -86 26 -5 -41 72 |
| 15  | 8    | 101.2°    | 138 172 -83 133 168 -79 136 165 |
| 20  | 8    | 59.4°     | -71 39 8 -71 39 7 -71 39 |
| 25  | 9    | **6.9°**  | 59 59 -51 59 59 -51 59 59 |

SNR=20 的 period-3 pattern 最清晰：`-71, 39, 8` 完美循环。
SNR=25 (slot 9) 几乎恒定（59°），仅每 3 帧有一个 -51° 偏移。

### 4.2 帧间 SRS 互相关（相邻帧，STO 校正后）

| SNR | slot | Adjacent Correlation | 静态信道理论值 |
|-----|------|---------------------|---------------|
| 10  | 8    | 0.47                | → 1.0         |
| 15  | 8    | 0.24                | → 1.0         |
| 20  | 8    | 0.25                | → 1.0         |
| 25  | 9    | **0.68**            | → 1.0         |

即使 SNR=25 也只有 0.68，说明 OAI SRS LS 估计器的固有精度有限。

### 4.3 STO (采样时间偏移) 的 Period-3 循环

SNR=20 (slot 8):
| fid % 3 | STO (samples) | sim_gt | NMSE (dB) |
|---------|---------------|--------|-----------|
| 0       | -0.08         | 0.08   | +20.8     |
| 1       | 1.57          | 0.94   | -8.7      |
| 2       | 2.57          | 0.78   | -2.0      |

SNR=25 (slot 9):
| fid % 3 | STO (samples) | sim_gt | NMSE (dB) |
|---------|---------------|--------|-----------|
| 0       | 1.93          | 0.95   | -10.1     |
| 1       | 1.93          | 0.95   | -10.1     |
| 2       | 2.93          | 0.73   | -0.8      |

Slot 8 的 STO 范围 = 2.65 samples → 巨大相位误差
Slot 9 的 STO 范围 = 1.0 sample → 较小

---

## 5. 修复方案

### Fix 1（快速）：强制 SRS 用 Slot 9

修改 `srs_detailed_config` 中的 SRS slot offset，使 SRS 固定在 slot 9 而非 slot 8。
- 优点：所有 SNR 点公平可比，slot 9 质量远优
- 缺点：不治本

### Fix 2（分析层）：基于 period-3 / similarity 过滤坏帧

在 `digital_twin_stats.py` 中增加帧过滤：
1. 按 `frame_id % 3` 分组，丢弃 NMSE 最差的 1/3
2. 或用 sim_gt > 0.3 阈值过滤
- 优点：不需改 OAI 代码，可立即应用
- 缺点：丢弃 1/3 数据

### Fix 3（根治）：排查 OAI SRS LS 估计代码

在 OAI 源码中定位 period-3 artifact：
- `openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c`：SRS LS 估计核心
- 重点检查 FFT 窗口定位、SRS pilot 序列索引、CP 处理逻辑
- 可能涉及 `openair1/PHY/NR_TRANSPORT/nr_srs.c` 中的 SRS 资源映射

---

## 6. 遗留问题

1. **Period-3 的精确来源**：需要在 OAI C 代码中定位具体是哪个变量/计数器导致的周期
2. **Slot 8 vs 9 差异的底层原因**：可能是 timing advance 更新时机、UL 调度与 SRS 的冲突
3. **即便 slot 9 好帧 sim_gt=0.95**，仍有 5% 误差——OAI LS 估计器的固有精度上限
4. **NMSE 在 slot 8 内仍非单调**（SNR=0/5 比 SNR=-5 差）——可能涉及 UE 功控反馈
