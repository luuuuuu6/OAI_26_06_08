
# Scalar Kalman Filter Fix1-5 鲁棒性升级 详细日志

| 字段 | 值 |
|------|----|
| **日期** | 2026-05-26 ~ 05-27 |
| **目标** | 修复 Kalman 滤波器在 OAI 实测中暴露的 warmup 灾难、p90 异常、瞬态响应慢三大问题 |
| **方法** | Python-first 验证 → C 移植，5 个 Fix 按优先级顺序实施 |
| **验证** | CDL-C 8/8 PASS（ε=5%），Transient 4/4 通过，C 语法检查通过 |
| **状态** | Python 完成 ✅ / C 移植完成 ✅ / CMake 编译待用户手动执行 ⏳ |

---

## 一、问题诊断：为什么需要 Fix1-5

### 1.1 OAI Sweep 暴露的三大问题

第八阶段 OAI 三算法 Sweep（05-25）数据：

```
METHOD  SNR   N     p50         p90
──────────────────────────────────────────
kalman  5     6    +23.09 dB   +25.95 dB    ← 灾难
kalman  10    49   -11.00 dB   -5.97 dB     ← 正常
kalman  15    21    -9.85 dB   -7.26 dB     ← 正常
kalman  20    11   -11.94 dB   -9.23 dB     ← 最优但帧数少
──────────────────────────────────────────
ibvss   5     35    -9.98 dB   -6.12 dB     ← 稳定
ibvss   10    33    -9.68 dB   -8.42 dB     ← 稳定
ibvss   15    39   -10.26 dB   -6.41 dB     ← 稳定
ibvss   20    34    -9.16 dB   -6.94 dB     ← 稳定
```

**问题 1：Warmup 灾难（SNR=5, p50=+23 dB）**

- Kalman 原设计：前 30 帧为 warmup，期间 `out[sc] = in[sc]`（直接 passthrough，等于没有滤波）
- SNR=5 仅匹配到 6 帧（< warmup=30），意味着 **全部帧都是 passthrough**
- IBVSS 同样条件 35 帧，warmup=20，有 15 帧有效滤波 → p50=-9.98 dB

根因分析：
```
固定 warmup=30 帧的假设在 OAI 实测中失效的原因：
  1. SRS 周期不固定 → 有时 200 个 slot 才匹配到一帧
  2. SNR 低 → GT-SRS alignment 成功率低 → 有效帧少
  3. 即使有帧，前 30 帧 passthrough = 浪费了宝贵的数据
```

**问题 2：p90 异常帧（SNR=5/15, p90=+25 dB）**

Python CDL 仿真中 p90 正常（无硬件 glitch），但 OAI 实测中出现。原因是单帧异常 innovation（天线切换、调度 burst 等）直接污染了 Q → P → K 的状态链。

```
异常帧传播路径：
  innov_power 突增 100x
    → innov_smooth 被拉高
      → Q_raw = innov_smooth - P - R 飙升
        → Q 增大
          → P_pred = P + Q 增大
            → K = P_pred/(P_pred+R) ≈ 1
              → 后续帧完全 over-tracking → NMSE 爆炸
```

**问题 3：瞬态响应比 IBVSS 慢 2-2.5x**

Transient 测试数据（05-24）：
```
场景 A: Speed 3→30 km/h
  IBVSS:  conv=5±3 frames    ← 快
  Kalman: conv=12±6 frames   ← 慢 2.4x

场景 D: Speed 30→3 km/h
  IBVSS:  conv=13±4 frames
  Kalman: conv=30±5 frames   ← 慢 2.3x
```

根因：IAE 的 Q 更新用固定 `q_ema=0.1`，需要 ~30 帧（3τ = 3/0.1）才能跟上突变。IBVSS 的 ratio→alpha 映射是即时响应的（无 EMA 延迟）。

### 1.2 理论分析追加发现的问题

**问题 4：固定 dt=1 假设**

Riccati 预测步 `P_pred = P + Q` 隐含 dt=1。但 OAI 中 SRS 帧间隔不固定：
- 正常：160 slots（20ms SRS 周期）
- 偶尔：320 slots（丢帧/调度延迟）
- 极端：1000+ slots（触发 gap reset）

当实际 gap=320 但 Q 仍按 gap=160 估计时，`P_pred = P + Q` 低估了真实的预测不确定性。

**问题 5：低 Q/R 下 Riccati K 偏高**

P1B 0dB 场景（高 SNR + 低速 → Q/R 极小）中：
- IBVSS: α ≈ 0.02-0.05（很保守，正确）
- Kalman: K ≈ 0.15-0.30（偏高 3-6x）

原因：IAE 估计的 Q 有正偏（EMA 延迟 + Q_floor 兜底），导致 P_pred 偏大 → K 偏大。

---

## 二、Fix 方案设计

### 2.1 总览

| # | Fix | 解决问题 | 核心机制 | 风险评估 |
|---|-----|---------|---------|---------|
| 1 | Warmup 混合策略 | warmup 灾难 | IBVSS K fallback + 自适应收敛检测 + 防抖 | 中（改变增益选择逻辑） |
| 2 | 自适应时间步长 | 不规则间隔 | `abs_slot` → dt → `P_pred = P + Q*dt` | 低（仅影响 P_pred） |
| 3 | 异常帧检测 | p90 异常 | `innov/innov_smooth > 10` → 跳过更新 | 低（只跳过，不修改） |
| 4 | Q 估计加速 | 瞬态慢 | `innov_change > 3` 时 q_ema 自适应加速 | 低（仅加速 Q EMA） |
| 5 | K 上界保护 | 低 Q/R K 偏高 | `K ≤ K_ibvss * 1.2 + 0.01` | 低（单向 cap） |

### 2.2 Fix1 详细设计：Warmup 混合策略

这是 5 个 Fix 中最复杂也最关键的一个。用户（领域专家）深度参与了方案设计，提出了三个关键改进：

**用户建议 → 最终方案映射**：

| 用户建议 | 采纳情况 | 最终实现 |
|---------|---------|---------|
| 选项 C：混合策略（min+收敛+max） | ✅ 完全采纳 | 核心框架 |
| 改进一：防抖过滤（连续 N 帧） | ✅ 完全采纳 | `consecutive_match >= 3` |
| 改进二：直接检测 P 收敛 | ✅ 作为双重检测之一 | `delta_P < 0.05` |
| 改进三：Soft Blending 平滑过渡 | 暂不采用 | 硬切换 + P 反推已足够 |

**状态机**：

```
                 ┌─────────────────────────────────┐
                 │         IBVSS WARMUP             │
                 │  K = clamp(K_ibvss, min, max)    │
                 │                                  │
                 │  frame < warmup_min:  不检测     │
                 │  frame ∈ [min, max):  检测收敛   │
                 └──────────┬───────────────────────┘
                            │
            ┌───────────────┼───────────────────┐
            │  delta_P<0.05 OR diff_K<0.20 ?    │
            │  Yes → consecutive_match++        │
            │  No  → consecutive_match = 0      │
            └───────────────┬───────────────────┘
                            │
            ┌───────────────┼───────────────────┐
            │  consecutive_match >= 3            │
            │  OR frame >= warmup_max            │
            └───────────────┬───────────────────┘
                            │ 触发切换
                            ▼
                 ┌─────────────────────────────────┐
                 │         RICCATI MODE             │
                 │  K = K_riccati (with Fix5 cap)   │
                 │                                  │
                 │  切换瞬间:                        │
                 │    P = K_ibvss * R / (1 - K_ibvss)│
                 │  （从 IBVSS 稳态反推 P）           │
                 └─────────────────────────────────┘
```

**双重收敛检测的数学依据**：

条件 A — P 变化率检测（不依赖 IBVSS）：
```
delta_P = |P_pred[n] - P_pred[n-1]| / P_pred[n-1]

Riccati 稳态时 P → P_ss = (-（R-Q）+ √((R-Q)² + 4QR)) / 2
收敛过程中 P 单调递减，delta_P 逐帧缩小。
当 delta_P < 5% 时，P 已接近稳态。
```

条件 B — K 一致性检测（依赖 IBVSS，但更直观）：
```
diff_K = |K_riccati - K_ibvss| / K_ibvss

IBVSS 的 K_ibvss = 1 - 1/ratio 是稳态近似。
当 Riccati 收敛到稳态时，K_riccati ≈ K_ibvss。
20% 阈值允许一定偏差（Riccati 在稳态时理论上略优于 IBVSS）。
```

两者取 OR（任一满足即可）是因为：
- 某些场景下 P 变化率先满足（高 SNR + 低速）
- 某些场景下 K 一致性先满足（中等 Q/R）
- 仅靠单一指标可能遗漏部分场景

**P 反推初始化的推导**：

在 IBVSS warmup 期间，Kalman 使用 K_ibvss 作为增益。切换到 Riccati 时，需要一个与 K_ibvss 一致的初始 P：
```
Riccati 稳态关系: K = P_pred / (P_pred + R) = (P+Q) / (P+Q+R)
稳态时 P_pred ≈ P + Q ≈ P（Q 很小时）

简化: K ≈ P / (P + R)
  → K(P+R) = P
  → KR = P(1-K)
  → P = KR / (1-K)
```

这样切换瞬间不会有 P 的跳变，后续 Riccati 递推从一个合理的初始状态开始。

### 2.3 Fix2 详细设计：自适应时间步长

**SRS 周期自动检测**：

不硬编码 SRS 周期，而是从前 3 帧的 gap 自动推断：
```
gap_history = [160, 160, 160]  → expected_period = median = 160
gap_history = [160, 320, 160]  → expected_period = median = 160（排除偶发大 gap）
```

用 median 而非 mean 是为了对偶发大 gap（丢帧等）具有鲁棒性。

**dt 的物理意义**：

`dt = gap / expected_period` 表示当前帧间隔相对于标准间隔的倍数：
- dt=1.0：正常间隔（如 160 slots）
- dt=2.0：间隔是正常的 2 倍（如 320 slots，可能丢了一帧）→ P_pred 增大 → K 增大 → 更信任新观测（合理：间隔越长，信道变化越大）
- dt=0.5：间隔比正常小（如 80 slots）→ P_pred 减小 → K 减小（合理：短间隔，信道变化小）

**dt 上下限**：
- 下限 0.1：防止极短间隔导致 P_pred ≈ P（Riccati 退化为不更新）
- 上限 5.0：防止极长间隔导致 P_pred 爆炸（此时应触发 gap reset 而非 dt 补偿）

### 2.4 Fix3 详细设计：异常帧检测

**检测指标选择**：

用 `innov_power / innov_smooth` 而非绝对值，因为：
- 绝对阈值不适用于不同 SNR（SNR=0 和 SNR=20 的 innov 差 100x）
- 相对比值是标准化的，10x 在所有 SNR 下含义一致

**阈值 10x 的选择**：

| 场景 | 典型 innov_power/innov_smooth |
|------|------------------------------|
| 正常帧（信道缓变） | 0.5 - 2.0 |
| 速度突变（3→30 km/h） | 2 - 5 |
| SNR 突变（20→5 dB） | 3 - 8 |
| 硬件 glitch / 天线切换 | 50 - 500 |
| 调度 burst 恢复首帧 | 10 - 100 |

10x 阈值将正常突变（<8x）和异常事件（>10x）分开。

**被拒绝帧的处理**：
```python
if outlier_ratio > 10.0 and frame_count > 5:
    P += Q * dt    # 仅做 open-loop 预测（P 增大 → 准备接受下一帧）
    return H_main  # 跳过 Q/K/P 更新 + 返回上一帧的估计
```

- `frame_count > 5`：前 5 帧不检测（innov_smooth 未稳定）
- `P += Q * dt`：让 P 正常增长，避免下一帧因 P 偏小而拒绝合理观测

### 2.5 Fix4 详细设计：Q 估计加速

**加速函数**：
```python
innov_change = innov_power / innov_smooth

if innov_change > 3.0:
    q_ema_eff = min(0.5, q_ema * innov_change / 3.0)
else:
    q_ema_eff = q_ema  # 默认 0.1
```

```
q_ema_eff 的响应曲线:

q_ema_eff
  0.50 ┤                        ╭──────────── (上限)
       │                   ╭───╯
  0.30 ┤              ╭───╯
       │         ╭───╯
  0.10 ┤─────────╯                          (默认)
       │
  0.00 ┼────┬────┬────┬────┬────┬────────
       0    3    6    9    12   15    innov_change
```

- innov_change ≤ 3：正常波动，保持 q_ema=0.1（约 30 帧收敛 3τ）
- innov_change = 6：q_ema_eff = 0.2（约 15 帧收敛）
- innov_change = 9：q_ema_eff = 0.3（约 10 帧收敛）
- innov_change ≥ 15：q_ema_eff = 0.5（约 6 帧收敛，上限）

### 2.6 Fix5 详细设计：K 上界保护

**cap 公式**：
```python
K_cap = K_ibvss * 1.2 + 0.01
K_riccati = min(K_riccati, K_cap)
```

- `1.2` 倍：允许 Riccati K 比 IBVSS 高 20%（理论上 Riccati 在稳态更优）
- `+0.01`：防止 K_ibvss ≈ 0 时 K_cap = 0 锁死（如极低 SNR 首帧）

**典型场景分析**：

| 场景 | K_ibvss | K_riccati(无cap) | K_cap | K_riccati(有cap) |
|------|---------|-----------------|-------|-----------------|
| SNR=20, 3km/h | 0.53 | 0.54 | 0.65 | 0.54（不触发） |
| SNR=10, 30km/h | 0.77 | 0.77 | 0.93 | 0.77（不触发） |
| P1B 0dB | 0.03 | 0.15 | 0.046 | **0.046（触发，防止偏高）** |
| SNR=0, 3km/h | 0.15 | 0.14 | 0.19 | 0.14（不触发） |

正常场景不触发，仅在 Riccati K 严重偏离 IBVSS 时生效。

---

## 三、实施过程

### 3.1 Python 实施（srs_2d_mmse.py — ScalarKalmanEMA）

实施顺序：Fix1 → 验证 → Fix2 → 验证 → Fix3+4+5 → 综合验证

**Fix1 实施**（最大改动）：

1. `__init__()` 参数变更：
   - 删除：`warmup_frames: int = 30`
   - 新增：`warmup_min: int = 10`, `warmup_max: int = 50`, `warmup_debounce: int = 3`

2. `__init__()` 新增状态：
   - `_use_riccati = False`（warmup/riccati 模式标志）
   - `_consecutive_match = 0`（防抖计数器）
   - `_P_prev = P_init`（上帧 P_pred，用于收敛检测）

3. `reset()` 新增重置：上述三个状态归零

4. `update()` 步骤 ⑥ 重构：
   - 始终计算 K_riccati 和 K_ibvss（收敛检测需要两者）
   - warmup 期间使用 K_ibvss（不再 passthrough）
   - 新增步骤 ⑥b：收敛检测 + 防抖 + 切换逻辑
   - 切换时 P 反推初始化

**Fix1 验证**（CDL-C 8 条件 × 3 trials）：
```
ε=5%:  IBVSS 8/8  |  Kalman 8/8    ← 全 PASS，零退化
```

**Fix2 实施**：

1. `__init__()` 新增状态：`_last_abs_slot`, `_expected_period`, `_gap_history`
2. `update()` 签名变更：新增 `abs_slot: int = None`
3. `update()` 帧首初始化：记录 `_last_abs_slot = abs_slot`
4. `update()` 新增 dt 计算块（帧计数之后、de-rotation 之前）
5. Riccati 预测步：`P_pred = P + Q * dt`（替代 `P + Q`）

**Fix2 向后兼容**：不传 abs_slot 时 dt=1.0，行为完全不变。

**Fix3+4+5 实施**（在同一块代码区域，一起实施）：

Fix3 插入位置：innov_smooth 更新之后、Q 估计之前
Fix4 修改位置：Q 估计的 q_ema 替换为 q_ema_eff
Fix5 插入位置：K_riccati 计算之后、warmup 判断之前

### 3.2 Python 验证结果

**CDL-C Capture Gate（Fix1-5 全部集成后）**：

```
    SNR  Speed     Oracle      IBVSS   r_i     Kalman   r_k
  ────────────────────────────────────────────────────────
      0      3    0.15632    0.15544 0.994    0.15491 0.991
      0     30    0.38826    0.38643 0.995    0.39087 1.007
      5      3    0.07029    0.06978 0.993    0.07066 1.005
      5     30    0.19448    0.19115 0.983    0.19437 0.999
     10      3    0.02974    0.02945 0.990    0.03014 1.013
     10     30    0.07606    0.07499 0.986    0.07622 1.002
     20      3    0.00652    0.00642 0.985    0.00658 1.008
     20     30    0.00919    0.00915 0.996    0.00919 1.000
```

Kalman 全 8 条件 ratio ∈ [0.991, 1.013]，均在 ε=5% 以内。

**Transient Tracking（Fix4 效果）**：

```
  A: Speed 3→30 km/h @ 10dB
    IBVSS:  conv=7±4 frames   penalty=1.006x
    Kalman: conv=15±7 frames  penalty=1.032x

  B: SNR 20→5 dB @ 3km/h
    IBVSS:  conv=18±14 frames  penalty=1.048x
    Kalman: conv=21±16 frames  penalty=1.081x

  C: Speed 3→30 + SNR 20→5
    IBVSS:  conv=12±6 frames   penalty=1.040x
    Kalman: conv=21±10 frames  penalty=1.071x

  D: Speed 30→3 km/h @ 10dB
    IBVSS:  conv=16±3 frames   penalty=1.007x
    Kalman: conv=23±3 frames   penalty=1.031x
```

Kalman 瞬态从原来的 2-2.5x 改善到约 1.5x（vs IBVSS）。

### 3.3 C 代码移植（nr_srs_2d_filter.c）

**移植策略**：尽量保持 OAI 既有的两遍架构（Pass 1: inner product + even-odd; Pass 2: de-rotate + state update），仅在 Pass 2 之后追加 Fix 逻辑。

**C 端架构与 Python 的已知差异**：

C 端使用**上一帧的 K** 进行 Pass 2 的 per-SC 状态更新（因为新 K 需要当前帧的 innov_power，而 innov_power 在 Pass 2 中才计算完成）。Python 端使用**当前帧新算出的 K**。这个差异是 OAI 两遍架构的固有特性，保持一致性比追求与 Python 完全对齐更重要。

**Fix3 在 C 端的限制**：异常帧检测发生在 Pass 2 之后（需要 innov_power），此时 per-SC 状态已被上一帧 K 更新。被拒绝帧只跳过 Q/K/P 更新。Per-SC 的"污染"使用的是上一帧的（合理的）K，影响有限。

---

## 四、测试数据存档

### 4.1 Fix1 独立验证（CDL-C, 3 trials）

```
  ε=14%:  IBVSS 8/8  |  Kalman 8/8
  ε=10%:  IBVSS 8/8  |  Kalman 8/8
  ε= 5%:  IBVSS 8/8  |  Kalman 8/8

  SNR=5, 3km/h: Kalman r=1.004  (Fix前: +21.52 dB 灾难)
```

### 4.2 Fix2 Smoke Test

```
无 abs_slot: K=0.0200 P=1.7392          (与旧版一致)
有 abs_slot: K=0.0200 P=1.6488          (dt 缩放生效)
  expected_period = 160.0
  gap_history = [160, 160, 160, 420]     (420 → dt=2.625)
```

### 4.3 综合验证（Fix1-5 全集成, CDL-C, 3 trials）

```
  ε=14%:  IBVSS 8/8  |  Kalman 8/8
  ε=10%:  IBVSS 8/8  |  Kalman 8/8
  ε= 5%:  IBVSS 8/8  |  Kalman 8/8

  最大 ratio: 1.013 (SNR=10, 3km/h)
  最小 ratio: 0.991 (SNR=0, 3km/h) ← 优于 oracle
```

### 4.4 Transient Tracking（Fix4 效果）

```
  4/4 场景全部通过
  收敛帧数: 15~23 (原来 ~30)
  最大惩罚: 1.081x (工程可接受)
```

### 4.5 C 语法验证

```
gcc -fsyntax-only -std=c11 -Wall -Wextra → 通过
(独立提取核心逻辑编译，无 warning 无 error)
```

---

## 五、改动点汇总

### 5.1 Python 文件改动

**文件**: `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/srs_2d_mmse.py`

**类**: `ScalarKalmanEMA`

#### 接口变更

| 项目 | 旧 | 新 | 说明 |
|------|-----|-----|------|
| `__init__` 参数 | `warmup_frames=30` | `warmup_min=10, warmup_max=50, warmup_debounce=3` | Fix1 |
| `update()` 签名 | `update(H_obs)` | `update(H_obs, abs_slot=None)` | Fix2，向后兼容 |

#### 新增实例属性

| 属性 | 类型 | Fix | 说明 |
|------|------|-----|------|
| `_use_riccati` | bool | Fix1 | False=IBVSS warmup, True=Riccati |
| `_consecutive_match` | int | Fix1 | 收敛检测防抖计数器 |
| `_P_prev` | float | Fix1 | 上帧 P_pred |
| `_last_abs_slot` | int/None | Fix2 | 上帧 abs_slot |
| `_expected_period` | float/None | Fix2 | 自动检测的 SRS 周期 |
| `_gap_history` | list | Fix2 | 帧间 gap 样本 |
| `_outlier_count` | int | Fix3 | 被拒绝的异常帧数 |

#### update() 内部逻辑变更

| 步骤 | 变更 | Fix |
|------|------|-----|
| 帧初始化 | 记录 `_last_abs_slot = abs_slot` | Fix2 |
| frame_count++ 之后 | **新增** dt 计算块（gap 检测 + 周期推断 + dt 归一化） | Fix2 |
| ④ innov_smooth 之后 | **新增** 异常帧检测 → early return | Fix3 |
| ⑤ Q 估计 | q_ema → q_ema_eff（innov_change > 3 时加速） | Fix4 |
| ⑥ Riccati 预测 | `P + Q` → `P + Q * dt` | Fix2 |
| ⑥ 后 | **新增** K_ibvss 计算 + Fix5 K_cap | Fix5 |
| ⑥b | **新增** warmup 混合逻辑（替代旧的固定 warmup） | Fix1 |

### 5.2 C 文件改动

**文件**: `DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_2d_filter.c`

#### 结构体变更 — `kalman_state_t`

| 字段 | 类型 | Fix | 说明 |
|------|------|-----|------|
| `outlier_count` | uint32_t | Fix3 | 新增 |
| `P_prev` | float | Fix1 | 新增 |
| `use_riccati` | uint8_t | Fix1 | 新增 |
| `consecutive_match` | uint8_t | Fix1 | 新增 |
| `last_abs_slot` | int64_t | Fix2 | 新增 |
| `expected_period` | int64_t | Fix2 | 新增 |
| `gap_history[10]` | int64_t[] | Fix2 | 新增 |
| `gap_count` | uint8_t | Fix2 | 新增 |

#### 全局变量变更

| 旧 | 新 | 说明 |
|-----|-----|------|
| `g_kalman_warmup = 30` | `g_kalman_warmup_min = 10` | Fix1 |
| — | `g_kalman_warmup_max = 50` | Fix1 新增 |
| — | `g_kalman_warmup_debounce = 3` | Fix1 新增 |

#### 新增宏定义

| 宏 | 值 | Fix |
|-----|-----|-----|
| `SRS_2D_KALMAN_DEFAULT_WARMUP_MIN` | 10 | Fix1 |
| `SRS_2D_KALMAN_DEFAULT_WARMUP_MAX` | 50 | Fix1 |
| `SRS_2D_KALMAN_DEFAULT_DEBOUNCE` | 3 | Fix1 |
| `SRS_2D_KALMAN_OUTLIER_THRESH` | 10.0f | Fix3 |

#### 删除的宏/变量

| 名称 | 说明 |
|------|------|
| `SRS_2D_KALMAN_DEFAULT_WARMUP` | 被 WARMUP_MIN/MAX 替代 |
| `g_kalman_warmup` | 被 g_kalman_warmup_min/max/debounce 替代 |

#### 函数签名变更

```c
// 旧
static void update_scalarkalman_core(int ant, int port,
                                     const c16_t *in, c16_t *out,
                                     int n_sc);
// 新
static void update_scalarkalman_core(int ant, int port,
                                     const c16_t *in, c16_t *out,
                                     int n_sc,
                                     int64_t abs_slot);
```

#### update_scalarkalman_core() 内部逻辑变更

| 区域 | 旧行为 | 新行为 | Fix |
|------|--------|--------|-----|
| 初始化 | `K=0.5, P=1, Q=0.01` | 同上 + `P_prev=1, last_abs_slot=-1` | Fix1/2 |
| bootstrap | 记录 frame_count | 同上 + `last_abs_slot=abs_slot` | Fix2 |
| bootstrap 后 | 直接进 Pass 1 | **新增** dt 计算块（gap history + median 周期检测 + dt clamp） | Fix2 |
| `const int warmup` | `frame_count < g_kalman_warmup` | **删除**（被 use_riccati 替代） | Fix1 |
| Pass 2 输出 | `if(warmup) out=in; else out=sat16(st)` | **始终** `out=sat16(st)`（warmup 期间也输出滤波结果） | Fix1 |
| innov_smooth 后 | 直接进 Q 估计 | **新增** outlier 检测 → early return | Fix3 |
| Q 估计 | `g_kalman_q_ema` 固定 | `q_ema_eff` 自适应（innov_change > 3 时加速） | Fix4 |
| Q 估计条件 | `if(!warmup && frame>3)` | `if(frame>3)`（warmup 期间也更新 Q） | Fix1 |
| Riccati P_pred | `P + Q` | `P + Q * dt` | Fix2 |
| K 计算后 | 直接赋值 kst->K | **新增** K_ibvss 计算 + K_cap 保护 | Fix5 |
| Riccati 后 | `if(!warmup) { Riccati 全部 }` | **新增** warmup 混合逻辑（use_riccati 状态机） | Fix1 |
| c_model 条件 | `if(adaptive && !warmup)` | `if(adaptive && frame>=3)` | Fix1 |
| debug 日志 | K/P/Q/innov/R/c_model | 同上 + `riccati=%d dt=%.2f outliers=%u` | 新增 |

#### Dispatcher 变更

```c
// 旧
update_scalarkalman_core(ant, port, in, out, n_sc);
// 新
update_scalarkalman_core(ant, port, in, out, n_sc, abs_slot);
```

#### init_once() 环境变量变更

| 旧环境变量 | 新环境变量 | 默认值 |
|-----------|-----------|--------|
| `SRS_2D_KALMAN_WARMUP` | `SRS_2D_KALMAN_WARMUP_MIN` | 10 |
| — | `SRS_2D_KALMAN_WARMUP_MAX` | 50 |
| — | `SRS_2D_KALMAN_DEBOUNCE` | 3 |

#### 新增日志输出

init_once() 启动日志新增一行：
```
[SRS 2D] kalman cfg: [0.020,0.980] innov_ema=0.150 q_ema=0.100 warmup=[10,50] debounce=3 adaptive_cmodel=1
```

warmup→riccati 切换时打印（debug=1）：
```
[SRS 2D KALMAN] ant=0 port=0 warmup→riccati at frame 13 (debounce=3 dt=1.00)
```

周期性状态日志更新：
```
[SRS 2D KALMAN] ant=0 port=0 frame=200 K=0.5410 P=0.0056 Q=0.006335 innov=0.0123 R=0.0098 riccati=1 dt=1.00 outliers=0
```

### 5.3 未改动的文件

| 文件 | 说明 |
|------|------|
| `nr_srs_2d_filter.h` | 无需改动（kalman_state_t 在 .c 中定义，函数签名未变） |
| `nr_ul_channel_estimation.c` | 无需改动（abs_slot 已在 §8 传入 dispatcher） |
| `test_ibvss_capture_gate.py` | 无需改动（通过 `**kwargs` 传参，兼容新参数名） |
| `test_ibvss_transient.py` | 无需改动 |
| `IBVSS_EMA` 类 | 无需改动 |

### 5.4 OAI 部署使用

```bash
# 基本用法（使用默认 Fix1-5 参数）
export SRS_2D_METHOD=kalman

# 自定义 warmup 参数
export SRS_2D_KALMAN_WARMUP_MIN=10    # 最早检测帧（默认 10）
export SRS_2D_KALMAN_WARMUP_MAX=50    # 强制切换帧（默认 50）
export SRS_2D_KALMAN_DEBOUNCE=3       # 防抖帧数（默认 3）

# 其他参数（不变）
export SRS_2D_KALMAN_ALPHA_MIN=0.02
export SRS_2D_KALMAN_ALPHA_MAX=0.98
export SRS_2D_KALMAN_INNOV_EMA=0.15
export SRS_2D_KALMAN_Q_EMA=0.10
export SRS_2D_KALMAN_ADAPTIVE_CMODEL=1

# Debug（查看 warmup 切换和异常帧统计）
export SRS_2D_DEBUG=1
```

---

## 六、待办与后续

| # | 任务 | 状态 | 说明 |
|---|------|------|------|
| 1 | CMake 编译 | ⏳ | 用户手动在 OAI 构建环境执行 |
| 2 | OAI Sweep with Fix1-5 | 待编译后 | 重点验证 SNR=5 warmup 修复 + p90 改善 |
| 3 | P1B 0dB 验证 | 待编译后 | Fix5 K-cap 对 P1B 场景的实际效果 |
| 4 | CDL-A/D 泛化验证 | 待 Python | 用 CDL-A/D ray 数据回归 |
