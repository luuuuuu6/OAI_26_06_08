#!/usr/bin/env python3
"""
test_sinc_interpolation.py — Sinc vs filt8 interpolation A/B test

Uses existing GT + SRS data to compare interpolation methods:
  1. filt8_legacy  (3-point smoothing at pilots, linear midpoint) — OAI's current
  2. filt8_opt     (exact pilot + linear midpoint) — our previous optimization
  3. sinc_W        (windowed sinc with W taps each side)
  4. oracle        (GT itself)

Usage:
  python3 test_sinc_interpolation.py --data-dir <snr_xxdB directory>
  python3 test_sinc_interpolation.py  # uses latest static experiment
"""

import argparse
import glob
import os
import sys
import numpy as np
from typing import Tuple, Optional

# ─── OAI filt8 coefficients (from filt16a_32.h, divided by 16384) ───────────

# Legacy comb-2: 3-point smoothing at pilots, linear midpoint
FILT8_LEGACY = {
    'start':   np.array([12288, 8192, 4096, 0, 0, 0, 0, 0]) / 16384.0,
    'middle2': np.array([4096, 8192, 8192, 8192, 4096, 0, 0, 0]) / 16384.0,
    'middle4': np.array([0, 0, 4096, 8192, 8192, 8192, 4096, 0]) / 16384.0,
    'end':     np.array([4096, 8192, 12288, 16384, 0, 0, 0, 0]) / 16384.0,
}

# Optimized comb-2: exact pilot reconstruction + linear midpoint
FILT8_OPT = {
    'start':   np.array([16384, 8192, 0, 0, 0, 0, 0, 0]) / 16384.0,
    'middle2': np.array([0, 8192, 16384, 8192, 0, 0, 0, 0]) / 16384.0,
    'middle4': np.array([0, 0, 0, 8192, 16384, 8192, 0, 0]) / 16384.0,
    'end_odd': np.array([0, 8192, 16384, 16384, 0, 0, 0, 0]) / 16384.0,
    'end_even':np.array([0, 0, 0, 8192, 16384, 16384, 0, 0]) / 16384.0,
}


# ─── Interpolation engines ──────────────────────────────────────────────────

def sinc_coeffs(half_width: int, use_window: bool = True) -> np.ndarray:
    """Compute windowed sinc coefficients for comb-2 midpoint interpolation.

    Returns array of length 2*half_width for taps at positions
    -(half_width-1)-0.5, ..., -0.5, +0.5, ..., +(half_width-1)+0.5
    relative to the midpoint.
    """
    n = np.arange(-half_width, half_width) + 0.5
    h = np.sinc(n)  # sinc(x) = sin(pi*x)/(pi*x)
    if use_window and half_width > 1:
        w = np.kaiser(2 * half_width, beta=6.0)
        h *= w
    h /= h.sum()
    return h


def interp_filt8_legacy(H_pilot: np.ndarray) -> np.ndarray:
    """Simulate OAI legacy filt8 comb-2 interpolation.

    H_pilot: (N_pilot,) complex — LS estimates at pilot SCs
    Returns: (2*N_pilot,) complex — interpolated to all SCs
    """
    N = len(H_pilot)
    out = np.zeros(2 * N, dtype=H_pilot.dtype)

    for k in range(N):
        if k == 0:
            # start: {0.75, 0.5, 0.25} applied to positions [0,1,2]
            out[0] += H_pilot[0] * 0.75
            out[1] += H_pilot[0] * 0.5
            if N > 1:
                out[2] += H_pilot[0] * 0.25
        elif k == N - 1:
            # end: {0.25, 0.5, 0.75, 1.0} applied to positions [-3,-2,-1,0] relative
            pos = 2 * k
            out[pos - 3] += H_pilot[k] * 0.25
            out[pos - 2] += H_pilot[k] * 0.5
            out[pos - 1] += H_pilot[k] * 0.75
            out[pos]     += H_pilot[k] * 1.0
        else:
            pos = 2 * k
            if k % 2 == 1:
                # middle2: {0.25, 0.5, 0.5, 0.5, 0.25} centered on pos
                out[pos - 1] += H_pilot[k] * 0.25
                out[pos]     += H_pilot[k] * 0.5
                out[pos + 1] += H_pilot[k] * 0.5
                out[pos + 2] += H_pilot[k] * 0.5
                if pos + 3 < 2 * N:
                    out[pos + 3] += H_pilot[k] * 0.25
            else:
                # middle4: shifted by 2
                if pos - 1 >= 0:
                    out[pos - 1] += H_pilot[k] * 0.25
                out[pos]     += H_pilot[k] * 0.5
                out[pos + 1] += H_pilot[k] * 0.5
                out[pos + 2] += H_pilot[k] * 0.5
                if pos + 3 < 2 * N:
                    out[pos + 3] += H_pilot[k] * 0.25
    return out


def interp_filt8_opt(H_pilot: np.ndarray) -> np.ndarray:
    """Simulate OAI optimized filt8 comb-2 interpolation.

    Exact pilot reconstruction + linear average midpoint.
    """
    N = len(H_pilot)
    out = np.zeros(2 * N, dtype=H_pilot.dtype)

    # Pilot positions: exact
    for k in range(N):
        out[2 * k] = H_pilot[k]

    # Midpoints: linear average of neighbors
    for k in range(N - 1):
        out[2 * k + 1] = 0.5 * (H_pilot[k] + H_pilot[k + 1])

    # Last midpoint: repeat last pilot
    out[2 * N - 1] = H_pilot[N - 1]
    return out


def interp_sinc(H_pilot: np.ndarray, half_width: int = 4,
                use_window: bool = True) -> np.ndarray:
    """Sinc interpolation for comb-2.

    H_pilot: (N_pilot,) complex — LS at pilot SCs (every 2nd SC)
    Returns: (2*N_pilot,) complex — full channel
    """
    N = len(H_pilot)
    out = np.zeros(2 * N, dtype=H_pilot.dtype)

    # Pilot positions: exact reconstruction
    for k in range(N):
        out[2 * k] = H_pilot[k]

    # Midpoints: windowed sinc
    coeffs = sinc_coeffs(half_width, use_window)

    for k in range(N - 1):
        val = 0.0j
        for j in range(2 * half_width):
            idx = k - half_width + 1 + j
            idx_clamped = max(0, min(N - 1, idx))
            val += H_pilot[idx_clamped] * coeffs[j]
        out[2 * k + 1] = val

    out[2 * N - 1] = H_pilot[N - 1]
    return out


def interp_dft(H_pilot: np.ndarray) -> np.ndarray:
    """DFT-based interpolation (theoretically perfect for bandlimited signals).

    Zero-pad in time domain to double the frequency resolution.
    """
    N = len(H_pilot)
    h_time = np.fft.ifft(H_pilot)
    h_padded = np.zeros(2 * N, dtype=h_time.dtype)
    h_padded[:N // 2] = h_time[:N // 2]
    h_padded[-(N // 2):] = h_time[N // 2:]
    out = np.fft.fft(h_padded) * 2  # scale for energy
    return out


# ─── STO simulation ────────────────────────────────────────────────────────

def apply_sto(H_freq: np.ndarray, sto_samples: float, n_fft: int) -> np.ndarray:
    """Apply STO as linear phase ramp in frequency domain."""
    sc_idx = np.arange(H_freq.shape[-1])
    phase = np.exp(-1j * 2 * np.pi * sto_samples * sc_idx / n_fft)
    return H_freq * phase


# ─── int16 quantization simulation ─────────────────────────────────────────

def quantize_int16(H: np.ndarray, n_bits: int = 15) -> np.ndarray:
    """Simulate int16 quantization (Q15 format)."""
    scale = 2 ** n_bits
    max_val = 2 ** 15 - 1
    H_q = np.clip(np.round(H.real * scale), -max_val, max_val) + \
           1j * np.clip(np.round(H.imag * scale), -max_val, max_val)
    return H_q / scale


# ─── Main evaluation ───────────────────────────────────────────────────────

def load_data(data_dir: str):
    """Load GT and SRS from experiment directory."""
    gt_dir = os.path.join(data_dir, 'sionna_gt')
    gt_files = sorted(glob.glob(os.path.join(gt_dir, 'gt_batch_ue0_seq*.npz')),
                      key=lambda f: int(f.split('seq')[1].split('.')[0]))
    if not gt_files:
        raise FileNotFoundError(f"No GT files in {gt_dir}")

    d0 = np.load(gt_files[0])
    H_gt = d0['h_matrix'][0, 0]  # first slot, first symbol: (N_rx, N_tx, FFT)
    print(f"GT shape: {H_gt.shape}")
    return H_gt


def run_comparison(H_gt_full: np.ndarray, K_TC: int = 2,
                   sto_values: list = None,
                   snr_dB: float = 20.0,
                   sinc_widths: list = None):
    """Compare interpolation methods on a single antenna pair.

    H_gt_full: (N_fft,) complex — true channel on all SCs
    """
    if sto_values is None:
        sto_values = [0.0, -1.5, -2.7, -4.0]
    if sinc_widths is None:
        sinc_widths = [1, 2, 4, 8, 16]

    n_fft = len(H_gt_full)

    # Active SCs (take center portion like NR SRS)
    n_active = (n_fft * 1248) // 2048
    sc_start = 0
    H_gt = H_gt_full[sc_start:sc_start + n_active]
    N_pilot = n_active // K_TC

    # Noise
    signal_power = np.mean(np.abs(H_gt) ** 2)
    noise_var = signal_power * 10 ** (-snr_dB / 10)

    print(f"\n{'='*80}")
    print(f"Comparison: n_fft={n_fft}, active={n_active}, K_TC={K_TC}, "
          f"N_pilot={N_pilot}, SNR={snr_dB} dB")
    print(f"{'='*80}")

    results = {}

    for sto in sto_values:
        print(f"\n--- STO = {sto:.1f} samples ---")

        # Apply STO to GT (simulates timing offset)
        H_sto = apply_sto(H_gt[np.newaxis, :], sto, n_fft)[0]

        # Add noise
        rng = np.random.default_rng(42)
        noise = rng.normal(0, np.sqrt(noise_var / 2), (n_active,)) + \
                1j * rng.normal(0, np.sqrt(noise_var / 2), (n_active,))
        H_noisy = H_sto + noise

        # Extract pilot SCs
        H_pilot = H_noisy[::K_TC]

        # --- Method 1: Legacy filt8 ---
        H_legacy = interp_filt8_legacy(H_pilot)[:n_active]
        nmse_legacy = compute_nmse(H_legacy, H_noisy, H_sto)

        # --- Method 2: Optimized filt8 ---
        H_opt = interp_filt8_opt(H_pilot)[:n_active]
        nmse_opt = compute_nmse(H_opt, H_noisy, H_sto)

        # --- Method 3: DFT (theoretically perfect) ---
        H_dft = interp_dft(H_pilot)[:n_active]
        nmse_dft = compute_nmse(H_dft, H_noisy, H_sto)

        # --- Method 4+: Sinc with various widths ---
        nmse_sinc = {}
        for W in sinc_widths:
            H_sinc = interp_sinc(H_pilot, half_width=W)[:n_active]
            nmse_sinc[W] = compute_nmse(H_sinc, H_noisy, H_sto)

        # --- Method: Oracle (noisy H itself, no interpolation needed) ---
        nmse_oracle = compute_nmse(H_noisy, H_noisy, H_sto)

        print(f"  {'Method':<20s} {'NMSE(vs GT+STO)':<18s} {'NMSE(LS-align)':<18s}")
        print(f"  {'-'*56}")
        print(f"  {'filt8_legacy':<20s} {nmse_legacy['raw']:>8.2f} dB       {nmse_legacy['aligned']:>8.2f} dB")
        print(f"  {'filt8_opt':<20s} {nmse_opt['raw']:>8.2f} dB       {nmse_opt['aligned']:>8.2f} dB")
        print(f"  {'DFT (perfect)':<20s} {nmse_dft['raw']:>8.2f} dB       {nmse_dft['aligned']:>8.2f} dB")
        for W in sinc_widths:
            label = f"sinc_W{W}"
            print(f"  {label:<20s} {nmse_sinc[W]['raw']:>8.2f} dB       {nmse_sinc[W]['aligned']:>8.2f} dB")
        print(f"  {'oracle (no interp)':<20s} {nmse_oracle['raw']:>8.2f} dB       {nmse_oracle['aligned']:>8.2f} dB")

        results[sto] = {
            'legacy': nmse_legacy,
            'opt': nmse_opt,
            'dft': nmse_dft,
            'sinc': nmse_sinc,
            'oracle': nmse_oracle,
        }

    return results


def compute_nmse(H_est: np.ndarray, H_ref: np.ndarray,
                 H_gt_sto: np.ndarray) -> dict:
    """Compute NMSE (raw and LS-aligned) of H_est vs H_gt_sto."""
    # Raw NMSE (no alignment)
    err_raw = H_est - H_gt_sto
    nmse_raw = np.mean(np.abs(err_raw) ** 2) / np.mean(np.abs(H_gt_sto) ** 2)
    nmse_raw_db = 10 * np.log10(max(nmse_raw, 1e-30))

    # LS-aligned NMSE
    alpha = np.vdot(H_est, H_gt_sto) / np.vdot(H_gt_sto, H_gt_sto)
    err_aligned = H_est - alpha * H_gt_sto
    nmse_aligned = np.mean(np.abs(err_aligned) ** 2) / np.mean(np.abs(H_est) ** 2)
    nmse_aligned_db = 10 * np.log10(max(nmse_aligned, 1e-30))

    return {'raw': nmse_raw_db, 'aligned': nmse_aligned_db, 'alpha': alpha}


def run_with_real_srs(data_dir: str):
    """Compare using actual SRS data from the experiment.

    Since legacy filt8 smooths pilot positions, we can't extract clean pilots.
    Instead, we use GT's pilot SCs as the reference "clean" pilot data,
    and compare what each interpolation method would produce vs the full GT.
    """
    from digital_twin_stats import load_srs_v2, load_gt

    gt_dir = os.path.join(data_dir, 'sionna_gt')
    H_srs, srs_meta = load_srs_v2(data_dir)
    H_gt_all, gt_slots = load_gt(gt_dir, return_slot_ids=True)
    G_ref = H_gt_all[0]  # static channel: all GT identical
    n_fft = H_srs.shape[-1]

    active = np.any(np.abs(H_srs) > 0, axis=(0, 1, 2))
    K = int(active.sum())
    sc_indices = np.where(active)[0]
    K_TC = 2

    print(f"\n{'='*80}")
    print(f"Real SRS Data Analysis: {H_srs.shape[0]} SRS frames, "
          f"active={K} SCs, K_TC={K_TC}")
    print(f"{'='*80}")

    # Use GT on active SCs as pilot source (clean, no filt8 contamination)
    gt_active = G_ref[0, 0, active]  # rx=0, tx=0
    gt_pilots = gt_active[::K_TC]    # every 2nd SC = pilot position

    print(f"\nGT-based interpolation test (rx=0, tx=0):")
    print(f"  GT pilots: {len(gt_pilots)}, GT active: {len(gt_active)}")

    # Interpolate GT pilots with each method
    methods = {}
    methods['filt8_legacy'] = interp_filt8_legacy(gt_pilots)[:K]
    methods['filt8_opt'] = interp_filt8_opt(gt_pilots)[:K]
    methods['DFT'] = interp_dft(gt_pilots)[:K]
    for W in [1, 2, 4, 8, 16]:
        methods[f'sinc_W{W}'] = interp_sinc(gt_pilots, half_width=W)[:K]

    print(f"\n  {'Method':<20s} {'NMSE vs GT':<15s} {'NMSE(LS-align)':<15s}")
    print(f"  {'-'*50}")
    for name, H_est in sorted(methods.items()):
        nm = compute_nmse(H_est, gt_active, gt_active)
        print(f"  {name:<20s} {nm['raw']:>8.2f} dB     {nm['aligned']:>8.2f} dB")

    # Now test with GT + STO (simulating what OAI actually sees)
    print(f"\nGT + STO simulation (testing STO robustness):")
    for sto in [0.0, -1.5, -2.7, -4.0]:
        gt_sto = apply_sto(gt_active[np.newaxis, :], sto, n_fft)[0]
        gt_pilots_sto = gt_sto[::K_TC]

        nm_legacy = compute_nmse(
            interp_filt8_legacy(gt_pilots_sto)[:K], gt_sto, gt_sto)
        nm_opt = compute_nmse(
            interp_filt8_opt(gt_pilots_sto)[:K], gt_sto, gt_sto)
        nm_sinc4 = compute_nmse(
            interp_sinc(gt_pilots_sto, half_width=4)[:K], gt_sto, gt_sto)
        nm_sinc8 = compute_nmse(
            interp_sinc(gt_pilots_sto, half_width=8)[:K], gt_sto, gt_sto)
        nm_dft = compute_nmse(
            interp_dft(gt_pilots_sto)[:K], gt_sto, gt_sto)

        print(f"  STO={sto:5.1f}: legacy={nm_legacy['raw']:6.1f}dB  "
              f"opt={nm_opt['raw']:6.1f}dB  "
              f"sinc4={nm_sinc4['raw']:6.1f}dB  "
              f"sinc8={nm_sinc8['raw']:6.1f}dB  "
              f"DFT={nm_dft['raw']:6.1f}dB")

    # Compare actual SRS (filt8 output) vs sinc-from-GT
    print(f"\nActual SRS vs GT NMSE (per-frame, rx=0 tx=0):")
    from digital_twin_stats import _estimate_sto_single
    nmse_srs_list = []
    nmse_sinc_list = []
    for fi in range(min(20, H_srs.shape[0])):
        h_srs = H_srs[fi, 0, 0, active]
        g = G_ref[0, 0, active]
        sto = _estimate_sto_single(H_srs[fi], G_ref, active, n_fft)
        sto_samples = sto * n_fft / (2 * np.pi)

        # SRS (filt8 output) vs GT
        alpha_srs = np.vdot(h_srs, g) / np.vdot(g, g)
        err_srs = h_srs - alpha_srs * g
        nmse_srs = 10 * np.log10(np.mean(np.abs(err_srs) ** 2) /
                                  np.mean(np.abs(h_srs) ** 2) + 1e-30)
        nmse_srs_list.append(nmse_srs)

        # sinc from GT pilots + STO (what sinc WOULD give)
        g_sto = apply_sto(g[np.newaxis, :], sto_samples, n_fft)[0]
        g_pilots_sto = g_sto[::K_TC]
        h_sinc = interp_sinc(g_pilots_sto, half_width=8)[:K]
        alpha_sinc = np.vdot(h_sinc, g_sto) / np.vdot(g_sto, g_sto)
        err_sinc = h_sinc - alpha_sinc * g_sto
        nmse_sinc = 10 * np.log10(np.mean(np.abs(err_sinc) ** 2) /
                                   np.mean(np.abs(h_sinc) ** 2) + 1e-30)
        nmse_sinc_list.append(nmse_sinc)

        if fi < 5 or nmse_srs > -3:
            print(f"  fi={fi:3d} STO={sto_samples:6.1f}  "
                  f"SRS(filt8)={nmse_srs:6.2f}dB  "
                  f"sinc8(GT)={nmse_sinc:6.2f}dB")

    nmse_srs_arr = np.array(nmse_srs_list)
    nmse_sinc_arr = np.array(nmse_sinc_list)
    print(f"\n  Summary (first {len(nmse_srs_arr)} frames):")
    print(f"    SRS(filt8): p50={np.median(nmse_srs_arr):.2f} dB, "
          f"mean={nmse_srs_arr.mean():.2f} dB")
    print(f"    sinc8(GT):  p50={np.median(nmse_sinc_arr):.2f} dB, "
          f"mean={nmse_sinc_arr.mean():.2f} dB")
    print(f"    Improvement: {np.median(nmse_srs_arr) - np.median(nmse_sinc_arr):.1f} dB "
          f"(lower is better for sinc)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data-dir', type=str,
                    default='/home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/'
                            'logs/q4_sweep_20260513_000323/snr_20dB')
    ap.add_argument('--snr', type=float, default=20.0)
    args = ap.parse_args()

    print("=" * 80)
    print("  Sinc vs filt8 Interpolation Comparison")
    print("=" * 80)
    print(f"  Data: {args.data_dir}")
    print(f"  SNR:  {args.snr} dB")

    # --- Part 1: Pure simulation with GT ---
    print("\n" + "=" * 80)
    print("  PART 1: Pure GT simulation (no OAI processing)")
    print("=" * 80)
    H_gt = load_data(args.data_dir)
    run_comparison(H_gt[0, 0], K_TC=2, snr_dB=args.snr,
                   sto_values=[0.0, -0.5, -1.0, -1.5, -2.0, -2.7, -3.5, -4.0],
                   sinc_widths=[1, 2, 4, 8, 16])

    # --- Part 2: Using actual SRS data ---
    print("\n" + "=" * 80)
    print("  PART 2: Real SRS data comparison")
    print("=" * 80)
    run_with_real_srs(args.data_dir)


if __name__ == '__main__':
    main()
