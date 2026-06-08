#!/usr/bin/env python3
"""Publication-style plots of true2d-vs-passthru(raw LS) gain for the native CDL
static SNR sweep.

Outputs:
  native_cdl_gain.png                 two-panel overview (gain curves + NMSE).
  native_cdl_gain_small_multiples.png one panel per CDL model (no overlap).
  native_cdl_nd_minus3_bars.png       cross-model bars at the -nd=-3 point.

Data = clean/usable regime from the 5-model x SNR static sweep
(sweep_native_cdl.sh, -nd -15..0). Drowned points (-nd 3, and 0 for B/C/D where
passthru NMSE went positive / coherence collapsed) are excluded.
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---- academic styling -------------------------------------------------------
plt.rcParams.update({
    "font.family": "DejaVu Serif",
    "mathtext.fontset": "dejavuserif",
    "font.size": 13,
    "axes.titlesize": 14,
    "axes.labelsize": 13,
    "legend.fontsize": 11,
    "xtick.labelsize": 12,
    "ytick.labelsize": 12,
    "axes.linewidth": 1.2,
    "axes.grid": True,
    "grid.alpha": 0.35,
    "grid.linestyle": ":",
    "lines.linewidth": 2.4,
    "lines.markersize": 8,
    "legend.frameon": True,
    "legend.framealpha": 0.92,
    "legend.edgecolor": "0.6",
    "figure.dpi": 140,
    "savefig.bbox": "tight",
})

# model -> list of (noise_power_dB, passthru_NMSE_dB, true2d_NMSE_dB)
DATA = {
    "CDL-A (290 ns)":  [(-15,-35.01,-37.06),(-12,-29.90,-33.31),(-9,-24.16,-29.79),(-6,-18.21,-26.40),(-3,-12.24,-22.91),(0,-6.23,-18.38)],
    "CDL-D (376 ns)":  [(-15,-33.78,-33.76),(-12,-28.43,-30.99),(-9,-22.62,-27.93),(-6,-16.66,-24.70),(-3,-10.67,-21.04)],
    "CDL-B (478 ns)":  [(-15,-33.32,-33.33),(-12,-28.02,-31.11),(-9,-22.22,-28.36),(-6,-16.27,-24.93),(-3,-10.28,-20.44)],
    "CDL-C (865 ns)":  [(-15,-34.16,-34.17),(-12,-28.96,-31.29),(-9,-23.20,-27.97),(-6,-17.25,-24.48),(-3,-11.26,-20.47)],
    "CDL-E (2064 ns)": [(-15,-35.22,-36.78),(-12,-29.85,-32.76),(-9,-24.01,-28.66),(-6,-18.05,-24.62),(-3,-12.07,-20.75),(0,-6.08,-16.37)],
}

# distinct (color, marker, linestyle) per model -> separable even in grayscale
STYLE = {
    "CDL-A (290 ns)":  dict(color="#0072B2", marker="o", linestyle="-"),
    "CDL-D (376 ns)":  dict(color="#D55E00", marker="s", linestyle="--"),
    "CDL-B (478 ns)":  dict(color="#009E73", marker="^", linestyle="-."),
    "CDL-C (865 ns)":  dict(color="#CC79A7", marker="D", linestyle=":"),
    "CDL-E (2064 ns)": dict(color="#9467bd", marker="v", linestyle=(0, (3, 1, 1, 1))),
}

# legacy(OAI FIR) vs true2d (NO gate), matched channel (seed 12345).
# model -> list of (noise_power_dB, legacy_NMSE_dB, true2d_NMSE_dB)
LEGACY = {
    "CDL-A (290 ns)":  [(-15,-38.51,-37.06),(-12,-33.44,-33.31),(-9,-27.73,-29.78),(-6,-21.80,-26.40),(-3,-15.81,-22.88),(0,-9.82,-18.13)],
    "CDL-D (376 ns)":  [(-15,-37.25,-33.79),(-12,-31.98,-30.98),(-9,-26.20,-27.94),(-6,-20.25,-24.70),(-3,-14.25,-21.04)],
    "CDL-B (478 ns)":  [(-15,-36.78,-33.34),(-12,-31.57,-31.10),(-9,-25.80,-28.34),(-6,-19.86,-24.90),(-3,-13.86,-20.47)],
    "CDL-C (865 ns)":  [(-15,-37.65,-34.16),(-12,-32.53,-31.28),(-9,-26.76,-27.97),(-6,-20.83,-24.45),(-3,-14.85,-20.45)],
    "CDL-E (2064 ns)": [(-15,-38.15,-36.77),(-12,-33.23,-32.76),(-9,-27.54,-28.66),(-6,-21.62,-24.62),(-3,-15.66,-20.71),(0,-9.66,-16.34)],
}

# true2d WITH high-SNR legacy-fallback gate ON (SRS_2D_LEGACY_GATE_DB=26, width=4),
# same channel (seed 12345). model -> list of (noise_power_dB, true2d_gated_NMSE_dB).
# High SNR (nd -15/-12) blends to legacy -> ties; low SNR keeps full MMSE wins.
GATED = {
    "CDL-A (290 ns)":  [(-15,-38.51),(-12,-33.46),(-9,-27.73),(-6,-26.40),(-3,-22.88)],
    "CDL-D (376 ns)":  [(-15,-37.25),(-12,-31.99),(-9,-26.34),(-6,-24.70),(-3,-21.04)],
    "CDL-B (478 ns)":  [(-15,-36.78),(-12,-31.57),(-9,-26.32),(-6,-24.90),(-3,-20.44)],
    "CDL-C (865 ns)":  [(-15,-37.66),(-12,-32.52),(-9,-26.79),(-6,-24.48),(-3,-20.47)],
    "CDL-E (2064 ns)": [(-15,-38.15),(-12,-33.23),(-9,-27.54),(-6,-24.62),(-3,-20.70)],
}


def unpack(points):
    nd = [x[0] for x in points]
    passthru = [x[1] for x in points]
    true2d = [x[2] for x in points]
    gain = [p - t for p, t in zip(passthru, true2d)]  # positive = true2d better
    return nd, passthru, true2d, gain


def save_overview(script_dir):
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15.5, 6.4), constrained_layout=True)

    for label, pts in DATA.items():
        _, p, t, gain = unpack(pts)
        st = STYLE[label]
        ax1.plot(p, gain, label=label, markeredgecolor="white",
                 markeredgewidth=0.7, **st)
        ax2.plot(p, t, markeredgecolor="white", markeredgewidth=0.7, **st)

    ax1.set_xlabel(r"raw-LS NMSE [dB]   (left $\rightarrow$ noisier / lower SNR)")
    ax1.set_ylabel("true2d gain over raw LS [dB]   (higher = better)")
    ax1.set_title("(a) Estimation gain vs operating SNR\n(native CDL, static, SISO)")
    ax1.axhline(0, color="0.35", linewidth=1.0)
    ax1.set_ylim(-1.0, 13.5)
    ax1.legend(title="CDL profile (RMS delay)", loc="upper right")
    ax1.invert_xaxis()

    lim = [-40, 0]
    ax2.plot(lim, lim, color="0.4", linestyle=(0, (6, 4)), linewidth=1.6, label=r"$y=x$ (no gain)")
    ax2.set_xlim(lim)
    ax2.set_ylim([-40, -10])
    ax2.set_xlabel("raw-LS NMSE [dB]")
    ax2.set_ylabel("true2d NMSE [dB]   (lower = better)")
    ax2.set_title("(b) true2d vs raw-LS NMSE\n(below diagonal = true2d wins)")
    ax2.legend(loc="upper left")

    out = os.path.join(script_dir, "native_cdl_gain.png")
    fig.savefig(out, dpi=150)
    print(f"wrote {out}")
    plt.close(fig)


def save_small_multiples(script_dir):
    fig, axes = plt.subplots(2, 3, figsize=(15, 8.4), sharex=True, sharey=True,
                             constrained_layout=True)
    axes = axes.ravel()
    for ax, (label, pts) in zip(axes, DATA.items()):
        nd, p, t, gain = unpack(pts)
        st = STYLE[label]
        ax.plot(nd, gain, markeredgecolor="white", markeredgewidth=0.7, **st)
        ax.set_title(label)
        ax.axhline(0, color="black", linewidth=0.9, alpha=0.45)
        for x, y in zip(nd, gain):
            ax.annotate(f"{y:.1f}", (x, y), textcoords="offset points",
                        xytext=(0, 7), ha="center", fontsize=9)
    axes[-1].axis("off")
    for ax in axes[:5]:
        ax.set_xlabel(r"noise_power_dB ($-nd$), left $\rightarrow$ noisier")
        ax.set_ylabel("true2d gain [dB]")
        ax.set_ylim(-0.8, 13.5)
        ax.invert_xaxis()
    fig.suptitle("true2d gain over raw LS by CDL profile (per-model panels)",
                 fontsize=15, y=1.02)
    out = os.path.join(script_dir, "native_cdl_gain_small_multiples.png")
    fig.savefig(out, dpi=150)
    print(f"wrote {out}")
    plt.close(fig)


def save_nd_minus3_bars(script_dir):
    # STATIC cross-model comparison at -nd=-3 (== effective SNR ~11 dB, s0 sweep).
    # Three-way NMSE (LS/legacy/true2d) + gains over BOTH baselines.
    leg_at = {label: {p[0]: p[1] for p in pts} for label, pts in LEGACY.items()}
    labels, ls, legacy, true2d = [], [], [], []
    for label, pts in DATA.items():
        row = next(x for x in pts if x[0] == -3)
        labels.append(label.split()[0])
        ls.append(row[1])
        true2d.append(row[2])
        legacy.append(leg_at[label][-3])
    x = list(range(len(labels)))
    gain_ls = [l - t for l, t in zip(ls, true2d)]       # true2d over raw LS
    gain_leg = [l - t for l, t in zip(legacy, true2d)]   # true2d over legacy

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5.2), constrained_layout=True)

    # (a) three-way NMSE bars
    w = 0.27
    ax1.bar([i - w for i in x], ls, width=w, label="raw LS", color="#999999",
            edgecolor="black", linewidth=0.6)
    ax1.bar(x, legacy, width=w, label="legacy (FIR)", color="#0072B2",
            edgecolor="black", linewidth=0.6)
    ax1.bar([i + w for i in x], true2d, width=w, label="true2d (2D-MMSE)", color="#D55E00",
            edgecolor="black", linewidth=0.6)
    ax1.set_xticks(x, labels)
    ax1.set_ylabel("NMSE [dB]   (lower = better)")
    ax1.set_title("(a) Three-way NMSE per CDL profile")
    ax1.grid(axis="y", alpha=0.35, linestyle=":")
    ax1.legend(fontsize=10)

    # (b) gain over each baseline
    w2 = 0.38
    b1 = ax2.bar([i - w2/2 for i in x], gain_ls, width=w2, label="true2d $-$ raw LS",
                 color="#777777", edgecolor="black", linewidth=0.6)
    b2 = ax2.bar([i + w2/2 for i in x], gain_leg, width=w2, label="true2d $-$ legacy",
                 color="#D55E00", edgecolor="black", linewidth=0.6)
    ax2.set_xticks(x, labels)
    ax2.set_ylabel("true2d gain [dB]   (higher = better)")
    ax2.set_title("(b) true2d gain over each baseline")
    ax2.grid(axis="y", alpha=0.35, linestyle=":")
    ax2.bar_label(b1, fmt="%.1f", padding=2, fontsize=8.5)
    ax2.bar_label(b2, fmt="%.1f", padding=2, fontsize=8.5)
    ax2.legend(fontsize=10)

    fig.suptitle("Cross-model comparison at $\\approx$SNR 11 dB  (static, native CDL; $-nd=-3$)",
                 fontsize=14, y=1.05)

    out = os.path.join(script_dir, "native_cdl_nd_minus3_bars.png")
    fig.savefig(out, dpi=150)
    print(f"wrote {out}")
    plt.close(fig)


def save_threeway(script_dir):
    """raw-LS / legacy / true2d(no-gate) / true2d(gated) NMSE vs effective SNR
    (5-model mean), plus true2d gain over legacy with and without the high-SNR
    legacy-fallback gate. Shows the gate removing the high-SNR loss to legacy."""
    import statistics as st
    pt  = {label: {p[0]: p[1] for p in pts} for label, pts in DATA.items()}
    gat = {label: {p[0]: p[1] for p in pts} for label, pts in GATED.items()}
    rows = []  # (snr, raw, leg, t2, t2g)
    for nd in (-15, -12, -9, -6, -3):
        raws, legs, t2s, t2gs = [], [], [], []
        for label, pts in LEGACY.items():
            r = next((x for x in pts if x[0] == nd), None)
            if r is None or nd not in pt.get(label, {}) or nd not in gat.get(label, {}):
                continue
            raws.append(pt[label][nd]); legs.append(r[1]); t2s.append(r[2]); t2gs.append(gat[label][nd])
        if not raws:
            continue
        rows.append((-st.mean(raws), st.mean(raws), st.mean(legs), st.mean(t2s), st.mean(t2gs)))
    rows.sort(key=lambda x: x[0])
    snr    = [r[0] for r in rows]
    raw_a  = [r[1] for r in rows]; leg_a = [r[2] for r in rows]
    t2_a   = [r[3] for r in rows]; t2g_a = [r[4] for r in rows]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15.5, 6.4), constrained_layout=True)

    # (a) NMSE vs SNR: raw-LS, legacy, true2d(no gate), true2d(gated)
    ax1.plot(snr, raw_a, color="#777777", marker="o", linestyle="-",  label="raw LS (passthru)")
    ax1.plot(snr, leg_a, color="#0072B2", marker="s", linestyle="--", label="legacy (OAI FIR)")
    ax1.plot(snr, t2_a,  color="#D55E00", marker="^", linestyle=":",  alpha=0.65,
             label="true2d (no gate)")
    ax1.plot(snr, t2g_a, color="#D55E00", marker="v", linestyle="-",  linewidth=2.8,
             label="true2d (gated)")
    ax1.set_xlabel("effective SRS SNR [dB]   ($\\approx -$raw-LS NMSE)")
    ax1.set_ylabel("NMSE [dB]   (lower = better)")
    ax1.set_title("(a) NMSE vs SNR with high-SNR legacy gate\n(5-model mean)")
    ax1.legend(loc="lower left")

    # (b) true2d gain over legacy: no-gate (crosses zero -> loses) vs gated (>=0)
    gain_leg   = [l - t for l, t in zip(leg_a, t2_a)]    # +ve = true2d better
    gain_leg_g = [l - t for l, t in zip(leg_a, t2g_a)]
    ax2.axhline(0, color="0.35", linewidth=1.0)
    ax2.fill_between(snr, 0, gain_leg, where=[g < 0 for g in gain_leg],
                     color="#0072B2", alpha=0.12)
    ax2.plot(snr, gain_leg,   color="#0072B2", marker="^", linestyle=":",  alpha=0.7,
             label="true2d(no gate) $-$ legacy")
    ax2.plot(snr, gain_leg_g, color="#1B7F3A", marker="v", linestyle="-",  linewidth=2.8,
             label="true2d(gated) $-$ legacy")
    ax2.set_xlabel("effective SRS SNR [dB]")
    ax2.set_ylabel("true2d gain over legacy [dB]")
    ax2.set_title("(b) gate removes the high-SNR loss to legacy\n(gated curve stays $\\geq 0$)")
    ax2.text(0.03, 0.05,
             "no-gate dips below 0 at high SNR (legacy wins);\n"
             "gated stays $\\geq 0$ everywhere (true2d never loses)",
             transform=ax2.transAxes, ha="left", va="bottom", fontsize=9.0, color="0.2",
             bbox=dict(boxstyle="round,pad=0.35", facecolor="white", edgecolor="0.7", alpha=0.9))
    ax2.legend(loc="upper right")

    out = os.path.join(script_dir, "native_cdl_3way.png")
    fig.savefig(out, dpi=150)
    print(f"wrote {out}")
    plt.close(fig)


if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    save_overview(script_dir)
    save_small_multiples(script_dir)
    save_nd_minus3_bars(script_dir)
    save_threeway(script_dir)
