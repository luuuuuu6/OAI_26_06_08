#!/usr/bin/env python3
"""Key deliverable figure: three-way NMSE comparison (raw LS / OAI legacy FIR /
true2d 2D-MMSE), 5-model mean, native CDL.
  (a) STATIC  : NMSE vs effective SRS SNR.
  (b) DYNAMIC : NMSE vs UE speed at fixed ~SNR 17 dB.
Lower NMSE = better. Data: native CDL A-E sweeps (rep=3, STO-aligned coh gate,
per-launch 5GC restart)."""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams.update({
    "font.family": "DejaVu Serif", "mathtext.fontset": "dejavuserif",
    "font.size": 13, "axes.titlesize": 14, "axes.labelsize": 13,
    "legend.fontsize": 11.5, "axes.grid": True, "grid.alpha": 0.35,
    "grid.linestyle": ":", "lines.linewidth": 2.6, "lines.markersize": 9,
    "figure.dpi": 140, "savefig.bbox": "tight",
})

C_LS, C_LEG, C_T2 = "#777777", "#0072B2", "#D55E00"

# ---- STATIC: 5-model mean NMSE [dB] vs effective SNR (= -raw-LS NMSE) ----
# (from native_cdl static sweep, §6.5/§6.6)
SNR   = [34, 29, 23, 17, 11, 6]
S_LS  = [-34.30, -29.03, -23.24, -17.29, -11.30, -6.16]
S_LEG = [-37.67, -32.55, -26.81, -20.87, -14.89, -9.74]
S_T2  = [-35.02, -31.89, -28.54, -25.01, -21.11, -17.24]

# ---- DYNAMIC: 5-model mean NMSE [dB] vs speed at ~SNR 17 dB ----
# LS measured at 0/30/60 (per-symbol estimator -> speed-independent); shown flat
# (dotted) beyond 60 km/h. legacy & true2d measured 0-300 km/h.
SPD     = [0, 30, 60, 120, 300]
D_LEG   = [-20.87, -19.58, -19.06, -18.98, -18.77]
D_T2    = [-25.03, -21.50, -21.72, -21.33, -21.03]
D_LS_m  = [-17.29, -15.79, -15.53]            # measured at 0/30/60
D_LS_flat = sum(D_LS_m) / len(D_LS_m)          # ~ -16.2, speed-independent

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15.5, 6.6), constrained_layout=True)

# (a) static
ax1.plot(SNR, S_LS,  color=C_LS,  marker="o", linestyle="-",  label="raw LS (passthru)")
ax1.plot(SNR, S_LEG, color=C_LEG, marker="s", linestyle="--", label="legacy (OAI FIR)")
ax1.plot(SNR, S_T2,  color=C_T2,  marker="^", linestyle="-",  label="true2d (2D-MMSE)")
ax1.set_xlabel("effective SRS SNR [dB]")
ax1.set_ylabel("NMSE [dB]   (lower = better)")
ax1.set_title("(a) Static: NMSE vs SNR\n(5-model mean, native CDL)")
ax1.legend(loc="upper left")
ax1.invert_xaxis()  # noisier (low SNR) to the right

# (b) dynamic
# raw LS: measured at 0/30/60 (solid), then dotted flat extension to 300 km/h
# (LS is per-symbol -> speed-independent; not measured >60 here).
ax2.plot([0, 30, 60], D_LS_m, color=C_LS, marker="o", linestyle="-",
         label="raw LS (passthru)")
ax2.plot([60, 120, 300], [D_LS_m[-1]] * 3, color=C_LS, marker="o", markerfacecolor="white",
         linestyle=(0, (1, 1)))
ax2.plot(SPD, D_LEG, color=C_LEG, marker="s", linestyle="--", label="legacy (OAI FIR)")
ax2.plot(SPD, D_T2,  color=C_T2,  marker="^", linestyle="-",  label="true2d (2D-MMSE)")
ax2.set_xscale("symlog", linthresh=10)
ax2.set_xticks(SPD); ax2.set_xticklabels([str(s) for s in SPD])
ax2.set_xlim(-2, 360)
ax2.set_xlabel("UE speed [km/h]   ($\\approx$SNR 17 dB)")
ax2.set_ylabel("NMSE [dB]   (lower = better)")
ax2.set_title("(b) Dynamic: NMSE vs speed\n(5-model mean, $\\approx$SNR 17 dB)")
ax2.legend(loc="upper right")

fig.suptitle("Three-way SRS channel estimation: raw LS  vs  OAI legacy (FIR)  vs  true2d (2D-MMSE)  \u2014 native CDL, 5-model mean",
             fontsize=15, y=1.06)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native_cdl_threeway_static_dynamic.png")
fig.savefig(out, dpi=150)
print(f"wrote {out}")
