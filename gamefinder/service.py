"""Everything the bot asks for, in one place: finding games, reading their reviews, recommending.

The background worker grows the catalog (store lists, then SteamSpy tags and store facts per
game) and works through the analysis queue. It steps aside while a player is waiting, since
both share Steam's rate limits.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from . import aspects, coplay, external, maintenance, reviews, semantic, titles
from .analyst import Analyst, heuristic_passport
from .config import Config
from .db import Db
import aiohttp

from .http import Http, HttpError
from .recommender import Catalog, Pick, recommend
from .rerank import candidate_from, profile_from, rerank
from .suggest import suggest
from .sources.deck import deck_fields, deck_status, proton_summary
from .sources.gog import Gog
from .sources.igdb import Igdb
from .sources.reddit import Reddit
from .sources.steam import Steam, region_prices, spy_fields, store_fields
from .taste import Taste, for_request

log = logging.getLogger(__name__)

CATALOG_REFRESH = 24 * 3600
MIN_REVIEWS = 200

# What a flaky network or a broken answer can raise from the HTTP helpers.
NET_ERRORS = (HttpError, OSError, asyncio.TimeoutError, aiohttp.ClientError, ValueError)


class Service:
    def __init__(self, cfg: Config, db: Db, http: Http):
        self.cfg = cfg
        self.db = db
        self.http = http
        self.steam = Steam(http, cfg.store_cc, cfg.steam_api_key)
        self.reddit = Reddit(http, cfg.reddit_client_id, cfg.reddit_client_secret)
        self.gog = Gog(http, cfg.store_cc)
        self.igdb = Igdb(http, cfg.igdb_client_id, cfg.igdb_client_secret)
        self.analyst = Analyst.from_config(http, cfg)
        self.catalog = Catalog()
        self._catalog_size = -1
        self._locks: dict[int, asyncio.Lock] = {}
        self.busy = 0   # players waiting right now
        self._prices: dict[tuple[int, str], tuple[float, dict]] = {}
        semantic.ensure_schema(db.conn)
        dropped = semantic.drop_other_models(db.conn, cfg.gemini_embed_model)
        if dropped:
            log.info("embeddings: dropped %d games indexed with another model", dropped)
        coplay.ensure_schema(db.conn)
        self._coplay_failed: set[int] = set()
        self._analysis_failed: set[int] = set()
        self._reindex_failed: set[int] = set()
        self._fetch_failed: set[int] = set()
        self._analysis_rest = 0.0
        self._embed_rest = 0.0
        self._tick = 0

    # --- games
    async def ensure_game(self, appid: int, name: str = "") -> dict | None:
        """The game's row, fetching tags and store facts first if they are missing."""
        g = self.db.game(appid)
        if g and g["store_ok"] and g["spy_ok"]:
            return g
        need_spy = not g or not g["spy_ok"]
        need_store = not g or not g["store_ok"]

        async def nothing():
            return None

        # Different hosts with their own rate limits: ask both at once.
        spy, d = await asyncio.gather(
            self.steam.spy_details(appid) if need_spy else nothing(),
            self.steam.store_details(appid) if need_store else nothing(),
            return_exceptions=True)
        for res in (spy, d):
            if isinstance(res, BaseException):
                log.warning("could not fetch game %s: %s", appid, res)
        if need_store and isinstance(d, dict):
            self.db.upsert_game(appid, **store_fields(d))
        if need_spy and isinstance(spy, dict):
            fields = {"name": name} if name and not g and not self.db.game(appid) else {}
            if spy.get("name"):
                fields.update(spy_fields(spy))
                if self.db.game(appid):
                    fields.pop("name", None)    # the store's name is the canonical one
            if self.db.game(appid) or fields.get("name"):
                self.db.upsert_game(appid, **fields, spy_ok=1)
        if need_store and d is None and self.db.game(appid):
            self.db.upsert_game(appid, store_ok=-1)    # not in this region's store, or not a game page
        return self.db.game(appid)

    async def resolve(self, text: str, limit: int = 5, strict: bool = False) -> list[dict]:
        """Games matching a title typed by a player: exact local match first, then store search.
        strict: only games that surely are the one named (a reference must not be a lookalike —
        "Alan Wake 2" is not on Steam and must not turn into "Alan Wake"); best match first."""
        local = self.db.find_by_name(text, limit)
        if local and titles.score(text, local[0]["name"]) == 1.0:
            return local[:1]
        self.busy += 1
        try:
            found = await self.steam.search(titles.expand(text), limit)
            if strict:
                found = sorted((i for i in found if titles.confident(text, i["name"])),
                               key=lambda i: -titles.score(text, i["name"]))
            out = []
            for item in found:
                g = self.db.game(item["appid"]) or await self.ensure_game(item["appid"], item["name"])
                if g and not g["is_dlc"]:
                    out.append(g)
            if strict:
                return out[:1]
            return out or local
        finally:
            self.busy -= 1

    async def prices(self, appids: list[int], cc: str) -> dict[int, dict]:
        """Current prices in a store region, cached for an hour (sales change daily)."""
        cc = (cc or self.cfg.store_cc).lower()
        now = time.time()
        out, todo = {}, []
        for a in appids:
            hit = self._prices.get((a, cc))
            if hit and now - hit[0] < 3600:
                out[a] = hit[1]
            elif a and a > 0:
                todo.append(a)
        if todo:
            fresh = await region_prices(self.http, todo, cc)
            for a, v in fresh.items():
                self._prices[(a, cc)] = (now, v)
                out[a] = v
        if len(self._prices) > 5000:
            self._prices.clear()
        return out

    async def refresh_deck(self, appid: int) -> None:
        deck, proton = await asyncio.gather(deck_status(self.http, appid, cc=self.cfg.store_cc),
                                            proton_summary(self.http, appid))
        fields = deck_fields(deck, proton)
        if deck is None:
            # Steam did not answer: keep any earlier tier, but mark it done so the worker moves on.
            fields = {k: v for k, v in fields.items() if k != "proton" or proton}
            fields["deck_ok"] = 1
        self.db.upsert_game(appid, **fields)

    # --- analysis
    def passport_fresh(self, appid: int) -> bool:
        p = self.db.passport(appid)
        if not p:
            return False
        age = time.time() - p["_updated_at"]
        if p["_source"] == "heuristic" and self.analyst.enabled:
            return age < 3 * 86400 and self.db.llm_games_today() >= self.cfg.llm_daily_games
        return age < self.cfg.passport_max_age_days * 86400

    async def analyze(self, appid: int, force: bool = False) -> dict | None:
        """Fresh review stats and passport for one game. Uses the LLM while today's budget lasts."""
        lock = self._locks.setdefault(appid, asyncio.Lock())
        async with lock:
            if not force and self.passport_fresh(appid):
                return self.db.passport(appid)
            g = await self.ensure_game(appid)
            if not g:
                self.db.dequeue(appid)
                return None
            try:
                stats, sample = await reviews.gather(self.steam, appid)
            except NET_ERRORS as e:
                log.warning("reviews for %s failed: %s", appid, e)
                return self.db.passport(appid)
            # A second store's players: GOG reviews join the sample, its rating goes next to Steam's.
            try:
                gog = await self.gog.find(g["name"])
                if gog:
                    gsum, gsample = await self.gog.reviews(gog["id"], limit=40)
                    stats["gog"] = {"rating": gog.get("rating"), "count": gsum.get("count") or gog.get("reviews"),
                                    "share_positive": gsum.get("share_positive"), "url": gog.get("url")}
                    sample = sample + gsample[:20]
            except Exception as e:
                log.warning("GOG for %s failed: %s", g["name"], e)
            self.db.set_review_stats(appid, stats)
            extra = await self.reddit.discussion(g["name"], limit=25) if self.reddit.enabled else []
            if self.analyst.enabled and self.db.llm_games_today() < self.cfg.llm_daily_games and sample:
                try:
                    passport, tin, tout, model = await self.analyst.passport(g, stats, sample, extra)
                    self.db.add_llm_usage(tin, tout)
                    self.db.set_passport(appid, passport, "llm", model, len(sample) + len(extra))
                    await self._index(appid, passport, sample)
                    return self.db.passport(appid)
                except Exception as e:  # any API or parsing failure: keep going on heuristics
                    log.warning("analyst failed for %s (%s): %s", appid, g["name"], e)
            passport = heuristic_passport(g, stats, sample)
            self.db.set_passport(appid, passport, "heuristic", "", len(sample))
            await self._index(appid, passport, sample)
            return self.db.passport(appid)

    async def _index(self, appid: int, passport: dict, sample: list[dict]) -> None:
        """Meaning vectors of the passport and of the best review snippets (free Gemini embeddings).
        After a refusal (the free per-minute limit) it rests five minutes instead of retrying each game."""
        if not (self.cfg.gemini_api_key and sample) or time.time() < self._embed_rest:
            return
        ok = await semantic.index_game(self.http, self.cfg.gemini_api_key, self.cfg.gemini_embed_model,
                                       self.db.conn, appid, passport, sample)
        if not ok:
            self._embed_rest = time.time() + 300
            log.info("embeddings for %s postponed; resting 5 min", appid)

    # --- players
    def refresh_catalog(self) -> None:
        size = self.db.game_count()
        if size != self._catalog_size or time.time() - self.catalog.built_at > 3600:
            self.catalog.build(self.db.catalog(MIN_REVIEWS))
            self._catalog_size = size

    def _next_coplay(self) -> int | None:
        """One game to harvest players' libraries for: the catalog's most reviewed first."""
        if not self.cfg.steam_api_key:
            return None
        skip = ",".join(str(a) for a in self._coplay_failed) or "0"
        row = self.db.conn.execute(
            "SELECT g.appid FROM games g LEFT JOIN coplay_meta m ON m.appid=g.appid "
            f"WHERE m.appid IS NULL AND g.appid NOT IN ({skip}) AND g.is_dlc=0 AND g.store_ok=1 "
            "AND g.positive+g.negative >= ? ORDER BY g.positive+g.negative DESC LIMIT 1",
            (MIN_REVIEWS,)).fetchone()
        if row:
            return row[0]
        old = coplay.stale(self.db.conn, 90, 1)
        return old[0] if old else None

    def _experience(self, taste: Taste):
        if not taste.vec:
            return None
        return lambda appids: semantic.experience_scores(self.db.conn, taste.vec, appids)

    async def recommend_now(self, user_id: int, req, seen: set[int] = frozenset(), limit: int = 3,
                            progress: Callable[[int, int], Awaitable[None]] | None = None,
                            stage: Callable[[str], Awaitable[None]] | None = None
                            ) -> tuple[list[Pick], list[dict]]:
        """Picks for one request (gamefinder.intent.Request), and the reference games it named.
        Nothing about the player is used or stored: every request starts from scratch, and `seen` only
        keeps this request's earlier results (and games turned down in it) from coming back."""
        self.busy += 1
        started = time.time()

        async def say(what: str) -> None:
            if stage:
                try:
                    await stage(what)
                except Exception:
                    pass

        try:
            self.refresh_catalog()
            seeds, avoid = [], []
            ext_passports: dict[int, dict] = {}
            boost: dict[int, float] = {}       # Steam games players compare a non-Steam reference to
            for names, out in ((req.seeds[:4], seeds), (req.avoid[:3], avoid)):
                for title in names:
                    g = await self.reference(title, ext_passports, boost if out is seeds else None)
                    if g and g["appid"] not in {x["appid"] for x in out}:
                        out.append(g)
            # The reference games are the anchor: know how they feel before looking for more
            # (a short wait at most; the background finishes what does not make it).
            # Reading reviews takes ~10 s a game against Steam's pace: never during a request. Unread
            # games work from their player tags now and are read first by the background worker.
            unread = [g["appid"] for g in seeds if g["appid"] > 0 and not self.db.passport(g["appid"])]
            if unread:
                self.db.enqueue_analysis(unread, priority=9)
            await say("Подбираю кандидатов")
            ids = list(self.catalog.vecs)
            passports = self.db.passports(ids + [g["appid"] for g in seeds if g["appid"] > 0])
            passports.update(ext_passports)
            stats = self._stats(ids)
            taste = for_request(req, seeds, avoid, passports, self.catalog.idf, set())

            async def embed_words():
                if not (req.words and self.cfg.gemini_api_key) or time.time() < self._embed_rest:
                    return None
                return await semantic.embed_query(self.http, self.cfg.gemini_api_key,
                                                  self.cfg.gemini_embed_model, req.words)

            # The model's proposals and the meaning of the player's words are fetched at the same time.
            words_vec, (scout, scout_why) = await asyncio.gather(embed_words(), self._scout(req, seeds, seen))
            taste.vec = semantic.combine([
                (words_vec, 0.6),
                (semantic.taste_vector_from_games(self.db.conn, [(g["appid"], 1.0) for g in seeds
                                                                  if g["appid"] > 0]), 0.4)])
            exp = self._experience(taste)
            co = self._coplay_seeds(taste)
            if boost:
                co["co_cands"] = {**co.get("co_cands", {}), **boost}
            # The model proposed games that match what the player LOVED; reviews verify them below.
            if scout:
                self.refresh_catalog()
                passports.update(self.db.passports(list(scout)))
                stats.update(self._stats(list(scout)))
                co["scout"] = scout
            limits = {"max_hours": req.max_hours, "min_hours": req.min_hours, "coop": req.coop,
                      "surprise": req.surprise, "axes": dict(req.axes), "mood": req.mood}
            exclude = set(seen)
            args = dict(mood=req.mood, exclude=exclude, experience=exp, limits=limits, **co)

            short = recommend(self.catalog, taste, passports, stats, limit=limit * 2, **args)
            # Candidates whose reviews are not read yet go first in the background queue: the next
            # request (and «Ещё 3») already has them.
            todo = [p.appid for p in short if not self.passport_fresh(p.appid)]
            if todo:
                self.db.enqueue_analysis(todo, priority=8)
            picks = recommend(self.catalog, taste, passports, stats, limit=limit, **args)
            log.info("request timing: %.1fs before the judge", time.time() - started)
            await say("Выбираю лучшие")
            picks = await self._judge_request(req, taste, seeds, avoid, passports, stats, picks, limit, args)
            if len(picks) < limit:
                # Not enough exact matches: the closest games without the hard limits, marked as such.
                loose = {**args, "limits": {"surprise": req.surprise},
                         "exclude": exclude | {p.appid for p in picks}}
                for p in recommend(self.catalog, taste, passports, stats, limit=limit - len(picks), **loose):
                    p.relaxed = True
                    picks.append(p)
            for p in picks:
                p.suggest_why = scout_why.get(p.appid, "")
                if taste.vec:
                    p.evidence = semantic.evidence(self.db.conn, taste.vec, p.appid, k=2)
                if p.because is None and taste.liked:
                    link = coplay.strongest_link(self.db.conn, taste.liked, p.appid)
                    p.because = link[0] if link else None
            nxt = recommend(self.catalog, taste, passports, stats, limit=8,
                            **{**args, "exclude": exclude | {p.appid for p in picks}})
            self.db.enqueue_analysis([p.appid for p in nxt if not self.passport_fresh(p.appid)], priority=5)
            return picks, seeds
        finally:
            self.busy -= 1

    async def aspect_options(self, title: str) -> tuple[dict | None, dict | None, list]:
        """(reference game, its passport, options for «Чем зацепила?»). Reads the game's reviews if
        they were never read, since the options come from what players praise in it."""
        self.busy += 1
        try:
            ext: dict = {}
            g = await self.reference(title, ext, None)
            if not g:
                return None, None, []
            if g["appid"] > 0:
                p = self.db.passport(g["appid"]) or await self.analyze(g["appid"])
            else:
                p = ext.get(g["appid"])
            return g, p, aspects.options(g, p, (p or {}).get("aspects"))
        finally:
            self.busy -= 1

    async def reference(self, title: str, ext_passports: dict, boost: dict | None) -> dict | None:
        """A game named as a reference: from Steam when it is surely there, else (an Epic or console
        exclusive, an old game) a card from the free LLM describing it. Cards get negative appids."""
        found = await self.resolve(title, limit=5, strict=True)
        if found:
            return found[0]
        # IGDB knows every platform: the game may be on Steam under another name, or elsewhere only.
        info = await self.igdb.find(title) if self.igdb.enabled else None
        if info and (info.get("stores") or {}).get("steam"):
            g = await self.ensure_game(int(info["stores"]["steam"]), info["name"])
            if g:
                return g
        card = await external.describe(self.analyst, self.db, title) if self.analyst.enabled else None
        if not card and info:
            g = igdb_game(info)
            ext_passports[g["appid"]] = heuristic_passport(g, None, [])
            return g
        if not card:
            return None
        if card.get("on_steam"):
            found = await self.resolve(card["name"], limit=5, strict=True)
            if found:
                return found[0]
        g = external.card_as_game(card)
        ext_passports[g["appid"]] = external.card_passport(card)
        if boost is not None:
            for name in card.get("steam_similar", [])[:5]:
                sim = await self.resolve(name, limit=5, strict=True)
                if sim:
                    boost[sim[0]["appid"]] = 0.6
        return g

    async def _scout(self, req, seeds: list[dict], seen) -> tuple[dict[int, float], dict[int, str]]:
        """Games the model proposes for this request, found in Steam: ({appid: boost}, {appid: why})."""
        wants = req.seeds or req.words or req.focus_labels or req.tags_want or req.mood != "any"
        if not wants or not self.analyst.enabled or self.db.llm_games_today() >= self.cfg.llm_daily_games:
            return {}, {}
        shown = [g["name"] for g in self.db.games(list(seen)).values()]
        usage: dict = {}
        try:
            ideas = await suggest(self.analyst, req, [g["name"] for g in seeds], shown, usage=usage)
        except Exception as e:
            log.warning("suggest failed: %s", e)
            ideas = None
        if usage.get("in"):
            self.db.add_llm_usage(usage["in"], usage.get("out", 0), games=0)
        if not ideas:
            return {}, {}
        taken = {g["appid"] for g in seeds} | set(seen)
        # Known games are found in the catalog at once; only a few unknown ones are looked up in Steam
        # now (each costs a few store requests), the rest are added for the background to fetch.
        hits, unknown = [], []
        for idea in ideas:
            local = [g for g in self.db.find_by_name(idea["title"], 5)
                     if titles.score(idea["title"], g["name"]) >= 0.9]
            if local:
                hits.append((local[0], idea))
            else:
                unknown.append(idea)
        sem = asyncio.Semaphore(3)

        async def find(idea: dict):
            async with sem:
                try:
                    found = await self.resolve(idea["title"], limit=5, strict=True)
                except Exception:
                    return None
            return (found[0], idea) if found else None

        looked = await asyncio.gather(*(find(i) for i in unknown[:6]))
        scout, why = {}, {}
        for hit in hits + list(looked):
            if not hit:
                continue
            g, idea = hit
            if g["appid"] in taken or g["appid"] in scout:
                continue
            scout[g["appid"]] = 0.4 + 0.4 * float(idea.get("fit") or 0.5)
            why[g["appid"]] = idea.get("why") or ""
        log.info("scout: %d ideas, %d found in Steam", len(ideas), len(scout))
        return scout, why

    def _coplay_seeds(self, taste: Taste) -> dict:
        if not self.cfg.steam_api_key or not taste.liked:
            return {}
        conn = self.db.conn
        liked = [(a, 1.0) for a in taste.liked]
        cands = coplay.candidates(conn, liked)
        for appid, _ in sorted(cands.items(), key=lambda kv: -kv[1])[:30]:
            if not self.db.game(appid):
                self.db.upsert_game(appid, name=f"App {appid}")
        return {"co_cands": cands, "coplay": lambda a: coplay.coplay_score(conn, liked, taste.disliked, a)}

    async def _judge_request(self, req, taste, seeds, avoid, passports, stats, picks, limit, args) -> list[Pick]:
        if not self.analyst.enabled or self.db.llm_games_today() >= self.cfg.llm_daily_games:
            return picks
        # Every candidate goes to the judge; the ones with only a tag estimate say so in their data.
        cands = recommend(self.catalog, taste, passports, stats, limit=12, **args)
        if len(cands) < limit:
            return picks
        names = {g["appid"]: g["name"] for g in seeds + avoid}
        profile = profile_from(taste, names, mood=req.mood, own_words=req.text)
        usage: dict = {}
        verdict = await rerank(self.analyst, profile,
                               [candidate_from(p, self.catalog.games[p.appid], stats.get(p.appid)) for p in cands],
                               k=limit, usage=usage)
        if usage.get("in"):
            self.db.add_llm_usage(usage["in"], usage.get("out", 0), games=0)
        if not verdict:
            return picks
        by_id = {p.appid: p for p in cands}
        out = []
        for v in verdict:
            p = by_id[v["id"]]
            p.judge_reason, p.judge_risk = v["reason"], v.get("risk", "")
            out.append(p)
        for p in cands:
            if len(out) >= limit:
                break
            if p not in out:
                out.append(p)
        return out

    def _stats(self, ids) -> dict[int, dict]:
        out = {}
        for appid in ids:
            row = self.db.review_stats(appid)
            if row:
                out[appid] = row[0]
        return out

    # --- background
    async def worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                did = await self._worker_step()
            except Exception:
                log.exception("worker step failed")
                did = False
            try:
                await asyncio.wait_for(stop.wait(), timeout=1 if did else 20)
            except asyncio.TimeoutError:
                pass

    async def _worker_step(self) -> bool:
        if self.busy:
            return False
        if time.time() - float(self.db.get_meta("catalog_at", "0")) > CATALOG_REFRESH:
            await self.seed_catalog()
            return True
        if time.time() - float(self.db.get_meta("maintenance_at", "0")) > 24 * 3600:
            self.db.set_meta("maintenance_at", str(time.time()))
            await asyncio.to_thread(maintenance.run_files, self.db, self.cfg)
            maintenance.run_db(self.db, self.cfg)
            return True
        for appid in self.db.games_missing_tags(40) + self.db.games_missing_store(40):
            if appid in self._fetch_failed:
                continue
            g = await self.ensure_game(appid)
            if not g or not g["spy_ok"] or not g["store_ok"]:
                self._fetch_failed.add(appid)
            return True
        for appid in self.db.games_missing_deck(1):
            await self.refresh_deck(appid)
            return True
        # The rest alternates, so neither starves: reading reviews (asked-for games first, then the
        # catalog by popularity, within 80% of the day's LLM budget) and harvesting players' libraries.
        self._tick += 1
        for job in ((self._analysis_step, self._coplay_step) if self._tick % 2 else
                    (self._coplay_step, self._analysis_step)):
            if await job():
                return True
        return False

    async def _analysis_step(self) -> bool:
        nxt = self.db.next_in_queue(1)
        if nxt:
            try:
                if not self.passport_fresh(nxt[0]):
                    await self.analyze(nxt[0])
            finally:
                # Dequeued even when Steam failed: the game comes back with the next player request.
                self.db.dequeue(nxt[0])
            return True
        if await self._reindex_step():
            return True
        if not self.analyst.enabled or self.db.llm_games_today() >= int(self.cfg.llm_daily_games * 0.8):
            return False
        if time.time() < self._analysis_rest:
            return False
        ahead = self.db.games_without_llm_passport(1, skip=self._analysis_failed)
        if not ahead:
            return False
        started = time.time()
        try:
            p = await self.analyze(ahead[0], force=True)
        except NET_ERRORS as e:
            log.info("analysis of %s failed: %s", ahead[0], e)
            p = None
        if not p or p.get("_source") != "llm" or p.get("_updated_at", 0) < started - 1:
            # The model did not answer (limits): leave this game for later and rest a little.
            self._analysis_failed.add(ahead[0])
            self._analysis_rest = time.time() + 120
        return True

    async def _reindex_step(self) -> bool:
        """A game the model has read but whose meaning vectors are missing (a refused call, a model
        change): fetch its reviews again and index them. No LLM call, only the free embeddings."""
        if not self.cfg.gemini_api_key or time.time() < self._embed_rest:
            return False
        row = self.db.conn.execute(
            "SELECT p.appid FROM passports p LEFT JOIN game_vectors v ON v.appid = p.appid "
            "WHERE p.source = 'llm' AND v.appid IS NULL AND p.appid > 0 "
            f"AND p.appid NOT IN ({','.join(str(a) for a in self._reindex_failed) or '0'}) "
            "ORDER BY p.updated_at DESC LIMIT 1").fetchone()
        if not row:
            return False
        appid = row[0]
        passport = self.db.passport(appid)
        try:
            _, sample = await reviews.gather(self.steam, appid)
        except NET_ERRORS as e:
            log.info("reindex %s: reviews failed: %s", appid, e)
            self._reindex_failed.add(appid)
            return True
        await self._index(appid, passport, sample)
        if not semantic.is_indexed(self.db.conn, appid):
            self._reindex_failed.add(appid)
        return True

    async def _coplay_step(self) -> bool:
        appid = self._next_coplay()
        if appid is None:
            return False
        await coplay.harvest(self.steam, self.db.conn, appid)
        if not coplay.harvested(self.db.conn, [appid]):
            self._coplay_failed.add(appid)      # reviews unreachable: try again after a restart
        return True

    async def seed_catalog(self) -> None:
        added = 0
        lists = [("topsellers", p) for p in range(self.cfg.catalog_pages * 4)]
        lists += [("top_rated", p) for p in range(self.cfg.catalog_pages * 2)]
        lists += [("popular_new", p) for p in range(2)]
        for kind, page in lists:
            if self.busy:
                await asyncio.sleep(5)
            try:
                items = await self.steam.store_list(kind, page)
            except NET_ERRORS as e:
                log.warning("store list %s/%s failed: %s", kind, page, e)
                continue
            for it in items:
                if not self.db.game(it["appid"]):
                    self.db.upsert_game(it["appid"], name=it["name"])
                    added += 1
        self.db.set_meta("catalog_at", str(time.time()))
        log.info("catalog: %d new games, %d total", added, self.db.game_count())


def igdb_game(info: dict) -> dict:
    """A game row for a reference that only IGDB knows (no LLM card): its tags drive the taste."""
    card = {"name": info["name"], "tags": info.get("tags") or {}, "feel": {}, "aspects": [],
            "platforms": info.get("platforms") or [], "on_steam": False, "steam_similar": []}
    g = external.card_as_game(card)
    g["igdb"] = info
    return g
