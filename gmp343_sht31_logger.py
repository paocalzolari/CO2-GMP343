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
  - Header raw:  #date time CO2[PPM] T[C] RH[%] flag [valve_pos valve_label] CO2RAW[PPM] CO2RAWUC[PPM]
  - Header _min: #date time CO2[PPM] CO2_std[PPM] T[C] T_std[C] RH[%] RH_std[%] ndata_60s_mean flag [valve_pos valve_label]
  - Std in PPM assoluto (non percentuale)

Formato file v5 (dal 2026-06-23, 3 valori CO2 dalla GMP343):
  - La sonda è configurata (comando FORM, salvato in EEPROM) per emettere
    3 grandezze: CO2 (filtrata+compensata), CO2RAW (non filtrata, compensata),
    CO2RAWUC (non filtrata, SENZA compensazioni) — vedi manuale M210514EN-C p.36.
  - Le colonne CO2RAW/CO2RAWUC sono aggiunte IN CODA alla riga `.raw`
    (dopo le colonne valvola) per non rompere i parser posizionali esistenti.
  - CO2 resta in colonna 3: i file `_min`/10/30/60 e il monitor sono invariati
    (aggregano la CO2 corretta). Gli aggregati dei valori grezzi si possono
    ricalcolare dal `.raw` e verranno aggiunti in un secondo momento.
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
import socket
import statistics


def _sd_notify(state):
    """Notifica systemd via $NOTIFY_SOCKET (WATCHDOG=1 / READY=1), zero
    dipendenze. No-op se non lanciato da systemd con notify/watchdog.
    Serve al watchdog di liveness: se il loop si impianta e smette di inviare
    WATCHDOG=1, systemd killa e riavvia il servizio (Restart=always).
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        if addr.startswith("@"):
            addr = "\0" + addr[1:]          # abstract namespace socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.connect(addr)
        s.sendall(state.encode())
        s.close()
    except Exception:
        pass
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

# ── Compensazione P/RH (opt-in, poll-mode) ────────────────────────────────────
# Import tollerante: i moduli bmp388 / gmp343_compensation servono SOLO se
# [compensation] enabled=true in sensors.ini. Se mancano o la feature è
# disattiva, il logger gira in RUN-mode come sempre.
try:
    import bmp388
    import gmp343_compensation as gmp343_comp
    _HAS_COMP_MODULES = True
except ImportError:
    _HAS_COMP_MODULES = False

# Import tollerante del driver TSI 4140 (flussimetro): serve solo se
# [tsi4140] enabled=true in sensors.ini.
try:
    import tsi4140
    _HAS_TSI = True
except ImportError:
    _HAS_TSI = False

# ── Percorsi ──────────────────────────────────────────────────────────────────
CONFIG_DIR      = os.path.expanduser("~/programs/CO2/config")
NAME_INI        = os.path.join(CONFIG_DIR, "name.ini")
SERIAL_INI      = os.path.join(CONFIG_DIR, "serial.ini")
SITE_INI        = os.path.join(CONFIG_DIR, "site.ini")
INTEGRATION_INI = os.path.join(CONFIG_DIR, "integration.ini")  # opzionale
SENSORS_INI     = os.path.join(CONFIG_DIR, "sensors.ini")      # SHT3X/BMP388/compensazione

CMD_START = b"R\r\n"

# ── Status JSON per acq-tools ────────────────────────────────────────────────
STATUS_JSON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "shared", "ipc_co2", "status.json")


# Stato compensazione P/RH pubblicato in status.json (per launcher + monitor GUI).
# Aggiornato da main() all'avvio; i valori live (rh inviata) ad ogni minuto.
_COMP_STATUS = {
    "comp_active": False,      # poll-mode con feed attivo
    "comp_rh_fed": None,       # %RH inviata alla sonda (None se non inviata)
    "comp_p_hpa": None,        # hPa usati per la compensazione (fissi o BMP388)
    "comp_p_source": None,     # "fixed" | "bmp388" | None
}


def _write_status_json(instrument_connected, last_co2=None, last_t=None,
                       last_rh=None, last_co2rawuc=None,
                       last_flow_mass=None, last_flow_vol=None):
    """Scrive status.json atomicamente (tmp + rename) per acq-tools.

    Aggiorna l'mtime — acq-tools considera stale dopo 120s.
    Include lo stato di compensazione P/RH (_COMP_STATUS) per launcher/monitor.
    last_co2_ppm = CO2 CORRETTA (compensata); last_co2rawuc_ppm = CO2 GREZZA
    (senza compensazioni) → le GUI mostrano entrambe con dicitura.
    last_flow_mass_slpm / last_flow_vol_lpm = flusso TSI 4140 (massa/volumetrico)
    per la finestra Current Data del monitor (live).
    """
    status = {
        "instrument_connected": instrument_connected,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "last_co2_ppm": last_co2,
        "last_co2rawuc_ppm": last_co2rawuc,
        "last_t_c": last_t,
        "last_rh_pct": last_rh,
        "last_flow_mass_slpm": last_flow_mass,
        "last_flow_vol_lpm": last_flow_vol,
    }
    status.update(_COMP_STATUS)
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


def load_sensors_config():
    """Legge config/sensors.ini e ritorna un dict con la config sensori e
    compensazione. Tollerante: se il file o le sezioni mancano, usa i default
    (SHT3X 0x44 abilitato, BMP388 disabilitato, compensazione DISABILITATA).

    Effetto collaterale: aggiorna le globali SHT31_BUS/SHT31_ADDR così che
    open_sht31_bus()/read_sht31() usino l'indirizzo da config (0x44 o 0x45).

    Ritorna:
      {
        "bmp388": {"enabled": bool, "bus": int, "addr": int},
        "comp":   {"enabled": bool, "addr": int,
                   "feed_pressure": bool, "feed_humidity": bool,
                   "poll_interval_s": float, "default_p_hpa": float},
      }
    """
    global SHT31_BUS, SHT31_ADDR
    cp = configparser.ConfigParser()
    cp.read(SENSORS_INI)

    def _addr(sec, key, default):
        raw = cp.get(sec, key, fallback=None)
        if raw is None:
            return default
        return int(raw, 16) if raw.strip().lower().startswith("0x") else int(raw)

    # SHT3X (sezione sht31_a) → aggiorna le globali
    if cp.has_section("sht31_a") and cp.getboolean("sht31_a", "enabled", fallback=True):
        SHT31_BUS  = cp.getint("sht31_a", "bus",  fallback=SHT31_BUS)
        SHT31_ADDR = _addr("sht31_a", "addr", SHT31_ADDR)

    bmp = {
        "enabled": cp.getboolean("bmp388", "enabled", fallback=False),
        "bus":     cp.getint("bmp388", "bus", fallback=1),
        "addr":    _addr("bmp388", "addr", 0x77),
    }
    tsi = {
        "enabled": cp.getboolean("tsi4140", "enabled", fallback=False),
        "port":    cp.get("tsi4140", "port", fallback="/dev/tsi4140"),
    }
    comp = {
        "enabled":         cp.getboolean("compensation", "enabled", fallback=False),
        "addr":            cp.getint("compensation", "gmp343_addr", fallback=0),
        "feed_pressure":   cp.getboolean("compensation", "feed_pressure", fallback=True),
        "feed_humidity":   cp.getboolean("compensation", "feed_humidity", fallback=True),
        "poll_interval_s": cp.getfloat("compensation", "poll_interval_s", fallback=2.0),
        "default_p_hpa":   cp.getfloat("compensation", "default_p_hpa", fallback=1013.0),
        # Pressione fissa (hPa) usata quando NON si alimenta P live (no BMP388).
        # Serve comunque alla sonda per la compensazione RH (vedi manuale). 0 = non impostare.
        "fixed_pressure_hpa": cp.getfloat("compensation", "fixed_pressure_hpa", fallback=0.0),
    }
    return {"bmp388": bmp, "comp": comp, "tsi": tsi}


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

def get_filenames(config, day=None):
    """Genera i nomi file giornalieri per la data `day` (UTC).

    `day=None` → oggi (UTC). Accetta anche un oggetto datetime (.date()
    viene preso) o una date.

    Restituisce (raw, min, 10min, 30min, 60min) per quella data.
    Underscore nei nomi (formato v3, in uso dal 2026-04-15).
    """
    if day is None:
        d = datetime.utcnow().date()
    elif isinstance(day, datetime):
        d = day.date()
    else:
        d = day
    yyyymmdd  = d.strftime("%Y%m%d")
    basename  = config.get("output", "basename",  fallback="carbocap343")
    extension = config.get("output", "extension", fallback="raw")
    site_name = config.get("location", "name",    fallback="unknown")
    data_dir  = get_data_dir(config)
    base = os.path.join(data_dir, f"{basename}_{site_name}_{yyyymmdd}_p00")
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
    # Colonne CO2RAW/CO2RAWUC aggiunte IN CODA (dopo valvola) anche negli
    # aggregati, così i parser posizionali esistenti restano validi.
    raw_co2  = "CO2RAW[PPM] CO2RAWUC[PPM] P[hPa]"
    avg_co2  = "CO2RAW[PPM] CO2RAW_std[PPM] CO2RAWUC[PPM] CO2RAWUC_std[PPM] P[hPa] P_std[hPa]"
    med_co2  = ("CO2RAW[PPM] CO2RAW_std[PPM] CO2RAW_median[PPM] "
                "CO2RAWUC[PPM] CO2RAWUC_std[PPM] CO2RAWUC_median[PPM] "
                "P[hPa] P_std[hPa] P_median[hPa]")
    # Colonne flussimetro TSI 4140 IN CODA (dopo le colonne CO2/P). Etichette
    # esplicite massa (SLPM = Standard L/min) vs volumetrico (Lpm alle
    # condizioni reali, calcolato con la P del BMP388).
    raw_tsi  = "FLOWmass[slpm] FLOWvol[Lpm]"
    avg_tsi  = "FLOWmass[slpm] FLOWmass_std[slpm] FLOWvol[Lpm] FLOWvol_std[Lpm]"
    med_tsi  = ("FLOWmass[slpm] FLOWmass_std[slpm] FLOWmass_median[slpm] "
                "FLOWvol[Lpm] FLOWvol_std[Lpm] FLOWvol_median[Lpm]")
    if valve_enabled:
        raw_header  = f"#date time CO2[PPM] T[C] RH[%] flag valve_pos valve_label {raw_co2} {raw_tsi}"
        avg_header  = f"#date time CO2[PPM] CO2_std[PPM] T[C] T_std[C] RH[%] RH_std[%] ndata_60s_mean flag valve_pos valve_label {avg_co2} {avg_tsi}"
        agg_header  = f"#date time CO2[PPM] CO2_std[PPM] T[C] T_std[C] RH[%] RH_std[%] ndata flag valve_pos valve_label {avg_co2} {avg_tsi}"
        agg60_header= f"#date time CO2[PPM] CO2_std[PPM] CO2_median[PPM] T[C] T_std[C] T_median[C] RH[%] RH_std[%] RH_median[%] ndata flag valve_pos valve_label {med_co2} {med_tsi}"
    else:
        raw_header  = f"#date time CO2[PPM] T[C] RH[%] flag {raw_co2} {raw_tsi}"
        avg_header  = f"#date time CO2[PPM] CO2_std[PPM] T[C] T_std[C] RH[%] RH_std[%] ndata_60s_mean flag {avg_co2} {avg_tsi}"
        agg_header  = f"#date time CO2[PPM] CO2_std[PPM] T[C] T_std[C] RH[%] RH_std[%] ndata flag {avg_co2} {avg_tsi}"
        agg60_header= f"#date time CO2[PPM] CO2_std[PPM] CO2_median[PPM] T[C] T_std[C] T_median[C] RH[%] RH_std[%] RH_median[%] ndata flag {med_co2} {med_tsi}"

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
    is a no-op.

    GAW-style filtering: stats are computed ONLY from minutes with
    flag != "calib" (calibration data must not pollute atmospheric
    means). The slot keeps a "sticky-calib" flag if any underlying
    minute was calib, so downstream consumers can identify slots that
    overlap with calibration events.

    If the whole slot is calib, writes a row with MISSING values + n=0
    + flag=calib + valve metadata (mode over all minutes).
    """
    if not buf:
        return
    measure_buf = [r for r in buf if r["flag"] != "calib"]
    if measure_buf:
        co2_n = [r["n"] for r in measure_buf]
        M_c, S_c, N = _pooled_mean_std(
            [r["co2"]     for r in measure_buf],
            [r["co2_std"] for r in measure_buf],
            co2_n)
        M_t, S_t, _ = _pooled_mean_std(
            [r["t"]     for r in measure_buf],
            [r["t_std"] for r in measure_buf], co2_n)
        M_r, S_r, _ = _pooled_mean_std(
            [r["rh"]     for r in measure_buf],
            [r["rh_std"] for r in measure_buf], co2_n)
        # CO2RAW / CO2RAWUC pooled (in coda)
        M_cr, S_cr, _ = _pooled_mean_std(
            [r.get("co2raw", MISSING)     for r in measure_buf],
            [r.get("co2raw_std", MISSING) for r in measure_buf], co2_n)
        M_cu, S_cu, _ = _pooled_mean_std(
            [r.get("co2rawuc", MISSING)     for r in measure_buf],
            [r.get("co2rawuc_std", MISSING) for r in measure_buf], co2_n)
        M_p, S_p, _ = _pooled_mean_std(
            [r.get("p", MISSING)     for r in measure_buf],
            [r.get("p_std", MISSING) for r in measure_buf], co2_n)
        # Flusso TSI (massa / volumetrico) pooled (in coda)
        M_fm, S_fm, _ = _pooled_mean_std(
            [r.get("fmass", MISSING)     for r in measure_buf],
            [r.get("fmass_std", MISSING) for r in measure_buf], co2_n)
        M_fv, S_fv, _ = _pooled_mean_std(
            [r.get("fvol", MISSING)     for r in measure_buf],
            [r.get("fvol_std", MISSING) for r in measure_buf], co2_n)
    else:
        M_c = S_c = M_t = S_t = M_r = S_r = MISSING
        M_cr = S_cr = M_cu = S_cu = MISSING
        M_p = S_p = MISSING
        M_fm = S_fm = M_fv = S_fv = MISSING
        N = 0
    flag = "calib" if any(r["flag"] == "calib" for r in buf) else "measure"
    if valve_enabled:
        # mode over MEASURE minutes preferred; if all calib, fall back
        # to all minutes so the row still carries a valve-pos hint.
        ref_buf = measure_buf if measure_buf else buf
        vpos = Counter(r["valve_pos"]   for r in ref_buf).most_common(1)[0][0]
        vlab = Counter(r["valve_label"] for r in ref_buf).most_common(1)[0][0]
        valve_suf = f" {vpos} {vlab}"
    else:
        valve_suf = ""
    ts = slot_ts.strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a") as f:
        f.write(
            f"{ts} {M_c:.2f} {S_c:.2f} "
            f"{M_t:.2f} {S_t:.2f} "
            f"{M_r:.2f} {S_r:.2f} "
            f"{N} {flag}{valve_suf} "
            f"{M_cr:.2f} {S_cr:.2f} {M_cu:.2f} {S_cu:.2f} "
            f"{M_p:.2f} {S_p:.2f} "
            f"{M_fm:.3f} {S_fm:.3f} {M_fv:.3f} {S_fv:.3f}\n"
        )


def _flush_60min_with_median(path, slot_ts, buf_minutes,
                             raw_co2, raw_t, raw_rh, raw_flag,
                             valve_enabled,
                             raw_co2raw=None, raw_co2rawuc=None, raw_p=None,
                             raw_fmass=None, raw_fvol=None):
    """Write a 60-min row computing mean/std/median directly from raw samples.

    `raw_co2/_t/_rh` are flat lists of per-sample readings collected
    during the past hour, `raw_flag` is the parallel list of per-sample
    flags ('measure'/'calib').

    GAW-style filtering: stats are computed ONLY from samples with
    flag != "calib" (calibration data must not contaminate atmospheric
    statistics). The hour keeps a "sticky-calib" flag if any underlying
    sample/minute was calib, so consumers can mark contaminated hours.

    `buf_minutes` is used only for flag/valve-pos metadata (decimated
    to per-minute granularity is enough for that).
    """
    if not raw_co2 and not buf_minutes:
        return

    raw_co2raw   = raw_co2raw   or []
    raw_co2rawuc = raw_co2rawuc or []
    raw_p        = raw_p        or []
    raw_fmass    = raw_fmass    or []
    raw_fvol     = raw_fvol     or []
    # Filter raw samples to MEASURE only
    if raw_flag and len(raw_flag) == len(raw_co2):
        m_co2 = [c for c, fl in zip(raw_co2, raw_flag) if fl != "calib"]
        m_t   = [t for t, fl in zip(raw_t,   raw_flag) if fl != "calib"]
        m_rh  = [r for r, fl in zip(raw_rh,  raw_flag) if fl != "calib"]
        m_cr  = [c for c, fl in zip(raw_co2raw,   raw_flag) if fl != "calib"] \
                if len(raw_co2raw) == len(raw_flag) else raw_co2raw
        m_cu  = [c for c, fl in zip(raw_co2rawuc, raw_flag) if fl != "calib"] \
                if len(raw_co2rawuc) == len(raw_flag) else raw_co2rawuc
        m_p   = [c for c, fl in zip(raw_p, raw_flag) if fl != "calib"] \
                if len(raw_p) == len(raw_flag) else raw_p
        m_fm  = [c for c, fl in zip(raw_fmass, raw_flag) if fl != "calib"] \
                if len(raw_fmass) == len(raw_flag) else raw_fmass
        m_fv  = [c for c, fl in zip(raw_fvol, raw_flag) if fl != "calib"] \
                if len(raw_fvol) == len(raw_flag) else raw_fvol
    else:
        # No flag list (legacy path): assume all measure
        m_co2, m_t, m_rh = raw_co2, raw_t, raw_rh
        m_cr, m_cu, m_p = raw_co2raw, raw_co2rawuc, raw_p
        m_fm, m_fv = raw_fmass, raw_fvol

    def _stats(values):
        clean = [v for v in values if v != MISSING]
        if not clean:
            return MISSING, MISSING, MISSING
        m = sum(clean) / len(clean)
        s = statistics.stdev(clean) if len(clean) > 1 else 0.0
        med = statistics.median(clean)
        return m, s, med

    M_c, S_c, Med_c = _stats(m_co2)
    M_t, S_t, Med_t = _stats(m_t)
    M_r, S_r, Med_r = _stats(m_rh)
    M_cr, S_cr, Med_cr = _stats(m_cr)
    M_cu, S_cu, Med_cu = _stats(m_cu)
    M_p,  S_p,  Med_p  = _stats(m_p)
    M_fm, S_fm, Med_fm = _stats(m_fm)
    M_fv, S_fv, Med_fv = _stats(m_fv)
    N = sum(1 for v in m_co2 if v != MISSING)
    # Sticky-calib: any minute or any sample = calib
    has_calib_minute = any(r["flag"] == "calib" for r in buf_minutes)
    has_calib_sample = bool(raw_flag) and any(fl == "calib" for fl in raw_flag)
    flag = "calib" if (has_calib_minute or has_calib_sample) else "measure"
    if valve_enabled and buf_minutes:
        # Mode over MEASURE minutes; fall back to all if no measure
        ref_buf = [r for r in buf_minutes if r["flag"] != "calib"] or buf_minutes
        vpos = Counter(r["valve_pos"]   for r in ref_buf).most_common(1)[0][0]
        vlab = Counter(r["valve_label"] for r in ref_buf).most_common(1)[0][0]
        valve_suf = f" {vpos} {vlab}"
    else:
        valve_suf = ""
    ts = slot_ts.strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a") as f:
        f.write(
            f"{ts} {M_c:.2f} {S_c:.2f} {Med_c:.2f} "
            f"{M_t:.2f} {S_t:.2f} {Med_t:.2f} "
            f"{M_r:.2f} {S_r:.2f} {Med_r:.2f} "
            f"{N} {flag}{valve_suf} "
            f"{M_cr:.2f} {S_cr:.2f} {Med_cr:.2f} "
            f"{M_cu:.2f} {S_cu:.2f} {Med_cu:.2f} "
            f"{M_p:.2f} {S_p:.2f} {Med_p:.2f} "
            f"{M_fm:.3f} {S_fm:.3f} {Med_fm:.3f} "
            f"{M_fv:.3f} {S_fv:.3f} {Med_fv:.3f}\n"
        )


def _make_minute_record(co2, co2_std, n_co2, t, t_std, rh, rh_std,
                        flag, valve_enabled, valve_status_file, valve_stale_s,
                        co2raw=MISSING, co2raw_std=MISSING,
                        co2rawuc=MISSING, co2rawuc_std=MISSING,
                        p=MISSING, p_std=MISSING,
                        fmass=MISSING, fmass_std=MISSING,
                        fvol=MISSING, fvol_std=MISSING):
    """Pack the just-closed minute aggregate into a dict for slot buffers.

    co2raw/co2rawuc (medie+std del minuto) servono a propagare i 3 valori CO2
    anche negli aggregati 10/30-min (pooled in _flush_slot).
    fmass/fvol = flusso TSI 4140 (massa SLPM / volumetrico Lpm), stesso pattern."""
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
        "co2raw": co2raw, "co2raw_std": co2raw_std,
        "co2rawuc": co2rawuc, "co2rawuc_std": co2rawuc_std,
        "p": p, "p_std": p_std,
        "fmass": fmass, "fmass_std": fmass_std,
        "fvol": fvol, "fvol_std": fvol_std,
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


def parse_three_co2(line):
    """Estrae i 3 valori CO2 emessi dalla GMP343 col FORM
    `CO2 " " CO2RAW " " CO2RAWUC` (riga tipo "  472.1   471.9   459.5").

    Ritorna (co2, co2raw, co2rawuc):
      - co2      = CO2 filtrata + compensata (P/T/RH/O2) → valore "corretto"
      - co2raw   = CO2 non filtrata, compensazioni ancora applicate
      - co2rawuc = CO2 non filtrata e SENZA compensazioni → valore "grezzo"

    Robusto alla transizione di formato: se la riga contiene un solo numero
    (vecchio FORM o sonda non riconfigurata) ritorna (co2, MISSING, MISSING)
    così il logger continua a funzionare; se il parsing fallisce del tutto
    ritorna (None, None, None).
    """
    try:
        nums = []
        for p in line.strip().split():
            tok = p.replace('.', '', 1).replace('-', '', 1)
            if tok.isdigit():
                nums.append(float(p))
        if not nums:
            return None, None, None
        co2      = nums[0]
        co2raw   = nums[1] if len(nums) >= 2 else MISSING
        co2rawuc = nums[2] if len(nums) >= 3 else MISSING
        return co2, co2raw, co2rawuc
    except Exception as e:
        print(f"Error parsing 3-CO2 line '{line}': {e}")
        return None, None, None


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

    # Config sensori + compensazione (legge sensors.ini, aggiorna SHT31_ADDR/BUS)
    sensors_cfg = load_sensors_config()
    comp_cfg = sensors_cfg["comp"]
    bmp_cfg  = sensors_cfg["bmp388"]
    tsi_cfg  = sensors_cfg["tsi"]
    tsi_enabled = bool(tsi_cfg["enabled"]) and _HAS_TSI
    if tsi_cfg["enabled"] and not _HAS_TSI:
        print("WARN: [tsi4140] enabled ma modulo tsi4140 non importabile → flusso non loggato")
    comp_enabled = bool(comp_cfg["enabled"]) and _HAS_COMP_MODULES
    if comp_cfg["enabled"] and not _HAS_COMP_MODULES:
        print("WARN: [compensation] enabled ma moduli bmp388/gmp343_compensation "
              "non importabili → resto in RUN-mode")
    if comp_enabled:
        print(f"[compensation] ATTIVA (poll-mode) — gmp343_addr={comp_cfg['addr']}, "
              f"feed P={comp_cfg['feed_pressure']} RH={comp_cfg['feed_humidity']}, "
              f"BMP388 bus {bmp_cfg['bus']} addr 0x{bmp_cfg['addr']:02x}")
    else:
        print("[compensation] disattiva (STANDBY) — RUN-mode, nessun feed P/RH")

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
    except serial.SerialException as e:
        print(f"Error opening serial port {device}: {e}")
        return

    sht31_bus = open_sht31_bus()

    # ── Flussimetro TSI 4140 (opzionale) ──────────────────────────────────────
    tsi_dev = None
    if tsi_enabled:
        try:
            tsi_dev = tsi4140.open_tsi(tsi_cfg["port"])
            print(f"[tsi4140] flussimetro aperto su {tsi_cfg['port']} "
                  f"(ping={tsi4140.ping(tsi_dev)}) — flusso di MASSA (SLPM)")
        except Exception as e:
            print(f"WARN: apertura TSI 4140 fallita ({e}) → flusso non loggato")
            tsi_dev = None

    # ── Setup modalità sonda ──────────────────────────────────────────────────
    # comp_enabled → POLL-mode (per poter inviare XP/XRH di compensazione).
    # Altrimenti → RUN-mode come da comportamento storico.
    bmp_dev = None
    if comp_enabled:
        bmp_dev = bmp388.open_bmp388(bus=bmp_cfg["bus"], addr=bmp_cfg["addr"])
        if not gmp343_comp.stop_run(ser, addr=comp_cfg["addr"]):
            print("WARN: impossibile fermare RUN-mode → fallback RUN-mode (no feed)")
            comp_enabled = False
            ser.write(CMD_START)
        else:
            # Pressione fissa (es. 1014 hPa) quando non c'è il BMP388: la sonda
            # la usa per la compensazione RH (richiesta dal manuale) e P.
            # Volatile (no SAVE) → ri-applicata ad ogni avvio, niente usura EEPROM.
            if comp_cfg["fixed_pressure_hpa"] > 0:
                gmp343_comp.set_pressure(ser, comp_cfg["fixed_pressure_hpa"], save=False)
                print(f"[compensation] pressione FISSA impostata a "
                      f"{comp_cfg['fixed_pressure_hpa']:.1f} hPa")
            gmp343_comp.enter_poll(ser)
            print(f"[compensation] sonda in POLL-mode (addr {comp_cfg['addr']})")
            # Pubblica lo stato compensazione per launcher/monitor
            _COMP_STATUS["comp_active"] = True
            if comp_cfg["feed_pressure"] and bmp_dev is not None:
                # P live dal BMP388 (aggiornata ad ogni ciclo nel loop)
                _COMP_STATUS["comp_p_source"] = "bmp388"
            elif comp_cfg["fixed_pressure_hpa"] > 0:
                _COMP_STATUS["comp_p_hpa"] = round(comp_cfg["fixed_pressure_hpa"], 1)
                _COMP_STATUS["comp_p_source"] = "fixed"
    else:
        # Recupero da eventuale POLL-mode residuo (sonda lasciata in POLL da
        # un'esecuzione comp precedente terminata senza teardown): in POLL la
        # sonda ignora R, quindi prima OPEN per riportarla a STOP, poi R.
        if _HAS_COMP_MODULES:
            ser.write(b"OPEN 0\r"); time.sleep(0.3); ser.reset_input_buffer()
        ser.write(CMD_START)

    _write_status_json(True)  # seriale aperta → strumento connesso
    _sd_notify("READY=1")     # segnala a systemd che il logger è pronto

    co2_values = []
    t_values   = []
    rh_values  = []
    co2raw_values   = []   # CO2RAW per-minuto (aggregati in coda)
    co2rawuc_values = []   # CO2RAWUC per-minuto
    p_values        = []   # P (BMP388) per-minuto
    fmass_values    = []   # flusso di MASSA (SLPM) per-minuto (TSI)
    fvol_values     = []   # flusso VOLUMETRICO (Lpm) per-minuto (TSI, calcolato)
    _last_fmass = None     # ultimo flusso massa per status.json (live)
    _last_fvol  = None     # ultimo flusso volumetrico per status.json (live)
    # Circuit-breaker TSI: se il flussimetro è muto, la lettura costa fino a
    # ~2s/ciclo (2 tentativi × timeout) e RALLENTEREBBE il campionamento CO2
    # (che è il MUST). Dopo TSI_FAIL_MAX letture fallite consecutive si sospende
    # la lettura per TSI_COOLDOWN_S secondi e si ri-sonda una volta.
    TSI_FAIL_MAX  = 5
    TSI_COOLDOWN_S = 60.0
    _tsi_fails = 0
    _tsi_skip_until = 0.0
    _last_co2 = None  # ultimo valore per status.json (CO2 corretta)
    _last_co2rawuc = None  # CO2 grezza (non compensata)
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
    # `buf_60_raw_flag` traccia il flag per-sample così possiamo escludere
    # i sample di calibrazione dalle statistiche atmosferiche.
    buf_60_raw_co2  = []
    buf_60_raw_t    = []
    buf_60_raw_rh   = []
    buf_60_raw_flag = []
    buf_60_raw_co2raw   = []   # CO2RAW per-sample (mediana 60-min)
    buf_60_raw_co2rawuc = []   # CO2RAWUC per-sample
    buf_60_raw_p        = []   # P per-sample (mediana 60-min)
    buf_60_raw_fmass    = []   # flusso massa per-sample (mediana 60-min)
    buf_60_raw_fvol     = []   # flusso volumetrico per-sample (mediana 60-min)

    print(f"Logging started. Raw: {raw_file}, Min: {avg_file}")
    print(f"Aggregates: 10/{file_10}  30/{file_30}  60/{file_60}")
    print(f"Serial: {device} @ {baudrate} bps; I2C bus {SHT31_BUS} addr 0x{SHT31_ADDR:02x}")

    def _files_for_day(day):
        """Restituisce le 5 path giornaliere per `day` e ne crea gli header
        se mancanti. Usato sia per la rotazione del giorno corrente sia
        per scrivere flush "in ritardo" sul file del giorno precedente
        (es. slot 23:00 chiuso a 00:00 del giorno dopo)."""
        files = get_filenames(config, day)
        write_headers_if_needed(*files, config=config,
                                valve_enabled=valve_enabled)
        return files

    def _on_minute_closed(min_record):
        """Hook called right after a 1-min record is written.

        Updates the 10/30/60-min slot buffers and flushes any buffer whose
        slot the just-closed minute has crossed. The 60-min flush also
        computes mean/std/median straight from the raw sample buffer.

        IMPORTANT: each flush writes to the file matching the SLOT'S DATE,
        not the current day's, so a slot that closes after midnight UTC
        (e.g. slot 23:00 → flushed at 00:00 the next day) ends up in the
        correct daily file.
        """
        nonlocal buf_10, slot_10, buf_30, slot_30, buf_60, slot_60
        nonlocal buf_60_raw_co2, buf_60_raw_t, buf_60_raw_rh, buf_60_raw_flag
        nonlocal buf_60_raw_co2raw, buf_60_raw_co2rawuc, buf_60_raw_p
        nonlocal buf_60_raw_fmass, buf_60_raw_fvol
        # 10-min
        this_slot10 = _slot_start(current_minute, 10)
        if this_slot10 != slot_10:
            slot_files = _files_for_day(slot_10)
            _flush_slot(slot_files[2], slot_10, buf_10, valve_enabled)
            buf_10 = []
            slot_10 = this_slot10
        buf_10.append(min_record)
        # 30-min
        this_slot30 = _slot_start(current_minute, 30)
        if this_slot30 != slot_30:
            slot_files = _files_for_day(slot_30)
            _flush_slot(slot_files[3], slot_30, buf_30, valve_enabled)
            buf_30 = []
            slot_30 = this_slot30
        buf_30.append(min_record)
        # 60-min: usa i RAW sample dell'ora per mean/std/median
        this_slot60 = _slot_start(current_minute, 60)
        if this_slot60 != slot_60:
            slot_files = _files_for_day(slot_60)
            _flush_60min_with_median(
                slot_files[4], slot_60, buf_60,
                buf_60_raw_co2, buf_60_raw_t, buf_60_raw_rh,
                buf_60_raw_flag, valve_enabled,
                raw_co2raw=buf_60_raw_co2raw,
                raw_co2rawuc=buf_60_raw_co2rawuc,
                raw_p=buf_60_raw_p,
                raw_fmass=buf_60_raw_fmass,
                raw_fvol=buf_60_raw_fvol)
            buf_60 = []
            buf_60_raw_co2  = []
            buf_60_raw_t    = []
            buf_60_raw_rh   = []
            buf_60_raw_flag = []
            buf_60_raw_co2raw   = []
            buf_60_raw_co2rawuc = []
            buf_60_raw_p        = []
            buf_60_raw_fmass    = []
            buf_60_raw_fvol     = []
            slot_60 = this_slot60
        buf_60.append(min_record)

    while True:
        # Heartbeat watchdog: se il loop si impianta (nessun ping entro
        # WatchdogSec) systemd killa+riavvia. Inviato a inizio ciclo, prima
        # di qualsiasi lettura che potrebbe bloccarsi.
        _sd_notify("WATCHDOG=1")
        try:
            # ── Acquisizione di un campione ───────────────────────────────────
            # comp_enabled → POLL-mode: leggi P (BMP388) e T/RH (SHT3X), inviali
            # alla sonda come compensazione (XP/XRH) e richiedi la misura (SEND).
            # _poll_tr porta T/RH a valle così non li si rilegge due volte.
            _poll_tr = None
            p_hpa = None        # P del BMP388 (solo in poll-mode); None → MISSING nel log
            if comp_enabled:
                p_hpa, _ = bmp388.read_bmp388(bmp_dev)
                pt, prh = read_sht31(sht31_bus)
                _poll_tr = (pt, prh)
                line = gmp343_comp.feed_and_send(
                    ser, comp_cfg["addr"],
                    p_hpa=(None if p_hpa is None else p_hpa),
                    rh_pct=(None if prh == MISSING else prh),
                    do_pressure=comp_cfg["feed_pressure"],
                    do_humidity=comp_cfg["feed_humidity"])
                # Stato per launcher/monitor: RH effettivamente inviata + P live
                if comp_cfg["feed_humidity"]:
                    _COMP_STATUS["comp_rh_fed"] = (
                        None if prh == MISSING else round(prh, 1))
                if comp_cfg["feed_pressure"] and p_hpa is not None:
                    _COMP_STATUS["comp_p_hpa"] = round(p_hpa, 1)
                time.sleep(comp_cfg["poll_interval_s"])
            else:
                line = ser.readline().decode(errors='ignore').strip()
            now = datetime.utcnow()

            new_files = get_filenames(config)
            if new_files != (raw_file, avg_file, file_10, file_30, file_60):
                raw_file, avg_file, file_10, file_30, file_60 = new_files
                write_headers_if_needed(raw_file, avg_file, file_10, file_30, file_60,
                                        config, valve_enabled)
                print(f"New day. Files rotated: {raw_file}, {avg_file}, ...")

            # ── Lettura flussimetro TSI 4140 (indipendente dal ciclo CO2) ──
            # flow_mass = flusso di MASSA (SLPM, misura nativa del meter);
            # flow_vol  = VOLUMETRICO (Lpm) calcolato con la P reale del BMP388
            #             (kPa = hPa/10). Senza P valida → volumetrico MISSING.
            flow_mass = MISSING
            flow_vol  = MISSING
            # Circuit-breaker: leggi solo se non in cooldown (protegge la
            # cadenza CO2 quando il TSI è muto/scollegato).
            if tsi_dev is not None and time.monotonic() >= _tsi_skip_until:
                fm, tg = tsi4140.read_flow_temp(tsi_dev)
                if fm == MISSING:
                    _tsi_fails += 1
                    if _tsi_fails >= TSI_FAIL_MAX:
                        _tsi_skip_until = time.monotonic() + TSI_COOLDOWN_S
                        _tsi_fails = 0
                        print(f"WARN: TSI muto ({TSI_FAIL_MAX} letture KO) → "
                              f"sospendo il flusso per {TSI_COOLDOWN_S:.0f}s",
                              flush=True)
                else:
                    _tsi_fails = 0
                    flow_mass = fm
                    # P per il volumetrico: BMP388 live se disponibile,
                    # altrimenti la pressione fissa di config (approssimata ma
                    # tiene il volumetrico visibile anche col BMP388 staccato).
                    p_for_vol = None
                    if p_hpa is not None:
                        p_for_vol = p_hpa
                    elif comp_cfg["fixed_pressure_hpa"] > 0:
                        p_for_vol = comp_cfg["fixed_pressure_hpa"]
                    if p_for_vol is not None:
                        flow_vol = tsi4140.to_volumetric(fm, tg, p_for_vol / 10.0)

            if line:
                ts_str, current_timestamp = timestamp_now()
                co2, co2raw, co2rawuc = parse_three_co2(line)
                p_log = p_hpa if p_hpa is not None else MISSING  # P BMP388 per il log

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
                        # In poll-mode T/RH sono già stati letti a inizio ciclo
                        # (e inviati alla sonda); in run-mode si leggono qui.
                        t, rh = _poll_tr if _poll_tr is not None else read_sht31(sht31_bus)
                        last_co2_value = co2
                        flag = _auto_flag(calib_auto, valve_enabled,
                                          valve_status_file, valve_stale_s,
                                          calib_labels, measure_position)

                        valve_suf_raw = _valve_suffix(valve_enabled, valve_status_file, valve_stale_s)
                        # Le 2 colonne CO2RAW/CO2RAWUC vanno IN CODA alla riga,
                        # dopo le eventuali colonne valvola, così i parser
                        # posizionali esistenti (monitor: CO2@2 T@3 RH@4 flag@5)
                        # restano validi e ignorano le colonne nuove.
                        with open(raw_file, 'a') as f_raw:
                            f_raw.write(
                                f"{ts_str} {co2:.2f} {t:.2f} {rh:.2f} "
                                f"{flag}{valve_suf_raw} "
                                f"{co2raw:.2f} {co2rawuc:.2f} {p_log:.2f} "
                                f"{flow_mass:.3f} {flow_vol:.3f}\n"
                            )

                    if current_timestamp.replace(second=0, microsecond=0) == current_minute:
                        # Stesso minuto: aggiorna i buffer (solo se non duplicato)
                        if not is_duplicate:
                            co2_values.append(co2)
                            t_values.append(t)
                            rh_values.append(rh)
                            co2raw_values.append(co2raw)
                            co2rawuc_values.append(co2rawuc)
                            p_values.append(p_log)
                            fmass_values.append(flow_mass)
                            fvol_values.append(flow_vol)
                            # Raw buffer per la mediana 60-min (un sample
                            # per chiamata, post-dedup). `flag` per-sample
                            # serve a escludere i sample di calibrazione
                            # dalle statistiche atmosferiche dell'ora.
                            buf_60_raw_co2.append(co2)
                            buf_60_raw_t.append(t)
                            buf_60_raw_rh.append(rh)
                            buf_60_raw_flag.append(flag)
                            buf_60_raw_co2raw.append(co2raw)
                            buf_60_raw_co2rawuc.append(co2rawuc)
                            buf_60_raw_p.append(p_log)
                            buf_60_raw_fmass.append(flow_mass)
                            buf_60_raw_fvol.append(flow_vol)
                    else:
                        # Cambio minuto: chiudi il minuto corrente e scrivi
                        # il record _min.raw, poi gli aggregati 10/30/60.
                        # IMPORTANTE: scrivi sul file del giorno del MINUTO
                        # CHIUSO (current_minute), non del giorno corrente:
                        # il record di 23:59 chiuso a 00:00 del giorno dopo
                        # deve restare nel _min.raw del giorno vecchio.
                        files_for_min = _files_for_day(current_minute)
                        with open(files_for_min[1], 'a') as f_avg:
                            ts_avg = current_minute.strftime("%Y-%m-%d %H:%M:%S")
                            if co2_values:
                                co2_avg = sum(co2_values) / len(co2_values)
                                co2_std = statistics.stdev(co2_values) if len(co2_values) > 1 else 0.0
                                n_co2   = len(co2_values)
                            else:
                                co2_avg, co2_std, n_co2 = MISSING, MISSING, 0
                            t_avg,  t_std  = mean_std_missing(t_values)
                            rh_avg, rh_std = mean_std_missing(rh_values)
                            cr_avg, cr_std = mean_std_missing(co2raw_values)
                            cu_avg, cu_std = mean_std_missing(co2rawuc_values)
                            p_avg,  p_std  = mean_std_missing(p_values)
                            fm_avg, fm_std = mean_std_missing(fmass_values)
                            fv_avg, fv_std = mean_std_missing(fvol_values)
                            flag = _auto_flag(calib_auto, valve_enabled,
                                              valve_status_file, valve_stale_s,
                                              calib_labels, measure_position)
                            valve_suf = _valve_suffix(valve_enabled, valve_status_file, valve_stale_s)
                            f_avg.write(
                                f"{ts_avg} {co2_avg:.2f} {co2_std:.2f} "
                                f"{t_avg:.2f} {t_std:.2f} "
                                f"{rh_avg:.2f} {rh_std:.2f} "
                                f"{n_co2} {flag}{valve_suf} "
                                f"{cr_avg:.2f} {cr_std:.2f} {cu_avg:.2f} {cu_std:.2f} "
                                f"{p_avg:.2f} {p_std:.2f} "
                                f"{fm_avg:.3f} {fm_std:.3f} {fv_avg:.3f} {fv_std:.3f}\n"
                            )
                        # Aggrega nei bucket 10/30/60 min
                        _on_minute_closed(_make_minute_record(
                            co2_avg, co2_std, n_co2,
                            t_avg, t_std, rh_avg, rh_std,
                            flag, valve_enabled, valve_status_file, valve_stale_s,
                            co2raw=cr_avg, co2raw_std=cr_std,
                            co2rawuc=cu_avg, co2rawuc_std=cu_std,
                            p=p_avg, p_std=p_std,
                            fmass=fm_avg, fmass_std=fm_std,
                            fvol=fv_avg, fvol_std=fv_std))

                        _last_co2 = co2_avg if co2_avg != MISSING else None
                        _last_co2rawuc = cu_avg if cu_avg != MISSING else None
                        _last_t   = t_avg   if t_avg   != MISSING else None
                        _last_rh  = rh_avg  if rh_avg  != MISSING else None
                        _last_fmass = fm_avg if fm_avg != MISSING else None
                        _last_fvol  = fv_avg if fv_avg != MISSING else None
                        _write_status_json(True, _last_co2, _last_t, _last_rh,
                                           last_co2rawuc=_last_co2rawuc,
                                           last_flow_mass=_last_fmass,
                                           last_flow_vol=_last_fvol)
                        current_minute = current_timestamp.replace(second=0, microsecond=0)
                        # Avvia il nuovo minuto: includi il campione corrente
                        # solo se non è un duplicato.
                        if is_duplicate:
                            co2_values = []
                            t_values   = []
                            rh_values  = []
                            co2raw_values   = []
                            co2rawuc_values = []
                            p_values        = []
                            fmass_values    = []
                            fvol_values     = []
                        else:
                            co2_values = [co2]
                            t_values   = [t]
                            rh_values  = [rh]
                            co2raw_values   = [co2raw]
                            co2rawuc_values = [co2rawuc]
                            p_values        = [p_log]
                            fmass_values    = [flow_mass]
                            fvol_values     = [flow_vol]
                            # Il sample corrente appartiene già al nuovo
                            # minuto: includilo anche nel raw buffer 60-min
                            # (se il flush 60 è stato appena eseguito,
                            # buf_60_raw_* è già stato azzerato in
                            # _on_minute_closed e questo è il primo sample
                            # del nuovo bucket; altrimenti si accumula).
                            buf_60_raw_co2.append(co2)
                            buf_60_raw_t.append(t)
                            buf_60_raw_rh.append(rh)
                            buf_60_raw_flag.append(flag)
                            buf_60_raw_co2raw.append(co2raw)
                            buf_60_raw_co2rawuc.append(co2rawuc)
                            buf_60_raw_p.append(p_log)
                            buf_60_raw_fmass.append(flow_mass)
                            buf_60_raw_fvol.append(flow_vol)
            else:
                if now.replace(second=0, microsecond=0) != current_minute:
                    files_for_min = _files_for_day(current_minute)
                    with open(files_for_min[1], 'a') as f_avg:
                        ts_avg = current_minute.strftime("%Y-%m-%d %H:%M:%S")
                        flag = _auto_flag(calib_auto, valve_enabled,
                                          valve_status_file, valve_stale_s,
                                          calib_labels, measure_position)
                        valve_suf = _valve_suffix(valve_enabled, valve_status_file, valve_stale_s)
                        f_avg.write(
                            f"{ts_avg} {MISSING:.2f} {MISSING:.2f} "
                            f"{MISSING:.2f} {MISSING:.2f} "
                            f"{MISSING:.2f} {MISSING:.2f} "
                            f"0 {flag}{valve_suf} "
                            f"{MISSING:.2f} {MISSING:.2f} {MISSING:.2f} {MISSING:.2f} "
                            f"{MISSING:.2f} {MISSING:.2f} "
                            f"{MISSING:.3f} {MISSING:.3f} {MISSING:.3f} {MISSING:.3f}\n"
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
                    co2raw_values   = []
                    co2rawuc_values = []
                    p_values        = []
                    fmass_values    = []
                    fvol_values     = []
        except serial.SerialException as e:
            print(f"Serial communication error: {e}. Retrying in 5 seconds...")
            _write_status_json(False)
            ser.close()
            time.sleep(5)
            try:
                ser.open()
                if comp_enabled:
                    # Ripristina POLL-mode dopo la riconnessione
                    if gmp343_comp.stop_run(ser, addr=comp_cfg["addr"]):
                        gmp343_comp.enter_poll(ser)
                    else:
                        ser.write(CMD_START)
                else:
                    ser.write(CMD_START)
                _write_status_json(True, _last_co2, _last_t, _last_rh,
                                   last_co2rawuc=_last_co2rawuc)
            except serial.SerialException as reopen_e:
                print(f"Unable to reopen serial port: {reopen_e}. Exiting.")
                _write_status_json(False)
                break
        except Exception as e:
            print(f"Unexpected error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    main()
