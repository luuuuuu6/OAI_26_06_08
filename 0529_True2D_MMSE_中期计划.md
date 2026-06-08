# 真·2D MMSE —— C 端建造中期计划（保守版）

| 字段 | 值 |
|------|----|
| **日期** | 2026-05-29 |
| **上游** | `0529_True2D_MMSE_先期计划.md`（总体）、`0529_Phase1_EMA_Wiener_离线日志.md`（离线算法） |
| **范围** | 从"离线算法"安全落地到"OAI C 运行代码"的全过程；**Gate 未过不写下一阶段代码** |
| **总原则** | 离线每次几秒、C 每次约 10 分钟且需 sudo；**任何数值结论先离线拿到，C 只做已验证 recipe 的忠实拷贝**。重演上次 Wiener 失败（alignment / 变换域 / scaling）的代价极高。 |

---

## 〇、为什么需要这份计划（诚实状态）

先期计划里 Phase 1.5 标了"自适应完成"，但那是在**理想白噪声+稠密**合成上。深入到"C 将要用的真实输入"后暴露三个尚未跨过的坎：

| # | 发现 | 含义 |
|---|------|------|
| 1 | 之前 -42/-45 是**导频-only 比较**（自己考自己，偏乐观） | 真实**稠密诚实 NMSE** 只有 ~-20（P1B passthru），但仍优于 Legacy -10.87 约 +9dB |
| 2 | 2048 零填充 idft → sinc 泄漏，rankK 爆、结果垃圾 | **必须精确 n_pil 点 FFT**（624 等非 2 的幂）→ 需任意长度 FFT（KISS-FFT） |
| 3 | auto-gamma 在合成白噪声很好，但真实数据**过度保留**（filt8 K=665；passthru K=47） | 自适应在真实伪影（有色噪声/稀疏导频/STO/量化）下**尚不鲁棒** |

结论：**当前还不能安全进 C**。必须先在离线把"真实输入链路 + 鲁棒自适应"做实（M0），再进 C（M1+）。

> **2026-05-29 真 2D 判定补充（详见 `0529_True2D_离线判定日志.md`）**：
> 1. 真 2D（全 Toeplitz）比对角高 **5–7 dB**、真实存在；可鲁棒落地的频率阶 = **窗口 Toeplitz LMMSE（= C 已有 `nr_srs_mmse_freq_filter`），不是对角**。
> 2. 5–7dB 增益来自精确 R_f；周期图数据 R(`FT{PDP}`≡经验自相关)受 DFT 泄漏偏差封顶（再平均无效）。
> 3. **ESPRIT 参数化 R 不鲁棒**：宽/密延迟谱(cdl_c/e)灾难性退化，弃出主线。
> 4. **C 现状零件齐未拼对**（`nr_ul_channel_estimation.c:968-1006`）：`mmse1d`=Toeplitz频但无时域级；`mmse2d`=filt8+MA(弱)+Kalman。真 2D 落地 = 把 mmse1d 的 Toeplitz 频阶接到 mmse2d 的 Kalman 时阶。
> 5. 剩余到 oracle 的 4–6 dB = 研究天花板，"全场景鲁棒/永不退化"约束下不追。

---

## 一、固定的设计决策（已由离线锁定，不再反复）

| 项目 | 决策 | 依据 |
|------|------|------|
| 作用对象 | **raw LS 导频**（白噪声），**不**用 filt8 输出（有色噪声，自适应崩） | 日志 §6/§7 + filt8 floor_k 扫描 |
| 频域变换 | active band / 导频的**精确 n_pil 点 FFT**，**禁**零填充到 2048 | 坎 #2 |
| 频域核心 | **窗口 Toeplitz 频域 LMMSE**（R=FT{清洗PDP}，per-link，MIMO 阶数无关）—— **= C 已有 `nr_srs_mmse_freq_filter`**；**不再用对角 Wiener**（弱 1–3dB） | 先期 §2.2（已修正）、`0529_True2D_离线判定日志.md` |
| 秩选择 | 频阶 Toeplitz 经 R=FT{清洗PDP} 隐式正则（窗口 W 控复杂度/正则）；不再依赖显式 rank 裁剪 | 判定日志 §二 |
| 时域级 | **per-link ω 相位预测 Kalman（PKF）+ IAE-Riccati 自适应 K**，**替换** `nr_srs_2d_filter` 全局去旋（去旋频选/相干多普勒下崩） | 先期 §2.3（已修正）、判定日志 §4.5 |
| 自适应 K 的 R_meas | **频阶传播白噪声 `σ²‖bᵀA⁻¹‖²`**（频阶 A,b,σ² 现成；坑：用 `A⁻¹conj(b)` 非 `A⁻¹b`）。已离线闭合 | 判定日志 §4.5.7 |
| 评估口径 | **稠密诚实 NMSE**（全 active SC，含插值位置），**禁**导频-only | 坎 #1 |
| 验收基准 | 相对 Legacy 的诚实稠密增益（P1B 目标 ≥ +6dB），**不**追虚高 -45 | 坎 #1 |

---

## 二、阶段与 Gate

> **2026-05-29 C 工作分解修正（覆盖下方 M1/M2 的"从零建对角 Wiener"旧框架）**：
> 频率阶**不再新建** `nr_srs_delay_wiener`/KISS-FFT —— 直接**复用 C 已有的 `nr_srs_mmse_freq_filter`**（它已是窗口 Toeplitz LMMSE：2048 dense-pilot IFFT → PDP → R=FT{清洗PDP} → 窗口 Toeplitz 解）。C 工作收敛为三块：
> - **F. 频阶验证/调参**：确认现有 `nr_srs_mmse_freq_filter` 数值≈离线窗口 Toeplitz（**关键 Gate**：2048 零填充 IFFT 估 R vs 离线精确网格，需验证一致；必要时加窗 W、或改 R 估计网格）。
> - **T. 时域级重写**：`nr_srs_2d_filter` 删全局去旋，改 **per-link ω + per-SC 复数状态的 PKF**（先期 §2.3、判定 §4.5.5）。
> - **C. 级联接线**：`mmse2d` 路径 = `nr_srs_mmse_freq_filter`（频）→ PKF（时），替换现有 filt8+MA+去旋。
> 下方 M1/M2（KISS-FFT、新对角 Wiener、auto-gamma rank）仅在"频阶验证发现现有函数不达标需另起"时才启用，否则跳过。M0 诚实稠密口径与 Gate 仍有效。

### M0 — 离线把"真实输入链路 + 鲁棒自适应"做实（**进 C 的前置，纯 Python**）

目标：在 `test_srs_2d_offline.py` 的 `data_ema_wiener_cport`（LS导频 + 精确FFT + 稠密重建）上，让 auto-gamma 在**真实 P1B passthru** 上稳定、诚实地优于 Legacy。

- [ ] M0.1 统一口径：cport 一律用精确 n_pil FFT + 稠密诚实 NMSE（已具备 `--cport-nfft 0`）。
- [ ] M0.2 诊断真实数据过保留根因：robust floor 在稀疏导频/STO 下是否偏低？K_TC 混叠是否制造伪峰？逐项定位。
- [ ] M0.3 让 auto-gamma 在真实 passthru P1B 稳定收敛到合理 K（flat 应 ~1-数个），诚实稠密 NMSE 稳定。
- [ ] M0.4 鲁棒性：扫 SNR（若有多 SNR passthru）、确认 EMA warmup、塌缩 fallback 行为。
- **Gate M0**：真实 P1B passthru **诚实稠密 NMSE ≥ Legacy + 6 dB**，且 auto-gamma 无需按数据手调（K 自动）。
- 失败处置：若真实伪影无法用纯 auto-gamma 解决，允许引入一个**有物理含义的鲁棒化**（如 STO 去斜、混叠抑制），但仍须是自适应、可移植 C。

### M0.5 —（可选）CDL 真实链路前置
- [ ] CDL 端到端 attach 阻塞（`FAIL-ATTACH-UL`，上行多径定时）。若要 CDL 真实验证，须先打通 attach（独立工作项，不阻塞 M0/M1 的 P1B 路线）。
- 在此之前 CDL 仅有"ray 数据离线合成"证据（先期 §2.2）。

### M1 — C 端数学与变换基座（**Gate M0 通过后**）
- [ ] M1.1 vendor 任意长度 FFT（KISS-FFT，单文件 BSD）或等价；接入 CMake；32B 对齐封装。
- [ ] M1.2 移植纯数学：`_norm_ppf`(Acklam)、Wilson-Hilferty chi² 分位点、`robust_noise_floor`、`adaptive_floor_threshold` —— **独立单元测试**（喂固定向量，比对 Python 输出，误差 < 1e-6）。
- [ ] M1.3 FFT 往返单元测试：随机向量 idft→dft 复原、与 numpy 比对 scaling。
- **Gate M1**：所有数学/FFT 单元测试通过（`gcc` 独立编译 + 比对 Python），不接触 OAI 主路径。

### M2 — C 端核心 Wiener 函数（离线 recipe 的忠实拷贝）
- [ ] M2.1 新函数 `nr_srs_delay_wiener(ls_est, ofdm_size, first_sc, k_tc, num_pilots, ant, port, out)`：
  收集导频 → 精确 n_pil FFT → per-link EMA PDP → `adaptive_floor_threshold` → soft gain → 稠密重建写 out。
- [ ] M2.2 per-(ant,port) EMA 持久状态：按运行时 `n_rx×n_ap` **动态分配**（严禁硬编码 2×2）；线程/生命周期对照 Kalman 状态机。
- [ ] M2.3 warmup（EMA 未收敛）/ 塌缩（全噪声）→ fallback 到 Legacy/MA，**永不比输入差**。
- [ ] M2.4 **离线-C 一致性**：同一帧输入喂 Python 与 C，逐 SC 比对输出（误差 < 量化级）。
- **Gate M2**：单帧 C 输出与离线 recipe 数值一致（< 0.5 dB / 量化级），不崩、32B 对齐无 assert。

### M3 — 集成与端到端
- [ ] M3.1 新模式 `SRS_ESTIMATOR=wiener`（新枚举），dispatch 仅走新分支，**不动** legacy/mmse2d/freqsmooth 路径。
- [ ] M3.2 环境变量：`SRS_WIENER_*`（EMA alpha、p_fa、debug），默认关闭、向后兼容。
- [ ] M3.3 端到端 P1B sweep，复现 Gate M0 的诚实稠密 NMSE。
- [ ] M3.4 性能：单 SRS 块 < 1 ms（含 n_pil FFT）。
- **Gate M3**：端到端 P1B 诚实稠密 NMSE 复现离线（误差 < 1dB）、延迟达标、Legacy 回归无变化。

### M4 — 全场景与 MIMO 扩展（对齐先期计划 Phase 4/5）
- [ ] CDL 真实数据（须 M0.5 attach 打通）端到端验证全 6 信道。
- [ ] 4×4 端到端；massive 复杂度评估（跨 RX 共享 PDP）。
- **Gate M4**：全 6 信道每 cell 相对 Legacy > 0 dB（ε=5%），4×4 不崩。

---

## 三、安全规则（从上次 Wiener 失败提炼，强制）

1. **禁零填充变换**：必须精确 n_pil 点 FFT，否则 sinc 泄漏。
2. **32B 对齐**：FFT 缓冲 `memalign(32,...)`，OAI AVX2 DFT 要求。
3. **禁 amp_scale hack**：后验功率归一化救不了相位/形状错误；scaling 必须从一开始就对。
4. **新模式隔离**：只加新分支，绝不改动已工作的 legacy/kalman/freqsmooth。
5. **离线-C 逐点比对**：每个 C 阶段都有对应的 Python 黄金参考，数值对齐才算过。
6. **状态动态分配**：per-(ant,port) 按运行时维度分配，4×4 smoke 必测。
7. **永不退化**：warmup/塌缩 fallback，保证 ≥ Legacy。

---

## 四、当前待办（紧接，2026-05-29 按真 2D 判定刷新）

| 优先 | 工作 | 状态 |
|------|------|------|
| 1 | **F：频阶** —— 修复共轭 bug（`A·w=conj(b)`/`out=Σconj(w)y`）+ EMA PDP + robust floor + R_meas 输出 | ✅ 已落 C（lint 净），#4(2048) 离线验证 OK |
| 2 | **T：时域级** —— 新增 `SRS_2D_METHOD=pkf`/`nr_srs_pkf_update`：per-link ω + per-SC PKF + IAE-Riccati，R_meas 从频阶 `g_freq_rmeas` 读 | ✅ 已落 C，Q/P 初值+warmup 对齐离线 |
| 3 | **C：dispatch** —— `SRS_ESTIMATOR=true2d`：Toeplitz频(ant≥0,出R_meas)→PKF时 | ✅ 已落 C（隔离，未碰 legacy/mmse1d/mmse2d）|
| 4 | **编译 + 端到端**（sudo，用户执行）：A) mmse1d 不回归/不崩；B) true2d P1B + `SRS_2D_DEBUG=1` 看 R_meas/PKF | ⏳ 待用户编译 |
| — | （旧）M1 KISS-FFT / 新对角 Wiener | **取消**：#4 验证 2048 够用，无需 KISS-FFT |

> **2026-05-29 进 C 完成（详见判定日志 §4.6）**：频/时/dispatch 三块代码全落、lint 干净；发现并修复**频阶共轭严重 bug**（`weights=A⁻¹b`→`bᵀA⁻ᵀy`，频选必崩，只平坦在格对）；#4 离线验证 2048/梳状标定 OK。改动文件：`nr_srs_mmse.{c,h}`、`nr_srs_2d_filter.{c,h}`、`nr_ul_channel_estimation.c`。**仅剩编译+端到端（交用户）**。

> **频/时可并行**：F（频阶，纯验证现有 C）与 T（时阶，新写 PKF）互不依赖，可同时推进；C（接线）待两者就绪。

---

## 五、决策点（待确认）

1. M0/F 验收阈值"P1B 诚实稠密 ≥ Legacy + 6 dB"是否接受？（现 cport ~+9dB at -20 vs -10.87）
2. **频阶**：先验证复用现有 `nr_srs_mmse_freq_filter`（推荐，省 KISS-FFT），还是仍按旧 M1/M2 新建对角 Wiener？（判定结论支持复用）
3. **时域级 K**：PKF 的 K 用固定值，还是沿用现有 Riccati/IBVSS 自适应（作用在 per-SC 复数残差）？
4. **推进方式**：频(F)、时(T)并行开工，还是先 F 后 T？（二者无依赖，建议并行）
5. CDL attach 打通（M0.5）是否并行立项。

---

## 七、端到端结果 + CDL attach 阻塞 + 转 OAI 原生 rfsim（2026-05-30）

### 7.1 true2d 端到端（P1B）：通路全对，但平坦信道无增益
- ✅ 编译通过（`ninja nr-softmodem`）；gnb.log 实证 `mode=true2d`、状态 4×4×8192 动态分配、**R_meas 已喂入**（无 warning）、PKF 逐 (ant,port) 在跑。集成/plumbing 完全正确。
- ⚠️ NMSE：legacy -10.46 vs true2d -9.58（per-ant），true2d **略差**。原因（非 bug）：
  1. **P1B 近平坦**（相干带宽 5× NR 带宽，见 `0528日志.SRS_NMSE_Floor_RootCause.md`）→ Toeplitz 频阶相对 legacy filt8 无优势（平坦下 filt8 近最优）。
  2. **时阶 K 恒=0.95**（innov≫R_meas）→ 不平滑。omega≈-1.7 rad/帧（每帧公共相位）。
- **proxy 不是精度瓶颈**：0528 根因日志证明 -10 是"每 SC LS 噪声"，全带频域平均可达 **-24.9 dB(SNR15)**，headroom ~15dB。真 2D 的价值在**频选**，P1B 展示不了。

### 7.2 CDL 端到端 attach 阻塞（cdl_a/c 均 FAIL）→ 根因 = UL 多径下 PRACH 定时不稳
- cdl_c：ulsch BLER 92%；cdl_a（DS 最低 30ns）：ulsch BLER 仅 ~10% 但**仍 FAIL-ATTACH-TIMEOUT(WAIT_RRC_RECFG)**。
- 关键证据：`timing_offset` 散乱 **0~18 样本（估距 0~703m）**，远超信道实际 span（cdl_a 290ns≈9样本）→ **PRACH 峰值检测被多径带偏/抖动 → TA 乱跳 → Msg3 max harq / RA 窗超时 → RRC PDU all-zero → 卡 WAIT_RRC_RECFG**。
- **判定**：是 **OAI 接收机 PRACH/UL 定时 + proxy 多径时序**的问题，**与 2D MMSE 算法、与本次 C 改动无关**（legacy 同样崩、发生在 attach 前）。修它工程量大且对验证算法无价值 → **放弃修 proxy CDL attach**。

### 7.3 转向：OAI 原生 rfsim + channelmod（attach 可靠 + 可上 CDL 等价信道）
- OAI `channelmod` 模型（`sim.h:240`）：含 **TDL_A/B/C/D/E**（= 3GPP 把 CDL-A/E 塌缩的纯延迟谱，对频域 SRS 估计**等价 CDL**）+ **`custom`**（可喂入我们 CDL a-e 的 (tau,power) 抽头）。**无 "CDL" 命名模型**。
- 原生 rfsim 自洽处理 PRACH/定时 → attach 可靠（CI 标准路径）。
- **代价 = 无现成 GT**：channelmod 内部生成信道 → 需 dump channelmod 真实信道 或 按确定性 (tau,amp,seed,Doppler) **离线重算** 当 GT。这是接下来核心活（比修 proxy attach 容易得多）。
- conf 起点：`targets/PROJECTS/GENERIC-NR-5GC/CONF/gnb.sa.band78.*rfsim*.conf` + `ue.*.conf`。
- **计划**：①最小 rfsim(gNB+UE)+TDL_C 验通 attach/SRS/true2d 在跑（无 GT）；②加 GT（dump 或重算）；③TDLC 上 legacy vs true2d 评估 → 首次展示频选增益。

### 7.4 原生 rfsim 尝试结果（2026-05-30）→ 本 build 原生样本流被 proxy 改造破坏
- 能力确认：`librfsimulator.so` 含 `init_channelmod/tdlModel/random_channel`；GPU-IPC 仅由 `RFSIM_GPU_IPC_V*` env 门控（不设即走原生）；proxy 用的 conf `gnb.sa.band78.fr1.106PRB.usrpb210.conf` 已含 `do_SRS=1` + channelmod 段(TDL_A)。
- 实跑（gNB 原生 rfsim server 6014 + UE 直连，**理想信道、未挂 chanmod**）：
  1. 端口对齐后 UE 能连上（`A client connects`）。
  2. 但 gNB 无 UE 时**自由空转** `nextRxTstamp` 冲到 6e8（simulator.c:2231-2246，空样本循环无实时节拍）→ UE 连上 `Not supported to send Tx out of order` → **UE `pbch not decoded` 死循环、同步不了**。
- **判定**：**理想信道下都同步不了 → 是这个 proxy 改造 build 的原生 rfsim 时序流控坏了**（gpu_ipc_v1~v8 改造遗留），与 2D MMSE 算法、与 C 改动无关。修复需在 `radio/rfsimulator/simulator.c` 的 native 分支（`!use_gpu_ipc_*` 门控，保 proxy 不变）做连接时刻时间对齐 + 加大 `--rfsimulator.wait_timeout` 节拍。

### 7.5 当前总状态与路线决策（2026-05-30）
**算法侧已闭环**：离线对 oracle 严格验证（窗口 Toeplitz 频 × 相位预测 Kalman 时；含共轭修复、R_meas 数据驱动闭合）；C 已实现并**编译通过、端到端 plumbing/PKF/R_meas 实测在跑**。

**三条端到端路全被"环境"卡住（均非算法/非本次 C 改动）**：
| 路径 | 阻塞 | 性质 |
|------|------|------|
| P1B(proxy) | 平坦信道，Toeplitz 无用武之地；地板 -10 是每 SC LS 噪声（可达 -25），time stage 因每帧相位 K 顶格不平滑 | 信道类型不对 |
| CDL(proxy) | UL 多径 → PRACH 定时乱跳 → attach 失败 | OAI/proxy 接收机 |
| 原生 rfsim(本build) | 原生样本流时序坏 → 理想信道也同步不了 | proxy 改造遗留 |

**路线决策（两个目标，工具不同）**：
- **A. 证明"算法/C 实现正确"（推荐，小而稳）= M2.4 离线-C 逐点比对**：把 C 的 `update_pkf_core`（时，纯算术无 FFT 依赖，最易）与 `nr_srs_mmse_freq_filter`（频，需对齐 OAI idft）抽成独立单元测试，喂固定向量逐 SC 比对 `test_srs_2d_offline.py` 黄金参考。一致 ⟹ C = 已验证正确算法。**不碰 attach/信道/GT/rfsim。** ✅ **已完成（2026-05-30，详见判定日志 §4.8）**：harness 在 `openair1/PHY/NR_ESTIMATION/c_unit_tests/`（`./run_all.sh`）。频阶**逐位一致(0–1 LSB)**；时阶发现并修复 Q 递推一处真实分歧（原 `frame_count>3` 门控 + EMA 后裁地板，早期帧差 ~590 LSB → 对齐离线后 ≤1 LSB），已改 `update_pkf_core`，**已编译进 `nr-softmodem`（2026-05-30）**。**C1（OAI 真定点 idft 核对，判定日志 §4.8.1）已完成**：定点 `idft2048` 对频阶输出仅扰动 −41~−44 dB、PDP 形状误差<1% → 非精度瓶颈，浮点参考结论可迁移运行系统。**至此①算法②C=算法③编译均闭环，仅剩④真实频选端到端演示（环境受限）。**
- **B. 在真实系统频选信道演示增益（大、有环境风险）= 原生 rfsim 通路**：见保存的计划 `.cursor/plans/native_rfsim_true2d_*.plan.md`（修 native 时序 + GT dump + TDL_C 评估，全程 GPU-IPC 门控、proxy 可切回不变）。

---

## 六、归档

| 用途 | 路径 |
|------|------|
| 总体先期计划 | `0529_True2D_MMSE_先期计划.md` |
| 离线算法日志 | `0529_Phase1_EMA_Wiener_离线日志.md` |
| 本中期计划 | `0529_True2D_MMSE_中期计划.md` |
| 离线脚本（统一 harness） | `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/test_srs_2d_offline.py`（`--true2d`/`--cascade`） |
| 离线 #4 验证 | `.../check_c_freq_mirror.py` |
| C 改动（已落、已编译） | `nr_srs_mmse.{c,h}`（共轭修复+EMA+robust floor+R_meas+`SRS_ESTIMATOR=true2d`）、`nr_srs_2d_filter.{c,h}`（`SRS_2D_METHOD/nr_srs_pkf_update`=PKF+Riccati+STO诊断）、`nr_ul_channel_estimation.c`（true2d dispatch） |
| 端到端日志 | `logs/q4_sweep_20260530_000314`(legacy)、`_000635`(true2d P1B)、`_002820`(cdl_c FAIL)、`_103215`(cdl_a FAIL) |
| 原生 rfsim 计划 | `.cursor/plans/native_rfsim_true2d_*.plan.md` |
| 判定日志 | `0529_True2D_离线判定日志.md` |
