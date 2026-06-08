# filt8 优化：-7 dB Floor 突破实验

| 字段 | 值 |
|------|----|
| **日期** | 2026-05-12 |
| **目标** | 诊断 SRS 信道估计 -7 dB NMSE floor 的根因 |
| **状态** | 诊断完成：floor 来自 float64 GT vs int16 SRS 的数值坐标系差异（详见 8.14） |

---

## 1. 问题定位

通过实验 7（静态 channel）和实验 8（SNR=40 dB）确认 -7 dB floor 跟 SNR/thermal noise 无关。

但**根因需要更审慎的分析**——不能简单归结为 filt8。下面逐层推导。

---

## 2. filt8 的具体问题

### 当前 filt8 的 overlap-add 有效权重（K_TC=2）

追踪 `filt8_middle2` + `filt8_middle4` 的叠加效果：

```
filt8_middle2 = [0.25, 0.5, 0.5, 0.5, 0.25, 0, 0, 0]   (Q14)
filt8_middle4 = [0, 0, 0.25, 0.5, 0.5, 0.5, 0.25, 0]    (Q14)

Pair(odd pilot at P+2, even pilot at P+4) 共用指针 P:

位置 P+0:  前 pair 尾部 0.25*LS[P]
位置 P+1:  前 pair 尾部 0.5*LS[P]           ← 看似 midpoint
位置 P+2:  0.5*LS[P+2] + 0.25*LS[P+4]      ← PILOT 但不是精确重建！
位置 P+3:  0.5*LS[P+2] + 0.5*LS[P+4]       ← midpoint (线性)
位置 P+4:  0.25*LS[P+2] + 0.5*LS[P+4]      ← PILOT 但不是精确重建！
位置 P+5:  0.5*LS[P+4]                      ← 给下个 pair 的 midpoint
```

**有效插值权重**：

| 位置类型 | 当前权重 | 问题 |
|----------|----------|------|
| Pilot 位置 | `0.25*prev + 0.5*self + 0.25*next` | **3 点移动平均**，丢失已知精确信息 |
| Midpoint | `0.5*left + 0.5*right` | 线性插值（OK） |

**注意：以下分析在 float 精度下成立，但 Python 仿真（Section 8.13）证明 filt8 系数不是 -7 dB floor 的根因。3 点平滑在 float 下仅引入 -28.9 dB 误差。**

---

## 3. 新系数设计

### 目标

- Pilot 位置：**精确重建**（weight = [0, 1.0, 0]）
- Midpoint：**线性插值**（weight = [0.5, 0.5]，与 legacy 相同）

### 新系数（Q14，16384 = 1.0）

```c
// filt16a_32.h — Optimized SRS Comb size 2
filt8_start_opt       = [16384, 8192,     0,     0, 0, 0, 0, 0]  // [1.0, 0.5, 0, ...]
filt8_start_shift2_opt= [    0,     0, 16384, 8192, 0, 0, 0, 0]
filt8_middle2_opt     = [    0,  8192, 16384, 8192, 0, 0, 0, 0]  // [0, 0.5, 1.0, 0.5, 0, ...]
filt8_middle4_opt     = [    0,     0,     0, 8192, 16384, 8192, 0, 0]
filt8_end_odd_opt     = [    0,  8192, 16384, 16384, 0, 0, 0, 0]  // odd k: 精确 + hold
filt8_end_even_opt    = [    0,     0,     0,  8192, 16384, 16384, 0, 0]  // even k: 跳过 middle2 区域
```

### 验证 overlap-add 有效权重

```
新 middle2_opt = [0, 0.5, 1.0, 0.5, 0, 0, 0, 0]
新 middle4_opt = [0, 0, 0, 0.5, 1.0, 0.5, 0, 0]

位置 P+2:  1.0*LS[P+2] + 0              ← PILOT 精确重建 ✓
位置 P+3:  0.5*LS[P+2] + 0.5*LS[P+4]   ← midpoint 线性插值 ✓
位置 P+4:  0 + 1.0*LS[P+4]             ← PILOT 精确重建 ✓
位置 P+5:  0.5*LS[P+4]                  ← 给下个 pair 的 midpoint ✓
```

### vs Legacy 的区别

| 位置 | Legacy 权重 | Optimized 权重 | 改善 |
|------|------------|---------------|------|
| Pilot | [0.25, **0.5**, 0.25] | [0, **1.0**, 0] | 消除 3 点平滑误差 |
| Midpoint | [0.5, 0.5] | [0.5, 0.5] | 相同 |

---

## 4. 代码改动

### 文件列表

| 文件 | 改动 |
|------|------|
| `openair1/PHY/NR_UE_ESTIMATION/filt16a_32.h` | 新增 `filt8_*_opt` 系数数组 |
| `openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c` | 新增 `SRS_OPTFILT` env 开关 + 优化路径 |

### 运行时切换

```bash
# 使用优化系数
export SRS_OPTFILT=1

# 使用 legacy 系数（默认）
export SRS_OPTFILT=0   # 或不设置
```

启动时日志输出：
```
SRS interpolation filter: OPTIMIZED (exact pilot recon)
SRS interpolation filter: LEGACY (3-pt smooth)
```

### 关键实现细节

1. **End filter 需要区分 odd/even k**：
   - Legacy 的 end filter 对所有 k 用同一个系数
   - 优化版对 odd k 用 `filt8_end_odd_opt`，even k 用 `filt8_end_even_opt`
   - 原因：even k 的 end 不能写入 position 0-2（那里是 odd k-1 的 pilot，已精确重建）

2. **Even k end 也需要推进指针**（`srs_estimated_channel16 = &srs_est[sc_offset]`）
   - Legacy 的 end 不推进指针（因为是最后几个 pilot）
   - 优化版的 even k end 需要推进，确保 wrap-around 后下一个 start 的指针正确

---

## 5. 测试计划

### 第一步：静态 channel A/B 对比（最可靠）

```bash
# A: Legacy baseline
SRS_ESTIMATOR=legacy SRS_OPTFILT=0 CHANNEL_SEED=42 UE_SPEED=0

# B: Optimized filter
SRS_ESTIMATOR=legacy SRS_OPTFILT=1 CHANNEL_SEED=42 UE_SPEED=0
```

**预期（已被 Section 8.13 证伪）**：
- ~~如果 -7 dB floor 确实来自 filt8 → B 的 p50 应该突破 -7 dB~~
- Python float 仿真证明 filt8 在 float 下达到 -22 dB，系数不是 floor 根因

### 第二步：动态 channel 对比

```bash
# 同上但 UE_SPEED=3
```

**注意**：动态下 GT-SRS 对齐问题仍然存在，NMSE 数值可能不可靠。重点看 A/B 的相对差异。

### 第三步：EWMA + OptFilt 组合

```bash
SRS_ESTIMATOR=2dmmse SRS_OPTFILT=1 CHANNEL_SEED=42 UE_SPEED=0
```

**预期**：OptFilt 消除 pilot 平滑误差 + EWMA 压 i.i.d. 随机成分 → 进一步改善。

---

## 6. -7 dB Floor 深度分析：filt8 vs int16 pipeline

### 6.1 filt8 smoothing 的误差推导

**Pilot 位置的误差分解**：

```
H_out[k] - H_true[k]
  = 插值误差 + 噪声项
  = 0.25·(H_true[k-2] + H_true[k+2] - 2·H_true[k])     ← deterministic, 二阶差分
  + 0.25·N[k-2] + 0.50·N[k] + 0.25·N[k+2]               ← random, 被平滑压缩
```

**频域滤波器视角**：

[0.25, 0.5, 0.25] 在 pilot 索引空间的频率响应：

```
H_filt(ω) = 0.5 + 0.5·cos(ω)

ω = 0 (flat channel):  H_filt = 1.0   → 无误差
ω = π/2:               H_filt = 0.5   → 50% 衰减 (-6 dB)
ω = π (Nyquist):       H_filt = 0     → 完全丢失
```

NMSE 贡献 = `∫ |1 - H_filt(ω)|² · S_H(ω) dω / ∫ S_H(ω) dω`，其中 `|1 - H_filt|² = sin⁴(ω/2)`。

### 6.2 跟 Sionna channel 的具体数值

```
TDL-A: max excess delay ≈ 400 ns
pilot 间距 = K_TC × Δf = 2 × 30 kHz = 60 kHz
pilot Nyquist 周期 = 1/(2×60 kHz) = 8.33 μs
channel 时延占比 = 400 ns / 8.33 μs ≈ 5%
```

channel PDP 的能量只用了 pilot Nyquist 带宽的 ~5%。大部分 channel 能量集中在 ω ≈ 0 附近，`sin⁴(ω/2)` 在那里极小。

**filt8 smoothing 的 NMSE 贡献估算 ≈ -15 ~ -20 dB，远小于 -7 dB。**

### 6.3 关键反面证据：D0 Passthru 实验

| 模式 | 做了什么 | NMSE p50 | active SC |
|------|----------|----------|-----------|
| legacy | LS + filt8 插值 | -6.64 dB | 1248 |
| **passthru** | **跳过 filt8，只输出原始 LS** | **-6.49 dB** | 624 |
| 差距 | | **0.15 dB** | |

**跳过 filt8 后 NMSE 几乎不变！** 这说明 -7 dB floor 的主因**不是 filt8**。

注意：D0 是动态 channel + GT 对齐问题，但 passthru 和 legacy 用的是同一组数据，对齐误差对两者影响相同，所以 A/B 差异（0.15 dB）是可信的。

### 6.4 代码确认的三个损失点

通过阅读源码，确认了每个损失点的**真实实现**：

**全链路**：

```
Sionna float64 H(f) × X(f)
  → Proxy GPU: round + clip(-32768,32767) → int16     ← 损失 ①
    → OAI int16 DFT (Q15 SIMD, scale=1, 多级 >>1)     ← 损失 ②
      → LS: int32(gen×rx) >> srs_generated_signal_bits → int16   ← 损失 ③
        → filt8: int16 overlap-add (adds_epi16 饱和)    ← 损失 ④
```

---

**损失 ① Proxy float64 → int16 量化**

代码位置：`v8.py` L1582-1588

```python
# 标准路径
self._buf_iq_out_3d[:, :, 0] = cp.clip(cp.around(out.real), -32768, 32767)
self._buf_iq_out_3d[:, :, 1] = cp.clip(cp.around(out.imag), -32768, 32767)
self.gpu_iq_out[:] = self._buf_iq_out_3d.ravel().astype(cp.int16)
```

- 直接 `round + clip`，**没有** 乘 2^N 的定标
- 信号幅度取决于 path loss × channel gain × TX power
- 如果信号只用了 int16 的 ~10 bit 动态范围（max ≈ 1024），有效精度就是 10 bit
- **关键未知**：运行时信号实际用了多少 bit？需要运行时测量

---

**损失 ② OAI int16 FFT（确认是 fixed-point）**

代码位置：`oai_dfts.c` + `slot_fep_nr.c`

```c
// slot_fep_nr.c L135: UL 接收 FFT，scale=1 固定
dft(dftsize, rxdata_ptr,
    (int16_t *)&rxdataF[symbol * ofdm_symbol_size], 1);
```

- **OAI 自研 int16 Q15 SIMD DFT**，不是 FFTW，不是 float
- `scale=1` 意味着每个子 DFT 阶段末尾做 `>>1` 右移
- 蝶形运算用 `adds_epi16`（饱和加）+ `mulhrs_epi16`（Q15 乘法）
- **2048 = 64×32 等分解**，每级子 DFT 各自 >>1，再加外层 >>1

精度损失估算：

```
dft2048 分解 ≈ 5-6 级递归子 DFT
每级 >>1 → 丢 5-6 bit
输入 16 bit → 输出 ~10-11 bit 有效精度
SNQR ≈ 60-66 dB
```

再加上每个 butterfly 的 Q15 twiddle 乘法（`mulhrs_epi16`）引入 ±1 LSB 噪声，
累积 ~2048 次操作后总量化噪声功率增大。

**但即使只剩 10 bit，SNQR ≈ 60 dB，仍远高于 -7 dB。FFT 单独不能解释 floor。**

---

**损失 ③ LS 乘法**

代码位置：`nr_ul_channel_estimation.c` L827-830

```c
ls_estimated.r += (int16_t)(((int32_t)generated_real * received_real
                            + (int32_t)generated_imag * received_imag)
                            >> nr_srs_info->srs_generated_signal_bits);
```

- `int16×int16 → int32`（无损），`>> srs_generated_signal_bits`（~15），截断到 `int16`
- 如果 received 只有 10 bit 有效，generated 有 15 bit：乘积 25 bit，>>15 后剩 10 bit
- **精度与 received 的有效 bit 数直接相关**（由 FFT 决定）

---

**综合分析：各损失点的 SNQR**

| 损失点 | 确认的实现 | 估算有效 bit | 估算 SNQR |
|--------|-----------|-------------|-----------|
| ① Proxy 量化 | round+clip, 无定标 | **取决于运行时信号幅度** | 如果 10 bit: ~60 dB |
| ② FFT | int16 Q15, 多级 >>1 | ~10-11 bit | ~60-66 dB |
| ③ LS 乘法 | int32 乘积 >>15 → int16 | ~10 bit (继承 FFT) | ~60 dB |
| ④ filt8 | int16 adds_epi16 | ~16 bit | 取决于 channel 形状 |

**所有单独损失点的 SNQR 都在 ~60 dB 级别，远高于 -7 dB (-7 dB = 20% 误差)。**

### 6.5 真正的问题：-7 dB 不是精度 floor

60 dB SNQR 意味着量化噪声 NMSE ≈ -60 dB，跟 -7 dB 差了 50 多 dB。
**int16 pipeline 精度不是 -7 dB floor 的主因。**

那 -7 dB 到底从哪来？最可能的解释是 **GT-SRS 评估方法本身的系统误差**：

1. **Proxy 没有显式定标**：float64 信号直接 round 到 int16，没有 ×2^N 归一化。信号的 absolute scale 取决于 path loss / channel gain / TX power，每帧不同。
2. **GT 保存的是 Sionna 的 H(f)**（float64），但 OAI 估计的 LS 值经过了 `generated × received >> shift` 链路，**归一化方式不同**。
3. **LS-aligned NMSE 只消除了全局 scale**（一个 complex scalar alpha），但无法消除 **per-subcarrier 的 scale/phase 差异**（比如 FFT 窗函数效应、CP 处理差异等）。
4. **filt8 的 3 点 pilot 平滑**确实引入了 per-subcarrier 的 deterministic 误差，但只贡献 ~-15 dB。

**-7 dB 更可能是评估方法的 artifact（GT 和 SRS 不在完全相同的归一化/格式下比较），而不是估计算法的真实误差。**

### 6.6 修正后的 floor 来源假说

| 假说 | 代码证据 | SNQR 估算 | 能否解释 -7 dB? |
|------|----------|-----------|----------------|
| FFT fixed-point 精度 | int16 Q15, 多级 >>1 | ~60 dB | **否**（差 50+ dB） |
| Proxy 量化 | round+clip, 无定标 | ~60 dB | **否** |
| LS 乘法截断 | int32>>15→int16 | ~60 dB | **否** |
| filt8 pilot 平滑 | [0.25,0.5,0.25] | float 下 -28.9 dB | **否**（Section 8.13 证伪） |
| **int16 全链路累积 + GT-SRS 数值坐标系差异** | float vs int16 两套数值系统 | — | **确认（Section 8.14）** |

**核心洞察**：单独每个 int16 损失点 SNQR ~60 dB，filt8 系数在 float 下 -22 dB，
但 GT(float64) vs SRS(int16) 这两个不同数值系统之间的固有差异无法被全局 alpha 消除。

### 6.7 静态 GT 平均验证（05-12 17:12）

用 `--static` 模式重新评估 `static_legacy2` 数据：取所有 2601 帧 GT 平均作为唯一参考，跳过 slot_id 配对，对 100 帧 SRS 全部评估。

| 模式 | NMSE p50 | 说明 |
|------|----------|------|
| 错误 slot_id 配对，无 STO | +18.36 dB | 配对完全错 + 线性相位斜率主导 |
| GT 平均，无 STO | +19.17 dB | 配对对了但 STO 仍主导 |
| **GT 平均 + per-frame STO** | **-6.40 dB** | 真实 floor |
| 之前 eval (slot_id + STO) | -6.53 dB | 碰巧接近（10 帧碰对了） |

**结论**：
1. **-6.4 dB floor 是真实的**，不是 slot_id 配对错误导致的 artifact
2. **STO 贡献 ~25 dB**：占误差的绝大部分
3. **STO 在帧间变化巨大**：mean=-15.5, std=27, range=[-65, +4] samples
4. **Pilot vs Midpoint 只差 0.22 dB**：filt8 不是 floor 来源
5. **per-SC alpha CoV=0.20, angle std=16°**：STO 校正后仍有残余频率相关误差

**-6.4 dB = STO 校正不完美的残差 + 帧间时序抖动的高阶效应**

### 6.8 STO 变化来源确认（05-12 17:17）

用 Global STO vs Per-frame STO 对比验证：

```
│ STO Mode       │   p10    │   p50    │   p90    │
│ No STO         │   +5.08  │  +19.17  │  +35.18  │
│ Global STO     │   -6.93  │  +24.95  │  +33.96  │  ← 对少数帧好，大部分帧更差
│ Per-frame STO  │   -7.57  │   -6.40  │   -0.32  │
```

**Global STO p50 = +25 dB**（比不做还差）→ 证明 STO 真的帧间变化，不是估计噪声。

**STO 帧间变化的根因**：OAI 闭环 Timing Advance (TA)。

```
OAI TA 控制流程:
  gNB_scheduler_dlsch.c L1038: 每 100 帧触发 ta_apply = true
  gNB_scheduler_dlsch.c L106:  组装 TA MAC CE (TA_COMMAND = ta_update)
  gNB_scheduler_ulsch.c L884:  从 PUSCH 更新 ta_update
  phy_procedures_nr_gNB.c L508: PHY 估计 est_delay → 量化为 0-63

结果: UE 收到 TA command → 调整 TX 时序 → SRS 到达 gNB 的时刻变了
     → gNB FFT 窗口固定 → 等效 STO 随帧变化
```

**完整因果链**：

```
OAI TA 闭环 (每~100帧一次)
  → UE TX 时序帧间变化 ±65 samples
    → LS 估计的 H(f) 包含帧间变化的线性相位斜率
      → GT H(f) 没有这个斜率（Proxy 不知道 TA）
        → per-frame STO polyfit 只能近似修掉
          → 残差 = -6.4 dB NMSE floor
```

**禁用 TA 后的预期**：STO 变为常数 → Global STO 一次修掉 → NMSE 应降到 int16 精度极限（~-20 dB 或更好）。

### 6.9 后续诊断（diagnose_nmse_floor.py，静态 channel 数据）

用 `static_legacy2_20260512_1152/snr_20dB` 的数据（speed=0, SNR=20, seed=42）跑了 6 项诊断：

| 诊断 | 值 | 结论 |
|------|-----|------|
| Per-SC alpha CoV | **0.66** | GT-SRS 间有巨大的频率相关 scale/phase 变化 |
| Per-frame \|alpha\| | 5.4 ~ 258, std=105 | 帧间 GT-SRS 对应极不可靠（即使静态 channel） |
| angle(alpha) std | 112° | 相位每帧剧烈变化 |
| NMSE（无 STO 校正） | **+4.68 dB** | 不做 STO 校正就是正值（完全没有估计意义） |
| NMSE（有 STO 校正，之前的 eval） | **-6.53 dB** | STO 校正修掉 ~12 dB，但残差还有 -7 dB |
| Pilot vs Midpoint NMSE 差 | **0.08 dB** | **filt8 插值不是问题** |
| SRS int16 有效 bit | **14.2 / 15** | **int16 精度不是问题** |
| 配对帧数 | 10 / 100 | slot_id 配对率极低 |

#### 结论

1. **filt8 不是 floor 的原因**：pilot 和 midpoint 的 NMSE 只差 0.08 dB
2. **int16 精度不是 floor 的原因**：信号用了 14.2 bit，SNQR >> -7 dB
3. **-7 dB floor 的真正来源是 STO + GT-SRS 对齐问题**：
   - 无 STO 校正：NMSE = +5 dB（STO 主导）
   - 有 STO 校正：NMSE = -7 dB（STO 校正不完美的残留）
   - per-SC alpha CoV = 0.66：频率维度的系统性偏移无法被全局 alpha 消除
4. **OptFilt 的改善预期极其有限**（~0.1 dB），因为 pilot/mid 差异已经只有 0.08 dB

### 6.6 OptFilt 测试结论（已关闭）

Python float 仿真（Section 8.13）已证明：替换 filt8 系数不能突破 -7 dB floor。
filt8_legacy 在 float 下 = -22 dB, sinc = -20 dB, 两者均 noise-limited 且 STO-invariant。
**不再需要运行 OptFilt A/B 测试。**

---

## 7. 编译状态

```
[10799/10799] Linking CXX executable nr-softmodem  ✅
```

编译通过，无 warning/error。

---

## 8. TA 实验与 GT-SRS 对齐诊断（2026-05-12 晚）

### 8.1 TA 禁用实验

**目的**：验证 OAI 的闭环 Timing Advance 是否导致帧间 STO 抖动。

**方法**：在 `gNB_scheduler_dlsch.c` 中添加 `DISABLE_TA_UPDATE=1` 环境变量守卫，禁止 MAC 层发送 TA MAC CE。

**结果对比**（static channel, SNR=20dB, legacy filt8）：

| 指标 | TA 开启 | TA 关闭 |
|------|---------|---------|
| SRS 帧数 | 101 | 9 |
| STO std | 25.70 samples | 0.67 samples |
| STO range | [-81, +3] | [-1, +1] |
| NMSE p50 (per-frame STO 校正) | -6.64 dB | -7.13 dB |
| NMSE p10 | -15.29 dB | -7.74 dB |
| 帧间相位 angle(α) std | 110° | 28° |

**关键结论**：
- TA 闭环**确实**是 STO 帧间抖动的根源（std 降低 40 倍）
- 但 NMSE 仅改善 0.7 dB（-6.64 → -7.13），因为 per-frame STO 校正已补偿大部分效果
- 完全禁用 TA 导致连接极不稳定（BLER 20-45%，9帧/928s），不可用于生产
- **TA 代码已回退**（无环境变量守卫），恢复原始行为

### 8.2 -7 dB Floor 因素排除

| 因素 | 诊断结果 | 是否 floor 来源？ |
|------|----------|---------------------|
| filt8 插值 | Pilot vs Midpoint NMSE 差 0.25-0.30 dB | **否** |
| int16 量化 | 有效位 14.2/15，SNQR >> 7 dB | **否** |
| TA 导致的 STO 抖动 | 禁用后 STO std 40x 降低，NMSE 改善 0.7 dB | **部分**（已被 STO 校正大部分吸收） |
| GT-SRS slot_id 失配 | 本数据集碰巧范围重叠可匹配 | 动态信道需要 auto-offset |

### 8.3 Phase 2 实施结果

#### Slot_id 自动对齐

在 `digital_twin_stats.py` 中添加了 `find_slot_offset()` 和 `align_by_slot(auto_offset=True)`：
- 自动检测 GT（IPC ts-based）和 SRS（SFN-based）的坐标系偏移
- 通过搜索最优常数 C 最大化匹配数
- 当前数据碰巧两个坐标系范围重叠，auto-offset 不影响结果
- 对更长运行或 IPC ts 更大的情况有用

#### N_TA_offset 相位校正

实现了 `apply_nta_correction(H_gt, active, n_ta_offset=800, n_fft=2048)`。

**关键发现**：N_TA 校正与 per-frame STO 校正**冗余**。
- Per-frame STO 校正是数据驱动的，已经自动吸收了 N_TA 产生的固定相位斜率
- 同时使用两者会导致**双重校正**，NMSE 从 -6.64 恶化到 +13.25 dB
- `apply_nta_correction` 保留作为 STO 校正关闭时的替代方案

### 8.4 最终诊断：-7 dB Floor 的本质

经过 TA 实验、filt8 排除、int16 排除、slot_id 对齐和 N_TA 校正后，-7 dB floor 仍然存在。
分析表明这是 **per-SC alpha CoV = 13.8%** 造成的——GT 和 SRS 在频域形状上有系统性差异，
单个全局复数 alpha 无法完全校准。

**可能原因**：
1. OAI LS 估计器的 `Y*conj(X_ref) >> shift` 操作引入了与 SRS 参考信号相关的频率依赖因子
2. FFT 的 fixed-point 精度在不同子载波上有不同的量化行为
3. Proxy 的 H(f) 参考点和 OAI 的 H_LS(f) 参考点在高阶相位上有差异

### 8.5 SRS LS 完整缩放链路追踪

```
信号路径: UE → Proxy → gNB

1. Sionna H(f): complex float64, |H_gt| RMS ≈ 53.15
   (频域理想信道，单位幅度级别)

2. UE SRS 生成 (srs_modulation_nr.c):
   - ZC 序列: rv_ul_ref_sig[u][v][idx][k], Q15 编码, |ZC| ≈ 32767
   - amp = AMP = 1 << 9 = 512
   - X_ref[k] = round(amp * ZC[k] / sqrt(N_ap)) >> 15 ≈ 362
   - srs_generated_signal_bits = log2_approx(512) = 9

3. Proxy 信道应用 (v8.py):
   Y(f) = H(f) · X(f)  (cupy FFT/IFFT, 标准归一化)

4. gNB 接收 FFT (oai_dfts.c, dft2048, scale=1):
   - 2048 = 2^11, 每级 butterfly ×(1/sqrt(2))
   - 总缩放: /sqrt(2048) ≈ /45.25
   - 结果: Y_rx[k] = int16 FFT 输出

5. LS 估计 (nr_ul_channel_estimation.c):
   H_LS.r = (conj(X_ref) · Y_rx).r >> 9
   H_LS ≈ |X_ref|² · H_true / 2^9 · (FFT/IFFT scaling)

实测:
  SRS RMS = 10064,  GT RMS = 53.15
  Scale ratio = SRS/GT = 189.33

Per-SC alpha (全局 alpha 之后的频域形状差异):
  CoV = 14.1%,  range = [0.379, 1.066]
  → 这个 CoV 是 -7 dB floor 的根因
```

### 8.6 Per-SC Bias Calibration 测试

使用 `estimate_sc_bias` + `apply_sc_bias` (split-half 验证)：

| 指标 | 无 bias | 有 bias | 变化 |
|------|---------|---------|------|
| per-frame NMSE p50 | -7.43 dB | -6.86 dB | -0.58 dB (恶化) |
| per-frame NMSE p10 | -16.22 dB | -11.40 dB | -4.82 dB (恶化) |
| accumulated NMSE | -9.20 dB | -3.88 dB | -5.33 dB (恶化) |

**结论：Per-SC bias calibration 失败。**

原因分析：
1. TA 引起的帧间 STO 抖动（std=25.7 samples）使 per-SC 相位 pattern 不稳定
2. Method E 的 per-frame STO 校正无法完美补偿——残留 STO 产生随帧变化的 per-SC 误差
3. Calibration half 估计的 bias 是这些变化 pattern 的平均，不适用于单独的 test frame
4. 根本问题：**bias 是非平稳的**，简单的静态 calibration 不 work

### 8.7 -7 dB Floor 最终诊断（已修正，2026-05-12 23:00）

```
因素排除表:
  ✗ filt8 插值:     pilot vs midpoint NMSE 差 0.25 dB     → 不是
  ✗ int16 量化:     有效位 14.2/15, SNQR >> 7 dB          → 不是
  ✗ N_TA offset:    per-frame STO 已自动吸收               → 不是独立因素
  ✗ slot_id 失配:   当前数据已能正确配对                   → 不是
  ✗ Per-SC bias:    非平稳，static calibration 反而恶化    → 不可简单消除
  ✗ TA/STO 抖动:    TA-OFF 实验 STO≈0 仍然 -7 dB          → 不是（下面详述）
```

### 8.8 TA-OFF vs TA-ON 对比实验（决定性证据）

TA-OFF 实验（`optfilt_test_20260512_2120`，9 帧）vs
TA-ON 实验（`optfilt_test_20260512_2217`，101 帧），均为 static channel, SNR=20dB：

```
               TA-OFF (9帧)       TA-ON (101帧)
STO std        0.67 samples       25.70 samples
NMSE best      -8.39 dB           -22.48 dB
NMSE p50       -7.41 dB           -6.64 dB
NMSE range     [-8.39, -5.71]     [-22.48, +29.30]
```

关键矛盾：

如果 "STO 抖动 → STO 校正残差" 是 floor 根因，
那 TA-OFF (STO≈0, 9/9帧) 应该突破 -7 dB 达到 -20 dB 级别。
但 TA-OFF 的 9 帧全部在 [-8.39, -5.71] dB，没有一帧低于 -9 dB。

```
TA-OFF 逐帧:
  frame 0: NMSE=-8.39 dB  STO=+0.00   ← STO=0, 仍然 -8 dB
  frame 1: NMSE=-6.99 dB  STO=+0.00
  frame 2: NMSE=-5.71 dB  STO=+1.00
  frame 3: NMSE=-6.98 dB  STO=+0.00
  frame 4: NMSE=-7.41 dB  STO=-1.00
  frame 5: NMSE=-8.37 dB  STO=-1.00
  frame 6: NMSE=-8.28 dB  STO=-0.00
  frame 7: NMSE=-7.45 dB  STO=-1.00
  frame 8: NMSE=-7.23 dB  STO=-1.01

TA-ON 中的 "好帧"（STO 也≈0）:
  frame 31: NMSE=-21.05 dB  STO=-0.00  ← 同样STO≈0, 却 -21 dB
  frame 42: NMSE=-19.64 dB  STO=-0.00
  frame 80: NMSE=-22.48 dB  STO=+1.00
  frame 95: NMSE=-21.56 dB  STO=-0.00
```

**结论**：之前 8.7 节把 floor 归因于 "TA → STO → STO 校正残差" 是错误的。
TA-OFF 实验直接否定了这个因果链。TA-ON 中的 "好帧" (-22 dB) 是异常值，
不能作为 "STO 小 → NMSE 好" 的论据，因为 TA-OFF（STO 全部≈0）达不到这个水平。

### 8.9 GT 帧间一致性分析（root cause）

对 TA-OFF 的 9 帧做了完整的帧间一致性分析，发现了真正的 root cause。

#### GT 帧间变化

```
GT 每帧 RMS (rx0, tx0):
  帧0: 48.49    帧3: 28.94    帧6: 97.60
  帧1: 41.16    帧4: 50.13    帧7: 84.88
  帧2: 58.96    帧5: 82.89    帧8: 48.14

GT RMS 范围: 28.94 ~ 97.60 (3.4 倍变化!)
GT 帧间 CoV: 36.1%
```

**静态信道下 GT 的 RMS 不应该有 3.4 倍的帧间变化！**

但 GT 的频域**形状**（per-SC pattern）是一致的：

```
GT 帧间 NMSE（GT 自己跟自己比, 帧0为参考）:
  GT[0] vs GT[1]: NMSE = -41.19 dB, |α|=0.85, ∠α=+65°
  GT[0] vs GT[2]: NMSE = -43.37 dB, |α|=1.22, ∠α=+117°
  GT[0] vs GT[3]: NMSE = -41.53 dB, |α|=0.60, ∠α=+165°
  GT[0] vs GT[6]: NMSE = -45.50 dB, |α|=2.01, ∠α=-141°
  ...
```

GT 形状一致 (-41 ~ -48 dB NMSE)，但全局 α 的幅度 (0.60~2.01) 和
相位 (-164°~+165°) 帧间剧烈变化。

原因: Sionna 在 speed=0 时保持路径时延/增益结构不变，
但每帧重新采样全局随机相位/幅度。

#### SRS 帧间变化

```
SRS 每帧 RMS (rx0, tx0):
  帧0: 10320   帧3: 10338   帧6: 10334
  帧1: 10283   帧4: 10517   帧7: 10385
  帧2: 10537   帧5: 10398   帧8: 10462

SRS RMS 范围: 10283 ~ 10537 (±1.2% 变化, 非常稳定)
```

SRS 的 RMS 非常稳定（静态信道 → int16 LS 估计的总能量一致）。

但 SRS 的频域**形状**帧间变化很大：

```
SRS 帧间 NMSE（SRS 自己跟自己比, 帧0为参考）:
  SRS[0] vs SRS[1]: NMSE = -14.86 dB
  SRS[0] vs SRS[2]: NMSE =  +5.46 dB  ← 完全不同！
  SRS[0] vs SRS[6]: NMSE = -17.06 dB
```

SRS 形状帧间变化 -17 ~ +5 dB，远差于 GT 的 -41 ~ -48 dB。
这说明 OAI 的 int16 处理链路在不同帧产生了不同的 per-SC distortion pattern。

#### SRS/GT 比值的帧间不一致

```
SRS/GT 全局比值:
  帧0: 212.8   帧3: 357.2   帧6: 105.9
              (GT 最小帧)      (GT 最大帧)

比值范围: 105.9 ~ 357.2 (3.4 倍变化, 完全由 GT 变化驱动)
```

#### 逐子载波误差分析

在帧0 (TA-OFF, STO=0.00, 最好帧) 的 rx0/tx0 天线对上：

```
Per-SC alpha (归一化后):
  |alpha_k/alpha_mean| CoV = 17.4%
  angle(alpha_k/alpha_mean) std = 12.0°

信道增益: |H_GT| 范围 [48.1, 48.8] (±0.7%, 信道几乎完全平坦)
```

信道本身是平坦的，但 per-SC alpha 仍有 17.4% CoV。
这说明误差不来自信道的频率选择性，而是处理链路本身的 per-SC 系统性偏差。

误差最差的 SC 集中在特定频率位置（SC 435-440, 529-534, 659-663），
这些位置跨帧不一致（std > 3 dB），表明误差是数据相关的，不是固定 pattern。

### 8.10 -7 dB Floor 的真正来源

```
-7 dB = GT 参考信号质量极限 + int16 处理链路 per-SC distortion

具体分解:
  1. GT 帧间全局幅度/相位变化 3.4 倍 (CoV=36%)
     → per-frame alpha 必须吸收这个变化 (α 从 103 到 340)
     → alpha 估计本身引入误差

  2. SRS 帧间形状变化大 (帧间 NMSE 最差 +5 dB)
     → int16 FFT + LS 处理链路对不同帧产生不同的 per-SC distortion
     → 这些 distortion 无法被单一全局 alpha 消除

  3. 结果: per-SC alpha CoV ≈ 14-17%（幅度）+ 12°（相位）
     → NMSE ≈ CoV² + sin²(phase_std)
     → ≈ 0.03 + 0.04 ≈ 0.07 ≈ -12 dB (单天线对理论值)
     → 合并全部天线对 + 帧间变异 → 实测 -7 ~ -8 dB

核心洞察:
  -7 dB 不是 OAI 信道估计精度的极限，
  而是 "Sionna float64 GT" vs "OAI int16 LS" 这两个不同处理链路
  之间的固有数值差异。

  类比: 用一把每次测量都会随机缩放 3.4 倍的尺子去量一根稳定的木棍，
  然后说"测量误差 20%"。误差不是来自木棍 (SRS)，而是来自尺子 (GT)。

因素排除完整表（修正版）:
  ✗ filt8 插值:      pilot vs midpoint NMSE 差 0.25 dB        → 不是
  ✗ int16 量化:      有效位 14.2/15, SNQR >> 7 dB             → 不是
  ✗ TA/STO 抖动:     TA-OFF (STO≈0) 仍然 -7 dB                → 不是
  ✗ N_TA offset:     per-frame STO 已自动吸收                  → 不是
  ✗ slot_id 失配:    当前数据已能正确配对                      → 不是
  ✗ Per-SC bias:     非平稳，static calibration 反而恶化       → 不可简单消除
  ✓ GT Doppler 残余: speed=0 时实际速度 0.08m/s → f_d≈0.93Hz  → 根因（下面详述）
  ? SRS per-SC 形变: 帧间 |alpha|≈0.5 pattern                   → 待查

根本解决方向（最终版，见 Section 8.14）:
  1. 修 Proxy 速度归零: speed=0 时 velocity 精确为零         → 已实施 ✓
  2. 验证修复效果                                             → 已完成 ✓ (8.12)
  3. float 仿真排除 filt8 假说                                → 已完成 ✓ (8.13)
  4. 最终结论: floor 来自 float64 GT vs int16 SRS 坐标系差异   → 见 8.14
```

### 8.11 GT Doppler 残余追踪（2026-05-12 23:30, 最终 root cause）

#### 发现过程

1. 检查 GT 原始 .npz 文件:
   - 同一 batch 内 GT RMS 非常稳定: 45.3-46.8, ±1.6%
   - 帧间 NMSE = -40 ~ -55 dB（形状极一致）

2. 但 align_by_slot 后的 GT（slot 57000-59000）RMS 呈正弦波振荡:

```
slot 57009: RMS= 7.63   ← 谷底
slot 57440: RMS=48.48
slot 57760: RMS=58.96   ← 峰值1
slot 57920: RMS=28.93   ← 快速下降
slot 58403: RMS=97.60   ← 峰值2
slot 58723: RMS=48.15   ← 下降中
```

3. 振荡周期 ≈ 2000 slots ≈ 1 秒 → 对应 f_d ≈ 1 Hz

#### Root cause

v8.py ChannelProducer 第 2402 行:

```python
_speed_mag = _tf.abs(_tf.random.normal(
    shape=[batch_size, N_UE], mean=Speed_local, stddev=0.1))
```

当 Speed_local=0 时: 实际速度 = |N(0, 0.1)| ≈ 0.08 m/s（半正态分布）

```
f_Doppler = v/λ = 0.08 / (c/f_c)
  f_c = 3.5 GHz → λ = 0.086 m
  f_d = 0.08/0.086 ≈ 0.93 Hz → 周期 ≈ 1.08 秒
```

信道在 attach_stable 结束后进入动态模式，sample_times 递增。
即使 speed=0，0.93 Hz 的 Doppler 也会让信道随时间正弦变化。

**"静态"信道根本不是静态的！GT RMS 在 28.93~97.60 之间正弦振荡。**

虽然 per-frame alpha 吸收全局幅度/相位变化，
但多径信道的 Doppler 对不同子载波的影响不完全一致
（不同路径到达角不同 → 不同 Doppler shift → per-SC 相位漂移），
这导致 per-SC alpha CoV = 14-17%。

#### 已实施修复

v8.py ChannelProducer 速度生成逻辑:

```python
# 修复前 (所有 speed 值都有 stddev=0.1 随机抖动):
_speed_mag = _tf.abs(_tf.random.normal(mean=Speed_local, stddev=0.1))

# 修复后 (speed=0 时精确归零):
if Speed_local == 0 or Speed_local == 0.0:
    velocities = _tf.zeros([batch_size, N_UE, 3])
else:
    # 原有逻辑保持不变
```

#### 预期效果

修复后 speed=0 的实验:
- GT RMS 应该完全稳定（无 Doppler 振荡）
- GT 帧间 NMSE 应该 = -inf dB（完全相同）
- SRS vs GT 的 NMSE 应大幅改善（per-SC CoV 应从 14% 降至 ~1%）
- 预期 NMSE 由 noise-limited 决定: ~-20 dB @ SNR=20dB

---

### 8.12 Doppler 修复后验证实验 (20260513_000323)

**实验条件**: `UE_SPEED=0`, `SRS_ESTIMATOR=legacy`, `SNR=20 dB`, TA 已启用
**数据**: 27 GT files (2686 slots), 1 SRS bin (100 frames)
**确认**: proxy.log 中出现 `[v8 ChannelProducer] velocities: EXACTLY zero (static channel)`

#### GT 稳定性验证 — 修复成功

| 指标 | 修复前 (speed=0 有 Doppler) | 修复后 (speed=0 精确零速) |
|---|---|---|
| GT RMS | min=28.94, max=97.60 (3.4x) | **45.254833 (全部相同)** |
| GT RMS CoV | 14-17% | **0.00%** |
| GT inter-frame NMSE | -41 ~ -48 dB | **-300 dB (bit-perfect)** |
| GT 行为 | 类正弦振荡 (Doppler ~0.93 Hz) | **完美静态** |

修复彻底消除了 GT 的 Doppler 残余。2686 个 slot 横跨 27000 slots 时间跨度，RMS 完全一致到浮点精度。

#### NMSE 结果 — 揭示 int16 处理链路的 STO 敏感性

```
NMSE LS-aligned overall: +0.88 dB
per-frame: p10=-4.88, p50=-0.86, p90=+12.90 dB
```

独立 per-frame 分析（以唯一静态 GT[0] 为参考，含 STO 修正）:

```
NMSE: mean=-3.14, p10=-6.17, p50=-3.46, p90=-0.38
STO (samples): mean=-2.7, std=0.8, range=[-4.3, -1.3]
|alpha|: mean=106, range=[8, 149]
```

**Per-frame NMSE 与 STO 的强相关**:

| STO 范围 (samples) | 帧数 | |alpha| 典型值 | NMSE 典型值 |
|---|---|---|---|
| -4.3 ~ -3.8 | 11 | 145-149 | **-6.3 ~ -6.8 dB** |
| -3.8 ~ -3.0 | 22 | 122-125 | -3.5 dB |
| -3.0 ~ -2.6 | 10 | 144-146 | -5.8 ~ -6.1 dB |
| -2.6 ~ -2.3 | 17 | 133-136 | -4.4 dB |
| -2.3 ~ -2.0 | 9 | 94-105 | -1.7 ~ -2.2 dB |
| -2.0 ~ -1.0 | 31 | 8-74 | **-0.01 ~ -0.9 dB** |

#### 关键发现: STO-dependent int16 处理链路失真

**同一 STO 组内，SRS 帧间高度一致** (intra-group NMSE):

| STO 范围 | 帧数 | intra-group NMSE p50 |
|---|---|---|
| [-5.0, -3.8) | 11 | **-17.2 dB** |
| [-3.8, -3.0) | 22 | **-18.4 dB** |
| [-3.0, -2.6) | 10 | **-18.2 dB** |
| [-2.6, -2.3) | 17 | **-18.1 dB** |
| [-2.3, -2.0) | 9 | **-19.1 dB** |
| [-2.0, -1.0) | 31 | **-16.4 dB** |

每个 STO 组内 SRS 形状非常一致（-16 ~ -19 dB），但不同 STO 组之间形状差异巨大（cross-group p50 = -1.5 dB）。

不同 STO 值导致信号到达时刻与 FFT 窗对齐关系不同，OAI 的 int16 处理链路（FFT + LS 乘法 + filt8 乘加）在不同对齐下产生不同的量化/截断 pattern。

### 8.13 Float 仿真验证 — 推翻 filt8 假说 (20260513_001700)

**实验**: `test_sinc_interpolation.py` — 用修复后的完美静态 GT 数据，在纯 float64 精度下模拟 filt8/sinc/DFT 插值，对比不同 STO 下的性能。

#### Part 1: float64 仿真 (GT + SNR=20dB 噪声, 无 int16)

| 方法 | STO=0.0 | STO=-1.5 | STO=-2.7 | STO=-4.0 |
|---|---|---|---|---|
| filt8_legacy | -22.2 dB | -22.4 dB | -22.5 dB | -22.2 dB |
| filt8_opt | -21.2 dB | -21.2 dB | -21.2 dB | -21.2 dB |
| sinc_W4 | -20.5 dB | -20.5 dB | -20.5 dB | -20.5 dB |
| sinc_W8 | -20.2 dB | -20.2 dB | -20.2 dB | -20.2 dB |
| oracle (无插值) | -20.0 dB | -20.0 dB | -20.0 dB | -20.0 dB |

**结论 1**: **在 float 精度下，所有方法都是 ~-20 dB（noise-limited），差异 < 2 dB。**
**结论 2**: **STO 对 float 插值完全无影响** — 所有方法在 8 个 STO 值下性能一致。

#### Part 2: 无噪声纯插值精度

| 方法 | NMSE (无噪声) |
|---|---|
| filt8_legacy | -28.9 dB |
| filt8_opt | -85.3 dB |
| sinc_W1~W8 | -84 ~ -85 dB |

filt8_legacy 的 3 点平滑引入 -28.9 dB 误差（对应 Section 2 的分析），
但这远小于实测的 -6.8 dB。filt8_opt/sinc 近乎无损。

#### Part 3: 实际 SRS 数据对比

| 数据源 | NMSE p50 |
|---|---|
| 实际 SRS (OAI int16 filt8) | **~0 dB** |
| sinc8 (float GT 插值) | **-72 dB** |

实际 OAI 输出 (0 dB) 与 float 仿真 (-22 dB) 差距 **22 dB**。
这个差距完全来自 int16 处理链路，不是插值方法。

#### 决定性推翻

| 假说 | float 仿真 | 实测 | 结论 |
|---|---|---|---|
| "filt8 系数是 -7 dB 根因" | filt8 达到 -22 dB | -6.8 dB | **否**: 系数不是问题 |
| "STO 通过 filt8 放大" | filt8 完全 STO-invariant | STO 影响 20 dB | **否**: STO 敏感性来自 int16 |
| "换 sinc 能突破 floor" | sinc = -20 dB, filt8 = -22 dB | — | **否**: 性能相当 |

### 8.14 -7 dB Floor 最终结论（2026-05-13 00:20, 所有假说已闭环）

```
因素排除完整表（最终版）:
  ✗ filt8 插值系数:   float 仿真 -22 dB, STO-invariant           → 不是
  ✗ 替换 sinc/Wiener: float 下性能相当 (-20 vs -22 dB)           → 无帮助
  ✗ int16 单步 SNQR:  每步 ~60 dB, 远高于 -7 dB                  → 单独不是
  ✗ TA/STO 抖动:      float 下 STO 无影响                         → 不是（通过 int16 间接影响）
  ✗ GT Doppler:       已修复 (v8.py velocity=0), GT 完美静态      → 已消除
  ✗ slot_id 失配:     当前数据已能正确配对                         → 不是
  ✗ Per-SC bias:      非平稳, calibration 反而恶化                → 不可简单消除

  ✓ int16 处理链路累积失真:
    OAI 全链路 (FFT + LS除法 + filt8乘加) 在 int16 定点下
    产生 STO-dependent 的 per-SC distortion pattern,
    这是 float 仿真无法复现的。
    → 这是 "Sionna float64 GT" vs "OAI int16 LS" 的固有数值差异
    → -7 dB 是评估方法的精度极限，不是 OAI 估计算法的真实误差

核心洞察:
  -7 dB 不是 OAI 的信道估计精度有问题，
  而是用 float64 GT 去评估 int16 输出时，
  两个数值系统之间的固有不可消除差异。

  如果要突破这个 floor，不能改 OAI（filt8/sinc/Wiener），
  而是要改 GT 的生成方式——让 Proxy 模拟 OAI 的 int16 链路生成 GT,
  使 GT 和 SRS 处于同一数值坐标系。
```

#### 下一步方向

| 方案 | 做什么 | 预期效果 |
|---|---|---|
| **A: int16-matched GT** | Proxy 在 GPU 端模拟 OAI 的 int16 FFT + LS 行为生成 GT | GT-SRS 同一坐标系, NMSE 应降至 noise-limited (-20 dB) |
| **B: float OAI 通路** | 在 OAI 添加 float32 信道估计分支用于评估 | 消除 int16 量化, 直接与 float GT 比较 |
| **C: 2D MMSE (教授方向)** | 用时频 2D MMSE 滤波器替代 LS+filt8 | 改善真实信道估计性能（与 -7 dB floor 问题正交） |

方案 C（教授 4/29 提出的 2D MMSE 方向）不解决评估 floor 问题，
但解决真实的信道估计性能提升，这才是研究目标。
-7 dB floor 是评估工具的精度极限，不应该阻碍 2D MMSE 算法开发。

详细方案见独立文档: `INT16_MATCHED_GT_PLAN.md`

---

### 8.15 根因深化：int16 信号幅度过低（2026-05-13 13:00, 8.14 的修正）

8.14 的结论 "int16 处理链路累积失真" 是正确方向，但不够具体。
05-13 的 libdfts.so + SRS 帧间一致性分析给出了**可量化的根因**：

#### 核心发现

```
SRS 帧间一致性分析 (speed=0, SNR=20dB, 50 帧):
  帧间 correlation = 0.54  (预期 0.99)
  帧间 self-NMSE   = -0.9 dB (预期 -20 dB)
  有效 SNR          = 2.3 dB (损失 17.7 dB!)
```

**直接原因**：UE IFFT (idft2048) 输出 RMS = 200，仅占 int16 (±32767) 的 **0.6%**。
int16 FFT 在这个幅度下只有 **7.6 bit 有效精度**。

#### int16 链路逐级精度

```
级别              信号 RMS    占 int16%    有效位数
[0] X_ref 频域      362       1.1%        8.5 bit
[1] UE IFFT 时域    200       0.6%        7.6 bit   ← 精度瓶颈
[2] Proxy→shm      ~564      1.7%        9.1 bit
[3] OAI FFT 频域   14000     42.7%       13.8 bit  ← FFT 放大后才 OK
[4] LS >>9 输出     9900     30.2%       13.3 bit
[5] filt8 输出      9900     30.2%       13.3 bit
```

#### 为什么级别 [1]-[2] 的低幅度导致级别 [3]-[5] 的大误差？

int16 FFT 的级联 butterfly (radix-2/4, 每级 >>1 或 mulhrs) 在低幅度输入下
不产生简单的加性白噪声，而是产生 **per-SC 相关的失真 pattern**。
此 pattern 依赖于时域信号的具体数值（包括热噪声的实现），
因此每帧的失真 pattern 不同 → 帧间 correlation 急剧下降。

#### 对 8.14 结论的修正

| 8.14 结论 | 修正 |
|-----------|------|
| "-7 dB 是评估方法的精度极限" | **部分正确**：Sionna GT 坐标系差异贡献 ~24 dB (帧平均 GT 从 +33→-6dB) |
| "不是 OAI 估计算法的真实误差" | **需修正**：int16 链路确实导致 per-frame 有效 SNR=2.3dB，这是 OAI 的真实精度限制 |
| "改 GT 生成方式能解决" | **不够**：即使 GT-SRS 完美对齐（帧平均 GT），self-NMSE 仍为 -0.9 dB |
| "passthru ≈ legacy 说明 filt8 不是根因" | **正确但解读需深化**：瓶颈在 LS 之前（int16 FFT），不在 filt8 |

#### 修正后的因素排除表

```
因素排除完整表（05-13 最终版）:
  ✗ filt8 插值系数:    float -22 dB; passthru ≈ legacy             → 排除
  ✗ 替换 sinc/Wiener:  float 下与 filt8 相当 (-20 vs -22 dB)       → 无帮助
  ✗ GT 坐标系差异:     帧平均 GT 消除后仍 -0.9 dB self-NMSE         → 不是唯一原因
  ✗ TA/STO 抖动:       STO std=0.11 samples, 相位翻转是另一原因     → 不是
  ✗ 热噪声:           SNR=40 vs 20 只改善 0.36 dB                  → 不是

  ✓ int16 信号幅度过低 (根因):
    AMP=512 → UE IFFT RMS=200 → 0.6% of int16 → 7.6 bit 有效精度
    → int16 FFT 级联量化产生 per-SC 相关失真
    → 每帧的失真 pattern 依赖于噪声实现，导致 correlation=0.54
    → 有效 SNR = 2.3 dB (损失 17.7 dB)
    → 这是 OAI SRS 链路在当前 AMP 设置下的固有精度限制

对策:
  短期: 用 SRS 帧平均做 GT (同一坐标系), 2D MMSE 时域平均提升有效 SNR
  中期: 考虑 Proxy PL gain (+14dB max) 或 AMP 调整
  长期: OAI 上游讨论 SRS AMP 设置是否合理 (真实硬件有 AGC, rfsim 没有)
```
