# 完整工作总结 — 2D MMSE 信道估计器 (2026-05-14 ~ 05-15)

## 一、项目目标

在 4-6 ops/SC 预算内实现 2D MMSE 信道估计器（时域 EMA + 可选频域 boxcar + phase de-rotation），
用真实 OAI rfsim 数据验证效果，最终移植到 OAI C 代码。

---

## 二、产出文件清单

| 文件 | 说明 |
|------|------|
| `G1C.../srs_2d_mmse.py` | 滤波器模块: SRS2DFilter (C风格) + SRS2DFilterVec (向量化) + AdaptiveSRS2DFilter (v7) |
| `G1C.../test_2d_mmse.py` | 测试脚本: 支持静态/动态, split-set GT, per-pair LS, α sweep, SNR sweep |
| `G1C.../prepare_2d_mmse_data.py` | 数据管线: sweep目录 → STO补偿 → GT对齐 → 标准NPZ |
| `G1C.../data_out/2d_mmse_input/*.npz` | 静态4点 + P1B speed=3 3点 = 7个数据集 |
| `G1C.../data_out/2d_mmse_results/*.png` | 所有实验图 |
| `EXPERIMENT_LOG_20260514_2D_MMSE.md` | 实验日志 (含启动命令和流程) |

(`G1C...` = `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy`)

---

## 三、Phase 1 — 静态信道验证 (完成)

### 数据

- 4个 SNR 点 (10/20/30/40 dB), 静态 (speed=0), seed=42
- Shape: (100 frames, 2rx, 2tx, 2048 SC), 1248 active SCs

### 主要结果 (split-set GT, α=0.1)

| SNR | Raw NMSE | EMA only | EMA gain |
|-----|----------|----------|----------|
| 10 dB | -6.5 dB | -17.0 dB | +10.5 dB |
| 20 dB | -12.3 dB | -25.3 dB | +13.0 dB |
| 30 dB | -20.7 dB | -27.7 dB | +7.0 dB |
| 40 dB | -22.7 dB | -29.8 dB | +7.1 dB |

### 关键发现

1. **频域 Boxcar 有害**: 3-tap boxcar 引入 -28.6 dB bias floor (信道频率选择性太强)
   → 结论: 去掉 boxcar, 算法定型为纯时域 EMA (2 ops/SC)

2. **SNR=40 时钟跳变**: 最后 4 帧 STO 从 ~4 跳到 ~20 样本
   → 自动检测并移除 (STO > 6 样本的帧)

3. **无 int16 硬 ceiling**: 之前误判的 -31 dB "ceiling" 是 split-set 帧数限制
   → 验证: cumulative oracle 在 90 帧时达 -50.38 dB (SNR=20)

4. **α sweep 最优 α=0.05** (50帧 eval 下): 非单调, 有真正的最优点
   → α 太小收敛不够, α 太大平均不充分

---

## 四、Phase 2 — 动态信道实验

### 数据生成

- 用 `run_q4_snr_sweep_v8.sh` + `UE_SPEED=3` 生成
- 3 SNR 点 (10/20/30), P1B 信道, 85-94 配对帧

### 诊断链

#### P0: Fix 1 (STO补偿) 验证
- 结论: Fix 1 **有效** (+5.8 dB NMSE 改善, angle std 降低)
- 假说"Fix 1 在动态下过度补偿" 排除

#### P1: GT 帧间相关性
- 发现: **GT corr = 0.999960 (所有 lag)** → GT 几乎不变
- 初步判断: "speed=3 没生效"

#### P2: Attach-stable 冻结诊断
- 发现: handoff 在 proxy log line 14268/14277 (跑完前 9 行才解冻)
- 全部数据在 300s 冻结期内采集
- 修法: 设 `ATTACH_STABLE_SEC=0` 重跑

#### P3: 关闭冻结后仍然 GT corr=1.0
- 结论: **不是冻结的问题**, 是信道模型本身

#### P4: 信道演化机制深挖
- 找到 Doppler 公式: `exp(j * 2π/λ * (r̂·v) * t)` 在 `channel_coefficients_JIN.py`
- 帧间差分验证: **信道确实在变** (100°/frame 相位旋转)
- 但去除全局旋转后 NMSE = -40.8 dB → **纯 phase rotation, 零 shape drift**

### 根因定位

**P1B ray 角度扩展极窄 → 所有 ray 共享几乎相同的 Doppler**
→ 信道退化为: 固定 shape × exp(j·ω·t) 全局旋转

验证: 多起点平均 de-rotated NMSE 在 5ms ~ 2500ms lag 上全部 -39 ~ -43 dB flat

### Phase rotation 速率异常

| Sweep | 实测旋转率 | 理论 3m/s 最大 |
|-------|-----------|-------------|
| freeze300 | 51 Hz | 35 Hz |
| nofreeze | 56 Hz | 35 Hz |

实测 > 理论最大值, 可能 velocity magnitude > 3 m/s 或 lambda_0 参数有误差。

---

## 五、Adaptive α 算法探索

### 设计演化

| 版本 | 改动 | 静态结果 | P1B结果 | 状态 |
|------|------|---------|---------|------|
| v3 | 16-probe innov, 乘法α调节 | FAIL (α runaway) | — | 弃 |
| v4 | 直接映射 (无乘法) | FAIL (α高) | OK | 弃 |
| v5 | p90 baseline + running max | α→min, 但static SNR=10差 | 很好(-12dB) | 部分 |
| v6 | 3dB derot gate | 同v5 | 同v5 | 部分 |
| v7 | full-SC innov + 5dB gate + bidirectional baseline | REGRESS (反馈环) | 不如v5 | 弃 |

### 核心困难

**Innovation-based adaptive α 在 bursty SRS noise 下 baseline 估计不稳定**:
- α ↔ H_smooth ↔ innovation 正反馈环
- SRS noise 有 heavy tail (STO spike帧), median baseline 被击穿
- De-rotation 在静态低 SNR 下误触发 (16-probe 估计不够准, full-SC 也有反馈问题)

### 当前最佳结果 (v5/v6, 非adaptive但含de-rotation)

| 场景 | Fixed α=0.05 | Adaptive (de-rotation) | Δ |
|------|-------------|----------------------|---|
| Static SNR=30 | -30.2 dB | -29.9 dB | +0.3 (OK) |
| P1B sp3 SNR=10 | -1.2 dB | -12.2 dB | **-11.0** |
| P1B sp3 SNR=20 | -1.9 dB | -12.6 dB | **-10.7** |
| P1B sp3 SNR=30 | -1.6 dB | -12.9 dB | **-11.2** |

**De-rotation 本身在有 Doppler 信道上给出 10+ dB 增益**, 是验证成功的。

---

## 六、架构级发现

### SRS 周期实际 = 5ms (不是 80ms)

- OAI conf: `periodicity = 10` (slots) = sl10 = 5 ms
- 离线数据的 160-slot 间隔是 **dump 采样率限制**, 非 SRS 测量间隔
- OAI 内部每 5ms 做一次 SRS 信道估计

### P1B 信道特性

- 极窄 angular spread → Doppler 退化为纯 global phase rotation
- Shape 在 5s 内完全不变 (-40 dB de-rotated NMSE, flat)
- 对教授管线下游 (PDP, R_H, eigenstructure) 无影响 (全部 phase-invariant)

### Sionna 版本

- `sionna==1.0.2`, namespace = `sionna.phy.*`
- CDL/TDL class 可用性待验证 (仓库内未使用)

---

## 七、下一步方向 (未执行)

### 确定需要做的

1. **Plan B (Dual-EMA)**: 用独立 reference EMA 打破 innovation-α 反馈环
2. **CDL 信道模型接入**: 通过 3GPP 标准 ray 参数表注入现有 Rays() 接口
3. **多场景验证**: CDL-A/C/D × speed × SNR 生成 ~20 数据点
4. **离线 dump 频率**: 需要提高才能让 30 km/h 数据在离线分析中有意义

### Go/No-Go Gate

**Exp 1 (fixed α scan)**: 在多 scenario 上找各自最优 α
- 如果最优 α 有 spread (0.01 ~ 0.3) → adaptive 有研究价值
- 如果全聚在 0.01-0.05 → SRS dump 间隔太长, 需要先解决 dump 频率

---

## 八、代码当前状态

- `srs_2d_mmse.py`: 含 SRS2DFilter + SRS2DFilterVec + AdaptiveSRS2DFilter (v7, 有已知问题)
- `test_2d_mmse.py`: 完整测试框架, 支持 `--speed` 自动区分静态/动态
- `prepare_2d_mmse_data.py`: 端到端数据管线, sweep → NPZ
- 静态数据结果稳定可重现, 动态数据分析管线完整但 GT 对齐需 LS alignment
