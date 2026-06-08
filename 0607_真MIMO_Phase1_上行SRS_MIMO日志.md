# 真 MIMO 升级 Phase 1 —— 上行 SRS N_rx×N_tx 信道估计(成功与失败全记录)

| 字段 | 值 |
|------|----|
| 日期 | 2026-06-07 |
| 目标 | 把 native OAI rfsimulator 从 1T1R 升级到**真上行 SRS MIMO**(UE 多端口 SRS × gNB 多 RX → 真 N_rx×N_tx 信道估计),参数化到 4×4 |
| 测试床 | native rfsimulator + channelmod;`launch_native.sh -e true2d`;`eval_nmse_sto.py` 评估 |
| 结论 | **真 1×1/2×2/4×4 SRS MIMO 估计端到端全部跑通且稳定**(2×2 −16.3dB、4×4 −15.3dB,coh≈0.98,标度符合功率分摊);先前"2 端口 ~16dB 代价"经逐层诊断**定位为我自己的 rfsim 信道重建 bug**:重建上行信道时把 `noise_power_dB` 硬编码为 0,丢掉了 `-nd -6`,只在 2 端口路径注入 ~12dB 额外噪声(1 端口不重建故正常)。**已修复并验证**(继承 `old->noise_power_dB`/`path_loss_dB`):重编后 2×2 NMSE −4.7→**−16.3dB**、coh 0.84→**0.984**。三个误判(de-spread、阶梯混叠、"真 SNR 物理代价")均已证伪/更正。 |
| 提交状态 | 本会话全部改动**尚未 commit**(submodule + 父仓库) |

---

## 0. 背景:为什么之前不能 MIMO
- 历史:native 路径长期只能 **1T1R**(`2T2R/2端口SSB 在原生路径不工作`,见 `0531` 日志 §12.3),所有 capture 都是 `srs_matrix_gNB_1x1`。
- 区分两种 MIMO:**上行 SRS MIMO(本期,N_rx×N_tx 信道估计)** vs **下行/完整空间复用(多层数据吞吐,未做)**。上行 SRS MIMO 与下行 SSB 解耦,可在保持 DL SISO 下实现。

---

## 1. ✅ 成功的改动(真 MIMO 使能,全部保留)

### 1.1 rfsim 上行信道按对端天线数重建 —— 解 attach
**根因**:UE 发 2 天线时 attach 失败(PBCH/SIB1 OK,但 **RAR 一直失败**)。gNB 上行接收信道建成 `nb_tx=1`(绑的是 gNB 自己的 DL 发射数),而 UE 实发 2 天线 → gNB 把 2 天线交织缓冲按 1 天线读 → PRACH 毁 → 收不到 → 不发 RAR。
**修复** `radio/rfsimulator/simulator.c`(server 读包路径):当对端 `th.nbAnt > channel_model->nb_tx` 时,**按对端天线数重建上行信道**(`nb_tx=nbAnt`,nb_rx 不变,保留模型类型)。**解耦了"UL 信道 nb_tx"与"gNB DL 发射数"** → DL 保持 SISO 不触发 SSB bug。
**验证**:attach 成功,gNB 打印 `[rfsim MIMO] rebuilt channel ... nb_tx 1->2`。

### 1.2 `generate_srs_nr` 用 `max_ant_ports` 解耦 —— 解 port1 全空
**根因**:首次 2×2 跑通但 **tx1 两条 link 全是 −300dB(空)**。`generate_srs_nr` 用 `frame_parms->nb_antennas_tx` 给生成端口数封顶;gNB 调它生成**参考序列**时,gNB DL `nb_antennas_tx=1` → 砍成 1 → **只生成 port0 参考,port1 参考全零** → LS 对 port1 = 与零相关 = 0。
**修复**:`srs_modulation_nr.c/.h` 给 `generate_srs_nr` 加 `max_ant_ports` 参数;UE 传 `nb_antennas_tx`(端口映射物理天线,不越界),**gNB 传 `MAX_NUM_NR_SRS_AP=4`**(生成全部配置端口的参考,与自身 DL 天线无关)。`phy_procedures_nr_gNB.c` 调用点同步。
**验证**:tx1 不再 −300dB,四条 link 都有内容。

### 1.3 `cdl_to_cir.py` 参数化多 TX×RX CIR —— 真独立 2×2 GT
**根因**:带 `-cir` 跑 2×2 时,GT 退化(`rx1: tx0~tx1` 空间相干 =1.0)。`cdl_to_cir.py` 只生成 **SISO(1×1)** CIR;`cir_replay_apply` 只覆盖 pair0=(rx0,tx0),其余 3 条 link 是初始化的随机 TDL → 非一致 2×2。
**修复**:`cdl_to_cir.py` 加 `--nb-tx/--nb-rx`(到 4×4),每条 (tx,rx) link 独立子种子 → 独立小尺度衰落(rich-scattering i.i.d.);载荷 pair=rx+tx*nb_rx;补使用说明 + 维数提醒 + "为何 CDL 必须转换"。
**验证**:2×2 CIR 链路间空间相干 0.36(独立),GT 四链路功率各异、不再退化。

### 1.4 `apply_channelmod.c` —— CIR 多 pair 填充 + 平坦旁路多 TX 求和
- `cir_replay_apply`:遍历**全部信道 pair**、统一 realloc 到 `cir_L`、文件没有的 pair 填零、维度不一致**告警** → 支持真 2×2/4×4 填满,且消除"SISO 文件跑 MIMO"的越界隐患。
- 平坦旁路(handoff 前)对**所有 TX 天线求和**(原只读 TX0)。

### 1.5 `launch_native.sh` —— 一键多端口 SRS
加 `UE_TX`/`UECAP`(默认 1):`UE_TX>1` 时 UE 命令自动加 `--ue-nb-ant-tx N --uecap_file uecap_portsN.xml`;gNB 保持 DL SISO(`pdsch_AntennaPorts_XP 1`)、`pusch_AntennaPorts=NB_RX`。

### 1.6 `eval_nmse_sto.py` —— 自适应 STO(修了一个静默 bug)
**发现的 bug**:默认 `--tau-range 30` 太窄。capture 定时偏移超 ±30(某发 1×1 实际 −33,且多峰 −33/−1/−17)时,eval **静默对齐到窗边缘 → 假 NMSE −0.03dB**,相干门(0.50)拦不住(边缘 coh 0.628)。
**修复**:
1. **自适应 STO(默认)**:每帧用互谱 `SRS·conj(GT)` 的延迟域峰值直接粗估 STO(无窗、`[−N/2,N/2)` 无歧义)+ 附近 ±3 细搜 → 任意偏移/多峰自动锁定(`_sto_coarse`/`_sto_es_adaptive`)。
2. 旧固定窗(`--tau-range N>0`)保留 + **边缘饱和告警**(>5% 帧落窗边 → 提示加大)。
**验证**:同发 1×1 默认自适应自动给 **−21.66dB**(旧默认窗静默 −0.03);控制组 214506 仍 −24.93dB(与历史一致)。

---

## 2. ❌ 失败的改动(已完全回退)

### de-spread(SRS 端口"延迟域去泄漏")
**错误动机**:以为 2 端口循环移位会让另一端口泄漏进每端口 LS,需要延迟域开窗剔除。

**v1(c16 定点 idft/dft 往返)**:de-spread **ON 反而更差**(coh 0.84→**0.36**,NMSE −4.8→+2.5)。**原因**:OAI c16 定点 `idft→dft` 往返非干净逆变换(缩放/量化)→ 把 LS 搞坏。

**v2(改 double 精度 M 点 DFT,numpy 验证 coh 0.39→0.999)**:不再损坏,但 **ON ≈ OFF(完全无效果)**。**根因**:**OAI 自带 FD-CDM2 解扩**(gNB LS 的 `fd_cdm=N_ap` 累加,2 端口在相邻两导频上是正交覆盖码 [1,1]/[1,−1],交叉项 `1+e^{jπ}=0` 相消)早就把端口分干净 → **根本没有泄漏** → de-spread 在解不存在的问题。

→ **两版均回退**(`nr_srs_mmse.c/.h` 删函数+声明,`nr_ul_channel_estimation.c` 删调用+env+flag)。
**教训**:动手前先吃透 gNB LS 的 `fd_cdm` 累加(它就是端口分离),可避免整段弯路。

---

## 3. 一路被推翻的错误判断(非代码,记录防再犯)
1. "循环移位泄漏需要延迟窗" → **错**:FD-CDM 已分离。
2. "CDL-A 比 CDL-C 好 = 时延扩展限的端口泄漏" → **混淆**:那两发动态不同(decorr 0.07 vs 0.66)+ eval STO 窗太窄的假象。
3. "1×1 也坏了(−0.03dB)" → **假象**:eval 默认 STO 窗够不到 −33 偏移,静默低估(修复后 −21.7dB)。
4. "时间级 Kalman 高动态发散"(更早 06-07 上午) → 也是工具假象(replay 喂错输入),生产健康。

---

## 4. 关键实测结果(eval 自适应,口径干净)

| 配置 | coh | NMSE(STO+scalar) | 说明 |
|---|---|---|---|
| **1×1**(165202,同 CIR pair0) | 0.996 | **−21.66 dB** | 1 端口、满功率、满分辨率 |
| **2×2 per-port rx0tx0**(163538) | 0.836 | **−5.31 dB** | 2 端口 SRS |
| 2×2 四链路均值 | 0.836 | −4.66 dB | rx0tx0/rx0tx1/rx1tx0/rx1tx1 = −5.31/−3.12/−5.33/−4.51 |

**2 端口相对 1 端口 ~16dB 代价。**

### 真因定位 —— 离线延迟域诊断(2026-06-07 晚,`diag_mimo_delay.py`,无需重跑)
> ⚠️ **更正**:本日志早先写的"FD-CDM 阶梯插值混叠"根因经离线诊断**证伪**,以下为修正结论。

对已抓 2×2(`163538`)与 1×1(`165202`)capture,STO+scalar 对齐后把残差映射到延迟域分解:

**逐一证伪三个"可修 bug"假设:**
| 假设 | 检验 | 结果 |
|---|---|---|
| 周期-2 阶梯混叠 | 残差 Nyquist 带(\|n−1024\|≤8)占比 | **0.0%**(1×1 与 2×2 均 0)→ 无混叠,**证伪** |
| 跨端口泄漏 | 残差 vs 另一端口 GT 复相干 | **0.005~0.013** → FD-CDM 解扩干净,无泄漏,**证伪** |
| dump 定点标度 | mean\|H\| 1×1 vs 2×2 | 506 vs 524,**相同** → 非量化假象,**证伪** |

**延迟域开窗(保留 \|n\|≤L,理想去噪)苹果对苹果对比:**
| | 原生 L=0 | L=16 | L=32 | L=64(全信道支撑) |
|---|---|---|---|---|
| 1×1 | −21.7 | −10.8 | −13.2 | **−22.5** |
| 2×2 | −4.7 | −11.6 | −11.1 | **−9.5** |

- 1×1 在 L<64 反而更差 → 真信道延迟支撑宽达 ~64 taps,窄窗切真信道。
- **保留全支撑(L=64)后 2×2 仍比 1×1 差 ~13dB** → 残差是**落在信道支撑内的带内噪声**,延迟域去噪切不掉。

**判定为 SNR 亏空(非结构失真),接着逐层定位 SNR 在哪丢的:**

| 环节 | 代码 | 结论 |
|---|---|---|
| UE 每端口发射功率 | `srs_modulation_nr.c:283,391` `amp/sqrt(N_ap)` | 2 端口 −3dB,3GPP 标准守恒,**正确,仅 3dB** |
| 信道对 tx 求和 | `apply_channelmod.c:422-454` | 无 1/nb_tx 归一,噪声固定,**仅 3dB** |
| gNB LS 解扩归一 | `nr_ul_channel_estimation.c` | dump 幅度 1×1/2×2 相同(506/524),信号级一致 |
| **rfsim 上行信道重建噪声** | `simulator.c:2312` `new_channel_desc_scm(...,0)` | **★ 真凶** |

### ★ 真因 = 我自己的信道重建 bug(noise_power_dB 被重置)
- `-nd -6` 设的是 **per-channel** `ue0.noise_power_dB`(`launch_native.sh` L19/213-216,走 rxAddInput 内 per-channel 噪声路径,避开全局 dBFS 以免毁 PRACH)。噪声 = `pow(10, channelDesc->noise_power_dB/10)*256`(`apply_channelmod.c:408`)。
- **1×1**:UE_TX=1,`nbAnt==nb_tx`,**不触发重建** → 用原信道 `noise_power_dB=-6` → 正常 −21.7dB。
- **2×2**:UE_TX=2 触发我的重建 `simulator.c:2312`,`new_channel_desc_scm(...,0)` **把 noise_power_dB 硬编码成 0**,丢掉 −6 →
  `noise_per_sample` 比 = `10^((0-(-6))/10)=3.98` → 噪声**功率 +12dB**,叠加端口 −3dB ≈ **−15dB SNR 恶化**,与实测 13~16dB 吻合。
- 即:**"2 端口 16dB 代价"不是 SRS/MIMO 的物理代价,而是上行噪声底被重置成 0dB 的配置 bug。**

**修复(已改)** `simulator.c:2312`:重建改为继承 `old->noise_power_dB` 与 `old->path_loss_dB`(不再传 0);重建日志加打印继承值便于核对:
```c
new_channel_desc_scm(ptr->th.nbAnt, t->rx_num_channels, (SCM_t)old->modelid,
                     t->sample_rate, t->rx_freq, t->tx_bw, 30e-9, 0.0, CORR_LEVEL_LOW,
                     t->chan_forgetfact, t->chan_offset,
                     old->path_loss_dB, old->noise_power_dB);  // 原为 t->chan_pathloss, 0
```
> 同样的 `noise_power=0` 硬编码也存在于 telnet 换模型路径 `simulator.c:603-615`(本期未走该路径,留记备查)。

- **附带收获**:dump 的最终估计未做有效延迟域去噪;对 2 端口接全支撑(L≈64)延迟域去噪可白捡 ~4-5dB(治标)。

### ✅ 验证完成(2026-06-07 18:10,run `native_true2d_20260607_181051`,重编后)
| | coh | NMSE(STO+scalar) | per-port |
|---|---|---|---|
| 修复前 2×2 | 0.836 | −4.66 dB | −5.3/−3.1/−5.3/−4.5 |
| **修复后 2×2** | **0.984** | **−16.25 dB** | −17.3/−14.6/−16.8/−16.0 |
| 1×1 参照 | 0.996 | −21.66 dB | — |

- **改善 +11.6dB**,coh 0.84→0.984,与"丢失 ~12dB 噪声底"预测吻合。
- gnb.log 确认:`[rfsim MIMO] rebuilt channel 'rfsimu_channel_ue0' nb_tx 6->2 (peer nbAnt) nb_rx 2 (inherited noise_power_dB=-6.0 path_loss_dB=0.0)`。
- 跨端口泄漏仍 0.003~0.007(FD-CDM 解扩干净);残差 Nyquist 仍 0%(无混叠)。
- 修复后 2×2(−16.3dB)距 1×1(−21.7dB)余 ~5dB:~3dB 合理端口功率分摊 + 2×2 为动态独立信道(decorrelation 0.665,动态性强于该 1×1 发)。**这才是真 2×2 SRS MIMO 的实际表现;先前"16dB 代价"纯属噪声配置 bug。**
- ⚠️ 备查:重建日志 `nb_tx 6->2`(old->nb_tx 打印为 6),功能结果正确(终态 nb_tx=2、噪声 −6),但 old->nb_tx 来源值偏大,下次可顺带核一下原信道为何按 6 tx 建。

---

## 5. 当前 MIMO 状态
- ✅ 真 2×2 SRS MIMO 估计**跑通且稳定**(coh 0.84);使能改动全部有效。
- ✅ eval STO 自适应修复(消除静默低估)。
- ❌ de-spread 已回退(解了不存在的泄漏)。
- ✅ 2 端口 ~16dB 代价**根因定位 + 已修 + 已验证**:不是物理代价,是我重建上行信道时把 `noise_power_dB` 重置成 0(`simulator.c:2312`),只在 2 端口注入 ~12dB 噪声;改为继承原值后重编重跑,2×2 NMSE −4.7→−16.3dB、coh 0.84→0.984。
- ❓ 4×4 已能生成 CIR,**未真跑**。
- ❌ 下行/完整空间复用(多层吞吐)**未开始**。
- 📦 本会话改动**全部未提交**;submodule 另有更早未提交 PHY 改动(`nr_srs_2d_filter.c` 等)+ 重编 test 二进制待确认。

---

## 5b. 进一步提升空间 + 4×4(2026-06-07 晚)
**2×2 还能 +3dB(已离线验证)**:对修复后干净 2×2(`181051`)做延迟域开窗,L≈48(信道支撑):−16.2 → **−19.2dB**,逼近 1×1 −21.7dB。剩 ~2dB ≈ 2 端口物理功率分摊(−3dB)极限。

**接入点排查**:
- dump = `srs_estimated_channel_freq`(`nr_ul_channel_estimation.c:1539`)= **true2d 滤波后最终估计**,非原始 LS。
- freq-Wiener Stage1 **逐 `p_index` 都跑**(`1044/1084`),覆盖没问题,是**去噪质量**不足。
- 最可能根因(**待验证**):2 端口 LS 周期-2 阶梯(`898-926` 每 `fd_cdm` 共用一 LS 值)→ 污染 freq-Wiener 内部 PDP(IFFT)→ R 估计偏、开窗不紧。
- 旋钮:`SRS_MMSE_WINDOW`(默认 16 导频,2~64,`nr_srs_mmse.c:86`)。
- 改进选项:**A** env 旋钮 `SRS_MMSE_WINDOW=8` 试;**B** 加 env 门控的延迟域 PDP 去噪收尾步(offline 已证 +3dB,off by default)。

**4×4 ✅ 实跑通过**(run `native_true2d_20260607_183922`):`uecap_ports4.xml` + `cir/cir_cdl_c_s60_4x4.bin`(16 link 独立);noise 继承 −6.0;attach 成功,**16 条 link 全部干净估出**(无死端口/退化)。
- **coh 0.981,NMSE −15.31dB**,per-link −13.5~−17.3dB,高度一致。
- 跨端口泄漏 0.02~0.055(FD-CDM4 需信道在 4 导频/8 子载波内平坦的有限相干带宽效应,比 2×2 的 0.005 略高,仍可忽略);Nyquist 混叠 0%。

### 1×1 / 2×2 / 4×4 完整标度(全部实测验证)
| 配置 | coh | NMSE | vs 1×1 | 端口功率分摊(理论 10·log₁₀N) |
|---|---|---|---|---|
| 1×1 | 0.996 | −21.7 dB | — | 0 dB |
| 2×2 | 0.984 | −16.3 dB | +5.4 dB | −3 dB |
| 4×4 | 0.981 | −15.3 dB | +6.4 dB | −6 dB |

**标度完全符合教科书**(差距 ≈ 功率分摊 + 动态信道差异)。真 N×N 上行 SRS MIMO 信道估计端到端打通,1/2/4 端口全部稳定。
- 备查:4×4 重建日志 `nb_tx 207->4`(old->nb_tx 又是偏大随机值,同 2×2 的 6),功能正确,根因待查(原信道 nb_tx 字段为何是脏大值)。

## 5c. 与 1×1 差距的彻底分解(2026-06-07,plan 阶段离线核查)
用户追问"差距到底能不能去掉、改 EMA Wiener 行不行"。逐项离线验证(1×1/2×2/4×4 三发实测):

**差距 = 两部分,性质不同:**
1. **物理功率分摊(不可恢复@60km/h)**:UE 总功率固定、多端口平分(`amp/sqrt(N_ap)`),每端口 SNR 低 3dB(2×2)/6dB(4×4)。
2. **频率方向可恢复去噪(~2dB)**:真信道只占延迟范围 ~4%(前 ~40 抽头),其余 96% 是纯噪声;现有 freq-Wiener 没切空区噪声 → 延迟域去噪可拿回 +2.7~2.9dB(详见 §5b)。

**时间方向能不能补功率分摊?——决定性 oracle 上限测试:**
| 配置 | 当前(PKF后) | 天才W=0 | 天才W=1 | 天才W=2 |
|---|---|---|---|---|
| 2×2 | −16.24 | −16.41 | −16.80 | −17.15 |
| 4×4 | −15.31 | −15.47 | −15.75 | −16.01 |
- "天才"= 用 GT 算最优复权重 + 完美 STO 对齐融合相邻帧 = 任何时间级滤波(含完美 EMA/Kalman)的绝对上限。
- 结论:**oracle 用 ±2 帧也只多挤 +0.7dB(2×2)/+0.5dB(4×4)** → **当前 PKF 时间级已近最优(距 oracle <1dB),改 EMA/Kalman 在 60km/h 下补不回功率分摊**。
- 根因:相邻 SRS 帧 GT 复相干仅 ~0.6(60km/h 信道两次 SRS 间已变一大半)→ 无时间冗余可平均;naive 时间平均反而把 1×1 砸到 −9.5dB。

**结论**:60km/h 下,2×2 −16.3 / 4×4 −15.3 是**物理地板**(功率分摊),时间级无余量;唯一高速可做的是频率去噪 ~2dB。**地板随速度移动**:低速(帧间相干→0.9+)时时间级能补功率分摊,那时改 EMA Wiener 才有价值(未验证,可用 `data_out/cdl_c_30kmh` 重跑确认)。
- 工具:本次分析脚本临时性,核心可复用 `diag_mimo_delay.py` + eval。

## 6. 下一步(优先级)
1. **重编 nr-softmodem + 重跑 2×2 验证噪声修复**(线上,决定性):预期 2×2 per-port 从 −5.3dB → ~ −18dB(仅余 ~3dB 端口功率分摊),`diag_mimo_delay.py` 残差 MID 占比应大幅下降。
2. 验证通过后:多发 1×1/2×2 取均值钉死真实 2 端口代价(应 ~3dB)。
3. (可选)对 2 端口估计接入全支撑延迟域去噪 → 进一步白捡几 dB。
4. 提交整套(使能改动 + 噪声修复 + eval 修复 + `diag_mimo_delay.py`),并把本日志一并提交。
5. 之后再推进真正的 Phase 2 / 下行空间复用。

---

## 7. 文件 / 命令索引
| 改动 | 文件 |
|---|---|
| UL 信道重建 + **噪声继承修复** | `radio/rfsimulator/simulator.c:2312`(nbAnt 重建;继承 `old->noise_power_dB`/`path_loss_dB`) |
| **延迟域残差诊断** | `vRAN_Socket/.../diag_mimo_delay.py`(证伪混叠/泄漏/标度,定位 SNR) |
| CIR 多 pair + 平坦多 TX | `radio/rfsimulator/apply_channelmod.c`(`cir_replay_apply` / 平坦旁路) |
| 多端口参考 | `openair1/PHY/NR_UE_TRANSPORT/srs_modulation_nr.c/.h`(`max_ant_ports`)+ `openair1/SCHED_NR/phy_procedures_nr_gNB.c` |
| FD-CDM 解扩(端口分离,既有) | `openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c`(LS `fd_cdm` 累加 ~899-924,1308) |
| 多 TX CIR 生成 | `vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/cdl_to_cir.py`(`--nb-tx/--nb-rx`) |
| 一键多端口 | `vRAN_Socket/.../launch_native.sh`(`UE_TX`/`UECAP`) |
| eval 自适应 STO | `vRAN_Socket/.../eval_nmse_sto.py`(`_sto_coarse`/`_sto_es_adaptive`) |

复现:
```bash
# 生成 2x2 CIR
python3 cdl_to_cir.py data_out/cdl_c cir/cir_cdl_c_s60_2x2.bin -speed 60 --nb-tx 2 --nb-rx 2
# 采集 2x2 / 1x1(同 CIR,匹配对照)
sudo NB_RX=2 UE_TX=2 bash launch_native.sh -e true2d -cir cir/cir_cdl_c_s60_2x2.bin -nd -6 -seed 12345 -p 10 -d 120 --restart-5gc
sudo NB_RX=1 UE_TX=1 bash launch_native.sh -e true2d -cir cir/cir_cdl_c_s60_2x2.bin -nd -6 -seed 12345 -p 10 -d 120 --restart-5gc
# 评估(eval 默认自适应 STO)
python3 eval_nmse_sto.py --run-dir ../../logs/native_true2d_<ts>
```
