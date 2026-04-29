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

import sys, os, json, signal, subprocess, logging
from datetime import datetime, timedelta, timezone, date as date_type
import configparser
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QGridLayout, QTabWidget, QPushButton,
    QComboBox, QDateEdit, QCheckBox, QFrame, QMessageBox,
    QDialog, QFormLayout, QSpinBox, QDoubleSpinBox, QLineEdit,
    QDialogButtonBox, QFileDialog, QToolButton,
    QPlainTextEdit, QScrollArea, QAction, QSizePolicy
)
from PyQt5.QtCore  import QTimer, Qt, QDate
from PyQt5.QtGui   import QFont, QPixmap
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
MIN_Y_RANGE     = 20.0   # range Y minimo (ppm)
Y_MARGIN_FACTOR = 0.10   # margine verticale relativo



# ══════════════════════════════════════════════════════════════════════════════
#  Funzioni dati
# ══════════════════════════════════════════════════════════════════════════════

def get_data_dir(cfg: configparser.ConfigParser) -> str:
    """Legge data_path da name.ini ed espande ~ (identico al logger)."""
    raw = cfg.get("output", "data_path", fallback="~/data")
    return os.path.expanduser(raw)


def build_filename(cfg: configparser.ConfigParser, d: date_type) -> str:
    """
    Trova il file _min per la data d usando glob (underscore nei nomi).
    Pattern: *_<YYYYMMDD>_p00_min.ext
    Restituisce stringa vuota se nessun file trovato.
    """
    import glob
    ext  = cfg.get("output", "extension", fallback="raw")
    ddir = get_data_dir(cfg)
    pattern = os.path.join(ddir, f"*_{d.strftime('%Y%m%d')}_p00_min.{ext}")
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
        return (None, None, None, None, None)
    path = max(matches, key=os.path.getmtime)
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(4096, size)
            f.seek(max(0, size - chunk))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return (None, None, None, None, None)
    lines = [ln.strip() for ln in tail.splitlines()
             if ln.strip() and not ln.startswith("#")]
    if not lines:
        return (None, None, None, None, None)
    parts = lines[-1].split()
    if len(parts) < 4:
        return (None, None, None, None, None)
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
        return (ts, None, None, None, None)
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
    return (ts, co2, t, rh, flag)


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


def read_file(path: str):
    """
    Legge un file dati _min.
    Supporta sia formato v3 (con T/RH, 10+ colonne) sia v2 (solo CO2, 6+ colonne):
      v3: date time CO2 CO2_std T T_std RH RH_std n flag [valve_pos valve_label]
      v2: date time CO2 CO2_std n flag [valve_pos valve_label]
    Per i file v2 T e RH sono restituiti come MISSING.
    Colonne valvola opzionali (integrazione valve-scheduler).
    Timestamp: YYYY-MM-DD HH:MM:SS
    Ritorna: (times, values, stds, counts, flags, t, t_std, rh, rh_std,
              valve_pos, valve_labels)
    """
    times, values, stds, counts, flags = [], [], [], [], []
    ts_t, ts_tstd, ts_rh, ts_rhstd = [], [], [], []
    valve_pos, valve_labels = [], []
    has_valve_cols = False
    if not path or not os.path.exists(path):
        return (times, values, stds, counts, flags,
                ts_t, ts_tstd, ts_rh, ts_rhstd,
                valve_pos, valve_labels)
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
                    # Discriminazione formato: v3 ha 9 colonne (ts=2 parole), v2 ne ha 5.
                    # Dopo ts+CO2 restano len(p)-3 colonne:
                    #   v3 → 7 (CO2_std T T_std RH RH_std n flag)
                    #   v2 → 3 (CO2_std n flag)  o 2 (n flag)  o 1 (flag)
                    remaining = len(p) - 3
                    if remaining >= 7:
                        # v3: CO2_std T T_std RH RH_std n flag
                        co2_std = float(p[3])
                        t_val   = float(p[4])
                        t_std   = float(p[5])
                        rh_val  = float(p[6])
                        rh_std  = float(p[7])
                        n       = int(p[8])
                        flag    = p[9].lower() if len(p) >= 10 else "measure"
                    else:
                        # v2: [CO2_std [n [flag]]]
                        co2_std = float(p[3]) if len(p) >= 4 else 0.0
                        n       = int(p[4])   if len(p) >= 5 else 1
                        flag    = p[5].lower() if len(p) >= 6 else "measure"
                        t_val, t_std, rh_val, rh_std = MISSING, MISSING, MISSING, MISSING
                    if flag not in ("measure", "calib"):
                        flag = "measure"
                    # Colonne opzionali valve-scheduler (dopo il flag)
                    # v3: posizioni p[10], p[11]; v2: posizioni p[6], p[7]
                    if remaining >= 7:
                        # v3: valve dopo flag a p[9]
                        valve_idx = 10
                    else:
                        # v2: valve dopo flag a p[5]
                        valve_idx = 6
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
                    times.append(dt)
                    values.append(co2)
                    stds.append(co2_std)
                    counts.append(n)
                    flags.append(flag)
                    ts_t.append(t_val)
                    ts_tstd.append(t_std)
                    ts_rh.append(rh_val)
                    ts_rhstd.append(rh_std)
                    valve_pos.append(vpos)
                    valve_labels.append(vlab)
                except ValueError:
                    continue
    except OSError:
        pass
    if not has_valve_cols:
        valve_pos, valve_labels = [], []
    return (times, values, stds, counts, flags,
            ts_t, ts_tstd, ts_rh, ts_rhstd,
            valve_pos, valve_labels)


def load_period(cfg: configparser.ConfigParser,
                start: date_type, n_days: int):
    """
    Carica n_days giorni a partire da start; ritorna array numpy ordinati.
    Tuple: (times, values, stds, counts, flags, t, t_std, rh, rh_std,
            valve_pos, valve_labels)
    valve_pos/valve_labels sono array vuoti se nessun file ha colonne valvola.
    """
    all_t, all_v, all_s, all_c, all_f = [], [], [], [], []
    all_tt, all_tstd, all_rh, all_rhstd = [], [], [], []
    all_vp, all_vl = [], []
    n_with_valve = 0
    for i in range(n_days):
        d = start + timedelta(days=i)
        t, v, s, c, f, tt, tstd, rh, rhstd, vp, vl = read_file(build_filename(cfg, d))
        all_t.extend(t)
        all_v.extend(v)
        all_s.extend(s)
        all_c.extend(c)
        all_f.extend(f)
        all_tt.extend(tt)
        all_tstd.extend(tstd)
        all_rh.extend(rh)
        all_rhstd.extend(rhstd)
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
    if n_with_valve > 0:
        valve_pos    = np.array(all_vp, dtype=int)[idx]
        valve_labels = np.array(all_vl)[idx]
    else:
        valve_pos    = np.array([], dtype=int)
        valve_labels = np.array([], dtype=str)
    return (times, values, stds, counts, flags,
            t_arr, tstd, rh_arr, rhstd,
            valve_pos, valve_labels)


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

        bar.addStretch()
        root.addLayout(bar)

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

    def _load_graph_style(self):
        """Legge da monitor.ini la sezione [graph] con default sicuri.

        Restituisce un dict con:
          - co2 line/scatter: style, point_size, line_width
          - T/RH line/scatter: trh_style, trh_point_size, trh_line_width
        """
        gcp = configparser.ConfigParser()
        if os.path.exists(MONITOR_INI):
            gcp.read(MONITOR_INI)

        def _norm_style(s):
            s = (s or "").strip().lower()
            return s if s in ("lines+points", "lines", "points") else "lines+points"

        return {
            "style":           _norm_style(gcp.get("graph", "style", fallback="lines+points")),
            "point_size":      max(2, gcp.getint("graph", "point_size", fallback=18)),
            "line_width":      max(0.2, gcp.getfloat("graph", "line_width", fallback=1.0)),
            "trh_style":       _norm_style(gcp.get("graph", "trh_style", fallback="lines")),
            "trh_point_size":  max(2, gcp.getint("graph", "trh_point_size", fallback=10)),
            "trh_line_width":  max(0.2, gcp.getfloat("graph", "trh_line_width", fallback=1.0)),
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

        # 2 subplots con asse X condiviso: CO2 (grande sopra), T+RH (piccolo sotto)
        gs = self.fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.08)
        self.ax    = self.fig.add_subplot(gs[0])
        self.ax_t  = self.fig.add_subplot(gs[1], sharex=self.ax)
        # Asse Y secondario (a destra) per RH sul pannello di T
        self.ax_rh = self.ax_t.twinx()

        # Nasconde i tick label X sul pannello CO2 (ax_t è l'asse X attivo)
        self.ax.tick_params(labelbottom=False)

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

        self.ax.set_ylabel("CO₂  1-min avg (ppm)", fontsize=10)
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
        result = load_period(self.cfg, start, n_days)

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
        def _remove_eb(eb):
            if eb is None:
                return
            try:
                if eb[0] is not None:
                    eb[0].remove()
                for cap in eb[1]:
                    cap.remove()
                for bar in eb[2]:
                    bar.remove()
            except Exception:
                pass

        _remove_eb(self._errorbar);    self._errorbar = None
        _remove_eb(self._errorbar_t);  self._errorbar_t = None
        _remove_eb(self._errorbar_rh); self._errorbar_rh = None

        if result is None:
            self.line.set_data([], [])
            self.line_t.set_data([], [])
            self.line_rh.set_data([], [])
            self.sc_measure.set_offsets(np.empty((0, 2)))
            self.sc_calib.set_offsets(np.empty((0, 2)))
            self.sc_t.set_offsets(np.empty((0, 2)))
            self.sc_rh.set_offsets(np.empty((0, 2)))
            self.flag_label.set_text("MEASURE")
            self.flag_label.set_color("#2060c0")
            self.flag_label.get_bbox_patch().set_edgecolor("#2060c0")
            self.ax.set_title("No data available", fontsize=11, loc="left")
            self._set_x_axis(start, n_days)
            self.canvas.draw_idle()
            self._ignore_lim_change = False
            return

        (times, values, stds, counts, flags,
         t_arr, t_std_arr, rh_arr, rh_std_arr,
         valve_pos, valve_labels) = result
        xt = mdates.date2num(times)
        # Sostituisci MISSING con NaN per i plot (break nella linea, no Y axis esteso)
        values_plot = np.where(values == MISSING, np.nan, values)
        t_plot      = np.where(t_arr  == MISSING, np.nan, t_arr)
        rh_plot     = np.where(rh_arr == MISSING, np.nan, rh_arr)
        # Salva per accesso dal tooltip
        self._t_plot  = t_plot
        self._rh_plot = rh_plot

        # ── Linee continue ────────────────────────────────────────────────
        self.line.set_data(xt, values_plot)
        self.line_t.set_data(xt, t_plot)
        self.line_rh.set_data(xt, rh_plot)

        # ── Scatter punti T e RH (filtra MISSING) ─────────────────────────
        mask_t  = t_arr  != MISSING
        mask_rh = rh_arr != MISSING
        if mask_t.any():
            self.sc_t.set_offsets(np.column_stack([xt[mask_t], t_arr[mask_t]]))
        else:
            self.sc_t.set_offsets(np.empty((0, 2)))
        if mask_rh.any():
            self.sc_rh.set_offsets(np.column_stack([xt[mask_rh], rh_arr[mask_rh]]))
        else:
            self.sc_rh.set_offsets(np.empty((0, 2)))

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
        want_valve = (hasattr(self, "chk_valve") and self.chk_valve.isChecked()
                      and valve_pos.size == len(times))
        if want_valve:
            # Rileva i run contigui di stessa posizione (trascura -1 = sconosciuta)
            from matplotlib import cm as _cm
            cmap = _cm.get_cmap("tab20")
            i = 0
            while i < len(valve_pos):
                cur_pos = int(valve_pos[i])
                if cur_pos < 1:
                    i += 1
                    continue
                j = i + 1
                while j < len(valve_pos) and int(valve_pos[j]) == cur_pos:
                    j += 1
                x0 = xt[i]
                x1 = xt[j-1] if j-1 < len(xt) else xt[-1]
                # estendi x1 di mezzo minuto (medie 1-min) per coprire l'intervallo
                x1_ext = x1 + (1.0 / (60 * 24)) * 0.5
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
                    lab = str(valve_labels[i]) if valve_labels.size > i else ""
                    if lab and lab != "-":
                        text = f"{cur_pos} {lab}"
                    else:
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

        # ── Titolo e label flag sulla stessa riga ─────────────────────────
        site  = self.cfg.get("location", "name", fallback="")
        label = self.combo.currentText()
        if n_days == 1:
            date_str = start.strftime("%Y-%m-%d")
        else:
            date_str = f"{start}  →  {start + timedelta(days=n_days-1)}"
        self.ax.set_title(f"{site}   CO₂   {date_str}  [{label}]",
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

    def refresh(self):
        """Chiamato dal timer: aggiorna solo se il periodo include oggi."""
        start, n_days = self._period_range()
        today = datetime.utcnow().date()
        end   = start + timedelta(days=n_days - 1)
        if start <= today <= end:
            self._reload()
        self._refresh_toolbar_readouts()

    def _refresh_toolbar_readouts(self):
        """Aggiorna i tre label "Live / 1-min avg / σ" nella toolbar grafico."""
        today = datetime.utcnow().date()
        # Live: ultima riga del .raw (~1 Hz)
        _, live_co2, _, _, _ = read_last_raw_sample(self.cfg, today)
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
        if event.inaxes is not self.ax:
            self._hide_tooltip()
            return

        contains, info = self.line.contains(event)
        if not contains:
            self._hide_tooltip()
            return

        idx = info["ind"][0]
        xd, yd = self.line.get_data()
        xv, yv = xd[idx], yd[idx]

        # Punto evidenziato
        self.hl_pt.set_data([xv], [yv])
        self.hl_pt.set_visible(True)

        # Testo tooltip: CO2 + T + RH allo stesso idx
        t_str = mdates.num2date(xv).strftime("%H:%M:%S")
        lines = [f"Ora:  {t_str}", f"CO₂: {yv:.2f} ppm"]
        if hasattr(self, "_t_plot") and idx < len(self._t_plot):
            tv = self._t_plot[idx]
            if not np.isnan(tv):
                lines.append(f"T:   {tv:.2f} °C")
        if hasattr(self, "_rh_plot") and idx < len(self._rh_plot):
            rv = self._rh_plot[idx]
            if not np.isnan(rv):
                lines.append(f"RH:  {rv:.2f} %")
        self.annot.set_text("\n".join(lines))
        self.annot.xy = (xv, yv)   # freccia punta al dato

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

        w = QWidget(); root = QVBoxLayout(w)

        info = QLabel(
            "Font sizes for the Monitor panel and CO₂ chart style.\n"
            "Changes require a Monitor restart to take effect.")
        info.setStyleSheet("color:#666;font-size:9pt"); info.setWordWrap(True)
        root.addWidget(info)

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

        root.addStretch()
        return w

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

            # monitor.ini — Aspetto (font + grafico)
            mini = self._read_ini(MONITOR_INI)
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

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(UPDATE_MS)
        self._tick()  # primo aggiornamento immediato
        # Tick rapido per i LED (legge valve_status.json, latenza ~2s)
        self.timer_fast = QTimer(self)
        self.timer_fast.timeout.connect(self._tick_fast)
        self.timer_fast.start(1000)

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
        self.setCentralWidget(tabs)
        tabs.addTab(self._build_monitor_tab(), "📊  Monitor")
        tabs.addTab(self._build_graph_tab(),   "📈  Chart")
        if _HAS_VALVE_SCHEDULER:
            self.tab_valve = TabValve()
            tabs.addTab(self.tab_valve, "🔧  VICI Valve")
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
        grp_co2 = QGroupBox("Current Data")
        sz = self.guicfg.getint("fonts","co2_value_size",fallback=24)
        cap_sz = self.guicfg.getint("fonts","caption_size",fallback=14)

        def _make_value_row(prefix: str, value_size: int, color: str, unit: str):
            """Returns (row_layout, value_label) → 'PREFIX:  VALUE unit'."""
            row = QHBoxLayout()
            lbl_p = QLabel(prefix)
            lbl_p.setStyleSheet(f"font-weight:bold;font-size:{cap_sz}px")
            row.addWidget(lbl_p)
            lbl = QLabel(f"--- {unit}")
            lbl.setFont(QFont("Arial", value_size, QFont.Bold))
            lbl.setStyleSheet(f"color:{color}")
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
        grp_st = QGroupBox("Daily Statistics")
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
        self._update_monitor()
        self.graph.refresh()

    def _tick_fast(self):
        """Tick rapido (1s): orologio UTC nel corner della menubar + LED
        MEASURE/CALIB letti direttamente da valve_status.json (latenza ~2s,
        la posizione è già visibile nel tab VICI Valve alla stessa cadenza)."""
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
        # Tab Grafico — flag_label in alto a destra del plot
        if hasattr(self, "graph") and hasattr(self.graph, "flag_label"):
            self.graph.flag_label.set_text(text)
            self.graph.flag_label.set_color(color)
            self.graph.flag_label.get_bbox_patch().set_edgecolor(color)
            self.graph.canvas.draw_idle()

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
        live_ts, live_co2, live_t, live_rh, _live_flag = read_last_raw_sample(
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

        if result is None:
            self.lbl_co2.setText("--- ppm"); self.lbl_status.setText("")
            self.lbl_std.setText("---"); self.lbl_n.setText("---")
            self.lbl_t.setText("--- °C"); self.lbl_rh.setText("--- %")
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
         t_arr, t_std_arr, rh_arr, rh_std_arr,
         valve_pos, valve_labels) = result
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
