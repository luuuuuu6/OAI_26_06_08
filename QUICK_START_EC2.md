# QUICK_START_EC2.md — 未来的我，看这一页

> 当你某天（毕业后 / 新机器上 / 忘了怎么搞）要重启 **OAI_luuuuuu** 项目时，照这份
> 1 页纸从零到能跑。详细背景见 `AWS_MIGRATION.md`。

---

## 0. 你需要先有什么

- AWS 账号 + 一个 IAM 用户的 `AccessKey/SecretKey`
  （当前使用：账号 `554615220681`，用户 `luuuuuu`，区域 `ap-northeast-2` 首尔）
- 一个 SSH key pair (`.pem`) 关联到 EC2

---

## 1. 开一台 EC2（按用途选）

| 用途 | 机型 | 按需价 | 备注 |
|---|---|---|---|
| 纯分析 / 画图 (`compare_nmse.py`, sweep 后处理) | `c6i.4xlarge` (16 vCPU, 32 GB) | ~$0.68/hr | 最常用 |
| Sionna 光追 / TF 仿真 (`v4.py`, `ablation_pdp_experiment.py`) | `g5.xlarge` (1× A10G 24 GB) | ~$1.01/hr | 要 GPU |
| 编译 OAI gNB/UE + 跑 Channel Proxy 全链路 | `c6i.8xlarge` (32 vCPU, 64 GB) | ~$1.36/hr | 编译慢，算力密集 |

- **AMI**: Ubuntu 22.04 LTS (`ami-0c9c942bd7bf113a2` ap-northeast-2 的最近版本，建议用 AWS 控制台选最新)
- **EBS**: gp3 ≥ 100 GB
- **Security group**: 只开 `22/tcp`，source 限制为自己 IP
- **Elastic IP**: 可选，想固定公网 IP 时绑定

---

## 2. SSH 进去 + 一键 bootstrap

```bash
ssh -i ~/.ssh/<你的key>.pem ubuntu@<EC2公网IP>

# 在 EC2 上：
curl -O https://raw.githubusercontent.com/luuuuuu6/OAI_luuuuuu/main/bootstrap_ec2.sh
# 基础（分析）
bash bootstrap_ec2.sh
# 或带 Sionna 仿真
# bash bootstrap_ec2.sh --with-sim
# 或带 OAI 编译
# bash bootstrap_ec2.sh --with-oai --with-sim
```

脚本会：

1. `apt install` 基础工具
2. 让你 `aws configure` 输凭证
3. `git clone --recurse-submodules` 拉代码
4. 建 `venv_lu`，装 `requirements.txt`
5. （可选）装 `requirements-sim.txt` + 检测 GPU
6. （可选）跑 OAI 的 `build_oai -I` 装编译依赖

---

## 3. 从 S3 拉你要的数据

S3 布局（由 Phase 1 一次性迁移上去）：

```
s3://oai-luuuuuu/
├── raw/                            # 原始/基础数据（~1.5 GB）
│   ├── cfr/                        # SRS / true CFR .npy
│   ├── cfr_results/                # 可视化图
│   └── saved_rays_data/            # 光追 ray 参数
├── runs/                           # 实验 log（~5.4 GB）
│   ├── 20260411_* ~ 20260422_*/    # 单次 gNB+UE+proxy 运行
│   └── q4_sweep_*/                 # Q4 SNR 扫参 (含 sionna_gt ground truth)
└── figures/
    └── data_out/                   # 论文/报告用图
```

**按需拉**（别一次全拉）：

```bash
# 分析 cfr 数据
aws s3 sync s3://oai-luuuuuu/raw/cfr/ \
            ~/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/cfr/

# 某一次 Q4 sweep 的结果
aws s3 sync s3://oai-luuuuuu/runs/q4_sweep_20260422_145841/ \
            ~/OAI_luuuuuu/DevChannelProxyJIN/logs/q4_sweep_20260422_145841/

# 跑 Sionna 时的 ray 参数
aws s3 sync s3://oai-luuuuuu/raw/saved_rays_data/ \
            ~/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/saved_rays_data/
```

---

## 4. 跑实验（用 tmux 防 SSH 掉线）

```bash
tmux new -s run

cd ~/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket
source venv_lu/bin/activate

# 分析 —— 举例：
python compare_nmse.py --sweep-dir ../logs/q4_sweep_20260422_145841

# OAI + Channel Proxy 全链路（需要 --with-oai 模式的机器）
cd G1C_MultiUE_MIMO_Channel_Proxy
sudo bash launch_all.sh -n 1 -ga 2 1 -ua 2 1        # 1 UE 2x1 MIMO

# 掉线后重新进：tmux a -t run
# 脱离：Ctrl+B, 然后按 D
```

---

## 5. 结果回传 + 关机

```bash
# 回传最新 run
RUN_ID=$(ls -td ~/OAI_luuuuuu/DevChannelProxyJIN/logs/*/ | head -1 | xargs basename)
aws s3 sync ~/OAI_luuuuuu/DevChannelProxyJIN/logs/$RUN_ID/ \
            s3://oai-luuuuuu/runs/$RUN_ID/ \
            --follow-symlinks
# 图
aws s3 sync ~/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/data_out/ \
            s3://oai-luuuuuu/figures/data_out/

# ⚠️ 下班前务必 stop（不是 terminate，EBS 留着下次继续用）
sudo shutdown -h now      # 机器关机，EC2 会进 stopped 状态
# 或在本地：
# aws ec2 stop-instances --instance-ids i-xxxxxxxx
```

**停机（stopped）只收 EBS 盘钱**（~$8/月/100GB），CPU/GPU 费用归零。

---

## 6. 常见坑

| 症状 | 解决 |
|---|---|
| `git clone` 后 `openairinterface5g_whan/` 空 | `git submodule update --init --recursive` |
| `import sionna` 报 CUDA 错 | 用 `tensorflow[and-cuda]==2.17.0`（requirements-sim.txt 已固定） |
| `aws s3 sync` 很慢 | bootstrap 脚本已帮你 `set max_concurrent_requests 20` |
| SSH 掉线实验死了 | 永远用 `tmux new -s <name>` |
| EC2 账单爆炸 | Billing → Budgets 设 $50/月 告警；下班前 stop |
| 找不到公网 IP | Stopped 再 Start 后 IP 会变，用 Elastic IP 固定 |

---

## 7. 真的彻底不用了

```bash
# 1) 最后一次把本地没回传的东西 sync 上 S3
# 2) Terminate EC2（不是 stop，彻底删除）
aws ec2 terminate-instances --instance-ids i-xxxxxxxx
# 3) 删 EBS 快照（如果有）
# 4) 数据还在 S3，论文随时能查
```

---

_这份 runbook 对应的详细背景 + 迁移理由 → `AWS_MIGRATION.md`_
