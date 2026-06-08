#!/usr/bin/env bash
# ============================================================
# sync_to_60.sh
# Sync OAI_luuuuuu from 165.132.192.78 (dclserver78)
#                  to 165.132.121.60 (dclcom57)
# ------------------------------------------------------------
# Usage:
#   ./sync_to_60.sh              # real sync
#   ./sync_to_60.sh --dry-run    # preview only, no transfer
#   ./sync_to_60.sh --check      # checksum verify both sides
# ============================================================

set -euo pipefail

SRC="/home/dclserver78/OAI_luuuuuu/"
DST_USER="dclcom57"
DST_HOST="165.132.121.60"
DST_PATH="~/OAI_luuuuuu/"
DST="${DST_USER}@${DST_HOST}:${DST_PATH}"

EXCLUDES=(
  --exclude='__pycache__/'
  --exclude='*.pyc'
  --exclude='*.pyo'
  --exclude='.pytest_cache/'
  --exclude='.mypy_cache/'
  --exclude='.ipynb_checkpoints/'
  --exclude='*.swp'
  --exclude='*.tmp'
  --exclude='core'
  --exclude='.DS_Store'
)

MODE="${1:-sync}"

color()  { printf "\033[1;36m%s\033[0m\n" "$*"; }
green()  { printf "\033[1;32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[1;33m%s\033[0m\n" "$*"; }

case "$MODE" in
  --dry-run|-n)
    color "[DRY RUN] Previewing changes from ${SRC} -> ${DST}"
    rsync -avzP -n "${EXCLUDES[@]}" "$SRC" "$DST"
    yellow "No files were transferred (dry-run)."
    ;;

  --check|-c)
    color "[CHECK] Verifying both sides with checksum (no transfer)"
    rsync -avn --checksum "${EXCLUDES[@]}" "$SRC" "$DST"
    green "If the file list above is empty, the two sides are identical."
    ;;

  sync|"")
    color "[SYNC] ${SRC} -> ${DST}"
    rsync -avzP "${EXCLUDES[@]}" "$SRC" "$DST"
    green "Sync completed successfully."
    ;;

  -h|--help)
    cat <<EOF
Usage: $(basename "$0") [option]

Options:
  (no option)   Real sync (compressed + partial resume + progress)
  --dry-run     Preview what would change, without transferring
  --check       Checksum verify both sides match exactly
  -h, --help    Show this help

Source : ${SRC}
Target : ${DST}
EOF
    ;;

  *)
    echo "Unknown option: $MODE"
    echo "Run '$(basename "$0") --help' for usage."
    exit 1
    ;;
esac
