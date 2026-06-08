# 实验日志：SRS 2D Channel Estimation（2026-05-09 ~ 05-12）

## 背景

教授 4/29 要求在 OAI SRS 上加 2D MMSE filtering（时域+频域），5/7 点名用 Lattice-RLS。
目标：压制 LS estimate 的噪声，改善 NMSE。

---

## 实验 0：PDP→R 频域 Two-Stage MMSE（05-09 ~ 05-11，已停止）

**详细记录**：`PDP_R_RETROSPECTIVE_20260511.md`

**做了什么**：实现了 1D 频域 complex MMSE（LS → IFFT → PDP → R(Δk) → per-SC MMSE solve）。

**结果**：
- 复杂度 5-10 ms/call（Legacy 0.1 ms），50-100× 过重
- Fair comparison（seed=42）下 NMSE p50 比 Legacy 差 3-6 dB
- Legacy 全 SNR 稳定在 -7 dB（fixed-point floor 主导）

**失败原因**：
1. 方向错了——做了 1D frequency，教授要的是 2D（time + frequency）
2. 复杂度爆炸——教授要 "negligible"
3. 没用教授点名的 lattice-RLS
4. Same-channel fair comparison 太晚才做（之前被 random seed 误导）

**有价值的产出**：eval 脚本、preflight、STO 校正工具、fair comparison 纪律。

---

## 实验 1：Python LSL M=2 验证（05-11）

**做了什么**：写了 `lsl_m2_reference.py`，用合成数据测试 M=2 Lattice-RLS。

**结果**：
- 静态 channel + i.i.d. noise：LSL gain ≈ +3 dB
- EWMA α=0.99 同条件：gain ≈ +20 dB

**发现的问题**：
1. LSL 的 joint-process estimator 在 `d(t)=u(t)` (self-prediction) 模式下收敛到 identity filter（数学证明：`b_0=u=d`，所以 `ρ_0→1, ρ_1,ρ_2→0`）
2. 改用 forward prediction error subtraction (`u - f_M`)，有效平均长度只有 M+1=3，天花板 ~5 dB
3. EWMA 有效平均长度 N_eff=1/(1-α)，α=0.99 时 N_eff=100，理论 +20 dB

**结论**：对 channel state estimation（已有 noisy LS，只需平滑），EWMA 是最优工具。LSL 的优势在 system identification（有 reference signal），不在 state smoothing。

---

## 实验 2：OAI EWMA α=0.9 实测（05-12 08:58）

**做了什么**：
- 新建 `nr_srs_2d_filter.c/h`，实现 EWMA（`H_smooth = α·H_prev + (1-α)·H_new`）
- 接入 dispatcher：`SRS_ESTIMATOR=2dmmse` → `NR_SRS_EST_MMSE2D` case
- 编译通过，跑 fair sweep（legacy vs 2dmmse，SNR=20，seed=42）

**结果**：

| 指标 | Legacy | EWMA α=0.9 | 差异 |
|------|--------|------------|------|
| NMSE LS-aligned p50 | -7.39 dB | -7.19 dB | +0.20 dB（略差） |
| p10 | -17.46 dB | -21.32 dB | -3.86 dB（好） |
| p90 | -2.63 dB | -0.86 dB | +1.77 dB（差） |

**分析**：EWMA 在稳定帧上有效（p10 改善），但在 channel 快变帧上引入 lag（p90 恶化）。整体 p50 持平。

---

## 实验 3：OAI EWMA α=0.5 实测（05-12 09:25）

**做了什么**：降低 α 到 0.5，减少 tracking lag。

**结果**：

| 指标 | Legacy | EWMA α=0.5 | 差异 |
|------|--------|------------|------|
| NMSE LS-aligned p50 | -7.24 dB | -7.20 dB | +0.04 dB（持平） |
| p10 | -18.97 dB | -18.17 dB | +0.80 dB |
| p90 | -2.08 dB | -0.23 dB | +1.85 dB（差） |

**分析**：α=0.5 也没有改进。问题不在 α 的选择。

---

## 实验 4：D0 Floor 诊断（05-12 10:01）

**做了什么**：跑 3 个模式对比，定位 -7 dB floor 来源。

**结果**：

| 模式 | NMSE LS-aligned p50 | paired frames | active SC |
|------|---------------------|---------------|-----------|
| legacy | -6.64 dB | 95 | 1248 |
| passthru | -6.49 dB | 96 | 624 |
| oracle | -0.04 dB | 7 | 2048 |

**关键发现**：

1. **Oracle p50 ≈ 0 dB** — 注入完美 GT 后评估出来 NMSE ≈ 0 dB（error ≈ signal）。这说明问题在下游评估管线，不在 estimator。

2. **Legacy ≈ Passthru**（-6.64 vs -6.49）— filt8/16 频域插值几乎没有贡献。

3. **Oracle 只 paired 7 frames**（vs legacy 95）— 统计不可靠。可能是 GT 注入的 slot_id 跟 Sionna GT 的 slot_id 对不上。

4. **Oracle active SC = 2048**（全部），而 legacy = 1248 — 说明 oracle 注入的数据覆盖了全部子载波，跟正常 SRS 的 sparse pattern 不一致。

5. **Oracle `|alpha| = 133`** vs legacy `|alpha| = 214` — scale 不一致，说明 oracle 注入的 GT scale 跟 OAI 内部 SRS 输出的 scale 不匹配。

---

## 当前状态总结

| 问题 | 状态 |
|------|------|
| LSL 在 self-prediction 模式下无效 | ✅ 已证明（数学 + Python 实测） |
| EWMA 在 OAI 实测无改进 | ✅ 已证明（α=0.9 和 0.5 都试了） |
| -7 dB floor 来源 | ⚠️ D0 结果指向评估管线问题，但 oracle 数据不可靠（只 7 frames） |
| GT-SRS alignment | ❌ 未解决（scale mismatch ~200×，slot pairing 问题） |

---

## 下一步方向

1. **修复 GT-SRS alignment** — 这是最优先的。如果评估方法本身有问题，任何算法改进都测不出来。具体：
   - 为什么 oracle 只 paired 7 frames？（slot_id 对齐问题）
   - 为什么 oracle active SC = 2048 而不是 1248？（注入格式问题）
   - `|alpha| = 200` 的 scale 差异从哪来？

2. **确认 estimator 是否真的有 -7 dB floor** — 如果 alignment 修好后 oracle NMSE 降到 -20 dB 以下，说明 estimator 有改进空间；如果还是 -7 dB，说明 floor 真的在 estimator 内。

3. **如果 floor 在 estimator 内** — 回到时域方法（EWMA/LSL），但需要先确认 noise 是否 i.i.d.。

---

## 文件清单（本次新增/修改）

| 文件 | 用途 |
|------|------|
| `nr_srs_2d_filter.c` | EWMA 实现（已编译通过，已实测） |
| `nr_srs_2d_filter.h` | 接口声明 |
| `nr_srs_mmse.c` | 改了 env 解析（加 `2dmmse`/`2d-lsl` 识别） |
| `nr_ul_channel_estimation.c` | 加了 MMSE2D dispatcher case |
| `CMakeLists.txt` | 加了 `nr_srs_2d_filter.c` |
| `ewma_test.sh` | A/B 测试脚本 |
| `lsl_m2_reference.py` | LSL Python reference（prediction error 版） |
| `lsl_m2_joint_process.py` | LSL joint-process 版（验证 identity filter 问题） |

---

## 关键教训

1. **先验证评估方法再调算法** — 我们花了时间调 EWMA/LSL，但问题可能在评估管线的 GT-SRS alignment。
2. **合成测试 ≠ 实际系统** — Python 里 EWMA +20 dB，OAI 里 +0 dB。差异来自 channel 非静态 + 评估管线问题。
3. **LSL self-prediction 是 identity filter** — 这是数学定理，不是实现 bug。教授推荐 lattice 的语境是 adaptive equalization，不是 state smoothing。
