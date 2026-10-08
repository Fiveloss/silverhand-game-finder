"""Offline tests for gamefinder.animate (animated cards). Run: python tests_animate.py

Covers are JPEGs generated with Pillow, so no network and no data/covers. The MP4s are decoded
with imageio-ffmpeg's own ffmpeg to count frames; the box structure is checked by hand."""

import io
import os
import struct
import sys
import tempfile
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import imageio_ffmpeg  # noqa: E402
from PIL import Image, ImageChops, ImageDraw  # noqa: E402

from gamefinder import animate as A  # noqa: E402
from gamefinder import render as R  # noqa: E402

PICK_OUT, BANNER_OUT = (960, 540), (960, 360)
FRAMES = round(A.FPS * A.SECONDS)
MAX_SECONDS = 2.5
MAX_PICK_BYTES = 400 * 1024
TIMING = os.environ.get('GF_TIMING', '1') != '0'   # speed checks; off on the server (install.sh)
TIMINGS: list[tuple[str, float, int]] = []


def make_cover(w=616, h=353, seed=0) -> bytes:
    im = Image.new("RGB", (w, h), (30 + seed * 40 % 200, 60, 120))
    d = ImageDraw.Draw(im)
    for i in range(0, w, 24):
        d.line((i, 0, w - i, h), fill=(200, 120 + seed * 30 % 100, 40), width=3)
    d.ellipse((w * .3, h * .2, w * .7, h * .8), fill=(240, 220, 180))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


COVER = make_cover()
COVERS = [make_cover(seed=i) for i in range(4)]
FEEL = [("Темп", 3, "неторопливая", "динамичная"), ("Сюжет", 9, "сюжет не важен", "сюжет в центре"),
        ("Исследование", 10, "исследовать нечего", "исследование — основа"),
        ("Сложность", 4, "лёгкая", "хардкорная")]
VIEW = {"rank": 1, "name": "Outer Wilds", "match": 0.91, "genres": ["Исследование", "Головоломка", "Космос"],
        "feel": FEEL, "recent": "96%", "reviews_total": 112834, "hours": "~22 ч", "price": "2 590 KZT (−40%)",
        "deck": "Steam Deck: проверено", "judge": True, "warning": ""}


# ---------------------------------------------------------------- helpers

def boxes(data: bytes) -> list[tuple[bytes, int, int]]:
    """Top-level MP4 boxes: (type, offset, size)."""
    out, i = [], 0
    while i + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[i:i + 8])
        if size == 1:
            size = struct.unpack(">Q", data[i + 8:i + 16])[0]
        if size < 8:
            break
        out.append((kind, i, size))
        i += size
    return out


def decode(data: bytes):
    """(meta, frames as PIL images) decoded by imageio-ffmpeg."""
    fd, path = tempfile.mkstemp(suffix=".mp4")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        gen = imageio_ffmpeg.read_frames(path)
        meta = next(gen)
        w, h = meta["size"]
        frames = [Image.frombytes("RGB", (w, h), bytes(b)) for b in gen]
        return meta, frames
    finally:
        os.unlink(path)


def check_mp4(data, size, label, max_bytes=None):
    assert isinstance(data, bytes) and len(data) > 1000, label
    kinds = [k for k, _, _ in boxes(data)]
    assert data[4:8] == b"ftyp" and kinds[0] == b"ftyp", (label, kinds)
    # +faststart: the index comes before the media data, so Telegram can start playing at once
    assert b"moov" in kinds and b"mdat" in kinds and kinds.index(b"moov") < kinds.index(b"mdat"), (label, kinds)
    # H.264, and no sound track
    assert b"avc1" in data and b"avcC" in data, label
    assert b"soun" not in data and b"mp4a" not in data, label
    if max_bytes:
        assert len(data) <= max_bytes, (label, len(data))
    meta, frames = decode(data)
    assert tuple(meta["size"]) == size, (label, meta["size"])
    assert meta["pix_fmt"].startswith("yuv420p"), (label, meta["pix_fmt"])
    assert abs(meta["fps"] - A.FPS) < .01, (label, meta["fps"])
    assert abs(meta["duration"] - A.SECONDS) < .1, (label, meta["duration"])
    assert len(frames) == FRAMES, (label, len(frames))
    lo, hi = frames[0].convert("L").getextrema()
    assert hi - lo > 60, (label, lo, hi)
    return frames


def timed(label, fn, *args):
    t = time.perf_counter()
    out = fn(*args)
    dt = time.perf_counter() - t
    TIMINGS.append((label, dt, len(out or b"")))
    assert not TIMING or dt < MAX_SECONDS, f"{label} took {dt:.2f} s"
    return out


def diff(a, b, box=None):
    """Mean absolute difference of two images (in box), 0..255."""
    if box:
        a, b = a.crop(box), b.crop(box)
    d = ImageChops.difference(a, b).convert("L")
    hist = d.histogram()
    return sum(i * n for i, n in enumerate(hist)) / max(1, sum(hist))


def pick_scene(view=VIEW, cover=COVER):
    base = A._decode(R.pick_card(view, cover))
    x, y, w, h = A.PICK_COVER
    poly = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return base, A.Scene(base, A.SCALE, A.PICK_TAPE, glint_poly=poly, art=[poly], ring=A.PICK_RING)


class Patched:
    """Sets module attributes for a with-block and puts them back."""

    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(A, k)
            setattr(A, k, v)

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(A, k, v)


# ---------------------------------------------------------------- tests

def test_pick_mp4():
    A.pick_card_mp4(VIEW, COVER)             # warm-up: fonts, imageio-ffmpeg lookup
    data = timed("pick_card_mp4 cover", A.pick_card_mp4, VIEW, COVER)
    frames = check_mp4(data, PICK_OUT, "pick", MAX_PICK_BYTES)
    # it moves: the tape and the glint change the picture between frames
    assert diff(frames[0], frames[FRAMES // 3]) > .5
    # frame 0 is the static card (the thumbnail): close to the JPEG scaled down
    still = A._decode(R.pick_card(VIEW, COVER)).resize(PICK_OUT, Image.LANCZOS)
    band = round(A.PICK_TAPE * A.SCALE) + 1
    inner = (0, band, PICK_OUT[0], PICK_OUT[1] - band)
    loss = diff(frames[0], still, inner)          # only what H.264 at CRF 25 and yuv420p lose
    assert loss < 5 and loss < diff(frames[FRAMES // 2], still, inner), loss


def test_pick_variants():
    views = [
        ({**VIEW, "match": None, "judge": False, "rank": 0}, None),
        ({"name": "Игра без обложки"}, None),
        ({**VIEW, "name": "Хроники Последнего Королевства: Возвращение Забытого Героя",
          "warning": "Много чтения: почти нет боёв"}, make_cover(460, 215, 3)),
    ]
    for i, (view, cov) in enumerate(views):
        check_mp4(timed(f"pick_card_mp4 variant {i}", A.pick_card_mp4, view, cov), PICK_OUT, f"variant {i}",
                  MAX_PICK_BYTES)


def test_banner_mp4():
    for n in (0, 1, 2, 3):
        data = timed(f"banner_mp4 {n} covers", A.banner_mp4, "ПОДБОРКА",
                     "как Hollow Knight · исследование, атмосфера · проще", COVERS[:n])
        check_mp4(data, BANNER_OUT, f"banner {n}", MAX_PICK_BYTES)
    # covers that are None draw placeholders, as in render
    check_mp4(A.banner_mp4("ПОДБОРКА", "", [None, COVER]), BANNER_OUT, "banner with None")


def test_seamless_loop():
    _, sc = pick_scene()
    # t=1 is t=0 exactly: the loop has no seam
    assert ImageChops.difference(sc.frame(0.0), sc.frame(1.0)).getbbox() is None
    # the last frame is close to the first one
    assert diff(sc.frame(0.0), sc.frame((FRAMES - 1) / FRAMES)) < 1.5
    # frame 0 is the scaled card apart from the tape rows
    inner = (0, sc.band, sc.w, sc.h - sc.band)
    assert ImageChops.difference(sc.frame(0.0), sc.base).crop(inner).getbbox() is None


def test_overlays_where_expected():
    _, sc = pick_scene()
    s = sc.s
    f0 = sc.frame(0)
    x, y, w, h = A.PICK_COVER
    cover = tuple(int(v * s) for v in (x, y, x + w, y + h))
    # the glint crosses the cover in the first half and stays inside it
    mid = sc.frame(.3)
    assert diff(f0, mid, cover) > 1
    outside = ImageChops.difference(f0, mid).crop((0, sc.band, sc.w, sc.h - sc.band))
    # (the breathing light and the scan band touch the rest only faintly)
    assert diff(f0, mid, (cover[2] + 30, sc.band, sc.w, sc.h - sc.band)) < diff(f0, mid, cover)
    assert outside.getbbox() is not None
    # the ring breathes: brighter at the middle of the loop
    cx, cy, r = (v * s for v in A.PICK_RING)
    ring = (int(cx - r - 20), int(cy - r - 20), int(cx + r + 20), int(cy + r + 20))
    bright = lambda im: sum(i * n for i, n in enumerate(im.crop(ring).convert("L").histogram()))  # noqa: E731
    assert bright(sc.frame(.5)) > bright(f0) * 1.03
    # the tapes move: the top strip differs, and top and bottom are the same strip
    f1 = sc.frame(.25)
    assert diff(f0, f1, (0, 0, sc.w, sc.band)) > 5
    top = f1.crop((0, 0, sc.w, sc.band))
    bottom = f1.crop((0, sc.h - sc.band, sc.w, sc.h))
    assert ImageChops.difference(top, bottom).getbbox() is None


def test_layout_matches_render():
    """The regions animate.py assumes are where render.py draws them."""
    base, _ = pick_scene()
    px = base.load()

    def amber(p):
        r, g, b = p
        return r > 200 and 110 < g < 200 and b < 90

    x, y, w, h = A.PICK_COVER
    # the amber frame just outside the cover, on all four sides
    for p in ((x - 2, y + h // 2), (x + w + 1, y + h // 2), (x + w // 2, y + h + 1), (x + w // 2 + 40, y - 2)):
        assert amber(px[p]) or px[p][0] > 220, (p, px[p])
    # tapes: amber stripes on the first and last rows, nothing amber just below the top one
    for row in (1, A.PICK_TAPE - 2, base.height - 2):
        assert sum(amber(px[i, row]) for i in range(0, base.width, 3)) > 100, row
    # the ring: lit segments at 12 o'clock (match 91%), its centre is dark with white digits around
    cx, cy, r = A.PICK_RING
    thick = max(8, r // 6)
    assert amber(px[cx + 4, cy - r + thick // 2]) or px[cx + 4, cy - r + thick // 2][0] > 200
    # banner: the front cover's frame is amber where _fan_poly says it is
    banner = A._decode(R.selection_banner("ПОДБОРКА", "тест", COVERS[:1]))
    poly = A._fan_poly(*A.BANNER_SLOTS[1][0])
    top_mid = ((poly[0][0] + poly[1][0]) / 2, (poly[0][1] + poly[1][1]) / 2)
    p = banner.getpixel((int(top_mid[0]), int(top_mid[1]) - 2))
    assert p[0] > 200 and p[2] < 200, p
    assert banner.getpixel((5, 3))[0] > 200 or banner.getpixel((15, 3))[0] > 200


def test_missing_ffmpeg_returns_none():
    missing = os.path.join(tempfile.gettempdir(), "no-such-dir", "ffmpeg-missing.exe")
    with Patched(FFMPEG_EXE=missing):
        t = time.perf_counter()
        assert A.pick_card_mp4(VIEW, COVER) is None
        assert A.banner_mp4("ПОДБОРКА", "", COVERS[:2]) is None
        assert time.perf_counter() - t < 2
    # and it works again afterwards (the slot was released)
    assert A.pick_card_mp4(VIEW, COVER)[4:8] == b"ftyp"


def test_ffmpeg_error_returns_none():
    with Patched(PRESET="no-such-preset"):
        assert A.pick_card_mp4(VIEW, COVER) is None
    with Patched(TIMEOUT=0.01):
        assert A.pick_card_mp4(VIEW, COVER) is None


def test_bad_input_returns_none():
    assert A.pick_card_mp4(None, None) is None
    assert A.banner_mp4(None, None, 42) is None


def test_busy_encoder_returns_none():
    assert A._SLOTS.acquire(timeout=5)
    try:
        with Patched(SLOT_WAIT=0.1):
            t = time.perf_counter()
            assert A.pick_card_mp4(VIEW, COVER) is None
            assert time.perf_counter() - t < 1.5
    finally:
        A._SLOTS.release()


def test_parallel_calls_serialised():
    out = []
    threads = [threading.Thread(target=lambda: out.append(A.pick_card_mp4(VIEW, COVER))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(out) == 3 and all(d and d[4:8] == b"ftyp" for d in out)


def test_no_temp_files_left():
    before = set(os.listdir(tempfile.gettempdir()))
    A.pick_card_mp4(VIEW, COVER)
    with Patched(PRESET="no-such-preset"):
        A.pick_card_mp4(VIEW, COVER)
    left = [f for f in set(os.listdir(tempfile.gettempdir())) - before if f.startswith("gf-")]
    assert not left, left


def main():
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    if TIMINGS:
        for label, dt, size in TIMINGS:
            print(f"  {label:34} {dt * 1000:5.0f} ms {size / 1024:5.0f} KB")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    import logging
    logging.basicConfig(level=logging.CRITICAL)
    main()
