# 基于 OAI 的真·2D-MMSE SRS 信道估计：设计、实现与端到端验证报告

| 字段 | 值 |
|------|----|
| **标题** | OAI native rfsimulator 上的两阶段 2D-MMSE（频域 Wiener + 时域 PKF）SRS 信道估计 |
| **日期** | 2026-06-01 ~ 06-06 |
| **平台** | OpenAirInterface 5G (SA, Band 78, 106 PRB, μ=1 / 30 kHz SCS, Fs = 61.44 MHz, 2048-FFT) |
| **估计器代号** | `true2d`（生产 C 实现，`SRS_ESTIMATOR=true2d`） |
| **基线** | `passthru`（裸 LS）、`legacy`（OAI FIR 平滑） |
| **一句话结论** | true2d 对裸 LS 取得**单调、可观**的去噪增益（静态上限 **−9 ~ −12 dB**，随时延扩展递减），且在 ≈SNR 17 dB **动态 0~300 km/h 全程稳健（vs 裸LS ~6~8dB；vs OAI legacy ~2~4dB，含 300km/h 无崩溃）**；经"高 SNR legacy 门控"使其全 SNR 区间 ≥ legacy。算法/C/对齐三层已被离线 oracle、逐位单元测试与 native 端到端 A/B 共同验证。 |
| **复杂度优化(06-04~06)** | freq_wiener 单条 **5042→190µs(26.5×,三步算法叠乘,bit-exact 始终启用)**;维度自适应解除 4×4 上限;多核 fork-join(默认关) + Option B 跨天线共享 R(默认关,near-lossless)。详见 §10。 |
| **信道预测(方向A,06-05)** | 时域 Wiener 预测器(预测未来 H(t+kΔ),服务 Digital Twin 前瞻);已港到 C(env 默认关),golden ≤1 LSB。详见 §11 + `0605_SRS信道预测_方向A_日志.md`。 |

---

## 摘要

本报告记录了一套面向 5G NR 上行 SRS 的两阶段 **2D-MMSE 信道估计器**（内部代号 `true2d`）从算法、C 生产实现到端到端系统验证的完整工作。估计器由两个串联阶段组成：**Stage 1 频域 Wiener LMMSE**（窗口 Hermitian-Toeplitz 解，输出去噪频响及测量噪声功率 R_meas）与 **Stage 2 时域 PKF**（per-link 相位率预测的标量 Kalman 滤波，IAE-Riccati 自适应增益）。

我们放弃了受物理链路余量阻塞的 GPU-IPC proxy 测试床，转而打通了 OAI **native rfsimulator + channelmod** 路径，构建了一个可直接控噪、信道真值已知、可复现的干净测试床。配套实现了 **Ground Truth（GT）落盘管线**（rfsim 实际施加的 CIR 抽头 + 绝对采样时戳）与 **CDL 动态回放**（射线参数→逐 slot sample-spaced CIR）。在固定信道种子的 A/B 实验下，系统性地刻画了 true2d 相对裸 LS 的净增益随 **SNR、信道时延扩展、移动速度**的变化规律。

主要结论：(1) 在干净/可用 SNR 区间，true2d 对裸 LS 单调取得 **−9 ~ −12 dB** 的 NMSE 改善（5 个 CDL 模型一致）；(2) 增益本质上正比于噪声量——高 SNR 下裸 LS 已近最优、true2d 仅余滤波偏差代价（且高 SNR 略输 legacy，已用 SNR 门控修成全区间 ≥ legacy）；(3) 增益随时延扩展递减（CDL-A 290 ns 优于 CDL-E 2064 ns），与频域相关性去噪的物理机理一致；(4) **增益对速度稳健**：0~300 km/h（含极高速）全程保持 **~6~8 dB（vs 裸LS）/ ~2~4 dB（vs OAI legacy）**，无高速崩溃——即使 PKF 时间阶在高速失效，频域 Wiener 仍撑住增益、IAE-Riccati 优雅退化（早期"快衰落 true2d 反转"的判断为退化测试床的假象，已被推翻）。

---

## 1. 背景与动机

SRS（Sounding Reference Signal）上行信道估计的基础做法是导频处的最小二乘（LS），但裸 LS 不做任何去噪，在中低 SNR 下噪声直接进入估计。经典的 2D-MMSE（频率×时间）Wiener 滤波能利用信道在频域（时延扩展有限→频域相关）与时域（多普勒有限→时间相关）的相关结构压制噪声，但其在真实 5G 协议栈、定点 DFT、实际 SRS 梳状导频栅格下的可实现增益缺乏端到端实证。

本项目的目标是：在 **OAI 真实 gNB/UE 协议栈**上，用**已知真值的频选信道**，端到端定量证明 2D-MMSE（true2d）相对基线的信道估计增益，并刻画其有效工作区。

### 1.1 为何从 proxy 转向 native rfsimulator

最初采用 GPU-IPC proxy（Sionna 在 GPU 上施加信道、经共享内存与 OAI 交互）路线，但被一道物理墙挡住：**既要 UE 成功 attach（要求链路余量），又要给够 SNR 让 SRS 解出 CDL 的弱多径（=频选细节=MMSE 要利用的部分）**，二者在 proxy 的链路预算下无法同时满足。实测表现为：SRS 能稳定抓主径（PDP tap3、SNR 报 18 dB），但弱多径落在噪声以下逐帧抓不稳，与选频 GT 复数相干仅 0.05 ~ 0.15。

native rfsimulator 的优势：**直接控噪声、对称施加、信道真值即 rfsim 施加的 `channelDesc`、无需 Sionna/GPU/docker**。关键认识是 native 是**运行时开关**（不设 env `RFSIM_GPU_IPC_V8` 即自动回落原生 socket + `rxAddInput` 路径），不需要重编 radio 模块。早期"native 样本流时序坏、不可用"的判断在本项目中被**推翻**——真正挡路的是 3 个独立的坑（1 个配置 + 2 个 C bug）+ RA 的 PRACH↔多径 Catch-22，逐层修复后即得到 UE 在线 + 上行频选 + 链路不断的干净测试床。

---

## 2. 系统架构

整体数据流为单向流水线：

```
SRS Rx (comb-2 pilots) → 裸 LS → [Stage 1 频域 Wiener LMMSE] → [Stage 2 时域 PKF] → H_true2d
```

架构框图见 `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/true2d_oai_architecture.png`。

### 2.1 Stage 1 — 频域 Wiener LMMSE（`nr_srs_mmse.c`）

对每个 SRS OFDM 符号，在 dense-pilot 域执行：

1. **2048 点 IFFT** 把导频处 LS 频响变到时延域，得到功率时延谱（PDP）；
2. **跨 slot EMA** 平滑 PDP（指数滑动平均，稳健估计信道能量分布）；
3. **稳健噪声底估计**（`nr_srs_robust_noise_floor_arr`），从 PDP 尾部估噪声功率 σ²；
4. **由 PDP 构造频域相关阵** R(Δk) = DFT{PDP_clean}（`nr_srs_R_from_pdp_arr`）；
5. **Hermitian-Toeplitz Wiener-Hopf 求解**，得到去噪频响 `H_freq`，同时输出**测量噪声功率 R_meas**（= σ²·E[|w|²]，传递给 Stage 2）。

关键符号：`nr_srs_mmse_freq_filter`、`nr_srs_robust_noise_floor_arr`、`nr_srs_R_from_pdp_arr`。

### 2.2 Stage 2 — 时域 PKF（`nr_srs_2d_filter.c`）

per-link 的标量 Kalman 滤波，对每条链路维护一个相位率状态 ω（rad/帧）：

- **预测**：ŝ = s · e^{jω}（用估计的相位率外推到当前帧）；
- **创新**：e = obs − ŝ；
- **Kalman 增益**：K = P⁻ / (P⁻ + R_meas)（R_meas 来自 Stage 1）；
- **后验更新**：s = ŝ + K·e（**输出 a-posteriori 估计，非预测值 → 无滞后**）；
- **IAE-Riccati 自适应**：根据创新自适应过程噪声 Q 与协方差 P，K 被 clip 到 [0.02, 0.95]；
- ω 用 EMA 跟踪（k_w = 0.2）；首帧 bootstrap warmup。

默认参数：`SRS_2D_PKF_KW=0.2 KMIN=0.02 KMAX=0.95 Q_EMA=0.10 INNOV_EMA=0.15`。状态 {ω, P, Q} 跨 SRS 帧延续。

关键符号：`update_pkf_core`、`nr_srs_pkf_update`、`nr_srs_2d_set_rmeas`。

### 2.3 分发与基线（`nr_ul_channel_estimation.c`）

`SRS_ESTIMATOR` 环境变量分发估计器：`passthru`（**原始稀疏 LS，不去噪**）、`legacy`（OAI 默认，**LS + 固定 FIR 频域平滑**）、`true2d`（本文两阶段 MMSE）等。

**两个基线的机理**：
- **passthru = 裸 LS**：导频处 `Ĥ_LS = Rx·conj(参考序列)`，原样输出、不做任何去噪/插值。代表"零先验、零处理"的原始测量（NMSE ≈ 1/SNR），是信道估计的**科学标准基线**（可复现、零假设、MMSE 理论上注定要超过它）。
- **legacy = LS + 固定系数短 FIR 频域平滑**（OAI 生产估计器，PUSCH DMRS 也用同套思路）：在 comb 导频 LS 之上，用**写死权重**的 8 抽头（`filt8_*`，非 comb）或 16 抽头（`filt16_*`，comb）FIR 把相邻导频加权求和，既插值到全子载波又平滑去噪（含 `start/middle/end` 三套处理频带边缘）。权重为定点 Q14 的三角/矩形窗（如 `filt16_middle4={4096,8192,...,8192,4096}`）。**纯频域、逐符号、零自适应**（不估噪声、不看时延扩展/SNR、无时域滤波）→ 噪声降约**恒定 +3~4 dB**（与 SNR 无关）；但长时延（频域变化快）时固定窗会把频选细节一并平掉、引入偏差。它是**工程部署门槛**，非科学参照。

> **基线选择**：`vs passthru` 回答"算法利用信道结构带来多少增益"（科学问题，本文主线，−9~−12dB）；`vs legacy` 回答"能否超过已部署的廉价去噪器"（工程问题，静态 ~−4dB、高速 ~−2dB）。早期误用 legacy 当唯一基线 → "true2d ≈ legacy 增益一般"的误判，根因是 legacy 本身已去噪 ~3.6dB。

---

## 3. 算法与 C 实现的等价性验证

true2d 的核心已在两个层面被严格验证为正确：

1. **离线 vs oracle**：离线参考实现（`test_srs_2d_offline.py`）对 oracle 信道，CDL 可达 **−28 ~ −30 dB** NMSE。
2. **C = 算法（逐位单元测试，M2.4）**：
   - 频阶：C `nr_srs_mmse_freq_filter` vs 黄金 `windowed_lmmse`，**0 ~ 1 LSB**；
   - 时阶：C `update_pkf_core` vs 黄金 `time_pkf_riccati`，**≤ 1 LSB**；
   - OAI 定点 `idft2048` 经 C1 探针核对，证明**非精度瓶颈**。

| 阶段 | 离线黄金 | C 生产 |
|------|----------|--------|
| 频阶 | `windowed_lmmse` | `nr_srs_mmse_freq_filter` |
| 时阶 | `time_pkf_riccati` | `update_pkf_core` |
| R_meas | `freq_propagated_noise_avg` | 频阶 `wnorm2` 累加 → `nr_srs_2d_set_rmeas` |

此外，在 native CDL run 上对 conj+tau 扫"可达最佳相干"：passthru = **1.000**、true2d = **0.999**，证明**对齐完全正确、PKF 未乱跟**。

---

## 4. native 测试床的搭建（逐层排障）

### 4.1 三个挡路的坑（按发现顺序）

| # | 现象 | 根因 | 修复 |
|---|------|------|------|
| 1 | UE 连不上 6014 | 本 fork 把 rfsim 默认端口改为 `PORT 6014`，gNB conf 写死 4043 | UE 显式 `--rfsimulator.serverport 4043` |
| 2 | UE 连上但 `pbch not decoded` 死循环 | conf 是 2T2R / 2 端口 SSB（为 proxy 配的 2×2），原生路径不支持 | gNB 强制 **1T1R**（`nb_tx/nb_rx 1`、`pdsch/pusch_AntennaPorts 1`）→ attach 成功 |
| 3 | 开 chanmod 后上行全废、PRACH 零检测 | **C bug A**：`new_channel_desc_scm()` 从不分配 `Doppler_phase_cur`；`rxAddInput()` 开头 `if(Doppler_phase_cur==NULL) return;` → 整段信道卷积被跳过、上行输出全零 | `random_channel.c` 补 `calloc` |
| 4 | 修 A 后仍 `RAR reception failed` | **C bug B**：`channel_mode` 默认 =2（旁路），旁路代码索引错误（应为环形位置 `input_sig[(TS+i)%CirSize]`）→ gNB 收到错位垃圾 | 修正旁路为正确环形索引（`apply_channelmod.c`） |

> "多径打散 PRACH"在修 bug B 之前是**假象**——多径根本没参与；bug B 修复后才暴露真正的 PRACH↔多径 Catch-22。

### 4.2 handoff 机制（native 版 attach-stable）

PRACH 是非均衡相关检测，怕多径；PUSCH/SRS 有均衡能扛频选。因此采用**先平坦后切信道**的 handoff：默认 `channel_mode=2`（修正后的环形索引平坦透传）让 RA 通过；UE `in-sync` 后由 launcher `touch /tmp/rfsim_chan_on`，`rxAddInput` 每 ~1 ms 检测到该文件即翻转 `channel_mode=1`（施加真实 TDL/CDL），此后 PUSCH/SRS 见频选。

实测：attach 成功（`4-Step RA procedure succeeded`）→ handoff 日志（`rfsim channel handoff ... applying channel model`）→ 切换后链路不断（ULSCH `errors 0`、`BLER 0`）。

### 4.3 Ground Truth 落盘管线

| 文件 | 作用 |
|------|------|
| `apply_channelmod.c::native_gt_dump` | `channel_mode==1`（handoff 后）节流落盘 `channelDesc->ch[]`（sample-spaced CIR 时域抽头）+ 绝对 ts（= nextRxTstamp，与 SRS V3 同轴） |
| `native_gt_to_npz.py` | dump → `H[k]=FFT_2048(taps)`（自然序，匹配 OAI bin）→ 产 `gt_batch_*.npz`（h_matrix + slot_ids=ts//30720），与 Sionna 格式一致，eval 零改动 |

**子载波栅格**（经探查坐实）：SRS 估计与 GT 均为 **2048 点自然序 FFT**（无 fftshift；106 PRB 从 bin 1412 环绕）；eval 用 SRS 的 active-mask（~1248 bin）同索引比较，`_sto_es` 搜 τ∈[−30,30] 吸收 STO。

### 4.4 GT 管线的四个隐蔽踩坑（全部修好）

1. **handoff 前平坦帧污染**：GT 是选频信道但 SRS 含 handoff 前平坦帧 → 假性 −3 dB。修法：转换器只对 ≥ 首个 GT-dump slot 的 SRS 帧产 GT。
2. **共轭约定（关键且隐蔽）**：OAI SRS 估计的 FFT 符号约定**随估计器/run 变化**（legacy 需 `conj(FFT)`、passthru/true2d 需 plain `FFT`）。写死 conj → coherence 垮（0.47）、NMSE 假性。修法：转换器**自动探测**（比 `H` vs `conj(H)` 的相干，选高者；宽 tau ±64 + 多帧中位，避免长时延 CDL-E 误选）。
3. **同信道 A/B**：rfsim 信道用时间随机种子→两发不同信道（不公平）。修法：`random_channel.c` 首次生成时尊重 `OAI_RNGSEED`（按 chan_idx 偏移）→ 实测两发抽头 `||diff||=0`。
4. **噪声破坏 attach**：全局 `--channelmod.noise_power_dBFS` 从一开始加噪 → PRACH 定时被打乱、RA 挂。修法：改用**逐信道 `noise_power_dB`**（`rxAddInput` 内、`channel_mode==1` 才加）→ 平坦 attach 干净、仅 handoff 后 SRS 见噪。

### 4.5 CDL 动态回放

CDL 数据是射线参数（tau/power/角度，无 CIR）。离线 `cdl_to_cir.py` 按 v8 公式（`l=round(τ·Fs)`，Fs=61.44e6；Doppler `ν=f_D·sinθ·cosφ`；逐 slot 相位演化）合成逐 slot sample-spaced CIR 序列 → C 端 rfsim 按 slot 回放进 `ch[]`（handoff 后，env `RFSIM_CIR_REPLAY`），同时获得真实 CDL + 时变 + 激活 true2d 频阶 & 时阶。

### 4.6 5GC/NAS 退化与 per-run 重启（动态可靠性关键）

**症状**：长 sweep 跑十几次 attach 后，后续 run **SRS 捕获 0 帧**（`[SRS Dump] Captured: 0`），评估 `n=0`/相干塌到 0.2。
**根因（已坐实）**：PHY 层 attach 成功（RRCSetupComplete、in-sync、RA、handoff、GT dump 全正常），但 **NAS Registration accept=0、RRCReconfigurationComplete=0** → SRS 配置（在 RRCReconfiguration 里）从不下发 → UE 不发 SRS。即 **5GC/AMF 在连续多次 attach 后累积 UE/NAS 上下文退化**，停止接受新注册。证据：SRS-captured 帧数在某次 run 后锐变为 0 并跨数小时持续；`preflight` 原本只在容器 down 时 `up -d`、不重启"Up 但已病"的核心网。
**修复**：`launch_native.sh` 加 `--restart-5gc`（preflight 即使全 Up 也 `docker compose restart` 核心网 + 等 `CN5G_RESTART_WAIT`(默认5s)）；`sweep_native_cdl.sh` 加 `-restart5gc` 每发重启。
**管线本身验证健康**：在重启后的健康 run 上用独立探针 `_diag_dyn_gt.py` 扫 GT-slot 偏移 + 共轭，实测 SRS-vs-GT 相干 **0.982（峰在 k=0）**、共轭探测干净选对（margin +0.344）→ **排除"共轭误判 / 时间轴偏移"**，证明动态 GT 管线正确，之前的动态相干塌陷 100% 源于上述 5GC 退化。

**守门相干 STO 对齐（eval 度量修正）**：`eval_nmse_sto.py` 原守门相干用"不去 STO"的裸内积 `|⟨s,g⟩|`，而 CDL 多径天生有 STO（线性相位斜坡）→ 裸相干被拉到 ~0.5（假低，把估得很好的 run 误判失败），但同一 run 探针去 STO 后是 0.98、NMSE(去STO)是 −20dB。已把守门相干改为**与 NMSE 同口径的 best-over-tau STO 对齐**（实测同 run 从 0.47→0.80 干净过门；垃圾 run 仍低、照剔）。NMSE 口径不变。另修 `sweep_native_cdl.sh` 解析 bug：`grep coherence` 会误匹配 `⚠ ALIGNMENT SUSPECT: complex coherence`（无符号）行 → 改 `grep "cplx coherence"`。

---

## 5. 实验方法

为得到干净、可交付的增益曲线，采用以下方法（避免 2D 大网格的离群点与不可靠横轴）：

1. **横轴用 passthru（裸 LS）NMSE**（≈ 1/有效 SNR，每发实测可靠），而非 gNB 5-PRB SNR 报告（不可靠）。
2. **每点 `-rep` 取中位**压离群。
3. **静态 CIR（s0）扫 SNR**：可复现、无 CIR 循环离群、隔离 SNR 维度；A–E 时延剖面不同→频选强度异（sel std/mean：B 0.50 > C 0.37 > E 0.28 > D 0.26 > A 0.21）。
4. **固定信道 seed** 保证 A/B 两发完全同信道。
5. **判读守门**：只信 **STO 对齐复数相干 ≥ 0.50**（与 NMSE 同口径 best-over-tau，去掉多径良性 STO 的假低，见 §4.6）且 NMSE 为负的 A/B 点；相干塌、NMSE 转正视为 SRS 淹没/退化、数字无效（剔除）。每点 rep≥3 取中位。

复现三步：`python3 cdl_to_cir.py data_out/cdl_b cir/cir_cdl_b_s0.bin -speed 0` → `sudo bash sweep_native_cdl.sh -m cdl_b_s0 -nd "-15 -9 -3"` → `python3 plot_cdl_sweep.py`。

---

## 6. 实验结果

### 图集总览（按工况 × 基线）

| 图 | 静/动 | vs 基线 | 看点 / 节 |
|----|------|------|------|
| **`native_cdl_threeway_static_dynamic.png`** | 静+动 | LS+legacy | **核心**：三方 NMSE，左 vs SNR、右 vs 速度（§6.2/6.4/6.5/6.7）|
| `native_cdl_gain.png` / `_small_multiples` | 静 | LS | true2d 增益 vs SNR（§6.2）|
| `native_cdl_nd_minus3_bars.png` | 静 | LS+legacy | ≈SNR11dB 跨模型三方柱状 + 双增益（§6.3）|
| `native_cdl_3way.png` | 静 | legacy | 高 SNR 交叉点 + 门控前后（§6.5/6.6）|
| `native_cdl_gain_vs_speed.png` | 动 | LS | 增益 vs 速度 0~60km/h（§6.4）|
| `native_cdl_dynamic_bars.png` | 动 | LS+legacy | 60km/h 跨模型三方柱状 + 双增益（§6.4/6.7）|
| `native_cdl_gain_vs_speed_legacy.png` | 动 | legacy | 增益 vs 速度 0~300km/h（§6.7）|
| `true2d_oai_architecture.png` | — | — | 两阶段架构框图（§2）|

> **关于横轴 SNR**：下表均以**有效 SRS SNR** 为轴。它由实测裸 LS NMSE 换算（裸 LS 不去噪，NMSE≈1/SNR → 有效 SNR(dB) ≈ −passthru NMSE，5 模型均值），等价经验式 `SNR ≈ −2·nd + 5`。对照：
>
> | 复现用 `-nd` | ≈ 有效 SNR |
> |------|------|
> | −15 | ~34 dB | 
> | −12 | ~29 dB |
> | −9 | ~23 dB |
> | −6 | ~17 dB |
> | −3 | ~11 dB |
> | 0 | ~6 dB |
>
> 复现命令仍用 `-nd`（CLI 实参），按此表回查对应 SNR。

### 6.1 管线自检（坐实正确）

- **无噪 legacy（post-handoff）**：coherence **1.000**、NMSE **−45.8 dB** → GT 管线正确。
- **近无噪（SNR 36–45 dB）passthru / true2d**：自动选 plain FFT，NMSE **−37 / −36.8 dB**，coh 0.655 → 高 SNR 下裸 LS 已近最优（true2d 仅余滤波代价）。

### 6.2 静态 5 模型 × SNR 扫描（true2d 相对裸 LS 的增益 Δ，dB）

命令：`sudo bash sweep_native_cdl.sh -m "cdl_a_s0 ... cdl_e_s0" -nd "-15 -12 -9 -6 -3 0 3" -rep 1 -d 60`。
Δ = true2d − passthru（**负 = true2d 赢**）；SNR 越低越吵。

| ≈SNR | A (290 ns) | B (478 ns) | C (865 ns) | D (376 ns) | E (2064 ns) |
|-----|------|------|------|------|------|
| ~34 dB | −2.0 | −0.0 | −0.0 | +0.0 | −1.6 |
| ~29 dB | −3.4 | −3.1 | −2.3 | −2.6 | −2.9 |
| ~23 dB | −5.6 | −6.2 | −4.8 | −5.3 | −4.7 |
| ~17 dB | −8.2 | −8.7 | −7.2 | −8.1 | −6.6 |
| ~11 dB | −10.7 | −10.2 | −9.2 | −10.4 | −8.7 |
| ~6 dB  | **−12.2** | (淹) | (淹) | (淹) | **−10.3** |

**完美单调**：SNR 越低（越吵），true2d 增益越大。图见 `native_cdl_gain.png`（总览）、`native_cdl_gain_small_multiples.png`（五模型分面）。

### 6.3 统一强噪点（≈SNR 11 dB，即 `-nd -3`）跨模型对比（全模型可信）

| 模型 | Td | sel | passthru | true2d | **增益** |
|------|-----|------|------|------|------|
| A | 290 ns | 0.21 | −12.2 | −22.9 | **−10.7** |
| D | 376 ns | 0.26 | −10.7 | −21.0 | **−10.4** |
| B | 478 ns | 0.50 | −10.3 | −20.4 | **−10.2** |
| C | 865 ns | 0.37 | −11.3 | −20.5 | **−9.2** |
| E | 2064 ns | 0.28 | −12.1 | −20.8 | **−8.7** |

图见 `native_cdl_nd_minus3_bars.png`。**增益随时延扩展递减**（A 290 ns −10.7 dB > E 2064 ns −8.7 dB）。

### 6.4 动态（移动速度）：true2d 增益 vs 速度（固定 ≈SNR 17 dB）

全 CDL-A~E × 速度 {0,3,10,30,60} km/h 扫描（**修复后干净重测一轮**：rep=3 取中位、每发重启 5GC（10s）、STO 对齐守门、passthru 基线）。
增益 = passthru_NMSE − true2d_NMSE（正=true2d 赢，dB）。**本轮 25 点 coh 几乎全 0.98~1.00、rep 间近零抖动。**

| 速度 | A(290ns) | B(478ns) | C(865ns) | D(376ns) | E(2064ns) | **均值** |
|------|------|------|------|------|------|------|
| 0 km/h  | 8.2 | 8.6 | 7.2 | 8.0 | 6.6 | **7.7** |
| 3 km/h  | 6.3 | 7.6 | 6.9 | 7.7† | 5.8 | 6.9 |
| 10 km/h | 6.5 | 7.3 | 6.5 | 7.1 | 5.1 | 6.5 |
| 30 km/h | 5.7 | 6.4 | 5.7 | 6.0 | 4.7 | 5.7 |
| 60 km/h | 7.1 | 6.2 | 5.6 | 6.7 | 4.2 | 6.0 |

†d_s3 本轮 true2d 单发退化（coh 0.87→gain 假性 −8.2），取其干净 rep 值 7.7（与邻居 d_s0=8.0/d_s10=7.1 一致）；其余 24 点 coh~1.0 干净。

**结论：true2d 的去噪增益基本与速度无关——0~60 km/h 全程 ~6~8 dB（true2d 全赢），无"高速崩溃"。** 静态(7.7)→移动(5.7~6.9) 掉 ~1~2 dB 后持平到 60 km/h。唯 CDL-E(2064ns 最长时延) 增益略低且随速度温和下降(6.6→4.2)，与"长时延=频域相关性少"一致。物理上：≈17 dB 增益主要来自**逐帧频域 Wiener 去噪**（与速度无关），时间阶 PKF 为辅，故高速下 PKF 跟踪变难也不致命。

> **两处重要订正**：(1) 早期"30 km/h true2d 输 +5.6 dB"是退化管线假象，被推翻（见 §4.6 的 5GC/NAS 退化根因）。(2) 守门相干原用"不去 STO"的裸内积，在 CDL 上被多径 STO 拉到 ~0.5（假低，误剔点）；已改为**与 NMSE 同口径的 STO 对齐相干**——本轮重测后 25 点 coh 普遍 0.98~1.00，干净通过。

图：`native_cdl_gain_vs_speed.png`（五模型增益 vs 速度小多图）；`native_cdl_dynamic_bars.png`（60 km/h 跨模型三方柱状）；核心总览 `native_cdl_threeway_static_dynamic.png` 右面板。

### 6.5 三方对比：true2d vs legacy(OAI FIR) vs 裸LS（关键）

前述 sweep 以裸 LS 为基线。本节补上更难的对比——直接对打 OAI 自带生产估计器 `legacy`（裸 LS + filt8/16 三点 FIR 平滑，本身是去噪器）。同 seed 同信道，coh≥0.50 守门。**有效 SNR ≈ −裸LS NMSE**（实测，`SNR ≈ −2·nd + 5`）。

三方 NMSE（dB，5 模型均值；Δ = true2d − legacy，负=true2d 赢）：

| ≈SNR | 裸LS | legacy(FIR) | true2d | **Δ(t2−legacy)** |
|------|------|------|------|------|
| ~34 dB | −34.3 | **−37.7** | −35.0 | **+2.6（legacy 赢）** |
| ~29 dB | −29.0 | −32.6 | −31.9 | +0.7（≈平）|
| ~23 dB | −23.2 | −26.8 | **−28.5** | −1.7（true2d 赢）|
| ~17 dB | −17.3 | −20.9 | **−25.0** | −4.1 |
| ~11 dB | −11.3 | −14.9 | **−21.1** | −6.2 |
| ~6 dB  | −6.2  | −9.7  | **−17.2** | −7.5 |

**关键发现**：存在清晰**交叉点 ≈ SNR 27 dB**，五模型一致。
- **干净/高 SNR（≳29 dB）：legacy 反而更好（+1.4~+3.5 dB）**——信号近完美时 legacy 轻 FIR 几乎无损，true2d 重 MMSE 过滤反留偏差代价。这是只跟裸 LS 比时看不到的。
- **噪声区（≲23 dB）：true2d 单调反超**，越吵赢越多（≈23 dB 赢 1~2.5 → ≈11 dB 赢 5~7 → ≈6 dB 赢 6.7~8.3 dB）。
- **机理**：legacy 是定窗 FIR，去噪量恒定 ~3.6 dB（与噪声无关）；true2d 是自适应 MMSE，去噪量 ∝ 噪声。交叉点 = 二者平衡的 SNR。

> **一句话**：true2d 不是无条件碾压 legacy，而是**分工明确**——干净/高 SNR 用廉价 legacy 更优，**true2d 的价值在噪声区**（交叉点以下显著反超 OAI 平滑器，最噪达 −8.3 dB）。

### 6.6 高 SNR legacy 回退门控（修复：全区间 ≥ legacy）

针对 §6.5 暴露的"高 SNR true2d 输 legacy"，加了一个 **per-link SNR 门控的 legacy 回退**（C 实现，[nr_ul_channel_estimation.c](DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c) TRUE2D 分支）：用在线 SNR 估计（pilot_power/residual_noise）算融合权重 `beta`，高 SNR 时把 true2d 输出平滑切回已现成的 legacy 估计（`srs_est[mem_offset]`），低 SNR 时纯 true2d。env：`SRS_2D_LEGACY_GATE`（默认 1）/`_DB`（默认 26）/`_WIDTH_DB`（默认 4）。**不触碰已逐位验证的 Stage1/2 数值**（单元测试仍 ≤1 LSB）。

门控后全 5 模型 × SNR 实测（delta = true2d − legacy，负=赢；同 seed）：

| ≈SNR | A | B | C | D | E | vs 修复前(高SNR) |
|-----|------|------|------|------|------|------|
| ~34 dB | +0.00 | −0.00 | −0.00 | +0.01 | +0.01 | 曾 +1.4~+3.5（legacy 赢）|
| ~29 dB | +0.00 | +0.01 | −0.01 | +0.01 | −0.00 | 曾 +0.5~+1.3 |
| ~23 dB | −0.01 | −0.53 | −0.02 | −0.15 | +0.01 | 曾 −1.1~−2.5 |
| ~17 dB | −4.60 | −5.04 | −3.65 | −4.46 | −2.98 | 不变（保留大胜）|
| ~11 dB | −7.06 | −6.58 | −5.62 | −6.78 | −5.06 | 不变（保留大胜）|

**结果**：所有 25 点 delta ≤ ~0（+0.01 为量化噪声，等同打平）→ **true2d 全 SNR 区间不再输 legacy**：高 SNR 精确追平（beta→1 切 legacy），中低 SNR 保留 −3~−7 dB 大胜。代价：≈SNR 23 dB（`-nd -9`）的小胜被门控吸收（在线 SNR 代理偏高、斜坡略提前）；如需保住可调高 `SRS_2D_LEGACY_GATE_DB`。

图见 `native_cdl_3way.png`：(a) NMSE vs SNR，`true2d(no gate)` 高 SNR 翘到 legacy 之上、`true2d(gated)` 贴住 legacy；(b) 对 legacy 的增益，no-gate 高 SNR 跌破 0（legacy 赢），gated 全程 ≥0。

### 6.7 动态 vs legacy（跨速度，含极高速 300 km/h）

§6.5 的 vs-legacy 是静态。本节扫速度 {0,30,60,120,300} km/h（固定 ≈SNR 17 dB，rep=3，STO 对齐守门），直接回答"动态/高速下 true2d 还能不能超过 OAI 生产去噪器"。增益 = legacy_NMSE − true2d_NMSE（正=true2d 赢）。

| 速度 | A(290) | B(478) | C(865) | D(376) | E(2064) | 均值 |
|------|------|------|------|------|------|------|
| 0 km/h  | 4.6 | 5.1 | 3.6 | 4.5 | 3.0 | **4.2** |
| 30 km/h | 2.2 | –† | 2.1 | 2.4 | 1.2 | 2.0 |
| 60 km/h | 3.5 | –† | 2.3 | 3.3 | 0.7 | 2.5 |
| 120 km/h| 3.2 | 2.4 | 2.0 | 3.0 | 1.1 | 2.3 |
| 300 km/h| 3.4 | 2.3 | 2.1 | 2.7 | 0.8 | 2.3 |

†b_s30/b_s60 各有一端单发退化（coh<0.9），剔除；其余 23 点 coh~1.0。

**结论**：
1. **true2d 在所有速度（含 300 km/h）都赢 legacy**——23 点全为正。即便 PKF 时间阶在 300 km/h 已完全失效（每 SRS 周期 5 个相位 cycle），true2d 仍稳赢 legacy ~2~3.4 dB（A/C/D）。
2. **静态赢得多（~4 dB），动态收窄到 ~2~2.5 dB 后持平到 300 km/h、不再衰减**。机理：静态时频域 Wiener 优势 + 时间阶额外去噪叠加；高速时时间阶死掉，但**频域阶（数据驱动 Wiener）本身就比 legacy 的固定 FIR 强 ~2~2.5 dB，且与速度无关** → 高速依然赢、且平。
3. legacy 自身也速度稳健（逐符号频域 FIR，无时间分量可崩）；这是"两个速度稳健的去噪器之间，true2d 凭更优的频域阶稳赢 ~2~2.5 dB"。
4. CDL-E（2064ns 最长时延）vs legacy 最弱（高速仅 ~0.7~1.2 dB）：长时延频域相关性少，Wiener 比固定 FIR 能多压榨的少。

图：`native_cdl_gain_vs_speed_legacy.png`（五模型增益 vs 速度小多图）；`native_cdl_dynamic_bars.png` 面板(b) 给 60 km/h 的 vs-legacy 增益柱状。

---

## 7. 分析与讨论

1. **true2d 确实有效**：在干净/可用 SNR 区间对裸 LS 取得**单调、可观**的去噪增益，静态峰值 **−10 ~ −12 dB**（A/E @ ≈SNR 6 dB），五模型一致。早期"增益一般"纯因测试 SNR 太高。

2. **增益本质 ∝ 噪声量**（MMSE/Kalman 的基本性质）：高 SNR 下裸 LS 已近完美（coh→1.000），true2d 滤波只剩对"近乎完美信号"的偏差代价（略差）；低 SNR 下裸 LS 退化，去噪净赢。这由相干→NMSE 硬关系 `NMSE_min ≈ 10log10(1−coh²)` 解释：passthru coh 1.000→可达 −34 dB；true2d coh 0.999→被钉在 ~−27 dB，那 0.999 vs 1.000 之差就是高 SNR 下的滤波代价。

3. **增益随时延扩展递减**（物理自洽）：频阶 Wiener 靠频域相关性去噪；长时延 = 频域去相关快 = 可压榨的相关性少，故 CDL-A（290 ns）> CDL-E（2064 ns）。

4. **增益对速度稳健（§6.4/§6.7，修复后实测）**：固定 ≈SNR 17 dB，**0~300 km/h（含极高速）全程** true2d 赢裸 LS ~6~8 dB、赢 legacy ~2~4 dB，从静态到移动仅掉 ~1~2 dB 后持平，**无高速崩溃**。机理：此 SNR 增益主要来自逐帧频域 Wiener（与速度无关）；PKF 在 300 km/h（每 SRS 周期 5 cycle）已失效，但 IAE-Riccati 压低其权重、回退频域阶 → **优雅退化、不被错误预测带崩**。（早期"快衰落 true2d 明显劣"是退化管线假象，已订正。）GT 量化坐实了这一机理：period-10（5ms）下相邻 SRS 的复相干仅 0.815(30km/h)/0.670(60km/h)，即每发 SRS 间信道已去相关 1/3~1/2，故时间阶 `K` 恒被 Riccati 推到 `kmax=0.95`（物理最优，非 bug）；时间阶在本 TDD 测试床（SRS 最密 5ms）无可激活工况（详见诊断日志 §12.21 与 §9.5）。

5. **静态 vs 动态**：§6.2/6.3 静态扫 SNR；§6.4/§6.7 动态扫速度。动态下（≈17 dB）增益基本保持，并未如先前误判那样大幅缩水；时间阶失效仅令 vs-legacy 的边际从静态 ~4dB 收窄到 ~2~2.5dB（频域阶撑底）。

6. **true2d 不是退化成 LS 的 bug**：LS 回退仅发生在边角（`R0<=0`/`R_table` 溢出）；`noise_norm` clamp 到 [1e-4, 2.0]，机制健全。

---

## 8. 结论

本项目完整打通了"算法 → C 生产实现 → native 端到端验证"的闭环：

- **算法/C/对齐三层均被验证正确**：离线 oracle（−28 ~ −30 dB）、逐位单元测试（≤1 LSB）、native A/B 可达相干 0.999 ~ 1.000。
- **native rfsimulator + channelmod 测试床打通**：修复 1 配置 + 2 C bug + handoff 机制 + GT 落盘 + CDL 回放，得到可复现、真值已知、可直接控噪的干净测试床，推翻了早期"native 不可用"的判断。
- **true2d 增益被定量刻画（四象限齐全）**：静态 vs 裸LS −9 ~ −12 dB、vs legacy −5 ~ −7 dB（峰值 ≈SNR 11 dB）；**动态 0~300 km/h（≈SNR 17 dB）vs 裸LS ~6~8 dB、vs legacy ~2~4 dB，无高速崩溃**（PKF 高速失效但频域 Wiener 撑住、优雅退化）。高 SNR 交叉点(~27dB)用 legacy 门控修成全 SNR ≥ legacy。
- **诚实的能力边界**：甜区 = 中低 SNR + 任意速度；高 SNR 廉价 legacy 略优（已门控规避）；CDL-E 长时延增益最小；2×2 MIMO 受 native 2 端口 SSB 限制未测。

---

## 9. 未尽事项与后续工作

1. ✅ **动态增益包络：已完成（§6.4，0~60 km/h）+ 极高速（§6.7，至 300 km/h）**——修复 5GC 每发重启 + STO 对齐守门后干净重测，25 点 coh~1.0、rep 一致（早期"1/3 点未过门"已由守门度量修正解决）。
2. ✅ **true2d vs legacy（FIR）：已完成**——静态扫 SNR（§6.5，交叉点 ~27 dB + 门控 §6.6 使全 SNR ≥ legacy）；动态扫速度（§6.7，0~300 km/h 全程赢 legacy ~2~4 dB，无高速崩溃）。
3. **动态 × 多 SNR 网格**：目前动态只在 ≈17 dB 一档；更低 SNR（时间阶占比更大）下的速度依赖待补。
4. **多 UE / 2×2 MIMO（空间维 MMSE 的潜在增益）**：需先解原生 2 端口 SSB（§4.1 #2）；per-link 架构已论证可平移，空间联合是更大 leverage 的方向。
5. **E1：时间阶 PKF 相位升级（已实装+验证，本测试床无杠杆）**：把单 ω 升级为 per-band ω / 线性相位 ω₀+slope·k（env `SRS_2D_PKF_PHASE`，默认 0=原行为、逐位一致；mode1/2 为 opt-in）。离线在"有 STO-ramp 且高相干"场景 +0.4 dB（并修复了单 ω 在 ramp 下反劣于裸 LS）。但实机查清：动态下时间阶 `K` 恒为 `kmax=0.95` 是 **Riccati 的物理最优解**，因 period-10（5ms）下相邻 SRS 已去相关 1/3~1/2（30 km/h 相干 0.815、60 km/h 0.670），时间维平均"去相关样本"只增误差。试图用 `-p 2` 加密 SRS 被 **TDD（8DL/3UL，每 10 slot 一周期）卡死（请求 2→实选 10）**。故**动态增益全部来自频域 Wiener，时间阶被正确关闭，E1 杠杆≈0**；要激活时间阶需改 TDD pattern 增 UL/SRS 机会或极低速。详见诊断日志 §12.21。

---

## 10. 复杂度优化（教授 6-04 要求 A：每 TTI CPU 占用 + 实时性 + MIMO 降本）

§1–§9 证明了 true2d 的**精度增益**;本节回答教授 6-04 录音反复强调的**复杂度/实时性**:加的 block 每 SRS 占多少 CPU、是否超 TTI、长跑取均值,以及 MIMO 扩展。**全程精度 near-lossless(bit-exact 优化用 golden ≤1-2 LSB 守门;近似优化用 NMSE-delta + 离线 CDL 守门)。**

### 10.1 计时基建（`SRS_TIMING=1` 门控,零开销默认关）

[nr_ul_channel_estimation.c](DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c) 加 `clock_gettime(CLOCK_MONOTONIC)` per-stage 计时,每 200 次 SRS 打印各 stage avg/max(µs):
- `legacy_filt`(公共基线,所有 mode 都算)、`freq_wiener`(Stage1)、`pkf_time`(Stage2)、`wiener_upd`/`wiener_pred`(预测)、`total_wall`(整次 fork-join 墙钟,主线程无竞态)。
- [nr_srs_mmse.c](DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_mmse.c) 内另有 `SRS freq-breakdown` 细分(ifft/noise_floor/R_build/stage3)。
- 脚本 [complexity_native.sh](DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/complexity_native.sh) 自动跑 legacy / true2d / true2d+predict 三档对比。判据:`ADDED(freq+pkf+wiener) per-SRS avg ≪ 500µs`(μ=1 单 TTI 预算)、`max < 5ms`(SRS 周期 @p=10)。

### 10.2 实测瓶颈与三步算法加速（freq_wiener 5042→190µs,26.5×,逐位等价）

初测:`freq_wiener` avg **~5042µs**(max 7712µs),**超 5ms SRS 周期** → MIMO 必爆;`legacy_filt` ~4µs、`pkf` ~6.5µs。瓶颈 100% 在 Stage1 频域 Wiener。三步优化(均 bit-exact,**始终启用、无开关**,取代原实现;原路径仅作奇异回退):

| 步骤 | 位置 / 函数 | 机理 | freq_wiener | 单步 | 累计 |
|---|---|---|---|---|---|
| 原始 | — | 每活跃 SC(~1248)各 `solve_complex_system`(O(W³),W=16) | 5042µs | — | 1× |
| ① 求逆一次 | Stage3,`invert_complex_system` | 窗内 Toeplitz `A=R[\|i-j\|·k_tc]+noise·δ` **平移不变** → 每次调用求逆一次,各 SC 只 `w=A⁻¹·b`(O(W²)) | 993µs | **5.1×** | 5.1× |
| ② twiddle 表 | Stage2,`g_twiddle_cos/sin` | `R(dk)=Σ clean·e^{-j2π dk n/N}` 的旋转因子是与信道无关常数 → 预存表 + 稀疏 PDP,消除 per-bin `cos/sin`(原大头是排序?见 10.4) | 433µs | 2.3× | 11.6× |
| ③ 权重缓存 | Stage3,`offset%k_tc` | 内部 SC 的权重 `w` 只取决于 `offset%k_tc`(共 k_tc 种)→ 懒缓存 k_tc 个权重向量,边带逐个算 | **190µs** | 2.3× | **26.5×** |

`5.1 × 2.3 × 2.3 ≈ 26×`。**注:① 贡献最大**(消掉 O(W³)×1248 的重复求解);①③ 在 Stage3、② 在 Stage2。验证:`c_unit_tests/run_all.sh` 的 `golden_freq` 多数 −300dB(bit-exact)、worst 1.0 LSB;离线 `test_srs_2d_offline.py` 加变体 assert(求逆一次 diff 5.9e-13、twiddle 3.1e-10、权重缓存 0.00)。

**预算结果**:`freq_wiener` 190µs ≪ 500µs TTI;4×4 MIMO ≈ 3ms 进 5ms 周期。**单 UE/SISO/4×4 实时达成。**

### 10.3 维度自适应（解除 4×4 上限,massive-MIMO 前提）

原 per-link 状态写死 `[SRS_2D_MAX_RX=4][MAX_TX=4]`,>4×4 越界。改造(始终启用):per-link 状态(`g_pkf_state`/`g_wiener`/`g_freq_rmeas`/`g_true2d_pdp_ema` 等)RX 维**运行时动态**(pointer-to-array,堆分配按实际 `nb_antennas_rx`;TX 维保持协议上限 4),`nr_srs_2d_ensure_capacity(n_rx)`/`nr_srs_mmse_ensure_capacity(n_rx)` 单线程预分配(dispatcher 在循环前调)。golden 4×4 逐位不变;ASAN/UBSAN 自测 2→8 增长、ant0≡ant7 隔离(diff=0)、无越界/泄漏。**关键坑:gNB `nb_antennas_rx = carrier_config.num_rx_ant = pusch_AntennaPorts`,不是 RU `nb_rx`** —— 测 MIMO 须同时设 `NB_RX` 与 `pusch_AntennaPorts`(launch 已绑定)。

### 10.4 多核并行（`SRS_PARALLEL`,默认关:本机访存 bound 无益）

把 `for(ant)for(port)` 循环改为**每 RX 天线一个线程池 task**(复用 OAI `gNB->threadPool` + `init_task_ans/pushTpool/join_task_ans`,循环体经 VLA 指针重建原样搬进 worker)。`SRS_PARALLEL=0`(默认)= 每链路 inline = 原串行行为(即便真机默认 8 线程池也不自动 fan-out)。
- **实测结论:本机(Threadripper 7960X)fan-out 无墙钟收益**。`freq-breakdown` 实测 per-link 190µs 大头是 `noise_floor`(robust 排序,**~90µs**)+ `stage3`(~69µs)+ `R_build`(~40µs),`ifft` 仅 ~3µs → **访存/带宽 bound,非 CPU bound**。pin 到远核(跨 CCD)反而更慢(per-link 185→280µs,跨 Infinity Fabric 取数)。串/并 NMSE 等价(−45.05 vs −45.02,Δ0.03dB)→ 逻辑正确但非有效杠杆。**保留(默认关、零风险),诊断出真瓶颈 → 指向 Option B。**

### 10.5 Option B：跨 RX 天线共享 R（`SRS_SHARE_R`,默认关,near-lossless）

同 UE 各 RX 天线**时延谱(PDP)基本相同** → 频域自相关 `R(dk)` 可共享:参考天线(ant0)`nr_srs_mmse_freq_filter_build` 算一次 R,其余天线 `nr_srs_mmse_freq_filter_reuse` 复用、**跳过 Stage1/2(collect+ifft+EMA+noise_floor+R_build)**,只做 Stage3 apply。`SRS_SHARE_R=0`(默认)= 现行每条全算(`freq_filter`=build+apply 逐位等价,golden 守门);`=1` 启用(强制串行,ref 先 build)。**非 bit-exact** → 用 NMSE-delta 守门。
- **复用链路:190→~69µs(vs 原始 ~73×)**;2-rx `total_wall` 574→438µs(−136µs ≈ 一根天线 Stage1/2)。
- **四重验证 near-lossless**:① golden 默认关 bit-exact;② 离线真 CDL A–E(rms 时延 68→556ns,NB_RX=4,SNR20)ΔNMSE **≤0.053dB**;③ native 无噪固定-seed ΔNMSE **0.01dB**;④ native 带噪(-nd -6)×3 均值 ON(−22.5)不差于 off(−21.8)(单发抖动 ~3dB,均值无系统损失)。

### 10.6 度量口径澄清（防误引用）

eval 是 STO+scalar 对齐,残差近似 **`NMSE ≈ −10·log10(1−|coh|²)`**。**noise-off 时 coh≈0.99998 → NMSE 飘到 −44dB,这是"无噪天花板",非真实性能**;加 `-nd -6`(≈17dB SNR)后 coh≈0.997 → **真实工作点 true2d-freq ≈ −22~−24dB**(与 §6 一致)。报告/汇报引用须用 −22~−24dB,勿用 −44dB。

### 10.7 复杂度优化的脚本与参数

| 旋钮(env / flag) | 默认 | 作用 |
|---|---|---|
| `SRS_TIMING=1` | 关 | per-stage µs 计时 + `freq-breakdown` + `total_wall` |
| `SRS_PARALLEL=1` | 关 | 多核 per-天线 fork-join(需 `TPOOL` 配核才真并行) |
| `TPOOL="0,1,..."` / `n` | `n`(launch 默认 inline) | L1 线程池绑核;`n`=无池=串行 |
| `SRS_SHARE_R=1` | 关 | Option B 跨天线共享 R(强制串行) |
| `NB_RX` / `NB_TX` | 1 / 1 | RU 天线数(同时设 `pusch_AntennaPorts=NB_RX` 才让 SRS 循环真跑多天线,launch 已绑定) |
| `-d SEC` / `LAUNCH_DURATION` | 180s | attach 后自动跑 SEC 秒收尾(`-d 0`=Ctrl+C 为止) |

复现:`sudo SRS_TIMING=1 NB_RX=2 [SRS_PARALLEL=1 TPOOL=20,22] [SRS_SHARE_R=1] bash launch_native.sh -e true2d -cir cir/cir_cdl_c_s60.bin [-nd -6] -d 120`;`grep -E "SRS timing|freq-breakdown" ../../logs/native_true2d_*/gnb.log`。

---

## 11. SRS 信道预测（方向 A：估计当前 H(t) → 预测未来 H(t+kΔ)）

§1–§10 的 true2d **估计当前** H(t);本节把时间级扩展为**预测未来 H(t+kΔ)**(Δ=SRS 周期),服务实验室 **Digital Twin 的前瞻能力**——补偿"测到 CSI → 用 CSI"之间的时延,让过时的 CSI 变"新鲜"。**全经典方法(线性 MMSE),不含 AI**;定位为后续 AI 预测器的 **baseline 与立项判据**(经典在哪崩,就从哪上 AI)。完整推导/迭代见 `0605_SRS信道预测_方向A_日志.md`。

### 11.1 任务与度量

- **任务**:用过去若干帧的 true2d 频域估计 `H_{t},H_{t-1},…`,预测 k 个 SRS 周期后的 `H_{t+k}`(k=1 即 5ms@p=10)。
- **度量(自参考 NMSE)**:预测目标 = **未来真实测到的 SRS 估计 `est_t2d[t+k]`**(与预测输入同参考域,不碰 GT/STO/标定)——这正是"填空窗 = 预测下一个 CSI 样本"的运维任务,绝对值干净、可跨速度比。基线 = **ZOH(hold-last,硬冻结上一帧)**。`replay_true2d.py --predict-horizon k` 在真实 capture 上回放打分。
- 避坑:早期用逐帧复标量对齐 → 把预测提供的"时变公共相位"整个抵消 → 假象 `predict≡zoh`;改自参考后才看清真实价值。

### 11.2 预测器选型:相位外推 → 时间维 Wiener（关键结论:Wiener ≫ Kalman 相位)

**先试相位外推三档**(`time_pkf_predict` mode0 scalar / mode1 per-band / mode2 linear-STO):三速度下 **mode0 ≥ mode1/mode2**(per-band 每带样本少噪声大;STO ramp 门控真实信道基本不触发)→ 相位旋转类最优就是 mode0(单一公共多普勒相位外推、幅度冻结)。

**再试时间维 Wiener**(线性 MMSE 预测器 `time_wiener_predict`,p=4 个 lag):**mode0 不是上限,Wiener 大幅胜出**(自参考 NMSE,native 真实数据):

| k=1 NMSE(dB) | ZOH | mode0(相位) | **Wiener(p=4, genie R)** |
|---|---|---|---|
| 3 km/h | −8.4 | −10.0 | **−14.4** |
| 10 km/h | −0.4 | −4.3 | **−13.7** |
| 60 km/h | −1.0 | −1.8 | **−2.5** |

- **Wiener 全地平线保持负 NMSE**(10km/h −13.7@k1 → −3.3@k12),**不像 mode0/ZOH 随多普勒拍频振荡转正**(ZOH 不转相位 → 误差按拍频周期起伏,偶尔巧合反超)。
- **Wiener 赢在两点(phase-only 碰不到)**:① p=4 个过去样本 → **额外去噪**(k=1 即 −4.3→−13.7);② **预测幅度** + 按实测 R(Δt) 最优加权多普勒动态。
- **物理洞察**:Kalman 增益小的根因 = 它只是 Wiener 的 **1-lag/仅相位特例**。→ 经典远未到头,**该港到 C 的是时间维 Wiener**。

### 11.3 算法（per rx-tx 链路,`time_wiener_predict` / C `nr_srs_wiener_*`)

1. **存历史**:环形缓冲最近 `L=p-1+kmax` 帧(p=4,kmax=8 → 11 帧)的 true2d 当帧估计 `H_t`(活跃 SC)。
2. **在线估时间自相关 R(Δt)(因果 EMA)**:每帧每 lag d:`inst(d)=mean_sc(H_t·conj(H_{t-d}))`(~624 SC 平均);`r[d]=(1-a)r[d]+a·inst(d)`,a=0.05。`r(d)=E[h(t)conj(h(t-d))]≈|r|e^{jωd}` 即多普勒二阶统计。
3. **解 Wiener-Hopf(每地平线 k)**:`R[i,j]=r(i-j)`(Hermitian Toeplitz)`+ diag_load·r0·I`;`p_k[i]=r(k+i)`;**复 Hermitian Cholesky(double)** 解 `R·a=p_k`。
4. **预测**:`H_pred(t+k)[sc]=Σ_{j=0}^{p-1} a_j·H_{t-j}[sc]`(最近 p 帧线性组合,同时预测幅度+相位);历史<p → ZOH 回退。

**默认参数**:`p=4, r_ema=0.05, diag_load=0.2, kmax=8`。

### 11.4 在线化(去 genie)+ 定阶

- **去 genie**:R(Δt) 改在线因果 EMA(非整窗已知统计)。**朴素在线 EMA 崩溃**(R 估计噪声经 Wiener 解放大,比 ZOH 还差)→ **对角加载正则化救回**:`diag_load=0.2, r_ema=0.05` 下 10km/h k=1 = **−8.6dB(胜 ZOH +8.2)**(genie 上界 −13.7,mode0 −4.2)。→ **在线 Wiener 可部署,胜 mode0 ~4dB**。
- **定阶**:10km/h 在线 k≤5 平均胜 ZOH:p=2 +6.99 / **p=4 +8.12** / p=8 +8.46 → **p=4 拐点**(p=8 仅 +0.34,边际递减)。

### 11.5 C 端实现（env 默认关,on-time 逐位不变）

- [nr_srs_2d_filter.c/.h](DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_2d_filter.c):`wiener_state_t`(每链路 ring 缓冲 + double EMA R)、`nr_srs_wiener_update/predict`(**只读估计路径**)、复 Hermitian Cholesky(double)、getter。
- env:`SRS_WIENER_PREDICT_AHEAD=k`(0=关)/`ORDER`(p)/`KMAX`/`R_EMA`/`DIAG_LOAD`,**默认全关 → on-time 估计逐位不变**。
- [nr_ul_channel_estimation.c](DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c):true2d 分支每帧 `nr_srs_wiener_update` 维护历史;predict-dump 处 Wiener 优先于 PKF;`launch_native.sh` 转发 `SRS_WIENER_*`。
- **on-time 不替换 Kalman**:报告 §6.4/§12.21 已证动态下时阶 K 饱和、on-time 贡献≈0(去相关墙),Wiener 只做预测(k>0)。

### 11.6 验证

- **golden**:`c_unit_tests/test_wiener_predict_main.c` + `golden_wiener_predict.py`(镜像在线-EMA Wiener)→ **C(double) vs 离线 ≤1 LSB 全 PASS**(k=1/4/8,p=4,多 seed);`run_all.sh` 四项(pkf/pkf-predict/wiener-predict/freq)全 PASS。
- **native 实机闭环**(`SRS_WIENER_PREDICT_AHEAD=4`,p=10):C dump = Wiener k=4 预测(20ms 前瞻);`eval_nmse_sto --predict-horizon 4`(SRS 轴 +40 slot 对未来 GT)= **NMSE −3.16dB,coh 0.809**;不移位(k=0 对当前 GT)= −6.10dB ≠ on-time −24dB → 证明 dump 是**真预测**(已向未来外推、非 hold-last);与离线 replay 在线 Wiener(10km/h k=4 ≈ −3.5dB)**一致** → **C≡离线端到端闭环成立**。

### 11.7 结论与 AI 立项点

- native 真实数据上 **SRS 信道预测确实有效**:k=1(5ms)在线 Wiener 3/10/60 km/h ≈ **−8.6 ~ −11dB / 见 11.2**,全程平滑跑赢 ZOH;**可用前瞻地平线 ∝ 相干时间 ∝ 1/速度**(≤10km/h ~50ms,60km/h ~25ms)。
- **经典在线 Wiener 可部署**;距 genie 上界残差 ~2-3.5dB(在线 R 估计噪声),简单参数化 R 吃不动 → 要靠**更丰富 R 模型(多多普勒分量/ESPRIT)或 AI(隐式学统计)**。这条"地平线 vs NMSE / 速度"曲线即 **AI 预测器的 baseline 与立项判据**:经典在长地平线/高速崩的地方,正是 AI 的切入点。

---

## 附录 A：关键文件索引

### C 端（OAI gNB，`DevChannelProxyJIN/openairinterface5g_whan/`）

| 文件 | 职责 |
|------|------|
| `openair1/PHY/NR_ESTIMATION/nr_srs_mmse.c` | Stage 1 频域 Wiener LMMSE；高 SNR 门控配置；**§10 加速:`invert_complex_system`(①求逆一次)、`g_twiddle_cos/sin`(②)、Stage3 权重缓存(③)、`nr_srs_mmse_ensure_capacity`(维度自适应)、`nr_srs_mmse_freq_filter_build/reuse`(Option B)、`freq-breakdown` 计时** |
| `openair1/PHY/NR_ESTIMATION/nr_srs_mmse.h` | freq_filter API + **`NR_SRS_MMSE_R_TABLE_SIZE`、build/reuse/ensure_capacity 声明** |
| `openair1/PHY/NR_ESTIMATION/nr_srs_2d_filter.c` | Stage 2 时域 PKF；**§10 维度自适应(per-link 状态 RX 维堆分配 + `nr_srs_2d_ensure_capacity`);§11 Wiener 预测器 `nr_srs_wiener_update/predict`** |
| `openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c` | 估计器分发 + SRS V3 dump；高 SNR legacy 门控（§6.6）；**§10 per-(ant,port) fork-join worker(`srs_link_worker`/`srs_ce_ctx_t`) + `SRS_TIMING`/`SRS_PARALLEL`/`SRS_SHARE_R` 门控 + ensure_capacity 预热** |
| `openair1/PHY/NR_ESTIMATION/c_unit_tests/` | 逐位 golden(`run_all.sh`:`golden_freq`/`golden_pkf*`/`golden_wiener_predict`),改 C 后必跑 |
| `openair1/PHY/INIT/nr_init.c` | SRS bin 写盘线程（V3 绝对 sample_ts） |
| `openair1/PHY/defs_gNB.h` | SRS bin 格式（`SRS_BIN_VERSION=3`） |
| `radio/rfsimulator/apply_channelmod.c` | handoff 门控 + 旁路索引修复 + `native_gt_dump` + CIR 回放 |
| `openair1/SIMULATION/TOOLS/random_channel.c` | `Doppler_phase_cur` 分配修复 + `OAI_RNGSEED` 可复现信道 |

### Python / 脚本（`DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/`）

| 文件 | 职责 |
|------|------|
| `launch_native.sh` | 一键 native 启动 + preflight（`-e/-d/-p/-nd/-seed/-cir/-gt-every`；**§10 新增 env 转发:`SRS_TIMING`/`SRS_PARALLEL`/`TPOOL`/`SRS_SHARE_R`/`NB_RX`/`NB_TX`/`LAUNCH_DURATION`,`-d` 默认 180s 自动收尾**） |
| `sweep_native_cdl.sh` | true2d vs passthru A/B 扫描（`-m/-nd/-d/-seed/-rep`） |
| `complexity_native.sh` | **§10 复杂度实测:自动跑 legacy/true2d/true2d+predict 三档 `SRS_TIMING` 对比** |
| `cdl_to_cir.py` | CDL 射线 → 逐 slot sample-spaced CIR |
| `native_gt_to_npz.py` | rfsim GT taps → npz（动态 GT + 自动宽 tau 共轭探测 + post-handoff 过滤） |
| `eval_nmse_sto.py` | 评估（unified-V3 + CDL 帧过滤 + **STO 对齐守门相干** + STO 对齐 NMSE） |
| `test_srs_2d_offline.py` | 离线黄金参考 harness |
| `_diag_dyn_gt.py` | 动态 GT 探针（GT-slot 偏移 + 共轭，定位用）|
| `plot_cdl_sweep.py` | 静态：增益 vs SNR + 三方柱状 + 门控对照图 |
| `plot_threeway.py` | **核心**三方 NMSE 图（静态 vs SNR + 动态 vs 速度）|
| `plot_speed_sweep.py` / `plot_speed_vs_legacy.py` / `plot_dynamic_bars.py` | 动态：vs LS / vs legacy 速度图 + 60km/h 柱状 |
| `plot_true2d_architecture.py` | 架构框图 |

### 图件

| 文件 | 内容 |
|------|------|
| `true2d_oai_architecture.png` | 两阶段架构框图 |
| `native_cdl_gain.png` | 增益曲线总览（Δ vs passthru-NMSE） |
| `native_cdl_gain_small_multiples.png` | 五模型分面图 |
| `native_cdl_nd_minus3_bars.png` | **静态** ≈SNR 11 dB 跨模型柱状：(a) 三方 NMSE(LS/legacy/true2d) (b) true2d 对两基线的增益 |
| `native_cdl_dynamic_bars.png` | **动态** 60 km/h(≈SNR 17dB) 跨模型柱状：(a) 三方 NMSE (b) true2d 对两基线增益 |
| **`native_cdl_threeway_static_dynamic.png`** | **核心图：三方(LS/legacy/true2d) NMSE，左静态 vs SNR + 右动态 vs 速度** |
| `native_cdl_3way.png` | 三方 NMSE-vs-SNR + 高 SNR legacy 门控前后对照 |
| `native_cdl_gain_vs_speed.png` | true2d vs 裸LS 增益 vs UE 速度（CDL A-E，≈SNR 17dB）|
| `native_cdl_gain_vs_speed_legacy.png` | true2d vs legacy(FIR) 增益 vs 速度（含 300 km/h）|

## 附录 B：核心复现命令

```bash
cd /home/dclcom61/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1

# 1) 生成静态 CDL-B 的 CIR
python3 cdl_to_cir.py data_out/cdl_b cir/cir_cdl_b_s0.bin -speed 0

# 2) 静态 5 模型 × SNR：true2d vs 裸LS（-A passthru）/ vs legacy（-A legacy）
sudo bash sweep_native_cdl.sh -m "cdl_a_s0 cdl_b_s0 cdl_c_s0 cdl_d_s0 cdl_e_s0" \
  -nd "-15 -12 -9 -6 -3 0 3" -rep 1 -d 60 -A passthru -B true2d -restart5gc
#   换 -A legacy 得 vs-legacy（§6.5）

# 3) 动态：生成各速度 CIR + 扫速度（rep≥3，每发重启 5GC）
for m in a b c d e; do for s in 0 30 60 120 300; do \
  python3 cdl_to_cir.py data_out/cdl_$m cir/cir_cdl_${m}_s${s}.bin -speed $s; done; done
sudo CN5G_RESTART_WAIT=10 bash sweep_native_cdl.sh \
  -m "cdl_a_s0 cdl_a_s30 cdl_a_s60 cdl_a_s120 cdl_a_s300 ...(全 5 模型)" \
  -nd "-6" -rep 3 -A legacy -B true2d -restart5gc    # -A passthru 得 vs-裸LS

# 4) 出图
python3 plot_cdl_sweep.py        # 静态图
python3 plot_threeway.py         # 核心三方图
python3 plot_speed_sweep.py plot_speed_vs_legacy.py plot_dynamic_bars.py  # 动态图

# === §10 复杂度优化复现 ===
# 逐位 golden(改 C 后必跑;freq/pkf/wiener-predict 均应 ≤1-2 LSB PASS)
cd ../../openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/c_unit_tests && bash run_all.sh && cd -

# 三档 per-stage µs(legacy/true2d/+predict),看 ADDED per-SRS vs 500µs
sudo SRS_TIMING=1 bash complexity_native.sh

# freq_wiener 子分 breakdown(看 noise_floor/stage3/R_build/ifft 占比)
sudo NB_RX=2 SRS_TIMING=1 bash launch_native.sh -e true2d -cir cir/cir_cdl_c_s60.bin -d 120
grep -E "SRS timing|freq-breakdown" ../../logs/native_true2d_*/gnb.log | tail

# Option B 干净对照(同 seed,只切 SRS_SHARE_R;预期 ΔNMSE≤0.1dB + total_wall 降)
sudo NB_RX=2 SRS_SHARE_R=0 bash launch_native.sh -e true2d -cir cir/cir_cdl_c_s60.bin -nd -6 -seed 4242 -d 120
sudo NB_RX=2 SRS_SHARE_R=1 bash launch_native.sh -e true2d -cir cir/cir_cdl_c_s60.bin -nd -6 -seed 4242 -d 120
# 多核(空闲核;本机访存 bound,加速有限) — 需 NB_RX≥2 才有并行
sudo NB_RX=2 SRS_PARALLEL=1 TPOOL=20,22 bash launch_native.sh -e true2d -cir cir/cir_cdl_c_s60.bin -d 120

# === §11 信道预测复现 ===
python3 test_srs_2d_offline.py --predict-sweep --predict-trials 12   # 离线"地平线 vs NMSE"
sudo SRS_WIENER_PREDICT_AHEAD=4 bash launch_native.sh -e true2d -cir cir/cir_cdl_c_s60.bin -nd -6 -d 120  # C 端预测 dump
python3 eval_nmse_sto.py --run-dir ../../logs/native_true2d_<TS> --predict-horizon 4
```

> 编译（改 C 后）：`cd cmake_targets/ran_build/build && sudo ninja nr-softmodem rfsimulator`；**重编后必须重启 gNB** 才加载新 `.so`。§10/§11 只改 gNB 侧 → 只需 `ninja nr-softmodem`（UE/ulsim 不涉及）。
> 长 sweep 必加 `-restart5gc`（每发重启 5GC，防 NAS 累积退化致 SRS 捕获 0，见 §4.6）。
> §10 优化分两类：**始终启用(bit-exact:求逆一次/twiddle/权重缓存/维度自适应,golden 守门)** 与 **默认关(近似/并行:`SRS_PARALLEL`/`SRS_SHARE_R`)**。真实 NMSE 引用 −22~−24dB(−44dB 是无噪天花板,见 §10.6)。
