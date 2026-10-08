"""GOG.com: a second store whose user reviews are added to Steam's.

Public endpoints only, no key. Shapes as observed in October 2026:

catalog.gog.com/v1/catalog?query=..&limit=..&countryCode=KZ&locale=en-US&productType=in:game,pack
    {"pages", "productCount", "products": [{"id": "1207659037" (str), "slug", "title",
     "productType": "game" | "pack" | "dlc", "storeLink", "reviewsRating": 42 (= 4.2 of 5, 0 when
     unrated), "reviewsCount": 1836, "price": {"final": "$0.85", "base", "discount",
     "finalMoney": {"amount", "currency"}} | null, ...}]}
    The search is fuzzy: with no real hit it still returns unrelated games, so the title is
    matched strictly here. Punctuation hurts it ("The Witcher 3: Wild Hunt" finds nothing, "the
    witcher 3 wild hunt" works), so the query is sent normalised. locale=ru-RU translates titles
    ("Ведьмак 3: ..."), so the search stays in English. Some base games are "pack" products
    (Cyberpunk 2077, S.T.A.L.K.E.R., the Witcher 3 Remastered).

reviews.gog.com/v1/products/<id>/reviews?language=in:en-US,ru-RU&limit=60&order=desc:votes|desc:date&page=1
    {"page", "limit", "pages", "reviewCount": text reviews, "ratingCount": all star ratings,
     "overallAvgRating": 4.2, "filteredAvgRating": avg for the language filter, "mostHelpful": {..},
     "_embedded": {"items": [{"id": uuid, "productId", "content": {"title", "description",
     "language": "en-US"}, "rating": {"value": 1..5}, "votes": {"upvotes", "downvotes"},
     "date": last edit ISO, "creationDate": ISO, "labels": ["verified_owner"], "status", ...}]}}
    There is no star distribution, so share_positive is estimated from the newest page.
    Descriptions carry HTML entities (&amp;). limit=200 on a big product times out (HTML 504
    "overcapacity" page), so pages are capped at 100. An unknown id answers 200 with zeros.

Rates: no limit headers and no 429 in a burst of 20 requests; responses are cached by Varnish
for 5 minutes, an uncached review page of 60 takes 3-5 s. One request a second per host.
"""

import html
import logging
import math
import re
import time
from datetime import datetime

log = logging.getLogger(__name__)

CATALOG = "https://catalog.gog.com/v1/catalog"
REVIEWS = "https://reviews.gog.com/v1/products/{id}/reviews"
STORE = "https://www.gog.com/ru/game/{slug}"

INTERVAL = 1.0
TIMEOUT = 25
MAX_PAGE = 100
MIN_TEXT = 120          # the same thresholds as reviews.py (not imported: reviews.py may import us)
MAX_TEXT = 1400
LANGS = {"en-US": "english", "ru-RU": "russian"}

_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9",
          "x": "10", "xi": "11", "xii": "12", "xiii": "13"}
# Trailing words that name an edition of the same game, not a different one.
_EDITION = re.compile(
    r"(?:\s+(?:the\s+)?(?:game of the year|goty|definitive|enhanced|ultimate|complete|deluxe|digital deluxe"
    r"|gold|special|anniversary|collectors|premium|standard|legendary|royal|divine|eternal)?\s*edition"
    r"|\s+(?:the\s+)?(?:final cut|directors cut)|\s+remastered|\s+goty|\s+complete)$")


def normalize(title: str) -> str:
    """Lowercase words and digits for comparison: 'S.T.A.L.K.E.R.: Shadow of Chernobyl' ->
    'stalker shadow of chernobyl', 'Final Fantasy VII' -> 'final fantasy 7'."""
    t = html.unescape(title or "").lower()
    t = re.sub(r"[™®©]", "", t)
    t = t.replace("&", " and ")
    t = re.sub(r"['’`.]", "", t)                    # Baldur's, S.T.A.L.K.E.R.
    t = re.sub(r"[^\w]+", " ", t).replace("_", " ")
    words = [_ROMAN.get(w, w) for w in t.split()]
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def base_title(title: str) -> str:
    """normalize() without edition suffixes and a parenthesised year: 'DOOM (2016)' -> 'doom',
    'Fallout: New Vegas Ultimate Edition' -> 'fallout new vegas'."""
    t = re.sub(r"\(\s*(?:19|20)\d\d\s*\)", " ", title or "")
    t = normalize(t)
    while True:
        s = _EDITION.sub("", t).strip()
        if s == t or not s:
            return t
        t = s


def _digits(t: str) -> list[str]:
    return re.findall(r"\d+", t)


def title_match(wanted: str, candidate: str) -> int:
    """0 = different games; 2 = same title; 1 = same game in another edition. Sequel numbers must
    agree, so 'Alan Wake 2' never matches 'Alan Wake' and 'Hollow Knight' never 'Hollow Knight: Silksong'."""
    a, b = normalize(wanted), normalize(candidate)
    if not a or not b:
        return 0
    if a == b:
        return 2
    ba, bb = base_title(wanted), base_title(candidate)
    if ba and ba == bb and _digits(ba) == _digits(bb):
        return 1
    return 0


def _int(v) -> int:
    try:
        return int(str(v).replace(",", "").replace(" ", "")) if v not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def _float(v) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def parse_date(value) -> tuple[str, float | None]:
    """('2026-09-28', unix time) from GOG's '2026-09-28T11:01:00+03:00'."""
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%Y-%m-%d"), dt.timestamp()
    except (TypeError, ValueError):
        return (value if isinstance(value, str) else "")[:10], None


def review_text(content: dict) -> str:
    title = html.unescape((content.get("title") or "").strip())
    body = html.unescape(content.get("description") or "")
    body = re.sub(r"<br\s*/?>", "\n", body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"https?://\S+", "", body)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n\s*\n+", "\n", body).strip()
    if title and not body.lower().startswith(title.lower()):
        body = f"{title.rstrip('.!?')}. {body}" if body else title
    return body


def review_weight(helpful: int, ts: float | None, verified: bool, now: float) -> float:
    """Like reviews.review_weight, with play time unknown: a flat +1 (what 25 hours earn on Steam)."""
    age_days = (now - ts) / 86400 if ts else 365
    w = 1.0 + 1.0
    w += math.log1p(max(helpful, 0)) / 2
    w += max(0.0, 1 - age_days / 365)
    return w * (1.0 if verified else 0.8)


def to_sample(item: dict, now: float) -> dict | None:
    """A GOG review in the shape pick_for_analysis produces, or None when unusable."""
    try:
        rating = int((item.get("rating") or {}).get("value") or 0)
        content = item.get("content") or {}
        text = review_text(content)
    except (TypeError, ValueError, AttributeError):
        return None
    if not 1 <= rating <= 5 or len(text) < MIN_TEXT:
        return None
    date, ts = parse_date(item.get("date") or item.get("creationDate") or "")
    votes = item.get("votes")
    helpful = _int(votes.get("upvotes")) if isinstance(votes, dict) else 0
    lang = content.get("language") if isinstance(content.get("language"), str) else ""
    return {
        "text": text[:MAX_TEXT],
        "up": rating >= 4,
        "hours": None,
        "date": date,
        "lang": LANGS.get(lang, lang),
        "helpful": helpful,
        "weight": review_weight(helpful, ts, "verified_owner" in (item.get("labels") or []), now),
        "rating": rating,
        "source": "gog",
    }


class Gog:
    def __init__(self, http, cc: str = "KZ"):
        self.http = http
        self.cc = (cc or "KZ").upper()

    async def _get(self, url: str, params: dict) -> dict | None:
        try:
            data = await self.http.get_json(url, params, interval=INTERVAL, retries=1, timeout=TIMEOUT)
        except Exception as e:      # HttpError, network, HTML error pages that fail to parse
            log.warning("GOG request failed: %s %s: %s", url, params.get("query", ""), e)
            return None
        return data if isinstance(data, dict) else None

    async def find(self, title: str) -> dict | None:
        """The GOG product for a game title, or None unless the title surely is the same game."""
        query = normalize(title)
        if not query:
            return None
        data = await self._get(CATALOG, {"query": query, "limit": 10, "countryCode": self.cc,
                                         "locale": "en-US", "productType": "in:game,pack"})
        best, best_key = None, None
        for p in (data or {}).get("products") or []:
            if not isinstance(p, dict) or p.get("productType") == "dlc" or not p.get("id"):
                continue
            score = title_match(title, str(p.get("title") or ""))
            if not score:
                continue
            key = (score, p.get("productType") == "game", _int(p.get("reviewsCount")))
            if best_key is None or key > best_key:
                best, best_key = p, key
        if not best:
            return None
        count = _int(best.get("reviewsCount"))
        rating = _float(best.get("reviewsRating"))
        slug = best.get("slug") or ""
        return {
            "id": str(best["id"]),
            "title": best.get("title") or "",
            "slug": slug,
            "url": STORE.format(slug=slug) if slug else best.get("storeLink") or "",
            "rating": round(rating / 10, 1) if count and rating else None,
            "reviews": count or None,
            "price": (best["price"].get("final") or "") if isinstance(best.get("price"), dict) else "",
        }

    async def _page(self, product_id, order: str, limit: int) -> tuple[dict, list[dict]]:
        data = await self._get(REVIEWS.format(id=product_id),
                               {"language": "in:en-US,ru-RU", "limit": limit, "order": order, "page": 1})
        if not data:
            return {}, []
        items = ((data.get("_embedded") or {}).get("items")) or []
        return data, [i for i in items if isinstance(i, dict)]

    async def reviews(self, product_id, limit: int = 60) -> tuple[dict, list[dict]]:
        """(summary, sample): summary {"avg": 0..5, "count": star ratings, "share_positive": share of
        4-5 stars among the newest reviews, "text_count", "sampled"} or {} when GOG has nothing;
        sample: up to `limit` reviews in the reviews.py sample shape (hours None, source "gog"),
        negatives over-sampled like pick_for_analysis, newest first."""
        per_page = max(1, min(int(limit or 60), MAX_PAGE))
        head, helpful = await self._page(product_id, "desc:votes", per_page)
        head2, recent = await self._page(product_id, "desc:date", per_page)
        head = head or head2
        count = _int(head.get("ratingCount")) if head else 0
        if not count:
            return {}, []

        def rating(i):
            try:
                return int((i.get("rating") or {}).get("value") or 0)
            except (TypeError, ValueError, AttributeError):
                return 0

        rated = [r for r in map(rating, recent or helpful) if 1 <= r <= 5]
        summary = {
            "avg": round(_float(head.get("overallAvgRating")), 2),
            "count": count,
            "share_positive": round(sum(r >= 4 for r in rated) / len(rated), 3) if rated else None,
            "text_count": _int(head.get("reviewCount")),
            "sampled": len(rated),
        }

        now = time.time()
        seen_ids, seen_text, pool = set(), set(), []
        for item in helpful + recent:
            rid = str(item.get("id") or "")
            if rid and rid in seen_ids:
                continue
            seen_ids.add(rid)
            try:
                s = to_sample(item, now)
            except Exception:       # a malformed review: skip it, keep the others
                s = None
            if not s or s["text"][:80].lower() in seen_text:
                continue
            seen_text.add(s["text"][:80].lower())
            pool.append(s)
        pos = sorted((p for p in pool if p["up"]), key=lambda p: -p["weight"])
        neg = sorted((p for p in pool if not p["up"]), key=lambda p: -p["weight"])
        n_neg = min(len(neg), max(round(limit * 0.35), limit - len(pos)))
        chosen = neg[:n_neg] + pos[:limit - n_neg]
        chosen.sort(key=lambda p: p["date"], reverse=True)
        return summary, chosen
