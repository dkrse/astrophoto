"""Loading, rendering, histogram and metadata for normal images, FITS and camera RAW files.

Nothing in here ever writes to the original file.
"""
import re
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, ImageEnhance

Image.MAX_IMAGE_PIXELS = None
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".gif"}
FITS_EXT = {".fit", ".fits", ".fts", ".fz"}
RAW_EXT = {".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".raf",
           ".orf", ".rw2", ".pef", ".srw", ".raw", ".3fr", ".erf", ".kdc", ".mrw", ".x3f"}
PREVIEW_EXT = IMAGE_EXT | FITS_EXT | RAW_EXT
KNOWN_EXT = PREVIEW_EXT | {".xisf", ".ser", ".avi"}
THUMB_SIDE = 1600
BAYER_PATTERNS = ["RGGB", "BGGR", "GRBG", "GBRG"]

# view options (all string values, used in URLs)
DEFAULT_OPTS = {"bayer": "auto", "stretch": "unlinked", "flip": "auto", "channels": "rgb", "view": "fit",
                "brightness": "0", "bp": "0", "mp": "0.5", "wp": "1", "north": "0"}
OPT_CHOICES = {
    "bayer": ["auto", "none", *BAYER_PATTERNS],
    "stretch": ["unlinked", "linked", "manual", "linear", "none"],
    "flip": ["auto", "none", "v", "h", "vh"],
    "channels": ["rgb", "bgr", "r", "g", "b", "lum"],
    "view": ["fit", "one"],
    "north": ["0", "1"],          # viewer: rotate to north up / east left (after plate solving) – display only
}
RENDER_KEYS = ["bayer", "stretch", "flip", "channels", "bp", "mp", "wp"]   # keys that affect pixels


def _num(v, lo, hi, default, digits=4):
    try:
        x = float(v)
        if not np.isfinite(x):
            raise ValueError
    except (TypeError, ValueError):
        return default
    s = f"{max(lo, min(hi, x)):.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def clean_opts(d: dict | None) -> dict:
    d = d or {}
    o = dict(DEFAULT_OPTS)
    for k, choices in OPT_CHOICES.items():
        if d.get(k) in choices:
            o[k] = d[k]
    o["brightness"] = _num(d.get("brightness", 0), -100, 100, "0", 0)
    o["bp"] = _num(d.get("bp", 0), 0, 0.99, "0")
    o["wp"] = _num(d.get("wp", 1), 0.01, 1, "1")
    o["mp"] = _num(d.get("mp", 0.5), 0.0001, 0.9999, "0.5")
    if float(o["wp"]) <= float(o["bp"]):
        o["bp"], o["wp"] = "0", "1"
    if o["stretch"] != "manual":   # levels only matter in manual mode (keeps cache keys stable)
        o["bp"], o["mp"], o["wp"] = "0", "0.5", "1"
    return o


# ---------------- stretching ----------------
def mtf(m, x):
    """Midtones transfer function: mtf(m, m) = 0.5."""
    return ((m - 1) * x) / ((2 * m - 1) * x - m + 1e-12)


def stf_params(c):
    """Auto STF (PixInsight-like): returns (shadow clip, midtones balance) for normalized data."""
    med = float(np.median(c))
    mad = float(np.median(np.abs(c - med))) * 1.4826
    shadow = max(0.0, med - 2.8 * mad)
    m = (med - shadow) / (1 - shadow + 1e-12)
    target = 0.25
    mid = (m * (target - 1)) / ((2 * m - 1) * target - m) if 0 < m < 1 else 0.5
    return shadow, float(min(max(mid, 1e-4), 0.9999))


def normalize(a: np.ndarray):
    """Robust 0..1 normalization (same in render and histogram, so levels match)."""
    a = np.nan_to_num(a.astype(np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    sample = a[::4, ::4] if a.shape[0] > 400 else a
    lo, hi = np.percentile(sample, 0.01), np.percentile(sample, 99.995)
    return np.clip((a - lo) / (hi - lo + 1e-12), 0, 1)


def stretch(a: np.ndarray, opts: dict) -> np.ndarray:
    """a: HxW or HxWx3 float (linear) -> uint8."""
    mode = opts["stretch"]
    a = np.nan_to_num(a.astype(np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    if mode == "none":
        mx = float(a.max())
        mx = 1.0 if mx <= 1.0 else (255.0 if mx <= 255 else 65535.0)
        return (np.clip(a / mx, 0, 1) * 255).astype(np.uint8)
    a = normalize(a)
    if mode == "linear":
        return (a * 255).astype(np.uint8)
    chans = [a] if a.ndim == 2 else [a[..., i] for i in range(a.shape[2])]
    if mode == "manual":
        bp, mp, wp = float(opts["bp"]), float(opts["mp"]), float(opts["wp"])
        out = [np.clip(mtf(mp, np.clip((c - bp) / (wp - bp + 1e-12), 0, 1)), 0, 1) for c in chans]
    else:
        if mode == "linked" and a.ndim == 3:
            params = [stf_params(a[::4, ::4].mean(axis=-1))] * len(chans)
        else:   # unlinked: each channel own stats -> neutral background
            params = [stf_params(c[::2, ::2]) for c in chans]
        out = [np.clip(mtf(mid, np.clip((c - sh) / (1 - sh + 1e-12), 0, 1)), 0, 1)
               for c, (sh, mid) in zip(chans, params)]
    res = out[0] if len(out) == 1 else np.stack(out, axis=-1)
    return (res * 255).astype(np.uint8)


# ---------------- demosaic ----------------
def shift_pattern(pat: str, xoff: int, yoff: int) -> str:
    grid = [[pat[0], pat[1]], [pat[2], pat[3]]]
    return "".join(grid[(r + yoff) % 2][(c + xoff) % 2] for r in (0, 1) for c in (0, 1))


def debayer_superpixel(a: np.ndarray, pattern: str) -> np.ndarray:
    h, w = (a.shape[0] // 2) * 2, (a.shape[1] // 2) * 2
    a = a[:h, :w]
    cells = [a[0::2, 0::2], a[0::2, 1::2], a[1::2, 0::2], a[1::2, 1::2]]
    r = cells[pattern.index("R")]
    b = cells[pattern.index("B")]
    gi = [i for i, c in enumerate(pattern) if c == "G"]
    g = (cells[gi[0]] + cells[gi[1]]) / 2
    return np.stack([r, g, b], axis=-1)


def _conv3(a, k, out=None):
    """3×3 convolution with reflected border; reuses one temporary buffer (no per-tap allocations)."""
    p = np.pad(a, 1, mode="reflect")
    h, w = a.shape
    if out is None:
        out = np.zeros_like(a)
    else:
        out[...] = 0
    tmp = np.empty_like(a)
    for dy in range(3):
        for dx in range(3):
            if k[dy][dx]:
                np.multiply(p[dy:dy + h, dx:dx + w], np.float32(k[dy][dx]), out=tmp)
                out += tmp
    return out


def debayer_bilinear(a: np.ndarray, pattern: str) -> np.ndarray:
    """Full-resolution bilinear demosaic."""
    # memory-lean: no coordinate grids / masks; each channel plane only holds its own CFA sites,
    # and the result is written straight into one preallocated float32 (h, w, 3) array
    a = np.asarray(a, dtype=np.float32)
    h, w = a.shape
    out = np.empty((h, w, 3), np.float32)
    cells = [(0, 0), (0, 1), (1, 0), (1, 1)]           # positions of the 4 pattern letters in a 2×2 cell
    for ci, (col, k) in enumerate((("R", [[.25, .5, .25], [.5, 1, .5], [.25, .5, .25]]),
                                   ("G", [[0, .25, 0], [.25, 1, .25], [0, .25, 0]]),
                                   ("B", [[.25, .5, .25], [.5, 1, .5], [.25, .5, .25]]))):
        plane = np.zeros((h, w), np.float32)
        for i, c in enumerate(pattern):
            if c == col:
                y, x = cells[i]
                plane[y::2, x::2] = a[y::2, x::2]
        _conv3(plane, k, out=out[..., ci])
        del plane
    return out


# ---------------- loaders (linear data) ----------------
def fits_header(path: Path):
    from astropy.io import fits
    with fits.open(path, memmap=False, lazy_load_hdus=True) as hdul:
        for h in hdul:
            if h.header.get("NAXIS", 0) >= 2:
                return h.header.copy()
        return hdul[0].header.copy()


def fits_cfa_pattern(header, opts) -> str | None:
    pat = opts["bayer"]
    if pat == "auto":
        pat = str(header.get("BAYERPAT", "") or header.get("COLORTYP", "")).strip().upper()
        if pat in BAYER_PATTERNS:
            def off(k):
                try:
                    return int(float(header.get(k, 0) or 0))
                except (TypeError, ValueError):
                    return 0
            pat = shift_pattern(pat, off("XBAYROFF"), off("YBAYROFF"))
    return pat if pat in BAYER_PATTERNS else None


def load_fits_raw(path: Path):
    from astropy.io import fits
    with fits.open(path, memmap=False) as hdul:
        hdu = next((h for h in hdul if h.data is not None and h.data.ndim >= 2), None)
        if hdu is None:
            return None, hdul[0].header.copy()
        return planes_first(hdu.data.astype(np.float32)), hdu.header.copy()


def planes_first(d: np.ndarray) -> np.ndarray:
    """FITS cubes are (planes, h, w); drop extra leading axes of 4-D+ data (e.g. (1, 3, h, w))."""
    while d.ndim > 3:
        d = d[0]
    return d


def to_image_planes(d: np.ndarray) -> np.ndarray:
    """3-D FITS data -> (h, w) mono or (h, w, 3) colour. 1 or 2 planes -> first plane, ≥3 -> first three.
    Arrays already in (h, w, 3|4) order (non-FITS sources) are kept."""
    if d.ndim != 3:
        return d
    if d.shape[-1] in (3, 4) and d.shape[0] > 4:
        return d[..., :3]
    return np.moveaxis(d[:3], 0, -1) if d.shape[0] >= 3 else d[0]


def load_fits(path: Path, opts: dict, max_side: int | None):
    data, header = load_fits_raw(path)
    if data is None:
        return None, header
    if data.ndim == 3 and opts["bayer"] in BAYER_PATTERNS:
        data = data[0]          # manual pattern on color file -> treat first plane as CFA
    if data.ndim == 3:
        data = to_image_planes(data)
    elif data.ndim == 2:
        pat = fits_cfa_pattern(header, opts)
        if pat and min(data.shape) >= 2:          # a CFA needs at least one 2×2 cell
            data = debayer_bilinear(data, pat) if max_side is None else debayer_superpixel(data, pat)
    flip = opts["flip"]
    if flip == "auto":
        flip = "v" if str(header.get("ROWORDER", "")).strip().upper() == "BOTTOM-UP" else "none"
    if "v" in flip:
        data = data[::-1]
    if "h" in flip:
        data = data[:, ::-1]
    if max_side:
        step = max(1, max(data.shape[:2]) // (max_side * 2))
        data = data[::step, ::step]
    return data, header


def load_raw(path: Path, opts: dict, max_side: int | None):
    import rawpy
    with rawpy.imread(str(path)) as raw:
        if opts["stretch"] == "none":
            return raw.postprocess(half_size=bool(max_side), use_camera_wb=True).astype(np.float32), None
        rgb = raw.postprocess(half_size=bool(max_side), use_camera_wb=True, no_auto_bright=True,
                              output_bps=16, gamma=(1, 1))
    return rgb.astype(np.float32), None


def load_image(path: Path, opts: dict, max_side: int | None):
    im = ImageOps.exif_transpose(Image.open(path))
    if im.mode in ("P", "RGBA", "LA", "CMYK", "YCbCr"):     # CMYK JPEGs would otherwise get wrong colours
        im = im.convert("RGB")
    if max_side and max(im.size) > max_side * 2:
        im.thumbnail((max_side * 2, max_side * 2))
    return np.asarray(im, dtype=np.float32), None


def is_8bit_image(path: Path) -> bool:
    try:
        with Image.open(path) as im:
            return im.mode in ("RGB", "RGBA", "L", "P", "LA", "CMYK", "YCbCr")
    except Exception:
        return False


def load_linear(src: Path, opts: dict, max_side: int | None):
    ext = src.suffix.lower()
    if ext in FITS_EXT:
        return load_fits(src, opts, max_side)
    if ext in RAW_EXT:
        return load_raw(src, opts, max_side)
    if ext in IMAGE_EXT:
        return load_image(src, opts, max_side)
    return None, None


def render(src: Path, opts: dict | None = None, max_side: int | None = THUMB_SIDE):
    """Returns (PIL image or None, fits header or None)."""
    opts = clean_opts(opts)
    ext = src.suffix.lower()
    data, header = load_linear(src, opts, max_side)
    if data is None:
        return None, header
    o = dict(opts)
    # ordinary 8-bit pictures are already display-ready: auto stretch would ruin them
    if ext in IMAGE_EXT and o["stretch"] in ("unlinked", "linked") and is_8bit_image(src):
        o["stretch"] = "none"
    if ext in RAW_EXT and o["stretch"] == "none":
        im = Image.fromarray(np.clip(data, 0, 255).astype(np.uint8))
    else:
        im = Image.fromarray(stretch(data, o))
    im = apply_channels(im, opts["channels"])
    b = int(float(opts.get("brightness", "0")))
    if b:
        im = ImageEnhance.Brightness(im).enhance(1 + b / 100)
    if max_side:
        im.thumbnail((max_side, max_side))
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    return im, header


def histogram(src: Path, opts: dict, bins: int = 256) -> dict | None:
    """Histogram of normalized linear data + auto-STF levels (for the manual levels UI)."""
    opts = clean_opts(opts)
    data, _ = load_linear(src, opts, 1200)
    if data is None:
        return None
    a = normalize(data)
    if a.ndim == 3 and a.shape[2] >= 3:
        chans = {"r": a[..., 0], "g": a[..., 1], "b": a[..., 2]}
        lum = a.mean(axis=-1)
    else:
        chans = {}
        lum = a if a.ndim == 2 else a[..., 0]
    out = {"bins": bins, "lum": np.histogram(lum, bins=bins, range=(0, 1))[0].tolist()}
    for k, c in chans.items():
        out[k] = np.histogram(c, bins=bins, range=(0, 1))[0].tolist()
    sh, mid = stf_params(lum[::2, ::2])
    out["auto"] = {"bp": round(sh, 5), "mp": round(mid, 5), "wp": 1}
    return out


def apply_channels(im: Image.Image, mode: str) -> Image.Image:
    """Display-only channel mapping (never touches the original file)."""
    if mode == "rgb":
        return im
    if im.mode != "RGB":
        im = im.convert("RGB")
    r, g, b = im.split()
    if mode == "bgr":
        return Image.merge("RGB", (b, g, r))
    if mode == "lum":
        return im.convert("L")
    return {"r": r, "g": g, "b": b}[mode]


def fits_is_mono(path: Path) -> bool:
    try:
        return int(fits_header(path).get("NAXIS", 0)) == 2
    except Exception:
        return False


def make_preview(src: Path, dst: Path, opts: dict | None = None):
    """Returns (ok, header, error) — header only for FITS, error text when the file could not be read."""
    try:
        im, header = render(src, opts, THUMB_SIDE)
        if im is None:
            return False, header, None
        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst, "JPEG", quality=85)
        return True, header, None
    except Exception as e:
        print(f"preview failed for {src}: {e}")
        return False, None, str(e) or e.__class__.__name__


# ---------------- metadata ----------------
def header_text(header) -> str | None:
    return header.tostring(sep="\n").rstrip() if header is not None else None


def parse_date_obs(v) -> datetime | None:
    s = str(v or "").strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            pass
    return None


def header_meta(header) -> dict:
    """Extract common acquisition values from a FITS header."""
    if header is None:
        return {}

    def g(*keys):
        for k in keys:
            if k in header and str(header[k]).strip() != "":
                return header[k]
        return None
    exp = g("EXPTIME", "EXPOSURE")
    try:
        exp = float(exp) if exp is not None else None
    except (TypeError, ValueError):
        exp = None
    m = {"exposure_s": exp, "gain": g("GAIN", "ISOSPEED"), "temperature": g("CCD-TEMP", "SET-TEMP"),
         "filter": g("FILTER")}
    out = {k: (v if k == "exposure_s" or v is None else str(v).strip()) for k, v in m.items()}
    out["date_obs"] = parse_date_obs(g("DATE-OBS", "DATE-LOC"))
    return out


def exif_meta(src: Path) -> dict:
    """Exposure / ISO / date from EXIF (camera RAW, JPEG, TIFF)."""
    try:
        import exifread
        with open(src, "rb") as fh:
            tags = exifread.process_file(fh, details=False)
    except Exception:
        return {}
    out = {}
    t = tags.get("EXIF ExposureTime")
    if t:
        try:
            v = t.values[0]
            out["exposure_s"] = float(v.num) / float(v.den) if hasattr(v, "num") else float(v)
        except Exception:
            pass
    iso = tags.get("EXIF ISOSpeedRatings")
    if iso:
        out["gain"] = f"ISO {iso}"
    d = tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime")
    if d:
        try:
            out["date_obs"] = datetime.strptime(str(d), "%Y:%m:%d %H:%M:%S")
        except ValueError:
            pass
    return out


# ---------------- frame type detection ----------------
TYPE_WORDS = [
    ("darkflat", r"dark[\s_-]*flats?|flat[\s_-]*darks?"),
    ("bias", r"bias|offsets?|zero"),
    ("dark", r"darks?"),
    ("flat", r"flats?|flat[\s_-]*field"),
    ("light", r"lights?|object|science"),
    ("stack", r"stack(ed)?|final|result|integration|processed"),
]


def type_from_text(s: str) -> str | None:
    s = (s or "").lower()
    for t, rx in TYPE_WORDS:
        if re.search(rf"(^|[^a-z]){rx}([^a-z]|$)", s):
            return t
    return None


def detect_frame_type(path: Path, rel: str = "", header=None) -> str:
    """IMAGETYP from FITS header, then file/folder name keywords, then a sensible default."""
    ext = path.suffix.lower()
    if header is None and ext in FITS_EXT:
        try:
            header = fits_header(path)
        except Exception:
            header = None
    if header is not None:
        for k in ("IMAGETYP", "FRAME", "IMAGETYPE", "FRAMETYP"):
            t = type_from_text(str(header.get(k, "")))
            if t:
                return t
    t = type_from_text(path.stem)
    if t:
        return t
    parts = Path(rel).parts[:-1] if rel else ()
    for part in reversed(parts):   # closest folder first
        t = type_from_text(part)
        if t:
            return t
    return "stack" if ext in IMAGE_EXT else "light"


# ---------------- info for side panel ----------------
def file_info(src: Path, stored_header: str | None) -> dict:
    """Returns {'image': [(k,v)], 'header': [(key,value,comment)], 'exif': [(k,v)]}."""
    info = {"image": [], "header": [], "exif": []}
    ext = src.suffix.lower()
    try:
        if ext in FITS_EXT:
            h = fits_header(src)
            for card in h.cards:
                if card.keyword in ("", "END"):
                    continue
                info["header"].append((card.keyword, str(card.value), card.comment))
            dims = [h.get(f"NAXIS{i}") for i in range(1, (h.get("NAXIS") or 0) + 1)]
            info["image"].append(("Dimensions", " × ".join(str(d) for d in dims)))
            info["image"].append(("BITPIX", str(h.get("BITPIX"))))
        elif ext in IMAGE_EXT:
            with Image.open(src) as im:
                info["image"] += [("Dimensions", f"{im.width} × {im.height}"), ("Mode", im.mode),
                                  ("Format", im.format or "")]
        elif ext in RAW_EXT:
            import rawpy
            with rawpy.imread(str(src)) as raw:
                s = raw.sizes
                info["image"] += [("Dimensions", f"{s.width} × {s.height}"),
                                  ("Raw dimensions", f"{s.raw_width} × {s.raw_height}"),
                                  ("Bayer", "".join(chr(raw.color_desc[i]) for i in raw.raw_pattern.flatten())
                                   if raw.raw_pattern is not None else "")]
    except Exception as e:
        info["image"].append(("Error", str(e)))
    if not info["header"] and stored_header:
        for line in stored_header.splitlines():
            k, _, rest = line.partition("=")
            v, _, c = rest.partition("/")
            info["header"].append((k.strip(), v.strip().strip("'").strip(), c.strip()))
    if ext in IMAGE_EXT | RAW_EXT:
        try:
            import exifread
            with open(src, "rb") as fh:
                tags = exifread.process_file(fh, details=False)
            for k, v in sorted(tags.items()):
                if "Thumbnail" in k or k.startswith("MakerNote") or len(str(v)) > 200:
                    continue
                info["exif"].append((k.split(" ", 1)[-1], str(v)))
        except Exception:
            pass
    return info
