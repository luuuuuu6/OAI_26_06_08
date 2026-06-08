#!/usr/bin/env python3
"""
plot_nmse_vs_snr_v2.py — clearer NMSE-vs-SNR + per-frame distribution.

Differences from v1:
  - 3 explicit lines per estimator: p10 / p50 / p90 (no shaded bands)
  - Annotates per-SNR sample count
  - Optional 2nd panel: per-frame NMSE distribution at one focus SNR (default 20)
  - Marks "different channel realization" SNR points if requested

Usage:
    python3 plot_nmse_vs_snr_v2.py \
        --legacy-dir /tmp/snrsweep_legacy \
        --new-dir    /tmp/snrsweep_pdpR \
        --focus-snr  20 \
        --out-dir    ~/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/data_out
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import numpy as np


def _import_helpers():
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    from eval_pdpR_nmse import (
        _import_loaders, detect_active_sc, pair_by_abs_slot, nmse_db,
        apply_sto_correction,
    )
    return _import_loaders, detect_active_sc, pair_by_abs_slot, nmse_db, apply_sto_correction


def list_snr_points(sweep_dir: Path) -> List[Tuple[float, Path]]:
    out = []
    for sub in sorted(sweep_dir.glob("snr_*dB")):
        m = re.match(r"snr_(-?\d+(?:\.\d+)?)dB", sub.name)
        if m:
            out.append((float(m.group(1)), sub))
    return out


def evaluate_run(run_dir: Path, srs_symbol: int = 12, skip_first: int = 0,
                 tol: int = 4, sto_correct: str = "none") -> Dict:
    _import_loaders, detect_active_sc, pair_by_abs_slot, nmse_db, apply_sto_correction = _import_helpers()
    load_srs_v2, load_gt = _import_loaders()
    H_srs, meta = load_srs_v2(str(run_dir), skip_first=skip_first)
    H_gt, gt_slots = load_gt(str(run_dir / "sionna_gt"), srs_symbol=srs_symbol,
                             return_slot_ids=True, skip_first=skip_first)
    srs_abs = np.asarray(meta["abs_slots"], dtype=np.int64)
    idx_s, idx_g = pair_by_abs_slot(srs_abs, gt_slots, tol=tol)
    if len(idx_s) < 1:
        return {"n_paired": 0, "ls_pf": np.array([])}
    H_s = H_srs[idx_s].astype(np.complex128)
    H_g = H_gt[idx_g].astype(np.complex128)
    active = detect_active_sc(H_s)
    if sto_correct != "none":
        H_s = apply_sto_correction(H_s, H_g, active, sto_correct)
    _, ls_pf, _ = nmse_db(H_s, H_g, active, ls_align=True)
    return {
        "n_paired": len(idx_s),
        "ls_pf": ls_pf,
        "p10":  float(np.percentile(ls_pf, 10)),
        "p50":  float(np.percentile(ls_pf, 50)),
        "p90":  float(np.percentile(ls_pf, 90)),
    }


def collect(sweep_dir: Path, label: str, sto_correct: str = "none") -> Dict[float, Dict]:
    pts = list_snr_points(sweep_dir)
    out: Dict[float, Dict] = {}
    print(f"\n=== {label}: {sweep_dir} ({len(pts)} SNR points, sto={sto_correct}) ===")
    for snr_db, run_dir in pts:
        try:
            res = evaluate_run(run_dir, sto_correct=sto_correct)
            out[snr_db] = res
            if res["n_paired"] > 0:
                print(f"  SNR={snr_db:+5.1f} dB | n={res['n_paired']:3d} | "
                      f"p10={res['p10']:+6.2f} p50={res['p50']:+6.2f} p90={res['p90']:+6.2f}")
            else:
                print(f"  SNR={snr_db:+5.1f} dB | (no paired frames)")
        except Exception as e:
            print(f"  SNR={snr_db:+5.1f} dB | ERROR: {e}")
    return out


def plot_main(rows_a: Dict[float, Dict], rows_b: Dict[float, Dict],
              label_a: str, label_b: str,
              focus_snr: Optional[float], png_path: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if focus_snr is not None and (focus_snr in rows_a or focus_snr in rows_b):
        fig, axes = plt.subplots(1, 2, figsize=(14, 5.5),
                                 gridspec_kw={"width_ratios": [3, 2]})
        ax = axes[0]
    else:
        fig, ax = plt.subplots(figsize=(9, 5.5))
        axes = [ax]

    # ---- Panel 1: percentiles vs SNR ----
    def get(rows, key):
        snrs = sorted(s for s in rows if rows[s]["n_paired"] > 0)
        return np.array(snrs), np.array([rows[s][key] for s in snrs]), \
               np.array([rows[s]["n_paired"] for s in snrs])

    for label, rows, color in [(label_a, rows_a, "#1f77b4"),
                                (label_b, rows_b, "#d62728")]:
        snrs, p50, n = get(rows, "p50")
        if len(snrs) == 0:
            continue
        _, p10, _ = get(rows, "p10")
        _, p90, _ = get(rows, "p90")
        ax.plot(snrs, p50, "o-",  color=color, lw=2.0, ms=8,
                label=f"{label} — median (p50)")
        ax.plot(snrs, p10, "v--", color=color, lw=1.0, ms=6, alpha=0.7,
                label=f"{label} — best 10% (p10)")
        ax.plot(snrs, p90, "^--", color=color, lw=1.0, ms=6, alpha=0.7,
                label=f"{label} — worst 10% (p90)")
        # annotate sample count beneath p50 markers
        for s, m, nn in zip(snrs, p50, n):
            ax.annotate(f"n={nn}", (s, m), xytext=(0, -16), textcoords="offset points",
                        ha="center", fontsize=7, color=color, alpha=0.8)

    ax.axhline(0, color="k", lw=0.5, alpha=0.4)
    ax.set_xlabel("Input SNR (dB)")
    ax.set_ylabel("LS-aligned NMSE (dB) — lower is better")
    ax.set_title("SRS channel-estimate NMSE vs SNR\n"
                 "(per-frame percentiles; n = paired-frame count)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=8, ncol=2)

    # ---- Panel 2: distribution at focus SNR (if present) ----
    if len(axes) > 1 and focus_snr is not None:
        ax2 = axes[1]
        bins = np.linspace(-50, 60, 56)
        for label, rows, color in [(label_a, rows_a, "#1f77b4"),
                                    (label_b, rows_b, "#d62728")]:
            if focus_snr not in rows or rows[focus_snr]["n_paired"] == 0:
                continue
            data = rows[focus_snr]["ls_pf"]
            ax2.hist(data, bins=bins, alpha=0.4, color=color,
                     label=f"{label} (n={len(data)}, p50={np.percentile(data,50):+.1f})")
            ax2.axvline(np.percentile(data, 10), color=color, ls=":", lw=1.2)
            ax2.axvline(np.percentile(data, 50), color=color, ls="-",  lw=1.5)
            ax2.axvline(np.percentile(data, 90), color=color, ls="--", lw=1.2)
        ax2.axvline(0, color="k", lw=0.5, alpha=0.4)
        ax2.set_xlabel("Per-frame NMSE (dB)")
        ax2.set_ylabel("Frame count")
        ax2.set_title(f"Per-frame NMSE distribution @ SNR={focus_snr:.0f} dB\n"
                      "vertical lines = p10/p50/p90")
        ax2.grid(True, alpha=0.3)
        ax2.legend(loc="upper right", fontsize=8)

    fig.tight_layout()
    fig.savefig(png_path, dpi=160)
    plt.close(fig)
    print(f"\n[png] {png_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--legacy-dir", required=True, type=Path)
    ap.add_argument("--new-dir",    required=True, type=Path)
    ap.add_argument("--out-dir",    type=Path,
                    default=Path("/home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/data_out"))
    ap.add_argument("--focus-snr",  type=float, default=20.0,
                    help="SNR (dB) for the per-frame distribution panel; "
                         "set negative to skip the panel")
    ap.add_argument("--sto-correct", choices=["none", "global", "per-frame"],
                    default="none",
                    help="Apply STO (timing-offset) correction to SRS before NMSE")
    ap.add_argument("--out-suffix", default="",
                    help="Append suffix to output file names (e.g. '_sto_global')")
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    rows_a = collect(args.legacy_dir, "legacy",      sto_correct=args.sto_correct)
    rows_b = collect(args.new_dir,    "PDP-R (new)", sto_correct=args.sto_correct)

    focus = None if args.focus_snr < 0 else args.focus_snr
    suffix = args.out_suffix or (f"_sto_{args.sto_correct}" if args.sto_correct != "none" else "")
    out_png = args.out_dir / f"nmse_vs_snr_v2{suffix}.png"
    plot_main(rows_a, rows_b, f"legacy ({args.sto_correct})",
              f"PDP-R ({args.sto_correct})", focus, out_png)
    print("Done.")


if __name__ == "__main__":
    main()
