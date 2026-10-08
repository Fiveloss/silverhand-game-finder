"""Games that are not on Steam, as references: "something like Alan Wake 2" (an Epic exclusive).

The bot cannot read Steam reviews or SteamSpy tags for such a game, so one free-LLM call asks the
model what it knows about it and returns a reference card: player-tag weights from a fixed Steam
vocabulary (shaped like SteamSpy votes, so taste.tag_vector() takes them as is), the 12 feel axes
and the 4-7 things people love about it. card_as_game() and card_passport() turn the card into a
game row and a passport, so the rest of the pipeline treats it like a Steam game with a negative
appid. Recommendations still come from the Steam catalog only.

Cards are cached in external_games; a game the model does not know is cached as such for a day.
Nothing here raises: describe() returns None on any failure.
"""

import hashlib
import json
import logging
import re
import sqlite3
import time

from .analyst import AXES, GENRE_TAGS, RateLimited, _parse_json, clamp, normalize

log = logging.getLogger(__name__)

CARD_VERSION = 1
CARD_TTL = 90 * 86400        # what a model knows about an existing game hardly changes
UNKNOWN_TTL = 86400          # "never heard of it" is retried after a day (typo, new release)
MAX_TAGS = 20
MIN_TAGS = 3                 # fewer valid tags than this: the model does not really know the game
MAX_ASPECTS = 7
LABEL_MAX = 28

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS external_games (
    title_key TEXT PRIMARY KEY,
    data TEXT NOT NULL,          -- the card as JSON, or {"known": false}
    updated_at INTEGER NOT NULL
);
"""

# Common Steam user tags. The model may only use these, spelled exactly like this.
_TAG_TEXT = """
Action|Adventure|RPG|Strategy|Simulation|Puzzle|Platformer|Shooter|FPS|Third-Person Shooter|
Third Person|First-Person|Top-Down|Isometric|Side Scroller|2D|3D|2.5D|Pixel Graphics|Cartoony|
Anime|Realistic|Stylized|Hand-drawn|Minimalist|Retro|Cinematic|Beautiful|Colorful|Dark|
Atmospheric|Story Rich|Narration|Lore-Rich|Choices Matter|Multiple Endings|Character Customization|
Great Soundtrack|Mystery|Detective|Investigation|Psychological|Psychological Horror|Survival Horror|
Horror|Lovecraftian|Supernatural|Gore|Violent|Dark Fantasy|Fantasy|Sci-fi|Cyberpunk|Space|
Post-apocalyptic|Zombies|Dystopian|Steampunk|Medieval|Historical|Military|War|World War II|
Western|Noir|Crime|Mythology|Magic|Aliens|Robots|Vampire|Comedy|Funny|Dark Humor|Emotional|
Sad|Relaxing|Cozy|Wholesome|Cute|Family Friendly|Philosophical|Surreal|Psychedelic|Thriller|
Drama|Romance|Mature|Nudity|Female Protagonist|Open World|Sandbox|Linear|Nonlinear|Exploration|
Metroidvania|Souls-like|Difficult|Masocore|Fast-Paced|Hack and Slash|Character Action Game|
Spectacle fighter|Beat 'em up|Fighting|Combat|Swordplay|Stealth|Immersive Sim|Action-Adventure|
Action RPG|JRPG|CRPG|Party-Based RPG|Tactical RPG|Strategy RPG|Turn-Based|Turn-Based Combat|
Turn-Based Strategy|Turn-Based Tactics|Real-Time|Real Time Tactics|Real-Time with Pause|RTS|
Grand Strategy|4X|Wargame|Hex Grid|Tactical|City Builder|Colony Sim|Base Building|Building|Crafting|
Survival|Open World Survival Craft|Automation|Management|Economy|Resource Management|Farming Sim|
Life Sim|Dating Sim|God Game|Visual Novel|Interactive Fiction|Choose Your Own Adventure|Point & Click|
Walking Simulator|Hidden Object|Escape Room|Logic|Word Game|Puzzle Platformer|Precision Platformer|
Physics|Roguelike|Roguelite|Action Roguelike|Rogue-like|Procedural Generation|Replay Value|
Deckbuilding|Card Game|Card Battler|Board Game|Tabletop|Dungeon Crawler|Looter Shooter|Loot|
Bullet Hell|Shoot 'Em Up|Twin Stick Shooter|Arcade|Rhythm|Music|Racing|Combat Racing|Driving|Sports|
Flight|Space Sim|Vehicular Combat|Naval|Tower Defense|Auto Battler|MOBA|Battle Royale|
Extraction Shooter|Hero Shooter|Boomer Shooter|Arena Shooter|Tactical Shooter|Military Shooter|
Sniper|Parkour|Quick-Time Events|Time Manipulation|Time Travel|Bullet Time|Gun Customization|
Destruction|Boss Rush|Martial Arts|Singleplayer|Multiplayer|Co-op|Online Co-Op|Local Co-Op|
Local Multiplayer|PvP|PvE|Massively Multiplayer|MMORPG|Competitive|Team-Based|Split Screen|
Asynchronous Multiplayer|Party Game|Social Deduction|Short|Episodic|Grinding|Inventory Management|
Perma Death|Level Editor|Moddable|Remake|Remaster|Sequel|Classic|Cult Classic|Old School|Indie|
Casual|Superhero|Ninja|Pirates|Dragons|Mechs|Dinosaurs|Cats|Hunting|Fishing|Underwater|Nature|
Underground|Heist|Conspiracy|Political|Political Sim|Diplomacy|Trading|Dynamic Narration|
Alternate History|Gothic|Faith|Satire|Parody|FMV|Experimental|Abstract|Artificial Intelligence|
Hacking|Programming|Mining|Cooking|Agriculture|Transportation|Trains|Job Simulator|Medical Sim|
Outbreak Sim|Unforgiving|Addictive|Controller
"""
STEAM_TAGS = sorted({t.strip() for t in re.split(r"[|\n]", _TAG_TEXT) if t.strip()} | GENRE_TAGS)


def _fold(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower().replace("&", "and"))


# Lookup that forgives case, spaces and hyphens: "soulslike", "Souls Like" -> "Souls-like".
_TAG_INDEX = {_fold(t): t for t in STEAM_TAGS}
_TAG_INDEX.update({_fold(a): t for a, t in {
    "Soulslike": "Souls-like", "Third Person Shooter": "Third-Person Shooter", "TPS": "Third-Person Shooter",
    "First Person Shooter": "FPS", "Sci fi": "Sci-fi", "Science Fiction": "Sci-fi",
    "Rogue-lite": "Roguelite", "Coop": "Co-op", "Cooperative": "Co-op", "Story-Rich": "Story Rich",
    "Open-World": "Open World", "Metroidvania-like": "Metroidvania", "Point and Click": "Point & Click",
}.items() if t in STEAM_TAGS})


def canon_tag(tag) -> str | None:
    return _TAG_INDEX.get(_fold(str(tag))) if tag else None


def title_key(title: str) -> str:
    """'Alan Wake II™' and 'alan  wake ii' share a key; Russian titles keep their letters."""
    s = str(title or "").casefold().replace("ё", "е").replace("&", " and ")
    s = re.sub(r"[™®©]", "", s)
    s = re.sub(r"[^\w]+", " ", s)
    return " ".join(s.split())[:120]


def ensure_schema(conn) -> None:
    conn = _conn(conn)
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def _conn(db) -> sqlite3.Connection:
    """Accept a db.Db or a bare sqlite3 connection."""
    return getattr(db, "conn", db)


SYSTEM = """You are a video game encyclopedia for a recommendation bot. The user names a game that \
may not be on Steam (Epic or console exclusive, an old game, a mobile game). Describe that game the \
way Steam players would tag it, so the bot can find similar games on Steam.

STRICT RULES
- Do not invent anything. If you are not sure which game the title means, or you do not know the \
game well enough to describe how it plays, return {"known": false} and nothing else.
- "name": the game's canonical (usually English) title, fixing typos and Russian spellings.
- "year": first release year, or null if unsure. "platforms": e.g. "PC", "PS5", "Xbox Series", \
"Switch", "iOS", "Android". "on_steam": true only if the game is sold on Steam.
- "tags": 8-20 Steam user tags with vote-like weights 1-100 (100 = the most defining tag, as players \
would vote). Use ONLY tags from the ALLOWED TAGS list below, spelled exactly as there.
- "feel": 0-10 for each axis. pace 0 slow, 10 frantic; difficulty 0 trivial, 10 punishing; story \
0 none, 10 the story is the point; freedom 0 corridor, 10 sandbox; complexity 0 simple, 10 deep \
systems; grind 0 none, 10 heavy; tension 0 relaxing, 10 stressful/scary; combat 0 none, 10 the core; \
exploration 0 none, 10 the core; social 0 solo, 10 built for playing with others; length 0 under \
5 h, 5 about 20-30 h, 10 hundreds of hours; replay 0 once, 10 endless.
- "aspects": 4-7 DISTINCT things players love about THIS game, concrete to it (not generic \
"good graphics"). label_ru: a short Russian label, at most 28 characters (e.g. "Атмосфера страха"). \
axes: the 1-3 feel axes this aspect is about with the value it implies. tags: 1-3 allowed tags \
it maps to. words_ru: how a Russian player would say it, under 100 characters.
- "steam_similar": up to 5 games that ARE on Steam and that players often compare it to. Only \
real titles you are sure exist on Steam; an empty list is fine.

Return ONE JSON object and nothing else:
""" + json.dumps({
    "known": True, "name": "string", "year": "integer|null", "platforms": ["string"], "on_steam": False,
    "tags": {"Allowed Tag": "integer 1-100"}, "feel": {k: "integer 0-10" for k in AXES},
    "aspects": [{"label_ru": "string", "axes": {"tension": 9}, "tags": ["Allowed Tag"], "words_ru": "string"}],
    "steam_similar": ["string"],
}, ensure_ascii=False) + "\n\nALLOWED TAGS: " + " | ".join(STEAM_TAGS)


# --- validation

def _short(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    if len(s) <= n:
        return s
    cut = s[:n + 1].rsplit(" ", 1)[0] if " " in s[:n + 1] else s[:n]
    return cut[:n].rstrip(" ,.;:-—")


def _tags(raw) -> dict[str, int]:
    if isinstance(raw, list):          # ["Horror", ...] instead of a dict: rank stands in for votes
        raw = {t: 100 - 4 * i for i, t in enumerate(raw) if isinstance(t, str)}
    if not isinstance(raw, dict):
        return {}
    nums = {}
    for t, v in raw.items():
        tag = canon_tag(t)
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if tag and x > 0 and x == x:
            nums[tag] = max(nums.get(tag, 0.0), x)
    if nums and max(nums.values()) <= 1.0:      # 0..1 shares instead of 1..100 votes
        nums = {t: x * 100 for t, x in nums.items()}
    out = {t: max(1, min(100, int(round(x)))) for t, x in nums.items()}
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:MAX_TAGS])


def _aspects(raw) -> list[dict]:
    out, seen = [], set()
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, dict):
            continue
        label = _short(a.get("label_ru"), LABEL_MAX)
        key = label.casefold()
        if not label or key in seen:
            continue
        seen.add(key)
        axes = a.get("axes") if isinstance(a.get("axes"), dict) else {}
        tags = a.get("tags") if isinstance(a.get("tags"), list) else []
        out.append({
            "label_ru": label,
            "axes": {k: clamp(v) for k, v in axes.items() if k in AXES},
            "tags": list(dict.fromkeys(t for t in map(canon_tag, tags) if t))[:3],
            "words_ru": _short(a.get("words_ru"), 160),
        })
        if len(out) >= MAX_ASPECTS:
            break
    return out


def _year(v) -> int | None:
    try:
        y = int(v)
    except (TypeError, ValueError):
        return None
    return y if 1970 <= y <= time.gmtime().tm_year + 2 else None


def _strings(raw, limit: int, size: int) -> list[str]:
    out = []
    for s in raw if isinstance(raw, list) else []:
        s = _short(s, size) if isinstance(s, str) else ""
        if s and s.casefold() not in {x.casefold() for x in out}:
            out.append(s)
    return out[:limit]


def validate(data, title: str) -> dict | None:
    """A clean card, or None when the model does not (really) know the game."""
    if not isinstance(data, dict) or data.get("known") is not True and str(data.get("known")).lower() != "true":
        return None
    tags = _tags(data.get("tags"))
    if len(tags) < MIN_TAGS:
        return None
    name = _short(data.get("name"), 100) or _short(title, 100)
    feel_raw = data.get("feel") if isinstance(data.get("feel"), dict) else {}
    on_steam = data.get("on_steam")
    return {
        "v": CARD_VERSION,
        "name": name,
        "known": True,
        "year": _year(data.get("year")),
        "platforms": _strings(data.get("platforms"), 8, 30),
        "on_steam": on_steam is True or str(on_steam).lower() == "true",
        "tags": tags,
        "feel": {k: clamp(feel_raw.get(k, 5)) for k in AXES},
        "aspects": _aspects(data.get("aspects")),
        "steam_similar": [s for s in _strings(data.get("steam_similar"), 6, 80)
                          if title_key(s) != title_key(name)][:5],
    }


# --- cache

def _cached(conn, key: str) -> tuple[bool, dict | None]:
    """(hit, card). A fresh negative entry is a hit with no card."""
    row = conn.execute("SELECT data, updated_at FROM external_games WHERE title_key=?", (key,)).fetchone()
    if not row:
        return False, None
    data, at = json.loads(row[0]), row[1]
    age = time.time() - at
    if not data.get("known"):
        return (True, None) if age < UNKNOWN_TTL else (False, None)
    if data.get("v") != CARD_VERSION or age >= CARD_TTL:
        return False, None
    return True, data


def _store(conn, keys, data: dict) -> None:
    now = int(time.time())
    blob = json.dumps(data, ensure_ascii=False)
    conn.executemany("INSERT OR REPLACE INTO external_games(title_key, data, updated_at) VALUES(?, ?, ?)",
                     [(k, blob, now) for k in dict.fromkeys(keys) if k])
    conn.commit()


# --- the call

async def _call(http, p, title: str) -> dict:
    payload = {
        "model": p.model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": f"GAME TITLE: {title}"}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 3000,
    }
    status, body = await http.post_json(p.url, payload, headers={"Authorization": f"Bearer {p.key}"})
    if status == 429:
        raise RateLimited(600 if p.resting_until else 90)
    if status != 200 or not isinstance(body, dict) or not body.get("choices"):
        raise RuntimeError(f"HTTP {status}")
    text = ((body["choices"][0] or {}).get("message") or {}).get("content") or ""
    return _parse_json(text)


async def describe(analyst, db_conn, title: str) -> dict | None:
    """The reference card for a game the model knows, from cache or one LLM call; None otherwise."""
    try:
        title = " ".join(str(title or "").split())[:120]
        key = title_key(title)
        if not key:
            return None
        conn = _conn(db_conn)
        ensure_schema(conn)
        hit, card = _cached(conn, key)
        if hit:
            return card
        for p in getattr(analyst, "providers", None) or []:
            if p.resting_until > time.time():
                continue
            try:
                data = await _call(analyst.http, p, title)
            except RateLimited as e:
                p.resting_until = time.time() + e.seconds
                log.info("external %r: %s rate limited, resting %ss", title, p.name, e.seconds)
                continue
            except Exception as e:
                log.warning("external %r: %s failed: %s", title, p.name, e)
                continue
            card = validate(data, title)
            if card is None:
                _store(conn, [key], {"known": False, "title": title})
                return None
            card["model"] = f"{p.name}/{p.model}"
            _store(conn, [key, title_key(card["name"])], card)
            return card
        return None     # no provider answered: not cached, the next request tries again
    except Exception:
        log.exception("external describe failed for %r", title)
        return None


# --- the card as the rest of the pipeline sees games

def card_appid(card: dict) -> int:
    """Stable negative id from the name: never collides with a Steam appid."""
    h = hashlib.sha1(title_key(card.get("name", "")).encode("utf-8")).hexdigest()
    return -(int(h[:12], 16) % 2_000_000_000 + 1)


def card_as_game(card: dict) -> dict:
    """A games-table-shaped row for the card. store_ok=0 keeps store-based dealbreakers off it."""
    name = card.get("name") or "?"
    feel = card.get("feel") or {}
    year = card.get("year")
    return {
        "appid": card_appid(card), "name": name, "name_lc": name.lower(),
        "tags": dict(card.get("tags") or {}), "genres": [], "categories": [],
        "short_desc": "; ".join(a["words_ru"] for a in card.get("aspects") or [] if a.get("words_ru"))[:600],
        "release_date": str(year) if year else "", "release_year": year,
        "price_cents": None, "currency": "", "discount": 0,
        "ru_text": 0, "ru_audio": 0, "early_access": 0, "mtx": 0, "online_only": 0,
        "single": int(feel.get("social", 0) < 7), "coop": 0, "pvp": 0, "drm_notice": "",
        "owners": 0, "positive": 0, "negative": 0, "is_dlc": 0, "adult": 0,
        "store_ok": 0, "spy_ok": 0, "deck": 0, "deck_notes": "", "proton": "", "deck_ok": 0,
        "updated_at": int(time.time()),
        # not in the games table: marks the row as a reference from outside Steam
        "external": True, "on_steam": bool(card.get("on_steam")),
        "platforms": list(card.get("platforms") or []), "steam_similar": list(card.get("steam_similar") or []),
    }


def card_passport(card: dict) -> dict:
    """A passport (analyst.normalize shape) from the card: feel as is, praise from the aspects."""
    aspects = card.get("aspects") or []
    tags = sorted((card.get("tags") or {}).items(), key=lambda kv: -kv[1])
    return normalize({
        "summary": "; ".join(a["words_ru"] for a in aspects[:3] if a.get("words_ru")),
        "real_genres": [t for t, _ in tags if t in GENRE_TAGS][:4],
        "core_loop": "",
        "feel": dict(card.get("feel") or {}),
        "moods": [],
        "praise": [{"point": a["label_ru"], "share": "most" if i < 2 else "many" if i < 4 else "some"}
                   for i, a in enumerate(aspects[:5])],
        "complaints": [],
        "quality": {},
        "state_now": "",
        "best_for": "",
        "avoid_if": "",
        "compared_to": list(card.get("steam_similar") or []),
        "hours_typical": "",
        "aspects": [dict(a) for a in aspects],
        "_source": "external",
        "_model": card.get("model", ""),
    })
