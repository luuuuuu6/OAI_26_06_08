/**
 * Adaptive Alpha EMA for SRS Channel Estimation.
 *
 * 2D mapping: α = a1·rot_rate + a2·SNR_est_dB + a3
 *   - rot_rate: Doppler-driven phase rotation rate (speed sensing)
 *   - SNR_est:  Even-odd pair frequency-domain estimation (noise sensing, open-loop)
 *
 * Per-frame cost: ~6 ops/SC + O(1) scalar.  Memory: H_main[N_SC] + 5 floats.
 *
 * Integration point: nr_ul_channel_estimation.c, after LS estimate is available.
 */

#include <math.h>
#include <string.h>

#ifndef M_PI_F
#define M_PI_F 3.14159265f
#endif

typedef struct {
    float re;
    float im;
} cf_t;

typedef struct {
    float alpha;
    float rr_smooth;       /* rot_rate EMA state */
    float snr_smooth;      /* SNR_est EMA state (dB) */
    float prev_rot_angle;  /* previous frame's rotation angle */
    int   initialized;
} adaptive_alpha_state_t;

/* Pre-computed from CDL ray parameters: C_model = Σ P_r·(1-cos(2πΔf·τ_r))/2 */
/* For CDL-C, 30kHz SCS, 100ns DS: ~0.000136.  Set per deployment. */
static float C_MODEL = 0.000136f;

/* 2D mapping coefficients (fitted from 30-condition oracle sweep) */
static float A1 = 0.1132f;   /* rot_rate coefficient */
static float A2 = 0.0305f;   /* SNR_est_dB coefficient */
static float A3 = 0.1074f;   /* intercept */

/* EMA smoothing constants */
static float RR_EMA  = 0.05f;
static float SNR_EMA = 0.02f;

/* Alpha bounds */
static float ALPHA_MIN = 0.01f;
static float ALPHA_MAX = 0.95f;

static inline float clampf(float x, float lo, float hi) {
    return x < lo ? lo : (x > hi ? hi : x);
}

void adaptive_alpha_init(adaptive_alpha_state_t *st) {
    st->alpha          = 0.5f;
    st->rr_smooth      = 0.0f;
    st->snr_smooth     = 10.0f;
    st->prev_rot_angle = 0.0f;
    st->initialized    = 0;
}

/**
 * adaptive_alpha_update — one SRS frame update.
 *
 * @param H_obs   [n_sc] current LS channel estimate (input, not modified)
 * @param H_main  [n_sc] EMA state (input/output, persistent across frames)
 * @param st      adaptive alpha state (persistent across frames)
 * @param n_sc    number of subcarriers (e.g. 1248)
 */
void adaptive_alpha_update(const cf_t *H_obs, cf_t *H_main,
                           adaptive_alpha_state_t *st, int n_sc)
{
    if (!st->initialized) {
        memcpy(H_main, H_obs, n_sc * sizeof(cf_t));
        st->initialized = 1;
        return;
    }

    /* ── Pass 1: inner product + even-odd pair (fused) ── */

    float inner_re = 0.0f, inner_im = 0.0f;
    float p_plus = 0.0f, p_minus = 0.0f;
    int n_pairs = n_sc / 2;

    for (int k = 0; k < n_pairs; k++) {
        int k0 = 2 * k, k1 = 2 * k + 1;

        /* Inner product: H_obs · conj(H_main) */
        inner_re += H_obs[k0].re * H_main[k0].re + H_obs[k0].im * H_main[k0].im;
        inner_im += H_obs[k0].im * H_main[k0].re - H_obs[k0].re * H_main[k0].im;
        inner_re += H_obs[k1].re * H_main[k1].re + H_obs[k1].im * H_main[k1].im;
        inner_im += H_obs[k1].im * H_main[k1].re - H_obs[k1].re * H_main[k1].im;

        /* Even-odd pair */
        float sr = H_obs[k0].re + H_obs[k1].re;
        float si = H_obs[k0].im + H_obs[k1].im;
        float dr = H_obs[k0].re - H_obs[k1].re;
        float di = H_obs[k0].im - H_obs[k1].im;
        p_plus  += sr * sr + si * si;
        p_minus += dr * dr + di * di;
    }

    /* Normalize rot */
    float inv_abs = 1.0f / sqrtf(inner_re * inner_re + inner_im * inner_im + 1e-30f);
    float rot_re = inner_re * inv_abs;
    float rot_im = inner_im * inv_abs;

    /* SNR estimation */
    float pp = p_plus  / (4.0f * n_pairs);
    float pm = p_minus / (4.0f * n_pairs);
    float pm_corr = pm - C_MODEL;
    if (pm_corr < 1e-10f) pm_corr = 1e-10f;
    float snr_lin = (pp - pm) / pm_corr;
    if (snr_lin < 0.1f) snr_lin = 0.1f;
    float snr_db = 10.0f * log10f(snr_lin);

    /* rot_rate */
    float rot_angle = atan2f(rot_im, rot_re);
    float rr = fabsf(rot_angle - st->prev_rot_angle);
    if (rr > M_PI_F) rr = 2.0f * M_PI_F - rr;
    st->prev_rot_angle = rot_angle;

    /* Smooth features */
    st->rr_smooth  = (1.0f - RR_EMA)  * st->rr_smooth  + RR_EMA  * rr;
    st->snr_smooth = (1.0f - SNR_EMA) * st->snr_smooth + SNR_EMA * snr_db;

    /* 2D α mapping */
    st->alpha = clampf(A1 * st->rr_smooth + A2 * st->snr_smooth + A3,
                        ALPHA_MIN, ALPHA_MAX);

    /* ── Pass 2: de-rotate + EMA update ── */

    float alpha = st->alpha;
    for (int k = 0; k < n_sc; k++) {
        /* H_derot = H_obs * conj(rot) */
        float dr = H_obs[k].re * rot_re + H_obs[k].im * rot_im;
        float di = H_obs[k].im * rot_re - H_obs[k].re * rot_im;
        /* H_main += alpha * (H_derot - H_main) */
        H_main[k].re += alpha * (dr - H_main[k].re);
        H_main[k].im += alpha * (di - H_main[k].im);
    }
}
