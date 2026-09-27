# Changelog

Tutte le modifiche notevoli al progetto sono documentate qui. Format
ispirato a [Keep a Changelog](https://keepachangelog.com/it/1.1.0/).

Branch principale: `main` (produzione, backend `gmp343_sht31_logger.py`
via `co2-logger.service`).

## [Unreleased]

### Fixed — independent data-integrity review (2026-09-27)
- **P2 — righe 1-min doppie dopo un riavvio con l'orologio indietro**
  (`gmp343_sht31_logger.py`): un Raspberry Pi 5 non ha RTC, quindi dopo un
  blackout riparte con l'orologio fermo all'ultimo shutdown (fake-hwclock),
  cioè INDIETRO rispetto ai minuti già chiusi dall'esecuzione precedente,
  finché l'NTP non lo corregge. `current_minute` veniva inizializzato solo
  dall'ora di sistema, senza guardare l'ultima riga già scritta nel file
  `_min` del giorno: il nuovo processo riscriveva minuti già presenti,
  duplicandoli. Due fix complementari:
  - **Fix A** (`autoexec/co2-logger.service`, `autoexec/install-systemd.sh`,
    `autoexec/systemd-time-wait-sync.override.conf`): il service ordina
    l'avvio `After=time-sync.target` (+ `Wants=`), e l'installer abilita
    `systemd-time-wait-sync.service` (disabilitato di default) con un
    drop-in che ne limita l'attesa a 90s — upstream ha
    `TimeoutStartSec=infinity`, che senza rete al boot bloccherebbe
    l'intero avvio del sistema, non solo questo service. Se il timeout
    scatta, il boot prosegue e il logger parte comunque con l'orologio
    ancora indietro: è il Fix B, non questa unit, a garantire la
    correttezza dei dati in quel caso.
  - **Fix B** (`gmp343_sht31_logger.py`, `_last_written_minute()`): il
    logger legge all'avvio l'ultima riga del file `_min` del giorno e,
    se l'orologio di sistema risulta ancora indietro rispetto a quella
    riga, riparte da lì (nessuna riscrittura, i campioni nel frattempo
    restano solo nel `.raw` finché l'orologio non recupera — stesso
    comportamento già esistente per lo step-indietro a runtime).
  - Test: `tests/test_logger_minute_integrity.py::
    test_restart_same_minute_never_duplicates_the_row`,
    `::test_restart_with_clock_behind_resumes_without_duplicating_rows`.
- **P3 — salto dell'orologio in avanti oltre 6h segnalato solo da una
  print** (`gmp343_sht31_logger.py`): il buco veniva già chiuso
  correttamente (una riga sola, non un burst di MISSING) ma l'unica
  traccia era una riga in journald, non persistente oltre la rotazione
  del log. Aggiunto un marker sticky in `status.json`
  (`last_clock_gap_min`, `last_clock_gap_at`, meccanismo IPC già in uso
  per launcher/monitor — NON nel file dati), oltre alla print (ora
  `WARNING:`, prima `ERROR:`). Test:
  `tests/test_logger_minute_integrity.py::
  test_forward_jump_over_6h_leaves_a_persistent_marker`.

Nessuna modifica al formato `.raw`/`_min` in condizioni normali (verificato:
la suite pre-esistente resta verde byte-per-byte sulle stesse asserzioni).
