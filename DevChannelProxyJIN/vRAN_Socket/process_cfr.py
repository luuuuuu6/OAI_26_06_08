import numpy as np
import struct
import matplotlib
matplotlib.use('Agg')  # 服务器无 GUI 环境必备
import matplotlib.pyplot as plt
import os
import glob

def scan_bin_and_extract_important(filename):
    """
    极速二进制扫描 (实时并发安全版)
    """
    header_fmt = "<IIHHHHI"
    header_size = struct.calcsize(header_fmt)
    
    important_slots = []
    last_layer_count = -1
    slot_index = 0
    last_valid_slot_pos = -1 # 记录上一个完整无误的 Slot 起始位置

    if not os.path.exists(filename):
        print(f"错误: 找不到文件 {filename}")
        return []

    with open(filename, "rb") as f:
        while True:
            # 记录当前指针位置
            current_pos = f.tell()
            header_bytes = f.read(header_size)
            
            # 1. 如果连 Header 都读不全 (OAI 刚写了一半，或到了文件末尾)，安全退出
            if not header_bytes or len(header_bytes) < header_size:
                break
            
            meta = struct.unpack(header_fmt, header_bytes)
            current_layers = meta[3]
            rx_ants = meta[4]
            ofdm_size = meta[5]
            
            payload_size = current_layers * rx_ants * ofdm_size * 4
            
            # 2. 检查文件剩余大小是否足够装下当前的 Payload
            f.seek(0, 2) # 跳到文件末端
            eof_pos = f.tell()
            f.seek(current_pos + header_size, 0) # 跳回刚才 Header 之后的位置
            
            if (eof_pos - f.tell()) < payload_size:
                break # 剩余数据不够一个完整的 Payload，说明这是个没写完的半成品，直接结束
                
            # 恭喜，这是一个完整的 Slot！保存其位置
            last_valid_slot_pos = current_pos
            
            if last_layer_count == -1:
                last_layer_count = current_layers

            # ==== 智能打标与筛选逻辑 ====
            is_important = False
            tag = ""

            if current_layers != last_layer_count:
                is_important, tag = True, "MIMO_Change"
                last_layer_count = current_layers
            elif current_layers > 1 and slot_index % 10 == 0:
                is_important, tag = True, "MIMO_Active"
            elif slot_index % 200 == 0:
                is_important, tag = True, "Trend"

            if is_important:
                data_bytes = f.read(payload_size)
                raw_data = np.frombuffer(data_bytes, dtype=np.int16)
                complex_data = raw_data[0::2] + 1j * raw_data[1::2]
                matrix = complex_data.reshape((current_layers, rx_ants, ofdm_size))
                
                important_slots.append({
                    "meta": meta,
                    "matrix": matrix,
                    "tag": tag,
                    "index": slot_index
                })
            else:
                f.seek(payload_size, 1)

            slot_index += 1

        # ==== 循环结束，确保补充读取最后一个【完整有效】的 Slot (Latest) ====
        if last_valid_slot_pos != -1:
            f.seek(last_valid_slot_pos)
            header_bytes = f.read(header_size)
            meta = struct.unpack(header_fmt, header_bytes)
            payload_size = meta[3] * meta[4] * meta[5] * 4
            data_bytes = f.read(payload_size)
            
            raw_data = np.frombuffer(data_bytes, dtype=np.int16)
            complex_data = raw_data[0::2] + 1j * raw_data[1::2]
            matrix = complex_data.reshape((meta[3], meta[4], meta[5]))
            
            if important_slots and important_slots[-1]["index"] == (slot_index - 1):
                important_slots[-1]["tag"] += "_Latest"
            else:
                important_slots.append({
                    "meta": meta,
                    "matrix": matrix,
                    "tag": "Latest",
                    "index": slot_index - 1
                })

    return important_slots

def visualize_cfr(slot_data, save_dir="cfr_results"):
    frame, slot, rnti, layers, rx_ants, ofdm_size, _ = slot_data["meta"]
    matrix = slot_data["matrix"]
    tag = slot_data["tag"]

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    # 自动寻找有效频率边界，裁剪无用底噪
    avg_mag = np.mean(np.abs(matrix), axis=(0, 1))
    active_indices = np.where(20 * np.log10(avg_mag + 1e-6) > -100)[0]
    max_idx = min(active_indices[-1] + 12, ofdm_size) if len(active_indices) > 0 else ofdm_size

    est_prbs = len(active_indices) // 12

    plt.figure(figsize=(10, 5))
    for l in range(layers):
        for r in range(rx_ants):
            mag_db = 20 * np.log10(np.abs(matrix[l, r, :max_idx]) + 1e-6)
            plt.plot(mag_db, label=f"L{l}-Rx{r}")

    plt.title(f"[{tag}] F{frame} S{slot} | RNTI:{hex(rnti)} | Layers:{layers} | Est.PRBs:~{est_prbs}")
    plt.xlabel("Subcarrier Index (Zoomed)")
    plt.ylabel("Magnitude (dB)")
    plt.grid(True, linestyle=':', alpha=0.7)
    
    # 根据是否有信号动态调整 Y 轴，让曲线更清晰
    valid_db = avg_mag[active_indices]
    if len(valid_db) > 0:
        plt.ylim([np.min(20 * np.log10(valid_db + 1e-6)) - 5, np.max(20 * np.log10(valid_db + 1e-6)) + 5])

    plt.legend(loc='upper right')
    
    save_path = os.path.join(save_dir, f"{tag}_f{frame}_s{slot}_{hex(rnti)}.png")
    plt.savefig(save_path, dpi=120, bbox_inches='tight')
    plt.close()
    
    return save_path

# ================= 主程序 =================
if __name__ == "__main__":
    log_path = "/home/dclserver78/OAI_luuuuuu/DevChannelProxyJIN/logs/latest"
    bin_files = glob.glob(os.path.join(log_path, "pusch_cfr_ue_*.bin"))

    if not bin_files:
        print("❌ 未找到 .bin 文件。请确认 OAI 已生成日志。")
    else:
        print(f"🔍 发现了 {len(bin_files)} 个 UE 数据文件，开始逐一扫描...")
        
        # 遍历所有的 bin 文件
        for bin_file in bin_files:
            print(f"\n==================================================")
            print(f"📂 正在极速扫描文件: {bin_file}")
            
            # 极速解析
            important_data = scan_bin_and_extract_important(bin_file)

            if important_data:
                print(f"✨ 从该文件中提取了 {len(important_data)} 个关键信道矩阵。开始生成图表...")
                
                for d in important_data:
                    path = visualize_cfr(d)
                    mimo_alert = " 🚀 [MIMO DETECTED!]" if d['meta'][3] > 1 else ""
                    print(f"  [Saved] {d['tag'].ljust(15)} | Slot:{d['index']} -> {path}{mimo_alert}")
            else:
                print("⚠️ 该文件中没有发现有效的数据块。")
                
        print("\n✅ 所有 UE 的重要图像已全部保存到 ./cfr_results/ 目录中。")