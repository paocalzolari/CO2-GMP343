#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gmp343_compensation.py
======================
Invio live a una sonda **Vaisala GMP343** dei valori di **pressione (P)** e
**umidità relativa (RH)** misurati da sensori esterni (BMP388 / SHT3X) per la
**compensazione interna** della sonda, tramite la **POLL-mode** seriale.

Perché poll-mode: in RUN-mode la GMP343 ignora ogni comando tranne `S`, quindi
non si possono inviare i valori di compensazione mentre streamma. In poll-mode
si alimentano `XP`/`XRH` (volatili, senza risposta) e si legge una misura con
`SEND <addr>` (formato definito da FORM, qui 3 valori CO2).

Protocollo verificato sul campo (firmware sonda CMN/BO, 2026-06-23):
  - Terminatore comandi = **CR-only** (`\\r`). Con `\\r\\n` i comandi vengono ignorati.
  - Indirizzo sonda = **0** (vedi comando ADDR — da NON inviare "nudo": entra in
    modalità modifica interattiva e blocca il parser).
  - `S`        → ferma RUN-mode (a volte serve ripeterlo: usare stop_run()).
  - `CLOSE`    → STOP → POLL ("line closed").
  - `XP a p`   → imposta pressione di compensazione (hPa, volatile, NO reply).
  - `XRH a r`  → imposta RH di compensazione (%RH, volatile, NO reply).
  - `SEND a`   → ritorna una misura (3 valori: CO2 CO2RAW CO2RAWUC).
  - `OPEN a`   → POLL → STOP ("line opened for operator commands").
  - `R`        → riavvia RUN-mode.

ATTENZIONE — i valori `XP`/`XRH` sono **volatili**: restano impostati finché non
sovrascritti o finché la sonda non viene resettata (che ripristina il valore
salvato con P/SAVE). In teardown conviene riportare la pressione al default con
restore_pressure() per non lasciare la compensazione "appesa" a un valore vecchio.

Uso tipico nel logger (poll-mode):
    import gmp343_compensation as comp
    comp.stop_run(ser); comp.enter_poll(ser)        # una volta, all'avvio
    ...
    line = comp.feed_and_send(ser, addr, p_hpa, rh_pct)   # ogni ciclo
    co2, co2raw, co2rawuc = parse_three_co2(line)
    ...
    comp.exit_poll(ser, addr)                       # allo spegnimento
"""

import time

# Range accettati dalla sonda (manuale M210514EN-C)
P_MIN, P_MAX   = 700.0, 1300.0      # hPa
RH_MIN, RH_MAX = 0.0, 100.0         # %RH
DEFAULT_P_HPA  = 1013.0             # default di fabbrica della sonda


def _drain(ser, secs):
    """Legge tutto per `secs` s, ritorna la stringa accumulata."""
    end = time.monotonic() + secs
    buf = b""
    while time.monotonic() < end:
        n = ser.in_waiting
        if n:
            buf += ser.read(n)
        else:
            time.sleep(0.03)
    return buf.decode(errors="ignore")


def _send(ser, cmd, wait=0.6):
    """Invia un comando con terminatore CR-only e ritorna la risposta."""
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode())
    return _drain(ser, wait)


def stop_run(ser, tries=6, addr=0):
    """Ferma il RUN-mode in modo robusto (S ripetuto + verifica via FORM).

    Ritorna True se confermato STOP-mode. Necessario perché un singolo `S`
    a volte non aggancia il boundary di riga dello streaming.

    IMPORTANTE: prima invia OPEN <addr>. Se la sonda è stata lasciata in
    POLL-mode (es. processo comp precedente ucciso senza teardown), in POLL
    essa IGNORA sia `S` che `R`: solo OPEN/SEND/X* funzionano. OPEN la riporta
    a STOP da POLL (ed è innocuo in STOP/RUN). Senza questo, stop_run e il
    fallback RUN non recuperano mai → nessun dato.
    """
    ser.reset_input_buffer()
    ser.write(f"OPEN {addr}\r".encode())
    time.sleep(0.3)
    ser.write(b"\r")
    _drain(ser, 0.8)
    for _ in range(tries):
        ser.reset_input_buffer()
        ser.write(b"S\r")
        time.sleep(0.4)
        ser.write(b"S\r")
        _drain(ser, 1.0)
        resp = _send(ser, "FORM", 1.1)
        if any(c.isalpha() for c in resp) or ">" in resp:
            return True
    return False


def enter_poll(ser):
    """STOP → POLL (comando CLOSE). Ritorna True se sembra riuscito."""
    resp = _send(ser, "CLOSE", 1.2)
    return "clos" in resp.lower() or ">" not in resp


def exit_poll(ser, addr=0):
    """POLL → STOP (comando OPEN <addr>)."""
    return _send(ser, f"OPEN {addr}", 1.2)


def feed_pressure(ser, addr, p_hpa):
    """Invia XP (pressione) se nel range valido. Nessuna risposta attesa."""
    if p_hpa is None or not (P_MIN <= p_hpa <= P_MAX):
        return False
    ser.write(f"XP {addr} {p_hpa:.1f}\r".encode())
    time.sleep(0.05)
    return True


def feed_humidity(ser, addr, rh_pct):
    """Invia XRH (umidità) se nel range valido. Nessuna risposta attesa."""
    if rh_pct is None or not (RH_MIN <= rh_pct <= RH_MAX):
        return False
    ser.write(f"XRH {addr} {rh_pct:.1f}\r".encode())
    time.sleep(0.05)
    return True


def feed_and_send(ser, addr, p_hpa=None, rh_pct=None,
                  do_pressure=True, do_humidity=True, wait=0.8):
    """Alimenta P/RH (se forniti e validi) e richiede una misura con SEND.

    Ritorna la riga di risposta (es. "  460.2   459.6   447.5") oppure ""
    se non arriva nulla. I valori fuori range vengono semplicemente saltati
    (la sonda userebbe comunque l'ultimo valore valido).
    """
    if do_pressure:
        feed_pressure(ser, addr, p_hpa)
    if do_humidity:
        feed_humidity(ser, addr, rh_pct)
    return _send(ser, f"SEND {addr}", wait).strip()


def set_pressure(ser, p_hpa, save=False):
    """In STOP-mode, imposta la pressione di lavoro (comando 'P', range 700-1300).

    Usata per la **pressione fissa** quando non c'è un sensore di pressione:
    la GMP343 la usa sia per la compensazione P (se PC ON) sia per quella di
    umidità (RHC la richiede comunque). save=True persiste in EEPROM (sconsigliato
    se chiamata ad ogni avvio: usura). save=False = volatile, va ri-applicata
    ad ogni avvio (ciò che fa il logger).
    """
    if p_hpa is None or not (P_MIN <= p_hpa <= P_MAX):
        return ""
    out = _send(ser, f"P {p_hpa:.1f}", 1.0)
    if save:
        out += _send(ser, "SAVE", 2.0)
    return out


def restore_pressure(ser, p_hpa=DEFAULT_P_HPA):
    """In STOP-mode, riporta la pressione di lavoro al default (senza SAVE).

    Da chiamare in teardown dopo exit_poll(), così la compensazione non resta
    agganciata all'ultimo valore inviato in poll-mode.
    """
    return set_pressure(ser, p_hpa, save=False)


if __name__ == "__main__":
    # Self-test contro la sonda (richiede che il logger NON occupi la porta).
    import serial, sys
    addr = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    s = serial.Serial("/dev/gmp343", 19200, bytesize=8, parity="N",
                      stopbits=1, timeout=0.2)
    time.sleep(1)
    print("stop_run:", stop_run(s))
    print("enter_poll:", enter_poll(s).strip() if isinstance(enter_poll(s), str) else enter_poll(s))
    print("SEND P=980 RH=55:", feed_and_send(s, addr, 980.0, 55.0))
    print("SEND P=700 RH=80:", feed_and_send(s, addr, 700.0, 80.0))
    print("exit_poll:", exit_poll(s, addr).strip())
    print("restore_pressure:", restore_pressure(s).strip())
    s.write(b"R\r"); time.sleep(1.5)
    print("stream:", _drain(s, 2.0).strip())
    s.close()
