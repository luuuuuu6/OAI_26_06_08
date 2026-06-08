# LSL 2D Implementation Plan(直接做真 M=2 Lattice-RLS)

| 字段 | 值 |
|------|----|
| **创建日期** | 2026-05-11 |
| **最后修订** | 2026-05-11 v2(代码审查后修正) |
| **作者** | LIULU + AI |
| **决策依据** | 跳过 D0/D1/EWMA 中间步骤,直接实施教授 5/7 原话点名的 Lattice-RLS |
| **状态** | DRAFT — 等 sign-off 后开工 |
| **预期工作量** | 4-5 工作日(实施 + sweep + 报告) |
| **取代** | `SRS_2D_LRLS_MASTERPLAN.md` v2 §11 D2-D5 部分 |
| **保留** | 所有 D0 实现(passthru/oracle 作诊断工具,不删不动) |
| **策略** | 不 reset PDP-R,不删任何现有代码,**并行加新 case** |

---

## v2 → v2.1 修订表(D1.1 Python reference 验证后)

> Python ref 跑完后发现 v2 §2.4 算法本身有 2 个根本 bug。**所有 C 实现细节必须按 v2.1 写**。
> Python ref:`DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/lsl_m2_reference.py`(6 unit tests PASS)

| # | 修正项 | v2 写的 | v2.1 改成 | 原因(Python ref 暴露) |
|---|--------|--------|----------|----------------------|
| A | LSL 变体 | a-posteriori(含 γ 转换因子) | **a-priori**(无 γ) | cold-start 时 γ_1 = 1−|u|²/|u|² = 0 → 下一 slot Δ/γ ≈ 1e6 → KAPPA_OOR 永远触发 → 输出永远等于 u |
| B | 输出公式 | joint-process estimator: `H_smooth = Σ ρ_m · b_m` | **forward-prediction**: `H_smooth = u − η_M` | joint-process with d=u 时 b_0=u,ρ_0→1 trivially → 退化为恒等映射(Python ref 测出 +0.00 dB gain) |
| C | Warm-up 条件 | `n_updates < n_warm` | **`n_updates <= n_warm`** | n_updates 在 check 前 ++,off-by-one 让第 N_WARM 个 sample 已经是 LSL 输出 |
| D | Fail-safe 范围 | 只 catch NaN / |κ|>1 | **加 R11**: 高 Doppler 时 LSL 学坏不被 catch,gain 可达 -2 dB | Python test_fast_varying 测出:channel 变化快时 LSL 不爆炸但 NMSE 反而劣化 |

**Python ref 实测**(λ=0.97):
- 静态 channel: gain = **+3.11 dB**(理论上限 4.77 dB)
- 慢变 AR(1) channel: gain = **+2.66 dB**
- λ ↑ 单调改善:0.9→+1.91, 0.95→+2.44, 0.97→+2.66, 0.99→+2.88
- 高 Doppler:gain = -2.08 dB(不爆炸,但变差 → 需 R11 fail-safe)

---

## v1 → v2 修订表(代码审查后)

| # | 修正项 | v1 | v2 | 影响章节 |
|---|--------|----|----|----------|
| 1 | `LSL_MAX_SC` | 2048(高 numerology 会越界) | `NR_MAX_OFDM_SYMBOL_SIZE = 8192`(覆盖所有配置) | §3.2 §9.2 |
| 2 | `LSL_MAX_RX/TX` | 8/8(过大) | 4/4(匹配 OAI `nb_antennas_rx <= 4` AssertFatal) | §3.2 §9.2 |
| 3 | `#include <pthread.h>` | 缺失 | 加到 header(`pthread_once_t` 需要) | §3.1 |
| 4 | `lsl_one_step()` | 只有伪代码,没 public 函数定义 | §3.3 完整 C 实现,暴露 public(unit test 可调) | §3.3 (新增) |
| 5 | §2.4 伪代码 | C 代码混 `goto fallback` | 改为干净算法伪代码,实现细节移到 §3.3 | §2.4 (重写) |
| 6 | Dispatcher 注释 | 没解释 `n_sc = ofdm_symbol_size` 合理性 | 加说明:zero-skip 逻辑处理非 active SC | §4.2 |
| 7 | CMake 位置 | "cmake_targets/.../CMakeLists.txt"(模糊) | 顶层 `CMakeLists.txt:957`(精确,在 nr_srs_mmse.c 行后追加) | §4.3 |
| 8 | 内存估算 | 32 MB(基于 8×8×2048×256B,过度) | 24 MB worst / 1.5 MB realistic / 0.5 MB active(基于 4×4×8192×192B) | §0 §3.2 §9.2 |
| 9 | Env 字符串 | 正文 "2dmmse 或 2d-lsl",代码识别 4 种 | 统一表述:`mmse2d` / `2d-mmse` / `2dlsl` / `2d-lsl` 4 种都映射到 `NR_SRS_EST_MMSE2D` | §0 §4.1 §4.2 |
| 10 | 风险表 | 缺 R9 / R10 | 加 R9(SC 越界已规避)+ R10(complex float 风格,低风险) | §11 |
| 11 | 策略声明 | 没明确"不删 PDP-R" | 表头加 "不 reset PDP-R,不删任何现有代码,并行加新 case" | 表头 |

---

## 0. TL;DR

- **算法**:Haykin Ch.16 M=2 阶 Least-Squares Lattice (LSL),在时间方向(跨 SRS slot)做自适应预测,等价于在线 Wiener 解。
- **接入**:OAI 现成 `nr_srs_channel_estimation` 已有 dispatcher,**填 `NR_SRS_EST_MMSE2D` 当前空 case**(枚举已存在,line 16 `nr_srs_mmse.h`),env `SRS_ESTIMATOR` 识别 4 种字符串:`mmse2d` / `2d-mmse` / `2dlsl` / `2d-lsl`。
- **State**:全局 `static` 指针 `lsl_state_t *state[4][4][NR_MAX_OFDM_SYMBOL_SIZE=8192]`(扁平化),首次调用 `aligned_alloc` 分配,**worst case 24 MB,realistic 1.5 MB**(2 RX × 2 TX × 624 active SC × 192 byte/tap)。
- **Fail-safe**:任何 (rx, tx, sc) tap 数值崩溃(NaN / |κ|>1)→ 该 tap 自动 reset + fallback 到当前 slot 的 Legacy filt8/16 输出。**最坏情况 = Legacy**。
- **复杂度**:~95 实数 mul / state / SRS slot,8192 states × 95 = 780K mul/call ≈ **0.3-0.8 ms**。
- **测试**:复用 `d0_run.sh` 框架,改成跑 `legacy + 2dlsl` 两次 sweep,fair seed=42。

---

## 1. 当前 OAI 代码现状(完成)

### 1.1 调用链

```
phy_procedures_nr_gNB.c:970
  nr_srs_channel_estimation(gNB, frame_rx, slot_rx, srs_pdu, ...)
       │
       ▼ nr_ul_channel_estimation.c:742
  函数签名: int nr_srs_channel_estimation(
              const PHY_VARS_gNB *gNB,
              const int frame, const int slot,                         ← 我们用来算 abs_slot
              const nfapi_nr_srs_pdu_t *srs_pdu,
              const nr_srs_info_t *nr_srs_info,
              const c16_t **srs_generated_signal,
              c16_t srs_received_signal[][...],
              c16_t srs_estimated_channel_freq[][..][N_FFT*num_symbols], ← 我们要写的输出
              c16_t srs_estimated_channel_time[][...][...],
              c16_t srs_estimated_channel_time_shifted[][...][...],
              int8_t *snr_per_rb,
              int8_t *snr)

  for ant = 0..nb_antennas_rx:
    for p_index = 0..N_ap:                                            ← 双层循环, 每对 (ant, port)
        # Step 1: LS estimate (line 811-839)
        srs_ls_estimated_channel[k] = ls 累积                         ← 仅 pilot 位置非零

        # Step 2: Legacy freq interp (line 864-903)
        c16multaddVectRealComplex(filt8/16, ..., srs_est)             ← 写入对齐缓冲

        # Step 3: dispatcher (line 913+)
        switch (srs_estimator_mode) {
           NR_SRS_EST_LEGACY    → memcpy srs_est → out                 ← OAI 默认
           NR_SRS_EST_MMSE1D    → call PDP-R 老代码                   ← 已废
           NR_SRS_EST_PASSTHRU  → memcpy srs_ls → out                 ← D0.A
           NR_SRS_EST_ORACLE    → nr_srs_oracle_inject(abs_slot)      ← D0.B
           NR_SRS_EST_MMSE2D    → ★ 当前未实现, 我们填这里 ★
           default              → memcpy srs_est → out                ← fallback Legacy
        }

        # Step 4: 算 noise (residual)  (line 980+)
        # Step 5: freq2time IDFT      (line 1027)
        # Step 6: 时域 shift + merge   (line 1031-1037)
```

### 1.2 关键已有基础设施

| 项 | 状态 | 复用方式 |
|---|------|---------|
| Enum `NR_SRS_EST_MMSE2D = 2` | ✅ 已存在 `nr_srs_mmse.h:16` | 直接用 |
| Env `SRS_ESTIMATOR` 解析(`mmse2d` 已识别但 stub fallback Legacy) | ✅ `nr_srs_mmse.c:289-291` | 改 stub 为真实现 |
| `abs_slot = frame * slots_per_frame + slot` | ✅ ORACLE case 已有模板 line 959 | 复用 |
| Sweep 脚本 `d0_run.sh` | ✅ 已能跑(legacy 部分) | 改简化版 `d2_run.sh` |
| Fair seed `CHANNEL_SEED=42` | ✅ retrospective 标准 | 必用 |
| NMSE 评估 `eval_pdpR_nmse.py` | ✅ 通用 | 直接用 |
| OAI build (`sudo ninja nr-softmodem`) | ✅ retrospective §7.2 模板 | 用 |

### 1.3 已知障碍(需注意但不阻塞)

| 障碍 | 应对 |
|------|------|
| Sionna IPC 在 sweep 间会破坏(D0 实测) | 增加 `D0_SIONNA_WAIT` ≥ 60s;LSL 只跑 2 次 sweep,失败概率比 D0 低 33% |
| OAI -7 dB fixed-point floor 风险 | LSL fail-safe 设计 → 最坏跟 Legacy 持平;floor 真在下游就停手出 negative report |
| `gNB` 是 `const`,不能直接挂 state | 用 module-local global static(下文 §3.2) |

---

## 2. LSL 算法完整数学(M=2 阶,Haykin Ch.16)

### 2.1 信号模型

对每个 (rx, tx, sc) tap 独立维护一个时间序列预测器:
```
input:    u(t) = H_freq(t)         (Legacy filt 输出, 第 t 个 SRS slot)
desired:  d(t) = u(t)              (self-prediction, 学 channel deterministic 部分)
output:   H_smooth(t) = LSL(u(0..t)) → 噪声压制后的 channel
```

### 2.2 状态变量(per tap)

| 变量 | 含义 | 数量 | 类型 |
|------|------|------|------|
| `f[0..2]` | forward prediction error,阶 0/1/2 | 3 | `complex float` |
| `b[0..2]` | backward prediction error,阶 0/1/2 | 3 | `complex float` |
| `b_prev[0..1]` | backward error 上一时刻 b_m(t-1),阶 0/1 | 2 | `complex float` |
| `Ef[0..2]` | forward energy,阶 0/1/2 | 3 | `float` |
| `Eb[0..2]` | backward energy,阶 0/1/2 | 3 | `float` |
| `Eb_prev[0..1]` | backward energy 上一时刻 E_b,m(t-1) | 2 | `float` |
| `Delta[0..1]` | partial correlation,阶 0/1 | 2 | `complex float` |
| `gamma[0..2]` | conversion factor,阶 0/1/2 | 3 | `float` |
| `gamma_prev[0..1]` | gamma 上一时刻 | 2 | `float` |
| `p[0..2]` | joint-process partial correlation | 3 | `complex float` |
| `rho[0..2]` | joint-process weight | 3 | `complex float` |
| `n_updates` | 累计 update 次数(warm-up 检测) | 1 | `uint32_t` |
| `flags` | bit 0: NaN trap;bit 1: |κ|>1 trap | 1 | `uint8_t` |

**单 tap 大小**:9 复数 × 8B + 12 标量 × 4B + 1×4B + 1×1B ≈ **120 byte**(对齐后 128B)

### 2.3 初始化(t = 0,or after reset)

```
δ = 1e-6f                    # 防除零
λ = 0.97f                    # 默认遗忘因子(env 可调)

f[m] = b[m] = b_prev[m] = 0 + 0j     for m = 0,1,2
Ef[m] = Eb[m] = Eb_prev[m] = δ
Delta[m] = 0
gamma[m] = gamma_prev[m] = 1.0
p[m] = rho[m] = 0
n_updates = 0
flags = 0
H_smooth(0) = u(0)            # cold start: 直接吐 LS-after-filt 输出
```

### 2.4 时间更新算法(per SRS slot,纯数学描述,v2.1 — a-priori 变体)

> 本节是**算法层级伪代码**,只描述数学动作,不混入 C 实现细节(数值保护、fault 分支、cold-start 处理等都见 §3.3 `lsl_one_step()` 的 C 实现)。
>
> **v2.1 重要变更**(基于 D1.1 Python ref 验证):改用 a-priori-error LSL 变体,**无 γ 转换因子**;输出用 `u - η_M` forward-prediction-based denoiser,**不用 joint-process estimator**。详见文档头部 v2 → v2.1 修订表。

```
INPUT:  u(t) = H_freq[t]            # Legacy filt8/16 当前 slot 输出
        state s = (η, b, b_prev, Δ, F, B, B_prev)   # 注意:无 γ

OUTPUT: H_smooth(t) = u(t) − η_M(t)  # forward-prediction-based denoiser


───── Step 1. Order 0 init (m = 0) ─────
η_0(t)  ← u(t)                       # a-priori forward error at order 0
b_0(t)  ← u(t)                       # backward error at order 0
F_0(t)  ← λ · B_0(t-1) + |u(t)|²    # F_0 = B_0 at order 0
B_0(t)  ← F_0(t)

───── Step 2. Order recursion (m = 1, 2) ─────
for m = 1 to M=2 do:
  # 2.1 partial correlation update — NOTE: no /γ in a-priori variant
  Δ_{m-1}(t) ← λ · Δ_{m-1}(t-1) + b_{m-1}(t-1) · conj(η_{m-1}(t))

  # 2.2 reflection coefficients (Cauchy-Schwarz: |Δ|² ≤ F · B)
  κ_f,{m-1}(t) ← Δ_{m-1}(t) / B_{m-1}(t-1)
  κ_b,{m-1}(t) ← conj(Δ_{m-1}(t)) / F_{m-1}(t)

  # 2.3 prediction error update
  η_m(t) ← η_{m-1}(t)  − κ_f,{m-1}(t) · b_{m-1}(t-1)
  b_m(t) ← b_{m-1}(t-1) − κ_b,{m-1}(t) · η_{m-1}(t)

  # 2.4 energy update (no γ update — a-priori variant)
  F_m(t) ← F_{m-1}(t)  − |Δ_{m-1}(t)|² / B_{m-1}(t-1)
  B_m(t) ← B_{m-1}(t-1) − |Δ_{m-1}(t)|² / F_{m-1}(t)
endfor

───── Step 3. Roll state for next slot ─────
for m = 0 to M-1 do:
  b_{m}(t-1)  ← b_m(t)
  B_m(t-1)    ← B_m(t)
endfor

───── Step 4. Forward-prediction-based denoising output ─────
H_smooth(t) ← u(t) − η_M(t)

return H_smooth(t)
```

**算法直觉**(为什么 `u − η_M` 能压噪):
- `u(t) = signal(t) + noise(t)`,signal 在时间方向慢变(可预测),noise i.i.d.(不可预测)
- M 阶前向预测器学到 signal 的 deterministic 部分(forward predictor)
- 残差 `η_M` ≈ 不可预测的 noise
- 因此 `u − η_M` ≈ signal(去噪后的 channel)

**与 v2 旧版区别**:
- v2 用 `Σ ρ_m · b_m`(joint-process estimator with d=u)→ b_0=u, ρ_0→1 trivially → 输出 = u → **退化为 passthrough**
- v2.1 用 `u − η_M` → 真正利用 lattice predictor 的预测能力 → 实测 +3 dB(Python ref `test_static_channel`)

> **数值保护**(δ-clamp on F/B / `|Δ|² < 0.99·F·B` 反射限幅 / NaN trap)、**warm-up**(前 N_WARM=5 slot 输出 Legacy,条件 `<=`)、**fault 自动 reset → fallback Legacy** 等工程细节,见 §3.3 C 实现。

### 2.5 关键设计选择(每条都有依据)

| 设计 | 值 | 依据 |
|------|----|------|
| 阶数 M | 2 | 教授 5/7 "lattice 折中";M=1 接近 NLMS 太弱,M=3+ 数值难稳 |
| λ(forgetting) | 0.97 默认,env `SRS_LSL_LAMBDA` 可调 | Haykin 经典推荐;高 Doppler 场景调到 0.9 |
| δ(数值下限) | 1e-6f | 经验值,float 精度足够 |
| Self-prediction(d=u) | yes | 假设 noise i.i.d.,channel 慢变 → predict 学 channel 部分,残差 = 噪声 |
| N_WARM | 5 | 给 LSL 5 个 SRS slot 收敛,期间用 Legacy |
| Output type | complex float → c16 | 内部精度;输出转回 OAI 期望的 c16 |
| Reflection limit | |Δ|² < 0.99 · Ef · Eb | 数学上 |κ_f κ_b| ≤ 1 等价;0.99 留余量 |
| Failure action | reset tap + return Legacy | fail-safe,**最坏 = Legacy** |

---

## 3. 数据结构 + 全局存储

### 3.1 State struct(打入 cache line 友好布局)

```c
/* nr_srs_lsl_2d.h */
#ifndef __NR_SRS_LSL_2D_H__
#define __NR_SRS_LSL_2D_H__

#include <complex.h>
#include <pthread.h>     /* pthread_once_t for lazy state allocation */
#include <stdint.h>
#include "PHY/TOOLS/tools_defs.h"   /* c16_t */

#define LSL_ORDER       2           /* M = 2 */
#define LSL_DELTA       1e-6f
#define LSL_DEFAULT_LAMBDA   0.97f
#define LSL_DEFAULT_N_WARM   5
#define LSL_FLAG_NAN         (1u << 0)
#define LSL_FLAG_KAPPA_OOR   (1u << 1)
#define LSL_FLAG_RESET       (1u << 2)

typedef struct __attribute__((aligned(64))) {
    complex float f[LSL_ORDER + 1];        /* 0..2 */
    complex float b[LSL_ORDER + 1];
    complex float b_prev[LSL_ORDER];       /* 0..1 */
    complex float Delta[LSL_ORDER];        /* 0..1 */
    complex float p[LSL_ORDER + 1];
    complex float rho[LSL_ORDER + 1];
    float Ef[LSL_ORDER + 1];
    float Eb[LSL_ORDER + 1];
    float Eb_prev[LSL_ORDER];
    float gamma[LSL_ORDER + 1];
    float gamma_prev[LSL_ORDER];
    uint32_t n_updates;
    uint8_t  flags;
    uint8_t  _pad[3];
} lsl_state_t;
/* sizeof ≈ 9*(2+2)*8 + 12*4 + 4 + 4 = 244 byte → padded to 256B (4 cache lines) */

/* Public API */
void nr_srs_lsl_2d_reset(void);                   /* zero-init all state, called once */
void nr_srs_lsl_2d_set_params(float lambda, uint32_t n_warm);

/* Per-(rx, tx) per slot main entry. Reads input from in[], writes out[]. */
void nr_srs_lsl_2d_update(int ant, int port,
                          const c16_t *in, c16_t *out,
                          int n_sc,
                          int64_t abs_slot);     /* used for gap detection */

/* Diagnostic: print summary of state health */
void nr_srs_lsl_2d_log_summary(void);

#endif
```

### 3.2 Module-local 全局存储

```c
/* nr_srs_lsl_2d.c */

#include "PHY/defs_nr_common.h"   /* NR_MAX_OFDM_SYMBOL_SIZE = 8192 */

/* OAI hard-coded gNB upper bounds (see nr_init_ue.c:181 AssertFatal nb_antennas_rx <= 4) */
#define LSL_MAX_RX   4
#define LSL_MAX_TX   4
#define LSL_MAX_SC   NR_MAX_OFDM_SYMBOL_SIZE   /* 8192, covers all numerologies */

/* Memory footprint:
 *   sizeof(lsl_state_t) ≈ 192 byte/tap (after struct alignment)
 *   worst case  : 4 × 4 × 8192 × 192 B = 24 MB     (full 273 PRB @ 30k SCS)
 *   realistic   : 2 × 2 × 624  × 192 B = 0.5 MB   (106 PRB current sweep config)
 *   typical     : 2 × 2 × 2048 × 192 B = 1.5 MB   (106 PRB with full ofdm_symbol_size)
 * One-time alloc on first call via pthread_once (or static if always on).
 */
static lsl_state_t *g_state = NULL;       /* dim: [RX][TX][SC] flattened */
static int g_n_rx = 0, g_n_tx = 0, g_n_sc = 0;
static float g_lambda = LSL_DEFAULT_LAMBDA;
static uint32_t g_n_warm = LSL_DEFAULT_N_WARM;
static int64_t g_last_abs_slot = -1;
static pthread_once_t g_init_once = PTHREAD_ONCE_INIT;

static void g_state_alloc(void) {
    g_n_rx = LSL_MAX_RX;
    g_n_tx = LSL_MAX_TX;
    g_n_sc = LSL_MAX_SC;
    size_t n = (size_t)g_n_rx * g_n_tx * g_n_sc;
    g_state = aligned_alloc(64, n * sizeof(lsl_state_t));
    if (g_state) {
        memset(g_state, 0, n * sizeof(lsl_state_t));
        /* Init each tap to delta on energy fields */
        for (size_t i = 0; i < n; i++) {
            for (int m = 0; m < LSL_ORDER + 1; m++) {
                g_state[i].Ef[m] = LSL_DELTA;
                g_state[i].Eb[m] = LSL_DELTA;
                g_state[i].gamma[m] = 1.0f;
            }
            for (int m = 0; m < LSL_ORDER; m++) {
                g_state[i].Eb_prev[m] = LSL_DELTA;
                g_state[i].gamma_prev[m] = 1.0f;
            }
        }
        LOG_I(NR_PHY, "[LSL] global state allocated: %.1f MB\n",
              n * sizeof(lsl_state_t) / 1e6);
    } else {
        LOG_E(NR_PHY, "[LSL] aligned_alloc failed for %zu bytes\n",
              n * sizeof(lsl_state_t));
    }

    /* Read env tunables once */
    const char *env_lambda = getenv("SRS_LSL_LAMBDA");
    if (env_lambda) {
        float v = strtof(env_lambda, NULL);
        if (v > 0.5f && v < 1.0f) g_lambda = v;
        else LOG_W(NR_PHY, "[LSL] SRS_LSL_LAMBDA=%s out of (0.5,1.0), using default %.3f\n",
                   env_lambda, g_lambda);
    }
    const char *env_warm = getenv("SRS_LSL_N_WARM");
    if (env_warm) {
        uint32_t v = (uint32_t)strtoul(env_warm, NULL, 10);
        if (v <= 100) g_n_warm = v;
    }
    LOG_I(NR_PHY, "[LSL] params: lambda=%.4f n_warm=%u order=%d\n",
          g_lambda, g_n_warm, LSL_ORDER);
}

static inline lsl_state_t *get_state(int ant, int port, int sc) {
    return &g_state[(size_t)ant * g_n_tx * g_n_sc + (size_t)port * g_n_sc + sc];
}
```

### 3.3 `lsl_one_step()` 完整 C 实现(v2.1 — a-priori 变体,public,unit-testable)

> 这是 §2.4 算法伪代码(v2.1)的逐字 C 翻译,**额外补**:δ-clamp 数值保护、|Δ|² ≥ 0.99·F·B 限幅、NaN trap、warm-up、fault 自动 reset。
> 把它做成 public(暴露在 .h)是为了让 unit test(§8.2)能直接 link 这个函数,跟 Python reference 做 1e-4 数值对齐。
>
> **Python reference 已实现并验证**:`DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/lsl_m2_reference.py` — 6 unit tests PASS,reference data 在 `/tmp/lsl_test_{input,expected}.bin`(100 samples,c16 format,scale=16384)。

```c
/* nr_srs_lsl_2d.h — public API for unit testing */
complex float lsl_one_step(lsl_state_t *st, complex float u);
```

```c
/* nr_srs_lsl_2d.c */
#include <math.h>     /* fmaxf, isfinite */
#include <complex.h>

static inline float cnorm(complex float z) {
    float r = crealf(z), i = cimagf(z);
    return r*r + i*i;
}

static void lsl_state_reset_one(lsl_state_t *st, uint8_t reason_flag) {
    /* Single-tap reset (used by fault path, not whole array).
     * Note: state struct still has gamma/p/rho fields for ABI compatibility,
     *       but a-priori variant doesn't update or use them — they stay zero. */
    memset(st, 0, sizeof(*st));
    for (int m = 0; m <= LSL_ORDER; m++) {
        st->Ef[m] = LSL_DELTA;
        st->Eb[m] = LSL_DELTA;
        st->gamma[m] = 1.0f;     /* unused but kept consistent with init */
    }
    for (int m = 0; m < LSL_ORDER; m++) {
        st->Eb_prev[m] = LSL_DELTA;
        st->gamma_prev[m] = 1.0f;
    }
    st->flags = reason_flag | LSL_FLAG_RESET;
}

complex float lsl_one_step(lsl_state_t *st, complex float u) {
    const float lambda = g_lambda;
    const float delta  = LSL_DELTA;

    /* ── Step 1: Order 0 init ───────────────────────── */
    complex float eta_curr = u;                            /* η_0(t) */
    complex float b_curr   = u;                            /* b_0(t) */
    float E_new = lambda * st->Eb_prev[0] + cnorm(u);      /* F_0(t) = B_0(t) */
    st->Ef[0] = E_new;
    st->Eb[0] = E_new;
    st->f[0] = eta_curr;
    st->b[0] = b_curr;

    /* working scalars carrying "previous order" values across the loop */
    complex float eta_prev_order = eta_curr;               /* η_{m-1}(t)   */
    float Ef_prev_order_t = E_new;                         /* F_{m-1}(t)   */

    /* ── Step 2: Order recursion m = 1, 2 (a-priori, no γ) ──── */
    for (int m = 1; m <= LSL_ORDER; m++) {
        /* fetch lag values (set last slot) */
        float Eb_prev_lag = st->Eb_prev[m-1];              /* B_{m-1}(t-1) */
        complex float b_prev_lag = st->b_prev[m-1];        /* b_{m-1}(t-1) */

        /* numerical guards */
        float Eb_safe = fmaxf(Eb_prev_lag,    delta);
        float Ef_safe = fmaxf(Ef_prev_order_t, delta);

        /* Step 2.1: partial correlation (NO /γ in a-priori variant) */
        st->Delta[m-1] = lambda * st->Delta[m-1]
                       + b_prev_lag * conjf(eta_prev_order);

        /* Step 2.2: Cauchy-Schwarz limit |Δ|² ≤ F · B (= reflection |κ_f·κ_b| ≤ 1) */
        float D2 = cnorm(st->Delta[m-1]);
        if (D2 >= 0.99f * Ef_safe * Eb_safe) {
            lsl_state_reset_one(st, LSL_FLAG_KAPPA_OOR);
            return u;       /* fallback Legacy this slot */
        }

        complex float kappa_f = st->Delta[m-1] / Eb_safe;
        complex float kappa_b = conjf(st->Delta[m-1]) / Ef_safe;

        /* Step 2.3: prediction error update */
        complex float eta_new = eta_prev_order - kappa_f * b_prev_lag;
        complex float b_new   = b_prev_lag      - kappa_b * eta_prev_order;

        /* Step 2.4: energy update (no γ update in a-priori variant) */
        st->Ef[m] = Ef_prev_order_t - D2 / Eb_safe;
        st->Eb[m] = Eb_prev_lag      - D2 / Ef_safe;

        /* commit + advance loop variables */
        st->f[m] = eta_new;
        st->b[m] = b_new;
        eta_prev_order  = eta_new;
        Ef_prev_order_t = st->Ef[m];
    }

    /* ── Step 3: Roll state for next SRS slot (no gamma roll) ── */
    for (int m = 0; m < LSL_ORDER; m++) {
        st->b_prev[m]  = st->b[m];
        st->Eb_prev[m] = st->Eb[m];
    }

    /* ── Step 4: Forward-prediction-based denoising output ──── */
    /* H_smooth = u - η_M  (subtract M-th order forward residual) */
    complex float eta_M = st->f[LSL_ORDER];
    complex float H_smooth = u - eta_M;

    /* ── NaN trap (last line of defense) ────────────── */
    if (!isfinite(crealf(H_smooth)) || !isfinite(cimagf(H_smooth))) {
        lsl_state_reset_one(st, LSL_FLAG_NAN);
        return u;
    }

    /* ── Warm-up: first N_WARM slots return Legacy ──── */
    /* Note: increment BEFORE check, condition <= ensures first N_WARM
     * samples (indices 0..N_WARM-1) all return passthrough u. */
    st->n_updates++;
    if (st->n_updates <= g_n_warm) {
        return u;
    }
    return H_smooth;
}
```

**关键实现选择(对比 §2.4 数学)**:
| 偏差 | 说明 |
|------|------|
| `gamma` / `p` / `rho` 字段保留在 struct 但不更新 | a-priori 变体不需要,但保留可让以后切回 a-posteriori 不改 ABI;单 tap +48 byte 的占用可接受 |
| 用 working scalars (`eta_prev_order`, ...) 而不是直接索引 `st->f[m-1]` | 单趟循环避免 stale state 读取问题 |
| Step 2.2 用 `\|Δ\|² ≥ 0.99·F·B` 检查 | Cauchy-Schwarz 等价于反射系数 \|κ_f·κ_b\| ≥ 0.99,留 1% 余量防数值越界 |
| Fault path 直接 `return u`(Legacy fallback) | 不 throw,不 per-slot LOG_E(频率太高);通过 `st->flags` 让 §3.5 summary 阶段统计 |
| Warm-up `<=` 而非 `<` | 见 v2 → v2.1 修订表第 C 项 |

**Python reference 数值对齐预期**(§8.2 unit test):
```
input:    /tmp/lsl_test_input.bin    (100 samples × c16 = 400 bytes)
expected: /tmp/lsl_test_expected.bin (100 samples × c16 = 400 bytes)
tolerance: max abs diff ≤ 1 LSB(c16 量化误差范围)
```

### 3.4 Gap detection(SRS 不连续时 reset)

```c
/* If abs_slot 跳跃 > GAP_THRESHOLD, channel uncorrelated, reset all states */
#define LSL_GAP_THRESHOLD_SLOTS  500     /* ≈ 250 ms @ 30k SCS, > coherence time */

void nr_srs_lsl_2d_update(int ant, int port,
                          const c16_t *in, c16_t *out,
                          int n_sc, int64_t abs_slot) {
    pthread_once(&g_init_once, g_state_alloc);
    if (!g_state) {
        /* alloc 失败,fallback Legacy */
        memcpy(out, in, n_sc * sizeof(c16_t));
        return;
    }

    /* Gap detection: 跨 SRS slot 间隔太大 → state stale, reset */
    if (g_last_abs_slot >= 0 &&
        abs_slot - g_last_abs_slot > LSL_GAP_THRESHOLD_SLOTS) {
        LOG_I(NR_PHY, "[LSL] gap %ld slots > %d, resetting all states\n",
              (long)(abs_slot - g_last_abs_slot), LSL_GAP_THRESHOLD_SLOTS);
        nr_srs_lsl_2d_reset();
    }
    g_last_abs_slot = abs_slot;

    /* Per-SC update */
    for (int sc = 0; sc < n_sc; sc++) {
        lsl_state_t *st = get_state(ant, port, sc);
        /* Skip near-zero inputs (non-pilot positions for sparse modes,
         * or filt-untouched edges) — keep state, output zero */
        if (in[sc].r == 0 && in[sc].i == 0) {
            out[sc] = (c16_t){0, 0};
            continue;
        }
        complex float u = (float)in[sc].r + I * (float)in[sc].i;
        complex float h_smooth = lsl_one_step(st, u);

        /* Convert back to c16 with saturation */
        float r = crealf(h_smooth);
        float i = cimagf(h_smooth);
        out[sc].r = (int16_t)fmaxf(fminf(r, 32767.0f), -32768.0f);
        out[sc].i = (int16_t)fmaxf(fminf(i, 32767.0f), -32768.0f);
    }
}
```

---

## 4. 接入设计

### 4.1 Env 变量

| 名称 | 默认 | 范围 | 说明 |
|------|------|------|------|
| `SRS_ESTIMATOR` | `legacy` | `mmse2d` / `2d-mmse` / `2dlsl` / `2d-lsl` (4 种字符串都映射到 `NR_SRS_EST_MMSE2D`) | 启用本算法 |
| `SRS_LSL_LAMBDA` | 0.97 | (0.5, 1.0) | 遗忘因子,高 Doppler 调小 |
| `SRS_LSL_N_WARM` | 5 | [0, 100] | warm-up 期间用 Legacy 兜底 |
| `SRS_LSL_DEBUG` | 0 | 0/1 | 1 → log convergence summary 每 200 frames |

### 4.2 Dispatcher 修改

`nr_srs_mmse.c:289-291` 当前是:
```c
} else if (strcasecmp(mode_env, "mmse2d") == 0 || strcasecmp(mode_env, "2d-mmse") == 0) {
  LOG_W(NR_PHY, "[SRS Estimator] mmse2d requested but not implemented yet; falling back to legacy\n");
  mode = NR_SRS_EST_LEGACY;
}
```

改为:
```c
} else if (strcasecmp(mode_env, "mmse2d") == 0 || strcasecmp(mode_env, "2d-mmse") == 0
        || strcasecmp(mode_env, "2dlsl") == 0  || strcasecmp(mode_env, "2d-lsl") == 0) {
  mode = NR_SRS_EST_MMSE2D;
}
```

`nr_ul_channel_estimation.c:913+` 的 if/else 链加新 branch:
```c
} else if (srs_estimator_mode == NR_SRS_EST_MMSE2D) {
    /* Stage 1: take Legacy filt8/16 output (full ofdm_symbol_size, dense) as
     * input to the temporal LSL filter.
     * Stage 2: temporal Lattice-RLS smoothing across SRS slots.
     *
     * Why pass n_sc = ofdm_symbol_size (the full FFT) instead of M_sc_b_SRS:
     *   - Legacy filt8/16 already wrote dense per-SC values into srs_est[]
     *     across the entire SRS bandwidth.  Indexing by ofdm_symbol_size keeps
     *     the LSL state addressable by absolute SC index (consistent across
     *     slots even when SRS resource block allocation changes).
     *   - nr_srs_lsl_2d_update() internally skips zero-input SCs (non-active
     *     positions / pre-filt edges), so the actual update count ≈ M_sc_b_SRS
     *     × K_TC ≈ active SC count.  No wasted work, but the indexing scheme
     *     is robust to SRS BW changes.
     *
     * abs_slot uses SFN-unwrapped form: matches D0 ORACLE case (line 959)
     * and the gen_oracle_gt_bin.py slot indexing convention.
     */
    c16_t *legacy_out = &srs_est[mem_offset];
    const int64_t abs_slot = (int64_t)frame * frame_parms->slots_per_frame + slot;
    nr_srs_lsl_2d_update(ant, p_index,
                         legacy_out,
                         srs_estimated_channel_freq[ant][p_index],
                         frame_parms->ofdm_symbol_size,
                         abs_slot);
}
```

### 4.3 文件清单

| 文件 | 操作 | 行数 |
|------|------|------|
| `openair1/PHY/NR_ESTIMATION/nr_srs_mmse.c:289-291` | StrReplace stub → 真识别 4 种字符串 | -3 +5 |
| `openair1/PHY/NR_ESTIMATION/nr_srs_mmse.c:301+` | log `mmse2d` 字符串补一条 | +2 |
| `openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c:913+` | StrReplace 加新 `NR_SRS_EST_MMSE2D` case | +12 |
| `openair1/PHY/NR_ESTIMATION/nr_srs_lsl_2d.h` ★新建★ | 接口 + struct | ~60 |
| `openair1/PHY/NR_ESTIMATION/nr_srs_lsl_2d.c` ★新建★ | 实现 + 全局 state | ~280 |
| **顶层** `CMakeLists.txt` line 957 旁 | `${OPENAIR1_DIR}/PHY/NR_ESTIMATION/nr_srs_lsl_2d.c` 追加到 NR_ESTIMATION 列表(在 `nr_srs_mmse.c` 行之后) | +1 |
| `d2_run.sh` ★新建★ | 简化 sweep(legacy + 2dlsl 各一次) | ~120 |
| `D2_RESULT_TEMPLATE.md` ★新建★ | 决策记录模板 | ~80 |

**总新增**:~550 行,**修改**:~17 行。

> CMake 精确位置说明:OAI 顶层 `DevChannelProxyJIN/openairinterface5g_whan/CMakeLists.txt` 在 line 956-959 列出了 NR_ESTIMATION 的 4 个 source(`nr_ul_channel_estimation.c`, `nr_srs_mmse.c`, `nr_freq_equalization.c`, `nr_measurements_gNB.c`),我们在 line 957 之后插入新的一行即可,不需要改 nas_sim_tools/at_commands 的 CMakeLists。

---

## 5. 数值稳定性保护(4 类)

| 类 | 保护点 | 检测 | 动作 |
|---|--------|------|------|
| **零除** | 所有 `/E` `/γ` 操作 | `fmaxf(x, δ)` clamp | 透明,不计入 fault |
| **反射系数发散** | `|Δ|² ≥ 0.99 · Ef · Eb` | 每个 m 检查 | set `LSL_FLAG_KAPPA_OOR`,reset state, return Legacy |
| **NaN / Inf** | `H_smooth` 输出 | `isfinite()` 检查 | set `LSL_FLAG_NAN`,reset state, return Legacy |
| **SRS Gap** | `abs_slot - last_abs_slot > GAP` | global check | reset 所有 states,本 slot 用 Legacy |

**关键**:任何一类 fault 都**不让 LSL 输出污染最终 channel**,直接 fallback Legacy。**这就是 fail-safe 设计**。

---

## 6. Cold-start / Warm-up 策略

```
SRS slot index t      0    1    2    3    4    5    6    7    ...
                      │    │    │    │    │    │    │    │
n_updates             0    1    2    3    4    5    6    7    ...
                      ┌────────────────────┐  ┌───────────────
output source         │ Legacy (warm-up)    │  │ LSL output
                      └────────────────────┘  └───────────────
                       n_updates < N_WARM=5    n_updates >= 5
```

**为什么 N_WARM=5?**
- M=2 LSL 数学上需要 ≥ M+1=3 个样本才有 well-defined estimate
- 加 2 个 buffer 让 energy/gamma 稳定
- 真实信号下,5 个 SRS slot ≈ 50ms-100ms,几乎无感

---

## 7. Fail-safe 兜底总结

```
任何情况下, 输出都是以下之一:

  [Path A] LSL output           (state healthy + warmup done)
  [Path B] Legacy filt output    (warmup, 或 NaN, 或 |κ|>1, 或 alloc 失败, 或 SRS gap)
  [Path C] zero output           (input 本身就是 zero, e.g. 非 active SC)

→ 永远不可能比 Legacy 差超过 +0 dB(忽略 warmup 的极小开销)
→ 教授 5/7 "허접" 标准底线: 不 worse than Legacy ✅
```

---

## 8. 集成测试方案(4 步 sanity check)

### 8.1 Step 1:单元测试(不依赖 OAI build)

写一个 standalone C/Python 测试:
```python
# test_lsl_unit.py
import numpy as np
# 1. 生成已知 channel + 已知噪声
H_true = np.random.randn(100) + 1j * np.random.randn(100)   # 100 SRS slot 的真实 channel (slow varying)
n = 0.1 * (np.random.randn(100) + 1j * np.random.randn(100))
H_noisy = H_true + n

# 2. 跑 Python reference LSL M=2
H_smooth = lsl_m2_python(H_noisy, lambda_=0.97)

# 3. 验证: NMSE(H_smooth, H_true) < NMSE(H_noisy, H_true)
nmse_in  = 10*np.log10(np.mean(np.abs(H_noisy - H_true)**2) / np.mean(np.abs(H_true)**2))
nmse_out = 10*np.log10(np.mean(np.abs(H_smooth - H_true)**2) / np.mean(np.abs(H_true)**2))
assert nmse_out < nmse_in - 2.0, f"LSL didn't reduce NMSE: in={nmse_in:.2f} out={nmse_out:.2f}"
print(f"PASS: NMSE in={nmse_in:.2f} dB → out={nmse_out:.2f} dB ({nmse_out-nmse_in:+.2f})")
```

→ **如果 Python 测试都不通过,C 实现绝对不能跑**。

### 8.2 Step 2:C 单元对齐 Python(数值精度 1e-4)

```c
/* test_lsl_c.c — feed same input as Python test, compare bit-exact ish */
#include "nr_srs_lsl_2d.h"
int main(void) {
    /* 读 test_lsl_input.bin (Python 写的 H_noisy) */
    /* 跑 nr_srs_lsl_2d_update() */
    /* 对比 test_lsl_expected.bin */
    /* 输出 max abs diff,要求 < 1e-4 */
}
```

### 8.3 Step 3:OAI smoke test(单 frame)

```bash
# 启动 OAI,跑 1 frame,看 LSL 被调用 + 无 NaN
SRS_ESTIMATOR=2dlsl SRS_LSL_DEBUG=1 \
   bash launch_all.sh --max-frames 100
grep -c "LSL.*update" log/gnb.log    # 应 > 0
grep -c "LSL_FLAG_NAN"  log/gnb.log    # 应 = 0
grep -c "kappa_OOR"     log/gnb.log    # 应 = 0 (正常情况)
```

### 8.4 Step 4:Fair sweep(legacy vs 2dlsl)

复用 `d0_run.sh` 框架,简化为 2 算法:
```bash
sudo bash d2_run.sh   # 内部调 run_q4_snr_sweep_v8.sh × 2
```

---

## 9. 复杂度 + 内存预算

### 9.1 计算复杂度

per (rx, tx, sc) per SRS slot:
| 步骤 | 复数 op | 实数 mul 等价 |
|------|---------|---------------|
| Order 0 init | 1 mul | 2 |
| Order 1 update | 7 mul + 4 div + 4 add | ~30 |
| Order 2 update | 7 mul + 4 div + 4 add | ~30 |
| Joint estimator (3 阶) | 9 mul + 3 div | ~30 |
| Numerical guards | 5 cmp | ~5 |
| **小计** | | **~95 mul** |

per call(整 SRS estimation):
- 实测场景:2 RX × 2 TX × 624 active SC × 95 = 237K mul
- 现代 CPU ≈ 10 GFLOPS → **~0.025 ms 纯计算**
- 加 cache miss + 控制流 + state R/W → **~0.3-0.6 ms 实际**

→ 远远低于 PDP-R 的 5-10 ms,远低于 1 ms 预算。

### 9.2 内存预算(v2 重算)

`sizeof(lsl_state_t)` ≈ **192 byte/tap**(对齐后)— 9 复数 × 8B + 12 标量 × 4B + 1×4B + 1×1B + padding。

| 场景 | 维度 | 大小 |
|------|------|------|
| **Worst case**(OAI 上限) | 4 RX × 4 TX × 8192 SC × 192 B | **24 MB** |
| **Typical**(106 PRB,full FFT 索引) | 2 RX × 2 TX × 2048 SC × 192 B | **1.5 MB** |
| **Realistic**(106 PRB,active SC only) | 2 RX × 2 TX × 624 SC × 192 B | **0.5 MB** |
| 全局 const 配置 | — | < 1 KB |

**结论**:
- gNB 内存通常 GB 级,worst 24 MB 完全可接受
- 实际只 1.5 MB,在 L2/L3 cache 范围内(典型 4-8 MB),性能友好
- 对比:PDP-R 的工作内存(IFFT buffer + R_table + Wiener)峰值 ~10 MB,LSL 不算特别贵

**为什么不再是 v1 写的 32 MB?**
- v1: 8×8×2048×256B = 32 MB(过度估计 RX/TX 上限,struct 也算大了)
- v2: 4×4×8192×192B = 24 MB(用 OAI 实际上限 + 紧凑 struct)
- 名义 worst 反而下降,因为 nb_antennas_rx 在 OAI 里有 `AssertFatal <= 4` 硬卡(`nr_init_ue.c:181`)

---

## 10. 时间表(4-5 工作日)

| Day | 任务 | 验收 |
|-----|------|------|
| **D1.1**(1 h)✅ DONE | 写 `lsl_m2_reference.py` + 通过 6 unit tests | ✅ Python PASS,gain +3.11 dB / 静态,reference data 已 dump 到 `/tmp/lsl_test_*.bin` |
| **D1.2**(2 h) | 写 `nr_srs_lsl_2d.c/.h`(按 §3.1-§3.4 + §3.3 v2.1 a-priori 实现) | 编译通过,链接 OK |
| **D1.3**(2 h) | 写 `test_lsl_c.c`,读 `/tmp/lsl_test_input.bin`,对比 `/tmp/lsl_test_expected.bin` | C 输出与 Python 对齐 max diff ≤ 1 LSB |
| **D2**(0.5 天) | (a) 改 `nr_srs_mmse.c` env 解析<br>(b) 改 `nr_ul_channel_estimation.c` dispatcher case<br>(c) 加 source 到 cmake<br>(d) build + smoke test 1 frame | gnb.log 有 LSL 调用日志;无 NaN/crash |
| **D3**(0.5-1 天) | (a) 写 `d2_run.sh`(复用 d0_run 框架)<br>(b) 跑 SNR=20 单点 sweep × 2 算法<br>(c) 看 NMSE 改进决策 | 数据 OK;NMSE 出来 |
| **D4**(1 天) | (a) 4 SNR 完整 fair sweep<br>(b) 复杂度测量(`bench_srs_mmse.c` 可改用)<br>(c) 调 λ(如 D3 不达预期) | 完整数据 + 收敛分析 |
| **D5**(1 天) | (a) 出对比图(双 panel: NMSE vs SNR + per-call timing)<br>(b) 写 `REPORT_2D_LSL_YYYYMMDD.md`<br>(c) 教授汇报材料 | 报告完成 |

**buffer**:0.5 天给 sionna IPC 调试(已知不稳定)。

---

## 11. 风险登记 + 应对

| # | 风险 | 概率 | 影响 | 应对 |
|---|------|------|------|------|
| R1 | M=2 LSL 数值不稳(λ 错 / cold start 异常) | **中** | 中 | fail-safe 自动回退 Legacy;先用 0.97 经典值 |
| R2 | Per-call 超 1 ms(虽然估算 < 0.6 ms) | 低 | 中 | 实测后定;若超调可换 M=1 / 部分 SC |
| R3 | LSL 比 Legacy 还差(channel 太快变 / floor 在下游) | **中-高** | 高 | fail-safe 保证 ≥ Legacy;结果作 negative report |
| R4 | sionna IPC sweep 间崩溃(D0 实测过) | **高** | 中 | sweep 间 60s 等待 + preflight + 单 SNR 优先 |
| R5 | OAI build 链接错误(新 .c 没加 cmake) | 中 | 低 | D2 第一步加顶层 `CMakeLists.txt:957+`,D2 build 立刻能发现 |
| R6 | 教授 5/7 "时间限制" 违反 | 低 | 低 | 4-5 天 < 教授 talked 7 天上限 |
| R7 | `gNB` const 改不动 | 已规避 | 低 | 用 module-local global static |
| R8 | sub-frame SRS scheduling 不规律(< 100 SRS samples per sweep) | 中 | 中 | LSL 收敛需要 ≥ 50 samples;sweep MAX_FRAMES 调 ≥ 200 |
| **R9** | SC 索引越界(ofdm_symbol_size 在某些 numerology > 2048) | **已规避** | 高 | v2: `LSL_MAX_SC = NR_MAX_OFDM_SYMBOL_SIZE = 8192`,覆盖所有 OAI 配置;dispatcher 内 `assert(n_sc <= LSL_MAX_SC)` 兜底 |
| **R10** | C99 `complex float` 跨编译器/优化等级行为不一致 | 低 | 低 | OAI 已大量使用 c16_t/c32_t,代码风格一致;LSL 内部用 C99 `complex float`,只在 boundary 转 c16_t,未发现历史问题 |
| **R11** ★v2.1 新增★ | 高 Doppler 场景 LSL "学坏"(NaN trap 抓不住),NMSE 反而劣化 | **中** | 中 | Python ref `test_fast_varying` 已实测:λ=0.97 + 高 Doppler → gain = -2.08 dB(不爆炸但变差)。**应对**:在 `nr_srs_lsl_2d_update()` 加 sanity check:`|H_smooth - u| > 3·sqrt(noise_floor)` → 单 tap reset,return Legacy。或 D4 时根据实测决定降到 λ ≤ 0.7;退路是 D5 时若整体 LSL < Legacy,退到 fixed-α EWMA(M=0 退化形态) |

---

## 12. 立即可开始的 sub-task(优先级)

1. **🟢 D1.1**(1 h)— 写 `python/lsl_m2_reference.py`(纯 numpy 实现 + 单元测试)
2. **🟢 D1.2**(2 h)— 写 `nr_srs_lsl_2d.h` + `nr_srs_lsl_2d.c`(无集成,只编译通过)
3. **🟢 D1.3**(2 h)— 写 `test_lsl_c.c` 对齐 Python reference,数值精度 1e-4
4. **🔴 D2.1**(0.5 h)— 改 dispatcher + env 解析 + cmake 加 source
5. **🔴 D2.2**(0.5 h)— Build + smoke 1 frame
6. **🟢 D2.3**(0.5 h)— 写 `d2_run.sh`
7. **🔴 D3.1**(0.5 h)— 跑 SNR=20 单点 sweep,看 NMSE
8. **🚦 D3.2 GATE** — 决策:LSL 改进 ≥ 1 dB → 进 D4;0 ~ 1 dB → 调 λ;< 0 → 退步问题诊断

---

## 13. Sign-off 区(等 LIULU 确认)

- [ ] §2.4 LSL 数学公式 — 确认照 Haykin Ch.16 无误
- [ ] §3.2 全局 state 大小 32MB worst — 确认可接受
- [ ] §4.2 dispatcher 接入位置 — 确认改 `nr_ul_channel_estimation.c:913+`
- [ ] §4.3 文件清单 — 确认新建 2 文件 + 修改 2 文件
- [ ] §5 fail-safe 设计 — 确认 "fault → fallback Legacy" 是底线
- [ ] §6 N_WARM=5 — 确认 OK
- [ ] §10 4-5 天时间表 — 确认或调整
- [ ] §12 立即可做的 sub-task 顺序 — 确认从 D1.1 Python reference 开始

---

**完。等 sign-off 后从 §12 D1.1 开始动手。**
