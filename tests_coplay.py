"""Offline tests for gamefinder.coplay. Run: python tests_coplay.py

A fake Steam stands in for the network: three fan bases (an RPG, a shooter, a puzzle game),
everyone also sinks hundreds of hours into a "CS2", and each fan base has its own favourites.
"""

import asyncio
import logging
import math
import os
import random
import sqlite3
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gamefinder import coplay  # noqa: E402
from gamefinder.http import HttpError  # noqa: E402

RPG, SHOOTER, PUZZLE, CS2 = 100, 200, 300, 730
RPG_FAV, RPG_FAV2, SHOOTER_FAV, PUZZLE_FAV = 101, 102, 201, 301
MARKER = 999        # only in libraries of players who must be ignored


def sid(n: int) -> str:
    return str(76561198000000000 + n)


class FakeSteam:
    def __init__(self, api_key="test-key"):
        self.api_key = api_key
        self.reviews_by_app: dict[int, dict[str, list]] = {}
        self.libraries: dict[str, list | None | Exception] = {}
        self.review_errors: set[tuple[int, str]] = set()
        self.owned_calls = 0

    def add_review(self, appid, steamid, *, up=True, hours=30.0, filt="all"):
        self.reviews_by_app.setdefault(appid, {}).setdefault(filt, []).append({
            "recommendationid": f"{appid}-{steamid}-{filt}", "voted_up": up, "review": "x",
            "author": {"steamid": steamid, "playtime_forever": int(hours * 60)}})

    async def reviews(self, appid, *, filter="recent", language="all", pages=1, per_page=100, day_range=None):
        assert language == "all"
        if (appid, filter) in self.review_errors:
            raise HttpError(503, f"appreviews/{appid}")
        return {}, list(self.reviews_by_app.get(appid, {}).get(filter, []))[:per_page * pages]

    async def owned_games(self, steam_id):
        self.owned_calls += 1
        lib = self.libraries.get(steam_id)
        if isinstance(lib, Exception):
            raise lib
        return None if lib is None else [dict(g) for g in lib]


def lib(**hours) -> list[dict]:
    return [{"appid": int(k[1:]), "name": k, "playtime_min": int(h * 60), "last_played": 0}
            for k, h in hours.items()]


def world(seed=1, cs2_share=0.75) -> FakeSteam:
    """40 public fans per game; most play CS2 a lot; each fan base has its own favourites."""
    rnd = random.Random(seed)
    steam = FakeSteam()
    n = 0
    favs = {RPG: [RPG_FAV, RPG_FAV2], SHOOTER: [SHOOTER_FAV], PUZZLE: [PUZZLE_FAV]}
    for game, own in favs.items():
        for i in range(40):
            n += 1
            s = sid(n)
            steam.add_review(game, s, hours=40, filt="all" if i % 2 else "recent")
            h = {f"g{game}": 40}
            if rnd.random() < cs2_share:
                h[f"g{CS2}"] = 300 + rnd.randint(0, 200)
            for f in own:
                if rnd.random() < 0.75:
                    h[f"g{f}"] = 20 + rnd.randint(0, 40)
            for _ in range(5):      # noise: a few random games each
                h[f"g{rnd.randint(5000, 9000)}"] = 6 + rnd.randint(0, 10)
            steam.libraries[s] = lib(**h)
    return steam


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    coplay.ensure_schema(conn)
    return conn


def harvest(steam, conn, appid, **kw) -> int:
    return asyncio.run(coplay.harvest(steam, conn, appid, **kw))


def harvested_world(**kw):
    steam, conn = world(**kw), db()
    for g in (RPG, SHOOTER, PUZZLE):
        assert harvest(steam, conn, g) == 40
    return steam, conn


# --- tests

def test_schema_idempotent():
    conn = db()
    coplay.ensure_schema(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"coplay", "coplay_meta", "coplay_pop"} <= tables


def test_aggregation_math():
    steam, conn = FakeSteam(), db()
    steam.add_review(RPG, sid(1), hours=20)
    steam.libraries[sid(1)] = lib(g100=20, g101=10, g102=2, g103=0)
    assert harvest(steam, conn, RPG) == 1
    total = math.log1p(20) + math.log1p(10) + math.log1p(2)
    rows = conn.execute("SELECT b, weight, players FROM coplay WHERE a=?", (RPG,)).fetchall()
    assert len(rows) == 1, rows                     # 102 is under 5 h, 103 unplayed, 100 is the seed
    b, w, c = rows[0]
    assert b == 101 and c == 1 and abs(w - math.log1p(10) / total) < 1e-9
    pop = dict(conn.execute("SELECT b, players FROM coplay_pop"))
    assert pop == {0: 1, 101: 1}, pop                # the seed is not counted in its own harvest
    assert conn.execute("SELECT players FROM coplay_meta WHERE appid=?", (RPG,)).fetchone()[0] == 1


def test_filters_reviews():
    steam, conn = FakeSteam(), db()
    steam.add_review(RPG, sid(1), up=False, hours=100)       # negative
    steam.add_review(RPG, sid(2), hours=5)                   # under 15 h
    steam.add_review(RPG, sid(3), hours=40)
    steam.add_review(RPG, sid(3), hours=40, filt="recent")   # same player in both lists
    for n in (1, 2):
        steam.libraries[sid(n)] = lib(g100=50, g999=50)
    steam.libraries[sid(3)] = lib(g100=40, g101=10)
    assert harvest(steam, conn, RPG) == 1
    assert steam.owned_calls == 1                            # deduplicated, filtered before fetching
    assert not conn.execute("SELECT 1 FROM coplay WHERE b=?", (MARKER,)).fetchone()
    assert not conn.execute("SELECT 1 FROM coplay_pop WHERE b=?", (MARKER,)).fetchone()


def test_reviewers_cap_and_top_neighbours():
    steam, conn = FakeSteam(), db()
    for n in range(30):
        steam.add_review(RPG, sid(n), hours=30)
        steam.libraries[sid(n)] = lib(g100=30, **{f"g{1000 + i}": 6 + (i % 7) for i in range(n * 20, n * 20 + 40)})
    assert harvest(steam, conn, RPG, reviewers=10) == 10
    assert steam.owned_calls == 10
    steam2, conn2 = FakeSteam(), db()
    steam2.add_review(RPG, sid(1), hours=30)
    steam2.libraries[sid(1)] = lib(g100=30, **{f"g{1000 + i}": 6 for i in range(400)})
    assert harvest(steam2, conn2, RPG) == 1
    assert conn2.execute("SELECT COUNT(*) FROM coplay WHERE a=?", (RPG,)).fetchone()[0] == coplay.TOP_NEIGHBOURS
    assert conn2.execute("SELECT COUNT(*) FROM coplay_pop").fetchone()[0] == 401   # pop keeps all + total


def test_popularity_correction():
    for share in (0.75, 1.0):
        _check_popularity(share)


def _check_popularity(share):
    _, conn = harvested_world(cs2_share=share)
    raw = conn.execute("SELECT b FROM coplay WHERE a=? ORDER BY weight DESC LIMIT 1", (RPG,)).fetchone()[0]
    assert raw == CS2                                    # by raw hours CS2 would win
    top = coplay.neighbours(conn, RPG, limit=10)
    ids = [b for b, _ in top]
    assert ids[0] in (RPG_FAV, RPG_FAV2) and set(ids[:2]) == {RPG_FAV, RPG_FAV2}, top
    scores = dict(coplay.neighbours(conn, RPG, limit=300))
    assert scores.get(CS2, 0) < 0.15, scores.get(CS2)
    assert all(0 <= s <= 1 for s in scores.values())
    assert SHOOTER_FAV not in scores and PUZZLE_FAV not in scores   # never co-played with the RPG
    assert scores[RPG_FAV] > 0.5
    # noise games played by a single fan stay well below the real favourites
    noise = [s for b, s in scores.items() if b >= 5000]
    assert noise and max(noise) < scores[RPG_FAV] / 3, max(noise)
    assert coplay.neighbours(conn, 12345) == []


def test_popularity_prior_from_owners():
    """With a single harvest there is no other sample; SteamSpy owners keep CS2 down."""
    steam, conn = world(), db()
    conn.execute("CREATE TABLE games (appid INTEGER PRIMARY KEY, owners INTEGER)")
    conn.executemany("INSERT INTO games VALUES(?, ?)", [(CS2, 100_000_000), (RPG_FAV, 300_000)])
    harvest(steam, conn, RPG)
    scores = dict(coplay.neighbours(conn, RPG, limit=300))
    assert scores[RPG_FAV] > 0.5 and scores.get(CS2, 0) < 0.15, (scores[RPG_FAV], scores.get(CS2))


def test_privacy():
    steam = world()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "coplay.sqlite")
        conn = sqlite3.connect(path)
        coplay.ensure_schema(conn)
        for g in (RPG, SHOOTER):
            asyncio.run(coplay.harvest(steam, conn, g))
        conn.close()
        blob = b""
        for name in os.listdir(d):
            with open(os.path.join(d, name), "rb") as f:
                blob += f.read()
        assert len(blob) > 1000
        for s in steam.libraries:
            assert s.encode() not in blob, s
            assert s.encode("utf-16-le") not in blob and s.encode("utf-16-be") not in blob
            assert int(s).to_bytes(8, "big") not in blob     # nor as an integer column value
        for table in ("coplay", "coplay_meta", "coplay_pop"):
            conn = sqlite3.connect(path)
            for row in conn.execute(f"SELECT * FROM {table}"):
                assert all(not (isinstance(v, int) and v > 2 ** 40) for v in row), row
            conn.close()


def test_private_libraries_and_errors():
    steam, conn = FakeSteam(), db()
    for n in range(12):
        steam.add_review(RPG, sid(n), hours=30)
    for n in range(4):
        steam.libraries[sid(n)] = None                         # private
    steam.libraries[sid(4)] = HttpError(500, "GetOwnedGames")
    steam.libraries[sid(5)] = OSError("connection reset")
    steam.libraries[sid(6)] = []                               # empty
    for n in range(7, 12):
        steam.libraries[sid(n)] = lib(g100=30, g101=20)
    assert harvest(steam, conn, RPG) == 5
    assert conn.execute("SELECT players FROM coplay WHERE a=? AND b=?", (RPG, 101)).fetchone()[0] == 5

    # one review list failing is fine; the other still counts
    steam.review_errors.add((SHOOTER, "all"))
    steam.add_review(SHOOTER, sid(50), hours=30, filt="recent")
    steam.libraries[sid(50)] = lib(g200=30, g201=20)
    assert harvest(steam, conn, SHOOTER) == 1

    # both failing: nothing written, the game stays unharvested and will be retried
    steam.review_errors |= {(PUZZLE, "all"), (PUZZLE, "recent")}
    assert harvest(steam, conn, PUZZLE) == 0
    assert coplay.harvested(conn, [RPG, SHOOTER, PUZZLE]) == {RPG, SHOOTER}

    # reviews fine but every library private: marked with 0 players so the worker moves on
    steam.add_review(400, sid(60), hours=30)
    steam.libraries[sid(60)] = None
    assert harvest(steam, conn, 400) == 0
    assert 400 in coplay.harvested(conn, [400])
    assert coplay.neighbours(conn, 400) == [] and coplay.coplay_score(conn, [(400, 1.0)], [], 101) is None

    # too many consecutive failures: give up early instead of hammering Steam
    steam3, conn3 = FakeSteam(), db()
    for n in range(30):
        steam3.add_review(RPG, sid(n), hours=30)
        steam3.libraries[sid(n)] = HttpError(429, "GetOwnedGames")
    assert harvest(steam3, conn3, RPG) == 0 and steam3.owned_calls == coplay.MAX_FAILURES

    # no API key: does nothing
    steam4 = FakeSteam(api_key="")
    steam4.add_review(RPG, sid(1))
    assert harvest(steam4, db(), RPG) == 0 and steam4.owned_calls == 0


def test_reharvest_merges():
    steam, conn = FakeSteam(), db()
    steam.add_review(RPG, sid(1), hours=30)
    steam.libraries[sid(1)] = lib(g100=30, g101=20)
    harvest(steam, conn, RPG)
    harvest(steam, conn, RPG)
    assert conn.execute("SELECT players FROM coplay_meta WHERE appid=?", (RPG,)).fetchone()[0] == 2
    assert conn.execute("SELECT players FROM coplay WHERE a=? AND b=101", (RPG,)).fetchone()[0] == 2
    assert dict(conn.execute("SELECT b, players FROM coplay_pop"))[0] == 2


def test_coplay_score():
    _, conn = harvested_world()
    fan = [(RPG, 1.0)]
    s_fav = coplay.coplay_score(conn, fan, [], RPG_FAV)
    s_other = coplay.coplay_score(conn, fan, [], SHOOTER_FAV)
    s_cs = coplay.coplay_score(conn, fan, [], CS2)
    assert s_fav is not None and s_fav > 0.5, s_fav
    assert s_other == 0.0, s_other                           # data, but no link: 0, not None
    assert s_cs is not None and s_cs < 0.15, s_cs
    assert coplay.coplay_score(conn, [(4242, 1.0)], [], 4343) is None    # nothing harvested
    assert coplay.coplay_score(conn, [], [], RPG_FAV) is None
    # weak likes count less; a love is capped at 1
    assert coplay.coplay_score(conn, [(RPG, 0.4)], [], RPG_FAV) < s_fav
    assert coplay.coplay_score(conn, [(RPG, 1.5)], [], RPG_FAV) == s_fav
    # the reverse direction: the candidate was harvested, the liked game was not
    assert coplay.coplay_score(conn, [(SHOOTER_FAV, 1.0)], [], SHOOTER) > 0.5
    # a strong neighbour of a disliked game is pushed down
    both = [(RPG, 1.0), (SHOOTER, 1.0)]
    plain = coplay.coplay_score(conn, both, [], SHOOTER_FAV)
    punished = coplay.coplay_score(conn, [(RPG, 1.0)], [SHOOTER], SHOOTER_FAV)
    assert plain > 0.5 and punished == 0.0, (plain, punished)
    mixed = coplay.coplay_score(conn, [(RPG, 1.0)], [SHOOTER], RPG_FAV)
    assert mixed == s_fav                                    # the RPG favourite is not a shooter neighbour
    for c in (RPG_FAV, RPG_FAV2, SHOOTER_FAV, PUZZLE_FAV, CS2, 5001):
        v = coplay.coplay_score(conn, both, [PUZZLE], c)
        assert v is None or 0.0 <= v <= 1.0


def test_strongest_link():
    _, conn = harvested_world()
    a, s = coplay.strongest_link(conn, [SHOOTER, RPG, PUZZLE], RPG_FAV)
    assert a == RPG and s > 0.5
    assert coplay.strongest_link(conn, [SHOOTER, PUZZLE], RPG_FAV) is None
    assert coplay.strongest_link(conn, [4242], RPG_FAV) is None


def test_candidates():
    _, conn = harvested_world()
    cand = coplay.candidates(conn, [(RPG, 1.0)])
    assert RPG not in cand
    ranked = sorted(cand, key=lambda b: -cand[b])
    assert set(ranked[:2]) == {RPG_FAV, RPG_FAV2}, ranked[:5]
    assert cand.get(CS2, 0) < cand[RPG_FAV] / 3
    assert all(0 < v <= 1 for v in cand.values())
    assert len(coplay.candidates(conn, [(RPG, 1.0)], limit=3)) == 3
    # from a liked game nobody harvested, the reverse link still finds the RPG
    rev = coplay.candidates(conn, [(RPG_FAV, 1.0)])
    assert RPG in rev and RPG_FAV not in rev, rev
    # two liked fan bases: both sets of favourites show up; negative weights are ignored
    two = coplay.candidates(conn, [(RPG, 1.0), (PUZZLE, 1.0), (SHOOTER, -1.0)], limit=4)
    assert {RPG_FAV, PUZZLE_FAV} <= set(two) and SHOOTER_FAV not in two, two
    assert coplay.candidates(conn, []) == {}


def test_stale():
    _, conn = harvested_world()
    assert coplay.stale(conn, max_age_days=90) == []
    conn.execute("UPDATE coplay_meta SET updated_at = 0 WHERE appid=?", (SHOOTER,))
    assert coplay.stale(conn, max_age_days=90, limit=5) == [SHOOTER]


def main():
    logging.basicConfig(level=logging.ERROR)     # harvest() logs the simulated failures as warnings
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
