# CO2 / GMP343 — Stato programmi correnti

Documento di riferimento sui programmi attivi nella cartella `programs/CO2/`.
Aggiornato il: **2026-04-15**

> ⚠️ Mantenere questo file allineato. Per rigenerarlo automaticamente usare la
> skill `co2-status` (`~/.claude/skills/co2-status/`).

---

## Programmi correnti (ultime versioni in uso)

| Ruolo | File corrente | Wrapper bash | Lancio automatico |
|---|---|---|---|
| **Backend logger** (headless, solo seriale → file) | `gmp343_sht31_logger.py` | `co2-logger` | **`co2-logger.service`** (systemd, watchdog) |
| **Logger di calibrazione** (con GUI, flag measure/calib) | `gmp343_sht31_calib.py` | `co2-calib` | — *(manuale, durante calibrazioni)* |
| **Visualizzazione** (monitor real-time + grafico) | `gmp343_sht31_monitor.py` | `co2-monitor` | `Monitor-GMP343.desktop` |

### Cosa fanno

- **`gmp343_sht31_logger.py`** — backend di acquisizione headless. Apre
  `/dev/gmp343` (CO2 via seriale) e `/dev/i2c-1` (SHT31-D per T/RH), legge
  entrambi in modo sincrono a ogni campione CO2, calcola medie 1 minuto e
  scrive due file giornalieri:
  - `carbocap343_<site>_<YYYYMMDD>_p00.raw` (campioni grezzi)
  - `carbocap343_<site>_<YYYYMMDD>_p00_min.raw` (medie 1 min)
  Configurazione letta da `~/programs/CO2/config/{serial,site,name}.ini`.
  Flag fisso: `measure`. Nessuna interfaccia. **È il processo che garantisce
  la continuità dell'acquisizione** — gira sotto systemd con restart automatico.
  Se l'SHT31 non è raggiungibile (bus I2C assente, errore di lettura),
  T e RH vengono registrati come `-999.99` (MISSING) ma CO2 continua.

- **`gmp343_sht31_calib.py`** — stesso backend di acquisizione ma con GUI
  PyQt5 minimale per gestire le sessioni di calibrazione. Permette di cambiare
  in vivo il flag tra `measure` e `calib`, che viene scritto sul file `_min`
  per ogni record. **Va usato solo manualmente durante una calibrazione**,
  dopo aver fermato il backend systemd (vedi sezione "Procedura calibrazione").

- **`gmp343_sht31_monitor.py`** — GUI di sola visualizzazione (PyQt5 +
  matplotlib). Due tab:
  - *Tab 1*: monitor real-time del valore CO₂ corrente con flag dal file `_min`
  - *Tab 2*: grafico CO₂ giornaliero con punti `calib` evidenziati in arancione
  Non scrive sul sensore: legge solo i file `_min` prodotti dal logger.
  Non conflitta col backend.

---

## Formato file v3 (in uso dal 2026-04-15)

- Nome file: `carbocap343_<site>_<YYYYMMDD>_p00_min.raw` (con underscore, non trattini)
- Data/ora: `YYYY-MM-DD HH:MM:SS`
- Header `raw`: `#date time CO2[PPM] T[C] RH[%] flag`
- Header `_min`: `#date time CO2[PPM] CO2_std[PPM] T[C] T_std[C] RH[%] RH_std[%] ndata_60s_mean flag`
- Std in **PPM assoluto** (non percentuale). T_std in °C, RH_std in %.
- Flag: `measure` o `calib` (modificabile da GUI in `gmp343_sht31_calib.py`)
- Sentinel per dato mancante: `-999.99` (CO2 non acquisito, SHT31 non
  raggiungibile, minuto vuoto). La GUI filtra queste sentinelle dalle
  statistiche e dal plot.

### Migrazione da formato v2

I file scritti prima del 2026-04-15 hanno il vecchio formato v2 (5 colonne
nel `_min`, senza T/RH). Il parser del monitor (`read_file` in
`gmp343_sht31_monitor.py`) è retrocompatibile: rileva il numero di colonne
e assegna `-999.99` a T/RH per i file v2. I file v2 di oggi (15/04)
acquisiti prima della transizione sono stati rinominati con suffisso
`.pre-sht31` per non confondere il parser.

---

## Watchdog & autoesecuzione

### Backend logger — systemd system service

Il backend `gmp343_sht31_logger.py` gira sotto **`co2-logger.service`**, un
systemd system service installato in `/etc/systemd/system/`. Caratteristiche:

- **Parte al boot** (`WantedBy=multi-user.target`), indipendente dalla sessione X
- **Restart automatico** su qualsiasi exit (`Restart=always`, `RestartSec=10`)
- **Limite restart**: max 10 tentativi in 10 minuti, poi systemd marca il
  service come `failed` (evita restart loop su bug persistenti)
- Gira come utente `misura`, gruppo `dialout` (accesso a `/dev/gmp343`)
- Log via journald: `journalctl -u co2-logger -f`

Sorgente versionato: [`autoexec/co2-logger.service`](autoexec/co2-logger.service).
Installazione: [`autoexec/install-systemd.sh`](autoexec/install-systemd.sh)
(richiede sudo, da eseguire una volta sola dopo clone del repo).

### Comandi utili

```bash
systemctl status co2-logger             # stato corrente
journalctl -u co2-logger -f             # log live
journalctl -u co2-logger --since today  # log di oggi
sudo systemctl restart co2-logger       # restart manuale
sudo systemctl stop co2-logger          # stop (per calibrazione)
sudo systemctl start co2-logger         # ripartenza
```

### Visualizzazione GUI — autostart desktop

`gmp343_sht31_monitor.py` parte al login X tramite
`~/.config/autostart/Monitor-GMP343.desktop` (sorgente versionato in
`autoexec/Monitor-GMP343.desktop`). Non ha watchdog: se crasha, va riavviata
manualmente. Non è critico perché legge solo file e non scrive dati.

### Cron utente

Due job, definiti in [`autoexec/crontab.txt`](autoexec/crontab.txt):

1. **Sync dati** (ogni 5 min): `rsync-tailscale.sh` sincronizza `~/data/`
   verso `cimone@ozone.bo.isac.cnr.it:/home/cimone/data/gmp343` con
   auto-recovery Tailscale (se DNS/rsync fallisce riavvia `tailscaled`
   e ritenta una volta).
2. **Watchdog Tailscale** (ogni 10 min): `tailscale-health.sh --fix`
   verifica la risoluzione DNS di GitHub, ozone, naslame1 e riavvia
   `tailscaled` se necessario. Il restart viene **posposto** se c'è un
   `git push/pull/fetch/rsync/scp` in corso (evita di troncare TCP).

Entrambi gli script vivono in [`~/programs/services/tools/`](../services/tools/)
(repo `paocalzolari/services`, clonata sul Raspberry). La logica
Tailscale è centralizzata lì: un unico punto di aggiornamento per tutti
i PC di acquisizione del network.

Richiesta sudoers (una volta sola, vedi header degli script):

```bash
echo 'misura ALL=(root) NOPASSWD: /bin/systemctl restart tailscaled' | \
    sudo tee /etc/sudoers.d/misura-tailscale
sudo chmod 440 /etc/sudoers.d/misura-tailscale
```

### Icone sul Desktop

Tre lanciatori grafici in `~/Desktop/` (sorgenti versionati nella radice
`programs/CO2/`):

| Icona | Cosa fa | Script invocato |
|---|---|---|
| **CO2 Monitor** | Apre la GUI di visualizzazione | `gmp343_sht31_monitor.py` |
| **CO2 Calibration** | Procedura calibrazione completa: stop service → GUI calib → restart service | `bin/co2-calib-mode.sh` |
| **CO2 Status** | Apre un terminale con stato + log live del service | `bin/co2-status.sh` |

`CO2 Calibration` e `CO2 Status` aprono un terminale (`Terminal=true`) in
modo che l'utente veda cosa succede e possa digitare la password sudo
quando necessario. `CO2 Monitor` non apre terminale (è solo una GUI Qt).

---

## Procedura calibrazione

Il backend systemd e `gmp343_sht31_calib.py` **non possono girare insieme**:
entrambi vogliono `/dev/gmp343` e scrivono sugli stessi file giornalieri.

**Modo consigliato (icona desktop):** doppio click su **CO2 Calibration** sul
desktop → si apre un terminale che esegue `bin/co2-calib-mode.sh`. Lo script
ferma il service, lancia la GUI di calibrazione, e quando chiudi la finestra
riavvia automaticamente il service. Ti chiede solo la password sudo (due volte).

**Modo manuale a riga di comando:**

```bash
sudo systemctl stop co2-logger                          # 1. ferma il backend
cd /home/misura/programs/CO2 && python3 gmp343_sht31_calib.py    # 2. apri la GUI calib
# ... esegui la calibrazione, usa il pulsante per togglare flag measure/calib ...
# ... chiudi la GUI quando hai finito ...
sudo systemctl start co2-logger                         # 3. riparti il backend
```

Durante la calibrazione i record vengono comunque scritti sui file giornalieri
del giorno corrente, con il flag `calib` per i campioni in calibrazione e
`measure` per gli altri. Quando riparte il backend systemd, continua a scrivere
sugli stessi file con flag `measure`.

---

## File obsoleti (presenti solo per archivio)

| File | Sostituito da |
|---|---|
| `gmp343_logger-7.py` | `gmp343_sht31_logger.py` |
| `gmp343_logger-8.py` | `gmp343_sht31_logger.py` |
| `gmp343_logger-9.py` | `gmp343_sht31_logger.py` *(rinominato il 2026-04-15)* |
| `gui_integrated_v11.py` | `gmp343_sht31_monitor.py` |
| `gui_integrated_v12.py` | `gmp343_sht31_monitor.py` |
| `gui_integrated_v13.py` | `gmp343_sht31_monitor.py` *(rinominato il 2026-04-15)* |
| `calib-GMP343-logger-old.py` | `gmp343_sht31_calib.py` |
| `calib-GMP343-logger-old1.py` | `gmp343_sht31_calib.py` |
| `calib-GMP343-logger.py` | `gmp343_sht31_calib.py` *(rinominato il 2026-04-15)* |

Non eliminare senza prima averne fatto un tag git.

`Vaisala-logger.desktop` (autoexec/) è ancora presente per uso storico ma non
è più installato in `~/.config/autostart/`: il backend ora è gestito da systemd.

---

## Hardware

- Raspberry Pi 5
- Sensore Vaisala GMP343 su `/dev/gmp343` (symlink udev → `/dev/ttyUSB0`)
- Regola udev: `/etc/udev/rules.d/60-gmp343.rules`
- Utente `misura` deve essere nel gruppo `dialout` per accedere alla seriale
