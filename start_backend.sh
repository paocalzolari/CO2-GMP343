#!/bin/bash
# start_backend.sh — Avvia il backend CO2 GMP343 via systemd.
# Scrive il PID file per compatibilità con acq-tools.
set -euo pipefail
PID_FILE="$HOME/programs/CO2/co2_backend.pid"

sudo systemctl start co2-logger
sleep 1

# Estrai il PID dal service systemd
PID=$(systemctl show --property MainPID --value co2-logger 2>/dev/null)
if [ -n "$PID" ] && [ "$PID" != "0" ]; then
    echo "$PID" > "$PID_FILE"
    echo "co2-logger avviato (PID $PID)"
else
    echo "WARN: service avviato ma PID non trovato" >&2
fi
