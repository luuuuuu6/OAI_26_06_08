# SRS 自适应频域去噪 (Adaptive FreqSmooth) 实装日志

| 字段 | 值 |
|------|----|
| **日期** | 2026-05-28 |
| **前提** | 0528 NMSE Floor 根因分析 → Stage1 残留 per-SC 噪声是 NMSE -10~-11 dB 底板的主因 |
| **目标** | 实现 delay-spread-aware 自适应频域平滑，flat 场景 NMSE 改善 +5~7 dB |
| **方法** | Phase 1 Python 离线验证 → Phase 2 OAI C 嵌入 (v1→v2) → Phase 3 MMSE1D 窗口修复 → Phase 4 Kalman 叠加 |
| **最终实测** | **FreqSmooth+Kalman: -17.39 dB (+6.52 dB vs Legacy -10.87 dB)** |

---

## 一、根因回顾

来自 `0528日志.SRS_NMSE_Floor_RootCause.md` 的核心结论：

| 指标 | 值 | 含义 |
|------|-----|------|
| P1B RX98 RMS delay spread | 1.04 ns | 近似 flat-fading |
| 相干带宽 / NR 带宽 | 5.1x | 整个 SRS 带宽内信道几乎不变 |
| GT smoothness_ratio | 0.00000004 | 完美平坦 |
| SRS (Kalman) smoothness_ratio | 0.0086 | 比 GT 粗糙 20 万倍 |

五组对照实验 NMSE 排名违反直觉：

```
Kalman (-11.16) > Legacy (-10.87) > OptFilt (-10.15) > MMSE1D (-9.66)
```

所有算法都被 Stage1 per-SC 噪声的天花板卡住。解决方向：**频域去噪**。

---

## 二、Phase 1：离线 Python 验证

### 2.1 新建脚本

**文件**: `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/eval_freq_smooth.py`

### 2.2 Legacy 数据验证结果

**数据**: `logs/q4_sweep_20260528_150304/snr_15dB/`（401 帧）

```
GT  roughness: 6.73e-08 (median)
SRS roughness: 0.0098 (median)
SRS noise_est: 0.0098 (median)
ratio rough/noise: 1.009          ← 完美确认：全是噪声
```

| 模式 | NMSE | 改善 |
|------|------|------|
| raw (无平滑) | -10.87 dB | baseline |
| mavg9 | -12.90 dB | +2.03 dB |
| mavg33 | -16.19 dB | +5.32 dB |
| mavg65 | -17.28 dB | +6.41 dB |
| mavg129 | -18.35 dB | +7.47 dB |
| mavg513 | -23.35 dB | +12.48 dB |
| full-band avg | -45.13 dB | +34.25 dB |

### 2.3 Kalman 数据叠加验证

**数据**: `logs/q4_sweep_20260528_151732/snr_15dB/`（359 帧）

| 模式 | NMSE | 改善 |
|------|------|------|
| Kalman raw | -11.16 dB | baseline |
| Kalman + mavg65 | -17.41 dB | +6.25 dB |
| Kalman + adaptive | -23.45 dB | +12.29 dB |

**结论**：频域平滑与时域 Kalman 是**正交**的。

---

## 三、Phase 2：OAI C 代码嵌入（v1 → v2 演进）

### 3.1 v1：固定阈值表（已废弃）

roughness 绝对值查阈值表选窗口。

**问题**：filt8 输出的 roughness (~0.01) 落在 mavg17~33 区间，被阈值卡住。

**实测**：-15.14 dB（+4.27 dB）— 未达目标。

### 3.2 v2：roughness / noise ratio 自适应（当前版本）

**核心洞察**：不应看 roughness 绝对值，而应看**频域变化中噪声占了多少比例**。

同时计算两个指标：

| 指标 | 公式 | 捕获内容 |
|------|------|---------|
| roughness (1st-order) | `Σ|H[k+1]-H[k]|² / Σ|H[k]|²` | 信号 + 噪声 |
| noise_est (2nd-order Laplacian) | `Σ|H[k+2]-2H[k+1]+H[k]|² / Σ|H[k]|²` | 几乎只有噪声 |

ratio = roughness / noise_est：

| ratio | 含义 | 窗口 |
|-------|------|------|
| ≈ 1.0 | 频域变化全是噪声 | 最大窗口 (mavg129) |
| ≈ 2.0 | 一半噪声一半信号 | 中等窗口 (mavg33) |
| ≥ 5.0 | 大部分真实频选 | 最小窗口 (mavg3) |

窗口映射：`log2(ratio)` 线性映射到窗口表 `[mavg3 .. mavg129]`。

**优势**：无需手动标定阈值，自动适应。

**实测**：ratio 稳定在 1.29~1.46，自动选 mavg65 → **-17.27 dB (+6.40 dB)**。

---

## 四、Phase 3：MMSE1D 窗口修复

| 参数 | 修改前 | 修改后 |
|------|--------|--------|
| `SRS_MMSE_MAX_WINDOW` | 8 | **64** |
| `SRS_MMSE_DEFAULT_WINDOW` | 4 | **16** |

---

## 五、Phase 4：FreqSmooth + Kalman 叠加

### 5.1 架构：级联两步处理

```
Stage1 filt8/16 (频域 FIR 插值)
  → srs_est[] (全带密集估计)
    → [Stage 1.5] nr_srs_freq_smooth() in-place (自适应频域 MA)
      → [Stage 2] nr_srs_2d_filter_update(kalman) (标量时域 Kalman)
        → srs_estimated_channel_freq[][] (最终输出)
```

### 5.2 注入安全性分析

| 检查项 | 结果 |
|--------|------|
| `srs_est[]` 生命周期 | 每次 (ant, port) 迭代独立 memset+填充，in-place 修改安全 |
| Kalman `in` 参数 | `const c16_t *`（只读），收到更干净的输入 |
| 下游 noise 计算 | 用 Kalman 的 `out`，不受 `srs_est` 修改影响 |
| Kalman 内部状态 | innovation 变小是好事，Q/R 估计更准 |
| 向后兼容 | `nr_srs_freq_smooth_enabled()` 检查 `SRS_FREQ_SMOOTH_MAX_WIN` 环境变量是否显式设置 |

### 5.3 控制方式

| 启动配置 | 行为 |
|---------|------|
| `SRS_ESTIMATOR=mmse2d SRS_2D_METHOD=kalman` | 纯 Kalman（不变，向后兼容） |
| `SRS_ESTIMATOR=mmse2d SRS_2D_METHOD=kalman SRS_FREQ_SMOOTH_MAX_WIN=129` | **FreqSmooth + Kalman** |
| `SRS_ESTIMATOR=freqsmooth SRS_FREQ_SMOOTH_MAX_WIN=129` | 纯 FreqSmooth |

### 5.4 实测结果

**数据**: `logs/q4_sweep_20260528_172149/snr_15dB/`（336 帧）

```
[SRS Estimator] mode=2d-filter
[SRS FreqSmooth] max_win=129 debug=1
[SRS 2D] method=kalman
[SRS 2D KALMAN] ant=0 port=0 warmup→riccati at frame 12

frame=1   rough=0.184983 noise=0.141753 ratio=1.30 win=65
frame=51  rough=0.017421 noise=0.013433 ratio=1.30 win=65
frame=101 rough=0.010181 noise=0.007331 ratio=1.39 win=65
frame=151 rough=0.011986 noise=0.008873 ratio=1.35 win=65
frame=201 rough=0.014447 noise=0.010632 ratio=1.36 win=65
```

**NMSE per-antenna = -17.39 dB (+6.52 dB vs Legacy)**

### 5.5 Kalman 边际贡献分析

| 模式 | NMSE | Kalman 增量 |
|------|------|------------|
| FreqSmooth alone | -17.27 dB | — |
| FreqSmooth + Kalman | -17.39 dB | **+0.12 dB** |

Kalman 叠加在本场景下只贡献了 0.12 dB。原因：

1. **flat + 低速 (3 m/s)**：信道时域变化极小，Kalman 的时域平滑几乎无用
2. **FreqSmooth 已压制大部分噪声**：Kalman 看到的 innovation 很小，K → 0（信任历史）
3. **gap reset**：gnb.log 显示 `gap 840 slots, resetting`，Kalman 状态被重置一次，重新 warmup 拉低了整体效果

**Kalman 的真正价值在高速/频选场景**：UE 速度高→信道时变快→时域滤波才有增量。

---

## 六、全量实测对比表

| # | 模式 | run timestamp | 配置 | NMSE | p50 | vs Legacy |
|---|------|--------------|------|------|-----|-----------|
| 1 | Legacy | `150304` | filt8 only | **-10.87 dB** | -11.32 | baseline |
| 2 | Kalman alone | `151732` | filt8 → Kalman | -11.16 dB | -11.58 | +0.29 dB |
| 3 | MMSE1D | `154103` | PDP→R Wiener (win=4) | -9.66 dB | -10.04 | -1.21 dB |
| 4 | FreqSmooth v1 | `163304` | filt8 → 固定阈值MA, max=65 | -15.14 dB | -15.92 | +4.27 dB |
| 5 | FreqSmooth v2 | `170005` | filt8 → ratio自适应MA, max=129 | -17.27 dB | -17.92 | +6.40 dB |
| 6 | **FreqSmooth v2 + Kalman** | **`172149`** | **filt8 → ratioMA → Kalman** | **-17.39 dB** | **-17.94** | **+6.52 dB** |

---

## 七、术语澄清：当前方案 vs 真正的 2D MMSE

| 项目 | 当前实现 | 理论 2D MMSE |
|------|---------|-------------|
| 频域处理 | 自适应 moving average (roughness/noise ratio) | Wiener 滤波 (需要 R(Δf) 频域相关) |
| 时域处理 | 标量 Kalman (IAE 自适应 Q) | Wiener 滤波 (需要 R(Δt) 时域相关) |
| 联合优化 | **否**。级联两步独立处理 | **是**。联合 R(Δf,Δt) 二维最优 |
| 计算复杂度 | O(N×win) + O(N) per frame | O(N²) 或 FFT-based O(N·logN) |
| 实际效果 | -17.39 dB (flat, SNR=15) | 理论上更优，但实现复杂度高 |

代码中 `SRS_ESTIMATOR=mmse2d` 的命名是历史遗留——"2D"指"有 Stage2 时域滤波"，不代表数学意义上的 2D MMSE。当前方案更准确的描述是：

```
级联自适应估计器 = 频域 adaptive MA + 时域 scalar Kalman
```

在 flat 场景下，频域 MA 接近最优（真值近似常数→均值就是最佳估计），所以实测效果已很好。在频选场景下，MA 不如 Wiener 最优，但自适应窗口缩小可避免严重退化。

---

## 八、变更文件清单

| 文件 | 操作 | 变更内容 |
|------|------|----------|
| `eval_freq_smooth.py` | **新建** | Phase 1 离线验证脚本 |
| `nr_srs_mmse.h` | 修改 | +`NR_SRS_EST_FREQSMOOTH=7` + `nr_srs_freq_smooth()` 声明 |
| `nr_srs_mmse.c` | 修改 | +mode 解析 + `MMSE_MAX_WINDOW` 8→64 + `DEFAULT_WINDOW` 4→16 |
| `nr_srs_freq_smooth.h` | **新建** | 头文件 + `nr_srs_freq_smooth_enabled()` |
| `nr_srs_freq_smooth.c` | **新建** | ratio 自适应核心 + MA + `_enabled()` 查询 |
| `nr_ul_channel_estimation.c` | 修改 | +FreqSmooth 独立分支 + MMSE2D 内 FreqSmooth 预处理 |
| `CMakeLists.txt` | 修改 | +`nr_srs_freq_smooth.c` |
| `launch_all_v9.sh` | 修改 | +`SRS_FREQ_SMOOTH_*` / `SRS_MMSE_WINDOW` 环境变量转发 |
| `preflight.sh` | 修改 | 5GC 从检查改为强制 `docker compose down+up` |

---

## 九、使用方法

### 9.1 推荐配置（FreqSmooth + Kalman）

```bash
sudo SRS_ESTIMATOR=mmse2d SRS_2D_METHOD=kalman \
     SRS_FREQ_SMOOTH_MAX_WIN=129 \
     SRS_FREQ_SMOOTH_DEBUG=1 \
     ONLY_SNR="15" MAX_FRAMES=300 CHANNEL_SEED=42 \
     bash run_q4_snr_sweep_v9.sh
```

### 9.2 纯 FreqSmooth（无时域滤波）

```bash
sudo SRS_ESTIMATOR=freqsmooth \
     SRS_FREQ_SMOOTH_MAX_WIN=129 \
     ONLY_SNR="15" MAX_FRAMES=300 CHANNEL_SEED=42 \
     bash run_q4_snr_sweep_v9.sh
```

### 9.3 纯 Kalman（向后兼容，不设 FREQ_SMOOTH 变量）

```bash
sudo SRS_ESTIMATOR=mmse2d SRS_2D_METHOD=kalman \
     ONLY_SNR="15" MAX_FRAMES=300 CHANNEL_SEED=42 \
     bash run_q4_snr_sweep_v9.sh
```

### 9.4 离线评估

```bash
python3 eval_nmse_clean.py  --run-dir logs/.../snr_15dB
python3 eval_freq_smooth.py --run-dir logs/.../snr_15dB --adaptive --full-band
```

---

## 十、后续方向

| 方向 | 说明 | 优先级 |
|------|------|--------|
| 多场景验证 | 频选信道 (CDL-A/C) + 高速 (30/120 km/h) 验证自适应泛化性 | 高 |
| 真正的频域 MMSE | 替换 MA 为 Wiener 滤波（利用 PDP→R(Δf)），已有 mmse1d 基础 | 中 |
| 真正的 2D MMSE | 联合 R(Δf,Δt) 优化，需要较大架构改造 | 低（长期） |
| MA O(N) 优化 | 当前 O(N×win) → cumsum 差分 | 中 |
| AVX2 SIMD | int16 向量化 MA kernel | 低（生产化） |
| UL Pre-Gain 联合优化 | g=4 提升 SQNR → FreqSmooth 效果更好 | 中 |

---

## 十一、数据归档

| 用途 | 路径 |
|------|------|
| Phase 1 验证脚本 | `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/eval_freq_smooth.py` |
| Legacy baseline | `DevChannelProxyJIN/logs/q4_sweep_20260528_150304/snr_15dB/` |
| Kalman alone | `DevChannelProxyJIN/logs/q4_sweep_20260528_151732/snr_15dB/` |
| MMSE1D | `DevChannelProxyJIN/logs/q4_sweep_20260528_154103/snr_15dB/` |
| FreqSmooth v1 max=65 | `DevChannelProxyJIN/logs/q4_sweep_20260528_163304/snr_15dB/` |
| FreqSmooth v2 ratio 自适应 | `DevChannelProxyJIN/logs/q4_sweep_20260528_170005/snr_15dB/` |
| **FreqSmooth v2 + Kalman** | **`DevChannelProxyJIN/logs/q4_sweep_20260528_172149/snr_15dB/`** |
| C 核心实现 | `openair1/PHY/NR_ESTIMATION/nr_srs_freq_smooth.c` |
| 根因分析日志 | `0528日志.SRS_NMSE_Floor_RootCause.md` |
