#!/bin/bash
# Installazione del systemd service co2-logger.service
#
# Cosa fa:
#   1. Copia co2-logger.service in /etc/systemd/system/
#   2. systemctl daemon-reload
#   3. Disabilita l'autostart di Vaisala-logger.desktop (per evitare conflitto
#      sulla porta seriale: il backend e il logger di calibrazione non possono
#      girare insieme — entrambi tengono /dev/gmp343 e scrivono sugli stessi file)
#   4. Stoppa eventuale gmp343_sht31_calib.py in esecuzione
#   5. systemctl enable --now co2-logger.service
#
# Richiede sudo. Eseguire da: programs/CO2/
#   sudo bash autoexec/install-systemd.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNIT_SRC="$SCRIPT_DIR/co2-logger.service"
UNIT_DST="/etc/systemd/system/co2-logger.service"
AUTOSTART_CALIB="/home/misura/.config/autostart/Vaisala-logger.desktop"

if [ "$EUID" -ne 0 ]; then
    echo "ERRORE: questo script va eseguito con sudo." >&2
    echo "  sudo bash $0" >&2
    exit 1
fi

if [ ! -f "$UNIT_SRC" ]; then
    echo "ERRORE: $UNIT_SRC non trovato" >&2
    exit 1
fi

echo "==> Copio $UNIT_SRC -> $UNIT_DST"
cp "$UNIT_SRC" "$UNIT_DST"
chmod 644 "$UNIT_DST"

echo "==> systemctl daemon-reload"
systemctl daemon-reload

if [ -f "$AUTOSTART_CALIB" ]; then
    echo "==> Disabilito autostart calibrazione: $AUTOSTART_CALIB -> .disabled"
    mv "$AUTOSTART_CALIB" "${AUTOSTART_CALIB}.disabled"
else
    echo "==> Autostart calibrazione già assente, ok"
fi

# Stop di eventuale processo gmp343_sht31_calib.py in esecuzione
if pgrep -f "gmp343_sht31_calib.py" > /dev/null; then
    echo "==> Stop processo gmp343_sht31_calib.py corrente"
    pkill -TERM -f "gmp343_sht31_calib.py" || true
    sleep 2
    if pgrep -f "gmp343_sht31_calib.py" > /dev/null; then
        echo "    (forzo SIGKILL)"
        pkill -KILL -f "gmp343_sht31_calib.py" || true
        sleep 1
    fi
fi

echo "==> systemctl enable --now co2-logger.service"
systemctl enable --now co2-logger.service

sleep 2
echo
echo "==> Stato del service:"
systemctl --no-pager status co2-logger.service || true

echo
echo "==> Fatto. Comandi utili:"
echo "  systemctl status co2-logger          # stato"
echo "  journalctl -u co2-logger -f          # log in live"
echo "  journalctl -u co2-logger --since today"
echo "  sudo systemctl restart co2-logger    # restart manuale"
echo
echo "Per una sessione di calibrazione:"
echo "  sudo systemctl stop co2-logger"
echo "  cd /home/misura/programs/CO2 && python3 gmp343_sht31_calib.py"
echo "  sudo systemctl start co2-logger"
