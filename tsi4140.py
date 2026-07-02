#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tsi4140.py — Driver minimale (stdlib + pyserial) per il flussimetro di massa
TSI serie 4000/4100 (modello 4140) collegato via seriale RS-232.

Protocollo (TSI 4000/4100 Design Guide, Appendix C — fonte primaria):
  - Seriale FISSA: 38400 baud, 8 data bit, no parity, 1 stop bit, no flow control.
  - Comandi ASCII case-sensitive, terminati da CR (0x0d). LF ignorato.
  - Ack: "OK"<CR><LF>; errore: "ERRn"<CR><LF>.
  - Lettura Flow/Temp/Pressure: DmFTPnnnn
      D = data transfer
      m = formato: A=ASCII comma, B=binary, C=ASCII <CR>-delimited
      F/T/P = richiede Flow / Temp / Pressure-setting ('x' per escludere)
      nnnn  = n. campioni 1..1000, 4 cifre con zeri iniziali
  - Unità flusso (SUn): S = Standard L/min (MASSA), V = Volumetric L/min.
    Questo meter è impostato in Standard/massa (SUS + SAVE): il flusso letto è
    di MASSA. Il volumetrico va calcolato (vedi to_volumetric()).
  - T (DCFT...) = temperatura del gas nel tubo di flusso, in °C (misura reale).

Nota pratica: dopo l'apertura della porta il PRIMO comando può ritornare un
ERR spurio (buffer sporco). Il driver fa drain + un retry.
"""

import time

try:
    import serial
except Exception:                       # pragma: no cover
    serial = None

MISSING = -999.99

# Costanti condizioni standard TSI (Appendix B): Tstd=21.11°C, Pstd=101.3 kPa
T_STD_K = 21.11 + 273.15                # 294.26 K
P_STD_KPA = 101.3

BAUD = 38400


def open_tsi(port="/dev/tsi4140", timeout=1.0):
    """Apre la porta seriale del TSI (38400 8N1) e svuota il buffer.
    Ritorna l'oggetto serial.Serial (aperto) o solleva l'eccezione."""
    if serial is None:
        raise RuntimeError("pyserial non disponibile")
    ser = serial.Serial(port, BAUD, bytesize=8, parity="N", stopbits=1,
                         timeout=timeout, write_timeout=timeout)
    time.sleep(0.2)
    try:
        ser.reset_input_buffer()
        ser.read(2000)                  # drain di eventuale streaming pendente
    except Exception:
        pass
    return ser


def _cmd(ser, cmd_bytes, max_lines=3, deadline_s=1.0):
    """Invia un comando (aggiunge CR) e legge la risposta riga-per-riga fino al
    terminatore, SENZA sleep fissi: readline ritorna appena arriva <LF> (~ms),
    quindi la latenza è minima (importante per non caricare la CPU nel loop).
    Ritorna i byte grezzi concatenati (max `max_lines` righe o `deadline_s`)."""
    ser.reset_input_buffer()
    ser.write(cmd_bytes + b"\r")
    out = b""
    end = time.monotonic() + deadline_s
    for _ in range(max_lines):
        if time.monotonic() >= end:
            break
        ln = ser.readline()          # ritorna appena c'è \n (o al timeout porta)
        if not ln:
            break
        out += ln
        # una riga dati dopo "OK" è sufficiente
        if b"OK" in out and (b"," in out or out.count(b"\n") >= 2):
            break
        if b"ERR" in out:
            break
    return out


def ping(ser):
    """Comando '?': True se il meter risponde OK."""
    try:
        return b"OK" in _cmd(ser, b"?", max_lines=2, deadline_s=0.8)
    except Exception:
        return False


def read_flow_temp(ser):
    """Legge flusso (di MASSA, SLPM) e temperatura gas (°C) con DCFTx0001.

    Ritorna (flow_slpm, t_gas_c). In caso di errore/lettura non valida ritorna
    (MISSING, MISSING). Fa un retry se la prima risposta è ERR/vuota (buffer
    sporco al primo comando dopo l'apertura).
    """
    for attempt in (1, 2):
        try:
            raw = _cmd(ser, b"DCFTx0001", max_lines=3, deadline_s=1.0)
        except Exception:
            return (MISSING, MISSING)
        txt = raw.decode("ascii", "replace").strip()
        # Attesa: "OK\r\n<flow>,<temp>"  (Mode C: più parametri → comma)
        if "OK" in txt and "," in txt:
            data = txt.split("OK", 1)[1].strip()
            # può contenere \r\n multipli; prendi la prima riga con la virgola
            for line in data.replace("\r", "\n").split("\n"):
                line = line.strip()
                if "," in line:
                    parts = line.split(",")
                    try:
                        flow = float(parts[0])
                        tgas = float(parts[1]) if len(parts) > 1 else MISSING
                        return (flow, tgas)
                    except ValueError:
                        break
        # ERR o formato inatteso → svuota e ritenta una volta
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        time.sleep(0.2)
    return (MISSING, MISSING)


def to_volumetric(flow_mass_slpm, t_gas_c, p_kpa):
    """Converte il flusso di MASSA (Standard L/min) in VOLUMETRICO (L/min)
    alle condizioni reali, con la formula del manuale (Appendix B/SUn):

        Q_vol = Q_std * (T_gas / T_std) * (P_std / P)

    T_gas in °C, P in kPa (usa la P reale del BMP388 per accuratezza).
    Ritorna MISSING se un input non è valido.
    """
    try:
        if (flow_mass_slpm is None or flow_mass_slpm == MISSING
                or t_gas_c is None or t_gas_c == MISSING
                or p_kpa is None or p_kpa <= 0):
            return MISSING
        t_k = t_gas_c + 273.15
        return flow_mass_slpm * (t_k / T_STD_K) * (P_STD_KPA / p_kpa)
    except Exception:
        return MISSING


if __name__ == "__main__":
    import sys
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/tsi4140"
    s = open_tsi(port)
    print("ping:", ping(s))
    fm, tg = read_flow_temp(s)
    print(f"flow_mass={fm} SLPM  t_gas={tg} °C")
    # esempio volumetrico a 99.8 kPa
    print("flow_vol@99.8kPa:", to_volumetric(fm, tg, 99.8))
    s.close()
