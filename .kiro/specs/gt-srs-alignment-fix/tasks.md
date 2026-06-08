# Implementation Plan

> 方法论：先写测试 demonstrate bug（探索性测试 EXPECTED TO FAIL on unfixed code）→ 固化 preservation 基线 → 实现修复 → 验证 fix + preservation。
>
> 落点决策（Orchestrator 已固定，任务中不再列为待定）：
> - **采集侧选 B1**（GT 侧记录可换算到 SFN 域的旁路量），不选 B2。依据：gNB 的 SRS dump 注入点只拿得到 SFN 域 frame/slot，拿不到 IPC 采样时间戳；B2 需把 IPC ts plumbing 进 PHY 实时路径，违反不变行为 3.1–3.3。proxy(`v8.py`) 本就持有 `ipc_ts` 且 GT 保存在非实时关键路径，B1 是非侵入正确落点。
> - **以评估侧（A）为主、采集侧（B1）为辅助 enabler，附带修 GT 节奏**。两套编号本质同速率（0.5ms/slot, mu=1）的单调 slot 计数器，只差常数偏移；真正缺的是 gap=0 物理验证（A3）+ 强制 STO 补偿（A2）。
>
> **C 端不变行为约束（贯穿全程）**：本 spec **不修改 OAI 实时信号链路**（C 端 PHY / IPC 时序）。B1 只在 `v8.py`（Python proxy，非实时关键路径）的 npz 写入处新增旁路元数据字段；**不在 C 端 SRS bin 头新增字段**，不改估计输出、不改 IPC 时序（保护 3.1–3.4）。
>
> **采集配置约束（贯穿全程）**：所有数据采集任务统一用 `UL_PRE_GAIN=1.0` 避免 int16 削顶（需求 2.7）。

- [ ] 1. 编写 Bug 条件探索性测试（实现修复前）
  - **Property 1: Bug Condition** - 参考系错位 + STO 未补偿导致配对非同一物理快照
  - **PBT 任务（必须项）**：用 property-based 测试在 UNFIXED 代码上 surface 反例 demonstrate 缺陷
  - **CRITICAL**: 此测试 MUST FAIL on unfixed code —— 失败即确认缺陷存在
  - **DO NOT attempt to fix the test or the code when it fails** —— 此阶段只观察、记录反例
  - **NOTE**: 此测试编码了期望行为，修复后它将转为通过以验证 fix（见 4.6）
  - **Scoped PBT Approach（确定性缺陷）**：把 property 收敛到可复现的具体失败用例（cdl_c 静态/动态 fixture + 已知注入 STO），保证可复现
  - 反例 (a) 参考系错位：在 UNFIXED `align_frames`（`eval_nmse_clean.py` L150–190，`C_rough = median(gt)-median(srs)`）上对 `gap=0` 配对断言 `corr(|GT|,|SRS|)` 应强相关 —— 实测 ≈ 0.08（频域 mavg101 平滑后仍 0.08），断言失败（demonstrate 编号不同源）
  - 反例 (b) STO 未补偿：scalar-only 口径（`eval_nmse_clean`）在 cdl_a 上断言 NMSE < 0 —— 实测 +12..+14 dB，断言失败
  - 反例 (c) 恒定 STO：用 `eval_nmse_sto.py::_sto_es` 网格搜索断言 median STO ≈ 0 —— 实测 ≈ +4（p10/p90 都 +4），断言失败
  - 边界（may pass）：静态场景（UE_SPEED=0, UL_PRE_GAIN=1.0, SRS_ONLY_CHANNEL=1, cdl_c）STO 对齐后 NMSE 转负（实测 +14.91 → -6.37 dB）—— 证明编号在无 Doppler 时能对上、STO 补偿必要且正确
  - 在 UNFIXED 代码上运行
  - **EXPECTED OUTCOME**: 测试 FAILS（这是正确的 —— 它证明 bug 存在）
  - 记录反例（如 "gap=0 corr=0.08 instead of strong correlation"、"scalar-only cdl_a = +13.x dB"、"median STO = +4"）以理解根因
  - 测试写完、跑过、失败被记录后标记此任务完成
  - _Bug_Condition: isBugCondition(X) = reference_mismatch OR sto_uncompensated（design）_
  - _Requirements: 1.1, 1.2, 1.4, 1.5_

- [ ] 2. D0 决定性诊断 —— 切分问题域（GT 参考系 vs 估计器侧）
  - **IMPORTANT**: 这是早期分诊任务，复用 `eval_nmse_sto.py`（不重造）
  - 以 `SRS_ESTIMATOR=passthru`（raw LS）+ STO 对齐在**动态场景**跑 `eval_nmse_sto.py`
  - 采集用 `UL_PRE_GAIN=1.0`（需求 2.7，保证数据洁净，避免 int16 削顶污染诊断）
  - 判据：若 raw LS 仍残留约 +5 dB ⇒ **纯 GT 参考系问题**（进入任务 4 的 A1/B1 编号映射修复）；若转负 ⇒ 缺陷在估计器侧（超出本 spec 范围，记录后停止）
  - 记录诊断结论以确认/推翻根因 1（参考系）
  - **EXPECTED OUTCOME**: 确认根因后方可进入实现阶段
  - _Requirements: 1.7, 2.6_

- [ ] 3. 编写 Preservation property 测试（实现修复前）
  - **Property 2: Preservation** - 非缺陷输入行为不变（F(X) = F'(X)）
  - **PBT 任务（必须项）**：preservation 本质是"对所有非缺陷输入"的全称性质，用 property-based 测试提供强保证
  - **IMPORTANT**: 遵循 observation-first 方法论 —— 先在 UNFIXED 代码上观察并记录真实输出，再写断言固化
  - 观察 (a) scalar-only 口径：在旧格式 bin 上记录 `eval_nmse_clean` 的 scalar-only NMSE 数值（global / per-antenna）
  - 观察 (b) 旧 bin 解析：记录缺少新字段（或缺 `slot_ids`）的旧格式 bin 经 `load_srs` 解析、`_unwrap_abs_slots`（含 `SFN_WRAP` 回绕）后的行为
  - 观察 (c) 估计器原始输出：记录 passthru / 各估计器的 `srs_estimated_channel_freq`（位级快照）
  - 写 property-based 测试，对随机但可控的旧格式 bin 输入断言：`align_frames(unified=False)` 与修复前 scalar-only 输出**位级一致**；旧 bin 仍可解析并退回 scalar-only 路径；估计器输出 F(X)=F'(X)
  - 在 UNFIXED 代码上运行
  - **EXPECTED OUTCOME**: 测试 PASS（确认基线行为，作为修复后回归防护）
  - 测试写完、跑过、在 unfixed 代码上通过后标记此任务完成
  - _Preservation: OAI 实时链路 / 估计输出 / IPC 时序 / 非 SRS slot / 旧 bin scalar-only 口径不变（design）_
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [ ] 4. 修复 GT↔SRS 对齐（统一参考系 + 强制 STO 补偿 + 采集侧编号打通）

  - [ ] 4.1 A1 评估侧：统一编号参考系（核心，对应根因 1）
    - 在 `eval_nmse_clean.py::align_frames` 新增**可选参数** `unified=`（默认 `False`，关闭时保持现有签名与 scalar-only 行为位级一致 → 保护 3.5）
    - 新增确定性映射函数 `unify_reference_frame(gt_slots, srs_abs, mapping)`，把 GT `slot_id = ts//30720`（绝对采样域）与 SRS `abs_slot = SFN*20+slot`（SFN 域）归一到同一物理 slot
    - 映射来源优先级：① 若 B1 已打通，直接用 GT/SRS 各自记录的同源量（恒等/已知线性换算）；② 否则用经 D0 验证的换算关系
    - **硬判据**：映射后必须能达成 gap=0 强相关，否则**显式报错而非静默凑数**（替换当前 `C_rough = median(gt)-median(srs)` 的中位数硬凑）
    - _Bug_Condition: reference_mismatch（GT/SRS 编号无共同原点）_
    - _Expected_Behavior: expectedBehavior(result) → 配对对应同一物理 slot，gap=0 可达成（design Property 1a/1c）_
    - _Preservation: unified=False 时 scalar-only 口径位级一致（3.5）_
    - _Requirements: 2.1, 2.2_

  - [ ] 4.2 A2 评估侧：强制 STO 补偿（对应根因 2）
    - 评估默认**复用** `eval_nmse_sto.py` 的逐帧/逐天线 STO 网格搜索（`tau_range`, `tau_step`，不重造 `_sto_es`）
    - 同时报告 scalar-only 与 STO 对齐两列（现有 `_print_one` 已支持），不破坏 clean 口径
    - 只读估计输出做对齐，不回写、不改估计器（保护 3.2）
    - _Bug_Condition: sto_uncompensated（FFT 域残留线性相位斜坡）_
    - _Expected_Behavior: 补偿后静态 NMSE < 0 dB（design Property 1a，复现 -6.37 dB 量级）_
    - _Preservation: 只读 srs_estimated_channel_freq，不改其输出（3.2）_
    - _Requirements: 2.3, 2.4_

  - [ ] 4.3 A3 评估侧：gap=0 同源相关性验证模式（对应根因 1 验证）
    - 在统一参考系下做 gap=0 配对，计算 `corr(|GT|, |SRS|)` 作为对齐成功的硬判据
    - 阈值设为远高于 0.08（如 ≥ 0.7，具体阈值在实现时基于静态 fixture 标定）
    - 验证失败时报告对齐未成功（与 A1 的显式报错联动）
    - _Bug_Condition: reference_mismatch 的物理验证_
    - _Expected_Behavior: gap=0 corr(|GT|,|SRS|) ≫ 0.08（design Property 1c）_
    - _Requirements: 2.2_

  - [ ] 4.4 B1 采集侧：在 `v8.py` 写 npz 时记录可换算 SFN 域的旁路元数据
    - 在 `GTBatchSaver._build_payload_and_swap_locked`（L1268–1290）写 npz 时，除现有 `slot_ids = ts//30720` 外，**新增字段**记录可换算到 SFN 域的旁路量（如对应 frame/slot 或共享锚点 timestamp，proxy 侧 `ipc_ts` 已持有）
    - 处理 `try_begin_slot` 在 `ipc_ts` 不可用时 fallback 到 `self._ul_slot_counter`（第三套编号）的问题：新字段需明确标注采用了哪套编号，缺 `ipc_ts` 时显式标记不可换算
    - **C 端约束（必须标注）**：本字段只在 `v8.py`（Python proxy，**非实时关键路径**）的 npz payload 新增；**不在 C 端 SRS bin 头新增字段**，不碰 OAI 实时信号链路、不改 IPC 时序、不改估计输出（保护 3.1, 3.3, 3.4）
    - 读侧（`load_srs` / `load_gt`）对缺少新字段的旧 bin **退回 scalar-only 路径**（保护 3.5）
    - 采集用 `UL_PRE_GAIN=1.0`（需求 2.7）
    - _Bug_Condition: reference_mismatch enabler（让 A1 映射为恒等/已知线性换算）_
    - _Expected_Behavior: GT 侧记录同源可换算量，A1 映射成功（design Property 1）_
    - _Preservation: 旁路元数据不改实时链路/IPC/估计输出（3.1, 3.3, 3.4）；旧 bin 缺字段退回 scalar-only（3.5）_
    - _Requirements: 2.1_

  - [ ] 4.5 GT 节奏：动态验证场景调整 `GT_SAVE_EVERY` 匹配 SRS 周期
    - 把动态验证场景的 `GT_SAVE_EVERY=100`（= 50 ms ≫ 相干时间 ~10–28 ms）调整到匹配 SRS 周期，避免每次保存的 GT 是独立衰落快照（帧间自相关 ≈ 0）
    - **权衡说明（必须记录）**：0528 日志教训 —— 加密 GT 在临界 attach 下会增开销；但 `SRS_ONLY_CHANNEL=1` 已解耦 attach，故动态采集场景可安全提高保存频率。这是采集配置任务，不改源码逻辑
    - 采集用 `UL_PRE_GAIN=1.0`（需求 2.7）
    - _Bug_Condition: GT 超相干时间保存（独立采集约束）_
    - _Expected_Behavior: 动态场景 GT 帧可与对应 slot SRS 对应（design Property 1b）_
    - _Requirements: 1.3, 2.5_

  - [ ] 4.6 验证 Bug 条件探索性测试现在通过
    - **Property 1: Expected Behavior** - 统一参考系 + STO 补偿后配对对应同一物理快照
    - **IMPORTANT**: 重跑任务 1 的**同一个**测试 —— 不要写新测试
    - 任务 1 的测试编码了期望行为；当它通过时即确认期望行为被满足
    - 断言：(a) 静态场景 NMSE < 0 dB；(b) 动态场景 per-frame |alpha| 的 std/median 相对 scalar-only baseline 显著下降、正地板消除；(c) gap=0 corr(|GT|,|SRS|) ≫ 0.08
    - **EXPECTED OUTCOME**: 测试 PASSES（确认 bug 已修复）
    - _Expected_Behavior: design Property 1（Validates Requirements 2.1–2.5）_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [ ] 4.7 验证 Preservation 测试仍然通过
    - **Property 2: Preservation** - 非缺陷输入行为不变
    - **IMPORTANT**: 重跑任务 3 的**同一组**测试 —— 不要写新测试
    - 断言：`align_frames(unified=False)` 与修复前 scalar-only 输出位级一致；旧 bin 仍可解析退回 scalar-only；估计器输出 F(X)=F'(X)
    - **EXPECTED OUTCOME**: 测试 PASS（确认无回归）
    - _Preservation: design Property 2（Validates Requirements 3.1–3.5）_
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [ ] 5. Checkpoint —— 确保所有测试通过
  - 运行完整测试套件：Property 1（Bug Condition → Expected Behavior）、Property 2（Preservation）、单元测试、集成测试
  - 确认 Property 1 在修复后通过、Property 2 仍通过（无回归）
  - 确认静态场景 NMSE < 0 dB、动态场景正地板消除、gap=0 强相关三项硬判据全部满足
  - 如有疑问，向用户确认
