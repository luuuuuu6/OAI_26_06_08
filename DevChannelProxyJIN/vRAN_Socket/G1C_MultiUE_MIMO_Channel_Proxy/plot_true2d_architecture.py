#!/usr/bin/env python3
"""Comparative architecture of the three SRS channel estimators, built on a
shared raw-LS front-end and progressing in sophistication:
    raw LS (passthru)  ->  OAI legacy (fixed FIR)  ->  true2d (2D-MMSE).
Clean academic block diagram."""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

plt.rcParams.update({"font.family": "DejaVu Sans", "figure.dpi": 150, "savefig.bbox": "tight"})

C_LS, C_LEG, C_T2 = "#6b6b6b", "#0072B2", "#D55E00"
EDGE = "#222222"

fig, ax = plt.subplots(figsize=(16, 9))
ax.set_xlim(0, 16); ax.set_ylim(0, 10); ax.axis("off")


def box(x, y, w, h, title, body, color, fill, tfs=12.5, bfs=10.5):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.04,rounding_size=0.12",
                                facecolor=fill, edgecolor=color, linewidth=2.0))
    ax.text(x + w / 2, y + h - 0.30, title, ha="center", va="top",
            fontsize=tfs, fontweight="bold", color=color)
    if body:
        ax.text(x + w / 2, y + h - 0.78, body, ha="center", va="top",
                fontsize=bfs, color="#222", linespacing=1.35)


def arrow(x1, y1, x2, y2, color=EDGE, lw=2.2, ls="-", label=None, lab_dy=0.16):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=17,
                                 linewidth=lw, color=color, linestyle=ls,
                                 connectionstyle="arc3,rad=0"))
    if label:
        ax.text((x1 + x2) / 2, (y1 + y2) / 2 + lab_dy, label, ha="center", va="bottom",
                fontsize=10, color=color, fontweight="bold")


ax.text(8, 9.6, "Three SRS channel estimators on a shared raw-LS front-end:  "
        "raw LS  →  OAI legacy (FIR)  →  true2d (2D-MMSE)",
        ha="center", va="center", fontsize=17, fontweight="bold")

# ---- shared front-end ----
box(0.3, 3.9, 3.0, 2.4, "Front-end (gNB)",
    "UE SRS, comb pilots\n\ngNB rxdataF\n\n$\\hat{H}_{LS}=Rx\\cdot conj(X)$\n(raw LS at pilots)",
    EDGE, "#eeeeee", tfs=13)
ax.text(1.8, 3.7, "shared $\\hat{H}_{LS}$", ha="center", va="top", fontsize=10, style="italic", color="#444")

# branch point
bx = 3.3

# ---- lane 1: passthru = raw LS ----
y1 = 7.7
box(4.4, y1, 4.2, 1.5, "passthru  =  raw LS",
    "output $\\hat{H}_{LS}$ unchanged\n(zero prior, zero processing)", C_LS, "#f2f2f2")
arrow(bx, 5.1, 4.4, y1 + 0.75, color=C_LS, label="$\\hat{H}_{LS}$")

# ---- lane 2: legacy ----
y2 = 4.65
box(4.4, y2, 4.6, 1.7, "legacy  (OAI default)",
    "LS + filt8/16 fixed FIR\nfrequency smoothing\n(hard-coded taps, per-symbol,\nzero adaptation)", C_LEG, "#e8f1fa")
arrow(bx, 5.0, 4.4, y2 + 0.85, color=C_LEG, label="$\\hat{H}_{LS}$")

# ---- lane 3: true2d (3 stages + gate) ----
y3 = 1.0
box(4.4, y3, 2.7, 2.1, "Stage 1:\nFreq Wiener",
    "LMMSE; PDP→R→\nToeplitz solve\n(data-driven,\nadaptive)", C_T2, "#fdece1", tfs=12, bfs=9.5)
box(7.5, y3, 2.5, 2.1, "Stage 2:\nTime PKF",
    "phase-predict\nKalman\n(IAE-Riccati)", C_T2, "#fdece1", tfs=12, bfs=9.5)
box(10.4, y3, 2.7, 2.1, "SNR gate",
    "high SNR →\nfall back\nto legacy", C_T2, "#fdece1", tfs=12, bfs=9.5)
arrow(bx, 4.7, 4.4, y3 + 0.95, color=C_T2, label="$\\hat{H}_{LS}$")
arrow(7.1, y3 + 0.95, 7.5, y3 + 0.95, color=C_T2, lw=2.0)
arrow(10.0, y3 + 0.95, 10.4, y3 + 0.95, color=C_T2, lw=2.0)
# gate fallback source: legacy estimate (dotted blue) into the gate
ax.add_patch(FancyArrowPatch((9.0, y2 + 0.0), (11.75, y3 + 1.9), arrowstyle="-|>",
             mutation_scale=14, linewidth=1.5, color=C_LEG, linestyle=(0, (4, 3)),
             connectionstyle="arc3,rad=-0.25"))
ax.text(10.9, 3.45, "legacy as\nhigh-SNR fallback", ha="center", va="center",
        fontsize=8.5, color=C_LEG, style="italic")

# ---- outputs (right) with gain annotation ----
ox = 13.5
arrow(8.6, y1 + 0.75, ox, y1 + 0.75, color=C_LS, lw=2.0)
ax.text(ox + 0.05, y1 + 0.75, "$\\hat{H}_{LS}$\n(baseline)", ha="left", va="center", fontsize=10, color=C_LS)
arrow(9.0, y2 + 0.85, ox, y2 + 0.85, color=C_LEG, lw=2.0)
ax.text(ox + 0.05, y2 + 0.85, "$\\hat{H}_{legacy}$\n~+3.6 dB vs LS\n(fixed, speed-robust)", ha="left", va="center", fontsize=9.5, color=C_LEG)
arrow(13.1, y3 + 0.95, ox, y3 + 0.95, color=C_T2, lw=2.0)
ax.text(ox + 0.05, y3 + 0.95, "$\\hat{H}_{true2d}$\nvs LS: −9~−12 dB (static)\n     ~6~8 dB (dyn 0-300km/h)\nvs legacy: −2~−7 dB", ha="left", va="center", fontsize=9.5, color=C_T2)

# sophistication arrow (far left)
ax.annotate("", xy=(0.18, 1.2), xytext=(0.18, 8.6),
            arrowprops=dict(arrowstyle="-|>", color="#999", lw=1.6))
ax.text(0.06, 4.9, "increasing denoising / complexity", rotation=90, ha="center", va="center",
        fontsize=10, color="#888")

ax.text(8, 0.35,
        "shared raw-LS front-end -> three estimators of increasing power:  "
        "passthru (zero-processing baseline)  ·  legacy (fixed FIR freq smoothing)  ·  "
        "true2d (data-driven two-stage MMSE + high-SNR legacy gate)",
        ha="center", va="center", fontsize=10.5, color="#444")

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "true2d_oai_architecture.png")
fig.savefig(out, dpi=170)
print(f"wrote {out}")
