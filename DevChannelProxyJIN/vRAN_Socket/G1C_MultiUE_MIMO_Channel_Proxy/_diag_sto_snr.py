#!/usr/bin/env python3
"""Proper GT<->SRS alignment using the repo's own STO machinery
(digital_twin_stats.estimate_sto + compute_estimation_snr), which the
eval_nmse_clean / eval_freq_smooth NMSE path does NOT apply. Reports estimation
SNR (higher=better; NMSE ~ -SNR) before and after STO correction, legacy vs true2d."""
from __future__ import annotations
import sys
import numpy as np
from eval_nmse_clean import load_srs, align_frames, get_active_mask
from digital_twin_stats import index_gt_slots, load_gt_by_refs, estimate_sto, compute_estimation_snr


def run(rd):
    H, meta = load_srs(rd)
    gs, gr = index_gt_slots(rd + "/sionna_gt")
    si, gi, _ = align_frames(meta["abs_slots"], gs, tol=20)
    H_s, H_g = H[si], load_gt_by_refs(gr, gi)
    active = get_active_mask(H_s)
    n_fft = meta["n_sc"]
    F = H_s.shape[0]

    sto_samples, slope = estimate_sto(H_s, H_g, active, n_fft)
    # no-correction baseline
    _, _, _, snr0, a0 = compute_estimation_snr(H_s, H_g, active, 0.0, n_fft)
    # STO-corrected (try both signs, keep the better — sign convention safety)
    _, _, pair_p, snr_p, _ = compute_estimation_snr(H_s, H_g, active, slope, n_fft)
    _, _, pair_m, snr_m, _ = compute_estimation_snr(H_s, H_g, active, -slope, n_fft)
    if snr_m > snr_p:
        snr_c, pair = snr_m, pair_m
    else:
        snr_c, pair = snr_p, pair_p
    return dict(F=F, sto=sto_samples, snr0=snr0, snr_c=snr_c, pair=pair, alpha=a0)


def main():
    print(f"{'run':>8}  frames  STO(samp)  SNR(no-corr)  SNR(STO-corr)   [NMSE=-SNR]")
    for tag, rd in [("legacy", sys.argv[1]), ("true2d", sys.argv[2])]:
        r = run(rd)
        print(f"{tag:>8}  {r['F']:6d}  {r['sto']:+8.2f}   {r['snr0']:+8.2f} dB   "
              f"{r['snr_c']:+8.2f} dB   (NMSE {-r['snr_c']:+.2f} dB)")
        pp = r["pair"]
        flat = "  ".join(f"rx{i}tx{j}={pp[i,j]:+.1f}" for i in range(pp.shape[0]) for j in range(pp.shape[1]))
        print(f"          per-pair SNR(corr): {flat}")


if __name__ == "__main__":
    main()
