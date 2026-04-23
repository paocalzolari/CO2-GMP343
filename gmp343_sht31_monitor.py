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

import sys, os, logging
from datetime import datetime, timedelta, timezone, date as date_type
import configparser
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QGridLayout, QTabWidget, QPushButton,
    QComboBox, QDateEdit, QCheckBox, QFrame, QMessageBox
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
try:
    if _VALVE_SCHED_DIR not in sys.path:
        sys.path.insert(0, _VALVE_SCHED_DIR)
    from valvescheduler.core.config import load_cfg as vs_load_cfg
    from valvescheduler.core.engine import ScheduleEngine
    from valvescheduler.core.schedule import load_schedule_csv, save_schedule_csv, default_example
    from valvescheduler.core.mqtt_pub import build_publisher as vs_build_publisher
    from valvescheduler.hardware.vici_emtca import VICIEMTCA, VICIError, VICITimeout
    from valvescheduler.gui.main_window import TabSchedule
    _HAS_VALVE_SCHEDULER = True
except ImportError:
    _HAS_VALVE_SCHEDULER = False

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

        bar.addWidget(QLabel("Periodo:"))
        self.combo = QComboBox()
        self.combo.addItems(["24h", "48h", "7 giorni", "Personalizzato"])
        self.combo.currentTextChanged.connect(self._on_period_change)
        bar.addWidget(self.combo)

        self.lbl_from = QLabel("Da:")
        bar.addWidget(self.lbl_from)
        self.date_from = QDateEdit(QDate.currentDate().addDays(-1))
        self.date_from.setCalendarPopup(True)
        self.date_from.dateChanged.connect(self._reload)
        bar.addWidget(self.date_from)

        self.lbl_to = QLabel("A:")
        bar.addWidget(self.lbl_to)
        self.date_to = QDateEdit(QDate.currentDate())
        self.date_to.setCalendarPopup(True)
        self.date_to.dateChanged.connect(self._reload)
        bar.addWidget(self.date_to)

        self._toggle_custom(False)

        if ASTRAL_OK:
            self.chk_night = QCheckBox("Zone notturne")
            self.chk_night.setChecked(True)
            self.chk_night.stateChanged.connect(self._reload)
            bar.addWidget(self.chk_night)

        # Checkbox posizione valvola (striscia colorata in basso).
        # Default ON: se i dati non la contengono, il disegno è automaticamente
        # no-op (retrocompat con file _min.raw storici a 6 colonne).
        self.chk_valve = QCheckBox("Posizione valvola")
        self.chk_valve.setChecked(True)
        self.chk_valve.setToolTip(
            "Mostra una striscia colorata in basso con la posizione della\n"
            "valvola VICI (richiede integration.ini abilitato).")
        self.chk_valve.stateChanged.connect(self._reload)
        bar.addWidget(self.chk_valve)

        btn_home = QPushButton("⌂ Home")
        btn_home.setToolTip("Torna alla vista completa")
        btn_home.clicked.connect(self._reset_view)
        bar.addWidget(btn_home)

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

    def _init_axes(self):
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
            linewidth=1.0,
            color="#2060c0", zorder=2
        )
        # Scatter punti MEASURE (blu) e CALIB (arancione) — sopra la linea
        self.sc_measure = self.ax.scatter(
            [], [], s=18, color="#2060c0",
            zorder=3, label="measure"
        )
        self.sc_calib = self.ax.scatter(
            [], [], s=28, color="#e06000",
            zorder=4, marker="D", label="calib"
        )

        # ── T e RH (pannello piccolo sotto, twin Y) ──────────────────────
        # T sull'asse Y sinistro (arancione)
        self.line_t, = self.ax_t.plot(
            [], [], "-",
            linewidth=1.0,
            color="#c05000", zorder=2, label="T"
        )
        self.ax_t.set_ylabel("T (°C)", fontsize=9, color="#c05000")
        self.ax_t.tick_params(axis="y", labelcolor="#c05000", labelsize=8)
        self.ax_t.grid(True, linestyle="--", linewidth=0.3, alpha=0.6)

        # RH sull'asse Y destro (verde-teal)
        self.line_rh, = self.ax_rh.plot(
            [], [], "-",
            linewidth=1.0,
            color="#007060", zorder=2, label="RH"
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

    # ── helper periodo ────────────────────────────────────────────────────────

    def _toggle_custom(self, show: bool):
        self.lbl_from.setVisible(show)
        self.date_from.setVisible(show)
        self.lbl_to.setVisible(show)
        self.date_to.setVisible(show)

    def _on_period_change(self, txt):
        self._toggle_custom(txt == "Personalizzato")
        self._reload()

    def _period_range(self):
        """Restituisce (start_date, n_days)."""
        txt = self.combo.currentText()
        today = datetime.utcnow().date()
        if txt == "24h":
            return today, 1
        elif txt == "48h":
            return today - timedelta(days=1), 2
        elif txt == "7 giorni":
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

        if result is None:
            self.line.set_data([], [])
            self.line_t.set_data([], [])
            self.line_rh.set_data([], [])
            self.sc_measure.set_offsets(np.empty((0, 2)))
            self.sc_calib.set_offsets(np.empty((0, 2)))
            self.flag_label.set_text("MEASURE")
            self.flag_label.set_color("#2060c0")
            self.flag_label.get_bbox_patch().set_edgecolor("#2060c0")
            self.ax.set_title("Nessun dato disponibile", fontsize=11, loc="left")
            self._set_x_axis(start, n_days)
            self.canvas.draw_idle()
            self._ignore_lim_change = False
            return

        (times, values, stds, counts, flags,
         t_arr, _tstd, rh_arr, _rhstd,
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

        # ── Scatter per flag (solo punti validi, MISSING esclusi) ─────────
        mask_valid = values != MISSING
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

        # ── Label flag: stato dell'ULTIMA acquisizione ─────────────────────
        last_flag = flags[-1] if len(flags) > 0 else "measure"
        if last_flag == "calib":
            self.flag_label.set_text("CALIB")
            self.flag_label.set_color("#e06000")
            self.flag_label.get_bbox_patch().set_edgecolor("#e06000")
        else:
            self.flag_label.set_text("MEASURE")
            self.flag_label.set_color("#2060c0")
            self.flag_label.get_bbox_patch().set_edgecolor("#2060c0")

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

class TabValve(QWidget):
    """Tab integrata del valve-scheduler nel monitor CO2.

    Wrappa header connessione + TabSchedule dal package valvescheduler.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._valve = None
        self._engine = None
        self._publisher = None
        self._vs_cfg = None
        self._program_dir = Path(_VALVE_SCHED_DIR)
        self._log = logging.getLogger("valve-tab")
        self._build_ui()
        self._load_valve_config()

    def _load_valve_config(self):
        ini_path = self._program_dir / "config" / "valve-scheduler.ini"
        if ini_path.exists():
            self._vs_cfg = vs_load_cfg(str(ini_path))
            self._publisher = vs_build_publisher(self._vs_cfg)
            n = int(self._vs_cfg.get("n_positions", 10))
            self._tab_sched.set_n_positions(n)
            # Porta VICI: usa /dev/vici se esiste, altrimenti da config
            if os.path.exists("/dev/vici"):
                self._vs_cfg["serial_port"] = "/dev/vici"
            sched_path = self._vs_cfg.get("schedule_file", "schedule/schedule.csv")
            if not Path(sched_path).is_absolute():
                sched_path = self._program_dir / sched_path
            self._tab_sched.load_initial(Path(sched_path))
            self._tab_sched._chk_loop.setChecked(
                bool(self._vs_cfg.get("loop_enabled", True)))
            self._log.info("config valve-scheduler caricata da %s", ini_path)
            # Auto-connect se richiesto dall'INI
            if self._vs_cfg.get("auto_connect"):
                QTimer.singleShot(500, self._connect_valve)
        else:
            self._log.warning("INI valve-scheduler non trovato: %s", ini_path)

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        # Header: connessione valvola
        hdr = QHBoxLayout()
        self._btn_connect = QPushButton("Apri porta VICI")
        self._btn_connect.clicked.connect(self._on_connect_clicked)
        hdr.addWidget(self._btn_connect)
        self._btn_configure = QPushButton("Configura attuatore")
        self._btn_configure.setToolTip(
            "IFM1 + AM3 + SM A + NP (multiposizione)")
        self._btn_configure.clicked.connect(self._on_configure_clicked)
        self._btn_configure.setEnabled(False)
        hdr.addWidget(self._btn_configure)
        self._btn_home = QPushButton("Home")
        self._btn_home.setToolTip("HM — posizione 1")
        self._btn_home.clicked.connect(self._on_home_clicked)
        self._btn_home.setEnabled(False)
        hdr.addWidget(self._btn_home)
        hdr.addStretch()
        self._lbl_valve_status = QLabel("valvola: non connessa")
        self._lbl_valve_status.setStyleSheet(
            "color:#8b8b8b;font-weight:bold;")
        hdr.addWidget(self._lbl_valve_status)
        lay.addLayout(hdr)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        lay.addWidget(line)

        # TabSchedule riusato dal package valve-scheduler
        service_dir = self._program_dir / "service"
        service_dir.mkdir(parents=True, exist_ok=True)
        self._tab_sched = TabSchedule(
            service_dir=service_dir,
            n_positions=10)
        lay.addWidget(self._tab_sched, stretch=1)

        # Wire signals
        self._tab_sched.start_requested.connect(self._on_start)
        self._tab_sched.stop_requested.connect(self._on_stop)
        self._tab_sched.pause_requested.connect(self._on_pause)
        self._tab_sched.resume_requested.connect(self._on_resume)
        self._tab_sched.skip_requested.connect(self._on_skip)
        self._tab_sched.loop_toggled.connect(self._on_loop_toggled)
        self._tab_sched.go_position_requested.connect(self._on_manual_go)

    # ── connessione valvola ──────────────────────────────────────────────
    def _on_connect_clicked(self):
        if self._valve is None:
            self._connect_valve()
        else:
            self._disconnect_valve()

    def _connect_valve(self):
        if not self._vs_cfg:
            QMessageBox.warning(self, "Config mancante",
                                "File valve-scheduler.ini non trovato.")
            return
        try:
            v = VICIEMTCA(
                port=self._vs_cfg["serial_port"],
                baud=int(self._vs_cfg.get("serial_baud", 9600)),
                timeout=float(self._vs_cfg.get("serial_timeout", 2.0)),
                dev_id=self._vs_cfg.get("serial_dev_id", ""),
                rs485=bool(self._vs_cfg.get("serial_rs485", False)))
            v.open()
            try:
                fw = v.ping()
            except VICITimeout:
                v.close()
                QMessageBox.critical(
                    self, "Nessuna risposta",
                    "Porta aperta ma attuatore non risponde.\n"
                    "Controllare cavo e alimentazione.")
                return
            self._valve = v
            self._log.info("VICI connesso — FW: %s", fw)
            if self._vs_cfg.get("configure_on_start"):
                try:
                    v.configure_multiposition(
                        n_positions=int(self._vs_cfg["n_positions"]),
                        go_home=bool(self._vs_cfg.get("go_home_on_start", False)))
                except Exception as exc:
                    self._log.warning("configure_on_start: %s", exc)
            self._btn_connect.setText("Chiudi porta VICI")
            self._btn_configure.setEnabled(True)
            self._btn_home.setEnabled(True)
            self._lbl_valve_status.setText(f"valvola: connessa  (FW: {fw})")
            self._lbl_valve_status.setStyleSheet(
                "color:#1a7f37;font-weight:bold;")
        except Exception as exc:
            QMessageBox.critical(self, "Errore apertura", str(exc))

    def _disconnect_valve(self):
        if self._engine and self._engine.isRunning():
            QMessageBox.warning(self, "Schedule in corso",
                                "Ferma lo schedule prima di chiudere.")
            return
        if self._valve:
            try:
                self._valve.close()
            except Exception:
                pass
            self._valve = None
        self._btn_connect.setText("Apri porta VICI")
        self._btn_configure.setEnabled(False)
        self._btn_home.setEnabled(False)
        self._lbl_valve_status.setText("valvola: non connessa")
        self._lbl_valve_status.setStyleSheet(
            "color:#8b8b8b;font-weight:bold;")

    def _on_configure_clicked(self):
        if not self._valve:
            return
        try:
            self._valve.configure_multiposition(
                n_positions=int(self._vs_cfg["n_positions"]),
                go_home=True)
            QMessageBox.information(
                self, "OK",
                f"Attuatore configurato multipos ({self._vs_cfg['n_positions']} pos).")
        except Exception as exc:
            QMessageBox.warning(self, "Errore", str(exc))

    def _on_home_clicked(self):
        if self._valve:
            try:
                self._valve.home()
            except Exception as exc:
                QMessageBox.warning(self, "Errore", str(exc))

    def _on_manual_go(self, position):
        if not self._valve:
            QMessageBox.warning(self, "Non connesso",
                                "Apri prima la porta VICI.")
            return
        try:
            self._valve.go_to(position)
        except Exception as exc:
            QMessageBox.warning(self, "Errore", str(exc))

    # ── engine ───────────────────────────────────────────────────────────
    def _on_start(self):
        if self._engine and self._engine.isRunning():
            return
        if not self._valve:
            QMessageBox.warning(self, "Non connesso",
                                "Apri prima la porta VICI.")
            return
        schedule = self._tab_sched.get_schedule()
        errs = schedule.validate(int(self._vs_cfg["n_positions"]))
        if errs:
            QMessageBox.warning(
                self, "Schedule non valido",
                "Errori:\n - " + "\n - ".join(errs))
            return
        self._tab_sched._persist()
        status_file = self._vs_cfg.get("status_file",
                                        "service/valve_status.json")
        if not Path(status_file).is_absolute():
            status_file = self._program_dir / status_file
        self._engine = ScheduleEngine(
            valve=self._valve,
            schedule=schedule,
            loop_enabled=self._tab_sched._chk_loop.isChecked(),
            status_file=Path(status_file),
            mqtt_publish=(self._publisher.publish
                          if self._publisher else None),
            mqtt_topic=self._vs_cfg.get("mqtt_topic", "valve/status"),
        )
        self._engine.status_changed.connect(self._tab_sched.on_status)
        self._engine.log_msg.connect(
            lambda msg: self._log.info("[engine] %s", msg))
        self._engine.start()
        self._log.info("schedule avviato — %d step", len(schedule))

    def _on_stop(self):
        if self._engine:
            self._engine.stop()

    def _on_pause(self):
        if self._engine:
            self._engine.pause()

    def _on_resume(self):
        if self._engine:
            self._engine.resume()

    def _on_skip(self):
        if self._engine:
            self._engine.skip_current_step()

    def _on_loop_toggled(self, enabled):
        if self._vs_cfg:
            self._vs_cfg["loop_enabled"] = enabled
        if self._engine and self._engine.isRunning():
            self._engine.set_loop_enabled(enabled)

    # ── cleanup ──────────────────────────────────────────────────────────
    def cleanup(self):
        if self._engine and self._engine.isRunning():
            self._engine.stop()
            self._engine.wait(3000)
        if self._publisher:
            try:
                self._publisher.stop()
            except Exception:
                pass
        if self._valve:
            try:
                self._valve.close()
            except Exception:
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

        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        tabs.addTab(self._build_monitor_tab(), "📊  Monitor")
        tabs.addTab(self._build_graph_tab(),   "📈  Grafico")
        if _HAS_VALVE_SCHEDULER:
            self.tab_valve = TabValve()
            tabs.addTab(self.tab_valve, "🔧  Valvola VICI")
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
        grp_ser = QGroupBox("Seriale")
        h = QHBoxLayout()
        h.addWidget(QLabel("Porta:"))
        self.lbl_port = QLabel("---"); h.addWidget(self.lbl_port)
        h.addWidget(QLabel("Baud:"))
        self.lbl_baud = QLabel(self.cfg.get("serial","baudrate",fallback="19200"))
        h.addWidget(self.lbl_baud)
        self.lbl_dot  = QLabel("●")
        self.lbl_dot.setStyleSheet("font-size:18px;color:#c00")
        h.addWidget(self.lbl_dot)
        self.lbl_stat = QLabel("Disconnesso")
        self.lbl_stat.setStyleSheet("font-weight:bold;color:#c00;font-size:10px")
        h.addWidget(self.lbl_stat)
        h.addStretch(); grp_ser.setLayout(h)
        vbox.addWidget(grp_ser)

        # CO2
        grp_co2 = QGroupBox("Dati Correnti")
        vco2 = QVBoxLayout()
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("CO₂:"))
        sz = self.guicfg.getint("fonts","co2_value_size",fallback=24)
        self.lbl_co2 = QLabel("--- ppm")
        self.lbl_co2.setFont(QFont("Arial", sz, QFont.Bold))
        self.lbl_co2.setStyleSheet("color:#0066cc")
        row1.addWidget(self.lbl_co2); row1.addStretch()
        vco2.addLayout(row1)
        self.lbl_status = QLabel("")
        self.lbl_status.setFont(QFont("Arial", 8))
        vco2.addWidget(self.lbl_status)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("σ:")); self.lbl_std = QLabel("---"); row2.addWidget(self.lbl_std)
        row2.addSpacing(8)
        row2.addWidget(QLabel("n:")); self.lbl_n   = QLabel("---"); row2.addWidget(self.lbl_n)
        row2.addStretch(); vco2.addLayout(row2)

        # T e RH (SHT31-D)
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("T:"))
        self.lbl_t = QLabel("--- °C")
        self.lbl_t.setFont(QFont("Arial", 14, QFont.Bold))
        self.lbl_t.setStyleSheet("color:#c05000")
        row3.addWidget(self.lbl_t)
        row3.addSpacing(16)
        row3.addWidget(QLabel("RH:"))
        self.lbl_rh = QLabel("--- %")
        self.lbl_rh.setFont(QFont("Arial", 14, QFont.Bold))
        self.lbl_rh.setStyleSheet("color:#007060")
        row3.addWidget(self.lbl_rh)
        row3.addStretch()
        vco2.addLayout(row3)

        grp_co2.setLayout(vco2); vbox.addWidget(grp_co2)

        # statistiche
        grp_st = QGroupBox("Statistiche")
        g = QGridLayout(); g.setSpacing(3)
        g.addWidget(QLabel("Min:"),0,0)
        self.lbl_min = QLabel("---"); self.lbl_min.setStyleSheet("color:#090;font-weight:bold;font-size:10px"); g.addWidget(self.lbl_min,0,1)
        g.addWidget(QLabel("Max:"),0,2)
        self.lbl_max = QLabel("---"); self.lbl_max.setStyleSheet("color:#c00;font-weight:bold;font-size:10px"); g.addWidget(self.lbl_max,0,3)
        g.addWidget(QLabel("Media:"),1,0)
        self.lbl_avg = QLabel("---"); self.lbl_avg.setStyleSheet("color:#06c;font-weight:bold;font-size:10px"); g.addWidget(self.lbl_avg,1,1)
        g.addWidget(QLabel("Letture:"),1,2)
        self.lbl_cnt = QLabel("0"); self.lbl_cnt.setStyleSheet("font-size:10px"); g.addWidget(self.lbl_cnt,1,3)
        grp_st.setLayout(g); vbox.addWidget(grp_st)

        # ultima lettura
        grp_last = QGroupBox("Ultima Acquisizione")
        vl = QVBoxLayout(); vl.setSpacing(2)
        self.lbl_ts   = QLabel("---"); self.lbl_ts.setFont(QFont("Arial",9,QFont.Bold)); vl.addWidget(self.lbl_ts)
        # Flag MEASURE / CALIB
        self.lbl_flag = QLabel("---")
        self.lbl_flag.setFont(QFont("Arial", 9, QFont.Bold))
        self.lbl_flag.setAlignment(Qt.AlignLeft)
        vl.addWidget(self.lbl_flag)
        self.lbl_file = QLabel("---")
        self.lbl_file.setStyleSheet("color:#666;font-size:8px")
        self.lbl_file.setWordWrap(True)
        vl.addWidget(self.lbl_file)
        grp_last.setLayout(vl); vbox.addWidget(grp_last)

        # soglie
        grp_thr = QGroupBox("Soglie")
        ht = QHBoxLayout(); ht.setSpacing(5)
        lo = self._thr("low_warning"); hi = self._thr("high_warning")
        ht.addWidget(QLabel("OK:"))
        lb = QLabel(f"{lo:.0f}–{hi:.0f}"); lb.setStyleSheet("color:#06c;font-weight:bold;font-size:9px"); ht.addWidget(lb)
        ht.addWidget(QLabel("Alto:"))
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
        sl = QLabel(f"Sito: {site}")
        sl.setFont(QFont("Arial", self.guicfg.getint("fonts","subtitle_size",fallback=9)))
        vt.addWidget(sl)
        hbox.addLayout(vt); hbox.addStretch()
        return hbox

    # ── tab grafico ───────────────────────────────────────────────────────────

    def _build_graph_tab(self):
        self.graph = GraphWidget(self.cfg)
        return self.graph

    # ── tick timer ────────────────────────────────────────────────────────────

    def _tick(self):
        self._update_monitor()
        self.graph.refresh()

    def _update_monitor(self):
        # Timestamp
        # Seriale — controlla esistenza device (supporta symlink udev come /dev/gmp343)
        port = self.cfg.get("serial","port",fallback="/dev/ttyUSB0")
        if os.path.exists(port):
            self.lbl_dot.setStyleSheet("font-size:18px;color:#090")
            self.lbl_stat.setText("Connesso"); self.lbl_stat.setStyleSheet("font-weight:bold;color:#090;font-size:10px")
            self.lbl_port.setText(port)
        else:
            self.lbl_dot.setStyleSheet("font-size:18px;color:#c00")
            self.lbl_stat.setText("Disconnesso"); self.lbl_stat.setStyleSheet("font-weight:bold;color:#c00;font-size:10px")
            self.lbl_port.setText(f"{port} (N/A)")

        # Ultimo dato
        today = datetime.utcnow().date()
        path  = build_filename(self.cfg, today)   # glob → path reale o ""
        result = load_period(self.cfg, today, 1)

        if result is None:
            self.lbl_co2.setText("--- ppm"); self.lbl_status.setText("")
            self.lbl_std.setText("---"); self.lbl_n.setText("---")
            self.lbl_t.setText("--- °C"); self.lbl_rh.setText("--- %")
            self.lbl_ts.setText("Nessun dato")
            self.lbl_flag.setText("---"); self.lbl_flag.setStyleSheet("color:#888;font-size:9px")
            self.lbl_file.setText("file non trovato" if not path else path)
            self.lbl_min.setText("---"); self.lbl_max.setText("---")
            self.lbl_avg.setText("---"); self.lbl_cnt.setText("0")
            return

        (times, values, stds, counts, flags,
         t_arr, _tstd, rh_arr, _rhstd,
         valve_pos, valve_labels) = result
        last_co2  = float(values[-1])
        last_std  = float(stds[-1])
        last_n    = int(counts[-1])
        last_ts   = times[-1].strftime("%Y/%m/%d %H:%M:%S")
        last_flag = flags[-1] if len(flags) > 0 else "measure"
        last_t    = float(t_arr[-1])
        last_rh   = float(rh_arr[-1])

        # Label flag
        if last_flag == "calib":
            self.lbl_flag.setText("● CALIB")
            self.lbl_flag.setStyleSheet("color:#e06000;font-weight:bold;font-size:10px")
        else:
            self.lbl_flag.setText("● MEASURE")
            self.lbl_flag.setStyleSheet("color:#2060c0;font-weight:bold;font-size:10px")

        # Filtra sentinella per statistiche (sia vecchia 999.99 che nuova -999.99)
        sent = self._thr("sentinel_value")
        valid = values[(np.abs(values - sent) > 0.1) & (values != MISSING)]

        dec = self.guicfg.getint("display","co2_decimals",fallback=2)
        col = self._color(last_co2)
        self.lbl_co2.setText(f"{last_co2:.{dec}f} ppm")
        self.lbl_co2.setStyleSheet(f"color:{col};font-weight:bold")
        self.lbl_std.setText(f"{last_std:.2f} ppm")
        self.lbl_n.setText(str(last_n))
        # T e RH: se MISSING mostra placeholder
        if last_t == MISSING:
            self.lbl_t.setText("--- °C")
        else:
            self.lbl_t.setText(f"{last_t:.2f} °C")
        if last_rh == MISSING:
            self.lbl_rh.setText("--- %")
        else:
            self.lbl_rh.setText(f"{last_rh:.2f} %")
        self.lbl_ts.setText(last_ts)
        self.lbl_file.setText(path)

        # Stato
        hi = self._thr("high_warning"); lo = self._thr("low_warning")
        if last_co2 > hi:
            self.lbl_status.setText(f"⚠ Sopra soglia ({hi:.0f} ppm)")
            self.lbl_status.setStyleSheet("color:#c00;font-weight:bold")
        elif last_co2 < lo:
            self.lbl_status.setText(f"⚠ Sotto soglia ({lo:.0f} ppm)")
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
