"""One aiohttp session with a minimum interval per host and polite retries.

Steam's store API allows roughly 200 requests per 5 minutes, SteamSpy one per second
(one per minute for request=all); the intervals below keep under both.
"""

import asyncio
import logging
import time
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger(__name__)

USER_AGENT = "SilverhandGameFinder/0.1 (+private Telegram bot)"

MIN_INTERVAL = {
    "store.steampowered.com": 1.6,
    "steamspy.com": 1.1,
    "api.steampowered.com": 0.3,
    "oauth.reddit.com": 1.1,
    "www.reddit.com": 2.0,
    "generativelanguage.googleapis.com": 6.0,   # free tier: about 10 requests a minute
    "api.groq.com": 2.5,
}


class HttpError(Exception):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status


class Http:
    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._session = session
        self._next_at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=60))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _wait_turn(self, host: str, interval: float | None) -> None:
        gap = MIN_INTERVAL.get(host, 0.2) if interval is None else interval
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            wait = self._next_at.get(host, 0) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_at[host] = max(now, self._next_at.get(host, 0)) + gap

    async def get_json(self, url: str, params: dict | None = None, *, headers: dict | None = None,
                       interval: float | None = None, retries: int = 3, timeout: float = 60):
        host = urlsplit(url).hostname or ""
        for attempt in range(retries + 1):
            await self._wait_turn(host, interval)
            try:
                s = await self.session()
                async with s.get(url, params=params, headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    if r.status == 429 or r.status >= 500:
                        raise HttpError(r.status, url)
                    if r.status != 200:
                        raise HttpError(r.status, url)
                    return await r.json(content_type=None)
            except HttpError as e:
                if e.status not in (429,) and e.status < 500 or attempt == retries:
                    raise
                delay = 30 * (attempt + 1) if e.status == 429 else 3 * (attempt + 1)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                if attempt == retries:
                    raise
                delay = 3 * (attempt + 1)
                log.debug("retrying %s after %s", url, e)
            await asyncio.sleep(delay)

    async def post_json(self, url: str, payload: dict, *, headers: dict | None = None,
                        timeout: float = 180) -> tuple[int, dict | None]:
        """(status, body) without raising on HTTP errors: LLM APIs report limits in the body."""
        host = urlsplit(url).hostname or ""
        await self._wait_turn(host, None)
        s = await self.session()
        async with s.post(url, json=payload, headers=headers,
                          timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            try:
                body = await r.json(content_type=None)
            except ValueError:
                body = None
            return r.status, body

    async def post_form(self, url: str, data: dict, *, auth: aiohttp.BasicAuth | None = None) -> dict:
        host = urlsplit(url).hostname or ""
        await self._wait_turn(host, None)
        s = await self.session()
        async with s.post(url, data=data, auth=auth) as r:
            if r.status != 200:
                raise HttpError(r.status, url)
            return await r.json(content_type=None)
