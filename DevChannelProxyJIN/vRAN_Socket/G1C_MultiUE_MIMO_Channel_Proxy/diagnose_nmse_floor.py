#!/usr/bin/env python3
"""
diagnose_nmse_floor.py — Deep diagnosis of the -7 dB NMSE floor.

Analyzes GT and SRS data to determine whether -7 dB comes from:
  (a) int16 pipeline precision (quantization noise)
  (b) filt8 interpolation error
  (c) GT-SRS normalization/format mismatch (evaluation artifact)

Key diagnostics:
  1. Per-subcarrier alpha(k): if |alpha(k)| varies across SC → normalization issue
  2. Per-subcarrier NMSE: which SCs have high error?
  3. Pilot vs midpoint NMSE: does filt8 interpolation matter?
  4. Amplitude histogram: how many int16 bits does SRS actually use?

Usage:
    python3 diagnose_nmse_floor.py --run-dir /path/to/snr_20dB
    python3 diagnose_nmse_floor.py --run-dir ~/OAI_luuuuuu/DevChannelProxyJIN/logs/static_legacy2_20260512_1152/snr_20dB
"""
import argparse
import sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from digital_twin_stats import load_srs_v2, load_gt, _apply_sto_correction, _estimate_sto_single


def detect_active(H_srs_frame):
    power = np.sum(np.abs(H_srs_frame) ** 2, axis=(0, 1))
    threshold = np.max(power) * 1e-6
    return np.where(power > threshold)[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--static", action="store_true",
                        help="Static channel mode: use average of ALL GT as single reference, "
                             "skip slot_id pairing (valid only for speed=0)")
    args = parser.parse_args()

    run_dir = args.run_dir
    gt_dir = str(Path(run_dir) / "sionna_gt")
    if not Path(gt_dir).exists():
        parent = Path(run_dir).parent
        gt_dir = str(parent / "sionna_gt")

    print(f"Loading SRS from: {run_dir}")
    H_srs_all, srs_meta = load_srs_v2(run_dir)
    srs_slots = srs_meta.get("abs_slots", None)
    print(f"  SRS: {H_srs_all.shape} frames, dtype={H_srs_all.dtype}")

    print(f"Loading GT from: {gt_dir}")
    H_gt_all, gt_slots = load_gt(gt_dir, return_slot_ids=True)
    print(f"  GT:  {H_gt_all.shape} frames, dtype={H_gt_all.dtype}")

    if args.static:
        # Static channel: H is the same every frame.
        # Use average GT as the single ground truth reference.
        H_gt_ref = np.mean(H_gt_all, axis=0)  # (rx, tx, sc)
        H_srs = H_srs_all
        H_gt = np.broadcast_to(H_gt_ref[np.newaxis], H_srs.shape).copy()
        print(f"  STATIC MODE: using average of {len(H_gt_all)} GT frames as reference")
        print(f"  All {len(H_srs)} SRS frames will be evaluated (no pairing needed)")
    elif srs_slots is not None and len(gt_slots) > 0:
        gt_slot_set = {int(s): i for i, s in enumerate(gt_slots)}
        paired_srs, paired_gt = [], []
        for si, ss in enumerate(srs_slots):
            ss_int = int(ss)
            if ss_int in gt_slot_set:
                paired_srs.append(si)
                paired_gt.append(gt_slot_set[ss_int])
        H_srs = H_srs_all[paired_srs]
        H_gt = H_gt_all[paired_gt]
        print(f"  Paired by slot_id: {len(paired_srs)} frames")
    else:
        n = min(len(H_srs_all), len(H_gt_all))
        H_srs = H_srs_all[:n]
        H_gt = H_gt_all[:n]
        print(f"  No slot IDs, using first {n} frames")

    if len(H_srs) == 0:
        print("ERROR: no frames to evaluate")
        return

    active_mask = np.zeros(H_srs.shape[-1], dtype=bool)
    active_idx = detect_active(H_srs[0])
    active_mask[active_idx] = True
    print(f"  Active SCs: {len(active_idx)} / {H_srs.shape[-1]}")

    n_fft = H_srs.shape[-1]
    from digital_twin_stats import estimate_sto

    # ── A) Global STO: one slope from all frames ──
    print(f"\n  [STO-A] Global STO (single slope from all frames)...")
    sto_samp_global, slope_global = estimate_sto(H_srs, H_gt, active_mask, n_fft)
    H_srs_global = _apply_sto_correction(H_srs, active_mask, slope_global, n_fft)
    print(f"  Global STO = {-sto_samp_global:+.2f} samples (slope={slope_global:.6f} rad/bin)")

    # ── B) Per-frame STO: one slope per frame ──
    print(f"  [STO-B] Per-frame STO...")
    H_srs_perframe = H_srs.copy()
    sto_slopes = []
    for fi in range(len(H_srs)):
        slope = _estimate_sto_single(H_srs[fi], H_gt[fi], active_mask, n_fft)
        sto_slopes.append(slope)
        H_srs_perframe[fi:fi+1] = _apply_sto_correction(
            H_srs[fi:fi+1], active_mask, slope, n_fft)
    sto_samples_pf = -np.array(sto_slopes) * n_fft / (2 * np.pi)
    print(f"  Per-frame STO: mean={np.mean(sto_samples_pf):+.2f}, "
          f"std={np.std(sto_samples_pf):.2f}, "
          f"range=[{np.min(sto_samples_pf):+.1f}, {np.max(sto_samples_pf):+.1f}]")

    # ── Compare A vs B ──
    def quick_nmse(H_s, H_g, active):
        s = H_s[:, :, :, active]
        g = H_g[:, :, :, active]
        sf = s.reshape(s.shape[0], -1)
        gf = g.reshape(g.shape[0], -1)
        gg = np.sum(gf.conj() * gf, axis=1)
        gs = np.sum(gf.conj() * sf, axis=1)
        alpha = gs / (gg + 1e-30)
        diff = s - alpha[:, None, None, None] * g
        ref = alpha[:, None, None, None] * g
        per_frame = []
        for i in range(len(s)):
            nmse_i = np.sum(np.abs(diff[i])**2) / (np.sum(np.abs(ref[i])**2) + 1e-30)
            per_frame.append(10*np.log10(nmse_i + 1e-30))
        pf = np.array(per_frame)
        return np.median(pf), np.percentile(pf, 10), np.percentile(pf, 90)

    p50_none, p10_none, p90_none = quick_nmse(H_srs, H_gt, active_idx)
    p50_global, p10_global, p90_global = quick_nmse(H_srs_global, H_gt, active_idx)
    p50_pf, p10_pf, p90_pf = quick_nmse(H_srs_perframe, H_gt, active_idx)

    print(f"\n  ┌─────────────────────────────────────────────────┐")
    print(f"  │ STO Mode       │   p10    │   p50    │   p90    │")
    print(f"  ├─────────────────────────────────────────────────┤")
    print(f"  │ No STO         │ {p10_none:+7.2f}  │ {p50_none:+7.2f}  │ {p90_none:+7.2f}  │")
    print(f"  │ Global STO     │ {p10_global:+7.2f}  │ {p50_global:+7.2f}  │ {p90_global:+7.2f}  │")
    print(f"  │ Per-frame STO  │ {p10_pf:+7.2f}  │ {p50_pf:+7.2f}  │ {p90_pf:+7.2f}  │")
    print(f"  └─────────────────────────────────────────────────┘")

    # Use per-frame corrected for remaining diagnostics
    active = active_idx
    n_sc = len(active)
    H_s = H_srs_perframe[:, :, :, active]
    H_g = H_gt[:, :, :, active]

    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("DIAGNOSTIC 1: Per-frame global alpha")
    print("=" * 70)
    s_flat = H_s.reshape(H_s.shape[0], -1)
    g_flat = H_g.reshape(H_g.shape[0], -1)
    gg = np.sum(g_flat.conj() * g_flat, axis=1)
    gs = np.sum(g_flat.conj() * s_flat, axis=1)
    alpha_global = gs / (gg + 1e-30)
    print(f"  |alpha| : median={np.median(np.abs(alpha_global)):.2f}, "
          f"std={np.std(np.abs(alpha_global)):.2f}, "
          f"range=[{np.min(np.abs(alpha_global)):.2f}, {np.max(np.abs(alpha_global)):.2f}]")
    print(f"  angle(α): std={np.std(np.angle(alpha_global)):.4f} rad "
          f"({np.degrees(np.std(np.angle(alpha_global))):.2f}°)")

    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("DIAGNOSTIC 2: Per-subcarrier alpha (frame-averaged)")
    print("=" * 70)
    # For each active SC, compute alpha across all frames and antennas
    alpha_per_sc = np.zeros(n_sc, dtype=np.complex128)
    for k in range(n_sc):
        s_k = H_s[:, :, :, k].ravel()
        g_k = H_g[:, :, :, k].ravel()
        alpha_per_sc[k] = np.vdot(g_k, s_k) / (np.vdot(g_k, g_k) + 1e-30)

    abs_alpha_sc = np.abs(alpha_per_sc)
    print(f"  |alpha(k)|: median={np.median(abs_alpha_sc):.4f}, "
          f"std={np.std(abs_alpha_sc):.4f}, "
          f"CoV={np.std(abs_alpha_sc)/np.median(abs_alpha_sc):.4f}")
    print(f"  range: [{np.min(abs_alpha_sc):.4f}, {np.max(abs_alpha_sc):.4f}]")
    print(f"  angle(α(k)) std: {np.std(np.angle(alpha_per_sc)):.4f} rad "
          f"({np.degrees(np.std(np.angle(alpha_per_sc))):.2f}°)")

    # If CoV > 0.01, there's per-SC normalization issue
    cov = np.std(abs_alpha_sc) / np.median(abs_alpha_sc)
    if cov > 0.05:
        print(f"  ⚠ CoV={cov:.4f} > 0.05 → significant per-SC scale variation!")
        print(f"    This means per-frame global alpha CANNOT fully align GT and SRS.")
        print(f"    The -7 dB floor likely includes this normalization artifact.")
    else:
        print(f"  ✓ CoV={cov:.4f} ≤ 0.05 → per-SC scale is flat, alpha alignment is OK.")

    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("DIAGNOSTIC 3: NMSE breakdown (global alpha vs per-SC alpha)")
    print("=" * 70)

    # NMSE with per-frame global alpha
    diff_global = H_s - alpha_global[:, None, None, None] * H_g
    nmse_global = np.mean(np.abs(diff_global) ** 2) / np.mean(np.abs(alpha_global[:, None, None, None] * H_g) ** 2)
    print(f"  NMSE (per-frame global α): {10 * np.log10(nmse_global + 1e-30):.2f} dB")

    # NMSE with per-SC alpha (best possible with linear correction)
    H_g_aligned_sc = np.zeros_like(H_g)
    for k in range(n_sc):
        H_g_aligned_sc[:, :, :, k] = alpha_per_sc[k] * H_g[:, :, :, k]
    diff_sc = H_s - H_g_aligned_sc
    nmse_sc = np.mean(np.abs(diff_sc) ** 2) / np.mean(np.abs(H_g_aligned_sc) ** 2)
    print(f"  NMSE (per-SC α):           {10 * np.log10(nmse_sc + 1e-30):.2f} dB")

    improvement = 10 * np.log10(nmse_global + 1e-30) - 10 * np.log10(nmse_sc + 1e-30)
    print(f"  Improvement from per-SC α: {improvement:.2f} dB")
    if improvement > 3:
        print(f"  ⚠ Large improvement → per-SC normalization mismatch IS a major floor contributor!")
    else:
        print(f"  ✓ Small improvement → per-SC normalization is NOT the main issue.")

    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("DIAGNOSTIC 4: Pilot vs Midpoint NMSE (filt8 contribution)")
    print("=" * 70)

    K_TC = 2
    pilot_indices = np.arange(0, n_sc, K_TC)
    mid_indices = np.arange(1, n_sc, K_TC)
    if len(mid_indices) > len(pilot_indices):
        mid_indices = mid_indices[:len(pilot_indices)]

    diff_g = H_s - alpha_global[:, None, None, None] * H_g
    pilot_err = np.mean(np.abs(diff_g[:, :, :, pilot_indices]) ** 2)
    mid_err = np.mean(np.abs(diff_g[:, :, :, mid_indices]) ** 2)
    pilot_pwr = np.mean(np.abs((alpha_global[:, None, None, None] * H_g)[:, :, :, pilot_indices]) ** 2)
    mid_pwr = np.mean(np.abs((alpha_global[:, None, None, None] * H_g)[:, :, :, mid_indices]) ** 2)

    nmse_pilot = pilot_err / (pilot_pwr + 1e-30)
    nmse_mid = mid_err / (mid_pwr + 1e-30)
    print(f"  Pilot NMSE:    {10 * np.log10(nmse_pilot + 1e-30):.2f} dB  (positions with LS data)")
    print(f"  Midpoint NMSE: {10 * np.log10(nmse_mid + 1e-30):.2f} dB  (interpolated by filt8)")
    print(f"  Difference:    {10 * np.log10(nmse_mid + 1e-30) - 10 * np.log10(nmse_pilot + 1e-30):.2f} dB")

    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("DIAGNOSTIC 5: SRS int16 dynamic range usage")
    print("=" * 70)

    H_srs_active = H_srs_all[:, :, :, active]
    real_vals = H_srs_active.real.ravel()
    imag_vals = H_srs_active.imag.ravel()
    all_vals = np.concatenate([real_vals, imag_vals])
    max_abs = np.max(np.abs(all_vals))
    rms = np.sqrt(np.mean(all_vals ** 2))
    effective_bits = np.log2(max_abs + 1) if max_abs > 0 else 0
    rms_bits = np.log2(rms + 1) if rms > 0 else 0

    print(f"  SRS int16 values: max|val|={max_abs:.0f}, rms={rms:.1f}")
    print(f"  Effective bits (peak): {effective_bits:.1f} / 15")
    print(f"  Effective bits (RMS):  {rms_bits:.1f} / 15")
    print(f"  Dynamic range used:    {20 * np.log10(max_abs / 32767):.1f} dB below full scale")

    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("DIAGNOSTIC 6: Per-frame NMSE distribution")
    print("=" * 70)

    per_frame_nmse = []
    for i in range(len(H_s)):
        d = H_s[i] - alpha_global[i] * H_g[i]
        r = alpha_global[i] * H_g[i]
        nmse_i = np.sum(np.abs(d[..., :]) ** 2) / (np.sum(np.abs(r[..., :]) ** 2) + 1e-30)
        per_frame_nmse.append(10 * np.log10(nmse_i + 1e-30))
    pf = np.array(per_frame_nmse)
    print(f"  p10 = {np.percentile(pf, 10):.2f} dB")
    print(f"  p50 = {np.percentile(pf, 50):.2f} dB")
    print(f"  p90 = {np.percentile(pf, 90):.2f} dB")
    print(f"  std = {np.std(pf):.2f} dB")

    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Overall NMSE (global α):  {10 * np.log10(nmse_global + 1e-30):.2f} dB")
    print(f"  Overall NMSE (per-SC α):  {10 * np.log10(nmse_sc + 1e-30):.2f} dB")
    print(f"  Per-SC α improvement:     {improvement:.2f} dB")
    print(f"  Pilot vs Mid difference:  {10 * np.log10(nmse_mid + 1e-30) - 10 * np.log10(nmse_pilot + 1e-30):.2f} dB")
    print(f"  SRS effective bits:       {effective_bits:.1f} / 15")
    print()
    if improvement > 3:
        print("  → Main floor source: GT-SRS per-SC normalization mismatch")
        print("  → Fix: use per-SC alpha in evaluation, or fix GT/SRS format alignment")
    elif 10 * np.log10(nmse_mid + 1e-30) - 10 * np.log10(nmse_pilot + 1e-30) > 3:
        print("  → Main floor source: filt8 midpoint interpolation error")
        print("  → Fix: optimized interpolation coefficients (方案 B/A)")
    elif effective_bits < 8:
        print("  → Main floor source: int16 dynamic range (only {:.0f} bits used)".format(effective_bits))
        print("  → Fix: adjust Proxy scaling or OAI FFT normalization")
    else:
        print("  → Floor source unclear from this diagnostic")
        print("  → Need: passthru test in static channel, or per-SC residual analysis")


if __name__ == "__main__":
    main()
