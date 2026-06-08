#!/usr/bin/env python3
"""replay_true2d.py - measure true-2D MMSE performance on REAL captured SRS LS.

Attach only matters for *capturing* the raw LS; the estimator comparison is done
OFFLINE here so it is decoupled from attach flakiness and run-to-run variance.

Input: a run-dir captured with SRS_ESTIMATOR=passthru (dumps the raw combed LS,
i.e. srs_matrix_gNB_*.bin holds the per-comb-SC LS) + sionna_gt/.

It applies, to the SAME real LS, the FULL ladder and reports per-frame STO+scalar
aligned NMSE vs GT (the fair metric, see eval_nmse_sto):
  - raw-LS    : combed LS scattered to nearest SC (no smoothing)        [floor]
  - true2d    : windowed Hermitian-Toeplitz freq LMMSE, R = FT{EMA PDP} [the algorithm,
                bit-identical to the C true2d freq stage per M2.4]
  - oracle    : same windowed Toeplitz but R = genie (from the GT PDP)  [ceiling]

Lower NMSE = better. true2d should sit well below raw-LS and approach oracle on a
frequency-selective channel (the 5-7 dB true-2D gain), which is the success test.

Usage:
  # capture once (cdl_a attaches reliably; cdl_b/c retry):
  #   sudo P1B_NPZ="" NPY_DIR=data_out/cdl_c UE_SPEED=0 CHANNEL_SEED=42 ONLY_SNR=25 \\
  #     MAX_FRAMES=400 UL_PRE_GAIN=1.0 SRS_ESTIMATOR=passthru SRS_PERIOD_SLOTS=10 \\
  #     GT_SAVE_EVERY=100 SRS_ONLY_CHANNEL=1 bash run_q4_snr_sweep_v9.sh
  python3 replay_true2d.py --run-dir ../../logs/<sweep>/snr_25dB --win 16
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

import numpy as np

from eval_nmse_clean import (load_srs, index_gt_slots, align_frames,
                             load_gt_by_refs, get_active_mask,
                             unify_reference_frame)
from check_c_freq_mirror import windowed_toeplitz_comb
from test_srs_2d_offline import (robust_noise_floor, time_pkf_riccati,
                                  time_pkf_predict, time_wiener_predict)

EPS = 1e-30


def detect_comb(active_idx, n_sc):
    """Infer (first_sc, k_tc, num_pilots) of the SRS comb from the active SCs.
    Active SCs wrap around DC; we unwrap to a linear pilot sequence."""
    a = np.sort(active_idx)
    # spacing = most common diff among contiguous run (the comb K_TC)
    d = np.diff(a)
    d = d[d > 0]
    k_tc = int(np.bincount(d).argmax()) if d.size else 1
    k_tc = max(1, k_tc)
    # pilots are at first + i*k_tc (mod n_sc); recover first as the start of the
    # band that, stepping by k_tc mod n_sc, covers all active SCs.
    aset = set(int(x) for x in active_idx)
    # candidate firsts: any active SC whose (sc - k_tc) mod n_sc is NOT active
    firsts = [sc for sc in active_idx if ((sc - k_tc) % n_sc) not in aset]
    first = int(min(firsts)) if firsts else int(a[0])
    # count pilots by walking
    n = 0
    sc = first
    while sc in aset:
        n += 1
        sc = (sc + k_tc) % n_sc
        if n > len(active_idx):
            break
    return first, k_tc, n


def extract_pilots(H_frame_link, first, k_tc, num_pilots, n_sc):
    """Pull combed pilots (complex) at first + i*k_tc % n_sc."""
    out = np.empty(num_pilots, dtype=np.complex128)
    sc = first
    for i in range(num_pilots):
        out[i] = H_frame_link[sc]
        sc = (sc + k_tc) % n_sc
    return out


def data_R(pilots, n_sc, max_dk, pdp_state, alpha=0.95):
    """C-faithful data R: contiguous pilots -> n_sc IFFT -> EMA PDP -> robust
    floor -> R(dk)=FT{clean PDP}. Normalized R(0)=1; returns (R, noise_norm)."""
    buf = np.zeros(n_sc, dtype=np.complex128)
    buf[:pilots.size] = pilots
    inst = np.abs(np.fft.ifft(buf)) ** 2
    if pdp_state[0] is None:
        pdp_state[0] = inst.copy()
    else:
        pdp_state[0] = alpha * pdp_state[0] + (1 - alpha) * inst
    pdp = pdp_state[0]
    floor = robust_noise_floor(pdp, k=3.0)
    clean = np.maximum(pdp - floor, 0.0)
    m = np.arange(n_sc)
    R = np.array([np.sum(clean * np.exp(-2j * np.pi * dk * m / n_sc))
                  for dk in range(max_dk + 1)], dtype=np.complex128)
    R0 = R[0].real
    if R0 <= 0:
        return None, 0.0
    # noise_norm = total noise / total signal (diagonal regularizer in R(0)=1 units)
    noise_norm = float(min(max((floor * n_sc) / (R0 + EPS), 1e-4), 2.0))
    return R / R0, noise_norm


def genie_R(H_gt_link_frames, active_idx, n_sc, max_dk):
    """Genie R from the GT: average PDP over frames -> R(dk)=FT{PDP}. R(0)=1."""
    pdp = np.zeros(n_sc)
    for H in H_gt_link_frames:
        full = np.zeros(n_sc, dtype=np.complex128)
        full[active_idx] = H[active_idx]
        pdp += np.abs(np.fft.ifft(full)) ** 2
    pdp /= len(H_gt_link_frames)
    m = np.arange(n_sc)
    R = np.array([np.sum(pdp * np.exp(-2j * np.pi * dk * m / n_sc))
                  for dk in range(max_dk + 1)], dtype=np.complex128)
    return R / (R[0].real + EPS)


def sto_nmse(s, g, k, n_sc, taus):
    """min over STO ramps of (err/sig) after optimal scalar (true-bin linear phase)."""
    ramps = np.exp(-2j * np.pi * np.outer(taus, k) / n_sc)
    gg = g[None, :] * ramps
    a = (gg.conj() @ s) / (np.sum(np.abs(gg) ** 2, axis=1) + EPS)
    err = np.mean(np.abs(s[None, :] - a[:, None] * gg) ** 2, axis=1)
    sig = np.mean(np.abs(a[:, None] * gg) ** 2, axis=1)
    j = int(np.argmin(err / (sig + EPS)))
    return float(err[j]), float(sig[j])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, help="passthru snr_XdB dir")
    ap.add_argument("--win", type=int, default=16)
    ap.add_argument("--srs-symbol", type=int, default=12)
    ap.add_argument("--tol", type=int, default=20)
    ap.add_argument("--tau-range", type=float, default=20.0)
    ap.add_argument("--tau-step", type=float, default=1.0)
    ap.add_argument("--max-frames", type=int, default=12,
                    help="subsample to this many paired frames (static channel "
                         "frames are near-identical; the Toeplitz solve is O(span*win^2) "
                         "per frame so keep small). Default 12.")
    ap.add_argument("--time-pkf", action="store_true",
                    help="also run the time stage (per-link omega phase-predictive "
                         "Kalman over consecutive frames) on top of the freq stage. "
                         "Use with a DYNAMIC capture (speed>0) + GT_SAVE_EVERY=1; on a "
                         "static channel the time stage adds ~0 (nothing to track).")
    ap.add_argument("--predict-phases", type=str, default="0,1,2",
                    help="comma list of E1 phase modes to compare in prediction: "
                         "0=scalar omega, 1=per-band omega, 2=linear-phase/STO")
    ap.add_argument("--wiener-order", type=int, default=4,
                    help="temporal Wiener (linear-MMSE) predictor order p (past samples); "
                         "0 disables. This is the classical prediction ceiling.")
    ap.add_argument("--wiener-r-ema", type=float, default=0.05,
                    help="EMA gain for the causal online autocorrelation R(dt) estimate")
    ap.add_argument("--wiener-diag-load", type=float, default=0.2,
                    help="diagonal loading (x r0) for the Wiener-Hopf solve (regularization); "
                         "online R is noisy so heavier loading (~0.2-0.3) is needed")
    ap.add_argument("--wiener-genie", action="store_true",
                    help="use whole-window (genie) R(dt) instead of causal online EMA "
                         "(reference upper bound; default is the deployable online EMA)")
    ap.add_argument("--wiener-compare-r", action="store_true",
                    help="add columns comparing online raw-EMA R vs parametric-Doppler R "
                         "vs genie R (the R-estimation ablation)")
    ap.add_argument("--predict-horizon", type=int, default=0,
                    help="Direction A: also run multi-step SRS channel PREDICTION. "
                         "For each SRS frame n, extrapolate the PKF state k=1..K frames "
                         "ahead (phase-only) and score the k-step-ahead prediction vs the "
                         "FUTURE GT frame n+k (STO+scalar aligned NMSE). Needs CONSECUTIVE "
                         "dynamic frames (speed>0 + GT_SAVE_EVERY=1). 0 = off.")
    args = ap.parse_args()

    rd = args.run_dir
    H, meta = load_srs(rd)
    gs, gr = index_gt_slots(rd + "/sionna_gt", srs_symbol=args.srs_symbol)
    # V3 SRS carries an absolute sample-slot id in the SAME axis as the GT slot
    # ids -> pair exactly at offset 0 (no SFN median-C guess, which mis-pairs on
    # native runs and collapses even the oracle to the noise floor). V2 falls back.
    if "sample_slots" in meta:
        si, gi, gap = unify_reference_frame(meta["sample_slots"], gs)
    else:
        si, gi, gap = align_frames(meta["abs_slots"], gs, tol=args.tol)
    if len(si) < 2:
        sys.exit("too few paired frames")
    H_s, H_g = H[si], load_gt_by_refs(gr, gi)
    # subsample frames. For the time stage (PKF) we need CONSECUTIVE frames so the
    # frame-to-frame channel correlation is preserved; for freq-only an
    # evenly-spaced subset is fine (static frames are near-identical).
    if H_s.shape[0] > args.max_frames:
        if args.time_pkf or args.predict_horizon > 0:
            sel = np.arange(args.max_frames)
        else:
            sel = np.linspace(0, H_s.shape[0] - 1, args.max_frames).astype(int)
        H_s, H_g = H_s[sel], H_g[sel]
    n_sc = meta["n_sc"]
    R_, T_, F = meta["rx"], meta["tx"], H_s.shape[0]

    active = get_active_mask(H_s)
    aidx = np.where(active)[0]
    first, k_tc, n_pil = detect_comb(aidx, n_sc)
    win = min(args.win, n_pil)
    max_dk = win * k_tc
    print(f"[replay] paired={F} frames, comb: first={first} k_tc={k_tc} "
          f"n_pil={n_pil} win={win} active={aidx.size}")

    taus = np.arange(-args.tau_range, args.tau_range + args.tau_step, args.tau_step)
    kbin = aidx.astype(np.float64)

    # genie R per link (from all GT frames)
    Rg = {}
    for rx in range(R_):
        for tx in range(T_):
            Rg[(rx, tx)] = genie_R([H_g[f, rx, tx] for f in range(F)], aidx, n_sc, max_dk)

    pdp_state = {(rx, tx): [None] for rx in range(R_) for tx in range(T_)}
    na = aidx.size
    # store per (link) the freq estimates over frames for the optional time stage
    est_raw = np.zeros((R_, T_, F, na), dtype=np.complex128)
    est_t2d = np.zeros((R_, T_, F, na), dtype=np.complex128)
    est_orc = np.zeros((R_, T_, F, na), dtype=np.complex128)
    G = np.zeros((R_, T_, F, na), dtype=np.complex128)

    for f in range(F):
        for rx in range(R_):
            for tx in range(T_):
                ls = H_s[f, rx, tx]
                G[rx, tx, f] = H_g[f, rx, tx, aidx]
                pilots = extract_pilots(ls, first, k_tc, n_pil, n_sc)
                raw = np.zeros(n_sc, dtype=np.complex128)
                sc = first
                for i in range(n_pil):
                    for j in range(k_tc):
                        raw[(sc + j) % n_sc] = pilots[i]
                    sc = (sc + k_tc) % n_sc
                Rd, nn = data_R(pilots, n_sc, max_dk, pdp_state[(rx, tx)])
                est_t = raw if Rd is None else windowed_toeplitz_comb(pilots, Rd, nn, n_sc, first, k_tc, win)
                est_o = windowed_toeplitz_comb(pilots, Rg[(rx, tx)], nn if Rd is not None else 1e-2,
                                               n_sc, first, k_tc, win)
                est_raw[rx, tx, f] = raw[aidx]
                est_t2d[rx, tx, f] = est_t[aidx]
                est_orc[rx, tx, f] = est_o[aidx]

    def ladder_nmse(est_RTFa):
        e = s = 0.0
        for rx in range(R_):
            for tx in range(T_):
                for f in range(F):
                    ee, ss = sto_nmse(est_RTFa[rx, tx, f], G[rx, tx, f], kbin, n_sc, taus)
                    e += ee; s += ss
        return 10 * np.log10(e / (s + EPS) + EPS)

    results = [("raw (LS)", ladder_nmse(est_raw)),
               ("true2d-freq", ladder_nmse(est_t2d)),
               ("oracle-freq", ladder_nmse(est_orc))]

    # ── optional time stage: per-link PKF over consecutive frames on the freq est ──
    # NOTE: the old "true2d-freq+PKF(time)" ladder was REMOVED (2026-06-07) because
    # it was misleading, NOT because the time stage is broken. The C nr_srs_pkf_update
    # is bit-exact (<=1 LSB) with time_pkf_riccati (see c_unit_tests/golden_pkf.py),
    # so the algorithm is fine. The bogus ~-2..-15 dB came from feeding the WRONG
    # input here: on true2d captures the dumped srs_matrix is already the C FINAL
    # estimate, so re-running an offline freq+time stage on top double-processes it;
    # production also has a high-SNR legacy-gate blend (nr_ul_channel_estimation.c)
    # this offline ladder lacked. Verify real time-stage quality with eval_nmse_sto.py
    # (reads the C output vs GT: -16..-45 dB across captures), not here.

    print("\n=== true-2D performance on REAL captured LS (STO+scalar aligned NMSE) ===")
    base = results[0][1]
    for name, nmse in results:
        gain = "" if name.startswith("raw") else f"  ({base - nmse:+.2f} dB vs raw)"
        print(f"  {name:24s}: {nmse:+.2f} dB{gain}")
    print("\n(raw=combed-LS floor, true2d-freq=windowed Toeplitz data-R [= C freq stage],"
          " oracle=genie-R ceiling. lower=better. Time-stage quality: use eval_nmse_sto.py.)")

    # ── Direction A: multi-step SRS channel PREDICTION vs future GT ──
    if args.predict_horizon > 0:
        if F < args.predict_horizon + 2:
            print(f"\n[predict] too few frames (F={F}) for horizon K={args.predict_horizon}; skipped.")
        else:
            horizons = list(range(1, args.predict_horizon + 1))

            # SELF-REFERENCED prediction metric (clean, operational, speed-comparable):
            # score the k-step prediction against the ACTUAL measured future SRS
            # estimate est_t2d[...,f] (same reference domain as the prediction input),
            # NOT against GT. This removes the SRS per-frame phase-reference drift /
            # calibration that contaminated the GT-based metric (and which made the
            # absolute NMSE non-comparable across captures). It is exactly the
            # gap-filling task: "predict the next CSI sample from past ones".
            #   zoh[k][f] NMSE = |est[f-k]      - est[f]|^2 / |est[f]|^2  (channel change)
            #   pred[k][f] NMSE = |est[f-k]*e^{jwk} - est[f]|^2 / |est[f]|^2
            def horizon_nmse(est_RTFa, k):
                e = s = 0.0
                for rx in range(R_):
                    for tx in range(T_):
                        tgt = est_t2d[rx, tx]            # (F, na) measured estimate
                        for f in range(k, F):
                            pv = est_RTFa[rx, tx, f]
                            if not np.all(np.isfinite(pv)):
                                continue
                            g = tgt[f]
                            e += float(np.sum(np.abs(pv - g) ** 2))
                            s += float(np.sum(np.abs(g) ** 2))
                return 10 * np.log10(e / (s + EPS) + EPS)

            # E1 phase modes to compare: 0=scalar omega, 1=per-band omega,
            # 2=linear phase (omega0 + slope*m, the STO-ramp model). ZOH is
            # mode-independent (no rotation), computed once.
            modes = [int(x) for x in args.predict_phases.split(",") if x.strip() != ""]
            zoh_RTF = {k: np.full((R_, T_, F, na), np.nan, dtype=np.complex128)
                       for k in horizons}
            pred_RTF = {pm: {k: np.full((R_, T_, F, na), np.nan, dtype=np.complex128)
                             for k in horizons} for pm in modes}
            use_wiener = args.wiener_order > 0
            # Wiener R-estimation variants to compute.
            if use_wiener and args.wiener_compare_r:
                wvars = [(f"W-emaR(p{args.wiener_order})", dict(online=True, r_param=False)),
                         ("W-param1D", dict(online=True, r_param=True)),
                         ("W-kline2", dict(online=True, r_kline=2)),
                         ("W-kline3", dict(online=True, r_kline=3)),
                         ("W-psdR", dict(online=True, r_psd=True)),
                         ("W-genie", dict(online=False, r_param=False))]
            elif use_wiener:
                wtag = "genie" if args.wiener_genie else f"emaR={args.wiener_r_ema}"
                wvars = [(f"Wiener(p={args.wiener_order},{wtag})",
                          dict(online=(not args.wiener_genie), r_param=False))]
            else:
                wvars = []
            wiener_RTF = {nm: {k: np.full((R_, T_, F, na), np.nan, dtype=np.complex128)
                               for k in horizons} for nm, _ in wvars}
            for rx in range(R_):
                for tx in range(T_):
                    Hf = est_t2d[rx, tx]
                    d = np.abs(np.diff(Hf, axis=0)) ** 2
                    r_meas = 0.5 * float(np.median(np.mean(d, axis=0))) if d.size else 1.0
                    r_meas = max(r_meas, 1e-9)
                    for pm in modes:
                        _filt, preds, zoh = time_pkf_predict(Hf, r_meas, horizons, phase_mode=pm)
                        for k in horizons:
                            pred_RTF[pm][k][rx, tx] = preds[k]
                            if pm == modes[0]:
                                zoh_RTF[k][rx, tx] = zoh[k]
                    for nm, kw in wvars:
                        wp = time_wiener_predict(Hf, horizons, p_order=args.wiener_order,
                                                 r_ema=args.wiener_r_ema,
                                                 diag_load=args.wiener_diag_load, **kw)
                        for k in horizons:
                            wiener_RTF[nm][k][rx, tx] = wp[k]

            mname = {0: "mode0(scalar)", 1: "mode1(per-band)", 2: "mode2(linear/STO)"}
            cols = [(mname[pm], pred_RTF[pm]) for pm in modes]
            for nm, _ in wvars:
                cols.append((nm, wiener_RTF[nm]))
            print("\n=== Direction A: SRS prediction vs FUTURE SRS estimate (self-referenced NMSE, dB) ===")
            hdr = "horizon   ZOH      " + "  ".join(f"{nm:>17s}" for nm, _ in cols)
            print(hdr)
            print("-" * len(hdr))
            zoh_db = {k: horizon_nmse(zoh_RTF[k], k) for k in horizons}
            col_db = [(nm, {k: horizon_nmse(arr[k], k) for k in horizons}) for nm, arr in cols]
            for k in horizons:
                cells = []
                for nm, db in col_db:
                    g = zoh_db[k] - db[k]            # gain vs ZOH (dB)
                    cells.append(f"{db[k]:+7.2f}({g:+.2f})")
                print(f"  k={k:<5d}  {zoh_db[k]:+7.2f}  " + "  ".join(cells))
            print("(cell = pred_NMSE(gain_vs_ZOH); more-negative pred = better; gain>0 = beats hold-last)")
            kk = [k for k in horizons if k <= 5]
            for nm, db in col_db:
                avg = float(np.mean([zoh_db[k] - db[k] for k in kk])) if kk else 0.0
                print(f"  [{nm}] mean gain vs ZOH over k<=5: {avg:+.2f} dB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
