#!/bin/bash
# co2-status.sh
# Mostra lo stato del systemd service co2-logger e i log live.
# Eseguito tipicamente da CO2-Status.desktop (con Terminal=true).

clear
echo "==============================================="
echo "  CO2 / GMP343 — Stato sistema"
echo "==============================================="
echo

echo "## Hardware"
if [ -e /dev/gmp343 ]; then
    ls -la /dev/gmp343
    echo "  -> sensore presente"
else
    echo "  /dev/gmp343 ASSENTE — sensore non collegato o udev non funziona"
fi
echo

echo "## Systemd service co2-logger"
systemctl status co2-logger --no-pager | head -10 || true
echo

echo "## Ultimi file giornalieri scritti"
ls -lh ~/data/carbocap343_*_min.raw 2>/dev/null | tail -5 || echo "  nessun file _min trovato in ~/data/"
echo

echo "## Processi attivi"
pgrep -af "gmp343_sht31_(logger|calib|monitor)" || echo "  nessuno"
echo

echo "==============================================="
echo "  Log live (Ctrl-C per uscire):"
echo "==============================================="
journalctl -u co2-logger -f --no-pager
