#!/usr/bin/env python3
"""
2D MMSE evaluation — supports both static and dynamic channels.

Static channel (speed=0):
  GT = mean of first-half frames (split-set), eval on second-half.
  Curves: Raw / EMA / EMA+Box / Cumul. avg (oracle)

Dynamic channel (speed>0):
  GT = per-frame Sionna GT (from H_gt key in NPZ).
  Curves: Raw / EMA / EMA+Box / Sliding-window avg

Outputs:
  Figure 1 — NMSE vs frame
  Figure 2 — Steady-state NMSE vs SNR
  Figure 3 — alpha sweep
"""
from pathlib import Path
from typing import Optional

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from srs_2d_mmse import SRS2DFilterVec

# ── paths ──
DATA_DIR = Path(__file__).parent / "data_out" / "2d_mmse_input"
OUT_DIR = Path(__file__).parent / "data_out" / "2d_mmse_results"

STO_OUTLIER_THRESH = 6.0


# ── helpers ──

def nmse_db(H_est, H_gt, ls_align=False):
    """NMSE in dB. Accepts (rx, tx, sc) or broadcastable shapes.

    ls_align: if True, compute per-(rx,tx) LS scalar alpha to remove
              scale/phase offset (needed when SRS and GT have different
              scales, e.g. int16 vs Sionna-normalized).
    """
    if ls_align:
        err_total = 0.0
        sig_total = 0.0
        if H_est.ndim >= 3:
            n_rx, n_tx = H_est.shape[0], H_est.shape[1]
            for rx in range(n_rx):
                for tx in range(n_tx):
                    s = H_est[rx, tx].flatten()
                    g = H_gt[rx, tx].flatten()
                    gg = np.sum(np.abs(g) ** 2)
                    if gg < 1e-30:
                        continue
                    alpha = np.sum(g.conj() * s) / gg
                    err_total += np.sum(np.abs(s - alpha * g) ** 2)
                    sig_total += np.sum(np.abs(alpha * g) ** 2)
        else:
            s = H_est.flatten()
            g = H_gt.flatten()
            gg = np.sum(np.abs(g) ** 2)
            if gg < 1e-30:
                return np.nan
            alpha = np.sum(g.conj() * s) / gg
            err_total = np.sum(np.abs(s - alpha * g) ** 2)
            sig_total = np.sum(np.abs(alpha * g) ** 2)
        if sig_total < 1e-30:
            return np.nan
        return 10.0 * np.log10(err_total / sig_total)
    else:
        err = np.mean(np.abs(H_est - H_gt) ** 2)
        sig = np.mean(np.abs(H_gt) ** 2)
        if sig < 1e-30:
            return np.nan
        return 10.0 * np.log10(err / sig)


def detect_sto_outliers(H_raw_active, active_sc):
    """Return boolean mask of clean frames."""
    Hr = H_raw_active[:, 0, 0, :]
    first_gap = np.where(np.diff(active_sc) > 1)[0]
    end = first_gap[0] + 1 if len(first_gap) > 0 else Hr.shape[1]
    pdiff = np.angle(Hr[:, 1:end] * np.conj(Hr[:, :end - 1]))
    sto = np.mean(pdiff, axis=1) * 2048 / (2 * np.pi)
    median_sto = np.median(sto)
    return np.abs(sto - median_sto) < STO_OUTLIER_THRESH, sto


def discover_files(speed: Optional[float] = None):
    """Find all NPZ files, grouped by speed. Returns {speed: {snr: filename}}."""
    groups = {}
    for f in sorted(DATA_DIR.glob("H_snr*dB_*.npz")):
        name = f.stem
        if "sto_fixed" in name:
            continue
        d = np.load(f, allow_pickle=True)
        spd = float(d["ue_speed"]) if "ue_speed" in d else 0.0
        snr = int(d["snr_dB"]) if "snr_dB" in d else None
        if snr is None:
            continue
        groups.setdefault(spd, {})[snr] = f.name
    if speed is not None:
        return {speed: groups.get(speed, {})}
    return groups


def load_data(snr_dB: int, speed: float = 0.0, file_map: Optional[dict] = None):
    """Load NPZ → (H_eval, H_gt_per_frame_or_None, H_gt_static, meta).

    For static (speed=0):
      H_gt_per_frame = None
      H_gt_static = mean of first-half frames (split-set GT)
      H_eval = second-half frames

    For dynamic (speed>0):
      H_gt_per_frame = per-frame Sionna GT, shape (N_eval, rx, tx, n_active)
      H_gt_static = None
      H_eval = all clean frames (no split needed since GT is independent)
    """
    if file_map and snr_dB in file_map:
        path = DATA_DIR / file_map[snr_dB]
    else:
        groups = discover_files(speed)
        fm = groups.get(speed, {})
        if snr_dB not in fm:
            raise FileNotFoundError(
                f"No data for SNR={snr_dB}, speed={speed}. "
                f"Available: {fm}")
        path = DATA_DIR / fm[snr_dB]

    d = np.load(path)
    H_full = d["H_sto_compensated"]
    active = d["active_sc"]
    H_active = H_full[:, :, :, active]

    has_gt = "H_gt" in d
    is_static = (float(d.get("ue_speed", 0)) == 0) and not has_gt

    # Outlier removal (using H_raw if available, else H_sto_compensated)
    if "H_raw" in d:
        H_raw_active = d["H_raw"][:, :, :, active]
    else:
        H_raw_active = H_active
    clean_mask, _ = detect_sto_outliers(H_raw_active, active)
    n_removed = int(np.sum(~clean_mask))

    if is_static:
        H_clean = H_active[clean_mask]
        n_gt = len(H_clean) // 2
        H_gt_static = np.mean(H_clean[:n_gt], axis=0)
        H_eval = H_clean[n_gt:]
        return H_eval, None, H_gt_static, {
            "snr_dB": int(d.get("snr_dB", snr_dB)),
            "ue_speed": 0.0,
            "is_static": True,
            "n_frames_total": H_active.shape[0],
            "n_frames_eval": len(H_eval),
            "n_frames_gt": n_gt,
            "n_removed": n_removed,
            "n_rx": H_active.shape[1],
            "n_tx": H_active.shape[2],
            "n_sc": H_active.shape[3],
        }
    else:
        H_gt_full = d["H_gt"][:, :, :, active] if has_gt else None
        # Apply same outlier mask to GT
        H_clean = H_active[clean_mask]
        H_gt_clean = H_gt_full[clean_mask] if H_gt_full is not None else None
        return H_clean, H_gt_clean, None, {
            "snr_dB": int(d.get("snr_dB", snr_dB)),
            "ue_speed": float(d.get("ue_speed", speed)),
            "is_static": False,
            "n_frames_total": H_active.shape[0],
            "n_frames_eval": len(H_clean),
            "n_removed": n_removed,
            "n_rx": H_active.shape[1],
            "n_tx": H_active.shape[2],
            "n_sc": H_active.shape[3],
        }


def scale_gt_to_srs(H_srs, H_gt):
    """Scale GT to match SRS amplitude level using per-pair real LS scalar.

    Uses a real positive scalar (not complex) so only the systematic
    amplitude difference (int16 vs Sionna normalization) is removed.
    Per-frame phase variations are preserved for fair tracking evaluation.
    """
    n_rx, n_tx = H_gt.shape[1], H_gt.shape[2]
    H_gt_scaled = H_gt.copy()
    for rx in range(n_rx):
        for tx in range(n_tx):
            g = H_gt[:, rx, tx, :].flatten()
            s = H_srs[:, rx, tx, :].flatten()
            rms_g = np.sqrt(np.mean(np.abs(g) ** 2))
            rms_s = np.sqrt(np.mean(np.abs(s) ** 2))
            if rms_g < 1e-30:
                continue
            scale = rms_s / rms_g
            H_gt_scaled[:, rx, tx, :] = scale * H_gt[:, rx, tx, :]
    return H_gt_scaled


def run_curves(H_eval, H_gt_per_frame, H_gt_static, alpha=0.1):
    """
    Run filter modes. Returns (nmse_A, nmse_B, nmse_C, nmse_D) arrays in dB.

    Static:  D = cumulative average (oracle), GT from split-set mean
    Dynamic: D = causal sliding-window avg, GT globally LS-scaled once
    """
    N, n_rx, n_tx, n_sc = H_eval.shape
    is_static = H_gt_static is not None

    # For dynamic: scale GT amplitude to match SRS (real scalar, preserves phase)
    if not is_static and H_gt_per_frame is not None:
        H_gt_per_frame = scale_gt_to_srs(H_eval, H_gt_per_frame)

    nmse_A = np.empty(N)
    nmse_B = np.empty(N)
    nmse_C = np.empty(N)
    nmse_D = np.empty(N)

    filt_B = [[SRS2DFilterVec(n_sc, alpha) for _ in range(n_tx)]
              for _ in range(n_rx)]
    filt_C = [[SRS2DFilterVec(n_sc, alpha) for _ in range(n_tx)]
              for _ in range(n_rx)]

    if is_static:
        H_cumsum = np.zeros((n_rx, n_tx, n_sc), dtype=H_eval.dtype)
    else:
        W = max(1, round(2.0 / alpha))
        ring = np.zeros((W, n_rx, n_tx, n_sc), dtype=H_eval.dtype)

    for n in range(N):
        frame = H_eval[n]
        gt = H_gt_static if is_static else H_gt_per_frame[n]

        nmse_A[n] = nmse_db(frame, gt)

        out_B = np.empty_like(frame)
        out_C = np.empty_like(frame)
        for rx in range(n_rx):
            for tx in range(n_tx):
                out_B[rx, tx] = filt_B[rx][tx].update_ema_only(frame[rx, tx])
                out_C[rx, tx] = filt_C[rx][tx].update(frame[rx, tx])
        nmse_B[n] = nmse_db(out_B, gt)
        nmse_C[n] = nmse_db(out_C, gt)

        if is_static:
            H_cumsum += frame
            nmse_D[n] = nmse_db(H_cumsum / (n + 1), gt)
        else:
            ring[n % W] = frame
            count = min(n + 1, W)
            H_avg = np.mean(ring[:count], axis=0) if n < W else np.mean(ring, axis=0)
            nmse_D[n] = nmse_db(H_avg, gt)

    return nmse_A, nmse_B, nmse_C, nmse_D


def steady_nmse(nmse_arr, tail=25):
    """Linear-average of last `tail` frames, then → dB."""
    vals = nmse_arr[-tail:]
    vals = vals[np.isfinite(vals)]
    if len(vals) == 0:
        return np.nan
    lin = 10.0 ** (vals / 10.0)
    return 10.0 * np.log10(np.mean(lin))


# ── Figure 1: NMSE vs frame ──

def plot_nmse_vs_frame(snr=20, speed=0.0, alpha=0.1, file_map=None):
    H_eval, H_gt_pf, H_gt_s, meta = load_data(snr, speed, file_map)
    A, B, C, D = run_curves(H_eval, H_gt_pf, H_gt_s, alpha)
    N = meta["n_frames_eval"]
    is_static = meta["is_static"]

    label_D = "D: Cumul. avg (oracle)" if is_static else f"D: Sliding avg (W={max(1,round(2/alpha))})"
    speed_str = "static" if is_static else f"speed={meta['ue_speed']:.0f} m/s"
    gt_str = "split-set GT" if is_static else "per-frame Sionna GT"

    print(f"  Data: {meta['n_frames_total']} total, {meta['n_removed']} outliers removed, "
          f"{N} eval frames [{gt_str}]")

    fig, ax = plt.subplots(figsize=(9, 5))
    frames = np.arange(1, N + 1)
    ax.plot(frames, A, ":", color="gray", alpha=0.6, label="A: Raw (no filter)")
    ax.plot(frames, B, "-.", color="tab:orange", label=f"B: EMA only (\u03b1={alpha})")
    ax.plot(frames, C, "-", color="tab:blue", lw=2,
            label=f"C: EMA+Boxcar (\u03b1={alpha})")
    ax.plot(frames, D, "--", color="tab:green", label=label_D)
    ax.set_xlabel("Eval frame index")
    ax.set_ylabel("NMSE (dB)")
    ax.set_title(f"2D MMSE \u2014 {speed_str}, SNR={snr} dB\n({gt_str})")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    tag = "static" if is_static else f"speed{int(meta['ue_speed'])}ms"
    out = OUT_DIR / f"fig1_nmse_vs_frame_snr{snr}_{tag}.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  [Fig1] saved \u2192 {out}")

    tail = min(25, N // 2)
    print(f"  Steady-state (last {tail} eval frames):")
    for label, arr in [("A raw", A), ("B EMA", B), ("C EMA+box", C), ("D oracle/sw", D)]:
        print(f"    {label:<12s} = {steady_nmse(arr, tail):+.2f} dB")
    return A, B, C, D


# ── Figure 2: Steady-state NMSE vs SNR ──

def plot_nmse_vs_snr(speed=0.0, alpha=0.1, file_map=None):
    snrs = sorted(file_map.keys()) if file_map else sorted(
        discover_files(speed).get(speed, {}).keys()
    )
    if not snrs:
        print(f"  No data found for speed={speed}")
        return

    is_static = (speed == 0.0)
    ss = {label: [] for label in ("A", "B", "C", "D")}

    for snr in snrs:
        H_eval, H_gt_pf, H_gt_s, meta = load_data(snr, speed, file_map)
        A, B, C, D = run_curves(H_eval, H_gt_pf, H_gt_s, alpha)
        tail = min(25, meta["n_frames_eval"] // 2)
        ss["A"].append(steady_nmse(A, tail))
        ss["B"].append(steady_nmse(B, tail))
        ss["C"].append(steady_nmse(C, tail))
        ss["D"].append(steady_nmse(D, tail))
        print(f"  SNR={snr:>2d}: {meta['n_removed']} outliers, {meta['n_frames_eval']} eval")

    speed_str = "static" if is_static else f"speed={speed:.0f} m/s"
    label_D = "D: Oracle avg" if is_static else f"D: Sliding avg"

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.plot(snrs, ss["A"], "s:", color="gray", label="A: Raw")
    ax.plot(snrs, ss["B"], "o-.", color="tab:orange", label=f"B: EMA (\u03b1={alpha})")
    ax.plot(snrs, ss["C"], "D-", color="tab:blue", lw=2,
            label=f"C: EMA+Box (\u03b1={alpha})")
    ax.plot(snrs, ss["D"], "^--", color="tab:green", label=label_D)
    ax.set_xlabel("SNR (dB)")
    ax.set_ylabel("Steady-state NMSE (dB)")
    ax.set_title(f"2D MMSE \u2014 {speed_str} (\u03b1={alpha})")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    tag = "static" if is_static else f"speed{int(speed)}ms"
    out = OUT_DIR / f"fig2_nmse_vs_snr_{tag}.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  [Fig2] saved \u2192 {out}")

    print(f"\n  {'SNR':>4s}  {'Raw':>8s}  {'EMA':>8s}  {'EMA+Box':>8s}  {'D':>8s}")
    for i, snr in enumerate(snrs):
        print(f"  {snr:>3d}   {ss['A'][i]:+8.2f}  {ss['B'][i]:+8.2f}  "
              f"{ss['C'][i]:+8.2f}  {ss['D'][i]:+8.2f}")


# ── Figure 3: alpha sweep ──

def plot_alpha_sweep(snr=20, speed=0.0, file_map=None):
    H_eval, H_gt_pf, H_gt_s, meta = load_data(snr, speed, file_map)
    N = meta["n_frames_eval"]
    tail = min(25, N // 2)
    is_static = meta["is_static"]

    alphas = np.array([0.01, 0.02, 0.05, 0.08, 0.1, 0.15, 0.2, 0.3, 0.5, 0.8, 1.0])
    ss_ema = []
    ss_2d = []

    for a in alphas:
        _, B, C, _ = run_curves(H_eval, H_gt_pf, H_gt_s, a)
        ss_ema.append(steady_nmse(B, tail))
        ss_2d.append(steady_nmse(C, tail))

    best_a_ema = alphas[np.argmin(ss_ema)]
    best_a_2d = alphas[np.argmin(ss_2d)]

    raw_nmse = steady_nmse(run_curves(H_eval, H_gt_pf, H_gt_s, 0.1)[0], tail)

    speed_str = "static" if is_static else f"speed={meta['ue_speed']:.0f} m/s"

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.plot(alphas, ss_ema, "o-", color="tab:orange",
            label=f"B: EMA only (best \u03b1={best_a_ema})")
    ax.plot(alphas, ss_2d, "D-", color="tab:blue", lw=2,
            label=f"C: EMA+Box (best \u03b1={best_a_2d})")
    ax.axhline(raw_nmse, ls=":", color="gray", label="A: Raw (no filter)")
    ax.set_xlabel("\u03b1 (EMA coefficient)")
    ax.set_ylabel("Steady-state NMSE (dB)")
    ax.set_title(f"\u03b1 sweep \u2014 {speed_str}, SNR={snr} dB")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    tag = "static" if is_static else f"speed{int(meta['ue_speed'])}ms"
    out = OUT_DIR / f"fig3_alpha_sweep_snr{snr}_{tag}.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  [Fig3] saved \u2192 {out}")
    print(f"  Best \u03b1 (EMA only):  {best_a_ema}  \u2192 {min(ss_ema):+.2f} dB")
    print(f"  Best \u03b1 (EMA+Box):   {best_a_2d}  \u2192 {min(ss_2d):+.2f} dB")

    for a, e, d in zip(alphas, ss_ema, ss_2d):
        print(f"    \u03b1={a:<5.2f}  EMA={e:+.2f}  EMA+Box={d:+.2f} dB")


# ── main ──

def main():
    import argparse
    parser = argparse.ArgumentParser(description="2D MMSE evaluation")
    parser.add_argument("--speed", type=float, default=None,
                        help="UE speed to evaluate (default: all available)")
    parser.add_argument("--snr", type=int, default=20,
                        help="SNR for Fig1/Fig3 (default: 20)")
    parser.add_argument("--alpha", type=float, default=0.1,
                        help="EMA alpha for Fig1/Fig2 (default: 0.1)")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    all_groups = discover_files(args.speed)
    if not all_groups:
        print("ERROR: no data found in", DATA_DIR)
        return

    for speed, file_map in sorted(all_groups.items()):
        speed_str = "static" if speed == 0 else f"speed={speed:.0f} m/s"
        snrs = sorted(file_map.keys())

        print("\n" + "=" * 65)
        print(f" 2D MMSE Evaluation \u2014 {speed_str}")
        print(f" Available SNR points: {snrs}")
        print("=" * 65)

        snr_for_fig1 = args.snr if args.snr in snrs else snrs[-1]

        print(f"\n\u2500\u2500 Fig 1: NMSE vs Frame (SNR={snr_for_fig1} dB) \u2500\u2500")
        plot_nmse_vs_frame(snr_for_fig1, speed, args.alpha, file_map)

        if len(snrs) >= 2:
            print(f"\n\u2500\u2500 Fig 2: Steady-state NMSE vs SNR \u2500\u2500")
            plot_nmse_vs_snr(speed, args.alpha, file_map)

        print(f"\n\u2500\u2500 Fig 3: \u03b1 sweep (SNR={snr_for_fig1} dB) \u2500\u2500")
        plot_alpha_sweep(snr_for_fig1, speed, file_map)

    print("\n" + "=" * 65)
    print(f" Done. Results in: {OUT_DIR}")
    print("=" * 65)


if __name__ == "__main__":
    main()
