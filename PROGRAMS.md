# CO2 / GMP343 — Stato programmi correnti

Documento di riferimento sui programmi attivi nella cartella `programs/CO2/`.
Aggiornato il: **2026-04-10**

> ⚠️ Mantenere questo file allineato. Per rigenerarlo automaticamente usare la
> skill `co2-status` (`~/.claude/skills/co2-status/`).

---

## Programmi correnti (ultime versioni in uso)

| Ruolo | File corrente | Wrapper bash | Lancio automatico |
|---|---|---|---|
| **Backend logger** (headless, solo seriale → file) | `gmp343_logger-9.py` | `co2-logger` | — |
| **Logger di calibrazione** (con GUI, flag measure/calib) | `calib-GMP343-logger.py` | `co2-calib` | `Vaisala-logger.desktop` |
| **Visualizzazione** (monitor real-time + grafico) | `gui_integrated_v13.py` | `co2-monitor` | `Monitor-GMP343.desktop` |

### Cosa fanno

- **`gmp343_logger-9.py`** — backend di acquisizione headless. Apre `/dev/gmp343`,
  legge il sensore, calcola medie 1 minuto e scrive due file giornalieri:
  - `carbocap343_<site>_<YYYYMMDD>_p00.raw` (campioni grezzi)
  - `carbocap343_<site>_<YYYYMMDD>_p00_min.raw` (medie 1 min)
  Configurazione letta da `~/programs/CO2/config/{serial,site,name}.ini`.
  Flag fisso: `measure`. Nessuna interfaccia.

- **`calib-GMP343-logger.py`** — stesso backend di acquisizione di
  `gmp343_logger-9.py` ma con GUI PyQt5 minimale per gestire le sessioni di
  calibrazione. Permette di cambiare in vivo il flag tra `measure` e `calib`,
  che viene scritto sul file `_min` per ogni record. È il programma in
  produzione su Cimone (lanciato da `Vaisala-logger.desktop`).

- **`gui_integrated_v13.py`** — GUI di sola visualizzazione (PyQt5 +
  matplotlib). Due tab:
  - *Tab 1*: monitor real-time del valore CO₂ corrente con flag dal file `_min`
  - *Tab 2*: grafico CO₂ giornaliero con punti `calib` evidenziati in arancione
  Non scrive sul sensore: legge solo i file `_min` prodotti dal logger.

---

## Formato file v2 (in uso dal 2026)

- Nome file: `carbocap343_<site>_<YYYYMMDD>_p00_min.raw` (con underscore, non trattini)
- Data/ora: `YYYY-MM-DD HH:MM:SS`
- Header `_min`: `#date time CO2[PPM] CO2_std[PPM] ndata_60s_mean flag`
- Std in **PPM assoluto** (non percentuale)
- Flag: `measure` o `calib` (modificabile da GUI in `calib-GMP343-logger.py`)

---

## Catena di lancio automatico

1. **Boot Raspberry Pi 5** → login utente `misura` → avvio sessione X11
2. La sessione legge i `.desktop` in `~/.config/autostart/`
3. Vengono lanciati in parallelo:
   - `Vaisala-logger.desktop` → `cd /home/misura/programs/CO2 && python3 calib-GMP343-logger.py > /tmp/co2logger.log 2>&1`
   - `Monitor-GMP343.desktop` → `cd /home/misura/programs/CO2 && python3 gui_integrated_v13.py > /tmp/co2monitor.log 2>&1`
4. Cron utente esegue ogni 5 minuti: `~/bin/rsync-co2.sh` (sync `~/data/` → server)

I sorgenti versionati dei `.desktop` e dello script rsync stanno in
`programs/CO2/autoexec/`. Le copie attive sono:
- `~/.config/autostart/Vaisala-logger.desktop`
- `~/.config/autostart/Monitor-GMP343.desktop`
- `~/bin/rsync-co2.sh`
- `crontab -l` (utente `misura`)

Per **ripristinare** il lancio automatico su un nuovo Raspberry vedi
`autoexec/README_autoexec.md`.

---

## File obsoleti (presenti solo per archivio)

| File | Sostituito da |
|---|---|
| `gmp343_logger-7.py` | `gmp343_logger-9.py` |
| `gmp343_logger-8.py` | `gmp343_logger-9.py` |
| `gui_integrated_v11.py` | `gui_integrated_v13.py` |
| `gui_integrated_v12.py` | `gui_integrated_v13.py` |
| `calib-GMP343-logger-old.py` | `calib-GMP343-logger.py` |
| `calib-GMP343-logger-old1.py` | `calib-GMP343-logger.py` |

Non eliminare senza prima averne fatto un tag git.

---

## Hardware

- Raspberry Pi 5
- Sensore Vaisala GMP343 su `/dev/gmp343` (symlink udev → `/dev/ttyUSB0`)
- Regola udev: `/etc/udev/rules.d/60-gmp343.rules`
