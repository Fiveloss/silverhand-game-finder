"""Collaborative filtering from real Steam players, not from the bot's own few users.

"People who put 15+ hours into A and recommended it also put many hours into B."

harvest() reads positive reviews of a game by players with min_hours or more, fetches the
public libraries of up to `reviewers` of them and keeps only aggregates:

coplay       (a, b) -> weight: sum over a's players of log1p(hours in b) / sum of log1p(hours)
             over that player's library (how much of their time b took), and players: how
             many of them put 5+ hours into b. Only the top neighbours per game are kept.
coplay_meta  a -> how many libraries were read for it, and when.
coplay_pop   b -> how many of all libraries read so far have 5+ hours in b; the row b = 0
             holds the total number of libraries read (appid 0 is not a game).

Privacy: steamids and per-person libraries live only in memory during one harvest; nothing
identifying is written to the database or the log.

Scores. A raw co-play count rewards games everyone owns (CS2, Dota 2, PUBG). neighbours()
compares P(b | played a) with P(b) among the players read for *other* games, smoothed towards
a prior (SteamSpy owners, else the pooled rate over all libraries) while there is little data, and takes the geometric mean of
  - leverage P(b|a) - P(b)        (needs real support, small for games everyone has)
  - 1 - 1 / lift                  (PMI-like, rewards specific links)
times a small bonus when a's players sink more hours than usual into b. 0 = no link.
"""

import logging
import math
import time

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS coplay (
    a INTEGER NOT NULL,
    b INTEGER NOT NULL,
    weight REAL NOT NULL,                   -- sum of b's share of each a-player's hours (log1p)
    players INTEGER NOT NULL DEFAULT 0,     -- a-players with 5+ hours in b
    PRIMARY KEY (a, b)
);
CREATE INDEX IF NOT EXISTS coplay_b ON coplay(b);
CREATE TABLE IF NOT EXISTS coplay_meta (
    appid INTEGER PRIMARY KEY,
    players INTEGER NOT NULL,               -- libraries read for this game (all harvests)
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS coplay_pop (
    b INTEGER PRIMARY KEY,                  -- 0 = total libraries read
    players INTEGER NOT NULL
);
"""

TOTAL = 0               # coplay_pop row with the number of libraries read
MIN_B_HOURS = 5         # hours in another game for it to count
TOP_NEIGHBOURS = 300
MAX_FAILURES = 6        # consecutive library errors before giving up on this harvest
MAX_PLAYED = 1500       # libraries with more played games are bots or idlers

K_POP = 20.0            # pseudo-players for P(b): the SteamSpy prior until real data arrives
K_COND = 5.0            # pseudo-players for P(b|a): one or two players are not a pattern
DEFAULT_PRIOR = 0.03
K_POOL = 10.0           # pseudo-players pulling the pooled rate towards DEFAULT_PRIOR
OWNERS_SCALE = 30_000_000   # owners at which the prior says "most heavy players have it"
PRIOR_MIN, PRIOR_MAX = 0.003, 0.7

DISLIKE_PENALTY = 0.6
CACHE_SIZE = 256


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(coplay)")}
    if "players" not in have:
        conn.execute("ALTER TABLE coplay ADD COLUMN players INTEGER NOT NULL DEFAULT 0")
    conn.commit()


# --- harvesting

def _positive_authors(reviews: list[dict], min_minutes: int, seen: set, out: list) -> None:
    for r in reviews:
        if not r.get("voted_up"):
            continue
        a = r.get("author") or {}
        sid = a.get("steamid")
        if not sid or sid in seen or (a.get("playtime_forever") or 0) < min_minutes:
            continue
        seen.add(sid)
        out.append(sid)


async def harvest(steam, conn, appid: int, *, reviewers: int = 40, min_hours: float = 15) -> int:
    """Read up to `reviewers` public libraries of players who liked `appid`; returns how many
    were used. Never raises on network trouble; a game whose reviews could not be fetched is
    left unmarked so it is tried again later."""
    ensure_schema(conn)
    if not getattr(steam, "api_key", ""):
        log.info("coplay: no Steam Web API key, skipping %s", appid)
        return 0

    authors: list[str] = []
    seen: set[str] = set()
    fetched = False
    for flt in ("all", "recent"):     # "all" = most helpful: long-time fans; "recent": today's
        if len(authors) >= reviewers * 2:
            break
        try:
            _, revs = await steam.reviews(appid, filter=flt, language="all", pages=1, per_page=100)
            fetched = True
        except Exception as e:  # network, HTTP, bad JSON: try the other list
            log.warning("coplay: reviews (%s) for %s failed: %s", flt, appid, e)
            continue
        _positive_authors(revs, int(min_hours * 60), seen, authors)
    seen.clear()
    if not fetched:
        return 0

    agg: dict[int, list] = {}       # b -> [weight, players]
    used = failures = 0
    for sid in authors:
        if used >= reviewers or failures >= MAX_FAILURES:
            break
        try:
            lib = await steam.owned_games(sid)
        except Exception as e:
            failures += 1
            log.debug("coplay: a library for %s failed: %s", appid, e)
            continue
        failures = 0
        if not lib:
            continue    # private or empty
        hours = {}
        for g in lib:
            try:
                h = (g.get("playtime_min") or 0) / 60
                if h > 0:
                    hours[int(g["appid"])] = h
            except (KeyError, TypeError, ValueError):
                continue
        total = sum(math.log1p(h) for h in hours.values())
        played = [b for b, h in hours.items() if h >= MIN_B_HOURS and b != appid]
        if total <= 0 or not played or len(played) > MAX_PLAYED:
            continue
        used += 1
        for b in played:
            cell = agg.setdefault(b, [0.0, 0])
            cell[0] += math.log1p(hours[b]) / total
            cell[1] += 1
        del lib, hours, played
    authors.clear()

    _store(conn, appid, agg, used)
    log.info("coplay: %s <- %d libraries, %d games", appid, used, len(agg))
    return used


def _store(conn, appid: int, agg: dict[int, list], used: int) -> None:
    """Merge one harvest into the tables. A repeat harvest adds a new sample to the old one."""
    now = int(time.time())
    if used:
        conn.executemany(
            "INSERT INTO coplay_pop(b, players) VALUES(?, ?) "
            "ON CONFLICT(b) DO UPDATE SET players = players + excluded.players",
            [(b, c) for b, (_, c) in agg.items()] + [(TOTAL, used)])
        for b, w, c in conn.execute("SELECT b, weight, players FROM coplay WHERE a=?", (appid,)):
            cell = agg.setdefault(b, [0.0, 0])
            cell[0] += w
            cell[1] += c
        top = sorted(agg.items(), key=lambda kv: (kv[1][1], kv[1][0]), reverse=True)[:TOP_NEIGHBOURS]
        conn.execute("DELETE FROM coplay WHERE a=?", (appid,))
        conn.executemany("INSERT INTO coplay(a, b, weight, players) VALUES(?, ?, ?, ?)",
                         [(appid, b, w, c) for b, (w, c) in top])
    conn.execute(
        "INSERT INTO coplay_meta(appid, players, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(appid) DO UPDATE SET players = players + excluded.players, "
        "updated_at = excluded.updated_at", (appid, used, now))
    conn.commit()
    _cache.clear()


# --- reading

_cache: dict[tuple, dict[int, float]] = {}


def _prior(conn, appids: list[int]) -> dict[int, float]:
    """Share of heavy players expected to have 5+ hours in each game, from SteamSpy owners."""
    out = {}
    try:
        for i in range(0, len(appids), 500):
            chunk = appids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for appid, owners in conn.execute(
                    f"SELECT appid, owners FROM games WHERE appid IN ({marks})", chunk):
                if owners:
                    out[appid] = min(PRIOR_MAX, max(PRIOR_MIN, owners / OWNERS_SCALE))
    except Exception:  # no games table (tests, a separate database): flat prior
        pass
    return out


def _meta_players(conn, appids: list[int]) -> dict[int, int]:
    out = {}
    for i in range(0, len(appids), 500):
        chunk = appids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for appid, n in conn.execute(
                f"SELECT appid, players FROM coplay_meta WHERE appid IN ({marks})", chunk):
            out[appid] = n
    return out


def _total(conn) -> int:
    row = conn.execute("SELECT players FROM coplay_pop WHERE b=?", (TOTAL,)).fetchone()
    return row[0] if row else 0


def _neighbour_map(conn, appid: int) -> dict[int, float] | None:
    """{b: 0..1} for one game, None when no library was read for it; cached until a harvest writes."""
    row = conn.execute("SELECT players FROM coplay_meta WHERE appid=?", (appid,)).fetchone()
    n_a = row[0] if row else 0
    if n_a <= 0:
        return None
    total = _total(conn)
    key = (id(conn), appid, n_a, total)
    hit = _cache.get(key)
    if hit is not None:
        return hit

    rows = conn.execute("SELECT b, weight, players FROM coplay WHERE a=?", (appid,)).fetchall()
    bs = [b for b, _, _ in rows]
    pop = {}
    for i in range(0, len(bs), 500):
        chunk = bs[i:i + 500]
        marks = ",".join("?" * len(chunk))
        pop.update(conn.execute(f"SELECT b, players FROM coplay_pop WHERE b IN ({marks})", chunk))
    seeds = _meta_players(conn, bs)
    prior = _prior(conn, bs)
    sum_w = sum(w for _, w, c in rows if c > 0)
    sum_c = sum(c for _, _, c in rows)
    mean_share = sum_w / sum_c if sum_c else 0.0

    out = {}
    for b, w, c in rows:
        if c <= 0:
            continue
        # P(b) among players read for other games; b's own fans are not a fair sample of b.
        others = max(0, total - n_a - seeds.get(b, 0))
        pop_other = max(0, pop.get(b, 0) - c)
        # Prior: SteamSpy owners when known, else the pooled rate over every library read.
        p0 = prior.get(b)
        if p0 is None:
            p0 = (pop.get(b, 0) + K_POOL * DEFAULT_PRIOR) / (total + K_POOL)
        p_b = (pop_other + K_POP * p0) / (others + K_POP)
        p_b = min(0.98, max(1e-4, p_b))
        p_ba = (c + K_COND * p_b) / (n_a + K_COND)
        if p_ba <= p_b:
            continue
        # Leverage P(b|a) - P(b) needs real support and stays small for games everyone has;
        # 1 - P(b)/P(b|a) is the PMI-like part that rewards specific links.
        s = math.sqrt((p_ba - p_b) * (1 - p_b / p_ba))
        s *= 1 - math.exp(-c / 1.5)     # one player is an anecdote, three start a pattern
        if mean_share > 0:
            # Players who sink more of their time into b than usual: up to ~11% more, or less.
            s *= min(2.0, max(0.5, (w / c) / mean_share)) ** 0.15
        out[b] = min(1.0, s)

    if len(_cache) >= CACHE_SIZE:
        _cache.clear()
    _cache[key] = out
    return out


def neighbours(conn, appid: int, limit: int = 50) -> list[tuple[int, float]]:
    """Games most specifically co-played with `appid`, best first, scores 0..1."""
    m = _neighbour_map(conn, appid) or {}
    return sorted(m.items(), key=lambda kv: -kv[1])[:limit]


def _link(conn, a: int, b: int) -> float | None:
    """Strength of a <-> b from either side's harvest; None when neither was harvested."""
    fwd, rev = _neighbour_map(conn, a), _neighbour_map(conn, b)
    if fwd is None and rev is None:
        return None
    return max((fwd or {}).get(b, 0.0), (rev or {}).get(a, 0.0))


def _combine(values: list[float], top: int = 3) -> float:
    """Noisy-OR of the strongest few links: one strong link counts, many weak ones do not pile up."""
    out = 1.0
    for v in sorted(values, reverse=True)[:top]:
        out *= 1 - max(0.0, min(1.0, v))
    return 1 - out


def coplay_score(conn, liked: list[tuple[int, float]], disliked: list[int], candidate: int) -> float | None:
    """0..1: how strongly fans of the player's liked games (weight > 0, capped at 1) play
    `candidate`, minus a penalty when it is a strong neighbour of games they disliked.
    None when there is no co-play data for any of the pairs."""
    pos, neg, known = [], [], False
    for appid, w in liked:
        if w <= 0 or appid == candidate:
            continue
        s = _link(conn, appid, candidate)
        if s is not None:
            known = True
            pos.append(s * min(1.0, w))
    for appid in disliked:
        if appid == candidate:
            continue
        s = _link(conn, appid, candidate)
        if s is not None:
            known = True
            neg.append(s)
    if not known:
        return None
    score = _combine(pos) - DISLIKE_PENALTY * (max(neg) if neg else 0.0)
    return max(0.0, min(1.0, score))


def strongest_link(conn, liked: list[int], candidate: int, min_score: float = 0.2) -> tuple[int, float] | None:
    """(liked appid, score) whose fans play `candidate` the most, for "because you liked X"."""
    best = None
    for appid in liked:
        if appid == candidate:
            continue
        s = _link(conn, appid, candidate)
        if s is not None and s >= min_score and (best is None or s > best[1]):
            best = (appid, s)
    return best


def candidates(conn, liked: list[tuple[int, float]], limit: int = 200) -> dict[int, float]:
    """{appid: 0..1} games reachable from the liked games through co-play, liked ones excluded."""
    liked = [(a, min(1.0, w)) for a, w in liked if w > 0]
    own = {a for a, _ in liked}
    found: dict[int, list[float]] = {}
    for a, w in liked:
        for b, s in (_neighbour_map(conn, a) or {}).items():
            if b not in own:
                found.setdefault(b, []).append(s * w)
        # The other way round: harvested games whose fans play this liked game.
        for (seed,) in conn.execute("SELECT a FROM coplay WHERE b=?", (a,)).fetchall():
            if seed not in own:
                s = (_neighbour_map(conn, seed) or {}).get(a, 0.0)
                if s > 0:
                    found.setdefault(seed, []).append(s * w)
    scored = {b: _combine(v) for b, v in found.items()}
    best = sorted(scored.items(), key=lambda kv: -kv[1])[:limit]
    return {b: s for b, s in best if s > 0}


# --- worker helpers

def harvested(conn, appids) -> set[int]:
    """Which of these games have been harvested (even if no public library was found)."""
    ensure_schema(conn)
    ids = list(appids)
    return set(_meta_players(conn, ids)) if ids else set()


def stale(conn, max_age_days: float = 90, limit: int = 1) -> list[int]:
    """Harvested games oldest first, older than max_age_days: candidates for a fresh sample."""
    ensure_schema(conn)
    since = int(time.time() - max_age_days * 86400)
    rows = conn.execute("SELECT appid FROM coplay_meta WHERE updated_at < ? ORDER BY updated_at LIMIT ?",
                        (since, limit))
    return [r[0] for r in rows]
