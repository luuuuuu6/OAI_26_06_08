#!/bin/bash
# run_sweep_with_iperf.sh — sweep + iperf UL traffic injector
#
# Why: at high SNR with no UL data, OAI scheduler skips SRS slots
# (PUCCH-only mode), so SRS bins stay empty. Injecting a 5 Mbps UDP
# UL flow keeps PUSCH busy, which makes the scheduler issue SRS.
#
# Usage:
#   sudo SRS_ESTIMATOR=mmse1d ONLY_SNR="25" \
#        SWEEP_ROOT=/tmp/snrsweep_pdpR \
#        bash run_sweep_with_iperf.sh

set -u

EXT_DN_IP="${EXT_DN_IP:-$(sudo docker inspect oai-ext-dn --format '{{ range .NetworkSettings.Networks }}{{ .IPAddress }}{{ end }}')}"
IPERF_BPS="${IPERF_BPS:-5M}"
IPERF_DUR="${IPERF_DUR:-1200}"     # max iperf runtime (long enough to cover sweep)
TUN_IFACE="${TUN_IFACE:-oaitun_ue1}"
SWEEP_SCRIPT="/home/dclcom61/OAI_luuuuuu/DevChannelProxyJIN/vRAN_Socket/G1C_MultiUE_MIMO_Channel_Proxy/run_q4_snr_sweep_v8.sh"

echo "[wrapper] ext-dn IP   : $EXT_DN_IP"
echo "[wrapper] iperf rate  : $IPERF_BPS UDP"
echo "[wrapper] iperf max   : ${IPERF_DUR}s"

# Verify oai-ext-dn is running before we try to use it
if ! sudo docker ps --format '{{.Names}}' | grep -qx oai-ext-dn; then
    echo "[wrapper] ERROR: oai-ext-dn container is NOT running."
    echo "[wrapper]        try: sudo docker start oai-ext-dn"
    exit 1
fi

# Verify iperf3 is installed in the container
if ! sudo docker exec oai-ext-dn which iperf3 >/dev/null 2>&1; then
    echo "[wrapper] ERROR: iperf3 is not installed in oai-ext-dn."
    echo "[wrapper]        try: sudo docker exec oai-ext-dn bash -c 'apt update && apt install -y iperf3'"
    exit 1
fi

# NOTE: oai-ext-dn (trf-gen-cn5g) typically runs `iperf3 -s` as its PID 1
# entrypoint. NEVER pkill it — that kills the container.
# Strategy: detect existing iperf3 listener; only start one if absent.
IPERF_PORT="${IPERF_PORT:-5201}"
if sudo docker exec oai-ext-dn ss -tnl 2>/dev/null | grep -q ":${IPERF_PORT} "; then
    echo "[wrapper] iperf3 server already listening on :${IPERF_PORT} (likely PID 1 entrypoint)"
elif sudo docker exec oai-ext-dn pgrep -f "iperf3 -s" >/dev/null 2>&1; then
    echo "[wrapper] iperf3 server already running (port unknown — using ${IPERF_PORT})"
else
    echo "[wrapper] no iperf3 server detected; starting one on :${IPERF_PORT}"
    if ! sudo docker exec -d oai-ext-dn iperf3 -s -p "$IPERF_PORT"; then
        echo "[wrapper] ERROR: failed to start iperf3 server in oai-ext-dn"
        exit 1
    fi
    sleep 2
    if ! sudo docker exec oai-ext-dn pgrep -f "iperf3 -s" >/dev/null 2>&1; then
        echo "[wrapper] ERROR: iperf3 server died immediately"
        exit 1
    fi
fi

# Launch the sweep in background (it inherits the env vars passed to us)
echo "[wrapper] starting sweep..."
bash "$SWEEP_SCRIPT" &
SWEEP_PID=$!

# Wait for tun interface to come up (UE attach + PDU session done)
UE_IP=""
for i in $(seq 1 60); do
    sleep 5
    if ip addr show "$TUN_IFACE" 2>/dev/null | grep -q "inet "; then
        UE_IP=$(ip -4 addr show "$TUN_IFACE" | grep -oP 'inet \K[0-9.]+')
        echo "[wrapper] tun $TUN_IFACE up at $UE_IP (after ${i}*5=$((i*5))s)"
        break
    fi
done

if [ -z "$UE_IP" ]; then
    echo "[wrapper] WARN: $TUN_IFACE never came up — sweep continues without iperf"
    wait "$SWEEP_PID"
    exit $?
fi

# Quick reachability test
echo -n "[wrapper] ping test: "
if ping -I "$UE_IP" -c 2 -W 2 "$EXT_DN_IP" >/dev/null 2>&1; then
    echo "OK"
else
    echo "FAIL — iperf will likely also fail; proceeding anyway"
fi

# Inject UL traffic
echo "[wrapper] launching iperf3 UL: $UE_IP -> $EXT_DN_IP:$IPERF_PORT at $IPERF_BPS UDP"
iperf3 -c "$EXT_DN_IP" -B "$UE_IP" -u -b "$IPERF_BPS" -t "$IPERF_DUR" -p "$IPERF_PORT" \
       --logfile /tmp/iperf_ul_$$.log &
IPERF_PID=$!

# Wait for sweep to finish, then stop iperf
wait "$SWEEP_PID"
SWEEP_RC=$?

if kill -0 "$IPERF_PID" 2>/dev/null; then
    kill "$IPERF_PID" 2>/dev/null
    sleep 1
    kill -9 "$IPERF_PID" 2>/dev/null
fi
# DO NOT pkill iperf3 in ext-dn — it's the container's PID 1.

echo "[wrapper] sweep RC=$SWEEP_RC"
echo "[wrapper] iperf log: /tmp/iperf_ul_$$.log (last 20 lines):"
tail -20 /tmp/iperf_ul_$$.log 2>/dev/null

exit $SWEEP_RC
