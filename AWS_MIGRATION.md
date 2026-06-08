# AWS Migration Runbook — OAI_luuuuuu

本手册把实验室本地服务器上的工作流 (`DevChannelProxyJIN` + OAI + Sionna) 迁移到
**GitHub (代码) + EC2 (计算) + S3 (数据)** 三件套。任何同事/助教拿到这份文档都能
在 AWS 上端到端复现。

> **迁移状态快照（2026-04-22 更新）**
>
> - ✅ **Phase 0**：GitHub 仓库已建 (`github.com/luuuuuu6/OAI_luuuuuu`)，含 OAI submodule
> - ✅ **Phase 1**：数据全量同步到 S3（见 §2.3 — `raw/` / `runs/` / `figures/` 已到位，
>   含从 `/tmp/oai_gpu_ipc/` 救回的 sionna_gt ground truth）
> - ✅ **Phase 2**：`requirements.txt` / `requirements-sim.txt` / `bootstrap_ec2.sh`
>   / `QUICK_START_EC2.md` 全部就绪
> - ⬜ **Phase 3**：在 EC2 上端到端验证（待用户执行）
> - ⬜ **Phase 4**：回收实验室机器（验证通过后执行）
>
> **日常操作请直接看 `QUICK_START_EC2.md`**（一页纸版）。

---

## 0. 资产拓扑

```
            ┌─────────────────────────┐
            │   GitHub                │
            │   ├── OAI_luuuuuu       │  <- 本仓库，分析脚本 + 配置
            │   └── openairinterface5g_whan │  <- submodule (OAI 5G 源码)
            └────────────┬────────────┘
                         │ git clone --recurse-submodules
                         ▼
            ┌─────────────────────────┐         ┌─────────────────────────┐
            │   EC2 (Ubuntu 22.04)    │◀──────▶ │   S3 bucket             │
            │   - venv + TF + Sionna  │  sync   │   ├── raw/              │
            │   - OAI build (可选)    │         │   ├── runs/             │
            │   - tmux 长任务         │         │   ├── sweeps/           │
            │                         │         │   └── figures/          │
            └─────────────────────────┘         └─────────────────────────┘
```

- **代码**：本仓库 + `openairinterface5g_whan` 子模块
- **数据**：原始光追 `.npy` / 运行 logs / 扫参输出 / 图 → 全部走 S3
- **运行**：EC2；分析在 CPU 机型，Sionna/TF 仿真在 GPU 机型

---

## 1. GitHub 侧 (一次性)

### 1.1 确认远端
```bash
git remote -v
# origin  https://github.com/<user>/OAI_luuuuuu.git (push)
```

### 1.2 推送本次准备
```bash
git add .gitignore AWS_MIGRATION.md \
        DevChannelProxyJIN/vRAN_Socket/requirements.txt \
        DevChannelProxyJIN/vRAN_Socket/requirements-sim.txt
git commit -m "chore(aws): prep migration — gitignore, requirements, runbook"
git push origin main
```

### 1.3 子模块注意
- OAI 源码 (`DevChannelProxyJIN/openairinterface5g_whan`) 作为 submodule 管理
- EC2 clone 时**必须**带 `--recurse-submodules`

---

## 2. S3 侧 (一次性)

> **已实际使用的桶名：`oai-luuuuuu`**（不是历史文档里写的 `luuuuuu-oai-data`）。
> 下文所有示例也按现状更新。

### 2.1 创建桶（参考，已完成）
```bash
# region 选离你最近的，当前用首尔 ap-northeast-2
aws s3 mb s3://oai-luuuuuu --region ap-northeast-2

# 建议开启版本控制 (防误删) —— 尚未开启
aws s3api put-bucket-versioning \
    --bucket oai-luuuuuu \
    --versioning-configuration Status=Enabled
```

### 2.2 实际目录约定
```
s3://oai-luuuuuu/
├── raw/                     # 原始/基础数据
│   ├── cfr/                 # SRS + true CFR .npy
│   ├── cfr_results/         # 可视化图
│   └── saved_rays_data/     # 光追 ray 参数
├── runs/                    # 所有实验 log（包含单次运行和 sweep）
│   ├── <YYYYMMDD_HHMMSS>_<tag>/     # 单次 gNB+UE+proxy
│   └── q4_sweep_<YYYYMMDD_HHMMSS>/  # Q4 SNR 扫参（含 sionna_gt GT）
└── figures/
    └── data_out/            # 论文/报告用图
```

注意：原设计有独立的 `sweeps/` 前缀，实际采用扁平方案 —— sweep 直接放在 `runs/` 下
以 `q4_sweep_` 前缀区分，避免双重路径。

### 2.3 首次上传 (Phase 1 — 2026-04-22 已执行完毕)
```bash
cd /home/dclserver78/OAI_luuuuuu/DevChannelProxyJIN

# 光追 ray 参数
aws s3 sync vRAN_Socket/saved_rays_data/ \
    s3://oai-luuuuuu/raw/saved_rays_data/

# 图
aws s3 sync vRAN_Socket/data_out/ \
    s3://oai-luuuuuu/figures/data_out/

# 全量 log (!! 关键 !! 用 --follow-symlinks 把 /tmp 里的 sionna_gt 救出来)
# 每个 run 目录下 sionna_gt -> /tmp/oai_gpu_ipc/sionna_gt/<run_id>，
# 实验室机器一重启 /tmp 就没了。
aws s3 sync logs/ s3://oai-luuuuuu/runs/ \
    --follow-symlinks \
    --exclude "latest" --exclude "latest/*"
```

执行结果（2026-04-22）：1903 objects / 7.09 GB 全部入桶。

### 2.4 Lifecycle (自动省钱)
Console → S3 → bucket → Management → Lifecycle rules:
- `runs/` + `sweeps/` → 30 天后转 Glacier Instant Retrieval
- `raw/` → 60 天后转 Glacier Deep Archive (超便宜；要用时提前 12h 解冻)

---

## 3. EC2 侧 (每次开机)

### 3.1 实例选型

| 场景 | 推荐机型 | 说明 |
|---|---|---|
| 纯分析/画图 (`compare_nmse.py`, `q4_convergence_sweep.py`) | `c6i.4xlarge` (16 vCPU) | 只装 `requirements.txt` |
| Sionna 光追 / `v4.py` 仿真 | `g5.xlarge` (A10G 24GB) | 加装 `requirements-sim.txt` |
| 编译 OAI 5G (gNB/UE) | `c6i.8xlarge`, Ubuntu 22.04 | 跟 OAI 官方 build-oai 脚本跑 |

- OS: **Ubuntu 22.04 LTS**
- 存储: gp3 **≥100 GB**
- Security Group: inbound `22/tcp` from your IP only

### 3.2 Bootstrap (在 EC2 里第一次)

```bash
sudo apt update
sudo apt install -y git git-lfs build-essential cmake \
                    python3.10 python3.10-venv python3-pip \
                    awscli tmux htop rsync

# AWS 凭证
aws configure   # AccessKey / SecretKey / ap-northeast-2 / json

# 拉代码 (含 OAI submodule)
git clone --recurse-submodules \
    https://github.com/<user>/OAI_luuuuuu.git
cd OAI_luuuuuu

# Python 环境 (分析)
cd DevChannelProxyJIN/vRAN_Socket
python3.10 -m venv venv_lu
source venv_lu/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 如果是 GPU 机型 + 要跑 Sionna 仿真
pip install -r requirements-sim.txt
python -c "import tensorflow as tf; print(tf.config.list_physical_devices('GPU'))"
```

### 3.3 拉数据 (按需)
```bash
export S3_BUCKET=s3://oai-luuuuuu

# 只拉当前实验需要的那一小块，别一次性拉 7GB
aws s3 sync $S3_BUCKET/raw/saved_rays_data/ \
    ~/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/saved_rays_data/
```

### 3.4 跑实验 (tmux 防断线)
```bash
tmux new -s q4sweep
cd ~/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy
source ../venv_lu/bin/activate
bash run_q4_snr_sweep.sh
# Ctrl+B, D  脱离; 再次登陆 `tmux a -t q4sweep`
```

### 3.5 回传结果
```bash
RUN_ID=$(ls -td ~/OAI_luuuuuu/DevChannelProxyJIN/logs/*/ | head -1 | xargs basename)
aws s3 sync ~/OAI_luuuuuu/DevChannelProxyJIN/logs/$RUN_ID/ \
    $S3_BUCKET/runs/$RUN_ID/ --follow-symlinks
aws s3 sync ~/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/data_out/ \
    $S3_BUCKET/figures/data_out/
```

### 3.6 不用时关机
AWS console → Instances → **Stop** (不是 Terminate!)
- Stopped 状态只收 EBS 磁盘费 (~$8/月/100GB)
- 再次启动时公网 IP 会变 (用 Elastic IP 可固定)

---

## 4. IAM / 安全最佳实践

1. **不要用 root access key**。到 IAM 新建 user `luuuuuu-dev`，只给
   - `AmazonS3FullAccess` (或更细：限定到 `oai-luuuuuu` 桶)
   - `AmazonEC2FullAccess` (自用没问题)
2. SSH 用 **密钥对**，不要开密码登陆。
3. Billing Alarm: Budgets → 设置 $50/月 告警邮件。

---

## 5. 常见坑

| 症状 | 原因 | 解决 |
|---|---|---|
| `git clone` 后 `openairinterface5g_whan/` 空 | 忘了 `--recurse-submodules` | `git submodule update --init --recursive` |
| EC2 上 `import sionna` 报 CUDA 错 | TF 2.17 要 CUDA 12.3，AMI 自带版本不匹配 | 用 `tensorflow[and-cuda]` 把 cuda wheel 一起装 |
| `aws s3 sync` 很慢 | 默认单连接 | `aws configure set default.s3.max_concurrent_requests 20` |
| 本地 `.npy` 没推到 GitHub | 正是期望行为 (被 `.gitignore` 屏蔽)  | 用 `aws s3 sync` 推到 S3 |
| SSH 断开后实验死了 | 没开 tmux | `tmux new -s <name>` 再跑 |
| EC2 账单爆炸 | GPU 实例忘记 Stop | Budgets 告警 + 每天下班前 `aws ec2 stop-instances` |

---

## 6. 交差 checklist

- [ ] GitHub 仓库 URL 可访问，且含 OAI submodule
- [ ] `requirements.txt` / `requirements-sim.txt` 在仓库里
- [ ] S3 桶存在，`raw/ runs/ sweeps/ figures/` 四个前缀已有数据
- [ ] 新开一台 EC2 能按本文档 §3.2 跑通 `python compare_nmse.py --help`
- [ ] Budget alarm 已配置
