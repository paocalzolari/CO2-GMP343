#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GMP343 Monitor Integrato v12
Tab 1 : Monitor real-time  (mostra flag corrente dal file)
Tab 2 : Grafico CO₂        (punti calib in arancione, label flag in alto dx)

Fix rispetto a v11:
  - read_file legge colonna flag opzionale (col 9) dal file dati
  - load_period restituisce anche array flags
  - Grafico: punti measure in blu, punti calib in arancione (#e06000)
  - Label variabile "MEASURE" / "CALIB" in alto a destra nel grafico
  - Tab monitor: mostra stato flag dell'ultima acquisizione
"""

import sys, os
from datetime import datetime, timedelta, timezone, date as date_type
import configparser

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QGridLayout, QTabWidget, QPushButton,
    QComboBox, QDateEdit, QCheckBox
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
    Trova il file _min per la data d usando glob.
    Non dipende dal nome stazione: cerca *-YYYYMMDD_min.ext
    Se trova più file restituisce quello modificato più di recente.
    Restituisce stringa vuota se nessun file trovato.
    """
    import glob
    ext  = cfg.get("output", "extension", fallback="raw")
    ddir = get_data_dir(cfg)
    pattern = os.path.join(ddir, f"*-{d.strftime('%Y%m%d')}_min.{ext}")
    matches = glob.glob(pattern)
    if not matches:
        return ""
    # Più file → prende il più recente per data di modifica
    return max(matches, key=os.path.getmtime)


def _startup_log():
    """Stampa percorsi risolti all'avvio per diagnostica."""
    print("=" * 60)
    print("GMP343 Monitor v10 — percorsi risolti")
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
        print(f"  File oggi    : {ddir}/*-{today.strftime('%Y%m%d')}_min.{ext}  [✗ NON TROVATO]")
    print("=" * 60)
    print()


def read_file(path: str):
    """
    Legge un file dati.
    Formato atteso per righe _min:
      YYYY MM DD HH MM SS CO2 std n [flag]
    Formato raw con flag (da calib-logger):
      YYYY MM DD HH MM SS.fff CO2 [flag]
    Restituisce liste (times, values, stds, counts, flags).
    flag è "measure" o "calib"; se assente → "measure".
    """
    times, values, stds, counts, flags = [], [], [], [], []
    if not path or not os.path.exists(path):
        return times, values, stds, counts, flags
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                if raw.startswith("YYYY"):
                    continue
                p = raw.split()
                if len(p) < 7:
                    continue
                try:
                    dt  = datetime.strptime(" ".join(p[:6]), "%Y %m %d %H %M %S")
                    co2 = float(p[6])
                    # std e n opzionali (file _min ne hanno, file raw no)
                    if len(p) >= 9:
                        try:
                            std = float(p[7])
                            n   = int(p[8])
                            flag = p[9].lower() if len(p) >= 10 else "measure"
                        except ValueError:
                            std  = 0.0
                            n    = 1
                            flag = p[7].lower() if len(p) >= 8 else "measure"
                    elif len(p) == 8:
                        std  = 0.0
                        n    = 1
                        flag = p[7].lower()
                    else:
                        std  = 0.0
                        n    = 1
                        flag = "measure"
                    if flag not in ("measure", "calib"):
                        flag = "measure"
                    times.append(dt)
                    values.append(co2)
                    stds.append(std)
                    counts.append(n)
                    flags.append(flag)
                except ValueError:
                    continue
    except OSError:
        pass
    return times, values, stds, counts, flags


def load_period(cfg: configparser.ConfigParser,
                start: date_type, n_days: int):
    """Carica n_days giorni a partire da start; restituisce array numpy ordinati."""
    all_t, all_v, all_s, all_c, all_f = [], [], [], [], []
    for i in range(n_days):
        d = start + timedelta(days=i)
        t, v, s, c, f = read_file(build_filename(cfg, d))
        all_t.extend(t)
        all_v.extend(v)
        all_s.extend(s)
        all_c.extend(c)
        all_f.extend(f)
    if not all_t:
        return None
    idx    = np.argsort(all_t)
    times  = np.array(all_t)[idx]
    values = np.array(all_v, dtype=float)[idx]
    stds   = np.array(all_s, dtype=float)[idx]
    counts = np.array(all_c, dtype=int)[idx]
    flags  = np.array(all_f)[idx]
    return times, values, stds, counts, flags


def day_xlim(d: date_type):
    """Restituisce (x0, x1) in numdate per la giornata d (00:00–24:00)."""
    x0 = mdates.date2num(datetime.combine(d, datetime.min.time()))
    x1 = x0 + 1.0
    return x0, x1


def smart_ylim(values):
    """Calcola ylim con margine."""
    lo, hi = float(np.nanmin(values)), float(np.nanmax(values))
    span = hi - lo
    if span < MIN_Y_RANGE:
        mid = (lo + hi) / 2
        lo, hi = mid - MIN_Y_RANGE / 2, mid + MIN_Y_RANGE / 2
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
        self.ax = self.fig.add_subplot(111)

        # Linea continua (tutti i punti, senza marker)
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
        self.ax.set_xlabel("Ora (UTC)", fontsize=10)
        self.ax.grid(True, linestyle="--", linewidth=0.3, alpha=0.6)

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

        if result is None:
            self.line.set_data([], [])
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

        times, values, stds, counts, flags = result
        xt = mdates.date2num(times)

        # ── Linea continua (tutti i punti) ────────────────────────────────
        self.line.set_data(xt, values)

        # ── Scatter per flag ──────────────────────────────────────────────
        mask_m = (flags == "measure")
        mask_c = (flags == "calib")

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

        # ── Zone notturne ─────────────────────────────────────────────────
        want_night = ASTRAL_OK and hasattr(self, "chk_night") and self.chk_night.isChecked()
        if want_night:
            days_list = [start + timedelta(days=i) for i in range(n_days)]
            x0_day    = mdates.date2num(datetime.combine(start, datetime.min.time()))
            x1_day    = x0_day + n_days
            for dawn_n, dusk_n in night_spans(self.cfg, days_list):
                # notte pre-alba
                p1 = self.ax.axvspan(x0_day, dawn_n, color="steelblue", alpha=0.10, zorder=0)
                # notte post-tramonto
                p2 = self.ax.axvspan(dusk_n, x1_day, color="steelblue", alpha=0.10, zorder=0)
                self._night_poly.extend([p1, p2])
                x0_day = x1_day  # next day (non usato nel loop, ma coerente)

        # ── Asse X fisso (intera giornata / periodo) ───────────────────────
        self._set_x_axis(start, n_days)

        # ── Asse Y ────────────────────────────────────────────────────────
        if self._zoom_ylim is None:
            lo, hi = smart_ylim(values)
            self.ax.set_ylim(lo, hi)
        else:
            self.ax.set_ylim(self._zoom_ylim)

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

        self.ax.set_xlabel(
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

        # Testo tooltip
        t_str = mdates.num2date(xv).strftime("%H:%M:%S")
        self.annot.set_text(f"Ora:  {t_str}\nCO₂: {yv:.2f} ppm")
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


# ══════════════════════════════════════════════════════════════════════════════
#  Finestra principale
# ══════════════════════════════════════════════════════════════════════════════

class GMP343Monitor(QMainWindow):

    def __init__(self):
        super().__init__()
        self.cfg     = self._load_config()
        self.guicfg  = self._load_gui_config()
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
        if abs(value - s) < 0.1:
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
        # Seriale
        port = self.cfg.get("serial","port",fallback="/dev/ttyUSB0")
        avail = [p.device for p in serial.tools.list_ports.comports()]
        if port in avail:
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
            self.lbl_ts.setText("Nessun dato")
            self.lbl_flag.setText("---"); self.lbl_flag.setStyleSheet("color:#888;font-size:9px")
            self.lbl_file.setText("file non trovato" if not path else path)
            self.lbl_min.setText("---"); self.lbl_max.setText("---")
            self.lbl_avg.setText("---"); self.lbl_cnt.setText("0")
            return

        times, values, stds, counts, flags = result
        last_co2  = float(values[-1])
        last_std  = float(stds[-1])
        last_n    = int(counts[-1])
        last_ts   = times[-1].strftime("%Y/%m/%d %H:%M:%S")
        last_flag = flags[-1] if len(flags) > 0 else "measure"

        # Label flag
        if last_flag == "calib":
            self.lbl_flag.setText("● CALIB")
            self.lbl_flag.setStyleSheet("color:#e06000;font-weight:bold;font-size:10px")
        else:
            self.lbl_flag.setText("● MEASURE")
            self.lbl_flag.setStyleSheet("color:#2060c0;font-weight:bold;font-size:10px")

        # Filtra sentinella per statistiche
        sent = self._thr("sentinel_value")
        valid = values[np.abs(values - sent) > 0.1]

        dec = self.guicfg.getint("display","co2_decimals",fallback=2)
        col = self._color(last_co2)
        self.lbl_co2.setText(f"{last_co2:.{dec}f} ppm")
        self.lbl_co2.setStyleSheet(f"color:{col};font-weight:bold")
        self.lbl_std.setText(f"{last_std:.2f} ppm")
        self.lbl_n.setText(str(last_n))
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

def main():
    _startup_log()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = GMP343Monitor()
    win.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
