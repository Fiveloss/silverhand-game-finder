"""Scoring games for one player.

score = 0.35 tags + 0.25 feel + 0.25 quality + 0.15 complaints, then a mood bonus, then picks
are spread out so three results are not three versions of the same game.

- tags: cosine between the player's liked-tags profile and the game's player-voted tags,
  minus half the cosine with the disliked-tags profile;
- feel: how close the game's passport axes are to the player's, weighted by how sure we are
  the axis matters to them;
- quality: review reception, leaning on recent reviews and players with 10+ hours;
- complaints: taste complaints pointing the player's way count for the game, against it
  otherwise; quality complaints (bugs, performance, monetization) always count against.
"""

import math
import random
from collections.abc import Callable
import time
from dataclasses import dataclass, field

from .analyst import AXES, heuristic_passport
from .reviews import quality_score, wilson_lower
from .taste import Taste, cosine, tag_vector

SHARE_WEIGHT = {"most": 1.0, "many": 0.6, "some": 0.3}
# Sharing only broad tags (Survival, Horror, First-Person) is about 0.25: not enough to call games alike.
MIN_TAG_SIM = 0.3

MOODS = {
    "any": "Любое",
    "evening": "На вечер",
    "story": "Сюжет",
    "chill": "Расслабиться",
    "challenge": "Челлендж",
    "coop": "С друзьями",
    "gems": "Скрытые жемчужины",
    "fresh": "Новинки",
}

DEALBREAKERS = {
    "mtx": "Донат и микротранзакции",
    "online_only": "Только онлайн",
    "early_access": "Ранний доступ",
    "no_ru": "Нет русского языка",
    "no_ru_audio": "Нет русской озвучки",
    "denuvo": "Denuvo",
    "horror": "Хоррор",
    "adult": "Контент 18+",
    "no_deck": "Не идёт на Steam Deck",
}


@dataclass
class Pick:
    appid: int
    score: float
    parts: dict[str, float]
    passport: dict
    passport_real: bool
    because: int | None = None                       # the liked game it is closest to
    feel_matches: list[str] = field(default_factory=list)
    taste_notes: list[tuple[str, bool]] = field(default_factory=list)   # (complaint, good for you)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)   # review snippets closest to their taste
    suggest_why: str = ""       # why the LLM proposed it (suggest.py), shown when the judge said nothing
    relaxed: bool = False       # filled in without the request's hard limits (not enough exact matches)
    judge_reason: str = ""      # from the LLM judge (rerank.py), when it ran
    judge_risk: str = ""


class Catalog:
    """Tag vectors and IDF for every recommendable game, rebuilt when the catalog grows."""

    def __init__(self):
        self.games: dict[int, dict] = {}
        self.vecs: dict[int, dict[str, float]] = {}
        self.idf: dict[str, float] = {}
        self.built_at = 0.0

    def build(self, games: list[dict]) -> None:
        df: dict[str, int] = {}
        for g in games:
            for t in g["tags"]:
                df[t] = df.get(t, 0) + 1
        n = max(1, len(games))
        # Capped: a rare tag («Underwater») must not make a fish-eating arcade a Subnautica twin.
        self.idf = {t: min(3.0, math.log(1 + n / c)) for t, c in df.items()}
        self.games = {g["appid"]: g for g in games}
        self.vecs = {g["appid"]: tag_vector(g["tags"], self.idf) for g in games}
        self.built_at = time.time()


def blocked(g: dict, passport: dict | None, dealbreakers: list[str]) -> bool:
    tags = g.get("tags") or {}
    q = (passport or {}).get("quality") or {}
    rules = {
        "mtx": g.get("mtx") or q.get("monetization", 0) >= 2,
        "online_only": g.get("online_only"),
        "early_access": g.get("early_access"),
        "no_ru": g.get("store_ok") and not g.get("ru_text"),
        "no_ru_audio": g.get("store_ok") and not g.get("ru_audio"),
        "denuvo": "denuvo" in (g.get("drm_notice") or "").lower(),
        "horror": any(t in tags for t in ("Horror", "Psychological Horror", "Survival Horror")),
        "adult": g.get("adult"),
        # Only a known "unsupported" or a borked ProtonDB tier blocks; unknown is not a no.
        "no_deck": g.get("deck") == 1 or g.get("proton") == "borked",
    }
    return any(rules.get(d) for d in dealbreakers)


def est_hours(p: dict, stats: dict | None) -> float:
    """Hours to finish, from players who liked it, else from the passport's length axis."""
    if stats and stats.get("median_hours_positive"):
        return float(stats["median_hours_positive"])
    return 3 * 1.45 ** p["feel"]["length"]      # 0 -> 3 h, 5 -> ~19 h, 10 -> ~120 h


def limits_fit(limits: dict, g: dict, p: dict, stats: dict | None) -> bool:
    if not limits:
        return True
    hours = est_hours(p, stats)
    # A replayable game fits "for one evening" (a run is short), not "for a couple of evenings".
    session_game = p["feel"]["replay"] >= 7 and limits.get("mood") == "evening"
    if limits.get("max_hours") and hours > limits["max_hours"] * 1.3 and not session_game:
        return False
    if limits.get("min_hours") and hours < limits["min_hours"] * 0.7:
        return False
    if limits.get("coop") and not g.get("coop") and p["feel"]["social"] < 6:
        return False
    # Only practical limits are filters («попроще», «не страшно», «покороче»). Story, exploration,
    # freedom... are wishes: they move the score (taste.confidence) but never throw a game out.
    for axis, target in (limits.get("axes") or {}).items():
        if axis in HARD_AXES and abs(p["feel"].get(axis, 5) - target) > 3:
            return False
    return True


HARD_AXES = {"difficulty", "tension", "length", "grind", "social"}


def mood_fit(mood: str, g: dict, p: dict) -> float | None:
    """Bonus for the mood, or None when the game does not fit it at all."""
    f = p["feel"]
    year = time.gmtime().tm_year
    if mood == "evening":
        return None if f["length"] > 5 and f["replay"] < 6 else 0.1 * (6 - min(f["length"], 6)) / 6
    if mood == "story":
        return 0.06 * (f["story"] - 5)         # a strong pull, not a gate: «сюжет» is one wish of several
    if mood == "chill":
        return None if f["tension"] > 6 or f["difficulty"] > 7 else 0.05 * (5 - f["tension"])
    if mood == "challenge":
        return None if f["difficulty"] < 7 else 0.05 * (f["difficulty"] - 6)
    if mood == "coop":
        return None if not g.get("coop") and f["social"] < 6 else 0.1
    if mood == "gems":
        return None if g.get("owners", 0) > 500_000 or g.get("positive", 0) + g.get("negative", 0) > 30_000 else 0.1
    if mood == "fresh":
        return None if (g.get("release_year") or 0) < year - 1 else 0.05
    return 0.0


def feel_fit(taste: Taste, p: dict) -> tuple[float, list[str]]:
    total = weight = 0.0
    matches = []
    for k in AXES:
        c = taste.confidence.get(k, 0)
        if c <= 0 or k not in taste.feel:
            continue
        d = abs(taste.feel[k] - p["feel"][k]) / 10
        total += c * (1 - d)
        weight += c
        if c >= 0.5 and d <= 0.15:
            matches.append(k)
    return (total / weight if weight else 0.5), matches


def complaint_fit(taste: Taste, p: dict) -> tuple[float, list[tuple[str, bool]], list[str]]:
    """0..1 around 0.5 (no complaints). Taste complaints are judged against the player's taste."""
    score, notes, warnings = 0.0, [], []
    for c in p["complaints"]:
        w = SHARE_WEIGHT.get(c.get("share"), 0.3)
        if c["kind"] == "taste" and c["axis"] in taste.feel:
            pref = taste.feel[c["axis"]]
            conf = taste.confidence.get(c["axis"], 0)
            # "too slow" (pace, low) is good news for someone who likes it slow (pref <= 4).
            fits = pref <= 4 if c["direction"] == "low" else pref >= 6
            against = pref >= 6 if c["direction"] == "low" else pref <= 4
            if fits:
                score += 0.25 * w * conf
                notes.append((c["point"], True))
            elif against:
                score -= 0.25 * w * conf
                notes.append((c["point"], False))
        elif c["kind"] == "quality":
            score -= 0.15 * w
            warnings.append(c["point"])
    for k, sev in p["quality"].items():
        if sev >= 2 and k in ("bugs", "performance", "monetization", "abandoned", "servers"):
            score -= 0.1 * (sev - 1)
    return max(0.0, min(1.0, 0.5 + score)), notes, warnings


def recommend(catalog: Catalog, taste: Taste, passports: dict[int, dict], review_stats: dict[int, dict],
              *, mood: str = "any", exclude: set[int] = frozenset(), limit: int = 3,
              pool: int = 40, experience: Callable[[list[int]], dict[int, float]] | None = None,
              co_cands: dict[int, float] | None = None,
              coplay: Callable[[int], float | None] | None = None,
              limits: dict | None = None, scout: dict[int, float] | None = None) -> list[Pick]:
    """Best `limit` games. Candidates without a real passport get a heuristic one from their tags.
    `experience` scores candidates by how close players' descriptions are to the player's taste;
    `co_cands`/`coplay` say how much real Steam players who love the same games play them."""
    co_cands = co_cands or {}
    like, dislike = taste.like_vec, taste.dislike_vec
    pre = []
    for appid, vec in catalog.vecs.items():
        if appid in exclude or appid in taste.played:
            continue
        g = catalog.games[appid]
        if blocked(g, passports.get(appid), taste.dealbreakers):
            continue
        sim = cosine(like, vec) if like else 0.3
        if dislike:
            sim -= 0.5 * cosine(dislike, vec)
        co = max(co_cands.get(appid, 0.0), (scout or {}).get(appid, 0.0))
        if like and sim < MIN_TAG_SIM and co < 0.3:
            continue    # nothing in common with what they love, and their fellow players don't play it
        if taste.core and co < 0.3 and not (taste.core & set(vec)):
            continue    # none of what defines the reference game (its top player tags)
        prior = quality_score(review_stats.get(appid)) if appid in review_stats else _spy_quality(g)
        pre.append((sim + 0.3 * prior + 0.3 * co, sim, appid))
    pre.sort(reverse=True)

    exp = experience([a for _, _, a in pre[:pool * 3]]) if experience else {}
    picks = []
    for _, sim, appid in pre[:pool * 3]:
        g = catalog.games[appid]
        p = passports.get(appid)
        real = p is not None and p.get("_source") == "llm"
        p = p or heuristic_passport(g, review_stats.get(appid), [])
        bonus = mood_fit(mood, g, p)
        if bonus is None or not limits_fit(limits or {}, g, p, review_stats.get(appid)):
            continue
        if limits and limits.get("surprise"):
            bonus += random.uniform(0, 0.25)     # shake the order: a different good game each time
        ff, matches = feel_fit(taste, p)
        stats = review_stats.get(appid)
        q = quality_score(stats) if stats else _spy_quality(g)
        cf, notes, warnings = complaint_fit(taste, p)
        parts = {"tags": min(1.0, max(0.0, sim) / 0.6), "feel": ff, "quality": q, "complaints": cf}
        # A heuristic passport's feel is mostly guessed from the same tags: trust the tags instead.
        w_tags, w_feel = (0.35, 0.25) if real else (0.45, 0.15)
        w = {"tags": w_tags, "feel": w_feel, "quality": 0.25, "complaints": 0.15}
        # Extra signals take their share from the base four when there is data for this game.
        extra = {}
        if scout and appid in scout:
            # The model proposed it for what the player loved: a strong vote, reviews still check it.
            parts["scout"], extra["scout"] = scout[appid], 0.2
        if appid in exp:
            parts["experience"], extra["experience"] = exp[appid], 0.18
        cs = coplay(appid) if coplay else None
        if cs is not None:      # None = nobody harvested yet; 0.0 = data, but no link
            parts["coplay"], extra["coplay"] = min(1.0, cs / 0.6), 0.15
        if extra:
            keep = 1 - sum(extra.values())
            w = {**{k: v * keep for k, v in w.items()}, **extra}
        if taste.weights:
            raw = {k: v * taste.weights.get(k, 1.0) for k, v in w.items()}
            total = sum(raw.values())
            w = {k: v / total for k, v in raw.items()}      # keep the sum at 1
        score = sum(w[k] * parts[k] for k in w) + bonus
        picks.append(Pick(appid, score, parts, p, real, feel_matches=matches,
                          taste_notes=notes, warnings=warnings))
        if len(picks) >= pool:
            break
    picks.sort(key=lambda x: -x.score)
    chosen = diversify(picks, catalog, limit)
    for pick in chosen:
        pick.because = closest_liked(catalog, taste, pick.appid)
    return chosen


def diversify(picks: list[Pick], catalog: Catalog, limit: int, penalty: float = 0.3) -> list[Pick]:
    chosen: list[Pick] = []
    rest = list(picks)
    while rest and len(chosen) < limit:
        def adjusted(p: Pick) -> float:
            if not chosen:
                return p.score
            return p.score - penalty * max(cosine(catalog.vecs[p.appid], catalog.vecs[c.appid]) for c in chosen)
        best = max(rest, key=adjusted)
        chosen.append(best)
        rest.remove(best)
    return chosen


def closest_liked(catalog: Catalog, taste: Taste, appid: int) -> int | None:
    vec = catalog.vecs.get(appid)
    best, best_sim = None, 0.35
    for liked in taste.liked[:30]:
        other = catalog.vecs.get(liked)
        if other and vec:
            s = cosine(vec, other)
            if s > best_sim:
                best, best_sim = liked, s
    return best


def _spy_quality(g: dict) -> float:
    return wilson_lower(g.get("positive", 0), g.get("positive", 0) + g.get("negative", 0))
