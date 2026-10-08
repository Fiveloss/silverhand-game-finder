"""Optional second source: what players say about a game in Reddit discussions.

Reddit blocks anonymous JSON, so this needs a "script"/"web" app's client id and secret
(app-only OAuth, client_credentials grant). Without them `discussion()` returns [].
Each `discussion()` call makes at most MAX_CALLS HTTP requests (token included).
"""

import asyncio
import html
import logging
import re
import time

import aiohttp

from gamefinder.http import Http, HttpError

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"
WEB = "https://www.reddit.com"

GAMING_SUBS = ["gaming", "patientgamers", "truegaming", "pcgaming", "Games", "gamingsuggestions"]
OPINION_TERMS = '(review OR worth OR impressions OR opinion OR thoughts OR "should I")'
OPINION_TITLE = re.compile(r"\b(review|worth|impressions?|opinions?|thoughts|should i|finished|"
                           r"recommend|verdict|hours in|beat)\b", re.I)

MAX_CALLS = 8
MAX_THREADS = 4
MIN_THREADS = 5
MIN_CHARS = 120
MAX_CHARS = 1500
TWO_YEARS = 2 * 365 * 86400
BOT_NAMES = {"automoderator", "[deleted]", "remindmebot", "sneakpeekbot", "wikitextbot"}

_LINK = re.compile(r"!?\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_QUOTE = re.compile(r"^\s*>.*$", re.M)
_FORMAT = re.compile(r"(\*\*|__|~~|>!|!<|`+|^#+\s*|^\s*[-*+]\s+|\^)", re.M)
_SPACE = re.compile(r"\s+")


def normalize(s: str) -> str:
    """Lowercase, drop trademark signs and punctuation, collapse whitespace."""
    s = re.sub(r"[™®©]", "", s.lower())
    s = re.sub(r"[^\w\s]", " ", s)
    return _SPACE.sub(" ", s).strip()


def clean(text: str) -> str:
    """Markdown/HTML body -> plain single-line text (quotes, links and URLs removed)."""
    text = html.unescape(text or "")
    text = _QUOTE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    text = _FORMAT.sub("", text)
    text = text.replace("&#x200B;", " ").replace("​", " ")
    text = _SPACE.sub(" ", text).strip()
    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS]
        sp = cut.rfind(" ")
        text = (cut[:sp] if sp > MAX_CHARS - 200 else cut).rstrip() + "…"
    return text


def is_bot(author: str | None, data: dict) -> bool:
    a = (author or "").lower()
    return (not a or a in BOT_NAMES or a.endswith("bot") or a.endswith("_bot")
            or data.get("distinguished") == "moderator" or bool(data.get("stickied")))


def _removed(body: str | None) -> bool:
    b = (body or "").strip().lower()
    return not b or b in ("[deleted]", "[removed]") or b.startswith("[removed by")


class Reddit:
    def __init__(self, http: Http, client_id: str, client_secret: str):
        self.http = http
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self._token: str | None = None
        self._token_until = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # ---- auth -------------------------------------------------------------------------

    async def _get_token(self) -> str | None:
        """Returns a cached app-only token, fetching a new one when (nearly) expired."""
        async with self._token_lock:
            if self._token and time.time() < self._token_until:
                return self._token
            data = await self.http.post_form(
                TOKEN_URL, {"grant_type": "client_credentials"},
                auth=aiohttp.BasicAuth(self.client_id, self.client_secret))
            token = (data or {}).get("access_token")
            if not token:
                raise HttpError(401, TOKEN_URL)
            expires = float(data.get("expires_in") or 3600)
            self._token = token
            self._token_until = time.time() + max(60.0, expires - 60)
            return token

    # ---- public -----------------------------------------------------------------------

    async def discussion(self, game_name: str, limit: int = 40) -> list[dict]:
        if not self.enabled or not (game_name or "").strip():
            return []
        run = _Run(self, game_name.strip())
        try:
            await run.collect()
        except HttpError as e:
            if e.status in (401, 403):
                self._token = None  # bad/expired token or credentials: refetch next time
            log.warning("reddit: %s while collecting %r", e, game_name)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as e:
            log.warning("reddit: %s: %s while collecting %r", type(e).__name__, e, game_name)
        except Exception:  # never let an optional source break the bot
            log.warning("reddit: unexpected error for %r", game_name, exc_info=True)
        return run.result(limit)


class _Run:
    """State of one discussion() call: request budget, threads seen, opinions collected."""

    def __init__(self, reddit: Reddit, name: str):
        self.r = reddit
        self.name = name
        self.norm = normalize(name)
        self.calls = 0
        self.threads: dict[str, dict] = {}
        self.items: list[dict] = []

    async def _api(self, path: str, params: dict):
        if self.calls >= MAX_CALLS:
            return None
        had_token = bool(self.r._token and time.time() < self.r._token_until)
        if not had_token:
            self.calls += 1
        token = await self.r._get_token()
        if self.calls >= MAX_CALLS:
            return None
        self.calls += 1
        return await self.r.http.get_json(
            API + path, {**params, "raw_json": 1},
            headers={"Authorization": f"bearer {token}"}, retries=1, timeout=30)

    def _title_matches(self, title: str) -> bool:
        return bool(self.norm) and f" {self.norm} " in f" {normalize(title)} "

    def _own_sub(self, sub: str) -> bool:
        return self.norm.replace(" ", "") == sub.lower().replace("_", "")

    async def _search(self, path: str, query: str, t: str, restrict: bool) -> None:
        params = {"q": query, "sort": "relevance", "t": t, "type": "link", "limit": 50}
        if restrict:
            params["restrict_sr"] = 1
        try:
            data = await self._api(path, params)
        except HttpError as e:
            if e.status in (401, 403):
                raise
            log.warning("reddit: search %s failed: %s", path, e)
            return
        for child in ((data or {}).get("data") or {}).get("children") or []:
            if child.get("kind") != "t3":
                continue
            d = child.get("data") or {}
            if (d.get("id") and d["id"] not in self.threads and not d.get("over_18")
                    and self._title_matches(d.get("title", ""))):
                self.threads[d["id"]] = d

    def _rank(self, d: dict) -> float:
        sub = d.get("subreddit", "")
        s = min(d.get("num_comments") or 0, 500) + min(d.get("score") or 0, 2000) / 10
        if sub.lower() in {x.lower() for x in GAMING_SUBS}:
            s *= 2.0
        if self._own_sub(sub):
            s *= 1.8
        if OPINION_TITLE.search(d.get("title", "")):
            s *= 1.5
        if (d.get("created_utc") or 0) >= time.time() - TWO_YEARS:
            s *= 1.3
        return s

    async def collect(self) -> None:
        q = f'"{self.name}" {OPINION_TERMS}'
        await self._search("/r/" + "+".join(GAMING_SUBS) + "/search", q, "year", True)
        await self._search("/search", q, "year", False)
        if len(self.threads) < MIN_THREADS:
            await self._search("/search", q, "all", False)

        best = sorted(self.threads.values(), key=self._rank, reverse=True)[:MAX_THREADS]
        for thread in best:
            self._add_selftext(thread)
        for thread in best:
            if self.calls >= MAX_CALLS:
                break
            try:
                data = await self._api(f"/comments/{thread['id']}",
                                       {"sort": "top", "limit": 60, "depth": 1})
            except HttpError as e:
                if e.status in (401, 403):
                    raise
                log.warning("reddit: comments %s failed: %s", thread["id"], e)
                continue
            self._add_comments(thread, data)

    def _add(self, text: str, score, created, url: str, sub: str) -> None:
        if len(text) < MIN_CHARS:
            return
        self.items.append({"source": "reddit", "text": text, "score": int(score or 0),
                           "created": int(created or 0), "url": url, "subreddit": sub})

    def _add_selftext(self, d: dict) -> None:
        if d.get("removed_by_category") or _removed(d.get("selftext")) or is_bot(d.get("author"), {}):
            return
        title = clean(d.get("title", ""))
        body = clean(d["selftext"])
        if len(body) < MIN_CHARS:
            return
        self._add(clean(f"{title}. {body}"), d.get("score"), d.get("created_utc"),
                  WEB + d.get("permalink", f"/comments/{d['id']}/"), d.get("subreddit", ""))

    def _add_comments(self, thread: dict, data) -> None:
        if not isinstance(data, list) or len(data) < 2:
            return
        for child in ((data[1] or {}).get("data") or {}).get("children") or []:
            if child.get("kind") != "t1":
                continue
            c = child.get("data") or {}
            if _removed(c.get("body")) or is_bot(c.get("author"), c):
                continue
            permalink = c.get("permalink") or f"{thread.get('permalink', '')}{c.get('id', '')}/"
            self._add(clean(c["body"]), c.get("score"), c.get("created_utc"),
                      WEB + permalink, c.get("subreddit") or thread.get("subreddit", ""))

    def result(self, limit: int) -> list[dict]:
        seen, out = set(), []
        for it in self.items:
            key = normalize(it["text"])[:200]
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        recent_from = time.time() - TWO_YEARS
        out.sort(key=lambda it: (it["created"] >= recent_from, it["score"]), reverse=True)
        return out[:max(0, limit)]
