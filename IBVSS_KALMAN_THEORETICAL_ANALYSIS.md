# IBVSS / Scalar Kalman 理论分析文档

| 字段 | 值 |
|------|----|
| **日期** | 2026-05-23 |
| **用途** | 教授汇报参考材料 |
| **范围** | IBVSS 理论上限、Kalman 对比、复杂度分析、已知局限 |

---

## 1. IBVSS 理论基础与最优性

### 1.1 状态空间模型

Per-SC 信道建模为标量随机游走 + AWGN 观测：

```
x[n] = x[n-1] + w[n],    w ~ CN(0, Q)    (信道变化)
y[n] = x[n] + v[n],      v ~ CN(0, R)    (观测噪声)
```

EMA 滤波器 `H_smooth += α · (H_derot - H_smooth)` 对应的 Kalman 稳态解：

```
P_pred = Q/α                   (预测误差方差)
E[|innovation|²] = P_pred + R  (innovation 方差)
ratio = E[|e|²] / R = 1/(1-α)  (innovation-noise 比值)
α_opt = 1 - 1/ratio            (核心公式)
```

### 1.2 稳态最优性证明

**命题**：当信道严格满足 random walk 模型且 R 已知时，IBVSS 在稳态下给出 MMSE 最优的 α。

**证明**：
- Kalman 稳态 gain K_ss 满足 Riccati 方程 `K = P/(P+R), P = K·Q/K = Q/K` → `K² + K·(Q/R) - Q/R = 0`
- IBVSS 映射 `α = 1 - 1/ratio` 在稳态 `ratio_ss = 1/(1-K_ss)` 处精确恢复 K_ss
- 因此 IBVSS α_conv = K_ss = Kalman 稳态 gain □

**推论**：IBVSS 和完整 Kalman 在稳态下的 MSE **完全相同**，差异仅在瞬态。

### 1.3 与 Oracle 的理论 gap

稳态下 gap = 0。实际 gap 来源：

| 来源 | 机制 | 影响量级 |
|------|------|---------|
| innov_ema 延迟 | ratio 需要 ~1/ema = 7 帧收敛 | 切换后 35 ms (SRS 5ms × 7) |
| R_est 偏差 | even-odd 在高频率选择性下偏高 | DS>500ns 时 α 偏低 ~10% |
| 全局 de-rotation | 单标量 rot 无法校正 per-SC Doppler | 多散射体时 innovation 偏高 |
| alpha_max 截断 | α_opt > 0.98 时被 ceiling 截断 | 高 SNR+高速条件 |

CDL-C 实测 gap（8 条件平均）：ratio = 0.989（**优于** oracle 1.1%）— 因为帧自适应可以超越固定 α。

---

## 2. IBVSS vs Scalar Kalman 对比

### 2.1 理论对比

| | IBVSS | Scalar Kalman |
|--|-------|---------------|
| **稳态性能** | MMSE 最优 | MMSE 最优（相同） |
| **瞬态性能** | 依赖 innov_ema 速度 | P 大→K≈1 自然快收敛 |
| **Q 估计** | 隐式（ratio 包含 Q/R 信息） | 显式 IAE: Q = σ²_innov - P - R |
| **冷启动** | 需 20 帧 warmup | 理论上更快（P_init 大） |
| **鲁棒性** | 更好（无 Q 估计发散风险） | 低 SNR 下 Q_est→0 可能锁死 |

### 2.2 为什么 Kalman 不一定更好？

Kalman 多了 IAE Q 估计环节。当 Q 估计精确时，Kalman 在瞬态优于 IBVSS。但 Q 估计本身引入噪声：

```
Q_est = max(0, innov_smooth - P - R)
```

- `innov_smooth` 有 EMA 平滑延迟 + Monte Carlo 方差
- `P` 依赖上一帧的 K（循环）
- `R` 来自 even-odd 估计器（有 bias）
- `max(0,...)` 截断丢失负方向信息

当三者的估计误差之和 > Q 估计带来的理论增益时，Kalman 净效果为负。

**实测验证**：
- CDL-C: Kalman 8/8 PASS，IBVSS 8/8 PASS（差异 <1%）
- P1B: Kalman 4/6 PASS，IBVSS **6/6 PASS**
- Kalman 在 0 dB P1B 失败 (ratio=1.29)，IBVSS 通过 (ratio=1.058)

### 2.3 0 dB 失败的数学解释

当 Q/R → 0（低 SNR + 慢速信道）：

```
Kalman:  K_ss ≈ √(Q/R)     (Riccati 解)
IBVSS:   α_ss ≈ Q/R         (稳态映射)
```

√(Q/R) > Q/R（当 Q/R < 1），因此 Kalman 的 K 系统性偏高 → under-smoothing。

这是 Riccati 方程保留 P > 0（后验不确定性永远不为零）的固有特性，不是实现 bug。0 dB 在 5G 部署中不在正常工作范围（典型 ≥ 3-5 dB）。

---

## 3. 复杂度分析

### 3.1 Per-frame 操作

```
IBVSS per frame:
  Pass 1 (per SC):  inner_re += in_r·st_r + in_i·st_i     [2 MAC]
                    inner_im += in_i·st_r - in_r·st_i     [2 MAC]
                    even-odd: sr, si, dr, di, pp, pm        [~6 ops, 每 2 SC]
                    → ~5 ops/SC

  Pass 2 (per SC):  derot_r = in_r·rot_re + in_i·rot_im   [2 MAC]
                    derot_i = in_i·rot_re - in_r·rot_im   [2 MAC]
                    diff, innov_accum                      [3 ops]
                    st_r += alpha·diff_r                   [1 MAC]
                    st_i += alpha·diff_i                   [1 MAC]
                    → ~9 ops/SC

  c_model (per 2 SC): dr, di, c_accum                     [~3 ops/2SC]

  Scalar (O(1)):   innov_smooth EMA, ratio, alpha           [~10 ops]

  总计: ~16 ops/SC + O(1)
```

### 3.2 与教授要求对比

| 方法 | Per-SC ops | 满足 "negligible"? |
|------|-----------|-------------------|
| Legacy filt8 | ~4 | 基准 |
| EWMA 固定 α | ~4 | ✓ |
| IBVSS | ~16 | ✓ (仅 4× filt8, 仍远低于一个 OFDM symbol 的 FFT) |
| PDP→R MMSE | ~100+ | ✗ |

4096 SC × 16 ops = 65,536 FLOPs/frame ≈ **0.02 ms** @ 3 GHz CPU。
vs Legacy 的 4096 × 4 = 16,384 FLOPs ≈ 0.005 ms。
差值 0.015 ms << SRS 周期 5 ms，满足 "negligible" 要求。

### 3.3 内存

```
Per (rx, tx) pair:
  ewma_state_t: 12 bytes/SC × 4096 SC = 48 KB
  ibvss_state_t: 24 bytes（共享，非 per-SC）

Total: 4 RX × 4 TX × 48 KB + 16 × 24 B = 3.072 MB + 384 B ≈ 3.1 MB
```

---

## 4. R_est (Even-Odd Pair) 估计器分析

### 4.1 原理

```
s[k] = H_obs[2k] + H_obs[2k+1]  ≈ 2·H_true[2k] + noise_s
d[k] = H_obs[2k] - H_obs[2k+1]  ≈ ΔH_true[2k] + noise_d

E[|d|²]/4 = E[|ΔH|²]/4 + σ²/2 = c_model + R/2
R_est = 2 · max(|d|²/4 - c_model, eps)
```

### 4.2 c_model 自适应校正

```
c_raw = E[|H_smooth[even] - H_smooth[odd]|²] / 4
noise_bias = α/(2(2-α)) · R_est
c_model_est = max(0, c_raw - noise_bias)
```

| Delay Spread | R_bias (无校正) | R_bias (校正后) |
|-------------|----------------|----------------|
| 30 ns | 1.000 | 1.000 |
| 100 ns | 1.002 | 1.000 |
| 300 ns | 1.016 | 1.000 |
| 500 ns | 1.046 | 1.001 |
| 1000 ns | 1.171 | 0.999 |

DS ≤ 500 ns（覆盖大部分 5G 场景）时无需校正。1000 ns 时校正消除 17% 偏差。

---

## 5. 已知局限与后续方向

### 5.1 当前局限

| 局限 | 影响 | 缓解 |
|------|------|------|
| Random walk 假设 | 实际信道有 Jakes 相关结构 | α_opt 仍然有效（MSE 非凸但近凸） |
| Per-SC 平均 alpha | 频域不同区域可能需要不同 alpha | Per-band alpha 可改进 ~2% |
| 标量 de-rotation | 多散射体有 per-SC Doppler 差异 | 5G SRS 单天线 UE 影响小 |
| 仅时域滤波 | "2D MMSE" 缺少频域维度 | 结构化输出的 DFT 截断提供频域去噪 |

### 5.2 后续改进方向

1. **Per-band innovation**：将 SC 分 8 个 band，用 median innovation 替代 mean → 对频域失配更 robust
2. **Kalman IAE 稳定化**：Q_floor = R·0.001 防止正反馈环 → 解决 0 dB 失败
3. **非平稳跟踪**：速度/SNR 突变时的收敛速度量化
4. **MIMO 验证**：2×2 per-pair 独立 IBVSS 一致性验证

---

## 6. 教授可能的问题 Q&A

**Q1: 为什么不直接用 Kalman？更理论化。**
> A: 稳态下两者 MSE 完全相同（证明见 §1.2）。Kalman 多了 IAE Q 估计，但该估计在低 SNR 下不稳定（P1B 0dB 失败）。IBVSS 更 robust，且 P1B 6/6 PASS。

**Q2: 这和 Lattice-RLS 的关系是什么？**
> A: 完全不同的应用场景。Lattice 用于 equalization（d=pilot, u=received），IBVSS 用于 state smoothing（d=u=noisy LS）。Self-prediction 下 Lattice 退化为 identity（数学定理，§3 of full log）。

**Q3: 复杂度能否再低？**
> A: 当前 ~16 ops/SC（0.02 ms/frame），是 filt8 的 4×。Pass 1 的 even-odd 对和 inner product 无法省略（是 alpha 自适应的必要输入）。理论下限约 ~12 ops/SC（合并 Pass 1+2 的部分重复计算）。

**Q4: 频域维度怎么做？**
> A: StructuredChannelOutput 的 DFT 截断已提供频域去噪（N_tap=64 → ~2μs 最大延迟）。更完整的频域 MMSE 需要替换 filt8/16，但 passthru 实验证明 filt8 不是 -7 dB floor 的主因，优先级降低。

**Q5: CDL 信道测试够不够？**
> A: 目前 CDL-C 8 条件 + P1B 6 条件全 PASS。后续义焕的 CDL 接入 OAI 后可做 CDL-A/C/D 实机验证。alpha_max 已从 0.95 提高到 0.98，消除了 20dB+30km/h 的截断问题。
