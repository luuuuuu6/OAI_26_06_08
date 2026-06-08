"""
2D MMSE Channel Estimator.

Filter classes:
  SRS2DFilter             — C-style explicit loops (direct C translation reference)
  SRS2DFilterVec          — numpy vectorized (fast for Python experiments)
  AdaptiveSRS2DFilter     — Phase 1: innovation/baseline ratio adaptive α
  DualEMASRS2DFilter      — Phase 2: 1D rot_rate mapping (reference only)
  AdaptiveAlphaEMA        — Phase 3: 2D (rot_rate + even-odd SNR) linear mapping
  IBVSS_EMA               — Phase 4: Innovation-Based Variable Step Size (no calibration)
  ScalarKalmanEMA         — Phase 5: Per-SC Scalar Kalman + IAE (full Riccati recursion)
  StructuredChannelOutput — Post-processing: structure extraction + DFT compression
"""
import numpy as np

ONE_THIRD = 1.0 / 3.0


class SRS2DFilter:
    """C-style 2D filter. One instance per (rx, tx) pair."""

    def __init__(self, n_sc: int, alpha: float = 0.1):
        self.n_sc = n_sc
        self.alpha = alpha
        self.H_smooth = np.zeros(n_sc, dtype=np.complex128)
        self.initialized = False

    def reset(self):
        self.H_smooth[:] = 0
        self.initialized = False

    def update(self, H_obs: np.ndarray) -> np.ndarray:
        """Full 2D: freq boxcar → time EMA."""
        return self._time_ema(self._freq_boxcar(H_obs))

    def update_ema_only(self, H_obs: np.ndarray) -> np.ndarray:
        """Time EMA only (no freq filtering)."""
        return self._time_ema(H_obs)

    def freq_boxcar(self, H_obs: np.ndarray) -> np.ndarray:
        """Freq boxcar only (stateless, no time EMA)."""
        return self._freq_boxcar(H_obs)

    # ── internals (C-translatable loops) ──

    def _freq_boxcar(self, H_obs: np.ndarray) -> np.ndarray:
        n = self.n_sc
        H_filt = np.empty(n, dtype=H_obs.dtype)
        H_filt[0] = (H_obs[0] + H_obs[1]) * 0.5
        for k in range(1, n - 1):
            H_filt[k] = (H_obs[k - 1] + H_obs[k] + H_obs[k + 1]) * ONE_THIRD
        H_filt[n - 1] = (H_obs[n - 2] + H_obs[n - 1]) * 0.5
        return H_filt

    def _time_ema(self, H_filt: np.ndarray) -> np.ndarray:
        a = self.alpha
        if not self.initialized:
            self.H_smooth[:] = H_filt
            self.initialized = True
        else:
            one_minus_a = 1.0 - a
            for k in range(self.n_sc):
                self.H_smooth[k] = one_minus_a * self.H_smooth[k] + a * H_filt[k]
        return self.H_smooth.copy()


class SRS2DFilterVec:
    """Vectorized version — identical results, ~100x faster in Python."""

    def __init__(self, n_sc: int, alpha: float = 0.1):
        self.n_sc = n_sc
        self.alpha = alpha
        self.H_smooth = np.zeros(n_sc, dtype=np.complex128)
        self.initialized = False

    def reset(self):
        self.H_smooth[:] = 0
        self.initialized = False

    def update(self, H_obs: np.ndarray) -> np.ndarray:
        return self._time_ema(self._freq_boxcar(H_obs))

    def update_ema_only(self, H_obs: np.ndarray) -> np.ndarray:
        return self._time_ema(H_obs)

    def freq_boxcar(self, H_obs: np.ndarray) -> np.ndarray:
        return self._freq_boxcar(H_obs)

    def _freq_boxcar(self, H_obs: np.ndarray) -> np.ndarray:
        H_filt = np.empty_like(H_obs)
        H_filt[0] = (H_obs[0] + H_obs[1]) * 0.5
        H_filt[1:-1] = (H_obs[:-2] + H_obs[1:-1] + H_obs[2:]) * ONE_THIRD
        H_filt[-1] = (H_obs[-2] + H_obs[-1]) * 0.5
        return H_filt

    def _time_ema(self, H_filt: np.ndarray) -> np.ndarray:
        if not self.initialized:
            self.H_smooth[:] = H_filt
            self.initialized = True
        else:
            self.H_smooth[:] = (1.0 - self.alpha) * self.H_smooth + self.alpha * H_filt
        return self.H_smooth.copy()


class AdaptiveSRS2DFilter:
    """De-rotation + innovation-based adaptive alpha + EMA.

    Pipeline per frame:
      1. LS estimate global phase rotation vs H_smooth
      2. Full-SC innovation for raw vs de-rotated candidates
      3. Gate: only de-rotate if >= derot_gain_db improvement
      4. Adapt alpha via direct mapping of innovation / baseline ratio
      5. EMA update using diff already computed for innovation

    Budget: ~4-6 ops/SC (dynamic with derot: 6, static bypass: 4-5).
    """

    def __init__(
        self,
        n_sc: int,
        alpha_init: float = 0.05,
        alpha_min: float = 0.01,
        alpha_max: float = 0.5,
        innov_thresh_hi: float = 3.0,
        innov_ema: float = 0.1,
        baseline_frames: int = 10,
        derot_gain_db: float = 5.0,
    ):
        self.n_sc = n_sc
        self.alpha = alpha_init
        self.alpha_min = alpha_min
        self.alpha_max = alpha_max
        self.innov_thresh_hi = innov_thresh_hi
        self.innov_ema_coeff = innov_ema
        self.derot_gain = 10.0 ** (-derot_gain_db / 10.0)

        self.H_smooth = np.zeros(n_sc, dtype=np.complex128)
        self.initialized = False
        self.innov_smooth = 0.0
        self.innov_baseline = 0.0
        self._baseline_accum = []
        self._baseline_frames = baseline_frames
        self._baseline_locked = False

        self.alpha_history = []
        self.innov_history = []
        self.derot_history = []
        self.rot_angle_history = []

    def reset(self):
        self.H_smooth[:] = 0
        self.initialized = False
        self.innov_smooth = 0.0
        self.innov_baseline = 0.0
        self._baseline_accum = []
        self._baseline_locked = False
        self.alpha_history = []
        self.innov_history = []
        self.derot_history = []
        self.rot_angle_history = []

    def update(self, H_obs: np.ndarray) -> np.ndarray:
        if not self.initialized:
            self.H_smooth[:] = H_obs
            self.initialized = True
            self.alpha_history.append(self.alpha)
            self.innov_history.append(0.0)
            self.derot_history.append(False)
            self.rot_angle_history.append(0.0)
            return self.H_smooth.copy()

        # 1. LS phase rotation
        inner = np.sum(H_obs * np.conj(self.H_smooth))
        inner_abs = abs(inner)
        rot = (inner / inner_abs) if inner_abs > 1e-30 else (1.0 + 0j)
        rot_angle = float(np.angle(rot))

        # 2. Full-SC innovation: raw vs de-rotated
        H_derot = H_obs * np.conj(rot)
        diff_raw = H_obs - self.H_smooth
        diff_derot = H_derot - self.H_smooth
        innov_raw = float(np.mean(np.abs(diff_raw) ** 2))
        innov_derot = float(np.mean(np.abs(diff_derot) ** 2))

        # 3. Gate: de-rotate only if >= derot_gain_db improvement
        if innov_derot < innov_raw * self.derot_gain:
            diff = diff_derot
            innov = innov_derot
            used_derot = True
        else:
            diff = diff_raw
            innov = innov_raw
            used_derot = False

        # 4. Baseline tracking (bidirectional slow EMA, capped upward)
        bl_coeff = 0.02
        if not self._baseline_locked:
            self._baseline_accum.append(innov)
            if len(self._baseline_accum) >= self._baseline_frames:
                self.innov_baseline = float(np.median(self._baseline_accum))
                self._baseline_locked = True
            else:
                self.innov_baseline = innov
        else:
            target = min(innov, self.innov_baseline * 2.0)
            self.innov_baseline = (1 - bl_coeff) * self.innov_baseline + bl_coeff * target

        self.innov_smooth = ((1 - self.innov_ema_coeff) * self.innov_smooth
                             + self.innov_ema_coeff * innov)

        # 5. Adapt alpha
        if self.innov_baseline > 1e-30 and self._baseline_locked:
            r = self.innov_smooth / self.innov_baseline
            frac = max(0.0, min(1.0, (r - 1.0) / (self.innov_thresh_hi - 1.0)))
            self.alpha = self.alpha_min + (self.alpha_max - self.alpha_min) * frac

        # 6. EMA using already-computed diff
        self.H_smooth[:] += self.alpha * diff

        self.alpha_history.append(self.alpha)
        self.innov_history.append(innov)
        self.derot_history.append(used_derot)
        self.rot_angle_history.append(rot_angle)
        return self.H_smooth.copy()


class DualEMASRS2DFilter:
    """Phase 2 version (1D rot_rate mapping, kept for reference)."""

    def __init__(self, n_sc, alpha_ref=0.02, alpha_init=0.5, alpha_min=0.01,
                 alpha_max=0.95, rot_rate_ema=0.05, rot_rate_a=0.1437,
                 rot_rate_b=0.4493):
        self.n_sc = n_sc
        self.alpha_ref = alpha_ref
        self.alpha = alpha_init
        self.alpha_min, self.alpha_max = alpha_min, alpha_max
        self.rot_rate_ema = rot_rate_ema
        self.rot_rate_a, self.rot_rate_b = rot_rate_a, rot_rate_b
        self.H_ref = np.zeros(n_sc, dtype=np.complex128)
        self.H_main = np.zeros(n_sc, dtype=np.complex128)
        self.initialized = False
        self._prev_rot_ref_angle = 0.0
        self._rot_rate_smooth = 0.0
        self.alpha_history, self.rot_main_history, self.rot_ref_angle_history = [], [], []

    def reset(self):
        self.H_ref[:] = 0; self.H_main[:] = 0; self.initialized = False
        self._prev_rot_ref_angle = 0.0; self._rot_rate_smooth = 0.0
        self.alpha_history, self.rot_main_history, self.rot_ref_angle_history = [], [], []

    def update(self, H_obs):
        if not self.initialized:
            self.H_ref[:] = H_obs; self.H_main[:] = H_obs; self.initialized = True
            self.alpha_history.append(self.alpha)
            self.rot_main_history.append(1.0 + 0j)
            self.rot_ref_angle_history.append(0.0)
            return self.H_main.copy()
        inner_m = np.sum(H_obs * np.conj(self.H_main))
        rot_m = (inner_m / abs(inner_m)) if abs(inner_m) > 1e-30 else (1.0 + 0j)
        inner_r = np.sum(H_obs * np.conj(self.H_ref))
        rot_r = (inner_r / abs(inner_r)) if abs(inner_r) > 1e-30 else (1.0 + 0j)
        ra = float(np.angle(rot_r))
        self.H_ref[:] += self.alpha_ref * (H_obs * np.conj(rot_r) - self.H_ref)
        rr = abs(ra - self._prev_rot_ref_angle)
        if rr > np.pi: rr = 2 * np.pi - rr
        self._prev_rot_ref_angle = ra
        self._rot_rate_smooth = (1 - self.rot_rate_ema) * self._rot_rate_smooth + self.rot_rate_ema * rr
        self.alpha = max(self.alpha_min, min(self.alpha_max,
                         self.rot_rate_a * self._rot_rate_smooth + self.rot_rate_b))
        self.H_main[:] += self.alpha * (H_obs * np.conj(rot_m) - self.H_main)
        self.alpha_history.append(self.alpha)
        self.rot_main_history.append(rot_m)
        self.rot_ref_angle_history.append(ra)
        return self.H_main.copy()


class AdaptiveAlphaEMA:
    """Phase 3: 2D adaptive alpha (rot_rate + even-odd SNR).

    Single EMA with self-referenced de-rotation + 2D α mapping.
    No Reference EMA — rot_rate from Main EMA rot, SNR from open-loop
    frequency-domain even-odd pair estimation.

    Per-frame pipeline:
      Pass 1 (fused): inner product for de-rotation + even-odd pair for SNR
      Pass 2: de-rotate + EMA update
      Scalar: rot_rate smooth + SNR smooth + α = a1·rr + a2·snr + a3

    C budget: ~6 ops/SC + O(1) scalar, ~10KB memory.
    """

    def __init__(
        self,
        n_sc: int,
        c_model: float = 0.000136,
        alpha_init: float = 0.5,
        alpha_min: float = 0.01,
        alpha_max: float = 0.98,
        rr_ema: float = 0.05,
        snr_ema: float = 0.02,
        a1: float = 0.1132,
        a2: float = 0.0305,
        a3: float = 0.1074,
    ):
        self.n_sc = n_sc
        self.c_model = c_model
        self.alpha = alpha_init
        self.alpha_min = alpha_min
        self.alpha_max = alpha_max
        self.rr_ema = rr_ema
        self.snr_ema = snr_ema
        self.a1 = a1
        self.a2 = a2
        self.a3 = a3

        self.H_main = np.zeros(n_sc, dtype=np.complex128)
        self.initialized = False
        self._prev_rot_angle = 0.0
        self._rr_smooth = 0.0
        self._snr_smooth = 10.0

        self.alpha_history = []
        self.rot_history = []

    def reset(self):
        self.H_main[:] = 0
        self.initialized = False
        self._prev_rot_angle = 0.0
        self._rr_smooth = 0.0
        self._snr_smooth = 10.0
        self.alpha_history = []
        self.rot_history = []

    def update(self, H_obs: np.ndarray) -> np.ndarray:
        if not self.initialized:
            self.H_main[:] = H_obs
            self.initialized = True
            self.alpha_history.append(self.alpha)
            self.rot_history.append(1.0 + 0j)
            return self.H_main.copy()

        # ① De-rotation: rot = sum(H_obs·conj(H_main)) / |sum|
        inner = np.sum(H_obs * np.conj(self.H_main))
        inner_abs = abs(inner)
        rot = (inner / inner_abs) if inner_abs > 1e-30 else (1.0 + 0j)

        # ② Even-odd pair SNR estimation (open loop, from H_obs directly)
        s = H_obs[0::2] + H_obs[1::2]
        d = H_obs[0::2] - H_obs[1::2]
        p_plus = float(np.mean(np.abs(s) ** 2)) / 4
        p_minus = float(np.mean(np.abs(d) ** 2)) / 4
        pm_corr = max(p_minus - self.c_model, 1e-10)
        snr_lin = max((p_plus - p_minus) / pm_corr, 0.1)
        snr_db = 10.0 * np.log10(snr_lin)

        # ③ rot_rate from de-rotation angle
        rot_angle = float(np.angle(rot))
        rr = abs(rot_angle - self._prev_rot_angle)
        if rr > np.pi:
            rr = 2 * np.pi - rr
        self._prev_rot_angle = rot_angle

        # ④ Smooth features + 2D mapping
        self._rr_smooth = (1 - self.rr_ema) * self._rr_smooth + self.rr_ema * rr
        self._snr_smooth = (1 - self.snr_ema) * self._snr_smooth + self.snr_ema * snr_db

        alpha_new = self.a1 * self._rr_smooth + self.a2 * self._snr_smooth + self.a3
        self.alpha = max(self.alpha_min, min(self.alpha_max, alpha_new))

        # ⑤ De-rotate + EMA update
        H_derot = H_obs * np.conj(rot)
        self.H_main[:] += self.alpha * (H_derot - self.H_main)

        self.alpha_history.append(self.alpha)
        self.rot_history.append(rot)
        return self.H_main.copy()


class IBVSS_EMA:
    """Phase 4: Innovation-Based Variable Step Size EMA.

    Replaces Phase 3's pre-calibrated 2D linear mapping with a
    calibration-free direct mapping derived from Kalman steady-state theory.

    Theory (random-walk channel + AWGN observation):
      At steady state, innovation variance = R_est / (1 - α_opt).
      Therefore: α_opt = 1 - R_est / innov_smooth.

    Key signals:
      - innov_smooth = EMA of mean |H_derot - H_smooth|²
      - R_est = even-odd pair noise estimate (open-loop, from H_obs)
      - α = clip(α_min, α_max, 1 - R_est / innov_smooth)

    Advantages over Phase 3 (AdaptiveAlphaEMA):
      - No pre-calibrated coefficients (a1, a2, a3)
      - Theoretically grounded in Kalman optimality
      - Generalizes across SNR and speed without lookup tables
      - Adaptive c_model from H_smooth handles frequency-selective channels
      - Only 3 core hyperparameters: α_min, α_max, innov_ema

    Per-frame pipeline:
      ① inner product → de-rotation (same as Phase 3)
      ② c_model from H_smooth (adaptive, for freq-selective compensation)
      ③ even-odd pair → R_est noise estimate (corrected by c_model)
      ④ innovation_power = mean |H_derot - H_smooth|²
      ⑤ α = clip(1 - 1/ratio), ratio = innov_smooth / R_est
      ⑥ de-rotate + EMA update

    C budget: ~8 ops/SC + O(1) scalar, ~10KB memory.
    """

    def __init__(
        self,
        n_sc: int,
        c_model: float = 0.0,
        alpha_init: float = 0.5,
        alpha_min: float = 0.02,
        alpha_max: float = 0.98,
        innov_ema: float = 0.15,
        warmup_frames: int = 20,
        adaptive_cmodel: bool = True,
        n_bands: int = 8,
    ):
        self.n_sc = n_sc
        self.c_model = c_model
        self.alpha = alpha_init
        self.alpha_init = alpha_init
        self.alpha_min = alpha_min
        self.alpha_max = alpha_max
        self.innov_ema = innov_ema
        self.warmup_frames = warmup_frames
        self.adaptive_cmodel = adaptive_cmodel
        self.n_bands = n_bands if n_sc >= n_bands * 4 else 1

        self.H_main = np.zeros(n_sc, dtype=np.complex128)
        self.initialized = False
        self._innov_smooth = 0.0
        self._frame_count = 0
        self._c_model_est = c_model

        self.alpha_history = []
        self.rot_history = []
        self.ratio_history = []
        self.snr_history = []
        self.innov_history = []

    def reset(self):
        self.H_main[:] = 0
        self.initialized = False
        self._innov_smooth = 0.0
        self._frame_count = 0
        self._c_model_est = self.c_model
        self.alpha = self.alpha_init
        self.alpha_history = []
        self.rot_history = []
        self.ratio_history = []
        self.snr_history = []
        self.innov_history = []

    def update(self, H_obs: np.ndarray) -> np.ndarray:
        if not self.initialized:
            self.H_main[:] = H_obs
            self.initialized = True
            self._frame_count = 1
            if self.adaptive_cmodel:
                d_obs = H_obs[0::2] - H_obs[1::2]
                self._c_model_est = float(np.mean(np.abs(d_obs) ** 2)) / 4
            self.alpha_history.append(self.alpha)
            self.rot_history.append(1.0 + 0j)
            self.ratio_history.append(1.0)
            self.snr_history.append(0.0)
            self.innov_history.append(0.0)
            return self.H_main.copy()

        self._frame_count += 1

        # ① De-rotation
        inner = np.sum(H_obs * np.conj(self.H_main))
        inner_abs = abs(inner)
        rot = (inner / inner_abs) if inner_abs > 1e-30 else (1.0 + 0j)

        # ② Even-odd pair noise estimation (open-loop, from H_obs)
        s = H_obs[0::2] + H_obs[1::2]
        d = H_obs[0::2] - H_obs[1::2]
        p_plus = float(np.mean(np.abs(s) ** 2)) / 4
        p_minus = float(np.mean(np.abs(d) ** 2)) / 4
        eps = max(p_plus * 1e-8, 1e-30)
        pm_corr = max(p_minus - self._c_model_est, eps)
        R_est = pm_corr * 2.0

        snr_lin = max((p_plus - p_minus) / pm_corr, 0.1)
        snr_db = 10.0 * np.log10(snr_lin)

        # ③ Innovation power (per-band median for robustness to freq-shape mismatch)
        H_derot = H_obs * np.conj(rot)
        innovation = H_derot - self.H_main
        if self.n_bands > 1:
            bsz = self.n_sc // self.n_bands
            innov_power = float(np.median([
                np.mean(np.abs(innovation[b*bsz:(b+1)*bsz]) ** 2)
                for b in range(self.n_bands)
            ]))
        else:
            innov_power = float(np.mean(np.abs(innovation) ** 2))

        # ④ Smooth innovation + direct Kalman mapping
        if self._frame_count <= 2:
            self._innov_smooth = innov_power
        else:
            self._innov_smooth = ((1.0 - self.innov_ema) * self._innov_smooth
                                  + self.innov_ema * innov_power)

        ratio = self._innov_smooth / max(R_est, eps)

        if self._frame_count > self.warmup_frames:
            alpha_raw = 1.0 - 1.0 / max(ratio, 1.001)
            self.alpha = max(self.alpha_min, min(self.alpha_max, alpha_raw))

        # ⑤ EMA update
        self.H_main[:] += self.alpha * innovation

        # ⑥ Adaptive c_model: start from frame 3 (not warmup)
        if self.adaptive_cmodel and self._frame_count >= 3:
            d_smooth = self.H_main[0::2] - self.H_main[1::2]
            c_raw = float(np.mean(np.abs(d_smooth) ** 2)) / 4
            a = self.alpha
            noise_bias = a / (2.0 * (2.0 - a)) * R_est
            self._c_model_est = max(0.0, c_raw - noise_bias)

        self.alpha_history.append(self.alpha)
        self.rot_history.append(rot)
        self.ratio_history.append(ratio)
        self.snr_history.append(snr_db)
        self.innov_history.append(innov_power)
        return self.H_main.copy()


class ScalarKalmanEMA:
    """Phase 5: Per-SC Scalar Kalman filter with IAE.

    Full Riccati recursion replaces IBVSS's steady-state shortcut.
    Innovation-based Adaptive Estimation (IAE) tracks process noise Q
    online, yielding theoretically optimal MMSE gain at every frame —
    not just at steady state.

    State-space model (per subcarrier, random-walk channel + AWGN):
        x[n] = x[n-1] + w[n],   w ~ CN(0, Q)   (channel variation)
        y[n] = x[n]   + v[n],   v ~ CN(0, R)   (observation noise)

    Riccati recursion (scalar):
        P_pred = P + Q
        K      = P_pred / (P_pred + R)         — optimal Kalman gain
        P      = (1 - K) · P_pred               — posterior covariance

    IAE for Q (Myers & Tapley 1976, simplified):
        σ²_innov ≈ P_pred + R  →  Q_est = σ²_innov − P − R

    Key advantages over IBVSS (Phase 4):
        - Riccati naturally tracks transients (e.g. after gap/reset)
        - Explicit Q gives physical insight (channel variability measure)
        - Optimal at every frame, not just at steady state

    Warmup strategy (Fix1 — hybrid):
        During warmup, uses IBVSS ratio→alpha mapping (no P/Q dependency).
        Transition to Riccati when convergence detected:
          - Min 10 frames before checking
          - Dual detection: P change-rate < 5% OR K_riccati-K_ibvss diff < 20%
          - Debounce: 3 consecutive matching frames required
          - Max 50 frames safety cap
        On switch: P re-initialized from IBVSS K to ensure seamless handover.

    Shared with IBVSS: de-rotation, even-odd R_est, adaptive c_model.
    """

    def __init__(
        self,
        n_sc: int,
        c_model: float = 0.0,
        P_init: float = 10.0,
        Q_init: float = 0.01,
        alpha_min: float = 0.02,
        alpha_max: float = 0.98,
        q_ema: float = 0.1,
        innov_ema: float = 0.15,
        adaptive_cmodel: bool = True,
        warmup_min: int = 10,
        warmup_max: int = 50,
        warmup_debounce: int = 3,
        n_bands: int = 8,
    ):
        self.n_sc = n_sc
        self.c_model = c_model
        self.P_init = P_init
        self.Q_init = Q_init
        self.alpha_min = alpha_min
        self.alpha_max = alpha_max
        self.q_ema = q_ema
        self.innov_ema = innov_ema
        self.adaptive_cmodel = adaptive_cmodel
        self.warmup_min = warmup_min
        self.warmup_max = warmup_max
        self.warmup_debounce = warmup_debounce
        self.n_bands = n_bands if n_sc >= n_bands * 4 else 1

        self.H_main = np.zeros(n_sc, dtype=np.complex128)
        self.initialized = False
        self.P = P_init
        self.Q = Q_init
        self.K = 0.5
        self._innov_smooth = 0.0
        self._frame_count = 0
        self._c_model_est = c_model

        self._use_riccati = False
        self._consecutive_match = 0
        self._P_prev = P_init
        self._last_abs_slot = None
        self._expected_period = None
        self._gap_history = []
        self._outlier_count = 0

        self.alpha_history = []
        self.rot_history = []
        self.ratio_history = []
        self.snr_history = []
        self.innov_history = []
        self.P_history = []
        self.Q_history = []

    @property
    def alpha(self):
        return self.K

    @alpha.setter
    def alpha(self, v):
        self.K = v

    def reset(self):
        self.H_main[:] = 0
        self.initialized = False
        self.P = self.P_init
        self.Q = self.Q_init
        self.K = 0.5
        self._innov_smooth = 0.0
        self._frame_count = 0
        self._c_model_est = self.c_model
        self._use_riccati = False
        self._consecutive_match = 0
        self._P_prev = self.P_init
        self._last_abs_slot = None
        self._expected_period = None
        self._gap_history = []
        self._outlier_count = 0
        self.alpha_history.clear()
        self.rot_history.clear()
        self.ratio_history.clear()
        self.snr_history.clear()
        self.innov_history.clear()
        self.P_history.clear()
        self.Q_history.clear()

    def update(self, H_obs: np.ndarray, abs_slot: int = None) -> np.ndarray:
        if not self.initialized:
            self.H_main[:] = H_obs
            self.initialized = True
            self._frame_count = 1
            self._last_abs_slot = abs_slot
            if self.adaptive_cmodel:
                d_obs = H_obs[0::2] - H_obs[1::2]
                self._c_model_est = float(np.mean(np.abs(d_obs) ** 2)) / 4
            sig_power = float(np.mean(np.abs(H_obs) ** 2))
            self.P = sig_power
            self.alpha_history.append(self.K)
            self.rot_history.append(1.0 + 0j)
            self.ratio_history.append(1.0)
            self.snr_history.append(0.0)
            self.innov_history.append(0.0)
            self.P_history.append(self.P)
            self.Q_history.append(self.Q)
            return self.H_main.copy()

        self._frame_count += 1

        # Fix2: adaptive time-step from abs_slot
        dt = 1.0
        if abs_slot is not None and self._last_abs_slot is not None:
            gap = abs_slot - self._last_abs_slot
            if gap > 0:
                self._gap_history.append(gap)
                if self._expected_period is None and len(self._gap_history) >= 3:
                    self._expected_period = float(sorted(self._gap_history)[len(self._gap_history) // 2])
                if self._expected_period is not None and self._expected_period > 0:
                    dt = max(0.1, min(float(gap) / self._expected_period, 5.0))
        self._last_abs_slot = abs_slot

        # ① De-rotation
        inner = np.sum(H_obs * np.conj(self.H_main))
        inner_abs = abs(inner)
        rot = (inner / inner_abs) if inner_abs > 1e-30 else (1.0 + 0j)

        # ② Even-odd pair → R_est (observation noise)
        s = H_obs[0::2] + H_obs[1::2]
        d = H_obs[0::2] - H_obs[1::2]
        p_plus = float(np.mean(np.abs(s) ** 2)) / 4
        p_minus = float(np.mean(np.abs(d) ** 2)) / 4
        c_capped = min(self._c_model_est, p_minus * 0.8)
        eps = max(p_plus * 1e-8, 1e-30)
        pm_corr = max(p_minus - c_capped, eps)
        R_meas = pm_corr * 2.0

        snr_lin = max((p_plus - p_minus) / pm_corr, 0.1)
        snr_db = 10.0 * np.log10(snr_lin)

        # ③ Innovation (per-band median for robustness to freq-shape mismatch)
        H_derot = H_obs * np.conj(rot)
        innovation = H_derot - self.H_main
        if self.n_bands > 1:
            bsz = self.n_sc // self.n_bands
            innov_power = float(np.median([
                np.mean(np.abs(innovation[b*bsz:(b+1)*bsz]) ** 2)
                for b in range(self.n_bands)
            ]))
        else:
            innov_power = float(np.mean(np.abs(innovation) ** 2))

        # ④ Smooth innovation variance
        if self._frame_count <= 2:
            self._innov_smooth = innov_power
        else:
            self._innov_smooth = ((1.0 - self.innov_ema) * self._innov_smooth
                                  + self.innov_ema * innov_power)

        # Fix3: outlier frame detection — reject before contaminating state
        outlier_ratio = innov_power / max(self._innov_smooth, 1e-30)
        if outlier_ratio > 10.0 and self._frame_count > 5:
            self.P += self.Q * dt
            self._outlier_count += 1
            self.alpha_history.append(self.K)
            self.rot_history.append(rot)
            self.ratio_history.append(self._innov_smooth / max(R_meas, eps))
            self.snr_history.append(snr_db)
            self.innov_history.append(innov_power)
            self.P_history.append(self.P)
            self.Q_history.append(self.Q)
            return self.H_main.copy()

        # ⑤ IAE: estimate Q from innovation statistics
        # Fix4: adaptive q_ema — accelerate when innovation surges
        innov_change = innov_power / max(self._innov_smooth, 1e-30)
        q_ema_eff = self.q_ema
        if innov_change > 3.0:
            q_ema_eff = min(0.5, self.q_ema * innov_change / 3.0)

        Q_raw = self._innov_smooth - self.P - R_meas
        if self._frame_count > 3:
            self.Q = (1.0 - q_ema_eff) * self.Q + q_ema_eff * Q_raw
        Q_floor = R_meas * 0.001
        self.Q = max(self.Q, Q_floor)

        # ⑥ Riccati prediction (Q scaled by adaptive dt)
        P_pred = self.P + self.Q * dt
        K_riccati = P_pred / (P_pred + R_meas + 1e-30)
        K_riccati = max(self.alpha_min, min(self.alpha_max, K_riccati))

        ratio = self._innov_smooth / max(R_meas, eps)
        K_ibvss = max(0.0, 1.0 - 1.0 / max(ratio, 1.001))

        # Fix5: K upper bound from IBVSS reference (prevents Riccati overshoot at low Q/R)
        K_cap = K_ibvss * 1.2 + 0.01
        K_riccati = min(K_riccati, K_cap)

        # ⑥b Warmup hybrid: IBVSS fallback with adaptive convergence detection
        if not self._use_riccati:
            self.K = max(self.alpha_min, min(self.alpha_max, K_ibvss))

            if self._frame_count >= self.warmup_min:
                delta_P = abs(P_pred - self._P_prev) / max(self._P_prev, 1e-30)
                diff_K = abs(K_riccati - K_ibvss) / max(K_ibvss, 0.01)
                if delta_P < 0.05 or diff_K < 0.20:
                    self._consecutive_match += 1
                else:
                    self._consecutive_match = 0

                if (self._consecutive_match >= self.warmup_debounce
                        or self._frame_count >= self.warmup_max):
                    self._use_riccati = True
                    K_cur = self.K
                    self.P = K_cur * R_meas / max(1.0 - K_cur, 0.01)
            self._P_prev = P_pred
        else:
            self.K = K_riccati

        # ⑦ State update
        self.H_main[:] += self.K * innovation

        # ⑧ Posterior covariance
        self.P = (1.0 - self.K) * P_pred

        # ⑨ Adaptive c_model: cap to p_minus * 0.5 for robustness
        if self.adaptive_cmodel and self._frame_count >= 3:
            d_smooth = self.H_main[0::2] - self.H_main[1::2]
            c_raw = float(np.mean(np.abs(d_smooth) ** 2)) / 4
            noise_bias = self.K / (2.0 * (2.0 - self.K)) * R_meas
            c_new = max(0.0, c_raw - noise_bias)
            self._c_model_est = min(c_new, p_minus * 0.5)
        self.alpha_history.append(self.K)
        self.rot_history.append(rot)
        self.ratio_history.append(ratio)
        self.snr_history.append(snr_db)
        self.innov_history.append(innov_power)
        self.P_history.append(self.P)
        self.Q_history.append(self.Q)
        return self.H_main.copy()


class StructuredChannelOutput:
    """Post-processing layer: extracts structured features + compressed H.

    Designed to run on top of IBVSS_EMA / ScalarKalmanEMA output.
    Supports both single-antenna (1×1) and MIMO (N_rx × N_tx) modes.

    Outputs a tuple ``(S, H_c)`` where *S* is a dict of structure fields
    aligned with the DL CSI pipeline (Junsu's CSE-CsiNet v3.3) for
    Digital-Twin data consistency.

    Shared structural fields (DL/UL aligned):
        p_inst, p_state   — instantaneous / accumulated PDP
        R_rx_inst, R_rx_state — instantaneous / accumulated RX spatial cov
        d_inst, d_state   — multi-lag temporal autocorrelation / accumulated
        confidence        — cold-start / reliability gate [0, 1]

    UL-specific fields:
        rsrp, snr_db, doppler_hz, speed_ms, ds_rms_s, sv

    Compressed channel:
        h_taps — delay-domain kept taps (actual compressed payload)
        H_c    — full-band DFT reconstruction (for eval / plotting)

    Compatibility aliases (old field names still present):
        pdp  → p_inst
        R_rx → R_rx_inst
    """

    def __init__(
        self,
        n_sc: int,
        n_rx: int = 1,
        n_tx: int = 1,
        n_tap: int = 64,
        scs_hz: float = 30e3,
        carrier_freq: float = 3.5e9,
        srs_period_s: float = 5e-3,
        extract_every: int = 1,
        ema_p: float = 0.10,
        ema_R: float = 0.05,
        ema_d: float = 0.15,
        l_lag: int = 8,
        warmup_frames: int = 20,
    ):
        self.n_sc = n_sc
        self.n_rx = n_rx
        self.n_tx = n_tx
        self.n_tap = min(n_tap, n_sc // 2)
        self.scs_hz = scs_hz
        self.carrier_freq = carrier_freq
        self.srs_period_s = srs_period_s
        self.extract_every = extract_every
        self._frame_count = 0
        self._prev_phase = 0.0

        self.ema_p = ema_p
        self.ema_R = ema_R
        self.ema_d = ema_d
        self.l_lag = l_lag
        self.warmup_frames = warmup_frames

        self.p_state = None
        self.R_rx_state = None
        self.d_state = None
        self._H_history = []

    # ------------------------------------------------------------------

    def extract(self, H_smooth, snr_db=0.0, rot_angle=0.0):
        """Extract structured features from smoothed channel estimate.

        Parameters
        ----------
        H_smooth : ndarray
            For SISO: shape (n_sc,)
            For MIMO: shape (n_rx, n_tx, n_sc)
        snr_db   : float — SNR estimate from IBVSS/Kalman
        rot_angle : float — de-rotation angle from IBVSS/Kalman

        Returns ``(S, H_c)`` or ``None`` if not an extraction frame.
        """
        self._frame_count += 1
        if self._frame_count % self.extract_every != 0:
            return None

        is_mimo = H_smooth.ndim == 3
        if not is_mimo:
            H = H_smooth[np.newaxis, np.newaxis, :]
        else:
            H = H_smooth

        n_rx, n_tx, n_sc = H.shape

        # -- RSRP --
        rsrp = float(np.mean(np.abs(H) ** 2))

        # -- Doppler (scalar, from rot_angle) --
        rr = abs(rot_angle - self._prev_phase)
        if rr > np.pi:
            rr = 2 * np.pi - rr
        self._prev_phase = rot_angle
        fd = rr / (2 * np.pi * self.srs_period_s) if self.srs_period_s > 0 else 0.0
        speed_ms = fd * 3e8 / self.carrier_freq if self.carrier_freq > 0 else 0.0

        # -- PDP --
        h_time = np.fft.ifft(H, axis=-1)
        pdp_full = np.mean(np.abs(h_time) ** 2, axis=(0, 1))
        p_inst = pdp_full[: self.n_tap].copy()

        self.p_state = self._ema(self.p_state, p_inst, self.ema_p)

        # -- RMS delay spread --
        dt = 1.0 / (n_sc * self.scs_hz)
        tau = np.arange(self.n_tap) * dt
        total = float(np.sum(p_inst)) + 1e-30
        mean_delay = float(np.sum(tau * p_inst) / total)
        mean_delay_sq = float(np.sum(tau ** 2 * p_inst) / total)
        ds_rms = float(np.sqrt(max(mean_delay_sq - mean_delay ** 2, 0.0)))

        # -- Compressed H: h_taps (payload) + H_c (full-band recon) --
        h_causal = h_time[:, :, : self.n_tap]
        h_anticausal = h_time[:, :, -self.n_tap :]
        h_taps = np.concatenate([h_causal, h_anticausal], axis=-1)

        h_trunc = np.zeros_like(h_time)
        h_trunc[:, :, : self.n_tap] = h_causal
        h_trunc[:, :, -self.n_tap :] = h_anticausal
        H_c = np.fft.fft(h_trunc, axis=-1)

        if not is_mimo:
            H_c = H_c[0, 0]
            h_taps = h_taps[0, 0]

        # -- Multi-lag temporal autocorrelation (d_inst) --
        self._H_history.append(H.copy())
        if len(self._H_history) > self.l_lag + 1:
            self._H_history.pop(0)

        d_inst = np.zeros(self.l_lag)
        H_flat = H.reshape(-1)
        pow_cur = float(np.sum(np.abs(H_flat) ** 2)) + 1e-30
        for lag in range(1, min(self.l_lag + 1, len(self._H_history))):
            H_prev = self._H_history[-(lag + 1)].reshape(-1)
            pow_prev = float(np.sum(np.abs(H_prev) ** 2)) + 1e-30
            denom = np.sqrt(pow_cur * pow_prev) + 1e-30
            d_inst[lag - 1] = float(abs(np.vdot(H_prev, H_flat))) / denom

        self.d_state = self._ema(self.d_state, d_inst, self.ema_d)

        # -- Confidence --
        confidence = float(min(1.0, self._frame_count / max(self.warmup_frames, 1)))

        # -- Build S dict --
        S = {
            "p_inst": p_inst,
            "p_state": self.p_state.copy(),
            "d_inst": d_inst,
            "d_state": self.d_state.copy(),
            "confidence": confidence,
            "rsrp": rsrp,
            "snr_db": snr_db,
            "doppler_hz": fd,
            "speed_ms": speed_ms,
            "ds_rms_s": ds_rms,
            "h_taps": h_taps,
            "pdp": p_inst,
        }

        # -- Spatial covariance + singular values --
        # Always emit these fields so SISO and MIMO share the same schema.
        R_rx_inst = np.zeros((n_rx, n_rx), dtype=np.complex128)
        for t in range(n_tx):
            h_stack = H[:, t, :].T  # (n_sc, n_rx)
            R_rx_inst += h_stack.conj().T @ h_stack
        R_rx_inst /= (n_sc * n_tx)

        self.R_rx_state = self._ema(self.R_rx_state, R_rx_inst, self.ema_R)

        sv_acc = np.zeros(min(n_rx, n_tx))
        for k in range(n_sc):
            _, s, _ = np.linalg.svd(H[:, :, k], full_matrices=False)
            sv_acc += s

        S["sv"] = sv_acc / n_sc
        S["R_rx_inst"] = R_rx_inst
        S["R_rx_state"] = self.R_rx_state.copy()
        S["R_rx"] = R_rx_inst

        return S, H_c

    # ------------------------------------------------------------------

    @staticmethod
    def _ema(state, inst, alpha):
        """EMA update; first-frame initialises from inst."""
        if state is None:
            return inst.copy()
        return (1.0 - alpha) * state + alpha * inst


# ── Sanity check: both implementations agree ──
if __name__ == "__main__":
    np.random.seed(0)
    n_sc, n_frames = 64, 20
    H_seq = np.random.randn(n_frames, n_sc) + 1j * np.random.randn(n_frames, n_sc)

    for alpha in [0.05, 0.1, 0.3]:
        fc = SRS2DFilter(n_sc, alpha)
        fv = SRS2DFilterVec(n_sc, alpha)
        max_err = 0.0
        for t in range(n_frames):
            rc = fc.update(H_seq[t])
            rv = fv.update(H_seq[t])
            max_err = max(max_err, np.max(np.abs(rc - rv)))
        print(f"alpha={alpha:.2f}  max |C - Vec| = {max_err:.2e}")
