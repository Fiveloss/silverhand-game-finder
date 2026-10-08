"""Offline tests for gamefinder.sources.gog. Run: python tests_gog.py

A fake Http stands in for gamefinder.http.Http: get_json(url, params, **kw) answers with payloads in the
shapes documented at the top of gog.py, or raises like the real one (HttpError, timeouts, bad JSON)."""

import asyncio
import logging
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gamefinder.http import HttpError  # noqa: E402
from gamefinder.sources import gog as G  # noqa: E402
from gamefinder.sources.gog import Gog, base_title, normalize, title_match  # noqa: E402

SAMPLE_KEYS = {"text", "up", "hours", "date", "lang", "helpful", "weight", "source"}
LONG = ("The atmosphere is incredible and the exploration keeps pulling you deeper. Every area hides "
        "shortcuts and secrets, the bosses are hard but fair, and the music is beautiful. ")


class FakeHttp:
    """handler(url, params) -> payload, or an Exception instance to raise."""
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def get_json(self, url, params=None, *, headers=None, interval=None, retries=3, timeout=60):
        self.calls.append({"url": url, "params": dict(params or {}), "interval": interval,
                           "retries": retries, "timeout": timeout})
        r = self.handler(url, params or {})
        if isinstance(r, BaseException):
            raise r
        return r


def run(coro):
    return asyncio.run(coro)


def product(pid, title, ptype="game", count=1836, rating=42, slug=None, price="$9.99"):
    return {"id": str(pid), "slug": slug if slug is not None else title.lower().replace(" ", "_"), "title": title,
            "productType": ptype, "storeLink": f"https://www.gog.com/en/game/{pid}", "reviewsRating": rating,
            "reviewsCount": count,
            "price": {"final": price, "base": price, "discount": None,
                      "finalMoney": {"amount": price.strip("$"), "currency": "USD"}} if price else None}


def catalog(*products):
    return {"pages": 1, "productCount": len(products), "products": list(products)}


def item(rid, stars, text=LONG, title="Great game", lang="en-US", up=3, date="2026-09-28T11:01:00+03:00",
         verified=True):
    return {"id": rid, "productId": "1", "content": {"title": title, "description": text, "language": lang},
            "rating": {"value": stars}, "votes": {"upvotes": up, "downvotes": 0},
            "date": date, "creationDate": date, "labels": ["verified_owner"] if verified else [],
            "status": "published"}


def reviews_page(items, rating_count=500, avg=4.2, review_count=120):
    return {"page": 1, "limit": 60, "pages": 3, "reviewCount": review_count, "ratingCount": rating_count,
            "overallAvgRating": avg, "filteredAvgRating": avg, "mostHelpful": {},
            "_embedded": {"items": items}}


def find(products, title):
    h = FakeHttp(lambda url, p: catalog(*products))
    return run(Gog(h).find(title)), h


# --- titles

def test_normalize_and_base_title():
    assert normalize("S.T.A.L.K.E.R.: Shadow of Chernobyl") == "stalker shadow of chernobyl"
    assert normalize("Final Fantasy VII") == "final fantasy 7"
    assert normalize("The Witcher 3: Wild Hunt") == "witcher 3 wild hunt"
    assert normalize("Baldur's Gate 3") == "baldurs gate 3"
    assert normalize("Tom Clancy&#039;s Splinter Cell™") == "tom clancys splinter cell"
    assert normalize("Ori &amp; the Blind Forest") == "ori and the blind forest"
    assert normalize("") == "" and normalize(None) == ""
    assert base_title("DOOM (2016)") == "doom"
    assert base_title("Fallout: New Vegas Ultimate Edition") == "fallout new vegas"
    assert base_title("The Witcher 3: Wild Hunt - Game of the Year Edition") == "witcher 3 wild hunt"
    assert base_title("Disco Elysium - The Final Cut") == "disco elysium"


def test_title_match_sequels_and_editions():
    assert title_match("Alan Wake 2", "Alan Wake") == 0
    assert title_match("Alan Wake", "Alan Wake 2") == 0
    assert title_match("Alan Wake II", "Alan Wake 2") == 2
    assert title_match("Alan Wake", "Alan Wake Remastered") == 1
    assert title_match("Alan Wake 2", "Alan Wake 2 Deluxe Edition") == 1
    assert title_match("Hollow Knight", "Hollow Knight: Silksong") == 0
    assert title_match("Hollow Knight", "Hollow Knight") == 2
    assert title_match("Baldur's Gate", "Baldur's Gate 3") == 0
    assert title_match("Divinity: Original Sin", "Divinity: Original Sin 2 - Definitive Edition") == 0
    assert title_match("Divinity: Original Sin 2", "Divinity: Original Sin 2 - Definitive Edition") == 1
    assert title_match("The Witcher 3: Wild Hunt", "The Witcher 2: Assassins of Kings Enhanced Edition") == 0
    assert title_match("The Witcher 3: Wild Hunt", "The Witcher 3: Wild Hunt - Game of the Year Edition") == 1
    assert title_match("Cyberpunk 2077", "Cyberpunk 2077: Ultimate Edition") == 1
    assert title_match("Cyberpunk 2077", "Cyberpunk 2078") == 0
    assert title_match("DOOM", "DOOM (2016)") == 1
    assert title_match("The Elder Scrolls V: Skyrim", "The Elder Scrolls V: Skyrim Special Edition") == 1
    assert title_match("", "Hollow Knight") == 0 and title_match("Hollow Knight", "") == 0


# --- find

def test_find_strict_title_and_shape():
    got, h = find([product(1, "Hollow Knight: Silksong", count=900),
                   product(2, "Hollow Knight", count=1836, rating=47, slug="hollow_knight", price="$14.99")],
                  "Hollow Knight")
    assert got == {"id": "2", "title": "Hollow Knight", "slug": "hollow_knight",
                   "url": "https://www.gog.com/ru/game/hollow_knight", "rating": 4.7, "reviews": 1836,
                   "price": "$14.99"}, got
    call = h.calls[0]
    assert call["url"] == G.CATALOG
    assert call["params"]["query"] == "hollow knight" and call["params"]["locale"] == "en-US"
    assert call["params"]["countryCode"] == "KZ" and call["params"]["productType"] == "in:game,pack"
    assert call["retries"] == 1 and call["interval"] == G.INTERVAL
    # punctuation stripped from the query (GOG's search chokes on it)
    _, h = find([], "The Witcher 3: Wild Hunt")
    assert h.calls[0]["params"]["query"] == "witcher 3 wild hunt"


def test_find_sequel_numbers():
    got, _ = find([product(10, "Alan Wake 2"), product(11, "Alan Wake's American Nightmare")], "Alan Wake")
    assert got is None
    got, _ = find([product(10, "Alan Wake"), product(11, "Alan Wake Remastered")], "Alan Wake 2")
    assert got is None
    got, _ = find([product(10, "Alan Wake", count=50), product(11, "Alan Wake 2", count=10)], "Alan Wake 2")
    assert got["id"] == "11"
    # fuzzy search noise: unrelated games never match
    got, _ = find([product(20, "Cyberpunk 2077"), product(21, "Stardew Valley")], "Hollow Knight")
    assert got is None


def test_find_editions_and_packs():
    got, _ = find([product(30, "The Witcher 3: Wild Hunt - Game of the Year Edition", ptype="pack")],
                  "The Witcher 3: Wild Hunt")
    assert got and got["id"] == "30"
    # the exact title beats an edition, a "game" beats a "pack", then more reviews
    got, _ = find([product(31, "Disco Elysium - The Final Cut", count=9000),
                   product(32, "Disco Elysium", count=10)], "Disco Elysium")
    assert got["id"] == "32"
    got, _ = find([product(33, "Cyberpunk 2077", ptype="pack", count=99999),
                   product(34, "Cyberpunk 2077", ptype="game", count=5)], "Cyberpunk 2077")
    assert got["id"] == "34"
    got, _ = find([product(35, "Fallout: New Vegas Ultimate Edition", count=100),
                   product(36, "Fallout: New Vegas Ultimate Edition", count=900)], "Fallout: New Vegas")
    assert got["id"] == "36"


def test_find_skips_dlc_and_bad_products():
    got, _ = find([product(40, "Hollow Knight", ptype="dlc", count=99999),
                   {"title": "Hollow Knight", "productType": "game"},             # no id
                   "garbage", None,
                   product(41, "Hollow Knight", count=3)], "Hollow Knight")
    assert got["id"] == "41"
    got, _ = find([product(42, "Hollow Knight", ptype="dlc")], "Hollow Knight")
    assert got is None
    # unrated: rating and reviews None; no slug: storeLink; no price: ""
    got, _ = find([product(43, "Hollow Knight", count=0, rating=0, slug="", price=None)], "Hollow Knight")
    assert got["rating"] is None and got["reviews"] is None and got["price"] == ""
    assert got["url"] == "https://www.gog.com/en/game/43"


def test_find_errors_never_raise():
    for err in (HttpError(504, G.CATALOG), HttpError(404, G.CATALOG), asyncio.TimeoutError(), OSError("reset"),
                ValueError("Expecting value: line 1 column 1"), RuntimeError("boom")):
        h = FakeHttp(lambda url, p, err=err: err)
        assert run(Gog(h).find("Hollow Knight")) is None, err
    for payload in (None, [], "<html>504 overcapacity</html>", 42, {}, {"products": None},
                    {"products": {"0": product(1, "Hollow Knight")}}, {"products": "x"}):
        h = FakeHttp(lambda url, p, payload=payload: payload)
        assert run(Gog(h).find("Hollow Knight")) is None, payload
    h = FakeHttp(lambda url, p: catalog(product(1, "x")))
    assert run(Gog(h).find("")) is None and run(Gog(h).find("™")) is None
    assert h.calls == []                                   # nothing to search for: no request


def test_find_garbage_fields_never_raise():
    bad = [
        ("reviewsCount '1,836'", dict(product(1, "Hollow Knight"), reviewsCount="1,836")),
        ("reviewsRating '42'", dict(product(1, "Hollow Knight"), reviewsRating="42")),
        ("price as a string", dict(product(1, "Hollow Knight"), price="$9.99")),
        ("title as a number", dict(product(1, "Hollow Knight"), title=123)),
    ]
    raised = []
    for name, p in bad:
        h = FakeHttp(lambda url, params, p=p: catalog(p))
        try:
            run(Gog(h).find("Hollow Knight"))
        except Exception as e:  # noqa: BLE001
            raised.append(f"{name}: {type(e).__name__}: {e}")
    assert not raised, raised


# --- reviews

def reviews_http(votes_items, date_items=None, **page):
    def handler(url, params):
        assert url == G.REVIEWS.format(id="1207659037"), url
        items = votes_items if params["order"] == "desc:votes" else (date_items if date_items is not None
                                                                     else votes_items)
        return reviews_page(items, **page)
    return FakeHttp(handler)


def test_reviews_sample_shape_and_summary():
    helpful = [item("a", 5, up=120, date="2026-09-01T10:00:00+00:00"),
               item("b", 1, text="Too hard &amp; unfair, I quit after the third boss. " * 4, title="Nope",
                    up=40, date="2026-08-01T10:00:00+00:00"),
               item("c", 4, text="Тёмная &quot;атмосфера&quot; и музыка, которые не отпускают. " * 3,
                    title="", lang="ru-RU", up=0, verified=False, date="2026-09-20T10:00:00+03:00")]
    recent = [item("d", 2, text="Boring after ten hours, the map is confusing and backtracking is endless. " * 2,
                   up=0, date="2026-10-01T10:00:00+00:00"),
              item("a", 5, up=120, date="2026-09-01T10:00:00+00:00"),                 # also in the top page
              item("e", 5, text="short", up=9),                                           # too short: dropped
              item("f", 3, text="Fine. " * 40, title="Fine", date="2026-09-30T00:00:00+00:00")]
    h = reviews_http(helpful, recent, rating_count=1836, avg=4.734, review_count=310)
    summary, sample = run(Gog(h).reviews("1207659037", limit=60))
    assert summary == {"avg": 4.73, "count": 1836, "share_positive": round(2 / 4, 3), "text_count": 310,
                       "sampled": 4}, summary
    assert len(h.calls) == 2 and {c["params"]["order"] for c in h.calls} == {"desc:votes", "desc:date"}
    assert all(c["params"]["limit"] == 60 and c["params"]["language"] == "in:en-US,ru-RU" for c in h.calls)
    ids = {s["text"][:20] for s in sample}
    assert len(sample) == 5, [s["text"][:30] for s in sample]                    # a,b,c,d,f; e too short, a once
    assert len(ids) == 5
    for s in sample:
        assert SAMPLE_KEYS <= set(s), set(s)
        assert s["hours"] is None and s["source"] == "gog"
        assert isinstance(s["up"], bool) and isinstance(s["helpful"], int) and s["weight"] > 0
        assert len(s["text"]) >= G.MIN_TEXT and len(s["text"]) <= G.MAX_TEXT
        assert "&amp;" not in s["text"] and "&quot;" not in s["text"]
        assert len(s["date"]) == 10 and s["date"][4] == "-"
        assert s["lang"] in ("english", "russian")
    assert [s["date"] for s in sample] == sorted((s["date"] for s in sample), reverse=True)   # newest first
    by = {s["text"][:12]: s for s in sample}
    neg = next(s for s in sample if s["text"].startswith("Nope"))
    assert neg["up"] is False and "Too hard & unfair" in neg["text"] and neg["helpful"] == 40
    ru = next(s for s in sample if s["lang"] == "russian")
    assert ru["up"] is True and 'Тёмная "атмосфера"' in ru["text"] and ru["date"] == "2026-09-20"
    three = next(s for s in sample if s["text"].startswith("Fine"))
    assert three["up"] is False                                          # 3 stars is not positive
    assert by and all(s["text"].count("Great game. ") <= 1 for s in sample)


def test_reviews_text_cleanup_and_limit():
    body = "<b>Loved</b> it&nbsp;&mdash; see https://example.com/x<br/>second line &lt;3 " + "x" * 150
    s = G.to_sample(item("z", 5, text=body, title="Loved it"), time.time())
    assert s["text"].startswith("Loved it") and "<b>" not in s["text"] and "https://" not in s["text"]
    assert "—" in s["text"] and "<3" in s["text"] and "\n" in s["text"]
    assert G.to_sample(item("z", 5, text="x" * 5000), time.time())["text"] == ("Great game. " + "x" * 5000)[:1400]
    assert G.to_sample(item("z", 0), time.time()) is None and G.to_sample(item("z", 6), time.time()) is None
    assert G.to_sample(item("z", 5, text="ok", title=""), time.time()) is None
    # limit: never more than asked, negatives over-sampled
    many = [item(f"p{i}", 5, title=f"Positive #{i}", up=i) for i in range(40)] + \
           [item(f"n{i}", 1, title=f"Negative #{i}", up=i) for i in range(10)]
    h = reviews_http(many, [])
    _, sample = run(Gog(h).reviews("1207659037", limit=20))
    assert len(sample) == 20
    assert sum(not s["up"] for s in sample) == 7, sum(not s["up"] for s in sample)     # round(20 * 0.35)
    assert h.calls[0]["params"]["limit"] == 20
    h = reviews_http(many, [])
    run(Gog(h).reviews("1207659037", limit=500))
    assert h.calls[0]["params"]["limit"] == G.MAX_PAGE


def test_reviews_unknown_product_and_errors():
    # an unknown id answers 200 with zeros
    h = FakeHttp(lambda url, p: {"page": 1, "limit": 60, "pages": 0, "reviewCount": 0, "ratingCount": 0,
                                 "overallAvgRating": 0, "filteredAvgRating": 0, "_embedded": {"items": []}})
    assert run(Gog(h).reviews("1")) == ({}, [])
    for err in (HttpError(504, "x"), asyncio.TimeoutError(), OSError("reset"), ValueError("not JSON"),
                RuntimeError("boom")):
        h = FakeHttp(lambda url, p, err=err: err)
        assert run(Gog(h).reviews("1")) == ({}, []), err
    for payload in (None, [], "<html>504</html>", {}, {"ratingCount": None}, {"_embedded": None},
                    {"ratingCount": 5, "_embedded": []}, {"ratingCount": 5, "_embedded": {"items": {"a": 1}}},
                    {"ratingCount": 5, "_embedded": {"items": [None, "x", 3, {"content": "x"}]}}):
        h = FakeHttp(lambda url, p, payload=payload: payload)
        summary, sample = run(Gog(h).reviews("1"))
        assert sample == [] and (summary == {} or summary["sampled"] == 0), (payload, summary)
    # one page fails, the other works: still a summary and a sample
    def half(url, p):
        if p["order"] == "desc:votes":
            return HttpError(504, url)
        return reviews_page([item("a", 5, title="One"), item("b", 4, title="Two")], rating_count=7)
    summary, sample = run(Gog(FakeHttp(half)).reviews("1"))
    assert summary["count"] == 7 and summary["share_positive"] == 1.0 and len(sample) == 2


def test_reviews_garbage_fields_never_raise():
    bad = [
        ("ratingCount 'many'", reviews_page([item("a", 5)], rating_count="many")),
        ("overallAvgRating 'n/a'", reviews_page([item("a", 5)], avg="n/a")),
        ("votes.upvotes 'x'", reviews_page([dict(item("a", 5), votes={"upvotes": "x"})])),
        ("votes as a list", reviews_page([dict(item("a", 5), votes=[1, 0])])),
        ("date as a unix number", reviews_page([dict(item("a", 5), date=1700000000)])),
        ("id as a list", reviews_page([dict(item("a", 5), id=["a"])])),
    ]
    raised = []
    for name, payload in bad:
        h = FakeHttp(lambda url, p, payload=payload: payload)
        try:
            run(Gog(h).reviews("1"))
        except Exception as e:  # noqa: BLE001
            raised.append(f"{name}: {type(e).__name__}: {e}")
    assert not raised, raised


def test_review_weight_and_dates():
    now = time.time()
    assert G.parse_date("2026-09-28T11:01:00+03:00")[0] == "2026-09-28"
    assert G.parse_date("garbage") == ("garbage", None) and G.parse_date("") == ("", None)
    fresh = G.review_weight(100, now - 86400, True, now)
    old = G.review_weight(100, now - 3 * 365 * 86400, True, now)
    unverified = G.review_weight(100, now - 86400, False, now)
    assert fresh > old and fresh > unverified and G.review_weight(0, None, True, now) == 2.0


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
    logging.basicConfig(level=logging.CRITICAL)
    main()
