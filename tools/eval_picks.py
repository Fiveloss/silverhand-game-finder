"""Run real requests through the whole pick pipeline (understanding, scout, scores, judge) and print
what the bot would answer. For checking pick quality by eye after a change; it calls the free
models with the keys from .env, but never Telegram.

    python tools/eval_picks.py <copy-of-a-database.db> ["как Subnautica" ...]

Use a copy: the run adds games and queue rows to the database it is given. Without requests it
runs the built-in set below.
"""

import asyncio
import dataclasses
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gamefinder import intent  # noqa: E402
from gamefinder.config import load_config  # noqa: E402
from gamefinder.db import Db  # noqa: E402
from gamefinder.http import Http  # noqa: E402
from gamefinder.service import Service  # noqa: E402

REQUESTS = [
    "как Subnautica",
    "как Hollow Knight",
    "как Alan Wake 2",
    "что-нибудь спокойное на вечер",
    "сюжетная как Ведьмак 3, без гринда",
    "кооператив с другом, не шутер",
    "как Disco Elysium",
    "хоррор как Dead Space",
    "как Stardew Valley",
    "как Baldur's Gate 3",
    "как Hades, но покороче",
    "как Outer Wilds",
]


TIMES: dict[str, list[float]] = {}


def timed(obj, name: str, label: str | None = None) -> None:
    """Wrap obj.name (a coroutine function) to add up its time under `label` for the current request."""
    fn = getattr(obj, name)

    async def wrapper(*a, **kw):
        t = time.time()
        try:
            return await fn(*a, **kw)
        finally:
            TIMES.setdefault(label or name, []).append(time.time() - t)

    setattr(obj, name, wrapper)


def host_timing(http: Http) -> None:
    """Time every HTTP call by host (Steam, SteamSpy, Gemini...)."""
    from urllib.parse import urlsplit
    for name in ("get_json", "post_json"):
        fn = getattr(http, name)

        def make(fn):
            async def wrapper(url, *a, **kw):
                t = time.time()
                try:
                    return await fn(url, *a, **kw)
                finally:
                    TIMES.setdefault("http " + (urlsplit(url).hostname or "?"), []).append(time.time() - t)
            return wrapper
        setattr(http, name, make(fn))


def top_tags(g: dict, n: int = 5) -> list[str]:
    return [t for t, _ in sorted((g.get("tags") or {}).items(), key=lambda kv: -kv[1])[:n]]


async def one(s: Service, text: str, uid: int) -> dict:
    TIMES.clear()
    t0 = time.time()
    try:
        req = await intent.parse(s.analyst, text)
    except Exception:
        req = intent.parse_heuristic(text)
    req.asked = True                        # as if the player skipped the question about the game
    picks, seeds = await s.recommend_now(uid, req)
    out = {"request": text, "label": req.label(), "seconds": round(time.time() - t0, 1),
           "timing": {k: [len(v), round(sum(v), 1)] for k, v in sorted(TIMES.items(), key=lambda kv: -sum(kv[1]))},
           "seeds": [{"name": g["name"], "tags": top_tags(g)} for g in seeds], "picks": []}
    for p in picks:
        g = s.db.game(p.appid) or s.catalog.games.get(p.appid) or {}
        src = (s.db.passport(p.appid) or {}).get("_source", "-")
        out["picks"].append({
            "name": g.get("name", p.appid), "score": round(p.score, 3), "passport": src,
            "relaxed": p.relaxed, "tags": top_tags(g),
            "parts": {k: round(v, 2) for k, v in p.parts.items() if abs(v) >= 0.01},
            "judge": p.judge_reason, "risk": p.judge_risk, "scout": p.suggest_why})
    return out


async def main() -> None:
    if len(sys.argv) < 2 or not Path(sys.argv[1]).is_file():
        print(__doc__)
        sys.exit(2)
    os.chdir(ROOT)
    cfg = dataclasses.replace(load_config(), db_path=sys.argv[1])
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    db, http = Db(cfg.db_path), Http()
    s = Service(cfg, db, http)
    host_timing(http)
    for name in ("reference", "_scout", "_judge_request", "resolve", "ensure_game", "aspect_options"):
        if hasattr(s, name):
            timed(s, name)
    timed(intent, "parse", "intent.parse")
    try:
        for i, text in enumerate(sys.argv[2:] or REQUESTS):
            res = await one(s, text, 900000 + i)
            print(json.dumps(res, ensure_ascii=False), flush=True)
    finally:
        await http.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
