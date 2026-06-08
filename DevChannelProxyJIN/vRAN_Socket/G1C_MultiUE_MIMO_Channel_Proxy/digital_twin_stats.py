"""
Digital Twin Statistical Cross-Validation Pipeline
===================================================
Computes large-scale / spatial channel statistics from both OAI SRS
estimates and Sionna Ray-Tracing ground truth, then cross-validates.

Metrics:
  1. RSRP  – Reference Signal Received Power per antenna pair
  2. R     – Spatial Covariance Matrix (RX-side and TX-side)
  3. SVD   – Singular-value spectrum of H per subcarrier
  4. SNR   – Estimation SNR (OAI vs GT, per-subcarrier / per-frame)
  5. PDP   – Power Delay Profile (IFFT-based, STO-corrected)

Usage:
    python digital_twin_stats.py [--log-dir ...] [--plot-dir ...]
"""

import argparse
import glob
import json
import os
import struct
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm

# ── SRS V2 binary loader ─────────────────────────────────────────────

SRS_BIN_MAGIC = 0x53525331  # "SRS1"


_SFN_MOD = 1024
_SLOTS_PER_FRAME = 20
_SFN_WRAP = _SFN_MOD * _SLOTS_PER_FRAME          # 20480 abs-slots per SFN cycle


def unwrap_srs_abs_slots(frame_ids: np.ndarray,
                         slot_ids: np.ndarray) -> np.ndarray:
    """Compute monotonically-increasing absolute slot indices from NR SFN.

    NR SFN is 10-bit (0-1023) and wraps every 10.24 s.  Naively computing
    ``frame_id * 20 + slot_id`` produces a sawtooth that breaks
    nearest-neighbour alignment with GT slot counters that never wrap.

    This function detects each downward SFN jump and accumulates a wrap
    offset so the returned sequence is monotonic.
    """
    raw = np.asarray(frame_ids, dtype=np.int64) * _SLOTS_PER_FRAME + \
          np.asarray(slot_ids, dtype=np.int64)
    out = np.empty_like(raw)
    offset: int = 0
    out[0] = raw[0]
    for i in range(1, len(raw)):
        if raw[i] < raw[i - 1] - _SFN_WRAP // 2:
            offset += _SFN_WRAP
        out[i] = raw[i] + offset
    return out


def load_srs_v2(log_dir: str, skip_first: int = 0) -> Tuple[np.ndarray, dict]:
    """Load SRS channel estimates from V2 binary files with header.

    Parameters
    ----------
    skip_first : discard the first *skip_first* SRS frames to avoid
                 transient data captured before RRC/AGC/scheduling
                 have converged after gNB/UE attach.

    Returns
    -------
    H : ndarray, shape (N_frames, N_rx, N_tx, N_sc), complex128
    meta : dict with rx, tx, n_sc, frame_ids, slot_ids, abs_slots
           abs_slots is SFN-unwrapped monotonic absolute slot index.
    """
    pattern = os.path.join(log_dir, "srs_matrix_gNB_*_seq*.bin")
    files = sorted(glob.glob(pattern),
                   key=lambda p: int(p.rsplit("seq", 1)[-1].split(".")[0]))
    if not files:
        raise FileNotFoundError(f"No SRS bin files in {log_dir}")

    all_H, all_frame_ids, all_slot_ids, all_ts = [], [], [], []
    meta = {}

    for fp in files:
        with open(fp, "rb") as f:
            hdr_raw = f.read(32)
            magic = struct.unpack("<I", hdr_raw[0:4])[0]
            if magic != SRS_BIN_MAGIC:
                raise ValueError(f"{fp}: bad magic 0x{magic:08x}")
            _, ver, rx, tx, n_el, n_frames = struct.unpack("<6I", hdr_raw[:24])
            meta = {"rx": rx, "tx": tx, "n_sc": n_el, "version": ver}

            for _ in range(n_frames):
                fid = struct.unpack("<I", f.read(4))[0]
                sid = struct.unpack("<I", f.read(4))[0]
                _rnti = struct.unpack("<H", f.read(2))[0]
                _pad = f.read(2)
                # V3+: absolute openair0 rx sample timestamp (same axis as GT).
                ts = struct.unpack("<q", f.read(8))[0] if ver >= 3 else -1
                n_bytes = rx * tx * n_el * 4
                iq = np.frombuffer(f.read(n_bytes), dtype=np.int16).reshape(rx, tx, n_el, 2)
                H = iq[..., 0].astype(np.float64) + 1j * iq[..., 1].astype(np.float64)
                all_H.append(H)
                all_frame_ids.append(fid)
                all_slot_ids.append(sid)
                all_ts.append(ts)

    meta["frame_ids"] = all_frame_ids
    meta["slot_ids"] = all_slot_ids
    meta["abs_slots"] = unwrap_srs_abs_slots(
        np.asarray(all_frame_ids, dtype=np.int64),
        np.asarray(all_slot_ids, dtype=np.int64),
    )
    ts_arr = np.asarray(all_ts, dtype=np.int64)
    if ts_arr.size > 0 and np.all(ts_arr >= 0):
        # sample-domain slot id (= sample_ts // 30720), same axis as GT slot_ids.
        meta["sample_slots"] = ts_arr // 30720
    H_all = np.array(all_H)

    if skip_first > 0 and skip_first < H_all.shape[0]:
        H_all = H_all[skip_first:]
        meta["frame_ids"] = meta["frame_ids"][skip_first:]
        meta["slot_ids"] = meta["slot_ids"][skip_first:]
        meta["abs_slots"] = meta["abs_slots"][skip_first:]
        if "sample_slots" in meta:
            meta["sample_slots"] = meta["sample_slots"][skip_first:]
        meta["skipped_warmup"] = skip_first

    return H_all, meta


# ── GT loader ─────────────────────────────────────────────────────────

def load_gt(gt_dir: str, srs_symbol: int = 12, ue_idx: int = 0,
            max_frames: Optional[int] = None,
            return_slot_ids: bool = False,
            skip_first: int = 0):
    """Load Sionna GT and extract the SRS OFDM symbol.

    Supports both old format (14 symbols per slot) and new compact format
    (only saved symbols, with symbol_indices metadata).

    Parameters
    ----------
    return_slot_ids : if True, returns (H_gt, slot_ids) where slot_ids is a
                      1-D np.int64 array of absolute slot indices per frame
                      (extracted from the npz `slot_ids` key; empty if the
                      file doesn't carry them).  Default False keeps the
                      original signature.
    skip_first : discard the first *skip_first* GT frames (transient warmup).

    Returns
    -------
    H_gt : (N_frames, N_rx, N_tx, N_sc) complex128
    [slot_ids] : (N_frames,) int64  — only if return_slot_ids=True
    """
    pattern = os.path.join(gt_dir, f"gt_batch_ue{ue_idx}_seq*.npz")
    files = sorted(glob.glob(pattern),
                   key=lambda p: int(p.rsplit("seq", 1)[-1].split(".")[0]))
    if not files:
        raise FileNotFoundError(f"No GT files: {pattern}")

    frames = []
    slot_ids: List[int] = []
    bypass_list: List[bool] = []
    for fp in files:
        try:
            data = np.load(fp)
            h = data["h_matrix"]
        except Exception:
            print(f"  [warn] skipping corrupt file: {fp}")
            continue

        if "symbol_indices" in data:
            sym_list = data["symbol_indices"].tolist()
            if srs_symbol in sym_list:
                sym_axis = sym_list.index(srs_symbol)
            else:
                sym_axis = 0
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
            frames.append(h[i, sym_axis].astype(np.complex128))
            if file_slot_ids is not None and i < len(file_slot_ids):
                slot_ids.append(int(file_slot_ids[i]))
            if max_frames and len(frames) >= max_frames + skip_first:
                H = np.array(frames)[skip_first:]
                sid_arr = np.asarray(slot_ids, dtype=np.int64)
                if skip_first > 0 and sid_arr.size > skip_first:
                    sid_arr = sid_arr[skip_first:]
                if return_slot_ids:
                    return H, sid_arr
                return H

    H = np.array(frames)
    sid_arr = np.asarray(slot_ids, dtype=np.int64)

    if skip_first > 0 and skip_first < H.shape[0]:
        H = H[skip_first:]
        sid_arr = sid_arr[skip_first:] if sid_arr.size > skip_first else sid_arr

    if return_slot_ids:
        return H, sid_arr
    return H


# ── Lazy GT loader (pair-only, avoids OOM on large GT dirs) ───────────

def index_gt_slots(gt_dir: str, srs_symbol: int = 12, ue_idx: int = 0,
                   skip_first: int = 0):
    """Scan GT npz files and build a lightweight slot index WITHOUT loading h_matrix.

    Returns
    -------
    gt_slots : (N_valid,) int64 — absolute slot id per valid GT frame
    gt_refs  : list of (file_path, frame_idx_in_file, sym_axis) tuples,
               one per valid GT frame, in the same order as gt_slots.
    """
    pattern = os.path.join(gt_dir, f"gt_batch_ue{ue_idx}_seq*.npz")
    files = sorted(glob.glob(pattern),
                   key=lambda p: int(p.rsplit("seq", 1)[-1].split(".")[0]))
    if not files:
        raise FileNotFoundError(f"No GT files: {pattern}")

    gt_slots: List[int] = []
    gt_refs: List[Tuple[str, int, int]] = []
    n_bypassed = 0

    for fp in files:
        try:
            data = np.load(fp)
        except Exception:
            continue

        if "symbol_indices" in data:
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

        n_frames = int(file_slot_ids.shape[0]) if file_slot_ids is not None else 0
        for i in range(n_frames):
            is_bp = bool(file_bypass[i]) if file_bypass is not None and i < len(file_bypass) else False
            if is_bp:
                n_bypassed += 1
                continue
            gt_slots.append(int(file_slot_ids[i]))
            gt_refs.append((fp, i, sym_axis))

        data.close()

    if skip_first > 0:
        gt_slots = gt_slots[skip_first:]
        gt_refs = gt_refs[skip_first:]

    return np.asarray(gt_slots, dtype=np.int64), gt_refs


def load_gt_by_refs(gt_refs, indices):
    """Load only the GT frames identified by indices into gt_refs.

    Groups reads by file to avoid redundant npz opens.

    Parameters
    ----------
    gt_refs  : list from index_gt_slots()
    indices  : integer array — positions into gt_refs to load

    Returns
    -------
    H_gt : (len(indices), N_rx, N_tx, N_sc) complex128
    """
    by_file: Dict[str, List[Tuple[int, int, int]]] = {}
    for out_pos, ref_idx in enumerate(indices):
        fp, frame_i, sym_ax = gt_refs[int(ref_idx)]
        by_file.setdefault(fp, []).append((out_pos, frame_i, sym_ax))

    result = [None] * len(indices)
    files_loaded = 0
    for fp, items in by_file.items():
        with np.load(fp) as data:
            h = data["h_matrix"]
            for out_pos, frame_i, sym_ax in items:
                result[out_pos] = h[frame_i, sym_ax].astype(np.complex128)
        files_loaded += 1

    H = np.stack(result)
    return H


# ── Frame alignment by absolute slot index (Prof. feedback 2026-04-23) ─
#
# GT and SRS are independent sampling processes of the same channel:
#   - SRS captures at scheduled SRS opportunities (every ~160 slots)
#   - GT saves every N UL slots (--gt-save-every, default 10)
# Taking H_srs[:n_common] paired with H_gt[:n_common] is WRONG: the first
# n_common frames of each cover completely different time windows.  We
# align by nearest-neighbor on the absolute slot index.


def find_slot_offset(abs_slot_srs: np.ndarray, abs_slot_gt: np.ndarray,
                     tol_slots: int = 20) -> Tuple[int, int]:
    """Find the constant offset C such that (srs + C) aligns with gt.

    GT uses ipc_ts // 30720 (large, non-wrapping), SRS uses NR SFN-based
    frame*20+slot (small, wrapping with unwrap).  Both increment at 1 per
    slot, so the difference is a constant C = median(gt) - median(srs).

    Returns (offset, n_matches) where n_matches is the number of pairs
    within tol_slots after applying the offset.
    """
    srs = np.asarray(abs_slot_srs, dtype=np.int64)
    gt = np.asarray(abs_slot_gt, dtype=np.int64)
    if len(srs) == 0 or len(gt) == 0:
        return 0, 0

    C_rough = int(np.median(gt)) - int(np.median(srs))
    gt_sorted = np.sort(gt)

    best_C, best_n = C_rough, 0
    for delta in range(-tol_slots * 2, tol_slots * 2 + 1):
        C = C_rough + delta
        srs_shifted = srs + C
        pos = np.searchsorted(gt_sorted, srs_shifted)
        cand_l = np.clip(pos - 1, 0, len(gt_sorted) - 1)
        cand_r = np.clip(pos, 0, len(gt_sorted) - 1)
        gaps = np.minimum(np.abs(srs_shifted - gt_sorted[cand_l]),
                          np.abs(srs_shifted - gt_sorted[cand_r]))
        n = int(np.sum(gaps <= tol_slots))
        if n > best_n:
            best_n = n
            best_C = C

    return best_C, best_n


def align_by_slot(abs_slot_srs: np.ndarray, abs_slot_gt: np.ndarray,
                  tol_slots: int = 20,
                  auto_offset: bool = True) -> Tuple[np.ndarray, np.ndarray, int]:
    """Nearest-neighbor alignment of two abs-slot sequences.

    For each SRS abs-slot, find the closest GT abs-slot within tol_slots.
    Unmatched SRS frames are dropped.

    When auto_offset is True and the raw sequences are in different
    coordinate systems (e.g. GT uses ipc_ts-based ids, SRS uses SFN-based),
    automatically discovers the constant offset C that maximises matches.

    Parameters
    ----------
    abs_slot_srs : (N_srs,)  sorted or unsorted int array
    abs_slot_gt  : (N_gt,)   sorted or unsorted int array
    tol_slots    : maximum allowed abs-slot gap between paired SRS and GT
                   (20 slots at 30 kHz SCS == 10 ms == 1 NR frame).
    auto_offset  : if True, auto-detect and correct coordinate system offset.

    Returns
    -------
    srs_idx      : (M,) int indices into the original SRS array
    gt_idx       : (M,) int indices into the original GT array
    median_gap   : int  median absolute abs-slot gap of the M matched pairs
    """
    srs = np.asarray(abs_slot_srs, dtype=np.int64)
    gt = np.asarray(abs_slot_gt, dtype=np.int64)

    offset = 0
    if auto_offset and len(srs) > 0 and len(gt) > 0:
        median_diff = abs(int(np.median(gt)) - int(np.median(srs)))
        if median_diff > tol_slots * 10:
            offset, n_auto = find_slot_offset(srs, gt, tol_slots)
            print(f"  [align] auto-offset: C={offset} "
                  f"(gt_median={int(np.median(gt))}, "
                  f"srs_median={int(np.median(srs))}, "
                  f"matches={n_auto}/{len(srs)})")
            srs = srs + offset

    gt_order = np.argsort(gt)
    gt_sorted = gt[gt_order]

    pos = np.searchsorted(gt_sorted, srs)
    cand_left = np.clip(pos - 1, 0, len(gt_sorted) - 1)
    cand_right = np.clip(pos, 0, len(gt_sorted) - 1)
    d_left = np.abs(srs - gt_sorted[cand_left])
    d_right = np.abs(srs - gt_sorted[cand_right])
    take_left = d_left <= d_right
    best = np.where(take_left, cand_left, cand_right)
    gap = np.where(take_left, d_left, d_right)

    keep = gap <= tol_slots
    srs_idx = np.where(keep)[0]
    gt_idx = gt_order[best[keep]]
    median_gap = int(np.median(gap[keep])) if keep.any() else -1
    return srs_idx, gt_idx, median_gap


# ── Active subcarrier mask ────────────────────────────────────────────

def get_active_mask(H_srs: np.ndarray, threshold: float = 1e-6) -> np.ndarray:
    """Boolean mask of active subcarriers from SRS magnitude."""
    avg_mag = np.mean(np.abs(H_srs), axis=(0, 1, 2))
    return avg_mag > threshold


# ── AGC: per-frame EWMA gain alignment (Prof. feedback 2026-04-23) ────
#
# Rationale
# ---------
# The raw per-frame SRS estimate carries a slowly-varying "receiver gain"
# g_rx(n) (OLPC / ADC AGC / v4.py per-antenna normalization interactions).
# This gain is not present on H_gt (Sionna outputs clean H(f)).  Averaging
# H_srs[:N] with g_rx(n) drifting across n biases the average and produces
# the jagged NMSE-vs-N / NMSE-vs-SNR curves we observed in §25.7.
#
# Fix: estimate the per-frame power ratio g(n) = P_srs(n) / P_gt(n), smooth
# it with a forgetting factor beta (EWMA), and rescale H_gt per-frame by
# sqrt(g_hat(n)).  This removes the g_rx(n) drift from the SRS↔GT scale
# relationship without destroying real LSF (the EWMA smoothing retains
# slow shadowing variations above the beta time constant).
#
# This is the Python-analysis-layer equivalent of an AGC implemented at
# the OAI receiver — mathematically equivalent when no ADC clipping
# occurs (which is the case in this software simulation).


def agc_ewma_align(H_srs: np.ndarray, H_gt: np.ndarray, active: np.ndarray,
                   beta: float = 0.95, residual_global: bool = True
                   ) -> Tuple[np.ndarray, np.ndarray, dict]:
    """Per-frame EWMA gain alignment of H_gt to H_srs.

    Parameters
    ----------
    H_srs, H_gt      : (N, rx, tx, n_sc) complex — unchanged on return
    active           : boolean mask over n_sc
    beta             : forgetting factor, 0 < beta < 1
                       effective window ≈ 1 / (1 - beta)
                       0.95 → 20 frames ; 0.99 → 100 frames
    residual_global  : if True, also apply a final global adjustment so
                       that mean(|H_srs|^2) == mean(|H_gt_aligned|^2)
                       exactly (guards against EWMA initial-transient bias)

    Returns
    -------
    H_srs_out        : H_srs (unchanged, returned for API symmetry)
    H_gt_out         : H_gt rescaled per-frame to match H_srs power
    info             : dict with keys
                         "g"        : (N,)  raw per-frame gain ratio
                         "g_hat"    : (N,)  EWMA-smoothed gain
                         "beta"     : float
                         "global_k" : float  residual global multiplier
                                              applied (1.0 if disabled)
    """
    if not (0.0 < beta < 1.0):
        raise ValueError(f"beta must be in (0,1), got {beta}")

    p_srs = np.mean(np.abs(H_srs[:, :, :, active]) ** 2, axis=(1, 2, 3))
    p_gt = np.mean(np.abs(H_gt[:, :, :, active]) ** 2, axis=(1, 2, 3))
    g = p_srs / (p_gt + 1e-30)

    N = len(g)
    g_hat = np.empty_like(g)
    g_hat[0] = g[0]
    for n in range(1, N):
        g_hat[n] = beta * g_hat[n - 1] + (1.0 - beta) * g[n]

    s = np.sqrt(g_hat)
    H_gt_out = H_gt * s[:, None, None, None]

    global_k = 1.0
    if residual_global:
        rms_srs = np.sqrt(np.mean(np.abs(H_srs[:, :, :, active]) ** 2))
        rms_gt_aligned = np.sqrt(np.mean(np.abs(H_gt_out[:, :, :, active]) ** 2))
        global_k = float(rms_srs / (rms_gt_aligned + 1e-30))
        H_gt_out = H_gt_out * global_k

    info = {
        "g": g.astype(np.float64),
        "g_hat": g_hat.astype(np.float64),
        "beta": float(beta),
        "global_k": global_k,
    }
    return H_srs, H_gt_out, info


# ── Neyman-Pearson PDP denoising (Prof. feedback 2026-04-23) ──────────
#
# For a complex-Gaussian-distributed time-domain noise sample, |h|^2 is
# exponentially distributed.  Under H0 (noise only):
#     P(|h|^2 > T | H0) = exp(-T / sigma_n^2)
# For a target false-alarm probability P_FA, the Neyman-Pearson threshold
# is T = -sigma_n^2 * ln(P_FA).
#
# sigma_n^2 is estimated from the tail of the causal PDP (well beyond any
# realistic delay spread, these bins are noise-only).


def np_pdp_threshold(pdp: np.ndarray, noise_tail_frac: float = 0.25,
                     P_FA: float = 1e-6) -> Tuple[float, float]:
    """Neyman-Pearson threshold for PDP tap detection.

    Parameters
    ----------
    pdp              : (n_fft,) PDP (already causal-averaged)
    noise_tail_frac  : fraction of causal-half bins (from the tail)
                       used as the noise-only estimate.  Default 0.25.
    P_FA             : target per-bin false-alarm probability.

    Returns
    -------
    threshold        : float
    sigma2_n         : float   estimated noise variance per bin
    """
    if P_FA <= 0.0 or P_FA >= 1.0:
        raise ValueError(f"P_FA must be in (0,1), got {P_FA}")
    half = len(pdp) // 2
    pdp_causal = pdp[:half]
    n_tail = max(4, int(half * noise_tail_frac))
    sigma2_n = float(np.mean(pdp_causal[-n_tail:]))
    threshold = -sigma2_n * float(np.log(P_FA))
    return threshold, sigma2_n


def denoised_pdp(pdp: np.ndarray, P_FA: float = 1e-6,
                 noise_tail_frac: float = 0.25
                 ) -> Tuple[np.ndarray, float, float]:
    """Apply Neyman-Pearson threshold: zero-out bins below threshold.

    Returns
    -------
    pdp_clean        : (n_fft,)  PDP with sub-threshold bins set to 0
    threshold        : float
    sigma2_n         : float
    """
    thr, sigma2_n = np_pdp_threshold(pdp, noise_tail_frac, P_FA)
    pdp_clean = np.where(pdp >= thr, pdp, 0.0)
    return pdp_clean, thr, sigma2_n


# ── Per-frame LS complex alignment (replaces C-code AGC) ─────────────
#
# C-code AGC is unsuitable for Sionna proxy simulation (signal RMS ≈ 6,
# int16 full-scale 0.02%, gain always hits ceiling → breaks sync).
# Per-frame LS α is mathematically superior: it normalizes power AND phase
# simultaneously, with zero impact on the OAI data collection pipeline.


def per_frame_ls_align(H_srs: np.ndarray, H_gt: np.ndarray,
                       active: np.ndarray,
                       sto_slope: float = 0.0, n_fft: int = 0
                       ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    """Per-frame complex LS alignment: α_n = <SRS_n, GT_n> / ||GT_n||².

    Parameters
    ----------
    H_srs, H_gt : (N, rx, tx, n_sc) complex
    active       : boolean mask (n_sc,)
    sto_slope    : STO phase ramp (applied to SRS before alignment)
    n_fft        : FFT size (needed if sto_slope != 0)

    Returns
    -------
    H_srs_corrected : (N, rx, tx, n_sc) SRS with STO correction applied
    alphas          : (N,) complex per-frame LS alignment factors
    nmse_per_frame  : (N,) per-frame NMSE in dB
    info            : dict with diagnostics
    """
    N = H_srs.shape[0]

    if sto_slope != 0.0 and n_fft > 0:
        H_srs_c = _apply_sto_correction(H_srs, active, sto_slope, n_fft)
    else:
        H_srs_c = H_srs.copy()

    H_s = H_srs_c[:, :, :, active]
    H_g = H_gt[:, :, :, active]

    alphas = np.empty(N, dtype=np.complex128)
    nmse_per_frame = np.empty(N, dtype=np.float64)

    for n in range(N):
        s_n = H_s[n].ravel()
        g_n = H_g[n].ravel()
        alphas[n] = np.vdot(g_n, s_n) / (np.vdot(g_n, g_n) + 1e-30)
        ref_n = alphas[n] * g_n
        err_n = s_n - ref_n
        sig_power = np.mean(np.abs(ref_n) ** 2)
        err_power = np.mean(np.abs(err_n) ** 2)
        nmse_per_frame[n] = 10.0 * np.log10(err_power / (sig_power + 1e-30))

    alpha_mags = np.abs(alphas)
    alpha_phases_deg = np.angle(alphas) * 180.0 / np.pi

    info = {
        "alpha_mag_mean": float(np.mean(alpha_mags)),
        "alpha_mag_std": float(np.std(alpha_mags)),
        "alpha_mag_range": [float(np.min(alpha_mags)), float(np.max(alpha_mags))],
        "alpha_phase_mean_deg": float(np.mean(alpha_phases_deg)),
        "alpha_phase_std_deg": float(np.std(alpha_phases_deg)),
        "nmse_mean_dB": float(np.mean(nmse_per_frame)),
        "nmse_std_dB": float(np.std(nmse_per_frame)),
        "nmse_range_dB": [float(np.min(nmse_per_frame)), float(np.max(nmse_per_frame))],
    }
    return H_srs_c, alphas, nmse_per_frame, info


def filter_period3(H_srs: np.ndarray, H_gt: np.ndarray,
                   frame_ids: np.ndarray, active: np.ndarray,
                   sto_slope: float = 0.0, n_fft: int = 0,
                   nmse_ceil_dB: float = 10.0,
                   abs_slots: Optional[np.ndarray] = None,
                   ) -> Tuple[np.ndarray, int, np.ndarray, dict]:
    """Drop bad frames caused by the period-3 OAI SRS artifact.

    Strategy (two-stage):
      1. Group frames by ``physical_index % 3`` and drop the worst
         group (the one with the highest fraction of NMSE > *nmse_ceil_dB*).
      2. Within the surviving frames, also drop any individual frame
         whose NMSE still exceeds *nmse_ceil_dB* (catches phase-shifts
         where the period-3 pattern occasionally realigns).

    Parameters
    ----------
    H_srs, H_gt : (N, rx, tx, n_sc) complex  – paired & STO-corrected
    frame_ids    : (N,) int  – radio-frame IDs (legacy; used when *abs_slots*
                   is not provided)
    active       : boolean mask (n_sc,)
    sto_slope    : STO phase-ramp slope (passed through to per_frame_ls_align)
    n_fft        : FFT size
    nmse_ceil_dB : frames above this NMSE are considered "bad" (default 10 dB)
    abs_slots    : (N,) int  – SFN-unwrapped absolute slot indices.  When
                   provided, grouping is ``abs_slots % 3`` which correctly
                   tracks the physical SRS periodicity even after
                   align_by_slot drops unmatched frames.  Falls back to
                   ``arange(N) % 3`` when None (legacy).

    Returns
    -------
    keep_mask    : (N,) bool – True for frames that survive filtering
    worst_group  : int 0/1/2 – the discarded ``seq_idx % 3`` group
    nmse_pf      : (N,) float – per-frame NMSE in dB (before filtering)
    info         : dict with per-group statistics
    """
    _, alphas, nmse_pf, _ = per_frame_ls_align(
        H_srs, H_gt, active, sto_slope=sto_slope, n_fft=n_fft)

    N = len(nmse_pf)
    if abs_slots is not None and len(abs_slots) == N:
        seq_groups = np.asarray(abs_slots, dtype=np.int64) % 3
    else:
        seq_groups = np.arange(N) % 3

    group_stats = {}
    for g in range(3):
        mask_g = seq_groups == g
        n_g = int(np.sum(mask_g))
        if n_g == 0:
            group_stats[g] = {"n": 0, "bad_frac": 1.0, "nmse_median": np.inf}
            continue
        nm_g = nmse_pf[mask_g]
        am_g = np.abs(alphas[mask_g])
        group_stats[g] = {
            "n": n_g,
            "bad_frac": float(np.mean(nm_g > nmse_ceil_dB)),
            "nmse_median": float(np.median(nm_g)),
            "nmse_mean": float(np.mean(nm_g)),
            "alpha_mag_mean": float(np.mean(am_g)),
            "alpha_mag_cv": float(np.std(am_g) / (np.mean(am_g) + 1e-30)),
        }

    worst_group = max(group_stats, key=lambda g: group_stats[g]["bad_frac"])

    # Stage 1: drop the worst seq%3 group
    keep_mask = seq_groups != worst_group
    # Stage 2: within survivors, also drop individual outliers
    keep_mask &= nmse_pf <= nmse_ceil_dB

    info = {
        "worst_group": int(worst_group),
        "kept": int(np.sum(keep_mask)),
        "dropped": int(N - np.sum(keep_mask)),
        "drop_pct": float((N - np.sum(keep_mask)) / N * 100),
        "group_stats": group_stats,
    }
    return keep_mask, worst_group, nmse_pf, info


def compute_nmse_vs_N(H_srs: np.ndarray, H_gt: np.ndarray,
                      active: np.ndarray, alphas: np.ndarray,
                      N_points: Optional[int] = 30
                      ) -> Tuple[np.ndarray, np.ndarray]:
    """NMSE as a function of accumulation depth N (should monotonically decrease).

    Accumulates frames 0..N-1 with per-frame α alignment, then computes
    the NMSE of the average channel estimate vs the average GT.

    Returns
    -------
    Ns        : (M,) array of accumulation depths
    nmse_vs_N : (M,) NMSE in dB at each depth
    """
    N_total = H_srs.shape[0]
    H_s = H_srs[:, :, :, active]
    H_g = H_gt[:, :, :, active]

    Ns = np.unique(np.geomspace(1, N_total, num=N_points).astype(int))
    nmse_vs_N = np.empty(len(Ns), dtype=np.float64)

    for i, n in enumerate(Ns):
        aligned_sum = np.zeros_like(H_s[0])
        gt_sum = np.zeros_like(H_g[0])
        for k in range(n):
            aligned_sum += H_s[k] / (alphas[k] + 1e-30)
            gt_sum += H_g[k]
        avg_aligned = aligned_sum / n
        avg_gt = gt_sum / n
        err_power = np.mean(np.abs(avg_aligned - avg_gt) ** 2)
        sig_power = np.mean(np.abs(avg_gt) ** 2)
        nmse_vs_N[i] = 10.0 * np.log10(err_power / (sig_power + 1e-30))

    return Ns, nmse_vs_N


# ── Method E Pipeline: production-ready SRS↔GT alignment ─────────────
#
# Full DSP chain validated on Q4 SNR sweep (see Change_log §47.11–§47.13):
#
#   1. Period-3 filtering  (filter_period3)
#   2. Per-frame STO: estimate → global anchor → clamp ±max_delta
#   3. Apply clamped STO correction
#   4. Per-frame scalar α  (LS fit: SRS ≈ α·GT·scale)
#   5. Phase-only de-rotation (coherent combining)
#   6. Outlier rejection (per-frame NMSE > threshold)
#   7. Cumulative averaging with global α
#
# Design notes:
# - Step 5 uses e^{-j∠α} rather than full /α to avoid noise amplification
#   from low-|α| frames (1–2 dB improvement at low/medium SNR).
# - The per-frame STO uses the SAME estimator direction as estimate_sto
#   (∠(GT · conj(α·SRS))) for consistent sign convention.
# - Residual sub-sample STO is negligible (polyfit is continuous-valued);
#   frequency-domain slope compensation adds 0.0 dB (verified §47.12).


def _estimate_sto_single(H_srs_frame: np.ndarray, H_gt_frame: np.ndarray,
                          active: np.ndarray, n_fft: int) -> float:
    """Per-frame STO slope estimate (median over antenna pairs).

    Same phase convention as ``estimate_sto``:
    slope = polyfit of ∠(GT · conj(α_LS · SRS)) vs freq_idx.

    Returns the slope in rad/bin (not samples).
    """
    ash = np.fft.fftshift(active)
    aidx = np.where(ash)[0]
    center = n_fft // 2
    fidx = aidx.astype(np.float64) - center
    slopes: List[float] = []
    for rx in range(H_srs_frame.shape[0]):
        for tx in range(H_srs_frame.shape[1]):
            g = np.fft.fftshift(H_gt_frame[rx, tx])[aidx]
            s = np.fft.fftshift(H_srs_frame[rx, tx])[aidx]
            a = np.vdot(s, g) / (np.vdot(s, s) + 1e-30)
            pu = np.unwrap(np.angle(g * np.conj(a * s)))
            slopes.append(np.polyfit(fidx, pu, 1)[0])
    return float(np.median(slopes))


def method_e_align(H_srs: np.ndarray, H_gt: np.ndarray,
                   active: np.ndarray, n_sc: int,
                   max_delta_samples: float = 2.0,
                   nmse_ceil_dB: float = 5.0,
                   ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict]:
    """Production Method E alignment: Anchor + Local STO + Phase Derotation + Outlier Reject.

    Parameters
    ----------
    H_srs, H_gt : (N, rx, tx, n_sc) complex — already period-3 filtered & slot-aligned
    active       : boolean mask (n_sc,)
    n_sc         : FFT size / number of subcarriers
    max_delta_samples : STO clamp window around global anchor (±samples)
    nmse_ceil_dB : per-frame NMSE threshold for outlier rejection

    Returns
    -------
    H_aligned    : (N, rx, tx, n_sc) phase-derotated SRS frames (ready for averaging)
    scale        : float — RMS(SRS)/RMS(GT) amplitude ratio
    keep_mask    : (N,) bool — True for non-outlier frames
    nmse_pf      : (N,) per-frame NMSE in dB (after STO + α alignment)
    info         : dict with diagnostics
    """
    N = H_srs.shape[0]
    srs_rms = float(np.sqrt(np.mean(np.abs(H_srs[:, :, :, active]) ** 2)))
    gt_rms = float(np.sqrt(np.mean(np.abs(H_gt[:, :, :, active]) ** 2)))
    scale = srs_rms / (gt_rms + 1e-30)

    # Stage 1: per-frame STO → global anchor → clamp
    raw_slopes = np.array([
        _estimate_sto_single(H_srs[fi], H_gt[fi], active, n_sc)
        for fi in range(N)
    ])
    raw_samples = raw_slopes * n_sc / (2.0 * np.pi)
    anchor_samp = float(np.median(raw_samples))
    anchor_slope = anchor_samp * 2.0 * np.pi / n_sc
    delta_slope = max_delta_samples * 2.0 * np.pi / n_sc
    clamped_slopes = np.clip(raw_slopes,
                             anchor_slope - delta_slope,
                             anchor_slope + delta_slope)
    n_clamped = int(np.sum(np.abs(raw_slopes - clamped_slopes) > 1e-10))

    # Stages 2–5: per-frame STO correction → α estimation → phase derotation
    H_aligned = np.zeros_like(H_srs)
    alphas = np.empty(N, dtype=np.complex128)
    nmse_pf = np.empty(N, dtype=np.float64)

    for fi in range(N):
        # Stage 2: apply clamped STO
        fc = _apply_sto_correction(H_srs[fi:fi + 1], active,
                                   clamped_slopes[fi], n_sc)
        # Stage 3: LS α
        sa = fc[0, :, :, active].flatten()
        ga = (H_gt[fi, :, :, active] * scale).flatten()
        ai = np.vdot(ga, sa) / (np.vdot(ga, ga) + 1e-30)
        alphas[fi] = ai

        # Stage 4: phase-only derotation (coherent combining)
        H_aligned[fi] = fc[0] * np.exp(-1j * np.angle(ai))

        # Per-frame NMSE
        ref = ai * ga
        err = sa - ref
        nmse_pf[fi] = 10.0 * np.log10(
            np.mean(np.abs(err) ** 2) / (np.mean(np.abs(ref) ** 2) + 1e-30))

    # Stage 5: outlier rejection
    keep_mask = nmse_pf <= nmse_ceil_dB

    info = {
        "anchor_samples": anchor_samp,
        "n_clamped": n_clamped,
        "n_kept": int(np.sum(keep_mask)),
        "n_outlier": int(np.sum(~keep_mask)),
        "scale": scale,
        "alpha_mag_mean": float(np.mean(np.abs(alphas[keep_mask]))) if keep_mask.any() else 0.0,
        "alpha_mag_std": float(np.std(np.abs(alphas[keep_mask]))) if keep_mask.any() else 0.0,
        "alpha_phase_std_deg": float(np.std(np.angle(alphas[keep_mask])) * 180 / np.pi) if keep_mask.any() else 0.0,
        "nmse_pf_median_dB": float(np.median(nmse_pf[keep_mask])) if keep_mask.any() else 99.0,
        "sto_range_samples": [float(raw_samples.min()), float(raw_samples.max())],
    }
    return H_aligned, scale, keep_mask, nmse_pf, info


def method_e_nmse_vs_N(H_aligned: np.ndarray, H_gt: np.ndarray,
                       active: np.ndarray, scale: float,
                       keep_mask: np.ndarray,
                       N_points: int = 30,
                       ) -> Tuple[np.ndarray, np.ndarray]:
    """Compute NMSE vs accumulation depth N using Method E aligned frames.

    Averages the phase-derotated frames (kept only), then fits one global
    α for the final NMSE calculation.

    Returns
    -------
    Ns        : (M,) accumulation depths
    nmse_vs_N : (M,) NMSE in dB
    """
    idx = np.where(keep_mask)[0]
    Nt = len(idx)
    if Nt < 2:
        return np.array([1]), np.array([99.0])

    Hs = H_aligned[idx][:, :, :, active]
    Hg = H_gt[idx][:, :, :, active] * scale

    Ns = np.unique(np.geomspace(1, Nt, num=N_points).astype(int))
    nmse = np.empty(len(Ns), dtype=np.float64)

    for i, n in enumerate(Ns):
        s_avg = np.mean(Hs[:n], axis=0).ravel()
        g_avg = np.mean(Hg[:n], axis=0).ravel()
        a = np.vdot(g_avg, s_avg) / (np.vdot(g_avg, g_avg) + 1e-30)
        ref = a * g_avg
        err = s_avg - ref
        nmse[i] = 10.0 * np.log10(
            np.mean(np.abs(err) ** 2) / (np.mean(np.abs(ref) ** 2) + 1e-30))

    return Ns, nmse


# ── Per-subcarrier bias calibration ──────────────────────────────────
#
# The gNB SRS LS estimator has a highly stable per-subcarrier systematic
# bias (bias_ρ > 0.98 across time).  With a calibration split (e.g. use
# first-half frames to estimate the bias, apply to second-half), this
# bias can be subtracted, yielding 8–31 dB improvement at all SNRs.
#
# Two usage modes:
#   1. estimate_sc_bias: compute bias from a set of aligned SRS+GT frames
#   2. apply_sc_bias:    subtract a previously estimated bias from new data
#
# The bias is defined as: bias[k] = avg(SRS_aligned)[k] - α · avg(GT_scaled)[k]
# and is computed per (rx, tx, subcarrier) element.


def estimate_sc_bias(H_aligned: np.ndarray, H_gt: np.ndarray,
                     active: np.ndarray, scale: float,
                     keep_mask: np.ndarray,
                     ) -> Tuple[np.ndarray, complex, dict]:
    """Estimate per-subcarrier bias from Method-E aligned frames.

    Parameters
    ----------
    H_aligned : (N, rx, tx, n_sc) phase-derotated SRS (from method_e_align)
    H_gt      : (N, rx, tx, n_sc) ground truth
    active    : boolean mask (n_sc,)
    scale     : amplitude ratio (from method_e_align)
    keep_mask : (N,) bool — non-outlier frames

    Returns
    -------
    bias       : (rx, tx, K_active) complex bias pattern
    alpha      : complex LS alignment factor used in bias computation
    info       : dict with diagnostics
    """
    idx = np.where(keep_mask)[0]
    Hs = np.mean(H_aligned[idx][:, :, :, active], axis=0)
    Hg = np.mean(H_gt[idx][:, :, :, active] * scale, axis=0)
    alpha = np.vdot(Hg.ravel(), Hs.ravel()) / (np.vdot(Hg.ravel(), Hg.ravel()) + 1e-30)
    bias = Hs - alpha * Hg

    bias_power = float(np.mean(np.abs(bias) ** 2))
    sig_power = float(np.mean(np.abs(alpha * Hg) ** 2))
    info = {
        "n_calib_frames": len(idx),
        "bias_to_signal_dB": 10.0 * np.log10(bias_power / (sig_power + 1e-30)),
        "alpha_mag": float(np.abs(alpha)),
    }
    return bias, alpha, info


def apply_sc_bias(H_aligned: np.ndarray, H_gt: np.ndarray,
                  active: np.ndarray, scale: float,
                  keep_mask: np.ndarray, bias: np.ndarray,
                  N_points: int = 30,
                  ) -> Tuple[float, np.ndarray, np.ndarray]:
    """Subtract pre-estimated bias and compute NMSE (and NMSE vs N).

    Parameters
    ----------
    H_aligned, H_gt, active, scale, keep_mask : same as method_e_nmse_vs_N
    bias   : (rx, tx, K_active) complex — from estimate_sc_bias
    N_points : number of N-axis sample points

    Returns
    -------
    nmse_final : float — final NMSE in dB (all kept frames)
    Ns         : (M,) accumulation depths
    nmse_vs_N  : (M,) NMSE in dB at each depth
    """
    idx = np.where(keep_mask)[0]
    Nt = len(idx)
    if Nt < 2:
        return 99.0, np.array([1]), np.array([99.0])

    Hs = H_aligned[idx][:, :, :, active]
    Hg = H_gt[idx][:, :, :, active] * scale

    avg_s = np.mean(Hs, axis=0) - bias
    avg_g = np.mean(Hg, axis=0)
    a = np.vdot(avg_g.ravel(), avg_s.ravel()) / (np.vdot(avg_g.ravel(), avg_g.ravel()) + 1e-30)
    ref = a * avg_g
    err = avg_s - ref
    nmse_final = float(10.0 * np.log10(
        np.mean(np.abs(err) ** 2) / (np.mean(np.abs(ref) ** 2) + 1e-30)))

    Ns = np.unique(np.geomspace(1, Nt, num=N_points).astype(int))
    nmse_vs_N = np.empty(len(Ns), dtype=np.float64)
    for i, n in enumerate(Ns):
        s_avg = np.mean(Hs[:n], axis=0) - bias
        g_avg = np.mean(Hg[:n], axis=0)
        a_n = np.vdot(g_avg.ravel(), s_avg.ravel()) / (np.vdot(g_avg.ravel(), g_avg.ravel()) + 1e-30)
        ref_n = a_n * g_avg
        err_n = s_avg - ref_n
        nmse_vs_N[i] = 10.0 * np.log10(
            np.mean(np.abs(err_n) ** 2) / (np.mean(np.abs(ref_n) ** 2) + 1e-30))

    return nmse_final, Ns, nmse_vs_N


# ── Metric 1: RSRP ───────────────────────────────────────────────────

def compute_rsrp(H: np.ndarray, active: np.ndarray) -> np.ndarray:
    """Per-frame RSRP for each (rx, tx) pair on active subcarriers.

    H : (N_frames, N_rx, N_tx, N_sc)
    Returns: (N_frames, N_rx, N_tx) in linear power
    """
    H_active = H[:, :, :, active]
    return np.mean(np.abs(H_active) ** 2, axis=-1)


# ── Metric 2: Spatial Covariance ──────────────────────────────────────

def compute_spatial_covariance(H: np.ndarray, active: np.ndarray
                               ) -> Tuple[np.ndarray, np.ndarray]:
    """Compute RX-side and TX-side spatial covariance matrices.

    Averaged over active subcarriers and all frames.

    R_rx[i,j] = (1/NKT) Σ_n Σ_k Σ_t H[n,i,t,k] conj(H[n,j,t,k])
    R_tx[i,j] = (1/NKR) Σ_n Σ_k Σ_r conj(H[n,r,i,k]) H[n,r,j,k]

    Returns R_rx (N_rx, N_rx), R_tx (N_tx, N_tx)
    """
    H_a = H[:, :, :, active]  # (N, rx, tx, K_active)
    N, n_rx, n_tx, K = H_a.shape

    # RX covariance: treat each subcarrier as a snapshot h = H[:, :, t, k] → (rx,)
    R_rx = np.zeros((n_rx, n_rx), dtype=np.complex128)
    for t in range(n_tx):
        # h_stack: (N*K, n_rx)
        h_stack = H_a[:, :, t, :].transpose(0, 2, 1).reshape(-1, n_rx)
        R_rx += h_stack.conj().T @ h_stack
    R_rx /= (N * K * n_tx)

    # TX covariance
    R_tx = np.zeros((n_tx, n_tx), dtype=np.complex128)
    for r in range(n_rx):
        h_stack = H_a[:, r, :, :].transpose(0, 2, 1).reshape(-1, n_tx)
        R_tx += h_stack.conj().T @ h_stack
    R_tx /= (N * K * n_rx)

    return R_rx, R_tx


# ── Metric 3: SVD Singular Values ────────────────────────────────────

def compute_svd_spectrum(H: np.ndarray, active: np.ndarray
                         ) -> Tuple[np.ndarray, np.ndarray]:
    """Per-subcarrier SVD singular values, averaged over frames.

    Returns:
        sv_per_sc : (K_active, min(rx,tx))  mean singular values
        sv_mean   : (min(rx,tx),)           wideband average
    """
    H_a = H[:, :, :, active]
    N, n_rx, n_tx, K = H_a.shape
    n_sv = min(n_rx, n_tx)

    sv_acc = np.zeros((K, n_sv), dtype=np.float64)
    for n in range(N):
        for k in range(K):
            _, s, _ = np.linalg.svd(H_a[n, :, :, k], full_matrices=False)
            sv_acc[k] += s
    sv_per_sc = sv_acc / N
    sv_mean = np.mean(sv_per_sc, axis=0)
    return sv_per_sc, sv_mean


# ── DFT-based channel denoising ──────────────────────────────────────
#
# Industry-standard approach: IFFT → time-domain windowing → FFT.
# Replaces the fragile "global linear STO fit" with a physically
# motivated method that works under frequency-selective fading.

def dft_denoise(H: np.ndarray, active: np.ndarray,
                cp_len: int = 144, guard: int = 8) -> np.ndarray:
    """DFT-based channel estimation denoising.

    Parameters
    ----------
    H      : (..., n_fft) complex array — frequency-domain channel estimate.
             Only active subcarriers carry signal; the rest should be ~0.
    active : (n_fft,) bool mask of active subcarriers
    cp_len : Cyclic prefix length in samples.  For NR mu=1 normal CP,
             the short CP is 144 samples and the long CP is 160.
             The CIR should be contained within this window.
    guard  : Extra samples beyond cp_len to keep (smooth roll-off).

    Returns
    -------
    H_denoised : same shape as H, denoised frequency-domain channel.
                 Non-active subcarriers are zeroed.
    """
    n_fft = H.shape[-1]
    orig_shape = H.shape
    H_flat = H.reshape(-1, n_fft)

    window_len = min(cp_len + guard, n_fft // 2)

    win = np.zeros(n_fft, dtype=np.float64)
    win[:window_len] = 1.0
    win[-window_len:] = 1.0
    if guard > 0:
        taper = np.linspace(1.0, 0.0, guard)
        win[cp_len:cp_len + guard] = taper
        win[n_fft - cp_len - guard:n_fft - cp_len] = taper[::-1]

    H_out = np.zeros_like(H_flat)
    for i in range(H_flat.shape[0]):
        h_time = np.fft.ifft(H_flat[i])
        h_time *= win
        H_denoised_row = np.fft.fft(h_time)
        H_out[i] = H_denoised_row

    H_out = H_out.reshape(orig_shape)
    mask = ~active
    H_out[..., mask] = 0.0
    return H_out


def dft_denoise_frames(H_srs: np.ndarray, active: np.ndarray,
                       cp_len: int = 144, guard: int = 8) -> np.ndarray:
    """Apply DFT denoising to each (frame, rx, tx) independently.

    H_srs : (N_frames, N_rx, N_tx, n_fft)
    """
    N, n_rx, n_tx, n_fft = H_srs.shape
    H_out = np.zeros_like(H_srs)
    for fi in range(N):
        for rx in range(n_rx):
            for tx in range(n_tx):
                H_out[fi, rx, tx] = dft_denoise(
                    H_srs[fi, rx, tx][np.newaxis, :],
                    active, cp_len=cp_len, guard=guard
                )[0]
    return H_out


# ── Metric 4: Estimation SNR ─────────────────────────────────────────

def apply_nta_correction(H_gt: np.ndarray, active: np.ndarray,
                         n_ta_offset: int, n_fft: int) -> np.ndarray:
    """Apply known N_TA_offset phase correction to GT channel.

    OAI's FFT window is shifted by N_TA_offset samples relative to the
    nominal slot boundary used by the Proxy.  This creates a deterministic
    linear phase slope: H_srs(k) = H_gt(k) * exp(-j*2*pi*k*N_TA_offset/N_fft).

    NOTE: This is an ALTERNATIVE to per-frame STO correction, not
    complementary.  If per-frame STO correction is used, it already absorbs
    the N_TA_offset effect (plus TA-induced timing variations).  Applying
    both will double-correct and degrade NMSE.

    Use this when per-frame STO correction is disabled or when you want to
    apply only the known deterministic offset without data-driven estimation.
    """
    if n_ta_offset == 0:
        return H_gt.copy()
    slope = -2.0 * np.pi * n_ta_offset / n_fft
    return _apply_sto_correction(H_gt, active, slope, n_fft)


def _apply_sto_correction(H: np.ndarray, active: np.ndarray,
                          sto_slope: float, n_fft: int) -> np.ndarray:
    """Apply STO phase ramp correction in fftshifted domain.

    Returns a copy of H with the linear phase ramp removed on active SCs.
    """
    H_out = np.zeros_like(H)
    H_out[:, :, :, active] = H[:, :, :, active]
    active_sh = np.fft.fftshift(active)
    active_idx = np.where(active_sh)[0]
    center = n_fft // 2
    freq_idx = active_idx.astype(np.float64) - center
    correction = np.exp(1j * sto_slope * freq_idx)

    H_sh = np.fft.fftshift(H_out, axes=-1)
    H_sh[:, :, :, active_idx] *= correction[np.newaxis, np.newaxis, np.newaxis, :]
    return np.fft.ifftshift(H_sh, axes=-1)


def compute_estimation_snr(H_srs: np.ndarray, H_gt: np.ndarray,
                           active: np.ndarray, sto_slope: float = 0.0,
                           n_fft: int = 0
                           ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, float, float]:
    """Estimation SNR: quality of OAI SRS relative to Sionna GT.

    Optionally applies STO correction to H_srs before alignment.
    Uses global complex LS alignment to compensate amplitude + constant phase,
    then computes SNR = |H_ref|^2 / |H_srs - H_ref|^2.

    Returns
    -------
    snr_per_sc    : (K_active,)  mean SNR per active subcarrier (dB)
    snr_per_frame : (N,)         wideband SNR per frame (dB)
    snr_per_pair  : (N_rx, N_tx) mean wideband SNR per antenna pair (dB)
    snr_wideband  : float        overall mean SNR (dB)
    alpha_mag     : float        magnitude of LS alignment factor
    """
    if sto_slope != 0.0 and n_fft > 0:
        H_srs_c = _apply_sto_correction(H_srs, active, sto_slope, n_fft)
        H_s = H_srs_c[:, :, :, active]
    else:
        H_s = H_srs[:, :, :, active]
    H_g = H_gt[:, :, :, active]

    alpha = np.vdot(H_g, H_s) / (np.vdot(H_g, H_g) + 1e-30)
    H_ref = alpha * H_g
    err = H_s - H_ref

    sig_per_sc = np.mean(np.abs(H_ref) ** 2, axis=(0, 1, 2))
    noise_per_sc = np.mean(np.abs(err) ** 2, axis=(0, 1, 2))
    snr_per_sc = 10.0 * np.log10(sig_per_sc / (noise_per_sc + 1e-30))

    sig_per_frame = np.mean(np.abs(H_ref) ** 2, axis=(1, 2, 3))
    noise_per_frame = np.mean(np.abs(err) ** 2, axis=(1, 2, 3))
    snr_per_frame = 10.0 * np.log10(sig_per_frame / (noise_per_frame + 1e-30))

    sig_per_pair = np.mean(np.abs(H_ref) ** 2, axis=(0, 3))
    noise_per_pair = np.mean(np.abs(err) ** 2, axis=(0, 3))
    snr_per_pair = 10.0 * np.log10(sig_per_pair / (noise_per_pair + 1e-30))

    snr_wideband = 10.0 * np.log10(
        np.mean(sig_per_sc) / (np.mean(noise_per_sc) + 1e-30))

    return snr_per_sc, snr_per_frame, snr_per_pair, float(snr_wideband), float(np.abs(alpha))


# ── Metric 5: PDP helpers ────────────────────────────────────────────

def estimate_sto(H_srs: np.ndarray, H_gt: np.ndarray,
                 active: np.ndarray, n_fft: int) -> Tuple[float, float]:
    """Estimate Symbol Timing Offset via linear phase ramp fitting.

    Averages over all frames and antenna pairs for robustness.

    Returns (sto_samples, slope) where slope is the raw phase ramp per bin.
    """
    H_s_avg = np.mean(H_srs, axis=0)
    H_g_avg = np.mean(H_gt, axis=0)

    H_s_sh = np.fft.fftshift(H_s_avg, axes=-1)
    H_g_sh = np.fft.fftshift(H_g_avg, axes=-1)
    active_sh = np.fft.fftshift(active)
    active_idx = np.where(active_sh)[0]
    center = n_fft // 2
    freq_idx = active_idx.astype(np.float64) - center

    all_slopes = []
    for rx in range(H_s_avg.shape[0]):
        for tx in range(H_s_avg.shape[1]):
            g = H_g_sh[rx, tx, active_idx]
            s = H_s_sh[rx, tx, active_idx]
            a = np.vdot(s, g) / (np.vdot(s, s) + 1e-30)
            phase_diff = np.angle(g * np.conj(a * s))
            phase_uw = np.unwrap(phase_diff)
            coeffs = np.polyfit(freq_idx, phase_uw, 1)
            all_slopes.append(coeffs[0])

    slope = float(np.mean(all_slopes))
    sto_samples = slope * n_fft / (2.0 * np.pi)
    return sto_samples, slope


def compute_pdp(H: np.ndarray, active: np.ndarray, n_fft: int,
                sto_correct_slope: Optional[float] = None
                ) -> Tuple[np.ndarray, np.ndarray]:
    """Power Delay Profile via IFFT of frequency-domain channel.

    Optionally applies STO correction (linear phase ramp removal) before IFFT.

    Parameters
    ----------
    H : (N, rx, tx, n_fft)
    active : boolean mask (n_fft,)
    sto_correct_slope : if given, removes this phase ramp before IFFT

    Returns
    -------
    pdp_avg       : (n_fft,)    PDP averaged over frames and antenna pairs
    pdp_per_frame : (N, n_fft)  PDP averaged over antenna pairs, per frame
    """
    H_padded = np.zeros_like(H)
    H_padded[:, :, :, active] = H[:, :, :, active]

    if sto_correct_slope is not None:
        active_sh = np.fft.fftshift(active)
        active_idx = np.where(active_sh)[0]
        center = n_fft // 2
        freq_idx = active_idx.astype(np.float64) - center
        correction = np.exp(1j * sto_correct_slope * freq_idx)

        H_sh = np.fft.fftshift(H_padded, axes=-1)
        H_sh[:, :, :, active_idx] *= correction[np.newaxis, np.newaxis, np.newaxis, :]
        H_padded = np.fft.ifftshift(H_sh, axes=-1)

    h_time = np.fft.ifft(H_padded, axis=-1)
    pdp_per_frame = np.mean(np.abs(h_time) ** 2, axis=(1, 2))
    pdp_avg = np.mean(pdp_per_frame, axis=0)
    return pdp_avg, pdp_per_frame


def compute_delay_spread(pdp: np.ndarray, n_fft: int,
                         scs_hz: float = 30e3,
                         threshold_mode: str = "fixed",
                         P_FA: float = 1e-6,
                         noise_tail_frac: float = 0.25) -> dict:
    """RMS delay spread and related metrics from a PDP vector.

    Parameters
    ----------
    threshold_mode   : "fixed" (legacy)  — excess delay computed at -10/-20 dB
                       "np"              — taps below NP threshold are zeroed
                                           before mean/RMS/excess metrics.
                                           Excess-delay bounds come from the
                                           first/last above-threshold bins.
    P_FA             : NP false-alarm probability (only if mode == "np")
    noise_tail_frac  : fraction of causal tail used to estimate sigma_n^2

    Returns
    -------
    dict with peak_delay_us, mean_delay_us, rms_delay_spread_us,
    excess_delay_10dB_us, excess_delay_20dB_us, and when NP is used:
        np_threshold, np_sigma2_n, n_active_taps
    """
    dt = 1.0 / (n_fft * scs_hz)
    half = n_fft // 2
    pdp_c = pdp[:half].copy()
    tau = np.arange(half) * dt * 1e6

    np_info = {}
    if threshold_mode == "np":
        thr, sigma2_n = np_pdp_threshold(pdp, noise_tail_frac, P_FA)
        active_taps = pdp_c >= thr
        n_active = int(np.sum(active_taps))
        pdp_c = np.where(active_taps, pdp_c, 0.0)
        np_info = {
            "np_threshold": float(thr),
            "np_sigma2_n": float(sigma2_n),
            "n_active_taps": n_active,
            "P_FA": float(P_FA),
        }

    peak_idx = int(np.argmax(pdp_c))
    peak_delay = tau[peak_idx]

    total = float(np.sum(pdp_c)) + 1e-30
    mean_delay = float(np.sum(tau * pdp_c) / total)
    mean_delay_sq = float(np.sum(tau ** 2 * pdp_c) / total)
    rms_ds = float(np.sqrt(max(mean_delay_sq - mean_delay ** 2, 0.0)))

    if threshold_mode == "np":
        active_idx = np.where(pdp_c > 0.0)[0]
        if len(active_idx) > 1:
            excess_10 = float(tau[active_idx[-1]] - tau[active_idx[0]])
        else:
            excess_10 = 0.0
        excess_20 = excess_10
    else:
        pdp_norm = pdp_c / (np.max(pdp_c) + 1e-30)
        pdp_dB = 10.0 * np.log10(pdp_norm + 1e-30)
        above_10 = np.where(pdp_dB >= -10.0)[0]
        above_20 = np.where(pdp_dB >= -20.0)[0]
        excess_10 = float(tau[above_10[-1]] - tau[above_10[0]]) if len(above_10) > 1 else 0.0
        excess_20 = float(tau[above_20[-1]] - tau[above_20[0]]) if len(above_20) > 1 else 0.0

    out = {
        "peak_delay_us": peak_delay,
        "mean_delay_us": mean_delay,
        "rms_delay_spread_us": rms_ds,
        "excess_delay_10dB_us": excess_10,
        "excess_delay_20dB_us": excess_20,
    }
    out.update(np_info)
    return out


# ── Plotting ──────────────────────────────────────────────────────────

def plot_rsrp_comparison(rsrp_srs: np.ndarray, rsrp_gt: np.ndarray,
                         scale_factor: float, plot_dir: str) -> str:
    """Bar chart comparing per-antenna-pair RSRP (dB scale)."""
    n_rx, n_tx = rsrp_srs.shape[1], rsrp_srs.shape[2]
    rsrp_srs_avg = np.mean(rsrp_srs, axis=0)
    rsrp_gt_avg = np.mean(rsrp_gt, axis=0)

    rsrp_srs_db = 10 * np.log10(rsrp_srs_avg + 1e-30)
    rsrp_gt_db = 10 * np.log10(rsrp_gt_avg * scale_factor ** 2 + 1e-30)

    labels = [f"Rx{r}-Tx{t}" for r in range(n_rx) for t in range(n_tx)]
    x = np.arange(len(labels))
    w = 0.35

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(x - w / 2, rsrp_srs_db.reshape(-1), w, label="OAI SRS", color="#2196F3")
    ax.bar(x + w / 2, rsrp_gt_db.reshape(-1), w, label="Sionna GT (scaled)", color="#FF9800")
    ax.set_ylabel("RSRP (dB, relative)")
    ax.set_title("RSRP Comparison: OAI SRS vs Sionna GT")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.legend()
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    plt.tight_layout()
    path = os.path.join(plot_dir, "rsrp_comparison.png")
    plt.savefig(path, dpi=200)
    plt.close()
    return path


def plot_rsrp_per_frame(rsrp_srs: np.ndarray, rsrp_gt: np.ndarray,
                        scale_factor: float, plot_dir: str) -> str:
    """Per-frame RSRP trend (total power across all antenna pairs)."""
    total_srs = np.sum(rsrp_srs, axis=(1, 2))
    total_gt = np.sum(rsrp_gt, axis=(1, 2)) * scale_factor ** 2

    srs_db = 10 * np.log10(total_srs + 1e-30)
    gt_db = 10 * np.log10(total_gt + 1e-30)

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(srs_db, "o-", label="OAI SRS", linewidth=1.5, markersize=4)
    ax.plot(gt_db, "s--", label="Sionna GT (scaled)", linewidth=1.5, markersize=4)
    ax.set_xlabel("Frame index")
    ax.set_ylabel("Total RSRP (dB)")
    ax.set_title("Per-Frame RSRP Stability")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)
    plt.tight_layout()
    path = os.path.join(plot_dir, "rsrp_per_frame.png")
    plt.savefig(path, dpi=200)
    plt.close()
    return path


def plot_covariance_heatmaps(R_rx_srs: np.ndarray, R_rx_gt: np.ndarray,
                             R_tx_srs: np.ndarray, R_tx_gt: np.ndarray,
                             plot_dir: str) -> str:
    """2x2 heatmap grid: RX/TX covariance magnitude for SRS vs GT."""
    fig, axes = plt.subplots(2, 2, figsize=(10, 9))

    def _plot_cov(ax, R, title):
        R_mag = np.abs(R)
        R_norm = R_mag / (np.max(R_mag) + 1e-30)
        im = ax.imshow(R_norm, cmap="YlOrRd", vmin=0, vmax=1, aspect="equal")
        ax.set_title(title, fontsize=11)
        n = R.shape[0]
        for i in range(n):
            for j in range(n):
                mag = R_mag[i, j]
                phase = np.angle(R[i, j], deg=True)
                ax.text(j, i, f"|{mag:.1f}|\n∠{phase:.0f}°",
                        ha="center", va="center", fontsize=9,
                        color="white" if R_norm[i, j] > 0.5 else "black")
        ax.set_xticks(range(n))
        ax.set_yticks(range(n))
        return im

    _plot_cov(axes[0, 0], R_rx_srs, "OAI SRS — RX Covariance")
    _plot_cov(axes[0, 1], R_rx_gt, "Sionna GT — RX Covariance")
    _plot_cov(axes[1, 0], R_tx_srs, "OAI SRS — TX Covariance")
    _plot_cov(axes[1, 1], R_tx_gt, "Sionna GT — TX Covariance")

    fig.suptitle("Spatial Covariance Matrix Comparison (Normalized Magnitude + Phase)",
                 fontsize=13, fontweight="bold")
    plt.tight_layout(rect=[0, 0, 1, 0.95])
    path = os.path.join(plot_dir, "covariance_heatmaps.png")
    plt.savefig(path, dpi=200)
    plt.close()
    return path


def plot_covariance_correlation(R_srs: np.ndarray, R_gt: np.ndarray,
                                side: str, plot_dir: str) -> str:
    """Normalize diagonal to 1, compare off-diagonal correlation magnitude."""
    def _normalize(R):
        d = np.sqrt(np.diag(np.abs(R)))
        D_inv = np.diag(1.0 / (d + 1e-30))
        return D_inv @ R @ D_inv

    C_srs = _normalize(R_srs)
    C_gt = _normalize(R_gt)
    n = C_srs.shape[0]

    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    for ax, C, label in [(axes[0], C_srs, "OAI SRS"), (axes[1], C_gt, "Sionna GT")]:
        im = ax.imshow(np.abs(C), cmap="coolwarm", vmin=0, vmax=1, aspect="equal")
        ax.set_title(f"{label} — {side} Correlation")
        for i in range(n):
            for j in range(n):
                ax.text(j, i, f"{np.abs(C[i, j]):.3f}",
                        ha="center", va="center", fontsize=11,
                        color="white" if np.abs(C[i, j]) > 0.5 else "black")
        ax.set_xticks(range(n))
        ax.set_yticks(range(n))
        plt.colorbar(im, ax=ax, shrink=0.8)

    plt.suptitle(f"Spatial Correlation Matrix — {side} side", fontsize=12, fontweight="bold")
    plt.tight_layout(rect=[0, 0, 1, 0.93])
    path = os.path.join(plot_dir, f"correlation_{side.lower()}.png")
    plt.savefig(path, dpi=200)
    plt.close()
    return path


def plot_svd_spectrum(sv_srs: np.ndarray, sv_gt: np.ndarray,
                      sv_mean_srs: np.ndarray, sv_mean_gt: np.ndarray,
                      plot_dir: str) -> str:
    """Singular value spectrum per active subcarrier + wideband bar."""
    n_sv = sv_srs.shape[1]
    K = sv_srs.shape[0]

    fig, axes = plt.subplots(1, 2, figsize=(14, 5), gridspec_kw={"width_ratios": [3, 1]})

    colors = ["#2196F3", "#FF9800", "#4CAF50", "#E91E63"]
    for s in range(n_sv):
        axes[0].plot(sv_srs[:, s], label=f"OAI σ{s+1}", linewidth=1.2, color=colors[s])
        axes[0].plot(sv_gt[:, s], "--", label=f"GT σ{s+1}", linewidth=1.2,
                     color=colors[s], alpha=0.7)
    axes[0].set_xlabel("Active subcarrier index")
    axes[0].set_ylabel("Singular value (mean over frames)")
    axes[0].set_title("Per-Subcarrier SVD Spectrum")
    axes[0].legend(ncol=2, fontsize=9)
    axes[0].grid(True, linestyle="--", alpha=0.3)

    x = np.arange(n_sv)
    w = 0.35
    axes[1].bar(x - w / 2, sv_mean_srs, w, label="OAI SRS", color="#2196F3")
    axes[1].bar(x + w / 2, sv_mean_gt, w, label="Sionna GT", color="#FF9800")
    axes[1].set_xticks(x)
    axes[1].set_xticklabels([f"σ{i+1}" for i in range(n_sv)])
    axes[1].set_title("Wideband Mean")
    axes[1].legend(fontsize=9)
    axes[1].grid(axis="y", linestyle="--", alpha=0.4)

    ratio = sv_mean_srs / (sv_mean_gt + 1e-30)
    cond_srs = sv_mean_srs[0] / (sv_mean_srs[-1] + 1e-30)
    cond_gt = sv_mean_gt[0] / (sv_mean_gt[-1] + 1e-30)
    fig.text(0.5, -0.02,
             f"Scale ratio σ_srs/σ_gt: [{', '.join(f'{r:.2f}' for r in ratio)}]  |  "
             f"Condition number — OAI: {cond_srs:.2f}, GT: {cond_gt:.2f}",
             ha="center", fontsize=10, style="italic")

    plt.tight_layout()
    path = os.path.join(plot_dir, "svd_spectrum.png")
    plt.savefig(path, dpi=200, bbox_inches="tight")
    plt.close()
    return path


def plot_condition_number_per_frame(H_srs: np.ndarray, H_gt: np.ndarray,
                                    active: np.ndarray, plot_dir: str) -> str:
    """Condition number trend across frames (wideband average)."""
    def _cn_per_frame(H, active):
        H_a = H[:, :, :, active]
        N, _, _, K = H_a.shape
        cn = np.zeros(N)
        for n in range(N):
            s_all = []
            for k in range(K):
                _, s, _ = np.linalg.svd(H_a[n, :, :, k], full_matrices=False)
                s_all.append(s[0] / (s[-1] + 1e-30))
            cn[n] = np.mean(s_all)
        return cn

    cn_srs = _cn_per_frame(H_srs, active)
    cn_gt = _cn_per_frame(H_gt, active)

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(cn_srs, "o-", label="OAI SRS", linewidth=1.5, markersize=4)
    ax.plot(cn_gt, "s--", label="Sionna GT", linewidth=1.5, markersize=4)
    ax.set_xlabel("Frame index")
    ax.set_ylabel("Condition Number (avg over subcarriers)")
    ax.set_title("Per-Frame Channel Condition Number")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)
    plt.tight_layout()
    path = os.path.join(plot_dir, "condition_number.png")
    plt.savefig(path, dpi=200)
    plt.close()
    return path


def plot_snr_per_subcarrier(snr_per_sc: np.ndarray, plot_dir: str) -> str:
    """Estimation SNR vs active subcarrier index."""
    fig, ax = plt.subplots(figsize=(14, 5))
    ax.plot(snr_per_sc, linewidth=0.8, color="#2196F3", alpha=0.7)
    mean_snr = float(np.mean(snr_per_sc))
    ax.axhline(y=mean_snr, color="#E91E63", linestyle="--", linewidth=1.5,
               label=f"Mean = {mean_snr:.1f} dB")
    p5, p95 = np.percentile(snr_per_sc, [5, 95])
    ax.axhspan(p5, p95, alpha=0.08, color="#2196F3", label=f"5-95 pctl: [{p5:.1f}, {p95:.1f}] dB")
    ax.set_xlabel("Active subcarrier index")
    ax.set_ylabel("Estimation SNR (dB)")
    ax.set_title("Per-Subcarrier Estimation SNR (OAI SRS vs Sionna GT)")
    ax.legend(fontsize=10)
    ax.grid(True, linestyle="--", alpha=0.4)
    plt.tight_layout()
    path = os.path.join(plot_dir, "snr_per_subcarrier.png")
    plt.savefig(path, dpi=200)
    plt.close()
    return path


def plot_snr_per_frame(snr_per_frame: np.ndarray, plot_dir: str) -> str:
    """Wideband estimation SNR time series."""
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(snr_per_frame, "o-", linewidth=1.5, markersize=4, color="#2196F3")
    mean_snr = float(np.mean(snr_per_frame))
    ax.axhline(y=mean_snr, color="#E91E63", linestyle="--", linewidth=1.5,
               label=f"Mean = {mean_snr:.1f} dB")
    ax.set_xlabel("Frame index")
    ax.set_ylabel("Wideband Estimation SNR (dB)")
    ax.set_title("Per-Frame Estimation SNR Stability")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)
    plt.tight_layout()
    path = os.path.join(plot_dir, "snr_per_frame.png")
    plt.savefig(path, dpi=200)
    plt.close()
    return path


def plot_pdp_comparison(pdp_srs: np.ndarray, pdp_gt: np.ndarray,
                        n_fft: int, scs_hz: float,
                        sto_samples: float, plot_dir: str) -> str:
    """PDP overlay (dB scale) for OAI SRS vs Sionna GT."""
    dt = 1.0 / (n_fft * scs_hz)
    half = n_fft // 2
    tau = np.arange(half) * dt * 1e6

    pdp_srs_h = pdp_srs[:half]
    pdp_gt_h = pdp_gt[:half]

    pdp_srs_dB = 10.0 * np.log10(pdp_srs_h / (np.max(pdp_srs_h) + 1e-30) + 1e-30)
    pdp_gt_dB = 10.0 * np.log10(pdp_gt_h / (np.max(pdp_gt_h) + 1e-30) + 1e-30)

    max_tau = min(tau[-1], 5.0)
    mask = tau <= max_tau

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(tau[mask], pdp_srs_dB[mask], linewidth=1.5,
            label="OAI SRS (STO corrected)", color="#2196F3")
    ax.plot(tau[mask], pdp_gt_dB[mask], linewidth=1.5,
            label="Sionna GT", color="#FF9800", alpha=0.8)
    ax.axhline(y=-10, color="gray", linestyle=":", alpha=0.5, label="-10 dB")
    ax.axhline(y=-20, color="gray", linestyle="--", alpha=0.5, label="-20 dB")
    ax.set_xlabel("Delay (\u03bcs)")
    ax.set_ylabel("Normalized PDP (dB)")
    ax.set_title(f"Power Delay Profile Comparison (STO = {sto_samples:.2f} samples)")
    ax.set_ylim(bottom=-40)
    ax.legend(fontsize=10)
    ax.grid(True, linestyle="--", alpha=0.4)
    plt.tight_layout()
    path = os.path.join(plot_dir, "pdp_comparison.png")
    plt.savefig(path, dpi=200)
    plt.close()
    return path


def plot_delay_spread_per_frame(pdp_pf_srs: np.ndarray, pdp_pf_gt: np.ndarray,
                                n_fft: int, scs_hz: float,
                                plot_dir: str) -> str:
    """Per-frame RMS delay spread comparison."""
    dt = 1.0 / (n_fft * scs_hz)
    half = n_fft // 2
    tau = np.arange(half) * dt * 1e6

    def _rms_ds(pdp_row):
        pdp_c = pdp_row[:half]
        total = float(np.sum(pdp_c)) + 1e-30
        m1 = np.sum(tau * pdp_c) / total
        m2 = np.sum(tau ** 2 * pdp_c) / total
        return np.sqrt(max(m2 - m1 ** 2, 0.0))

    N = pdp_pf_srs.shape[0]
    ds_srs = np.array([_rms_ds(pdp_pf_srs[n]) for n in range(N)])
    ds_gt = np.array([_rms_ds(pdp_pf_gt[n]) for n in range(N)])

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(ds_srs, "o-", label="OAI SRS", linewidth=1.5, markersize=4, color="#2196F3")
    ax.plot(ds_gt, "s--", label="Sionna GT", linewidth=1.5, markersize=4, color="#FF9800")
    ax.set_xlabel("Frame index")
    ax.set_ylabel("RMS Delay Spread (\u03bcs)")
    ax.set_title("Per-Frame RMS Delay Spread Comparison")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)
    plt.tight_layout()
    path = os.path.join(plot_dir, "pdp_delay_spread.png")
    plt.savefig(path, dpi=200)
    plt.close()
    return path


# ── Plot: Per-frame LS alpha diagnostics ──────────────────────────────

def plot_per_frame_alpha(alphas: np.ndarray, plot_dir: str) -> str:
    """Per-frame LS alpha: magnitude and phase trends."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True)

    mags = np.abs(alphas)
    phases = np.angle(alphas) * 180.0 / np.pi

    ax1.plot(mags, "o-", markersize=3, linewidth=1, color="#2196F3")
    ax1.axhline(y=np.mean(mags), color="#E91E63", linestyle="--",
                label=f"Mean = {np.mean(mags):.2f}")
    ax1.set_ylabel("|alpha_n|")
    ax1.set_title("Per-Frame LS Alpha (AGC Equivalent)")
    ax1.legend()
    ax1.grid(True, linestyle="--", alpha=0.4)

    ax2.plot(phases, "o-", markersize=3, linewidth=1, color="#FF9800")
    ax2.axhline(y=np.mean(phases), color="#E91E63", linestyle="--",
                label=f"Mean = {np.mean(phases):.1f} deg")
    ax2.set_ylabel("Phase(alpha_n) [deg]")
    ax2.set_xlabel("Frame index")
    ax2.legend()
    ax2.grid(True, linestyle="--", alpha=0.4)

    plt.tight_layout()
    path = os.path.join(plot_dir, "per_frame_ls_alpha.png")
    plt.savefig(path, dpi=200)
    plt.close()
    return path


def plot_nmse_vs_N(Ns: np.ndarray, nmse_vs_N: np.ndarray,
                   plot_dir: str) -> str:
    """NMSE as a function of accumulation depth N."""
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(Ns, nmse_vs_N, "o-", markersize=5, linewidth=2, color="#4CAF50")
    ax.set_xlabel("Number of accumulated frames N")
    ax.set_ylabel("NMSE (dB)")
    ax.set_title("NMSE vs Accumulation Depth (per-frame LS alpha aligned)")
    ax.set_xscale("log")
    ax.grid(True, linestyle="--", alpha=0.4)

    is_mono = all(nmse_vs_N[i] >= nmse_vs_N[i + 1] - 0.5
                  for i in range(len(nmse_vs_N) - 1))
    status = "MONOTONE" if is_mono else "NON-MONOTONE"
    ax.text(0.95, 0.95, status, transform=ax.transAxes, fontsize=12,
            ha="right", va="top", fontweight="bold",
            color="green" if is_mono else "red",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.8))

    plt.tight_layout()
    path = os.path.join(plot_dir, "nmse_vs_N.png")
    plt.savefig(path, dpi=200)
    plt.close()
    return path


def plot_per_frame_nmse(nmse_per_frame: np.ndarray, plot_dir: str) -> str:
    """Per-frame NMSE time series."""
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(nmse_per_frame, "o-", linewidth=1.5, markersize=4, color="#9C27B0")
    mean_v = float(np.mean(nmse_per_frame))
    ax.axhline(y=mean_v, color="#E91E63", linestyle="--", linewidth=1.5,
               label=f"Mean = {mean_v:.1f} dB")
    ax.set_xlabel("Frame index")
    ax.set_ylabel("NMSE (dB)")
    ax.set_title("Per-Frame NMSE (per-frame LS alpha aligned)")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)
    plt.tight_layout()
    path = os.path.join(plot_dir, "per_frame_nmse.png")
    plt.savefig(path, dpi=200)
    plt.close()
    return path


# ── Main ──────────────────────────────────────────────────────────────

# ═════════════════════════════════════════════════════════════════
# Direction A: Digital-Twin look-ahead fidelity (channel prediction)
# ═════════════════════════════════════════════════════════════════

def compute_prediction_accuracy(H_srs: np.ndarray, H_gt: np.ndarray,
                                active: np.ndarray, horizons) -> Dict[int, dict]:
    """DT look-ahead fidelity: apply the offline PKF predictor to the aligned SRS
    estimate sequence and score the k-step-ahead prediction against the FUTURE GT
    (per-frame, per-link LS-alpha NMSE, scale-invariant). Also reports the
    zero-order-hold (hold-last) baseline. A Digital Twin's core value is
    anticipating the channel, so the predicted-vs-future-GT NMSE quantifies how
    far ahead the twin stays faithful.

    Returns {k: {'pred_db', 'zoh_db', 'gain_db', 'n'}} (gain = zoh - pred).

    NOTE: a single FIXED complex calibration (SRS<->GT amplitude + constant
    hardware offset, estimated globally over all on-time pairs) is applied to GT
    instead of a per-frame LS alpha. A per-frame complex alpha would absorb the
    very time-varying phase that prediction supplies, collapsing pred==zoh; the
    fixed calibration removes only the constant offset and keeps the channel's
    time evolution, so the phase-extrapolation benefit is measurable."""
    # Lazy import avoids the test_srs_2d_offline <-> digital_twin_stats import cycle.
    from test_srs_2d_offline import time_pkf_predict

    F, R, T, _ = H_srs.shape
    aidx = np.where(active)[0]
    horizons = sorted({int(k) for k in horizons if 1 <= int(k) < F})
    if not horizons or aidx.size == 0:
        return {}
    eps = 1e-30

    # Fixed global complex calibration mapping GT -> SRS units (amplitude + const
    # offset), from the on-time (same-frame) relationship over all frames/links.
    s_all = H_srs[:, :, :, aidx].ravel()
    g_all = H_gt[:, :, :, aidx].ravel()
    cal = np.vdot(g_all, s_all) / (np.vdot(g_all, g_all) + eps)

    acc = {k: dict(pe=0.0, psig=0.0, ze=0.0, zsig=0.0, n=0) for k in horizons}
    for rx in range(R):
        for tx in range(T):
            Hf = H_srs[:, rx, tx, aidx]                  # (F, n_act)
            d = np.abs(np.diff(Hf, axis=0)) ** 2
            r_meas = 0.5 * float(np.median(np.mean(d, axis=0))) if d.size else 1.0
            r_meas = max(r_meas, 1e-9)
            _filt, preds, zoh = time_pkf_predict(Hf, r_meas, horizons)
            for k in horizons:
                for f in range(k, F):
                    pv = preds[k][f]
                    if not np.all(np.isfinite(pv)):
                        continue
                    zv = zoh[k][f]
                    ref = cal * H_gt[f, rx, tx, aidx]      # future GT in SRS units
                    acc[k]["pe"] += float(np.sum(np.abs(pv - ref) ** 2))
                    acc[k]["psig"] += float(np.sum(np.abs(ref) ** 2))
                    acc[k]["ze"] += float(np.sum(np.abs(zv - ref) ** 2))
                    acc[k]["zsig"] += float(np.sum(np.abs(ref) ** 2))
                    acc[k]["n"] += 1

    out = {}
    for k in horizons:
        a = acc[k]
        if a["n"] == 0:
            continue
        pred_db = 10.0 * np.log10(a["pe"] / (a["psig"] + eps) + eps)
        zoh_db = 10.0 * np.log10(a["ze"] / (a["zsig"] + eps) + eps)
        out[k] = {"pred_db": pred_db, "zoh_db": zoh_db,
                  "gain_db": zoh_db - pred_db, "n": a["n"]}
    return out


def plot_prediction_accuracy(acc: Dict[int, dict], plot_dir: str) -> str:
    """NMSE-vs-horizon curves: PKF phase-prediction vs hold-last baseline."""
    ks = sorted(acc.keys())
    pred = [acc[k]["pred_db"] for k in ks]
    zoh = [acc[k]["zoh_db"] for k in ks]
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(ks, pred, "o-", label="PKF prediction")
    ax.plot(ks, zoh, "s--", label="zero-order hold (last SRS)")
    ax.set_xlabel("prediction horizon k (SRS frames)")
    ax.set_ylabel("NMSE vs future GT (dB)")
    ax.set_title("Digital Twin look-ahead fidelity (Direction A)")
    ax.grid(True, alpha=0.3)
    ax.legend()
    plt.tight_layout()
    path = os.path.join(plot_dir, "prediction_accuracy.png")
    plt.savefig(path, dpi=200)
    plt.close()
    return path


def main():
    ap = argparse.ArgumentParser(description="Digital Twin Statistical Cross-Validation")
    ap.add_argument("--log-dir",
                    default="/home/dclserver78/OAI_luuuuuu/DevChannelProxyJIN/logs/latest")
    ap.add_argument("--gt-subdir", default="sionna_gt")
    ap.add_argument("--srs-symbol", type=int, default=12)
    ap.add_argument("--ue-idx", type=int, default=0)
    ap.add_argument("--scs", type=float, default=30e3,
                    help="Subcarrier spacing in Hz (default: 30000)")
    ap.add_argument("--plot-dir",
                    default="/home/dclserver78/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/data_out")
    ap.add_argument("--report-json", default=None)
    ap.add_argument("--use-method-d", action="store_true",
                    help="Treat Method-D (per-frame alpha + per-frame STO) as the primary estimator-quality metric in the summary.")
    ap.add_argument("--method-d-max-delta-samples", type=float, default=8.0,
                    help="Clamp window for Method-D per-frame STO around the median anchor (default: 8 samples).")
    ap.add_argument("--method-d-nmse-ceil-db", type=float, default=99.0,
                    help="Outlier threshold used for Method-D keep_mask (default: 99 dB keeps all frames).")
    ap.add_argument("--predict-horizons", type=lambda s: [int(x) for x in s.split(",") if x.strip()],
                    default=[1, 2, 4, 8, 16],
                    help="Direction A: SRS look-ahead horizons (frames) for the "
                         "DT prediction-accuracy metric. Empty string disables it.")
    args = ap.parse_args()

    print("=" * 70)
    print("  Digital Twin Statistical Cross-Validation Pipeline")
    print("=" * 70)

    # ── Load data ─────────────────────────────────────────────────────
    print("\n[1/7] Loading SRS data...")
    H_srs, srs_meta = load_srs_v2(args.log_dir)
    n_rx, n_tx, n_sc = srs_meta["rx"], srs_meta["tx"], srs_meta["n_sc"]
    print(f"  SRS: {H_srs.shape[0]} frames, {n_rx}x{n_tx} MIMO, {n_sc} subcarriers")

    print("\n[2/7] Loading Sionna GT data...")
    gt_dir = os.path.join(args.log_dir, args.gt_subdir)
    H_gt_all, gt_slot_ids = load_gt(gt_dir, srs_symbol=args.srs_symbol,
                                    ue_idx=args.ue_idx, return_slot_ids=True)
    print(f"  GT:  {H_gt_all.shape[0]} frames available")

    if gt_slot_ids.size > 0 and "abs_slots" in srs_meta:
        srs_abs = srs_meta["abs_slots"]
        srs_keep, gt_keep, median_gap = align_by_slot(
            srs_abs, gt_slot_ids, tol_slots=20)
        H_srs = H_srs[srs_keep]
        H_gt = H_gt_all[gt_keep]
        n_frames = len(srs_keep)
        print(f"  Align: SLOT  paired {n_frames} "
              f"(median gap {median_gap} slots)")
    else:
        n_frames = min(H_srs.shape[0], H_gt_all.shape[0])
        H_srs = H_srs[:n_frames]
        H_gt = H_gt_all[:n_frames]
        print(f"  Align: INDEX (legacy fallback) — {n_frames} frames")

    active = get_active_mask(H_srs)
    n_active = int(np.sum(active))
    print(f"  Active subcarriers: {n_active} / {n_sc}")

    # ── Compute amplitude scaling factor (OAI c16_t -> Sionna float) ──
    srs_rms = np.sqrt(np.mean(np.abs(H_srs[:, :, :, active]) ** 2))
    gt_rms = np.sqrt(np.mean(np.abs(H_gt[:, :, :, active]) ** 2))
    scale = srs_rms / (gt_rms + 1e-30)
    print(f"  Amplitude scale factor (SRS/GT): {scale:.2f}")

    # ── Metric 1: RSRP ───────────────────────────────────────────────
    print("\n[3/7] Computing RSRP...")
    rsrp_srs = compute_rsrp(H_srs, active)
    rsrp_gt = compute_rsrp(H_gt, active)

    rsrp_srs_mean = np.mean(rsrp_srs, axis=0)
    rsrp_gt_mean = np.mean(rsrp_gt, axis=0)

    rsrp_srs_db = 10 * np.log10(rsrp_srs_mean + 1e-30)
    rsrp_gt_db = 10 * np.log10(rsrp_gt_mean * scale ** 2 + 1e-30)

    print(f"  RSRP (dB, relative):")
    for r in range(n_rx):
        for t in range(n_tx):
            diff = rsrp_srs_db[r, t] - rsrp_gt_db[r, t]
            print(f"    Rx{r}-Tx{t}:  SRS={rsrp_srs_db[r,t]:.2f} dB, "
                  f"GT(scaled)={rsrp_gt_db[r,t]:.2f} dB, "
                  f"delta={diff:+.2f} dB")

    srs_pattern = rsrp_srs_mean.flatten() / (np.sum(rsrp_srs_mean) + 1e-30)
    gt_pattern = rsrp_gt_mean.flatten() / (np.sum(rsrp_gt_mean) + 1e-30)
    pattern_cos = float(np.dot(srs_pattern, gt_pattern) /
                        (np.linalg.norm(srs_pattern) * np.linalg.norm(gt_pattern) + 1e-30))
    print(f"  RSRP pattern cosine similarity: {pattern_cos:.6f}")

    total_srs = np.sum(rsrp_srs, axis=(1, 2))
    total_gt = np.sum(rsrp_gt, axis=(1, 2))
    srs_spread = 10 * np.log10(np.max(total_srs) / (np.min(total_srs) + 1e-30) + 1e-30)
    gt_spread = 10 * np.log10(np.max(total_gt) / (np.min(total_gt) + 1e-30) + 1e-30)
    print(f"  Frame-to-frame RSRP spread: SRS={srs_spread:.2f} dB, GT={gt_spread:.2f} dB")

    # ── Metric 2: Spatial Covariance ──────────────────────────────────
    print("\n[4/7] Computing Spatial Covariance Matrices...")
    R_rx_srs, R_tx_srs = compute_spatial_covariance(H_srs, active)
    R_rx_gt, R_tx_gt = compute_spatial_covariance(H_gt, active)

    def _cov_similarity(R1, R2):
        a = np.vdot(R2.flatten(), R1.flatten()) / (np.vdot(R2.flatten(), R2.flatten()) + 1e-30)
        err = np.linalg.norm(R1 - a * R2, "fro")
        ref = np.linalg.norm(R1, "fro")
        return float(err / (ref + 1e-30))

    def _correlation_matrix(R):
        d = np.sqrt(np.abs(np.diag(R)))
        D_inv = np.diag(1.0 / (d + 1e-30))
        return D_inv @ R @ D_inv

    C_rx_srs = _correlation_matrix(R_rx_srs)
    C_rx_gt = _correlation_matrix(R_rx_gt)
    C_tx_srs = _correlation_matrix(R_tx_srs)
    C_tx_gt = _correlation_matrix(R_tx_gt)

    rx_rel_err = _cov_similarity(R_rx_srs, R_rx_gt)
    tx_rel_err = _cov_similarity(R_tx_srs, R_tx_gt)

    print(f"  RX Covariance — relative Frobenius error: {rx_rel_err:.4f}")
    print(f"  TX Covariance — relative Frobenius error: {tx_rel_err:.4f}")
    print(f"  RX Correlation matrix (OAI):")
    for i in range(n_rx):
        row = "    " + "  ".join(f"{np.abs(C_rx_srs[i,j]):.4f}" for j in range(n_rx))
        print(row)
    print(f"  RX Correlation matrix (GT):")
    for i in range(n_rx):
        row = "    " + "  ".join(f"{np.abs(C_rx_gt[i,j]):.4f}" for j in range(n_rx))
        print(row)
    print(f"  TX Correlation matrix (OAI):")
    for i in range(n_tx):
        row = "    " + "  ".join(f"{np.abs(C_tx_srs[i,j]):.4f}" for j in range(n_tx))
        print(row)
    print(f"  TX Correlation matrix (GT):")
    for i in range(n_tx):
        row = "    " + "  ".join(f"{np.abs(C_tx_gt[i,j]):.4f}" for j in range(n_tx))
        print(row)

    # ── Metric 3: SVD ─────────────────────────────────────────────────
    print("\n[5/7] Computing SVD Singular Value Spectrum...")

    H_gt_scaled = H_gt * scale
    sv_srs, sv_mean_srs = compute_svd_spectrum(H_srs, active)
    sv_gt, sv_mean_gt = compute_svd_spectrum(H_gt_scaled, active)

    n_sv = sv_mean_srs.shape[0]
    print(f"  Wideband mean singular values:")
    for s in range(n_sv):
        ratio = sv_mean_srs[s] / (sv_mean_gt[s] + 1e-30)
        print(f"    s{s+1}: SRS={sv_mean_srs[s]:.2f}, GT={sv_mean_gt[s]:.2f}, ratio={ratio:.4f}")

    cond_srs = sv_mean_srs[0] / (sv_mean_srs[-1] + 1e-30)
    cond_gt = sv_mean_gt[0] / (sv_mean_gt[-1] + 1e-30)
    print(f"  Condition number — SRS: {cond_srs:.2f}, GT: {cond_gt:.2f}")

    for s in range(n_sv):
        corr = float(np.corrcoef(sv_srs[:, s], sv_gt[:, s])[0, 1])
        print(f"  s{s+1} per-subcarrier Pearson correlation: {corr:.6f}")

    # ── STO estimation (shared by SNR and PDP) ────────────────────────
    print("\n[6/8] Estimating STO and computing Estimation SNR...")
    sto_samples, sto_slope = estimate_sto(H_srs, H_gt, active, n_sc)
    print(f"  Estimated STO: {sto_samples:.2f} samples")

    # ── Metric 4: Estimation SNR alignment ladder ────────────────────
    # Method A: one global complex alpha, no STO.
    snr_per_sc_raw, snr_per_frame_raw, _, snr_wb_raw, _ = \
        compute_estimation_snr(H_srs, H_gt, active)
    # Method B: one global STO slope, then one global complex alpha.
    snr_per_sc, snr_per_frame, snr_per_pair, snr_wideband, snr_alpha = \
        compute_estimation_snr(H_srs, H_gt, active,
                               sto_slope=sto_slope, n_fft=n_sc)
    # Method C: per-frame complex alpha only, no STO.
    _, pf_alphas_no_sto, pf_nmse_no_sto, pf_info_no_sto = \
        per_frame_ls_align(H_srs, H_gt, active, sto_slope=0.0, n_fft=0)
    method_c_snr = -float(np.mean(pf_nmse_no_sto))
    # Method D: per-frame STO + per-frame complex alpha.
    H_method_d, method_d_scale, method_d_keep, method_d_nmse_pf, method_d_info = \
        method_e_align(H_srs, H_gt, active, n_sc,
                       max_delta_samples=args.method_d_max_delta_samples,
                       nmse_ceil_dB=args.method_d_nmse_ceil_db)
    method_d_snr_all = -float(np.mean(method_d_nmse_pf))
    method_d_snr_kept = -float(np.mean(method_d_nmse_pf[method_d_keep])) if np.any(method_d_keep) else float("-inf")
    method_d_outlier_frac = float(np.mean(~method_d_keep) * 100.0)
    method_d_sto_clamped_frac = float(method_d_info["n_clamped"] / max(1, n_frames) * 100.0)

    # Sensitivity sweep: estimator ranking must not depend on the STO clamp.
    # Keep this diagnostic cheap and deterministic by running on the same
    # paired frames with fixed deltas.
    method_d_delta_results = {}
    for delta in (2.0, 5.0, 8.0):
        _, _, keep_delta, nmse_delta, info_delta = method_e_align(
            H_srs, H_gt, active, n_sc,
            max_delta_samples=delta,
            nmse_ceil_dB=args.method_d_nmse_ceil_db)
        key = f"{delta:g}"
        method_d_delta_results[key] = {
            "snr_all_dB": -float(np.mean(nmse_delta)),
            "snr_kept_dB": -float(np.mean(nmse_delta[keep_delta])) if np.any(keep_delta) else float("-inf"),
            "nmse_outlier_fraction_pct": float(np.mean(~keep_delta) * 100.0),
            "sto_clamped_fraction_pct": float(info_delta["n_clamped"] / max(1, n_frames) * 100.0),
            "n_clamped": int(info_delta["n_clamped"]),
            "anchor_samples": float(info_delta["anchor_samples"]),
            "sto_range_samples": info_delta["sto_range_samples"],
        }

    print(f"  Wideband estimation SNR (raw / STO-corrected): "
          f"{snr_wb_raw:.2f} / {snr_wideband:.2f} dB")
    print(f"  Global LS |alpha|: {snr_alpha:.4f}")
    print(f"  Alignment ladder:")
    print(f"    method_a_global_alpha_no_sto:      {snr_wb_raw:.2f} dB")
    print(f"    method_b_global_alpha_global_sto:  {snr_wideband:.2f} dB")
    print(f"    method_c_per_frame_alpha_no_sto:   {method_c_snr:.2f} dB")
    print(f"    method_d_per_frame_alpha_sto:      {method_d_snr_all:.2f} dB "
          f"(kept={method_d_snr_kept:.2f} dB, nmse_outlier={method_d_outlier_frac:.1f}%, "
          f"sto_clamped={method_d_sto_clamped_frac:.1f}%)")
    print(f"    gain_from_alignment A->D:          {method_d_snr_all - snr_wb_raw:+.2f} dB")
    print(f"  Method-D delta sweep:")
    for delta_key in ("2", "5", "8"):
        r = method_d_delta_results[delta_key]
        print(f"    method_d_delta={delta_key:>3s}: all={r['snr_all_dB']:.2f} dB, "
              f"kept={r['snr_kept_dB']:.2f} dB, "
              f"sto_outlier={r['sto_clamped_fraction_pct']:.1f}%, "
              f"nmse_outlier={r['nmse_outlier_fraction_pct']:.1f}%")
    print(f"  Method-D STO: anchor={method_d_info['anchor_samples']:.2f} samples, "
          f"range=[{method_d_info['sto_range_samples'][0]:.2f}, {method_d_info['sto_range_samples'][1]:.2f}], "
          f"clamped={method_d_info['n_clamped']}/{n_frames}")
    print(f"  Per-frame SNR range: [{np.min(snr_per_frame):.1f}, {np.max(snr_per_frame):.1f}] dB")
    print(f"  Per-subcarrier SNR: mean={np.mean(snr_per_sc):.1f} dB, "
          f"5th pctl={np.percentile(snr_per_sc, 5):.1f} dB, "
          f"95th pctl={np.percentile(snr_per_sc, 95):.1f} dB")
    print(f"  Per-antenna-pair SNR (dB):")
    for r in range(n_rx):
        for t in range(n_tx):
            print(f"    Rx{r}-Tx{t}: {snr_per_pair[r, t]:.2f} dB")

    # ── Metric 4b: Per-frame LS α alignment (replaces C-code AGC) ────
    print("\n[6b/8] Per-frame LS alpha alignment (AGC equivalent)...")
    H_srs_sto, pf_alphas, pf_nmse, pf_info = \
        per_frame_ls_align(H_srs, H_gt, active,
                           sto_slope=sto_slope, n_fft=n_sc)

    print(f"  Per-frame |alpha|: mean={pf_info['alpha_mag_mean']:.2f}, "
          f"std={pf_info['alpha_mag_std']:.2f}, "
          f"range=[{pf_info['alpha_mag_range'][0]:.2f}, {pf_info['alpha_mag_range'][1]:.2f}]")
    print(f"  Per-frame phase(alpha): mean={pf_info['alpha_phase_mean_deg']:.1f} deg, "
          f"std={pf_info['alpha_phase_std_deg']:.1f} deg")
    print(f"  Per-frame NMSE: mean={pf_info['nmse_mean_dB']:.2f} dB, "
          f"range=[{pf_info['nmse_range_dB'][0]:.1f}, {pf_info['nmse_range_dB'][1]:.1f}] dB")

    # NMSE vs N (monotonic decrease check)
    Ns, nmse_vs_N = compute_nmse_vs_N(H_srs_sto, H_gt, active, pf_alphas)
    is_monotone = all(nmse_vs_N[i] >= nmse_vs_N[i + 1] - 0.5
                      for i in range(len(nmse_vs_N) - 1))
    print(f"  NMSE vs N: N=1 → {nmse_vs_N[0]:.2f} dB, "
          f"N={Ns[-1]} → {nmse_vs_N[-1]:.2f} dB, "
          f"monotone={'YES' if is_monotone else 'NO (check!)'}")

    # ── Metric 5: PDP ────────────────────────────────────────────────
    print("\n[7/7] Computing Power Delay Profile...")

    pdp_srs, pdp_pf_srs = compute_pdp(H_srs, active, n_sc, sto_correct_slope=sto_slope)
    pdp_gt, pdp_pf_gt = compute_pdp(H_gt, active, n_sc, sto_correct_slope=None)

    ds_srs = compute_delay_spread(pdp_srs, n_sc, args.scs)
    ds_gt = compute_delay_spread(pdp_gt, n_sc, args.scs)

    print(f"  PDP metrics (OAI SRS, STO-corrected):")
    print(f"    Peak delay:        {ds_srs['peak_delay_us']:.3f} us")
    print(f"    Mean delay:        {ds_srs['mean_delay_us']:.3f} us")
    print(f"    RMS delay spread:  {ds_srs['rms_delay_spread_us']:.3f} us")
    print(f"    Excess delay @-10dB: {ds_srs['excess_delay_10dB_us']:.3f} us")
    print(f"    Excess delay @-20dB: {ds_srs['excess_delay_20dB_us']:.3f} us")
    print(f"  PDP metrics (Sionna GT):")
    print(f"    Peak delay:        {ds_gt['peak_delay_us']:.3f} us")
    print(f"    Mean delay:        {ds_gt['mean_delay_us']:.3f} us")
    print(f"    RMS delay spread:  {ds_gt['rms_delay_spread_us']:.3f} us")
    print(f"    Excess delay @-10dB: {ds_gt['excess_delay_10dB_us']:.3f} us")
    print(f"    Excess delay @-20dB: {ds_gt['excess_delay_20dB_us']:.3f} us")

    # PDP shape correlation (causal half, above noise floor)
    half = n_sc // 2
    pdp_srs_c = pdp_srs[:half]
    pdp_gt_c = pdp_gt[:half]
    above_floor = (pdp_srs_c > np.max(pdp_srs_c) * 1e-4) | (pdp_gt_c > np.max(pdp_gt_c) * 1e-4)
    if np.sum(above_floor) > 2:
        pdp_corr = float(np.corrcoef(pdp_srs_c[above_floor], pdp_gt_c[above_floor])[0, 1])
    else:
        pdp_corr = 0.0
    print(f"  PDP shape Pearson correlation: {pdp_corr:.6f}")

    # ── Direction A: DT look-ahead fidelity (SRS channel prediction) ──
    pred_acc = {}
    if args.predict_horizons:
        print("\n[+] Direction A: SRS prediction accuracy vs horizon (DT look-ahead)...")
        pred_acc = compute_prediction_accuracy(H_srs, H_gt, active, args.predict_horizons)
        if pred_acc:
            print("  horizon   PKF-pred   hold-last   gain(zoh-pred)")
            print("  " + "-" * 50)
            for k in sorted(pred_acc):
                d = pred_acc[k]
                print(f"  k={k:<5d}  {d['pred_db']:+8.2f}   {d['zoh_db']:+8.2f}    "
                      f"{d['gain_db']:+7.2f} dB  (n={d['n']})")
        else:
            print("  (too few aligned frames for the requested horizons; skipped)")

    # ── Plotting ──────────────────────────────────────────────────────
    print("\n[*] Generating plots...")
    os.makedirs(args.plot_dir, exist_ok=True)
    saved = []
    if pred_acc:
        saved.append(plot_prediction_accuracy(pred_acc, args.plot_dir))

    saved.append(plot_rsrp_comparison(rsrp_srs, rsrp_gt, scale, args.plot_dir))
    saved.append(plot_rsrp_per_frame(rsrp_srs, rsrp_gt, scale, args.plot_dir))
    saved.append(plot_covariance_heatmaps(R_rx_srs, R_rx_gt, R_tx_srs, R_tx_gt, args.plot_dir))
    saved.append(plot_covariance_correlation(R_rx_srs, R_rx_gt, "RX", args.plot_dir))
    saved.append(plot_covariance_correlation(R_tx_srs, R_tx_gt, "TX", args.plot_dir))
    saved.append(plot_svd_spectrum(sv_srs, sv_gt, sv_mean_srs, sv_mean_gt, args.plot_dir))
    saved.append(plot_condition_number_per_frame(H_srs, H_gt_scaled, active, args.plot_dir))
    saved.append(plot_snr_per_subcarrier(snr_per_sc, args.plot_dir))
    saved.append(plot_snr_per_frame(snr_per_frame, args.plot_dir))
    saved.append(plot_pdp_comparison(pdp_srs, pdp_gt, n_sc, args.scs, sto_samples, args.plot_dir))
    saved.append(plot_delay_spread_per_frame(pdp_pf_srs, pdp_pf_gt, n_sc, args.scs, args.plot_dir))
    saved.append(plot_per_frame_alpha(pf_alphas, args.plot_dir))
    saved.append(plot_per_frame_nmse(pf_nmse, args.plot_dir))
    saved.append(plot_nmse_vs_N(Ns, nmse_vs_N, args.plot_dir))

    for p in saved:
        print(f"  -> {os.path.basename(p)}")

    # ── JSON Report ───────────────────────────────────────────────────
    report = {
        "log_dir": args.log_dir,
        "n_frames": n_frames,
        "mimo_config": f"{n_rx}x{n_tx}",
        "n_active_sc": n_active,
        "amplitude_scale_factor": float(scale),
        "rsrp": {
            "pattern_cosine_similarity": pattern_cos,
            "per_pair_srs_dB": rsrp_srs_db.tolist(),
            "per_pair_gt_scaled_dB": rsrp_gt_db.tolist(),
            "frame_spread_srs_dB": float(srs_spread),
            "frame_spread_gt_dB": float(gt_spread),
        },
        "covariance": {
            "rx_frobenius_relative_error": rx_rel_err,
            "tx_frobenius_relative_error": tx_rel_err,
            "rx_correlation_srs": np.abs(C_rx_srs).tolist(),
            "rx_correlation_gt": np.abs(C_rx_gt).tolist(),
            "tx_correlation_srs": np.abs(C_tx_srs).tolist(),
            "tx_correlation_gt": np.abs(C_tx_gt).tolist(),
        },
        "svd": {
            "wideband_sv_srs": sv_mean_srs.tolist(),
            "wideband_sv_gt": sv_mean_gt.tolist(),
            "condition_number_srs": float(cond_srs),
            "condition_number_gt": float(cond_gt),
            "per_sc_pearson": [
                float(np.corrcoef(sv_srs[:, s], sv_gt[:, s])[0, 1])
                for s in range(n_sv)
            ],
        },
        "snr": {
            "wideband_raw_dB": snr_wb_raw,
            "wideband_sto_corrected_dB": snr_wideband,
            "primary_method": "method_d_per_frame_alpha_sto" if args.use_method_d else "method_b_global_alpha_global_sto",
            "sto_samples": sto_samples,
            "ls_alpha_magnitude": snr_alpha,
            "per_frame_mean_dB": float(np.mean(snr_per_frame)),
            "per_frame_std_dB": float(np.std(snr_per_frame)),
            "per_sc_mean_dB": float(np.mean(snr_per_sc)),
            "per_sc_5th_pctl_dB": float(np.percentile(snr_per_sc, 5)),
            "per_sc_95th_pctl_dB": float(np.percentile(snr_per_sc, 95)),
            "per_pair_dB": snr_per_pair.tolist(),
            "alignment_methods": {
                "method_a_global_alpha_no_sto_dB": snr_wb_raw,
                "method_b_global_alpha_global_sto_dB": snr_wideband,
                "method_c_per_frame_alpha_no_sto_dB": method_c_snr,
                "method_d_per_frame_alpha_sto_all_dB": method_d_snr_all,
                "method_d_per_frame_alpha_sto_kept_dB": method_d_snr_kept,
                "gain_a_to_d_dB": method_d_snr_all - snr_wb_raw,
                "gain_b_to_d_dB": method_d_snr_all - snr_wideband,
                "method_d_outlier_fraction_pct": method_d_outlier_frac,
                "method_d_sto_clamped_fraction_pct": method_d_sto_clamped_frac,
                "method_d_anchor_samples": method_d_info["anchor_samples"],
                "method_d_sto_range_samples": method_d_info["sto_range_samples"],
                "method_d_n_clamped": method_d_info["n_clamped"],
                "method_d_scale": method_d_info["scale"],
                "method_d_max_delta_samples": args.method_d_max_delta_samples,
                "method_d_nmse_ceil_dB": args.method_d_nmse_ceil_db,
                "method_d_delta_sweep": method_d_delta_results,
            },
        },
        "pdp": {
            "sto_samples": sto_samples,
            "pdp_shape_correlation": pdp_corr,
            "srs": ds_srs,
            "gt": ds_gt,
        },
        "per_frame_ls_alpha": {
            **pf_info,
            "nmse_vs_N_depths": Ns.tolist(),
            "nmse_vs_N_dB": nmse_vs_N.tolist(),
            "nmse_vs_N_monotone": bool(is_monotone),
        },
        "prediction_lookahead": {
            str(k): {"pred_nmse_dB": pred_acc[k]["pred_db"],
                     "zoh_nmse_dB": pred_acc[k]["zoh_db"],
                     "gain_dB": pred_acc[k]["gain_db"],
                     "n_samples": pred_acc[k]["n"]}
            for k in sorted(pred_acc)
        },
        "plots": [os.path.basename(p) for p in saved],
    }

    out = args.report_json or os.path.join(args.log_dir, "stats_report.json")
    try:
        with open(out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nReport saved: {out}")
    except PermissionError:
        out = "/tmp/stats_report.json"
        with open(out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nReport saved (fallback): {out}")

    # ── Summary ───────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  CROSS-VALIDATION SUMMARY  ({} frames, {}x{} MIMO)".format(n_frames, n_rx, n_tx))
    print("=" * 70)
    print(f"  1. RSRP pattern similarity:       {pattern_cos:.6f}  (1.0 = perfect)")
    print(f"  2. RX covariance relative error:  {rx_rel_err:.4f}  (0.0 = perfect)")
    print(f"     TX covariance relative error:  {tx_rel_err:.4f}  (0.0 = perfect)")
    for s in range(n_sv):
        corr = float(np.corrcoef(sv_srs[:, s], sv_gt[:, s])[0, 1])
        print(f"  3. SVD s{s+1} spectral correlation:  {corr:.6f}  (1.0 = perfect)")
    print(f"     Condition number — SRS: {cond_srs:.2f}, GT: {cond_gt:.2f}")
    if args.use_method_d:
        print(f"  4. Estimation SNR (method-D):     {method_d_snr_all:.2f} dB")
        print(f"     Legacy wideband method-B:      {snr_wideband:.2f} dB")
    else:
        print(f"  4. Estimation SNR (wideband):     {snr_wideband:.2f} dB")
        print(f"     Method-D SNR (diagnostic):     {method_d_snr_all:.2f} dB")
    print(f"     Method-D STO range:            [{method_d_info['sto_range_samples'][0]:.2f}, "
          f"{method_d_info['sto_range_samples'][1]:.2f}] samples")
    print(f"     Method-D STO clamped:          {method_d_info['n_clamped']}/{n_frames}")
    print(f"     Alignment gain A→D:            {method_d_snr_all - snr_wb_raw:+.2f} dB")
    print(f"     Per-frame SNR std:             {np.std(snr_per_frame):.2f} dB")
    print(f"  5. PDP shape correlation:         {pdp_corr:.6f}  (1.0 = perfect)")
    print(f"     RMS delay spread — SRS: {ds_srs['rms_delay_spread_us']:.3f} us, "
          f"GT: {ds_gt['rms_delay_spread_us']:.3f} us")
    print(f"     STO correction applied:        {sto_samples:.2f} samples")
    print(f"  6. Per-frame LS alpha (AGC equiv):")
    print(f"     |alpha| mean={pf_info['alpha_mag_mean']:.2f}, "
          f"std={pf_info['alpha_mag_std']:.2f}")
    print(f"     Per-frame NMSE mean:           {pf_info['nmse_mean_dB']:.2f} dB")
    print(f"     NMSE vs N monotone:            {'YES' if is_monotone else 'NO'}")
    print(f"     NMSE @ N={Ns[-1]}:             {nmse_vs_N[-1]:.2f} dB")
    print("=" * 70)


if __name__ == "__main__":
    main()
