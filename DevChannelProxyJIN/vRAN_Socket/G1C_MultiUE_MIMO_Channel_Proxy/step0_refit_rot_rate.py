"""
Step 0: Re-fit rot_rate→α linear mapping using Reference EMA rot_rate.

Purpose:
  Phase 1 的拟合系数 (0.157, 0.446) 标定时 rot_rate 在 oracle 固定 α 下测。
  部署时 rot_rate 来自 Reference EMA（α_ref=0.02）。
  By construction 消除标定/部署 rot 源失配：
    用 Reference EMA 的 rot_rate 重跑 8-speed 表，无条件 re-fit (a, b)。

Output:
  - 8-speed rot_rate 表（Reference EMA 条件）
  - 新 (a, b) 系数 + R²
  - Oracle α_opt 交叉验证
  - JSON 结果文件

Usage:
  python3 step0_refit_rot_rate.py [--ray-dir data_out/cdl_c_30kmh]
"""
import argparse
import json
import time
import numpy as np
from pathlib import Path
from multisc_cdl_sim import (
    load_cdl_rays, generate_cdl_channel, add_awgn,
    oracle_sweep, measure_ref_ema_rot_rate, N_SC, BURN_IN
)

SPEEDS = [3, 5, 8, 10, 15, 20, 25, 30]
SNR_DB = 10
N_FRAMES = 2000
N_TRIALS = 10
ALPHA_REF = 0.02

KNOWN_ALPHA_OPT = {
    3: 0.416, 5: 0.524, 8: 0.574, 10: 0.616,
    15: 0.691, 20: 0.724, 25: 0.758, 30: 0.794,
}


def linear_fit(x, y):
    """Least-squares linear fit: y = a*x + b. Returns (a, b, R²)."""
    x, y = np.array(x), np.array(y)
    n = len(x)
    sx, sy = x.sum(), y.sum()
    sxx = (x * x).sum()
    sxy = (x * y).sum()
    denom = n * sxx - sx * sx
    a = (n * sxy - sx * sy) / denom
    b = (sy - a * sx) / n
    y_pred = a * x + b
    ss_res = ((y - y_pred) ** 2).sum()
    ss_tot = ((y - y.mean()) ** 2).sum()
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return float(a), float(b), float(r2)


def main():
    parser = argparse.ArgumentParser(description="Step 0: re-fit rot_rate→α")
    parser.add_argument("--ray-dir", default="data_out/cdl_c_30kmh")
    parser.add_argument("--n-trials", type=int, default=N_TRIALS)
    parser.add_argument("--verify-oracle", action="store_true",
                        help="Cross-check oracle α_opt (slower)")
    args = parser.parse_args()

    rays = load_cdl_rays(args.ray_dir)
    alpha_grid = np.logspace(-2, 0, 150)

    print("=" * 72)
    print("Step 0: Re-fit rot_rate→α mapping (Reference EMA, α_ref=0.02)")
    print("=" * 72)
    print(f"  CDL rays: {args.ray_dir}")
    print(f"  Speeds: {SPEEDS} km/h")
    print(f"  SNR: {SNR_DB} dB, N_SC: {N_SC}, Frames: {N_FRAMES}")
    print(f"  Trials: {args.n_trials}, Burn-in: {BURN_IN}")
    print(f"  α_ref: {ALPHA_REF}")
    if args.verify_oracle:
        print(f"  Oracle verification: ON (α grid {len(alpha_grid)} points)")
    print()

    results = []
    t_total = time.time()

    for speed in SPEEDS:
        t0 = time.time()
        rot_rate_trials = []
        oracle_alpha_trials = []
        oracle_mse_trials = []

        for trial in range(args.n_trials):
            rng = np.random.default_rng(42000 + speed * 1000 + trial)
            H_true = generate_cdl_channel(rays, speed, N_FRAMES, rng=rng)
            H_obs = add_awgn(H_true, SNR_DB, rng)

            rr = measure_ref_ema_rot_rate(H_obs, alpha_ref=ALPHA_REF)
            rot_rate_trials.append(rr)

            if args.verify_oracle:
                _, a_opt, o_mse = oracle_sweep(H_true, H_obs, alpha_grid)
                oracle_alpha_trials.append(a_opt)
                oracle_mse_trials.append(o_mse)

        rot_rate_mean = float(np.mean(rot_rate_trials))
        rot_rate_std = float(np.std(rot_rate_trials))
        known_aopt = KNOWN_ALPHA_OPT[speed]

        row = {
            "speed_kmh": speed,
            "rot_rate_ref_ema": rot_rate_mean,
            "rot_rate_std": rot_rate_std,
            "alpha_opt_known": known_aopt,
        }

        status = f"  {speed:>3d} km/h: rot_rate={rot_rate_mean:.4f}±{rot_rate_std:.4f}"
        status += f"  α_opt(known)={known_aopt:.3f}"

        if args.verify_oracle and oracle_alpha_trials:
            v_alpha = float(np.mean(oracle_alpha_trials))
            v_mse = float(np.mean(oracle_mse_trials))
            dev = abs(v_alpha - known_aopt) / known_aopt * 100
            row["alpha_opt_verified"] = v_alpha
            row["oracle_mse_verified"] = v_mse
            row["deviation_pct"] = dev
            status += f"  α_opt(verified)={v_alpha:.3f} (Δ={dev:.1f}%)"

        status += f"  [{time.time()-t0:.1f}s]"
        print(status)
        results.append(row)

    # ── Linear fit ──
    rot_rates = [r["rot_rate_ref_ema"] for r in results]
    alpha_opts = [r["alpha_opt_known"] for r in results]
    a, b, r2 = linear_fit(rot_rates, alpha_opts)

    print()
    print("=" * 72)
    print("Re-fit Result")
    print("=" * 72)
    print(f"  α = {a:.4f} · rot_rate + {b:.4f}")
    print(f"  R² = {r2:.4f}")
    print()

    print("  Residual analysis:")
    for r in results:
        rr = r["rot_rate_ref_ema"]
        alpha_pred = a * rr + b
        alpha_true = r["alpha_opt_known"]
        residual_pct = (alpha_pred - alpha_true) / alpha_true * 100
        direction = "over" if residual_pct > 0 else "under"
        print(f"    {r['speed_kmh']:>3d} km/h: pred={alpha_pred:.4f} "
              f"true={alpha_true:.3f}  {residual_pct:+.1f}% ({direction}-predict)")

    alpha_pred_3 = a * rot_rates[0] + b
    residual_3 = (alpha_pred_3 - KNOWN_ALPHA_OPT[3]) / KNOWN_ALPHA_OPT[3] * 100
    print(f"\n  ⚠ 3km/h residual: {residual_3:+.1f}%")
    if residual_3 > 0:
        print("    → 过度预测 → 欠平滑方向 (失败风险方向)")
    else:
        print("    → 欠预测 → 过度平滑方向 (安全方向)")

    print(f"\n  旧系数 (oracle rot): a=0.157, b=0.446, R²=0.91")
    print(f"  新系数 (ref EMA rot): a={a:.4f}, b={b:.4f}, R²={r2:.4f}")
    print(f"\n  Total time: {time.time()-t_total:.1f}s")

    # ── Save ──
    out_dir = Path(__file__).parent / "data_out"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "step0_refit_results.json"
    save_data = {
        "fit": {"a": a, "b": b, "r2": r2},
        "fit_old": {"a": 0.157, "b": 0.446, "r2": 0.91},
        "speeds": results,
        "params": {
            "snr_dB": SNR_DB, "n_sc": N_SC, "n_frames": N_FRAMES,
            "n_trials": args.n_trials, "alpha_ref": ALPHA_REF,
            "burn_in": BURN_IN, "ray_dir": args.ray_dir,
        },
    }
    with open(out_file, "w") as f:
        json.dump(save_data, f, indent=2)
    print(f"\n  Results saved to: {out_file}")

    return a, b, r2


if __name__ == "__main__":
    main()
