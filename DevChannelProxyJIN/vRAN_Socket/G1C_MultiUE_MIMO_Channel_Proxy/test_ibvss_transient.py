"""
Transient Tracking Test — tests IBVSS and Kalman response to abrupt changes.

Test scenarios:
  A. Speed change:  3 km/h → 30 km/h  at frame 1000
  B. SNR change:    20 dB → 5 dB      at frame 1000
  C. Combined:      speed+SNR simultaneous change

Metrics:
  - Convergence frames: number of frames for alpha to reach within 10% of new steady-state
  - Transient MSE penalty: MSE during transition / steady-state MSE
  - Final alpha accuracy: deviation from oracle alpha in new regime

Usage:
  python3 test_ibvss_transient.py [--n-trials 10]
"""
import argparse
import json
import sys
import time
import numpy as np
from pathlib import Path
from multisc_cdl_sim import (
    load_cdl_rays, generate_cdl_channel, add_awgn,
    oracle_sweep, run_fixed_alpha_ema,
    N_SC, BURN_IN, T_FRAME, speed_to_fD
)
from srs_2d_mmse import IBVSS_EMA, ScalarKalmanEMA

N_FRAMES_PER_SEGMENT = 1000
N_FRAMES_TOTAL = N_FRAMES_PER_SEGMENT * 2
ALPHA_GRID = np.logspace(-2, 0, 150)


def generate_speed_change_channel(rays, speed1, speed2, n_per_seg, rng):
    """Generate channel with abrupt speed change at midpoint, phase-continuous."""
    H1 = generate_cdl_channel(rays, speed1, n_per_seg, rng=rng)
    phase_at_switch = np.angle(H1[-1])
    rng2 = np.random.default_rng(rng.integers(0, 2**31))
    H2_raw = generate_cdl_channel(rays, speed2, n_per_seg, rng=rng2)
    phase_offset = phase_at_switch - np.angle(H2_raw[0])
    H2 = H2_raw * np.exp(1j * phase_offset[np.newaxis, :])
    return np.concatenate([H1, H2], axis=0)


def generate_snr_change_obs(H_true, snr1, snr2, n_per_seg, rng):
    """Add AWGN with different SNR for each segment."""
    H1_true = H_true[:n_per_seg]
    H2_true = H_true[n_per_seg:]
    H1_obs = add_awgn(H1_true, snr1, rng)
    rng2 = np.random.default_rng(rng.integers(0, 2**31))
    H2_obs = add_awgn(H2_true, snr2, rng2)
    return np.concatenate([H1_obs, H2_obs], axis=0)


def run_filter(FilterClass, H_true, H_obs, **kwargs):
    """Run a filter, return per-frame MSE + alpha trajectory."""
    n_frames, n_sc = H_true.shape
    filt = FilterClass(n_sc=n_sc, **kwargs)
    mse_per_frame = np.zeros(n_frames)
    for n in range(n_frames):
        H_est = filt.update(H_obs[n])
        if n > 0:
            rot = filt.rot_history[-1]
            H_tf = H_est * rot
            mse_per_frame[n] = float(np.mean(np.abs(H_tf - H_true[n]) ** 2))
    alpha_traj = np.array(filt.alpha_history if hasattr(filt, 'alpha_history')
                          else [getattr(filt, 'alpha', 0.5)] * n_frames)
    return mse_per_frame, alpha_traj


def compute_convergence_frames(alpha_traj, change_point, target_alpha, threshold=0.10):
    """Count frames after change_point for alpha to reach within threshold of target."""
    post = alpha_traj[change_point:]
    for i, a in enumerate(post):
        if abs(a - target_alpha) / max(abs(target_alpha), 1e-6) < threshold:
            return i
    return len(post)


def compute_oracle_for_segment(rays, speed, snr_db, n_frames, rng):
    """Compute oracle alpha for a given speed/SNR."""
    H_t = generate_cdl_channel(rays, speed, n_frames, rng=rng)
    H_o = add_awgn(H_t, snr_db, rng)
    curve, alpha_opt, mse_opt = oracle_sweep(H_t, H_o, ALPHA_GRID)
    return alpha_opt, mse_opt


def run_scenario(scenario_name, rays, n_trials, params):
    """Run one transient scenario."""
    speed1, speed2 = params['speed1'], params['speed2']
    snr1, snr2 = params['snr1'], params['snr2']
    change_point = N_FRAMES_PER_SEGMENT

    rng_oracle = np.random.default_rng(99999)
    alpha_opt_seg2, _ = compute_oracle_for_segment(
        rays, speed2, snr2, N_FRAMES_PER_SEGMENT, rng_oracle)

    ibvss_conv_frames_list = []
    kalman_conv_frames_list = []
    ibvss_transient_ratio_list = []
    kalman_transient_ratio_list = []
    ibvss_final_alpha_list = []
    kalman_final_alpha_list = []

    for trial in range(n_trials):
        seed = 50000 + trial
        rng = np.random.default_rng(seed)

        if speed1 != speed2:
            H_true = generate_speed_change_channel(
                rays, speed1, speed2, N_FRAMES_PER_SEGMENT, rng)
        else:
            H_true = generate_cdl_channel(rays, speed1, N_FRAMES_TOTAL, rng=rng)

        rng_noise = np.random.default_rng(seed + 1000000)
        if snr1 != snr2:
            H_obs = generate_snr_change_obs(
                H_true, snr1, snr2, N_FRAMES_PER_SEGMENT, rng_noise)
        else:
            H_obs = add_awgn(H_true, snr1, rng_noise)

        ibvss_mse, ibvss_alpha = run_filter(IBVSS_EMA, H_true, H_obs)
        kalman_mse, kalman_alpha = run_filter(ScalarKalmanEMA, H_true, H_obs)

        ibvss_conv = compute_convergence_frames(
            ibvss_alpha, change_point, alpha_opt_seg2)
        kalman_conv = compute_convergence_frames(
            kalman_alpha, change_point, alpha_opt_seg2)

        settle_start = min(change_point + 200, N_FRAMES_TOTAL)
        ibvss_ss_mse = float(np.mean(ibvss_mse[settle_start:]))
        kalman_ss_mse = float(np.mean(kalman_mse[settle_start:]))

        trans_window = ibvss_mse[change_point:change_point + 200]
        ibvss_trans_ratio = (float(np.mean(trans_window)) /
                             max(ibvss_ss_mse, 1e-30))
        trans_window_k = kalman_mse[change_point:change_point + 200]
        kalman_trans_ratio = (float(np.mean(trans_window_k)) /
                              max(kalman_ss_mse, 1e-30))

        ibvss_conv_frames_list.append(ibvss_conv)
        kalman_conv_frames_list.append(kalman_conv)
        ibvss_transient_ratio_list.append(ibvss_trans_ratio)
        kalman_transient_ratio_list.append(kalman_trans_ratio)
        ibvss_final_alpha_list.append(float(np.mean(ibvss_alpha[-200:])))
        kalman_final_alpha_list.append(float(np.mean(kalman_alpha[-200:])))

    result = {
        "scenario": scenario_name,
        "params": params,
        "oracle_alpha_seg2": alpha_opt_seg2,
        "n_trials": n_trials,
        "ibvss": {
            "conv_frames_mean": float(np.mean(ibvss_conv_frames_list)),
            "conv_frames_std": float(np.std(ibvss_conv_frames_list)),
            "transient_ratio_mean": float(np.mean(ibvss_transient_ratio_list)),
            "final_alpha_mean": float(np.mean(ibvss_final_alpha_list)),
            "final_alpha_std": float(np.std(ibvss_final_alpha_list)),
        },
        "kalman": {
            "conv_frames_mean": float(np.mean(kalman_conv_frames_list)),
            "conv_frames_std": float(np.std(kalman_conv_frames_list)),
            "transient_ratio_mean": float(np.mean(kalman_transient_ratio_list)),
            "final_alpha_mean": float(np.mean(kalman_final_alpha_list)),
            "final_alpha_std": float(np.std(kalman_final_alpha_list)),
        },
    }
    return result


def main():
    parser = argparse.ArgumentParser(description="IBVSS Transient Tracking Test")
    parser.add_argument("--n-trials", type=int, default=10)
    parser.add_argument("--ray-dir", default="data_out/cdl_c_30kmh")
    args = parser.parse_args()

    rays = load_cdl_rays(args.ray_dir)

    scenarios = [
        ("A: Speed 3→30 km/h @ 10dB", {
            "speed1": 3, "speed2": 30, "snr1": 10, "snr2": 10}),
        ("B: SNR 20→5 dB @ 3km/h", {
            "speed1": 3, "speed2": 3, "snr1": 20, "snr2": 5}),
        ("C: Speed 3→30 + SNR 20→5", {
            "speed1": 3, "speed2": 30, "snr1": 20, "snr2": 5}),
        ("D: Speed 30→3 km/h @ 10dB", {
            "speed1": 30, "speed2": 3, "snr1": 10, "snr2": 10}),
    ]

    print("=" * 78)
    print("  Transient Tracking Test: IBVSS vs Kalman")
    print("=" * 78)
    print(f"  Trials: {args.n_trials}, Frames: {N_FRAMES_TOTAL} "
          f"(change @ {N_FRAMES_PER_SEGMENT})")
    print()

    all_results = []
    for name, params in scenarios:
        t0 = time.time()
        result = run_scenario(name, rays, args.n_trials, params)
        elapsed = time.time() - t0
        all_results.append(result)

        ib = result["ibvss"]
        km = result["kalman"]
        print(f"  {name}")
        print(f"    oracle α (seg2) = {result['oracle_alpha_seg2']:.4f}")
        print(f"    IBVSS:  conv={ib['conv_frames_mean']:.0f}±"
              f"{ib['conv_frames_std']:.0f} frames  "
              f"transient_penalty={ib['transient_ratio_mean']:.3f}x  "
              f"α_final={ib['final_alpha_mean']:.4f}±{ib['final_alpha_std']:.4f}")
        print(f"    Kalman: conv={km['conv_frames_mean']:.0f}±"
              f"{km['conv_frames_std']:.0f} frames  "
              f"transient_penalty={km['transient_ratio_mean']:.3f}x  "
              f"α_final={km['final_alpha_mean']:.4f}±{km['final_alpha_std']:.4f}")
        print(f"    [{elapsed:.1f}s]")
        print()

    out_dir = Path(__file__).parent / "data_out"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "transient_tracking_results.json"
    with open(out_file, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"  Results saved to: {out_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
