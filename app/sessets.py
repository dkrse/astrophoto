"""One session = one night with ONE acquisition set: exposure, gain / ISO, filter and camera temperature.

Light frames whose set differs from their session's set are moved to a sibling session of the same night
(same project, same date) with that set – created when none exists yet (equipment, conditions and linked
calibration sets are copied). Calibration frames and stacks are never moved by this rule.

Matching tolerances (camera values jitter from frame to frame):
  exposure    rounded to 0.1 s (phone / DSLR: 10.024 s == 10.045 s)
  gain / ISO  "101.0" == "101", case-insensitive text otherwise ("ISO 800")
  filter      case-insensitive, trimmed
  camera temp within ±1.5 °C of the session's set point (integer °C); only for cooled sensors: "Cooled" in
              the equipment list, or – camera not described there – a SET-TEMP in the frame header
"""
import re

from sqlalchemy import select, func

from .models import FrameFile, ImagingSession, Project, CatalogItem

SET_FIELDS = ("exposure_s", "gain", "filter", "cam_temp")
TEMP_TOL = 1.5


def norm_exposure(v):
    try:
        return round(float(v), 1) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def norm_gain(v):
    s = str(v).strip() if v is not None else ""
    if not s:
        return None
    try:
        x = float(s)
        return str(int(x)) if x == int(x) else str(x)
    except ValueError:
        return s[:50]


def norm_filter(v):
    s = str(v).strip() if v is not None else ""
    return s[:50] or None


def parse_temp(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace("°C", "").replace("°", "").replace(",", ".").strip())
    except ValueError:
        return None


def cooling(db, camera: str | None):
    """True = cooled, False = uncooled (equipment list), None = unknown camera / not specified."""
    if not camera:
        return None
    it = db.scalars(select(CatalogItem).where(CatalogItem.kind == "camera", CatalogItem.name == camera)).first()
    v = it.spec.get("cooled") if it else None
    return True if v == "Cooled" else False if v == "Uncooled" else None


def uncooled(db, camera: str | None) -> bool:
    return cooling(db, camera) is False


_SETTEMP = re.compile(r"^SET-TEMP\s*=", re.M)


def ignore_temp(cool, f: FrameFile) -> bool:
    """Camera temperature splits sessions only for cooled sensors: equipment list says so, or – camera not
    described – the frame's header carries a cooler set point (SET-TEMP)."""
    if cool is not None:
        return not cool
    return not _SETTEMP.search(f.header or "")


def frame_key(f: FrameFile, ignore_temp: bool = False) -> dict:
    t = None if ignore_temp else parse_temp(f.temperature)
    return {"exposure_s": norm_exposure(f.exposure_s), "gain": norm_gain(f.gain), "filter": norm_filter(f.filter),
            "cam_temp": float(round(t)) if t is not None else None}


def _eq_text(a, b):
    return (a or "").casefold() == (b or "").casefold()


def matches(s: ImagingSession, key: dict) -> bool:
    if norm_exposure(s.exposure_s) != key["exposure_s"] or not _eq_text(norm_gain(s.gain), key["gain"]) \
            or not _eq_text(norm_filter(s.filter), key["filter"]):
        return False
    if s.cam_temp is None or key["cam_temp"] is None:
        return s.cam_temp is None and key["cam_temp"] is None
    return abs(s.cam_temp - key["cam_temp"]) <= TEMP_TOL


def session_key(s: ImagingSession) -> dict:
    """The set of a session in the form frame_key() gives for a frame."""
    return {"exposure_s": norm_exposure(s.exposure_s), "gain": norm_gain(s.gain), "filter": norm_filter(s.filter),
            "cam_temp": s.cam_temp}


def same_set(a: ImagingSession, b: ImagingSession) -> bool:
    """Same acquisition set with the tolerances of matches() ('101' = '101.0', camera temperature ±1.5 °C)."""
    return matches(a, session_key(b))


def same_rig(a: ImagingSession, b: ImagingSession) -> bool:
    """Not two different telescopes / cameras (an empty field matches anything)."""
    return all(not getattr(a, k) or not getattr(b, k) or _eq_text(getattr(a, k), getattr(b, k))
               for k in ("telescope", "camera"))


def has_set(s: ImagingSession) -> bool:
    return any(getattr(s, k) is not None for k in SET_FIELDS)


def other_lights(db, s: ImagingSession, exclude_id=None) -> int:
    """Processed lights of `s` (frames still importing / pending have no metadata yet and do not count)."""
    q = select(func.count(FrameFile.id)).where(FrameFile.session_id == s.id, FrameFile.frame_type == "light",
                                               FrameFile.status.in_(["done", "error"]))
    if exclude_id:
        q = q.where(FrameFile.id != exclude_id)
    return db.scalar(q) or 0


def apply_key(s: ImagingSession, key: dict):
    for k in SET_FIELDS:
        setattr(s, k, key[k])


def accepts(db, s: ImagingSession, f: FrameFile, key: dict | None = None) -> bool:
    """True when light `f` may sit in `s`: same set, or `s` is still open (no set and no other lights)."""
    if f.status not in ("done", "error"):
        return True                               # set not known yet – the worker places it when processed
    key = key or frame_key(f, ignore_temp(cooling(db, s.camera), f))
    return matches(s, key) or (not has_set(s) and not other_lights(db, s, f.id))


COPY_FIELDS = ("name", "date", "location", "telescope", "camera", "mount", "software", "seeing", "sqm",
               "temperature", "humidity", "moon_illum")


def sibling_for(db, base: ImagingSession, key: dict) -> ImagingSession:
    """Session of base's night with set `key`: an existing one, else a new copy of `base`."""
    for s in db.scalars(select(ImagingSession).where(ImagingSession.project_id == base.project_id,
                                                     ImagingSession.id != base.id,
                                                     ImagingSession.date == base.date
                                                     if base.date else ImagingSession.date.is_(None))
                        .order_by(ImagingSession.id)):
        if matches(s, key):                       # (an all-empty key reuses an unset sibling – no 1-frame twins)
            return s
    s = ImagingSession(project_id=base.project_id, **{k: getattr(base, k) for k in COPY_FIELDS})
    apply_key(s, key)
    s.calib_sets = list(base.calib_sets)
    db.add(s)
    db.flush()
    return s


def lock_project(db, project_id):
    """Serialise set decisions per project (worker + requests could otherwise create twin sessions)."""
    db.execute(select(Project.id).where(Project.id == project_id).with_for_update())


def place_light(db, f: FrameFile, base: ImagingSession | None = None,
                processed_only: bool = True) -> ImagingSession | None:
    """Puts light `f` into the session of base's night (default: its current session) whose set matches.
    An open session adopts the frame's set. Returns the session the frame ends up in.
    Frames still importing / pending have no metadata yet: left where they are (the worker places them once
    their header is read – it calls this with processed_only=False)."""
    if f.frame_type != "light" or not f.project_id:
        return f.session
    if processed_only and f.status not in ("done", "error"):
        return f.session
    base = base or (db.get(ImagingSession, f.session_id) if f.session_id else None)
    if base is None or base.project_id != f.project_id:
        return None
    lock_project(db, f.project_id)
    key = frame_key(f, ignore_temp(cooling(db, base.camera), f))
    if matches(base, key):
        target = base
    elif not has_set(base):
        # open session: its set is decided by the majority once no light in it is waiting for metadata
        # (a single focus test frame processed first must not push the real batch into another session)
        f.session_id = base.id
        db.flush()
        if not waiting_lights(db, base, f.id):
            split_session(db, base, f.id)        # f itself is still "pending" while the worker places it
        return db.get(ImagingSession, f.session_id)
    else:
        target = sibling_for(db, base, key)
    f.session_id = target.id
    db.flush()
    return target


def waiting_lights(db, s: ImagingSession, exclude_id=None) -> int:
    q = select(func.count(FrameFile.id)).where(FrameFile.session_id == s.id, FrameFile.frame_type == "light",
                                               FrameFile.status.in_(["importing", "pending"]))
    if exclude_id:
        q = q.where(FrameFile.id != exclude_id)
    return db.scalar(q) or 0


def split_session(db, s: ImagingSession, include_id=None) -> int:
    """Processed lights of `s` into per-set sessions; an unset session takes the largest set. Returns moved count."""
    lights = list(db.scalars(select(FrameFile).where(FrameFile.session_id == s.id, FrameFile.frame_type == "light",
                                                     FrameFile.status.in_(["done", "error"]) | (FrameFile.id == include_id))
                             .order_by(FrameFile.date_obs.asc().nulls_last(), FrameFile.id)))
    if not lights:
        return 0
    cool = cooling(db, s.camera)
    keys = {f.id: frame_key(f, ignore_temp(cool, f)) for f in lights}
    if not any(matches(s, k) for k in keys.values()):
        groups = {}
        for k in keys.values():
            t = tuple(sorted(k.items()))
            groups[t] = groups.get(t, 0) + 1
        apply_key(s, dict(max(groups.items(), key=lambda kv: kv[1])[0]))
    moved = 0
    for f in lights:
        if not matches(s, keys[f.id]):
            f.session_id = sibling_for(db, s, keys[f.id]).id
            moved += 1
    db.flush()
    return moved


def split_project(db, p: Project) -> int:
    """Sorts all lights of a project into per-set sessions (the largest set stays in each original session).
    Returns the number of frames moved."""
    lock_project(db, p.id)
    return sum(split_session(db, s) for s in list(db.scalars(
        select(ImagingSession).where(ImagingSession.project_id == p.id).order_by(ImagingSession.id))))


def set_label(s) -> str:
    """'120 s · gain 101 · L · −10 °C' (empty when the session has no set)."""
    parts = []
    if s.exposure_s is not None:
        e = s.exposure_s
        parts.append(f"{e:g} s")
    if s.gain:
        g = norm_gain(s.gain)
        parts.append(g if not g[:1].isdigit() else f"gain {g}")
    if s.filter:
        parts.append(s.filter)
    if s.cam_temp is not None:
        parts.append(f"{s.cam_temp:g} °C")
    return " · ".join(parts)


# ---------- session names and nights from their files ----------
def majority_night(files):
    """Night most of the files were taken in (lights count first; ties: the earlier night). None without dates."""
    from collections import Counter
    from .core import night_of
    dated = [f for f in files if f.date_obs]
    use = [f for f in dated if f.frame_type == "light"] or dated
    if not use:
        return None
    c = Counter(night_of(f.date_obs) for f in use)
    return min(c, key=lambda d: (-c[d], d))


def fix_sessions(db, p: Project, rename: bool = False) -> dict:
    """Sessions of project p: an empty name (rename=True: every name) becomes the project name; the night becomes the one most of the
    session's files were taken in. A session moving onto a night that already has a session with the same set
    is merged into it (empty fields filled, files and calibration links moved). Returns counts."""
    from .core import moon_illumination
    out = {"named": 0, "redated": 0, "merged": 0}
    lock_project(db, p.id)
    sessions = list(db.scalars(select(ImagingSession).where(ImagingSession.project_id == p.id)
                               .order_by(ImagingSession.id)))
    for s in sessions:
        if (rename or not (s.name or "").strip()) and s.name != p.name:
            s.name = p.name
            out["named"] += 1
    gone = set()
    for s in sessions:
        if s.id in gone:
            continue
        night = majority_night(s.files)
        if not night or night == s.date:
            continue
        twin = next((x for x in sessions if x.id != s.id and x.id not in gone and x.date == night
                     and same_set(x, s) and same_rig(x, s)), None)
        if twin:
            for f in list(s.files):
                f.session_id = twin.id
            for k in ("location", "telescope", "camera", "mount", "software", "seeing", "sqm",
                      "temperature", "humidity", "notes"):
                if getattr(twin, k) in (None, "") and getattr(s, k) not in (None, ""):
                    setattr(twin, k, getattr(s, k))
            twin.calib_sets = list({c.id: c for c in [*twin.calib_sets, *s.calib_sets]}.values())
            db.flush()
            db.expire(s, ["files"])
            db.delete(s)
            gone.add(s.id)
            out["merged"] += 1
        else:
            s.date = night
            s.moon_illum = moon_illumination(night)
            out["redated"] += 1
    db.flush()
    return out
