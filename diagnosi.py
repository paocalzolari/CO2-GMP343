#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diagnosi.py — Esegui sul Raspberry Pi:
  python3 diagnosi.py

Mostra esattamente cosa vede co2-monitor:
percorsi, file ini letti, file dati trovati, prime righe del file.
"""

import os, sys, glob, configparser
from datetime import datetime, timedelta

# ── stessi percorsi di gui_integrated_v7/v8 ──────────────────────────────────
# Simula sia esecuzione come script che come binario PyInstaller

print()
print("=" * 65)
print("  DIAGNOSI co2-monitor")
print("=" * 65)

# 1. Dove crede di essere il programma?
script_dir = os.path.dirname(os.path.abspath(__file__))
print(f"\n[1] Directory script     : {script_dir}")
print(f"    Directory /opt        : {os.path.exists('/opt/co2-monitor')}")

# Prova entrambe le basi possibili
candidates = [
    script_dir,
    "/opt/co2-monitor",
    os.path.expanduser("~/programs/CO2"),
]

for base in candidates:
    if os.path.isdir(base):
        cfg_dir = os.path.join(base, "config")
        has_cfg = os.path.isdir(cfg_dir)
        has_name = os.path.exists(os.path.join(cfg_dir, "name.ini"))
        has_site = os.path.exists(os.path.join(cfg_dir, "site.ini"))
        print(f"\n[BASE] {base}")
        print(f"       config/        {'✓' if has_cfg   else '✗'}")
        print(f"       config/name.ini{'✓' if has_name  else '✗'}")
        print(f"       config/site.ini{'✓' if has_site  else '✗'}")

# 2. Legge tutti i config disponibili e mostra valori chiave
print("\n" + "─" * 65)
print("[2] CONTENUTO FILE INI")
for base in candidates:
    cfg_dir = os.path.join(base, "config")
    if not os.path.isdir(cfg_dir):
        continue
    print(f"\n  Base: {base}")
    cfg = configparser.ConfigParser()
    files_read = cfg.read([
        os.path.join(cfg_dir, "name.ini"),
        os.path.join(cfg_dir, "site.ini"),
        os.path.join(cfg_dir, "serial.ini"),
    ])
    print(f"  File letti: {[os.path.basename(f) for f in files_read]}")

    site     = cfg.get("location", "name",      fallback="⚠ MANCANTE")
    basename = cfg.get("output",   "basename",  fallback="⚠ MANCANTE")
    ext      = cfg.get("output",   "extension", fallback="⚠ MANCANTE")
    data_dir = cfg.get("output",   "data_dir",  fallback="⚠ NON PRESENTE → fallback /home/misura/data")
    port     = cfg.get("serial",   "port",      fallback="⚠ MANCANTE")

    print(f"  location.name  = '{site}'")
    print(f"  output.basename= '{basename}'")
    print(f"  output.extension='{ext}'")
    print(f"  output.data_dir= '{data_dir}'")
    print(f"  serial.port    = '{port}'")

# 3. File dati: cosa c'è in /home/misura/data ?
print("\n" + "─" * 65)
print("[3] FILE DATI IN /home/misura/data/")
data_dir_real = "/home/misura/data"
if os.path.isdir(data_dir_real):
    files = sorted(os.listdir(data_dir_real))
    print(f"  Totale file: {len(files)}")
    print(f"  Ultimi 5:")
    for f in files[-5:]:
        fpath = os.path.join(data_dir_real, f)
        size  = os.path.getsize(fpath)
        mtime = datetime.fromtimestamp(os.path.getmtime(fpath)).strftime("%Y-%m-%d %H:%M")
        print(f"    {f}  ({size} B, mod: {mtime})")
else:
    print(f"  ✗ Cartella non esiste: {data_dir_real}")

# 4. Ricerca glob per oggi e ieri
print("\n" + "─" * 65)
print("[4] RICERCA FILE CON GLOB")
today     = datetime.utcnow().date()
yesterday = today - timedelta(days=1)
for d in [today, yesterday]:
    pattern = os.path.join(data_dir_real, f"*-{d.strftime('%Y%m%d')}_min.*")
    matches = glob.glob(pattern)
    print(f"  {d}  pattern: *-{d.strftime('%Y%m%d')}_min.*")
    if matches:
        for m in matches:
            print(f"    ✓ TROVATO: {m}")
    else:
        print(f"    ✗ nessun file trovato")

# 5. Prova a leggere il file più recente
print("\n" + "─" * 65)
print("[5] LETTURA FILE PIÙ RECENTE")
pattern_any = os.path.join(data_dir_real, "*_min.*")
all_min = glob.glob(pattern_any)
if all_min:
    newest = max(all_min, key=os.path.getmtime)
    print(f"  File: {newest}")
    with open(newest, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    print(f"  Righe totali: {len(lines)}")
    print(f"  Prima riga  : {lines[0].rstrip()!r}")
    if len(lines) > 1:
        print(f"  Seconda riga: {lines[1].rstrip()!r}")
    print(f"  Ultima riga : {lines[-1].rstrip()!r}")

    # Prova a parsare l'ultima riga dati
    for raw in reversed(lines):
        p = raw.split()
        if len(p) >= 9 and not raw.startswith("YYYY"):
            try:
                from datetime import datetime as dt
                ts  = dt.strptime(" ".join(p[:6]), "%Y %m %d %H %M %S")
                co2 = float(p[6])
                std = float(p[7])
                n   = int(p[8])
                print(f"\n  Ultimo dato valido:")
                print(f"    Timestamp : {ts}")
                print(f"    CO₂       : {co2} ppm")
                print(f"    σ         : {std}")
                print(f"    n campioni: {n}")
            except ValueError as e:
                print(f"  ✗ Errore parsing ultima riga: {e}")
            break
    else:
        print("  ✗ Nessuna riga dati valida trovata")
else:
    print(f"  ✗ Nessun file *_min.* in {data_dir_real}")

print("\n" + "=" * 65)
print("  Fine diagnosi")
print("=" * 65)
print()
