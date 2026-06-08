#!/usr/bin/env python3
"""
verify_float_fft_gain.py — E1 对照实验
验证 float FFT 是否能突破 int16 FFT 导致的帧间 correlation=0.54 瓶颈。

方法:
  用 Sionna GT H(f) + SRS ref 模拟完整 SRS 链路,
  分别走 int16 FFT 路径和 float FFT 路径, 比较帧间一致性。

用法:
  python3 verify_float_fft_gain.py --gt-dir <path> [--snr 20] [--n-frames 50]
"""
import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from oai_dft_wrapper import (
    OAIDftWrapper, srs_ls_int16, srs_filt8_int16,
    FFT_SIZE, K_TC, M_SC_B_SRS, SRS_GEN_BITS
)


def load_gt_frames(gt_dir: str, max_frames: int = 50):
    """Load GT H(f) from npz files."""
    gt_path = Path(gt_dir)
    files = sorted(gt_path.glob("gt_batch_ue0_seq*.npz"))
    if not files:
        raise FileNotFoundError(f"No GT files in {gt_dir}")
    
    all_h = []
    for f in files:
        data = np.load(f)
        h = data["h_matrix"]  # (batch, n_sym, n_rx, n_tx, n_sc)
        h = h[:, 0, :, :, :]  # take first symbol → (batch, n_rx, n_tx, n_sc)
        all_h.append(h)
        if sum(x.shape[0] for x in all_h) >= max_frames:
            break
    
    H = np.concatenate(all_h, axis=0)[:max_frames]
    return H


def load_srs_ref(ref_path: str = "/tmp/oai_gpu_ipc/srs_ref.bin"):
    """Load SRS reference signal dumped by OAI."""
    data = np.fromfile(ref_path, dtype=np.int16)
    n_ports = 2
    n_sc = FFT_SIZE
    expected = n_ports * n_sc * 2
    if len(data) < expected:
        raise ValueError(f"srs_ref.bin too small: {len(data)} < {expected}")
    ref = data[:expected].reshape(n_ports, n_sc, 2)
    return ref[:, :, 0], ref[:, :, 1]  # (n_ports, n_sc) real/imag


def get_pilot_sc(n_sc=FFT_SIZE, k_tc=K_TC, m_sc=M_SC_B_SRS):
    """Generate pilot subcarrier indices for comb-2."""
    k0 = 0
    return np.arange(m_sc) * k_tc + k0


def pipeline_int16_fft(H_freq, X_ref_r, X_ref_i, pilot_sc, dft, snr_db=20):
    """Full int16 pipeline: H×X → IFFT → int16 → OAI FFT → LS."""
    X_ref = X_ref_r.astype(np.float64) + 1j * X_ref_i.astype(np.float64)
    Y_freq = H_freq * X_ref
    
    y_time = np.fft.ifft(Y_freq) * np.sqrt(FFT_SIZE)
    
    # Add noise
    if snr_db is not None:
        sig_pwr = np.mean(np.abs(y_time)**2)
        noise_std = np.sqrt(sig_pwr / (10**(snr_db/10)) / 2)
        y_time += noise_std * (np.random.randn(FFT_SIZE) + 1j*np.random.randn(FFT_SIZE))
    
    # int16 quantization
    y_iq = np.zeros(FFT_SIZE * 2, dtype=np.int16)
    y_iq[0::2] = np.clip(np.round(y_time.real), -32768, 32767).astype(np.int16)
    y_iq[1::2] = np.clip(np.round(y_time.imag), -32768, 32767).astype(np.int16)
    
    # OAI int16 FFT
    Y_rx_iq = dft.dft2048(y_iq, scale=1)
    rx_r = Y_rx_iq[0::2].astype(np.int16)
    rx_i = Y_rx_iq[1::2].astype(np.int16)
    
    # LS
    ls_r, ls_i = srs_ls_int16(
        X_ref_r.astype(np.int16), X_ref_i.astype(np.int16),
        rx_r, rx_i, pilot_sc)
    
    return ls_r.astype(np.float64) + 1j * ls_i.astype(np.float64)


def pipeline_float_fft(H_freq, X_ref_r, X_ref_i, pilot_sc, snr_db=20):
    """Float pipeline: H×X → IFFT → int16 → FLOAT FFT → float LS."""
    X_ref = X_ref_r.astype(np.float64) + 1j * X_ref_i.astype(np.float64)
    Y_freq = H_freq * X_ref
    
    y_time = np.fft.ifft(Y_freq) * np.sqrt(FFT_SIZE)
    
    # Add noise (same realization requires same seed — handled by caller)
    if snr_db is not None:
        sig_pwr = np.mean(np.abs(y_time)**2)
        noise_std = np.sqrt(sig_pwr / (10**(snr_db/10)) / 2)
        y_time += noise_std * (np.random.randn(FFT_SIZE) + 1j*np.random.randn(FFT_SIZE))
    
    # int16 quantization (same as Proxy output)
    y_iq_r = np.clip(np.round(y_time.real), -32768, 32767).astype(np.float64)
    y_iq_i = np.clip(np.round(y_time.imag), -32768, 32767).astype(np.float64)
    
    # FLOAT FFT (numpy, equivalent to FFTW float)
    y_cpx = y_iq_r + 1j * y_iq_i
    Y_rx = np.fft.fft(y_cpx)
    
    # Normalize to match OAI dft scale=1 behavior (÷sqrt(N) approx)
    # OAI: input RMS=564 → output RMS=14000, ratio≈24.8
    # numpy fft: no normalization, ratio=N=2048 for DC
    # OAI dft(scale=1) ≈ FFT / sqrt(N) approximately
    # We'll calibrate by matching pilot SC power to int16 path
    # For now use /sqrt(N) as first approximation
    Y_rx_norm = Y_rx / np.sqrt(FFT_SIZE)
    
    # Float LS: conj(X_ref) × Y_rx / |X_ref|^2
    rx_at_pilots = Y_rx_norm[pilot_sc]
    gen_at_pilots = X_ref[pilot_sc]
    ls_float = rx_at_pilots * np.conj(gen_at_pilots) / (np.abs(gen_at_pilots)**2 + 1e-10)
    
    return ls_float


def pipeline_float_fft_int16ls(H_freq, X_ref_r, X_ref_i, pilot_sc, snr_db=20):
    """Float FFT but then cast to int16 and do int16 LS (tests cast-back degradation)."""
    X_ref = X_ref_r.astype(np.float64) + 1j * X_ref_i.astype(np.float64)
    Y_freq = H_freq * X_ref
    
    y_time = np.fft.ifft(Y_freq) * np.sqrt(FFT_SIZE)
    
    if snr_db is not None:
        sig_pwr = np.mean(np.abs(y_time)**2)
        noise_std = np.sqrt(sig_pwr / (10**(snr_db/10)) / 2)
        y_time += noise_std * (np.random.randn(FFT_SIZE) + 1j*np.random.randn(FFT_SIZE))
    
    # int16 quantization
    y_iq_r = np.clip(np.round(y_time.real), -32768, 32767).astype(np.float64)
    y_iq_i = np.clip(np.round(y_time.imag), -32768, 32767).astype(np.float64)
    
    # Float FFT
    y_cpx = y_iq_r + 1j * y_iq_i
    Y_rx = np.fft.fft(y_cpx) / np.sqrt(FFT_SIZE)
    
    # Cast back to int16 (tests if cast-back kills the benefit)
    rx_r = np.clip(np.round(Y_rx.real), -32768, 32767).astype(np.int16)
    rx_i = np.clip(np.round(Y_rx.imag), -32768, 32767).astype(np.int16)
    
    # int16 LS (same as OAI)
    ls_r, ls_i = srs_ls_int16(
        X_ref_r.astype(np.int16), X_ref_i.astype(np.int16),
        rx_r, rx_i, pilot_sc)
    
    return ls_r.astype(np.float64) + 1j * ls_i.astype(np.float64)


def interframe_analysis(estimates, label):
    """Compute inter-frame NMSE and correlation."""
    N = len(estimates)
    h0 = estimates[0]
    nmse_list = []
    corr_list = []
    for i in range(1, N):
        hi = estimates[i]
        alpha = np.vdot(h0, hi) / np.vdot(h0, h0)
        hi_aligned = hi / alpha
        nmse = np.mean(np.abs(hi_aligned - h0)**2) / np.mean(np.abs(h0)**2)
        nmse_list.append(10*np.log10(max(nmse, 1e-30)))
        c = np.abs(np.vdot(h0, hi)) / (np.linalg.norm(h0) * np.linalg.norm(hi))
        corr_list.append(c)
    
    nmse_arr = np.array(nmse_list)
    corr_arr = np.array(corr_list)
    print(f"\n  [{label}]")
    print(f"    Inter-frame self-NMSE: p10={np.percentile(nmse_arr,10):.2f}, "
          f"p50={np.percentile(nmse_arr,50):.2f}, p90={np.percentile(nmse_arr,90):.2f} dB")
    print(f"    Inter-frame |corr|:   mean={np.mean(corr_arr):.4f}, "
          f"min={np.min(corr_arr):.4f}, max={np.max(corr_arr):.4f}")
    print(f"    Effective SNR:        {-np.percentile(nmse_arr,50):.1f} dB")
    return np.percentile(nmse_arr, 50), np.mean(corr_arr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gt-dir", required=True, help="Path to sionna_gt directory")
    parser.add_argument("--srs-ref", default="/tmp/oai_gpu_ipc/srs_ref.bin")
    parser.add_argument("--snr", type=float, default=20.0)
    parser.add_argument("--n-frames", type=int, default=30)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    
    print("=" * 70)
    print("  E1 对照实验: int16 FFT vs float FFT")
    print("=" * 70)
    
    # Load data
    print(f"\n[1] Loading GT from {args.gt_dir}...")
    H_all = load_gt_frames(args.gt_dir, args.n_frames)
    print(f"    GT shape: {H_all.shape}")
    
    print(f"[2] Loading SRS ref from {args.srs_ref}...")
    ref_r, ref_i = load_srs_ref(args.srs_ref)
    print(f"    SRS ref shape: {ref_r.shape}")
    
    pilot_sc = get_pilot_sc()
    print(f"    Pilot SC: {len(pilot_sc)} subcarriers")
    
    # Initialize OAI DFT
    print(f"[3] Initializing OAI DFT wrapper...")
    try:
        dft = OAIDftWrapper()
        have_oai_dft = True
        print("    OAI libdfts.so loaded OK")
    except Exception as e:
        print(f"    WARNING: Cannot load libdfts.so ({e})")
        print(f"    Will skip int16 FFT path (only float paths)")
        have_oai_dft = False
    
    # Run pipelines
    print(f"\n[4] Running {args.n_frames} frames, SNR={args.snr} dB, seed={args.seed}")
    print("-" * 70)
    
    rx_idx, tx_idx = 0, 0
    X_r = ref_r[tx_idx]  # (2048,)
    X_i = ref_i[tx_idx]
    
    est_int16 = []
    est_float = []
    est_float_int16ls = []
    
    for frame in range(args.n_frames):
        np.random.seed(args.seed + frame)
        H_f = H_all[frame, rx_idx, tx_idx, :].astype(np.complex128)
        
        # Path A: int16 FFT (bit-exact OAI)
        if have_oai_dft:
            np.random.seed(args.seed + frame)
            h_int16 = pipeline_int16_fft(H_f, X_r, X_i, pilot_sc, dft, args.snr)
            est_int16.append(h_int16)
        
        # Path B: float FFT + float LS
        np.random.seed(args.seed + frame)
        h_float = pipeline_float_fft(H_f, X_r, X_i, pilot_sc, args.snr)
        est_float.append(h_float)
        
        # Path C: float FFT → int16 → int16 LS (tests cast-back)
        if have_oai_dft:
            np.random.seed(args.seed + frame)
            h_f_i16ls = pipeline_float_fft_int16ls(H_f, X_r, X_i, pilot_sc, args.snr)
            est_float_int16ls.append(h_f_i16ls)
    
    # Analysis
    print(f"\n{'='*70}")
    print(f"  RESULTS ({args.n_frames} frames, SNR={args.snr} dB)")
    print(f"{'='*70}")
    
    if est_int16:
        interframe_analysis(est_int16, "A: OAI int16 FFT + int16 LS (当前系统)")
    
    interframe_analysis(est_float, "B: float FFT + float LS (E1 最佳情况)")
    
    if est_float_int16ls:
        interframe_analysis(est_float_int16ls, "C: float FFT → int16 → int16 LS (E1 实际方案)")
    
    print(f"\n{'='*70}")
    print("  结论:")
    if est_int16 and est_float:
        _, corr_a = interframe_analysis(est_int16, "recap A")
        _, corr_b = interframe_analysis(est_float, "recap B")
        print(f"\n    如果 B 的 corr ({corr_b:.3f}) >> A 的 corr ({corr_a:.3f}):")
        print(f"    → E1 (float FFT) 值得做")
        print(f"    如果 B ≈ A: → 瓶颈不在 FFT，E1 无效")


if __name__ == "__main__":
    main()
