# Configurazione Autostart - Raspberry Pi

> Per la **panoramica completa** dei programmi correnti vedi
> [`../PROGRAMS.md`](../PROGRAMS.md).

## Architettura del lancio automatico

Due meccanismi diversi a seconda del programma:

| Programma | Meccanismo | File |
|---|---|---|
| Backend logger (`gmp343_logger-9.py`) | **systemd system service** | `co2-logger.service` |
| Monitor GUI (`gui_integrated_v13.py`) | **autostart desktop X** | `Monitor-GMP343.desktop` |

Il backend ha bisogno di girare sempre, anche senza login grafico, e di essere
riavviato automaticamente in caso di crash → systemd. Il monitor GUI invece
serve solo quando c'è una sessione X con qualcuno davanti → autostart desktop.

## 1. Backend logger — systemd service (con watchdog)

### Installazione (una volta sola)

```bash
cd /home/misura/programs/CO2
sudo bash autoexec/install-systemd.sh
```

Lo script:
1. Copia `co2-logger.service` in `/etc/systemd/system/`
2. Esegue `systemctl daemon-reload`
3. Disabilita l'eventuale autostart `Vaisala-logger.desktop` (rinominato
   `.disabled`) per evitare conflitti sulla porta seriale
4. Stoppa eventuale `calib-GMP343-logger.py` in esecuzione
5. Esegue `systemctl enable --now co2-logger.service`

### Comandi utili

```bash
systemctl status co2-logger             # stato corrente
journalctl -u co2-logger -f             # log live
journalctl -u co2-logger --since today  # log di oggi
sudo systemctl restart co2-logger       # restart manuale
sudo systemctl stop co2-logger          # stop (per calibrazione)
sudo systemctl start co2-logger         # ripartenza
```

### Caratteristiche del service

- `Restart=always`, `RestartSec=10` → riavvio automatico su qualsiasi exit
- `StartLimitBurst=10` in 600s → max 10 restart/10min, poi marca `failed`
- `User=misura`, `Group=dialout` → accesso seriale senza root
- Log via journald (non `/tmp/`)

### Procedura calibrazione

Backend systemd e `calib-GMP343-logger.py` non possono coesistere — entrambi
vogliono `/dev/gmp343`. Per fare una calibrazione:

```bash
sudo systemctl stop co2-logger
cd /home/misura/programs/CO2 && python3 calib-GMP343-logger.py
# ... usa la GUI per togglare flag measure/calib durante la sessione ...
# ... chiudi la GUI quando hai finito ...
sudo systemctl start co2-logger
```

## 2. Monitor GUI — autostart desktop X

Il monitor parte automaticamente al login grafico tramite il `.desktop` in
`~/.config/autostart/`. Per ripristinarlo su un nuovo Raspberry Pi:

```bash
cp autoexec/Monitor-GMP343.desktop  ~/.config/autostart/
cp autoexec/pcmanfm-desktop.desktop ~/.config/autostart/
chmod +x ~/.config/autostart/Monitor-GMP343.desktop
chmod +x ~/.config/autostart/pcmanfm-desktop.desktop
```

> **Nota su `pcmanfm-desktop.desktop`**: non è specifico CO2 ma è un fix di
> sistema. LXDE-pi su Raspberry Pi avvia `pcmanfm --desktop` tramite
> `/etc/xdg/lxsession/LXDE-pi/autostart` con il prefisso `@` (respawn), ma
> dopo qualche crash lxsession smette di rispawnarlo e le icone sul desktop
> spariscono finché non riavvii. Questa entry XDG è un backup che lo rilancia
> a ogni login, indipendente dal contatore di respawn lxsession.

Il file in questa cartella è il **sorgente versionato** del `.desktop`. La
copia attiva deve restare allineata — se modifichi qui, ricopia in
`~/.config/autostart/`.

Stdout/stderr → `/tmp/co2monitor.log` per debug.

> **Nota**: il file `Vaisala-logger.desktop` è ancora presente in questa
> cartella per riferimento storico (quando il backend girava come autostart
> X dell'app calibrazione). **Non reinstallarlo** in `~/.config/autostart/`:
> conflitterebbe con il systemd service.

## 3. Cron (crontab.txt)

Il file `crontab.txt` contiene la configurazione cron dell'utente `misura`.
Per ripristinarla:

```bash
crontab autoexec/crontab.txt
```

Attività pianificate:
- Ogni 5 minuti: sincronizzazione dati CO2 verso server remoto
  (`ozone.bo.isac.cnr.it`) tramite `~/bin/rsync-co2.sh`

## 4. Rsync backup (rsync-co2.sh)

Script `rsync-co2.sh` da copiare in `~/bin/` e rendere eseguibile:

```bash
mkdir -p ~/bin
cp rsync-co2.sh ~/bin/rsync-co2.sh
chmod +x ~/bin/rsync-co2.sh
```

Sincronizza `~/data/` → `cimone@ozone.bo.isac.cnr.it:/home/cimone/data/gmp343`.
Usa un touchfile per evitare esecuzioni concorrenti.

## Hardware (Raspberry Pi 5)

- Sensore GMP343 collegato su `/dev/gmp343` (symlink udev persistente → `ttyUSB0`)
- Regola udev: `/etc/udev/rules.d/60-gmp343.rules`
- Utente `misura` deve essere nel gruppo `dialout` per accedere alla seriale

## Verifica rapida post-boot

```bash
ls /dev/gmp343                       # symlink udev presente?
systemctl status co2-logger          # backend systemd attivo?
journalctl -u co2-logger -n 30       # ultimi log del backend
pgrep -af gui_integrated             # monitor GUI attivo?
tail /tmp/co2monitor.log             # log del monitor
ls ~/data/$(date +%Y%m%d)*.raw 2>/dev/null  # file giornalieri scritti?
```
