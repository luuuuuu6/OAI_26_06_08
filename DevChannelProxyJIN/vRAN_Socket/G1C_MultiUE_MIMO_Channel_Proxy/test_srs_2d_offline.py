#!/usr/bin/env python3
"""Unified offline harness for the SRS true-2D MMSE estimator (freq x time).

Covers BOTH stages of the separable 2D MMSE before porting to C:
  - frequency stage: windowed Toeplitz LMMSE (== C nr_srs_mmse_freq_filter)
    + diagonal / ESPRIT / genie comparisons   [--true2d]
  - time stage: phase-predictive Kalman (PKF) + IAE-Riccati adaptive K, with
    data-driven R_meas from the freq stage; vs EWMA / the C global-de-rotation
    [--cascade]
Also keeps the original data-only freq Wiener checks (--synth-mf / --cdl-dir /
--run-dir). Intentionally does NOT touch OAI runtime code; validates the math
we want before porting anything back to C.
(Formerly test_wiener_offline.py — renamed; it now spans freq+time, not just Wiener.)
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from digital_twin_stats import index_gt_slots, load_gt_by_refs  # noqa: E402
from eval_nmse_clean import align_frames, get_active_mask, load_srs, nmse_ls  # noqa: E402


def nmse_db(est: np.ndarray, ref: np.ndarray) -> float:
    err = np.mean(np.abs(est - ref) ** 2)
    sig = np.mean(np.abs(ref) ** 2)
    return 10.0 * np.log10(err / (sig + 1e-30) + 1e-30)


def moving_average(x: np.ndarray, win: int) -> np.ndarray:
    if win <= 1:
        return x.copy()
    pad = win // 2
    xp = np.pad(x, (pad, pad), mode="reflect")
    kernel = np.ones(win, dtype=np.float64) / float(win)
    return np.convolve(xp, kernel, mode="valid")


def active_circular_order(active: np.ndarray) -> np.ndarray:
    """Return active SC indices in contiguous circular order."""
    aidx = np.where(active)[0]
    if aidx.size <= 1:
        return aidx
    gaps = np.diff(np.r_[aidx, aidx[0] + active.size])
    start = (int(np.argmax(gaps)) + 1) % aidx.size
    return np.r_[aidx[start:], aidx[:start]]


def smooth_active_complex(h: np.ndarray, order: np.ndarray, win: int) -> np.ndarray:
    out = h.copy()
    flat = out.reshape(-1, out.shape[-1])
    src = h.reshape(-1, h.shape[-1])
    for row in range(flat.shape[0]):
        vals = src[row, order]
        flat[row, order] = moving_average(vals.real, win) + 1j * moving_average(vals.imag, win)
    return out


def freq_response_from_pdp(n_sc: int, delays: np.ndarray, taps: np.ndarray) -> np.ndarray:
    k = np.arange(n_sc, dtype=np.float64)
    h = np.zeros(n_sc, dtype=np.complex128)
    for d, a in zip(delays, taps):
        h += a * np.exp(-2j * np.pi * k * float(d) / float(n_sc))
    return h


def correlation_from_pdp(n_sc: int, delays: np.ndarray, powers: np.ndarray) -> np.ndarray:
    dk = np.arange(n_sc, dtype=np.float64)
    r = np.zeros(n_sc, dtype=np.complex128)
    for d, p in zip(delays, powers):
        r += p * np.exp(-2j * np.pi * dk * float(d) / float(n_sc))
    return r


def R_from_pdp_direct(clean: np.ndarray, max_dk: int) -> np.ndarray:
    """Reference R(dk) = sum_n clean[n] * exp(-j 2pi dk n / N), dk=0..max_dk.
    Mirrors the current C nr_srs_R_from_pdp_arr (per-dk, per-bin cos/sin)."""
    n = clean.size
    nn = np.arange(n)
    dk = np.arange(max_dk + 1)
    return (clean[None, :] * np.exp(-2j * np.pi * dk[:, None] * nn[None, :] / n)).sum(axis=1)


def R_from_pdp_table(clean: np.ndarray, max_dk: int):
    """Twiddle-table + sparse variant of R_from_pdp_direct (proof of the C accel).

    Precompute cos_tab[m]=cos(2pi m/N), sin_tab[m]=sin(2pi m/N), m=0..N-1, then
      R(dk) = sum over nonzero bins n of clean[n] * (cos_tab[(dk*n)%N] - j sin_tab[(dk*n)%N]).
    Numerically identical to R_from_pdp_direct (table built from small angle 2pi m/N
    vs direct big angle -2pi dk n/N -> ~1 ulp). N must be a power of 2 so (dk*n)%N
    is exact via masking. Returns (R, n_trig_saved)."""
    n = clean.size
    assert (n & (n - 1)) == 0, "N must be a power of two for &(N-1) masking"
    mask = n - 1
    m = np.arange(n)
    cos_tab = np.cos(2.0 * np.pi * m / n)
    sin_tab = np.sin(2.0 * np.pi * m / n)
    nz_idx = np.nonzero(clean > 0.0)[0]
    nz_val = clean[nz_idx]
    R = np.empty(max_dk + 1, dtype=np.complex128)
    for dk in range(max_dk + 1):
        idx = (dk * nz_idx) & mask
        R[dk] = np.sum(nz_val * (cos_tab[idx] - 1j * sin_tab[idx]))
    # cos/sin saved vs direct: direct does (max_dk+1)*nnz transcendental pairs;
    # table does only N once (the tables) -> reused across all dk.
    n_trig_direct = (max_dk + 1) * nz_idx.size
    return R, n_trig_direct


def lmmse_fullband(y: np.ndarray, r_delta: np.ndarray, sigma2: float, jitter: float = 1e-9) -> np.ndarray:
    """Full-band LMMSE: h_hat = R (R + sigma2 I)^-1 y.

    This is deliberately small-N and offline-only. It is the reference math for
    later C implementations, not the intended realtime algorithm.
    """
    n = y.size
    idx = np.arange(n)
    lag = idx[:, None] - idx[None, :]            # signed lag, -(n-1)..(n-1)
    # Hermitian Toeplitz: R[k,k']=r(k-k'); r(-m)=conj(r(m)). Using circular
    # (lag % n) is only valid for integer delays; CDL has fractional delays.
    r = np.where(lag >= 0, r_delta[np.abs(lag)], np.conj(r_delta[np.abs(lag)]))
    a = r + (sigma2 + jitter) * np.eye(n, dtype=np.complex128)
    weights_y = np.linalg.solve(a, y)
    return r @ weights_y


def oracle_pdp_wiener_active(h_srs: np.ndarray, h_gt: np.ndarray, order: np.ndarray) -> np.ndarray:
    """Oracle-PDP Wiener on the active band only.

    This is an offline upper-bound check. It uses GT only to estimate the PDP
    and noise level; it should not be ported directly to runtime.
    """
    out = h_srs.copy()
    f_count, n_rx, n_tx, _ = h_srs.shape
    n_act = order.size
    eps = 1e-30

    for rx in range(n_rx):
        for tx in range(n_tx):
            y = h_srs[:, rx, tx, :][:, order]
            g = h_gt[:, rx, tx, :][:, order]

            alpha = np.sum(np.conj(g) * y, axis=1) / (np.sum(np.abs(g) ** 2, axis=1) + eps)
            ref = alpha[:, None] * g
            residual = y - ref

            sigma2_freq = float(np.mean(np.abs(residual) ** 2))
            ref_delay = np.fft.ifft(ref, axis=-1)
            pdp = np.mean(np.abs(ref_delay) ** 2, axis=0)
            sigma2_delay = sigma2_freq / float(n_act)
            gain = pdp / (pdp + sigma2_delay + eps)

            y_delay = np.fft.ifft(y, axis=-1)
            est = np.fft.fft(y_delay * gain[None, :], axis=-1)
            view = out[:, rx, tx, :]
            view[:, order] = est

    return out


def estimate_noise_delay(delay_power: np.ndarray, mode: str) -> float:
    tail = delay_power[(delay_power.size * 3) // 4:]
    if tail.size == 0:
        tail = delay_power
    if mode == "tail-p25-exp":
        return float(max(np.percentile(tail, 25), 1e-30) / 0.2877)
    if mode == "tail-p25":
        return float(max(np.percentile(tail, 25), 1e-30))
    if mode == "tail-mean":
        return float(max(np.mean(tail), 1e-30))
    return float(max(np.median(tail), 1e-30))


def data_pdp_wiener_active(h_srs: np.ndarray, order: np.ndarray, noise_mode: str) -> tuple[np.ndarray, dict]:
    """Data-driven active-band Wiener using only SRS estimates.

    The active SC vector is treated as the observed frequency band. Its IFFT
    gives an active-band delay profile without zero-padding inactive OFDM SCs.
    The averaged delay power estimates PDP; a far-tail statistic estimates the
    delay-domain noise floor.
    """
    out = h_srs.copy()
    f_count, n_rx, n_tx, _ = h_srs.shape
    eps = 1e-30
    gains = []
    noise_vals = []

    for rx in range(n_rx):
        for tx in range(n_tx):
            y = h_srs[:, rx, tx, :][:, order]
            y_delay = np.fft.ifft(y, axis=-1)
            delay_power = np.mean(np.abs(y_delay) ** 2, axis=0)
            noise_delay = estimate_noise_delay(delay_power, noise_mode)
            pdp = np.maximum(delay_power - noise_delay, 0.0)

            # If the data-only PDP collapses, transparently return the input
            # for this antenna pair. Runtime C should use the same fallback.
            if float(np.sum(pdp)) <= eps:
                gain = np.ones_like(pdp)
            else:
                gain = pdp / (pdp + noise_delay + eps)

            est = np.fft.fft(y_delay * gain[None, :], axis=-1)
            view = out[:, rx, tx, :]
            view[:, order] = est
            gains.append(gain)
            noise_vals.append(noise_delay)

    gain_all = np.concatenate([g.ravel() for g in gains]) if gains else np.array([])
    diag = {
        "noise_delay_median": float(np.median(noise_vals)) if noise_vals else 0.0,
        "gain_mean": float(np.mean(gain_all)) if gain_all.size else 0.0,
        "gain_p90": float(np.percentile(gain_all, 90)) if gain_all.size else 0.0,
        "gain_nonzero": float(np.mean(gain_all > 1e-3)) if gain_all.size else 0.0,
    }
    return out, diag


def data_topk_delay_active(h_srs: np.ndarray, order: np.ndarray, top_k: int) -> np.ndarray:
    """Data-only active-band delay truncation.

    This is not final Wiener, but it is an important no-GT baseline: if a
    channel is nearly flat, retaining the strongest active-band delay taps
    should approach the full-band average behavior.
    """
    out = h_srs.copy()
    _, n_rx, n_tx, _ = h_srs.shape

    for rx in range(n_rx):
        for tx in range(n_tx):
            y = h_srs[:, rx, tx, :][:, order]
            y_delay = np.fft.ifft(y, axis=-1)
            delay_power = np.mean(np.abs(y_delay) ** 2, axis=0)
            keep = np.argsort(delay_power)[-max(1, min(top_k, order.size)):]
            mask = np.zeros(order.size, dtype=np.float64)
            mask[keep] = 1.0
            est = np.fft.fft(y_delay * mask[None, :], axis=-1)
            view = out[:, rx, tx, :]
            view[:, order] = est

    return out


def data_energy_delay_active(h_srs: np.ndarray, order: np.ndarray, energy_frac: float) -> tuple[np.ndarray, dict]:
    """Data-only delay truncation with automatic tap count selection."""
    out = h_srs.copy()
    _, n_rx, n_tx, _ = h_srs.shape
    chosen_k = []

    for rx in range(n_rx):
        for tx in range(n_tx):
            y = h_srs[:, rx, tx, :][:, order]
            y_delay = np.fft.ifft(y, axis=-1)
            delay_power = np.mean(np.abs(y_delay) ** 2, axis=0)

            ranked = np.argsort(delay_power)[::-1]
            total = float(np.sum(delay_power)) + 1e-30
            csum = np.cumsum(delay_power[ranked])
            k = int(np.searchsorted(csum, energy_frac * total, side="left")) + 1
            k = max(1, min(k, order.size))
            keep = ranked[:k]

            mask = np.zeros(order.size, dtype=np.float64)
            mask[keep] = 1.0
            est = np.fft.fft(y_delay * mask[None, :], axis=-1)
            view = out[:, rx, tx, :]
            view[:, order] = est
            chosen_k.append(k)

    diag = {
        "k_median": float(np.median(chosen_k)) if chosen_k else 0.0,
        "k_min": float(np.min(chosen_k)) if chosen_k else 0.0,
        "k_max": float(np.max(chosen_k)) if chosen_k else 0.0,
    }
    return out, diag


def _norm_ppf(q: float) -> float:
    """Standard-normal quantile (Acklam rational approx; portable to C)."""
    if q <= 0.0:
        return -1e9
    if q >= 1.0:
        return 1e9
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1.0 - 0.02425
    if q < plow:
        t = np.sqrt(-2.0 * np.log(q))
        return (((((c[0] * t + c[1]) * t + c[2]) * t + c[3]) * t + c[4]) * t + c[5]) / \
               ((((d[0] * t + d[1]) * t + d[2]) * t + d[3]) * t + 1.0)
    if q > phigh:
        t = np.sqrt(-2.0 * np.log(1.0 - q))
        return -(((((c[0] * t + c[1]) * t + c[2]) * t + c[3]) * t + c[4]) * t + c[5]) / \
                ((((d[0] * t + d[1]) * t + d[2]) * t + d[3]) * t + 1.0)
    t = q - 0.5
    r = t * t
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * t / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)


def adaptive_floor_threshold(pdp: np.ndarray, p_fa: float | None = None,
                             k: float = 3.0, n_iter: int = 5) -> tuple[float, float, float]:
    """Fully data-driven (floor, keep-threshold) — no hand-set gamma.

    1. Robust noise floor mu via iterative trim (taps below k*floor are noise).
    2. Measure the noise population's effective DOF  M = mu^2 / var(noise)
       (EMA-averaged exponential power -> ~Gamma(M, mu/M)).
    3. Keep-threshold = mu * gamma, where gamma is the (1 - p_fa) quantile of
       the normalized noise power, p_fa = 1/N (expect <=1 false tap per band).
       gamma via Wilson-Hilferty chi-square(df=2M) quantile (portable to C):
         gamma = (1 - 2/(9df) + z*sqrt(2/(9df)))^3,  z = Phi^-1(1-p_fa).
    Both floor AND threshold come from data; only p_fa=1/N is a (principled,
    data-derived) spec, far less sensitive than a hand-tuned gamma.
    """
    n = pdp.size
    if p_fa is None:
        p_fa = 1.0 / max(n, 2)
    f = float(np.median(pdp))
    if f <= 0.0:
        f = float(np.mean(pdp)) + 1e-30
    noise = pdp
    for _ in range(n_iter):
        sel = pdp[pdp < k * f]
        if sel.size < 8:
            break
        nf = float(np.mean(sel))
        noise = sel
        if nf <= 0.0:
            break
        if abs(nf - f) < 1e-6 * f:
            f = nf
            break
        f = nf
    mu = max(f, 1e-30)
    var = float(np.var(noise)) + 1e-30
    m_eff = max(1.0, mu * mu / var)
    z = _norm_ppf(1.0 - p_fa)
    df = 2.0 * m_eff
    wh = (1.0 - 2.0 / (9.0 * df) + z * np.sqrt(2.0 / (9.0 * df))) ** 3
    gamma = max(1.0, float(wh))
    return mu, mu * gamma, gamma


def robust_noise_floor(pdp: np.ndarray, k: float = 3.0, n_iter: int = 5) -> float:
    """Iterative robust estimate of the (mean) noise floor of a delay PDP.

    Channel taps are sparse and sit well above the noise; most delay bins are
    noise. Start from the median (>=50% are noise) and iteratively re-estimate
    the noise mean from bins below k*floor, rejecting signal taps. Converges to
    the noise mean WITHOUT any hand-set absolute/relative threshold, so the
    downstream `pdp > floor*gamma` tap count auto-adapts to the channel:
    flat -> 1 tap, frequency-selective -> many taps.
    """
    f = float(np.median(pdp))
    if f <= 0.0:
        f = float(np.mean(pdp)) + 1e-30
    for _ in range(n_iter):
        noise = pdp[pdp < k * f]
        if noise.size < 4:
            break
        nf = float(np.mean(noise))
        if nf <= 0.0:
            break
        if abs(nf - f) < 1e-6 * f:
            f = nf
            break
        f = nf
    return max(f, 1e-30)


def data_pdp_ema_wiener_active(h_srs: np.ndarray, order: np.ndarray, alpha: float,
                               rel_db: float, gamma: float,
                               noise_mode: str, adaptive: bool = False,
                               floor_k: float = 3.0, auto_gamma: bool = False,
                               p_fa: float | None = None) -> tuple[np.ndarray, dict]:
    """Causal multi-frame EMA PDP + relative-peak rank soft Wiener (data-only).

    Realtime-faithful candidate for the C port:
      1. Per (rx,tx) link, IFFT the active band to the delay domain.
      2. Walk frames in temporal order, EMA the per-tap delay power
         (pdp_ema = alpha*pdp_ema + (1-alpha)*|y_delay|^2). This is the
         second-order statistic (R_f) estimate; EMA lowers its variance.
      3. Robust noise floor from the EMA'd PDP far tail; sig = max(pdp-floor,0).
      4. RANK SELECTION = relative-to-peak threshold: keep tap n if
         pdp_ema[n] > peak * 10^(-rel_db/10), AND pdp_ema[n] > floor*gamma.
         This is robust to the COLOURED delay-noise of the filt8 input
         (those bins sit above the scalar floor but far below the dominant
         peak, so they are rejected). Energy-fraction selection is NOT used
         because on real data its budget gets inflated by the coloured tail.
         Flat -> K=1 (~top1 / full-band-average); freq-selective -> few taps.
      5. Soft Wiener gain sig/(sig+floor) on kept taps, zero elsewhere; FFT
         back into the active band of the current frame only.

    Per-link processing => MIMO-order agnostic by design.
    """
    out = h_srs.copy()
    f_count, n_rx, n_tx, _ = h_srs.shape
    eps = 1e-30
    rel_lin = 10.0 ** (-rel_db / 10.0)
    gain_nz = []
    ranks = []

    for rx in range(n_rx):
        for tx in range(n_tx):
            y = h_srs[:, rx, tx, :][:, order]            # (f_count, n_act)
            y_delay = np.fft.ifft(y, axis=-1)            # (f_count, n_act)
            view = out[:, rx, tx, :]

            pdp_ema = None
            for f in range(f_count):
                inst = np.abs(y_delay[f]) ** 2
                pdp_ema = inst.copy() if pdp_ema is None else alpha * pdp_ema + (1.0 - alpha) * inst

                if auto_gamma:
                    # Fully data-driven: floor AND threshold from noise statistics
                    # (no fixed gamma, no fixed rel_db). p_fa = 1/N.
                    floor, thr, _g = adaptive_floor_threshold(pdp_ema, p_fa=p_fa, k=floor_k)
                    sig = np.maximum(pdp_ema - floor, 0.0)
                    keep = (pdp_ema > thr) & (sig > 0.0)
                elif adaptive:
                    # Adaptive floor, fixed gamma multiplier.
                    floor = robust_noise_floor(pdp_ema, k=floor_k)
                    sig = np.maximum(pdp_ema - floor, 0.0)
                    keep = (pdp_ema > floor * gamma) & (sig > 0.0)
                else:
                    floor = estimate_noise_delay(pdp_ema, noise_mode)
                    sig = np.maximum(pdp_ema - floor, 0.0)
                    peak = float(np.max(pdp_ema))
                    keep = (pdp_ema > peak * rel_lin) & (pdp_ema > floor * gamma) & (sig > 0.0)

                gain = np.where(keep, sig / (sig + floor + eps), 0.0)
                view[f, order] = np.fft.fft(y_delay[f] * gain, axis=-1)
                gain_nz.append(float(np.mean(gain > 1e-3)))
                ranks.append(int(np.count_nonzero(keep)))

    diag = {
        "gain_nonzero": float(np.mean(gain_nz)) if gain_nz else 0.0,
        "rank_median": float(np.median(ranks)) if ranks else 0.0,
        "rank_p90": float(np.percentile(ranks, 90)) if ranks else 0.0,
    }
    return out, diag


def data_ema_wiener_cport(h_ls: np.ndarray, order: np.ndarray, k_tc: int, alpha: float,
                          p_fa: float | None, floor_k: float,
                          ifft_size: int = 2048) -> tuple[np.ndarray, np.ndarray, dict]:
    """C-faithful recipe: LS pilots -> 2048 dense-pilot IFFT (white noise kept)
    -> causal EMA PDP -> adaptive_floor_threshold -> soft gain -> dense band
    reconstruction by evaluating the kept delay taps at the full subcarrier grid.

    This mirrors exactly what the C port will do (idft/dft 2048 + NUDFT recon),
    so the offline result is the spec the C must reproduce. Returns the dense
    estimate, the dense active-SC index list, and diagnostics.
    """
    f_count, n_rx, n_tx, n_sc = h_ls.shape
    n_pil = order.size
    if ifft_size <= 0:
        ifft_size = n_pil
    n_dense = n_pil * k_tc
    first = int(order[0])
    dense_idx = (first + np.arange(n_dense)) % n_sc

    out = np.zeros((f_count, n_rx, n_tx, n_sc), dtype=np.complex128)
    nbin = np.arange(ifft_size)
    ranks, gain_nz = [], []

    for rx in range(n_rx):
        for tx in range(n_tx):
            yp = h_ls[:, rx, tx, :][:, order]               # (f, n_pil) pilot LS
            pdp_ema = None
            view = out[:, rx, tx, :]
            for f in range(f_count):
                buf = np.zeros(ifft_size, dtype=np.complex128)
                buf[:n_pil] = yp[f]
                h_time = np.fft.ifft(buf)                    # white noise preserved
                inst = np.abs(h_time) ** 2
                pdp_ema = inst.copy() if pdp_ema is None else alpha * pdp_ema + (1.0 - alpha) * inst

                floor, thr, _g = adaptive_floor_threshold(pdp_ema, p_fa=p_fa, k=floor_k)
                sig = np.maximum(pdp_ema - floor, 0.0)
                keep = (pdp_ema > thr) & (sig > 0.0)
                gain = np.where(keep, sig / (sig + floor + 1e-30), 0.0)
                h_filt = h_time * gain

                kn = nbin[keep]                              # kept delay taps only
                if kn.size == 0:
                    continue
                hv = h_filt[keep]
                # H_dense[m] = sum_n h_filt[n] exp(-j2pi (m/k_tc) n / N)
                phase = -2j * np.pi * np.outer(dense_idx / float(k_tc), kn) / float(ifft_size)
                view[f, dense_idx] = np.exp(phase) @ hv
                ranks.append(int(kn.size))
                gain_nz.append(float(np.mean(gain > 1e-3)))

    diag = {
        "rank_median": float(np.median(ranks)) if ranks else 0.0,
        "gain_nonzero": float(np.mean(gain_nz)) if gain_nz else 0.0,
        "n_dense": n_dense,
    }
    return out, dense_idx, diag


# ===========================================================================
# True-2D building blocks (freq windowed-Toeplitz / ESPRIT R) + time stage
# (PKF). Consolidated here so there is ONE offline harness; --true2d and
# --cascade run modes use these on the shared CDL synthesis (synth_cdl_channel).
# ===========================================================================

def synth_cdl_channel(mdir: Path, n_sc: int, n_frames: int, doppler: float,
                      rng: np.random.Generator, mode: str, band_bw: float
                      ) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Build a time-varying per-link CDL channel from ray (tau,power[,phi_r]).

    mode='randomwalk': diffusive tap-phase drift (unpredictable).
    mode='coherent'  : constant-velocity Doppler omega=doppler*cos(phi_r) per
                       ray (predictable) — needed to test phase prediction.
    Returns H[F,n_sc], d_bins, pw, ds_ns.
    """
    tau = np.load(mdir / "tau_rays_for_ChannelBlock.npy").ravel().astype(np.float64)
    pw = np.load(mdir / "power_rays_for_ChannelBlock.npy").ravel().astype(np.float64)
    pw = pw / (pw.sum() + 1e-30)
    mean_t = np.sum(pw * tau)
    ds_ns = np.sqrt(np.sum(pw * (tau - mean_t) ** 2)) * 1e9
    d_bins = tau * band_bw
    k = np.arange(n_sc)[:, None]
    E = np.exp(-2j * np.pi * k * (d_bins[None, :] / n_sc))
    a0 = np.sqrt(pw) * np.exp(1j * rng.uniform(0.0, 2.0 * np.pi, pw.size))
    H = np.empty((n_frames, n_sc), dtype=np.complex128)
    if mode == "coherent":
        phi_f = mdir / "phi_r_rays_for_ChannelBlock.npy"
        if phi_f.exists():
            ang = np.load(phi_f).ravel().astype(np.float64)
            ang = np.deg2rad(ang) if np.nanmax(np.abs(ang)) > 6.5 else ang
        else:
            ang = rng.uniform(0.0, 2.0 * np.pi, pw.size)
        omega_ray = doppler * np.cos(ang)
        for f in range(n_frames):
            H[f] = E @ (a0 * np.exp(1j * omega_ray * f))
    else:
        a = a0.copy()
        for f in range(n_frames):
            a = a * np.exp(1j * doppler * rng.standard_normal(pw.size))
            H[f] = E @ a
    return H, d_bins, pw, ds_ns


def windowed_lmmse(y: np.ndarray, r_delta: np.ndarray, sigma2: float, win: int) -> np.ndarray:
    """Per-subcarrier banded Hermitian-Toeplitz LMMSE (mirrors C windowed solve).

    For each target k: window of `win` observed SC; A = R_ww + sigma2 I from
    r_delta; estimate h_k = r_{k,w} A^-1 y_w. r_delta is length-N freq autocorr
    (r_delta[|lag|], Hermitian).
    """
    n = y.size
    half = win // 2
    out = np.empty(n, dtype=np.complex128)
    idx_all = np.arange(n)

    def r_lag(lag: np.ndarray) -> np.ndarray:
        a = np.abs(lag)
        return np.where(lag >= 0, r_delta[a], np.conj(r_delta[a]))

    for k in range(n):
        start = min(max(k - half, 0), n - win)
        w_idx = idx_all[start:start + win]
        A = r_lag(w_idx[:, None] - w_idx[None, :]) + sigma2 * np.eye(win, dtype=np.complex128)
        b = r_lag(k - w_idx)
        try:
            out[k] = b @ np.linalg.solve(A, y[w_idx])
        except np.linalg.LinAlgError:
            out[k] = y[k]
    return out


def windowed_lmmse_reuse(y: np.ndarray, r_delta: np.ndarray, sigma2: float, win: int) -> np.ndarray:
    """Same windowed Hermitian-Toeplitz LMMSE as windowed_lmmse, but exploiting
    that the window matrix A[i,j]=r(|i-j|)+sigma2*delta is SHIFT-INVARIANT (depends
    only on i-j, not on the window position): invert A ONCE, then each subcarrier
    is out[k] = b_k^T (A^-1 y_w) -- an O(win^2) mat-vec instead of an O(win^3) solve.
    Numerically identical to windowed_lmmse (inv vs solve roundoff ~1e-12). This is
    the offline proof of the C Stage1 weight-reuse optimization."""
    n = y.size
    half = win // 2
    out = np.empty(n, dtype=np.complex128)
    idx_all = np.arange(n)

    def r_lag(lag):
        a = np.abs(lag)
        return np.where(lag >= 0, r_delta[a], np.conj(r_delta[a]))

    base = np.arange(win)
    A = r_lag(base[:, None] - base[None, :]) + sigma2 * np.eye(win, dtype=np.complex128)
    try:
        Ainv = np.linalg.inv(A)
    except np.linalg.LinAlgError:
        return windowed_lmmse(y, r_delta, sigma2, win)  # fallback
    for k in range(n):
        start = min(max(k - half, 0), n - win)
        w_idx = idx_all[start:start + win]
        b = r_lag(k - w_idx)
        out[k] = b @ (Ainv @ y[w_idx])
    return out


def windowed_lmmse_comb(ls_pilots: np.ndarray, R: np.ndarray, noise_norm: float,
                        k_tc: int, num_pilots: int, win: int,
                        use_cache: bool = False) -> np.ndarray:
    """Faithful mirror of the C Stage3 comb-pilot windowed Wiener-Hopf solve
    (nr_srs_mmse_freq_filter): targets on a comb grid (k_tc spacing), W-pilot
    window, A[r,c]=Rget((r-c)*k_tc)+noise*delta inverted once, per-target
    rhs=conj(R_hy), out=sum conj(w)*ls.

    use_cache=True implements Option A: for INTERIOR subcarriers (no center/start
    clamp) the weight vector depends only on offset%k_tc -> lazily cache k_tc weight
    vectors and reuse. Edge subcarriers are computed individually. Bit-identical to
    use_cache=False (same deterministic mat-vec, just reused), proving the residue
    keying + edge split.  R is length max_dk+1 (R[|dk|], Hermitian)."""
    max_dk = R.size - 1

    def Rget(d: int) -> complex:
        a = abs(d)
        if a > max_dk:
            return 0.0 + 0.0j
        return R[a] if d >= 0 else np.conj(R[a])

    A = np.empty((win, win), dtype=np.complex128)
    for r in range(win):
        for c in range(win):
            A[r, c] = Rget((r - c) * k_tc)
        A[r, r] += noise_norm
    Ainv = np.linalg.inv(A)

    span = num_pilots * k_tc
    out = np.zeros(span, dtype=np.complex128)
    wcache = [None] * k_tc

    def build_w(offset: int, start: int) -> np.ndarray:
        rhs = np.empty(win, dtype=np.complex128)
        for row in range(win):
            tdk = offset - (start + row) * k_tc
            at = abs(tdk)
            if at > max_dk:
                rhs[row] = 0.0 + 0.0j
            else:
                rhs[row] = np.conj(R[at]) if tdk >= 0 else R[at]
        return Ainv @ rhs

    for offset in range(span):
        center_u = (offset + k_tc // 2) // k_tc
        center = min(max(center_u, 0), num_pilots - 1)
        start_u = center - win // 2
        start = min(max(start_u, 0), num_pilots - win)
        interior = (center == center_u) and (start == start_u)
        s = offset % k_tc
        if use_cache and interior and wcache[s] is not None:
            w = wcache[s]
        else:
            w = build_w(offset, start)
            if use_cache and interior:
                wcache[s] = w
        acc = 0.0 + 0.0j
        for idx in range(win):
            acc += np.conj(w[idx]) * ls_pilots[start + idx]
        out[offset] = acc
    return out


def esprit_reconstruct_r(Y: np.ndarray, n_sc: int, sigma2: float,
                         p_model: int, sub_m: int, p_mult: float) -> tuple[np.ndarray, int]:
    """Parametric R via ESPRIT delay estimation -> R(dk)=sum_p p_d e^{-j2pi dk tau_d/N}.

    Spatial-smoothing + forward-backward covariance over subarrays (size M) and
    all frames; ESPRIT rotational invariance -> off-grid delays (leakage-free);
    LS per-frame amplitudes -> average power -> R. (Validated fragile on wide
    delay spread; kept for the record / --true2d comparison.)
    """
    frames = Y.shape[0]
    M = sub_m
    R = np.zeros((M, M), dtype=np.complex128)
    snaps = 0
    for f in range(frames):
        S = np.lib.stride_tricks.sliding_window_view(Y[f], M).T
        R += S @ S.conj().T
        snaps += S.shape[1]
    R /= max(snaps, 1)
    J = np.fliplr(np.eye(M))
    R = 0.5 * (R + J @ R.conj() @ J)
    w, V = np.linalg.eigh(R)
    order = np.argsort(w)[::-1]
    w = w[order].real
    V = V[:, order]
    if p_model > 0:
        P = min(p_model, M - 2)
    else:
        noise_lvl = float(np.median(w[(3 * M) // 4:]))
        P = max(1, min(int(np.sum(w > noise_lvl * p_mult)), M // 3))
    Us = V[:, :P]
    psi = np.linalg.pinv(Us[:-1, :]) @ Us[1:, :]
    tau_bins = (-np.angle(np.linalg.eigvals(psi)) * n_sc / (2.0 * np.pi)) % n_sc
    kcol = np.arange(n_sc)[:, None]
    A = np.exp(-2j * np.pi * kcol * (tau_bins[None, :]) / n_sc)
    Ainv = np.linalg.pinv(A)
    pwv = np.zeros(P, dtype=np.float64)
    for f in range(frames):
        pwv += np.abs(Ainv @ Y[f]) ** 2
    pwv = np.maximum(pwv / frames - sigma2, 0.0)
    dk = np.arange(n_sc)[:, None]
    r_delta = (np.exp(-2j * np.pi * dk * tau_bins[None, :] / n_sc) @ pwv).astype(np.complex128)
    return r_delta, P


def r_from_pdp(Hf_frame_history_ema: np.ndarray) -> np.ndarray:
    """R(dk) = FT{cleaned PDP} from one EMA'd delay-power vector (Wiener-Khinchin)."""
    floor = robust_noise_floor(Hf_frame_history_ema, k=3.0)
    return np.fft.fft(np.maximum(Hf_frame_history_ema - floor, 0.0))


def _band_median_power(v: np.ndarray, n_bands: int = 8) -> float:
    n = v.size
    bsz = (n + n_bands - 1) // n_bands
    vals = [float(np.mean(np.abs(v[b * bsz:(b + 1) * bsz]) ** 2))
            for b in range(n_bands) if v[b * bsz:(b + 1) * bsz].size]
    return float(np.median(vals)) if vals else 0.0


def time_ewma_persc(Hf: np.ndarray, alpha: float) -> np.ndarray:
    """Causal per-SC complex EWMA (no phase prediction)."""
    out = np.empty_like(Hf)
    s = Hf[0].copy()
    out[0] = s
    for f in range(1, Hf.shape[0]):
        s = alpha * s + (1.0 - alpha) * Hf[f]
        out[f] = s
    return out


def time_derot_ewma(Hf: np.ndarray, alpha: float) -> np.ndarray:
    """Replicate C nr_srs_2d_filter global-scalar de-rotation EWMA, output NOT
    re-rotated (mirrors the current C behaviour / bug)."""
    out = np.empty_like(Hf)
    st = Hf[0].copy()
    out[0] = st
    for f in range(1, Hf.shape[0]):
        obs = Hf[f]
        rot = np.sum(obs * np.conj(st))
        rot = rot / (np.abs(rot) + 1e-30)
        st = st + (1.0 - alpha) * (obs * np.conj(rot) - st)
        out[f] = st
    return out


def time_pkf_link(Hf: np.ndarray, k_val: float, k_w: float) -> np.ndarray:
    """Phase-predictive tracker: per-link common omega + per-SC complex state,
    output current-frame (re-rotated) estimate. Fixed gains."""
    out = np.empty_like(Hf)
    s = Hf[0].copy()
    w = 0.0
    out[0] = s
    for f in range(1, Hf.shape[0]):
        obs = Hf[f]
        w = w + k_w * (np.angle(np.sum(obs * np.conj(s)) + 1e-30) - w)
        pred = s * np.exp(1j * w)
        s = pred + k_val * (obs - pred)
        out[f] = s
    return out


def time_pkf_riccati(Hf: np.ndarray, r_meas: float, k_w: float = 0.2,
                     kmin: float = 0.02, kmax: float = 0.95,
                     q_ema: float = 0.1, innov_ema: float = 0.15) -> np.ndarray:
    """PKF + IAE Riccati adaptive gain given measurement noise r_meas.
    P_pred=P+Q; K=P_pred/(P_pred+R); Q=EMA(innov-P-R); needs a real R (C port
    must feed R from the freq stage residual-noise estimate)."""
    out = np.empty_like(Hf)
    s = Hf[0].copy()
    w = 0.0
    P = r_meas
    Q = r_meas * 0.01
    innov_smooth = None
    out[0] = s
    for f in range(1, Hf.shape[0]):
        obs = Hf[f]
        w = w + k_w * (np.angle(np.sum(obs * np.conj(s)) + 1e-30) - w)
        pred = s * np.exp(1j * w)
        innov = obs - pred
        ip = _band_median_power(innov)
        innov_smooth = ip if innov_smooth is None else (1.0 - innov_ema) * innov_smooth + innov_ema * ip
        P_pred = P + Q
        K = float(np.clip(P_pred / (P_pred + r_meas + 1e-30), kmin, kmax))
        Q = (1.0 - q_ema) * Q + q_ema * max(innov_smooth - P - r_meas, r_meas * 1e-3)
        s = pred + K * innov
        P = (1.0 - K) * P_pred
        out[f] = s
    return out


def time_pkf_predict(Hf: np.ndarray, r_meas: float, horizons,
                     k_w: float = 0.2, kmin: float = 0.02, kmax: float = 0.95,
                     q_ema: float = 0.1, innov_ema: float = 0.15,
                     phase_mode: int = 0, n_bands: int = 8, band_min: int = 8,
                     slope_max: float = 0.2, ramp_gate: float = 1.5
                     ) -> tuple[np.ndarray, dict, dict]:
    """Phase-predictive Kalman with multi-step look-ahead prediction (Direction A).

    Runs the SAME PKF + IAE-Riccati recursion as ``time_pkf_riccati`` (per-link
    phase-rate + per-SC complex state). After each frame's UPDATE it snapshots the
    converged state and EXTRAPOLATES ``k`` frames ahead WITHOUT any observation.

    phase_mode mirrors the C ``update_pkf_core`` E1 phase model (SRS_2D_PKF_PHASE):
        0 = scalar omega:      H_pred(f0+k) = s(f0)*exp(j*omega*k)         (default,
                               bit-identical to the original mode0 implementation)
        1 = per-band omega:    each of n_bands freq sub-bands gets its own omega_b
                               -> exp(j*omega_b*k); bands with < band_min SC fall
                               back to the global omega.
        2 = linear phase:      phase(m) = omega0 + slope*(m - m_center), slope =
                               EMA of the per-active-SC linear phase ramp (the STO
                               ramp), de-ramped intercept + ramp gate (ramp_gate).
    Magnitude is held constant in all modes.

    Returns:
        filt:  (F, n_act)            on-time filtered estimate.
        preds: dict{k: (F, n_act)}   preds[k][f] = k-step prediction of frame f
                                     (from the state at f-k). Rows f<k = NaN.
        zoh:   dict{k: (F, n_act)}   hold-last baseline zoh[k][f] = s(f-k).
    """
    horizons = sorted({int(k) for k in horizons if int(k) >= 1})
    F = Hf.shape[0]
    na = Hf.shape[1]
    filt = np.empty_like(Hf)
    preds = {k: np.full_like(Hf, np.nan) for k in horizons}
    zoh = {k: np.full_like(Hf, np.nan) for k in horizons}

    # Per-band layout over the n_act active SC (mode1).
    wnb = max(1, min(n_bands, na)) if phase_mode == 1 else 1
    wbsz = (na + wnb - 1) // wnb
    bidx = np.minimum(np.arange(na) // wbsz, wnb - 1)   # band index per active SC
    m = np.arange(na, dtype=np.float64)                 # active-SC index
    mc = 0.5 * (na - 1)                                 # band center

    s = Hf[0].copy()
    w = 0.0
    w_band = np.zeros(n_bands)
    slope = 0.0
    P = r_meas
    Q = r_meas * 0.01
    innov_smooth = None
    filt[0] = s
    # snapshots used to launch predictions: (s, omega, omega_band, slope, apply_ramp)
    state_hist = [(s.copy(), w, w_band.copy(), slope, False)]

    for f in range(1, F):
        obs = Hf[f]
        p = obs * np.conj(s)                            # per-SC frame-to-frame rotation
        inner = np.sum(p)
        dphi = np.angle(inner + 1e-30)
        apply_ramp = False

        if phase_mode < 2:
            w = w + k_w * (dphi - w)
        if phase_mode == 1:
            for b in range(wnb):
                sel = (bidx == b)
                if int(np.count_nonzero(sel)) >= band_min:
                    dphi_b = np.angle(np.sum(p[sel]) + 1e-30)
                    w_band[b] += k_w * (dphi_b - w_band[b])
                else:
                    w_band[b] = w
        if phase_mode >= 2:
            # slope = angle(sum_adjacent p[i]*conj(p[i-1])) (wrap-free), EMA + clip
            cross = np.sum(p[1:] * np.conj(p[:-1])) if na > 1 else 0.0
            slope_meas = float(np.angle(cross)) if np.abs(cross) > 0 else 0.0
            slope += k_w * (slope_meas - slope)
            slope = float(np.clip(slope, -slope_max, slope_max))
            deramp = np.exp(-1j * slope * (m - mc))
            icpt = np.sum(p * deramp)
            if np.abs(icpt) > ramp_gate * np.abs(inner):
                w = (1.0 - k_w) * w + k_w * float(np.angle(icpt))
                apply_ramp = True
            else:
                w = w + k_w * (dphi - w)

        # predict step (filtering): rotate state per-mode, then Riccati update.
        if phase_mode >= 2:
            eff_slope = slope if apply_ramp else 0.0
            rot = np.exp(1j * (w + eff_slope * (m - mc)))
        elif phase_mode == 1:
            rot = np.exp(1j * w_band[bidx])
        else:
            rot = np.exp(1j * w)
        pred = s * rot
        innov = obs - pred
        ip = _band_median_power(innov)
        innov_smooth = ip if innov_smooth is None else (1.0 - innov_ema) * innov_smooth + innov_ema * ip
        P_pred = P + Q
        K = float(np.clip(P_pred / (P_pred + r_meas + 1e-30), kmin, kmax))
        Q = (1.0 - q_ema) * Q + q_ema * max(innov_smooth - P - r_meas, r_meas * 1e-3)
        s = pred + K * innov
        P = (1.0 - K) * P_pred
        filt[f] = s
        state_hist.append((s.copy(), w, w_band.copy(), slope, apply_ramp))

    # Launch look-ahead predictions from every frame's post-update state, rotating
    # the per-mode phase by k steps.
    for f0 in range(F):
        s0, w0, wb0, sl0, ar0 = state_hist[f0]
        if phase_mode == 1:
            rot1 = np.exp(1j * wb0[bidx])               # per-SC per-band rate
        elif phase_mode >= 2:
            eff_slope = sl0 if ar0 else 0.0
            ramp = eff_slope * (m - mc)
        for k in horizons:
            tgt = f0 + k
            if tgt < F:
                if phase_mode == 1:
                    preds[k][tgt] = s0 * (rot1 ** k)              # per-band: exp(j*w_b*k)
                elif phase_mode >= 2:
                    preds[k][tgt] = s0 * np.exp(1j * (w0 * k + ramp * k))  # linear phase
                else:
                    preds[k][tgt] = s0 * np.exp(1j * w0 * k)      # scalar omega
                zoh[k][tgt] = s0                                  # hold-last baseline
    return filt, preds, zoh


def _wiener_coeffs(r: np.ndarray, p: int, horizons, diag_load: float) -> dict:
    """Solve the Wiener-Hopf normal equations R a = p_k for each horizon k.
    R[i,j]=r(i-j) Hermitian Toeplitz (+ diagonal loading), p_k[i]=r(k+i).
    R+load is Hermitian positive-definite -> the C port uses Cholesky; here
    np.linalg.solve is the float64 reference."""
    idx = np.arange(p)
    lag = idx[:, None] - idx[None, :]
    R = np.where(lag >= 0, r[np.abs(lag)], np.conj(r[np.abs(lag)]))
    r0 = r[0].real if r[0].real > 0 else 1.0
    R = R + diag_load * r0 * np.eye(p, dtype=np.complex128)
    out = {}
    for k in horizons:
        pk = r[k + idx]
        try:
            a = np.linalg.solve(R, pk)
        except np.linalg.LinAlgError:
            a = np.zeros(p, dtype=np.complex128)
            a[0] = 1.0
        out[k] = a
    return out


def _param_R(r: np.ndarray, maxlag: int, fit_lags: int = 6) -> np.ndarray:
    """Parametric (Doppler-model) reconstruction of the temporal autocorrelation
    from a noisy estimate r[d]. Models the channel by ONE dominant Doppler shift
    (phase) + an exponential magnitude decay (Doppler spread): a 3-parameter model
       r_param(d) = c * exp(-lam*d) * exp(j*omega0*d)
    drastically fewer DOF than per-lag estimates -> far lower variance, and it
    extrapolates cleanly to large lags.
      omega0 = angle( sum_{d>=1} r[d] conj(r[d-1]) )    (robust phase increment)
      c, lam : LS exp-decay fit of |r[d]| (magnitude is rotation-invariant)."""
    D = min(maxlag, max(1, fit_lags))
    num = 0.0 + 0.0j
    for d in range(1, D + 1):
        num += r[d] * np.conj(r[d - 1])
    om = float(np.angle(num)) if np.abs(num) > 0 else 0.0
    mags = np.abs(r[:D + 1])
    r0 = mags[0] if mags[0] > 0 else 1.0
    ds = np.arange(D + 1)
    valid = mags > 1e-12 * r0
    if int(valid.sum()) >= 2:
        A = np.vstack([np.ones(int(valid.sum())), -ds[valid].astype(float)]).T
        y = np.log(mags[valid])
        sol, _res, _rk, _sv = np.linalg.lstsq(A, y, rcond=None)
        c = float(np.exp(sol[0]))
        lam = max(float(sol[1]), 0.0)
    else:
        c, lam = float(r0), 0.0
    d_all = np.arange(maxlag + 1, dtype=np.float64)
    return c * np.exp(-lam * d_all) * np.exp(1j * om * d_all)


def _psd_R(r: np.ndarray, maxlag: int, nfft: int = 0) -> np.ndarray:
    """PSD-projection denoise of a noisy autocorrelation estimate r[d].

    A sequence is a valid autocorrelation iff its power spectrum (DFT of the
    two-sided Hermitian extension) is non-negative everywhere (Wiener-Khinchin /
    Bochner). A noisy EMA estimate violates this -> the Wiener-Hopf Toeplitz
    solve becomes ill-conditioned and the predictor "blows up". We project back
    onto the valid set: build S = DFT{r(-L..L)}, clip S<0 to 0, inverse-DFT.
    The result is the closest (in spectral L2) sequence with a non-negative PSD
    -> a positive-semidefinite Toeplitz R, exactly the property the genie r has.
    Unlike the single-Doppler ``_param_R`` this adds NO shape bias (multi-cluster
    CDL is preserved); it only removes the spectral negativity that is pure noise.
    """
    L = len(r) - 1
    if nfft <= 0:
        nfft = 1
        while nfft < 8 * (L + 1):
            nfft *= 2
    c = np.zeros(nfft, dtype=np.complex128)
    c[0] = r[0]
    for d in range(1, L + 1):
        c[d] = r[d]
        c[nfft - d] = np.conj(r[d])
    S = np.fft.fft(c).real
    S = np.clip(S, 0.0, None)
    c2 = np.fft.ifft(S)
    out = np.zeros(maxlag + 1, dtype=np.complex128)
    m = min(maxlag, nfft - 1)
    out[: m + 1] = c2[: m + 1]
    return out


def _kline_R(r: np.ndarray, maxlag: int, k_comp: int = 3, nfft: int = 0) -> np.ndarray:
    """Multi-component (K-line) Doppler reconstruction of the autocorrelation.

    Generalises ``_param_R`` (which is a single Doppler line + exp decay) to a
    sum of K dominant Doppler components -- the physically correct picture for a
    multi-cluster CDL channel (each cluster -> its own net Doppler). We obtain it
    non-parametrically from the spectrum: PSD = DFT{Hermitian r}, clip negatives
    (validity, as in _psd_R), keep only the K strongest spectral bins (the K
    dominant Doppler shifts), zero the rest, inverse-DFT. K=1 ~ single-Doppler;
    K->all = full PSD projection. This denoises like PSD while imposing a
    low-rank (few-Doppler) structure -> fewer DOF, lower variance.
    """
    L = len(r) - 1
    if nfft <= 0:
        nfft = 1
        while nfft < 8 * (L + 1):
            nfft *= 2
    c = np.zeros(nfft, dtype=np.complex128)
    c[0] = r[0]
    for d in range(1, L + 1):
        c[d] = r[d]
        c[nfft - d] = np.conj(r[d])
    S = np.clip(np.fft.fft(c).real, 0.0, None)
    if k_comp > 0 and k_comp < nfft:
        keep = np.argpartition(S, -k_comp)[-k_comp:]
        mask = np.zeros(nfft, dtype=bool)
        mask[keep] = True
        S = np.where(mask, S, 0.0)
    c2 = np.fft.ifft(S)
    out = np.zeros(maxlag + 1, dtype=np.complex128)
    m = min(maxlag, nfft - 1)
    out[: m + 1] = c2[: m + 1]
    return out


def time_wiener_predict(Hf: np.ndarray, horizons, p_order: int = 4,
                        diag_load: float = 0.2, online: bool = True,
                        r_ema: float = 0.05, r_param: bool = False,
                        r_psd: bool = False, r_kline: int = 0,
                        r_truth: np.ndarray | None = None) -> dict:
    """Temporal Wiener (linear-MMSE) predictor — the classical prediction ceiling.

    Unlike the phase-only PKF (rotate last sample by e^{jwk}, magnitude frozen),
    this predicts the future channel as a LINEAR combination of the last p samples:

        h_hat(t+k) = sum_{j=0}^{p-1} a_j^(k) * h(t-j)

    coefficients from the Wiener-Hopf equations on the per-link complex temporal
    autocorrelation r(d)=E[h(t)*conj(h(t-d))] (averaged over subcarriers). It
    captures BOTH the common-phase rotation AND amplitude/multi-lag dynamics (the
    part phase-only models cannot), so it is >= mode0 and is the optimal LINEAR
    predictor. preds[k][f] = k-step prediction of frame f from {f-k..f-k-(p-1)}.

    online=True (deployable / C-faithful): r(d) is estimated CAUSALLY by EMA from
      past frames only (no genie), and the prediction launched from frame t uses
      the r available at t. r_ema = EMA gain.
    online=False: r(d) estimated over the whole window (genie statistics) -> a
      mild upper bound, kept for reference / gap-to-deployable comparison.

    r_truth (genie-R isolation): when given (a clean/noise-free channel array of
      the same shape as Hf), the autocorrelation r(d) is computed from r_truth's
      TRUE statistics over the whole window (zero estimation variance), while the
      predictions are still launched from the NOISY ``Hf`` inputs. This isolates
      exactly the R-estimation-variance gap that the parametric model targets:
      Wiener-EMA / Wiener-param / Wiener-genie all predict from the same Hf and
      differ only in how r(d) is obtained. Overrides ``online``/``r_param``.
    """
    horizons = sorted({int(k) for k in horizons if int(k) >= 1})
    F, na = Hf.shape
    p = max(1, int(p_order))
    kmax = max(horizons) if horizons else 1
    maxlag = p - 1 + kmax
    preds = {k: np.full_like(Hf, np.nan) for k in horizons}

    if r_truth is not None:
        # Genie autocorrelation from the TRUE channel; predict from noisy Hf.
        r = np.zeros(maxlag + 1, dtype=np.complex128)
        for d in range(maxlag + 1):
            if F - d > 0:
                r[d] = np.mean(r_truth[d:] * np.conj(r_truth[:F - d]))
        coeffs = _wiener_coeffs(r, p, horizons, diag_load)
        for k in horizons:
            a = coeffs[k]
            for f in range(k + p - 1, F):
                acc = np.zeros(na, dtype=np.complex128)
                for j in range(p):
                    acc += a[j] * Hf[f - k - j]
                preds[k][f] = acc
        return preds

    if not online:
        # Batch (genie) autocorrelation over the whole window.
        r = np.zeros(maxlag + 1, dtype=np.complex128)
        for d in range(maxlag + 1):
            if F - d > 0:
                r[d] = np.mean(Hf[d:] * np.conj(Hf[:F - d]))
        if r_param:
            r = _param_R(r, maxlag)
        elif r_kline > 0:
            r = _kline_R(r, maxlag, r_kline)
        elif r_psd:
            r = _psd_R(r, maxlag)
        coeffs = _wiener_coeffs(r, p, horizons, diag_load)
        for k in horizons:
            a = coeffs[k]
            for f in range(k + p - 1, F):
                acc = np.zeros(na, dtype=np.complex128)
                for j in range(p):
                    acc += a[j] * Hf[f - k - j]
                preds[k][f] = acc
        return preds

    # Causal online EMA: walk frames, update r(d) from past only, predict from t.
    r = np.zeros(maxlag + 1, dtype=np.complex128)
    r_init = np.zeros(maxlag + 1, dtype=bool)
    for t in range(F):
        dmax = min(t, maxlag)
        for d in range(dmax + 1):
            inst = np.mean(Hf[t] * np.conj(Hf[t - d]))
            if not r_init[d]:
                r[d] = inst
                r_init[d] = True
            else:
                r[d] = (1.0 - r_ema) * r[d] + r_ema * inst
        if t < p - 1:
            continue
        if r_param:
            r_use = _param_R(r, maxlag)
        elif r_kline > 0:
            r_use = _kline_R(r, maxlag, r_kline)
        elif r_psd:
            r_use = _psd_R(r, maxlag)
        else:
            r_use = r
        coeffs = _wiener_coeffs(r_use, p, horizons, diag_load)
        for k in horizons:
            tgt = t + k
            if tgt < F:
                a = coeffs[k]
                acc = np.zeros(na, dtype=np.complex128)
                for j in range(p):
                    acc += a[j] * Hf[t - j]
                preds[k][tgt] = acc
    return preds


def freq_propagated_noise_avg(r_delta: np.ndarray, sigma2: float, win: int, n_sc: int,
                              n_probe: int = 64) -> float:
    """Data-driven measurement-noise R_meas for the time stage = the freq stage's
    PROPAGATED WHITE NOISE: each LMMSE output is w^H y with w=A^-1 b (A=R_ww+
    sigma2 I, b=r_{k,w}); pilots carry independent noise sigma2, so the output
    white-noise variance is sigma2*||w||^2. Average over SC. This is the part the
    time filter can remove (the time-constant freq bias is NOT noise and must NOT
    be in R). Uses only R (from PDP) and sigma2 -> fully data-driven (no GT).
    """
    half = win // 2
    idx_all = np.arange(n_sc)

    def r_lag(lag):
        a = np.abs(lag)
        return np.where(lag >= 0, r_delta[a], np.conj(r_delta[a]))

    acc, cnt = 0.0, 0
    step = max(1, n_sc // n_probe)
    for k in range(0, n_sc, step):
        start = min(max(k - half, 0), n_sc - win)
        w_idx = idx_all[start:start + win]
        A = r_lag(w_idx[:, None] - w_idx[None, :]) + sigma2 * np.eye(win, dtype=np.complex128)
        b = r_lag(k - w_idx)
        try:
            # est = b^T A^-1 y => weight vector is (b^T A^-1)^T = A^-1 conj(b)
            # for complex Hermitian A (NOT A^-1 b — that aligns with the noise
            # subspace and over-estimates the gain by orders of magnitude).
            w = np.linalg.solve(A, np.conj(b))
        except np.linalg.LinAlgError:
            continue
        acc += sigma2 * float(np.real(np.conj(w) @ w))
        cnt += 1
    return max(acc / max(cnt, 1), 1e-12)


def freq_stage_toeplitz(Y: np.ndarray, n_sc: int, sigma2: float, win: int,
                        alpha: float, floor_k: float,
                        r_genie: np.ndarray | None = None
                        ) -> tuple[np.ndarray, np.ndarray]:
    """Per-frame windowed Toeplitz freq LMMSE; data R (FT{cleaned EMA PDP}) or
    genie R. Returns (Hf, r_last) where r_last is the converged R (for the
    data-driven posterior-MMSE R_meas)."""
    out = np.empty_like(Y)
    pdp_ema = None
    r = r_genie if r_genie is not None else np.zeros(n_sc, dtype=np.complex128)
    for f in range(Y.shape[0]):
        yf = Y[f]
        if r_genie is None:
            inst = np.abs(np.fft.ifft(yf)) ** 2
            pdp_ema = inst.copy() if pdp_ema is None else alpha * pdp_ema + (1.0 - alpha) * inst
            r = np.fft.fft(np.maximum(pdp_ema - robust_noise_floor(pdp_ema, k=floor_k), 0.0))
        out[f] = windowed_lmmse(yf, r, sigma2, win)
    return out, r


def pct(arr: np.ndarray) -> str:
    p10, p50, p90 = np.percentile(arr, [10, 50, 90])
    return f"[p10={p10:+.2f}, p50={p50:+.2f}, p90={p90:+.2f}]"


def run_real_data(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    gt_dir = run_dir / "sionna_gt"
    print(f"[load] {run_dir}")
    h_srs, meta = load_srs(str(run_dir), skip_first=args.skip_first)
    gt_slots, gt_refs = index_gt_slots(str(gt_dir), srs_symbol=args.srs_symbol, skip_first=args.skip_first)
    si, gi, gap = align_frames(meta["abs_slots"], gt_slots, tol=args.tol)
    if len(si) < 2:
        raise RuntimeError("too few paired SRS/GT frames")

    print(f"  paired={len(si)} median_gap={gap}")
    h_s = h_srs[si]
    h_g = load_gt_by_refs(gt_refs, gi)
    if args.max_frames is not None and args.max_frames > 0 and h_s.shape[0] > args.max_frames:
        print(f"  [max-frames] truncating {h_s.shape[0]} -> {args.max_frames} paired frames")
        h_s = h_s[:args.max_frames]
        h_g = h_g[:args.max_frames]
    active = get_active_mask(h_s)
    order = active_circular_order(active)
    print(f"  active SC: {order.size}/{meta['n_sc']} first={order[0]} last={order[-1]}")

    ma = smooth_active_complex(h_s, order, args.ma_win)
    data_wiener, data_diag = data_pdp_wiener_active(h_s, order, args.noise_mode)
    topk_results = {
        k: nmse_ls(data_topk_delay_active(h_s, order, k), h_g, active)
        for k in args.topk_list
    }
    energy_est, energy_diag = data_energy_delay_active(h_s, order, args.energy_frac)
    energy_nmse = nmse_ls(energy_est, h_g, active)
    wiener = oracle_pdp_wiener_active(h_s, h_g, order)

    raw_nmse = nmse_ls(h_s, h_g, active)
    ma_nmse = nmse_ls(ma, h_g, active)
    data_nmse = nmse_ls(data_wiener, h_g, active)
    w_nmse = nmse_ls(wiener, h_g, active)

    print()
    print("mode                  global_nmse   per_ant_nmse     per-frame p10/p50/p90")
    print("----------------------------------------------------------------------------")
    print(f"raw_input             {raw_nmse['global_dB']:+10.2f}   {raw_nmse['per_ant_dB']:+12.2f}   {pct(raw_nmse['pf_pa'])}")
    print(f"ma{args.ma_win:<19d} {ma_nmse['global_dB']:+10.2f}   {ma_nmse['per_ant_dB']:+12.2f}   {pct(ma_nmse['pf_pa'])}")
    print(f"data_pdp_wiener       {data_nmse['global_dB']:+10.2f}   {data_nmse['per_ant_dB']:+12.2f}   {pct(data_nmse['pf_pa'])}")
    for k, res in topk_results.items():
        print(f"data_delay_top{k:<7d} {res['global_dB']:+10.2f}   {res['per_ant_dB']:+12.2f}   {pct(res['pf_pa'])}")
    print(f"data_delay_energy     {energy_nmse['global_dB']:+10.2f}   {energy_nmse['per_ant_dB']:+12.2f}   {pct(energy_nmse['pf_pa'])}")
    print(f"oracle_pdp_wiener     {w_nmse['global_dB']:+10.2f}   {w_nmse['per_ant_dB']:+12.2f}   {pct(w_nmse['pf_pa'])}")
    print()
    print(f"=== EMA-PDP rank-Wiener sweep (rel_db={args.ema_rel_db}, gamma={args.rank_gamma}, noise={args.ema_noise_mode}) ===")
    print("alpha   global_nmse   per_ant_nmse   gain_nz%   rankK(med/p90)   per-frame p10/p50/p90")
    print("--------------------------------------------------------------------------------------")
    best = None
    for alpha in args.ema_alpha_list:
        est, diag = data_pdp_ema_wiener_active(h_s, order, alpha, args.ema_rel_db,
                                               args.rank_gamma, args.ema_noise_mode,
                                               adaptive=args.ema_adaptive, floor_k=args.ema_floor_k,
                                               auto_gamma=args.ema_auto_gamma, p_fa=args.ema_pfa)
        res = nmse_ls(est, h_g, active)
        print(f"{alpha:<6.3f}  {res['global_dB']:+10.2f}   {res['per_ant_dB']:+12.2f}   "
              f"{100.0 * diag['gain_nonzero']:6.2f}   {diag['rank_median']:5.0f}/{diag['rank_p90']:<5.0f}   "
              f"{pct(res['pf_pa'])}")
        if best is None or res['per_ant_dB'] < best[1]:
            best = (alpha, res['per_ant_dB'])
    if best is not None:
        gate = "PASS" if best[1] <= -30.0 else "below target"
        print(f"\n[Gate 1] best EMA-Wiener per-ant = {best[1]:+.2f} dB @ alpha={best[0]} "
              f"(target <= -30 dB: {gate})")
    if args.cport:
        est_c, dense_idx, cdiag = data_ema_wiener_cport(
            h_s, order, args.k_tc, args.cport_alpha, args.ema_pfa, args.ema_floor_k,
            ifft_size=args.cport_nfft)
        dense_mask = np.zeros(meta["n_sc"], dtype=bool)
        dense_mask[dense_idx] = True
        rc = nmse_ls(est_c, h_g, dense_mask)
        print()
        print(f"=== C-faithful pilot recipe (idft2048 + dense recon, k_tc={args.k_tc}, "
              f"alpha={args.cport_alpha}) ===")
        print(f"  pilots={order.size} -> dense={cdiag['n_dense']}  rankK(med)={cdiag['rank_median']:.0f}  "
              f"gain_nz={100.0 * cdiag['gain_nonzero']:.1f}%")
        print(f"  cport_wiener  global={rc['global_dB']:+.2f}  per_ant={rc['per_ant_dB']:+.2f}  "
              f"{pct(rc['pf_pa'])}")
    print()
    print("data-PDP diagnostics:")
    print(f"  noise_delay_median={data_diag['noise_delay_median']:.6g}")
    print(f"  gain_mean={data_diag['gain_mean']:.4f} gain_p90={data_diag['gain_p90']:.4f} "
          f"gain_nonzero={100.0 * data_diag['gain_nonzero']:.1f}%")
    print(f"  energy_topk: frac={args.energy_frac:.3f} k_median={energy_diag['k_median']:.1f} "
          f"k_range=[{energy_diag['k_min']:.0f},{energy_diag['k_max']:.0f}]")

    return 0


@dataclass
class CaseResult:
    name: str
    raw: float
    ma: float
    wiener: float
    passed: bool


def run_case(name: str, n_sc: int, delays: np.ndarray, taps: np.ndarray, snr_db: float, ma_win: int, rng: np.random.Generator) -> CaseResult:
    h = freq_response_from_pdp(n_sc, delays, taps)
    sig_power = np.mean(np.abs(h) ** 2)
    sigma2 = sig_power / (10.0 ** (snr_db / 10.0)) if np.isfinite(snr_db) else 0.0
    noise = np.sqrt(sigma2 / 2.0) * (rng.standard_normal(n_sc) + 1j * rng.standard_normal(n_sc))
    y = h + noise

    powers = np.abs(taps) ** 2
    r_delta = correlation_from_pdp(n_sc, delays, powers)

    raw_nmse = nmse_db(y, h)
    ma_nmse = nmse_db(moving_average(y.real, ma_win) + 1j * moving_average(y.imag, ma_win), h)
    w_nmse = nmse_db(lmmse_fullband(y, r_delta, sigma2), h)

    # Basic acceptance: Wiener should improve raw and should not be worse than
    # MA by more than 0.5 dB on these oracle-PDP synthetic checks.
    passed = w_nmse < raw_nmse and w_nmse <= ma_nmse + 0.5
    return CaseResult(name, raw_nmse, ma_nmse, w_nmse, passed)


def run_identity_case(n_sc: int, rng: np.random.Generator) -> CaseResult:
    delays = np.array([0, 7, 23])
    taps = np.array([1.0 + 0.2j, 0.4 - 0.1j, 0.2 + 0.3j])
    h = freq_response_from_pdp(n_sc, delays, taps)
    r_delta = correlation_from_pdp(n_sc, delays, np.abs(taps) ** 2)
    est = lmmse_fullband(h, r_delta, 0.0)
    w_nmse = nmse_db(est, h)
    return CaseResult("identity_sigma0", -300.0, -300.0, w_nmse, w_nmse < -80.0)


def run_pure_noise_case(n_sc: int, rng: np.random.Generator) -> CaseResult:
    sigma2 = 1.0
    y = np.sqrt(sigma2 / 2.0) * (rng.standard_normal(n_sc) + 1j * rng.standard_normal(n_sc))
    r_delta = np.zeros(n_sc, dtype=np.complex128)
    est = lmmse_fullband(y, r_delta, sigma2)
    out_power_db = 10.0 * np.log10(np.mean(np.abs(est) ** 2) + 1e-30)
    return CaseResult("pure_noise_zero_prior", 0.0, 0.0, out_power_db, out_power_db < -80.0)


def run_synth_multiframe(args: argparse.Namespace) -> int:
    """Multi-frame synthetic check: does EMA rank-Wiener keep multipath in
    frequency-selective channels (where MA collapses), without GT?

    Builds F frames of a (slowly time-varying) channel + AWGN, then compares
    raw / MA / EMA-rank-Wiener / per-frame oracle LMMSE. This is the only
    frequency-selective evidence available until CDL real-data attach works.
    """
    rng = np.random.default_rng(args.seed)
    n_sc = args.n_sc
    n_frames = args.synth_frames
    order = np.arange(n_sc)

    def nmse_all(est, ref):
        return 10.0 * np.log10(np.mean(np.abs(est - ref) ** 2) / (np.mean(np.abs(ref) ** 2) + 1e-30) + 1e-30)

    cases = [
        ("flat_1tap", np.array([0]), np.array([1.0 + 0j])),
        ("fsel_2tap", np.array([0, 11]), np.array([1.0 + 0j, 0.6 + 0.2j])),
        ("fsel_4tap", np.array([0, 5, 17, 31]), np.array([1.0, 0.5 - 0.2j, 0.3 + 0.1j, 0.2j])),
    ]

    print(f"=== synthetic multi-frame (n_sc={n_sc}, frames={n_frames}, snr={args.snr_db} dB, "
          f"ma_win={args.ma_win}, alpha={args.synth_alpha}, rel_db={args.ema_rel_db}) ===")
    print("case          raw      ma       ema_wiener   oracle    rankK(med)")
    print("-------------------------------------------------------------------")
    rc = 0
    for name, delays, taps in cases:
        cur = taps.astype(np.complex128).copy()
        r_delta = correlation_from_pdp(n_sc, delays, np.abs(taps) ** 2)
        H = np.zeros((n_frames, n_sc), dtype=np.complex128)
        for f in range(n_frames):
            cur = cur * np.exp(1j * args.synth_doppler * rng.standard_normal(len(taps)))
            H[f] = freq_response_from_pdp(n_sc, delays, cur)  # delays fixed, tap phases drift
        sig_power = np.mean(np.abs(H) ** 2)
        sigma2 = sig_power / (10.0 ** (args.snr_db / 10.0))
        noise = np.sqrt(sigma2 / 2.0) * (rng.standard_normal((n_frames, n_sc)) + 1j * rng.standard_normal((n_frames, n_sc)))
        Y = H + noise

        Y4 = Y[:, None, None, :]
        H4 = H[:, None, None, :]

        ma = smooth_active_complex(Y4, order, args.ma_win)
        ema, diag = data_pdp_ema_wiener_active(Y4, order, args.synth_alpha, args.ema_rel_db,
                                               args.rank_gamma, args.ema_noise_mode,
                                               adaptive=args.ema_adaptive, floor_k=args.ema_floor_k,
                                               auto_gamma=args.ema_auto_gamma, p_fa=args.ema_pfa)
        oracle = np.empty_like(Y)
        for f in range(n_frames):
            oracle[f] = lmmse_fullband(Y[f], r_delta, sigma2)

        raw_db = nmse_all(Y, H)
        ma_db = nmse_all(ma[:, 0, 0, :], H)
        ema_db = nmse_all(ema[:, 0, 0, :], H)
        or_db = nmse_all(oracle, H)
        flag = "" if ema_db <= ma_db + 0.5 else "  <-- EMA worse than MA!"
        if ema_db > ma_db + 0.5:
            rc = 1
        print(f"{name:12s} {raw_db:7.2f}  {ma_db:7.2f}  {ema_db:9.2f}   {or_db:7.2f}   {diag['rank_median']:.0f}{flag}")

    print("\n[Synth Gate] EMA rank-Wiener should be >= MA-0.5dB on every case "
          f"(esp. freq-selective): {'PASS' if rc == 0 else 'FAIL'}")
    return rc


def run_cdl_offline(args: argparse.Namespace) -> int:
    """Offline CDL-A..E validation from ray data (no OAI attach needed).

    Builds the per-link frequency channel directly from CDL (tau, power) rays,
    mapping physical delay to the real SRS band (n_act*scs). This is the only
    way to validate frequency-selective generalization while CDL end-to-end
    attach (FAIL-ATTACH-UL) is unresolved. Compares raw / MA / EMA-rank-Wiener
    / per-frame oracle LMMSE.
    """
    base = Path(args.cdl_dir)
    models = sorted(p for p in base.glob("cdl_*")
                    if p.is_dir() and (p / "tau_rays_for_ChannelBlock.npy").exists())
    if not models:
        raise RuntimeError(f"no cdl_* ray dirs under {base}")

    rng = np.random.default_rng(args.seed)
    n_sc = args.n_sc
    n_frames = args.synth_frames
    order = np.arange(n_sc)
    band_bw = 1248.0 * 30e3  # real SRS active band: 1248 SC * 30 kHz = 37.44 MHz
    k = np.arange(n_sc)[:, None]

    def nmse_all(est, ref):
        return 10.0 * np.log10(np.mean(np.abs(est - ref) ** 2) / (np.mean(np.abs(ref) ** 2) + 1e-30) + 1e-30)

    print(f"=== CDL offline (n_sc={n_sc}, frames={n_frames}, snr={args.snr_db} dB, "
          f"ma_win={args.ma_win}, alpha={args.synth_alpha}, rel_db={args.ema_rel_db}, "
          f"doppler={args.synth_doppler}) ===")
    print("model     DS(ns)  raw      ma       ema_wiener   od_diag  data_lmmse  oracle   rankK   ema-MA")
    print("---------------------------------------------------------------------------------------------")
    rc = 0
    for mdir in models:
        tau = np.load(mdir / "tau_rays_for_ChannelBlock.npy").ravel().astype(np.float64)
        pw = np.load(mdir / "power_rays_for_ChannelBlock.npy").ravel().astype(np.float64)
        pw = pw / (pw.sum() + 1e-30)
        mean_t = np.sum(pw * tau)
        ds_ns = np.sqrt(np.sum(pw * (tau - mean_t) ** 2)) * 1e9

        d_bins = tau * band_bw  # delay in cycles across the band == bin index on n_sc grid
        E = np.exp(-2j * np.pi * k * (d_bins[None, :] / n_sc))  # (n_sc, n_rays)
        a = np.sqrt(pw) * np.exp(1j * rng.uniform(0.0, 2.0 * np.pi, pw.size))

        H = np.empty((n_frames, n_sc), dtype=np.complex128)
        for f in range(n_frames):
            a = a * np.exp(1j * args.synth_doppler * rng.standard_normal(pw.size))
            H[f] = E @ a

        sig_power = np.mean(np.abs(H) ** 2)
        sigma2 = sig_power / (10.0 ** (args.snr_db / 10.0))
        noise = np.sqrt(sigma2 / 2.0) * (rng.standard_normal((n_frames, n_sc)) + 1j * rng.standard_normal((n_frames, n_sc)))
        Y = H + noise
        Y4 = Y[:, None, None, :]

        ma = smooth_active_complex(Y4, order, args.ma_win)
        ema, diag = data_pdp_ema_wiener_active(Y4, order, args.synth_alpha, args.ema_rel_db,
                                               args.rank_gamma, args.ema_noise_mode,
                                               adaptive=args.ema_adaptive, floor_k=args.ema_floor_k,
                                               auto_gamma=args.ema_auto_gamma, p_fa=args.ema_pfa)
        # Full Toeplitz LMMSE is O(n^3); skip for large grids (use od_diag as the
        # practical ceiling there, since exact R is genie-only anyway).
        oracle = None
        if n_sc <= args.cdl_oracle_max_nsc:
            r_delta = correlation_from_pdp(n_sc, d_bins, pw)
            oracle = np.empty_like(Y)
            for f in range(n_frames):
                oracle[f] = lmmse_fullband(Y[f], r_delta, sigma2)

        # data LMMSE: causal EMA of the empirical frequency autocorrelation
        # (averaged over subcarriers + frames -> low variance), genie sigma2,
        # full Toeplitz LMMSE per frame. Tests whether a DATA-estimated R_f can
        # reach the Toeplitz oracle (the path beyond diagonal delay-Wiener).
        dl = None
        if args.cdl_lmmse and n_sc <= args.cdl_oracle_max_nsc:
            dl = np.empty_like(Y)
            r_ema = None
            a_em = args.synth_alpha
            Lw = min(args.cdl_lmmse_lags, n_sc)
            lag_win = np.zeros(n_sc)                              # half-Hann lag taper (PSD-safe)
            lag_win[:Lw] = 0.5 * (1.0 + np.cos(np.pi * np.arange(Lw) / Lw))
            for f in range(n_frames):
                yf = Y[f]
                Yp = np.fft.fft(yf, n=2 * n_sc)
                acf = np.fft.ifft(np.abs(Yp) ** 2)[:n_sc] / float(n_sc)  # biased -> PSD
                r_ema = acf.copy() if r_ema is None else a_em * r_ema + (1.0 - a_em) * acf
                r_use = r_ema * lag_win
                r_use[0] = max(r_use[0].real - sigma2, 1e-12)    # remove noise on lag-0 (genie)
                dl[f] = lmmse_fullband(yf, r_use, sigma2)

        # oracle DIAGONAL (DFT-delay-basis) Wiener: true clean PDP on the grid,
        # per-tap gain. Upper bound of ANY diagonal delay-domain estimator;
        # gap vs Toeplitz oracle = the off-grid DFT-leakage penalty.
        clean_delay = np.fft.ifft(H, axis=-1)
        p_grid = np.mean(np.abs(clean_delay) ** 2, axis=0)
        noise_tap = sigma2 / n_sc
        gain_od = p_grid / (p_grid + noise_tap + 1e-30)
        od = np.fft.fft(np.fft.ifft(Y, axis=-1) * gain_od[None, :], axis=-1)

        raw_db = nmse_all(Y, H)
        ma_db = nmse_all(ma[:, 0, 0, :], H)
        ema_db = nmse_all(ema[:, 0, 0, :], H)
        od_db = nmse_all(od, H)
        or_db = nmse_all(oracle, H) if oracle is not None else float("nan")
        dl_db = nmse_all(dl, H) if dl is not None else float("nan")
        delta = ema_db - ma_db
        flag = "OK" if ema_db <= ma_db + 0.5 else "EMA<MA"
        if ema_db > ma_db + 0.5:
            rc = 1
        print(f"{mdir.name:9s} {ds_ns:6.1f}  {raw_db:7.2f}  {ma_db:7.2f}  {ema_db:9.2f}   "
              f"{od_db:8.2f}  {dl_db:8.2f}  {or_db:7.2f}   {diag['rank_median']:5.0f}    {delta:+6.2f} {flag}")

    print(f"\n[CDL Gate] EMA rank-Wiener >= MA-0.5dB on every CDL model: {'PASS' if rc == 0 else 'FAIL'}")
    print("(od = oracle DIAGONAL delay-Wiener upper bound; oracle = full Toeplitz LMMSE)")
    return rc


def run_true2d(args: argparse.Namespace) -> int:
    """Freq-stage ceiling study on CDL: diagonal vs windowed-Toeplitz(data R /
    ESPRIT R / genie R) vs full Toeplitz oracle. (Was test_true2d_wiener.py.)"""
    base = Path(args.cdl_dir)
    models = sorted(p for p in base.glob("cdl_*")
                    if p.is_dir() and (p / "tau_rays_for_ChannelBlock.npy").exists())
    if not models:
        raise RuntimeError(f"no cdl_* ray dirs under {base}")
    rng = np.random.default_rng(args.seed)
    n_sc, n_frames = args.n_sc, args.synth_frames
    band_bw = 1248.0 * 30e3
    sub_m = args.esprit_m if args.esprit_m > 0 else n_sc // 2

    def nmse(est, ref):
        return 10.0 * np.log10(np.mean(np.abs(est - ref) ** 2) / (np.mean(np.abs(ref) ** 2) + 1e-30) + 1e-30)

    print(f"=== TRUE-2D freq stage (n_sc={n_sc}, frames={n_frames}, snr={args.snr_db}, "
          f"win={args.win}, esprit_P={args.esprit_p or 'auto'} M={sub_m}) ===")
    print("model        DS(ns)   raw    od_diag  win_pdpR  win_esprit  win_genie  oracle   esP  esp-oracle")
    print("-----------------------------------------------------------------------------------------------")
    for mdir in models:
        H, d_bins, pw, ds_ns = synth_cdl_channel(mdir, n_sc, n_frames, args.synth_doppler,
                                                 rng, "randomwalk", band_bw)
        sigma2 = float(np.mean(np.abs(H) ** 2)) / (10.0 ** (args.snr_db / 10.0))
        noise = np.sqrt(sigma2 / 2.0) * (rng.standard_normal((n_frames, n_sc))
                                         + 1j * rng.standard_normal((n_frames, n_sc)))
        Y = H + noise
        r_genie = correlation_from_pdp(n_sc, d_bins, pw)
        p_grid = np.mean(np.abs(np.fft.ifft(H, axis=-1)) ** 2, axis=0)
        gain_od = p_grid / (p_grid + sigma2 / n_sc + 1e-30)
        r_esprit, esP = esprit_reconstruct_r(Y, n_sc, sigma2, args.esprit_p, sub_m, args.esprit_mult)

        est_od = np.empty_like(Y); est_pd = np.empty_like(Y); est_es = np.empty_like(Y)
        est_wg = np.empty_like(Y); est_or = np.empty_like(Y)
        pdp_ema = None
        for f in range(n_frames):
            yf = Y[f]
            est_od[f] = np.fft.fft(np.fft.ifft(yf) * gain_od)
            est_or[f] = lmmse_fullband(yf, r_genie, sigma2)
            est_wg[f] = windowed_lmmse(yf, r_genie, sigma2, args.win)
            est_es[f] = windowed_lmmse(yf, r_esprit, sigma2, args.win)
            inst = np.abs(np.fft.ifft(yf)) ** 2
            pdp_ema = inst.copy() if pdp_ema is None else args.synth_alpha * pdp_ema + (1.0 - args.synth_alpha) * inst
            est_pd[f] = windowed_lmmse(yf, np.fft.fft(np.maximum(pdp_ema - robust_noise_floor(pdp_ema, k=args.ema_floor_k), 0.0)), sigma2, args.win)

        od, pd, es = nmse(est_od, H), nmse(est_pd, H), nmse(est_es, H)
        wg, orc = nmse(est_wg, H), nmse(est_or, H)
        print(f"{mdir.name:11s} {ds_ns:6.1f}  {nmse(Y, H):6.2f}  {od:7.2f}  {pd:8.2f}  "
              f"{es:9.2f}  {wg:8.2f}  {orc:7.2f}  {esP:4d}   {es - orc:+6.2f}")
    print("\nwin_pdpR = data R (== C nr_srs_mmse_freq_filter); win_esprit = parametric (fragile on wide DS)")
    print("win_genie = genie R windowed; oracle = full Toeplitz (true-2D freq upper bound)")
    return 0


def run_cascade(args: argparse.Namespace) -> int:
    """Time-stage cascade on top of the Toeplitz freq stage: EWMA vs C-derot vs
    phase-predictive Kalman (fixed / Riccati adaptive). (Was test_2d_cascade.py.)"""
    base = Path(args.cdl_dir)
    models = sorted(p for p in base.glob("cdl_*")
                    if p.is_dir() and (p / "tau_rays_for_ChannelBlock.npy").exists())
    if not models:
        raise RuntimeError(f"no cdl_* ray dirs under {base}")
    rng = np.random.default_rng(args.seed)
    n_sc, n_frames = args.n_sc, args.synth_frames
    band_bw = 1248.0 * 30e3
    ewma_grid = [0.5, 0.7, 0.8, 0.9, 0.95]
    kval_grid, kw_grid = [0.2, 0.3, 0.5, 0.7], [0.1, 0.2, 0.4]

    def nmse(est, ref):
        return 10.0 * np.log10(np.mean(np.abs(est - ref) ** 2) / (np.mean(np.abs(ref) ** 2) + 1e-30) + 1e-30)

    print(f"=== 2D cascade: Toeplitz-freq x time-stage (n_sc={n_sc}, frames={n_frames}, "
          f"snr={args.snr_db}, win={args.win}, doppler={args.synth_doppler}, mode={args.doppler_mode}) ===")
    print("model        raw    f_data  +ewma  +derot  +pkf_fix  ricc_dataR  ricc_genieR | dataR-fix  Rd/Rg")
    print("---------------------------------------------------------------------------------------------------")
    for mdir in models:
        H, _, _, _ = synth_cdl_channel(mdir, n_sc, n_frames, args.synth_doppler,
                                       rng, args.doppler_mode, band_bw)
        sigma2 = float(np.mean(np.abs(H) ** 2)) / (10.0 ** (args.snr_db / 10.0))
        noise = np.sqrt(sigma2 / 2.0) * (rng.standard_normal((n_frames, n_sc))
                                         + 1j * rng.standard_normal((n_frames, n_sc)))
        Y = H + noise
        Hf, r_last = freq_stage_toeplitz(Y, n_sc, sigma2, args.win, args.synth_alpha, args.ema_floor_k)
        f_only = nmse(Hf, H)
        ewma_best = min(nmse(time_ewma_persc(Hf, al), H) for al in ewma_grid)
        derot_best = min(nmse(time_derot_ewma(Hf, al), H) for al in ewma_grid)
        pkf_fix = min(nmse(time_pkf_link(Hf, kv, kw), H) for kv in kval_grid for kw in kw_grid)
        # DATA-DRIVEN R_meas = freq-stage propagated white noise (sigma2*||b^T A^-1||^2);
        # genie residual var (incl. leakage bias) shown only for reference.
        r_data = freq_propagated_noise_avg(r_last, sigma2, args.win, n_sc)
        r_genie = float(np.mean(np.abs(Hf - H) ** 2))
        ricc_d = nmse(time_pkf_riccati(Hf, r_data), H)
        ricc_g = nmse(time_pkf_riccati(Hf, r_genie), H)
        print(f"{mdir.name:11s} {nmse(Y, H):6.2f} {f_only:7.2f} {ewma_best:6.2f} "
              f"{derot_best:6.2f} {pkf_fix:8.2f} {ricc_d:10.2f} {ricc_g:11.2f} | "
              f"{ricc_d - pkf_fix:+8.2f}  {r_data / (r_genie + 1e-30):5.2f}")
    print("\n+ewma per-SC EWMA(best a); +derot C global-derot(broken); +pkf_fix PKF best fixed;")
    print("ricc_dataR = Riccati w/ DATA-DRIVEN R_meas (sigma2*||b^T A^-1||^2, freq propagated noise)")
    print("ricc_genieR = Riccati w/ genie residual (ref); dataR-fix ~0 => fully data-driven adaptive K works")
    print("Rd/Rg = data R_meas / genie residual (≈1 minus the leakage-bias share)")
    return 0


# ===========================================================================
# Direction A: SRS channel-prediction sweep (horizon x speed x SRS period).
# Self-contained coherent-Doppler synthesis (CDL-C/E by RMS delay spread) so it
# runs without captured data; pass --cdl-dir for exact 3GPP rays instead.
# ===========================================================================

# Representative RMS delay spreads (seconds). CDL-C ~ 300 ns (urban),
# CDL-E ~ 2064 ns (long-delay), matching the enhancement-plan references.
PREDICT_DS_NS = {"cdl_c": 300.0, "cdl_e": 2064.0}


def speed_to_omega_per_frame(speed_kmh: float, period_slots: int, fc_hz: float,
                             slot_sec: float) -> float:
    """Max per-SRS-frame Doppler phase (rad). Couples speed AND SRS period:
        f_d = v * fc / c ;  omega = 2*pi*f_d*(period_slots*slot_sec).
    Sparser SRS (larger period) or higher speed both grow omega -> harder to
    predict, which is exactly the regime the study probes."""
    c = 299_792_458.0
    f_d = (speed_kmh / 3.6) * fc_hz / c
    return 2.0 * np.pi * f_d * (period_slots * slot_sec)


def synth_coherent_expdp(ds_ns: float, n_sc: int, n_frames: int,
                         omega_per_frame: float, band_bw: float,
                         rng: np.random.Generator, n_taps: int = 12,
                         angle_spread: float = 1.0) -> np.ndarray:
    """Coherent (constant-velocity) frequency-selective channel from an
    exponential PDP with RMS delay spread ds_ns. Each tap carries a constant
    per-frame Doppler omega_per_frame*cos(AoA) where the arrival angles span a
    FINITE spread (rad) about the motion direction -> a NET common Doppler (the
    physical Doppler shift of a moving UE, trackable by the scalar PKF) plus a
    spread (which de-correlates amplitude over long horizons = classical limit).
    angle_spread=pi reproduces the isotropic (zero-net-Doppler) case."""
    ds = ds_ns * 1e-9
    tau = np.sort(rng.exponential(ds, n_taps))
    tau = tau - tau.min()
    pw = np.exp(-tau / (ds + 1e-30))
    pw = pw / (pw.sum() + 1e-30)
    d_bins = tau * band_bw
    k = np.arange(n_sc)[:, None]
    E = np.exp(-2j * np.pi * k * (d_bins[None, :] / n_sc))
    a0 = np.sqrt(pw) * np.exp(1j * rng.uniform(0.0, 2.0 * np.pi, n_taps))
    ang = rng.uniform(-angle_spread, angle_spread, n_taps)
    omega_ray = omega_per_frame * np.cos(ang)
    H = np.empty((n_frames, n_sc), dtype=np.complex128)
    for f in range(n_frames):
        H[f] = E @ (a0 * np.exp(1j * omega_ray * f))
    return H


def run_predict_sweep(args: argparse.Namespace) -> int:
    """Direction A sweep: prediction NMSE vs (horizon, speed, SRS period) for
    CDL-C/E. Reports the classical-prediction knee (where phase extrapolation
    stops beating hold-last) and writes a CSV; optional PNG curves."""
    rng = np.random.default_rng(args.seed)
    n_sc = args.predict_nsc
    n_frames = args.predict_frames
    scs = args.predict_scs_khz * 1e3
    slot_sec = 1e-3 / (2 ** 1)            # mu=1 (30 kHz) -> 0.5 ms/slot
    band_bw = n_sc * scs
    horizons = args.predict_horizons
    speeds = args.predict_speeds
    periods = args.predict_periods
    snr_db = args.predict_snr

    cdl_dirs = {}
    if args.cdl_dir is not None:
        base = Path(args.cdl_dir)
        for name in PREDICT_DS_NS:
            d = base / name
            if (d / "tau_rays_for_ChannelBlock.npy").exists():
                cdl_dirs[name] = d

    def err_sig(est: np.ndarray, ref: np.ndarray, k: int) -> tuple[float, float]:
        m = np.arange(k, est.shape[0])
        return (float(np.sum(np.abs(est[m] - ref[m]) ** 2)),
                float(np.sum(np.abs(ref[m]) ** 2)))

    def err_sig_valid(est: np.ndarray, ref: np.ndarray) -> tuple[float, float]:
        # NaN-aware: only score frames the predictor actually produced (the
        # Wiener warm-up leaves the first k+p-1 frames NaN).
        mask = np.isfinite(est)
        return (float(np.sum(np.abs((est - ref)[mask]) ** 2)),
                float(np.sum(np.abs(ref[mask]) ** 2)))

    rows = []   # CSV rows: model, ds_ns, speed, period, omega, k, pred, zoh, filt
    print(f"=== Direction A prediction sweep (n_sc={n_sc}, frames={n_frames}, "
          f"trials={args.predict_trials}, snr={snr_db} dB, fc={args.predict_fc_ghz} GHz, "
          f"scs={args.predict_scs_khz} kHz) ===")
    print("model  DS(ns)  v(km/h)  period  omega/fr |  on-time |  k: pred/zoh (dB) ...  | knee")
    print("-" * 110)
    for model, ds_ns in PREDICT_DS_NS.items():
        for v in speeds:
            for period in periods:
                omega = speed_to_omega_per_frame(v, period, args.predict_fc_ghz * 1e9, slot_sec)
                # Aggregate err/sig over independent realizations for low-variance NMSE.
                pe = {k: 0.0 for k in horizons}; ze = {k: 0.0 for k in horizons}
                sg = {k: 0.0 for k in horizons}
                we = {k: 0.0 for k in horizons}   # Wiener causal-EMA r(d)
                wp = {k: 0.0 for k in horizons}   # Wiener parametric Doppler r(d)
                wq = {k: 0.0 for k in horizons}   # Wiener PSD-projected r(d)
                wg = {k: 0.0 for k in horizons}   # Wiener genie r(d) (true stats)
                wsg = {k: 0.0 for k in horizons}  # signal energy over scored Wiener frames
                fe = fs = 0.0
                ds_show = ds_ns
                for _trial in range(args.predict_trials):
                    if model in cdl_dirs:
                        H, _d, _p, ds_show = synth_cdl_channel(
                            cdl_dirs[model], n_sc, n_frames, omega, rng, "coherent", band_bw)
                    else:
                        H = synth_coherent_expdp(ds_ns, n_sc, n_frames, omega, band_bw, rng,
                                                 angle_spread=args.predict_angle_spread)
                    sig = float(np.mean(np.abs(H) ** 2))
                    sigma2 = sig / (10.0 ** (snr_db / 10.0))
                    noise = np.sqrt(sigma2 / 2.0) * (rng.standard_normal((n_frames, n_sc))
                                                     + 1j * rng.standard_normal((n_frames, n_sc)))
                    Y = H + noise
                    filt, preds, zoh = time_pkf_predict(Y, sigma2, horizons)
                    de, ds2 = err_sig(filt, H, 0); fe += de; fs += ds2
                    for k in horizons:
                        pek, sk = err_sig(preds[k], H, k); pe[k] += pek; sg[k] += sk
                        zek, _ = err_sig(zoh[k], H, k); ze[k] += zek
                    if args.predict_wiener:
                        w_ema = time_wiener_predict(
                            Y, horizons, p_order=args.predict_porder,
                            diag_load=args.predict_diag_load, online=True,
                            r_ema=args.predict_rema, r_param=False)
                        w_par = time_wiener_predict(
                            Y, horizons, p_order=args.predict_porder,
                            diag_load=args.predict_diag_load, online=True,
                            r_ema=args.predict_rema, r_param=True)
                        w_psd = time_wiener_predict(
                            Y, horizons, p_order=args.predict_porder,
                            diag_load=args.predict_diag_load, online=True,
                            r_ema=args.predict_rema, r_psd=True)
                        w_gen = time_wiener_predict(
                            Y, horizons, p_order=args.predict_porder,
                            diag_load=args.predict_diag_load, r_truth=H)
                        for k in horizons:
                            eek, ssk = err_sig_valid(w_ema[k], H); we[k] += eek; wsg[k] += ssk
                            wp[k] += err_sig_valid(w_par[k], H)[0]
                            wq[k] += err_sig_valid(w_psd[k], H)[0]
                            wg[k] += err_sig_valid(w_gen[k], H)[0]
                filt_db = 10.0 * np.log10(fe / (fs + 1e-30) + 1e-30)
                knee = None
                cells = []
                wcells = []
                for k in horizons:
                    pdb = 10.0 * np.log10(pe[k] / (sg[k] + 1e-30) + 1e-30)
                    zdb = 10.0 * np.log10(ze[k] / (sg[k] + 1e-30) + 1e-30)
                    if knee is None and (zdb - pdb) <= 0.0:
                        knee = k
                    cells.append(f"k{k}:{pdb:+.1f}/{zdb:+.1f}")
                    if args.predict_wiener:
                        wedb = 10.0 * np.log10(we[k] / (wsg[k] + 1e-30) + 1e-30)
                        wpdb = 10.0 * np.log10(wp[k] / (wsg[k] + 1e-30) + 1e-30)
                        wqdb = 10.0 * np.log10(wq[k] / (wsg[k] + 1e-30) + 1e-30)
                        wgdb = 10.0 * np.log10(wg[k] / (wsg[k] + 1e-30) + 1e-30)
                        wcells.append(f"k{k}:{wedb:+.1f}/{wpdb:+.1f}/{wqdb:+.1f}/{wgdb:+.1f}")
                    else:
                        wedb = wpdb = wqdb = wgdb = float("nan")
                    rows.append((model, ds_show, v, period, omega, k, pdb, zdb, filt_db,
                                 wedb, wpdb, wqdb, wgdb))
                knee_s = f"k={knee}" if knee is not None else ">K"
                print(f"{model:6s} {ds_show:6.0f}  {v:7.0f}  {period:6d}  {omega:7.3f} | "
                      f"{filt_db:+7.2f} | " + "  ".join(cells) + f" | {knee_s}")
                if args.predict_wiener:
                    print(f"{'  wiener EMA/param/psd/genie':>30s} | " + "  ".join(wcells))

    if args.predict_csv:
        import csv
        with open(args.predict_csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["model", "ds_ns", "speed_kmh", "period_slots", "omega_per_frame",
                        "horizon", "pred_nmse_db", "zoh_nmse_db", "filt_nmse_db",
                        "wiener_ema_db", "wiener_param_db", "wiener_psd_db",
                        "wiener_genie_db"])
            w.writerows(rows)
        print(f"\n[predict-sweep] wrote {len(rows)} rows -> {args.predict_csv}")

    if args.predict_plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            fig, axes = plt.subplots(1, len(PREDICT_DS_NS), figsize=(6 * len(PREDICT_DS_NS), 4.5),
                                     squeeze=False)
            for ax, (model, _ds) in zip(axes[0], PREDICT_DS_NS.items()):
                for v in speeds:
                    for period in periods:
                        ys = [r[6] for r in rows if r[0] == model and r[2] == v and r[3] == period]
                        if ys:
                            ax.plot(horizons, ys, marker="o", label=f"{v}km/h P{period}")
                ax.set_title(f"{model} prediction NMSE")
                ax.set_xlabel("horizon k (SRS frames)")
                ax.set_ylabel("NMSE (dB)")
                ax.grid(True, alpha=0.3)
                ax.legend(fontsize=7)
            fig.tight_layout()
            fig.savefig(args.predict_plot, dpi=120)
            print(f"[predict-sweep] wrote curves -> {args.predict_plot}")
        except Exception as exc:  # pragma: no cover - plotting is best-effort
            print(f"[predict-sweep] plot skipped: {exc}")

    print("\n(pred = PKF phase-extrapolation k frames ahead; zoh = hold-last baseline; "
          "knee = first horizon where pred no longer beats zoh -> classical limit / AI motivation.)")
    if args.predict_wiener:
        print("(wiener EMA/param/psd/genie = temporal Wiener NMSE with causal-EMA / parametric-Doppler "
              "/ PSD-projected / genie autocorrelation; all predict from the SAME noisy inputs. "
              "genie = R-estimation lower bound; psd = PSD-projection denoise (no shape bias); "
              "the psd-vs-genie gap left = residual estimation variance.)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", default=None, help="Optional real snr_XdB directory for SRS/GT offline test")
    ap.add_argument("--n-sc", type=int, default=128)
    ap.add_argument("--snr-db", type=float, default=15.0)
    ap.add_argument("--ma-win", type=int, default=33)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--srs-symbol", type=int, default=12)
    ap.add_argument("--skip-first", type=int, default=0)
    ap.add_argument("--max-frames", type=int, default=None,
                    help="Use only the first N paired frames (e.g. to skip degraded late frames)")
    ap.add_argument("--tol", type=int, default=20)
    ap.add_argument("--noise-mode", choices=["tail-median", "tail-p25", "tail-p25-exp", "tail-mean"],
                    default="tail-median")
    ap.add_argument("--ema-alpha-list", type=lambda s: [float(x) for x in s.split(",")],
                    default=[0.9, 0.95, 0.98, 0.99],
                    help="EMA alpha values to sweep for data_pdp_ema_wiener")
    ap.add_argument("--ema-rel-db", type=float, default=20.0,
                    help="Relative-to-peak keep threshold (dB) for EMA rank-Wiener tap selection")
    ap.add_argument("--synth-mf", action="store_true",
                    help="Run multi-frame synthetic freq-selective check (no run-dir needed)")
    ap.add_argument("--cdl-dir", type=str, default=None,
                    help="Base dir containing cdl_* ray dirs; runs offline CDL-A..E validation")
    ap.add_argument("--cdl-lmmse", action="store_true",
                    help="Also evaluate data-estimated frequency-correlation Toeplitz LMMSE (genie sigma2)")
    ap.add_argument("--cdl-lmmse-lags", type=int, default=64,
                    help="Lag-window length for data LMMSE autocorrelation taper")
    ap.add_argument("--cdl-oracle-max-nsc", type=int, default=600,
                    help="Skip O(n^3) Toeplitz oracle/data_lmmse above this n_sc")
    ap.add_argument("--synth-frames", type=int, default=200, help="Frames for --synth-mf")
    ap.add_argument("--synth-alpha", type=float, default=0.95, help="EMA alpha for --synth-mf")
    ap.add_argument("--synth-doppler", type=float, default=0.02,
                    help="Per-frame tap phase drift std (rad) for --synth-mf time variation")
    ap.add_argument("--rank-gamma", type=float, default=2.0,
                    help="Tap-keep threshold: keep delay taps with EMA power > floor*gamma")
    ap.add_argument("--ema-adaptive", action="store_true",
                    help="TRUE adaptive selector: robust data-driven noise floor + floor*gamma "
                         "(no fixed rel_db). K auto-adapts: flat->1, freq-selective->many")
    ap.add_argument("--ema-floor-k", type=float, default=3.0,
                    help="Iterative noise-floor trim factor (taps below k*floor count as noise)")
    ap.add_argument("--ema-auto-gamma", action="store_true",
                    help="FULLY adaptive: derive keep-threshold from measured noise DOF + p_fa "
                         "(no fixed gamma, no fixed rel_db)")
    ap.add_argument("--ema-pfa", type=float, default=None,
                    help="Per-tap false-alarm rate for auto-gamma (default 1/N_active)")
    ap.add_argument("--cport", action="store_true",
                    help="Run the C-faithful pilot recipe (dense-pilot idft2048 + dense recon)")
    ap.add_argument("--k-tc", type=int, default=2, help="SRS comb K_TC for --cport pilot grid")
    ap.add_argument("--cport-alpha", type=float, default=0.97, help="EMA alpha for --cport")
    ap.add_argument("--cport-nfft", type=int, default=0,
                    help="FFT size for --cport (0 = exact n_pilots, no zero-pad leakage)")
    ap.add_argument("--ema-noise-mode", choices=["tail-median", "tail-p25", "tail-p25-exp", "tail-mean"],
                    default="tail-mean",
                    help="Noise-floor estimator for the EMA rank-Wiener")
    ap.add_argument("--topk-list", type=lambda s: [int(x) for x in s.split(",")],
                    default=[1, 2, 4, 8, 16])
    ap.add_argument("--energy-frac", type=float, default=0.90)
    # --- true-2D freq stage + 2D cascade (consolidated from spin-off scripts) ---
    ap.add_argument("--true2d", action="store_true",
                    help="CDL freq-stage ceiling: diagonal vs windowed-Toeplitz(data/ESPRIT/genie) vs oracle")
    ap.add_argument("--cascade", action="store_true",
                    help="CDL time-stage cascade on Toeplitz freq: EWMA vs C-derot vs PKF(fixed/Riccati)")
    ap.add_argument("--win", type=int, default=64, help="banded Toeplitz window size (true2d/cascade)")
    ap.add_argument("--esprit-p", type=int, default=0, help="ESPRIT model order (0=auto)")
    ap.add_argument("--esprit-m", type=int, default=0, help="ESPRIT subarray size (0=n_sc//2)")
    ap.add_argument("--esprit-mult", type=float, default=4.0, help="auto-P eigenvalue threshold mult")
    ap.add_argument("--doppler-mode", choices=["randomwalk", "coherent"], default="coherent",
                    help="cascade Doppler model: coherent=predictable (constant velocity)")
    # --- Direction A: SRS channel-prediction sweep ---
    ap.add_argument("--predict-sweep", action="store_true",
                    help="Direction A: sweep prediction NMSE vs horizon x speed x SRS period "
                         "(CDL-C/E). Self-contained; pass --cdl-dir for exact 3GPP rays.")
    ap.add_argument("--predict-horizons", type=lambda s: [int(x) for x in s.split(",")],
                    default=[1, 2, 4, 8, 16, 32],
                    help="Look-ahead horizons k (SRS frames) to evaluate")
    ap.add_argument("--predict-speeds", type=lambda s: [float(x) for x in s.split(",")],
                    default=[10, 30, 60, 120], help="UE speeds (km/h) to sweep")
    ap.add_argument("--predict-periods", type=lambda s: [int(x) for x in s.split(",")],
                    default=[10, 40, 80], help="SRS_PERIOD_SLOTS values to sweep")
    ap.add_argument("--predict-snr", type=float, default=20.0, help="SNR (dB) for the sweep")
    ap.add_argument("--predict-nsc", type=int, default=256, help="subcarriers in the synthetic grid")
    ap.add_argument("--predict-frames", type=int, default=160, help="frames per sweep point")
    ap.add_argument("--predict-trials", type=int, default=8,
                    help="independent realizations averaged per sweep point (low-variance NMSE)")
    ap.add_argument("--predict-angle-spread", type=float, default=1.0,
                    help="arrival-angle half-spread (rad) about motion direction; "
                         "smaller -> stronger net Doppler (more predictable), pi -> isotropic")
    ap.add_argument("--predict-fc-ghz", type=float, default=3.5, help="carrier freq (GHz) for Doppler")
    ap.add_argument("--predict-scs-khz", type=float, default=30.0, help="subcarrier spacing (kHz)")
    ap.add_argument("--predict-csv", type=str, default=None, help="write sweep results to this CSV")
    ap.add_argument("--predict-plot", type=str, default=None, help="write NMSE-vs-horizon curves PNG")
    ap.add_argument("--predict-wiener", action="store_true",
                    help="Direction A §8.1: also evaluate the temporal Wiener predictor with "
                         "three autocorrelation sources -- causal EMA r(d), parametric Doppler "
                         "r(d), and genie r(d) (true stats, noisy inputs) -- to quantify how far "
                         "the parametric model closes the EMA->genie gap.")
    ap.add_argument("--predict-porder", type=int, default=4,
                    help="Wiener predictor AR order p (number of past frames used)")
    ap.add_argument("--predict-diag-load", type=float, default=0.2,
                    help="Diagonal loading (x r0) for the Wiener-Hopf solve")
    ap.add_argument("--predict-rema", type=float, default=0.05,
                    help="EMA gain for the causal online autocorrelation estimate")
    args = ap.parse_args()

    if args.predict_sweep:
        return run_predict_sweep(args)

    if args.cdl_dir is not None:
        if args.true2d:
            return run_true2d(args)
        if args.cascade:
            return run_cascade(args)
        return run_cdl_offline(args)

    if args.synth_mf:
        return run_synth_multiframe(args)

    if args.run_dir is not None:
        return run_real_data(args)

    rng = np.random.default_rng(args.seed)
    cases = [
        run_case("flat_1tap", args.n_sc, np.array([0]), np.array([1.0 + 0.0j]), args.snr_db, args.ma_win, rng),
        run_case("freq_selective_2tap", args.n_sc, np.array([0, 11]), np.array([1.0 + 0.0j, 0.6 + 0.2j]), args.snr_db, args.ma_win, rng),
        run_case("freq_selective_4tap", args.n_sc, np.array([0, 5, 17, 31]), np.array([1.0, 0.5 - 0.2j, 0.3 + 0.1j, 0.2j]), args.snr_db, args.ma_win, rng),
        run_identity_case(args.n_sc, rng),
        run_pure_noise_case(args.n_sc, rng),
    ]

    print("case                 raw_nmse    ma_nmse     wiener_metric  status")
    print("---------------------------------------------------------------------")
    for c in cases:
        print(f"{c.name:22s} {c.raw:9.2f}  {c.ma:9.2f}  {c.wiener:13.2f}  {'PASS' if c.passed else 'FAIL'}")

    return 0 if all(c.passed for c in cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
