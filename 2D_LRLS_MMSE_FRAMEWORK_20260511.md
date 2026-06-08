# 2D-MMSE SRS 信道估计框架计划(Lattice-RLS 自适应)

**日期**:2026-05-11
**作者**:LIULU
**状态**:Fresh Start(取代 `LATTICE_RLS_FILTERBANK_DESIGN.md` 和 `PDP_R_RETROSPECTIVE` 路线)
**核心目标**:实现教授 4/29 + 5/7 双重要求 — **2D MMSE filtering**,**MMSE 系数用 Lattice-RLS 在线自适应学出**

---

## 0. 上下文与教授原话锚定

### 0.1 教授要求的逻辑链

```
最终目标:2D MMSE filtering(4/29 段 A2 原话)
   ↓ MMSE 系数 W = (R_HH + σ²I)⁻¹ R_Hp  怎么定?
   │
   ├─ 路径1:知道 R_HH, R_Hp 参数 → 直接套公式  ❌ 我们不知道 channel statistics
   ├─ 路径2:参数化预制 filter bank             ⚠ 上次 design 误读为主路线
   └─ 路径3:Adaptive filtering(4/29 段 A4)   ✅ 5/7 展开为 Lattice-RLS
        ├─ LMS              ❌ 性能不行
        ├─ Standard RLS     ❌ 太复杂(O(W²) per sample)
        └─ Lattice-RLS      ✅ 5/7 教授显式点名(O(W) per sample,稳定)
```

→ **Lattice-RLS 不是替代 2D MMSE,而是用来"在线学出" 2D MMSE 系数的方法**

### 0.2 核心证据(教授原话精确引用)

| 编号 | 出处 | 原话(韩) | 翻译 |
|------|------|----------|------|
| **A2** | `listening_0429:219-221` | "기본적으로 2D 필터링을 해주면 노이즈가 서프레션이 돼. 그리고 그 2D 필터링의 가장 기본은 mmse 방식이야" | 基本上做 2D filtering 就能 suppress 噪声;2D filtering 最基础的就是 MMSE |
| **A4** | `listening_0429:222` | "어댑티브 필터링 채널 익스포페이션 방식으로 그걸 보통 정하거나" | 系数用 adaptive filtering 方式定 |
| **A5** | `listening_0429:225-227` | "DMRS 쪽에 보면 2D 필터링을 어떻게 하는지 확인을 해보고 그러면 SRS 쪽에도 동일한 2D 필터링을 SRS 모양에 맞춰서 한 다음에" | 看 DMRS 怎么做 2D filtering,SRS 照样做(按 SRS 自己的 pattern) |
| **B1** | `listening0507:14-15` | "레티스 기반 RND스야 그런 것들이 이미 기존들이 많이 있다고" | Lattice-based RLS 已经有很多现成方案 |
| **B2** | `listening0507:17` | "본인이 하는 게 채널 에스티메이션을 잘하려는 게 목적이 아니잖아" | 你的目的不是做完美信道估计 |
| **B3** | `listening0507:24` | "복잡도 때문에 늘어난 건 얼마큼 무시할 수 있는데 성능은 얼마큼 좋아졌다" | 汇报模板:复杂度增量可忽略 + 性能改进多少 |

---

## 1. 总体架构

### 1.1 数据流(per SRS slot)

```
┌──────────────────────────────────────────────────────────────────┐
│  Step 0: OAI native LS estimation                                │
│    输入:接收 SRS pilot symbols                                   │
│    输出:H_LS(t, f) for all (rx, tx, sc)                          │
│    状态:已存在(OAI 默认 SRS estimation 路径)                  │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│  Step 1: 2D 滑窗组装(per target SC)                            │
│    对每个 target (rx, tx, sc₀):                                  │
│      - 频域取 Wf=4 个最近 pilot SC: f₀-3, f₀-1, f₀+1, f₀+3       │
│      - 时域取 Wt=4 个过去 SRS slot: t₀-3, t₀-2, t₀-1, t₀         │
│      - 组装:u(n) = vec(H_LS_window) ∈ C^16                       │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│  Step 2: 2D-Lattice-RLS Joint-Process Estimator                  │
│    state per (rx, tx, sc₀):                                       │
│      - forward errors f_m(n), m=1..M                              │
│      - backward errors b_m(n), m=1..M                             │
│      - reflection coeffs κ_f,m, κ_b,m                             │
│      - joint-process coeffs ρ_m                                   │
│      - backward error energy β_m                                   │
│    update:LSL Joint-Process(Haykin "Adaptive Filter Theory" Ch.16)│
│    output:H_smooth(t₀, f₀)                                       │
└──────────────────────────────────────────────────────────────────┘
                              ↓
                      caller(下游 PUSCH/SRS 应用)
```

### 1.2 教授指令到架构的映射

| 教授指令 | 架构对应 |
|----------|----------|
| A2: "2D 필터링" | Step 1 的 Wt × Wf 二维窗口 |
| A2: "MMSE 가장 기본" | Step 2 的 joint-process estimator(等价于 MMSE 的递推求解) |
| A4: "어댑티브 필터링" | Step 2 整体是 adaptive |
| A5: "DMRS 동일한 2D 필터링" | Wt × Wf 窗口形态从 OAI DMRS 实现照搬 |
| B1: "Lattice 기반 RLS" | Step 2 用 LSL(Least-Squares Lattice)算法 |

---

## 2. Reset 范围(完全重新开始的边界)

### 2.1 删除/回退(Phase 0 D1 完成)

| 文件 | 当前 | 操作 | 目标 |
|------|------|------|------|
| `nr_srs_mmse.c` | 779 行(PDP-R 实现) | **回退** | `nr_srs_mmse.c.bak.after_cleanupC_snapshot`(633 行) |
| `nr_srs_mmse.h` | 41 行 | **回退** | `.bak.after_cleanupC_snapshot` 对应版本 |
| `nr_ul_channel_estimation.c` | PDP-R caller | **回退** | `.bak.after_cleanupC_snapshot` 版本 |
| `LATTICE_RLS_FILTERBANK_DESIGN.md` | 草稿 | **作废**(加 `[ARCHIVED]` 前缀) | 历史记录用 |
| `PDP_R_RETROSPECTIVE_20260511.md` | 总结 | **归档**(加 `[ARCHIVED]` 前缀) | 历史教训 |
| `1d,2d-mmse_change_log.md` 等 | 过程 | **归档** | 历史 |

### 2.2 保留(经验证有用的基础设施)

| 文件 | 价值 | 操作 |
|------|------|------|
| `preflight.sh` (+ `--inside-sweep`) | sweep 框架修复 | 保留 |
| `run_q4_snr_sweep_v8.sh` | inter-point preflight 集成 | 保留(改一行 SRS_ALG) |
| `eval_pdpR_nmse.py` | 通用 NMSE 评估器 | **改名 `eval_srs_nmse.py`** |
| `plot_nmse_vs_snr_v2.py` | 多算法对比 | 保留(下次升级到 v3 支持 4 算法) |
| `bench_srs_mmse.c` | 复杂度测量 | 保留 |
| `CHANNEL_SEED=42` 模板 | fair comparison | 强制坚持 |
| `nr_srs_mmse.c.bak*` 系列 | 历史快照 | 保留(不删) |

### 2.3 新建文件

| 文件 | 角色 |
|------|------|
| `nr_srs_2d_mmse.c` ★ | 主入口 + dispatcher(env-driven 算法选择) |
| `nr_srs_2d_mmse.h` ★ | 接口 + alg enum |
| `nr_srs_2d_window.c` ★ | Step 1:2D 滑窗组装 + 时域 history buffer |
| `nr_srs_2d_window.h` ★ | 接口 |
| `nr_srs_lattice_rls.c` ★ | Step 2:LSL Joint-Process M=2 |
| `nr_srs_lattice_rls.h` ★ | 接口 |
| `unit_test_lattice_rls.c` ★ | 离线收敛测试(对随机 AR 信号验证) |
| `2D_LRLS_MMSE_FRAMEWORK_20260511.md` | 本文 |
| `DMRS_2D_FILTER_NOTES.md`(D2 产出) | DMRS 调研结果 |

---

## 3. 算法详细设计

### 3.1 2D 窗口几何(Step 1)

**SRS pilot pattern**(假设 OAI 默认 K_TC=4 comb):
- 频域:每 K_TC=4 个 SC 一个 pilot
- 时域:每 SRS_period 个 slot 一次

**窗口选择**:
- Wf = 4(频域 pilot 数,跨 ±2 个 pilot 周期 = ±8 SC)
- Wt = 4(时域 SRS slot 数,过去 4 帧)
- W = Wf × Wt = 16(LRLS input vector 维度)

**target SC 在窗口内的位置**:中心(t₀, f₀ 最近 pilot)

**时域 history buffer**:
- per (rx, tx, sc₀):**ring buffer 长度 Wt**
- 每个 SRS slot 在 caller 入口 push 当前 H_LS,pop 最旧
- 内存:2 ant × 2 ant × 2048 sc × 4 slot × 8 byte(complex16)= **256 KB**

### 3.2 Lattice-RLS Joint-Process Estimator(Step 2)

**算法来源**:Haykin "Adaptive Filter Theory" 4th ed., Chapter 16, "Least-Squares Lattice Filters"
**变体**:joint-process estimation(求解 desired signal 而非纯预测)

**state per (rx, tx, sc₀)**:

```c
typedef struct {
    complex_float f[M+1];        // forward prediction errors
    complex_float b[M+1];        // backward prediction errors
    complex_float b_prev[M+1];   // b(n-1) for recursion
    float beta[M+1];             // backward error energy(累积)
    float alpha[M+1];            // forward error energy
    complex_float kappa_f[M];    // forward reflection coeffs
    complex_float kappa_b[M];    // backward reflection coeffs
    complex_float rho[M];        // joint-process coeffs(MMSE 输出权重)
    float gamma[M+1];            // angle parameter(用于增益归一化)
    int initialized;             // first-call flag
} lrls_state_t;
```

**M=2 时,per (rx, tx, sc₀) state ≈ 80 byte**
**总 state: 2×2×2048 × 80 ≈ 640 KB**(可接受)

**LSL Joint-Process update(per new sample u(n) + desired d(n))**:

```
// 输入:
//   u(n) = LS estimate at target position(标量)
//   d(n) = target estimate from window(用 Step 1 的 Wf 频域加权和作为 desired)

// 阶 0 初始化:
f[0] = u(n);    b[0] = u(n);    gamma[0] = 1.0;

// 对每阶 m = 1..M:
for m in 1..M:
    // 反射系数自适应(Burg-style 估计):
    delta_m = lambda * delta_m_prev + (b_prev[m-1] * conj(f[m-1])) / gamma[m-1]
    kappa_f[m] = delta_m / beta[m-1]
    kappa_b[m] = delta_m / alpha[m-1]
    
    // 前向后向误差更新:
    f[m] = f[m-1] - kappa_f[m] * b_prev[m-1]
    b[m] = b_prev[m-1] - kappa_b[m] * f[m-1]
    
    // 能量累积:
    alpha[m] = alpha[m-1] - |delta_m|² / beta[m-1]
    beta[m]  = beta[m-1] - |delta_m|² / alpha[m-1]
    
    // gamma update(数值稳定):
    gamma[m] = gamma[m-1] - |b[m-1]|² / beta[m-1]
    
    // joint-process 系数更新:
    e[m] = e[m-1] - rho[m] * b[m]              // 残差
    rho[m] = lambda * rho[m] + (e[m-1] * conj(b[m])) / beta[m]

// 输出:
H_smooth(t, f) = u(n) - e[M]   // = sum_m rho[m] * b[m]

// 滚动:
b_prev[*] <- b[*]
```

**关键参数**:
- M = 2 阶(教授要求 lattice,M=1 退化成 NLMS,失去 lattice 优势)
- λ = 0.95(forgetting factor,标准值)
- 数值保护:`gamma`, `alpha`, `beta` 加 `+ε(=1e-10)`,避免除零

**首次启动**:
- `initialized=0`:第一次直接输出 LS,不更新 state
- 第 2 次起:state 用前 1 次 sample warm-up,M 阶完整跑

### 3.3 与 DMRS 2D filtering 的关系(Phase 1 调研后细化)

教授 A5 直接指令:**先看 OAI DMRS 2D filtering 怎么做**。Phase 1(D2)产出 `DMRS_2D_FILTER_NOTES.md`,需回答:

1. OAI DMRS estimation 在哪个文件?(候选:`nr_pdsch_channel_estimation.c`, `nr_dmrs_estimation.c`)
2. DMRS 2D filtering 是 separable(频域 × 时域分开)还是 joint?
3. Window 形状(Wt × Wf)?
4. 系数是 hardcoded 还是 dynamic?
5. 有没有跨 slot 的 state?

→ 根据答案决定:
- 如果 DMRS 是 **separable**:我们也用 separable 2D-LRLS(频域 fixed FIR + 时域 LRLS),实现量小
- 如果 DMRS 是 **joint**:我们用 joint LRLS,vector input W 维(实现量大但更精确)

**保守 default**:Phase 2 先做 separable 版本,Phase 3 看是否有空间升级到 joint。

---

## 4. 模块化设计(env switch + dispatcher)

### 4.1 环境变量

```bash
# ───────────────────────────────────────────
# 算法选择(D1 后生效)
# ───────────────────────────────────────────
SRS_ALG=legacy       # OAI 默认 LS+filt8/16(baseline)
SRS_ALG=dmrs2d       # Phase 2:Wt×Wf 2D fixed-weight MMSE(借 DMRS 模板)
SRS_ALG=lrls2d       # Phase 3:2D MMSE + Lattice-RLS adaptive(主推方案)

# ───────────────────────────────────────────
# LRLS 调参
# ───────────────────────────────────────────
SRS_LRLS_ORDER=2     # M 阶数(默认 2;1=NLMS 应急退化)
SRS_LRLS_LAMBDA=0.95 # forgetting factor
SRS_LRLS_WT=4        # 时域窗口长度
SRS_LRLS_WF=4        # 频域窗口长度
SRS_LRLS_SKIP=1      # 每 N 个 SRS slot 跑一次 LRLS(=1 表示每帧;=2 减半复杂度)

# ───────────────────────────────────────────
# 调试(可选)
# ───────────────────────────────────────────
SRS_DEBUG_LRLS_DUMP=0  # 1=dump per-frame LRLS state 到 /tmp/lrls_state_*.bin
```

### 4.2 caller 修改(最小侵入)

`nr_ul_channel_estimation.c` 仅改一处:
```c
// before:
nr_srs_mmse_freq_filter(...);

// after:
nr_srs_2d_filter(...);    // dispatcher 内部根据 SRS_ALG 分发
```

### 4.3 退路(每个 phase 都能独立回退)

| 阶段 | 失败时的回退 |
|------|--------------|
| Phase 2(dmrs2d)NMSE 退化 | `SRS_ALG=legacy`(env 切换,无需重 build) |
| Phase 3(lrls2d)数值不稳 | `SRS_LRLS_ORDER=1`(降级 NLMS) |
| Phase 3 复杂度爆 | `SRS_LRLS_SKIP=2` 或 4 |
| 全方案撞 OAI floor | 数据写在报告里,转向 fixed-point pipeline 调研(下一研究主题) |

---

## 5. 实施时间表(8-10 天)

| Day | Phase | 任务 | 产出 |
|-----|-------|------|------|
| **D1** | 0 Reset | • 回退 `nr_srs_mmse.{c,h}` + caller 到 `.bak.after_cleanupC_snapshot`<br>• 归档 PDP-R 文档(加 `[ARCHIVED]`)<br>• 重命名 eval 脚本<br>• Build + 跑一次 Legacy sweep 确认 baseline | • OAI 编译通过<br>• Legacy NMSE 数据(对照基线) |
| **D2** | 1 调研 | • 通读 OAI DMRS 2D filtering 实现<br>• 抽取 window 形状 + 系数公式 | `DMRS_2D_FILTER_NOTES.md` |
| **D3** | 2 框架 | • 写 `nr_srs_2d_mmse.{c,h}` dispatcher<br>• 写 `nr_srs_2d_window.{c,h}`(history buffer + 滑窗)<br>• 集成 `SRS_ALG=dmrs2d`(fixed weights from DMRS template) | `SRS_ALG=dmrs2d` 能 run |
| **D4** | 2 验证 | • Same-seed sweep:legacy vs dmrs2d<br>• 出第一版对比图 | NMSE 对比数据 |
| **D5** | 3 LRLS | • 写 `nr_srs_lattice_rls.{c,h}`(M=2 LSL)<br>• 写 `unit_test_lattice_rls.c`(离线 AR 信号验证收敛) | LRLS 单元测试通过 |
| **D6** | 3 集成 | • LRLS 接到 dispatcher,`SRS_ALG=lrls2d` 跑通<br>• 调 λ / SKIP 参数 | `SRS_ALG=lrls2d` 能 run |
| **D7** | 4 评估 | • 4 算法 sweep:legacy / dmrs2d / lrls2d / pdpR(对照已有数据)<br>• `bench_srs_mmse.c` 测复杂度 | 4 算法 NMSE + 复杂度数据 |
| **D8** | 4 出图 | • 升级 `plot_nmse_vs_snr_v3.py` 支持 4 算法<br>• 出 SNR-NMSE 双图 + 复杂度表 | 最终图 |
| **D9** | 5 报告 | • slides + 数据表(按教授 B3 模板) | 教授交付材料 |
| **D10** | buffer | debug / 重跑 / 答辩准备 | — |

---

## 6. 评估方案(完全复用 + 扩展)

### 6.1 工具链

```bash
# 单次 sweep(每个算法)
preflight && \
CHANNEL_SEED=42 SRS_ALG=<alg> bash run_q4_snr_sweep_v8.sh

# 4 算法 batch
for alg in legacy dmrs2d lrls2d pdpR; do
    preflight && CHANNEL_SEED=42 SRS_ALG=$alg bash run_q4_snr_sweep_v8.sh
done

# NMSE 评估(per run)
python eval_srs_nmse.py --run-dir <dir> --sto-correct per-frame

# 4 算法对比图
python plot_nmse_vs_snr_v3.py \
    --legacy logs/legacy_seed42_<ts> \
    --dmrs2d logs/dmrs2d_seed42_<ts> \
    --lrls2d logs/lrls2d_seed42_<ts> \
    --pdpR logs/pdpR_v2_seed42_<ts>

# 复杂度
SRS_BENCH=1 ./bench_srs_mmse <alg>
```

### 6.2 期望数据矩阵(Phase 4 D7 产出)

| 算法 | NMSE p50 (SNR=10) | NMSE p50 (SNR=20) | per-call ms | OAI total Δ% | 推荐? |
|------|-------------------|-------------------|-------------|--------------|--------|
| Legacy | −7.0 dB(实测基线) | −7.1 dB | 0.1 | 0% | baseline |
| dmrs2d | -10 ~ -12(期望) | −9 ~ −11 | 1.5 | +1% | 中间态 |
| **lrls2d** | **-12 ~ -16(期望)** | **−11 ~ −14** | 3-4 | +2% | **✓✓ 主推** |
| pdpR(对照) | −0.88(实测) | −2.46 | 5-10 | +5% | ✗ 失败案例 |

### 6.3 关键判定逻辑

```
if lrls2d 比 Legacy 提升 ≥ 3 dB AND OAI Δ ≤ 5%:
    → 满足教授要求(B3 模板)→ 可交付
elif lrls2d 比 Legacy 仅提升 < 1 dB:
    → 撞 OAI fixed-point floor 假设成立
    → 报告写明,转向 fixed-point 调研主题
else:
    → 中间情况:数据完整呈现,让教授判断是否继续
```

---

## 7. 复杂度预算 + 应对

### 7.1 复杂度细分(per SRS slot,所有 rx×tx×sc)

| 算法 | 主要操作 | ops 估算 | 估算时间 |
|------|---------|---------|---------|
| Legacy | filt8/16 FIR per SC | 2048×16 = 32K | 0.1 ms |
| **dmrs2d** | Wf×Wt 复数 MAC per SC | 2048×16×2 = 64K | ~0.5-1.5 ms |
| **lrls2d** | dmrs2d + LSL update per (rx,tx,sc) | 8192 × (M=2 阶 × ~30 ops) ≈ 500K | ~2-4 ms |

### 7.2 复杂度风险对策

| 触发条件 | 应对 |
|----------|------|
| `lrls2d` per-call > 3 ms | `SRS_LRLS_SKIP=2`(每 2 帧跑一次,中间用 H_smooth(t-1) 近似) |
| `lrls2d` per-call > 5 ms | `SRS_LRLS_ORDER=1`(降级 NLMS,放弃 lattice 优势) |
| OAI 出现 SRS deadline miss(capture rate < 2%) | 上面两条同时启用 |
| 仍然不行 | 仅对 Wf=2(而非 4)做 LRLS,降低 input 维度 |

---

## 8. 风险表(全周期)

| Phase | 风险 | 概率 | 影响 | 应对 |
|-------|------|------|------|------|
| 0 Reset | `.bak.after_cleanupC_snapshot` 跟当前 caller 接口不兼容 | 低 | 高 | 提前 diff 一遍 caller signature,必要时改 caller 到 633 行版本 |
| 1 调研 | OAI DMRS 没有 2D filtering(只有 1D) | 中 | 中 | 教授 A5 是"如果有就照抄";没有就**自己设计 separable 2D**,用经典 OFDM-MMSE 模板 |
| 2 dmrs2d | DMRS pilot pattern 跟 SRS 不兼容(K_TC, comb) | 中 | 中 | 重设计 window 形状(SRS 频域 stride=4,不是 DMRS 的连续 SC) |
| 3 LRLS | LSL 数值不稳(`gamma`, `alpha`, `beta` 接近 0) | 中 | 高 | 加 ε 保护;先用 λ=0.95;预留 `SRS_LRLS_LAMBDA` env tune |
| 3 LRLS | M=2 实测发散 | 低 | 高 | 单元测试(D5)用已知 AR(2) 信号验证;过 unit test 才集成 |
| 3 LRLS | per-(rx,tx,sc) 8192 个 tap 同时收敛失败(部分 ill-conditioned) | 中 | 中 | 收敛监控:dump `gamma[M]` 直方图;ill-conditioned tap 自动 fallback 到 fixed-weight |
| 4 评估 | lrls2d 跟 Legacy 几乎没差(撞 fixed-point floor) | **中-高** | 中 | retrospective 已识别风险,把数据完整出图,作为"floor 假设证据"交付 |
| 4 评估 | lrls2d 比 dmrs2d 还差(adaptive 反而扰动) | 低 | 中 | dump LRLS state 看是否震荡;调小 step / 增大 λ |
| 全程 | 时间表过紧(retrospective 提示 OAI 集成实际 2-3 天) | 高 | 中 | D10 buffer + 优先保 D7 出关键数据(其他可削) |

---

## 9. 交付物清单

### 9.1 代码

```
DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/
├── nr_srs_mmse.c               (回退到 633 行版本,作为 dispatcher 入口或废弃)
├── nr_srs_2d_mmse.c   ★新建    (主入口 + dispatcher)
├── nr_srs_2d_mmse.h   ★新建
├── nr_srs_2d_window.c ★新建    (Step 1)
├── nr_srs_2d_window.h ★新建
├── nr_srs_lattice_rls.c ★新建  (Step 2,M=2 LSL)
├── nr_srs_lattice_rls.h ★新建
└── nr_ul_channel_estimation.c  (caller 改一行)

DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/
├── unit_test_lattice_rls.c ★新建  (离线 LRLS 收敛测试)
├── eval_srs_nmse.py             (改名自 eval_pdpR_nmse.py)
├── plot_nmse_vs_snr_v3.py ★新建  (4 算法 overlay)
├── bench_srs_mmse.c             (复用,加 lrls2d 支持)
└── run_q4_snr_sweep_v8.sh       (env SRS_ALG 已支持,无需改)
```

### 9.2 文档

```
2D_LRLS_MMSE_FRAMEWORK_20260511.md  (本文)
DMRS_2D_FILTER_NOTES.md             (D2 产出)
report_lrls2d_results_<timestamp>.md (D9 产出,给教授)
[ARCHIVED] PDP_R_RETROSPECTIVE_20260511.md
[ARCHIVED] LATTICE_RLS_FILTERBANK_DESIGN.md
```

### 9.3 数据 + 图

```
logs/<timestamp>/
├── legacy_seed42_<ts>/    (4 SNR 点)
├── dmrs2d_seed42_<ts>/    (4 SNR 点)
├── lrls2d_seed42_<ts>/    (4 SNR 点)
├── pdpR_v2_seed42_<ts>/   (对照,已有)
├── nmse_vs_snr_v3.png     (主对比图,p10/p50/p90 三线 × 4 算法)
├── complexity_table.md    (per-call ms + Δ%)
└── nmse_vs_snr_data.csv   (raw 数据,16 行)
```

### 9.4 教授汇报模板(D9 D交付,严格按 B3 格式)

```markdown
# SRS Channel Estimation Improvement Report

## 1. 选择方案
基于 Lattice-based RLS(教授 5/7 推荐),在 2D(time + frequency)上自适应学习 MMSE 系数。
理由:[一句话 — 复杂度可忽略 + 性能 gap 最大]

## 2. 性能改进
- LS-aligned NMSE p50 vs SNR 曲线(图 1)
- Legacy 在 SNR ∈ [10, 25] dB 区间稳定 -7 dB
- 新方案在同区间 -12 ~ -14 dB
- **Gap: 5-7 dB**(target 3 dB+ ✓)

## 3. 复杂度
- SRS estimation block per-call:Legacy 0.1 ms → 新方案 X ms
- OAI 接收机整体:Legacy total → 新方案 total,Δ ≤ 2%(可忽略 ✓)

## 4. 结论
[复杂度可忽略 + 5-7 dB 性能 gap]
```

---

## 10. 立刻开始的 3 个 Sub-task(优先级排序)

### 🟢 (a) Phase 0 Reset(D1 上午,2 小时)

```bash
cd DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/
sudo cp nr_srs_mmse.c.bak.after_cleanupC_snapshot nr_srs_mmse.c
# 同样回退 nr_srs_mmse.h 和 nr_ul_channel_estimation.c(找对应 bak)
# 重 build:
cd ../../../cmake_targets/ran_build/build && sudo ninja nr-softmodem
# 跑 Legacy baseline:
cd ../../../vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/
preflight && CHANNEL_SEED=42 SRS_ALG=legacy bash run_q4_snr_sweep_v8.sh
```

### 🟢 (b) Phase 1 DMRS 调研(D1 下午 + D2 全天)

要回答 5 个问题(§3.3):
1. OAI DMRS estimation 文件位置?
2. 2D filtering 是 separable 还是 joint?
3. Window Wt × Wf?
4. 系数 hardcoded 还是 dynamic?
5. 有跨 slot state 吗?

产出 `DMRS_2D_FILTER_NOTES.md`。

### 🟡 (c) Phase 2 dmrs2d 框架实现(D3-D4)

按 §3.1 + §3.3 实现 fixed-weight 2D filtering,这是 LRLS 的"骨架"。
LRLS 在 D5+ 把 fixed weights 替换为 LSL update。

---

## 11. 跟上次失败的差异(三大避坑)

| 上次失败原因 | 这次预防 |
|--------------|---------|
| 1D 而非 2D | 架构 §1.1 强制 Wt × Wf 二维窗口 |
| 在线解 R 矩阵(O(W³))复杂度爆 | 改用 LSL Joint-Process(O(W) per sample) |
| 没用 Lattice-RLS | Step 2 直接是 LSL,M=2 起步 |
| 没参考 DMRS 实现 | Phase 1 强制调研(D2 deliverable) |
| Same-channel 比较太晚 | D1 Reset 后立刻跑 baseline,所有比较都 `CHANNEL_SEED=42` |
| 复杂度估算过乐观(实测 50× 估算) | §7.1 用 PDP-R 实测做基准,不再凭"理论应该"估算 |
| 文档跟实现脱节 | 每个 phase 结束 commit + 更新本框架文档 |

---

## 12. 终极判定标准(交付前自查)

- [ ] 教授原话 A2:**有 2D filtering**(Wt × Wf 窗口)
- [ ] 教授原话 A2:**MMSE 形态**(joint-process estimator 等价于 MMSE 递推)
- [ ] 教授原话 A4:**Adaptive coefficients**(LRLS state 跨 slot 自适应)
- [ ] 教授原话 A5:**借鉴 DMRS**(D2 调研 + D3 模板移植)
- [ ] 教授原话 B1:**Lattice-based RLS**(M=2 LSL,不是 NLMS)
- [ ] 教授原话 B2:**不过度追求完美**(M=2 就停,不上 M=4+)
- [ ] 教授原话 B3:**复杂度可忽略 + 性能 gap**(report 双指标)

---

**完。等你 sign-off 后,从 §10(a) Phase 0 Reset 开始动手。**

---

## 文档版本

- v1.0 (2026-05-11):初版,fresh start 取代 PDP-R / FB+LRLS 路线
