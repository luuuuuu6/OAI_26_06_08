#!/usr/bin/env python3
"""DYNAMIC cross-model bar chart at 60 km/h (~SNR 17 dB): three-way NMSE
(raw LS / legacy / true2d) + true2d gain over both baselines. Mirrors the static
native_cdl_nd_minus3_bars.png. 5-model, native CDL, rep=3, STO-aligned gate.
(60 km/h = highest speed with all three estimators measured: LS from the
true2d-vs-passthru run, legacy from the true2d-vs-legacy run, same seed.)"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams.update({
    "font.family": "DejaVu Serif", "mathtext.fontset": "dejavuserif",
    "font.size": 13, "axes.grid": True, "grid.alpha": 0.35, "grid.linestyle": ":",
    "figure.dpi": 140, "savefig.bbox": "tight",
})

# per-model NMSE [dB] at 60 km/h, ~SNR 17 dB (order A,D,B,C,E by RMS delay)
LABELS = ["CDL-A", "CDL-D", "CDL-B", "CDL-C", "CDL-E"]
LS     = [-15.53, -16.13, -14.77, -16.11, -15.13]
LEGACY = [-19.09, -19.61, -18.34, -19.63, -18.64]
TRUE2D = [-22.65, -22.84, -20.92, -21.75, -19.29]

x = list(range(len(LABELS)))
gain_ls  = [l - t for l, t in zip(LS, TRUE2D)]
gain_leg = [l - t for l, t in zip(LEGACY, TRUE2D)]

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5.2), constrained_layout=True)

w = 0.27
ax1.bar([i - w for i in x], LS, width=w, label="raw LS", color="#999999",
        edgecolor="black", linewidth=0.6)
ax1.bar(x, LEGACY, width=w, label="legacy (FIR)", color="#0072B2",
        edgecolor="black", linewidth=0.6)
ax1.bar([i + w for i in x], TRUE2D, width=w, label="true2d (2D-MMSE)", color="#D55E00",
        edgecolor="black", linewidth=0.6)
ax1.set_xticks(x, LABELS)
ax1.set_ylabel("NMSE [dB]   (lower = better)")
ax1.set_title("(a) Three-way NMSE per CDL profile")
ax1.grid(axis="y", alpha=0.35, linestyle=":")
ax1.legend(fontsize=10)

w2 = 0.38
b1 = ax2.bar([i - w2/2 for i in x], gain_ls, width=w2, label="true2d $-$ raw LS",
             color="#777777", edgecolor="black", linewidth=0.6)
b2 = ax2.bar([i + w2/2 for i in x], gain_leg, width=w2, label="true2d $-$ legacy",
             color="#D55E00", edgecolor="black", linewidth=0.6)
ax2.set_xticks(x, LABELS)
ax2.set_ylabel("true2d gain [dB]   (higher = better)")
ax2.set_title("(b) true2d gain over each baseline")
ax2.grid(axis="y", alpha=0.35, linestyle=":")
ax2.bar_label(b1, fmt="%.1f", padding=2, fontsize=8.5)
ax2.bar_label(b2, fmt="%.1f", padding=2, fontsize=8.5)
ax2.legend(fontsize=10)

fig.suptitle("Cross-model comparison at 60 km/h  (dynamic, $\\approx$SNR 17 dB, native CDL)",
             fontsize=14, y=1.05)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native_cdl_dynamic_bars.png")
fig.savefig(out, dpi=150)
print(f"wrote {out}")
