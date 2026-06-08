#!/usr/bin/env python3
"""
Generate professor presentation figures:
  Fig 1: SRS Uplink Channel Estimation — Schematic Block Diagram
  Fig 2: NMSE vs SNR — Three-Algorithm Comparison (OAI Sweep)
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import numpy as np
import os

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figures")
os.makedirs(OUT_DIR, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════
#  Color palette
# ═══════════════════════════════════════════════════════════════════════
C_INPUT   = "#E3F2FD"   # light blue
C_PROC    = "#FFF3E0"   # light orange
C_AI      = "#E8F5E9"   # light green
C_OUTPUT  = "#F3E5F5"   # light purple
C_STORE   = "#FFEBEE"   # light red
C_BORDER  = "#37474F"
C_ARROW   = "#455A64"
C_TEXT    = "#212121"

C_EWMA   = "#1976D2"
C_IBVSS  = "#E64A19"
C_KALMAN = "#2E7D32"

# ═══════════════════════════════════════════════════════════════════════
#  Shared helpers
# ═══════════════════════════════════════════════════════════════════════

def _box(ax, x, y, w, h, title, bg, fs=9.5, bold=True, sub=None, sfs=7.5):
    """Rounded box with optional subtitle."""
    ax.add_patch(FancyBboxPatch(
        (x - w / 2, y - h / 2), w, h, boxstyle="round,pad=0.07",
        facecolor=bg, edgecolor=C_BORDER, linewidth=1.4, zorder=2))
    wt = 'bold' if bold else 'normal'
    ty = y + (0.12 if sub else 0)
    ax.text(x, ty, title, ha='center', va='center', fontsize=fs,
            fontweight=wt, color=C_TEXT, zorder=3)
    if sub:
        ax.text(x, y - 0.15, sub, ha='center', va='center',
                fontsize=sfs, color='#616161', style='italic', zorder=3)

def _arr(ax, x1, y1, x2, y2, label=None, lfs=7.5):
    """Straight arrow with optional label."""
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle="-|>", color=C_ARROW,
                                lw=1.6, mutation_scale=13), zorder=1)
    if label:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        off = 0.18 if abs(x2 - x1) > abs(y2 - y1) else 0
        ax.text(mx, my + off, label, ha='center', va='bottom',
                fontsize=lfs, color='#424242',
                bbox=dict(boxstyle='round,pad=0.12', fc='white',
                          ec='none', alpha=0.85))


def _elbow_arr(ax, points, label=None, lfs=7.5, color=C_ARROW, lw=1.6,
               label_pos=0.55, label_offset=(0, 0)):
    """Orthogonal multi-segment arrow with one arrowhead at the end."""
    xs, ys = zip(*points)
    ax.plot(xs[:-1], ys[:-1], color=color, lw=lw, zorder=1)
    ax.annotate("", xy=points[-1], xytext=points[-2],
                arrowprops=dict(arrowstyle="-|>", color=color, lw=lw,
                                mutation_scale=13), zorder=1)
    if label:
        seg_lengths = [
            ((points[i + 1][0] - points[i][0]) ** 2 +
             (points[i + 1][1] - points[i][1]) ** 2) ** 0.5
            for i in range(len(points) - 1)
        ]
        total = sum(seg_lengths) or 1
        target = total * label_pos
        acc = 0
        lx, ly = points[0]
        for i, seg_len in enumerate(seg_lengths):
            if acc + seg_len >= target:
                ratio = (target - acc) / (seg_len or 1)
                lx = points[i][0] + ratio * (points[i + 1][0] - points[i][0])
                ly = points[i][1] + ratio * (points[i + 1][1] - points[i][1])
                break
            acc += seg_len
        ax.text(lx + label_offset[0], ly + label_offset[1], label,
                ha='center', va='center', fontsize=lfs, color=color,
                fontweight='bold',
                bbox=dict(boxstyle='round,pad=0.15', fc='white',
                          ec='none', alpha=0.9), zorder=4)


# ═══════════════════════════════════════════════════════════════════════
#  Fig 1: UL Block Diagram  (clean 5-row linear layout)
# ═══════════════════════════════════════════════════════════════════════

def draw_block_diagram():
    fig, ax = plt.subplots(figsize=(24, 15))
    ax.set_xlim(0, 24)
    ax.set_ylim(-0.8, 15.2)
    ax.set_aspect('equal')
    ax.axis('off')

    # ── Title ──
    ax.text(12, 14.55,
            "SRS Uplink Channel Estimation and Compression Pipeline",
            ha='center', fontsize=23, fontweight='bold', color=C_TEXT)
    ax.text(12, 14.05,
            "From raw SRS observations to structured RAN digital-twin channel records",
            ha='center', fontsize=13.5, color='#757575')

    # ── Row 1: acquisition and LS estimate ──
    bw, bh = 4.3, 1.15
    y1 = 12.7
    _box(ax, 2.8, y1, bw, bh, "UE / Terminal", C_INPUT, fs=13,
         sub="transmits SRS", sfs=9.5)
    _box(ax, 9.4, y1, 4.6, bh, "RF Front-end / Proxy", C_INPUT, fs=13,
         sub="int16 ADC + FFT grid", sfs=9.5)
    _box(ax, 16.1, y1, 4.2, bh, "LS Channel Estimate", C_INPUT, fs=13,
         sub="H_LS[k,t] = Y[k,t] / X_ref[k]", sfs=9.5)
    _box(ax, 21.4, y1, 3.2, bh, "Frame Buffer", C_INPUT, fs=12,
         sub="time series H_LS", sfs=9)
    _arr(ax, 4.95, y1, 7.1, y1, "SRS waveform", lfs=9.5)
    _arr(ax, 11.7, y1, 14.0, y1, "frequency grid Y[k]", lfs=9.5)
    _arr(ax, 18.2, y1, 19.8, y1, "H_LS", lfs=9.5)

    # ── Row 2: phase normalization ──
    y2 = 10.55
    _box(ax, 12.0, y2, 5.3, 1.15, "Phase De-rotation", C_PROC, fs=13,
         sub="remove common phase using previous H_smooth", sfs=9)
    _elbow_arr(ax, [(21.4, y1 - bh / 2), (21.4, y2), (14.65, y2)],
               "H_LS[k,t]", lfs=9.5, label_pos=0.33, label_offset=(0.25, 0.2))

    ax.text(5.0, y2, "Goal: stabilize temporal filtering before extracting\n"
                     "slow channel structure and compact taps.",
            ha='left', va='center', fontsize=10.5, color='#546E7A',
            bbox=dict(boxstyle='round,pad=0.25', fc='#FAFAFA',
                      ec='#CFD8DC', alpha=0.95))

    # ── Row 3: adaptive filtering ──
    y3 = 7.7
    ow, oh = 21.2, 2.65
    ax.add_patch(FancyBboxPatch(
        (12.0 - ow / 2, y3 - oh / 2), ow, oh, boxstyle="round,pad=0.15",
        facecolor='#FAFAFA', edgecolor=C_BORDER, linewidth=2.0,
        linestyle='--', zorder=0))
    ax.text(12.0, y3 + oh / 2 + 0.28,
            "Adaptive Time-Domain Filtering  (runtime selectable by SRS_2D_METHOD)",
            ha='center', fontsize=13.5, fontweight='bold', color=C_TEXT)

    sw, sh = 3.8, 1.15
    _box(ax, 3.5, y3, sw, sh, "Even-Odd R_est", '#FFF3E0', fs=11.5,
         sub="noise floor + c_model", sfs=8.5)
    _box(ax, 8.2, y3, sw, sh, "EWMA", C_PROC, fs=12.5,
         sub="fixed alpha baseline", sfs=8.5)
    _box(ax, 12.8, y3, sw, sh, "IBVSS", C_AI, fs=12.5,
         sub="alpha = 1 - 1 / ratio", sfs=8.5)
    _box(ax, 17.5, y3, sw, sh, "Kalman", C_AI, fs=12.5,
         sub="Riccati gain update", sfs=8.5)
    _box(ax, 21.2, y3, 2.2, sh, "Selector", '#ECEFF1', fs=11,
         sub="best method", sfs=8)

    _arr(ax, 5.4, y3, 6.3, y3, "R_est", lfs=8.5)
    _arr(ax, 10.1, y3, 10.9, y3)
    _arr(ax, 14.7, y3, 15.6, y3)
    _arr(ax, 19.4, y3, 20.1, y3)

    _elbow_arr(ax, [(12.0, y2 - 0.58), (12.0, y3 + oh / 2)],
               "H_derot", lfs=9.5, label_pos=0.25,
               label_offset=(0.75, 0.25))
    ax.text(12.8, y3 - 1.05,
            "Innovation P4: per-band median metric (B = 8) feeds IBVSS confidence",
            ha='center', fontsize=10.2, color='#616161', style='italic')

    # ── Row 4: structured outputs ──
    y4 = 4.35
    _box(ax, 6.4, y4, 6.0, 1.35, "Structure Extraction", C_OUTPUT, fs=13.5,
         sub="3 EMA states: p, R, d + quality metrics", sfs=9.5)
    _box(ax, 17.0, y4, 6.0, 1.35, "DFT Compression", C_OUTPUT, fs=13.5,
         sub="keep dominant taps + reconstruct full band", sfs=9.5)

    _elbow_arr(ax, [(21.2, y3 - sh / 2), (21.2, 5.55), (12.0, 5.55),
                    (12.0, y4 + 0.85)],
               "H_smooth", lfs=9.5, label_pos=0.45, label_offset=(0, 0.18))
    _arr(ax, 12.0, y4 + 0.85, 6.4, y4 + 0.68)
    _arr(ax, 12.0, y4 + 0.85, 17.0, y4 + 0.68)

    s_text = ("S = { p_state, R_rx_state, d_state,\n"
              "      confidence, rsrp, snr,\n"
              "      doppler, sv, ds_rms }")
    ax.text(6.4, y4 - 0.95, s_text, ha='center', va='top', fontsize=10.2,
            family='monospace',
            bbox=dict(boxstyle='round,pad=0.25', fc='#F3E5F5',
                      ec='#CE93D8', alpha=0.9))
    h_text = ("h_taps : compressed payload (2·N_tap)\n"
              "H_c    : full-band recon (N_sc)")
    ax.text(17.0, y4 - 0.95, h_text, ha='center', va='top', fontsize=10.2,
            family='monospace',
            bbox=dict(boxstyle='round,pad=0.25', fc='#F3E5F5',
                      ec='#CE93D8', alpha=0.9))

    # ── Row 5: storage ──
    y5 = 0.8
    _box(ax, 12.0, y5, 11.2, 1.25,
         "Database / RAN Digital Twin  (Minji)", C_STORE, fs=13.5,
         sub="append time-indexed channel records: (S, h_taps, H_c) x t",
         sfs=9.5)
    _elbow_arr(ax, [(6.4, y4 - 1.8), (6.4, 2.0), (9.2, 2.0), (9.2, y5 + 0.62)],
               "S", lfs=10.5, color='#7B1FA2', label_pos=0.42)
    _elbow_arr(ax, [(17.0, y4 - 1.45), (17.0, 2.0), (14.8, 2.0),
                    (14.8, y5 + 0.62)],
               "h_taps + H_c", lfs=10.5, color='#7B1FA2', label_pos=0.45)

    # DL reference
    ax.text(12.0, -0.1,
            "Interface alignment with DL: decoded channel structure S + channel representation H",
            ha='center', fontsize=11.5, color='#1565C0', fontweight='bold',
            bbox=dict(boxstyle='round,pad=0.25', fc='#E3F2FD',
                      ec='#90CAF9'))

    plt.tight_layout()
    path = os.path.join(OUT_DIR, "fig1_block_diagram.png")
    fig.savefig(path, dpi=220, bbox_inches='tight',
                facecolor='white', edgecolor='none')
    print(f"[OK] Saved: {path}")
    plt.close(fig)
    return path


# ═══════════════════════════════════════════════════════════════════════
#  Fig 2: NMSE vs SNR — Three-Algorithm Comparison
# ═══════════════════════════════════════════════════════════════════════

def draw_nmse_chart():
    snr = np.array([5, 10, 15, 20])

    data = {
        'EWMA': {
            'mean': [-11.04, -10.30, -11.33, -10.83],
            'p50':  [-9.33,   -0.62,  -9.27,  -8.88],
            'p10':  [-12.50, -12.61, -13.26, -12.07],
            'p90':  [+25.87, +25.40,  -1.72,  +1.65],
            'N':    [99, 102, 103, 49],
        },
        'IBVSS': {
            'mean': [-10.10, -10.27, -10.03, -10.54],
            'p50':  [-10.46, -10.06, -10.87, -11.00],
            'p10':  [-12.28, -12.63, -13.73, -12.85],
            'p90':  [-7.77,   -8.55,  -6.41, +21.55],
            'N':    [67, 66, 106, 75],
        },
        'Kalman': {
            'mean': [-8.78,  -10.16, -10.35, -10.31],
            'p50':  [+21.52, -11.19,  -9.92, -11.45],
            'p10':  [-11.62, -14.08, -12.86, -12.88],
            'p90':  [+27.00,  -7.00, +25.25,  -8.58],
            'N':    [87, 100, 59, 66],
        },
    }

    colors = {'EWMA': C_EWMA, 'IBVSS': C_IBVSS, 'Kalman': C_KALMAN}
    markers = {'EWMA': 's', 'IBVSS': 'o', 'Kalman': '^'}

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("OAI Real-System NMSE vs SNR — Three-Algorithm Comparison\n"
                 "(RF Simulator Channel, CHANNEL_SEED=42, 2×2 MIMO, 1248 active SC)",
                 fontsize=13, fontweight='bold', y=0.98)

    metrics = [
        ('p50', 'NMSE p50 (Median) — Primary Metric',
         'Robust central tendency; lower is better'),
        ('p10', 'NMSE p10 (Best 10%) — Best-Case Performance',
         'Performance ceiling; lower is better'),
        ('p90', 'NMSE p90 (Worst 10%) — Robustness Metric',
         'Worst-case frames; lower is better. Outliers marked ×'),
        ('mean', 'NMSE Mean — Overall Average',
         'Sensitive to outliers; shown for completeness'),
    ]

    OUTLIER_THRESH = 15.0  # dB above which we consider it an outlier

    for idx, (metric, title, subtitle) in enumerate(metrics):
        ax = axes[idx // 2][idx % 2]
        ax.set_title(title, fontsize=10, fontweight='bold', pad=8)
        ax.text(0.5, 1.01, subtitle, transform=ax.transAxes,
                ha='center', va='bottom', fontsize=7.5, color='#757575')

        for name in ['EWMA', 'IBVSS', 'Kalman']:
            vals = np.array(data[name][metric])
            c = colors[name]
            m = markers[name]

            good_mask = vals < OUTLIER_THRESH
            bad_mask = ~good_mask

            # Plot good points with lines
            plot_vals = vals.copy()
            plot_vals[bad_mask] = np.nan
            ax.plot(snr, plot_vals, color=c, marker=m, markersize=7,
                    linewidth=2, label=name, zorder=3)

            # Mark outliers with × at a capped position
            if np.any(bad_mask):
                for si, v in zip(snr[bad_mask], vals[bad_mask]):
                    cap_y = min(v, 12)
                    ax.plot(si, cap_y, marker='x', color=c,
                            markersize=10, markeredgewidth=2.5, zorder=4)
                    ax.annotate(f"+{v:.0f} dB",
                                xy=(si, cap_y), xytext=(si + 0.8, cap_y + 1.5),
                                fontsize=7, color=c, fontweight='bold',
                                arrowprops=dict(arrowstyle='->', color=c,
                                                lw=0.8),
                                zorder=4)

        ax.set_xlabel("SNR (dB)", fontsize=9)
        ax.set_ylabel("NMSE (dB)", fontsize=9)
        ax.set_xticks(snr)
        ax.grid(True, alpha=0.3, linestyle='--')
        ax.legend(fontsize=8, loc='best')

        if metric == 'p50':
            ax.set_ylim(-15, 5)
            ax.axhline(-10, color='gray', linestyle=':', alpha=0.5)
            ax.text(5.3, -9.5, "−10 dB reference", fontsize=7,
                    color='gray')
        elif metric == 'p10':
            ax.set_ylim(-16, -8)
        elif metric == 'p90':
            ax.set_ylim(-16, 15)
            ax.axhline(0, color='red', linestyle=':', alpha=0.4)
            ax.text(5.3, 0.5, "0 dB (no filtering benefit)", fontsize=7,
                    color='red', alpha=0.7)
        elif metric == 'mean':
            ax.set_ylim(-14, 5)

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    path = os.path.join(OUT_DIR, "fig2_nmse_vs_snr.png")
    fig.savefig(path, dpi=200, bbox_inches='tight',
                facecolor='white', edgecolor='none')
    print(f"[OK] Saved: {path}")
    plt.close(fig)

    # ── Fig 2b: Clean p50-only comparison (for slides) ──
    fig2, ax2 = plt.subplots(1, 1, figsize=(9, 5.5))
    ax2.set_title("NMSE p50 (Median) vs SNR — OAI Real-System Comparison",
                   fontsize=13, fontweight='bold', pad=12)

    for name in ['EWMA', 'IBVSS', 'Kalman']:
        vals = np.array(data[name]['p50'])
        c = colors[name]
        m = markers[name]

        good = vals < OUTLIER_THRESH
        plot_v = vals.copy()
        plot_v[~good] = np.nan

        n_arr = data[name]['N']
        ax2.plot(snr, plot_v, color=c, marker=m, markersize=9,
                 linewidth=2.5, label=name, zorder=3)

        for si, vi, ni, g in zip(snr, vals, n_arr, good):
            if g:
                ax2.annotate(f"{vi:.1f} dB\n(N={ni})",
                             xy=(si, vi), xytext=(0, -18),
                             textcoords='offset points',
                             ha='center', fontsize=7, color=c)
            else:
                cap_y = 5
                ax2.plot(si, cap_y, marker='x', color=c,
                         markersize=12, markeredgewidth=3, zorder=4)
                ax2.annotate(f"OUTLIER\n+{vi:.0f} dB (N={ni})\nwarmup issue",
                             xy=(si, cap_y),
                             xytext=(si + 1.5, cap_y + 2),
                             fontsize=7, color=c, fontweight='bold',
                             arrowprops=dict(arrowstyle='->', color=c, lw=1),
                             zorder=4)

    ax2.set_xlabel("SNR (dB)", fontsize=11)
    ax2.set_ylabel("NMSE (dB)", fontsize=11)
    ax2.set_xticks(snr)
    ax2.set_ylim(-14, 8)
    ax2.grid(True, alpha=0.3, linestyle='--')
    ax2.axhline(-10, color='gray', linestyle=':', alpha=0.5)
    ax2.text(19.5, -9.5, "−10 dB ref", fontsize=8, color='gray', ha='right')
    ax2.legend(fontsize=10, loc='upper right',
               framealpha=0.9, edgecolor='gray')

    # Conclusion annotation
    ax2.text(0.02, 0.02,
             "Conclusion: IBVSS p50 ∈ [−10.0, −11.0] at all SNR (most robust)\n"
             "Kalman best at SNR=20 (−11.45 dB) but catastrophic at SNR=5 (warmup)",
             transform=ax2.transAxes, fontsize=8, va='bottom',
             bbox=dict(boxstyle='round,pad=0.3', fc='#FFFDE7',
                       ec='#FBC02D', alpha=0.9))

    plt.tight_layout()
    path2 = os.path.join(OUT_DIR, "fig2b_nmse_p50_clean.png")
    fig2.savefig(path2, dpi=200, bbox_inches='tight',
                 facecolor='white', edgecolor='none')
    print(f"[OK] Saved: {path2}")
    plt.close(fig2)

    # ── Fig 2c: p90 robustness chart ──
    fig3, ax3 = plt.subplots(1, 1, figsize=(9, 5.5))
    ax3.set_title("NMSE p90 (Worst 10% Frames) vs SNR — Robustness Comparison",
                   fontsize=13, fontweight='bold', pad=12)

    for name in ['EWMA', 'IBVSS', 'Kalman']:
        vals = np.array(data[name]['p90'])
        c = colors[name]
        m = markers[name]

        good = vals < OUTLIER_THRESH
        plot_v = vals.copy()
        plot_v[~good] = np.nan
        ax3.plot(snr, plot_v, color=c, marker=m, markersize=9,
                 linewidth=2.5, label=name, zorder=3)

        for si, vi, g in zip(snr, vals, good):
            if g:
                ax3.annotate(f"{vi:.1f}",
                             xy=(si, vi), xytext=(0, 10),
                             textcoords='offset points',
                             ha='center', fontsize=7.5, color=c,
                             fontweight='bold')
            else:
                cap_y = 12
                ax3.plot(si, cap_y, marker='x', color=c,
                         markersize=12, markeredgewidth=3, zorder=4)
                ax3.annotate(f"+{vi:.0f}",
                             xy=(si, cap_y), xytext=(si + 0.6, cap_y + 1),
                             fontsize=7.5, color=c, fontweight='bold',
                             arrowprops=dict(arrowstyle='->', color=c,
                                             lw=0.8))

    ax3.set_xlabel("SNR (dB)", fontsize=11)
    ax3.set_ylabel("NMSE p90 (dB)", fontsize=11)
    ax3.set_xticks(snr)
    ax3.set_ylim(-16, 15)
    ax3.grid(True, alpha=0.3, linestyle='--')
    ax3.axhline(0, color='red', linestyle=':', alpha=0.5)
    ax3.text(19.5, 0.5, "0 dB (no benefit)", fontsize=8, color='red',
             ha='right', alpha=0.7)

    # IBVSS highlight region
    ibvss_p90 = np.array(data['IBVSS']['p90'])
    good_ib = ibvss_p90 < OUTLIER_THRESH
    if np.sum(good_ib) >= 2:
        ax3.fill_between(snr[good_ib],
                         ibvss_p90[good_ib] - 0.8,
                         ibvss_p90[good_ib] + 0.8,
                         color=C_IBVSS, alpha=0.1, zorder=1)

    ax3.legend(fontsize=10, loc='upper right',
               framealpha=0.9, edgecolor='gray')

    ax3.text(0.02, 0.02,
             "IBVSS: p90 ∈ [−6, −9] at all SNR except one outlier\n"
             "→ Adaptive α suppresses worst-case frames effectively",
             transform=ax3.transAxes, fontsize=8, va='bottom',
             bbox=dict(boxstyle='round,pad=0.3', fc='#E8F5E9',
                       ec='#81C784', alpha=0.9))

    plt.tight_layout()
    path3 = os.path.join(OUT_DIR, "fig2c_nmse_p90_robustness.png")
    fig3.savefig(path3, dpi=200, bbox_inches='tight',
                 facecolor='white', edgecolor='none')
    print(f"[OK] Saved: {path3}")
    plt.close(fig3)

    return path, path2, path3


# ═══════════════════════════════════════════════════════════════════════
#  Fig 3: Unified DL / UL Architecture for Digital Twin
# ═══════════════════════════════════════════════════════════════════════

C_DL = "#E3F2FD"
C_UL = "#E8F5E9"
C_DB = "#FFEBEE"
C_SHARED = "#FFFDE7"


def draw_unified_ul_dl_diagram():
    fig, ax = plt.subplots(figsize=(18, 12))
    ax.set_xlim(0, 18)
    ax.set_ylim(-0.4, 12.0)
    ax.set_aspect('equal')
    ax.axis('off')

    # ── Title ──
    ax.text(9, 11.4,
            "Digital Twin Channel Data Pipeline: Unified DL / UL Architecture",
            ha='center', fontsize=18, fontweight='bold', color=C_TEXT)
    ax.text(9, 10.95,
            "Different estimators, same database contract: channel structure S + channel representation H",
            ha='center', fontsize=10.8, color='#757575')

    # ═══════════════════════════════════════════════════
    #  DL branch   (y ≈ 7.5 – 10)
    # ═══════════════════════════════════════════════════
    ax.add_patch(FancyBboxPatch(
        (0.45, 7.05), 17.1, 3.35, boxstyle="round,pad=0.15",
        facecolor='#F5F9FF', edgecolor='#1565C0', linewidth=1.8,
        linestyle='--', zorder=0))
    ax.text(0.95, 10.05,
            "Downlink CSI Feedback  |  Junsu: CSE-CsiNet v3.3",
            fontsize=12.2, fontweight='bold', color='#1565C0')

    yd = 8.65
    xs = [2.0, 5.0, 8.0, 11.0, 14.0]
    labels = ["CSI-RS + UE", "ColdAE + FiLM", "Quantize + Air",
              "gNB Decode", "DL Output"]
    subs   = ["pilot observes H_t", "cold + structure + instance",
              "z_code feedback", "SharedStateCell\nGRU + 3 EMA",
              "S_dl + H_dl"]
    for x, lb, sb in zip(xs, labels, subs):
        bg = C_SHARED if lb == "DL Output" else C_DL
        _box(ax, x, yd, 2.35, 0.9, lb, bg, fs=9.2, sub=sb, sfs=7.2)
    for i in range(4):
        _arr(ax, xs[i] + 1.2, yd, xs[i + 1] - 1.2, yd)

    dl_detail = ("S_dl = {p_state, R_tx_state,\n"
                 "        d_state, c_t}\n"
                 "H_dl = Ĥ_cold + c_t·(Ĥ_str+Ĥ_inst)")
    ax.text(14.0, yd - 0.82, dl_detail, ha='center', va='top',
            fontsize=7.5, family='monospace',
            bbox=dict(boxstyle='round,pad=0.15', fc=C_DL, ec='#90CAF9'))

    ax.text(16.4, yd, "Neural codec path",
            ha='center', va='center', fontsize=8.5, color='#1565C0',
            rotation=90, fontweight='bold')

    # ═══════════════════════════════════════════════════
    #  Consistent format banner  (y ≈ 6.2)
    # ═══════════════════════════════════════════════════
    yb = 6.35
    ax.plot([1.6, 16.4], [yb, yb], color='#FBC02D', lw=2.5, ls='--', zorder=1)
    ax.text(9, yb + 0.18,
            "consistent output contract:  (S, H)  for every timestamp",
            ha='center', fontsize=10.6, fontweight='bold', color='#F57F17',
            bbox=dict(boxstyle='round,pad=0.15', fc=C_SHARED, ec='#FBC02D'))

    shared_fields = (
        "Shared schema: p_state  |  R_tx/R_rx_state  |  d_state  |  confidence / quality  |  H representation")
    ax.text(9, yb - 0.32, shared_fields, ha='center', fontsize=8.3,
            color='#795548')

    # ═══════════════════════════════════════════════════
    #  UL branch   (y ≈ 2.5 – 5.8)
    # ═══════════════════════════════════════════════════
    ax.add_patch(FancyBboxPatch(
        (0.45, 2.15), 17.1, 3.55, boxstyle="round,pad=0.15",
        facecolor='#F5FFF5', edgecolor='#2E7D32', linewidth=1.8,
        linestyle='--', zorder=0))
    ax.text(0.95, 5.35,
            "Uplink SRS Estimation  |  Liu: IBVSS / Kalman",
            fontsize=12.2, fontweight='bold', color='#2E7D32')

    yu = 4.1
    xu = [2.0, 5.0, 8.0, 11.0, 14.0]
    ulabels = ["SRS + LS Est.", "De-rotation", "IBVSS / Kalman",
               "Structure + DFT", "UL Output"]
    usubs   = ["int16 grid -> H_LS", "phase stabilized",
               "adaptive alpha\nper-band median",
               "EMA states + taps\nconfidence metrics",
               "S_ul + H_ul"]
    for x, lb, sb in zip(xu, ulabels, usubs):
        bg = C_SHARED if lb == "UL Output" else C_UL
        _box(ax, x, yu, 2.35, 0.9, lb, bg, fs=9.2, sub=sb, sfs=7.2)
    for i in range(4):
        _arr(ax, xu[i] + 1.2, yu, xu[i + 1] - 1.2, yu)

    # sub-blocks (small, below IBVSS)
    _box(ax, 7.0, 2.75, 1.8, 0.52, "R_est", '#FFF3E0', fs=7.5,
         bold=False, sub="even-odd", sfs=6.5)
    _box(ax, 9.0, 2.75, 1.8, 0.52, "Innov P4", '#FFF3E0', fs=7.5,
         bold=False, sub="per-band med.", sfs=6.5)
    ax.annotate("", xy=(7.0, 3.0), xytext=(7.0, yu - 0.45),
                arrowprops=dict(arrowstyle="<|-", color='#888', lw=0.9,
                                mutation_scale=9))
    ax.annotate("", xy=(9.0, 3.0), xytext=(9.0, yu - 0.45),
                arrowprops=dict(arrowstyle="<|-", color='#888', lw=0.9,
                                mutation_scale=9))

    ul_detail = ("S_ul = {p_state, R_rx_state,\n"
                 "        d_state, confidence,\n"
                 "        rsrp, snr, sv, …}\n"
                 "H_ul = h_taps (compressed)\n"
                 "     + H_c   (full-band recon)")
    ax.text(14.0, yu - 0.82, ul_detail, ha='center', va='top',
            fontsize=7.5, family='monospace',
            bbox=dict(boxstyle='round,pad=0.15', fc=C_UL, ec='#A5D6A7'))

    ax.text(16.4, yu, "Signal-processing path",
            ha='center', va='center', fontsize=8.5, color='#2E7D32',
            rotation=90, fontweight='bold')

    # ═══════════════════════════════════════════════════
    #  Database   (y ≈ 0.5)
    # ═══════════════════════════════════════════════════
    ydb = 0.75
    _box(ax, 9, ydb, 9.2, 0.95,
         "Database / RAN Digital Twin  (Minji)", C_DB, fs=12,
         sub="time-indexed records: (S_dl, H_dl) + (S_ul, H_ul)  ->  twin applications",
         sfs=8)

    # arrows from DL/UL outputs down to DB
    _elbow_arr(ax, [(14.0, yd - 1.35), (14.0, 6.85), (16.15, 6.85),
                    (16.15, 1.55), (13.6, 1.55), (13.6, ydb + 0.48)],
               "S_dl, H_dl", lfs=8.5, color='#1565C0', label_pos=0.23,
               label_offset=(0.15, 0.15))
    _elbow_arr(ax, [(14.0, yu - 1.5), (14.0, 1.72), (13.25, 1.72),
                    (13.25, ydb + 0.48)],
               "S_ul, H_ul", lfs=8.5, color='#2E7D32', label_pos=0.38,
               label_offset=(0.1, 0.15))

    # method note
    note = ("Method differs, format aligned:\n"
            "  DL: Neural codec (CsiNet + FiLM + GRU)\n"
            "  UL: Signal processing (IBVSS / Kalman)\n"
            "  Both → same (S, H) schema for DB")
    ax.text(0.95, 1.18, note, fontsize=7.6, va='top', family='monospace',
            bbox=dict(boxstyle='round,pad=0.2', fc='white',
                      ec='#BDBDBD', alpha=0.95))

    plt.tight_layout()
    path = os.path.join(OUT_DIR, "fig_unified_ul_dl_architecture.png")
    fig.savefig(path, dpi=220, bbox_inches='tight',
                facecolor='white', edgecolor='none')
    print(f"[OK] Saved: {path}")
    plt.close(fig)
    return path


# ═══════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("  Generating Professor Presentation Figures")
    print("=" * 60)

    p1 = draw_block_diagram()
    p2, p2b, p2c = draw_nmse_chart()
    p3 = draw_unified_ul_dl_diagram()

    print("\n" + "=" * 60)
    print("  All figures saved to:", OUT_DIR)
    print("=" * 60)
    print(f"\n  1. Block Diagram (UL detail):         {p1}")
    print(f"  2. NMSE 4-panel:                      {p2}")
    print(f"  3. NMSE p50 clean:                    {p2b}")
    print(f"  4. NMSE p90 robustness:               {p2c}")
    print(f"  5. Unified DL/UL architecture:        {p3}")
