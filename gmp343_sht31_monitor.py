#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gmp343_sht31_monitor.py — GMP343 + SHT31-D Monitor integrato
Tab 1 : Monitor real-time  (CO2 + T + RH + flag corrente dal file)
Tab 2 : Grafico CO₂        (punti calib in arancione, label flag fuori plot)

Formato file v3 (dal 2026-04-15, con T/RH):
  - Header: #date time CO2[PPM] CO2_std[PPM] T[C] T_std[C] RH[%] RH_std[%] ndata_60s_mean flag
  - Dato mancante: -999.99
  - Parser retrocompatibile col formato v2 (5 colonne, senza T/RH).
"""

import sys, os, json, signal, subprocess, logging, time, tracemalloc
from datetime import datetime, timedelta, timezone, date as date_type
import configparser
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QGridLayout, QTabWidget, QPushButton,
    QComboBox, QDateEdit, QCheckBox, QFrame, QMessageBox,
    QDialog, QFormLayout, QSpinBox, QDoubleSpinBox, QLineEdit,
    QDialogButtonBox, QFileDialog, QToolButton, QColorDialog,
    QPlainTextEdit, QScrollArea, QAction, QSizePolicy
)
from PyQt5.QtCore  import QTimer, Qt, QDate
from PyQt5.QtGui   import QFont, QPixmap, QColor
import serial, serial.tools.list_ports

import matplotlib
matplotlib.use("Qt5Agg")
from matplotlib.backends.backend_qt5agg import (
    FigureCanvasQTAgg as FigureCanvas,
    NavigationToolbar2QT as NavToolbar
)
from matplotlib.figure import Figure
import matplotlib.dates  as mdates
import matplotlib.ticker as mticker
import numpy as np

# ── valve-scheduler opzionale ─────────────────────────────────────────────────
_VALVE_SCHED_DIR = os.path.expanduser("~/programs/valve-scheduler")
_HAS_VALVE_SCHEDULER = Path(_VALVE_SCHED_DIR).is_dir()
if _HAS_VALVE_SCHEDULER and _VALVE_SCHED_DIR not in sys.path:
    sys.path.insert(0, _VALVE_SCHED_DIR)
_VALVE_STATUS_JSON = Path(
    "/home/misura/programs/valve-scheduler/service/valve_status.json")
_VALVE_STATUS_STALE_S = 10.0
_MEASURE_POSITION = 1


def read_live_valve() -> tuple[int, str, bool]:
    """Legge valve_status.json e ritorna (pos, label, fresh).

    pos: posizione corrente (-1 se sconosciuta)
    label: step_label dal JSON (vuoto se mancante)
    fresh: True se timestamp entro _VALVE_STATUS_STALE_S
    """
    if not _VALVE_STATUS_JSON.exists():
        return (-1, "", False)
    try:
        with _VALVE_STATUS_JSON.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return (-1, "", False)
    ts_str = str(data.get("timestamp", ""))
    fresh = False
    if ts_str:
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            fresh = age <= _VALVE_STATUS_STALE_S
        except (ValueError, TypeError):
            fresh = False
    try:
        pos = int(data.get("position", -1))
    except (TypeError, ValueError):
        pos = -1
    label = str(data.get("step_label", ""))
    return (pos, label, fresh)

# ── astral opzionale ──────────────────────────────────────────────────────────
try:
    from astral import LocationInfo
    from astral.sun import sun
    import pytz
    ASTRAL_OK = True
except ImportError:
    ASTRAL_OK = False

# ── Percorsi ──────────────────────────────────────────────────────────────────
# I file ini stanno SEMPRE in ~/programs/CO2/config/
# Il binario PyInstaller e lo script python3 leggono entrambi da lì.
CONFIG_DIR  = os.path.expanduser("~/programs/CO2/config")
SERIAL_INI  = os.path.join(CONFIG_DIR, "serial.ini")
SITE_INI    = os.path.join(CONFIG_DIR, "site.ini")
NAME_INI    = os.path.join(CONFIG_DIR, "name.ini")
MONITOR_INI = os.path.join(CONFIG_DIR, "monitor.ini")

# Immagine sensore: accanto allo script o al binario
import sys as _sys
if getattr(_sys, "frozen", False):
    _INSTALL_DIR = os.path.dirname(_sys.executable)
else:
    _INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
SENSOR_IMG = os.path.join(_INSTALL_DIR, "gmp343_sensor.png")

UPDATE_MS       = 5000   # refresh timer (ms)

# ── Diagnostica memoria ──────────────────────────────────────────────────────
# RSS sempre loggato (costo trascurabile) → /tmp/co2-monitor-rss.log
# tracemalloc opt-in via env: MONITOR_TRACEMALLOC=1 python3 gmp343_sht31_monitor.py
RSS_LOG_INTERVAL_MS     = int(os.environ.get(
    "MONITOR_RSS_INTERVAL_MS", "300000"))   # 5 min default (override per test)
# Intervallo snapshot tracemalloc: default 30 min, override per diagnosi
# rapide con MONITOR_TRACEMALLOC_INTERVAL_MS (es. 120000 = 2 min).
TRACEMALLOC_INTERVAL_MS = int(os.environ.get(
    "MONITOR_TRACEMALLOC_INTERVAL_MS", "1800000"))   # 30 min default
# Profondità traceback: default 4 (basso = GUI reattiva); override per
# avere lo stack completo con MONITOR_TRACEMALLOC_DEPTH.
TRACEMALLOC_DEPTH       = int(os.environ.get("MONITOR_TRACEMALLOC_DEPTH", "4"))
RSS_LOG_FILE            = "/tmp/co2-monitor-rss.log"
TRACEMALLOC_LOG_FILE    = "/tmp/co2-monitor-tracemalloc.log"
TRACEMALLOC_ENABLED     = os.environ.get("MONITOR_TRACEMALLOC", "").lower() in (
    "1", "true", "yes", "on")

# Default per-window styling — shared between GraphWidget (rendering) and
# MonitorConfigDialog Appearance tab (form). Keep in sync with the
# GraphWidget._WINDOW_DEFAULTS class attribute (which mirrors this).
# "60m-med" = mediana 60-min (legge il file _60min.raw con metric=median).
MonitorWindow_PER_WINDOW_DEFAULTS = {
    "1m":     {"color_co2": "#2060c0", "color_t": "#c05000", "color_rh": "#007060",
               "linestyle": "-", "line_width": 1.0, "alpha": 0.55},
    "10m":    {"color_co2": "#1a4a99", "color_t": "#983c00", "color_rh": "#00554a",
               "linestyle": "-", "line_width": 1.8, "alpha": 0.85},
    "30m":    {"color_co2": "#0c2d66", "color_t": "#6b2900", "color_rh": "#003a32",
               "linestyle": "-", "line_width": 2.4, "alpha": 0.92},
    "60m":    {"color_co2": "#000033", "color_t": "#401900", "color_rh": "#001f1a",
               "linestyle": "-", "line_width": 3.0, "alpha": 1.00},
    "60m-med":{"color_co2": "#a40000", "color_t": "#a05000", "color_rh": "#0d6e60",
               "linestyle": "--", "line_width": 2.4, "alpha": 1.00},
}
# Ordered list of all available windows
MonitorWindow_PRIORITY = ["1m", "10m", "30m", "60m", "60m-med"]
# Map window keys → (file_suffix, metric)
MonitorWindow_SUFFIX_METRIC = {
    "1m":     ("min",    "mean"),
    "10m":    ("10min",  "mean"),
    "30m":    ("30min",  "mean"),
    "60m":    ("60min",  "mean"),
    "60m-med":("60min",  "median"),
}
MIN_Y_RANGE     = 20.0   # range Y minimo (ppm)
Y_MARGIN_FACTOR = 0.10   # margine verticale relativo



# ══════════════════════════════════════════════════════════════════════════════
#  Funzioni dati
# ══════════════════════════════════════════════════════════════════════════════

def get_data_dir(cfg: configparser.ConfigParser) -> str:
    """Legge data_path da name.ini ed espande ~ (identico al logger)."""
    raw = cfg.get("output", "data_path", fallback="~/data")
    return os.path.expanduser(raw)


def build_filename(cfg: configparser.ConfigParser, d: date_type,
                   suffix: str = "min") -> str:
    """
    Trova il file aggregato per la data d e la finestra temporale `suffix`.

    suffix ∈ {"min" (1-min), "10min", "30min", "60min"}.
    Pattern: *_<YYYYMMDD>_p00_<suffix>.ext
    Restituisce stringa vuota se nessun file trovato.
    """
    import glob
    ext  = cfg.get("output", "extension", fallback="raw")
    ddir = get_data_dir(cfg)
    pattern = os.path.join(ddir,
                           f"*_{d.strftime('%Y%m%d')}_p00_{suffix}.{ext}")
    matches = glob.glob(pattern)
    if not matches:
        return ""
    return max(matches, key=os.path.getmtime)


def read_last_raw_sample(cfg: configparser.ConfigParser, d: date_type):
    """Legge l'ultima riga del file `.raw` (campioni grezzi del giorno d).

    Formati supportati:
      - v3 (dal 2026-04-15): `date time CO2 T RH flag`  → 6 colonne
      - v2 (prima):          `date time CO2 flag`       → 4 colonne (T/RH = None)
    Timestamp `YYYY-MM-DD HH:MM:SS.fff`. Sentinella `-999.99` per
    valori mancanti.

    Returns: (ts_datetime|None, co2|None, t|None, rh|None, flag|None).
    Tutto None se il file manca, è vuoto, o l'ultima riga non è parsabile.
    """
    import glob
    ext = cfg.get("output", "extension", fallback="raw")
    ddir = get_data_dir(cfg)
    pattern = os.path.join(ddir, f"*_{d.strftime('%Y%m%d')}_p00.{ext}")
    matches = [m for m in glob.glob(pattern)
               if "_p00_min." not in os.path.basename(m)]
    if not matches:
        return (None, None, None, None, None, None, None, None)
    path = max(matches, key=os.path.getmtime)
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(4096, size)
            f.seek(max(0, size - chunk))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return (None, None, None, None, None, None, None, None)
    lines = [ln.strip() for ln in tail.splitlines()
             if ln.strip() and not ln.startswith("#")]
    if not lines:
        return (None, None, None, None, None, None, None, None)
    parts = lines[-1].split()
    if len(parts) < 4:
        return (None, None, None, None, None, None, None, None)
    ts_str = f"{parts[0]} {parts[1]}"
    ts = None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            ts = datetime.strptime(ts_str, fmt)
            break
        except ValueError:
            continue
    try:
        co2 = float(parts[2])
    except (ValueError, IndexError):
        return (ts, None, None, None, None, None, None, None)
    if len(parts) >= 6:
        # v3: date time CO2 T RH flag
        try:
            t = float(parts[3]); rh = float(parts[4]); flag = parts[5]
        except (ValueError, IndexError):
            t, rh, flag = None, None, None
    else:
        # v2: date time CO2 flag (no T/RH)
        t, rh = None, None
        flag = parts[3] if len(parts) >= 4 else None
    # P (BMP388): ultima colonna del formato v5 (…CO2RAW CO2RAWUC P). Sulle
    # righe vecchie (no P) l'ultimo token è un flag/CO2RAWUC: il float-guard
    # e il check MISSING evitano falsi positivi sulla riga live (sempre v5).
    # Formato colonne in coda: … CO2RAW CO2RAWUC P [FLOWmass FLOWvol].
    # Dal 2026-07-02 ci sono 2 colonne flusso IN CODA → la P non è più
    # l'ultima colonna ma la terz'ultima. Distinzione per numero di colonne.
    def _fnum(idx):
        try:
            v = float(parts[idx])
            return v if v != MISSING else None
        except (ValueError, IndexError):
            return None
    p = fmass = fvol = None
    def _is_float(s):
        try:
            float(s); return True
        except (ValueError, TypeError):
            return False
    # Colonne valvola presenti se parts[7] (valve_label, es. "pos1-ambient")
    # NON è un numero (senza valvola parts[7] sarebbe CO2RAWUC, un float).
    has_valve_cols = len(parts) >= 8 and not _is_float(parts[7])
    n_tail = len(parts) - (8 if has_valve_cols else 6)  # colonne dopo flag/valvola
    if n_tail >= 5:
        # …CO2RAW CO2RAWUC P FLOWmass FLOWvol → P a -3, flusso a -2/-1
        p = _fnum(-3); fmass = _fnum(-2); fvol = _fnum(-1)
    elif n_tail >= 3:
        # …CO2RAW CO2RAWUC P → P ultima colonna
        p = _fnum(-1)
    return (ts, co2, t, rh, flag, p, fmass, fvol)


def read_last_min_flow(cfg: configparser.ConfigParser, d: date_type):
    """Legge la media 1-min del flusso TSI dall'ultima riga del file `_min`.

    Le colonne flusso sono le ultime 4: FLOWmass FLOWmass_std FLOWvol
    FLOWvol_std → media massa = parts[-4], media volumetrico = parts[-2].
    Ritorna (fmass_avg|None, fvol_avg|None); None se file/valore mancante.
    """
    import glob
    ext = cfg.get("output", "extension", fallback="raw")
    ddir = get_data_dir(cfg)
    pattern = os.path.join(ddir, f"*_{d.strftime('%Y%m%d')}_p00_min.{ext}")
    matches = glob.glob(pattern)
    if not matches:
        return (None, None)
    path = max(matches, key=os.path.getmtime)
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return (None, None)
    lines = [ln.strip() for ln in tail.splitlines()
             if ln.strip() and not ln.startswith("#")]
    if not lines:
        return (None, None)
    parts = lines[-1].split()

    def _fnum(idx):
        try:
            v = float(parts[idx])
            return v if v != MISSING else None
        except (ValueError, IndexError):
            return None
    # Nuovo formato min con flusso ha >= 4 colonne extra in coda: FLOWmass
    # FLOWmass_std FLOWvol FLOWvol_std. Guard: serve un minimo di colonne.
    if len(parts) >= 12:
        return (_fnum(-4), _fnum(-2))
    return (None, None)


def _startup_log():
    """Stampa percorsi risolti all'avvio per diagnostica."""
    print("=" * 60)
    print("GMP343 Monitor v13 — percorsi risolti")
    print("=" * 60)
    print(f"  Installazione : {_INSTALL_DIR}")
    print(f"  Config utente : {CONFIG_DIR}")
    for label, path in [("SERIAL_INI", SERIAL_INI),
                        ("SITE_INI",   SITE_INI),
                        ("NAME_INI",   NAME_INI),
                        ("MONITOR_INI",MONITOR_INI),
                        ("SENSOR_IMG", SENSOR_IMG)]:
        exists = "✓" if os.path.exists(path) else "✗ MANCANTE"
        print(f"  {label:<12}: {path}  [{exists}]")

    cfg = configparser.ConfigParser()
    cfg.read([SERIAL_INI, SITE_INI, NAME_INI])
    ddir  = get_data_dir(cfg)
    today = datetime.utcnow().date()
    found = build_filename(cfg, today)
    print(f"  data_path    : {ddir}")
    if found:
        print(f"  File oggi    : {found}  [✓ TROVATO]")
    else:
        ext = cfg.get("output", "extension", fallback="raw")
        print(f"  File oggi    : {ddir}/*_{today.strftime('%Y%m%d')}_p00_min.{ext}  [✗ NON TROVATO]")
    print("=" * 60)
    print()


MISSING = -999.99


def read_file(path: str, metric: str = "mean"):
    """
    Legge un file dati `_min.raw` (1-min) o `_Nmin.raw` (10/30/60-min).
    Supporta v2/v3/v4:
      v2: date time CO2 CO2_std n flag [valve_pos valve_label]
      v3: date time CO2 CO2_std T T_std RH RH_std n flag [valve_pos valve_label]
      v4: date time CO2 CO2_std CO2_med T T_std T_med RH RH_std RH_med n flag [valve_pos valve_label]
          (file 60-min con mediane calcolate dai sample raw)

    `metric`:
      - "mean" (default): le colonne 'values'/'t'/'rh' contengono CO2/T/RH
        media (compatibile con tutto).
      - "median": per file v4 sostituisce le medie con le mediane;
        per file v2/v3 (no mediana disponibile) cade a mean.

    Per i file v2 T/RH sono restituiti come MISSING. Colonne valvola
    opzionali. Timestamp: YYYY-MM-DD HH:MM:SS.
    Ritorna: (times, values, stds, counts, flags, t, t_std, rh, rh_std,
              valve_pos, valve_labels)
    """
    times, values, stds, counts, flags = [], [], [], [], []
    ts_t, ts_tstd, ts_rh, ts_rhstd = [], [], [], []
    ts_p = []
    ts_fmass, ts_fvol = [], []   # flusso TSI (massa/volumetrico) — in coda
    valve_pos, valve_labels = [], []
    has_valve_cols = False
    if not path or not os.path.exists(path):
        return (times, values, stds, counts, flags,
                ts_t, ts_tstd, ts_rh, ts_rhstd, ts_p,
                valve_pos, valve_labels, ts_fmass, ts_fvol)
    # Il formato v4 (con MEDIANE) esiste SOLO nel file 60-min. Dal 2026-06-24
    # i file 1/10/30-min hanno 4 colonne CO2RAW/CO2RAWUC IN CODA che alzano
    # il conteggio colonne: senza questo gate verrebbero scambiati per v4.
    # Quindi il ramo median si attiva solo per "_60min." (gli altri restano v3).
    is_60min_file = "_60min." in os.path.basename(path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                if raw.startswith("#"):
                    continue
                p = raw.split()
                if len(p) < 3:
                    continue
                try:
                    dt  = datetime.strptime(f"{p[0]} {p[1]}", "%Y-%m-%d %H:%M:%S")
                    co2 = float(p[2])
                    # Discriminazione formato: v4 ha 11 colonne dati (CO2_std
                    # CO2_med T T_std T_med RH RH_std RH_med n flag), v3 ha 7
                    # (CO2_std T T_std RH RH_std n flag), v2 fino a 3.
                    remaining = len(p) - 3
                    if is_60min_file and remaining >= 10:
                        # v4: CO2_std CO2_med T T_std T_med RH RH_std RH_med n flag
                        co2_std    = float(p[3])
                        co2_median = float(p[4])
                        t_val      = float(p[5])
                        t_std      = float(p[6])
                        t_median   = float(p[7])
                        rh_val     = float(p[8])
                        rh_std     = float(p[9])
                        rh_median  = float(p[10])
                        n          = int(p[11])
                        flag       = p[12].lower() if len(p) >= 13 else "measure"
                        if metric == "median":
                            co2    = co2_median
                            t_val  = t_median
                            rh_val = rh_median
                        valve_idx = 13
                    elif remaining >= 7:
                        # v3: CO2_std T T_std RH RH_std n flag
                        co2_std = float(p[3])
                        t_val   = float(p[4])
                        t_std   = float(p[5])
                        rh_val  = float(p[6])
                        rh_std  = float(p[7])
                        n       = int(p[8])
                        flag    = p[9].lower() if len(p) >= 10 else "measure"
                        valve_idx = 10
                    else:
                        # v2: [CO2_std [n [flag]]]
                        co2_std = float(p[3]) if len(p) >= 4 else 0.0
                        n       = int(p[4])   if len(p) >= 5 else 1
                        flag    = p[5].lower() if len(p) >= 6 else "measure"
                        t_val, t_std, rh_val, rh_std = MISSING, MISSING, MISSING, MISSING
                        valve_idx = 6
                    if flag not in ("measure", "calib"):
                        flag = "measure"
                    if len(p) >= valve_idx + 2:
                        has_valve_cols = True
                        try:
                            vpos = int(p[valve_idx])
                        except ValueError:
                            vpos = -1
                        vlab = p[valve_idx + 1]
                    else:
                        vpos = -1
                        vlab = "-"
                    # P (BMP388) + flusso TSI: dal 2026-07-02 ci sono colonne
                    # flusso IN CODA, quindi NON si può più usare l'indice
                    # negativo (P non è più l'ultima colonna). Si calcola la
                    # posizione FORWARD dopo valvola + blocco CO2RAW/CO2RAWUC:
                    #   min/10/30:  CO2RAW CO2RAW_std CO2RAWUC CO2RAWUC_std P …
                    #               → P a tail_start+4, FLOWmass +6, FLOWvol +8
                    #   60-min:     …(con mediane)… P a tail_start+6,
                    #               FLOWmass +9, FLOWvol +12
                    tail_start = valve_idx + (2 if has_valve_cols else 0)
                    if is_60min_file:
                        p_pos, fm_pos, fv_pos = (tail_start + 6,
                                                 tail_start + 9, tail_start + 12)
                    else:
                        p_pos, fm_pos, fv_pos = (tail_start + 4,
                                                 tail_start + 6, tail_start + 8)

                    def _col(idx):
                        try:
                            return float(p[idx])
                        except (ValueError, IndexError):
                            return MISSING
                    p_val     = _col(p_pos)
                    fmass_val = _col(fm_pos)
                    fvol_val  = _col(fv_pos)
                    times.append(dt)
                    values.append(co2)
                    stds.append(co2_std)
                    counts.append(n)
                    flags.append(flag)
                    ts_t.append(t_val)
                    ts_tstd.append(t_std)
                    ts_rh.append(rh_val)
                    ts_rhstd.append(rh_std)
                    ts_p.append(p_val)
                    ts_fmass.append(fmass_val)
                    ts_fvol.append(fvol_val)
                    valve_pos.append(vpos)
                    valve_labels.append(vlab)
                except ValueError:
                    continue
    except OSError:
        pass
    if not has_valve_cols:
        valve_pos, valve_labels = [], []
    return (times, values, stds, counts, flags,
            ts_t, ts_tstd, ts_rh, ts_rhstd, ts_p,
            valve_pos, valve_labels, ts_fmass, ts_fvol)


def load_period(cfg: configparser.ConfigParser,
                start: date_type, n_days: int,
                suffix: str = "min",
                metric: str = "mean"):
    """
    Carica n_days giorni a partire da start dalla finestra `suffix`
    (default `min` = medie 1 minuto). `metric="median"` ritorna le mediane
    al posto delle medie (solo per file v4, es. `_60min.raw`). Per file
    v2/v3 cade silenziosamente a mean (non c'è la colonna mediana).
    Tuple: (times, values, stds, counts, flags, t, t_std, rh, rh_std,
            valve_pos, valve_labels)
    valve_pos/valve_labels sono array vuoti se nessun file ha colonne valvola.
    """
    all_t, all_v, all_s, all_c, all_f = [], [], [], [], []
    all_tt, all_tstd, all_rh, all_rhstd = [], [], [], []
    all_tp = []
    all_fm, all_fv = [], []   # flusso TSI (massa/volumetrico)
    all_vp, all_vl = [], []
    n_with_valve = 0
    for i in range(n_days):
        d = start + timedelta(days=i)
        t, v, s, c, f, tt, tstd, rh, rhstd, tp, vp, vl, fm, fv = read_file(
            build_filename(cfg, d, suffix), metric=metric)
        all_t.extend(t)
        all_v.extend(v)
        all_s.extend(s)
        all_c.extend(c)
        all_f.extend(f)
        all_tt.extend(tt)
        all_tstd.extend(tstd)
        all_rh.extend(rh)
        all_rhstd.extend(rhstd)
        all_tp.extend(tp)
        all_fm.extend(fm)
        all_fv.extend(fv)
        if vp:
            all_vp.extend(vp)
            all_vl.extend(vl)
            n_with_valve += 1
        else:
            all_vp.extend([-1] * len(t))
            all_vl.extend(["-"] * len(t))
    if not all_t:
        return None
    idx     = np.argsort(all_t)
    times   = np.array(all_t)[idx]
    values  = np.array(all_v, dtype=float)[idx]
    stds    = np.array(all_s, dtype=float)[idx]
    counts  = np.array(all_c, dtype=int)[idx]
    flags   = np.array(all_f)[idx]
    t_arr   = np.array(all_tt,    dtype=float)[idx]
    tstd    = np.array(all_tstd,  dtype=float)[idx]
    rh_arr  = np.array(all_rh,    dtype=float)[idx]
    rhstd   = np.array(all_rhstd, dtype=float)[idx]
    p_arr   = np.array(all_tp,    dtype=float)[idx] if all_tp else np.array([], dtype=float)
    fm_arr  = np.array(all_fm,    dtype=float)[idx] if all_fm else np.array([], dtype=float)
    fv_arr  = np.array(all_fv,    dtype=float)[idx] if all_fv else np.array([], dtype=float)
    if n_with_valve > 0:
        valve_pos    = np.array(all_vp, dtype=int)[idx]
        valve_labels = np.array(all_vl)[idx]
    else:
        valve_pos    = np.array([], dtype=int)
        valve_labels = np.array([], dtype=str)
    return (times, values, stds, counts, flags,
            t_arr, tstd, rh_arr, rhstd, p_arr,
            valve_pos, valve_labels, fm_arr, fv_arr)


def day_xlim(d: date_type):
    """Restituisce (x0, x1) in numdate per la giornata d (00:00–24:00)."""
    x0 = mdates.date2num(datetime.combine(d, datetime.min.time()))
    x1 = x0 + 1.0
    return x0, x1


def smart_ylim(values, min_range=MIN_Y_RANGE, fallback=(0.0, 500.0)):
    """
    Calcola ylim con margine, ignorando MISSING e NaN.
    min_range: range Y minimo (ppm per CO2). Su T/RH passare valore più piccolo.
    fallback: (lo, hi) se nessun dato valido (default adatto a CO2).
    """
    arr = np.asarray(values, dtype=float)
    arr = arr[(arr != MISSING) & ~np.isnan(arr)]
    if len(arr) == 0:
        return fallback
    lo, hi = float(np.min(arr)), float(np.max(arr))
    span = hi - lo
    if span < min_range:
        mid = (lo + hi) / 2
        lo, hi = mid - min_range / 2, mid + min_range / 2
    else:
        lo -= span * Y_MARGIN_FACTOR
        hi += span * Y_MARGIN_FACTOR
    return lo, hi


# ══════════════════════════════════════════════════════════════════════════════
#  Calcolo zona notturna
# ══════════════════════════════════════════════════════════════════════════════

def night_spans(cfg: configparser.ConfigParser, days: list):
    """
    Restituisce lista di (dawn_num, dusk_num) per ogni giorno in days.
    Usa astral se disponibile; altrimenti 06:00–18:00 fisso.
    """
    spans = []
    if ASTRAL_OK:
        try:
            loc = LocationInfo()
            loc.name      = cfg.get("location", "name",      fallback="Site")
            loc.latitude  = cfg.getfloat("location", "latitude",  fallback=0.0)
            loc.longitude = cfg.getfloat("location", "longitude", fallback=0.0)
            loc.timezone  = cfg.get("location", "timezone",   fallback="UTC")
            tz = pytz.timezone(loc.timezone)
        except Exception:
            loc = None
    else:
        loc = None

    for d in days:
        try:
            if loc is not None:
                s     = sun(loc.observer, date=d, tzinfo=tz)
                dawn  = s["sunrise"].replace(tzinfo=None)
                dusk  = s["sunset"].replace(tzinfo=None)
            else:
                dawn = datetime.combine(d, datetime.min.time()) + timedelta(hours=6)
                dusk = datetime.combine(d, datetime.min.time()) + timedelta(hours=18)
            spans.append((mdates.date2num(dawn), mdates.date2num(dusk)))
        except Exception:
            dawn = datetime.combine(d, datetime.min.time()) + timedelta(hours=6)
            dusk = datetime.combine(d, datetime.min.time()) + timedelta(hours=18)
            spans.append((mdates.date2num(dawn), mdates.date2num(dusk)))
    return spans


# ══════════════════════════════════════════════════════════════════════════════
#  Widget grafico
# ══════════════════════════════════════════════════════════════════════════════

class GraphWidget(QWidget):
    """
    Widget autonomo con Figure + toolbar + controlli.
    Gestisce tutto ciò che riguarda il grafico.
    """

    def __init__(self, cfg: configparser.ConfigParser, parent=None):
        super().__init__(parent)
        self.cfg         = cfg
        self._night_poly = []   # patch zone notturne
        self._valve_poly = []   # patch posizione valvola (striscia bassa)
        self._valve_text = []   # label testuali "pos=N" lungo la striscia
        self._sc_by_pos     = []   # PathCollection scatter per posizione valvola
        self._pos_legend    = None # Legend handle (rimosso/ricreato a ogni reload)
        self._errorbar      = None # ErrorbarContainer per σ sul grafico CO₂
        self._errorbar_t    = None # ErrorbarContainer per σ sul grafico T
        self._errorbar_rh   = None # ErrorbarContainer per σ sul grafico RH
        self._zoom_xlim  = None # None = vista libera
        self._zoom_ylim  = None

        self._build_ui()
        self._init_axes()

        # Carica subito
        self._reload()

    # ── costruzione UI ────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        # ── riga controlli ────────────────────────────────────────────────────
        bar = QHBoxLayout()

        bar.addWidget(QLabel("Period:"))
        self.combo = QComboBox()
        self.combo.addItems(["24h", "48h", "7 days", "Custom"])
        self.combo.currentTextChanged.connect(self._on_period_change)
        bar.addWidget(self.combo)

        self.lbl_from = QLabel("From:")
        bar.addWidget(self.lbl_from)
        self.date_from = QDateEdit(QDate.currentDate().addDays(-1))
        self.date_from.setCalendarPopup(True)
        self.date_from.dateChanged.connect(self._reload)
        bar.addWidget(self.date_from)

        self.lbl_to = QLabel("To:")
        bar.addWidget(self.lbl_to)
        self.date_to = QDateEdit(QDate.currentDate())
        self.date_to.setCalendarPopup(True)
        self.date_to.dateChanged.connect(self._reload)
        bar.addWidget(self.date_to)

        self._toggle_custom(False)

        if ASTRAL_OK:
            self.chk_night = QCheckBox("Night zones")
            self.chk_night.setChecked(True)
            self.chk_night.stateChanged.connect(self._reload)
            bar.addWidget(self.chk_night)

        # Checkbox posizione valvola (striscia colorata in basso).
        # Default ON: se i dati non la contengono, il disegno è automaticamente
        # no-op (retrocompat con file _min.raw storici a 6 colonne).
        self.chk_valve = QCheckBox("Valve position")
        self.chk_valve.setChecked(True)
        self.chk_valve.setToolTip(
            "Show a colored strip at the bottom with the VICI valve\n"
            "position (requires integration.ini enabled).")
        self.chk_valve.stateChanged.connect(self._reload)
        bar.addWidget(self.chk_valve)

        # Window selector: 4 checkboxes (1m / 10m / 30m / 60m). Multi-select.
        # Default state from monitor.ini → [graph] → windows.
        bar.addSpacing(8)
        bar.addWidget(QLabel("Show:"))
        self.chk_windows = {}
        # gs_cfg loaded later in _init_axes; here we read once standalone
        gcp_init = configparser.ConfigParser()
        if os.path.exists(MONITOR_INI):
            gcp_init.read(MONITOR_INI)
        wraw_init = gcp_init.get("graph", "windows", fallback="1m").strip()
        wset_init = {w.strip() for w in wraw_init.split(",")
                     if w.strip() in {"1m", "10m", "30m", "60m", "60m-med"}} or {"1m"}
        _wlabels = {"1m": "1m", "10m": "10m", "30m": "30m",
                    "60m": "60m", "60m-med": "60m med"}
        _wtips = {
            "1m":      "1-min average (raw rate)",
            "10m":     "10-min average (pooled from 1-min)",
            "30m":     "30-min average (pooled from 1-min)",
            "60m":     "60-min mean — computed from raw samples",
            "60m-med": "60-min MEDIAN — computed from raw samples (robust to outliers)",
        }
        for w in ("1m", "10m", "30m", "60m", "60m-med"):
            cb = QCheckBox(_wlabels[w])
            cb.setChecked(w in wset_init)
            cb.setToolTip(_wtips[w])
            cb.stateChanged.connect(self._on_windows_changed)
            bar.addWidget(cb)
            self.chk_windows[w] = cb

        btn_home = QPushButton("⌂ Home")
        btn_home.setToolTip("Reset to full view")
        btn_home.clicked.connect(self._reset_view)
        bar.addWidget(btn_home)

        # — Live readouts (live, 1-min avg, σ) on the right of Home —
        bar.addSpacing(10)
        sep_live = QFrame(); sep_live.setFrameShape(QFrame.VLine); sep_live.setFrameShadow(QFrame.Sunken)
        bar.addWidget(sep_live)
        bar.addSpacing(6)
        lbl_live_cap = QLabel("Live:")
        lbl_live_cap.setStyleSheet("color:#666;font-weight:bold")
        bar.addWidget(lbl_live_cap)
        self.lbl_chart_live = QLabel("--- ppm")
        self.lbl_chart_live.setFont(QFont("Arial", 11, QFont.Bold))
        self.lbl_chart_live.setStyleSheet("color:#0066cc")
        self.lbl_chart_live.setToolTip("Last raw sample (~1 Hz from .raw)")
        bar.addWidget(self.lbl_chart_live)
        bar.addSpacing(10)
        lbl_avg_cap = QLabel("1-min avg:")
        lbl_avg_cap.setStyleSheet("color:#666;font-weight:bold")
        bar.addWidget(lbl_avg_cap)
        self.lbl_chart_avg = QLabel("--- ppm")
        self.lbl_chart_avg.setFont(QFont("Arial", 11, QFont.Bold))
        self.lbl_chart_avg.setStyleSheet("color:#444")
        self.lbl_chart_avg.setToolTip("Last 1-min average (from _min.raw)")
        bar.addWidget(self.lbl_chart_avg)
        bar.addSpacing(10)
        lbl_std_cap = QLabel("σ:")
        lbl_std_cap.setStyleSheet("color:#666;font-weight:bold")
        bar.addWidget(lbl_std_cap)
        self.lbl_chart_std = QLabel("---")
        self.lbl_chart_std.setStyleSheet("color:#666")
        self.lbl_chart_std.setToolTip("Std dev of the last 1-min average")
        bar.addWidget(self.lbl_chart_std)

        # Compensazione P/RH inviata alla sonda (da status.json, vedi _refresh_comp_label)
        self.lbl_comp = QLabel("")
        self.lbl_comp.setStyleSheet("color:#0a7d36;font-weight:bold")
        self.lbl_comp.setToolTip(
            "Values sent to the GMP343 for its internal compensation "
            "(RH from the SHT3X, P fixed or from the BMP388). Temperature is internal to the probe.")
        bar.addWidget(self.lbl_comp)

        bar.addStretch()
        root.addLayout(bar)

        # ── striscia legenda finestre (sopra il grafico) ──────────────────
        # Mostra "■ Nm" per ogni finestra abilitata; usa il colore CO₂ della
        # finestra. Si aggiorna in self._refresh_legend_strip() chiamata da
        # _on_windows_changed e a fine _reload.
        self._legend_bar = QHBoxLayout()
        self._legend_bar.setContentsMargins(8, 0, 8, 0)
        self._legend_bar.setSpacing(12)
        self._legend_bar.addStretch()  # placeholder per primo build
        root.addLayout(self._legend_bar)

        # ── figura ────────────────────────────────────────────────────────────
        self.fig = Figure(facecolor="white")
        # tight_layout gestisce automaticamente i margini incluso asse Y
        self.fig.set_tight_layout({"pad": 2.5, "h_pad": 1.5, "w_pad": 1.5})
        self.canvas = FigureCanvas(self.fig)
        self.canvas.setSizePolicy(
            self.canvas.sizePolicy().Expanding,
            self.canvas.sizePolicy().Expanding
        )
        # Ricalcola layout ad ogni resize della finestra
        self.canvas.mpl_connect("resize_event", lambda e: self.fig.tight_layout(pad=2.5))

        toolbar = NavToolbar(self.canvas, self)
        root.addWidget(toolbar)
        root.addWidget(self.canvas)

    # Default per-window styling — riferimento alla costante module-level
    # (la stessa usata dal dialog Appearance per gli stessi default).
    _WINDOW_DEFAULTS = MonitorWindow_PER_WINDOW_DEFAULTS
    _LINESTYLES = ("-", "--", ":", "-.")

    def _load_graph_style(self):
        """Legge da monitor.ini la sezione [graph] con default sicuri.

        Restituisce un dict con:
          - co2 line/scatter: style, point_size, line_width
          - T/RH line/scatter: trh_style, trh_point_size, trh_line_width
          - windows: set delle finestre temporali abilitate
          - per_window: dict {window: {color_co2, color_t, color_rh,
                                        linestyle, line_width, alpha}}
        """
        gcp = configparser.ConfigParser()
        if os.path.exists(MONITOR_INI):
            gcp.read(MONITOR_INI)

        def _norm_style(s):
            s = (s or "").strip().lower()
            return s if s in ("lines+points", "lines", "points") else "lines+points"

        wraw = gcp.get("graph", "windows", fallback="1m").strip()
        valid = {"1m", "10m", "30m", "60m", "60m-med"}
        wset = {w.strip() for w in wraw.split(",") if w.strip() in valid}
        if not wset:
            wset = {"1m"}

        # Per-window: legge le chiavi <attr>_<window> con fallback al default
        per_window = {}
        for w, defaults in self._WINDOW_DEFAULTS.items():
            ls = gcp.get("graph", f"linestyle_{w}",
                         fallback=defaults["linestyle"]).strip()
            if ls not in self._LINESTYLES:
                ls = defaults["linestyle"]
            per_window[w] = {
                "color_co2":  gcp.get("graph", f"color_co2_{w}",
                                      fallback=defaults["color_co2"]).strip(),
                "color_t":    gcp.get("graph", f"color_t_{w}",
                                      fallback=defaults["color_t"]).strip(),
                "color_rh":   gcp.get("graph", f"color_rh_{w}",
                                      fallback=defaults["color_rh"]).strip(),
                "linestyle":  ls,
                "line_width": max(0.2, gcp.getfloat(
                    "graph", f"line_width_{w}", fallback=defaults["line_width"])),
                "alpha":      min(1.0, max(0.05, gcp.getfloat(
                    "graph", f"alpha_{w}", fallback=defaults["alpha"]))),
            }

        return {
            "style":           _norm_style(gcp.get("graph", "style", fallback="lines+points")),
            "point_size":      max(2, gcp.getint("graph", "point_size", fallback=18)),
            "line_width":      max(0.2, gcp.getfloat("graph", "line_width", fallback=1.0)),
            "trh_style":       _norm_style(gcp.get("graph", "trh_style", fallback="lines")),
            "trh_point_size":  max(2, gcp.getint("graph", "trh_point_size", fallback=10)),
            "trh_line_width":  max(0.2, gcp.getfloat("graph", "trh_line_width", fallback=1.0)),
            "windows":         wset,
            "per_window":      per_window,
        }

    def _init_axes(self):
        # Stile da monitor.ini → applicato a line/scatter alla creazione
        gs_cfg = self._load_graph_style()
        self._graph_style    = gs_cfg["style"]
        self._point_size     = gs_cfg["point_size"]
        self._line_width     = gs_cfg["line_width"]
        self._trh_style      = gs_cfg["trh_style"]
        self._trh_point_size = gs_cfg["trh_point_size"]
        self._trh_line_width = gs_cfg["trh_line_width"]
        self._enabled_windows = gs_cfg["windows"]   # set
        self._per_window      = gs_cfg["per_window"]
        # Lista artist matplotlib creati per le finestre non-primarie;
        # ricreati a ogni reload e ripuliti qui sotto.
        self._overlay_lines = []

        # 4 subplots con asse X condiviso: CO2 (grande sopra), P, FLUSSO TSI,
        # T+RH (piccolo sotto). Asse tempo sincronizzato (sharex) su tutti.
        gs = self.fig.add_gridspec(4, 1, height_ratios=[3, 1, 1, 1], hspace=0.08)
        self.ax      = self.fig.add_subplot(gs[0])
        self.ax_p    = self.fig.add_subplot(gs[1], sharex=self.ax)   # P (BMP388)
        self.ax_flow = self.fig.add_subplot(gs[2], sharex=self.ax)   # flusso TSI
        self.ax_t    = self.fig.add_subplot(gs[3], sharex=self.ax)
        # Asse Y secondario (a destra) per RH sul pannello di T
        self.ax_rh = self.ax_t.twinx()

        # Tick label X solo sul pannello in basso (ax_t): gli altri li nascondono
        self.ax.tick_params(labelbottom=False)
        self.ax_p.tick_params(labelbottom=False)
        self.ax_flow.tick_params(labelbottom=False)

        # ── Flusso TSI 4140 (pannello tra P e T/RH): massa + volumetrico ──
        self.line_fmass, = self.ax_flow.plot(
            [], [], "-", linewidth=self._trh_line_width,
            color="#8a0a8a", zorder=2, label="mass (SLPM)")
        self.line_fvol,  = self.ax_flow.plot(
            [], [], "-", linewidth=self._trh_line_width,
            color="#a83232", zorder=2, label="vol (Lpm)")
        self.ax_flow.set_ylabel("Flow (L/min)", fontsize=9, color="#8a0a8a")
        self.ax_flow.tick_params(axis="y", labelcolor="#8a0a8a", labelsize=8)
        self.ax_flow.grid(True, linestyle="--", linewidth=0.3, alpha=0.6)
        self.ax_flow.legend(loc="upper right", fontsize=7, framealpha=0.8, ncol=2)

        # ── P (pannello pressione, tra CO2 e T/RH) ────────────────────────
        self.line_p, = self.ax_p.plot(
            [], [], "-", linewidth=self._trh_line_width,
            color="#5030a0", zorder=2, label="P"
        )
        self.sc_p = self.ax_p.scatter(
            [], [], s=self._trh_point_size, color="#5030a0", zorder=3, label="P"
        )
        self.ax_p.set_ylabel("P (hPa)", fontsize=9, color="#5030a0")
        self.ax_p.tick_params(axis="y", labelcolor="#5030a0", labelsize=8)
        self.ax_p.grid(True, linestyle="--", linewidth=0.3, alpha=0.6)

        # ── CO2 (pannello principale) ─────────────────────────────────────
        self.line, = self.ax.plot(
            [], [], "-",
            linewidth=self._line_width,
            color="#2060c0", zorder=2
        )
        # Scatter punti MEASURE (blu) e CALIB (arancione) — sopra la linea
        self.sc_measure = self.ax.scatter(
            [], [], s=self._point_size, color="#2060c0",
            zorder=3, label="measure"
        )
        self.sc_calib = self.ax.scatter(
            [], [], s=self._point_size + 10, color="#e06000",
            zorder=4, marker="D", label="calib"
        )

        # ── T e RH (pannello piccolo sotto, twin Y) ──────────────────────
        # T sull'asse Y sinistro (arancione)
        self.line_t, = self.ax_t.plot(
            [], [], "-",
            linewidth=self._trh_line_width,
            color="#c05000", zorder=2, label="T"
        )
        # Scatter T (stessi colori della linea, sopra)
        self.sc_t = self.ax_t.scatter(
            [], [], s=self._trh_point_size, color="#c05000",
            zorder=3, label="T"
        )
        self.ax_t.set_ylabel("T (°C)", fontsize=9, color="#c05000")
        self.ax_t.tick_params(axis="y", labelcolor="#c05000", labelsize=8)
        self.ax_t.grid(True, linestyle="--", linewidth=0.3, alpha=0.6)

        # RH sull'asse Y destro (verde-teal)
        self.line_rh, = self.ax_rh.plot(
            [], [], "-",
            linewidth=self._trh_line_width,
            color="#007060", zorder=2, label="RH"
        )
        # Scatter RH (stessi colori della linea, sopra)
        self.sc_rh = self.ax_rh.scatter(
            [], [], s=self._trh_point_size, color="#007060",
            zorder=3, label="RH"
        )
        self.ax_rh.set_ylabel("RH (%)", fontsize=9, color="#007060")
        self.ax_rh.tick_params(axis="y", labelcolor="#007060", labelsize=8)
        # Punto hover
        self.hl_pt, = self.ax.plot(
            [], [], "o",
            markersize=9,
            markerfacecolor="red",
            markeredgecolor="#800000",
            markeredgewidth=1.5,
            zorder=10, visible=False
        )

        # Tooltip
        self.annot = self.ax.annotate(
            "", xy=(0, 0),
            xycoords="data",
            xytext=(0.02, 0.95),
            textcoords="axes fraction",
            bbox=dict(boxstyle="round,pad=0.5",
                      fc="lightyellow", ec="black", lw=1.2, alpha=0.95),
            arrowprops=dict(arrowstyle="->", lw=1.2, color="black"),
            fontsize=9, fontweight="bold", visible=False
        )

        # Label MEASURE / CALIB — fuori dall'area di plot, a destra del titolo
        # Usa fig.text con transform figura: non dipende dai limiti degli assi
        self.flag_label = self.fig.text(
            0.99, 0.97, "MEASURE",
            ha="right", va="top",
            transform=self.fig.transFigure,
            fontsize=10, fontweight="bold",
            color="#2060c0",
            bbox=dict(boxstyle="round,pad=0.3",
                      fc="white", ec="#2060c0", lw=1.5, alpha=0.9)
        )

        self.ax.set_ylabel("CO2 (comp.) 1min [PPM]", fontsize=10)
        self.ax.grid(True, linestyle="--", linewidth=0.3, alpha=0.6)
        # xlabel va sul pannello inferiore (ax_t è il bottom visibile)
        self.ax_t.set_xlabel("Ora (UTC)", fontsize=10)

        # Connetti eventi
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)

        self._ignore_lim_change = False
        self.ax.callbacks.connect("xlim_changed", self._on_lim_changed)
        self.ax.callbacks.connect("ylim_changed", self._on_lim_changed)

        # Applica visibilità in base allo stile (lines / points / lines+points)
        self._apply_graph_style()
        # Legenda iniziale (se la striscia è già stata costruita in _build_ui)
        if hasattr(self, "_legend_bar"):
            self._refresh_legend_strip()

    def _apply_graph_style(self):
        """Show/hide line+scatter for CO₂ and T/RH per their own style."""
        # CO₂ (pannello superiore) → self._graph_style
        co2_lines  = self._graph_style != "points"
        co2_points = self._graph_style != "lines"
        self.line.set_visible(co2_lines)
        for a in (self.sc_measure, self.sc_calib):
            a.set_visible(co2_points)
        for a in self._sc_by_pos:
            a.set_visible(co2_points)
        # T/RH (pannello inferiore) → self._trh_style
        trh_lines  = self._trh_style != "points"
        trh_points = self._trh_style != "lines"
        for a in (self.line_t, self.line_rh):
            a.set_visible(trh_lines)
        for a in (self.sc_t, self.sc_rh):
            a.set_visible(trh_points)

    # ── helper periodo ────────────────────────────────────────────────────────

    def _toggle_custom(self, show: bool):
        self.lbl_from.setVisible(show)
        self.date_from.setVisible(show)
        self.lbl_to.setVisible(show)
        self.date_to.setVisible(show)

    def _on_period_change(self, txt):
        self._toggle_custom(txt == "Custom")
        self._reload()

    def _period_range(self):
        """Restituisce (start_date, n_days)."""
        txt = self.combo.currentText()
        today = datetime.utcnow().date()
        if txt == "24h":
            return today, 1
        elif txt == "48h":
            return today - timedelta(days=1), 2
        elif txt == "7 days":
            return today - timedelta(days=6), 7
        else:  # Personalizzato
            d0 = self.date_from.date().toPyDate()
            d1 = self.date_to.date().toPyDate()
            n  = max(1, (d1 - d0).days + 1)
            return d0, n

    # ── caricamento e disegno ─────────────────────────────────────────────────

    def _reload(self):
        """Ricarica dati e ridisegna. Preserva zoom se attivo."""
        start, n_days = self._period_range()
        # Primary window (smallest selected): drives the main line + scatter +
        # errorbars. Other selected windows are added as overlay lines.
        # 60m-med (median) is treated as "wider than 60m" in the priority so
        # if both 60m and 60m-med are checked, 60m mean is the primary.
        _priority = MonitorWindow_PRIORITY
        primary_w = next((w for w in _priority if w in self._enabled_windows),
                         "1m")
        suf, met = MonitorWindow_SUFFIX_METRIC[primary_w]
        result = load_period(self.cfg, start, n_days,
                             suffix=suf, metric=met)

        # Salva il dict per-window del primary; lo applichiamo agli artist
        # DOPO set_data/set_offsets (vedi _apply_primary_window_style sotto).
        ws_p = self._per_window[primary_w]

        self._ignore_lim_change = True  # evita loop callback

        # ── Pulisci zone notturne ──────────────────────────────────────────
        for p in self._night_poly:
            try:
                p.remove()
            except Exception:
                pass
        self._night_poly = []

        # ── Pulisci patch e label valvola dal ciclo precedente ─────────────
        for p in self._valve_poly:
            try:
                p.remove()
            except Exception:
                pass
        self._valve_poly = []
        for t in self._valve_text:
            try:
                t.remove()
            except Exception:
                pass
        self._valve_text = []

        # ── Pulisci scatter colorati per posizione e legenda ───────────────
        for sc in self._sc_by_pos:
            try:
                sc.remove()
            except Exception:
                pass
        self._sc_by_pos = []
        if self._pos_legend is not None:
            try:
                self._pos_legend.remove()
            except Exception:
                pass
            self._pos_legend = None

        # ── Pulisci errorbar precedenti (CO₂, T, RH) ─────────────────────
        # LEAK FIX (2026-06-28): errorbar() registra un ErrorbarContainer in
        # ax.containers. Rimuovere SOLO i figli (line/caps/barlinecols) e fare
        # self._errorbar=None NON toglie il container dalla lista: resta vivo e
        # trattiene i barlinecols (LineCollection con Path/MaskedArray dei dati)
        # → crescita RAM ~MB/tick (3 errorbar × ogni 5s, ax.containers +3/tick).
        # In matplotlib 3.6 Container.remove() NON stacca da ax.containers, quindi
        # NON ci si affida a eb.remove(): si rimuovono i figli E si filtra il
        # container da ax.containers PER IDENTITÀ (== su tuple matcherebbe per
        # contenuto). Questo azzera la crescita di ax.containers.
        def _remove_eb(eb):
            if eb is None:
                return
            try:
                children = [eb[0]] if eb[0] is not None else []
                children += list(eb[1]) + list(eb[2])   # caplines + barlinecols
                for art in children:
                    try:
                        art.remove()
                    except Exception:
                        pass
            except Exception:
                pass
            # Stacca SEMPRE il container da ax.containers (per identità)
            for axx in (self.ax, self.ax_t, self.ax_rh):
                try:
                    axx.containers[:] = [c for c in axx.containers if c is not eb]
                except Exception:
                    pass

        _remove_eb(self._errorbar);    self._errorbar = None
        _remove_eb(self._errorbar_t);  self._errorbar_t = None
        _remove_eb(self._errorbar_rh); self._errorbar_rh = None

        # Pulisci linee overlay delle finestre non-primarie (ricreate sotto)
        for ln in self._overlay_lines:
            try:
                ln.remove()
            except Exception:
                pass
        self._overlay_lines = []

        if result is None:
            self.line.set_data([], [])
            self.line_t.set_data([], [])
            self.line_rh.set_data([], [])
            self.line_p.set_data([], [])
            self.line_fmass.set_data([], [])
            self.line_fvol.set_data([], [])
            self.sc_measure.set_offsets(np.empty((0, 2)))
            self.sc_calib.set_offsets(np.empty((0, 2)))
            self.sc_t.set_offsets(np.empty((0, 2)))
            self.sc_rh.set_offsets(np.empty((0, 2)))
            self.sc_p.set_offsets(np.empty((0, 2)))
            self.flag_label.set_text("MEASURE")
            self.flag_label.set_color("#2060c0")
            self.flag_label.get_bbox_patch().set_edgecolor("#2060c0")
            self.ax.set_title("No data available", fontsize=11, loc="left")
            self._set_x_axis(start, n_days)
            self.canvas.draw_idle()
            self._ignore_lim_change = False
            return

        (times, values, stds, counts, flags,
         t_arr, t_std_arr, rh_arr, rh_std_arr, p_arr,
         valve_pos, valve_labels, fmass_arr, fvol_arr) = result
        xt = mdates.date2num(times)
        # Sostituisci MISSING con NaN per i plot (break nella linea, no Y axis esteso)
        values_plot = np.where(values == MISSING, np.nan, values)
        t_plot      = np.where(t_arr  == MISSING, np.nan, t_arr)
        rh_plot     = np.where(rh_arr == MISSING, np.nan, rh_arr)
        # P può essere array vuoto (file pre-v6 o senza P): allinea a NaN.
        if p_arr.size == values.size:
            p_plot = np.where(p_arr == MISSING, np.nan, p_arr)
        else:
            p_arr  = np.full(values.size, MISSING)
            p_plot = np.full(values.size, np.nan)
        # Salva per accesso dal tooltip
        self._t_plot  = t_plot
        self._rh_plot = rh_plot
        self._p_plot  = p_plot

        # ── Linee continue ────────────────────────────────────────────────
        self.line.set_data(xt, values_plot)
        self.line_t.set_data(xt, t_plot)
        self.line_rh.set_data(xt, rh_plot)
        self.line_p.set_data(xt, p_plot)
        # Flusso TSI (massa + volumetrico): MISSING → NaN per spezzare la linea
        if fmass_arr.size == values.size:
            fmass_plot = np.where(fmass_arr == MISSING, np.nan, fmass_arr)
        else:
            fmass_arr  = np.full(values.size, MISSING)
            fmass_plot = np.full(values.size, np.nan)
        if fvol_arr.size == values.size:
            fvol_plot = np.where(fvol_arr == MISSING, np.nan, fvol_arr)
        else:
            fvol_arr  = np.full(values.size, MISSING)
            fvol_plot = np.full(values.size, np.nan)
        self.line_fmass.set_data(xt, fmass_plot)
        self.line_fvol.set_data(xt, fvol_plot)
        # Salva per accesso dal tooltip (flusso massa/volumetrico)
        self._fmass_plot = fmass_plot
        self._fvol_plot  = fvol_plot

        # ── Scatter punti T, RH e P (filtra MISSING) ──────────────────────
        mask_t  = t_arr  != MISSING
        mask_rh = rh_arr != MISSING
        mask_p  = p_arr  != MISSING
        if mask_t.any():
            self.sc_t.set_offsets(np.column_stack([xt[mask_t], t_arr[mask_t]]))
        else:
            self.sc_t.set_offsets(np.empty((0, 2)))
        if mask_rh.any():
            self.sc_rh.set_offsets(np.column_stack([xt[mask_rh], rh_arr[mask_rh]]))
        else:
            self.sc_rh.set_offsets(np.empty((0, 2)))
        if mask_p.any():
            self.sc_p.set_offsets(np.column_stack([xt[mask_p], p_arr[mask_p]]))
        else:
            self.sc_p.set_offsets(np.empty((0, 2)))

        # ── Applica styling per-window al PRIMARY (DOPO set_data/set_offsets).
        # Necessario in questo ordine perché set_facecolor/set_edgecolor su
        # PathCollection può non propagare se chiamato prima di set_offsets.
        # Forziamo alpha=1.0 per il primary (gli alpha < 1 servono per gli
        # overlay non per la traccia principale).
        self.line.set_color(ws_p["color_co2"])
        self.line.set_linestyle(ws_p["linestyle"])
        self.line.set_linewidth(ws_p["line_width"])
        self.line.set_alpha(1.0)
        self.sc_measure.set_facecolor(ws_p["color_co2"])
        self.sc_measure.set_edgecolor(ws_p["color_co2"])
        # sc_calib resta arancione: ha semantica fissa (calibration diamond)

        self.line_t.set_color(ws_p["color_t"])
        self.line_t.set_linestyle(ws_p["linestyle"])
        self.line_t.set_linewidth(ws_p["line_width"])
        self.line_t.set_alpha(1.0)
        self.sc_t.set_facecolor(ws_p["color_t"])
        self.sc_t.set_edgecolor(ws_p["color_t"])

        self.line_rh.set_color(ws_p["color_rh"])
        self.line_rh.set_linestyle(ws_p["linestyle"])
        self.line_rh.set_linewidth(ws_p["line_width"])
        self.line_rh.set_alpha(1.0)
        self.sc_rh.set_facecolor(ws_p["color_rh"])
        self.sc_rh.set_edgecolor(ws_p["color_rh"])

        # ── Scatter dei punti CO₂ ─────────────────────────────────────────
        # Se i file hanno colonne valvola (formato "v3+valvola"), coloriamo
        # ogni punto in base alla posizione valvola con palette tab20.
        # Altrimenti retrocompatibilità: blu per measure, arancione per calib.
        mask_valid = values != MISSING
        has_valve_data = (valve_pos.size == len(times)
                          and np.any(valve_pos >= 1))

        if has_valve_data:
            # Nascondi gli scatter legacy (flag-based)
            self.sc_measure.set_offsets(np.empty((0, 2)))
            self.sc_calib.set_offsets(np.empty((0, 2)))

            from matplotlib import cm as _cm
            cmap = _cm.get_cmap("tab20")
            unique_pos = sorted({int(p) for p in valve_pos
                                 if int(p) >= 1})
            legend_handles = []
            for pos in unique_pos:
                mask = mask_valid & (valve_pos == pos)
                if not mask.any():
                    continue
                color = cmap((pos - 1) % 20)
                # Etichetta: prima label non vuota incontrata per quella posizione
                label_for_pos = ""
                for vl in valve_labels[mask]:
                    s = str(vl)
                    if s and s != "-":
                        label_for_pos = s
                        break
                lab = f"pos {pos} ({label_for_pos})" if label_for_pos else f"pos {pos}"
                # Marker diverso a seconda del flag: cerchio per measure,
                # diamante per calib (la regola si basa sul flag già scritto)
                # ma per semplicità usiamo cerchio per posizione di misura
                # e diamante per le altre (regola posizionale visiva).
                # Approssimiamo: il "flag" prevalente nei dati di questa
                # posizione decide il marker.
                flags_here = flags[mask]
                is_calib = np.any(flags_here == "calib")
                marker = "D" if is_calib else "o"
                size   = self._point_size + 10 if is_calib else self._point_size
                sc = self.ax.scatter(
                    xt[mask], values[mask],
                    s=size, color=color, marker=marker,
                    zorder=4 if is_calib else 3,
                    label=lab,
                )
                self._sc_by_pos.append(sc)
                legend_handles.append(sc)

            if legend_handles:
                self._pos_legend = self.ax.legend(
                    handles=legend_handles, loc="upper left",
                    fontsize=8, framealpha=0.85, ncol=1,
                )
            # Riallinea la visibilità degli scatter appena creati allo stile
            self._apply_graph_style()
        else:
            # Retrocompat: file senza colonne valvola → blu/arancione su flag
            mask_m = mask_valid & (flags == "measure")
            mask_c = mask_valid & (flags == "calib")
            if mask_m.any():
                self.sc_measure.set_offsets(np.column_stack([xt[mask_m], values[mask_m]]))
            else:
                self.sc_measure.set_offsets(np.empty((0, 2)))
            if mask_c.any():
                self.sc_calib.set_offsets(np.column_stack([xt[mask_c], values[mask_c]]))
            else:
                self.sc_calib.set_offsets(np.empty((0, 2)))

        # ── Errorbar (σ del minuto) sui punti CO₂ ──────────────────────────
        # Disegnati sopra la linea ma sotto gli scatter, in grigio
        # semi-trasparente per non oscurare i punti colorati.
        eb_mask = mask_valid & (stds != MISSING) & (stds > 0)
        if eb_mask.any():
            self._errorbar = self.ax.errorbar(
                xt[eb_mask], values[eb_mask],
                yerr=stds[eb_mask],
                fmt="none", ecolor="#666", elinewidth=0.8,
                capsize=2, capthick=0.6, alpha=0.5, zorder=2.5,
            )

        # ── Errorbar T (σ del minuto, asse Y sinistro) ────────────────────
        eb_mask_t = (t_arr != MISSING) & (t_std_arr != MISSING) & (t_std_arr > 0)
        if eb_mask_t.any():
            self._errorbar_t = self.ax_t.errorbar(
                xt[eb_mask_t], t_arr[eb_mask_t],
                yerr=t_std_arr[eb_mask_t],
                fmt="none", ecolor="#c05000", elinewidth=0.7,
                capsize=2, capthick=0.5, alpha=0.45, zorder=2.5,
            )

        # ── Errorbar RH (σ del minuto, asse Y destro twin) ────────────────
        eb_mask_rh = (rh_arr != MISSING) & (rh_std_arr != MISSING) & (rh_std_arr > 0)
        if eb_mask_rh.any():
            self._errorbar_rh = self.ax_rh.errorbar(
                xt[eb_mask_rh], rh_arr[eb_mask_rh],
                yerr=rh_std_arr[eb_mask_rh],
                fmt="none", ecolor="#007060", elinewidth=0.7,
                capsize=2, capthick=0.5, alpha=0.45, zorder=2.5,
            )

        # ── Label flag: fonte LIVE (valve_status.json, ~2s) con fallback ──
        live_pos, live_label, live_fresh = read_live_valve()
        if live_fresh and live_pos >= 1:
            last_pos = live_pos
            last_flag = "measure" if live_pos == _MEASURE_POSITION else "calib"
            label_for_text = live_label
        else:
            last_flag = flags[-1] if len(flags) > 0 else "measure"
            last_pos = int(valve_pos[-1]) if valve_pos.size > 0 else -1
            label_for_text = (str(valve_labels[-1])
                              if valve_labels.size > 0
                              and str(valve_labels[-1]) not in ("-", "")
                              else "")
        # Pos 10 → rosso (span-high), altre calib → arancione, pos 1 → blu.
        if last_flag == "calib" and last_pos == 10:
            color = "#c00000"; text = f"CALIB pos{last_pos}"
        elif last_flag == "calib":
            color = "#e06000"
            text = f"CALIB pos{last_pos}" if last_pos >= 1 else "CALIB"
        else:
            color = "#2060c0"; text = "MEASURE"
        if label_for_text and label_for_text != "-":
            text = f"{text} ({label_for_text})"
        self.flag_label.set_text(text)
        self.flag_label.set_color(color)
        self.flag_label.get_bbox_patch().set_edgecolor(color)

        # ── Striscia posizione valvola (in basso, ymin=0..ymax=0.04 axes) ──
        # Attiva solo se abbiamo dati valvola (integrazione valve-scheduler)
        # E la checkbox è spuntata. Ogni run contiguo di stessa posizione
        # diventa un axvspan colorato (cmap tab20 → 20 posizioni distinguibili).
        # La striscia rappresenta lo STATO FISICO della valvola, che NON
        # dipende dalle medie: la sorgiamo SEMPRE dai dati 1-min (suffix
        # "min"), indipendentemente dalla finestra di averaging selezionata.
        # Così i periodi di calibrazione restano visibili anche quando 1m non
        # è tra le finestre attive (negli aggregati i minuti calib sono esclusi
        # dalle medie e i PUNTI dati spariscono, ma la striscia deve restare).
        vp_s = np.array([], dtype=int)
        if hasattr(self, "chk_valve") and self.chk_valve.isChecked():
            _vres = load_period(self.cfg, start, n_days,
                                suffix="min", metric="mean")
            if (_vres is not None and _vres[10].size == len(_vres[0])
                    and _vres[10].size > 0):
                # Sorgente preferita: valvola a 1-min (confini precisi)
                _vt = _vres[0]; vp_s = _vres[10]; vl_s = _vres[11]
                xt_s = mdates.date2num(_vt)
                step_day = 1.0 / (60.0 * 24.0)        # passo 1-min fisso
            elif valve_pos.size == len(times) and valve_pos.size > 0:
                # Fallback: dati della finestra primaria (se i .raw 1-min
                # mancano per il periodo). Passo = quello della finestra.
                import re as _re
                vp_s = valve_pos; vl_s = valve_labels; xt_s = xt
                _m_step = _re.match(r"(\d+)", primary_w)
                step_day = (int(_m_step.group(1)) if _m_step else 1) / (60.0 * 24.0)
        if vp_s.size > 0:
            # Rileva i run contigui di stessa posizione (trascura -1 = sconosciuta)
            from matplotlib import cm as _cm
            cmap = _cm.get_cmap("tab20")
            # gap_thresh SPEZZA un run quando c'è un buco temporale: senza, se
            # la valvola è nella stessa posizione prima e dopo un'interruzione
            # di acquisizione, l'axvspan unirebbe due istanti distanti ore in
            # un'unica fascia (bug "stati uniti").
            gap_thresh = step_day * 1.5   # tolleranza: oltre 1.5× il passo = buco
            i = 0
            while i < len(vp_s):
                cur_pos = int(vp_s[i])
                if cur_pos < 1:
                    i += 1
                    continue
                j = i + 1
                while (j < len(vp_s) and int(vp_s[j]) == cur_pos
                       and (xt_s[j] - xt_s[j-1]) <= gap_thresh):
                    j += 1
                x0 = xt_s[i]
                x1 = xt_s[j-1] if j-1 < len(xt_s) else xt_s[-1]
                # estendi x1 di mezzo passo per coprire l'intervallo del campione
                x1_ext = x1 + step_day * 0.5
                color = cmap((cur_pos - 1) % 20)
                patch = self.ax.axvspan(
                    x0, x1_ext, ymin=0.0, ymax=0.04,
                    color=color, alpha=0.85, zorder=1)
                self._valve_poly.append(patch)
                # Etichetta testuale sulla fascia solo se abbastanza larga (>5% del plot)
                try:
                    xmin, xmax = self.ax.get_xlim()
                    width_frac = (x1_ext - x0) / max(1e-9, (xmax - xmin))
                except Exception:
                    width_frac = 1.0
                if width_frac > 0.03:
                    # SOLO il numero di posizione: il riquadro colorato (bbox)
                    # del testo è centrato sullo span e, con un testo lungo
                    # (es. "10 pos2-CO2-calib-cylinder-LL73782"), sborderebbe di
                    # ore oltre l'estensione reale della valvola, sovrapponendosi
                    # ai periodi adiacenti e facendo SEMBRARE fuse due calib
                    # distinte. Il nome completo resta nella legenda (pos N (...)).
                    text = str(cur_pos)
                    txt = self.ax.text(
                        (x0 + x1_ext) / 2.0, 0.02, text,
                        transform=self.ax.get_xaxis_transform(),
                        ha="center", va="center", fontsize=7,
                        color="white", weight="bold",
                        bbox=dict(facecolor=color, alpha=0.9,
                                  edgecolor="none", pad=1),
                        zorder=2)
                    self._valve_text.append(txt)
                i = j

        # ── Overlay lines per le finestre temporali NON primarie ──────────
        # Ogni finestra ha i propri colori/linewidth/linestyle/alpha letti
        # da self._per_window (configurabile dal dialog Appearance).
        for w in _priority:
            if w == primary_w or w not in self._enabled_windows:
                continue
            suf_w, met_w = MonitorWindow_SUFFIX_METRIC[w]
            res_w = load_period(self.cfg, start, n_days,
                                suffix=suf_w, metric=met_w)
            if res_w is None:
                continue
            (t_w, v_w, _, _, _, tt_w, _, rh_w, _, _, _, _, _, _) = res_w
            xt_w = mdates.date2num(t_w)
            v_plot  = np.where(v_w  == MISSING, np.nan, v_w)
            tt_plot = np.where(tt_w == MISSING, np.nan, tt_w)
            rh_plot = np.where(rh_w == MISSING, np.nan, rh_w)
            ws = self._per_window[w]
            ln_co2, = self.ax.plot(
                xt_w, v_plot, ws["linestyle"],
                linewidth=ws["line_width"], color=ws["color_co2"],
                alpha=ws["alpha"], zorder=2.6, label=f"CO₂ {w}")
            ln_t, = self.ax_t.plot(
                xt_w, tt_plot, ws["linestyle"],
                linewidth=ws["line_width"], color=ws["color_t"],
                alpha=ws["alpha"], zorder=2.6)
            ln_rh, = self.ax_rh.plot(
                xt_w, rh_plot, ws["linestyle"],
                linewidth=ws["line_width"], color=ws["color_rh"],
                alpha=ws["alpha"], zorder=2.6)
            self._overlay_lines.extend([ln_co2, ln_t, ln_rh])

        # ── Zone notturne ─────────────────────────────────────────────────
        want_night = ASTRAL_OK and hasattr(self, "chk_night") and self.chk_night.isChecked()
        if want_night:
            days_list = [start + timedelta(days=i) for i in range(n_days)]
            for i, (dawn_n, dusk_n) in enumerate(night_spans(self.cfg, days_list)):
                # Inizio e fine del singolo giorno i (CORR-001/GUI-004: fix multiday)
                day_num = mdates.date2num(datetime.combine(days_list[i], datetime.min.time()))
                # Zone notturne su entrambi i pannelli (CO2 e T/RH)
                for axis in (self.ax, self.ax_t):
                    p1 = axis.axvspan(day_num, dawn_n,        color="steelblue", alpha=0.10, zorder=0)
                    p2 = axis.axvspan(dusk_n,  day_num + 1.0, color="steelblue", alpha=0.10, zorder=0)
                    self._night_poly.extend([p1, p2])

        # ── Asse X fisso (intera giornata / periodo) ───────────────────────
        self._set_x_axis(start, n_days)

        # ── Assi Y ────────────────────────────────────────────────────────
        # CO2: rispetta zoom manuale se presente
        if self._zoom_ylim is None:
            lo, hi = smart_ylim(values)
            self.ax.set_ylim(lo, hi)
        else:
            self.ax.set_ylim(self._zoom_ylim)
        # T e RH: sempre autoscale con min_range ridotto, ignorano lo zoom CO2
        self.ax_t.set_ylim(*smart_ylim(t_arr,   min_range=2.0,  fallback=(15.0, 30.0)))
        self.ax_rh.set_ylim(*smart_ylim(rh_arr, min_range=10.0, fallback=(0.0, 100.0)))
        self.ax_p.set_ylim(*smart_ylim(p_arr,   min_range=2.0,  fallback=(980.0, 1040.0)))
        # Flusso: range dai due flussi combinati (massa+vol), min_range piccolo
        _fall = np.concatenate([a for a in (fmass_arr, fvol_arr) if a.size]) \
                if (fmass_arr.size or fvol_arr.size) else np.array([])
        self.ax_flow.set_ylim(*smart_ylim(_fall, min_range=0.1, fallback=(0.0, 1.0)))

        # ── Titolo e label flag sulla stessa riga ─────────────────────────
        site  = self.cfg.get("location", "name", fallback="")
        label = self.combo.currentText()
        if n_days == 1:
            date_str = start.strftime("%Y-%m-%d")
        else:
            date_str = f"{start}  →  {start + timedelta(days=n_days-1)}"
        self.ax.set_title(f"{site}   CO₂ corrected   {date_str}  [{label}]",
                          fontsize=11, fontweight="bold", loc="left")

        self.canvas.draw_idle()
        self._ignore_lim_change = False

    def _set_x_axis(self, start: date_type, n_days: int):
        """Imposta asse X: limiti e formatter iniziale."""
        x0 = mdates.date2num(datetime.combine(start, datetime.min.time()))
        x1 = x0 + n_days

        if self._zoom_xlim is not None:
            self.ax.set_xlim(self._zoom_xlim)
        else:
            self.ax.set_xlim(x0, x1)

        # Tick basati sulla finestra visibile corrente
        self._update_x_ticks()

        self.ax_t.set_xlabel(
            "Ora (UTC)" if n_days == 1 else "Data / Ora (UTC)",
            fontsize=10
        )

    def _update_x_ticks(self):
        """
        Ricalcola locator e formatter in base ai minuti VISIBILI.
        Chiamata sia all'init che ad ogni cambio xlim (zoom/pan).

        Tabella intervalli:
          visibile          locator              formato
          ──────────────────────────────────────────────
          ≤  10 min      MinuteLocator(1)        HH:MM:SS  (o HH:MM)
          ≤  30 min      MinuteLocator(5)        HH:MM
          ≤  60 min      MinuteLocator(10)       HH:MM
          ≤ 180 min      MinuteLocator(30)       HH:MM
          ≤ 360 min      HourLocator(1)          HH:MM
          ≤  24 h        HourLocator(2)          HH:MM
          ≤  72 h        HourLocator(6)          dd HH:MM
          ≤   7 gg       HourLocator(12)         dd/mm HH:MM
          >   7 gg       DayLocator(1)           dd/mm
        """
        xlim    = self.ax.get_xlim()
        vis_min = (xlim[1] - xlim[0]) * 24 * 60  # minuti visibili

        if vis_min <= 10:
            loc = mdates.MinuteLocator(interval=1)
            fmt = mdates.DateFormatter("%H:%M:%S")
        elif vis_min <= 30:
            loc = mdates.MinuteLocator(interval=5)
            fmt = mdates.DateFormatter("%H:%M")
        elif vis_min <= 60:
            loc = mdates.MinuteLocator(interval=10)
            fmt = mdates.DateFormatter("%H:%M")
        elif vis_min <= 180:
            loc = mdates.MinuteLocator(interval=30)
            fmt = mdates.DateFormatter("%H:%M")
        elif vis_min <= 360:
            loc = mdates.HourLocator(interval=1)
            fmt = mdates.DateFormatter("%H:%M")
        elif vis_min <= 1440:          # ≤ 24h
            loc = mdates.HourLocator(interval=2)
            fmt = mdates.DateFormatter("%H:%M")
        elif vis_min <= 4320:          # ≤ 72h
            loc = mdates.HourLocator(interval=6)
            fmt = mdates.DateFormatter("%d %H:%M")
        elif vis_min <= 10080:         # ≤ 7gg
            loc = mdates.HourLocator(interval=12)
            fmt = mdates.DateFormatter("%d/%m %H:%M")
        else:
            loc = mdates.DayLocator(interval=1)
            fmt = mdates.DateFormatter("%d/%m")

        self.ax.xaxis.set_major_locator(loc)
        self.ax.xaxis.set_major_formatter(fmt)
        self.fig.autofmt_xdate(rotation=30, ha="right")

    # ── zoom / reset ─────────────────────────────────────────────────────────

    def _on_lim_changed(self, _ax):
        """Traccia zoom manuale e ricalcola tick asse X."""
        if self._ignore_lim_change:
            return
        self._zoom_xlim = self.ax.get_xlim()
        self._zoom_ylim = self.ax.get_ylim()
        self._update_x_ticks()   # ← tick adattativi immediati

    def _on_release(self, event):
        """Dopo rilascio mouse aggiorna zoom salvato."""
        self._zoom_xlim = self.ax.get_xlim()
        self._zoom_ylim = self.ax.get_ylim()

    def _reset_view(self):
        """Torna alla vista completa del periodo."""
        self._zoom_xlim = None
        self._zoom_ylim = None
        self._reload()

    # ── aggiornamento real-time ───────────────────────────────────────────────

    def _refresh_legend_strip(self):
        """Pulisce e ricostruisce la striscia legenda con un riquadro per
        ogni finestra abilitata. Colore = colore CO₂ della finestra (è
        l'identificativo visivo principale)."""
        # Rimuovi tutti i widget esistenti dal layout
        while self._legend_bar.count() > 0:
            item = self._legend_bar.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        title = QLabel("Averages:")
        title.setStyleSheet("color:#666;font-size:9pt;font-weight:bold")
        self._legend_bar.addWidget(title)
        _legend_labels = {"1m": "1m", "10m": "10m", "30m": "30m",
                          "60m": "60m mean", "60m-med": "60m median"}
        for w in ("1m", "10m", "30m", "60m", "60m-med"):
            if w not in self._enabled_windows:
                continue
            ws = self._per_window[w]
            swatch = QLabel("■")
            swatch.setStyleSheet(
                f"color:{ws['color_co2']};font-size:14pt;font-weight:bold")
            self._legend_bar.addWidget(swatch)
            lbl = QLabel(_legend_labels[w])
            lbl.setStyleSheet("color:#333;font-size:9pt;font-weight:bold")
            self._legend_bar.addWidget(lbl)
        self._legend_bar.addStretch()

    def _on_windows_changed(self, _state=None):
        """Aggiorna self._enabled_windows + persiste in monitor.ini, poi reload."""
        wset = {w for w, cb in self.chk_windows.items() if cb.isChecked()}
        if not wset:
            # Almeno una finestra deve essere visibile; fallback a 1m senza loop:
            # rimettiamo la checkbox 1m e usciamo (lo stateChanged ricondurrà qui).
            self.chk_windows["1m"].blockSignals(True)
            self.chk_windows["1m"].setChecked(True)
            self.chk_windows["1m"].blockSignals(False)
            wset = {"1m"}
        self._enabled_windows = wset
        # Persisti su monitor.ini → [graph] → windows
        try:
            gcp = configparser.ConfigParser()
            if os.path.exists(MONITOR_INI):
                gcp.read(MONITOR_INI)
            if "graph" not in gcp:
                gcp["graph"] = {}
            ordered = [w for w in ("1m", "10m", "30m", "60m", "60m-med") if w in wset]
            gcp["graph"]["windows"] = ",".join(ordered)
            with open(MONITOR_INI, "w") as f:
                gcp.write(f)
        except OSError:
            pass
        self._refresh_legend_strip()
        self._reload()

    def refresh(self):
        """Chiamato dal timer: aggiorna solo se il periodo include oggi."""
        start, n_days = self._period_range()
        today = datetime.utcnow().date()
        end   = start + timedelta(days=n_days - 1)
        if start <= today <= end:
            self._reload()
        self._refresh_toolbar_readouts()

    def _refresh_comp_label(self):
        """Aggiorna la dicitura compensazione P/RH inviata alla sonda
        (legge i campi comp_* da shared/ipc_co2/status.json scritti dal logger)."""
        if not hasattr(self, "lbl_comp"):
            return
        try:
            sj = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "shared", "ipc_co2", "status.json")
            with open(sj) as f:
                d = json.load(f)
            # CO2 corretta (compensata) vs grezza (CO2RAWUC), con dicitura
            c = d.get("last_co2_ppm"); g = d.get("last_co2rawuc_ppm")
            cs = f"{c:.1f}" if isinstance(c, (int, float)) else "—"
            gs = f"{g:.1f}" if isinstance(g, (int, float)) else "—"
            co2_s = f"CO₂ corrected {cs} · raw {gs} ppm"
            if not d.get("comp_active"):
                self.lbl_comp.setText(f"{co2_s}   ·   Comp: OFF")
                self.lbl_comp.setStyleSheet("color:#999;font-weight:bold")
                return
            rh = d.get("comp_rh_fed"); p = d.get("comp_p_hpa"); src = d.get("comp_p_source")
            rh_s = f"RH {rh:.1f}%→probe" if isinstance(rh, (int, float)) else "RH —"
            src_s = {"fixed": "fixed", "bmp388": "BMP388"}.get(src, "")
            p_s = f"P {p:.1f} hPa ({src_s})" if isinstance(p, (int, float)) else "P —"
            self.lbl_comp.setText(f"{co2_s}   ·   Comp → {rh_s} · {p_s}")
            self.lbl_comp.setStyleSheet("color:#0a7d36;font-weight:bold")
        except Exception:
            self.lbl_comp.setText("")

    def _refresh_toolbar_readouts(self):
        """Aggiorna i tre label "Live / 1-min avg / σ" nella toolbar grafico."""
        self._refresh_comp_label()
        today = datetime.utcnow().date()
        # Live: ultima riga del .raw (~1 Hz)
        _, live_co2, _, _, _, _, _, _ = read_last_raw_sample(self.cfg, today)
        if live_co2 is None or live_co2 == MISSING:
            self.lbl_chart_live.setText("--- ppm")
        else:
            self.lbl_chart_live.setText(f"{live_co2:.2f} ppm")
        # Media 1 min + σ: ultima riga del _min.raw
        result = load_period(self.cfg, today, 1)
        if result is None:
            self.lbl_chart_avg.setText("--- ppm")
            self.lbl_chart_std.setText("---")
            return
        (_t, vals, stds, *_rest) = result
        if len(vals) > 0 and vals[-1] != MISSING:
            self.lbl_chart_avg.setText(f"{float(vals[-1]):.2f} ppm")
        else:
            self.lbl_chart_avg.setText("--- ppm")
        if len(stds) > 0 and stds[-1] != MISSING:
            self.lbl_chart_std.setText(f"{float(stds[-1]):.2f} ppm")
        else:
            self.lbl_chart_std.setText("---")

    # ── hover ────────────────────────────────────────────────────────────────

    def _on_motion(self, event):
        # Il tooltip appare passando su QUALSIASI pannello (CO2/P/Flusso/T-RH):
        # i pannelli condividono l'asse X, quindi si trova il campione più
        # vicino alla X del cursore e si mostra il readout COMPLETO.
        panels = (self.ax, self.ax_p, self.ax_flow, self.ax_t, self.ax_rh)
        if event.inaxes not in panels or event.xdata is None:
            self._hide_tooltip()
            return

        xd, yd = self.line.get_data()
        if xd is None or len(xd) == 0:
            self._hide_tooltip()
            return
        xd = np.asarray(xd, dtype=float)
        idx = int(np.argmin(np.abs(xd - event.xdata)))
        xv = xd[idx]
        yv = yd[idx]   # CO2 (può essere NaN se mancante a quell'idx)

        # Punto evidenziato sul pannello CO2 (solo se CO2 valida)
        if not np.isnan(yv):
            self.hl_pt.set_data([xv], [yv])
            self.hl_pt.set_visible(True)
        else:
            self.hl_pt.set_visible(False)

        # Testo tooltip: Ora + CO2 + T + RH + P + Flusso (massa/vol) allo stesso idx
        t_str = mdates.num2date(xv).strftime("%H:%M:%S")
        lines = [f"Ora:  {t_str}"]
        if not np.isnan(yv):
            lines.append(f"CO₂:  {yv:.2f} ppm")
        def _add(attr, label, unit, dec=2):
            arr = getattr(self, attr, None)
            if arr is not None and idx < len(arr):
                v = arr[idx]
                if not np.isnan(v):
                    lines.append(f"{label} {v:.{dec}f} {unit}")
        _add("_t_plot",     "T:   ", "°C")
        _add("_rh_plot",    "RH:  ", "%")
        _add("_p_plot",     "P:   ", "hPa")
        _add("_fmass_plot", "Fm:  ", "slpm", 3)
        _add("_fvol_plot",  "Fv:  ", "Lpm",  3)
        self.annot.set_text("\n".join(lines))
        # freccia: punta alla CO2 se valida, altrimenti al centro Y del plot
        y_anchor = yv if not np.isnan(yv) else np.mean(self.ax.get_ylim())
        self.annot.xy = (xv, y_anchor)
        xv, yv = xv, y_anchor

        # Posizione tooltip in fraction degli assi (0-1)
        # così non può mai uscire dalla figura
        xlim = self.ax.get_xlim()
        ylim = self.ax.get_ylim()
        xn = (xv - xlim[0]) / (xlim[1] - xlim[0])  # 0=sinistra, 1=destra
        yn = (yv - ylim[0]) / (ylim[1] - ylim[0])  # 0=basso,    1=alto

        # 4 quadranti: tooltip va sempre nel quadrante opposto al punto
        tx = 0.03 if xn > 0.5 else 0.72   # sinistra o destra assi
        ty = 0.08 if yn > 0.5 else 0.82   # basso o alto assi

        self.annot.set_position((tx, ty))
        self.annot.set_visible(True)
        self.canvas.draw_idle()

    def _hide_tooltip(self):
        changed = self.annot.get_visible() or self.hl_pt.get_visible()
        self.annot.set_visible(False)
        self.hl_pt.set_visible(False)
        if changed:
            self.canvas.draw_idle()

    def cleanup(self):
        """Rilascia la Figure matplotlib — chiamare prima della distruzione (GUI-002)."""
        import matplotlib.pyplot as plt
        try:
            self.fig.clear()
            plt.close(self.fig)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  Tab Valvola VICI (valve-scheduler integrato)
# ══════════════════════════════════════════════════════════════════════════════

class DaemonSettingsDialog(QDialog):
    """Dialog per editare le impostazioni del valve-daemon."""

    def __init__(self, current: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Valve-daemon settings")
        self.setMinimumWidth(420)
        form = QFormLayout(self)
        form.setSpacing(8)

        ini_lbl = QLabel(current.get("ini_path", "—"))
        ini_lbl.setStyleSheet("color:#666;font-size:8pt")
        form.addRow("INI file:", ini_lbl)

        self.ed_port = QLineEdit(current.get("serial_port", "/dev/vici"))
        form.addRow("Serial port:", self.ed_port)

        self.cb_baud = QComboBox()
        self.cb_baud.setEditable(True)
        for b in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200):
            self.cb_baud.addItem(str(b))
        self.cb_baud.setEditText(str(current.get("serial_baud", 9600)))
        form.addRow("Baud rate:", self.cb_baud)

        self.sp_timeout = QDoubleSpinBox()
        self.sp_timeout.setRange(0.1, 30.0)
        self.sp_timeout.setSingleStep(0.5)
        self.sp_timeout.setValue(float(current.get("serial_timeout", 2.0)))
        self.sp_timeout.setSuffix(" s")
        form.addRow("Serial timeout:", self.sp_timeout)

        self.chk_rs485 = QCheckBox("RS-485 (default RS-232)")
        self.chk_rs485.setChecked(bool(current.get("serial_rs485", False)))
        form.addRow("Serial mode:", self.chk_rs485)

        self.ed_dev_id = QLineEdit(current.get("serial_dev_id", ""))
        self.ed_dev_id.setPlaceholderText("(RS-485 only, default Z)")
        form.addRow("Device ID:", self.ed_dev_id)

        self.sp_n = QSpinBox()
        self.sp_n.setRange(2, 40)
        self.sp_n.setValue(int(current.get("n_positions", 10)))
        form.addRow("Number of positions:", self.sp_n)

        self.sp_idle = QDoubleSpinBox()
        self.sp_idle.setRange(0.5, 30.0)
        self.sp_idle.setSingleStep(0.5)
        self.sp_idle.setValue(float(current.get("idle_poll_s", 2.0)))
        self.sp_idle.setSuffix(" s")
        form.addRow("CP idle polling:", self.sp_idle)

        self.chk_auto = QCheckBox("Open valve at daemon startup")
        self.chk_auto.setChecked(bool(current.get("auto_connect", True)))
        form.addRow("", self.chk_auto)

        self.chk_cfg_start = QCheckBox(
            "Apply IFM1+AM3+SMA+NP at startup (advanced)")
        self.chk_cfg_start.setChecked(bool(current.get("configure_on_start", False)))
        form.addRow("", self.chk_cfg_start)

        self.chk_home_start = QCheckBox(
            "HM (go to pos 1) at startup")
        self.chk_home_start.setChecked(bool(current.get("go_home_on_start", False)))
        form.addRow("", self.chk_home_start)

        # "Files and folders" — visual separator
        sep = QLabel("───── Files and folders ─────")
        sep.setStyleSheet("color:#888;font-size:9pt;padding-top:8px")
        sep.setAlignment(Qt.AlignCenter)
        form.addRow(sep)

        prog_dir = "/home/misura/programs/valve-scheduler"
        path_hint = QLabel(
            f"Relative paths are resolved against {prog_dir}/.\n"
            "To use folders elsewhere, write an absolute path.")
        path_hint.setStyleSheet("color:#666;font-size:8pt")
        path_hint.setWordWrap(True)
        form.addRow(path_hint)

        self.ed_status = QLineEdit(
            current.get("status_file", "service/valve_status.json"))
        self.ed_status.setToolTip(
            "Status JSON — written by the daemon, read by the CO2 logger.\n"
            "If you change this, update config/integration.ini of the logger too.")
        form.addRow("Status JSON file:", self.ed_status)

        h_log = QHBoxLayout()
        self.ed_logdir = QLineEdit(current.get("log_dir", "log"))
        self.ed_logdir.setToolTip(
            "Directory for valve-daemon.log (rotation 1MB × 5).")
        h_log.addWidget(self.ed_logdir)
        btn_browse_log = QToolButton()
        btn_browse_log.setText("…")
        btn_browse_log.clicked.connect(
            lambda: self._browse_dir(self.ed_logdir, prog_dir))
        h_log.addWidget(btn_browse_log)
        wrap_log = QWidget(); wrap_log.setLayout(h_log)
        h_log.setContentsMargins(0, 0, 0, 0)
        form.addRow("Log folder:", wrap_log)

        h_sch = QHBoxLayout()
        self.ed_sched = QLineEdit(
            current.get("schedule_file", "schedule/schedule.csv"))
        self.ed_sched.setToolTip(
            "Default schedule CSV (loaded at GUI startup).\n"
            "The active schedule is the one edited in the table.")
        h_sch.addWidget(self.ed_sched)
        btn_browse_sched = QToolButton()
        btn_browse_sched.setText("…")
        btn_browse_sched.clicked.connect(
            lambda: self._browse_file(self.ed_sched, prog_dir,
                                       "CSV (*.csv);;All files (*)"))
        h_sch.addWidget(btn_browse_sched)
        wrap_sch = QWidget(); wrap_sch.setLayout(h_sch)
        h_sch.setContentsMargins(0, 0, 0, 0)
        form.addRow("Default schedule CSV:", wrap_sch)

        warn = QLabel(
            "On save, the daemon stops the IdlePoller and reopens the port\n"
            "with the new settings (~1 s). Running schedules must be\n"
            "stopped first. Changing the log folder requires a daemon\n"
            "restart (sudo systemctl restart valve-daemon).")
        warn.setStyleSheet("color:#666;font-size:9pt")
        form.addRow(warn)

        btns = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _browse_dir(self, line_edit: QLineEdit, base: str) -> None:
        start = line_edit.text().strip() or base
        if not Path(start).is_absolute():
            start = str(Path(base) / start)
        d = QFileDialog.getExistingDirectory(
            self, "Select folder", start)
        if d:
            line_edit.setText(d)

    def _browse_file(self, line_edit: QLineEdit, base: str,
                     filter_str: str) -> None:
        start = line_edit.text().strip() or base
        if not Path(start).is_absolute():
            start = str(Path(base) / start)
        f, _ = QFileDialog.getOpenFileName(
            self, "Select file", start, filter_str)
        if f:
            line_edit.setText(f)

    def values(self) -> dict:
        try:
            baud = int(self.cb_baud.currentText().strip())
        except ValueError:
            baud = 9600
        return {
            "serial_port":        self.ed_port.text().strip() or "/dev/vici",
            "serial_baud":        baud,
            "serial_timeout":     float(self.sp_timeout.value()),
            "serial_rs485":       self.chk_rs485.isChecked(),
            "serial_dev_id":      self.ed_dev_id.text().strip(),
            "n_positions":        int(self.sp_n.value()),
            "idle_poll_s":        float(self.sp_idle.value()),
            "auto_connect":       self.chk_auto.isChecked(),
            "configure_on_start": self.chk_cfg_start.isChecked(),
            "go_home_on_start":   self.chk_home_start.isChecked(),
            "status_file":        self.ed_status.text().strip()
                                    or "service/valve_status.json",
            "log_dir":            self.ed_logdir.text().strip() or "log",
            "schedule_file":      self.ed_sched.text().strip()
                                    or "schedule/schedule.csv",
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Configurazione CO2 logger (Output, Seriale GMP343, Sito, Sensori, Layout Pi 5)
# ══════════════════════════════════════════════════════════════════════════════

# Pinout 40-pin GPIO — uguale per Pi 2/3/4/5. Sorgente: pinout.xyz e
# raspberrypi.com/documentation/computers/raspberry-pi.html
PI5_PINOUT = """
                  Raspberry Pi 5 — 40-pin GPIO header

           +---+---+               LEGENDA
   3V3 ─── │ 1 │ 2 │ ─── 5V        ★ I2C-1 (default per sensori)
  GPIO2 ★ │ 3 │ 4 │ ─── 5V         I2C-1 SDA = pin 3 (GPIO2)
  GPIO3 ★ │ 5 │ 6 │ ─── GND        I2C-1 SCL = pin 5 (GPIO3)
  GPIO4   │ 7 │ 8 │   GPIO14      Power: 3V3 = pin 1, GND = pin 6/9/14/...
   GND    │ 9 │10 │   GPIO15
  GPIO17  │11 │12 │   GPIO18      ID_SD/ID_SC = HAT EEPROM (NON usare)
  GPIO27  │13 │14 │ ─── GND
  GPIO22  │15 │16 │   GPIO23      Bus I2C aggiuntivi su Pi 5 (via dtoverlay):
   3V3    │17 │18 │   GPIO24       i2c-3 SDA=GPIO4, SCL=GPIO5  (pin 7+29)
  GPIO10  │19 │20 │ ─── GND        i2c-4 SDA=GPIO8, SCL=GPIO9  (pin 24+21)
  GPIO9   │21 │22 │   GPIO25       i2c-5 SDA=GPIO12, SCL=GPIO13(pin 32+33)
  GPIO11  │23 │24 │   GPIO8        i2c-6 SDA=GPIO22, SCL=GPIO23(pin 15+16)
   GND    │25 │26 │   GPIO7
  ID_SD   │27 │28 │   ID_SC        Per attivarli: aggiungere a /boot/firmware/config.txt:
  GPIO5   │29 │30 │ ─── GND          dtoverlay=i2c3,pins_2_3
  GPIO6   │31 │32 │   GPIO12         dtoverlay=i2c4
  GPIO13  │33 │34 │ ─── GND          dtparam=i2c_arm=on  (i2c-1, già attivo)
  GPIO19  │35 │36 │   GPIO16
  GPIO26  │37 │38 │   GPIO20      Indirizzi I2C noti dei sensori che hai:
   GND    │39 │40 │   GPIO21       SHT31-D    0x44 (ADDR pin LOW, default)
           +---+---+               SHT31-D    0x45 (ADDR pin HIGH)
                                   BMP388     0x77 (SDO LOW, default)
                                   BMP388     0x76 (SDO HIGH)

Per collegare un secondo SHT31-D + un BMP388 in parallelo sullo stesso bus i2c-1:

   SHT31-D primario (0x44, già installato) ──┐
                                              ├── pin 3 (SDA) + pin 5 (SCL)
   SHT31-D secondario (0x45, ADDR a Vcc)  ───┤    pin 1 (3V3) + pin 6 (GND)
                                              │
   BMP388 (0x77, default)                 ────┘

I tre sensori condividono SDA/SCL/3V3/GND. Possono coesistere perché
hanno indirizzi I2C diversi. Per discriminare il SHT31 secondario,
collega il pin ADDR a 3V3 (default è a GND → 0x44).
Verifica con: `i2cdetect -y 1` (devono comparire 0x44, 0x45, 0x77).
"""


class _IniMixin:
    """Helper di lettura/scrittura INI molto semplice (configparser)."""
    @staticmethod
    def _read_ini(path: str) -> configparser.ConfigParser:
        cp = configparser.ConfigParser()
        cp.optionxform = str  # preserva case
        if os.path.exists(path):
            cp.read(path, encoding="utf-8")
        return cp

    @staticmethod
    def _write_ini(path: str, cp: configparser.ConfigParser) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            cp.write(f)


class MonitorConfigDialog(QDialog, _IniMixin):
    """Editor INI del CO2 logger / monitor.

    Tab:
      - Output (name.ini)
      - Seriale GMP343 (serial.ini)
      - Sito (site.ini)
      - Sensori I2C (sensors.ini, ancora non letto dal logger — preview)
      - Layout Pi 5 (riferimento)
    """

    def __init__(self, config_dir: str, parent=None):
        super().__init__(parent)
        self.config_dir = config_dir
        self.setWindowTitle("CO2 logger settings")
        self.resize(640, 560)

        lay = QVBoxLayout(self)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_output_tab(),    "Output")
        self._tabs.addTab(self._build_serial_tab(),    "GMP343 Serial")
        self._tabs.addTab(self._build_site_tab(),      "Site")
        self._tabs.addTab(self._build_sensors_tab(),   "I2C Sensors")
        self._tabs.addTab(self._build_layout_tab(),    "Pi 5 Layout")
        self._tabs.addTab(self._build_aspetto_tab(),   "Appearance")
        lay.addWidget(self._tabs)

        warn = QLabel(
            "Changes to Output / Serial require a restart of the CO2 "
            "logger to take effect:\n"
            "    sudo systemctl restart co2-logger")
        warn.setStyleSheet("color:#666;font-size:9pt")
        warn.setWordWrap(True)
        lay.addWidget(warn)

        btns = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    # ────────────────────────────────────────────────────── Tab Output
    def _build_output_tab(self) -> QWidget:
        ini = self._read_ini(os.path.join(self.config_dir, "name.ini"))
        sec = ini["output"] if "output" in ini else {}

        w = QWidget(); form = QFormLayout(w)
        info = QLabel(
            "Filename and folder of the `.raw` and `_min.raw` files written by the logger.")
        info.setStyleSheet("color:#666;font-size:9pt")
        info.setWordWrap(True)
        form.addRow(info)

        h_dir = QHBoxLayout()
        self.ed_data_path = QLineEdit(sec.get("data_path", "~/data"))
        h_dir.addWidget(self.ed_data_path)
        btn_d = QToolButton(); btn_d.setText("…")
        btn_d.clicked.connect(lambda: self._browse_dir(self.ed_data_path))
        h_dir.addWidget(btn_d)
        wrap = QWidget(); wrap.setLayout(h_dir); h_dir.setContentsMargins(0,0,0,0)
        form.addRow("Data folder:", wrap)

        self.ed_basename = QLineEdit(sec.get("basename", "carbocap343"))
        form.addRow("File basename:", self.ed_basename)
        self.ed_extension = QLineEdit(sec.get("extension", "raw"))
        form.addRow("Extension:", self.ed_extension)

        ex = QLabel(
            f"Daily file example:\n"
            f"  {self.ed_basename.text()}_<site>_<YYYYMMDD>_p00.{self.ed_extension.text()}")
        ex.setStyleSheet("color:#888;font-size:8pt;font-family:monospace")
        form.addRow("", ex)
        # update example dynamically
        def _upd():
            ex.setText(
                f"Daily file example:\n"
                f"  {self.ed_basename.text()}_<site>_<YYYYMMDD>_p00.{self.ed_extension.text()}")
        self.ed_basename.textChanged.connect(_upd)
        self.ed_extension.textChanged.connect(_upd)
        return w

    # ──────────────────────────────────────────────── Tab Seriale GMP343
    def _build_serial_tab(self) -> QWidget:
        ini = self._read_ini(os.path.join(self.config_dir, "serial.ini"))
        sec = ini["serial"] if "serial" in ini else {}

        w = QWidget(); form = QFormLayout(w)
        info = QLabel(
            "Serial port for the Vaisala GMP343 sensor. On Raspberry Pi 5 "
            "the udev symlink `/dev/gmp343` is persistent; use that.")
        info.setStyleSheet("color:#666;font-size:9pt"); info.setWordWrap(True)
        form.addRow(info)

        self.ed_serial_port = QLineEdit(sec.get("port", "/dev/gmp343"))
        form.addRow("Port:", self.ed_serial_port)

        self.cb_serial_baud = QComboBox(); self.cb_serial_baud.setEditable(True)
        for b in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200):
            self.cb_serial_baud.addItem(str(b))
        self.cb_serial_baud.setEditText(sec.get("baudrate", "19200"))
        form.addRow("Baud rate:", self.cb_serial_baud)

        self.sp_bytesize = QSpinBox(); self.sp_bytesize.setRange(5, 8)
        self.sp_bytesize.setValue(int(sec.get("bytesize", 8)))
        form.addRow("Bytesize:", self.sp_bytesize)

        self.cb_parity = QComboBox()
        self.cb_parity.addItems(["N", "E", "O"])
        self.cb_parity.setCurrentText(sec.get("parity", "N"))
        form.addRow("Parity:", self.cb_parity)

        self.sp_stopbits = QSpinBox(); self.sp_stopbits.setRange(1, 2)
        self.sp_stopbits.setValue(int(sec.get("stopbits", 1)))
        form.addRow("Stopbits:", self.sp_stopbits)

        self.sp_serial_timeout = QSpinBox()
        self.sp_serial_timeout.setRange(1, 30)
        self.sp_serial_timeout.setValue(int(float(sec.get("timeout", 1))))
        self.sp_serial_timeout.setSuffix(" s")
        form.addRow("Timeout:", self.sp_serial_timeout)
        return w

    # ─────────────────────────────────────────────────── Tab Sito
    def _build_site_tab(self) -> QWidget:
        ini = self._read_ini(os.path.join(self.config_dir, "site.ini"))
        sec = ini["location"] if "location" in ini else {}

        w = QWidget(); form = QFormLayout(w)
        info = QLabel(
            "Station identification and coordinates for sunrise/sunset "
            "computation (night zones in the chart).")
        info.setStyleSheet("color:#666;font-size:9pt"); info.setWordWrap(True)
        form.addRow(info)

        self.ed_site_name = QLineEdit(sec.get("name", "ISACBO"))
        form.addRow("Station name:", self.ed_site_name)

        self.sp_lat = QDoubleSpinBox()
        self.sp_lat.setRange(-90.0, 90.0); self.sp_lat.setDecimals(6)
        self.sp_lat.setValue(float(sec.get("latitude", 44.523624)))
        form.addRow("Latitude:", self.sp_lat)

        self.sp_lon = QDoubleSpinBox()
        self.sp_lon.setRange(-180.0, 180.0); self.sp_lon.setDecimals(6)
        self.sp_lon.setValue(float(sec.get("longitude", 11.338379)))
        form.addRow("Longitude:", self.sp_lon)

        self.cb_tz = QComboBox(); self.cb_tz.setEditable(True)
        for tz in ("UTC", "Europe/Rome", "Europe/London"):
            self.cb_tz.addItem(tz)
        self.cb_tz.setEditText(sec.get("timezone", "UTC"))
        form.addRow("Timezone:", self.cb_tz)
        return w

    # ────────────────────────────────────────────── Tab Sensori I2C
    def _build_sensors_tab(self) -> QWidget:
        ini = self._read_ini(os.path.join(self.config_dir, "sensors.ini"))

        w = QWidget(); lay = QVBoxLayout(w)
        warn = QLabel(
            "⚠ Preview config — the CURRENT logger only reads the primary "
            "SHT31-D hardcoded (bus 1, addr 0x44). Saving here prepares "
            "sensors.ini for when the logger is updated to read it "
            "(Phase 2 of the refactor).")
        warn.setWordWrap(True)
        warn.setStyleSheet(
            "background:#fff8e1;border:1px solid #f5b800;"
            "padding:6px;border-radius:4px;font-size:9pt")
        lay.addWidget(warn)

        # SHT31 primario
        self._sens_widgets = {}
        for key, default in [
            ("sht31_a",  {"label":"Primary SHT31-D (T+RH)",  "enabled":True,
                          "bus":1, "addr":"0x44"}),
            ("sht31_b",  {"label":"Secondary SHT31-D (T+RH)","enabled":False,
                          "bus":1, "addr":"0x45"}),
            ("bmp388",   {"label":"BMP388 (P+T)",            "enabled":False,
                          "bus":1, "addr":"0x77"}),
        ]:
            sec = ini[key] if key in ini else {}
            grp = QGroupBox(default["label"])
            f = QFormLayout(grp)
            chk = QCheckBox("Enabled")
            chk.setChecked(self._cfg_bool(sec.get("enabled"), default["enabled"]))
            f.addRow("", chk)
            sp_bus = QSpinBox(); sp_bus.setRange(0, 9)
            sp_bus.setValue(int(sec.get("bus", default["bus"])))
            f.addRow("I2C bus:", sp_bus)
            ed_addr = QLineEdit(sec.get("addr", default["addr"]))
            ed_addr.setPlaceholderText("e.g. 0x44")
            f.addRow("I2C address:", ed_addr)
            lay.addWidget(grp)
            self._sens_widgets[key] = (chk, sp_bus, ed_addr)

        hint = QLabel(
            "Tip: before connecting a new sensor, run "
            "`i2cdetect -y 1` to make sure the bus is free at the "
            "desired address.")
        hint.setStyleSheet("color:#666;font-size:8pt")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addStretch()
        return w

    # ─────────────────────────────────────────── Tab Layout Pi 5
    def _build_layout_tab(self) -> QWidget:
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget()
        vbox = QVBoxLayout(inner)
        title = QLabel("GPIO header layout — Raspberry Pi 5 (same as Pi 4/3/2)")
        title.setStyleSheet("font-weight:bold;font-size:11pt")
        vbox.addWidget(title)

        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setFont(QFont("Monospace", 9))
        view.setPlainText(PI5_PINOUT)
        view.setStyleSheet("background:#fafafa")
        vbox.addWidget(view, stretch=1)

        scroll.setWidget(inner)
        return scroll

    # ────────────────────────────────────────────────── Tab Aspetto
    def _build_aspetto_tab(self) -> QWidget:
        ini = self._read_ini(MONITOR_INI)
        fnt = ini["fonts"] if "fonts" in ini else {}
        grp = ini["graph"] if "graph" in ini else {}
        win = ini["window"] if "window" in ini else {}

        # Scroll area: la tab cresce con la sezione per-finestra (4 box)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget(); root = QVBoxLayout(inner)

        info = QLabel(
            "Font sizes for the Monitor panel and chart styling.\n"
            "Changes require a Monitor restart to take effect.")
        info.setStyleSheet("color:#666;font-size:9pt"); info.setWordWrap(True)
        root.addWidget(info)

        # — Default window geometry (letto da [window] di monitor.ini)  ——————
        g_win = QGroupBox("Default window geometry")
        fw = QFormLayout(g_win)

        def _ispin(initial, lo, hi):
            sp = QSpinBox(); sp.setRange(lo, hi)
            try: sp.setValue(int(float(initial)))
            except (TypeError, ValueError): sp.setValue(lo)
            return sp

        # x, y supportano valori negativi (multi-monitor con monitor a sinistra)
        self.sp_win_x      = _ispin(win.get("x",      100),  -4000, 10000)
        self.sp_win_y      = _ispin(win.get("y",       50),  -4000, 10000)
        self.sp_win_width  = _ispin(win.get("width",  1200),   400,  6000)
        self.sp_win_height = _ispin(win.get("height",  800),   300,  4000)
        fw.addRow("X position (px):",  self.sp_win_x)
        fw.addRow("Y position (px):",  self.sp_win_y)
        fw.addRow("Width (px):",       self.sp_win_width)
        fw.addRow("Height (px):",      self.sp_win_height)

        # Suggerimento live: legge la geometria della finestra del Monitor
        # nel momento in cui si apre il dialog. Comodo per copiare nei 4
        # spinbox sopra le dimensioni attuali (ad es. dopo aver ridimensionato
        # la finestra a piacere col mouse).
        parent = self.parent()
        if parent is not None and hasattr(parent, "geometry"):
            g = parent.geometry()
            cur = QLabel(
                f"<i>Current window:</i> <b>{g.width()} × {g.height()}</b> "
                f"px  @  X=<b>{g.x()}</b>, Y=<b>{g.y()}</b>"
            )
            cur.setStyleSheet("color:#246;font-size:9pt;"
                              "background:#eef4fa;padding:4px;border-radius:3px")
            cur.setTextFormat(Qt.RichText)
            fw.addRow(cur)

        hint = QLabel("Applied at next Monitor startup. "
                      "(Closing the app does NOT overwrite these values.)")
        hint.setStyleSheet("color:#888;font-size:8pt"); hint.setWordWrap(True)
        fw.addRow(hint)
        root.addWidget(g_win)

        # — Fonts —
        g_font = QGroupBox("Font sizes (px)")
        f1 = QFormLayout(g_font)

        def _spin(initial, lo, hi):
            sp = QSpinBox(); sp.setRange(lo, hi)
            try: sp.setValue(int(float(initial)))
            except (TypeError, ValueError): sp.setValue(lo)
            return sp

        self.sp_co2_value_size = _spin(fnt.get("co2_value_size", 24), 8, 72)
        f1.addRow("CO₂ value (live + average):", self.sp_co2_value_size)
        self.sp_label_size = _spin(fnt.get("label_size", 14), 6, 36)
        f1.addRow("Panel labels:", self.sp_label_size)
        self.sp_caption_size = _spin(fnt.get("caption_size", 14), 6, 32)
        f1.addRow("'LIVE' / '1-MIN AVERAGE' captions:", self.sp_caption_size)
        self.sp_file_path_size = _spin(fnt.get("file_path_size", 13), 6, 32)
        f1.addRow("File path (Last Sample):", self.sp_file_path_size)
        self.sp_title_size = _spin(fnt.get("title_size", 14), 8, 36)
        f1.addRow("Title:", self.sp_title_size)
        self.sp_subtitle_size = _spin(fnt.get("subtitle_size", 12), 6, 24)
        f1.addRow("Subtitle (site):", self.sp_subtitle_size)
        self.sp_small_size = _spin(fnt.get("small_size", 12), 6, 20)
        f1.addRow("Small text:", self.sp_small_size)
        root.addWidget(g_font)

        # — Chart CO₂ —
        g_graph = QGroupBox("CO₂ chart style")
        f2 = QFormLayout(g_graph)
        _style_to_idx = {"lines+points": 0, "lines": 1, "points": 2}
        self.cb_graph_style = QComboBox()
        self.cb_graph_style.addItems(["Lines + Points", "Lines only", "Points only"])
        self.cb_graph_style.setCurrentIndex(
            _style_to_idx.get(grp.get("style", "lines+points").strip().lower(), 0))
        f2.addRow("Style:", self.cb_graph_style)

        self.sp_point_size = QSpinBox(); self.sp_point_size.setRange(2, 200)
        try: self.sp_point_size.setValue(int(float(grp.get("point_size", 18))))
        except (TypeError, ValueError): self.sp_point_size.setValue(18)
        f2.addRow("Point size (matplotlib s):", self.sp_point_size)

        self.sp_line_width = QDoubleSpinBox()
        self.sp_line_width.setRange(0.2, 6.0)
        self.sp_line_width.setSingleStep(0.1); self.sp_line_width.setDecimals(2)
        try: self.sp_line_width.setValue(float(grp.get("line_width", 1.0)))
        except (TypeError, ValueError): self.sp_line_width.setValue(1.0)
        f2.addRow("Line width (pt):", self.sp_line_width)
        root.addWidget(g_graph)

        # — Chart T/RH —
        g_trh = QGroupBox("T / RH chart style (bottom panel)")
        f3 = QFormLayout(g_trh)
        self.cb_trh_style = QComboBox()
        self.cb_trh_style.addItems(["Lines + Points", "Lines only", "Points only"])
        self.cb_trh_style.setCurrentIndex(
            _style_to_idx.get(grp.get("trh_style", "lines").strip().lower(), 1))
        f3.addRow("Style:", self.cb_trh_style)

        self.sp_trh_point_size = QSpinBox(); self.sp_trh_point_size.setRange(2, 200)
        try: self.sp_trh_point_size.setValue(int(float(grp.get("trh_point_size", 10))))
        except (TypeError, ValueError): self.sp_trh_point_size.setValue(10)
        f3.addRow("Point size (matplotlib s):", self.sp_trh_point_size)

        self.sp_trh_line_width = QDoubleSpinBox()
        self.sp_trh_line_width.setRange(0.2, 6.0)
        self.sp_trh_line_width.setSingleStep(0.1); self.sp_trh_line_width.setDecimals(2)
        try: self.sp_trh_line_width.setValue(float(grp.get("trh_line_width", 1.0)))
        except (TypeError, ValueError): self.sp_trh_line_width.setValue(1.0)
        f3.addRow("Line width (pt):", self.sp_trh_line_width)
        root.addWidget(g_trh)

        # — Per-window overlay styles (1m / 10m / 30m / 60m) —
        # Note: la finestra "primaria" (più piccola selezionata) usa lo stile
        # CO₂/T-RH sopra; queste impostazioni sono usate per gli OVERLAY
        # (le finestre non-primarie disegnate come linee sovrapposte).
        info_w = QLabel(
            "<b>Per-window overlay styles</b> — colors, line style,"
            " line width and alpha for each averaging window. The"
            " smallest enabled window uses the chart styles above;"
            " the others are drawn as overlays with these settings.")
        info_w.setStyleSheet("color:#444;font-size:9pt;padding-top:6px")
        info_w.setWordWrap(True)
        root.addWidget(info_w)

        # Salva i widget per finestra in un dict per facile accesso da _on_save
        self.window_widgets = {}

        defaults = MonitorWindow_PER_WINDOW_DEFAULTS  # alias outside class
        for w_key in ("1m", "10m", "30m", "60m", "60m-med"):
            d = defaults[w_key]
            _gbox_titles = {"1m": "1m overlay", "10m": "10m overlay",
                            "30m": "30m overlay", "60m": "60m mean overlay",
                            "60m-med": "60m MEDIAN overlay"}
            gbox = QGroupBox(_gbox_titles.get(w_key, f"{w_key} overlay"))
            form = QFormLayout(gbox)

            def _color_button(initial_hex):
                btn = QPushButton("    ")
                btn.setFixedSize(60, 22)
                btn.setStyleSheet(
                    f"background-color:{initial_hex};"
                    f"border:1px solid #888;border-radius:3px")
                btn._hex = initial_hex
                def pick():
                    c = QColorDialog.getColor(QColor(btn._hex), self,
                                              "Pick color")
                    if c.isValid():
                        btn._hex = c.name()
                        btn.setStyleSheet(
                            f"background-color:{btn._hex};"
                            f"border:1px solid #888;border-radius:3px")
                btn.clicked.connect(pick)
                return btn

            btn_co2 = _color_button(grp.get(f"color_co2_{w_key}", d["color_co2"]))
            form.addRow("CO₂ color:", btn_co2)
            btn_t = _color_button(grp.get(f"color_t_{w_key}", d["color_t"]))
            form.addRow("T color:", btn_t)
            btn_rh = _color_button(grp.get(f"color_rh_{w_key}", d["color_rh"]))
            form.addRow("RH color:", btn_rh)

            cb_ls = QComboBox()
            cb_ls.addItems(["solid (-)", "dashed (--)", "dotted (:)", "dash-dot (-.)"])
            ls_to_idx = {"-": 0, "--": 1, ":": 2, "-.": 3}
            cb_ls.setCurrentIndex(ls_to_idx.get(
                grp.get(f"linestyle_{w_key}", d["linestyle"]).strip(), 0))
            form.addRow("Line style:", cb_ls)

            sp_lw = QDoubleSpinBox()
            sp_lw.setRange(0.2, 8.0); sp_lw.setSingleStep(0.1); sp_lw.setDecimals(2)
            try: sp_lw.setValue(float(grp.get(f"line_width_{w_key}", d["line_width"])))
            except (TypeError, ValueError): sp_lw.setValue(d["line_width"])
            form.addRow("Line width (pt):", sp_lw)

            sp_alpha = QDoubleSpinBox()
            sp_alpha.setRange(0.05, 1.0); sp_alpha.setSingleStep(0.05); sp_alpha.setDecimals(2)
            try: sp_alpha.setValue(float(grp.get(f"alpha_{w_key}", d["alpha"])))
            except (TypeError, ValueError): sp_alpha.setValue(d["alpha"])
            form.addRow("Alpha (0..1):", sp_alpha)

            self.window_widgets[w_key] = {
                "btn_co2": btn_co2, "btn_t": btn_t, "btn_rh": btn_rh,
                "cb_ls": cb_ls, "sp_lw": sp_lw, "sp_alpha": sp_alpha,
            }
            root.addWidget(gbox)

        root.addStretch()
        scroll.setWidget(inner)
        return scroll

    # ────────────────────────────────────────────── helpers + save
    @staticmethod
    def _cfg_bool(v, default: bool = False) -> bool:
        if v is None:
            return default
        return str(v).strip().lower() in ("true", "yes", "1", "on")

    def _browse_dir(self, line_edit: QLineEdit) -> None:
        start = os.path.expanduser(line_edit.text().strip() or "~")
        d = QFileDialog.getExistingDirectory(self, "Select folder", start)
        if d:
            line_edit.setText(d)

    def _on_save(self) -> None:
        try:
            # name.ini
            ini = self._read_ini(os.path.join(self.config_dir, "name.ini"))
            if "output" not in ini:
                ini["output"] = {}
            ini["output"]["data_path"] = self.ed_data_path.text().strip()
            ini["output"]["basename"]  = self.ed_basename.text().strip()
            ini["output"]["extension"] = self.ed_extension.text().strip()
            self._write_ini(os.path.join(self.config_dir, "name.ini"), ini)

            # serial.ini
            ini = self._read_ini(os.path.join(self.config_dir, "serial.ini"))
            if "serial" not in ini:
                ini["serial"] = {}
            ini["serial"]["port"]     = self.ed_serial_port.text().strip()
            ini["serial"]["baudrate"] = self.cb_serial_baud.currentText().strip()
            ini["serial"]["bytesize"] = str(self.sp_bytesize.value())
            ini["serial"]["parity"]   = self.cb_parity.currentText()
            ini["serial"]["stopbits"] = str(self.sp_stopbits.value())
            # `timeout` letto dal logger con getint() → scrivi come intero
            ini["serial"]["timeout"]  = str(int(round(self.sp_serial_timeout.value())))
            self._write_ini(os.path.join(self.config_dir, "serial.ini"), ini)

            # site.ini
            ini = self._read_ini(os.path.join(self.config_dir, "site.ini"))
            if "location" not in ini:
                ini["location"] = {}
            ini["location"]["name"]      = self.ed_site_name.text().strip()
            ini["location"]["latitude"]  = f"{self.sp_lat.value():.6f}"
            ini["location"]["longitude"] = f"{self.sp_lon.value():.6f}"
            ini["location"]["timezone"]  = self.cb_tz.currentText().strip()
            self._write_ini(os.path.join(self.config_dir, "site.ini"), ini)

            # sensors.ini
            ini = self._read_ini(os.path.join(self.config_dir, "sensors.ini"))
            for key, (chk, sp_bus, ed_addr) in self._sens_widgets.items():
                if key not in ini:
                    ini[key] = {}
                ini[key]["enabled"] = "true" if chk.isChecked() else "false"
                ini[key]["bus"]     = str(sp_bus.value())
                ini[key]["addr"]    = ed_addr.text().strip() or "0x44"
            self._write_ini(os.path.join(self.config_dir, "sensors.ini"), ini)

            # monitor.ini — Aspetto (window geometry + font + grafico)
            mini = self._read_ini(MONITOR_INI)
            if "window" not in mini:
                mini["window"] = {}
            mini["window"]["x"]      = str(self.sp_win_x.value())
            mini["window"]["y"]      = str(self.sp_win_y.value())
            mini["window"]["width"]  = str(self.sp_win_width.value())
            mini["window"]["height"] = str(self.sp_win_height.value())
            if "fonts" not in mini:
                mini["fonts"] = {}
            mini["fonts"]["co2_value_size"] = str(self.sp_co2_value_size.value())
            mini["fonts"]["label_size"]     = str(self.sp_label_size.value())
            mini["fonts"]["caption_size"]   = str(self.sp_caption_size.value())
            mini["fonts"]["file_path_size"] = str(self.sp_file_path_size.value())
            mini["fonts"]["title_size"]     = str(self.sp_title_size.value())
            mini["fonts"]["subtitle_size"]  = str(self.sp_subtitle_size.value())
            mini["fonts"]["small_size"]     = str(self.sp_small_size.value())
            if "graph" not in mini:
                mini["graph"] = {}
            _idx_to_style = {0: "lines+points", 1: "lines", 2: "points"}
            mini["graph"]["style"]          = _idx_to_style[self.cb_graph_style.currentIndex()]
            mini["graph"]["point_size"]     = str(self.sp_point_size.value())
            mini["graph"]["line_width"]     = f"{self.sp_line_width.value():.2f}"
            mini["graph"]["trh_style"]      = _idx_to_style[self.cb_trh_style.currentIndex()]
            mini["graph"]["trh_point_size"] = str(self.sp_trh_point_size.value())
            mini["graph"]["trh_line_width"] = f"{self.sp_trh_line_width.value():.2f}"
            # Per-window overlay styles
            _idx_to_ls = {0: "-", 1: "--", 2: ":", 3: "-."}
            for w_key, widgets in self.window_widgets.items():
                mini["graph"][f"color_co2_{w_key}"]  = widgets["btn_co2"]._hex
                mini["graph"][f"color_t_{w_key}"]    = widgets["btn_t"]._hex
                mini["graph"][f"color_rh_{w_key}"]   = widgets["btn_rh"]._hex
                mini["graph"][f"linestyle_{w_key}"]  = _idx_to_ls[widgets["cb_ls"].currentIndex()]
                mini["graph"][f"line_width_{w_key}"] = f"{widgets['sp_lw'].value():.2f}"
                mini["graph"][f"alpha_{w_key}"]      = f"{widgets['sp_alpha'].value():.2f}"
            self._write_ini(MONITOR_INI, mini)

        except OSError as exc:
            QMessageBox.critical(self, "Write error", str(exc))
            return

        # Confirm + optional logger restart
        reply = QMessageBox.question(
            self, "Saved",
            "Configuration written.\n\n"
            "To apply it to the CO2 logger backend (serial port, "
            "data folder) a service restart is needed:\n\n"
            "  sudo systemctl restart co2-logger\n\n"
            "Do you want to do it now?",
            QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            try:
                subprocess.run(["sudo", "-n", "systemctl", "restart",
                                "co2-logger"], check=True, timeout=10)
                QMessageBox.information(
                    self, "Restart done",
                    "co2-logger restarted.")
            except (subprocess.CalledProcessError,
                    subprocess.TimeoutExpired,
                    FileNotFoundError) as exc:
                QMessageBox.warning(
                    self, "Automatic restart failed",
                    f"Cannot automatically restart "
                    f"(needs passwordless sudo):\n{exc}\n\n"
                    "Run manually from a terminal:\n"
                    "  sudo systemctl restart co2-logger")
        self.accept()


class TabValve(QWidget):
    """Tab valvola VICI — controllo completo via IPC verso valve-daemon.

    Embed TabSchedule (editor schedule + controlli engine) dal package
    valvescheduler. I segnali vengono ridiretti a un client IPC che parla
    con il daemon via Unix socket. La GUI non apre la seriale: solo il
    daemon possiede il VICI (niente race condition).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._log = logging.getLogger("valve-tab")
        # IPC client (lazy-importato per non rompere il monitor su sistemi
        # senza valve-scheduler installato)
        from valvescheduler.core.ipc_client import (
            DaemonClient, DaemonError, DaemonUnreachable)
        from valvescheduler.gui.main_window import TabSchedule
        self._DaemonError = DaemonError
        self._DaemonUnreachable = DaemonUnreachable
        self._client = DaemonClient(timeout=2.0)
        self._TabSchedule = TabSchedule
        self._build_ui()
        # Refresh stato dal daemon (1s)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start(1000)
        self._refresh_status()

    # ───────────────────────────────────────────────────────────── UI
    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        # Header: stato daemon + comandi manuali immediati
        hdr = QHBoxLayout()
        self._lbl_daemon = QLabel("● daemon: ?")
        self._lbl_daemon.setFont(QFont("Arial", 10, QFont.Bold))
        hdr.addWidget(self._lbl_daemon)
        hdr.addSpacing(16)

        self._lbl_pos_live = QLabel("pos=—")
        self._lbl_pos_live.setFont(QFont("Arial", 14, QFont.Bold))
        hdr.addWidget(self._lbl_pos_live)
        hdr.addSpacing(8)
        self._lbl_label_live = QLabel("")
        self._lbl_label_live.setStyleSheet("color:#444;font-size:10pt")
        hdr.addWidget(self._lbl_label_live)

        hdr.addStretch()

        self._btn_home = QPushButton("Home (HM)")
        self._btn_home.setToolTip("Go to position 1")
        self._btn_home.clicked.connect(self._on_home)
        hdr.addWidget(self._btn_home)
        self._btn_configure = QPushButton("Configure actuator")
        self._btn_configure.setToolTip(
            "IFM1 + AM3 + SM A + NP (multipos valve)")
        self._btn_configure.clicked.connect(self._on_configure)
        hdr.addWidget(self._btn_configure)
        self._btn_settings = QPushButton("Settings…")
        self._btn_settings.setToolTip(
            "Edit serial port, n_positions, polling, etc.")
        self._btn_settings.clicked.connect(self._on_settings)
        hdr.addWidget(self._btn_settings)
        lay.addLayout(hdr)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        lay.addWidget(line)

        # TabSchedule (riusato): tabella editabile + Start/Stop/Pause/Skip/Loop
        service_dir = Path(
            "/home/misura/programs/valve-scheduler/service")
        service_dir.mkdir(parents=True, exist_ok=True)
        self._tab_sched = self._TabSchedule(
            service_dir=service_dir, n_positions=10)
        # Auto-carica l'ultimo schedule usato (priorità _last_schedule.csv,
        # fallback schedule/schedule.csv, fallback default 1..N). Senza
        # questo l'utente avrebbe dovuto ricaricare il CSV ad ogni avvio
        # della GUI Monitor.
        try:
            default_csv = service_dir.parent / "schedule" / "schedule.csv"
            self._tab_sched.load_initial(
                default_csv if default_csv.exists() else None)
        except Exception as exc:
            self._log.warning("auto-load schedule failed: %s", exc)
        # Inietta "Sincronizza schedule" nella riga pulsanti tabella, dopo
        # `Salva CSV…` e dopo lo stretch → allineato a destra.
        if hasattr(self._tab_sched, "_table_btn_row"):
            self._btn_sync = QPushButton("⟳ Sync schedule")
            self._btn_sync.setToolTip(
                "Send labels/positions/durations of the table to the daemon.\n"
                "Press after editing cells: the IdlePoller will use the\n"
                "new labels for `step_label` in the _min.raw file.")
            self._btn_sync.clicked.connect(self._on_sync_schedule)
            self._tab_sched._table_btn_row.addWidget(self._btn_sync)
        lay.addWidget(self._tab_sched, stretch=1)

        # Wire signals → IPC client
        self._tab_sched.start_requested.connect(self._on_start)
        self._tab_sched.stop_requested.connect(self._on_stop)
        self._tab_sched.pause_requested.connect(self._on_pause)
        self._tab_sched.resume_requested.connect(self._on_resume)
        self._tab_sched.skip_requested.connect(self._on_skip)
        self._tab_sched.loop_toggled.connect(self._on_loop_toggled)
        self._tab_sched.go_position_requested.connect(self._on_manual_go)
        # Pulsante manuale "Sincronizza schedule al daemon" (vedi _build_ui)
        # Push iniziale: lo schedule caricato da _last_schedule.csv all'avvio
        # del monitor è già la versione confermata, quindi va bene mandarla.
        QTimer.singleShot(500, self._push_schedule_to_daemon)

        # AUTO-PUSH all'edit: quando l'utente modifica una cella della tabella
        # schedule (es. la label del nome bombola), pusha lo schedule al daemon
        # in automatico (debounce 1.5s) — così la label arriva subito in
        # valve_status.json → .raw → striscia, senza dover premere Sync.
        # cellChanged scatta a edit CONFERMATO (non per tasto), quindi è sicuro;
        # il debounce coalescente evita push multipli su edit di più celle.
        self._autopush_timer = QTimer(self)
        self._autopush_timer.setSingleShot(True)
        self._autopush_timer.timeout.connect(self._on_autopush)
        try:
            self._tab_sched._table.cellChanged.connect(
                lambda *_: self._autopush_timer.start(1500))
        except Exception as exc:
            self._log.warning("auto-push schedule non agganciato: %s", exc)

    def _on_autopush(self) -> None:
        """Debounced: pusha lo schedule editato al daemon + persiste il CSV."""
        try:
            if self._push_schedule_to_daemon(silent=True):
                self._tab_sched._persist()
        except Exception as exc:
            self._log.warning("auto-push schedule fallito: %s", exc)

    # ────────────────────────────────────────────── refresh stato daemon
    def _refresh_status(self) -> None:
        if not self._client.is_alive():
            self._lbl_daemon.setText("● daemon: OFFLINE")
            self._lbl_daemon.setStyleSheet(
                "color:#c00;font-weight:bold;font-size:10pt")
            self._set_btns_enabled(False)
            return
        try:
            st = self._client.get_status()
        except self._DaemonError as exc:
            self._lbl_daemon.setText(f"● daemon: error ({exc})")
            self._lbl_daemon.setStyleSheet(
                "color:#c00;font-weight:bold;font-size:10pt")
            return
        valve_open = bool(st.get("valve_open"))
        sched_running = bool(st.get("schedule_running"))
        if valve_open:
            self._lbl_daemon.setText("● daemon: ONLINE • valve open")
            self._lbl_daemon.setStyleSheet(
                "color:#1a7f37;font-weight:bold;font-size:10pt")
        else:
            self._lbl_daemon.setText("● daemon: ONLINE • valve closed")
            self._lbl_daemon.setStyleSheet(
                "color:#b58900;font-weight:bold;font-size:10pt")
        # Position live
        pos = int(st.get("position", -1))
        self._lbl_pos_live.setText(f"pos={pos}" if pos >= 1 else "pos=—")
        if pos == 1:
            self._lbl_pos_live.setStyleSheet("color:#2060c0;font-weight:bold")
        elif pos == 10:
            self._lbl_pos_live.setStyleSheet("color:#c00000;font-weight:bold")
        elif pos >= 1:
            self._lbl_pos_live.setStyleSheet("color:#e06000;font-weight:bold")
        else:
            self._lbl_pos_live.setStyleSheet("color:#888;font-weight:bold")
        # Label live (dal valve_status.json, già letto da read_live_valve)
        live_pos, live_label, live_fresh = read_live_valve()
        if live_fresh and live_label and live_label not in ("-", ""):
            self._lbl_label_live.setText(f"({live_label})")
        else:
            self._lbl_label_live.setText("")
        self._set_btns_enabled(valve_open and not sched_running)
        # Aggiorna countdown + stato + bottoni Start/Stop del TabSchedule.
        # L'IPC get_status è minimale (niente state/seconds_remaining/step):
        # lo stato schedule completo è solo in valve_status.json, da cui
        # TabSchedule.on_status() ricava countdown, step e abilitazione bottoni.
        # Senza questo, nel monitor il countdown resta fermo e "Start" sempre
        # abilitato anche con schedule in esecuzione.
        try:
            with _VALVE_STATUS_JSON.open("r", encoding="utf-8") as _vf:
                self._tab_sched.on_status(json.load(_vf))
        except Exception:
            pass

    def _set_btns_enabled(self, enabled: bool) -> None:
        self._btn_home.setEnabled(enabled)
        self._btn_configure.setEnabled(enabled)

    # ────────────────────────────────────────────── azioni → IPC client
    def _safe_call(self, fn, *args, **kwargs) -> bool:
        try:
            fn(*args, **kwargs)
            return True
        except self._DaemonUnreachable:
            QMessageBox.warning(self, "Daemon unreachable",
                                "valve-daemon is not responding. Check:\n"
                                "  systemctl status valve-daemon")
            return False
        except self._DaemonError as exc:
            QMessageBox.warning(self, "Daemon error", str(exc))
            return False

    def _on_home(self):
        self._safe_call(self._client.home)

    def _on_configure(self):
        if QMessageBox.question(
                self, "Configure actuator",
                "Applying IFM1 + AM3 + SM A + NP10 and HM.\n"
                "Proceed only after running AL with the valve removed.\n\n"
                "Continue?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        if self._safe_call(self._client.configure):
            QMessageBox.information(
                self, "Configuration done",
                "Actuator configured in multiposition mode "
                "(AM3, NP10) and Home executed.\n"
                "Current position: 1.")

    def _on_manual_go(self, position: int):
        self._safe_call(self._client.go, int(position))

    def _on_start(self):
        sched = self._tab_sched.get_schedule()
        steps = [{"position": s.position,
                  "minutes": s.minutes,
                  "label": s.label} for s in sched.steps]
        loop = self._tab_sched._chk_loop.isChecked()
        if not steps:
            QMessageBox.warning(self, "Empty schedule",
                                "Add at least one step.")
            return
        # Persiste localmente (autosave)
        try:
            self._tab_sched._persist()
        except Exception:
            pass
        self._safe_call(self._client.start_schedule, steps, loop)

    def _on_stop(self):
        self._safe_call(self._client.stop_schedule)

    def _on_pause(self):
        self._safe_call(self._client.pause)

    def _on_resume(self):
        self._safe_call(self._client.resume)

    def _on_skip(self):
        self._safe_call(self._client.skip)

    def _on_loop_toggled(self, enabled: bool):
        self._safe_call(self._client.set_loop, bool(enabled))

    # ───────────────────────────── push label allo daemon (no start)
    def _push_schedule_to_daemon(self, silent: bool = True) -> bool:
        """Manda la schedule corrente (label per posizione) al daemon SENZA
        avviarla. Permette all'IdlePoller di scrivere `step_label` corretta
        nel JSON (e quindi nel file _min.raw) anche senza premere Start.

        `silent=True` → non mostra dialog di errore (push automatico iniziale).
        `silent=False` → mostra messaggi di errore (chiamata da pulsante)."""
        try:
            sched = self._tab_sched.get_schedule()
        except Exception as exc:
            if not silent:
                QMessageBox.warning(self, "Error",
                                    f"Schedule not readable: {exc}")
            return False
        if not sched.steps:
            if not silent:
                QMessageBox.warning(self, "Empty schedule",
                                    "Add at least one step first.")
            return False
        steps = [{"position": s.position,
                  "minutes": s.minutes,
                  "label": s.label} for s in sched.steps]
        try:
            self._client.set_schedule(steps)
            return True
        except self._DaemonUnreachable:
            if not silent:
                QMessageBox.warning(self, "Daemon unreachable",
                                    "Check `systemctl status valve-daemon`.")
            return False
        except self._DaemonError as exc:
            if not silent:
                QMessageBox.warning(self, "Daemon error", str(exc))
            return False

    def _on_sync_schedule(self) -> None:
        if self._push_schedule_to_daemon(silent=False):
            # autosave also to CSV for persistence across restart
            try:
                self._tab_sched._persist()
            except Exception:
                pass
            QMessageBox.information(
                self, "Synced",
                "Schedule sent to the daemon. The labels have also been saved\n"
                "to _last_schedule.csv (restored on next launch).")

    def _on_settings(self):
        try:
            current = self._client.get_config()
        except (self._DaemonError, self._DaemonUnreachable) as exc:
            QMessageBox.warning(self, "Daemon unavailable",
                                f"Cannot read config: {exc}")
            return
        dlg = DaemonSettingsDialog(current, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return
        new_cfg = dlg.values()
        # Compare with current: if identical, no reload
        if all(current.get(k) == v for k, v in new_cfg.items()):
            QMessageBox.information(self, "No changes",
                                    "Settings are unchanged.")
            return
        try:
            self._client.reload_config(new_cfg)
        except self._DaemonError as exc:
            QMessageBox.warning(self, "Reload failed", str(exc))
            return
        except self._DaemonUnreachable as exc:
            QMessageBox.warning(self, "Daemon unreachable", str(exc))
            return
        QMessageBox.information(
            self, "Settings saved",
            "Config updated. Daemon has reopened the serial port "
            "with the new settings.")
        # Aggiorna n_positions nella tabella schedule (TabSchedule)
        if hasattr(self._tab_sched, "set_n_positions"):
            try:
                self._tab_sched.set_n_positions(int(new_cfg["n_positions"]))
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────── cleanup
    def cleanup(self):
        # Niente da rilasciare: il client IPC apre/chiude socket per ogni call.
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  Finestra principale
# ══════════════════════════════════════════════════════════════════════════════

class GMP343Monitor(QMainWindow):

    def __init__(self):
        super().__init__()
        self.cfg     = self._load_config()
        self.guicfg  = self._load_gui_config()
        self._ports_cache     = []   # SER-007: cache comports() per evitare blocchi GUI
        self._ports_cache_age = 0    # tick counter
        self._build_ui()

        # File-based trigger per il free-RAM on-demand. PyQt5 non
        # propaga i Python signals quando app.exec_() è in corso, e
        # SIGUSR1 di default fa terminate → quindi NIENTE signal.
        # Lo script bin/co2-free-ram.sh crea questo file; _tick lo
        # rileva, lo cancella ed esegue il cleanup.
        # IMPORTANT: deve essere assegnato PRIMA di self._tick() perché
        # _tick lo legge.
        self._free_ram_trigger = "/tmp/co2-monitor-free-ram.trigger"
        self._tick_count = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(UPDATE_MS)
        self._tick()  # primo aggiornamento immediato
        # Tick rapido per i LED (legge valve_status.json, latenza ~2s)
        self.timer_fast = QTimer(self)
        self.timer_fast.timeout.connect(self._tick_fast)
        self.timer_fast.start(1000)

        # Diagnostica RSS (sempre) + tracemalloc (opt-in via env)
        self._setup_diagnostics()

    # ── diagnostica memoria ──────────────────────────────────────────────────

    def _setup_diagnostics(self):
        """Logging periodico RSS + tracemalloc snapshot (opt-in).

        RSS: una riga ogni 5 min su RSS_LOG_FILE. Costo: open/write ~µs.
        tracemalloc: opt-in via env MONITOR_TRACEMALLOC=1. Quando attivo,
        ogni 30 min logga i top-10 allocator differenziali rispetto al
        baseline iniziale su TRACEMALLOC_LOG_FILE. Costo: overhead ~5-10%
        sull'app — accettabile per diagnosi mirate (1-2 giorni).
        """
        self._diag_t0 = datetime.now()
        self._rss_timer = QTimer(self)
        self._rss_timer.timeout.connect(self._log_rss)
        self._rss_timer.start(RSS_LOG_INTERVAL_MS)
        self._log_rss()   # baseline a t=0

        if TRACEMALLOC_ENABLED:
            # Profondità bassa: il log usa solo l'ultima riga del traceback
            # (compare_to "lineno"), e depth alta rallenta MOLTO la GUI
            # (cambio tab lentissimo, specie via RustDesk). 4 frame bastano.
            tracemalloc.start(TRACEMALLOC_DEPTH)
            self._tm_baseline = tracemalloc.take_snapshot()
            self._tm_timer = QTimer(self)
            self._tm_timer.timeout.connect(self._log_tracemalloc)
            self._tm_timer.start(TRACEMALLOC_INTERVAL_MS)
            print(f"MONITOR: tracemalloc enabled → {TRACEMALLOC_LOG_FILE}",
                  file=sys.stderr, flush=True)

    def _read_self_rss_mb(self) -> float:
        """Legge VmRSS da /proc/self/status (MB). -1 se errore."""
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024.0
        except Exception:
            pass
        return -1.0

    def _log_rss(self):
        rss_mb = self._read_self_rss_mb()
        # Conteggio artisti matplotlib: se crescono nel tempo → leak di artisti
        # non rilasciati (diagnostica leak RAM, 2026-06-28).
        art = ""
        try:
            g = self.graph
            art = (f"  cont={len(g.ax.containers)+len(g.ax_t.containers)+len(g.ax_rh.containers)}"
                   f" coll={len(g.ax.collections)+len(g.ax_t.collections)+len(g.ax_rh.collections)}"
                   f" lines={len(g.ax.lines)+len(g.ax_t.lines)+len(g.ax_rh.lines)}"
                   f" texts={len(g.ax.texts)} patches={len(g.ax.patches)}")
        except Exception:
            pass
        try:
            with open(RSS_LOG_FILE, "a") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')}  "
                        f"pid={os.getpid()}  rss={rss_mb:7.1f} MB  "
                        f"tick={self._tick_count}{art}\n")
        except OSError:
            pass

    def _log_tracemalloc(self):
        if not TRACEMALLOC_ENABLED:
            return
        try:
            snap = tracemalloc.take_snapshot()
            diff = snap.compare_to(self._tm_baseline, "lineno")
            rss_mb = self._read_self_rss_mb()
            with open(TRACEMALLOC_LOG_FILE, "a") as f:
                f.write(f"\n===== {datetime.now().isoformat(timespec='seconds')}"
                        f"   rss={rss_mb:.1f} MB"
                        f"   uptime={(datetime.now()-self._diag_t0)}"
                        " =====\n")
                for stat in diff[:10]:
                    where = stat.traceback.format()[-1].strip()
                    f.write(f"  +{stat.size_diff/1024:9.1f} kB  "
                            f"+{stat.count_diff:6d} alloc   {where}\n")
                # Stack completo dei top-3 grower: il sito che CHIAMA il leaker
                # è quello che ci interessa (non l'ultima riga in libreria).
                diff_tb = snap.compare_to(self._tm_baseline, "traceback")
                f.write("  --- full traceback top-3 ---\n")
                for stat in diff_tb[:3]:
                    f.write(f"  +{stat.size_diff/1024:9.1f} kB  "
                            f"+{stat.count_diff:6d} alloc:\n")
                    for ln in stat.traceback.format():
                        f.write(f"      {ln}\n")
        except Exception as e:
            print(f"tracemalloc log error: {e}",
                  file=sys.stderr, flush=True)

    def _free_ram_now(self):
        """Best-effort RAM cleanup. Conservative on purpose: only gc.collect.

        We deliberately DO NOT call plt.close("all") here — even though
        we use Figure() and not pyplot, the FigureCanvasQTAgg backend
        has cross-references with pyplot's manager and closing 'all'
        can take down our canvas. Same for forcing a graph._reload(),
        which would interleave with the running tick.
        """
        import gc, resource
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        for _ in range(3):
            gc.collect()
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is in KiB on Linux
        print(f"FREE-RAM: maxRSS {rss_before/1024:.1f} MB → "
              f"{rss_after/1024:.1f} MB (gc.collect ×3)",
              file=sys.stderr, flush=True)

    # ── configurazione ────────────────────────────────────────────────────────

    def _load_config(self):
        cfg = configparser.ConfigParser()
        cfg.read([SERIAL_INI, SITE_INI, NAME_INI])
        return cfg

    def _load_gui_config(self):
        cfg = configparser.RawConfigParser()   # Raw: % non viene interpolato
        defaults = {
            "window": {"width":"1200","height":"800","x":"100","y":"50"},
            "thresholds": {"min_valid":"0","max_valid":"10000",
                           "low_warning":"300","high_warning":"2000",
                           "sentinel_value":"999.99"},
            "colors": {"normal_color":"#0066cc","low_color":"#ff9900",
                       "high_color":"#cc0000","invalid_color":"#999999"},
            "fonts": {"title_size":"14","subtitle_size":"9",
                      "co2_value_size":"24","label_size":"10","small_size":"8"},
            "display": {"show_out_of_range":"true","show_sentinel":"false",
                        "co2_decimals":"2"},
        }
        if os.path.exists(MONITOR_INI):
            cfg.read(MONITOR_INI)
        for sec, vals in defaults.items():
            if not cfg.has_section(sec):
                cfg.add_section(sec)
            for k, v in vals.items():
                if not cfg.has_option(sec, k):
                    cfg.set(sec, k, v)
        return cfg

    def _thr(self, key):
        return self.guicfg.getfloat("thresholds", key, fallback=0)

    def _color(self, value):
        s = self._thr("sentinel_value")
        # Accetta sia vecchia sentinella 999.99 sia nuova -999.99 (MISSING)
        if abs(value - s) < 0.1 or value == MISSING:
            return self.guicfg.get("colors", "invalid_color", fallback="#999")
        lo = self._thr("min_valid"); hi = self._thr("max_valid")
        if value < lo or value > hi:
            return self.guicfg.get("colors", "invalid_color", fallback="#999")
        if value > self._thr("high_warning"):
            return self.guicfg.get("colors", "high_color", fallback="#c00")
        if value < self._thr("low_warning"):
            return self.guicfg.get("colors", "low_color", fallback="#f90")
        return self.guicfg.get("colors", "normal_color", fallback="#06c")

    # ── costruzione UI ────────────────────────────────────────────────────────

    def _build_ui(self):
        w  = self.guicfg.getint("window", "width",  fallback=1200)
        h  = self.guicfg.getint("window", "height", fallback=800)
        x  = self.guicfg.getint("window", "x",      fallback=100)
        y  = self.guicfg.getint("window", "y",      fallback=50)
        self.setWindowTitle("GMP343 Monitor  v2")
        # Pavimento basso per il resize manuale: lascia ridurre la finestra
        # fino a 400×300 anche con font value grandi (24 pt). I widget
        # interni hanno sizePolicy Ignored sull'orizzontale per non bloccare.
        self.setMinimumSize(400, 300)
        self.setGeometry(x, y, w, h)

        # Menubar — Settings
        mb = self.menuBar()
        m_cfg = mb.addMenu("&Settings")
        act_cfg_co2 = QAction("CO2 logger settings…", self)
        act_cfg_co2.triggered.connect(self._open_co2_config)
        m_cfg.addAction(act_cfg_co2)
        if _HAS_VALVE_SCHEDULER:
            act_cfg_valve = QAction("Valve-daemon settings…", self)
            act_cfg_valve.triggered.connect(self._open_valve_config)
            m_cfg.addAction(act_cfg_valve)

        # Top-right corner of the menubar: stacked UTC clock + last valid
        # sample timestamp (two rows, compact format = HH:MM:SS only;
        # full date is in the tooltip). The widget is allowed to shrink so
        # it never blocks the window from being resized smaller.
        corner = QWidget()
        corner.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        c_lay = QVBoxLayout(corner)
        c_lay.setContentsMargins(8, 1, 10, 1)
        c_lay.setSpacing(0)
        self.lbl_now = QLabel("Now ---")
        self.lbl_now.setStyleSheet(
            "font-family:monospace;font-size:9pt;font-weight:bold;color:#222")
        self.lbl_now.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_now.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_now.setMinimumWidth(0)
        self.lbl_now.setToolTip("Current UTC time")
        c_lay.addWidget(self.lbl_now)
        self.lbl_last_valid = QLabel("Last valid ---")
        self.lbl_last_valid.setStyleSheet(
            "font-family:monospace;font-size:9pt;color:#666")
        self.lbl_last_valid.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_last_valid.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_last_valid.setMinimumWidth(0)
        self.lbl_last_valid.setToolTip(
            "Timestamp of the last 1-min average with a valid CO₂ value")
        c_lay.addWidget(self.lbl_last_valid)
        mb.setCornerWidget(corner, Qt.TopRightCorner)

        tabs = QTabWidget()
        self._main_tabs = tabs
        self.setCentralWidget(tabs)
        # OTTIMIZZAZIONE CPU: quando l'utente torna sul tab Chart, forza un
        # refresh immediato (altrimenti il grafico è stato "congelato" mentre
        # era nascosto — vedi _tick/_tick_fast, che saltano il redraw se il
        # grafico non è visibile).
        tabs.currentChanged.connect(self._on_main_tab_changed)

        def _scrollable(widget):
            """Avvolge widget in un QScrollArea per permettere a tutto il
            contenuto di restare leggibile anche a finestra piccola."""
            sa = QScrollArea()
            sa.setWidgetResizable(True)
            sa.setFrameShape(QFrame.NoFrame)
            sa.setWidget(widget)
            return sa

        # Monitor: scroll perché Last Sample/Stats stanno sotto il blocco CO₂.
        tabs.addTab(_scrollable(self._build_monitor_tab()), "📊  Monitor")
        # Chart: NO scroll — la canvas matplotlib è già responsive.
        tabs.addTab(self._build_graph_tab(), "📈  Chart")
        if _HAS_VALVE_SCHEDULER:
            self.tab_valve = TabValve()
            # VICI Valve: scroll per leggere Step sequence anche su finestra
            # piccola (la tabella ha sizeHint largo).
            tabs.addTab(_scrollable(self.tab_valve), "🔧  VICI Valve")
        else:
            self.tab_valve = None
        tabs.setStyleSheet("QTabBar::tab { padding: 6px 18px; font-size: 11pt; }")

    # ── tab monitor ───────────────────────────────────────────────────────────

    def _build_monitor_tab(self):
        tab = QWidget()
        vbox = QVBoxLayout(tab)

        # header
        vbox.addLayout(self._make_header())

        # seriale
        grp_ser = QGroupBox("Serial")
        h = QHBoxLayout()
        h.addWidget(QLabel("Port:"))
        self.lbl_port = QLabel("---"); h.addWidget(self.lbl_port)
        h.addWidget(QLabel("Baud:"))
        self.lbl_baud = QLabel(self.cfg.get("serial","baudrate",fallback="19200"))
        h.addWidget(self.lbl_baud)
        self.lbl_dot  = QLabel("●")
        self.lbl_dot.setStyleSheet("font-size:18px;color:#c00")
        h.addWidget(self.lbl_dot)
        self.lbl_stat = QLabel("Disconnected")
        self.lbl_stat.setStyleSheet("font-weight:bold;color:#c00;font-size:10px")
        h.addWidget(self.lbl_stat)
        h.addStretch(); grp_ser.setLayout(h)
        vbox.addWidget(grp_ser)

        # Current Data — two columns side by side, separated by a vertical
        # rule. Left = Live (last sample from .raw, ~1 Hz), Right = 1-min
        # average (last row of _min.raw). σ and n live in the right column
        # because they're statistics OF the 1-min average, not of the live.
        grp_co2 = QGroupBox("Current Data — CO₂ corrected (compensated)")
        sz = self.guicfg.getint("fonts","co2_value_size",fallback=24)
        cap_sz = self.guicfg.getint("fonts","caption_size",fallback=14)

        def _make_value_row(prefix: str, value_size: int, color: str, unit: str):
            """Returns (row_layout, value_label) → 'PREFIX:  VALUE unit'.

            Le label hanno minimum width = 0 per non bloccare il resize
            verticale della finestra a dimensioni piccole; NON usiamo
            QSizePolicy.Ignored perché in combinazione con row.addStretch()
            faceva collassare la label di valore a 0 px (testo aggiornato
            ma invisibile — regressione 2026-05-20). Il pavimento del
            resize è gestito da self.setMinimumSize(400, 300) + dalla
            QScrollArea che wrappa la tab Monitor.
            """
            row = QHBoxLayout()
            lbl_p = QLabel(prefix)
            lbl_p.setStyleSheet(f"font-weight:bold;font-size:{cap_sz}px")
            lbl_p.setMinimumWidth(0)
            row.addWidget(lbl_p)
            lbl = QLabel(f"--- {unit}")
            lbl.setFont(QFont("Arial", value_size, QFont.Bold))
            lbl.setStyleSheet(f"color:{color}")
            lbl.setMinimumWidth(0)
            row.addWidget(lbl)
            row.addStretch()
            return row, lbl

        def _make_column(title: str, title_color: str):
            """Empty VBox with a colored bold title at the top."""
            col = QVBoxLayout(); col.setSpacing(6)
            head = QLabel(title)
            head.setStyleSheet(
                f"color:{title_color};font-weight:bold;font-size:{cap_sz+2}px;"
                f"border-bottom:2px solid {title_color};padding-bottom:2px")
            head.setMinimumWidth(0)
            col.addWidget(head)
            return col

        # — LEFT COLUMN: Live —
        col_live = _make_column("LIVE", "#0066cc")
        row, self.lbl_co2_live = _make_value_row("CO₂:", sz,    "#0066cc", "ppm")
        col_live.addLayout(row)
        row, self.lbl_t_live   = _make_value_row("T:",   14,    "#c05000", "°C")
        col_live.addLayout(row)
        row, self.lbl_rh_live  = _make_value_row("RH:",  14,    "#007060", "%")
        col_live.addLayout(row)
        row, self.lbl_p_live   = _make_value_row("P:",   14,    "#5030a0", "hPa")
        col_live.addLayout(row)
        # Flusso TSI 4140: massa (SLPM, misura nativa) e volumetrico (Lpm, calc.)
        row, self.lbl_fmass_live = _make_value_row("Flow mass:", 14, "#8a0a8a", "slpm")
        col_live.addLayout(row)
        row, self.lbl_fvol_live  = _make_value_row("Flow vol:",  14, "#a83232", "Lpm")
        col_live.addLayout(row)
        col_live.addStretch()

        # — RIGHT COLUMN: 1-min average + σ + n (stats of the average) —
        col_avg = _make_column("1-MIN AVERAGE", "#444")
        row, self.lbl_co2 = _make_value_row("CO₂:", sz, "#0066cc", "ppm")
        col_avg.addLayout(row)
        # σ + n right under the CO₂ 1-min value
        row_stats = QHBoxLayout()
        lbl_sigma = QLabel("σ:"); lbl_sigma.setStyleSheet(f"font-weight:bold;font-size:{cap_sz}px")
        row_stats.addWidget(lbl_sigma)
        self.lbl_std = QLabel("---")
        self.lbl_std.setStyleSheet(f"font-size:{cap_sz}px")
        row_stats.addWidget(self.lbl_std)
        row_stats.addSpacing(12)
        lbl_n = QLabel("n:"); lbl_n.setStyleSheet(f"font-weight:bold;font-size:{cap_sz}px")
        row_stats.addWidget(lbl_n)
        self.lbl_n = QLabel("---")
        self.lbl_n.setStyleSheet(f"font-size:{cap_sz}px")
        row_stats.addWidget(self.lbl_n)
        row_stats.addStretch()
        col_avg.addLayout(row_stats)
        row, self.lbl_t   = _make_value_row("T:",   14, "#c05000", "°C")
        col_avg.addLayout(row)
        row, self.lbl_rh  = _make_value_row("RH:",  14, "#007060", "%")
        col_avg.addLayout(row)
        row, self.lbl_p   = _make_value_row("P:",   14, "#5030a0", "hPa")
        col_avg.addLayout(row)
        row, self.lbl_fmass = _make_value_row("Flow mass:", 14, "#8a0a8a", "slpm")
        col_avg.addLayout(row)
        row, self.lbl_fvol  = _make_value_row("Flow vol:",  14, "#a83232", "Lpm")
        col_avg.addLayout(row)
        col_avg.addStretch()

        # — Outer HBox: live | vertical separator | 1-min —
        outer = QHBoxLayout(); outer.setSpacing(14)
        outer.addLayout(col_live, stretch=1)
        sep = QFrame(); sep.setFrameShape(QFrame.VLine); sep.setFrameShadow(QFrame.Sunken)
        sep.setStyleSheet("color:#999")
        outer.addWidget(sep)
        outer.addLayout(col_avg, stretch=1)

        vco2 = QVBoxLayout()
        vco2.addLayout(outer)
        # Status line below (warnings on the live CO₂ value)
        self.lbl_status = QLabel("")
        self.lbl_status.setFont(QFont("Arial", 9))
        vco2.addWidget(self.lbl_status)
        grp_co2.setLayout(vco2); vbox.addWidget(grp_co2)

        # Daily statistics
        grp_st = QGroupBox("Daily Statistics — CO₂ corrected (compensated)")
        g = QGridLayout(); g.setSpacing(3)
        g.addWidget(QLabel("Min:"),0,0)
        self.lbl_min = QLabel("---"); self.lbl_min.setStyleSheet("color:#090;font-weight:bold;font-size:10px"); g.addWidget(self.lbl_min,0,1)
        g.addWidget(QLabel("Max:"),0,2)
        self.lbl_max = QLabel("---"); self.lbl_max.setStyleSheet("color:#c00;font-weight:bold;font-size:10px"); g.addWidget(self.lbl_max,0,3)
        g.addWidget(QLabel("Mean:"),1,0)
        self.lbl_avg = QLabel("---"); self.lbl_avg.setStyleSheet("color:#06c;font-weight:bold;font-size:10px"); g.addWidget(self.lbl_avg,1,1)
        g.addWidget(QLabel("Samples:"),1,2)
        self.lbl_cnt = QLabel("0"); self.lbl_cnt.setStyleSheet("font-size:10px"); g.addWidget(self.lbl_cnt,1,3)
        grp_st.setLayout(g); vbox.addWidget(grp_st)

        # Last sample
        grp_last = QGroupBox("Last Sample")
        vl = QVBoxLayout(); vl.setSpacing(2)
        self.lbl_ts   = QLabel("---"); self.lbl_ts.setFont(QFont("Arial",9,QFont.Bold)); vl.addWidget(self.lbl_ts)
        self.lbl_flag = QLabel("---")
        self.lbl_flag.setFont(QFont("Arial", 9, QFont.Bold))
        self.lbl_flag.setAlignment(Qt.AlignLeft)
        vl.addWidget(self.lbl_flag)
        self.lbl_file = QLabel("---")
        fp_sz = self.guicfg.getint("fonts","file_path_size",fallback=13)
        self.lbl_file.setStyleSheet(f"color:#666;font-size:{fp_sz}px")
        self.lbl_file.setWordWrap(True)
        vl.addWidget(self.lbl_file)
        grp_last.setLayout(vl); vbox.addWidget(grp_last)

        # Thresholds
        grp_thr = QGroupBox("Thresholds")
        ht = QHBoxLayout(); ht.setSpacing(5)
        lo = self._thr("low_warning"); hi = self._thr("high_warning")
        ht.addWidget(QLabel("OK:"))
        lb = QLabel(f"{lo:.0f}–{hi:.0f}"); lb.setStyleSheet("color:#06c;font-weight:bold;font-size:9px"); ht.addWidget(lb)
        ht.addWidget(QLabel("High:"))
        la = QLabel(f">{hi:.0f}"); la.setStyleSheet("color:#c00;font-weight:bold;font-size:9px"); ht.addWidget(la)
        ht.addStretch(); grp_thr.setLayout(ht); vbox.addWidget(grp_thr)

        vbox.addStretch()
        return tab

    def _make_header(self):
        hbox = QHBoxLayout()
        img = QLabel()
        if os.path.exists(SENSOR_IMG):
            px = QPixmap(SENSOR_IMG).scaled(80,80, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            img.setPixmap(px)
        else:
            img.setText("GMP343"); img.setFixedSize(80,80)
            img.setAlignment(Qt.AlignCenter)
            img.setStyleSheet("border:2px solid #333;background:#f0f0f0;font-weight:bold")
        hbox.addWidget(img)
        vt = QVBoxLayout()
        ts = self.guicfg.getint("fonts","title_size",fallback=14)
        tl = QLabel("GMP343 CO₂\nMonitor")
        tl.setFont(QFont("Arial", ts, QFont.Bold))
        vt.addWidget(tl)
        site = self.cfg.get("location","name",fallback="Unknown")
        sl = QLabel(f"Site: {site}")
        sl.setFont(QFont("Arial", self.guicfg.getint("fonts","subtitle_size",fallback=9)))
        vt.addWidget(sl)
        hbox.addLayout(vt); hbox.addStretch()
        # LED stato MEASURE/CALIB in alto a destra (replica quello della tab Grafico)
        self.lbl_flag_top = QLabel("MEASURE")
        self.lbl_flag_top.setFont(QFont("Arial", 11, QFont.Bold))
        self.lbl_flag_top.setAlignment(Qt.AlignCenter)
        self.lbl_flag_top.setStyleSheet(
            "color:#2060c0;background:#ffffff;"
            "border:1.5px solid #2060c0;border-radius:8px;"
            "padding:4px 12px;")
        hbox.addWidget(self.lbl_flag_top, 0, Qt.AlignTop)
        return hbox

    # ── tab grafico ───────────────────────────────────────────────────────────

    def _build_graph_tab(self):
        self.graph = GraphWidget(self.cfg)
        return self.graph

    # ── menu Configurazione ──────────────────────────────────────────────────

    def _open_co2_config(self):
        dlg = MonitorConfigDialog(CONFIG_DIR, parent=self)
        dlg.exec_()

    def _open_valve_config(self):
        if hasattr(self, "tab_valve") and self.tab_valve is not None:
            self.tab_valve._on_settings()

    # ── tick timer ────────────────────────────────────────────────────────────

    def _tick(self):
        # Wrap tutto in try/except: una qualsiasi eccezione qui dentro
        # NON deve uccidere il QTimer (sintomo classico: GUI freeze con
        # grafico fermo a uno stato precedente). Loggiamo su stderr che
        # finisce in /tmp/co2monitor.log per diagnosi.
        t0 = time.monotonic()
        # File-based free-RAM trigger (vedi bin/co2-free-ram.sh)
        try:
            if os.path.exists(self._free_ram_trigger):
                os.unlink(self._free_ram_trigger)
                self._free_ram_now()
        except Exception as exc:
            self._log_tick_error("free_ram_trigger", exc)
        try:
            self._update_monitor()
        except Exception as exc:
            self._log_tick_error("update_monitor", exc)
        try:
            # OTTIMIZZAZIONE CPU: il redraw del grafico è la voce di CPU più
            # pesante del monitor. Se il tab Chart non è visibile (utente su
            # Monitor/VICI o finestra minimizzata) NON ridisegnare: i valori
            # live/1-min della finestra Current Data sono aggiornati da
            # _update_monitor (testo, economico). Al ritorno sul tab Chart
            # _on_main_tab_changed forza il refresh.
            if self.graph.isVisible():
                self.graph.refresh()
        except Exception as exc:
            self._log_tick_error("graph.refresh", exc)
        # Reload count + GC periodico per liberare riferimenti matplotlib
        # morti accumulati nelle finestre overlay (worst case: 17k reload
        # al giorno con 4 overlay → vale la pena un gc.collect() ogni
        # tanto per evitare crescita di RAM nel tempo).
        self._tick_count = getattr(self, "_tick_count", 0) + 1
        if self._tick_count % 100 == 0:
            import gc
            gc.collect()
        # Diagnostica: se il tick è stato molto lento, segnalalo
        dt = time.monotonic() - t0
        if dt > 2.0:
            print(f"WARN: _tick took {dt:.2f}s (#{self._tick_count})",
                  file=sys.stderr, flush=True)

    def _log_tick_error(self, where: str, exc: Exception) -> None:
        """Log a tick exception to stderr (visible in /tmp/co2monitor.log)
        without killing the timer."""
        import traceback
        print(f"ERROR in {where}: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)

    def _tick_fast(self):
        """Tick rapido (1s): orologio UTC nel corner della menubar + LED
        MEASURE/CALIB letti direttamente da valve_status.json (latenza ~2s,
        la posizione è già visibile nel tab VICI Valve alla stessa cadenza).
        Wrapped in try/except so a single failure doesn't stop the QTimer
        (symptom: clock freezes at the failure time)."""
        try:
            self._tick_fast_impl()
        except Exception as exc:
            self._log_tick_error("tick_fast", exc)

    def _tick_fast_impl(self):
        # UTC clock — formato compatto (HH:MM:SS); data completa nel tooltip
        now = datetime.utcnow()
        self.lbl_now.setText(now.strftime("Now %H:%M:%S"))
        self.lbl_now.setToolTip(now.strftime("Current UTC: %Y-%m-%d %H:%M:%S"))

        live_pos, live_label, live_fresh = read_live_valve()
        if not live_fresh or live_pos < 1:
            return  # senza dato live, lasciamo lo stato del tick lento
        last_pos = live_pos
        last_flag = "measure" if live_pos == _MEASURE_POSITION else "calib"
        if last_flag == "calib" and last_pos == 10:
            color = "#c00000"; text = f"CALIB pos{last_pos}"
        elif last_flag == "calib":
            color = "#e06000"; text = f"CALIB pos{last_pos}"
        else:
            color = "#2060c0"; text = "MEASURE"
        if live_label and live_label != "-":
            text = f"{text} ({live_label})"
        # Monitor tab — LED in alto a destra + footer "Ultima Acquisizione"
        if hasattr(self, "lbl_flag_top"):
            self.lbl_flag_top.setText(text)
            self.lbl_flag_top.setStyleSheet(
                f"color:{color};background:#ffffff;"
                f"border:1.5px solid {color};border-radius:8px;padding:4px 12px;")
        if hasattr(self, "lbl_flag"):
            self.lbl_flag.setText(f"● {text}")
            self.lbl_flag.setStyleSheet(
                f"color:{color};font-weight:bold;font-size:10px")
        # Tab Grafico — flag_label in alto a destra del plot.
        # OTTIMIZZAZIONE CPU: prima si faceva draw_idle() dell'INTERO canvas
        # OGNI SECONDO solo per aggiornare questa etichetta, che però cambia
        # rarissimamente (measure↔calib). Ora ridisegniamo SOLO quando il testo
        # o il colore cambiano davvero, E solo se il tab Chart è visibile.
        if hasattr(self, "graph") and hasattr(self.graph, "flag_label"):
            if (text, color) != getattr(self, "_last_graph_flag", None):
                self._last_graph_flag = (text, color)
                self.graph.flag_label.set_text(text)
                self.graph.flag_label.set_color(color)
                self.graph.flag_label.get_bbox_patch().set_edgecolor(color)
                if self.graph.isVisible():
                    self.graph.canvas.draw_idle()

    def _on_main_tab_changed(self, _idx=None):
        """Quando si passa a un tab, se è il Chart forza subito un refresh
        (il grafico è rimasto congelato mentre era nascosto per risparmiare CPU)."""
        try:
            if hasattr(self, "graph") and self.graph.isVisible():
                self._last_graph_flag = None   # forza il redraw del flag
                self.graph.refresh()
        except Exception as exc:
            self._log_tick_error("tab_changed", exc)

    def _update_monitor(self):
        # Timestamp
        # Seriale — controlla esistenza device (supporta symlink udev come /dev/gmp343)
        port = self.cfg.get("serial","port",fallback="/dev/ttyUSB0")
        if os.path.exists(port):
            self.lbl_dot.setStyleSheet("font-size:18px;color:#090")
            self.lbl_stat.setText("Connected"); self.lbl_stat.setStyleSheet("font-weight:bold;color:#090;font-size:10px")
            self.lbl_port.setText(port)
        else:
            self.lbl_dot.setStyleSheet("font-size:18px;color:#c00")
            self.lbl_stat.setText("Disconnected"); self.lbl_stat.setStyleSheet("font-weight:bold;color:#c00;font-size:10px")
            self.lbl_port.setText(f"{port} (N/A)")

        # Ultimo dato
        today = datetime.utcnow().date()
        path  = build_filename(self.cfg, today)   # glob → path reale o ""
        result = load_period(self.cfg, today, 1)

        # Tempo reale: ultimo campione dal file .raw (indipendente da _min)
        live_ts, live_co2, live_t, live_rh, _live_flag, live_p, live_fmass, live_fvol = read_last_raw_sample(
            self.cfg, today)
        dec = self.guicfg.getint("display","co2_decimals",fallback=2)
        if live_co2 is None or live_co2 == MISSING:
            self.lbl_co2_live.setText("--- ppm")
        else:
            col_live = self._color(live_co2)
            self.lbl_co2_live.setText(f"{live_co2:.{dec}f} ppm")
            self.lbl_co2_live.setStyleSheet(f"color:{col_live};font-weight:bold")
        if live_t is None or live_t == MISSING:
            self.lbl_t_live.setText("--- °C")
        else:
            self.lbl_t_live.setText(f"{live_t:.2f} °C")
        if live_rh is None or live_rh == MISSING:
            self.lbl_rh_live.setText("--- %")
        else:
            self.lbl_rh_live.setText(f"{live_rh:.2f} %")
        if live_p is None or live_p == MISSING:
            self.lbl_p_live.setText("--- hPa")
        else:
            self.lbl_p_live.setText(f"{live_p:.2f} hPa")
        # Flusso TSI live (dall'ultimo campione .raw)
        if live_fmass is None or live_fmass == MISSING:
            self.lbl_fmass_live.setText("--- slpm")
        else:
            self.lbl_fmass_live.setText(f"{live_fmass:.3f} slpm")
        if live_fvol is None or live_fvol == MISSING:
            self.lbl_fvol_live.setText("--- Lpm")
        else:
            self.lbl_fvol_live.setText(f"{live_fvol:.3f} Lpm")
        # Flusso TSI 1-min (media dall'ultima riga del file _min)
        fm_avg, fv_avg = read_last_min_flow(self.cfg, today)
        self.lbl_fmass.setText("--- slpm" if fm_avg is None
                               else f"{fm_avg:.3f} slpm")
        self.lbl_fvol.setText("--- Lpm" if fv_avg is None
                              else f"{fv_avg:.3f} Lpm")

        if result is None:
            self.lbl_co2.setText("--- ppm"); self.lbl_status.setText("")
            self.lbl_std.setText("---"); self.lbl_n.setText("---")
            self.lbl_t.setText("--- °C"); self.lbl_rh.setText("--- %")
            self.lbl_p.setText("--- hPa")
            self.lbl_ts.setText("No data")
            self.lbl_flag.setText("---"); self.lbl_flag.setStyleSheet("color:#888;font-size:9px")
            self.lbl_flag_top.setText("---")
            self.lbl_flag_top.setStyleSheet(
                "color:#888;background:#ffffff;"
                "border:1.5px solid #888;border-radius:8px;padding:4px 12px;")
            self.lbl_file.setText("file not found" if not path else path)
            self.lbl_min.setText("---"); self.lbl_max.setText("---")
            self.lbl_avg.setText("---"); self.lbl_cnt.setText("0")
            self.lbl_last_valid.setText("Last valid: ---")
            return

        (times, values, stds, counts, flags,
         t_arr, t_std_arr, rh_arr, rh_std_arr, p_arr,
         valve_pos, valve_labels, _fm_arr, _fv_arr) = result
        last_co2  = float(values[-1])
        last_std  = float(stds[-1])
        last_n    = int(counts[-1])
        last_ts   = times[-1].strftime("%Y/%m/%d %H:%M:%S")
        last_flag = flags[-1] if len(flags) > 0 else "measure"
        last_t    = float(t_arr[-1])
        last_t_std  = float(t_std_arr[-1])
        last_rh   = float(rh_arr[-1])
        last_rh_std = float(rh_std_arr[-1])

        # Label flag — fonte LIVE per aggiornamento immediato:
        #   1) valve_status.json (latenza ~2s, scritta dall'IdlePoller)
        #   2) fallback: ultimo flag/pos del file _min.raw (latenza ~60s)
        live_pos, live_label, live_fresh = read_live_valve()
        if live_fresh and live_pos >= 1:
            last_pos = live_pos
            last_flag = "measure" if live_pos == _MEASURE_POSITION else "calib"
            label_for_text = live_label
        else:
            last_pos = int(valve_pos[-1]) if len(valve_pos) > 0 else -1
            label_for_text = (valve_labels[-1] if len(valve_labels) > 0
                              and valve_labels[-1] not in ("-", "")
                              else "")
        # Pos 10 → rosso (span-high), altre pos calib → arancione, pos 1 → blu.
        if last_flag == "calib" and last_pos == 10:
            color = "#c00000"
            text_top = f"CALIB pos{last_pos}"
        elif last_flag == "calib":
            color = "#e06000"
            text_top = f"CALIB pos{last_pos}" if last_pos >= 1 else "CALIB"
        else:
            color = "#2060c0"
            text_top = "MEASURE"
        if label_for_text and label_for_text != "-":
            text_top = f"{text_top} ({label_for_text})"
        self.lbl_flag.setText(f"● {text_top}")
        self.lbl_flag.setStyleSheet(
            f"color:{color};font-weight:bold;font-size:10px")
        self.lbl_flag_top.setText(text_top)
        self.lbl_flag_top.setStyleSheet(
            f"color:{color};background:#ffffff;"
            f"border:1.5px solid {color};border-radius:8px;padding:4px 12px;")

        # Filtra sentinella per statistiche (sia vecchia 999.99 che nuova -999.99)
        sent = self._thr("sentinel_value")
        valid = values[(np.abs(values - sent) > 0.1) & (values != MISSING)]

        dec = self.guicfg.getint("display","co2_decimals",fallback=2)
        col = self._color(last_co2)
        if last_std == MISSING or last_co2 == MISSING:
            self.lbl_co2.setText(f"{last_co2:.{dec}f} ppm")
        else:
            self.lbl_co2.setText(f"{last_co2:.{dec}f} ± {last_std:.{dec}f} ppm")
        self.lbl_co2.setStyleSheet(f"color:{col};font-weight:bold")
        self.lbl_std.setText(f"{last_std:.2f} ppm")
        self.lbl_n.setText(str(last_n))
        # T and RH 1-min average: show "value ± std unit" (placeholder if MISSING).
        # If T_std/RH_std are MISSING but the value is valid (legacy v2 file or
        # n=1 minute) we show only the value, no ±.
        def _fmt(v, sd, unit, dec=2):
            if v == MISSING:
                return f"--- {unit}"
            if sd == MISSING:
                return f"{v:.{dec}f} {unit}"
            return f"{v:.{dec}f} ± {sd:.{dec}f} {unit}"

        self.lbl_t.setText(_fmt(last_t, last_t_std, "°C"))
        self.lbl_rh.setText(_fmt(last_rh, last_rh_std, "%"))
        # P 1-min average (P P_std nel _min). NON usare indice negativo: dal
        # 2026-07-02 ci sono colonne flusso IN CODA, quindi _last[-2]/_last[-1]
        # sarebbero FLOWvol/FLOWvol_std (bug: mostrava ~0.6 "hPa"). Si usa
        # l'indice FORWARD dopo valvola + blocco CO2RAW/CO2RAWUC (P a
        # tail_start+4, P_std a +5), come read_file.
        p_avg, p_sd = MISSING, MISSING
        try:
            with open(path) as _f:
                _last = [ln for ln in _f.read().splitlines()
                         if ln and not ln.startswith("#")][-1].split()
            def _isf(s):
                try:
                    float(s); return True
                except (ValueError, TypeError):
                    return False
            # valvola presente se _last[11] (valve_label) NON è numerico
            has_v = len(_last) >= 12 and not _isf(_last[11])
            p_pos = (12 if has_v else 10) + 4
            p_avg = float(_last[p_pos]); p_sd = float(_last[p_pos + 1])
        except Exception:
            p_avg, p_sd = MISSING, MISSING
        self.lbl_p.setText(_fmt(p_avg, p_sd, "hPa"))
        self.lbl_ts.setText(last_ts)
        self.lbl_file.setText(path)

        # Last valid sample time (last index where CO₂ != MISSING).
        # Compact format: HH:MM:SS (today) o YYYY-MM-DD HH:MM (older).
        valid_idx = np.where(values != MISSING)[0]
        if valid_idx.size:
            ts = times[valid_idx[-1]]
            if ts.date() == datetime.utcnow().date():
                short = ts.strftime("Last valid %H:%M:%S")
            else:
                short = ts.strftime("Last valid %m-%d %H:%M")
            self.lbl_last_valid.setText(short)
            self.lbl_last_valid.setToolTip(
                ts.strftime("Last valid 1-min sample: %Y-%m-%d %H:%M:%S UTC"))
        else:
            self.lbl_last_valid.setText("Last valid ---")
            self.lbl_last_valid.setToolTip(
                "No valid 1-min sample found in current data window")

        # Stato
        hi = self._thr("high_warning"); lo = self._thr("low_warning")
        if last_co2 > hi:
            self.lbl_status.setText(f"⚠ Above threshold ({hi:.0f} ppm)")
            self.lbl_status.setStyleSheet("color:#c00;font-weight:bold")
        elif last_co2 < lo:
            self.lbl_status.setText(f"⚠ Below threshold ({lo:.0f} ppm)")
            self.lbl_status.setStyleSheet("color:#f90;font-weight:bold")
        else:
            self.lbl_status.setText(""); self.lbl_status.setStyleSheet("")

        if len(valid) > 0:
            self.lbl_min.setText(f"{np.min(valid):.{dec}f} ppm")
            self.lbl_max.setText(f"{np.max(valid):.{dec}f} ppm")
            self.lbl_avg.setText(f"{np.mean(valid):.{dec}f} ppm")
            self.lbl_cnt.setText(str(len(valid)))
        else:
            self.lbl_min.setText("---"); self.lbl_max.setText("---")
            self.lbl_avg.setText("---"); self.lbl_cnt.setText("0")


# ══════════════════════════════════════════════════════════════════════════════
#  Avvio
# ══════════════════════════════════════════════════════════════════════════════

    def closeEvent(self, event):
        # GUI-001/ARCH-004: ferma timer; GUI-002: rilascia Figure matplotlib
        self.timer.stop()
        self.graph.cleanup()
        if self.tab_valve:
            self.tab_valve.cleanup()
        event.accept()


def main():
    _startup_log()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = GMP343Monitor()
    win.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
