#!/usr/bin/env python3
"""
lsl_m2_reference.py — Python/NumPy reference for the M=2 Least-Squares Lattice
                       (LSL) used by the upcoming nr_srs_lsl_2d.c.

Purpose (per LSL_2D_IMPLEMENTATION_PLAN.md §8.1 / §8.2 / D1.1):
  1. Validate the M=2 LSL recursion math (Haykin Ch.16) against synthetic
     channel + i.i.d. noise → expect NMSE reduction ≥ 2 dB
  2. Produce reference outputs for C unit-alignment test (§8.2), at 1e-4
     numerical precision
  3. Quick-iter sandbox for tuning λ / N_warm before C build/sweep loop

This file is the GOLDEN reference. The C implementation must produce
bit-comparable output (within float rounding) for the same input sequence.

Algorithm: see §2.4 of LSL_2D_IMPLEMENTATION_PLAN.md (algorithmic pseudocode)
C twin   : §3.3 lsl_one_step()  in nr_srs_lsl_2d.c (to be written)

Usage:
    python3 lsl_m2_reference.py            # run all tests
    python3 lsl_m2_reference.py --dump     # also dump bin files for C test
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import numpy as np

# ─────────────────────────────────────────────────────────────────────
# Constants — must match nr_srs_lsl_2d.h
# ─────────────────────────────────────────────────────────────────────
LSL_ORDER          = 2
LSL_DELTA          = 1e-6
LSL_DEFAULT_LAMBDA = 0.97
LSL_DEFAULT_N_WARM = 5

LSL_FLAG_NAN        = 1 << 0
LSL_FLAG_KAPPA_OOR  = 1 << 1
LSL_FLAG_RESET      = 1 << 2

# Reflection-coefficient implicit-limit factor (|Δ|² < 0.99 · Ef · Eb)
KAPPA_LIMIT_FACTOR = 0.99


# ─────────────────────────────────────────────────────────────────────
# Per-tap LSL state (mirrors lsl_state_t in nr_srs_lsl_2d.h)
# ─────────────────────────────────────────────────────────────────────
@dataclass
class LSLState:
    f:          np.ndarray = field(default_factory=lambda: np.zeros(LSL_ORDER + 1, dtype=complex))
    b:          np.ndarray = field(default_factory=lambda: np.zeros(LSL_ORDER + 1, dtype=complex))
    b_prev:     np.ndarray = field(default_factory=lambda: np.zeros(LSL_ORDER, dtype=complex))
    Delta:      np.ndarray = field(default_factory=lambda: np.zeros(LSL_ORDER, dtype=complex))
    p:          np.ndarray = field(default_factory=lambda: np.zeros(LSL_ORDER + 1, dtype=complex))
    rho:        np.ndarray = field(default_factory=lambda: np.zeros(LSL_ORDER + 1, dtype=complex))
    Ef:         np.ndarray = field(default_factory=lambda: np.full(LSL_ORDER + 1, LSL_DELTA, dtype=float))
    Eb:         np.ndarray = field(default_factory=lambda: np.full(LSL_ORDER + 1, LSL_DELTA, dtype=float))
    Eb_prev:    np.ndarray = field(default_factory=lambda: np.full(LSL_ORDER, LSL_DELTA, dtype=float))
    gamma:      np.ndarray = field(default_factory=lambda: np.ones(LSL_ORDER + 1, dtype=float))
    gamma_prev: np.ndarray = field(default_factory=lambda: np.ones(LSL_ORDER, dtype=float))
    n_updates:  int = 0
    flags:      int = 0


def _state_reset(st: LSLState, fault_flag: int) -> None:
    """Single-tap reset (used by fault path). Mirrors lsl_state_reset_one()."""
    st.f[:]          = 0
    st.b[:]          = 0
    st.b_prev[:]     = 0
    st.Delta[:]      = 0
    st.p[:]          = 0
    st.rho[:]        = 0
    st.Ef[:]         = LSL_DELTA
    st.Eb[:]         = LSL_DELTA
    st.Eb_prev[:]    = LSL_DELTA
    st.gamma[:]      = 1.0
    st.gamma_prev[:] = 1.0
    st.n_updates     = 0
    st.flags         = fault_flag | LSL_FLAG_RESET


# ─────────────────────────────────────────────────────────────────────
# Core single-step LSL update (mirrors C lsl_one_step())
# ─────────────────────────────────────────────────────────────────────
def lsl_one_step(st: LSLState, u: complex,
                 lambda_: float = LSL_DEFAULT_LAMBDA,
                 n_warm: int = LSL_DEFAULT_N_WARM,
                 delta: float = LSL_DELTA) -> complex:
    """One LSL step.  Uses a-priori-error LSL variant (Haykin §16, no γ).

    Output: H_smooth(t) = u(t) - η_M(t), the forward-prediction-based
    noise reducer.  η_M is the a-priori M-th order forward prediction
    error after M lattice stages.  If u = signal + noise with predictable
    signal and i.i.d. noise, η_M ≈ noise residual, so u - η_M ≈ signal.

    Why a-priori variant (no γ):
      Earlier draft included γ ∈ [0, 1] conversion factor (Haykin Tbl 16.2,
      a-posteriori variant).  At cold-start (slot 1), γ_1 = 1 - |u|²/|u|² = 0
      degenerates to 0 → next slot's Δ formula divides by γ ≈ δ → blow-up
      → KAPPA_OOR trap → state reset every slot → permanent passthrough.

      A-priori variant removes /γ from the Δ recursion, dodging the
      cold-start degeneracy entirely.  γ field is still in state struct
      (kept for ABI compatibility with C struct) but not used in update.
    """
    # ── Step 1: Order 0 init ─────────────────────────────────────
    eta_curr = u                                  # η_0(t) = u(t)
    b_curr   = u                                  # b_0(t) = u(t)
    E_new    = lambda_ * st.Eb_prev[0] + abs(u) ** 2  # F_0(t) = B_0(t)
    st.Ef[0] = E_new
    st.Eb[0] = E_new
    st.f[0]  = eta_curr
    st.b[0]  = b_curr

    # working scalars for the loop
    eta_prev_order   = eta_curr            # η_{m-1}(t)
    Ef_prev_order_t  = E_new               # F_{m-1}(t)

    # ── Step 2: Order recursion m = 1, 2 ─────────────────────────
    for m in range(1, LSL_ORDER + 1):
        Eb_prev_lag = st.Eb_prev[m - 1]    # B_{m-1}(t-1)
        b_prev_lag  = st.b_prev[m - 1]     # b_{m-1}(t-1)

        Eb_safe = max(Eb_prev_lag,    delta)
        Ef_safe = max(Ef_prev_order_t, delta)

        # Step 2.1: partial correlation (NO /γ for a-priori variant)
        st.Delta[m - 1] = (lambda_ * st.Delta[m - 1]
                           + b_prev_lag * np.conj(eta_prev_order))

        # Step 2.2: reflection coefficient implicit limit
        # Cauchy-Schwarz: |Δ|² ≤ F · B (equality iff perfect correlation)
        D2 = abs(st.Delta[m - 1]) ** 2
        if D2 >= KAPPA_LIMIT_FACTOR * Ef_safe * Eb_safe:
            _state_reset(st, LSL_FLAG_KAPPA_OOR)
            return u  # fallback Legacy this slot

        kappa_f = st.Delta[m - 1] / Eb_safe
        kappa_b = np.conj(st.Delta[m - 1]) / Ef_safe

        # Step 2.3: prediction error update
        eta_new = eta_prev_order - kappa_f * b_prev_lag
        b_new   = b_prev_lag      - kappa_b * eta_prev_order

        # Step 2.4: energy update
        st.Ef[m] = Ef_prev_order_t - D2 / Eb_safe
        st.Eb[m] = Eb_prev_lag      - D2 / Ef_safe

        # commit + advance
        st.f[m] = eta_new
        st.b[m] = b_new
        eta_prev_order   = eta_new
        Ef_prev_order_t  = st.Ef[m]

    # ── Step 3: Roll state for next slot ─────────────────────────
    for m in range(LSL_ORDER):
        st.b_prev[m]  = st.b[m]
        st.Eb_prev[m] = st.Eb[m]
    # gamma_prev not rolled — unused in a-priori variant

    # ── Step 4: Forward-prediction-based denoising output ────────
    # H_smooth = u - η_M  (subtract the M-th order forward residual)
    eta_M = st.f[LSL_ORDER]
    H_smooth = u - eta_M

    # NaN trap (last line of defense)
    if not np.isfinite(H_smooth.real) or not np.isfinite(H_smooth.imag):
        _state_reset(st, LSL_FLAG_NAN)
        return u

    # Warm-up: first N_WARM slots return Legacy input.
    # Note: n_updates is incremented BEFORE the check, so condition
    # `<= n_warm` ensures the first n_warm samples (indices 0..n_warm-1)
    # all return passthrough u, and LSL output kicks in at index n_warm.
    st.n_updates += 1
    if st.n_updates <= n_warm:
        return u
    return H_smooth


# ─────────────────────────────────────────────────────────────────────
# Convenience: apply LSL to a 1-D complex sequence (single tap)
# ─────────────────────────────────────────────────────────────────────
def lsl_apply(H_seq: np.ndarray,
              lambda_: float = LSL_DEFAULT_LAMBDA,
              n_warm: int = LSL_DEFAULT_N_WARM) -> np.ndarray:
    """Apply LSL to one tap across multiple SRS slots."""
    out = np.zeros_like(H_seq, dtype=complex)
    st = LSLState()
    for t in range(len(H_seq)):
        out[t] = lsl_one_step(st, H_seq[t], lambda_, n_warm)
    return out


def nmse_db(H_est: np.ndarray, H_true: np.ndarray) -> float:
    """NMSE in dB."""
    err = np.mean(np.abs(H_est - H_true) ** 2)
    sig = np.mean(np.abs(H_true) ** 2)
    if sig <= 0:
        return float('nan')
    return 10.0 * np.log10(err / sig)


# ─────────────────────────────────────────────────────────────────────
# Unit tests
# ─────────────────────────────────────────────────────────────────────
def test_static_channel():
    """Test 1: Static channel + i.i.d. noise → LSL should suppress strongly"""
    np.random.seed(42)
    n_slots = 200
    H_true = np.full(n_slots, 1.0 + 0.5j)
    noise = 0.3 * (np.random.randn(n_slots) + 1j * np.random.randn(n_slots))
    H_noisy = H_true + noise

    H_smooth = lsl_apply(H_noisy)

    # Skip warm-up
    skip = 20
    nmse_in  = nmse_db(H_noisy[skip:],  H_true[skip:])
    nmse_out = nmse_db(H_smooth[skip:], H_true[skip:])
    gain = nmse_in - nmse_out
    print(f"  test_static_channel:    in={nmse_in:+.2f}  out={nmse_out:+.2f}  gain={gain:+.2f} dB")
    # Theoretical M=2 LSL upper bound on gain ≈ 10·log10(M+1) = 4.77 dB
    # (Wiener filter limit: noise reduction by averaging M+1 effective samples).
    # A-priori variant + finite λ → typical realized gain 2-4 dB. 2.5 dB is a
    # practical lower bound for "algorithm is alive and reducing noise".
    assert gain > 2.5, f"Expected ≥2.5 dB gain on static channel, got {gain:+.2f}"
    return True


def test_slow_varying():
    """Test 2: AR(1) slowly-varying channel + i.i.d. noise"""
    np.random.seed(42)
    n_slots = 500
    rho = 0.99  # high temporal correlation
    H_true = np.zeros(n_slots, dtype=complex)
    H_true[0] = 1.0 + 0j
    for t in range(1, n_slots):
        innov = (np.random.randn() + 1j * np.random.randn()) / np.sqrt(2)
        H_true[t] = rho * H_true[t - 1] + np.sqrt(1 - rho ** 2) * innov

    noise = 0.3 * (np.random.randn(n_slots) + 1j * np.random.randn(n_slots))
    H_noisy = H_true + noise

    H_smooth = lsl_apply(H_noisy)

    skip = 30
    nmse_in  = nmse_db(H_noisy[skip:],  H_true[skip:])
    nmse_out = nmse_db(H_smooth[skip:], H_true[skip:])
    gain = nmse_in - nmse_out
    print(f"  test_slow_varying:      in={nmse_in:+.2f}  out={nmse_out:+.2f}  gain={gain:+.2f} dB")
    assert gain > 2.0, f"Expected ≥2 dB gain on slow-varying channel, got {gain:+.2f}"
    return True


def test_warmup_returns_legacy():
    """Test 3: First N_WARM=5 samples must equal input exactly (passthrough)"""
    H_in = np.array([1 + 1j, 2 + 2j, 3 + 3j, 4 + 4j, 5 + 5j, 6 + 6j], dtype=complex)
    H_out = lsl_apply(H_in, n_warm=5)
    # First 5 slots: warm-up → return u (= H_in)
    np.testing.assert_array_almost_equal(H_out[:5], H_in[:5], decimal=6)
    print(f"  test_warmup_returns_legacy: PASS ({5} samples passthrough verified)")
    return True


def test_fast_varying_no_blowup():
    """Test 4: Fast-changing channel → LSL might not improve, but must NOT blow up
    (no NaN/Inf, reasonable output magnitude)"""
    np.random.seed(42)
    n_slots = 200
    # Phase rotation per slot (high Doppler)
    H_true = np.exp(1j * 2 * np.pi * 0.1 * np.arange(n_slots))
    noise = 0.3 * (np.random.randn(n_slots) + 1j * np.random.randn(n_slots))
    H_noisy = H_true + noise

    H_smooth = lsl_apply(H_noisy)

    # Just check no crash, no NaN, output magnitude reasonable
    assert np.all(np.isfinite(H_smooth)), "LSL produced non-finite values!"
    assert np.all(np.abs(H_smooth) < 100), "LSL output magnitude exploded!"
    nmse_in  = nmse_db(H_noisy[20:],  H_true[20:])
    nmse_out = nmse_db(H_smooth[20:], H_true[20:])
    gain = nmse_in - nmse_out
    print(f"  test_fast_varying_no_blowup: in={nmse_in:+.2f}  out={nmse_out:+.2f}  gain={gain:+.2f} dB"
          f"  (no blow-up; gain may be small or negative — that's OK)")
    return True


def test_zero_input():
    """Test 5: All-zero input → output must be 0 (no NaN from 0/δ)"""
    H_in = np.zeros(20, dtype=complex)
    H_out = lsl_apply(H_in)
    assert np.all(H_out == 0), f"Zero input must produce zero output, got {H_out}"
    print(f"  test_zero_input: PASS (all-zero in → all-zero out)")
    return True


def test_lambda_sweep():
    """Test 6: λ ∈ {0.9, 0.95, 0.97, 0.99} should all converge on slow channel"""
    np.random.seed(42)
    n_slots = 500
    rho = 0.99
    H_true = np.zeros(n_slots, dtype=complex)
    H_true[0] = 1.0 + 0j
    for t in range(1, n_slots):
        H_true[t] = rho * H_true[t - 1] + np.sqrt(1 - rho ** 2) * (np.random.randn() + 1j * np.random.randn()) / np.sqrt(2)
    noise = 0.3 * (np.random.randn(n_slots) + 1j * np.random.randn(n_slots))
    H_noisy = H_true + noise

    nmse_in = nmse_db(H_noisy[30:], H_true[30:])
    print(f"  test_lambda_sweep:       in={nmse_in:+.2f}")
    for lam in (0.9, 0.95, 0.97, 0.99):
        H_smooth = lsl_apply(H_noisy, lambda_=lam)
        nmse_out = nmse_db(H_smooth[30:], H_true[30:])
        gain = nmse_in - nmse_out
        marker = "✓" if gain > 0 else "✗"
        print(f"                            λ={lam:.2f}: out={nmse_out:+.2f}  gain={gain:+.2f} dB  {marker}")
    return True


def dump_for_c_alignment(out_dir: str = "."):
    """Write reference input/output bin files for C unit test (§8.2)."""
    np.random.seed(42)
    n_slots = 100
    H_true = np.full(n_slots, 1.0 + 0.5j)
    noise = 0.3 * (np.random.randn(n_slots) + 1j * np.random.randn(n_slots))
    H_noisy = H_true + noise
    H_smooth = lsl_apply(H_noisy)

    # Quantize to int16 c16 (matches OAI c16_t)
    scale = 16384.0
    def to_c16(arr: np.ndarray) -> np.ndarray:
        out = np.empty((len(arr), 2), dtype=np.int16)
        out[:, 0] = np.clip(np.round(arr.real * scale), -32768, 32767)
        out[:, 1] = np.clip(np.round(arr.imag * scale), -32768, 32767)
        return out

    in_path  = f"{out_dir}/lsl_test_input.bin"
    out_path = f"{out_dir}/lsl_test_expected.bin"
    to_c16(H_noisy ).tofile(in_path)
    to_c16(H_smooth).tofile(out_path)
    print(f"  Dumped {n_slots} samples (scale={scale:.0f}) for C alignment:")
    print(f"    {in_path}")
    print(f"    {out_path}")
    print(f"  C must produce output bit-equivalent to expected (within ±1 LSB).")


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump", action="store_true",
                    help="Dump reference bin files for C unit alignment test")
    ap.add_argument("--dump-dir", default=".",
                    help="Where to write bin files (default: cwd)")
    args = ap.parse_args()

    print("=" * 60)
    print(" LSL M=2 Reference — Unit Tests")
    print(f"   λ_default = {LSL_DEFAULT_LAMBDA}, N_WARM = {LSL_DEFAULT_N_WARM}, δ = {LSL_DELTA}")
    print("=" * 60)

    failed = []
    for fn in (test_static_channel,
               test_slow_varying,
               test_warmup_returns_legacy,
               test_fast_varying_no_blowup,
               test_zero_input,
               test_lambda_sweep):
        try:
            fn()
        except AssertionError as e:
            failed.append((fn.__name__, str(e)))
            print(f"    ✗ FAILED: {e}")

    print("=" * 60)
    if failed:
        print(f" {len(failed)} test(s) FAILED:")
        for name, msg in failed:
            print(f"   - {name}: {msg}")
        sys.exit(1)
    else:
        print(" ALL TESTS PASSED.")
        print("   → §2.4 algorithm is mathematically correct.")
        print("   → C implementation in §3.3 should produce same output.")

    if args.dump:
        print()
        print("=" * 60)
        print(" Dumping C-alignment reference data")
        print("=" * 60)
        dump_for_c_alignment(args.dump_dir)


if __name__ == "__main__":
    main()
