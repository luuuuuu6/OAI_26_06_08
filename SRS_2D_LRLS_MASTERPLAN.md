# SRS 2D-LRLS Masterplan(完全重启版)

| 字段 | 值 |
|------|----|
| **创建日期** | 2026-05-11 |
| **当前版本** | **v2**(2026-05-11 同日修订,基于 review 反馈) |
| **作者** | LIULU + AI 协作 |
| **状态** | v2 DRAFT — 等待 sign-off |
| **取代文档** | `LATTICE_RLS_FILTERBANK_DESIGN.md`(已 ARCHIVED) |
| **承接文档** | `PDP_R_RETROSPECTIVE_20260511.md`(失败教训) |
| **配套文档** | `D0_FLOOR_DIAGNOSIS_PLAN.md`(D0 实施细节) |
| **核心目标** | 实现教授 4/29 明确要求的 **2D MMSE filtering**,系数用 5/7 点名的 **Lattice-RLS** 在线自适应,**前提是 D0 floor 诊断证明 floor 在 SRS estimator 内** |

---

## v1 → v2 Changelog

v2 基于一份独立 review 修复了 v1 的 3 个盲点:

| 改动 | v1 | v2 | 引发位置 |
|------|----|----|----------|
| Floor 来源 | 假设"时域平均能突破" | **不假设,加 D0 实证诊断** | §2 表格 + §7 D0 + §10 |
| Timeline | 10 天 | **7-8 天**(分阶段递进) | §7 时间表 |
| EWMA 角色 | R6 fallback(实施完才退) | **D2 mandatory checkpoint**(prerequisite gate) | §7 + §9 R6 |
| 5/7 行 28-29 | 漏引 | **补全(教授时间限制原则)** | §1.2 + §2 |
| Doppler-coherence vs SRS-period | 未提 | **加 R8** | §9 |
| Target dB 数字 | NMSE ≥ 2 dB(预设) | **D0 后由 oracle NMSE 决定** | §10 |
| §5.3.3 d(t)=u(t) 数学 | 公式无注释 | **加 self-prediction 解释** | §5.3.3 末 |
| §5.4 内存边界 | 7.5 MB | **加 single-cell 假设注** | §5.4 末 |
| §5.5 "per slot" | 模糊 | **改 "per SRS slot",标 SRS 周期** | §5.5 头 |

---

## 0. TL;DR(一页纸版,v2)

- **Reset**:回退所有 PDP-R / 简化 two-stage 残留代码到 Legacy baseline,保留所有评估基础设施。
- **D0 mandatory(0.5–1 天)**:加 `SRS_ESTIMATOR=passthru` + `SRS_ESTIMATOR=oracle`,实证诊断 -7 dB floor 来源。**floor 在下游就 STOP,不进 D2**。
- **D2 mandatory(0.5–1 天)**:fixed-α EWMA(M=0 lattice 的 degenerate 形态)实证。**EWMA < 1 dB 改进就 STOP,LSL 不会更好**。
- **新算法(D3-5,仅 D0 + D2 都过才做)**:**2D-LRLS** = `Legacy 频域插值` × `Lattice-RLS 跨 SRS slot 时域自适应`。
- **关键数学**:Haykin Ch.16 的 **M=2 阶 LSL (Least-Squares Lattice)**,per (rx, tx, sc) 独立维护 state。
- **复杂度预算**:per-call ≤ 1 ms(教授 "negligible" 要求),目标 OAI 整机 ≤ +3% 运行时间。
- **性能预期**:**target dB 由 D0 oracle NMSE 决定**(不预设),典型 SNR ∈ [10, 25] dB。
- **时间预算**:**7-8 个工作日**(每阶段 hard go/no-go gate)。
- **教授原则**:符合 5/7 行 28-29 "여기서 채널 에스티메이션 시간을 너무 오래 보낼 생각은 없어"。

---

## 1. 教授原话证据基线(不再脑补)

> 本节是所有后续设计的**唯一权威依据**。任何后续争议都回到这一节。

### 1.1 4/29 — 2D + MMSE 的明确指令

**`listening_0429` 行 219–221** ★最关键★
```
근데 타임 코릴레이션이 있고 주파수 코릴레이션이 있기 때문에
노이즈는 얘네들이 다 인디펜던트지만
사실은 이 채널 시그널 SNS 시그널 자체는 다 코릴레이션이 있다고
그러면 시그널의 코릴레이션을 이용해서 노이즈를 최대한 서프레션 해줘야 되겠지.
그럼 그게 기본적으로 2D 필터링을 해주면 노이즈가 서프레션이 돼.
그리고 그 2D 필터링의 가장 기본은 mmse 방식이야.
```

→ **要求 2D filtering,baseline 是 MMSE。**

**行 222(允许的退路 1)**:
```
도플러 이동 속도에다가 이 프리퀀시 셀렉티비티 정도 파라미터화해가지고
그냥 필터 개수를 프리플라이즈 해가지고 적당한 걸 선택해서 쓰는 정도로만 해도
그게 성능이 그렇게 나쁘지 않아.
```
→ 不必完美 MMSE,可以预制 filter bank 选择。

**行 222(允许的退路 2 — 重要)**:
```
MMS에 어차피 파라미터를 우리가 알면 그냥 거기에 맞춰서 코피전트를 정하면 되는데
그걸 모르면 그 코피전트를 정하는 게
예를 들면 어댑티브 필터링 채널 익스포페이션 방식으로 이제 그걸 보통 정하거나
```
→ MMSE 系数未知时,**用 adaptive filtering 来确定**。

**行 225–227(教授给的实施提示)**:
```
현재 oai가 그런 게 전혀 없다고 하면 그걸 추가로 넣어줘야 일단 기본적으로 채널 추정 자체가 되는 거고
아마 DMRS 쪽에는 있을 거야...
DMRS 쪽에 보면 그걸 채널 측정하기 위해서 2D 필터링을 어떻게 하는지 확인을 해보고
그러면 기본적으로 SRS 쪽에도 동일한 2D 필터링을 SRS 모양에 맞춰서 한 다음에
```
→ "OAI 没有就加上,先看 DMRS 怎么做,再照搬到 SRS"。
→ **下文 §3.3 调研结果显示 DMRS 实际上也没有 2D filtering — 这条提示需要修订。**

### 1.2 5/7 — Adaptive 退路的具体化 + 时间限制

**`listening0507` 行 14–17** ★算法点名★
```
LMS는 수렵은 빨리 하는데 성능이 안 나와
그럼 그걸 갖다가 이제 RNS 기반으로 해야 되는데 RNS는 엄청 복잡해.
그래서 수렴 속도랑 복잡도를 보통 내가 잡아가지고 트레이드 오프로 잘 만드는 게 보통
그래서 레티스 기반 RND스야 그런 것들이 이미 기존들이 많이 있다고
```
→ **明确点名 Lattice-RLS**(对应 4/29 的 "adaptive filtering" 退路 2)。

**5/7 验收标准**(行 24–25):
```
복잡도 때문에 늘어난 건 이게 얼마큼 무시할 수 있는데 성능은 얼마큼 좋아졌다.
그렇게 딱 정리해서 얘기해 주면 돼요.
```
→ 汇报模板:**复杂度增量可忽略 + 性能改进多少**。

**`listening0507` 行 28–29** ★时间限制(v2 新增)★
```
다시 말하지만 여기서 채널 에스티메이션 시간을 너무 오래 보낼 생각은 없어.
그냥 주어진 시간 안에 적당한 걸 잡아서
현재 oai가 LS 추정 기반이면 그건 너무 허접하니까
그냥 야 인간적으로 이 정도까지는 잘해야지 이 정도까지만 해.
```
→ "再说一遍,**不打算在 channel estimation 上花太久**。
   在限定时间内挑个适当的,OAI 现在 LS 太粗糙,
   **作为人至少做到这个水平就行,做到这就够了**。"

**v2 强化的工程含义**:
- ❌ 不允许追求"完美 LSL 实现"
- ❌ 不允许 10 天日程
- ✅ MVP 优先:**EWMA 改进 ≥ 1.5 dB 就视为 acceptable submission**,LSL 是锦上添花
- ✅ 每个 phase hard go/no-go gate,确保不在死路上烧时间

### 1.3 综合定位

| 教授要求 | 我们的方案 |
|---------|-----------|
| 2D filtering(time + frequency 都用相关性) | ✅ 频域 Legacy 插值 + 时域 Lattice-RLS(D0 通过后) |
| MMSE 形态 | ✅ Lattice-RLS 在最小均方误差准则下收敛到 Wiener 解 |
| 系数 adaptive 求解 | ✅ Lattice-RLS 是 adaptive filtering |
| Lattice 算法(5/7 点名) | ✅ Haykin Ch.16 M=2 LSL update |
| Complexity negligible | ✅ Per-call ≤ 1 ms 预算 |
| 复用 OAI DMRS 实现 | ⚠️ 调研后发现 DMRS 也没 2D,需要自己实现(见 §3.3) |
| **不花太久(5/7 行 28-29)** | ✅ **7-8 天分阶段 + 每阶段 hard gate** |
| **MVP-first** | ✅ **EWMA 阶段(D2)就能交付,LSL(D3-5)是 stretch** |

---

## 2. 上次失败的根因总结(不重蹈覆辙)

来自 `PDP_R_RETROSPECTIVE_20260511.md`,本次设计逐条规避:

| 失败点 | PDP-R 的做法 | 本次规避策略 |
|--------|-------------|-------------|
| **方向** | 1D frequency MMSE | ✅ 真 2D(时域 + 频域)— 但前提是 D0 通过 |
| **系数求解** | 在线解 4×4 复数矩阵 | ✅ Lattice-RLS 自适应,无矩阵求逆 |
| **复杂度** | 5–10 ms / call,SRS deadline miss | ✅ 严格 ≤ 1 ms,预算可控 |
| **算法点名** | 没用 Lattice-RLS | ✅ 严格按 5/7 用 Lattice-RLS |
| **Floor 突破** | 频域优化撞 fixed-point floor (-7 dB) | **⚠️ v2: 不再假设"时域平均能突破"** — D0 实证诊断 floor 来源,floor 在下游就直接 STOP |
| **Fair comparison** | 早期没 fix seed | ✅ 强制 `CHANNEL_SEED=42`,沿用 retrospective 模板 |
| **退路** | 改动侵入式,难回退 | ✅ env-driven dispatcher (`SRS_ESTIMATOR=...`) |
| **盲跑长流程** (v1 自身错误) | "10 天后才知道结果" | **✅ v2: 每 phase hard gate,1-3 天可决策** |
| **EWMA 错位(v1 错误)** | EWMA 当 fallback,实施完 LSL 才退 | **✅ v2: EWMA 当 prerequisite gate,2 天定 LSL 上限** |

### 2.1 ★ Floor 假设的修正(v2 核心修复)★

v1 §2 表格写"时域跨 slot 平均能突破 floor",但这跟 retrospective §3.3 的实证结论自相矛盾:

> "OAI 接收链路 fixed-point 量化噪声主导,任何算法都难突破 -7 dB floor,**除非动 fixed-point 流水线**"

**矛盾点**:retrospective 说 noise 来自接收链路 fixed-point quantization。这种 noise 跨 SRS slot **不是 i.i.d.**(每次都是同样的 ADC + 同样的位宽截断 + 同样的 c16→int16 转换在同样的 DSP block 上),时域平均**无法压掉相关 noise**。

**v2 修正**:把"时域平均能突破"从 assumption 降级为 **D0 待证 hypothesis**:
- D0.A `passthru` 测试:跳过 freq filt,看 LS 直接输出 NMSE → 区分 floor 在 LS 阶段 vs filt 阶段
- D0.B `oracle` 测试:注入 GT,看 OAI 下游 NMSE 下限 → 区分 floor 在 estimator 内 vs 评估管线下游
- D0 决策矩阵见 §7 + 配套文档 `D0_FLOOR_DIAGNOSIS_PLAN.md`

→ **D0 不通过就不进 LSL 实施**。教授 5/7 行 28-29 不允许烧时间在死路上。

---

## 3. 现状调研(reset 前的 ground truth)

### 3.1 当前 SRS estimation 调用链

```
nr_srs_channel_estimation()                  ← nr_ul_channel_estimation.c:742
  ├─ 计算 LS estimate 到 srs_ls_estimated_channel[]   (lines 811–839)
  ├─ Legacy 频域插值 filt8_*/filt16_*  →  srs_est[]   (lines 864–903)
  └─ dispatcher (line 793, 913):
       ├─ NR_SRS_EST_LEGACY      → 直接用 srs_est[] (line 939–942)
       ├─ NR_SRS_EST_MMSE1D      → 调 nr_srs_mmse_freq_filter() ★PDP-R 当前实现★
       ├─ NR_SRS_EST_MMSE2D = 2  → 枚举已定义但 dispatcher 没 case ★ 占位待填 ★
       └─ NR_SRS_EST_MMSE1D_ADAPT → 同 MMSE1D
```

**关键发现**:`NR_SRS_EST_MMSE2D` 枚举已存在但**未实现**——这就是我们要填的位置。

### 3.2 当前文件状态(NR_ESTIMATION/)

| 文件 | 当前行数 | 状态 |
|------|---------|------|
| `nr_srs_mmse.c` | 789 | PDP-R 实现(1D),要回退 |
| `nr_srs_mmse.h` | 41 | 已含 NR_SRS_EST_MMSE2D 枚举,**保留** |
| `nr_ul_channel_estimation.c` | 1135+ | dispatcher caller,要清掉 PDP-R 路径,加 2D-LRLS 路径 |
| `nr_srs_mmse.c.bak.after_cleanupC_snapshot` | 633 | PDP-R 之前的干净状态,作为 reset 参考 |
| `nr_srs_mmse.c.bak.20260511_pre_ewma` | 790 | 当前 PDP-R 备份(已存在) |

### 3.3 ★ 关键反常识发现:OAI DMRS 也没有 2D filtering ★

教授 4/29 行 225–227 推测 "DMRS 那边可能有 2D filtering",但**实测代码不支持这个推测**:

| 文件 | 频域插值 | 跨符号平均 | 跨 slot 平均 | 真 2D? |
|------|---------|-----------|-------------|--------|
| `nr_ul_channel_estimation.c` (UL DMRS) | ✅ filt8/16 | ❌ 无 | ❌ 无 | ❌ |
| `nr_dl_channel_estimation.c` (DL DMRS) | ✅ filt8/16 | ❌ 无 | ❌ 无 | ❌ |
| `nr_srs_channel_estimation` | ✅ filt8/16 | ❌ 无 | ❌ 无 | ❌ |

证据:
- `NR_ESTIMATION/` 目录下 0 处出现 `slot_avg / prev_chan / EWMA / forget` 等跨时间词
- 所有 estimation 函数都是无 static state 的(每次调完丢)

**结论**:**OAI 整体没有 2D channel estimation,从 DMRS 到 SRS 都是 1D frequency interpolation**。
教授 A5 的 "看 DMRS 怎么做" 提示等价于 "OAI 现在没有,你需要从零加上"——回到 4/29 行 225 的 "현재 oai가 그런 게 전혀 없다고 하면 그걸 추가로 넣어줘야"。

→ **本设计是 OAI SRS 加 2D filtering 的首次尝试,意义比想象的大。**

### 3.4 评估基础设施(全部保留,验证有用)

| 工具 | 文件 | 用途 |
|------|------|------|
| Sweep driver | `run_q4_snr_sweep_v8.sh` | SNR 扫描 + inter-point preflight |
| Preflight | `preflight.sh` (含 `--inside-sweep`) | sweep 间 zombie 清理 |
| NMSE 评估 | `eval_pdpR_nmse.py`(后期改名 `eval_srs_nmse.py`)| 离线 NMSE p10/p50/p90 |
| 出图 | `plot_nmse_vs_snr_v2.py` | 多算法 overlay |
| 复杂度 | `bench_srs_mmse.c` | per-call timing |
| Fair comparison | `CHANNEL_SEED=42` 模板 | 跨算法可比 |

---

## 4. Reset 范围(完全重启的边界)

### 4.1 🔴 必须删除/回退

| 项目 | 操作 |
|------|------|
| `nr_srs_mmse.c`(789 行 PDP-R) | 回退到 Legacy 形态(保留文件,清空 PDP-R 实现) |
| `nr_srs_mmse_freq_filter` 函数 | 重写为 **空 stub**(2D-LRLS 不走这个入口) |
| `solve_complex_system` / `nr_srs_pdp_to_R_dft` / `nr_srs_estimate_noise_floor` | 删除(PDP-R 专用) |
| `nr_ul_channel_estimation.c` 中 `if (MMSE1D)` 调 PDP-R 的分支 | 清掉,改为只走 dispatcher |
| `LATTICE_RLS_FILTERBANK_DESIGN.md` | ✅ 已加 ARCHIVED 标签 |
| 仓库根的临时 md (`1d,2d-mmse_change_log.md` 等过程产物) | 移到 `archive/`(可选,不阻塞主线) |

### 4.2 🟢 必须保留

| 项目 | 理由 |
|------|------|
| `nr_srs_mmse.h` 中的 `nr_srs_estimator_mode_t` 枚举 | 要新加 `NR_SRS_EST_2D_LRLS = 4`,沿用现有 dispatcher |
| `nr_srs_get_estimator_mode()` env getter | 已经实现,扩展即可 |
| `nr_srs_estimate_residual_noise_power()` | 通用 noise 估计,2D-LRLS 也用 |
| 所有评估基础设施(§3.4) | 验证有用 |
| `CHANNEL_SEED=42` fair comparison 纪律 | 必须保留 |
| `nr_srs_mmse.c.bak.*` 系列备份 | 不删,作为历史档案 |

### 4.3 🟡 归档(参考价值,不参与新工作)

| 项目 | 操作 |
|------|------|
| `PDP_R_RETROSPECTIVE_20260511.md` | 加 `[REFERENCE — superseded by SRS_2D_LRLS_MASTERPLAN]` 头部标签 |
| `LATTICE_RLS_FILTERBANK_DESIGN.md` | 已加 ARCHIVED |
| `change_log_v4_to_v7*.tex` 等历史 tex | 留原位 |
| `reply_0507_srs_mmse*.tex/md` | 留原位 |

---

## 5. 算法设计:2D-LRLS

### 5.1 信号模型

设 `H(t, f)` 为真实 channel,在第 `t` 个 SRS slot 第 `f` 个子载波上。LS estimate:
```
H_LS(t, f) = H(t, f) + n(t, f),  n ~ CN(0, σ²)
```

教授 4/29 指出:
- 频域:`H(t, f)` 跨 `f` 有相关性(coherence bandwidth)
- 时域:`H(t, f)` 跨 `t` 有相关性(coherence time)

2D MMSE 的目标是利用这两个相关性同时压噪。

### 5.2 算法骨架(分离式 2D filter)

```
                     LS estimate H_LS(t, f)
                              ↓
    ┌─────────────────────────────────────────────┐
    │ Stage 1 (频域): Legacy filt8/16 插值        │ ← 已有,不动
    │   per-symbol per-(rx,tx)                     │
    │   输出: H_freq(t, f)                         │
    └─────────────────────────────────────────────┘
                              ↓
    ┌─────────────────────────────────────────────┐
    │ Stage 2 (时域): Lattice-RLS 自适应平滑      │ ★ 新增 ★
    │   per-(rx, tx, sc) 独立维护 LSL state        │
    │   输入序列: H_freq(t-K..t, f) for fixed (rx,tx,sc)│
    │   M=2 阶 lattice predictor                   │
    │   输出: H_2D(t, f)                           │
    └─────────────────────────────────────────────┘
                              ↓
                       送给 caller
```

> **数学等价性**:可分离 2D filter `W(τ_t, τ_f) = W_t(τ_t) · W_f(τ_f)` 在频域 ⟂ 时域相关性的假设下,接近最优 2D MMSE。这是 OFDM channel estimation 文献(Edfors 1998, Cavers 1991)的标准近似。

### 5.3 Stage 2 详细数学:M=2 阶 Lattice-RLS(Haykin Ch.16)

#### 5.3.1 状态变量(per (rx, tx, sc))

```
反射系数:    κ_f,m(t),  κ_b,m(t),     m = 0, 1
前向预测误差: f_m(t),               m = 0, 1, 2
后向预测误差: b_m(t),               m = 0, 1, 2
能量项:      E_f,m(t),  E_b,m(t),    m = 0, 1
联合 PE 项:  Δ_m(t),               m = 0, 1
转换因子:    γ_m(t),               m = 0, 1
联合估计权重: ρ_m(t),              m = 0, 1
```

#### 5.3.2 初始化(t = 0)

```
κ_f,m(0) = κ_b,m(0) = 0
E_f,m(0) = E_b,m(0) = δ        (small positive, 例如 1e-3)
Δ_m(0) = 0
γ_m(0) = 1
ρ_m(0) = 0
b_m(-1) = 0
H_2D(0, f) = H_freq(0, f)        ← warm-up,纯 frequency-only
```

#### 5.3.3 时间更新(每个新 SRS slot t,对每个 (rx, tx, sc))

输入:`u(t) = H_freq(t, f)`(freq-stage 输出)。
λ:遗忘因子(0.95–0.99)。

```
# Order 0
f_0(t) = b_0(t) = u(t)
E_f,0(t) = E_b,0(t) = λ·E_f,0(t-1) + |u(t)|²
γ_0(t) = 1

# Order recursion: m = 1, 2
for m = 1, 2:
    Δ_{m-1}(t) = λ·Δ_{m-1}(t-1) + b_{m-1}(t-1) · conj(f_{m-1}(t)) / γ_{m-1}(t-1)
    κ_f,{m-1}(t) = Δ_{m-1}(t) / E_b,{m-1}(t-1)
    κ_b,{m-1}(t) = conj(Δ_{m-1}(t)) / E_f,{m-1}(t)
    f_m(t) = f_{m-1}(t) - κ_f,{m-1}(t) · b_{m-1}(t-1)
    b_m(t) = b_{m-1}(t-1) - κ_b,{m-1}(t) · f_{m-1}(t)
    E_f,m(t) = E_f,{m-1}(t) - |Δ_{m-1}(t)|² / E_b,{m-1}(t-1)
    E_b,m(t) = E_b,{m-1}(t-1) - |Δ_{m-1}(t)|² / E_f,{m-1}(t)
    γ_m(t) = γ_{m-1}(t) - |b_{m-1}(t)|² / E_b,{m-1}(t)

# Joint-process estimator(把 lattice 输出转回 channel estimate)
for m = 0, 1, 2:
    p_m(t) = λ·p_m(t-1) + b_m(t) · conj(d(t)) / γ_m(t)
        # 其中 d(t) = u(t)(desired = LS-after-freq-smoothing,做 self-prediction 噪声压制)
    ρ_m(t) = p_m(t) / E_b,m(t)
H_2D(t, f) = Σ_m ρ_m(t) · b_m(t)
```

#### 5.3.5 ★ Self-prediction 数学含义说明(v2 新增)★

> 这一段直接关系到 LSL 是否能压噪——必须先弄清楚才能 review §5.3.3 公式。

我们的 desired 信号 `d(t) = u(t) = H_freq(t, f)`,即 LSL 在做 **self-prediction**:
用 `H_freq(t-1), H_freq(t-2), ...` 预测 `H_freq(t)`。

**这个为什么能压噪?** 关键假设:
- 真 channel `H(t)` 在 SRS 周期上是 **deterministically slowly-varying**(Doppler-coherent)
- LS 噪声 `n(t)` 在 SRS 周期上是 **i.i.d. 零均值**

LSL 学到的 lattice predictor 拟合的是 `H(t)` 的 deterministic 部分(因为它有时域相关性,可预测)。
i.i.d. 噪声 `n(t)` 不可预测,贡献到 `b_m(t)` 上是平均 0 的随机扰动 → 通过 `ρ_m(t) · b_m(t)` 加权时被抑制。

**关键依赖**:
- ✅ 如果 noise 是 channel SNR 主导(i.i.d.) → LSL 有效
- ❌ 如果 noise 是 fixed-point quantization 主导(跨 slot 强相关) → LSL **学不动 deterministic 部分,反而把 noise 学进去**

→ **这正是 D0 必须先验证的根本原因**。如果 D0 显示 noise 是 quantization 主导,LSL 不仅没用,可能更差(把 quantization pattern 当 channel 学了)。

#### 5.3.4 数值稳定性保护

```
# 防除零
E_b,{m-1}(t-1) = max(E_b,{m-1}(t-1), δ)
E_f,{m-1}(t)   = max(E_f,{m-1}(t),   δ)
γ_{m-1}(t-1)   = max(γ_{m-1}(t-1),   ε)

# 反射系数限幅(数值漂移检测)
|κ_f,m(t)| ≤ 1.0  → 否则触发该 (rx,tx,sc) state reset
```

### 5.4 状态存储设计

```
struct lrls_state_t {
    c16_t   kappa_f[2], kappa_b[2];
    c16_t   delta[2];
    c16_t   p[3], rho[3];
    float   E_f[3], E_b[3];
    float   gamma[3];
    c16_t   b_prev[3];      // b_m(t-1)
    uint8_t valid;          // 0 = uninit, 1 = warming, 2 = converged
    uint16_t n_updates;     // 收敛检测用
};

// 全局 state(per gNB):
struct lrls_state_t  g_lrls[NB_ANT_RX_MAX][NB_ANT_TX_MAX][OFDM_SYMBOL_SIZE_MAX];
```

**内存估算**:
- per state ≈ 24 × 8 byte (复数) + 12 × 4 byte (float) = ~240 byte
- 假设 4 RX × 4 TX × 2048 SC = 32768 states
- **总计 ≈ 7.5 MB**(可接受,gNB 内存通常 GB 级)

> **边界假设(v2 新增)**:此估算基于 **single-cell + 单 SRS resource pool**。
> Multi-cell 部署(N cells)线性放大到 7.5N MB,N ≤ 10 仍可控。
> 大规模 multi-cell + multi-port pool 时需要重新评估,首版不考虑。

### 5.5 复杂度分析

> **v2 命名修正**:下文所有 "per slot" 应理解为 **per SRS slot**(SRS 调度周期通常 5/10/20 ms,远大于普通 OFDM slot 的 0.5/1 ms)。这意味着 LSL 的时间步长 ≠ OFDM symbol 步长,对应的 Doppler-coherence 假设见 §9 R8。

#### Per-(rx, tx, sc) per **SRS slot**:
- Order recursion(2 阶):~20 复数 mul + ~10 复数 add + ~6 div
- Joint-process estimator:~10 复数 mul
- **小计:~40 复数 ops ≈ 80 实数 mul**

#### Per **SRS-estimation call**(整个 SRS estimation):
- 假设 2 RX × 2 TX × 2048 SC = 8192 states
- 8192 × 80 = ~650K 实数 mul
- 在现代 CPU (~10 GFLOPS) 上 = **~0.07 ms 纯计算**
- 加 cache miss + 控制流开销:**实测预期 0.3–0.8 ms**

→ ✅ **远低于 1 ms 预算,远低于 PDP-R 的 5–10 ms**

---

## 6. 模块化方案(env-driven dispatcher)

### 6.1 新增 env var

```bash
# 现有(保留)
SRS_ESTIMATOR=legacy        # OAI 默认 filt8/16(NR_SRS_EST_LEGACY)

# 新增
SRS_ESTIMATOR=2dlrls        # 本设计:Legacy freq + Lattice-RLS time
                      # → enum NR_SRS_EST_2D_LRLS = 4

# Tuning(可选)
SRS_LRLS_LAMBDA=0.97  # 遗忘因子,默认 0.97
SRS_LRLS_ORDER=2      # lattice 阶数,默认 2
SRS_LRLS_DELTA=1e-3   # 数值稳定 δ
SRS_LRLS_DEBUG=0      # 1 = log per-state convergence
```

### 6.2 文件结构(reset 后)

| 文件 | 角色 | 操作 |
|------|------|------|
| `nr_srs_mmse.c` | 保留 dispatcher 入口 + env getter | **删 PDP-R 内部实现,保留壳** |
| `nr_srs_mmse.h` | 加 `NR_SRS_EST_2D_LRLS = 4` 枚举 + 新接口声明 | 改 |
| `nr_srs_lrls_2d.c` ★ 新建 ★ | M=2 LSL 实现 + 全局 state | 新 |
| `nr_srs_lrls_2d.h` ★ 新建 ★ | 接口声明 + state struct | 新 |
| `nr_ul_channel_estimation.c` | dispatcher caller,加 2D-LRLS case | 改(精确 1 处 + memcpy 改成调 lrls) |

### 6.3 dispatcher case(伪代码)

```c
switch (srs_estimator_mode) {
case NR_SRS_EST_LEGACY:
    memcpy(out, &srs_est[mem_offset], n_bytes);  // 现状
    break;

case NR_SRS_EST_2D_LRLS:
    // Stage 1: Legacy 频域插值已经在 srs_est[] 里了
    // Stage 2: 跨 slot Lattice-RLS
    nr_srs_lrls_2d_update(ant, p_index, &srs_est[mem_offset],
                          frame, slot, ofdm_symbol_size, out);
    break;

// ... 其他 mode ...
}
```

---

## 7. 实施时间表(v2:7-8 工作日,每阶段 hard gate)

> **设计原则**(吸收 review):每个 phase 末有明确 go/no-go 决策点。失败立即 STOP 或降级,**不允许"先做完再说"**。

| Day | 任务 | 产出 | **Go/No-Go Gate** |
|-----|------|------|-------------------|
| **D0** ★新★ | Floor 来源诊断:实现 `SRS_ESTIMATOR=passthru` + `SRS_ESTIMATOR=oracle`,跑 SNR=20 sweep | passthru NMSE + oracle NMSE | **oracle NMSE < -15 dB**: 进 D1<br>**oracle ≈ -7 dB**: STOP,改去 audit 评估管线/fixed-point |
| **D1** | Reset:回退 PDP-R 代码 + 重新 build + 验证 Legacy 跑通 | `SRS_ESTIMATOR=legacy` baseline | NMSE p50 ≈ -7 dB(跟 retrospective 一致) |
| **D2** ★mandatory★ | 实现 fixed-α EWMA(α=0.7/0.9/0.95 三档),跑 4 SNR fair sweep | EWMA NMSE 表 + 对比图 | **改进 ≥ 1.5 dB**: MVP 达成,继续 D3<br>**改进 < 1 dB**: 已是 acceptable submission(教授 5/7 行 28 标准),可直接 D6 出报告<br>**改进 0**: STOP,LSL 不会更好 |
| **D3** | 实现 M=2 LSL `nr_srs_lrls_2d.c/h` + 全局 state 分配 | 编译通过 + Python reference 数值对齐 1e-6 | 单元测试 pass |
| **D4** | 集成 dispatcher + env var + warm-up + stress test | `SRS_ESTIMATOR=2dlrls` 跑通 10 分钟无 NaN/Inf | grep WARN 无异常,|κ|<1 |
| **D5** | 跑 4 SNR 点 fair sweep + bench timing | 完整数据 | LSL > EWMA ≥ 0.5 dB(否则 LSL 价值低,退用 EWMA) |
| **D6** | 数据整理 + 报告(slides + 表) | `REPORT_2D_LRLS_YYYYMMDD.md` | 含 5/7 行 24-25 模板填好的复杂度+性能两条 |
| **D7** | Buffer + 教授会议准备 | 演示脚本 | 能 5 分钟讲清楚 |

### 7.1 关键 Decision Tree

```
D0 oracle NMSE
    ├─ < -15 dB ──→ floor 在 estimator 内, 进 D1+
    ├─ -7 ~ -15  ──→ floor 部分在下游, target 改"接近 oracle", 进 D1+
    └─ ≈ -7 dB  ──→ STOP, 改去 fixed-point audit ❌

D2 EWMA NMSE 改进
    ├─ ≥ 1.5 dB ──→ MVP 达成, 继续 D3 LSL stretch
    ├─ 1 ~ 1.5  ──→ acceptable, 继续 D3 但降低期望
    ├─ 0 ~ 1    ──→ MVP 已交付水准, 跳到 D6 出报告
    └─ ≈ 0      ──→ STOP, 时域无 gain, 改方向 ❌

D5 LSL vs EWMA 增益
    ├─ ≥ 0.5 dB ──→ 用 LSL 出最终报告
    ├─ 0 ~ 0.5  ──→ LSL 边际价值低, 用 EWMA 出报告(更简单, 教授更喜欢)
    └─ ≤ 0      ──→ LSL 学坏了, 用 EWMA 出报告
```

### 7.2 v1 → v2 时间表压缩说明

| 项目 | v1(10 天) | v2(7-8 天) | 节省理由 |
|------|-----------|------------|---------|
| Reset + Legacy 验证 | D1(1 天) | D1(0.5 天) | reset 只是 sudo cp + build,不需 1 天 |
| LSL 实现+集成 | D2-D5(4 天) | D3-D4(2 天) | D3 数值对齐用 Python pre-test,不必从 0 调 C |
| Sweep + 出图 | D6-D7(2 天) | D5(1 天) | sweep 跑 1 次 ≤ 1 小时,不必 2 天 |
| 调参 | D8(1 天) | 合到 D5 | EWMA 已经定主 α,LSL 只需 sweep λ |
| 报告 | D9-D10(2 天) | D6-D7(1.5 天) | 模板已 §8.3 提供 |
| **新增 D0** | — | +0.5–1 天 | 投资,可能省 7 天 |
| **新增 D2 EWMA gate** | — | 0.5–1 天 | 投资,可能省 5 天 |

---

## 8. 评估方案

### 8.1 Fair comparison 协议(沿用 retrospective 教训)

```bash
# 必须显式设置
export CHANNEL_SEED=42

# 每个算法独立 sweep,中间 preflight
for ALG in legacy 2dlrls; do
    bash preflight.sh
    SRS_ESTIMATOR=$ALG CHANNEL_SEED=42 \
        bash run_q4_snr_sweep_v8.sh \
            --snr-points "10,15,20,25" \
            --manifest "logs/2d_lrls_${ALG}_seed42"
done
```

### 8.2 数据交付清单

| 指标 | 工具 | 输出位置 |
|------|------|---------|
| NMSE p10/p50/p90 vs SNR | `eval_srs_nmse.py` (rename from pdpR) | `nmse_table.csv` |
| 双算法 overlay 图 | `plot_nmse_vs_snr_v2.py` | `nmse_vs_snr_2dlrls.png` |
| Per-call timing(SRS block) | `bench_srs_mmse.c` | `bench_legacy_vs_2dlrls.txt` |
| OAI 整机 timing 增量 | gNB log 解析 | `total_runtime_delta.md` |
| Convergence trace(可选) | `SRS_LRLS_DEBUG=1` | `lrls_convergence_*.log` |

### 8.3 教授汇报模板(5/7 行 24-25 要求)

```
【复杂度报告】
Legacy:    Per-call X.X ms
2D-LRLS:  Per-call Y.Y ms (+(Y-X)/X ×100%)
OAI 整机:  +Z%(可忽略)

【性能报告】
SNR=10 dB: NMSE p50  Legacy −7.0 → 2D-LRLS −9.X  (改进 X.X dB)
SNR=15 dB: NMSE p50  Legacy −7.1 → 2D-LRLS −10.X (改进 X.X dB)
SNR=20 dB: NMSE p50  Legacy −7.1 → 2D-LRLS −11.X (改进 X.X dB)
SNR=25 dB: NMSE p50  Legacy −7.2 → 2D-LRLS −12.X (改进 X.X dB)

【结论】
"在限定时间内做了 Lattice-RLS 2D filtering,
 复杂度增量 +Z% 可忽略,性能在中低 SNR 改进 2-4 dB"
```

---

## 9. 风险登记 + 应对(v2 修订)

| 风险 | 概率 | 影响 | 应对 |
|------|------|------|------|
| **R1**: M=2 LSL 数值不稳(λ 错) | 中 | 高 | 先用经典 λ=0.97;加 |κ|<1 限幅 + state reset 触发 |
| **R2**: Per-call 超 1 ms 预算 | 低 | 中 | 降到 M=1 阶 lattice;或只对 partial SC 跑 |
| **R3**: Cross-slot state 持久化破坏 OAI 调度 | 低 | 高 | global state 加 mutex(SRS 调用串行,实际不必,但加注释) |
| **R4**: -7 dB floor 仍突破不了 | **中** | **高** | **v2: D0 提前诊断**,floor 在下游就直接 STOP,不到 D5 才发现 |
| **R5**: SRS 调度太稀疏,LRLS 收敛太慢 | 中 | 中 | 加 forced warm-up frames;用 SRS_LRLS_DEBUG 看 n_updates |
| **R6** (v2 修订): 实施超时 | 低 | 中 | **v2: 改为 prerequisite gate,D2 EWMA 实测先于 D3 LSL,不再是 D5 后才 fallback** |
| **R7**: 教授下一次会议改主意 | 低 | 不可控 | 保持文档 traceability,任何变更都重新对比 §1 原话证据 |
| **R8** ★v2 新增★: Doppler-coherence ↔ SRS-period 失配 | **中-高** | **中-高** | 见 §9.1 详细分析 |

### 9.1 R8 详解:Doppler-coherence vs SRS-period 失配

LSL self-prediction 假设 channel 在相邻 SRS slot 之间有 deterministic 相关性。这要求:

```
Coherence time T_c >> SRS period T_SRS
(等价于 Doppler shift f_d << 1 / T_SRS)
```

**典型 SRS 周期** = 5 ms / 10 ms / 20 ms。

| 场景 | f_d | T_c (1/f_d) | T_SRS=10ms 适用? |
|------|-----|-------------|------------------|
| 静止室内 | ~5 Hz | 200 ms | ✅ T_c/T_SRS = 20,LSL 完美 |
| 行人 (3 km/h, 3.5 GHz) | ~10 Hz | 100 ms | ✅ 比 10:1,LSL 有效 |
| 车速 (60 km/h, 3.5 GHz) | ~200 Hz | 5 ms | ⚠️ T_c < T_SRS,LSL 反而引入误差 |
| 高速 (300 km/h, 3.5 GHz) | ~1 kHz | 1 ms | ❌ LSL 完全失效 |

**Sionna 数字孪生当前场景**:retrospective 没说,**需要 D0 时确认**。
- 如果场景静态(数字孪生固定 layout),T_c → ∞,LSL 完美
- 如果场景含 mobility,需要查 Sionna config 拿到 f_d

**应对**:
1. D0 时用 Sionna config + 已 dump 的 channel realization 算 empirical T_c
2. 如果 T_c < 5 × T_SRS,**LSL 的 λ 必须设小**(0.5–0.7,快遗忘);不要用经典 0.97
3. 如果 T_c < T_SRS,**LSL 路线放弃**,改做 frequency-only 的方案(回到 retrospective Section 6 EWMA-在-频域 的反向思路)

→ **R8 是 EWMA 阶段(D2)就能暴露**:如果 EWMA 在 α=0.95 表现差,但在 α=0.5 表现好,意味着 channel 变化快,直接预示 LSL λ 必须小。

---

## 10. 成功定义(Definition of Done,v2)

> **v2 修订**:具体 dB 数字推迟到 D0 oracle NMSE 后定。下面是结构性 DoD,数字 placeholder 用 X/Y 表示。

### 10.1 D0 后填空的 target

D0 跑完 oracle injection,得到 `oracle_NMSE_p50_at_SNR20` = X dB。然后:
- **理论上限**:X
- **MVP target(EWMA D2)**:max(X + 2, -7 + 1.5) = ?(取 oracle 上限和绝对最小改进的较大者)
- **Stretch target(LSL D5)**:max(X + 1, MVP + 0.5)

### 10.2 最低交付(D6 必须达成)

- [ ] D0 floor 诊断报告 + go/no-go 决策记录
- [ ] D2 EWMA 4 SNR fair sweep 完成
- [ ] EWMA NMSE 改进 ≥ 1 dB(教授 5/7 行 28 "인간적으로 이 정도까지" 的最低门槛)
- [ ] Per-call timing 测量数据存档
- [ ] 一份汇报文档包含 §8.3 模板填好的复杂度+性能两条

### 10.3 期望交付(D5-D7 完成所有项)

- [ ] LSL D5 数据完整,LSL > EWMA ≥ 0.5 dB
- [ ] OAI 整机 runtime 增量 ≤ +5%
- [ ] LRLS state convergence 在 50 SRS slots 内完成
- [ ] R8 Doppler-coherence 实测有据(empirical T_c 可报)

### 10.4 理想交付(stretch only)

- [ ] LSL NMSE p50 在所有 SNR 点 ≥ 2 dB 改进
- [ ] 复杂度增量 OAI 整机 < +2%
- [ ] 收敛后 |κ| 数值健康,能持续跑 1 小时
- [ ] LSL 在 R8 高 Doppler 场景下也保持 EWMA 之上

### 10.5 教授视角的"成功"

按 5/7 行 28-29 的标准,**最低交付就是 acceptable submission**。理想 stretch 是奖励项,**不要为了拿 stretch 多花 3 天**。

---

## 11. 立即开始的 Sub-task(v2:D0 优先于 D1)

> **v2 修订**:在 reset 前先做 floor 诊断。详细实施见配套文档 `D0_FLOOR_DIAGNOSIS_PLAN.md`。

### D0(0.5–1 天):Floor 诊断

按依赖顺序:

1. **🟢 D0.1**(30 min)— 在 `nr_srs_mmse.h` 加 2 个枚举:
   ```c
   NR_SRS_EST_PASSTHRU = 5,   // LS 直接输出, 跳过 freq filt
   NR_SRS_EST_ORACLE   = 6,   // 注入 GT, 测下游 floor
   ```
2. **🔴 D0.2**(2 h) — 改 `nr_ul_channel_estimation.c` dispatcher,加 2 个 case(详见 `D0_FLOOR_DIAGNOSIS_PLAN.md` §3)
3. **🟢 D0.3**(1 h) — 写 `gen_oracle_gt_bin.py`(从 Sionna GT npz 转 binary 索引文件)
4. **🔴 D0.4**(1 h) — Build + 验证两个 case 不 crash
5. **🟢 D0.5**(2 h) — 跑 SNR=20 sweep × 3 算法(legacy / passthru / oracle)
6. **🔴 D0.6**(1 h) — 离线分析,填决策矩阵(§7.1 Decision Tree)
7. **🚦 D0.7 GATE** — go/no-go 决策记录到 `D0_RESULT_YYYYMMDD.md`

### D1(0.5 天):Reset 到 Legacy

(只有 D0.7 通过才执行)

1. **🟢 D1.1**(30 min)— 把 `LATTICE_RLS_FILTERBANK_DESIGN.md` 移到 `archive/`(可选)
2. **🟢 D1.2**(30 min)— 在 retrospective 加 `[REFERENCE]` 头部标签
3. **🔴 D1.3**(1 h)— Reset:`sudo cp` backup `nr_srs_mmse.c.bak.after_cleanupC_snapshot` 回主路径 + 修订 dispatcher
4. **🔴 D1.4**(30 min)— `cd build && sudo ninja nr-softmodem`,验证编译通过
5. **🟢 D1.5**(1 h)— `preflight && SRS_ESTIMATOR=legacy CHANNEL_SEED=42 bash run_q4_snr_sweep_v8.sh` 跑 SNR=20,验证 baseline NMSE ≈ -7 dB

### D2(0.5–1 天):EWMA mandatory checkpoint

(D1 通过才执行)

1. **🔴 D2.1**(2 h)— 在 dispatcher 加 `NR_SRS_EST_EWMA = 7`,实现 fixed-α EWMA(per (rx,tx,sc) 一个 c16 H_prev + α = SRS_EWMA_ALPHA env)
2. **🔴 D2.2**(30 min)— Build
3. **🟢 D2.3**(2 h)— 跑 4 SNR sweep × 3 个 α(0.7 / 0.9 / 0.95)
4. **🚦 D2.4 GATE** — 看 NMSE 改进:
   - ≥ 1.5 dB → 进 D3 LSL
   - 1 ~ 1.5 → 继续 D3 但目标降低
   - < 1 → 跳到 D6,EWMA 当 final submission
   - = 0 → STOP,改方向

---

## 12. Sign-off 区(v2:等待 LIULU 确认后开工)

- [ ] §1 教授原话证据基线(含 v2 行 28-29)— 确认引用准确,无脑补
- [ ] §2.1 Floor 假设修正 — 确认接受 D0 实证诊断,不再假设"时域平均能突破"
- [ ] §3.3 "DMRS 也没 2D" — 确认接受
- [ ] §4 Reset 范围 — 确认边界
- [ ] §5.3 M=2 LSL 数学 — 确认照 Haykin Ch.16
- [ ] §5.3.5 self-prediction 数学含义 — 确认理解 "noise i.i.d. 假设" 的依赖
- [ ] §7 v2 时间表(7-8 天 + 每阶段 hard gate)— 确认
- [ ] §9 R8 Doppler-coherence 风险 — 确认 D0 时一并查 Sionna config 拿 f_d
- [ ] §10 v2 DoD(target dB 推迟到 D0 后定)— 确认
- [ ] §11 D0 → D1 → D2 顺序 — 确认从 D0 开始

---

## 13. 附录 A:与已废弃 design 的对照

| 维度 | LATTICE_RLS_FILTERBANK_DESIGN(已废弃) | SRS_2D_LRLS_MASTERPLAN v1 | SRS_2D_LRLS_MASTERPLAN v2(本文) |
|------|----------|----|----|
| 频域处理 | 新 Filter Bank(N=4 套预制系数) | Legacy filt8/16 不动 | Legacy filt8/16 不动 |
| 时域处理 | "M=1 lattice"(实际是 NLMS) | M=2 LSL | M=2 LSL |
| MMSE 实现 | 离线参数化 + 在线 NLMS 微调 | LSL 在线 adaptive | LSL 在线 adaptive |
| 复杂度估算 | 2-3 ms(乐观) | 0.3-0.8 ms | 0.3-0.8 ms |
| Floor 假设处理 | 没提 | 假设"时域能突破"(错) | **D0 实证诊断** |
| EWMA 角色 | 没提 | R6 fallback | **D2 prerequisite gate** |
| 时间预算 | 5 天 | 10 天 | **7-8 天 + 每阶段 gate** |
| 5/7 行 28-29 | 没提 | 漏引 | **明确符合** |
| R8 Doppler-coherence | 没提 | 没提 | **新增** |

---

## 14. 附录 B:v2 review 反馈来源

v2 修订基于一份独立技术 review,核心反馈:
1. **盲点 1**: -7 dB floor 来源未诊断,plan §2 "时域平均能突破" 与 retrospective §3.3 "fixed-point 主导" 自相矛盾
2. **盲点 2**: 5/7 行 28-29 教授时间限制原话被漏引,plan 10 天违背"너무 오래 보낼 생각은 없어"
3. **盲点 3**: EWMA 应是 prerequisite gate(因为它是 LSL 的 degenerate case),v1 把它当 fallback 是逻辑顺序错

5 个 minor 建议(self-prediction 解释 / single-cell 边界 / per SRS slot / target 推迟 / R8)全部吸收。

---

**完。v2 等 sign-off。**

**配套立即执行文档**:`D0_FLOOR_DIAGNOSIS_PLAN.md`
