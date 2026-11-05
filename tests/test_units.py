"""Pure functions: options, stretching, demosaic, type detection, dates, exposure summary."""
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.core import (parse_dmy, fmt_dmy, fmt_dur, moon_illumination, night_of, exposure_summary, clean_filename,
                      download_name, safe_name, thumb_rel)
from app.framestats import frame_stats, flag_suspects
from app.preview import (clean_opts, mtf, stretch, debayer_superpixel, debayer_bilinear, shift_pattern,
                         type_from_text, detect_frame_type, DEFAULT_OPTS)
from conftest import write_fits, star_frame


def test_clean_opts_defaults_and_validation():
    assert clean_opts(None) == DEFAULT_OPTS
    o = clean_opts({"bayer": "evil", "stretch": "manual", "bp": "0.1", "mp": "2", "wp": "0.05", "brightness": "500"})
    assert o["bayer"] == "auto"
    assert o["brightness"] == "100"
    assert (o["bp"], o["wp"]) == ("0", "1")          # wp <= bp -> reset
    assert o["mp"] == "0.9999"
    # levels are ignored unless stretch is manual (stable cache keys)
    assert clean_opts({"stretch": "linked", "bp": "0.3"})["bp"] == "0"


def test_mtf_midpoint():
    for m in (0.05, 0.25, 0.5, 0.8):
        assert abs(mtf(m, m) - 0.5) < 1e-6
    assert abs(mtf(0.3, 0.0)) < 1e-9 and abs(mtf(0.3, 1.0) - 1) < 1e-6


def test_stretch_modes_return_uint8():
    a = star_frame(60, 80).astype(np.float32)
    for mode in ("unlinked", "linked", "linear", "none"):
        out = stretch(a, {**DEFAULT_OPTS, "stretch": mode})
        assert out.dtype == np.uint8 and out.shape == a.shape
    manual = stretch(a, clean_opts({"stretch": "manual", "bp": "0.5", "wp": "0.6"}))
    assert manual.mean() < stretch(a, clean_opts({"stretch": "unlinked"})).mean()   # black point clips background


def test_debayer_patterns():
    a = np.zeros((4, 4), np.float32)
    a[0::2, 0::2] = 10            # R positions in RGGB
    sp = debayer_superpixel(a, "RGGB")
    assert sp.shape == (2, 2, 3) and sp[..., 0].mean() == 10 and sp[..., 2].mean() == 0
    assert debayer_superpixel(a, "BGGR")[..., 2].mean() == 10           # same data read as BGGR -> blue
    assert debayer_bilinear(a, "RGGB").shape == (4, 4, 3)
    assert shift_pattern("RGGB", 1, 0) == "GRBG" and shift_pattern("RGGB", 0, 1) == "GBRG"


def test_type_detection_words():
    assert type_from_text("Light Frame") == "light"
    assert type_from_text("Dark Flat") == "darkflat"
    assert type_from_text("flat_dark_001") == "darkflat"
    assert type_from_text("Bias Frame") == "bias"
    assert type_from_text("offsets") == "bias"
    assert type_from_text("FLATS") == "flat"
    assert type_from_text("darkness_photo") is None           # word boundary
    assert type_from_text("M42_stacked") == "stack"


def test_detect_frame_type_header_then_folder(tmp_path):
    f = write_fits(tmp_path / "x.fits", IMAGETYP="Dark Frame")
    assert detect_frame_type(f) == "dark"
    g = write_fits(tmp_path / "biases" / "b1.fits")
    assert detect_frame_type(g, "biases/b1.fits") == "bias"
    h = tmp_path / "photo.jpg"
    h.write_bytes(b"")
    assert detect_frame_type(h, "photo.jpg") == "stack"


def test_dates():
    assert parse_dmy("05-11-2026") == date(2026, 11, 5)
    assert parse_dmy("5.11.2026") == date(2026, 11, 5)
    assert parse_dmy("2026-11-05") == date(2026, 11, 5)
    assert parse_dmy("99-99-9999") is None and parse_dmy("") is None
    assert fmt_dmy(date(2026, 1, 2)) == "02-01-2026"
    assert fmt_dmy(datetime(2026, 1, 2, 3, 4)) == "02-01-2026 03:04"
    assert night_of(datetime(2025, 10, 14, 1, 0)) == date(2025, 10, 13)   # after midnight -> previous evening
    assert night_of(datetime(2025, 10, 13, 22, 0)) == date(2025, 10, 13)


def test_moon_illumination_known_phases():
    assert moon_illumination(date(2025, 10, 21)) < 5      # new moon 21-10-2025
    assert moon_illumination(date(2025, 10, 6)) > 95      # full moon 07-10-2025
    assert moon_illumination(None) is None


def test_fmt_dur():
    assert fmt_dur(0) == "0s" and fmt_dur(45) == "45s" and fmt_dur(120) == "2m" and fmt_dur(3720) == "1h 02m"


def test_exposure_summary_excludes_rejected():
    F = lambda **k: SimpleNamespace(**{"frame_type": "light", "rejected": False, "filter": "Ha", "exposure_s": 120.0, **k})  # noqa
    files = [F(), F(), F(rejected=True), F(filter="OIII", exposure_s=300.0), F(frame_type="dark"), F(exposure_s=None)]
    s = exposure_summary(files)
    assert s["total"] == 540 and s["count"] == 4 and s["missing"] == 1
    assert {r["filter"]: r["count"] for r in s["rows"]} == {"Ha": 3, "OIII": 1}


def test_filename_helpers():
    assert clean_filename("../../etc/passwd") == "passwd"
    assert clean_filename("C:\\x\\..\\a.fits") == "a.fits"
    assert clean_filename("") == "file"
    assert download_name('Mliečna dráha/M31 "x"') == "Mliečna dráha_M31 _x"
    assert "/" not in safe_name("../a b.fits") and safe_name("../a b.fits").endswith(".fits")
    assert str(thumb_rel("projects/1/light/a.fits")) == "projects/1/.thumbs/light__a.fits.jpg"
    assert thumb_rel("projects/1/light/a.fits") != thumb_rel("projects/1/dark/a.fits")


def test_frame_stats_fwhm_and_suspects(tmp_path):
    sharp = frame_stats(write_fits(tmp_path / "s.fits", star_frame(sigma=1.6)))
    blurry = frame_stats(write_fits(tmp_path / "b.fits", star_frame(sigma=4.0)))
    assert abs(sharp["fwhm"] - 2.3548 * 1.6) < 0.6        # true FWHM 3.77 px
    assert blurry["fwhm"] > sharp["fwhm"] * 1.8
    assert sharp["stars"] > 50
    lights = [SimpleNamespace(frame_type="light", stat_stars=sharp["stars"], stat_fwhm=sharp["fwhm"],
                              stat_bg=sharp["bg"], stat_noise=sharp["noise"], suspect=False) for _ in range(5)]
    lights.append(SimpleNamespace(frame_type="light", stat_stars=blurry["stars"], stat_fwhm=blurry["fwhm"],
                                  stat_bg=blurry["bg"], stat_noise=blurry["noise"], suspect=False))
    assert flag_suspects(lights) == 1 and lights[-1].suspect
