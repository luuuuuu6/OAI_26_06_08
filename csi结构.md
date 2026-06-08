# CSE-CsiNet v3.3 — How We Modified Existing Building Blocks

> This note is written for a student who is about to build a similar
> stateful AI channel codec for **5G uplink (SRS)**. We describe what
> *prior building blocks* we started from in the **downlink CSI
> feedback** problem, what *modifications* we made on top of each of
> them to arrive at v3.3, and which of those modifications can be
> reused as-is in the uplink setting versus which ones almost
> certainly have to be redesigned.
>
> For the codebase layout / how to run things, see the companion
> document `v33_implementation_guide.md`. For a paper-style
> contribution writeup, see `v33_paper_overview.md`.

---

## 1. The problem we were solving (downlink CSI feedback)

- Sender: the UE estimates `H_t` from DL pilots (NZP-CSI-RS).
- Receiver: the gNB reconstructs `Ĥ_t` from a codeword sent by the UE.
- Slot-to-slot channels are strongly correlated: long-term statistics
  (delay spread, angle spread) are nearly stationary, only the
  *instantaneous* `H` varies fast.
- The 5G NR Type-II codebook decomposes the report into
  W1 (wideband, long-term) + W2 (subband, instantaneous), but it has
  **no time accumulation** — every slot is reported independently.

It is precisely this *missing temporal accumulation* that v3.3 is
designed to fix. Every modification described below is aligned along
that single axis.

---

## 2. The four prior building blocks we started from

| # | Prior work | What it does | Role in v3.3 |
|---|---|---|---|
| B1 | **CsiNet** (Wen et al., 2018) | autoencoder for one wideband `H` | backbone of the cold path (kept as-is) |
| B2 | **5G NR Type-II W1/W2** | (wideband long-term) + (subband instantaneous) decomposition | *philosophy* of the hierarchical residual codec |
| B3 | **FiLM** (Perez et al., 2018) | feature-wise affine modulation `(1+γ)·h + β` | conditioning every hidden layer with the accumulated state |
| B4 | **GRU + EMA** | temporal state accumulators | the three EMA channels (`p`, `R`, `d`) and the GRU latent of the shared state cell |

In addition to these four, three components in v3.3 had no direct
prior counterpart and were essentially built from scratch (sections
3.5, 3.6, 3.7).

---

## 3. Modification of each building block

Each subsection follows the same six-slot template:

- **Plain baseline** — what the prior work originally does
- **What breaks in our setting** — why using it as-is fails for
  downlink CSI feedback
- **Our modification** — what we changed
- **What it buys us** — the resulting capability
- **Note for the uplink student** — what carries over to SRS and what
  to watch out for

### 3.1 CsiNet → ColdAE with Phase-0 solo training and Phase-1 freeze

- *Plain baseline.* `H → enc → quant → dec → Ĥ`. One wideband
  snapshot, single shot.
- *What breaks.* If we let CsiNet train *jointly* with the residual
  heads and the state cell, the cold path silently absorbs both
  "structural" and "instantaneous" content, fights for capacity with
  the W1/W2 heads, and breaks the *baseline-match invariant* (i.e.
  with cold only at `t = 0` we should recover vanilla CsiNet's NMSE).
- *Our modification.* Pull the cold path out as `ColdAE` and use a
  two-phase schedule:
  - **Phase 0**: pre-train `ColdAE` *alone* on per-slot
    instantaneous `H`.
  - **Phase 1**: freeze `ColdAE` and train only the residual heads
    and the state cell.
- *What it buys us.* The cold path keeps a clean meaning: "static,
  time-agnostic baseline". Every later mechanism (router, sync,
  align) is then defined *relative to* that fixed baseline, and
  ablations cleanly separate cold from W1/W2 contribution.
- *Uplink note.* Two-phase training transfers directly. The one
  subtlety in SRS is that hopping/comb patterns mean a single slot
  observes only part of the band — you may need to define the cold
  path's input as a *rolling-fill wideband estimate* rather than the
  raw per-slot SRS measurement.

### 3.2 NR W1/W2 → 3-tier hierarchical residual codec (cold + str + inst)

- *Plain baseline.* W1 (long-term, wideband) + W2 (instantaneous,
  subband). Codebook-index based, not AI-base. W2 is re-measured every
  slot.
- *What breaks.* W1 itself is a *finite codebook index* — there is
  no place to accumulate temporal information into it. W2 is per-slot
  by definition, so accumulation is meaningless there.
- *Our modification.* Keep the *philosophy* of the decomposition,
  replace it with neural networks + uniform quantizers, and add one
  extra tier:
  ```
  H_t  ≈  Ĥ_cold(z_cold)                          ← static (frozen CsiNet)
       +  Ĥ_str (z_str , s_prev)                   ← wideband, accumulated
       +  Σ_k  Ĥ_inst,k(z_inst,k , s_prev)         ← subband, instantaneous
  ```
  Here `s_prev` is the accumulated state. Each tier owns a clean
  semantic axis: cold = no time, str = time + wideband, inst = no
  time + subband.
- *What it buys us.* "Structural information" becomes localized: it
  is exactly what `z_str` represents. Every later mechanism (router,
  sync, align, fingerprint) operates on this clean three-tier
  decomposition.
- *Uplink note.* The natural unit of "subband" in SRS is the
  *hop* or *comb partition*, not a contiguous block of RBs. The fact
  that one SRS slot observes only part of the band must be made
  explicit in the W2-style input (see also 3.3).

### 3.3 FiLM → state-modulated W1/W2 heads, on both sides of the quantizer

- *Plain baseline.* Use an external condition (a question, a style
  vector, ...) to produce `(γ, β)` and apply
  `h ← (1 + γ) ⊙ h + β` at every hidden layer.
- *What breaks.* The most obvious alternative — concatenating
  `s_prev` to the head's input tensor — dilutes the state through
  successive layers and, more importantly, *breaks across the
  quantizer*: the encoder and decoder live on different nodes, so
  any coupling that does not survive quantization is lost.
- *Our modification.* Apply FiLM at *every hidden layer* of both the
  W1 and W2 heads, using `s_prev` as the modulating vector. The same
  `s_prev` modulates the encoder (UE side) and the decoder (gNB side)
  because the two state cells share weights and converge under the
  sync loss (3.4).
- *What it buys us.*
  - The state crosses the quantizer "out of band": it never enters
    the quantized codeword, but it shapes both sides of it.
  - Encoder and decoder agree on the modulation `(γ, β)`, so even
    when the codeword is corrupted by quantization noise, the
    semantic axes remain aligned.
- *Uplink note.* The pattern "use side information to modulate
  hidden activations" carries over directly. In SRS, useful side
  information includes hop id, comb index, RB index, RSRP, time
  since last SRS — embed these and concatenate with `s_prev` as the
  FiLM input.

### 3.4 GRU + EMA → SharedStateCell + L_sync (stitching the two sides)

- *Plain baseline.* Accumulate temporal state on a single node:
  GRU is a learned recurrence, EMA is a closed-form smoother.
- *What breaks.* In downlink the central constraint is "**the same
  state must exist on both UE and gNB**". Sending the state over the
  air is too expensive (hundreds to thousands of bits per slot); not
  sending it lets the two states drift.
- *Our modification.* Three things together:
  1. The two sides each instantiate the *same* cell (same weights,
     two distinct hidden states). At training time both instances
     live in the same graph and back-propagate into the same
     weights.
  2. Each side feeds its cell only with quantities it actually
     observes:
     - UE side: `(p_inst(H_t), R_inst(H_t), d_inst(H_t), c_t, z_code)`
     - gNB side: `(p_inst(Ĥ_t), R_inst(Ĥ_t), d_inst(Ĥ_t), c_t, z_code)`
     The codeword `z` and the router output `c_t` are the only
     shared signals — and those are sent over the air anyway.
  3. A **sync loss** `L_sync = ‖s_ue − s_bs‖²` stitches the two
     latent states during training. At inference time the loss is
     gone, but the weights have already been shaped so that the two
     states track each other from codewords alone.

> **What "structural information" actually means in the code.**
> Three classical second-order statistics are extracted from a
> channel snapshot. We call them `p`, `R`, `d`:
>
> - **`p_inst` — power-delay profile (PDP).** Shape `(Nd,)`. The
>   mean power at each delay tap, computed in the delay domain as
>   `p[d] = mean over (Nr, Nt) of |H[d]|²`. It captures the
>   *delay-domain dispersion* of the multipath cluster (LOS-like vs.
>   long-spread NLOS).
> - **`R_inst` — transmit-side spatial covariance.** Stored as a
>   real-flattened vector of length `2·Nt²`. Computed slot-by-slot
>   as `R = mean over (Nr, Nd) of Hᴴ H`. It captures the
>   *angular dispersion* on the transmit side, i.e. which spatial
>   directions carry energy (the same structure that NR Type-II W1
>   tries to summarize with codebook vectors).
> - **`d_inst` — Doppler-spectrum proxy.** A multi-lag normalised
>   autocorrelation along the time axis,
>   `d[τ] = mean |⟨H_t , H_{t-τ}⟩| / |H|²` over a small set of
>   lags `τ`. It captures *time coherence*, i.e. how fast the channel
>   decorrelates (rough proxy for UE speed × carrier frequency).
>
> In v3.3 the `SharedStateCell` accumulates each of these three
> statistics with its own learnable-rate EMA channel — `p_state`,
> `R_state`, `d_state` — and on top of that maintains a GRU hidden
> `s_lat ∈ R^{d_lat}` that absorbs the more volatile, learned-latent
> aspects of the dynamics. The full state on each side is therefore
> the tuple `(p_state, R_state, d_state, s_lat)`. The same cell
> instance is invoked on UE and gNB sides, with each side feeding
> its own `(p_inst, R_inst, d_inst)` extracted from `H_t` (UE) or
> `Ĥ_t` (gNB).

- *What it buys us.* The state never goes on the air, yet after
  training the two sides remain synchronized. Combining EMA over the
  three classical second-order statistics (`p`, `R`, `d`) with a
  GRU latent lets us accumulate *physical statistics* and a
  *learned latent* in one cell.
- *Uplink note.*
  - In a single-node SRS deployment ("one node observes everything")
    no sync is needed — a plain GRU+EMA is enough.
  - In a *distributed* setup (RU↔DU↔CU fronthaul, multi-RU joint
    processing), the same sync-loss trick applies, just with the
    sender/receiver roles redrawn for the SRS scenario.

### 3.5 [new] Codeword router `c_t`

This component had no direct counterpart in the prior baselines, so
we list it as "added" rather than "modified", but the motivation is
downlink-specific.

- *Why we needed it.* In the first few slots of a session `s_prev`
  is essentially empty, so `Ĥ_str + Ĥ_inst` is dominated by *noise*,
  not signal. Adding it to `Ĥ_cold` makes NMSE *worse*, not better.
- *What we built.* A scalar gate
  `c_t = sigmoid(MLP(|z_cold|, |z_str|, |z_inst|, s_prev))` and
  reconstruction
  `Ĥ_t = Ĥ_cold + c_t · (Ĥ_str + Σ_k Ĥ_inst,k)`. At `t = 0` we
  hard-clamp `c_0 ≡ 0`, which preserves the baseline-match
  invariant: a cold start always degenerates to vanilla CsiNet.
- *What it buys us.* The model is cold-only for the first few slots
  and smoothly opens the residual gate as `s_prev` accumulates,
  inside a single feed-forward graph.
- *Uplink note.* SRS has the same cold-start problem — at the first
  measurement the channel statistics are empty. Reusing the gate
  almost as-is should pay off. Adding SRS-side measurements (RSRP,
  SINR estimates) to the gate's input typically accelerates learning.

### 3.6 [new] Structure fingerprint + matcher (session resumption)

A pragmatic addition driven by operational constraints rather than
a prior building block.

- *Why we needed it.* What happens when the UE wakes up from DRX?
  Always resetting the state pays the cold-start penalty every wake;
  always reusing the previous state injects a stale prior whenever
  the scenario has changed.
- *What we built.* Serialize the state triple
  `(p_state, R_state, s_lat)` into a fingerprint. On wake-up, compare
  it via cosine similarity to the channel statistics observed in the
  first new slot. Above a threshold, restore the state (warm start);
  otherwise reset to zeros (cold start). For the next
  `min_slots_to_trust` slots after a warm start, keep the router
  `c_t` artificially conservative.
- *Uplink note.* Equally relevant to SRS-aperiodic triggers and RRC
  reconfiguration boundaries. The hyperparameters (EMA half-life,
  match threshold, `min_slots_to_trust`) need to be retuned to the
  SRS time scale.

### 3.7 [new] Bit-allocation mode (quality / compress)

- *Why we needed it.* A fixed bit budget conflates two evaluation
  axes: "keep performance, reduce bits" and "keep bits, improve
  performance". We wanted both readings.
- *What we built.*
  - `quality` mode: fixed bit budget, optimize NMSE only.
  - `compress` mode: each head carries a learnable per-element
    keep-mask trained with a Straight-Through Estimator, plus a
    bit-penalty `λ_bits · ‖m‖₁` in the loss. The model itself
    decides how many bits to drop while staying within an NMSE
    floor.
- *Uplink note.* In SRS-fronthaul scenarios, fronthaul bandwidth is
  often the binding constraint; `compress` mode is then the default
  rather than a knob.

---

## 4. "Reused / Modified / Built from scratch" — one-page summary

| Aspect | Reused | Modified | Built from scratch |
|---|---|---|---|
| Compression backbone | CsiNet AE | Phase-0 solo + Phase-1 freeze | — |
| Residual decomposition | NR W1/W2 philosophy | AI-base nets + 3-tier (cold + str + inst) | — |
| Conditioning | FiLM | applied symmetrically across the quantizer | encoder/decoder share the same modulation |
| Temporal state | GRU + EMA | three EMA channels (`p`, `R`, `d`) + GRU latent `s_lat` | shared cell instance + L_sync |
| Gating | (none) | — | StatefulConfidenceHead `c_t` |
| Session resumption | (none) | — | StructureFingerprint + StructureMatcher |
| Bit allocation | (NR fixed) | — | BitKeepMask + bit-penalty (compress mode) |

---

## 5. What to revisit when porting to SRS uplink

### 5.1 Likely safe to reuse almost as-is
- The skeleton of **ColdAE + frozen-cold + FiLM-modulated residual
  heads**.
- The **codeword router `c_t`** and its ramp-up effect.
- The **L_sync, L_align** training trick (whenever there are two
  nodes that should agree on a state).

### 5.2 Will need to be redefined
- **Sender / receiver pair.** Downlink had UE → gNB. In SRS,
  compression makes sense for the *RU → DU* (fronthaul) link, or for
  *RU ↔ RU coordination* (joint processing). This mapping decides
  who holds `s_prev` and where the sync loss lives.
- **Meaning of "subband".** SRS observes only a fraction of the band
  per slot (hop / comb). Replace the W2 subband index with the SRS
  hop index / comb index, and embed the schedule itself as input.
- **Reweighting the Doppler channel for non-uniform cadence.** v3.3
  computes `d_inst` assuming a uniform per-slot cadence (the lag
  axis is in slots). In SRS the cadence varies (periodic /
  semi-persistent / aperiodic) and the time gap between successive
  measurements can change by orders of magnitude. Replace `d_inst`
  with a *time-gap-weighted* normalised autocorrelation — for
  instance, evaluate the underlying lag at the actual elapsed time
  `Δt` rather than at integer slots — and consider adding the SRS
  cadence schedule itself as an extra input to the codeword router.
- **State-cell inputs.** We used `(p_inst, R_inst, d_inst, c_t, z)`. SRS
  measurement chains expose RSRP, SINR estimates, beam ids, etc.
  almost for free — feed them in as additional state inputs.
- **Fingerprint time scale.** EMA half-life, `min_slots_to_trust`,
  match threshold all need to be retuned to the SRS configuration
  (periodic / semi-persistent / aperiodic).

### 5.3 Likely new components to build
- **Multi-port / multi-UE concurrent SRS.** We only handled a single
  UE. SRS multiplexes multiple UEs on the same resource, so the
  codec input/output tensors need an explicit UE axis.
- **Hop / comb pattern awareness.** Analogous to our subband
  position embedding for W2, but with a *non-deterministic*
  time-axis schedule, so the schedule itself becomes part of the
  input.
- **Fronthaul-friendly quantizer.** We used uniform 2-bit. SRS
  fronthaul scenarios usually demand low distortion under tight
  latency budgets, so the quantizer module should likely be
  redesigned rather than reused.
