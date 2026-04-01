# Configurazione Autostart - Raspberry Pi

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

## Avvio manuale del sistema
Il logger e il monitor non hanno un servizio systemd configurato.
Per avviarli manualmente:
```bash
cd ~/programs/CO2
python3 gmp343_logger-9.py &
python3 gui_integrated_v13.py
```

## Hardware (Raspberry Pi 5)
- UART abilitata: `dtparam=uart0=on` in `/boot/firmware/config.txt`
- Sensore GMP343 collegato su `/dev/ttyUSB0` (adattatore USB-seriale)
