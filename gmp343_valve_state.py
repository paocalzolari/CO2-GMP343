#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gmp343_valve_state.py
=====================
Lettore dello stato della valvola multiposizione VICI pubblicato dal
programma valve-scheduler (~/programs/valve-scheduler/).

Integrazione opt-in con gmp343_sht31_logger.py:
quando l'integrazione è abilitata (config/integration.ini → enabled=true),
ogni riga del file `_min.raw` contiene 2 colonne aggiuntive dopo il flag:
`valve_pos` (intero 1..N o `-1` se sconosciuto) e `valve_label` (stringa
senza spazi, `-` se vuota/sconosciuta).

Progettato per Raspberry Pi 5 (bassa latenza, zero deps esterne):
  - solo stdlib (json, os, datetime)
  - cache per mtime: non riparsa se il file non è cambiato
  - tollerante: file mancante, JSON corrotto, stato stale → (None, "")
  - nessun lock: il valve-scheduler scrive in modo atomico (tmp + rename),
    quindi leggere senza lock è safe

Formato atteso del JSON prodotto da valve-scheduler:
{
  "timestamp": "2026-04-23T10:30:45+00:00",
  "state": "running",           // running|paused|stopped|idle
  "step_index": 3,
  "step_total": 10,
  "step_label": "span-low",
  "position": 5,
  "position_target": 5,
  "seconds_remaining": 120,
  "loop_enabled": true,
  "cycle_count": 2
}
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

# Sentinelle coerenti con il formato .raw del GMP343 (v2)
SENTINEL_POS = -1
SENTINEL_LABEL = "-"

# Cache interna: evita re-parse se mtime invariato.
_cache: dict = {"path": None, "mtime": 0.0, "pos": None, "label": ""}


def _is_stale(ts_str: str, stale_after_s: float) -> bool:
    """Verifica se il timestamp è più vecchio di stale_after_s secondi.

    Restituisce False se il parsing fallisce (si assume fresco).
    """
    if not ts_str or stale_after_s <= 0:
        return False
    try:
        # Supporta sia "+00:00" sia suffisso "Z"
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > stale_after_s
    except (ValueError, TypeError):
        return False


def read_valve_status(path: str,
                      stale_after_s: float = 10.0
                      ) -> Tuple[Optional[int], str]:
    """Legge lo stato corrente della valvola da valve_status.json.

    Args:
        path: percorso al file JSON (supporta `~`).
        stale_after_s: se il timestamp nel JSON è più vecchio di questi
            secondi, lo stato viene considerato non valido. Passa 0 per
            disabilitare il check.

    Returns:
        (pos, label): pos è int (1..N) se valido, None altrimenti.
            label è una stringa (può essere vuota).

    La funzione non solleva eccezioni: problemi di I/O, parsing, o stato
    stale restituiscono sempre (None, "").
    """
    try:
        expanded = os.path.expanduser(path)
        st = os.stat(expanded)
    except OSError:
        return (None, "")

    # Cache hit: mtime invariato e stesso file
    if _cache["path"] == expanded and st.st_mtime == _cache["mtime"]:
        return (_cache["pos"], _cache["label"])

    try:
        with open(expanded, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        # File troncato durante rename atomico o corrotto: salta
        return (None, "")

    if _is_stale(str(data.get("timestamp", "")), stale_after_s):
        return (None, "")

    pos_raw = data.get("position", -1)
    label = str(data.get("step_label", ""))

    try:
        pos_int = int(pos_raw)
    except (TypeError, ValueError):
        pos_int = -1

    if pos_int < 1:
        # posizione sconosciuta o engine in idle
        return (None, "")

    # aggiorna cache
    _cache.update(path=expanded, mtime=st.st_mtime, pos=pos_int, label=label)
    return (pos_int, label)


def format_for_raw(path: str,
                   stale_after_s: float = 10.0
                   ) -> Tuple[str, str]:
    """Restituisce (pos_str, label_str) pronti per essere scritti in `.raw`.

    Sentinelle:
      - pos: '-1' se sconosciuta
      - label: '-' se vuota o sconosciuta

    La label viene sanitizzata: spazi → underscore, tab/newline rimossi.
    Così il file resta whitespace-separated parsabile da np.loadtxt e dai
    parser esistenti (gui_integrated_v13.read_file).
    """
    pos, label = read_valve_status(path, stale_after_s)
    pos_str = str(pos) if pos is not None else str(SENTINEL_POS)
    safe = "".join(c for c in label if c not in (" ", "\t", "\n", "\r"))
    # in alternativa: safe = label.replace(" ", "_")... ma è più sicuro
    # rimuovere spazi proprio (la label del valve-scheduler non dovrebbe
    # mai contenerli, ma meglio difendersi)
    safe = safe or SENTINEL_LABEL
    return pos_str, safe


def get_flag(path: str,
             stale_after_s: float = 10.0,
             calib_labels: list[str] | None = None
             ) -> str:
    """Determina il flag measure/calib in base alla valve_label corrente.

    Args:
        path: percorso al file valve_status.json.
        stale_after_s: soglia staleness (secondi).
        calib_labels: lista di label (case-insensitive) che indicano "calib".
            Se None o vuota, ritorna sempre "measure".

    Returns:
        "calib" se la label corrente è in calib_labels, "measure" altrimenti.
        Se il file è mancante/stale/corrotto → "measure" (fallback sicuro).
    """
    if not calib_labels:
        return "measure"
    pos, label = read_valve_status(path, stale_after_s)
    if pos is None or not label:
        return "measure"
    label_lower = label.lower()
    for cl in calib_labels:
        if cl.lower() == label_lower:
            return "calib"
    return "measure"


# ----------------------------------------------------------------- CLI helper
def _cli_dump(path: str) -> int:
    """python3 gmp343_valve_state.py <path> — stampa stato per diagnostica."""
    pos, label = read_valve_status(path, stale_after_s=0)
    pos_s, lab_s = format_for_raw(path, stale_after_s=0)
    print(f"file      : {os.path.expanduser(path)}")
    print(f"exists    : {os.path.exists(os.path.expanduser(path))}")
    print(f"pos       : {pos}")
    print(f"label     : {label!r}")
    print(f"raw-format: {pos_s} {lab_s}")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("uso: python3 gmp343_valve_state.py <path-to-valve_status.json>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(_cli_dump(sys.argv[1]))
