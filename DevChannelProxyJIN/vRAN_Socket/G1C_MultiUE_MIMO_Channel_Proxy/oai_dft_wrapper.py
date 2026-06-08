#!/usr/bin/env python3
"""
oai_dft_wrapper.py — bit-exact OAI int16 DFT from Python via libdfts.so.

Provides:
  OAIDftWrapper : ctypes bridge to dft2048 / dfts_autoinit
  srs_ls_int16  : LS channel estimation (int16 arithmetic)
  srs_filt8_int16 : filt8 comb-2 interpolation (int16 arithmetic)
  int16_matched_pipeline : full H(f) → int16 SRS estimate

Usage:
    from oai_dft_wrapper import OAIDftWrapper, int16_matched_pipeline
    dft = OAIDftWrapper()
    H_est = int16_matched_pipeline(H_freq, X_ref, dft)
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Optional

import numpy as np

_HERE = Path(__file__).resolve().parent
_OAI_BUILD = Path(os.environ.get(
    "OAI_LIBDFTS_PATH",
    str(_HERE.parent.parent / "openairinterface5g_whan"
        / "cmake_targets" / "ran_build" / "build" / "libdfts.so")))
_STUB_PATH = _HERE / "liboai_stub.so"

# OAI SRS parameters (hardcoded in nr_radio_config.c for our setup)
FFT_SIZE = 2048
K_TC = 2
M_SRS_PRB = 104
M_SC_B_SRS = M_SRS_PRB * 12 // K_TC  # 624
SRS_GEN_BITS = 9  # log2(AMP) where AMP=512


# ─── filt8 coefficients from filt16a_32.h ─────────────────────────────

FILT8_OPT = {
    "start":            np.array([16384, 8192,     0,     0,     0,     0, 0, 0], dtype=np.int16),
    "start_shift2":     np.array([    0,     0, 16384, 8192,     0,     0, 0, 0], dtype=np.int16),
    "middle2":          np.array([    0, 8192, 16384, 8192,     0,     0, 0, 0], dtype=np.int16),
    "middle4":          np.array([    0,     0,     0, 8192, 16384, 8192, 0, 0], dtype=np.int16),
    "end_odd":          np.array([    0, 8192, 16384, 16384,     0,     0, 0, 0], dtype=np.int16),
    "end_even":         np.array([    0,     0,     0, 8192, 16384, 16384, 0, 0], dtype=np.int16),
    "end_odd_shift2":   np.array([0, 0,     0, 8192, 16384, 16384,     0,     0], dtype=np.int16),
    "end_even_shift2":  np.array([0, 0,     0,    0,     0,  8192, 16384, 16384], dtype=np.int16),
}

FILT8_LEGACY = {
    "start":         np.array([12288, 8192, 4096, 0, 0, 0, 0, 0], dtype=np.int16),
    "start_shift2":  np.array([    0,    0, 12288, 8192, 4096, 0, 0, 0], dtype=np.int16),
    "middle2":       np.array([ 4096, 8192, 8192, 8192, 4096, 0, 0, 0], dtype=np.int16),
    "middle4":       np.array([    0,    0, 4096, 8192, 8192, 8192, 4096, 0], dtype=np.int16),
    "end":           np.array([ 4096, 8192, 12288, 16384, 0, 0, 0, 0], dtype=np.int16),
    "end_shift2":    np.array([    0,    0,  4096,  8192, 12288, 16384, 0, 0], dtype=np.int16),
}


# ═════════════════════════════════════════════════════════════════════════
#  OAI DFT wrapper (ctypes)
# ═════════════════════════════════════════════════════════════════════════

class OAIDftWrapper:
    """Thin ctypes wrapper around OAI's libdfts.so."""

    def __init__(self, libdfts_path: Optional[str] = None,
                 stub_path: Optional[str] = None):
        stub = str(stub_path or _STUB_PATH)
        dfts = str(libdfts_path or _OAI_BUILD)
        if not os.path.isfile(stub):
            raise FileNotFoundError(f"stub not found: {stub}")
        if not os.path.isfile(dfts):
            raise FileNotFoundError(f"libdfts.so not found: {dfts}")

        self._stub = ctypes.CDLL(stub, mode=ctypes.RTLD_GLOBAL)
        self._lib = ctypes.CDLL(dfts)
        self._lib.dfts_autoinit()

        self._lib.dft2048.argtypes = [
            ctypes.POINTER(ctypes.c_int16),
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_ubyte,
        ]
        self._lib.dft2048.restype = None

    def dft2048(self, x_iq: np.ndarray, scale: int = 1) -> np.ndarray:
        """Run OAI's int16 DFT-2048 (bit-exact).

        Parameters
        ----------
        x_iq : (4096,) int16 — interleaved [Re0, Im0, Re1, Im1, ...]
        scale : 0 or 1 (OAI convention; 1 = divide by √2 at final stage)

        Returns
        -------
        y_iq : (4096,) int16 — same layout
        """
        assert x_iq.dtype == np.int16 and x_iq.shape == (FFT_SIZE * 2,)
        n = FFT_SIZE * 2
        x_al = _get_aligned_view(_x_pool, n)
        y_al = _get_aligned_view(_y_pool, n)
        np.copyto(x_al, x_iq)
        y_al[:] = 0
        self._lib.dft2048(
            x_al.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            y_al.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            ctypes.c_ubyte(scale),
        )
        return y_al.copy()

    def dft2048_complex(self, x_cpx: np.ndarray, scale: int = 1) -> np.ndarray:
        """Convenience: complex int16 input → complex output.

        Parameters
        ----------
        x_cpx : (2048,) with real/imag in int16 range

        Returns
        -------
        y_cpx : (2048,) complex128 (values are integer-valued)
        """
        iq = np.zeros(FFT_SIZE * 2, dtype=np.int16)
        iq[0::2] = np.clip(np.round(x_cpx.real), -32768, 32767).astype(np.int16)
        iq[1::2] = np.clip(np.round(x_cpx.imag), -32768, 32767).astype(np.int16)
        out = self.dft2048(iq, scale)
        return out[0::2].astype(np.float64) + 1j * out[1::2].astype(np.float64)


_ALIGN = 32  # AVX2

# Pre-allocate reusable aligned buffers to avoid per-call allocation issues.
# dft2048 works on 2048 complex int16 = 4096 int16 = 8192 bytes.
_BUF_ELEMS = FFT_SIZE * 2  # 4096 int16
_x_pool = np.zeros(_BUF_ELEMS + _ALIGN // 2, dtype=np.int16)
_y_pool = np.zeros(_BUF_ELEMS + _ALIGN // 2, dtype=np.int16)


def _get_aligned_view(pool: np.ndarray, n: int) -> np.ndarray:
    """Return an aligned slice from the pre-allocated pool."""
    addr = pool.ctypes.data
    off_bytes = (_ALIGN - addr % _ALIGN) % _ALIGN
    off_elem = off_bytes // pool.itemsize
    return pool[off_elem:off_elem + n]


# ═════════════════════════════════════════════════════════════════════════
#  SRS reference sequence loader
# ═════════════════════════════════════════════════════════════════════════

def load_srs_ref(path: str) -> dict:
    """Load SRS reference signal dumped by OAI (SRS_REF_DUMP_PATH).

    Returns dict with keys:
      ofdm_size, n_ap, m_sc_b_srs, k_tc,
      ref_r  : (n_ap, ofdm_size) int16  — real part
      ref_i  : (n_ap, ofdm_size) int16  — imag part
      pilot_sc : (m_sc_b_srs,) int      — pilot subcarrier indices (derived)
    """
    with open(path, "rb") as f:
        hdr = np.fromfile(f, dtype=np.uint32, count=4)
        ofdm_size, n_ap, m_sc_b_srs, k_tc = int(hdr[0]), int(hdr[1]), int(hdr[2]), int(hdr[3])
        data = np.fromfile(f, dtype=np.int16)

    data = data.reshape(n_ap, ofdm_size, 2)
    ref_r = data[:, :, 0].copy()
    ref_i = data[:, :, 1].copy()

    # Derive pilot SC positions: find non-zero elements in port 0
    nz = np.where(np.abs(ref_r[0].astype(np.int32)) + np.abs(ref_i[0].astype(np.int32)) > 0)[0]
    pilot_sc = nz

    return dict(
        ofdm_size=ofdm_size, n_ap=n_ap, m_sc_b_srs=m_sc_b_srs, k_tc=k_tc,
        ref_r=ref_r, ref_i=ref_i, pilot_sc=pilot_sc,
    )


# ═════════════════════════════════════════════════════════════════════════
#  LS estimation (int16 arithmetic, matches nr_ul_channel_estimation.c)
# ═════════════════════════════════════════════════════════════════════════

def srs_ls_int16(gen_r: np.ndarray, gen_i: np.ndarray,
                 rx_r: np.ndarray, rx_i: np.ndarray,
                 pilot_sc: np.ndarray,
                 srs_gen_bits: int = SRS_GEN_BITS,
                 K_TC: int = K_TC) -> tuple[np.ndarray, np.ndarray]:
    """Bit-exact SRS LS estimation at pilot positions.

    Parameters
    ----------
    gen_r, gen_i : (ofdm_size,) int16 — X_ref real/imag
    rx_r, rx_i   : (ofdm_size,) int16 — dft2048 output real/imag
    pilot_sc     : (M_SC_B_SRS,) int — pilot subcarrier indices

    Returns
    -------
    ls_r, ls_i : (n_pilots,) int16 — LS estimates at each pilot group
    """
    n_pilots = len(pilot_sc) // K_TC
    ls_r_out = np.zeros(n_pilots, dtype=np.int16)
    ls_i_out = np.zeros(n_pilots, dtype=np.int16)

    for k in range(n_pilots):
        acc_r = np.int32(0)
        acc_i = np.int32(0)
        for cdm in range(K_TC):
            sc = pilot_sc[k * K_TC + cdm]
            gr = np.int32(gen_r[sc])
            gi = np.int32(gen_i[sc])
            rr = np.int32(rx_r[sc])
            ri = np.int32(rx_i[sc])
            acc_r += np.int16((gr * rr + gi * ri) >> srs_gen_bits)
            acc_i += np.int16((gr * ri - gi * rr) >> srs_gen_bits)

        ls_r_out[k] = np.int16(acc_r)
        ls_i_out[k] = np.int16(acc_i)

    return ls_r_out, ls_i_out


# ═════════════════════════════════════════════════════════════════════════
#  filt8 interpolation (int16, matches c16multaddVectRealComplex)
# ═════════════════════════════════════════════════════════════════════════

def _c16multadd(filt: np.ndarray, alpha_r: int, alpha_i: int,
                y_r: np.ndarray, y_i: np.ndarray, offset: int):
    """Bit-exact c16multaddVectRealComplex for 8 taps.

    y[offset:offset+8] += filt * alpha  (Q14, saturating)
    """
    ar = np.int32(alpha_r)
    ai = np.int32(alpha_i)
    for i in range(8):
        f = np.int32(filt[i])
        if f == 0:
            continue
        pr = np.int16((ar * f + 0x4000) >> 15)
        pi = np.int16((ai * f + 0x4000) >> 15)
        pr2 = np.int16(np.clip(np.int32(pr) * 2, -32768, 32767))
        pi2 = np.int16(np.clip(np.int32(pi) * 2, -32768, 32767))
        idx = offset + i
        if 0 <= idx < len(y_r):
            y_r[idx] = np.int16(np.clip(np.int32(y_r[idx]) + np.int32(pr2),
                                        -32768, 32767))
            y_i[idx] = np.int16(np.clip(np.int32(y_i[idx]) + np.int32(pi2),
                                        -32768, 32767))


def srs_filt8_int16(ls_r: np.ndarray, ls_i: np.ndarray,
                    pilot_sc: np.ndarray,
                    ofdm_size: int = FFT_SIZE,
                    use_opt: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """Bit-exact filt8 comb-2 interpolation.

    Matches nr_ul_channel_estimation.c SRS comb_size==0 path.

    Returns
    -------
    ch_r, ch_i : (ofdm_size,) int16 — interpolated channel estimate
    """
    filt = FILT8_OPT if use_opt else FILT8_LEGACY
    ch_r = np.zeros(ofdm_size, dtype=np.int16)
    ch_i = np.zeros(ofdm_size, dtype=np.int16)
    n_pilots = len(ls_r)

    # mem_offset: whether first pilot is at even or odd position
    first_sc = pilot_sc[0] if len(pilot_sc) > 0 else 0
    mem_offset = first_sc % (K_TC * 2)  # 0 or non-zero

    for k in range(n_pilots):
        ar = int(ls_r[k])
        ai = int(ls_i[k])
        sc = pilot_sc[k * K_TC] if k * K_TC < len(pilot_sc) else first_sc + k * K_TC

        if use_opt:
            if k == 0:
                _c16multadd(filt["start"], ar, ai, ch_r, ch_i, sc)
            elif sc < K_TC:
                f = filt["start"] if mem_offset == 0 else filt["start_shift2"]
                _c16multadd(f, ar, ai, ch_r, ch_i, sc)
            elif k == n_pilots - 1 or sc + K_TC >= ofdm_size:
                if k % 2 == 1:
                    f = (filt["end_odd"] if (mem_offset == 0 or k == n_pilots - 1)
                         else filt["end_odd_shift2"])
                    _c16multadd(f, ar, ai, ch_r, ch_i, sc - 1)
                else:
                    f = (filt["end_even"] if (mem_offset == 0 or k == n_pilots - 1)
                         else filt["end_even_shift2"])
                    _c16multadd(f, ar, ai, ch_r, ch_i, sc - 3)
            elif k % 2 == 1:
                _c16multadd(filt["middle2"], ar, ai, ch_r, ch_i, sc - 1)
            else:
                _c16multadd(filt["middle4"], ar, ai, ch_r, ch_i, sc - 3)
        else:
            if k == 0:
                _c16multadd(filt["start"], ar, ai, ch_r, ch_i, sc)
            elif sc < K_TC:
                f = filt["start"] if mem_offset == 0 else filt["start_shift2"]
                _c16multadd(f, ar, ai, ch_r, ch_i, sc)
            elif k == n_pilots - 1 or sc + K_TC >= ofdm_size:
                f = filt["end"] if mem_offset == 0 else filt["end_shift2"]
                _c16multadd(f, ar, ai, ch_r, ch_i, sc - 1)
            elif k % 2 == 1:
                _c16multadd(filt["middle2"], ar, ai, ch_r, ch_i, sc - 1)
            else:
                _c16multadd(filt["middle4"], ar, ai, ch_r, ch_i, sc - 3)

    return ch_r, ch_i


# ═════════════════════════════════════════════════════════════════════════
#  Full pipeline: H(f) → int16-matched SRS estimate
# ═════════════════════════════════════════════════════════════════════════

def int16_matched_pipeline(
        H_freq: np.ndarray,
        X_ref_r: np.ndarray,
        X_ref_i: np.ndarray,
        pilot_sc: np.ndarray,
        dft: OAIDftWrapper,
        use_opt_filt: bool = True,
) -> np.ndarray:
    """Generate an int16-matched GT from Sionna H(f) for one (rx, tx) pair.

    Parameters
    ----------
    H_freq    : (FFT_SIZE,) complex128 — Sionna channel frequency response
    X_ref_r/i : (FFT_SIZE,) int16 — SRS reference signal
    pilot_sc  : (M_SC_B_SRS,) int — pilot subcarrier indices
    dft       : OAIDftWrapper instance

    Returns
    -------
    H_est : (FFT_SIZE,) complex64 — int16-matched channel estimate
    """
    # Step 1: channel application in frequency domain
    X_ref = X_ref_r.astype(np.float64) + 1j * X_ref_i.astype(np.float64)
    Y_freq = H_freq * X_ref

    # Step 2: IFFT (float)
    # Factor √N accounts for the UE→Proxy roundtrip normalization:
    #   UE:    idft2048(X_ref) ≈ IFFT(X)*√N    (OAI convention)
    #   Proxy: FFT(x_ue)      ≈ X_ref*√N       (CuPy, unnormalized)
    #   Proxy: IFFT(H * X*√N) = IFFT(H*X)*√N
    # Our shortcut Y=H*X uses numpy IFFT (÷N), so multiply by √N to match.
    y_time = np.fft.ifft(Y_freq) * np.sqrt(FFT_SIZE)

    # Step 3: int16 quantization (same as Proxy _gpu_compute_core)
    y_iq = np.zeros(FFT_SIZE * 2, dtype=np.int16)
    y_iq[0::2] = np.clip(np.round(y_time.real), -32768, 32767).astype(np.int16)
    y_iq[1::2] = np.clip(np.round(y_time.imag), -32768, 32767).astype(np.int16)

    # Step 4: OAI int16 FFT (bit-exact via libdfts.so)
    Y_rx_iq = dft.dft2048(y_iq, scale=1)
    rx_r = Y_rx_iq[0::2].astype(np.int16)
    rx_i = Y_rx_iq[1::2].astype(np.int16)

    # Step 5: LS estimation
    ls_r, ls_i = srs_ls_int16(
        X_ref_r.astype(np.int16), X_ref_i.astype(np.int16),
        rx_r, rx_i, pilot_sc)

    # Step 6: filt8 interpolation
    ch_r, ch_i = srs_filt8_int16(ls_r, ls_i, pilot_sc,
                                  ofdm_size=FFT_SIZE, use_opt=use_opt_filt)

    return (ch_r.astype(np.float32) + 1j * ch_i.astype(np.float32)).astype(np.complex64)


# ═════════════════════════════════════════════════════════════════════════
#  Self-test
# ═════════════════════════════════════════════════════════════════════════

def _selftest():
    """Quick sanity check: DC input → DC output."""
    w = OAIDftWrapper()
    x = np.zeros(FFT_SIZE * 2, dtype=np.int16)
    x[0::2] = 100
    y = w.dft2048(x)
    dc_r = int(y[0])
    print(f"[selftest] DC input=100 → DC output={dc_r}  "
          f"(float ref={100*FFT_SIZE/np.sqrt(FFT_SIZE):.0f})")

    x2 = np.zeros(FFT_SIZE * 2, dtype=np.int16)
    for i in range(FFT_SIZE):
        phase = 2 * np.pi * 7 * i / FFT_SIZE
        x2[2*i] = np.int16(np.clip(round(500 * np.cos(phase)), -32768, 32767))
        x2[2*i+1] = np.int16(np.clip(round(500 * np.sin(phase)), -32768, 32767))
    y2 = w.dft2048(x2)
    mag = np.sqrt(y2[0::2].astype(float)**2 + y2[1::2].astype(float)**2)
    peak_bin = int(np.argmax(mag))
    print(f"[selftest] Tone at bin 7 → peak at bin {peak_bin}, "
          f"mag={mag[peak_bin]:.0f}, max_sidelobe={np.sort(mag)[-2]:.0f}")

    ok = dc_r > 4000 and peak_bin == 7
    print(f"[selftest] {'PASS' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    _selftest()
