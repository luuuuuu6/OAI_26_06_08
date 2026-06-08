#!/usr/bin/env python3
"""#4 validation: faithfully MIRROR the C nr_srs_mmse_freq_filter true2d recipe
(combed pilots -> contiguous -> NFFT IFFT -> PDP -> R(dk)=FT{PDP} -> windowed
Toeplitz w/ Wiener-Hopf conjugate -> reconstruct dense) and compare against the
correct GENIE windowed Toeplitz on the same combed pilots. Answers: does the
2048 zero-pad + contiguous-comb mapping reproduce the correct frequency LMMSE on
a frequency-selective channel (where mmse1d was never validated, P1B being flat)?

float math (the #4 question is calibration, not int16 quantization).
"""
from __future__ import annotations
import argparse
import numpy as np
from pathlib import Path
from test_srs_2d_offline import synth_cdl_channel, correlation_from_pdp, robust_noise_floor


def windowed_toeplitz_comb(pilots, R_sc, sigma2, n_sc, first, k_tc, win):
    """Reconstruct all n_sc from combed pilots via per-target windowed Toeplitz
    LMMSE (Wiener-Hopf conjugate). R_sc(dk) is the freq autocorrelation indexed
    by SUBCARRIER lag dk (length>=max lag). pilots: complex[n_pil] at SC
    first+k_tc*i. Mirrors the C solve."""
    n_pil = pilots.size
    out = np.zeros(n_sc, dtype=np.complex128)
    span = n_pil * k_tc

    def R(dk):
        a = abs(int(dk))
        v = R_sc[a] if a < R_sc.size else 0.0
        return v if dk >= 0 else np.conj(v)

    for offset in range(span):
        target_sc = (first + offset) % n_sc
        center = int((offset + k_tc // 2) // k_tc)
        center = min(max(center, 0), n_pil - 1)
        start = min(max(center - win // 2, 0), n_pil - win)
        A = np.empty((win, win), dtype=np.complex128)
        b = np.empty(win, dtype=np.complex128)
        for r in range(win):
            pr = (start + r) * k_tc
            for c in range(win):
                pc = (start + c) * k_tc
                A[r, c] = R(pr - pc)
            A[r, r] += sigma2
            b[r] = R(offset - pr)            # R_hy at SC lag (target - pilot_row)
        try:
            w = np.linalg.solve(A, np.conj(b))          # Wiener-Hopf: A w = conj(b)
            out[target_sc] = np.sum(np.conj(w) * pilots[start:start + win])
        except np.linalg.LinAlgError:
            out[target_sc] = pilots[center]
    return out


def c_mirror_R(pilots, nfft, k_tc, max_dk, alpha_pdp_state):
    """C recipe R_table: contiguous pilots -> NFFT IFFT -> EMA PDP -> robust floor
    -> R(dk)=sum_m PDP_clean[m] exp(-j2pi dk m/NFFT), dk in SC units (as C does)."""
    buf = np.zeros(nfft, dtype=np.complex128)
    buf[:pilots.size] = pilots
    h_time = np.fft.ifft(buf)
    inst = np.abs(h_time) ** 2
    if alpha_pdp_state[0] is None:
        alpha_pdp_state[0] = inst.copy()
    else:
        alpha_pdp_state[0] = 0.95 * alpha_pdp_state[0] + 0.05 * inst
    pdp = alpha_pdp_state[0]
    floor = robust_noise_floor(pdp, k=3.0)
    clean = np.maximum(pdp - floor, 0.0)
    m = np.arange(nfft)
    R = np.array([np.sum(clean * np.exp(-2j * np.pi * dk * m / nfft)) for dk in range(max_dk + 1)],
                 dtype=np.complex128)
    return R


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cdl-dir", default="data_out")
    ap.add_argument("--n-sc", type=int, default=512)
    ap.add_argument("--k-tc", type=int, default=2)
    ap.add_argument("--win", type=int, default=16)
    ap.add_argument("--frames", type=int, default=30)
    ap.add_argument("--snr-db", type=float, default=20.0)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    n_sc, k_tc, win = args.n_sc, args.k_tc, args.win
    n_pil = n_sc // k_tc
    first = 0
    band_bw = 1248.0 * 30e3
    max_dk = win * k_tc

    def nmse(est, ref, mask=None):
        if mask is None:
            mask = np.ones(ref.shape[-1], bool)
        e = est[..., mask] - ref[..., mask]
        return 10 * np.log10(np.mean(np.abs(e) ** 2) / (np.mean(np.abs(ref[..., mask]) ** 2) + 1e-30) + 1e-30)

    print(f"=== C freq-mirror #4 check (n_sc={n_sc}, K_TC={k_tc}, n_pil={n_pil}, win={win}, "
          f"snr={args.snr_db}) ===")
    print("model      raw_pil  genie_comb  cmirror_npil  cmirror_2npil  cmirror_2048")
    print("--------------------------------------------------------------------------")
    for name in ["cdl_a", "cdl_c", "cdl_e"]:
        mdir = Path(args.cdl_dir) / name
        if not (mdir / "tau_rays_for_ChannelBlock.npy").exists():
            continue
        H, d_bins, pw, _ = synth_cdl_channel(mdir, n_sc, args.frames, 0.0, rng, "randomwalk", band_bw)
        sigma2 = float(np.mean(np.abs(H) ** 2)) / 10 ** (args.snr_db / 10)
        pilot_sc = (first + k_tc * np.arange(n_pil)) % n_sc
        R_genie = correlation_from_pdp(n_sc, d_bins, pw)   # SC-lag autocorr (genie)

        accs = {key: [] for key in ["raw", "genie", "npil", "2npil", "2048"]}
        st_npil, st_2npil, st_2048 = [None], [None], [None]
        for f in range(args.frames):
            noise = np.sqrt(sigma2 / 2) * (rng.standard_normal(n_sc) + 1j * rng.standard_normal(n_sc))
            Y = H[f] + noise
            pilots = Y[pilot_sc]
            # raw pilots scattered to dense (nearest) — reference floor
            accs["raw"].append(nmse(Y, H[f]))   # full-obs raw (upper ref only)
            # genie combed Toeplitz (correct R, correct conjugate)
            accs["genie"].append(nmse(windowed_toeplitz_comb(pilots, R_genie, sigma2, n_sc, first, k_tc, win), H[f]))
            for key, nfft, st in [("npil", n_pil, st_npil), ("2npil", n_pil * k_tc, st_2npil), ("2048", 2048, st_2048)]:
                R = c_mirror_R(pilots, nfft, k_tc, max_dk, st)
                R = R / (R[0].real + 1e-30) * (R_genie[0].real)  # rescale R(0) to genie scale for fair sigma2
                accs[key].append(nmse(windowed_toeplitz_comb(pilots, R, sigma2, n_sc, first, k_tc, win), H[f]))
        print(f"{name:9s}  {np.mean(accs['raw']):6.2f}   {np.mean(accs['genie']):8.2f}    "
              f"{np.mean(accs['npil']):9.2f}     {np.mean(accs['2npil']):9.2f}     {np.mean(accs['2048']):9.2f}")
    print("\ngenie_comb = correct combed-pilot Toeplitz (R genie). cmirror_* = C recipe at NFFT.")
    print("If cmirror_2048 ~ genie_comb -> C 2048/comb calibration OK on freq-selective.")
    print("If only cmirror_2npil (NFFT=n_pil*K_TC) ~ genie -> C should use NFFT=n_pil*K_TC, not 2048.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
