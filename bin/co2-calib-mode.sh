#!/bin/bash
# co2-calib-mode.sh
# Procedura calibrazione GMP343:
#   1. ferma il systemd service co2-logger (richiede sudo)
#   2. avvia calib-GMP343-logger.py (GUI per togglare flag measure/calib)
#   3. quando l'utente chiude la GUI, riavvia il systemd service
#
# Eseguito tipicamente da CO2-Calib.desktop (con Terminal=true).

set -e

CO2_DIR="/home/misura/programs/CO2"
CALIB_PY="$CO2_DIR/calib-GMP343-logger.py"

echo "==============================================="
echo "  CO2 / GMP343 — Modalità calibrazione"
echo "==============================================="
echo

if ! systemctl is-active --quiet co2-logger; then
    echo "AVVISO: il systemd service co2-logger NON è attivo."
    echo "Stato corrente:"
    systemctl status co2-logger --no-pager | head -5 || true
    echo
    read -p "Continuo comunque? [y/N] " ans
    if [[ "$ans" != "y" && "$ans" != "Y" ]]; then
        echo "Annullato."
        exit 1
    fi
fi

echo "==> Step 1/3: Stop systemd service co2-logger"
echo "    (potrebbe chiedere la password sudo)"
sudo systemctl stop co2-logger
sleep 1
echo "    service fermato."
echo

echo "==> Step 2/3: Avvio GUI calibrazione"
echo "    Usa la GUI per togglare il flag measure/calib durante la sessione."
echo "    Chiudi la finestra quando hai finito per riprendere l'acquisizione."
echo
cd "$CO2_DIR"
python3 "$CALIB_PY" || true
echo
echo "    GUI calibrazione chiusa."
echo

echo "==> Step 3/3: Restart systemd service co2-logger"
sudo systemctl start co2-logger
sleep 2
echo
echo "==> Stato del service dopo restart:"
systemctl status co2-logger --no-pager | head -8 || true

echo
echo "==============================================="
echo "  Calibrazione completata."
echo "==============================================="
