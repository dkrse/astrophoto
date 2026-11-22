"""Objects in the field (HYG stars, Stellarium deep-sky extras, viewer filter) and the photometric quality check."""
import math

import numpy as np
import pytest

from app import annotate, photometry, platesolve, skycat, skymap
from test_sky import fake_solve

DENEB = (310.35798, 45.28034)
VEIL = (314.16, 31.75)                        # NGC 6992


# ---------- star catalogue (HYG) ----------
@pytest.mark.parametrize("bf,expect", [("21Alp And", "α And"), ("58    Oph", "58 Oph"), ("Kap1Cet", "κ¹ Cet"),
                                       ("Bet Ori", "β Ori"), ("", None), ("xx", None), ("  12", None)])
def test_bayer_designation(bf, expect):
    assert skycat.bayer(bf) == expect


def test_star_colour_from_bv():
    blue, sun, red = skycat.bv_to_rgb(-0.3), skycat.bv_to_rgb(0.65), skycat.bv_to_rgb(1.8)
    assert blue[2] > blue[0] and red[0] > red[2]               # hot stars blue, cool stars red
    assert sun[0] >= sun[2] and min(sun) > 200                 # Sun-like: nearly white
    assert skycat.bv_to_rgb(None) == (255, 255, 255)
    assert all(0 <= c <= 255 for c in skycat.bv_to_rgb(5) + skycat.bv_to_rgb(-5))


HYG_HEAD = '"id","hip","hd","hr","gl","bf","proper","ra","dec","dist","mag","spect","ci","con","var","var_min","var_max"\n'


def test_hyg_loader(tmp_path):
    p = tmp_path / "hyg.csv"
    p.write_text(HYG_HEAD +
                 '0,,,,"","",Sol,0.0,0.0,0.0,-26.7,G2V,0.656,"","",,\n'
                 '1,27989,39801,2061,"","58Alp Ori",Betelgeuse,5.919529,7.407063,152.6,0.45,M2Ib,1.5,Ori,Alp,0.571,0.281\n'
                 '2,1,224700,,"","","",0.00006,1.089009,100000,9.1,F5,0.482,Psc,"",,\n'
                 '3,5,,,"","","",0.1,1.0,50,9.5,K0,1.0,Psc,"",,\n'                    # no HD / name: skipped
                 '4,7,,,"","","Named",0.2,2.0,10,abc,K0,1.0,Psc,"",,\n')             # no magnitude: skipped
    st = skycat._load_stars(p)
    assert st["name"] == ["HD 39801", "HD 224700"]
    bet = skycat.star_record(st, 0)
    assert bet["common"] == "Betelgeuse · α Ori" and bet["spect"] == "M2Ib" and bet["bv"] == 1.5
    assert bet["var"] == {"name": "α Ori", "bright": 0.281, "faint": 0.571}
    assert bet["dist_ly"] == pytest.approx(152.6 * skycat.PC_TO_LY, abs=0.1)
    assert abs(bet["ra"] - 5.919529 * 15) < 1e-4 and bet["color"].startswith("#") and bet["star"]
    other = skycat.star_record(st, 1)
    assert other["dist_ly"] is None and other["var"] is None and other["common"] is None   # 100000 pc = unknown
    assert skycat._load_stars(tmp_path / "missing.csv") == {}


def test_real_hyg_catalogue_in_the_image():
    st = skycat.stars()
    assert st and len(st["name"]) > 90000
    i = st["name"].index("HD 197345")
    r = skycat.star_record(st, i)
    assert "Deneb" in r["common"] and "α Cyg" in r["common"] and r["spect"].startswith("A2")
    assert 1000 < r["dist_ly"] < 4000


# ---------- extra deep-sky objects (Stellarium) ----------
def _row(n, ra, dec, typ, vmag="99", maj="0", mnr="0", ngc="0", ic="0", kpc="0", plx="0", **desig):
    """One catalogue line; desig: column number (as in DESIGNATIONS) -> value, e.g. c22='229'."""
    r = ["0"] * 45
    r[0], r[1], r[2], r[3], r[4], r[5] = str(n), str(ra), str(dec), "99", vmag, typ
    r[6], r[7], r[8], r[9], r[12], r[14] = "", maj, mnr, "0", plx, kpc
    r[16], r[17] = ngc, ic
    for k, v in desig.items():
        r[int(k[1:]) - 1] = v
    for i in (30, 33, 34, 35, 36, 37, 38, 39):        # string columns: empty
        if r[i] == "0":
            r[i] = ""
    return "\t".join(r) + "\n"


def test_stellarium_loader(tmp_path):
    (tmp_path / "catalog.txt").write_text(
        "# Stellarium DSO catalog\n"
        + _row(1, 79.35, 34.38, "EN", vmag="6", maj="50", mnr="30", ic="405", kpc="0.46", c22="229", c26="795")
        + _row(2, 294.92, 10.52, "DN", vmag="6", maj="40", c21="142", c25="688")
        + _row(3, 312.9, 31.4, "SNR", maj="210", c22="103")
        + _row(4, 10.0, 10.0, "G", vmag="15", c29="12345")                      # PGC only: skipped
        + _row(5, 314.0, 31.7, "SNR", maj="60", ngc="6992", plx="2.2", c31="182b"))
    (tmp_path / "names.dat").write_text('B    142             _("Barnard\'s E Nebula") # BH\n'
                                        'SH2  103             _("Cygnus Loop") # X\n')
    d = skycat._load_dso(tmp_path)
    by = {o["name"]: o for o in d["objects"]}
    assert set(by) == {"B 142", "Sh2-103"}
    e = by["B 142"]
    assert e["aliases"] == ["LDN 688"] and e["common"] == "Barnard's E Nebula" and e["mag"] is None   # DN: opacity
    assert e["type_label"] == "Dark nebula" and e["maj"] == 40 and e["min"] is None
    assert by["Sh2-103"]["common"] == "Cygnus Loop" and by["Sh2-103"]["type_label"] == "Supernova remnant"
    assert d["cross"]["ic405"] == {"aliases": ["Sh2-229", "LBN 795"], "dist_ly": round(0.46 * skycat.KPC_TO_LY)}
    assert d["cross"]["ngc6992"]["dist_ly"] == round(skycat.KPC_TO_LY / 2.2)              # from the parallax
    assert skycat._load_dso(tmp_path / "missing") == {"objects": [], "cross": {}}


def test_catalogue_merges_extras_and_cross_designations():
    ic405 = platesolve.find_object("Sh2-229")
    assert ic405 and ic405["name"] == "IC 405" and "Sh2-229" in ic405["aliases"] and ic405["dist_ly"]
    assert platesolve.find_object("sh 2-229") is ic405
    b = platesolve.find_object("B 142")
    assert b and b["catalog"] == "Stellarium" and b["type_label"] == "Dark nebula"
    assert platesolve.find_object("Barnard 142") is None or platesolve.find_object("Barnard 142") is b
    veil = platesolve.find_object("NGC 6992")
    assert veil["catalog"] == "OpenNGC" and 1000 < veil["dist_ly"] < 3000
    assert platesolve.find_object("M51")["name"] == "NGC 5194"                    # OpenNGC still wins


def test_sky_map_stays_openngc():
    pts = skymap.catalogue_points(max_mag=12)
    assert pts and not any(p["n"].startswith(("Sh2-", "LBN ", "LDN ", "B ")) for p in pts)


# ---------- annotations: stars, extras, measured brightness ----------
def test_annotations_list_stars_after_deep_sky_objects():
    ann = platesolve.annotations(fake_solve(ra=DENEB[0], dec=DENEB[1], scale_arcsec=6, w=1200, h=900))
    objs = ann["objects"]
    stars = [o for o in objs if o.get("star")]
    assert stars and len(stars) <= platesolve.MAX_STARS
    first_star = objs.index(stars[0])
    assert all(not o.get("star") for o in objs[:first_star]) and all(o.get("star") for o in objs[first_star:])
    assert [s["mag"] for s in stars] == sorted(s["mag"] for s in stars)        # brightest first
    deneb = stars[0]
    assert deneb["name"] == "HD 197345" and "Deneb" in deneb["common"]
    assert abs(deneb["u"] - 0.5) < 0.01 and abs(deneb["v"] - 0.5) < 0.01 and deneb["inside"]
    assert all(s["inside"] and 0 <= s["u"] <= 1 and 0 <= s["v"] <= 1 for s in stars)
    assert all({"ra", "dec", "color", "spect", "dist_ly"} <= set(s) for s in stars)
    dso = [o for o in objs if not o.get("star")]
    assert any(o["name"].startswith("LBN ") and o["catalog"] == "Stellarium" for o in dso)   # extras listed too
    assert all({"ra", "dec", "aliases", "catalog"} <= set(o) for o in dso)


def test_measured_brightness_is_attached_to_listed_stars():
    res = fake_solve(ra=DENEB[0], dec=DENEB[1], scale_arcsec=6, w=1200, h=900)
    st = skycat.stars()
    ann = platesolve.annotations(res)
    stars = [o for o in ann["objects"] if o.get("star")][:3]
    res["photometry"] = {"zero_point": 20.0, "qc": {"sat_mag": 2.0},
                         "matches": [[s["ra"] + 1.5 / 3600, s["dec"], s["mag"] + 0.1, s["mag"] + 0.15, 300.0]
                                     for s in stars[1:]]}     # 1.5″ off: still the same star
    out = {o["name"]: o for o in platesolve.annotations(res)["objects"] if o.get("star")}
    assert "meas" not in out[stars[0]["name"]]                 # not among the measured stars
    m = out[stars[1]["name"]]["meas"]
    assert m["delta"] == 0.05 and m["band"] == "Gaia BP" and m["snr"] == 300.0
    assert m["saturated"] is (m["cat"] < 2.0)
    assert st is skycat.stars()                                  # loaded once
    res["photometry"]["matches"] = [[stars[1]["ra"] + 60 / 3600, stars[1]["dec"], 5, 5, 10]]   # 1′ away: no match
    assert all("meas" not in o for o in platesolve.annotations(res)["objects"])


# ---------- viewer filter / annotated JPEG ----------
def test_object_filter():
    objs = [{"name": "NGC 1"}, {"name": "HD 1", "star": True, "mag": 6.0}, {"name": "HD 2", "star": True, "mag": 9.5},
            {"name": "HD 3", "star": True, "mag": None}]
    names = lambda f: [o["name"] for o in annotate.filter_objects(objs, annotate.parse_filter(**f))]
    assert names({}) == ["NGC 1", "HD 1", "HD 2", "HD 3"]
    assert names({"smag": "8"}) == ["NGC 1", "HD 1"]
    assert names({"dso": "0"}) == ["HD 1", "HD 2", "HD 3"]
    assert names({"stars": "0"}) == ["NGC 1"]
    assert names({"smag": "nonsense"}) == ["NGC 1", "HD 1", "HD 2", "HD 3"]
    assert annotate.filter_objects(objs, None) == objs
    assert annotate.object_label({"name": "HD 1", "star": True, "common": "Deneb · α Cyg"}) == "Deneb · α Cyg"
    assert annotate.object_label({"name": "HD 1", "star": True, "common": None}) == "HD 1"
    assert annotate.object_label({"name": "NGC 5194", "messier": "M51", "common": "Whirlpool"}) == "M51 / NGC 5194 – Whirlpool"


def test_annotated_jpeg_honours_the_filter(monkeypatch, tmp_path):
    from PIL import Image
    seen = []
    orig = annotate.draw_annotations
    monkeypatch.setattr(annotate, "draw_annotations",
                        lambda im, ann, *a, **k: seen.append([o["name"] for o in ann["objects"]]) or orig(im, ann, *a, **k))
    monkeypatch.setattr(annotate, "render", lambda src, opts, max_side=None: (Image.new("RGB", (300, 200)), None))
    res = fake_solve(ra=VEIL[0], dec=VEIL[1], scale_arcsec=18, w=300, h=200)
    assert annotate.annotated_jpeg(tmp_path / "x.fits", {}, res, "none")[:3] == b"\xff\xd8\xff"
    assert annotate.annotated_jpeg(tmp_path / "x.fits", {}, res, "none",
                                   flt=annotate.parse_filter(dso="0", stars="1", smag="8"))
    everything, only_bright = seen
    assert "NGC 6992" in everything and any(n.startswith("HD ") for n in everything)
    assert only_bright and all(n.startswith("HD") for n in only_bright) and len(only_bright) < len(everything)
    assert "NGC 6992" not in only_bright


def test_viewer_has_filter_links_and_quality_block(client, project, tmp_path):
    from conftest import upload, write_fits, drain, star_frame
    upload(client, write_fits(tmp_path / "v.fits", data=star_frame(h=200, w=200)), "light", project_id=project)
    drain()
    from sqlalchemy import select
    from app.models import SessionLocal, FrameFile
    with SessionLocal() as db:
        fid = db.scalars(select(FrameFile).where(FrameFile.project_id == project)).first().id
    html = client.get(f"/files/{fid}/view").text
    for frag in ('id="flt-dso"', 'id="flt-stars"', 'id="flt-smag"', 'id="fi-qc"', 'id="fi-qc-svg"',
                 "fltQuery()"):
        assert frag in html, frag
    ext = html.replace("http://www.w3.org", "")
    assert "http://" not in ext and "https://" not in ext                     # offline app: nothing external


# ---------- photometric quality ----------
def synthetic_field(n=800, zp=25.0, sat=None, grad_u=0.0, vign=0.0, slope=1.0, noise=0.03, seed=7, W=2000, H=1500):
    """Matched stars: catalogue mags, measured mags (with the given defects), SNR and pixel positions."""
    rng = np.random.default_rng(seed)
    cat = rng.uniform(7, 15, n)
    x, y = rng.uniform(0, W, n), rng.uniform(0, H, n)
    u, v = x / W * 2 - 1, y / H * 2 - 1
    meas = 12 + (cat - 12) * slope + noise * rng.standard_normal(n)
    meas += grad_u / 2 * u + vign / 2 * (u * u + v * v)
    if sat is not None:                                      # clipped flux: brighter stars measure too faint
        meas = np.where(cat < sat, meas + (sat - cat) * 0.6, meas)
    snr = 10 ** ((16.5 - cat) / 2.5) * 5
    return cat, meas, snr, x, y, (W, H)


def test_quality_of_a_clean_frame():
    q = photometry.quality(*synthetic_field())
    assert q and q["scatter_mag"] == pytest.approx(0.03, abs=0.01) and q["zp_err"] < 0.005
    assert q["slope"] == pytest.approx(1.0, abs=0.02) and q["sat_mag"] is None
    assert q["gradient_mag"] < 0.03 and abs(q["vignetting_mag"]) < 0.03
    levels = {v["title"]: v["level"] for v in q["verdicts"]}
    assert levels["Precise photometry"] == "ok" and levels["Linear response"] == "ok" and levels["Even field"] == "ok"
    assert len(q["plot"]) <= 600 and all(len(p) == 3 for p in q["plot"])


def test_quality_finds_saturation_gradient_and_vignetting():
    q = photometry.quality(*synthetic_field(sat=10.0, grad_u=0.3, vign=0.25))
    assert q["sat_mag"] == pytest.approx(10.0, abs=0.35)
    assert q["gradient_mag"] == pytest.approx(0.3, abs=0.04)
    gx, gy = q["gradient_vec"]
    assert gx > 0 and abs(gy) < abs(gx) / 4                     # fainter towards the right (+u)
    assert q["vignetting_mag"] == pytest.approx(0.25, abs=0.04)
    titles = [v["title"] for v in q["verdicts"]]
    assert "Saturation" in titles and "Uneven field" in titles and "Vignetting" in titles
    assert "{dir}" in next(v["text"] for v in q["verdicts"] if v["title"] == "Uneven field")   # filled by the viewer
    used = [p for p in q["plot"] if p[2]]
    assert used and min(p[0] for p in used) >= q["sat_mag"]


def test_quality_flags_non_linear_and_brighter_corners():
    q = photometry.quality(*synthetic_field(slope=0.85, vign=-0.3))
    assert q["slope"] == pytest.approx(0.85, abs=0.02)
    lv = {v["title"]: v["level"] for v in q["verdicts"]}
    assert lv["Non-linear response"] == "warn" and lv["Corners brighter"] == "warn" and "Even field" not in lv


def test_quality_needs_enough_good_stars():
    cat, meas, snr, x, y, size = synthetic_field(n=8)
    assert photometry.quality(cat, meas, snr, x, y, size) is None


def test_saturation_onset_ignores_noise():
    rng = np.random.default_rng(1)
    cat = rng.uniform(8, 14, 400)
    assert photometry.saturation_onset(cat, 0.1 * rng.standard_normal(400), 0.1) is None
    r = np.where(cat < 9.5, 0.8, 0.0) + 0.02 * rng.standard_normal(400)
    assert photometry.saturation_onset(cat, r, 0.02) == pytest.approx(9.5, abs=0.2)


def test_limiting_magnitude_with_positions_returns_quality_and_matches():
    rng = np.random.default_rng(5)
    n = 400
    ra, dec = 100 + rng.uniform(-0.3, 0.3, n), 10 + rng.uniform(-0.3, 0.3, n)
    mag = rng.uniform(9, 16, n)
    zp = 25.0
    flux = 10 ** ((zp - mag) / 2.5)
    snr = flux / 10 ** ((zp - 17.0) / 2.5) * 5
    keep = snr >= 5
    x, y = rng.uniform(0, 1000, n), rng.uniform(0, 1000, n)
    res = photometry.limiting_magnitude(snr[keep], flux[keep], ra[keep], dec[keep], ra, dec, mag,
                                        x=x[keep], y=y[keep], size=(1000, 1000))
    assert res["zero_point"] == pytest.approx(zp, abs=0.02) and res["qc"]["scatter_mag"] < 0.02
    m = res["matches"]
    assert 0 < len(m) <= 300 and all(len(r) == 5 for r in m)
    assert [r[2] for r in m] == sorted(r[2] for r in m)                     # brightest first
    assert all(abs(r[3] - r[2]) < 0.05 for r in m)                           # measured ≈ catalogue
    plain = photometry.limiting_magnitude(snr[keep], flux[keep], ra[keep], dec[keep], ra, dec, mag)
    assert "qc" not in plain and plain["matches"]


def test_solve_photometry_uses_wcs_positions(monkeypatch):
    """The field uniformity is measured in the solved array's frame (the one the viewer maps), not ASTAP x / y."""
    res = fake_solve(ra=100.0, dec=10.0, scale_arcsec=2.0, w=1200, h=900)
    w = platesolve._wcs(res)
    rng = np.random.default_rng(3)
    n = 600
    px, py = rng.uniform(0, 1200, n), rng.uniform(0, 900, n)
    ra, dec = w.all_pix2world(np.column_stack([px, py]), 0).T
    mag = rng.uniform(9, 14, n)
    u = px / 1200 * 2 - 1
    flux = 10 ** ((25 - mag - 0.15 * u) / 2.5)              # stars fainter towards +u (right): 0.3 mag edge to edge
    snr = flux / 10 ** ((25 - 16.0) / 2.5) * 5               # background limited: SNR 5 at 16 mag
    monkeypatch.setattr(photometry, "catalogue_stars", lambda db, ra0, dec0, r: (ra, dec, mag))
    junk_xy = np.zeros(n)                                     # ASTAP's own x / y must not be used
    out = platesolve._photometry((snr, flux, ra, dec, junk_xy, junk_xy), res, 0.5, 2.0)
    q = out["qc"]
    assert q["gradient_mag"] == pytest.approx(0.3, abs=0.03)
    assert q["gradient_vec"][0] > 0 and abs(q["gradient_vec"][1]) < 0.02
    assert math.isclose(out["zero_point"], 25.0, abs_tol=0.05)


def test_objects_known_to_both_catalogues_appear_once():
    hh = [o for o in platesolve.catalogue() if "b33" in o["keys"]]
    assert len(hh) == 1 and hh[0]["catalog"] == "OpenNGC"           # OpenNGC addendum "B033" = Stellarium "B 33"
    assert "LDN 1730" in hh[0]["aliases"] and "Horsehead" in (hh[0]["common"] or "")   # B 33 = its own name
    assert platesolve.find_object("B 33") is hh[0]
    cat = platesolve.catalogue()
    ngc_keys = {k for o in cat if o["catalog"] == "OpenNGC" for k in o["keys"]}
    extra_desig = {platesolve.ident_key(d) for o in cat if o["catalog"] == "Stellarium"
                   for d in [o["name"], *o["aliases"]]}
    assert not (ngc_keys & extra_desig)                             # no object listed twice
