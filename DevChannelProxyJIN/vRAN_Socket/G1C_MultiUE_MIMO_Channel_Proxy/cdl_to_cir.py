#!/usr/bin/env python3
"""Synthesize a per-slot sample-spaced CIR sequence from 3GPP CDL ray parameters,
for playback inside the native OAI rfsimulator (RFSIM_CIR_REPLAY / launch_native -cir).

WHY A CONVERSION IS NEEDED (cannot feed CDL data directly)
----------------------------------------------------------
The CDL data on disk (data_out/cdl_*) is a *static ray table*: per ray it stores
delay tau[s], linear power, and arrival/departure angles. It is NOT a channel
impulse response (CIR). The rfsimulator channel model consumes a per-slot,
sample-spaced CIR tap vector ch[l] (l = 0..L-1 at Fs = 61.44 MHz). So we convert:
  1. quantize each ray to a delay tap  l_r = round((tau_r - tau_min) * Fs);
  2. evolve each ray's phase across slots with its Doppler
     nu_r = f_D * sin(theta_r) * cos(phi_r)   (horizontal UE motion);
  3. sum rays into taps. For each slot n (t = n * 0.5 ms at numerology mu=1):
        ch[l] = sum_r sqrt(P_r) * exp(j*(2*pi*nu_r*t + psi_r)) , at tap l = l_r
speed=0 -> identical (real CDL) CIR every slot (static profile);
speed>0 -> time-varying fading that exercises true2d's time stage (PKF).

MIMO / DIMENSION REMINDER  (<<< READ THIS >>>)
----------------------------------------------
--nb-tx (UE SRS antenna ports) and --nb-rx (gNB RX antennas) set the CIR matrix
size. The bin MUST match the runtime antennas: launch_native.sh UE_TX == --nb-tx
and NB_RX == --nb-rx, otherwise rfsim falls back / links go stale and the 2x2/4x4
GT is degenerate (see history: a SISO 1x1 bin under a 2x2 run left 3 of 4 links as
frozen random TDL -> meaningless MIMO NMSE).
  * Each (tx,rx) link gets an INDEPENDENT small-scale realization (same CDL PDP +
    per-ray Doppler, independent random ray phases) -> rich-scattering i.i.d. MIMO.
  * Caps: nb_tx in {1,2,4} (3GPP SRS / estimator SRS_2D_MAX_TX = 4); nb_rx up to 4.
  * Pair order on disk MUST be pair = rx + tx*nb_rx (rx inner, tx outer), matching
    apply_channelmod.c::cir_replay_apply and native_gt_dump.

Output binary (little-endian) consumed by apply_channelmod.c::cir_replay:
    u32 magic=0x43495231 ('CIR1'), u32 n_slots, u32 L, i32 nb_tx, i32 nb_rx, f64 fs
    then n_slots * (nb_tx*nb_rx) * L * {f64 re, f64 im}
    (slot-major, then pair-major [pair = rx + tx*nb_rx], then tap, re/im interleaved)

Usage:
    # SISO (default, back-compatible):
    python3 cdl_to_cir.py <cdl_dir> <out.bin> [-speed KMH] [-slots N] [-L TAPS] [-seed S]
    # 2x2 MIMO (UE 2 SRS ports x gNB 2 RX):
    python3 cdl_to_cir.py data_out/cdl_c cir/cir_cdl_c_s60_2x2.bin -speed 60 --nb-tx 2 --nb-rx 2
    # 4x4 MIMO:
    python3 cdl_to_cir.py data_out/cdl_c cir/cir_cdl_c_s60_4x4.bin -speed 60 --nb-tx 4 --nb-rx 4
    # then run with matching antennas:
    #   sudo NB_RX=<nb-rx> UE_TX=<nb-tx> bash launch_native.sh -e true2d -cir <out.bin> -nd -6 ...
"""
import argparse
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from multisc_cdl_sim import load_cdl_rays, speed_to_fD  # noqa: E402

FS = 61.44e6          # OAI NR mu=1 sample rate (= 2048 * 30 kHz)
T_SLOT = 0.5e-3       # mu=1 slot duration (30720 samples / Fs)
CIR_MAGIC = 0x43495231  # 'CIR1'


def build_cir_sequence(rays, speed_kmh, n_slots, L, seed):
    """Return ch of shape (n_slots, L) complex128 (SISO sum-of-rays)."""
    rng = np.random.default_rng(seed)
    power = np.asarray(rays["power"], dtype=np.float64)
    power = power / power.sum()                  # normalize (sum P = 1)
    tau = np.asarray(rays["tau"], dtype=np.float64)
    theta_r = np.asarray(rays["theta_r"], dtype=np.float64)
    phi_r = np.asarray(rays["phi_r"], dtype=np.float64)
    n_rays = len(power)

    # delay tap index per ray (aligned so the first path lands at tap 0)
    tau0 = tau - tau.min()
    tap = np.round(tau0 * FS).astype(np.int64)
    tap = np.clip(tap, 0, L - 1)

    f_D = speed_to_fD(speed_kmh)
    nu = f_D * np.sin(theta_r) * np.cos(phi_r)   # per-ray Doppler (Hz)
    psi = rng.uniform(0, 2 * np.pi, size=n_rays)
    amp = np.sqrt(power)

    ch = np.zeros((n_slots, L), dtype=np.complex128)
    n_arr = np.arange(n_slots, dtype=np.float64)
    # per-ray complex gain over slots: (n_slots, n_rays)
    phase = 2 * np.pi * np.outer(n_arr * T_SLOT, nu) + psi[None, :]
    gain = amp[None, :] * np.exp(1j * phase)
    # scatter-add into taps
    for r in range(n_rays):
        ch[:, tap[r]] += gain[:, r]
    return ch


def build_mimo_cir(rays, speed_kmh, n_slots, L, seed, nb_tx, nb_rx):
    """Return ch of shape (n_slots, npair, L), npair = nb_tx*nb_rx,
    pair index = rx + tx*nb_rx. Each (tx,rx) link is an INDEPENDENT small-scale
    realization (shared CDL PDP + per-ray Doppler, independent random ray phases
    via a distinct sub-seed) -> rich-scattering i.i.d. MIMO. nb_tx==nb_rx==1
    reproduces the legacy SISO output bit-for-bit (pair 0 uses the base seed)."""
    npair = nb_tx * nb_rx
    out = np.zeros((n_slots, npair, L), dtype=np.complex128)
    for tx in range(nb_tx):
        for rx in range(nb_rx):
            pair = rx + tx * nb_rx
            # distinct phase RNG per link -> spatially decorrelated fading.
            link_seed = seed if pair == 0 else seed + 100003 * pair
            out[:, pair, :] = build_cir_sequence(rays, speed_kmh, n_slots, L, link_seed)
    return out


def write_cir_bin(path, ch_pairs, nb_tx=1, nb_rx=1):
    """ch_pairs: (n_slots, npair, L), pair = rx + tx*nb_rx. Layout on disk:
    slot-major, then pair-major, then tap, re/im interleaved (matches
    apply_channelmod.c::cir_replay_apply src indexing)."""
    n_slots, npair, L = ch_pairs.shape
    assert npair == nb_tx * nb_rx, f"npair {npair} != nb_tx*nb_rx {nb_tx*nb_rx}"
    with open(path, "wb") as f:
        f.write(struct.pack("<IIIiid", CIR_MAGIC, n_slots, L, nb_tx, nb_rx, FS))
        for n in range(n_slots):
            flat = ch_pairs[n].reshape(-1)        # (npair*L,), pair-major then tap
            buf = np.empty(2 * npair * L, dtype=np.float64)
            buf[0::2] = flat.real
            buf[1::2] = flat.imag
            f.write(buf.tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cdl_dir", help="data_out/cdl_* ray directory")
    ap.add_argument("out", help="output cir_replay.bin")
    ap.add_argument("-speed", type=float, default=None,
                    help="UE speed km/h (default: from cdl_metadata.npz, else 3)")
    ap.add_argument("-slots", type=int, default=2000, help="number of slots (loops in C)")
    ap.add_argument("-L", type=int, default=128, help="CIR taps (sample-spaced)")
    ap.add_argument("-seed", type=int, default=12345, help="ray-phase RNG seed")
    ap.add_argument("--nb-tx", type=int, default=1,
                    help="UE SRS antenna ports (1/2/4). MUST match launch_native UE_TX")
    ap.add_argument("--nb-rx", type=int, default=1,
                    help="gNB RX antennas (up to 4). MUST match launch_native NB_RX")
    args = ap.parse_args()

    if args.nb_tx not in (1, 2, 4):
        ap.error(f"--nb-tx must be 1, 2 or 4 (3GPP SRS / SRS_2D_MAX_TX=4), got {args.nb_tx}")
    if not (1 <= args.nb_rx <= 4):
        ap.error(f"--nb-rx must be 1..4, got {args.nb_rx}")

    rays = load_cdl_rays(args.cdl_dir)
    speed = args.speed
    if speed is None:
        meta_path = os.path.join(args.cdl_dir, "cdl_metadata.npz")
        try:
            meta = np.load(meta_path, allow_pickle=True)
            speed = float(meta["speed_kmh"]) if "speed_kmh" in meta.files else 3.0
        except Exception:
            speed = 3.0

    tau = np.asarray(rays["tau"], dtype=np.float64)
    td_ns = (tau.max() - tau.min()) * 1e9
    max_tap = int(np.round((tau.max() - tau.min()) * FS))
    if max_tap >= args.L:
        print(f"[cdl-cir] WARN: max delay tap {max_tap} >= L={args.L}; "
              f"increase -L (Td={td_ns:.0f}ns)")

    ch = build_mimo_cir(rays, speed, args.slots, args.L, args.seed, args.nb_tx, args.nb_rx)
    write_cir_bin(args.out, ch, args.nb_tx, args.nb_rx)

    # quick diagnostics (on pair 0 = (rx0,tx0))
    c0 = ch[:, 0, :]                       # (n_slots, L)
    sel = np.abs(np.fft.fft(c0[0], 2048))
    sel = sel[sel > 1e-9]
    print(f"[cdl-cir] {args.cdl_dir}: {len(rays['power'])} rays, Td={td_ns:.0f}ns, "
          f"speed={speed}km/h f_D={speed_to_fD(speed):.1f}Hz, "
          f"MIMO {args.nb_rx}rx x {args.nb_tx}tx ({args.nb_tx*args.nb_rx} links)")
    print(f"[cdl-cir] wrote {args.out}: n_slots={args.slots} L={args.L} "
          f"|H| sel(std/mean)={sel.std()/sel.mean():.3f} "
          f"(pair0 slot0 vs slot{args.slots-1} CIR corr="
          f"{abs(np.vdot(c0[0], c0[-1]))/(np.linalg.norm(c0[0])*np.linalg.norm(c0[-1])+1e-12):.3f})")
    # MIMO sanity: inter-link spatial decorrelation at slot 0 (should be << 1)
    if args.nb_tx * args.nb_rx >= 2:
        a, b = ch[0, 0, :], ch[0, 1, :]
        coh = abs(np.vdot(a, b)) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12)
        print(f"[cdl-cir] inter-link spatial coh (pair0~pair1) = {coh:.3f} (independent => low)")


if __name__ == "__main__":
    main()
