"""Annotated JPEG: the rendered frame with objects in the field, RA/Dec grid and N/E arrows burnt in."""
import io
import math
from pathlib import Path

from PIL import ImageDraw, ImageFont

from .platesolve import annotations
from .preview import render

MAX_SIDE = 3000
OBJ = (255, 211, 77)
GRID = (120, 170, 255)
COMPASS = (126, 231, 135)


def to_display(u: float, v: float, orient: int, flip: str):
    """Fractions of the solved array -> fractions of the displayed image (same as the viewer overlay)."""
    if orient == 180:
        u, v = 1 - u, 1 - v
    elif orient == 90:
        u, v = 1 - v, u
    elif orient == 270:
        u, v = v, 1 - u
    if "h" in flip:
        u = 1 - u
    if "v" in flip:
        v = 1 - v
    return u, v


FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def north_up(ann: dict, orient: int, flip: str, W: int, H: int):
    """(mirror, angle): mirror horizontally first (if east is not left of north), then rotate clockwise by
    `angle` degrees (screen convention, like CSS rotate) so that north points up."""
    def px(u, v):
        u, v = to_display(u, v, orient, flip)
        return u * W, v * H
    c, n, e = (px(*ann["compass"][k]) for k in ("c", "n", "e"))
    nx, ny, ex, ey = n[0] - c[0], n[1] - c[1], e[0] - c[0], e[1] - c[1]
    mirror = (nx * ey - ny * ex) > 0             # normal sky: east 90° counter-clockwise from north
    if mirror:
        nx = -nx
    return mirror, -90 - math.degrees(math.atan2(ny, nx))


def _font(size: int):
    try:
        return ImageFont.truetype(FONT, size)
    except OSError:
        pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                       # very old Pillow without scalable default font
        return ImageFont.load_default()


def draw_annotations(im, ann: dict, orient: int, flip: str, grid: bool = True, xf=None, size=None):
    """xf: optional mapping of pixel coordinates of the un-rotated image (size) onto `im` (north-up export)."""
    SW, SH = size or im.size                      # size of the image the (u, v) fractions refer to
    d = ImageDraw.Draw(im)
    lw = max(1, round(max(im.size) / 900))
    fs = max(12, round(max(im.size) / 80))
    font, small = _font(fs), _font(max(10, round(fs * 0.8)))
    xf = xf or (lambda x, y: (x, y))
    P = lambda u, v: xf(*(a * b for a, b in zip(to_display(u, v, orient, flip), (SW, SH))))
    W, H = im.size
    if grid:
        for g in ann["grid"]:
            pts = [P(u, v) for u, v in g["pts"]]
            d.line(pts, fill=GRID, width=lw)
            mx, my = pts[len(pts) // 2]
            d.text((mx + 4, my - fs), g["label"], fill=GRID, font=small, stroke_width=2, stroke_fill=(0, 0, 0))
    for o in ann["objects"]:
        cx, cy = P(o["u"], o["v"])
        rx = ry = fs * 0.6
        ang = 0.0
        if o.get("axis"):
            ax, ay = P(*o["axis"])
            rx = max(math.hypot(ax - cx, ay - cy), 6)
            ry = max(rx * ((o.get("min") or o["maj"]) / o["maj"]), 4)
            ang = math.atan2(ay - cy, ax - cx)
        ca, sa = math.cos(ang), math.sin(ang)
        poly = [(cx + rx * math.cos(t) * ca - ry * math.sin(t) * sa, cy + rx * math.cos(t) * sa + ry * math.sin(t) * ca)
                for t in (2 * math.pi * i / 72 for i in range(73))]
        d.line(poly, fill=OBJ, width=lw + 1)
        label = " / ".join(x for x in (o.get("messier"), o["name"]) if x) + (f" – {o['common']}" if o.get("common") else "")
        r = max(rx, ry) * 0.75
        tw = d.textlength(label, font=font)
        tx = cx + r + 4 if cx + r + 4 + tw <= W - 4 else max(4, cx - r - 4 - tw)   # keep the label inside
        ty = min(max(cy - r - fs, 4), H - fs - 4)
        d.text((tx, ty), label, fill=OBJ, font=font, stroke_width=3, stroke_fill=(0, 0, 0))
    # compass (top-left corner)
    c = ann["compass"]
    ox, oy = P(*c["c"])
    base = (fs * 4.5, fs * 4.5)
    for k in ("n", "e"):
        x, y = P(*c[k])
        dx, dy = x - ox, y - oy
        L = math.hypot(dx, dy) or 1
        end = (base[0] + dx / L * fs * 2.2, base[1] + dy / L * fs * 2.2)
        d.line([base, end], fill=COMPASS, width=lw + 2)
        d.text((base[0] + dx / L * fs * 2.9 - fs / 3, base[1] + dy / L * fs * 2.9 - fs / 2), k.upper(),
               fill=COMPASS, font=font, stroke_width=3, stroke_fill=(0, 0, 0))
    return im


def rotate_north_up(im, ann: dict, orient: int, flip: str):
    """Image turned north up / east left + the matching point transform (for drawing the overlay)."""
    from PIL import ImageOps
    W, H = im.size
    mirror, ang = north_up(ann, orient, flip, W, H)
    if mirror:
        im = ImageOps.mirror(im)
    out = im.rotate(-ang, expand=True, resample=3, fillcolor=(0, 0, 0))    # PIL: counter-clockwise
    W2, H2 = out.size
    a = math.radians(ang)
    ca, sa = math.cos(a), math.sin(a)

    def xf(x, y):
        if mirror:
            x = W - x
        dx, dy = x - W / 2, y - H / 2
        return W2 / 2 + dx * ca - dy * sa, H2 / 2 + dx * sa + dy * ca
    return out, xf


def annotated_jpeg(src: Path, opts: dict, res: dict, flip: str, grid: bool = True, north: bool = False) -> bytes | None:
    im, _ = render(src, {**opts, "flip": flip}, max_side=None)
    if im is None:
        return None
    if max(im.size) > MAX_SIDE:
        im.thumbnail((MAX_SIDE, MAX_SIDE))
    im = im.convert("RGB")
    ann, orient = annotations(res), res.get("orient", 0)
    if north:
        size = im.size
        im, xf = rotate_north_up(im, ann, orient, flip)
        draw_annotations(im, ann, orient, flip, grid, xf=xf, size=size)
    else:
        draw_annotations(im, ann, orient, flip, grid)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()
