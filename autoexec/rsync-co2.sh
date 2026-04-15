#!/bin/bash
# rsync-co2.sh
# Sincronizza ~/data/ verso cimone@ozone.bo.isac.cnr.it:/home/cimone/data/gmp343.
# Eseguito da cron utente ogni 5 minuti.
#
# Auto-recovery Tailscale: se la risoluzione DNS del server remoto fallisce
# (sintomo tipico di magicDNS IPv6 bloccata dopo sospensione/ripresa di rete),
# riavvia `tailscaled` via `sudo -n` e ritenta una volta. Se fallisce ancora,
# esce con codice 1 e logga — il prossimo run di cron ci riproverà.
#
# Richiede sudo senza password per `systemctl restart tailscaled`. Per abilitarlo:
#   echo 'misura ALL=(root) NOPASSWD: /bin/systemctl restart tailscaled' | \
#       sudo tee /etc/sudoers.d/misura-tailscale
#   sudo chmod 440 /etc/sudoers.d/misura-tailscale

set -u

TOUCHFILE="$HOME/00-RSYNC_IN_PROGRESS"
LOG="/tmp/rsync-co2.log"
REMOTE_HOST="ozone.bo.isac.cnr.it"
REMOTE_USER="cimone"
REMOTE_PATH="/home/cimone/data/gmp343"
SRC_DIR="/home/misura/data/"

# Cleanup del touchfile in ogni scenario di uscita
trap 'rm -f "$TOUCHFILE"' EXIT

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

# Tronca log se supera 1 MB (mantiene ultime 500 righe)
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# ── Single-instance lock ─────────────────────────────────────────────────────
if [ -e "$TOUCHFILE" ]; then
    log "rsync già in corso (touchfile presente), skip"
    exit 0
fi
date > "$TOUCHFILE"

# ── Helper ──────────────────────────────────────────────────────────────────
dns_ok() {
    getent hosts "$REMOTE_HOST" > /dev/null 2>&1
}

restart_tailscale() {
    log "restart tailscaled"
    if sudo -n /bin/systemctl restart tailscaled 2>>"$LOG"; then
        sleep 5
        return 0
    fi
    log "sudo restart tailscaled FALLITO (controlla sudoers)"
    return 1
}

do_rsync() {
    rsync -az --timeout=60 \
        -e "ssh -o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
        "$SRC_DIR" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PATH}" \
        >> "$LOG" 2>&1
}

# ── Pre-check DNS ────────────────────────────────────────────────────────────
if ! dns_ok; then
    log "DNS KO per $REMOTE_HOST, tentativo restart Tailscale"
    if ! restart_tailscale; then
        exit 1
    fi
    if ! dns_ok; then
        log "DNS ancora KO dopo restart, abort"
        exit 1
    fi
    log "DNS OK dopo restart"
fi

# ── Rsync con un retry in caso di errore di rete ─────────────────────────────
if do_rsync; then
    log "rsync OK"
    exit 0
fi

rc=$?
log "rsync fallito (rc=$rc), restart Tailscale e retry"
if restart_tailscale && do_rsync; then
    log "rsync OK dopo restart"
    exit 0
fi

log "rsync FALLITO anche dopo restart, abort"
exit 1
