#!/usr/bin/env python3
"""Convert the native rfsimulator GT dump (time-domain channel taps) into the
Sionna-compatible `gt_batch_ue0_seq*.npz` format that eval_nmse_sto.py consumes.

Background
----------
On the native rfsim path the ground-truth uplink channel is the (frozen) set of
sample-spaced CIR taps `channelDesc->ch[]` that rfsim actually convolves the
uplink with. The C dumper (apply_channelmod.c::native_gt_dump, gated by
NATIVE_GT_DUMP=1) appends binary records of those taps + the absolute openair0
rx sample timestamp (same axis as the SRS V3 bin -> slot_id = ts // 30720).

This script computes the frequency response H[k] = FFT_2048(zero-pad(taps)) on
the *natural* OAI FFT grid (no fftshift, 106 PRB wrapping from bin 1412), then
emits a GT npz whose `slot_ids` exactly match the SRS bin's `sample_slots`, so
eval's unified-V3 nearest-neighbour pairing is exact. The UL channel is static
(rfsim never regenerates it), so the same H is replicated across all SRS slots.

Usage
-----
    python3 native_gt_to_npz.py <native_gt.bin> <run_dir> [out_dir]

  <native_gt.bin> : path written by the C dumper (NATIVE_GT_PATH)
  <run_dir>       : the gNB run dir containing srs_matrix_gNB_*.bin
  [out_dir]       : where to write gt_batch_ue0_seq0.npz
                    (default: <run_dir>/sionna_gt)
"""

import os
import sys
import struct
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from digital_twin_stats import load_srs_v2  # noqa: E402

N_FFT = 2048
GT_MAGIC = 0x4E475431  # 'NGT1'


def read_native_gt(path):
    """Parse the C dumper binary into a list of records."""
    with open(path, "rb") as f:
        data = f.read()
    recs = []
    off = 0
    n = len(data)
    while off + 32 <= n:
        (magic,) = struct.unpack_from("<I", data, off)
        if magic != GT_MAGIC:
            break
        off += 4
        (clen,) = struct.unpack_from("<I", data, off); off += 4
        (nb_tx,) = struct.unpack_from("<i", data, off); off += 4
        (nb_rx,) = struct.unpack_from("<i", data, off); off += 4
        (sr,) = struct.unpack_from("<d", data, off); off += 8
        (ts,) = struct.unpack_from("<q", data, off); off += 8
        (td,) = struct.unpack_from("<d", data, off); off += 8
        npair = nb_tx * nb_rx
        ntap = npair * clen
        need = 16 * ntap
        if off + need > n:
            break
        vals = struct.unpack_from("<%dd" % (2 * ntap), data, off)
        off += need
        arr = np.asarray(vals, dtype=np.float64).reshape(npair, clen, 2)
        ch = arr[..., 0] + 1j * arr[..., 1]  # (npair, clen); pair = rx + tx*nb_rx
        recs.append(dict(ts=ts, clen=clen, nb_tx=nb_tx, nb_rx=nb_rx,
                         sr=sr, td=td, ch=ch))
    return recs


def taps_to_freq(ch_pair, clen):
    """Sample-spaced CIR taps -> natural-order 2048-point frequency response.

    Plain np.fft.fft (natural bin order, matches OAI rxdataF indexing). The
    conjugation convention that matches the dumped SRS estimate is auto-detected
    per run in main() (it depends on the estimator/path), so we do NOT conjugate
    here.
    """
    h = np.zeros(N_FFT, dtype=np.complex128)
    m = min(clen, N_FFT)
    h[:m] = ch_pair[:m]
    return np.fft.fft(h)


def _coh(a, b):
    d = np.linalg.norm(a) * np.linalg.norm(b)
    return abs(np.vdot(b, a)) / d if d > 0 else 0.0


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    gt_bin = sys.argv[1]
    run_dir = sys.argv[2]
    out_dir = sys.argv[3] if len(sys.argv) > 3 else os.path.join(run_dir, "sionna_gt")

    recs = read_native_gt(gt_bin)
    if not recs:
        print(f"[native-gt] ERROR: no valid records in {gt_bin}")
        sys.exit(2)

    nb_rx, nb_tx = recs[-1]["nb_rx"], recs[-1]["nb_tx"]
    rec_slots = np.array([rec["ts"] // 30720 for rec in recs], dtype=np.int64)
    # Per-record frequency response (plain FFT): (n_rec, n_rx, n_tx, 2048)
    n_rec = len(recs)
    rec_H = np.zeros((n_rec, nb_rx, nb_tx, N_FFT), dtype=np.complex64)
    for i, rec in enumerate(recs):
        for tx in range(nb_tx):
            for rx in range(nb_rx):
                rec_H[i, rx, tx] = taps_to_freq(rec["ch"][rx + tx * nb_rx], rec["clen"]).astype(np.complex64)
    # dynamic? measure how much the CIR varies across records
    if n_rec > 1:
        c0 = recs[0]["ch"][0]
        c1 = recs[-1]["ch"][0]
        dyn = 1.0 - abs(np.vdot(c0, c1)) / (np.linalg.norm(c0) * np.linalg.norm(c1) + 1e-12)
    else:
        dyn = 0.0
    print(f"[native-gt] {n_rec} record(s); nb_rx={nb_rx} nb_tx={nb_tx} "
          f"clen={recs[-1]['clen']} Td={recs[-1]['td']:.3e}s ts=[{recs[0]['ts']}..{recs[-1]['ts']}] "
          f"first/last-CIR decorrelation={dyn:.3f} ({'DYNAMIC' if dyn > 0.02 else 'static'})")

    # SRS sample-slots to cover.
    try:
        Hs, meta = load_srs_v2(run_dir)
        srs_slots = np.asarray(meta.get("sample_slots"), dtype=np.int64)
    except Exception as e:
        print(f"[native-gt] WARN: cannot read SRS ({e}); using dumped slots")
        Hs, srs_slots = None, np.unique(rec_slots)

    # Auto-detect conjugation: compare H vs conj(H) over a WIDE STO search
    # (the SRS timing locks to the channel energy centroid, not the first tap, so
    # long-delay profiles like CDL-E need tau ~ -17 samples; a narrow window would
    # miss the peak and mis-pick the conjugation). Use several post-handoff frames
    # and take the median coherence for robustness.
    use_conj, coh_plain, coh_conj = False, None, None
    if Hs is not None:
        active = np.mean(np.abs(Hs), axis=(0, 1, 2)) > 1e-6
        ai = np.where(active)[0]
        if ai.size:
            k = ai.astype(np.float64)
            taus = np.arange(-64, 64.01, 1.0)
            ramps = np.exp(-2j * np.pi * np.outer(taus, k) / N_FFT)  # (n_tau, K)

            def best_coh(s, g):
                gg = g[None, :] * ramps                  # (n_tau, K)
                inner = np.abs(gg.conj() @ s)            # (n_tau,)
                d = np.linalg.norm(s) * np.linalg.norm(g) + 1e-12
                return float(np.max(inner) / d)

            order0 = np.argsort(rec_slots)
            rs0 = rec_slots[order0]
            n_srs = Hs.shape[0]
            probe = [i for i in range(n_srs - 1, max(-1, n_srs - 400), -40)
                     if srs_slots[i] >= rec_slots.min()][:8]
            cps, ccs = [], []
            for fi in probe:
                s = Hs[fi, 0, 0][ai]
                pos = np.searchsorted(rs0, srs_slots[fi])
                cand = [p for p in (pos - 1, pos) if 0 <= p < len(rs0)]
                if not cand:
                    continue
                gi = order0[min(cand, key=lambda p: abs(rs0[p] - srs_slots[fi]))]
                g = rec_H[gi, 0, 0][ai]
                cps.append(best_coh(s, g))
                ccs.append(best_coh(s, np.conj(g)))
            if cps:
                coh_plain = float(np.median(cps))
                coh_conj = float(np.median(ccs))
                use_conj = coh_conj > coh_plain
    if use_conj:
        rec_H = np.conj(rec_H)
    print(f"[native-gt] conjugation: coh(plain)={coh_plain} coh(conj)={coh_conj} "
          f"-> using {'conj(FFT)' if use_conj else 'FFT'}")

    # GT exists only post-handoff (first dumped slot). Keep SRS slots >= that;
    # for each kept SRS slot, assign the GT record with the nearest slot.
    gt_start_slot = int(rec_slots.min())
    srs_slots = np.asarray(srs_slots, dtype=np.int64)
    n_total = len(srs_slots)
    keep = srs_slots >= gt_start_slot
    slots = srs_slots[keep].astype(np.uint32)
    N = len(slots)

    order = np.argsort(rec_slots)
    rs_sorted = rec_slots[order]
    h_matrix = np.zeros((N, 1, nb_rx, nb_tx, N_FFT), dtype=np.complex64)
    for n, sl in enumerate(slots.astype(np.int64)):
        pos = np.searchsorted(rs_sorted, sl)
        cand = [p for p in (pos - 1, pos) if 0 <= p < len(rs_sorted)]
        best_p = min(cand, key=lambda p: abs(rs_sorted[p] - sl))
        h_matrix[n, 0] = rec_H[order[best_p]]
    print(f"[native-gt] handoff slot >= {gt_start_slot}: kept {N}/{n_total} "
          f"post-handoff SRS slots (per-slot nearest-CIR GT)")

    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "gt_batch_ue0_seq0.npz")
    np.savez(out,
             h_matrix=h_matrix,
             slot_ids=slots,
             bypass_flags=np.zeros(N, dtype=np.uint8),
             symbol_indices=np.array([12], dtype=np.uint32),
             gnb_ant=np.uint32(nb_rx),
             ue_ant=np.uint32(nb_tx),
             fft_size=np.uint32(N_FFT))
    print(f"[native-gt] wrote {out}: {N} frames, H shape {h_matrix.shape}")


if __name__ == "__main__":
    main()
