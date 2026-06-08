"""
Search optimal CDL model combination for linear adaptive-alpha mapping.

Pipeline:
1) Stage-1: exhaustive subset search over A/B/C/D/E with lightweight trials.
2) Stage-2: refine top-K subsets with higher trials to reduce Monte-Carlo variance.

All simulations enforce RNG-independence rules:
- Complex AWGN real/imag are drawn sequentially from ONE RNG object.
- Channel randomness and noise randomness are generated sequentially from ONE RNG
  object per trial (no channel/noise seed re-init coupling).

Usage:
  python3 cdl_combo_search.py
  python3 cdl_combo_search.py --stage1-cal-trials 1 --stage1-gate-trials 1 --refine-top-k 5
"""

import argparse
import itertools
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

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

MODEL_TO_DIR = {
    "A": "cdl_a",
    "B": "cdl_b",
    "C": "cdl_c_30kmh",
    "D": "cdl_d",
    "E": "cdl_e",
}

CAL_SEED_BASE = 42000
GATE_SEED_BASE = 99000
MODEL_SEED_STEP = 1_000_000


@dataclass
class CalPoint:
    model: str
    snr_db: int
    speed_kmh: int
    rr: float
    snr_est_db: float
    alpha_opt: float


@dataclass
class GateOracle:
    model: str
    snr_db: int
    speed_kmh: int
    trial: int
    alpha_opt: float
    oracle_mse: float


def load_rays(base_dir: Path) -> Dict[str, Dict[str, np.ndarray]]:
    rays_by_model: Dict[str, Dict[str, np.ndarray]] = {}
    for model, subdir in MODEL_TO_DIR.items():
        d = base_dir / subdir
        if not d.exists():
            raise FileNotFoundError(f"Missing ray directory for CDL-{model}: {d}")
        rays = {
            "power": np.load(d / "power_rays_for_ChannelBlock.npy").flatten().astype(np.float64),
            "tau": np.load(d / "tau_rays_for_ChannelBlock.npy").flatten().astype(np.float64),
            "phi_r": np.load(d / "phi_r_rays_for_ChannelBlock.npy").flatten().astype(np.float64),
            "theta_r": np.load(d / "theta_r_rays_for_ChannelBlock.npy").flatten().astype(np.float64),
        }
        p_sum = rays["power"].sum()
        if p_sum <= 1e-30:
            raise ValueError(f"Zero power in CDL-{model}")
        rays["power"] /= p_sum
        rays_by_model[model] = rays
    return rays_by_model


def compute_c_model(rays: Dict[str, np.ndarray]) -> float:
    return float(np.sum(rays["power"] * (1.0 - np.cos(2.0 * np.pi * SCS * rays["tau"]))) / 2.0)


def speed_to_fd(speed_kmh: float) -> float:
    return (speed_kmh / 3.6) * FC_HZ / LIGHT


def generate_channel(rays: Dict[str, np.ndarray], speed_kmh: float, rng: np.random.Generator):
    f_d_max = speed_to_fd(speed_kmh)
    power = cp.asarray(rays["power"])
    tau = cp.asarray(rays["tau"])
    phi_r = cp.asarray(rays["phi_r"])
    theta_r = cp.asarray(rays["theta_r"])

    doppler = f_d_max * cp.sin(theta_r) * cp.cos(phi_r)
    psi = cp.asarray(rng.uniform(0.0, 2.0 * np.pi, power.size))

    n_arr = cp.arange(N_FRAMES, dtype=cp.float64)
    k_arr = cp.arange(N_SC, dtype=cp.float64)
    time_phase = 2.0 * cp.pi * cp.outer(n_arr * T_FRAME, doppler) + psi[cp.newaxis, :]
    time_comp = cp.sqrt(power[cp.newaxis, :]) * cp.exp(1j * time_phase)
    freq_steer = cp.exp(1j * (-2.0 * cp.pi * cp.outer(k_arr * SCS, tau)))
    return time_comp @ freq_steer.T


def add_awgn(h_true, snr_db: float, rng: np.random.Generator):
    sig_pow = float(cp.mean(cp.abs(h_true) ** 2))
    noise_var = sig_pow * (10.0 ** (-snr_db / 10.0))
    noise_std = np.sqrt(noise_var / 2.0)
    noise_re = rng.normal(0.0, noise_std, h_true.shape)
    noise_im = rng.normal(0.0, noise_std, h_true.shape)
    return h_true + cp.asarray(noise_re + 1j * noise_im)


def oracle_sweep(h_true, h_obs, alpha_grid):
    h_sm = cp.tile(h_obs[0], (alpha_grid.size, 1)).astype(cp.complex128)
    mse_acc = cp.zeros(alpha_grid.size, dtype=cp.float64)
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
    prev_ang = 0.0
    rr_list: List[float] = []
    snr_list: List[float] = []

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
            rr_list.append(rr)
            snr_list.append(snr_inst)

    return float(np.mean(rr_list)), float(10.0 * np.log10(np.mean(snr_list)))


def run_fixed_alpha(h_true, h_obs, alpha: float):
    h_sm = h_obs[0].copy().astype(cp.complex128)
    mse: List[float] = []
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
    mse: List[float] = []

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


def seed_for(base: int, model_idx: int, snr: int, speed: int, trial: int):
    return base + model_idx * MODEL_SEED_STEP + snr * 10000 + speed * 1000 + trial


def all_subsets(models: Sequence[str], require_model: str = "") -> List[Tuple[str, ...]]:
    out: List[Tuple[str, ...]] = []
    for size in range(1, len(models) + 1):
        for subset in itertools.combinations(models, size):
            if require_model and require_model not in subset:
                continue
            out.append(subset)
    return out


def fit_coeff(cal_points: Iterable[CalPoint], subset: Tuple[str, ...]):
    subset_set = set(subset)
    rows = [p for p in cal_points if p.model in subset_set]
    x = np.column_stack(
        [
            np.array([p.rr for p in rows], dtype=np.float64),
            np.array([p.snr_est_db for p in rows], dtype=np.float64),
            np.ones(len(rows), dtype=np.float64),
        ]
    )
    y = np.array([p.alpha_opt for p in rows], dtype=np.float64)
    coeff, _, _, _ = np.linalg.lstsq(x, y, rcond=None)
    y_pred = x @ coeff
    denom = np.sum((y - np.mean(y)) ** 2)
    r2 = 0.0 if denom <= 1e-12 else float(1.0 - np.sum((y - y_pred) ** 2) / denom)
    return float(coeff[0]), float(coeff[1]), float(coeff[2]), r2


def precompute_calibration(
    rays_by_model: Dict[str, Dict[str, np.ndarray]],
    c_model_by_model: Dict[str, float],
    cal_trials: int,
    alpha_grid,
):
    points: List[CalPoint] = []
    models = list(MODEL_TO_DIR.keys())
    total = len(models) * len(CAL_SNRS) * len(CAL_SPEEDS) * cal_trials
    done = 0
    print(f"[Cal] precompute start: {total} runs")
    for model_idx, model in enumerate(models):
        rays = rays_by_model[model]
        c_model = c_model_by_model[model]
        for snr in CAL_SNRS:
            for speed in CAL_SPEEDS:
                rr_acc: List[float] = []
                snr_acc: List[float] = []
                alpha_acc: List[float] = []
                for trial in range(cal_trials):
                    seed = seed_for(CAL_SEED_BASE, model_idx, snr, speed, trial)
                    rng = np.random.default_rng(seed)
                    h_true = generate_channel(rays, speed, rng)
                    h_obs = add_awgn(h_true, snr, rng)
                    alpha_opt, _ = oracle_sweep(h_true, h_obs, alpha_grid)
                    rr, snr_est = measure_features(h_obs, c_model)
                    rr_acc.append(rr)
                    snr_acc.append(snr_est)
                    alpha_acc.append(alpha_opt)
                    done += 1
                points.append(
                    CalPoint(
                        model=model,
                        snr_db=snr,
                        speed_kmh=speed,
                        rr=float(np.mean(rr_acc)),
                        snr_est_db=float(np.mean(snr_acc)),
                        alpha_opt=float(np.mean(alpha_acc)),
                    )
                )
                print(f"[Cal] {done}/{total} model={model} snr={snr} speed={speed}")
    return points


def precompute_gate_oracle(
    rays_by_model: Dict[str, Dict[str, np.ndarray]],
    gate_trials: int,
    alpha_grid,
):
    out: List[GateOracle] = []
    models = list(MODEL_TO_DIR.keys())
    total = len(models) * len(GATE_SNRS) * len(GATE_SPEEDS) * gate_trials
    done = 0
    print(f"[GateOracle] precompute start: {total} runs")
    for model_idx, model in enumerate(models):
        rays = rays_by_model[model]
        for snr in GATE_SNRS:
            for speed in GATE_SPEEDS:
                for trial in range(gate_trials):
                    seed = seed_for(GATE_SEED_BASE, model_idx, snr, speed, trial)
                    rng = np.random.default_rng(seed)
                    h_true = generate_channel(rays, speed, rng)
                    h_obs = add_awgn(h_true, snr, rng)
                    alpha_opt, _ = oracle_sweep(h_true, h_obs, alpha_grid)
                    oracle_mse = run_fixed_alpha(h_true, h_obs, alpha_opt)
                    out.append(
                        GateOracle(
                            model=model,
                            snr_db=snr,
                            speed_kmh=speed,
                            trial=trial,
                            alpha_opt=alpha_opt,
                            oracle_mse=oracle_mse,
                        )
                    )
                    done += 1
                print(f"[GateOracle] {done}/{total} model={model} snr={snr} speed={speed}")
    return out


def evaluate_subset(
    subset: Tuple[str, ...],
    coeff: Tuple[float, float, float],
    rays_by_model: Dict[str, Dict[str, np.ndarray]],
    c_model_by_model: Dict[str, float],
    gate_oracle_rows: Sequence[GateOracle],
):
    a1, a2, a3 = coeff
    by_key: Dict[Tuple[str, int, int], List[GateOracle]] = {}
    for row in gate_oracle_rows:
        by_key.setdefault((row.model, row.snr_db, row.speed_kmh), []).append(row)

    gate_rows = []
    pass_cnt = 0
    deployment_pass = 0
    deployment_total = 0
    worst_ratio = 0.0

    for model in MODEL_TO_DIR.keys():
        rays = rays_by_model[model]
        c_model = c_model_by_model[model]
        for snr in GATE_SNRS:
            for speed in GATE_SPEEDS:
                rows = by_key[(model, snr, speed)]
                oracle_vals: List[float] = []
                adapt_vals: List[float] = []
                for row in rows:
                    model_idx = list(MODEL_TO_DIR.keys()).index(model)
                    seed = seed_for(GATE_SEED_BASE, model_idx, snr, speed, row.trial)
                    rng = np.random.default_rng(seed)
                    h_true = generate_channel(rays, speed, rng)
                    h_obs = add_awgn(h_true, snr, rng)
                    adapt_mse = run_adaptive(h_true, h_obs, c_model, a1, a2, a3)
                    oracle_vals.append(row.oracle_mse)
                    adapt_vals.append(adapt_mse)

                oracle_mean = float(np.mean(oracle_vals))
                adapt_mean = float(np.mean(adapt_vals))
                ratio = adapt_mean / oracle_mean
                passed = ratio <= (1.0 + EPSILON)
                pass_cnt += 1 if passed else 0
                if speed == 3:
                    deployment_total += 1
                    deployment_pass += 1 if passed else 0
                worst_ratio = max(worst_ratio, ratio)
                gate_rows.append(
                    {
                        "model": model,
                        "snr_db": snr,
                        "speed_kmh": speed,
                        "oracle_mse": oracle_mean,
                        "adaptive_mse": adapt_mean,
                        "ratio": ratio,
                        "pass": passed,
                    }
                )
    return {
        "subset": list(subset),
        "fit": {"a1": a1, "a2": a2, "a3": a3},
        "gate_pass": pass_cnt,
        "gate_total": len(MODEL_TO_DIR) * len(GATE_SNRS) * len(GATE_SPEEDS),
        "deployment_pass": deployment_pass,
        "deployment_total": deployment_total,
        "worst_ratio": worst_ratio,
        "gate_rows": gate_rows,
    }


def rank_key(item):
    return (
        item["gate_pass"],
        item["deployment_pass"],
        -item["worst_ratio"],
        item["fit"]["r2"],
    )


def run_stage(
    stage_name: str,
    subsets: Sequence[Tuple[str, ...]],
    rays_by_model: Dict[str, Dict[str, np.ndarray]],
    c_model_by_model: Dict[str, float],
    cal_trials: int,
    gate_trials: int,
    alpha_grid_size: int,
):
    print(f"\n========== {stage_name} ==========")
    alpha_grid = cp.logspace(-2, 0, alpha_grid_size)
    cal_points = precompute_calibration(rays_by_model, c_model_by_model, cal_trials, alpha_grid)
    gate_oracle_rows = precompute_gate_oracle(rays_by_model, gate_trials, alpha_grid)

    results = []
    for idx, subset in enumerate(subsets, start=1):
        a1, a2, a3, r2 = fit_coeff(cal_points, subset)
        eval_res = evaluate_subset(
            subset=subset,
            coeff=(a1, a2, a3),
            rays_by_model=rays_by_model,
            c_model_by_model=c_model_by_model,
            gate_oracle_rows=gate_oracle_rows,
        )
        eval_res["fit"]["r2"] = r2
        results.append(eval_res)
        print(
            f"[{stage_name}] {idx}/{len(subsets)} subset={'+'.join(subset)} "
            f"gate={eval_res['gate_pass']}/{eval_res['gate_total']} "
            f"dep={eval_res['deployment_pass']}/{eval_res['deployment_total']} "
            f"worst={eval_res['worst_ratio']:.3f} r2={r2:.3f}"
        )
    results.sort(key=rank_key, reverse=True)
    return results


def main():
    parser = argparse.ArgumentParser(description="CDL combo search")
    parser.add_argument("--data-out-dir", default="data_out")
    parser.add_argument("--require-model", default="C", help="Only evaluate subsets containing this model; empty=all")
    parser.add_argument("--stage1-cal-trials", type=int, default=1)
    parser.add_argument("--stage1-gate-trials", type=int, default=1)
    parser.add_argument("--stage1-grid", type=int, default=60)
    parser.add_argument("--refine-top-k", type=int, default=5)
    parser.add_argument("--stage2-cal-trials", type=int, default=3)
    parser.add_argument("--stage2-gate-trials", type=int, default=3)
    parser.add_argument("--stage2-grid", type=int, default=80)
    parser.add_argument("--output-json", default="data_out/cdl_combo_search_result.json")
    args = parser.parse_args()

    t0 = time.time()
    base_dir = Path(args.data_out_dir)
    rays_by_model = load_rays(base_dir)
    c_model_by_model = {m: compute_c_model(r) for m, r in rays_by_model.items()}
    subsets = all_subsets(list(MODEL_TO_DIR.keys()), require_model=args.require_model)
    print(f"Subset count: {len(subsets)} (require-model={args.require_model or 'none'})")

    stage1_res = run_stage(
        stage_name="Stage-1",
        subsets=subsets,
        rays_by_model=rays_by_model,
        c_model_by_model=c_model_by_model,
        cal_trials=args.stage1_cal_trials,
        gate_trials=args.stage1_gate_trials,
        alpha_grid_size=args.stage1_grid,
    )

    top_subsets = [tuple(item["subset"]) for item in stage1_res[: args.refine_top_k]]
    stage2_res = run_stage(
        stage_name="Stage-2",
        subsets=top_subsets,
        rays_by_model=rays_by_model,
        c_model_by_model=c_model_by_model,
        cal_trials=args.stage2_cal_trials,
        gate_trials=args.stage2_gate_trials,
        alpha_grid_size=args.stage2_grid,
    )

    best = stage2_res[0]
    elapsed = time.time() - t0
    print("\n========== FINAL ==========")
    print(
        f"Best subset: {'+'.join(best['subset'])} | "
        f"gate={best['gate_pass']}/{best['gate_total']} | "
        f"deployment={best['deployment_pass']}/{best['deployment_total']} | "
        f"worst={best['worst_ratio']:.4f} | r2={best['fit']['r2']:.4f}"
    )
    print(f"Elapsed: {elapsed:.1f}s")

    out_path = Path(args.output_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "config": {
                    "epsilon": EPSILON,
                    "n_frames": N_FRAMES,
                    "burn_in": BURN_IN,
                    "stage1": {
                        "cal_trials": args.stage1_cal_trials,
                        "gate_trials": args.stage1_gate_trials,
                        "alpha_grid": args.stage1_grid,
                    },
                    "stage2": {
                        "cal_trials": args.stage2_cal_trials,
                        "gate_trials": args.stage2_gate_trials,
                        "alpha_grid": args.stage2_grid,
                        "refine_top_k": args.refine_top_k,
                    },
                    "require_model": args.require_model,
                },
                "c_model_by_model": c_model_by_model,
                "stage1_results": stage1_res,
                "stage2_results": stage2_res,
                "best": best,
                "elapsed_sec": elapsed,
            },
            f,
            indent=2,
        )
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
