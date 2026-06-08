# sionna-proxy 容器 zombie 进程泄漏问题

> 记录日期:2026-05-10
> 触发场景:`run_q4_snr_sweep_v8.sh` + `launch_all_v8.sh` 反复跑 sweep 后

---

## 1. 现象

跑 sweep 几次/几十次之后,`sudo docker exec sionna-proxy ps -ef` 看到一堆:

```
root  155   1  0 10:03 ?  00:00:00 [python3] <defunct>
root  156   1  3 10:03 ?  00:09:15 [python3] <defunct>
root  671   1  0 10:42 ?  00:00:00 [python3] <defunct>
...
root  7833  1 57 13:57 ?  00:15:10 [python3] <defunct>
```

`<defunct>` = **zombie 进程**(已经死了但 parent 没 `wait()` 回收)。

实测一天跑 28 次 sweep,累积 **28+ zombie**(累积速度 ~1 个/sweep)。

---

## 2. 影响

| 层面 | 影响 |
|---|---|
| CPU | 0(zombie 不占 CPU) |
| 内存 | 极小(每 zombie ~几十 KB,28 个 < 1 MB) |
| **PID 表** | 占着 PID,理论上有 PID 耗尽风险(Linux 默认 32768 PID,基本到不了) |
| **fd / shm 持有** | **可能持有未释放的 GPU IPC handle / shm segment**(主要副作用) |
| 跑下次 sweep | 新 sweep 的 GPU IPC 看到旧 zombie 的残留 → AGC / handshake 异常 → UE attach 失败 |

→ 最实际的影响:**新 sweep UE attach 失败 / SRS 数据不来**(2026-05-10 我们大量 SNR=20 / SNR=25 失败的根因)。

---

## 3. 技术根因

| 位置 | 机制 |
|---|---|
| `launch_all_v8.sh` 启动 sionna proxy | `docker exec sionna-proxy python3 v8.py ...` |
| `v8.py` 内部 | 用 Python `multiprocessing` 起子进程(`UnifiedChannelProducerProcess`、GPU IPC worker、Sionna init worker 等) |
| sweep 结束 / SIGTERM 触发 | `launch_all` 的 `cleanup()` 用 `pkill -9 -f v8.py` 暴力杀主进程 |
| 主进程被 SIGKILL | **没机会**调用 `pool.close()` / `wait()` 子进程 |
| 子进程被孤立 | 父进程死 → kernel 把它们 reparent 到 PID 1(容器 init,这里是 `/sbin/docker-init`) |
| 子进程自然退出后 | docker-init 应该 `wait()` 回收,但 **subreaper 行为不一定触发到所有 grandchild** → 残留 zombie |

补充:容器是用 `sleep infinity` 当 entrypoint,挂载 sionna 镜像后通过 `docker exec` 运行 Python 脚本——这种用法下 docker-init 不一定挂在所有 Python 子进程的祖先链上,导致 reaping 失败。

---

## 4. 修复方案

按"投入 / 收益"排序:

### A. (短期) launch_all 加 graceful SIGTERM
**位置**:`launch_all_v8.sh` 的 `cleanup()` 函数

**改法**:把暴力 `pkill -9 -f v8.py` 改成两段式:

```bash
# 先 SIGINT/SIGTERM,给 multiprocessing pool 5 秒 cleanup
sudo docker exec sionna-proxy pkill -INT -f "v[0-9]\.py" 2>/dev/null
for i in 1 2 3 4 5; do
    if ! sudo docker exec sionna-proxy pgrep -f "v[0-9]\.py" >/dev/null; then
        break
    fi
    sleep 1
done
# 5 秒还没退,才 SIGKILL
sudo docker exec sionna-proxy pkill -9 -f "v[0-9]\.py" 2>/dev/null
```

**投入**:30 分钟改 + 调试
**收益**:每次 sweep 留下的 zombie 从 3-10 减到 0-1
**风险**:多了 5 秒 cleanup 等待,但比 docker restart 快

### B. (中期) sionna-proxy 容器加 `--init` flag
**位置**:`docker-compose.yml` 的 sionna-proxy service

**改法**:加 `init: true`

```yaml
services:
  sionna-proxy:
    init: true                  # 用 tini 当 PID 1,自动 reap zombie
    image: oai_sionna_luuuuuu-oai_sionna_proxy:latest
    ...
```

或用 docker run 时加 `--init`。

**投入**:5 分钟改 yaml + `docker compose down && docker compose up -d`
**收益**:tini 主动 reap **任何** orphan,zombie 永久消失
**风险**:docker compose down/up 会断开当前容器,需要重新 attach gNB(10-20 分钟恢复)

### C. (长期) `v8.py` 自己加 SIGTERM handler
**位置**:Python 源码 `v8.py`

**改法**:

```python
import signal

def _graceful_shutdown(signum, frame):
    print(f"[v8] caught signal {signum}, draining multiprocessing pool...")
    if 'pool' in globals():
        pool.close()
        pool.join()
    sys.exit(0)

signal.signal(signal.SIGTERM, _graceful_shutdown)
signal.signal(signal.SIGINT,  _graceful_shutdown)
```

**投入**:1-2 小时(还要找所有 multiprocessing pool / process 的引用)
**收益**:即使没 A,SIGTERM 也能 graceful;跟 A 互补
**风险**:`pool.join()` 可能 hang(子进程卡死时),要加 timeout

### D. (现状) preflight 自动 docker restart sionna-proxy
**位置**:`preflight.sh` 已实现(detect zombie → `docker restart sionna-proxy`)

**优点**:零代码改动,自动化
**缺点**:每次重启容器多 5-8 秒;sionna 内部 cuda init 也要重新跑

---

## 5. 推荐路线

1. **本周**:继续用 D(preflight),不动 launch_all / v8.py
2. **下周**:做 B(加 `--init` 到 docker-compose,5 分钟改完)——一劳永逸
3. **月内**:做 A(graceful cleanup),让 sweep 收尾更优雅
4. **以后**:做 C 作为 defense-in-depth

**优先 B 的原因**:tini reap zombie 是行业标准做法,Docker 官方都推荐;改 yaml 比改 bash/Python 安全,不会引入新 bug。

---

## 6. 长期监控

把 zombie count 加到 `preflight.sh` 输出(已经有了),还可以 grep `gnb.log` 末尾的:

```
[SRS Dump] Captured : N frames
```

如果 N 比预期(3% × frame 数)低很多,可能就是 zombie 影响了 GPU IPC handoff,要立即 docker restart sionna-proxy。

---

## 7. 相关文件

- `launch_all_v8.sh` — 包含 `cleanup()` 函数的 sweep launcher
- `run_q4_snr_sweep_v8.sh` — 上层 sweep 调度器
- `preflight.sh` — 当前的 workaround(自动 detect + restart)
- `v8.py`(在 sionna-proxy 容器内) — Sionna proxy 主进程,用 multiprocessing
