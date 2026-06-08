# 当前研究进展报告：OAI 数字孪生与 SRS 信道估计增强

日期：2026-05-21  
范围：截至当前代码与日志状态，对最近推进的研究内容、代码完成度、当前卡点和下一步工作进行整理。  
相关文档：`未来最终愿景.md`、`Change_log.md`、`log_26_05_17_adaptive_alpha_cdl.md`、`log_26_05_17_19_adaptive_alpha_oai_ab.md`、`DEBUG_LOG_20260521_HW_SLOT_OFFSET_ROOT_CAUSE.md`

---

## 1. 一句话结论

当前项目已经从“搭建 OAI + Channel Proxy 数据引擎”推进到“在 OAI gNB 侧真实落地 SRS 信道估计增强算法，并建立可重复 A/B 验证链路”的阶段。

最新工作重点不是 AI 训练本身，而是在把 AI 训练前最关键的底层数据质量打牢：让 OAI 输出的 SRS 信道估计更可信、让 GT 和 SRS 能正确对齐、让 EWMA / adaptive alpha 这类 2D 时域滤波算法可以在真实 OAI C 路径中运行和公平比较。

目前阶段判断：

| 模块 | 当前状态 | 说明 |
|------|----------|------|
| 数据引擎 | 基本完成，持续修补 | OAI gNB/UE + Channel Proxy + Sionna/GT dump 已可端到端运行 |
| SRS 信道估计增强 | 正在攻坚 | EWMA / adaptive alpha 已接入 OAI C 侧，但公平 A/B 仍需重跑 |
| 评估体系 | 已形成主线口径 | 在线 C 输出应使用 `eval_pdpR_nmse.py`，固定 STO + per-antenna LS 对齐 |
| dclcom61 新服务器迁移 | 基本完成 | CUDA/TF/Sionna/OAI 环境已跑通，UE attach 不稳定问题已定位并修复 |
| AI 特征/模型训练 | 尚未正式展开 | 仍在等待阶段二输出稳定可信的 SRS 信道估计 |

---

## 2. 整体研究目标

最终目标是构建一个 OAI 数字孪生系统：

```text
Channel Proxy + OAI 环回
        ↓
带真值标签的 SRS/GT 信道数据
        ↓
OAI gNB 侧信道估计增强
        ↓
PDP / 空间协方差 / Doppler / SNR 等状态特征
        ↓
AI 模型训练
        ↓
部署到 RAN Twin / Sensing Block
```

也就是说，项目不是单独做一个滤波器，也不是单独做一个 AI 模型，而是要打通完整链路：

1. 自己生成可控信道数据。
2. 在 OAI 真实 PHY 流程里提取 SRS 信道估计。
3. 用 GT 验证估计质量。
4. 将更干净的信道估计转成 AI 可用的状态样本。
5. 最终服务定位、场景识别、信道预测或 RAN Twin 感知任务。

当前工作处在第 2 步和第 3 步之间：信道估计增强 + 评估闭环。

---

## 3. 最近最新推进的研究内容

### 3.1 Adaptive alpha 算法从探索走到 C 侧落地

最近一条主线是 adaptive alpha，也就是让 SRS 时域平滑的 EMA 系数不再固定，而是根据当前信道状态自动变化。

原始动机：

- 慢信道、低噪声时，可以多平滑，降低噪声。
- 快信道、高 Doppler 时，不能过度平滑，否则会跟不上信道变化。
- 不同 SNR 下最优 alpha 也不同，单一固定 alpha 很难覆盖所有情况。

探索路径大致如下：

| 路径 | 结果 | 原因 |
|------|------|------|
| `rho_obs` 自相关特征 | 淘汰 | 对 SNR 和 Doppler 混合敏感，且方向在某些场景下错误 |
| innovation / 残差特征 | 淘汰 | 来自滤波器输出，会形成闭环正反馈，容易发散 |
| 1D `rot_rate` | 保留为中间方案 | 能感知 Doppler，但不能感知 SNR |
| 2D `rot_rate + even-odd SNR` | 当前主方案 | 一个特征感知速度，一个特征感知噪声，且都比较适合 C 化 |

最终形成的算法形式是：

```text
每个 SRS 帧：
  1. 根据当前观测 H_obs 和平滑状态 H_smooth 的相位旋转提取 rot_rate
  2. 用 even-odd 子载波差分估计 SNR
  3. 对 rot_rate 和 SNR 做 EMA 平滑
  4. alpha = clamp(a1 * rot_rate + a2 * SNR_dB + a3)
  5. 用该 alpha 更新 H_smooth
```

Python 侧对应 `srs_2d_mmse.py` 中的 `AdaptiveAlphaEMA`。  
OAI C 侧对应 `nr_srs_2d_filter.c` 中的 adaptive 分支。

需要注意：这个 adaptive alpha 并不是已经证明对所有信道都有效。它在 CDL 标准模型范围内验证较好，但在 P1B ray-tracing 信道上泛化失败。这说明 `(rot_rate, SNR)` 两个特征不足以描述所有真实几何信道的最优 alpha 行为。

因此当前更准确的定位是：

> adaptive alpha 是一个已经接入 OAI 的可运行研究原型，适合作为 EWMA 的对照和 CDL 范围内的增强方案；但它还不是最终通用信道估计算法。

---

### 3.2 OAI C 侧已经支持 EWMA / adaptive 切换

代码层面已经不是只在 Python 里跑仿真，而是接入了 OAI gNB 的 SRS 估计流程。

核心文件：

| 文件 | 作用 |
|------|------|
| `DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_2d_filter.c` | EWMA / adaptive alpha 的 C 实现 |
| `DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_2d_filter.h` | 方法枚举、接口和环境变量说明 |
| `DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c` | SRS 估计主流程，调用 2D filter |

当前 C 侧支持：

```bash
SRS_ESTIMATOR=2d-mmse
SRS_2D_METHOD=ewma
```

或：

```bash
SRS_ESTIMATOR=2d-mmse
SRS_2D_METHOD=adaptive
```

C 侧 adaptive 支持通过环境变量覆盖参数：

| 环境变量 | 说明 |
|----------|------|
| `SRS_2D_ADAPT_A1` | `rot_rate` 系数 |
| `SRS_2D_ADAPT_A2` | SNR 系数 |
| `SRS_2D_ADAPT_A3` | 截距 |
| `SRS_2D_ADAPT_CMODEL` | even-odd SNR 的信道频域残差校正 |
| `SRS_2D_ADAPT_ALPHA_INIT` | 初始 alpha |
| `SRS_2D_ADAPT_ALPHA_MIN` | alpha 下限 |
| `SRS_2D_ADAPT_ALPHA_MAX` | alpha 上限 |
| `SRS_2D_ADAPT_RR_EMA` | rot_rate 平滑系数 |
| `SRS_2D_ADAPT_SNR_EMA` | SNR 平滑系数 |

这意味着现在可以在真实 OAI 运行中做：

```bash
METHODS="ewma adaptive" \
SRS_ESTIMATOR=2d-mmse \
bash run_q4_snr_ab_compare_v9.sh
```

然后分别采集 EWMA 和 adaptive 的在线 SRS 输出。

---

### 3.3 A/B 自动化实验框架已经搭好

最近推进的另一个重点是把实验从“手动跑一个点”变成“可以自动化 sweep 和 A/B 对比”。

当前主要脚本：

| 脚本 | 作用 |
|------|------|
| `run_q4_snr_ab_compare_v8.sh` / `run_q4_snr_ab_compare_v9.sh` | 顶层 A/B 控制器，按 method 分别跑 EWMA 和 adaptive |
| `run_q4_snr_sweep_v8.sh` / `run_q4_snr_sweep_v9.sh` | 单 method 的 SNR sweep |
| `launch_all_v8.sh` / `launch_all_v9.sh` | 启动 gNB、UE、Proxy 和监控逻辑 |
| `run_q4_postprocess_v8.sh` | 独立后处理脚本 |
| `eval_pdpR_nmse.py` | 在线 C 侧输出的直接 NMSE 评估工具 |

这套框架最近修了一个重要问题：过去只看 SRS bin 文件是否存在，导致 7 帧 partial flush 也会被误判为 OK。现在新增了 `MIN_SRS_FRAMES`，会读取 SRS bin 文件头里的帧数，要求实际 SRS 帧数达标。

现在完成条件更接近：

```text
GT 文件数达标
AND SRS bin 数达标
AND SRS 实际帧数达标
```

这个修复很重要，因为没有足够 SRS 帧数，EWMA 和 adaptive 的 NMSE 对比没有意义。

---

### 3.4 dclcom61 迁移和 UE attach 随机失败问题已定位

从 dclcom57 迁移到 dclcom61 后，遇到了一个很大的系统问题：UE attach 成功率随机下降，很多 sweep 卡在 `PARTIAL` 或 `FAIL-ATTACH`。

最初观察到的现象：

- UE 日志大量 `Error decoding PBCH`
- gNB 侧大量 phantom RA
- ULSCH BLER 接近 97-99%
- 无法进入稳定 SRS 调度阶段
- `hw_slot_offset` 有时是 7，有时是 8

一开始怀疑 `hw_slot_offset=8` 是主因，因为 TDD 配置下 slot 8 是 UL slot，看起来像 UE 初始同步错到 UL slot。

最新结论更精确：

> 真正核心根因是运行态 STO ±1 样本抖动，也就是 OAI UE 的 `nr_adjust_synch_ue` timing correction 在 GPU-IPC/rfsim 模式下引入了不该有的时序微调。`hw_slot_offset=8` 是加重因素，但不是最终致命点。

最终有效修复：

```bash
--ue-timing-correction-disable
```

该参数已经加入：

- `launch_all_v8.sh`
- `launch_all_v9.sh`

并且 socket 和 GPU-IPC 两条 UE 启动路径都加了。

修复后，即使首次 `hw_slot_offset=8`，UE 也能通过 resync 机制恢复并完成 attach。这说明当前不需要修改 OAI 源码去强行改 `rx_offset` 或拒绝某些 `hw_slot_offset`。

这次排查也形成了一个重要教训：

> 不要轻易在 PSS 初始同步路径上手改 `rx_offset`。它是 PSS 相关器找到的真实峰值位置，强行修正会破坏 PBCH 解码窗口。

---

### 3.5 在线评估口径已修正

之前 adaptive 在线输出看起来 NMSE 很差，甚至出现正 dB，看似算法失败。但后来发现主要是评估口径错了。

已经确认的问题有三层：

| 问题 | 影响 |
|------|------|
| 固定 STO 偏移 | OAI FFT 窗口和 Proxy GT 之间存在约 13.7 samples 的系统偏移 |
| per-antenna 相位不一致 | 每个 `(rx, tx)` 链路有独立相位参考，不能只用全局 LS 对齐 |
| slot 配对容差过宽 | `tol>0` 会引入非精确帧配对，NMSE 虚高 |

因此在线 C 侧输出推荐评估方式是：

```bash
python3 eval_pdpR_nmse.py \
  --run-dir <snr_XdB> \
  --sto-fixed-samples 13.7 \
  --tol 0
```

并重点看：

```text
NMSE LS-per-antenna
```

而不是只看 raw NMSE 或 global LS NMSE。

当前 `eval_pdpR_nmse.py` 已经加入：

- `--sto-fixed-samples`
- `NMSE LS-per-antenna`
- per-(rx,tx) breakdown

这使得在线 OAI C 输出终于有了比较合理的评估口径。

---

## 4. 代码目前做到什么程度

### 4.1 已完成：数据引擎和端到端采集能力

已经具备：

- OAI gNB / UE 通过 rfsim 或 GPU-IPC 跑通。
- Channel Proxy v8/v9 负责注入信道。
- Proxy 可以保存 Sionna GT。
- OAI gNB 可以 dump SRS 估计结果。
- 支持 SNR sweep。
- 支持 CDL 标准模型和 P1B ray-tracing 数据。
- 支持 2x2 MIMO 常用实验，架构上也考虑了 4x4。

这部分已经能支撑“有 GT 的 OAI SRS 信道估计验证”。

### 4.2 已完成：OAI SRS 2D temporal filter 接入

当前 `NR_SRS_EST_MMSE2D` 路径已经接入：

```text
Legacy filt8/filt16 频域插值
        ↓
nr_srs_2d_filter_update()
        ↓
EWMA 或 adaptive alpha 时域平滑
        ↓
dump / 后续使用
```

也就是说，当前所谓 `2d-mmse` 在代码里的真实含义更准确地说是：

> Stage 1 用 OAI 原有频域 FIR 插值，Stage 2 做跨 SRS slot 的时域滤波。

它还不是完整的二维 Wiener/MMSE 矩阵滤波器。

### 4.3 已完成：adaptive alpha 在线实现

C 侧 adaptive 已实现：

- 每个 `(rx, tx)` 维护独立状态。
- 计算当前帧与历史平滑信道的内积。
- 从内积相位变化得到 `rot_rate`。
- 从 even-odd 子载波差分估计 SNR。
- 平滑特征。
- 映射得到 alpha。
- 用 alpha 更新平滑信道。
- 异常时 fallback 到 EWMA。

当前代码已有 debug 输出，可以在 `SRS_2D_DEBUG=1` 时观察：

- alpha
- rot_rate smooth
- SNR smooth
- frame count
- fallback count

### 4.4 已完成：A/B 采集和门限控制

当前 A/B 框架支持：

- 同一套参数下跑 `ewma` 和 `adaptive`。
- 每个 method 独立输出 sweep 目录。
- manifest 记录每个 SNR 点的状态。
- 通过 `MIN_SRS_FRAMES` 防止 partial flush 被误判成功。
- v9 增加 attach 诊断状态机。

### 4.5 已完成：UE attach 修复

`launch_all_v8.sh` 和 `launch_all_v9.sh` 已添加：

```bash
--ue-timing-correction-disable
```

这解决了 dclcom61 上 GPU-IPC/rfsim 模式下 STO 抖动引发的 attach 不稳定问题。

### 4.6 已完成：在线评估工具增强

`eval_pdpR_nmse.py` 已经能：

- 加载 SRS bin 和 Sionna GT。
- 按 abs slot 配对。
- 应用固定 STO 补偿。
- 输出 raw NMSE、global LS NMSE、per-antenna LS NMSE。
- 分解每个 `(rx, tx)` 的 NMSE。

这让后续 EWMA vs adaptive 的比较更可信。

---

## 5. 当前还没有完全完成的部分

### 5.1 修复后公平 A/B 还需要重跑

05-19 的首轮 A/B 数据不能直接作为结论，因为：

- EWMA 和 adaptive 的 SRS 帧数不对等。
- 部分结果来自 partial flush。
- 旧评估口径会对在线 C 输出二次滤波。
- dclcom61 迁移后又遇到 attach 不稳定。

现在底层问题已修复，但需要重新跑一轮严格 A/B：

```bash
METHODS="ewma adaptive"
SRS_ESTIMATOR=2d-mmse
MIN_SRS_FRAMES=100
```

再用统一口径评估：

```bash
--sto-fixed-samples 13.7 --tol 0
```

只有这轮跑完，才能说 adaptive 在真实 OAI 在线链路里到底比 EWMA 好多少。

### 5.2 adaptive alpha 的适用范围还有限

CDL 标准模型内，`rot_rate + even-odd SNR` 的 2D 映射是有效的。  
但 P1B ray-tracing 数据上，即使用 perfect SNR，很多 RX 位置仍然失败。

这说明 P1B 这类几何信道需要更多物理特征，例如：

- delay spread
- Doppler spectrum width
- AoA/AoD spread
- 更长 lag 的时间相关特征
- 频域相关结构

所以当前 adaptive alpha 不能写成“最终解决方案”。更合理的定位是：

> 它验证了闭环自适应 SRS 时域滤波在 OAI 中可实现，也提供了一个可比较的 online adaptive baseline。

### 5.3 真正 2D MMSE 仍未完成

当前 `2d-mmse` 名字下的实现主要是：

- 频域：沿用 OAI legacy filt8/filt16。
- 时域：EWMA 或 adaptive EMA。

真正意义上的 2D MMSE 应该包括：

- 频域 Wiener/MMSE 插值，替代固定 FIR。
- 时域 Wiener/MMSE 平滑，基于 Doppler 相关。
- 或者联合时频滤波。
- 参数可由 delay spread、Doppler、SNR 估计驱动。

这些还没有最终完成。

### 5.4 SRS STO / delay compensation 仍是长期核心问题

`Change_log.md` 中已经分析过，SRS 相比 DMRS 缺少关键的 delay compensation：

- DMRS 有 `nr_est_delay()`
- DMRS 有 `delay_table` 相位补偿
- SRS 原始路径没有完整等价机制

此前尝试过：

- 整数 delay 补偿：有效，但只能解决一部分。
- DFT 降噪：因 OAI int16 DFT 溢出失败。
- 互相关延迟估计：保留为后续验证方向。

这条线可能比 adaptive alpha 更接近“教授说的 2D filtering / STO 问题”的核心。

---

## 6. 当前最重要的实验结论

### 6.1 数据引擎层面

OAI + Proxy + GT dump 这一套已经能支撑研究，但对时序非常敏感。尤其是：

- UE attach
- SRS 调度
- GT/SRS slot 配对
- STO 补偿
- partial flush

这些不是小问题，它们直接决定 NMSE 结果可信不可信。

### 6.2 adaptive alpha 层面

CDL 范围内：

- `rot_rate` 能有效感知 Doppler。
- even-odd pair SNR 能提供开环 SNR 特征。
- 2D 映射比 1D `rot_rate` 泛化更好。
- Python 原型和 C 侧实现都已经完成。

P1B 范围内：

- 两维特征不够。
- 即使 perfect SNR 也不能救。
- 问题不是 SNR 估计脏，而是信道物理维度不足。

### 6.3 OAI 在线评估层面

在线 C 输出不能用旧的 `test_2d_mmse.py` 直接二次滤波评估。  
当前更合理的评估方式是 `eval_pdpR_nmse.py`：

- 固定 STO 补偿。
- 精确 slot 配对。
- per-antenna LS 对齐。

### 6.4 dclcom61 attach 问题

最终有效修复是禁用 UE timing correction：

```bash
--ue-timing-correction-disable
```

这件事很关键，因为如果 UE attach 不稳定，后续所有 A/B 结果都会被控制面不稳定污染。

---

## 7. 我现在实际在做什么

从代码和日志看，现在正在做的是：

1. 把 SRS 信道估计增强算法从 Python 推到 OAI C 侧。
2. 让 `ewma` 和 `adaptive` 可以在真实 OAI 运行里切换。
3. 修实验框架，避免假 OK、partial flush、错误评估口径。
4. 解决新服务器 dclcom61 上的 UE attach 不稳定问题。
5. 为下一轮公平 A/B 做准备。

更直白地说：

> 现在不是在“训练 AI”，而是在修 AI 之前的数据地基。如果 SRS 信道估计和 GT 对齐不可信，后面的特征提取和 AI 模型训练都会建立在错误数据上。

---

## 8. 总结

当前项目整体进度可以这样理解：

1. **平台已经起来了**：OAI、Proxy、GT、SRS dump、sweep 框架都已具备。
2. **算法已经进 OAI 了**：EWMA 和 adaptive alpha 已经不是纯 Python，而是接进了 gNB SRS 估计路径。
3. **评估口径正在变可信**：已经识别并修正了 STO、per-antenna phase、partial flush 等问题。
4. **新服务器问题已解决关键根因**：UE attach 不稳定主要由 STO timing correction 抖动触发，已通过启动参数修复。
5. **公平 A/B 结论尚未定稿**：此前数据受帧数不对等、attach 不稳定和评估口径影响，不能作为最终对比结论。
6. **阶段二仍在收敛中**：adaptive alpha 是当前可运行原型，完整 2D MMSE / STO 补偿仍是尚未最终完成的部分。

因此，截至目前最准确的定位是：

> 研究已经从“能不能跑起来”推进到“能不能可信地比较信道估计算法”。代码已经具备在线运行和 A/B 验证能力，但最终算法结论仍处在稳定链路验证阶段。
