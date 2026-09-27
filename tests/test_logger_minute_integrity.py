"""End-to-end data-integrity tests for gmp343_sht31_logger.py (the CURRENT
production backend run by co2-logger.service).

The acquisition loop is driven through main() with a fake serial port and a
fake UTC clock, then the daily files are read back. Checked invariants:

* midnight UTC: every per-sample line lives in the file of its own date, the
  23:59 minute is the last row of day N and 00:00 the first row of day N+1;
* one 1-min row per elapsed minute, no duplicates, chronological order;
* gaps stay gaps: a minute without valid samples is a MISSING row
  (-999.99 values, n=0) and never a copy of a previous value;
* valid samples are never thrown away because the instrument went silent
  later in the same minute;
* a minute with calibration samples is flagged ``calib``;
* the 60-min statistics cover exactly the samples of their own hour.
"""
import configparser
from datetime import datetime, timedelta

import pytest
import serial

import gmp343_sht31_logger as gmp

MISSING = -999.99


# ── fakes ──────────────────────────────────────────────────────────────────
class FakeClock:
    def __init__(self, t0):
        self.t = t0

    def set(self, t):
        self.t = t

    def advance(self, seconds):
        self.t = self.t + timedelta(seconds=seconds)


def make_fake_datetime(clock):
    class FakeDateTime(datetime):
        @classmethod
        def utcnow(cls):
            return clock.t

        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return clock.t
            return clock.t.replace(tzinfo=tz)

    return FakeDateTime


class FakeSerial:
    """Replays a script of (utc_datetime, line) pairs. Reading an entry moves
    the fake clock to its time. When the script ends it raises
    SerialException and refuses to reopen, which makes main() return."""

    def __init__(self, script, clock):
        self.script = list(script)
        self.clock = clock

    def readline(self):
        if not self.script:
            raise serial.SerialException("end of script")
        t, line = self.script.pop(0)
        self.clock.set(t)
        return (line + "\r\n").encode() if line else b""

    def write(self, _data):
        return None

    def reset_input_buffer(self):
        return None

    def close(self):
        return None

    def open(self):
        raise serial.SerialException("end of script")


class FakeTSI:
    """Flow meter whose read takes `delay_s` of wall-clock time (the real
    TSI 4140 read can take up to ~2 s: 2 attempts x serial timeout)."""

    def __init__(self, clock, delay_s):
        self.clock = clock
        self.delay_s = delay_s

    def open_tsi(self, _port):
        return object()

    def ping(self, _dev):
        return True

    def read_flow_temp(self, _dev):
        self.clock.advance(self.delay_s)
        return 1.0, 20.0

    def to_volumetric(self, fm, _tg, _p_kpa):
        return fm


# ── harness ────────────────────────────────────────────────────────────────
def run_logger(tmp_path, monkeypatch, script, t0, *, tsi_delay_s=None,
               flag_fn=None, valve_fn=None):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    data_dir = tmp_path / "data"
    (cfg_dir / "name.ini").write_text(
        f"[output]\nbasename = carbocap343\nextension = raw\n"
        f"data_path = {data_dir}\n")
    (cfg_dir / "serial.ini").write_text("[serial]\nport = /dev/null\n")
    (cfg_dir / "site.ini").write_text("[location]\nname = TEST\n")
    sensors = "[sht31_a]\nenabled = false\n"
    if tsi_delay_s is not None:
        sensors += "[tsi4140]\nenabled = true\nport = /dev/null\n"
    (cfg_dir / "sensors.ini").write_text(sensors)

    monkeypatch.setattr(gmp, "NAME_INI", str(cfg_dir / "name.ini"))
    monkeypatch.setattr(gmp, "SERIAL_INI", str(cfg_dir / "serial.ini"))
    monkeypatch.setattr(gmp, "SITE_INI", str(cfg_dir / "site.ini"))
    monkeypatch.setattr(gmp, "SENSORS_INI", str(cfg_dir / "sensors.ini"))
    monkeypatch.setattr(gmp, "INTEGRATION_INI", str(cfg_dir / "none.ini"))
    monkeypatch.setattr(gmp, "_write_status_json", lambda *a, **k: None)
    monkeypatch.setattr(gmp, "_HAS_SMBUS", False)
    monkeypatch.setattr(gmp.time, "sleep", lambda _s: None)

    clock = FakeClock(t0)
    monkeypatch.setattr(gmp, "datetime", make_fake_datetime(clock))
    monkeypatch.setattr(gmp.serial, "Serial",
                        lambda *a, **k: FakeSerial(script, clock))
    if tsi_delay_s is not None:
        monkeypatch.setattr(gmp, "_HAS_TSI", True)
        monkeypatch.setattr(gmp, "tsi4140", FakeTSI(clock, tsi_delay_s),
                            raising=False)
    if flag_fn is not None:
        monkeypatch.setattr(
            gmp, "_auto_flag", lambda *a, **k: flag_fn(clock.t))
    if valve_fn is not None:
        monkeypatch.setattr(gmp, "load_valve_integration",
                            lambda: (True, "/nonexistent", 10.0, False, [], 1))
        monkeypatch.setattr(gmp, "valve_format_for_raw",
                            lambda *a, **k: valve_fn(clock.t), raising=False)

    gmp.main()
    return data_dir


def restart_logger(tmp_path, monkeypatch, first_script, t0, second_script, t1,
                    **kw):
    """Runs the logger twice against the SAME data dir, simulating a
    process restart (systemd Restart=always) at wall-clock `t1`."""
    data = run_logger(tmp_path, monkeypatch, first_script, t0, **kw)
    (tmp_path / "config").rename(tmp_path / "config_old")
    run_logger(tmp_path, monkeypatch, second_script, t1, **kw)
    return data


def samples(start, end, step_s=2.0, base=420.0):
    """Valid CO2 lines every `step_s` from start (inclusive) to end
    (exclusive), with distinct values so the dedup never drops them."""
    out = []
    t = start
    i = 0
    while t < end:
        out.append((t, f"{base + i * 0.01:.2f}"))
        t += timedelta(seconds=step_s)
        i += 1
    return out


def silence(start, end, step_s=1.0, line=""):
    """Empty readline timeouts (or a repeated non-CO2 line) every step_s."""
    out = []
    t = start
    while t < end:
        out.append((t, line))
        t += timedelta(seconds=step_s)
    return out


def read_rows(path):
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    assert lines and lines[0].startswith("#"), f"missing header in {path}"
    return [ln.split() for ln in lines[1:]]


def min_file(data_dir, day):
    return data_dir / f"carbocap343_TEST_{day}_p00_min.raw"


def raw_file(data_dir, day):
    return data_dir / f"carbocap343_TEST_{day}_p00.raw"


def row_times(rows):
    return [datetime.strptime(r[0] + " " + r[1], "%Y-%m-%d %H:%M:%S")
            for r in rows]


def assert_one_row_per_minute(rows, first, last):
    times = row_times(rows)
    expected = []
    t = first
    while t <= last:
        expected.append(t)
        t += timedelta(minutes=1)
    assert times == expected, (
        f"1-min rows are not exactly one per minute {first}..{last}:\n"
        f"missing={sorted(set(expected) - set(times))}\n"
        f"extra/dup={[x for x in times if times.count(x) > 1 or x not in expected]}")


def assert_missing_row(row):
    # date time CO2 CO2_std T T_std RH RH_std n flag CO2RAW ... FLOWvol_std
    assert float(row[2]) == MISSING and float(row[3]) == MISSING, row
    assert row[8] == "0", row
    for tok in row[10:]:
        assert float(tok) == MISSING, row


# ── midnight UTC ───────────────────────────────────────────────────────────
def test_midnight_samples_land_in_the_file_of_their_own_date(tmp_path,
                                                             monkeypatch):
    """With a slow secondary read (TSI), the sample read at 23:59:59 used to
    be timestamped after midnight but appended to the previous day's file."""
    t0 = datetime(2026, 9, 26, 23, 58, 0)
    script = samples(t0 + timedelta(seconds=1),
                     datetime(2026, 9, 27, 0, 2, 0))
    data = run_logger(tmp_path, monkeypatch, script, t0, tsi_delay_s=1.5)

    for day in ("20260926", "20260927"):
        want = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        for r in read_rows(raw_file(data, day)):
            assert r[0] == want, f"sample {r[0]} {r[1]} in file of {day}"
        for r in read_rows(min_file(data, day)):
            assert r[0] == want, f"1-min row {r[0]} {r[1]} in file of {day}"


def test_midnight_rows_split_without_loss_or_duplicates(tmp_path,
                                                        monkeypatch):
    t0 = datetime(2026, 9, 26, 23, 57, 0)
    script = samples(t0 + timedelta(seconds=1),
                     datetime(2026, 9, 27, 0, 3, 0))
    data = run_logger(tmp_path, monkeypatch, script, t0)

    day1 = read_rows(min_file(data, "20260926"))
    day2 = read_rows(min_file(data, "20260927"))
    assert_one_row_per_minute(day1, datetime(2026, 9, 26, 23, 57),
                              datetime(2026, 9, 26, 23, 59))
    assert_one_row_per_minute(day2, datetime(2026, 9, 27, 0, 0),
                              datetime(2026, 9, 27, 0, 1))
    for r in day1 + day2:
        assert float(r[2]) != MISSING and int(r[8]) >= 29, r


# ── gaps stay gaps, no data invented, no data thrown away ─────────────────
def test_silence_keeps_earlier_samples_and_writes_one_missing_row_per_minute(
        tmp_path, monkeypatch):
    """Instrument goes silent at 12:00:30 and comes back at 12:04:10."""
    t0 = datetime(2026, 9, 27, 11, 59, 0)
    script = (samples(t0 + timedelta(seconds=1),
                      datetime(2026, 9, 27, 12, 0, 30))
              + silence(datetime(2026, 9, 27, 12, 0, 31),
                        datetime(2026, 9, 27, 12, 4, 10))
              + samples(datetime(2026, 9, 27, 12, 4, 10),
                        datetime(2026, 9, 27, 12, 6, 0), base=430.0))
    data = run_logger(tmp_path, monkeypatch, script, t0)
    rows = read_rows(min_file(data, "20260927"))
    # the minute in progress when the script ends (12:05) is not closed yet
    assert_one_row_per_minute(rows, datetime(2026, 9, 27, 11, 59),
                              datetime(2026, 9, 27, 12, 4))
    by_time = dict(zip(row_times(rows), rows))

    r1200 = by_time[datetime(2026, 9, 27, 12, 0)]
    assert int(r1200[8]) == 15, f"12:00 lost its 15 valid samples: {r1200}"
    assert float(r1200[2]) == pytest.approx(420.0 + 0.01 * 37, abs=0.005)

    for m in (1, 2, 3):
        assert_missing_row(by_time[datetime(2026, 9, 27, 12, m)])
    assert float(by_time[datetime(2026, 9, 27, 12, 4)][2]) != MISSING


def test_unparseable_lines_do_not_freeze_the_minute_clock(tmp_path,
                                                          monkeypatch):
    """Non-empty but non-CO2 lines (e.g. an error string) for 3 minutes."""
    t0 = datetime(2026, 9, 27, 12, 0, 0)
    script = (samples(t0 + timedelta(seconds=1),
                      datetime(2026, 9, 27, 12, 0, 30))
              + silence(datetime(2026, 9, 27, 12, 0, 31),
                        datetime(2026, 9, 27, 12, 3, 20), line="ERR ***")
              + samples(datetime(2026, 9, 27, 12, 3, 20),
                        datetime(2026, 9, 27, 12, 5, 0), base=430.0))
    data = run_logger(tmp_path, monkeypatch, script, t0)
    rows = read_rows(min_file(data, "20260927"))
    assert_one_row_per_minute(rows, datetime(2026, 9, 27, 12, 0),
                              datetime(2026, 9, 27, 12, 3))
    by_time = dict(zip(row_times(rows), rows))
    assert int(by_time[datetime(2026, 9, 27, 12, 0)][8]) == 15
    for m in (1, 2):
        assert_missing_row(by_time[datetime(2026, 9, 27, 12, m)])


def test_stalled_loop_writes_missing_rows_for_skipped_minutes(tmp_path,
                                                              monkeypatch):
    """One read blocks for ~3 minutes (valid samples before and after)."""
    t0 = datetime(2026, 9, 27, 12, 0, 0)
    script = (samples(t0 + timedelta(seconds=1),
                      datetime(2026, 9, 27, 12, 0, 40))
              + samples(datetime(2026, 9, 27, 12, 3, 5),
                        datetime(2026, 9, 27, 12, 5, 0), base=430.0))
    data = run_logger(tmp_path, monkeypatch, script, t0)
    rows = read_rows(min_file(data, "20260927"))
    assert_one_row_per_minute(rows, datetime(2026, 9, 27, 12, 0),
                              datetime(2026, 9, 27, 12, 3))
    by_time = dict(zip(row_times(rows), rows))
    for m in (1, 2):
        assert_missing_row(by_time[datetime(2026, 9, 27, 12, m)])


def test_forward_jump_over_6h_leaves_a_persistent_marker(tmp_path,
                                                          monkeypatch):
    """A clock jump forward beyond MAX_GAP_FILL_MIN (NTP step, not an
    outage) is not filled with a burst of MISSING rows, and must leave a
    trace that outlives a single `print` line in journald: a sticky field
    in status.json (here read directly off the module, since the test
    harness no-ops _write_status_json)."""
    assert gmp.MAX_GAP_FILL_MIN == 360
    t0 = datetime(2026, 9, 27, 12, 0, 0)
    script = (samples(t0 + timedelta(seconds=1),
                      datetime(2026, 9, 27, 12, 0, 40))
              + samples(datetime(2026, 9, 27, 18, 35, 0),
                        datetime(2026, 9, 27, 18, 37, 0), base=500.0))
    run_logger(tmp_path, monkeypatch, script, t0)
    assert gmp._LAST_CLOCK_GAP["last_clock_gap_min"] is not None
    assert gmp._LAST_CLOCK_GAP["last_clock_gap_min"] > gmp.MAX_GAP_FILL_MIN
    assert gmp._LAST_CLOCK_GAP["last_clock_gap_at"] is not None


def test_clock_step_backwards_never_duplicates_minute_rows(tmp_path,
                                                           monkeypatch):
    t0 = datetime(2026, 9, 27, 12, 4, 0)
    script = (samples(t0 + timedelta(seconds=1),
                      datetime(2026, 9, 27, 12, 5, 30))
              # NTP steps the clock back by 2.5 minutes
              + samples(datetime(2026, 9, 27, 12, 3, 0),
                        datetime(2026, 9, 27, 12, 7, 0), base=430.0))
    data = run_logger(tmp_path, monkeypatch, script, t0)
    times = row_times(read_rows(min_file(data, "20260927")))
    assert len(times) == len(set(times)), f"duplicate minute rows: {times}"
    assert times == sorted(times), f"rows out of order: {times}"


# ── restart across a process crash (systemd Restart=always) ───────────────
def test_restart_same_minute_never_duplicates_the_row(tmp_path, monkeypatch):
    """A crash+restart within the SAME minute (RestartSec=10) must not
    produce two rows for that minute."""
    t0 = datetime(2026, 9, 27, 12, 0, 0)
    s1 = samples(t0 + timedelta(seconds=1), datetime(2026, 9, 27, 12, 3, 30))
    t1 = datetime(2026, 9, 27, 12, 3, 40)
    s2 = samples(t1, datetime(2026, 9, 27, 12, 6, 0), base=440.0)
    data = restart_logger(tmp_path, monkeypatch, s1, t0, s2, t1)
    times = row_times(read_rows(min_file(data, "20260927")))
    assert len(times) == len(set(times)), f"duplicate minute rows: {times}"


def test_restart_with_clock_behind_resumes_without_duplicating_rows(
        tmp_path, monkeypatch):
    """Raspberry Pi 5 reboot after a power outage: with no RTC, the system
    clock restarts from the state saved at shutdown, i.e. BEHIND the
    minutes already closed by the previous run, until NTP corrects it. The
    restarted process must not rewrite/duplicate those rows on disk, and
    must resume 1-min rows only once the (recovering) clock reaches the
    first minute not yet written."""
    t0 = datetime(2026, 9, 27, 12, 0, 0)
    s1 = samples(t0 + timedelta(seconds=1), datetime(2026, 9, 27, 12, 5, 30))
    # reboot: clock restored 3 min behind; NTP steps it forward mid-run
    t1 = datetime(2026, 9, 27, 12, 2, 0)
    s2 = (samples(t1 + timedelta(seconds=1), datetime(2026, 9, 27, 12, 4, 30),
                  base=440.0)
          + samples(datetime(2026, 9, 27, 12, 8, 1),
                    datetime(2026, 9, 27, 12, 9, 30), base=450.0))
    data = restart_logger(tmp_path, monkeypatch, s1, t0, s2, t1)
    rows = read_rows(min_file(data, "20260927"))
    times = row_times(rows)
    assert len(times) == len(set(times)), f"duplicate minute rows: {times}"
    assert times == sorted(times), f"rows out of order: {times}"
    # the 5 rows written by the first run (12:00..12:04) survive untouched
    assert times[:5] == [t0.replace(minute=m) for m in range(5)]


# ── calibration flag and 60-min window ─────────────────────────────────────
def test_minute_with_calibration_samples_is_flagged_calib(tmp_path,
                                                          monkeypatch):
    """Valve on a calibration position during 12:01, back to ambient at
    12:02:00. The 12:01 row must not be labelled 'measure'."""
    calib_from = datetime(2026, 9, 27, 12, 1, 0)
    calib_to = datetime(2026, 9, 27, 12, 2, 0)

    def flag_fn(now):
        return "calib" if calib_from <= now < calib_to else "measure"

    t0 = datetime(2026, 9, 27, 12, 0, 0)
    script = samples(t0 + timedelta(seconds=1),
                     datetime(2026, 9, 27, 12, 4, 0))
    data = run_logger(tmp_path, monkeypatch, script, t0, flag_fn=flag_fn)
    rows = read_rows(min_file(data, "20260927"))
    flags = {t: r[9] for t, r in zip(row_times(rows), rows)}
    assert flags[datetime(2026, 9, 27, 12, 0)] == "measure"
    assert flags[datetime(2026, 9, 27, 12, 1)] == "calib"
    assert flags[datetime(2026, 9, 27, 12, 2)] == "measure"


def test_hourly_statistics_use_only_samples_of_their_own_hour(tmp_path,
                                                              monkeypatch):
    t0 = datetime(2026, 9, 27, 12, 58, 0)
    script = samples(t0 + timedelta(seconds=1),
                     datetime(2026, 9, 27, 13, 3, 0))
    data = run_logger(tmp_path, monkeypatch, script, t0)
    f60 = data / "carbocap343_TEST_20260927_p00_60min.raw"
    rows = read_rows(f60)
    assert rows, "no 60-min row written"
    r = rows[0]
    assert (r[0], r[1]) == ("2026-09-27", "12:00:00")
    # samples at 12:58:01..12:59:59 every 2 s = 60 samples, none from 13:00
    assert int(r[11]) == 60, f"12:00 hour row has N={r[11]}: {r}"


def test_valve_columns_describe_the_minute_not_the_closing_instant(
        tmp_path, monkeypatch):
    """Valve moves from position 1 to 3 at 12:01:00. The 12:00 row must
    report position 1 (its samples), with the 1-min format of valve mode."""
    switch = datetime(2026, 9, 27, 12, 1, 0)

    def valve_fn(now):
        return ("1", "ambient") if now < switch else ("3", "span")

    def flag_fn(now):
        return "measure" if now < switch else "calib"

    t0 = datetime(2026, 9, 27, 12, 0, 0)
    script = samples(t0 + timedelta(seconds=1),
                     datetime(2026, 9, 27, 12, 3, 0))
    data = run_logger(tmp_path, monkeypatch, script, t0, flag_fn=flag_fn,
                      valve_fn=valve_fn)
    path = min_file(data, "20260927")
    header = path.read_text().splitlines()[0].split()
    rows = read_rows(path)
    assert all(len(r) == len(header) for r in rows), (header, rows)
    by_time = dict(zip(row_times(rows), rows))
    r1200 = by_time[datetime(2026, 9, 27, 12, 0)]
    r1201 = by_time[datetime(2026, 9, 27, 12, 1)]
    assert (r1200[9], r1200[10], r1200[11]) == ("measure", "1", "ambient")
    assert (r1201[9], r1201[10], r1201[11]) == ("calib", "3", "span")
