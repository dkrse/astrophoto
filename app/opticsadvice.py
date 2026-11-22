"""Optics recommendations from the history of one setup (telescope · camera), night by night.

Input: the per-session medians of the 🔬 Optics analyses (see measured.session_measured / optics_trend):
corners_pct (corner FWHM vs. centre, %), tilt_pct (FWHM change across the field, %), tilt_dir (side where stars
grow), findings_share (share of frames with radial / tangential / uniform elongation …) and the corrector
spacing the user logged for the night (backfocus_mm).

What can be concluded from several nights – and not from a single frame:
  * a tilt towards the SAME side of the sensor (read-out orientation, independent of how frames were viewed)
    on most nights is mechanical (camera / adapter / focuser) -> tilt plate;
    a tilt whose side CHANGES is sag, flexure or a camera not seated firmly
  * soft corners vs. the logged spacing: the spacing with the sharpest corners, a parabola fitted through
    (spacing, corners %) when ≥ 3 different spacings were tried, and the direction to continue in
  * lasting changes between nights (fixed / got worse) so that the user can connect them with what changed
  * elongation in one direction on many nights: guiding / tracking, not optics"""
from collections import Counter

TILT_PCT = 20                     # tilt above this (FWHM % across the field) counts (optics.py: warning level)
CORNERS_PCT = 25                  # corners this much larger than the centre count as soft (optics.py)
CHANGE_PTS = 15                   # change of corners / tilt between two nights worth mentioning (points)
RECENT = 3                        # nights looked at for "persistent"


def _fmt_d(r):
    return r.get("date_dmy") or r.get("date") or "?"


def advise(rows: list) -> list:
    """rows: optics_trend rows (one per session). Returns one entry per setup with recommendations."""
    by = {}
    for r in rows:
        if r.get("corners_pct") is None and r.get("tilt_pct") is None and r.get("elong") is None:
            continue                                          # session without optics analyses
        by.setdefault(r["equipment"], []).append(r)
    out = []
    for eq, rs in sorted(by.items()):
        rs = _nights(rs)
        items = _tilt(rs) + _spacing(rs) + _changes(rs) + _guiding(rs)
        if not items:
            items = [{"level": "ok", "title": "No recurring optical problem",
                      "text": f"{len(rs)} night(s) analysed: corners, tilt and star shapes within the limits."}]
        order = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
        out.append({"equipment": eq, "nights": len(rs), "items": sorted(items, key=lambda x: order[x["level"]]),
                    "series": [{"date": r.get("date"), "date_dmy": r.get("date_dmy"), "corners_pct": r.get("corners_pct"),
                                "tilt_pct": r.get("tilt_pct"), "tilt_dir": r.get("tilt_dir"),
                                "backfocus_mm": r.get("backfocus_mm"), "project": r.get("project"),
                                "session_id": r.get("session_id"), "project_id": r.get("project_id")} for r in rs]})
    return out


DIRS = ["right", "bottom right", "bottom", "bottom left", "left", "top left", "top", "top right"]   # optics.py
SAME_SIDE_R = 0.7                 # mean resultant length of the tilt directions that counts as one side


def _mean_dir(pairs):
    """[(side, weight)] -> (side of the weighted circular mean, mean resultant length R 0…1) or (None, 0).
    'top left' and 'left' are 45° apart – nearly the same side; opposite sides cancel."""
    import math
    x = y = wsum = 0.0
    for d, w in pairs:
        if d in DIRS and w:
            a = math.radians(DIRS.index(d) * 45)
            x, y, wsum = x + w * math.cos(a), y + w * math.sin(a), wsum + w
    if not wsum:
        return None, 0.0
    r = math.hypot(x, y) / wsum
    ang = math.degrees(math.atan2(y, x)) % 360
    return DIRS[int((ang + 22.5) // 45) % 8], r


def _med(v):
    v = sorted(x for x in v if x is not None)
    return (v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2) if v else None


def _nights(rs):
    """One row per night of a setup (several projects / sets the same night are one optical state)."""
    by = {}
    for r in rs:
        by.setdefault(r.get("date") or f"#{r.get('session_id')}", []).append(r)
    out = []
    for d, g in sorted(by.items()):
        side, rr = _mean_dir([(r.get("tilt_dir"), r.get("tilt_pct") or 1) for r in g])
        bfs = [r.get("backfocus_mm") for r in g if r.get("backfocus_mm") is not None]
        shares = [r.get("findings_share") for r in g if r.get("findings_share")]
        out.append({**g[0], "corners_pct": _med([r.get("corners_pct") for r in g]),
                    "tilt_pct": _med([r.get("tilt_pct") for r in g]), "elong": _med([r.get("elong") for r in g]),
                    "tilt_dir": side if rr >= SAME_SIDE_R else None,      # sessions of the night disagree: none
                    "tilt_mixed": side is not None and rr < SAME_SIDE_R,   # known sides that disagree (≠ unknown)
                    "backfocus_mm": Counter(bfs).most_common(1)[0][0] if bfs else None,
                    "findings_share": {k: sum(x.get(k, 0) for x in shares) / len(shares) for k in shares[0]}
                    if shares else None,
                    "project": ", ".join(sorted({r.get("project") or "" for r in g}))})
    return out


def _tilt(rs):
    t = [r for r in rs if r.get("tilt_pct") is not None]
    if not t:
        return []
    recent = t[-RECENT:]
    tilted = [r for r in recent if r["tilt_pct"] > TILT_PCT]
    items = []
    known = [r for r in tilted if r.get("tilt_dir") or r.get("tilt_mixed")]
    if len(tilted) >= 2 and len(known) < 2:                  # sides unknown (analyses from before the sensor frame)
        items.append({"level": "info", "title": "Tilt on several nights – side unknown",
                      "text": f"Tilt {', '.join(f'{r['tilt_pct']:.0f} %' for r in tilted)} on {len(tilted)} of the last "
                              f"{len(recent)} nights, but the side is not known for enough of them. Run 🔬 Optics "
                              f"again on a few frames of these nights to tell a mechanical tilt (same side) from "
                              f"sag / flexure (changing side)."})
        return items
    tilted = known if len(tilted) >= 2 else tilted
    if len(tilted) >= 2:
        dirs = [r.get("tilt_dir") for r in tilted if r.get("tilt_dir")]
        side, rr = _mean_dir([(r.get("tilt_dir"), r["tilt_pct"]) for r in tilted])
        if side and len(dirs) >= 2 and rr >= SAME_SIDE_R:
            items.append({"level": "warn", "title": f"Mechanical tilt towards the {side} of the sensor",
                          "text": f"Stars grow towards the same side on {len(tilted)} of the last {len(recent)} "
                                  f"nights ({', '.join(f'{r['tilt_pct']:.0f} % {r.get('tilt_dir') or ''}'.strip() for r in tilted)}). "
                                  f"The same side every night means the camera is tilted against the optical axis: "
                                  f"check the adapters / threads and the focuser drawtube, or correct it with a tilt "
                                  f"plate (adjust on the {side} side of the sensor as read out – row 1 at the top)."})
        else:
            items.append({"level": "warn", "title": "Tilt that changes side",
                          "text": f"Tilt on {len(tilted)} of the last {len(recent)} nights, but towards different "
                                  f"sides ({', '.join(r.get('tilt_dir') or 'mixed within the night' for r in tilted)}). Not a fixed tilt: "
                                  f"focuser sag under the camera weight, flexure, or the camera not seated "
                                  f"firmly – lock the focuser, tighten compression rings, check at different "
                                  f"altitudes. A tilt plate would not help."})
    return items


def _fit_parabola(xs, ys):
    """Minimum of y = a(x − x0)² + c through the points (least squares), or None if not convex."""
    import numpy as np
    a, b, c = np.polyfit(xs, ys, 2)
    if a <= 0:
        return None
    x0 = -b / (2 * a)
    return float(x0), float(a * x0 * x0 + b * x0 + c)


def _spacing(rs):
    c = [r for r in rs if r.get("corners_pct") is not None]
    if not c:
        return []
    soft = [r for r in c[-RECENT:] if r["corners_pct"] > CORNERS_PCT]
    with_bf = [r for r in c if r.get("backfocus_mm") is not None]
    items = []
    if with_bf:
        per = {}
        for r in with_bf:
            per.setdefault(round(r["backfocus_mm"], 2), []).append(r["corners_pct"])
        med = {k: sorted(v)[len(v) // 2] for k, v in per.items()}
        best = min(med, key=med.get)
        tried = sorted(med)
        txt = "Corners vs. spacing: " + ", ".join(f"{k:g} mm → {med[k]:+.0f} %" for k in tried) + "."
        if len(tried) >= 3:
            fit = _fit_parabola(tried, [med[k] for k in tried])
            if fit and tried[0] - 1 <= fit[0] <= tried[-1] + 1:
                inside = tried[0] <= fit[0] <= tried[-1]
                items.append({"level": "info" if med[best] <= CORNERS_PCT else "warn",
                              "title": f"Best spacing ≈ {fit[0]:.1f} mm",
                              "text": txt + f" A parabola through the {len(tried)} spacings has its minimum at "
                                            f"{fit[0]:.1f} mm (≈{max(fit[1], 0):.0f} %)"
                                            + ("." if inside else " – just outside the tested range: try it.")})
                return items
        if best in (tried[0], tried[-1]) and len(tried) >= 2 and med[best] > CORNERS_PCT:
            way = "shorter" if best == tried[0] else "longer"
            items.append({"level": "warn", "title": f"Try a {way} spacing",
                          "text": txt + f" The sharpest corners so far are at the {way} end ({best:g} mm) – continue "
                                        f"in that direction in steps of 0.5–1 mm."})
        elif med[best] > CORNERS_PCT:
            items.append({"level": "warn", "title": "Corners soft at every tested spacing",
                          "text": txt + " No spacing gives sharp corners – check the field flattener matches the "
                                        "telescope, collimation, or the sensor is too large for the corrected field."})
        else:
            items.append({"level": "ok", "title": f"Spacing {best:g} mm gives sharp corners",
                          "text": txt + f" Keep {best:g} mm."})
    elif len(soft) >= 2:
        share = lambda k: sum((r.get("findings_share") or {}).get(k, 0) for r in soft) / len(soft)
        hint = (" Elongation is radial at the edges – typical of a wrong corrector / reducer spacing (or coma)."
                if share("radial") >= 0.4 else
                " Elongation is tangential at the edges – typical of a wrong spacing (the other direction) or "
                "astigmatism." if share("tangential") >= 0.4 else "")
        items.append({"level": "warn", "title": "Soft corners on several nights",
                      "text": f"Corner stars {', '.join(f'{r['corners_pct']:.0f} %' for r in soft)} larger than in "
                              f"the centre on {len(soft)} of the last {min(RECENT, len(c))} nights.{hint} Enter the "
                              f"corrector spacing (backfocus, mm) in the session dialog when you change it – "
                              f"after a few nights the best spacing is computed from your own data."})
    return items


def _changes(rs):
    """The latest lasting change per metric: median of the 2 nights after vs. the 2 nights before differs by
    CHANGE_PTS and crosses the problem level; both nights after the change (so never the latest night alone)
    and the nights before are on their side – single outlier nights are not reported."""
    items = []
    for key, label, lim in (("corners_pct", "Corners", CORNERS_PCT), ("tilt_pct", "Tilt", TILT_PCT)):
        s = [r for r in rs if r.get(key) is not None]
        found = None
        for i in range(1, len(s)):
            before, after = _med([r[key] for r in s[max(0, i - 2):i]]), _med([r[key] for r in s[i:i + 2]])
            side = s[i][key] > lim
            persists = len(s[i:i + 2]) == 2 and all((r[key] > lim) == side for r in s[i:i + 2]) \
                and all((r[key] > lim) != side for r in s[max(0, i - 2):i])   # stable on both sides of the change
            if persists and abs(after - before) >= CHANGE_PTS and (before > lim) != (after > lim):
                found = (s[i - 1], s[i], before, after)
        if found:
            a, b, before, after = found
            bf = ""
            if a.get("backfocus_mm") is not None and b.get("backfocus_mm") is not None \
                    and a["backfocus_mm"] != b["backfocus_mm"]:
                bf = f" The spacing changed {a['backfocus_mm']:g} → {b['backfocus_mm']:g} mm in between."
            better = after < before
            items.append({"level": "ok" if better else "warn",
                          "title": f"{label} {'better' if better else 'worse'} since {_fmt_d(b)}",
                          "text": f"About {before:.0f} % before, {after:.0f} % from {_fmt_d(b)} on (nights around the "
                                  f"change).{bf}" + ("" if bf else " Note in the session what was changed (spacing, "
                                                                  "rotation, collimation, adapter) to keep what works.")})
    return items


def _guiding(rs):
    rec = [r for r in rs[-5:] if r.get("findings_share")]
    bad = [r for r in rec if r["findings_share"].get("uniform", 0) >= 0.5]
    if len(bad) >= 2:
        return [{"level": "warn", "title": "Stars elongated in one direction on several nights",
                 "text": f"On {len(bad)} of the last {len(rec)} nights most frames show the same elongation also in "
                         f"the centre – not an optical error: guiding / tracking (balance, backlash, periodic "
                         f"error, polar alignment), wind or cable drag."}]
    return []
