#!/bin/bash
# install_libraries.sh
# Installa le librerie Python necessarie per il sistema di acquisizione CO2 GMP343
# Raspberry Pi OS (Debian Bookworm) - Aprile 2026

set -e

echo "========================================"
echo " CO2-GMP343 - Installazione librerie"
echo "========================================"
echo ""

# Verifica Python3 e pip3
if ! command -v python3 &>/dev/null; then
    echo "[ERRORE] Python3 non trovato. Installazione..."
    sudo apt-get update && sudo apt-get install -y python3 python3-pip
fi

if ! command -v pip3 &>/dev/null; then
    echo "Installazione pip3..."
    sudo apt-get install -y python3-pip
fi

echo "[1/5] Aggiornamento pip..."
pip3 install --upgrade pip --break-system-packages 2>/dev/null || \
    python3 -m pip install --upgrade pip

echo ""
echo "[2/5] Installazione PyQt5 (GUI framework)..."
# Su Raspberry Pi preferire i pacchetti apt per evitare compilazione
if python3 -c "import PyQt5" 2>/dev/null; then
    echo "  PyQt5 gia installato."
else
    sudo apt-get install -y python3-pyqt5 2>/dev/null || \
        pip3 install PyQt5 --break-system-packages
fi

echo ""
echo "[3/5] Installazione pyserial (comunicazione seriale)..."
pip3 install pyserial --break-system-packages 2>/dev/null || \
    pip3 install pyserial

echo ""
echo "[4/5] Installazione matplotlib e numpy (grafici e calcolo numerico)..."
# Su Raspberry Pi preferire i pacchetti apt
if python3 -c "import matplotlib" 2>/dev/null; then
    echo "  matplotlib gia installato."
else
    sudo apt-get install -y python3-matplotlib 2>/dev/null || \
        pip3 install matplotlib --break-system-packages
fi

if python3 -c "import numpy" 2>/dev/null; then
    echo "  numpy gia installato."
else
    sudo apt-get install -y python3-numpy 2>/dev/null || \
        pip3 install numpy --break-system-packages
fi

echo ""
echo "[5/5] Installazione astral e pytz (calcolo alba/tramonto, opzionale)..."
# astral 2.2 disponibile su piwheels per Raspberry Pi
pip3 install "astral==2.2" pytz --break-system-packages 2>/dev/null || \
    pip3 install "astral==2.2" pytz --extra-index-url https://www.piwheels.org/simple 2>/dev/null || \
    echo "  [AVVISO] astral/pytz non installati - le funzioni alba/tramonto saranno disabilitate."

echo ""
echo "========================================"
echo " Verifica installazione"
echo "========================================"
echo ""

python3 - <<'PYCHECK'
libs = {
    "PyQt5":      "PyQt5",
    "serial":     "pyserial",
    "matplotlib": "matplotlib",
    "numpy":      "numpy",
    "astral":     "astral (opzionale)",
    "pytz":       "pytz (opzionale)",
}
ok = True
for mod, pkg in libs.items():
    try:
        __import__(mod)
        print(f"  [OK]  {pkg}")
    except ImportError:
        if "opzionale" in pkg:
            print(f"  [--]  {pkg} - non disponibile")
        else:
            print(f"  [ERR] {pkg} - NON TROVATO")
            ok = False

print()
if ok:
    print("Tutte le librerie obbligatorie sono installate.")
else:
    print("ATTENZIONE: alcune librerie obbligatorie mancano.")
    exit(1)
PYCHECK

echo ""
echo "Installazione completata."
echo ""
echo "Per avviare il sistema:"
echo "  cd ~/programs/CO2"
echo "  python3 gmp343_logger-9.py &   # logger in background"
echo "  python3 gui_integrated_v13.py  # monitor grafico"
echo ""
