# 2D MMSE Phase 1 实验日志 — 2026-05-14

## 目标

在 4 mul-add/SC 预算内实现 2D MMSE 信道估计器（频域 3-tap boxcar + 时域 EMA），
用已有静态信道数据验证效果，为 C 移植和动态信道实验做准备。

## 产出文件

| 文件 | 说明 |
|------|------|
| `G1C.../srs_2d_mmse.py` | 滤波器模块：SRS2DFilter (C 风格) + SRS2DFilterVec (向量化) |
| `G1C.../test_2d_mmse.py` | 测试脚本 v2：split-set GT, outlier 检测, α=0.01~1.0 |
| `G1C.../data_out/2d_mmse_results/fig1_*.png` | NMSE vs frame (SNR=20, 4 条曲线) |
| `G1C.../data_out/2d_mmse_results/fig2_*.png` | Steady-state NMSE vs SNR |
| `G1C.../data_out/2d_mmse_results/fig3_*.png` | α sweep (SNR=20) |

（`G1C...` = `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy`）

## 数据

`data_out/2d_mmse_input/` 下 5 个 NPZ：

- `H_snr{10,20,30,40}dB_static_seed42.npz` — 静态信道 (speed=0), seed=42
- `H_snr20dB_sto_fixed.npz` — STO 补偿版 (未使用)
- Shape: `(N_frames, 2, 2, 2048)` complex128, `active_sc` 1248 个索引

## 诊断发现

### P0: SNR=40 时钟跳变

最后 4 帧 (96-99) STO 从 ~4 跳到 ~20 样本，NMSE 飙到 +3.2 dB。

- 根因：采集时 timing sync 异常，非 int16 问题
- 处理：test_2d_mmse.py 自动检测 STO > 6 样本的帧并移除
- 去除后 SNR=40 经验 SNR = 17.3 dB，与 SNR=30 (17.2 dB) 一致

### "int16 ceiling" 假象

之前误判为 -31 dB 硬 ceiling，实际是 split-set eval 帧数限制：

- 50 帧 eval → 平均增益 10·log₁₀(50) ≈ 17 dB
- single-frame ~-14 dB + 17 dB gain ≈ -31 dB ← 恰好对上
- Test 1 验证：SNR=20 cumulative oracle 在 90 帧时达 **-50.38 dB**
- Test 3 验证：SNR=20 理论 100 帧 oracle = -36.8 dB

**结论**：无硬 ceiling。int16 的真实影响在 single-frame 层面
（经验 SNR cap ~17 dB for SNR ≥ 20），但多帧平均持续有效。

### Boxcar 有害

3-tap boxcar 在无噪 GT 上的 NMSE = -28.62 dB（不可消除 bias）。
信道频率选择性 (roughness=0.016) 使得相邻 SC 平均引入系统误差。

- 所有 SNR 点：EMA+Box 比纯 EMA 差 ~2 dB
- 结论：**去掉 boxcar，算法定型为纯 time-EMA**（预算降为 2 ops/SC）

## Phase 1 结果 (split-set GT, α=0.1)

| SNR | Raw | EMA only | EMA+Box | Oracle | EMA gain |
|-----|-----|----------|---------|--------|----------|
| 10 | -6.52 | -17.01 | -16.78 | -16.71 | +10.5 dB |
| 20 | -12.30 | -25.28 | -23.44 | -32.55 | +13.0 dB |
| 30 | -20.66 | -27.65 | -25.06 | -30.93 | +7.0 dB |
| 40 | -22.73 | -29.79 | -26.53 | -30.51 | +7.1 dB |

### α sweep (SNR=20, split-set)

| α | EMA only | EMA+Box |
|---|----------|---------|
| 0.01 | -25.99 | -24.66 |
| 0.02 | -28.66 | -25.98 |
| **0.05** | **-29.07** | -25.68 |
| 0.10 | -25.28 | -23.44 |
| 0.30 | -19.74 | -19.19 |

最优 α=0.05 是 50 帧 eval 窗口的产物（等效窗口=20 帧 vs 50 帧 budget）。

## 关键 implication

EMA 在静态信道下离 Oracle **远** (gap ~21 dB when oracle uses 90 frames)。
之前看到 "EMA 接近 Oracle" 是两者都撞 eval 窗口限制的假象。

EMA 相对 uniform avg 的价值在于**动态信道的自动遗忘**。
静态信道无法验证这一点 → Phase 2 动态数据是完整故事的必要条件。

## Phase 2 规划

### 数据需求

用 `run_q4_snr_sweep_v8.sh` 生成动态信道数据：

| Speed | SNR 点 | Frames | 目的 |
|-------|--------|--------|------|
| 0 m/s | 10,20,30 | 300 | 已有 (seed42), 可重采补充帧数 |
| 3 m/s | 10,20,30 | 300 | 低速移动，α trade-off 起点 |
| 10 m/s | 10,20,30 | 300 | 中速移动 |
| 30 m/s | 10,20,30 | 300 | 高速移动，coherence time ~3.3ms |

### 预期发现

- α sweep 在动态信道下出现 U 形最优（非单调）
- 不同 speed 下最优 α 不同
- EMA vs uniform avg 在高速下 EMA 反超

### 需验证

- Fix 1 (STO 补偿) 在动态信道下是否安全
  - 对比：不做 STO 补偿 vs 做 STO 补偿 的 correlation
  - 若补偿后更差 → Fix 1 需要 slope 幅度限制

## 终端命令

见文件末尾 "启动命令" 章节。

---

## 启动命令：dynamic channel sweep

### 前置条件

```bash
# 确认 docker、5GC、sionna-proxy 容器正常
docker ps | grep -E "sionna|oai"
```

### Speed = 3 m/s (低速)

```bash
cd /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy

sudo SNR_POINTS="10 20 30" \
     MAX_FRAMES=300 \
     CHANNEL_SEED=42 \
     UE_SPEED=3 \
     bash run_q4_snr_sweep_v8.sh
```

### Speed = 10 m/s (中速)

```bash
sudo SNR_POINTS="10 20 30" \
     MAX_FRAMES=300 \
     CHANNEL_SEED=42 \
     UE_SPEED=10 \
     bash run_q4_snr_sweep_v8.sh
```

### Speed = 30 m/s (高速)

```bash
sudo SNR_POINTS="10 20 30" \
     MAX_FRAMES=300 \
     CHANNEL_SEED=42 \
     UE_SPEED=30 \
     bash run_q4_snr_sweep_v8.sh
```

### 采集后处理（一条命令搞定）

`prepare_2d_mmse_data.py` 自动完成：SRS bin 加载 → STO 补偿 → GT 对齐 → 标准 NPZ。

```bash
cd /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy

# 处理整个 sweep 目录（指定 speed）
python3 prepare_2d_mmse_data.py \
    --sweep-dir /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/logs/q4_sweep_YYYYMMDD_HHMMSS \
    --speed 3 --seed 42

# 或处理单个 SNR 目录
python3 prepare_2d_mmse_data.py \
    --run-dir .../logs/q4_sweep_.../snr_20dB \
    --snr 20 --speed 3 --seed 42
```

输出到 `data_out/2d_mmse_input/`，命名规则：
- 静态: `H_snr{X}dB_static_seed{S}.npz`
- 动态: `H_snr{X}dB_speed{V}ms_seed{S}.npz`

NPZ 包含统一键名：`H_sto_compensated`, `H_gt` (动态), `active_sc`, `snr_dB`, `ue_speed`, ...

### 分析出图

```bash
# 跑所有可用数据（自动发现静态+动态）
python3 test_2d_mmse.py

# 只跑特定 speed
python3 test_2d_mmse.py --speed 3

# 指定 SNR 和 alpha
python3 test_2d_mmse.py --speed 3 --snr 20 --alpha 0.1
```

`test_2d_mmse.py` 自动区分静态/动态：
- 静态 → split-set GT (帧平均), oracle = cumul. avg
- 动态 → 逐帧 Sionna GT, oracle = 滑动窗口 avg (W=2/α)

### 完整三步流程

```
Step 0:  sudo ... bash run_q4_snr_sweep_v8.sh    # 采集 (~15-45min/speed)
Step 1:  python3 prepare_2d_mmse_data.py ...      # 后处理 (~30s/sweep)
Step 2:  python3 test_2d_mmse.py --speed X        # 出图 (~2s)
```
