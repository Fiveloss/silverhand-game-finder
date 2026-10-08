"""Offline tests for gamefinder.render (image cards). Run: python tests_render.py

Covers are a JPEG generated with Pillow or None; views come from gamefinder.views (what the bot sends)
and from hand-made edge cases. No network: cover() is driven with a fake session and a temp cache."""

import asyncio
import io
import os
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw  # noqa: E402

from gamefinder import render as R  # noqa: E402
from gamefinder import views  # noqa: E402
from gamefinder.analyst import AXES, normalize  # noqa: E402
from gamefinder.recommender import Pick  # noqa: E402

PICK_SIZE, BANNER_SIZE = (1280, 720), (1280, 480)
GAME_SIZE = "game"          # 1280 wide, R.GAME_MIN_H..R.GAME_MAX_H high
MAX_SECONDS = 1.0
LONG_CARD_SECONDS = 0.4
TIMING = os.environ.get('GF_TIMING', '1') != '0'   # speed checks; off on the server (install.sh)
TIMINGS: list[tuple[str, float]] = []


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

LONG_NAME = "Хроники Последнего Королевства: Возвращение Забытого Героя Северных Земель"
LONG_WORD = "Сверхдлинноеназваниеигрыбезединогопробелачтобыпроверитьпереносслов"
LONG_DASH = "Повесть о Пустоши — Полное Издание Режиссёра с Дополнениями и Саундтреком"


def check_jpeg(data, size, label):
    assert isinstance(data, bytes) and data[:3] == b"\xff\xd8\xff", label
    with Image.open(io.BytesIO(data)) as im:
        assert im.format == "JPEG", label
        if size == GAME_SIZE:
            assert im.width == 1280 and R.GAME_MIN_H <= im.height <= R.GAME_MAX_H, (label, im.size)
        else:
            assert im.size == size, (label, im.size)
        im.load()
        assert im.mode == "RGB"
        # not a blank canvas
        lo, hi = im.convert("L").getextrema()
        assert hi - lo > 60, (label, lo, hi)
        return im.size


def timed(label, fn, *args):
    t = time.perf_counter()
    out = fn(*args)
    dt = time.perf_counter() - t
    TIMINGS.append((label, dt))
    assert not TIMING or dt < MAX_SECONDS, f"{label} took {dt:.2f} s"
    return out


def pick(name, *args):
    return check_jpeg(timed(f"pick_card {name}", R.pick_card, *args), PICK_SIZE, name)


def game(name, *args):
    return check_jpeg(timed(f"game_card {name}", R.game_card, *args), GAME_SIZE, name)


def banner(name, *args):
    return check_jpeg(timed(f"banner {name}", R.selection_banner, *args), BANNER_SIZE, name)


# --- realistic views, built the way the bot builds them (views.py)

HK_GAME = {"appid": 367520, "name": "Hollow Knight", "genres": ["Action", "Adventure", "Indie"],
           "positive": 412000, "negative": 12000, "price_cents": 145000, "currency": "KZT", "discount": 50,
           "deck": 3, "proton": "platinum", "tags": {"Metroidvania": 3000}}
HK_PASSPORT = normalize({
    "summary": "Мрачная рисованная метроидвания о павшем королевстве насекомых: огромный связанный мир, "
               "честные, но жёсткие бои и боссы, которые запоминаются надолго.",
    "real_genres": ["Metroidvania", "Souls-like", "Platformer"],
    "feel": {"pace": 6, "difficulty": 9, "story": 4, "freedom": 7, "complexity": 5, "grind": 3, "tension": 6,
             "combat": 8, "exploration": 10, "social": 0, "length": 7, "replay": 4},
    "praise": [{"point": "атмосфера и рисованная графика", "share": "most"},
               {"point": "огромный связанный мир, полный секретов", "share": "most"},
               {"point": "музыка", "share": "many"}, {"point": "боссы", "share": "many"},
               {"point": "цена за такой объём контента", "share": "some"}],
    "complaints": [{"point": "бэктрекинг и долгие пробежки к боссам", "share": "many", "kind": "taste",
                    "axis": "difficulty", "direction": "high"},
                   {"point": "карту надо покупать", "share": "some", "kind": "taste", "axis": "exploration",
                    "direction": "high"},
                   {"point": "редкие вылеты на Steam Deck", "share": "some", "kind": "quality"}],
    "state_now": "Игра завершена, патчи вышли давно, сообщество живое.",
    "hours_typical": "30-40 ч",
    "_source": "llm"})
HK_STATS = {"recent_share": 0.96, "total": 424000, "median_hours_positive": 41.4, "engaged_share": 0.94,
            "engaged_n": 120}


def hk_pick():
    return Pick(367520, 0.87, {}, HK_PASSPORT, True, because=1, feel_matches=["exploration", "difficulty"],
                warnings=["Поздние боссы очень сложные"], judge_reason="Как в Ori, тут решает исследование.")


def full_pick_view():
    v = views.pick_view(HK_GAME, hk_pick(), HK_STATS, 1, {"exploration": 9})
    assert v["name"] == "Hollow Knight" and len(v["feel"]) == 4 and v["judge"] and v["warning"]
    return v


def full_game_view():
    v = views.game_view(HK_GAME, HK_PASSPORT, HK_STATS)
    assert len(v["feel"]) == 12 and v["praise"] and v["complaints"] and v["match"] is None
    return v


NONE_VIEW = {k: None for k in ("rank", "name", "match", "genres", "feel", "recent", "reviews_total", "hours",
                               "price", "deck", "judge", "warning", "praise", "complaints", "store_genres",
                               "engaged", "state_now", "summary")}
EMPTY_VIEW = {"rank": 0, "name": "", "match": None, "genres": [], "feel": [], "recent": "", "reviews_total": 0,
              "hours": "", "price": "", "deck": "", "judge": False, "warning": "", "praise": [],
              "complaints": [], "store_genres": [], "engaged": "", "state_now": "", "summary": ""}


def long_view(name):
    v = full_game_view()
    v.update({
        "name": name, "rank": 12, "match": 0.999,
        "genres": ["Метроидвания с элементами соулс-лайка", "Тёмное фэнтези", "Платформер", "Ещё жанр", "И ещё"],
        "store_genres": ["Приключенческий экшен от третьего лица", "Инди"],
        "hours": "~1500 ч", "price": "1 234 567 KZT −95%", "deck": "Steam Deck: проверено · ProtonDB: platinum",
        "warning": "Очень длинное предупреждение о том, что игра может вылетать на старых видеокартах " * 2,
        "summary": "Очень длинное описание игры, которое не помещается ни в четыре, ни в пять строк. " * 8,
        "engaged": "94% наигравших 10+ ч довольны, а ещё многие из них прошли игру дважды и больше",
        "state_now": "Разработчики регулярно выпускают патчи и обещают крупное дополнение " * 3,
        "praise": [("Невероятно атмосферный, огромный и полностью связанный мир с кучей секретов " * 2,
                    "почти все")] * 5,
        "complaints": [("Бэктрекинг, бесконечные пробежки к боссам и дорогие карты у картографа " * 2,
                        "многие", True)] * 6,
        "feel": [("Очень длинная подпись оси", 7, "невероятно неторопливая и медитативная",
                  "безумно динамичная и стремительная")] * 12,
    })
    return v


# --- pick_card

def test_pick_card_full_view():
    pick("full", full_pick_view(), COVER)
    pick("full, no cover", full_pick_view(), None)


def test_pick_card_variants():
    v = full_pick_view()
    pick("no match", dict(v, match=None), COVER)                 # the ring shows fresh reviews then
    pick("no match no recent", dict(v, match=None, recent=None), None)
    pick("match as percent", dict(v, match=87), None)
    pick("match garbage", dict(v, match="n/a"), None)
    pick("recent float", dict(v, recent=0.43), None)
    pick("zero match", dict(v, match=0.0, recent="12%"), None)
    pick("tiny cover", v, make_cover(120, 60))
    pick("tall cover", v, make_cover(300, 900))
    pick("png cover", v, _png_cover())
    pick("broken cover bytes", v, b"\xff\xd8\xff not really a jpeg")
    pick("empty cover bytes", v, b"")


def test_pick_card_long_cyrillic_names():
    for name in (LONG_NAME, LONG_WORD, LONG_DASH, "Ы" * 60):
        assert len(name) >= 60
        pick(f"long {name[:12]}", long_view(name), COVER)
        pick(f"long {name[:12]} no cover", long_view(name), None)


def test_pick_card_empty_and_none():
    pick("empty", EMPTY_VIEW, None)
    pick("none", NONE_VIEW, None)
    pick("bare dict", {}, None)
    pick("feel garbage", dict(EMPTY_VIEW, name="X", feel=[("Темп", None, None, None), ("Сюжет",), "ab",
                                                          ("Сложность", "abc", "лёгкая", "хардкорная"),
                                                          ("Бои", 99, "", ""), ("Исследование", -5, "", "")]), COVER)


# --- game_card

def test_game_card_full_view():
    game("full", full_game_view(), COVER)
    game("full, no cover", full_game_view(), None)
    game("with match and rank", dict(full_game_view(), match=0.8, rank=2, judge=True), COVER)


def test_game_card_twelve_axes():
    v = full_game_view()
    assert [x[0] for x in v["feel"]] == [views.AXIS_LABEL[k].capitalize() for k in AXES]
    game("12 axes", v, COVER)
    for n in (1, 2, 5, 11, 13, 20):
        game(f"{n} axes", dict(v, feel=(v["feel"] * 2)[:n]), None)


def test_game_card_long_cyrillic_names():
    for name in (LONG_NAME, LONG_WORD, LONG_DASH):
        game(f"long {name[:12]}", long_view(name), COVER)
        game(f"long {name[:12]} no cover", long_view(name), None)


def test_game_card_empty_and_none():
    game("empty", EMPTY_VIEW, None)
    game("none", NONE_VIEW, None)
    game("bare dict", {}, COVER)
    game("only name", {"name": "Hollow Knight"}, None)
    game("praise strings", dict(EMPTY_VIEW, name="X", praise=["коротко", ""], complaints=[("x",), "y"]), None)
    game("praise no complaints", dict(full_game_view(), complaints=[]), None)
    game("complaints no praise", dict(full_game_view(), praise=[]), None)


# --- nothing cut off: the game card grows, the pick card fits its chips

SUB_STATE = ("Игра полностью завершена и стабильна для сюжетного прохождения, хотя давние технические "
             "шероховатости с физикой транспорта и левиафанов иногда всплывают")
SUB_SUMMARY = ("Выживание на дне чужого океана: вы единственный уцелевший после крушения корабля и строите базы, "
               "подлодки и снаряжение из того, что добываете на всё больших глубинах. Сюжет подаётся через "
               "находки и радиосигналы, а страх перед темнотой и левиафанами работает лучше любого хоррора. "
               "Боёв почти нет: главное оружие — осторожность, разведка и вовремя построенная база. "
               "Играть лучше вслепую, без гайдов.")


def subnautica_view():
    """What the owner saw cut on his phone: a long state, a ~400-character summary, long items."""
    v = full_game_view()
    v.update({
        "name": "Subnautica", "genres": ["Выживание", "Исследование", "Открытый мир"],
        "hours": "~38 ч", "price": "1 299 ₽ (−75%)", "deck": "Steam Deck: проверено · ProtonDB: platinum",
        "engaged": "95% наигравших 10+ ч довольны", "state_now": SUB_STATE, "summary": SUB_SUMMARY,
        "confidence": "reviews", "reviews_used": 87,
        "praise": [("Глубокая и приятная система строительства баз и управления подлодками", "почти все"),
                   ("Атмосфера океана: красота мелководья и ужас глубин", "почти все"),
                   ("Сюжет, который раскрывается через исследование, а не катсцены", "многие"),
                   ("Звук и музыка", "многие"),
                   ("Отлично оптимизирована после релиза, идёт даже на слабых ПК", "некоторые")],
        "complaints": [("Ужасная официальная реализация VR-режима (плохой интерфейс и укачивание)", "некоторые"),
                       ("Страх глубины и левиафаны — для кого-то слишком жутко", "многие", True),
                       ("Гринд ресурсов в середине игры", "некоторые", True),
                       ("Объекты прогружаются на глазах, иногда проваливаешься сквозь текстуры", "некоторые")],
    })
    assert 350 <= len(SUB_SUMMARY) <= 450
    return v


def game_height(view, cover=None, label="game"):
    data = timed(f"game_card {label}", R.game_card, view, cover)
    return check_jpeg(data, GAME_SIZE, label)[1]


def test_game_card_long_text_not_clipped():
    short_h = game_height(dict(EMPTY_VIEW, name="Hollow Knight"), None, "short")
    assert short_h == R.GAME_MIN_H, short_h
    hk_h = game_height(full_game_view(), COVER, "hk")
    assert R.LAST_CLIPPED == [], R.LAST_CLIPPED
    t = time.perf_counter()
    long_h = game_height(subnautica_view(), COVER, "subnautica")
    dt = time.perf_counter() - t
    assert R.LAST_CLIPPED == [], R.LAST_CLIPPED            # state, summary, praise, complaints all in full
    assert long_h > hk_h and long_h > 1280, (long_h, hk_h)
    assert not TIMING or dt < LONG_CARD_SECONDS, f"long game card took {dt:.2f} s"


def test_game_card_caps():
    # absurd text: the card stops at the maximum height, and says what it had to cut
    v = long_view(LONG_NAME)
    v["state_now"] = SUB_STATE * 4
    h = game_height(v, COVER, "absurd")
    assert h <= R.GAME_MAX_H and "state" in R.LAST_CLIPPED, (h, R.LAST_CLIPPED)
    # past the maximum the last items of the longer column go first
    keep = R.GAME_MAX_H
    try:
        R.GAME_MAX_H = 1700
        h = game_height(v, COVER, "absurd, low cap")
        assert h <= 1700 and {"praise", "complaints"} & set(R.LAST_CLIPPED), (h, R.LAST_CLIPPED)
    finally:
        R.GAME_MAX_H = keep
    # a summary longer than the cap ends at a sentence, not mid-word
    sents = [f"Предложение номер {i} про то, как устроена игра и что в ней хорошего и плохого." for i in range(20)]
    lines = R._summary_lines(" ".join(sents), R.sofia(36, 500), 1168, R.SUMMARY_LINES)
    assert len(lines) <= R.SUMMARY_LINES and lines[-1].endswith(".") and "summary" in R.LAST_CLIPPED
    # a praise item of four lines is drawn in full, a fifth line is cut
    pf, avail = R.sofia(30, 600), (1280 - 2 * 44 - 56) // 2 - 40
    n = next(n for n in range(1, 200) if len(R.wrap("слово " * n, pf, avail)) == 4)
    while len(R.wrap("слово " * (n + 1), pf, avail)) == 4:
        n += 1
    for words, cut in ((n, False), (n + 1, True)):
        v = dict(EMPTY_VIEW, name="X", praise=[("слово " * words, "многие")], complaints=[("коротко", "")])
        game_height(v, None, f"{words}-word item")
        assert ("praise" in R.LAST_CLIPPED) == cut, (words, R.LAST_CLIPPED)


def test_game_card_shade_cache_bounded():
    for n in range(10):
        R.game_card(dict(EMPTY_VIEW, name="X", summary="Слово " * (40 * n)), None)
    assert len(R._SHADE) <= R.SHADE_KEEP


def test_pick_card_long_russian_chips():
    base = full_pick_view()
    for label, extra in (
            ("rub discount", {"price": "1 299 ₽ (−75%)", "hours": "40–60 ч"}),
            ("unavailable", {"price": "Нет в продаже в регионе", "hours": "~120 ч",
                             "deck": "Steam Deck: не поддерживается"}),
            ("not in region", {"price": "Недоступна в регионе", "genres": ["Приключенческий экшен от третьего лица"]}),
            ("dollars", {"price": "$7.49", "hours": "~1500 ч"}),
            ("free", {"price": "Бесплатно", "genres": ["Симулятор выживания", "Открытый мир", "Песочница"]}),
            ("long warning", {"warning": "Для тех, кто боится глубины, местами по-настоящему страшно"})):
        pick(label, dict(base, **extra), COVER)
        cut = set(R.LAST_CLIPPED) & {"price", "hours", "genres", "warning", "name", "stats", "feel"}
        assert not cut, (label, R.LAST_CLIPPED)
    assert R._price_parts("1 299 ₽ (−75%)") == ("1 299 ₽", "−75%")
    assert R._price_parts("390 KZT -90%") == ("390 KZT", "−90%")
    assert R._price_parts("Бесплатно") == ("Бесплатно", "") and R._price_parts("$7.49") == ("$7.49", "")
    # Sofia Sans has no ₽: it is drawn with the mono font, and widths count it
    f = R.sofia(32, 600)
    assert not R._has_glyph(f, "₽") and R._has_glyph(f, "Ж") and R._has_glyph(f, "−")
    assert [t for t, _ in R._runs(f, "1 299 ₽")] == ["1 299 ", "₽"] and R.glen(f, "₽") > 5


def _region(data, box):
    with Image.open(io.BytesIO(data)) as im:
        return im.convert("RGB").crop(box)


def _diff(a, b):
    from PIL import ImageChops, ImageStat
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


def _green(im):
    from PIL import ImageStat
    r, g, _ = ImageStat.Stat(im).mean
    return g - r


def test_confidence_badges():
    for fn, v, box in ((R.pick_card, full_pick_view(), (300, 60, 640, 110)),
                       (R.game_card, full_game_view(), (230, 60, 584, 106))):
        plain = fn(dict(v, confidence=None), COVER)
        reviews = fn(dict(v, confidence="reviews", reviews_used=87), COVER)
        tags = fn(dict(v, confidence="tags"), COVER)
        garbage = fn(dict(v, confidence="???", reviews_used="n/a"), COVER)
        p, r, t, g = (_region(x, box) for x in (plain, reviews, tags, garbage))
        assert _diff(p, r) > 8 and _diff(p, t) > 8 and _diff(r, t) > 4, fn.__name__
        assert _diff(p, g) < 1, fn.__name__                     # unknown kind: no badge
        assert _green(r) > _green(t), fn.__name__                # reviews green-ish, tags amber/grey
        fn(dict(v, confidence="reviews", reviews_used=None), None)
    im = Image.new("RGB", (800, 100))
    assert R.confidence_badge(im, 700, 10, {"confidence": "reviews", "reviews_used": 21}) > 0
    assert R.confidence_badge(im, 700, 10, {"confidence": "tags"}) > 0
    assert R.confidence_badge(im, 700, 10, {}) == 0 and R.confidence_badge(im, 700, 10, {"confidence": "x"}) == 0


# --- selection_banner

def test_selection_banner_covers():
    title, sub = "Как Hollow Knight", "исследование · попроще · на пару вечеров"
    banner("0 covers", title, sub, [])
    banner("None covers", title, sub, None)
    banner("1 cover", title, sub, COVERS[:1])
    banner("2 covers", title, sub, COVERS[:2])
    banner("3 covers", title, sub, COVERS[:3])
    banner("4 covers (3 shown)", title, sub, COVERS)
    banner("3 covers, missing art", title, sub, [None, COVERS[0], b"broken"])
    banner("3 missing", title, sub, [None, None, None])


def test_selection_banner_texts():
    banner("long title", LONG_NAME, "очень длинный подзаголовок · " * 8, COVERS[:3])
    banner("long word", LONG_WORD, LONG_WORD, [])
    banner("empty texts", "", "", COVERS[:1])
    banner("no subtitle", "Подборка", "", [])


# --- edge cases the bot does not produce today, kept apart so a failure stays isolated

def test_long_single_word_name_speed():
    # a 90-character name without spaces: wrap() breaks it with a hyphen one character at a time
    for name in ("Ы" * 90, "W" * 90):
        for fn, size in ((R.pick_card, PICK_SIZE), (R.game_card, GAME_SIZE)):
            check_jpeg(timed(f"{fn.__name__} {name[:3]}x90", fn, dict(EMPTY_VIEW, name=name), None), size, name)
        check_jpeg(timed(f"banner {name[:3]}x90", R.selection_banner, name, "", []), BANNER_SIZE, name)


def test_none_inside_inputs():
    raised = []
    for label, fn, args in (
            ("selection_banner(None, None, [None])", R.selection_banner, (None, None, [None])),
            ("selection_banner('Подборка', None, [])", R.selection_banner, ("Подборка", None, [])),
            ("game_card praise=[None]", R.game_card, (dict(EMPTY_VIEW, name="X", praise=[None]), None)),
            ("game_card complaints=[None]", R.game_card, (dict(EMPTY_VIEW, name="X", complaints=[None]), None))):
        try:
            data = fn(*args)
            assert data[:3] == b"\xff\xd8\xff"
        except Exception as e:  # noqa: BLE001
            raised.append(f"{label}: {type(e).__name__}: {e}")
    assert not raised, raised


# --- the rest of the module's helpers

def test_helpers():
    assert R.initials("Hollow Knight") == "HK" and R.initials("Ведьмак") == "ВЕ" and R.initials("") == "?"
    assert R.initials("--- ???") == "?"
    assert R.freshness("96%") == (96, R.GOOD) and R.freshness(0.43)[0] == 43 and R.freshness(None)[0] is None
    assert R.freshness("—") == (None, R.FAINT) and R.freshness(75)[1] == R.ACC
    assert R.share_level("почти все") == 3 and R.share_level("многие") == 2 and R.share_level("35%") == 2
    assert R.share_level("") == 0 and R.share_level(None) == 0
    assert R.plural(1, "a", "b", "c") == "a" and R.plural(3, "a", "b", "c") == "b" and R.plural(11, "a", "b", "c") == "c"
    assert R.thousands(424000) == "424 000"
    f = R.sofia(40, 900)
    assert R.clip(LONG_NAME, f, 200).endswith("…") and f.getlength(R.clip(LONG_NAME, f, 200)) <= 200
    assert all(f.getlength(ln) <= 300 for ln in R.wrap(LONG_WORD, f, 300))
    font, lines = R.fit(LONG_NAME.upper(), lambda z: R.sofia(z, 900), 600, 2, range(112, 51, -4))
    assert len(lines) <= 2 and all(font.getlength(ln) <= 600 for ln in lines)


def test_cover_cache_and_fake_session():
    class Resp:
        def __init__(self, status, body):
            self.status, self.body = status, body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read(self):
            return self.body

    class FakeSession:          # an aiohttp.ClientSession stand-in; no `session` attribute on purpose
        def __init__(self, answers):
            self.answers, self.urls = list(answers), []

        def get(self, url, timeout=None):
            self.urls.append(url)
            a = self.answers.pop(0) if self.answers else (404, b"")
            if isinstance(a, Exception):
                raise a
            return Resp(*a)

    with tempfile.TemporaryDirectory() as tmp:
        s = FakeSession([(404, b""), (200, COVER)])
        data = asyncio.run(R.cover(s, 367520, cache_dir=tmp))
        assert data == COVER and len(s.urls) == 2 and "367520" in s.urls[0]
        assert os.path.isfile(os.path.join(tmp, "367520.jpg"))
        s2 = FakeSession([])
        assert asyncio.run(R.cover(s2, 367520, cache_dir=tmp)) == COVER and s2.urls == []     # from the disk
        # no art anywhere (errors, non-images, tiny files): None, a .miss file, then no new requests
        s3 = FakeSession([OSError("reset"), (200, b"<html>not an image</html>" * 100), (200, b"x")])
        assert asyncio.run(R.cover(s3, 42, cache_dir=tmp)) is None
        assert os.path.isfile(os.path.join(tmp, "42.miss"))
        s4 = FakeSession([(200, COVER)])
        assert asyncio.run(R.cover(s4, 42, cache_dir=tmp)) is None and s4.urls == []
        # garbage input never raises
        assert asyncio.run(R.cover(FakeSession([]), "not-an-id", cache_dir=tmp)) is None


def _png_cover():
    buf = io.BytesIO()
    Image.new("RGBA", (460, 215), (200, 40, 40, 128)).save(buf, "PNG")
    return buf.getvalue()


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
        slowest = max(TIMINGS, key=lambda x: x[1])
        print(f"\n{len(TIMINGS)} renders, slowest {slowest[1] * 1000:.0f} ms ({slowest[0]}), "
              f"mean {sum(t for _, t in TIMINGS) / len(TIMINGS) * 1000:.0f} ms")
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
