"""The game's passport: what it actually is and how it feels, according to the people who played it.

With a key for a free LLM API (Google Gemini, Groq as a fallback), the model reads a sample of
recent and helpful reviews (Steam, plus Reddit when configured) and fills a fixed shape. Without one, a heuristic passport is built from the
player-voted tags and keyword counts in the reviews: rougher, but the recommender still works.

The important part is the complaints: each one is either about quality (bugs, performance,
monetization) or about taste, with the axis and direction it points to. "Too slow" is a
complaint on pace, low; for someone who likes slow games it is a reason to recommend.
"""

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# key: (low end, high end) in Russian, as the bot shows them.
AXES = {
    "pace": ("неторопливая", "динамичная"),
    "difficulty": ("лёгкая", "хардкорная"),
    "story": ("сюжет не важен", "сюжет в центре"),
    "freedom": ("линейная", "полная свобода"),
    "complexity": ("простые механики", "глубокие системы"),
    "grind": ("без гринда", "много гринда"),
    "tension": ("расслабляющая", "напряжённая"),
    "combat": ("почти без боёв", "бои — основа"),
    "exploration": ("исследовать нечего", "исследование — основа"),
    "social": ("одиночная", "игра с людьми"),
    "length": ("короткая", "очень длинная"),
    "replay": ("на один раз", "бесконечная реиграбельность"),
}
QUALITY_KEYS = ("bugs", "performance", "monetization", "abandoned", "ending", "ru_localization", "servers")
SHARES = ("most", "many", "some")

PASSPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "real_genres": {"type": "array", "items": {"type": "string"}},
        "core_loop": {"type": "string"},
        "feel": {
            "type": "object",
            "properties": {k: {"type": "integer"} for k in AXES},
            "required": list(AXES),
            "additionalProperties": False,
        },
        "moods": {"type": "array", "items": {"type": "string"}},
        "praise": {"type": "array", "items": {
            "type": "object",
            "properties": {"point": {"type": "string"}, "share": {"type": "string", "enum": list(SHARES)}},
            "required": ["point", "share"], "additionalProperties": False}},
        "complaints": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "point": {"type": "string"},
                "share": {"type": "string", "enum": list(SHARES)},
                "kind": {"type": "string", "enum": ["taste", "quality"]},
                "axis": {"type": "string", "enum": [*AXES, "none"]},
                "direction": {"type": "string", "enum": ["high", "low", "none"]},
            },
            "required": ["point", "share", "kind", "axis", "direction"], "additionalProperties": False}},
        "quality": {
            "type": "object",
            "properties": {k: {"type": "integer"} for k in QUALITY_KEYS},
            "required": list(QUALITY_KEYS),
            "additionalProperties": False,
        },
        "state_now": {"type": "string"},
        "best_for": {"type": "string"},
        "avoid_if": {"type": "string"},
        "compared_to": {"type": "array", "items": {"type": "string"}},
        "hours_typical": {"type": "string"},
    },
    "required": ["summary", "real_genres", "core_loop", "feel", "moods", "praise", "complaints",
                 "quality", "state_now", "best_for", "avoid_if", "compared_to", "hours_typical"],
    "additionalProperties": False,
}

SYSTEM = """You analyse what players really say about a video game, for a recommendation bot that \
must be more honest than store pages and paid review sites. You get store facts, player-voted tags, \
review statistics and a sample of player reviews (negatives are over-sampled on purpose; the real \
positive share is in the statistics). Judge the game by what the reviews describe, not by its \
marketing. Write every text field in Russian, concise and concrete, no marketing language; when \
addressing the reader use the informal «ты», never «Вы».

Fields:
- summary: 2-3 sentences: what the game actually is and what playing it feels like.
- real_genres: 2-5 precise genres as players would name them (e.g. "иммерсив-сим", "метроидвания", \
"рогалик-колодостроитель"), even when the store genre says otherwise.
- core_loop: one sentence: what you do minute to minute.
- feel: 0-10 for each axis. pace 0 slow, 10 frantic; difficulty 0 trivial, 10 punishing; story 0 none, \
10 the story is the point; freedom 0 corridor, 10 sandbox; complexity 0 simple, 10 deep systems; \
grind 0 none, 10 heavy; tension 0 relaxing, 10 stressful/scary; combat 0 none, 10 the core; \
exploration 0 none, 10 the core; social 0 solo, 10 built for playing with others; length 0 under 5 h, \
5 about 20-30 h, 10 hundreds of hours; replay 0 once, 10 endless.
- moods: 2-4 short moods it suits ("на вечер", "залипнуть надолго", "с друзьями", "погрустить"...).
- praise: up to 5 concrete things players praise, with how common (most/many/some).
- complaints: up to 6 concrete complaints. kind "quality" for objective problems (bugs, performance, \
monetization, abandoned development, broken balance, bad port); kind "taste" when the complaint is \
really about a trait some players love (too slow, too hard, too much text, too long, too grindy). \
For taste complaints set axis and direction (e.g. "слишком медленно" -> pace, low; "слишком сложно" -> \
difficulty, high). For quality complaints axis and direction are "none".
- quality: severity 0-3 of each problem as reviews describe it now (0 none, 3 serious). ru_localization: \
problems with Russian translation or its absence if Russian reviewers complain.
- state_now: one sentence: is the game better or worse now than at launch, judging by review dates.
- best_for / avoid_if: one sentence each, about player taste, not demographics.
- compared_to: up to 5 other games reviewers compare it to.
- hours_typical: how long players say it takes, e.g. "20-30 ч на сюжет".
If the reviews do not say something, give your best estimate from the tags and keep text fields short."""


def clamp(v, lo: int = 0, hi: int = 10) -> int:
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return (lo + hi) // 2


def normalize(p: dict) -> dict:
    """Make any passport (from a model, from heuristics or from an old row) safe to use."""
    p = dict(p)
    p["feel"] = {k: clamp((p.get("feel") or {}).get(k, 5)) for k in AXES}
    p["quality"] = {k: clamp((p.get("quality") or {}).get(k, 0), 0, 3) for k in QUALITY_KEYS}
    for k in ("real_genres", "moods", "compared_to"):
        p[k] = [str(x) for x in _as_list(p.get(k)) if x and not isinstance(x, (dict, list))]
    # A model may drop a field or answer with bare strings: every point becomes a full dict here,
    # so no card or score ever meets a missing key.
    p["praise"] = [{"point": x["point"], "share": x["share"]} for x in map(_point, _as_list(p.get("praise"))) if x]
    complaints = []
    for c in map(_point, _as_list(p.get("complaints"))):
        if not c:
            continue
        c["kind"] = c.get("kind") if c.get("kind") in ("taste", "quality") else "quality"
        if c.get("axis") not in AXES or c["kind"] != "taste" or c.get("direction") not in ("high", "low"):
            c["axis"], c["direction"] = "none", "none"
        complaints.append(c)
    p["complaints"] = complaints
    for k in ("summary", "core_loop", "state_now", "best_for", "avoid_if", "hours_typical"):
        p[k] = str(p.get(k) or "")
    return p


def _as_list(v) -> list:
    return list(v) if isinstance(v, (list, tuple)) else []


def _point(x) -> dict | None:
    """A praise or complaint item as {"point": str, "share": one of SHARES, ...}, or None if empty."""
    if isinstance(x, str):
        x = {"point": x}
    if not isinstance(x, dict) or not str(x.get("point") or "").strip():
        return None
    x = dict(x)
    x["point"] = str(x["point"]).strip()
    if x.get("share") not in SHARES:
        x["share"] = "some"
    return x


def build_prompt(game: dict, stats: dict | None, sample: list[dict], extra: list[dict]) -> str:
    top_tags = sorted((game.get("tags") or {}).items(), key=lambda kv: -kv[1])[:20]
    facts = {
        "name": game["name"],
        "released": game.get("release_date"),
        "store_genres": game.get("genres"),
        "player_tags_by_votes": [t for t, _ in top_tags],
        "store_description": game.get("short_desc"),
        "early_access": bool(game.get("early_access")),
        "in_app_purchases": bool(game.get("mtx")),
        "russian": {"text": bool(game.get("ru_text")), "audio": bool(game.get("ru_audio"))},
    }
    if stats:
        facts["review_stats"] = {
            "all_time_positive": _pct(stats.get("all_share")), "reviews_total": stats.get("total"),
            "recent_positive": _pct(stats.get("recent_share")), "recent_window_days": stats.get("recent_span_days"),
            "positive_among_10h_plus": _pct(stats.get("engaged_share")),
            "negatives_from_under_2h": _pct(stats.get("quick_negative_share")),
        }
    lines = [f"GAME FACTS\n{json.dumps(facts, ensure_ascii=False)}", "\nPLAYER REVIEWS (Steam, GOG)"]
    for r in sample:
        mark = "+" if r["up"] else "-"
        hours = f"{r['hours']}h " if r.get("hours") is not None else ""
        lines.append(f"[{r.get('source') or 'steam'} {mark} {hours}{r['date']} {r['lang']}] {r['text']}")
    if extra:
        lines.append("\nOTHER DISCUSSIONS")
        for r in extra:
            lines.append(f"[{r.get('source', '?')} score {r.get('score', 0)}] {r['text']}")
    return "\n".join(lines)


def _pct(v):
    return None if v is None else f"{round(v * 100)}%"


@dataclass
class Provider:
    """An OpenAI-compatible chat endpoint with a free tier."""
    name: str
    url: str
    key: str
    model: str
    max_reviews: int        # free tiers cap tokens per minute; Groq's is small
    resting_until: float = 0.0
    kind: str = ""          # "flash" | "lite" | "groq": which tasks it is spent on (Analyst.order)


# Which provider does which job. The free Gemini Flash quota is tiny (about 20 calls a day per
# model), so it is kept for the judge, where its quality shows most; the background reading of
# reviews never touches it. Quick calls a player waits for go to the fastest providers first.
TASK_ORDER = {
    "passport": ("lite", "groq"),
    "intent": ("groq", "lite", "flash"),
    "scout": ("groq", "lite", "flash"),
    "card": ("groq", "lite", "flash"),
    "judge": ("flash", "groq", "lite"),
}
# Seconds one attempt may take. A free tier sometimes hangs on a call it would answer at once
# when asked again, so a player-facing call that hangs is tried once more, then the next provider.
TASK_TIMEOUT = {"intent": 15, "scout": 20, "card": 20, "judge": 25, "passport": 180}
TASK_ATTEMPTS = {"passport": 1}
# A thinking model answers a short structured question about twice as fast with little thinking.
QUICK_EXTRA = {"flash": {"reasoning_effort": "low"}, "lite": {"reasoning_effort": "low"}}


def providers_for(analyst, task: str) -> list:
    """The providers to try for a task, in order, skipping those resting after a limit."""
    order = getattr(analyst, "order", None)
    ps = order(task) if callable(order) else list(getattr(analyst, "providers", None) or [])
    return [p for p in ps if p.resting_until <= time.time()]


async def post_llm(http, p, payload: dict, task: str) -> tuple[int, object]:
    """(status, body) of one chat call for `task`, with its timeout and one retry after a hang."""
    payload = {**payload, **payload_extra(p, task)}
    attempts = TASK_ATTEMPTS.get(task, 2)
    for attempt in range(attempts):
        try:
            return await http.post_json(p.url, payload, headers={"Authorization": f"Bearer {p.key}"},
                                        timeout=TASK_TIMEOUT.get(task, 60))
        except (asyncio.TimeoutError, TimeoutError):
            if attempt == attempts - 1:
                raise
            log.info("%s: %s hung, asking again", task, p.name)
    raise RuntimeError("unreachable")


def payload_extra(p, task: str) -> dict:
    return dict(QUICK_EXTRA.get(getattr(p, "kind", ""), {})) if task != "passport" else {}


def check_status(p, status: int, body) -> None:
    """Raise RateLimited for a limit or an overload, with the rest the answer asks for."""
    if status == 429:
        raise RateLimited(limit_rest(p, body))
    if status in (500, 502, 503, 504):
        raise RateLimited(60)       # "high demand": Google's own overload, usually gone in a minute


def limit_rest(p, body) -> int:
    """How long to leave a provider alone after a 429. A daily quota (Gemini says so in the answer,
    with the time until it resets) rests it until then; a per-minute one 90 s, 600 s if repeated."""
    err = body[0] if isinstance(body, list) and body else body
    err = (err or {}).get("error") if isinstance(err, dict) else None
    daily, retry = False, 0
    for d in (err or {}).get("details") or [] if isinstance(err, dict) else []:
        if not isinstance(d, dict):
            continue
        for v in d.get("violations") or []:
            if isinstance(v, dict) and "PerDay" in str(v.get("quotaId", "")):
                daily = True
        delay = str(d.get("retryDelay") or "")
        if delay.endswith("s"):
            try:
                retry = int(float(delay[:-1]))
            except ValueError:
                pass
    if daily:
        return max(600, min(retry or 6 * 3600, 26 * 3600))
    return 600 if getattr(p, "resting_until", 0) else 90



class Analyst:
    def __init__(self, http, providers: list[Provider]):
        self.http = http
        self.providers = [p for p in providers if p.key]

    @classmethod
    def from_config(cls, http, cfg) -> "Analyst":
        return cls(http, [
            Provider("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                     cfg.gemini_api_key, cfg.gemini_model, max_reviews=60, kind="flash"),
            # Same free key, a lighter model with its own, much bigger quota.
            Provider("Gemini Lite", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                     cfg.gemini_api_key, cfg.gemini_lite_model, max_reviews=60, kind="lite"),
            Provider("Groq", "https://api.groq.com/openai/v1/chat/completions",
                     cfg.groq_api_key, cfg.groq_model, max_reviews=25, kind="groq"),
        ])

    def order(self, task: str) -> list[Provider]:
        """Providers for a task (TASK_ORDER), resting ones included; unknown kinds go last."""
        ranks = TASK_ORDER.get(task)
        if not ranks:
            return list(self.providers)
        known = sorted((p for p in self.providers if p.kind in ranks), key=lambda p: ranks.index(p.kind))
        return known + [p for p in self.providers if not p.kind]

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    def describe(self) -> str:
        return " → ".join(f"{p.name} ({p.model})" for p in self.providers) or "эвристика"

    async def passport(self, game: dict, stats: dict | None, sample: list[dict],
                       extra: list[dict] | None = None) -> tuple[dict, int, int, str]:
        """(passport, input tokens, output tokens, "provider/model"). Tries each provider that is
        not resting after a rate limit; raises when none could answer, and the caller falls back."""
        errors = []
        for p in providers_for(self, "passport"):
            prompt = build_prompt(game, stats, sample[:p.max_reviews], (extra or [])[:p.max_reviews // 3])
            try:
                data, tin, tout = await self._call(p, prompt)
                return normalize(data), tin, tout, f"{p.name}/{p.model}"
            except RateLimited as e:
                p.resting_until = time.time() + e.seconds
                errors.append(f"{p.name}: limit, resting {e.seconds}s")
            except Exception as e:
                errors.append(f"{p.name}: {type(e).__name__} {e}")
        raise RuntimeError("; ".join(errors) or "all providers are resting after rate limits")

    async def _call(self, p: Provider, prompt: str) -> tuple[dict, int, int]:
        payload = {
            "model": p.model,
            "messages": [{"role": "system", "content": SYSTEM + SCHEMA_HINT},
                         {"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 8192,
        }
        status, body = await post_llm(self.http, p, payload, "passport")
        check_status(p, status, body)
        if status != 200 or not body:
            err = body.get("error", {}) if isinstance(body, dict) else {}
            if isinstance(err, list):
                err = err[0] if err else {}
            msg = err.get("message", body) if isinstance(err, dict) else err
            raise RuntimeError(f"HTTP {status}: {str(msg)[:200]}")
        choice = body["choices"][0]
        text = (choice.get("message") or {}).get("content") or ""
        usage = body.get("usage") or {}
        return _parse_json(text), int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


class RateLimited(Exception):
    def __init__(self, seconds: int):
        super().__init__(f"rate limited for {seconds}s")
        self.seconds = seconds


# Free providers get JSON mode, not schema enforcement: spell the shape out; normalize() fixes the rest.
SCHEMA_HINT = (
    "\n\nReturn ONE JSON object and nothing else, with exactly these keys and types: "
    + json.dumps({
        "summary": "string", "real_genres": ["string"], "core_loop": "string",
        "feel": {k: "integer 0-10" for k in AXES}, "moods": ["string"],
        "praise": [{"point": "string", "share": "most|many|some"}],
        "complaints": [{"point": "string", "share": "most|many|some", "kind": "taste|quality",
                        "axis": "|".join([*AXES, "none"]), "direction": "high|low|none"}],
        "quality": {k: "integer 0-3" for k in QUALITY_KEYS},
        "state_now": "string", "best_for": "string", "avoid_if": "string",
        "compared_to": ["string"], "hours_typical": "string",
    }, ensure_ascii=False))


def _parse_json(text: str) -> dict:
    """JSON from a model's answer: also inside ```json fences, with trailing commas, or cut short
    after the last complete field (the light model sometimes stops mid-object)."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    body = m.group(0) if m else text[text.find("{"):] if "{" in text else ""
    if not body:
        raise json.JSONDecodeError("no JSON object", text, 0)
    body = re.sub(r",\s*([}\]])", r"\1", body)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    # Cut short: drop the unfinished tail and close what is open.
    cut = body[:body.rfind(",")] if "," in body else body
    stack = []
    in_str = esc = False
    for ch in cut:
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    return json.loads(cut + "".join(reversed(stack)))


# --- heuristics: no key, or the API failed

# tag -> {axis: push}; pushes are scaled by the tag's share of votes.
TAG_AXES = {
    "Fast-Paced": {"pace": 4}, "Action": {"pace": 2, "combat": 2}, "Bullet Hell": {"pace": 4, "difficulty": 3},
    "Relaxing": {"tension": -4, "pace": -3}, "Cozy": {"tension": -4, "pace": -2}, "Wholesome": {"tension": -3},
    "Walking Simulator": {"pace": -4, "combat": -4, "story": 3}, "Turn-Based": {"pace": -3},
    "Turn-Based Strategy": {"pace": -3, "complexity": 2}, "Turn-Based Tactics": {"pace": -3, "complexity": 2},
    "Difficult": {"difficulty": 4}, "Souls-like": {"difficulty": 4, "combat": 3}, "Masocore": {"difficulty": 5},
    "Precision Platformer": {"difficulty": 3}, "Casual": {"difficulty": -3, "complexity": -2},
    "Story Rich": {"story": 4}, "Visual Novel": {"story": 4, "combat": -4, "pace": -3}, "Choices Matter": {"story": 3},
    "Lore-Rich": {"story": 2}, "Narration": {"story": 2}, "Interactive Fiction": {"story": 4},
    "Open World": {"freedom": 4, "exploration": 3, "length": 2}, "Sandbox": {"freedom": 5, "replay": 2},
    "Linear": {"freedom": -4}, "Nonlinear": {"freedom": 2}, "Exploration": {"exploration": 4},
    "Metroidvania": {"exploration": 3, "combat": 2},
    "Strategy": {"complexity": 2}, "Grand Strategy": {"complexity": 5, "length": 3}, "4X": {"complexity": 4, "length": 3},
    "Management": {"complexity": 2}, "Automation": {"complexity": 4, "length": 3}, "Simulation": {"complexity": 1},
    "Colony Sim": {"complexity": 3, "replay": 3}, "Deckbuilding": {"complexity": 2, "replay": 2},
    "Grinding": {"grind": 4}, "Grind": {"grind": 4}, "MMORPG": {"grind": 4, "social": 4, "length": 4},
    "Looter Shooter": {"grind": 3}, "Farming Sim": {"grind": 2, "tension": -3},
    "Horror": {"tension": 4}, "Psychological Horror": {"tension": 4}, "Survival Horror": {"tension": 4},
    "Survival": {"tension": 2, "grind": 2}, "Atmospheric": {"exploration": 1},
    "Puzzle": {"combat": -3, "pace": -2}, "Hack and Slash": {"combat": 4, "pace": 2}, "Shooter": {"combat": 4},
    "FPS": {"combat": 4, "pace": 2}, "Combat": {"combat": 3}, "Character Action Game": {"combat": 4, "pace": 3},
    "Multiplayer": {"social": 3}, "Co-op": {"social": 3}, "Online Co-Op": {"social": 3}, "PvP": {"social": 4},
    "Massively Multiplayer": {"social": 4}, "Singleplayer": {"social": -3},
    "Short": {"length": -4}, "Great Soundtrack": {},
    "Replay Value": {"replay": 4}, "Roguelike": {"replay": 4, "difficulty": 2}, "Roguelite": {"replay": 4},
    "Procedural Generation": {"replay": 3}, "Rogue-like": {"replay": 4}, "RPG": {"length": 2, "story": 1},
    "JRPG": {"length": 3, "story": 2, "grind": 1}, "CRPG": {"length": 3, "story": 3, "complexity": 2},
}

# Player tags that name a genre (the rest describe mood, art, themes or features).
GENRE_TAGS = {
    "Action", "Adventure", "RPG", "Strategy", "Simulation", "Puzzle", "Platformer", "Shooter", "FPS",
    "Third-Person Shooter", "Metroidvania", "Roguelike", "Roguelite", "Rogue-like", "Action Roguelike",
    "Souls-like", "Hack and Slash", "Beat 'em up", "Fighting", "Racing", "Sports", "Survival", "Survival Horror",
    "Horror", "Psychological Horror", "Open World", "Sandbox", "Visual Novel", "Interactive Fiction",
    "Point & Click", "Walking Simulator", "Immersive Sim", "Stealth", "Tactical", "Turn-Based Tactics",
    "Turn-Based Strategy", "Grand Strategy", "4X", "RTS", "Real Time Tactics", "City Builder", "Colony Sim",
    "Base Building", "Automation", "Management", "Card Game", "Deckbuilding", "Card Battler", "JRPG", "CRPG",
    "Action RPG", "Party-Based RPG", "Tactical RPG", "MMORPG", "MOBA", "Battle Royale", "Looter Shooter",
    "Bullet Hell", "Shoot 'Em Up", "Twin Stick Shooter", "Arcade", "Rhythm", "Precision Platformer",
    "Puzzle Platformer", "Physics", "Detective", "Mystery", "Exploration", "Life Sim", "Farming Sim",
    "Dating Sim", "Hidden Object", "Tower Defense", "Auto Battler", "Extraction Shooter", "Hero Shooter",
    "Space Sim", "Flight", "Driving", "Economy", "Crafting", "Dungeon Crawler", "Character Action Game",
    "Boomer Shooter", "Arena Shooter", "Narrative", "Choose Your Own Adventure", "Investigation",
}

# (regex, Russian text, kind, axis, direction) read from negative reviews.
COMPLAINTS = [
    (r"\bbug|glitch|баг|глюк", "баги", "quality", "bugs", None),
    (r"optimi[sz]|stutter|\bfps\b|perform|оптимиз|фриз|тормоз|лага", "оптимизация", "quality", "performance", None),
    (r"crash|вылет", "вылеты", "quality", "bugs", None),
    (r"microtransact|pay.?to.?win|\bp2w\b|gacha|донат|гача|battle pass", "донат и монетизация", "quality", "monetization", None),
    (r"abandon|no updates|dead game|заброш|разраб.{0,20}забил", "разработка заброшена", "quality", "abandoned", None),
    (r"server|always.?online|сервер", "сервера и онлайн", "quality", "servers", None),
    (r"ending|концовк", "концовка", "quality", "ending", None),
    (r"перевод|локализ|translation", "перевод", "quality", "ru_localization", None),
    (r"\bslow|boring|tedious|walking sim|медленн|скучн|нудн|затянут", "слишком медленно и затянуто", "taste", "pace", "low"),
    (r"too hard|frustrat|unfair|punishing|слишком сложн|нечестн|бесит", "слишком сложно", "taste", "difficulty", "high"),
    (r"too easy|слишком легк|слишком лёгк", "слишком легко", "taste", "difficulty", "low"),
    (r"grind|гринд|фарм", "много гринда", "taste", "grind", "high"),
    (r"too short|short game|коротк", "коротко", "taste", "length", "low"),
    (r"too long|затянут|слишком длин", "слишком длинно", "taste", "length", "high"),
    (r"too much text|wall of text|reading|много текста|читать", "много текста", "taste", "story", "high"),
    (r"repetiti|same thing|однообраз|одно и то же", "однообразно", "taste", "replay", "low"),
]
PRAISE = [
    (r"story|plot|narrat|сюжет|истори", "сюжет"),
    (r"atmospher|атмосфер", "атмосфера"),
    (r"soundtrack|music|музык|саундтрек", "музыка"),
    (r"art style|visual|graphics|beautiful|графи|визуал|красив", "визуал"),
    (r"gameplay|mechanic|геймплей|механик", "геймплей"),
    (r"explor|исследов", "исследование"),
    (r"combat|fight|бои|боёв|боевк", "боевая система"),
    (r"characters|персонаж", "персонажи"),
    (r"co-?op|friends|кооп|с друзьями", "игра с друзьями"),
    (r"replay|реиграб", "реиграбельность"),
]


def heuristic_passport(game: dict, stats: dict | None, sample: list[dict]) -> dict:
    tags = game.get("tags") or {}
    top = max(tags.values(), default=1) or 1
    push = {k: 0.0 for k in AXES}
    for tag, votes in tags.items():
        for axis, p in TAG_AXES.get(tag, {}).items():
            push[axis] += p * (votes / top) ** 0.5
    if game.get("coop") or game.get("pvp"):
        push["social"] += 1
    if game.get("store_ok") == 1 and not game.get("single"):
        push["social"] += 3
    # Without a tag saying so, a game is not grindy and not built around other players.
    base = {**{k: 5.0 for k in AXES}, "grind": 3.0, "social": 3.0}
    # tanh keeps two strong tags from pinning an axis at 10.
    feel = {k: base[k] + 5 * math.tanh(push[k] / 6) for k in AXES}
    hours = (stats or {}).get("median_hours_positive")
    if hours:
        feel["length"] = 2 + min(8, hours / 8)

    def shares(texts, patterns):
        n = max(1, len(texts))
        return [(sum(bool(re.search(p[0], t, re.I)) for t in texts) / n, p) for p in patterns]

    neg = [r["text"] for r in sample if not r["up"]]
    pos = [r["text"] for r in sample if r["up"]]
    complaints, quality = [], {k: 0 for k in QUALITY_KEYS}
    for share, (_, text, kind, key, direction) in sorted(shares(neg, COMPLAINTS), key=lambda x: -x[0]):
        if share < 0.12 or len(neg) < 4:
            continue
        label = "most" if share > 0.5 else "many" if share > 0.25 else "some"
        if kind == "quality":
            quality[key] = max(quality[key], 3 if share > 0.4 else 2 if share > 0.2 else 1)
            complaints.append({"point": text, "share": label, "kind": "quality", "axis": "none", "direction": "none"})
        else:
            complaints.append({"point": text, "share": label, "kind": "taste", "axis": key, "direction": direction})
    praise = [{"point": text, "share": "most" if s > 0.5 else "many" if s > 0.25 else "some"}
              for s, (_, text) in sorted(shares(pos, PRAISE), key=lambda x: -x[0]) if s >= 0.15][:5]
    top_tags = [t for t, _ in sorted(tags.items(), key=lambda kv: -kv[1]) if t in GENRE_TAGS]
    return normalize({
        "summary": game.get("short_desc", "")[:300],
        "real_genres": top_tags[:4],
        "core_loop": "",
        "feel": feel,
        "moods": [],
        "praise": praise,
        "complaints": complaints[:6],
        "quality": quality,
        "state_now": "",
        "best_for": "",
        "avoid_if": "",
        "compared_to": [],
        "hours_typical": f"~{hours:g} ч у тех, кому понравилось" if hours else "",
    })
