#!/usr/bin/env python3
"""
lsl_m2_joint_process.py — M=2 LSL with Joint-Process Estimator (真正的 LRLS)

对比:
  - lsl_m2_reference.py 用 "u - f_M" (prediction error subtraction) → ~5 dB 天花板
  - 本文件用 joint-process estimator (Σ ρ_m · b_m) → 理论 ~15-20 dB

关键区别:
  joint-process 的 p_m = λ·p_m + b_m·conj(d)/γ_m 是指数加权递归累积,
  N_eff = 1/(1-λ), 跟 EWMA 一样有平均效应。

γ cold-start 问题的解决:
  前 N_WARM 个 slot 输出 Legacy (passthrough), 这期间 γ 从退化值逐渐稳定。
  warm-up 结束后 γ 已正常, joint-process 输出有效。
"""
from __future__ import annotations

import numpy as np
from dataclasses import dataclass, field

# ─── Constants ───
LSL_ORDER = 2
LSL_DELTA = 1e-6
LSL_DEFAULT_LAMBDA = 0.97
LSL_DEFAULT_N_WARM = 5
KAPPA_LIMIT_FACTOR = 0.99


@dataclass
class LSLState:
    b_prev:     np.ndarray = field(default_factory=lambda: np.zeros(LSL_ORDER, dtype=complex))
    Delta:      np.ndarray = field(default_factory=lambda: np.zeros(LSL_ORDER, dtype=complex))
    p:          np.ndarray = field(default_factory=lambda: np.zeros(LSL_ORDER + 1, dtype=complex))
    Ef:         np.ndarray = field(default_factory=lambda: np.full(LSL_ORDER + 1, LSL_DELTA))
    Eb:         np.ndarray = field(default_factory=lambda: np.full(LSL_ORDER + 1, LSL_DELTA))
    Eb_prev:    np.ndarray = field(default_factory=lambda: np.full(LSL_ORDER, LSL_DELTA))
    gamma:      np.ndarray = field(default_factory=lambda: np.ones(LSL_ORDER + 1))
    gamma_prev: np.ndarray = field(default_factory=lambda: np.ones(LSL_ORDER))
    n_updates:  int = 0


def lsl_one_step_joint(st: LSLState, u: complex,
                       lambda_: float = LSL_DEFAULT_LAMBDA,
                       n_warm: int = LSL_DEFAULT_N_WARM,
                       delta: float = LSL_DELTA) -> complex:
    """
    M=2 LSL with Joint-Process Estimator output.
    
    Key difference from prediction-error version:
      Output = Σ ρ_m · b_m  (exponentially-weighted accumulation via p_m)
      NOT u - f_M (which only uses M+1 samples)
    """
    d = u  # desired signal = input (self-prediction / noise suppression)

    # ── Order 0 ──
    b_arr = np.zeros(LSL_ORDER + 1, dtype=complex)
    f_arr = np.zeros(LSL_ORDER + 1, dtype=complex)
    
    f_arr[0] = u
    b_arr[0] = u
    E_new = lambda_ * st.Eb_prev[0] + abs(u) ** 2
    st.Ef[0] = E_new
    st.Eb[0] = E_new
    st.gamma[0] = 1.0  # γ_0 = 1 by convention

    # ── Order recursion m = 1, 2 ──
    for m in range(1, LSL_ORDER + 1):
        Eb_prev_lag = st.Eb_prev[m - 1]
        b_prev_lag = st.b_prev[m - 1]
        gam_prev = st.gamma_prev[m - 1]

        Eb_safe = max(Eb_prev_lag, delta)
        Ef_safe = max(st.Ef[m - 1], delta)
        gam_safe = max(gam_prev, delta)

        # Δ update (a-posteriori variant: includes /γ)
        st.Delta[m - 1] = (lambda_ * st.Delta[m - 1]
                           + b_prev_lag * np.conj(f_arr[m - 1]) / gam_safe)

        # Stability check
        D2 = abs(st.Delta[m - 1]) ** 2
        if D2 >= KAPPA_LIMIT_FACTOR * Ef_safe * Eb_safe:
            # Reset and fallback
            st.Delta[m - 1] = 0
            return u

        kappa_f = st.Delta[m - 1] / Eb_safe
        kappa_b = np.conj(st.Delta[m - 1]) / Ef_safe

        f_arr[m] = f_arr[m - 1] - kappa_f * b_prev_lag
        b_arr[m] = b_prev_lag - kappa_b * f_arr[m - 1]

        # Energy update
        st.Ef[m] = max(st.Ef[m - 1] - D2 / Eb_safe, delta)
        st.Eb[m] = max(Eb_prev_lag - D2 / Ef_safe, delta)

        # γ update (conversion factor)
        st.gamma[m] = max(gam_prev - abs(b_prev_lag) ** 2 / Eb_safe, delta)

    # ── Joint-Process Estimator (核心降噪) ──
    H_smooth = 0.0 + 0.0j
    for m in range(LSL_ORDER + 1):
        gam_safe = max(st.gamma[m], delta)
        Eb_safe = max(st.Eb[m], delta)
        # p_m 递归: 这就是指数加权平均! N_eff = 1/(1-λ)
        st.p[m] = lambda_ * st.p[m] + b_arr[m] * np.conj(d) / gam_safe
        rho_m = st.p[m] / Eb_safe
        H_smooth += rho_m * b_arr[m]

    # ── Roll state ──
    for m in range(LSL_ORDER):
        st.b_prev[m] = b_arr[m]
        st.Eb_prev[m] = st.Eb[m]
        st.gamma_prev[m] = st.gamma[m]

    # ── NaN check ──
    if not np.isfinite(H_smooth):
        return u

    # ── Warm-up ──
    st.n_updates += 1
    if st.n_updates <= n_warm:
        return u

    return H_smooth


def lsl_apply_joint(H_seq, lambda_=LSL_DEFAULT_LAMBDA, n_warm=LSL_DEFAULT_N_WARM):
    out = np.zeros_like(H_seq, dtype=complex)
    st = LSLState()
    for t in range(len(H_seq)):
        out[t] = lsl_one_step_joint(st, H_seq[t], lambda_, n_warm)
    return out


def ewma_apply(H_seq, alpha=0.97):
    out = np.zeros_like(H_seq, dtype=complex)
    out[0] = H_seq[0]
    for t in range(1, len(H_seq)):
        out[t] = alpha * out[t - 1] + (1 - alpha) * H_seq[t]
    return out


def nmse_db(H_est, H_true):
    err = np.mean(np.abs(H_est - H_true) ** 2)
    sig = np.mean(np.abs(H_true) ** 2)
    if sig <= 0:
        return float('nan')
    return 10.0 * np.log10(err / sig)


# ─── Tests ───
def main():
    print("=" * 70)
    print(" LSL M=2 Joint-Process vs EWMA vs Prediction-Error Comparison")
    print("=" * 70)

    np.random.seed(42)

    for scenario, n_slots, make_channel in [
        ("Static channel", 500, lambda n: np.full(n, 1.0 + 0.5j)),
        ("Slow AR(1) ρ=0.99", 500, None),
        ("Very slow AR(1) ρ=0.999", 1000, None),
    ]:
        if make_channel is not None:
            H_true = make_channel(n_slots)
        else:
            # AR(1) channel
            rho = float(scenario.split("ρ=")[1])
            H_true = np.zeros(n_slots, dtype=complex)
            H_true[0] = 1.0
            for t in range(1, n_slots):
                innov = (np.random.randn() + 1j * np.random.randn()) / np.sqrt(2)
                H_true[t] = rho * H_true[t - 1] + np.sqrt(1 - rho ** 2) * innov

        noise = 0.3 * (np.random.randn(n_slots) + 1j * np.random.randn(n_slots))
        H_noisy = H_true + noise

        skip = 50  # skip warm-up + transient

        nmse_in = nmse_db(H_noisy[skip:], H_true[skip:])

        print(f"\n{'─' * 70}")
        print(f" Scenario: {scenario} ({n_slots} slots, skip={skip})")
        print(f" Input NMSE: {nmse_in:+.2f} dB")
        print(f"{'─' * 70}")
        print(f" {'Method':<35} {'NMSE (dB)':>10} {'Gain (dB)':>10}")
        print(f" {'─' * 35} {'─' * 10} {'─' * 10}")

        # EWMA with various α
        for alpha in [0.9, 0.95, 0.97, 0.99]:
            H_ewma = ewma_apply(H_noisy, alpha)
            nmse_out = nmse_db(H_ewma[skip:], H_true[skip:])
            gain = nmse_in - nmse_out
            print(f" EWMA α={alpha:<28} {nmse_out:>+10.2f} {gain:>+10.2f}")

        print()

        # LSL Joint-Process with various λ
        for lam in [0.9, 0.95, 0.97, 0.99]:
            H_lsl = lsl_apply_joint(H_noisy, lambda_=lam, n_warm=5)
            nmse_out = nmse_db(H_lsl[skip:], H_true[skip:])
            gain = nmse_in - nmse_out
            print(f" LSL-Joint λ={lam:<25} {nmse_out:>+10.2f} {gain:>+10.2f}")

        print()

        # LSL Prediction-Error (old method) for comparison
        from lsl_m2_reference import lsl_apply
        H_pred = lsl_apply(H_noisy, lambda_=0.97, n_warm=5)
        nmse_pred = nmse_db(H_pred[skip:], H_true[skip:])
        gain_pred = nmse_in - nmse_pred
        print(f" LSL-PredErr λ=0.97 (old)           {nmse_pred:>+10.2f} {gain_pred:>+10.2f}")

    print(f"\n{'=' * 70}")
    print(" Done. Compare LSL-Joint vs EWMA — should be comparable now.")
    print("=" * 70)


if __name__ == "__main__":
    main()
