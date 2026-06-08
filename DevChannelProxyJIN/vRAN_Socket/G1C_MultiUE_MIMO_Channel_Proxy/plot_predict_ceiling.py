#!/usr/bin/env python3
"""Decisive "classical prediction ceiling vs horizon" figure (Direction A).

Reads the CSV written by ``test_srs_2d_offline.py --predict-sweep --predict-wiener``
and draws, per (speed, SRS period), the self-contained NMSE-vs-horizon curves for
the three reference predictors:

  * ZOH            -- hold-last baseline (do nothing)
  * EMA-Wiener     -- the deployable classical predictor (== the C nr_srs_wiener)
  * genie-Wiener   -- the classical LINEAR-MMSE ceiling (true second-order stats)

The "knee" (first horizon where the deployable predictor stops beating ZOH) is
marked on each panel. This figure is the report core AND the AI go/no-go
criterion: the genie curve is the classical ceiling -- AI can only earn the gap
ABOVE genie at the horizons where genie itself still beats ZOH.

Usage:
  python3 plot_predict_ceiling.py ceiling.csv --model cdl_c --out predict_ceiling.png
"""
import argparse
import csv
import collections


def load(path):
    rows = []
    with open(path) as fh:
        for r in csv.DictReader(fh):
            rows.append(r)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", help="CSV from --predict-sweep --predict-wiener")
    ap.add_argument("--model", default="cdl_c", help="channel model to plot (ds label)")
    ap.add_argument("--out", default="predict_ceiling.png")
    args = ap.parse_args()

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = [r for r in load(args.csv) if r["model"] == args.model]
    if not rows:
        raise SystemExit(f"no rows for model={args.model} in {args.csv}")

    speeds = sorted({float(r["speed_kmh"]) for r in rows})
    periods = sorted({int(r["period_slots"]) for r in rows})

    # cell[(period, speed)][horizon] = dict of the metric columns
    cell = collections.defaultdict(dict)
    for r in rows:
        per = int(r["period_slots"])
        spd = float(r["speed_kmh"])
        k = int(r["horizon"])
        cell[(per, spd)][k] = r

    nrows, ncols = len(periods), len(speeds)
    fig, axes = plt.subplots(nrows, ncols, figsize=(3.6 * ncols, 3.2 * nrows),
                             squeeze=False, sharex=True)
    for i, per in enumerate(periods):
        for j, spd in enumerate(speeds):
            ax = axes[i][j]
            d = cell.get((per, spd), {})
            ks = sorted(d.keys())
            if not ks:
                ax.set_visible(False)
                continue
            zoh = [float(d[k]["zoh_nmse_db"]) for k in ks]
            ema = [float(d[k]["wiener_ema_db"]) for k in ks]
            gen = [float(d[k]["wiener_genie_db"]) for k in ks]
            ax.plot(ks, zoh, "o-", color="0.5", label="ZOH (hold-last)")
            ax.plot(ks, ema, "s-", color="tab:blue", label="EMA-Wiener (deployable)")
            ax.plot(ks, gen, "^--", color="tab:green", label="genie (classical ceiling)")

            # knee: first horizon where EMA no longer beats ZOH (gain <= 0)
            knee = None
            for k in ks:
                if float(d[k]["wiener_ema_db"]) - float(d[k]["zoh_nmse_db"]) >= 0.0:
                    knee = k
                    break
            if knee is not None:
                ax.axvline(knee, color="tab:red", ls=":", lw=1.2)
                ax.annotate(f"knee k={knee}", (knee, ax.get_ylim()[1]),
                            color="tab:red", fontsize=7, va="top", ha="left")

            ax.set_title(f"{args.model}  {spd:.0f} km/h  P={per}", fontsize=9)
            ax.grid(True, alpha=0.3)
            if j == 0:
                ax.set_ylabel("NMSE (dB)")
            if i == nrows - 1:
                ax.set_xlabel("prediction horizon k (SRS frames)")
            if i == 0 and j == 0:
                ax.legend(fontsize=7, loc="lower right")

    fig.suptitle(f"Classical SRS-prediction ceiling vs horizon ({args.model}); "
                 f"lower=better; gap above genie = AI opportunity", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(args.out, dpi=130)
    print(f"[ceiling] wrote {args.out}")

    # text summary: per (period,speed) knee + genie best gain at k=1
    print(f"\n=== {args.model}: knee (EMA beats ZOH up to) + genie gain@k1 ===")
    print("period  speed   knee   genie@k1(gain vs ZOH)")
    for per in periods:
        for spd in speeds:
            d = cell.get((per, spd), {})
            if not d:
                continue
            ks = sorted(d.keys())
            knee = next((k for k in ks
                         if float(d[k]["wiener_ema_db"]) - float(d[k]["zoh_nmse_db"]) >= 0.0),
                        None)
            k1 = ks[0]
            g1 = float(d[k1]["zoh_nmse_db"]) - float(d[k1]["wiener_genie_db"])
            print(f"  {per:<5d} {spd:6.0f}   {('k='+str(knee)) if knee else '>max':>5s}"
                  f"   {float(d[k1]['wiener_genie_db']):+6.2f} dB ({g1:+.2f})")


if __name__ == "__main__":
    main()
