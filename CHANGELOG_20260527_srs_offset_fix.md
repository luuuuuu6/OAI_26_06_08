# 变更日志 — 2026-05-27 SRS Offset 越界修复及 NMSE 评估改进

| 字段 | 值 |
|------|----|
| **日期** | 2026-05-27 |
| **编制** | 刘 |
| **涉及模块** | OAI gNB MAC / eval_nmse_clean / sweep manifest |

---

## 1. 问题概述

在 `SRS_PERIOD_SLOTS=10` 的多 SNR sweep 测试中，当 UE 多次重连导致 uid 递增到 2 以上时，gNB 在 ASN.1 编码阶段崩溃（segfault）。同时 NMSE 评估工具存在 GT/SRS 重复配对问题，导致虚假的时间漂移警告。

---

## 2. 修改 1：修复 SRS PeriodicityAndOffset 越界崩溃

### 2.1 文件

```
DevChannelProxyJIN/openairinterface5g_whan/openair2/LAYER2/NR_MAC_gNB/nr_radio_config.c
```

### 2.2 函数

`configure_periodic_srs()`（第 747–849 行）

### 2.3 根因

TDD 10-slot 周期（DDDDDDDXUU）中只有 2 个 full UL slot（slot 8、slot 9）。`get_ul_slot_offset(fs, uid, false)` 的计算方式是：

```
period_idx   = uid / 2
slot_in_period = uid % 2
offset       = ul_slot_idxs[slot_in_period] + period_idx * 10
```

各 uid 对应的 offset：

| uid | offset | sl10 允许范围 [0..9] | 结果 |
|-----|--------|---------------------|------|
| 0   | 8      | OK                  | 正常 |
| 1   | 9      | OK                  | 正常 |
| 2   | 18     | **越界**            | 崩溃 |
| 3   | 19     | **越界**            | 崩溃 |

当 `SRS_PERIOD_SLOTS=10` 时，`check_periodicity(10, 10, fs)` 命中，选择 `sl10`。但 uid≥2 时 offset≥18，超出 3GPP TS 38.331 中 `sl10 INTEGER(0..9)` 的 ASN.1 约束，导致 UPER 编码失败：

```
Assertion (enc_rval.encoded > 0) failed!
In ue_context_setup_request() .../mac_rrc_dl_handler.c:690
Could not encode CellGroup, failed element srs-ResourceToAddModList
Segmentation fault (core dumped)
```

**崩溃实证**（`q4_sweep_20260527_133923/snr_10dB/gnb.log` 第 694–701 行）：

```
[NR_MAC]   SRS_PERIOD_SLOTS override: requested ideal period 10 slots for uid 2

Assertion (enc_rval.encoded > 0) failed!
In ue_context_setup_request() .../mac_rrc_dl_handler.c:690
Could not encode CellGroup, failed element srs-ResourceToAddModList

launch_all_v9.sh: line 716: 1539737 Segmentation fault (core dumped)
```

### 2.4 修改内容

**修改前**（第 769–829 行）：一条 if-else 链，仅检查 `check_periodicity(N, ideal_period, fs)` 而不检查 `offset < N`。

**修改后**（第 769–848 行）：

1. 新增候选周期表（第 771 行）：
   ```c
   static const int srs_periods[] = {4, 5, 8, 10, 16, 20, 32, 40, 64, 80, 160, 320, 640, 1280, 2560};
   ```

2. 统一选择逻辑（第 772–778 行）：遍历候选周期，选第一个同时满足 `check_periodicity()` **和** `offset < period` 的值：
   ```c
   int selected_period = 2560;
   for (int i = 0; i < (int)(sizeof(srs_periods) / sizeof(srs_periods[0])); i++) {
     if (check_periodicity(srs_periods[i], ideal_period, fs) && offset < srs_periods[i]) {
       selected_period = srs_periods[i];
       break;
     }
   }
   ```

3. 当周期被调整时输出警告日志（第 780–783 行）：
   ```c
   if (selected_period != ideal_period)
     LOG_W(NR_MAC,
           "SRS period adjusted for uid %d: requested=%d selected=%d offset=%d\n",
           uid, ideal_period, selected_period, offset);
   ```

4. 用 `switch (selected_period)` 替代原来的 if-else 链设置 ASN.1 choice（第 786–847 行），覆盖 sl4 到 sl2560 全部 15 种周期。

### 2.5 各 uid 修复后行为（`SRS_PERIOD_SLOTS=10`, TDD 10-slot）

`check_periodicity(N, 10, fs)` 要求 `N % 10 == 0` 且 `10 < N+1`，有效候选为 10, 20, 40, 80, ...

| uid | offset | 选择的周期 | 变化说明 |
|-----|--------|-----------|---------|
| 0   | 8      | sl10      | 不变（8 < 10） |
| 1   | 9      | sl10      | 不变（9 < 10） |
| 2   | 18     | **sl20**  | 自动提升（18 < 20） |
| 3   | 19     | **sl20**  | 自动提升（19 < 20） |
| 4   | 28     | **sl40**  | 自动提升（28 < 40） |

### 2.6 未修改的部分

- `get_ul_slot_offset()`（offset 计算逻辑正确，不改）
- `check_periodicity()`（周期有效性检查逻辑正确，不改）
- `verify_radio_configuration()`（不改）
- `mac_rrc_dl_handler.c` 的 `ue_context_setup_request()`（不改）

---

## 3. 修改 2：修复 NMSE eval 中 GT 重复配对问题

### 3.1 文件

```
DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/eval_nmse_clean.py
```

### 3.2 函数

`align_frames()`（第 166–198 行）及新增 `_pair_unique_nearest()`（第 129–163 行）

### 3.3 根因

当前默认配置中：
- SRS 每 20 slots 采样一帧（`SRS_PERIOD_SLOTS=10`，TDD 10-slot 周期下每两个周期一帧）
- GT 每 100 slots 保存一帧（`GT_SAVE_EVERY=100`）

因此约 5 个 SRS 帧才对应 1 个 GT 帧。旧 `align_frames()` 对每个 SRS 帧独立搜索最近 GT 帧，允许多个 SRS 帧重复匹配同一个 GT 帧。

实测 `snr_10dB` 数据：

| 指标 | 修改前 | 修改后 |
|------|--------|--------|
| SRS 帧 | 518 | 518 |
| GT 帧 | 206 | 206 |
| paired | 275 | **105** |
| unique GT | 105 | **105** |
| 重复 GT | 170 | **0** |
| median gap | 19 | **0** |
| Q4-Q1 drift | +2.6 dB (WARNING) | **+2.0 dB (stable)** |

### 3.4 修改内容

**新增** `_pair_unique_nearest()`（第 129–163 行）：
- 对每个 GT 帧，搜索最近的**未使用**的 SRS 帧
- 用 `used_srs` 集合跟踪已配对的 SRS 帧
- 保证 GT/SRS 一对一配对，不重复
- 返回 `(srs_idx, gt_idx, gaps)` 三元组

**重写** `align_frames()`（第 166–198 行）：
- 偏移搜索阶段调用 `_pair_unique_nearest()` 替代原来的向量化最近邻
- 选择最佳偏移时优先最大匹配数，其次最小 median gap
- 输出改为 `unique_matches=N/min(srs, gt)` 替代原来的 `matches=N/srs`

### 3.5 验证结果（`snr_10dB`）

```
[align] offset C=-3435 (gt_median=10433, srs_median=13828, unique_matches=105/206)
  Q1 gap: median=1.0, max=19, mean=1.69
  Q2 gap: median=0.0, max=1, mean=0.46
  Q3 gap: median=0.0, max=0, mean=0.00
  Q4 gap: median=0.0, max=0, mean=0.00
paired: 105 frames (median_gap=0)

NMSE global     : -6.66 dB
NMSE per-antenna: -10.48 dB
Q4-Q1 drift = +2.0 dB (stable)
```

---

## 4. 修改 3：修正 sweep manifest 显示歧义

### 4.1 文件

```
DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/run_q4_snr_sweep_v9.sh
```

### 4.2 修改行

| 行号 | 修改前 | 修改后 |
|------|--------|--------|
| 210 | `"snr_dB" "status" "n_gt" "n_srs" "subdir"` | `"snr_dB" "status" "n_gt_files" "n_srs" "subdir"` |
| 320 | `gt=${EXIST_SEQS}` | `gt_files=${EXIST_SEQS}` |
| 325 | `gt=${EXIST_SEQS}` | `gt_files=${EXIST_SEQS}` |
| 495 | `gt=${N_SEQS}/${REQ_SEQS}` | `gt_files=${N_SEQS}/${REQ_SEQS}` |
| 496 | `printf ... "$N_SEQS"` | 列宽从 `%-8s` 改为 `%-10s` |

### 4.3 原因

原 manifest 中的 `n_gt` 列容易被误解为 GT 帧数，实际是 GT npz 文件数。例如 `n_gt=3` 对应 3 个文件共 206 帧（seq0:100 + seq1:100 + seq2:6）。改名为 `n_gt_files` 消除歧义。

---

## 5. 编译与验证

### 5.1 编译

```bash
cd .../openairinterface5g_whan/cmake_targets/ran_build/build
sudo ninja nr-softmodem nr-uesoftmodem
# [8/8] Linking CXX executable nr-softmodem
```

### 5.2 SRS 修复验证

验证命令：

```bash
sudo ONLY_SNR="5 10 15 20" MAX_FRAMES=200 SRS_ESTIMATOR=2dmmse \
     SRS_2D_METHOD=kalman SRS_PERIOD_SLOTS=10 \
     GT_SAVE_EVERY=100 UL_PRE_GAIN=1.0 \
     bash run_q4_snr_sweep_v9.sh
```

验证 sweep：`q4_sweep_20260527_184252`

| SNR | 修改前 | 修改后 |
|-----|--------|--------|
| 5 dB | OK | OK |
| 10 dB | **segfault (uid=2)** | **OK** |
| 15 dB | **segfault (uid=2)** | **OK** |
| 20 dB | **segfault (uid=2)** | FAIL-ATTACH-TIMEOUT（与 SRS 无关） |

`snr_10dB` gnb.log 确认修复生效（第 533–534 行）：

```
[NR_MAC]   SRS_PERIOD_SLOTS override: requested ideal period 10 slots for uid 2
[NR_MAC]   SRS period adjusted for uid 2: requested=10 selected=20 offset=18
```

验证三项标准全部通过：

- [x] uid=2 出现 `SRS period adjusted ... requested=10 selected=20 offset=18`
- [x] 不再出现 `Could not encode CellGroup, failed element srs-ResourceToAddModList`
- [x] 无 Segmentation fault / Assertion 失败

### 5.3 语法检查

```bash
bash -n run_q4_snr_sweep_v9.sh       # OK
python3 -m py_compile eval_nmse_clean.py  # OK
# linter: 无报错
```
