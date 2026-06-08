#!/usr/bin/env python3
"""
eval_pdpR_nmse.py — minimal NMSE evaluator for the direct PDP->R two-stage
                    SRS MMSE estimator.

Design principles (vs the legacy q4_convergence_sweep.py):
  - Single KPI: NMSE in dB (per-frame and aggregate)
  - Two flavors only:
        (a) raw     : NMSE = E[|H_srs - H_gt|^2] / E[|H_gt|^2]
        (b) ls_aligned : with global complex scale alpha = <H_gt,H_srs>/<H_gt,H_gt>
                         (removes amplitude + constant phase, preserves geometry)
  - No STO correction, no N-frame averaging, no PCA / SVD / DFT denoising
  - No plotting (numbers only); reuses load_srs_v2 / load_gt for stable IO
  - Active subcarriers auto-detected from the SRS estimator output
  - Frame pairing: by absolute slot index from SRS bin header + GT npz slot_ids,
                   nearest within tol; falls back to 1:1 truncation if no slots.

Usage:
    python3 eval_pdpR_nmse.py --run-dir /tmp/method_d_mmse1d_pdpR/snr_20dB
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np


def _import_loaders():
    """Locate digital_twin_stats.py next to this script (or in PYTHONPATH)."""
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    from digital_twin_stats import (  # noqa: WPS433
        load_srs_v2, load_gt, align_by_slot,
        index_gt_slots, load_gt_by_refs,
    )
    return load_srs_v2, load_gt, align_by_slot, index_gt_slots, load_gt_by_refs


def _import_sto_helpers():
    """Borrow STO machinery from digital_twin_stats."""
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    from digital_twin_stats import (  # noqa: WPS433
        estimate_sto, _apply_sto_correction, _estimate_sto_single,
    )
    return estimate_sto, _apply_sto_correction, _estimate_sto_single


def apply_sto_correction(H_srs: np.ndarray, H_gt: np.ndarray,
                         active: np.ndarray, mode: str) -> np.ndarray:
    """Return H_srs after STO correction.
    mode = 'none'      : no correction
           'global'    : single STO slope estimated from all frames
           'per-frame' : one STO slope per frame
    """
    if mode == "none":
        return H_srs
    estimate_sto, _apply_sto, _estimate_sto_single = _import_sto_helpers()
    n_fft = H_srs.shape[-1]
    if mode == "global":
        _, sto_slope = estimate_sto(H_srs, H_gt, active, n_fft)
        return _apply_sto(H_srs, active, sto_slope, n_fft)
    if mode == "per-frame":
        H_out = H_srs.copy()
        for fi in range(H_srs.shape[0]):
            slope = _estimate_sto_single(H_srs[fi], H_gt[fi], active, n_fft)
            H_out[fi:fi+1] = _apply_sto(H_srs[fi:fi+1], active, slope, n_fft)
        return H_out
    raise ValueError(f"unknown STO mode: {mode}")


def detect_active_sc(H: np.ndarray) -> np.ndarray:
    """Active SC mask = positions that are non-zero in ANY frame/ant."""
    mag = np.sum(np.abs(H), axis=(0, 1, 2))  # collapse all but SC axis
    return mag > 1e-6


def pair_by_abs_slot(srs_abs: np.ndarray, gt_slots: np.ndarray, tol: int = 4):
    """Greedy nearest-slot pairing. Returns (idx_srs, idx_gt) such that
    H_srs[idx_srs] aligns with H_gt[idx_gt]. Falls back to truncation if either
    side lacks slot ids."""
    if len(srs_abs) == 0 or len(gt_slots) == 0:
        n = min(len(srs_abs) if len(srs_abs) else 1, len(gt_slots) if len(gt_slots) else 1)
        return np.arange(n), np.arange(n)
    out_s, out_g = [], []
    used_g = np.zeros(len(gt_slots), dtype=bool)
    for i, s in enumerate(srs_abs):
        diffs = np.abs(gt_slots - s)
        diffs[used_g] = 1 << 30
        j = int(np.argmin(diffs))
        if diffs[j] <= tol:
            out_s.append(i)
            out_g.append(j)
            used_g[j] = True
    return np.asarray(out_s, dtype=int), np.asarray(out_g, dtype=int)


def nmse_db(H_srs: np.ndarray, H_gt: np.ndarray, active: np.ndarray,
            ls_align: bool, per_antenna: bool = False):
    """Returns (nmse_dB_aggregate, nmse_dB_per_frame_array, alpha_used).

    ls_align=True, per_antenna=False : single complex alpha per frame (original)
    ls_align=True, per_antenna=True  : independent alpha per (frame, rx, tx)
    """
    H_s = H_srs[:, :, :, active]
    H_g = H_gt[:, :, :, active]
    F, R, T, K = H_s.shape
    if ls_align and per_antenna:
        alpha_rt = np.zeros((F, R, T), dtype=np.complex128)
        for rx in range(R):
            for tx in range(T):
                gg = np.sum(H_g[:, rx, tx, :].conj() * H_g[:, rx, tx, :], axis=1)
                gs = np.sum(H_g[:, rx, tx, :].conj() * H_s[:, rx, tx, :], axis=1)
                alpha_rt[:, rx, tx] = gs / (gg + 1e-30)
        diff = H_s - alpha_rt[:, :, :, None] * H_g
        err_pf = np.mean(np.abs(diff) ** 2, axis=(1, 2, 3))
        sig_pf = np.mean(np.abs(alpha_rt[:, :, :, None] * H_g) ** 2, axis=(1, 2, 3))
        nmse_pf = err_pf / (sig_pf + 1e-30)
        nmse_pf_dB = 10.0 * np.log10(nmse_pf + 1e-30)
        nmse_agg_dB = 10.0 * np.log10(np.mean(err_pf) / (np.mean(sig_pf) + 1e-30) + 1e-30)
        return float(nmse_agg_dB), nmse_pf_dB, alpha_rt
    elif ls_align:
        s_flat = H_s.reshape(H_s.shape[0], -1)
        g_flat = H_g.reshape(H_g.shape[0], -1)
        gg = np.sum(g_flat.conj() * g_flat, axis=1)
        gs = np.sum(g_flat.conj() * s_flat, axis=1)
        alpha = gs / (gg + 1e-30)
    else:
        alpha = np.ones(H_s.shape[0], dtype=np.complex128)
    diff = H_s - alpha[:, None, None, None] * H_g
    err_pf = np.mean(np.abs(diff) ** 2, axis=(1, 2, 3))
    sig_pf = np.mean(np.abs(alpha[:, None, None, None] * H_g) ** 2, axis=(1, 2, 3))
    nmse_pf = err_pf / (sig_pf + 1e-30)
    nmse_pf_dB = 10.0 * np.log10(nmse_pf + 1e-30)
    nmse_agg_dB = 10.0 * np.log10(np.mean(err_pf) / (np.mean(sig_pf) + 1e-30) + 1e-30)
    return float(nmse_agg_dB), nmse_pf_dB, alpha


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, help="Path to snr_*dB run dir "
                    "(must contain srs_matrix_*.bin and sionna_gt/*.npz)")
    ap.add_argument("--srs-symbol", type=int, default=12,
                    help="GT symbol index that holds the SRS slot (default 12)")
    ap.add_argument("--skip-first", type=int, default=0,
                    help="Drop first N frames (transient) on both sides")
    ap.add_argument("--tol", type=int, default=20,
                    help="Slot-pairing tolerance (default 20)")
    ap.add_argument("--sto-correct", choices=["none", "global", "per-frame"],
                    default="none",
                    help="Apply STO (timing-offset) correction before NMSE")
    ap.add_argument("--dump-sto", action="store_true",
                    help="Print per-frame STO statistics (samples) for root-cause analysis")
    ap.add_argument("--sto-fixed-samples", type=float, default=None,
                    help="Apply a fixed STO correction (in samples) instead of estimating. "
                         "GPU-IPC V8 fixes the V7 off-by-one; set to 1.0 only for old V7 data. "
                         "Set to 0 to disable. (default: 1.0)")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    gt_dir = run_dir / "sionna_gt"
    if not (run_dir.exists() and gt_dir.exists()):
        sys.exit(f"missing run-dir or gt subdir under {run_dir}")

    (load_srs_v2, load_gt, align_by_slot_fn,
     index_gt_slots, load_gt_by_refs) = _import_loaders()

    print(f"[load] {run_dir}")
    H_srs, meta_srs = load_srs_v2(str(run_dir), skip_first=args.skip_first)
    print(f"  SRS:  shape={H_srs.shape}  rx={meta_srs['rx']} tx={meta_srs['tx']} n_sc={meta_srs['n_sc']}")

    srs_abs = np.asarray(meta_srs["abs_slots"], dtype=np.int64)

    print(f"[index GT] scanning metadata only (lazy)...")
    gt_slots_arr, gt_refs = index_gt_slots(
        str(gt_dir), srs_symbol=args.srs_symbol, skip_first=args.skip_first)
    print(f"  GT indexed: {len(gt_slots_arr)} valid frames from {len(set(r[0] for r in gt_refs))} files")

    if gt_slots_arr.size > 0 and srs_abs.size > 0:
        idx_s, idx_g, median_gap = align_by_slot_fn(srs_abs, gt_slots_arr, tol_slots=args.tol)
        print(f"  paired: {len(idx_s)} frames  (tol={args.tol} slots, median_gap={median_gap})")
    else:
        idx_s, idx_g = pair_by_abs_slot(srs_abs, gt_slots_arr, tol=args.tol)
        print(f"  paired: {len(idx_s)} frames  (tol={args.tol} slots, legacy fallback)")

    if len(idx_s) < 1:
        sys.exit("no paired frames after slot alignment; widen --tol or check data")

    print(f"[load GT] loading {len(idx_g)} paired frames...")
    H_g = load_gt_by_refs(gt_refs, idx_g)
    H_s = H_srs[idx_s].astype(np.complex128)
    n_gt_files = len(set(gt_refs[int(i)][0] for i in idx_g))
    print(f"  loaded: H_s={H_s.shape} H_g={H_g.shape} from {n_gt_files} GT files")

    active = detect_active_sc(H_s)
    n_act = int(np.sum(active))
    print(f"  active SC: {n_act} / {meta_srs['n_sc']}  (K_TC ~ {meta_srs['n_sc'] // max(n_act, 1)})")

    n_fft = H_s.shape[-1]

    # Fixed STO correction (known offset from prior analysis)
    if args.sto_fixed_samples is not None:
        _, _, _est_single = _import_sto_helpers()
        _, _apply_sto, _ = _import_sto_helpers()
        sto_slope = -2.0 * np.pi * args.sto_fixed_samples / n_fft
        print(f"  applying fixed STO correction: {args.sto_fixed_samples} samples")
        H_s = _apply_sto(H_s, active, sto_slope, n_fft)
    elif args.sto_correct != "none":
        print(f"  applying STO correction: mode={args.sto_correct}")
        if args.dump_sto:
            estimate_sto, _, _estimate_sto_single = _import_sto_helpers()
            slopes = []
            for fi in range(H_s.shape[0]):
                slopes.append(_estimate_sto_single(H_s[fi], H_g[fi], active, n_fft))
            slopes = np.array(slopes)
            sto_samples = -slopes * n_fft / (2 * np.pi)
            print(f"  per-frame STO (samples): mean={np.mean(sto_samples):+.3f}  "
                  f"std={np.std(sto_samples):.3f}  median={np.median(sto_samples):+.3f}  "
                  f"range=[{np.min(sto_samples):+.2f}, {np.max(sto_samples):+.2f}]")
            print(f"  → constant ≈ system offset; large std ≈ per-frame jitter (AGC/timing)")
        H_s = apply_sto_correction(H_s, H_g, active, args.sto_correct)

    raw_dB, raw_pf, _ = nmse_db(H_s, H_g, active, ls_align=False)
    ls_dB,  ls_pf, alpha = nmse_db(H_s, H_g, active, ls_align=True)
    pa_dB,  pa_pf, alpha_rt = nmse_db(H_s, H_g, active, ls_align=True, per_antenna=True)

    rsrp_srs_dB = 10.0 * np.log10(np.mean(np.abs(H_s[:, :, :, active]) ** 2) + 1e-30)
    rsrp_gt_dB  = 10.0 * np.log10(np.mean(np.abs(H_g[:, :, :, active]) ** 2) + 1e-30)

    print()
    print("======== RESULTS ========")
    print(f"  Frames evaluated     : {len(idx_s)}")
    print(f"  RSRP (SRS, dBish)    : {rsrp_srs_dB:+8.2f}")
    print(f"  RSRP (GT,  dBish)    : {rsrp_gt_dB:+8.2f}")
    if alpha.ndim == 1:
        print(f"  |alpha| median       : {np.median(np.abs(alpha)):.4f}   "
              f"(scale factor SRS/GT)")
    print()
    print(f"  NMSE raw   (no align): {raw_dB:+7.2f} dB   "
          f"per-frame [p10,p50,p90]=[{np.percentile(raw_pf,10):+.2f},"
          f"{np.percentile(raw_pf,50):+.2f},{np.percentile(raw_pf,90):+.2f}]")
    print(f"  NMSE LS-aligned      : {ls_dB:+7.2f} dB   "
          f"per-frame [p10,p50,p90]=[{np.percentile(ls_pf,10):+.2f},"
          f"{np.percentile(ls_pf,50):+.2f},{np.percentile(ls_pf,90):+.2f}]")
    print(f"  NMSE LS-per-antenna  : {pa_dB:+7.2f} dB   "
          f"per-frame [p10,p50,p90]=[{np.percentile(pa_pf,10):+.2f},"
          f"{np.percentile(pa_pf,50):+.2f},{np.percentile(pa_pf,90):+.2f}]")

    # Per-(rx,tx) breakdown
    n_rx, n_tx = H_s.shape[1], H_s.shape[2]
    if n_rx * n_tx <= 16:
        print()
        print("  Per-antenna breakdown (per-antenna LS):")
        for rx in range(n_rx):
            for tx in range(n_tx):
                H_s_1 = H_s[:, rx:rx+1, tx:tx+1, :]
                H_g_1 = H_g[:, rx:rx+1, tx:tx+1, :]
                d1, _, a1 = nmse_db(H_s_1, H_g_1, active, ls_align=True)
                print(f"    rx{rx}_tx{tx}: NMSE={d1:+7.2f} dB  |alpha|={np.median(np.abs(a1)):.2f}")

    print()
    print("Interpretation: NMSE_dB < 0 means error << signal (good).")
    print("                NMSE_dB > 0 means error >= signal (bad — algorithm broken or scale wrong).")
    print("                LS-aligned strips amplitude/phase; if it drops far below raw,")
    print("                the issue is just a global scale, not a structural error.")
    print("                LS-per-antenna independently aligns each (rx,tx) pair; if this")
    print("                is much better than global LS, the mismatch is per-antenna phase.")


if __name__ == "__main__":
    main()
