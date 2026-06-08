# SYNC_README — 项目同步说明

本工作目录 `OAI_luuuuuu` 提供一个一键同步脚本 [`sync_to_60.sh`](./sync_to_60.sh)，
用于把项目从开发机同步到目标主机。

---

## 1. 双机信息

| 角色 | 主机 | 用户 | 项目路径 |
|------|------|------|---------|
| 源 (开发机) | `165.132.192.78` | `dclserver78` | `/home/dclserver78/OAI_luuuuuu/` |
| 目标 (部署机) | `165.132.121.60` | `dclcom57` | `~/OAI_luuuuuu/` |

传输方式：`rsync` over SSH，带压缩、增量、断点续传。首次全量约 17 GB，
之后每次只传差异，通常秒级到分钟级完成。

---

## 2. 脚本能做什么

脚本 `sync_to_60.sh` 默认会：

- 保留权限、时间戳等元信息 (`-a`)
- 压缩传输 (`-z`)
- 显示进度并支持断点续传 (`-P`)
- 自动排除以下无意义的产物：
  - `__pycache__/`、`*.pyc`、`*.pyo`
  - `.pytest_cache/`、`.mypy_cache/`、`.ipynb_checkpoints/`
  - `*.swp`、`*.tmp`、`core`、`.DS_Store`
- 任何一步失败立即中止 (`set -euo pipefail`)，避免半同步状态

---

## 3. 使用方法

```bash
cd ~/OAI_luuuuuu

./sync_to_60.sh --check     # 先校验两边是否一致
./sync_to_60.sh --dry-run   # 预览将要同步的内容（不实际传输）
./sync_to_60.sh             # 真正执行同步
```

三种模式说明：

| 命令 | 作用 | 是否传输文件 |
|------|------|------------|
| `./sync_to_60.sh --check`   | 按内容哈希比对两端，若输出文件列表为空即两边完全一致 | 否 |
| `./sync_to_60.sh --dry-run` | 预览**这次如果真同步**会动哪些文件 | 否 |
| `./sync_to_60.sh`           | 实际执行同步，过程中会提示输入目标机密码一次 | 是 |
| `./sync_to_60.sh --help`    | 显示帮助信息 | 否 |

---

## 4. 推荐工作流（防止断线）

建议在 `screen` 中执行，以免 SSH 断开导致同步中止：

```bash
screen -S sync
cd ~/OAI_luuuuuu
./sync_to_60.sh
# 按 Ctrl+A 再按 D 可以离开（detach）
# 用 screen -r sync 随时回来查看进度
```

如果传输中途真的断了，**重新跑同一条命令**即可，`rsync` 会自动
跳过已同步的文件，只补传差异，不会从头重来。

---

## 5. 常见场景

### 5.1 只想同步某个子目录

脚本默认整目录同步。如只想快速传某个子目录，手动执行：

```bash
rsync -avzP \
  /home/dclserver78/OAI_luuuuuu/DevChannelProxyJIN/ \
  dclcom57@165.132.121.60:~/OAI_luuuuuu/DevChannelProxyJIN/
```

### 5.2 想反向同步（目标机 → 开发机）

```bash
rsync -avzP \
  dclcom57@165.132.121.60:~/OAI_luuuuuu/ \
  /home/dclserver78/OAI_luuuuuu/
```

⚠️ 注意方向：源与目标对调即可。不确定时先加 `-n` 做 dry-run。

### 5.3 修改排除规则

直接编辑 `sync_to_60.sh` 里的 `EXCLUDES` 数组，例如想忽略大 zip：

```bash
EXCLUDES=(
  --exclude='__pycache__/'
  --exclude='*.pyc'
  # ...
  --exclude='*.zip'          # 新增这一行
)
```

---

## 6. 验证传输是否完整

除了 `./sync_to_60.sh --check` 外，还可以快速对比两边统计：

```bash
echo "=== 本机 ==="
du -sh /home/dclserver78/OAI_luuuuuu
find /home/dclserver78/OAI_luuuuuu -type f | wc -l

echo "=== 目标机 ==="
ssh dclcom57@165.132.121.60 \
  "du -sh ~/OAI_luuuuuu && find ~/OAI_luuuuuu -type f | wc -l"
```

文件数和总大小基本一致即可（因排除规则、时区元信息等，允许**极小差异**）。

---

## 7. 注意事项

- 脚本**不处理 `.ssh`、`.bashrc` 等家目录配置**，只同步 `OAI_luuuuuu/` 下内容。
- 目标机已有同名文件会被 **覆盖**（`rsync -a` 默认行为），
  如果不希望覆盖目标机上较新的文件，可加 `--update`：
  ```bash
  rsync -avzP --update ...
  ```
- 脚本**不会删除目标机上源端已不存在的文件**。如需镜像模式（严格一致），
  可手动加 `--delete`，但务必先 dry-run 确认：
  ```bash
  rsync -avzP -n --delete ...   # 先预览
  rsync -avzP    --delete ...   # 再实际删除同步
  ```
- `venv_lu/` 这种虚拟环境目录目前**会被同步**。如果两台机 Python 版本或
  系统库差异较大，建议在目标机重建 venv 而不是沿用源端的。

---

## 8. 快速参考

```bash
# 一键同步
./sync_to_60.sh

# 校验
./sync_to_60.sh --check

# 预览
./sync_to_60.sh --dry-run

# 后台 screen 运行
screen -S sync ./sync_to_60.sh
```
