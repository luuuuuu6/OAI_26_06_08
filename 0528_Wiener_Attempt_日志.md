# SRS 频域 Wiener/LMMSE 尝试日志

| 字段 | 值 |
|------|----|
| **日期** | 2026-05-28 ~ 05-29 |
| **前提** | 0528 FreqSmooth 实装完成（MA+Kalman = -17.39 dB），计划升级为真正的频域 Wiener/LMMSE |
| **结论** | **C 端 delay-domain shrinkage 方案失败。离线验证发现 oracle-PDP Wiener 可达 -38~-45 dB，但 data-PDP Wiener 因 noisy PDP 估计无法使用。delay Top-K 硬截断在 flat 场景达 -45 dB 但非 Wiener。下一步需多帧 PDP 平均。** |

---

## 一、背景与动机

来自 `0528_Adaptive_FreqSmooth_实装日志.md` 的结论：

- MA+Kalman 在 P1B flat 场景达到 -17.39 dB（+6.52 dB vs Legacy）
- MA 是等权矩形窗，不是最优滤波器
- ALDLC 要求事项中将"2D 可分离 MMSE（频域 Wiener + 时域 Wiener）"列为 2026 Q3 目标
- WSSUS 可分离性理论保证：频域 MMSE + 时域 MMSE 的级联 = 联合 2D MMSE

因此启动频域 MA → Wiener 的升级。

---

## 二、理论分析

### 2.1 WSSUS 可分离性

在 WSSUS 假设下 `R(Δf, Δt) = R_f(Δf) · R_t(Δt)`，2D MMSE 可分解为级联：
- Stage 1.5：频域 MMSE（利用 R_f）
- Stage 2：时域 MMSE（利用 R_t，即 Kalman）

当前架构（级联 FreqSmooth + Kalman）在结构上是正确的，只需把 MA 升级为 Wiener。

### 2.2 频域 Wiener 公式

delay-domain Wiener shrinkage：

```
h_delay = IFFT(h_freq)           # 频域 → delay域
gain[n] = PDP[n] / (PDP[n] + σ²) # per-tap Wiener 增益
h_est = FFT(h_delay × gain)      # delay域 → 频域
```

其中 PDP 是功率延迟谱，σ² 是 per-tap 噪声功率。

### 2.3 现有 MMSE1D 代码

`nr_srs_mmse.c` 已有 PDP→R→Toeplitz→Gauss Jordan 的完整骨架，但实测 -9.66 dB（比 Legacy 还差），原因：
- 窗口过小（默认 win=4）
- noisy LS 直接估 PDP → R(Δk) 被噪声污染
- per-target SC 局部解小矩阵

---

## 三、C 端实现尝试（失败）

### 3.1 第一版：delay-domain shrinkage

**实现位置**：`nr_srs_freq_smooth.c` 内新增 `wiener_delay_shrink_inplace()`

**方案**：
1. 把 active SRS band 填入 2048 全网格（inactive SC 置零）
2. OAI `idft()` 变换到 delay domain
3. 远尾 [3N/4, N) 的 P25 估计 noise floor
4. per-tap Wiener gain: `S/(S+N)`
5. OAI `dft()` 变换回频域
6. 写回 active band

**环境变量控制**：`SRS_FREQ_METHOD=wiener|ma|auto`

**代码变更**：
- `nr_srs_freq_smooth.c`：+`srs_freq_method_t` 枚举、+`wiener_delay_shrink_inplace()` 函数、+方法选择逻辑
- `nr_srs_freq_smooth.h`：+`SRS_FREQ_METHOD` 文档
- `launch_all_v9.sh`：+`SRS_FREQ_METHOD` 环境变量转发
- `nr_ul_channel_estimation.c`：无变更（复用 `nr_srs_freq_smooth_enabled()` 路径）

### 3.2 崩溃：alignment assertion

**首次实测**：gNB 在第 1 帧立即 segfault。

```
Assertion (((intptr_t)output&algn)==0) failed!
Segmentation fault (core dumped)
```

**根因**：`calloc()` 在 glibc x86-64 上只保证 16 字节对齐，但 OAI 的 DFT/IDFT 内部使用 AVX2 SIMD 指令，要求 32 字节对齐。

**修复**：`calloc()` → `memalign(32, ...)`

### 3.3 修复后实测：NMSE +7.61 dB（严重退化）

编译通过，不再崩溃，但 NMSE 严重恶化：

| 模式 | NMSE per-ant | scale SRS/GT |
|------|-------------|-------------|
| MA + Kalman | **-17.44 dB** | 234.1x |
| Wiener + Kalman | **+7.61 dB** | 69.2x |

信号幅度被压缩约 3.4 倍。

### 3.4 诊断：roundtrip ratio

加入诊断 log 测量 DFT roundtrip 的输入/输出比：

```
[SRS FreqWiener] frame=51 ... in=(12916,1406) out=(1065,543) ratio=0.0825
```

ratio 不是固定的 1/N 或 1/sqrt(N)，而是每帧变化（0.036~0.64）。

**根因**：不是 DFT scaling 问题，而是 **Wiener gain 本身把信号压缩了**。

原因链：
1. 输入 `freq_in` 只填了 1248/2048 个 SC（其余是 0）
2. IDFT 后信号集中在少量 delay tap，但噪声被 inactive SC 的 0 稀释
3. noise floor 估计虽然适用于全 2048 网格，但信号能量也被稀释
4. Wiener gain 对大部分 tap 给出接近 0 的增益
5. DFT 回去后频域功率大幅下降
6. 后加的 `amp_scale = sqrt(P_in/P_out)` 补偿是工程 hack，无法修复相位/形状破坏

### 3.5 决定回退

确认 delay-domain shrinkage 路径在全 2048 网格上不可用后，决定：
1. 删除全部 Wiener 相关 C 代码
2. 删除 `SRS_FREQ_METHOD` 环境变量
3. 恢复 `nr_srs_freq_smooth.c` 为纯 MA 状态
4. 删除 launcher 中的 `SRS_FREQ_METHOD` 转发
5. 转向 Python 离线验证

**回退后代码验证**：
- `gcc -fsyntax-only`：通过
- `bash -n launch_all_v9.sh`：通过
- `rg SRS_FREQ_METHOD|FreqWiener|wiener_delay_shrink|amp_scale`：0 匹配

---

## 四、Python 离线验证

### 4.1 合成测试（`test_wiener_offline.py`）

新建脚本，实现 oracle-PDP LMMSE 的数学验证：

```python
def lmmse_fullband(y, r_delta, sigma2):
    R = toeplitz_from_r_delta(r_delta)
    return R @ np.linalg.solve(R + sigma2 * I, y)
```

5 个合成测试全部 PASS：

```
flat_1tap              raw -15.54  ma -32.39  wiener -42.20  PASS
freq_selective_2tap    raw -14.97  ma  -5.87  wiener -36.27  PASS
freq_selective_4tap    raw -14.89  ma  -4.40  wiener -24.73  PASS
identity_sigma0        wiener -211.79                        PASS
pure_noise_zero_prior  wiener -300.00                        PASS
```

**关键发现**：在频选场景（2tap/4tap），MA 因为抹平频谱结构而严重退化（-5.87/-4.40 dB），但 oracle Wiener 保持优秀（-36.27/-24.73 dB）。

### 4.2 真实数据离线验证

扩展脚本支持 `--run-dir` 参数，加载真实 SRS dump + GT 做离线对比。

新增三类 data-only 方法：
- `data_pdp_wiener`：noisy SRS → IFFT → avg delay power → noise floor → soft Wiener gain
- `data_delay_topK`：只保留最强 K 个 delay tap，其余清零
- `data_delay_energy`：按能量累积自动选 K

### 4.3 Legacy baseline 结果（`q4_sweep_20260528_150304/snr_15dB`）

```
raw_input          per-ant NMSE = -10.87 dB
ma129              per-ant NMSE = -18.31 dB
data_pdp_wiener    per-ant NMSE = -12.43 dB   ← 失败：几乎等于 raw
data_delay_top1    per-ant NMSE = -45.13 dB   ← 极好但非 Wiener
data_delay_energy  per-ant NMSE = -45.13 dB   (auto K=1)
oracle_pdp_wiener  per-ant NMSE = -43.45 dB   ← 理论上限
```

### 4.4 MA+Kalman run 结果（`q4_sweep_20260528_221806/snr_15dB`）

```
raw_input          per-ant NMSE = -17.44 dB
ma129              per-ant NMSE = -18.73 dB
data_pdp_wiener    per-ant NMSE = -17.45 dB   ← 失败：等于 raw
data_delay_top1    per-ant NMSE = -45.14 dB
data_delay_energy  per-ant NMSE = -45.14 dB   (auto K=1)
oracle_pdp_wiener  per-ant NMSE = -38.45 dB
```

---

## 五、data_pdp_wiener 失败根因分析

### 5.1 核心问题：noisy PDP 估计

用 noisy SRS 做 `IFFT → |.|² → 取平均` 估 PDP 时，每个 delay tap 的 observed power 为：

```
observed[n] = |h_true[n] + w[n]|²
            = |h_true[n]|² + |w[n]|² + cross_terms
```

P1B flat 场景下：
- 信号集中在 ~1 个 delay tap
- 噪声均匀分布在所有 1248 个 delay tap
- noise floor 估计（远尾 median/P25）给出正确的 σ²

但 Wiener gain 公式 `gain = max(0, obs-σ²) / (max(0, obs-σ²) + σ²)` 中：
- 由于 `|w[n]|²` 服从指数分布（2 DOF chi-squared），方差 = 均值²
- 约 **25% 的噪声 tap** 的 observed power 超过 σ²，导致 `gain > 0`
- 诊断证实：`gain_nonzero = 24.6%`

### 5.2 与 oracle 的差距

| 项目 | oracle | data |
|------|--------|------|
| PDP 来源 | GT（干净） | noisy SRS（方差大） |
| σ² 来源 | LS residual（精确） | 远尾统计（近似） |
| gain_nonzero | ~0.08%（仅信号 tap） | 24.6%（噪声泄漏） |
| NMSE | -38 ~ -43 dB | -12 ~ -17 dB |

### 5.3 data_delay_top1 为什么成功

Top-1 是二值 mask {0, 1}，只保留功率最大的 1 个 delay tap：
- flat 场景下信号 tap 功率 >> 所有噪声 tap，选择几乎总是正确
- 等价于全带平均的一种 rank-1 投影
- 理论降噪 = `10*log10(1248)` ≈ 31 dB → Legacy raw -10.87 + 31 ≈ -42 dB → 与实测 -45 dB 一致

但它 **不是 Wiener**：在频选信道下 K=1 会丢失多径结构。

---

## 六、data_pdp_wiener noise 估计方式对比

扫描三种 noise estimator，均无改善：

| noise_mode | noise_delay_median | gain_nonzero | per-ant NMSE |
|------------|-------------------|-------------|-------------|
| tail-median | 5.71 | 24.6% | -17.45 dB |
| tail-p25 | 1.57 | 37.3% | -17.45 dB |
| tail-p25-exp | 5.46 | 25.2% | -17.45 dB |

问题不在 noise floor 统计方法，而在 **单帧 PDP 估计方差太大**。

---

## 七、变更文件清单（最终状态）

### C 端（已回退到纯 MA）

| 文件 | 最终状态 | 备注 |
|------|---------|------|
| `nr_srs_freq_smooth.c` | 纯 MA，无 Wiener 代码 | 已清理 `SRS_FREQ_METHOD`、`wiener_delay_shrink_inplace`、`amp_scale` |
| `nr_srs_freq_smooth.h` | 原始 MA 头文件 | 已删除 `SRS_FREQ_METHOD` 文档 |
| `launch_all_v9.sh` | 无 `SRS_FREQ_METHOD` 转发 | 保留 `SRS_FREQ_SMOOTH_MAX_WIN` |

### Python（新增离线验证）

| 文件 | 操作 | 内容 |
|------|------|------|
| `test_wiener_offline.py` | **新建** | 合成测试 + 真实 run-dir 离线评估（oracle/data-PDP/topK/energy Wiener） |

---

## 八、关键结论与教训

### 8.1 C 端直接试错的代价

| 问题 | 教训 |
|------|------|
| alignment crash | OAI DFT 需要 32 字节对齐，`calloc` 不够 |
| 全 2048 网格 shrinkage 破坏信号 | inactive SC 置零 + IDFT 导致 sinc 泄漏 + 信号稀释 |
| 幅度补偿 hack（amp_scale） | 后验功率归一化无法修复相位/形状破坏 |
| 每次调试需要 attach + sweep | 10 分钟/次，大量时间浪费 |

### 8.2 离线验证的价值

| 发现 | 来源 |
|------|------|
| oracle Wiener 可达 -38~-45 dB | Python 离线 → 证明 Wiener 公式本身正确 |
| data-PDP Wiener 因 noisy PDP 失败 | Python 离线 → 发现根因是 PDP 方差，不是公式 |
| Top-1 delay 在 flat 场景 = -45 dB | Python 离线 → 发现最简方案 |
| gain_nonzero=24.6% | 诊断输出 → 定量确认噪声 tap 泄漏 |

### 8.3 Wiener 的理论上限

P1B flat 场景下，频域 Wiener 的理论上限：

```
NMSE_min ≈ raw_NMSE - 10*log10(N_active)
         ≈ -10.87 - 10*log10(1248)
         ≈ -10.87 - 31
         ≈ -42 dB
```

实测 oracle Wiener = -43.45 dB，Top-1 = -45.13 dB，均接近理论值。

---

## 九、后续方向

### 9.1 使 data-PDP Wiener 可用的三条路线

| 路线 | 方案 | 优先级 |
|------|------|--------|
| A. 多帧 PDP EMA | 对最近 M 帧的 delay power 做 EMA/滑窗平均，压低 PDP 估计方差 | **高** |
| B. MA→PDP→Wiener 级联 | 先用 MA 粗降噪，再从 MA 输出估 PDP，对原始信号做 Wiener | 中 |
| C. 参数化 PDP 模型 | 假设指数衰减 PDP，从 roughness/noise ratio 反推 delay spread | 低 |

### 9.2 下一步具体行动

1. 在 `test_wiener_offline.py` 中实现多帧 PDP EMA Wiener，按帧顺序模拟
2. 扫 EMA alpha = 0.9, 0.95, 0.98, 0.99
3. 在 Legacy 和 MA+Kalman 两个 run 上验证收敛曲线
4. 如果离线验证成功，再考虑 C 端移植

---

## 十、数据归档

| 用途 | 路径 |
|------|------|
| 离线验证脚本 | `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/test_wiener_offline.py` |
| Legacy baseline | `DevChannelProxyJIN/logs/q4_sweep_20260528_150304/snr_15dB/` |
| MA+Kalman run | `DevChannelProxyJIN/logs/q4_sweep_20260528_221806/snr_15dB/` |
| Wiener 失败 run (alignment crash) | `DevChannelProxyJIN/logs/q4_sweep_20260528_223255/snr_15dB/` |
| Wiener 失败 run (NMSE +7 dB) | `DevChannelProxyJIN/logs/q4_sweep_20260528_225504/snr_15dB/` |
| C 端代码（已回退到 MA） | `openair1/PHY/NR_ESTIMATION/nr_srs_freq_smooth.c` |
| 分析计划 | `.cursor/plans/safe_true_wiener_811b5b8d.plan.md` |
| 后续 PDP EMA 计划 | `.cursor/plans/real_wiener_analysis_0a06f541.plan.md` |
