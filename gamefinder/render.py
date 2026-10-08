"""Image cards for the bot: every recommendation and game breakdown goes out as a photo.

    await cover(http, appid)              -> Steam art (bytes) cached on disk, or None
    pick_card(view, cover)                -> JPEG 1280x720, one game of a selection
    game_card(view, cover)                -> JPEG 1280 wide, 1100-2400 high (as its text needs)
    selection_banner(title, sub, covers)  -> JPEG 1280x480, the header of a selection

SILVERHAND look in the Game Finder amber: near-black, amber neon (#ff9f1c), a little
hologram cyan, signal tape, a dot grid and scanlines; palette and the tape come from
tools/make_intro.py. Fonts are bundled in assets/fonts (Sofia Sans Extra Condensed for
display, IBM Plex Mono for captions, both SIL OFL), so the server needs nothing installed.

Sizes are for phones: Telegram shows a photo about 360-400 px wide, so at 1280 px titles
are 80+ px and the smallest labels 26 px. Rendering stays light for a 300 MB VPS: no
supersampled full canvases, glows are blurred at a quarter size on small patches, fonts
and static layers are cached. A pick card takes about 50 ms, a long game card about 110 ms.

Nothing important is cut: the game card grows (GAME_MIN_H..GAME_MAX_H) to hold its state
line, summary and every praise and complaint in full; the pick card picks a smaller chip font
before it cuts. LAST_CLIPPED lists the fields the last card still had to cut (tests use it).
Sofia Sans has no ₽ or ₸: text() and the width helpers draw such characters with IBM Plex Mono.
"""

from __future__ import annotations

import io
import logging
import math
import os
import re
import time
import zlib
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "assets" / "fonts"
# The RU file is the google/fonts Sofia Sans Extra Condensed with its cmap pointed at the
# font's own .loclRUS glyphs: the default lowercase is Bulgarian (д like g, т like m), and
# Pillow without libraqm cannot apply the locl feature. Made with fontTools:
#   m = GSUB lookup of feature locl/cyrl/RUS; for each unicode cmap: cmap[cp] = m.get(g, g)
DISPLAY_FILE = FONT_DIR / "SofiaSansExtraCondensedRU[wght].ttf"
DISPLAY_ORIGINAL = FONT_DIR / "SofiaSansExtraCondensed[wght].ttf"
MONO_FILES = {400: "IBMPlexMono-Regular.ttf", 500: "IBMPlexMono-Medium.ttf",
              600: "IBMPlexMono-SemiBold.ttf", 700: "IBMPlexMono-Bold.ttf"}
FALLBACK_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "C:/Windows/Fonts/arialbd.ttf"]

# Palette of tools/make_intro.py: amber neon; hologram cyan only for tiny accents.
BG = (10, 7, 4)
ACC = (255, 159, 28)
HI = (255, 228, 176)
DEEP = (176, 78, 0)
EX = (54, 24, 2)
INK = (246, 240, 232)
TAPE_K = (11, 8, 5)
CYAN = (59, 228, 255)
DIM = (150, 138, 124)       # secondary text
FAINT = (92, 80, 66)        # tertiary text, unlit words
UNLIT = (122, 110, 96)      # the end of a scale the value does not lean to
OFF = (46, 32, 18)          # unlit segments
CHROME = (176, 178, 186)    # silver for neutral frames
PANEL = (16, 12, 8)
# Freshness of reviews, muted so the amber stays the loudest colour.
GOOD = (118, 192, 124)
BAD = (214, 98, 74)

STEAM_ART = [
    "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{}/capsule_616x353.jpg",
    "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{}/header.jpg",
    "https://cdn.akamai.steamstatic.com/steam/apps/{}/header.jpg",
]
MISS_TTL = 6 * 3600          # a game without art is asked again after six hours
JPEG = dict(format="JPEG", quality=88, subsampling=0)


# ---------------------------------------------------------------- cover art

async def cover(http, appid: int, cache_dir="data/covers") -> bytes | None:
    """Steam art for the game, cached on disk as <appid>.jpg; None when there is none.

    http is gamefinder.http.Http (its session() is used), an aiohttp.ClientSession or None
    (then a short-lived session is made). Never raises."""
    try:
        appid = int(appid)
        folder = Path(cache_dir)
        path = folder / f"{appid}.jpg"
        miss = folder / f"{appid}.miss"
        if path.is_file() and path.stat().st_size > 0:
            try:
                os.utime(path)          # used: the daily cleanup (maintenance.py) keeps recent covers
            except OSError:
                pass
            return path.read_bytes()
        if miss.is_file() and time.time() - miss.stat().st_mtime < MISS_TTL:
            return None
        data = await _download(http, appid)
        folder.mkdir(parents=True, exist_ok=True)
        if data is None:
            miss.touch()
            return None
        tmp = folder / f"{appid}.{os.getpid()}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, path)
        miss.unlink(missing_ok=True)
        return data
    except Exception as e:  # noqa: BLE001 - art is decoration, never a reason to fail
        log.warning("cover %s: %s", appid, e)
        return None


async def _download(http, appid: int) -> bytes | None:
    import aiohttp

    own = None
    if http is None:
        session = own = aiohttp.ClientSession()
    elif hasattr(http, "session") and callable(http.session):
        session = await http.session()
    else:
        session = http
    try:
        for url in STEAM_ART:
            try:
                async with session.get(url.format(appid), timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        continue
                    data = await r.read()
            except Exception as e:  # noqa: BLE001
                log.debug("cover %s %s: %s", appid, url, e)
                continue
            if 1000 < len(data) < 3_000_000 and _is_image(data):
                return data
        return None
    finally:
        if own is not None:
            await own.close()


def _is_image(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.width >= 100 and im.height >= 50
    except Exception:  # noqa: BLE001
        return False


def _open_cover(data: bytes | None) -> Image.Image | None:
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.draft("RGB", (640, 360))
        return im.convert("RGB")
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- fonts and text

@lru_cache(maxsize=48)
def sofia(size: int, weight: int = 800) -> ImageFont.FreeTypeFont:
    """Sofia Sans Extra Condensed at a weight 100-1000 (it is a variable font)."""
    size = max(8, int(size))
    try:
        path = DISPLAY_FILE if DISPLAY_FILE.exists() else DISPLAY_ORIGINAL
        f = ImageFont.truetype(str(path), size)
        try:
            f.set_variation_by_axes([weight])
        except Exception:  # noqa: BLE001 - FreeType without variations: default weight
            pass
        return f
    except OSError:
        return _fallback(size)


@lru_cache(maxsize=24)
def mono(size: int, weight: int = 600) -> ImageFont.FreeTypeFont:
    size = max(8, int(size))
    try:
        return ImageFont.truetype(str(FONT_DIR / MONO_FILES.get(weight, MONO_FILES[600])), size)
    except OSError:
        return _fallback(size)


def _fallback(size):
    for p in FALLBACK_FONTS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


_GLYPHS: dict[tuple[str, str], bool] = {}


def _has_glyph(font, ch) -> bool:
    """Whether the font file draws ch (Sofia Sans has no ₽ or ₸): its mask differs from
    the one of a surely missing character. Cached per file and character."""
    if ord(ch) < 0x2000 or ch.isspace():
        return True
    key = (getattr(font, "path", ""), ch)
    ok = _GLYPHS.get(key)
    if ok is None:
        try:
            m, n = font.getmask(ch), font.getmask("\U0010fffd")
            ok = not (m.size == n.size and bytes(m) == bytes(n))
        except Exception:  # noqa: BLE001
            ok = True
        _GLYPHS[key] = ok
    return ok


def _runs(font, s):
    """s split into runs of (text, font): characters the font lacks go to IBM Plex Mono."""
    if not s or max(s) < "\u2000" or all(_has_glyph(font, ch) for ch in s):
        return [(s, font)]
    fb = mono(max(8, round(font.size * .8)), 600)
    out: list[tuple[str, ImageFont.FreeTypeFont]] = []
    for ch in s:
        f = font if _has_glyph(font, ch) else fb
        if out and out[-1][1] is f:
            out[-1] = (out[-1][0] + ch, f)
        else:
            out.append((ch, f))
    return out


def glen(font, s) -> float:
    """font.getlength with the fallback for missing glyphs."""
    runs = _runs(font, s)
    if len(runs) == 1:
        return font.getlength(s)
    return sum(f.getlength(t) for t, f in runs)


def tlen(font, text, tracking=0.0) -> float:
    return glen(font, text) + tracking * max(0, len(text) - 1)


def text(d, xy, s, font, fill, tracking=0.0, anchor="ls"):
    """Text with letter spacing (Pillow has none). Returns the x after the text."""
    x, y = xy
    runs = _runs(font, s)
    if not tracking and len(runs) == 1:
        d.text((x, y), s, font=font, fill=fill, anchor=anchor)
        return x + font.getlength(s)
    if anchor[0] == "r":
        x -= tlen(font, s, tracking)
    elif anchor[0] == "m":
        x -= tlen(font, s, tracking) / 2
    a = "l" + anchor[1]
    for t, f in runs:
        if not tracking:
            d.text((x, y), t, font=f, fill=fill, anchor=a)
            x += f.getlength(t)
            continue
        for ch in t:
            d.text((x, y), ch, font=f, fill=fill, anchor=a)
            x += f.getlength(ch) + tracking
    return x - tracking


# Debug hook: the text fields the last pick_card / game_card had to cut ("state", "summary",
# "praise", ...). Empty when everything was drawn in full. Tests read it.
LAST_CLIPPED: list[str] = []


def _note(field: str) -> None:
    if field not in LAST_CLIPPED:
        LAST_CLIPPED.append(field)


def _clip(field, s, font, width, tracking=0.0) -> str:
    """clip() that records field in LAST_CLIPPED when it had to cut."""
    out = clip(s, font, width, tracking)
    if out != s:
        _note(field)
    return out


def clip(s, font, width, tracking=0.0) -> str:
    """s cut with an ellipsis to fit width."""
    if tlen(font, s, tracking) <= width:
        return s
    lo, hi = 0, len(s)          # binary search for the longest prefix that fits
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if tlen(font, s[:mid].rstrip(" ,.;:—-") + "…", tracking) <= width:
            lo = mid
        else:
            hi = mid - 1
    s = s[:lo].rstrip(" ,.;:—-")
    return s + "…" if s else ""


def wrap(s, font, width) -> list[str]:
    """Greedy word wrap; a word longer than the line is broken with a hyphen.

    A line's width is estimated as the sum of its words (getlength is slow, and measuring
    the whole line for every word made long texts quadratic); near the edge the line is
    measured, and every finished line is checked once (the exact way when one is over).
    A one- or two-letter word ('с', 'в', 'и') goes to the next line with the word after it."""
    words = _glue(s.split())
    lines = _wrap(words, font, width, max(12.0, font.size * .5))
    if any(" " in ln and glen(font, ln) > width for ln in lines):
        lines = _wrap(words, font, width, None)
    return lines


def _glue(words):
    out, pend = [], ""
    for w in words:
        t = f"{pend} {w}" if pend else w
        if len(w) <= 2 and w.isalpha():
            pend = t
            continue
        out.append(t)
        pend = ""
    if pend:
        out.append(pend)
    return out


def _wrap(words, font, width, margin):
    lines, cur, cur_w = [], "", 0.0
    space = glen(font, " ")
    for w in words:
        ww = glen(font, w)
        if not cur and ww <= width:
            cur, cur_w = w, ww
            continue
        if cur:
            est = cur_w + space + ww
            if margin is not None and est <= width - margin:
                ok = True
            elif margin is not None and est > width + margin:
                ok = False
            else:
                ok = glen(font, f"{cur} {w}") <= width
            if ok:
                cur, cur_w = f"{cur} {w}", est
                continue
            lines.append(cur)
        while glen(font, w) > width and len(w) > 2:
            # The longest prefix that fits with its hyphen: binary search, not one letter at a time.
            lo, hi = 1, len(w) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if glen(font, w[:mid] + "-") <= width:
                    lo = mid
                else:
                    hi = mid - 1
            lines.append(w[:lo] + "-")
            w = w[lo:]
        cur, cur_w = w, glen(font, w)
    if cur:
        lines.append(cur)
    return lines


def fit(s, make_font, width, max_lines, sizes) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """The largest size from sizes at which s wraps into max_lines; else the smallest,
    with the last line clipped."""
    split = _split_at_separator(s) if max_lines >= 2 else None
    for size in sizes:
        f = make_font(size)
        lines = wrap(s, f, width)
        if len(lines) > 1 and split and all(glen(f, p) <= width for p in split):
            return f, split
        if len(lines) <= max_lines and not any(ln.endswith("-") for ln in lines[:-1]):
            return f, _balance(s, f, width, lines)
    f = make_font(sizes[-1])
    lines = wrap(s, f, width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = clip(lines[-1] + " " + "…" * 8, f, width).rstrip("…").rstrip() + "…"
    return f, lines


def _split_at_separator(s):
    """'Disco Elysium - The Final Cut' -> ['Disco Elysium', 'The Final Cut'];
    'Mor: Utopia' -> ['Mor:', 'Utopia']. None without a separator."""
    m = re.search(r"\s+[-–—]\s+|:\s+", s)
    if not m:
        return None
    head, tail = s[:m.start()].strip(), s[m.end():].strip()
    if not head or not tail:
        return None
    return [head + (":" if m.group().startswith(":") else ""), tail]


def _balance(s, f, width, lines):
    """Two lines of a title split evenly instead of a full first line and a stub;
    a line never starts with a dash."""
    words = s.split()
    if len(lines) != 2 or len(words) < 2:
        return lines

    def bad(i):
        return words[i] in ("-", "–", "—")
    cuts = [i for i in range(1, len(words)) if not bad(i)] or list(range(1, len(words)))
    best = min(cuts, key=lambda i: max(glen(f, " ".join(words[:i])), glen(f, " ".join(words[i:]))))
    a, b = " ".join(words[:best]), " ".join(words[best:])
    return [a, b] if max(glen(f, a), glen(f, b)) <= width else lines


# ---------------------------------------------------------------- drawing helpers

def mix(a, b, k):
    """k of color a over color b."""
    return tuple(round(x * k + y * (1 - k)) for x, y in zip(a, b))


@lru_cache(maxsize=32)
def _gradient_column(h, stops):
    col = Image.new("RGB", (1, h))
    px = col.load()
    for y in range(h):
        f = y / max(1, h - 1)
        for (f0, c0), (f1, c1) in zip(stops, stops[1:]):
            if f0 <= f <= f1:
                px[0, y] = mix(c1, c0, (f - f0) / max(1e-6, f1 - f0))
                break
    return col


def vgradient(w, h, stops):
    """Vertical gradient through ((fraction, color), ...)."""
    return _gradient_column(h, tuple(stops)).resize((w, h))


def glow(img, mask, xy, color=ACC, radius=18, strength=1.0):
    """Soft neon light of mask (L, placed at xy) on img; blurred at a quarter size."""
    pad = int(radius * 2)
    w, h = mask.width + 2 * pad, mask.height + 2 * pad
    s = 4
    small = Image.new("L", (max(1, w // s), max(1, h // s)), 0)
    small.paste(mask.resize((max(1, mask.width // s), max(1, mask.height // s)), Image.BILINEAR),
                (pad // s, pad // s))
    g = small.filter(ImageFilter.GaussianBlur(radius / s)).resize((w, h), Image.BILINEAR)
    if strength != 1.0:
        g = g.point(lambda v: min(255, int(v * strength)))
    x, y = int(xy[0]) - pad, int(xy[1]) - pad
    img.paste(color, (x, y, x + w, y + h), g)


def rect_mask(w, h, width=0):
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rectangle((0, 0, w - 1, h - 1), fill=None if width else 255,
                                outline=255 if width else None, width=width or 1)
    return m


def aa_layer(w, h, scale=3):
    """A transparent RGBA patch drawn at scale; finish with aa_done()."""
    im = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    return im, ImageDraw.Draw(im)


def aa_done(img, patch, xy, size):
    small = patch.resize(size, Image.LANCZOS)
    img.paste(small, (int(xy[0]), int(xy[1])), small)
    return small


def tape(w, band, phase=0.12):
    """Signal tape (from tools/make_intro.py)."""
    img = Image.new("RGB", (w, band), TAPE_K)
    d = ImageDraw.Draw(img)
    p = band * 2
    off = phase * p
    for x0 in range(-3 * p, w + 3 * p, p):
        x = x0 + off
        d.polygon([(x, 0), (x + p / 2, 0), (x + p / 2 - band, band), (x - band, band)], fill=ACC)
    return img


def grad_text(img, xy, s, font, stops=((0, HI), (.5, ACC), (1, DEEP)), depth=0,
              glow_strength=0.0, lines_tex=True, tracking=0.0):
    """Title text in the brand style: amber gradient with a line texture, a dark extrusion
    below and a glow. xy is the left end of the baseline. Returns the right x."""
    asc = -font.getbbox("ЁЙH", anchor="ls")[1]
    desc = font.getbbox("gyр", anchor="ls")[3]
    w = int(tlen(font, s, tracking)) + 8
    h = asc + desc + 4
    mask = Image.new("L", (w, h), 0)
    text(ImageDraw.Draw(mask), (2, asc + 2), s, font, 255, tracking)
    ox, oy = int(xy[0]) - 2, int(xy[1]) - asc - 2
    if glow_strength:
        glow(img, mask, (ox, oy), ACC, radius=max(8, font.size // 5), strength=glow_strength)
    for k in range(1, depth + 1):
        img.paste(EX if k < depth else (0, 0, 0), (ox, oy + k, ox + w, oy + k + h), mask)
    cap = -font.getbbox("H", anchor="ls")[1]
    top = asc + 2 - cap
    fill = Image.new("RGB", (w, h), stops[-1][1])
    fill.paste(vgradient(w, cap + 2, stops), (0, top))
    fill.paste(stops[0][1], (0, 0, w, top))
    if lines_tex:
        fd = ImageDraw.Draw(fill)
        step = max(3, font.size // 22)
        dark = mix((0, 0, 0), stops[1][1], .22)
        for y in range(top + step, h, step):
            fd.line((0, y, w, y), fill=dark)
    img.paste(fill, (ox, oy, ox + w, oy + h), mask)
    return xy[0] + tlen(font, s, tracking)


def brackets(d, x0, y0, x1, y1, length=26, width=3, color=HI, gap=8):
    """HUD corner brackets just outside the box."""
    x0, y0, x1, y1 = x0 - gap, y0 - gap, x1 + gap, y1 + gap
    for cx, cy, sx, sy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        d.line((cx, cy, cx + sx * length, cy), fill=color, width=width)
        d.line((cx, cy, cx, cy + sy * length), fill=color, width=width)


def chip(d, x, y, s, font, fg=INK, border=None, fill=None, pad=14, h=44, tracking=0.0):
    """A tag: text in a box with a cut corner. y is the top; returns the right x."""
    w = int(tlen(font, s, tracking)) + 2 * pad
    c = 10
    poly = [(x, y), (x + w, y), (x + w, y + h - c), (x + w - c, y + h), (x, y + h)]
    if fill:
        d.polygon(poly, fill=fill)
    if border:
        d.line(poly + [poly[0]], fill=border, width=2)
    text(d, (x + pad, y + h / 2), s, font, fg, tracking, anchor="lm")
    return x + w


def chip_width(s, font, pad=14, tracking=0.0):
    return int(tlen(font, s, tracking)) + 2 * pad


# ---------------------------------------------------------------- backdrop

_SHADE: dict[tuple[int, int], Image.Image] = {}
SHADE_KEEP = 6          # game cards have a height of their own: keep only the latest masks


def _shade(w, h):
    """Vignette plus scanlines as one dark mask, made once per size (a few sizes kept)."""
    m = _SHADE.pop((w, h), None)
    if m is not None:
        _SHADE[(w, h)] = m          # most recent last
    else:
        while len(_SHADE) >= SHADE_KEEP:
            _SHADE.pop(next(iter(_SHADE)))
        sw, sh = w // 16, h // 16
        v = Image.new("L", (sw, sh), 255)
        ImageDraw.Draw(v).ellipse((-sw * .18, -sh * .25, sw * 1.18, sh * 1.25), fill=0)
        v = v.filter(ImageFilter.GaussianBlur(sw * .09)).resize((w, h), Image.BILINEAR)
        v = v.point(lambda x: int(x * .78))
        scan = Image.new("L", (w, h), 0)
        sd = ImageDraw.Draw(scan)
        for y in range(0, h, 3):
            sd.line((0, y, w, y), fill=30)
        from PIL import ImageChops
        m = _SHADE[(w, h)] = ImageChops.lighter(v, scan)
    return m


def backdrop(w, h, cov, seed="", glow_at=(.25, .3), dim=.74, ids_at=(), fade=None, ref_h=None):
    """Blurred and darkened cover (or plain near-black), an amber glow, a dot grid, faint
    review ids at ids_at, vignette and scanlines. fade, ((fraction, 0..255), ...) down the
    height, then covers the picture with the background colour by that much.

    ref_h: the height the cover picture and the glow are laid out on (glow_at is a fraction
    of it), so a taller canvas keeps them as they are at the top; the picture's last row is
    stretched below. Default: h."""
    rh = int(ref_h or h)
    if cov is not None:
        sw, sh, shr = max(4, w // 24), max(4, h // 24), max(4, rh // 24)
        small = ImageOps.fit(cov, (sw, shr), Image.BILINEAR)
        gray = ImageOps.grayscale(small).convert("RGB")
        small = Image.blend(small, gray, .35)
        if shr != sh:
            ext = Image.new("RGB", (sw, sh), BG)
            ext.paste(small.crop((0, 0, sw, min(sh, shr))), (0, 0))
            if sh > shr:
                ext.paste(small.crop((0, shr - 1, sw, shr)).resize((sw, sh - shr)), (0, shr))
            small = ext
        small = small.filter(ImageFilter.GaussianBlur(1.0))
        img = small.resize((w, h), Image.BICUBIC)
        img = Image.blend(img, Image.new("RGB", (w, h), BG), dim)
    else:
        img = Image.new("RGB", (w, h), BG)
    gw, gh = w // 8, h // 8
    rgh = rh / 8
    g = Image.new("L", (gw, gh), 0)
    cx, cy = glow_at[0] * gw, glow_at[1] * rgh
    ImageDraw.Draw(g).ellipse((cx - gw * .42, cy - rgh * .6, cx + gw * .42, cy + rgh * .6), fill=70)
    g = g.filter(ImageFilter.GaussianBlur(gw * .1)).resize((w, h), Image.BILINEAR)
    img.paste(ACC, (0, 0, w, h), g)
    if fade:
        col = _gradient_column(h, tuple((f, (v, v, v)) for f, v in fade)).convert("L")
        img.paste(BG, (0, 0, w, h), col.resize((w, h), Image.NEAREST))
    d = ImageDraw.Draw(img)
    step = 32
    dot = mix(ACC, BG, .16)
    for x in range(step, w, step):
        for y in range(step, h, step):
            d.rectangle((x, y, x + 1, y + 1), fill=dot)
    # faint review ids, only where the caller knows nothing sits on top
    rnd = zlib.crc32(seed.encode()) or 7
    f = mono(26, 500)
    faint = mix(ACC, BG, .06)
    for x, y in ids_at:
        rnd = (rnd * 1103515245 + 12345) & 0x7FFFFFFF
        d.text((x, y), f"review#{rnd % 9000000 + 1000000} +{71 + rnd % 28}%", font=f, fill=faint)
    img.paste((0, 0, 0), (0, 0, w, h), _shade(w, h))
    return img


def tapes(img, band=12, bottom=True):
    t = tape(img.width, band)
    img.paste(t, (0, 0))
    if bottom:
        img.paste(t, (0, img.height - band))


# ---------------------------------------------------------------- cover frame and placeholder

def initials(name: str) -> str:
    words = [w for w in re.split(r"[\s:\-–—_.,!?()\[\]]+", name or "") if w and w[0].isalnum()]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def placeholder(w, h, name="") -> Image.Image:
    """Art for a game without a cover: a perspective grid, amber glow, initials."""
    img = Image.new("RGB", (w, h), (14, 10, 6))
    gw, gh = max(2, w // 8), max(2, h // 8)
    g = Image.new("L", (gw, gh), 0)
    ImageDraw.Draw(g).ellipse((gw * .2, gh * .15, gw * .8, gh * 1.05), fill=120)
    g = g.filter(ImageFilter.GaussianBlur(gw * .12)).resize((w, h), Image.BILINEAR)
    img.paste(ACC, (0, 0, w, h), g)
    d = ImageDraw.Draw(img)
    hor = int(h * .62)
    line = mix(ACC, BG, .30)
    # floor: lines running to the vanishing point and rows getting denser to the horizon
    for i in range(-12, 13):
        d.line((w / 2 + i * w * .02, hor, w / 2 + i * w * .16, h), fill=line, width=1)
    yy, k = h, 0
    while yy > hor + 2:
        d.line((0, yy, w, yy), fill=line, width=1)
        k += 1
        yy = hor + (h - hor) * (0.72 ** k)
    d.line((0, hor, w, hor), fill=mix(ACC, BG, .7), width=2)
    for x in range(0, w, 24):
        for y in range(12, hor - 6, 24):
            d.point((x, y), fill=mix(ACC, BG, .22))
    ini = initials(name)
    f = sofia(int(h * .5), 900)
    tw = f.getlength(ini)
    grad_text(img, ((w - tw) / 2, hor - h * .08), ini, f, depth=max(2, h // 60), glow_strength=1.2)
    d = ImageDraw.Draw(img)
    cap = mono(max(12, h // 16), 600)
    text(d, (w / 2, h - h * .07), "NO SIGNAL", cap, mix(ACC, BG, .8), tracking=max(2, h // 70), anchor="ms")
    return img


def framed_cover(img, cov, box, name="", glow_strength=.95):
    """Sharp cover in box with an amber neon frame, outer glow and corner brackets."""
    x, y, w, h = box
    art = ImageOps.fit(cov, (w, h), Image.LANCZOS) if cov is not None else placeholder(w, h, name)
    glow(img, rect_mask(w, h), (x, y), ACC, radius=26, strength=glow_strength)
    d = ImageDraw.Draw(img)
    d.rectangle((x - 1, y - 1, x + w, y + h), fill=(0, 0, 0))
    img.paste(art, (x, y))
    # a thin darkening at the bottom so overlays and the frame read on bright art
    sh = vgradient(1, 64, ((0, (0, 0, 0)), (1, (255, 255, 255)))).convert("L").resize((w, 64))
    img.paste((0, 0, 0), (x, y + h - 64, x + w, y + h), sh.point(lambda v: v * 0.45))
    d.rectangle((x - 2, y - 2, x + w + 1, y + h + 1), outline=ACC, width=3)
    d.line((x + 1, y + 1, x + w - 2, y + 1), fill=HI, width=1)
    brackets(d, x, y, x + w, y + h, length=28, width=3, color=HI, gap=10)


# ---------------------------------------------------------------- widgets

def ring(img, cx, cy, r, value, big, label=None, thick=None, segs=40):
    """Segmented match meter: value 0..1 lit clockwise from the top, big text inside."""
    value = max(0.0, min(1.0, value))
    thick = thick or max(8, r // 6)
    S = 3
    size = 2 * r + 8
    patch, d = aa_layer(size, size, S)
    c = size * S / 2
    rr = (r - thick / 2) * S
    lit = round(value * segs)
    step = 360 / segs
    for i in range(segs):
        a0 = -90 + i * step + step * .14
        a1 = -90 + (i + 1) * step - step * .14
        if i < lit:
            k = i / max(1, segs - 1)
            col = mix(HI, ACC, .55) if i == lit - 1 else mix(ACC, DEEP, .55 + .45 * k)
        else:
            col = OFF
        d.arc((c - rr - thick * S / 2, c - rr - thick * S / 2, c + rr + thick * S / 2, c + rr + thick * S / 2),
              a0, a1, fill=col + (255,), width=thick * S)
    ro = (r + 4) * S
    d.ellipse((c - ro, c - ro, c + ro, c + ro), outline=mix(ACC, BG, .35) + (255,), width=S)
    ri = (r - thick - 6) * S
    d.ellipse((c - ri, c - ri, c + ri, c + ri), outline=mix(ACC, BG, .22) + (255,), width=S)
    x0, y0 = cx - size / 2, cy - size / 2
    # glow from the lit part only
    if lit:
        gm = Image.new("L", (size * S, size * S), 0)
        a_end = -90 + lit * step
        ImageDraw.Draw(gm).arc((c - rr - thick * S / 2, c - rr - thick * S / 2,
                                c + rr + thick * S / 2, c + rr + thick * S / 2),
                               -90, a_end, fill=255, width=thick * S)
        glow(img, gm.resize((size, size), Image.BILINEAR), (x0, y0), ACC, radius=max(10, r // 6), strength=1.1)
    aa_done(img, patch, (x0, y0), (size, size))
    d = ImageDraw.Draw(img)
    num, unit = (big[:-1], "%") if big.endswith("%") else (big, "")
    fn = sofia(int(r * .92), 900)
    fu = sofia(int(r * .42), 800)
    w = fn.getlength(num) + (fu.getlength(unit) + 3 if unit else 0)
    base = cy + fn.size * .33 - (r * .12 if label else 0)
    text(d, (cx - w / 2, base), num, fn, INK)
    if unit:
        text(d, (cx - w / 2 + fn.getlength(num) + 3, base), unit, fu, ACC)
    if label:
        fl = mono(max(18, int(r * .2)), 600)
        text(d, (cx, base + r * .3), label, fl, mix(ACC, BG, .85), tracking=2, anchor="ms")


def seg_bar(img, x, y, w, h, value, n=10, gap=5, slant=7):
    """A bar of n slanted segments; value 0..10 of them lit, the last one brightest."""
    v = max(0.0, min(10.0, float(value)))
    lit = int(round(v * n / 10))
    S = 3
    patch, d = aa_layer(w + slant + 2, h, S)
    sw = (w - gap * (n - 1)) / n
    gm = Image.new("L", (w + slant + 2, h), 0)
    gd = ImageDraw.Draw(gm)
    for i in range(n):
        x0 = i * (sw + gap)
        poly = [(x0 + slant, 0), (x0 + sw + slant, 0), (x0 + sw, h), (x0, h)]
        if i < lit:
            col = HI if i == lit - 1 else mix(ACC, DEEP, .6 + .4 * (i + 1) / max(1, lit))
            gd.polygon(poly, fill=255)
        else:
            col = OFF
        d.polygon([(px * S, py * S) for px, py in poly], fill=col + (255,))
    if lit:
        glow(img, gm, (x, y), ACC, radius=8, strength=.7)
    aa_done(img, patch, (x, y), (w + slant + 2, h))


def feel_row(img, x, y, w, item, label_font, word_font, bar_h=16, show_label=True):
    """One axis: the label, the low and high words at the ends (the one the value leans to
    lit), the segmented bar under them. y is the top; returns the height used."""
    label, value, low, high = _feel_item(item)
    d = ImageDraw.Draw(img)
    base = y + word_font.size * .8
    lean = value - 5
    low_c = INK if lean <= -1.5 else (DIM if lean < 1.5 else UNLIT)
    high_c = INK if lean >= 1.5 else (DIM if lean > -1.5 else UNLIT)
    lab = label.upper() if show_label else ""
    lab_w = tlen(label_font, lab, 2) + 36 if lab else 0
    # long words: the label in the middle goes (the words name the scale anyway), and
    # only then the longer word is cut; the size stays, so all rows look alike
    wf = word_font
    if lab and wf.getlength(low) + wf.getlength(high) + lab_w + 24 > w:
        lab, lab_w = "", 0
    wl, wh = wf.getlength(low), wf.getlength(high)
    if wl + wh + lab_w + 24 > w:
        # still too long: the shorter word keeps its width, the longer one is cut
        half = (w - 24) / 2
        room_lo = max(half, w - 24 - wh) if wl > wh else half
        room_hi = max(half, w - 24 - wl) if wh >= wl else half
        low, high = _clip("feel", low, wf, room_lo), _clip("feel", high, wf, room_hi)
        wl, wh = wf.getlength(low), wf.getlength(high)
    text(d, (x, base), low, wf, low_c)
    text(d, (x + w, base), high, wf, high_c, anchor="rs")
    if lab:
        text(d, (x + (wl + w - wh) / 2, base - 1), lab, label_font, mix(ACC, BG, .9), tracking=2, anchor="ms")
    by = int(base + word_font.size * .32)
    seg_bar(img, x, by, w - 8, bar_h, value)
    return by + bar_h - y


def _feel_item(item):
    try:
        label, value, low, high = (list(item) + ["", 5, "", ""])[:4]
        value = float(value if value is not None else 5)
    except (TypeError, ValueError):
        label, value, low, high = "", 5.0, "", ""
    return str(label or ""), max(0.0, min(10.0, value)), str(low or ""), str(high or "")


def freshness(recent) -> tuple[int | None, tuple]:
    """('96%' or 96) -> (96, dot color): green 85+, amber 70-85, red below."""
    if recent is None or recent == "":
        return None, FAINT
    m = re.search(r"\d+(?:[.,]\d+)?", str(recent))
    if not m:
        return None, FAINT
    v = float(m.group().replace(",", "."))
    v = round(v * 100) if v <= 1 and "%" not in str(recent) and isinstance(recent, float) else round(v)
    return v, GOOD if v >= 85 else ACC if v >= 70 else BAD


def dot(img, cx, cy, r, color):
    S = 4
    patch, d = aa_layer(2 * r + 2, 2 * r + 2, S)
    d.ellipse((S, S, (2 * r + 1) * S, (2 * r + 1) * S), fill=color + (255,))
    m = Image.new("L", (2 * r + 2, 2 * r + 2), 0)
    ImageDraw.Draw(m).ellipse((1, 1, 2 * r, 2 * r), fill=255)
    glow(img, m, (cx - r - 1, cy - r - 1), color, radius=r, strength=.8)
    aa_done(img, patch, (cx - r - 1, cy - r - 1), (2 * r + 2, 2 * r + 2))


def warn_icon(img, x, y, s, color=ACC):
    S = 4
    patch, d = aa_layer(s, s, S)
    d.polygon([(s * S / 2, S), (s * S - S, s * S - S), (S, s * S - S)], fill=color + (255,))
    f = sofia(int(s * .78) * S, 900)
    d.text((s * S / 2, s * S - 2.6 * S), "!", font=f, fill=BG + (255,), anchor="ms")
    aa_done(img, patch, (x, y), (s, s))


WARN_SIZES = (32, 29, 26)


def warning_layout(s, w):
    """(font, lines, plate height) of a warning in a strip w wide: one line at the largest
    size that holds it, else two lines (the second one cut only when even that is short)."""
    room = w - 92
    for size in WARN_SIZES:
        f = sofia(size, 600)
        if glen(f, s) <= room:
            return f, [s], 52
    f = sofia(28, 600)
    lines = wrap(s, f, room)
    if len(lines) > 2:
        lines = [lines[0], _clip("warning", " ".join(lines[1:]), f, room)]
    return f, lines, 86


def warning_strip(img, x, y, w, s, h=50, font=None, lines=None):
    """A warning on a dark plate with a scrap of signal tape at the left; font and lines
    come from warning_layout() (one line at 32 when they are not given)."""
    d = ImageDraw.Draw(img)
    plate = Image.new("L", (w, h), 0)
    ImageDraw.Draw(plate).rectangle((0, 0, w, h), fill=210)
    img.paste((26, 12, 6), (x, y, x + w, y + h), plate)
    img.paste(tape(22, h, 0), (x, y))
    d.line((x, y, x + w, y), fill=mix(ACC, BG, .45), width=1)
    d.line((x, y + h - 1, x + w, y + h - 1), fill=mix(ACC, BG, .45), width=1)
    warn_icon(img, x + 36, y + (h - 30) // 2, 30)
    f = font or sofia(32, 600)
    lines = lines or [_clip("warning", s, f, w - 92)]
    lh = f.size * 1.12
    base = y + h / 2 + 1 - lh * (len(lines) - 1) / 2
    for i, ln in enumerate(lines):
        text(d, (x + 78, base + i * lh), ln, f, INK, anchor="lm")


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def thousands(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def _match(view) -> float | None:
    m = view.get("match")
    if m is None:
        return None
    try:
        m = float(m)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, m / 100 if m > 1 else m))


PRICE_OFF = re.compile(r"\s*\(?\s*([−-]\s?\d+\s?%)\s*\)?\s*$")


def _price_parts(price):
    """'390 KZT −90%' or '1 299 ₽ (−75%)' -> ('390 KZT', '−90%'); any other text -> (text, '')."""
    m = PRICE_OFF.search(price)
    if not m or not price[:m.start()].strip():
        return price, ""
    return price[:m.start()].strip(), m.group(1).replace("-", "−").replace(" ", "")


def price_chip(d, x, y, price, font, h=48):
    """The price, and the discount in a solid amber block."""
    main, off = _price_parts(price)
    x = chip(d, x, y, main, font, fg=INK, border=mix(ACC, BG, .55), fill=PANEL, h=h)
    if off:
        x = chip(d, x - 2, y, off, font, fg=BG, fill=ACC, h=h, pad=10)
    return x


def price_width(price, font):
    main, off = _price_parts(price)
    if not off:
        return chip_width(main, font)
    return chip_width(main, font) + chip_width(off, font, pad=10) - 2


def _meta_items(view):
    return [(k, " ".join(str(view[k]).split())) for k in ("hours", "price", "deck")
            if view.get(k) and str(view[k]).strip()]


def _item_width(kind, s, font):
    return price_width(s, font) if kind == "price" else chip_width(s, font)


def meta_layout(view, w, sizes=(32,), gap=12):
    """(font, [(kind, text)]) of the hours, price and Steam Deck chips in one row w wide: the
    largest size from sizes at which all fit; a long Deck line keeps its first part (Deck
    over ProtonDB) and says 'Deck:' for 'Steam Deck:' before anything is cut, and only then
    the last chips are cut or dropped."""
    items = _meta_items(view)
    if not items:
        return sofia(sizes[0], 600), []

    def short(its):
        return [(k, "Deck:" + s[len("Steam Deck:"):] if k == "deck" and s.startswith("Steam Deck:") else s)
                for k, s in its]
    variants = [(items, False), (short(items), False)]       # (chips, lost a part)
    if any(k == "deck" and " · " in s for k, s in items):
        first = [(k, s.split(" · ")[0] if k == "deck" else s) for k, s in items]
        variants += [(first, True), (short(first), True)]

    def width(its, f):
        return sum(_item_width(k, s, f) for k, s in its) + gap * (len(its) - 1)
    for its, lost in variants:
        for size in sizes:
            f = sofia(size, 600)
            if width(its, f) <= w:
                if lost:
                    _note("deck")
                return f, its
    f = sofia(sizes[-1], 600)
    out, cx = [], 0
    for k, s in variants[-1][0]:
        cw = _item_width(k, s, f)
        if cx + cw > w:
            _note(k)
            if k != "price":
                s = clip(s, f, w - cx - 28)
                if len(s) >= 6:
                    out.append((k, s))
            break
        out.append((k, s))
        cx += cw + gap
    return f, out


def meta_chips(d, x, y, items, font, h=48, gap=12):
    """Chips from meta_layout() in one row; returns the width used."""
    cx = x
    for kind, s in items:
        if kind == "price":
            cx = price_chip(d, cx, y, s, font, h=h) + gap
        else:
            cx = chip(d, cx, y, s, font, fg=INK, border=mix(CHROME, BG, .6), fill=PANEL, h=h) + gap
    return cx - x - (gap if items else 0)


def genre_font(genres, w, sizes, gap=10, tracking=1.5):
    """The largest size from sizes at which all genres fit in one row of w."""
    for size in sizes:
        f = sofia(size, 700)
        total = sum(chip_width(str(g).upper(), f, tracking=tracking) for g in genres) + gap * (len(genres) - 1)
        if total <= w:
            return f
    return sofia(sizes[-1], 700)


def genre_chips(d, x, y, w, genres, font, h=46, gap=10, max_rows=1):
    """Genre tags, as many as fit in max_rows rows; returns the rows used."""
    cx, row = x, 0
    for g in genres:
        s = str(g).upper()
        cw = chip_width(s, font, tracking=1.5)
        if cx + cw > x + w:
            if cx > x and row + 1 < max_rows:
                row += 1
                cx = x
            elif cx == x:
                s = _clip("genres", s, font, w - 30, 1.5)
                cw = chip_width(s, font, tracking=1.5)
            else:
                continue
        chip(d, cx, y + row * (h + gap), s, font, fg=HI, border=mix(ACC, BG, .5),
             fill=(26, 16, 6), h=h, tracking=1.5)
        cx += cw + gap
    return row + 1 if genres else 0


def judge_tag(img, x, y, right=False):
    """'ВЫБОР ИИ' in hologram cyan. x is the left edge, or the right with right=True."""
    f = mono(26, 700)
    s = "ВЫБОР ИИ"
    w = int(tlen(f, s, 2)) + 56
    if right:
        x -= w
    h = 46
    plate = Image.new("L", (w, h), 220)
    img.paste((4, 18, 22), (x, y, x + w, y + h), plate)
    glow(img, rect_mask(w, h, 2), (x, y), CYAN, radius=10, strength=.8)
    d = ImageDraw.Draw(img)
    d.rectangle((x, y, x + w - 1, y + h - 1), outline=CYAN, width=2)
    cx, cy = x + 22, y + h / 2
    d.polygon([(cx, cy - 9), (cx + 9, cy), (cx, cy + 9), (cx - 9, cy)], fill=CYAN)
    text(d, (x + 40, cy + 1), s, f, (214, 250, 255), tracking=2, anchor="lm")
    return w


def rank_tag(img, x, y, rank):
    """'#1' on an amber notch."""
    f = sofia(54, 900)
    s = f"#{rank}"
    w = int(f.getlength(s)) + 46
    h = 64
    d = ImageDraw.Draw(img)
    poly = [(x, y), (x + w, y), (x + w - 18, y + h), (x, y + h)]
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).polygon([(px - x, py - y) for px, py in poly], fill=255)
    glow(img, m, (x, y), ACC, radius=14, strength=.9)
    img.paste(vgradient(w, h, ((0, HI), (.45, ACC), (1, DEEP))), (x, y), m)
    text(d, (x + 16, y + h / 2 + 2), s, f, BG, anchor="lm")
    return w


def confidence_badge(img, x, y, view, right=True):
    """How sure the breakdown is, from view["confidence"]: "reviews" -> a green
    'РАЗБОР ПО N ОТЗЫВАМ' (N = view["reviews_used"]), "tags" -> a muted amber
    'ОЦЕНКА ПО ТЕГАМ'; anything else draws nothing. x is the left edge, or the right with
    right=True; y is the top. Returns the width (0 when nothing was drawn)."""
    kind = str(view.get("confidence") or "").strip().lower()
    if kind == "reviews":
        try:
            n = max(0, int(view.get("reviews_used") or 0))
        except (TypeError, ValueError):
            n = 0
        s = f"РАЗБОР ПО {n} {plural(n, 'ОТЗЫВУ', 'ОТЗЫВАМ', 'ОТЗЫВАМ')}" if n else "РАЗБОР ПО ОТЗЫВАМ"
        col, plate_c, ink = GOOD, (6, 20, 9), (206, 240, 208)
    elif kind == "tags":
        s = "ОЦЕНКА ПО ТЕГАМ"
        col, plate_c, ink = mix(ACC, CHROME, .5), (24, 18, 12), (226, 212, 192)
    else:
        return 0
    f = mono(22, 700)
    h = 40
    w = int(tlen(f, s, 1.5)) + 52
    if right:
        x -= w
    plate = Image.new("L", (w, h), 228)
    img.paste(plate_c, (x, y, x + w, y + h), plate)
    if kind == "reviews":
        glow(img, rect_mask(w, h, 2), (x, y), col, radius=8, strength=.55)
    d = ImageDraw.Draw(img)
    d.rectangle((x, y, x + w - 1, y + h - 1), outline=col, width=2)
    cx, cy = x + 20, y + h / 2
    if kind == "reviews":
        # three bars, like the signal meters next to praise and complaints
        for i in range(3):
            bh = 6 + i * 4
            d.rectangle((cx - 9 + i * 7, cy + 7 - bh, cx - 5 + i * 7, cy + 7), fill=col)
    else:
        d.polygon([(cx, cy - 8), (cx + 8, cy), (cx, cy + 8), (cx - 8, cy)], outline=col, width=2)
    text(d, (x + 38, cy + 1), s, f, ink, tracking=1.5, anchor="lm")
    return w


def eyebrow(d, x, y, s, size=26, color=ACC, anchor="ls"):
    return text(d, (x, y), s, mono(size, 600), color, tracking=3, anchor=anchor)


def _out(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, **JPEG)
    return buf.getvalue()


# ---------------------------------------------------------------- cards

def title_block(img, x, top, bottom, w, s, max_lines, sizes, valign="middle", glow_strength=.55):
    """A game's name in the brand title style, as large as fits both the width and the
    height between top and bottom. Returns (y of the last baseline, bottom of the block)."""
    s = s.upper()
    sizes = list(sizes)
    font, lines = fit(s, lambda z: sofia(z, 900), w, max_lines, sizes)
    while True:
        cap = font.size * .65
        lh = int(font.size * .9)
        block = cap + lh * (len(lines) - 1) + font.size * .08
        smaller = [z for z in sizes if z < font.size]
        if block <= bottom - top or not smaller:
            break
        font, lines = fit(s, lambda z: sofia(z, 900), w, max_lines, smaller)
    if lines and lines[-1].endswith("…") and not s.endswith("…"):
        _note("name")
    y0 = top if valign == "top" else top + max(0, (bottom - top - block) / 2)
    y = y0 + cap
    for ln in lines:
        grad_text(img, (x, y), ln, font, depth=max(3, font.size // 18), glow_strength=glow_strength)
        y += lh
    return y - lh, y0 + block


def stat_block(img, x, cy, w, view, big=60, small=30, compact=False):
    """Next to the ring: what the ring shows, then the share of fresh reviews that praise
    the game with its coloured dot, then the number of reviews. cy is the ring's center."""
    d = ImageDraw.Draw(img)
    m = _match(view)
    if compact:
        # one caption line instead of two, so the block is no taller than a small ring
        eyebrow(d, x, cy - 30, clip("СОВПАДЕНИЕ С ЗАПРОСОМ" if m is not None else "СВЕЖИЕ ОТЗЫВЫ",
                                    mono(22, 600), w, 3), size=22, color=mix(ACC, BG, .92))
        div = cy - 16
    else:
        eyebrow(d, x, cy - 40, "СОВПАДЕНИЕ" if m is not None else "СВЕЖИЕ ОТЗЫВЫ", color=mix(ACC, BG, .92))
        sub = "с запросом" if m is not None else "положительные"
        text(d, (x, cy - 4), sub, sofia(small, 500), DIM)
        div = cy + 14
    d.line((x, div, x + w, div), fill=mix(ACC, BG, .28), width=1)
    pct, col = freshness(view.get("recent"))
    base = div + big * .84 + (4 if compact else 0)
    fb = sofia(big, 900)
    fs = sofia(small, 500)
    if m is not None:
        if pct is not None:
            dot(img, x + 11, int(base - big * .33), 11, col)
            d = ImageDraw.Draw(img)
            end = text(d, (x + 32, base), f"{pct}%", fb, INK)
            text(d, (end + 12, base), _clip("stats", "свежих хвалят", fs, x + w - end - 12), fs, DIM)
        else:
            base = div + small * 1.3
            text(d, (x, base), "свежих отзывов мало", fs, UNLIT)
    else:
        base = div + 4
    total = view.get("reviews_total") or 0
    if total:
        t = f"{thousands(total)} {plural(total, 'отзыв', 'отзыва', 'отзывов')} всего"
        f = sofia(small - 2, 500)
        text(d, (x, base + small + 6), _clip("stats", t, f, w), f, UNLIT)


def pick_card(view: dict, cover: bytes | None) -> bytes:
    """One recommendation of a selection, 1280x720 JPEG."""
    LAST_CLIPPED.clear()
    W, H, M = 1280, 720, 44
    name = str(view.get("name") or "Без названия")
    cov = _open_cover(cover)
    img = backdrop(W, H, cov, seed=name, glow_at=(.27, .32))
    tapes(img)

    # left: the cover with the rank, the AI pick and how sure the breakdown is on it
    cx0, cy0, cw, ch = M, M + 6, 600, 344
    framed_cover(img, cov, (cx0, cy0, cw, ch), name)
    if view.get("rank"):
        rank_tag(img, cx0 - 2, cy0 + 22, view["rank"])
    if view.get("judge"):
        judge_tag(img, cx0 + cw - 18, cy0 + ch - 64, right=True)
    confidence_badge(img, cx0 + cw - 16, cy0 + 16, view)

    # bottom left: genres, then hours, price and Steam Deck; the name between them and the cover.
    # Long chips get a smaller font (never below 26) rather than an ellipsis.
    d = ImageDraw.Draw(img)
    chips_y = H - 12 - 24 - 48
    meta_font, meta = meta_layout(view, cw, sizes=(32, 30, 28, 26))
    has_meta = bool(meta)
    genres = [" ".join(str(g).split()) for g in (view.get("genres") or []) if g and str(g).strip()][:3]
    genre_y = chips_y - 14 - 46 if has_meta else chips_y
    if has_meta:
        meta_chips(d, cx0, chips_y, meta, meta_font, h=48)
    if genres:
        genre_chips(d, cx0, genre_y, cw, genres, genre_font(genres, cw, (30, 28, 26)), h=46)
    low = genre_y if genres else chips_y if has_meta else H - 40
    title_block(img, cx0, cy0 + ch + 30, low - 26, cw, name, 2, range(112, 51, -4))

    # right: the match ring and fresh reviews
    rx, rw = 692, W - M - 692
    r = 86
    m = _match(view)
    pct, _ = freshness(view.get("recent"))
    rcy = cy0 + r + 2
    if m is not None:
        ring(img, rx + r, rcy, r, m, f"{round(m * 100)}%")
    else:
        ring(img, rx + r, rcy, r, (pct or 0) / 100, f"{pct}%" if pct is not None else "—")
    sx = rx + 2 * r + 34
    stat_block(img, sx, rcy, W - M - sx, view)

    # feel: four axes, and the warning at the bottom (two lines when it is long)
    feel = list(view.get("feel") or [])[:4]
    warning = " ".join(str(view.get("warning") or "").split())
    wf_, wlines, wh = warning_layout(warning, rw) if warning else (None, [], 0)
    fy = rcy + r + 46
    bottom = H - 12 - 24 - (wh + 26 if warning else 0)
    if feel:
        step = min(86, (bottom - fy) / len(feel))
        lf, wf = mono(24, 600), sofia(30, 600)
        for i, item in enumerate(feel):
            feel_row(img, rx, int(fy + i * step), rw, item, lf, wf, bar_h=16)
    if warning:
        warning_strip(img, rx, H - 12 - 24 - wh, rw, warning, h=wh, font=wf_, lines=wlines)
    return _out(img)


SHARE_LEVELS = (("большинств", 3), ("почти все", 3), ("most", 3), ("многие", 2), ("many", 2),
                ("часть", 1), ("некоторые", 1), ("немногие", 1), ("some", 1), ("few", 1))


def share_level(share: str) -> int:
    """'большинство' 3, 'многие' 2, 'некоторые' 1, '35%' by the number; 0 when unknown."""
    s = (share or "").lower()
    for key, level in SHARE_LEVELS:
        if key in s:
            return level
    m = re.search(r"\d+", s)
    if m:
        v = int(m.group())
        return 3 if v >= 50 else 2 if v >= 25 else 1
    return 0


def signal(d, x, y_bottom, level, color):
    """Three bars like a signal meter: how many players say it."""
    for i in range(3):
        h = 10 + i * 7
        x0 = x + i * 9
        d.rectangle((x0, y_bottom - h, x0 + 5, y_bottom), fill=color if i < level else OFF)


GAME_MIN_H, GAME_MAX_H = 1100, 2400     # game_card grows with its text instead of cutting it
GAME_REF_H = 1280                       # the hero's backdrop is laid out on this height
STATE_LINES, SUMMARY_LINES, ITEM_LINES, COLUMN_ITEMS = 4, 8, 4, 5


def _wrap_cap(field, s, font, width, max_lines):
    """s wrapped; past max_lines the last line is cut with an ellipsis and field noted."""
    lines = wrap(s, font, width)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [clip(" ".join(lines[max_lines - 1:]), font, width)]
        _note(field)
    return lines


def _summary_lines(s, font, width, max_lines):
    """The summary wrapped in full; when longer than max_lines it ends at the last sentence
    that fits (cut with an ellipsis only when that would leave less than half)."""
    lines = wrap(s, font, width)
    if len(lines) <= max_lines:
        return lines
    _note("summary")
    parts = re.split(r"(?<=[.!?…])\s+", s)
    lo, hi = 0, len(parts) - 1          # the most sentences that fit: binary search
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(wrap(" ".join(parts[:mid]), font, width)) <= max_lines:
            lo = mid
        else:
            hi = mid - 1
    head = wrap(" ".join(parts[:lo]), font, width) if lo else []
    if head and len(head) * 2 >= max_lines:
        return head
    return lines[:max_lines - 1] + [clip(" ".join(lines[max_lines - 1:]), font, width)]


def game_card(view: dict, cover: bytes | None) -> bytes:
    """A detailed breakdown of one game, a 1280 wide JPEG; the height (GAME_MIN_H to
    GAME_MAX_H) is whatever its text needs, so nothing is cut: everything is measured first
    with the real fonts, then the canvas is made and drawn."""
    LAST_CLIPPED.clear()
    W, M = 1280, 44
    name = str(view.get("name") or "Без названия")
    cov = _open_cover(cover)
    cx0, cy0, cw, ch = M, 48, 544, 312
    full = W - 2 * M
    col_w = (W - 2 * M - 56) // 2
    top = cy0 + ch + 30

    # ---- measure
    # chips (hours, price, Deck), then how the engaged players feel: after the chips when
    # it fits there, else on its own lines
    meta_font, meta = meta_layout(view, full, sizes=(30,))
    used = sum(_item_width(k, t, meta_font) for k, t in meta) + 12 * (len(meta) - 1) if meta else 0
    engaged = " ".join(str(view.get("engaged") or "").split())
    ef = sofia(32, 600)
    ex = M + used + (14 if used else 0)
    eng_inline = bool(engaged) and W - M - ex > 220 and glen(ef, engaged) <= W - M - ex - 24
    eng_lines = _wrap_cap("engaged", engaged, ef, full - 24, 2) if engaged and not eng_inline else []
    state = " ".join(str(view.get("state_now") or "").split())
    stf = sofia(32, 600)
    st_lines = _wrap_cap("state", state, stf, full - 24, STATE_LINES) if state else []
    summary = " ".join(str(view.get("summary") or "").split())
    sf = sofia(36, 500)
    feel = list(view.get("feel") or [])[:12]
    rows = math.ceil(len(feel) / 2)
    step = 60
    pf = sofia(30, 600)

    def column(items, field):
        out = []
        for it in items:
            txt, share, taste = _review_item(it)
            if txt:
                out.append((_wrap_cap(field, txt, pf, col_w - 40, ITEM_LINES), share, taste))
        return out[:COLUMN_ITEMS]
    praise = list(view.get("praise") or [])
    complaints = list(view.get("complaints") or [])
    cols = [column(praise, "praise"), column(complaints, "complaints")]

    def col_h(items):
        return 40 + (sum(len(ln) * 36 + 10 for ln, _, _ in items) if items else 46)

    def need(s_lines):
        y = top
        y += 62 if meta or eng_inline else 0
        y += 46 + (len(eng_lines) - 1) * 40 if eng_lines else 0
        y += 46 + (len(st_lines) - 1) * 40 if st_lines else 0
        y += 6 + len(s_lines) * 42 + 16 if s_lines else 0
        y += 44 + (rows - 1) * step + 50 if feel else 0
        y += 18 + max(col_h(cols[0]), col_h(cols[1])) if praise or complaints else 0
        return y + 12 + 28

    s_lines = _summary_lines(summary, sf, full - 24, SUMMARY_LINES) if summary else []
    # past the cap (only with absurd text): the last items of the longer column go first,
    # then summary lines
    while need(s_lines) > GAME_MAX_H:
        longer = max((0, 1), key=lambda i: col_h(cols[i]))
        if len(cols[longer]) > 2:
            cols[longer] = cols[longer][:-1]
            _note(("praise", "complaints")[longer])
        elif len(s_lines) > 4:
            s_lines = _summary_lines(summary, sf, full - 24, len(s_lines) - 1)
        else:
            break
    H = max(GAME_MIN_H, min(GAME_MAX_H, need(s_lines)))

    # ---- draw: below the hero the background goes to near-black, so long text stays readable
    ref = GAME_REF_H
    img = backdrop(W, H, cov, seed=name, glow_at=(.25, .16), dim=.70, ref_h=ref,
                   fade=((0, 0), (.24 * ref / H, 0), (.38 * ref / H, 215), (1, 235)))
    tapes(img)

    # hero: cover left; the name, genres, the ring and fresh reviews right
    framed_cover(img, cov, (cx0, cy0, cw, ch), name)
    if view.get("rank"):
        rank_tag(img, cx0 - 2, cy0 + 20, view["rank"])
    if view.get("judge"):
        judge_tag(img, cx0 + cw - 18, cy0 + ch - 62, right=True)
    confidence_badge(img, cx0 + cw - 16, cy0 + 16, view)
    rx = cx0 + cw + 48
    rw = W - M - rx
    r = 56
    ring_top = cy0 + ch - 2 * r
    genres, seen = [], set()
    for g in list(view.get("genres") or []) + list(view.get("store_genres") or []):
        if g and str(g).strip().lower() not in seen:
            seen.add(str(g).strip().lower())
            genres.append(" ".join(str(g).split()))
    title_bottom = ring_top - 22 - (42 + 18 if genres else 0)
    _, tb = title_block(img, rx, cy0 - 2, title_bottom, rw, name, 2, range(100, 47, -4), valign="top",
                        glow_strength=.5)
    d = ImageDraw.Draw(img)
    if genres:
        genre_chips(d, rx, int(tb + 18), rw, genres[:5], sofia(28, 700), h=42)
    m = _match(view)
    pct, _ = freshness(view.get("recent"))
    rcy = ring_top + r
    if m is not None:
        ring(img, rx + r, rcy, r, m, f"{round(m * 100)}%", thick=11)
    else:
        ring(img, rx + r, rcy, r, (pct or 0) / 100, f"{pct}%" if pct is not None else "—", thick=11)
    sx = rx + 2 * r + 28
    stat_block(img, sx, rcy - 4, W - M - sx, view, big=50, small=28, compact=True)
    d = ImageDraw.Draw(img)

    def marker(y):
        d.polygon([(M, y + 12), (M + 12, y + 21), (M, y + 30)], fill=ACC)

    y = top
    # chips: hours, price, Deck; after them how the engaged players feel
    if meta or eng_inline:
        if meta:
            meta_chips(d, M, y, meta, meta_font, h=46)
        if eng_inline:
            d.polygon([(ex, y + 14), (ex + 12, y + 23), (ex, y + 32)], fill=ACC)
            text(d, (ex + 24, y + 34), engaged, ef, INK)
        y += 46 + 16
    if eng_lines:
        marker(y)
        for i, ln in enumerate(eng_lines):
            text(d, (M + 24, y + 32 + i * 40), ln, ef, INK)
        y += 46 + (len(eng_lines) - 1) * 40
    # the state now, as many lines as it takes
    if st_lines:
        marker(y)
        for i, ln in enumerate(st_lines):
            text(d, (M + 24, y + 32 + i * 40), ln, stf, mix(INK, BG, .92))
        y += 46 + (len(st_lines) - 1) * 40
    # summary in full, with a thin amber rule at the left like a quote
    if s_lines:
        y += 6
        d.rectangle((M, y, M + 3, y + len(s_lines) * 42 - 10), fill=ACC)
        for i, ln in enumerate(s_lines):
            text(d, (M + 24, y + 29 + i * 42), ln, sf, INK if i == 0 else mix(INK, BG, .88))
        y += len(s_lines) * 42 + 16

    # feel: twelve axes in two columns
    if feel:
        eyebrow(d, M, y + 22, "ОЩУЩЕНИЯ ПО ОТЗЫВАМ")
        d.line((M + 400, y + 13, W - M, y + 13), fill=mix(ACC, BG, .3), width=1)
        y += 44
        lf, wf = mono(20, 600), sofia(28, 600)
        for i, item in enumerate(feel):
            col, row = i // rows, i % rows
            feel_row(img, M + col * (col_w + 56), y + row * step, col_w, item, lf, wf, bar_h=12)
        y += (rows - 1) * step + 50
    d = ImageDraw.Draw(img)

    # praise and complaints side by side, every item in full; the bars say how many players say it
    if praise or complaints:
        y += 18
        taste_any = any(taste for _, _, taste in cols[1])
        for col, (head, items, kind) in enumerate((("ХВАЛЯТ", cols[0], "plus"), ("РУГАЮТ", cols[1], "minus"))):
            x = M + col * (col_w + 56)
            eyebrow(d, x, y + 22, head, color=GOOD if kind == "plus" else mix(BAD, INK, .85))
            if kind == "minus" and taste_any:
                fl = mono(20, 600)
                lx = x + col_w - tlen(fl, "ДЕЛО ВКУСА", 1.5)
                text(d, (lx, y + 21), "ДЕЛО ВКУСА", fl, CYAN, tracking=1.5)
                d.rectangle((lx - 22, y + 6, lx - 10, y + 18), fill=CYAN)
            yy = y + 40
            if not items:
                text(d, (x, yy + 30), "ничего заметного", pf, UNLIT)
                continue
            for lines, share, taste in items:
                color = GOOD if kind == "plus" else (CYAN if taste else BAD)
                level = share_level(share)
                signal(d, x, yy + 32, level or 1, color if level else mix(color, BG, .5))
                for i, ln in enumerate(lines):
                    text(d, (x + 40, yy + 31 + i * 36), ln, pf, INK)
                yy += len(lines) * 36 + 10
    return _out(img)


def _review_item(it):
    if isinstance(it, str):
        return " ".join(it.split()), "", False
    if not isinstance(it, (list, tuple)):
        it = [it] if it is not None else []
    it = list(it) + ["", "", False]
    return " ".join(str(it[0] or "").split()), str(it[1] or ""), bool(it[2])


def selection_banner(title: str, subtitle: str, covers: list[bytes | None]) -> bytes:
    """The header of a selection, 1280x480 JPEG: a big title and up to three covers fanned."""
    title, subtitle, covers = title or "", subtitle or "", list(covers or [])
    W, H = 1280, 480
    covs = [_open_cover(c) for c in (covers or [])][:3]
    n = len(covs)
    img = backdrop(W, H, None, seed=title + subtitle, glow_at=(.74, .5) if n else (.3, .5), dim=.8,
                   ids_at=[(-30, 404), (700, 26)] if n else [(-30, 404), (760, 380)])
    band = 14

    # covers fanned at the right: the first one in front, the others behind it
    if n:
        cw, chh = 300, 172
        cx, cy = 944, H / 2 + 4
        slots = {1: [(0, 0, 0, 0)],
                 2: [(1, 120, 8, -7), (0, -70, -6, 4)],
                 3: [(1, -150, -4, 9), (2, 150, 6, -9), (0, 0, 0, 0)]}[n]
        for i, dx, dy, ang in slots:
            front = i == 0
            k = 1.14 if front else 1.0
            _fan_card(img, covs[i], int(cw * k), int(chh * k), cx + dx, cy + dy, ang, front, name=str(i + 1))

    # the title block at the left, centered between the tapes
    d = ImageDraw.Draw(img)
    x = 64
    room = (590 if n else W - 2 * x) - x
    font, lines = fit(str(title or "").upper(), lambda z: sofia(z, 900), room, 2, range(156, 71, -6))
    lh = int(font.size * .86)
    cap = font.size * .65
    f = mono(28, 600)
    subs = []
    if subtitle:
        # break at the ' · ' between parts first, inside a part only when it is too long
        cur = ""
        for part in (p.strip() for p in " ".join(str(subtitle).split()).split("·")):
            if not part:
                continue
            t = f"{cur} · {part}" if cur else part
            if tlen(f, t, 1.0) <= room - 30:
                cur = t
                continue
            if cur:
                subs.append(cur)
            cur = part
        if cur:
            subs.append(cur)
        subs = [ln for s in subs for ln in (wrap(s, f, room - 30) if tlen(f, s, 1.0) > room - 30 else [s])]
        if len(subs) > 2:
            subs = [subs[0], clip(" · ".join(subs[1:]), f, room - 30)]
    block = 26 + 26 + cap + lh * (len(lines) - 1) + (12 + 40 * len(subs) if subs else 0)
    y = band + (H - 2 * band - block) / 2 + 26
    eyebrow(d, x, y, "// SILVERHAND · GAME FINDER", size=26)
    y += 26 + cap
    for ln in lines:
        grad_text(img, (x, y), ln, font, depth=max(4, font.size // 18), glow_strength=.8)
        y += lh
    d = ImageDraw.Draw(img)
    y = y - lh + 12
    end = x
    for ln in subs:
        y += 40
        end = text(d, (x, y), ln, f, INK, tracking=1.0)
    if subs:
        d.rectangle((end + 10, y - 22, end + 24, y + 4), fill=ACC)
    tapes(img, band=band)
    return _out(img)


def _fan_card(img, art, w, h, cx, cy, angle, front, name=""):
    """A cover tilted by angle with a frame, a shadow and a glow, centered at (cx, cy)."""
    pic = ImageOps.fit(art, (w, h), Image.LANCZOS) if art is not None else placeholder(w, h, name)
    if not front:
        pic = Image.blend(pic, Image.new("RGB", (w, h), BG), .35)
    b = 4 if front else 3
    card = Image.new("RGBA", (w + 2 * b, h + 2 * b), (ACC if front else mix(ACC, BG, .7)) + (255,))
    card.paste(pic, (b, b))
    if front:
        ImageDraw.Draw(card).line((b, b, w + b - 1, b), fill=HI + (255,), width=1)
    rot = card.rotate(angle, resample=Image.BICUBIC, expand=True) if angle else card
    alpha = rot.getchannel("A")
    x, y = int(cx - rot.width / 2), int(cy - rot.height / 2)
    # a shadow under it, then the amber light around it
    sh = alpha.resize((alpha.width // 4, alpha.height // 4)).filter(ImageFilter.GaussianBlur(5))
    sh = sh.resize(alpha.size, Image.BILINEAR).point(lambda v: int(v * .85))
    img.paste((0, 0, 0), (x + 10, y + 18, x + 10 + rot.width, y + 18 + rot.height), sh)
    glow(img, alpha, (x, y), ACC, radius=24 if front else 16, strength=1.0 if front else .45)
    img.paste(rot, (x, y), rot)
    if front:
        brackets(ImageDraw.Draw(img), x + b, y + b, x + rot.width - b, y + rot.height - b,
                 length=22, width=3, color=HI, gap=10)
