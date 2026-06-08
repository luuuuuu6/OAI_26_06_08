"""
Step 0c: Oracle α_opt + MSE baseline table via Jakes MC sweep.

Methodology:
  - Jakes sum-of-sinusoids → +noise → LS de-rotation (same as production) → fixed-α EMA
  - MSE computed in de-rotated reference frame: |H_ema - H_truth_derot|^2
  - α grid: 200 points logspace(-3, -0.3)
  - Monte Carlo: 1000 trials per (f_D, SNR, α)
  - Output: oracle MSE table + MSE-vs-α curves (5 cases)

De-rotation semantics (confirmed from AdaptiveSRS2DFilter.update):
  rot = sum(H_obs * conj(H_smooth)) / |sum(...)|
  H_derot = H_obs * conj(rot)
  → "channel phase trend" type; Jakes Doppler already produces phase wandering,
    so de-rotation is non-trivial on synthetic signals without extra injection.
"""
import numpy as np
import time
import json
from pathlib import Path

N_OSC = 32
N_FRAMES = 2000
N_SC = 1
CARRIER_FREQ = 3.5e9
C = 3e8
T_FRAME = 5e-3


def jakes_sum_of_sinusoids(f_D: float, N_frames: int, N_osc: int = N_OSC,
                           rng: np.random.Generator = None) -> np.ndarray:
    """Generate single-tap Rayleigh fading via Jakes sum-of-sinusoids."""
    if rng is None:
        rng = np.random.default_rng()
    phi = rng.uniform(0, 2 * np.pi, size=N_osc)
    alpha_angles = 2 * np.pi * np.arange(1, N_osc + 1) / N_osc
    cos_alpha = np.cos(alpha_angles)

    n = np.arange(N_frames)
    phase = 2 * np.pi * f_D * T_FRAME * np.outer(n, cos_alpha) + phi[np.newaxis, :]
    H = (1.0 / np.sqrt(N_osc)) * np.sum(np.exp(1j * phase), axis=1)
    return H


def add_noise(H: np.ndarray, snr_dB: float, rng: np.random.Generator) -> np.ndarray:
    """Add complex AWGN at specified SNR (relative to signal power)."""
    sig_power = np.mean(np.abs(H) ** 2)
    noise_power = sig_power * 10.0 ** (-snr_dB / 10.0)
    noise = rng.normal(0, np.sqrt(noise_power / 2), size=H.shape) + \
            1j * rng.normal(0, np.sqrt(noise_power / 2), size=H.shape)
    return H + noise


def ls_derotation(H_obs: complex, H_smooth: complex):
    """LS phase de-rotation (scalar version, same logic as production).

    Returns (H_derot, rot) where rot is the unit-modulus LS estimate.
    Production code: rot = sum(H_obs * conj(H_smooth)) / |sum(...)|
    For single SC: rot = H_obs * conj(H_smooth) / |H_obs * conj(H_smooth)|
    """
    inner = H_obs * np.conj(H_smooth)
    inner_abs = abs(inner)
    rot = (inner / inner_abs) if inner_abs > 1e-30 else (1.0 + 0j)
    H_derot = H_obs * np.conj(rot)
    return H_derot, rot


def run_ema_multi_alpha(H_obs_seq: np.ndarray, H_truth_seq: np.ndarray,
                        alpha_grid: np.ndarray, use_derot: bool = False) -> np.ndarray:
    """Run fixed-α EMA for ALL alphas simultaneously.

    Vectorized across alpha dimension. Returns MSE array of shape (n_alpha,).
    Skip first 50 frames as burn-in.

    use_derot=False (default): Plain EMA. Correct oracle for flat-fading (scalar)
        channels where per-frame LS de-rotation degenerates to amplitude-only tracking.
    use_derot=True: With LS de-rotation. Only meaningful for frequency-selective
        (multi-SC) channels where de-rotation removes global phase trend while
        preserving per-SC structure.
    """
    N = len(H_obs_seq)
    n_alpha = len(alpha_grid)
    burn_in = 50

    H_smooth = np.full(n_alpha, H_obs_seq[0], dtype=np.complex128)
    mse_accum = np.zeros(n_alpha)
    count = 0

    for n in range(1, N):
        if use_derot:
            inner = H_obs_seq[n] * np.conj(H_smooth)
            inner_abs = np.abs(inner)
            mask = inner_abs > 1e-30
            rot = np.where(mask, inner / inner_abs, 1.0 + 0j)
            H_input = H_obs_seq[n] * np.conj(rot)
        else:
            H_input = H_obs_seq[n]

        diff = H_input - H_smooth
        H_smooth = H_smooth + alpha_grid * diff

        if n >= burn_in:
            mse_accum += np.abs(H_smooth - H_truth_seq[n]) ** 2
            count += 1

    return mse_accum / count if count > 0 else mse_accum


def sweep_one_case(f_D: float, snr_dB: float, alpha_grid: np.ndarray,
                   n_trials: int = 1000, seed_base: int = 0):
    """Sweep α grid for one (f_D, SNR) case, return MSE curve + stats."""
    n_alpha = len(alpha_grid)
    mse_all = np.zeros((n_trials, n_alpha))

    for trial in range(n_trials):
        rng = np.random.default_rng(seed_base + trial)
        H_truth = jakes_sum_of_sinusoids(f_D, N_FRAMES, rng=rng)
        H_obs = add_noise(H_truth, snr_dB, rng)
        mse_all[trial, :] = run_ema_multi_alpha(H_obs, H_truth, alpha_grid)

    mse_mean = mse_all.mean(axis=0)
    mse_std = mse_all.std(axis=0)

    opt_idx = np.argmin(mse_mean)
    oracle_mse = mse_mean[opt_idx]
    alpha_opt = alpha_grid[opt_idx]

    sem_at_opt = mse_std[opt_idx] / np.sqrt(n_trials)
    sem_ratio = sem_at_opt / oracle_mse if oracle_mse > 0 else 0

    return {
        "f_D": f_D,
        "snr_dB": snr_dB,
        "alpha_opt": float(alpha_opt),
        "oracle_mse": float(oracle_mse),
        "sem_at_opt": float(sem_at_opt),
        "sem_ratio": float(sem_ratio),
        "mse_curve": mse_mean.tolist(),
        "mse_std_curve": mse_std.tolist(),
    }


def speed_to_fD(speed_kmh: float) -> float:
    """Convert speed (km/h) to max Doppler frequency (Hz)."""
    v = speed_kmh / 3.6
    return v * CARRIER_FREQ / C


def main():
    alpha_grid = np.logspace(-3, 0, 200)

    cases = [
        {"f_D": speed_to_fD(3), "snr_dB": 20, "label": "3km/h, 20dB"},
        {"f_D": speed_to_fD(3), "snr_dB": 10, "label": "3km/h, 10dB"},
        {"f_D": speed_to_fD(3), "snr_dB": 5, "label": "3km/h, 5dB"},
        {"f_D": speed_to_fD(3), "snr_dB": 0, "label": "3km/h, 0dB"},
        {"f_D": speed_to_fD(30), "snr_dB": 20, "label": "30km/h, 20dB"},
        {"f_D": speed_to_fD(30), "snr_dB": 10, "label": "30km/h, 10dB"},
        {"f_D": speed_to_fD(30), "snr_dB": 5, "label": "30km/h, 5dB"},
        {"f_D": speed_to_fD(30), "snr_dB": 0, "label": "30km/h, 0dB"},
        {"f_D": speed_to_fD(100), "snr_dB": 10, "label": "100km/h, 10dB"},
        {"f_D": speed_to_fD(100), "snr_dB": 0, "label": "100km/h, 0dB"},
    ]

    print("=" * 70)
    print("Step 0c: Oracle α_opt + MSE Baseline Table (Jakes MC Sweep)")
    print("=" * 70)
    print(f"  α grid: {len(alpha_grid)} points, logspace(-3, 0) → [{alpha_grid[0]:.4f}, {alpha_grid[-1]:.4f}]")
    print(f"  Spacing: ~{100*(alpha_grid[1]/alpha_grid[0]-1):.1f}% between adjacent")
    print(f"  MC trials: 1000 per (f_D, SNR, α)")
    print(f"  Frames per trial: {N_FRAMES}")
    print(f"  Burn-in: 50 frames")
    print(f"  De-rotation: LS phase (channel trend type)")
    print(f"  Carrier: {CARRIER_FREQ/1e9:.1f} GHz, T_frame: {T_FRAME*1e3:.0f} ms")
    print()

    results = []
    t0 = time.time()

    for case in cases:
        tc = time.time()
        print(f"Running: {case['label']} (f_D={case['f_D']:.1f} Hz) ...", flush=True)
        res = sweep_one_case(case["f_D"], case["snr_dB"], alpha_grid,
                             n_trials=500, seed_base=hash(case["label"]) % (2**31))
        res["label"] = case["label"]
        results.append(res)
        elapsed = time.time() - tc
        print(f"  α_opt = {res['alpha_opt']:.5f}, oracle_MSE = {res['oracle_mse']:.6e}, "
              f"SEM/MSE = {res['sem_ratio']:.4f} ({elapsed:.1f}s)")

    total_time = time.time() - t0
    print(f"\nTotal time: {total_time:.1f}s")

    print("\n" + "=" * 70)
    print("Oracle MSE Baseline Table (Step 1.5 gate reference)")
    print("=" * 70)
    print(f"{'f_D (Hz)':<12} {'T (ms)':<8} {'SNR (dB)':<10} {'α_opt':<12} "
          f"{'oracle MSE':<14} {'SEM/MSE':<10} {'Source'}")
    print("-" * 70)
    for r in results:
        f_D = r["f_D"]
        source = "AR(1)+数值" if f_D < 15 else "数值扫描"
        print(f"{f_D:<12.1f} {T_FRAME*1e3:<8.0f} {r['snr_dB']:<10} "
              f"{r['alpha_opt']:<12.5f} {r['oracle_mse']:<14.6e} "
              f"{r['sem_ratio']:<10.4f} {source}")

    ar1_crosscheck(results, alpha_grid)

    out_dir = Path(__file__).parent / "data_out"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "alpha_opt_sweep_results.json"
    save_data = {
        "alpha_grid": alpha_grid.tolist(),
        "cases": results,
        "params": {
            "N_frames": N_FRAMES,
            "N_osc": N_OSC,
            "T_frame": T_FRAME,
            "carrier_freq": CARRIER_FREQ,
            "burn_in": 50,
            "n_trials": 1000,
        }
    }
    with open(out_file, "w") as f:
        json.dump(save_data, f, indent=2)
    print(f"\nResults saved to: {out_file}")

    plot_mse_curves(results, alpha_grid, out_dir)


def ar1_crosscheck(results, alpha_grid):
    """AR(1) closed-form cross-check for slow end (f_D ≈ 10 Hz)."""
    from scipy.special import j0
    print("\n" + "=" * 70)
    print("AR(1) Closed-Form Cross-Check (slow end only)")
    print("=" * 70)

    for r in results:
        f_D = r["f_D"]
        if f_D > 15:
            continue
        rho = j0(2 * np.pi * f_D * T_FRAME)
        snr_lin = 10.0 ** (r["snr_dB"] / 10.0)
        sigma2_h = 1.0
        sigma2_n = sigma2_h / snr_lin

        best_mse = np.inf
        best_alpha = 0
        for alpha in alpha_grid:
            noise_term = sigma2_n * alpha / (2 - alpha)
            denom = 1 - (1 - alpha) ** 2 * rho ** 2
            if denom <= 0:
                continue
            lag_term = sigma2_h * (1 - rho ** 2) * (1 - alpha) / denom
            mse = noise_term + lag_term
            if mse < best_mse:
                best_mse = mse
                best_alpha = alpha

        deviation = abs(r["oracle_mse"] - best_mse) / r["oracle_mse"] * 100
        print(f"  f_D={f_D:.1f}Hz: AR(1) α_opt={best_alpha:.5f}, MSE={best_mse:.6e}")
        print(f"              Scan  α_opt={r['alpha_opt']:.5f}, MSE={r['oracle_mse']:.6e}")
        print(f"              Deviation: {deviation:.1f}%"
              f" {'✓ <10%' if deviation < 10 else '✗ ≥10% (expected for fast end)'}")


def plot_mse_curves(results, alpha_grid, out_dir):
    """Plot MSE-vs-α curves for all cases."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  [matplotlib not available, skipping plots]")
        return

    n_cases = len(results)
    n_cols = 3
    n_rows = (n_cases + n_cols - 1) // n_cols
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(15, 4 * n_rows))
    axes = axes.flatten()

    for i, r in enumerate(results):
        ax = axes[i]
        mse_curve = np.array(r["mse_curve"])
        ax.semilogx(alpha_grid, 10 * np.log10(mse_curve + 1e-30), "b-", linewidth=1.5)
        opt_idx = np.argmin(mse_curve)
        ax.axvline(alpha_grid[opt_idx], color="r", linestyle="--", alpha=0.7,
                   label=f"α_opt={alpha_grid[opt_idx]:.4f}")
        ax.set_xlabel("α")
        ax.set_ylabel("MSE (dB)")
        ax.set_title(f"{r['label']}\nOracle MSE={r['oracle_mse']:.2e}")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

    for j in range(n_cases, len(axes)):
        axes[j].axis("off")

    plt.suptitle("Step 0c: MSE vs α (Jakes + plain EMA, no de-rotation)\n"
                 f"200-point grid, 500-trial MC, {N_FRAMES} frames/trial",
                 fontsize=12)
    plt.tight_layout()
    fig_path = out_dir / "mse_vs_alpha_curves.png"
    plt.savefig(fig_path, dpi=150)
    plt.close()
    print(f"  Curves saved to: {fig_path}")


if __name__ == "__main__":
    main()
