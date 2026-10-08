"""Run real requests through the whole pick pipeline (understanding, scout, scores, judge) and print
what the bot would answer. For checking pick quality by eye after a change; it calls the free
models with the keys from .env, but never Telegram.

    python tools/eval_picks.py <copy-of-a-database.db> ["как Subnautica" ...]
    python tools/eval_picks.py <copy-of-a-database.db> --golden [tools/golden.json]

--golden checks the picks against a fixed set: for each request, games that fit ("good", a pick
from it is a hit) and games that must never come up ("forbid"). It prints the hits, the
forbidden picks and a total, and exits with 1 when a forbidden game was picked.

Use a copy: the run adds games and queue rows to the database it is given. Without requests it
runs the built-in set below.
"""

import asyncio
import dataclasses
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gamefinder import intent, titles  # noqa: E402
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
    spare = [(s.db.game(p.appid) or {}).get("name", p.appid) for p in picks[3:]]
    picks = picks[:3]                       # what the player sees; the rest are spares
    out = {"request": text, "label": req.label(), "seconds": round(time.time() - t0, 1),
           "timing": {k: [len(v), round(sum(v), 1)] for k, v in sorted(TIMES.items(), key=lambda kv: -sum(kv[1]))},
           "seeds": [{"name": g["name"], "tags": top_tags(g)} for g in seeds], "picks": [], "spare": spare}
    for p in picks:
        g = s.db.game(p.appid) or s.catalog.games.get(p.appid) or {}
        src = (s.db.passport(p.appid) or {}).get("_source", "-")
        out["picks"].append({
            "name": g.get("name", p.appid), "score": round(p.score, 3), "passport": src,
            "relaxed": p.relaxed, "tags": top_tags(g),
            "parts": {k: round(v, 2) for k, v in p.parts.items() if abs(v) >= 0.01},
            "judge": p.judge_reason, "risk": p.judge_risk, "scout": p.suggest_why})
    return out


def matches(name: str, wanted: list[str]) -> str | None:
    """The entry of `wanted` that is this game (the same title, an edition of it, or its full name)."""
    edition_off = re.split(r" - | – | — ", name)[0]       # "Sekiro: Shadows Die Twice - GOTY Edition"
    for w in wanted:
        if titles.same_game(w, name) or titles.same_game(w, edition_off):
            return w
    return None


async def golden(s: Service, path: str) -> int:
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    hits = forbidden = picked = good_picks = 0
    pause = float(os.environ.get("GOLDEN_PAUSE", "15"))   # like real players: Groq's per-minute quota refills
    for i, case in enumerate(cases):
        if i:
            await asyncio.sleep(pause)
        res = await one(s, case["request"], 910000 + i)
        names = [p["name"] for p in res["picks"]]
        good = [n for n in names if matches(n, case["good"])]
        bad = [n for n in names if matches(n, case.get("forbid", []))]
        hits += bool(good)
        forbidden += len(bad)
        picked += len(names)
        good_picks += len(good)
        mark = "FORBIDDEN" if bad else ("ok" if good else "miss")
        print(f"{mark:9} {case['request']:40} {res['seconds']:5.1f}s  " + " | ".join(
            ("+" if n in good else "x" if n in bad else " ") + n for n in names), flush=True)
    print(f"\nhit (a known good game among the picks): {hits}/{len(cases)}; known good picks: "
          f"{good_picks}/{picked}; forbidden picks: {forbidden}")
    return 1 if forbidden else 0


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
        if len(sys.argv) > 2 and sys.argv[2] == "--golden":
            code = await golden(s, sys.argv[3] if len(sys.argv) > 3 else str(ROOT / "tools" / "golden.json"))
            await http.close()
            db.close()
            sys.exit(code)
        for i, text in enumerate(sys.argv[2:] or REQUESTS):
            res = await one(s, text, 900000 + i)
            print(json.dumps(res, ensure_ascii=False), flush=True)
    finally:
        await http.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
