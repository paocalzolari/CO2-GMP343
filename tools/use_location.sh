#!/bin/bash
# tools/use_location.sh — switch INI CO2-GMP343 tra location.
#
# Pattern allineato a o3-monitor / nox-monitor / meteo-cimone /
# calib-ps-49i.
#
# CO2-GMP343 ha 5 file INI in `config/`:
#   integration.ini   — integrazioni opt-in (valve-scheduler, ...)
#   monitor.ini       — config GUI monitor (window size, fonts, ...)
#   name.ini          — nome stazione/strumento per file output
#   serial.ini        — porta seriale USB (/dev/gmp343 via udev rule,
#                        location-agnostic)
#   site.ini          — coordinate + nome location (LOCATION-SPECIFIC)
#
# Solo `site.ini` cambia tra deployment. Gli altri 4 INI sono
# location-agnostic (la porta seriale viene risolta dalla udev rule
# `/dev/gmp343`, indipendentemente dall'host fisico).
#
# Location supportate (`config/site.<loc>.ini`):
#   bo-shelter  → Bologna shelter (ISACBO, lat 44.523624, lon 11.338379)
#                  ATTUALMENTE in produzione qui (utente 2026-05-04).
#   cmn         → Monte Cimone GAW (CMN, lat 44.193, lon 10.701, alt 2165m)
#                  TEMPLATE PARZIALE: da popolare quando si vorra'
#                  deployare la sonda GMP343 a Cimone.
#   bo-lab      → Bologna lab — TEMPLATE NON POPOLATO (no setup GMP343
#                  in laboratorio al momento).
#
# Uso:
#   tools/use_location.sh bo-shelter   # passa a Bologna shelter
#   tools/use_location.sh cmn          # passa a Cimone
#   tools/use_location.sh status       # location attuale
#   tools/use_location.sh list         # template disponibili

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
CFG="$ROOT/config"
BACKUP="$CFG/.backup"
INI_BASENAME="site"
LOCATIONS=("cmn" "bo-shelter" "bo-lab")

usage() {
    cat <<EOF
Uso: $(basename "$0") <location|cmd>
  location: cmn | bo-shelter | bo-lab
  cmd     : status | list

Esempi:
  $(basename "$0") bo-shelter   # passa a Bologna shelter
  $(basename "$0") cmn          # passa a Cimone
  $(basename "$0") status       # location attuale
EOF
    exit 1
}

# Estrae name della location dalla sezione [location]
name_from_site() {
    local f="$1"
    awk '/^\[location\]/{found=1; next} /^\[/{found=0} found && /^name[[:space:]]*=/{gsub(/^name[[:space:]]*=[[:space:]]*/,""); print; exit}' "$f"
}

list_templates() {
    echo "Template disponibili in $CFG/:"
    for loc in "${LOCATIONS[@]}"; do
        local f="$CFG/${INI_BASENAME}.${loc}.ini"
        if [[ -f "$f" ]]; then
            local name
            name=$(name_from_site "$f")
            echo "  ✓ ${loc}    (location name=${name})"
        else
            echo "  ✗ ${loc}    (template non popolato)"
        fi
    done
}

detect_location() {
    local active="$CFG/${INI_BASENAME}.ini"
    if [[ ! -f "$active" ]]; then echo "missing"; return; fi
    local name_active
    name_active=$(name_from_site "$active")
    for loc in "${LOCATIONS[@]}"; do
        local tpl="$CFG/${INI_BASENAME}.${loc}.ini"
        [[ -f "$tpl" ]] || continue
        local name_tpl
        name_tpl=$(name_from_site "$tpl")
        if [[ "$name_active" == "$name_tpl" ]]; then
            echo "$loc"
            return
        fi
    done
    echo "custom (name=$name_active)"
}

show_status() {
    local loc
    loc=$(detect_location)
    echo "Location attuale: $loc"
    echo "  ${INI_BASENAME}.ini → $loc"
}

apply_location() {
    local loc="$1"
    local template="$CFG/${INI_BASENAME}.${loc}.ini"
    local active="$CFG/${INI_BASENAME}.ini"
    if [[ ! -f "$template" ]]; then
        echo "ERROR: template mancante: $template" >&2
        echo "       Lancia '$(basename "$0") list' per vedere i template disponibili." >&2
        exit 2
    fi
    mkdir -p "$BACKUP"
    if [[ -f "$active" ]]; then
        local stamp
        stamp=$(date +%Y%m%dT%H%M%SZ)
        cp -p "$active" "$BACKUP/${INI_BASENAME}_${stamp}.ini"
        echo "  backup: $BACKUP/${INI_BASENAME}_${stamp}.ini"
    fi
    cp "$template" "$active"
    echo "  ${INI_BASENAME}.ini ← ${loc} template"
}

main() {
    [[ $# -ge 1 ]] || usage
    case "$1" in
        status) show_status ;;
        list) list_templates ;;
        cmn|bo-shelter|bo-lab)
            echo "Switch a location: $1"
            apply_location "$1"
            echo
            show_status
            echo
            echo "Riavvia il logger CO2 per applicare:"
            echo "  sudo systemctl restart co2-logger"
            ;;
        *) usage ;;
    esac
}

main "$@"
