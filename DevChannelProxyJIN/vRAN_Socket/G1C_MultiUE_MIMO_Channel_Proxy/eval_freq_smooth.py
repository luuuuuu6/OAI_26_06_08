#!/usr/bin/env python3
"""eval_freq_smooth.py — Adaptive frequency-domain smoothing evaluation

Offline post-processing tool: loads SRS + GT, applies moving average of
various window sizes to the SRS estimate, and evaluates NMSE improvement.

Key features:
  1. Fixed-window sweep: mavg3 .. mavg513
  2. Roughness metric: mean(|H[k+1]-H[k]|^2) / mean(|H[k]|^2)
  3. Adaptive window selection based on roughness vs noise estimate
  4. Per-frame and aggregate NMSE (per-antenna LS alpha, same as eval_nmse_clean)

Usage:
  python3 eval_freq_smooth.py --run-dir logs/.../snr_15dB
  python3 eval_freq_smooth.py --run-dir logs/.../snr_15dB --windows 3,5,9,17,33,65,129
  python3 eval_freq_smooth.py --run-dir logs/.../snr_15dB --adaptive
"""

import argparse
import sys
from pathlib import Path

import numpy as np
from scipy.ndimage import uniform_filter1d

sys.path.insert(0, str(Path(__file__).resolve().parent))
from digital_twin_stats import index_gt_slots, load_gt_by_refs  # noqa: E402

# Re-use SRS loader and alignment from eval_nmse_clean
from eval_nmse_clean import (  # noqa: E402
    load_srs,
    align_frames,
    get_active_mask,
)

WINDOW_TABLE = [3, 5, 9, 17, 33, 65, 129, 257, 513]


# ═══════════════════════════════════════════════════════════════
# Frequency-domain smoothing
# ═══════════════════════════════════════════════════════════════

def freq_moving_average(H, active_mask, win_size):
    """Apply symmetric moving average along the subcarrier axis.

    Only smooths within active subcarriers using mirror padding to
    avoid boundary artifacts.

    Parameters
    ----------
    H : (..., N_sc) complex array — SRS channel estimate
    active_mask : (N_sc,) bool
    win_size : int — odd window size

    Returns
    -------
    H_smooth : same shape as H
    """
    if win_size <= 1:
        return H.copy()

    aidx = np.where(active_mask)[0]
    if len(aidx) == 0:
        return H.copy()

    H_out = H.copy()
    orig_shape = H.shape
    flat = H_out.reshape(-1, H_out.shape[-1])

    for row in range(flat.shape[0]):
        active_vals = flat[row, aidx]
        smoothed = uniform_filter1d(active_vals.real, size=win_size, mode='reflect') + \
                   1j * uniform_filter1d(active_vals.imag, size=win_size, mode='reflect')
        flat[row, aidx] = smoothed

    return H_out.reshape(orig_shape)


def compute_roughness(H, active_mask):
    """Frequency-domain roughness: mean(|H[k+1]-H[k]|^2) / mean(|H[k]|^2).

    Computed per-frame, returns array of shape (N_frames,).
    """
    aidx = np.where(active_mask)[0]
    if len(aidx) < 2:
        return np.zeros(H.shape[0])

    H_act = H[..., aidx]  # (F, Rx, Tx, N_active)
    diff = np.diff(H_act, axis=-1)
    diff_power = np.mean(np.abs(diff) ** 2, axis=(-3, -2, -1))
    sig_power = np.mean(np.abs(H_act) ** 2, axis=(-3, -2, -1))
    return diff_power / (sig_power + 1e-30)


def compute_noise_estimate(H, active_mask):
    """Noise estimate from pilot-interpolation residual.

    For comb-2 SRS, pilots are at even indices within active region.
    noise_est = mean(|H_pilot - H_interp_at_pilot|^2) / mean(|H_pilot|^2)

    Approximation: use odd-even difference as proxy for noise level.
    """
    aidx = np.where(active_mask)[0]
    if len(aidx) < 4:
        return np.zeros(H.shape[0])

    H_act = H[..., aidx]
    even = H_act[..., 0::2]
    odd = H_act[..., 1::2]
    n = min(even.shape[-1], odd.shape[-1])
    diff = even[..., :n] - odd[..., :n]
    noise_power = np.mean(np.abs(diff) ** 2, axis=(-3, -2, -1))
    sig_power = np.mean(np.abs(H_act) ** 2, axis=(-3, -2, -1))
    return noise_power / (sig_power + 1e-30)


def select_adaptive_window(roughness_per_frame, noise_est_per_frame,
                           window_table=None):
    """Select window size per frame based on roughness vs noise ratio.

    If roughness ~ noise: all frequency variation is noise -> wide window.
    If roughness >> noise: real frequency selectivity -> narrow window.

    Returns per-frame window sizes and the median window.
    """
    if window_table is None:
        window_table = WINDOW_TABLE

    ratio = roughness_per_frame / (noise_est_per_frame + 1e-30)

    # ratio ~ 1.0 means roughness ≈ noise -> flat channel -> large window
    # ratio >> 1.0 means real selectivity -> small window
    # Map ratio to window index via log scale
    # ratio=1 -> largest window, ratio=10 -> smallest window
    log_ratio = np.log10(np.clip(ratio, 0.1, 100.0))

    # Linear mapping: log_ratio in [0, 1] -> window_idx from [max, 0]
    n = len(window_table)
    idx_float = (log_ratio - 0.0) / (1.0 - 0.0) * (n - 1)
    idx_float = np.clip(idx_float, 0, n - 1)
    # Invert: low ratio -> high index (wide window)
    idx = np.round((n - 1) - idx_float).astype(int)
    idx = np.clip(idx, 0, n - 1)

    per_frame_win = np.array([window_table[i] for i in idx])
    return per_frame_win, int(np.median(per_frame_win))


def apply_adaptive_smooth(H_srs, active_mask, roughness, noise_est,
                          window_table=None):
    """Apply per-frame adaptive window smoothing."""
    if window_table is None:
        window_table = WINDOW_TABLE

    per_frame_win, median_win = select_adaptive_window(
        roughness, noise_est, window_table)

    F = H_srs.shape[0]
    H_out = H_srs.copy()
    for f in range(F):
        w = per_frame_win[f]
        if w > 1:
            H_out[f] = freq_moving_average(
                H_srs[f:f+1], active_mask, w)[0]

    return H_out, per_frame_win, median_win


# ═══════════════════════════════════════════════════════════════
# NMSE computation (same formula as eval_nmse_clean)
# ═══════════════════════════════════════════════════════════════

def nmse_per_antenna(H_s, H_g, active):
    """Per-antenna LS alpha NMSE, matching eval_nmse_clean.py."""
    F, R, T, _ = H_s.shape
    eps = 1e-30
    aidx = np.where(active)[0]

    err_pf = np.zeros(F)
    sig_pf = np.zeros(F)

    for f in range(F):
        e_sum, s_sum = 0.0, 0.0
        for rx in range(R):
            for tx in range(T):
                si = H_s[f, rx, tx, aidx]
                gi = H_g[f, rx, tx, aidx]
                ai = np.vdot(gi, si) / (np.vdot(gi, gi) + eps)
                di = si - ai * gi
                e_sum += np.mean(np.abs(di) ** 2)
                s_sum += np.mean(np.abs(ai * gi) ** 2)
        err_pf[f] = e_sum / (R * T)
        sig_pf[f] = s_sum / (R * T)

    nmse_dB = 10 * np.log10(np.mean(err_pf) / (np.mean(sig_pf) + eps) + eps)
    pf_dB = 10 * np.log10(err_pf / (sig_pf + eps) + eps)
    return nmse_dB, pf_dB


def pct(arr):
    p10, p50, p90 = np.percentile(arr, [10, 50, 90])
    return f"[p10={p10:+.2f}, p50={p50:+.2f}, p90={p90:+.2f}]"


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True,
                    help="Path to snr_XdB directory")
    ap.add_argument("--srs-symbol", type=int, default=12)
    ap.add_argument("--skip-first", type=int, default=0)
    ap.add_argument("--tol", type=int, default=20)
    ap.add_argument("--windows", type=str, default=None,
                    help="Comma-separated window sizes, e.g. 3,5,9,17,33,65,129")
    ap.add_argument("--adaptive", action="store_true",
                    help="Run adaptive window selection")
    ap.add_argument("--full-band", action="store_true",
                    help="Also test full-band averaging")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    gt_dir = run_dir / "sionna_gt"
    if not run_dir.exists():
        sys.exit(f"run-dir not found: {run_dir}")
    if not gt_dir.exists():
        sys.exit(f"sionna_gt not found under {run_dir}")

    windows = WINDOW_TABLE[:]
    if args.windows:
        windows = [int(x) for x in args.windows.split(",")]

    # ── Load data ──
    print(f"[load] {run_dir}")
    H_srs, meta = load_srs(str(run_dir), skip_first=args.skip_first)
    print(f"  SRS: {H_srs.shape}  rx={meta['rx']} tx={meta['tx']} n_sc={meta['n_sc']}")

    print(f"[index GT] scanning metadata...")
    gt_slots, gt_refs = index_gt_slots(
        str(gt_dir), srs_symbol=args.srs_symbol, skip_first=args.skip_first)
    print(f"  GT indexed: {len(gt_slots)} valid frames")

    si, gi, gap = align_frames(meta["abs_slots"], gt_slots, tol=args.tol)
    print(f"  paired: {len(si)} frames  (median_gap={gap})")

    if len(si) < 2:
        sys.exit("too few paired frames")

    print(f"[load GT] loading {len(gi)} paired frames...")
    H_s = H_srs[si]
    H_g = load_gt_by_refs(gt_refs, gi)
    active = get_active_mask(H_s)
    n_act = int(active.sum())
    print(f"  active SC: {n_act}/{meta['n_sc']}")

    # ── Roughness and noise estimate ──
    print()
    print("=" * 60)
    print("  Frequency-domain diagnostics")
    print("=" * 60)

    roughness = compute_roughness(H_s, active)
    noise_est = compute_noise_estimate(H_s, active)

    roughness_gt = compute_roughness(H_g, active)

    print(f"  GT  roughness: {np.median(roughness_gt):.2e} (median)")
    print(f"  SRS roughness: {np.median(roughness):.4f} (median)")
    print(f"  SRS noise_est: {np.median(noise_est):.4f} (median)")
    print(f"  ratio rough/noise: {np.median(roughness / (noise_est + 1e-30)):.3f}")
    print()

    # ── Baseline NMSE (raw, no smoothing) ──
    nmse_raw, pf_raw = nmse_per_antenna(H_s, H_g, active)
    print("=" * 60)
    print("  NMSE Results")
    print("=" * 60)
    print(f"  {'Mode':<20s} {'NMSE':>8s} {'vs raw':>8s}  percentiles")
    print(f"  {'-'*20} {'-'*8} {'-'*8}  {'-'*30}")
    print(f"  {'raw (no smooth)':<20s} {nmse_raw:+8.2f}  {0:+8.2f}  {pct(pf_raw)}")

    results = [("raw", 0, nmse_raw)]

    # ── Fixed-window sweep ──
    for win in windows:
        H_smooth = freq_moving_average(H_s, active, win)
        nmse_w, pf_w = nmse_per_antenna(H_smooth, H_g, active)
        delta = nmse_w - nmse_raw
        label = f"mavg{win}"
        print(f"  {label:<20s} {nmse_w:+8.2f}  {delta:+8.2f}  {pct(pf_w)}")
        results.append((label, win, nmse_w))

    # ── Full-band average ──
    if args.full_band:
        aidx = np.where(active)[0]
        H_fb = H_s.copy()
        flat = H_fb.reshape(-1, H_fb.shape[-1])
        for row in range(flat.shape[0]):
            mean_val = np.mean(flat[row, aidx])
            flat[row, aidx] = mean_val
        H_fb = H_fb.reshape(H_s.shape)
        nmse_fb, pf_fb = nmse_per_antenna(H_fb, H_g, active)
        delta = nmse_fb - nmse_raw
        print(f"  {'full-band avg':<20s} {nmse_fb:+8.2f}  {delta:+8.2f}  {pct(pf_fb)}")
        results.append(("full-band", n_act, nmse_fb))

    # ── Adaptive ──
    if args.adaptive:
        print()
        print("=" * 60)
        print("  Adaptive window selection")
        print("=" * 60)

        H_adapt, per_frame_win, median_win = apply_adaptive_smooth(
            H_s, active, roughness, noise_est)
        nmse_a, pf_a = nmse_per_antenna(H_adapt, H_g, active)
        delta = nmse_a - nmse_raw

        win_vals, win_counts = np.unique(per_frame_win, return_counts=True)
        print(f"  median window: {median_win}")
        print(f"  window distribution:")
        for v, c in zip(win_vals, win_counts):
            print(f"    mavg{v}: {c} frames ({100*c/len(per_frame_win):.1f}%)")
        print(f"  NMSE adaptive:  {nmse_a:+.2f} dB  (vs raw: {delta:+.2f} dB)")
        print(f"  per-frame: {pct(pf_a)}")

        results.append(("adaptive", median_win, nmse_a))

    # ── Summary table ──
    print()
    print("=" * 60)
    print("  Summary: Window vs NMSE")
    print("=" * 60)
    for label, win, nmse in results:
        delta = nmse - nmse_raw
        print(f"  {label:<20s} win={win:<6d} NMSE={nmse:+.2f} dB  delta={delta:+.2f} dB")


if __name__ == "__main__":
    main()
