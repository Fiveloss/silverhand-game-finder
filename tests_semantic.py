"""Offline tests for gamefinder/semantic.py: python tests_semantic.py. No network: a fake Http."""

import asyncio
import math
import os
import random
import sqlite3
import sys
import traceback
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gamefinder import semantic as sem  # noqa: E402

MODEL = "gemini-embedding-2"


def fake_embedding(text: str, dims: int) -> list[float]:
    """Bag of words hashed into `dims` slots: texts sharing words get close vectors."""
    v = [0.0] * dims
    for w in text.lower().replace("|", " ").split():
        if w in ("title:", "none", "text:", "task:", "search", "result", "query:"):
            continue
        v[zlib.crc32(w.encode()) % dims] += 1.0
    v[0] += 0.01
    return v


class FakeHttp:
    """post_json(url, payload, headers=...) -> (status, body), like gamefinder.http.Http."""

    def __init__(self, responses=None, dims_out=3072, reject_dimensions=False):
        self.calls = []
        self.responses = list(responses or [])
        self.dims_out = dims_out
        self.reject_dimensions = reject_dimensions

    async def post_json(self, url, payload, *, headers=None, timeout=180):
        self.calls.append((url, payload, headers))
        if self.responses:
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        if self.reject_dimensions and "dimensions" in payload:
            return 400, {"error": {"message": "Unknown name \"dimensions\": Cannot find field."}}
        n = payload.get("dimensions", self.dims_out)
        data = [{"object": "embedding", "index": i, "embedding": fake_embedding(t, max(n, self.dims_out))}
                for i, t in enumerate(payload["input"])]
        data.reverse()      # order must come from "index", not position
        return 200, {"object": "list", "data": data, "model": payload["model"]}


def run(coro):
    return asyncio.run(coro)


def db():
    conn = sqlite3.connect(":memory:")
    sem.ensure_schema(conn)
    return conn


def unit(dims=sem.DIM, seed=1):
    rnd = random.Random(seed)
    return sem.normalize([rnd.gauss(0, 1) for _ in range(dims)])


PASSPORT = {
    "summary": "Неторопливое исследование подводного мира, атмосфера одиночества и тревоги.",
    "core_loop": "Плаваешь, собираешь ресурсы, строишь базу и спускаешься глубже.",
    "praise": [{"point": "атмосфера", "share": "most"}, {"point": "исследование", "share": "many"}],
    "best_for": "Тем, кто любит исследовать в своём темпе.",
    "complaints": [],
}

SAMPLE = [
    {"text": "The atmosphere is incredible. Every time you dive deeper you feel the tension rise, "
             "and the moment you hear a leviathan roar you forget to breathe. Exploring the dark is "
             "terrifying and rewarding at once.",
     "up": True, "hours": 60.0, "lang": "english", "helpful": 40},
    {"text": "☐ Bad ☑ Good ☐ Excellent\n---{ Graphics }---\n☐ Potato ☑ Decent\n---{ Price }---\n"
             "☑ Worth it ☐ Wait for sale ☐ Refund it",
     "up": True, "hours": 5.0, "lang": "english", "helpful": 2},
    {"text": "Bought it on sale for $5, great price, buy it, the developer keeps patching it. "
             "Update after update the DLC is worth the price. 10/10 would buy again.",
     "up": True, "hours": 3.0, "lang": "english", "helpful": 0},
    {"text": "Затягивает невероятно: каждый раз думаешь «ещё одно погружение» и залипаешь до утра. "
             "Ощущение, что ты один в огромном океане, не отпускает.",
     "up": True, "hours": 45.0, "lang": "russian", "helpful": 10},
    {"text": "The late game becomes a tedious grind for materials, and backtracking across the map "
             "feels boring after the twentieth hour. I lost all sense of tension.",
     "up": False, "hours": 30.0, "lang": "english", "helpful": 5},
    {"text": "Calm and relaxing sections in the shallows where you can just float and explore the reefs. "
             "It feels meditative. You can spend hours just building your base. The music in the kelp "
             "forest makes you slow down. Every biome has its own mood and you feel it right away.",
     "up": True, "hours": 80.0, "lang": "english", "helpful": 3},
]


# --- storage

def test_quantisation_round_trip():
    v = unit(seed=7)
    blob = sem.pack(v)
    assert len(blob) == 4 + sem.DIM == 772, len(blob)
    back = sem.unpack(blob)
    assert len(back) == sem.DIM
    assert sem.cosine(v, back) > 0.9999, sem.cosine(v, back)
    scale = max(abs(x) for x in v) / 127
    assert max(abs(a - b) for a, b in zip(v, back)) <= scale / 2 + 1e-9
    # A stored vector compared without dequantising gives the same cosine.
    w = unit(seed=8)
    assert abs(sem._blob_cosine(blob, w) - sem.cosine(v, w)) < 1e-3
    assert sem.unpack(b"") is None and sem.unpack(None) is None
    assert sem.unpack(sem.pack([0.0] * 4)) == [0.0] * 4


def test_cosine_and_score_mapping():
    a, b = [1.0, 0.0], [0.0, 1.0]
    assert sem.cosine(a, a) == 1.0 and sem.cosine(a, b) == 0.0
    assert sem.cosine(a, [1.0, 0.0, 0.0]) == 0.0         # mismatched sizes never crash
    assert sem.score_from_cosine(sem.COS_CEIL + 0.1) == 1.0
    assert sem.score_from_cosine(sem.COS_FLOOR - 0.1) == 0.0
    mid = (sem.COS_FLOOR + sem.COS_CEIL) / 2
    assert abs(sem.score_from_cosine(mid) - 0.5) < 1e-9
    assert sem.normalize([0.0, 0.0]) is None
    c = sem.combine([([1.0, 0.0], 1.0), ([0.0, 1.0], 1.0), (None, 5.0)])
    assert abs(c[0] - c[1]) < 1e-9 and abs(math.hypot(*c) - 1) < 1e-9


def test_experience_score():
    conn = db()
    v, other = unit(seed=1), unit(seed=2)
    conn.execute("INSERT INTO game_vectors(appid, kind, vec) VALUES(1, 'experience', ?)", (sem.pack(v),))
    conn.execute("INSERT INTO game_vectors(appid, kind, vec) VALUES(2, 'experience', ?)", (sem.pack(other),))
    same = sem.experience_score(conn, v, 1)
    assert same is not None and same > 0.99, same
    # Random 768-dim vectors are nearly orthogonal: far below the floor.
    assert sem.experience_score(conn, v, 2) == 0.0
    assert sem.experience_score(conn, v, 3) is None             # not indexed
    assert sem.experience_score(conn, None, 1) is None          # no taste vector
    # The scale of the taste vector does not matter.
    assert abs(sem.experience_score(conn, [x * 5 for x in v], 1) - same) < 1e-9
    bulk = sem.experience_scores(conn, v, [1, 2, 3])
    assert set(bulk) == {1, 2} and abs(bulk[1] - same) < 1e-9


def test_experience_text():
    t = sem.experience_text(PASSPORT)
    for part in ("Неторопливое", "Плаваешь", "атмосфера; исследование", "Для кого: Тем"):
        assert part in t, (part, t)
    assert sem.experience_text({}) == ""
    assert "x; y" in sem.experience_text({"praise": ["x", {"point": "y"}]})


# --- snippets

def test_snippet_selection():
    snips = sem.pick_snippets(SAMPLE)
    texts = [s["text"] for s in snips]
    assert snips, "no snippets"
    for s in snips:
        assert sem.SNIPPET_MIN <= len(s["text"]) <= sem.SNIPPET_MAX, s
        assert set(s) == {"text", "up", "hours", "lang"}
    # Checklist templates and price talk are not about how it feels to play.
    assert not any("☐" in t or "☑" in t for t in texts), texts
    assert not any("$5" in t or "10/10" in t for t in texts), texts
    # Experience descriptions in both languages made it.
    assert any("tension" in t for t in texts)
    assert any("Затягивает" in t or "океане" in t for t in texts)
    # At most two pieces from one review.
    assert sum("reef" in t or "biome" in t or "kelp" in t or "base" in t for t in texts) <= 2
    assert any(not s["up"] for s in snips) and sum(s["up"] for s in snips) >= len(snips) / 2
    # Hours travel with the snippet.
    assert any(s["hours"] == 60.0 for s in snips)
    # The limit holds and the positive share leans positive.
    many = [dict(SAMPLE[0], text=f"Review {i}: " + SAMPLE[0]["text"], up=i % 3 != 0)
            for i in range(40)]
    picked = sem.pick_snippets(many, limit=10)
    assert len(picked) == 10 and sum(s["up"] for s in picked) == 7, [s["up"] for s in picked]


def test_cut_long_sentence():
    long = "It feels " + "really " * 80 + "great."
    pieces = sem._cut(long)
    assert len(pieces) == 1 and len(pieces[0]) <= sem.SNIPPET_MAX and pieces[0].endswith("…")
    short = sem._cut("Too short. Also short.")
    assert short == []


# --- evidence

def test_evidence_ordering():
    conn = db()
    q = unit(seed=11)
    noise = unit(seed=12)

    def mix(a, w):
        return sem.normalize([a_i * w + n_i * (1 - w) for a_i, n_i in zip(q, noise)])

    rows = [
        (0, "far positive", 1, 10.0, mix(q, 0.1)),
        (1, "close negative", 0, 30.0, mix(q, 0.95)),
        (2, "close positive", 1, 40.0, mix(q, 0.93)),     # a hair further, but positive: first
        (3, "mid positive", 1, 5.0, mix(q, 0.6)),
    ]
    for idx, text, up, hours, v in rows:
        conn.execute("INSERT INTO review_snippets(appid, idx, text, up, hours, vec) VALUES(7, ?, ?, ?, ?, ?)",
                     (idx, text, up, hours, sem.pack(v)))
    ev = sem.evidence(conn, q, 7, k=2)
    assert [e["text"] for e in ev] == ["close positive", "close negative"], ev
    assert ev[0]["hours"] == 40.0 and ev[0]["up"] is True and ev[1]["up"] is False
    assert [e["text"] for e in sem.evidence(conn, q, 7, k=4)][-1] == "far positive"
    assert sem.evidence(conn, q, 8) == [] and sem.evidence(conn, None, 7) == []


# --- the API

def test_embed_texts_shape_and_truncation():
    http = FakeHttp()
    vecs = run(sem.embed_texts(http, "KEY", MODEL, ["dark ocean tension", "cozy farming"]))
    assert len(vecs) == 2 and all(len(v) == sem.DIM for v in vecs)
    assert all(abs(math.sqrt(sum(x * x for x in v)) - 1) < 1e-9 for v in vecs)
    url, payload, headers = http.calls[0]
    assert url == "https://generativelanguage.googleapis.com/v1beta/openai/embeddings"
    assert payload == {"model": MODEL, "input": ["dark ocean tension", "cozy farming"], "dimensions": 768}
    assert headers == {"Authorization": "Bearer KEY"}
    # Order comes from "index" (the fake returns them reversed).
    expect = sem.normalize(fake_embedding("dark ocean tension", 3072)[:sem.DIM])
    assert sem.cosine(vecs[0], expect) > 0.999999
    assert run(sem.embed_texts(http, "KEY", MODEL, [])) == []
    assert run(sem.embed_texts(http, "", MODEL, ["x"])) is None


def test_embed_rate_limit_and_errors_return_none():
    assert run(sem.embed_texts(FakeHttp([(429, {"error": {"code": 429}})]), "K", MODEL, ["a"])) is None
    assert run(sem.embed_texts(FakeHttp([(500, None)]), "K", MODEL, ["a"])) is None
    assert run(sem.embed_texts(FakeHttp([(200, {"data": [{"index": 0, "embedding": [1.0] * 768}]})]),
                               "K", MODEL, ["a", "b"])) is None          # wrong count
    assert run(sem.embed_texts(FakeHttp([(200, {"data": [{"index": 0, "embedding": [1.0] * 10}]})]),
                               "K", MODEL, ["a"])) is None               # too short
    assert run(sem.embed_texts(FakeHttp([OSError("connection reset")]), "K", MODEL, ["a"])) is None
    assert run(sem.embed_texts(FakeHttp([asyncio.TimeoutError()]), "K", MODEL, ["a"])) is None


def test_dimensions_rejected_falls_back_to_truncation():
    sem._NO_DIMENSIONS.discard("old-model")
    http = FakeHttp(reject_dimensions=True)
    vecs = run(sem.embed_texts(http, "K", "old-model", ["a b c"]))
    assert vecs and len(vecs[0]) == sem.DIM
    assert "dimensions" in http.calls[0][1] and "dimensions" not in http.calls[1][1]
    run(sem.embed_texts(http, "K", "old-model", ["d"]))
    assert "dimensions" not in http.calls[2][1]       # remembered: one request from now on
    sem._NO_DIMENSIONS.discard("old-model")


def test_batches_over_limit():
    http = FakeHttp()
    vecs = run(sem.embed_texts(http, "K", MODEL, [f"text {i}" for i in range(sem.MAX_BATCH + 5)]))
    assert len(vecs) == sem.MAX_BATCH + 5 and len(http.calls) == 2


# --- schema and indexing

def test_schema():
    conn = db()
    sem.ensure_schema(conn)                    # idempotent
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"game_vectors", "review_snippets", "user_vectors"} <= tables
    cols = [r[1] for r in conn.execute("PRAGMA table_info(review_snippets)")]
    assert cols[:6] == ["appid", "idx", "text", "up", "hours", "lang"] and "vec" in cols
    pk = sorted((r[5], r[1]) for r in conn.execute("PRAGMA table_info(game_vectors)") if r[5])
    assert [c for _, c in pk] == ["appid", "kind"]


def test_index_game_and_replace():
    conn = db()
    http = FakeHttp()
    assert run(sem.index_game(http, "K", MODEL, conn, 264710, PASSPORT, SAMPLE)) is True
    assert len(http.calls) == 1                        # one request per game
    sent = http.calls[0][1]["input"]
    assert sent[0].startswith("title: none | text: Неторопливое")
    assert sem.is_indexed(conn, 264710)
    n = conn.execute("SELECT COUNT(*) FROM review_snippets WHERE appid=264710").fetchone()[0]
    assert n == len(sent) - 1 and 0 < n <= sem.MAX_SNIPPETS
    size = sum(len(r[0]) for r in conn.execute("SELECT vec FROM game_vectors"))
    size += sum(len(r[0]) + len(r[1].encode()) for r in conn.execute("SELECT vec, text FROM review_snippets"))
    assert size < 25_000, size
    # The player's words find the matching snippet.
    q = run(sem.embed_query(http, "K", MODEL, "tension when you dive deeper into the dark"))
    ev = sem.evidence(conn, q, 264710, k=1)
    assert "tension" in ev[0]["text"], ev
    assert sem.experience_score(conn, q, 264710) is not None
    # Re-indexing replaces snippets instead of piling them up.
    assert run(sem.index_game(http, "K", MODEL, conn, 264710, PASSPORT, SAMPLE[:1]))
    n2 = conn.execute("SELECT COUNT(*) FROM review_snippets WHERE appid=264710").fetchone()[0]
    assert n2 == 1, n2


def test_index_game_failures():
    conn = db()
    assert run(sem.index_game(FakeHttp([(429, {})]), "K", MODEL, conn, 1, PASSPORT, SAMPLE)) is False
    assert not sem.is_indexed(conn, 1)
    assert conn.execute("SELECT COUNT(*) FROM review_snippets").fetchone()[0] == 0
    assert run(sem.index_game(FakeHttp(), "K", MODEL, conn, 1, {}, [])) is False
    # A heuristic passport with no text still indexes the snippets.
    assert run(sem.index_game(FakeHttp(), "K", MODEL, conn, 2, {}, SAMPLE)) is True
    assert not sem.is_indexed(conn, 2)
    assert conn.execute("SELECT COUNT(*) FROM review_snippets WHERE appid=2").fetchone()[0] > 0


def test_taste_vector_from_games():
    conn = db()
    a, b, c = unit(seed=21), unit(seed=22), unit(seed=23)
    for appid, v in ((1, a), (2, b), (3, c)):
        conn.execute("INSERT INTO game_vectors(appid, kind, vec) VALUES(?, 'experience', ?)", (appid, sem.pack(v)))
    t = sem.taste_vector_from_games(conn, [(1, 1.5), (2, 1.0), (99, 1.0)])
    assert t and abs(math.sqrt(sum(x * x for x in t)) - 1) < 1e-9
    assert sem.cosine(t, a) > sem.cosine(t, b) > 0.4 and abs(sem.cosine(t, c)) < 0.15
    pushed = sem.taste_vector_from_games(conn, [(1, 1.0), (3, -0.5)])
    assert sem.cosine(pushed, c) < 0
    assert sem.taste_vector_from_games(conn, [(3, -1.0)]) is None    # only dislikes
    assert sem.taste_vector_from_games(conn, [(99, 1.0)]) is None    # nothing indexed
    assert sem.taste_vector_from_games(conn, []) is None


def test_user_vector_round_trip():
    conn = db()
    v = unit(seed=31)
    sem.set_user_vector(conn, 42, v, "люблю исследовать")
    back = sem.user_vector(conn, 42)
    assert sem.cosine(v, back) > 0.9999 and sem.user_vector(conn, 43) is None


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
