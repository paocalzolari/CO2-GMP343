#!/bin/bash
# Avvio della GUI CO2 Monitor con tracemalloc attivato.
#
# Lanciare a mano quando si vuole indagare la crescita di RSS:
#   bin/co2-monitor-debug.sh
#
# Logga ogni 30 min i top-10 frame allocator in:
#   /tmp/co2-monitor-tracemalloc.log
# RSS continua a essere loggato in:
#   /tmp/co2-monitor-rss.log
#
# Per terminare e tornare alla GUI normale: chiudi la finestra e cron
# orario / autostart riprendono dalla prossima occasione, oppure:
#   bin/co2-monitor-restart.sh --force

set -u

if pgrep -f '[g]mp343_sht31_monitor.py' >/dev/null; then
    echo "Monitor già attivo — kill prima di rilanciare in debug:"
    pgrep -af '[g]mp343_sht31_monitor.py' | head
    read -r -p "Procedo con kill + restart-debug? [y/N] " ans
    case "$ans" in
        y|Y|yes) pgrep -f '[g]mp343_sht31_monitor.py' | xargs -r kill
                 sleep 3
                 pgrep -f '[g]mp343_sht31_monitor.py' | xargs -r kill -9 2>/dev/null
                 sleep 1 ;;
        *) echo "Annullato."; exit 1 ;;
    esac
fi

cd "$(dirname "$0")/.."
echo "Avvio Monitor con tracemalloc attivo..."
echo "Log: /tmp/co2-monitor-tracemalloc.log"
echo "RSS: /tmp/co2-monitor-rss.log"
echo

MONITOR_TRACEMALLOC=1 \
DISPLAY="${DISPLAY:-:0}" \
XAUTHORITY="${XAUTHORITY:-/home/misura/.Xauthority}" \
exec python3 gmp343_sht31_monitor.py "$@"
