<!-- SYNC-PAIR: doc/SISTEMA_CO2_GMP343.md (IT) — keep aligned (bilingual-docs policy) -->
# CO₂ acquisition system — Vaisala GMP343 (Bologna shelter)

> System document. Station: Pi `misura@100.80.169.95` (Raspberry Pi 5),
> operational target **ISAC-CNR Bologna shelter**.
> Dev branch: `feat-bmp388-sht3x-poll` (repo `CO2-GMP343`, remotes `origin`+`nas`).
> Last update: 2026-06-24.

---

## 1. Overview

CO₂ measurement with the **Vaisala GMP343** NDIR probe (CARBOCAP), with
**real-time internal compensation** fed by external sensors:

- **Pressure (P)** from **Bosch BMP388** (I2C 0x77)
- **Relative humidity (RH)** from **Sensirion SHT3X** (I2C 0x44)
- **Temperature (T)**: measured **internally** by the probe (chamber sensor) —
  NOT fed from outside.

The probe outputs **3 simultaneous CO₂ values** (see §4); the backend logs
CO₂ + the 3 values + T/RH/P to `.raw` and 1/10/30/60-min aggregate files.
PyQt5 monitoring GUI + yad launcher. Calibration via VICI multiposition valve
(valve-scheduler).

Architecture: **headless backend** (`co2-logger.service`, systemd) + **separate
GUI** (monitor, closeable without stopping acquisition) + **valve-daemon**
(VICI control over a socket).

---

## 2. Hardware and wiring (Raspberry Pi 5)

Both sensors are **I2C on bus 1**, paralleled on SDA/SCL, distinguished by
address. ⚠️ **Power at 3.3 V** (BMP388 absolute max is 3.6 V: never 5 V).

| Sensor | VCC | GND | SDA | SCL | Addr |
|---|---|---|---|---|---|
| SHT3X (T/RH) | Pin 1 (3.3V) | Pin 6 | Pin 3 (GPIO2/SDA1) | Pin 5 (GPIO3/SCL1) | 0x44 |
| BMP388 (P) | Pin 17 (3.3V) | Pin 9 | Pin 3 *(shared)* | Pin 5 *(shared)* | 0x77 |

SDA (pin 3) and SCL (pin 5) are **unique** on the bus: the two sensors' wires
must be joined (breadboard/splitter/solder). Power/GND on different pins to avoid
splitting. Check: `i2cdetect -y 1` → must show **0x44** and **0x77**.

GMP343: serial **RS-232, /dev/gmp343, 19200 8N1**. VICI: /dev/vici, RS-232 9600.

---

## 3. Live compensation (poll-mode)

To feed P/RH live the probe must be in **POLL-mode** (in RUN it ignores all but
`S`). Field-verified protocol (see also the `instrument-driver` skill):

- Command terminator = **CR-only** (`\r`), NOT `\r\n`.
- `CLOSE` STOP→POLL · `OPEN <addr>` POLL→STOP · `SEND <addr>` = one measurement.
- `XP <addr> <hPa>` sets P (volatile, no reply) · `XRH <addr> <%>` sets RH.
- Probe address = **0**. Do **not** send a bare `ADDR` (enters interactive edit
  mode and blocks the parser).
- **POLL recovery**: a process killed while in POLL leaves the probe in POLL →
  `S`/`R` ignored. `stop_run()` sends **`OPEN <addr>` first** to recover.

Config in `config/sensors.ini`:

```ini
[sht31_a]      # T/RH
enabled = true
addr = 0x44

[bmp388]       # P
enabled = true
addr = 0x77

[compensation]
enabled = true            # true=POLL-mode with live feed; false=RUN-mode (no feed)
gmp343_addr = 0
feed_pressure = true      # live P from BMP388
feed_humidity = true      # live RH from SHT3X
fixed_pressure_hpa = 1014.0   # fallback if BMP388 absent
poll_interval_s = 2.0
```

⚠️ **Metrological caveat**: the "correct" compensation pressure is the gas
pressure **in the cell/flow**, not ambient. The in-line BMP388 is a good
approximation; the downstream capillary has dP ~0.7–3.7 hPa (<0.4 %, ~1–2 ppm).
See `scheda_capillare_dP_GMP343.pdf`.

---

## 4. Data file format (v6, since 2026-06-24)

The probe is configured (`FORM` command, saved to EEPROM) to output 3 CO₂ values:
`FORM CO2 " " CO2RAW " " CO2RAWUC`

- **CO2** = filtered + compensated (P/T/RH/O₂) + linearized → **corrected** value
- **CO2RAW** = unfiltered, compensations still applied
- **CO2RAWUC** = unfiltered and **without** compensation → **raw** (for re-corrections)

Columns appended **AT THE END** (after valve) to preserve positional parsers
(corrected CO2 stays in column 3):

| File | Trailing data header |
|---|---|
| `.raw` (per-sample) | `… CO2RAW[PPM] CO2RAWUC[PPM] P[hPa]` |
| `_min` / `_10min` / `_30min` | `… CO2RAW CO2RAW_std CO2RAWUC CO2RAWUC_std P P_std` |
| `_60min` | `… (with medians) … CO2RAW_median … P P_std P_median` |

Sentinels: `MISSING = -999.99`. Filename: `carbocap343_ISACBO_YYYYMMDD_p00*.raw`.
Example `_min` row:
`2026-06-24 10:28:00 497.93 0.85 24.80 0.02 65.14 0.04 3 measure 1 pos1-ambient 499.03 1.72 488.20 1.37 1012.95 0.01`

> Format change: when not in production, rename the day's files to `.old` and
> restart clean (consistent header). The monitor detects the 60-min format **by
> filename** (`_60min.`), not by column count.

---

## 5. GUI (text always in ENGLISH)

**Monitor** (`gmp343_sht31_monitor.py`):
- **Current Data** — LIVE columns (from `.raw`) and 1-MIN AVERAGE (from `_min`):
  corrected CO₂, T, RH, **P**. Group titles: "CO₂ corrected (compensated)".
- **Daily Statistics** — min/max/mean of corrected CO₂.
- **Chart** — 3 stacked panels with a synchronized time axis (`sharex`):
  CO₂ corrected (top) · **P [hPa]** (middle) · T/RH (bottom).
- Chart bar: `CO₂ corrected … · raw … · Compensation: RH …→probe · P … (BMP388/fixed)`.
- **VICI Valve** tab: daemon status, position, schedule (Start/Stop + countdown).

**Launcher** (`bin/co2-launcher.sh`, yad): backend/monitor status, last CO₂
(corrected/raw), Compensation line. Refreshes on Apply/Refresh.

> Rule: **all UI text in English** (CLAUDE.md). Chat/comments stay IT.

---

## 6. Calibration — valve-scheduler (VICI, 10 positions)

`valve-daemon.service` drives the valve over a socket. Schedule = CSV
`position,minutes,label` in `~/programs/valve-scheduler/schedule/`:
- `schedule-1.csv` → full 10-position cal (zero/span-low/mid/high…)
- `schedule-cylinder.csv` → ambient + CO₂ cylinder
- `schedule-2.csv`, `schedule-semplice.csv` → 2 steps

State in `service/valve_status.json` (`state`, `step_index`, `seconds_remaining`…).
The daemon does **not** replace a running schedule: to change it, **STOP** then
START. The monitor's VICI Valve tab shows countdown + Start/Stop by reading
`valve_status.json` (`on_status`).

Opt-in integration: with `integration.ini` the logger annotates `valve_pos
valve_label` on each row and can mark `flag=calib` automatically.

---

## 7. Operations

```bash
# Backend status / restart (acquisition)
systemctl is-active co2-logger.service
sudo systemctl restart co2-logger.service     # also recovers POLL via OPEN-first

# GUI (monitor + launcher) — clean restart (file script so pkill won't kill ssh)
bash /tmp/restart_guis.sh

# Enable the BMP388 (live P): wire → i2cdetect -y 1 → in sensors.ini
#   [bmp388] enabled=true , [compensation] feed_pressure=true → restart logger
# Verify: status.json → comp_p_source="bmp388", comp_p_hpa ~ ambient

# Datagram (file format) change: rename the day's files
for f in ~/data/*/carbocap343_*_$(date -u +%Y%m%d)_*.raw; do mv "$f" "$f.old"; done
sudo systemctl restart co2-logger.service     # recreate files with new header
```

`status.json` (`shared/ipc_co2/`): `last_co2_ppm` (corrected),
`last_co2rawuc_ppm` (raw), `last_t_c`, `last_rh_pct`, `comp_active`,
`comp_rh_fed`, `comp_p_hpa`, `comp_p_source` (bmp388|fixed).

---

## 8. Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| Pi **powers off** when connecting the BMP388 | NOT consumption (µA). Near-short: reversed wiring at the Pi end or faulty board. Measure BMP388 current alone, off the Pi (kΩ/µA = ok). Connect with Pi off. 3.3 V, never 5 V. Genuine 5A PD supply (verified). |
| I2C bus **empty** (`i2cdetect`) | Wiring: SDA/SCL swapped or shared lines not joined; no power. |
| Logger doesn't read T/RH after connecting the sensor | The logger opens the bus only at startup → `sudo systemctl restart co2-logger.service`. |
| `_min` shows **n=0 / no data** after restart | Probe left in POLL (process killed without teardown) → ignores S/R. Fix: `stop_run` sends `OPEN <addr>` first (in code). |
| Probe commands ignored / "ERROR: Expecting integer" | `\r\n` terminator instead of `\r`; or bare `ADDR` (interactive prompt) → send empty `\r`. |
| Compensated CO₂ wrong | Volatile `XP` stuck at an old value → restore `P <default>`. |
| GUI **very slow** (tab switch, RustDesk) | tracemalloc left active at high depth → normal mode; depth ≤4. |
| VICI Valve tab: countdown frozen, "Start" always on | Monitor wasn't feeding state to `TabSchedule.on_status()` (minimal IPC get_status) → now reads `valve_status.json` (fix `26ce125`). |

---

## 9. Main files (`~/programs/CO2/`)

| File | Role |
|---|---|
| `gmp343_sht31_logger.py` | Backend: serial CO2 + SHT3X/BMP388 I2C + compensation feed + file writing |
| `gmp343_compensation.py` | Poll-mode helper (stop_run/enter_poll/feed_and_send/exit_poll/set_pressure) |
| `bmp388.py` | BMP388 driver (smbus2, Bosch float compensation) |
| `gmp343_sht31_monitor.py` | PyQt5 monitor GUI (Current Data, Daily Stats, 3-panel chart, VICI tab) |
| `bin/co2-launcher.sh` | yad launcher |
| `config/sensors.ini` | SHT3X / BMP388 / compensation config |
| `config/serial.ini`, `name.ini`, `site.ini`, `integration.ini` | serial/site/valve config |
| `doc/GMP343_User_Guide_M210514EN-C.pdf` | Official probe manual |
| `shared/ipc_co2/status.json` | IPC state for launcher/monitor |

---

## 10. Commit history (branch `feat-bmp388-sht3x-poll`)

| Commit | Content |
|---|---|
| `184c7cc` | Poll-mode P/RH feed built (standby) + manual + BMP388 driver |
| `9fa9d82` | 3 CO2 in aggregates + RH/P feed + POLL recovery + English GUI |
| `4cad7fd` | BMP388 activation (live P feed, `comp_p_source=bmp388`) |
| `97867b9` | P logged in raw+aggregates (v6) + shown in Current Data |
| `26ce125` | Fix VICI Valve tab: countdown + Start/Stop synced |
| `9a44b40` | P panel in the chart (between CO2 and T/RH, shared time axis) |

---

## 11. References

- Manual: **GMP343 User's Guide M210514EN-C** (`doc/`, also on NotebookLM ACQ-Instruments)
- Datasheets: BMP388 (BST-BMP388-DS001), SHT3x-DIS
- Skills: `instrument-driver` (§ Vaisala GMP343), `co2-status`, `acq-graph-guard` (Bug 6),
  `cal-preflight`, `nox-cal-t200up-146i`
- Memories: `project_co2_gmp343_3co2_session`, `reference_gmp343_serial_protocol`,
  `feedback_gui_always_english`
- Diary: `~/obsidian-acq/50-Diario/2026/2026-06-23.md`, `2026-06-24.md`
