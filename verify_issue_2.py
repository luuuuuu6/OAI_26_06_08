"""
verify_issue_2.py — 验证 v4.py Issue #2: sample_times 不更新导致信道不变

用法:  cd G1C_MultiUE_MIMO_Channel_Proxy && python verify_issue_2.py [--gpu 0]

预期结果:
  - "Phase A" 两次调用 generate_fn() 输出完全相同 → 确认 Issue #2 存在
  - "Phase B" 使用递增 sample_times 后输出不同   → 确认修复方向正确

附带验证 Issue #1: carrier_frequency=3.5 vs 3.5e9 对 Doppler 的影响
"""
import os, sys, argparse, time
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("--gpu", type=int, default=0)
ap.add_argument("--npy-dir", type=str, default=None,
                help="Path to saved_rays_data/ (auto-detected if not given)")
args = ap.parse_args()

os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu)
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

import tensorflow as tf
from sionna.phy import PI, SPEED_OF_LIGHT
from sionna.phy.channel.tr38901 import PanelArray, Topology, Rays
from channel_coefficients_JIN import ChannelCoefficientsGeneratorJIN

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NPY_DIR = args.npy_dir or os.path.join(os.path.dirname(SCRIPT_DIR), "saved_rays_data")

N_UE, N_BS, N_FFT, scs = 1, 1, 2048, 30e3
buffer_symbol_size = 100
batch_size = 1
gnb_nx, gnb_ny = 1, 1
ue_nx, ue_ny = 1, 1
Speed = 10.0

# ── ray data ──
print(f"Loading ray data from: {NPY_DIR}")
phi_r = tf.convert_to_tensor(np.load(os.path.join(NPY_DIR, "phi_r_rays_for_ChannelBlock.npy")))
phi_t = tf.convert_to_tensor(np.load(os.path.join(NPY_DIR, "phi_t_rays_for_ChannelBlock.npy")))
theta_r = tf.convert_to_tensor(np.load(os.path.join(NPY_DIR, "theta_r_rays_for_ChannelBlock.npy")))
theta_t = tf.convert_to_tensor(np.load(os.path.join(NPY_DIR, "theta_t_rays_for_ChannelBlock.npy")))
power = tf.convert_to_tensor(np.load(os.path.join(NPY_DIR, "power_rays_for_ChannelBlock.npy")))
tau = tf.convert_to_tensor(np.load(os.path.join(NPY_DIR, "tau_rays_for_ChannelBlock.npy")))

mean_xpr, stddev_xpr = 7, 4  # UMa-NLOS defaults
xpr_pdp = 10 ** (tf.random.normal(
    shape=[batch_size, N_BS, N_UE, 1, phi_r.shape[-1]],
    mean=mean_xpr, stddev=stddev_xpr) / 10)
PDP = Rays(delays=tau, powers=power, aoa=phi_r, aod=phi_t,
           zoa=theta_r, zod=theta_t, xpr=xpr_pdp)

velocities = tf.abs(tf.random.normal(
    shape=[batch_size, N_UE, 3], mean=Speed, stddev=0.1, dtype=tf.float32))
topology = Topology(
    velocities, "rx",
    tf.zeros([batch_size, N_BS, N_UE]),
    tf.zeros([batch_size, N_BS, N_UE]),
    tf.zeros([batch_size, N_BS, N_UE]),
    tf.zeros([batch_size, N_BS, N_UE]),
    tf.random.uniform([batch_size, N_BS, N_UE], 0, 2, dtype=tf.int32) > 0,
    tf.ones([1, N_BS, N_UE]),
    tf.random.normal([batch_size, N_BS, 3], 0, PI / 5, dtype=tf.float32),
    tf.random.normal([batch_size, N_UE, 3], 0, PI / 5, dtype=tf.float32))

from channel_coefficients_JIN import random_binary_mask_tf_complex64
ActiveUE_fixed = tf.constant(random_binary_mask_tf_complex64(N_UE, k=N_UE), dtype=tf.complex64)
ServingBS_fixed = tf.constant(random_binary_mask_tf_complex64(N_BS, k=N_BS), dtype=tf.complex64)


def run_test(carrier_freq, label):
    print(f"\n{'='*70}")
    print(f"  {label}:  carrier_frequency = {carrier_freq}")
    lam = SPEED_OF_LIGHT / carrier_freq
    print(f"  lambda_0 = {lam:.6e} m  (correct ≈ 0.0857 m)")
    print(f"{'='*70}")

    ArrayTX = PanelArray(
        num_rows_per_panel=gnb_ny, num_cols_per_panel=gnb_nx,
        num_rows=1, num_cols=1, polarization='single',
        polarization_type='V', antenna_pattern='omni',
        carrier_frequency=carrier_freq)
    ArrayRX = PanelArray(
        num_rows_per_panel=ue_ny, num_cols_per_panel=ue_nx,
        num_rows=1, num_cols=1, polarization='single',
        polarization_type='V', antenna_pattern='omni',
        carrier_frequency=carrier_freq)

    gen = ChannelCoefficientsGeneratorJIN(carrier_freq, scs, ArrayTX, ArrayRX, False)
    h_field, aoa, zoa = gen._H_PDP_FIX(topology, PDP, N_FFT, scs)
    h_field = tf.transpose(h_field, [0, 3, 5, 6, 1, 2, 7, 4])
    aoa = tf.transpose(aoa, [0, 3, 1, 2, 4])
    zoa = tf.transpose(zoa, [0, 3, 1, 2, 4])

    sample_times_fixed = tf.cast(
        tf.range(buffer_symbol_size), gen.rdtype) / tf.constant(scs, gen.rdtype)

    def generate(st):
        h_delay, _, _, _ = gen._H_TTI_sequential_fft_o_ELW2_noProfile(
            topology, ActiveUE_fixed, ServingBS_fixed, st, h_field, aoa, zoa)
        return h_delay

    # ── Phase A: 复现 v4.py 的 bug（固定 sample_times）──
    print("\n--- Phase A: Fixed sample_times (reproducing v4.py behavior) ---")
    h1 = generate(sample_times_fixed).numpy()
    h2 = generate(sample_times_fixed).numpy()
    max_diff = np.abs(h1 - h2).max()
    identical = np.array_equal(h1, h2)
    print(f"  h1.shape = {h1.shape}")
    print(f"  max |h1 - h2| = {max_diff}")
    print(f"  identical?     = {identical}")
    if identical:
        print("  >>> CONFIRMED: Issue #2 exists — channel output is IDENTICAL across calls")
    else:
        print(f"  >>> Unexpected: outputs differ (max_diff={max_diff})")

    # ── Phase B: 正确做法（递增 sample_times）──
    print("\n--- Phase B: Incrementing sample_times (correct behavior) ---")
    st_batch0 = tf.cast(tf.range(0, buffer_symbol_size), gen.rdtype) / scs
    st_batch1 = tf.cast(tf.range(buffer_symbol_size, 2 * buffer_symbol_size), gen.rdtype) / scs
    h_b0 = generate(st_batch0).numpy()
    h_b1 = generate(st_batch1).numpy()
    max_diff_b = np.abs(h_b0 - h_b1).max()
    identical_b = np.array_equal(h_b0, h_b1)
    print(f"  max |batch0 - batch1| = {max_diff_b}")
    print(f"  identical?             = {identical_b}")
    if not identical_b:
        rel_change = max_diff_b / (np.abs(h_b0).max() + 1e-30)
        print(f"  relative change        = {rel_change:.6e}")
        print("  >>> GOOD: Channel evolves over time when sample_times increment")
    else:
        print("  >>> WARNING: Even with different sample_times, output is identical!")
        print("  >>> This likely means Doppler effect is zero (check carrier_frequency / velocity)")

    return identical, identical_b


print("\n" + "#" * 70)
print("# Test 1: Using v4.py's ORIGINAL carrier_frequency = 3.5 (likely wrong)")
print("#" * 70)
ident_a1, ident_b1 = run_test(3.5, "Issue #1 check: carrier_freq=3.5")

print("\n" + "#" * 70)
print("# Test 2: Using CORRECT carrier_frequency = 3.5e9")
print("#" * 70)
ident_a2, ident_b2 = run_test(3.5e9, "Corrected: carrier_freq=3.5e9")

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"Issue #2 (sample_times fixed):  {'CONFIRMED' if ident_a2 else 'not reproduced'}")
print(f"Issue #1 (carrier_freq=3.5):    "
      f"{'Doppler negligible — lambda_0 off by 1e9' if ident_b1 else 'Doppler still works somehow'}")
print(f"Both fixed (freq=3.5e9 + incr): "
      f"{'Channel properly evolves' if not ident_b2 else 'STILL BROKEN — investigate further'}")
print("=" * 70)
