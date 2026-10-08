"""Offline tests for gamefinder.aspects («Чем зацепила?»). Run: python tests_aspects.py"""

import json
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gamefinder import aspects as A  # noqa: E402
from gamefinder.analyst import AXES, normalize  # noqa: E402
from gamefinder.intent import Request  # noqa: E402

KEY_OK = re.compile(r"^[a-z0-9_]{1,12}$")

# --- realistic reference games: Steam-like tag votes and passports (analyst.normalize shape)

HK = {"appid": 367520, "name": "Hollow Knight",
      "tags": {"Metroidvania": 3000, "Indie": 2500, "Souls-like": 2400, "Difficult": 2300, "Hand-drawn": 2000,
               "Atmospheric": 1900, "Exploration": 1500, "Great Soundtrack": 1400, "2D": 1300, "Platformer": 1200,
               "Action": 1000, "Dark Fantasy": 900, "Singleplayer": 800}}
HK_P = normalize({
    "feel": {"exploration": 10, "difficulty": 9, "combat": 8, "tension": 6, "story": 4, "pace": 6, "length": 7},
    "praise": [{"point": "атмосфера и рисованная графика", "share": "most"},
               {"point": "исследование огромного связанного мира", "share": "most"},
               {"point": "музыка Кристофера Ларкина", "share": "many"},
               {"point": "боссы", "share": "many"}],
    "complaints": [{"point": "слишком сложно", "share": "some", "kind": "taste",
                    "axis": "difficulty", "direction": "high"}],
    "real_genres": ["Metroidvania"], "_source": "llm"})

DE = {"appid": 632470, "name": "Disco Elysium",
      "tags": {"RPG": 3000, "Story Rich": 2800, "Detective": 2500, "Choices Matter": 2300, "Great Soundtrack": 1800,
               "Philosophical": 1600, "Political": 1500, "Atmospheric": 1400, "Dark Humor": 1300,
               "Isometric": 1000, "Text-Based": 900}}
DE_P = normalize({
    "feel": {"story": 10, "combat": 0, "complexity": 7, "freedom": 7, "pace": 2},
    "praise": [{"point": "гениальные тексты и диалоги", "share": "most"},
               {"point": "юмор", "share": "many"},
               {"point": "политика и философия", "share": "some"}],
    "complaints": [{"point": "очень много читать", "share": "some", "kind": "taste",
                    "axis": "story", "direction": "high"}],
    "real_genres": ["CRPG", "Detective"], "_source": "llm"})

# tags as a plain list, most voted first (the other shape _game_tags accepts)
SV = {"appid": 413150, "name": "Stardew Valley",
      "tags": ["Farming Sim", "Life Sim", "Pixel Graphics", "Relaxing", "Cozy", "Multiplayer", "Co-op", "RPG",
               "Crafting", "Cute", "Indie"]}
SV_P = normalize({
    "feel": {"tension": 1, "length": 9, "social": 5, "pace": 3, "grind": 6},
    "praise": [{"point": "уютная атмосфера", "share": "most"},
               {"point": "кооператив с друзьями", "share": "many"}],
    "_source": "heuristic"})

AMN = {"appid": 57300, "name": "Amnesia: The Dark Descent",
       "tags": {"Horror": 3000, "Psychological Horror": 2500, "Survival Horror": 2000, "Atmospheric": 1900,
                "Lovecraftian": 1500, "Dark": 1200, "First-Person": 900, "Puzzle": 800}}
AMN_P = normalize({
    "feel": {"tension": 10, "combat": 0, "story": 6, "exploration": 5},
    "praise": [{"point": "страшно до дрожи", "share": "most"}, {"point": "звук", "share": "many"}]})

ALL = [("hk", HK, HK_P), ("de", DE, DE_P), ("sv", SV, SV_P), ("amn", AMN, AMN_P)]

# LLM reference-card aspects in the external.py shape
HK_CARD = [
    {"label_ru": "Шорткаты и связанный мир", "axes": {"exploration": 9}, "tags": ["Metroidvania", "Exploration"],
     "words_ru": "мир, который открывается срезками и тайными проходами"},
    {"label_ru": "Атмосфера", "axes": {}, "tags": ["Atmospheric"], "words_ru": "мрачная тихая атмосфера"},
    {"label_ru": "атмосфера", "axes": {}, "tags": ["Atmospheric"], "words_ru": "дубль"},          # dup label
    {"label_ru": "Геймплей", "axes": {}, "tags": []},                                                 # junk
    {"label_ru": "Тоска павшего королевства Халлоунест", "axes": {"story": 6}, "tags": ["Lore-Rich"],
     "words_ru": "история павшего королевства, которую собираешь по кусочкам"},                     # > 28 chars
    {"label_ru": "Тишина и одиночество", "axes": {"tension": 4}, "tags": [], "words_ru": "тишина"},  # free-form
]


def check_option(o):
    assert isinstance(o, A.Aspect)
    assert KEY_OK.match(o.key), o.key
    assert A.valid_key(o.key), o.key
    assert len(f"asp:{o.key}".encode()) <= 64
    assert o.label and len(o.label) <= A.MAX_LABEL, (o.label, len(o.label))
    assert o.label == o.label.strip() and "  " not in o.label
    assert all(k in AXES and 0 <= v <= 10 for k, v in o.axes.items()), o.axes
    assert len(o.tags) <= 5 and len(set(o.tags)) == len(o.tags)
    assert o.polarity in (-1, 0, 1)


def check_options(opts, limit=6):
    assert len(opts) <= limit
    for o in opts:
        check_option(o)
    keys = [o.key for o in opts]
    assert len(set(keys)) == len(keys), keys
    labels = [o.label.lower() for o in opts]
    assert len(set(labels)) == len(labels), labels
    groups = [o.group for o in opts if o.group]
    assert len(set(groups)) == len(groups), groups


def by_key(opts):
    return {o.key: o for o in opts}


# --- options

def test_options_hollow_knight():
    opts = A.options(HK, HK_P)
    check_options(opts)
    k = by_key(opts)
    assert len(opts) == 6
    for key in ("explore", "challenge", "atmos", "combat", "music", "visual"):
        assert key in k, (key, list(k))
    assert opts[0].key == "explore"                      # the most characteristic first
    assert k["visual"].label == "Рисованная графика" and "Hand-drawn" in k["visual"].tags
    assert {"Exploration", "Metroidvania"} <= set(k["explore"].tags)
    assert k["challenge"].axes == {"difficulty": 8} and "Difficult" in k["challenge"].tags


def test_options_disco_elysium():
    opts = A.options(DE, DE_P)
    check_options(opts)
    k = by_key(opts)
    assert "writing" in k and "story" in k and "humor" in k, list(k)
    assert k["humor"].label == "Чёрный юмор" and "Dark Humor" in k["humor"].tags
    assert "combat" not in k                             # combat 0: nothing to love there
    assert all(o.key != "challenge" for o in opts)


def test_options_stardew_tag_list():
    opts = A.options(SV, SV_P)
    check_options(opts)
    k = by_key(opts)
    for key in ("cozy", "farm", "friends", "visual"):
        assert key in k, (key, list(k))
    assert opts[0].key == "cozy"
    assert k["visual"].label == "Пиксель-арт"
    assert k["cozy"].axes == {"tension": 2} and "Relaxing" in k["cozy"].tags
    assert "Farming Sim" in k["farm"].tags and "Co-op" in k["friends"].tags


def test_options_horror():
    opts = A.options(AMN, AMN_P)
    check_options(opts)
    k = by_key(opts)
    assert opts[0].key == "tension"
    assert k["tension"].label == "Психологический ужас"
    assert {"Horror", "Psychological Horror"} <= set(k["tension"].tags)
    assert k["atmos"].label == "Мрачная атмосфера"       # Dark among the tags
    assert k["setting"].label == "Лавкрафтовский ужас"
    assert "cozy" not in k and "combat" not in k


def test_options_card_priority_dedupe_limit():
    opts = A.options(HK, HK_P, HK_CARD)
    check_options(opts)
    labels = [o.label for o in opts]
    # card aspects first, in their order; the duplicate «атмосфера» and the junk «Геймплей» dropped
    assert labels[0] == "Шорткаты и связанный мир", labels
    assert labels[1] == "Атмосфера", labels
    assert sum(lab.lower() == "атмосфера" for lab in labels) == 1
    assert "Геймплей" not in labels
    assert labels[2].startswith("Тоска павшего") and labels[2].endswith("…") and len(labels[2]) <= 28, labels[2]
    assert labels[3] == "Тишина и одиночество", labels
    # card values kept as given
    assert opts[0].axes == {"exploration": 9} and opts[0].tags == ["Metroidvania", "Exploration"]
    assert opts[0].words.startswith("мир, который")
    # the canonical fill does not repeat a card's group
    assert opts[0].group == "explore" and all(o.key != "explore" for o in opts[1:])
    assert sum(o.group == "atmos" for o in opts) == 1
    # a free-form card aspect gets a positional key that is still valid callback data
    free = opts[3]
    assert free.group == "" and free.key == "c5", (free.key, free.group)
    # limit: only card aspects when there are enough of them
    three = A.options(HK, HK_P, HK_CARD, limit=3)
    assert [o.label for o in three] == labels[:3]
    two = A.options(HK, HK_P, limit=2)
    assert len(two) == 2 and [o.key for o in two] == [o.key for o in A.options(HK, HK_P)[:2]]
    assert len(A.options(HK, HK_P, limit=12)) <= 12


def test_options_card_two_aspects_same_group_kept():
    # aspects.py:304 says «two card aspects in one group are both kept: the model meant them apart»
    card = [{"label_ru": "Бои с боссами", "axes": {"combat": 9}, "tags": ["Souls-like"], "words_ru": "боссы"},
            {"label_ru": "Сражения на арене", "axes": {"combat": 8}, "tags": ["Combat"], "words_ru": "арены"}]
    opts = A.options(HK, HK_P, card)
    labels = [o.label for o in opts]
    assert "Бои с боссами" in labels and "Сражения на арене" in labels, labels
    check_options(opts)


def test_options_card_garbage_and_empty():
    card = [None, "строка", {"label_ru": ""}, {"label_ru": "Всё"}, {"label": "Lore", "axes": {"story": "7.6", "x": 3,
                                                                                         "exploration": 99}},
            {"label_ru": "Музыка", "axes": None, "tags": ["Great Soundtrack", 5, "Great Soundtrack"]}]
    opts = A.options(HK, HK_P, card)
    check_options(opts)
    assert opts[0].label == "Lore" and opts[0].axes == {"story": 8, "exploration": 10}, opts[0]
    assert opts[1].label == "Музыка" and opts[1].tags == ["Great Soundtrack"]
    assert A.options({}, None) == [] and A.options({"tags": {}}, normalize({})) == []
    assert A.options({"tags": {"Indie": 100, "2D": 50}}, None) == []      # nothing specific known


def test_options_labels_keys_everywhere():
    for _, g, p in ALL:
        for limit in (1, 3, 6, 10):
            check_options(A.options(g, p, limit=limit), limit)
        check_options(A.options(g, p, HK_CARD))
    for group in A._C:                     # every canonical aspect is a valid button
        check_option(A.canonical(group))
    long_card = [{"label_ru": "Очень-очень-длинноеслововкоторомнетпробеловвообще и ещё", "axes": {}}]
    check_options(A.options(HK, HK_P, long_card))


# --- apply

def hk_req(**kw):
    return Request(text="как Hollow Knight", seeds=["Hollow Knight"], **kw)


def test_apply_picked_axes_become_targets():
    opts = A.options(HK, HK_P)
    req = hk_req(tags_want=["Indie"])
    before = req.to_json()
    r = A.apply(req, HK, HK_P["feel"], A.pick(opts, ["explore", "atmos"]))
    assert req.to_json() == before                       # the original is untouched
    assert r is not req
    assert r.axes == {"exploration": 10}, r.axes         # the seed's own 10, on the same side as 9
    assert r.focus_axes == ["exploration"]
    assert r.tags_want[0] == "Indie" and {"Exploration", "Metroidvania", "Atmospheric"} <= set(r.tags_want)
    assert "исследовать огромный мир" in r.words and "атмосфера" in r.words
    assert r.focus_labels == ["Исследование мира", "Атмосфера"]
    # the seed's tags tied to what they did not pick are muted, the picked ones never
    assert {"Souls-like", "Difficult", "Hand-drawn", "Great Soundtrack"} <= set(r.mute_tags), r.mute_tags
    assert not set(r.mute_tags) & set(r.tags_want)
    assert "Indie" not in r.mute_tags and "2D" not in r.mute_tags      # not tied to any aspect
    # the result is still a Request the bot can store
    back = Request.from_json(r.to_json())
    assert back.focus_axes == ["exploration"] and back.mute_tags == r.mute_tags


def test_apply_multiple_axes_and_typed_axes_win():
    opts = A.options(HK, HK_P)
    r = A.apply(hk_req(axes={"difficulty": 3}), HK, HK_P["feel"], A.pick(opts, ["challenge", "combat", "explore"]))
    assert r.axes["difficulty"] == 3                     # typed «попроще» beats the aspect
    assert r.axes["combat"] == 8 and r.axes["exploration"] == 10, r.axes
    assert r.focus_axes == ["combat", "difficulty", "exploration"]
    assert set(r.axes) == {"difficulty", "combat", "exploration"}
    # no seed feel: the aspect's own targets
    r2 = A.apply(hk_req(), HK, None, A.pick(opts, ["explore", "challenge"]))
    assert r2.axes == {"exploration": 9, "difficulty": 8}


def test_apply_all_of_it_and_nothing_chosen():
    opts = A.options(HK, HK_P)
    req = hk_req(axes={"pace": 7}, tags_want=["Indie"], words="бодро")
    for r in (A.apply(req, HK, HK_P["feel"], opts, all_of_it=True),
              A.apply(req, HK, HK_P["feel"], [], all_of_it=False),
              A.apply(req, HK, HK_P["feel"], A.pick(opts, []))):
        assert r == req and r is not req
        assert r.focus_axes is None and r.mute_tags == [] and r.focus_labels == []


def test_apply_works_on_plain_objects():
    class R:
        pass
    r = A.apply(R(), HK, HK_P["feel"], A.pick(A.options(HK, HK_P), ["music"]))
    assert r.axes == {} and r.tags_want == ["Great Soundtrack"] and r.focus_axes == []
    assert r.tags_avoid == [] and "музыка" in r.words


# --- free-text answers

def test_from_text_negation_low_difficulty():
    opts = A.options(HK, HK_P)
    ch = A.from_text("сложность бесила", opts)
    assert [(a.key, a.polarity) for a in ch] == [("challenge", -1)]
    r = A.apply(hk_req(), HK, HK_P["feel"], ch)
    assert r.axes == {"difficulty": 2}, r.axes          # 10 - 8, at most 3
    assert {"Difficult", "Souls-like"} <= set(r.tags_avoid)
    assert not set(r.tags_avoid) & set(r.tags_want)
    assert r.focus_axes == []
    for text in ("не люблю хардкор", "сложность меня раздражала", "слишком сложно", "без сложности"):
        got = {a.key: a.polarity for a in A.from_text(text, opts)}
        assert got.get("challenge") == -1, (text, got)


def test_from_text_mixed_answer():
    opts = A.options(HK, HK_P)
    ch = A.from_text("Исследование и атмосфера, а сложность бесила", opts)
    got = [(a.key, a.polarity) for a in ch]
    assert got == [("explore", 1), ("atmos", 1), ("challenge", -1)], got
    r = A.apply(hk_req(), HK, HK_P["feel"], ch)
    assert r.axes == {"exploration": 10, "difficulty": 2}, r.axes
    assert r.focus_axes == ["exploration"]
    assert "Atmospheric" in r.tags_want and "Difficult" in r.tags_avoid and "Difficult" not in r.tags_want
    assert r.focus_labels == ["Исследование мира", "Атмосфера"]
    # lukewarm: left out of the focus, its axis dropped
    ch = A.from_text("атмосфера, а бои так себе", opts)
    assert {a.key: a.polarity for a in ch} == {"atmos": 1, "combat": 0}
    r = A.apply(hk_req(), HK, HK_P["feel"], ch)
    assert "combat" not in r.axes and "combat" not in r.focus_axes
    assert "Action" in r.mute_tags or "Souls-like" in r.mute_tags


def test_from_text_everything():
    opts = A.options(HK, HK_P)
    for text in ("всё", "Всё", "всё сразу", "Всё понравилось!", "в целом", "все"):
        assert A.wants_all(text), text
        ch = A.from_text(text, opts)
        assert [a.key for a in ch] == [o.key for o in opts] and all(a.polarity == 1 for a in ch), text
    assert not A.wants_all("всё, кроме сложности") and not A.wants_all("атмосфера")
    ch = A.from_text("всё, кроме сложности", opts)
    pol = {a.key: a.polarity for a in ch}
    assert pol["challenge"] == 0 and all(pol[o.key] == 1 for o in opts if o.key != "challenge"), pol
    assert ch[-1].key == "challenge"                     # liked ones first
    r = A.apply(hk_req(), HK, HK_P["feel"], ch)
    assert "difficulty" not in r.axes and "Difficult" in r.mute_tags


def test_from_text_not_offered_and_own_words():
    opts = A.options(HK, HK_P)
    ch = A.from_text("атмосфера, ещё сюжет", opts)
    assert [(a.key, a.polarity) for a in ch] == [("atmos", 1), ("story", 1)]
    assert ch[1].axes == {"story": 8} and ch[1].label == "Сюжет"
    ch = A.from_text("ощущение одиночества в огромном подземелье", opts)
    own = [a for a in ch if a.key == "own"]
    assert own and own[0].polarity == 1 and own[0].group == ""
    check_option(own[0])
    assert A.from_text("", opts) == [] and A.from_text("   ", opts) == []
    long = "мне зашло то, как всё в этом мире медленно раскрывается через крошечные детали окружения " * 3
    for a in A.from_text(long, opts):
        check_option(a)


def test_from_text_mne_ponravilos_is_positive():
    # «мне понравилась музыка» contains «не понрав» inside «мНЕ ПОНРАВилась»; «мне очень» contains «не очень»
    opts = A.options(HK, HK_P)
    wrong = []
    for text in ("мне понравилась музыка", "мне нравится музыка", "мне очень понравилась музыка",
                 "мне понравилось, как мир связан шорткатами", "очень понравилась музыка"):
        got = {a.key: a.polarity for a in A.from_text(text, opts)}
        key = "music" if "музык" in text else "explore"
        if got.get(key) != 1:
            wrong.append((text, got))
    assert not wrong, wrong


# --- bot helpers

def test_keyboard_toggles():
    opts = A.options(HK, HK_P)
    rows = A.keyboard(opts)
    flat = [b for row in rows for b in row]
    assert all(len(row) <= 2 for row in rows)
    assert [d for _, d in flat] == [f"asp:{o.key}" for o in opts] + ["asp:all"]
    assert all(not t.startswith("✓") for t, _ in flat)
    assert rows[-1] == [("Всё сразу", "asp:all")]          # no «Готово» before anything is picked
    rows = A.keyboard(opts, ["explore", "music"])
    flat = dict((d, t) for row in rows for t, d in row)
    assert flat["asp:explore"] == "✓ Исследование мира" and flat["asp:music"] == "✓ Музыка"
    assert flat["asp:atmos"] == "Атмосфера"
    assert rows[-1] == [("Всё сразу", "asp:all"), ("Готово", "asp:done")]
    # toggling off: back to unmarked
    rows = A.keyboard(opts, {"explore"} - {"explore"})
    assert rows[-1] == [("Всё сразу", "asp:all")]
    for _, g, p in ALL:
        for row in A.keyboard(A.options(g, p, HK_CARD), [o.key for o in A.options(g, p)]):
            for t, d in row:
                assert len(d.encode()) <= 64 and t
    assert A.keyboard([]) == [[("Всё сразу", "asp:all")]]


def test_dump_load_round_trip():
    for _, g, p in ALL:
        opts = A.options(g, p, HK_CARD)
        rows = json.loads(json.dumps(A.dump(opts), ensure_ascii=False))
        assert A.load(rows) == opts
    ch = A.from_text("атмосфера, а сложность бесила", A.options(HK, HK_P))
    assert A.load(json.loads(json.dumps(A.dump(ch)))) == ch        # polarity survives
    loaded = A.load([None, "x", {"label": "без ключа"}, {"key": "k", "label": ""},
                     {"key": "own", "label": "Тишина", "future_field": 1, "axes": {"tension": "3", "bogus": 5}}])
    assert len(loaded) == 1
    a = loaded[0]
    assert a.key == "own" and a.words == "Тишина" and a.tags == [] and a.axes == {"tension": 3} and a.polarity == 1
    assert A.load(None) == [] and A.load([]) == []


def test_pick():
    opts = A.options(HK, HK_P)
    got = A.pick(opts, ["music", "explore", "nope"])
    assert [o.key for o in got] == ["explore", "music"]          # in the options' order
    assert A.pick(opts, []) == [] and A.pick([], ["explore"]) == []
    assert A.question("Hollow Knight") == "Чем именно зацепила Hollow Knight?"


def test_hooks_are_what_reviews_praise_not_the_setting():
    from gamefinder import aspects
    """Cyberpunk 2077 is loved for its story, builds and world, not for neon: a game with only the
    setting is weak where it counts, a game praised for the same things is strong."""
    cp_game = {"name": "Cyberpunk 2077", "tags": {"Cyberpunk": 1000, "Open World": 900, "RPG": 800, "Story Rich": 700,
                                                  "Atmospheric": 600, "FPS": 500, "Great Soundtrack": 400}}
    cp = {"_source": "llm", "feel": {"story": 9, "complexity": 7, "exploration": 8},
          "praise": [{"point": "Атмосфера и дизайн Найт-Сити", "share": "most"},
                     {"point": "Глубокий сюжет и персонажи", "share": "most"},
                     {"point": "Отличный саундтрек", "share": "many"},
                     {"point": "Гибкая прокачка и билды", "share": "many"}]}
    hooks = [a.group for a in aspects.hooks(cp_game, cp)]
    assert set(hooks[:2]) == {"atmos", "story"} and "depth" in hooks, hooks
    assert not set(hooks) & aspects.NOT_A_HOOK
    neon = aspects.strengths({"tags": {"Cyberpunk": 1000, "Atmospheric": 700, "Flight": 600}}, None)
    rpg = aspects.strengths({"tags": {"RPG": 1000, "Open World": 900}},
                            {"_source": "llm", "feel": {"story": 9, "exploration": 8, "complexity": 8},
                             "praise": [{"point": "Сюжет и квесты", "share": "most"},
                                        {"point": "Прокачка персонажа", "share": "many"}]})
    fit = lambda st: sum(st.get(h, 0) for h in hooks) / len(hooks)  # noqa: E731
    assert fit(rpg) > fit(neon) + 0.3, (fit(rpg), fit(neon))
    assert rpg["story"] == 1.0 and neon.get("story", 0) == 0


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
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    main()
