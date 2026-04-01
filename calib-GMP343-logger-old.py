#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calib-GMP343-logger.py
Logger GMP343 con GUI per sessioni di calibrazione.

Funzionalità:
  - Acquisizione seriale identica a gmp343_logger-7.py
  - GUI mostra: data/ora UTC, valore CO₂, path file in scrittura
  - Pulsante toggle MEASURE / CALIB aggiunge flag in fondo a ogni riga
  - Default: MEASURE
  - File ini letti da ~/programs/CO2/config/
  - File dati scritti in data_path da name.ini (default ~/data)
  - Nome file: calib-<site>-<YYYYMMDD>.raw
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

# ── Percorsi ──────────────────────────────────────────────────────────────────
CONFIG_DIR = os.path.expanduser("~/programs/CO2/config")
NAME_INI   = os.path.join(CONFIG_DIR, "name.ini")
SERIAL_INI = os.path.join(CONFIG_DIR, "serial.ini")
SITE_INI   = os.path.join(CONFIG_DIR, "site.ini")

CMD_START  = b"R\r\n"

# ── Flag modalità ─────────────────────────────────────────────────────────────
FLAG_MEASURE = "measure"
FLAG_CALIB   = "calib"


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
    """Restituisce (raw_file, min_file) per oggi, stesso schema del logger principale."""
    today     = datetime.utcnow().strftime("%Y%m%d")
    basename  = cfg.get("output", "basename",  fallback="carbocap")
    extension = cfg.get("output", "extension", fallback="raw")
    site      = cfg.get("location", "name",    fallback="unknown")
    data_dir  = get_data_dir(cfg)
    raw_file  = os.path.join(data_dir, f"{basename}-{site}-{today}.{extension}")
    min_file  = os.path.join(data_dir, f"{basename}-{site}-{today}_min.{extension}")
    return raw_file, min_file


def get_filename(cfg) -> str:
    """Restituisce solo il file raw (usato per mostrare path nella GUI)."""
    return get_filenames(cfg)[0]


def write_headers_if_needed(raw_file: str, min_file: str):
    if not os.path.exists(raw_file):
        with open(raw_file, "w") as f:
            f.write("YYYY MM DD hh mm ss.fff CO2(ppm) flag\n")
    if not os.path.exists(min_file):
        with open(min_file, "w") as f:
            f.write("YYYY MM DD hh mm ss CO2(ppm) std n flag\n")


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
        self.cfg     = cfg
        self.signals = signals
        self.flag    = FLAG_MEASURE   # modificato dalla GUI
        self._stop   = threading.Event()

    def set_flag(self, flag: str):
        self.flag = flag

    def stop(self):
        self._stop.set()

    def run(self):
        cfg = self.cfg

        device     = cfg.get("serial", "port",     fallback="/dev/ttyUSB0")
        baudrate   = cfg.getint("serial", "baudrate", fallback=19200)
        bytesize   = cfg.getint("serial", "bytesize", fallback=8)
        parity_str = cfg.get("serial", "parity",   fallback="N")
        stopbits   = cfg.getint("serial", "stopbits", fallback=1)
        timeout    = cfg.getint("serial", "timeout",  fallback=1)

        parity_map = {"N": serial.PARITY_NONE,
                      "E": serial.PARITY_EVEN,
                      "O": serial.PARITY_ODD}
        parity = parity_map.get(parity_str.upper(), serial.PARITY_NONE)

        # Apri porta
        try:
            ser = serial.Serial(
                port=device, baudrate=baudrate, bytesize=bytesize,
                parity=parity, stopbits=stopbits, timeout=timeout
            )
            time.sleep(2)
            ser.write(CMD_START)
        except serial.SerialException as e:
            self.signals.serial_err.emit(str(e))
            return

        # Loop acquisizione
        co2_buf   = []          # campioni del minuto corrente
        flag_buf  = []          # flag dei campioni del minuto corrente
        cur_min   = datetime.utcnow().replace(second=0, microsecond=0)

        raw_file, min_file = get_filenames(cfg)
        write_headers_if_needed(raw_file, min_file)

        while not self._stop.is_set():
            try:
                line = ser.readline().decode(errors="ignore").strip()
                now  = datetime.utcnow()

                # Cambio giorno → nuovi file
                new_raw, new_min = get_filenames(cfg)
                if new_raw != raw_file:
                    raw_file, min_file = new_raw, new_min
                    write_headers_if_needed(raw_file, min_file)

                if not line:
                    # Nessun dato: se il minuto è scaduto scrivi sentinella
                    if now.replace(second=0, microsecond=0) != cur_min:
                        ts_avg = cur_min.strftime("%Y %m %d %H %M %S")
                        # flag del minuto = calib se almeno un campione era calib
                        min_flag = "calib" if "calib" in flag_buf else "measure"
                        with open(min_file, "a") as f:
                            f.write(f"{ts_avg} 999.99 0.00 0 {min_flag}\n")
                        cur_min  = now.replace(second=0, microsecond=0)
                        co2_buf  = []
                        flag_buf = []
                    continue

                ts_str = now.strftime("%Y %m %d %H %M %S.%f")[:-3]
                value  = self._parse(line)
                if value is None:
                    continue

                # Scrivi riga raw con flag
                with open(raw_file, "a") as f:
                    f.write(f"{ts_str} {value:.2f} {self.flag}\n")

                now_min = now.replace(second=0, microsecond=0)
                if now_min == cur_min:
                    co2_buf.append(value)
                    flag_buf.append(self.flag)
                else:
                    # Scrivi media minuto precedente
                    ts_avg   = cur_min.strftime("%Y %m %d %H %M %S")
                    min_flag = "calib" if "calib" in flag_buf else "measure"
                    if co2_buf:
                        avg = sum(co2_buf) / len(co2_buf)
                        std = statistics.stdev(co2_buf) if len(co2_buf) > 1 else 0.0
                        n   = len(co2_buf)
                    else:
                        avg, std, n = 999.99, 0.0, 0
                    with open(min_file, "a") as f:
                        f.write(f"{ts_avg} {avg:.2f} {std:.2f} {n} {min_flag}\n")
                    cur_min  = now_min
                    co2_buf  = [value]
                    flag_buf = [self.flag]

                self.signals.new_value.emit(ts_str, value, raw_file)

            except serial.SerialException as e:
                self.signals.serial_err.emit(str(e))
                ser.close()
                time.sleep(5)
                try:
                    ser.open()
                    ser.write(CMD_START)
                except serial.SerialException:
                    break
            except Exception as e:
                print(f"[acq] {e}")
                time.sleep(1)

    @staticmethod
    def _parse(line: str):
        try:
            for p in line.split():
                if p.replace(".", "", 1).lstrip("-").isdigit():
                    v = float(p)
                    if 0 < v < 10000:   # range plausibile CO₂
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
        self.cfg     = load_config()
        self.signals = AcqSignals()
        self.thread  = AcqThread(self.cfg, self.signals)
        self.mode    = FLAG_MEASURE   # stato corrente

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

    def _on_serial_err(self, msg: str):
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
        self.thread.stop()
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
