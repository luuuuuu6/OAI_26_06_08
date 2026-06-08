# 工作日志 2026-05-17: Adaptive Alpha + CDL 多信道方案实施

## 概述

按照 `adaptive_alpha_cdl_plan_2eabfcc0.plan.md` 执行。完成 Step 0a/0b/0c/1/1.5，Step 2 adapter 已编写待容器验证。

---

## Step 0a: SRS Dump 真实间隔确认 ✅

**结论: Scenario A — 5ms 粒度，无子采样**

### 代码证据

| 文件 | 关键发现 |
|------|---------|
| `openairinterface5g_whan/openair1/PHY/defs_gNB.h:55` | `SRS_TWIN_SNR_THRESHOLD = -999` → 等效禁用，所有帧都被捕获 |
| `openairinterface5g_whan/openair1/PHY/defs_gNB.h:53` | `MAX_DUMP_FRAMES = 100` |
| `nr_ul_channel_estimation.c:1177-1218` | 每次 SRS 估计都 `memcpy` 到 ping-pong buffer，`dump_count >= 100` 时触发写盘 |

### 机制总结

```
SRS period = 10 slots = 5ms (@30kHz SCS)
每次 SRS → memcpy 到 srs_buffer[w_idx][dump_count]
dump_count >= 100 → signal writer thread → flush to .bin
无条件跳帧（SNR threshold = -999 = always pass）
```

- 数据粒度: **5ms**（连续帧间 slot 差 = 10）
- 不需要改 OAI dump 代码
- 之前看到的 160-slot 间隔——机制推断为"不应发生"，但**未做经验确认**

**⚠️ 未闭合**：计划 tier-3 要求"跑 10 秒短实验数 dump 帧数"来经验验证。当前结论纯基于代码阅读。如果代码分析正确，160-slot 观察应有其他解释（如 ping-pong drop、启动延迟）——这个"其他解释"是断言，不是验证。相对于后续问题的严重性，此项优先级最低，但 loop 不算闭合。

---

## Step 0b: Sionna CDL API 验证 ✅

**容器**: `8f2650d9956b` / `oai_sionna_luuuuuu-oai_sionna_proxy:latest`

### 验证结果

```
Sionna version: 1.0.2
CDL class: ✅ 可 import
TDL class: ✅ 可 import
```

### CDL.__init__ 签名

```python
CDL(model, delay_spread, carrier_frequency, ut_array, bs_array, direction,
    ut_orientation=None, bs_orientation=None, min_speed=0.0, max_speed=None, precision=None)
```

### CDL 输出格式

```
h.shape = (batch=1, rx=1, rx_ant=1, tx=1, tx_ant=1, paths=24, time_steps=10)
tau.shape = (batch=1, rx=1, tx=1, paths=24)
h.dtype = complex64
```

### CDL 内部 Ray 参数（关键发现）

| 属性 | Shape | 说明 |
|------|-------|------|
| `_aoa` | (1,1,1,24,20) | **24 clusters × 20 rays/cluster**，已内部展开 |
| `_aod` | (1,1,1,24,20) | 同上 |
| `_zoa` | (1,1,1,24,20) | |
| `_zod` | (1,1,1,24,20) | |
| `_delays` | (1,1,1,24) | per-cluster 延迟 |
| `_powers` | (1,1,1,24) | per-cluster 功率 |
| `NUM_RAYS` | 20 | 每 cluster ray 数 |

**结论**: Path B 可行。CDL 已完成 cluster→ray 展开（Table 7.5-3 的角度 offset），直接从 CDL 提取参数 → reshape(24×20=480 rays) → 喂 v8.py `Rays()`。Adapter 工作量 ~1-2hr。

---

## Step 0c: Oracle α_opt + MSE 基准表 ✅ （最终状态见"修正 section — 最终 Go/No-Go"）

### ls_derotation 语义确认

**类型: 信道自身相位趋势**

```python
# AdaptiveSRS2DFilter.update() 中：
inner = np.sum(H_obs * np.conj(H_smooth))
rot = inner / abs(inner)
H_derot = H_obs * np.conj(rot)
```

对于 single-SC 平坦衰落信道，de-rotation 退化为纯振幅跟踪（去除所有相位信息）。
Oracle sweep 在**不使用 de-rotation** 的条件下运行（plain EMA）。

### Oracle MSE 表

α grid: 200 点 logspace(-3, 0)，500-trial MC，2000 frames/trial，burn-in 50

| f_D (Hz) | Speed | T (ms) | SNR (dB) | α_opt | oracle MSE | SEM/MSE |
|-----------|-------|--------|----------|-------|-----------|---------|
| 9.7 | 3 km/h | 5 | 20 | 0.870 | 8.73e-3 | 0.008 |
| 9.7 | 3 km/h | 5 | 10 | 0.637 | 6.14e-2 | 0.007 |
| 9.7 | 3 km/h | 5 | 5 | 0.499 | 1.47e-1 | 0.008 |
| 9.7 | 3 km/h | 5 | 0 | 0.365 | 3.25e-1 | 0.009 |
| 97.2 | 30 km/h | 5 | 20 | 1.000 | 9.97e-3 | 0.008 |
| 97.2 | 30 km/h | 5 | 10 | 0.966 | 9.59e-2 | 0.008 |
| 97.2 | 30 km/h | 5 | 5 | 0.870 | 2.79e-1 | 0.008 |
| 97.2 | 30 km/h | 5 | 0 | 0.615 | 6.91e-1 | 0.008 |
| 324.1 | 100 km/h | 5 | 10 | 0.966 | 9.69e-2 | 0.008 |
| 324.1 | 100 km/h | 5 | 0 | 0.615 | 6.88e-1 | 0.008 |

### 关键发现（修正计划原假设）

1. **EMA 在高 SNR 的价值是 speed-conditional**：3km/h（高 ρ=0.977）在 20dB 仍有 α_opt=0.87（比 pass-through 好 12.7%，Lyapunov 验证），因为 Jakes J₀ lag-2+ 衰减慢于 AR(1) 的 ρ^k。但 30km/h（ρ≈−0.26）在 20dB α_opt=1.0（pass-through 真最优，低相关+低噪→平滑只加 lag）。
2. **EMA 价值的主要驱动是 SNR，不是速度**：低 SNR 全部速度都有显著价值；高 SNR 只有低速/高 ρ 区间有价值。
3. **α_opt 的速度依赖性（ratio 1.5-1.7×）远小于计划预期（≥3×）**。但 α_opt 距离不等于部署价值（坏 proxy：同样 1.7× 在 0dB=0.21dB penalty、在 5dB=1.31dB penalty）。
4. **计划中 α_opt ~ [0.003, 0.15] 的假设完全错误**：实际 α_opt 范围 [0.37, 1.0]。原因是 5ms 帧间隔 + 3.5GHz 载频 → 即使 3km/h 也有 σ²_Δh = 0.046 的帧间变化。

### AR(1) Cross-check — 已结案（闭式公式是错的，sweep 是对的）

偏差数据：
- 10dB, f_D=9.7Hz: 闭式偏差 3.1%（偶然接近，但公式本身有系统误差）
- 20dB, f_D=9.7Hz: 闭式偏差 14.6%

**Root cause 已关闭（Lyapunov 验证）**：闭式公式 `P = σ²_n·α/(2-α) + σ²_h·(1-ρ²)·(1-α)/(1-(1-α)²ρ²)` 是一个错误的近似——它忽略了 EMA tracking error 与 AR(1) innovation 之间的相关性。Lyapunov 2×2 状态空间精确解与数值模拟吻合到 0.03%，而闭式偏离精确解 10-30%。

结论：检查器（闭式）比被检查者（sweep）还不准。Sweep harness 在全 SNR 范围可信。20dB 的 α_opt=0.87（vs pass-through 有 12.7% MSE 增益）是真实效应——Jakes J₀ 的长 lag 相关性被 EMA 短窗平均利用。

### 输出物

- `data_out/alpha_opt_sweep_results.json` — 完整数值结果
- `data_out/mse_vs_alpha_curves.png` — MSE-vs-α 曲线图

---

## Step 1: Dual-EMA Adaptive Alpha 实现 ✅

### 架构演变

原计划的 "innovation/baseline" 自适应在稳态信道下失效（baseline 跟踪 innovation → ratio≈1.0 → α 卡在 α_min）。经过三轮迭代：

1. **Innovation/Baseline 方案** → 稳态下 r≈1.0，α 无法自适应 ❌
2. **Gradient-Correlation (VSSLMS) 方案** → 快信道下 Doppler 振荡导致反相关，α 错误下降 ❌
3. **PE 双参考方案** → prediction error ≠ estimation MSE（快信道 PE 反转） ❌
4. **ρ_obs 自相关方案** → 物理驱动，直接可工作 ✅（限制：单变量不能分离速度/SNR）

### 最终实现: `DualEMASRS2DFilter` (autocorrelation-based)

文件: `srs_2d_mmse.py`

```
H_obs → LS de-rotation → Main EMA (adaptive α) → Output
                        ↗
Online lag-1 autocorrelation → ρ_obs → α mapping
```

自适应逻辑:
```python
ρ_obs = smooth(Re(H[n] · conj(H[n-1]))) / smooth(|H|²)
α = α_min + (α_max - α_min) × (1 - |ρ_obs|)
```

- |ρ_obs| ≈ 1 → 慢信道/高 SNR → 低 α（多平滑）
- |ρ_obs| ≈ 0 → 快信道/低 SNR → 高 α（少平滑）

### 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| alpha_ref | 0.02 | 参考 EMA（de-rotation 相位基准） |
| alpha_min | 0.01 | α 下限 |
| alpha_max | 0.95 | α 上限 |
| rho_ema | 0.02 | ρ_obs 平滑系数 |
| derot_gain_db | 5.0 | De-rotation gate 阈值 |

### 设计决策记录

- **为什么保留 Reference EMA**: 提供 de-rotation 的稳定相位参考（生产环境 multi-SC 需要）
- **为什么用 |ρ_obs| 而非 Re(ρ_obs)**: Jakes 在快端有 ρ < 0（J₀ 振荡区），用绝对值确保 α 单调
- **已知限制**: 单变量 ρ_obs 在高 SNR 时给出错误方向（ρ_obs 高→低α，但高SNR实际不需要平滑）。此限制在 multi-SC 场景下通过频域结构信息缓解（Step 2+）

---

## Step 1.5: Unit Test 结果 ❌ FAIL @ε=0.10 （方向判断见"修正 section — 最终 Go/No-Go"）

计划锁定 ε=0.10。执行中无理由放宽到 0.25（不合规）。在承诺标准下两个 case 均 FAIL。

### Test 1: Rayleigh Fading MSE Gate

| Case | Adaptive MSE | Oracle MSE | Ratio | Pass@1.10 |
|------|-------------|-----------|-------|-----------|
| 3 km/h, 0dB | 0.394 | 0.325 | 1.21 | ❌ |
| 30 km/h, 0dB | 0.771 | 0.691 | 1.12 | ❌ |

注：Step 1.5 的 FAIL 不阻塞方向判断——方向的 alive/kill 由 B2 penalty 决定（见修正 section），不由 ρ_obs 算法的精度决定。算法需要改进但方向有价值。

### Test 2: P1B 方向性

```
α(3km/h) = 0.498 < α(30km/h) = 0.822  ✅
```

### Test 3: Per-Frame Cost

```
Single EMA (N_SC=1248): 53.6 μs/frame
Dual-EMA   (N_SC=1248): 82.9 μs/frame
Overhead: 1.55× (< 2.0×)  ✅
```

---

## Step 2: CDL Ray 参数注入 — 进行中

### 已完成

编写 `cdl_ray_adapter.py`：从 Sionna CDL 提取 per-ray 参数 → reshape → 保存为 v8.py 兼容的 npy 格式。

### adapter 逻辑

```
CDL(model, delay_spread, ...) → 内部展开 24 clusters × 20 rays
  ↓
提取: _aoa, _aod, _zoa, _zod (24,20), _delays, _powers (24,)
  ↓
Flatten: (24×20) = 480 rays
  ↓
delays: repeat(24→480), powers: /20 then repeat
  ↓
reshape → (1, 1, 1, 1, 480) → save as .npy
```

### 待执行（容器 `8f2650d9956b` / `oai_sionna_luuuuuu-oai_sionna_proxy`）

```bash
# 生成 CDL-C 30km/h
docker exec 8f2650d9956b python3 \
  /workspace/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/cdl_ray_adapter.py \
  --model C --speed 30 --output /workspace/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/data_out/cdl_c_30kmh

# 生成 CDL-A 3km/h
docker exec 8f2650d9956b python3 \
  /workspace/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/cdl_ray_adapter.py \
  --model A --delay_spread 30e-9 --speed 3 --output /workspace/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/data_out/cdl_a_3kmh

# 生成 CDL-D 30km/h (LOS)
docker exec 8f2650d9956b python3 \
  /workspace/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/cdl_ray_adapter.py \
  --model D --delay_spread 30e-9 --speed 30 --output /workspace/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/data_out/cdl_d_30kmh
```

---

## 新增/修改文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `srs_2d_mmse.py` | 修改 | 新增 `DualEMASRS2DFilter` 类 |
| `alpha_opt_sweep.py` | 新增 | Step 0c oracle MSE 扫描工具 |
| `test_adaptive_alpha.py` | 新增 | Step 1.5 unit test |
| `cdl_ray_adapter.py` | 新增 | Step 2 CDL→v8 参数转换器 |
| `data_out/alpha_opt_sweep_results.json` | 生成 | Oracle 数值结果 |
| `data_out/mse_vs_alpha_curves.png` | 生成 | MSE-vs-α 曲线图 |

---

---

## ⚠️ 修正：go/no-go 判断（后续 review 触发）

### Step 1.5 状态更正：FAIL（不是 PASS）

**事实**：计划锁定 ε=0.10，实测 ratio 1.21 和 1.12，均 >1.10。
执行中将 ε 无理由放宽到 0.25——计划只允许看到曲线平坦后*收紧*，未授权放宽。
在承诺的 ε=0.10 标准下，两个 case 均 **FAIL**。

| Case | Adaptive MSE | Oracle MSE | Ratio | Pass@1.10 |
|------|-------------|-----------|-------|-----------|
| 3km/h, 0dB | 0.394 | 0.325 | 1.21 | ❌ |
| 30km/h, 0dB | 0.771 | 0.691 | 1.12 | ❌ |

**参考系确认**：Oracle（plain EMA）和 Test1（DualEMASRS2DFilter with `derot_gain_db=100`→gate 永不触发→plain EMA）在 single-SC 下是同一量。MSE 均为 `|H_ema - H_truth|²`，无参考系错配。但 de-rotation 路径完全未 exercise——生产行为在此 gate 中未被验证。

### ≥3× Precondition：全面 FAIL → 触发重新论证

计划 Exp1 写死："不同场景 α_opt 差异 <3× → adaptive 的存在价值本身需要重新论证"

| SNR | α_opt 范围 | Speed ratio |
|-----|-----------|-------------|
| 0dB | [0.365, 0.615] | **1.68×** ❌ |
| 5dB | [0.499, 0.870] | **1.74×** ❌ |
| 10dB | [0.637, 0.966] | **1.52×** ❌ |
| 20dB | [0.870, 1.000] | **1.15×** ❌ |

**全部远低于 3×。** 速度维度的 α 差异不足以支撑 adaptive 的部署价值。

### Exp2-B2 分析：Global Single-α Penalty

**问题**：一个精心选择的 fixed α 跑遍所有速度，能差多少？

**初版数据（200-point grid, 500 trials, 初验）**：0dB max penalty = 0.43 dB

**Lyapunov 复验后（300-point grid, 300 trials, 5000 frames, burn-in 200）**：

| 场景 | 0dB (global α=0.435) | 5dB (global α=0.758) |
|------|---------------------|---------------------|
| 3km/h | 0.174 dB penalty | **1.306 dB** penalty |
| 30km/h | 0.213 dB | 0.362 dB |

**Operative 数字（复验后）**：
- 0dB max penalty = **0.21 dB** → KILL（adaptive 无部署价值）
- 5dB max penalty = **1.31 dB** → ALIVE（有实质部署价值）

3-speed extended（含 100km/h，0dB）：global α=0.489，max penalty = 0.473 dB，仍 < 0.5 dB → kill 不变。

### Cross-SNR 分析：真正有价值的维度

（初版数据，200-point grid, 500 trials, 2000 frames。未经 Lyapunov 复验的具体数字，结论方向可信但具体值可能有初版精度偏差。）

当 SNR 在 0dB 和 5dB 之间变化时，单一 global α 的最大惩罚：

| 场景 | Penalty (global α≈0.57) |
|------|------------------------|
| 30km/h, 5dB | **~1.69 dB** |
| 3km/h, 0dB | ~1.11 dB |
| 3km/h, 5dB | ~0.13 dB |
| 30km/h, 0dB | ~0.01 dB |

**Cross-SNR 最大惩罚 ~1.69 dB。** 这说明跨 SNR 场景有价值，但此表的主要功能是定性指出"价值在 SNR 轴不在速度轴"——具体数字若需作决策依据，应在 multi-SC precondition 中用 300-point grid 复验。

### ρ_obs 算法在价值区间的方向性问题（Issue #3）

| 条件 | ρ_obs 趋势 | 算法给出 α | 实际需要 α | 方向 |
|------|-----------|-----------|-----------|------|
| SNR 降低 | ρ_obs ↓（噪声去相关） | α ↑（少平滑） | α ↓（多平滑抑噪） | **反** ❌ |
| Speed 增加 | ρ_obs ↓（真去相关） | α ↑（少平滑） | α ↑ | ✓ |

**ρ_obs 在唯一有价值的维度（SNR）上驱动方向是反的。**

### 最终 Go/No-Go 结论（第二次修正，Lyapunov 验证后）

**之前的 "全局 kill" 结论过宽，被 SNR 分层验证推翻。**

```
Harness 验证:          PASS (Lyapunov 精确解 vs 数值模拟 = 0.03% 吻合)
AR(1) 闭式公式:        是错误近似 (偏差 10-30%)，不再用于任何 cross-check
≥3× Precondition:     退役 (证实为坏 proxy：同样 1.7× 在 0dB=0.21dB 在 5dB=1.31dB)

SNR 分层 B2 Penalty (唯一有效的 kill 指标):
  0dB:  0.21 dB → KILL (adaptive 无部署价值)
  5dB:  1.31 dB → ALIVE (有实质部署价值，由 3km/h 腿扛 1.306 dB)
  20dB: 12.7% oracle 增益 vs pass-through → 也有价值 (AR(1) 看不到的 Jakes 长 lag 结构)

结论: 方向是活的。scalar flat-fading 在 5dB 有 1.31 dB 部署价值。
      "速度自适应 α" 在中等 SNR (≈5dB) 有实质意义。
      ρ_obs 在固定 SNR 跨速度时方向正确（慢→高ρ→低α ✓）。
```

### 方法论收获（可迁移）

1. **验证工具本身需要校准**：AR(1) 闭式被赋予 cross-check 权威前未对 ground truth 验证，结果它自己是错的，差点杀掉一个活方向。
2. **α_opt 距离是部署价值的坏 proxy**：≥3× gate 应永久退役，替代为 SNR 分层 B2-penalty。
3. **harness 验证的金标准**：代数不动点（Lyapunov）vs 时域蒙卡，零代码共享，非平凡值吻合到 <1%。

### 精度边界

- "1.31 dB" 是等权（3km/h 和 30km/h 各 50%）假设下的值。结论（5dB 有价值）对加权不敏感（3km/h 单腿就 1.306 dB），但具体数字等生产先验定后再钉。
- "比值对 Jakes 近似免疫" 的论证不严谨——正确理由是 3km/h（价值腿）的 ρ 误差仅 0.0015（Step A），快速腿的 0.085 误差不扛价值。

### 决策

**方向不转。** Scalar 验证弧线闭合。下一步：

**Multi-SC precondition: BANKED (2026-05-18)**

验证完成，三层通过：
- Part 1 (OFF, 10dB): ALIVE 1.258 dB → continue 与 de-rotation 对错解耦
- Part 2 (Test C): PASS (angle error 10⁻¹⁶, MSE 10⁻³²) → de-rotation 原语数学正确
- Part 3 (Test A): PASS (max deviation 0.87% < 2%) → de-rotation inert when expected

B2 penalty (CDL-C, de-rotation ON):
- 0dB: 0.041 dB → KILL
- 5dB: 0.255 dB → KILL
- 10dB: **1.085 dB → ALIVE** (3km/h 腿扛 1.085, 30km/h 0.489)

**Direction: CONTINUE.**

### ρ_obs Capture Gate (2026-05-18): FAIL

ρ_obs 在 multi-SC CDL 上彻底失败：
- 3km/h: adaptive MSE = 0.135 = **2.93× oracle**（limit 1.14×）→ FAIL
- 30km/h: adaptive MSE = 0.164 = 2.15× oracle → FAIL
- α 收敛到 0.14（oracle 要 0.42）和 0.34（oracle 要 0.79）→ 系统性过度平滑
- **比不自适应（global fixed-α）还差 ~3.6 dB**

Root cause: de-rotation 制造表观相干 → ρ_obs 读成"信道慢" → α 过低。
这和 scalar SNR-轴方向反是同一个根缺陷的 multi-SC manifestation。
**ρ_obs（作为 feature）正式死亡；连续映射架构本身在 rot_rate 下复活（见下）。** "multi-SC 频域结构缓解 ρ_obs"的期票兑付为负。

### rot_rate Feature 发现 + Spec 推导 (2026-05-18)

从已有数据推出替代 feature（不是新实验，是磁盘数据计算）：

**Feature 分离度（CDL-C, 10dB, 20 realizations, oracle 固定 α 下的 rot）**：

| Feature | 3km/h | 30km/h | d' (Cohen's d) | 可用？ |
|---------|-------|--------|-----------------|--------|
| ρ_obs_derotated (POISONED) | 0.841 | 0.704 | 4.1 | ❌ 中毒 |
| ρ_obs_raw | 0.823 | 0.517 | 5.7 | ✓ |
| **rot_rate** | **0.260** | **2.319** | **21.6** | **✓✓✓** |
| innov_magnitude | 0.323 | 0.360 | 0.6 | ✗ |
| freq_gradient | 0.185 | 0.182 | 0.6 | ✗ |

d' 表 rot_rate 值（0.260/2.319）来自 20 realizations 平均。d' = 21.6 是旁证（feature 强分离），不承载部署保证。

**rot_rate = |angle(rot[n]) − angle(rot[n−1])|**
- **rot 来源必须指定**：Reference EMA（α_ref=0.02）的 rot，不是 Main EMA 的闭环 rot
- 物理含义 ≈ 2πf_D T。管线不变（是 de-rotation 的输入，洗不掉自己在追踪的量）
- 满足 spec 约束："observable 与真信道速度的关系必须对处理管线不变"

**Binary vs 连续（8 中间速度验证，5 realizations/speed，oracle 固定 α 下的 rot）**：

| Speed | rot_rate | α_opt |
|-------|---------|-------|
| 3 | 0.269 | 0.416 |
| 5 | 0.432 | 0.524 |
| 8 | 0.723 | 0.574 |
| 10 | 0.829 | 0.616 |
| 15 | 1.294 | 0.691 |
| 20 | 1.673 | 0.724 |
| 25 | 2.130 | 0.758 |
| 30 | 2.369 | 0.794 |

rot_rate 值与 d' 表略有差异（0.269 vs 0.260, 2.369 vs 2.319）——不同 run（5 vs 20 realizations），正常 MC 抖动，不影响结论。Canonical 拟合用此 8 速度表。

**α_opt vs rot_rate 线性拟合: α = 0.157·rot_rate + 0.446, R²=0.91**
- R²=0.91 证明关系是连续斜坡不是二元台阶；"近二元"是两点采样 artifact
- ⚠️ **R²=0.91 是拟合优度，不是部署闸。** Binding test = 3km/h 腿实测 MSE-ratio ≤ 1.14
- ⚠️ **拟合最大残差在 3km/h（全部部署价值所在的腿）**：oracle α=0.416, 拟合预测 0.489, +17.5% 过度预测 → 欠平滑方向

**⚠️ 标定/部署失配风险**：系数 (0.157, 0.446) 标定时 rot_rate 在 oracle 固定 α（每速度不同）下测。部署 rot_rate 来自 Reference EMA（α_ref=0.02）的 rot — 不同 α 下 rot 分布不同。→ capture gate 前必须用 Reference EMA 的 rot_rate 重跑 8-speed 表并**无条件 re-fit 系数**（by construction 消除失配；capture gate 是唯一 binding arbiter）。

**误差预算**：用错 α 的代价 = 1.76× oracle → p_max = 18.4%。旁证，不承载部署保证。

### Capture Gate Pre-commits (写码前锁死)

- ε = **0.14** (14%), pre-committed, no relaxation
  Basis: pass ⟹ 1.085 − ε_dB ≥ 0.5 deployment threshold (零自由旋钮)
- Reference: **true-frame** — MSE = mean_k|H_ema[k]·rot − H_true[k]|²
- Oracle: 3km/h α_opt=0.416 MSE=0.04614; 30km/h α_opt=0.794 MSE=0.07614
- Deployment: 3km/h leg only (1.085 dB > 0.5); 30km/h 0.489 dB not relevant

**⚠️ Spec 状态: DERIVED，未经 gate 验证。**
R²=0.91 是拟合优度，不是部署闸。Binding validation = 3km/h leg 实测 adaptive MSE-ratio ≤ 1.14，不是 R²。拟合最大残差在 3km/h leg（oracle α=0.416，拟合预测 0.489，+17.5%，欠平滑=失败方向）。新窗口不得因 R²=0.91 宣告成功；必须实跑 capture gate 在 3km/h leg 以 ε=0.14 判定。

**⚠️ rot_rate 来源：标定/部署失配风险。**
系数 (0.157, 0.446) 标定时 rot_rate 在 oracle 固定 α（每速度不同）下测。部署 rot_rate 来自 Reference EMA（α_ref=0.02）的 rot。两者 α 不同 → rot 分布不同。Spec 指定：rot 源 = Reference EMA @α_ref=0.02。Step 0 用同源 rot_rate 无条件 re-fit 系数（不设"沿用"捷径）。Capture gate (Step B) 是唯一 binding arbiter。

- Algorithm: **rot_rate→α continuous mapping** (DERIVED, 待验证)
  - Mapping: α = a·rot_rate + b (初始拟合 a=0.157, b=0.446; 可能需 re-fit)
  - rot_rate 来源: **Reference EMA 的 rot**（α_ref=0.02，稳定源）
- Scope: CDL-C/10dB single cell; generalization is separate gate

### Phase 2 执行结果 (2026-05-18，新窗口完成)

**Capture Gate: ALL PASS (ratio = 1.011)**

执行中发现并修复了 DualEMA 架构缺陷：
- 旧: Main EMA de-rotation 引用 H_ref（Reference EMA）→ H_ref 太慢跟不上 → derot gate 对 3km/h 从不触发 → Main EMA 实际是 plain EMA without de-rot → 与 oracle (self-ref de-rot) 不同构
- 新: 双自引用 de-rotation
  - Main EMA: rot_main = sum(H_obs·conj(H_main))/|...| → 与 oracle 完全同构
  - Reference EMA: rot_ref = sum(H_obs·conj(H_ref))/|...| → 仅用于 rot_rate 测量

Step 0 re-fit: a=0.1437, b=0.4493, R²=0.9163（旧: 0.157, 0.446, 0.91，接近但 re-fit 消除了标定/部署失配）

| Speed | Oracle MSE | Adaptive MSE | Ratio | α_opt | α_conv | Pass@1.14 |
|-------|-----------|-------------|-------|-------|--------|-----------|
| 3km/h (部署腿) | 0.03751 | 0.03792 | **1.011** | 0.448 | 0.489 | ✅ |
| 30km/h (sanity) | 0.08506 | 0.08601 | **1.011** | 0.857 | 0.821 | ✅ |

参数: N_SC=1248（生产 SRS 子载波数）, SNR=10dB, CDL-C, 2000 frames, 20 trials

部署价值兑现: adaptive loss vs oracle = 10·log10(1.011) = 0.048 dB → net value = 1.085 − 0.048 = **1.037 dB > 0.5 threshold ✅**

新增文件:
- `multisc_cdl_sim.py` — multi-SC CDL 信道仿真 + oracle sweep + rot_rate 工具
- `step0_refit_rot_rate.py` — Step 0 无条件 re-fit 脚本
- `step_b_capture_gate.py` — Step B capture gate 脚本
- `data_out/step0_refit_results.json` — re-fit 数据
- `data_out/step_b_capture_gate_results.json` — capture gate 数据

### Step C Generalization Gate 结果 (2026-05-18)

**CDL 模型泛化: ALL PASS (4/4 legs)**

| 条件 | 3km/h ratio | 30km/h ratio |
|------|------------|-------------|
| CDL-A 10dB | 1.053 | 1.117 |
| CDL-D 10dB | 1.005 | 1.070 |

CDL-C/10dB 系数天然泛化到 CDL-A/D，无需 re-fit。物理原因: rot_rate ≈ 2πf_DT 只取决于 Doppler，不取决于信道模型的 delay/角度结构。

**SNR 泛化: FAIL (3/4 legs fail)**

| 条件 | 3km/h ratio | 30km/h ratio |
|------|------------|-------------|
| CDL-C 5dB | 1.209 FAIL | 1.113 PASS |
| CDL-C 20dB | 1.644 FAIL | 2.427 FAIL |

Root cause: rot_rate 是纯 Doppler feature，不含 SNR 信息 → 映射在所有 SNR 下给出相同 α。

### Phase 2 自审 — 四项验证 (2026-05-18)

**验证 A: Step 0 拟合目标过期**
KNOWN_ALPHA_OPT 硬编码自 Phase 1，与当前架构 fresh oracle 系统性偏差 +9~12%。
影响有限: MSE 曲线在最优点附近平坦，α 偏 10% 只带来 1% MSE 惩罚。

| Speed | Phase 1 (hardcoded) | Fresh (current arch) | Δ |
|-------|--------------------|--------------------|---|
| 3 | 0.416 | 0.454 | +9.0% |
| 10 | 0.616 | 0.689 | +11.9% |
| 30 | 0.794 | 0.870 | +9.5% |

**验证 B: B2 penalty 复验 → 成立**
当前架构 B2 = 1.111 dB（Phase 1: 1.085 dB），ε=0.14 推导链成立。
重要修正: 30km/h penalty = **1.037 dB > 0.5 dB** → 30km/h 也有部署价值（Phase 1 报 0.489 dB 是基于旧 oracle 的错误结论）。

| Speed | Global α=0.658 MSE | Oracle MSE | Penalty |
|-------|-------------------|-----------|---------|
| 3km/h | 0.0550 | 0.0428 | 1.087 dB |
| 30km/h | 0.1097 | 0.0864 | **1.037 dB** |

**验证 C: MSE 曲线平坦度 → 关键发现**
fixed α=0.5 (无自适应) vs oracle:
- 3km/h: ratio **1.023 PASS** — 曲线极平，任何合理 α 都行
- 30km/h: ratio **1.863 FAIL** — 曲线陡峭，必须自适应

自适应的真正价值在 30km/h 腿（从 1.86× 降到 1.01×），不在 3km/h 腿。
但 30km/h 腿 B2=1.037 dB > 0.5 dB → 确实有部署价值。

**验证 D: Fresh re-fit → 旧系数反而更好（因为曲线平坦）**
新系数 (fresh target): a=0.1613, b=0.4850
旧系数 (Phase 1 target): a=0.1437, b=0.4493
两者都 PASS，旧系数 ratio 更低（1.012 vs 1.049），因为 MSE 曲线平坦区偏保守也没惩罚。

### Phase 2 修正叙事

Phase 1 的叙事: "自适应对 3km/h 部署腿有价值（B2=1.085 dB），30km/h 不重要（0.489 dB）"
修正后的叙事: "自适应对**两条腿都有价值**（B2≈1.1 dB）:
- 3km/h: MSE 曲线平坦，任何 α∈[0.4, 0.7] 都行 → 自适应保底不坏
- 30km/h: MSE 曲线陡峭，必须 α≈0.87 → **自适应是刚需**（fixed 0.5 → 1.86× FAIL）
- 真正的价值: 一个映射同时覆盖两条腿，不需要 per-speed 调参"

### Phase 3 方向: 二维映射 α=f(rot_rate, SNR_est)

当前 1D 映射瓶颈: rot_rate 只感知 Doppler，不感知 SNR → 跨 SNR 泛化 FAIL。

**Feature 实测数据 (CDL-C, 8 conditions)**:

| SNR | Speed | rot_rate | |ρ_raw| | innov |
|-----|-------|---------|--------|-------|
| 0 | 3 | 0.27 | 0.48 | 1.26 |
| 10 | 3 | 0.27 | 0.88 | 0.17 |
| 20 | 3 | 0.28 | 0.96 | 0.05 |
| 0 | 30 | 2.58 | 0.27 | 1.80 |
| 10 | 30 | 2.59 | 0.50 | 0.63 |
| 20 | 30 | 2.61 | 0.56 | 0.52 |

两个候选 SNR feature:
- |ρ_raw|: 3km/h 区分度极好 (0.48→0.96), 30km/h 弱 (0.27→0.56)
- innov: 两速度都有区分度, 30km/h 高 SNR 饱和

**三方案评估**:

| | OAI SNR | Innovation | |ρ_raw| |
|-|---------|-----------|---------|
| 精度 | 最高 | 中等 | 3km/h 极好, 30km/h 弱 |
| 计算 | 零 | 零(已有) | 极小 |
| 仿真验证 | 不可行 | 可行 | 可行 |
| 实现复杂度 | 中(耦合OAI) | 中(分离噪声) | 低 |

### Phase 3: SNR feature 探索 → 二维映射 (2026-05-18)

**目标**: 解决 1D 映射的 SNR 泛化失败，升级为 α = f(rot_rate, SNR_est)。

**方案 2 (Innovation as SNR feature): 死亡**

2D 拟合: α = 0.2394·rot_rate − 0.4848·innov + 0.6567, R²=0.86
闭环测试: **4/8 FAIL**。0dB/3km/h 上正反馈死循环（α 被压到 0.076，oracle 需 0.207）。

Root cause: innovation = noise + tracking_error。α 降低 → tracking_error 增大 → innov 增大 → α 进一步降低。闭环反馈方向错误。与 Phase 1 的 innovation 失败是同族问题。

**方法论教训（第六条）**:
> 凡从滤波器输出派生的 feature 都有闭环反馈风险。只有从原始观测 H_obs 派生的 feature 才安全。

| Feature 来源 | 闭环？ | 结果 |
|-------------|--------|------|
| Innovation (H_main diff) | 是 | Phase 1 ❌, Phase 3 ❌ |
| ρ_obs derotated (H_derot) | 是 | Phase 1 ❌ |
| rot_rate (rot angle) | 弱 | ✓ (rot 不受 α 强影响) |
| **even-odd pair (H_obs)** | **否** | **✓** |

**方案 4 (Even-Odd Pair + C_model 校正): 最终选择**

原理: 利用 OFDM 频域结构——信道跨子载波平滑，噪声跨子载波独立:
```
P_minus = mean |H_obs[even] − H_obs[odd]|² / 4  ≈ C_model + σ²_n/2
C_model = Σ_r P_r × (1−cos(2πΔf·τ_r)) / 2       (从 ray 参数预计算的常数)
σ²_n = 2 × (P_minus − C_model)
SNR_est = (P_plus − P_minus) / (P_minus − C_model)
```

优势:
- **开环**: 纯从 H_obs 计算，不经过滤波器，无闭环反馈
- **零额外内存**: 不需要 H_obs_prev，单帧完成
- **SCS 无关**: Δf × τ_max ≈ 7% (NR CP 设计常数)，所有 SCS 配置精度相同
- **C 友好**: 可融入 inner product 循环，零额外 pass

SNR 估计精度（C_model 校正后，系统偏移 +3dB 被线性拟合吸收）:

| 真实 SNR | 估计 SNR | 偏移 | 与速度相关？ |
|---------|---------|------|-----------|
| 0 dB | 3.0 dB | +3.0 | 否 |
| 10 dB | 13.0 dB | +3.0 | 否 |
| 20 dB | 23.0 dB | +3.0 | 否 |

**2D 拟合**:
```
α = 0.1132·rot_rate + 0.0305·SNR_est_dB + 0.1074
R² = 0.9082
训练集: 5 SNR (0,5,10,15,20 dB) × 6 速度 (3,5,10,15,20,30 km/h) = 30 条件
```

**Capture Gate: 8/8 PASS (跨 SNR + 跨速度)**

| SNR | 3km/h ratio | 30km/h ratio |
|-----|------------|-------------|
| 0 dB | 1.036 | 1.064 |
| 5 dB | 1.016 | 1.012 |
| 10 dB | 1.033 | 1.067 |
| 20 dB | 1.102 | 1.041 |

对比 1D 映射:
- 1D (rot_rate only): 10dB PASS, 5dB FAIL (1.21), 20dB FAIL (1.64/2.43)
- **2D (rot_rate + SNR): 全部 PASS, 最大 ratio 1.102**

### 最终算法（Phase 3 定稿）

```
每 5ms SRS 帧:
  ① rot_rate = |angle(rot[n]) − angle(rot[n−1])|     ← 感知 Doppler（速度）
  ② SNR_est = even-odd pair + C_model 校正             ← 感知噪声（开环）
  ③ α = clamp(0.113·rot_rate + 0.031·SNR_dB + 0.107)  ← 二维线性映射
  ④ H_main += α × (H_derot − H_main)                  ← EMA 更新（同 OAI 原有逻辑）
```

C 实现预算:
- Pass 1: inner product + even-odd pair (融合) → ~3 ops/SC
- Pass 2: de-rotate + EMA update → ~3 ops/SC
- 标量: atan2 + 2 个 EMA smooth + 1 个 linear map → O(1)
- 内存: H_main[1248] + 4 个 float = ~10KB
- **无 Reference EMA，无 H_obs_prev，比 Phase 2 版本更简单**

### 代码产出总览

| 文件 | Phase | 说明 |
|------|-------|------|
| `srs_2d_mmse.py` | 2 | DualEMASRS2DFilter (Phase 2 版，待升级 Phase 3) |
| `multisc_cdl_sim.py` | 2 | multi-SC CDL 仿真 + oracle sweep + 工具 |
| `step0_refit_rot_rate.py` | 2 | 1D re-fit 脚本 |
| `step_b_capture_gate.py` | 2 | 1D capture gate |
| `step_c_generalization_gate.py` | 2 | 泛化测试 |
| `cdl_ray_adapter.py` | 1 | CDL→v8 ray 参数转换 |
| `data_out/cdl_c_30kmh/` | 1 | CDL-C ray 参数 |
| `data_out/cdl_a/`, `cdl_d/` | 2 | CDL-A/D ray 参数 |

### Phase 3 跨模型验证 + 碗深分析 + 锁定 (2026-05-18)

**CDL 全模型混合标定: 失败**

5 模型混合拟合: α = -0.2376·rot + 0.0321·SNR + 0.5894, R²=0.76, **a1 变负**。
Capture gate: 3/40 PASS。混合后线性拟合完全崩溃。

Root cause: **各 CDL 模型在同一 (rot_rate, SNR) 下 α_opt 不同**。

| Model | 3km/h 10dB α_opt | Doppler spread | AoA spread |
|-------|------------------|---------------|-----------|
| CDL-A | 0.59 | 5.62 Hz (最宽) | 71° (最散) |
| CDL-E | 0.37 | 2.20 Hz (最窄) | 27° (最窄) |

rot_rate 只看相位旋转速度（mean Doppler），看不到 Doppler spectrum 宽度。
高速端（30km/h）各模型 α_opt 收敛到 0.84-0.89；低速端（3km/h）发散成扇形。
**单条曲线（无论线性还是非线性）无法表示扇形——是特征维度缺失，不是模型类问题。**

**碗深实测 (CDL-A vs CDL-E, 3km/h, 10dB)**

| α | CDL-A penalty | CDL-E penalty |
|---|-------------|-------------|
| 0.30 | +2.14 dB | +0.45 dB |
| 0.40 | +0.76 dB | +0.01 dB ◄ opt |
| **0.48** | **+0.13 dB** | **+0.34 dB** |
| 0.55 | +0.00 dB ◄ opt | +0.86 dB |
| 0.70 | +0.56 dB | +2.11 dB |

**碗深判定: 折中 α=0.48 最大额外损失 = 0.34 dB (CDL-E)**。
碗不对称: 欠平滑(α 偏高)代价 > 过度平滑(α 偏低) → 折中应偏高α侧。
当前 2D 映射(CDL-C 标定)在低速端输出 α≈0.49 → 自然是偏高折中 → 对所有模型安全。

**第三特征 |R(1)|/R(0) 验证: 不引入**

| Model | rot_rate | |R(1)|/R(0) | α_opt |
|-------|---------|-----------|-------|
| CDL-A | 1.634 | 0.867 | 0.572 |
| CDL-E | 1.565 | 0.881 | 0.394 |

**|R(1)| 只差 1.6%，无区分度。**
物理原因: J₀(x) ≈ 1−x²/4 在 x=2πf_DT=0.305 处是二次平顶。Doppler spread 差异只进到 x² 量级 ≈ 1%，被 10dB 噪声淹没。
这不是错的特征，是 lag 选在了平顶。判别信息在 2πf_D·kT ~ O(1) 的更长 lag 上。对 3km/h/3.5GHz 放弃它是正确的。

---

### P1B per-scenario 标定验证 (7.5GHz, GPU, 2026-05-18)

CuPy 14 + Blackwell GPU（119× 加速 vs numpy CPU）。
每个 RX 位置独立标定 (a1, a2, a3)，然后跑 capture gate。

| RX | Rays | τ_max | R² | Gate | Worst ratio |
|----|------|-------|-----|------|------------|
| 0 | 380 | 799ns | 0.912 | 3/8 | 1.80 |
| 200 | 380 | 1405ns | 0.743 | 1/8 | 8.69 |
| 500 | 400 | 218ns | 0.862 | 7/8 | 1.30 |
| 800 | 400 | 285ns | 0.925 | 1/8 | 1.96 |
| 1000 | 400 | 222ns | 0.956 | 6/8 | 1.31 |

即使 per-RX 标定，RX 0/200/800 仍大面积 FAIL。

**3.5GHz 载频修正 (2026-05-18)**：v8.py 硬编码 carrier_frequency=3.5e9。之前用 7.5GHz 跑 P1B 是载频错误。3.5GHz 重跑后仍大面积 FAIL，且整体更差。**载频不是主因**——这是被新数据确认的唯一事实。

**B-4 去混淆实验 (2026-05-18)**: 完美 SNR 对照

给每个 RX 喂完美 SNR（true SNR，不经 even-odd 估计），per-RX 标定后跑 capture gate。
目的：分离 H1（SNR 特征垃圾→映射崩）和 H2（映射本身在 P1B 不成立）。

| RX | τ_max | R²_perfect | 3km/h avg R_eo | 3km/h avg R_ps | 完美SNR救了？ |
|----|-------|-----------|---------------|---------------|------------|
| 0 | 799ns | 0.631 | 5.63 | 6.43 | 没有，更差 |
| 200 | 1405ns | 0.667 | 4.14 | 2.61 | 改善但仍全挂 |
| 500 | 218ns | 0.813 | 1.12 | 1.12 | 差不多 |
| 800 | 285ns | 0.657 | 4.24 | 5.54 | 没有，更差 |
| 1000 | 222ns | 0.823 | 1.44 | 2.06 | 更差 |

**H1 证伪**：完美 SNR 未修复长 τ_max RX，RX 0/800 反更差。"SNR 特征被 τ_max 打穿"机制排除。
**H2 成立**：R²_perfect = 0.63-0.67、干净输入下 a1 仍出极端值 (2.45/4.24/−0.29)。在这 5 个 P1B RX 上 α_opt 与 (rot_rate, SNR) **不存在稳定线性关系**。与 Phase 3 五模型混合拟合崩溃同性质（特征维度缺失，非脏输入）。

**τ_max 与 R² 相关但因果未验**：短 τ_max RX (218/222ns) R² 也仅 0.81-0.82，RX 1000 完美 SNR 下从 4/8 退化到 2/8。非线性不限于长 τ_max。"τ_max 导致非线性"为假设，无实验支撑，不入结论。"需要更多物理量"为方向猜测，非本实验结论。

**全局解读（非 B-4 实验结论，标记为解读）**：H2 成立的实际含义不是"算法架构错了"——是 P1B 这类 ray-tracing 信道超出了 (rot_rate, SNR) 两维能描述的范围。CDL/3.5GHz 绑定假设内算法仍然是验证通过的。P1B 归位为 scope 边界问题，不回头否定已锁的 CDL 结论。

---

## 锁定结论 (2026-05-18, 修订版)

**结论**: rot_rate + even-odd SNR（CDL-C 系数）2D 映射，跨 CDL 模型折中损失 **< 0.5 dB**。

⚠️ 精确表述：**此 < 0.5 dB 是在 3km/h 10dB 单一操作点实测的 spot-check，不是全域保证。** 高速端各模型收敛（扇形结构），折中只会更好；但低速端不同 CDL 模型碗深不同（CDL-A @ α=0.30 有 +2.14 dB，碗并不平），"低速碗平"是 CDL-C 的性质，不可外推到所有模型。结论未崩（0.34 < 0.5），但叙事曾被过度一般化，此处修正。

映射行为: **低速端输出 α≈0.49（跨 CDL 折中），高速端由 rot_rate 自适应到 α≈0.85+（各模型收敛）**。

**绑定假设**:
- 载频 3.5 GHz
- ⚠️ **帧间隔 T = 5 ms — 未经验验证**（Step 0a 仅通过代码阅读推出，160-slot 经验观测被归为 artifact 但未实验否定。若真实 T ≠ 5ms，2πf_DT、α_opt landscape、J₀ 平顶论证全部失效。这是全链最底层的未闭合项，优先级最高。与 AR(1) 闭式是同一反模式——methodology #1"验证工具本身需要校准"应当适用于此但执行时遗漏。）
- 速度范围 3–30 km/h
- SNR 范围 0–20 dB

**未验证/未推导项 → 闭合记录 (2026-05-18)**:

1. **T=5ms → 已闭合: T=5ms 确认**
   配置文件 `gnb.sa.band78.fr1.106PRB.usrpb210.conf` 第 182 行:
   `srs_detailed_config.periodicity = 10` (slot 单位) → 10 slots × 0.5ms (30kHz SCS) = **5ms**。
   此配置覆盖了 `set_ideal_period()` 的动态计算 (nb_slots_per_period × MAX_MOBILES_PER_GNB = 160)。
   TDD pattern: dl_UL_TransmissionPeriodicity=6 (5ms), 7DL+1mixed+2UL = 10 slots/period。
   **T=5ms 从配置级确认，全链假设成立。**
   160-slot 经验观测仍未解释（可能是 ping-pong drop、启动延迟、或不同运行配置的残留），但不影响 T=5ms 的配置级确认。此项降级为 tier-3 遗留问题。

2. **0.5 dB 部署阈值 → 已闭合: 无推导来源**
   在日志和 plan 中追溯，0.5 dB 首次出现即为断言（"deployment threshold"），无 3GPP 规范引用、无 BLER 仿真推导、无系统级分析支撑。
   **影响**: 所有 alive/kill 决策的判据是一个未锚定的断言。结论不一定错（"net value > 0.5"的稳健性取决于净值离 0.5 多远），但 0.5 这条线本身需要在未来补推导。
   候选锚定方法: (a) 0.5 dB MSE 改善对应多少 BLER 改善？(b) 和不做自适应的 baseline 的吞吐量差异。这需要系统级仿真，超出当前信道估计层的范围。

3. **碗深单点外推 → 暂不补测**
   T=5ms 下的碗深数据（CDL-A vs CDL-E @ 3km/h 10dB）因 T 假设错误而**全部作废**。
   在正确的 T (可能 80ms, 也可能 per-deployment 不同) 下需要完全重跑碗深分析。
   由于 #1 (T) 尚未确定最终值，碗深补测 blocked on T 的确定。

**已知边界**:
- P1B (3.5GHz 修正后): per-RX 标定后仍 3/5 RX 大面积 FAIL。B-4 去混淆实验（完美 SNR 对照）证伪了 H1（SNR 特征失效），确认 H2（α_opt 与 (rot_rate, SNR) 在 P1B 上不存在稳定线性关系）。τ_max 与 R² 相关但因果未验。P1B 归位为 scope 边界：(rot_rate, SNR) 两维不足以描述 ray-tracing 信道的 α_opt 行为。
- |R(1)|/R(0)|: 在 3.5GHz/3km/h/T=5ms 操作点无区分度（J₀ 二次平顶，x=0.305）。此结论绑定在 T=5ms——若 T 更大，x 离开平顶，|R(1)| 可能复活。
- 跨 CDL 模型: 碗深在 spot-check 操作点 < 0.5 dB。全域保证需更多操作点验证。

**最终算法**:

```
每 5ms SRS 帧 (AdaptiveAlphaEMA):
  ① rot_rate = |angle(rot[n]) − angle(rot[n−1])|        ← Doppler 感知
  ② SNR_est = even-odd pair + C_model 校正               ← 噪声感知（开环）
  ③ α = clamp(0.113·rot_rate + 0.031·SNR_dB + 0.107)    ← 2D 映射
  ④ H_main += α × (H_derot − H_main)                    ← EMA 更新
```

**代码产出**:

| 文件 | 说明 |
|------|------|
| `srs_2d_mmse.py` → `AdaptiveAlphaEMA` | Python 最终版 (Phase 3, 验证通过) |
| `srs_adaptive_alpha.c` | C 蓝图 (可集成到 OAI `nr_ul_channel_estimation.c`) |
| `multisc_cdl_sim.py` | 仿真工具链 |
| `data_out/cdl_{a,b,c_30kmh,d,e}/` | 全 5 个 CDL 模型 ray 参数 |

---

## 技术备忘（冻结区 — 仅保留不含 claim 的参考信息）

所有事实 claim 的 canonical 版本在各 Phase section 和"锁定结论"。本区不做独立陈述，仅存放参考常数。

### 物理参数

- 载频 3.5 GHz，SCS 30 kHz，帧间隔 T = 5 ms
- σ²_Δh = 2(1−J₀(2πf_D T))：3km/h = 0.046，30km/h = 2.52
- f_D：3km/h = 9.7 Hz，30km/h = 97.2 Hz，100km/h = 324.1 Hz

### 容器信息

```
Container ID: 8f2650d9956b
Image: oai_sionna_luuuuuu-oai_sionna_proxy:latest
Network: host
GPU: all NVIDIA devices
Volumes:
  ./vRAN_Socket → /workspace/vRAN_Socket
  ./openairinterface5g_whan → /workspace/openairinterface5g_whan
```

### 编辑铁律

修正 section 是所有事实 claim 的唯一 canonical 载体。Body sections（Step 0a–2、技术备忘）从此冻结，不再编辑其内容。如需更新事实，只修改修正 section。
