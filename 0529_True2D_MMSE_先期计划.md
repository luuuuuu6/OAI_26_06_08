# 真·可分离 2D MMSE（全场景）先期计划

| 字段 | 值 |
|------|----|
| **日期** | 2026-05-29 |
| **前置** | `0528_Adaptive_FreqSmooth_实装日志.md`（MA+Kalman=-17.39 dB）、`0528_Wiener_Attempt_日志.md`（Wiener C 端失败已回退） |
| **最终目标** | 实现真正的频域 Wiener（替代 MA）× 时域 Kalman 级联 = 可分离 2D MMSE，并在**全场景矩阵**上相对 Legacy 改善 > 0 dB（ε=5%），对齐 ALDLC §4.4 / §8（Q3） |
| **本计划范围** | 只做规划与离线先行，不在 gate 通过前改 OAI C 运行代码 |

---

## 〇、核心结论（来自前两份日志 + 代码复核）

1. **算法/公式是对的**：`oracle_pdp_wiener=-43`、`lmmse_fullband` 合成测试全 PASS（含 2tap/4tap 频选）。
2. **失败根因不是"单帧 PDP 方差"**：离线 `data_pdp_wiener_active` 已对全部帧做 `np.mean(..., axis=0)` 平均（见 `test_srs_2d_offline.py:151`），仍只有 -17。真因有两层：
   - **层1（C 实现）**：把 active band 零填充到 2048 全网格再 `idft`，sinc 泄漏 + 信号稀释 → 幅度压缩 3.4×。正确做法是 active band 自身的 **N_act 点精确变换**。
   - **层2（估计）**：soft Wiener 用**含噪 PDP + 有偏尾部地板**，~25% 噪声 tap 漏过（`gain_nonzero=24.6%`）。`data_delay_top1=-45`（硬 mask）证明纯数据可达 oracle，差距全在地板/阈值调校。
3. **MA 不可作全场景方案**：flat 好（-17），但合成频选直接崩（2tap -5.87 / 4tap -4.40）。
4. **数据已就位**：CDL-A/B/C/D/E ray 数据已转换完成（见 §1.1），但**尚未生成任何 CDL 的 SRS dump+GT run**，"全场景"目前零实测证据。
5. **MIMO 阶数无关是硬约束**：算法不能只对 2×2 有效，必须按"逐空间链路独立处理频域向量"设计，天然扩展到 4×4 / massive MIMO。

---

## 一、目标与验收标准

### 1.1 全场景矩阵（验收口径，对齐 ALDLC §4.4）

**"全场景"= 全部 6 种信道模型都要跑通，不是子集**：P1B + CDL-A/B/C/D/E。

| 维度 | 取值（必跑全集） | 数据来源/开关 |
|------|------------|--------------|
| 信道模型 | **P1B(flat) + CDL-A/B/C/D/E（全 5 种）** | `NPY_DIR=<cdl_dir>`（清空 `P1B_NPZ`）或 `P1B_NPZ=...` |
| 速度→Doppler | 低速3（基线）/ 中速30（CDL-A/C）/ 高速120（选做） | `UE_SPEED=<m/s>`（复用同一 ray 数据） |
| SNR | {-5,0,5,10,15,20,25} dB | `ONLY_SNR` / `SNR_POINTS` |
| 天线 | 2×2 先验证，**算法设计须 4×4 / massive 无缝扩展** | `-ga/-ua` |
| 种子 | 固定 42 | `CHANNEL_SEED=42` |

CDL 数据实际路径（已确认存在，各含 6 个 `*_rays_for_ChannelBlock.npy`）：

```
DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/data_out/cdl_a
                                                              .../data_out/cdl_b
                                                              .../data_out/cdl_c   (+ cdl_c_30kmh 速度变体)
                                                              .../data_out/cdl_d
                                                              .../data_out/cdl_e
```

信道特性参考（用于解读结果）：CDL-A/B/C = NLOS 频选（延迟扩展递减），CDL-D/E = LOS（含主导径，近平坦/Rician）。这套覆盖了从强频选到近平坦的完整谱，配合 P1B(超平坦) 构成全场景。

> 注 1：`launch_all_v9.sh:72` 给 `P1B_NPZ` 设了默认值，跑 CDL 必须显式 `P1B_NPZ="" NPY_DIR=data_out/cdl_a`，否则 P1B 会覆盖 CDL（v8.py:3922 "Falls through only when --p1b-npz is NOT given"）。
> 注 2（重要坑）：**Proxy 跑在 Docker 容器内**（cwd=`/workspace/vRAN_Socket/...`）。`NPY_DIR` 必须传**相对路径**（如 `data_out/cdl_c`），v8.py:4004 会 `join(_SCRIPT_DIR, ...)` 解析到容器内正确位置。传宿主机绝对路径会 `FileNotFoundError` 导致 Proxy 崩、整套挂起。
> 注 3：`NPY_DIR` 不被 `run_q4` 转发，须以命令行 `NPY_DIR=... bash run_q4...` 形式 export 进环境，由 `launch_all` 子进程继承。

### 1.2 数值目标（per-antenna NMSE）

| 场景 | Legacy | MA+Kalman(现) | 真2D MMSE 目标 |
|------|--------|---------------|----------------|
| P1B flat | -10.87 | -17.39 | **≤ -30**（逼近 oracle -38~-43） |
| CDL-A/B/C 频选 | 待测 | 预计 MA 退化 | **显著优于 MA，且优于 Legacy** |
| CDL-D/E LOS | 待测 | — | 不劣于 Legacy（ε=5%） |
| 全 6 信道矩阵 | — | — | **每个 cell 改善 > 0 dB（ε=5%），无任一信道退化** |

### 1.3 非数值要求
- 处理延迟 < 1 ms/SRS 块（ALDLC §5.1）。
- Legacy 路径不受影响（环境变量切换，默认关闭）。
- EMA warmup / PDP 塌缩有 fallback，永不比输入差。
- **MIMO 阶数无关**：同一份代码在 2×2 与 4×4/massive 上都能跑，无硬编码维度；详见 §2.4。

---

## 二、算法与架构决策（定稿）

### 2.1 架构：可分离级联（不做非可分离 2D）
WSSUS 下 `R(Δf,Δt)=R_f·R_t` ⇒ 可分离级联 = 联合 2D MMSE 最优。
```
raw-LS → [Stage1 频域 窗口Toeplitz LMMSE(用 R_f)] → [Stage2 时域 相位预测Kalman(用 R_t)] → 输出
```
> **2026-05-29 定稿（详见 `0529_True2D_离线判定日志.md`）**：
> - Stage1 = **窗口 Toeplitz 频域 LMMSE**（= C 已有 `nr_srs_mmse_freq_filter`），见 §2.2。
> - Stage2 = **per-link ω 相位预测 Kalman（PKF）**，**替换**现有 `nr_srs_2d_filter` 的全局去旋（去旋已实测在频选/相干多普勒下崩，见 §2.3）。
> - 级联实测：CDL 可达 ~-30 dB，全速度段不退化。两段 C 零件都在，关键是"频阶接时阶 + 时阶换 PKF"。

### 2.2 频域算法：窗口 Toeplitz 频域 LMMSE（修正，2026-05-29 判定，详见 `0529_True2D_离线判定日志.md`）

> ⚠️ **重大修正**：原结论"对角 Wiener 即现实天花板、Toeplitz 非现实目标"经 `test_true2d_wiener.py` 判定后**部分推翻**。真 2D（全 Toeplitz）比对角高 **5–7 dB** 且真实可达其上界；可鲁棒落地的频率阶应是**窗口 Toeplitz LMMSE**（非对角），它稳定优于对角 1–3 dB 且从不崩。

- **频率阶定稿 = 窗口 Toeplitz 频域 LMMSE**，R(Δk)=FT{清洗后 EMA-PDP}（维纳-辛钦）。**这正是 C 已有的 `nr_srs_mmse_freq_filter`**。per-link、MIMO 阶数无关；窗口 W 控复杂度。
- 天花板谱（CDL A–E，SNR15/n_sc256 实测）：对角 od_diag -20~-24 < **窗口 Toeplitz(数据R) -22~-24** < 窗口 Toeplitz(精确R) -23~-28 < **全 Toeplitz oracle = 真2D -27~-29**。
- **5–7 dB 真 2D 增益的来源 = 精确 R_f，与对角/Toeplitz 结构无关**：`FT{PDP}`≡经验自相关（同一周期图），都受 CDL 离格延迟的 **DFT 泄漏结构性偏差**封顶，再平均无效（200帧×α0.98 仅 +0.6dB）。
- **ESPRIT 参数化 R 判定（一锤定音）**：窄延迟谱(cdl_a/b/d)可逼近 win_genie，但**宽/密延迟谱(cdl_c/e)灾难性退化（-10~-25，常比 raw 差），高 SNR×多帧也救不回 → 结构性失败，不满足全场景鲁棒**。故参数化超分辨仍列为 ceiling 研究，**不进主线**。
- **不选**：① 对角 Wiener（弱于 Toeplitz 1–3dB，无理由再用）；② 全带 LMMSE O(N³)；③ ESPRIT/CS 主线（脆弱）。剩余到 oracle 的 4–6 dB = 研究天花板，工程上不追。
- **关键发现（CDL + raw-LS 离线，2026-05-29，详见 Phase 1 日志 §5/§6）**：
  - **固定阈值不是全场景解**：flat 最优 rel_db=20（→-42），CDL 最优 rel_db=60（→-20~-28），**两者相反**，固定值无法通吃（已用真实 raw-LS + 合成 CDL 双向证实）。
  - "作用在 raw LS（白噪声）"对 flat 成立（rel_db=20 → -42），但**不解决** flat↔CDL 的裁剪张力。
  - **解法 = 自适应裁剪：按噪声地板 `pdp>floor·gamma` 保 tap**（flat 自动 1 tap、CDL 自动几十 tap，天然随信道自适应）。历史失败均源于**地板估计**（filt8 有色噪声、passthru K_TC 混叠），非思路问题。
- **Phase 1.5（已完成于白噪声稠密输入，2026-05-29）**：**全自适应无参数选择器** = `adaptive_floor_threshold`（迭代鲁棒 floor + 实测噪声 DOF → Wilson-Hilferty chi² 分位点求 gamma，`p_fa=1/N`）+ soft gain。floor、gamma **都从数据推**，无手调 gamma/rel_db。K 自动伸缩（flat→4、CDL→38~74），全场景在天花板 ~1-3dB 内（flat -35、合成频选 -32~-34、CDL A–E -22~-28）。
  - **输入约定**：必须是白噪声+稠密延迟谱（filt8 有色噪声、passthru 稀疏均会崩）。→ C 端用 `nr_srs_mmse.c` 的 dense-pilot IFFT。
- 不选：全 LMMSE 求逆 O(N³)、data 经验自相关 Toeplitz（发散）、参数化超分辨/CS（未来 ceiling 研究）。

### 2.3 时域算法：per-link ω 相位预测 Kalman（PKF）—— 替换现有全局去旋（修正，2026-05-29 判定）

> ⚠️ **重大修正**：原"维持现有 Kalman"作废。审 `nr_srs_2d_filter.c` + 离线复现（`test_2d_cascade.py`）发现现有 adaptive/ibvss/kalman 三模式都用**单标量全局去旋 + 输出未回旋**，在频选/相干多普勒下**灾难性崩**（输出比 raw 还差）。

- **时域级定稿 = PKF**：每 (ant,port) 一个公共相位率 ω（功率加权聚合、EMA 更新）+ 每 SC 一个复数状态 s。每帧：`pred=s·e^{jω}` → `s=pred+K(obs−pred)` → **输出 s（当前帧、已回旋）**。
- 相干多普勒实测（CDL，SNR15）：**PKF 决定性优于 EWMA**，差距随速度拉大（低速 +1~4 dB、中速 +5~9、高速 +7~14 dB）；EWMA 中/高速反伤、去旋全速度崩。
- per-SC ≈ per-link，**per-link 公共 ω 更简单鲁棒**，进 C 首选；K 用 **IAE-Riccati 自适应**（作用在 per-SC 复数残差，**不做全局去旋**）。
- **自适应 K 的测量噪声 R_meas（已闭合，2026-05-29）= 频阶传播白噪声 `R_meas=σ²·mean_k‖bₖᵀA⁻¹‖²`**（b、A、σ² 全来自频阶 Toeplitz，C 现成）。离线验证：全数据驱动 Riccati ≈ genie 版（差≤0.25 dB）、匹配/超过最优固定 K、高速反超（cdl_a −5.4 dB）。详见判定日志 §4.5.7。
  - ⚠️ 实现坑：复 Hermitian 下权重是 `A⁻¹·conj(b)` 非 `A⁻¹b`（漏共轭会把噪声增益高估 ~7000×）。
- warmup/塌缩 fallback、gap-reset 保留；彻底删除"单标量全局去旋 + 输出未回旋"。
- **进 C 前置条件已全部满足**（频阶复用 + 时阶 PKF + R_meas 闭合），Kalman 改写可动手。
- **2026-05-29 已落 C（详见判定日志 §4.6）**：`SRS_ESTIMATOR=true2d` = 窗口 Toeplitz 频（修复共轭 bug + EMA + robust floor + 出 R_meas）→ PKF 时（`SRS_2D_METHOD` 无关，强制 `nr_srs_pkf_update`）。**发现并修复频阶共轭严重 bug**（原 mmse1d 只平坦在格对，频选必崩）。#4（2048 零填充/梳状标定）离线验证 OK，无需 KISS-FFT。隔离实现未碰 legacy/mmse1d/mmse2d/freqsmooth。仅剩编译+端到端（用户执行）。

### 2.4 MIMO 阶数无关设计（硬约束）

频域 Wiener 的处理对象是**单条空间链路 (rx, tx) 的频域向量** `H_{rx,tx}[k]`。因此算法对 MIMO 阶数天然无关：

- **逐链路独立处理**：对每个 (rx, tx) 做 N_act FFT → PDP/gain → IFFT。总复杂度 `O(n_rx·n_tx · N logN)`，线性于链路数，2×2→4×4→massive 平滑扩展。
- **状态按链路动态分配**：PDP-EMA / Kalman 的 per-(ant,port) 状态数组**必须按运行时 `n_rx×n_tx` 动态分配**，严禁硬编码 2×2（这是 massive MIMO 的常见坑）。
- **massive MIMO 友好优化（建议，非必须）**：同一 UE 在不同 RX 天线上的 **PDP 是同一传播过程的二阶统计量**，统计上相同。可在天线间**共享/聚合 PDP-EMA**（跨 RX 平均），既降状态量又增平均样本数 → PDP 估计更准。这对天线数大时收益尤其明显。
- **不引入空间维联合（保持 per-link）**：利用空间相关 R_space 的联合估计会破坏可分离性、且对阵列标定敏感，不利于"全场景鲁棒"。空间维优化列为未来 ceiling 研究，默认按 per-link。
- **复杂度护栏**：massive 下 FFT 数随链路线性增长，需保证单 SRS 块仍 < 1 ms；必要时按 RX 子集/共享 PDP 降算量（Phase 5 评估）。

---

## 三、分阶段任务与 Gate

> 原则：**离线每次几秒，C 端每次约 10 分钟**（前日志教训）。所有数值结论必须先在离线拿到，C 端只做"已验证算法的忠实移植"。

### Phase 0 — 数据矩阵生成（解锁一切的前提）
- [x] CDL-A~E ray 数据路径已确认（`data_out/cdl_{a,b,c,d,e}`）。
- [ ] **Smoke test**：先用 CDL-C 单点（速度 3、SNR 15、MAX_FRAMES 50、2×2）跑通 `P1B_NPZ="" NPY_DIR=.../data_out/cdl_c`，确认 GT-SRS 对齐、scale 正常。
- [ ] **全 6 信道数据生成**：P1B + CDL-A/B/C/D/E，每种 速度 {3} × SNR {5,15,25}（先低速），2×2，seed=42，MAX_FRAMES≥300，归档 `logs/`。
- [ ] **速度扩展**：至少在 CDL-A、CDL-C 上加 速度 {30}，验证时域级与时变下的频域 Wiener。
- **Gate 0**：全 6 信道每 run 的 `eval_nmse_clean.py` Legacy baseline 跑通且 GT-SRS 配对正常（paired≥2、gap 合理）。
- 风险：CDL 数据 scale/格式与 P1B 不一致导致对齐失败 → 先 smoke test 单点定位。

### Phase 1 — 离线攻克"地板/秩"（真正的核心，纯 Python）✅ 完成 2026-05-29（详见 `0529_Phase1_EMA_Wiener_离线日志.md`）
在 `test_srs_2d_offline.py` 内新增/改造（**不动 C**）：
- [x] 因果 EMA 版 `data_pdp_ema_wiener_active`（按帧顺序更新 PDP），扫 α∈{0.9,0.95,0.98,0.99}。
- [x] 噪声地板 `tail-mean` + **相对峰值秩选择 `rel_db`**（替代 energy_frac，对 filt8 有色噪声鲁棒）。
- [x] 诊断量：`gain_nonzero`、有效秩 K、per-frame p10/50/90；新增 `--synth-mf` 多帧合成频选验证。
- **Gate 1 PASS**：真实 P1B data-only Wiener -17 → **-45.12 dB**（K=1，gnz 0.10%，追平 oracle/top1）。合成 flat/2tap/4tap **全部追平 oracle**，MA 在频选崩。
- 固化参数（供 Phase 3）：N_act 精确 FFT、α=0.95、rel_db=20、tail-mean floor、gamma=2、kept-tap soft gain、warmup fallback MA。

### Phase 2 — 离线全矩阵泛化验证（证明"全场景"）
- [ ] 把 Phase 1 最优配置在**全 6 信道**（P1B + CDL-A/B/C/D/E）矩阵上跑离线评估，对比 raw / ma129 / wiener / oracle。
- [ ] 重点确认：CDL-A/B/C 上 **Wiener ≫ MA** 且优于 Legacy；CDL-D/E、P1B 不劣化。
- [ ] 速度敏感性：在 CDL-A/C 上加 30 m/s，确认频域 Wiener 在时变下仍成立（PDP 是二阶统计量，应稳定）。
- **Gate 2**：离线全 6 信道每 cell 相对 Legacy 改善 > 0 dB（ε=5%），**无任一信道退化**。
- 失败处置：若某类信道退化 → 回到 Phase 1 调秩/地板，或对该类启用 fallback。

### Phase 3 — C 端忠实移植（仅在 Gate 2 通过后）
- [ ] 在 `nr_srs_freq_smooth.c` 内新增 Wiener 路径，由环境变量选择（`SRS_FREQ_SMOOTH_METHOD=ma|wiener`，默认 ma 向后兼容；命名与现有 `SRS_FREQ_SMOOTH_*` 家族一致）。
- [ ] **精确 N_act 点变换**：vendor 一个任意长度 FFT（KISS-FFT 单文件 BSD，N_act≈1248 mixed-radix，<<1ms），**绝不零填充到 2048**。
- [ ] **持久 PDP-EMA 状态按运行时 `n_rx×n_tx` 动态分配**（严禁硬编码 2×2；复用 Kalman 状态机的 per-(ant,port) 模式），逐链路独立处理 → MIMO 阶数无关（§2.4）。
- [ ] 32B 对齐（`memalign(32,...)`，前日志已踩）；warmup/塌缩 fallback 到 MA；不引入 `amp_scale` hack。
- [ ] 编译验证：`gcc -fsyntax-only`、CMake 构建、单帧不崩；并在 4×4 配置下确认状态分配/无越界（哪怕不调优）。
- **Gate 3**：C 端单场景（P1B）NMSE 与离线一致（误差 < 0.5 dB），延迟 < 1 ms；4×4 配置可跑通不崩。
- 风险：DFT 长度/对齐/状态生命周期/MIMO 维度 → 先 P1B 单 SNR smoke，再扩。

### Phase 4 — 端到端全矩阵验收
- [ ] C 端在全 6 信道矩阵上 sweep，复现 Gate 2 的离线结论。
- [ ] 更新日志（`0529_*` 实装日志），归档全部 run。
- **Gate 4**：端到端全 6 信道每 cell 改善 > 0 dB（ε=5%），无任一信道退化，Legacy 路径回归无变化。

### Phase 5 — MIMO 扩展与时域升级
- [ ] **4×4 端到端验收**（ALDLC §5.2 目标"4×4 常态化"）：全 6 信道在 4×4 下相对 Legacy 改善 > 0 dB；评估延迟 < 1 ms。
- [ ] massive MIMO 复杂度评估 + 跨 RX 共享 PDP-EMA 优化（§2.4）。
- [ ] （可选）高 Doppler 时域级升级（AR(2)/固定滞后 Wiener）；参数化超分辨 ceiling 研究。

---

## 四、风险登记表

| # | 风险 | 触发阶段 | 缓解/回退 |
|---|------|---------|----------|
| R1 | CDL 数据 scale/对齐与 P1B 不同 | P0 | 单 SNR 小帧 smoke；复核 STO/幅度对齐 |
| R2 | soft Wiener 地板始终漏噪声，flat 到不了 -35 | P1 | 两段式（低秩投影→Wiener）；自适应秩 |
| R3 | 某类 CDL 退化 | P2 | 该类 fallback 到 MA/Legacy；调秩 |
| R4 | 任意长度 FFT 移植/对齐/性能 | P3 | KISS-FFT；P1B smoke；延迟 profiling |
| R5 | per-(ant,port) 状态生命周期错误 | P3 | 对照 Kalman 状态机；warmup fallback |
| R6 | 时变下 PDP-EMA 滞后 | P2/P4 | α 自适应；高 Doppler 缩短记忆 |
| R7 | massive MIMO 下逐链路 FFT 算量超 1 ms | P5 | 跨 RX 共享 PDP、按子集处理、SIMD |
| R8 | 状态数组硬编码 2×2 → 4×4 越界/崩 | P3 | 按运行时 n_rx×n_tx 动态分配，4×4 smoke |

---

## 五、决策点（已全部锁定 2026-05-29）

1. **CDL 数据路径**：`data_out/cdl_{a,b,c,d,e}`（全 5 种 + cdl_c_30kmh）。✅
2. **数据矩阵规模**：Phase 0 = 全 6 信道 × {3 m/s} × SNR {5,15,25} × 2×2；CDL-A/C 额外 {30 m/s}。✅
3. **速度档**：低速 3、中速 30、高速 **120 m/s**（选做）。✅
4. **环境变量命名**：采用 `SRS_FREQ_SMOOTH_METHOD=ma|wiener`（比旧 `SRS_FREQ_METHOD` 更一致）。✅
5. **Gate 1 目标**：P1B flat data-only Wiener **≤ -30 dB** 即达标。✅
6. **MIMO**：Phase 3 起按**阶数无关 + 自适应**编写，**当前全部在 2×2 验证**，4×4 端到端测试延后到 Phase 5。✅

---

## 六、数据/文件归档约定

| 用途 | 路径 |
|------|------|
| CDL ray 数据 | `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/data_out/cdl_{a,b,c,d,e}` |
| CDL 生成/适配 | `gen_cdl_rays_standalone.py` / `cdl_ray_adapter.py` / `multisc_cdl_sim.py` |
| 离线脚本 | `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/test_srs_2d_offline.py` |
| 频域核心(待加 Wiener) | `openair1/PHY/NR_ESTIMATION/nr_srs_freq_smooth.c` |
| 评估 | `eval_nmse_clean.py` / `eval_freq_smooth.py` |
| 全矩阵 run | `DevChannelProxyJIN/logs/<timestamp>_<tag>/snr_*dB/` |
| 本计划 | `0529_True2D_MMSE_先期计划.md` |
