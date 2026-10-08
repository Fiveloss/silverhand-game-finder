"""Offline tests: python tests.py. No network, no Telegram, no LLM key."""

import os
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gamefinder import texts, views  # noqa: E402
from gamefinder.intent import Request  # noqa: E402
from gamefinder.analyst import AXES, heuristic_passport, normalize  # noqa: E402
from gamefinder.db import Db  # noqa: E402
from gamefinder.recommender import Catalog, blocked, complaint_fit, recommend  # noqa: E402
from gamefinder.reviews import clean_text, compute_stats, pick_for_analysis, quality_score, wilson_lower  # noqa: E402
from gamefinder.sources.steam import parse_languages, parse_owners, store_fields  # noqa: E402
from gamefinder.titles import confident, score as title_score  # noqa: E402
from gamefinder.taste import Taste, cosine, for_request, tag_vector  # noqa: E402

NOW = time.time()


def review(up, hours, text="x" * 200, days_ago=1, votes=0, lang="english"):
    return {"recommendationid": str(id(text)) + str(hours) + str(days_ago), "voted_up": up,
            "review": text, "language": lang, "votes_up": votes,
            "timestamp_created": int(NOW - days_ago * 86400),
            "author": {"playtime_at_review": int(hours * 60)}}


def game(appid, name, tags, **kw):
    g = {"appid": appid, "name": name, "name_lc": name.lower(), "tags": tags, "genres": [], "categories": [],
         "short_desc": "", "release_date": "", "release_year": 2020, "price_cents": 1000, "currency": "USD",
         "discount": 0, "ru_text": 1, "ru_audio": 0, "early_access": 0, "mtx": 0, "online_only": 0, "single": 1,
         "coop": 0, "pvp": 0, "drm_notice": "", "owners": 100000, "positive": 9000, "negative": 1000,
         "is_dlc": 0, "adult": 0, "store_ok": 1, "spy_ok": 1}
    g.update(kw)
    return g


def test_parsers():
    assert parse_owners("1,000,000 .. 2,000,000") == 1_000_000
    assert parse_languages("English<strong>*</strong>, Russian<br><strong>*</strong>languages with full audio support") == (True, False)
    assert parse_languages("English, Russian<strong>*</strong>") == (True, True)
    assert parse_languages("English") == (False, False)
    f = store_fields({"name": "X", "type": "game", "categories": [{"id": 1, "description": "Multi-player"},
                      {"id": 35, "description": "In-App Purchases"}], "genres": [{"id": "70", "description": "Early Access"}],
                      "release_date": {"date": "Jun 18, 2020"}, "is_free": True})
    assert f["online_only"] == 1 and f["mtx"] == 1 and f["early_access"] == 1 and f["price_cents"] == 0
    assert f["release_year"] == 2020


def test_clean_text():
    t = clean_text("[h1]Title[/h1] [b]bold[/b] [url=http://x]link[/url] http://y.com\n\n\nend")
    assert "[" not in t and "http" not in t and "link" in t and "\n\n" not in t


def test_stats():
    recent = [review(True, 20) for _ in range(40)] + [review(False, 1) for _ in range(10)]
    s = compute_stats({"total_positive": 900, "total_negative": 100}, recent, NOW)
    assert abs(s["recent_share"] - 0.8) < 1e-9
    assert s["engaged_share"] == 1.0 and s["quick_negative_share"] == 1.0
    assert s["trend"] is not None and s["trend"] < 0
    assert 0 < quality_score(s) < 1
    assert wilson_lower(9, 10) < wilson_lower(900, 1000)
    assert quality_score(None) == 0.5


def test_pick_for_analysis_oversamples_negatives():
    rows = [review(True, 30, f"good game number {i} " * 10) for i in range(80)]
    rows += [review(False, 5, f"bad game reason {i} " * 10) for i in range(10)]
    rows += [review(True, 10, "short")]                                 # too short
    rows += [review(True, 30, "good game number 0 " * 10)]              # duplicate
    picked = pick_for_analysis(rows, limit=20, now=NOW)
    assert len(picked) == 20
    assert sum(not p["up"] for p in picked) == 7                        # 35% of 20
    assert all(len(p["text"]) >= 120 for p in picked)


def test_heuristic_passport():
    g = game(1, "Cozy Farm", {"Farming Sim": 900, "Relaxing": 800, "Cozy": 700, "Singleplayer": 500})
    sample = [{"text": "Way too slow and boring, nothing happens", "up": False},
              {"text": "So slow, tedious grind", "up": False},
              {"text": "Boring and slow", "up": False},
              {"text": "Too much grind and farming", "up": False},
              {"text": "Lovely atmosphere and music", "up": True}]
    p = heuristic_passport(g, None, sample)
    assert p["feel"]["tension"] <= 2 and p["feel"]["pace"] <= 4
    assert all(0 <= v <= 10 for v in p["feel"].values())
    slow = [c for c in p["complaints"] if c["axis"] == "pace"]
    assert slow and slow[0]["direction"] == "low" and slow[0]["kind"] == "taste"


def test_normalize_is_safe():
    p = normalize({"feel": {"pace": 15, "story": "7"}, "complaints": [
        {"point": "x", "kind": "quality", "axis": "pace", "direction": "low", "share": "most"}]})
    assert p["feel"]["pace"] == 10 and p["feel"]["story"] == 7 and p["feel"]["grind"] == 5
    assert p["complaints"][0]["axis"] == "none"
    assert set(p["feel"]) == set(AXES)


def test_normalize_repairs_a_sloppy_model_answer():
    """A model that drops a field or answers with bare strings must not break scores or cards."""
    p = normalize({"praise": ["story", {"point": ""}, {"point": "combat", "share": "lots"}, 7],
                   "complaints": [{"point": "bugs"}, "grind", None,
                                  {"point": "too long", "kind": "taste", "axis": "length", "direction": "high"}],
                   "compared_to": ["Hades", {"name": "x"}, 3], "real_genres": "roguelike"})
    assert p["praise"] == [{"point": "story", "share": "some"}, {"point": "combat", "share": "some"}]
    assert [c["kind"] for c in p["complaints"]] == ["quality", "quality", "taste"]
    assert all(c["axis"] == "none" for c in p["complaints"][:2]) and p["complaints"][2]["axis"] == "length"
    assert p["compared_to"] == ["Hades", "3"] and p["real_genres"] == []
    complaint_fit(Taste(feel={k: 2.0 for k in AXES}, confidence={k: 1.0 for k in AXES}), p)  # must not raise


def test_provider_routing_and_daily_limits():
    """Flash's tiny free quota goes to the judge only; a daily 429 rests a provider until the reset."""
    from types import SimpleNamespace
    from gamefinder.analyst import Analyst, limit_rest, payload_extra, providers_for
    cfg = SimpleNamespace(gemini_api_key="k", gemini_model="flash", gemini_lite_model="lite",
                          groq_api_key="g", groq_model="llama")
    a = Analyst.from_config(None, cfg)
    kinds = lambda task: [p.kind for p in providers_for(a, task)]  # noqa: E731
    assert kinds("passport") == ["lite", "groq"]
    assert kinds("judge") == ["flash", "groq", "lite"]
    assert kinds("intent") == ["groq", "lite", "flash"]
    flash = a.order("judge")[0]
    daily = [{"error": {"code": 429, "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "58009s"}]}}]
    assert limit_rest(flash, daily) == 58009
    assert limit_rest(flash, {"error": {"code": 429}}) == 90
    flash.resting_until = time.time() + limit_rest(flash, daily)
    assert kinds("judge") == ["groq", "lite"]
    assert payload_extra(flash, "judge") == {"reasoning_effort": "low"}
    assert payload_extra(flash, "passport") == {} and payload_extra(a.order("judge")[1], "judge") == {}
    no_groq = Analyst.from_config(None, SimpleNamespace(**{**vars(cfg), "groq_api_key": ""}))
    assert [p.kind for p in no_groq.order("intent")] == ["lite", "flash"]


def test_one_game_per_series():
    from gamefinder.recommender import Pick, diversify, series_key
    assert series_key("The Witcher 2: Assassins of Kings Enhanced Edition") == "witcher"
    assert series_key("Dead Space 2") == series_key("Dead Space") == "dead space"
    c = Catalog()
    c.games = {1: {"name": "The Witcher 2"}, 2: {"name": "The Witcher"}, 3: {"name": "Cyberpunk 2077"},
               4: {"name": "Dragon Age"}}
    c.vecs = {1: {"RPG": 1.0}, 2: {"RPG": 1.0}, 3: {"Cyberpunk": 1.0}, 4: {"Fantasy": 1.0}}
    picks = [Pick(a, s, {}, {}, False) for a, s in ((1, 1.1), (2, 1.0), (3, 0.9), (4, 0.8))]
    assert [p.appid for p in diversify(picks, c, 3)] == [1, 3, 4]


def test_complaint_reads_against_taste():
    p = normalize({"feel": {}, "complaints": [
        {"point": "слишком медленно", "share": "most", "kind": "taste", "axis": "pace", "direction": "low"}]})
    slow_lover = Taste(feel={"pace": 2.0}, confidence={"pace": 1.0})
    speed_lover = Taste(feel={"pace": 9.0}, confidence={"pace": 1.0})
    good, notes_good, _ = complaint_fit(slow_lover, p)
    bad, notes_bad, _ = complaint_fit(speed_lover, p)
    assert good > 0.5 > bad
    assert notes_good == [("слишком медленно", True)] and notes_bad == [("слишком медленно", False)]
    q = normalize({"feel": {}, "quality": {"bugs": 3}, "complaints": [
        {"point": "баги", "share": "many", "kind": "quality", "axis": "none", "direction": "none"}]})
    score, _, warnings = complaint_fit(slow_lover, q)
    assert score < 0.5 and warnings == ["баги"]


def test_dealbreakers():
    g = game(1, "Gacha", {"RPG": 10}, mtx=1)
    assert blocked(g, None, ["mtx"]) and not blocked(g, None, ["horror"])
    h = game(2, "Scary", {"Horror": 10}, ru_text=0)
    assert blocked(h, None, ["horror"]) and blocked(h, None, ["no_ru"])
    assert blocked(game(3, "Bad", {"RPG": 1}), {"quality": {"monetization": 2}}, ["mtx"])
    assert blocked(game(4, "Anticheat", {"FPS": 1}, deck=1), None, ["no_deck"])
    assert not blocked(game(5, "Unknown", {"FPS": 1}, deck=0), None, ["no_deck"])


def test_taste_and_recommend():
    games = [
        game(1, "Outer Wilds", {"Exploration": 1000, "Space": 900, "Mystery": 800, "Story Rich": 700, "Puzzle": 600}),
        game(2, "Subnautica", {"Exploration": 1000, "Survival": 900, "Underwater": 800, "Open World": 700}),
        game(3, "Return of the Obra Dinn", {"Mystery": 1000, "Detective": 900, "Puzzle": 800, "Story Rich": 600}),
        game(4, "Call of Duty", {"FPS": 1000, "Shooter": 900, "Multiplayer": 800, "Action": 700}),
        game(5, "Battlefield", {"FPS": 1000, "Shooter": 900, "War": 800, "Multiplayer": 700}),
        game(6, "Outer Worlds Gacha", {"Exploration": 1000, "Space": 900, "Mystery": 800}, mtx=1),
        game(7, "The Witness", {"Puzzle": 1000, "Exploration": 900, "Mystery": 800, "Open World": 500}),
    ]
    cat = Catalog()
    cat.build(games)
    assert abs(cosine(cat.vecs[1], cat.vecs[1]) - 1) < 1e-9
    assert cosine(cat.vecs[1], cat.vecs[3]) > cosine(cat.vecs[1], cat.vecs[4])
    by_id = {g["appid"]: g for g in games}
    # «как Outer Wilds, но не как Call of Duty, неторопливое, без доната»
    req = Request(seeds=["Outer Wilds"], avoid=["Call of Duty"], axes={"pace": 2}, dealbreakers=["mtx"])
    taste = for_request(req, [by_id[1]], [by_id[4]], {}, cat.idf, set())
    assert taste.liked == [1] and taste.disliked == [4] and taste.feel["pace"] == 2
    picks = recommend(cat, taste, {}, {}, limit=3)
    ids = [p.appid for p in picks]
    assert 1 not in ids and 4 not in ids and 6 not in ids     # the references themselves, dealbreaker
    assert 5 not in ids                                        # like the disliked shooter
    assert {3, 7} <= set(ids) <= {2, 3, 7}                      # Subnautica shares too little (0.15)
    assert picks[0].because == 1
    # Real players who love Outer Wilds also sink hours into Subnautica: it gets in despite weak tags.
    co = recommend(cat, taste, {}, {}, limit=3, co_cands={2: 0.8},
                   coplay=lambda a: 0.8 if a == 2 else 0.0, experience=lambda ids: {a: 0.9 for a in ids})
    assert 2 in [p.appid for p in co]
    sub = next(p for p in co if p.appid == 2)
    assert sub.parts["coplay"] == 1.0 and sub.parts["experience"] == 0.9


def test_request_focus():
    seed = game(1, "Hollow Knight", {"Metroidvania": 1000, "Difficult": 900, "Exploration": 800})
    passports = {1: normalize({"feel": {"difficulty": 9, "exploration": 9, "combat": 8}})}
    idf = {"Metroidvania": 1.0, "Difficult": 1.0, "Exploration": 1.0}
    whole = for_request(Request(seeds=["Hollow Knight"]), [seed], [], passports, idf, set())
    assert whole.confidence["difficulty"] == 0.6 and whole.feel["difficulty"] == 9
    # they loved the exploration, not the difficulty
    req = Request(seeds=["Hollow Knight"], focus_axes=["exploration"], mute_tags=["Difficult"],
                  axes={"exploration": 9})
    focused = for_request(req, [seed], [], passports, idf, set())
    assert focused.confidence["exploration"] == 1.0 and focused.confidence["difficulty"] == 0.15
    assert focused.like_vec["Difficult"] < whole.like_vec["Difficult"] / 3
    other = for_request(Request(seeds=["Hollow Knight"], diversify=True, tags_want=["Puzzle"]),
                        [seed], [], passports, idf, set())
    assert "Metroidvania" not in other.like_vec and "Puzzle" in other.like_vec


def test_titles():
    assert title_score("Alan Wake 2", "Alan Wake") == 0                 # the sequel is not on Steam
    assert title_score("Hades", "Hades II") == 0 and confident("hades 2", "Hades II")
    assert confident("disco elysium", "Disco Elysium - The Final Cut")
    assert confident("witcher 3", "The Witcher 3: Wild Hunt") and confident("GTA 5", "Grand Theft Auto V")
    assert not confident("Outer Wilds", "Outer Worlds")
    assert title_score("hollow knight", "Hollow Knight") > title_score("hollow knight", "Hollow Knight: Silksong")
    assert confident("bg3", "Baldur's Gate 3") and confident("cyberpunk", "Cyberpunk 2077")


def test_db_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        db = Db(os.path.join(d, "t.db"))
        db.upsert_game(10, name="Hades", tags={"Roguelike": 5})
        db.upsert_game(10, positive=300, negative=10)
        g = db.game(10)
        assert g["tags"] == {"Roguelike": 5} and g["positive"] == 300 and g["name_lc"] == "hades"
        assert db.find_by_name("had")[0]["appid"] == 10
        assert [x["appid"] for x in db.catalog(200)] == [10]
        db.enqueue_analysis([10, 11], priority=1)
        db.enqueue_analysis([11], priority=5)
        assert db.next_in_queue(2) == [11, 10]
        db.set_passport(10, {"feel": {}}, "llm", "m", 30)
        assert db.passport(10)["_source"] == "llm" and db.queue_size() == 1
        u = db.user(5, "Ann")
        assert u["prefs"] == {} and u["state"] == ""
        db.update_user(5, prefs={"pace": 2}, dealbreakers=["mtx"])
        assert db.user(5)["prefs"] == {"pace": 2}
        db.set_user_game(5, 10, "like", "steam", 600)
        db.set_user_game(5, 10, "love", "chat", 0)
        row = db.user_games(5)[0]
        assert row["verdict"] == "love" and row["playtime_min"] == 600
        db.mark_shown(5, [10], {10: {"tags": 0.7}})
        db.set_user_game(5, 10, "want", "feedback")
        assert db.feedback(5) == [("want", {"tags": 0.7})]
        db.add_llm_usage(1000, 200)
        assert db.llm_games_today() == 1
        db.reset_user(5)
        assert db.user_games(5) == []
        db.close()


def test_texts_render():
    g = game(1, "Disco <Elysium>", {"RPG": 10}, genres=["RPG"])
    p = normalize({"summary": "Детектив & RPG", "real_genres": ["CRPG"], "feel": {"story": 10},
                   "praise": [{"point": "текст", "share": "most"}],
                   "complaints": [{"point": "много текста", "share": "many", "kind": "taste",
                                   "axis": "story", "direction": "high"}]})
    p.update({"_source": "llm", "_reviews_used": 60})
    stats = compute_stats({"total_positive": 90, "total_negative": 10}, [review(True, 20)] * 30, NOW)
    out = texts.passport_card(g, p, stats)
    assert out.startswith("🟠 <b>Disco &lt;Elysium&gt;</b>") and "&amp;" in out and "по 60 отзывам" in out
    from gamefinder.recommender import Pick
    pick = Pick(1, 0.8, {}, p, True, because=2, feel_matches=["story"],
                taste_notes=[("много текста", True)], warnings=["баги"])
    card = views.pick_caption(g, pick, "Planescape")
    assert "Planescape" in card and "скорее плюс" in card and "⚠️" in card
    pick.judge_reason, pick.judge_risk = "Как в Planescape, тут решают разговоры.", "много чтения"
    judged = views.pick_caption(g, pick, "Planescape")
    assert "решают разговоры" in judged and "много чтения" in judged
    assert "скорее плюс" not in judged and "⚠️" in judged       # quality warnings stay
    view = views.pick_view(g, pick, stats, 1, {"story": 9})
    assert view["feel"][0][0] == "Сюжет" and len(view["feel"]) == 4 and view["judge"]
    out = texts.passport_card(game(1, "x", {}, deck=3, proton="gold"), p, None)
    assert "Совместимость" in out and "ProtonDB: gold" in out
    assert texts.price(game(1, "x", {}, price_cents=297500, currency="KZT")) == "2 975 KZT"
    assert tag_vector({}, {}) == {}


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
    main()
