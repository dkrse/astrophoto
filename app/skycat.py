"""Star and extra deep-sky catalogues for "objects in the field" (loaded once, kept in memory).

* HYG v4.1 (CC-BY-SA 4.0, https://codeberg.org/astronexus/hyg) – stars with an HD number, a proper name or a
  Bayer / Flamsteed designation: spectral type, B–V colour index, variability and distance (Hipparcos / Gaia).
* Stellarium DSO catalogue v3.x (GPL-2.0+, https://github.com/Stellarium/stellarium, nebulae/default) –
  Sharpless, Barnard, LBN, LDN, van den Bergh, RCW, Cederblad, Collinder, Melotte, Trumpler, Stock, Arp, HCG
  and PK objects that have no NGC / IC number (those come from OpenNGC), cross designations and distances
  of NGC / IC objects, and common names (names.dat).

Both files are downloaded when the image is built (see the Dockerfile); missing files only mean fewer objects."""
import math
import os
import re
import threading
from pathlib import Path

import numpy as np

HYG_FILE = Path(os.environ.get("HYG_FILE", "/opt/hyg/hygdata_v41.csv"))
STELLARIUM_DIR = Path(os.environ.get("STELLARIUM_DIR", "/opt/stellarium"))
PC_TO_LY = 3.261564
KPC_TO_LY = PC_TO_LY * 1000

_GREEK = {"Alp": "α", "Bet": "β", "Gam": "γ", "Del": "δ", "Eps": "ε", "Zet": "ζ", "Eta": "η", "The": "θ",
          "Iot": "ι", "Kap": "κ", "Lam": "λ", "Mu": "μ", "Nu": "ν", "Xi": "ξ", "Omi": "ο", "Pi": "π", "Rho": "ρ",
          "Sig": "σ", "Tau": "τ", "Ups": "υ", "Phi": "φ", "Chi": "χ", "Psi": "ψ", "Ome": "ω"}
_SUP = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")

_LOCK = threading.Lock()
_STARS = None
_DSO = None


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ---------- stars (HYG) ----------
def bayer(bf: str | None) -> str | None:
    """HYG 'bf' field ('21Alp And', '58    Oph', 'Kap1Cet') -> 'α And', '58 Oph', 'κ¹ Cet'."""
    bf = (bf or "").strip()
    if len(bf) < 4 or not bf[-3:].isalpha():
        return None
    con, head = bf[-3:], bf[:-3].strip()
    m = re.fullmatch(r"(\d*)\s*([A-Za-z]{2,3})?(\d*)", head)
    if not m:
        return None
    flam, greek, idx = m.groups()
    if greek:
        return f"{_GREEK.get(greek, greek)}{idx.translate(_SUP)} {con}"
    return f"{flam} {con}" if flam else None


def bv_to_rgb(bv: float | None) -> tuple:
    """Approximate display colour of a star from its B–V index (Ballesteros temperature -> black body sRGB),
    blended towards white so that it stays readable on a dark image."""
    if bv is None:
        return (255, 255, 255)
    bv = min(max(bv, -0.4), 2.0)
    t = 4600 * (1 / (0.92 * bv + 1.7) + 1 / (0.92 * bv + 0.62)) / 100       # Kelvin / 100
    if t <= 66:
        r, g = 255, 99.4708025861 * math.log(t) - 161.1195681661
        b = 0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * (t - 60) ** -0.1332047592
        g = 288.1221695283 * (t - 60) ** -0.0755148492
        b = 255
    c = [min(max(x, 0), 255) for x in (r, g, b)]
    return tuple(int(round(0.55 * x + 0.45 * 255)) for x in c)


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % rgb


def stars() -> dict | None:
    """HYG stars with an HD number or a name, as column arrays (ra / dec in degrees, J2000); loaded once."""
    global _STARS
    if _STARS is None:
        with _LOCK:
            if _STARS is None:
                _STARS = _load_stars(HYG_FILE)
    return _STARS or None


def _load_stars(path: Path) -> dict:
    import csv
    if not path.exists():
        return {}
    cols = {k: [] for k in ("ra", "dec", "mag", "name", "common", "spect", "bv", "var", "dist_ly", "hip")}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("id") == "0":                          # the Sun
                continue
            hd, proper, bf = (r.get("hd") or "").strip(), (r.get("proper") or "").strip(), bayer(r.get("bf"))
            mag, ra, dec = _num(r.get("mag")), _num(r.get("ra")), _num(r.get("dec"))
            if not (hd or proper or bf) or mag is None or ra is None or dec is None:
                continue
            name = f"HD {hd}" if hd else (bf or proper)
            pc = _num(r.get("dist"))
            var = None
            vname, vmin, vmax = (r.get("var") or "").strip(), _num(r.get("var_min")), _num(r.get("var_max"))
            rng = (min(vmin, vmax), max(vmin, vmax)) if vmin is not None and vmax is not None else (None, None)
            if vname or (rng[0] is not None and rng[1] - rng[0] >= 0.05):
                con = (r.get("con") or "").strip()
                if vname:
                    vname = f"{_GREEK.get(vname[:3], vname[:3])}{vname[3:].translate(_SUP)}" \
                        if vname[:3] in _GREEK else vname
                    vname = f"{vname} {con}".strip() if con else vname
                var = {"name": vname or None, "bright": rng[0], "faint": rng[1]}
            cols["ra"].append(ra * 15)
            cols["dec"].append(dec)
            cols["mag"].append(mag)
            cols["name"].append(name)
            cols["common"].append(" · ".join(x for x in (proper, bf) if x and x != name) or None)
            cols["spect"].append((r.get("spect") or "").strip() or None)
            cols["bv"].append(_num(r.get("ci")))
            cols["var"].append(var)
            cols["dist_ly"].append(round(pc * PC_TO_LY, 1) if pc and 0 < pc < 100000 else None)   # 100000 = unknown
            cols["hip"].append((r.get("hip") or "").strip() or None)
    if not cols["ra"]:
        return {}
    for k in ("ra", "dec", "mag"):
        cols[k] = np.array(cols[k], dtype=float)
    return cols


def star_record(st: dict, i: int) -> dict:
    """One star as an object-in-the-field record (position fields u / v / inside are added by the caller)."""
    bv = st["bv"][i]
    return {"name": st["name"][i], "messier": None, "common": st["common"][i], "type": "Star", "star": True,
            "mag": round(float(st["mag"][i]), 2), "maj": None, "min": None, "spect": st["spect"][i],
            "bv": bv, "color": _hex(bv_to_rgb(bv)), "var": st["var"][i], "dist_ly": st["dist_ly"][i],
            "hip": st["hip"][i], "ra": round(float(st["ra"][i]), 5), "dec": round(float(st["dec"][i]), 5),
            "catalog": "HYG"}


# ---------- extra deep-sky objects (Stellarium) ----------
# column (1-based, from the catalogue header) -> (label prefix, names.dat prefix, joiner)
DESIGNATIONS = [(22, "Sh2", "SH2", "-"), (21, "B", "B", " "), (23, "vdB", "VDB", " "), (26, "LBN", "LBN", " "),
                (25, "LDN", "LDN", " "), (24, "RCW", "RCW", " "), (31, "Ced", "CED", " "), (27, "Cr", "CR", " "),
                (28, "Mel", "MEL", " "), (42, "Tr", "TR", " "), (43, "St", "ST", " "), (32, "Arp", "ARP", " "),
                (38, "HCG", "HCG", " "), (34, "PK", "PK", " ")]
DSO_TYPES = {"G": "Galaxy", "GX": "Galaxy", "GC": "Globular cluster", "OC": "Open cluster", "NB": "Nebula",
             "PN": "Planetary nebula", "DN": "Dark nebula", "RN": "Reflection nebula", "C+N": "Cluster + nebula",
             "HA": "HII region", "HII": "HII region", "EN": "Emission nebula", "SNR": "Supernova remnant",
             "BN": "Bipolar nebula", "SA": "Stellar association", "SC": "Star cloud", "CL": "Star cluster",
             "IG": "Interacting galaxies", "RG": "Radio galaxy", "QSO": "Quasar", "MOC": "Molecular cloud",
             "*": "Star", "**": "Double star", "": "Other"}


def _names(path: Path) -> dict:
    """names.dat: {('SH2', '229'): 'Flaming Star Nebula', …} – the first name given for a designation."""
    out = {}
    if not path.exists():
        return out
    rx = re.compile(r'^(\S+)\s+(\S+)\s+_\("(.+?)"\)')
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = rx.match(line)
        if m:
            out.setdefault((m.group(1).upper(), m.group(2).lstrip("0") or "0"), m.group(3))
    return out


def dso() -> dict:
    """{'objects': [...extra DSOs without NGC / IC...], 'cross': {'ngc6992': {'aliases': [...], 'dist_ly': …}}}."""
    global _DSO
    if _DSO is None:
        with _LOCK:
            if _DSO is None:
                _DSO = _load_dso(STELLARIUM_DIR)
    return _DSO


def _load_dso(folder: Path) -> dict:
    out = {"objects": [], "cross": {}}
    path = folder / "catalog.txt"
    if not path.exists():
        return out
    names = _names(folder / "names.dat")
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            r = [c.strip() for c in line.rstrip("\n").split("\t")]
            if len(r) < 43:
                continue
            ra, dec = _num(r[1]), _num(r[2])
            if ra is None or dec is None:
                continue
            desig, common = [], None
            for col, label, nprefix, join in DESIGNATIONS:
                v = r[col - 1] if col - 1 < len(r) else ""
                if v and v != "0":
                    desig.append(f"{label}{join}{v}")
                    common = common or names.get((nprefix, v.lstrip("0") or "0"))
            kpc, plx = _num(r[14]), _num(r[12])
            dist = round(kpc * KPC_TO_LY) if kpc and kpc > 0 else (round(KPC_TO_LY / plx) if plx and plx > 0 else None)
            ngc, ic, m = r[16], r[17], r[18]
            if ngc not in ("", "0") or ic not in ("", "0") or m not in ("", "0"):
                key = f"ngc{int(ngc)}" if ngc not in ("", "0") else (f"ic{int(ic)}" if ic not in ("", "0") else f"m{int(m)}")
                c = out["cross"].setdefault(key, {"aliases": [], "dist_ly": None})
                c["aliases"] += [d for d in desig if d not in c["aliases"]]
                c["dist_ly"] = c["dist_ly"] or dist
                continue
            if not desig:                                   # PGC / UGC / ACO-only galaxies: too many, too faint
                continue
            vmag, bmag = _num(r[4]), _num(r[3])
            mag = vmag if vmag is not None and vmag < 30 else (bmag if bmag is not None and bmag < 30 else None)
            if r[5] in ("DN", "MOC"):                       # dark nebulae: the column is an opacity class
                mag = None
            maj, mn, pa = _num(r[7]), _num(r[8]), _num(r[9])
            out["objects"].append({
                "name": desig[0], "aliases": desig[1:], "messier": None, "type": r[5],
                "type_label": DSO_TYPES.get(r[5], r[5] or "Other"), "ra": ra, "dec": dec,
                "maj": maj or None, "min": mn or None, "pa": pa, "mag": mag, "common": common,
                "dist_ly": dist, "catalog": "Stellarium"})
    return out
