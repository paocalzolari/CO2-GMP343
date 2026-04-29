#!/bin/bash
# co2-launcher.sh — yad GUI launcher for the CO2 / GMP343 stack.
#
# One dialog, one tick → action. Shows live status of:
#   - Backend logger (systemd unit co2-logger.service)
#   - Monitor GUI (gmp343_sht31_monitor.py)
# and last CO2 ppm + last sample mtime from status.json.
#
# Calibration is now driven automatically by valve-scheduler (rule:
# valve_pos != measure_position → flag=calib), so the manual calib GUI
# is no longer surfaced from this launcher. For exceptional manual
# sessions, run bin/co2-calib-mode.sh directly.
#
# Inspired by ~/programs/acq-tools/acq-launcher.sh (multi-program launcher).

set -u

ICON="$HOME/programs/CO2/gmp343_sensor.png"
STATUS_JSON="$HOME/programs/CO2/shared/ipc_co2/status.json"
STATUS_MAX_AGE_S=120

# Qt platform (xcb on X / wayland on Wayland)
if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
    export QT_QPA_PLATFORM=wayland
else
    export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
    export DISPLAY="${DISPLAY:-:0}"
fi

# ── status helpers ──────────────────────────────────────────────────────────
backend_status() {
    # 🔴 STOPPED   service inactive/failed
    # 🟡 NO INSTR  service running but no fresh status.json or instrument false
    # 🟢 ONLINE    service running, status.json fresh, instrument_connected=true
    if ! systemctl is-active --quiet co2-logger 2>/dev/null; then
        echo "🔴 STOPPED"; return
    fi
    if [ -f "$STATUS_JSON" ]; then
        local now mtime age
        now=$(date +%s)
        mtime=$(stat -c %Y "$STATUS_JSON" 2>/dev/null || echo 0)
        age=$((now - mtime))
        if [ "$age" -le "$STATUS_MAX_AGE_S" ] \
           && grep -q '"instrument_connected"[[:space:]]*:[[:space:]]*true' "$STATUS_JSON"; then
            echo "🟢 ONLINE"; return
        fi
    fi
    echo "🟡 NO INSTR"
}

gui_status() {
    # Match the Python interpreter running the script (avoid matching this shell)
    if pgrep -f "python3.*$1" >/dev/null 2>&1; then
        echo "🟢 OPEN"
    else
        echo "⚪ CLOSED"
    fi
}

last_co2() {
    # Extract last_co2_ppm from status.json with a tiny python one-liner;
    # silent on parse failure.
    python3 -c "
import json,sys
try:
    d=json.load(open('$STATUS_JSON'))
    v=d.get('last_co2_ppm')
    print(f'{v:.2f} ppm' if isinstance(v,(int,float)) else '—')
except Exception:
    print('—')
" 2>/dev/null
}

last_age() {
    if [ -f "$STATUS_JSON" ]; then
        local now mtime age
        now=$(date +%s)
        mtime=$(stat -c %Y "$STATUS_JSON" 2>/dev/null || echo 0)
        age=$((now - mtime))
        if [ "$age" -lt 60 ]; then
            echo "${age}s ago"
        elif [ "$age" -lt 3600 ]; then
            echo "$((age/60))m ago"
        else
            echo "$((age/3600))h ago"
        fi
    else
        echo "—"
    fi
}

# ── action helpers ──────────────────────────────────────────────────────────
notify() { notify-send -i "$ICON" "CO2 / GMP343" "$1" 2>/dev/null || true; }

backend_start()   { sudo -n systemctl start   co2-logger 2>&1 | head -3 | tr '\n' ' '; }
backend_stop()    { sudo -n systemctl stop    co2-logger 2>&1 | head -3 | tr '\n' ' '; }
backend_restart() { sudo -n systemctl restart co2-logger 2>&1 | head -3 | tr '\n' ' '; }

monitor_open() {
    # Avoid double-launch
    if pgrep -f "python3.*gmp343_sht31_monitor.py" >/dev/null; then
        notify "Monitor already running"; return
    fi
    cd "$HOME/programs/CO2" || return
    setsid python3 -u gmp343_sht31_monitor.py < /dev/null \
        > /tmp/co2monitor.log 2>&1 &
    disown
    notify "Monitor started"
}

monitor_close() {
    if pkill -f "python3.*gmp343_sht31_monitor.py" 2>/dev/null; then
        notify "Monitor closed"
    else
        notify "Monitor not running"
    fi
}

status_term() {
    # Open a terminal showing co2-status.sh (re-uses existing script)
    for term in lxterminal x-terminal-emulator xterm gnome-terminal konsole; do
        if command -v "$term" >/dev/null 2>&1; then
            "$term" -e bash -c "$HOME/programs/CO2/bin/co2-status.sh; read -p 'Premi INVIO per chiudere...'" &
            return
        fi
    done
    notify "No terminal emulator found (xterm/lxterminal/...)"
}

dispatch() {
    local sel="$1"
    [ -z "$sel" ] && return
    IFS='|' read -ra CMDS <<< "$sel"
    for c in "${CMDS[@]}"; do
        case "$c" in
            ""|noop) ;;
            backend_start)   backend_start   ;;
            backend_stop)    backend_stop    ;;
            backend_restart) backend_restart ;;
            monitor_open)    monitor_open    ;;
            monitor_close)   monitor_close   ;;
            status_term)     status_term     ;;
        esac
    done
}

# ── main loop ───────────────────────────────────────────────────────────────
SEP="__SEP__"

while true; do
    S_BE=$(backend_status)
    S_MON=$(gui_status "gmp343_sht31_monitor.py")
    LAST=$(last_co2)
    AGE=$(last_age)

    SELECTION=$(yad --list --checklist \
        --title="CO2 / GMP343 Launcher — ISAC CNR" \
        --width=620 --height=420 \
        --window-icon="$ICON" \
        --image="$ICON" --image-on-top \
        --text="<b>Backend:</b> $S_BE   ·   <b>Monitor:</b> $S_MON\n<b>Last CO₂:</b> $LAST   ·   <b>Updated:</b> $AGE   ·   <i>(refresh on Apply/Refresh)</i>" \
        --column="" --column="Status" --column="Component" --column="Action" --column="cmd" \
        --hide-column=5 --print-column=5 --separator="|" \
        --grid-lines=HOR --expand-column=4 \
        --button="Refresh":3 --button="Apply":0 --button="Close":1 \
        FALSE "$S_BE"   "Backend"  "▶  Start"        "backend_start"   \
        FALSE ""        "Backend"  "■  Stop"         "backend_stop"    \
        FALSE ""        "Backend"  "↻  Restart"      "backend_restart" \
        FALSE ""        ""         ""                "$SEP"            \
        FALSE "$S_MON"  "Monitor"  "🖥  Open GUI"     "monitor_open"    \
        FALSE ""        "Monitor"  "✖  Close GUI"    "monitor_close"   \
        FALSE ""        ""         ""                "$SEP"            \
        FALSE "—"       "Status"   "📜  Terminal status + journal -f"  "status_term" \
        2>/dev/null)
    RC=$?

    case "$RC" in
        0)  dispatch "$SELECTION" ;;   # Apply
        3)  ;;                         # Refresh
        *)  break ;;                   # Close (1), Esc, etc.
    esac
done
