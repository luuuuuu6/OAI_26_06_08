#!/usr/bin/env python3
"""eval_denoise_real.py - score the PRODUCTION C delay-gate (nr_srs_delay_gate)
on REAL dumps, in-sandbox, without a native run.

Feeds each real per-link estimate sequence (the dumped srs_estimated_channel_freq)
through the actual C function via the test_denoise harness (env SRS_DENOISE_PEAK_DB),
then scores the gated output's NMSE vs Sionna GT with the same STO-aligned metric as
eval_nmse_sto. Confirms the C code reproduces the offline +3 dB gain.

Run (after `make test_denoise` in c_unit_tests):
  python3 eval_denoise_real.py --run-dir <run> [--peak-db 30]
  python3 eval_denoise_real.py --ab3 <1x1> <2x2> <4x4>
"""
from __future__ import annotations
import argparse
import os
import subprocess
import time
from pathlib import Path

import numpy as np

from offline_freq_denoise import load_paired, nmse_sto

BIN = (Path(__file__).resolve().parent
       / "../../openairinterface5g_whan/openair1/PHY/NR_ESTIMATION/c_unit_tests/test_denoise").resolve()


def gate_link_c(seq, n_fft, peak_db):
    """Run the C gate over one link's frame sequence [F, n_fft] complex (int values)."""
    F = seq.shape[0]
    lines = [f"{F} {n_fft}"]
    iv = np.empty((F, 2 * n_fft), dtype=np.int64)
    iv[:, 0::2] = np.rint(seq.real).astype(np.int64)
    iv[:, 1::2] = np.rint(seq.imag).astype(np.int64)
    for f in range(F):
        lines.append(" ".join(map(str, iv[f])))
    env = dict(os.environ)
    env["SRS_DENOISE_PEAK_DB"] = str(peak_db)
    proc = subprocess.run([str(BIN)], input="\n".join(lines) + "\n",
                          capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise SystemExit(f"test_denoise failed: {proc.stderr}")
    rows = [r for r in proc.stdout.strip().splitlines() if r.strip()]
    out = np.empty((F, n_fft), dtype=np.complex128)
    for f, row in enumerate(rows):
        v = np.array(row.split(), dtype=np.int64)
        out[f] = v[0::2] + 1j * v[1::2]
    return out


def run_one(run_dir, peak_db):
    H_s, H_g, aidx, n_fft = load_paired(run_dir)
    F, R, T = H_s.shape[0], H_s.shape[1], H_s.shape[2]
    base, _ = nmse_sto(H_s, H_g, aidx, n_fft)
    Hd = np.zeros_like(H_s)
    t0 = time.perf_counter()
    for rx in range(R):
        for tx in range(T):
            Hd[:, rx, tx, :] = gate_link_c(H_s[:, rx, tx, :], n_fft, peak_db)
    dt = time.perf_counter() - t0
    gated, _ = nmse_sto(Hd, H_g, aidx, n_fft)
    print(f"\n=== {run_dir} ===")
    print(f"  {F} frames, {R}x{T}, n_fft={n_fft}")
    print(f"  baseline NMSE (C dump)        : {base:+.2f} dB")
    print(f"  C delay-gate NMSE (peak_db={peak_db:g}): {gated:+.2f} dB   "
          f"(gain {base - gated:+.2f} dB)")
    print(f"  [C gate wall: {dt:.1f}s over {R*T} links x {F} frames]")
    return base, gated


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir")
    ap.add_argument("--ab3", nargs=3, metavar=("R1x1", "R2x2", "R4x4"))
    ap.add_argument("--peak-db", type=float, default=30.0)
    args = ap.parse_args()
    if not BIN.exists():
        raise SystemExit(f"build first: make test_denoise  (missing {BIN})")
    runs = [args.run_dir] if args.run_dir else (list(args.ab3) if args.ab3 else [])
    if not runs:
        ap.error("provide --run-dir or --ab3")
    for rd in runs:
        run_one(rd, args.peak_db)


if __name__ == "__main__":
    raise SystemExit(main())
