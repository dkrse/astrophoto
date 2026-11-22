"""Mosaic planning and coverage of a project.

A plan (stored as JSON in projects.mosaic) is a grid of cols × rows panels of one camera field (fov_w × fov_h
arcmin) with an overlap, centred on (ra, dec) and turned by `rot` – the position angle (north through east) of
the panels' "up" axis, the same quantity as the camera rotation of a plate solve. Panel 1 is the top-left one
seen north up / east left (for rot = 0), numbered row by row.

Coverage is measured from the plate solves of the project's light frames: sample points of every panel are
tested against the exact TAN field of each solved frame. Integration is attributed per session: a session whose
solved frames all fall into one panel gives it all its light integration; a session spread over several panels
(several panels shot in one night) is split in proportion to its solved frames per panel. Everything is pure
geometry on J2000 coordinates – no network, no catalogue."""
import json
import math

import numpy as np

LIMITS = {"cols": (1, 10), "rows": (1, 10), "overlap": (0.0, 60.0), "fov_w": (1.0, 1200.0),
          "fov_h": (1.0, 1200.0), "rot": (-360.0, 360.0), "ra": (0.0, 360.0), "dec": (-90.0, 90.0)}
SAMPLES = 12                      # sample points per panel side for the coverage
DONE, PARTIAL = 0.9, 0.1          # covered fraction: ≥ DONE complete, < PARTIAL missing


# ---------- gnomonic projection (degrees) ----------
def to_plane(ra, dec, ra0, dec0):
    """(ra, dec) -> standard coordinates (xi east, eta north) in degrees around (ra0, dec0); NaN behind the plane."""
    r = np.radians
    ra, dec = np.asarray(ra, dtype=float), np.asarray(dec, dtype=float)
    d = r(ra - ra0)
    cosc = np.sin(r(dec0)) * np.sin(r(dec)) + np.cos(r(dec0)) * np.cos(r(dec)) * np.cos(d)
    with np.errstate(divide="ignore", invalid="ignore"):
        xi = np.cos(r(dec)) * np.sin(d) / cosc
        eta = (np.cos(r(dec0)) * np.sin(r(dec)) - np.sin(r(dec0)) * np.cos(r(dec)) * np.cos(d)) / cosc
    bad = cosc <= 0
    return np.where(bad, np.nan, np.degrees(xi)), np.where(bad, np.nan, np.degrees(eta))


def from_plane(xi, eta, ra0, dec0):
    """Standard coordinates (degrees) -> (ra, dec) in degrees."""
    xi, eta = np.radians(np.asarray(xi, dtype=float)), np.radians(np.asarray(eta, dtype=float))
    d0 = math.radians(dec0)
    den = math.cos(d0) - eta * math.sin(d0)
    ra = math.radians(ra0) + np.arctan2(xi, den)
    dec = np.arctan2(eta * math.cos(d0) + math.sin(d0), np.hypot(xi, den))
    return np.degrees(ra) % 360, np.degrees(dec)


def _axes(rot: float):
    """Unit vectors (xi, eta) of the panel grid: 'right' (columns) and 'down' (rows) for position angle rot."""
    c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    rot_v = lambda x, y: (x * c + y * s, -x * s + y * c)       # turns north (0, 1) to PA rot: (sin, cos)
    return rot_v(-1.0, 0.0), rot_v(0.0, -1.0)                   # west = right, south = down (north up, east left)


# ---------- plan ----------
def normalize(plan: dict | None) -> dict | None:
    """Validated plan with numbers in range, or None (missing / invalid values)."""
    if not isinstance(plan, dict):
        return None
    out = {}
    for k, (lo, hi) in LIMITS.items():
        try:
            v = float(plan.get(k))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(v):
            return None
        if k in ("cols", "rows"):
            v = int(round(v))
        if k == "ra":
            v %= 360
        if not lo <= v <= hi:
            return None
        out[k] = v
    return out


def load(text: str | None) -> dict | None:
    try:
        return normalize(json.loads(text)) if text else None
    except ValueError:
        return None


def panels(plan: dict) -> list:
    """Panels of a plan: number, row, column, centre (ra, dec), corners [[ra, dec] ×4] and plane polygon."""
    right, down = _axes(plan["rot"])
    fw, fh = plan["fov_w"] / 60, plan["fov_h"] / 60
    step_c, step_r = fw * (1 - plan["overlap"] / 100), fh * (1 - plan["overlap"] / 100)
    out = []
    for r in range(plan["rows"]):
        for c in range(plan["cols"]):
            oc, orow = (c - (plan["cols"] - 1) / 2) * step_c, (r - (plan["rows"] - 1) / 2) * step_r
            cx, cy = oc * right[0] + orow * down[0], oc * right[1] + orow * down[1]
            poly = [(cx + sx * fw / 2 * right[0] + sy * fh / 2 * down[0],
                     cy + sx * fw / 2 * right[1] + sy * fh / 2 * down[1]) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
            ra, dec = from_plane(cx, cy, plan["ra"], plan["dec"])
            cra, cdec = from_plane([p[0] for p in poly], [p[1] for p in poly], plan["ra"], plan["dec"])
            out.append({"n": r * plan["cols"] + c + 1, "row": r + 1, "col": c + 1,
                        "ra": round(float(ra), 5), "dec": round(float(dec), 5), "xi": cx, "eta": cy,
                        "plane": [[round(x, 5), round(y, 5)] for x, y in poly],
                        "corners": [[round(float(a), 5), round(float(b), 5)] for a, b in zip(cra, cdec)]})
    return out


def defaults(field: dict | None, target: dict | None) -> dict | None:
    """A 1 × 1 plan (to start from) from the latest solved field (size, rotation) centred on the target."""
    if not field:
        return None
    w = field["wcs"]
    (c11, c12), (c21, c22) = w["cd"]
    rot = math.degrees(math.atan2(c12, c22))                    # PA of the image's +y axis
    fw = math.hypot(c11, c21) * w["w"] * 60
    fh = math.hypot(c12, c22) * w["h"] * 60
    ra, dec = (target["ra"], target["dec"]) if target else tuple(w["crval"])
    return normalize({"cols": 1, "rows": 1, "overlap": 15, "fov_w": round(fw, 1), "fov_h": round(fh, 1),
                      "rot": round(rot, 1), "ra": ra, "dec": dec})


# ---------- coverage ----------
def _inside(wcs: dict, ra, dec):
    """Boolean mask: points (ra, dec) inside the solved field (pixel edges 0.5 … w + 0.5, FITS 1-based)."""
    xi, eta = to_plane(ra, dec, wcs["crval"][0], wcs["crval"][1])
    inv = np.linalg.inv(np.array(wcs["cd"], dtype=float))
    dx = inv[0, 0] * xi + inv[0, 1] * eta
    dy = inv[1, 0] * xi + inv[1, 1] * eta
    x, y = wcs["crpix"][0] + dx, wcs["crpix"][1] + dy
    with np.errstate(invalid="ignore"):
        return (x >= 0.5) & (x <= wcs["w"] + 0.5) & (y >= 0.5) & (y <= wcs["h"] + 0.5)


def _samples(plan: dict, p: dict):
    right, down = _axes(plan["rot"])
    fw, fh = plan["fov_w"] / 60, plan["fov_h"] / 60
    t = (np.arange(SAMPLES) + 0.5) / SAMPLES - 0.5
    a, b = np.meshgrid(t * fw, t * fh)
    xi = p["xi"] + a * right[0] + b * down[0]
    eta = p["eta"] + a * right[1] + b * down[1]
    return from_plane(xi.ravel(), eta.ravel(), plan["ra"], plan["dec"])


def coverage(plan: dict, frames: list, sessions: dict) -> dict:
    """frames: [{'session': id, 'wcs': {...}}] of solved light frames; sessions: {id: {'date', 'integration',
    'lights'}}. Returns panels with covered fraction, status, integration and sessions, plus summary."""
    from .skymap import valid_wcs, outline
    pns = panels(plan)
    frames = [f for f in frames if valid_wcs(f.get("wcs"))]
    pts = [_samples(plan, p) for p in pns]
    covered = [np.zeros(len(pt[0]), dtype=bool) for pt in pts]
    per_session = {}                                             # session -> panel -> solved frames
    fields, seen = [], set()
    for f in frames:
        w = f["wcs"]
        best, best_frac = None, 0.0
        for i, (ra, dec) in enumerate(pts):
            m = _inside(w, ra, dec)
            covered[i] |= m
            frac = float(m.mean())
            if frac > best_frac:
                best, best_frac = i, frac
        if best is not None and best_frac >= 0.25:
            ps = per_session.setdefault(f["session"], {})
            ps[best] = ps.get(best, 0) + 1
        key = (round(w["crval"][0], 2), round(w["crval"][1], 2), w["w"], w["h"])
        if key not in seen and len(fields) < 300:                # outlines for the drawing (distinct pointings)
            seen.add(key)
            ol = outline(w)
            xi, eta = to_plane([q[0] for q in ol], [q[1] for q in ol], plan["ra"], plan["dec"])
            if np.all(np.isfinite(xi)):
                fields.append({"session": f["session"], "plane": [[round(float(a), 5), round(float(b), 5)]
                                                                  for a, b in zip(xi, eta)]})
    integ = [0.0] * len(pns)
    who = [dict() for _ in pns]
    for sid, by_panel in per_session.items():
        s = sessions.get(sid) or {}
        total = sum(by_panel.values())
        for i, n in by_panel.items():
            share = (s.get("integration") or 0.0) * n / total
            integ[i] += share
            who[i][sid] = who[i].get(sid, 0.0) + share
    out = []
    for i, p in enumerate(pns):
        frac = float(covered[i].mean())
        out.append({**p, "covered": round(frac, 3), "integration": round(integ[i], 1),
                    "status": "done" if frac >= DONE else ("partial" if frac >= PARTIAL else "missing"),
                    "sessions": [{"id": sid, "date": (sessions.get(sid) or {}).get("date"), "integration": round(v, 1)}
                                 for sid, v in sorted(who[i].items(), key=lambda kv: -kv[1])]})
    integr = [p["integration"] for p in out]
    return {"plan": plan, "panels": out, "fields": fields,
            "summary": {"panels": len(out), "done": sum(p["status"] == "done" for p in out),
                        "partial": sum(p["status"] == "partial" for p in out),
                        "missing": sum(p["status"] == "missing" for p in out),
                        "solved_frames": len(frames), "integration": round(sum(integr), 1),
                        "min_integration": round(min(integr), 1) if integr else 0.0}}


def project_data(db, p) -> dict:
    """Plan (stored or a default from the latest solve) + coverage of project p."""
    from sqlalchemy import select, func, cast, JSON
    from .models import FrameFile, ImagingSession
    from .platesolve import find_object
    sj = cast(FrameFile.solve, JSON)
    rows = db.execute(select(FrameFile.session_id, sj["wcs"]).where(
        FrameFile.project_id == p.id, FrameFile.frame_type == "light", FrameFile.rejected.is_not(True),
        FrameFile.solve.is_not(None), sj["ok"].as_boolean().is_(True)).order_by(FrameFile.id.desc())).all()
    frames = [{"session": sid, "wcs": w} for sid, w in rows if w]
    sess = {}
    for sid, d, n, integ in db.execute(
            select(ImagingSession.id, ImagingSession.date, func.count(FrameFile.id),
                   func.coalesce(func.sum(FrameFile.exposure_s), 0.0))
            .outerjoin(FrameFile, (FrameFile.session_id == ImagingSession.id) & (FrameFile.frame_type == "light")
                       & FrameFile.rejected.is_not(True))
            .where(ImagingSession.project_id == p.id).group_by(ImagingSession.id, ImagingSession.date)):
        sess[sid] = {"date": d.isoformat() if d else None, "lights": n, "integration": float(integ or 0)}
    stored = load(p.mosaic)
    target = find_object(p.target) or find_object(p.name)
    dflt = defaults(frames[0] if frames else None, {"ra": target["ra"], "dec": target["dec"]} if target else None)
    plan = stored or dflt
    if not plan:
        return {"plan": None, "stored": False, "solved_frames": 0, "defaults": None,
                "error": "Plate-solve at least one light frame (🎯 Solve) to plan a mosaic from its field of view."}
    out = coverage(plan, frames, sess)
    out["stored"] = stored is not None
    out["defaults"] = dflt
    out["target"] = {"name": target["messier"] or target["name"], "ra": target["ra"], "dec": target["dec"]} \
        if target else None
    if target:
        xi, eta = to_plane(target["ra"], target["dec"], plan["ra"], plan["dec"])
        out["target"]["plane"] = [round(float(xi), 5), round(float(eta), 5)]
        out["target"]["maj"] = target.get("maj")
    unassigned = sum(1 for sid in sess if sess[sid]["lights"]
                     and not any(sid == s["id"] for pn in out["panels"] for s in pn["sessions"]))
    out["summary"]["sessions_unplaced"] = unassigned
    return out
