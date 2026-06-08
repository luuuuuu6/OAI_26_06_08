# 问题定位日志（2026-05-05）

## 1) 本轮定位目标

围绕以下核心问题做最小化定位：

- 为什么 `STO` 补偿后 `NMSE/Estimation SNR` 会大幅改善？
- 当前误差到底主要来自 `proxy/simulator 相位链`，还是 `OAI SRS 估计链`？
- `legacy` 与 `mmse1d` 的对比是否公平（是否混入 slot/seed/路径差异）？

## 2) 本轮使用的数据与方法

本轮直接复用了现有日志，不重采大数据，重点跑同批数据的离线统一分析：

- 主分析脚本：`DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/q4_convergence_sweep.py`
- 对照脚本：`run_method_e_sweep.py`
- 重点复核目录：
  - `DevChannelProxyJIN/logs/mmse_2x2_seed8102_*`
  - `DevChannelProxyJIN/logs/mmse_main_seed8103_*`
- 输出目录：
  - `DevChannelProxyJIN/data_out/locate_sto_mmse_seed8102`
  - `DevChannelProxyJIN/data_out/locate_sto_mmse_seed8103`
  - `DevChannelProxyJIN/data_out/locate_sto_mmse_seed8103_skip40`

## 3) 关键发现（结论先行）

### A. STO/线性相位斜坡仍是主导误差项

同一 run 内仅切换后处理 `denoise=none -> denoise=sto`，`NMSE` 有 10~20 dB 级改善，说明 STO 不是小修正，而是主导误差项。

示例（seed8102）：

- `legacy_delay_off_srs_trigger`
  - `N30: +12.31 dB (none) -> -6.31 dB (sto)`，改善约 `18.6 dB`
- `mmse1d_delay_off_srs_trigger`
  - `N30: +3.79 dB (none) -> -7.78 dB (sto)`，改善约 `11.6 dB`

### B. delay_on 分支在当前链路下明显有害

seed8102 下（统一 `srs_trigger`）：

- `legacy + sto`: `delay_off N30=-6.31 dB`，`delay_on N30=+1.38 dB`
- `mmse1d + sto`: `delay_off N30=-7.78 dB`，`delay_on N30=+22.47 dB`

结论：当前 `delay_on` 不是有效补偿，反而会破坏结果。

### C. 现有 A/B 对比被“slot 不一致”严重污染

本轮从各 run 的 `slot_ids` 抽取到：

- seed8102
  - `legacy_delay_off` -> slot `18`
  - `legacy_delay_on` -> slot `9`
  - `mmse1d_delay_off` -> slot `8`
  - `mmse1d_delay_on` -> slot `8`
- seed8103
  - `legacy_delay_off` -> slot `8`
  - `mmse1d_delay_off` -> slot `18`

这说明很多“看起来是 estimator 差异”的结果，实际夹杂了 slot 差异，导致结论不稳。

### D. 非平稳/坏帧问题仍在，且会在大 N 反噬均值

部分 run 在 `N15/N30` 尚可，但 `N75/N100` 明显恶化；`run_method_e_sweep.py` 也显示部分 run outlier 比例很高（如 `46/100`），说明大窗口平均会被坏帧拉坏，不能只看单点 N。

## 4) 本轮收束判断

- `STO` 补偿是必要项，且是主要增益来源。
- 当前 `delay_on` 路径应继续保持关闭（`delay_off`）。
- 在未固定同 slot、同 seed、同链路前，`legacy vs mmse1d` 结论不具备公平性。
- 当前阶段应先做“公平 A/B 最小实验”，再谈 `mmse1d` 是否稳定优于 `legacy`。

## 5) 下一步执行方案（公平 A/B 最小实验）

目标：仅改变 estimator，其他全部固定。

- 固定同一 `CHANNEL_SEED`
- 固定 `SRS_DELAY_COMP=0`（即 `delay_off`）
- 固定单一 `ONLY_SNR`（默认 10 dB）
- 固定同一 attach 策略（默认 `ATTACH_STABLE_TRIGGER=srs`）
- 仅切换 `SRS_ESTIMATOR=legacy/mmse1d`
- 自动检查两组 run 的 `slot_ids` 是否一致；不一致则自动重跑 `mmse1d`，直到匹配或达到重试上限

> 说明：这一步是为了剔除 slot 混杂变量，建立可解释的最小对照。

## 6) 已落地脚本

已新增一键脚本（见同目录）：

- `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/run_fair_ab_minimal.sh`

该脚本会：

- 自动跑 `legacy` 与 `mmse1d`（固定 `delay_off`）
- 自动比对 slot 签名并重试 `mmse1d`
- 自动调用 `q4_convergence_sweep.py` 生成 `none/sto` 两套报告
- 自动输出简明汇总：`N15/N30/N100` 的 `NMSE/PDP/RSRP`

推荐运行方式：

```bash
cd /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy
bash run_fair_ab_minimal.sh
```

可调参数示例：

```bash
SEED=8201 SNR_POINT=10 MAX_FRAMES=100 MAX_RETRY=4 bash run_fair_ab_minimal.sh
```

## 7) STO 现象复核（新增）

围绕“为什么 STO 能显著改善、以及能否直接作为主修复”做了二次复核，结论如下：

### 7.1 STO 改善是“真实现象”，但不是每个 run 都同向

在 `fair_ab_seed8301_20260505_215309`（同 seed、同 slot=8、公平 A/B）中：

```text
[denoise=none]
legacy : N30=+17.47 dB, RSRP30=17.42 dB, PDP30=0.7275
mmse1d : N30=+11.76 dB, RSRP30=11.92 dB, PDP30=0.9907

[denoise=sto]
legacy : N30=+12.94 dB, RSRP30=13.19 dB, PDP30=0.7525
mmse1d : N30=-6.79 dB,  RSRP30=0.76 dB,  PDP30=0.9959
```

这里 `mmse1d` 的 STO 增益非常大，说明线性相位斜坡确实可能是主导误差源之一。

但在 `seed8201` 的三组（legacy / trim0 / trim0.10）上，同样口径下 STO 对 NMSE 不总是提升：

```text
legacy         : N30 +27.26 (none) -> +26.27 (sto), 小幅改善
mmse1d trim0   : N30 +10.96 (none) -> +15.42 (sto), 反而变差（但PDP提升明显）
mmse1d trim010 : N27 +20.57 (none) -> +22.43 (sto), 变差
```

结论：STO 是关键变量，但“仅打开 denoise=sto 就稳定变好”并不成立，仍需结合 run 条件与估计链路看。

### 7.2 为什么这不矛盾

`q4_convergence_sweep.py` 中 `denoise=sto` 的 slope 来自 `estimate_sto(H_srs, H_gt, ...)`，属于离线评估中的 GT 辅助补偿。它能很好揭示“相位斜坡是否重要”，但不等价于线上可直接部署的盲补偿效果。

因此：

- 它是很好的“诊断显微镜”；
- 但不是“线上默认算法已解决”的证明。

## 8) 本轮收束后的执行建议（更新：先定位 STO，再谈参数）

基于 `fair_ab_seed8301`（同 seed / 同 slot=8 / 同 SNR / 同 delay_off）的结果，当前优先级更新为：

```text
1) 固定 run_fair_ab_minimal.sh 作为标准实验入口（公平 A/B 基线）。
2) 脚本默认输出 STO diagnostics：
   - sto_samples
   - sto_slope
   - per-frame STO range
   - median_gap_slots
3) 先解释“legacy + sto 仍差、mmse1d + sto 明显变好”的机制差异。
4) 在完成 1~3 之前，暂缓 sigma2 cap/floor 调参。
```

本轮公平 A/B 关键结论（N=30）：

```text
denoise=none:
legacy  +17.47 dB
mmse1d  +11.76 dB   (mmse1d 好约 5.7 dB，但两者仍被 STO 污染)

denoise=sto:
legacy  +12.94 dB
mmse1d  -6.79 dB    (mmse1d 好约 19.7 dB，进入可用区间)
```

因此当前主结论不再是“继续调 MMSE 参数”，而是：

- `mmse1d` 在公平条件下已证明方向正确；
- 未解主问题是 `STO` 来源与 raw 评估口径；
- 应优先完成 STO 诊断闭环，再决定是否进入下一轮 `mmse1d` 参数细调。

## 9) 实验脚本能力补充（坏帧定位）

`run_fair_ab_minimal.sh` 已补充坏帧诊断输出，用于解释 `N30 -> N100` 退化：

```text
[bad_frame_diagnostics]
legacy : top_bad_frames=...
mmse1d : top_bad_frames=...
```

每个坏帧条目包含：

- `frame` / `slot` / `abs`（帧与绝对时序定位）
- `gap`（SRS↔GT 对齐残差，单位 slots）
- `nmse`（按 denoise=sto 同口径计算的每帧 NMSE）
- `sto`（每帧局部 STO samples 估计）

可通过环境变量调整输出数量：

```bash
BAD_FRAME_TOPK=8 bash run_fair_ab_minimal.sh
```

## 10) 公平性闸门补充（帧数一致性）

在最新一次 `seed8301` 复现中出现：

- `legacy_slot=frames=102`
- `mmse1d_slot=frames=85`

虽然 `slot signature` 一致（都为 slot=8），但帧数差异较大，已会影响 N 轴公平比较。

为此 `run_fair_ab_minimal.sh` 增加了第二道闸门：

- 第一闸门：`slot signature` 必须匹配
- 第二闸门：`mmse1d_frames >= ceil(legacy_frames * MIN_FRAME_RATIO)`

默认：

```bash
MIN_FRAME_RATIO=0.90
```

若仅 slot 匹配但帧数不足，脚本会自动拒绝该次 `mmse1d` 并继续重试。

## 11) 2026-05-05 sigma2 A/B/C/D 完整结果（seed8201）

> 注：本节为第 8 节之后的实测更新；第 8 节中“暂缓 sigma2 cap/floor 调参”的策略已被本节实测结果覆盖。

### 11.1 运行信息

- 扫描脚本：`run_mmse_sigma2_abcd.sh`
- run id：`mmse_sigma2_abcd_seed8201_20260505_224610`
- 统一条件：
  - `SRS_ESTIMATOR=mmse1d`
  - `SRS_DELAY_COMP=0`
  - `ONLY_SNR=10`
  - `CHANNEL_SEED=8201`
  - `ATTACH_STABLE_TRIGGER=srs`
  - `align=slot`
  - 主分析口径：`denoise=sto`, `fixed_N=30`

### 11.2 A/B/C/D 配置定义

```text
A: trim=0, cap=2.0, floor=1e-4, clip=6.0
B: trim=0, cap=1.0, floor=1e-4, clip=6.0
C: trim=0, cap=0.5, floor=1e-4, clip=6.0
D: trim=0, cap=1.0, floor=1e-3, clip=6.0
```

### 11.3 公平性与重试记录（slot signature）

本轮开启了 `STRICT_SLOT_MATCH=1`，锚点 slot 为 `8`。

各尝试的 slot/frames：

```text
A_try1: frames=82,  slots=8   (接受)
B_try1: frames=100, slots=19  (拒绝)
B_try2: frames=69,  slots=8   (接受)
C_try1: frames=82,  slots=8   (接受)
D_try1: frames=32,  slots=9   (拒绝)
D_try2: frames=80,  slots=8   (接受)
```

### 11.4 主结果（denoise=sto, fixed N=30）

来自 `abcd_summary.csv/txt`：

```text
Case A: paired=82, N30 NMSE=-4.06 dB, RSRPerr30=1.46 dB, PDP30=0.9891
Case B: paired=69, N30 NMSE=+23.81 dB, RSRPerr30=24.11 dB, PDP30=0.1071
Case C: paired=82, N30 NMSE=+26.09 dB, RSRPerr30=26.49 dB, PDP30=0.1556
Case D: paired=80, N30 NMSE=+28.88 dB, RSRPerr30=29.22 dB, PDP30=0.0501
```

N=15/20/30 细化：

```text
A: N15=-10.93, N20=-10.72, N30=-4.06
B: N15=-9.47,  N20=-10.09, N30=+23.81
C: N15=-8.55,  N20=-7.34,  N30=+26.09
D: N15=+29.00, N20=+33.19, N30=+28.88
```

### 11.5 补充对照（同批日志 denoise=none）

为区分“参数本体变化”与“sto 后处理影响”，对同批 run 追加了 `denoise=none` 离线分析：

```text
A: N30 +12.60 dB
B: N30 +23.08 dB
C: N30 +34.16 dB
D: N30 +30.56 dB
```

`sto - none` 在 N30 的变化：

```text
A: -16.66 dB   (显著改善)
B: +0.73 dB    (基本无改善，略变差)
C: -8.06 dB    (有改善，但仍很差)
D: -1.68 dB    (轻微改善，但仍很差)
```

### 11.6 收束结论

```text
1) 本轮最优且唯一可用候选是 A（cap=2.0, floor=1e-4）。
2) 将 cap 收紧到 1.0/0.5（B/C）在本 seed 下会在 N30 明显崩溃。
3) 提高 floor 到 1e-3（D）进一步恶化，说明该方向不适合当前链路。
4) 因此“更小 cap 更稳”这一假设在本轮被否定；当前应保持较宽 cap（至少不低于 2.0）。
```

补充：

- A 虽在 `N30` 已显著优于其他组，但在更大 N（如 N50+）仍会出现退化，说明动态非平稳/坏帧问题仍在；
- sigma2 稳健化已证明有效，但尚未完全解决长窗口失稳。
