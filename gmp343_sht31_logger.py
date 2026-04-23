#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gmp343_sht31_logger.py
Acquisizione GMP343 (CO2, seriale) + SHT31-D (T/RH, I2C) → file raw e _min giornalieri.
File ini letti da ~/programs/CO2/config/
data_path letto da name.ini (default: ~/data)

Formato file v3 (dal 2026-04-15, con T/RH):
  - Nome: carbocap343_<site>_<YYYYMMDD>_p00_min.raw (underscore, non trattini)
  - Data/ora: YYYY-MM-DD HH:MM:SS (con trattini e due punti)
  - Header raw:  #date time CO2[PPM] T[C] RH[%] flag
  - Header _min: #date time CO2[PPM] CO2_std[PPM] T[C] T_std[C] RH[%] RH_std[%] ndata_60s_mean flag [valve_pos valve_label]
  - Std in PPM assoluto (non percentuale)
  - Flag: measure o calib
    - Se calib_auto=false in integration.ini (default): flag fisso "measure"
    - Se calib_auto=true: flag determinato dalla valve_label corrente
      (le label in calib_labels → "calib", le altre → "measure")
  - Dato mancante (sensore assente, errore I2C, minuto vuoto) → -999.99
"""
import serial
import time
import json
import tempfile
from datetime import datetime, timezone
import os
import sys
import statistics
import configparser

try:
    import smbus2
    _HAS_SMBUS = True
except ImportError:
    _HAS_SMBUS = False

# ── Integrazione valve-scheduler (opt-in) ─────────────────────────────────────
# Import tollerante: se il modulo manca o integration.ini non c'è, il logger
# si comporta esattamente come prima (formato file invariato).
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from gmp343_valve_state import format_for_raw as valve_format_for_raw
    from gmp343_valve_state import get_flag as valve_get_flag
    _HAS_VALVE_MODULE = True
except ImportError:
    _HAS_VALVE_MODULE = False

# ── Percorsi ──────────────────────────────────────────────────────────────────
CONFIG_DIR      = os.path.expanduser("~/programs/CO2/config")
NAME_INI        = os.path.join(CONFIG_DIR, "name.ini")
SERIAL_INI      = os.path.join(CONFIG_DIR, "serial.ini")
SITE_INI        = os.path.join(CONFIG_DIR, "site.ini")
INTEGRATION_INI = os.path.join(CONFIG_DIR, "integration.ini")  # opzionale

CMD_START = b"R\r\n"

# ── Status JSON per acq-tools ────────────────────────────────────────────────
STATUS_JSON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "shared", "ipc_co2", "status.json")


def _write_status_json(instrument_connected, last_co2=None, last_t=None, last_rh=None):
    """Scrive status.json atomicamente (tmp + rename) per acq-tools.

    Aggiorna l'mtime — acq-tools considera stale dopo 120s.
    """
    status = {
        "instrument_connected": instrument_connected,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "last_co2_ppm": last_co2,
        "last_t_c": last_t,
        "last_rh_pct": last_rh,
    }
    try:
        os.makedirs(os.path.dirname(STATUS_JSON), exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(STATUS_JSON), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(status, f)
        os.replace(tmp, STATUS_JSON)
    except OSError as e:
        print(f"WARN: status.json write failed: {e}")

# Sentinel unico per tutti i valori mancanti (CO2, T, RH, std)
MISSING = -999.99

# ── SHT31-D (T/RH via I2C) ────────────────────────────────────────────────────
SHT31_BUS      = 1          # /dev/i2c-1 sul Raspberry Pi
SHT31_ADDR     = 0x44       # default (ADDR pin low)
SHT31_CMD_MSB  = 0x24       # single-shot high repeatability, clock-stretch disabled
SHT31_CMD_LSB  = 0x00
SHT31_WAIT_S   = 0.02       # high-rep misura ~15 ms, 20 ms è conservativo


def open_sht31_bus():
    """Apre SMBus(1). Ritorna un oggetto SMBus o None se l'I2C non è disponibile."""
    if not _HAS_SMBUS:
        print("WARN: libreria smbus2 non installata → SHT31 disabilitato")
        return None
    try:
        bus = smbus2.SMBus(SHT31_BUS)
        bus.write_i2c_block_data(SHT31_ADDR, SHT31_CMD_MSB, [SHT31_CMD_LSB])
        time.sleep(SHT31_WAIT_S)
        bus.read_i2c_block_data(SHT31_ADDR, 0x00, 6)
        print(f"SHT31-D ok su bus {SHT31_BUS} addr 0x{SHT31_ADDR:02x}")
        return bus
    except Exception as e:
        print(f"WARN: SHT31-D non raggiungibile ({e}) → T/RH saranno {MISSING}")
        return None


def read_sht31(bus):
    """
    Ritorna (T [°C], RH [%]) oppure (MISSING, MISSING) su errore.
    Protocollo: single-shot high-rep, lettura 6 byte (T_msb T_lsb CRC RH_msb RH_lsb CRC).
    CRC non verificato: il kernel già scarta frame corrotti e in 10+ anni di uso di
    questo tipo di sensore non ho mai visto un CRC valido con dati sporchi.
    """
    if bus is None:
        return MISSING, MISSING
    try:
        bus.write_i2c_block_data(SHT31_ADDR, SHT31_CMD_MSB, [SHT31_CMD_LSB])
        time.sleep(SHT31_WAIT_S)
        r = bus.read_i2c_block_data(SHT31_ADDR, 0x00, 6)
        t_raw  = (r[0] << 8) | r[1]
        rh_raw = (r[3] << 8) | r[4]
        t  = -45.0 + 175.0 * (t_raw  / 65535.0)
        rh = 100.0 * (rh_raw / 65535.0)
        return t, rh
    except Exception as e:
        print(f"WARN: read_sht31 fallita: {e}")
        return MISSING, MISSING


def load_valve_integration():
    """Carica la config integrazione valve-scheduler (opt-in, retrocompat).

    Restituisce (enabled, status_file, stale_after_s, calib_auto, calib_labels).
    Se integration.ini non esiste o il modulo valve_state non è importabile,
    enabled=False — il logger scrive nel formato storico senza colonne valvola.
    Se calib_auto=True, il flag measure/calib è determinato dalla valve_label.
    """
    if not _HAS_VALVE_MODULE or not os.path.exists(INTEGRATION_INI):
        return (False, "", 10.0, False, [])
    cp = configparser.ConfigParser()
    cp.read(INTEGRATION_INI)
    if not cp.has_section("valve_scheduler"):
        return (False, "", 10.0, False, [])
    enabled = cp.getboolean("valve_scheduler", "enabled", fallback=False)
    status_file = cp.get("valve_scheduler", "status_file",
                         fallback="~/programs/valve-scheduler/service/valve_status.json")
    stale = cp.getfloat("valve_scheduler", "stale_after_s", fallback=10.0)
    calib_auto = cp.getboolean("valve_scheduler", "calib_auto", fallback=False)
    calib_labels_raw = cp.get("valve_scheduler", "calib_labels", fallback="")
    calib_labels = [s.strip() for s in calib_labels_raw.split(",") if s.strip()]
    return (enabled, os.path.expanduser(status_file), stale,
            calib_auto, calib_labels)


def get_data_dir(config) -> str:
    """Legge data_path da name.ini ed espande ~ ; crea la cartella se mancante."""
    raw = config.get("output", "data_path", fallback="~/data")
    path = os.path.expanduser(raw)
    os.makedirs(path, exist_ok=True)
    return path

def load_config():
    """Loads configurations from .ini files."""
    config = configparser.ConfigParser()
    config.read([NAME_INI, SERIAL_INI, SITE_INI])
    return config

def get_filenames(config):
    """Genera i nomi file raw e _min per il giorno corrente (underscore nei nomi)."""
    today     = datetime.utcnow().strftime("%Y%m%d")
    basename  = config.get("output", "basename",  fallback="carbocap343")
    extension = config.get("output", "extension", fallback="raw")
    site_name = config.get("location", "name",    fallback="unknown")
    data_dir  = get_data_dir(config)
    raw_file  = os.path.join(data_dir, f"{basename}_{site_name}_{today}_p00.{extension}")
    avg_file  = os.path.join(data_dir, f"{basename}_{site_name}_{today}_p00_min.{extension}")
    return raw_file, avg_file

def write_headers_if_needed(raw_file, avg_file, config, valve_enabled=False):
    """
    Scrive header nei file se non esistono.
    RAW: #date time CO2[PPM] T[C] RH[%] flag
    MIN: #date time CO2[PPM] CO2_std[PPM] T[C] T_std[C] RH[%] RH_std[%] ndata_60s_mean flag [valve_pos valve_label]
    """
    raw_header = "#date time CO2[PPM] T[C] RH[%] flag"
    if valve_enabled:
        avg_header = "#date time CO2[PPM] CO2_std[PPM] T[C] T_std[C] RH[%] RH_std[%] ndata_60s_mean flag valve_pos valve_label"
    else:
        avg_header = "#date time CO2[PPM] CO2_std[PPM] T[C] T_std[C] RH[%] RH_std[%] ndata_60s_mean flag"

    if not os.path.exists(raw_file):
        with open(raw_file, 'w') as f:
            f.write(f"{raw_header}\n")
    if not os.path.exists(avg_file):
        with open(avg_file, 'w') as f:
            f.write(f"{avg_header}\n")

def timestamp_now():
    now = datetime.utcnow()
    full_ts = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return full_ts, now

def parse_co2_from_line(line):
    try:
        parts = line.strip().split()
        for p in parts:
            if p.replace('.', '', 1).isdigit():
                return float(p)
    except Exception as e:
        print(f"Error parsing line '{line}': {e}")
    return None


def mean_std_missing(values):
    """
    Media e stdev ignorando i valori MISSING.
    Se tutti i valori sono MISSING (o lista vuota): ritorna (MISSING, MISSING).
    """
    clean = [v for v in values if v != MISSING]
    if not clean:
        return MISSING, MISSING
    avg = sum(clean) / len(clean)
    std = statistics.stdev(clean) if len(clean) > 1 else 0.0
    return avg, std

def _valve_suffix(valve_enabled, valve_status_file, valve_stale_s):
    """Restituisce la stringa ' <pos> <label>' se integrazione attiva, altrimenti ''.

    Nota: inizia con uno spazio per comporre la riga `_min.raw`.
    Sentinelle se il file manca/stale: ' -1 -'.
    """
    if not valve_enabled:
        return ""
    try:
        pos_s, lab_s = valve_format_for_raw(valve_status_file, valve_stale_s)
        return f" {pos_s} {lab_s}"
    except Exception:
        return " -1 -"


def _auto_flag(calib_auto, valve_enabled, valve_status_file, valve_stale_s, calib_labels):
    """Determina il flag measure/calib in base alla valvola (se calib_auto attivo).

    Se calib_auto è False o la valvola non è abilitata: ritorna 'measure'.
    """
    if not calib_auto or not valve_enabled:
        return "measure"
    try:
        return valve_get_flag(valve_status_file, valve_stale_s, calib_labels)
    except Exception:
        return "measure"


def main():
    config = load_config()
    get_data_dir(config)

    # Integrazione valve-scheduler (opt-in, letta una volta all'avvio)
    (valve_enabled, valve_status_file, valve_stale_s,
     calib_auto, calib_labels) = load_valve_integration()
    if valve_enabled:
        print(f"[integration] valve-scheduler ATTIVA — status_file={valve_status_file}")
        if calib_auto:
            print(f"[integration] calib_auto ATTIVO — calib_labels={calib_labels}")
        else:
            print("[integration] calib_auto disattivo — flag sempre 'measure'")
    else:
        print("[integration] valve-scheduler disattiva (formato file storico)")

    device = config.get('serial', 'port', fallback='/dev/ttyUSB0')
    baudrate = config.getint('serial', 'baudrate', fallback=19200)
    bytesize = config.getint('serial', 'bytesize', fallback=8)
    parity_str = config.get('serial', 'parity', fallback='N')
    stopbits = config.getint('serial', 'stopbits', fallback=1)
    timeout = config.getint('serial', 'timeout', fallback=1)

    parity_map = {'N': serial.PARITY_NONE, 'E': serial.PARITY_EVEN, 'O': serial.PARITY_ODD}
    parity = parity_map.get(parity_str.upper(), serial.PARITY_NONE)

    try:
        ser = serial.Serial(
            port=device,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout=timeout
        )
        time.sleep(2)
        ser.write(CMD_START)
    except serial.SerialException as e:
        print(f"Error opening serial port {device}: {e}")
        return

    sht31_bus = open_sht31_bus()
    _write_status_json(True)  # seriale aperta → strumento connesso

    co2_values = []
    t_values   = []
    rh_values  = []
    _last_co2 = None  # ultimo valore per status.json
    _last_t   = None
    _last_rh  = None
    current_minute = datetime.utcnow().replace(second=0, microsecond=0)

    raw_file, avg_file = get_filenames(config)
    write_headers_if_needed(raw_file, avg_file, config, valve_enabled)

    print(f"Logging started. Raw: {raw_file}, Min: {avg_file}")
    print(f"Serial: {device} @ {baudrate} bps; I2C bus {SHT31_BUS} addr 0x{SHT31_ADDR:02x}")

    while True:
        try:
            line = ser.readline().decode(errors='ignore').strip()
            now = datetime.utcnow()

            new_raw_file, new_avg_file = get_filenames(config)
            if new_raw_file != raw_file or new_avg_file != avg_file:
                raw_file, avg_file = new_raw_file, new_avg_file
                write_headers_if_needed(raw_file, avg_file, config, valve_enabled)
                print(f"New day. New files: {raw_file}, {avg_file}")

            if line:
                ts_str, current_timestamp = timestamp_now()
                co2 = parse_co2_from_line(line)

                if co2 is not None:
                    t, rh = read_sht31(sht31_bus)

                    flag = _auto_flag(calib_auto, valve_enabled,
                                      valve_status_file, valve_stale_s, calib_labels)

                    with open(raw_file, 'a') as f_raw:
                        f_raw.write(f"{ts_str} {co2:.2f} {t:.2f} {rh:.2f} {flag}\n")

                    if current_timestamp.replace(second=0, microsecond=0) == current_minute:
                        co2_values.append(co2)
                        t_values.append(t)
                        rh_values.append(rh)
                    else:
                        with open(avg_file, 'a') as f_avg:
                            ts_avg = current_minute.strftime("%Y-%m-%d %H:%M:%S")
                            if co2_values:
                                co2_avg = sum(co2_values) / len(co2_values)
                                co2_std = statistics.stdev(co2_values) if len(co2_values) > 1 else 0.0
                                n_co2   = len(co2_values)
                            else:
                                co2_avg, co2_std, n_co2 = MISSING, MISSING, 0
                            t_avg,  t_std  = mean_std_missing(t_values)
                            rh_avg, rh_std = mean_std_missing(rh_values)
                            flag = _auto_flag(calib_auto, valve_enabled,
                                              valve_status_file, valve_stale_s, calib_labels)
                            valve_suf = _valve_suffix(valve_enabled, valve_status_file, valve_stale_s)
                            f_avg.write(
                                f"{ts_avg} {co2_avg:.2f} {co2_std:.2f} "
                                f"{t_avg:.2f} {t_std:.2f} "
                                f"{rh_avg:.2f} {rh_std:.2f} "
                                f"{n_co2} {flag}{valve_suf}\n"
                            )
                        _last_co2 = co2_avg if co2_avg != MISSING else None
                        _last_t   = t_avg   if t_avg   != MISSING else None
                        _last_rh  = rh_avg  if rh_avg  != MISSING else None
                        _write_status_json(True, _last_co2, _last_t, _last_rh)
                        current_minute = current_timestamp.replace(second=0, microsecond=0)
                        co2_values = [co2]
                        t_values   = [t]
                        rh_values  = [rh]
            else:
                if now.replace(second=0, microsecond=0) != current_minute:
                    with open(avg_file, 'a') as f_avg:
                        ts_avg = current_minute.strftime("%Y-%m-%d %H:%M:%S")
                        flag = _auto_flag(calib_auto, valve_enabled,
                                          valve_status_file, valve_stale_s, calib_labels)
                        valve_suf = _valve_suffix(valve_enabled, valve_status_file, valve_stale_s)
                        f_avg.write(
                            f"{ts_avg} {MISSING:.2f} {MISSING:.2f} "
                            f"{MISSING:.2f} {MISSING:.2f} "
                            f"{MISSING:.2f} {MISSING:.2f} "
                            f"0 {flag}{valve_suf}\n"
                        )
                    current_minute = now.replace(second=0, microsecond=0)
                    co2_values = []
                    t_values   = []
                    rh_values  = []
        except serial.SerialException as e:
            print(f"Serial communication error: {e}. Retrying in 5 seconds...")
            _write_status_json(False)
            ser.close()
            time.sleep(5)
            try:
                ser.open()
                ser.write(CMD_START)
                _write_status_json(True, _last_co2, _last_t, _last_rh)
            except serial.SerialException as reopen_e:
                print(f"Unable to reopen serial port: {reopen_e}. Exiting.")
                _write_status_json(False)
                break
        except Exception as e:
            print(f"Unexpected error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    main()
