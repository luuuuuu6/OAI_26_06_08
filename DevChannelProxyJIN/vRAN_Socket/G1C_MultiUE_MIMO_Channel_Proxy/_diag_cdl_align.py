#!/usr/bin/env python3
"""Diagnose the CDL SRS<->GT positive-NMSE: is it a bulk timing/delay offset?
For each paired frame/antenna, compare scalar-only alignment (what
eval_nmse_clean does) vs scalar + integer-delay alignment (remove a common
linear phase ramp across the active band, estimated by delay-domain xcorr)."""
from __future__ import annotations
import sys
import numpy as np
from eval_nmse_clean import (load_srs, index_gt_slots, align_frames,
                             load_gt_by_refs, get_active_mask)


def aligned_nmse(s, g, search=60):
    """Return (nmse_scalar_dB, nmse_delay_dB, best_delay) for one band vector."""
    eps = 1e-30
    a = np.vdot(g, s) / (np.vdot(g, g) + eps)
    e0 = np.mean(np.abs(s - a * g) ** 2) / (np.mean(np.abs(a * g) ** 2) + eps)

    n = s.size
    m = np.arange(n)
    # delay-domain cross-correlation peak (bulk linear-phase / timing offset)
    x = np.fft.ifft(s * np.conj(g))
    d0 = int(np.argmax(np.abs(x)))
    cands = [d0, d0 - n]
    best = e0
    best_d = 0
    for dc in set(list(range(-search, search + 1)) + cands):
        ramp = np.exp(-2j * np.pi * m * dc / n)
        gg = g * ramp
        aa = np.vdot(gg, s) / (np.vdot(gg, gg) + eps)
        e = np.mean(np.abs(s - aa * gg) ** 2) / (np.mean(np.abs(aa * gg) ** 2) + eps)
        if e < best:
            best = e
            best_d = dc
    return 10 * np.log10(e0 + eps), 10 * np.log10(best + eps), best_d


def main():
    run_dir = sys.argv[1]
    H_srs, meta = load_srs(run_dir)
    gt_slots, gt_refs = index_gt_slots(run_dir + "/sionna_gt")
    si, gi, gap = align_frames(meta["abs_slots"], gt_slots, tol=20)
    H_s = H_srs[si]
    H_g = load_gt_by_refs(gt_refs, gi)
    active = get_active_mask(H_s)
    aidx = np.where(active)[0]
    R, T = meta["rx"], meta["tx"]
    F = H_s.shape[0]

    e0s, eds, delays = [], [], []
    for f in range(F):
        for rx in range(R):
            for tx in range(T):
                s = H_s[f, rx, tx, aidx]
                g = H_g[f, rx, tx, aidx]
                e0, ed, d = aligned_nmse(s, g)
                e0s.append(e0); eds.append(ed); delays.append(d)
    e0s = np.array(e0s); eds = np.array(eds); delays = np.array(delays)
    print(f"paired frames={F}, antennas={R*T}, active SC={aidx.size}")
    print(f"  scalar-only      NMSE: median={np.median(e0s):+.2f} dB  mean={np.mean(e0s):+.2f}")
    print(f"  scalar+delay     NMSE: median={np.median(eds):+.2f} dB  mean={np.mean(eds):+.2f}")
    print(f"  best integer delay   : median={np.median(delays):.0f} bins  "
          f"p10/p90={np.percentile(delays,10):.0f}/{np.percentile(delays,90):.0f}  "
          f"unique={np.unique(delays).size}")


if __name__ == "__main__":
    main()
