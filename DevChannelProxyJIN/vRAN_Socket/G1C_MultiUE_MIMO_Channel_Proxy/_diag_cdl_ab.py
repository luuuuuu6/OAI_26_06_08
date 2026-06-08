#!/usr/bin/env python3
"""CDL A/B under PROPER alignment: per-frame/link scalar + STO (linear phase in
true FFT bin index) alignment, then NMSE. Removes the timing-offset artifact the
default evaluator misses, so legacy vs true2d can be compared fairly."""
from __future__ import annotations
import sys
import numpy as np
from eval_nmse_clean import (load_srs, index_gt_slots, align_frames,
                             load_gt_by_refs, get_active_mask)


def run(rd):
    H, meta = load_srs(rd)
    gs, gr = index_gt_slots(rd + "/sionna_gt")
    si, gi, _ = align_frames(meta["abs_slots"], gs, tol=20)
    Hs, Hg = H[si], load_gt_by_refs(gr, gi)
    ai = np.where(get_active_mask(Hs))[0]
    N = meta["n_sc"]
    k = ai.astype(float)
    taus = np.arange(-30, 30, 0.5)
    ramps = np.exp(-2j * np.pi * np.outer(taus, k) / N)  # [n_tau, n_act]

    def nmse(s, g):
        a = np.vdot(g, s) / (np.vdot(g, g) + 1e-30)
        return np.mean(np.abs(s - a * g) ** 2) / (np.mean(np.abs(a * g) ** 2) + 1e-30)

    e0, est = [], []
    F = Hs.shape[0]
    for f in range(F):
        for rx in range(meta["rx"]):
            for tx in range(meta["tx"]):
                s = Hs[f, rx, tx, ai]
                g = Hg[f, rx, tx, ai]
                e0.append(nmse(s, g))
                gg = g[None, :] * ramps
                a = (gg.conj() @ s) / (np.sum(np.abs(gg) ** 2, axis=1) + 1e-30)
                err = np.mean(np.abs(s[None, :] - a[:, None] * gg) ** 2, axis=1)
                sig = np.mean(np.abs(a[:, None] * gg) ** 2, axis=1)
                est.append(np.min(err / (sig + 1e-30)))
    return F, 10 * np.log10(np.median(e0)), 10 * np.log10(np.median(est))


def main():
    print(f"{'run':>10}  frames  scalar-only  scalar+STO")
    for tag, rd in [("legacy", sys.argv[1]), ("true2d", sys.argv[2])]:
        F, e0, est = run(rd)
        print(f"{tag:>10}  {F:6d}  {e0:+8.2f} dB  {est:+8.2f} dB")


if __name__ == "__main__":
    main()
