"""
Step C: Generalization Gate.

Tests whether CDL-C/10dB-fitted rot_rate→α mapping generalizes to:
  C.1: Other SNR (5dB, 20dB) on CDL-C
  C.2: Other CDL models (CDL-A, CDL-D) at 10dB

For each condition: oracle sweep + adaptive filter, report MSE-ratio.
ε = 0.14 (14%) binding threshold, same as Step B.

Usage:
  python3 step_c_generalization_gate.py [--n-trials 10]
"""
import argparse
import json
import time
import numpy as np
import sys
from pathlib import Path
from multisc_cdl_sim import (
    load_cdl_rays, generate_cdl_channel, add_awgn,
    oracle_sweep, run_fixed_alpha_ema,
    N_SC, BURN_IN
)
from srs_2d_mmse import DualEMASRS2DFilter

EPSILON = 0.14
N_FRAMES = 2000

FIT_A = 0.1437
FIT_B = 0.4493

CONDITIONS = [
    {"label": "C.1: CDL-C 5dB",  "ray_dir": "data_out/cdl_c_30kmh", "snr": 5,  "speeds": [3, 30]},
    {"label": "C.1: CDL-C 20dB", "ray_dir": "data_out/cdl_c_30kmh", "snr": 20, "speeds": [3, 30]},
    {"label": "C.2: CDL-A 10dB", "ray_dir": "data_out/cdl_a",       "snr": 10, "speeds": [3, 30]},
    {"label": "C.2: CDL-D 10dB", "ray_dir": "data_out/cdl_d",       "snr": 10, "speeds": [3, 30]},
]


def run_adaptive_filter(H_true, H_obs, rot_rate_a, rot_rate_b):
    n_frames, n_sc = H_true.shape
    filt = DualEMASRS2DFilter(
        n_sc=n_sc, alpha_ref=0.02, alpha_init=0.5,
        alpha_min=0.01, alpha_max=0.95,
        rot_rate_ema=0.05, rot_rate_a=rot_rate_a, rot_rate_b=rot_rate_b,
    )
    mse_list = []
    for n in range(n_frames):
        H_est = filt.update(H_obs[n])
        if n >= BURN_IN and n > 0:
            rot_main = filt.rot_main_history[-1]
            H_tf = H_est * rot_main
            mse_list.append(float(np.mean(np.abs(H_tf - H_true[n]) ** 2)))
    alpha_conv = float(np.mean(filt.alpha_history[-200:]))
    return float(np.mean(mse_list)), alpha_conv


def run_condition(cond, n_trials, alpha_grid, fit_a, fit_b):
    rays = load_cdl_rays(cond["ray_dir"])
    snr = cond["snr"]
    results = []

    for speed in cond["speeds"]:
        # Oracle sweep (5 trials for sweep, all for final MSE)
        mse_curves = []
        for trial in range(min(n_trials, 5)):
            rng = np.random.default_rng(77000 + snr * 10000 + speed * 1000 + trial)
            H_true = generate_cdl_channel(rays, speed, N_FRAMES, rng=rng)
            H_obs = add_awgn(H_true, snr, rng)
            curve, _, _ = oracle_sweep(H_true, H_obs, alpha_grid)
            mse_curves.append(curve)
        avg_curve = np.mean(mse_curves, axis=0)
        alpha_opt = float(alpha_grid[np.argmin(avg_curve)])

        oracle_trials, adaptive_trials, aconv_trials = [], [], []
        for trial in range(n_trials):
            rng = np.random.default_rng(77000 + snr * 10000 + speed * 1000 + trial)
            H_true = generate_cdl_channel(rays, speed, N_FRAMES, rng=rng)
            H_obs = add_awgn(H_true, snr, rng)
            o_mse, _ = run_fixed_alpha_ema(H_true, H_obs, alpha_opt)
            a_mse, a_conv = run_adaptive_filter(H_true, H_obs, fit_a, fit_b)
            oracle_trials.append(o_mse)
            adaptive_trials.append(a_mse)
            aconv_trials.append(a_conv)

        o_mean = float(np.mean(oracle_trials))
        a_mean = float(np.mean(adaptive_trials))
        ratio = a_mean / o_mean
        a_conv_mean = float(np.mean(aconv_trials))
        passed = ratio <= (1 + EPSILON)
        status = "PASS" if passed else "FAIL"

        print(f"    [{status}] {speed:>3d} km/h: oracle={o_mean:.6f} (α={alpha_opt:.4f}) "
              f"adaptive={a_mean:.6f} (α_conv={a_conv_mean:.4f}) "
              f"ratio={ratio:.4f}")

        results.append({
            "speed_kmh": speed, "oracle_mse": o_mean, "alpha_opt": alpha_opt,
            "adaptive_mse": a_mean, "alpha_converged": a_conv_mean,
            "ratio": ratio, "passed": passed,
        })

    return results


def main():
    parser = argparse.ArgumentParser(description="Step C: Generalization Gate")
    parser.add_argument("--n-trials", type=int, default=10)
    parser.add_argument("--fit-a", type=float, default=FIT_A)
    parser.add_argument("--fit-b", type=float, default=FIT_B)
    args = parser.parse_args()

    alpha_grid = np.logspace(-2, 0, 150)

    print("=" * 76)
    print("Step C: Generalization Gate")
    print("=" * 76)
    print(f"  ε = {EPSILON*100:.0f}% (binding: MSE-ratio ≤ {1+EPSILON:.2f})")
    print(f"  Mapping: α = {args.fit_a:.4f}·rot_rate + {args.fit_b:.4f} (CDL-C/10dB fit)")
    print(f"  Trials: {args.n_trials}, Frames: {N_FRAMES}, Burn-in: {BURN_IN}")
    print()

    all_conditions = CONDITIONS

    all_results = {}
    n_pass, n_total = 0, 0
    t_total = time.time()

    for cond in all_conditions:
        t0 = time.time()
        print(f"  {cond['label']}:")
        results = run_condition(cond, args.n_trials, alpha_grid,
                                args.fit_a, args.fit_b)
        elapsed = time.time() - t0
        print(f"    [{elapsed:.1f}s]")
        print()
        all_results[cond["label"]] = results
        for r in results:
            n_total += 1
            if r["passed"]:
                n_pass += 1

    # Summary
    print("=" * 76)
    print("GENERALIZATION SUMMARY")
    print("=" * 76)
    print(f"  {'Condition':<25s} {'3km/h ratio':>12s} {'30km/h ratio':>13s} {'Verdict':>8s}")
    print("  " + "-" * 60)
    for label, results in all_results.items():
        r3 = next((r for r in results if r["speed_kmh"] == 3), None)
        r30 = next((r for r in results if r["speed_kmh"] == 30), None)
        ratio3 = f"{r3['ratio']:.4f}" if r3 else "N/A"
        ratio30 = f"{r30['ratio']:.4f}" if r30 else "N/A"
        all_pass = all(r["passed"] for r in results)
        verdict = "PASS" if all_pass else "FAIL"
        print(f"  {label:<25s} {ratio3:>12s} {ratio30:>13s} {verdict:>8s}")

    total_time = time.time() - t_total
    print(f"\n  Overall: {n_pass}/{n_total} legs pass ({total_time:.0f}s)")

    if n_pass == n_total:
        print("  → CDL-C/10dB 系数泛化成功，无需 per-condition re-fit")
    else:
        fail_conds = [label for label, results in all_results.items()
                      if not all(r["passed"] for r in results)]
        print(f"  → FAIL 条件: {', '.join(fail_conds)}")
        print("  → 可能需要 per-SNR 或 per-model re-fit")

    print("=" * 76)

    out_dir = Path(__file__).parent / "data_out"
    out_file = out_dir / "step_c_generalization_results.json"
    with open(out_file, "w") as f:
        json.dump({
            "fit": {"a": args.fit_a, "b": args.fit_b},
            "epsilon": EPSILON,
            "results": all_results,
            "summary": {"pass": n_pass, "total": n_total},
        }, f, indent=2)
    print(f"\n  Results saved to: {out_file}")

    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main())
