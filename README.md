# GMP343 CO₂ Acquisition System

> 🚨 **PC nuovo? Non clonare questo repo a mano.**
> Esiste un setup unificato che fa tutto (clone repo + dipendenze + Claude
> Code + .desktop + alias):
> ```
> git clone git@github.com:paocalzolari/acq-pc-setup.git ~/acq-pc-setup
> sudo bash ~/acq-pc-setup/install.sh
> ```
> Vedi [paocalzolari/acq-pc-setup](https://github.com/paocalzolari/acq-pc-setup).

## Struttura

```
programs/CO2/
├── gmp343_logger-5.py      ← acquisizione seriale → file dati
├── gui_integrated_v4.py    ← monitor + grafico (PyQt5)
├── gmp343_sensor.png       ← immagine sensore (GUI)
└── config/
    ├── serial.ini          ← porta, baudrate, parametri seriale
    ├── site.ini            ← coordinate, timezone, nome stazione
    ├── name.ini            ← basename e estensione file output
    └── monitor.ini         ← dimensioni finestra, soglie, colori
```

Dati scritti in:  `/home/misura/data/`

## Avvio

```bash
cd ~/programs/CO2

# 1. Logger (in background, lasciare sempre attivo)
python3 gmp343_logger-5.py &

# 2. GUI
python3 gui_integrated_v4.py
```

## Configurazione

### config/serial.ini
```ini
[serial]
port     = /dev/ttyUSB0
baudrate = 19200
bytesize = 8
parity   = N
stopbits = 1
timeout  = 1
```

### config/site.ini
```ini
[location]
name      = NomeStazione
latitude  = 44.0
longitude = 11.0
timezone  = Europe/Rome
```

### config/name.ini
```ini
[output]
basename  = carbocap
extension = raw
```

### config/monitor.ini
```ini
[window]
width  = 1200
height = 800
x      = 100
y      = 50

[thresholds]
low_warning    = 300
high_warning   = 2000
sentinel_value = 999.99

[display]
co2_decimals = 2
```
