"""Test delle funzioni pure di gmp343_logger-9.py — la versione CORRENTE.

Attenzione: il nome del modulo ha un trattino (`gmp343_logger-9.py`), quindi
va importato via `importlib` (l'import diretto non funziona per i trattini)."""
import configparser
import importlib.util
from pathlib import Path

import pytest


# Import dinamico del modulo con trattino nel nome
_MODULE_PATH = Path(__file__).resolve().parent.parent / "gmp343_logger-9.py"
_spec = importlib.util.spec_from_file_location("gmp343_logger_9",
                                                  _MODULE_PATH)
gmp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gmp)


# ── parse_co2_from_line ──────────────────────────────────────────────────

class TestParseCO2:
    @pytest.mark.parametrize("line,expected", [
        ("412.5", 412.5),
        ("  412.5  ", 412.5),
        ("CO2 412.5 ppm", 412.5),
        ("412", 412.0),            # int accettato
        ("0.00", 0.0),
    ])
    def test_extract_first_numeric(self, line, expected):
        assert gmp.parse_co2_from_line(line) == expected

    def test_garbage_returns_none(self):
        assert gmp.parse_co2_from_line("no numbers here") is None
        assert gmp.parse_co2_from_line("") is None

    def test_negative_numbers_not_parsed(self):
        """parse_co2_from_line usa `.isdigit()` quindi non accetta numeri
        negativi. È un comportamento a design (CO2 non può essere < 0).
        Documentato qui come contratto."""
        # "-5" non matcha perché .replace('.','').isdigit() == False su '-'
        assert gmp.parse_co2_from_line("-5") is None


# ── get_data_dir ─────────────────────────────────────────────────────────

class TestGetDataDir:
    def test_expands_tilde(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = configparser.ConfigParser()
        cfg["output"] = {"data_path": "~/mydata"}
        out = gmp.get_data_dir(cfg)
        assert out == str(tmp_path / "mydata")
        # La cartella viene creata se mancante
        assert (tmp_path / "mydata").is_dir()

    def test_absolute_path_used_as_is(self, tmp_path):
        cfg = configparser.ConfigParser()
        cfg["output"] = {"data_path": str(tmp_path / "abs")}
        out = gmp.get_data_dir(cfg)
        assert out == str(tmp_path / "abs")
        assert (tmp_path / "abs").is_dir()

    def test_missing_key_uses_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = configparser.ConfigParser()
        cfg["output"] = {}
        out = gmp.get_data_dir(cfg)
        # Default: ~/data
        assert out.endswith("/data")


# ── get_filenames ────────────────────────────────────────────────────────

class TestGetFilenames:
    def _cfg(self, tmp_path, basename="carbocap343", site="ISACBO",
              extension="raw"):
        cfg = configparser.ConfigParser()
        cfg["output"] = {
            "basename": basename, "extension": extension,
            "data_path": str(tmp_path),
        }
        cfg["location"] = {"name": site}
        return cfg

    def test_generates_raw_and_min_files(self, tmp_path):
        cfg = self._cfg(tmp_path)
        raw, avg = gmp.get_filenames(cfg)
        # Entrambi nella stessa directory, con nomi distinti
        assert "_p00.raw" in raw
        assert "_p00_min.raw" in avg
        assert raw != avg
        assert str(tmp_path) in raw

    def test_site_name_appears_in_filename(self, tmp_path):
        cfg = self._cfg(tmp_path, site="MTC_SUMMIT")
        raw, _ = gmp.get_filenames(cfg)
        assert "MTC_SUMMIT" in raw

    def test_date_in_filename_is_utc_today(self, tmp_path):
        from datetime import datetime
        cfg = self._cfg(tmp_path)
        raw, _ = gmp.get_filenames(cfg)
        today = datetime.utcnow().strftime("%Y%m%d")
        assert today in raw


# ── write_headers_if_needed ──────────────────────────────────────────────

class TestWriteHeaders:
    def test_headers_written_when_files_missing(self, tmp_path):
        raw = tmp_path / "test.raw"
        avg = tmp_path / "test_min.raw"
        cfg = configparser.ConfigParser()
        gmp.write_headers_if_needed(str(raw), str(avg), cfg)
        assert raw.exists()
        assert avg.exists()
        # Header formato v2
        raw_content = raw.read_text()
        assert raw_content.startswith("#date time CO2[PPM] flag")
        avg_content = avg.read_text()
        assert avg_content.startswith("#date time CO2[PPM] CO2_std[PPM] "
                                        "ndata_60s_mean flag")

    def test_headers_not_duplicated_on_existing_files(self, tmp_path):
        raw = tmp_path / "test.raw"
        avg = tmp_path / "test_min.raw"
        raw.write_text("#date time CO2[PPM] flag\nexisting data\n")
        avg.write_text("#date time CO2[PPM] CO2_std[PPM] ndata_60s_mean flag\n")
        cfg = configparser.ConfigParser()
        gmp.write_headers_if_needed(str(raw), str(avg), cfg)
        # Non deve aver aggiunto niente
        raw_lines = raw.read_text().splitlines()
        assert len(raw_lines) == 2   # header + "existing data"
        assert raw_lines[0].startswith("#date time CO2")


# ── timestamp_now ────────────────────────────────────────────────────────

def test_timestamp_now_returns_iso_string_and_datetime():
    from datetime import datetime, timedelta
    ts_str, ts_dt = gmp.timestamp_now()
    assert isinstance(ts_str, str)
    assert isinstance(ts_dt, datetime)
    # Stringa: YYYY-MM-DD HH:MM:SS.fff
    parsed = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f")
    # Differenza < 1 sec tra la stringa e il datetime
    assert abs((parsed - ts_dt).total_seconds()) < 1.0


def test_timestamp_now_returns_utc():
    """timestamp_now usa datetime.utcnow() — quindi è UTC naive."""
    from datetime import datetime
    import time
    ts_str, ts_dt = gmp.timestamp_now()
    now_utc = datetime.utcnow()
    assert abs((ts_dt - now_utc).total_seconds()) < 2.0
