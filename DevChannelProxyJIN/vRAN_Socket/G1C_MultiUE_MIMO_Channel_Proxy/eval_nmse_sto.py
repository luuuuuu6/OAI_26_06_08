#!/usr/bin/env python3
"""eval_nmse_sto.py - SRS vs GT NMSE with per-frame STO (timing-offset) alignment.

eval_nmse_clean.py / eval_freq_smooth.py align SRS to GT with only a per-frame
complex SCALAR (LS alpha). On a multipath/CDL channel the OAI SRS estimate and
the Sionna GT differ by a sample-timing offset (STO) = a linear phase ramp
across the FFT bins, which a single scalar cannot remove -> NMSE blows up
positive (measured +12..+14 dB on cdl_a even at SNR25). This tool additionally
estimates and removes a per-frame, per-antenna STO (linear phase in the true
FFT bin index) before the scalar, which is the correct alignment for a delayed
channel (validated to pull cdl_a legacy from +14.7 to +2.1 dB).

It reports BOTH the scalar-only NMSE (== eval_nmse_clean) and the STO-aligned
NMSE per antenna, so legacy vs true2d can be compared fairly under
--srs-only-channel runs.

Usage:
  python3 eval_nmse_sto.py --run-dir logs/.../snr_25dB
  python3 eval_nmse_sto.py --ab logs/.../legacy/snr_25dB logs/.../true2d/snr_25dB
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

import numpy as np

from eval_nmse_clean import (load_srs, index_gt_slots, align_frames,
                             unify_reference_frame,
                             load_gt_by_refs, get_active_mask)

EPS = 1e-30
# Same-physical-slot pairing must yield high COMPLEX coherence |<s,g>|/(||s||||g||)
# between SRS and GT (a constant STO/linear-phase does NOT reduce it). A correct
# pairing sits high (~0.9 observed); well below this means the pairing/reference
# is wrong. (Magnitude-envelope corr was abandoned: it is undefined on flat GT.)
CORR_MIN = 0.50
# GT[srs_symbol] frequency-selectivity (std/mean of |H| across SC) below which a
# frame is treated as FLAT (pre-handoff, SRS-only mask not yet on CDL) and dropped.
SEL_MIN = 0.05


def _scalar_es(s, g):
    """Return (err, sig) after optimal complex-scalar alignment (== eval_nmse_clean)."""
    a = np.vdot(g, s) / (np.vdot(g, g) + EPS)
    return float(np.mean(np.abs(s - a * g) ** 2)), float(np.mean(np.abs(a * g) ** 2))


def _sto_es(s, g, ramps):
    """Return (err, sig, best_tau_idx): best over a grid of STO ramps applied to g
    (true-bin linear phase), each followed by an optimal complex scalar."""
    gg = g[None, :] * ramps                      # [n_tau, K]
    a = (gg.conj() @ s) / (np.sum(np.abs(gg) ** 2, axis=1) + EPS)  # [n_tau]
    err = np.mean(np.abs(s[None, :] - a[:, None] * gg) ** 2, axis=1)
    sig = np.mean(np.abs(a[:, None] * gg) ** 2, axis=1)
    j = int(np.argmin(err / (sig + EPS)))
    return float(err[j]), float(sig[j]), j


def _sto_coarse(s, g, n_fft, kidx):
    """Coarse integer STO with NO fixed search window: a pure timing offset is a
    linear phase across subcarriers, so the cross-spectrum s*conj(g) IDFT'd to the
    delay domain peaks exactly at the offset. argmax is unambiguous over
    [-n_fft/2, n_fft/2) -> handles any timing offset without a magic max range."""
    cross = np.zeros(n_fft, dtype=np.complex128)
    cross[kidx] = s * np.conj(g)
    prof = np.abs(np.fft.ifft(cross))
    n0 = int(np.argmax(prof))
    return float(n0 if n0 <= n_fft // 2 else n0 - n_fft)


def _sto_es_adaptive(s, g, n_fft, kidx, fine_half=3.0, fine_step=0.25):
    """Adaptive STO+scalar: coarse delay-peak estimate (any offset, no window) +
    a small fine grid around it. Returns (err, sig, tau)."""
    tau0 = _sto_coarse(s, g, n_fft, kidx)
    taus = tau0 + np.arange(-fine_half, fine_half + fine_step, fine_step)
    gg = g[None, :] * np.exp(-2j * np.pi * np.outer(taus, kidx) / n_fft)
    a = (gg.conj() @ s) / (np.sum(np.abs(gg) ** 2, axis=1) + EPS)
    err = np.mean(np.abs(s[None, :] - a[:, None] * gg) ** 2, axis=1)
    sig = np.mean(np.abs(a[:, None] * gg) ** 2, axis=1)
    j = int(np.argmin(err / (sig + EPS)))
    return float(err[j]), float(sig[j]), float(taus[j])


def evaluate(run_dir: str, srs_symbol: int, tol: int,
             tau_range: float, tau_step: float, predict_horizon: int = 0):
    H, meta = load_srs(run_dir)
    gt_slots, gt_refs = index_gt_slots(run_dir + "/sionna_gt", srs_symbol=srs_symbol)
    # Direction A: when the capture dumped the k-step PREDICTION (gNB env
    # SRS_PKF_PREDICT_AHEAD=k), SRS[n] holds the predicted H(n+k), so shift the
    # SRS slot axis forward by k*SRS_period to pair each prediction with its
    # FUTURE GT slot. 0 = standard on-time evaluation.
    if predict_horizon > 0:
        axis_key = "sample_slots" if "sample_slots" in meta else "abs_slots"
        ax = np.sort(meta[axis_key])
        period = int(np.median(np.diff(ax))) if ax.size > 1 else 1
        period = max(period, 1)
        shift = predict_horizon * period
        meta[axis_key] = meta[axis_key] + shift
        print(f"  [predict] horizon k={predict_horizon}, SRS_period={period} slots "
              f"-> shift SRS axis +{shift} to score vs FUTURE GT")
    # V3 SRS carries an absolute sample-slot id in the SAME axis as the GT slot
    # ids -> pair exactly (no per-run median-C guess). V2 falls back to legacy.
    if "sample_slots" in meta:
        si, gi, gap = unify_reference_frame(meta["sample_slots"], gt_slots)
        align_mode = "unified-V3"
    else:
        si, gi, gap = align_frames(meta["abs_slots"], gt_slots, tol=tol)
        align_mode = "legacy-medianC"
    if len(si) < 2:
        raise SystemExit("too few paired frames")
    H_s, H_g = H[si], load_gt_by_refs(gt_refs, gi)
    active = get_active_mask(H_s)
    aidx = np.where(active)[0]

    # Drop pre-handoff frames where GT[srs_symbol] is still FLAT (the SRS-only
    # mask flattens every symbol until the CDL handoff fires). A flat GT carries
    # no frequency selectivity -> nothing for the estimator to exploit and it
    # makes the |.| correlation meaningless. Keep only frames whose GT magnitude
    # varies across SC (std/mean > sel_min): those are the post-handoff CDL frames.
    g_sel = (np.std(np.abs(H_g[:, :, :, aidx]), axis=-1)
             / (np.mean(np.abs(H_g[:, :, :, aidx]), axis=-1) + EPS)).mean(axis=(1, 2))
    cdl_mask = g_sel > SEL_MIN
    n_cdl = int(cdl_mask.sum())
    if n_cdl >= 2:
        H_s, H_g = H_s[cdl_mask], H_g[cdl_mask]
        print(f"  [cdl-filter] kept {n_cdl}/{len(cdl_mask)} frequency-selective GT frames "
              f"(dropped {len(cdl_mask) - n_cdl} flat pre-handoff frames; sel>{SEL_MIN})")
    else:
        print(f"  [cdl-filter] WARNING: only {n_cdl} CDL frames (GT mostly FLAT, "
              f"handoff likely never fired) -> measuring a flat channel, keeping all")

    n_fft = meta["n_sc"]
    R, T, F = meta["rx"], meta["tx"], H_s.shape[0]
    k = aidx.astype(np.float64)
    # STO alignment method: adaptive (tau_range<=0, default) estimates the offset
    # per frame from the cross-spectrum delay peak (no fixed window -> any offset,
    # multi-modal jumps handled); legacy fixed grid (tau_range>0) for reproducibility.
    adaptive = (tau_range <= 0.0)
    if not adaptive:
        taus = np.arange(-tau_range, tau_range + tau_step, tau_step)
        ramps = np.exp(-2j * np.pi * np.outer(taus, k) / n_fft)   # [n_tau, K]

    # gap=0 same-slot COMPLEX coherence at the best STO (the gate). A multipath STO
    # is a benign linear phase ramp that the NMSE below removes; computing |<s,g>|
    # WITHOUT removing it understates alignment, so the gate uses the SAME best-STO
    # alignment as the NMSE -> reflects true alignment, still rejects wrong pairings.
    coh = []
    for f in range(F):
        for rx in range(R):
            for tx in range(T):
                a = H_s[f, rx, tx, aidx]
                b = H_g[f, rx, tx, aidx]
                d = (np.linalg.norm(a) * np.linalg.norm(b))
                if d <= EPS:
                    continue
                if adaptive:
                    tau0 = _sto_coarse(a, b, n_fft, aidx)
                    bb = b * np.exp(-2j * np.pi * tau0 * k / n_fft)
                    coh.append(float(np.abs(np.vdot(bb, a)) / d))
                else:
                    bb = b[None, :] * ramps                  # [n_tau, K]
                    coh.append(float(np.max(np.abs(bb.conj() @ a)) / d))
    env_corr = float(np.mean(coh)) if coh else 0.0

    # accumulate err/sig per frame/antenna, aggregate as mean(err)/mean(sig)
    # (identical aggregation to eval_nmse_clean.nmse_ls)
    err0 = np.zeros((R, T, F)); sig0 = np.zeros((R, T, F))
    err1 = np.zeros((R, T, F)); sig1 = np.zeros((R, T, F))
    sto = np.zeros((R, T, F))
    for f in range(F):
        for rx in range(R):
            for tx in range(T):
                s = H_s[f, rx, tx, aidx]
                g = H_g[f, rx, tx, aidx]
                err0[rx, tx, f], sig0[rx, tx, f] = _scalar_es(s, g)
                if adaptive:
                    e, sg, tau = _sto_es_adaptive(s, g, n_fft, aidx)
                else:
                    e, sg, j = _sto_es(s, g, ramps)
                    tau = taus[j]
                err1[rx, tx, f], sig1[rx, tx, f] = e, sg
                sto[rx, tx, f] = tau

    def agg(err, sig):
        per_ant = 10 * np.log10(np.mean(err, axis=2) / (np.mean(sig, axis=2) + EPS) + EPS)
        overall = 10 * np.log10(np.mean(err) / (np.mean(sig) + EPS) + EPS)
        return per_ant, overall

    pa0, ov0 = agg(err0, sig0)
    pa1, ov1 = agg(err1, sig1)
    # Fixed-grid only: edge-saturation = fraction of best-STO at the grid edge (true
    # offset exceeds the window -> NMSE under-estimated, must widen --tau-range).
    # Adaptive has no fixed window, so this is 0.
    sto_edge = 0.0 if adaptive else float(np.mean(np.abs(sto) >= (tau_range - tau_step - 1e-9)))
    return dict(F=F, R=R, T=T, paired=len(si), gap=gap,
                align_mode=align_mode, env_corr=env_corr,
                scalar_ant=pa0, scalar=ov0, sto_ant=pa1, sto=ov1,
                sto_med=float(np.median(sto)),
                sto_p10=float(np.percentile(sto, 10)),
                sto_p90=float(np.percentile(sto, 90)),
                sto_edge=sto_edge, tau_range=tau_range)


def _print_one(tag, r):
    print(f"\n=== {tag} ===")
    print(f"  align mode   : {r['align_mode']}")
    print(f"  paired frames: {r['paired']} (gap={r['gap']}), {r['R']}x{r['T']} links")
    corr_tag = "OK" if r["env_corr"] >= CORR_MIN else "BAD"
    print(f"  cplx coherence: {r['env_corr']:+.3f} (|<SRS,GT>|/||.||, need >= {CORR_MIN:.2f}) [{corr_tag}]")
    if r["env_corr"] < CORR_MIN:
        print(f"  ⚠ ALIGNMENT SUSPECT: complex coherence {r['env_corr']:.3f} < {CORR_MIN:.2f} "
              f"-> pairing may not be the same physical slot; NMSE below is NOT trustworthy")
    sto_method = "adaptive" if r.get("tau_range", 0.0) <= 0.0 else f"fixed +/-{r['tau_range']:.0f}"
    print(f"  STO estimate : median={r['sto_med']:+.2f} samp "
          f"(p10/p90={r['sto_p10']:+.1f}/{r['sto_p90']:+.1f}) [{sto_method}]")
    if r.get("sto_edge", 0.0) > 0.05:
        print(f"  ⚠ STO EDGE-SATURATION: {100*r['sto_edge']:.0f}% of frames hit the "
              f"+/-{r['tau_range']:.0f}-sample search edge -> true timing offset exceeds "
              f"the window; NMSE is UNDER-ESTIMATED. Re-run with a larger --tau-range.")
    print(f"  NMSE scalar-only   (== eval_nmse_clean): {r['scalar']:+.2f} dB")
    print(f"  NMSE STO+scalar    (timing-aligned)    : {r['sto']:+.2f} dB")
    for rx in range(r["R"]):
        for tx in range(r["T"]):
            print(f"    rx{rx}tx{tx}: scalar={r['scalar_ant'][rx,tx]:+6.2f}  "
                  f"sto={r['sto_ant'][rx,tx]:+6.2f} dB")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", help="single snr_XdB dir")
    ap.add_argument("--ab", nargs=2, metavar=("LEGACY", "TRUE2D"),
                    help="two snr_XdB dirs to A/B compare")
    ap.add_argument("--srs-symbol", type=int, default=12)
    ap.add_argument("--tol", type=int, default=20)
    ap.add_argument("--tau-range", type=float, default=0.0,
                    help="STO search. 0 (default) = ADAPTIVE: per-frame coarse delay-peak "
                         "estimate (any offset, no fixed window) + fine refine. >0 = legacy "
                         "fixed +/-N grid (reproducible; warns on edge-saturation).")
    ap.add_argument("--tau-step", type=float, default=0.5)
    ap.add_argument("--predict-horizon", type=int, default=0,
                    help="Direction A: score a PREDICTION capture (gNB env "
                         "SRS_PKF_PREDICT_AHEAD=k) vs FUTURE GT by shifting the SRS "
                         "slot axis +k*SRS_period before pairing. 0 = on-time.")
    args = ap.parse_args()

    if not args.run_dir and not args.ab:
        ap.error("provide --run-dir or --ab")

    if args.run_dir:
        r = evaluate(args.run_dir, args.srs_symbol, args.tol, args.tau_range, args.tau_step,
                     predict_horizon=args.predict_horizon)
        _print_one(args.run_dir, r)

    if args.ab:
        rl = evaluate(args.ab[0], args.srs_symbol, args.tol, args.tau_range, args.tau_step,
                      predict_horizon=args.predict_horizon)
        rt = evaluate(args.ab[1], args.srs_symbol, args.tol, args.tau_range, args.tau_step,
                      predict_horizon=args.predict_horizon)
        _print_one("LEGACY " + args.ab[0], rl)
        _print_one("TRUE2D " + args.ab[1], rt)
        d_scalar = rt["scalar"] - rl["scalar"]
        d_sto = rt["sto"] - rl["sto"]
        print("\n=== A/B (true2d - legacy; negative = true2d better) ===")
        print(f"  delta NMSE scalar-only : {d_scalar:+.2f} dB")
        print(f"  delta NMSE STO-aligned : {d_sto:+.2f} dB  <-- the fair metric")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
