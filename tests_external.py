"""Offline tests for reference games outside Steam: python tests_external.py. No network."""

import asyncio
import json
import os
import sqlite3
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gamefinder import external  # noqa: E402
from gamefinder.analyst import AXES, QUALITY_KEYS, Analyst, Provider, normalize  # noqa: E402
from gamefinder.external import (card_as_game, card_passport, describe, ensure_schema,  # noqa: E402
                                 title_key)
from gamefinder.taste import cosine, tag_vector  # noqa: E402


class FakeHttp:
    """Answers post_json from a queue per host; records every call."""

    def __init__(self, answers: dict[str, list]):
        self.answers = {h: list(a) for h, a in answers.items()}
        self.calls = []

    async def post_json(self, url, payload, *, headers=None, timeout=180):
        self.calls.append((url, payload))
        host = url.split("/")[2]
        status, content = self.answers[host].pop(0)
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False)
        body = {"choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50}} if status == 200 else {"error": "x"}
        return status, body


def analyst(http) -> Analyst:
    return Analyst(http, [Provider("Gemini", "https://gemini.test/v1/chat", "k1", "gm", 60),
                          Provider("Groq", "https://groq.test/v1/chat", "k2", "gq", 25)])


def db():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    return conn


ALAN_WAKE = {
    "known": True, "name": "Alan Wake 2", "year": 2023, "platforms": ["PC", "PS5", "Xbox Series"],
    "on_steam": False,
    "tags": {"Psychological Horror": 100, "Survival Horror": 90, "Story Rich": 85, "Third Person": 70,
             "Atmospheric": 75, "Mystery": 65, "Detective": 55, "Cinematic": 50, "Made Up Tag": 99,
             "soulslike": 0, "third-person shooter": 40},
    "feel": {"pace": 3, "difficulty": 4, "story": 14, "freedom": 3, "complexity": "4", "grind": -2,
             "tension": 9, "combat": 5, "exploration": 6, "social": 0, "length": 5, "replay": 2},
    "aspects": [
        {"label_ru": "Атмосфера страха", "axes": {"tension": 9, "bogus": 3}, "tags": ["Psychological Horror", "Nope"],
         "words_ru": "давящая атмосфера, страшно идти дальше"},
        {"label_ru": "Детективная доска и расследование улик", "axes": {"exploration": 12},
         "tags": ["Detective"], "words_ru": "собираешь улики на доске разума"},
        {"label_ru": "атмосфера страха", "axes": {}, "tags": [], "words_ru": "дубль"},
        {"label_ru": "Сюжет-головоломка", "axes": {"story": 10}, "tags": ["Story Rich"], "words_ru": "мета-сюжет"},
        {"label_ru": "Музыкальные вставки", "axes": {}, "tags": ["Great Soundtrack"], "words_ru": "рок-опера"},
        {"label_ru": "Два героя", "axes": {"story": 8}, "tags": [], "words_ru": "Сага и Алан"},
        {"label_ru": "Свет как оружие", "axes": {"combat": 5}, "tags": ["Survival Horror"], "words_ru": "фонарик"},
        {"label_ru": "Визуал", "axes": {}, "tags": ["Cinematic"], "words_ru": "кино"},
        {"label_ru": "Лишний", "axes": {}, "tags": [], "words_ru": "восьмой"},
        {"label_ru": "", "axes": {}, "tags": [], "words_ru": "пустой"},
    ],
    "steam_similar": ["Control", "Alan Wake", "Silent Hill 2", "Alan Wake 2", "Control", "The Medium", "Observer"],
}


def run(coro):
    return asyncio.run(coro)


def test_good_card():
    http = FakeHttp({"gemini.test": [(200, ALAN_WAKE)]})
    conn = db()
    card = run(describe(analyst(http), conn, "алан вейк 2"))
    assert card and card["known"] is True and card["name"] == "Alan Wake 2"
    assert card["year"] == 2023 and card["on_steam"] is False and card["platforms"][0] == "PC"
    assert card["model"] == "Gemini/gm"
    # the system prompt carries the vocabulary and the no-inventing rule
    system = http.calls[0][1]["messages"][0]["content"]
    assert "Psychological Horror" in system and '"known": false' in system
    assert "алан вейк 2" in http.calls[0][1]["messages"][1]["content"]
    # cached under the asked title and the canonical name
    keys = {r[0] for r in conn.execute("SELECT title_key FROM external_games")}
    assert keys == {title_key("алан вейк 2"), title_key("Alan Wake 2")}, keys
    # similar: deduplicated, without the game itself, at most 5
    assert card["steam_similar"] == ["Control", "Alan Wake", "Silent Hill 2", "The Medium", "Observer"]


def test_unknown_tags_dropped():
    card = run(describe(analyst(FakeHttp({"gemini.test": [(200, ALAN_WAKE)]})), db(), "Alan Wake 2"))
    tags = card["tags"]
    assert "Made Up Tag" not in tags
    assert "Souls-like" not in tags          # weight 0 is not a vote
    assert tags["Third-Person Shooter"] == 40  # case-insensitive match to the canonical spelling
    assert set(tags) <= set(external.STEAM_TAGS)
    assert all(1 <= v <= 100 for v in tags.values())
    assert len(external.STEAM_TAGS) >= 150
    # 0..1 shares become vote-like weights; a list becomes ranked weights
    assert external._tags({"Horror": 1.0, "Mystery": 0.5}) == {"Horror": 100, "Mystery": 50}
    assert external._tags(["Horror", "Mystery"]) == {"Horror": 100, "Mystery": 96}


def test_axes_clamped():
    card = run(describe(analyst(FakeHttp({"gemini.test": [(200, ALAN_WAKE)]})), db(), "Alan Wake 2"))
    f = card["feel"]
    assert set(f) == set(AXES)
    assert f["story"] == 10 and f["grind"] == 0 and f["complexity"] == 4
    assert all(isinstance(v, int) and 0 <= v <= 10 for v in f.values())
    a = card["aspects"]
    assert a[0]["axes"] == {"tension": 9} and a[0]["tags"] == ["Psychological Horror"]
    assert a[1]["axes"] == {"exploration": 10}


def test_aspects_trimmed():
    card = run(describe(analyst(FakeHttp({"gemini.test": [(200, ALAN_WAKE)]})), db(), "Alan Wake 2"))
    a = card["aspects"]
    assert len(a) == 7, len(a)
    labels = [x["label_ru"] for x in a]
    assert all(0 < len(x) <= 28 for x in labels), labels
    assert len({x.casefold() for x in labels}) == len(labels)        # duplicate dropped
    assert labels[1] == "Детективная доска и", labels[1]             # cut on a word boundary
    assert "Лишний" not in labels


def test_unknown_game_negative_cache():
    http = FakeHttp({"gemini.test": [(200, {"known": False}), (200, ALAN_WAKE)]})
    conn = db()
    an = analyst(http)
    assert run(describe(an, conn, "Zzyzx Quest 7")) is None
    row = conn.execute("SELECT data FROM external_games WHERE title_key=?", (title_key("Zzyzx Quest 7"),)).fetchone()
    assert row and json.loads(row[0])["known"] is False
    # within a day: no new call
    assert run(describe(an, conn, "zzyzx quest 7!")) is None
    assert len(http.calls) == 1
    # a day later it is asked again
    conn.execute("UPDATE external_games SET updated_at = ?", (int(time.time()) - 86400 - 5,))
    assert run(describe(an, conn, "Zzyzx Quest 7")) is not None
    assert len(http.calls) == 2


def test_too_few_tags_is_unknown():
    thin = {**ALAN_WAKE, "tags": {"Horror": 100, "Fake": 50}}
    conn = db()
    assert run(describe(analyst(FakeHttp({"gemini.test": [(200, thin)]})), conn, "X")) is None
    assert conn.execute("SELECT COUNT(*) FROM external_games").fetchone()[0] == 1


def test_cache_hit_no_call():
    http = FakeHttp({"gemini.test": [(200, ALAN_WAKE)]})
    conn = db()
    an = analyst(http)
    first = run(describe(an, conn, "Alan Wake 2"))
    again = run(describe(an, conn, "ALAN WAKE 2"))
    by_name = run(describe(an, conn, "alan wake 2™"))
    assert first == again == by_name
    assert len(http.calls) == 1


def test_429_next_provider():
    http = FakeHttp({"gemini.test": [(429, "")], "groq.test": [(200, ALAN_WAKE)]})
    an = analyst(http)
    card = run(describe(an, db(), "Alan Wake 2"))
    assert card and card["model"] == "Groq/gq"
    assert an.providers[0].resting_until > time.time()
    assert [c[0].split("/")[2] for c in http.calls] == ["gemini.test", "groq.test"]
    # resting provider is skipped next time
    http.answers["groq.test"].append((200, {**ALAN_WAKE, "name": "Control"}))
    assert run(describe(an, db(), "Control"))["name"] == "Control"
    assert http.calls[-1][0].startswith("https://groq.test")


def test_failures_return_none_uncached():
    conn = db()
    http = FakeHttp({"gemini.test": [(500, ""), (200, "not json at all")], "groq.test": [(429, "")]})
    an = analyst(http)
    assert run(describe(an, conn, "Alan Wake 2")) is None
    assert conn.execute("SELECT COUNT(*) FROM external_games").fetchone()[0] == 0
    an.providers[1].resting_until = 0
    http.answers["groq.test"].append((200, ALAN_WAKE))
    # garbage from one provider falls through to the next
    assert run(describe(an, conn, "Alan Wake 2"))["model"] == "Groq/gq"
    conn = db()
    # no analyst / no providers / broken db: still None, never raises
    assert run(describe(None, conn, "Alan Wake 2")) is None
    assert run(describe(Analyst(http, []), conn, "Alan Wake 2")) is None
    assert run(describe(an, object(), "Alan Wake 2")) is None
    assert run(describe(an, conn, "   ")) is None


def test_accepts_db_object():
    class Db:
        conn = sqlite3.connect(":memory:")
    card = run(describe(analyst(FakeHttp({"gemini.test": [(200, ALAN_WAKE)]})), Db(), "Alan Wake 2"))
    assert card and Db.conn.execute("SELECT COUNT(*) FROM external_games").fetchone()[0] == 1   # asked title = canonical name: one row


def test_card_as_game_and_passport():
    card = run(describe(analyst(FakeHttp({"gemini.test": [(200, ALAN_WAKE)]})), db(), "Alan Wake 2"))
    g = card_as_game(card)
    assert g["appid"] < 0 and g["appid"] == card_as_game(dict(card))["appid"]
    assert card_as_game({**card, "name": "Control"})["appid"] != g["appid"]
    assert g["name"] == "Alan Wake 2" and g["genres"] == [] and g["store_ok"] == 0
    assert g["release_year"] == 2023 and g["external"] is True and g["on_steam"] is False
    for k in ("tags", "categories", "short_desc", "positive", "negative", "owners", "mtx", "online_only",
              "early_access", "ru_text", "is_dlc", "deck", "proton", "drm_notice", "coop", "single"):
        assert k in g, k
    vec = tag_vector(g["tags"], {})
    assert vec and "Atmospheric" not in vec            # generic tags skipped as for Steam games
    assert abs(sum(x * x for x in vec.values()) - 1) < 1e-9
    steam_like = tag_vector({"Psychological Horror": 3000, "Survival Horror": 2500, "Story Rich": 1800}, {})
    assert cosine(vec, steam_like) > 0.5

    p = card_passport(card)
    assert normalize(p) == p
    assert p["feel"] == card["feel"] and set(p["quality"]) == set(QUALITY_KEYS)
    assert [x["point"] for x in p["praise"]] == [a["label_ru"] for a in card["aspects"][:5]]
    assert p["praise"][0]["share"] == "most" and p["praise"][-1]["share"] == "some"
    assert "Psychological Horror" in p["real_genres"] and p["compared_to"][0] == "Control"
    assert p["_source"] == "external" and p["aspects"][0]["label_ru"] == "Атмосфера страха"
    # a bare card still gives a usable passport
    bare = card_passport({"name": "X"})
    assert set(bare["feel"]) == set(AXES) and bare["praise"] == []


TESTS = [v for k, v in dict(globals()).items() if k.startswith("test_")]

if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"ok   {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)
