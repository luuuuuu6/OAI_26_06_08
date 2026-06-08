# v7.py Changelog — Attach-Stable Auto Handoff

> 基于 v6.py，针对 v6 动态信道导致初始 attach / RRC / SRS 不稳定的问题增加稳定启动机制。  
> 日期：2026-05-02  
> 相关文件：`v7.py`、`launch_all_v7.sh`、`run_q4_snr_sweep_v7.sh`

---

## 0. 背景

v6 修复了 carrier frequency、sample_times、velocity、P1B distance、global normalization 等物理问题后，信道动态更真实，但在 OAI attach 初期暴露出控制面不稳定：

- 默认动态信道下，UE 常能完成初始同步，但 gNB 侧大量 RA 停在 `WAIT_Msg3`
- UE 侧可能生成 `RRCSetupComplete`，但 gNB 未稳定收到，导致无法进入 `RRCReconfiguration`
- periodic SRS 需要 `RRCReconfigurationComplete` 后才真正启用，因此表现为 `SRS=0`

对照实验确认：

| 配置 | 结果 | 结论 |
|---|---:|---|
| `UE_SPEED=3`, stable 45s | `PARTIAL`, `srs=0` | 过早进入动态信道，attach / SRS 配置不稳 |
| `UE_SPEED=0`, stable 300s | `OK`, `srs=1` | 长稳定窗口能完成 SRS 配置 |
| `UE_SPEED=3`, stable 300s | `OK`, `srs=1` | 问题不是 `Speed=3` 本身，而是切动态时机 |
| `UE_SPEED=3`, auto trigger | `OK`, `srs=1~2` | 事件触发切动态可替代固定等待 |

---

## 1. v7 核心设计

### 1.1 Attach-Stable Window

`v7.py` 启动时先冻结跨 batch 的 `sample_times`：

```python
sample_times_now = _build_sample_times(0)
```

这样 RRC / NAS / SRS 配置阶段看到的是准静态信道，避免 v6 默认动态信道在控制面尚未完成时造成 UL Msg3、SRB1、RRCReconfiguration 等过程不稳定。

### 1.2 Auto Handoff

v7 不再只依赖固定秒数，而是支持自动触发切换：

1. `launch_all_v7.sh` 监控 `gnb.log`
2. 默认等待 gNB 侧出现：

```text
Received RRCReconfigurationComplete
```

3. 再等待 `ATTACH_STABLE_POST_DELAY` 秒
4. 创建触发文件：

```text
/tmp/oai_gpu_ipc/v7_dynamic_enable
```

5. `v7.py` 的 ChannelProducer 检测到该文件后切换为动态 `sample_times`

触发日志示例：

```text
[attach-stable-watcher] trigger matched (rrc_reconfig); sleeping 15s
[attach-stable-watcher] trigger file written: /tmp/oai_gpu_ipc/v7_dynamic_enable
[v7 AttachStable] dynamic channel handoff triggered by file: /tmp/oai_gpu_ipc/v7_dynamic_enable
```

### 1.3 Fallback Ceiling

如果 auto trigger 一直没有出现，`--attach-stable-sec` 仍作为最大稳定窗口：

```text
ATTACH_STABLE_SEC=300
```

到达上限后强制切动态，避免无限稳定等待。

---

## 2. 新增 / 修改参数

### 2.1 `v7.py`

| 参数 | 默认值 | 作用 |
|---|---:|---|
| `--attach-stable-sec` | `300.0` | 最大稳定窗口；auto 模式下是 fallback ceiling |
| `--attach-stable-mode` | `auto` | `auto` 由触发文件切动态；`time` 只按固定时间切动态 |
| `--attach-stable-trigger-file` | `/tmp/oai_gpu_ipc/v7_dynamic_enable` | auto 模式下 ChannelProducer 监听的触发文件 |
| `--p1b-distance-mode` | `default` | P1B 情况下默认使用 `--default-distance-m`，避免 tau 推导距离塌陷 |
| `--default-distance-m` | `100.0` | P1B default 模式和无 P1B fallback 的 BS-UE 距离 |

### 2.2 `launch_all_v7.sh`

| 参数 / 环境变量 | 默认值 | 作用 |
|---|---:|---|
| `ATTACH_STABLE_SEC` / `-stable` | `300` | 最大稳定窗口 |
| `ATTACH_STABLE_MODE` / `-stable-mode` | `auto` | auto 或 time |
| `ATTACH_STABLE_TRIGGER` / `-stable-trigger` | `rrc_reconfig` | 触发事件：`rrc_reconfig`、`srs`、`off` |
| `ATTACH_STABLE_POST_DELAY` / `-stable-delay` | `15` | 触发事件后延迟切动态 |
| `ATTACH_STABLE_TRIGGER_FILE` | `/tmp/oai_gpu_ipc/v7_dynamic_enable` | 触发文件路径 |

### 2.3 `run_q4_snr_sweep_v7.sh`

新增对上述 auto-stable 参数的环境变量读取、打印、manifest 记录和参数透传。

---

## 3. 验证结果

### 3.1 固定 300s 稳定窗口

命令核心参数：

```bash
ONLY_SNR="10" UE_SPEED=3 ATTACH_STABLE_SEC=300 \
P1B_DISTANCE_MODE=default DEFAULT_DIST_M=100 \
MAX_FRAMES=100 MIN_SRS_BINS=1 HARD_CEILING_SEC=360 \
bash run_q4_snr_sweep_v7.sh
```

代表结果：

```text
SNR=10 dB  status=OK  gt=45  srs=1
```

结论：`UE_SPEED=3` 本身可以工作，只要 attach 阶段不太早进入动态 `sample_times`。

### 3.2 Auto trigger 默认策略

默认参数：

```text
ATTACH_STABLE_MODE=auto
ATTACH_STABLE_TRIGGER=rrc_reconfig
ATTACH_STABLE_POST_DELAY=15
ATTACH_STABLE_SEC=300
```

代表结果：

```text
SNR=10 dB  status=OK  gt=23  srs=1
SNR=10 dB  status=OK  gt=25  srs=2
```

结论：auto trigger 能在未等满 300s 的情况下切动态，并仍然产出 SRS bin。

---

## 4. NMSE / 数据价值初步结论

对 `q4_sweep_20260502_175618/snr_10dB` 运行：

```bash
python3 q4_convergence_sweep.py \
  --sweep-dir /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/logs/q4_sweep_20260502_175618 \
  --plot-dir /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/data_out
```

对齐质量：

```text
raw(srs=101, gt=2471) -> paired 101
median gap = 3 slots
shape = 2x2, 2048 SC
```

N 轴结果显示短窗口有效、长窗口跨动态/失稳阶段后崩坏：

| N | NMSE | RSRP err | PDP corr |
|---:|---:|---:|---:|
| 10 | `-6.35 dB` | `0.90 dB` | `0.9999` |
| 15 | `-7.25 dB` | `0.75 dB` | `0.9997` |
| 20 | `-6.94 dB` | `0.80 dB` | `0.9994` |
| 30 | `-6.83 dB` | `0.82 dB` | `0.9998` |
| 100 | `+14.99 dB` | `15.13 dB` | `0.2884` |

当前建议：

- 用 `--fixed-n 15` 作为短窗口代表值做初步 SNR sweep
- 不要把默认 `N=100` 作为当前 v7 动态链路的代表 NMSE
- `PDP corr` 和 `RSRP err` 说明功率时延结构已有分析价值
- `sigma1_corr` 仍低，细粒度 MIMO / 频域奇异值结构还需要继续诊断

生成的 N=15 报告：

```text
/home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/data_out/q4_convergence_report_v7_auto_N15.json
```

---

## 5. 已知风险 / 后续方向

- auto handoff 后仍可见 `No SRS signal`、`Invalid timing advance offset`、`UL Failure`，说明动态切换后的链路仍不完全稳定
- ChannelProducer 仍有 `ring buffer full / dropped`，可能影响 UL 控制面和长窗口 NMSE
- 后续应比较 `ATTACH_STABLE_POST_DELAY=15/30`、`ATTACH_STABLE_TRIGGER=rrc_reconfig/srs` 的稳定性
- 全 SNR sweep 前建议先固定 `--fixed-n 15`，并保留 N-axis 曲线用于判断每个 SNR 点的有效窗口

---

## 6. 修改文件汇总

| 文件 | 类型 | 改动内容 |
|---|---|---|
| `v7.py` | Python | 新增 attach-stable auto/time 模式、触发文件监听、P1B fixed distance 默认策略 |
| `launch_all_v7.sh` | Shell | 新增 gNB 日志 watcher，检测 RRC/SRS 事件并写 trigger file |
| `run_q4_snr_sweep_v7.sh` | Shell | 新增 auto-stable 环境变量透传、manifest 记录 |
| `v6_before_v7_attach_stable_20260502.py` | Python | v6 修改前备份 |
