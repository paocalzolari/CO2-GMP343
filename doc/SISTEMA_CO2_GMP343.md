<!-- SYNC-PAIR: doc/CO2_GMP343_SYSTEM.md (EN) — tenere allineati (policy bilingual-docs) -->
# Sistema di acquisizione CO₂ — Vaisala GMP343 (shelter Bologna)

> Documento di sistema. Stazione: Pi `misura@100.80.169.95` (Raspberry Pi 5),
> destinazione operativa **shelter ISAC-CNR Bologna**.
> Branch di sviluppo: `feat-bmp388-sht3x-poll` (repo `CO2-GMP343`, remote `origin`+`nas`).
> Ultimo aggiornamento: 2026-06-24.

---

## 1. Panoramica

Sistema di misura della **CO₂** con sonda NDIR **Vaisala GMP343** (CARBOCAP),
con **compensazione interna in tempo reale** alimentata da sensori esterni:

- **Pressione (P)** da **Bosch BMP388** (I2C 0x77)
- **Umidità relativa (RH)** da **Sensirion SHT3X** (I2C 0x44)
- **Temperatura (T)**: misurata **internamente** dalla sonda (sensore di camera) —
  NON si invia dall'esterno.

La sonda emette **3 valori di CO₂** simultanei (vedi §4) e il backend logga
CO₂ + i 3 valori + T/RH/P in file `.raw` e aggregati 1/10/30/60-min.
GUI di monitoraggio (PyQt5) + launcher (yad). Calibrazione via valvola
multiposizione VICI (valve-scheduler).

Architettura: **backend headless** (`co2-logger.service`, systemd) + **GUI
separata** (monitor, può essere chiusa senza fermare l'acquisizione) +
**valve-daemon** (controllo VICI via socket).

---

## 2. Hardware e cablaggio (Raspberry Pi 5)

Entrambi i sensori sono **I2C sul bus 1**, in parallelo su SDA/SCL, distinti
dall'indirizzo. ⚠️ **Alimentazione 3.3 V** (il BMP388 ha massimo assoluto 3.6 V:
mai 5 V).

| Sensore | VCC | GND | SDA | SCL | Addr |
|---|---|---|---|---|---|
| SHT3X (T/RH) | Pin 1 (3.3V) | Pin 6 | Pin 3 (GPIO2/SDA1) | Pin 5 (GPIO3/SCL1) | 0x44 |
| BMP388 (P) | Pin 17 (3.3V) | Pin 9 | Pin 3 *(condiviso)* | Pin 5 *(condiviso)* | 0x77 |

SDA (pin 3) e SCL (pin 5) sono **uniche** sul bus: i fili dei due sensori vanno
uniti (breadboard/splitter/saldatura). Alimentazione/GND su pin diversi per non
sdoppiare. Verifica: `i2cdetect -y 1` → devono comparire **0x44** e **0x77**.

GMP343: seriale **RS-232, /dev/gmp343, 19200 8N1**. VICI: /dev/vici, RS-232 9600.

---

## 3. Compensazione live (poll-mode)

Per inviare P/RH live la sonda deve essere in **POLL-mode** (in RUN ignora tutto
tranne `S`). Protocollo verificato sul campo (vedi anche skill `instrument-driver`):

- Terminatore comandi = **CR-only** (`\r`), NON `\r\n`.
- `CLOSE` STOP→POLL · `OPEN <addr>` POLL→STOP · `SEND <addr>` = una misura.
- `XP <addr> <hPa>` imposta P (volatile, no reply) · `XRH <addr> <%>` imposta RH.
- Indirizzo sonda = **0**. **Non** inviare `ADDR` nudo (entra in modalità modifica
  interattiva e blocca il parser).
- **Recovery POLL**: un processo ucciso in POLL lascia la sonda in POLL → `S`/`R`
  ignorati. `stop_run()` invia **`OPEN <addr>` per primo** per recuperare.

Config in `config/sensors.ini`:

```ini
[sht31_a]      # T/RH
enabled = true
addr = 0x44

[bmp388]       # P
enabled = true
addr = 0x77

[compensation]
enabled = true            # true=POLL-mode con feed live; false=RUN-mode (no feed)
gmp343_addr = 0
feed_pressure = true      # P live dal BMP388
feed_humidity = true      # RH live dallo SHT3X
fixed_pressure_hpa = 1014.0   # fallback se BMP388 assente
poll_interval_s = 2.0
```

⚠️ **Caveat metrologico**: la P "corretta" per la compensazione è quella del gas
**in cella/flusso**, non l'atmosferica. Il BMP388 in linea è buona approssimazione;
il capillare a valle ha dP ~0.7–3.7 hPa (<0.4 %, ~1–2 ppm). Vedi
`scheda_capillare_dP_GMP343.pdf`.

---

## 4. Formato file dati (v6, dal 2026-06-24)

La sonda è configurata (comando `FORM`, salvato in EEPROM) per emettere 3 CO₂:
`FORM CO2 " " CO2RAW " " CO2RAWUC`

- **CO2** = filtrata + compensata (P/T/RH/O₂) + linearizzata → valore **corretto**
- **CO2RAW** = non filtrata, compensazioni ancora applicate
- **CO2RAWUC** = non filtrata e **senza** compensazioni → **grezzo** (per ri-correzioni)

Colonne aggiunte **IN CODA** (dopo valvola) per non rompere i parser posizionali
(CO2 corretta resta in colonna 3):

| File | Header dati (coda) |
|---|---|
| `.raw` (per-campione) | `… CO2RAW[PPM] CO2RAWUC[PPM] P[hPa]` |
| `_min` / `_10min` / `_30min` | `… CO2RAW CO2RAW_std CO2RAWUC CO2RAWUC_std P P_std` |
| `_60min` | `… (con mediane) … CO2RAW_median … P P_std P_median` |

Sentinelle: `MISSING = -999.99`. Nome file: `carbocap343_ISACBO_YYYYMMDD_p00*.raw`.
Esempio riga `_min`:
`2026-06-24 10:28:00 497.93 0.85 24.80 0.02 65.14 0.04 3 measure 1 pos1-ambient 499.03 1.72 488.20 1.37 1012.95 0.01`

> Cambio formato: se non in produzione, rinominare i file del giorno `.old` e
> ripartire puliti (header coerente). Il monitor discrimina il formato 60-min
> **per nome file** (`_60min.`), non per conteggio colonne.

---

## 5. GUI (testo sempre in INGLESE)

**Monitor** (`gmp343_sht31_monitor.py`):
- **Current Data** — colonne LIVE (dal `.raw`) e 1-MIN AVERAGE (dal `_min`):
  CO₂ corretta, T, RH, **P**. Titoli gruppo: "CO₂ corrected (compensated)".
- **Daily Statistics** — min/max/mean CO₂ corretta.
- **Chart** — 3 pannelli con asse tempo sincronizzato (`sharex`):
  CO₂ corrected (sopra) · **P [hPa]** (in mezzo) · T/RH (sotto).
- Barra grafico: `CO₂ corrected … · raw … · Compensation: RH …→probe · P … (BMP388/fixed)`.
- Tab **VICI Valve**: stato daemon, posizione, schedule (Start/Stop + countdown).

**Launcher** (`bin/co2-launcher.sh`, yad): stato backend/monitor, ultima CO₂
(corretta/grezza), riga Compensation. Si aggiorna su Apply/Refresh.

> Regola: **tutto il testo UI in inglese** (CLAUDE.md). Chat/commenti restano IT.

---

## 6. Calibrazione — valve-scheduler (VICI 10 posizioni)

`valve-daemon.service` controlla la valvola via socket. Schedule = CSV
`position,minutes,label` in `~/programs/valve-scheduler/schedule/`:
- `schedule-1.csv` → cal completa 10 posizioni (zero/span-low/mid/high…)
- `schedule-cylinder.csv` → ambient + bombola CO₂
- `schedule-2.csv`, `schedule-semplice.csv` → 2 step

Stato in `service/valve_status.json` (`state`, `step_index`, `seconds_remaining`…).
Il daemon **non sostituisce** uno schedule in corso: per cambiarlo, **STOP** poi
START. La tab VICI Valve del monitor mostra countdown + Start/Stop leggendo
`valve_status.json` (`on_status`).

Integrazione opt-in: con `integration.ini` il logger annota `valve_pos valve_label`
nelle righe e può marcare `flag=calib` automaticamente.

---

## 7. Operazioni

```bash
# Stato / riavvio backend (acquisizione)
systemctl is-active co2-logger.service
sudo systemctl restart co2-logger.service     # recupera anche POLL via OPEN-first

# GUI (monitor + launcher) — restart "pulito" (script file per non auto-killare ssh)
bash /tmp/restart_guis.sh

# Attivare il BMP388 (P live): cablare → i2cdetect -y 1 → in sensors.ini
#   [bmp388] enabled=true , [compensation] feed_pressure=true → restart logger
# Verifica: status.json → comp_p_source="bmp388", comp_p_hpa ~ atmosferica

# Cambio datagramma (formato file): rinominare i file del giorno
for f in ~/data/*/carbocap343_*_$(date -u +%Y%m%d)_*.raw; do mv "$f" "$f.old"; done
sudo systemctl restart co2-logger.service     # ricrea file con header nuovo
```

`status.json` (`shared/ipc_co2/`): `last_co2_ppm` (corretta), `last_co2rawuc_ppm`
(grezza), `last_t_c`, `last_rh_pct`, `comp_active`, `comp_rh_fed`, `comp_p_hpa`,
`comp_p_source` (bmp388|fixed).

---

## 8. Troubleshooting

| Sintomo | Causa / Fix |
|---|---|
| Il Pi **si spegne** collegando il BMP388 | NON è consumo (µA). Quasi-corto: cablaggio invertito al lato Pi o board guasta. Misurare la corrente del solo BMP388 fuori dal Pi (kΩ/µA = ok). Collegare a Pi spento. 3.3 V, mai 5 V. Alim. PD 5A genuina (verificata). |
| Bus I2C **vuoto** (`i2cdetect`) | Cablaggio: SDA/SCL invertiti o linee condivise non unite; alimentazione assente. |
| Logger non legge T/RH dopo aver collegato il sensore | Il logger apre il bus solo all'avvio → `sudo systemctl restart co2-logger.service`. |
| `_min` a **n=0 / nessun dato** dopo restart | Sonda lasciata in POLL (processo ucciso senza teardown) → ignora S/R. Fix: `stop_run` invia `OPEN <addr>` per primo (già in codice). |
| Comandi sonda ignorati / "ERROR: Expecting integer" | Terminatore `\r\n` invece di `\r`; oppure `ADDR` nudo (prompt interattivo) → inviare `\r` vuoti. |
| CO₂ compensata sballata | `XP` volatile rimasto a valore vecchio → ripristinare `P <default>`. |
| GUI **lentissima** (cambio tab, RustDesk) | tracemalloc lasciato attivo a depth alta → modalità normale; depth ≤4. |
| Tab VICI Valve: countdown fermo, "Start" sempre acceso | Il monitor non passava lo stato a `TabSchedule.on_status()` (IPC get_status minimale) → ora legge `valve_status.json` (fix `26ce125`). |

---

## 9. File principali (`~/programs/CO2/`)

| File | Ruolo |
|---|---|
| `gmp343_sht31_logger.py` | Backend: CO2 seriale + SHT3X/BMP388 I2C + feed compensazione + scrittura file |
| `gmp343_compensation.py` | Helper poll-mode (stop_run/enter_poll/feed_and_send/exit_poll/set_pressure) |
| `bmp388.py` | Driver BMP388 (smbus2, compensazione float Bosch) |
| `gmp343_sht31_monitor.py` | GUI monitor PyQt5 (Current Data, Daily Stats, chart 3 pannelli, tab VICI) |
| `bin/co2-launcher.sh` | Launcher yad |
| `config/sensors.ini` | Config SHT3X / BMP388 / compensazione |
| `config/serial.ini`, `name.ini`, `site.ini`, `integration.ini` | Config seriale/sito/valvola |
| `doc/GMP343_User_Guide_M210514EN-C.pdf` | Manuale ufficiale sonda |
| `shared/ipc_co2/status.json` | Stato IPC per launcher/monitor |

---

## 10. Cronologia commit (branch `feat-bmp388-sht3x-poll`)

| Commit | Contenuto |
|---|---|
| `184c7cc` | Feed P/RH poll-mode costruito (standby) + manuale + driver BMP388 |
| `9fa9d82` | 3 CO2 negli aggregati + feed RH/P + recovery POLL + GUI EN |
| `4cad7fd` | Attivazione BMP388 (feed P live, `comp_p_source=bmp388`) |
| `97867b9` | P loggata in raw+aggregati (v6) + mostrata in Current Data |
| `26ce125` | Fix tab VICI Valve: countdown + Start/Stop sincronizzati |
| `9a44b40` | Pannello P nel chart (tra CO2 e T/RH, asse tempo condiviso) |

---

## 11. Riferimenti

- Manuale: **GMP343 User's Guide M210514EN-C** (`doc/`, anche su NotebookLM ACQ-Instruments)
- Datasheet: BMP388 (BST-BMP388-DS001), SHT3x-DIS
- Skill: `instrument-driver` (§ Vaisala GMP343), `co2-status`, `acq-graph-guard` (Bug 6),
  `cal-preflight`, `nox-cal-t200up-146i`
- Memorie: `project_co2_gmp343_3co2_session`, `reference_gmp343_serial_protocol`,
  `feedback_gui_always_english`
- Diario: `~/obsidian-acq/50-Diario/2026/2026-06-23.md`, `2026-06-24.md`
