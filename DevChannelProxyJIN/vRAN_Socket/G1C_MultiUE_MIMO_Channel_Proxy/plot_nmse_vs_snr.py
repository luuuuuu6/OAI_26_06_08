#!/usr/bin/env python3
"""
plot_nmse_vs_snr.py  — compare NMSE-vs-SNR for two estimators.

Reuses eval_pdpR_nmse.py for IO + NMSE math; only adds the multi-SNR loop
and matplotlib plotting on top. No STO correction, no PCA — same simple
KPI as the single-point evaluator.

Usage:
    python3 plot_nmse_vs_snr.py \
        --legacy-dir /tmp/snrsweep_legacy \
        --new-dir    /tmp/snrsweep_pdpR \
        --out-dir    /home/.../data_out
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np


def _import_helpers():
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    from eval_pdpR_nmse import (
        _import_loaders, detect_active_sc, pair_by_abs_slot, nmse_db,
    )
    return _import_loaders, detect_active_sc, pair_by_abs_slot, nmse_db


def list_snr_points(sweep_dir: Path) -> List[Tuple[float, Path]]:
    """Find snr_*dB subdirs and parse SNR values."""
    out: List[Tuple[float, Path]] = []
    for sub in sorted(sweep_dir.glob("snr_*dB")):
        m = re.match(r"snr_(-?\d+(?:\.\d+)?)dB", sub.name)
        if m:
            out.append((float(m.group(1)), sub))
    return out


def evaluate_run(run_dir: Path, srs_symbol: int = 12, skip_first: int = 0,
                 tol: int = 4) -> Dict:
    _import_loaders, detect_active_sc, pair_by_abs_slot, nmse_db = _import_helpers()
    load_srs_v2, load_gt = _import_loaders()

    H_srs, meta = load_srs_v2(str(run_dir), skip_first=skip_first)
    H_gt, gt_slots = load_gt(str(run_dir / "sionna_gt"), srs_symbol=srs_symbol,
                             return_slot_ids=True, skip_first=skip_first)
    srs_abs = np.asarray(meta["abs_slots"], dtype=np.int64)
    idx_s, idx_g = pair_by_abs_slot(srs_abs, gt_slots, tol=tol)

    if len(idx_s) < 1:
        return {"n_paired": 0}

    H_s = H_srs[idx_s].astype(np.complex128)
    H_g = H_gt[idx_g].astype(np.complex128)
    active = detect_active_sc(H_s)

    raw_dB, raw_pf, _ = nmse_db(H_s, H_g, active, ls_align=False)
    ls_dB,  ls_pf, _ = nmse_db(H_s, H_g, active, ls_align=True)

    return {
        "n_paired":  len(idx_s),
        "n_active":  int(np.sum(active)),
        "raw_agg":   float(raw_dB),
        "ls_agg":    float(ls_dB),
        "ls_p10":    float(np.percentile(ls_pf, 10)),
        "ls_p50":    float(np.percentile(ls_pf, 50)),
        "ls_p90":    float(np.percentile(ls_pf, 90)),
    }


def collect(sweep_dir: Path, label: str) -> List[Dict]:
    pts = list_snr_points(sweep_dir)
    rows: List[Dict] = []
    print(f"\n=== {label}: {sweep_dir} ({len(pts)} SNR points) ===")
    for snr_db, run_dir in pts:
        try:
            res = evaluate_run(run_dir)
            res["snr_dB"] = snr_db
            res["estimator"] = label
            rows.append(res)
            if res.get("n_paired", 0) > 0:
                print(f"  SNR={snr_db:+5.1f} dB | paired={res['n_paired']:3d} | "
                      f"NMSE_LS_agg={res['ls_agg']:+6.2f}  "
                      f"p10={res['ls_p10']:+6.2f}  p50={res['ls_p50']:+6.2f}  "
                      f"p90={res['ls_p90']:+6.2f}")
            else:
                print(f"  SNR={snr_db:+5.1f} dB | no paired frames; skipped")
        except Exception as e:
            print(f"  SNR={snr_db:+5.1f} dB | ERROR: {e}")
    return rows


def write_csv(rows: List[Dict], path: Path):
    import csv
    cols = ["estimator", "snr_dB", "n_paired", "n_active",
            "raw_agg", "ls_agg", "ls_p10", "ls_p50", "ls_p90"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    print(f"\n[csv] {path}")


def make_plot(rows_a: List[Dict], rows_b: List[Dict],
              label_a: str, label_b: str, png_path: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def to_arrays(rows):
        rows = sorted([r for r in rows if r.get("n_paired", 0) > 0],
                      key=lambda r: r["snr_dB"])
        snr  = np.array([r["snr_dB"] for r in rows])
        agg  = np.array([r["ls_agg"] for r in rows])
        p10  = np.array([r["ls_p10"] for r in rows])
        p50  = np.array([r["ls_p50"] for r in rows])
        p90  = np.array([r["ls_p90"] for r in rows])
        return snr, agg, p10, p50, p90

    sa, agg_a, p10_a, p50_a, p90_a = to_arrays(rows_a)
    sb, agg_b, p10_b, p50_b, p90_b = to_arrays(rows_b)

    fig, ax = plt.subplots(figsize=(8.4, 5.8))
    if len(sa) > 0:
        ax.fill_between(sa, p10_a, p90_a, alpha=0.15, color="C0")
        ax.plot(sa, p50_a, "o-", color="C0", label=f"{label_a} (median)")
        ax.plot(sa, agg_a, "x:", color="C0", label=f"{label_a} (aggregate)")
    if len(sb) > 0:
        ax.fill_between(sb, p10_b, p90_b, alpha=0.15, color="C1")
        ax.plot(sb, p50_b, "s-", color="C1", label=f"{label_b} (median)")
        ax.plot(sb, agg_b, "x:", color="C1", label=f"{label_b} (aggregate)")
    ax.axhline(0, color="k", lw=0.5, alpha=0.3)
    ax.set_xlabel("Input SNR (dB)")
    ax.set_ylabel("NMSE (dB) — LS-aligned, lower is better")
    ax.set_title("SRS channel-estimate NMSE vs SNR\n"
                 "shaded band = per-frame [p10, p90]")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=9)
    fig.tight_layout()
    fig.savefig(png_path, dpi=140)
    plt.close(fig)
    print(f"[png] {png_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--legacy-dir", required=True, type=Path,
                    help="Sweep root for legacy estimator (contains snr_*dB/)")
    ap.add_argument("--new-dir", required=True, type=Path,
                    help="Sweep root for the new PDP->R estimator")
    ap.add_argument("--out-dir", type=Path,
                    default=Path("/home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/data_out"),
                    help="Where to write the CSV + PNG (default data_out/)")
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    rows_legacy = collect(args.legacy_dir, "legacy")
    rows_new    = collect(args.new_dir,    "PDP-R (new)")

    write_csv(rows_legacy + rows_new, args.out_dir / "nmse_vs_snr.csv")
    make_plot(rows_legacy, rows_new, "legacy", "PDP-R (new)",
              args.out_dir / "nmse_vs_snr.png")

    print("\nDone.")


if __name__ == "__main__":
    main()
