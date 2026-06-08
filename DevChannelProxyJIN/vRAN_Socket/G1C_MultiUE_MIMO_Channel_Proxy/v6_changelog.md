# v6.py Changelog — G1C Multi-UE MIMO Channel Proxy

> 基于 v4.py，修复 45 条问题清单中的 ~24 条 + 新增物理改进  
> 日期：2026-04-30  
> 验证脚本：`verify_issue_2.py`（5-phase 物理一致性测试）

---

## 0. 背景：v4.py 中发现的两个致命 bug

### Bug #1: `carrier_frequency = 3.5`（应为 `3.5e9` Hz）

- **位置**：v4.py 第 194 行
- **影响**：Sionna `PanelArray` / `ChannelCoefficientsGeneratorJIN` 期望 Hz 单位。`3.5` 导致 `λ₀ = c/f = 8.57×10⁷ m`（应为 0.0857 m），Doppler 频率趋近 0，天线间距计算差 9 个数量级
- **验证**：`verify_issue_2.py` Phase B — `carrier_freq=3.5` 时递增 sample_times 后 `max_diff=0.0`（Doppler 完全消失）

### Bug #2: `sample_times_fixed` 不随 batch 递增

- **位置**：v4.py 第 1817-1819 行
- **影响**：`sample_times_fixed = range(N)/scs` 在循环外创建后不再变化。`_H_TTI_sequential_fft_o_ELW2_noProfile` 是纯确定性函数（内部无 `tf.random`），相同输入 → 相同输出。Producer 无限循环生成**完全相同的信道 realization**
- **验证**：`verify_issue_2.py` Phase A — 两次调用 `identical=True`

### 两个 bug 的乘法效应

这两个 bug **互为掩盖**：
- 只修 #2（递增 sample_times）但保留 `carrier_freq=3.5` → Doppler ≈ 0，信道仍然静态
- 只修 #1（改成 3.5e9）但保留固定 sample_times → 输出仍然重复

**必须同时修复**。

### 跨 batch 边界连续性（Phase C 验证）

- `_H_TTI_...` 内部对 `sample_times` 使用**绝对时间**语义：`exp(j·2π/λ·v·r̂·t)`
- 连续 sample_times 的 batch 边界跳变 = `2.6×10⁻⁷`（浮点精度级）
- 重置 sample_times 的 batch 边界跳变 = `1.6×10⁻⁵`（61.6 倍差距）
- **结论**：只需累积递增 sample_times，无需额外相位连续性维护

---

## 1. 物理正确性修复（Physics Correctness）

### 1.1 载频单位（v4 问题清单 #1）

```
v4:  carrier_frequency = 3.5
v6:  carrier_frequency = 3.5e9
```

### 1.2 sample_times 跨 batch 累积（v4 问题清单 #2）

```python
# v4: 固定（每 batch 完全相同）
sample_times_fixed = tf.range(N) / scs

# v6: 用 int64 基底避免长时间运行后浮点精度丢失
def _build_sample_times(b_idx):
    start = tf.constant(b_idx * buffer_symbol_size, dtype=tf.int64)
    idx_i64 = start + tf.range(buffer_symbol_size, dtype=tf.int64)
    return tf.cast(idx_i64, gen.rdtype) / scs
```

相比简单的 `batch_idx * batch_duration` 浮点累加，int64 基底在 batch_idx > 10⁶ 时仍保持完整精度。

### 1.3 velocities 物理建模

```python
# v4: 各分量独立正态，|v| = sqrt(Vx²+Vy²+Vz²) ≈ sqrt(3)·Speed
velocities = tf.abs(tf.random.normal([B, N_UE, 3], mean=Speed, stddev=0.1))

# v6: 水平面随机方向 + 标量速度，|v| ≈ Speed，Vz=0
phi = tf.random.uniform([B, N_UE], 0, 2π)
speed_mag = tf.abs(tf.random.normal([B, N_UE], mean=Speed, stddev=0.1))
velocities = [speed_mag·cos(phi), speed_mag·sin(phi), 0]
```

### 1.4 LOS path angles

```python
# v4: 全零
los_aoa = tf.zeros(...)  # 所有 BS-UE 对的 LOS 方向相同

# v6: 随机采样，zenith 限制在 [π/3, 2π/3] 内
los_aoa = tf.random.uniform(..., 0, 2π)
los_zoa = tf.random.uniform(..., π/3, 2π/3)
```

### 1.5 distance_3d 从 P1B tau 推导

```python
# v4: 硬编码 1m
distance_3d = tf.ones([1, N_BS, N_UE])

# v6: 由最短径的传播延迟推算
tau_first = tf.reduce_min(tau_rays, axis=-1)
distance_3d = max(tau_first * SPEED_OF_LIGHT, 1.0)  # floor at 1m
```

### 1.6 能量归一化：全局固定基准

```python
# v4: per-batch 归一化（抹掉 Rayleigh fading 功率波动）
energy = tf.reduce_sum(|h|², axis=3, keepdims=True)  # 每 batch 独立
h_norm = h / sqrt(energy)

# v6: batch 0 计算全局基准，所有后续 batch 共享
_h0 = generate_unnorm(sample_times_batch0)
_ref_norm = sqrt(mean(|_h0|²))
h_norm = h / _ref_norm  # 全局 scalar，保留 Doppler 功率波动
```

### 1.7 物理参数守护（新增）

```python
def _validate_physics_params(cf, sc, sp):
    if not 1e8 < cf < 1e12:
        raise ValueError(f"carrier_frequency={cf} Hz — expected 100 MHz–1 THz")
    if sc not in (15e3, 30e3, 60e3, 120e3, 240e3):
        raise ValueError(f"scs={sc} Hz — not standard NR numerology")
    ...
```

在 CLI 参数解析**之后**调用，确保用户 `--speed` 等覆盖值也被检查。使用 `ValueError`（非 `assert`），`python -O` 不会跳过。

---

## 2. 鲁棒性修复（Robustness）

### 2.1 batch_idx 立即递增（v4 问题清单 #2 附带）

```python
# v4: batch_idx 在循环末尾递增 → 下游异常时重试会重复 batch
symbol_counter += buffer_symbol_size

# v6: generate_fn 成功后立即递增，downstream 异常不会重复
h = generate_fn(sample_times_now)
batch_idx += 1  # 这里立即递增
# ... downstream processing ...
```

### 2.2 Producer 死亡检测（v4 问题清单 #4）

```python
# v4: warmup 阶段打印警告后继续，主循环不检查
if not proc.is_alive():
    print("WARN ...")  # 然后继续执行

# v6: 主循环每 1000 次迭代检查，死亡后干净退出
if _iter_count % 1000 == 0 and not proc.is_alive():
    print(f"ERROR: exitcode={proc.exitcode}")
    break
```

### 2.3 连续失败限制（v4 问题清单 #5）

```python
# v4: 无限重试（每秒打一次 traceback 直到天荒地老）
except Exception:
    traceback.print_exc()
    time.sleep(1.0)

# v6: 20 次连续失败后退出
consecutive_errors += 1
if consecutive_errors >= 20:
    break
# 成功时重置 consecutive_errors = 0
```

### 2.4 Socket mode 拒绝 multi-UE（v4 问题清单 #6）

```python
# v4: 静默广播同一 processed 给所有 UE，结果错误
# v6: 直接拒绝
if num_ues > 1:
    raise ValueError("Socket mode does not support num_ues>1")
```

### 2.5 _stalled_ue_logged 可恢复（v4 问题清单 #15）

```python
# v4: UE 标记 stalled 后永不清除
self._stalled_ue_logged.add(sk)

# v6: UE 恢复活跃时移除标记，允许再次检测
_recovered = self._stalled_ue_logged & active_set
if _recovered:
    print(f"UE recovered from stall")
    self._stalled_ue_logged -= _recovered
```

### 2.6 天线数一致性校验（v4 问题清单 #9）

```python
# v6: main 入口处校验
if args.gnb_ant != args.gnb_nx * args.gnb_ny:
    raise ValueError(...)
```

### 2.7 `random.sample` 边界检查（v4 问题清单 #39）

```python
# v4: len(all_indices) < num_ues 时 ValueError（无有用信息）
# v6: 提前检查并给出有意义的错误消息
if num_ues > len(all_indices):
    raise ValueError(f"--num-ues={num_ues} exceeds available RX indices ...")
```

---

## 3. 性能 / 内存修复（Performance & Memory）

### 3.1 skip_quant（v4 问题清单 #14）

```python
# v4: UL superposition 中每个 UE 的 process_slot_ipc 都做完整 clip+cast→int16
#     结果写到 _ul_dummy_out（立即被丢弃），只用 gpu_out（complex128）累加

# v6: _gpu_compute_core(skip_quant=True) 跳过最后的量化步骤
if not skip_quant:
    # clip + cast + write to gpu_iq_out
    ...
# UL path: process_slot_ipc(..., skip_quant=True)
```

UL superposition 中 CUDA Graph 也对应跳过（`_use_graph_this_call = not skip_quant`）。

### 3.2 NoiseProducer 降精度 + 缩 buffer（v4 问题清单 #16）

```
v4: dtype=float64, maxlen=256, BATCH_SIZE=64
    → 单 buffer ≈ 480 MB (gnb_ant=4)
    → 4 UE × 2 (DL+UL) ≈ 4 GB

v6: dtype=float32, maxlen=64, BATCH_SIZE=32
    → 单 buffer ≈ 15 MB
    → 4 UE × 2 ≈ 120 MB  (~16x VRAM 节省)
```

Consumer 的 `_regenerate_noise` 在赋值时隐式 cast 到 float64，物理精度不变。

### 3.3 GTBatchSaver 异步写盘（v4 问题清单 #18）

```python
# v4: np.savez_compressed 同步阻塞主循环（数十~数百 ms）

# v6: 后台 daemon thread + Queue(maxsize=16)
#     queue 满时 fallback 到同步写（backpressure, 不丢数据）
#     flush_all() 先 join() 确保队列排空
```

### 3.4 bypass_copy 预分配（v4 问题清单 #7）

```python
# v4: 每次调用 cp.zeros((nsamps, dst_nbAnt*2), dtype=cp.int16)

# v6: lazy 分配 + key 缓存，相同 (nsamps, dst_nbAnt) 复用
#     额外修复：非对称天线时 dst_2d[:, min_ant*2:] = 0（v4 此处未初始化）
```

### 3.5 UL set_last_ul_rx_ts 合并（v4 问题清单 #17）

```python
# v4 (bypass mode): 每个 UE 各调用一次 futex_wake
for k in range(N):
    self.ipc_gnb.set_last_ul_rx_ts(ue_head - 1)  # N 次 wake

# v6: 收集最大 head，循环外只 wake 一次
if _any_bypass:
    self.ipc_gnb.set_last_ul_rx_ts(int(_max_new_head - 1))
```

### 3.6 IPCRingBuffer sync 移出锁外（v4 问题清单 #22）

```python
# v4: 在 with s.not_full 锁内做 Device.synchronize()（阻塞所有等锁进程）

# v6: 锁内只做 index 更新 + notify，sync 移到锁外
#     sync 后 re-notify 防止 consumer 竞态
with s.not_full:
    # write + index update + notify
s.not_empty.notify_all()
cp.cuda.Device(0).synchronize()    # 锁外
with s.not_empty:
    s.not_empty.notify_all()        # re-notify
```

---

## 4. CUDA Graph 改进

### 4.1 Relaxed capture mode（v4 问题清单 #3）

```python
# v4: self.stream.begin_capture()  — Global mode，其他 stream 冲突

# v6: Relaxed mode + fallback
try:
    mode = cp.cuda.runtime.streamCaptureModeRelaxed
except AttributeError:
    mode = 2  # CUDA runtime magic number
stream.begin_capture(mode=mode)
```

### 4.2 可恢复的 capture 失败（v4 问题清单 #19）

```python
# v4: capture 失败 → self.use_cuda_graph = False（永久禁用）

# v6: 最多重试 5 次，成功后重置计数器
self._capture_failure_count += 1
# 不设 use_cuda_graph = False，下一 slot 可以再试
```

---

## 5. 杂项修复

### 5.1 PEP 604 语法兼容性（v4 问题清单 #10）

```python
# v4: hdr_vals: Tuple[...]|None = None  — Python 3.10+ only
# v6: hdr_vals: Optional[Tuple[...]] = None  — Python 3.8+ 兼容
```

### 5.2 CPU mode noise_dBFS（v4 问题清单 #34）

```python
# v4: GPU_AVAILABLE=False → noise_std_abs=None（绝对噪声模式静默降级为无噪声）
# v6: noise_std_abs = float(rms / sqrt(2))（CPU 模式用 Python float）
```

### 5.3 移除死代码 _ul_clip_3d（v4 问题清单 #27）

```python
# v4: self._ul_clip_3d = cp.zeros(...)  — 分配后从未使用
# v6: 已删除
```

### 5.4 pl_linear 去掉 Python 分支（v4 问题清单 #20）

```python
# v4: if pl_linear != 1.0: self.gpu_out *= pl_linear  — graph 中 Python 分支
# v6: self.gpu_out *= cp.float64(pl_linear)  — 无条件乘（1.0 时开销忽略不计）
```

---

## 6. 验证结果（verify_issue_2.py 5-Phase）

| Phase | 测试 | 结果 |
|-------|------|------|
| A | 固定 sample_times 两次调用 | `identical=True` → Issue #2 确认 |
| B (3.5) | 递增 sample_times, carrier=3.5 | `identical=True` → Issue #1 确认（Doppler=0） |
| B (3.5e9) | 递增 sample_times, carrier=3.5e9 | `relative Δ=93.5%` → 信道正确演变 |
| C | 跨 batch 边界连续性 | 连续=2.6e-7 vs 重置=1.6e-5（61.6x）→ 绝对时间语义 |
| D | Speed=0 sanity check | `identical=True` → Doppler 是唯一时变源 |

---

## 7. 未修复项（留待后续版本）

| 编号 | 问题 | 原因 |
|------|------|------|
| #11 | IPCRingBuffer wrap-around view vs copy | 仅读取场景无影响，风险低 |
| #12 | 无反压机制 | 需要架构层面设计变更 |
| #13 | partial slot bypass 不施加信道 | 边界情况，频率低 |
| #21 | load_p1b_per_ue 每次读完整 npz | 启动时一次性开销，可接受 |
| #23 | tf.experimental.dlpack 已废弃 | 等待 TF 升级后统一迁移 |
| #24 | fft_lib 参数未使用 | dead arg，移除需协调 CLI |
| #25 | handle_queue cleanup | 需要重构 IPC 初始化流程 |
| #26 | 120s XLA 超时 | 观察实际编译时间后再调整 |
