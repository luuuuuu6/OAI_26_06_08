#!/usr/bin/env python3
"""SRS SNR Calibration: regress measured [SRS Twin] SNR vs configured SNR_set
to recover the (G_FFT + G_proc) offset for OAI's SRS measurement path.

Model:
    SNR_meas = slope * SNR_set + intercept
where (under our hypothesis):
    slope     ≈ 1.0    if linear regime, no saturation
    intercept ≈ G_FFT_SRS + G_proc_SRS

For OAI default config (m_SRS=104 PRB, K_TC=2, 2 ports, FFT 2048):
    M_sc_b_SRS = 104 × 12 / 2 = 624 SC actually occupied by SRS
    G_FFT_SRS = 10·log10(2048/624) ≈ +5.16 dB

NOTE: OAI's nr_radio_config.c:930 hardcodes c_SRS via rrc_get_max_nr_csrs(),
ignoring the conf field "freq_hopping_c_srs". So even if you set 0 in conf,
the actual c_SRS is 25 (m_SRS=104). Don't rely on conf for SRS BW.

Therefore:
    G_proc_SRS = intercept - 5.16 dB

Usage:
    python3 srs_snr_calibration.py /path/to/q4_sweep_root/

Auto-discovers all snr_*dB subdirs under the sweep root, extracts SNR_set
from manifest + SNR_meas from [SRS Twin] log lines, runs np.polyfit.
"""
import sys
import os
import re
import glob
import numpy as np

# ── Constants for our setup ────────────────────────────────────────────
# OAI hardcodes c_SRS = rrc_get_max_nr_csrs(106, 0) = 25 → m_SRS = 104 PRB
# (nr_radio_config.c:930 ignores conf "freq_hopping_c_srs" value)
# K_TC hardcoded to 2 (transmissionComb_PR_n2 at nr_radio_config.c:964)
# → M_sc_b_SRS = 104 × 12 / 2 = 624 SC actually carrying SRS
M_SRS_PRB = 104
K_TC = 2
M_SC_B_SRS = M_SRS_PRB * 12 // K_TC          # 624 SC
FFT_SIZE = 2048
G_FFT_SRS_DB = 10 * np.log10(FFT_SIZE / M_SC_B_SRS)  # ≈ +5.16 dB
PIPELINE = "tx2rx2"


def extract_srs_meas_from_gnb_log(gnb_log_path):
    """Pull all [SRS Twin] log lines, return list of (captured, snr_db) tuples."""
    if not os.path.exists(gnb_log_path):
        return []
    pat = re.compile(r"\[SRS Twin\]\s+captured=(\d+)\s+written=\d+\s+dropped=\d+\s+SNR=(-?\d+)\s+dB")
    samples = []
    with open(gnb_log_path, "r", errors="ignore") as f:
        for line in f:
            m = pat.search(line)
            if m:
                samples.append((int(m.group(1)), int(m.group(2))))
    return samples


def extract_snr_set_from_subdir(subdir_name):
    """Parse 'snr_10dB' or 'snr_m5dB' → +10 / -5"""
    m = re.match(r"snr_(m?)(\d+)dB", subdir_name)
    if not m:
        return None
    sign = -1 if m.group(1) == "m" else 1
    return sign * int(m.group(2))


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 srs_snr_calibration.py <sweep_root_or_multi_sweep_root>")
        print("  python3 srs_snr_calibration.py <sweep_dir_1> <sweep_dir_2> ...")
        print("  python3 srs_snr_calibration.py --last-n 4")
        print()
        print("Modes:")
        print("  - One root containing snr_*dB/ subdirs")
        print("  - Parent dir containing multiple q4_sweep_*/ from a for-loop")
        print("  - Multiple explicit q4_sweep_*/ paths")
        print("  - --last-n N: auto-pick latest N q4_sweep_* under default logs/")
        sys.exit(1)

    snr_subdirs = []
    if sys.argv[1] == "--last-n":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 4
        default_root = "/home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/logs"
        all_sweeps = sorted(glob.glob(os.path.join(default_root, "q4_sweep_*")),
                            key=os.path.getmtime, reverse=True)[:n]
        for s in all_sweeps:
            snr_subdirs.extend(glob.glob(os.path.join(s, "snr_*dB")))
        snr_subdirs = sorted(snr_subdirs)
        print(f"--last-n {n} → picked: {[os.path.basename(s) for s in all_sweeps]}")
        print()
    elif len(sys.argv) >= 3 and all(os.path.isdir(a) for a in sys.argv[1:]):
        # Multiple explicit sweep roots
        for root in sys.argv[1:]:
            snr_subdirs.extend(glob.glob(os.path.join(root, "snr_*dB")))
        snr_subdirs = sorted(snr_subdirs)
    else:
        root = sys.argv[1]
        snr_subdirs = sorted(glob.glob(os.path.join(root, "snr_*dB")))
        if not snr_subdirs:
            snr_subdirs = sorted(glob.glob(os.path.join(root, "q4_sweep_*", "snr_*dB")))

    if not snr_subdirs:
        print(f"No snr_*dB subdirs found")
        sys.exit(1)

    print(f"Found {len(snr_subdirs)} SNR sweep points")
    print()
    print(f"  OAI SRS config: m_SRS={M_SRS_PRB} PRB, K_TC={K_TC} → M_sc_b_SRS={M_SC_B_SRS} SC")
    print(f"  G_FFT_SRS = 10·log10({FFT_SIZE}/{M_SC_B_SRS}) = {G_FFT_SRS_DB:+.2f} dB")
    print()

    # ── Extract data ──
    rows = []
    for d in snr_subdirs:
        snr_set = extract_snr_set_from_subdir(os.path.basename(d))
        if snr_set is None:
            continue
        gnb_log = os.path.join(d, "gnb.log")
        samples = extract_srs_meas_from_gnb_log(gnb_log)
        if not samples:
            print(f"  ⚠️  {os.path.basename(d):>10}: SNR_set={snr_set:+3d} → no [SRS Twin] log found")
            continue
        # Use the LAST [SRS Twin] line (highest captured count = most stable estimate)
        # Drop captured=0 (single-frame snapshot, can be noisy)
        valid = [(c, s) for c, s in samples if c > 0]
        if not valid:
            valid = samples  # fallback to captured=0 if nothing else
        captured, snr_meas = valid[-1]
        # Also collect all valid samples for this point (variance estimate)
        all_meas = [s for c, s in samples if c > 0] or [samples[-1][1]]
        rows.append(dict(
            sweep_dir=os.path.basename(d),
            snr_set=snr_set,
            snr_meas=snr_meas,
            captured=captured,
            n_samples=len(all_meas),
            mean_snr=float(np.mean(all_meas)),
            std_snr=float(np.std(all_meas)) if len(all_meas) > 1 else 0.0,
        ))
        print(f"  ✓ {os.path.basename(d):>10}: SNR_set={snr_set:+3d}  SNR_meas={snr_meas:+3d} dB  "
              f"(captured={captured}, n={len(all_meas)}, "
              f"mean={np.mean(all_meas):.1f}±{np.std(all_meas):.2f})")

    if len(rows) < 2:
        print(f"\nNeed ≥2 valid points for regression, got {len(rows)}")
        sys.exit(1)

    # ── Linear regression ──
    snr_set_arr = np.array([r["snr_set"] for r in rows], dtype=float)
    snr_meas_arr = np.array([r["snr_meas"] for r in rows], dtype=float)

    print(f"\n{'='*70}")
    print("Linear regression: SNR_meas = slope · SNR_set + intercept")
    print(f"{'='*70}\n")

    slope, intercept = np.polyfit(snr_set_arr, snr_meas_arr, 1)
    pred = slope * snr_set_arr + intercept
    residuals = snr_meas_arr - pred
    ss_res = np.sum(residuals ** 2)
    ss_tot = np.sum((snr_meas_arr - np.mean(snr_meas_arr)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    rmse = np.sqrt(np.mean(residuals ** 2))

    print(f"  slope     = {slope:.4f}   (expect 1.000 ± 0.05)")
    print(f"  intercept = {intercept:+.3f} dB")
    print(f"  R²        = {r2:.5f}")
    print(f"  RMSE      = {rmse:.3f} dB")
    print()

    # ── Diagnostic ──
    print(f"{'='*70}")
    print("Diagnostic (per reviewer's slope-check criteria):")
    print(f"{'='*70}\n")
    if 0.95 <= slope <= 1.05:
        print(f"  ✓ slope ∈ [0.95, 1.05]: linear regime, model clean")
        g_proc = intercept - G_FFT_SRS_DB
        print(f"  → G_proc_SRS = intercept - G_FFT_SRS = {intercept:+.2f} - {G_FFT_SRS_DB:+.2f} = {g_proc:+.2f} dB")
    elif slope < 0.9:
        print(f"  ⚠️  slope = {slope:.3f} < 0.9: possibly SRS estimator saturating at high SNR")
        print(f"     → Try dropping the highest SNR point and re-fit")
    elif slope > 1.1:
        print(f"  ⚠️  slope = {slope:.3f} > 1.1: low-SNR estimator failure (treating noise as signal)")
        print(f"     → Try dropping the lowest SNR point and re-fit")
    else:
        print(f"  ⚠️  slope = {slope:.3f} marginal — review individual residuals below")

    if r2 < 0.99 and len(rows) >= 4:
        print(f"  ⚠️  R² = {r2:.4f} < 0.99: per-point variance high, consider longer per-SNR runs")

    print()
    print(f"{'='*70}")
    print("Per-point residuals:")
    print(f"{'='*70}\n")
    print(f"  {'sweep':<25} {'SNR_set':>8} {'SNR_meas':>9} {'predicted':>10} {'residual':>9}")
    print(f"  {'-'*70}")
    for r, p, res in zip(rows, pred, residuals):
        print(f"  {r['sweep_dir']:<25} {r['snr_set']:>+7d}  "
              f"{r['snr_meas']:>+8d}  {p:>+9.2f}  {res:>+9.3f}")
    print()

    # ── Recommended next step ──
    print(f"{'='*70}")
    print("Recommended use of this calibration:")
    print(f"{'='*70}\n")
    print(f"  SNR_meas (predicted) = {slope:.3f} × SNR_set + {intercept:+.2f}  dB")
    print(f"  Inverse: SNR_set ≈ (SNR_meas - {intercept:+.2f}) / {slope:.3f}")
    print()
    print(f"  For NMSE-vs-SRS-SNR plotting, use SNR_meas (from [SRS Twin] log) on x-axis,")
    print(f"  not SNR_set, since that's what the receiver actually sees.")


if __name__ == "__main__":
    main()
