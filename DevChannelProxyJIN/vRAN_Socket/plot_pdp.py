import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import os
import glob
import sys

# ==========================================
# 1. 路径配置
# ==========================================
LOG_DIR = os.path.expanduser("~/OAI_luuuuuu/DevChannelProxyJIN/logs/latest/")

if os.path.exists("./gt_records"):
    GT_DIR = os.path.abspath("./gt_records")
elif os.path.exists("./G1C_MultiUE_MIMO_Channel_Proxy/gt_records"):
    GT_DIR = os.path.abspath("./G1C_MultiUE_MIMO_Channel_Proxy/gt_records")
else:
    print("❌ 找不到 gt_records 文件夹！")
    sys.exit()

def main():
    csv_files = sorted(glob.glob(os.path.join(LOG_DIR, "pusch_cfr_ue_*.csv")))
    if not csv_files:
        print(f"❌ 在 {LOG_DIR} 下找不到 CSV 文件。")
        return

    for csv_file in csv_files:
        base_name = os.path.basename(csv_file)
        print(f"\n📂 正在读取 OAI 数据: {base_name} ...")
        
        df = pd.read_csv(csv_file, header=None).dropna(axis=1, how='all')
        if df.empty:
            continue

        try:
            target_frame = int(df.iloc[0, 0])
            target_slot = int(df.iloc[0, 1])
            try:
                rnti = hex(int(str(df.iloc[0, 2]).strip(), 16))
            except:
                rnti = str(df.iloc[0, 2])
        except Exception:
            print("❌ 解析前三列失败。")
            continue

        # ==========================================
        # 1. 提取 OAI 频域信道 (2048 长度)
        # ==========================================
        data = df.iloc[0, 3:].values.astype(float)
        cfr_complex_oai = data[0::2] + 1j * data[1::2]
        
        # 将 OAI 频域数据从标准 FFT 顺序移动到以 0 为中心 (-1024 到 1023)
        cfr_oai_shifted = np.fft.fftshift(cfr_complex_oai)

        # 🌟 核心算法：提取实际分配的 PRB 带宽 Mask 🌟
        # 能量大于最大值 1% 的子载波才认为是实际分配了信号的
        pwr_freq_oai = np.abs(cfr_oai_shifted)**2
        threshold = np.max(pwr_freq_oai) * 0.01 
        valid_mask = pwr_freq_oai > threshold

        # ==========================================
        # 2. 匹配 Sionna Ground Truth
        # ==========================================
        npy_pattern = os.path.join(GT_DIR, f"gt_ul_ue0_f{target_frame}_s{target_slot}.npy")
        if not os.path.exists(npy_pattern):
            npy_pattern = os.path.join(GT_DIR, f"gt_ul_ue1_f{target_frame}_s{target_slot}.npy")
            
        if not os.path.exists(npy_pattern):
            print(f"⚠️ 找不到对应的真值 NPY。")
            continue
            
        gt_matrix = np.load(npy_pattern)
        true_cfr_sym = gt_matrix[0, 0, 0, :]
        cfr_true_shifted = np.fft.fftshift(true_cfr_sym)

        # 将 OAI 的有限带宽应用到 Sionna 真值上 
        cfr_true_masked_shifted = np.zeros_like(cfr_true_shifted)
        cfr_true_masked_shifted[valid_mask] = cfr_true_shifted[valid_mask]

        # ==========================================
        # 3. 统一转回时域计算 PDP (包含 FFT 窗口偏移处理)
        # ==========================================
        # 注意：转回时域前需要 ifftshift
        cir_time_oai = np.fft.ifft(np.fft.ifftshift(cfr_oai_shifted))
        cir_time_true_masked = np.fft.ifft(np.fft.ifftshift(cfr_true_masked_shifted))
        cir_time_true_unmasked = np.fft.ifft(np.fft.ifftshift(cfr_true_shifted)) # 保留原汁原味用于图3

        # 将时域零点移到中心，以便观察 Timing Advance 导致的负延迟 (提前量)
        cir_time_oai = np.fft.fftshift(cir_time_oai)
        cir_time_true_masked = np.fft.fftshift(cir_time_true_masked)
        cir_time_true_unmasked = np.fft.fftshift(cir_time_true_unmasked)

        # 归一化为 dB
        def to_db(cir):
            pwr = np.abs(cir)**2
            db = 10 * np.log10(pwr + 1e-12)
            return db - np.max(db)

        db_oai = to_db(cir_time_oai)
        db_true_masked = to_db(cir_time_true_masked)
        db_true_unmasked = to_db(cir_time_true_unmasked)

        # ==========================================
        # 4. 绘图：四图全景 (新增幅度验证)
        # ==========================================
        # 将原来的 3 行改为 4 行，同时把画布高度从 18 拉长到 24
        fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, figsize=(14, 24))
        
        # X 轴：以 1024 为零时延点。截取 -50 到 150 的区间来放大看。
        N = 2048
        center = N // 2
        view_start = center - 50
        view_end = center + 150
        x_axis_time = np.arange(-50, 150)

        # 【图 1】微观PDP对比 (原来的 ax1)
        ax1.plot(x_axis_time, db_oai[view_start:view_end], color='blue', linewidth=2, label='OAI Estimated (Band-limited)')
        ax1.plot(x_axis_time, db_true_masked[view_start:view_end], color='red', linestyle='--', linewidth=2.5, label='Sionna GT (Band-limited Masked)')
        ax1.set_title(f"Fair Micro-PDP Alignment (Both Band-limited) | RNTI: {rnti} | F:{target_frame} S:{target_slot}", fontsize=16, fontweight='bold')
        ax1.set_xlabel("Delay Samples (0 = FFT Window Start, <0 = Timing Advance)", fontsize=12)
        ax1.set_ylabel("Normalized Power (dB)", fontsize=12)
        ax1.set_ylim(-30, 5)
        ax1.grid(True, linestyle='--', alpha=0.7)
        ax1.legend()

        # ==================== 核心数据计算 ====================
        valid_indices = np.where(valid_mask)[0] - center # 映射到 -1024 ~ 1023
        
        # 1. 计算频域幅度 (用于图2)
        mag_gt_db = 10 * np.log10(np.abs(cfr_true_shifted[valid_mask])**2 + 1e-12)
        mag_oai_db = 10 * np.log10(np.abs(cfr_oai_shifted[valid_mask])**2 + 1e-12)
        
        # 2. 计算频域相位 (用于图3) - 先解卷绕再Mask
        phases_oai_full = np.unwrap(np.angle(cfr_oai_shifted))
        phases_gt_full = np.unwrap(np.angle(cfr_true_shifted))
        valid_phases_oai = phases_oai_full[valid_mask]
        valid_phases_gt = phases_gt_full[valid_mask]
        
        # 相位起始点对齐
        valid_phases_oai -= valid_phases_oai[0]
        valid_phases_gt -= valid_phases_gt[0]
        # ======================================================

        # 【图 2】新增：频域幅度 (放在相位图上方，直观展示深衰落)
        ax2.plot(valid_indices, mag_oai_db, color='blue', label="OAI Magnitude (dB)")
        ax2.plot(valid_indices, mag_gt_db, color='red', linestyle='--', label="Sionna Magnitude (dB)")
        ax2.set_title("Frequency Domain Magnitude on Allocated PRBs (Deep Fade / Spectral Null)", fontsize=16, fontweight='bold')
        ax2.set_xlabel("Subcarrier Index (Relative to DC)", fontsize=12)
        ax2.set_ylabel("Magnitude (dB)", fontsize=12)
        ax2.grid(True, linestyle='--', alpha=0.7)
        ax2.legend()

        # 【图 3】频域相位 (原来的 ax2)
        ax3.plot(valid_indices, valid_phases_oai, color='blue', label="OAI Phase (Unwrapped)")
        ax3.plot(valid_indices, valid_phases_gt, color='red', linestyle='--', label="Sionna Phase (Unwrapped)")
        ax3.set_title("Frequency Domain Phase on Allocated PRBs (Phase Inversion at Null)", fontsize=16, fontweight='bold')
        ax3.set_xlabel("Subcarrier Index (Relative to DC)", fontsize=12)
        ax3.set_ylabel("Phase (Radians)", fontsize=12)
        ax3.grid(True, linestyle='--', alpha=0.7)
        ax3.legend()

        # 【图 4】Sionna 绝对物理真值 (原来的 ax3)
        y_valid = db_true_unmasked[view_start:view_end]
        valid_taps = y_valid > -35
        x_valid = x_axis_time[valid_taps]
        y_taps = y_valid[valid_taps]

        markerline, stemlines, baseline = ax4.stem(x_valid, y_taps, linefmt='red', markerfmt='ro', basefmt=' ', bottom=-40)
        plt.setp(stemlines, 'linewidth', 2.5)
        plt.setp(markerline, 'markersize', 8)
        
        # 💡 这里我帮你把标题里的 🔍 表情删掉了，这样你的终端就不会再报 Glyph missing 的警告了
        ax4.set_title("ZOOM IN: Sionna Absolute Physical Multipath (Infinite Bandwidth)", fontsize=16, fontweight='bold', color='darkred')
        ax4.set_xlabel("Delay Samples", fontsize=12)
        ax4.set_ylabel("Normalized Power (dB)", fontsize=12)
        ax4.set_ylim(-40, 5)
        ax4.set_xlim(-50, 150)
        ax4.grid(True, linestyle='--', alpha=0.7)
        ax4.fill_between([-50, 150], -40, 5, color='red', alpha=0.05)

        plt.tight_layout()
        save_path = f"Fair_Validation_{rnti}_F{target_frame}_S{target_slot}.png"
        plt.savefig(save_path, dpi=300)
        print(f"🎉 成功！报告已保存至: {save_path}\n")
        
if __name__ == "__main__":
    main()