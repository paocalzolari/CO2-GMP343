#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gmp343_logger-8.py
Acquisizione seriale GMP343 → file raw e _min giornalieri.
File ini letti da ~/programs/CO2/config/
data_path letto da name.ini (default: ~/data)

Formato file v2 (dal 2026):
  - Nome: carbocap343_<site>_<YYYYMMDD>_p00_min.raw (underscore, non trattini)
  - Data/ora: YYYY-MM-DD HH:MM:SS (con trattini e due punti)
  - Header _min: #date time CO2[PPM] CO2_std[PPM] ndata_60s_mean flag
  - Std in PPM assoluto (non percentuale)
  - Flag fisso: measure
"""
import serial
import time
from datetime import datetime
import os
import sys
import statistics
import configparser

# ── Percorsi ──────────────────────────────────────────────────────────────────
# I file ini stanno SEMPRE in ~/programs/CO2/config/
CONFIG_DIR = os.path.expanduser("~/programs/CO2/config")
NAME_INI   = os.path.join(CONFIG_DIR, "name.ini")
SERIAL_INI = os.path.join(CONFIG_DIR, "serial.ini")
SITE_INI   = os.path.join(CONFIG_DIR, "site.ini")

CMD_START = b"R\r\n"


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

def write_headers_if_needed(raw_file, avg_file, config):
    """
    Scrive header nei file se non esistono.
    RAW: #date time CO2[PPM] flag
    MIN: #date time CO2[PPM] CO2_std[PPM] ndata_60s_mean flag
    """
    raw_header = "#date time CO2[PPM] flag"
    avg_header = "#date time CO2[PPM] CO2_std[PPM] ndata_60s_mean flag"

    if not os.path.exists(raw_file):
        with open(raw_file, 'w') as f:
            f.write(f"{raw_header}\n")
    if not os.path.exists(avg_file):
        with open(avg_file, 'w') as f:
            f.write(f"{avg_header}\n")

def timestamp_now():
    now = datetime.utcnow()
    # Formato: YYYY-MM-DD HH:MM:SS.fff
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

def main():
    config = load_config()
    data_dir = get_data_dir(config)   # crea cartella se non esiste

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

    co2_values = []
    current_minute = datetime.utcnow().replace(second=0, microsecond=0)

    raw_file, avg_file = get_filenames(config)
    write_headers_if_needed(raw_file, avg_file, config)

    print(f"Logging started. Raw data in: {raw_file}, Averaged data in: {avg_file}")
    print(f"Serial connection: {device} @ {baudrate} bps")

    while True:
        try:
            line = ser.readline().decode(errors='ignore').strip()
            now = datetime.utcnow()
            
            new_raw_file, new_avg_file = get_filenames(config)
            if new_raw_file != raw_file or new_avg_file != avg_file:
                raw_file, avg_file = new_raw_file, new_avg_file
                write_headers_if_needed(raw_file, avg_file, config)
                print(f"New day. New files: {raw_file}, {avg_file}")

            if line:
                ts_str, current_timestamp = timestamp_now()
                value = parse_co2_from_line(line)
                
                if value is not None:
                    with open(raw_file, 'a') as f_raw:
                        f_raw.write(f"{ts_str} {value:.2f} measure\n")

                    if current_timestamp.replace(second=0, microsecond=0) == current_minute:
                        co2_values.append(value)
                    else:
                        with open(avg_file, 'a') as f_avg:
                            ts_avg = current_minute.strftime("%Y-%m-%d %H:%M:%S")
                            if co2_values:
                                avg = sum(co2_values) / len(co2_values)
                                std = statistics.stdev(co2_values) if len(co2_values) > 1 else 0.0
                                n   = len(co2_values)
                            else:
                                avg, std, n = 999.99, 0.00, 0
                            f_avg.write(f"{ts_avg} {avg:.2f} {std:.2f} {n} measure\n")
                        current_minute = current_timestamp.replace(second=0, microsecond=0)
                        co2_values = [value]
            else:
                if now.replace(second=0, microsecond=0) != current_minute:
                    with open(avg_file, 'a') as f_avg:
                        ts_avg = current_minute.strftime("%Y-%m-%d %H:%M:%S")
                        f_avg.write(f"{ts_avg} 999.99 0.00 0 measure\n")
                    current_minute = now.replace(second=0, microsecond=0)
                    co2_values = []
        except serial.SerialException as e:
            print(f"Serial communication error: {e}. Retrying in 5 seconds...")
            ser.close()
            time.sleep(5)
            try:
                ser.open()
                ser.write(CMD_START)
            except serial.SerialException as reopen_e:
                print(f"Unable to reopen serial port: {reopen_e}. Exiting.")
                break
        except Exception as e:
            print(f"Unexpected error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    main()
