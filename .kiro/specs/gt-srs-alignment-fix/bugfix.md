# Bugfix 需求文档

## 简介

在 OAI gNB + Sionna/Proxy 数字孪生系统中，SRS 信道估计的 NMSE 评估存在一个正地板（约 +5 dB 残留），淹没了所有估计器算法的真实增益。一个经过严格离线 + 逐位单元测试验证的 true2d 估计器（窗口 Toeplitz 频阶 × PKF 时阶，CDL 上可达约 -30 dB）在端到端无法被读出 1–3 dB 的增益，原因不是估计器，而是 GT（Ground Truth）与 SRS 的对应关系不可靠。

**根因修正（重要）**：早期版本把根因归结为单一的“IPC ring buffer 管道延迟 δ ≈ 3 slots 导致 GT(T) 与 SRS(T+δ) 错位”。后续证据（`0529_True2D_离线判定日志.md` §4.9）**部分推翻了这个结论**：`gap=0`（同 slot 直接配对）仍然零相关（cdl_c 幅度包络相关仅 +0.08），若仅是 δ 延迟，gap=0 配对本应相关。因此本文档把根因修正为更准确的多因素问题：

- **主因**：SRS dump 的 frame/slot 编号与 GT 的 `slot_id`（= ts // 30720）**不同源 / 不同参考系**，eval 只用一个常数偏移去凑中位数，从未把一对 GT/SRS 配到同一物理信道快照。
- **叠加因素 (a)**：IPC 管道引入**恒定 ≈ +4 样本的 STO**（p10/p90 都 +4），评估管线未补偿。
- **叠加因素 (b)**：GT 保存节奏 `GT_SAVE_EVERY=100`（= 50 ms）远超信道相干时间（~10–28 ms），动态场景下每次保存的 GT 是**独立衰落快照**（GT 帧间自相关 ≈ 0）。
- **叠加因素 (c)**：`UL_PRE_GAIN=2.0` 触发 int16 削顶，污染 attach 与数据采集。

这是一个 `SRS_GT_ALIGNMENT_ISSUE` 类的**预存 bug**，与估计器算法、与近期 C 端改动无关（旧 cdl_a legacy 数据也有此问题）。2026-05-31 在静态场景（UE_SPEED=0 + UL_PRE_GAIN=1.0 + SRS_ONLY_CHANNEL=1 + cdl_c）下，对齐已被坐实可行：STO 测得恒定 +4 样本，NMSE 从 scalar-only +14.91 dB 经 STO 对齐后转负到 -6.37 dB（rx1tx1 -9.49 dB）。这证明 SRS 与 GT 在静态信道上确实对得上；speed=3 的 +5 dB 残留来自 Doppler 把动态包络去相关，叠加未补偿的 +4 样本 STO。

### 推导的 Bug 条件（方法论）

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = (GT_frame, SRS_frame) 配对
  OUTPUT: boolean

  // 配对的 GT 帧与 SRS 帧不对应同一物理信道快照：
  //  - 编号参考系错位（SRS dump 编号 vs GT slot_id = ts//30720 不同源）
  //  - 或存在未补偿的恒定 +4 样本 IPC 管道 STO
  RETURN (GT_frame.reference_frame ≠ SRS_frame.reference_frame)
         OR (uncompensated_STO(X) ≠ 0)
END FUNCTION
```

```pascal
// Property: Fix Checking —— 配对应对应同一物理快照
FOR ALL X WHERE isBugCondition(X) DO
  result ← evaluate'(X)   // 统一参考系 + 补偿 STO 后
  ASSERT 静态场景 NMSE(result) < 0 dB
     AND 动态场景 std(|alpha|)/median(|alpha|) 显著下降
     AND gap=0 配对 corr(|GT|,|SRS|) 强相关（≫ 0.08）
END FOR
```

```pascal
// Property: Preservation Checking —— 非缺陷输入行为不变
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)   // 信号处理链路 / 估计输出 / IPC 时序 不变
END FOR
```

- **F**：修复前的评估 / 对应链路（编号参考系错位、STO 未补偿）
- **F'**：修复后的评估 / 对应链路（参考系统一、STO 补偿）

## Bug 分析

### 当前行为（缺陷）

1.1 WHEN 评估脚本按 slot 编号配对 GT 帧与 SRS 帧 THEN 系统使用 SRS dump 的 frame/slot 编号与 GT 的 `slot_id`（= ts // 30720）进行匹配，但两者不同源 / 不同参考系，从未把一对 GT/SRS 配到同一物理信道快照

1.2 WHEN 用 `gap=0`（同 slot 直接配对）评估 GT 与 SRS 的幅度包络相关性 THEN 系统测得零相关（cdl_c +0.08，频域 mavg101 平滑后仍 0.08），表明问题不只是管道延迟 δ（若仅是 δ 延迟，gap=0 配对本应相关）

1.3 WHEN GT 以 `GT_SAVE_EVERY=100`（= 50 ms，远超信道相干时间 ~10–28 ms）的节奏保存 THEN 系统每次保存的 GT 是独立衰落快照（GT 帧间自相关 ≈ 0），动态场景下无法与对应 slot 的 SRS 对应

1.4 WHEN IPC 管道把 SRS IQ 传输至 gNB 做信道估计 THEN 系统引入恒定 ≈ +4 样本的 STO（p10/p90 都 +4），且评估管线未补偿，导致逐子载波线性相位错位

1.5 WHEN 评估脚本仅用一个常数偏移做单标量对齐去凑中位数 THEN 系统在动态场景（UE_SPEED=3）STO 对齐后 per-frame |alpha| 仍剧烈波动、NMSE 残留约 +5 dB 正地板（cdl_c legacy STO 对齐 +5.04 dB），淹没估计器 1–3 dB 的真实增益

1.6 WHEN 数据采集以 `UL_PRE_GAIN=2.0` 运行 THEN 系统触发 int16 削顶，导致 ULSCH BLER 约 65%、attach 掷硬币，污染 SRS/GT 数据采集

1.7 WHEN 以 `SRS_ESTIMATOR=passthru`（raw LS）+ STO 对齐在动态场景做判别诊断 THEN 系统的 raw LS 同样残留约 +5 dB 正地板（与估计器算法、与近期 C 改动无关；旧 cdl_a legacy 数据亦如此），表明缺陷是预存的 GT↔SRS 对齐问题而非估计器侧

### 期望行为（正确）

2.1 WHEN 评估脚本配对 GT 帧与 SRS 帧 THEN 系统 SHALL 使用统一 / 同源的编号参考系（把 SRS dump 的 frame/slot 编号与 GT 的 `slot_id`（= ts // 30720）对齐到同一物理 slot），使每个配对对应同一物理信道快照

2.2 WHEN 编号参考系统一后用 `gap=0`（同 slot 配对）评估相关性 THEN 系统 SHALL 测得 GT 与 SRS 幅度包络强相关（显著高于当前 0.08 的零相关水平）

2.3 WHEN IPC 管道引入恒定 +4 样本 STO THEN 系统 SHALL 在评估对齐中补偿该恒定 STO（逐帧 / 逐天线线性相位对齐）

2.4 WHEN 在静态场景（UE_SPEED=0, UL_PRE_GAIN=1.0, SRS_ONLY_CHANNEL=1, cdl_c）做 STO 对齐评估 THEN 系统 SHALL 使 NMSE 转负（已实测 scalar-only +14.91 dB → STO 对齐 -6.37 dB，rx1tx1 -9.49 dB）

2.5 WHEN 在动态场景统一参考系并补偿 STO 后评估 THEN 系统 SHALL 使 per-frame |alpha| 的 std/median 显著下降、正地板消除，残留仅来自可解释 / 可分离的 Doppler 去相关部分

2.6 WHEN 以 `SRS_ESTIMATOR=passthru`（raw LS）+ STO 对齐做判别诊断 THEN 系统 SHALL 据此切分问题域：若 raw LS 仍残留约 +5 dB ⇒ 纯 GT 参考系问题（查 GT 符号 / 时延 / CP 参考系）；若转负 ⇒ 缺陷在估计器侧

2.7 WHEN 进行 SRS/GT 数据采集 THEN 系统 SHALL 使用 `UL_PRE_GAIN=1.0` 以避免 int16 削顶，保证 attach 稳定与数据洁净

### 不变行为（回归防护）

3.1 WHEN Proxy 进行实际 UL 频域卷积信号处理时 THEN 系统 SHALL CONTINUE TO 使用相同的 H(f) 对 UE 信号进行频域卷积，不影响实际信号处理链路

3.2 WHEN gNB 执行 SRS 信道估计的核心 LS + FIR 插值流程时 THEN 系统 SHALL CONTINUE TO 产生与修复前完全相同的 `srs_estimated_channel_freq` 输出

3.3 WHEN IPC ring buffer 进行正常的 UL IQ 数据传输时 THEN 系统 SHALL CONTINUE TO 保持现有的数据传输时序和吞吐量，不引入额外延迟

3.4 WHEN 系统在非 SRS slot 运行时 THEN 系统 SHALL CONTINUE TO 正常处理 PUSCH/PUCCH 等其他 UL 信道，不受 GT↔SRS 对齐修复影响

3.5 WHEN 评估脚本处理旧格式的 bin 文件时 THEN 系统 SHALL CONTINUE TO 兼容旧版 SRS bin 文件格式（向后兼容）
