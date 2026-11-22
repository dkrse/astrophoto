"""Calibration check of a session: do the linked library sets fit its light frames?

Rules (what makes a master usable for the lights):
  darks      same camera, gain, offset, binning; exposure equal (else only scalable with a bias); sensor
             temperature within DARK_TEMP_TOL (cooled cameras); not older than DARK_MAX_AGE_D (sensor ageing,
             new hot pixels)
  bias       same camera, gain, offset, binning
  flats      same camera and telescope, every filter of the lights covered; taken the same night / setup –
             a flat from another night is only valid if nothing in the optical train moved (rotation, focus,
             dust): info after 0 days, warning after FLAT_MAX_AGE_D
  dark flats / bias for the flats: flats need one of them; dark flats with the exposure of the flats
Missing values (unknown camera, gain …) are never reported as mismatches – only what is known on both sides.
Binning / offset of the lights come from the FITS header of one light frame (XBINNING / YBINNING, OFFSET or
BLKLEVEL), read only when a linked set states them."""
from .sessets import norm_gain, norm_filter, parse_temp

DARK_TEMP_TOL = 2.0              # °C
DARK_EXP_TOL = 0.02              # relative
DARK_MAX_AGE_D = 365
FLAT_MAX_AGE_D = 30


def _eq(a, b) -> bool | None:
    """Text equality ignoring case / spaces; None when one side is unknown."""
    a, b = (str(x).strip().casefold() if x not in (None, "") else None for x in (a, b))
    return None if a is None or b is None else a == b


def _gain_eq(a, b):
    return _eq(norm_gain(a), norm_gain(b))


def _binning(x, y=None) -> str | None:
    """'2', '2x2', (2, 2) -> '2x2'; None when unknown."""
    try:
        bx = int(float(str(x).strip()))
        by = int(float(str(y).strip())) if y not in (None, "") else bx
    except (TypeError, ValueError):
        return None
    return f"{bx}x{by}" if bx > 0 and by > 0 else None


def _label(c) -> str:
    return f"“{c.name}”"


def check(s, lights: list, sets: list | None = None) -> list:
    """Findings for session s with its light frames (FrameFile-like) and linked sets (default s.calib_sets):
    [{'level': ok|info|warn|bad, 'kind': dark|flat|bias|darkflat|general, 'title', 'text'}], worst first."""
    sets = list(s.calib_sets if sets is None else sets)
    out = []

    def add(level, kind, title, text):
        out.append({"level": level, "kind": kind, "title": title, "text": text})

    lights = [f for f in lights if f.frame_type == "light" and not getattr(f, "rejected", False)]
    if not lights:
        return []
    by = {t: [c for c in sets if c.frame_type == t] for t in ("dark", "bias", "flat", "darkflat")}
    exps = sorted({round(f.exposure_s, 2) for f in lights if f.exposure_s} | ({round(s.exposure_s, 2)} if s.exposure_s else set()))
    gain = s.gain or next((f.gain for f in lights if f.gain), None)
    filters = sorted({norm_filter(f.filter) for f in lights if norm_filter(f.filter)} |
                     ({norm_filter(s.filter)} if norm_filter(s.filter) else set()), key=str.casefold)
    temp = s.cam_temp if s.cam_temp is not None else next((parse_temp(f.temperature) for f in lights
                                                          if parse_temp(f.temperature) is not None), None)

    hdr_cache = []

    def light_header() -> dict:
        """XBINNING / OFFSET … of one light frame – read only when a linked set states binning or offset."""
        if not hdr_cache:
            from .platesolve import _hdr
            f = next((f for f in lights if getattr(f, "has_header", True) and getattr(f, "header", None)), None)
            hdr_cache.append(_hdr(f.header) if f is not None else {})
        return hdr_cache[0]

    def common(c, kind):
        """Camera / gain / binning / offset mismatches shared by all frame types (flats: camera + binning)."""
        if _eq(c.camera, s.camera) is False:
            add("bad", kind, f"Other camera: {_label(c)}", f"{c.camera} vs. the session's {s.camera}.")
        if kind in ("dark", "bias", "darkflat") and _gain_eq(c.gain, gain) is False:
            add("bad", kind, f"Other gain: {_label(c)}", f"Gain {c.gain} vs. {gain} of the lights – the offset / "
                                                         f"read-out pattern differs, the master does not fit.")
        if getattr(c, "binning", None):
            h = light_header()
            lb = _binning(h.get("XBINNING"), h.get("YBINNING"))
            if lb and _binning(*str(c.binning).lower().replace("×", "x").split("x")[:2]) not in (None, lb):
                add("bad", kind, f"Other binning: {_label(c)}", f"Binning {c.binning} vs. {lb} of the lights.")
        if kind in ("dark", "bias", "darkflat") and getattr(c, "offset", None):
            h = light_header()
            lo = h.get("OFFSET") if h.get("OFFSET") is not None else h.get("BLKLEVEL")
            if _gain_eq(c.offset, lo) is False:
                add("warn", kind, f"Other offset: {_label(c)}", f"Offset {c.offset} vs. {lo} of the lights – "
                                                                f"the bias level differs.")

    # ---- darks ----
    if not by["dark"]:
        add("warn" if not by["bias"] else "info", "dark", "No darks linked",
            "Thermal signal and hot pixels stay in the lights (dithering + bad-pixel rejection help)."
            + (" A bias alone removes only the offset." if by["bias"] else ""))
    for c in by["dark"]:
        common(c, "dark")
        if c.exposure_s and exps and not any(abs(c.exposure_s - e) <= DARK_EXP_TOL * e for e in exps):
            add("warn" if by["bias"] else "bad", "dark", f"Other exposure: {_label(c)}",
                f"Darks {c.exposure_s:g} s vs. lights {', '.join(f'{e:g}' for e in exps)} s – "
                + ("only usable scaled with the bias (dark optimisation)." if by["bias"] else
                   "not usable without a bias to scale them."))
        ct = parse_temp(c.temperature)
        if ct is not None and temp is not None and abs(ct - temp) > DARK_TEMP_TOL:
            add("warn", "dark", f"Other sensor temperature: {_label(c)}",
                f"Darks at {ct:g} °C, lights at {temp:g} °C (dark current roughly doubles every ~6 °C).")
        if c.capture_date and s.date and (s.date - c.capture_date).days > DARK_MAX_AGE_D:
            add("info", "dark", f"Old darks: {_label(c)}",
                f"Taken {(s.date - c.capture_date).days} days before the session – new hot pixels may be missing.")
    # ---- bias ----
    for c in by["bias"]:
        common(c, "bias")
    # ---- flats ----
    if not by["flat"]:
        add("warn", "flat", "No flats linked", "Vignetting and dust shadows stay in the image; gradients are "
                                               "harder to remove.")
    for c in by["flat"]:
        common(c, "flat")
        if _eq(c.telescope, s.telescope) is False:
            add("bad", "flat", f"Other telescope: {_label(c)}", f"{c.telescope} vs. {s.telescope} – vignetting "
                                                                f"and dust differ.")
        if c.capture_date and s.date:
            age = abs((s.date - c.capture_date).days)
            if age > FLAT_MAX_AGE_D:
                add("warn", "flat", f"Flats from another month: {_label(c)}",
                    f"{age} days from the session – only valid if the camera was not rotated / removed and the "
                    f"dust did not move.")
            elif age > 1:
                add("info", "flat", f"Flats from another night: {_label(c)}",
                    f"{age} days from the session – fine if the camera rotation and focus were unchanged.")
    flat_filters = {norm_filter(c.filter).casefold() for c in by["flat"] if norm_filter(c.filter)}
    if by["flat"] and flat_filters:
        missing = [f for f in filters if f.casefold() not in flat_filters]
        if missing:
            add("bad", "flat", "Flats missing for a filter",
                f"No flat for {', '.join(missing)} (linked: {', '.join(sorted(flat_filters))}) – flats are "
                f"per filter (dust and vignetting change with it).")
    # ---- calibration of the flats ----
    if by["flat"] and not by["bias"] and not by["darkflat"]:
        add("warn", "darkflat", "Flats without bias / dark flats",
            "The flats keep the camera offset – the flat correction is then wrong (over / under-corrected "
            "vignetting). Link a bias or dark flats.")
    for c in by["darkflat"]:
        common(c, "darkflat")
        fexp = {round(f.exposure_s, 2) for f in by["flat"] if f.exposure_s}
        if c.exposure_s and fexp and not any(abs(c.exposure_s - e) <= DARK_EXP_TOL * max(e, 0.01) for e in fexp):
            add("warn", "darkflat", f"Dark flats with another exposure: {_label(c)}",
                f"{c.exposure_s:g} s vs. flats {', '.join(f'{e:g}' for e in sorted(fexp))} s.")
    if not out:
        add("ok", "general", "Calibration fits", "Darks, flats and their calibration match the light frames.")
    order = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
    return sorted(out, key=lambda x: order[x["level"]])


def summary(findings: list) -> dict:
    lv = [f["level"] for f in findings]
    worst = "bad" if "bad" in lv else ("warn" if "warn" in lv else ("info" if "info" in lv else ("ok" if lv else None)))
    return {"level": worst, "problems": sum(1 for x in lv if x in ("bad", "warn"))}
