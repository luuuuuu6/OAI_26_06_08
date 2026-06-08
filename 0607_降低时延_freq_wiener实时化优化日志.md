# SRS true-2D 复杂度/时延优化日志 —— freq_wiener 5042 → ~190µs(26.5×,逐位等价)

| 字段 | 值 |
|------|----|
| 日期 | 2026-06-04 ~ 06(整理 2026-06-07) |
| 主题 | 把 true-2D 频域 Wiener(Stage1)从 **超 SRS 周期(5042µs)** 降到 **TTI 预算内(~190µs,实测 ~136µs)**,使 SISO/4×4 MIMO 实时可行;全程 **bit-exact**(golden ≤1-2 LSB 守门) |
| 动机 | 教授 6-04 录音「要求 A」:每 TTI CPU 占用 + 实时性 + MIMO 降本 |
| 判据 | `ADDED(freq+pkf+wiener) per-SRS avg ≪ 500µs`(μ=1 单 TTI 预算);`max < 5ms`(SRS 周期 @p=10) |
| 关联 | `2D_MMSE_项目报告.md` §10;`0605_SRS信道预测_方向A_日志.md` §4.11.9 |
| 状态 | **达成**:freq_wiener 实测 avg ~136µs(max 510µs)≪ 500µs TTI;total_wall ~378-438µs;golden_freq 全 PASS(worst 1.0 LSB) |

---

## 0. 一句话结论
true-2D 频域 Wiener 初版 **5042µs/SRS(超 5ms 周期 → MIMO 必爆)**。三步**纯算法**优化(求逆一次 + twiddle 表 + 权重缓存),**始终启用、无开关、逐位等价**,叠乘 **26.5×** → 进 TTI 预算。瓶颈经 `freq-breakdown` 细分定位为**访存 bound 的 robust noise-floor 排序(~100µs)**,不是 IFFT(~3µs);多核 fan-out 本机无益(访存墙),真正的 MIMO 降本杠杆是 Option B 跨天线共享 R。

---

## 1. 计时基建(`SRS_TIMING=1` 门控,默认零开销)

两级计时,默认关(`srs_tmg_on=-1`),开了才有任何成本:

**① per-stage + total_wall**(`nr_ul_channel_estimation.c`):`clock_gettime(CLOCK_MONOTONIC)` 累计 `legacy_filt / freq_wiener / pkf_time / wiener_upd / wiener_pred / total_wall`,每 200 次 SRS 打印 avg/max(µs),并算 `ADDED(freq+pkf+wiener)` 对比 500µs TTI 预算。

```82:104:DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c
static inline void srs_tmg_add(int s, double ns)
{
  if (srs_tmg_on <= 0)
    return;
  srs_tmg_ns[s] += ns;
  srs_tmg_cnt[s]++;
  if (ns > srs_tmg_max[s])
    srs_tmg_max[s] = ns;
  /* log periodically, keyed on TOTAL (added once per estimation in the main thread
   * -> a clean trigger even when the per-block timers race under a threaded pool). */
  if (s == SRS_TMG_TOTAL && srs_tmg_cnt[SRS_TMG_TOTAL] > 0
      && (srs_tmg_cnt[SRS_TMG_TOTAL] % 200 == 0)) {
    double add_avg = 0.0;
    for (int i = SRS_TMG_FREQ; i < SRS_TMG_TOTAL; i++)   /* exclude TOTAL from the sum */
      if (srs_tmg_cnt[i])
        add_avg += srs_tmg_ns[i] / (double)srs_tmg_cnt[i];
```

- `total_wall` = 整次 per-link fork-join 墙钟(主线程测,无竞态,串/并都有效);per-block 在 worker 内累加,仅 `TPOOL=n`(inline)时无竞态。

**② freq 内部细分 `freq-breakdown`**(`nr_srs_mmse.c`):把 ~190µs 拆成 `ifft / noise_floor / R_build / stage3`,用来定位真瓶颈(见 §5)。

脚本 [`complexity_native.sh`](DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/complexity_native.sh) 自动跑 legacy / true2d / true2d+predict 三档。

---

## 2. 瓶颈定位(初测)

| stage | 初测 avg | 备注 |
|---|---|---|
| `legacy_filt` | ~4 µs | 公共基线(所有 mode 都算) |
| **`freq_wiener`(Stage1)** | **~5042 µs(max 7712)** | **瓶颈 100% 在此,超 5ms SRS 周期 → MIMO 必爆** |
| `pkf_time`(Stage2) | ~6.5 µs | 时间级很便宜 |

根因:原实现对**每个活跃 SC(~1248 个)**各调一次 `solve_complex_system`(窗内 W=16 的复 Hermitian 线性解,O(W³))→ 1248 × O(16³) 重复求解。

---

## 3. 三步算法加速(均 bit-exact,始终启用,取代原实现;原路径仅作奇异回退)

| 步骤 | 位置 / 函数 | 机理 | freq_wiener | 单步 | 累计 |
|---|---|---|---|---|---|
| 原始 | — | 每 SC 各 `solve_complex_system`(O(W³)) | 5042µs | — | 1× |
| **① 求逆一次** | Stage3 `invert_complex_system` | 窗内 Toeplitz `A=R[\|i-j\|·k_tc]+noise·δ` **平移不变** → 整次只求逆一次,各 SC 只 `w=A⁻¹·b`(O(W²)) | 993µs | **5.1×** | 5.1× |
| **② twiddle 表** | Stage2 `g_twiddle_cos/sin` | `R(dk)=Σ clean·e^{-j2π dk n/N}` 旋转因子是信道无关常数 → 预存表 + 稀疏 PDP,消 per-bin `cos/sin` | 433µs | 2.3× | 11.6× |
| **③ 权重缓存** | Stage3 `offset%k_tc` | 内部 SC 权重只取决于 `offset%k_tc`(共 k_tc 种)→ 懒缓存,边带逐个算 | **190µs** | 2.3× | **26.5×** |

`5.1 × 2.3 × 2.3 ≈ 26×`,**① 贡献最大**(消掉 O(W³)×1248 的重复求解)。

### 3.1 ① 求逆一次(`invert_complex_system`)
窗矩阵 `A[row][col]=R(|row-col|·k_tc)+noise·δ` 只依赖 `row-col`(Toeplitz、平移不变),与 SC 位置无关 → 不再每 SC 解一次,而是整窗 Gauss-Jordan 求逆一次,各 SC 退化成 O(W²) 的 `w=A⁻¹·b` 矩阵-向量乘:

```1102:1130:DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_mmse.c
  /* === Stage 3: per-target MMSE solve using complex R_table ===
   * Weight reuse: the window matrix A[row][col] = R(|row-col|*k_tc) + noise*delta
   * is SHIFT-INVARIANT (depends only on row-col, not on start_pilot/offset), so it
   * is built & inverted ONCE here and each target's weights become w = Ainv*conj(b)
   * (an O(window^2) mat-vec) instead of an O(window^3) solve_complex_system per
   * subcarrier. Numerically identical to the per-target solve (inv vs solve
   * roundoff ~1e-13; verified offline windowed_lmmse_reuse, diff < 1e-9).
   * The per-target RHS conj(b) still varies with target_dk; apply stays:
   *   out = sum_i conj(w_i)*y_i == b^T A^-1 y (the correct Wiener-Hopf LMMSE). */
  const int span = (int)num_pilots * (int)k_tc;
  double wnorm2_sum = 0.0;   /* true2d: accumulate ||weights||^2 for R_meas */
  int wnorm2_cnt = 0;
  _mft_t = mft_now();

  double complex A_const[SRS_MMSE_MAX_WINDOW][SRS_MMSE_MAX_WINDOW];
  double complex A_inv[SRS_MMSE_MAX_WINDOW][SRS_MMSE_MAX_WINDOW];
  for (int row = 0; row < window_pilots; row++) {
    for (int col = 0; col < window_pilots; col++) {
      const int pp_dk = (row - col) * (int)k_tc;
      const int abs_pp = pp_dk >= 0 ? pp_dk : -pp_dk;
      if (abs_pp > max_delta_k)
        A_const[row][col] = 0.0;
      else
        A_const[row][col] = pp_dk >= 0 ? R_table[abs_pp] : conj(R_table[abs_pp]);
      if (row == col)
        A_const[row][col] += noise_norm;
    }
  }
  const int ainv_ok = (invert_complex_system(window_pilots, A_const, A_inv) == 0);
```

`invert_complex_system`(Gauss-Jordan + 部分主元,`nr_srs_mmse.c:665`)是新增的"一次求逆"函数;奇异(`pivot<1e-12`)时回退原 per-target `solve_complex_system`。

### 3.2 ② twiddle 表 + 稀疏 PDP(`R_build`)
`R(dk)=Σ_n clean(n)·e^{-j2π dk·n/N}` 的旋转因子 `e^{-j2π m/N}` 是**与信道无关的常数**。预存 `g_twiddle_cos/sin[N]`(单线程预热),再把 PDP 扫成稀疏 `(idx,val)` 列表(只留 `clean>0` 的 bin),每个 `dk` 查表累加,消除 per-bin `cos/sin`:

```1003:1013:DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_mmse.c
  const int twiddle_mask = SRS_MMSE_PDP_IFFT_SIZE - 1;   /* N is a power of two */
  for (int dk = 0; dk <= max_delta_k; dk++) {
    double acc_r = 0.0;
    double acc_i = 0.0;
    for (int k = 0; k < nnz; k++) {
      const int m = (dk * nz_idx[k]) & twiddle_mask;
      acc_r += nz_val[k] * g_twiddle_cos[m];
      acc_i -= nz_val[k] * g_twiddle_sin[m];   /* exp(-j*phase): -sin term */
    }
    R_table[dk] = acc_r + I * acc_i;
  }
```

### 3.3 ③ 权重缓存(残基类,`offset%k_tc`)
内部(非边带)SC 的 `target_dk = (offset - center·k_tc) + (W/2-row)·k_tc` 只依赖 `offset%k_tc` → 权重 `w=A⁻¹·conj(b)` 只有 `k_tc` 种取值。懒缓存(按残基类 `s=offset%k_tc`),命中即跳过 O(W²) mat-vec;边带 SC 仍逐个算:

```1156:1181:DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/nr_srs_mmse.c
    if (can_cache && interior && w_cached[s]) {
      /* cache hit: reuse the residue-class weight vector (no mat-vec) */
      w_active = w_cache[s];
    } else if (ainv_ok) {
      /* Wiener-Hopf RHS = conj(R_hy); weights = A_inv * rhs (O(W^2) mat-vec). */
      double complex rhs[SRS_MMSE_MAX_WINDOW] = {0};
      for (int row = 0; row < window_pilots; row++) {
        const int pilot_row_offset = (start_pilot + row) * (int)k_tc;
        const int target_dk = offset - pilot_row_offset;
        const int abs_t = target_dk >= 0 ? target_dk : -target_dk;
        if (abs_t > max_delta_k)
          rhs[row] = 0.0;
        else
          rhs[row] = target_dk >= 0 ? conj(R_table[abs_t]) : R_table[abs_t];
      }
      for (int i = 0; i < window_pilots; i++) {
        double complex acc = 0.0;
        for (int j = 0; j < window_pilots; j++)
          acc += A_inv[i][j] * rhs[j];
        weights[i] = acc;
      }
      w_active = weights;
      if (can_cache && interior) {
        memcpy(w_cache[s], weights, sizeof(double complex) * (size_t)window_pilots);
        w_cached[s] = 1;
      }
    } else {
```

---

## 4. 实测结果(native rfsim,真实 gnb.log,`SRS_TIMING=1`)

`grep -E "SRS timing|freq-breakdown" logs/native_true2d_*/gnb.log`:

```
[SRS timing] legacy_filt  avg=3.49 us   max=15.48 us
[SRS timing] freq_wiener  avg=136.42 us max=509.84 us
[SRS timing] pkf_time     avg=6.85 us   max=31.05 us
[SRS timing] total_wall   avg=378~438 us max=986 us
[SRS timing] ADDED(freq+pkf+wiener) per-SRS avg=131~152 us  (TTI budget 500 us @mu=1)

[SRS freq-breakdown] ifft         avg=2.82 us   max=18.18 us
[SRS freq-breakdown] noise_floor  avg=99.91 us  max=266.45 us
[SRS freq-breakdown] R_build      avg=43.55 us  max=104.32 us
[SRS freq-breakdown] stage3       avg=60.27 us  max=151.52 us
```

**预算判据达成**:`ADDED ~131-152µs ≪ 500µs TTI`;`freq_wiener avg 136µs`(初版 5042µs)。4×4 MIMO ≈ 3ms 进 5ms 周期 → **SISO/4×4 实时达成**。

**关键洞察(细分暴露真瓶颈)**:190µs 大头是 `noise_floor`(robust 迭代-trim 排序,**~100µs**)+ `stage3`(~60µs)+ `R_build`(~44µs);`ifft` 仅 **~3µs**(纠正了"IFFT 是大头"的直觉)。→ **访存/带宽 bound,非 CPU bound**(决定了 §6 多核为何无益、Option B 为何是正确杠杆)。

---

## 5. 正确性验证(bit-exact 守门)

**① C 单元测试** `c_unit_tests/run_all.sh` 的 `golden_freq`(C double vs float64 recipe 镜像,共享 ref-IDFT):

```
[ok ] ofdm=256 k_tc=2 n_pil=128 ...  max=0.0 LSB  agreement=-300.00 dB
[ok ] ofdm=256 k_tc=4 n_pil=64  ...  max=1.0 LSB  agreement=-101.29 dB
[ok ] ofdm=512 k_tc=2 n_pil=256 ...  max=0.0 LSB  agreement=-300.00 dB
PASS: worst max LSB deviation 1.0 (tol 2 LSB)
```

多数 −300dB(完全 bit-exact),worst 1.0 LSB(定点末位)。

**② 离线变体 assert**(`test_srs_2d_offline.py`,与原 per-target 解逐项比):求逆一次 diff **5.9e-13**、twiddle **3.1e-10**、权重缓存 **0.00**(完全相同)。

→ 三步优化**算法层逐位等价**,不是近似,无精度代价。

---

## 6. 配套降本(辅助,非本次时延主线)

- **维度自适应(始终启用)**:per-link 状态 RX 维运行时动态(pointer-to-array 堆分配),解除写死的 4×4 上限 → 64rx 功能可跑。golden 4×4 逐位不变;ASAN/UBSAN 自测无越界/泄漏。**坑:gNB `nb_antennas_rx = pusch_AntennaPorts`,测 MIMO 须同设 `NB_RX` 与 `pusch_AntennaPorts`**。
- **多核 fork-join(`SRS_PARALLEL=1`,默认关)**:每 RX 天线一个线程池 task。**本机(Threadripper 7960X)无墙钟收益**——freq_wiener 是访存 bound,pin 远核(跨 CCD/Infinity Fabric)反而 per-link 185→280µs。串/并 NMSE 等价(Δ0.03dB)。保留(零风险),但非有效杠杆。
- **Option B 跨天线共享 R(`SRS_SHARE_R=1`,默认关,near-lossless)**:同 UE 各 RX 天线 PDP 基本相同 → ref 天线 `nr_srs_mmse_freq_filter_build` 算一次 R,其余 `_reuse` 跳过 Stage1/2(含那 ~100µs noise_floor)只做 Stage3 → 复用链路 190→**~69µs**;2-rx total_wall 574→438µs(−136µs)。四重验证 near-lossless(离线真 CDL A–E ΔNMSE ≤0.053dB;native 固定-seed 0.01dB)。**这才是 massive-MIMO 的正确降本路**(绕开访存墙)。

---

## 6.5 后续叠加:延迟域去噪 gate 的时延(`SRS_DENOISE_PEAK_DB`,见 `0608_频域去噪进C` 日志)

2026-06-08 在 PKF+legacy-blend 之后新增 `nr_srs_delay_gate`(IDFT→峰值相对掩码→DFT 去噪,默认关 bit-exact)。**新增计时 bucket `denoise_gate`**:

| 项 | 实测 |
|---|---|
| `denoise_gate` per-link | **~16-17µs**(2 次定点 FFT + O(N) 掩码) |
| 关键:**不触发那 ~100µs 鲁棒噪底排序** | 噪底用延迟谱中段 [N/2,3N/4) 均值(O(N) 无 qsort) |
| 2×2(4 链路)total_wall | 1005→1063µs(+58µs) |
| 4×4(16 链路)total_wall | 4232→4232µs 量级(+~210µs) |

**与 Option B 叠加(4×4,native,`SRS_DENOISE_PEAK_DB=30 SRS_SHARE_R=1`)**:`freq_wiener` 188→**91µs**、`total_wall` 4232→**2612µs(max 3099)**;去噪开销不变(16.6µs/链路)。→ **去噪与 Option B 正交可叠加;Option B 给 4×4(+去噪)腾出充足实时余量(2.6ms ≪ 5ms)。**

---

## 7. 复现与脚本

旋钮(env,默认全关):

| env | 作用 |
|---|---|
| `SRS_TIMING=1` | per-stage µs + `freq-breakdown` + `total_wall` |
| `SRS_PARALLEL=1` + `TPOOL="0,1,..."` | 多核 per-天线 fork-join(`TPOOL=n`=串行) |
| `SRS_SHARE_R=1` | Option B 跨天线共享 R(强制串行) |
| `SRS_DENOISE_PEAK_DB=30` | 延迟域去噪 gate(见 `0608` 日志;+~16µs/链路,默认关) |
| `NB_RX`/`NB_TX`(+ `pusch_AntennaPorts=NB_RX`,launch 已绑) | MIMO 天线数 |

复现:
```bash
sudo SRS_TIMING=1 NB_RX=2 [SRS_PARALLEL=1 TPOOL=20,22] [SRS_SHARE_R=1] \
     bash launch_native.sh -e true2d -cir cir/cir_cdl_c_s60.bin [-nd -6] -d 120
grep -E "SRS timing|freq-breakdown" ../../logs/native_true2d_*/gnb.log
# 正确性:
cd ../../openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/c_unit_tests && bash run_all.sh
```

## 8. 文件索引

| 文件 | 内容 |
|---|---|
| `openair1/PHY/NR_ESTIMATION/nr_srs_mmse.c` | freq Wiener:`invert_complex_system`(求逆一次,:665)、twiddle 稀疏 R_build(:1003)、权重缓存(:1156)、`freq-breakdown` 计时(:49) |
| `openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c` | per-stage 计时基建(:44)、TRUE2D dispatch、Option B/多核调度 |
| `c_unit_tests/golden_freq.py` + `test_freq_main.c` + `run_all.sh` | freq 级 C↔离线位级守门 |
| `test_srs_2d_offline.py` | 离线 recipe 镜像 + 三步变体 assert |
| `vRAN_Socket/.../complexity_native.sh` | legacy/true2d/+predict 三档计时自动对比 |
| `2D_MMSE_项目报告.md` §10 | 正式汇总 |

## 9. 结论
- **freq_wiener 5042 → ~136µs(实测,26.5× 算法),≪ 500µs TTI**,SISO/4×4 MIMO 实时达成,**全程 bit-exact**。
- 真瓶颈是**访存 bound 的 robust noise-floor 排序(~100µs)**,非 IFFT;故多核无益,**Option B(跨天线共享 R)是 massive-MIMO 的正确降本杠杆**。
- 所有优化默认行为对估计路径**逐位不变**(golden ≤1 LSB),近似项(Option B)默认关、NMSE-delta 守门。
