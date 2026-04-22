# Progetto CO2-GMP343 — Acquisizione e calibrazione CO₂ (Vaisala GMP343)

Sistema di acquisizione, monitoraggio e calibrazione del **sensore Vaisala
GMP343** (NDIR — Non-Dispersive InfraRed) per misure di CO₂ atmosferica.
Pensato per girare su Raspberry Pi 5 in postazione fissa, con backend
headless gestito da systemd e GUI di visualizzazione/calibrazione separate.

Le convenzioni cross-cutting valide per tutti i programmi della home
(formato `.raw`, sentinelle, stack tecnico, salvataggio file, policy doc
bilingue, sync) sono definite nel [CLAUDE.md globale](../../CLAUDE.md) e
non vengono ripetute qui.

## Strumento

**Vaisala GMP343** — sensore NDIR a singolo cammino ottico per CO₂
ambiente (range tipico 0–1000 ppm o 0–2000 ppm a seconda del modello,
con uscita digitale seriale). Output ASCII su porta seriale RS-232 a
**19200 bps 8N1**. Comando di avvio acquisizione continua: `R\r\n`. Lo
strumento risponde con righe testo contenenti il valore CO₂ corrente.

Hardware locale (postazione Monte Cimone / ISAC-BO):

- Raspberry Pi 5 con sensore GMP343 collegato via USB-RS232
- Symlink udev persistente `/dev/gmp343 → /dev/ttyUSB0` (regola in
  `/etc/udev/rules.d/60-gmp343.rules`)
- Utente `misura` nel gruppo `dialout` per accesso seriale

> Nota NDIR: il sensore GMP343 ha **compensazione interna di pressione e
> temperatura** abilitabile via comando, ma per misure GAW-grade può
> essere necessaria una correzione P/T post-acquisizione (vedere skill
> `atmo-ghg` e `libri-tecnici` per riferimenti). I dati `.raw` qui
> registrati sono i valori CO₂ così come usciti dal sensore: la correzione
> finale è demandata alla pipeline di analisi.

## Architettura

Programma **monolitico PyQt5** (vedere skill `acq-monolithic`). Non c'è
la separazione backend headless / GUI / driver / shared dei programmi più
recenti come [o3-monitor-headless](../o3-monitor-headless/CLAUDE.md) o
[nox-monitor-headless](../nox-monitor-headless/CLAUDE.md).

Lo strato di acquisizione e quello di visualizzazione sono però **due
processi distinti**:

1. **Backend logger** (`gmp343_logger-9.py`) — script Python da riga di
   comando, no GUI. Apre `/dev/gmp343`, legge in continuo, calcola medie
   1 minuto, scrive due file giornalieri. Gira come **systemd service**
   `co2-logger.service` con watchdog `Restart=always`.
2. **Monitor GUI** (`gui_integrated_v13.py`) — PyQt5 + matplotlib, due
   tab (monitor real-time + grafico giornaliero). **Non scrive sul
   sensore**, legge solo i file `_min.raw` prodotti dal backend. Avvio
   automatico al login X via `~/.config/autostart/Monitor-GMP343.desktop`.
3. **Logger di calibrazione** (`calib-GMP343-logger.py`) — variante del
   backend con GUI minimale per togglare il flag `measure ↔ calib` durante
   le sessioni di calibrazione. Lanciato manualmente. **Non può girare
   insieme al backend systemd** (entrambi tengono `/dev/gmp343`).

> Il backend e la GUI di visualizzazione possono coesistere senza
> conflitti perché la GUI è in sola lettura sui file `.raw`. Il logger di
> calibrazione invece è esclusivo: la procedura standard è `stop service
> → calib → start service` (vedere `bin/co2-calib-mode.sh`).

## File principali

```text
gmp343_logger-9.py              Backend logger headless (CORRENTE)
calib-GMP343-logger.py          Logger con GUI per sessioni calibrazione (CORRENTE)
gui_integrated_v13.py           Monitor real-time + grafico (CORRENTE)
diagnosi.py                     Script diagnostico (verifica config, file dati, parsing)
gmp343_sensor.png               Immagine sensore (icona + decorazione GUI)
build.sh                        Build PyInstaller → eseguibili in dist/
install_libraries.sh            Installazione librerie Python
LIBRERIE.md                     Documentazione delle librerie usate
PROGRAMS.md                     Stato programmi correnti (file di riferimento operativo)
README.md                       Guida rapida
```

Sotto-directory:

```text
config/         File .ini (serial, site, name, monitor)
autoexec/       Sorgenti versionati di systemd unit, .desktop, cron, rsync
bin/            Script di gestione (co2-calib-mode.sh, co2-status.sh)
```

Lanciatori desktop in radice repo (vengono copiati in `~/Desktop/`):

| Icona | File `.desktop` | Cosa fa |
|---|---|---|
| CO2 Monitor | [`CO2-Monitor.desktop`](CO2-Monitor.desktop) | Apre la GUI di visualizzazione |
| CO2 Calibration | [`CO2-Calib.desktop`](CO2-Calib.desktop) | Procedura completa: stop service → GUI calib → restart service |
| CO2 Status | [`CO2-Status.desktop`](CO2-Status.desktop) | Terminale con stato systemd + log live |

## Versioni multiple — qual è la corrente

Nel repo convivono **più versioni storiche** dei tre programmi principali.
Per evitare confusione, ecco la tabella aggiornata (vedere anche
[PROGRAMS.md](PROGRAMS.md), che resta il riferimento operativo):

| Ruolo | File CORRENTE | Versioni obsolete (archivio) |
|---|---|---|
| Backend logger | `gmp343_logger-9.py` | `gmp343_logger-7.py`, `gmp343_logger-8.py` |
| Monitor GUI | `gui_integrated_v13.py` | `gui_integrated_v11.py`, `gui_integrated_v12.py` |
| Logger calibrazione | `calib-GMP343-logger.py` | `calib-GMP343-logger-old.py`, `calib-GMP343-logger-old1.py` |

> **Avvertimento**: per modifiche strutturali partire SEMPRE dalla
> versione corrente. Le versioni vecchie sono mantenute solo per archivio
> e non vanno toccate (in caso, fare prima un tag git). `build.sh` punta
> ancora a `gui_integrated_v11.py` e `gmp343_logger-7.py` — va aggiornato
> se si vuole ricompilare con PyInstaller.

## Configurazione

Tutti i file `.ini` stanno in [`config/`](config/) e vengono letti dai tre
programmi tramite `configparser`. I percorsi sono **fissi** (path assoluto
`~/programs/CO2/config/`): il programma deve essere in
`~/programs/CO2/`, non altrove. Su PC con clone in `~/programs/CO2-GMP343/`
serve symlink o sostituzione del percorso.

```text
config/serial.ini    porta seriale, baudrate, byte/parity/stop
config/site.ini      nome stazione, lat/lon, timezone (per zone notturne astral)
config/name.ini      basename file output, estensione, data_path
config/monitor.ini   dimensioni finestra GUI, soglie, colori, font
```

Esempio valori effettivi:

```ini
# serial.ini
[serial]
port = /dev/gmp343
baudrate = 19200
bytesize = 8
parity = N
stopbits = 1
timeout = 1

# site.ini
[location]
name = ISACBO
latitude = 44.523624
longitude = 11.338379
timezone = UTC

# name.ini
[output]
basename  = carbocap343
extension = raw
data_path = ~/data
```

## Output

Due file giornalieri scritti dal backend in `~/data/`:

```text
carbocap343_<site>_<YYYYMMDD>_p00.raw       Campioni grezzi (uno per riga seriale)
carbocap343_<site>_<YYYYMMDD>_p00_min.raw   Medie 1 minuto (con SD e n campioni)
```

Header file `_min` (formato v2 in uso dal 2026):

```text
#date time CO2[PPM] CO2_std[PPM] ndata_60s_mean flag
```

Caratteristiche formato v2:

- Underscore nei nomi file (non più trattini)
- Timestamp `YYYY-MM-DD HH:MM:SS` (UTC)
- SD in **ppm assoluto**, non percentuale
- `flag` ∈ {`measure`, `calib`} — `calib` solo durante sessioni di taratura
- Sentinella per minuto senza dati validi: `999.99 0.00 0`

> Le sentinelle qui (`999.99` per CO₂ mancante) sono **diverse** da quelle
> usate da o3-monitor / nox-monitor (`-99.9`). È un'eredità storica del
> programma: non normalizzare senza coordinarsi con la pipeline a valle.

Sincronizzazione dati: cron utente esegue ogni 5 minuti
[`autoexec/rsync-co2.sh`](autoexec/rsync-co2.sh) che spinge `~/data/`
verso `cimone@ozone.bo.isac.cnr.it:/home/cimone/data/gmp343`.

## Avvio

### Avvio normale (dopo boot)

Tutto è automatico:

- Backend → systemd (`co2-logger.service`, parte al boot)
- Monitor GUI → autostart X (`~/.config/autostart/Monitor-GMP343.desktop`)
- Rsync dati → cron ogni 5 minuti

### Comandi manuali

```bash
# stato + log
systemctl status co2-logger
journalctl -u co2-logger -f

# restart backend
sudo systemctl restart co2-logger

# avvio manuale GUI (se chiusa)
cd ~/programs/CO2 && python3 gui_integrated_v13.py
```

### Procedura calibrazione

Due modi equivalenti — quello con icona desktop è preferito perché evita
errori di sequenza:

**Metodo icona desktop (consigliato)** — doppio click su **CO2
Calibration**: parte un terminale che esegue
[`bin/co2-calib-mode.sh`](bin/co2-calib-mode.sh) che ferma il service,
lancia la GUI di calibrazione, e al termine riavvia il service.

**Metodo manuale**:

```bash
sudo systemctl stop co2-logger
cd ~/programs/CO2 && python3 calib-GMP343-logger.py
# ... toggla flag measure/calib durante la sessione, poi chiudi la GUI
sudo systemctl start co2-logger
```

### Installazione iniziale (clone su nuovo Raspberry)

```bash
cd ~/programs/CO2
bash install_libraries.sh                  # PyQt5, pyserial, matplotlib, numpy, astral, pytz
sudo bash autoexec/install-systemd.sh      # installa e abilita il service
cp autoexec/Monitor-GMP343.desktop ~/.config/autostart/
crontab autoexec/crontab.txt
```

## Note importanti

- **NDIR e correzione P/T**: il GMP343 ha compensazione interna; per analisi
  GAW/ICOS è comunque opportuno verificare la correzione di pressione e
  temperatura in fase di post-processing (vedere skill `atmo-ghg`).
- **Drift del sensore**: i sensori NDIR derivano nel tempo (settimane/mesi).
  La calibrazione periodica con bombole **zero** (N₂ o aria sintetica) e
  **span** (CO₂ certificato a concentrazione nota) è essenziale per
  mantenere l'accuratezza nel range osservato (≈400–500 ppm di fondo a
  Monte Cimone, picchi più alti in sito ISAC-BO urbano).
- **Esclusività porta seriale**: backend systemd e logger di calibrazione
  non possono coesistere — sempre fermare uno prima di avviare l'altro.
- **Path hardcoded**: i programmi cercano `~/programs/CO2/config/` (non
  `~/programs/CO2-GMP343/config/`). Su PC con cartella clonata serve
  symlink: `ln -s ~/programs/CO2-GMP343 ~/programs/CO2`.
- **Versioni multiple**: prima di toccare un file, verificare che sia
  quello CORRENTE (vedere tabella sopra e [PROGRAMS.md](PROGRAMS.md)).
- **Refactoring backend/GUI**: per una eventuale evoluzione verso il
  pattern modulare moderno, partire dalla skill `acq-package` e prendere
  come riferimento [o3-monitor-headless](../o3-monitor-headless/CLAUDE.md)
  e [nox-monitor-headless](../nox-monitor-headless/CLAUDE.md).

## Riferimenti incrociati

- [CLAUDE.md globale della home](../../CLAUDE.md) — convenzioni generali
- [PROGRAMS.md](PROGRAMS.md) — stato operativo dei programmi correnti
- [LIBRERIE.md](LIBRERIE.md) — librerie Python e installazione
- [autoexec/README_autoexec.md](autoexec/README_autoexec.md) — autostart e systemd
- [thermo49i/CLAUDE.md](../thermo49i/CLAUDE.md) — pattern driver e thread di polling
- [o3-monitor-headless/CLAUDE.md](../o3-monitor-headless/CLAUDE.md) — esempio di
  programma con architettura moderna backend/GUI separati
- Skill rilevanti:
  - `acq-monolithic` — paradigma di questo programma
  - `acq-package` — eventuale refactoring futuro
  - `acq-data-integrity` — audit scrittura file `.raw`
  - `acq-qa`, `acq-ergonomics` — review codice/UX
  - `instrument-driver` — pattern seriale RS-232
  - `realtime-graph` — grafico matplotlib in PyQt5
  - `ini-config-gui` — gestione config `.ini`
  - `atmo-ghg` — analisi dati gas serra (correzione, baseline, growth rate)
  - `calibration-stats` — OLS/ODR e GUM per calibrazione zero/span
  - `libri-tecnici` — formule e protocolli (NDIR, ICOS, GAW)
