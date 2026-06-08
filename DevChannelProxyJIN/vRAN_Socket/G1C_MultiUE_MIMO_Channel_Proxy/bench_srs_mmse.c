/*
 * Self-contained micro-benchmark for SRS frequency-domain channel estimation.
 *
 * Two paths:
 *   - legacy_filt16: emulates OAI's filt16 scatter (16-tap real coefficients
 *                    applied to each LS pilot, accumulated into output bins).
 *   - mmse1d_local : reproduces the local-window LMMSE/Wiener filter used by
 *                    nr_srs_mmse_freq_filter() (exponential PDP correlation
 *                    R(dk) = exp(-|dk|/L_corr), Gauss elimination per target).
 *
 * Goal: report avg/min/max us per call for both, with realistic SRS sizes,
 * so we can quote the measured complexity / real-time impact.
 *
 * NOTE: This is a stand-alone benchmark, NOT linked against OAI. The legacy
 *       path uses plain C int16 multiply-add (no SSE/AVX intrinsics), so the
 *       reported legacy time is an upper bound; OAI's actual SIMD path will
 *       be faster. The mmse1d path uses the same algorithm and the same
 *       double-precision math as the real implementation, so its number is
 *       directly comparable with the OAI build.
 */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define MAX_WINDOW 8
#define DEFAULT_CORR_LEN_SC 12.0

typedef struct {
  int16_t r;
  int16_t i;
} c16_t;

static int clamp_int(int v, int lo, int hi)
{
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

static int16_t clamp_i16(double v)
{
  if (v > INT16_MAX) return INT16_MAX;
  if (v < INT16_MIN) return INT16_MIN;
  return (int16_t)lrint(v);
}

/* ---------------- legacy filt16 path (per-pilot 16-tap FIR) ---------------- */
/* OAI filt16_start = {12288, 8192, 8192, 8192, 4096, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}
 * Coefficients are real int16 weights; output is c16_t accumulator. */
static const int16_t filt16_start[16] = {
    12288, 8192, 8192, 8192, 4096, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
};

static void legacy_filt16(const c16_t *ls_est,
                          int ofdm_symbol_size,
                          uint16_t first_subcarrier,
                          uint16_t k_tc,
                          uint16_t num_pilots,
                          c16_t *out)
{
  memset(out, 0, sizeof(c16_t) * (size_t)ofdm_symbol_size);
  uint16_t pilot_sc = first_subcarrier % ofdm_symbol_size;
  for (int p = 0; p < num_pilots; p++) {
    const c16_t ls = ls_est[pilot_sc];
    for (int t = 0; t < 16; t++) {
      const int16_t w = filt16_start[t];
      if (w == 0) continue;
      uint16_t out_sc = (pilot_sc + (uint16_t)t) % (uint16_t)ofdm_symbol_size;
      int32_t acc_r = (int32_t)out[out_sc].r + ((int32_t)w * (int32_t)ls.r) / 16384;
      int32_t acc_i = (int32_t)out[out_sc].i + ((int32_t)w * (int32_t)ls.i) / 16384;
      out[out_sc].r = (int16_t)clamp_int(acc_r, INT16_MIN, INT16_MAX);
      out[out_sc].i = (int16_t)clamp_int(acc_i, INT16_MIN, INT16_MAX);
    }
    pilot_sc = (uint16_t)((pilot_sc + k_tc) % (uint16_t)ofdm_symbol_size);
  }
}

/* ---------------- mmse1d local-window LMMSE path (matches OAI) ------------- */
/* Identical to nr_srs_mmse_freq_filter() but stripped of logging / sigma2
 * config plumbing. We pass noise_norm directly (already clamped). */

static int solve_real_system(int size,
                             double matrix[MAX_WINDOW][MAX_WINDOW + 1],
                             double *solution)
{
  for (int col = 0; col < size; col++) {
    double pivot_abs = fabs(matrix[col][col]);
    int pivot = col;
    for (int row = col + 1; row < size; row++) {
      const double cand = fabs(matrix[row][col]);
      if (cand > pivot_abs) {
        pivot_abs = cand;
        pivot = row;
      }
    }
    if (pivot_abs < 1e-12) return -1;
    if (pivot != col) {
      for (int j = 0; j <= size; j++) {
        const double tmp = matrix[col][j];
        matrix[col][j] = matrix[pivot][j];
        matrix[pivot][j] = tmp;
      }
    }
    const double diag = matrix[col][col];
    for (int j = col; j <= size; j++) matrix[col][j] /= diag;
    for (int row = 0; row < size; row++) {
      if (row == col) continue;
      const double factor = matrix[row][col];
      for (int j = col; j <= size; j++) matrix[row][j] -= factor * matrix[col][j];
    }
  }
  for (int row = 0; row < size; row++) solution[row] = matrix[row][size];
  return 0;
}

static void mmse1d_local(const c16_t *ls_est,
                         int ofdm_symbol_size,
                         uint16_t first_subcarrier,
                         uint16_t k_tc,
                         uint16_t num_pilots,
                         double noise_norm,
                         int window_pilots,
                         double corr_len_sc,
                         c16_t *out)
{
  memset(out, 0, sizeof(c16_t) * (size_t)ofdm_symbol_size);
  if (window_pilots > num_pilots) window_pilots = num_pilots;
  if (window_pilots > MAX_WINDOW) window_pilots = MAX_WINDOW;

  const int span = (int)num_pilots * (int)k_tc;
  for (int offset = 0; offset < span; offset++) {
    const uint16_t target_sc = (uint16_t)((first_subcarrier + offset) % ofdm_symbol_size);
    int center_pilot = (offset + (k_tc / 2)) / k_tc;
    center_pilot = clamp_int(center_pilot, 0, num_pilots - 1);

    int start_pilot = center_pilot - window_pilots / 2;
    start_pilot = clamp_int(start_pilot, 0, num_pilots - window_pilots);

    double system[MAX_WINDOW][MAX_WINDOW + 1] = {{0}};
    double weights[MAX_WINDOW] = {0};

    for (int row = 0; row < window_pilots; row++) {
      const int pilot_row = start_pilot + row;
      const int pilot_row_offset = pilot_row * k_tc;
      for (int col = 0; col < window_pilots; col++) {
        const int pilot_col = start_pilot + col;
        const int pilot_col_offset = pilot_col * k_tc;
        system[row][col] = exp(-fabs((double)(pilot_row_offset - pilot_col_offset)) / corr_len_sc);
        if (row == col) system[row][col] += noise_norm;
      }
      system[row][window_pilots] = exp(-fabs((double)(offset - pilot_row_offset)) / corr_len_sc);
    }

    if (solve_real_system(window_pilots, system, weights) != 0) {
      const uint16_t nearest_sc = (uint16_t)((first_subcarrier + center_pilot * k_tc) % ofdm_symbol_size);
      out[target_sc] = ls_est[nearest_sc];
      continue;
    }

    double acc_r = 0.0, acc_i = 0.0;
    for (int idx = 0; idx < window_pilots; idx++) {
      const int pilot_idx = start_pilot + idx;
      const uint16_t sc = (uint16_t)((first_subcarrier + pilot_idx * k_tc) % ofdm_symbol_size);
      acc_r += weights[idx] * (double)ls_est[sc].r;
      acc_i += weights[idx] * (double)ls_est[sc].i;
    }
    out[target_sc].r = clamp_i16(acc_r);
    out[target_sc].i = clamp_i16(acc_i);
  }
}

/* ---------------- timing helpers ---------------- */
static double now_ns(void)
{
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec * 1e9 + (double)ts.tv_nsec;
}

static void rand_pilots(c16_t *ls, int n)
{
  for (int i = 0; i < n; i++) {
    ls[i].r = (int16_t)((rand() & 0xFFFF) - 32768);
    ls[i].i = (int16_t)((rand() & 0xFFFF) - 32768);
  }
}

typedef struct {
  double avg_us;
  double min_us;
  double max_us;
  int trials;
} bench_result_t;

static bench_result_t time_legacy(const c16_t *ls,
                                  int ofdm_symbol_size,
                                  uint16_t first_sc,
                                  uint16_t k_tc,
                                  uint16_t num_pilots,
                                  c16_t *out,
                                  int trials)
{
  bench_result_t r = {0, 1e18, 0, trials};
  double total_ns = 0;
  for (int it = 0; it < trials; it++) {
    const double t0 = now_ns();
    legacy_filt16(ls, ofdm_symbol_size, first_sc, k_tc, num_pilots, out);
    const double dt = now_ns() - t0;
    total_ns += dt;
    if (dt < r.min_us * 1000.0) r.min_us = dt / 1000.0;
    if (dt > r.max_us * 1000.0) r.max_us = dt / 1000.0;
  }
  r.avg_us = total_ns / trials / 1000.0;
  return r;
}

static bench_result_t time_mmse1d(const c16_t *ls,
                                  int ofdm_symbol_size,
                                  uint16_t first_sc,
                                  uint16_t k_tc,
                                  uint16_t num_pilots,
                                  double noise_norm,
                                  int window_pilots,
                                  double corr_len_sc,
                                  c16_t *out,
                                  int trials)
{
  bench_result_t r = {0, 1e18, 0, trials};
  double total_ns = 0;
  for (int it = 0; it < trials; it++) {
    const double t0 = now_ns();
    mmse1d_local(ls, ofdm_symbol_size, first_sc, k_tc, num_pilots,
                 noise_norm, window_pilots, corr_len_sc, out);
    const double dt = now_ns() - t0;
    total_ns += dt;
    if (dt < r.min_us * 1000.0) r.min_us = dt / 1000.0;
    if (dt > r.max_us * 1000.0) r.max_us = dt / 1000.0;
  }
  r.avg_us = total_ns / trials / 1000.0;
  return r;
}

int main(int argc, char **argv)
{
  /* Defaults: 273 PRB (3276 subcarriers), k_tc=2, 1638 pilots. */
  int N_PRB = 273;
  int k_tc = 2;
  int trials = 200;
  if (argc >= 2) N_PRB = atoi(argv[1]);
  if (argc >= 3) k_tc = atoi(argv[2]);
  if (argc >= 4) trials = atoi(argv[3]);

  const int ofdm_symbol_size = 4096;
  const int n_sc = N_PRB * 12;
  const int num_pilots = n_sc / k_tc;
  const uint16_t first_sc = 0;

  printf("=== SRS frequency-domain channel-estimator micro-benchmark ===\n");
  printf("CPU avg user-perceivable timer (clock_gettime CLOCK_MONOTONIC)\n");
  printf("Config: N_PRB=%d, n_sc=%d, k_tc=%d, num_pilots=%d, trials=%d\n",
         N_PRB, n_sc, k_tc, num_pilots, trials);

  c16_t *ls = calloc((size_t)ofdm_symbol_size, sizeof(c16_t));
  c16_t *out = calloc((size_t)ofdm_symbol_size, sizeof(c16_t));
  if (!ls || !out) { fprintf(stderr, "alloc fail\n"); return 1; }

  srand(0x1234);
  rand_pilots(ls, ofdm_symbol_size);

  /* warm-up */
  legacy_filt16(ls, ofdm_symbol_size, first_sc, (uint16_t)k_tc, (uint16_t)num_pilots, out);
  mmse1d_local(ls, ofdm_symbol_size, first_sc, (uint16_t)k_tc, (uint16_t)num_pilots,
               0.01, 4, DEFAULT_CORR_LEN_SC, out);

  bench_result_t legacy = time_legacy(ls, ofdm_symbol_size, first_sc,
                                      (uint16_t)k_tc, (uint16_t)num_pilots,
                                      out, trials);
  bench_result_t mmse_w4 = time_mmse1d(ls, ofdm_symbol_size, first_sc,
                                       (uint16_t)k_tc, (uint16_t)num_pilots,
                                       0.01, 4, DEFAULT_CORR_LEN_SC, out, trials);
  bench_result_t mmse_w6 = time_mmse1d(ls, ofdm_symbol_size, first_sc,
                                       (uint16_t)k_tc, (uint16_t)num_pilots,
                                       0.01, 6, DEFAULT_CORR_LEN_SC, out, trials);
  bench_result_t mmse_w8 = time_mmse1d(ls, ofdm_symbol_size, first_sc,
                                       (uint16_t)k_tc, (uint16_t)num_pilots,
                                       0.01, 8, DEFAULT_CORR_LEN_SC, out, trials);

  printf("\nResults (us per SRS-symbol estimation call):\n");
  printf("%-14s | %10s | %10s | %10s\n", "Path", "avg us", "min us", "max us");
  printf("---------------+------------+------------+------------\n");
  printf("%-14s | %10.2f | %10.2f | %10.2f\n", "legacy filt16",
         legacy.avg_us, legacy.min_us, legacy.max_us);
  printf("%-14s | %10.2f | %10.2f | %10.2f\n", "mmse1d W=4",
         mmse_w4.avg_us, mmse_w4.min_us, mmse_w4.max_us);
  printf("%-14s | %10.2f | %10.2f | %10.2f\n", "mmse1d W=6",
         mmse_w6.avg_us, mmse_w6.min_us, mmse_w6.max_us);
  printf("%-14s | %10.2f | %10.2f | %10.2f\n", "mmse1d W=8",
         mmse_w8.avg_us, mmse_w8.min_us, mmse_w8.max_us);

  printf("\nRatio mmse1d/legacy (avg):  W=4: %.1fx   W=6: %.1fx   W=8: %.1fx\n",
         mmse_w4.avg_us / legacy.avg_us,
         mmse_w6.avg_us / legacy.avg_us,
         mmse_w8.avg_us / legacy.avg_us);

  /* NR slot duration for SCS = 30 kHz is 0.5 ms; for 15 kHz is 1.0 ms. */
  printf("\nReal-time budget reference: NR 30 kHz slot = 500 us; 15 kHz slot = 1000 us\n");

  free(ls);
  free(out);
  return 0;
}
