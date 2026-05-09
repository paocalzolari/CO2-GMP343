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
import math
import tempfile
from collections import Counter
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

    Restituisce (enabled, status_file, stale_after_s, calib_auto,
                 calib_labels, measure_position).
    Se integration.ini non esiste o il modulo valve_state non è importabile,
    enabled=False — il logger scrive nel formato storico senza colonne valvola.
    Se calib_auto=True, il flag measure/calib è determinato così:
      - flag "calib" se valve_pos != measure_position (regola primaria)
      - oppure se valve_label è in calib_labels (legacy, OR)
      - se valve-scheduler non risponde → mantiene l'ultimo flag valido
    """
    if not _HAS_VALVE_MODULE or not os.path.exists(INTEGRATION_INI):
        return (False, "", 10.0, False, [], 1)
    cp = configparser.ConfigParser()
    cp.read(INTEGRATION_INI)
    if not cp.has_section("valve_scheduler"):
        return (False, "", 10.0, False, [], 1)
    enabled = cp.getboolean("valve_scheduler", "enabled", fallback=False)
    status_file = cp.get("valve_scheduler", "status_file",
                         fallback="~/programs/valve-scheduler/service/valve_status.json")
    stale = cp.getfloat("valve_scheduler", "stale_after_s", fallback=10.0)
    calib_auto = cp.getboolean("valve_scheduler", "calib_auto", fallback=False)
    calib_labels_raw = cp.get("valve_scheduler", "calib_labels", fallback="")
    calib_labels = [s.strip() for s in calib_labels_raw.split(",") if s.strip()]
    measure_position = cp.getint("valve_scheduler", "measure_position", fallback=1)
    return (enabled, os.path.expanduser(status_file), stale,
            calib_auto, calib_labels, measure_position)


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
    """Genera i nomi file giornalieri.

    Restituisce (raw, min, 10min, 30min, 60min) per il giorno corrente.
    Underscore nei nomi (formato v3, in uso dal 2026-04-15).
    """
    today     = datetime.utcnow().strftime("%Y%m%d")
    basename  = config.get("output", "basename",  fallback="carbocap343")
    extension = config.get("output", "extension", fallback="raw")
    site_name = config.get("location", "name",    fallback="unknown")
    data_dir  = get_data_dir(config)
    base = os.path.join(data_dir, f"{basename}_{site_name}_{today}_p00")
    return (
        f"{base}.{extension}",            # campioni grezzi (~1 Hz)
        f"{base}_min.{extension}",        # medie 1 min
        f"{base}_10min.{extension}",      # medie 10 min
        f"{base}_30min.{extension}",      # medie 30 min
        f"{base}_60min.{extension}",      # medie 60 min
    )


def write_headers_if_needed(raw_file, avg_file, file_10, file_30, file_60,
                            config, valve_enabled=False):
    """Scrive l'header nei file giornalieri se non esistono.

    RAW : #date time CO2 T RH flag [valve_pos valve_label]
    MIN : #date time CO2 CO2_std T T_std RH RH_std ndata_60s_mean flag [...]
    10/30 min: come MIN ma `ndata` = totale campioni nel bucket.
    60   min: come MIN ma con MEDIANE aggiunte (calcolate da raw):
              CO2 CO2_std CO2_median T T_std T_median RH RH_std RH_median.
    """
    if valve_enabled:
        raw_header  = "#date time CO2[PPM] T[C] RH[%] flag valve_pos valve_label"
        avg_header  = "#date time CO2[PPM] CO2_std[PPM] T[C] T_std[C] RH[%] RH_std[%] ndata_60s_mean flag valve_pos valve_label"
        agg_header  = "#date time CO2[PPM] CO2_std[PPM] T[C] T_std[C] RH[%] RH_std[%] ndata flag valve_pos valve_label"
        agg60_header= "#date time CO2[PPM] CO2_std[PPM] CO2_median[PPM] T[C] T_std[C] T_median[C] RH[%] RH_std[%] RH_median[%] ndata flag valve_pos valve_label"
    else:
        raw_header  = "#date time CO2[PPM] T[C] RH[%] flag"
        avg_header  = "#date time CO2[PPM] CO2_std[PPM] T[C] T_std[C] RH[%] RH_std[%] ndata_60s_mean flag"
        agg_header  = "#date time CO2[PPM] CO2_std[PPM] T[C] T_std[C] RH[%] RH_std[%] ndata flag"
        agg60_header= "#date time CO2[PPM] CO2_std[PPM] CO2_median[PPM] T[C] T_std[C] T_median[C] RH[%] RH_std[%] RH_median[%] ndata flag"

    pairs = [
        (raw_file, raw_header),
        (avg_file, avg_header),
        (file_10,  agg_header),
        (file_30,  agg_header),
        (file_60,  agg60_header),   # extended format with medians
    ]
    for path, header in pairs:
        if not os.path.exists(path):
            with open(path, 'w') as f:
                f.write(f"{header}\n")


# ──────────────────────────────────────────────────── pooled aggregation
def _pooled_mean_std(values, stds, ns):
    """Pooled mean & std using law of total variance.

    Skips MISSING entries (any of value/std == MISSING or n<=0).
    Returns (M, S, N_total). If no usable rows: (MISSING, MISSING, 0).
    """
    use = [(v, s, n) for v, s, n in zip(values, stds, ns)
           if v != MISSING and s != MISSING and n > 0]
    if not use:
        return MISSING, MISSING, 0
    N = sum(n for _, _, n in use)
    if N <= 0:
        return MISSING, MISSING, 0
    M = sum(n * v for v, _, n in use) / N
    # Var[X] = E[Var[X|i]] + Var[E[X|i]]
    inner = sum(n * s * s for _, s, n in use) / N
    outer = sum(n * (v - M) ** 2 for v, _, n in use) / N
    var = inner + outer
    if N > 1:
        var = var * N / (N - 1)   # unbiased estimator
    S = math.sqrt(var) if var > 0 else 0.0
    return M, S, N


def _slot_start(t, granularity_min):
    """Aligned slot start for a clock-aligned bucket of given size in minutes."""
    return t.replace(
        minute=(t.minute // granularity_min) * granularity_min,
        second=0, microsecond=0,
    )


def _flush_slot(path, slot_ts, buf, valve_enabled):
    """Write one aggregated record summarising the minute records in `buf`.

    `buf` is a list of dicts (output of `_make_minute_record`). Empty `buf`
    is a no-op. Computes pooled CO2/T/RH stats, sums n, sticky-calib flag,
    most-frequent valve_pos/label.
    """
    if not buf:
        return
    co2_v  = [r["co2"]      for r in buf]
    co2_s  = [r["co2_std"]  for r in buf]
    co2_n  = [r["n"]        for r in buf]
    M_c, S_c, N = _pooled_mean_std(co2_v, co2_s, co2_n)
    M_t, S_t, _ = _pooled_mean_std(
        [r["t"]  for r in buf], [r["t_std"]  for r in buf], co2_n)
    M_r, S_r, _ = _pooled_mean_std(
        [r["rh"] for r in buf], [r["rh_std"] for r in buf], co2_n)
    flag = "calib" if any(r["flag"] == "calib" for r in buf) else "measure"
    if valve_enabled:
        # mode (most-frequent); ties broken by most recent
        vpos = Counter(r["valve_pos"]   for r in buf).most_common(1)[0][0]
        vlab = Counter(r["valve_label"] for r in buf).most_common(1)[0][0]
        valve_suf = f" {vpos} {vlab}"
    else:
        valve_suf = ""
    ts = slot_ts.strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a") as f:
        f.write(
            f"{ts} {M_c:.2f} {S_c:.2f} "
            f"{M_t:.2f} {S_t:.2f} "
            f"{M_r:.2f} {S_r:.2f} "
            f"{N} {flag}{valve_suf}\n"
        )


def _flush_60min_with_median(path, slot_ts, buf_minutes,
                             raw_co2, raw_t, raw_rh, valve_enabled):
    """Write a 60-min row computing mean/std/median directly from raw samples.

    `raw_co2/_t/_rh` are flat lists of all per-sample readings collected
    during the past hour (MISSING values are filtered per quantity at
    aggregation time). `buf_minutes` is reused only for flag/valve-pos
    metadata (sticky-calib + most-frequent), since those are already
    decimated per-minute and that level of granularity is enough.
    """
    if not raw_co2 and not buf_minutes:
        return

    def _stats(values):
        clean = [v for v in values if v != MISSING]
        if not clean:
            return MISSING, MISSING, MISSING
        m = sum(clean) / len(clean)
        s = statistics.stdev(clean) if len(clean) > 1 else 0.0
        med = statistics.median(clean)
        return m, s, med

    M_c, S_c, Med_c = _stats(raw_co2)
    M_t, S_t, Med_t = _stats(raw_t)
    M_r, S_r, Med_r = _stats(raw_rh)
    N = sum(1 for v in raw_co2 if v != MISSING)
    flag = "calib" if any(r["flag"] == "calib" for r in buf_minutes) else "measure"
    if valve_enabled and buf_minutes:
        vpos = Counter(r["valve_pos"]   for r in buf_minutes).most_common(1)[0][0]
        vlab = Counter(r["valve_label"] for r in buf_minutes).most_common(1)[0][0]
        valve_suf = f" {vpos} {vlab}"
    else:
        valve_suf = ""
    ts = slot_ts.strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a") as f:
        f.write(
            f"{ts} {M_c:.2f} {S_c:.2f} {Med_c:.2f} "
            f"{M_t:.2f} {S_t:.2f} {Med_t:.2f} "
            f"{M_r:.2f} {S_r:.2f} {Med_r:.2f} "
            f"{N} {flag}{valve_suf}\n"
        )


def _make_minute_record(co2, co2_std, n_co2, t, t_std, rh, rh_std,
                        flag, valve_enabled, valve_status_file, valve_stale_s):
    """Pack the just-closed minute aggregate into a dict for slot buffers."""
    if valve_enabled:
        try:
            vpos_str, vlabel_str = valve_format_for_raw(
                valve_status_file, valve_stale_s)
        except Exception:
            vpos_str, vlabel_str = "-1", "-"
    else:
        vpos_str, vlabel_str = "-1", "-"
    return {
        "co2": co2, "co2_std": co2_std, "n": n_co2,
        "t":   t,   "t_std":   t_std,
        "rh":  rh,  "rh_std":  rh_std,
        "flag": flag,
        "valve_pos": vpos_str, "valve_label": vlabel_str,
    }

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


def _auto_flag(calib_auto, valve_enabled, valve_status_file, valve_stale_s,
               calib_labels, measure_position=1):
    """Determina il flag measure/calib in base alla valvola (se calib_auto attivo).

    Regola (in valve_get_flag): "calib" se valve_pos != measure_position
    OR valve_label ∈ calib_labels. Se valve-scheduler non risponde, viene
    mantenuto l'ultimo flag valido.

    Se calib_auto è False o la valvola non è abilitata: ritorna 'measure'.
    """
    if not calib_auto or not valve_enabled:
        return "measure"
    try:
        return valve_get_flag(valve_status_file, valve_stale_s,
                              calib_labels, measure_position)
    except Exception:
        return "measure"


def main():
    config = load_config()
    get_data_dir(config)

    # Integrazione valve-scheduler (opt-in, letta una volta all'avvio)
    (valve_enabled, valve_status_file, valve_stale_s,
     calib_auto, calib_labels, measure_position) = load_valve_integration()
    if valve_enabled:
        print(f"[integration] valve-scheduler ATTIVA — status_file={valve_status_file}")
        if calib_auto:
            print(f"[integration] calib_auto ATTIVO — measure_position={measure_position}, "
                  f"calib_labels={calib_labels}")
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
    # Dedup-by-value: il GMP343 emette a 1 Hz ma aggiorna internamente ogni
    # ~2 s, quindi vediamo run di campioni identici. Saltiamo le ripetizioni
    # per non oversamplare e rendere onesta la σ del minuto.
    last_co2_value = None
    current_minute = datetime.utcnow().replace(second=0, microsecond=0)

    raw_file, avg_file, file_10, file_30, file_60 = get_filenames(config)
    write_headers_if_needed(raw_file, avg_file, file_10, file_30, file_60,
                            config, valve_enabled)

    # Slot-aligned buffers per le aggregazioni 10/30/60 minuti.
    # Si flushano quando il minuto appena chiuso entra in un nuovo slot,
    # così il record del bucket precedente viene scritto subito dopo
    # la fine del bucket (latenza ~1 minuto).
    buf_10  = []; slot_10  = _slot_start(current_minute, 10)
    buf_30  = []; slot_30  = _slot_start(current_minute, 30)
    buf_60  = []; slot_60  = _slot_start(current_minute, 60)
    # Raw sample buffer per la mediana 60-min: vi accumuliamo OGNI singolo
    # campione (post-dedup) dell'ora corrente; al boundary 60-min
    # calcoliamo mean/std/median direttamente dai sample (non dai pooled
    # 1-min, così la mediana è quella vera dei dati grezzi).
    buf_60_raw_co2 = []
    buf_60_raw_t   = []
    buf_60_raw_rh  = []

    print(f"Logging started. Raw: {raw_file}, Min: {avg_file}")
    print(f"Aggregates: 10/{file_10}  30/{file_30}  60/{file_60}")
    print(f"Serial: {device} @ {baudrate} bps; I2C bus {SHT31_BUS} addr 0x{SHT31_ADDR:02x}")

    def _on_minute_closed(min_record):
        """Hook called right after a 1-min record is written.

        Updates the 10/30/60-min slot buffers and flushes any buffer whose
        slot the just-closed minute has crossed. The 60-min flush also
        computes mean/std/median straight from the raw sample buffer.
        """
        nonlocal buf_10, slot_10, buf_30, slot_30, buf_60, slot_60
        nonlocal buf_60_raw_co2, buf_60_raw_t, buf_60_raw_rh
        # 10-min
        this_slot10 = _slot_start(current_minute, 10)
        if this_slot10 != slot_10:
            _flush_slot(file_10, slot_10, buf_10, valve_enabled)
            buf_10 = []
            slot_10 = this_slot10
        buf_10.append(min_record)
        # 30-min
        this_slot30 = _slot_start(current_minute, 30)
        if this_slot30 != slot_30:
            _flush_slot(file_30, slot_30, buf_30, valve_enabled)
            buf_30 = []
            slot_30 = this_slot30
        buf_30.append(min_record)
        # 60-min: usa i RAW sample dell'ora per mean/std/median
        this_slot60 = _slot_start(current_minute, 60)
        if this_slot60 != slot_60:
            _flush_60min_with_median(
                file_60, slot_60, buf_60,
                buf_60_raw_co2, buf_60_raw_t, buf_60_raw_rh,
                valve_enabled)
            buf_60 = []
            buf_60_raw_co2 = []
            buf_60_raw_t   = []
            buf_60_raw_rh  = []
            slot_60 = this_slot60
        buf_60.append(min_record)

    while True:
        try:
            line = ser.readline().decode(errors='ignore').strip()
            now = datetime.utcnow()

            new_files = get_filenames(config)
            if new_files != (raw_file, avg_file, file_10, file_30, file_60):
                raw_file, avg_file, file_10, file_30, file_60 = new_files
                write_headers_if_needed(raw_file, avg_file, file_10, file_30, file_60,
                                        config, valve_enabled)
                print(f"New day. Files rotated: {raw_file}, {avg_file}, ...")

            if line:
                ts_str, current_timestamp = timestamp_now()
                co2 = parse_co2_from_line(line)

                if co2 is not None:
                    # Dedup-by-value: il sensore aggiorna ~ogni 2 s.
                    # Se il valore CO₂ è identico al precedente non aggiunge
                    # informazione nuova → saltiamo la scrittura sul .raw e
                    # NON lo conteggiamo nelle statistiche del minuto.
                    is_duplicate = (last_co2_value is not None
                                    and co2 == last_co2_value)
                    if is_duplicate:
                        # Avanzamento del minuto va comunque gestito
                        # (anche senza letture indipendenti) — vedi sotto.
                        pass
                    else:
                        t, rh = read_sht31(sht31_bus)
                        last_co2_value = co2
                        flag = _auto_flag(calib_auto, valve_enabled,
                                          valve_status_file, valve_stale_s,
                                          calib_labels, measure_position)

                        valve_suf_raw = _valve_suffix(valve_enabled, valve_status_file, valve_stale_s)
                        with open(raw_file, 'a') as f_raw:
                            f_raw.write(f"{ts_str} {co2:.2f} {t:.2f} {rh:.2f} {flag}{valve_suf_raw}\n")

                    if current_timestamp.replace(second=0, microsecond=0) == current_minute:
                        # Stesso minuto: aggiorna i buffer (solo se non duplicato)
                        if not is_duplicate:
                            co2_values.append(co2)
                            t_values.append(t)
                            rh_values.append(rh)
                            # Raw buffer per la mediana 60-min (un sample
                            # per chiamata, post-dedup)
                            buf_60_raw_co2.append(co2)
                            buf_60_raw_t.append(t)
                            buf_60_raw_rh.append(rh)
                    else:
                        # Cambio minuto: chiudi il minuto corrente e scrivi
                        # il record _min.raw, poi gli aggregati 10/30/60.
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
                                              valve_status_file, valve_stale_s,
                                              calib_labels, measure_position)
                            valve_suf = _valve_suffix(valve_enabled, valve_status_file, valve_stale_s)
                            f_avg.write(
                                f"{ts_avg} {co2_avg:.2f} {co2_std:.2f} "
                                f"{t_avg:.2f} {t_std:.2f} "
                                f"{rh_avg:.2f} {rh_std:.2f} "
                                f"{n_co2} {flag}{valve_suf}\n"
                            )
                        # Aggrega nei bucket 10/30/60 min
                        _on_minute_closed(_make_minute_record(
                            co2_avg, co2_std, n_co2,
                            t_avg, t_std, rh_avg, rh_std,
                            flag, valve_enabled, valve_status_file, valve_stale_s))

                        _last_co2 = co2_avg if co2_avg != MISSING else None
                        _last_t   = t_avg   if t_avg   != MISSING else None
                        _last_rh  = rh_avg  if rh_avg  != MISSING else None
                        _write_status_json(True, _last_co2, _last_t, _last_rh)
                        current_minute = current_timestamp.replace(second=0, microsecond=0)
                        # Avvia il nuovo minuto: includi il campione corrente
                        # solo se non è un duplicato.
                        if is_duplicate:
                            co2_values = []
                            t_values   = []
                            rh_values  = []
                        else:
                            co2_values = [co2]
                            t_values   = [t]
                            rh_values  = [rh]
                            # Il sample corrente appartiene già al nuovo
                            # minuto: includilo anche nel raw buffer 60-min
                            # (se il flush 60 è stato appena eseguito,
                            # buf_60_raw_* è già stato azzerato in
                            # _on_minute_closed e questo è il primo sample
                            # del nuovo bucket; altrimenti si accumula).
                            buf_60_raw_co2.append(co2)
                            buf_60_raw_t.append(t)
                            buf_60_raw_rh.append(rh)
            else:
                if now.replace(second=0, microsecond=0) != current_minute:
                    with open(avg_file, 'a') as f_avg:
                        ts_avg = current_minute.strftime("%Y-%m-%d %H:%M:%S")
                        flag = _auto_flag(calib_auto, valve_enabled,
                                          valve_status_file, valve_stale_s,
                                          calib_labels, measure_position)
                        valve_suf = _valve_suffix(valve_enabled, valve_status_file, valve_stale_s)
                        f_avg.write(
                            f"{ts_avg} {MISSING:.2f} {MISSING:.2f} "
                            f"{MISSING:.2f} {MISSING:.2f} "
                            f"{MISSING:.2f} {MISSING:.2f} "
                            f"0 {flag}{valve_suf}\n"
                        )
                    # Anche un minuto vuoto va nei buffer: la pooling salta
                    # i MISSING ma il minuto conta come "trascorso" per gli
                    # slot 10/30/60.
                    _on_minute_closed(_make_minute_record(
                        MISSING, MISSING, 0,
                        MISSING, MISSING, MISSING, MISSING,
                        flag, valve_enabled, valve_status_file, valve_stale_s))
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
