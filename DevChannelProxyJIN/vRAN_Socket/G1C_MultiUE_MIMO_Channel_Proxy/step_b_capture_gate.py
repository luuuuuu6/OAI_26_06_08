"""
Step B: Capture Gate — rot_rate→α adaptive filter vs oracle.

Binding test (pre-committed, no relaxation):
  ε = 0.14 (14%)
  3km/h leg MSE-ratio ≤ 1.14   (deployment-relevant: B2=1.085 dB > 0.5)
  30km/h leg as sanity check    (B2=0.489 dB < 0.5, not deployment-relevant)

Comparison structure:
  Oracle   = self-referenced de-rotation + fixed α (same as Phase 1)
  Adaptive = DualEMA (self-ref derot for Main EMA + rot_rate from Ref EMA)

  Both Main EMAs use identical self-referenced de-rotation.
  True-frame MSE: |H_main·rot_main − H_true|²

Usage:
  python3 step_b_capture_gate.py [--n-trials 20]
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
SNR_DB = 10
N_FRAMES = 2000
RAY_DIR = "data_out/cdl_c_30kmh"

FIT_A = 0.1437
FIT_B = 0.4493


def run_adaptive_filter(H_true, H_obs, rot_rate_a, rot_rate_b):
    """Run DualEMASRS2DFilter with rot_rate mapping, true-frame MSE.

    Main EMA uses self-referenced de-rotation (same as oracle).
    True-frame: H_main * rot_main.
    """
    n_frames, n_sc = H_true.shape
    filt = DualEMASRS2DFilter(
        n_sc=n_sc,
        alpha_ref=0.02,
        alpha_init=0.5,
        alpha_min=0.01,
        alpha_max=0.95,
        rot_rate_ema=0.05,
        rot_rate_a=rot_rate_a,
        rot_rate_b=rot_rate_b,
    )

    mse_list = []
    for n in range(n_frames):
        H_est = filt.update(H_obs[n])

        if n >= BURN_IN and n > 0:
            rot_main = filt.rot_main_history[-1]
            H_tf = H_est * rot_main
            mse = float(np.mean(np.abs(H_tf - H_true[n]) ** 2))
            mse_list.append(mse)

    mse_arr = np.array(mse_list)
    alpha_converged = float(np.mean(filt.alpha_history[-200:]))
    return float(mse_arr.mean()), alpha_converged


def main():
    parser = argparse.ArgumentParser(description="Step B: Capture Gate")
    parser.add_argument("--n-trials", type=int, default=20)
    parser.add_argument("--ray-dir", default=RAY_DIR)
    parser.add_argument("--fit-a", type=float, default=FIT_A)
    parser.add_argument("--fit-b", type=float, default=FIT_B)
    args = parser.parse_args()

    rays = load_cdl_rays(args.ray_dir)
    alpha_grid = np.logspace(-2, 0, 150)

    print("=" * 72)
    print("Step B: Capture Gate (rot_rate → α, self-ref de-rotation)")
    print("=" * 72)
    print(f"  ε = {EPSILON*100:.0f}% (binding: MSE-ratio ≤ {1+EPSILON:.2f})")
    print(f"  Mapping: α = {args.fit_a:.4f}·rot_rate + {args.fit_b:.4f}")
    print(f"  SNR: {SNR_DB} dB, N_SC: {N_SC}, Frames: {N_FRAMES}")
    print(f"  Trials: {args.n_trials}, Burn-in: {BURN_IN}")
    print(f"  Oracle: self-referenced de-rot + best fixed-α (Phase 1 structure)")
    print()

    speeds = [3, 30]
    all_results = []
    gate_pass = True

    for speed in speeds:
        t0 = time.time()

        # Find oracle α_opt (self-ref derot, same as Phase 1)
        print(f"  {speed} km/h: oracle sweep ...", end="", flush=True)
        mse_curves = []
        for trial in range(min(args.n_trials, 5)):
            rng = np.random.default_rng(99000 + speed * 1000 + trial)
            H_true = generate_cdl_channel(rays, speed, N_FRAMES, rng=rng)
            H_obs = add_awgn(H_true, SNR_DB, rng)
            curve, _, _ = oracle_sweep(H_true, H_obs, alpha_grid)
            mse_curves.append(curve)
        avg_curve = np.mean(mse_curves, axis=0)
        opt_idx = np.argmin(avg_curve)
        alpha_opt = float(alpha_grid[opt_idx])
        print(f" α_opt={alpha_opt:.4f}", flush=True)

        # Run oracle + adaptive for all trials
        oracle_mse_trials = []
        adaptive_mse_trials = []
        alpha_conv_trials = []

        for trial in range(args.n_trials):
            rng = np.random.default_rng(99000 + speed * 1000 + trial)
            H_true = generate_cdl_channel(rays, speed, N_FRAMES, rng=rng)
            H_obs = add_awgn(H_true, SNR_DB, rng)

            o_mse, _ = run_fixed_alpha_ema(H_true, H_obs, alpha_opt)
            oracle_mse_trials.append(o_mse)

            a_mse, a_conv = run_adaptive_filter(
                H_true, H_obs, args.fit_a, args.fit_b)
            adaptive_mse_trials.append(a_mse)
            alpha_conv_trials.append(a_conv)

        oracle_mean = float(np.mean(oracle_mse_trials))
        adaptive_mean = float(np.mean(adaptive_mse_trials))
        ratio = adaptive_mean / oracle_mean
        alpha_conv_mean = float(np.mean(alpha_conv_trials))

        is_deployment = speed == 3
        passed = ratio <= (1 + EPSILON)
        if is_deployment and not passed:
            gate_pass = False

        status_str = "PASS" if passed else "FAIL"
        relevance = "DEPLOYMENT" if is_deployment else "sanity-check"

        elapsed = time.time() - t0
        print(f"  [{status_str}] {speed} km/h ({relevance}):")
        print(f"    Oracle MSE:   {oracle_mean:.6f} (α_opt={alpha_opt:.4f})")
        print(f"    Adaptive MSE: {adaptive_mean:.6f} (α_conv={alpha_conv_mean:.4f})")
        print(f"    Ratio: {ratio:.4f} (limit: {1+EPSILON:.2f})")
        print(f"    [{elapsed:.1f}s]")
        print()

        all_results.append({
            "speed_kmh": speed,
            "oracle_mse": oracle_mean,
            "oracle_alpha_opt": alpha_opt,
            "adaptive_mse": adaptive_mean,
            "ratio": ratio,
            "alpha_converged": alpha_conv_mean,
            "passed": passed,
            "deployment_relevant": is_deployment,
        })

    # ── Verdict ──
    print("=" * 72)
    if gate_pass:
        print("CAPTURE GATE: PASS")
        print("  → rot_rate→α mapping captures deployment value")
        print("  → Continue to Step C (generalization gate)")
    else:
        print("CAPTURE GATE: FAIL")
        r3 = all_results[0]
        print(f"  → 3km/h ratio = {r3['ratio']:.4f} > {1+EPSILON:.2f}")
        print(f"  → α_converged = {r3['alpha_converged']:.4f} "
              f"(oracle = {r3['oracle_alpha_opt']:.4f})")
        overshoot = (r3['alpha_converged'] - r3['oracle_alpha_opt']) / r3['oracle_alpha_opt'] * 100
        if overshoot > 0:
            print(f"  → α {overshoot:+.1f}% over oracle → under-smoothing")
        else:
            print(f"  → α {overshoot:+.1f}% under oracle → over-smoothing")
        print("  → Debug: check rot_rate_smooth convergence, mapping residual")
    print("=" * 72)

    out_dir = Path(__file__).parent / "data_out"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "step_b_capture_gate_results.json"
    save_data = {
        "verdict": "PASS" if gate_pass else "FAIL",
        "epsilon": EPSILON,
        "fit": {"a": args.fit_a, "b": args.fit_b},
        "results": all_results,
        "params": {
            "snr_dB": SNR_DB, "n_sc": N_SC, "n_frames": N_FRAMES,
            "n_trials": args.n_trials, "burn_in": BURN_IN,
        },
    }
    with open(out_file, "w") as f:
        json.dump(save_data, f, indent=2)
    print(f"\n  Results saved to: {out_file}")

    return 0 if gate_pass else 1


if __name__ == "__main__":
    sys.exit(main())
