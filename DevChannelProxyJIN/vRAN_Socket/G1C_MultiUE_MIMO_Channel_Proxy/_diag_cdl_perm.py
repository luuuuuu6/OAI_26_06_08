#!/usr/bin/env python3
"""Is the residual CDL SRS<->GT mismatch an antenna/port mapping issue?
Build the cross-link delay+scalar-aligned NMSE matrix: SRS link (rx,tx) vs GT
link (rx',tx'). If identity is best -> mapping OK (residual is structural/STO).
If a permutation is much better -> antenna/port mapping mismatch."""
from __future__ import annotations
import sys
import numpy as np
from eval_nmse_clean import (load_srs, index_gt_slots, align_frames,
                             load_gt_by_refs, get_active_mask)
from _diag_cdl_align import aligned_nmse


def main():
    run_dir = sys.argv[1]
    H_srs, meta = load_srs(run_dir)
    gt_slots, gt_refs = index_gt_slots(run_dir + "/sionna_gt")
    si, gi, _ = align_frames(meta["abs_slots"], gt_slots, tol=20)
    H_s = H_srs[si]
    H_g = load_gt_by_refs(gt_refs, gi)
    aidx = np.where(get_active_mask(H_s))[0]
    R, T = meta["rx"], meta["tx"]
    L = R * T
    F = H_s.shape[0]
    nf = min(F, 30)

    # cross-link matrix: rows = SRS link, cols = GT link, value = mean delay-aligned NMSE
    M = np.zeros((L, L))
    for li, (rx, tx) in enumerate([(r, t) for r in range(R) for t in range(T)]):
        for lj, (rx2, tx2) in enumerate([(r, t) for r in range(R) for t in range(T)]):
            acc = []
            for f in range(nf):
                s = H_s[f, rx, tx, aidx]
                g = H_g[f, rx2, tx2, aidx]
                _, ed, _ = aligned_nmse(s, g, search=30)
                acc.append(ed)
            M[li, lj] = np.mean(acc)
    links = [f"rx{r}tx{t}" for r in range(R) for t in range(T)]
    print("cross-link delay+scalar NMSE (dB): rows=SRS, cols=GT")
    print("            " + "  ".join(f"{c:>8}" for c in links))
    for li in range(L):
        row = "  ".join(f"{M[li,lj]:+8.2f}" for lj in range(L))
        best = links[int(np.argmin(M[li]))]
        print(f"  {links[li]:>8}  {row}   best->{best}")
    diag = np.mean([M[i, i] for i in range(L)])
    offdiag_best = np.mean([min(M[i, j] for j in range(L) if j != i) for i in range(L)])
    print(f"\nmean diagonal (identity map) = {diag:+.2f} dB")
    print(f"mean best off-diagonal        = {offdiag_best:+.2f} dB")


if __name__ == "__main__":
    main()
