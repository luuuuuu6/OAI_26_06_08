"""
Multi-SC CDL Sweep Harness (true-frame MSE).

Lyapunov-validated EMA core (±0.03%), extended to multi-SC with de-rotation.
De-rotation verified: Test C (primitive, 10^-16 precision), Test A (identity, 0.87%).

Usage:
  from multisc_sweep_harness import generate_cdl_channel, sweep_true_frame, load_cdl_params

  tau, power, aoa = load_cdl_params('data_out/cdl_c_30kmh')
  H_true = generate_cdl_channel(tau, power, aoa, f_D, N_FRAMES, N_SC, rng)
  alpha_opt, oracle_mse, sem, curve = sweep_true_frame(H_true, snr_dB)
"""
import numpy as np

CARRIER = 3.5e9
C_LIGHT = 3e8
T_FRAME = 5e-3
DELTA_F = 30e3
N_SC = 128
N_FRAMES = 3000
BURN_IN = 200
N_TRIALS = 80
ALPHA_GRID = np.logspace(-3, 0, 300)


def fD(speed_kmh):
    return (speed_kmh / 3.6) * CARRIER / C_LIGHT


def load_cdl_params(path):
    """Load CDL ray parameters from adapter output."""
    tau = np.load(path + '/tau_rays_for_ChannelBlock.npy').squeeze()
    power = np.load(path + '/power_rays_for_ChannelBlock.npy').squeeze()
    aoa = np.load(path + '/phi_r_rays_for_ChannelBlock.npy').squeeze()
    return tau, power, aoa


def generate_cdl_channel(tau, power, aoa, f_D_val, N_frames, N_sc, rng):
    """Generate time-varying frequency-selective channel from CDL rays.

    H[n,k] = Σ_r √p_r · exp(j·θ_r(n)) · exp(-j·2π·k·Δf·τ_r)
    θ_r(n) = 2π·f_D·cos(AoA_r)·n·T + φ_init_r
    """
    n_rays = len(tau)
    phi_init = rng.uniform(0, 2 * np.pi, n_rays)
    doppler = f_D_val * np.cos(aoa)
    k = np.arange(N_sc)
    freq_kernel = np.exp(-1j * 2 * np.pi * k[None, :] * DELTA_F * tau[:, None])
    wk = np.sqrt(power)[:, None] * freq_kernel
    H = np.zeros((N_frames, N_sc), dtype=np.complex128)
    for n in range(N_frames):
        H[n, :] = np.exp(1j * (2 * np.pi * doppler * n * T_FRAME + phi_init)) @ wk
    return H


def sweep_true_frame(H_true, snr_dB, alpha_grid=None, n_trials=None):
    """Oracle sweep in TRUE-FRAME MSE.

    EMA runs in de-rotated frame (internal stability).
    MSE measured after rotating back to true frame:
      MSE = mean_k |H_ema[k]·rot - H_true[k]|²

    Returns: (alpha_opt, oracle_mse, sem, mse_curve)
    """
    if alpha_grid is None:
        alpha_grid = ALPHA_GRID
    if n_trials is None:
        n_trials = N_TRIALS

    N_frames, N_sc = H_true.shape
    n_alpha = len(alpha_grid)
    mse_all = np.zeros((n_trials, n_alpha))

    sig_power = np.mean(np.abs(H_true) ** 2)
    noise_std = np.sqrt(sig_power * 10 ** (-snr_dB / 10) / 2)

    for trial in range(n_trials):
        rng = np.random.default_rng(trial * 7919 + int(snr_dB * 100))
        noise = rng.normal(0, noise_std, H_true.shape) + \
                1j * rng.normal(0, noise_std, H_true.shape)
        H_obs = H_true + noise

        H_smooth = np.tile(H_obs[0], (n_alpha, 1))
        mse_accum = np.zeros(n_alpha)
        count = 0

        for n in range(1, N_frames):
            inner = np.sum(H_obs[n][None, :] * np.conj(H_smooth), axis=1)
            inner_abs = np.abs(inner)
            mask = inner_abs > 1e-30
            rot = np.where(mask, inner / inner_abs, 1.0 + 0j)

            H_derot = H_obs[n][None, :] * np.conj(rot[:, None])
            diff = H_derot - H_smooth
            H_smooth = H_smooth + alpha_grid[:, None] * diff

            if n >= BURN_IN:
                H_est_true = H_smooth * rot[:, None]
                mse_frame = np.mean(np.abs(H_est_true - H_true[n][None, :]) ** 2, axis=1)
                mse_accum += mse_frame
                count += 1

        mse_all[trial] = mse_accum / count

    mse_mean = mse_all.mean(axis=0)
    mse_std = mse_all.std(axis=0)
    opt_idx = np.argmin(mse_mean)
    oracle_mse = mse_mean[opt_idx]
    sem = mse_std[opt_idx] / np.sqrt(n_trials)
    return alpha_grid[opt_idx], oracle_mse, sem, mse_mean


def measure_rot_rate(H_true, snr_dB, alpha_ref=0.02, n_frames=1000,
                     n_realizations=5, rng_seed_base=0):
    """Measure rot_rate from Reference EMA's de-rotation (deployment source).

    rot_rate = |angle(rot[n]) - angle(rot[n-1])|
    where rot is from Reference EMA (α_ref=0.02, stable source).

    Returns: mean rot_rate across realizations.
    """
    N_frames_use = min(n_frames, H_true.shape[0])
    sig_power = np.mean(np.abs(H_true) ** 2)
    noise_std = np.sqrt(sig_power * 10 ** (-snr_dB / 10) / 2)

    rr_vals = []
    for ri in range(n_realizations):
        rng = np.random.default_rng(rng_seed_base + ri * 7919)
        noise = rng.normal(0, noise_std, H_true.shape) + \
                1j * rng.normal(0, noise_std, H_true.shape)
        H_obs = H_true + noise

        H_ref = H_obs[0].copy()
        prev_angle = 0.0
        rr_frame = []

        for n in range(1, N_frames_use):
            inner = np.sum(H_obs[n] * np.conj(H_ref))
            rot = (inner / abs(inner)) if abs(inner) > 1e-30 else (1.0 + 0j)
            H_derot = H_obs[n] * np.conj(rot)
            H_ref += alpha_ref * (H_derot - H_ref)

            angle_n = np.angle(rot)
            if n > 50:
                d = abs(angle_n - prev_angle)
                if d > np.pi:
                    d = 2 * np.pi - d
                rr_frame.append(d)
            prev_angle = angle_n

        rr_vals.append(np.mean(rr_frame))

    return np.mean(rr_vals), np.std(rr_vals)


if __name__ == "__main__":
    print("Multi-SC Sweep Harness — Quick Self-Test")
    cdl_path = 'data_out/cdl_c_30kmh'
    tau, power, aoa = load_cdl_params(cdl_path)
    print(f"  CDL-C loaded: {len(tau)} rays, delay [{tau.min()*1e9:.0f}, {tau.max()*1e9:.0f}] ns")

    rng = np.random.default_rng(3000 + 10)
    H = generate_cdl_channel(tau, power, aoa, fD(3), 500, N_SC, rng)
    print(f"  Channel shape: {H.shape}, power: {np.mean(np.abs(H)**2):.3f}")

    rr_mean, rr_std = measure_rot_rate(H, 10, n_frames=500, n_realizations=3)
    print(f"  rot_rate (3km/h, 10dB, ref EMA): {rr_mean:.4f} ± {rr_std:.4f}")
    print("  Done.")
