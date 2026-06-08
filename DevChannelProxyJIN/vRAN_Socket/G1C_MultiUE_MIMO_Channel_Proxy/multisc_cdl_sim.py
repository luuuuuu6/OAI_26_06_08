"""
Multi-SC CDL Channel Simulation + Oracle/Rot-rate Utilities.

Reusable harness for Phase 2 (Step 0, Step A, Step B).

Channel model:
  H[n, k] = Σ_r √(P_r) · exp(-j2π·k·Δf·τ_r) · exp(j(2π·ν_r·n·T + ψ_r))
  where ν_r = f_D_max · sin(θ_ZoA_r) · cos(φ_AoA_r)  (horizontal UE movement)

True-frame MSE:
  MSE[n] = mean_k |H_smooth[k]·rot[n] − H_true[n,k]|²
  (rotate estimate back to true channel frame)
"""
import numpy as np
from pathlib import Path

CARRIER_FREQ = 3.5e9
C_LIGHT = 3e8
T_FRAME = 5e-3
SCS = 30e3
N_SC = 1248
BURN_IN = 100


def load_cdl_rays(data_dir: str) -> dict:
    """Load CDL ray parameters from .npy files."""
    d = Path(data_dir)
    return {
        'phi_r':   np.load(d / "phi_r_rays_for_ChannelBlock.npy").flatten(),
        'phi_t':   np.load(d / "phi_t_rays_for_ChannelBlock.npy").flatten(),
        'theta_r': np.load(d / "theta_r_rays_for_ChannelBlock.npy").flatten(),
        'theta_t': np.load(d / "theta_t_rays_for_ChannelBlock.npy").flatten(),
        'power':   np.load(d / "power_rays_for_ChannelBlock.npy").flatten(),
        'tau':     np.load(d / "tau_rays_for_ChannelBlock.npy").flatten(),
    }


def load_p1b_rays(npz_path: str, rx_idx: int = 0) -> dict:
    """Load a single RX's rays from P1B ray-tracing .npz file.

    Converts P1B format (degrees, 6D arrays) to the flat dict format
    expected by generate_cdl_channel().
    """
    p1b = np.load(npz_path, allow_pickle=True)
    tau = p1b['tau'][rx_idx].flatten()
    power = p1b['power'][rx_idx].flatten()
    valid = power > 0
    return {
        'tau':     tau[valid].astype(np.float64),
        'power':   power[valid].astype(np.float64),
        'phi_r':   np.deg2rad(p1b['phi_r_deg'][rx_idx].flatten()[valid]).astype(np.float64),
        'phi_t':   np.deg2rad(p1b['phi_t_deg'][rx_idx].flatten()[valid]).astype(np.float64),
        'theta_r': np.deg2rad(p1b['theta_r_deg'][rx_idx].flatten()[valid]).astype(np.float64),
        'theta_t': np.deg2rad(p1b['theta_t_deg'][rx_idx].flatten()[valid]).astype(np.float64),
    }


def load_p1b_all_rx(npz_path: str) -> int:
    """Return the number of RX positions in a P1B .npz file."""
    p1b = np.load(npz_path, allow_pickle=True)
    return int(p1b['num_rx'])


def speed_to_fD(speed_kmh: float) -> float:
    return (speed_kmh / 3.6) * CARRIER_FREQ / C_LIGHT


def generate_cdl_channel(rays: dict, speed_kmh: float, n_frames: int,
                         n_sc: int = N_SC, rng: np.random.Generator = None):
    """Generate multi-SC CDL channel via sum-of-rays.

    Returns H_true of shape (n_frames, n_sc), complex128.
    """
    if rng is None:
        rng = np.random.default_rng()

    f_D_max = speed_to_fD(speed_kmh)
    power = rays['power']
    tau = rays['tau']
    phi_r = rays['phi_r']
    theta_r = rays['theta_r']
    n_rays = len(power)

    doppler = f_D_max * np.sin(theta_r) * np.cos(phi_r)
    psi = rng.uniform(0, 2 * np.pi, size=n_rays)

    n_arr = np.arange(n_frames, dtype=np.float64)
    k_arr = np.arange(n_sc, dtype=np.float64)

    time_phase = 2 * np.pi * np.outer(n_arr * T_FRAME, doppler) + psi[np.newaxis, :]
    time_comp = np.sqrt(power[np.newaxis, :]) * np.exp(1j * time_phase)

    freq_phase = -2 * np.pi * np.outer(k_arr * SCS, tau)
    freq_steer = np.exp(1j * freq_phase)

    H = time_comp @ freq_steer.T
    return H


def add_awgn(H_true: np.ndarray, snr_dB: float,
             rng: np.random.Generator) -> np.ndarray:
    """Add complex AWGN at specified per-SC SNR."""
    sig_power = np.mean(np.abs(H_true) ** 2)
    noise_var = sig_power * 10.0 ** (-snr_dB / 10.0)
    noise = rng.normal(0, np.sqrt(noise_var / 2), size=H_true.shape) + \
            1j * rng.normal(0, np.sqrt(noise_var / 2), size=H_true.shape)
    return H_true + noise


def _derot_inner(H_obs_row, H_ref_row):
    """LS de-rotation: rot = sum(H_obs·conj(H_ref)) / |sum|."""
    inner = np.dot(H_obs_row, np.conj(H_ref_row))
    inner_abs = abs(inner)
    if inner_abs < 1e-30:
        return 1.0 + 0j
    return inner / inner_abs


# ── Oracle sweep (vectorized across α) ──

def oracle_sweep(H_true: np.ndarray, H_obs: np.ndarray,
                 alpha_grid: np.ndarray, burn_in: int = BURN_IN):
    """Sweep fixed-α EMA with self-referenced de-rotation, true-frame MSE.

    Returns (mse_curve, alpha_opt, oracle_mse).
    mse_curve: shape (n_alpha,)
    """
    n_frames, n_sc = H_true.shape
    n_alpha = len(alpha_grid)

    H_smooth = np.tile(H_obs[0], (n_alpha, 1)).astype(np.complex128)
    mse_accum = np.zeros(n_alpha, dtype=np.float64)
    count = 0

    for n in range(1, n_frames):
        inner = H_smooth.conj() @ H_obs[n]
        inner_abs = np.abs(inner)
        mask = inner_abs > 1e-30
        rot = np.where(mask, inner / inner_abs, 1.0 + 0j)

        H_derot = H_obs[n][np.newaxis, :] * np.conj(rot[:, np.newaxis])
        H_smooth += alpha_grid[:, np.newaxis] * (H_derot - H_smooth)

        if n >= burn_in:
            H_est_true = H_smooth * rot[:, np.newaxis]
            err = np.mean(np.abs(H_est_true - H_true[n][np.newaxis, :]) ** 2, axis=1)
            mse_accum += err
            count += 1

    mse_curve = mse_accum / count if count > 0 else mse_accum
    opt_idx = np.argmin(mse_curve)
    return mse_curve, float(alpha_grid[opt_idx]), float(mse_curve[opt_idx])


# ── Reference EMA rot_rate measurement ──

def measure_ref_ema_rot_rate(H_obs: np.ndarray, alpha_ref: float = 0.02,
                             burn_in: int = BURN_IN):
    """Run Reference EMA (fixed α_ref), return mean rot_rate after burn-in.

    rot_rate = |angle(rot[n]) − angle(rot[n−1])| with wrapping to [0, π].
    rot comes from Reference EMA's de-rotation (stable source).
    """
    n_frames, n_sc = H_obs.shape
    H_ref = H_obs[0].copy().astype(np.complex128)

    rot_angles = np.zeros(n_frames, dtype=np.float64)

    for n in range(1, n_frames):
        rot = _derot_inner(H_obs[n], H_ref)
        rot_angles[n] = np.angle(rot)
        H_derot = H_obs[n] * np.conj(rot)
        H_ref += alpha_ref * (H_derot - H_ref)

    rot_rates = np.abs(np.diff(rot_angles[1:]))
    rot_rates = np.where(rot_rates > np.pi, 2 * np.pi - rot_rates, rot_rates)

    return float(np.mean(rot_rates[burn_in:]))


# ── Single run: fixed-α EMA with true-frame MSE (for capture gate) ──

def run_fixed_alpha_ema(H_true: np.ndarray, H_obs: np.ndarray,
                        alpha: float, burn_in: int = BURN_IN):
    """Single fixed-α EMA with self-referenced de-rotation.

    Returns (mean_mse, mse_per_frame).
    """
    n_frames, n_sc = H_true.shape
    H_smooth = H_obs[0].copy().astype(np.complex128)
    mse_list = []

    for n in range(1, n_frames):
        rot = _derot_inner(H_obs[n], H_smooth)
        H_derot = H_obs[n] * np.conj(rot)
        H_smooth += alpha * (H_derot - H_smooth)

        if n >= burn_in:
            H_est_true = H_smooth * rot
            mse = float(np.mean(np.abs(H_est_true - H_true[n]) ** 2))
            mse_list.append(mse)

    mse_arr = np.array(mse_list)
    return float(mse_arr.mean()), mse_arr


# ── DualEMA-structure oracle sweep (for apples-to-apples capture gate) ──

def oracle_sweep_dual_ema(H_true: np.ndarray, H_obs: np.ndarray,
                          alpha_grid: np.ndarray,
                          alpha_ref: float = 0.02,
                          derot_gain_db: float = 5.0,
                          burn_in: int = BURN_IN):
    """Sweep fixed-α on DualEMA structure (same de-rotation/gate as deployment).

    Precomputes H_ref trajectory and derot decisions (shared across all α),
    then vectorizes the Main EMA sweep.

    Returns (mse_curve, alpha_opt, oracle_mse).
    """
    n_frames, n_sc = H_true.shape
    n_alpha = len(alpha_grid)
    derot_gain = 10.0 ** (-derot_gain_db / 10.0)

    H_ref = H_obs[0].copy().astype(np.complex128)
    H_inputs = np.empty((n_frames, n_sc), dtype=np.complex128)
    rots = np.empty(n_frames, dtype=np.complex128)
    used_derots = np.empty(n_frames, dtype=bool)

    H_inputs[0] = H_obs[0]
    rots[0] = 1.0 + 0j
    used_derots[0] = False

    for n in range(1, n_frames):
        inner = np.dot(H_obs[n], np.conj(H_ref))
        inner_abs = abs(inner)
        rot = inner / inner_abs if inner_abs > 1e-30 else 1.0 + 0j

        H_derot = H_obs[n] * np.conj(rot)
        innov_raw = float(np.mean(np.abs(H_obs[n] - H_ref) ** 2))
        innov_derot = float(np.mean(np.abs(H_derot - H_ref) ** 2))

        if innov_derot < innov_raw * derot_gain:
            H_input = H_derot
            used_derot = True
        else:
            H_input = H_obs[n]
            used_derot = False

        H_inputs[n] = H_input
        rots[n] = rot
        used_derots[n] = used_derot
        H_ref += alpha_ref * (H_input - H_ref)

    H_main = np.tile(H_obs[0], (n_alpha, 1)).astype(np.complex128)
    mse_accum = np.zeros(n_alpha, dtype=np.float64)
    count = 0

    for n in range(1, n_frames):
        H_main += alpha_grid[:, np.newaxis] * (H_inputs[n][np.newaxis, :] - H_main)

        if n >= burn_in:
            if used_derots[n]:
                H_est_true = H_main * rots[n]
            else:
                H_est_true = H_main
            mse = np.mean(np.abs(H_est_true - H_true[n][np.newaxis, :]) ** 2, axis=1)
            mse_accum += mse
            count += 1

    mse_curve = mse_accum / count if count > 0 else mse_accum
    opt_idx = np.argmin(mse_curve)
    return mse_curve, float(alpha_grid[opt_idx]), float(mse_curve[opt_idx])


def run_dual_ema_fixed_alpha(H_true: np.ndarray, H_obs: np.ndarray,
                             alpha: float, alpha_ref: float = 0.02,
                             derot_gain_db: float = 5.0,
                             burn_in: int = BURN_IN):
    """Run DualEMA with fixed α, return true-frame MSE (derot-aware)."""
    n_frames, n_sc = H_true.shape
    derot_gain = 10.0 ** (-derot_gain_db / 10.0)

    H_ref = H_obs[0].copy().astype(np.complex128)
    H_main = H_obs[0].copy().astype(np.complex128)
    mse_list = []

    for n in range(1, n_frames):
        inner = np.dot(H_obs[n], np.conj(H_ref))
        inner_abs = abs(inner)
        rot = inner / inner_abs if inner_abs > 1e-30 else 1.0 + 0j

        H_derot = H_obs[n] * np.conj(rot)
        innov_raw = float(np.mean(np.abs(H_obs[n] - H_ref) ** 2))
        innov_derot = float(np.mean(np.abs(H_derot - H_ref) ** 2))

        if innov_derot < innov_raw * derot_gain:
            H_input = H_derot
            used_derot = True
        else:
            H_input = H_obs[n]
            used_derot = False

        H_ref += alpha_ref * (H_input - H_ref)
        H_main += alpha * (H_input - H_main)

        if n >= burn_in:
            H_est = H_main * rot if used_derot else H_main
            mse = float(np.mean(np.abs(H_est - H_true[n]) ** 2))
            mse_list.append(mse)

    mse_arr = np.array(mse_list)
    return float(mse_arr.mean()), mse_arr
