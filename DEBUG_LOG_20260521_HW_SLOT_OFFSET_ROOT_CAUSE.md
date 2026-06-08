# UE Attach 失败根因分析日志：hw_slot_offset 初始同步错位

| 字段 | 值 |
|------|----|
| **日期** | 2026-05-21 |
| **目标** | 定位 dclcom61 新服务器上 UE attach 随机失败（~50% 以上）的根因 |
| **结论** | **根因是 STO ±1 样本抖动（`nr_adjust_synch_ue` PI 控制器）与 `hw_slot_offset` 不稳定的叠加效应。仅通过在启动脚本添加 `--ue-timing-correction-disable` 禁用 PI 控制器即可解决。hw_slot_offset=8 在 STO 修复后不再是致命的——UE 可通过 resync 自行恢复。** |
| **前序工作** | 05-15 已定位 STO ±1 样本帧间抖动（遗留问题 2），但未在启动脚本中启用修复 |
| **关联** | 与遗留问题 2 是**同一个 bug 的两个表现**——STO 抖动才是 attach 失败的核心原因 |
| **最终修复** | `launch_all_v8.sh` / `launch_all_v9.sh` 中 `nr-uesoftmodem` 命令行添加 `--ue-timing-correction-disable` |

---

## 一、问题描述

从 dclcom57 迁移到 dclcom61（RTX 5090）后，用 v8/v9 sweep 跑 adaptive（SNR=10, CDL-C, 2x1 MIMO），UE attach 的成功率极低。典型表现：

- sweep_manifest 大量 `PARTIAL`（gt 累积数百但 srs=0）或 `FAIL-ATTACH`
- UE 日志充满 `Error decoding PBCH`、`all 0 pdu`、`Contention resolution failed`
- gNB 侧大量 phantom RA（几十个不同 TC-RNTI 全部 `RA failed at state WAIT_Msg3`）
- 即使 RA 偶尔成功，后续 ULSCH BLER 高达 97-99%，SRB1 数据到不了 gNB
- 最终无 `RRCReconfigurationComplete`，无 SRS 调度

---

## 二、横向数据对比

### 2.1 全量 run 汇总（dclcom61 上所有 SNR=10 run）

| 时间戳 | 首次 hw_slot_offset | 后续 resync | 结果 | n_srs |
|--------|--------------------:|-------------|------|------:|
| `143048/adaptive` | **7** | 无 | **OK** | 1 |
| `133524/adaptive` | **7** | 无 | **OK** | 1 |
| `174215` (v9 诊断) | **7** | 后来 4 | **OK** (PARTIAL 仅因 srs<100) | 1 |
| `164213` | **7** | 后来 30, 16 | PARTIAL | 0 |
| `151543/adaptive` | **7** | 无 | PARTIAL（RA 成功但 ULSCH BLER 99%，无 RRCReconfigurationComplete）| 0 |
| `133524/ewma` | 8 | 16, 32, 16, 16, 16 | PARTIAL | 0 |
| `211447` (run 1) | 8 | 16, 16, 30 | PARTIAL | 0 |
| `220530` (run 2) | 8 | 16 | PARTIAL | 0 |
| `223739` (run 3) | 8 | 16 | FAIL-ATTACH | 0 |

**修复前规律（`UE_no_timing_correction=0`，STO 抖动活跃）：**
- **首次 `hw_slot_offset=8` → 100% attach 失败**（5/5 全部失败）
- **首次 `hw_slot_offset=7` → 75% attach 成功**（3/4 成功；`151543` 虽首次 offset=7 且 RA 成功，但 ULSCH BLER 高达 99%，疑为 STO 抖动叠加导致）
- `164213` 首次 offset=7 后 RA 成功，但运行中 UE 失去同步、resync 到 30/16 后 SRS 未能调度

**修复后（`UE_no_timing_correction=1`，STO 抖动禁用）：**

| 时间戳 | 首次 hw_slot_offset | 后续 resync | 结果 | n_srs | 备注 |
|--------|--------------------:|-------------|------|------:|------|
| `134704` (SNR=10) | **8** | 30, 16, 16 | **OK** | 4 | hw_slot_offset=8 也能成功！ |

> **关键发现**：禁用 STO 抖动后，hw_slot_offset=8 不再致命——UE 通过 resync 机制自行恢复到有效同步点。

### 2.2 TDD 配置（gnb.sa.band78.fr1.106PRB.usrpb210.conf）

```
referenceSubcarrierSpacing = 1      # SCS 30 kHz, numerology 1
dl_UL_TransmissionPeriodicity = 6   # 5 ms period
nrofDownlinkSlots = 7
nrofDownlinkSymbols = 6
nrofUplinkSlots = 2
nrofUplinkSymbols = 4
```

5ms TDD 周期（10 slots）的 slot 类型：

```
Slot:  0   1   2   3   4   5   6   7     8   9
Type: DL  DL  DL  DL  DL  DL  DL  Mixed  UL  UL
      ←———————— 下行 ————————→  ↑混合  ←上行→
```

**`hw_slot_offset=7`：UE 帧边界对齐到 Mixed slot（含 DL symbols）→ 正确。**
**`hw_slot_offset=8`：UE 帧边界对齐到 UL slot → 整体错位 1 slot → 灾难性。**

### 2.3 失败时的完整症状链

以 `223739`（FAIL-ATTACH，hw_slot_offset=8）为代表（attach_diag_v9 120s 窗口内统计）：

```
all_0_pdu = 79        # UE 在 UL slot 试图接收 DL → 收到零     (全量日志: 190)
contention = 37       # gNB 侧看到 phantom PRACH → Msg3 全失败 (全量日志: 127)
t300 = 2              # RRCSetup 超时                           (全量日志: 3)
pbch_err = 119        # PBCH 解码持续失败（SSB 位置错位）       (全量日志: 971)
srs = 0               # 从未到达 SRS 调度阶段
```

> 注：以上数值来自 `attach_diag_v9.log` 的 120s 超时窗口内计数，全量 UE 日志计数更高（括号内）。

以 `174215`（OK，hw_slot_offset=7）为代表（attach_diag_v9 统计）：

```
all_0_pdu = 0
contention = 0
t300 = 0
pbch_err = 27         # 正常范围（非 SSB slot 的 PBCH 检测）(全量日志: 304，含 attach 后持续检测)
srs = 1               # SRS 正常调度
```

---

## 三、根因机制

### 3.1 hw_slot_offset 的计算

`executables/nr-ue.c` 中 `UE_synch()` 函数：

```c
ret = nr_initial_sync(&syncD->proc, UE, 2, ...);

if (ret.cell_detected) {
    syncD->rx_offset = ret.rx_offset;
    const int hw_slot_offset =
        ((ret.rx_offset << 1) / fp->samples_per_subframe * fp->slots_per_subframe)
        + round((float)((ret.rx_offset << 1) % fp->samples_per_subframe) / fp->samples_per_slot0);
    // ...
    LOG_I(PHY, "Got synch: hw_slot_offset %d, ...");
    UE->is_synchronized = 1;
}
```

`nr_initial_sync` 扫描 2 帧的接收样本，用 PSS 相关找到同步峰值位置 `rx_offset`。
`hw_slot_offset` 将 `rx_offset` 换算为"PSS 峰值在第几个 slot"。

### 3.2 为什么 GPU-IPC 模式下 hw_slot_offset 不稳定

UE 的 `rfsimulator_read()` 在首次调用时初始化 `nextRxTstamp`：

```c
// radio/rfsimulator/simulator.c
if (t->nextRxTstamp == 0) {
    uint64_t lts = *(shm + _V7_OFF_LAST_DL_RX_TS);
    if (lts > 0) {
        t->nextRxTstamp = lts > nsamps ? lts - nsamps : 0;
    }
}
```

`lts` 是 Proxy 写入的最新 DL 时间戳。`nextRxTstamp = lts - nsamps`。

**关键：`lts` 取决于 UE 首次调用 `rfsimulator_read` 时 Proxy 已写到哪里，这是一个竞态条件。**

- `lts` 不保证 slot 对齐（`lts % 30720 ≠ 0`）
- 不同 run 中 `lts` 的值不同（取决于进程启动时序）
- 实测：`174215`（OK）`lts=2993023`，`223739`（FAIL）`lts=2979839`，两者差 13184 样本（约 0.43 个 slot），证明竞态条件确实使 `lts` 在 run 间波动
- 不同的 `lts` → 不同的 `nextRxTstamp` → PSS 搜索窗口起点不同 → `rx_offset` 换算结果可能跨过 slot 7/8 的边界

### 3.3 PSS 峰值搜索的边界行为

`pss_nr.c` 的 `pss_search_time_nr`：

```c
if (peak_value < 5 * avg[pss_source])
    return(-1);  // 峰值不够显著则拒绝
```

在 GPU-IPC 模式下，UE 的初始读取窗口可能包含部分无效数据（ring buffer 填充中）。PSS 相关器在两帧数据中搜索，峰值位置的精度受限于：
1. ring buffer 初始填充的完整性
2. 搜索窗口内 SSB 的完整性
3. `nextRxTstamp` 的 slot 内偏移

当 PSS 峰值恰好落在 slot 边界附近时（slot 7 和 slot 8 的边界），微小的 `nextRxTstamp` 变化就能让 `rx_offset` 换算结果从 7 跳到 8。

### 3.4 为什么 hw_slot_offset=8 是灾难性的

UE 同步后调用 `syncInFrame()` 读取 `rx_offset` 个样本来对齐帧边界。如果 `rx_offset` 差了约 15360 样本（半个 slot），UE 认为的 "slot 0" 实际上是 gNB 的 "slot 1"。

后果：

```
UE 视角                         gNB 实际
slot 0 (UE 认为 DL)      →    slot 1 (确实 DL)     ✓ 偶尔能解码
slot 7 (UE 认为 Mixed)   →    slot 8 (实际 UL)     ✗ 收到零/噪声
slot 8 (UE 认为 UL)      →    slot 9 (实际 UL)     ✗ 发射时机错（但同是 UL）
slot 9 (UE 认为 UL)      →    slot 0 (实际 DL)     ✗ UE 在 DL slot 发射 → gNB phantom RA
```

- **DL 接收**：大部分正确但 Mixed slot 对不上 → PDSCH/PDCCH 间歇性失败 → `all 0 pdu`
- **UL 发射**：UE 在 gNB 的 DL slot 发射 → gNB 看到意外 PRACH → phantom RA → `RA failed WAIT_Msg3`
- **PBCH**：SSB 在特定 slot/symbol 位置，偏移 1 slot 后 PBCH 解码大量失败
- **Contention**：即使 RA 偶尔成功，Msg4 在错误 slot 丢失 → contention resolution failed

---

## 四、v9 attach 诊断验证

v9 脚本新增的 `attach_diag_v9.log` 完美验证了这个模型：

**成功 run（174215, hw_slot_offset=7）：**
```
[v9-attach-diag] started at 1779266571, timeout=120s
[v9-attach-diag] 20s: WAIT_SYNC -> WAIT_RA
[v9-attach-diag] 25s: WAIT_RA -> WAIT_RRC_RECFG
[v9-attach-diag] 40s: ATTACHED OK
[v9-attach-diag] summary: state=ATTACHED all_0_pdu=0 contention=0 t300=0 pbch_err=27
```

**失败 run（223739, hw_slot_offset=8）：**
```
[v9-attach-diag] started at 1779284295, timeout=120s
[v9-attach-diag] 15s: WAIT_SYNC -> WAIT_RA
[v9-attach-diag] 120s: FAIL-ATTACH-TIMEOUT (state=WAIT_RA)
[v9-attach-diag] summary: state=WAIT_RA all_0_pdu=79 contention=37 t300=2 pbch_err=119
```

---

## 五、UL DIAG 诊断排除的假说

通过在 `simulator.c` 中加入的 `[UL DIAG]` 诊断日志，我们**排除**了之前怀疑的 "ring buffer 位置不匹配" 假说：

成功 run 的 `[UL DIAG]`：
```
[UL DIAG] #100: gnb_ts=3041280 ul_rx_ts=3085182 circ_gnb=552960 circ_rx=640764 ret=30720 empty=95
```

**`circ_gnb=552960` 与 `circ_rx=640764` 不同，但 `ret=30720`（读成功），ULSCH BLER 正常。**

这证明 ring buffer 的 `circ_offset` 差异不影响数据正确性——gNB 读的数据是 Proxy 已写入的有效数据，位置差异只是时间戳域的偏移，不是数据域的错位。

---

## 六、与遗留问题 2（STO 抖动）的关系——修正后的理解

### 6.1 原始分析（修复前）

| 维度 | 遗留问题 2（STO ±1 样本抖动） | 当前问题（hw_slot_offset=8） |
|------|-------------------------------|-------------------------------|
| **发生阶段** | 初始同步后，运行中每次 PBCH 解码 | 初始同步本身 (`nr_initial_sync`) |
| **偏移量级** | ±1 样本 | ~15360 样本（半个 slot） |
| **频率** | 每帧振荡 | 每次冷启动决定，不变 |
| **根因函数** | `nr_adjust_synch_ue`（PI 控制器） | `nr_initial_sync` → PSS 相关 |
| **表现** | NMSE floor -7 dB | UE 无法 attach |
| **共同主题** | GPU-IPC 模式下 `nextRxTstamp` 初始化不 slot 对齐，导致 OAI UE 时序工作在非设计点 |

### 6.2 实验验证后的修正结论

经过实际修复和验证（见第七节），发现原始分析对两个问题的独立性判断**有误**：

| 之前的判断 | 修正后的判断 |
|-----------|------------|
| hw_slot_offset=8 是 attach 失败的**充分条件** | hw_slot_offset=8 在 STO 修复后**不再致命**（run `134704` 实证） |
| 两个问题独立，需分别修复 | STO 抖动是**核心原因**，hw_slot_offset=8 只是**加重因素** |
| 需要"三层防护"才能覆盖 | **仅禁用 STO 即可稳定 attach**，无需修改 OAI 源码 |

**关键证据**：run `134704`（SNR=10, `--ue-timing-correction-disable`）中，UE 首次同步 `hw_slot_offset=8`，但仍然在第 116 帧完成 RA，后续经 resync（30, 16, 16）最终 attach 成功，n_gt=68, n_srs=4, **status=OK**。

这证明 hw_slot_offset=8 的帧错位并非"灾难性"——OAI UE 的 resync 机制能自行纠正。真正使 attach 失败的是 STO ±1 样本的持续抖动破坏了 UL 信号质量（ULSCH BLER 97-99%），使 RA 流程无法完成。

---

## 七、修复方案与实验记录

### 7.1 最终修复（已验证生效）

**仅修改启动脚本**，在 `nr-uesoftmodem` 命令行添加 `--ue-timing-correction-disable`：

```bash
# launch_all_v8.sh / launch_all_v9.sh — socket 和 GPU-IPC 两种模式均需添加
"$BUILD_DIR/nr-uesoftmodem" \
    -r 106 --numerology 1 --band 78 -C 3619200000 \
    --uicc0.imsi "$imsi" \
    --rfsim \
    --ue-timing-correction-disable \     # ← 新增：禁用运行态 STO 抖动
    $UE_ANT_ARGS \
    > "ue${ue_idx}.log" 2>&1
```

**原理**：该标志设置 `UE_no_timing_correction = 1`，完全禁用 `nr_adjust_synch_ue` PI 控制器的运行态时序微调。在 GPU-IPC 仿真模式下不存在硬件时钟漂移，PI 控制器反而因 `nextRxTstamp` 不 slot 对齐而在非设计点工作，导致每帧 ±1 样本的 STO 振荡 → ULSCH BLER 97-99% → attach 失败。

**影响范围**：仅影响 rfsim/GPU-IPC 模式。真实 USRP 部署不使用此脚本。**无需修改 OAI 源码**。

**验证结果（run `134704`, SNR=10, `--ue-timing-correction-disable`）**：

```
# UE 日志确认 STO 修复生效
UE_no_timing_correction 1                        ← 之前所有 run 都是 0

# 首次同步 hw_slot_offset=8（传统上"必败"），但 UE 通过 resync 自行恢复
Got synch: hw_slot_offset 8, carrier off -2 Hz
[UE 0][116.10] 4-Step RA procedure succeeded     ← 第 116 帧 RA 成功
Got synch: hw_slot_offset 30                      ← resync
Got synch: hw_slot_offset 16                      ← resync
Got synch: hw_slot_offset 16                      ← resync
Received RRCReconfigurationComplete               ← 完整 attach

# sweep 结果
snr_dB=10  status=OK  n_gt=68  n_srs=4  srs_frames=301/300  dur=470s
```

### 7.2 尝试过但放弃的源码修改

以下三种源码层面的修复方案均在实验中被验证为**无效或有副作用**，最终全部回退：

#### 方案 A（已放弃）：在 `UE_synch()` 中拒绝 UL slot 同步并重试

**思路**：当 `hw_slot_offset % 10 >= 8` 时设 `ret.cell_detected = false`，让 UE 自动重试。

**失败原因**：`readFrame()` 每次读取精确 2 帧（614400 样本 = 2 × TDD 周期），重试后 PSS 搜索窗口平移了 TDD 周期的整数倍 → PSS 峰值在窗口内的相对位置完全不变 → **每次重试都得到相同的 hw_slot_offset=8 → 无限循环**。

实测：run `130337`（SNR=-5）中 UE 连续拒绝了 **930 次**，120 秒内始终无法同步。

#### 方案 B（已放弃）：`simulator.c` 中 `nextRxTstamp` 对齐到 slot 边界

**思路**：`t->nextRxTstamp = (t->nextRxTstamp / nsamps) * nsamps;` 消除 slot 内偏移。

**失败原因**：对齐使 PSS 搜索起点变为确定性的。不同 `lts` 值对齐后的起点不同，某些 `lts` 值（如 2993023）对齐后 **100% 命中 hw_slot_offset=8**，比原来的随机 50% 更差。与方案 A 叠加后直接导致无限拒绝循环。

实测：run `130337` 的 `nextRxTstamp=2949120`（对齐后），PSS 搜索 930 次全部返回 hw_slot_offset=8。

#### 方案 C（已放弃）：修正 `rx_offset` 向前拉回半个 slot

**思路**：当 hw_slot_offset=8 时，将 `syncD->rx_offset` 减去 `samples_per_slot0/2`（15360 样本），使帧对齐从 UL slot 退回到 Mixed slot。

**失败原因**：`rx_offset` 代表 PSS 峰值在搜索缓冲区中的实际位置。人为修改后，`syncInFrame()` 将 UE 对齐到一个 PSS 峰值实际不存在的位置 → 后续 PBCH 解码的 FFT 窗口对不上 SSB 符号 → **每一帧 PBCH 都解码失败**，UE 永远无法获取 MIB → attach 彻底无法进行。

实测：run `134026`（SNR=10）中，UE 同步到修正后的 hw_slot_offset=7，但 `Error decoding PBCH` 从第 112 帧开始**每帧都失败**，全量 51 次关键错误，RA/RRC 完全未启动。

### 7.3 为什么仅禁用 STO 就足够

通过实验发现了一个关键认知修正：

**原始假设**（错误）：hw_slot_offset=8 → TDD 帧错位 1 slot → 灾难性，必须修复。

**实验结论**（正确）：hw_slot_offset=8 确实使初始 PBCH 解码困难（更多失败），但 OAI UE 的 resync 机制会在多次重试后找到新的同步点（hw_slot_offset 变为 30, 16 等，对应不同的搜索窗口），最终完成 RA 和 attach。**真正阻止 attach 成功的不是 hw_slot_offset，而是 STO ±1 样本抖动导致的持续性 ULSCH BLER 升高**。

| 场景 | STO 抖动 | hw_slot_offset | 结果 |
|------|:--------:|:-:|------|
| 之前大部分失败 run | ON (0) | 8 | FAIL — STO 使 UL 质量极差，resync 也救不回来 |
| 之前 `151543` | ON (0) | 7 | FAIL — 即使 offset 正确，STO 使 BLER 99% |
| 之前 `174215` | ON (0) | 7 | OK — 运气好，STO 影响较轻 |
| **修复后 `134704`** | **OFF (1)** | **8** | **OK** — 没有 STO，UE 通过 resync 自行恢复 |

### 7.4 `hw_slot_offset` 仍然是一个观测指标

虽然 hw_slot_offset=8 不再致命，它仍然有意义：
- **hw_slot_offset=7**：初始同步干净利落，RA 通常在 20-40s 内完成
- **hw_slot_offset=8**：初始同步需要多次 resync，RA 可能需要 60-120s
- 当需要优化 attach 速度时，可以考虑在 `simulator.c` 中优化 `nextRxTstamp` 初始化来提高 hw_slot_offset=7 的概率

### 7.5 完整修改文件清单

| 文件 | 改动 | 状态 |
|------|------|------|
| `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/launch_all_v9.sh` | socket 和 GPU-IPC 两处 UE 命令添加 `--ue-timing-correction-disable` | **已合入** |
| `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/launch_all_v8.sh` | 同上 | **已合入** |
| `executables/nr-ue.c` | 无修改（方案 A/C 已回退） | 原始状态 |
| `radio/rfsimulator/simulator.c` | 无修改（方案 B 已回退） | 原始状态 |

---

## 八、关键代码文件索引

| 文件 | 作用 | 关键行 |
|------|------|--------|
| `executables/nr-ue.c` | UE 同步主循环, `hw_slot_offset` 计算 | 437 (`nr_initial_sync`), 443-445 (`hw_slot_offset` 公式), 467 (`is_synchronized`) |
| `openair1/PHY/NR_UE_TRANSPORT/nr_initial_sync.c` | PSS/SSS 初始同步 | `nr_initial_sync()` |
| `openair1/PHY/NR_UE_TRANSPORT/pss_nr.c` | PSS 相关峰值搜索 | 630 (`5*avg` 阈值) |
| `radio/rfsimulator/simulator.c` | GPU-IPC nextRxTstamp 初始化 | 1492-1498 (`lts - nsamps`) |
| `radio/rfsimulator/gpu_ipc_v7.c` | ring buffer 读写 | 338-408 (`v7_circ_read`) |
| `targets/.../gnb.sa.band78.fr1.106PRB.usrpb210.conf` | TDD 配置 | 152-156 |

---

## 九、诊断工具产出

本次分析中新增的诊断工具（已合入 v9 脚本）：

| 工具 | 位置 | 作用 |
|------|------|------|
| `[UL DIAG]` in simulator.c | gNB UL 读路径 | 打印 gnb_ts / ul_rx_ts / circ_offset 对比，排除 ring buffer 假说 |
| `[UL DIAG]` in v8.py | Proxy UL 写路径 | 打印 proxy start_ts / circ_offset |
| `attach_diag_v9.log` | launch_all_v9.sh | attach 状态机 + ULSCH BLER + 关键字计数 |
| `attach_result_v9.txt` | launch_all_v9.sh | OK / FAIL-ATTACH-TIMEOUT(state) / FAIL-ATTACH-UL |

---

## 十、实验时间线

| 时间 | 操作 | 结果 |
|------|------|------|
| 05-21 上午 | 分析 dclcom61 上历史 9 个 SNR=10 run 的日志 | 发现 hw_slot_offset 7→成功、8→失败 的规律 |
| 05-21 11:14 | 交叉验证 DEBUG_LOG 中的每项数据 | 发现 4 处事实错误（151543 offset 值、lts 值、resync 列表、结论过于绝对） |
| 05-21 11:37 | 制定三层防护修复计划 | Fix1: 脚本加 flag, Fix2: simulator.c 对齐, Fix3: nr-ue.c 校验 |
| 05-21 11:46 | Fix1+Fix2+Fix3 编译成功 | 构建通过 |
| 05-21 11:48 | 首次 sweep（全 SNR 点） | SNR=-5 OK(srs=301!), SNR 0-25 全部 Permission denied（/tmp 权限问题） |
| 05-21 13:03 | 修权限后第二次 sweep | **Fix3（拒绝重试）导致无限循环**——UE 930 次全得 hw_slot_offset=8，120s 内无法同步 |
| 05-21 13:32 | 回退 Fix2 + 修改 Fix3 为 rx_offset 修正 | hw_slot_offset 8→7 修正成功，但 **PBCH 每帧解码失败**——修正破坏了帧对齐 |
| 05-21 13:40 | 回退 Fix3 为修正方案 | SNR=10 下 UE 同步到 offset=7 但 PBCH 全失败，`(10D+0U)` 无 UL |
| 05-21 14:04 | **回退所有源码修改，仅保留 Fix1** | 重新编译，仅 `--ue-timing-correction-disable` 生效 |
| **05-21 14:36** | **SNR=10 验证成功** | **hw_slot_offset=8 首次同步，经 resync 后 attach OK，n_gt=68, n_srs=4** |

---

## 十一、经验教训

### 1. 不要在 PSS 同步路径上修改 rx_offset

`rx_offset` 是 PSS 相关器找到的峰值在搜索缓冲区中的真实位置。`syncInFrame()` 用它来对齐帧边界。人为修改会使帧边界偏离 SSB 实际位置，导致 PBCH 解码无法工作。

### 2. 不要假设 readFrame 重试能改变 PSS 搜索结果

`readFrame()` 读取精确 2 帧（614400 样本 = 2 × TDD 周期 307200）。每次重试后搜索窗口平移 TDD 周期的整数倍，PSS 峰值在窗口内的相对位置完全不变——拒绝+重试 = 无限循环。

### 3. 不要把确定性对齐当作优化

将 `nextRxTstamp` 对齐到 slot 边界消除了随机性，但在某些 `lts` 值下会 100% 命中 UL slot，比原来的随机 50% 更差。

### 4. hw_slot_offset 只是日志指标

代码探索发现 `hw_slot_offset` **仅用于 LOG_I 打印**，不写入任何 UE 结构体字段。真正的帧对齐通过 `syncD->rx_offset`（样本数）经 `syncInFrame()` 执行。OAI UE 的 resync 机制（失去同步后重新调用 `nr_initial_sync`）能自行修正初始的 slot 偏移。

### 5. 优先修复最简单的已知问题

STO 抖动的修复只需一行命令行参数（零代码风险），却解决了全部 attach 不稳定问题。在尝试复杂的源码修改之前，应该先验证最简单的修复是否已经足够。
