# Configurazione Autostart - Raspberry Pi

## Avvio automatico al boot (autostart desktop)

I programmi partono automaticamente al login tramite i file `.desktop`
in `~/.config/autostart/`. Per ripristinarli su un nuovo Raspberry Pi:

```bash
cp autoexec/Vaisala-logger.desktop ~/.config/autostart/
cp autoexec/Monitor-GMP343.desktop ~/.config/autostart/
chmod +x ~/.config/autostart/Vaisala-logger.desktop
chmod +x ~/.config/autostart/Monitor-GMP343.desktop
```

Programmi avviati automaticamente:
- **Vaisala-logger.desktop** — `calib-GMP343-logger.py` (acquisizione dati)
- **Monitor-GMP343.desktop** — `gui_integrated_v13.py` (monitor grafico)

## Cron (crontab.txt)
Il file `crontab.txt` contiene la configurazione cron dell'utente `misura`.
Per ripristinarla: `crontab crontab.txt`

Attività pianificate:
- Ogni 5 minuti: sincronizzazione dati CO2 verso server remoto (ozone.bo.isac.cnr.it)

## Rsync backup (rsync-co2.sh)
Script `rsync-co2.sh` da copiare in `~/bin/` e rendere eseguibile:
```bash
cp rsync-co2.sh ~/bin/rsync-co2.sh
chmod +x ~/bin/rsync-co2.sh
```
Sincronizza `~/data/` verso `cimone@ozone.bo.isac.cnr.it:/home/cimone/data/gmp343`
Usa un touchfile per evitare esecuzioni concorrenti.

## Hardware (Raspberry Pi 5)
- Sensore GMP343 collegato su `/dev/gmp343` (symlink udev persistente → ttyUSB0)
- Regola udev: `/etc/udev/rules.d/60-gmp343.rules`
