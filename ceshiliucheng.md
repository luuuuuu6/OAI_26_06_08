# Step 1: preflight
preflight

# Step 2: legacy with seed=42
sudo CHANNEL_SEED=42 \
     SRS_ESTIMATOR=legacy ONLY_SNR="10 15 20 25" MAX_FRAMES=100 MIN_SRS_BINS=1 \
     SWEEP_ROOT=/tmp/legacy_seed42 \
     bash /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/run_q4_snr_sweep_v8.sh

# Step 3: preflight
preflight

# Step 4: mmse1d with seed=42 (SAME seed → SAME channel)
sudo CHANNEL_SEED=42 \
     SRS_ESTIMATOR=mmse1d ONLY_SNR="10 15 20 25" MAX_FRAMES=100 MIN_SRS_BINS=1 \
     SWEEP_ROOT=/tmp/mmse1d_seed42 \
     bash /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/run_q4_snr_sweep_v8.sh

# Step 5: 对比
python3 /home/dclcom57/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/plot_nmse_vs_snr_v2.py \
    --legacy-dir /tmp/legacy_seed42 \
    --new-dir /tmp/mmse1d_seed42 \
    --focus-snr 20 --sto-correct per-frame