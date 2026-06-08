"""
Capture Gate — calibration-free adaptive α vs oracle.

Tests IBVSS_EMA and ScalarKalmanEMA across multiple speeds and SNRs.
Also runs AdaptiveAlphaEMA (Phase 3) as a reference baseline.

Binding test:
  ε = 0.14 (14%)
  MSE-ratio = adaptive_mse / oracle_mse ≤ 1.14

Usage:
  python3 test_ibvss_capture_gate.py [--n-trials 10] [--ray-dir data_out/cdl_c_30kmh]
"""
import argparse
import json
import time
import sys
import numpy as np
from pathlib import Path
from multisc_cdl_sim import (
    load_cdl_rays, generate_cdl_channel, add_awgn,
    oracle_sweep, run_fixed_alpha_ema,
    N_SC, BURN_IN
)
from srs_2d_mmse import IBVSS_EMA, AdaptiveAlphaEMA, ScalarKalmanEMA

EPSILON = 0.14
EPSILON_STRICT = 0.10
EPSILON_TIGHT = 0.05
N_FRAMES = 2000
SPEEDS = [3, 30]
SNRS = [0, 5, 10, 20]


def run_ibvss(H_true, H_obs, **kwargs):
    """Run IBVSS_EMA, return (mean_mse, alpha_converged, diagnostics)."""
    n_frames, n_sc = H_true.shape
    filt = IBVSS_EMA(n_sc=n_sc, **kwargs)

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
    diag = {
        "alpha_final": filt.alpha,
        "alpha_conv": alpha_conv,
        "innov_smooth_final": filt._innov_smooth,
        "ratio_mean": float(np.mean(filt.ratio_history[-200:])),
        "snr_mean": float(np.mean(filt.snr_history[-200:])),
    }
    return float(mse_arr.mean()), alpha_conv, diag


def run_kalman(H_true, H_obs, **kwargs):
    """Run ScalarKalmanEMA, return (mean_mse, alpha_converged, diagnostics)."""
    n_frames, n_sc = H_true.shape
    filt = ScalarKalmanEMA(n_sc=n_sc, **kwargs)

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
    diag = {
        "alpha_final": filt.K,
        "alpha_conv": alpha_conv,
        "P_final": filt.P,
        "Q_final": filt.Q,
        "innov_smooth_final": filt._innov_smooth,
        "ratio_mean": float(np.mean(filt.ratio_history[-200:])),
        "snr_mean": float(np.mean(filt.snr_history[-200:])),
    }
    return float(mse_arr.mean()), alpha_conv, diag


def run_phase3(H_true, H_obs):
    """Run AdaptiveAlphaEMA (Phase 3) as reference."""
    n_frames, n_sc = H_true.shape
    filt = AdaptiveAlphaEMA(n_sc=n_sc)

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
    return float(mse_arr.mean()), alpha_conv


def main():
    parser = argparse.ArgumentParser(description="IBVSS Capture Gate")
    parser.add_argument("--n-trials", type=int, default=20)
    parser.add_argument("--ray-dir", default="data_out/cdl_c_30kmh")
    parser.add_argument("--speeds", nargs="+", type=int, default=SPEEDS)
    parser.add_argument("--snrs", nargs="+", type=float, default=SNRS)
    args = parser.parse_args()

    rays = load_cdl_rays(args.ray_dir)
    alpha_grid = np.logspace(-2, 0, 150)

    eps_levels = [
        ("ε=14%", EPSILON),
        ("ε=10%", EPSILON_STRICT),
        ("ε= 5%", EPSILON_TIGHT),
    ]

    print("=" * 78)
    print("  Capture Gate: IBVSS + ScalarKalman vs oracle")
    print("=" * 78)
    print(f"  Primary ε = {EPSILON*100:.0f}%, also reporting ε={EPSILON_STRICT*100:.0f}% and ε={EPSILON_TIGHT*100:.0f}%")
    print(f"  Speeds: {args.speeds} km/h")
    print(f"  SNRs: {args.snrs} dB")
    print(f"  Trials: {args.n_trials}, Frames: {N_FRAMES}, Burn-in: {BURN_IN}")
    print()

    all_results = []
    n_pass_ibvss = 0
    n_pass_kalman = 0
    n_total = 0
    gate_counts = {name: {"ibvss": 0, "kalman": 0} for name, _ in eps_levels}

    for snr_db in args.snrs:
        print(f"─── SNR = {snr_db} dB ───")
        for speed in args.speeds:
            t0 = time.time()
            n_total += 1

            oracle_curves = []
            for trial in range(min(args.n_trials, 3)):
                rng = np.random.default_rng(42000 + int(snr_db) * 10000
                                            + speed * 1000 + trial)
                H_true = generate_cdl_channel(rays, speed, N_FRAMES, rng=rng)
                H_obs = add_awgn(H_true, snr_db, rng)
                curve, _, _ = oracle_sweep(H_true, H_obs, alpha_grid)
                oracle_curves.append(curve)
            avg_curve = np.mean(oracle_curves, axis=0)
            opt_idx = np.argmin(avg_curve)
            alpha_opt = float(alpha_grid[opt_idx])

            oracle_trials = []
            ibvss_trials, ibvss_alpha_trials, ibvss_diags = [], [], []
            kalman_trials, kalman_alpha_trials, kalman_diags = [], [], []
            phase3_trials, phase3_alpha_trials = [], []

            for trial in range(args.n_trials):
                rng = np.random.default_rng(42000 + int(snr_db) * 10000
                                            + speed * 1000 + trial)
                H_true = generate_cdl_channel(rays, speed, N_FRAMES, rng=rng)
                H_obs = add_awgn(H_true, snr_db, rng)

                o_mse, _ = run_fixed_alpha_ema(H_true, H_obs, alpha_opt)
                oracle_trials.append(o_mse)

                i_mse, i_alpha, i_diag = run_ibvss(H_true, H_obs)
                ibvss_trials.append(i_mse)
                ibvss_alpha_trials.append(i_alpha)
                ibvss_diags.append(i_diag)

                k_mse, k_alpha, k_diag = run_kalman(H_true, H_obs)
                kalman_trials.append(k_mse)
                kalman_alpha_trials.append(k_alpha)
                kalman_diags.append(k_diag)

                p_mse, p_alpha = run_phase3(H_true, H_obs)
                phase3_trials.append(p_mse)
                phase3_alpha_trials.append(p_alpha)

            oracle_mean = float(np.mean(oracle_trials))
            ibvss_mean = float(np.mean(ibvss_trials))
            kalman_mean = float(np.mean(kalman_trials))
            phase3_mean = float(np.mean(phase3_trials))

            r_ibvss = ibvss_mean / max(oracle_mean, 1e-30)
            r_kalman = kalman_mean / max(oracle_mean, 1e-30)
            r_phase3 = phase3_mean / max(oracle_mean, 1e-30)

            p_ibvss = r_ibvss <= (1 + EPSILON)
            p_kalman = r_kalman <= (1 + EPSILON)
            if p_ibvss:
                n_pass_ibvss += 1
            if p_kalman:
                n_pass_kalman += 1

            for name, eps_val in eps_levels:
                if r_ibvss <= (1 + eps_val):
                    gate_counts[name]["ibvss"] += 1
                if r_kalman <= (1 + eps_val):
                    gate_counts[name]["kalman"] += 1

            ibvss_std = float(np.std(ibvss_trials))
            kalman_std = float(np.std(kalman_trials))

            elapsed = time.time() - t0
            avg_k_diag = {
                k: float(np.mean([d[k] for d in kalman_diags]))
                for k in kalman_diags[0]
            }

            tag_i = "PASS" if p_ibvss else "FAIL"
            tag_k = "PASS" if p_kalman else "FAIL"
            print(f"  {speed:3d} km/h | "
                  f"oracle={oracle_mean:.5f} (α*={alpha_opt:.4f})")
            print(f"    IBVSS  [{tag_i}] mse={ibvss_mean:.5f}±{ibvss_std:.5f} "
                  f"r={r_ibvss:.3f} α={np.mean(ibvss_alpha_trials):.3f}")
            print(f"    Kalman [{tag_k}] mse={kalman_mean:.5f}±{kalman_std:.5f} "
                  f"r={r_kalman:.3f} α={np.mean(kalman_alpha_trials):.3f} "
                  f"P={avg_k_diag['P_final']:.4f} Q={avg_k_diag['Q_final']:.6f}")
            print(f"    Ph3    mse={phase3_mean:.5f} "
                  f"r={r_phase3:.3f} α={np.mean(phase3_alpha_trials):.3f}  "
                  f"[{elapsed:.1f}s]")

            all_results.append({
                "snr_dB": snr_db,
                "speed_kmh": speed,
                "oracle_mse": oracle_mean,
                "oracle_alpha_opt": alpha_opt,
                "ibvss_mse": ibvss_mean,
                "ibvss_mse_std": ibvss_std,
                "ibvss_ratio": r_ibvss,
                "ibvss_alpha_conv": float(np.mean(ibvss_alpha_trials)),
                "ibvss_alpha_std": float(np.std(ibvss_alpha_trials)),
                "kalman_mse": kalman_mean,
                "kalman_mse_std": kalman_std,
                "kalman_ratio": r_kalman,
                "kalman_alpha_conv": float(np.mean(kalman_alpha_trials)),
                "kalman_alpha_std": float(np.std(kalman_alpha_trials)),
                "kalman_diag": avg_k_diag,
                "phase3_mse": phase3_mean,
                "phase3_ratio": r_phase3,
                "phase3_alpha_conv": float(np.mean(phase3_alpha_trials)),
                "passed_ibvss": p_ibvss,
                "passed_kalman": p_kalman,
            })
        print()

    print("=" * 78)
    print("  Multi-level Gate Summary:")
    for name, eps_val in eps_levels:
        ic = gate_counts[name]["ibvss"]
        kc = gate_counts[name]["kalman"]
        print(f"    {name}:  IBVSS {ic}/{n_total}  |  Kalman {kc}/{n_total}")
    print()

    hdr = (f"  {'SNR':>5s}  {'Speed':>5s}  {'Oracle':>9s}  "
           f"{'IBVSS':>9s} {'r_i':>5s}  "
           f"{'Kalman':>9s} {'r_k':>5s}  "
           f"{'Ph3':>9s} {'r_p':>5s}  {'α_std_i':>7s}")
    print(hdr)
    print("  " + "─" * 84)
    for r in all_results:
        print(f"  {r['snr_dB']:5.0f}  {r['speed_kmh']:5d}  "
              f"{r['oracle_mse']:9.5f}  "
              f"{r['ibvss_mse']:9.5f} {r['ibvss_ratio']:5.3f}  "
              f"{r['kalman_mse']:9.5f} {r['kalman_ratio']:5.3f}  "
              f"{r['phase3_mse']:9.5f} {r['phase3_ratio']:5.3f}  "
              f"{r['ibvss_alpha_std']:7.4f}")
    print("=" * 78)

    out_dir = Path(__file__).parent / "data_out"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "capture_gate_results.json"
    save_data = {
        "verdict_ibvss": f"{n_pass_ibvss}/{n_total} PASS",
        "verdict_kalman": f"{n_pass_kalman}/{n_total} PASS",
        "gate_multi_level": {
            name: {"ibvss": gate_counts[name]["ibvss"],
                   "kalman": gate_counts[name]["kalman"],
                   "total": n_total}
            for name, _ in eps_levels
        },
        "epsilon": EPSILON,
        "n_trials": args.n_trials,
        "n_frames": N_FRAMES,
        "burn_in": BURN_IN,
        "ray_dir": args.ray_dir,
        "results": all_results,
    }
    with open(out_file, "w") as f:
        json.dump(save_data, f, indent=2)
    print(f"\n  Results saved to: {out_file}")

    return 0 if (n_pass_ibvss == n_total and n_pass_kalman == n_total) else 1


if __name__ == "__main__":
    sys.exit(main())
