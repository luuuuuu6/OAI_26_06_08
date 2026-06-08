#!/usr/bin/env python3
"""diag_mimo_delay.py - WHERE does the 2-port SRS error live (delay domain)?

Decisive diagnostic for the "2-port costs ~16 dB vs 1-port" question. For each
link it STO+scalar aligns the C SRS estimate to the GT (same as eval_nmse_sto),
forms the residual r = s - a*g_aligned, and maps s / residual onto the full FFT
grid -> delay (IDFT). It then splits the RESIDUAL energy into delay bands:

  * LOW   |n| <= low        : genuine channel / interpolation tail
  * NYQ   |n - N/2| <= half : period-2 "staircase" alias signature. A
                              zero-order-hold across each FD-CDM2 pilot pair
                              replicates spectrum at Fs/2 -> energy at delay N/2.
  * MID   everything else

Reading:
  - NYQ fraction HIGH on 2x2 but ~0 on 1x1  -> structural FD-CDM staircase alias
                                                (fixable in gNB interpolation).
  - residual ~ white (flat across all bands) -> SNR/noise dominated (NOT a PHY
                                                bug; -3 dB power split + noise).
"""
from __future__ import annotations
import argparse
import numpy as np

from eval_nmse_clean import (load_srs, index_gt_slots, load_gt_by_refs,
                             get_active_mask, align_frames, unify_reference_frame)
from eval_nmse_sto import _sto_coarse, SEL_MIN

EPS = 1e-30


def _load_paired(run_dir, srs_symbol, tol):
    H, meta = load_srs(run_dir)
    gt_slots, gt_refs = index_gt_slots(run_dir + "/sionna_gt", srs_symbol=srs_symbol)
    if "sample_slots" in meta:
        si, gi, gap = unify_reference_frame(meta["sample_slots"], gt_slots)
    else:
        si, gi, gap = align_frames(meta["abs_slots"], gt_slots, tol=tol)
    H_s, H_g = H[si], load_gt_by_refs(gt_refs, gi)
    active = get_active_mask(H_s)
    aidx = np.where(active)[0]
    g_sel = (np.std(np.abs(H_g[:, :, :, aidx]), axis=-1)
             / (np.mean(np.abs(H_g[:, :, :, aidx]), axis=-1) + EPS)).mean(axis=(1, 2))
    cdl = g_sel > SEL_MIN
    if cdl.sum() >= 2:
        H_s, H_g = H_s[cdl], H_g[cdl]
    return H_s, H_g, aidx, meta["n_sc"]


def diag(run_dir, srs_symbol=12, tol=20, low=8, half=8):
    H_s, H_g, aidx, n_fft = _load_paired(run_dir, srs_symbol, tol)
    F, R, T = H_s.shape[0], H_s.shape[1], H_s.shape[2]
    k = aidx.astype(np.float64)
    nyq = n_fft // 2
    n = np.arange(n_fft)
    dist_nyq = np.minimum(np.abs(n - nyq), np.abs(n - nyq))  # distance to Nyquist
    low_mask = (n <= low) | (n >= n_fft - low)
    nyq_mask = np.abs(n - nyq) <= half

    print(f"\n=== {run_dir} ===")
    print(f"  {F} frames, {R}x{T} links, n_fft={n_fft}, active_sc={len(aidx)}, nyq_bin={nyq}")
    for rx in range(R):
        for tx in range(T):
            res_prof = np.zeros(n_fft)
            sig_prof = np.zeros(n_fft)
            etot = stot = 0.0
            # cross-port leakage accumulators: how much of the residual on this link
            # correlates with the OTHER tx port's (aligned) GT on the same rx.
            leak_num = {ox: 0.0 for ox in range(T) if ox != tx}
            leak_den_r = 0.0
            leak_den_g = {ox: 0.0 for ox in range(T) if ox != tx}
            for f in range(F):
                s = H_s[f, rx, tx, aidx]
                g = H_g[f, rx, tx, aidx]
                if np.linalg.norm(s) <= EPS or np.linalg.norm(g) <= EPS:
                    continue
                tau = _sto_coarse(s, g, n_fft, aidx)
                gg = g * np.exp(-2j * np.pi * tau * k / n_fft)
                a = np.vdot(gg, s) / (np.vdot(gg, gg) + EPS)
                r = s - a * gg
                etot += float(np.mean(np.abs(r) ** 2))
                stot += float(np.mean(np.abs(a * gg) ** 2))
                Sfull = np.zeros(n_fft, dtype=np.complex128); Sfull[aidx] = s
                Rfull = np.zeros(n_fft, dtype=np.complex128); Rfull[aidx] = r
                sig_prof += np.abs(np.fft.ifft(Sfull)) ** 2
                res_prof += np.abs(np.fft.ifft(Rfull)) ** 2
                leak_den_r += float(np.vdot(r, r).real)
                for ox in leak_num:
                    go = H_g[f, rx, ox, aidx]
                    if np.linalg.norm(go) <= EPS:
                        continue
                    leak_num[ox] += np.vdot(go, r)            # complex
                    leak_den_g[ox] += float(np.vdot(go, go).real)
            nmse = 10 * np.log10(etot / (stot + EPS) + EPS)
            rtot = res_prof.sum() + EPS
            f_low = res_prof[low_mask].sum() / rtot
            f_nyq = res_prof[nyq_mask].sum() / rtot
            f_mid = 1.0 - f_low - f_nyq
            s_low = sig_prof[low_mask].sum() / (sig_prof.sum() + EPS)
            leak_str = "  ".join(
                f"leak<-tx{ox}={abs(leak_num[ox]) / (np.sqrt(leak_den_r * leak_den_g[ox]) + EPS):.3f}"
                for ox in leak_num)
            print(f"  rx{rx}tx{tx}: NMSE={nmse:+6.2f} dB | residual delay: "
                  f"LOW(|n|<={low})={100*f_low:4.1f}%  NYQ(|n-{nyq}|<={half})={100*f_nyq:4.1f}%  "
                  f"MID={100*f_mid:4.1f}%   [sig LOW={100*s_low:.0f}%]  {leak_str}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dirs", nargs="+", help="one or more capture run dirs (e.g. 1x1 and 2x2)")
    ap.add_argument("--srs-symbol", type=int, default=12)
    ap.add_argument("--low", type=int, default=8)
    ap.add_argument("--half", type=int, default=8)
    a = ap.parse_args()
    for rd in a.run_dirs:
        diag(rd, a.srs_symbol, low=a.low, half=a.half)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
