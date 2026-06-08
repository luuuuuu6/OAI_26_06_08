#!/usr/bin/env python3
"""Offline analyzer for V7_NOISE_DIAG_DIR dumps.

Quantifies the zero-padding bias in v7.py _gpu_compute_core noise injection
by triple-cross-checking three independent measurements per slot:

  truth      : real_sig = mean(|y_flat|^2) * pl^2          (pre-channel non-zero)
  bug formula: bug_sig  = mean(|gpu_out_pre_noise|^2)      (with zero padding)
  v7 actual  : sigma²_v7 (pulled from GPU as v7 actually used)
  observation: obs_total = mean(|gpu_out_post_noise|^2)    (post-noise full IQ)

Three sanity checks:
  1. SNR_bias = SNR_actual - SNR_set ≈ -active_dB           (bug magnitude)
  2. obs_total ≈ bug_sig + sigma²_v7                         (energy conservation)
  3. real_sig vs bug_sig ratio matches active_ratio (1/active_ratio = bias)

Usage:
  python3 analyze_noise_diag.py /tmp/oai_gpu_ipc/v7_noise_diag/
"""
import sys
import os
import glob
import numpy as np


def analyze_one(npz_path):
    z = np.load(npz_path, allow_pickle=False)
    y_flat = z["y_flat"]                          # (N_data, n_rx) complex
    pre_noise = z["gpu_out_pre_noise"]            # (total_cpx * n_rx,) complex, post-PL pre-noise
    post_noise = z["gpu_out_post_noise"]          # (total_cpx * n_rx,) complex, post-noise final
    sigma_sq_v7 = float(z["sigma_sq_v7"])         # variance v7 ACTUALLY used (per scalar IQ component)
    snr_db = float(z["snr_db"])
    pl_linear = float(z["pl_linear"])
    noise_on = bool(z["noise_on"])
    direction = str(z["direction"])
    n_tx = int(z["n_tx"])
    n_rx = int(z["n_rx"])
    total_cpx = int(z["total_cpx"])
    slot_idx = int(z["slot_idx"])

    pipeline = f"{direction}-tx{n_tx}rx{n_rx}"

    # 1. Truth: signal power per IQ sample on the active (non-zero) regions
    real_per_sample = float(np.mean(np.abs(y_flat) ** 2)) * (pl_linear ** 2)
    n_data_per_rx = y_flat.shape[0]               # samples per receive antenna
    active_ratio = n_data_per_rx / total_cpx      # structural non-zero fraction
    active_db = 10 * np.log10(active_ratio) if active_ratio > 0 else float("-inf")

    # 2. Bug formula: what v7 line 1144-1145 actually computes
    bug_sig_pwr = float(np.mean(np.abs(pre_noise) ** 2))

    # 3. Observed energy (post-noise) — should ≈ bug_sig + sigma²_v7 (complex variance)
    # NOTE: sigma²_v7 here is per scalar (R or I) component variance because
    # v7 uses _tmp_n_std = sqrt(sig_pwr / 2). The complex noise variance per
    # sample is 2 * sigma_sq_v7. So obs_total ≈ bug_sig + 2*sigma_sq_v7.
    obs_total = float(np.mean(np.abs(post_noise) ** 2))
    expected_total = bug_sig_pwr + 2.0 * sigma_sq_v7
    sanity_total = obs_total / expected_total if expected_total > 0 else float("nan")

    # 4. Implied actual SNR vs configured SNR
    # v7 sets sigma² (complex) = 2 * sigma_sq_v7 = bug_sig / 10^(SNR/10)
    # So real SNR = real_sig / (2*sigma_sq_v7) = (real / bug) * 10^(SNR/10)
    if noise_on and bug_sig_pwr > 0 and not np.isnan(sigma_sq_v7) and sigma_sq_v7 > 0:
        snr_v7_internal = bug_sig_pwr / (2.0 * sigma_sq_v7)        # should ≈ 10^(SNR_set/10)
        snr_actual_linear = real_per_sample / (2.0 * sigma_sq_v7)
        snr_actual_db = 10 * np.log10(snr_actual_linear)
        snr_bias_db = snr_actual_db - snr_db
        # Cross-check: SNR_set should match v7-internal (sanity for our reconstruction)
        snr_v7_internal_db = 10 * np.log10(snr_v7_internal)
        v7_consistency_db = snr_v7_internal_db - snr_db   # should ≈ 0
    else:
        snr_actual_db = float("nan")
        snr_bias_db = float("nan")
        v7_consistency_db = float("nan")

    return dict(
        slot_idx=slot_idx,
        pipeline=pipeline,
        snr_set=snr_db,
        pl=pl_linear,
        noise_on=noise_on,
        n_data_per_rx=n_data_per_rx,
        total_cpx=total_cpx,
        active_pct=active_ratio * 100,
        active_db=active_db,
        real_sig=real_per_sample,
        bug_sig=bug_sig_pwr,
        sigma_sq_v7=sigma_sq_v7,
        obs_total=obs_total,
        sanity_total=sanity_total,
        snr_actual_db=snr_actual_db,
        snr_bias_db=snr_bias_db,
        v7_consistency_db=v7_consistency_db,
    )


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 analyze_noise_diag.py <V7_NOISE_DIAG_DIR>")
        sys.exit(1)
    diag_dir = sys.argv[1]
    files = sorted(glob.glob(os.path.join(diag_dir, "slot_*.npz")))
    if not files:
        print(f"No slot_*.npz found in {diag_dir}")
        sys.exit(1)
    print(f"Found {len(files)} dump files in {diag_dir}\n")

    results = [analyze_one(f) for f in files]

    # ── Per-slot table ──
    print(f"{'='*150}")
    print(f"Per-slot detail (each row = 1 dumped slot):")
    print(f"{'='*150}\n")
    print(f"{'pipeline':>14} {'slot':>6} {'SNR_set':>8} {'PL':>5} "
          f"{'active%':>8} {'active_dB':>10} "
          f"{'real_sig':>11} {'bug_sig':>11} {'σ²_v7':>11} {'obs':>11} "
          f"{'SNR_act':>8} {'SNR_bias':>9} {'v7_cons':>8} {'sanity':>7}")
    print("-" * 150)
    for r in results:
        if np.isnan(r["snr_bias_db"]):
            bias_s = "      n/a"
            actual_s = "     n/a"
            cons_s = "    n/a "
        else:
            bias_s = f"{r['snr_bias_db']:+9.2f}"
            actual_s = f"{r['snr_actual_db']:+8.2f}"
            cons_s = f"{r['v7_consistency_db']:+8.3f}"
        sanity_s = f"{r['sanity_total']:7.3f}" if not np.isnan(r["sanity_total"]) else "    n/a"
        print(f"{r['pipeline']:>14} {r['slot_idx']:>6} "
              f"{r['snr_set']:>+8.1f} {r['pl']:>5.2f} "
              f"{r['active_pct']:>7.2f}% {r['active_db']:>+10.2f} "
              f"{r['real_sig']:>11.3e} {r['bug_sig']:>11.3e} {r['sigma_sq_v7']:>11.3e} {r['obs_total']:>11.3e} "
              f"{actual_s} {bias_s} {cons_s} {sanity_s}")
    print()

    # ── Per-pipeline aggregate ──
    print(f"{'='*150}")
    print(f"Per-pipeline summary (median over slots):")
    print(f"{'='*150}\n")
    pipes = sorted(set(r["pipeline"] for r in results))
    for pipe in pipes:
        sub = [r for r in results if r["pipeline"] == pipe]
        if not sub:
            continue
        bias_arr = np.array([r["snr_bias_db"] for r in sub if not np.isnan(r["snr_bias_db"])])
        active_arr = np.array([r["active_pct"] for r in sub])
        sanity_arr = np.array([r["sanity_total"] for r in sub if not np.isnan(r["sanity_total"])])
        cons_arr = np.array([r["v7_consistency_db"] for r in sub if not np.isnan(r["v7_consistency_db"])])

        print(f"  {pipe}:  n_slots={len(sub)}")
        print(f"    active_pct  : median={np.median(active_arr):6.2f}%  "
              f"min={np.min(active_arr):6.2f}%  max={np.max(active_arr):6.2f}%  "
              f"std={np.std(active_arr):6.2f}%")
        if len(bias_arr) > 0:
            print(f"    SNR_bias    : median={np.median(bias_arr):+6.2f} dB  "
                  f"min={np.min(bias_arr):+6.2f}  max={np.max(bias_arr):+6.2f}  "
                  f"std={np.std(bias_arr):.2f}")
        if len(sanity_arr) > 0:
            sanity_status = "✓" if 0.9 < np.median(sanity_arr) < 1.1 else "⚠️"
            print(f"    sanity_total: median={np.median(sanity_arr):.4f}  {sanity_status}  "
                  f"(should ≈ 1.000; >0.1 deviation = noise correlation / PL accounting issue)")
        if len(cons_arr) > 0:
            cons_med = np.median(np.abs(cons_arr))
            cons_status = "✓" if cons_med < 1.0 else "⚠️"
            print(f"    v7 consistency: median |{cons_med:.3f}| dB  {cons_status}  "
                  f"(typical 0.1-0.5 dB from IFFT/CP/filter residual is OK; >1.0 dB = pipeline-vs-dump mismatch)")
        print()

    # ── Active-ratio bucketing (THE key analysis per #2 reviewer feedback) ──
    print(f"{'='*150}")
    print(f"BUCKETED BY active_ratio (slot-type proxy: PRACH/Msg3/PUSCH/full-UL):")
    print(f"{'='*150}\n")
    print("  Theoretical curve: SNR_bias = -10·log10(active_ratio)")
    print("  Bucket boundaries (% active samples):")
    print("    PRACH-like   : 0-8%     (sparse: PRACH 1-2 OFDM symbols)")
    print("    Msg3-like    : 8-15%    (small PUSCH grant)")
    print("    PUSCH-small  : 15-25%   (5-10 PRB UL data)")
    print("    PUSCH-mid    : 25-40%   (medium UL)")
    print("    PUSCH-full   : 40-60%   (large UL)")
    print("    Continuous   : 60-100%  (DL data slot, near-fully-occupied)")
    print()

    BUCKETS = [(0, 8, "PRACH-like"), (8, 15, "Msg3-like"),
               (15, 25, "PUSCH-small"), (25, 40, "PUSCH-mid"),
               (40, 60, "PUSCH-full"), (60, 100, "Continuous")]
    for pipe in pipes:
        sub_pipe = [r for r in results if r["pipeline"] == pipe]
        print(f"  {pipe}:")
        print(f"    {'bucket':<14} {'n':>4} {'active%_med':>12} {'expected_bias':>15} "
              f"{'measured_bias':>15} {'diff':>9} {'std':>7}")
        print(f"    {'-'*80}")
        for lo, hi, name in BUCKETS:
            sub = [r for r in sub_pipe if lo <= r["active_pct"] < hi
                   and not np.isnan(r["snr_bias_db"])]
            if not sub:
                continue
            act_med = np.median([r["active_pct"] for r in sub])
            bias_arr = np.array([r["snr_bias_db"] for r in sub])
            bias_med = np.median(bias_arr)
            bias_std = np.std(bias_arr)
            expected = -10 * np.log10(act_med / 100)
            diff = bias_med - expected
            diff_marker = " " if abs(diff) < 1.0 else "⚠️"
            print(f"    {name:<14} {len(sub):>4} {act_med:>11.2f}% "
                  f"{expected:>+14.2f}dB {bias_med:>+14.2f}dB "
                  f"{diff:>+8.2f}{diff_marker} {bias_std:>7.2f}")
        print()

    # ── Theoretical fit: SNR_bias = -10·log10(active_ratio)? ──
    print(f"{'='*150}")
    print(f"Theoretical curve fit (per pipeline):  SNR_bias = -10·log10(active_ratio)")
    print(f"{'='*150}\n")
    for pipe in pipes:
        sub = [r for r in results if r["pipeline"] == pipe
               and not np.isnan(r["snr_bias_db"]) and r["active_pct"] > 0]
        if len(sub) < 2:
            continue
        active = np.array([r["active_pct"] / 100 for r in sub])
        bias_meas = np.array([r["snr_bias_db"] for r in sub])
        bias_pred = -10 * np.log10(active)
        residual = bias_meas - bias_pred
        ss_res = np.sum(residual ** 2)
        ss_tot = np.sum((bias_meas - np.mean(bias_meas)) ** 2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
        rmse = np.sqrt(np.mean(residual ** 2))
        fit_status = "✓ excellent (zero-padding model dominant)" if r2 > 0.95 else \
                     "✓ good (zero-padding accounts for >75%)" if r2 > 0.75 else \
                     "⚠️ poor (other sources of bias dominate)"
        print(f"  {pipe}: n={len(sub)}  R²={r2:.4f}  RMSE={rmse:.2f} dB  → {fit_status}")
    print()

    # ── Export scatter data for plotting ──
    csv_path = os.path.join(diag_dir, "scatter_data.csv")
    with open(csv_path, "w") as f:
        f.write("pipeline,slot_idx,active_ratio,active_pct,snr_set,snr_actual,snr_bias,"
                "v7_consistency,sanity_total,real_sig,bug_sig,sigma_sq_v7\n")
        for r in results:
            f.write(f"{r['pipeline']},{r['slot_idx']},{r['active_pct']/100:.6f},"
                    f"{r['active_pct']:.4f},{r['snr_set']:.2f},"
                    f"{r['snr_actual_db']:.4f},{r['snr_bias_db']:.4f},"
                    f"{r['v7_consistency_db']:.4f},{r['sanity_total']:.6f},"
                    f"{r['real_sig']:.6e},{r['bug_sig']:.6e},{r['sigma_sq_v7']:.6e}\n")
    print(f"Scatter CSV exported: {csv_path}")
    print(f"  → plot with: gnuplot/python; columns are active_ratio (x) and snr_bias (y)")
    print(f"  → theoretical curve: y = -10 * log10(x)")
    print()

    # ── Interpretation guide ──
    print("="*150)
    print("Interpretation:")
    print("="*150)
    print("  active_pct        = N_data_per_rx / total_cpx (structural non-zero ratio)")
    print("  active_dB         = 10·log10(active_ratio); equals -SNR_bias if bug is purely structural zero-padding")
    print("  real_sig          = mean(|y_flat|²) × PL²  (TRUE per-IQ-sample power)")
    print("  bug_sig           = mean(|gpu_out_pre_noise|²)  (what v7 line 1144-1145 averages over)")
    print("  σ²_v7             = noise variance per R/I component v7 actually injected")
    print("  obs_total         = mean(|gpu_out_post_noise|²)  (final observation)")
    print("  SNR_actual        = real_sig / (2·σ²_v7)  in dB  (link's actual SNR, may differ from setting)")
    print("  SNR_bias          = SNR_actual - SNR_set  (positive = link is QUIETER than configured)")
    print("  v7_consistency    = (bug_sig / (2·σ²_v7)) - SNR_set  (typical 0.1-0.5 dB from IFFT/CP residual)")
    print("  sanity_total      = obs_total / (bug_sig + 2·σ²_v7)  (should ≈ 1.0; deviation = noise correlation issues)")
    print()
    print("Critical signals to watch for in the bucket table & R² fit:")
    print("  - R² > 0.95             → zero-padding is the dominant bug; recommended fix (use _tmp_y_flat) will cleanly resolve it")
    print("  - 0.75 < R² < 0.95      → mostly zero-padding; check residuals at low active_pct (PRACH bucket)")
    print("  - R² < 0.75             → other bias sources matter; look at PRACH bucket diff for IFFT leakage")
    print("  - bucket diff > 1 dB    → that slot type has additional source of bias beyond zero-padding")
    print("  - v7_consistency > 1 dB → pipeline-vs-dump mismatch (should investigate before trusting other metrics)")


if __name__ == "__main__":
    main()
