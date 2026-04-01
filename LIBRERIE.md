# Librerie Python — GMP343 CO₂ Acquisition System

## Sommario

| Libreria       | Tipo        | Script            | pip install          |
|----------------|-------------|-------------------|----------------------|
| sys            | stdlib      | gui               | —                    |
| os             | stdlib      | gui, logger       | —                    |
| datetime       | stdlib      | gui, logger       | —                    |
| configparser   | stdlib      | gui, logger       | —                    |
| time           | stdlib      | logger            | —                    |
| statistics     | stdlib      | logger            | —                    |
| PyQt5          | terza parte | gui               | pip3 install PyQt5   |
| pyserial       | terza parte | gui, logger       | pip3 install pyserial|
| matplotlib     | terza parte | gui               | pip3 install matplotlib |
| numpy          | terza parte | gui               | pip3 install numpy   |
| astral         | terza parte | gui (opzionale)   | pip3 install astral==2.2 |
| pytz           | terza parte | gui (opzionale)   | pip3 install pytz    |

---

## Librerie Standard (stdlib) — già incluse in Python

### `sys`
Accesso a parametri e funzioni del sistema Python.
Usato per: avviare/terminare l'applicazione Qt (`sys.argv`, `sys.exit()`).

### `os`
Operazioni su file e directory del sistema operativo.
Usato per: verificare se un file esiste (`os.path.exists()`), costruire percorsi
(`os.path.join()`), creare directory dati (`os.makedirs()`).

### `datetime`
Manipolazione di date e orari.
Usato per: leggere timestamp dai file dati, calcolare intervalli di tempo,
determinare il giorno corrente per caricare il file giusto.

### `configparser`
Lettura di file di configurazione in formato `.ini`.
Usato per: caricare tutti i parametri da `serial.ini`, `site.ini`, `name.ini`,
`monitor.ini` senza hardcodare valori nello script.

### `time`
Funzioni legate al tempo di sistema.
Usato nel logger per: pause tra letture (`time.sleep()`), gestione del loop
di acquisizione al minuto.

### `statistics`
Calcoli statistici su sequenze di numeri.
Usato nel logger per: calcolare media e deviazione standard sui campioni CO₂
acquisiti nel minuto (mediazione al minuto del file `_min`).

---

## Librerie Terze Parti — da installare

### `PyQt5`
Framework per interfacce grafiche (GUI), binding Python di Qt5.
Usato per: finestra principale, tab Monitor/Grafico, layout, pulsanti,
label, combo box, selettori data, timer per aggiornamento periodico.

Installazione:
```bash
pip3 install PyQt5 --break-system-packages
```

Sottomoduli usati:
- `QtWidgets` — tutti i widget visibili (finestre, pulsanti, label…)
- `QtCore`    — QTimer (aggiornamento periodico), Qt (costanti), QDate
- `QtGui`     — QFont (font), QPixmap (immagine sensore)

### `pyserial`  (package: `serial`)
Comunicazione con porte seriali RS232/USB.
Usato per: aprire la porta seriale, leggere i dati raw dal sensore GMP343,
rilevare se la porta è connessa (lista porte disponibili).

Installazione:
```bash
pip3 install pyserial --break-system-packages
```

### `matplotlib`
Libreria per grafici scientifici 2D.
Usato per: disegnare il grafico CO₂ nel tempo, gestire zoom e pan,
tooltip hover, zone notturne, formattazione asse X con date/ore,
toolbar interattiva (zoom, pan, salvataggio PNG).

Installazione:
```bash
pip3 install matplotlib --break-system-packages
```

Sottomoduli usati:
- `Figure`                — oggetto figura matplotlib
- `FigureCanvasQTAgg`     — integrazione figura dentro finestra Qt
- `NavigationToolbar2QT`  — toolbar zoom/pan/save integrata Qt
- `matplotlib.dates`      — formattazione asse X con date e ore
- `matplotlib.ticker`     — controllo dei tick (non usato direttamente)

### `numpy`
Calcolo numerico su array, molto più veloce delle liste Python.
Usato per: ordinare i dati per timestamp (`np.argsort`), calcolare
min/max/media delle misure (`np.nanmin`, `np.nanmax`, `np.mean`),
filtrare valori sentinella, convertire liste in array efficienti.

Installazione:
```bash
pip3 install numpy --break-system-packages
```

### `astral`  *(opzionale)*
Calcolo di eventi astronomici: alba, tramonto, crepuscolo.
Usato per: disegnare le zone grigie notturne sul grafico CO₂,
calcolate con precisione dalla posizione geografica della stazione.
Se non installato il programma funziona ugualmente (zone notturne disabilitate).

Installazione (versione specifica per compatibilità piwheels/Raspberry Pi):
```bash
pip3 install astral==2.2 --break-system-packages
```

⚠️  Su Raspberry Pi usare esattamente `astral==2.2` dal repository piwheels.
    La versione 3.x ha API incompatibili.

### `pytz`  *(opzionale, richiesto da astral)*
Gestione dei fusi orari (timezone).
Usato per: convertire i tempi alba/tramonto di astral nel timezone corretto
della stazione, rimuovere l'info timezone per compatibilità con matplotlib.

Installazione:
```bash
pip3 install pytz --break-system-packages
```

---

## Installazione completa su Raspberry Pi (Debian Bookworm)

```bash
# Obbligatorie
pip3 install PyQt5 pyserial matplotlib numpy --break-system-packages

# Opzionali (zone notturne)
pip3 install astral==2.2 pytz --break-system-packages
```

> Il flag `--break-system-packages` è necessario su Debian Bookworm (Raspberry Pi OS
> recente) che protegge l'ambiente Python di sistema da installazioni pip non gestite
> dal package manager apt.

---

## Note su Raspberry Pi

**piwheels** è il repository binario ottimizzato per Raspberry Pi. pip lo usa
automaticamente. Per `astral` la versione 2.2 su piwheels ha API leggermente
diverse dalla versione 2.2 su PyPI ufficiale: gli script sono già scritti per
essere compatibili con la versione piwheels.
