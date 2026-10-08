"""Offline tests for gamefinder.sources.reddit. Run: python tests_reddit.py"""

import asyncio
import logging
import time

from gamefinder.http import HttpError
from gamefinder.sources.reddit import Reddit, clean, normalize

NOW = int(time.time())
OLD = NOW - 4 * 365 * 86400
LONG = ("I sank about sixty hours into Hollow Knight and the combat never got old. The map "
        "design rewards curiosity and the bosses are tough but fair, worth every cent. ")


def post(id_, title, sub, score=500, comments=200, selftext="", created=NOW - 86400, **kw):
    d = {"id": id_, "title": title, "subreddit": sub, "score": score, "num_comments": comments,
         "selftext": selftext, "created_utc": float(created), "author": "someone",
         "permalink": f"/r/{sub}/comments/{id_}/slug/", "over_18": False}
    d.update(kw)
    return {"kind": "t3", "data": d}


def listing(children):
    return {"kind": "Listing", "data": {"after": None, "children": children}}


def comment(id_, body, score=10, author="player", created=NOW - 3600, sub="patientgamers", **kw):
    d = {"id": id_, "body": body, "score": score, "author": author, "created_utc": float(created),
         "subreddit": sub, "permalink": f"/r/{sub}/comments/t1/slug/{id_}/", "depth": 0}
    d.update(kw)
    return {"kind": "t1", "data": d}


SEARCH_SUBS = listing([
    post("t1", "Hollow Knight review after 60 hours - is it worth it?", "patientgamers",
         selftext=LONG + " Thread body here with [a link](https://example.com/x) included."),
    post("t2", "Silksong delayed again", "gaming"),  # title lacks the name -> ignored
])
SEARCH_ALL = listing([
    post("t1", "Hollow Knight review after 60 hours - is it worth it?", "patientgamers"),
    post("t3", "Should I buy Hollow Knight on sale?", "HollowKnight", score=50, comments=80),
    post("t4", "Hollow Knight: thoughts?", "nsfwstuff", over_18=True),
])
COMMENTS_T1 = [
    listing([post("t1", "Hollow Knight review", "patientgamers")]),
    listing([
        comment("c1", "> quoted text that should go\n\n**Absolutely** " + LONG + "\n\nSee [wiki](https://hk.wiki/x)", score=900),
        comment("c2", "[deleted]", score=5),
        comment("c3", "[removed]", score=5),
        comment("c4", LONG + " auto", author="AutoModerator", score=1),
        comment("c5", LONG + " bot", author="RemindMeBot", score=1),
        comment("c6", "Too short to matter.", score=300),
        comment("c7", "https://youtu.be/abc https://imgur.com/def " * 5, score=200),
        comment("c8", LONG, score=50),  # duplicate text of c1 core -> distinct (c1 has 'Absolutely')
        comment("c9", "Old opinion: " + LONG, score=5000, created=OLD),
        comment("c10", "x" * 5000, score=3),
        {"kind": "more", "data": {"count": 12, "children": ["a", "b"]}},
    ]),
]
COMMENTS_T3 = [
    listing([post("t3", "Should I buy Hollow Knight", "HollowKnight")]),
    listing([
        comment("d1", LONG, score=40, sub="HollowKnight"),  # duplicate of c8 -> deduped
        comment("d2", "Mod note " + LONG, distinguished="moderator", sub="HollowKnight"),
    ]),
]


class FakeHttp:
    def __init__(self, routes, token_expires=3600, fail=None):
        self.routes = routes
        self.fail = fail or {}
        self.gets, self.posts = [], []
        self.token_expires = token_expires

    async def post_form(self, url, data, *, auth=None):
        self.posts.append((url, data, auth))
        assert url == "https://www.reddit.com/api/v1/access_token"
        assert data == {"grant_type": "client_credentials"}
        assert auth.login == "id" and auth.password == "secret"
        return {"access_token": f"tok{len(self.posts)}", "token_type": "bearer",
                "expires_in": self.token_expires, "scope": "*"}

    async def get_json(self, url, params=None, *, headers=None, interval=None, retries=3, timeout=60):
        self.gets.append((url, dict(params or {}), dict(headers or {})))
        for key, exc in self.fail.items():
            if key in url:
                raise exc
        path = url.replace("https://oauth.reddit.com", "")
        if path.endswith("/search"):
            key = ("search_subs" if path.startswith("/r/") else "search_" + params["t"])
        else:
            key = path
        return self.routes.get(key, listing([]))


ROUTES = {"search_subs": SEARCH_SUBS, "search_year": listing([]), "search_all": SEARCH_ALL,
          "/comments/t1": COMMENTS_T1, "/comments/t3": COMMENTS_T3}


def run(coro):
    return asyncio.run(coro)


def test_disabled():
    http = FakeHttp(ROUTES)
    r = Reddit(http, "", "secret")
    assert not r.enabled
    assert run(r.discussion("Hollow Knight")) == []
    assert not http.gets and not http.posts
    assert Reddit(http, "id", "secret").enabled


def test_clean():
    assert clean("> quote\nreal [text](http://a.b/c) here https://x.y/z  \n\n **bold**") == "real text here bold"
    assert clean("Tom &amp; Jerry") == "Tom & Jerry"
    long = clean("word " * 1000)
    assert len(long) <= 1501 and long.endswith("…")
    assert normalize("Hollow Knight™: Silksong!") == "hollow knight silksong"


def test_discussion_filters_and_shapes():
    http = FakeHttp(ROUTES)
    r = Reddit(http, "id", "secret")
    out = run(r.discussion("Hollow Knight"))
    texts = [o["text"] for o in out]

    # request shape
    assert len(http.posts) == 1
    assert len(http.posts) + len(http.gets) <= 8
    for url, params, headers in http.gets:
        assert url.startswith("https://oauth.reddit.com/")
        assert headers["Authorization"] == "bearer tok1"
    sub_search = http.gets[0]
    assert "patientgamers" in sub_search[0] and sub_search[1]["restrict_sr"] == 1
    assert sub_search[1]["q"].startswith('"Hollow Knight"') and sub_search[1]["t"] == "year"
    assert any(p.get("t") == "all" for _, p, _ in http.gets), "falls back to t=all"
    comment_calls = [g for g in http.gets if "/comments/" in g[0]]
    assert {g[0].rsplit("/", 1)[1] for g in comment_calls} == {"t1", "t3"}  # t2/t4 filtered
    assert comment_calls[0][1]["sort"] == "top" and comment_calls[0][1]["depth"] == 1

    # dict shape
    for o in out:
        assert set(o) == {"source", "text", "score", "created", "url", "subreddit"}
        assert o["source"] == "reddit" and isinstance(o["score"], int) and isinstance(o["created"], int)
        assert o["url"].startswith("https://www.reddit.com/r/")
        assert 120 <= len(o["text"]) <= 1501
        assert "http" not in o["text"] and "](" not in o["text"] and "**" not in o["text"]
        assert "quoted text" not in o["text"]

    # filtering
    assert not any("auto" in t[-6:] or "bot" in t[-5:] for t in texts)
    assert not any(t.startswith("Mod note") for t in texts)
    assert not any("[deleted]" in t or "[removed]" in t for t in texts)
    assert not any("Too short" in t for t in texts)
    assert sum(1 for t in texts if t == clean(LONG)) == 1, "deduped"
    assert any(t.startswith("Hollow Knight review after 60 hours") for t in texts), "selftext kept"
    assert any(t.startswith("x" * 100) for t in texts), "long comment truncated, not dropped"

    # ordering: recent first by score, the old high-score one last
    assert out[0]["text"].startswith("Absolutely")
    assert out[-1]["text"].startswith("Old opinion")
    recent = [o["score"] for o in out if o["created"] > OLD + 1]
    assert recent == sorted(recent, reverse=True)

    # limit
    assert len(run(r.discussion("Hollow Knight", limit=2))) == 2


def test_token_cached_and_refreshed():
    http = FakeHttp(ROUTES)
    r = Reddit(http, "id", "secret")
    run(r.discussion("Hollow Knight"))
    run(r.discussion("Hollow Knight"))
    assert len(http.posts) == 1, "token reused while valid"
    r._token_until = time.time() - 1  # expire it
    run(r.discussion("Hollow Knight"))
    assert len(http.posts) == 2
    assert http.gets[-1][2]["Authorization"] == "bearer tok2"


def test_budget():
    many = listing([post(f"p{i}", f"Hollow Knight thoughts {i}", "Games") for i in range(20)])
    http = FakeHttp({"search_subs": many, "search_year": many})
    out = run(Reddit(http, "id", "secret").discussion("Hollow Knight"))
    assert out == [] or isinstance(out, list)
    assert len(http.posts) + len(http.gets) <= 8
    assert not any(p.get("t") == "all" for _, p, _ in http.gets), "no fallback when enough threads"


def test_never_raises():
    # comments of one thread fail -> others still returned
    http = FakeHttp(ROUTES, fail={"/comments/t3": HttpError(500, "x")})
    out = run(Reddit(http, "id", "secret").discussion("Hollow Knight"))
    assert out and all(o["subreddit"] == "patientgamers" for o in out)

    # every call fails with a connection error
    http = FakeHttp(ROUTES, fail={"oauth.reddit.com": OSError("boom")})
    assert run(Reddit(http, "id", "secret").discussion("Hollow Knight")) == []

    # token endpoint rejects the credentials
    class BadAuth(FakeHttp):
        async def post_form(self, url, data, *, auth=None):
            raise HttpError(401, url)
    assert run(Reddit(BadAuth(ROUTES), "id", "secret").discussion("Hollow Knight")) == []

    # 401 on API drops the cached token
    http = FakeHttp(ROUTES, fail={"/search": HttpError(401, "x")})
    r = Reddit(http, "id", "secret")
    assert run(r.discussion("Hollow Knight")) == []
    assert r._token is None


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} tests passed")
