#!/bin/bash
# stop_backend.sh — Ferma il backend CO2 GMP343 via systemd.
set -euo pipefail
PID_FILE="$HOME/programs/CO2/co2_backend.pid"

sudo systemctl stop co2-logger
rm -f "$PID_FILE"
echo "co2-logger fermato"
