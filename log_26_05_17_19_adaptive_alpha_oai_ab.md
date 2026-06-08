# Adaptive Alpha 算法开发、OAI 集成与 A/B 验证完整日志

| 字段 | 值 |
|------|----|
| **时间跨度** | 2026-05-17 ~ 2026-05-20 |
| **核心目标** | 将 Python 层 adaptive α 算法落地到 OAI C 侧，建立 ewma vs adaptive 自动化 A/B 对比闭环；服务器迁移 (dclcom57→dclcom61, RTX 5090 Blackwell) 与评估口径修复 |
| **前序工作** | `26_05 中旬日志整合.md` (05-09~05-15)、`log_26_05_17_adaptive_alpha_cdl.md` (05-17) |
| **关键路径** | `DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/` (下称 `G1C/`) |
| **OAI 路径** | `DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/` |

---

## 目录

- [第一部分：算法方向探索与路径淘汰 (05-17)](#第一部分算法方向探索与路径淘汰-05-17)
- [第二部分：二维映射收敛与 CDL 全模型标定 (05-17 ~ 05-18)](#第二部分二维映射收敛与-cdl-全模型标定-05-17--05-18)
- [第三部分：仿真地基修复 — RNG 独立性 (05-18)](#第三部分仿真地基修复--rng-独立性-05-18)
- [第四部分：CDL 组合搜索与最优子集 (05-18)](#第四部分cdl-组合搜索与最优子集-05-18)
- [第五部分：OAI C 侧 Adaptive 集成 (05-19)](#第五部分oai-c-侧-adaptive-集成-05-19)
- [第六部分：A/B 自动化框架与运行 (05-19)](#第六部分ab-自动化框架与运行-05-19)
- [第七部分：结果异常溯源与流程重构 (05-19)](#第七部分结果异常溯源与流程重构-05-19)
- [第八部分：服务器迁移 dclcom57→dclcom61 (05-20)](#第八部分服务器迁移-dclcom57dclcom61-05-20)
- [第九部分：评估口径根因诊断与修复 (05-20)](#第九部分评估口径根因诊断与修复-05-20)
- [第十部分：关键发现与教训](#第十部分关键发现与教训)
- [第十一部分：产出文件完整清单](#第十一部分产出文件完整清单)
- [第十二部分：当前状态与下一步](#第十二部分当前状态与下一步)

---

# 第一部分：算法方向探索与路径淘汰 (05-17)

## 1.1 起点：从 plan 恢复上下文

从 `adaptive_alpha_cdl_plan` 和 `log_26_05_17_adaptive_alpha_cdl.md` 恢复。
Phase 1 已完成（1D `ρ_obs` 方案 FAIL、`rot_rate` 替代方案初步成功）。
本轮目标：推进到 Phase 2（build→capture gate→generalization gate）并最终落地 OAI。

## 1.2 路径 1：ρ_obs 作为自适应特征 — 淘汰

| 项目 | 结论 |
|------|------|
| 问题 | ρ_obs（lag-1 自相关系数）对 Doppler/SNR 混合敏感，连续映射不稳定 |
| 实验 | capture gate 多次 FAIL，映射 R² 过低 |
| 结论 | **淘汰**，不作为一维特征继续 |

## 1.3 路径 2：rot_rate 作为 Doppler 传感器 — 保留

替换 ρ_obs 为 `rot_rate = |angle(rot[n]) − angle(rot[n−1])|`，物理含义更纯（直接测量帧间相位旋转速度）。

### DualEMASRS2DFilter 架构修正

发现原始架构 bug：de-rotation 使用了全局参考而非自参考。
修改为双路自参考 de-rotation + 独立 rot_rate 提取。

### 1D rot_rate 映射结果

CDL-C 30km/h 单模型标定下：`α = f(rot_rate)` 线性映射成立，capture gate 通过 (8/8 PASS)。

## 1.4 路径 3：innovation（滤波残差）作为 2D 特征 — 淘汰

| 项目 | 结论 |
|------|------|
| 问题 | innovation 受 α 本身影响 → 形成正反馈闭环 → 不稳定 |
| 物理诊断 | 当 α 偏大时 innovation 偏大 → 进一步推高 α → 发散 |
| 结论 | **淘汰**，闭环特征本质上不适合控制 α |

---

# 第二部分：二维映射收敛与 CDL 全模型标定 (05-17 ~ 05-18)

## 2.1 为什么需要二维

1D `rot_rate→α` 在固定 SNR 下有效，但跨 SNR 泛化失败。
原因：α_opt 同时依赖 Doppler（决定最优追踪速度）和 SNR（决定噪声压制需求）。

## 2.2 SNR 估计方案评估

| 方案 | 原理 | 结论 |
|------|------|------|
| A: 相邻 SC 差分 | `|H[k+1]−H[k]|²` | 子载波间隔变化时不鲁棒 |
| B: Even-Odd Pair | `|H[2k]−H[2k+1]|²` 含 C_model 校正 | **选用** — 开环、无反馈、可C化 |
| C: 外部 SINR 报告 | 依赖上层协议栈 | 延迟大、跨层依赖重 |

### Even-Odd Pair SNR 估计

```
noise_est = mean(|H[even] − H[odd]|²) / 2
signal_est = mean(|H|²) − noise_est − C_model
SNR_dB = 10·log₁₀(signal_est / noise_est)
```

`C_model` 是信号残差校正项（信道在相邻子载波间的固有差异），从无噪 GT 离线标定。

## 2.3 2D 映射定型

```
α = clamp(A1·rot_rate + A2·SNR_dB + A3, α_min, α_max)
```

其中 `rot_rate` 和 `SNR_est` 各自经 EMA 平滑后输入映射。

## 2.4 CDL 全模型引入

将 CDL-A/B/C/D/E 五种标准模型全部接入仿真框架，目标是找到泛化最优的训练组合。

| CDL 模型 | 特征 | 用途 |
|----------|------|------|
| CDL-A | NLOS, 丰富多径 | 城区微蜂窝 |
| CDL-B | NLOS, 中等时延扩展 | 城区宏站 |
| CDL-C | NLOS, 300ns 时延扩展 | 基线标定 |
| CDL-D | LOS 主径 + 弱散射 | 空旷场景 |
| CDL-E | LOS, 大 K-factor | 高楼/工厂 |

### P1B Ray-Tracing 数据验证

| 项目 | 结论 |
|------|------|
| 载频修正 | v8.py 硬编码 3.5 GHz，之前 P1B 测试误用 7.5 GHz |
| 3.5 GHz 重测 | 3/5 RX 大面积 FAIL |
| B-4 去混淆实验 | 排除 SNR 特征失效，确认 P1B 超出 (rot_rate, SNR) 两维描述能力 |
| 结论 | P1B 归为 scope 边界，不参与训练标定 |

---

# 第三部分：仿真地基修复 — RNG 独立性 (05-18)

## 3.1 Bug 1：AWGN 实虚部相同

**现象**：CuPy 加速后标定结果异常偏离 NumPy 基线。

**根因**：`add_awgn()` 中对实部和虚部分别调用 `np.random.default_rng(seed)`，导致每次重新初始化——两部分产生相同随机序列。

**修复**：单一 `rng` 对象，实虚部顺序抽取。

```python
# Before (bug)
rng_r = np.random.default_rng(seed)
rng_i = np.random.default_rng(seed)  # ← 与 real 完全相同！

# After (fix)
rng = np.random.default_rng(seed)
noise = rng.standard_normal(shape) + 1j * rng.standard_normal(shape)
```

## 3.2 Bug 2：信道/噪声 seed 耦合

**现象**：信道生成和噪声生成使用同一 seed，导致两者统计相关。

**修复**：传入同一 `rng` 对象并顺序消费（channel 先画、noise 后画），状态自然推进，保证不重叠。

## 3.3 CDL-C 单模型复测

修复后重跑 CDL-C only 标定：

| 指标 | 修前 | 修后 |
|------|------|------|
| Capture gate | 不稳定 | **8/8 PASS** |
| 结论 | RNG 污染结果 | 地基确认 |

---

# 第四部分：CDL 组合搜索与最优子集 (05-18)

## 4.1 搜索方法

- 遍历 CDL-A/B/C/D/E 的 31 个非空子集
- 每个子集：用子集数据标定 2D mapping 系数 → 在全 5 模型上跑 gate 测试
- 两阶段评估：标定精度 + 泛化 gate PASS 率

## 4.2 结果

| 最优组合 | 说明 |
|----------|------|
| **C+E** | NLOS+LOS 互补，跨模型泛化最佳 |

## 4.3 最终系数

```
A1 = 0.113    (rot_rate 系数)
A2 = 0.031    (SNR_dB 系数)
A3 = 0.107    (截距)
C_MODEL = ...  (even-odd 校正)
α_min = 0.01, α_max = 0.5
α_init = 0.1
RR_EMA = 0.1, SNR_EMA = 0.05
```

---

# 第五部分：OAI C 侧 Adaptive 集成 (05-19)

## 5.1 改动范围

| 文件 | 改动 |
|------|------|
| `nr_srs_2d_filter.c` | 新增 `adaptive_state_t` 结构体 + `update_adaptive_core()` 函数 |
| `nr_srs_2d_filter.h` | 新增 adaptive method enum + 环境变量文档 |

## 5.2 C 侧架构

```c
typedef struct {
    float rot_rate_ema;
    float snr_ema;
    float alpha_current;
    float prev_phase;       // 上一帧内积相位
    int   initialized;
} adaptive_state_t;

// 每 SRS 帧调用
void update_adaptive_core(adaptive_state_t *st,
                          int16_t *H_new, int16_t *H_smooth,
                          int n_active, int *active_sc);
```

核心逻辑：
1. 计算 `H_new` 与 `H_smooth` 的内积 → 提取帧间相位差 → `rot_rate`
2. Even-Odd Pair SNR 估计（含 C_model）
3. rot_rate / SNR 各自 EMA 平滑
4. 2D 线性映射 → α
5. EMA 更新 `H_smooth`

## 5.3 运行时切换

```bash
export SRS_2D_METHOD=ewma       # 原始 EWMA (默认)
export SRS_2D_METHOD=adaptive   # 新 Adaptive Alpha
```

所有 adaptive 系数均可通过环境变量覆盖：

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `SRS_2D_ADAPT_A1` | rot_rate 系数 | 0.113 |
| `SRS_2D_ADAPT_A2` | SNR 系数 | 0.031 |
| `SRS_2D_ADAPT_A3` | 截距 | 0.107 |
| `SRS_2D_ADAPT_CMODEL` | even-odd C_model | ... |
| `SRS_2D_ADAPT_ALPHA_MIN` | α 下限 | 0.01 |
| `SRS_2D_ADAPT_ALPHA_MAX` | α 上限 | 0.5 |
| `SRS_2D_ADAPT_ALPHA_INIT` | α 初始值 | 0.1 |
| `SRS_2D_ADAPT_RR_EMA` | rot_rate 平滑系数 | 0.1 |
| `SRS_2D_ADAPT_SNR_EMA` | SNR 平滑系数 | 0.05 |

## 5.4 回退机制

adaptive 模式下如遇异常（无活跃子载波、内积过低、even-odd 对不足），自动回退到 EWMA 本帧逻辑。

---

# 第六部分：A/B 自动化框架与运行 (05-19)

## 6.1 脚本体系

```
run_q4_snr_ab_compare_v8.sh          ← A/B 顶层控制器
  └─ run_q4_snr_sweep_v8.sh          ← 单 method sweep (capture-only)
       └─ launch_all_v8.sh           ← 单 SNR 点启动 (gNB + UE + proxy)

run_q4_postprocess_v8.sh             ← 独立后处理 (prepare + test)
```

## 6.2 A/B 流程

```bash
sudo METHODS="ewma adaptive" \
     SNR_POINTS="5 10 20" \
     MAX_FRAMES=100 \
     bash run_q4_snr_ab_compare_v8.sh
```

对每个 method：
1. 设置 `SRS_2D_METHOD={method}`
2. 调用 `run_q4_snr_sweep_v8.sh` 逐 SNR 点采集
3. 产出 manifest（gt/srs 数量与状态）
4. 打印后处理命令或自动后台执行

## 6.3 首轮运行结果

| 来源 | 说明 |
|------|------|
| `q4_ab_compare_20260519_134742/ewma` | EWMA sweep, SNR 0/5/10/20, status=OK |
| `q4_ab_compare_20260519_150510/adaptive` | Adaptive sweep, SNR 0/5/10/20, status=OK |

### eval_pdpR_nmse.py 直接评估 (per-frame STO)

| SNR | EWMA LS-NMSE | 帧数 | Adaptive LS-NMSE | 帧数 |
|-----|----------:|-----:|---------------:|-----:|
| 5 dB | -4.83 dB | 7 | +0.97 dB | 89 |
| 10 dB | -2.89 dB | 28 | -0.64 dB | 90 |
| 20 dB | -6.43 dB | 6 | +3.05 dB | 92 |

## 6.4 结果解读 → 不可信

**帧数严重不对等**是核心问题：

- EWMA 在 5dB/20dB 仅 7/6 帧（partial flush），adaptive 有 89-92 帧
- 帧数由 SRS 决定，GT 远多于 SRS（141 vs 7），但评估样本取决于 SRS

---

# 第七部分：结果异常溯源与流程重构 (05-19)

## 7.1 问题定位

| 问题 | 根因 | 影响 |
|------|------|------|
| EWMA 帧数异常少 | 低 SNR 下 RRC/MAC 不稳定 → SRS 调度稀疏 | 配对帧数由 SRS 决定 |
| 旧门限判完成标准 | 只看"SRS 文件数≥1"，不看文件内帧数 | 7 帧 partial flush 被判 OK |
| 口径混用 | `test_2d_mmse.py` 对在线输出做二次 EMA → 双重滤波 | NMSE 值失真 |
| 历史 -20dB 不可直接对比 | 来自离线 split-set 流程，不是在线配对口径 | 数值预期不一致 |

### gNB 日志证据（EWMA 5dB）

```
[SRS Dump] Flushing partial buffer: 7 frames
[SRS Dump] Written srs_matrix_gNB_2x2_seq0.bin (7 frames, seq 0)
[SRS Dump]   Captured : 7 frames
```

### gNB 日志证据（Adaptive 5dB）

```
[SRS Dump] Written srs_matrix_gNB_2x2_seq0.bin (100 frames, seq 0)
[SRS Dump]   Captured : 100 frames
```

### 低 SNR 链路不稳定证据

```
[NR_MAC]   Invalid timing advance offset for RNTI xxxx    ← 大量出现
[NR_RRC]   Send RRCReestablishment ...                      ← UE 重建
[SRS 2D] all states reset                                   ← 状态丢失
Scheduling retransmission of Msg3                           ← RA 重传
```

## 7.2 修复措施

### 修复 1：采集与后处理解耦

**改动**：`run_q4_snr_sweep_v8.sh` 移除内嵌的 `prepare_2d_mmse_data.py` + `test_2d_mmse.py`。

**新增**：`run_q4_postprocess_v8.sh` 独立脚本，支持 `--background` 后台模式。

**理由**：在线 A/B 输出的 SRS 已经过 C 侧滤波，不应再套一层 Python EMA。

### 修复 2：SRS 帧数硬门限

**改动**：新增 `MIN_SRS_FRAMES` 环境变量（默认=`MAX_FRAMES`），三处同步：

| 位置 | 作用 |
|------|------|
| `run_q4_snr_sweep_v8.sh` 外层 watcher | 等 SRS 帧数达标才 SIGTERM |
| `run_q4_snr_sweep_v8.sh` 完成判定 | GT + SRS bins + SRS frames 三重门限 |
| `launch_all_v8.sh` 内层 watcher | 同步门限逻辑 |

**实现**：通过 `sum_srs_frames()` 函数解析 SRS bin 文件头部 `n_frames` 字段，精确计数。

```bash
# 完成条件（三重 AND）
n_gt >= MAX_SEQS
n_srs_bins >= MIN_SRS_BINS
n_srs_frames >= MIN_SRS_FRAMES    ← 新增
```

### 修复 3：A/B 脚本清理

- 移除 `snapshot_post_analysis()`（不再拷贝 `data_out`）
- 新增 `AUTO_POSTPROCESS_BG` 选项（可选后台自动后处理）
- 每个 method 完成后打印独立后处理命令

---

# 第八部分：服务器迁移 dclcom57→dclcom61 (05-20)

## 8.1 迁移背景

将实验环境从 dclcom57 迁移到 dclcom61 (deepgadget)。新服务器配置：

| 组件 | 规格 |
|------|------|
| CPU | AMD Ryzen Threadripper 7960X (24C/48T) |
| GPU | 2x NVIDIA GeForce RTX 5090 (32GB, Blackwell sm_120) |
| RAM | 128 GB DDR5 |
| Storage | 916 GB NVMe |
| Driver | 580.95, CUDA 13.0 |

## 8.2 迁移步骤与遇到的问题

| 步骤 | 操作 | 问题 | 解决 |
|------|------|------|------|
| 1 | Docker 镜像加载 | 镜像名不匹配 | `docker-compose.yml` 修正镜像名 |
| 2 | 5GC 核心网启动 | 缺失 5GC compose | 从 `oai-cn5g` 目录拉起 |
| 3 | 路径修正 | 8个脚本硬编码 `/home/dclcom57/` | 全局替换为 `/home/dclcom61/` |
| 4 | CuPy GPU 失败 | `CUDA_ERROR_NO_BINARY_FOR_GPU` (sm_120) | 容器内 CUDA toolkit 12.3→12.8 升级 |
| 5 | NumPy/TF 兼容性 | CuPy 14 拉升 numpy 2.x → TF 2.17 崩 | CuPy 降回 13.6 + numpy 1.26 |
| 6 | TF GPU 失败 | TF 2.17 PTX 对 Blackwell 无效 (`INVALID_PTX`) | TF 2.17→2.21 升级 |
| 7 | TF GPU 无法识别 | TF 2.18 缺 cuDNN 9 | 安装 nvidia-cudnn-cu12 等 pip 包 + ldconfig |
| 8 | Sionna API 兼容 | Sionna 1.0.2 + TF 2.21 版本冲突 | Sionna 1.0.2→1.2.2 升级 |
| 9 | gNB 启动失败 | 缺 `libsctp.so.1` | `apt install libsctp1` |
| 10 | gNB 配置失败 | 缺 `libconfig.so.9` | `apt install libconfig9` |

## 8.3 RTX 5090 (Blackwell) 兼容性要点

TensorFlow 官方至今（2026-05）未发布带 sm_120 原生 kernel 的版本。NVIDIA 已停止发布 TF 容器（25.02 为最后一版）。

**TF 2.21 可以通过 PTX JIT 方式在 Blackwell 上运行**，但没有预编译的架构特定优化 kernel。实测性能：

- CuPy：正常（nvcc 12.8 JIT 编译，性能无影响）
- TF：PTX JIT 首次慢，后续从 `/root/.nv/ComputeCache/` 缓存读取
- 信道 Proxy 实测帧率：~3.5 ms/slot（正常，与源服务器持平）

### 最终容器内环境

| 组件 | 版本 |
|------|------|
| CUDA toolkit | 12.8 |
| TensorFlow | 2.21.0 |
| Sionna | 1.2.2 |
| CuPy | 13.6.0 |
| NumPy | 1.26.4 |
| cuDNN (pip) | 9.22.0 |

## 8.4 UE 接入随机失败问题

迁移后发现 UE 初始同步的 `hw_slot_offset` 会随机落在 7 或 8。TDD 配置中 slot 8 是 UL slot，若 UE 同步到此位置，SIB1 PDSCH 解码持续失败（数百次 NACK），导致 attach 超时。

| 对比 | hw_slot_offset=7 (正常) | hw_slot_offset=8 (失败) |
|------|-------------------------|-------------------------|
| SIB1 NACK | 0 | 762+ |
| Attach 时间 | ~120s | >775s 或超时 |
| SRS 采集 | 正常 | 从未配置 |

**结论**：非算法问题，是 OAI rfsim/GPU-IPC 场景下 PSS 同步的时序随机性。与 ewma/adaptive 方法选择无关。

---

# 第九部分：评估口径根因诊断与修复 (05-20)

## 9.1 症状

首次用 `test_2d_mmse.py` 评估 adaptive 在线输出时，NMSE 显示：

```
A raw      = +3.53 dB
B EMA      = +0.33 dB
C EMA+box  = +0.33 dB
```

首次用 `eval_pdpR_nmse.py --sto-correct per-frame` 评估时：

```
NMSE raw        : +46.13 dB
NMSE LS-aligned : +10.95 dB
```

两种工具均显示正值 NMSE——看似算法完全失败。

## 9.2 诊断过程

### 层级 1：确认 test_2d_mmse.py 口径不适用

`test_2d_mmse.py` 对 C 侧已滤波的输出再套 Python EMA → 双重滤波。且 adaptive 输出在 de-rotated 参考系中，与 GT 的绝对相位存在累积差异。**结论：此工具不适用于 adaptive 在线评估。**

### 层级 2：eval_pdpR_nmse.py 的全局 LS 对齐不够

| 对齐方式 | NMSE (dB) |
|----------|-----------|
| raw (无对齐) | +46.13 |
| LS global alpha | +10.95 |
| LS per-subcarrier alpha | +4.21 |
| LS per-(rx,tx,sc) alpha | -299.95 (自检通过) |

**per-(rx,tx,sc) 到 -300 dB 说明数据链路完好**，但跨天线的 alpha 变异系数 = 0.99 → 每根天线有独立的相位偏移。

### 层级 3：STO 系统性偏移

对精确配对帧 (delta=0) 做 per-(rx,tx) 相位斜率分析：

| 指标 | 值 |
|------|-----|
| STO 均值 | -13.71 samples |
| STO 标准差 | 1.84 samples |
| 跨天线一致性 | < 1 sample 差异 |
| per-antenna 常相位 phi0 | 10~51 rad（巨大且不一致） |

**根因**：OAI FFT 窗口比 Proxy GT 晚约 13.7 个采样点。加上每天线独立的相位偏移，全局 LS 无法对齐。

### 层级 4：验证修正效果

应用 STO=-13.7 修正 + per-antenna LS 对齐后：

| 配对精度 | NMSE global LS | NMSE per-antenna LS | 最佳天线对 |
|----------|----------------|---------------------|------------|
| tol=0 (8帧) | +9.67 dB | **+0.39 dB** | rx0_tx1: **-0.48 dB** |
| tol=4 (92帧) | +12.66 dB | +3.32 dB | rx1_tx1: +2.87 dB |

**结论：Adaptive 算法本身正常工作。** 精确配对 + 正确对齐口径下 NMSE 接近 0 dB，p10 到 -5.3 dB，在 SNR=10 dB 下合理。

## 9.3 三层问题总结

| 问题 | 影响 (dB) | 修复方式 |
|------|-----------|----------|
| STO = -13.7 samples | ~17 dB | `--sto-fixed-samples 13.7` |
| per-antenna 相位不一致 | ~9 dB | per-(rx,tx) LS 对齐 |
| slot 配对误差 (tol>0) | ~3 dB | `--tol 0` 或 `--tol 1` |

## 9.4 eval_pdpR_nmse.py 改进

新增两项功能：

1. **`--sto-fixed-samples N`**：应用已知的固定 STO 补偿（无需从数据重新估计）
2. **NMSE LS-per-antenna 指标**：自动输出 per-(rx,tx) 独立 LS 对齐后的 NMSE + 逐天线分解

用法示例：

```bash
python3 eval_pdpR_nmse.py \
  --run-dir <snr_XdB> \
  --sto-fixed-samples 13.7 \
  --tol 0
```

输出新增行：

```
NMSE LS-per-antenna  :   +0.39 dB   per-frame [p10,p50,p90]=[-5.34,-0.44,+8.83]

Per-antenna breakdown (per-antenna LS):
  rx0_tx0: NMSE=  +0.92 dB  |alpha|=138.91
  rx0_tx1: NMSE=  -0.48 dB  |alpha|=151.05
  rx1_tx0: NMSE=  +0.33 dB  |alpha|=123.79
  rx1_tx1: NMSE=  +0.88 dB  |alpha|=136.15
```

---

# 第十部分：关键发现与教训

## L1：GT 多不等于可评估样本多

GT 的作用是"给 SRS 找参考"。当 GT >> SRS 时，`N_eval ≈ SRS 帧数`。
GT 再多也不能补偿缺失的 SRS 观测。

## L2：文件存在 ≠ 数据充足

OAI 的 SRS dump 使用 ping-pong 缓冲（`MAX_DUMP_FRAMES=100`）。
如果在凑满 100 帧前被 SIGTERM，会 partial flush（7 帧写到文件里）。
旧门限只检查"文件数≥1"，无法区分 100 帧文件和 7 帧文件。

## L3：低 SNR 下链路控制面不稳定

5 dB SNR 场景下，gNB 日志出现大量：
- `Invalid timing advance offset` → TA 闭环异常
- `RRCReestablishment` → UE 连接重建
- `[SRS 2D] all states reset` → 滤波器状态清零
- `Scheduling retransmission of Msg3` → RA 过程重传

这意味着 SRS 调度机会在低 SNR 时会被 MAC/RRC 事件"吃掉"，导致 SRS 帧数远少于预期。

## L4：口径一致性是对比的前提

| 口径 | 工具 | GT 来源 | 适用场景 |
|------|------|---------|----------|
| 离线 split-set | `prepare_2d_mmse_data.py` + `test_2d_mmse.py` | 帧平均 | legacy 采集 + Python 滤波验证 |
| 在线直接配对 | `eval_pdpR_nmse.py` | per-frame GT | C 侧在线输出评估 |

两套口径的 NMSE 数值**不能直接比较**。

## L5：采集与评估必须解耦

混在一个脚本里 → 评估代码会"自动"对数据再加工 → 口径失真。
拆开后：采集脚本只管采集、manifest 只记录物理量（帧数/文件数/状态），评估完全由用户选择工具。

## L6：全局 LS 对齐对 MIMO 数据不充分

SRS 和 GT 之间存在 per-antenna 的独立相位偏移（OAI SRS estimator 和 Sionna proxy 的 per-antenna phase reference 不同）。单一全局 alpha 无法消除这种失配，导致 NMSE 虚高 ~9 dB。正确口径需要 per-(rx,tx) 独立 LS 对齐。

## L7：STO 是系统性的，应固定补偿

OAI 的 FFT 窗口与 Sionna proxy 存在固定的定时偏移（约 -13.7 samples）。这个偏移跨帧、跨天线一致，不应每帧重新估计（容易被噪声带偏），而应作为系统参数固定补偿。

## L8：RTX 5090 (Blackwell) TF 兼容性注意

TF 官方至今未发布带 sm_120 原生 kernel 的版本。TF 2.21 通过 PTX JIT 可以工作，但需要：CUDA toolkit 12.8+、cuDNN 9（pip 安装）、Sionna 1.2.2。容器内 CUDA 升级后需更新软链接并清除 CuPy kernel 缓存。

---

# 第十一部分：产出文件完整清单

## A. 算法验证脚本（本轮新增）

| 文件 | 说明 |
|------|------|
| `G1C/step0_refit_rot_rate.py` | Phase 2 Step 0: rot_rate→α 线性映射重拟合 |
| `G1C/step_b_capture_gate.py` | Phase 2 Step B: capture gate 测试 |
| `G1C/step_c_generalization_gate.py` | Phase 2 Step C: SNR/CDL 泛化 gate |
| `G1C/cdl_c_single_retest.py` | RNG 修复后 CDL-C 单模型复测 (8/8 PASS) |
| `G1C/cdl_combo_search.py` | CDL-A~E 31子集组合搜索，最优=C+E |
| `G1C/srs_adaptive_alpha.c` | C 蓝图（OAI 集成前的参考实现） |
| `G1C/multisc_cdl_sim.py` | 多子载波 CDL 仿真工具（oracle sweep 等） |

## B. OAI C 侧集成（核心落地）

| 文件 | 改动说明 |
|------|----------|
| `nr_srs_2d_filter.c` | 新增 adaptive 状态/核心函数/env 参数/回退 |
| `nr_srs_2d_filter.h` | 新增 enum/注释/env 变量文档 |

## C. 运行流程脚本

| 文件 | 说明 | 本轮改动 |
|------|------|----------|
| `G1C/run_q4_snr_ab_compare_v8.sh` | A/B 对比顶层控制器 | **新增** → 后改为 capture-only |
| `G1C/run_q4_snr_sweep_v8.sh` | SNR sweep 采集器 | 移除内嵌后处理 + 新增 `MIN_SRS_FRAMES` |
| `G1C/launch_all_v8.sh` | 单点启动脚本 | 同步 `MIN_SRS_FRAMES` + `sum_srs_frames()` |
| `G1C/run_q4_postprocess_v8.sh` | 独立后处理 | **新增**，支持 `--background` |

## D. 数据处理与评估链路（已有，本轮使用/分析）

| 文件 | 说明 |
|------|------|
| `G1C/prepare_2d_mmse_data.py` | sweep→STO补偿→GT对齐→标准NPZ |
| `G1C/test_2d_mmse.py` | split-set 离线评估（EMA/Box/Oracle曲线） |
| `G1C/eval_pdpR_nmse.py` | 在线直接配对 NMSE 评估（本轮主用） |
| `G1C/digital_twin_stats.py` | SRS/GT 数据加载与对齐 |
| `G1C/srs_sto_compensate.py` | STO 补偿工具 |
| `G1C/srs_2d_mmse.py` | Python 滤波器模块 (EMA/AdaptiveAlpha) |

## E. 过程日志与文档

| 文件 | 说明 |
|------|------|
| `log_26_05_17_adaptive_alpha_cdl.md` | 05-17 当天详细工作日志 |
| `log_26_05_17_19_adaptive_alpha_oai_ab.md` | **本文件** — 05-17~20 完整总结 |
| `26_05 中旬日志整合.md` | 05-09~15 前序工作（floor定位+2D MMSE验证） |

## F. 实验产物（logs 目录）

| 路径 | 说明 |
|------|------|
| `logs/q4_ab_compare_20260519_134742/ewma/` | EWMA sweep (dclcom57, SNR 0/5/10/20) |
| `logs/q4_ab_compare_20260519_150510/adaptive/` | Adaptive sweep (dclcom57, SNR 0/5/10/20) |
| `logs/q4_ab_compare_20260520_133524/` | A/B compare (dclcom61, ewma PARTIAL + adaptive OK) |
| `logs/q4_ab_compare_20260520_143048/adaptive/` | Adaptive 单独跑 (dclcom61, OK, 用于口径诊断) |
| 各 `snr_*dB/` 下 | `gnb.log`, `proxy.log`, `srs_matrix_*.bin`, `sionna_gt/`, `H_sto_compensated.npz` |
| 各 `sweep_manifest.txt` | 每点 status/n_gt/n_srs |
| 各 `ab_compare_manifest.txt` | method 级汇总 |

---

# 第十二部分：当前状态与下一步

## 当前状态

| 项目 | 状态 |
|------|------|
| Adaptive α Python 算法 | ✅ CDL-C+E 标定通过，8/8 gate PASS |
| OAI C 侧集成 | ✅ 编译通过，env 切换可用 |
| A/B 采集框架 | ✅ 解耦完成，`MIN_SRS_FRAMES` 门限生效 |
| A/B 首轮数据 (05-19) | ⚠️ 帧数不对等，结论不可用 |
| 离线/在线口径 | ✅ 已明确区分，不再混用 |
| 服务器迁移 (dclcom61) | ✅ 全链路可运行（TF 2.21 + Sionna 1.2.2 + CUDA 12.8） |
| 评估口径诊断 | ✅ 三层问题定位完毕（STO + per-antenna phase + slot tol） |
| eval_pdpR_nmse.py 增强 | ✅ 新增 per-antenna LS + `--sto-fixed-samples` |
| Adaptive NMSE 验证 | ✅ per-antenna LS 下 +0.39 dB (tol=0)，确认算法正常 |

## 已知风险

1. **低 SNR 链路稳定性**：5 dB 下 SRS 采集受 RRC/MAC 事件干扰严重，即使加了帧数门限也可能需要很长等待时间。
2. **UE 接入随机失败**：`hw_slot_offset` 随机落到 UL slot 导致 SIB1 持续 NACK。需加 watchdog 自动重启 UE。
3. **STO 值可能因服务器/配置变化**：-13.7 samples 是在 dclcom61 上实测的系统偏移，换服务器/配置需重新标定。
4. **TF Blackwell 性能**：PTX JIT 性能可接受但非原生优化。未来如需极致性能可考虑 PyTorch (Sionna 2.0) 迁移。

## 下一步

| 优先级 | 任务 | 说明 |
|--------|------|------|
| **P0** | 重跑 A/B（ewma+adaptive 同轮） | `METHODS="ewma adaptive"` + UE attach watchdog，确保两轮都成功 |
| **P1** | 统一口径公平对比 | 用 `eval_pdpR_nmse.py --sto-fixed-samples 13.7 --tol 0` 对比 ewma vs adaptive |
| P2 | 多 SNR 点 sweep | SNR=5/10/20/30，观察 per-antenna NMSE vs SNR 趋势 |
| P3 | UE attach watchdog | 在 `launch_all_v8.sh` 加 SIB1 NACK 监控，超阈值自动重启 UE |
| P4 | STO 标定自动化 | 将 STO 估计集成到 postprocess pipeline，每次 sweep 自动输出系统 STO 值 |

---

# 附录：关键命令速查

## 采集（capture-only）

```bash
cd ~/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy

# A/B 对比采集
sudo SNR_POINTS="5 10 20" MAX_FRAMES=100 MIN_SRS_FRAMES=100 \
     METHODS="ewma adaptive" \
     bash run_q4_snr_ab_compare_v8.sh

# 单 method 采集
sudo SNR_POINTS="10 20" MAX_FRAMES=100 SRS_2D_METHOD=adaptive \
     SRS_ESTIMATOR=2d-mmse \
     bash run_q4_snr_sweep_v8.sh
```

## 后处理（独立）

```bash
# 前台
bash run_q4_postprocess_v8.sh --sweep-dir <path> --speed 0 --seed 42

# 后台
bash run_q4_postprocess_v8.sh --sweep-dir <path> --speed 0 --seed 42 --background
```

## 在线直接评估

```bash
# 逐点评估（推荐口径：固定 STO + per-antenna LS + 精确配对）
python3 eval_pdpR_nmse.py --run-dir <snr_XdB> \
  --sto-fixed-samples 13.7 --tol 0

# 旧口径（per-frame STO 估计，仅供参考）
python3 eval_pdpR_nmse.py --run-dir <snr_XdB> --sto-correct per-frame --dump-sto

# 批量对比
for snr in 5 10 20; do
  echo "=== EWMA $snr ==="
  python3 eval_pdpR_nmse.py --run-dir <ewma_root>/snr_${snr}dB \
    --sto-fixed-samples 13.7 --tol 0
  echo "=== Adaptive $snr ==="
  python3 eval_pdpR_nmse.py --run-dir <adapt_root>/snr_${snr}dB \
    --sto-fixed-samples 13.7 --tol 0
done
```

## 离线 baseline 复现

```bash
# 采集 (legacy)
sudo UE_SPEED=0 CHANNEL_SEED=42 SRS_ESTIMATOR=legacy GT_SAVE_EVERY=10 \
     SNR_POINTS="10 20 30 40" MAX_FRAMES=300 ATTACH_STABLE_SEC=300 \
     bash run_q4_snr_sweep_v8.sh

# 后处理
python3 prepare_2d_mmse_data.py --sweep-dir <path> --speed 0 --seed 42
python3 test_2d_mmse.py --speed 0
```
