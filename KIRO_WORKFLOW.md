# KIRO_WORKFLOW.md — 笔记本直连 AWS 用 KIRO 的完整流程

> **目标形态**：笔记本跑 KIRO（IDE 壳）→ Remote-SSH → AWS EC2 编辑盒 `oai-luuuuuu1`
> 上写代码 / git / 跑分析；需要 GPU 时再 Start 第二台 GPU 盒。**dclserver78 完全不参与**。
>
> **本文件定位**：`SETUP_MANUAL3.md` 是从零搭建的详细手册；本文件是**打通后每天要用的一页纸**。
> 开工下班抄这里，故障查这里，不用再翻大手册。

---

## 架构速览

```
  你的笔记本 (Windows, KIRO IDE)
       │  SSH (Remote-SSH 扩展透明接管)
       ▼
  ┌───────────────────────────────────┐
  │ AWS EC2 编辑盒  oai-luuuuuu1       │  ← c7i.2xlarge, $0.36/hr
  │ i-0b1afe76a52a91bbf               │     ~$4/月 stopped (50 GB EBS)
  │ ~/OAI_luuuuuu/                    │
  └───────────┬───────────────────────┘
              │ git                    │ aws s3
              ▼                        ▼
           GitHub                 S3: oai-luuuuuu
              ▲
              │ git pull
  ┌───────────┴───────────────────────┐
  │ AWS EC2 GPU 盒  oai-luuuuuu-gpu    │  ← 按需 Start, ~$2/hr
  │ (Sionna / CUDA 仿真专用，延后建)   │     用完立刻 Stop
  └───────────────────────────────────┘
```

---

## 关键资产一览（复制粘贴用）

| 项目 | 值 |
|------|-----|
| 编辑盒 Instance ID | `i-0b1afe76a52a91bbf` |
| 编辑盒初始公网 IP | `3.34.28.179`（**Stop/Start 后会变**） |
| 编辑盒 Security Group | `sg-0df84b6bac64599a7`（`oai-luuuuuu1-sg`） |
| Region | `ap-northeast-2` |
| Key Pair | `oai_luuuuuu`（ED25519） |
| 笔记本 pem 路径 | `C:\Users\11459\.ssh\oai_luuuuuu.pem` |
| S3 桶 | `oai-luuuuuu`（首尔） |
| GitHub 仓库 | `git@github.com:luuuuuu6/OAI_luuuuuu.git` |
| IAM 用户 | `luuuuuu`（Account `554615220681`） |
| 已放行的笔记本 IP | `1.232.78.23/32`（家）、`165.132.121.19/32`（实验室） |

---

## Phase 1：一次性打通（40–60 分钟，只做一次）

### ☐ 1.1 笔记本 `~/.ssh/config` 加主机段

笔记本 **PowerShell**（如果已加过可跳过）：

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

### ☐ 1.2 首次 SSH 联通性测试

笔记本 PowerShell：

```powershell
ssh oai-luuuuuu1
# 第一次问 yes/no → yes
# 成功提示: ubuntu@ip-172-31-17-243:~$
```

EC2 里快速检查：

```bash
nproc              # 应 16
free -h            # 应 ~32 GiB
df -h /            # 应 ~50 GB
```

> **这一步必须先通**，KIRO 的 Remote-SSH 底层就是这条 SSH，ssh 不通 KIRO 也连不上。

### ☐ 1.3 EC2 装基础工具

```bash
sudo apt update
sudo apt install -y git tmux htop unzip curl
```

### ☐ 1.4 EC2 生成自己的 SSH key + 加 GitHub Deploy Key

```bash
ssh-keygen -t ed25519 -C "oai-edit-2026" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub       # 复制整行 ssh-ed25519 AAAA...
```

浏览器打开 `https://github.com/luuuuuu6/OAI_luuuuuu/settings/keys/new`：
- Title: `oai-edit-2026`
- Key: 粘贴上面那行
- Allow write access: **不勾**
- Add key

验证：

```bash
ssh -T git@github.com
# 应看到: Hi luuuuuu6/OAI_luuuuuu! You've successfully authenticated...
```

### ☐ 1.5 Git 用户信息 + clone 仓库

```bash
git config --global user.name "luuuuuu6"
git config --global user.email "你的邮箱"

cd ~
git clone git@github.com:luuuuuu6/OAI_luuuuuu.git
cd OAI_luuuuuu
git submodule update --init --recursive
```

### ☐ 1.6 装 AWS CLI + 凭证

```bash
cd ~
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip -q awscliv2.zip && sudo ./aws/install
rm -rf aws awscliv2.zip
aws --version

aws configure
#   Access Key ID:       <luuuuuu 的 access key>
#   Secret Access Key:   <同上>
#   Default region:      ap-northeast-2
#   Default output:      json

aws configure set default.s3.max_concurrent_requests 20
aws sts get-caller-identity    # 应返回 Account 554615220681 / user luuuuuu
```

### ☐ 1.7 S3 smoke test（拉一块数据 + 测分析脚本）

```bash
cd ~/OAI_luuuuuu
bash bootstrap_ec2.sh           # 建 venv + 装依赖（CPU 版本，约 5-10 min）

# 拉最新一次 Q4 sweep
LATEST=$(aws s3 ls s3://oai-luuuuuu/runs/ | awk '{print $2}' | grep '^q4_sweep_' | tail -1 | tr -d '/')
echo "LATEST=$LATEST"
aws s3 sync s3://oai-luuuuuu/runs/$LATEST/ ~/OAI_luuuuuu/DevChannelProxyJIN/logs/$LATEST/

cd ~/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket
source venv_lu/bin/activate
python compare_nmse.py --help   # 能出 help 就算通了
```

### ☐ 1.8 笔记本装 KIRO + Remote-SSH 扩展

1. 浏览器打开 `https://kiro.dev` → 下载 Windows 版 → 安装
2. 启动 KIRO → 登录 **AWS Builder ID**（邮箱注册即可，免费）
3. 左侧 Extensions (`Ctrl+Shift+X`) → 搜 **`Open Remote - SSH`**（作者 **jeanp413**，不是微软官方） → Install

   > 为啥要 jeanp413 这个：KIRO 底层是 Code-OSS，微软官方 `Remote-SSH` 有 License 限制跑不起来，jeanp413 版是社区专门解决这个的。

### ☐ 1.9 KIRO 连上编辑盒

1. `Ctrl+Shift+P` → 输入 `Remote-SSH: Connect to Host`
2. 下拉里会自动出现 **`oai-luuuuuu1`**（读的就是 1.1 写的 config）
3. 点它 → 新窗口 → 等 30 秒装 VSCode server → 左下角出现绿色 `SSH: oai-luuuuuu1` = 成功
4. File → Open Folder → 输 `/home/ubuntu/OAI_luuuuuu` → OK
5. `Ctrl+~` 开终端 → 应该直接是 `ubuntu@ip-...:~/OAI_luuuuuu$`

**打完 ☐ 1.1–1.9 所有勾，Phase 1 完成，以后只走 Phase 2。**

---

## Phase 2：日常使用（每天重复）

### 2.1 开工一键（推荐脚本，见 §2.5）

粗略步骤：

1. **启动 EC2**（如果昨晚 Stop 了）
2. **取新公网 IP**（Stop/Start 后 IP 会变）
3. **更新笔记本 `~/.ssh/config` 的 HostName**
4. **如果换网络了**：把当前笔记本出口 IP 加到 SG
5. **打开 KIRO** → `Remote-SSH: Connect to Recent` → `oai-luuuuuu1`

### 2.2 日常写代码 / 分析

在 KIRO 里（左下角是绿色 `SSH: oai-luuuuuu1`）：

- 编辑文件 → 直接存到 EC2 硬盘
- `Ctrl+~` 开终端 → 所有命令都在 EC2 上跑
- Source Control 面板 → commit / push 走 EC2 的 Deploy Key 推到 GitHub

**拉 S3 新数据**（KIRO 终端里）：

```bash
LATEST=$(aws s3 ls s3://oai-luuuuuu/runs/ | awk '{print $2}' | grep '^q4_sweep_' | tail -1 | tr -d '/')
aws s3 sync s3://oai-luuuuuu/runs/$LATEST/ ~/OAI_luuuuuu/DevChannelProxyJIN/logs/$LATEST/
```

**跑分析**：

```bash
cd ~/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket
source venv_lu/bin/activate
python compare_nmse.py --run-dir ../logs/q4_sweep_xxxx/
```

### 2.3 要跑 Sionna 大仿真 → GPU 盒（见 `SETUP_MANUAL3.md §G`）

**前提**：已按 §G 建好 GPU 盒 + 存了 `oai-luuuuuu-gpu-ready-v1` AMI。

```bash
# 编辑盒里（KIRO 终端）: push 代码
git add -A && git commit -m "xxx" && git push

# 笔记本 PowerShell: 启动 GPU 盒
$GPU_ID = "i-xxxxxxxxxxxxxxxxx"    # TODO: GPU 盒建好后填
aws ec2 start-instances --instance-ids $GPU_ID --region ap-northeast-2
aws ec2 wait instance-running --instance-ids $GPU_ID --region ap-northeast-2

# 笔记本 SSH 直连 GPU 盒（或 KIRO 再开 Remote-SSH 窗口）
ssh oai-luuuuuu-gpu
cd ~/OAI_luuuuuu/DevChannelProxyJIN
git pull

tmux new -s sweep
cd vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy
sudo bash launch_all.sh -n 1 -v v4 -bs 280
# Ctrl-b d 脱离（可以安心关笔记本睡觉）
```

跑完回来：

```bash
ssh oai-luuuuuu-gpu
tmux attach -t sweep                                   # 看结果
RUN_ID=$(ls -1dt ~/OAI_luuuuuu/DevChannelProxyJIN/logs/q4_sweep_* | head -1 | xargs basename)
aws s3 sync ~/OAI_luuuuuu/DevChannelProxyJIN/logs/$RUN_ID/ s3://oai-luuuuuu/runs/$RUN_ID/
sudo shutdown -h now                                   # 必须立刻 Stop，GPU 巨贵
```

### 2.4 下班一键

KIRO 里 `Ctrl+Shift+P` → `Remote-SSH: Close Remote Connection`，然后笔记本 PowerShell：

```powershell
aws ec2 stop-instances --instance-ids i-0b1afe76a52a91bbf --region ap-northeast-2
```

或者更简单：在 KIRO 终端里直接：

```bash
sudo shutdown -h now   # EC2 自己进入 stopped，本地 SSH 会断
```

**费用对比**（编辑盒 c7i.2xlarge + 50 GB gp3）：

| 状态 | 每天 |
|------|------|
| Running 24h | ≈ $8.6 |
| Stopped（只 EBS） | ≈ $0.13（$4/月） |
| Terminated | $0（但环境全删，**不要这么做**） |

### 2.5 省事脚本（强烈推荐做一次）

笔记本桌面新建两个 `.ps1`。开工下班双击就完事。

#### `start-edit.ps1`（开工一键）

```powershell
$ID = "i-0b1afe76a52a91bbf"
$SG = "sg-0df84b6bac64599a7"
$REGION = "ap-northeast-2"
$CFG = "$HOME\.ssh\config"

Write-Host "[1/4] 启动 EC2..." -ForegroundColor Cyan
aws ec2 start-instances --instance-ids $ID --region $REGION | Out-Null
aws ec2 wait instance-running --instance-ids $ID --region $REGION

Write-Host "[2/4] 取公网 IP..." -ForegroundColor Cyan
$NEW_IP = aws ec2 describe-instances --instance-ids $ID --region $REGION `
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text
Write-Host "      新 IP: $NEW_IP"

Write-Host "[3/4] 更新 ~/.ssh/config 的 HostName..." -ForegroundColor Cyan
$lines = Get-Content $CFG
$inBlock = $false
$updated = foreach ($line in $lines) {
    if ($line -match '^\s*Host\s+oai-luuuuuu1\s*$') { $inBlock = $true;  $line; continue }
    if ($inBlock -and $line -match '^\s*Host\s+\S+') { $inBlock = $false }
    if ($inBlock -and $line -match '^\s*HostName\s+') { "    HostName $NEW_IP" } else { $line }
}
Set-Content -Path $CFG -Value $updated -Encoding utf8
Write-Host "      已把 oai-luuuuuu1 的 HostName 改为 $NEW_IP"

Write-Host "[4/4] 放行当前笔记本出口 IP..." -ForegroundColor Cyan
$MY_IP = (Invoke-RestMethod ifconfig.me/ip).Trim()
$desc = "laptop-$(Get-Date -Format yyyyMMdd)"
aws ec2 authorize-security-group-ingress --group-id $SG `
  --protocol tcp --port 22 --cidr "$MY_IP/32" `
  --region $REGION 2>$null
Write-Host "      当前 IP: $MY_IP/32（重复加会报 Duplicate，可忽略）"

Write-Host ""
Write-Host "就绪！在 KIRO 里按 Ctrl+Shift+P → Remote-SSH: Connect to Host → oai-luuuuuu1" -ForegroundColor Green
```

#### `stop-edit.ps1`（下班一键）

```powershell
$ID = "i-0b1afe76a52a91bbf"
$REGION = "ap-northeast-2"
aws ec2 stop-instances --instance-ids $ID --region $REGION | Out-Null
Write-Host "编辑盒已 Stop，今晚只计 EBS ≈ $0.13/天" -ForegroundColor Green
```

**第一次运行前**：PowerShell 以管理员身份跑一次 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`，否则会报脚本不能执行。

---

## 特殊场景

### A. 今天换了网络（家 → 学校 / 咖啡店 / 手机热点）

- 跑 `start-edit.ps1`，第 4 步会自动把新 IP 加到 SG
- 或手动：

```powershell
$ip = (Invoke-RestMethod ifconfig.me/ip).Trim()
aws ec2 authorize-security-group-ingress `
  --group-id sg-0df84b6bac64599a7 `
  --protocol tcp --port 22 --cidr "$ip/32" `
  --region ap-northeast-2
```

### B. SG 规则堆太多想清理

```powershell
# 先看当前有哪些 /32
aws ec2 describe-security-groups --group-ids sg-0df84b6bac64599a7 `
  --region ap-northeast-2 `
  --query 'SecurityGroups[0].IpPermissions[0].IpRanges[*].CidrIp' --output table

# 删一条
aws ec2 revoke-security-group-ingress `
  --group-id sg-0df84b6bac64599a7 `
  --protocol tcp --port 22 --cidr 1.2.3.4/32 `
  --region ap-northeast-2
```

### C. 我人不在笔记本边上，就是想把 EC2 关了省钱

任何装了 AWS CLI 的地方（包括手机 termux、dclserver78）：

```bash
aws ec2 stop-instances --instance-ids i-0b1afe76a52a91bbf --region ap-northeast-2
```

---

## 故障速查

| 症状 | 最可能原因 | 处理 |
|------|----------|------|
| KIRO 连接卡在 `Setting up SSH Host` | EC2 还没 ready | 等 30 秒，或回 PowerShell `ssh oai-luuuuuu1` 验证 SSH 本身通不通 |
| `ssh: connect to host ... port 22: Connection timed out` | SG 不含当前笔记本出口 IP | 跑 `start-edit.ps1` 第 4 步；或 `curl ifconfig.me` 查当前 IP 去控制台加 |
| `ssh: Could not resolve hostname ...` | config 里 HostName 是旧 IP（Stop/Start 后 IP 变了） | `start-edit.ps1` 会自动改；或手动编辑 `C:\Users\11459\.ssh\config` |
| `Permission denied (publickey)` | pem 权限没收紧 / IdentityFile 路径错 | 笔记本跑 `icacls C:\Users\11459\.ssh\oai_luuuuuu.pem`，只能你自己 R |
| KIRO 里 `git push` 报 publickey | EC2 的 Deploy Key 没加 / 加错仓库 | EC2 里跑 `ssh -T git@github.com`；不通就回 §1.4 重加 |
| `aws: command not found` | EC2 没装 AWS CLI | 回 §1.6 |
| `aws s3 sync` 报 `AccessDenied` | `aws configure` 凭证错 / region 错 | EC2 里重跑 `aws configure`，确认 region = `ap-northeast-2` |
| 半夜收到账单警报 | 忘 Stop 了 | 马上跑 `stop-edit.ps1`；设 AWS Budget 每月 $50 告警 |
| `ssh: Host key verification failed` | IP 变过，known_hosts 里旧指纹 | `ssh-keygen -R 3.34.28.179`（换成你之前的 IP），重连 |
| VSCode server 装一半卡住 | EC2 磁盘满 / 网络抖 | EC2 里 `df -h /` 看空间；`rm -rf ~/.vscode-server` 后重连 |

---

## 下一步（做完本文档就能直接上手）

- [ ] `SETUP_MANUAL3.md §G` 开始推进 GPU 盒（Service Quota 申请 → 建 g6e.xlarge → §G.5 Sionna Proxy 复刻 → 存 Custom AMI）
- [ ] 在笔记本桌面放好 `start-edit.ps1` / `stop-edit.ps1`
- [ ] AWS Console → Billing → Budgets 设 $50/月告警
- [ ] 考虑切 SSM（`SETUP_MANUAL3.md §附录 → 根治：切到 SSM`），一次性 30 分钟，之后不用再管 SG

---

_本文件和 `SETUP_MANUAL3.md` / `manual.md` 保持一致；任何命令冲突以此为准（本文是每日实战版）。_
_不含任何密钥、IAM Secret、PAT，可安全提交 git。_
