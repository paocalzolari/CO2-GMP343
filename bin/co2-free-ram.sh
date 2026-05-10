#!/bin/bash
# co2-free-ram.sh — best-effort RAM cleanup that keeps acquisition,
# communication and chart visualization fully running.
#
# What it does (non-destructive):
#   1. Touch a trigger file the Monitor checks at every tick: when it
#      sees the file, it does gc.collect() + plt.close + chart reload
#      (frees matplotlib artists not yet collected) and unlinks the
#      trigger. We can't use SIGUSR1 because PyQt5 doesn't propagate
#      Python signals during app.exec_() and the OS default action of
#      SIGUSR1 is "terminate" — that would kill the Monitor.
#   2. drop_caches=3 (page+inode+dentry) — kernel reclaims, but the OS
#      will refill caches as I/O happens. Cosmetic for free, not
#      destructive.
#   3. Reports before/after free MB and which processes are heaviest.
#
# Does NOT touch:
#   - co2-logger.service (acquisition keeps running)
#   - valve-daemon (communication keeps running)
#   - GMP343 Monitor process (still up; just lighter)
#   - rsync cron (data sync continues)

set -u

ICON="$HOME/programs/CO2/gmp343_sensor.png"
notify() { notify-send -i "$ICON" "CO2 free-RAM" "$1" 2>/dev/null || true; }

# -------- BEFORE --------
echo "=== before ==="
free -h | awk 'NR==1{print} NR==2{print}'
# Find the real Python Monitor process (not bash whose cmdline happens
# to contain the script name as a substring — pgrep -f matches that
# too).
find_monitor_pid() {
    for p in $(pgrep python3 2>/dev/null); do
        cmd=$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null)
        case "$cmd" in
            *gmp343_sht31_monitor.py*) echo "$p"; return ;;
        esac
    done
}
mon_pid=$(find_monitor_pid)
if [ -n "$mon_pid" ]; then
    rss_before=$(awk '/VmRSS/{print $2}' /proc/$mon_pid/status 2>/dev/null)
    echo "Monitor PID $mon_pid → RSS $rss_before kB"
fi

# -------- 1. trigger Monitor GC via file --------
if [ -n "$mon_pid" ]; then
    : > /tmp/co2-monitor-free-ram.trigger
    echo "→ trigger file touched (Monitor will gc+plt.close+reload at next tick, ≤5s)"
else
    echo "→ Monitor not running, skipping trigger"
fi
sleep 6  # give the Monitor up to 1 tick + slack to react

# -------- 2. drop kernel caches --------
sync
if sudo -n sysctl -q -w vm.drop_caches=3 2>/dev/null; then
    echo "→ kernel page+inode+dentry caches dropped"
else
    echo "→ drop_caches needs sudo (skipped)"
fi

# -------- AFTER --------
echo
echo "=== after ==="
free -h | awk 'NR==1{print} NR==2{print}'
mon_pid_after=$(find_monitor_pid)
if [ -n "$mon_pid_after" ]; then
    rss_after=$(awk '/VmRSS/{print $2}' /proc/$mon_pid_after/status 2>/dev/null)
    if [ "$mon_pid" = "$mon_pid_after" ]; then
        echo "Monitor PID $mon_pid_after → RSS $rss_after kB (was $rss_before kB)"
    else
        echo "Monitor RESTARTED (was PID $mon_pid → now $mon_pid_after, RSS $rss_after kB)"
    fi
elif [ -n "$mon_pid" ]; then
    echo "Monitor DISAPPEARED (was PID $mon_pid)"
fi

# -------- top consumers --------
echo
echo "=== top 10 RSS consumers now ==="
ps -eo pid,rss,comm --sort=-rss --no-headers 2>&1 | head -10 | \
    awk '{printf "  %6s  %6.1f MB  %s\n", $1, $2/1024, $3}'

notify "drop_caches done. Check /tmp/co2-free-ram.log for detail."
