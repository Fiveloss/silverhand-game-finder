"""The English interface (the default) and the language switch: python tests_i18n.py.

The other test files check the Russian texts (they pin the language to Russian); this one checks
that an English player gets English everywhere: labels, captions, the banner line, the cards'
words, the aspect buttons and the heuristic passport, and that the context really switches.
"""

import asyncio
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gamefinder import aspects, i18n, intent, render, texts, views  # noqa: E402
from gamefinder.analyst import AXES, AXES_EN, HEURISTIC_EN, axis_ends  # noqa: E402
from gamefinder.recommender import Pick  # noqa: E402
from gamefinder.service import _heuristic_en, _merge_text, _text_fields, _clean_text_fields  # noqa: E402
from gamefinder.sources.deck import deck_label  # noqa: E402

CYR = re.compile(r"[а-яё]", re.I)
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def passport(source="llm"):
    # A model passport is written in Russian; a heuristic one takes its summary from the store (English).
    llm = source == "llm"
    return {"summary": "Игра про космос." if llm else "A game about space.",
            "core_loop": "Летаешь и смотришь." if llm else "", "state_now": "", "best_for": "",
            "avoid_if": "", "hours_typical": "~12 ч у тех, кому понравилось", "real_genres": ["Exploration"],
            "moods": [], "feel": {k: 5 for k in AXES},
            "praise": [{"point": "сюжет", "share": "most"}],
            "complaints": [{"point": "баги", "share": "some", "kind": "quality", "axis": "none", "direction": "none"}],
            "quality": {}, "compared_to": [], "_source": source, "_reviews_used": 40, "_updated_at": 1}


@test
def test_default_is_english_and_the_switch_is_scoped():
    assert i18n.lang() == "en" and i18n.tr("a", "б") == "a"
    with i18n.using("ru"):
        assert i18n.tr("a", "б") == "б" and i18n.is_ru()
    assert i18n.lang() == "en"
    assert i18n.norm("de") == "en" and i18n.norm(None) == "en" and i18n.norm("ru") == "ru"


@test
def test_context_follows_threads_and_tasks():
    async def main():
        i18n.set_lang("ru")
        in_thread = await asyncio.to_thread(i18n.lang)
        in_task = await asyncio.create_task(asyncio.sleep(0, result=i18n.lang()))
        return in_thread, in_task
    assert asyncio.run(main()) == ("ru", "ru")
    assert i18n.lang() == "en"              # asyncio.run worked in a copy of the context


@test
def test_labels_in_english():
    assert set(AXES_EN) == set(AXES) and axis_ends("pace") == ("slow", "fast")
    assert texts.axis_label("social") == "co-op" and texts.share_word("most") == "almost everyone"
    assert deck_label(3, "native") == "Steam Deck: verified · Linux: native"
    for k in intent.REFINES:
        assert not CYR.search(intent.refine_label(k)), k
    r = intent.Request(seeds=["Hollow Knight"], max_hours=10, axes={"difficulty": 3}, dealbreakers=["mtx"])
    assert r.label() == "like Hollow Knight · easier · for a couple of evenings · no microtransactions"
    assert intent.Request().label() == "anything"


@test
def test_aspect_buttons_in_english_keep_their_groups():
    for group, label in aspects._EN.items():
        assert aspects.group_of(label) == group, (group, label)
    opts = [aspects.canonical(g) for g in ("explore", "atmos", "combat")]
    assert [o.label for o in opts] == ["Exploring the world", "Atmosphere", "Combat system"]
    got = aspects.from_text("the atmosphere and exploration, but the combat was annoying", opts)
    assert {(a.group, a.polarity) for a in got} == {("explore", 1), ("atmos", 1), ("combat", -1)}
    assert aspects.question("Hollow Knight") == "What exactly hooked you in Hollow Knight?"


@test
def test_captions_have_no_russian_for_an_english_player():
    p = _heuristic_en(passport("heuristic"))
    g = {"appid": 1, "name": "Outer Wilds", "genres": ["Adventure"], "positive": 900, "negative": 100,
         "deck": 3, "proton": "", "price_cents": 1499, "currency": "USD", "store_ok": 1, "ru_text": 1}
    pick = Pick(1, 0.8, {"tags": 0.7, "scout": 1.0}, p, False)
    pick.evidence = [{"text": "Отличная игра", "hours": 10}, {"text": "A wonderful game about space", "hours": 30}]
    cap = views.pick_caption(g, pick, "Hollow Knight")
    assert not CYR.search(cap), cap
    assert "A wonderful game" in cap and "Отличная" not in cap        # Russian quotes are left out
    stats = {"total": 1000, "all_share": 0.9, "recent_share": 0.95, "recent_span_days": 30,
             "engaged_n": 20, "engaged_share": 0.9, "quick_negative_share": 0.2}
    full = texts.passport_card(g, p, stats)
    assert not CYR.search(full) and "Русский" not in full, full
    view = views.pick_view(g, pick, stats, 1)
    assert view["hours"] == "~12 h for those" or view["hours"].startswith("~"), view["hours"]


@test
def test_heuristic_phrases_all_translated():
    assert all(not CYR.search(en) for en in HEURISTIC_EN.values())
    p = _heuristic_en(passport("heuristic"))
    assert p["praise"][0]["point"] == "the story" and p["complaints"][0]["point"] == "bugs"
    assert p["hours_typical"] == "~12 h for those who liked it"


@test
def test_translation_merge_keeps_numbers_and_shape():
    p = passport()
    src = _text_fields(p)
    answer = {**{k: "x" for k in src if isinstance(src[k], str)}, "real_genres": ["Exploration"], "moods": [],
              "praise": ["the story"], "complaints": ["bugs", "extra"]}       # complaints of a wrong length
    t = _clean_text_fields(answer, src)
    assert t["praise"] == ["the story"] and t["complaints"] == src["complaints"]
    q = _merge_text(p, t)
    assert q["praise"][0] == {"point": "the story", "share": "most"} and q["feel"] == p["feel"]
    assert q["summary"] == "x" and p["summary"] == "Игра про космос."       # the original is untouched
    assert _clean_text_fields("not a dict", src) is None


@test
def test_cards_draw_in_english():
    view = {"rank": 1, "name": "Outer Wilds", "match": 0.91, "genres": ["Exploration"],
            "feel": [(texts.axis_label(k).capitalize(), 5, *axis_ends(k)) for k in ("pace", "story")],
            "recent": "96%", "reviews_total": 112834, "hours": "~22 h", "price": "$14.99",
            "deck": "Steam Deck: verified", "judge": True, "warning": "", "confidence": "reviews",
            "reviews_used": 40}
    jpg = render.pick_card(view, None)
    assert jpg[:2] == b"\xff\xd8" and len(jpg) > 10_000


@test
def test_profile_texts_fit_telegram_limits():
    from gamefinder import profile
    for lang in ("en", "ru"):
        for open_access in (False, True):
            assert profile.utf16(profile.description(lang, open_access)) <= 512, lang
        assert profile.utf16(profile.ABOUT[lang]) <= 120, lang
    assert not CYR.search(profile.description("en", False) + profile.ABOUT["en"])


@test
def test_commands_in_both_languages():
    from gamefinder.bot import commands
    en, ru = commands("en", owner=True), commands("ru", owner=True)
    assert [c for c, _ in en] == [c for c, _ in ru] and "lang" in [c for c, _ in en]
    assert not CYR.search(" ".join(d for _, d in commands("en")).replace("Язык", ""))


def main():
    failed = 0
    for fn in TESTS:
        i18n.set_lang("en")
        try:
            fn()
            print("ok  ", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
