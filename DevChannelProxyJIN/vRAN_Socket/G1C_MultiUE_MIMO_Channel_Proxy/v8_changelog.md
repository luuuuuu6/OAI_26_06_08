# v8.py Changelog — Async GT D2H Pipeline

> 基于 v7.py，针对 v7 在 `--gt-save-every=1` 时主线程阻塞导致 attach 失败的问题，
> 把 GT 保存的 GPU→CPU 拷贝异步化。
>
> 相关文件：`v8.py`、`launch_all_v8.sh`、`run_q4_snr_sweep_v8.sh`、`v8.use.md`（待写）
>
> A/B 回退：所有 v7 文件全部保留不动；v8 默认走 async，但可以 `--gt-async-copy=off`
> 退回 v7 行为做对照。

---

## 0. 背景：v7 在 `save_every=1` 下的失败模式

| | save_every=10（v7 默认） | save_every=1（诊断需要） |
|---|---|---|
| GPU→CPU 同步 `.get()` 频率 | 每 10 个 UL slot 一次 | **每个 UL slot** 一次 |
| 单次 `.get()` 阻塞 | ~5 ms（强制 default stream sync） | 同左 |
| UL per-slot wall（实测） | ~0.5 ms | **~7 ms** |
| Real-time 比 | ~5× | **0.07×** |
| 后果 | 正常 | **UL pipeline 拖死 → IPC ringbuffer 堆积 → RA/RRC 消息超时 → SRS=0** |

实测数据（`logs/20260507_180834_..._snr-5/proxy.log`）：

```
[E2E frame#23770 1UE (6D+7U)] wall=65.10ms  Proxy(DL=16.0+UL=49.0)=64.99ms  | per slot=5.01
                              ↑↑↑ DL 16ms，UL 49ms — UL 比 DL 慢 3 倍
attach_stable_watcher: still waiting for 'Received RRCReconfigurationComplete'
[sweep] SNR=-5 dB  status=ABORTED  gt=124  srs=0  dur=134s
                                          ↑↑↑ SRS=0
```

根因定位：`v7.py:866-907 record_ul_slot()` 第 900 行 `ch_ul[sym_idx].get()`
在主线程同步等待整个 cuda default stream 完成。`save_every=1` 让这个开销 ×10。

---

## 1. v8 一句话

> v7 把 **磁盘 I/O** 异步到 background thread，但 **GPU→CPU 拷贝** 仍同步阻塞主线程。
> v8 把 **GPU→CPU 拷贝** 也异步到 dedicated cuda stream + 第二条 background thread。

---

## 2. 设计要点

### 2.1 三阶段 API（必须按顺序调用）

```python
# 在 _ipc_ul_superposition_slot 中，每个 UL slot：

decision, slot_id = self.gt_saver.try_begin_slot(ipc_ts=ts)
# ↑ slot 级决策，所有 UE 共用，避免 per-UE 不一致

gt_tokens = []
for k in ues:
    ...
    self.pipelines_ul[k].process_slot_ipc(...)

    if decision:
        # ★ P0 修复：stage 在 release 之前
        gt_tokens.append(self.gt_saver.stage_for_ue(k, channels_ul))
        # stage_for_ue 内部:
        #   - GPU→GPU 拷贝到 self._gpu_staging_pool 中的 buffer (default stream)
        #   - event.record(); event.synchronize()  ← 仅 ~10–30 μs

    self.channel_buffers[k].release_batch(n_held)   # 现在安全：staging 已完成

if decision and gt_tokens:
    self.gt_saver.commit_slot(gt_tokens, slot_id, partial_bypass=...)
    # commit_slot 内部:
    #   - copy_stream.wait_event(stage_event)  ← 防御性同步
    #   - cp.cuda.runtime.memcpyAsync(staging_gpu → pinned_host) on copy_stream
    #   - copy_event.record(copy_stream)
    #   - _copy_q.put_nowait(PendingD2H)
    #   - 立刻返回，不等
```

### 2.2 双 background 线程

```
主线程                   _copy_drain_loop          _writer_loop
───────                ──────────────────         ────────────
record (try_begin)
  │
  ├─ for k:
  │    process_slot_ipc       (GPU)
  │    stage_for_ue(k):       GPU→GPU + event.sync (~30μs)
  │    release_batch(n_held)
  │
  └─ commit_slot(tokens):     copy_stream D2H + put _copy_q
      └→ 立刻返回                ──┐
                                    ↓
                              copy_event.synchronize()
                              np.copy(pinned_host) → h_full
                              return staging_gpu / pinned_host → pool
                              _enqueue_to_writer_buffer(...)
                              满 BATCH_SIZE → put _write_q  ──┐
                                                                ↓
                                                          np.savez_compressed
                                                          → disk
```

### 2.3 资源池

| Pool | 默认大小 | 单位大小（默认 shape） | 总占用 |
|---|---|---|---|
| `_pinned_pool` (pinned host) | 32 | (1, 2, 2, 2048) complex64 = 32 KB | ~1 MB host RAM |
| `_gpu_staging_pool` (GPU) | 32 | 同上 | ~1 MB VRAM |
| `_copy_q` (drain queue) | 36 | dataclass | < 10 KB |
| `_write_q` (writer queue) | 16 | (fname, payload) | 取决于 batch |

通过 `--gt-pinned-pool-size` / `--gt-staging-gpu-buffers` 调整。

### 2.4 反压策略

| 资源耗尽 | v8 行为 |
|---|---|
| `_pinned_pool` 或 `_gpu_staging_pool` 空 | `stage_for_ue` 退化为同步 `.get()`，记 `_async_overflow_count++` |
| `_copy_q` 满 | `commit_slot` 同步等 event 完成，直接 push 到 writer buffer |
| `_write_q` 满 | `_enqueue_writer_payload` 同步 `np.savez_compressed`（v7 行为） |

**永远不丢数据**，只会回退到同步路径并打 warning。

---

## 3. 与 v7 关键差异（按 P0/P1 风险层级）

### 3.1 P0：staging 必须在 release_batch 之前

| | v7 | v8 |
|---|---|---|
| `process_slot_ipc(...)` | ✓ | ✓ |
| `release_batch(n_held)` | **先 release** | 后 release |
| `record_ul_slot(...)` 含 `.get()` | **后 record** | — |
| `stage_for_ue(...)` 含 GPU→GPU + event.sync | — | **先 stage** |
| `commit_slot(...)` | — | 后 commit |

v7 顺序在 ChannelProducer（独立 `_mp.Process`）覆写 `channels` 时存在数据竞争 —
不是同 cuda context、不在同 default stream 队列里。v8 修复。

### 3.2 P1：`_buffers / _seq` 加锁

`_writer_loop`、主线程 sync fallback、`_copy_drain_loop` 三方都可能修改 `_buffers[ue_idx]`。
v8 加 `self._buffer_locks = {k: threading.Lock() for k ...}`，所有 mutation 持锁。

`_build_payload_and_swap_locked()` 要求调用方持锁；`_enqueue_writer_payload()` 锁外调用
（`np.savez_compressed` 不应该在锁内做）。

### 3.3 P1：异常路径必须归还资源

`stage_for_ue` 拿到 staging_gpu / pinned_host 后，任何抛异常的路径都必须 `_return_*_pool`，
否则 pool 会泄漏。v8 在所有 try/except 里加了显式归还。

---

## 4. 性能预期

| 指标 | v7 (save_every=1) | v8 (save_every=1) | 变化 |
|---|---|---|---|
| GPU→CPU 主线程阻塞 | ~5 ms（stream sync） | ~30 μs（GPU→GPU + event.sync） | **~150× 改进** |
| UL per-slot wall（实测预期） | ~7 ms | < 1 ms | ~10× 改进 |
| Real-time | 0.07× | 期望 > 1× | — |
| attach 完成时间 | 卡死 | 期望几秒 | — |
| 输出 npz 字节内容 | — | **完全等同 v7** | — |

---

## 5. CLI / env vars

### 5.1 v8.py 新增 CLI

| 参数 | 默认 | 含义 |
|---|---|---|
| `--gt-async-copy {on,off}` | `on` | 关掉则等同 v7 行为，便于 A/B |
| `--gt-pinned-pool-size N` | `32` | pinned host pool 容量 |
| `--gt-staging-gpu-buffers N` | `32` | GPU staging pool 容量 |

### 5.2 launch_all_v8.sh 新增 env

```bash
GT_ASYNC_COPY="on"            # on|off
GT_PINNED_POOL_SIZE=32
GT_STAGING_GPU_BUFFERS=32
```

可在调用时覆写：

```bash
GT_ASYNC_COPY=off sudo bash launch_all_v8.sh   # v7 行为对照
```

### 5.3 sweep / A·B 默认指向 v8，可覆写

```bash
# 默认 (v8):
sudo bash run_q4_snr_sweep_v8.sh
bash run_fair_ab_minimal.sh
bash run_mmse_sigma2_abcd.sh

# 强制走 v7（回归对比）:
SWEEP_SCRIPT=$(pwd)/run_q4_snr_sweep_v7.sh PROXY_VER=v7 bash run_fair_ab_minimal.sh
```

---

## 6. 文件清单（本次升级）

| 新增 / 修改 | 文件 | 说明 |
|---|---|---|
| 新增 | `v8.py` | 复制 v7.py 后改造 GTBatchSaver / `_ipc_ul_superposition_slot` / CLI |
| 新增 | `launch_all_v8.sh` | 默认 PROXY_VER=v8；新增 `GT_ASYNC_COPY` / `GT_PINNED_POOL_SIZE` / `GT_STAGING_GPU_BUFFERS` |
| 新增 | `run_q4_snr_sweep_v8.sh` | 默认 PROXY_VER=v8；调 `launch_all_v8.sh` |
| 新增 | `v8_changelog.md` | 本文件 |
| 修改 | `run_fair_ab_minimal.sh` | `SWEEP_SCRIPT` 默认指向 v8（env 可覆写） |
| 修改 | `run_mmse_sigma2_abcd.sh` | 同上 |
| 不动 | `v7.py` / `launch_all_v7.sh` / `run_q4_snr_sweep_v7.sh` | A/B 回退保险 |

---

## 7. 验证计划（V1 / V2 / V3）

### V1 — 一致性测试

跑同一份配置，分别用 v8 (async on) 和 v8 (async off)，对比输出 npz：

```bash
sudo GT_ASYNC_COPY=on  ONLY_SNR=10 MAX_FRAMES=30 bash run_q4_snr_sweep_v8.sh   # → run_async/
sudo GT_ASYNC_COPY=off ONLY_SNR=10 MAX_FRAMES=30 bash run_q4_snr_sweep_v8.sh   # → run_sync/

python3 -c "
import numpy as np, glob
for sync_f, async_f in zip(sorted(glob.glob('run_sync/.../sionna_gt/gt_*.npz')),
                            sorted(glob.glob('run_async/.../sionna_gt/gt_*.npz'))):
    a = np.load(async_f); s = np.load(sync_f)
    assert np.allclose(a['h_matrix'], s['h_matrix']), f'mismatch: {async_f}'
    print(f'{async_f}: OK')
"
```

预期：完全一致。

### V2 — 性能基准

跑 v8 with `save_every=1`，从 proxy.log 提取 UL per-slot wall：

```bash
sudo bash run_q4_snr_sweep_v8.sh   # 默认 GT_ASYNC_COPY=on, GT_SAVE_EVERY=1
grep "per slot" logs/latest/proxy.log | tail -50
```

预期：UL per-slot < 1 ms（v7 same config: ~7 ms），attach 完成时间从卡死 → 数十秒以内。

### V3 — gap 分布诊断

```bash
python3 - <<'PY'
import sys, numpy as np
sys.path.insert(0, '.')
from digital_twin_stats import load_srs_v2, load_gt, align_by_slot
H_srs, meta = load_srs_v2('logs/latest')
H_gt, gt_slots = load_gt('logs/latest/sionna_gt', return_slot_ids=True)
srs_abs = np.asarray(meta['abs_slots'], dtype=np.int64)
print(f"SRS  : {len(srs_abs)} frames")
print(f"GT   : {len(gt_slots)} frames, gap_median={int(np.median(np.diff(np.sort(gt_slots))))}")
for tol in (20, 5, 3, 1, 0):
    s, g, _ = align_by_slot(srs_abs, gt_slots, tol_slots=tol)
    if s.size:
        gaps = np.abs(srs_abs[s] - gt_slots[g])
        print(f'tol={tol:>2}: {s.size}/{srs_abs.size} ({100*s.size/srs_abs.size:.1f}%)  med={int(np.median(gaps))}  max={int(gaps.max())}')
s, g, _ = align_by_slot(srs_abs, gt_slots, tol_slots=20)
gaps = np.abs(srs_abs[s] - gt_slots[g])
u, c = np.unique(gaps, return_counts=True)
print('gap dist:', dict(zip(u.tolist(), c.tolist())))
PY
```

预期可能性（参考 SRS_GT_ALIGNMENT_ISSUE.md §10.5）：
- 全 gap=0 → 情况 A：纯采样率问题，对齐已解决
- 单峰 gap=N → 情况 B：+N IPC 管道延迟
- 散布 → 情况 C：两层都有

---

## 8. 风险 & 回滚

### 风险

| | 缓解 |
|---|---|
| cuda stream 同步漏洞导致 corrupt | stage 用 `event.synchronize()` 显式等待；commit 用 `copy_stream.wait_event` 防御性同步 |
| pool 满频繁 fallback | 监控 `_async_overflow_count`，必要时调大 |
| shutdown 死锁 | `_copy_q.join() → flush → _write_q.join() → 各线程 join(timeout)` 顺序 |
| 跨进程内存竞争（P0） | stage 强制在 release_batch 之前 |
| 多线程修改 buffer | `_buffer_locks` per UE |

### 回滚

任何一步出问题，立即切回 v7：

```bash
# 临时单次回滚
PROXY_VER=v7 SWEEP_SCRIPT=$(pwd)/run_q4_snr_sweep_v7.sh bash run_fair_ab_minimal.sh

# 或永久回滚
sed -i 's/run_q4_snr_sweep_v8.sh/run_q4_snr_sweep_v7.sh/' run_fair_ab_minimal.sh
sed -i 's/run_q4_snr_sweep_v8.sh/run_q4_snr_sweep_v7.sh/' run_mmse_sigma2_abcd.sh
sed -i 's/PROXY_VER:-v8/PROXY_VER:-v7/' run_fair_ab_minimal.sh run_mmse_sigma2_abcd.sh
```

v7 文件全部保留不动。
