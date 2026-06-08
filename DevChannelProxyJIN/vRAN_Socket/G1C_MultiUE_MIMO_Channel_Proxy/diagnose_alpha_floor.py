#!/usr/bin/env python3
"""
diagnose_alpha_floor.py — 诊断 SRS vs GT 的 -7 dB floor 来源

分析内容:
  1. Per-frame NMSE 分布 + 好帧/差帧分类
  2. Per-SC alpha 变化（scale 是否 frequency-dependent）
  3. 画一帧的 SRS vs GT 频谱对比（best/worst/median 帧）
  4. 残差频谱分析（error 是白噪声还是有结构）

Usage:
  python3 diagnose_alpha_floor.py --run-dir ../../logs/save1_test_20260512_1100/snr_20dB
"""
import argparse
import sys
import numpy as np

sys.path.insert(0, '.')
from digital_twin_stats import load_srs_v2, load_gt, align_by_slot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-dir', required=True)
    ap.add_argument('--tol', type=int, default=4)
    ap.add_argument('--plot', action='store_true', help='Save plots (requires matplotlib)')
    args = ap.parse_args()

    # Load data
    print(f'[load] {args.run_dir}')
    H_srs, meta = load_srs_v2(args.run_dir)
    H_gt, gt_slots = load_gt(f'{args.run_dir}/sionna_gt', return_slot_ids=True)

    srs_abs = np.asarray(meta['abs_slots'], dtype=np.int64)
    srs_idx, gt_idx, _ = align_by_slot(srs_abs, gt_slots, tol_slots=args.tol)

    H_s = H_srs[srs_idx]  # (N, rx, tx, sc) complex
    H_g = H_gt[gt_idx]

    N, n_rx, n_tx, n_sc = H_s.shape
    print(f'Paired: {N} frames, shape=({n_rx},{n_tx},{n_sc})')

    # Find active SC (non-zero in SRS)
    srs_power_per_sc = np.mean(np.abs(H_s) ** 2, axis=(0, 1, 2))
    active_mask = srs_power_per_sc > 0
    n_active = np.sum(active_mask)
    print(f'Active SC: {n_active}/{n_sc}')

    # ═══ Analysis 1: Per-frame alpha and NMSE ═══
    print('\n' + '=' * 60)
    print(' Analysis 1: Per-frame alpha and NMSE')
    print('=' * 60)

    alphas = np.zeros(N, dtype=complex)
    nmse_per_frame = np.zeros(N)

    for i in range(N):
        s = H_s[i].flatten()
        g = H_g[i].flatten()
        # LS alpha: minimize |s - alpha * g|^2
        alpha = np.sum(s * np.conj(g)) / np.sum(np.abs(g) ** 2)
        alphas[i] = alpha
        aligned = alpha * g
        err = np.sum(np.abs(s - aligned) ** 2)
        sig = np.sum(np.abs(s) ** 2)
        nmse_per_frame[i] = 10 * np.log10(err / sig) if sig > 0 else np.nan

    print(f'|alpha| stats: median={np.median(np.abs(alphas)):.2f}, '
          f'std={np.std(np.abs(alphas)):.2f}, '
          f'min={np.min(np.abs(alphas)):.2f}, max={np.max(np.abs(alphas)):.2f}')
    print(f'angle(alpha) stats: median={np.median(np.angle(alphas)):.4f} rad, '
          f'std={np.std(np.angle(alphas)):.4f} rad')
    print(f'NMSE per-frame: p10={np.percentile(nmse_per_frame,10):.2f}, '
          f'p50={np.percentile(nmse_per_frame,50):.2f}, '
          f'p90={np.percentile(nmse_per_frame,90):.2f} dB')

    # Best / worst / median frames
    sorted_idx = np.argsort(nmse_per_frame)
    best_i = sorted_idx[0]
    worst_i = sorted_idx[-1]
    med_i = sorted_idx[N // 2]
    print(f'\nBest frame:   idx={best_i}, NMSE={nmse_per_frame[best_i]:.2f} dB, |alpha|={np.abs(alphas[best_i]):.2f}')
    print(f'Median frame: idx={med_i}, NMSE={nmse_per_frame[med_i]:.2f} dB, |alpha|={np.abs(alphas[med_i]):.2f}')
    print(f'Worst frame:  idx={worst_i}, NMSE={nmse_per_frame[worst_i]:.2f} dB, |alpha|={np.abs(alphas[worst_i]):.2f}')

    # ═══ Analysis 2: Per-SC alpha variation ═══
    print('\n' + '=' * 60)
    print(' Analysis 2: Per-SC alpha (is scale frequency-dependent?)')
    print('=' * 60)

    # Use median frame for per-SC analysis
    s_med = H_s[med_i, 0, 0, :]  # rx=0, tx=0
    g_med = H_g[med_i, 0, 0, :]

    # Per-SC alpha (only active SC)
    alpha_per_sc = np.zeros(n_sc, dtype=complex)
    for sc in range(n_sc):
        if active_mask[sc] and np.abs(g_med[sc]) > 0:
            alpha_per_sc[sc] = s_med[sc] / g_med[sc]

    active_alphas = np.abs(alpha_per_sc[active_mask])
    active_alphas = active_alphas[active_alphas > 0]

    if len(active_alphas) > 0:
        print(f'Per-SC |alpha| (median frame, rx0/tx0):')
        print(f'  median={np.median(active_alphas):.2f}, std={np.std(active_alphas):.2f}')
        print(f'  min={np.min(active_alphas):.2f}, max={np.max(active_alphas):.2f}')
        print(f'  CoV (std/mean)={np.std(active_alphas)/np.mean(active_alphas):.4f}')
        print(f'  → CoV < 0.1 means scale is nearly constant across freq (good)')
        print(f'  → CoV > 0.3 means significant per-SC scale variation (bad)')

    # ═══ Analysis 3: Residual structure ═══
    print('\n' + '=' * 60)
    print(' Analysis 3: Residual error structure (median frame)')
    print('=' * 60)

    global_alpha = alphas[med_i]
    residual = s_med - global_alpha * g_med  # error after global align
    residual_active = residual[active_mask]
    signal_active = s_med[active_mask]

    res_power = np.abs(residual_active) ** 2
    sig_power = np.abs(signal_active) ** 2

    print(f'Residual power stats (active SC):')
    print(f'  mean={np.mean(res_power):.2f}, std={np.std(res_power):.2f}')
    print(f'  max/mean ratio={np.max(res_power)/np.mean(res_power):.1f}')
    print(f'  → ratio < 5: residual is noise-like (white)')
    print(f'  → ratio > 20: residual has structure (specific SC are bad)')

    # Check if residual is correlated with signal (would indicate scale error)
    corr = np.abs(np.corrcoef(np.abs(residual_active), np.abs(signal_active))[0, 1])
    print(f'\n|residual| vs |signal| correlation: {corr:.4f}')
    print(f'  → corr < 0.1: residual independent of signal (noise-dominated)')
    print(f'  → corr > 0.5: residual scales with signal (multiplicative error)')

    # ═══ Analysis 4: Slot gap impact ═══
    print('\n' + '=' * 60)
    print(' Analysis 4: Slot gap vs NMSE correlation')
    print('=' * 60)

    gaps = np.abs(srs_abs[srs_idx] - gt_slots[gt_idx])
    corr_gap_nmse = np.corrcoef(gaps, nmse_per_frame)[0, 1]
    print(f'Slot gap range: {gaps.min()} to {gaps.max()}, median={np.median(gaps):.0f}')
    print(f'Correlation(gap, NMSE): {corr_gap_nmse:.4f}')
    print(f'  → corr > 0.3: larger gap = worse NMSE (alignment matters)')
    print(f'  → corr < 0.1: gap doesn\'t affect NMSE (alignment is fine)')

    # Gap=0 vs gap>0 comparison
    zero_gap = gaps == 0
    if np.sum(zero_gap) > 5:
        nmse_gap0 = np.median(nmse_per_frame[zero_gap])
        nmse_gapN = np.median(nmse_per_frame[~zero_gap])
        print(f'\nNMSE median for gap=0: {nmse_gap0:.2f} dB ({np.sum(zero_gap)} frames)')
        print(f'NMSE median for gap>0: {nmse_gapN:.2f} dB ({np.sum(~zero_gap)} frames)')
        print(f'Difference: {nmse_gapN - nmse_gap0:+.2f} dB')

    # ═══ Summary ═══
    print('\n' + '=' * 60)
    print(' SUMMARY')
    print('=' * 60)
    print(f'Floor NMSE p50 = {np.percentile(nmse_per_frame, 50):.2f} dB')
    print(f'|alpha| variation (per-frame): std/median = {np.std(np.abs(alphas))/np.median(np.abs(alphas)):.4f}')
    if len(active_alphas) > 0:
        cov = np.std(active_alphas) / np.mean(active_alphas)
        print(f'|alpha| variation (per-SC):    CoV = {cov:.4f}')
    print(f'Residual-signal correlation:   {corr:.4f}')
    print(f'Gap-NMSE correlation:          {corr_gap_nmse:.4f}')

    if args.plot:
        try:
            import matplotlib.pyplot as plt

            fig, axes = plt.subplots(2, 2, figsize=(14, 10))

            # Plot 1: NMSE histogram
            axes[0, 0].hist(nmse_per_frame, bins=30, edgecolor='black')
            axes[0, 0].axvline(np.median(nmse_per_frame), color='r', linestyle='--', label=f'p50={np.median(nmse_per_frame):.1f}')
            axes[0, 0].set_xlabel('NMSE (dB)')
            axes[0, 0].set_title('Per-frame NMSE distribution')
            axes[0, 0].legend()

            # Plot 2: SRS vs GT spectrum (median frame)
            sc_range = np.arange(n_sc)
            axes[0, 1].plot(sc_range[active_mask], 20 * np.log10(np.abs(s_med[active_mask]) + 1), label='SRS', alpha=0.7)
            axes[0, 1].plot(sc_range[active_mask], 20 * np.log10(np.abs(global_alpha * g_med[active_mask]) + 1), label='GT×α', alpha=0.7)
            axes[0, 1].set_xlabel('Subcarrier')
            axes[0, 1].set_ylabel('Magnitude (dB)')
            axes[0, 1].set_title(f'Spectrum comparison (median frame, NMSE={nmse_per_frame[med_i]:.1f} dB)')
            axes[0, 1].legend()

            # Plot 3: Per-SC alpha magnitude
            axes[1, 0].plot(sc_range[active_mask], active_alphas[:n_active], '.', markersize=1)
            axes[1, 0].axhline(np.median(active_alphas), color='r', linestyle='--')
            axes[1, 0].set_xlabel('Subcarrier')
            axes[1, 0].set_ylabel('|alpha| per SC')
            axes[1, 0].set_title('Per-SC scale factor (median frame)')

            # Plot 4: Residual magnitude
            axes[1, 1].plot(sc_range[active_mask], 10 * np.log10(res_power + 1), '.', markersize=1)
            axes[1, 1].set_xlabel('Subcarrier')
            axes[1, 1].set_ylabel('Residual power (dB)')
            axes[1, 1].set_title('Residual after global align (median frame)')

            plt.tight_layout()
            out_path = f'{args.run_dir}/alpha_floor_diagnosis.png'
            plt.savefig(out_path, dpi=150)
            print(f'\nPlot saved: {out_path}')
            plt.close()
        except ImportError:
            print('\nmatplotlib not available, skipping plots')


if __name__ == '__main__':
    main()
