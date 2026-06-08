# Phase 1 离线日志：因果 EMA + 相对峰值秩 Wiener

| 字段 | 值 |
|------|----|
| **日期** | 2026-05-29 |
| **依据** | `0529_True2D_MMSE_先期计划.md` Phase 1 |
| **范围** | 纯 Python 离线（`test_srs_2d_offline.py`），不动 OAI C |
| **结论** | **Gate 1 PASS**：真实 P1B data-only Wiener -17 → **-45.12 dB**（追平 oracle/top1）；合成频选 flat/2tap/4tap **全部追平 oracle**，MA 在频选崩。算法 = 因果 EMA PDP + 相对峰值秩选择 + soft Wiener，MIMO 阶数无关、对 filt8 有色噪声鲁棒。 |

---

## 一、问题回顾（来自 Wiener 失败日志）

`data_pdp_wiener` 即使对全帧平均 PDP 仍只有 -17（≈raw），根因是 soft Wiener gain 在含噪 PDP + 有偏地板下，~25% 噪声 tap 泄漏（`gain_nonzero=24.6%`）。`data_delay_top1=-45` 证明纯数据信息足够，差距在"地板/tap 选择"。

---

## 二、本次实现（`test_srs_2d_offline.py`）

新增 `data_pdp_ema_wiener_active()`，逐空间链路独立处理：

1. active band 自身 N_act 点 IFFT 到延迟域。
2. **因果 EMA**：`pdp_ema = α·pdp_ema + (1-α)·|y_delay|²`（实时可移植；二阶统计量 R_f 估计，EMA 降方差）。
3. 远尾稳健 noise floor；`sig = max(pdp_ema - floor, 0)`。
4. **相对峰值秩选择（关键）**：保留 `pdp_ema > peak·10^(-rel_db/10)` 且 `> floor·gamma` 的 tap。
5. kept tap 上 soft Wiener gain `sig/(sig+floor)`，其余置零；IFFT 回频域。

新增合成多帧验证 `run_synth_multiframe()`（`--synth-mf`）：构造多帧时变频选信道，对比 raw/MA/EMA-Wiener/逐帧 oracle LMMSE。

### 选择器演进（踩坑记录）
| 方案 | 真实 P1B | 合成 4tap | 问题 |
|------|---------|----------|------|
| 绝对阈值 `floor·gamma` | -15（K=47） | — | filt8 有色噪声高于 floor，压不住 |
| 能量占比 `energy_frac=0.95` | **-45**（K=1） | -15（K=3，漏 tap） | 频选漏弱 tap |
| 能量占比 `energy_frac=0.99` | -16（K=30） | -33（K=4） | 有色噪声尾巴撑大能量预算 |
| **相对峰值 `rel_db=20`** | **-45**（K=1） | **-33**（K=4） | ✅ 两边都对 |

相对峰值阈值对有色噪声鲁棒：flat 下有色噪声虽高于 floor 但远低于巨大单峰 → 被拒；频选多径在峰值 ~20 dB 内 → 保留。

---

## 三、结果

### 3.1 真实 P1B（Gate 1）

| run | raw | data_pdp_wiener | **EMA-Wiener** | oracle | top1 |
|-----|-----|-----------------|----------------|--------|------|
| Legacy `150304` | -10.87 | -12.43 | **-45.12**（K=1，gnz0.10%） | -43.45 | -45.13 |
| MA+Kalman `221806` | -17.44 | -17.45 | **-44.34**（K=1，gnz0.09%） | -38.45 | -45.14 |

α∈{0.9,0.95,0.98,0.99} 都稳定；**Gate 1 目标 ≤-30 dB → 实测 -45，PASS**。

### 3.2 合成多帧频选（泛化性）

`--synth-mf --n-sc 256 --synth-frames 150 --snr-db 15 --ma-win 33 --ema-rel-db 20`

| case | raw | MA | **EMA-Wiener** | oracle | K |
|------|-----|-----|----------------|--------|---|
| flat_1tap | -14.97 | -29.89 | **-38.44** | -38.44 | 1 |
| fsel_2tap | -14.96 | **-3.84（崩）** | **-36.31** | -36.31 | 2 |
| fsel_4tap | -15.02 | **-8.24（崩）** | **-32.98** | -32.98 | 4 |

**三种场景 EMA-Wiener 全部精确追平 oracle**；MA 在频选严重退化（甚至比 raw 差）。证明全场景泛化（待 CDL 真实数据最终确认）。

---

## 四、固化算法参数（供 Phase 3 C 移植）

> ⚠️ 本表为仅 P1B 阶段的初版；经 §5 CDL 验证后**已修正**（见 §5.3）：作用对象改为 raw LS、rel_db 改高（轻裁剪）。以 §5.3 为准。

| 参数 | 初版(P1B) | 修正(全场景, §5.3) |
|------|-----------|--------------------|
| 作用对象 | filt8 输出 | **raw LS（白噪声）** |
| 变换 | N_act 点精确 FFT/IFFT (KISS-FFT) | 同 |
| EMA α | 0.95 | 0.95~0.97，per-(ant,port) 动态分配 |
| 选择器 | 相对峰值 `rel_db=20`（激进，杀 CDL 多径） | **`rel_db` 高(~轻裁剪)** + floor·gamma 守门 |
| 地板 | `tail-mean` | 同；gamma=2 |
| gain | `sig/(sig+floor)`，kept tap | 同；warmup/塌缩 fallback MA |

---

## 五、CDL A–E 离线验证（关键修正，2026-05-29 增补）

CDL 端到端 attach 阻塞（拿不到真实 SRS dump），改用 ray 数据 `data_out/cdl_*` 的 (tau,power) **离线合成**频域信道（按真实 SRS 带宽 1248·30kHz 映射延迟），新增 `run_cdl_offline()`（`--cdl-dir`）。

### 5.1 第一版结果（rel_db=20，沿用 P1B 调参）→ 暴露问题

n_sc=1248, MA win=65, SNR15：

| model | DS(ns) | raw | MA | EMA(rel20) | od_diag(对角天花板) |
|-------|--------|-----|-----|-----------|---------------------|
| cdl_a | 30 | -15 | -24.4 | -17.9 | -25.3 |
| cdl_c | 100 | -15 | -15.8 | -17.4 | -29.3 |
| cdl_e | 260 | -15 | -13.2 | -14.2 | -29.2 |

`rel_db=20` 的 EMA-Wiener 比对角天花板差 **6-13 dB**，rankK 只有 6-18（CDL 能量铺在 30-75 bin）→ **秩裁剪把真实弱多径砍了**。

### 5.2 根因与修正：noise color + 裁剪强度

- 修了 `lmmse_fullband` 的 **circulant→Hermitian Toeplitz** bug（分数延迟下 circulant 错误；整数延迟合成测试没暴露）。修后 genie Toeplitz oracle = -27~-32（但需精确 R，**genie-only，非现实目标**）。
- 新增 **od_diag**（真值 PDP 的对角 Wiener）= 现实可达天花板 = **-23~-29**（对角架构够用）。
- **rel_db=60（基本不裁剪）**：

| model | MA | EMA(rel20) | **EMA(rel60)** | od_diag |
|-------|-----|-----------|----------------|---------|
| cdl_b | -11.9 | -15.9 | **-20.26** | -23.4 |
| cdl_c | -15.8 | -17.4 | **-27.96** | -29.3 |
| cdl_e | -13.2 | -14.2 | **-26.73** | -29.2 |

EMA(rel60) 逼近 od_diag（差 1.3-3 dB），但**真实 P1B（filt8 有色噪声）rel60 → -14.87（K=82 崩）**。

### 5.3 全场景结论（修正 §2.2 理解）

1. **对角延迟域 Wiener 是全场景可行的**：白噪声输入下，软增益+轻裁剪在 flat(-45) 和 CDL(-20~-28) 都逼近 od_diag。
2. CDL 之前差的原因不是算法，是 **`rel_db=20` 为 filt8 有色噪声 P1B 过拟合**，激进裁剪杀死 CDL 多径。
3. **真凶 = filt8 有色噪声**：flat 下逼着激进裁剪，激进裁剪杀 CDL 多径 → 冲突。
4. **解法 = 频域 Wiener 作用在 raw LS（白噪声）而非 filt8 输出**：单一软增益+轻裁剪即可全场景，无需 flat/频选二选一。
5. genie Toeplitz oracle 非公平目标；**data-Toeplitz-LMMSE 经验自相关版发散/脆弱**，放弃；对角架构 + 白噪声输入是正路。
6. 标定提醒：合成数值随 n_sc 变（n_sc 同时是带宽采样与 FFT 尺寸），定性结论稳健，绝对 dB 以 n_sc=1248 为准。

## 六、raw-LS 验证（passthru, 2026-05-29 增补）→ 推翻"高 rel_db 通吃"

跑了 `SRS_ESTIMATOR=passthru` P1B run（`q4_sweep_20260529_163146`），dump raw 稀疏 LS（白噪声）。

- **数据质量坑**：该 run 仅前 ~92 帧（Q1）干净（raw LS=-10.2 dB），attach-stable 窗口后转 dynamic，帧 92+ 的 dump scale 爆掉（+30 dB，Q4-Q1 漂移 +42.5 dB）。须 `--max-frames 90` 只用 Q1。
- 前 90 帧 raw LS=-9.78，top1/oracle=-45/-44.9（信息充足）。
- **flat 上扫 rel_db**：20→**-42.34** / 25→-22.2 / 30→-18.8 / 40→-17.5。flat 要 **低 rel_db(20)**。
- 而 §5.2 CDL 要 **高 rel_db(60)**。**两者最优相反 → 固定 rel_db 不能全场景**（上轮结论作废）。

### 修正结论（覆盖 §5.3 第 4 点）
1. **"作用在 raw LS"对 flat 成立**：白噪声 + rel_db=20 → -42。
2. **必须自适应裁剪**：固定 rel_db 无解。最自然方案 = **噪声地板阈值 `pdp>floor·gamma`**（flat 自动 1 tap、CDL 自动几十 tap，天然随信道自适应）。其历史失败均源于**地板估计**（filt8 有色噪声、passthru K_TC 混叠），非思路问题。
3. **离线代理已达极限**：filt8 输入=有色噪声，passthru=稀疏导频有 K_TC 混叠伪峰。真实 C 用 **dense-pilot IFFT**（`nr_srs_mmse.c` 已有）→ 白噪声 + 稠密，无混叠。

## 七、下一步（Phase 1.5：自适应地板选择器）

1. 核心 = 在**白噪声稠密延迟谱**上做**鲁棒噪声地板估计 + `floor·gamma` 自适应保 tap**（替代固定 rel_db）。
2. 选项 A：搭更真实的离线 harness（dense-pilot IFFT 模拟 K_TC，去混叠）确定地板/gamma；
   选项 B：直接进 C，复用 `nr_srs_mmse.c` 的 dense-pilot IFFT + 自适应地板选择器，端到端验证。
3. CDL 真实数据仍待 attach 打通（Phase 2 最终确认）。

---

## 八、真正的自适应选择器（Phase 1.5，2026-05-29 完成于白噪声稠密输入）

去掉固定 `rel_db`，改为**纯噪声地板驱动**：

- `robust_noise_floor(pdp, k=3)`：从中位数出发，迭代取 `pdp < k·floor` 的均值作噪声地板（信道 tap 稀疏、多数 bin 是噪声 → 收敛到噪声均值，无需任何手设阈值）。
- 选择：`keep = pdp_ema > floor·gamma`；soft Wiener gain `sig/(sig+floor)`。
- K 随信道**自动伸缩**：flat→1，频选→几十。开关 `--ema-adaptive`。

### gamma 扫描（白噪声稠密；synth n_sc=256，CDL n_sc=1248）

| gamma | flat | 2tap | 4tap | cdl_a | cdl_c | cdl_e |
|-------|------|------|------|-------|-------|-------|
| 2 | -32.6 | -32.2 | -30.8 | -23.4 | -26.0 | -22.8 |
| 3 | -35.3 | -34.0 | -32.3 | -23.5 | -27.2 | -22.7 |
| **4** | **-37.1** | **-34.9** | **-32.8** | **-23.3** | **-27.8** | **-22.5** |
| 6 | -38.3 | -35.5 | -33.1 | -22.6 | -27.5 | -21.8 |
| 天花板 | -38.5 | -36.3 | -33.3 | -25.2(od) | -29.6(od) | -24.7(od) |

**gamma=4：全场景都在天花板 ~2 dB 内，K 自动伸缩。** 但 gamma=4 仍是手调。

### 全自适应 auto-gamma（零手调门限，`--ema-auto-gamma`）

gamma 也从数据推：`adaptive_floor_threshold()` 实测噪声 tap 的 `均值²/方差` → 有效自由度 M → Wilson-Hilferty chi²(2M) 在 `1-p_fa` 的分位点 = gamma（`p_fa=1/N`，含义"全带期望≤1虚警"）。floor、gamma **都从数据来**。

| auto-gamma | flat | 2tap | 4tap | cdl_a | cdl_c | cdl_e |
|------------|------|------|------|-------|-------|-------|
| EMA | -35.4 | -33.9 | -32.4 | -23.6 | -28.1 | -27.8 |
| K(全自动) | 4 | 5 | 6 | 53 | 44 | 38 |
| 天花板 | -38.3 | -35.8 | -33.1 | -25.2 | -29.3 | -29.2 |

**与手调 gamma=4 几乎等效（CDL 相同，flat 差 ~2dB），但无 gamma、无 rel_db。** 参数清单：floor/gamma 数据驱动；`p_fa=1/N` 是规格非旋钮；`floor_k=3` 是通用 3σ 鲁棒常数。真正的无参数自适应达成（白噪声稠密输入）。

### 局限（与 C 端衔接）
- 在**真实 filt8 输出**（有色噪声）上自适应仍崩（K=665）：单标量地板对有色噪声无效。
- 在 **passthru 稀疏导频**上也崩（K_TC 混叠）。
- 两者都**不是代表性输入**。真正自适应**必须配 C 端 dense-pilot IFFT（白噪声+稠密、无混叠，`nr_srs_mmse.c` 已有）**。这是 Phase 3 的输入约定。

> **2026-05-29 后续修正（详见 `0529_True2D_离线判定日志.md`）**：本日志的"对角延迟域 Wiener"经真 2D 判定后**不再是频率阶定稿**——窗口 Toeplitz 频域 LMMSE（= C 已有 `nr_srs_mmse_freq_filter`）稳定优于对角 1–3 dB；真 2D(全 Toeplitz) 比对角高 5–7dB 但受 R 估计偏差/ESPRIT 脆弱性封顶。频率阶改用窗口 Toeplitz，对角路线归档为对照。

## 九、归档

| 用途 | 路径 |
|------|------|
| 离线脚本 | `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/test_srs_2d_offline.py` |
| 真实 P1B Legacy | `DevChannelProxyJIN/logs/q4_sweep_20260528_150304/snr_15dB` |
| 真实 P1B MA+Kalman | `DevChannelProxyJIN/logs/q4_sweep_20260528_221806/snr_15dB` |
| CDL ray 数据 | `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/data_out/cdl_{a..e}` |
| 复现命令(真实) | `python3 test_srs_2d_offline.py --run-dir <dir> --tol 20 --ema-rel-db 20` |
| 复现命令(合成) | `python3 test_srs_2d_offline.py --synth-mf --n-sc 256 --synth-frames 150 --snr-db 15 --ma-win 33 --ema-rel-db 20` |
| 复现命令(CDL) | `python3 test_srs_2d_offline.py --cdl-dir data_out --n-sc 1248 --synth-frames 50 --snr-db 15 --ma-win 65 --ema-rel-db 60` |
| 复现命令(CDL+oracle) | `python3 test_srs_2d_offline.py --cdl-dir data_out --cdl-lmmse --n-sc 256 --synth-frames 40 --snr-db 15` |
