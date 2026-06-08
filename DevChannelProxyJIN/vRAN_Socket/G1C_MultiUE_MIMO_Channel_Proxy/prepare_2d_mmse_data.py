#!/usr/bin/env python3
"""
Convert sweep output → standardized NPZ for test_2d_mmse.py.

Handles both static (speed=0, no GT alignment needed) and
dynamic (speed>0, per-frame Sionna GT aligned by slot).

Usage — single run directory:
    python3 prepare_2d_mmse_data.py \
        --run-dir logs/q4_sweep_.../snr_20dB \
        --snr 20 --speed 3 --seed 42

Usage — full sweep directory:
    python3 prepare_2d_mmse_data.py \
        --sweep-dir logs/q4_sweep_YYYYMMDD \
        --speed 3 --seed 42

Output: data_out/2d_mmse_input/H_snr{X}dB_speed{V}ms_seed{S}.npz
  (static: H_snr{X}dB_static_seed{S}.npz)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from digital_twin_stats import load_srs_v2, load_gt
from eval_pdpR_nmse import pair_by_abs_slot, detect_active_sc
from srs_sto_compensate import compensate_sto


def process_one_run(
    run_dir: str,
    snr_dB: int,
    ue_speed: float,
    channel_seed: int,
    out_dir: Path,
    srs_symbol: int = 12,
    sto_weighting: str = "power",
):
    """Process one SNR-point run directory → one NPZ."""
    run_path = Path(run_dir)
    gt_dir = run_path / "sionna_gt"

    print(f"\n{'='*60}")
    print(f"  SNR={snr_dB} dB  speed={ue_speed} m/s  seed={channel_seed}")
    print(f"  run_dir: {run_path}")
    print(f"{'='*60}")

    # 1. Load SRS
    H_srs, meta_srs = load_srs_v2(str(run_path))
    N, n_rx, n_tx, n_sc = H_srs.shape
    print(f"  SRS: {N} frames, {n_rx}x{n_tx} MIMO, {n_sc} SC")

    # 2. Detect active subcarriers
    active_mask = detect_active_sc(H_srs)
    active_sc = np.where(active_mask)[0]
    print(f"  Active SC: {len(active_sc)}")

    # 3. STO compensation (per rx/tx pair)
    H_comp = H_srs.copy()
    for rx in range(n_rx):
        for tx in range(n_tx):
            h = H_srs[:, rx, tx, :]
            h_fixed, stats = compensate_sto(
                h, active_sc, N_fft=n_sc,
                weighting=sto_weighting, return_stats=True,
            )
            H_comp[:, rx, tx, :] = h_fixed
            print(f"  STO [rx{rx}_tx{tx}]: std={stats['delta_std']:.3f} "
                  f"corr {stats['corr_before']:.4f} -> {stats['corr_after']:.4f}")

    # 4. Load GT + align (if GT available)
    H_gt_aligned = None
    srs_abs = meta_srs.get("abs_slots", np.arange(N))

    if gt_dir.exists() and any(gt_dir.glob("gt_batch_ue0_seq*.npz")):
        H_gt_raw, gt_slots = load_gt(
            str(gt_dir), srs_symbol=srs_symbol,
            return_slot_ids=True,
        )
        print(f"  GT: {H_gt_raw.shape[0]} frames loaded")

        if gt_slots is not None and len(gt_slots) > 0:
            idx_srs, idx_gt = pair_by_abs_slot(srs_abs, gt_slots, tol=4)
            n_paired = len(idx_srs)
            print(f"  Paired: {n_paired} / {N} SRS frames matched to GT")

            if n_paired > 0:
                H_comp = H_comp[idx_srs]
                H_gt_aligned = H_gt_raw[idx_gt]
                srs_abs = srs_abs[idx_srs]
                N = n_paired
        else:
            n_common = min(N, H_gt_raw.shape[0])
            H_comp = H_comp[:n_common]
            H_gt_aligned = H_gt_raw[:n_common]
            srs_abs = srs_abs[:n_common]
            N = n_common
            print(f"  GT: no slot_ids, truncated to {n_common} frames")
    else:
        print(f"  GT: not found in {gt_dir}, skipping GT alignment")

    # 5. Save standardized NPZ
    is_static = (ue_speed == 0)
    if is_static:
        fname = f"H_snr{snr_dB}dB_static_seed{channel_seed}.npz"
    else:
        fname = f"H_snr{snr_dB}dB_speed{int(ue_speed)}ms_seed{channel_seed}.npz"

    out_path = out_dir / fname

    save_dict = dict(
        H_sto_compensated=H_comp,
        active_sc=active_sc,
        abs_slots=srs_abs,
        snr_dB=snr_dB,
        n_frames=N,
        n_rx=n_rx,
        n_tx=n_tx,
        n_sc=n_sc,
        channel_seed=channel_seed,
        ue_speed=ue_speed,
    )
    if H_gt_aligned is not None:
        save_dict["H_gt"] = H_gt_aligned
        print(f"  Saved with per-frame GT: H_gt shape={H_gt_aligned.shape}")

    np.savez_compressed(out_path, **save_dict)
    print(f"  -> {out_path}  ({N} frames)")
    return out_path


def parse_snr_from_dirname(dirname: str) -> int | None:
    """Extract SNR from directory name like 'snr_20dB' or 'snr_m5dB'."""
    m = re.match(r"snr_(m?)(\d+)dB", dirname)
    if m:
        sign = -1 if m.group(1) == "m" else 1
        return sign * int(m.group(2))
    return None


def main():
    parser = argparse.ArgumentParser(description="Prepare 2D MMSE input data")
    parser.add_argument("--run-dir", help="Single SNR run directory")
    parser.add_argument("--sweep-dir", help="Sweep root (processes all snr_*dB/ subdirs)")
    parser.add_argument("--snr", type=int, help="SNR in dB (required with --run-dir)")
    parser.add_argument("--speed", type=float, required=True, help="UE speed in m/s")
    parser.add_argument("--seed", type=int, default=42, help="Channel seed")
    parser.add_argument("--srs-symbol", type=int, default=12, help="SRS OFDM symbol index for GT")
    parser.add_argument("--out-dir", default=None,
                        help="Output directory (default: data_out/2d_mmse_input/)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else (
        Path(__file__).parent / "data_out" / "2d_mmse_input"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.sweep_dir:
        sweep = Path(args.sweep_dir)
        snr_dirs = sorted(sweep.glob("snr_*dB"))
        if not snr_dirs:
            print(f"ERROR: no snr_*dB directories in {sweep}")
            sys.exit(1)
        for sd in snr_dirs:
            snr = parse_snr_from_dirname(sd.name)
            if snr is None:
                print(f"  SKIP: cannot parse SNR from {sd.name}")
                continue
            process_one_run(str(sd), snr, args.speed, args.seed, out_dir,
                            srs_symbol=args.srs_symbol)

    elif args.run_dir:
        if args.snr is None:
            snr = parse_snr_from_dirname(Path(args.run_dir).name)
            if snr is None:
                print("ERROR: --snr required when --run-dir name doesn't match snr_*dB")
                sys.exit(1)
        else:
            snr = args.snr
        process_one_run(args.run_dir, snr, args.speed, args.seed, out_dir,
                        srs_symbol=args.srs_symbol)
    else:
        print("ERROR: specify --sweep-dir or --run-dir")
        sys.exit(1)

    print(f"\nDone. Output in: {out_dir}")


if __name__ == "__main__":
    main()
