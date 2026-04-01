#!/bin/bash
# =============================================================================
# build.sh — Compila gui_integrated_v11.py e gmp343_logger-7.py in eseguibili
#            binari con PyInstaller (codice sorgente non leggibile)
#
# Eseguire SUL RASPBERRY PI dalla cartella ~/programs/CO2/
#
# Risultato:
#   dist/co2-monitor/co2-monitor   ← eseguibile GUI
#   dist/co2-logger/co2-logger     ← eseguibile logger
#
# Uso:
#   cd ~/programs/CO2
#   bash build.sh
# =============================================================================

set -e
cd "$(dirname "$0")"

echo "=================================================="
echo " GMP343 CO2 — Build eseguibili binari"
echo "=================================================="
echo ""

# ── 1. Verifica PyInstaller ──────────────────────────────────────────────────
if ! python3 -m PyInstaller --version &>/dev/null; then
    echo "PyInstaller non trovato. Installazione..."
    pip3 install pyinstaller --break-system-packages
fi
echo "✓ PyInstaller: $(python3 -m PyInstaller --version)"

# ── 2. Pulisce build precedenti ──────────────────────────────────────────────
echo ""
echo "Pulizia build precedenti..."
rm -rf build dist __pycache__ *.spec
echo "✓ Pulito"

# ── 3. Build GUI (gui_integrated_v11.py) ─────────────────────────────────────
echo ""
echo "── Build co2-monitor (GUI) ──────────────────────"
python3 -m PyInstaller \
    --name co2-monitor \
    --onedir \
    --noconsole \
    --clean \
    --add-data "config:config" \
    --add-data "gmp343_sensor.png:." \
    --hidden-import PyQt5 \
    --hidden-import PyQt5.QtWidgets \
    --hidden-import PyQt5.QtCore \
    --hidden-import PyQt5.QtGui \
    --hidden-import matplotlib \
    --hidden-import matplotlib.backends.backend_qt5agg \
    --hidden-import numpy \
    --hidden-import serial \
    --hidden-import serial.tools.list_ports \
    --hidden-import astral \
    --hidden-import astral.sun \
    --hidden-import pytz \
    gui_integrated_v11.py

echo "✓ co2-monitor compilato"

# ── 4. Build Logger (gmp343_logger-7.py) ─────────────────────────────────────
echo ""
echo "── Build co2-logger (Logger) ────────────────────"
python3 -m PyInstaller \
    --name co2-logger \
    --onedir \
    --console \
    --clean \
    --add-data "config:config" \
    --hidden-import serial \
    --hidden-import serial.tools.list_ports \
    gmp343_logger-7.py

echo "✓ co2-logger compilato"

# ── 5. Pulizia file intermedi ─────────────────────────────────────────────────
echo ""
echo "Pulizia file intermedi..."
rm -rf build __pycache__ co2-monitor.spec co2-logger.spec
echo "✓ Pulito"

# ── 6. Riepilogo ──────────────────────────────────────────────────────────────
echo ""
echo "=================================================="
echo " Build completato!"
echo "=================================================="
echo ""
echo "Eseguibili in:"
echo "  dist/co2-monitor/co2-monitor"
echo "  dist/co2-logger/co2-logger"
echo ""
echo "Test rapido:"
echo "  ./dist/co2-logger/co2-logger &"
echo "  ./dist/co2-monitor/co2-monitor"
echo ""
echo "Per installare in /usr/local/bin (avvio da qualsiasi dir):"
echo "  sudo cp -r dist/co2-monitor /opt/"
echo "  sudo cp -r dist/co2-logger  /opt/"
echo "  sudo ln -sf /opt/co2-monitor/co2-monitor /usr/local/bin/co2-monitor"
echo "  sudo ln -sf /opt/co2-logger/co2-logger   /usr/local/bin/co2-logger"
