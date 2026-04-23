"""Test delle funzioni pure del monitor GUI (gui_integrated_v13.py).

Le funzioni puro-Python sono: get_data_dir, build_filename, read_file,
smart_ylim, night_spans (solo se astral non è installato → fallback).
Tutto il resto è PyQt5 (widgets, signal/slot) e non coperto qui."""
import configparser
from datetime import date as date_type, datetime

import numpy as np
import pytest


# Import del modulo
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import gui_integrated_v13 as gui


# ── get_data_dir ─────────────────────────────────────────────────────────

def test_gui_get_data_dir_expands_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = configparser.ConfigParser()
    cfg["output"] = {"data_path": "~/mydata"}
    assert gui.get_data_dir(cfg) == str(tmp_path / "mydata")


def test_gui_get_data_dir_default_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = configparser.ConfigParser()
    # Manca completamente la sezione [output] → usa fallback ~/data
    out = gui.get_data_dir(cfg)
    assert out.endswith("/data")


# ── build_filename ───────────────────────────────────────────────────────

class TestBuildFilename:
    def test_returns_empty_when_no_match(self, tmp_path):
        cfg = configparser.ConfigParser()
        cfg["output"] = {"extension": "raw", "data_path": str(tmp_path)}
        d = date_type(2026, 4, 22)
        assert gui.build_filename(cfg, d) == ""

    def test_returns_matching_file(self, tmp_path):
        cfg = configparser.ConfigParser()
        cfg["output"] = {"extension": "raw", "data_path": str(tmp_path)}
        # Crea file che matcha il pattern: *_20260422_p00_min.raw
        expected = tmp_path / "carbocap343_ISACBO_20260422_p00_min.raw"
        expected.write_text("")
        d = date_type(2026, 4, 22)
        assert gui.build_filename(cfg, d) == str(expected)

    def test_picks_most_recent_when_multiple_match(self, tmp_path):
        import time
        cfg = configparser.ConfigParser()
        cfg["output"] = {"extension": "raw", "data_path": str(tmp_path)}
        old = tmp_path / "a_20260422_p00_min.raw"
        new = tmp_path / "b_20260422_p00_min.raw"
        old.write_text("old")
        time.sleep(0.01)
        new.write_text("new")
        # Forza mtime: new più recente
        import os
        os.utime(old, (old.stat().st_atime, old.stat().st_mtime - 10))
        d = date_type(2026, 4, 22)
        assert gui.build_filename(cfg, d) == str(new)


# ── read_file ────────────────────────────────────────────────────────────

class TestReadFile:
    # Da 2026-04-23: read_file ritorna una 7-tupla (sono state aggiunte le
    # colonne opzionali valve_pos e valve_labels in coda) per integrazione
    # con valve-scheduler. Per file a 6 colonne (storici) queste liste sono
    # vuote — retrocompatibilità totale.

    def test_missing_path_returns_empty_lists(self):
        result = gui.read_file("/does/not/exist.raw")
        assert result == ([], [], [], [], [], [], [])

    def test_empty_path_returns_empty_lists(self):
        assert gui.read_file("") == ([], [], [], [], [], [], [])

    def test_parses_v2_format(self, tmp_path):
        f = tmp_path / "test.raw"
        f.write_text(
            "#date time CO2[PPM] CO2_std[PPM] ndata_60s_mean flag\n"
            "2026-04-22 11:00:00 412.5 0.3 60 measure\n"
            "2026-04-22 11:01:00 413.0 0.4 58 measure\n"
        )
        times, values, stds, counts, flags, valve_pos, valve_labels = gui.read_file(str(f))
        assert len(times) == 2
        assert values == [412.5, 413.0]
        assert stds == [0.3, 0.4]
        assert counts == [60, 58]
        assert flags == ["measure", "measure"]
        # File a 6 colonne: nessuna colonna valvola → liste vuote
        assert valve_pos == []
        assert valve_labels == []

    def test_skips_comment_lines(self, tmp_path):
        f = tmp_path / "c.raw"
        f.write_text(
            "# commento\n"
            "#date time CO2[PPM] CO2_std[PPM] ndata flag\n"
            "2026-04-22 11:00:00 412.5 0.3 60 measure\n"
        )
        times, values, *_ = gui.read_file(str(f))
        assert len(times) == 1

    def test_invalid_rows_skipped(self, tmp_path):
        f = tmp_path / "bad.raw"
        f.write_text(
            "2026-04-22 11:00:00 412.5 0.3 60 measure\n"
            "not-a-valid-row\n"
            "2026-04-22 11:02:00 413.5 0.2 60 measure\n"
        )
        times, values, *_ = gui.read_file(str(f))
        # Solo le 2 righe valide
        assert len(times) == 2

    def test_flag_calib_preserved_measure_default(self, tmp_path):
        f = tmp_path / "mixed.raw"
        f.write_text(
            "2026-04-22 11:00:00 412.5 0.3 60 measure\n"
            "2026-04-22 11:01:00 999.99 0.0 0 calib\n"
            "2026-04-22 11:02:00 413.0 0.4 58\n"           # no flag → default measure
        )
        _, _, _, _, flags, _, _ = gui.read_file(str(f))
        assert flags == ["measure", "calib", "measure"]

    def test_unknown_flag_coerced_to_measure(self, tmp_path):
        f = tmp_path / "u.raw"
        f.write_text(
            "2026-04-22 11:00:00 412.5 0.3 60 bogus\n"
        )
        _, _, _, _, flags, _, _ = gui.read_file(str(f))
        assert flags == ["measure"]

    # Nuovi test: integrazione valve-scheduler (formato esteso 8 colonne)

    def test_parses_extended_8col_format(self, tmp_path):
        f = tmp_path / "ext.raw"
        f.write_text(
            "#date time CO2[PPM] CO2_std[PPM] ndata_60s_mean flag valve_pos valve_label\n"
            "2026-04-23 10:30:00 412.34 0.85 60 measure 3 span-low\n"
            "2026-04-23 10:31:00 413.12 0.91 60 measure -1 -\n"
            "2026-04-23 10:32:00 414.50 1.10 55 calib 5 span-mid\n"
        )
        times, values, stds, counts, flags, valve_pos, valve_labels = gui.read_file(str(f))
        assert len(times) == 3
        assert valve_pos == [3, -1, 5]
        assert valve_labels == ["span-low", "-", "span-mid"]
        assert flags == ["measure", "measure", "calib"]

    def test_mixed_col_counts_in_same_file(self, tmp_path):
        # Scenario edge: un file con alcune righe a 6 e altre a 8 colonne
        # (non dovrebbe succedere in produzione, ma il parser non deve
        # crashare — assegna sentinelle dove le colonne mancano).
        f = tmp_path / "mixed_cols.raw"
        f.write_text(
            "2026-04-23 10:30:00 412.34 0.85 60 measure\n"
            "2026-04-23 10:31:00 413.12 0.91 60 measure 4 test\n"
        )
        times, _, _, _, _, valve_pos, valve_labels = gui.read_file(str(f))
        assert len(times) == 2
        # has_valve_cols=True perché almeno una riga ha 8 colonne
        # → le liste sono popolate, con sentinelle per la riga a 6 col
        assert valve_pos == [-1, 4]
        assert valve_labels == ["-", "test"]


# ── smart_ylim ───────────────────────────────────────────────────────────

class TestSmartYlim:
    def test_identical_values_get_minimum_range(self):
        # Tutti i valori uguali → la funzione deve applicare un range minimo
        vals = np.array([400.0, 400.0, 400.0])
        lo, hi = gui.smart_ylim(vals)
        assert hi - lo >= gui.MIN_Y_RANGE * 0.99

    def test_wide_range_gets_margin(self):
        vals = np.array([400.0, 500.0])
        lo, hi = gui.smart_ylim(vals)
        # Range almeno 100, con margine
        assert lo < 400.0
        assert hi > 500.0
        assert (hi - lo) > 100.0


# ── day_xlim ─────────────────────────────────────────────────────────────

def test_day_xlim_returns_24h_range():
    d = date_type(2026, 4, 22)
    x0, x1 = gui.day_xlim(d)
    # x1 = x0 + 1.0 giorno (matplotlib num dates)
    assert x1 - x0 == pytest.approx(1.0, abs=1e-9)
