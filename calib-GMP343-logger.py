#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calib-GMP343-logger.py
Logger GMP343 con GUI per sessioni di calibrazione.

Formato file v2 (dal 2026):
  - Nome: carbocap343_<site>_<YYYYMMDD>_p00_min.raw (underscore, non trattini)
  - Data/ora: YYYY-MM-DD HH:MM:SS (con trattini e due punti)
  - Header _min: #date time CO2[PPM] CO2_std[PPM] ndata_60s_mean flag
  - Std in PPM assoluto (non percentuale)
  - Flag: measure o calib (modificabile da GUI)
"""

import sys
import os
import threading
import serial
import time
import statistics
import configparser
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QGroupBox
)
from PyQt5.QtCore  import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui   import QFont

# ── Integrazione valve-scheduler (opt-in) ─────────────────────────────────────
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from gmp343_valve_state import format_for_raw as valve_format_for_raw
    _HAS_VALVE_MODULE = True
except ImportError:
    _HAS_VALVE_MODULE = False

# ── Percorsi ──────────────────────────────────────────────────────────────────
CONFIG_DIR      = os.path.expanduser("~/programs/CO2/config")
NAME_INI        = os.path.join(CONFIG_DIR, "name.ini")
SERIAL_INI      = os.path.join(CONFIG_DIR, "serial.ini")
SITE_INI        = os.path.join(CONFIG_DIR, "site.ini")
INTEGRATION_INI = os.path.join(CONFIG_DIR, "integration.ini")  # opzionale

CMD_START  = b"R\r\n"

# ── Flag modalità ─────────────────────────────────────────────────────────────
FLAG_MEASURE = "measure"
FLAG_CALIB   = "calib"

_RECONNECT_DELAY_INIT = 5    # secondi, delay iniziale riconnessione
_RECONNECT_DELAY_MAX  = 60   # secondi, cap backoff esponenziale


# ══════════════════════════════════════════════════════════════════════════════
#  Configurazione
# ══════════════════════════════════════════════════════════════════════════════

def load_config():
    cfg = configparser.ConfigParser()
    cfg.read([NAME_INI, SERIAL_INI, SITE_INI])
    return cfg


def get_data_dir(cfg) -> str:
    raw  = cfg.get("output", "data_path", fallback="~/data")
    path = os.path.expanduser(raw)
    os.makedirs(path, exist_ok=True)
    return path


def get_filenames(cfg) -> tuple:
    """Restituisce (raw_file, min_file) per oggi (underscore nei nomi)."""
    today     = datetime.utcnow().strftime("%Y%m%d")
    basename  = cfg.get("output", "basename",  fallback="carbocap343")
    extension = cfg.get("output", "extension", fallback="raw")
    site      = cfg.get("location", "name",    fallback="unknown")
    data_dir  = get_data_dir(cfg)
    raw_file  = os.path.join(data_dir, f"{basename}_{site}_{today}_p00.{extension}")
    min_file  = os.path.join(data_dir, f"{basename}_{site}_{today}_p00_min.{extension}")
    return raw_file, min_file


def get_filename(cfg) -> str:
    """Restituisce solo il file raw (usato per mostrare path nella GUI)."""
    return get_filenames(cfg)[0]


def load_valve_integration():
    """Carica la config integrazione valve-scheduler (opt-in, retrocompat).

    Returns (enabled: bool, status_file: str, stale_after_s: float).
    Se integration.ini non esiste o il modulo valve_state non è importabile,
    enabled=False (formato .raw invariato).
    """
    if not _HAS_VALVE_MODULE or not os.path.exists(INTEGRATION_INI):
        return (False, "", 10.0)
    cp = configparser.ConfigParser()
    cp.read(INTEGRATION_INI)
    if not cp.has_section("valve_scheduler"):
        return (False, "", 10.0)
    enabled = cp.getboolean("valve_scheduler", "enabled", fallback=False)
    status_file = cp.get("valve_scheduler", "status_file",
                         fallback="~/programs/valve-scheduler/service/valve_status.json")
    stale = cp.getfloat("valve_scheduler", "stale_after_s", fallback=10.0)
    return (enabled, os.path.expanduser(status_file), stale)


def _valve_suffix(enabled, status_file, stale_s):
    """Restituisce ' <pos> <label>' se abilitato, '' altrimenti.
    Tollerante a qualsiasi errore (ritorna ' -1 -')."""
    if not enabled:
        return ""
    try:
        pos_s, lab_s = valve_format_for_raw(status_file, stale_s)
        return f" {pos_s} {lab_s}"
    except Exception:
        return " -1 -"


def write_headers_if_needed(raw_file: str, min_file: str, valve_enabled: bool = False):
    # Usa open('x') per creazione atomica — evita TOCTOU e troncamento (DI-004)
    try:
        with open(raw_file, "x", encoding="utf-8") as f:
            f.write("#date time CO2[PPM] flag\n")
    except FileExistsError:
        pass
    try:
        with open(min_file, "x", encoding="utf-8") as f:
            if valve_enabled:
                f.write("#date time CO2[PPM] CO2_std[PPM] ndata_60s_mean flag valve_pos valve_label\n")
            else:
                f.write("#date time CO2[PPM] CO2_std[PPM] ndata_60s_mean flag\n")
    except FileExistsError:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  Segnali Qt per comunicazione thread → GUI
# ══════════════════════════════════════════════════════════════════════════════

class AcqSignals(QObject):
    new_value  = pyqtSignal(str, float, str)   # timestamp_str, co2, filepath
    serial_err = pyqtSignal(str)               # messaggio errore


# ══════════════════════════════════════════════════════════════════════════════
#  Thread di acquisizione
# ══════════════════════════════════════════════════════════════════════════════

class AcqThread(threading.Thread):
    """
    Legge dalla porta seriale in background.
    Scrive ogni campione su file con flag corrente.
    Emette segnale Qt per aggiornare la GUI.
    """

    def __init__(self, cfg, signals: AcqSignals):
        super().__init__(daemon=True)
        self.cfg        = cfg
        self.signals    = signals
        self._flag      = FLAG_MEASURE
        self._flag_lock = threading.Lock()   # ARCH-001: protegge self._flag
        self._stop      = threading.Event()
        # Integrazione valve-scheduler (opt-in): letta una volta all'avvio
        (self._valve_enabled,
         self._valve_status_file,
         self._valve_stale_s) = load_valve_integration()
        if self._valve_enabled:
            print(f"[integration] valve-scheduler ATTIVA — "
                  f"status_file={self._valve_status_file}")

    def set_flag(self, flag: str):
        with self._flag_lock:
            self._flag = flag

    def _get_flag(self) -> str:
        with self._flag_lock:
            return self._flag

    def stop(self):
        self._stop.set()

    def run(self):
        cfg = self.cfg

        device     = cfg.get("serial",   "port",     fallback="/dev/ttyUSB0")
        baudrate   = cfg.getint("serial", "baudrate", fallback=19200)
        bytesize   = cfg.getint("serial", "bytesize", fallback=8)
        parity_str = cfg.get("serial",   "parity",   fallback="N")
        stopbits   = cfg.getint("serial", "stopbits", fallback=1)
        timeout    = cfg.getfloat("serial", "timeout", fallback=1.0)  # float sub-secondo (SER-009)

        parity_map = {"N": serial.PARITY_NONE,
                      "E": serial.PARITY_EVEN,
                      "O": serial.PARITY_ODD}
        parity = parity_map.get(parity_str.upper(), serial.PARITY_NONE)

        # Apertura iniziale con retry (SER-006: non terminare al primo fallimento)
        ser = None
        reconnect_delay = _RECONNECT_DELAY_INIT
        while not self._stop.is_set():
            try:
                ser = serial.Serial(
                    port=device, baudrate=baudrate, bytesize=bytesize,
                    parity=parity, stopbits=stopbits, timeout=timeout
                )
                time.sleep(2)
                ser.write(CMD_START)
                reconnect_delay = _RECONNECT_DELAY_INIT
                break
            except serial.SerialException as e:
                self.signals.serial_err.emit(str(e))
                if self._stop.is_set():
                    return
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, _RECONNECT_DELAY_MAX)

        if ser is None or self._stop.is_set():
            return

        co2_buf  = []
        flag_buf = []
        cur_min  = datetime.utcnow().replace(second=0, microsecond=0)

        raw_file, min_file = get_filenames(cfg)
        write_headers_if_needed(raw_file, min_file, self._valve_enabled)

        try:  # finally garantisce ser.close() in ogni caso (ARCH-002)
            while not self._stop.is_set():

                # ── Lettura seriale — try/except isolato (DI-002) ─────────────
                try:
                    line = ser.readline().decode(errors="ignore").strip()
                except serial.SerialException as e:
                    self.signals.serial_err.emit(str(e))
                    try:
                        ser.close()
                    except Exception:
                        pass
                    # Backoff esponenziale (SER-003)
                    reconnect_delay = _RECONNECT_DELAY_INIT
                    while not self._stop.is_set():
                        time.sleep(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 2, _RECONNECT_DELAY_MAX)
                        try:
                            ser.open()
                            ser.reset_input_buffer()   # svuota stale buffer (SER-002)
                            ser.reset_output_buffer()
                            ser.write(CMD_START)
                            reconnect_delay = _RECONNECT_DELAY_INIT
                            break
                        except serial.SerialException:
                            pass
                    continue

                now = datetime.utcnow()

                # ── Cambio giorno — try/except isolato ────────────────────────
                try:
                    new_raw, new_min = get_filenames(cfg)
                    if new_raw != raw_file or new_min != min_file:  # DI-008: controlla entrambi
                        # Scrivi l'ultimo minuto nel VECCHIO file prima del cambio (DI-005)
                        if co2_buf:
                            ts_avg   = cur_min.strftime("%Y-%m-%d %H:%M:%S")
                            min_flag = FLAG_CALIB if FLAG_CALIB in flag_buf else self._get_flag()
                            avg      = sum(co2_buf) / len(co2_buf)
                            std      = statistics.stdev(co2_buf) if len(co2_buf) > 1 else 0.0
                            n        = len(co2_buf)
                            vsuf = _valve_suffix(self._valve_enabled,
                                                 self._valve_status_file,
                                                 self._valve_stale_s)
                            with open(min_file, "a", encoding="utf-8") as mf:
                                mf.write(f"{ts_avg} {avg:.2f} {std:.2f} {n} {min_flag}{vsuf}\n")
                                mf.flush()  # DI-003
                            co2_buf  = []
                            flag_buf = []
                            cur_min  = now.replace(second=0, microsecond=0)
                        raw_file, min_file = new_raw, new_min
                        write_headers_if_needed(raw_file, min_file, self._valve_enabled)
                except OSError as e:
                    print(f"[acq] cambio giorno: {e}")

                if not line:
                    # Timeout readline: se il minuto è scaduto scrivi sentinella
                    if now.replace(second=0, microsecond=0) != cur_min:
                        try:
                            ts_avg   = cur_min.strftime("%Y-%m-%d %H:%M:%S")
                            # Usa self._get_flag() come fallback se flag_buf vuoto (DI-006/CORR-003)
                            min_flag = FLAG_CALIB if FLAG_CALIB in flag_buf else self._get_flag()
                            vsuf = _valve_suffix(self._valve_enabled,
                                                 self._valve_status_file,
                                                 self._valve_stale_s)
                            with open(min_file, "a", encoding="utf-8") as mf:
                                mf.write(f"{ts_avg} 999.99 0.00 0 {min_flag}{vsuf}\n")
                                mf.flush()  # DI-003
                        except OSError as e:
                            print(f"[acq] write sentinel: {e}")
                        cur_min  = now.replace(second=0, microsecond=0)
                        co2_buf  = []
                        flag_buf = []
                    continue

                ts_str       = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                value        = self._parse(line)
                if value is None:
                    continue

                current_flag = self._get_flag()  # lettura atomica (ARCH-001)

                # ── Scrittura raw — try/except isolato (DI-002) ───────────────
                try:
                    with open(raw_file, "a", encoding="utf-8") as rf:
                        rf.write(f"{ts_str} {value:.2f} {current_flag}\n")
                        rf.flush()  # DI-003
                except OSError as e:
                    print(f"[acq] write raw: {e}")

                now_min = now.replace(second=0, microsecond=0)
                if now_min == cur_min:
                    co2_buf.append(value)
                    flag_buf.append(current_flag)
                else:
                    # ── Scrittura media minuto — try/except isolato (DI-002) ──
                    try:
                        ts_avg   = cur_min.strftime("%Y-%m-%d %H:%M:%S")
                        # Usa self._get_flag() come fallback se flag_buf vuoto (DI-006/CORR-003)
                        min_flag = FLAG_CALIB if FLAG_CALIB in flag_buf else self._get_flag()
                        if co2_buf:
                            avg = sum(co2_buf) / len(co2_buf)
                            std = statistics.stdev(co2_buf) if len(co2_buf) > 1 else 0.0
                            n   = len(co2_buf)
                        else:
                            avg, std, n = 999.99, 0.0, 0
                        vsuf = _valve_suffix(self._valve_enabled,
                                             self._valve_status_file,
                                             self._valve_stale_s)
                        with open(min_file, "a", encoding="utf-8") as mf:
                            mf.write(f"{ts_avg} {avg:.2f} {std:.2f} {n} {min_flag}{vsuf}\n")
                            mf.flush()  # DI-003
                    except OSError as e:
                        print(f"[acq] write min: {e}")
                    cur_min  = now_min
                    co2_buf  = [value]
                    flag_buf = [current_flag]

                # ── Segnale GUI — try/except isolato (DI-002) ─────────────────
                try:
                    self.signals.new_value.emit(ts_str, value, raw_file)
                except Exception as e:
                    print(f"[acq] signal: {e}")

        finally:
            try:
                ser.close()  # ARCH-002: porta sempre chiusa all'uscita
            except Exception:
                pass

    @staticmethod
    def _parse(line: str):
        try:
            for p in line.split():
                if p.replace(".", "", 1).lstrip("-").isdigit():
                    v = float(p)
                    if -999 < v < 10000:   # range plausibile CO₂ (negativo ammesso in calibrazione)
                        return v
        except Exception:
            pass
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  Finestra principale
# ══════════════════════════════════════════════════════════════════════════════

class CalibLogger(QMainWindow):

    def __init__(self):
        super().__init__()
        self.cfg           = load_config()
        self.signals       = AcqSignals()
        self.thread        = AcqThread(self.cfg, self.signals)
        self.mode          = FLAG_MEASURE
        self._serial_error = False  # traccia stato errore per ripristino colore

        self._build_ui()
        self._connect_signals()

        self.thread.start()

        # Clock UI aggiornato ogni secondo
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self._update_clock)
        self.clock_timer.start(1000)

    # ── costruzione UI ────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setWindowTitle("GMP343 Calibration Logger")
        self.setFixedSize(520, 340)

        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setSpacing(10)
        vbox.setContentsMargins(16, 12, 16, 12)

        # ── Data / Ora ────────────────────────────────────────────────────────
        grp_time = QGroupBox("Data / Ora (UTC)")
        ht = QHBoxLayout()
        self.lbl_datetime = QLabel("----/--/-- --:--:--")
        self.lbl_datetime.setFont(QFont("Courier", 16, QFont.Bold))
        self.lbl_datetime.setAlignment(Qt.AlignCenter)
        ht.addWidget(self.lbl_datetime)
        grp_time.setLayout(ht)
        vbox.addWidget(grp_time)

        # ── Valore CO₂ ────────────────────────────────────────────────────────
        grp_co2 = QGroupBox("CO₂")
        hc = QHBoxLayout()
        self.lbl_co2 = QLabel("--- ppm")
        self.lbl_co2.setFont(QFont("Arial", 28, QFont.Bold))
        self.lbl_co2.setAlignment(Qt.AlignCenter)
        self.lbl_co2.setStyleSheet("color: #0055aa;")
        hc.addWidget(self.lbl_co2)
        grp_co2.setLayout(hc)
        vbox.addWidget(grp_co2)

        # ── File in scrittura ─────────────────────────────────────────────────
        grp_file = QGroupBox("File in scrittura")
        hf = QHBoxLayout()
        self.lbl_file = QLabel(get_filename(self.cfg))
        self.lbl_file.setFont(QFont("Courier", 8))
        self.lbl_file.setStyleSheet("color: #444;")
        self.lbl_file.setWordWrap(True)
        hf.addWidget(self.lbl_file)
        grp_file.setLayout(hf)
        vbox.addWidget(grp_file)

        # ── Pulsante MEASURE / CALIB ──────────────────────────────────────────
        self.btn_mode = QPushButton()
        self.btn_mode.setFixedHeight(52)
        self.btn_mode.setFont(QFont("Arial", 14, QFont.Bold))
        self.btn_mode.clicked.connect(self._toggle_mode)
        self._apply_mode_style()
        vbox.addWidget(self.btn_mode)

        # ── Stato seriale ─────────────────────────────────────────────────────
        self.lbl_serial = QLabel(
            f"Porta: {self.cfg.get('serial','port',fallback='/dev/ttyUSB0')}"
            f"  @  {self.cfg.get('serial','baudrate',fallback='19200')} bps"
        )
        self.lbl_serial.setFont(QFont("Arial", 8))
        self.lbl_serial.setStyleSheet("color: #666;")
        self.lbl_serial.setAlignment(Qt.AlignCenter)
        vbox.addWidget(self.lbl_serial)

    # ── segnali ───────────────────────────────────────────────────────────────

    def _connect_signals(self):
        self.signals.new_value.connect(self._on_new_value)
        self.signals.serial_err.connect(self._on_serial_err)

    # ── slot ──────────────────────────────────────────────────────────────────

    def _on_new_value(self, ts_str: str, value: float, filepath: str):
        """Ricevuto dal thread di acquisizione."""
        self.lbl_co2.setText(f"{value:.2f} ppm")
        self.lbl_file.setText(filepath)
        # Ripristina colore normale dopo recovery errore seriale (CORR-004)
        if self._serial_error:
            self._serial_error = False
            self.lbl_co2.setStyleSheet("color: #0055aa;")
            self.lbl_serial.setText(
                f"Porta: {self.cfg.get('serial','port',fallback='/dev/ttyUSB0')}"
                f"  @  {self.cfg.get('serial','baudrate',fallback='19200')} bps"
            )
            self.lbl_serial.setStyleSheet("color: #666;")

    def _on_serial_err(self, msg: str):
        self._serial_error = True
        self.lbl_co2.setText("ERRORE")
        self.lbl_co2.setStyleSheet("color: #cc0000;")
        self.lbl_serial.setText(f"⚠ Errore seriale: {msg}")
        self.lbl_serial.setStyleSheet("color: #cc0000; font-weight: bold;")

    def _update_clock(self):
        now = datetime.utcnow().strftime("%Y/%m/%d  %H:%M:%S")
        self.lbl_datetime.setText(now)

    def _toggle_mode(self):
        if self.mode == FLAG_MEASURE:
            self.mode = FLAG_CALIB
        else:
            self.mode = FLAG_MEASURE
        self.thread.set_flag(self.mode)
        self._apply_mode_style()

    def _apply_mode_style(self):
        if self.mode == FLAG_MEASURE:
            self.btn_mode.setText("● MEASURE")
            self.btn_mode.setStyleSheet(
                "QPushButton {"
                "  background-color: #28a745;"
                "  color: white;"
                "  border-radius: 8px;"
                "  border: 2px solid #1e7e34;"
                "}"
                "QPushButton:hover { background-color: #218838; }"
            )
        else:
            self.btn_mode.setText("● CALIB")
            self.btn_mode.setStyleSheet(
                "QPushButton {"
                "  background-color: #fd7e14;"
                "  color: white;"
                "  border-radius: 8px;"
                "  border: 2px solid #e96b02;"
                "}"
                "QPushButton:hover { background-color: #e96b02; }"
            )

    # ── chiusura ──────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self.clock_timer.stop()        # GUI-003: ferma timer prima della distruzione widget
        self.thread.stop()
        self.thread.join(timeout=3)    # DI-001/ARCH-003: attende scrittura ultimo campione
        event.accept()


# ══════════════════════════════════════════════════════════════════════════════
#  Avvio
# ══════════════════════════════════════════════════════════════════════════════

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = CalibLogger()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
