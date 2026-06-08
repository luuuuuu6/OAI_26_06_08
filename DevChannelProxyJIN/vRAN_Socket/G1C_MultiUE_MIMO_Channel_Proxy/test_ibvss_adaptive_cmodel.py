"""
Test IBVSS with adaptive c_model vs fixed c_model=0 under high freq selectivity.

Verifies that adaptive_cmodel=True automatically compensates for
channel variation between adjacent subcarriers in P1B-like conditions.

Usage:
  python3 test_ibvss_adaptive_cmodel.py
"""
import numpy as np
import sys
from gen_cdl_rays_standalone import generate_cdl_c_rays
from multisc_cdl_sim import (
    generate_cdl_channel, add_awgn, oracle_sweep, run_fixed_alpha_ema,
    N_SC, BURN_IN
)
from srs_2d_mmse import IBVSS_EMA

EPSILON = 0.14
N_FRAMES = 2000
SPEED_KMH = 3
SNR_DB = 10
N_TRIALS = 5
DELAY_SPREADS_NS = [100, 300, 500, 1000, 1500]


def run_ibvss_variant(H_true, H_obs, adaptive_cmodel, c_model=0.0):
    n_frames, n_sc = H_true.shape
    filt = IBVSS_EMA(n_sc=n_sc, c_model=c_model,
                     adaptive_cmodel=adaptive_cmodel)
    mse_list = []
    for n in range(n_frames):
        H_est = filt.update(H_obs[n])
        if n >= BURN_IN and n > 0:
            rot = filt.rot_history[-1]
            H_tf = H_est * rot
            mse = float(np.mean(np.abs(H_tf - H_true[n]) ** 2))
            mse_list.append(mse)

    mse_arr = np.array(mse_list)
    alpha_conv = float(np.mean(filt.alpha_history[-200:]))
    c_est = filt._c_model_est
    return float(mse_arr.mean()), alpha_conv, c_est


def main():
    alpha_grid = np.logspace(-2, 0, 150)

    print("=" * 85)
    print("  IBVSS Adaptive c_model Test (freq selectivity compensation)")
    print("=" * 85)
    print(f"  Speed: {SPEED_KMH} km/h, SNR: {SNR_DB} dB")
    print(f"  Delay spreads: {DELAY_SPREADS_NS} ns")
    print()

    print(f"  {'DS':>6s}  {'c_true':>10s}  {'c_est':>10s}  "
          f"{'α*':>6s}  {'α_fix':>6s}  {'α_adp':>6s}  "
          f"{'r_fix':>7s}  {'r_adp':>7s}  {'Δ':>6s}")
    print("  " + "─" * 78)

    for ds_ns in DELAY_SPREADS_NS:
        rays = generate_cdl_c_rays(delay_spread_ns=ds_ns)

        oracle_curves = []
        fix_trials, adp_trials = [], []
        fix_alpha_trials, adp_alpha_trials = [], []
        c_true_list, c_est_list = [], []

        for trial in range(N_TRIALS):
            rng = np.random.default_rng(88000 + int(ds_ns) * 100 + trial)
            H_true = generate_cdl_channel(rays, SPEED_KMH, N_FRAMES, rng=rng)
            H_obs = add_awgn(H_true, SNR_DB, rng)

            if trial < 3:
                curve, _, _ = oracle_sweep(H_true, H_obs, alpha_grid)
                oracle_curves.append(curve)

        avg_curve = np.mean(oracle_curves, axis=0)
        alpha_opt = float(alpha_grid[np.argmin(avg_curve)])

        for trial in range(N_TRIALS):
            rng = np.random.default_rng(88000 + int(ds_ns) * 100 + trial)
            H_true = generate_cdl_channel(rays, SPEED_KMH, N_FRAMES, rng=rng)
            H_obs = add_awgn(H_true, SNR_DB, rng)

            c_true = float(np.mean(np.abs(
                H_true[:, 0::2] - H_true[:, 1::2]) ** 2)) / 4
            c_true_list.append(c_true)

            fix_mse, fix_a, _ = run_ibvss_variant(
                H_true, H_obs, adaptive_cmodel=False, c_model=0.0)
            adp_mse, adp_a, c_est = run_ibvss_variant(
                H_true, H_obs, adaptive_cmodel=True)

            fix_trials.append(fix_mse)
            adp_trials.append(adp_mse)
            fix_alpha_trials.append(fix_a)
            adp_alpha_trials.append(adp_a)
            c_est_list.append(c_est)

        oracle_trials = []
        for trial in range(N_TRIALS):
            rng = np.random.default_rng(88000 + int(ds_ns) * 100 + trial)
            H_true = generate_cdl_channel(rays, SPEED_KMH, N_FRAMES, rng=rng)
            H_obs = add_awgn(H_true, SNR_DB, rng)
            o_mse, _ = run_fixed_alpha_ema(H_true, H_obs, alpha_opt)
            oracle_trials.append(o_mse)

        oracle_mean = float(np.mean(oracle_trials))
        fix_mean = float(np.mean(fix_trials))
        adp_mean = float(np.mean(adp_trials))

        r_fix = fix_mean / max(oracle_mean, 1e-30)
        r_adp = adp_mean / max(oracle_mean, 1e-30)
        improvement = (r_fix - r_adp) / r_fix * 100

        c_true_avg = float(np.mean(c_true_list))
        c_est_avg = float(np.mean(c_est_list))

        print(f"  {ds_ns:6.0f}  {c_true_avg:10.6f}  {c_est_avg:10.6f}  "
              f"{alpha_opt:6.4f}  "
              f"{np.mean(fix_alpha_trials):6.4f}  "
              f"{np.mean(adp_alpha_trials):6.4f}  "
              f"{r_fix:7.3f}  {r_adp:7.3f}  {improvement:+5.1f}%")

    print()
    print("  α_fix = IBVSS(c_model=0, adaptive=False)")
    print("  α_adp = IBVSS(adaptive_cmodel=True)")
    print("  Δ = relative improvement of adaptive over fixed")
    print("=" * 85)

    return 0


if __name__ == "__main__":
    sys.exit(main())
