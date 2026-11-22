"""Fixes of the second review 2026-11-22: night from file names, transparency-reference invalidation after
commits, deterministic ties, cancelling the integrity walk, /stats frame count, lasting optics changes, tilt side
in the sensor frame, star-magnitude filter clamp, binning / offset in the calibration check."""
import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import calibcheck, integrity, measured, opticsadvice
from app.models import SessionLocal, FrameFile, ImagingSession
from app.routes_archive import path_night
from conftest import upload, write_fits, star_frame, drain


# ---------- night from a file name ----------
@pytest.mark.parametrize("rel,night", [
    ("x/M42_2024-03-16_0001.fits", date(2024, 3, 16)),               # frame counter, not 00:01
    ("x/Light_2024-03-16_0300s_gain100.fits", date(2024, 3, 16)),    # exposure, not 03:00
    ("x/Light_2024-03-16_0300sec.fits", date(2024, 3, 16)),
    ("x/Light_2024-03-16_01-30-00.fits", date(2024, 3, 15)),         # NINA time after midnight
    ("x/Light_2024-03-16_013000_x.fits", date(2024, 3, 15)),
    ("x/Light_2024-03-16_21-30-00.fits", date(2024, 3, 16)),
    ("x/Light_2024-03-16_25-30-00.fits", date(2024, 3, 16)),         # not a time
    ("x/Light_2024-03-16_01-30.fits", date(2024, 3, 16)),            # HH-MM only: ambiguous -> the date
])
def test_path_night_reads_only_full_times(rel, night):
    assert path_night(rel) == night


# ---------- transparency reference: invalidated after the commit, only by relevant changes ----------
def test_zp_reference_invalidated_after_relevant_commits(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "zp.fits", data=star_frame(seed=990)), "light", project_id=project)
    drain()
    gen = lambda: measured._REF["gen"]
    with SessionLocal() as db:
        f = db.scalars(select(FrameFile).where(FrameFile.project_id == project)).first()
        g0 = gen()
        f.stat_noise = 12.0                                        # unrelated column
        db.commit()
        assert gen() == g0
        f.solve = json.dumps({"ok": True})
        db.flush()
        assert gen() == g0                                         # not before the commit
        db.commit()
        assert gen() == g0 + 1
        f.rejected = True
        db.rollback()                                              # rolled back: nothing to invalidate
        assert gen() == g0 + 1
        s = db.get(ImagingSession, f.session_id)
        s.camera = "Another camera"
        db.commit()
        assert gen() == g0 + 2
        db.delete(db.get(FrameFile, f.id))
        db.commit()
        assert gen() == g0 + 3


# ---------- deterministic ties ----------
def test_dominant_gain_tie_is_deterministic():
    def fr(i, gain):
        return SimpleNamespace(id=i, frame_type="light", optics=None,
                               solve=json.dumps({"ok": True, "sky": {"gain": gain, "zp_s": 20.0 + i}}))
    files = [fr(1, "101"), fr(2, "100"), fr(3, "101"), fr(4, "100")]
    assert {measured.session_measured(files)["gain"], measured.session_measured(files[::-1])["gain"]} == {"100"}


# ---------- integrity walk stops on cancel ----------
def test_integrity_walk_honours_cancel(monkeypatch):
    seen = []

    def walk(_root):
        for i in range(1000):
            seen.append(i)
            if i == 2:
                integrity.state["cancel"] = True
            yield (str(integrity.ARCHIVE_DIR), [], [f"orphan{i}.fits"])
    monkeypatch.setattr(integrity.os, "walk", walk)
    monkeypatch.setattr(integrity, "_save_report", lambda rep: None)
    integrity.state.update(cancel=False)
    integrity.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        integrity.run_check(False)
    finally:
        integrity.state.update(cancel=False)
    assert len(seen) <= 4


# ---------- /stats counts frames, not aggregation groups ----------
def test_stats_light_frame_count(client, project, tmp_path):
    import re
    before = int(re.search(r"<b>([\d\s,.]+)</b><span>light frames", client.get("/stats").text).group(1)
                 .replace(",", "").replace(" ", "").replace(".", "") or 0)
    for i in range(3):                                             # same night, filter, target: one group
        upload(client, write_fits(tmp_path / f"st{i}.fits", data=star_frame(seed=1000 + i), EXPTIME=60.0),
               "light", project_id=project)
    drain()
    after = int(re.search(r"<b>([\d\s,.]+)</b><span>light frames", client.get("/stats").text).group(1)
                .replace(",", "").replace(" ", "").replace(".", ""))
    assert after - before == 3


# ---------- optics advice: the latest night alone is no lasting change ----------
def R(d, **k):
    return {"equipment": "E", "date": d, "date_dmy": d, "session_id": 1, "project_id": 1, "project": "P",
            "corners_pct": 10, "tilt_pct": 5, "tilt_dir": None, "elong": 1.0, "backfocus_mm": None,
            "findings_share": {}, **k}


@pytest.mark.parametrize("series", [[10, 10, 10, 50], [10, 50]])
def test_latest_outlier_night_is_not_a_change(series):
    rows = [R(f"2026-01-0{i + 1}", corners_pct=c) for i, c in enumerate(series)]
    titles = [x["title"] for x in opticsadvice.advise(rows)[0]["items"]]
    assert not any(t.startswith("Corners") for t in titles)


# ---------- tilt side in the sensor frame ----------
@pytest.mark.parametrize("side,orient,flip,expect", [
    ("left", 0, "none", "left"), ("left", 0, "h", "right"), ("top", 0, "v", "bottom"),
    ("top left", 0, "vh", "bottom right"),
    ("right", 90, "none", "top"),          # sensor turned clockwise for display: its top appears on the right
    ("right", 270, "none", "bottom"), ("bottom", 180, "none", "top"), ("nowhere", 0, "none", None)])
def test_sensor_side(side, orient, flip, expect):
    assert measured.sensor_side(side, orient, flip) == expect


def test_same_tilt_viewed_flipped_is_one_side():
    res = lambda flip, side: {"diagnosis": [{"key": "tilt", "dir": side, "level": "warn"}], "flip": flip,
                              "orient": 0, "fwhm_px": 3, "metrics": {"tilt_pct": 40}}
    a = measured.optics_summary(res("none", "left"))
    b = measured.optics_summary(res("h", "right"))               # same frame, viewed mirrored
    assert a["tilt_dir"] == b["tilt_dir"] == "left" and a["dir_frame"] == "sensor"


def test_old_view_dependent_sides_are_ignored():
    old = SimpleNamespace(id=1, frame_type="light", solve=None,
                          optics=json.dumps({"fwhm_px": 3, "tilt_dir": "left", "metrics": {"tilt_pct": 40}}))
    new = SimpleNamespace(id=2, frame_type="light", solve=None,
                          optics=json.dumps({"fwhm_px": 3, "tilt_dir": "right", "dir_frame": "sensor",
                                             "metrics": {"tilt_pct": 40}}))
    m = measured.session_measured([old, new])
    assert m["tilt_dir"] == "right" and m["tilt_frames"] == 1


# ---------- viewer: remembered star limit is clamped for filtering too ----------
def test_viewer_clamps_the_remembered_star_limit(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "vw.fits", data=star_frame(seed=1010)), "light", project_id=project)
    drain()
    with SessionLocal() as db:
        fid = db.scalars(select(FrameFile).where(FrameFile.project_id == project)).first().id
    assert "sl.value=v;FLT.smag=v;" in client.get(f"/files/{fid}/view").text


# ---------- calibration check: binning / offset from the lights' header ----------
def _S():
    return SimpleNamespace(camera="ASI2600", telescope="RC8", gain="100", exposure_s=300.0, filter="Ha",
                           cam_temp=-10.0, date=date(2026, 3, 10), calib_sets=[])


def _C(t, **k):
    return SimpleNamespace(**{"name": t, "frame_type": t, "camera": "ASI2600", "telescope": "RC8", "gain": "100",
                              "offset": None, "exposure_s": 300.0 if t == "dark" else 1.0, "temperature": "-10",
                              "filter": "Ha" if t == "flat" else None, "binning": None,
                              "capture_date": date(2026, 3, 10), **k})


def _L(header):
    return SimpleNamespace(frame_type="light", exposure_s=300.0, gain="100", filter="Ha", temperature="-10",
                           rejected=False, has_header=True, header=header)


HDR = "XBINNING=                    1\nYBINNING=                    1\nOFFSET  =                   50\n"


def test_calibration_binning_and_offset():
    f = calibcheck.check(_S(), [_L(HDR)], [_C("dark", binning="2x2", offset="30"), _C("flat", binning="1"),
                                          _C("bias", offset="50")])
    t = {x["title"].split(":")[0]: x["level"] for x in f}
    assert t.get("Other binning") == "bad" and t.get("Other offset") == "warn"
    assert sum(1 for x in f if x["title"].startswith("Other binning")) == 1   # the 1×1 flat fits
    ok = calibcheck.check(_S(), [_L(HDR)], [_C("dark", binning="1x1", offset="50.0"), _C("flat"), _C("bias")])
    assert [x["level"] for x in ok] == ["ok"]
    unknown = calibcheck.check(_S(), [_L("")], [_C("dark", binning="2x2", offset="30"), _C("flat"), _C("bias")])
    assert [x["level"] for x in unknown] == ["ok"]               # lights' values unknown: no mismatch
