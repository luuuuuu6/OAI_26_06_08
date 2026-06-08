#!/usr/bin/env python3
"""One-shot diagnostic for the native DYNAMIC GT/eval coherence collapse.

Given a raw GT dump (apply_channelmod.c::native_gt_dump) and a run_dir with SRS
V3 bins, this probes WHERE the dynamic pipeline breaks, WITHOUT touching the
production converter/eval:

  1) slot-offset sweep k in [-K..K]: pair each SRS frame to the GT record at
     (its slot + k) and report best-over-STO coherence for plain FFT and conj.
       - peak at k=0           -> time axes aligned (H2 ruled out)
       - peak at k!=0          -> SRS<->GT slot-index offset  (H2)
  2) conjugation decision at k=0: per-frame coh(plain) vs coh(conj); if they are
     close / flip frame-to-frame the median auto-detect mis-picks (H1).

Usage:  python3 _diag_dyn_gt.py <native_gt.bin> <run_dir>
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from native_gt_to_npz import read_native_gt, taps_to_freq, N_FFT  # noqa: E402
from digital_twin_stats import load_srs_v2  # noqa: E402

KMAX = 10
NPROBE = 40


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    gt_bin, run_dir = sys.argv[1], sys.argv[2]

    recs = read_native_gt(gt_bin)
    if not recs:
        print("no GT records"); sys.exit(2)
    rec_slots = np.array([r["ts"] // 30720 for r in recs], dtype=np.int64)
    order = np.argsort(rec_slots)
    rec_slots = rec_slots[order]
    recs = [recs[i] for i in order]
    rec_H = np.stack([taps_to_freq(r["ch"][0], r["clen"]) for r in recs])  # (n_rec,2048)

    try:
        Hs, meta = load_srs_v2(run_dir)
    except FileNotFoundError:
        print(f"\nERROR: no SRS bins in {run_dir}\n"
              "  -> this run captured 0 SRS frames (likely 5GC/NAS degradation:\n"
              "     RRCReconfigurationComplete=0). Re-run launch_native.sh with\n"
              "     --restart-5gc and point this probe at a run dir that has\n"
              "     srs_matrix_gNB_*.bin. Pick it e.g.:\n"
              "       RUN=$(ls -dt $LOGS/native_passthru_* | while read d; do "
              "ls \"$d\"/srs_matrix_*.bin >/dev/null 2>&1 && echo \"$d\" && break; done)")
        sys.exit(4)
    if Hs is None or Hs.shape[0] == 0:
        print(f"\nERROR: SRS array empty in {run_dir} (0 captured frames).")
        sys.exit(4)
    ss = meta.get("sample_slots", meta.get("slot_ids"))
    if ss is None:
        print("\nERROR: SRS meta has neither sample_slots nor slot_ids (old bin?).")
        sys.exit(4)
    srs_slots = np.asarray(ss, dtype=np.int64)
    print(f"GT: n_rec={len(recs)} slots[{rec_slots[0]}..{rec_slots[-1]}]  "
          f"SRS: n={Hs.shape[0]} slots[{srs_slots.min()}..{srs_slots.max()}]")

    active = np.mean(np.abs(Hs), axis=(0, 1, 2)) > 1e-6
    ai = np.where(active)[0]
    kf = ai.astype(np.float64)
    taus = np.arange(-64, 64.01, 1.0)
    ramps = np.exp(-2j * np.pi * np.outer(taus, kf) / N_FFT)

    def best_coh(s, g):
        gg = g[None, :] * ramps
        inner = np.abs(gg.conj() @ s)
        d = np.linalg.norm(s) * np.linalg.norm(g) + 1e-12
        return float(np.max(inner) / d)

    # SRS frames that have a GT record covering their slot
    cand_fi = [i for i in range(Hs.shape[0])
               if rec_slots[0] <= srs_slots[i] <= rec_slots[-1]]
    if not cand_fi:
        print("no SRS frame within GT slot range"); sys.exit(3)
    step = max(1, len(cand_fi) // NPROBE)
    probe = cand_fi[::step][:NPROBE]
    print(f"probing {len(probe)} SRS frames (of {len(cand_fi)} in-range)\n")

    # ---- slot-offset sweep ----
    print("  k(slot)  coh_plain  coh_conj   (median over probe frames)")
    results = {}
    for k in range(-KMAX, KMAX + 1):
        cps, ccs = [], []
        for fi in probe:
            j = int(np.searchsorted(rec_slots, srs_slots[fi]))
            j = j + k
            if not (0 <= j < len(rec_slots)):
                continue
            s = Hs[fi, 0, 0][ai]
            g = rec_H[j][ai]
            cps.append(best_coh(s, g))
            ccs.append(best_coh(s, np.conj(g)))
        if cps:
            results[k] = (float(np.median(cps)), float(np.median(ccs)))
            mark = "  <-- k=0" if k == 0 else ("  <== PEAK" if False else "")
            print(f"   {k:+3d}     {results[k][0]:.3f}      {results[k][1]:.3f}{mark}")

    # peak location
    kp_plain = max(results, key=lambda k: results[k][0])
    kp_conj = max(results, key=lambda k: results[k][1])
    print(f"\n  peak coh_plain at k={kp_plain:+d} ({results[kp_plain][0]:.3f}); "
          f"peak coh_conj at k={kp_conj:+d} ({results[kp_conj][1]:.3f})")

    # ---- conjugation decision at k=0 (what the converter sees) ----
    cps, ccs, flips = [], [], 0
    for fi in probe:
        j = int(np.searchsorted(rec_slots, srs_slots[fi]))
        if not (0 <= j < len(rec_slots)):
            continue
        s = Hs[fi, 0, 0][ai]
        g = rec_H[j][ai]
        cp, cc = best_coh(s, g), best_coh(s, np.conj(g))
        cps.append(cp); ccs.append(cc)
        if cc > cp:
            flips += 1
    cps, ccs = np.array(cps), np.array(ccs)
    print(f"\n  k=0 per-frame: coh_plain med={np.median(cps):.3f} "
          f"[{cps.min():.2f},{cps.max():.2f}]  "
          f"coh_conj med={np.median(ccs):.3f} [{ccs.min():.2f},{ccs.max():.2f}]")
    print(f"  frames where conj>plain: {flips}/{len(cps)} "
          f"(converter picks conj if median(conj)>median(plain))")
    margin = np.median(cps) - np.median(ccs)
    print(f"  median margin (plain-conj) = {margin:+.3f}  "
          f"-> {'PLAIN (correct)' if margin > 0 else 'CONJ (likely MIS-pick under motion)'}")


if __name__ == "__main__":
    main()
