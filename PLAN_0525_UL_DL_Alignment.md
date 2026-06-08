# UL-DL 对齐实施计划（稳妥版）— 05-25 修订

| 字段 | 值 |
|------|----|
| **修订日期** | 2026-05-25 |
| **目标** | 将 SRS 上行结构化输出与准秀 DL CSI/CSE-CsiNet 输出在 Digital Twin 数据层保持一致 |
| **原则** | 本周先做稳妥部分：格式规范、统一架构图、Python prototype、验证测试；暂不承诺 OAI C 实时结构输出 |
| **参考文档** | `csi结构.md`, `0524日志.SRS_MMSE_FULL_LOGmd`, `录音26_05_22` |

---

## 0. 修订结论

原计划总体方向正确，但有几处需要收敛：

1. `StructuredChannelOutput` 当前只在 `srs_2d_mmse.py` 中定义，尚未接入 OAI C 实时链路。因此本周修改它应定位为 **Python 结构化输出 prototype**，不能表述为已经完成 OAI 实时存储。
2. C 代码同步结构输出涉及实时复杂度、I/O、DB schema、`extract_every` 和 Minji 存储接口，本周不作为核心承诺，后移。
3. `H_c` 当前是 DFT 截断后再 FFT 回来的 full-band reconstruction，不是真正短向量压缩 payload。后续应新增 `h_taps` 作为真正存储的压缩表示。
4. Doppler 多延迟自相关需要双边归一化，并考虑 SRS 非均匀 cadence，不应只用整数 lag。
5. Spatial covariance 需严谨命名：准秀 DL 文档是 transmit-side covariance；我方 UL 目前计算的是 RX-side covariance，应命名为 `R_rx_inst/R_rx_state` 或更泛化的 `R_spatial_*`。

本修订版只推进稳妥、可解释、低风险的部分。

---

## 1. 教授要求与本周交付边界

### 1.1 教授 05-22 指导的核心

教授要求把刘的 SRS 上行工作作为准秀下行 PMI/CSI 工作的 **uplink version** 来整理：

- 准秀：下行 CSI/PMI → 结构信息 + instantaneous H
- 刘：上行 SRS → 信道估计 → 结构信息 + compressed H
- 两者输出应在 Digital Twin 数据层保持 consistent

教授还明确要求：

- 画 schematic block diagram
- 明确每个 block 的 input / processing / output
- 用 OAI RF simulator 的测试信道先做性能验证
- 后续 CDL/DCL 信道由义焕完成后再对接
- 生成的数据后续要能进入 Minji 的数据库/RAN Twin

### 1.2 本周只承诺的稳妥交付

| 交付 | 内容 | 状态目标 |
|------|------|----------|
| 统一架构图 | DL/UL 对称框图，展示 consistent 输出格式 | 周二前完成 |
| NMSE 性能图 | 现有 EWMA/IBVSS/Kalman OAI sweep 结果整理 | 已有图，必要时微调 |
| 输出格式规范 | 定义 `S_ul + H_ul` 与 `S_dl + H_dl` 如何对齐 | 周二前完成 |
| Python prototype | 扩展 `StructuredChannelOutput`，输出 p/R/d state 等字段 | 周二或周三完成 |
| 验证测试 | 字段 shape、数值范围、压缩/重建一致性 | 周三前完成 |

### 1.3 本周不承诺的内容

| 暂缓项 | 原因 |
|--------|------|
| OAI C 实时结构输出 | 需要 DB schema、I/O 策略、实时开销评估 |
| Fingerprint/session resumption | 属于未来增强，教授当前未要求 |
| Fronthaul quantizer / BitKeepMask | 准秀 DL 特有，本周不需要 |
| 完整 AI 版 SRS codec | 当前路线是信号处理 + 结构化输出，不转成 CsiNet |

---

## 2. 当前项目真实状态

### 2.1 已完成的核心算法

当前 SRS 上行主线已经完成：

- OAI C 端三算法：EWMA / IBVSS / Scalar Kalman
- Python 端算法验证：CDL-C/A/D、P1B、瞬态跟踪
- OAI 三算法 sweep：EWMA / IBVSS / Kalman 对比
- 结构化输出类：`StructuredChannelOutput`

当前 OAI 实测结论：

```text
综合鲁棒性: IBVSS 最稳
高 SNR 理想条件: Kalman 最优
固定 EWMA: 对异常帧/数据质量波动敏感
```

### 2.2 当前 `StructuredChannelOutput` 的真实能力

文件：

```text
DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/srs_2d_mmse.py
```

当前类：

```python
class StructuredChannelOutput:
    def extract(self, H_smooth, snr_db=0.0, rot_angle=0.0):
        ...
        return S, H_c
```

当前输出：

| 字段 | 当前含义 | 问题 |
|------|----------|------|
| `rsrp` | 平均接收功率 | 可保留 |
| `snr_db` | 外部传入 SNR | 可保留 |
| `doppler_hz` | 由 `rot_angle` 差分得到的标量 Doppler | 与准秀 `d_inst` 向量不一致 |
| `speed_ms` | 由 Doppler 换算速度 | 可保留 |
| `pdp` | 单帧 PDP | 缺少 `p_inst/p_state` 区分 |
| `ds_rms_s` | RMS delay spread | 可保留 |
| `R_rx` | RX spatial covariance | 缺少 `R_rx_state` 累积 |
| `sv` | MIMO 奇异值 | 可保留 |
| `H_c` | full-band DFT-denoised reconstruction | 不是真正短向量 compressed payload |

---

## 3. 与准秀架构的客观对齐

### 3.1 准秀 DL CSE-CsiNet 的关键输出思想

根据 `csi结构.md`，准秀的结构信息包括：

```text
p_inst: PDP，delay-domain power profile
R_inst: spatial covariance
d_inst: multi-lag normalized temporal autocorrelation
```

并通过 SharedStateCell 累积：

```text
p_state: accumulated PDP
R_state: accumulated spatial covariance
d_state: accumulated Doppler/time-coherence proxy
s_lat: learned latent state
```

DL 重建形式：

```text
H_dl = H_cold + c_t * (H_str + H_inst)
```

其中 `c_t` 是冷启动/状态可信度门控。

### 3.2 我方 UL 应该对齐的内容

我方不需要复制准秀的 neural codec，但需要在数据层对齐结构：

| DL 概念 | UL 稳妥对应 | 是否本周做 |
|---------|-------------|------------|
| `p_inst` | `p_inst` = 当前帧 PDP | 做 |
| `p_state` | `p_state` = PDP EMA 累积 | 做 |
| `R_inst` | `R_rx_inst` = 当前帧 RX covariance | 做 |
| `R_state` | `R_rx_state` = RX covariance EMA 累积 | 做 |
| `d_inst` | `d_inst` = 多延迟归一化自相关 | 做 |
| `d_state` | `d_state` = Doppler proxy EMA 累积 | 做 |
| `c_t` | `confidence` = warmup/SNR/数据可用性门控 | 做轻量版 |
| `H_dl` | `H_ul_recon` 或当前 `H_c` | 保留 |
| compressed codeword | `h_taps` = delay-domain kept taps | 做 Python prototype |
| `s_lat` | 无直接对应 | 不做 |
| L_sync | 单节点不需要 | 不做 |

---

## 4. 稳妥版任务列表

## T0: 修订计划日志

| 项目 | 内容 |
|------|------|
| 文件 | `PLAN_0525_UL_DL_Alignment.md` |
| 状态 | 当前文档 |
| 目的 | 把原计划修正为稳妥版，避免过度承诺 |

---

## T1: 输出格式规范文档

| 项目 | 内容 |
|------|------|
| 新建文件 | `output_format_spec.md` |
| 优先级 | P0 |
| 风险 | 低 |

### 内容

定义统一输出格式：

```text
DL output: (S_dl, H_dl)
UL output: (S_ul, H_ul)
```

UL 建议字段：

```python
S_ul = {
    # shared structural fields
    "p_inst": ndarray,          # current PDP
    "p_state": ndarray,         # accumulated PDP
    "R_rx_inst": ndarray,       # current RX covariance
    "R_rx_state": ndarray,      # accumulated RX covariance
    "d_inst": ndarray,          # multi-lag autocorrelation
    "d_state": ndarray,         # accumulated time-coherence proxy
    "confidence": float,        # cold-start / reliability gate

    # UL-specific fields
    "rsrp": float,
    "snr_db": float,
    "doppler_hz": float,
    "speed_ms": float,
    "ds_rms_s": float,
    "sv": ndarray,

    # compatibility aliases
    "pdp": ndarray,
    "R_rx": ndarray,
}

H_ul = {
    "h_taps": ndarray,          # actual compressed payload
    "H_c": ndarray,             # reconstructed full-band H for eval/plot
}
```

### 注意

- `h_taps` 是真正压缩存储字段。
- `H_c` 保留为 full-band reconstruction，方便 NMSE 与后续处理。
- `R_rx_*` 不强行命名为 `R_inst/R_state`，避免与准秀 DL 的 TX-side covariance 混淆。

---

## T2: Python 结构化输出 prototype

| 项目 | 内容 |
|------|------|
| 修改文件 | `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/srs_2d_mmse.py` |
| 修改类 | `StructuredChannelOutput` |
| 优先级 | P0 |
| 风险 | 低到中 |

### T2.1 新增初始化参数

在 `__init__()` 中新增可选参数：

```python
ema_p: float = 0.10
ema_R: float = 0.05
ema_d: float = 0.15
l_lag: int = 8
```

新增状态：

```python
self.p_state = None
self.R_rx_state = None
self.d_state = None
self.H_history = []
```

### T2.2 新增 `p_inst/p_state`

当前 `pdp_trunc` 改为：

```python
p_inst = pdp_trunc
self.p_state = ema_update(self.p_state, p_inst, self.ema_p)
```

输出：

```python
S["p_inst"] = p_inst
S["p_state"] = self.p_state.copy()
S["pdp"] = p_inst              # compatibility alias
```

### T2.3 新增 `R_rx_inst/R_rx_state`

当前 `R_rx` 计算保留，但改成：

```python
R_rx_inst = R_rx
self.R_rx_state = ema_update(self.R_rx_state, R_rx_inst, self.ema_R)
```

输出：

```python
S["R_rx_inst"] = R_rx_inst
S["R_rx_state"] = self.R_rx_state.copy()
S["R_rx"] = R_rx_inst          # compatibility alias
```

### T2.4 新增 `d_inst/d_state`

保存历史 H：

```python
self.H_history.append(H.copy())
if len(self.H_history) > self.l_lag + 1:
    self.H_history.pop(0)
```

计算双边归一化多延迟自相关：

```python
d_inst = np.zeros(self.l_lag)
H_cur = H.reshape(-1)
pow_cur = np.sum(np.abs(H_cur) ** 2) + 1e-30

for lag in range(1, min(self.l_lag + 1, len(self.H_history))):
    H_prev = self.H_history[-(lag + 1)].reshape(-1)
    pow_prev = np.sum(np.abs(H_prev) ** 2) + 1e-30
    denom = np.sqrt(pow_cur * pow_prev) + 1e-30
    d_inst[lag - 1] = abs(np.vdot(H_prev, H_cur)) / denom
```

输出：

```python
S["d_inst"] = d_inst
S["d_state"] = self.d_state.copy()
```

### T2.5 新增 `confidence`

不做复杂 learned router，只做轻量、可解释的可靠度：

```python
warmup_progress = min(1.0, self._frame_count / max(self.warmup_frames, 1))
snr_factor = np.clip((snr_db + 5.0) / 25.0, 0.0, 1.0)
confidence = float(0.7 * warmup_progress + 0.3 * snr_factor)
```

或更保守：

```python
confidence = float(warmup_progress)
```

本周建议先用 `warmup_progress`，避免过度解释 SNR 映射。

### T2.6 新增 `h_taps`

当前逻辑：

```python
h_trunc = h_time.copy()
h_trunc[:, :, self.n_tap:-self.n_tap] = 0.0
H_c = np.fft.fft(h_trunc, axis=-1)
```

新增真正压缩 payload：

```python
h_taps = np.concatenate(
    [h_time[:, :, :self.n_tap], h_time[:, :, -self.n_tap:]],
    axis=-1,
)
```

输出方式：

```python
S["h_taps_shape"] = h_taps.shape
return S, {"h_taps": h_taps, "H_c": H_c}
```

但为了兼容旧接口，本周更稳妥的实现是：

```python
S["h_taps"] = h_taps
return S, H_c
```

即 `return S, H_c` 不变，只把 `h_taps` 放进 `S`。后续再整理成正式 tuple。

---

## T3: Python 验证测试

| 项目 | 内容 |
|------|------|
| 新建文件 | `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/test_structured_output.py` |
| 优先级 | P0 |
| 风险 | 低 |

### 测试项

1. **向后兼容**
   - `extract(H_smooth, snr_db, rot_angle)` 仍返回 `(S, H_c)`
   - 原字段 `pdp`, `R_rx`, `doppler_hz`, `H_c` 可用

2. **字段 shape**
   - `p_inst.shape == (n_tap,)`
   - `p_state.shape == (n_tap,)`
   - `d_inst.shape == (l_lag,)`
   - `d_state.shape == (l_lag,)`
   - MIMO 下 `R_rx_inst.shape == (n_rx, n_rx)`
   - `h_taps.shape[-1] == 2 * n_tap`

3. **数值范围**
   - `0 <= d_inst <= 1 + eps`
   - `0 <= d_state <= 1 + eps`
   - `0 <= confidence <= 1`
   - `p_state >= 0`

4. **静态/快变区分**
   - 静态 H 序列：`d_inst` 接近 1
   - 随机快变 H 序列：`d_inst` 平均明显低于静态

5. **压缩/重建一致性**
   - 用 `h_taps` 重建 `H_c`，与类内部 `H_c` 一致

### 不做的测试

不要要求：

```text
d_inst 严格单调递减
R_state 所有 eigenvalue > 0
```

原因：

- 多径/多普勒下自相关可能振荡，不保证严格单调。
- covariance 理论上 positive semi-definite，不一定 strictly positive definite。

---

## T4: 统一上下行 Block Diagram

| 项目 | 内容 |
|------|------|
| 修改文件 | `plot_professor_figures.py` |
| 新增函数 | `draw_unified_ul_dl_diagram()` |
| 输出图 | `figures/fig_unified_ul_dl_architecture.png` |
| 优先级 | P0 |

### 图中必须表达

1. DL 分支（准秀）：

```text
CSI-RS → UE H estimate → CSE-CsiNet v3.3
       → Cold/Structure/Instantaneous heads
       → S_dl + H_dl
```

2. UL 分支（刘）：

```text
SRS → LS estimate → De-rotation → IBVSS/Kalman
    → StructuredChannelOutput
    → S_ul + H_ul
```

3. 统一数据层：

```text
S = {p_state, R_state, d_state, confidence, ...}
H = compressed / reconstructed channel representation
```

4. DB/RAN Twin：

```text
(S_dl, H_dl), (S_ul, H_ul) → Database → RAN Digital Twin
```

### 图中不要过度承诺

不要写：

```text
OAI C real-time DB writer completed
```

可以写：

```text
Python prototype / DB interface to be finalized with Minji
```

---

## T5: NMSE 图整理

| 项目 | 内容 |
|------|------|
| 文件 | 现有 `figures/clean_*.png`, `figures/academic_*.png` |
| 优先级 | P0 |
| 状态 | 大部分已完成 |

建议汇报主图：

1. `clean_p50.png` 或 `academic_p50.png`：主性能图
2. `clean_errorbar.png` 或 `academic_errorbar.png`：展示分布/鲁棒性
3. 如需一张总览图，用 `clean_2panel.png`

结论表述建议：

```text
IBVSS maintains stable median NMSE around -10 to -11 dB across SNR.
Kalman achieves the best high-SNR point but is more sensitive to warmup / frame gaps.
EWMA is a simple baseline but less robust to abnormal frames.
```

---

## T6: 后续 C 代码同步（后移）

| 项目 | 内容 |
|------|------|
| 修改文件 | `nr_srs_2d_filter.c/h` |
| 当前优先级 | P3 |
| 触发条件 | Minji DB schema / 输出周期 / 文件格式明确后 |

### 后续可能工作

1. C 端新增 structure extraction state
2. 每 `extract_every` 帧生成结构信息
3. 输出到文件/共享内存/DB writer
4. 评估实时复杂度和 I/O 开销

### 为什么后移

- C 端实时 I/O 风险高。
- 当前教授汇报需要的是架构与实验结果，不需要 DB writer 已经上线。
- 结构字段还需要与 Minji 的 schema 对齐。

---

## T7: Fingerprint / Session Resumption（未来方向）

| 项目 | 内容 |
|------|------|
| 当前优先级 | P4 |
| 触发条件 | 教授或专利方向明确要求 cold/warm start 管理 |

准秀中 Fingerprint 的思想适用于：

- aperiodic SRS trigger
- RRC reconfiguration
- UE sleep/wake
- RU/DU state restore

但本周不做。

---

## 5. 文件修改总览（稳妥版）

### 本周建议修改

| 文件 | 改动 |
|------|------|
| `PLAN_0525_UL_DL_Alignment.md` | 本修订版计划 |
| `output_format_spec.md` | 新建统一输出格式规范 |
| `srs_2d_mmse.py` | 扩展 `StructuredChannelOutput` Python prototype |
| `test_structured_output.py` | 新建结构输出测试 |
| `plot_professor_figures.py` | 新增统一上下行架构图 |

### 本周不建议修改

| 文件 | 原因 |
|------|------|
| `nr_srs_2d_filter.c/h` | C 实时输出后移 |
| `nr_ul_channel_estimation.c` | dispatcher 不需要变 |
| `nr_srs_mmse.c/h` | 与结构输出无直接关系 |
| `eval_nmse_clean.py` | 当前 NMSE 评估链路稳定，不动 |
| `launch_all_v9.sh`, `run_q4_*` | sweep 已完成，不动 |

---

## 6. 时间线（稳妥版）

```text
05-25
  - 修订计划日志
  - 开始 output_format_spec.md

05-26
  - 完成统一上下行架构图
  - 扩展 StructuredChannelOutput Python prototype
  - 新增 test_structured_output.py

05-27 周二简报
  - 展示统一架构图
  - 展示 NMSE 性能图
  - 说明输出格式已与准秀结构信息对齐
  - 说明 C 实时 DB 输出将在 Minji schema 确认后推进

05-28
  - 根据简报反馈修正图和输出格式
  - 如有时间，补充 Python prototype 测试结果

05-29 周四正式展示
  - 完整展示：架构 + 算法 + OAI sweep + 输出格式
  - 明确后续：DB schema、C writer、CDL/DCL 信道对接
```

---

## 7. 汇报用一句话总结

```text
Our uplink SRS pipeline does not copy the downlink neural codec,
but it exports the same Digital-Twin-facing structure:
accumulated delay profile, spatial covariance, time-coherence statistics,
confidence, and compressed channel representation.
```

中文解释：

```text
我们不复制准秀的下行 AI codec，但在 Digital Twin 数据接口上保持一致：
都输出累积的 delay profile、spatial covariance、time-coherence statistics、
confidence，以及压缩后的 channel 表示。
```

---

## 8. 最终原则

1. **对齐输出格式，不强行对齐算法实现。**
2. **本周先完成可解释、可展示、可验证的 Python prototype。**
3. **C 实时输出和数据库写入等 Minji schema 明确后再做。**
4. **`h_taps` 才是真正 compressed payload；`H_c` 是重建/评估用 full-band H。**
5. **Doppler 自相关使用双边归一化，并为非均匀 SRS cadence 预留扩展。**
6. **命名保持物理严谨：UL 先用 `R_rx_*`，不要混同 DL 的 TX covariance。**
# UL-DL 对齐实施计划 — 05-25 制定

| 字段 | 值 |
|------|----|
| **制定日期** | 2026-05-25 (周日) |
| **目标** | 将 SRS 上行系统输出格式与准秀 CSE-CsiNet v3.3 下行系统对齐，满足教授"consistent"要求 |
| **截止** | 05-27 (周二) 简报 / 05-29 (周四) 正式展示 |
| **参考文档** | `csi结构.md`（准秀 v3.3 架构）、`0524日志.SRS_MMSE_FULL_LOGmd`（我方开发日志）、`录音26_05_22`（教授指导录音） |

---

## 0. 背景：教授 05-22 核心要求

教授在 7 分钟录音中明确了三个要求：

1. **上下行对称架构**：SRS 上行产出的结构信息和压缩 H，要与准秀下行 PMI 的输出格式**保持一致（consistent）**
2. **数据存储管线**：生成的 (S, H_c) 数据要能持续存储到数据库，供 Minji 的 RAN Digital Twin 使用
3. **成果展示**：画系统框图（schematic block diagram）+ 性能验证图（NMSE vs SNR）

---

## 1. 当前状态对比

### 1.1 准秀 CSE-CsiNet v3.3（下行 CSI Feedback）

**问题设定**：UE 估计下行 H_t → 压缩 → 空中接口传输 → gNB 重建 Ĥ_t

**核心架构**：
- 4 个 building block：CsiNet (B1) + NR W1/W2 (B2) + FiLM (B3) + GRU+EMA (B4)
- 3 个新组件：Codeword router c_t (§3.5) + Fingerprint (§3.6) + Bit allocation (§3.7)

**三层分解**：
```
Ĥ_t = Ĥ_cold(z_cold)                        ← 静态基线 (frozen CsiNet)
    + c_t · [ Ĥ_str(z_str, s_prev)           ← 宽带累积结构
            + Σ_k Ĥ_inst,k(z_inst,k, s_prev) ] ← 子带瞬时
```

**状态累积**（SharedStateCell）：
```
p_state[t] = (1−α_p)·p_state[t-1] + α_p·p_inst(Ĥ_t)    ← PDP 累积 (learnable α)
R_state[t] = (1−α_R)·R_state[t-1] + α_R·R_inst(Ĥ_t)    ← 空间协方差累积
d_state[t] = (1−α_d)·d_state[t-1] + α_d·d_inst(Ĥ_t)    ← Doppler 累积
s_lat[t]   = GRU(s_lat[t-1], concat(p,R,d,c_t,z))       ← learned latent
```

**结构信息定义**（v3.3 §3.4 原文）：
- `p_inst`：PDP，shape (N_d,)，延迟域功率谱 — 多径扩展
- `R_inst`：发射侧空间协方差，real-flattened (2·Nt²,) — 角度扩展
- `d_inst`：多延迟归一化自相关，`d[τ] = mean|⟨H_t, H_{t-τ}⟩|/|H|²` — 时间相干性

**UE-gNB 同步**：L_sync = ‖s_ue − s_bs‖²（两端共享权重、独立状态、sync loss 对齐）

### 1.2 我方 SRS-IBVSS（上行 Channel Estimation）

**问题设定**：gNB 从 SRS 信号做上行信道估计 → 自适应滤波 → 结构提取 + H 压缩 → 存储

**核心架构**：
- 信号处理路线（非 AI）：LS → De-rotation → IBVSS/Kalman（自适应 α）
- 结构提取：StructuredChannelOutput 类（单帧提取，无时域累积）
- H 压缩：DFT truncation（N_tap=64）

**当前输出（StructuredChannelOutput.extract()）**：
```python
S = {
    "rsrp":       float,                # 参考信号接收功率
    "snr_db":     float,                # SNR (from even-odd)
    "doppler_hz": float,                # 多普勒频移 (标量)
    "speed_ms":   float,                # UE 速度
    "pdp":        ndarray (N_tap,),     # 截断 PDP (瞬时，无累积)
    "ds_rms_s":   float,                # RMS 延迟扩展
    "R_rx":       ndarray (Nr, Nr),     # 空间协方差 (瞬时，无累积)
    "sv":         ndarray (min(Nr,Nt),),# 奇异值谱
}
H_c = ndarray (n_sc,)                   # DFT 截断压缩 H
```

### 1.3 关键差异

| 维度 | 准秀 (DL) | 我 (UL) | 差距 |
|------|-----------|---------|------|
| **核心方法** | AI (CsiNet + GRU + FiLM) | 信号处理 (IBVSS/Kalman) | 路线不同但目标一致 |
| **结构信息累积** | ✅ 3 个 EMA 通道 (learnable rate) | ❌ 仅瞬时提取 | **需补充** |
| **Doppler 表示** | 多延迟自相关向量 d_inst[τ] | 标量 doppler_hz | **需对齐** |
| **H 分解** | 3 层 (cold + str + inst) | 1 层 (H_c only) | **可选补充** |
| **置信度门控** | c_t router (sigmoid gate) | warmup 隐含 | **需显式化** |
| **UE-gNB 同步** | L_sync | 不需要（单节点） | ✅ 无需对齐 |
| **Session resumption** | Fingerprint matcher | 无 | 未来方向 |
| **压缩方式** | Neural (autoencoder + quantizer) | DFT truncation | 路线不同，输出一致即可 |
| **RSRP/SNR/SVD** | 无（DL 不需要） | ✅ 已有 | UL 独有，保留 |

---

## 2. 统一输出格式定义

### 2.1 Structure S（上下行共享字段）

| 字段 | DL 名称 | UL 名称 | 类型 | 说明 | 状态 |
|------|---------|---------|------|------|------|
| **p_inst** | p_inst | p_inst | ndarray (N_tap,) | 瞬时 PDP | UL ✅ 已有（名为 `pdp`） |
| **p_state** | p_state | p_state | ndarray (N_tap,) | EMA 累积 PDP | UL ❌ **需新增** |
| **R_inst** | R_inst | R_inst | ndarray (Nr, Nr) complex | 瞬时空间协方差 | UL ✅ 已有（名为 `R_rx`） |
| **R_state** | R_state | R_state | ndarray (Nr, Nr) complex | EMA 累积空间协方差 | UL ❌ **需新增** |
| **d_inst** | d_inst | d_inst | ndarray (L_lag,) | 多延迟归一化自相关 | UL ❌ **需新增**（当前仅标量） |
| **d_state** | d_state | d_state | ndarray (L_lag,) | EMA 累积 Doppler 谱 | UL ❌ **需新增** |
| **confidence** | c_t | confidence | float [0,1] | 估计可靠度门控 | UL ❌ **需新增** |

### 2.2 Structure S（UL 独有字段 — DL 不需要）

| 字段 | 类型 | 说明 |
|------|------|------|
| rsrp | float | 参考信号接收功率（UL 独有） |
| snr_db | float | SNR 估计（UL 独有，from even-odd） |
| speed_ms | float | UE 速度（UL 独有） |
| doppler_hz | float | 标量 Doppler（向后兼容） |
| ds_rms_s | float | RMS 延迟扩展 |
| sv | ndarray | 奇异值谱（MIMO） |

### 2.3 Compressed H

| 字段 | DL | UL | 类型 | 说明 | 状态 |
|------|-----|-----|------|------|------|
| **H_c** | Ĥ_cold + c_t·(Ĥ_str+Ĥ_inst) | DFT truncation | ndarray (n_sc,) or (Nr,Nt,n_sc) | 完整压缩 H | UL ✅ |
| **H_base** | Ĥ_cold (frozen baseline) | EMA 长期平均 H | ndarray (同上) | 长期结构 H | UL ❌ **可选新增** |
| **H_residual** | Ĥ_str + Ĥ_inst | H_c − H_base | ndarray (同上) | 瞬时残差 | UL ❌ **可选新增** |

---

## 3. 任务分解

### T1: 结构信息 EMA 累积层 ⭐ P0

**文件**: `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/srs_2d_mmse.py`
**修改位置**: `StructuredChannelOutput` 类 (L732-856)
**改动类型**: 修改现有类

**具体改动**:

1. `__init__()` 新增状态变量和 EMA 参数：
   ```
   + self.p_state = None         # 累积 PDP
   + self.R_state = None         # 累积 R_rx
   + self.d_state = None         # 累积 Doppler 谱
   + self.H_base  = None         # 长期平均 H
   + self.ema_p   = ema_p        # PDP EMA 速率, 默认 0.1
   + self.ema_R   = ema_R        # R_rx EMA 速率, 默认 0.05
   + self.ema_d   = ema_d        # Doppler EMA 速率, 默认 0.15
   + self.ema_H   = ema_H        # H_base EMA 速率, 默认 0.05
   ```

2. `extract()` 中在计算 pdp_trunc 后新增 EMA 累积：
   ```
   p_inst = pdp_trunc
   if self.p_state is None:
       self.p_state = p_inst.copy()
   else:
       self.p_state = (1 - self.ema_p) * self.p_state + self.ema_p * p_inst
   ```

3. R_rx 计算后同理做 EMA 累积 → R_state

4. 输出 S dict 新增: `p_inst`, `p_state`, `R_state`
   - 原 `pdp` 字段改名为 `p_inst`（保留 `pdp` 别名向后兼容）
   - 原 `R_rx` 保留为瞬时版本，新增 `R_state` 为累积版本

**工作量**: ~1.5h
**风险**: 低（纯新增，不改现有逻辑）

---

### T2: Doppler 改为多延迟自相关向量 ⭐ P0

**文件**: 同 `srs_2d_mmse.py` → `StructuredChannelOutput`
**修改位置**: `__init__()` + `extract()`

**具体改动**:

1. `__init__()` 新增：
   ```
   + self.L_lag = L_lag           # 自相关延迟数, 默认 8
   + self.H_history = []          # 最近 L_lag+1 帧的 H_smooth
   ```

2. `extract()` 中新增多延迟自相关计算：
   ```python
   # 保存当前帧
   self.H_history.append(H.copy())
   if len(self.H_history) > self.L_lag + 1:
       self.H_history.pop(0)

   # 多延迟归一化自相关
   d_inst = np.zeros(self.L_lag)
   H_flat = H.reshape(-1)
   norm = np.sum(np.abs(H_flat)**2) + 1e-30
   for lag in range(1, min(self.L_lag + 1, len(self.H_history))):
       H_prev = self.H_history[-(lag+1)].reshape(-1)
       d_inst[lag-1] = abs(np.sum(H_flat * np.conj(H_prev))) / norm
   ```

3. d_inst 做 EMA 累积 → d_state

4. 输出新增: `d_inst`, `d_state`
   - 保留 `doppler_hz` 标量（向后兼容）

**与准秀格式的对应**:
```
准秀: d[τ] = mean |⟨H_t, H_{t-τ}⟩| / |H|²,  τ ∈ {1,...,L}
我:    d_inst[τ-1] = |⟨H_t, H_{t-τ}⟩| / |H_t|²    (相同公式)
```

**工作量**: ~1h
**风险**: 低。需要额外内存存 L_lag+1 帧 H_smooth（L_lag=8, n_sc=1248 → ~80KB，可忽略）

---

### T3: 增加 confidence 输出 ⭐ P0

**文件**: 同 `srs_2d_mmse.py` → `StructuredChannelOutput`
**修改位置**: `extract()` 签名 + 内部

**具体改动**:

1. `extract()` 新增参数：
   ```
   def extract(self, H_smooth, snr_db=0.0, rot_angle=0.0,
   +            alpha=0.5, frame_count=0, warmup=20):
   ```

2. 计算 confidence（对应准秀的 c_t router）：
   ```python
   if frame_count < warmup:
       confidence = float(frame_count) / warmup
   else:
       confidence = min(1.0, 0.8 + 0.2 * max(snr_db, 0) / 20.0)
   ```

3. 输出新增: `S["confidence"] = confidence`

**工作量**: ~0.5h
**风险**: 极低

---

### T4: H 分解为 H_base + H_residual（可选） P1

**文件**: 同 `srs_2d_mmse.py` → `StructuredChannelOutput`
**修改位置**: `extract()` 返回值

**具体改动**:

1. `extract()` 中 H_c 计算后：
   ```python
   if self.H_base is None:
       self.H_base = H_c.copy()
   else:
       self.H_base = (1 - self.ema_H) * self.H_base + self.ema_H * H_c
   H_residual = H_c - self.H_base
   ```

2. 返回值扩展（向后兼容）：
   ```python
   # 原来: return S, H_c
   # 改为: return S, H_c
   # 但 S 中新增:
   S["H_base"] = self.H_base.copy()
   S["H_residual"] = H_residual
   ```

**与准秀的映射**:
| 准秀 | 我 | 物理含义 |
|-----|-----|---------|
| Ĥ_cold (frozen CsiNet baseline) | — | 我没有 AI baseline，跳过 |
| Ĥ_cold + c_t·Ĥ_str | H_base (EMA average) | 长期信道结构 |
| Ĥ_inst | H_residual = H_c − H_base | 瞬时变化/残差 |

**工作量**: ~1h
**风险**: 低

---

### T5: 端到端验证测试 ⭐ P0

**新建文件**: `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/test_structured_output.py`

**测试用例设计**:

```
Test 1: EMA 收敛性
  - CDL-C 仿真 200 帧
  - 验证 p_state 方差 < p_inst 方差 (EMA 平滑效果)
  - 验证 R_state 正定 (所有 eigenvalue > 0)

Test 2: d_inst 格式正确性
  - 静态信道: d_inst ≈ [1, 1, 1, ...] (完全相关)
  - 高速信道: d_inst 单调递减 d[0] > d[1] > ...
  - 长度 = L_lag

Test 3: confidence 单调性
  - frame_count 0→30: confidence 线性递增 [0, 1)
  - frame_count > warmup: confidence ∈ [0.8, 1.0]

Test 4: H_base + H_residual 一致性
  - 验证 H_base + H_residual ≈ H_c (数值误差 < 1e-10)
  - 验证 H_base 随时间收敛到稳态

Test 5: 与 IBVSS 集成测试
  - IBVSS → extract() → 验证所有字段类型和 shape
  - 8 个 CDL 条件 × IBVSS，确认无 crash

Test 6: 向后兼容性
  - 原有 extract(H_smooth, snr_db, rot_angle) 三参数调用仍正常工作
  - S 中仍含 pdp, R_rx, doppler_hz 原字段
```

**工作量**: ~1.5h

---

### T6: 统一架构 Block Diagram（上下行对称） ⭐ P0

**修改文件**: `plot_professor_figures.py` 新增函数 `draw_unified_block_diagram()`

**设计要求**:
- 上半部分: 准秀 DL CSI Feedback 流水线
- 下半部分: 我 UL SRS Estimation 流水线
- 中间: 共享输出格式 (S, H) 和 Digital Twin 存储层
- 突出"consistent"设计：用颜色/虚线标注对应关系
- 底部: Minji 的数据库/RAN Twin

**布局草图**:
```
┌─────────────────────────── Digital Twin Data Pipeline ──────────────────────────┐
│                                                                                 │
│  ┌─── DL CSI Feedback (준수) ─────────────────────────────────────────────┐     │
│  │                                                                        │     │
│  │  CSI-RS → UE H est. → ColdAE → FiLM heads → Quantize → Air → Decode │     │
│  │                          │                                     │       │     │
│  │                    SharedStateCell ◄──────────────────────────►│       │     │
│  │                    (GRU + 3 EMA)         L_sync                │       │     │
│  │                          │                                     │       │     │
│  │  OUTPUT: S_dl = {p_state, R_state, d_state}                   │       │     │
│  │          H_dl = Ĥ_cold + c_t·(Ĥ_str + Ĥ_inst)               │       │     │
│  └──────────────────────────┬─────────────────────────────────────┘       │     │
│                             │                                             │     │
│                      ◄── consistent format ──►                           │     │
│                             │                                             │     │
│  ┌─── UL SRS Estimation (류) ────────────────────────────────────┐       │     │
│  │                                                                │       │     │
│  │  SRS → LS est. → De-rot → IBVSS/Kalman → StructuredOutput    │       │     │
│  │                              │              ├ 3 EMA channels   │       │     │
│  │                        Even-Odd R_est       ├ DFT compression │       │     │
│  │                        Innovation P4        └ Confidence gate  │       │     │
│  │                                                                │       │     │
│  │  OUTPUT: S_ul = {p_state, R_state, d_state, rsrp, snr, sv}   │       │     │
│  │          H_ul = H_c (= H_base + H_residual)                  │       │     │
│  └──────────────────────────┬─────────────────────────────────────┘       │     │
│                             │                                             │     │
│                             ▼                                             │     │
│                  ┌─── Database / RAN Twin (민지) ──┐                     │     │
│                  │  (S_dl, H_dl, S_ul, H_ul) × t   │                     │     │
│                  │  → Digital Twin applications     │                     │     │
│                  └──────────────────────────────────┘                     │     │
└─────────────────────────────────────────────────────────────────────────────┘
```

**工作量**: ~2h

---

### T7: 统一输出格式文档 P1

**新建文件**: `output_format_spec.md`

**内容**:
- §1: 输出 tuple (S, H_c) 定义
- §2: 共享字段表（p_state, R_state, d_state, confidence）
- §3: UL 独有字段（rsrp, snr, sv, speed）
- §4: DL 独有字段（c_t gate detail, z_code）
- §5: 数据类型和形状约定（dtype, shape, units）
- §6: 存储格式建议（供 Minji 参考：JSON metadata + NPZ tensor）
- §7: 采样频率约定（DL: per CSI-RS period, UL: per SRS period）

**工作量**: ~1h

---

### T8: C 代码同步结构输出 P2（周四前）

**修改文件**: 
- `DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_2d_filter.c`
- `DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_2d_filter.h`

**改动**:
1. 新增 `structured_output_state_t` 结构体（p_state, R_state, d_state, H_base）
2. 新增 `nr_srs_extract_structure()` 函数
3. 在 dispatcher 中每 N 帧调用一次
4. 输出写入共享内存或文件

**工作量**: ~3-4h
**依赖**: T1-T4 Python 验证通过后再移植

---

### T9: Fingerprint Session Resumption P3（未来方向）

**文件**: `srs_2d_mmse.py` 新增 `StructureFingerprint` 类

**设计**:
- 序列化 (p_state, R_state, d_state) → fingerprint 向量
- 新 SRS 帧到来时计算与 fingerprint 的余弦相似度
- 阈值以上: 恢复旧状态 (warm start)
- 阈值以下: 重置 (cold start)
- warm start 后 min_slots_to_trust 帧内保持 confidence 保守

**工作量**: ~3h
**触发条件**: 教授或专利需求明确后再做

---

## 4. 文件修改总览

### 需要修改的文件

| 文件 | 任务 | 改动量 |
|------|------|--------|
| `G1C_.../srs_2d_mmse.py` | T1+T2+T3+T4 | `StructuredChannelOutput` 类 ~+80 行 |
| `plot_professor_figures.py` | T6 | 新增 `draw_unified_block_diagram()` ~+150 行 |
| `nr_srs_2d_filter.c` | T8 (P2) | 新增结构体 + 函数 ~+200 行 |
| `nr_srs_2d_filter.h` | T8 (P2) | 新增声明 ~+20 行 |

### 需要新建的文件

| 文件 | 任务 | 预估行数 |
|------|------|---------|
| `G1C_.../test_structured_output.py` | T5 | ~250 行 |
| `output_format_spec.md` | T7 | ~150 行 |

### 不需要改动的文件

- `srs_2d_mmse.py` 中的 `IBVSS_EMA`, `ScalarKalmanEMA` — 不动（T1-T4 只改 StructuredChannelOutput）
- `nr_srs_mmse.c/h` — 不动
- `nr_ul_channel_estimation.c` — 不动（dispatcher 层面不变）
- `eval_nmse_clean.py` — 不动
- 所有 shell 脚本 — 不动
- 所有测试脚本（capture gate 等）— 不动

---

## 5. 时间线

```
05-25 (周日) 下午
  ├─ ✅ 本计划文档完成
  ├─ T1+T2+T3: 修改 StructuredChannelOutput    [3.5h]
  └─ T5: 验证测试                               [1.5h]

05-26 (周一)
  ├─ T4: H_base/H_residual 分解                 [1h]
  ├─ T6: 统一 Block Diagram                     [2h]
  └─ T7: 输出格式文档                            [1h]

05-27 (周二) ★ 教授简短汇报
  ├─ 展示: 统一架构 Block Diagram (上下行对称)
  ├─ 展示: NMSE vs SNR 三算法对比图
  ├─ 说明: 新增输出格式与准秀对齐
  └─ 时间线: CDL 信道对接等待义焕

05-28 (周三)
  └─ T8: C 代码同步结构输出 (开始)               [3-4h]

05-29 (周四) ★ 教授正式结果展示
  ├─ 展示: 完整实现 (Python + C)
  ├─ 展示: 端到端验证结果
  ├─ 展示: 输出格式文档 (供 Minji 对接)
  └─ 讨论: CDL 信道对接计划

06-02~ (下下周)
  ├─ T9: Fingerprint (如需要)
  └─ CDL 信道对接 (义焕 initial version ready)
```

---

## 6. 准秀架构中不需要在 UL 复现的部分

以下组件是 DL CSI Feedback 特有的，UL SRS 不需要：

| 准秀组件 | 为什么不需要 |
|---------|-------------|
| CsiNet autoencoder | UL 不经过空中接口压缩传输，直接本地处理 |
| FiLM conditioning | FiLM 用于跨量化器传递状态，UL 无量化器 |
| L_sync 同步损失 | UL 单节点（gNB 本地），无双端同步问题 |
| Quantizer (2-bit uniform) | UL 不做 air-interface quantization |
| Phase-0/Phase-1 训练 | UL 用信号处理而非 AI，无需分阶段训练 |
| BitKeepMask + bit-penalty | UL 存储不受 air-interface bit 限制 |
| GRU latent s_lat | UL 用 IBVSS/Kalman 状态替代 learned latent |

**但这些组件的"思想"是可以借鉴的**：
- FiLM 的思想 → 用 side info (SNR, hop_id) 调制提取过程（未来 AI 版本可用）
- c_t router 的思想 → confidence 门控（T3 已吸收）
- 3-tier 分解的思想 → H_base + H_residual（T4 已吸收）
- 3 EMA channels 的思想 → p_state, R_state, d_state（T1 已吸收）

---

## 7. 教训和设计原则

1. **格式一致 > 方法一致**：教授要的是输出格式 consistent，不是让我们也用 AI。信号处理路线出相同格式的 (S, H_c) 完全可以。

2. **EMA 速率用固定值**：准秀用 learnable rate（因为他是端到端训练），我们用固定 rate（因为我们是信号处理路线）。EMA 的物理含义一样，只是 rate 怎么确定的不同。

3. **向后兼容**：所有新增字段都是追加，不改原有字段。`extract()` 的三参数老接口仍然工作。

4. **先 Python 验证，后 C 移植**：跟之前 IBVSS/Kalman 的开发纪律一致。P1B relative-eps bug 就是在 Python 发现的。

5. **不要过度设计**：Fingerprint、Multi-UE、Fronthaul quantizer 都是准秀 §5.3 提到的"可能需要的新组件"，但教授没有明确要求，放到未来。
