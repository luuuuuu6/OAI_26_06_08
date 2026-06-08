# PDP→R Two-Stage SRS MMSE 算法 — 完整开发回顾

**日期范围**:2026-05-09 至 2026-05-11
**作者**:LIULU
**状态**:**已停止开发,pivot 到 EWMA 时域 filter 方案**

---

## 0. TL;DR

实现了直接 PDP→R 二阶段 complex MMSE SRS channel estimator,作为 OAI legacy fixed FIR 的替代。

**结果**:
- ✓ 算法实现完整可工作(经 8000+ frames 实测)
- ✓ 修复了多个基础设施问题(zombie cleanup, channel seed, inter-point preflight 等)
- ✗ **复杂度比 Legacy 高 50-100×**(per-call 5-10 ms vs 0.1 ms)→ 在 OAI 实时路径上引起 SRS deadline miss
- ✗ **same-channel fair comparison 下,新算法 NMSE p50 比 Legacy 差 3-6 dB**(LS-aligned NMSE p50:Legacy −7,新算法 −1 ~ −4)
- ✗ **方向性错误**:做了 1D frequency MMSE,但教授真正要求的是 **2D filtering(time + frequency)**

**Pivot 决策**:停止 PDP→R 优化,转向 **跨 SRS slot 的 EWMA 时域 filter**——符合教授 4/29 / 5/7 指令的"复杂度 negligible + 性能 gap"原则。

---

## 1. 教授指令(原话)

### 4/29 Meeting(关于 channel estimation 路线)

> "타임 코릴레이션이 있고 주파수 코릴레이션이 있기 때문에... 시그널의 코릴레이션을 이용해서 노이즈를 최대한 서프레션 해줘야 되겠지. 그럼 그게 기본적으로 **2D 필터링**을 해주면 노이즈가 서프레션이 돼. 그리고 그 2D 필터링의 가장 기본은 mmse 방식이야."

→ **2D filtering(time + frequency)**,基础是 MMSE。

> "도플러 이동 속도에다가 프리퀀시 셀렉티비티 정도 파라미터화해가지고 그냥 필터 개수를 프리플라이즈 해가지고 적당한 걸 선택해서 쓰는 정도로만 해도 그게 성능이 그렇게 나쁘지 않아."

→ 不一定要 full MMSE,可以参数化 + 预制 filter bank,效果也够。

### 5/7 Meeting(具体实施 guidance)

> "본인이 하는 게 채널 에스티메이션을 잘하려는 게 목적이 아니잖아. 채널이 있으면 좀 더 개선하겠다는 포인트를 그냥 하나 줄 정도잖아."

→ Channel estimation **不是目的**,只是"如果有 channel 就稍微改进"的一个 point。

> "복잡도가 너무 복잡하면 oai 수신단이 너무 그것 때문에 느리게 돌 거 아니야."

→ 复杂度太高会让 OAI 跑慢。

> "LMS는 수렵은 빨리 하는데 성능이 안 나와... RNS 기반으로 해야 되는데 RNS는 엄청 복잡해. 그래서... **레티스 기반 RND** 그런 것들이 이미 기존들이 많이 있다고."

→ adaptive 推荐 **lattice-based RLS**(LMS 太弱,full RLS 太重)。

> "복잡도 때문에 늘어난 건 이게 얼마큼 무시할 수 있는데 성능은 얼마큼 좋아졌다."

→ 报告方式:复杂度增加可忽略 + 性能改进多少。

> "OAI가 LS 추정 기반이면 그건 너무 허접하니까 그냥 야 인간적으로 이 정도까지는 잘해야지 이 정도까지만 해."

→ OAI 现在 LS 太"허접"(简陋),"作为人至少做到这个水平"——**底线改进**即可。

---

## 2. 我们做了什么(完整时间线)

### 2.1 删除旧的简化 two-stage(τ_rms→corr_len)

之前有一版"简化 two-stage":LS → IFFT → 算 τ_rms 标量 → 套 `L = N/(2π·τ_rms)` 公式 → 用 `R(Δk) = exp(-|Δk|/L)` 做 1D MMSE。

**问题**:τ_rms 假设 PDP 是指数衰减(实际不是)+ L<12 时矩阵 ill-conditioning → method-D 反而 −7.58 dB。

**清理动作**(`Cleanup C`,~363 行删除):
- 删 `nr_srs_compute_tau_rms_samples`、`nr_srs_estimate_corr_len_from_ls` 等
- 删 `SRS_MMSE_LCORR_*` 系列宏、`lcorr_status_t` enum
- 删 `srs_mmse_robust_noise_floor_enabled` env helper
- 改 caller 用 default corr_len 临时占位

### 2.2 实现新算法:Direct PDP→R two-stage

**核心 pipeline**(`nr_srs_mmse_freq_filter` in `nr_srs_mmse.c`):

```
LS estimate
  ↓ Stage 1: LS → 时域 PDP
collect_pilots_dense → pilot_freq_dense
nr_srs_pdp_ifft → h_time(N=2048)
estimate_noise_floor → noise_floor (median of |h[N/2..N)|^2)
PDP_clean(n) = max(0, |h(n)|^2 - noise_floor)
  ↓ Stage 2: PDP → 复数频域 R 矩阵
R(Δk) = Σ_n PDP_clean(n) · e^{-j·2π·Δk·n/N}   ← Wiener-Khinchin
R_table[Δk] /= R(0)                              ← 归一化对角线为 1
  ↓ Stage 3: 每个 target SC 解 MMSE
build (W×W) complex Hermitian system Σ + noise_norm·I
solve_complex_system (4×4 复数 Gauss elim)
out = Σ_i w_i · H_LS(pilot_i)   (复数加权)
```

**新增函数**(`nr_srs_mmse.c` 现 779 行):
- `nr_srs_estimate_noise_floor` — 时域噪底估计
- `nr_srs_pdp_to_R_dft` — 单 Δk 处的 R 计算
- `solve_complex_system` — 4×4 complex Gauss elim

**删除函数**:
- `solve_real_system`(被复数版替代)
- `nr_srs_compute_tau_rms_samples`、`nr_srs_estimate_corr_len_from_ls` 等(简化 two-stage 残留)

**改动文件**:
- `nr_srs_mmse.c`: 633 → 779 行(净增 146)
- `nr_srs_mmse.h`: 38 → 41 行(去掉 `corr_len_sc` 参数)
- `nr_ul_channel_estimation.c` caller: 简化(去掉 `mmse_corr_len_sc` 局部变量)

### 2.3 noise_floor 修复(2026-05-11)

**问题**:首版算法在 SNR=10/15 dB 出现 NMSE collapse(p50 +15 ~ +26 dB)。诊断显示 `R(0)` 归一化在低 SNR 下过小,导致 R_table 不稳。

**修复**:把 `nr_srs_estimate_noise_floor` 从 "median of [N/2, N)" 改为 "25th percentile of [3N/4, N)"。
- 远 tail 减少 K_TC=4 alias 污染
- 25th percentile 比 median 更鲁棒于 chi-square 长尾

**效果**:
- pdpR SNR=10 NMSE p50:**+15.51 → −0.88 dB**(改善 16 dB)
- pdpR SNR=15 NMSE p50:**+26.16 → −4.25 dB**(改善 30 dB)
- collapse 状态完全消除

### 2.4 基础设施改进(全部可复用到任何后续算法)

#### 2.4.1 `eval_pdpR_nmse.py`(102 行)
- 离线 NMSE 评估器
- 输入:run dir(SRS bin + GT npz)
- 输出:raw NMSE / LS-aligned NMSE / per-frame p10/p50/p90
- 选项:`--sto-correct {none, global, per-frame}` 用于诊断
- 选项:`--dump-sto` 看 per-frame STO 数值

#### 2.4.2 `plot_nmse_vs_snr_v2.py`
- 多 SNR 对比图(p10/p50/p90 三线 + per-frame distribution panel)
- 标注 sample count
- 支持 STO 校正模式切换

#### 2.4.3 `preflight.sh` + `--inside-sweep` flag
- 启动 sweep 前自动检查 + 修复:
  - host 进程残留(launch_all/nr-softmodem/...)
  - sionna-proxy 容器内 zombie python3
  - `/tmp/oai_gpu_ipc/` shm 残留
  - `oai-ext-dn` 容器健康(iperf3 server)
  - 所有 5GC 容器 `Up` 状态
- `--inside-sweep` 模式跳过 host 进程检查(避免 sweep 自残)

#### 2.4.4 `run_q4_snr_sweep_v8.sh` 的 inter-point preflight
- 每个 SNR 点结束后自动调 `preflight.sh --inside-sweep`
- 解决了 zombie 累积导致后续 SNR 点 ABORTED 的连锁问题

#### 2.4.5 `run_sweep_with_iperf.sh`(实验性)
- wrapper 脚本,在 sweep 期间灌 iperf UDP UL traffic
- 目的:让高 SNR 时 OAI 调度器多发 SRS slots
- 实测效果:对 SRS rate 影响小,**未采用**

#### 2.4.6 STO 校正实现
- 集成 `digital_twin_stats.py` 里的 `estimate_sto` / `_apply_sto_correction`
- 加 `--sto-correct {global, per-frame}` 到 eval

### 2.5 Bug 修复

| Bug | 表现 | Fix |
|---|---|---|
| Python heredoc 把 C 字符串 `\n` 翻成真换行 | freq_filter 重写后 6 处 LOG 打印格式串损坏,build 失败 | Python 改用 `\\n` 或外部文件传递 |
| sweep 内 preflight 自残 | sweep 跑到 SNR=10 后 cleanup 时 pkill 把 sweep 自己父进程杀掉 | 加 `--inside-sweep` flag,跳过 host 进程检查 |
| sionna-proxy zombie 累积 | 每跑一次 sweep 留 3-10 个 zombie python3,日积月累影响新 sweep | preflight 检测到 zombie 自动 docker restart sionna-proxy |
| `oai-ext-dn` 自杀 | 早期 wrapper 用 `pkill -f "iperf3 -s"` 杀了容器 PID 1 | 改成 `ss -tnl` 检测 listener,不主动启 server(容器自带) |
| CHANNEL_SEED 未透传 | sweep 命令没设 → Sionna 每次随机 → 不同 sweep 不可比 | 文档化必须显式 `CHANNEL_SEED=42` |

---

## 3. 实测结果

### 3.1 复杂度对比

| 算法 | per-call 估计时间 | 倍数 |
|---|---|---|
| Legacy fixed FIR(filt8/16) | ~0.1 ms | 1× |
| **PDP→R complex MMSE** | **~5-10 ms** | **50-100×** |

证据:同 sweep,Legacy 12 分钟跑完 4 个 SNR 点;PDP→R 跑 40 分钟。SRS Dump captured rate Legacy ~3-6%,PDP→R ~0.85% — 因 deadline miss SRS 被 OAI 丢弃。

### 3.2 性能对比(seed=42, per-frame STO 校正)

| SNR | Legacy NMSE p50 | PDP-R NMSE p50 | Δ(差) |
|---|---|---|---|
| 10 | **−7.01 dB** | −0.88 dB | +6.13 |
| 15 | **−7.15 dB** | −4.25 dB | +2.90 |
| 20 | **−7.12 dB** | −2.46 dB | +4.66 |
| 25 | **−7.18 dB** | −3.01 dB | +4.17 |

**Legacy 全 SNR 稳定在 −7 dB(几乎 SNR-independent),PDP-R 在 −1 ~ −4 dB**。

### 3.3 Legacy NMSE 的 floor 解释

Legacy NMSE 全部 −7 dB 几乎不随 SNR 变,说明:
- **OAI 接收链路 fixed-point 量化噪声主导**,不是 channel SNR 主导
- 任何算法都难突破这个 −7 dB floor,**除非动 fixed-point 流水线**
- 这是为什么 PDP-R 即使理论更优,实测也敌不过 Legacy

### 3.4 PDP-R 的"分布问题"

per-frame NMSE 分布:
- Legacy:几乎单峰集中在 [-15, 0]
- PDP-R:**双峰**(一部分帧极好,一部分帧极差)
- p90 极差(SNR=10 = +31 dB,SNR=25 = +18 dB)说明算法对 channel realization 高度敏感

---

## 4. 失败原因 root cause

### 4.1 方向错了(最大问题)

教授要的是 **2D filtering(time + frequency)**。
我们做的是 **1D filtering(frequency only)**。

**后果**:即使把 frequency 方向做到极致(complex MMSE),也只能拿到 1D 改进的上限。但 OAI 当前 floor(fixed-point 噪声 + Legacy frequency interpolation)已经接近这个 1D 上限,所以"做更精的 1D"没空间。

时间方向的相关性完全没利用 → 错过了主要 noise suppression 机会。

### 4.2 复杂度爆炸

教授明确说复杂度要 **negligible**。我们的 per-call 5-10 ms 直接让 OAI 实时路径 deadline miss → SRS 调度变稀疏。

具体哪些步骤贵:
- PDP IFFT(2048 点)
- noise_floor median(qsort 512 floats)
- R(Δk) DFT × ~16 个 Δk(每个 N=2048 trig+MAC)
- 每 target SC:complex Gauss elim
- 复数加权应用

任何一步单独还能接受,**全套堆一起在每帧实时跑**就崩了。

### 4.3 没听教授说的 "lattice-based RLS"

教授 5/7 明确点名 lattice-based RLS。我们 0 调研、0 实现。

### 4.4 Same-channel comparison 太晚才做

前期所有对比都用不同 Sionna seed → channel realization 不同 → 数据 noise 让我误以为新算法在 SNR=20 大胜(28 dB)。

直到加 `CHANNEL_SEED=42` 才看到真相:**新算法在每个 SNR 都不如 Legacy**。

---

## 5. 不全是负面 — 有价值的产出

| 产出 | 价值 |
|---|---|
| `eval_pdpR_nmse.py` + `plot_nmse_vs_snr_v2.py` | 通用 SRS estimator NMSE 评估器,EWMA 之后照样能用 |
| `preflight.sh` + sweep inter-point 集成 | 解决了 OAI sweep 框架的 zombie 累积痛点,以后所有 sweep 都受益 |
| STO 诊断工具(`--sto-correct per-frame --dump-sto`) | 诊断 OAI ↔ Sionna 参考系不一致问题 |
| 教训:**先做 fair comparison(同 seed),再下结论** | 避免被 channel realization noise 误导 |
| 教训:**先看 OAI 现有实现,再加新东西** | 节省盲目 reinvent 的时间 |
| 备份文件 `.bak.20260510_pre_two_stage` 等 | 任何方向都能回退 |

---

## 6. 下一步:EWMA 时域 filter

**新方向**(2026-05-11 决定):
- 频域:**保留 Legacy filt8/16**(已经接近 OAI floor,够用)
- 时域:**加跨 SRS slot 的 EWMA**(`H_avg = α·H_new + (1-α)·H_old`)
- 这就是教授 4/29 要的 2D filtering 的最简版

**预期**:
- 复杂度增加 ~10%(per-SC 1 mul + 1 add)
- 性能改进 1-3 dB(EWMA 平均掉 per-frame noise)
- 满足教授"negligible 复杂度 + 可见 gap"

**Stretch goal**:
- α 改 adaptive(LMS / lattice-RLS)
- α 按 Doppler / channel coherence time 自动调

---

## 7. 文件清单(本次开发涉及)

### 修改的文件
```
DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_mmse.c   (633 → 790 行)
DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_mmse.h   (38 → 41 行)
DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c  (caller 修改)
DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/run_q4_snr_sweep_v8.sh  (加 inter-point preflight)
```

### 新建的文件
```
DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/preflight.sh
DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/eval_pdpR_nmse.py
DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/plot_nmse_vs_snr.py
DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/plot_nmse_vs_snr_v2.py
DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/run_sweep_with_iperf.sh
DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/ZOMBIE_LEAK_NOTE.md
```

### 文档
```
PDP_R_RETROSPECTIVE_20260511.md (本文件)
sto_problem.md (之前的 STO 调研)
```

---

## 7.1 备份文件清单(2026-05-11 EWMA 切换前完整快照)

**核心 OAI C 代码(root-owned,sudo cp)**:
```
DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/
  nr_srs_mmse.c.bak.20260511_pre_ewma                       27,851 bytes
  nr_srs_mmse.h.bak.20260511_pre_ewma                        1,500 bytes
  nr_ul_channel_estimation.c.bak.20260511_pre_ewma          48,270 bytes
```

**Sweep / 评估工具(用户 owned)**:
```
DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/
  run_q4_snr_sweep_v8.sh.bak.20260511_pre_ewma              21,500 bytes (inter-point preflight 集成版)
  preflight.sh.bak.20260511_pre_ewma                         5,488 bytes (含 --inside-sweep flag)
  eval_pdpR_nmse.py.bak.20260511_pre_ewma                    9,472 bytes (含 --sto-correct/--dump-sto)
  plot_nmse_vs_snr.py.bak.20260511_pre_ewma                  6,564 bytes (v1 单图版)
  plot_nmse_vs_snr_v2.py.bak.20260511_pre_ewma               8,384 bytes (v2 双图 + STO 模式)
  run_sweep_with_iperf.sh.bak.20260511_pre_ewma              4,107 bytes (实验性,未采用)
```

**Sweep 数据 + 图(`logs/PDP_R_DATA_BACKUP_20260511/`)**:
```
legacy_seed42_manifest.txt        (4 SNR 点 OK,12 分钟跑完)
pdpR_v2_seed42_manifest.txt       (4 SNR 点 OK,40 分钟跑完)
nmse_vs_snr_v2_sto_per-frame.png  (主对比图,显示 Legacy 全面胜出)
nmse_vs_snr.csv                   (8 行原始数据)
```

**更早的备份**(各代码变迁的历史快照):
```
DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/
  nr_srs_mmse.c.bak_20260510_pre_two_stage           (1035 行,清理前)
  nr_srs_mmse.c.bak.after_cleanupC_snapshot          (633 行,Cleanup C 后/PDP-R 前)
  nr_srs_mmse.c.bak.20260511_pre_ewma                (790 行,本次 EWMA 切换前) ← 当前
```

### 7.2 一键回退命令(EWMA 失败时用)

```bash
# 回退到 PDP-R 状态(本次备份)
sudo cp /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_mmse.c.bak.20260511_pre_ewma \
        /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_mmse.c
sudo cp /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_mmse.h.bak.20260511_pre_ewma \
        /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_mmse.h
sudo cp /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c.bak.20260511_pre_ewma \
        /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c
# 重新 build
cd /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/openairinterface5g_whan/cmake_targets/ran_build/build && sudo ninja nr-softmodem
```

---

## 8. 给后续工作的建议

1. **不要删 PDP-R 代码**——保留为 "alternative implementation",未来如果做 reference 对比可用
2. **EWMA 实现要保护退路**:用 env var `SRS_EWMA_ALPHA` 控制(α=0 → 退化为 frame-by-frame Legacy)
3. **测试用 fair comparison 模板**:`CHANNEL_SEED=42` 是必需,2 个算法都用同一 seed
4. **每次跑前 `preflight`**(top-level)
5. **复杂度报告**:加 `gettimeofday()` 测量 per-frame `nr_srs_mmse_freq_filter` 耗时,跟 Legacy 比

---

**完。**
