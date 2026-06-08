#!/usr/bin/env python3
"""offline_freq_denoise.py - OFFLINE proof of the "frequency-domain (delay-domain)
denoising" gain for MIMO SRS, BEFORE touching the C estimator.

Background (see 0607_MIMO增强_多符号与频域去噪分析日志.md §2):
  The C dump (`srs_matrix_gNB_*`) is the FILTERED estimate `srs_estimated_channel_freq`
  (post freq-Wiener Stage1 + PKF), NOT raw LS. The true channel occupies only ~4%
  of the delay range; the other ~96% of delay taps are pure noise that the current
  16-pilot local Toeplitz window does NOT zero out. A GLOBAL delay-domain support
  cut recovers ~+2.9/+2.7 dB on 2x2/4x4 (§2.3).

  §2.5: a FIXED hard window is not universally safe (it clips long delay spreads and
  hurts 1x1). The safe version must find the support from a CROSS-FRAME EMA PDP
  (alpha 0.95) + robust noise floor (far-tail 25th pct) -- exactly the state already
  living inside nr_srs_mmse.c (g_true2d_pdp_ema). This script prototypes BOTH so we
  can prove the gain + the 1x1 safety in Python first, then port the EMA-adaptive
  version into the freq-Wiener.

This denoises the dumped (already filtered) estimate, so the measured gain is a
CONSERVATIVE lower bound of what the same cut achieves inside Stage1 on the LS.

Modes:
  --mode hardwin : sweep fixed delay window L (keep |n|<=L), reproduce §2.3 scan.
  --mode ema     : causal cross-frame EMA-PDP adaptive support cut (the C-bound algo).

Usage:
  python3 offline_freq_denoise.py --run-dir <run> --mode hardwin
  python3 offline_freq_denoise.py --run-dir <run> --mode ema [--alpha 0.95] [--margin 3] [--guard 2]
  python3 offline_freq_denoise.py --ab3 <1x1run> <2x2run> <4x4run> --mode ema
"""
from __future__ import annotations
import argparse
import numpy as np

from eval_nmse_clean import (load_srs, index_gt_slots, load_gt_by_refs,
                             get_active_mask, align_frames, unify_reference_frame)
from eval_nmse_sto import _sto_coarse, _sto_es_adaptive, SEL_MIN

EPS = 1e-30


# ────────────────────────────────────────────────────────────────────
# Data loading (mirrors eval_nmse_sto.evaluate front-half: pair + cdl-filter)
# ────────────────────────────────────────────────────────────────────
def load_paired(run_dir, srs_symbol=12, tol=20):
    H, meta = load_srs(run_dir)
    gt_slots, gt_refs = index_gt_slots(run_dir + "/sionna_gt", srs_symbol=srs_symbol)
    if "sample_slots" in meta:
        si, gi, _ = unify_reference_frame(meta["sample_slots"], gt_slots)
    else:
        si, gi, _ = align_frames(meta["abs_slots"], gt_slots, tol=tol)
    if len(si) < 2:
        raise SystemExit("too few paired frames")
    H_s, H_g = H[si], load_gt_by_refs(gt_refs, gi)
    active = get_active_mask(H_s)
    aidx = np.where(active)[0]
    # keep only frequency-selective (post-handoff CDL) GT frames
    g_sel = (np.std(np.abs(H_g[:, :, :, aidx]), axis=-1)
             / (np.mean(np.abs(H_g[:, :, :, aidx]), axis=-1) + EPS)).mean(axis=(1, 2))
    cdl = g_sel > SEL_MIN
    if cdl.sum() >= 2:
        H_s, H_g = H_s[cdl], H_g[cdl]
    return H_s, H_g, aidx, meta["n_sc"]


# ────────────────────────────────────────────────────────────────────
# NMSE (STO+scalar aligned, identical aggregation to eval_nmse_sto)
# ────────────────────────────────────────────────────────────────────
def nmse_sto(H_s, H_g, aidx, n_fft):
    F, R, T = H_s.shape[0], H_s.shape[1], H_s.shape[2]
    err = np.zeros((R, T, F)); sig = np.zeros((R, T, F))
    for f in range(F):
        for rx in range(R):
            for tx in range(T):
                s = H_s[f, rx, tx, aidx]
                g = H_g[f, rx, tx, aidx]
                if np.linalg.norm(s) <= EPS or np.linalg.norm(g) <= EPS:
                    continue
                e, sg, _ = _sto_es_adaptive(s, g, n_fft, aidx)
                err[rx, tx, f], sig[rx, tx, f] = e, sg
    overall = 10 * np.log10(np.mean(err) / (np.mean(sig) + EPS) + EPS)
    per_ant = 10 * np.log10(np.mean(err, axis=2) / (np.mean(sig, axis=2) + EPS) + EPS)
    return overall, per_ant


# ────────────────────────────────────────────────────────────────────
# Delay-domain transforms (mirror diag_mimo_delay: zero-fill full grid -> ifft)
# ────────────────────────────────────────────────────────────────────
def to_delay(H_link, aidx, n_fft):
    """H_link [F, n_fft] (active populated) -> delay [F, n_fft] complex."""
    full = np.zeros((H_link.shape[0], n_fft), dtype=np.complex128)
    full[:, aidx] = H_link[:, aidx]
    return np.fft.ifft(full, axis=1)


def from_delay(h_time, aidx):
    """delay [F, n_fft] -> freq, re-extract active bins -> [F, n_fft] (active populated)."""
    full = np.fft.fft(h_time, axis=1)
    out = np.zeros_like(full)
    out[:, aidx] = full[:, aidx]
    return out


# ────────────────────────────────────────────────────────────────────
# (a) Fixed hard-window denoise: keep |n|<=L (incl. negative-delay wrap)
# ────────────────────────────────────────────────────────────────────
def denoise_hardwin(H_s, aidx, n_fft, L):
    F, R, T = H_s.shape[0], H_s.shape[1], H_s.shape[2]
    n = np.arange(n_fft)
    keep = (n <= L) | (n >= n_fft - L)
    out = np.zeros_like(H_s)
    for rx in range(R):
        for tx in range(T):
            ht = to_delay(H_s[:, rx, tx, :], aidx, n_fft)
            ht[:, ~keep] = 0.0
            out[:, rx, tx, :] = from_delay(ht, aidx)
    return out


# ────────────────────────────────────────────────────────────────────
# (b) EMA-PDP adaptive support denoise  (== the algorithm to port into C)
#   - causal EMA of |h_time|^2 across frames (alpha, C default 0.95)
#   - robust noise floor = far-tail [3N/4,N) 25th percentile (C nr_srs_estimate_noise_floor)
#   - support = bins where EMA > margin*noise_floor, dilated by `guard` bins
#   - per-frame: zero delay taps outside support, transform back
# ────────────────────────────────────────────────────────────────────
def robust_noise_floor(ema_pdp, n_fft):
    tail = ema_pdp[(3 * n_fft) // 4:]          # past dominant K_TC alias
    return max(1e-12, float(np.percentile(tail, 25)))


def dilate(mask, guard):
    if guard <= 0:
        return mask
    out = mask.copy()
    for s in range(1, guard + 1):
        out |= np.roll(mask, s) | np.roll(mask, -s)
    return out


def midband_noise(ema_pdp, n_fft):
    """Clean noise-power estimate: MEAN over the mid-delay band [N/2, 3N/4). Clear of
    the channel near n=0 AND its negative-delay wrap near n=N. O(N), no sort. == C."""
    return max(1e-12, float(ema_pdp[n_fft // 2:(3 * n_fft) // 4].mean()))


def denoise_ema(H_s, aidx, n_fft, alpha=0.95, margin=3.0, guard=2,
                peak_db=None, gamma=None, energy_frac=None, report=False, quantize=False):
    """Causal cross-frame EMA-PDP support cut.

    Threshold modes (both SNR-adaptive, both portable to C):
      - peak_db (preferred): keep delay bins with EMA >= peak_EMA * 10^(-peak_db/10),
        i.e. within `peak_db` dB of the strongest tap. Peak-relative => one constant
        works across 1x1/2x2/4x4 and degrades gracefully (never over-cuts the main lobe).
      - margin (fallback/legacy): keep bins with EMA > margin * robust_noise_floor.
    A floor guard (always-on) additionally requires EMA > 2*noise_floor so a peak-relative
    cut can never resurrect pure-noise bins on a very flat (low-delay-spread) channel.
    """
    F, R, T = H_s.shape[0], H_s.shape[1], H_s.shape[2]
    out = np.zeros_like(H_s)
    supp_sizes = []
    for rx in range(R):
        for tx in range(T):
            ht = to_delay(H_s[:, rx, tx, :], aidx, n_fft)   # [F, n_fft]
            if quantize:
                # mimic OAI's int16 (c16) idft output: round + clip to int16. The
                # masked-out (small/noise) taps are zeroed anyway; the kept (large)
                # support taps survive quantization -> tests if the gain survives fixed point.
                ht = (np.clip(np.rint(ht.real), -32768, 32767)
                      + 1j * np.clip(np.rint(ht.imag), -32768, 32767))
            p = np.abs(ht) ** 2
            ema = p[0].copy()
            ht_dn = np.empty_like(ht)
            for f in range(F):
                if f > 0:
                    ema = alpha * ema + (1.0 - alpha) * p[f]
                if energy_frac is not None:
                    # Energy-cumulative: keep the fewest (strongest) taps capturing
                    # energy_frac of total EMA power. Adapts to delay-spread & SNR;
                    # constant = energy fraction (interpretable, no dB).
                    order = np.argsort(ema)[::-1]
                    cum = np.cumsum(ema[order])
                    tot = cum[-1] + 1e-30
                    ncut = int(np.searchsorted(cum, energy_frac * tot)) + 1
                    mask = np.zeros(n_fft, dtype=bool)
                    mask[order[:ncut]] = True
                elif gamma is not None:
                    # CFAR: thr = gamma * mid-band noise mean. SNR + delay-spread
                    # adaptive, gamma = detection threshold (no magic dB).
                    thr = gamma * midband_noise(ema, n_fft)
                    mask = ema >= thr
                elif peak_db is not None:
                    nf = robust_noise_floor(ema, n_fft)
                    thr = ema.max() * (10.0 ** (-peak_db / 10.0))
                    mask = (ema >= thr) & (ema > 2.0 * nf)
                else:
                    nf = robust_noise_floor(ema, n_fft)
                    mask = ema > (margin * nf)
                mask = dilate(mask, guard)
                supp_sizes.append(int(mask.sum()))
                row = ht[f].copy()
                row[~mask] = 0.0
                ht_dn[f] = row
            out[:, rx, tx, :] = from_delay(ht_dn, aidx)
    if report:
        ss = np.array(supp_sizes)
        thr_desc = (f"efrac={energy_frac}" if energy_frac is not None
                    else (f"gamma={gamma}" if gamma is not None
                          else (f"peak_db={peak_db}" if peak_db is not None else f"margin={margin}")))
        print(f"    [ema] support taps: median={np.median(ss):.0f} "
              f"p10/p90={np.percentile(ss,10):.0f}/{np.percentile(ss,90):.0f} "
              f"(of {n_fft}; alpha={alpha} {thr_desc} guard={guard})")
    return out


# ────────────────────────────────────────────────────────────────────
def run_one(run_dir, mode, args):
    H_s, H_g, aidx, n_fft = load_paired(run_dir, args.srs_symbol, args.tol)
    F, R, T = H_s.shape[0], H_s.shape[1], H_s.shape[2]
    base, _ = nmse_sto(H_s, H_g, aidx, n_fft)
    print(f"\n=== {run_dir} ===")
    print(f"  {F} frames, {R}x{T}, n_fft={n_fft}, active_sc={len(aidx)}")
    print(f"  baseline NMSE (filtered dump): {base:+.2f} dB")

    if mode == "hardwin":
        print(f"  {'L':>5} {'NMSE':>9} {'gain':>8}")
        best = (None, -1e9)
        for L in args.windows:
            Hd = denoise_hardwin(H_s, aidx, n_fft, L)
            nm, _ = nmse_sto(Hd, H_g, aidx, n_fft)
            g = base - nm
            flag = "  <-- best" if g > best[1] else ""
            if g > best[1]:
                best = (L, g)
            print(f"  {L:>5} {nm:>+9.2f} {g:>+8.2f}{flag}")
        print(f"  best: L={best[0]}  gain={best[1]:+.2f} dB")
        return base, best

    elif mode == "ema":
        Hd = denoise_ema(H_s, aidx, n_fft, args.alpha, args.margin, args.guard,
                         peak_db=args.peak_db, gamma=args.gamma,
                         energy_frac=args.energy_frac, report=True,
                         quantize=args.quantize)
        nm, pa = nmse_sto(Hd, H_g, aidx, n_fft)
        g = base - nm
        verdict = "GAIN" if g > 0.1 else ("SAFE(no harm)" if g > -0.2 else "HARM!")
        print(f"  EMA-denoised NMSE            : {nm:+.2f} dB   (gain {g:+.2f} dB) [{verdict}]")
        for rx in range(R):
            for tx in range(T):
                print(f"      rx{rx}tx{tx}: {pa[rx,tx]:+.2f} dB")
        return base, (nm, g)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir")
    ap.add_argument("--ab3", nargs=3, metavar=("R1x1", "R2x2", "R4x4"),
                    help="three runs to compare (1x1, 2x2, 4x4)")
    ap.add_argument("--mode", choices=["hardwin", "ema"], default="ema")
    ap.add_argument("--srs-symbol", type=int, default=12)
    ap.add_argument("--tol", type=int, default=20)
    ap.add_argument("--windows", type=int, nargs="+",
                    default=[32, 48, 64, 96, 128, 256],
                    help="hardwin: delay window L values to sweep")
    ap.add_argument("--alpha", type=float, default=0.95, help="ema: EMA PDP coeff (C=0.95)")
    ap.add_argument("--margin", type=float, default=3.0,
                    help="ema: legacy support threshold = margin * noise_floor")
    ap.add_argument("--peak-db", type=float, default=None, dest="peak_db",
                    help="ema: peak-relative threshold (keep bins within X dB of peak).")
    ap.add_argument("--gamma", type=float, default=None,
                    help="ema: CFAR threshold = gamma * mid-band noise mean.")
    ap.add_argument("--energy-frac", type=float, default=None, dest="energy_frac",
                    help="ema: keep fewest strongest taps capturing this energy fraction "
                         "(e.g. 0.99). Adapts to delay-spread/SNR; constant=energy frac.")
    ap.add_argument("--guard", type=int, default=2, help="ema: dilate support by N bins")
    ap.add_argument("--quantize", action="store_true",
                    help="ema: round delay taps to int16 (mimic OAI c16 idft) -> "
                         "tests if the gain survives fixed-point round-trip")
    args = ap.parse_args()

    runs = []
    if args.run_dir:
        runs = [args.run_dir]
    elif args.ab3:
        runs = list(args.ab3)
    else:
        ap.error("provide --run-dir or --ab3")

    for rd in runs:
        run_one(rd, args.mode, args)


if __name__ == "__main__":
    raise SystemExit(main())
