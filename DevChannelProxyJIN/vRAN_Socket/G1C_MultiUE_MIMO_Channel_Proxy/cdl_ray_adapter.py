"""
Step 2: CDL Ray Parameter Adapter for v8.py.

Extracts per-ray parameters from Sionna CDL models and saves them
in the format expected by v8.py's Rays() interface.

Output shape: (batch=1, N_BS=1, N_UE=1, 1, n_rays)
where n_rays = num_clusters × NUM_RAYS_PER_CLUSTER (24×20=480 for CDL-C)

Usage (inside Sionna container):
  python3 cdl_ray_adapter.py --model C --delay_spread 100e-9 --speed 30 --output ./data_out/cdl_c_30kmh
  python3 cdl_ray_adapter.py --model A --delay_spread 30e-9 --speed 3 --output ./data_out/cdl_a_3kmh
"""
import os
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

import argparse
import numpy as np


def extract_cdl_rays(model: str, delay_spread: float, carrier_frequency: float,
                     speed_kmh: float, num_bs_ant: int = 4, num_ue_ant: int = 1):
    """Extract per-ray parameters from Sionna CDL model.

    Returns dict with keys: phi_r, phi_t, theta_r, theta_t, power, tau, xpr
    Each shaped (1, 1, 1, 1, n_rays) matching v8.py's expected format.
    """
    import tensorflow as tf
    tf.get_logger().setLevel("ERROR")
    from sionna.phy.channel.tr38901 import CDL, PanelArray

    bs_array = PanelArray(
        num_rows_per_panel=num_bs_ant, num_cols_per_panel=1,
        polarization='single', polarization_type='V',
        antenna_pattern='omni', carrier_frequency=carrier_frequency)
    ue_array = PanelArray(
        num_rows_per_panel=num_ue_ant, num_cols_per_panel=1,
        polarization='single', polarization_type='V',
        antenna_pattern='omni', carrier_frequency=carrier_frequency)

    speed_ms = speed_kmh / 3.6
    cdl = CDL(model=model, delay_spread=delay_spread,
              carrier_frequency=carrier_frequency,
              ut_array=ue_array, bs_array=bs_array,
              direction='uplink',
              min_speed=speed_ms, max_speed=speed_ms)

    num_clusters = int(cdl._num_clusters.numpy())
    num_rays_per_cluster = cdl.NUM_RAYS

    aoa = cdl._aoa.numpy().squeeze()       # (num_clusters, num_rays)
    aod = cdl._aod.numpy().squeeze()
    zoa = cdl._zoa.numpy().squeeze()
    zod = cdl._zod.numpy().squeeze()
    delays = cdl.delays.numpy()            # (num_clusters,) — absolute, in seconds
    powers = cdl._powers.numpy().squeeze()  # (num_clusters,) — normalized, sum=1

    n_rays = num_clusters * num_rays_per_cluster

    aoa_flat = aoa.reshape(n_rays)
    aod_flat = aod.reshape(n_rays)
    zoa_flat = zoa.reshape(n_rays)
    zod_flat = zod.reshape(n_rays)

    delays_flat = np.repeat(delays, num_rays_per_cluster)
    powers_per_ray = powers / num_rays_per_cluster
    powers_flat = np.repeat(powers_per_ray, num_rays_per_cluster)

    xpr_dB = 7.0  # 3GPP default for CDL NLOS models
    if model in ('D', 'E'):
        xpr_dB = 11.0
    xpr_linear = np.full(n_rays, 10.0 ** (xpr_dB / 10.0), dtype=np.float32)

    target_shape = (1, 1, 1, 1, n_rays)
    ray_data = {
        'phi_r': aoa_flat.reshape(target_shape).astype(np.float32),
        'phi_t': aod_flat.reshape(target_shape).astype(np.float32),
        'theta_r': zoa_flat.reshape(target_shape).astype(np.float32),
        'theta_t': zod_flat.reshape(target_shape).astype(np.float32),
        'power': powers_flat.reshape(target_shape).astype(np.float32),
        'tau': delays_flat.reshape(target_shape).astype(np.float32),
        'xpr': xpr_linear.reshape(target_shape).astype(np.float32),
    }

    metadata = {
        'model': model,
        'delay_spread': delay_spread,
        'carrier_frequency': carrier_frequency,
        'speed_kmh': speed_kmh,
        'num_clusters': num_clusters,
        'num_rays_per_cluster': num_rays_per_cluster,
        'n_rays_total': n_rays,
        'xpr_dB': xpr_dB,
    }

    return ray_data, metadata


def save_ray_data(ray_data: dict, metadata: dict, output_dir: str):
    """Save ray data as numpy files (v8.py compatible format)."""
    os.makedirs(output_dir, exist_ok=True)

    np.save(os.path.join(output_dir, "phi_r_rays_for_ChannelBlock.npy"), ray_data['phi_r'])
    np.save(os.path.join(output_dir, "phi_t_rays_for_ChannelBlock.npy"), ray_data['phi_t'])
    np.save(os.path.join(output_dir, "theta_r_rays_for_ChannelBlock.npy"), ray_data['theta_r'])
    np.save(os.path.join(output_dir, "theta_t_rays_for_ChannelBlock.npy"), ray_data['theta_t'])
    np.save(os.path.join(output_dir, "power_rays_for_ChannelBlock.npy"), ray_data['power'])
    np.save(os.path.join(output_dir, "tau_rays_for_ChannelBlock.npy"), ray_data['tau'])

    np.savez(os.path.join(output_dir, "cdl_metadata.npz"), **metadata)

    print(f"  Saved to: {output_dir}")
    print(f"  Model: CDL-{metadata['model']}, DS={metadata['delay_spread']*1e9:.0f}ns, "
          f"Speed={metadata['speed_kmh']}km/h")
    print(f"  Rays: {metadata['n_rays_total']} "
          f"({metadata['num_clusters']} clusters × {metadata['num_rays_per_cluster']} rays)")
    print(f"  Delay range: [{ray_data['tau'].min()*1e9:.1f}, {ray_data['tau'].max()*1e9:.1f}] ns")
    print(f"  Power range: [{10*np.log10(ray_data['power'].max()):.1f}, "
          f"{10*np.log10(ray_data['power'][ray_data['power']>0].min()):.1f}] dB")


def main():
    parser = argparse.ArgumentParser(description="CDL Ray Adapter for v8.py")
    parser.add_argument("--model", default="C", choices=["A", "B", "C", "D", "E"])
    parser.add_argument("--delay_spread", type=float, default=100e-9,
                        help="RMS delay spread in seconds (default: 100ns)")
    parser.add_argument("--carrier_freq", type=float, default=3.5e9)
    parser.add_argument("--speed", type=float, default=30, help="Speed in km/h")
    parser.add_argument("--output", default="./data_out/cdl_rays",
                        help="Output directory for npy files")
    parser.add_argument("--bs_ant", type=int, default=4)
    parser.add_argument("--ue_ant", type=int, default=1)
    args = parser.parse_args()

    print(f"Extracting CDL-{args.model} ray parameters...")
    ray_data, metadata = extract_cdl_rays(
        model=args.model,
        delay_spread=args.delay_spread,
        carrier_frequency=args.carrier_freq,
        speed_kmh=args.speed,
        num_bs_ant=args.bs_ant,
        num_ue_ant=args.ue_ant,
    )
    save_ray_data(ray_data, metadata, args.output)

    print(f"\nTo use in v8.py config, set:")
    print(f'  "npy_directory": "{os.path.abspath(args.output)}"')


if __name__ == "__main__":
    main()
