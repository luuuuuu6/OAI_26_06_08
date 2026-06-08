"""
Q4 Convergence Sweep Analyzer
=============================
Two-axis statistical convergence study of the OAI SRS-based channel estimator
against the Sionna ground-truth channel:

    x-axis A : number of averaged SRS frames  N  (log-spaced)
    x-axis B : input AWGN SNR  (dB, from sweep runs)

For each axis point we compute six indicators relative to GT:

    1) NMSE (dB)                — global magnitude + phase alignment
    2) RSRP error (dB)          — amplitude calibration
    3) PDP shape Pearson        — delay structure fidelity
    4) Per-SC σ1 Pearson        — beamforming-ready rank-1 subspace
    5) RX covariance Frobenius  — spatial correlation fidelity
    6) Estimation SNR (dB)      — signal-processing parlance, equivalent to -NMSE

Modes of use
------------
  (1) Single-run N sweep (uses logs/latest or any single OAI run):

        python3 q4_convergence_sweep.py \\
            --run-dir /path/to/a/single/run \\
            --plot-dir ../../vRAN_Socket/data_out

  (2) Multi-run SNR sweep (produced by run_q4_snr_sweep.sh):

        python3 q4_convergence_sweep.py \\
            --sweep-dir /path/to/logs/q4_sweep_YYYYMMDD_HHMMSS \\
            --plot-dir  ../../vRAN_Socket/data_out

      Generates both the N-axis convergence (from the highest-SNR point) and
      the SNR-axis convergence (at fixed N).

Outputs (in plot_dir)
---------------------
    q4_Nsweep_nmse.png           q4_Nsweep_rsrp.png
    q4_Nsweep_pdp.png            q4_Nsweep_sigma1.png
    q4_Nsweep_cov.png            q4_Nsweep_estsnr.png
    q4_SNRsweep_nmse.png         q4_SNRsweep_rsrp.png
    q4_SNRsweep_pdp.png          q4_SNRsweep_sigma1.png
    q4_SNRsweep_cov.png          q4_SNRsweep_estsnr.png
    q4_dashboard_N.png           q4_dashboard_SNR.png
    q4_convergence_report.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Reuse loaders + metric primitives from digital_twin_stats.py
_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)

from digital_twin_stats import (  # noqa: E402
    load_srs_v2,
    load_gt,
    get_active_mask,
    compute_spatial_covariance,
    compute_svd_spectrum,
    compute_pdp,
    estimate_sto,
    _apply_sto_correction,
    agc_ewma_align,
    np_pdp_threshold,
    align_by_slot,
    dft_denoise_frames,
)


# ── Core per-point metrics (at a given (H_srs_bar, H_gt_bar)) ──────────────

def _safe_corrcoef(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation with guard against zero-variance inputs."""
    a = np.asarray(a).ravel()
    b = np.asarray(b).ravel()
    if a.size < 2 or b.size < 2:
        return 0.0
    sa = float(np.std(a))
    sb = float(np.std(b))
    if sa <= 1e-20 or sb <= 1e-20:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def compute_point_metrics(H_srs_bar: np.ndarray,
                          H_gt_bar: np.ndarray,
                          active: np.ndarray,
                          n_sc: int,
                          sto_slope: float,
                          scale: float,
                          pfa: Optional[float] = None) -> Dict[str, float]:
    """Compute the 6 convergence indicators for one (H_srs_bar, H_gt_bar) pair.

    Both inputs have shape (1, n_rx, n_tx, n_fft).  sto_slope comes from the
    full-data STO estimate (stable) so that per-N PDP correlations are not
    polluted by unstable low-N phase fits.  `scale` aligns GT to SRS magnitude
    (mirrors digital_twin_stats.py convention).

    Parameters
    ----------
    pfa : if given, apply Neyman-Pearson denoising to the SRS and GT PDPs
          before computing the shape Pearson correlation.  The noise floor
          is estimated independently for each side from its own tail bins.
          Typical values: 1e-4 / 1e-6 / 1e-8.  When None, legacy -40 dB
          floor mask is used (back-compat).
    """
    H_gt_scaled = H_gt_bar * scale

    # STO-correct SRS copy (for PDP + SNR metrics that depend on timing)
    H_srs_sto = _apply_sto_correction(H_srs_bar, active, sto_slope, n_sc)

    H_s = H_srs_sto[:, :, :, active]
    H_g = H_gt_scaled[:, :, :, active]

    # LS global alignment (amplitude + constant phase) to isolate noise from scale
    alpha = np.vdot(H_g, H_s) / (np.vdot(H_g, H_g) + 1e-30)
    H_ref = alpha * H_g
    err = H_s - H_ref

    sig_pow = float(np.mean(np.abs(H_ref) ** 2))
    err_pow = float(np.mean(np.abs(err) ** 2))
    gt_pow = float(np.mean(np.abs(H_g) ** 2))

    nmse = err_pow / (gt_pow * float(np.abs(alpha)) ** 2 + 1e-30)
    nmse_dB = 10.0 * np.log10(nmse + 1e-30)
    est_snr_dB = 10.0 * np.log10(sig_pow / (err_pow + 1e-30))

    # RSRP error (dB) — amplitude-only, already scaled
    rsrp_srs = np.mean(np.abs(H_srs_bar[:, :, :, active]) ** 2)
    rsrp_gt = np.mean(np.abs(H_gt_scaled[:, :, :, active]) ** 2)
    rsrp_err_dB = float(abs(10.0 * np.log10((rsrp_srs + 1e-30) / (rsrp_gt + 1e-30))))

    # PDP shape Pearson correlation
    pdp_srs, _ = compute_pdp(H_srs_bar, active, n_sc, sto_correct_slope=sto_slope)
    pdp_gt, _ = compute_pdp(H_gt_bar, active, n_sc, sto_correct_slope=None)
    half = n_sc // 2
    pdp_srs_c = pdp_srs[:half]
    pdp_gt_c = pdp_gt[:half]
    n_active_taps_srs = n_active_taps_gt = -1
    if pfa is not None:
        # Neyman-Pearson: any bin above either side's NP threshold is kept.
        # Union mask preserves "signal-present" bins for both curves; the
        # below-threshold region is forced to the respective noise floor so
        # the Pearson is computed on comparable supports.
        thr_srs, sig_srs = np_pdp_threshold(pdp_srs, P_FA=pfa)
        thr_gt, sig_gt = np_pdp_threshold(pdp_gt, P_FA=pfa)
        mask_srs = pdp_srs_c >= thr_srs
        mask_gt = pdp_gt_c >= thr_gt
        mask = mask_srs | mask_gt
        n_active_taps_srs = int(np.sum(mask_srs))
        n_active_taps_gt = int(np.sum(mask_gt))
        if int(np.sum(mask)) >= 2:
            pdp_corr = _safe_corrcoef(pdp_srs_c[mask], pdp_gt_c[mask])
        else:
            pdp_corr = 0.0
    else:
        # Legacy -40 dB soft floor mask
        floor = (pdp_srs_c > np.max(pdp_srs_c) * 1e-4) | \
                (pdp_gt_c > np.max(pdp_gt_c) * 1e-4)
        if int(np.sum(floor)) >= 2:
            pdp_corr = _safe_corrcoef(pdp_srs_c[floor], pdp_gt_c[floor])
        else:
            pdp_corr = 0.0

    # Per-subcarrier σ1 Pearson correlation (treated as function of SC index)
    sv_srs, _ = compute_svd_spectrum(H_srs_bar, active)
    sv_gt, _ = compute_svd_spectrum(H_gt_scaled, active)
    sigma1_corr = _safe_corrcoef(sv_srs[:, 0], sv_gt[:, 0])

    # RX spatial covariance Frobenius error (power-normalized)
    R_rx_srs, _ = compute_spatial_covariance(H_srs_bar, active)
    R_rx_gt, _ = compute_spatial_covariance(H_gt_scaled, active)
    a_cov = np.vdot(R_rx_gt.flatten(), R_rx_srs.flatten()) / (
        np.vdot(R_rx_gt.flatten(), R_rx_gt.flatten()) + 1e-30)
    cov_err = float(np.linalg.norm(R_rx_srs - a_cov * R_rx_gt, "fro") /
                    (np.linalg.norm(R_rx_srs, "fro") + 1e-30))

    out = {
        "nmse_dB":      float(nmse_dB),
        "est_snr_dB":   float(est_snr_dB),
        "rsrp_err_dB":  float(rsrp_err_dB),
        "pdp_corr":     float(pdp_corr),
        "sigma1_corr":  float(sigma1_corr),
        "cov_fro_err":  float(cov_err),
        "ls_alpha_mag": float(np.abs(alpha)),
    }
    if pfa is not None:
        out["np_taps_srs"] = n_active_taps_srs
        out["np_taps_gt"] = n_active_taps_gt
    return out


# ── N-axis sweep on a single run ──────────────────────────────────────────

def default_N_grid(n_total: int) -> List[int]:
    """Log-spaced averaging depths capped at n_total."""
    candidates = [1, 2, 3, 5, 8, 10, 15, 20, 30, 50, 75, 100, 150,
                  200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000]
    out = [n for n in candidates if n <= n_total]
    if out[-1] != n_total:
        out.append(n_total)
    return sorted(set(out))


def load_run(run_dir: str,
             srs_symbol: int = 12,
             ue_idx: int = 0,
             gt_subdir: str = "sionna_gt",
             align_mode: str = "slot",
             tol_slots: int = 20,
             skip_first: int = 0) -> Tuple[np.ndarray, np.ndarray, Dict]:
    """Load SRS + GT for a single run and pair them frame-by-frame.

    Parameters
    ----------
    align_mode : "slot"  (default, CORRECT) — pair SRS and GT by nearest
                        absolute slot index (frame_id*20 + slot_id vs GT's
                        stored slot_ids).  Unmatched SRS frames dropped.
                 "index" (LEGACY, BROKEN)    — pair by array index; assumes
                        SRS[:n] and GT[:n] cover the same time window, which
                        is false whenever SRS and GT have different sampling
                        cadences (the default setup: SRS every ~160 slots,
                        GT every 10 slots).  Kept only for reproducing
                        pre-2026-04-23 results.
    tol_slots  : max acceptable abs-slot gap between SRS and its paired GT
                 (default 20 = 1 NR frame = 10 ms).
    skip_first : discard the first N SRS/GT frames (post-attach transient).
    """
    H_srs, srs_meta = load_srs_v2(run_dir, skip_first=skip_first)
    gt_dir = os.path.join(run_dir, gt_subdir)

    if align_mode == "slot":
        H_gt, gt_slot_ids = load_gt(gt_dir, srs_symbol=srs_symbol,
                                    ue_idx=ue_idx, return_slot_ids=True)
        srs_abs = srs_meta["abs_slots"]

        if gt_slot_ids.size == 0:
            raise RuntimeError(
                f"{gt_dir}: GT npz files don't carry slot_ids — "
                "cannot align.  Re-save GT with v4.py >= 2026-04-22 that "
                "writes slot_ids, or run with align_mode='index' to "
                "reproduce legacy (broken) behavior.")

        srs_keep, gt_keep, median_gap = align_by_slot(srs_abs, gt_slot_ids,
                                                     tol_slots=tol_slots)
        n_pairs = len(srs_keep)
        if n_pairs < 2:
            raise RuntimeError(
                f"{run_dir}: only {n_pairs} SRS-GT pairs within "
                f"tol={tol_slots} slots; check capture cadence.")
        H_srs = H_srs[srs_keep]
        H_gt = H_gt[gt_keep]

        meta = {
            "run_dir": run_dir,
            "align_mode": "slot",
            "tol_slots": int(tol_slots),
            "n_srs_raw": int(srs_abs.size),
            "n_gt_raw": int(gt_slot_ids.size),
            "n_pairs": int(n_pairs),
            "n_common": int(n_pairs),
            "median_gap_slots": int(median_gap),
            "n_rx": srs_meta["rx"],
            "n_tx": srs_meta["tx"],
            "n_sc": srs_meta["n_sc"],
        }
    elif align_mode == "index":
        H_gt = load_gt(gt_dir, srs_symbol=srs_symbol, ue_idx=ue_idx)
        n_common = min(H_srs.shape[0], H_gt.shape[0])
        H_srs = H_srs[:n_common]
        H_gt = H_gt[:n_common]
        meta = {
            "run_dir": run_dir,
            "align_mode": "index",
            "n_common": n_common,
            "n_rx": srs_meta["rx"],
            "n_tx": srs_meta["tx"],
            "n_sc": srs_meta["n_sc"],
        }
    else:
        raise ValueError(f"Unknown align_mode: {align_mode}")

    return H_srs.astype(np.complex128), H_gt.astype(np.complex128), meta


def sweep_N_axis(H_srs: np.ndarray,
                 H_gt: np.ndarray,
                 N_grid: List[int],
                 n_sc: int,
                 agc_mode: str = "none",
                 agc_beta: float = 0.95,
                 pfa: Optional[float] = None,
                 denoise: str = "sto") -> Tuple[Dict, Dict]:
    """Compute the 6 metrics at each N in N_grid.

    Parameters
    ----------
    agc_mode  : "none" (legacy) | "ewma" (per-frame EWMA alignment of H_gt
                                          to H_srs, plus residual global k)
    agc_beta  : forgetting factor for EWMA mode
    pfa       : if given, use Neyman-Pearson denoising for PDP correlation
    denoise   : "sto"  — linear STO fit + per-frame alpha (default)
                "dft"  — DFT-based IFFT→window→FFT denoising
                "none" — no per-frame correction, only global scale+alpha
    """
    active = get_active_mask(H_srs)
    n_active = int(np.sum(active))
    if n_active < 4:
        raise RuntimeError(f"Too few active subcarriers ({n_active}); check SRS data")

    agc_info: Dict = {"mode": agc_mode}
    if agc_mode == "ewma":
        H_srs, H_gt, info = agc_ewma_align(H_srs, H_gt, active,
                                           beta=agc_beta,
                                           residual_global=True)
        agc_info.update({
            "beta": info["beta"],
            "global_k": info["global_k"],
            "g_hat_mean": float(np.mean(info["g_hat"])),
            "g_hat_std": float(np.std(info["g_hat"])),
        })
    elif agc_mode != "none":
        raise ValueError(f"Unknown agc_mode: {agc_mode}")

    srs_rms = float(np.sqrt(np.mean(np.abs(H_srs[:, :, :, active]) ** 2)))
    gt_rms = float(np.sqrt(np.mean(np.abs(H_gt[:, :, :, active]) ** 2)))
    scale = srs_rms / (gt_rms + 1e-30)

    n_total = H_srs.shape[0]
    sto_slope = 0.0

    if denoise == "dft":
        H_srs_dn = dft_denoise_frames(H_srs, active, cp_len=144, guard=8)
        H_srs_aligned = np.zeros_like(H_srs)
        for fi in range(n_total):
            s_a = H_srs_dn[fi, :, :, active].flatten()
            g_a = (H_gt[fi, :, :, active] * scale).flatten()
            alpha_i = np.vdot(g_a, s_a) / (np.vdot(g_a, g_a) + 1e-30)
            H_srs_aligned[fi] = H_srs_dn[fi] / (alpha_i + 1e-30)
    elif denoise == "sto":
        _, sto_slope = estimate_sto(H_srs, H_gt, active, n_sc)
        H_srs_aligned = np.zeros_like(H_srs)
        for fi in range(n_total):
            frame_1 = H_srs[fi:fi+1]
            gt_1 = H_gt[fi:fi+1]
            _, slope_i = estimate_sto(frame_1, gt_1, active, n_sc)
            frame_corr = _apply_sto_correction(frame_1, active, slope_i, n_sc)
            s_a = frame_corr[0, :, :, active].flatten()
            g_a = (gt_1[0, :, :, active] * scale).flatten()
            alpha_i = np.vdot(g_a, s_a) / (np.vdot(g_a, g_a) + 1e-30)
            H_srs_aligned[fi] = frame_corr[0] / (alpha_i + 1e-30)
    else:
        H_srs_aligned = np.zeros_like(H_srs)
        for fi in range(n_total):
            s_a = H_srs[fi, :, :, active].flatten()
            g_a = (H_gt[fi, :, :, active] * scale).flatten()
            alpha_i = np.vdot(g_a, s_a) / (np.vdot(g_a, g_a) + 1e-30)
            H_srs_aligned[fi] = H_srs[fi] / (alpha_i + 1e-30)

    rows = []
    for N in N_grid:
        H_s_bar = np.mean(H_srs_aligned[:N], axis=0, keepdims=True)
        H_g_bar = np.mean(H_gt[:N], axis=0, keepdims=True)
        m = compute_point_metrics(H_s_bar, H_g_bar, active, n_sc,
                                  0.0, scale, pfa=pfa)
        m["N"] = int(N)
        rows.append(m)
        print(f"  N={N:5d}  NMSE={m['nmse_dB']:+6.2f} dB  "
              f"RSRPerr={m['rsrp_err_dB']:5.2f} dB  "
              f"ρPDP={m['pdp_corr']:.4f}  ρσ1={m['sigma1_corr']:.4f}  "
              f"CovFro={m['cov_fro_err']:.4f}  estSNR={m['est_snr_dB']:+6.2f} dB")
    summary = {
        "n_active_sc": n_active,
        "n_sc": int(n_sc),
        "sto_slope": float(sto_slope),
        "scale": float(scale),
        "agc": agc_info,
        "pfa": (None if pfa is None else float(pfa)),
        "denoise": denoise,
    }
    return {"rows": rows}, summary


# ── SNR-sweep harness (iterates sweep-dir) ────────────────────────────────

_SNR_DIR_RE = re.compile(r"^snr_(m?)(\d+)dB$")


def discover_snr_runs(sweep_dir: str) -> List[Tuple[float, str]]:
    """Return [(snr_dB, run_dir), ...] sorted by SNR ascending."""
    out = []
    if not os.path.isdir(sweep_dir):
        raise FileNotFoundError(f"sweep dir not found: {sweep_dir}")
    for name in sorted(os.listdir(sweep_dir)):
        p = os.path.join(sweep_dir, name)
        if not os.path.isdir(p):
            continue
        m = _SNR_DIR_RE.match(name)
        if not m:
            continue
        sign = -1.0 if m.group(1) == "m" else 1.0
        snr = sign * float(m.group(2))
        out.append((snr, p))
    out.sort(key=lambda x: x[0])
    return out


# ── Plot helpers ──────────────────────────────────────────────────────────

# Consistent styling
_STYLE = {
    "nmse_dB":      dict(color="#d62728", marker="o", label="NMSE (dB)"),
    "est_snr_dB":   dict(color="#2ca02c", marker="s", label="Estimation SNR (dB)"),
    "rsrp_err_dB":  dict(color="#ff7f0e", marker="^", label="|RSRP error| (dB)"),
    "pdp_corr":     dict(color="#1f77b4", marker="D", label=r"$\rho_{\mathrm{PDP}}$"),
    "sigma1_corr":  dict(color="#9467bd", marker="v", label=r"$\rho_{\sigma_1}$"),
    "cov_fro_err":  dict(color="#8c564b", marker="p", label="RX Cov Frobenius err"),
}

_Y_LABEL = {
    "nmse_dB": "NMSE (dB)",
    "est_snr_dB": "Estimation SNR (dB)",
    "rsrp_err_dB": "|RSRP error| (dB)",
    "pdp_corr": "PDP shape Pearson  ρ",
    "sigma1_corr": "σ1 per-SC Pearson  ρ",
    "cov_fro_err": "RX Cov Frobenius rel. err",
}


def _save_single(fig, path):
    plt.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def plot_single_curve(xs, ys, xlabel: str, metric: str,
                      title: str, path: str,
                      x_log: bool = False,
                      reference_line: Optional[Tuple[float, float, str]] = None):
    """Single-curve plot with optional slope reference line."""
    fig, ax = plt.subplots(figsize=(7.5, 4.8))
    st = _STYLE[metric]
    ax.plot(xs, ys, linestyle="-", linewidth=2.0,
            color=st["color"], marker=st["marker"],
            markersize=7, markerfacecolor="white",
            markeredgewidth=1.8, label=st["label"])
    if reference_line is not None:
        slope_dB_per_decade, intercept, ref_label = reference_line
        x_ref = np.array([min(xs), max(xs)], dtype=float)
        y_ref = intercept + slope_dB_per_decade * np.log10(np.maximum(x_ref, 1e-12))
        ax.plot(x_ref, y_ref, linestyle=":", linewidth=1.4, color="gray",
                label=ref_label)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(_Y_LABEL[metric])
    ax.set_title(title)
    if x_log:
        ax.set_xscale("log")
    ax.grid(True, which="both", linestyle="--", alpha=0.4)
    ax.legend(loc="best")
    _save_single(fig, path)


def plot_dashboard(xs, rows: List[Dict[str, float]],
                   xlabel: str, title_prefix: str,
                   path: str, x_log: bool = False):
    """2x3 dashboard: all 6 metrics on one figure."""
    fig, axes = plt.subplots(2, 3, figsize=(14, 8))
    metrics = ["nmse_dB", "est_snr_dB", "rsrp_err_dB",
               "pdp_corr", "sigma1_corr", "cov_fro_err"]
    for ax, m in zip(axes.ravel(), metrics):
        ys = [r[m] for r in rows]
        st = _STYLE[m]
        ax.plot(xs, ys, linestyle="-", linewidth=1.8,
                color=st["color"], marker=st["marker"],
                markersize=6, markerfacecolor="white", markeredgewidth=1.5)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(_Y_LABEL[m])
        ax.set_title(st["label"])
        if x_log:
            ax.set_xscale("log")
        ax.grid(True, which="both", linestyle="--", alpha=0.4)
    fig.suptitle(f"{title_prefix} — 6-Metric Convergence Dashboard",
                 fontsize=13, y=1.02)
    _save_single(fig, path)


# ── Orchestration ─────────────────────────────────────────────────────────

def run_N_axis(run_dir: str, plot_dir: str, tag: str = "Nsweep",
               agc_mode: str = "none", agc_beta: float = 0.95,
               pfa: Optional[float] = None,
               align_mode: str = "slot", tol_slots: int = 20,
               skip_first: int = 0,
               denoise: str = "sto"
               ) -> Tuple[List[Dict], Dict]:
    print(f"\n[Q4/N-axis] Loading run: {run_dir}")
    H_srs, H_gt, meta = load_run(run_dir, align_mode=align_mode,
                                 tol_slots=tol_slots,
                                 skip_first=skip_first)
    n_common = meta["n_common"]
    if meta["align_mode"] == "slot":
        print(f"  Align: SLOT  raw(srs={meta['n_srs_raw']}, gt={meta['n_gt_raw']})"
              f" → paired {n_common}  "
              f"(median gap {meta['median_gap_slots']} slots, tol={meta['tol_slots']})")
    else:
        print(f"  Align: INDEX (legacy) — common={n_common} (may be misaligned!)")
    print(f"  Shape: {meta['n_rx']}x{meta['n_tx']}, {meta['n_sc']} SC")
    print(f"  AGC: {agc_mode}  (beta={agc_beta})   "
          f"PDP-NP: {'off' if pfa is None else f'P_FA={pfa:g}'}")

    N_grid = default_N_grid(n_common)
    print(f"  N grid: {N_grid}")
    data, summary = sweep_N_axis(H_srs, H_gt, N_grid, meta["n_sc"],
                                 agc_mode=agc_mode, agc_beta=agc_beta, pfa=pfa,
                                 denoise=denoise)
    rows = data["rows"]

    xs = [r["N"] for r in rows]

    plot_single_curve(xs, [r["nmse_dB"] for r in rows],
                      "Accumulated frames N", "nmse_dB",
                      "NMSE Convergence vs N",
                      os.path.join(plot_dir, f"q4_{tag}_nmse.png"),
                      x_log=True,
                      reference_line=(-10.0, rows[0]["nmse_dB"],
                                      "−10 dB / decade reference"))
    plot_single_curve(xs, [r["est_snr_dB"] for r in rows],
                      "Accumulated frames N", "est_snr_dB",
                      "Estimation SNR vs N",
                      os.path.join(plot_dir, f"q4_{tag}_estsnr.png"),
                      x_log=True,
                      reference_line=(+10.0, rows[0]["est_snr_dB"],
                                      "+10 dB / decade reference"))
    plot_single_curve(xs, [r["rsrp_err_dB"] for r in rows],
                      "Accumulated frames N", "rsrp_err_dB",
                      "RSRP error vs N",
                      os.path.join(plot_dir, f"q4_{tag}_rsrp.png"),
                      x_log=True)
    plot_single_curve(xs, [r["pdp_corr"] for r in rows],
                      "Accumulated frames N", "pdp_corr",
                      "PDP Shape Pearson ρ vs N",
                      os.path.join(plot_dir, f"q4_{tag}_pdp.png"),
                      x_log=True)
    plot_single_curve(xs, [r["sigma1_corr"] for r in rows],
                      "Accumulated frames N", "sigma1_corr",
                      "σ1 per-SC Pearson ρ vs N",
                      os.path.join(plot_dir, f"q4_{tag}_sigma1.png"),
                      x_log=True)
    plot_single_curve(xs, [r["cov_fro_err"] for r in rows],
                      "Accumulated frames N", "cov_fro_err",
                      "RX Covariance Frobenius error vs N",
                      os.path.join(plot_dir, f"q4_{tag}_cov.png"),
                      x_log=True)

    plot_dashboard(xs, rows, "Accumulated frames N",
                   f"N-axis ({os.path.basename(run_dir)})",
                   os.path.join(plot_dir, f"q4_dashboard_{tag}.png"),
                   x_log=True)

    return rows, {"meta": meta, **summary}


def run_SNR_axis(sweep_dir: str, plot_dir: str,
                 fixed_N: int = 100, tag: str = "SNRsweep",
                 agc_mode: str = "none", agc_beta: float = 0.95,
                 pfa: Optional[float] = None,
                 align_mode: str = "slot", tol_slots: int = 20,
                 skip_first: int = 0,
                 denoise: str = "sto"
                 ) -> Tuple[List[Dict], Dict]:
    """For each SNR run, load data, compute metrics at fixed_N, collect.

    AGC and NP denoising are applied per SNR point (not across the sweep).
    """
    print(f"\n[Q4/SNR-axis] Scanning {sweep_dir}")
    print(f"  Align: {align_mode.upper()}  "
          f"AGC: {agc_mode}  (beta={agc_beta})   "
          f"PDP-NP: {'off' if pfa is None else f'P_FA={pfa:g}'}")
    pairs = discover_snr_runs(sweep_dir)
    if not pairs:
        raise RuntimeError(f"No snr_*dB subdirs found in {sweep_dir}")
    print(f"  Found {len(pairs)} SNR points: "
          f"{[f'{s:g}dB' for s, _ in pairs]}")

    rows = []
    for snr, run_dir in pairs:
        try:
            H_srs, H_gt, meta = load_run(run_dir, align_mode=align_mode,
                                         tol_slots=tol_slots,
                                         skip_first=skip_first)
        except Exception as e:
            print(f"  [skip] SNR={snr} dB: {e}")
            continue
        n_common = meta["n_common"]
        if n_common < 2:
            print(f"  [skip] SNR={snr} dB: only {n_common} frames")
            continue
        N_use = min(fixed_N, n_common)

        active = get_active_mask(H_srs)
        if int(np.sum(active)) < 4:
            print(f"  [skip] SNR={snr} dB: too few active SCs")
            continue

        agc_info_point: Dict = {"mode": agc_mode}
        if agc_mode == "ewma":
            H_srs, H_gt, info = agc_ewma_align(H_srs, H_gt, active,
                                               beta=agc_beta,
                                               residual_global=True)
            agc_info_point.update({
                "beta": info["beta"],
                "global_k": info["global_k"],
                "g_hat_std": float(np.std(info["g_hat"])),
            })

        _, sto_slope = estimate_sto(H_srs, H_gt, active, meta["n_sc"])
        srs_rms = float(np.sqrt(np.mean(np.abs(H_srs[:, :, :, active]) ** 2)))
        gt_rms = float(np.sqrt(np.mean(np.abs(H_gt[:, :, :, active]) ** 2)))
        scale = srs_rms / (gt_rms + 1e-30)

        n_sc = meta["n_sc"]
        H_srs_a = np.zeros_like(H_srs)
        for fi in range(H_srs.shape[0]):
            f1 = H_srs[fi:fi+1]; g1 = H_gt[fi:fi+1]
            if denoise == "sto":
                _, sl_i = estimate_sto(f1, g1, active, n_sc)
                fc = _apply_sto_correction(f1, active, sl_i, n_sc)
            else:
                fc = f1
            sa = fc[0,:,:,active].flatten()
            ga = (g1[0,:,:,active]*scale).flatten()
            ai = np.vdot(ga, sa) / (np.vdot(ga, ga) + 1e-30)
            H_srs_a[fi] = fc[0] / (ai + 1e-30)

        H_s_bar = np.mean(H_srs_a[:N_use], axis=0, keepdims=True)
        H_g_bar = np.mean(H_gt[:N_use], axis=0, keepdims=True)
        m = compute_point_metrics(H_s_bar, H_g_bar, active, n_sc,
                                  0.0, scale, pfa=pfa)
        m["snr_dB"] = float(snr)
        m["N_used"] = int(N_use)
        m["agc"] = agc_info_point
        rows.append(m)
        print(f"  SNR={snr:+6.1f} dB  N={N_use:4d}  "
              f"NMSE={m['nmse_dB']:+6.2f} dB  "
              f"RSRPerr={m['rsrp_err_dB']:5.2f} dB  "
              f"ρPDP={m['pdp_corr']:.4f}  ρσ1={m['sigma1_corr']:.4f}  "
              f"CovFro={m['cov_fro_err']:.4f}  estSNR={m['est_snr_dB']:+6.2f} dB")

    rows.sort(key=lambda r: r["snr_dB"])
    xs = [r["snr_dB"] for r in rows]

    plot_single_curve(xs, [r["nmse_dB"] for r in rows],
                      "Input SNR (dB)", "nmse_dB",
                      f"NMSE vs Input SNR  (N={fixed_N})",
                      os.path.join(plot_dir, f"q4_{tag}_nmse.png"))
    plot_single_curve(xs, [r["est_snr_dB"] for r in rows],
                      "Input SNR (dB)", "est_snr_dB",
                      f"Estimation SNR vs Input SNR  (N={fixed_N})",
                      os.path.join(plot_dir, f"q4_{tag}_estsnr.png"))
    plot_single_curve(xs, [r["rsrp_err_dB"] for r in rows],
                      "Input SNR (dB)", "rsrp_err_dB",
                      f"RSRP error vs Input SNR  (N={fixed_N})",
                      os.path.join(plot_dir, f"q4_{tag}_rsrp.png"))
    plot_single_curve(xs, [r["pdp_corr"] for r in rows],
                      "Input SNR (dB)", "pdp_corr",
                      f"PDP Shape ρ vs Input SNR  (N={fixed_N})",
                      os.path.join(plot_dir, f"q4_{tag}_pdp.png"))
    plot_single_curve(xs, [r["sigma1_corr"] for r in rows],
                      "Input SNR (dB)", "sigma1_corr",
                      f"σ1 per-SC ρ vs Input SNR  (N={fixed_N})",
                      os.path.join(plot_dir, f"q4_{tag}_sigma1.png"))
    plot_single_curve(xs, [r["cov_fro_err"] for r in rows],
                      "Input SNR (dB)", "cov_fro_err",
                      f"RX Cov Frobenius err vs Input SNR  (N={fixed_N})",
                      os.path.join(plot_dir, f"q4_{tag}_cov.png"))

    plot_dashboard(xs, rows, "Input SNR (dB)",
                   f"SNR-axis (fixed N={fixed_N})",
                   os.path.join(plot_dir, f"q4_dashboard_{tag}.png"))

    return rows, {"fixed_N": int(fixed_N),
                  "snr_points": [(s, os.path.basename(p)) for s, p in pairs]}


# ── CLI ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description=__doc__)
    ap.add_argument("--run-dir", default=None,
                    help="Single OAI run dir (for N-axis only)")
    ap.add_argument("--sweep-dir", default=None,
                    help="q4_sweep_* dir with snr_*dB/ subdirs "
                         "(enables both N-axis and SNR-axis)")
    ap.add_argument("--plot-dir",
                    default=os.path.join(os.path.dirname(__file__),
                                        "..", "..", "data_out"))
    ap.add_argument("--fixed-n", type=int, default=100,
                    help="N used when sweeping SNR axis")
    ap.add_argument("--n-axis-run", default=None,
                    help="Which run to use for N-axis when sweep-dir is given "
                         "(default: highest-SNR run in sweep)")
    ap.add_argument("--report-json", default=None,
                    help="JSON report path "
                         "(default: <plot_dir>/q4_convergence_report[_<tag-suffix>].json)")
    ap.add_argument("--agc", choices=["none", "ewma"], default="none",
                    help="Per-frame AGC mode applied to H_gt to track "
                         "receiver-gain drift against H_srs.  "
                         "'ewma' = EWMA forgetting-factor alignment "
                         "(Prof. feedback 2026-04-23).  Default: none.")
    ap.add_argument("--agc-beta", type=float, default=0.95,
                    help="EWMA forgetting factor (0,1); "
                         "0.95 ≈ 20-frame effective window, "
                         "0.99 ≈ 100-frame.  Default 0.95.")
    ap.add_argument("--pfa", type=float, default=None,
                    help="If given, apply Neyman-Pearson PDP denoising "
                         "(typical 1e-4 / 1e-6 / 1e-8).  When omitted, "
                         "the legacy -40 dB soft floor is used.")
    ap.add_argument("--denoise", choices=["sto", "dft", "none"], default="sto",
                    help="Per-frame denoising: 'sto' (linear phase ramp fit, "
                         "default), 'dft' (IFFT window), 'none' (no correction).")
    ap.add_argument("--tag-suffix", default=None,
                    help="Extra suffix appended to plot/report filenames "
                         "so multiple configs don't overwrite each other "
                         "(e.g. 'baseline', 'agc', 'agc_pfa1e-6').")
    ap.add_argument("--align", choices=["slot", "index"], default="slot",
                    help="SRS↔GT frame pairing.  'slot' (default) = nearest"
                         " abs-slot match — the CORRECT method.  'index' ="
                         " pre-2026-04-23 legacy behavior that pairs by "
                         "array index and silently compares mismatched-"
                         "time-window frames.  Use 'index' only to "
                         "reproduce pre-fix results.")
    ap.add_argument("--align-tol-slots", type=int, default=20,
                    help="Max abs-slot gap when align=slot (default 20 = "
                         "1 NR frame = 10 ms at 30 kHz SCS).")
    ap.add_argument("--skip-first", type=int, default=0,
                    help="Discard the first N SRS/GT frames "
                         "(post-attach transient, default 0).")
    args = ap.parse_args()

    if not args.run_dir and not args.sweep_dir:
        ap.error("must supply --run-dir and/or --sweep-dir")

    os.makedirs(args.plot_dir, exist_ok=True)

    # Tag suffix handling — auto-derive if not given, so files encode config
    if args.tag_suffix is not None:
        suffix = args.tag_suffix
    else:
        parts = []
        if args.align != "slot":
            parts.append(f"align-{args.align}")
        if args.agc != "none":
            parts.append(f"agc-{args.agc}")
        if args.pfa is not None:
            parts.append(f"pfa{args.pfa:.0e}".replace("e-0", "e-"))
        suffix = "_".join(parts) if parts else ""
    n_tag = f"Nsweep_{suffix}" if suffix else "Nsweep"
    snr_tag = f"SNRsweep_{suffix}" if suffix else "SNRsweep"

    report = {
        "fixed_N_for_SNR_sweep": int(args.fixed_n),
        "plot_dir": args.plot_dir,
        "config": {
            "align_mode": args.align,
            "align_tol_slots": int(args.align_tol_slots),
            "agc_mode": args.agc,
            "agc_beta": float(args.agc_beta),
            "pfa": (None if args.pfa is None else float(args.pfa)),
            "tag_suffix": suffix,
        },
    }

    # N-axis
    if args.sweep_dir:
        pairs = discover_snr_runs(args.sweep_dir)
        if args.n_axis_run:
            n_run = args.n_axis_run
        else:
            _, n_run = pairs[-1]  # highest SNR
        print(f"\n[Q4] N-axis source run: {n_run}")
        n_rows, n_summary = run_N_axis(n_run, args.plot_dir, tag=n_tag,
                                       agc_mode=args.agc,
                                       agc_beta=args.agc_beta,
                                       pfa=args.pfa,
                                       align_mode=args.align,
                                       tol_slots=args.align_tol_slots,
                                       skip_first=args.skip_first,
                                       denoise=args.denoise)
        report["N_axis"] = {"source_run": n_run, **n_summary,
                            "points": n_rows}
    elif args.run_dir:
        print(f"\n[Q4] N-axis source run: {args.run_dir}")
        n_rows, n_summary = run_N_axis(args.run_dir, args.plot_dir, tag=n_tag,
                                       agc_mode=args.agc,
                                       agc_beta=args.agc_beta,
                                       pfa=args.pfa,
                                       align_mode=args.align,
                                       tol_slots=args.align_tol_slots,
                                       skip_first=args.skip_first,
                                       denoise=args.denoise)
        report["N_axis"] = {"source_run": args.run_dir, **n_summary,
                            "points": n_rows}

    # SNR-axis
    if args.sweep_dir:
        snr_rows, snr_summary = run_SNR_axis(args.sweep_dir, args.plot_dir,
                                             fixed_N=args.fixed_n,
                                             tag=snr_tag,
                                             agc_mode=args.agc,
                                             agc_beta=args.agc_beta,
                                             pfa=args.pfa,
                                             align_mode=args.align,
                                             tol_slots=args.align_tol_slots,
                                             skip_first=args.skip_first,
                                             denoise=args.denoise)
        report["SNR_axis"] = {"sweep_dir": args.sweep_dir,
                              **snr_summary, "points": snr_rows}

    # JSON report
    default_report_name = ("q4_convergence_report"
                           + (f"_{suffix}" if suffix else "")
                           + ".json")
    report_path = args.report_json or os.path.join(args.plot_dir,
                                                   default_report_name)

    def _safe(obj):
        if isinstance(obj, dict):
            return {k: _safe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_safe(x) for x in obj]
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return obj

    with open(report_path, "w") as f:
        json.dump(_safe(report), f, indent=2)
    print(f"\n[Q4] Report saved: {report_path}")
    print(f"[Q4] Plots saved under: {args.plot_dir}")


if __name__ == "__main__":
    main()
