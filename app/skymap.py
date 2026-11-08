"""Sky map of all projects: exact field outlines from the plate solves (TAN WCS), catalogue positions from OpenNGC,
ecliptic and galactic equator from astropy, constellation of each field from the IAU boundaries (astropy).

Everything is J2000 / ICRS. No constellation figures are drawn – only data that can be computed exactly."""
import json
import math
import time

_LINES: dict | None = None
_CACHE: dict = {}


def tan_to_radec(wcs: dict, x: float, y: float):
    """FITS (1-based) pixel -> (ra, dec) in degrees with the gnomonic (TAN) projection of the solve."""
    (c11, c12), (c21, c22) = wcs["cd"]
    dx, dy = x - wcs["crpix"][0], y - wcs["crpix"][1]
    xi, eta = math.radians(c11 * dx + c12 * dy), math.radians(c21 * dx + c22 * dy)
    ra0, dec0 = math.radians(wcs["crval"][0]), math.radians(wcs["crval"][1])
    den = math.cos(dec0) - eta * math.sin(dec0)
    ra = ra0 + math.atan2(xi, den)
    dec = math.atan2(eta * math.cos(dec0) + math.sin(dec0), math.hypot(xi, den))
    return round(math.degrees(ra) % 360, 4), round(math.degrees(dec), 4)


def valid_wcs(wcs) -> bool:
    """All numbers present and finite, a non-degenerate CD matrix and a size."""
    try:
        nums = [*wcs["crpix"], *wcs["crval"], *wcs["cd"][0], *wcs["cd"][1], wcs["w"], wcs["h"]]
        if len(nums) != 10 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in nums):
            return False
        (a, b), (c, d) = wcs["cd"]
        return a * d - b * c != 0 and wcs["w"] > 0 and wcs["h"] > 0
    except (TypeError, KeyError, IndexError):
        return False


def outline(wcs: dict, per_side: int = 4) -> list:
    """Field outline (corners + points along the edges) as [[ra, dec], …]."""
    w, h = wcs["w"], wcs["h"]
    x0, x1, y0, y1 = 0.5, w + 0.5, 0.5, h + 0.5
    pts = []
    for i in range(per_side):
        t = i / per_side
        pts.append((x0 + (x1 - x0) * t, y0))
    for i in range(per_side):
        t = i / per_side
        pts.append((x1, y0 + (y1 - y0) * t))
    for i in range(per_side):
        t = i / per_side
        pts.append((x1 - (x1 - x0) * t, y1))
    for i in range(per_side):
        t = i / per_side
        pts.append((x0, y1 - (y1 - y0) * t))
    return [list(tan_to_radec(wcs, x, y)) for x, y in pts]


def constellation(ra, dec) -> str | None:
    try:
        from astropy.coordinates import SkyCoord, get_constellation
        import astropy.units as u
        return str(get_constellation(SkyCoord(ra * u.deg, dec * u.deg, frame="icrs")))
    except Exception:
        return None


def reference_lines() -> dict:
    """Ecliptic (J2000 mean) and galactic equator as RA/Dec polylines."""
    global _LINES
    if _LINES is None:
        import numpy as np
        import astropy.units as u
        from astropy.coordinates import SkyCoord, BarycentricMeanEcliptic, Galactic
        lon = np.arange(0, 361, 2.0)
        ecl = SkyCoord(lon * u.deg, np.zeros_like(lon) * u.deg, frame=BarycentricMeanEcliptic()).icrs
        gal = SkyCoord(l=lon * u.deg, b=np.zeros_like(lon) * u.deg, frame=Galactic()).icrs
        gc = SkyCoord(l=0 * u.deg, b=0 * u.deg, frame=Galactic()).icrs
        r = lambda c: [[round(float(a), 3), round(float(d), 3)] for a, d in zip(c.ra.deg, c.dec.deg)]
        _LINES = {"ecliptic": r(ecl), "galactic": r(gal),
                  "galactic_centre": [round(float(gc.ra.deg), 3), round(float(gc.dec.deg), 3)]}
    return _LINES


def catalogue_points(max_mag: float = 10.0) -> list:
    from .platesolve import catalogue
    out = []
    for o in catalogue():
        if o["messier"] or (o["mag"] is not None and o["mag"] <= max_mag):
            out.append({"n": o["messier"] or o["name"], "alt": o["name"] if o["messier"] else None,
                        "c": o["common"], "t": o["type_label"], "ra": round(o["ra"], 4), "dec": round(o["dec"], 4),
                        "mag": o["mag"], "maj": o["maj"], "m": bool(o["messier"])})
    return out


def project_fields(db) -> list:
    """One solved field per session (the latest solve) for every project, plus the catalogue position of the
    project's target when it is known."""
    from sqlalchemy import select, cast, JSON
    from .models import FrameFile, Project
    from .platesolve import find_object
    sj = cast(FrameFile.solve, JSON)
    q = (select(FrameFile.project_id, FrameFile.session_id, FrameFile.id, sj["wcs"].label("wcs"),
                sj["ok"].label("ok"), FrameFile.date_obs)
         .where(FrameFile.solve.is_not(None), FrameFile.project_id.is_not(None))
         .distinct(FrameFile.project_id, FrameFile.session_id)
         .order_by(FrameFile.project_id, FrameFile.session_id, FrameFile.id.desc()))
    fields = {}
    for pid, sid, fid, wcs, ok, d in db.execute(q):
        if not ok or not valid_wcs(wcs):
            continue
        try:
            fields.setdefault(pid, []).append({"file": fid, "session": sid, "outline": outline(wcs),
                                               "ra": round(wcs["crval"][0], 4), "dec": round(wcs["crval"][1], 4),
                                               "date": d.date().isoformat() if d else None})
        except (TypeError, ValueError, KeyError, IndexError, ZeroDivisionError):
            continue                                 # one odd solve must not break the whole map
    out = []
    for pid, name, target in db.execute(select(Project.id, Project.name, Project.target)):
        fl = fields.get(pid, [])
        obj = find_object(target) or find_object(name)
        if not fl and not obj:
            continue
        ra, dec = (fl[0]["ra"], fl[0]["dec"]) if fl else (obj["ra"], obj["dec"])
        out.append({"id": pid, "name": name, "target": target, "fields": fl,
                    "obj": {"n": obj["messier"] or obj["name"], "ra": round(obj["ra"], 4),
                            "dec": round(obj["dec"], 4)} if obj else None,
                    "ra": ra, "dec": dec, "con": constellation(ra, dec)})
    return out


def skymap_data(db, max_mag: float = 10.0) -> dict:
    key = max_mag
    from sqlalchemy import select, func
    from .models import FrameFile, Project
    # changes when a solve is added / replaced (length + id sum) or a project is added / renamed / retargeted
    from sqlalchemy.dialects.postgresql import aggregate_order_by
    proj = func.concat(Project.id, ":", Project.name, ":", func.coalesce(Project.target, ""))
    stamp = (*db.execute(select(func.count(FrameFile.solve), func.sum(func.length(FrameFile.solve)),
                                func.sum(FrameFile.id).filter(FrameFile.solve.is_not(None)))).one(),
             db.scalar(select(func.md5(func.string_agg(proj, aggregate_order_by("|", Project.id))))))
    hit = _CACHE.get(key)
    if hit and hit[0] == stamp and time.time() - hit[1] < 300:
        return hit[2]
    data = {"projects": project_fields(db), "catalogue": catalogue_points(max_mag), **reference_lines()}
    _CACHE[key] = (stamp, time.time(), data)
    return data
