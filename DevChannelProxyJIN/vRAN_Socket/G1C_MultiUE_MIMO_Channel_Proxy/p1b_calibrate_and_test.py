"""
P1B per-scenario calibration + capture gate (7.5GHz).

Uses multiprocessing for parallel oracle sweeps.
"""
import numpy as np
import sys
import time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

CARRIER_FREQ = 7.5e9
C_LIGHT = 3e8
T_FRAME = 5e-3
SCS = 30e3
N_SC = 1248
BURN_IN = 100
N_F = 2000
GRID = np.logspace(-2, 0, 60)
EPSILON = 0.14


def speed_to_fD(speed_kmh):
    return (speed_kmh / 3.6) * CARRIER_FREQ / C_LIGHT


def generate_channel(rays, speed_kmh, n_frames, rng):
    f_D_max = speed_to_fD(speed_kmh)
    power, tau = rays['power'], rays['tau']
    phi_r, theta_r = rays['phi_r'], rays['theta_r']
    n_rays = len(power)
    doppler = f_D_max * np.sin(theta_r) * np.cos(phi_r)
    psi = rng.uniform(0, 2 * np.pi, size=n_rays)
    n_arr = np.arange(n_frames, dtype=np.float64)
    k_arr = np.arange(N_SC, dtype=np.float64)
    time_phase = 2 * np.pi * np.outer(n_arr * T_FRAME, doppler) + psi[np.newaxis, :]
    time_comp = np.sqrt(power[np.newaxis, :]) * np.exp(1j * time_phase)
    freq_steer = np.exp(1j * (-2 * np.pi * np.outer(k_arr * SCS, tau)))
    return time_comp @ freq_steer.T


def add_awgn(H, snr_dB, rng):
    sig_pow = np.mean(np.abs(H) ** 2)
    nv = sig_pow * 10.0 ** (-snr_dB / 10.0)
    return H + rng.normal(0, np.sqrt(nv/2), H.shape) + 1j*rng.normal(0, np.sqrt(nv/2), H.shape)


def oracle_sweep(H_true, H_obs, alpha_grid):
    n_frames, n_sc = H_true.shape
    n_alpha = len(alpha_grid)
    H_sm = np.tile(H_obs[0], (n_alpha, 1)).astype(np.complex128)
    mse_acc = np.zeros(n_alpha)
    count = 0
    for n in range(1, n_frames):
        inner = H_sm.conj() @ H_obs[n]
        ia = np.abs(inner)
        rot = np.where(ia > 1e-30, inner/ia, 1.0+0j)
        H_d = H_obs[n][np.newaxis, :] * np.conj(rot[:, np.newaxis])
        H_sm += alpha_grid[:, np.newaxis] * (H_d - H_sm)
        if n >= BURN_IN:
            est = H_sm * rot[:, np.newaxis]
            mse_acc += np.mean(np.abs(est - H_true[n][np.newaxis, :]) ** 2, axis=1)
            count += 1
    curve = mse_acc / count if count > 0 else mse_acc
    idx = np.argmin(curve)
    return float(alpha_grid[idx]), float(curve[idx])


def run_fixed_alpha(H_true, H_obs, alpha):
    n_f, n_sc = H_true.shape
    H_sm = H_obs[0].copy().astype(np.complex128)
    mse_l = []
    for n in range(1, n_f):
        inner = np.dot(H_obs[n], np.conj(H_sm))
        ia = abs(inner)
        rot = inner/ia if ia > 1e-30 else 1.0+0j
        H_sm += alpha * (H_obs[n]*np.conj(rot) - H_sm)
        if n >= BURN_IN:
            mse_l.append(float(np.mean(np.abs(H_sm*rot - H_true[n])**2)))
    return float(np.mean(mse_l))


def measure_features(H_obs, c_model):
    n_f, n_sc = H_obs.shape
    H_sm = H_obs[0].copy().astype(np.complex128)
    rrs = []; prev = 0.0
    snr_frames = []
    for n in range(1, n_f):
        inner = np.dot(H_obs[n], np.conj(H_sm))
        rot = inner/abs(inner) if abs(inner) > 1e-30 else 1.0+0j
        H_sm += 0.5*(H_obs[n]*np.conj(rot) - H_sm)
        ang = float(np.angle(rot))
        rr = abs(ang - prev)
        if rr > np.pi: rr = 2*np.pi - rr
        prev = ang
        if n >= BURN_IN: rrs.append(rr)
        s = H_obs[n, 0::2] + H_obs[n, 1::2]
        d = H_obs[n, 0::2] - H_obs[n, 1::2]
        pp = float(np.mean(np.abs(s)**2))/4
        pm = float(np.mean(np.abs(d)**2))/4
        pmc = max(pm - c_model, 1e-10)
        if n >= BURN_IN:
            snr_frames.append(max((pp-pm)/pmc, 0.1))
    return float(np.mean(rrs)), float(10*np.log10(np.mean(snr_frames)))


def run_adaptive(H_true, H_obs, c_model, a1, a2, a3):
    n_f, n_sc = H_true.shape
    H_main = H_obs[0].copy().astype(np.complex128)
    alpha = 0.5; rr_sm = 0.0; snr_sm = 10.0; prev = 0.0
    mse_l = []
    for n in range(1, n_f):
        inner = np.dot(H_obs[n], np.conj(H_main))
        ia = abs(inner)
        rot = inner/ia if ia > 1e-30 else 1.0+0j
        H_d = H_obs[n]*np.conj(rot)
        H_main += alpha*(H_d - H_main)
        ang = float(np.angle(rot))
        rr = abs(ang-prev)
        if rr > np.pi: rr = 2*np.pi-rr
        prev = ang
        rr_sm = 0.95*rr_sm + 0.05*rr
        s = H_obs[n, 0::2]+H_obs[n, 1::2]
        d = H_obs[n, 0::2]-H_obs[n, 1::2]
        pp = float(np.mean(np.abs(s)**2))/4
        pm = float(np.mean(np.abs(d)**2))/4
        pmc = max(pm-c_model, 1e-10)
        snr_inst = max((pp-pm)/pmc, 0.1)
        snr_sm = 0.98*snr_sm + 0.02*10*np.log10(snr_inst)
        alpha = max(0.01, min(0.95, a1*rr_sm + a2*snr_sm + a3))
        if n >= BURN_IN:
            mse_l.append(float(np.mean(np.abs(H_main*rot - H_true[n])**2)))
    return float(np.mean(mse_l))


def process_one_condition(args):
    """Worker function for parallel execution."""
    rays, c_model, snr, speed, seed = args
    rng = np.random.default_rng(seed)
    Ht = generate_channel(rays, speed, N_F, rng)
    Ho = add_awgn(Ht, snr, rng)
    a_opt, mse_opt = oracle_sweep(Ht, Ho, GRID)
    rr, snr_est = measure_features(Ho, c_model)
    return snr, speed, rr, snr_est, a_opt, mse_opt


def main():
    p1b = np.load('../P1B_Valid_Results/Area1_7.5GHz_Rays_Valid_RXs.npz', allow_pickle=True)
    counts = p1b['counts'].squeeze()

    rx_indices = [0, 200, 500, 800, 1000]

    print("P1B Per-Scenario Calibration (7.5GHz)")
    print(f"Carrier: {CARRIER_FREQ/1e9} GHz, ε={EPSILON}")
    print(f"RX positions: {rx_indices}")
    print("=" * 70)
    sys.stdout.flush()

    for rx_idx in rx_indices:
        t0 = time.time()
        n_rays = int(counts[rx_idx])
        tau = p1b['tau'][rx_idx, 0, 0, 0, 0, :n_rays].astype(np.float64)
        power = p1b['power'][rx_idx, 0, 0, 0, 0, :n_rays].astype(np.float64)
        phi_r = np.deg2rad(p1b['phi_r_deg'][rx_idx, 0, 0, 0, 0, :n_rays].astype(np.float64))
        theta_r = np.deg2rad(p1b['theta_r_deg'][rx_idx, 0, 0, 0, 0, :n_rays].astype(np.float64))
        psum = power.sum()
        if psum < 1e-30:
            print(f"\nRX {rx_idx}: zero power, skip"); continue
        power = power / psum
        c_model = float(np.sum(power * (1 - np.cos(2*np.pi*SCS*tau))) / 2)
        rays = {'power': power, 'tau': tau, 'phi_r': phi_r, 'theta_r': theta_r}

        # ── Calibration: parallel oracle sweep ──
        tasks = []
        for snr in [0, 5, 10, 15, 20]:
            for speed in [3, 5, 10, 15, 20, 30]:
                for t in range(2):
                    tasks.append((rays, c_model, snr, speed,
                                  50000+rx_idx*1000+snr*100+speed*10+t))

        cal_data = []
        with ProcessPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(process_one_condition, t) for t in tasks]
            for f in as_completed(futures):
                cal_data.append(f.result())

        agg = {}
        for snr, speed, rr, snr_est, a_opt, _ in cal_data:
            key = (snr, speed)
            if key not in agg:
                agg[key] = {'rr': [], 'snr_est': [], 'a_opt': []}
            agg[key]['rr'].append(rr)
            agg[key]['snr_est'].append(snr_est)
            agg[key]['a_opt'].append(a_opt)

        points = []
        for (snr, speed), v in sorted(agg.items()):
            points.append((np.mean(v['rr']), np.mean(v['snr_est']), np.mean(v['a_opt'])))

        D = np.array(points)
        X = np.column_stack([D[:, 0], D[:, 1], np.ones(len(D))])
        y = D[:, 2]
        coeff, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        a1, a2, a3 = coeff
        y_pred = X @ coeff
        r2 = 1 - ((y-y_pred)**2).sum() / ((y-y.mean())**2).sum()

        # ── Capture gate ──
        n_pass = 0; n_total = 0; worst_ratio = 0
        for snr in [0, 5, 10, 20]:
            for speed in [3, 30]:
                rng = np.random.default_rng(99000+rx_idx*1000+snr*100+speed)
                Ht = generate_channel(rays, speed, N_F, rng)
                Ho = add_awgn(Ht, snr, rng)
                a_opt_t, _ = oracle_sweep(Ht, Ho, GRID)
                om = run_fixed_alpha(Ht, Ho, a_opt_t)
                am = run_adaptive(Ht, Ho, c_model, a1, a2, a3)
                ratio = am / om
                n_total += 1
                if ratio <= 1 + EPSILON: n_pass += 1
                worst_ratio = max(worst_ratio, ratio)

        elapsed = time.time() - t0
        status = "ALL PASS" if n_pass == n_total else f"{n_pass}/{n_total}"
        print(f"\nRX {rx_idx} (rays={n_rays}, τ_max={tau.max()*1e9:.0f}ns, C={c_model:.6f}):")
        print(f"  Fit: α = {a1:.4f}·rot + {a2:.4f}·SNR + {a3:.4f}, R²={r2:.3f}")
        print(f"  Gate: {status}, worst ratio={worst_ratio:.4f} [{elapsed:.0f}s]")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
