"""
CDL-C single re-test with RNG independence guarantees.

Goal:
  1) Fix RNG independence in simulation path.
  2) Re-run CDL-C only calibration + gate and report PASS count out of 8.

RNG rules enforced here:
  - Complex AWGN real/imag are generated from two sequential draws on ONE RNG object.
  - Channel random phase and noise random numbers are also drawn sequentially from ONE RNG
    object per trial (no channel/noise same-seed reinitialization).

Usage:
  python3 cdl_c_single_retest.py
  python3 cdl_c_single_retest.py --cal-trials 8 --gate-trials 8 --alpha-grid 80
"""

import argparse
import json
import time
from pathlib import Path

import cupy as cp
import numpy as np


FC_HZ = 3.5e9
LIGHT = 3e8
T_FRAME = 5e-3
SCS = 30e3
N_SC = 1248
BURN_IN = 100
N_FRAMES = 2000
EPSILON = 0.14

CAL_SNRS = [0, 5, 10, 15, 20]
CAL_SPEEDS = [3, 5, 10, 15, 20, 30]
GATE_SNRS = [0, 5, 10, 20]
GATE_SPEEDS = [3, 30]


def load_cdl_c_rays(ray_dir: Path):
    rays = {
        "power": np.load(ray_dir / "power_rays_for_ChannelBlock.npy").flatten().astype(np.float64),
        "tau": np.load(ray_dir / "tau_rays_for_ChannelBlock.npy").flatten().astype(np.float64),
        "phi_r": np.load(ray_dir / "phi_r_rays_for_ChannelBlock.npy").flatten().astype(np.float64),
        "theta_r": np.load(ray_dir / "theta_r_rays_for_ChannelBlock.npy").flatten().astype(np.float64),
    }
    p_sum = rays["power"].sum()
    if p_sum <= 1e-30:
        raise ValueError("CDL-C power sum is zero.")
    rays["power"] /= p_sum
    return rays


def speed_to_fd(speed_kmh: float) -> float:
    return (speed_kmh / 3.6) * FC_HZ / LIGHT


def generate_channel(rays, speed_kmh: float, n_frames: int, rng: np.random.Generator):
    f_d_max = speed_to_fd(speed_kmh)
    power = cp.asarray(rays["power"])
    tau = cp.asarray(rays["tau"])
    phi_r = cp.asarray(rays["phi_r"])
    theta_r = cp.asarray(rays["theta_r"])

    doppler = f_d_max * cp.sin(theta_r) * cp.cos(phi_r)
    psi = cp.asarray(rng.uniform(0.0, 2.0 * np.pi, power.size))

    n_arr = cp.arange(n_frames, dtype=cp.float64)
    k_arr = cp.arange(N_SC, dtype=cp.float64)

    time_phase = 2.0 * cp.pi * cp.outer(n_arr * T_FRAME, doppler) + psi[cp.newaxis, :]
    time_comp = cp.sqrt(power[cp.newaxis, :]) * cp.exp(1j * time_phase)
    freq_steer = cp.exp(1j * (-2.0 * cp.pi * cp.outer(k_arr * SCS, tau)))
    return time_comp @ freq_steer.T


def add_awgn(h_true, snr_db: float, rng: np.random.Generator):
    sig_pow = float(cp.mean(cp.abs(h_true) ** 2))
    noise_var = sig_pow * (10.0 ** (-snr_db / 10.0))
    noise_std = np.sqrt(noise_var / 2.0)

    # Two independent draws from one RNG state.
    n_re = rng.normal(0.0, noise_std, h_true.shape)
    n_im = rng.normal(0.0, noise_std, h_true.shape)
    return h_true + cp.asarray(n_re + 1j * n_im)


def oracle_sweep(h_true, h_obs, alpha_grid):
    n_alpha = alpha_grid.size
    h_sm = cp.tile(h_obs[0], (n_alpha, 1)).astype(cp.complex128)
    mse_acc = cp.zeros(n_alpha, dtype=cp.float64)
    count = 0

    for n in range(1, h_true.shape[0]):
        inner = h_sm.conj() @ h_obs[n]
        inner_abs = cp.abs(inner)
        rot = cp.where(inner_abs > 1e-30, inner / inner_abs, 1.0 + 0j)
        h_derot = h_obs[n][cp.newaxis, :] * cp.conj(rot[:, cp.newaxis])
        h_sm += alpha_grid[:, cp.newaxis] * (h_derot - h_sm)

        if n >= BURN_IN:
            h_est_true = h_sm * rot[:, cp.newaxis]
            mse_acc += cp.mean(cp.abs(h_est_true - h_true[n][cp.newaxis, :]) ** 2, axis=1)
            count += 1

    curve = mse_acc / count
    idx = int(cp.argmin(curve).item())
    return float(alpha_grid[idx].item()), float(curve[idx].item())


def measure_features(h_obs, c_model: float):
    h_sm = h_obs[0].copy().astype(cp.complex128)
    rot_rates = []
    snr_list = []
    prev_ang = 0.0

    for n in range(1, h_obs.shape[0]):
        inner = cp.dot(h_obs[n], cp.conj(h_sm))
        inner_abs = float(cp.abs(inner))
        rot = inner / inner_abs if inner_abs > 1e-30 else 1.0 + 0j
        h_sm += 0.5 * (h_obs[n] * cp.conj(rot) - h_sm)

        ang = float(cp.angle(rot))
        rr = abs(ang - prev_ang)
        if rr > np.pi:
            rr = 2.0 * np.pi - rr
        prev_ang = ang

        s = h_obs[n, 0::2] + h_obs[n, 1::2]
        d = h_obs[n, 0::2] - h_obs[n, 1::2]
        p_plus = float(cp.mean(cp.abs(s) ** 2)) / 4.0
        p_minus = float(cp.mean(cp.abs(d) ** 2)) / 4.0
        p_minus_c = max(p_minus - c_model, 1e-10)
        snr_inst = max((p_plus - p_minus) / p_minus_c, 0.1)

        if n >= BURN_IN:
            rot_rates.append(rr)
            snr_list.append(snr_inst)

    return float(np.mean(rot_rates)), float(10.0 * np.log10(np.mean(snr_list)))


def run_fixed_alpha(h_true, h_obs, alpha: float):
    h_sm = h_obs[0].copy().astype(cp.complex128)
    mse = []
    for n in range(1, h_true.shape[0]):
        inner = cp.dot(h_obs[n], cp.conj(h_sm))
        inner_abs = float(cp.abs(inner))
        rot = inner / inner_abs if inner_abs > 1e-30 else 1.0 + 0j
        h_sm += alpha * (h_obs[n] * cp.conj(rot) - h_sm)
        if n >= BURN_IN:
            mse.append(float(cp.mean(cp.abs(h_sm * rot - h_true[n]) ** 2)))
    return float(np.mean(mse))


def run_adaptive(h_true, h_obs, c_model: float, a1: float, a2: float, a3: float):
    h_main = h_obs[0].copy().astype(cp.complex128)
    alpha = 0.5
    rr_sm = 0.0
    snr_sm = 10.0
    prev_ang = 0.0
    mse = []

    for n in range(1, h_true.shape[0]):
        inner = cp.dot(h_obs[n], cp.conj(h_main))
        inner_abs = float(cp.abs(inner))
        rot = inner / inner_abs if inner_abs > 1e-30 else 1.0 + 0j

        h_main += alpha * (h_obs[n] * cp.conj(rot) - h_main)

        ang = float(cp.angle(rot))
        rr = abs(ang - prev_ang)
        if rr > np.pi:
            rr = 2.0 * np.pi - rr
        prev_ang = ang
        rr_sm = 0.95 * rr_sm + 0.05 * rr

        s = h_obs[n, 0::2] + h_obs[n, 1::2]
        d = h_obs[n, 0::2] - h_obs[n, 1::2]
        p_plus = float(cp.mean(cp.abs(s) ** 2)) / 4.0
        p_minus = float(cp.mean(cp.abs(d) ** 2)) / 4.0
        p_minus_c = max(p_minus - c_model, 1e-10)
        snr_inst = max((p_plus - p_minus) / p_minus_c, 0.1)
        snr_sm = 0.98 * snr_sm + 0.02 * (10.0 * np.log10(snr_inst))

        alpha = max(0.01, min(0.95, a1 * rr_sm + a2 * snr_sm + a3))

        if n >= BURN_IN:
            mse.append(float(cp.mean(cp.abs(h_main * rot - h_true[n]) ** 2)))

    return float(np.mean(mse))


def trial_seed(base: int, snr: int, speed: int, trial: int) -> int:
    return base + snr * 10000 + speed * 1000 + trial


def main():
    parser = argparse.ArgumentParser(description="CDL-C single re-test")
    parser.add_argument("--ray-dir", default="data_out/cdl_c_30kmh")
    parser.add_argument("--cal-trials", type=int, default=5)
    parser.add_argument("--gate-trials", type=int, default=5)
    parser.add_argument("--alpha-grid", type=int, default=80)
    parser.add_argument("--out-json", default="data_out/cdl_c_single_retest_result.json")
    args = parser.parse_args()

    ray_dir = Path(args.ray_dir)
    rays = load_cdl_c_rays(ray_dir)
    c_model = float(np.sum(rays["power"] * (1.0 - np.cos(2.0 * np.pi * SCS * rays["tau"]))) / 2.0)
    alpha_grid = cp.logspace(-2, 0, args.alpha_grid)

    print("=" * 72)
    print("CDL-C SINGLE RETEST (RNG independence fixed)")
    print("=" * 72)
    print(f"Frames={N_FRAMES}, Burn-in={BURN_IN}, N_SC={N_SC}, epsilon={EPSILON}")
    print(f"Calibration trials={args.cal_trials}, Gate trials={args.gate_trials}, alpha_grid={args.alpha_grid}")
    print(f"C_model={c_model:.6f}")
    print()

    t0 = time.time()
    cal_points = []
    for snr in CAL_SNRS:
        for speed in CAL_SPEEDS:
            rr_list = []
            snr_list = []
            alpha_list = []
            for t in range(args.cal_trials):
                seed = trial_seed(42000, snr, speed, t)
                rng = np.random.default_rng(seed)
                h_true = generate_channel(rays, speed, N_FRAMES, rng)
                h_obs = add_awgn(h_true, snr, rng)
                alpha_opt, _ = oracle_sweep(h_true, h_obs, alpha_grid)
                rr, snr_est = measure_features(h_obs, c_model)
                rr_list.append(rr)
                snr_list.append(snr_est)
                alpha_list.append(alpha_opt)
            cal_points.append((float(np.mean(rr_list)), float(np.mean(snr_list)), float(np.mean(alpha_list))))

    data = np.asarray(cal_points, dtype=np.float64)
    x = np.column_stack([data[:, 0], data[:, 1], np.ones(len(data))])
    y = data[:, 2]
    coeff, _, _, _ = np.linalg.lstsq(x, y, rcond=None)
    a1, a2, a3 = [float(v) for v in coeff]
    y_pred = x @ coeff
    r2 = float(1.0 - np.sum((y - y_pred) ** 2) / np.sum((y - np.mean(y)) ** 2))

    print(f"Fit: alpha = {a1:.6f}*rot_rate + {a2:.6f}*snr_db + {a3:.6f}")
    print(f"R^2 = {r2:.6f}")
    print()

    results = []
    pass_cnt = 0
    total = 0

    print(f"{'SNR':>4} {'Spd':>4} {'Oracle':>10} {'Adapt':>10} {'Ratio':>8} {'Gate':>6}")
    for snr in GATE_SNRS:
        for speed in GATE_SPEEDS:
            oracle_vals = []
            adapt_vals = []
            for t in range(args.gate_trials):
                seed = trial_seed(99000, snr, speed, t)
                rng = np.random.default_rng(seed)
                h_true = generate_channel(rays, speed, N_FRAMES, rng)
                h_obs = add_awgn(h_true, snr, rng)
                alpha_opt, _ = oracle_sweep(h_true, h_obs, alpha_grid)
                oracle_vals.append(run_fixed_alpha(h_true, h_obs, alpha_opt))
                adapt_vals.append(run_adaptive(h_true, h_obs, c_model, a1, a2, a3))

            oracle_mean = float(np.mean(oracle_vals))
            adapt_mean = float(np.mean(adapt_vals))
            ratio = adapt_mean / oracle_mean
            gate = ratio <= (1.0 + EPSILON)
            pass_cnt += 1 if gate else 0
            total += 1
            gate_str = "PASS" if gate else "FAIL"
            print(f"{snr:4d} {speed:4d} {oracle_mean:10.6f} {adapt_mean:10.6f} {ratio:8.4f} {gate_str:>6}")

            results.append(
                {
                    "snr_db": snr,
                    "speed_kmh": speed,
                    "oracle_mse": oracle_mean,
                    "adaptive_mse": adapt_mean,
                    "ratio": ratio,
                    "pass": bool(gate),
                }
            )

    elapsed = time.time() - t0
    print()
    print(f"CDL-C Gate Summary: {pass_cnt}/{total}")
    print(f"Elapsed: {elapsed:.1f}s")
    print("=" * 72)

    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "fit": {"a1": a1, "a2": a2, "a3": a3, "r2": r2},
                "gate_summary": {"pass": pass_cnt, "total": total, "epsilon": EPSILON},
                "gate_rows": results,
                "params": {
                    "n_frames": N_FRAMES,
                    "n_sc": N_SC,
                    "burn_in": BURN_IN,
                    "cal_trials": args.cal_trials,
                    "gate_trials": args.gate_trials,
                    "alpha_grid": args.alpha_grid,
                    "cal_snrs": CAL_SNRS,
                    "cal_speeds": CAL_SPEEDS,
                    "gate_snrs": GATE_SNRS,
                    "gate_speeds": GATE_SPEEDS,
                },
            },
            f,
            indent=2,
        )
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
