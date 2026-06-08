# IBVSS 实现日志 — 2026-05-23

## 背景

上周完成了 Phase 3 的 2D adaptive alpha（`rot_rate + even-odd SNR → α = a1·rr + a2·snr + a3`），
在 CDL-C 上校准通过，但在 P1B ray-tracing 信道下**泛化失败**——线性映射假设不适用于
复杂信道模型。教授在 05-22 会议中提出新要求：

1. SRS 估计需要提取**结构化信道信息**（PDP, 空间协方差, SVD, RSRP, Doppler, SNR）
2. 估计后的 H 需要**压缩存储**（DFT 截断 / low-rank SVD）
3. 整体服务于 Digital Twin 数据管线

本次工作目标：将 adaptive alpha 从 Phase 3 的预校准线性映射升级为 Phase 4 的
**IBVSS（Innovation-Based Variable Step Size）**——无需校准、理论自洽、跨信道泛化。

---

## 一、理论推导：Kalman 稳态映射

### 1.1 问题定义

EMA 滤波器跟踪随机游走信道：
```
H_true[n] = H_true[n-1] + w[n],   w ~ CN(0, Q)    (过程噪声)
H_obs[n]  = H_true[n] + v[n],     v ~ CN(0, σ²)   (观测噪声)
```

EMA 更新：`H_smooth[n] = H_smooth[n-1] + α · (H_derot[n] - H_smooth[n-1])`

Innovation: `e[n] = H_derot[n] - H_smooth[n-1]`

### 1.2 稳态 Innovation 方差

从 Kalman 滤波器 Riccati 方程的稳态解：
```
P_pred = Q/α           (预测误差方差)
E[|e|²] = P_pred + σ² = Q/α + σ²    (innovation 方差)
```

定义 `ratio = E[|e|²] / σ²`，从 Riccati 稳态条件 `α² + α·(Q/σ²) = Q/σ²` 可得：
```
Q/σ² = α²/(1-α)
ratio = Q/(α·σ²) + 1 = α/(1-α) + 1 = 1/(1-α)
```

### 1.3 核心公式

**因此 α 和 ratio 的关系是**：
```
ratio = 1/(1-α)   ⟹   α = 1 - 1/ratio
```

其中：
- `ratio = innov_smooth / R_est`
- `innov_smooth` = innovation 功率的 EMA 平滑值
- `R_est` = even-odd pair 噪声估计

**自洽性验证**：
- 3 km/h, 10 dB: oracle α* = 0.36 → 预期 ratio = 1/(1-0.36) = 1.56 → α = 1-1/1.56 = 0.36 ✓
- 30 km/h, 10 dB: oracle α* = 0.78 → 预期 ratio = 1/(1-0.78) = 4.55 → α = 1-1/4.55 = 0.78 ✓

### 1.4 与 Phase 3 的本质区别

| 特性 | Phase 3 (AdaptiveAlphaEMA) | Phase 4 (IBVSS_EMA) |
|------|--------------------------|---------------------|
| α 映射 | `α = a1·rr + a2·snr + a3` (线性) | `α = 1 - 1/ratio` (Kalman) |
| 校准 | 需要 CDL 数据拟合 a1, a2, a3 | **零校准** |
| 特征 | rot_rate (闭环), SNR (开环) | innov/R_est 比值 (全开环) |
| 泛化 | CDL 通过, P1B 失败 | 理论上跨信道通用 |
| 超参数 | 5个 (a1, a2, a3, rr_ema, snr_ema) | 3个 (α_min, α_max, innov_ema) |

---

## 二、Python 实现 (srs_2d_mmse.py)

### 2.1 IBVSS_EMA 类

文件：`DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/srs_2d_mmse.py`

在 `AdaptiveAlphaEMA` 之后新增 `IBVSS_EMA` 类。每帧流水线：

```
① inner product → de-rotation (与 Phase 3 相同)
② adaptive c_model from H_smooth (频率选择性补偿)
③ even-odd pair → R_est 噪声估计 (用 c_model 校正)
④ innovation_power = mean |H_derot - H_smooth|²
⑤ innov_smooth EMA → ratio → α = clip(1 - 1/ratio)
⑥ EMA 更新: H_smooth += α · innovation
⑦ 自适应 c_model 更新 (扣除 H_smooth 残余噪声)
```

关键设计决策：
- `warmup_frames=20`：前 20 帧保持 α_init=0.5 不做自适应，让 H_smooth 收敛
- `innov_ema=0.15`：innovation 平滑系数，比 Phase 3 的 rr_ema=0.05 快，因为 IBVSS 不需要慢速特征提取
- `adaptive_cmodel=True`（默认开启）：用 H_smooth 在线估计 c_model

### 2.2 第一次尝试失败：乘法步进设计

最初设计是：
```python
if ratio_smooth > ratio_hi:     # ratio_hi = 1.2
    alpha *= step_up             # step_up = 1.05
elif ratio_smooth < ratio_lo:   # ratio_lo = 0.9
    alpha *= step_down           # step_down = 0.95
```

**失败原因**：ratio 在稳态时不是 ~1，而是 1/(1-α)。对于 α=0.36（3km/h），
ratio 约 1.56，远大于 ratio_hi=1.2，导致 alpha 一路爬升到 0.95 ceiling。

测试结果：
```
3 km/h:  ratio=2.867, α=0.950 (oracle=0.361)  — 严重 under-smoothing
30 km/h: ratio=1.156, α=0.950 (oracle=0.781)  — 也超标
```

### 2.3 修正：直接 Kalman 映射

改为直接映射后立即通过：
```python
alpha_raw = 1.0 - 1.0 / max(ratio, 1.001)
self.alpha = clip(alpha_raw, alpha_min, alpha_max)
```

---

## 三、CDL Capture Gate 验证

### 3.1 CDL-C 数据生成

由于 Sionna 不可用，编写了 `gen_cdl_rays_standalone.py`，从 3GPP TR 38.901
Table 7.7.1-4 直接生成 CDL-C ray 参数（480 rays = 24 clusters × 20 sub-rays）。

### 3.2 单点快速验证 (SNR=10dB, 3/30 km/h)

```
[PASS]   3 km/h | oracle=0.02974  IBVSS=0.02918 (r=0.981, α=0.303)
[PASS]  30 km/h | oracle=0.07606  IBVSS=0.07491 (r=0.985, α=0.781)
```

**两个条件 ratio < 1.0** — IBVSS 比 oracle 固定 alpha 还好（帧自适应带来的增益）。

### 3.3 全 SNR × 速度 Sweep (5 trials)

```
==============================================================================
  RESULT: 8/8 conditions PASS (ε=14%)

    SNR   Speed     Oracle      IBVSS  r_ibvss        Ph3    r_ph3  Gate
  ────────────────────────────────────────────────────────────────────────
      0      3    0.15345    0.15157    0.988    0.16387    1.068  PASS
      0     30    0.38987    0.38958    0.999    0.41599    1.067  PASS
      5      3    0.07156    0.07063    0.987    0.08423    1.177  PASS
      5     30    0.19555    0.19214    0.983    0.19677    1.006  PASS
     10      3    0.02965    0.02917    0.984    0.03551    1.198  PASS
     10     30    0.07843    0.07722    0.985    0.07855    1.002  PASS
     20      3    0.00621    0.00605    0.973    0.00794    1.278  PASS
     20     30    0.00955    0.00961    1.007    0.00961    1.007  PASS
==============================================================================
```

关键观察：
- IBVSS 在 **7/8 条件下优于 oracle**（ratio < 1.0）
- Phase 3 在 20dB+3km/h 下 **ratio=1.278 FAIL**（α_conv=0.817 远超 oracle=0.610）
- IBVSS 的 α 自动跟踪 oracle α*：低 SNR 给小 alpha（多平均），高 SNR 给大 alpha（快跟踪）

---

## 四、频率选择性鲁棒性验证 (P1B 模拟)

### 4.1 问题

Even-odd pair 噪声估计 `d = H_obs[even] - H_obs[odd]` 假设相邻 SC 信道近似相同。
如果信道频率选择性很强（大 delay spread），`|d|²` 会包含信道变化分量，导致 R_est 偏高。

### 4.2 R_est bias vs delay spread

通过增大 CDL-C delay spread 模拟 P1B：

```
   DS(ns)    c_model  R_bias_raw  R_bias_cor      α*  α_ibvss0  α_ibvssC
  ──────────────────────────────────────────────────────────────────────────
       30   0.000032      1.0000      0.9998  0.3606    0.2952    0.2953
      100   0.000362      1.0021      1.0003  0.3606    0.3002    0.3011
      300   0.003317      1.0164      1.0003  0.3606    0.3084    0.3164
      500   0.008958      1.0461      1.0006  0.3606    0.2772    0.2995
     1000   0.036098      1.1707      0.9994  0.3606    0.2305    0.3041
```

- DS ≤ 300ns：R_est bias < 2%，无需校正
- DS = 1000ns（P1B 级别）：R_est 偏高 17%，无校正时 α 偏低 36%（0.231 vs oracle 0.361）

### 4.3 自适应 c_model

**问题**：H_smooth 有残余噪声，直接用 `mean|H_smooth[even] - H_smooth[odd]|²` 会严重
高估 c_model（0.042 vs 真值 0.000091 — 差 460 倍）。

**解决**：EMA 稳态时 H_smooth 的 per-SC 噪声方差为 `α/(2-α)·σ²`，因此：
```
c_raw = mean|H_smooth[even] - H_smooth[odd]|² / 4
noise_bias = α / (2·(2-α)) · R_est
c_model_est = max(0, c_raw - noise_bias)
```

### 4.4 校正后结果

```
      DS      c_true       c_est      α*   α_fix   α_adp    r_fix    r_adp       Δ
  ──────────────────────────────────────────────────────────────────────────────
     100    0.000091    0.000451  0.3606  0.2992  0.3030    0.985    0.983   +0.2%
     300    0.000789    0.000713  0.3606  0.2995  0.3088    0.995    0.987   +0.9%
     500    0.002184    0.002236  0.3606  0.2745  0.2975    1.022    0.990   +3.1%
    1000    0.008795    0.007013  0.3719  0.2534  0.3268    1.213    1.002  +17.4%
    1500    0.019778    0.021257  0.3719  0.1874  0.3103    1.617    1.012  +37.4%
```

- DS=1000ns：从 r=1.213（FAIL）到 r=**1.002**（完美 PASS）
- DS=1500ns：从 r=1.617 到 r=**1.012**
- 正常 delay spread 下无负面影响

---

## 五、OAI C 侧实现

### 5.1 修改文件

- `openair1/PHY/NR_ESTIMATION/nr_srs_2d_filter.h`：
  - 新增 `SRS_2D_METHOD_IBVSS = 3` 枚举
  - 更新文档说明

- `openair1/PHY/NR_ESTIMATION/nr_srs_2d_filter.c`：
  - 新增 `ibvss_state_t` 结构体（alpha, innov_smooth, c_model_est, frame_count）
  - 新增 `update_ibvss_core()` 函数（~200 行）
  - 新增 IBVSS 环境变量解析和日志输出
  - 在方法调度中添加 ibvss 分支

### 5.2 C 代码架构

```
update_ibvss_core(ant, port, in, out, n_sc):
  ├─ bootstrap first frame → copy in to state, return
  ├─ Pass 1: inner product + even-odd pairs (single loop)
  │   └─ compute rot_re, rot_im, R_est (with c_model_est correction)
  ├─ alpha = ist->alpha  (使用上一帧的 innov_smooth 计算的值)
  ├─ Pass 2: de-rotate + innovation + EMA update (single loop)
  │   ├─ H_derot = H_obs * conj(rot)
  │   ├─ innovation = H_derot - H_smooth
  │   ├─ innov_accum += |innovation|²
  │   └─ H_smooth += alpha * innovation
  ├─ update innov_smooth (EMA of innov_power)
  ├─ update alpha for next frame: α = clip(1 - 1/ratio)
  └─ adaptive c_model from updated H_smooth (partial loop)
```

### 5.3 使用方式

```bash
export SRS_2D_METHOD=ibvss
export SRS_2D_DEBUG=1           # 可选：每 200 帧输出日志
# 可选超参数调整：
export SRS_2D_IBVSS_ALPHA_INIT=0.5
export SRS_2D_IBVSS_ALPHA_MIN=0.02
export SRS_2D_IBVSS_ALPHA_MAX=0.95
export SRS_2D_IBVSS_INNOV_EMA=0.15
export SRS_2D_IBVSS_WARMUP=20
export SRS_2D_IBVSS_ADAPTIVE_CMODEL=1   # 默认开启
```

### 5.4 注意事项

- alpha 使用**上一帧**的 innov_smooth 计算（one frame delay），由于 EMA 平滑，
  影响可忽略，但避免了需要 3-pass 的问题
- inner_abs2 过小时 fallback 到 EWMA（与 Phase 3 相同的防护逻辑）
- 自适应 c_model 只在 warmup 之后启用，且使用噪声偏置校正

---

## 六、结构化输出层

### 6.1 StructuredChannelOutput 类

文件：`srs_2d_mmse.py`，在 IBVSS_EMA 之后。

接收 IBVSS 的 H_smooth，提取：

| 输出 | 说明 | 用途 |
|------|------|------|
| `rsrp` | 参考信号接收功率 | 路径损耗估计 |
| `snr_db` | 信噪比 (from even-odd) | 链路质量 |
| `doppler_hz` | 多普勒频移 | 移动速度估计 |
| `speed_ms` | UE 速度 (m/s) | Sensing |
| `pdp` | 截断功率延迟谱 (N_tap,) | 多径结构 |
| `ds_rms_s` | RMS 延迟扩展 | 信道建模参数 |
| `R_rx` | RX 空间协方差 (MIMO) | 波束赋形 |
| `sv` | 奇异值谱 (MIMO) | 秩/条件数 |
| `H_c` | DFT 截断压缩 H | 存储 / AI 训练 |

### 6.2 DFT 压缩原理

```
H_obs (n_sc) → IFFT → h_time (n_sc)
                       ├─ keep h_time[0:N_tap]     (有用多径)
                       ├─ keep h_time[-N_tap:]      (非因果部分)
                       └─ zero  h_time[N_tap:-N_tap] (噪声)
                    → FFT → H_c (n_sc, 去噪/压缩)
```

默认 N_tap=64，对于 30kHz SCS + 4096 FFT，对应 ~2μs 最大延迟。

---

## 七、文件清单

### 新增文件
| 文件 | 说明 |
|------|------|
| `test_ibvss_capture_gate.py` | IBVSS capture gate 多 SNR/速度验证脚本 |
| `test_ibvss_freq_selectivity.py` | R_est bias vs delay spread 诊断 |
| `test_ibvss_adaptive_cmodel.py` | 自适应 c_model 效果对比 |
| `gen_cdl_rays_standalone.py` | 3GPP CDL-C ray 参数生成（无 Sionna 依赖） |
| `data_out/cdl_c_30kmh/*.npy` | 生成的 CDL-C 480 ray 参数文件 |
| `data_out/ibvss_capture_gate_results.json` | capture gate 完整结果 |

### 修改文件
| 文件 | 改动 |
|------|------|
| `srs_2d_mmse.py` | +IBVSS_EMA 类, +StructuredChannelOutput 类 |
| `nr_srs_2d_filter.h` | +SRS_2D_METHOD_IBVSS 枚举, +IBVSS env 文档 |
| `nr_srs_2d_filter.c` | +ibvss_state_t, +update_ibvss_core(), +env解析 |

---

## 八、待办 / 下一步

1. **编译验证**：在 Docker 中编译 OAI，确认 C 代码无语法错误
2. **RF Simulator A/B 测试**：`SRS_2D_METHOD=ewma` vs `ibvss`，用 `eval_pdpR_nmse.py` 评估
3. **P1B 信道实测**：确认自适应 c_model 在真实 ray-tracing 信道下的表现
4. **准秀合作**：将 structured output (S) 的格式与 Junsu 的 DL PMI 工作对齐
5. **教授汇报**：准备 block diagram（IBVSS 流水线 + 结构输出层）

---

## 九、关键数字速查

```
IBVSS 核心公式：  α = 1 - 1/(innov_smooth / R_est)
CDL Capture Gate： 8/8 PASS, worst ratio = 1.007 (20dB, 30km/h)
Phase 3 对比：     20dB+3km/h Ph3 ratio=1.278 FAIL, IBVSS=0.975 PASS
频选补偿：         DS=1000ns r: 1.213→1.002 (adaptive c_model)
C 代码行数：       ~200 行 (update_ibvss_core)
超参数：           α_min=0.02, α_max=0.95, innov_ema=0.15, warmup=20
```
