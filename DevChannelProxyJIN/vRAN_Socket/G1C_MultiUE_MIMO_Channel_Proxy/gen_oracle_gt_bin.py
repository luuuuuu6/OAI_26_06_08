#!/usr/bin/env python3
"""
gen_oracle_gt_bin.py — convert Sionna GT npz directory into the binary
                        lookup file consumed by D0 oracle injection
                        (NR_SRS_EST_ORACLE in nr_srs_mmse.c).

File layout (little-endian, all sizes in bytes):
  header  (24)            : magic, version, n_entries, n_rx, n_tx, n_sc, _pad
  index   (n_entries*12)  : slot_id (i64), offset_c16 (u32) — sorted ascending
  payload (n_entries * n_rx*n_tx*n_sc * 4) : c16 (int16 real, int16 imag)

Slab order in payload (per entry): [rx][tx][sc] of c16.

Usage:
  # GT npz lives at <sweep_run_dir>/sionna_gt/gt_batch_ue0_seq*.npz
  python3 gen_oracle_gt_bin.py \
      --gt-dir <sweep_run_dir>/sionna_gt \
      --out    /tmp/oracle_gt.bin \
      --srs-symbol 12 --ue 0 --scale 32767
"""
import argparse
import glob
import os
import struct
import sys
from pathlib import Path

import numpy as np

ORACLE_MAGIC = 0xDA7AABCD
ORACLE_VERSION = 1


def load_gt_self_contained(gt_dir: str, srs_symbol: int = 12, ue_idx: int = 0,
                            max_frames: int | None = None):
    """Self-contained GT loader (no matplotlib dependency).

    Mirrors digital_twin_stats.load_gt() with return_slot_ids=True semantics
    but without importing matplotlib transitively.

    Returns
    -------
    H        : (N_frames, N_rx, N_tx, N_sc) complex128
    slot_ids : (N_frames,) int64
    """
    pattern = os.path.join(gt_dir, f"gt_batch_ue{ue_idx}_seq*.npz")
    files = sorted(glob.glob(pattern),
                   key=lambda p: int(p.rsplit("seq", 1)[-1].split(".")[0]))
    if not files:
        raise FileNotFoundError(f"No GT files: {pattern}")

    frames = []
    slot_ids: list[int] = []
    for fp in files:
        try:
            data = np.load(fp)
            h = data["h_matrix"]  # (N_frames, n_sym, N_rx, N_tx, N_sc) complex64
        except Exception as e:
            print(f"  [warn] skipping corrupt file {fp}: {e}", file=sys.stderr)
            continue

        # symbol_indices tells us which of h's axis-1 entries is the SRS symbol
        if "symbol_indices" in data.files:
            sym_list = data["symbol_indices"].tolist()
            sym_axis = sym_list.index(srs_symbol) if srs_symbol in sym_list else 0
        else:
            sym_axis = srs_symbol

        file_slot_ids = None
        if "slot_ids" in data.files:
            file_slot_ids = np.asarray(data["slot_ids"]).ravel().astype(np.int64)

        file_bypass = None
        if "bypass_flags" in data.files:
            file_bypass = np.asarray(data["bypass_flags"]).ravel()

        for i in range(h.shape[0]):
            is_bypass = bool(file_bypass[i]) if file_bypass is not None and i < len(file_bypass) else False
            if is_bypass:
                continue
            # Pull (N_rx, N_tx, N_sc) for this frame's SRS symbol.
            frames.append(h[i, sym_axis, ...])
            if file_slot_ids is not None and i < len(file_slot_ids):
                slot_ids.append(int(file_slot_ids[i]))

            if max_frames is not None and len(frames) >= max_frames:
                break
        if max_frames is not None and len(frames) >= max_frames:
            break

    if not frames:
        return np.empty((0, 0, 0, 0), dtype=np.complex128), np.empty(0, dtype=np.int64)
    H = np.stack(frames, axis=0).astype(np.complex128)
    return H, np.asarray(slot_ids, dtype=np.int64)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gt-dir", required=True,
                    help="Directory containing gt_batch_ue<UE>_seq*.npz")
    ap.add_argument("--out", required=True,
                    help="Output binary file path (consumed by SRS_ORACLE_GT_FILE)")
    ap.add_argument("--srs-symbol", type=int, default=12,
                    help="OFDM symbol index of SRS in the slot (default 12)")
    ap.add_argument("--ue", type=int, default=0,
                    help="UE index (default 0)")
    ap.add_argument("--scale", type=str, default="auto",
                    help="Quantization scale: float multiplier before int16 round, "
                         "or 'auto' to use 0.5 * 32767 / max(|H|) "
                         "(default 'auto')")
    ap.add_argument("--max-frames", type=int, default=None,
                    help="Limit number of frames loaded from GT (default: all)")
    args = ap.parse_args()

    H, slot_ids = load_gt_self_contained(args.gt_dir,
                                         srs_symbol=args.srs_symbol,
                                         ue_idx=args.ue,
                                         max_frames=args.max_frames)
    if H.size == 0:
        print(f"ERROR: load_gt returned empty H from {args.gt_dir}", file=sys.stderr)
        return 1
    if slot_ids.size == 0:
        print(f"ERROR: GT npz lacks 'slot_ids' key — required for oracle alignment",
              file=sys.stderr)
        return 1
    if H.shape[0] != slot_ids.shape[0]:
        print(f"ERROR: shape mismatch H[0]={H.shape[0]} vs slot_ids[0]={slot_ids.shape[0]}",
              file=sys.stderr)
        return 1

    n_frames, n_rx, n_tx, n_sc = H.shape
    print(f"loaded {n_frames} frames, shape (n_rx={n_rx}, n_tx={n_tx}, n_sc={n_sc})")
    print(f"  slot_ids range: [{int(slot_ids[0])}, {int(slot_ids[-1])}]")

    # Sort by slot_id (binary search in C assumes sorted index)
    order = np.argsort(slot_ids, kind="stable")
    slot_ids_sorted = slot_ids[order].astype(np.int64)
    H_sorted = H[order]

    # Detect duplicates (would break binary search uniqueness)
    dup_mask = np.diff(slot_ids_sorted) == 0
    if dup_mask.any():
        n_dup = int(dup_mask.sum())
        print(f"  WARN: {n_dup} duplicate slot_ids found; keeping first occurrence each",
              file=sys.stderr)
        keep = np.ones(len(slot_ids_sorted), dtype=bool)
        keep[1:] = ~dup_mask
        slot_ids_sorted = slot_ids_sorted[keep]
        H_sorted = H_sorted[keep]
        n_frames = H_sorted.shape[0]

    # Quantize complex64/128 -> int16 c16
    abs_max = float(np.max(np.abs(H_sorted)))
    if abs_max == 0:
        print("ERROR: GT all zeros", file=sys.stderr)
        return 1

    if args.scale == "auto":
        # Conservative: leave 6 dB headroom so downstream OAI processing
        # (e.g. freq2time IDFT) can't saturate the int16 range.
        scale = 0.5 * 32767.0 / abs_max
        print(f"  abs(H) max = {abs_max:.4e}, auto scale = {scale:.4e} "
              f"(= 0.5 * 32767 / abs_max)")
    else:
        scale = float(args.scale)
        print(f"  abs(H) max = {abs_max:.4e}, fixed scale = {scale:.4e}")

    H_q = np.empty((*H_sorted.shape, 2), dtype=np.int16)
    H_q[..., 0] = np.clip(np.round(H_sorted.real * scale), -32768, 32767).astype(np.int16)
    H_q[..., 1] = np.clip(np.round(H_sorted.imag * scale), -32768, 32767).astype(np.int16)
    print(f"  post-quant max |c16| = {int(np.max(np.abs(H_q.view(np.int16))))}")

    # Build header
    header = struct.pack("<IIIBBHQ",
                         ORACLE_MAGIC, ORACLE_VERSION, n_frames,
                         n_rx, n_tx, n_sc, 0)
    assert len(header) == 24, f"header size {len(header)} != 24"

    # Build index (sorted by slot_id, ascending)
    per_entry = n_rx * n_tx * n_sc  # in c16 units
    index = bytearray()
    for i in range(n_frames):
        offset_c16 = i * per_entry
        index += struct.pack("<qI", int(slot_ids_sorted[i]), offset_c16)
    assert len(index) == n_frames * 12

    payload = H_q.tobytes()
    expect_payload = n_frames * per_entry * 4  # 4 bytes per c16
    assert len(payload) == expect_payload, \
        f"payload size {len(payload)} != expected {expect_payload}"

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(header)
        f.write(bytes(index))
        f.write(payload)

    sz = out_path.stat().st_size
    expect_total = 24 + n_frames * 12 + expect_payload
    print(f"wrote {out_path}: {sz:,} bytes ({sz/1e6:.2f} MB)")
    if sz != expect_total:
        print(f"  WARN: total size {sz} != expected {expect_total}", file=sys.stderr)
        return 1

    print(f"  scale used = {scale:.6e}")
    print(f"  OAI side: export SRS_ORACLE_GT_FILE={out_path.resolve()}")
    print(f"  OAI side: export SRS_ESTIMATOR=oracle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
