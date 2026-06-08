# D0 Floor Diagnosis — Result Record

> **Usage**: copy this template to `D0_RESULT_<YYYYMMDD>.md` after d0_run.sh
> finishes, then fill in the X / Y / Z placeholders from the script output.
> The decision section is the gate for entering D1 of the masterplan.

| Field | Value |
|-------|-------|
| Date                 | YYYY-MM-DD |
| Operator             | LIULU |
| Sweep root           | `/home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/logs/d0_<TS>/` |
| GT source dir        | `<LOG_BASE>/legacy/snr_20dB/sionna_gt/` |
| Oracle bin path      | `<LOG_BASE>/oracle_gt.bin` |
| Oracle bin size      | XX MB |
| GT quantization scale used | XXX (from `oracle_bin.log` "scale used = ...") |
| MAX_FRAMES per sweep | 100 |
| CHANNEL_SEED         | 42 |
| SNR (dB)             | 20 |
| OAI build commit     | (paste `git -C openairinterface5g_whan rev-parse HEAD` short hash) |

## R8 Sionna mobility check (filled before reading NMSE)

| Field | Value |
|-------|-------|
| Sionna config file inspected | `<path>` |
| UE_SPEED env at sweep        | X m/s |
| Doppler shift estimate (f_d) | X Hz |
| Empirical T_c = 1/f_d        | X ms |
| SRS period (T_SRS)           | 5 / 10 / 20 ms |
| T_c / T_SRS ratio            | NN  (LSL viable iff > 5) |

> **Rule of thumb**: T_c/T_SRS > 5 → keep λ default 0.97; T_c/T_SRS in [1, 5]
> → λ ≤ 0.7; T_c/T_SRS < 1 → LSL pointless, prefer frequency-side approach.

## Measurements (read from `d0_summary.txt`)

| Mode | NMSE p50 LS-aligned (dB) | p10 / p90 | Sample count |
|------|---------------------------|-----------|--------------|
| legacy   | -X.XX | -X.XX / -X.XX | NN |
| passthru | -X.XX | -X.XX / -X.XX | NN |
| oracle   | -X.XX | -X.XX / -X.XX | NN |

## Section decomposition

```
A (LS noise, ADC + pilot LS)        = passthru − oracle = X.XX dB
B (filt8/16 contribution, can be ±) = legacy   − passthru = X.XX dB
C (downstream + eval pipeline)       = oracle             = X.XX dB
```

## Decision (mark ONE)

- [ ] ✅ **GO LSL**       — `oracle ≤ -15 dB`. Floor in estimator. Proceed to D1 reset.
- [ ] ⚠️ **GO with reduced target** — `oracle ∈ (-15, -10]`. LSL ceiling ≈ |oracle|. Proceed to D1 with adjusted DoD §10.1.
- [ ] 🟡 **MARGINAL**     — `oracle ∈ (-10, -7]`. Downstream contributes meaningfully. Discuss with LIULU; LSL upside ≤ 1 dB.
- [ ] ❌ **STOP**         — `oracle > -7 dB`. Downstream is the floor. Do not pursue LSL. Possible re-directions:
    - Audit OAI fixed-point pipeline (c16 → IDFT → time bin) for saturation / quant losses.
    - Reduce eval pipeline error (STO model, slot-pair tolerance, scale alignment).
    - Or report negative result to professor: "OAI receiver-chain noise dominates; SRS estimator changes give negligible improvement."

## Anomalies / notes

> Anything unexpected:
> - oracle p50 ≫ 0 (oracle worse than legacy?) → likely scale mismatch; rerun with `D0_SCALE` half/double
> - oracle abort with "slot N not in GT" warnings → SFN wrap or seed mismatch between sweeps
> - passthru ≈ legacy (B ≈ 0) → freq filt is doing nothing; may indicate sparse-output bug
> - large p90 spread → channel realization sensitive; try larger MAX_FRAMES

## Next-step gate

If decision is GO or GO-reduced:
- [ ] Update `SRS_2D_LRLS_MASTERPLAN.md` §10.1 with concrete `oracle_NMSE_p50_at_SNR20` value
- [ ] Proceed to D1 (Reset to Legacy baseline) per masterplan §11

If decision is MARGINAL:
- [ ] Open discussion thread, list pros/cons of continuing
- [ ] Decision recorded in this file before any code change

If decision is STOP:
- [ ] File a "negative result" note pointing to this record
- [ ] Roll back any speculative LSL prep (none yet at D0 stage, so just don't proceed)
- [ ] Talk to professor with the section-decomposition table

---

**Filled by**: LIULU
**Reviewed**: AI co-pilot
**Date**: YYYY-MM-DD
