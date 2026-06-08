#!/usr/bin/env python3
"""Verification tests for the expanded StructuredChannelOutput.

Tests cover:
  1. Backward compatibility  — old 3-arg call still works, old field names present
  2. Field shapes             — p_inst, p_state, d_inst, d_state, h_taps, etc.
  3. Numeric ranges           — d_inst in [0,1], confidence in [0,1], p_state >= 0
  4. Static vs fast-varying   — d_inst ≈ 1 for static channel, lower for random
  5. Compression consistency  — h_taps → reconstruct → matches H_c
  6. EMA convergence          — p_state variance < p_inst variance over time
  7. MIMO mode                — R_rx_inst, R_rx_state, sv shapes
"""

import sys, os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from srs_2d_mmse import StructuredChannelOutput

PASS = 0
FAIL = 0

def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def test_backward_compat():
    """Old 3-arg call, old field names."""
    print("\n=== Test 1: Backward compatibility ===")
    n_sc = 128
    sco = StructuredChannelOutput(n_sc)
    H = np.random.randn(n_sc) + 1j * np.random.randn(n_sc)
    result = sco.extract(H, snr_db=10.0, rot_angle=0.1)
    check("returns tuple", result is not None and len(result) == 2)
    S, H_c = result
    check("S is dict", isinstance(S, dict))
    check("H_c is ndarray", isinstance(H_c, np.ndarray))
    check("'pdp' alias present", "pdp" in S)
    check("'rsrp' present", "rsrp" in S)
    check("'snr_db' present", "snr_db" in S)
    check("'doppler_hz' present", "doppler_hz" in S)
    check("'speed_ms' present", "speed_ms" in S)
    check("'ds_rms_s' present", "ds_rms_s" in S)
    check("'R_rx_inst' present for SISO", "R_rx_inst" in S)
    check("'R_rx_state' present for SISO", "R_rx_state" in S)
    check("'sv' present for SISO", "sv" in S)


def test_field_shapes():
    """New fields have correct shapes."""
    print("\n=== Test 2: Field shapes ===")
    n_sc, n_tap, l_lag = 256, 32, 8
    sco = StructuredChannelOutput(n_sc, n_tap=n_tap, l_lag=l_lag)

    for _ in range(3):
        H = np.random.randn(n_sc) + 1j * np.random.randn(n_sc)
        sco.extract(H)

    S, H_c = sco.extract(H)
    check("p_inst shape", S["p_inst"].shape == (n_tap,))
    check("p_state shape", S["p_state"].shape == (n_tap,))
    check("d_inst shape", S["d_inst"].shape == (l_lag,))
    check("d_state shape", S["d_state"].shape == (l_lag,))
    check("h_taps shape", S["h_taps"].shape == (2 * n_tap,))
    check("H_c shape", H_c.shape == (n_sc,))
    check("confidence is float", isinstance(S["confidence"], float))
    check("SISO R_rx_inst shape", S["R_rx_inst"].shape == (1, 1))
    check("SISO R_rx_state shape", S["R_rx_state"].shape == (1, 1))
    check("SISO sv shape", S["sv"].shape == (1,))


def test_numeric_ranges():
    """Values within expected bounds."""
    print("\n=== Test 3: Numeric ranges ===")
    n_sc = 128
    sco = StructuredChannelOutput(n_sc, warmup_frames=10)

    for i in range(15):
        H = np.random.randn(n_sc) + 1j * np.random.randn(n_sc)
        result = sco.extract(H, snr_db=10.0)
    S, _ = result

    check("d_inst >= 0", np.all(S["d_inst"] >= -1e-9))
    check("d_inst <= 1+eps", np.all(S["d_inst"] <= 1.0 + 1e-6))
    check("d_state >= 0", np.all(S["d_state"] >= -1e-9))
    check("d_state <= 1+eps", np.all(S["d_state"] <= 1.0 + 1e-6))
    check("confidence in [0,1]", 0.0 <= S["confidence"] <= 1.0)
    check("p_state >= 0", np.all(S["p_state"] >= -1e-15))
    check("confidence == 1.0 after warmup",
          S["confidence"] == 1.0)

    sco2 = StructuredChannelOutput(n_sc, warmup_frames=100)
    H = np.random.randn(n_sc) + 1j * np.random.randn(n_sc)
    S2, _ = sco2.extract(H)
    check("confidence < 1 before warmup done",
          S2["confidence"] < 1.0)


def test_static_vs_fast():
    """Static channel → d_inst ≈ 1; random → d_inst lower."""
    print("\n=== Test 4: Static vs fast-varying ===")
    n_sc, n_frames = 128, 30

    H_static = np.random.randn(n_sc) + 1j * np.random.randn(n_sc)
    sco_s = StructuredChannelOutput(n_sc)
    for _ in range(n_frames):
        result = sco_s.extract(H_static)
    d_static = result[0]["d_inst"]

    sco_f = StructuredChannelOutput(n_sc)
    for _ in range(n_frames):
        H_rand = np.random.randn(n_sc) + 1j * np.random.randn(n_sc)
        result = sco_f.extract(H_rand)
    d_fast = result[0]["d_inst"]

    static_mean = np.mean(d_static)
    fast_mean = np.mean(d_fast)
    check(f"static d_inst mean={static_mean:.4f} > 0.95", static_mean > 0.95)
    check(f"fast d_inst mean={fast_mean:.4f} < static", fast_mean < static_mean)
    check(f"fast d_inst mean < 0.5", fast_mean < 0.5)


def test_compression_consistency():
    """h_taps → zero-pad → FFT should match H_c."""
    print("\n=== Test 5: Compression/reconstruction consistency ===")
    n_sc, n_tap = 256, 32
    sco = StructuredChannelOutput(n_sc, n_tap=n_tap)
    H = np.random.randn(n_sc) + 1j * np.random.randn(n_sc)
    S, H_c = sco.extract(H)

    h_taps = S["h_taps"]
    h_recon = np.zeros(n_sc, dtype=complex)
    h_recon[:n_tap] = h_taps[:n_tap]
    h_recon[-n_tap:] = h_taps[n_tap:]
    H_recon = np.fft.fft(h_recon)

    err = np.max(np.abs(H_c - H_recon))
    check(f"h_taps→H_c reconstruction error = {err:.2e} < 1e-10", err < 1e-10)


def test_ema_convergence():
    """p_state variance should be lower than frame-to-frame p_inst variance."""
    print("\n=== Test 6: EMA convergence ===")
    n_sc, n_frames = 128, 100
    sco = StructuredChannelOutput(n_sc, n_tap=32, ema_p=0.1)

    p_insts = []
    p_states = []
    for _ in range(n_frames):
        H = np.random.randn(n_sc) + 1j * np.random.randn(n_sc)
        S, _ = sco.extract(H)
        p_insts.append(S["p_inst"].copy())
        p_states.append(S["p_state"].copy())

    p_insts = np.array(p_insts)
    p_states = np.array(p_states)

    var_inst = np.mean(np.var(p_insts, axis=0))
    var_state = np.mean(np.var(p_states, axis=0))
    check(f"p_state var ({var_state:.6f}) < p_inst var ({var_inst:.6f})",
          var_state < var_inst)


def test_mimo():
    """MIMO-specific fields: R_rx_inst, R_rx_state, sv."""
    print("\n=== Test 7: MIMO mode ===")
    n_sc, n_rx, n_tx = 64, 2, 2
    sco = StructuredChannelOutput(n_sc, n_rx=n_rx, n_tx=n_tx, n_tap=16)

    for _ in range(5):
        H = (np.random.randn(n_rx, n_tx, n_sc)
             + 1j * np.random.randn(n_rx, n_tx, n_sc))
        result = sco.extract(H)

    S, H_c = result
    check("R_rx_inst present", "R_rx_inst" in S)
    check("R_rx_state present", "R_rx_state" in S)
    check("R_rx alias present", "R_rx" in S)
    check("R_rx_inst shape", S["R_rx_inst"].shape == (n_rx, n_rx))
    check("R_rx_state shape", S["R_rx_state"].shape == (n_rx, n_rx))
    check("sv present", "sv" in S)
    check("sv shape", S["sv"].shape == (min(n_rx, n_tx),))
    check("H_c MIMO shape", H_c.shape == (n_rx, n_tx, n_sc))
    check("h_taps MIMO shape", S["h_taps"].shape == (n_rx, n_tx, 32))

    eigs = np.linalg.eigvalsh(S["R_rx_state"])
    check("R_rx_state eigenvalues >= -eps", np.all(eigs >= -1e-10))


if __name__ == "__main__":
    test_backward_compat()
    test_field_shapes()
    test_numeric_ranges()
    test_static_vs_fast()
    test_compression_consistency()
    test_ema_convergence()
    test_mimo()

    print(f"\n{'='*50}")
    print(f"  TOTAL: {PASS} PASS, {FAIL} FAIL")
    print(f"{'='*50}")
    sys.exit(1 if FAIL > 0 else 0)
