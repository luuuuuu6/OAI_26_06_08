# Digital Twin Channel Data — Unified Output Format Specification

| Field | Value |
|-------|-------|
| **Version** | 0.1 (draft) |
| **Date** | 2026-05-25 |
| **Scope** | Defines the common output tuple `(S, H)` for both DL CSI and UL SRS pipelines |
| **Audience** | Junsu (DL), Liu (UL), Minji (DB/RAN Twin), Professor |

---

## 1. Design Principle

Both DL and UL pipelines output a structure tuple `(S, H)` per extraction interval.
The **method** differs (DL: neural codec; UL: signal processing), but the
**data-layer schema** is kept consistent so that Minji's database and RAN Twin
can ingest both without format-specific branching.

---

## 2. Structure S — Shared Fields

These fields appear in both `S_dl` and `S_ul`.
Names are intentionally aligned; physical meaning may differ by link direction.

| Field | Type | Shape | Unit | Description |
|-------|------|-------|------|-------------|
| `p_inst` | real ndarray | `(N_tap,)` | linear power | Instantaneous power-delay profile (current frame) |
| `p_state` | real ndarray | `(N_tap,)` | linear power | EMA-accumulated PDP (long-term delay structure) |
| `d_inst` | real ndarray | `(L_lag,)` | dimensionless [0,1] | Multi-lag normalised temporal autocorrelation (current frame) |
| `d_state` | real ndarray | `(L_lag,)` | dimensionless [0,1] | EMA-accumulated time-coherence proxy |
| `confidence` | float | scalar | [0, 1] | Reliability gate (cold-start ramp / data-quality indicator) |

### Spatial covariance — direction-aware naming

Because DL and UL observe different antenna sides, we use explicit naming:

| Field | DL (Junsu) | UL (Liu) |
|-------|-----------|----------|
| Instantaneous | `R_tx_inst` — TX-side covariance | `R_rx_inst` — RX-side covariance |
| Accumulated | `R_tx_state` — TX-side accumulated | `R_rx_state` — RX-side accumulated |
| Type | complex ndarray | complex ndarray |
| Shape | `(N_tx, N_tx)` | `(N_rx, N_rx)` |

Both are spatial covariance matrices; the prefix (`tx` / `rx`) clarifies which
antenna array is characterised.

---

## 3. Structure S — UL-Specific Fields

These fields are produced only by the UL SRS pipeline.
DL does not need them (or computes equivalents internally).

| Field | Type | Shape | Unit | Description |
|-------|------|-------|------|-------------|
| `rsrp` | float | scalar | linear power | Reference signal received power |
| `snr_db` | float | scalar | dB | SNR estimate (from even-odd pair) |
| `doppler_hz` | float | scalar | Hz | Scalar Doppler frequency (from rot_angle) |
| `speed_ms` | float | scalar | m/s | UE speed estimate (`fd · c / fc`) |
| `ds_rms_s` | float | scalar | seconds | RMS delay spread |
| `sv` | real ndarray | `(min(Nr,Nt),)` | linear | Wideband-average singular values (MIMO rank) |

### Compatibility aliases (UL only)

To avoid breaking existing code that reads the old field names:

| Alias | Points to |
|-------|-----------|
| `pdp` | same object as `p_inst` |
| `R_rx` | same object as `R_rx_inst` |

---

## 4. Compressed Channel H

| Field | Type | Shape | Description |
|-------|------|-------|-------------|
| `h_taps` | complex ndarray | `(N_rx, N_tx, 2·N_tap)` or `(2·N_tap,)` | Delay-domain kept taps — **actual compressed payload for storage** |
| `H_c` | complex ndarray | `(N_rx, N_tx, N_sc)` or `(N_sc,)` | Full-band DFT-reconstructed H — for evaluation / plotting only |

### Relationship

```
h_taps = concat(h_time[..., :N_tap], h_time[..., -N_tap:])      # compress
H_c    = FFT(zero_pad(h_taps, N_sc))                              # reconstruct
```

`h_taps` is what gets stored in the database.
`H_c` is derived on-demand for NMSE evaluation or downstream processing.

### DL equivalent

| DL field | Meaning |
|----------|---------|
| `z_code` | Quantised codeword (neural encoder output) |
| `H_dl` | `Ĥ_cold + c_t · (Ĥ_str + Ĥ_inst)` — reconstructed H |

The DL compressed representation is the quantised codeword `z_code`;
the UL compressed representation is `h_taps`.
Both can be decoded/reconstructed into a full-band H for downstream use.

---

## 5. EMA Accumulation Convention

Both DL and UL accumulate `p`, `R`, `d` with per-channel EMA:

```
x_state[t] = (1 − α) · x_state[t-1] + α · x_inst[t]
```

| | DL (Junsu) | UL (Liu) |
|--|-----------|----------|
| Rate type | Learnable (backprop) | Fixed (signal-processing) |
| Suggested defaults | — | α_p=0.10, α_R=0.05, α_d=0.15 |
| Initialisation | Zero / learned init | First-frame copy |

The physical semantics are identical; only the rate-selection mechanism differs.

---

## 6. Doppler Autocorrelation Convention

```
d_inst[τ] = |⟨H_t, H_{t-τ}⟩| / sqrt(‖H_t‖² · ‖H_{t-τ}‖²)
```

- Bilateral normalisation (not single-sided `/ ‖H_t‖²`).
- `τ` is in units of SRS periods (integer lag index).
- Default `L_lag = 8`.
- For non-uniform SRS cadence, the actual elapsed time `Δt` per lag
  should be stored alongside `d_inst` if needed (future extension).

---

## 7. Storage Format Recommendation (for Minji)

### Per-extraction record

```python
record = {
    "timestamp_ms": int,         # wall-clock or slot-based timestamp
    "link_dir": "ul" | "dl",     # which pipeline produced this
    "ue_id": int,                # UE identifier
    "S": { ... },                # structure dict (all fields above)
    "h_taps": ndarray,           # compressed channel payload
}
```

### File format options

| Option | Pros | Cons |
|--------|------|------|
| NPZ (per-session) | Fast numpy I/O, compact | No streaming append |
| HDF5 | Streaming append, typed datasets | Heavier dependency |
| JSON metadata + binary | Human-readable header | More files |

Recommendation: start with **NPZ per session** (simplest), migrate to HDF5
when streaming / multi-UE is needed.

### Extraction interval

| Scenario | Suggested `extract_every` |
|----------|--------------------------|
| Offline analysis | 1 (every SRS frame) |
| Real-time DB | 10–50 (every 50–250 ms at 5 ms SRS period) |
| Storage budget | Tune to target MB/hour |

---

## 8. Dimension Defaults

| Symbol | Typical value | Meaning |
|--------|---------------|---------|
| `N_sc` | 1248 (OAI active SC) or 4096 (full FFT) | Subcarriers |
| `N_tap` | 64 | Delay taps kept (≈ 2 μs at 30 kHz SCS) |
| `L_lag` | 8 | Temporal autocorrelation lags |
| `N_rx` | 2 | gNB receive antennas |
| `N_tx` | 2 | UE transmit antennas (SRS ports) |
| SCS | 30 kHz | Subcarrier spacing |
| SRS period | 5 ms (160 slots) | Typical periodic SRS config |
