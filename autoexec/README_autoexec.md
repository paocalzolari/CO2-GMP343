# Configurazione Autostart - Raspberry Pi

> Per la **panoramica completa** dei programmi correnti vedi
> [`../PROGRAMS.md`](../PROGRAMS.md).

## Avvio automatico al boot (autostart desktop)

I programmi partono automaticamente al login dell'utente `misura` tramite i
file `.desktop` in `~/.config/autostart/`. Per ripristinarli su un nuovo
Raspberry Pi:

```bash
cp autoexec/Vaisala-logger.desktop  ~/.config/autostart/
cp autoexec/Monitor-GMP343.desktop  ~/.config/autostart/
chmod +x ~/.config/autostart/Vaisala-logger.desktop
chmod +x ~/.config/autostart/Monitor-GMP343.desktop
```

I file in questa cartella sono i **sorgenti versionati** dei `.desktop`. Le
copie attive devono restare allineate — se modifichi qui, ricopia in
`~/.config/autostart/`.

Programmi avviati automaticamente:
- **Vaisala-logger.desktop** → `calib-GMP343-logger.py` (logger calibrazione con GUI)
- **Monitor-GMP343.desktop** → `gui_integrated_v13.py` (monitor grafico)

Entrambi i `.desktop` redirigono stdout/stderr in `/tmp/co2logger.log` e
`/tmp/co2monitor.log` per debug.

## Cron (crontab.txt)

Il file `crontab.txt` contiene la configurazione cron dell'utente `misura`.
Per ripristinarla:

```bash
crontab autoexec/crontab.txt
```

Attività pianificate:
- Ogni 5 minuti: sincronizzazione dati CO2 verso server remoto
  (`ozone.bo.isac.cnr.it`) tramite `~/bin/rsync-co2.sh`

## Rsync backup (rsync-co2.sh)

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

## Verifica rapida post-boot

```bash
ls /dev/gmp343                 # symlink presente?
pgrep -af calib-GMP343         # logger in esecuzione?
pgrep -af gui_integrated_v13   # monitor in esecuzione?
tail /tmp/co2logger.log        # log del logger
tail /tmp/co2monitor.log       # log del monitor
```
