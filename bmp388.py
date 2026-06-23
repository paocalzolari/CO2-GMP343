#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bmp388.py
=========
Driver per il sensore di pressione barometrica Bosch **BMP388** via I2C
(smbus2, zero dipendenze esterne oltre smbus2 già usato dal logger).

Pensato per il sistema GMP343: la pressione letta qui viene inviata alla
sonda Vaisala GMP343 come valore di compensazione (comando poll-mode
`XP <addr> <hPa>`).

Riferimenti
-----------
- Bosch BMP388 Datasheet (BST-BMP388-DS001), reg. map cap. 4 e algoritmo
  di compensazione floating-point cap. 9.2 (identico alla BMP3 Sensor API
  Bosch, funzioni `compensate_temperature` / `compensate_pressure`).
- Indirizzo I2C: 0x77 (SDO=alto, default Adafruit) o 0x76 (SDO=basso).
- CHIP_ID atteso: 0x50 (BMP388). 0x60 = BMP390 (compatibile a registri).

Uso
---
    import bmp388
    dev = bmp388.open_bmp388(bus=1, addr=0x77)   # None se assente/errore
    if dev:
        p_hpa, t_c = bmp388.read_bmp388(dev)      # (None, None) su errore

Convenzioni
-----------
- Nessuna eccezione propagata al chiamante: errori I2C → ritorna None così
  il logger può scrivere la sentinella MISSING senza crashare.
- La camera di misura della GMP343 lavora a pressione ambiente: usiamo
  oversampling moderato + filtro IIR leggero, forced mode (una misura per
  chiamata), coerente con un ciclo di acquisizione ~ secondi.
"""

import time

try:
    import smbus2
    _HAS_SMBUS = True
except ImportError:
    _HAS_SMBUS = False

# ── Registri BMP388 ───────────────────────────────────────────────────────────
REG_CHIP_ID   = 0x00
REG_ERR       = 0x02
REG_STATUS    = 0x03
REG_DATA      = 0x04   # press_xlsb..temp_msb: 6 byte consecutivi (0x04..0x09)
REG_PWR_CTRL  = 0x1B
REG_OSR       = 0x1C
REG_ODR       = 0x1D
REG_CONFIG    = 0x1F
REG_CALIB     = 0x31   # 21 byte di coefficienti di trimming (0x31..0x45)
REG_CMD       = 0x7E

CHIP_ID_BMP388 = 0x50
CHIP_ID_BMP390 = 0x60
CMD_SOFTRESET  = 0xB6

# PWR_CTRL: press_en(bit0) | temp_en(bit1) | mode(bit4-5); 0x33 = forced+both en
# (mode 01/10 = forced). Usiamo forced: una conversione su richiesta.
_PWR_FORCED = 0x13      # press_en=1, temp_en=1, mode=01 (forced)
# OSR: osr_p (bit0-2) | osr_t (bit3-5). x8 press (011), x1 temp (000) = 0x03
_OSR_VALUE  = 0x03
# CONFIG: IIR filter coeff (bit1-3). coeff 1 (001<<1)=0x02 — filtro leggero.
_CFG_VALUE  = 0x02


def _s8(v):
    return v - 256 if v > 127 else v


def _s16(v):
    return v - 65536 if v > 32767 else v


class BMP388:
    """Stato di un BMP388 aperto: bus, indirizzo e coefficienti pre-scalati."""

    def __init__(self, bus, addr):
        self.bus = bus
        self.addr = addr
        self.par = {}        # coefficienti float pre-scalati
        self._read_calibration()

    def _read_calibration(self):
        """Legge e pre-scala i 21 byte di coefficienti NVM (datasheet 9.1)."""
        c = self.bus.read_i2c_block_data(self.addr, REG_CALIB, 21)
        nvm_t1  = c[1] << 8 | c[0]
        nvm_t2  = c[3] << 8 | c[2]
        nvm_t3  = _s8(c[4])
        nvm_p1  = _s16(c[6] << 8 | c[5])
        nvm_p2  = _s16(c[8] << 8 | c[7])
        nvm_p3  = _s8(c[9])
        nvm_p4  = _s8(c[10])
        nvm_p5  = c[12] << 8 | c[11]
        nvm_p6  = c[14] << 8 | c[13]
        nvm_p7  = _s8(c[15])
        nvm_p8  = _s8(c[16])
        nvm_p9  = _s16(c[18] << 8 | c[17])
        nvm_p10 = _s8(c[19])
        nvm_p11 = _s8(c[20])

        # Pre-scalatura floating-point (Bosch BMP3 API, divisori = potenze di 2)
        self.par = {
            "t1":  nvm_t1  / 2 ** -8,
            "t2":  nvm_t2  / 2 ** 30,
            "t3":  nvm_t3  / 2 ** 48,
            "p1": (nvm_p1 - 2 ** 14) / 2 ** 20,
            "p2": (nvm_p2 - 2 ** 14) / 2 ** 29,
            "p3":  nvm_p3  / 2 ** 32,
            "p4":  nvm_p4  / 2 ** 37,
            "p5":  nvm_p5  / 2 ** -3,
            "p6":  nvm_p6  / 2 ** 6,
            "p7":  nvm_p7  / 2 ** 8,
            "p8":  nvm_p8  / 2 ** 15,
            "p9":  nvm_p9  / 2 ** 48,
            "p10": nvm_p10 / 2 ** 48,
            "p11": nvm_p11 / 2 ** 65,
        }

    def _compensate_temperature(self, uncomp_temp):
        p = self.par
        d1 = uncomp_temp - p["t1"]
        d2 = d1 * p["t2"]
        return d2 + (d1 * d1) * p["t3"]   # t_lin (°C)

    def _compensate_pressure(self, uncomp_press, t_lin):
        p = self.par
        d1 = p["p6"] * t_lin
        d2 = p["p7"] * t_lin ** 2
        d3 = p["p8"] * t_lin ** 3
        out1 = p["p5"] + d1 + d2 + d3

        d1 = p["p2"] * t_lin
        d2 = p["p3"] * t_lin ** 2
        d3 = p["p4"] * t_lin ** 3
        out2 = uncomp_press * (p["p1"] + d1 + d2 + d3)

        d1 = uncomp_press ** 2
        d2 = p["p9"] + p["p10"] * t_lin
        d3 = d1 * d2
        d4 = d3 + uncomp_press ** 3 * p["p11"]
        return out1 + out2 + d4            # Pa

    def read(self):
        """Una misura forced-mode. Ritorna (pressione_hPa, temperatura_C)."""
        # Trigger forced conversion
        self.bus.write_byte_data(self.addr, REG_PWR_CTRL, _PWR_FORCED)
        # Tempo conversione: x8 press + x1 temp ≈ < 15 ms; 25 ms conservativo
        time.sleep(0.025)
        d = self.bus.read_i2c_block_data(self.addr, REG_DATA, 6)
        raw_press = d[2] << 16 | d[1] << 8 | d[0]
        raw_temp  = d[5] << 16 | d[4] << 8 | d[3]
        t_lin = self._compensate_temperature(raw_temp)
        press_pa = self._compensate_pressure(raw_press, t_lin)
        return press_pa / 100.0, t_lin


def open_bmp388(bus=1, addr=0x77):
    """Apre il BMP388 su /dev/i2c-<bus> all'indirizzo dato.

    Ritorna un oggetto BMP388 pronto all'uso, oppure None se smbus2 manca,
    il sensore non risponde o il CHIP_ID non è quello atteso.
    """
    if not _HAS_SMBUS:
        print("WARN: libreria smbus2 non installata → BMP388 disabilitato")
        return None
    try:
        b = smbus2.SMBus(bus)
        chip_id = b.read_byte_data(addr, REG_CHIP_ID)
        if chip_id not in (CHIP_ID_BMP388, CHIP_ID_BMP390):
            print(f"WARN: BMP388 CHIP_ID inatteso 0x{chip_id:02x} "
                  f"(atteso 0x50/0x60) su 0x{addr:02x}")
            return None
        # Soft reset + attesa boot
        b.write_byte_data(addr, REG_CMD, CMD_SOFTRESET)
        time.sleep(0.01)
        # Configurazione oversampling / IIR / ODR
        b.write_byte_data(addr, REG_OSR, _OSR_VALUE)
        b.write_byte_data(addr, REG_CONFIG, _CFG_VALUE)
        dev = BMP388(b, addr)
        model = "BMP390" if chip_id == CHIP_ID_BMP390 else "BMP388"
        print(f"{model} ok su bus {bus} addr 0x{addr:02x}")
        return dev
    except Exception as e:
        print(f"WARN: BMP388 non raggiungibile ({e}) → P sarà MISSING")
        return None


def read_bmp388(dev):
    """Wrapper sicuro: (pressione_hPa, temperatura_C) oppure (None, None)."""
    if dev is None:
        return None, None
    try:
        return dev.read()
    except Exception as e:
        print(f"WARN: read_bmp388 fallita: {e}")
        return None, None


if __name__ == "__main__":
    # Test rapido da terminale: python3 bmp388.py [addr_hex]
    import sys
    a = int(sys.argv[1], 16) if len(sys.argv) > 1 else 0x77
    d = open_bmp388(bus=1, addr=a)
    if d:
        for _ in range(5):
            p, t = read_bmp388(d)
            if p is not None:
                print(f"P = {p:8.2f} hPa   T = {t:6.2f} °C")
            time.sleep(1)
