# SETUP_MANUAL3.md — 按 DCL Manual 3 重建 AWS 开发环境

> **背景**：原 Phase 3 EC2（`i-0e310cd8e1322bf65`、`c6i.4xlarge`）已于 2026-04-22 terminate，
> 同时清掉 Security Group `sg-0efda33158b334efc` 和 Key Pair `oai-phase3-test`。
> GitHub Deploy Key 已删除。S3 桶 `oai-luuuuuu`（7.09 GB / 1903 obj）保留。
>
> 本文件是"从零按实验室 Manual 3 规范重开"的一页纸操作清单，之后可平滑切换到 KIRO。
>
> **配套参考**：`manual.md`（DCL Manual 3 韩文原文）、`AWS_MIGRATION.md`、
> `QUICK_START_EC2.md`、`Change_log.md §26`。

---

## 进度总览（最后更新：2026-04-22）

| 阶段 | 步骤 | 状态 |
|------|------|------|
| §A | 创建 Key Pair `oai_luuuuuu` | ✅ 完成 |
| §A | pem 放到笔记本 `C:\Users\11459\.ssh\oai_luuuuuu.pem` | ✅ 完成 |
| §B | 创建编辑盒 `oai-luuuuuu1`（c7i.2xlarge / 50GB gp3） | ✅ 完成 |
| §C | 笔记本 `~/.ssh/config` 写入 `Host oai-luuuuuu1` | ⏳ **下一步** |
| §D | 首次 SSH 测通 | ⏳ |
| §E | 编辑盒装 git + 生成 key + GitHub Deploy Key + `git clone` | ⏳ |
| §E | 编辑盒装 AWS CLI + `aws configure` | ⏳ |
| §E | S3 smoke test（`compare_nmse.py --help`） | ⏳ |
| §F | 今天用完 Stop 编辑盒（省钱） | ⏳ |
| §G | GPU 盒 Service Quota 申请 | ⏳ 延后 |
| §G | GPU 盒建好（g6e.xlarge / g5.xlarge） | ⏳ 延后 |
| §G.5 | GPU 盒 Sionna Proxy 复刻（Docker + OAI build + smoke test） | ⏳ 延后 |
| §G.5 | 把验证好的 GPU 盒存为 Custom AMI（灾备快照） | ⏳ 延后 |
| §H | 切换到 KIRO | ⏳ 最终 |
| §I.1 | 笔记本装 AWS CLI + `aws configure`（`laptop-oai-2026` key） | ✅ 完成 2026-04-23 |
| §I.2 | 建 IAM Role `oai-ec2-ssm-role` + Instance Profile，挂到 EC2 | ⏳ **进行中** |
| §I.3 | 验证 SSM 注册（`PingStatus: Online`） | ⏳ |
| §I.4 | 笔记本装 Session Manager Plugin | ⏳ |
| §I.5 | 试连 `aws ssm start-session` | ⏳ |
| §I.6 | `~/.ssh/config` 加 `oai-luuuuuu1-ssm` 段（ProxyCommand） | ⏳ |
| §I.7 | 关 SG 22 入站（可选收尾） | ⏳ |

> 当前卡在：**§I.2**（笔记本 PowerShell 建 IAM Role + Instance Profile，挂到 EC2）。

---

## 架构说明（Manual 3 §1）

实验室规范是"**两台 EC2** + GitHub + S3 + 本地 H100"四件套。本文件分两阶段建：

| 阶段 | 机器 | 用途 | 何时建 |
|------|------|------|--------|
| **阶段 1（本次）** | **编辑盒** `c7i.2xlarge`（无 GPU） | 代码编辑、git、轻量分析 | **现在就建** |
| **阶段 2（延后）** | **GPU 验证盒** `g6e.xlarge` / `g5.xlarge` | CUDA / Sionna 仿真验证 | 等 G-family 配额批下来再建（见 §G） |

> 日常 90% 时间只用编辑盒（便宜 ~$0.36/hr）。GPU 盒只在跑仿真那几小时才 Start，完了 Stop，其余时间只计 EBS ~$4/月。

---

## 0. 前置确认

- [x] 旧 EC2 已 terminate、SG/Key 已清、Deploy Key 已删
- [x] S3 桶 `oai-luuuuuu` 保留（ap-northeast-2）
- [x] IAM 用户 `luuuuuu`，本机（dclserver78）`~/.aws/credentials` 可用
- [x] GitHub 仓库 `git@github.com:luuuuuu6/OAI_luuuuuu.git`（Private）
- [x] 本次重建要创建的资源见 §A—§F 逐步打勾

**区域**：全程使用 **ap-northeast-2（首尔）**。所有 AWS 控制台操作前先看右上角区域。

---

## 资产登记（做完边填边记）

> 这一节是本文最重要的输出。每一步做完把值填进来，以后 KIRO / 下一台机器 / 下个人直接抄。

### 阶段 1：编辑盒（已建好 ✅ 2026-04-22）

| 项目 | 值 |
|------|-----|
| 编辑盒 Name | **`oai-luuuuuu1`** |
| 编辑盒 Instance ID | **`i-0b1afe76a52a91bbf`** |
| 编辑盒 公网 IPv4（首次） | **`3.34.28.179`**（stop/start 后会变） |
| 编辑盒 Public DNS | `ec2-3-34-28-179.ap-northeast-2.compute.amazonaws.com` |
| 编辑盒 私网 IP | `172.31.17.243` |
| 可用区 | `ap-northeast-2b` |
| 实例类型 | `c7i.2xlarge` (16 vCPU / 32 GiB / 无 GPU) |
| 根卷 | 50 GiB **gp3** |
| AMI | Canonical Ubuntu 22.04 amd64 (`ami-04d25ae66444b2b10`) |
| Key Pair 名称 | **`oai_luuuuuu`**（ED25519） |
| 本机 pem 路径（Windows 笔记本）| **`C:\Users\11459\.ssh\oai_luuuuuu.pem`** |
| Security Group | **`oai-luuuuuu1-sg`** / **`sg-0df84b6bac64599a7`** |
| SG 入站规则 | TCP/22 from `1.232.78.23/32`（笔记本当前公网 IP）|
| Deploy Key 标题（待加） | `oai-edit-2026` |
| 成本 | ~$0.36/hr running；~$4/月 stopped（50 GiB EBS） |

### 阶段 2：GPU 盒（未建，见 §G）

| 项目 | 值 |
|------|-----|
| GPU 盒 Name | `oai-luuuuuu-gpu`（拟） |
| GPU 盒 Instance ID | — |
| 机型候选 | `g6e.xlarge` (L40S 48GB) / `g5.xlarge` (A10G 24GB) |
| G-family 配额状态 | ⏳ 待申请 Service Quota（新账户默认 0 vCPU） |

---

## A. 创建 Key Pair（一次即可）✅ 已完成 2026-04-22

**入口**：EC2 → 左侧 Network & Security → **Key Pairs** → 右上 Create key pair  
**直达**：`https://ap-northeast-2.console.aws.amazon.com/ec2/home?region=ap-northeast-2#KeyPairs:`

| 字段 | 值 |
|------|-----|
| 名称 | `oai_luuuuuu` |
| 密钥对类型 | **ED25519**（不要 RSA） |
| 私钥文件格式 | **.pem** |
| 标签（可选） | `Project = oai-manual3` |

点「创建密钥对」→ 浏览器**自动下载 `oai_luuuuuu.pem`**（**只下载这一次**，丢了重建）。

**放到本机**（哪台机要 SSH 到 EC2，就放哪台）。
本次选择 **方案 B：pem 只放笔记本（Windows），不放 dclserver78**，和最终 KIRO 状态一致。

**Windows PowerShell**：

```powershell
# 1) 确保 .ssh 目录存在
New-Item -ItemType Directory -Force -Path $HOME\.ssh | Out-Null

# 2) 搬 pem（本次原始路径是 E:\）
Move-Item "E:\oai_luuuuuu.pem" "$HOME\.ssh\oai_luuuuuu.pem"

# 3) 收紧权限（Windows 用 icacls，替代 chmod 400）
$key = "$HOME\.ssh\oai_luuuuuu.pem"
icacls $key /inheritance:r
icacls $key /grant:r "$($env:USERNAME):R"

# 4) 验证
icacls $key
```

**Mac / Linux**：

```bash
mv ~/Downloads/oai_luuuuuu.pem ~/.ssh/oai_luuuuuu.pem
chmod 400 ~/.ssh/oai_luuuuuu.pem
ls -la ~/.ssh/oai_luuuuuu.pem   # 应为 -r--------
```

**完成标记**：
- [x] `.pem` 已下载并权限收紧
- [x] 放在：`C:\Users\11459\.ssh\oai_luuuuuu.pem`（Windows 笔记本）

---

## B. 创建编辑盒 EC2（c7i.2xlarge + 50GB gp3）✅ 已完成 2026-04-22

**入口**：EC2 → Instances → 右上 **Launch instances**（橙色）

### B.1 Name and tags

| 字段 | 值（本次实际） |
|------|-----|
| Name | **`oai-luuuuuu1`** |
| 附加 Tag | （未加，可选） |

### B.2 Application and OS Images (AMI)

- 标签页：**Quick Start**
- OS：**Ubuntu**
- AMI 下拉：**Ubuntu Server 22.04 LTS (HVM), SSD Volume Type**
- 架构：**64-bit (x86)** ⚠️ 不要 ARM / 不要 24.04

### B.3 Instance type

- 搜索并选择 **`c7i.2xlarge`**（16 vCPU / 32 GB RAM / 无 GPU）
- 约 **$0.36/hr**

### B.4 Key pair (login)

- 下拉选 **`oai_luuuuuu`**
- ⚠️ 不要点 "Create new"，不要选 "Proceed without key pair"

### B.5 Network settings（点右边 Edit）

| 字段 | 值（本次实际） |
|------|-----|
| VPC | 默认 (`vpc-0dc4c4ff33f2bc2f9`) |
| Subnet | No preference（自动分到 `ap-northeast-2b`）|
| **Auto-assign public IP** | **Enable** ✅ |
| Firewall | Create security group |
| Security group name | **`oai-luuuuuu1-sg`** |
| Security Group ID | **`sg-0df84b6bac64599a7`** |

**入站规则**（本次实际，**源 = 笔记本公网 IP**，不是 dclserver78）：

| Type | Protocol | Port | Source |
|------|----------|------|--------|
| SSH | TCP | 22 | **`1.232.78.23/32`**（笔记本当前 KT 公网 IP） |

> ⚠️ 不要用 `0.0.0.0/0`。Manual 3 §5.5 明确禁止。
>
> ⚠️ **源 IP 是笔记本的，不是 dclserver78 的**。
> 本次采用方案 B：SSH 发起方是**你的笔记本**，所以 SG source 用**笔记本公网 IP**。
> 以后换地方（家 → 咖啡店 → 出国）笔记本公网 IP 会变，需**进 SG 改这条 source**，见附录"IP 变化应对"。
> 查当前笔记本 IP：`(Invoke-RestMethod ifconfig.me/ip).Trim()`（PowerShell）。

### B.6 Configure storage

| 字段 | 值 |
|------|-----|
| Size | **50 GiB** ⚠️（不要留 8） |
| Volume type | **gp3** ⚠️（不要 gp2） |
| IOPS / Throughput | 默认 3000 / 125 |
| Delete on termination | ✅ 勾选 |
| Encrypted | 可勾可不勾（勾了免费） |

### B.7 Advanced details

| 字段 | 值 |
|------|-----|
| IAM instance profile | **留空**（暂走直连 SSH；未来要走 SSM 再加 `AmazonSSMManagedInstanceCore` Role） |
| User data | 留空 |

其它项全部默认。

### B.8 Summary 核对 → Launch instance

核对右侧 Summary：
- Number of instances: **1**
- Instance type: c7i.2xlarge
- Key pair: oai_luuuuuu
- Storage: 1× **50 GiB gp3**
- Security group: New → oai-edit-sg (22/TCP from My IP)

点 **Launch instance**。

### B.9 启动后登记（本次已填）

- Instance ID：`i-0b1afe76a52a91bbf`
- Public IPv4：`3.34.28.179`
- Public DNS：`ec2-3-34-28-179.ap-northeast-2.compute.amazonaws.com`
- AZ：`ap-northeast-2b`
- Status check：✅ 3/3 已通过 (2026-04-22)

**完成标记**：
- [x] 实例 Running、Status check 通过
- [x] Instance ID / 公网 IP 已填入资产登记

---

## C. 本机 `~/.ssh/config`（笔记本）

### Windows 笔记本（本次实际用法）

PowerShell 一次执行：

```powershell
New-Item -ItemType Directory -Force -Path $HOME\.ssh | Out-Null

@"
# === OAI 编辑盒 (c7i.2xlarge, Manual 3) ===
Host oai-luuuuuu1
    HostName 3.34.28.179
    User ubuntu
    IdentityFile C:\Users\11459\.ssh\oai_luuuuuu.pem
    ServerAliveInterval 60
    ServerAliveCountMax 3

"@ | Add-Content -Path $HOME\.ssh\config -Encoding utf8

Get-Content $HOME\.ssh\config    # 验证
```

### Mac / Linux 备用

```sshconfig
Host oai-luuuuuu1
    HostName 3.34.28.179
    User ubuntu
    IdentityFile ~/.ssh/oai_luuuuuu.pem
    ServerAliveInterval 60
    ServerAliveCountMax 3
```

> stop→start 后公网 IP 会变，届时回来改 `HostName` 即可。
> 一键查新 IP：`aws ec2 describe-instances --instance-ids i-0b1afe76a52a91bbf --query 'Reservations[0].Instances[0].PublicIpAddress' --output text`

**完成标记**：
- [ ] `~/.ssh/config` 已加 `oai-luuuuuu1` 段（笔记本）

---

## D. 首次 SSH 上机

在笔记本 PowerShell：

```powershell
ssh oai-luuuuuu1
# 第一次问 yes/no → yes
# 成功：ubuntu@ip-172-31-17-243:~$
```

**上去之后立刻做一次基础检查**：

```bash
uname -a
nproc                              # 应 16
free -h                            # 应 ~32 GiB
df -h /                            # 应 ~50 GB
```

**连不上的三个常见原因**：
1. `~/.ssh/oai_luuuuuu.pem` 权限不是 400 → `chmod 400`
2. Security Group 的 My IP 已变 → 在 SG inbound 里改成 `curl ifconfig.me` 的当前 IP
3. `HostName` 填错（stop/start 后 IP 变过）→ 改 `~/.ssh/config`

**完成标记**：
- [ ] `ssh oai-edit` 成功
- [ ] `nproc`、`free -h`、`df -h` 正常

---

## E. 编辑盒一次性环境（git + Deploy Key + clone）

**全部在编辑盒里执行（`ssh oai-edit` 进去之后）**。

### E.1 基础工具

```bash
sudo apt update
sudo apt install -y git tmux htop unzip curl
```

### E.2 生成 EC2 自己的 SSH key

```bash
ssh-keygen -t ed25519 -C "oai-edit-2026" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
```

**复制整行输出**（以 `ssh-ed25519 AAAA...` 开头）。

### E.3 GitHub 加 Deploy Key（只读）

浏览器打开：
`https://github.com/luuuuuu6/OAI_luuuuuu/settings/keys/new`

| 字段 | 填 |
|------|-----|
| Title | `oai-edit-2026` |
| Key | 粘贴 E.2 的公钥整行 |
| Allow write access | ❌ **不勾** |

点 **Add key**。

### E.4 Git 用户信息（一次）

```bash
git config --global user.name "luuuuuu6"
git config --global user.email "你的邮箱"
```

### E.5 Clone 仓库 + submodule

```bash
cd ~
git clone git@github.com:luuuuuu6/OAI_luuuuuu.git
cd OAI_luuuuuu
git submodule update --init --recursive
```

> 第一次 `ssh -T git@github.com` 可以先测，应看到
> `Hi luuuuuu6/OAI_luuuuuu! You've successfully authenticated, but GitHub does not provide shell access.`

### E.6 AWS CLI + 凭证（用来从 S3 拉数据）

```bash
# 装 AWS CLI v2
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip -q awscliv2.zip && sudo ./aws/install
rm -rf aws awscliv2.zip
aws --version

# 配凭证（使用你现有的 IAM user luuuuuu 的 Access Key）
aws configure
# Access Key ID / Secret / Region = ap-northeast-2 / Output = json

aws configure set default.s3.max_concurrent_requests 20
aws sts get-caller-identity       # 应显示 Account 554615220681 / user luuuuuu
```

### E.7（可选）跑仓库里的 bootstrap

```bash
cd ~/OAI_luuuuuu
bash bootstrap_ec2.sh           # 仅分析 (CPU)
# bash bootstrap_ec2.sh --with-sim   # 要 Sionna/TF GPU 仿真（编辑盒没 GPU，不建议）
# bash bootstrap_ec2.sh --with-oai   # 装 OAI 编译依赖（编辑盒纯分析通常不用）
```

> 注意：`bootstrap_ec2.sh` 原本含 `git clone` 步骤；在这里你已手动 clone，脚本里的 clone 遇到已存在会跳过或报警，按提示处理即可。

### E.8 拉一小块 S3 数据做 smoke test

```bash
# 举例：最新一次 Q4 sweep
aws s3 sync s3://oai-luuuuuu/runs/q4_sweep_20260422_145841/ \
            ~/OAI_luuuuuu/DevChannelProxyJIN/logs/q4_sweep_20260422_145841/

cd ~/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket
source venv_lu/bin/activate       # bootstrap 会建这个 venv
python compare_nmse.py --help     # 能出帮助就算通了
```

**完成标记**：
- [ ] Deploy Key 已加
- [ ] 代码 clone + submodule 成功
- [ ] AWS CLI 通，`sts get-caller-identity` 正常
- [ ] `compare_nmse.py --help` 能跑

---

## F. 下班前：Stop 省钱

```bash
# 方式 1：在编辑盒内部
sudo shutdown -h now

# 方式 2：在本机（你的电脑）
aws ec2 stop-instances --instance-ids i-xxxxxxxxxxxxx
```

**费用对照**（c7i.2xlarge + 50 GB gp3）：

| 状态 | 每天 |
|------|------|
| Running 24h | ≈ **$8.6**（按 $0.36/hr） |
| Stopped（只 EBS）| ≈ **$0.13**（≈ $4/月） |
| Terminated | $0（但盘和环境全删） |

> **绝对不要 terminate** 除非项目收尾。

**完成标记**：
- [ ] 今天用完已 Stop

---

## G. GPU 验证盒（第二台，延后建）

> Manual 3 §3 设计的第二台机器，**跑 Sionna / CUDA 验证用**，不是日常机，只在需要时 Start。

### G.1 为什么是两台不是一台

| 原因 | 说明 |
|------|------|
| **GPU 太贵不能常开** | g6e.xlarge ~$2.2/hr，24/7 挂一个月要 ~$1500；c7i.2xlarge 只 $0.36/hr |
| **编辑 ≠ 执行** | 写代码、git、看日志在编辑盒上做就够；只有跑仿真那段时间才需要 GPU |
| **工作流** | 编辑盒 `git push` → GPU 盒 `git pull` 跑 → 结果 `aws s3 sync` 回 S3 → `sudo shutdown -h now` |
| **手册规范** | Manual 3 §1.3 明确把两种角色分开 |

### G.2 前置：G-family 配额

新 AWS 账号默认 G / VT 配额 = **0 vCPU**，直接开会报 `vCPU limit of 0`，**必须先申请**：

1. AWS 控制台 → **Service Quotas** → Amazon EC2
2. 搜 **"Running On-Demand G and VT instances"** → 点开
3. 右上「请求增加配额」→ 填 **`8`**（`g6.xlarge` 是 4 vCPU，要 `g6e.xlarge` 或两台同时用就填 8）
4. 提交 → 等邮件审批（通常几小时到 1–2 天）

### G.3 建 GPU 盒（等配额批下来后）

和 §B 编辑盒几乎一样，**只改 4 处**：

| 字段 | GPU 盒取值 |
|------|-----------|
| Name | `oai-luuuuuu-gpu` |
| **AMI** | **AWS Deep Learning Base AMI (Ubuntu 22.04)** ← CUDA/驱动预装，比裸 Ubuntu 省几小时 |
| **实例类型** | **`g6e.xlarge`**（L40S 48GB）优先；不可用时退 `g5.xlarge`（A10G 24GB） |
| Storage | 50 GiB gp3（可加到 100 GB） |
| Key pair | 同一把 **`oai_luuuuuu`** |
| Security Group | 可复用 **`oai-luuuuuu1-sg`**（入站 22 from 笔记本 IP）|
| Auto-assign public IP | Enable |

> ⚠️ **g6e 在首尔 ap-northeast-2 不一定有货**。若报容量不足：
> - 降级 `g5.xlarge`（A10G 24GB，首尔通常有）
> - 或换区域 `us-east-1` / `us-west-2`（但数据在首尔 S3，跨区 sync 有流量费）

### G.4 GPU 盒首次上机

和编辑盒一样把 `oai-luuuuuu-gpu` 加到 `~/.ssh/config`：

```sshconfig
Host oai-luuuuuu-gpu
    HostName <公网 IP>
    User ubuntu
    IdentityFile C:\Users\11459\.ssh\oai_luuuuuu.pem
```

### G.5 Sionna Proxy 复刻清单（GPU 盒专属）

> **本节是整个 AWS 迁移最关键的一段**：把 dclserver78 上那套 `docker compose up sionna-proxy` + `sudo bash launch_all.sh` 完整搬到 GPU 盒，**架构一模一样，不做任何改动**。

#### G.5.0 现状架构回顾（为什么必须单机）

当前 `DevChannelProxyJIN/docker-compose.yml` 决定了整个 runtime：

```
┌─────── GPU 盒（单机）─────────┐
│                                │
│  Host 原生:                    │
│    nr-softmodem (gNB)          │   ← launch_all.sh 用 sudo 启
│    nr-uesoftmodem (UE)         │
│         │                      │
│         │ 共享内存 IPC         │
│         │ /tmp/oai_gpu_ipc/    │
│         ▼                      │
│  Docker: sionna-proxy          │   ← network_mode: host
│    tensorflow:2.17.0-gpu       │     ipc: host
│    + sionna 1.0.2              │     privileged: true
│    + cupy-cuda12x              │     NVIDIA_VISIBLE_DEVICES=all
│                                │
│       NVIDIA GPU（必需）       │
└────────────────────────────────┘
```

三条硬约束：
1. **`network_mode: host` + `ipc: host`** → Host gNB 与容器必须同机
2. **`privileged: true` + `NVIDIA_*`** → 必须有 NVIDIA 驱动 + `nvidia-container-toolkit`
3. **`/tmp/oai_gpu_ipc/gpu_ipc_shm` 共享内存** → 纯 Linux tmpfs，跨机器不可行

结论：**编辑盒完全不能跑**这套流水线；必须全部落到 GPU 盒。

#### G.5.1 选对 AMI，省 2 小时

在 §G.3 的 Launch Instance 页面，AMI 必须选：

**AWS Deep Learning Base GPU AMI (Ubuntu 22.04)**

搜索框输入 `Deep Learning Base OSS Nvidia Driver GPU` → 选 Ubuntu 22.04 那一个。这个 AMI 预装了：

| 组件 | 预装版本（大致） | 对应项目依赖 |
|------|----------------|-------------|
| NVIDIA Driver | 5xx 系列 | 必需 |
| CUDA 12.x | 12.x | `cupy-cuda12x` / TF 2.17 |
| Docker Engine | 24+ | `docker compose` |
| `nvidia-container-toolkit` | ✅ | `deploy.resources.devices[gpu]` |
| `docker compose` plugin | ✅ | 启动 `sionna-proxy` |

> ⚠️ 如果选了裸 Ubuntu 22.04（像编辑盒那样）→ 要自己装驱动 + CUDA + toolkit，加起来 2–3 小时，还容易踩签名 / DKMS 坑。**别省这步**。

启动后第一件事确认：

```bash
nvidia-smi                          # 应看到 L40S 或 A10G
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
# 若第二条能在容器里看到同一张卡 → nvidia-container-toolkit OK
```

#### G.5.2 克隆仓库 + 拉 submodule（OAI 代码量很大）

和编辑盒 §E 的步骤**完全一样**，但 GPU 盒要**单独**的 Deploy Key：

```bash
ssh-keygen -t ed25519 -C "oai-gpu-2026" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
# → 贴到 https://github.com/luuuuuu6/OAI_luuuuuu/settings/keys/new
#   Title: oai-gpu-2026, Allow write access: ❌
```

```bash
cd ~
git clone git@github.com:luuuuuu6/OAI_luuuuuu.git
cd OAI_luuuuuu
git submodule update --init --recursive     # openairinterface5g_whan 大约 1–2 GB
```

#### G.5.3 Build `sionna-proxy` Docker 镜像（~15–25 min，首次）

```bash
cd ~/OAI_luuuuuu/DevChannelProxyJIN
docker compose build sionna-proxy
# Dockerfile 基础镜像: tensorflow/tensorflow:2.17.0-gpu-jupyter
# 会 pip 装 sionna==1.0.2 / cupy-cuda12x / jupyterlab / open3d 等
# 完成后 docker images 应看到 devchannelproxyjin-sionna-proxy 或类似名
```

**Build 产出永远留在 EBS**：只要不 Terminate，之后 Stop/Start 100% 保留，再次启动秒进容器。

#### G.5.4 Build OAI host 二进制（`nr-softmodem` / `nr-uesoftmodem`，~20–30 min，首次）

host 这一侧是**原生编译**，不在 Docker 里：

```bash
cd ~/OAI_luuuuuu/DevChannelProxyJIN/openairinterface5g_whan/cmake_targets

# 首次只跑一次，装系统依赖
./build_oai -I                      # 约 10–15 min

# 编 gNB + UE
./build_oai --gNB --nrUE -w USRP    # 约 15–25 min
# 编完应看到:
#   ran_build/build/nr-softmodem
#   ran_build/build/nr-uesoftmodem
```

#### G.5.5 共享内存 tmpfs 目录

```bash
sudo mkdir -p /tmp/oai_gpu_ipc
sudo chmod 777 /tmp/oai_gpu_ipc
# launch_all.sh 会把 /tmp/oai_gpu_ipc/gpu_ipc_shm 权限改成 666，这里先给目录权限
```

#### G.5.6 Smoke test：两步验证（决定能不能正式跑）

**Step 1 — Bypass 模式（验证 OAI host 自己通，跟 sionna 无关）**：

```bash
cd ~/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy
sudo bash launch_all.sh -n 1 -b
# -n 1: 1 UE；-b: bypass channel（不过 Sionna）
# 看到 gNB↔UE SRS 握手、没 segfault → host OAI 编译 OK
```

**Step 2 — 真 Sionna（30 帧 quick sweep，验证 GPU + TF + Sionna）**：

```bash
# 用最小规模跑一次真的 Sionna pipeline
sudo bash launch_all.sh -n 1 -v v0        # v0.py 是最简 pipeline
# 看到：
#   proxy.log 里 "Entering main loop" 或 "Pipeline ready"
#   gNB 端 SRS 出 PDP 数据
# → Sionna + L40S + CUDA 12 + TF 2.17 全链路通
```

**两个 smoke test 通过前，不要跑大 sweep。**

#### G.5.7 成功后立刻存 Custom AMI（强烈推荐）

辛辛苦苦 build 好的 Docker 镜像 + OAI 二进制 + 驱动配置，丢了就又是 1 小时。存个快照：

```
AWS 控制台 → EC2 → 选 oai-luuuuuu-gpu 实例
→ Actions → Image and templates → Create image
Name: oai-luuuuuu-gpu-ready-v1
Description: DL Base + sionna-proxy built + OAI host built + smoke tested YYYY-MM-DD
No reboot: ⬜（建议不勾，让它重启以保证一致性）
```

以后 GPU 盒的 EBS 若出任何问题，从这个 AMI 直接 launch 一台新的，全套环境**5 分钟**就位。

#### G.5.8 日常跑 Q4 sweep（同 dclserver78，零改动）

```bash
# 上机
ssh oai-luuuuuu-gpu
cd ~/OAI_luuuuuu/DevChannelProxyJIN
git pull

# tmux 包住，SSH 断了不中断
tmux new -s sweep
cd vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy
sudo bash launch_all.sh -n 1 -v v4 -bs 280
# Ctrl-b d 脱离 tmux

# 跑完回同一 session 看结果：tmux attach -t sweep

# 数据回 S3
RUN_ID=$(ls -1dt ~/OAI_luuuuuu/DevChannelProxyJIN/logs/q4_sweep_* | head -1 | xargs basename)
aws s3 sync ~/OAI_luuuuuu/DevChannelProxyJIN/logs/$RUN_ID/ \
            s3://oai-luuuuuu/runs/$RUN_ID/

# 下机 Stop（务必 Stop 不是 Terminate）
sudo shutdown -h now
```

#### G.5.9 故障快速定位

| 症状 | 大概率原因 | 处置 |
|------|----------|------|
| `docker: Error response from daemon: could not select device driver "nvidia"` | 没装 `nvidia-container-toolkit`（选了错 AMI） | 换 Deep Learning Base AMI 重开 |
| `docker compose build` 中 `pip install sionna` 超时 | 公网带宽抖 / PyPI 慢 | 重跑 `docker compose build`；EBS 已缓存部分层，第二次快 |
| `./build_oai -I` 报缺系统包 | 跑早了，apt 源没更新 | `sudo apt update` 后重跑 |
| `launch_all.sh` 里 `docker exec sionna-proxy` 报 "No such container" | 容器没起 | 先 `cd DevChannelProxyJIN && docker compose up -d sionna-proxy` |
| gNB 起来但 UE 接不上 | `/tmp/oai_gpu_ipc/gpu_ipc_shm` 权限不是 666 | `sudo chmod 666 /tmp/oai_gpu_ipc/gpu_ipc_shm*` |
| GPU 利用率 0%，但脚本在跑 | TF 没认卡 | `docker exec sionna-proxy python -c "import tensorflow as tf; print(tf.config.list_physical_devices('GPU'))"` 必须非空 |

**完成标记（§G.5 专属）**：
- [ ] Deep Learning Base AMI 启动后 `nvidia-smi` + `docker run --gpus all ... nvidia-smi` 都正常
- [ ] GPU 盒 Deploy Key 已加，repo + submodule clone 完成
- [ ] `docker compose build sionna-proxy` 成功
- [ ] `./build_oai --gNB --nrUE` 成功，`nr-softmodem` / `nr-uesoftmodem` 可执行
- [ ] `/tmp/oai_gpu_ipc/` 已建
- [ ] Smoke test 1 (`-n 1 -b` bypass) 通过
- [ ] Smoke test 2 (`-n 1 -v v0` 真 Sionna) 通过
- [ ] Custom AMI `oai-luuuuuu-gpu-ready-v1` 已创建

---

### G.6 GPU 盒日常运维

```bash
# 在 GPU 盒上（通过 SSH 或 KIRO 远程）
git clone git@github.com:luuuuuu6/OAI_luuuuuu.git   # 首次，需加 GPU 盒 Deploy Key
# 后续只 pull
git pull

# 跑 Sionna（§G.5.8 详版）
cd ~/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy
sudo bash launch_all.sh -n 1 -v v4 -bs 280

# 结果回 S3
aws s3 sync ~/OAI_luuuuuu/DevChannelProxyJIN/logs/q4_sweep_xxxx/ \
            s3://oai-luuuuuu/runs/q4_sweep_xxxx/

# 用完立即关（省钱）
sudo shutdown -h now     # 进入 stopped，只计 EBS
```

> GPU 盒需要**单独的 GitHub Deploy Key**（和编辑盒分开），按 §E.2–E.3 重做一次（注释里写 `oai-gpu-2026`），§G.5.2 已包含。

### G.7 编辑盒 + GPU 盒联动：一键验证脚本

编辑盒上放一个脚本（Manual 3 §9.8）：

```bash
#!/bin/bash
# ~/verify_on_gpu.sh
GPU_ID="i-xxxxxxxx"    # GPU 盒 Instance ID，建好后填
aws ec2 start-instances --instance-ids $GPU_ID
aws ec2 wait instance-running --instance-ids $GPU_ID
ssh oai-luuuuuu-gpu "cd ~/OAI_luuuuuu && git pull && \
                     CUDA_VISIBLE_DEVICES=0 python run_channel_sim.py && \
                     aws s3 sync results/ s3://oai-luuuuuu/runs/ && \
                     sudo shutdown -h now"
echo "验证完成，GPU 盒已关"
```

**完成标记**（§G 整章总览）：
- [ ] Service Quota 已申请（G and VT → 8 vCPU）
- [ ] Service Quota 已批
- [ ] GPU 盒已建（填 §G.3 表格中"本次实际"列）
- [ ] `~/.ssh/config` 加 `oai-luuuuuu-gpu` 段
- [ ] §G.5 Sionna Proxy 复刻清单**全部打勾**（独立小节里）
- [ ] Custom AMI `oai-luuuuuu-gpu-ready-v1` 已保存
- [ ] `verify_on_gpu.sh` 已放到编辑盒

---

## H. 切换到 KIRO（本文做完后的事）

当本文 §A–§F 做完、环境在编辑盒上已跑通：

1. 本机下载 **KIRO**：`https://kiro.dev`
2. 登 AWS Builder ID 或你们组 IAM Identity Center（SSO）
3. 在 KIRO 装扩展 **`Open Remote - SSH`**（jeanp413，下载量 ~292K）
4. `Ctrl+Shift+P` → **Remote-SSH: Connect to Host** → **`oai-edit`**
   （KIRO 读的是同一份 `~/.ssh/config`，所以会无缝接管）
5. 在 KIRO 打开 `~/OAI_luuuuuu/`，继续写代码 / 跑 git / 跑实验

Cursor → KIRO 的切换不涉及 EC2、不涉及 S3、不涉及 GitHub，**本质只换了本机的 IDE 壳**。

---

## I. 切换到 SSM（Session Manager 零公网入站方案）

> **动机**：目前 SG 入站白名单着笔记本公网 IP（KT 动态，隔几天换一次；出差回家更频繁）。
> 每换一次都要去 SG 改、或者被锁在门外。SSM Session Manager 能让笔记本**走 AWS 的反向隧道**进 EC2，
> **完全不用开 EC2 的 22 端口到公网**；理论上连一整条入站规则都可以不要。
>
> **工作原理一句话**：EC2 内部跑的 `ssm-agent` 主动连出 AWS SSM 服务（443 出站）；
> 笔记本 `aws ssm start-session` 通过 AWS 控制面把流量转给那个 agent。两头都只走 443 出站，
> EC2 入站规则可以全关。攻击面从"扫 22 端口暴破 pem"缩成"攻破 AWS 账号（需偷 IAM 凭证 + 绕 MFA）"。

### 进度

| 阶段 | 状态 |
|------|------|
| §I.1 笔记本装 AWS CLI + `aws configure` | ✅ 已完成 2026-04-23 |
| §I.2 建 IAM Role `oai-ec2-ssm-role` + Instance Profile，挂到 `oai-luuuuuu1` | ⏳ |
| §I.3 验证 SSM 注册（`PingStatus: Online`） | ⏳ |
| §I.4 笔记本装 Session Manager Plugin | ⏳ |
| §I.5 试连 `aws ssm start-session` | ⏳ |
| §I.6 `~/.ssh/config` 加 `oai-luuuuuu1-ssm`（走 ProxyCommand） | ⏳ |
| §I.7 收尾：关 SG 22 入站（可选） | ⏳ |

### 资产登记（SSM 专属）

| 项目 | 值 |
|------|-----|
| IAM Role Name | `oai-ec2-ssm-role` |
| Role ARN | `arn:aws:iam::554615220681:role/oai-ec2-ssm-role` |
| 附加 Managed Policy | `arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore` |
| Instance Profile Name | `oai-ec2-ssm-role`（和 Role 同名，AWS 惯例） |
| 关联的 EC2 | `i-0b1afe76a52a91bbf`（`oai-luuuuuu1`） |
| 笔记本 Access Key 描述 | `laptop-oai-2026`（2026-04-23 建） |

### I.1 前置：笔记本具备 AWS CLI 控制面（已完成）

执行过的三条验证命令：

```powershell
aws --version                        # 应 aws-cli/2.x
aws sts get-caller-identity          # 应返回 user/luuuuuu + Account 554615220681
aws ec2 describe-instances --instance-ids i-0b1afe76a52a91bbf --query "Reservations[0].Instances[0].State.Name"   # 应 running
```

### I.2 建 IAM Role + Instance Profile，挂到 EC2（笔记本 PowerShell）

> AWS 规定 EC2 不能直接挂 Role，必须挂一层 **Instance Profile**（里面装着 Role）。
> 命令可重入：资源已存在会报 `EntityAlreadyExists`，忽略继续即可。

**一次性 5 条**（逐条贴，看一条结果再下一条）：

```powershell
# I.2.1 建 Role（允许 ec2 服务 assume）
aws iam create-role `
  --role-name oai-ec2-ssm-role `
  --assume-role-policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"ec2.amazonaws.com\"},\"Action\":\"sts:AssumeRole\"}]}" `
  --description "Allow EC2 to use AWS Systems Manager (Session Manager)"

# I.2.2 给 Role 附 SSM 托管策略（这一步才是真正赋予 SSM 权限）
aws iam attach-role-policy `
  --role-name oai-ec2-ssm-role `
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

# I.2.3 建 Instance Profile（和 Role 同名）
aws iam create-instance-profile --instance-profile-name oai-ec2-ssm-role

# I.2.4 把 Role 放进 Instance Profile
aws iam add-role-to-instance-profile `
  --instance-profile-name oai-ec2-ssm-role `
  --role-name oai-ec2-ssm-role

# I.2.5 把 Instance Profile 挂到 EC2（等 IAM 全球同步 15 秒再跑）
Start-Sleep -Seconds 15
aws ec2 associate-iam-instance-profile `
  --instance-id i-0b1afe76a52a91bbf `
  --iam-instance-profile Name=oai-ec2-ssm-role `
  --region ap-northeast-2
```

**成功标志**：I.2.5 返回的 JSON 里 `State: associating` 或 `associated`。

**如果 I.2.1 报 `EntityAlreadyExists`**：正常（之前已经从别处建过），继续 I.2.2。
**如果 I.2.5 报 `IncorrectState`**：等 30 秒再跑一次（EC2 在状态转换）。
**如果 I.2.5 报 `already has an associated instance profile`**：说明早就挂过，跳过。

### I.3 验证 SSM 注册（等 1–2 分钟）

`ssm-agent` 本身已经预装在 Ubuntu 22.04 官方 AMI 里（`snap list amazon-ssm-agent` 可验证），
只等 IAM Role 到位后第一次成功 call home 就会注册。

```powershell
# 等 90 秒（首次注册通常 30–120 秒）
Start-Sleep -Seconds 90

# 查询注册状态
aws ssm describe-instance-information `
  --filters "Key=InstanceIds,Values=i-0b1afe76a52a91bbf" `
  --region ap-northeast-2
```

**成功标志**：`InstanceInformationList` 里有一条记录，字段：
- `PingStatus: Online`
- `PlatformType: Linux`
- `AgentVersion: 3.x.x`

如果返回 `InstanceInformationList: []` 空列表：再等 60 秒重跑；仍空见 §I.8 故障排查。

### I.4 笔记本装 Session Manager Plugin（一次性）

Plugin 不是 AWS CLI 的一部分，需要**单独装**。

1. 下载：<https://s3.amazonaws.com/session-manager-downloads/plugin/latest/windows/SessionManagerPluginSetup.exe>
2. 双击安装（一路 Next）
3. **关掉所有 PowerShell / cmd 窗口**，重开一个新的（否则 PATH 没刷新认不到 plugin）
4. 验证：

   ```powershell
   session-manager-plugin
   ```

   应输出 `The Session Manager plugin was installed successfully. ...`。

### I.5 试连 SSM Session（不经过 SSH / pem）

```powershell
aws ssm start-session --target i-0b1afe76a52a91bbf --region ap-northeast-2
```

**成功表现**：几秒后直接进 EC2 的 shell，提示符变成 `sh-5.1$` 或类似。
敲 `whoami` 看到 `ssm-user`（SSM 默认用户，不是 ubuntu）。敲 `sudo -i` 可以切 root。

退出会话：`exit` 回笔记本 PowerShell。

> 这一步通过 → **SSM 通路 100% 验证**。入站 22 端口从此可以关。

### I.6 `~/.ssh/config` 走 SSM（KIRO / VSCode Remote-SSH 也能无缝走）

SSM 默认用户 `ssm-user` 不适合跑代码（无 sudo 的 home、无 ubuntu 那套环境）。
**真正日常用法是把 SSM 当"隧道"**，SSH 还是走 `ubuntu` 用户 + pem，但流量不走公网 22 端口，走 SSM 的 443 反向隧道。

笔记本 PowerShell 一次执行（**在之前已有的 `oai-luuuuuu1` 段后面追加一个新 Host**）：

```powershell
@"

# === OAI 编辑盒 via SSM (无需公网 22, 走 AWS SSM 隧道) ===
Host oai-luuuuuu1-ssm
    HostName i-0b1afe76a52a91bbf
    User ubuntu
    IdentityFile C:\Users\lu\.ssh\oai_luuuuuu.pem
    ProxyCommand "C:\Program Files\Amazon\AWSCLIV2\aws.exe" ssm start-session --target %h --document-name AWS-StartSSHSession --parameters "portNumber=%p" --region ap-northeast-2
    ServerAliveInterval 60
    ServerAliveCountMax 3

"@ | Add-Content -Path $HOME\.ssh\config -Encoding utf8

Get-Content $HOME\.ssh\config
```

> 关键字段：
> - `HostName i-0b1afe76a52a91bbf` ← **Instance ID**，不是公网 IP（SSM 按 ID 路由）
> - `ProxyCommand` ← 让 ssh 把 TCP 流量包进一个 SSM session
> - `"C:\Program Files\Amazon\AWSCLIV2\aws.exe"` ← Windows 上 `aws.exe` 的绝对路径，别用裸 `aws`（ssh 子进程里 PATH 可能不全）

**如果你的 aws.exe 不在默认路径**：PowerShell 里跑 `(Get-Command aws).Source` 看真路径，把上面 `ProxyCommand` 里那串替换掉。

**试连**：

```powershell
ssh oai-luuuuuu1-ssm
```

期望：和普通 SSH 一样进到 `ubuntu@ip-172-31-17-243:~$`，只是建立连接慢 2–3 秒（SSM 握手开销）。

### I.7 收尾：关 SG 22 入站（可选，但强烈建议）

**必须先把 §I.6 `ssh oai-luuuuuu1-ssm` 跑通再做这一步**，否则万一 SSM 出问题你会被锁在门外。

```powershell
# 删掉现有所有 22/TCP 入站规则（白名单 IP 可能不止一条）
aws ec2 describe-security-groups `
  --group-ids sg-0df84b6bac64599a7 `
  --region ap-northeast-2 `
  --query "SecurityGroups[0].IpPermissions[?FromPort==\`22\`]"

# 示例：撤掉之前白名单里的 IP（按上面输出里的 CidrIp 值替换）
aws ec2 revoke-security-group-ingress `
  --group-id sg-0df84b6bac64599a7 `
  --protocol tcp --port 22 --cidr 1.232.78.23/32 `
  --region ap-northeast-2
```

之后所有入站规则清空，EC2 公网 `nmap` 看就是全黑洞；只有 SSM 通道能进。

> 日后如果 SSM 出问题需要"降级回直连 SSH"：重开一条 22 白名单即可，详见附录"IP 变化应对"。

### I.8 故障排查

| 症状 | 原因 / 处置 |
|------|-----------|
| `describe-instance-information` 一直返回 `InstanceInformationList: []` | IAM Instance Profile 没关联成功，或 ssm-agent 没跑。SSH 进 EC2 跑 `sudo snap services amazon-ssm-agent` 看是否 `active`；`sudo snap restart amazon-ssm-agent` 重启它。|
| `start-session: TargetNotConnected` | Role/Profile 刚挂上 IAM 全球同步没完。等 2 分钟重跑。|
| `start-session: AccessDeniedException ... ssm:StartSession` | 当前 IAM user `luuuuuu` 没有 `ssm:StartSession` 权限。给 `Researcher` 组加托管策略 `AmazonSSMFullAccess`（最小化可做自定义策略，见 AWS 文档）。|
| `session-manager-plugin: not found` | Plugin 没装 / 没重开窗口。重做 §I.4。|
| `ssh oai-luuuuuu1-ssm` 卡在 `Establishing session...` | `ProxyCommand` 里 `aws.exe` 路径错了。跑 `(Get-Command aws).Source` 看真路径。|
| 临时想回老方式用 22 直连 | `ssh oai-luuuuuu1`（`~/.ssh/config` 里 §C 那条 `HostName 3.34.28.179` 的 Host），但 SG 必须放开 22 给你当前 IP。|

### I.9 为什么 SSM 比直连 SSH 安全（可跳过阅读）

| 攻击路径 | 直连 SSH | SSM |
|----------|----------|-----|
| 扫描公网 22 端口 | ✅ 端口存在，可暴破 pem | ❌ 端口不存在 |
| 偷 pem 文件 | ✅ 直接登 | ✅ 也能登（SSM 模式下 pem 还要用来走 SSH 层），但 IAM 凭证还得单独偷 |
| 偷笔记本 `~/.aws/credentials` | 无用 | ✅ 可 `aws ssm start-session` 进去 |
| 偷 + 绕 MFA | 没这要求 | ⚠️ 可通过 IAM 配强制要求 |
| 凭证泄露后作废时间 | 重签 Key Pair（操作繁琐） | IAM 控制台一键作废 Access Key（1 分钟全球生效） |

**结论**：SSM 把"长期公网攻击面（22 端口 + pem 不朽）"换成了"短期凭证面（Access Key 可秒作废 + 可选 MFA）"，整体攻击门槛显著提高。AWS 官方最佳实践。

---

## 附录：IP 变化应对（重要）

SG 现在只允许 `1.232.78.23/32`（笔记本今天的 KT 公网 IP）。下列情况 IP 会变：
- 回家 / 换咖啡店 / 换校园 WiFi / 手机热点
- 第二天早上路由器重启（KT 动态 IP 常见）
- 出差 / 出国

### 一行 PowerShell：加当前 IP 到 SG

```powershell
$ip = (Invoke-RestMethod ifconfig.me/ip).Trim()
aws ec2 authorize-security-group-ingress `
  --group-id sg-0df84b6bac64599a7 `
  --protocol tcp --port 22 `
  --cidr "$ip/32" `
  --region ap-northeast-2
Write-Host "已加入: $ip/32"
```

（要求笔记本装了 AWS CLI + `aws configure` 过；否则走控制台网页改）

### 控制台网页改

EC2 → 安全组 → `oai-luuuuuu1-sg` → 编辑入站规则 → 把旧 IP 改掉，加新 IP `/32`。

### 根治：切到 SSM

完整操作步骤见 **§I**。核心思路：

1. 给 EC2 挂 IAM Role + Instance Profile（含 `AmazonSSMManagedInstanceCore`）
2. 笔记本装 **Session Manager Plugin**
3. `~/.ssh/config` 改成 `ProxyCommand aws ssm start-session ...`
4. SG 22 入站可彻底关掉

切完 SSM 后：换地方 / 动态 IP 变化 / 出国全不影响。

---

## 附录：常见坑速查

| 症状 | 解决 |
|------|------|
| `ssh: Permission denied (publickey)` | 先 `chmod 400 ~/.ssh/oai_luuuuuu.pem`；确认 `User ubuntu` |
| `ssh: connect timed out` | SG 的 source IP 不是你当前 IP；`curl ifconfig.me` 看当前 IP，去 SG inbound 改 |
| `Permission denied (publickey)` on `git clone` | Deploy Key 没加 / 加错仓库 / 勾了 `~/.ssh/id_ed25519` 以外的 key；`ssh -T git@github.com` 测 |
| `Could not resolve host: github.com` | 实例没公网；检查 VPC/子网/Auto-assign public IP |
| `aws: command not found` | §E.6 没装 AWS CLI |
| `AccessDenied` on `aws s3 sync` | `aws configure` 没配 / region 不对 |
| 公网 IP 变了（stop/start） | 在 `~/.ssh/config` 里改 `HostName`，或用 `describe-instances --query ... PublicIpAddress` |
| 费用意外高 | 控制台 Billing → Budgets 设 $50/月 告警；确认不该 running 的已 stop |

---

## 附录：资源清单（一键清理用）

做完项目要彻底收尾时，按这个清单删：

```bash
# 假设 Instance ID = $EDIT_ID, SG = $SG_ID, Key = oai_luuuuuu
aws ec2 terminate-instances --instance-ids $EDIT_ID
aws ec2 wait instance-terminated --instance-ids $EDIT_ID
aws ec2 delete-security-group --group-id $SG_ID
aws ec2 delete-key-pair --key-name oai_luuuuuu
rm -f ~/.ssh/oai_luuuuuu.pem
# GitHub 上删 Deploy Key "oai-edit-2026"
# （如不保留数据）aws s3 rm s3://oai-luuuuuu --recursive && aws s3 rb s3://oai-luuuuuu
```

---

_对应 Manual 3（DCL）、`AWS_MIGRATION.md`、`QUICK_START_EC2.md`。_
_本文件不含密钥、IAM 秘密、PAT；可安全提交到 git。_
