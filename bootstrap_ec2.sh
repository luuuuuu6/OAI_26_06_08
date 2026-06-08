#!/usr/bin/env bash
# ============================================================
# bootstrap_ec2.sh — one-click EC2 setup for OAI_luuuuuu
# ------------------------------------------------------------
# Gets a fresh Ubuntu 22.04 EC2 from "bare metal" to a state
# where you can `python compare_nmse.py --help` (and optionally
# run Sionna simulation or compile OAI).
#
# Assumptions
#   • Running as user `ubuntu` on Ubuntu 22.04 LTS.
#   • AWS credentials will be configured interactively after apt.
#   • Script is idempotent — re-running is safe.
#
# Usage
#   # Minimum: analysis-only (CPU instance like c6i.4xlarge)
#   bash bootstrap_ec2.sh
#
#   # Add Sionna / TensorFlow (GPU instance like g5.xlarge)
#   bash bootstrap_ec2.sh --with-sim
#
#   # Add OAI build toolchain (c6i.8xlarge or bigger)
#   bash bootstrap_ec2.sh --with-oai
#
#   # Everything
#   bash bootstrap_ec2.sh --with-sim --with-oai
# ============================================================

set -euo pipefail

WITH_SIM=false
WITH_OAI=false
REPO_URL="https://github.com/luuuuuu6/OAI_luuuuuu.git"
S3_BUCKET="s3://oai-luuuuuu"
AWS_REGION="ap-northeast-2"

for arg in "$@"; do
  case "$arg" in
    --with-sim) WITH_SIM=true ;;
    --with-oai) WITH_OAI=true ;;
    -h|--help)
      sed -n '2,27p' "$0"
      exit 0
      ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

log() { printf "\n\033[1;36m[bootstrap]\033[0m %s\n" "$*"; }

# ------------------------------------------------------------
# 0. Sanity checks
# ------------------------------------------------------------
log "Checking OS ..."
if ! grep -q "Ubuntu 22.04" /etc/os-release 2>/dev/null; then
  echo "WARNING: this script targets Ubuntu 22.04. Continuing anyway ..."
fi

# ------------------------------------------------------------
# 1. Base apt packages
# ------------------------------------------------------------
log "Installing base apt packages (git, python, awscli, tmux, ...)"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git git-lfs build-essential cmake rsync htop tmux \
  python3.10 python3.10-venv python3.10-dev python3-pip \
  awscli unzip ca-certificates curl

# ------------------------------------------------------------
# 2. AWS credentials
# ------------------------------------------------------------
if ! aws sts get-caller-identity >/dev/null 2>&1; then
  log "AWS CLI is not yet configured. Run 'aws configure' now:"
  echo "   • AWS Access Key ID / Secret Key  (IAM user luuuuuu-dev)"
  echo "   • Default region: ${AWS_REGION}"
  echo "   • Default output format: json"
  aws configure
else
  log "AWS CLI already configured as $(aws sts get-caller-identity --query Arn --output text)"
fi

# speed up large s3 sync
aws configure set default.s3.max_concurrent_requests 20
aws configure set default.s3.max_queue_size 10000

# ------------------------------------------------------------
# 3. Clone repo (with OAI submodule)
# ------------------------------------------------------------
REPO_DIR="$HOME/OAI_luuuuuu"
if [ ! -d "$REPO_DIR/.git" ]; then
  log "Cloning repo with submodules into $REPO_DIR ..."
  git clone --recurse-submodules "$REPO_URL" "$REPO_DIR"
else
  log "Repo already exists at $REPO_DIR — pulling latest ..."
  cd "$REPO_DIR"
  git pull --ff-only
  git submodule update --init --recursive
fi
cd "$REPO_DIR"

# ------------------------------------------------------------
# 4. Python venv + analysis requirements
# ------------------------------------------------------------
VENV_DIR="$REPO_DIR/DevChannelProxyJIN/vRAN_Socket/venv_lu"
if [ ! -d "$VENV_DIR" ]; then
  log "Creating Python venv at $VENV_DIR ..."
  python3.10 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"
pip install --upgrade pip wheel

log "Installing analysis requirements (requirements.txt) ..."
pip install -r "$REPO_DIR/DevChannelProxyJIN/vRAN_Socket/requirements.txt"

# ------------------------------------------------------------
# 5. Optional: Sionna / TensorFlow (GPU)
# ------------------------------------------------------------
if [ "$WITH_SIM" = true ]; then
  log "Installing simulation requirements (Sionna + TF[and-cuda]) ..."
  pip install -r "$REPO_DIR/DevChannelProxyJIN/vRAN_Socket/requirements-sim.txt"

  log "Checking GPU availability ..."
  python - <<'PY'
import tensorflow as tf
gpus = tf.config.list_physical_devices('GPU')
print("GPU(s) visible to TensorFlow:", gpus or "NONE (CPU only)")
PY
fi

# ------------------------------------------------------------
# 6. Optional: OAI build toolchain
# ------------------------------------------------------------
if [ "$WITH_OAI" = true ]; then
  log "Installing OAI build deps via the project's own build_oai -I ..."
  cd "$REPO_DIR/DevChannelProxyJIN/openairinterface5g_whan/cmake_targets"
  sudo ./build_oai -I
  log "OAI deps installed. To actually build gNB/UE run:"
  echo "   cd $REPO_DIR/DevChannelProxyJIN/openairinterface5g_whan/cmake_targets"
  echo "   ./build_oai --gNB --nrUE -w SIMU --ninja"
fi

# ------------------------------------------------------------
# 7. (Optional) pull experiment data from S3 on demand
# ------------------------------------------------------------
log "S3 bucket $S3_BUCKET summary:"
aws s3 ls "$S3_BUCKET/" || true
cat <<EOF

Data is NOT auto-downloaded to keep the bootstrap fast.
Pull only what you need, e.g.:
  aws s3 sync $S3_BUCKET/raw/saved_rays_data/ \\
              $REPO_DIR/DevChannelProxyJIN/vRAN_Socket/saved_rays_data/
  aws s3 sync $S3_BUCKET/raw/cfr/ \\
              $REPO_DIR/DevChannelProxyJIN/vRAN_Socket/cfr/
EOF

# ------------------------------------------------------------
# 8. Done
# ------------------------------------------------------------
log "Bootstrap complete."
echo "--------------------------------------------------------"
echo "Next steps:"
echo "  1. cd $REPO_DIR/DevChannelProxyJIN/vRAN_Socket"
echo "  2. source venv_lu/bin/activate"
echo "  3. python compare_nmse.py --help    # smoke test"
echo "  4. tmux new -s run                  # long-running jobs"
echo "--------------------------------------------------------"
