#!/bin/bash
# Restart della GUI CO2 Monitor — workaround crescita RAM (~1.2 GB dopo ore).
#
# Uso:
#   co2-monitor-restart.sh                   # restart sempre se up (default)
#   co2-monitor-restart.sh --if-above-mb 700 # restart solo se RSS > 700 MB
#   co2-monitor-restart.sh --force           # alias di default (esplicito)
#
# Se l'utente ha chiuso la GUI volontariamente (non è up), NON la riavvia.
#
# Pensato per due slot cron:
#   - orario: --if-above-mb 700  (preventivo, no-op se GUI è sotto soglia)
#   - 03:00:  --force            (igienico, libera comunque le cache nightly)
#
# Log: /tmp/co2-monitor-restart.log

set -u

LOG=/tmp/co2-monitor-restart.log
exec >>"$LOG" 2>&1
echo "================ $(date -Iseconds)  args=$*  ================"

# ── Parse args ───────────────────────────────────────────────────────────────
THRESHOLD_MB=0
while [ $# -gt 0 ]; do
    case "$1" in
        --if-above-mb)  THRESHOLD_MB="${2:-0}"; shift 2 ;;
        --force)        THRESHOLD_MB=0;        shift   ;;
        --help|-h)
            grep '^# ' "$0" | head -20
            exit 0 ;;
        *) echo "WARN: arg sconosciuto: $1"; shift ;;
    esac
done

# ── Costanti ─────────────────────────────────────────────────────────────────
MONITOR_PY=gmp343_sht31_monitor.py
PATTERN='[g]mp343_sht31_monitor.py'
CO2_DIR=/home/misura/programs/CO2

# ── Liveness ─────────────────────────────────────────────────────────────────
PIDS=$(pgrep -f "$PATTERN" || true)
if [ -z "$PIDS" ]; then
    echo "Monitor non attivo: niente da fare (utente l'ha chiuso volontariamente)."
    exit 0
fi

# ── Soglia RSS ───────────────────────────────────────────────────────────────
if [ "$THRESHOLD_MB" -gt 0 ]; then
    # somma RSS di tutti i match (di solito 1 solo)
    RSS_KB=$(ps -o rss= -p $PIDS | awk '{s+=$1} END{print s}')
    RSS_MB=$(( RSS_KB / 1024 ))
    echo "Monitor PID=$PIDS  RSS=${RSS_MB} MB  soglia=${THRESHOLD_MB} MB"
    if [ "$RSS_MB" -le "$THRESHOLD_MB" ]; then
        echo "Sotto soglia: skip restart."
        exit 0
    fi
    echo "Sopra soglia: procedo col restart."
else
    echo "Modalità force: PID=$PIDS"
    ps -o pid,rss,etime --no-headers $PIDS
fi

# ── Kill ─────────────────────────────────────────────────────────────────────
echo "Kill SIGTERM..."
kill $PIDS 2>/dev/null
for i in 1 2 3 4 5; do
    sleep 1
    pgrep -f "$PATTERN" >/dev/null || break
done
if pgrep -f "$PATTERN" >/dev/null; then
    echo "Ancora vivo dopo 5s, SIGKILL."
    pgrep -f "$PATTERN" | xargs -r kill -9
    sleep 1
fi

# ── Restart ──────────────────────────────────────────────────────────────────
echo "Restart in $CO2_DIR..."
cd "$CO2_DIR" || { echo "ERRORE: cd $CO2_DIR fallito"; exit 1; }

# Cron non eredita DISPLAY/XAUTHORITY: serve passarli esplicitamente per
# attaccarsi al server X dell'utente (sessione LXDE-pi).
DISPLAY="${DISPLAY:-:0}" \
XAUTHORITY="${XAUTHORITY:-/home/misura/.Xauthority}" \
nohup python3 "$MONITOR_PY" >/tmp/co2monitor.log 2>&1 < /dev/null &
disown
sleep 5

NEWPID=$(pgrep -f "$PATTERN" | tail -1)
if [ -n "$NEWPID" ]; then
    NEW_RSS_KB=$(ps -o rss= -p "$NEWPID" | tr -d ' ')
    echo "Monitor ripartito: PID=$NEWPID  RSS=$(( NEW_RSS_KB / 1024 )) MB"
    exit 0
fi
echo "ERRORE: restart fallito (nessun PID dopo 5s)"
exit 2
