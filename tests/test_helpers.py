"""Unit tests for small pure helpers that had no direct coverage (formatting, parsing, geometry, settings)."""
import json
import math
from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pytest

from app import core, platesolve, preview, conditions, measured, worker
from app.core import (fmt_size, fmt_num, fmt_dur, fmt_dmy, hash_pw, check_pw, coerce_setting, SETTINGS, accent_vars,
                      none_if_empty, clean_filename, download_name, parse_specs, spec_summary, night_of,
                      exposure_summary)


# ---------- formatting ----------
@pytest.mark.parametrize("n,out", [(None, "0 B"), (0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"),
                                   (1536, "1.5 KB"), (5 * 1024 ** 3, "5.0 GB"), (1024 ** 5, "1.0 PB")])
def test_fmt_size(n, out):
    assert fmt_size(n) == out


@pytest.mark.parametrize("v,out", [(None, ""), (3.0, "3"), (2.345, "2.3"), (7, "7"), ("x", "x")])
def test_fmt_num(v, out):
    assert fmt_num(v) == out


def test_fmt_num_digits():
    assert fmt_num(2.5, 2) == "2.50" and fmt_num(1.25, 0) == "1"


@pytest.mark.parametrize("s,out", [(None, "0s"), (0, "0s"), (-5, "0s"), (30, "30s"), (60, "1m"), (90, "1m 30s"),
                                   (3600, "1h 00m"), (22320, "6h 12m"), (59.6, "1m")])
def test_fmt_dur(s, out):
    assert fmt_dur(s) == out


def test_fmt_dmy():
    assert fmt_dmy(None) == "" and fmt_dmy(date(2025, 1, 2)) == "02-01-2025"
    assert fmt_dmy(datetime(2025, 1, 2, 3, 4)) == "02-01-2025 03:04"


# ---------- passwords ----------
def test_hash_and_check_password():
    h = hash_pw("tajné heslo")
    assert h.startswith("pbkdf2$") and "tajné" not in h
    assert check_pw("tajné heslo", h) and not check_pw("tajne heslo", h)
    assert hash_pw("a") != hash_pw("a")                      # random salt
    assert not check_pw("a", "garbage") and not check_pw("a", "")


# ---------- settings ----------
def test_coerce_setting_types_and_limits():
    assert coerce_setting("page_size", "50") == 50
    assert coerce_setting("page_size", "0") == 1 and coerce_setting("page_size", "99999") == 500
    assert coerce_setting("page_size", "abc") == SETTINGS["page_size"][0]
    assert coerce_setting("show_stats", "on") is True and coerce_setting("show_stats", "0") is False
    assert coerce_setting("accent_color", "#AbCdEf") == "#AbCdEf"
    assert coerce_setting("accent_color", "red") == SETTINGS["accent_color"][0]
    assert coerce_setting("default_theme", "dark") == "dark"
    assert coerce_setting("default_theme", "neon") == SETTINGS["default_theme"][0]
    assert coerce_setting("app_title", "  ") == SETTINGS["app_title"][0]


def test_accent_vars_contrast():
    assert accent_vars("#ffffff")["accent_fg"] == "#0d1017"
    assert accent_vars("#000000")["accent_fg"] == "#ffffff"
    assert accent_vars("#000000")["accent_dark"] == "#595959"


# ---------- names ----------
def test_none_if_empty():
    assert none_if_empty("  ") is None and none_if_empty(None) is None and none_if_empty(" a ") == "a"
    assert none_if_empty(0) == "0" and none_if_empty(12.5) == "12.5"


def test_clean_filename_and_download_name():
    assert clean_filename("C:\\data\\x\\M31.fits") == "M31.fits"
    assert clean_filename("../../.hidden") == "hidden"
    assert clean_filename("a\x00b\x1f.fit") == "ab.fit"
    assert clean_filename("") == "file" and len(clean_filename("x" * 400)) == 250
    assert clean_filename("Mliečna dráha.jpg") == "Mliečna dráha.jpg"           # unicode kept
    assert download_name('M31: "best"?') == "M31_ _best"
    assert download_name("...") == "download"


# ---------- equipment specs ----------
def test_parse_specs_validates():
    s = json.loads(parse_specs("telescope", {"spec_aperture_mm": "80", "focal_mm": "480,5",
                                             "telescope_type": "Refractor"}))
    assert s == {"aperture_mm": 80, "focal_mm": 480.5, "telescope_type": "Refractor"}
    assert parse_specs("telescope", {"aperture_mm": "-1", "focal_mm": "x", "telescope_type": "Laser"}) is None
    assert parse_specs("software", {"aperture_mm": "80"}) is None


def test_spec_summary():
    assert spec_summary("telescope", {"aperture_mm": 80, "focal_mm": 480, "telescope_type": "Refractor"}) \
        == "80 mm · 480 mm · f/6.0 · Refractor"
    cam = spec_summary("camera", {"width_px": 6000, "height_px": 4000, "pixel_um": 3.76})
    assert cam.startswith("6000×4000 px") and "22.6×15.0 mm" in cam
    assert spec_summary("mount", {}) == ""


# ---------- nights / exposure ----------
@pytest.mark.parametrize("dt,night", [
    (datetime(2025, 10, 12, 21, 0), date(2025, 10, 12)),
    (datetime(2025, 10, 13, 3, 0), date(2025, 10, 12)),          # after midnight -> previous evening
    (datetime(2025, 10, 13, 12, 0), date(2025, 10, 13)),
    (datetime(2025, 1, 1, 2, 0), date(2024, 12, 31)),            # across the year
    (None, None)])
def test_night_of(dt, night):
    assert night_of(dt) == night


def test_exposure_summary_groups_by_filter():
    F = lambda **k: SimpleNamespace(**{"frame_type": "light", "rejected": False, "filter": None, "exposure_s": 60.0, **k})
    files = [F(filter="Ha"), F(filter="Ha", exposure_s=120.0), F(), F(exposure_s=None),
             F(rejected=True), F(frame_type="dark")]
    r = exposure_summary(files)
    assert r["total"] == 240.0 and r["count"] == 4 and r["missing"] == 1
    assert {x["filter"]: x["count"] for x in r["rows"]} == {"Ha": 2, "—": 2}


# ---------- FITS header values ----------
def test_fits_value_quotes_and_comments():
    assert preview.fits_value(" 'M31/M32 '  / object") == "M31/M32"
    assert preview.fits_value(" 'O''Brien' / x") == "O'Brien"
    assert preview.fits_value(" 120.5 / [s] exposure") == "120.5"
    assert preview.fits_value(" T") == "T"


def test_platesolve_hdr_keeps_slash_in_strings():
    h = "OBJECT  = 'M31/M32'           / target\nEXPTIME =                 120. / seconds\nCOMMENT no value\n"
    assert platesolve._hdr(h) == {"OBJECT": "M31/M32", "EXPTIME": "120."}
    assert platesolve._hdr(None) == {}


def test_conditions_hdr_float():
    h = "CCD-TEMP=                -10.2 / C\nXPIXSZ  = '3.76'\nFOCALLEN= 'n/a'\n"
    assert conditions._hdr_float(h, {"CCD-TEMP"}) == -10.2
    assert conditions._hdr_float(h, {"XPIXSZ"}) == 3.76
    assert conditions._hdr_float(h, {"FOCALLEN"}) is None
    assert conditions._hdr_float(None, {"X"}) is None


@pytest.mark.parametrize("v,out", [
    ("2025-10-12T21:30:15.123456", datetime(2025, 10, 12, 21, 30, 15, 123456)),
    ("2025-10-12T21:30:15", datetime(2025, 10, 12, 21, 30, 15)),
    ("2025-10-12T21:30:15Z", datetime(2025, 10, 12, 21, 30, 15)),
    ("2025-10-12T21:30:15+00:00", datetime(2025, 10, 12, 21, 30, 15)),
    ("2025-10-12T23:30:15+02:00", datetime(2025, 10, 12, 21, 30, 15)),     # converted to UTC
    ("2025-10-12T16:30:15-0500", datetime(2025, 10, 12, 21, 30, 15)),
    ("2025-10-12 21:30:15", datetime(2025, 10, 12, 21, 30, 15)),
    ("2025-10-12", datetime(2025, 10, 12)),
    ("", None), (None, None), ("garbage", None)])
def test_parse_date_obs(v, out):
    assert preview.parse_date_obs(v) == out


def test_preview_num_clamps():
    assert preview._num("2.5000", 0, 10, "1") == "2.5"
    assert preview._num("99", 0, 10, "1") == "10" and preview._num("nan", 0, 10, "1") == "1"
    assert preview._num(None, 0, 10, "1") == "1" and preview._num("-0.0", 0, 10, "1") == "0"


def test_planes_first_and_to_image_planes():
    assert preview.planes_first(np.zeros((1, 1, 3, 4, 5))).shape == (3, 4, 5)
    assert preview.to_image_planes(np.zeros((3, 10, 20))).shape == (10, 20, 3)
    assert preview.to_image_planes(np.zeros((2, 10, 20))).shape == (10, 20)
    assert preview.to_image_planes(np.zeros((10, 20, 3))).shape == (10, 20, 3)
    assert preview.to_image_planes(np.zeros((10, 20))).shape == (10, 20)


def test_stf_params_range():
    rng = np.random.default_rng(0)
    shadow, mid = preview.stf_params(np.clip(rng.normal(0.1, 0.01, 10000), 0, 1))
    assert 0 <= shadow < 0.1 and 0 < mid < 0.5            # dark background is stretched (mid < 0.5)
    assert preview.stf_params(np.zeros(100)) == (0.0, 0.5)


# ---------- sky geometry ----------
def test_sexa():
    assert platesolve._sexa("05:35:17.3", True) == pytest.approx(83.822, abs=1e-3)
    assert platesolve._sexa("-05:23:28", False) == pytest.approx(-5.391, abs=1e-3)
    assert platesolve._sexa("-00:30:00", False) == pytest.approx(-0.5)
    assert platesolve._sexa("nonsense", True) is None


def test_separation_and_position_angle():
    assert float(platesolve._sep_deg(10, 0, 11, 0)) == pytest.approx(1)
    assert float(platesolve._sep_deg(0, 89, 180, 89)) == pytest.approx(2)
    assert float(platesolve._sep_deg(359.5, 0, 0.5, 0)) == pytest.approx(1)      # RA wrap
    assert platesolve._pa_deg(10, 0, 10, 1) == pytest.approx(0)                     # north
    assert platesolve._pa_deg(10, 0, 11, 0) == pytest.approx(90)                    # east
    assert platesolve._pa_deg(10, 0, 10, -1) == pytest.approx(180)


def test_offset_roundtrip():
    ra, dec = platesolve._offset(100, 60, 45, 0.5)
    assert float(platesolve._sep_deg(100, 60, ra, dec)) == pytest.approx(0.5, rel=0.01)
    assert platesolve._pa_deg(100, 60, ra, dec) == pytest.approx(45, abs=1)


# ---------- stored analysis / view options ----------
def test_optics_summary():
    regions = [{"fwhm_px": i} for i in range(9)]
    res = {"stars": 120, "fwhm_px": 2.5, "elong": 1.1, "regions": regions,
           "diagnosis": [{"key": "tilt", "dir": "NE", "level": "warn"}, {"key": "focus", "level": "ok"}]}
    s = measured.optics_summary(res)
    assert s["fwhm_centre_px"] == 4 and s["tilt_dir"] == "NE" and s["findings"] == ["tilt"] and s["bin"] == 1
    assert measured.optics_summary({})["fwhm_centre_px"] is None


@pytest.mark.parametrize("raw", [None, "", "{broken", "[1,2]", '"x"'])
def test_get_opts_falls_back_to_defaults(raw):
    assert worker.get_opts(SimpleNamespace(view_opts=raw)) == preview.clean_opts(None)
