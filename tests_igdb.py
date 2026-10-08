"""Offline tests for gamefinder.sources.igdb. Run: python tests_igdb.py"""

import asyncio
import logging
import time

from gamefinder.sources import igdb as I
from gamefinder.sources.igdb import Igdb, map_tags, normalise, pick, where_to_buy
from gamefinder.taste import tag_vector


# --- fakes in the documented shapes (https://api-docs.igdb.com/)

class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    async def text(self):
        return str(self.body)


class FakeSession:
    """handler(url, params, body, headers) -> (status, json) or raises."""
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def post(self, url, params=None, data=None, headers=None, timeout=None, **kw):
        body = data.decode() if isinstance(data, bytes) else data
        self.calls.append({"url": url, "params": params, "body": body, "headers": headers, "t": time.monotonic()})
        status, payload = self.handler(url, params, body, headers)
        return FakeResponse(status, payload)


class FakeHttp:
    def __init__(self, handler):
        self.s = FakeSession(handler)

    async def session(self):
        return self.s


TOKEN = {"access_token": "tok1", "expires_in": 5587808, "token_type": "bearer"}


def ts(year):
    return int(time.mktime((year, 6, 1, 0, 0, 0, 0, 0, 0)))


ALAN_WAKE = {
    "id": 1001, "name": "Alan Wake", "slug": "alan-wake", "first_release_date": ts(2010), "game_type": 0,
    "genres": [{"id": 31, "name": "Adventure"}, {"id": 5, "name": "Shooter"}],
    "themes": [{"id": 1, "name": "Action"}, {"id": 19, "name": "Horror"}, {"id": 20, "name": "Thriller"}],
    "player_perspectives": [{"id": 2, "name": "Third person"}],
    "game_modes": [{"id": 1, "name": "Single player"}],
    "platforms": [{"id": 6, "name": "PC (Microsoft Windows)"}, {"id": 12, "name": "Xbox 360"}],
    "external_games": [{"id": 1, "external_game_source": 1, "uid": "108710",
                        "url": "https://store.steampowered.com/app/108710"}],
    "total_rating_count": 900, "rating": 80.2, "rating_count": 700,
}
ALAN_WAKE_2 = {
    "id": 1002, "name": "Alan Wake II", "slug": "alan-wake-ii", "first_release_date": ts(2023), "game_type": 0,
    "alternative_names": [{"id": 9, "name": "Alan Wake 2"}],
    "genres": [{"id": 31, "name": "Adventure"}, {"id": 5, "name": "Shooter"}],
    "themes": [{"id": 1, "name": "Action"}, {"id": 19, "name": "Horror"}, {"id": 21, "name": "Survival"},
               {"id": 43, "name": "Mystery"}],
    "keywords": [{"id": 1, "name": "psychological horror"}, {"id": 2, "name": "Female Protagonist"},
                 {"id": 3, "name": "some-unmapped-keyword"}, {"id": 4, "name": "Souls-like"}],
    "player_perspectives": [{"id": 2, "name": "Third person"}],
    "game_modes": [{"id": 1, "name": "Single player"}],
    "platforms": [{"id": 6, "name": "PC (Microsoft Windows)"}, {"id": 167, "name": "PlayStation 5"},
                  {"id": 169, "name": "Xbox Series X|S"}],
    "similar_games": [{"id": 1001, "name": "Alan Wake"}, {"id": 7, "name": "Control"}],
    "external_games": [
        {"id": 2, "external_game_source": {"id": 26}, "uid": "a1b2c3d4e5",
         "url": "https://store.epicgames.com/en-US/p/alan-wake-2"},
        {"id": 3, "external_game_source": 36, "uid": "UP0000",
         "url": "https://store.playstation.com/en-us/product/UP0000"},
    ],
    "websites": [{"id": 5, "type": 1, "url": "https://www.alanwake.com"}],
    "total_rating": 88.4, "total_rating_count": 500, "rating": 86.0, "rating_count": 300,
    "aggregated_rating": 89.9, "aggregated_rating_count": 80,
    "summary": "A writer trapped in a nightmare.", "cover": {"id": 1, "image_id": "co6jar"},
    "involved_companies": [{"company": {"name": "Remedy Entertainment"}, "developer": True, "publisher": False},
                           {"company": {"name": "Epic Games Publishing"}, "developer": False, "publisher": True}],
}
AW2_DLC = {"id": 1003, "name": "Alan Wake II: Night Springs", "game_type": 1, "total_rating_count": 50}
AW2_DELUXE = {"id": 1004, "name": "Alan Wake II", "game_type": 0, "version_parent": 1002,
              "total_rating_count": 5000}
WITCHER3 = {"id": 1942, "name": "The Witcher 3: Wild Hunt", "game_type": 0, "total_rating_count": 4000,
            "external_games": [{"external_game_source": 1, "uid": "292030"},
                               {"external_game_source": 5, "uid": "1207664663",
                                "url": "https://www.gog.com/game/the_witcher_3_wild_hunt"}],
            "websites": [{"type": 17, "url": "https://www.gog.com/en/game/the_witcher_3_wild_hunt"},
                         {"type": 13, "url": "https://store.steampowered.com/app/292030"}]}
WITCHER2 = {"id": 478, "name": "The Witcher 2: Assassins of Kings", "game_type": 0, "total_rating_count": 2000}
RE4 = {"id": 1, "name": "Resident Evil 4", "game_type": 0, "first_release_date": ts(2005),
       "total_rating_count": 1500}
RE4R = {"id": 2, "name": "Resident Evil 4", "game_type": 8, "first_release_date": ts(2023),
        "total_rating_count": 900}
ARKHAM_A = {"id": 10, "name": "Batman: Arkham Asylum", "game_type": 0, "total_rating_count": 2000}
ARKHAM_C = {"id": 11, "name": "Batman: Arkham City", "game_type": 0, "total_rating_count": 2500}


def run(coro):
    return asyncio.run(coro)


# --- matching

def test_pick_sequel_numbers():
    res = [ALAN_WAKE, ALAN_WAKE_2, AW2_DLC]
    assert pick("Alan Wake 2", res)["id"] == 1002
    assert pick("Alan Wake II", res)["id"] == 1002
    assert pick("alan wake", res)["id"] == 1001
    assert pick("Alan Wake 2", [ALAN_WAKE]) is None, "the original is not the sequel"
    assert pick("Alan Wake 3", res) is None
    # Hades 2 must not become Hades
    assert pick("Hades 2", [{"id": 5, "name": "Hades", "game_type": 0}]) is None
    print("ok pick sequel numbers")


def test_pick_main_over_dlc_and_editions():
    assert pick("Alan Wake II", [AW2_DLC, AW2_DELUXE, ALAN_WAKE_2])["id"] == 1002
    # a DLC named exactly is still found when nothing else matches
    assert pick("Alan Wake II: Night Springs", [AW2_DLC])["id"] == 1003
    # subtitle omitted
    assert pick("Witcher 3", [WITCHER2, WITCHER3])["id"] == 1942
    assert pick("Ведьмак 3", [WITCHER2, WITCHER3])["id"] == 1942
    # same name twice: main game first, an explicit year wins
    assert pick("Resident Evil 4", [RE4R, RE4])["id"] == 1
    assert pick("Resident Evil 4 (2023)", [RE4, RE4R])["id"] == 2
    # a series name matching several games before the colon: unsure
    assert pick("Batman", [ARKHAM_A, ARKHAM_C]) is None
    assert pick("Batman Arkham City", [ARKHAM_A, ARKHAM_C])["id"] == 11
    assert pick("Outer Wilds", [{"id": 3, "name": "The Outer Worlds", "game_type": 0}]) is None
    assert pick("anything", []) is None and pick("x", None) is None
    print("ok pick main game over DLC and editions")


# --- normalising

def test_stores_extraction():
    g = normalise(ALAN_WAKE_2)
    assert g["stores"] == {"steam": None, "gog": None, "epic": "alan-wake-2"}, g["stores"]
    assert g["store_urls"]["epic"] == "https://store.epicgames.com/p/alan-wake-2"
    assert g["store_urls"]["playstation"].startswith("https://store.playstation.com/")
    w = normalise(WITCHER3)
    assert w["stores"]["steam"] == 292030 and w["stores"]["gog"] == 1207664663 and w["stores"]["epic"] is None
    assert w["store_urls"]["steam"] == "https://store.steampowered.com/app/292030/"
    assert w["store_urls"]["gog"] == "https://www.gog.com/en/game/the_witcher_3_wild_hunt"
    # legacy enum field names and website-only links
    stores, urls = I.extract_stores(
        [{"category": 1, "uid": "108710"}, {"category": 26, "uid": "f00ba4"}],
        [{"category": 17, "url": "https://www.gog.com/game/alan_wake"},
         {"type": {"id": 16}, "url": "https://www.epicgames.com/store/en-US/product/alan-wake/home"}])
    assert stores == {"steam": 108710, "gog": "alan_wake", "epic": "alan-wake"}, stores
    stores, urls = I.extract_stores([{"external_game_source": 26, "uid": "f00ba4"}], [])
    assert stores["epic"] == "f00ba4" and "epic" not in urls
    assert I.extract_stores(None, None) == ({"steam": None, "gog": None, "epic": None}, {})
    print("ok stores extraction")


def test_normalise_fields():
    g = normalise(ALAN_WAKE_2)
    assert g["igdb_id"] == 1002 and g["name"] == "Alan Wake II" and g["year"] == 2023
    assert g["platforms"] == ["PC (Microsoft Windows)", "PlayStation 5", "Xbox Series X|S"]
    assert g["genres"] == ["Adventure", "Shooter"] and "Horror" in g["themes"]
    assert g["perspectives"] == ["Third person"] and g["modes"] == ["Single player"]
    assert g["similar"] == ["Alan Wake", "Control"]
    assert g["user_rating"] == 86.0 and g["user_votes"] == 300 and g["critic_rating"] == 89.9
    assert g["cover_url"] == "https://images.igdb.com/igdb/image/upload/t_cover_big/co6jar.jpg"
    assert g["developers"] == ["Remedy Entertainment"] and g["publishers"] == ["Epic Games Publishing"]
    assert g["keywords"][0] in ("psychological horror", "Female Protagonist", "Souls-like")
    assert g["keywords"][-1] == "some-unmapped-keyword"
    many = dict(ALAN_WAKE_2, keywords=[{"name": f"kw{i}"} for i in range(100)])
    assert len(normalise(many)["keywords"]) == 30
    bare = normalise({"id": 7, "name": "Bare"})
    assert bare["year"] is None and bare["user_rating"] is None and bare["user_votes"] == 0
    assert bare["cover_url"] is None and bare["tags"] == {} and bare["similar"] == []
    print("ok normalise fields")


def test_tag_mapping():
    t = normalise(ALAN_WAKE_2)["tags"]
    for tag in ("Adventure", "Shooter", "Action", "Horror", "Survival", "Mystery", "Third Person",
                "Singleplayer", "Psychological Horror", "Female Protagonist", "Souls-like",
                "Third-Person Shooter", "Survival Horror", "Action-Adventure"):
        assert tag in t, (tag, t)
    assert t["Adventure"] == 100 and t["Horror"] == 80 and t["Psychological Horror"] == 50
    assert t["Third Person"] == 60
    assert map_tags(genres=["Role-playing (RPG)"], themes=["Open world"], modes=["Co-operative"],
                    keywords=["metroidvania", "PSYCHOLOGICAL-HORROR"]) == {
        "RPG": 100, "Open World": 80, "Co-op": 60, "Metroidvania": 50, "Psychological Horror": 50}
    # a tag from several sources gets a bonus, capped
    both = map_tags(themes=["Open world"], keywords=["open world"])
    assert both["Open World"] == 90
    assert map_tags(genres=["Shooter"], perspectives=["First person"])["FPS"] == I.W_COMBO
    assert map_tags() == {} and map_tags(genres=["Nonexistent"]) == {}
    entries = sum(len(m) for m in (I.GENRE_MAP, I.THEME_MAP, I.PERSPECTIVE_MAP, I.MODE_MAP, I.KEYWORD_MAP))
    assert entries >= 80, entries
    # every tag the map produces is one the rest of the bot knows
    from gamefinder.external import STEAM_TAGS
    produced = {v for m in (I.GENRE_MAP, I.THEME_MAP, I.PERSPECTIVE_MAP, I.MODE_MAP) for vs in m.values() for v in vs}
    produced |= set(I.KEYWORD_MAP.values()) | {tag for _, tag in I.COMBOS}
    assert produced - set(STEAM_TAGS) <= {"VR"}, produced - set(STEAM_TAGS)
    # taste.tag_vector takes it as is; Singleplayer is generic and drops out
    vec = tag_vector(t, {})
    assert vec and "Singleplayer" not in vec and abs(sum(x * x for x in vec.values()) - 1) < 1e-9
    assert vec["Adventure"] > vec["Psychological Horror"]
    print("ok tag mapping")


def test_where_to_buy():
    shops = where_to_buy(normalise(ALAN_WAKE_2))
    assert shops[0] == ("Epic Games Store", "https://store.epicgames.com/p/alan-wake-2")
    labels = [l for l, _ in shops]
    assert labels == ["Epic Games Store", "PlayStation 5", "Xbox Series X|S"], labels
    assert dict(shops)["PlayStation 5"].startswith("https://store.playstation.com/")
    w = [l for l, _ in where_to_buy(normalise(WITCHER3))]
    assert w == ["Steam", "GOG"], w
    assert where_to_buy({"platforms": ["PC (Microsoft Windows)"]}) == [("PC", None)]
    assert where_to_buy({}) == []
    print("ok where to buy")


# --- the client

def api_handler(games_by_query, *, token=TOKEN, fail_token=False, statuses=None):
    """Twitch token + /games: search bodies by quoted text, id lookups by id."""
    statuses = list(statuses or [])

    def handler(url, params, body, headers):
        if url == I.TOKEN_URL:
            assert params["grant_type"] == "client_credentials" and params["client_id"] == "cid"
            return (400, {"status": 400, "message": "invalid client secret"}) if fail_token else (200, dict(token))
        assert url == f"{I.API}/games", url
        assert headers["Client-ID"] == "cid" and headers["Authorization"].startswith("Bearer ")
        if statuses:
            return statuses.pop(0)
        if body.startswith("search"):
            q = body.split('"')[1]
            assert "where version_parent = null" in body and "external_games.external_game_source" in body
            return 200, games_by_query.get(q.lower(), [])
        gid = int(body.split("where id = ")[1].split(";")[0])
        return 200, [g for g in games_by_query.get("*", []) if g["id"] == gid]
    return handler


def make(handler, cid="cid", secret="sec"):
    http = FakeHttp(handler)
    c = Igdb(http, cid, secret)
    c.interval = 0
    return c, http.s


def test_find_and_game():
    c, s = make(api_handler({"alan wake 2": [ALAN_WAKE, ALAN_WAKE_2, AW2_DLC], "*": [WITCHER3]}))

    async def go():
        g = await c.find("Alan Wake 2")
        assert g and g["igdb_id"] == 1002 and g["stores"]["epic"] == "alan-wake-2"
        again = await c.find("alan wake 2")
        assert again is g, "cached"
        w = await c.game(1942)
        assert w["name"] == "The Witcher 3: Wild Hunt" and w["stores"]["steam"] == 292030
        assert await c.game(999) is None
        assert await c.find("Nothing Like It") is None
    run(go())
    searches = [x for x in s.calls if x["url"].endswith("/games") and x["body"].startswith("search")]
    assert len(searches) == 2, "one search for Alan Wake 2, one for the unknown title; the repeat is cached"
    print("ok find and game")


def test_query_is_sanitised_and_expanded():
    c, s = make(api_handler({"the witcher 3": [WITCHER2, WITCHER3]}))
    g = run(c.find('Ведьмак 3'))
    assert g and g["igdb_id"] == 1942
    c2, s2 = make(api_handler({}))
    run(c2.find('Evil"; fields *; where id = 1'))
    body = [x["body"] for x in s2.calls if x["url"].endswith("/games")][0]
    assert body.count('"') == 2 and body.count(";") == 4, body
    print("ok query sanitised and expanded")


def test_token_caching():
    c, s = make(api_handler({"*": [WITCHER3]}))

    async def go():
        await c.game(1942)
        c._games.clear()
        await c.game(1942)
        assert sum(x["url"] == I.TOKEN_URL for x in s.calls) == 1, "token reused"
        c._token_until = 0          # expired
        c._games.clear()
        await c.game(1942)
        assert sum(x["url"] == I.TOKEN_URL for x in s.calls) == 2, "token renewed after expiry"
    run(go())
    hdr = [x["headers"] for x in s.calls if x["url"].endswith("/games")][0]
    assert hdr["Authorization"] == "Bearer tok1" and hdr["Client-ID"] == "cid"
    assert c._token_until - time.monotonic() > 5587808 - 1000
    print("ok token caching")


def test_401_renews_token_once():
    c, s = make(api_handler({"*": [WITCHER3]}, statuses=[(401, {"message": "Authorization Failure"})]))
    g = run(c.game(1942))
    assert g and g["igdb_id"] == 1942
    assert sum(x["url"] == I.TOKEN_URL for x in s.calls) == 2
    print("ok 401 renews token")


def test_errors_never_raise():
    # disabled: no network at all
    c, s = make(api_handler({}), cid="", secret="")
    assert not c.enabled and run(c.find("Alan Wake 2")) is None and run(c.game(1)) is None and not s.calls
    assert make(api_handler({}))[0].enabled
    # bad credentials: None, and Twitch is not asked again right away
    c, s = make(api_handler({}, fail_token=True))
    assert run(c.find("Alan Wake 2")) is None and run(c.find("Control")) is None
    assert sum(x["url"] == I.TOKEN_URL for x in s.calls) == 1
    # HTTP 400 with an error body
    c, s = make(api_handler({}, statuses=[(400, [{"title": "Syntax Error"}])]))
    assert run(c.find("Alan Wake 2")) is None
    # 429 and 500 retried, then success
    c, s = make(api_handler({"*": [WITCHER3]}, statuses=[(429, {}), (500, {})]))
    t0 = time.monotonic()
    assert run(c.game(1942))["igdb_id"] == 1942
    assert time.monotonic() - t0 >= 2.9, "backs off 1 s then 2 s"
    # the session itself throwing, bad JSON, a non-list body, a junk id
    def boom(*a):
        raise RuntimeError("socket on fire")
    c, _ = make(boom)
    assert run(c.find("Alan Wake 2")) is None and run(c.game(1)) is None
    c, _ = make(api_handler({}, statuses=[(200, ValueError("bad json"))]))
    assert run(c.game(1)) is None
    c, _ = make(api_handler({}, statuses=[(200, {"not": "a list"})]))
    assert run(c.game(1)) is None
    assert run(c.game("not-an-id")) is None
    assert run(make(api_handler({}))[0].find("   ")) is None
    print("ok errors never raise")


def test_spacing():
    c, s = make(api_handler({"*": [WITCHER3]}))
    c.interval = 0.3

    async def go():
        await asyncio.gather(*(c.game(i) for i in (1, 2, 3)))
    run(go())
    times = [x["t"] for x in s.calls if x["url"].endswith("/games")]
    assert len(times) == 3
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(g >= 0.28 for g in gaps), gaps
    print("ok spacing")


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    test_pick_sequel_numbers()
    test_pick_main_over_dlc_and_editions()
    test_stores_extraction()
    test_normalise_fields()
    test_tag_mapping()
    test_where_to_buy()
    test_find_and_game()
    test_query_is_sanitised_and_expanded()
    test_token_caching()
    test_401_renews_token_once()
    test_errors_never_raise()
    test_spacing()
    print("all igdb tests passed")
