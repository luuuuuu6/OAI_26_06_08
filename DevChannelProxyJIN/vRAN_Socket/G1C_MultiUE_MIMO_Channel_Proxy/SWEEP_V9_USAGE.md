# Q4 SNR Sweep v9 使用手册

本文记录 `run_q4_snr_sweep_v9.sh` / `launch_all_v9.sh` 的常用运行方式和关键环境变量。

## 一、基本说明

`run_q4_snr_sweep_v9.sh` 是批量 SNR sweep 入口。它会逐个 SNR 点调用：

```bash
launch_all_v9.sh
```

虽然脚本名是 `v9`，当前默认 Proxy 仍是：

```bash
PROXY_VER=v8
```

也就是实际启动：

```bash
v8.py
```

当前默认行为：

| 项目 | 默认值 |
|------|--------|
| Proxy | `v8.py` |
| SRS 周期 | `SRS_PERIOD_SLOTS=10` |
| GT 保存频率 | `GT_SAVE_EVERY=10` |
| UL pre-gain | `UL_PRE_GAIN=1.0` |
| SRS estimator | `SRS_ESTIMATOR=legacy` |

注意：`SRS_PERIOD_SLOTS` 是 OAI C 侧新增的环境变量，修改后需要重新编译 OAI 才会生效。

---

## 二、最常用命令

### LS baseline

```bash
sudo \
ONLY_SNR="20 30 40" \
MAX_FRAMES=200 \
SRS_ESTIMATOR=legacy \
SRS_PERIOD_SLOTS=10 \
GT_SAVE_EVERY=100 \
UL_PRE_GAIN=1.0 \
bash run_q4_snr_sweep_v9.sh
```

### Kalman

```bash
sudo \
ONLY_SNR="20 30 40" \
MAX_FRAMES=200 \
SRS_ESTIMATOR=2dmmse \
SRS_2D_METHOD=kalman \
SRS_PERIOD_SLOTS=10 \
GT_SAVE_EVERY=100 \
UL_PRE_GAIN=1.0 \
bash run_q4_snr_sweep_v9.sh
```

### IBVSS

```bash
sudo \
ONLY_SNR="20 30 40" \
MAX_FRAMES=200 \
SRS_ESTIMATOR=2dmmse \
SRS_2D_METHOD=ibvss \
SRS_PERIOD_SLOTS=10 \
GT_SAVE_EVERY=100 \
UL_PRE_GAIN=1.0 \
bash run_q4_snr_sweep_v9.sh
```

### UL Pre-Gain 对比

建议先试 `1.5` / `2.0`，不要直接上 `4.0`。

```bash
sudo \
ONLY_SNR="30 40" \
MAX_FRAMES=200 \
SRS_ESTIMATOR=legacy \
SRS_PERIOD_SLOTS=10 \
GT_SAVE_EVERY=100 \
UL_PRE_GAIN=1.5 \
bash run_q4_snr_sweep_v9.sh
```

---

## 三、变量清单

### 1. Sweep 控制

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SNR_POINTS` | `-5 0 5 10 15 20 25` | 要跑的 SNR 列表 |
| `ONLY_SNR` | 空 | 只跑指定 SNR，覆盖 `SNR_POINTS` |
| `SWEEP_ROOT` | 自动 timestamp | 指定输出根目录，可用于恢复 |
| `MAX_FRAMES` | `300` | 每个 SNR 点目标 SRS frame 数 |
| `MIN_SRS_BINS` | `1` | 至少需要多少个 SRS bin |
| `MIN_SRS_FRAMES` | `MAX_FRAMES` | 至少需要多少个 SRS frame |
| `HARD_CEILING_SEC` | `900` | 单个 SNR 点最长运行秒数 |

### 2. MIMO / UE / Channel

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GNB_NX` | `2` | gNB 阵列横向天线数 |
| `GNB_NY` | `1` | gNB 阵列纵向天线数 |
| `UE_NX` | `2` | UE 阵列横向天线数 |
| `UE_NY` | `1` | UE 阵列纵向天线数 |
| `NUM_UES` | `1` | UE 数 |
| `P1B_NPZ` | `../P1B_Valid_Results/Area1_7.5GHz_Rays_Valid_RXs.npz` | P1B ray 数据 |
| `UE_RX_INDICES` | `98` | 使用的 RX index |
| `NPY_DIR` | 空 | 自定义 ray npy 目录 |
| `CHANNEL_SEED` | `42` | 随机种子 |
| `UE_SPEED` | 空 | UE 速度，空则使用 `v8.py` 内置默认 |
| `DEFAULT_DIST_M` | `100` | 默认 BS-UE 距离 |
| `P1B_DISTANCE_MODE` | `default` | `default` 或 `tau` |

### 3. Proxy / GT / AGC

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_VER` | `v8` | 启动 `${PROXY_VER}.py` |
| `UL_PRE_GAIN` | `1.0` | Proxy 侧 UL pre-gain，写入 gNB `ul_rx` 前放大 |
| `GT_SAVE_EVERY` | `10` | 每 N 个 UL slot 保存一次 GT（launcher 实际默认 10；干净 unified-V3 A/B 用 `1`）|
| `DIGITAL_AGC` | `0` | OAI FFT 后 digital AGC，不建议用于解决 FFT 前量化问题 |

`GT_SAVE_EVERY` 越大越省盘但 GT 越疏：`10`（=5ms，launcher 默认）通常足够配对；`100`（=50ms）远超相干时间、配对很疏，不推荐；做干净的 unified-V3 逐 slot 精确配对（尤其动态 A/B）用 `1`（每 UL slot 都存，磁盘 ~×10）。

### 4. SRS estimator / SRS 周期

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SRS_ESTIMATOR` | `legacy` | OAI SRS estimator 模式 |
| `SRS_PERIOD_SLOTS` | `10` | SRS 周期 override，单位 slot |
| `SRS_OPTFILT` | `0` | SRS interpolation filter 相关开关 |
| `SRS_REF_DUMP_PATH` | `/tmp/oai_gpu_ipc/srs_ref.bin` | SRS reference dump 路径 |

常用 estimator：

```bash
SRS_ESTIMATOR=legacy
SRS_ESTIMATOR=2dmmse SRS_2D_METHOD=kalman
SRS_ESTIMATOR=2dmmse SRS_2D_METHOD=ibvss
```

SRS 周期说明：

| 值 | 含义 |
|----|------|
| `SRS_PERIOD_SLOTS=10` | 每 10 slots，推荐实验默认 |
| `SRS_PERIOD_SLOTS=160` | 接近原 OAI 自动配置行为 |

### 5. Kalman / 2D Filter

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SRS_2D_METHOD` | 空 | `kalman` / `ibvss` 等 |
| `SRS_2D_DEBUG` | 空 | 打开 2D filter debug |
| `SRS_2D_KALMAN_WARMUP_MIN` | C 默认 | Kalman warmup 最小帧数 |
| `SRS_2D_KALMAN_WARMUP_MAX` | C 默认 | Kalman warmup 最大帧数 |
| `SRS_2D_KALMAN_DEBOUNCE` | C 默认 | Kalman warmup debounce |
| `SRS_2D_KALMAN_Q_EMA` | C 默认 | Q 平滑系数 |
| `SRS_2D_KALMAN_INNOV_EMA` | C 默认 | innovation 平滑系数 |
| `SRS_2D_ALPHA` | C 默认 | 2D filter / IBVSS 相关 alpha |

### 6. Attach 稳定窗口

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ATTACH_STABLE_SEC` | `300` | attach 后最大 stable/freeze 时间 |
| `ATTACH_STABLE_MODE` | `auto` | `auto` 或 `time` |
| `ATTACH_STABLE_TRIGGER` | `rrc_reconfig` | `rrc_reconfig` / `srs` / `off` |
| `ATTACH_STABLE_POST_DELAY` | `15` | trigger 后延迟多少秒再 dynamic handoff |

---

## 四、结果目录

每次 sweep 输出到：

```bash
DevChannelProxyJIN/logs/q4_sweep_YYYYMMDD_HHMMSS/
```

每个 SNR 点一个目录：

```bash
snr_20dB/
snr_30dB/
snr_40dB/
```

关键文件：

| 文件 | 说明 |
|------|------|
| `sweep_manifest.txt` | sweep 总结 |
| `snr_*/srs_matrix_gNB_*.bin` | OAI SRS estimator 输出 |
| `snr_*/sionna_gt/*.npz` | Sionna GT |
| `snr_*/gnb.log` | gNB 日志 |
| `snr_*/proxy.log` | Proxy 日志 |
| `snr_*/attach_diag_v9.log` | attach 诊断 |

---

## 五、评估 NMSE

使用 `eval_nmse_clean.py` 做纯 LS baseline 对比。它会 lazy 加载 GT，只读取和 SRS 配对的帧，避免 OOM。

```bash
python3 eval_nmse_clean.py \
  --run-dir DevChannelProxyJIN/logs/q4_sweep_YYYYMMDD_HHMMSS/snr_20dB \
  --tol 20
```

输出重点看：

```text
NMSE per-antenna
Temporal Stability
scale SRS/GT
```

说明：

- `NMSE per-antenna` 是当前推荐主指标。
- `scale SRS/GT` 用于观察 SRS 与 GT 尺度差。
- `Temporal Stability` 用于发现后期数据质量退化。

---

## 六、LS vs Kalman 对比方式

不要在同一次 run 里同时对比 LS 和 Kalman。当前 dump 的 `srs_matrix_gNB_*.bin` 存的是所选 `SRS_ESTIMATOR` 的最终输出。

所以应分别跑两次：

### Run A: LS

```bash
sudo \
ONLY_SNR="20 30 40" \
MAX_FRAMES=200 \
SRS_ESTIMATOR=legacy \
SRS_PERIOD_SLOTS=10 \
GT_SAVE_EVERY=100 \
bash run_q4_snr_sweep_v9.sh
```

### Run B: Kalman

```bash
sudo \
ONLY_SNR="20 30 40" \
MAX_FRAMES=200 \
SRS_ESTIMATOR=2dmmse \
SRS_2D_METHOD=kalman \
SRS_PERIOD_SLOTS=10 \
GT_SAVE_EVERY=100 \
bash run_q4_snr_sweep_v9.sh
```

然后分别用 `eval_nmse_clean.py` 评估对应目录，比较 `NMSE per-antenna`。

---

## 七、注意事项

1. `SRS_PERIOD_SLOTS` 依赖 OAI C 修改，修改后必须重新编译 OAI。
2. `UL_PRE_GAIN` **保持 `1.0`（默认）**。实测 `2.0` 会把 UL 样点 int16 削顶/饱和 → ULSCH BLER 飙升（CDL 上 ~65%）→ attach 掷硬币/失败；`1.0` 才能稳定 attach（2026-05-30 实证，CDL-c 静态 gain=1.0 采到 431 SRS 帧）。除非明确做 pre-gain 对比实验，否则不要 >1.0。
3. `GT_SAVE_EVERY` launcher 实际默认 `10`（本手册旧版误标 100，已更正）。`100` 太疏不推荐；干净 unified-V3 逐 slot 精确配对 / 动态 A/B 用 `1`。
4. `SRS_ESTIMATOR=2dmmse` 时，具体算法由 `SRS_2D_METHOD` 决定。
5. 如果 `eval_nmse_clean.py` 显示 `paired` 很少，优先检查 `SRS_PERIOD_SLOTS`、`GT_SAVE_EVERY`（疏则配对少）、attach 状态和 `--tol`。
6. CDL 评估用 `SRS_ONLY_CHANNEL=1`（只在 SRS 符号 12 保留 CDL，其余符号平坦，GT[12]=SRS 所见 CDL）。配套 `SRS_ONLY_DL_FLAT`（默认 1）会把**整个下行**也平坦化——DL 不需要 CDL（评估只看 UL SRS vs GT[12]），DL 走多径会让 PBCH/PDSCH/RRC 下行解码失败、attach 掷硬币。保持默认 1；只有要让 DL 也跑 CDL 时才设 0。`SRS_FLAT_MODE` 默认 `power`（保功率，NLOS 上 attach 才稳）。

