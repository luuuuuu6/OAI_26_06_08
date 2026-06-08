#!/usr/bin/env python3
"""eval_nmse_clean.py — SRS vs GT per-frame LS NMSE baseline

Pure measurement tool: loads SRS + GT, aligns by slot, computes
per-frame LS NMSE (global alpha and per-antenna alpha).
No filtering, no STO correction. Assumes V8 GPU IPC.

Data formats:
  SRS: srs_matrix_gNB_RxTx_seq*.bin — int16 IQ, (N, Rx, Tx, Nsc)
  GT:  sionna_gt/gt_batch_ue0_seq*.npz — complex64, symbol 12

Usage:
  python3 eval_nmse_clean.py --run-dir logs/.../snr_10dB
"""

import sys
import struct
import glob
import os
import argparse
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from digital_twin_stats import index_gt_slots, load_gt_by_refs  # noqa: E402

SRS_BIN_MAGIC = 0x53525331  # "SRS1"
SRS_SLOT_SAMPLES = 30720    # samples per slot at mu=1 (== GT v8.py _SLOT_SAMPLES)


# ═════════════════════════════════════════════════════════════════
# Data loaders — self-contained, no external dependency
# ═════════════════════════════════════════════════════════════════

def load_srs(run_dir: str, skip_first: int = 0):
    """Load SRS from bin files. Returns H (N, Rx, Tx, Nsc) complex128 + meta."""
    pattern = os.path.join(run_dir, "srs_matrix_gNB_*_seq*.bin")
    files = sorted(glob.glob(pattern),
                   key=lambda p: int(p.rsplit("seq", 1)[-1].split(".")[0]))
    if not files:
        raise FileNotFoundError(f"no SRS bins in {run_dir}")

    all_H, all_fid, all_sid, all_ts = [], [], [], []
    meta = {}

    for fp in files:
        with open(fp, "rb") as f:
            hdr = f.read(32)
            magic, ver, rx, tx, n_sc, n_frames = struct.unpack("<6I", hdr[:24])
            if magic != SRS_BIN_MAGIC:
                raise ValueError(f"{fp}: bad magic 0x{magic:08X}")
            meta.update(rx=rx, tx=tx, n_sc=n_sc, version=ver)

            for _ in range(n_frames):
                fid = struct.unpack("<I", f.read(4))[0]
                sid = struct.unpack("<I", f.read(4))[0]
                f.read(4)  # rnti(u16) + pad(u16)
                # V3+: absolute openair0 rx sample timestamp (same axis as GT).
                ts = struct.unpack("<q", f.read(8))[0] if ver >= 3 else -1
                iq = np.frombuffer(f.read(rx * tx * n_sc * 4),
                                   dtype=np.int16).reshape(rx, tx, n_sc, 2)
                H = iq[..., 0].astype(np.float64) + 1j * iq[..., 1].astype(np.float64)
                all_H.append(H)
                all_fid.append(fid)
                all_sid.append(sid)
                all_ts.append(ts)

    if skip_first > 0:
        all_H = all_H[skip_first:]
        all_fid = all_fid[skip_first:]
        all_sid = all_sid[skip_first:]
        all_ts = all_ts[skip_first:]

    H_srs = np.stack(all_H)
    abs_slots = _unwrap_abs_slots(np.array(all_fid, dtype=np.int64),
                                  np.array(all_sid, dtype=np.int64))
    meta["abs_slots"] = abs_slots
    # V3+: expose sample-domain slot id (= sample_ts // SLOT_SAMPLES). This lives
    # in the SAME absolute axis as the GT slot_ids (v8.py ipc_ts // 30720), so the
    # eval can pair exactly without guessing a per-run constant offset C.
    ts_arr = np.array(all_ts, dtype=np.int64)
    if ts_arr.size > 0 and np.all(ts_arr >= 0):
        meta["sample_slots"] = ts_arr // SRS_SLOT_SAMPLES
    return H_srs, meta


def _unwrap_abs_slots(frame_ids, slot_ids):
    """SFN unwrap: frame*20+slot with 10240-frame wrap detection."""
    SFN_WRAP = 20 * 1024  # 20480
    raw = frame_ids * 20 + slot_ids
    out = np.empty_like(raw)
    out[0] = raw[0]
    offset = 0
    for i in range(1, len(raw)):
        if raw[i] < raw[i - 1] - SFN_WRAP // 2:
            offset += SFN_WRAP
        out[i] = raw[i] + offset
    return out


def load_gt(gt_dir: str, srs_symbol: int = 12, skip_first: int = 0):
    """Load GT from NPZ files. Returns H (M, Rx, Tx, Nsc) complex128 + slot_ids."""
    pattern = os.path.join(gt_dir, "gt_batch_ue0_seq*.npz")
    files = sorted(glob.glob(pattern),
                   key=lambda p: int(p.rsplit("seq", 1)[-1].split(".")[0]))
    if not files:
        raise FileNotFoundError(f"no GT npz in {gt_dir}")

    frames, slot_ids = [], []

    for fp in files:
        data = np.load(fp)
        h = data["h_matrix"]  # (batch, n_sym, gnb_ant, ue_ant, fft)

        sym_axis = 0
        if "symbol_indices" in data:
            sym_list = data["symbol_indices"].tolist()
            if srs_symbol in sym_list:
                sym_axis = sym_list.index(srs_symbol)

        bypass = data.get("bypass_flags", None)
        file_slots = data.get("slot_ids", None)

        for i in range(h.shape[0]):
            if bypass is not None and i < len(bypass) and bool(bypass[i]):
                continue
            frames.append(h[i, sym_axis].astype(np.complex128))
            if file_slots is not None and i < len(file_slots):
                slot_ids.append(int(file_slots[i]))

    if skip_first > 0:
        frames = frames[skip_first:]
        slot_ids = slot_ids[skip_first:]

    H_gt = np.stack(frames) if frames else np.empty((0,), dtype=np.complex128)
    return H_gt, np.array(slot_ids, dtype=np.int64)


def _pair_unique_nearest(srs_abs, gt_slots, offset, tol):
    """Pair each GT frame with the nearest shifted SRS frame at most once."""
    shifted = srs_abs + offset
    order = np.argsort(gt_slots)
    gt_sorted = gt_slots[order]

    used_srs = set()
    pairs = []
    gaps_out = []
    for gt_pos, gt_slot in enumerate(gt_sorted):
        idx = int(np.searchsorted(shifted, gt_slot, side="left"))
        candidates = []
        if idx < len(shifted):
            candidates.append(idx)
        if idx > 0:
            candidates.append(idx - 1)
        candidates = [i for i in candidates if i not in used_srs]
        if not candidates:
            continue

        best = min(candidates, key=lambda i: abs(int(shifted[i]) - int(gt_slot)))
        gap = abs(int(shifted[best]) - int(gt_slot))
        if gap <= tol:
            used_srs.add(best)
            pairs.append((best, int(order[gt_pos])))
            gaps_out.append(gap)

    if not pairs:
        return np.array([], dtype=int), np.array([], dtype=int), np.array([], dtype=int)

    srs_idx = np.array([p[0] for p in pairs], dtype=int)
    gt_idx = np.array([p[1] for p in pairs], dtype=int)
    gaps = np.array(gaps_out, dtype=int)
    sort_order = np.argsort(srs_idx)
    return srs_idx[sort_order], gt_idx[sort_order], gaps[sort_order]


def align_frames(srs_abs, gt_slots, tol=20):
    """One-to-one nearest-neighbor slot alignment with auto offset detection."""
    if len(srs_abs) == 0 or len(gt_slots) == 0:
        return np.array([], dtype=int), np.array([], dtype=int), 0

    C_rough = int(np.median(gt_slots)) - int(np.median(srs_abs))

    best_C, best_n, best_gap = C_rough, 0, None
    for delta in range(-tol * 2, tol * 2 + 1):
        C = C_rough + delta
        srs_idx, gt_idx, gaps = _pair_unique_nearest(srs_abs, gt_slots, C, tol)
        n = len(srs_idx)
        gap = float(np.median(gaps)) if n else float("inf")
        if n > best_n or (n == best_n and (best_gap is None or gap < best_gap)):
            best_n, best_C, best_gap = n, C, gap

    srs_idx, gt_idx, per_frame_gaps = _pair_unique_nearest(srs_abs, gt_slots, best_C, tol)
    median_gap = int(np.median(per_frame_gaps)) if len(per_frame_gaps) else -1

    print(f"  [align] offset C={best_C} (gt_median={int(np.median(gt_slots))}, "
          f"srs_median={int(np.median(srs_abs))}, "
          f"unique_matches={best_n}/{min(len(srs_abs), len(gt_slots))})")

    if len(per_frame_gaps) >= 8:
        n_q = len(per_frame_gaps) // 4
        for q in range(4):
            sl = per_frame_gaps[q * n_q:(q + 1) * n_q]
            print(f"    Q{q+1} gap: median={np.median(sl):.1f}, "
                  f"max={np.max(sl)}, mean={np.mean(sl):.2f}")
        if np.median(per_frame_gaps[-n_q:]) > np.median(per_frame_gaps[:n_q]) + 2:
            print("    ⚠ WARNING: gap increasing over time → likely GT/SRS drift")

    return srs_idx, gt_idx, median_gap


def unify_reference_frame(srs_sample_slots, gt_slots, tol=None):
    """Same-physical-slot pairing for V3 SRS (`sample_slots`) vs GT slots.

    Both `srs_sample_slots` (= SRS sample_ts // 30720) and `gt_slots` (= proxy
    ipc_ts // 30720) live in the SAME absolute sample-slot axis (verified: within
    a run the offset is ~0, i.e. they share an origin), so pairing uses OFFSET 0
    and nearest-neighbor -- NO per-run median-C guess (which align_frames needs
    because the SFN domain wraps and GT/SRS medians span different windows), NO
    SFN unwrap. With dense GT (GT_SAVE_EVERY=1) every SRS slot has its own GT and
    gaps collapse to 0; with sparse GT (e.g. 100) we fall back to the nearest GT,
    which is harmless on a static channel and is guarded by the gap=0 envelope
    correlation check downstream.

    Returns (srs_idx, gt_idx, median_gap). Each GT/SRS used at most once.
    """
    srs = np.asarray(srs_sample_slots, dtype=np.int64)
    gt = np.asarray(gt_slots, dtype=np.int64)
    if tol is None:
        # absorb GT sparsity: ~half the GT save cadence, floored generously.
        if gt.size > 1:
            spacing = int(np.median(np.diff(np.sort(gt))))
        else:
            spacing = 1
        tol = max(64, spacing)
    # offset 0: shared absolute axis (no median-C). nearest-neighbor within tol.
    srs_idx, gt_idx, gaps = _pair_unique_nearest(srs, gt, 0, tol)
    med = int(np.median(gaps)) if len(gaps) else -1
    mx = int(gaps.max()) if len(gaps) else -1
    print(f"  [unify] V3 absolute sample-slot pairing (offset=0, tol={tol}): "
          f"{len(srs_idx)}/{srs.size} paired, median gap={med}, max gap={mx} "
          f"(gap>0 only reflects GT sparsity, not reference mismatch)")
    return np.asarray(srs_idx, dtype=int), np.asarray(gt_idx, dtype=int), med


def get_active_mask(H_srs):
    """Active subcarrier mask from SRS magnitude."""
    avg_mag = np.mean(np.abs(H_srs), axis=(0, 1, 2))
    return avg_mag > 1e-6


# ═════════════════════════════════════════════════════════════════
# NMSE computation
# ═════════════════════════════════════════════════════════════════

def nmse_ls(H_s, H_g, active):
    """LS NMSE: per-frame LS alpha alignment, global + per-antenna."""
    F, R, T, _ = H_s.shape
    eps = 1e-30
    aidx = np.where(active)[0]

    err_g_pf = np.zeros(F)
    sig_g_pf = np.zeros(F)
    err_pa_pf = np.zeros(F)
    sig_pa_pf = np.zeros(F)

    per_ant_err = np.zeros((R, T))
    per_ant_sig = np.zeros((R, T))

    for f in range(F):
        s = H_s[f, :, :, aidx].ravel()
        g = H_g[f, :, :, aidx].ravel()
        a = np.vdot(g, s) / (np.vdot(g, g) + eps)
        diff = s - a * g
        err_g_pf[f] = np.mean(np.abs(diff) ** 2)
        sig_g_pf[f] = np.mean(np.abs(a * g) ** 2)

        e_sum, s_sum = 0.0, 0.0
        for rx in range(R):
            for tx in range(T):
                si = H_s[f, rx, tx, aidx]
                gi = H_g[f, rx, tx, aidx]
                ai = np.vdot(gi, si) / (np.vdot(gi, gi) + eps)
                di = si - ai * gi
                e_i = np.mean(np.abs(di) ** 2)
                s_i = np.mean(np.abs(ai * gi) ** 2)
                e_sum += e_i
                s_sum += s_i
                per_ant_err[rx, tx] += e_i
                per_ant_sig[rx, tx] += s_i
        err_pa_pf[f] = e_sum / (R * T)
        sig_pa_pf[f] = s_sum / (R * T)

    # aggregate: total_err / total_sig (robust to outliers)
    nmse_g = 10 * np.log10(np.mean(err_g_pf) / (np.mean(sig_g_pf) + eps) + eps)
    nmse_pa = 10 * np.log10(np.mean(err_pa_pf) / (np.mean(sig_pa_pf) + eps) + eps)
    pf_g_dB = 10 * np.log10(err_g_pf / (sig_g_pf + eps) + eps)
    pf_pa_dB = 10 * np.log10(err_pa_pf / (sig_pa_pf + eps) + eps)
    ant_dB = 10 * np.log10(per_ant_err / (per_ant_sig + eps) + eps)

    return dict(global_dB=nmse_g, per_ant_dB=nmse_pa,
                pf_global=pf_g_dB, pf_pa=pf_pa_dB, ant_dB=ant_dB)


# ═════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════

def pct(arr):
    p10, p50, p90 = np.percentile(arr, [10, 50, 90])
    return f"[p10={p10:+.2f}, p50={p50:+.2f}, p90={p90:+.2f}]"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--srs-symbol", type=int, default=12)
    ap.add_argument("--skip-first", type=int, default=0)
    ap.add_argument("--tol", type=int, default=20)
    ap.add_argument("--predict-horizon", type=int, default=0,
                    help="Direction A: score a PREDICTION capture (dumped with "
                         "SRS_PKF_PREDICT_AHEAD=k) against the FUTURE GT. Shifts the "
                         "SRS abs-slots by +k*SRS_period before GT alignment, so "
                         "SRS[n] (= predicted H(n+k)) is paired with GT[n+k]. "
                         "0 = standard on-time evaluation.")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    gt_dir = run_dir / "sionna_gt"
    if not run_dir.exists():
        sys.exit(f"run-dir not found: {run_dir}")
    if not gt_dir.exists():
        sys.exit(f"sionna_gt not found under {run_dir}")

    print(f"[load] {run_dir}")
    H_srs, meta = load_srs(str(run_dir), skip_first=args.skip_first)
    print(f"  SRS: {H_srs.shape}  rx={meta['rx']} tx={meta['tx']} n_sc={meta['n_sc']}")

    print(f"[index GT] scanning metadata only (lazy)...")
    gt_slots, gt_refs = index_gt_slots(
        str(gt_dir), srs_symbol=args.srs_symbol, skip_first=args.skip_first)
    print(f"  GT indexed: {len(gt_slots)} valid frames from {len(set(r[0] for r in gt_refs))} files")

    align_src = meta["abs_slots"]
    if args.predict_horizon > 0:
        # Prediction capture: SRS[n] holds the k-step prediction of slot n+k, so
        # shift the SRS slot axis forward by k*SRS_period to pair with future GT.
        srs_sorted = np.sort(meta["abs_slots"])
        period = int(np.median(np.diff(srs_sorted))) if srs_sorted.size > 1 else 1
        period = max(period, 1)
        shift = args.predict_horizon * period
        align_src = meta["abs_slots"] + shift
        print(f"[predict] horizon k={args.predict_horizon}, SRS_period={period} slots "
              f"-> shifting SRS axis by +{shift} slots to score vs FUTURE GT")

    si, gi, gap = align_frames(align_src, gt_slots, tol=args.tol)
    print(f"  paired: {len(si)} frames  (median_gap={gap})")

    if len(si) < 2:
        sys.exit("too few paired frames")

    print(f"[load GT] loading {len(gi)} paired frames...")
    H_s = H_srs[si]
    H_g = load_gt_by_refs(gt_refs, gi)
    active = get_active_mask(H_s)
    n_act = int(active.sum())
    aidx = np.where(active)[0]

    scale = np.sqrt(np.mean(np.abs(H_s[:, :, :, aidx]) ** 2) /
                    (np.mean(np.abs(H_g[:, :, :, aidx]) ** 2) + 1e-30))

    print(f"  active SC: {n_act}/{meta['n_sc']}")
    print(f"  scale SRS/GT: {scale:.1f}x")
    R, T = meta["rx"], meta["tx"]

    ls = nmse_ls(H_s, H_g, active)
    print()
    print("=" * 50)
    if args.predict_horizon > 0:
        print(f"  k={args.predict_horizon} PREDICTION vs FUTURE GT (per-antenna alpha)")
    else:
        print("  LS baseline (single-frame, per-antenna alpha)")
    print("=" * 50)
    print(f"  NMSE global     : {ls['global_dB']:+.2f} dB  {pct(ls['pf_global'])}")
    print(f"  NMSE per-antenna: {ls['per_ant_dB']:+.2f} dB  {pct(ls['pf_pa'])}")
    for rx in range(R):
        for tx in range(T):
            print(f"    rx{rx}_tx{tx}: {ls['ant_dB'][rx,tx]:+.2f} dB")

    # ── Temporal stability (quartile analysis) ──
    F = len(si)
    if F >= 8:
        print()
        print("=" * 50)
        print("  Temporal Stability (per-quartile LS per-antenna)")
        print("=" * 50)
        n_q = F // 4
        for q in range(4):
            s = slice(q * n_q, (q + 1) * n_q)
            seg_err = ls["pf_pa"][s]
            print(f"  Q{q+1} (frames {q*n_q:3d}-{(q+1)*n_q-1:3d}): "
                  f"median={np.median(seg_err):+.2f} dB, "
                  f"p90={np.percentile(seg_err, 90):+.2f} dB")
        drift = np.median(ls["pf_pa"][-n_q:]) - np.median(ls["pf_pa"][:n_q])
        if abs(drift) > 2.0:
            print(f"  ⚠ Q4-Q1 drift = {drift:+.1f} dB → data quality degrades over time")
        else:
            print(f"  ✓ Q4-Q1 drift = {drift:+.1f} dB (stable)")


if __name__ == "__main__":
    main()
