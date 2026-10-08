"""Animated cards: a short seamless MP4 loop made from the static card of render.py.

    pick_card_mp4(view, cover)            -> MP4 960x540, 2 s at 15 fps, or None
    banner_mp4(title, subtitle, covers)   -> MP4 960x360, 2 s at 15 fps, or None

The card is drawn once by render.pick_card / render.selection_banner and scaled to the
output size; per frame only cheap overlays change, the motion of tools/make_intro.py:
the signal tape at the top and bottom runs sideways, a soft diagonal glint crosses the
cover, the amber accents (the match ring, frames, the title) breathe, and a faint band
of scanlines drifts down. Every motion fits exactly into one period, so the loop has no
seam, and frame 0 is the static card (the thumbnail Telegram shows before playback).

Encoding is the bundled ffmpeg of imageio-ffmpeg (or ffmpeg on PATH): raw RGB frames are
streamed to its stdin, one at a time, so memory stays at about two frames plus x264;
H.264 yuv420p, no audio, +faststart. One encode at a time (the VPS has 300 MB and 30%
of a core); anything that goes wrong returns None and the bot sends the JPEG instead.

The layout regions (tape height, the cover box, the ring) mirror render.py's code; if the
layout there changes, update PICK_* / BANNER_* below (tests_animate.py checks them).

    python -m gamefinder.animate   -> assets/brand/cards/pick-1.mp4 and pick-1-preview.gif
"""

from __future__ import annotations

import io
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import render

log = logging.getLogger(__name__)

FPS = 15
SECONDS = 2.0
SCALE = 0.75                 # 1280x720 -> 960x540: a third fewer pixels, still sharp on a phone
CRF = 25
PRESET = "veryfast"
THREADS = 1                  # under a 30% CPU quota more threads only add memory
TIMEOUT = 20.0               # seconds for one encode, then ffmpeg is killed
SLOT_WAIT = 15.0             # how long to wait for the encoder before giving up (-> JPEG)
FFMPEG_EXE: str | None = None  # explicit path; None -> imageio-ffmpeg, then ffmpeg on PATH
TAPE_PERIODS = 2             # tape stripes pass this many periods per loop
GLINT = (255, 246, 226)
GLOW = .42                   # strength of the breathing light at its peak

# render.pick_card: W, H, M = 1280, 720, 44; tapes(img) with band 12; the cover at
# (M, M + 6, 600, 344); the ring r = 86 at (rx + r, cy0 + r + 2) with rx = 692.
PICK_SIZE = (1280, 720)
PICK_TAPE = 12
PICK_COVER = (44, 50, 600, 344)
PICK_RING = (692 + 86, 50 + 86 + 2, 86)
# render.selection_banner: 1280x480, tapes band 14; covers fanned by _fan_card around
# (944, H / 2 + 4), 300x172 (the front one x1.14), slots (index, dx, dy, angle).
BANNER_SIZE = (1280, 480)
BANNER_TAPE = 14
BANNER_FAN = dict(cx=944, cy=480 / 2 + 4, w=300, h=172, front=1.14)
BANNER_SLOTS = {1: [(0, 0, 0, 0)],
                2: [(1, 120, 8, -7), (0, -70, -6, 4)],
                3: [(1, -150, -4, 9), (2, 150, 6, -9), (0, 0, 0, 0)]}

_SLOTS = threading.BoundedSemaphore(1)
_EXE: list[str] = []


# ---------------------------------------------------------------- public

def pick_card_mp4(view: dict, cover: bytes | None) -> bytes | None:
    """One recommendation as a looping MP4 (960x540, ~2 s); None on any failure."""
    try:
        t0 = time.perf_counter()
        base = _decode(render.pick_card(view, cover))
        x, y, w, h = PICK_COVER
        cover_poly = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        scene = Scene(base, SCALE, PICK_TAPE, glint_poly=cover_poly,
                      art=[[(x + 2, y + 2), (x + w - 3, y + 2), (x + w - 3, y + h - 3), (x + 2, y + h - 3)]],
                      ring=PICK_RING)
        data = _encode(scene)
        log.debug("pick mp4 %d KB in %.2f s", len(data or b"") // 1024, time.perf_counter() - t0)
        return data
    except Exception as e:  # noqa: BLE001 - animation is decoration: the JPEG goes instead
        log.warning("pick mp4: %s", e)
        return None


def banner_mp4(title, subtitle, covers) -> bytes | None:
    """The header of a selection as a looping MP4 (960x360, ~2 s); None on any failure."""
    try:
        covers = list(covers or [])
        base = _decode(render.selection_banner(title, subtitle, covers))
        n = min(3, len(covers))
        polys = [_fan_poly(i, dx, dy, ang) for i, dx, dy, ang in BANNER_SLOTS.get(n, [])]
        front = polys[-1] if polys else None      # the front card is drawn last
        # without covers the glint runs over the amber title instead
        scene = Scene(base, SCALE, BANNER_TAPE, glint_poly=front, art=polys, ring=None,
                      glint_text=front is None)
        return _encode(scene)
    except Exception as e:  # noqa: BLE001
        log.warning("banner mp4: %s", e)
        return None


# ---------------------------------------------------------------- the scene

def smooth(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


class Scene:
    """The static card at output size plus the precomputed overlays; frame(t), t in 0..1."""

    def __init__(self, card: Image.Image, scale: float, tape: int, glint_poly=None, art=(),
                 ring=None, glint_text=False):
        W, H = card.size
        self.w, self.h = w, h = int(W * scale) // 2 * 2, int(H * scale) // 2 * 2
        self.s = s = w / W
        base = card if (w, h) == card.size else card.resize((w, h), Image.LANCZOS)
        self.base = base

        # tape: drawn at 4x and scaled down, so it can move by a fraction of a pixel
        self.band = band = max(4, round(tape * s))
        self.period = 2 * band
        self.tape_master = render.tape((w + 2 * self.period) * 4, band * 4, 0.12)

        # amber accents: the card's own amber pixels outside the cover art and the tapes,
        # their light blurred at a quarter size; it breathes over the loop
        r, g, b = base.split()
        hot = r.point(lambda v: 255 if v >= 185 else 0)
        warm = ImageChops.subtract(r, b).point(lambda v: 255 if v >= 115 else 0)
        acc = ImageChops.multiply(hot, warm)
        d = ImageDraw.Draw(acc)
        for poly in art:
            d.polygon([(px * s, py * s) for px, py in poly], fill=0)
        d.rectangle((0, 0, w, band + 1), fill=0)
        d.rectangle((0, h - band - 2, w, h), fill=0)
        self.acc_hard = acc
        q = 4
        light = acc.resize((w // q, h // q), Image.BOX).filter(ImageFilter.GaussianBlur(2.2))
        if ring:
            # the match ring gets a halo of its own, a little stronger than the rest
            rcx, rcy, rr = (v * s / q for v in ring)
            halo = Image.new("L", light.size, 0)
            ImageDraw.Draw(halo).ellipse((rcx - rr, rcy - rr, rcx + rr, rcy + rr), outline=170,
                                         width=max(2, round(14 * s / q)))
            light = ImageChops.lighter(light, halo.filter(ImageFilter.GaussianBlur(3.5)))
        light = light.resize((w, h), Image.BILINEAR).point(lambda v: min(255, int(v * 1.15)))
        self.glow = Image.composite(Image.new("RGB", (w, h), render.ACC), Image.new("RGB", (w, h)), light)
        self.glow_box = light.getbbox()
        if self.glow_box:
            self.glow = self.glow.crop(self.glow_box)

        # glint: a soft diagonal band with a thin bright line, clipped to the cover
        # (or, with glint_text, to the amber title letters)
        self.glint = None
        if glint_poly is not None or glint_text:
            if glint_poly is not None:
                pts = [(px * s, py * s) for px, py in glint_poly]
                x0, y0 = int(min(p[0] for p in pts)), int(min(p[1] for p in pts))
                x1, y1 = int(math.ceil(max(p[0] for p in pts))), int(math.ceil(max(p[1] for p in pts)))
                clip = Image.new("L", (x1 - x0, y1 - y0), 0)
                ImageDraw.Draw(clip).polygon([(px - x0, py - y0) for px, py in pts], fill=255)
                peak = 1.0
            else:
                box = acc.getbbox() or (0, 0, w, h)
                x0, y0, x1, y1 = box
                clip = acc.crop(box)
                peak = 1.5
            self.glint = ((x0, y0, x1, y1), clip, _glint_band(y1 - y0, s, peak))

        # drifting scanlines: a soft warm band, brighter on every third row
        bh = max(24, int(h * .16))
        bar = Image.new("RGB", (1, bh))
        px = bar.load()
        for y in range(bh):
            k = math.sin(math.pi * (y + .5) / bh) ** 2 * (1.0 if y % 3 == 1 else .45)
            px[0, y] = (round(22 * k), round(15 * k), round(7 * k))
        self.bar = bar.resize((w, bh), Image.NEAREST)

    def frame(self, t: float) -> Image.Image:
        """t is the fraction of the loop; frame(0) is the static card, frame(1) == frame(0)."""
        w, h = self.w, self.h
        img = self.base.copy()

        # breathing light on the amber accents: 0 at t=0, the strongest at the middle
        amp = .5 - .5 * math.cos(math.tau * t)
        if self.glow_box and amp > .02:
            lut = [int(v * amp * GLOW) for v in range(256)] * 3
            box = self.glow_box
            img.paste(ImageChops.add(img.crop(box), self.glow.point(lut)), box[:2])

        # scanline band drifting down, out of sight at both ends of the loop
        bh = self.bar.height
        y = round(-bh + t * (h + bh))
        top, bot = max(0, y), min(h, y + bh)
        if bot > top:
            part = self.bar.crop((0, top - y, w, bot - y))
            img.paste(ImageChops.add(img.crop((0, top, w, bot)), part), (0, top))

        # glint across the cover in the first half of the loop
        if self.glint:
            (x0, y0, x1, y1), clip, band = self.glint
            u = smooth((t - .08) / .5)
            if 0 < u < 1:
                cw = x1 - x0
                gx = round(-band.width + u * (cw + band.width))
                m = Image.new("L", (cw, y1 - y0), 0)
                m.paste(band, (gx, 0))
                m = ImageChops.multiply(m, clip)
                img.paste(GLINT, (x0, y0, x1, y1), m)

        # signal tape running sideways by whole periods over the loop
        p = self.period
        shift = (t * TAPE_PERIODS * p) % p
        x = round((p - shift) * 4)
        strip = self.tape_master.crop((x, 0, x + w * 4, self.tape_master.height)).resize(
            (w, self.band), Image.BOX)
        img.paste(strip, (0, 0))
        img.paste(strip, (0, h - self.band))
        return img

    def frames(self, fps: int = FPS, seconds: float = SECONDS):
        n = max(2, round(fps * seconds))
        for i in range(n):
            yield self.frame(i / n)


def _glint_band(h: int, s: float, peak: float = 1.0) -> Image.Image:
    """L patch of the glint: a wide soft band and a thin sharper line beside it, slanted."""
    bw, line, slant = int(58 * s), max(3, int(6 * s)), int(h * .42)
    pad = int(24 * s)
    W = bw + slant + 2 * pad + line + int(26 * s)
    q = 2
    soft = Image.new("L", (W // q, h // q), 0)
    ImageDraw.Draw(soft).polygon([((pad + slant) / q, 0), ((pad + slant + bw) / q, 0),
                                  ((pad + bw) / q, h / q), (pad / q, h / q)], fill=int(72 * peak))
    soft = soft.filter(ImageFilter.GaussianBlur(13 * s / q)).resize((W, h), Image.BILINEAR)
    thin = Image.new("L", (W, h), 0)
    lx = pad + bw + int(26 * s)
    ImageDraw.Draw(thin).polygon([(lx + slant, 0), (lx + slant + line, 0), (lx + line, h), (lx, h)],
                                 fill=min(255, int(118 * peak)))
    thin = thin.filter(ImageFilter.GaussianBlur(max(1.0, 2 * s)))
    return ImageChops.lighter(soft, thin)


def _fan_poly(i, dx, dy, angle):
    """Corners of a fanned cover's picture in selection_banner, rotated as _fan_card does."""
    f = BANNER_FAN
    k = f["front"] if i == 0 else 1.0
    w, h = int(f["w"] * k), int(f["h"] * k)
    cx, cy = f["cx"] + dx, f["cy"] + dy
    a = math.radians(angle)
    ca, sa = math.cos(a), math.sin(a)
    # PIL's rotate(angle) turns counter-clockwise on screen (y grows downwards)
    return [(cx + x * ca + y * sa, cy - x * sa + y * ca)
            for x, y in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]


def _decode(jpeg: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(jpeg))
    return im.convert("RGB")


# ---------------------------------------------------------------- encoding

def ffmpeg_exe() -> str | None:
    if FFMPEG_EXE:
        return FFMPEG_EXE
    if not _EXE:
        exe = None
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as e:  # noqa: BLE001
            log.info("imageio-ffmpeg: %s", e)
        _EXE.append(exe or shutil.which("ffmpeg") or "")
    return _EXE[0] or None


def _encode(scene: Scene, fps: int = FPS, seconds: float = SECONDS) -> bytes | None:
    """Frames of the scene streamed into ffmpeg; the MP4 bytes, or None."""
    exe = ffmpeg_exe()
    if not exe:
        log.warning("animate: no ffmpeg")
        return None
    if not _SLOTS.acquire(timeout=SLOT_WAIT):
        log.warning("animate: encoder busy")
        return None
    fd, out = tempfile.mkstemp(prefix="gf-", suffix=".mp4")
    os.close(fd)
    n = max(2, round(fps * seconds))
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{scene.w}x{scene.h}", "-r", str(fps), "-i", "-",
           "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF),
           "-pix_fmt", "yuv420p", "-g", str(n), "-threads", str(THREADS),
           "-movflags", "+faststart", "-an", out]
    try:
        with tempfile.TemporaryFile() as err:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err,
                                    creationflags=flags)
            watchdog = threading.Timer(TIMEOUT, proc.kill)
            watchdog.start()
            try:
                for i in range(n):
                    proc.stdin.write(scene.frame(i / n).tobytes())
                proc.stdin.close()
                code = proc.wait()
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            finally:
                watchdog.cancel()
            if code != 0:
                err.seek(0)
                log.warning("ffmpeg exit %s: %s", code, err.read(500).decode("utf-8", "replace").strip())
                return None
        data = Path(out).read_bytes()
        return data if data[4:8] == b"ftyp" else None
    finally:
        _SLOTS.release()
        try:
            os.unlink(out)
        except OSError:
            pass


# ---------------------------------------------------------------- previews

def _preview() -> None:
    """assets/brand/cards/pick-1.mp4 and pick-1-preview.gif from the README's pick-1 card."""
    from .analyst import AXES
    from .texts import AXIS_LABEL

    out = render.ROOT / "assets" / "brand" / "cards"
    cov_path = render.ROOT / "data" / "covers" / "753640.jpg"
    cov = cov_path.read_bytes() if cov_path.is_file() else None
    feel = [(AXIS_LABEL[k].capitalize(), v, *AXES[k])
            for k, v in {"pace": 3, "story": 9, "exploration": 10, "difficulty": 4}.items()]
    view = {"rank": 1, "name": "Outer Wilds", "match": 0.91, "genres": ["Исследование", "Головоломка", "Космос"],
            "feel": feel, "recent": "96%", "reviews_total": 112834, "hours": "~22 ч",
            "price": "2 590 KZT (−40%)", "deck": "Steam Deck: проверено", "judge": True, "warning": ""}
    t0 = time.perf_counter()
    data = pick_card_mp4(view, cov)
    if not data:
        sys.exit("encoding failed")
    print(f"pick-1.mp4 {len(data) / 1024:.0f} KB in {time.perf_counter() - t0:.2f} s")
    (out / "pick-1.mp4").write_bytes(data)
    gif = out / "pick-1-preview.gif"
    subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(out / "pick-1.mp4"),
                    "-filter_complex", "scale=640:-2:flags=lanczos,split[a][b];[a]palettegen=max_colors=160[p];"
                    "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
                    "-loop", "0", str(gif)], check=True)
    print(f"{gif.name} {gif.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    _preview()
