"""
Test IBVSS robustness under increasing frequency selectivity.

Simulates P1B-like conditions by increasing CDL-C delay spread.
Checks:
  1. R_est bias: even-odd noise estimate vs true noise variance
  2. IBVSS capture gate at each delay spread
  3. alpha convergence accuracy

Usage:
  python3 test_ibvss_freq_selectivity.py
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
DELAY_SPREADS_NS = [30, 100, 300, 500, 1000]


def measure_rest_bias(H_true, H_obs, sigma2_true):
    """Measure even-odd R_est vs true noise variance, per frame."""
    n_frames, n_sc = H_true.shape
    rest_list = []
    c_model_list = []

    for n in range(n_frames):
        d = H_true[n, 0::2] - H_true[n, 1::2]
        c_true = float(np.mean(np.abs(d) ** 2))
        c_model_list.append(c_true)

        d_obs = H_obs[n, 0::2] - H_obs[n, 1::2]
        p_minus = float(np.mean(np.abs(d_obs) ** 2)) / 4
        rest_list.append(p_minus * 2.0)

    c_model_mean = float(np.mean(c_model_list))
    rest_raw = float(np.mean(rest_list))
    rest_corrected = rest_raw - c_model_mean / 2
    return {
        "sigma2_true": sigma2_true,
        "R_est_raw": rest_raw,
        "R_est_corrected": rest_corrected,
        "c_model": c_model_mean,
        "bias_raw": rest_raw / sigma2_true,
        "bias_corrected": rest_corrected / max(sigma2_true, 1e-30),
    }


def run_ibvss_test(H_true, H_obs, c_model=0.0):
    n_frames, n_sc = H_true.shape
    filt = IBVSS_EMA(n_sc=n_sc, c_model=c_model)

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
    ratio_conv = float(np.mean(filt.ratio_history[-200:]))
    return float(mse_arr.mean()), alpha_conv, ratio_conv


def main():
    alpha_grid = np.logspace(-2, 0, 150)

    print("=" * 80)
    print("  IBVSS Frequency Selectivity Robustness Test")
    print("=" * 80)
    print(f"  Speed: {SPEED_KMH} km/h, SNR: {SNR_DB} dB")
    print(f"  Delay spreads: {DELAY_SPREADS_NS} ns")
    print(f"  Trials: {N_TRIALS}, Frames: {N_FRAMES}")
    print()

    print(f"  {'DS(ns)':>7s}  {'c_model':>9s}  {'R_bias_raw':>10s}  "
          f"{'R_bias_cor':>10s}  {'α*':>6s}  {'α_ibvss0':>8s}  "
          f"{'α_ibvssC':>8s}  {'r_ib0':>7s}  {'r_ibC':>7s}  {'Gate':>4s}")
    print("  " + "─" * 90)

    all_pass = True

    for ds_ns in DELAY_SPREADS_NS:
        rays = generate_cdl_c_rays(delay_spread_ns=ds_ns)

        oracle_trials = []
        ibvss0_trials = []
        ibvssC_trials = []
        ibvss0_alpha = []
        ibvssC_alpha = []
        bias_data = []

        alpha_opt = None

        for trial in range(N_TRIALS):
            rng = np.random.default_rng(77000 + int(ds_ns) * 100 + trial)
            H_true = generate_cdl_channel(rays, SPEED_KMH, N_FRAMES, rng=rng)
            H_obs = add_awgn(H_true, SNR_DB, rng)

            sig_power = float(np.mean(np.abs(H_true) ** 2))
            sigma2 = sig_power * 10.0 ** (-SNR_DB / 10.0)

            if trial < 3:
                curve, _, _ = oracle_sweep(H_true, H_obs, alpha_grid)
                oracle_trials.append(curve)

            if trial == 2:
                avg_c = np.mean(oracle_trials, axis=0)
                alpha_opt = float(alpha_grid[np.argmin(avg_c)])

            if alpha_opt is not None:
                o_mse, _ = run_fixed_alpha_ema(H_true, H_obs, alpha_opt)
            else:
                o_mse, _ = run_fixed_alpha_ema(H_true, H_obs, 0.3)

            c_model_for_ds = float(np.mean(
                np.abs(H_true[:, 0::2] - H_true[:, 1::2]) ** 2))

            i0_mse, i0_a, _ = run_ibvss_test(H_true, H_obs, c_model=0.0)
            iC_mse, iC_a, _ = run_ibvss_test(H_true, H_obs,
                                               c_model=c_model_for_ds / 4)

            ibvss0_trials.append(i0_mse)
            ibvssC_trials.append(iC_mse)
            ibvss0_alpha.append(i0_a)
            ibvssC_alpha.append(iC_a)

            bd = measure_rest_bias(H_true, H_obs, sigma2)
            bias_data.append(bd)

            if alpha_opt is None:
                avg_c = np.mean([oracle_sweep(H_true, H_obs, alpha_grid)[0]
                                 for _ in range(1)], axis=0)
                alpha_opt = float(alpha_grid[np.argmin(avg_c)])
                o_mse, _ = run_fixed_alpha_ema(H_true, H_obs, alpha_opt)

        if alpha_opt is not None:
            oracle_mse_list = []
            for trial in range(N_TRIALS):
                rng = np.random.default_rng(77000 + int(ds_ns) * 100 + trial)
                H_true = generate_cdl_channel(rays, SPEED_KMH, N_FRAMES, rng=rng)
                H_obs = add_awgn(H_true, SNR_DB, rng)
                o_mse, _ = run_fixed_alpha_ema(H_true, H_obs, alpha_opt)
                oracle_mse_list.append(o_mse)
            oracle_mean = float(np.mean(oracle_mse_list))
        else:
            oracle_mean = 1.0

        ibvss0_mean = float(np.mean(ibvss0_trials))
        ibvssC_mean = float(np.mean(ibvssC_trials))

        r_ib0 = ibvss0_mean / max(oracle_mean, 1e-30)
        r_ibC = ibvssC_mean / max(oracle_mean, 1e-30)

        avg_bd = {k: float(np.mean([b[k] for b in bias_data]))
                  for k in bias_data[0]}

        passed = r_ibC <= (1 + EPSILON)
        if not passed:
            all_pass = False
        tag = "PASS" if passed else "FAIL"

        print(f"  {ds_ns:7.0f}  {avg_bd['c_model']:9.6f}  "
              f"{avg_bd['bias_raw']:10.4f}  {avg_bd['bias_corrected']:10.4f}  "
              f"{alpha_opt:6.4f}  "
              f"{np.mean(ibvss0_alpha):8.4f}  {np.mean(ibvssC_alpha):8.4f}  "
              f"{r_ib0:7.3f}  {r_ibC:7.3f}  {tag}")

    print()
    print("  c_model = E[|H_true[even] - H_true[odd]|²] (channel variation)")
    print("  R_bias_raw = R_est_raw / σ²_true  (1.0 = perfect)")
    print("  R_bias_cor = R_est_corrected / σ²_true  (after c_model subtraction)")
    print("  α_ibvss0 = IBVSS with c_model=0, α_ibvssC = IBVSS with oracle c_model")
    print("=" * 80)

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
