#!/bin/bash
# start_gui.sh — Avvia la GUI monitor CO2 GMP343.
set -euo pipefail
cd "$HOME/programs/CO2"
nohup python3 gmp343_sht31_monitor.py > /tmp/co2monitor.log 2>&1 &
echo "GUI monitor avviata (PID $!)"
