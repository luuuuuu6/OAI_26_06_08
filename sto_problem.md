OAI 的 CP placement / IPC 时序 / AGC 让 H_LS 跟 H_GT 天生有一个 STO offset—我一直在找这个的实际原因，我们应该如何去找到根本性的原因？


现在跑这个,你就能 **量化 STO 实际行为**:

```bash
# 看 SNR=20 的 STO 数值(用 mmse1d 数据,样本最多)
python3 /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/eval_pdpR_nmse.py \
    --run-dir /tmp/snrsweep_pdpR/snr_20dB \
    --sto-correct per-frame --dump-sto

# 也看 legacy 那边(对照,看是不是同样的 STO)
python3 /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/eval_pdpR_nmse.py \
    --run-dir /tmp/snrsweep_legacy/snr_20dB \
    --sto-correct per-frame --dump-sto
```

输出会印一行:
```
per-frame STO (samples): mean=+1.234  std=0.567  median=+1.100  range=[+0.50, +2.50]
→ constant ≈ system offset; large std ≈ per-frame jitter (AGC/timing)
```

**判读 4 种 case**:

| 现象 | 指向 | 下一步 |
|---|---|---|
| `mean ≈ 整数,std < 0.5`(比如 mean=3.0, std=0.3)| **A. N_TA_offset 系统性偏移** | grep gnb.log `N_TA_offset` 看配置值 |
| `mean ≈ 0,std 大`(比如 mean=0.1, std=2.5)| **D. AGC/sample-level jitter** | 看 sysmon.csv 的 AGC 调整频率 |
| `mean ≈ 半整数`(比如 0.5, 1.5)| **B/C. CP / OFDM symbol position 歧义** | 比对 OAI vs Sionna 的 symbol indexing |
| `mean / std 都很大且 random` | 多个原因叠加 | 一个个排除 |

---

## 第 2 步:根据上一步结果,定向找根因

### 如果是 A(N_TA_offset)

```bash
grep "N_TA_offset" /tmp/snrsweep_pdpR/snr_20dB/gnb.log
# 比如看到: "RU 0 Setting N_TA_offset to 800 samples (UL Freq 3600120, N_RB 106, mu 1)"
# 800 samples × 30.72 MHz / sample rate ratio = ?
# 如果计算的 N_TA 跟 OAI 默认差,就是它
```

### 如果是 B/C(symbol position)

看 OAI 的 SRS 处理代码:
```bash
grep -n "srs_pdu->time_start_position\|symbol_offset.*srs\|first_symbol.*srs" \
    /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/openairinterface5g_whan/openair1/PHY/NR_TRANSPORT/srs_rx.c
```

然后看 sionna proxy 的 v8.py 里 GT 是怎么记录 symbol index 的:
```bash
grep -n "srs_symbol\|symbol_index\|gt_symbol" \
    /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/v8.py
```

两边对齐就能找到偏差几个符号(每符号 = 144 + 2048 = 2192 samples × N_TA scale)。

### 如果是 D(AGC jitter)

看 launch_all 启动 nr-softmodem 时的 AGC 配置:
```bash
grep -n "AGC\|agc\|NR_DIGITAL_AGC" \
    /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/launch_all_v8.sh
```

如果 `-agc` 在 wave AGC 模式,关掉再跑 sweep 看 STO std 是不是降。

---

## 第 3 步:**精确测量** — 用 zadoff-chu 序列对齐

终极根因如果上面都对不上,可以单独跑:
1. 让 UE 发一个**纯静态 SRS**(不动信道,纯接收链路)
2. 比较 OAI 的 H_LS 和 Sionna 注入的 H 之间的相位斜率
3. 那个斜率的 sample 偏移**精确**就是 STO

这一步要改测试代码,工作量较大,**先看第 1 步数据是不是已经够定位**。

---

## 我的建议

**今天**:跑第 1 步那个 `--dump-sto` 命令,**把 mean/std/range 数字贴回来**。30 秒跑完。

我看完数字,**立刻能告诉你**STO 是 A/B/C/D 哪一类,然后定向看对应文件。

不超过 1 小时能定位。这是我们手头工具能干的最大边界。