#!/usr/bin/env python3
"""true2d gain over OAI legacy (filt8/16 FIR) vs UE speed, fixed sweet-spot SNR
(-nd -6 ~17 dB), native CDL A-E, rep=3, per-launch 5GC restart, STO-aligned gate.
gain = legacy_NMSE - true2d_NMSE (>0 = true2d beats legacy). Academic small
multiples (one panel per model, real dB y-axis) + 5-model mean panel."""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams.update({
    "font.family": "DejaVu Serif", "mathtext.fontset": "dejavuserif",
    "font.size": 13, "axes.grid": True, "grid.alpha": 0.35, "grid.linestyle": ":",
    "lines.linewidth": 2.2, "lines.markersize": 8, "figure.dpi": 140, "savefig.bbox": "tight",
})

# model -> {speed: (gain_dB, valid)}; valid=0 = one estimator's run degraded (coh<0.9)
DATA = {
    "CDL-A (290 ns)":  {0:(4.63,1), 30:(2.17,1), 60:(3.53,1), 120:(3.19,1), 300:(3.40,1)},
    "CDL-D (376 ns)":  {0:(4.48,1), 30:(2.36,1), 60:(3.29,1), 120:(3.00,1), 300:(2.70,1)},
    "CDL-B (478 ns)":  {0:(5.05,1), 30:(0,0),    60:(0,0),    120:(2.44,1), 300:(2.30,1)},
    "CDL-C (865 ns)":  {0:(3.64,1), 30:(2.11,1), 60:(2.27,1), 120:(2.01,1), 300:(2.14,1)},
    "CDL-E (2064 ns)": {0:(2.99,1), 30:(1.23,1), 60:(0.74,1), 120:(1.05,1), 300:(0.75,1)},
}
STYLE = {
    "CDL-A (290 ns)":  dict(color="#0072B2", marker="o", linestyle="-"),
    "CDL-D (376 ns)":  dict(color="#D55E00", marker="s", linestyle="--"),
    "CDL-B (478 ns)":  dict(color="#009E73", marker="^", linestyle="-."),
    "CDL-C (865 ns)":  dict(color="#CC79A7", marker="D", linestyle=":"),
    "CDL-E (2064 ns)": dict(color="#9467bd", marker="v", linestyle=(0, (3, 1, 1, 1))),
}
ORDER = ["CDL-A (290 ns)", "CDL-D (376 ns)", "CDL-B (478 ns)",
         "CDL-C (865 ns)", "CDL-E (2064 ns)"]
SPEEDS = [0, 30, 60, 120, 300]

mean_y = []
for sp in SPEEDS:
    vals = [DATA[m][sp][0] for m in DATA if DATA[m][sp][1] == 1]
    mean_y.append(sum(vals) / len(vals) if vals else float("nan"))

YLIM = (0, 6)
fig, axes = plt.subplots(2, 3, figsize=(13.5, 7.6), sharex=True, sharey=True,
                         constrained_layout=True)
axes = axes.ravel()
for ax, label in zip(axes[:5], ORDER):
    d = DATA[label]; st = STYLE[label]
    xs = [sp for sp in SPEEDS if d[sp][1] == 1]
    ys = [d[sp][0] for sp in SPEEDS if d[sp][1] == 1]
    ax.plot(xs, ys, markeredgecolor="white", markeredgewidth=0.8, **st)
    for sp in xs:
        ax.annotate(f"{d[sp][0]:.1f}", (sp, d[sp][0]), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=9, color=st["color"])
    ax.set_title(label, fontsize=13)

axm = axes[5]
axm.plot(SPEEDS, mean_y, color="black", marker="*", markersize=13, linewidth=2.6,
         markeredgecolor="white", markeredgewidth=0.8)
for sp, g in zip(SPEEDS, mean_y):
    axm.annotate(f"{g:.1f}", (sp, g), textcoords="offset points", xytext=(0, 8),
                 ha="center", fontsize=9, color="black")
axm.set_title("5-model mean", fontsize=13, fontweight="bold")

for i, ax in enumerate(axes):
    ax.axhline(0, color="0.4", linewidth=0.9)
    ax.set_xscale("symlog", linthresh=10)
    ax.set_xticks(SPEEDS); ax.set_xticklabels([str(s) for s in SPEEDS])
    ax.set_xlim(-2, 360); ax.set_ylim(*YLIM)
    ax.grid(True, alpha=0.35, linestyle=":")
    if i % 3 == 0:
        ax.set_ylabel("true2d gain over\nlegacy (FIR) [dB]")
    if i >= 3:
        ax.set_xlabel("UE speed [km/h]")

fig.suptitle("true2d gain over OAI legacy (FIR) vs UE speed  (native CDL, fixed $\\approx$SNR 17 dB)\n"
             "true2d beats legacy at EVERY speed incl. 300 km/h: ~4 dB static, ~2-2.5 dB moving "
             "(no collapse \u2014 freq-stage Wiener > fixed FIR even when PKF dies)",
             fontsize=13.5, y=1.12)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native_cdl_gain_vs_speed_legacy.png")
fig.savefig(out, dpi=150)
print(f"wrote {out}")
print("mean gain-over-legacy by speed: " + ", ".join(f"{s}={g:.1f}" for s, g in zip(SPEEDS, mean_y)))
