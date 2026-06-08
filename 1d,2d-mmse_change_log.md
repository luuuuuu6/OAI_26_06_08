# SRS 1D-MMSE / 2D-MMSE 设计日志

日期：2026-05-02

本文记录当前 OAI 分支中 gNB 侧 SRS 信道估计现状、`1D-MMSE` 与 `2D-MMSE` 的关系，以及先实现 `1D-MMSE` 的初步规划。

## 1. 背景

当前 v7 Channel Proxy 已经通过 `attach-stable` 机制解决了初始 attach 阶段 SRS 不出现的问题，能够稳定得到 `srs_matrix_gNB_*.bin`。但后续 NMSE 分析发现：

- 短窗口，例如 `--fixed-n 15`，NMSE 可以进入可用范围。
- 长窗口，例如默认 `N=100`，NMSE 容易明显变差。
- 当前 OAI 侧日志中虽然出现 `SRS 2D-MMSE`，但代码并没有实现严格意义上的二维 MMSE 信道估计。

因此下一步需要判断：是否应该在 OAI 的 SRS 估计链路中真正实现 MMSE，优先从风险较低的 `1D-MMSE` 开始。

## 2. 当前 OAI SRS 估计链路

gNB 侧 SRS 处理主链路如下：

```text
MAC/RRC 配置 SRS
→ gNB_scheduler_srs.c 生成 nfapi_nr_srs_pdu_t
→ srs_rx.c::nr_fill_srs() 登记到 gNB->srs[]
→ phy_procedures_nr_gNB.c::phy_procedures_gNB_uespec_RX()
→ srs_rx.c::nr_get_srs_signal() 从 rxdataF 抽取 SRS RE
→ nr_ul_channel_estimation.c::nr_srs_channel_estimation()
→ fill_srs_channel_matrix() / SRS dump / MAC RI-TPMI 使用
```

核心估计函数：

```text
DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c
  nr_srs_channel_estimation()
```

当前 `nr_srs_channel_estimation()` 的主要行为：

1. 用本地生成的 SRS 序列和接收 SRS 做 LS 估计。
2. 对 SRS comb 上的 LS 点使用固定 `filt8_*` / `filt16_*` 滤波器做频域插值。
3. 将结果写入 `srs_estimated_channel_freq`。
4. 计算噪声、SNR、`snr_per_rb`。
5. 做一个被标注为 `SRS 2D-MMSE` 的相邻子载波互相关相位斜率估计和相位旋转补偿。
6. 将最终 `srs_estimated_channel_freq` dump 到 `srs_matrix_gNB_*.bin`。

重要判断：

```text
当前所谓 SRS 2D-MMSE = 互相关相位斜率估计 + 频域相位补偿
不是严格的 2D-MMSE / Wiener 信道估计。
```

## 3. 1D-MMSE 和 2D-MMSE 的关系

`1D-MMSE` 和 `2D-MMSE` 不是互相排斥的两套独立方案。更合理的工程路线是：

```text
1D-MMSE 是 2D-MMSE 的频率维基础模块。
```

### 1D-MMSE

`1D-MMSE` 只在一个维度上利用相关性，当前最适合先做频率维：

```text
H_ls(freq)
→ 使用 R_freq 和 sigma2
→ H_mmse(freq)
```

对 SRS 来说，它替代当前固定 `filt8/filt16` 频域插值。

优点：

- 不需要改变 SRS RRC 配置。
- 不依赖多 SRS symbol。
- 不需要跨 slot 历史缓存。
- 最小化对 OAI 实时链路的影响。
- 能直接和当前 legacy 插值做 NMSE 对照。

### 2D-MMSE

`2D-MMSE` 同时利用时间和频率两个维度：

```text
H_ls(time, freq)
→ 使用 R_time 和 R_freq
→ H_mmse(time, freq)
```

实际工程中通常不直接构造巨大二维矩阵，而使用可分离近似：

```text
R_2D ≈ R_time ⊗ R_freq
```

这样可以实现为：

```text
LS grid
→ frequency MMSE
→ time MMSE
→ 2D-MMSE result
```

因此 `1D-MMSE` 可以自然升级到 `2D-MMSE`。

## 4. 为什么现在先做 1D-MMSE

当前 OAI 默认 SRS 配置是 1 个 symbol：

```text
nr_radio_config.c:
  srs_res->resourceMapping.nrofSymbols = n1
```

这意味着同一个 SRS occasion 内没有足够的时间维样本。要做真正 `2D-MMSE`，必须先解决时间维来源：

- 方案 A：把 SRS 配成 `n2` 或 `n4`，同一次 SRS 内有多个 OFDM symbol。
- 方案 B：保持 `n1`，但在 gNB 侧新增 per-RNTI/per-port/per-rx 的跨 SRS 周期历史缓存。

这两个方案都比 `1D-MMSE` 风险更高。因此先做 `1D-MMSE` 更稳：

```text
先提升频域估计质量，建立 LS/MMSE/dump/NMSE 对照框架；
后续再在同一个接口上扩展时间维。
```

## 5. 对当前 NMSE 不稳定问题的预期影响

`1D-MMSE` 可能改善：

- 低 SNR 下 LS 估计噪声较大的问题。
- SRS comb 稀疏导致的频域插值误差。
- `fill_srs_channel_matrix()` 抽样 PRG 时的局部抖动。
- 短窗口 NMSE，例如 `--fixed-n 15/30`。

`1D-MMSE` 不一定能解决：

- v7 动态 handoff 后真实信道随时间变化导致的长窗口非平稳。
- `N=100` 把多段不同动态信道混在一起导致的 NMSE 变差。
- GT 与 SRS dump 之间的 STO/相位斜率/时间对齐误差。

因此预期是：

```text
1D-MMSE 主要改善“估计器噪声和频域插值”；
它不会单独解决“动态信道长窗口非平稳”。
```

## 6. 1D-MMSE 先期实现规划

### 6.1 目标

第一阶段目标不是直接替换全部 SRS 链路，而是做一个可对照、可回退的 `SRS 1D-MMSE` 实现：

```text
legacy: 当前 LS + filt8/filt16
mmse1d: LS + frequency-domain MMSE
```

输出仍然写回：

```text
srs_estimated_channel_freq[rx][tx][k]
```

这样后续链路无需大改：

- `freq2time()` / timing advance 可以继续使用。
- `fill_srs_channel_matrix()` 可以继续使用。
- SRS dump 可以继续使用。
- MAC 侧 RI/TPMI 可以继续使用。

### 6.2 建议新增文件

建议新增独立模块，避免把 `nr_ul_channel_estimation.c` 继续变大：

```text
openair1/PHY/NR_ESTIMATION/nr_srs_mmse.h
openair1/PHY/NR_ESTIMATION/nr_srs_mmse.c
```

建议暴露的函数：

```c
typedef enum {
  NR_SRS_EST_LEGACY = 0,
  NR_SRS_EST_MMSE1D = 1,
  NR_SRS_EST_MMSE2D = 2,
} nr_srs_estimator_mode_t;

void nr_srs_build_ls_grid(...);

void nr_srs_mmse_freq_filter(...);

void nr_srs_mmse_time_filter(...);

void nr_srs_mmse2d_estimate(...);
```

第一阶段可以只实现：

```text
nr_srs_build_ls_grid()
nr_srs_mmse_freq_filter()
```

`nr_srs_mmse_time_filter()` 和 `nr_srs_mmse2d_estimate()` 可以先预留接口或暂不接入。

### 6.3 `nr_srs_channel_estimation()` 的改动点

当前 LS 与固定插值混在同一个循环中。需要拆成两步：

```text
Step 1: 只提取 LS pilot 点
Step 2: 根据 estimator mode 选择 legacy 插值或 mmse1d
```

建议改造逻辑：

```text
for ant
  for port
    build LS pilot grid

    if mode == legacy:
      run current filt8/filt16 interpolation
    else if mode == mmse1d:
      run frequency MMSE interpolation/filtering

    write srs_estimated_channel_freq[ant][port]
```

第一版可以先少改结构：

- 保留当前 legacy 代码路径。
- 新增 `mmse1d` 分支。
- 不删除当前 `filt8/filt16`。
- 不改变 dump 格式。

### 6.4 频域 1D-MMSE 算法建议

先实现低复杂度版本，不做每个 RE 的大型矩阵求逆。

候选方案：

```text
H_mmse(k) = sum_i w_i(k) * H_ls(pilot_i)
```

其中权重由频域相关性和噪声决定：

```text
w(k) = r_hp(k)^H * inv(R_pp + sigma2 I)
```

频域相关模型可以先用指数相关：

```text
R_freq(delta_k) = exp(-abs(delta_k) / corr_len_sc)
```

初始参数建议：

```text
corr_len_sc = 12 或 24
window_pilots = 4 或 6
sigma2 = 当前 noise 统计得到的 noise_power
```

第一版可以用浮点 `double` 实现小矩阵求解，便于验证正确性。验证有效后再考虑定点/SIMD 优化。

### 6.5 模式开关

需要一个运行时开关，避免每次切代码。

候选开关：

```text
SRS_ESTIMATOR=legacy
SRS_ESTIMATOR=mmse1d
SRS_ESTIMATOR=mmse2d
```

实现位置待定：

- 最简单：从环境变量读取，只影响实验分支。
- 更正式：加入 gNB 配置参数，写入 `PHY_VARS_gNB`。

先期建议使用环境变量，降低接入成本：

```c
const char *mode = getenv("SRS_ESTIMATOR");
```

后续稳定后再转为正式配置项。

### 6.6 Dump 与对照

为了判断 `1D-MMSE` 是否真的改善 NMSE，需要能对照 legacy 与 mmse1d。

第一阶段建议：

- 默认 dump 当前最终 `srs_estimated_channel_freq`，保持兼容。
- 日志里打印估计器模式：

```text
[SRS Estimator] mode=legacy
[SRS Estimator] mode=mmse1d
```

第二阶段可以扩展 dump：

```text
srs_matrix_gNB_legacy_*.bin
srs_matrix_gNB_mmse1d_*.bin
```

但第一版不建议马上改 dump 文件格式，避免影响 `q4_convergence_sweep.py`。

### 6.7 验证计划

建议按以下顺序验证：

1. 编译通过，legacy 模式结果与当前结果一致。
2. `SRS_ESTIMATOR=mmse1d` 能正常 attach、正常产生 SRS bin。
3. 对同一套 v7 sweep 分别跑 legacy 和 mmse1d。
4. 用 `q4_convergence_sweep.py` 分析：

```text
--fixed-n 15
--fixed-n 30
--fixed-n 50
--fixed-n 100
```

关注指标：

- NMSE dB 是否下降。
- PDP correlation 是否提高。
- RSRP error 是否减小。
- SRS dump 数量是否正常。
- gNB 是否仍能稳定完成 RRC/NAS/SRS。

### 6.8 风险

主要风险：

- `sigma2` 估计不准会导致 MMSE 过平滑或欠平滑。
- 频域相关长度 `corr_len_sc` 设错会导致 NMSE 反而变差。
- 浮点小矩阵求解可能增加实时开销。
- 当前 SRS 估计代码中多 symbol 维度尚未真正用好，暂时不要宣称实现 2D。
- 当前 `SRS 2D-MMSE` 日志名称容易误导，后续应改名为 `SRS delay compensation` 或类似名称。

## 7. 未来升级到 2D-MMSE 的路线

`1D-MMSE` 完成后，2D 升级路线有两条。

### 路线 A：同一次 SRS 内多 symbol

需要改：

- RRC/MAC SRS 配置，支持 `nrofSymbols = n2/n4`。
- `nr_srs_channel_estimation()`，按 `nr_srs_info->k_0_p[p][l]` 遍历每个 SRS symbol。
- 构造 `H_ls[l][k]`。
- 在频域 MMSE 后增加时间维 MMSE。

优点：

- 时间维来自同一个 slot，缓存简单。
- 更接近标准 2D time-frequency estimator。

缺点：

- 需要改 SRS 配置。
- 当前 beam report 路径中有 `num_reported_symbols == 1` 的限制，需要同步处理。

### 路线 B：跨周期 SRS history

保持当前 `n1`，新增历史缓存：

```text
key = RNTI + rx_ant + srs_port + subcarrier/prg
value = 最近 K 次 SRS 估计
```

然后做时间维 MMSE：

```text
H_mmse_time(t, k) = time_filter(history[:, k])
```

优点：

- 不需要改变 SRS RRC 配置。
- 适合当前周期性 SRS。

缺点：

- 需要处理 UE 动态、handoff 后非平稳、RNTI 状态清理。
- 时间相关模型需要谨慎，速度高时容易过平滑。

## 8. 当前结论

当前最稳妥的路线：

```text
先做 SRS 1D-MMSE 频域估计；
保留 legacy 路径；
保留现有 dump 格式；
用固定窗口 NMSE 对照验证；
确认有效后，再扩展多 symbol 或 history-based 2D-MMSE。
```

短期不要把当前 `SRS 2D-MMSE: xcorr_delay=...` 当作真正 2D-MMSE。它应该被视为一个 STO/相位斜率补偿模块。

## 9. 2026-05-02 实现记录：SRS 1D-MMSE 初版

本次已经按先期设计完成第一版 `SRS 1D-MMSE` 代码接入。目标是先提供一个可回退、可对照的频域 MMSE 估计路径，不改变现有 legacy 默认行为和 SRS dump 文件格式。

### 9.1 新增文件

新增：

```text
DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_mmse.h
DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_mmse.c
```

`nr_srs_mmse.h` 定义：

```c
typedef enum {
  NR_SRS_EST_LEGACY = 0,
  NR_SRS_EST_MMSE1D = 1,
  NR_SRS_EST_MMSE2D = 2,
} nr_srs_estimator_mode_t;
```

主要接口：

```c
nr_srs_estimator_mode_t nr_srs_get_estimator_mode(void);

uint32_t nr_srs_estimate_residual_noise_power(...);

void nr_srs_mmse_freq_filter(...);
```

### 9.2 接入文件

修改：

```text
DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c
DevChannelProxyJIN/openairinterface5g_whan/CMakeLists.txt
```

`nr_ul_channel_estimation.c` 中新增：

```c
#include "nr_srs_mmse.h"
```

并在 `nr_srs_channel_estimation()` 中加入：

```c
const nr_srs_estimator_mode_t srs_estimator_mode = nr_srs_get_estimator_mode();
```

默认情况下继续走原来的 `filt8/filt16` legacy 路径。只有在环境变量打开时才走 MMSE：

```text
SRS_ESTIMATOR=mmse1d
```

`CMakeLists.txt` 中已将新源文件加入 gNB 侧 `PHY_NR_SRC`：

```text
${OPENAIR1_DIR}/PHY/NR_ESTIMATION/nr_srs_mmse.c
```

### 9.3 运行开关

默认行为：

```bash
unset SRS_ESTIMATOR
```

或：

```bash
export SRS_ESTIMATOR=legacy
```

均走原 OAI legacy SRS 估计路径。

启用 1D-MMSE：

```bash
export SRS_ESTIMATOR=mmse1d
```

预留但尚未实现：

```bash
export SRS_ESTIMATOR=mmse2d
```

当前 `mmse2d` 会打印 warning，并回退到 legacy，避免误以为已经实现真 2D-MMSE。

### 9.4 可调参数

第一版 1D-MMSE 使用小窗口频域 Wiener 滤波，默认参数如下：

```bash
export SRS_MMSE_WINDOW=4
export SRS_MMSE_CORR_LEN_SC=12.0
```

如果不设置环境变量，则使用默认值：

```text
SRS_MMSE_WINDOW = 4
SRS_MMSE_CORR_LEN_SC = 12.0
```

含义：

- `SRS_MMSE_WINDOW`：每个目标子载波附近使用多少个 SRS pilot 点构造局部 Wiener 解。
- `SRS_MMSE_CORR_LEN_SC`：频域相关长度，单位是 subcarrier。

### 9.5 算法实现摘要

第一版 `nr_srs_mmse_freq_filter()` 做的是局部频域 Wiener/MMSE 滤波：

```text
H_mmse(k) = sum_i w_i(k) * H_ls(pilot_i)
```

权重通过小矩阵求解得到：

```text
w(k) = inv(R_pp + sigma2 I) * r_hp(k)
```

频域相关模型：

```text
R_freq(delta_sc) = exp(-abs(delta_sc) / corr_len_sc)
```

噪声功率第一版来自当前 legacy 插值结果与 LS pilot 点之间的残差：

```text
sigma2 ≈ mean(|H_ls(pilot) - H_legacy(pilot)|^2)
```

这样做的好处是：

- 不需要改现有 SRS 接收和 LS 提取逻辑。
- 不需要改 dump 格式。
- `legacy` 和 `mmse1d` 可以使用同一套 SRS bin / NMSE 分析流程。

### 9.6 保持不变的行为

本次没有改：

- `srs_matrix_gNB_*.bin` 文件格式。
- `fill_srs_channel_matrix()`。
- MAC 侧 RI/TPMI 消费路径。
- SRS RRC 配置。
- 默认 legacy 行为。
- 当前 `SRS 2D-MMSE: xcorr_delay=...` 的相位斜率补偿模块。

注意：当前 `SRS 2D-MMSE: xcorr_delay=...` 仍然不是严格意义上的 2D-MMSE，后续建议改名为 `SRS delay compensation` 或类似名称。

### 9.7 当前验证状态

已完成：

```text
ReadLints: no linter errors
git diff --check: no whitespace errors
```

未由助手执行完整编译。编译由用户在已有 OAI build 目录中执行：

```bash
cd /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/openairinterface5g_whan/cmake_targets/ran_build/build
ninja nr-softmodem nr-uesoftmodem
```

### 9.8 后续实验建议

建议先做两组最小对照：

```bash
# legacy
unset SRS_ESTIMATOR

# mmse1d
export SRS_ESTIMATOR=mmse1d
```

每组用相同 v7 sweep 条件跑 SNR=10 dB，然后分析：

```bash
python q4_convergence_sweep.py --fixed-n 15  ...
python q4_convergence_sweep.py --fixed-n 30  ...
python q4_convergence_sweep.py --fixed-n 50  ...
python q4_convergence_sweep.py --fixed-n 100 ...
```

重点观察：

- `--fixed-n 15/30` 的 NMSE 是否下降。
- PDP correlation 是否上升。
- RSRP error 是否降低。
- SRS bin 数量是否和 legacy 一样稳定。
- `N=100` 是否仍然受动态信道非平稳影响。

预期：

```text
1D-MMSE 主要改善频域插值和噪声导致的估计误差；
不保证单独解决 v7 动态 handoff 后的长窗口非平稳 NMSE 问题。
```

## 10. 2026-05-02 初次 legacy / mmse1d 对照结果

### 10.1 对照运行

本次使用同一套 v7 sweep 条件，在 SNR=10 dB 下分别跑了 legacy 和 `mmse1d`：

```text
legacy  sweep: /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/logs/q4_sweep_20260502_191419
mmse1d  sweep: /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/logs/q4_sweep_20260502_224942
```

manifest 记录：

```text
legacy : status=OK, n_gt=24, n_srs=2
mmse1d : status=OK, n_gt=53, n_srs=1
```

gNB 日志确认：

```text
legacy : [SRS Estimator] mode=legacy
mmse1d : [SRS Estimator] mode=mmse1d
```

离线分析实际配对帧数：

```text
legacy : raw(srs=102, gt=2312) -> paired 102, median gap 3 slots
mmse1d : raw(srs=45,  gt=5224) -> paired 45,  median gap 3 slots
```

说明：

- 两组都成功进入 SRS dump 和 Q4 分析流程。
- `mmse1d` 组帧数少于 legacy，因此当前结果是初步 A/B evidence，不是最终统计结论。
- 两组是动态链路下的独立运行，不是完全相同随机轨迹的严格 replay 对照。

### 10.2 无 AGC 修正结果

分析命令使用 `q4_convergence_sweep.py`，slot 对齐，`AGC=none`。

固定窗口结果：

```text
N=15:
  legacy  NMSE=+16.02 dB, RSRPerr=16.12 dB, PDP corr=0.5070, CovFro=0.6306
  mmse1d  NMSE= +4.20 dB, RSRPerr= 5.61 dB, PDP corr=0.7079, CovFro=0.0388

N=30:
  legacy  NMSE=+15.42 dB, RSRPerr=15.54 dB, PDP corr=0.6606, CovFro=0.4551
  mmse1d  NMSE= -0.29 dB, RSRPerr= 2.87 dB, PDP corr=0.8985, CovFro=0.0290
```

初步观察：

- `mmse1d` 在 N=15 时 NMSE 比 legacy 低约 11.8 dB。
- `mmse1d` 在 N=30 时 NMSE 比 legacy 低约 15.7 dB。
- `mmse1d` 的 RSRP error 明显更小，说明幅度标定更接近 GT。
- `mmse1d` 的 PDP correlation 更高，说明 delay-domain 形状更接近 GT。
- `mmse1d` 的 covariance Frobenius error 明显更低，说明空间相关结构也更稳定。

`mmse1d` 的 N-axis 在小窗口下表现尤其明显：

```text
mmse1d:
  N=1   NMSE=-2.17 dB
  N=2   NMSE=-3.31 dB
  N=3   NMSE=-4.50 dB
  N=5   NMSE=-4.46 dB
  N=8   NMSE=-4.01 dB
  N=10  NMSE=-3.90 dB
```

这说明 1D-MMSE 对单帧/短窗口频域估计质量确实有帮助，不只是依赖长时间平均。

### 10.3 AGC sanity check

为了排除“只是幅度缩放碰巧更准”的可能性，又使用 `--agc ewma` 做了一组离线 sanity check。

这里的 AGC 不是 gNB 运行时算法，也不是本次 `mmse1d` 新增功能。它是 `q4_convergence_sweep.py` 里已有的离线分析选项，用 EWMA 追踪接收增益漂移，对 GT 做随时间变化的幅度对齐：

```text
--agc none : 直接比较 OAI SRS 和 Sionna GT
--agc ewma : 分析阶段对 GT 做 EWMA 幅度对齐
```

AGC 后的固定窗口结果：

```text
N=15:
  legacy  NMSE=+4.63 dB, RSRPerr= 5.92 dB, PDP corr=0.9984, CovFro=0.1270
  mmse1d  NMSE=+4.20 dB, RSRPerr= 5.61 dB, PDP corr=0.7083, CovFro=0.0388

N=30:
  legacy  NMSE=+13.52 dB, RSRPerr=13.71 dB, PDP corr=0.9257, CovFro=0.1655
  mmse1d  NMSE= -0.29 dB, RSRPerr= 2.87 dB, PDP corr=0.8989, CovFro=0.0290
```

AGC sanity check 的含义：

- legacy 在 N=15 下经过 AGC 后会明显变好，说明 legacy 原始结果中确实包含接收增益/幅度漂移影响。
- `mmse1d` 在 AGC 前后几乎不变，说明它这次的改善不是单纯由离线幅度对齐造成的。
- N=30 下 `mmse1d` 仍明显优于 legacy，尤其 NMSE、RSRP error 和 CovFro。

### 10.4 当前结论

> 注：本节为 2026-05-02 的阶段性判断，已被第 11 节（2026-05-05）修正。

当前初步结论：

```text
SRS 1D-MMSE 方向有效。
它显著改善了 v7 动态链路下的短窗口 SRS 频域估计质量。
```

更具体地说：

- `mmse1d` 对 `--fixed-n 15/30` 的 NMSE 有明显改善。
- `mmse1d` 对 RSRP error 和 covariance error 也有明显改善。
- 这符合 1D-MMSE 的预期作用：减少频域插值和噪声导致的估计误差。
- 当前结果还不能证明它已经解决所有 v7 动态非平稳问题，因为两组不是同一随机轨迹 replay。

### 10.5 下一步建议

建议下一步做重复统计：

```text
legacy  x 3 runs
mmse1d  x 3 runs
```

每次都记录：

```text
SRS_ESTIMATOR mode
paired SRS frames
N=15 NMSE / RSRPerr / PDP corr / CovFro
N=30 NMSE / RSRPerr / PDP corr / CovFro
是否出现 attach/RRC/RLF 异常
```

如果 3 次重复后 `mmse1d` 均值和方差仍明显优于 legacy，再进入下一阶段：

```text
1. 调参：SRS_MMSE_WINDOW, SRS_MMSE_CORR_LEN_SC
2. 加强噪声估计方式
3. 准备 history-based 2D-MMSE
```

## 11. 2026-05-05 seed8102/8103 结论修正（收束本轮）

本节用于修正第 10 节中的“`mmse1d` 显著优于 legacy”早期判断。

关键背景：

```text
1) 早期对照中 delay compensation 默认开启，导致结论被混淆。
2) 当时已在 C 侧将 SRS_DELAY_COMP 默认改为 off，并增加 xcorr_delay 异常保护（max_abs）。
3) 在统一 ATTACH_STABLE_TRIGGER=srs 条件下完成了 seed8102 + seed8103 复核。
```

### 11.1 运行状态判断

`ABORTED / srs=0` 不是 `mmse1d + delay on` 的必然行为，更可能是运行偶发问题。重跑后四组均可 `OK`，且 paired 约 100。

### 11.2 seed8102（统一 srs trigger）结果

```text
legacy + delay off : paired=102, N15=-7.37 dB, N30=-6.31 dB, RSRPerr30=0.92 dB, PDP30=0.9990
legacy + delay on  : paired=102, N15=+3.27 dB, N30=+1.38 dB, RSRPerr30=3.76 dB, PDP30=0.9885
mmse1d + delay off : paired=101, N15=-7.13 dB, N30=-7.78 dB, RSRPerr30=0.67 dB, PDP30=0.9998
mmse1d + delay on  : paired=100, N15=+5.75 dB, N30=+22.47 dB, RSRPerr30=22.49 dB, PDP30=0.2145
```

结论：`delay on` 明显有害，且劣化幅度远超 `>=3 dB` 判据。

### 11.3 seed8103（主链路，仅 delay off）结果

```text
legacy + delay off : paired=100, N15=-6.68 dB, N30=-5.71 dB, RSRPerr30=1.04 dB, PDP30=0.9984
mmse1d + delay off : paired=102, N15=-6.68 dB, N30=+1.58 dB, RSRPerr30=3.90 dB, PDP30=0.7778
```

结论：

```text
N=15: mmse1d 与 legacy 基本持平；
N=30: mmse1d 明显差于 legacy。
```

### 11.4 跨 seed 收束结论

```text
1) delay compensation 是当前主要负面因素，默认关闭是正确决策。
2) clean 主链路（delay off）下，legacy 已稳定在约 -6~-7 dB（N=15/30）。
3) 当前 mmse1d 没有“稳定优于 legacy”的证据，只在部分 seed/N 上偶发小幅改善。
4) 因此本轮停止继续 seed 扩展（不做 seed8104 重复验证）。
```

### 11.5 下一步（从“继续跑”转为“修正”）

后续重点应从重复 sweep 切换到 `mmse1d` 参数修正：

```text
A. sigma2 估计稳健化（floor / clipping / 稳健聚合）；
B. 小网格调参：SRS_MMSE_WINDOW 与 SRS_MMSE_CORR_LEN_SC；
C. 统一以 N=30 NMSE + RSRPerr + PDP 作为主判据，做最小回归验证。
```

执行策略：

```text
先修正，再做小规模验证；不再进行无参数变化的重复大规模 seed 扫描。
```

## 12. 2026-05-05 删除 SRS delay compensation 路径

本次修改把此前的 SRS frequency-domain phase-slope delay compensation 路径从 `nr_ul_channel_estimation.c` 中完全移除。

删除范围：

```text
1) 删除 SRS_DELAY_COMP / SRS_DELAY_COMP_MAX_ABS 环境变量解析。
2) 删除 xcorr_delay 相邻子载波互相关估计。
3) 删除基于 xcorr_delay 的全频域相位旋转补偿。
4) 删除相关日志与 math/strings 依赖。
```

删除原因：

```text
seed8102 的 2x2 对照已经证明 delay on 对 legacy 和 mmse1d 都明显有害；
seed8103 clean 主链路也说明后续问题应集中在 mmse1d 自身参数和 sigma2 估计，而不是继续保留 delay compensation 分支。
```

重要结论：

```text
这个模块不是严格意义上的 2D-MMSE。
它更像 blind phase-slope / STO compensation，无法可靠区分真实信道时延与同步/相位斜率误差。
在当前 v7 动态链路 + SRS dump / GT NMSE 验证条件下，该补偿会把估计矩阵整体转坏，因此下线。
```

后续运行口径：

```text
1) 不再使用 SRS_DELAY_COMP=0/1 做 A/B。
2) 后续只比较 SRS_ESTIMATOR=legacy 与 SRS_ESTIMATOR=mmse1d。
3) 下一步重点改 mmse1d：sigma2 稳健化、SRS_MMSE_WINDOW / SRS_MMSE_CORR_LEN_SC 小网格调参。
```

