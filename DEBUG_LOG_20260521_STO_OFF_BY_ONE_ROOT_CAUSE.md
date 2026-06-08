# SRS/GT +1 样本 STO Off-by-One 根因分析

| 字段 | 值 |
|------|----|
| **日期** | 2026-05-21 |
| **目标** | 定位 SRS 与 GT 之间恒定 +1.000 样本时间偏移（STO）的根因 |
| **结论** | `simulator.c` 第 1495 行 UE DL 初始同步公式 `nextRxTstamp = lts - nsamps` 将 Proxy 的 inclusive-end 时间戳误当作 exclusive-end，导致 UE 全局时间轴偏移 -1 样本 |
| **关联** | 这是"遗留问题 2（STO ±1 样本抖动）"和"hw_slot_offset 不稳定"的**共同根源** |
| **前序** | 同日完成 hw_slot_offset 分析和 `--ue-timing-correction-disable` 修复（见 `DEBUG_LOG_20260521_HW_SLOT_OFFSET_ROOT_CAUSE.md`） |

---

## 一、问题发现

在禁用 STO 抖动（`--ue-timing-correction-disable`）后，SNR=10 sweep 成功（status=OK），但 `eval_pdpR_nmse.py` 分析显示 **NMSE LS-per-antenna = +4.76 ~ +5.66 dB**（正值，误差大于信号）。

| SNR | NMSE（无 STO 校正） | NMSE（per-frame STO 校正） |
|:---:|:-------------------:|:-------------------------:|
| 0 dB | +4.76 dB | **-12.06 dB** |
| 5 dB | +5.66 dB | **-4.63 dB** |
| 15 dB | +4.87 dB | **-9.87 dB** |
| 25 dB | +4.97 dB | **-8.15 dB** |

Per-frame STO 校正后 NMSE 大幅改善（10~17 dB），说明 SRS/GT 之间存在显著的采样时间偏移。

---

## 二、STO 测量

使用 `eval_pdpR_nmse.py --sto-correct per-frame --dump-sto` 精确测量每帧 STO：

| SNR | STO mean | STO std | STO median | STO range |
|:---:|:--------:|:-------:|:----------:|:---------:|
| 0 dB | +0.993 | **0.062** | **+1.000** | [+0.22, +1.02] |
| 15 dB | +0.996 | **0.102** | **+0.999** | [+0.11, +1.67] |
| 25 dB | -0.418 | 8.751 | **+0.999** | [-58.34, +2.02] |

**关键发现**：STO median 恒定为 **+1.000 样本**，std 极小（0.06~0.1）。这不是随机抖动，是**系统性的恒定偏移**。

---

## 三、与 PI 控制器（遗留问题 2）的关系

| 状态 | PI 控制器 | STO 表现 | 说明 |
|------|:---------:|---------|------|
| 修复前 | ON (`UE_no_timing_correction=0`) | 逐帧 ±1 样本振荡 | PI 检测到 +1 偏移，尝试补偿但过冲→振荡 |
| 修复后 | OFF (`UE_no_timing_correction=1`) | 恒定 +1.000 样本 | 偏移暴露为常量 |

**PI 控制器不是 bug 的根源——它在试图修复这个 +1 偏移，但补偿方式是振荡式的**（第 N 帧补偿 +1，第 N+1 帧发现过冲→补偿 -1，无限循环）。这就是"遗留问题 2"中 STO ±1 抖动的全部机制。

---

## 四、根因追踪

### 4.1 GPU-IPC V7 共享内存时间戳语义

V7 ring buffer 有两套时间戳语义并存：

| 语义 | 含义 | 谁使用 |
|------|------|--------|
| **START**（块首样本索引） | 覆盖范围 `[T, T+nsamps)` | `v7_circ_write`、OAI 的 `*ptimestamp` |
| **END inclusive**（块末样本索引） | 值 = `T + nsamps - 1` | `v7_circ_read` consumer_ts、Proxy 的 `last_*_rx_ts` |

### 4.2 写入端（`v7_circ_write`, `gpu_ipc_v7.c:318-321`）

```c
// 写入时保存 **START** 时间戳
if (last_ts_off >= 0) {
    *shm_u64(ctx->shm_raw, last_ts_off) = timestamp;       // START
    if (last_nsamps_off >= 0)
        *shm_u32(ctx->shm_raw, last_nsamps_off) = (uint32_t)nsamps;
}
```

gNB 调用 `gpu_ipc_v7_dl_write(timestamp=T)` → SHM `last_dl_tx_ts = T`（**START**）

### 4.3 Proxy 处理（`v8.py:3620-3630, 3284`）

```python
# Proxy 读取 gNB 写入的 START 时间戳
cur_dl_ts = self.ipc_gnb.get_last_dl_tx_ts()      # = T (START)
dl_nsamps = self.ipc_gnb.get_last_dl_tx_nsamps()   # = S
gnb_dl_head = cur_dl_ts + dl_nsamps                 # = T + S (exclusive end) ✓

# Proxy 处理后写入 DL RX 数据，通知 UE
self.ipc_ues[k].set_last_dl_rx_ts(int(start_ts + delta - 1))  # = T + S - 1 (END inclusive)
```

Proxy 将 **exclusive end** (`T + S`) 转换为 **inclusive end** (`T + S - 1`) 写入 `last_dl_rx_ts`。这是为了配合 `v7_circ_read` 的就绪判断逻辑 (`last >= target_ts + nsamps - 1`)，**设计上是正确的**。

### 4.4 读取端就绪判断（`v7_circ_read`, `gpu_ipc_v7.c:346-350`）

```c
uint64_t need_end = target_ts + (uint64_t)nsamps - 1;    // END inclusive
uint64_t last = *shm_u64(ctx->shm_raw, last_ts_off);     // Proxy 写的 END inclusive
if (last >= need_end)  // 数据就绪 ✓
```

这里的语义是一致的：producer 和 consumer 都用 END inclusive，**没有 bug**。

### 4.5 Bug 所在：UE DL 初始同步（`simulator.c:1492-1497`）

```c
if (t->nextRxTstamp == 0) {
    uint64_t lts = *((volatile uint64_t *)(t->gpu_ipc_v7.shm_raw + _V7_OFF_LAST_DL_RX_TS));
    if (lts > 0) {
        t->nextRxTstamp = lts > (uint64_t)nsamps ? lts - (uint64_t)nsamps : 0;
        //                                          ^^^^^^^^^^^^^^^^^^^^
        //                                          BUG: lts 是 END inclusive (T+S-1)
        //                                          lts - nsamps = T+S-1-S = T-1
        //                                          应该是 T，差了 1 样本
    }
}
```

**`lts` 读取的是 `_V7_OFF_LAST_DL_RX_TS`（Proxy 写入的 END inclusive = `T + S - 1`）**。

公式 `lts - nsamps` 隐式假设 `lts` 是 **exclusive end**（即 `T + S`），但实际是 **inclusive end**（即 `T + S - 1`）。

结果：`nextRxTstamp = (T + S - 1) - S = T - 1`，比正确值 `T` **少了 1 个样本**。

### 4.6 偏移如何传播到 SRS/GT

```
UE DL 初始同步: nextRxTstamp = T - 1 (比 Proxy 的数据块起始早 1 样本)
        ↓
UE 所有后续读取基于此偏移（nextRxTstamp += nsamps 保持偏移不变）
        ↓
UE 帧边界整体偏移 -1 样本
        ↓
UE 发送 UL SRS 的时间戳也偏移 -1 样本
        ↓
Proxy 收到 UE 的 UL 数据，基于 UE 的时间戳应用信道模型并保存 GT
GT slot_id = ue_ul_ts // 30720（基于 UE 偏移后的时间戳）
        ↓
gNB 读取 UL 数据（gNB 的 nextRxTstamp 从 0 开始，未受影响）
gNB 在 nextRxTstamp 处估计 SRS
        ↓
SRS 时间戳（gNB 视角）与 GT 时间戳（UE 视角）差 1 样本
```

---

## 五、完整数据流对比

以一个 slot（nsamps=30720）为例，Proxy 写入的 DL 数据从 T=0 开始：

| 步骤 | 组件 | 时间戳值 | 语义 |
|------|------|:--------:|------|
| 1 | gNB 写 DL TX | `last_dl_tx_ts = 0` | START |
| 2 | Proxy 读 gNB | `gnb_dl_head = 0 + 30720 = 30720` | exclusive end |
| 3 | Proxy 写 DL RX | `last_dl_rx_ts = 0 + 30720 - 1 = 30719` | END inclusive |
| 4 | **UE 初始同步** | `nextRxTstamp = 30719 - 30720 = -1` → clamp 到 0 | **本应是 0，恰好正确** |

第二个 slot（Proxy 写入 T=30720 到 T=61439）：

| 步骤 | 组件 | 时间戳值 | 语义 |
|------|------|:--------:|------|
| 1 | gNB 写 DL TX | `last_dl_tx_ts = 30720` | START |
| 2 | Proxy 读 gNB | `gnb_dl_head = 30720 + 30720 = 61440` | exclusive end |
| 3 | Proxy 写 DL RX | `last_dl_rx_ts = 30720 + 30720 - 1 = 61439` | END inclusive |
| 4 | **UE 初始同步** | `nextRxTstamp = 61439 - 30720 = 30719` | **应该是 30720，差了 1** |

实测数据验证（run `134704`）：

```
GPU IPC V7 UE DL initial sync: nextRxTstamp=2949119 (last_dl_rx_ts=2979839)
  2979839 - 30720 = 2949119 ✓（公式计算一致）
  2979839 = T + delta - 1，若 delta=30720，则 T = 2979839 - 30720 + 1 = 2949120
  正确的 nextRxTstamp 应该是 2949120，实际得到 2949119，差 1 样本 ✓
```

---

## 六、修复方案

### 方案：修正 `simulator.c` 初始同步公式

```c
// 修正前（bug）
t->nextRxTstamp = lts > (uint64_t)nsamps ? lts - (uint64_t)nsamps : 0;

// 修正后
t->nextRxTstamp = lts >= (uint64_t)nsamps ? lts - (uint64_t)nsamps + 1 : 0;
```

**改动说明**：
- `lts - nsamps + 1`：将 inclusive end (`lts = T + S - 1`) 正确转换为前一个块的 START (`T + S - 1 - S + 1 = T`)
- 同时将 `>` 改为 `>=`：当 `lts == nsamps` 时（即 `lts = S - 1 + 1 = S`？），确保不会下溢

**预期效果**：
1. UE 全局时间轴偏移从 -1 变为 0
2. SRS/GT 之间的 STO 从 +1.000 变为 ~0.000
3. `eval_pdpR_nmse.py` 无需任何 STO 校正即可达到 -7~-12 dB NMSE
4. PI 控制器（即使启用）不再有 +1 偏移需要补偿 → 不再振荡

**风险**：
- 极低。仅改变 V7 UE DL 首次读取的起始位置，偏移量为 1 样本（vs 30720 样本/slot）
- 对 `hw_slot_offset` 的影响可忽略（1/30720 ≈ 0.003%）
- 不影响 gNB 侧读取（gNB 从 `nextRxTstamp=0` 开始，不依赖 `lts`）

---

## 七、验证计划

1. **编译 OAI**（仅 `simulator.c` 改动）
2. **跑 SNR=10 单点 sweep**
3. **STO 验证**：`eval_pdpR_nmse.py --sto-correct per-frame --dump-sto`
   - 预期：STO median ≈ 0.000（之前是 +1.000）
4. **NMSE 验证**：`eval_pdpR_nmse.py`（无任何 STO 参数）
   - 预期：NMSE LS-per-antenna ≈ -7 ~ -12 dB（之前无校正时是 +5 dB）
5. **PI 控制器验证**（可选）：去掉 `--ue-timing-correction-disable` 重跑
   - 预期：STO 不再振荡（因为偏移已修复，PI 控制器没有需要补偿的量），NMSE 仍然正常

---

## 八、三个问题的统一根因

本次调试过程中分析的三个独立现象，实际上**共享同一个根因**：

```
simulator.c:1495 — lts - nsamps（off-by-one）
    │
    ├──► UE nextRxTstamp 偏移 -1 样本
    │       │
    │       ├──► PSS 搜索窗口偏移 → hw_slot_offset 在 7/8 边界不稳定
    │       │     （遗留问题 3: hw_slot_offset 随机）
    │       │
    │       ├──► UE 帧边界偏移 -1 样本 → PI 控制器检测到偏移 → ±1 振荡
    │       │     （遗留问题 2: STO ±1 抖动）
    │       │
    │       └──► SRS/GT 时间戳对不齐 → NMSE +5 dB floor
    │             （本次发现: +1 样本 STO）
    │
    └──► 修复 lts - nsamps + 1 后，三个问题同时解决
```

---

## 九、关键代码文件索引

| 文件 | 位置 | 作用 |
|------|------|------|
| `radio/rfsimulator/simulator.c:1495` | UE DL V7 初始同步 | **Bug 所在**：`lts - nsamps` 应为 `lts - nsamps + 1` |
| `radio/rfsimulator/gpu_ipc_v7.c:318-321` | `v7_circ_write` | 写入 START 时间戳（正确） |
| `radio/rfsimulator/gpu_ipc_v7.c:404-405` | `v7_circ_read` consumer_ts | 写入 END inclusive 时间戳（正确，配合 Proxy） |
| `radio/rfsimulator/gpu_ipc_v7.h` | SHM 布局 | `_V7_OFF_LAST_DL_RX_TS`=320（Proxy 写 END inclusive） |
| `vRAN_Socket/.../v8.py:3284` | Proxy DL 通知 | `set_last_dl_rx_ts(start_ts + delta - 1)`（END inclusive，正确） |
| `vRAN_Socket/.../v8.py:693-709` | Proxy SHM 读写 | 硬编码 offset 与 `.h` 一致 |
| `vRAN_Socket/.../eval_pdpR_nmse.py:155` | NMSE 分析 | `--sto-fixed-samples` 临时补偿（根因修复后应恢复 None） |
