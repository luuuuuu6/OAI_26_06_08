#!/usr/bin/env python3
"""
SRS STO Compensation Module.

Fix 1: per-frame weighted phase slope STO compensation.
Removes the linear phase ramp caused by ±0.5 sample inter-frame timing jitter
in the OAI/Proxy IPC path. See DEBUG_LOG_20260514_STO_ROOT_CAUSE.md.

Usage as library:
    from srs_sto_compensate import compensate_sto
    H_fixed, stats = compensate_sto(H, active_sc, return_stats=True)

Usage as CLI:
    python3 srs_sto_compensate.py --run-dir /path/to/snr_20dB --out H_compensated.npz
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import numpy as np


def compensate_sto(
    H: np.ndarray,
    active_sc_indices: np.ndarray,
    N_fft: int = 2048,
    weighting: str = "power",
    min_active_sc: int = 50,
    return_stats: bool = False,
) -> np.ndarray | tuple[np.ndarray, dict]:
    """
    Compensate per-frame STO jitter via weighted phase slope removal.

    Parameters
    ----------
    H : complex array, shape (N_frames, N_sc) or (N_frames, N_active)
        Channel estimates. If N_sc == len(active_sc_indices), treated as
        already indexed to active SCs (compensation applied in-place on
        those columns). Otherwise active_sc_indices used to index into H.
    active_sc_indices : int array
        Subcarrier indices used for slope fitting.
    N_fft : int
        FFT size (2048 for 106PRB/30kHz).
    weighting : 'power' | 'uniform'
        'power' weights by |H[k]|^2 (robust at channel nulls).
    min_active_sc : int
        Skip frame if fewer than this many non-zero SCs (warn).
    return_stats : bool
        If True, return (H_out, stats_dict).

    Returns
    -------
    H_compensated : same shape as H
    stats : dict (optional)
        delta_estimated, fit_residual_std, corr_before, corr_after
    """
    H = np.atleast_2d(H).copy()
    N_frames, N_sc = H.shape
    k = np.asarray(active_sc_indices, dtype=np.float64)

    if N_sc == len(k):
        h_is_active_only = True
        H_active = H
    else:
        h_is_active_only = False
        H_active = H[:, active_sc_indices.astype(int)]

    deltas = np.zeros(N_frames)
    fit_residuals = np.zeros(N_frames)

    for i in range(N_frames):
        h_i = H_active[i]

        non_zero = np.abs(h_i) > 0
        if np.sum(non_zero) < min_active_sc:
            continue

        k_valid = k[non_zero]
        h_valid = h_i[non_zero]

        # Unwrap from the strongest SC for robustness
        peak_idx = np.argmax(np.abs(h_valid))
        phase_raw = np.angle(h_valid)
        phase = np.unwrap(phase_raw, discont=np.pi)
        # Re-unwrap relative to peak position for better continuity
        if peak_idx > 0:
            left = np.unwrap(phase_raw[:peak_idx + 1][::-1])[::-1]
            right = np.unwrap(phase_raw[peak_idx:])
            right -= right[0] - left[-1]
            phase = np.concatenate([left[:-1], right])

        if weighting == "power":
            w = np.abs(h_valid) ** 2
        else:
            w = np.ones_like(k_valid)
        w_sum = np.sum(w)
        if w_sum < 1e-30:
            continue
        w = w / w_sum

        k_mean = np.sum(w * k_valid)
        p_mean = np.sum(w * phase)
        k_c = k_valid - k_mean
        p_c = phase - p_mean
        denom = np.sum(w * k_c ** 2)
        if denom < 1e-30:
            continue
        slope = np.sum(w * k_c * p_c) / denom

        deltas[i] = -slope * N_fft / (2 * np.pi)

        fitted = p_mean + slope * k_c
        residual = phase - (p_mean + slope * (k_valid - k_mean))
        fit_residuals[i] = np.sqrt(np.sum(w * residual ** 2))

        # Compensate: remove slope from ALL SCs in this frame
        if h_is_active_only:
            H[i] *= np.exp(-1j * slope * k)
        else:
            all_k = np.arange(N_sc, dtype=np.float64)
            H[i] *= np.exp(-1j * slope * all_k)

    if not return_stats:
        return H.squeeze()

    stats = {
        "delta_estimated": deltas,
        "delta_mean": np.mean(deltas),
        "delta_std": np.std(deltas),
        "delta_range": (np.min(deltas), np.max(deltas)),
        "fit_residual_std": fit_residuals,
        "corr_before": _mean_pair_corr(np.atleast_2d(H_active)),
        "corr_after": _mean_pair_corr(H[:, active_sc_indices.astype(int)] if not h_is_active_only else H),
    }
    return H.squeeze(), stats


def _mean_pair_corr(H: np.ndarray, max_pairs: int = 500) -> float:
    """Mean |correlation| across adjacent frame pairs."""
    N = H.shape[0]
    if N < 2:
        return np.nan
    corrs = []
    for i in range(min(N - 1, max_pairs)):
        c = np.abs(np.vdot(H[i], H[i + 1])) / (
            np.linalg.norm(H[i]) * np.linalg.norm(H[i + 1]) + 1e-30
        )
        corrs.append(c)
    return float(np.mean(corrs))


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="SRS STO Compensation: remove per-frame timing jitter from H estimates."
    )
    parser.add_argument("--run-dir", required=True,
                        help="Path to snr_*dB directory (contains srs_matrix_*.bin + sionna_gt/)")
    parser.add_argument("--out", default=None,
                        help="Output .npz path (default: <run-dir>/H_sto_compensated.npz)")
    parser.add_argument("--weighting", default="power", choices=["power", "uniform"])
    parser.add_argument("--rx", type=int, default=None, help="Process only this RX index")
    parser.add_argument("--tx", type=int, default=None, help="Process only this TX index")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from digital_twin_stats import load_srs_v2

    print(f"[STO Compensate] Loading SRS from {args.run_dir}")
    srs, meta = load_srs_v2(args.run_dir)
    N_frames, n_rx, n_tx, n_sc = srs.shape
    print(f"  Shape: {srs.shape} (frames, rx, tx, sc)")

    active_mask = np.abs(srs[0, 0, 0, :]) > 0
    active_sc = np.where(active_mask)[0]
    print(f"  Active SC: {len(active_sc)}")

    rx_range = [args.rx] if args.rx is not None else range(n_rx)
    tx_range = [args.tx] if args.tx is not None else range(n_tx)

    srs_out = srs.copy()
    all_stats = {}

    for rx in rx_range:
        for tx in tx_range:
            h = srs[:, rx, tx, :]
            h_comp, stats = compensate_sto(
                h, active_sc, N_fft=2048,
                weighting=args.weighting, return_stats=True
            )
            srs_out[:, rx, tx, :] = h_comp
            key = f"rx{rx}_tx{tx}"
            all_stats[key] = stats
            print(f"  [{key}] STO std={stats['delta_std']:.3f} samples, "
                  f"corr {stats['corr_before']:.4f} -> {stats['corr_after']:.4f}")

    out_path = args.out or str(Path(args.run_dir) / "H_sto_compensated.npz")
    np.savez_compressed(
        out_path,
        H_raw=srs,
        H_compensated=srs_out,
        active_sc=active_sc,
        n_frames=N_frames,
        n_rx=n_rx,
        n_tx=n_tx,
        n_sc=n_sc,
        abs_slots=meta.get("abs_slots", []),
        **{f"delta_{k}": v["delta_estimated"] for k, v in all_stats.items()},
    )
    print(f"\n  Saved: {out_path}")
    print(f"  Keys: H_raw, H_compensated, active_sc, delta_rx*_tx*, ...")


if __name__ == "__main__":
    main()
