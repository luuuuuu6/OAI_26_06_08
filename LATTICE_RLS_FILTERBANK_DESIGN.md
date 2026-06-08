# [ARCHIVED — 已废弃,2026-05-11]

> **本草案已被 `SRS_2D_LRLS_MASTERPLAN.md` 取代,不再执行。**
>
> 废弃原因:
> 1. 与 `PDP_R_RETROSPECTIVE_20260511.md` Section 6 的 "频域保 Legacy" 决策冲突
> 2. M=1 lattice 简化版实际写的是 NLMS,不是 lattice
> 3. Filter Bank 仍是频域 1D MMSE,跟 PDP-R 撞同一个 floor
> 4. 与教授 4/29 原话 "기본적으로 2D 필터링" + 5/7 "레티스 기반 RLS" 的真实组合不符
>
> 历史保留供回顾,**不要按本文档实施**。

---

# SRS Estimator V3 设计:Filter Bank + Lattice-RLS

**日期**:2026-05-11
**目标**:实现教授 4/29 + 5/7 推荐的 2D filtering 架构(complexity negligible + 性能 gap vs LS)
**前提**:PDP-R 路径已 sidelined(详见 `PDP_R_RETROSPECTIVE_20260511.md`)

---

## 1. 架构总览

```
                ┌────────────────────────────────────────────┐
                │  SRS slot t  →  LS estimate H_LS(t, f)     │  ← 已有(OAI native)
                └────────────────────────────────────────────┘
                                    ↓
            ┌───────────────────────────────────────────────────┐
            │  ① 粗估当前 PDP shape(从 LS IFFT 取)             │
            │     输出:τ_rms(or coherence_bw)→ filter index │
            └───────────────────────────────────────────────────┘
                                    ↓
            ┌───────────────────────────────────────────────────┐
            │  ② Filter Bank(频率方向,offline 预制)            │
            │     选 1 of N 套 MMSE 系数 → 频域 smoothing       │
            │     输出:H_FB(t, f)                              │
            └───────────────────────────────────────────────────┘
                                    ↓
            ┌───────────────────────────────────────────────────┐
            │  ③ Lattice-RLS(时间方向,跨 SRS slot 自适应)     │
            │     state 跨 slot 持久化,在线更新                │
            │     输出:H_smooth(t, f)                          │
            └───────────────────────────────────────────────────┘
                                    ↓
                              送给 caller
```

教授原话映射:
- ② 对应 "PDP나 도플러 정도의 단계별로 해서 적당한 필터를 만들어 놓고 그 필터를 그냥 선택적으로 적용"
- ③ 对应 "레티스 기반 RND" 显式点名

---

## 2. 模块化(env 切换 + 文件分层)

### 新增 env var

```bash
SRS_ALG=legacy   # OAI native filt8/16(default)
SRS_ALG=pdpR     # 我们的 complex MMSE(已实现,sidelined)
SRS_ALG=fb       # Filter Bank only(只 ② 不 ③)
SRS_ALG=fbrls    # Filter Bank + Lattice-RLS(完整方案)
```

### 文件结构

| 文件 | 角色 | 状态 |
|---|---|---|
| `nr_srs_mmse.c` | 主入口 + PDP-R + dispatcher | 已有 |
| `nr_srs_filterbank.c` ★ 新建 | ① PDP 粗估 + ② Filter Bank | 待写 |
| `nr_srs_filterbank.h` ★ 新建 | 接口 + 预制系数表 | 待写 |
| `nr_srs_lattice_rls.c` ★ 新建 | ③ Lattice RLS 实现 | 待写 |
| `nr_srs_lattice_rls.h` ★ 新建 | 接口 | 待写 |
| `nr_srs_mmse.h` | 加 alg mode enum + getter 声明 | 改 |
| `nr_ul_channel_estimation.c` caller | 不动 | — |

---

## 3. 详细设计

### 3.1 Filter Bank 设计

**离线**(写在 `.h` 里 hardcoded):
- N=4 套预制 MMSE 系数,对应 4 种典型 PDP:
  - **FB0**: Short delay (τ_rms < 100 ns) — 室内 LOS,系数集中
  - **FB1**: Medium delay (100 ns ≤ τ_rms < 500 ns) — 室外 NLOS
  - **FB2**: Long delay (500 ns ≤ τ_rms < 1500 ns) — 城市散射
  - **FB3**: Very long delay (≥ 1500 ns) — 远距离反射
- 每套系数 = 4×4 复数矩阵(window=4 pilots),按经典 OFDM-MMSE 公式离线算好

**在线**:
- 估当前 τ_rms(从 LS IFFT 算 PDP,跟之前 PDP-R 类似但更轻量)
- 查表选 FB 索引
- 用选中系数做频域 weighted sum(per-target SC,~16 复数 MAC)

**复杂度**:
- PDP 估计:1 次 IFFT(2048 点,跟 PDP-R 一样)+ 简单 τ_rms 计算
- 查表:O(1)
- 频域 smoothing:per-target SC 16 复数 MAC = 256 ops
- 整体 per-call:~1-2 ms(比 PDP-R 5-10 ms 快 5×,比 Legacy 0.1 ms 慢 10×)

### 3.2 Lattice-RLS 设计

**核心 idea**:对每个 (rx, tx, sc) 维护一个跨 SRS slot 的 LRLS predictor。

**简化版**(M=1 阶 lattice,等价于 adaptive EWMA):

```
对每个 (rx, tx, sc):
  state: H_prev[rx][tx][sc],  α[rx][tx][sc],  forget_factor λ ≈ 0.95

每个新 SRS slot:
  e = H_FB(t) - α * H_prev      ← 预测误差
  α_new = α + step_size * e * conj(H_prev) / (|H_prev|^2 + ε)   ← 系数更新
  H_smooth(t) = α_new * H_prev + (1-α_new) * H_FB(t)            ← 平滑输出
  H_prev ← H_smooth(t)
  α ← α_new
```

**完整版**(M=2 阶 lattice,Haykin Ch 16):
- 维护 forward/backward prediction errors f_m(n), b_m(n)
- 反射系数 κ_f, κ_b 用 LSL update
- 内存:per (rx, tx, sc) ~16 bytes,总共 ~128 KB(2 ant × 2 ant × 2048 sc × 16 byte)

**复杂度**:
- per (rx, tx, sc):~10 复数 ops(M=1)or ~30 ops(M=2)
- 整体 per-call:8192 (rx*tx*sc) × 30 ops ≈ 250K ops ≈ 0.5-1 ms

**首次启动**:state 初始化为 H_FB(t=0),α=0(纯当前 slot,不 smoothing)。**warm-up 后逐渐收敛**。

### 3.3 复杂度预算总结

| 算法 | per-call 估计 | vs Legacy |
|---|---|---|
| Legacy | 0.1 ms | 1× |
| **Filter Bank only** (`fb`) | **~1.5 ms** | ~15× |
| **Filter Bank + Lattice-RLS** (`fbrls`) | **~2-3 ms** | ~25× |
| (参考)PDP-R | 5-10 ms | 50-100× |

**关键判断**:`fbrls` 的 ~2-3 ms **可能仍超过 OAI 实时预算**(0.5 ms)。如果 sweep 看到 SRS dropped,就需要简化:
- M=1 lattice(不要 M=2)
- 或者只在每 N 个 SRS slot 跑一次 LRLS

---

## 4. 评估方案(完全复用)

| 工具 | 用途 | 状态 |
|---|---|---|
| `preflight.sh --inside-sweep` | sweep 间清环境 | ✅ 已有 |
| `run_q4_snr_sweep_v8.sh` | 跑 sweep | ✅ 已有 |
| `eval_pdpR_nmse.py` | NMSE per SNR | ✅ 已有 |
| `plot_nmse_vs_snr_v2.py` | 双图对比 | ✅ 已有(改一下能支持 4 算法对比) |
| `bench_srs_mmse.c` | 复杂度测量 | ✅ 已有(待用) |

**测试流程**(同 seed=42 跑 4 次):

```bash
preflight && \
  CHANNEL_SEED=42 SRS_ALG=legacy ... bash run_q4_snr_sweep_v8.sh
preflight && \
  CHANNEL_SEED=42 SRS_ALG=fb     ... bash run_q4_snr_sweep_v8.sh
preflight && \
  CHANNEL_SEED=42 SRS_ALG=fbrls  ... bash run_q4_snr_sweep_v8.sh
preflight && \
  CHANNEL_SEED=42 SRS_ALG=pdpR   ... bash run_q4_snr_sweep_v8.sh   # 对照(已有数据)

# 出 4 算法对比图
python3 plot_nmse_vs_snr_v3.py --legacy ... --fb ... --fbrls ... --pdpR ...
```

**预期数据交付**:

| 算法 | NMSE p50 (SNR=20) | per-call ms | OAI total Δ% | 推荐? |
|---|---|---|---|---|
| Legacy | -7 | 0.1 | 0% | baseline |
| FB | -8 ~ -10 (期望) | 1.5 | +1% | ✓ middle |
| **FB+LRLS** | **-10 ~ -13 (期望)** | 2-3 | +2% | ✓✓ 主推 |
| PDP-R | -2 ~ -4(实测) | 5-10 | +5% | ✗ |

---

## 5. 实施时间表

| 天 | 任务 | 产出 |
|---|---|---|
| **D1**(今天) | 设计 doc(本文)+ Filter Bank 系数离线生成(Python script) | ~5 套 MMSE 系数表 |
| **D2** | 实现 `nr_srs_filterbank.c/h`,集成到 nr_srs_mmse.c | `SRS_ALG=fb` 能跑 |
| **D3** | 实现 `nr_srs_lattice_rls.c/h`(M=1 起),集成 | `SRS_ALG=fbrls` 能跑 |
| **D4** | 跑 4 算法 sweep + 复杂度测量 + 出图 | 完整对比数据 |
| **D5** | 写报告 (slides + 数据表)| 给教授的最终交付 |

---

## 6. 关键风险 + 应对

| 风险 | 应对 |
|---|---|
| FB 系数离线生成需要 channel model 假设 | 用 3GPP 38.901 标准 PDP(EPA/EVA/ETU)对应 4 套系数 |
| LRLS 数值不稳(λ 选错) | 先用 λ=0.95 经典值,跑通后再 tune |
| 复杂度仍超预算 | 简化:LRLS 只对 partial subcarriers 跑 |
| FB index 选择 mis-classification | 加 hysteresis(连续 N 帧同一 class 才切) |

---

## 7. 立刻开始的 3 个 sub-task(优先级排序)

1. **🟢 (a)** 用 Python + 3GPP TDL channel model 离线算 4 套 MMSE 系数,塞到 `nr_srs_filterbank.h` 里
2. **🟡 (b)** 写 `nr_srs_lattice_rls.c` 算法骨架(M=1 简化版,先跑通)
3. **🟢 (c)** 改 `plot_nmse_vs_snr_v2.py` 支持 4 算法 overlay

**推荐顺序**:(a) → (b) → (c)。
(a) 给我们 concrete data input,(b) 是 main work,(c) 是 last-mile。

---

**等你 sign-off 后,从 (a) 开始动手**。
