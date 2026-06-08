"""
Generate CDL-C ray parameters from 3GPP TR 38.901 Table 7.7.1-4.

No Sionna dependency. Produces .npy files compatible with multisc_cdl_sim.py.

Usage:
  python3 gen_cdl_rays_standalone.py [--output data_out/cdl_c_30kmh]
"""
import argparse
import os
import numpy as np

CDL_C_TABLE = [
    #  tau_n/DS   Power(dB)  AoD(°)   AoA(°)   ZoD(°)   ZoA(°)
    (0.0000,     -4.4,       -46.6,    -18.7,   101.2,    89.2),
    (0.2099,     -1.2,       -22.8,    -20.3,    98.6,    88.0),
    (0.2219,     -3.5,       -22.8,     44.2,    98.6,    91.3),
    (0.2329,     -5.2,       -40.7,     55.2,   100.6,    92.1),
    (0.2176,     -2.5,        -1.8,     59.1,    97.8,    93.5),
    (0.6366,      0.0,         0.0,      0.0,    97.0,    90.0),
    (0.6448,     -2.2,         0.7,     37.1,    97.1,    91.0),
    (0.6560,     -3.9,        -3.5,    -23.0,    97.7,    88.5),
    (0.6584,     -7.4,         1.5,     -5.7,    97.0,    89.5),
    (0.7935,     -7.1,       -36.7,     -5.7,   100.4,    89.5),
    (1.0808,    -10.7,       -33.1,     22.6,   100.1,    90.7),
    (1.2610,    -11.1,       -22.4,     27.2,    98.5,    90.8),
    (1.5521,     -5.1,        31.9,    -12.2,    96.2,    89.6),
    (1.7146,     -6.8,        20.9,    -31.7,    96.8,    88.9),
    (2.0433,     -8.7,       -12.4,     34.4,    98.0,    91.1),
    (2.4832,    -13.2,        -2.7,    -15.3,    97.3,    89.4),
    (2.5119,    -13.9,       -13.8,    -30.4,    98.1,    88.8),
    (2.5697,    -13.9,       -38.1,     25.0,   100.2,    90.7),
    (3.0059,    -15.8,        20.1,    -10.1,    96.9,    89.7),
    (3.2052,    -17.1,       -10.5,    -10.1,    97.8,    89.7),
    (4.3024,    -16.0,       -18.9,     21.3,    98.3,    90.6),
    (4.6430,    -15.7,        -1.1,     17.9,    97.1,    90.5),
    (5.0547,    -21.6,        -9.9,      3.6,    97.6,    90.1),
    (6.3100,    -22.8,        44.6,    -10.2,    94.2,    89.7),
]

N_RAYS_PER_CLUSTER = 20

OFFSET_ANGLES_CDL = np.array([
    0.0447, -0.0447,  0.1413, -0.1413,  0.2492, -0.2492,
    0.3715, -0.3715,  0.5129, -0.5129,  0.6797, -0.6797,
    0.8844, -0.8844,  1.1481, -1.1481,  1.5195, -1.5195,
    2.1551, -2.1551,
])

C_PHI_AoA = 15.0
C_PHI_AoD = 5.0
C_THETA_ZoA = 7.0
C_THETA_ZoD = 3.0


def generate_cdl_c_rays(delay_spread_ns=100.0):
    """Generate CDL-C rays from 3GPP table. Returns dict of flat arrays."""
    ds = delay_spread_ns * 1e-9
    n_clusters = len(CDL_C_TABLE)
    n_rays = n_clusters * N_RAYS_PER_CLUSTER

    tau_flat = np.zeros(n_rays)
    power_flat = np.zeros(n_rays)
    phi_r_flat = np.zeros(n_rays)
    phi_t_flat = np.zeros(n_rays)
    theta_r_flat = np.zeros(n_rays)
    theta_t_flat = np.zeros(n_rays)

    for i, (tau_norm, p_db, aod, aoa, zod, zoa) in enumerate(CDL_C_TABLE):
        base = i * N_RAYS_PER_CLUSTER
        power_lin = 10.0 ** (p_db / 10.0) / N_RAYS_PER_CLUSTER

        for j in range(N_RAYS_PER_CLUSTER):
            idx = base + j
            tau_flat[idx] = tau_norm * ds
            power_flat[idx] = power_lin
            phi_r_flat[idx] = np.deg2rad(aoa + OFFSET_ANGLES_CDL[j] * C_PHI_AoA)
            phi_t_flat[idx] = np.deg2rad(aod + OFFSET_ANGLES_CDL[j] * C_PHI_AoD)
            theta_r_flat[idx] = np.deg2rad(zoa + OFFSET_ANGLES_CDL[j] * C_THETA_ZoA)
            theta_t_flat[idx] = np.deg2rad(zod + OFFSET_ANGLES_CDL[j] * C_THETA_ZoD)

    power_flat /= power_flat.sum()

    return {
        'phi_r': phi_r_flat.astype(np.float32),
        'phi_t': phi_t_flat.astype(np.float32),
        'theta_r': theta_r_flat.astype(np.float32),
        'theta_t': theta_t_flat.astype(np.float32),
        'power': power_flat.astype(np.float32),
        'tau': tau_flat.astype(np.float32),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data_out/cdl_c_30kmh")
    parser.add_argument("--delay-spread", type=float, default=100.0,
                        help="RMS delay spread in ns")
    args = parser.parse_args()

    rays = generate_cdl_c_rays(args.delay_spread)
    os.makedirs(args.output, exist_ok=True)

    for key, arr in rays.items():
        fname = f"{key}_rays_for_ChannelBlock.npy"
        np.save(os.path.join(args.output, fname), arr)

    n_rays = len(rays['power'])
    print(f"Generated CDL-C rays: {n_rays} rays "
          f"({len(CDL_C_TABLE)} clusters × {N_RAYS_PER_CLUSTER})")
    print(f"  Delay spread: {args.delay_spread} ns")
    print(f"  Delay range: [{rays['tau'].min()*1e9:.1f}, {rays['tau'].max()*1e9:.1f}] ns")
    print(f"  Power range: [{10*np.log10(rays['power'].max()):.1f}, "
          f"{10*np.log10(rays['power'][rays['power']>0].min()):.1f}] dB")
    print(f"  Saved to: {args.output}")


if __name__ == "__main__":
    main()
