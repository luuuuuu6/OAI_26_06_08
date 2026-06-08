# OAI NR SRS（探测参考信号）代码地图与项目分析

> 生成日期：2026-04-30  
> 基于 `openairinterface5g_whan` 代码库，以 **5G NR** 为核心（LTE 仅附录对照）

---

## 目录

1. [项目背景与 SRS 在本项目中的角色](#1-项目背景与-srs-在本项目中的角色)
2. [NR SRS 序列生成与频域映射](#2-nr-srs-序列生成与频域映射)
3. [NR SRS 功率控制](#3-nr-srs-功率控制)
4. [NR SRS 调度与过程控制](#4-nr-srs-调度与过程控制)
5. [NR SRS 接收与信道估计](#5-nr-srs-接收与信道估计)
6. [NR SRS 仿真与调试](#6-nr-srs-仿真与调试)
7. [项目中发现的 SRS 相关 Bug 与关键改动](#7-项目中发现的-srs-相关-bug-与关键改动)
8. [关键文件汇总](#8-关键文件汇总)
9. [GDB 调试建议](#9-gdb-调试建议)
10. [附录：LTE SRS 对照](#附录lte-srs-对照)

---

## 1. 项目背景与 SRS 在本项目中的角色

本项目（OAI Digital Twin 验证系统）的核心目标是：将 **Sionna Channel Proxy** 合成的信道 Ground Truth (GT) 与 **OAI gNB SRS 估计器**输出的信道估计进行交叉验证，以证明数字孪生系统的忠实度。

SRS 在本项目中的核心地位：

- **SRS 是唯一的上行参考信号采集点** — gNB 通过 SRS 做上行信道估计，其输出是我们与 GT 对比的唯一数据源
- **SRS Dump 系统**（`defs_gNB.h` 中的 `srs_pingpong_t`）将 gNB 估计的 SRS 信道矩阵以二进制文件 dump 到磁盘
- **GT Saver**（`v4.py` 中的 `GTBatchSaver`）保存 Sionna 合成的频域信道 H(f)，仅保存 SRS 所在的 OFDM symbol
- 后处理管线（`digital_twin_stats.py`、`q4_convergence_sweep.py`）比较 SRS dump 与 GT，计算 NMSE、SNR、PDP 等指标

**SRS 位置**：Symbol 12（0-indexed），由 `nr_radio_config.c` 中 `startPosition=1` 推导：`l₀ = 14 - 1 - 1 = 12`

**TDD 配置**：`7DL + 1Special(6DL+4UL) + 2UL(slot8, slot9)`，SRS 默认调度在 slot 8 或 slot 9

---

## 2. NR SRS 序列生成与频域映射

### 2.1 基序列生成

**文件**：`openair1/PHY/NR_REFSIG/ul_ref_seq_nr.c` / `.h`

按 TS 38.211 Section 5.2.2 生成 NR 上行低 PAPR 基序列（含 Zadoff-Chu）。`ul_ref_seq_nr.h` 定义了 `SRS_SB_CONF` 等 SRS 子载波分配和带宽相关表。

### 2.2 SRS 调制与物理层映射

**文件**：`openair1/PHY/NR_UE_TRANSPORT/srs_modulation_nr.c` / `.h`

实现 TS 38.211 Section 6.4.1.4 的完整 SRS 生成流程。

**核心函数**：

| 函数 | 作用 |
|------|------|
| `generate_srs_nr()` | NR SRS 调制与频域映射主函数 |
| `group_number_hopping()` | 序列组跳频（伪随机序列 `c(i)` 驱动） |
| `sequence_number_hopping()` | 序列号跳频（M_sc > 72 时启用） |
| `compute_F_b()` | 频域位置计算（含频率跳频机制） |
| `is_srs_period_nr()` | 周期性 SRS 发送判定 |
| `ue_srs_procedures_nr()` | NR UE 侧 SRS 完整处理过程 |

**SRS 周期表**（17 种）：`{1, 2, 4, 5, 8, 10, 16, 20, 32, 40, 64, 80, 160, 320, 640, 1280, 2560}`

**调用链**：
```
ue_srs_procedures_nr()
  → is_srs_period_nr()          // 判断当前 slot 是否发 SRS
  → generate_srs_nr()           // 基于 nfapi_nr_srs_pdu_t 配置
    → group_number_hopping()    // 伪随机序列 → 序列组号 u
    → sequence_number_hopping() // 序列号 v
    → compute_F_b()             // 频域位置
    → 从 ul_ref_seq_nr 取基序列 → 乘以 amp → 写入 txdataF[]
```

---

## 3. NR SRS 功率控制

**文件**：`openair2/LAYER2/NR_MAC_UE/nr_ue_power_procedures.c`

实现 TS 38.213 的 NR SRS 发射功率计算。

**核心函数**：`get_srs_tx_power_ue()`

**单元测试**：`openair2/LAYER2/NR_MAC_UE/tests/test_nr_ue_power_procedures.cpp`，包含 `test_srs_power` 等测试用例，覆盖 `NR_SRS_Config`、TPC 累积、`srs_PowerControlAdjustmentStates` 等参数。

---

## 4. NR SRS 调度与过程控制

### 4.1 UE 端（发送方）

**文件**：`openair1/SCHED_NR_UE/phy_procedures_nr_ue.c`

在上行处理链中调用 `ue_srs_procedures_nr()`（实现在 `srs_modulation_nr.c`）。

### 4.2 gNB 端（接收方）— 本项目最关键路径

**文件**：`openair1/SCHED_NR/phy_procedures_nr_gNB.c`

gNB 端 SRS 接收是本项目 SRS Dump 的直接上游，包含完整接收链路：

```
phy_procedures_nr_gNB()
  → generate_srs_nr()               // 生成本地参考序列（用于 LS 估计）
  → nr_get_srs_signal()             // 从 rxdataF 抽取 SRS 接收信号
  → nr_srs_channel_estimation()     // LS 信道估计 + FIR 频域插值
  → nr_est_timing_advance_srs()     // TA 估计
  → fill_srs_channel_matrix()       // 构造 SRS 信道矩阵
  → fill_srs_reported_symbol()      // 填充 SRS indication
  → check_srs_pdu()                 // 验证 SRS PDU
  → [SRS Dump 注入点]               // 写入 srs_pingpong_t 缓冲区
```

附加函数：
- `fill_srs_channel_matrix()` — 构造信道矩阵报告
- `fill_srs_reported_symbol()` — 填充 beamforming / codebook 报告
- `generate_srs_stats` — SRS 性能计数器

### 4.3 FAPI / NFAPI 接口

| 文件 | 作用 |
|------|------|
| `SCHED_NR/fapi_nr_l1.c` | `nr_fill_srs()` — 解析 NR FAPI SRS PDU |
| `nfapi/tests/p7/nr_fapi_srs_indication_test.c` | SRS indication 打包/解包一致性测试 |
| `nfapi/tests/p7/nr_fapi_ul_tti_request_test.c` | UL TTI 中 SRS PDU 逻辑测试 |

### 4.4 MAC 层调度

**文件**：`openair2/LAYER2/NR_MAC_UE/nr_ue_scheduler.c`

| 函数 | 作用 |
|------|------|
| `nr_ue_periodic_srs_scheduling()` | 周期性 SRS 调度 |
| `nr_ue_aperiodic_srs_scheduling()` | 非周期性 SRS 调度（由 DCI 触发） |

### 4.5 gNB MAC 层 SRS 调度

**文件**：`openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_srs.c`

- `nr_schedule_srs()` — gNB 侧 SRS 调度
- `configure_periodic_srs()` — 周期性 SRS 配置（slot 偏移通过 `get_ul_slot_offset()` 计算）

**文件**：`openair2/LAYER2/NR_MAC_gNB/nr_radio_config.c`

- L751：SRS slot 偏移 = `get_ul_slot_offset(fs, uid, false)` — 对 uid=0 返回第一个全 UL slot (slot 8)

---

## 5. NR SRS 接收与信道估计

### 5.1 SRS 信号抽取

**文件**：`openair1/PHY/NR_TRANSPORT/srs_rx.c`

`nr_get_srs_signal()` — 从 `rxdataF` 中按 comb 结构抽取 SRS 接收信号，供 `nr_srs_channel_estimation()` 使用。

### 5.2 信道估计 — LS + 频域 FIR 插值

**文件**：`openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c`

**核心函数**：`nr_srs_channel_estimation()`

**算法流程**：

1. **LS 估计**：对每个 SRS 子载波
   ```
   ls_estimated.r = Σ (generated_real × received_real + generated_imag × received_imag) >> bits
   ls_estimated.i = Σ (generated_real × received_imag - generated_imag × received_real) >> bits
   ```
   其中求和是在 FD-CDM 维度上做的（`fd_cdm` 个子载波累加），用于分离多端口。

2. **频域 FIR 插值**：LS 估计得到的是 comb 齿上的值，需要在齿间插值
   - comb_size=0（K_TC=2）：使用 `filt8_start/middle2/middle4/end` 系列滤波器
   - comb_size=1（K_TC=4）：使用 `filt16_start/middle/end` 系列滤波器

3. **噪声功率估计**：LS 估计值与滤波后信道的差 → 噪声功率 → SNR per RB

**注意**：未实现 MMSE 估计器。当前方案是 **LS + 平滑插值**。

### 5.3 SRS Dump 注入点（本项目新增）

**文件**：`openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c`

在 `nr_srs_channel_estimation()` 返回后，如果 SRS 信噪比 > `SRS_TWIN_SNR_THRESHOLD`（已改为 -999 以禁用门限），则将 SRS 信道估计写入 `srs_pingpong_t` 双缓冲结构，由后台 writer 线程写入 `.bin` 文件。

每帧附带元数据：RNTI、帧号、slot 号。

### 5.4 TA 估计

**文件**：`openair1/PHY/NR_ESTIMATION/nr_measurements_gNB.c`

`nr_est_timing_advance_srs()` — 在 `srs_estimated_channel_time` 上做 TA 估计。

### 5.5 头文件声明

**文件**：`openair1/PHY/NR_ESTIMATION/nr_ul_estimation.h`

声明 `nr_srs_channel_estimation()`、`nr_est_timing_advance_srs()` 等。

---

## 6. NR SRS 仿真与调试

### 6.1 NR ulsim

**文件**：`openair1/SIMULATION/NR_PHY/ulsim.c`

使用 **`-E 1`** 开启 SRS 仿真：

```bash
./nr_ulsim -n300 -s40 -E 1
```

开启后的行为：
- 向 PHY 下发 `NFAPI_NR_UL_CONFIG_SRS_PDU_TYPE`（与 PUSCH 并存时 `n_pdus=2`）
- 构造完整的 `nfapi_nr_srs_pdu_t` 配置（包括 `config_index`、`num_ant_ports`、`sequence_id` 等）
- 在 UE 侧构造 `FAPI_NR_UL_CONFIG_TYPE_SRS` 配置
- 走完整 gNB SRS 接收链路
- 统计打印 SRS 性能计数器

**CMake 注册的 SRS 专项测试**：
```
add_physim_test(5g nr_ulsim misc.test15 "SRS, SNR 40 dB" -n300 -s40 -E 1)
```

### 6.2 性能统计

gNB 在 `defs_gNB.h` 中定义了以下 SRS 计数器：
- `rx_srs_stats` — SRS 总接收时间
- `generate_srs_stats` — SRS 序列生成时间
- `get_srs_signal_stats` — SRS 信号抽取时间
- `srs_channel_estimation_stats` — SRS 信道估计时间

---

## 7. 项目中发现的 SRS 相关 Bug 与关键改动

以下是 `Change_log.md` 中记录的与 SRS 直接相关的重要发现和修复，按严重程度排列：

### 7.1 [极高] CP 长度分配顺序错误（§44）

**位置**：`v4.py` line 200

**根因**：Sionna Proxy 的 `SYMBOL_SIZES` 将 extended CP (160 samples) 错误地放在了 symbol 12-13，而 NR 标准要求在 symbol 0 和 7。

```python
# 错误（导致 SRS symbol 12 的 FFT 窗口偏移 16 samples）
SYMBOL_SIZES = [CP1 + FFT_SIZE] * 12 + [CP2 + FFT_SIZE] * 2

# 正确
SYMBOL_SIZES = ([CP2 + FFT_SIZE] + [CP1 + FFT_SIZE] * 6
                + [CP2 + FFT_SIZE] + [CP1 + FFT_SIZE] * 6)
```

**影响**：SRS symbol 12 的 OAI FFT 窗口相比 Proxy IFFT 输出循环偏移 16 samples → 跨 1248 活跃子载波产生 ~9.8 圈相位旋转 → NMSE 锯齿、Estimation SNR 始终为负。

**诊断关键**：幅度相关 0.9946 证明数据本身正确，问题纯在相位。

### 7.2 [极高] Proxy 无随机种子控制（§49 BUG 9）

**位置**：`v4.py` + `channel_coefficients_JIN.py`

**根因**：7 组 `tf.random.*` 和 Sionna `config.tf_rng` 调用均无 seed 设置 → 每个 SNR 点重启 proxy 生成完全不同的信道实现 → NMSE vs SNR 非单调。

**修复**：新增 `--seed` CLI 参数 + 全局种子设置（TF/NP/CuPy/random 四路 RNG）。

### 7.3 [致命] SRS SNR 门限选择偏差（§49 BUG 2）

**位置**：`openair1/PHY/defs_gNB.h`

**根因**：`SRS_TWIN_SNR_THRESHOLD=5` 在低 SNR 下丢弃大部分帧，造成选择偏差。

**修复**：改为 `-999`（实质上禁用门限）。

### 7.4 [致命] GT slot_ids 语义错误（§49 BUG 1）

**位置**：`v4.py` GTBatchSaver

**根因**：GT 的 `slot_ids` 使用 proxy 内部计数器，与 SRS 的 NR abs_slot 不在同一刻度 → GT-SRS 帧对齐漂移。

**修复**：`slot_id = int(ipc_ts) // 30720`（NR 绝对 slot 编号）。

### 7.5 [高] SRS 调度被 ul_failure 阻断（§1.4）

**位置**：`openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_srs.c`

**根因**：Ring buffer 溢出 → UL 信号丢失 → PUSCH 解码失败 → `ul_failure=true` → `nr_schedule_srs()` 跳过 UE → SRS 采集归零。SRS 是独立参考信号，不应依赖数据传输。

**修复**：移除 `ul_failure` 条件，仅保留 `transm_interrupt` 定时器检查。**效果**：SRS 采集帧数从 31 → 297 → 418 帧。

### 7.6 [高] SRS LS 估计器 Period-3 质量退化（§47）

**现象**：SRS 帧按 `frame_id % 3` 分为 3 组，每 3 帧中有 1 帧是垃圾数据（sim_gt ≈ 0.08）。

**附加发现**：Slot 8（DL→UL 切换后首个完整 UL slot）质量显著劣于 Slot 9。

**处理**：分析层 `filter_period3()` 过滤 + 数据采集层 per-frame 质量标记。

### 7.7 [高] SFN 10.24s 回绕（§49 BUG 4）

**位置**：`digital_twin_stats.py`

**根因**：NR SFN 每 10.24s 回绕（1024 帧 × 10ms = 10.24s），导致 `srs_abs` 非单调 → 运行 >10s 的 sweep 对齐灾难。

**修复**：`unwrap_srs_abs_slots()` 检测 SFN 下跳并累加 20480 偏移。

### 7.8 [高] UE RA Contention Resolution 崩溃（§49 BUG 8）

**位置**：`openair2/LAYER2/NR_MAC_UE/nr_ra_procedures.c` L1196

**根因**：SR failure 64 次后触发 RA 重建，C-RNTI RA 的 MSG3 不需要 RRC payload，但 `nr_ra_contention_resolution_failed()` 中缺少 `!msg3_C_RNTI` 守卫 → RRC 层 `switch(ra_trigger)` 走到 `default` → `AssertFatal` → UE 进程 segfault。

**修复**：
```c
if (!mac->msg3_C_RNTI)
    nr_mac_rrc_msg3_ind(mac->ue_id, 0, true);
```

### 7.9 验证结果演进

| 阶段 | NMSE (最佳) | 说明 |
|------|------------|------|
| CP bug 修复前 | -24 dB (65% 好帧) | 相位旋转 ~9.8 圈 |
| CP 修复 + Method C | -29 ~ -32 dB | LS anchor + local refinement |
| 9 Bug 全修后 | **-38.4 dB** (outlier 0%) | BUG 1-8 全部修复 |
| Per-SC Bias (Oracle) | **-375 dB** (浮点极限) | 证明误差 100% 确定性 |

---

## 8. 关键文件汇总

> 所有路径相对于 `DevChannelProxyJIN/openairinterface5g_whan/`

### 8.1 NR SRS PHY 层

| 阶段 | 文件 | 核心函数/结构 |
|------|------|--------------|
| 基序列 | `openair1/PHY/NR_REFSIG/ul_ref_seq_nr.c` / `.h` | ZC 低 PAPR 基序列, `SRS_SB_CONF` |
| 序列生成 | `openair1/PHY/NR_UE_TRANSPORT/srs_modulation_nr.c` / `.h` | `generate_srs_nr()`, `ue_srs_procedures_nr()` |
| UE 调度 | `openair1/SCHED_NR_UE/phy_procedures_nr_ue.c` | 调用 `ue_srs_procedures_nr()` |
| gNB 接收 | `openair1/SCHED_NR/phy_procedures_nr_gNB.c` | SRS 接收全链路 |
| SRS 抽取 | `openair1/PHY/NR_TRANSPORT/srs_rx.c` | `nr_get_srs_signal()` |
| 信道估计 | `openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c` | `nr_srs_channel_estimation()` |
| TA 估计 | `openair1/PHY/NR_ESTIMATION/nr_measurements_gNB.c` | `nr_est_timing_advance_srs()` |
| 头文件 | `openair1/PHY/NR_ESTIMATION/nr_ul_estimation.h` | 声明 |
| FAPI | `openair1/SCHED_NR/fapi_nr_l1.c` | `nr_fill_srs()` |
| 仿真 | `openair1/SIMULATION/NR_PHY/ulsim.c` | `-E 1` 开启 SRS |

### 8.2 NR SRS MAC / RRC 层

| 文件 | 核心函数/结构 |
|------|--------------|
| `openair2/LAYER2/NR_MAC_UE/nr_ue_scheduler.c` | `nr_ue_periodic_srs_scheduling()`, `nr_ue_aperiodic_srs_scheduling()` |
| `openair2/LAYER2/NR_MAC_UE/nr_ue_power_procedures.c` | `get_srs_tx_power_ue()` |
| `openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_srs.c` | `nr_schedule_srs()` |
| `openair2/LAYER2/NR_MAC_gNB/nr_radio_config.c` | `configure_periodic_srs()`, SRS slot 偏移 |

### 8.3 本项目 SRS Dump 系统（新增/修改）

| 文件 | 内容 |
|------|------|
| `openair1/PHY/defs_gNB.h` | `srs_pingpong_t` 结构, `srs_bin_header_t` V2, `SRS_TWIN_SNR_THRESHOLD` |
| `openair1/PHY/NR_ESTIMATION/nr_ul_channel_estimation.c` | SRS 采集注入点 (RNTI + 帧号 + SNR 判定) |
| `openair1/PHY/INIT/nr_init.c` | `srs_write_one_batch()`, `srs_disk_writer_thread()`, `free_srs_digital_twin_system()` |

### 8.4 Sionna Proxy / 分析脚本

| 文件 | 内容 |
|------|------|
| `vRAN_Socket/.../v4.py` | `GTBatchSaver`, `SYMBOL_SIZES` (CP 修复), `--seed`, `--gt-symbols` |
| `vRAN_Socket/.../digital_twin_stats.py` | 5+2 维交叉验证 + Method E + Per-SC Bias |
| `vRAN_Socket/.../q4_convergence_sweep.py` | 6 指标 × {N-axis, SNR-axis} 收敛分析 |
| `vRAN_Socket/.../compare_nmse.py` | NMSE 对比（新旧 GT 格式兼容） |
| `vRAN_Socket/.../launch_all_luuuuuu.sh` | 启动脚本（`-mf` 帧数 early-stop, `-seed`） |
| `vRAN_Socket/.../run_q4_snr_sweep_luuuuuu.sh` | 多点 SNR sweep wrapper |

---

## 9. GDB 调试建议

### 9.1 NR ulsim 单步跟踪

```bash
# 编译（调试模式）
cd build && cmake .. -DCMAKE_BUILD_TYPE=Debug && make nr_ulsim

# 启动 GDB
gdb --args ./nr_ulsim -n10 -s40 -E 1

# 关键断点（按信号流顺序）
b generate_srs_nr          # UE 发送：SRS 序列生成 + 频域映射
b nr_get_srs_signal        # gNB 接收：从 rxdataF 抽取 SRS
b nr_srs_channel_estimation # gNB：LS 估计 + FIR 插值
b nr_est_timing_advance_srs # gNB：TA 估计
```

### 9.2 关键变量观察

- `srs_generated_signal[p_index][subcarrier]` — 本地参考序列
- `srs_received_signal[ant][subcarrier]` — 接收 SRS 信号
- `srs_ls_estimated_channel[subcarrier]` — LS 估计结果
- `srs_estimated_channel_freq[ant][port][subcarrier]` — 插值后的频域信道
- `snr_per_rb[]` / `snr` — 每 RB 和宽带 SNR

### 9.3 实际全链路调试（OAI + Sionna Proxy）

```bash
# 使用 launch_all_luuuuuu.sh 启动全系统
sudo bash launch_all_luuuuuu.sh -d 60 -mf 200 -seed 42

# 或使用 SNR sweep
CHANNEL_SEED=42 sudo bash run_q4_snr_sweep_luuuuuu.sh
```

**注意**：强制停止后需清理 GPU IPC 残留：
```bash
rm -rf /tmp/oai_gpu_ipc/gpu_ipc_shm_* /tmp/oai_gpu_ipc/sionna_gt/*
sudo docker restart sionna-proxy
```

---

## 附录：LTE SRS 对照

> 仅供参考，本项目不涉及 LTE

| 阶段 | LTE 文件 | 核心函数 |
|------|----------|---------|
| 基序列 | `PHY/LTE_REFSIG/lte_ul_ref.c` | `ul_ref_sigs[]` (ZC 序列表) |
| 序列生成 | `PHY/LTE_UE_TRANSPORT/srs_modulation.c` | `generate_srs()` |
| 功率控制 | `SCHED_UE/srs_pc.c` | `srs_power_cntl()` (36.213 §5.1.3.1) |
| UE 调度 | `SCHED_UE/phy_procedures_lte_ue.c` | `ue_srs_procedures()`, `ue_compute_srs_occasion()` |
| 公共逻辑 | `SCHED/phy_procedures_lte_common.c` | `is_srs_occasion_common()`, `compute_srs_pos()` |
| gNB 接收 | `SCHED/phy_procedures_lte_eNb.c` | `srs_procedures()` |
| 信道估计 | `PHY/LTE_ESTIMATION/lte_ul_channel_estimation.c` | `lte_srs_channel_estimation()` (LS: `mult_cpx_conj_vector`) |
| 测量 | `PHY/LTE_ESTIMATION/lte_eNB_measurements.c` | `lte_eNB_srs_measurements()` |
| 仿真 | `SIMULATION/LTE_PHY/ulsim.c` | `osrs` 布尔参数 |

**LTE vs NR 主要差异**：
- LTE SRS 固定在子帧最后一个 symbol；NR SRS 位置可配置
- LTE 无 FD-CDM 多端口分离；NR 支持最多 4 端口 FD-CDM
- LTE LS 估计直接用 `mult_cpx_conj_vector`；NR 增加了 FIR 频域插值
- NR 支持 aperiodic / semi-persistent / periodic 三种触发；LTE 只有 periodic / aperiodic

---

## 参考

- 3GPP TS 38.211 — NR Physical channels and modulation (Section 6.4.1.4: Sounding reference signal)
- 3GPP TS 38.213 — NR Physical layer procedures for control (SRS power control)
- 3GPP TS 38.214 — NR Physical layer procedures for data (SRS-based CSI)
- `Change_log.md` — 项目完整改动日志（§1–§49）
