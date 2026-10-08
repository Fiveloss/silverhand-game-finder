"""IGDB (Twitch's game database): any game a player names, not only Steam ones, and where it is sold.

Steam knows nothing about Epic exclusives (Alan Wake 2), console-only games or GOG-only games.
IGDB does: genres, themes, keywords, perspectives and modes, platforms, ratings, similar games and
the game's ids in other stores (external_games). This module finds a game by title, fetches one by
id, and returns a normalised dict whose "tags" are pseudo Steam user tags with vote-like weights,
so taste.tag_vector() treats a non-Steam game like a Steam one.

API: https://api-docs.igdb.com/ - POST https://api.igdb.com/v4/{endpoint} with an Apicalypse text
body, headers Client-ID and "Authorization: Bearer <app access token>"; the token comes from
https://id.twitch.tv/oauth2/token (client_credentials) and lives for ~60 days. 4 requests a second,
at most 8 open at once. Enum fields moved to tables in 2025 (game_type, external_game_source,
websites.type) and kept the old enum values as ids, which is what the constants below are.

Nothing here raises: find() and game() log and return None on any failure.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

import aiohttp

from .. import titles

log = logging.getLogger(__name__)

API = "https://api.igdb.com/v4"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
IMAGE_URL = "https://images.igdb.com/igdb/image/upload/t_{size}/{image_id}.jpg"

MIN_INTERVAL = 0.3          # the limit is 4 requests a second
MAX_OPEN = 4                # the limit is 8 open requests
AUTH_RETRY_AFTER = 300      # wrong credentials: do not hammer Twitch
CACHE_SIZE = 512

# game_types (formerly games.category): https://api-docs.igdb.com/#game-type
MAIN_GAME, DLC, EXPANSION, BUNDLE, STANDALONE, MOD, EPISODE, SEASON = 0, 1, 2, 3, 4, 5, 6, 7
REMAKE, REMASTER, EXPANDED, PORT, FORK, PACK, UPDATE = 8, 9, 10, 11, 12, 13, 14
# Lower is "more the game itself": a main game beats its remake, which beats any add-on.
TYPE_RANK = {MAIN_GAME: 0, REMAKE: 1, REMASTER: 1, EXPANDED: 1, PORT: 1, STANDALONE: 1, FORK: 2,
             EXPANSION: 3, EPISODE: 3, SEASON: 4, DLC: 4, BUNDLE: 4, PACK: 4, MOD: 5, UPDATE: 5}

# external_game_sources (formerly external_games.category): https://api-docs.igdb.com/#external-game
SRC_STEAM, SRC_GOG, SRC_MICROSOFT, SRC_EPIC, SRC_XBOX, SRC_PSN = 1, 5, 11, 26, 31, 36
# website_types (formerly websites.category): https://api-docs.igdb.com/#website
WEB_STEAM, WEB_EPIC, WEB_GOG = 13, 16, 17

FIELDS = ",".join([
    "name", "slug", "first_release_date", "game_type", "version_parent", "parent_game",
    "alternative_names.name", "genres.name", "themes.name", "keywords.name",
    "player_perspectives.name", "game_modes.name", "platforms.name", "similar_games.name",
    "total_rating", "total_rating_count", "rating", "rating_count",
    "aggregated_rating", "aggregated_rating_count", "summary", "storyline", "cover.image_id",
    "external_games.external_game_source", "external_games.uid", "external_games.url",
    "websites.type", "websites.url", "franchises.name",
    "involved_companies.company.name", "involved_companies.developer", "involved_companies.publisher",
])

# --- IGDB vocabulary -> Steam user tags

W_GENRE, W_THEME, W_PERSPECTIVE, W_MODE, W_KEYWORD, W_COMBO = 100, 80, 60, 60, 50, 90

GENRE_MAP = {   # https://api-docs.igdb.com/#genre (all 23)
    "Point-and-click": ["Point & Click", "Adventure"],
    "Fighting": ["Fighting"],
    "Shooter": ["Shooter"],
    "Music": ["Music", "Rhythm"],
    "Platform": ["Platformer"],
    "Puzzle": ["Puzzle"],
    "Racing": ["Racing", "Driving"],
    "Real Time Strategy (RTS)": ["RTS", "Strategy"],
    "Role-playing (RPG)": ["RPG"],
    "Simulator": ["Simulation"],
    "Sport": ["Sports"],
    "Strategy": ["Strategy"],
    "Turn-based strategy (TBS)": ["Turn-Based Strategy", "Strategy"],
    "Tactical": ["Tactical"],
    "Hack and slash/Beat 'em up": ["Hack and Slash", "Beat 'em up"],
    "Quiz/Trivia": ["Casual"],
    "Pinball": ["Arcade"],
    "Adventure": ["Adventure"],
    "Indie": ["Indie"],
    "Arcade": ["Arcade"],
    "Visual Novel": ["Visual Novel"],
    "Card & Board Game": ["Card Game", "Board Game"],
    "MOBA": ["MOBA"],
}

THEME_MAP = {   # https://api-docs.igdb.com/#theme
    "Action": ["Action"],
    "Fantasy": ["Fantasy"],
    "Science fiction": ["Sci-fi"],
    "Horror": ["Horror"],
    "Thriller": ["Thriller"],
    "Survival": ["Survival"],
    "Historical": ["Historical"],
    "Stealth": ["Stealth"],
    "Comedy": ["Comedy"],
    "Business": ["Management", "Economy"],
    "Drama": ["Drama"],
    "Non-fiction": ["Realistic"],
    "Sandbox": ["Sandbox"],
    "Educational": ["Casual"],
    "Kids": ["Family Friendly"],
    "Open world": ["Open World"],
    "Warfare": ["War", "Military"],
    "Party": ["Party Game"],
    "4X (explore, expand, exploit, and exterminate)": ["4X"],
    "Erotic": ["Nudity", "Mature"],
    "Mystery": ["Mystery"],
    "Romance": ["Romance"],
}

PERSPECTIVE_MAP = {   # https://api-docs.igdb.com/#player-perspective
    "First person": ["First-Person"],
    "Third person": ["Third Person"],
    "Bird view / Isometric": ["Isometric"],
    "Side view": ["Side Scroller"],
    "Text": ["Interactive Fiction"],
    "Auditory": ["Experimental"],
    "Virtual Reality": ["VR"],      # a Steam tag, though not in external.STEAM_TAGS
}

MODE_MAP = {   # https://api-docs.igdb.com/#game-mode
    "Single player": ["Singleplayer"],
    "Multiplayer": ["Multiplayer"],
    "Co-operative": ["Co-op"],
    "Split screen": ["Split Screen", "Local Multiplayer"],
    "Massively Multiplayer Online (MMO)": ["Massively Multiplayer"],
    "Battle Royale": ["Battle Royale"],
}

# IGDB keywords are free text; looked up after folding case, spaces and punctuation.
KEYWORD_MAP = {
    "metroidvania": "Metroidvania", "souls-like": "Souls-like", "soulslike": "Souls-like",
    "soulsborne": "Souls-like", "roguelike": "Roguelike", "roguelite": "Roguelite", "rogue-lite": "Roguelite",
    "action roguelike": "Action Roguelike", "psychological horror": "Psychological Horror",
    "survival horror": "Survival Horror", "cosmic horror": "Lovecraftian", "lovecraftian": "Lovecraftian",
    "cthulhu": "Lovecraftian", "supernatural": "Supernatural", "ghosts": "Supernatural",
    "paranormal": "Supernatural", "psychological": "Psychological", "gore": "Gore", "violence": "Violent",
    "dark fantasy": "Dark Fantasy", "cyberpunk": "Cyberpunk", "post-apocalyptic": "Post-apocalyptic",
    "post apocalyptic": "Post-apocalyptic", "zombies": "Zombies", "zombie": "Zombies", "dystopia": "Dystopian",
    "dystopian": "Dystopian", "steampunk": "Steampunk", "medieval": "Medieval", "western": "Western",
    "wild west": "Western", "noir": "Noir", "film noir": "Noir", "crime": "Crime", "mythology": "Mythology",
    "greek mythology": "Mythology", "norse mythology": "Mythology", "magic": "Magic", "aliens": "Aliens",
    "robots": "Robots", "vampires": "Vampire", "vampire": "Vampire", "dragons": "Dragons", "dragon": "Dragons",
    "dinosaurs": "Dinosaurs", "pirates": "Pirates", "ninja": "Ninja", "mechs": "Mechs", "mech": "Mechs",
    "superhero": "Superhero", "superheroes": "Superhero", "space": "Space", "world war ii": "World War II",
    "world war 2": "World War II", "alternate history": "Alternate History", "gothic": "Gothic",
    "detective": "Detective", "investigation": "Investigation", "conspiracy": "Conspiracy",
    "heist": "Heist", "time travel": "Time Travel", "time manipulation": "Time Manipulation",
    "bullet time": "Bullet Time", "female protagonist": "Female Protagonist",
    "multiple endings": "Multiple Endings", "choices matter": "Choices Matter",
    "branching storyline": "Choices Matter", "branching narrative": "Choices Matter",
    "moral choices": "Choices Matter", "moral decisions": "Choices Matter", "story rich": "Story Rich",
    "narrative": "Story Rich", "character customization": "Character Customization",
    "immersive sim": "Immersive Sim", "walking simulator": "Walking Simulator",
    "stealth": "Stealth", "parkour": "Parkour", "crafting": "Crafting", "base building": "Base Building",
    "open world": "Open World", "exploration": "Exploration", "sandbox": "Sandbox",
    "procedural generation": "Procedural Generation", "procedurally generated": "Procedural Generation",
    "permadeath": "Perma Death", "difficult": "Difficult", "hard": "Difficult",
    "deckbuilding": "Deckbuilding", "deck building": "Deckbuilding", "deckbuilder": "Deckbuilding",
    "card battler": "Card Battler", "dungeon crawler": "Dungeon Crawler", "loot": "Loot",
    "looter shooter": "Looter Shooter", "bullet hell": "Bullet Hell", "shoot 'em up": "Shoot 'Em Up",
    "shmup": "Shoot 'Em Up", "twin-stick shooter": "Twin Stick Shooter", "boss rush": "Boss Rush",
    "hack and slash": "Hack and Slash", "character action": "Character Action Game",
    "action rpg": "Action RPG", "jrpg": "JRPG", "crpg": "CRPG", "party-based": "Party-Based RPG",
    "tactical rpg": "Tactical RPG", "turn-based combat": "Turn-Based Combat", "turn-based": "Turn-Based",
    "real-time with pause": "Real-Time with Pause", "city builder": "City Builder",
    "city building": "City Builder", "colony sim": "Colony Sim", "colony management": "Colony Sim",
    "automation": "Automation", "management": "Management", "tower defense": "Tower Defense",
    "auto battler": "Auto Battler", "farming": "Farming Sim", "life simulation": "Life Sim",
    "dating sim": "Dating Sim", "romance": "Romance", "visual novel": "Visual Novel",
    "interactive fiction": "Interactive Fiction", "hidden object": "Hidden Object",
    "escape room": "Escape Room", "physics": "Physics", "puzzle platformer": "Puzzle Platformer",
    "precision platformer": "Precision Platformer", "2d platformer": "Platformer",
    "3d platformer": "Platformer", "side-scrolling": "Side Scroller", "side scroller": "Side Scroller",
    "top-down": "Top-Down", "top down": "Top-Down", "isometric": "Isometric", "pixel art": "Pixel Graphics",
    "pixel graphics": "Pixel Graphics", "hand-drawn": "Hand-drawn", "anime": "Anime", "retro": "Retro",
    "cel shading": "Stylized", "cinematic": "Cinematic", "atmospheric": "Atmospheric",
    "relaxing": "Relaxing", "cozy": "Cozy", "cute": "Cute", "wholesome": "Wholesome",
    "emotional": "Emotional", "surreal": "Surreal", "philosophical": "Philosophical",
    "dark humor": "Dark Humor", "satire": "Satire", "parody": "Parody", "fmv": "FMV",
    "full motion video": "FMV", "quick time events": "Quick-Time Events", "qte": "Quick-Time Events",
    "rhythm": "Rhythm", "hunting": "Hunting", "fishing": "Fishing", "underwater": "Underwater",
    "space simulation": "Space Sim", "flight simulator": "Flight", "naval": "Naval",
    "vehicular combat": "Vehicular Combat", "martial arts": "Martial Arts", "swordplay": "Swordplay",
    "sword fighting": "Swordplay", "sniper": "Sniper", "hacking": "Hacking", "cooking": "Cooking",
    "trains": "Trains", "mining": "Mining", "trading": "Trading", "diplomacy": "Diplomacy",
    "grand strategy": "Grand Strategy", "wargame": "Wargame", "hex grid": "Hex Grid",
    "tactical shooter": "Tactical Shooter", "hero shooter": "Hero Shooter", "arena shooter": "Arena Shooter",
    "boomer shooter": "Boomer Shooter", "extraction shooter": "Extraction Shooter",
    "local co-op": "Local Co-Op", "online co-op": "Online Co-Op", "co-op": "Co-op", "pvp": "PvP",
    "pve": "PvE", "social deduction": "Social Deduction", "episodic": "Episodic", "remake": "Remake",
    "remaster": "Remaster", "nonlinear": "Nonlinear", "linear": "Linear", "lore": "Lore-Rich",
}

# Two signals that together mean a narrower Steam tag: ({needed tags}, tag).
COMBOS = [
    ({"Shooter", "First-Person"}, "FPS"),
    ({"Shooter", "Third Person"}, "Third-Person Shooter"),
    ({"RPG", "Action"}, "Action RPG"),
    ({"Adventure", "Action"}, "Action-Adventure"),
    ({"Horror", "Survival"}, "Survival Horror"),
    ({"RPG", "Tactical"}, "Tactical RPG"),
    ({"Turn-Based Strategy", "Tactical"}, "Turn-Based Tactics"),
    ({"Platformer", "Side Scroller"}, "2D"),
    ({"Open World", "Survival", "Crafting"}, "Open World Survival Craft"),
    ({"RPG", "Massively Multiplayer"}, "MMORPG"),
]


def _fold(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s).lower().replace("&", "and"))


_KEYWORDS = {_fold(k): v for k, v in KEYWORD_MAP.items()}
_GENRES = {_fold(k): v for k, v in GENRE_MAP.items()}
_THEMES = {_fold(k): v for k, v in THEME_MAP.items()}
_PERSPECTIVES = {_fold(k): v for k, v in PERSPECTIVE_MAP.items()}
_MODES = {_fold(k): v for k, v in MODE_MAP.items()}


def map_tags(genres=(), themes=(), keywords=(), perspectives=(), modes=()) -> dict[str, int]:
    """IGDB names -> {Steam tag: vote-like weight}. A tag named by several sources gets a bonus."""
    hits: dict[str, list[int]] = {}

    def add(names, table, weight):
        for n in names or ():
            for tag in table.get(_fold(n), ()):
                hits.setdefault(tag, []).append(weight)

    add(genres, _GENRES, W_GENRE)
    add(themes, _THEMES, W_THEME)
    add(perspectives, _PERSPECTIVES, W_PERSPECTIVE)
    add(modes, _MODES, W_MODE)
    for k in keywords or ():
        tag = _KEYWORDS.get(_fold(k))
        if tag:
            hits.setdefault(tag, []).append(W_KEYWORD)
    tags = {t: min(120, max(ws) + 10 * (len(ws) - 1)) for t, ws in hits.items()}
    for needed, tag in COMBOS:
        if needed <= tags.keys() and tag not in tags:
            tags[tag] = W_COMBO
    return tags


# --- response parsing

def _ref_id(v):
    """A reference field comes back as an id, or as an object when expanded."""
    if isinstance(v, dict):
        v = v.get("id")
    return v if isinstance(v, int) else None


def _names(items) -> list[str]:
    out = []
    for x in items or ():
        n = x.get("name") if isinstance(x, dict) else None
        if n and n not in out:
            out.append(n)
    return out


def _rating(v) -> float | None:
    return round(float(v), 1) if isinstance(v, (int, float)) else None


def _uid(uid):
    s = str(uid or "").strip()
    return int(s) if s.isdigit() else (s or None)


EPIC_SLUG = re.compile(r"epicgames\.com/(?:store/)?(?:[a-z]{2}(?:-[a-zA-Z]{2})?/)?(?:p|product)/([^/?#]+)", re.I)
STEAM_APP = re.compile(r"store\.steampowered\.com/app/(\d+)", re.I)
GOG_GAME = re.compile(r"gog\.com/(?:[a-z]{2}/)?game/([^/?#]+)", re.I)


def extract_stores(external_games, websites) -> tuple[dict, dict]:
    """({"steam": appid, "gog": id, "epic": slug}, {store: url}) from external_games and websites.
    GOG falls back to the URL slug and Epic to the external uid when the better id is missing."""
    stores = {"steam": None, "gog": None, "epic": None}
    urls: dict[str, str] = {}
    epic_uid = None
    for e in external_games or ():
        if not isinstance(e, dict):
            continue
        src = _ref_id(e.get("external_game_source")) or _ref_id(e.get("category"))
        uid, url = _uid(e.get("uid")), e.get("url") or ""
        if src == SRC_STEAM and isinstance(uid, int) and not stores["steam"]:
            stores["steam"] = uid
        elif src == SRC_GOG and uid and not stores["gog"]:
            stores["gog"] = uid
            if url:
                urls.setdefault("gog", url)
        elif src == SRC_EPIC:
            m = EPIC_SLUG.search(url)
            if m and not stores["epic"]:
                stores["epic"] = m.group(1)
            epic_uid = epic_uid or uid
        elif src == SRC_PSN and url:
            urls.setdefault("playstation", url)
        elif src in (SRC_XBOX, SRC_MICROSOFT) and url:
            urls.setdefault("xbox", url)
    for w in websites or ():
        if not isinstance(w, dict):
            continue
        kind, url = _ref_id(w.get("type")) or _ref_id(w.get("category")), w.get("url") or ""
        if kind == WEB_STEAM or STEAM_APP.search(url):
            m = STEAM_APP.search(url)
            if m and not stores["steam"]:
                stores["steam"] = int(m.group(1))
        elif kind == WEB_EPIC or EPIC_SLUG.search(url):
            m = EPIC_SLUG.search(url)
            if m and not stores["epic"]:
                stores["epic"] = m.group(1)
            if url:
                urls.setdefault("epic", url)
        elif kind == WEB_GOG or GOG_GAME.search(url):
            if url:
                urls["gog"] = url       # the store page beats the external uid's url
            m = GOG_GAME.search(url)
            if m and not stores["gog"]:
                stores["gog"] = m.group(1)
    if stores["epic"] and "epic" not in urls:
        urls["epic"] = f"https://store.epicgames.com/p/{stores['epic']}"
    if not stores["epic"] and epic_uid:
        stores["epic"] = str(epic_uid)      # an Epic catalog id, not a slug: no URL from it
    if stores["steam"]:
        urls["steam"] = f"https://store.steampowered.com/app/{stores['steam']}/"
    return stores, urls


def normalise(g: dict) -> dict:
    """An IGDB game object (FIELDS expanded) -> the dict the rest of the bot uses."""
    genres, themes = _names(g.get("genres")), _names(g.get("themes"))
    keywords = _names(g.get("keywords"))
    perspectives, modes = _names(g.get("player_perspectives")), _names(g.get("game_modes"))
    stores, urls = extract_stores(g.get("external_games"), g.get("websites"))
    ts = g.get("first_release_date")
    year = datetime.fromtimestamp(ts, timezone.utc).year if isinstance(ts, (int, float)) else None
    cover = (g.get("cover") or {}).get("image_id") if isinstance(g.get("cover"), dict) else None
    mapped = [k for k in keywords if _fold(k) in _KEYWORDS]
    companies = [c for c in g.get("involved_companies") or () if isinstance(c, dict)]
    return {
        "igdb_id": g.get("id"),
        "name": g.get("name") or "",
        "slug": g.get("slug") or "",
        "url": f"https://www.igdb.com/games/{g['slug']}" if g.get("slug") else None,
        "year": year,
        "summary": (g.get("summary") or "").strip(),
        "storyline": (g.get("storyline") or "").strip(),
        "game_type": _ref_id(g.get("game_type")),
        "platforms": _names(g.get("platforms")),
        "stores": stores,
        "store_urls": urls,
        "genres": genres,
        "themes": themes,
        # the ones that mean something to the tag map first; IGDB lists can run to hundreds
        "keywords": (mapped + [k for k in keywords if k not in mapped])[:30],
        "perspectives": perspectives,
        "modes": modes,
        "similar": _names(g.get("similar_games"))[:10],
        "franchises": _names(g.get("franchises")),
        "developers": [c["company"]["name"] for c in companies
                       if c.get("developer") and isinstance(c.get("company"), dict) and c["company"].get("name")],
        "publishers": [c["company"]["name"] for c in companies
                       if c.get("publisher") and isinstance(c.get("company"), dict) and c["company"].get("name")],
        "user_rating": _rating(g.get("rating")),
        "user_votes": int(g.get("rating_count") or 0),
        "critic_rating": _rating(g.get("aggregated_rating")),
        "critic_votes": int(g.get("aggregated_rating_count") or 0),
        "total_rating": _rating(g.get("total_rating")),
        "cover_url": IMAGE_URL.format(size="cover_big", image_id=cover) if cover else None,
        "tags": map_tags(genres, themes, keywords, perspectives, modes),
    }


# --- "где купить"

CONSOLES = [   # (platform name prefix in IGDB, label), newest first
    ("PlayStation 5", "PlayStation 5"), ("PlayStation 4", "PlayStation 4"),
    ("Xbox Series X|S", "Xbox Series X|S"), ("Xbox One", "Xbox One"),
    ("Nintendo Switch 2", "Nintendo Switch 2"), ("Nintendo Switch", "Nintendo Switch"),
]


def where_to_buy(g: dict) -> list[tuple[str, str | None]]:
    """[(label, url or None)]: PC stores first, then current consoles, for a "где купить" line."""
    urls, stores, platforms = g.get("store_urls") or {}, g.get("stores") or {}, g.get("platforms") or []
    out = []
    for key, label in (("steam", "Steam"), ("gog", "GOG"), ("epic", "Epic Games Store")):
        if stores.get(key) or urls.get(key):
            out.append((label, urls.get(key)))
    seen = set()
    for prefix, label in CONSOLES:
        if any(p == prefix for p in platforms) and label not in seen:
            seen.add(label)
            family = "playstation" if "PlayStation" in label else "xbox" if "Xbox" in label else ""
            out.append((label, urls.get(family)))
    if not out and any(p.startswith("PC") for p in platforms):
        out.append(("PC", None))
    return out


# --- matching

YEAR_HINT = re.compile(r"\s*\((19\d\d|20\d\d)\)\s*$")


def _clean_query(text: str) -> str:
    return re.sub(r'["\\;]', " ", text).strip()[:100]


def _year_of(g: dict) -> int | None:
    ts = g.get("first_release_date")
    return datetime.fromtimestamp(ts, timezone.utc).year if isinstance(ts, (int, float)) else None


def _match_score(query: str, g: dict) -> float:
    names = [g.get("name") or ""] + _names(g.get("alternative_names"))
    return max((titles.score(query, n) for n in names if n), default=0.0)


def pick(query: str, results: list[dict], threshold: float = 0.85) -> dict | None:
    """The result that surely is the game named, or None.
    Same sequel number (titles.score), the closest name, main games before DLC and editions,
    then the year in "(2023)" if given, then the most rated. A bare series name that only matches
    the part before a subtitle of several different games ("Batman") is ambiguous: None."""
    m = YEAR_HINT.search(query)
    year = int(m.group(1)) if m else None
    q = YEAR_HINT.sub("", query).strip()
    scored = []
    for g in results or ():
        if not isinstance(g, dict) or not g.get("name"):
            continue
        s = _match_score(q, g)
        if s < threshold:
            continue
        rank = TYPE_RANK.get(_ref_id(g.get("game_type")) if g.get("game_type") is not None else MAIN_GAME, 3)
        if g.get("version_parent"):
            rank = max(rank, 2)         # an edition of another game
        scored.append((s, rank, g))
    if not scored:
        return None
    best_rank = min(r for _, r, _ in scored)
    if best_rank <= 1:
        scored = [x for x in scored if x[1] <= 1]   # never an add-on when the game itself matched
    top = max(s for s, _, _ in scored)
    best = [x for x in scored if x[0] >= top - 1e-9]
    if top < 1.0:
        distinct = {titles.core(g["name"]) for _, r, g in best if r <= 1}
        if len(distinct) > 1:
            return None
    if year:
        dated = [x for x in best if _year_of(x[2]) == year]
        best = dated or best
    best.sort(key=lambda x: (x[1], -int(x[2].get("total_rating_count") or 0),
                             -int(x[2].get("rating_count") or 0)))
    return best[0][2]


class Igdb:
    def __init__(self, http, client_id: str, client_secret: str):
        self.http = http
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.interval = MIN_INTERVAL
        self._token: str | None = None
        self._token_until = 0.0
        self._auth_failed_until = 0.0
        self._token_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._next_at = 0.0
        self._open = asyncio.Semaphore(MAX_OPEN)
        self._games: dict[int, dict] = {}
        self._found: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # --- public
    async def find(self, title: str) -> dict | None:
        """The game a player named, normalised, or None when IGDB has no confident match."""
        if not self.enabled or not (title or "").strip():
            return None
        key = titles.normalize(title)
        try:
            if key in self._found:
                return self._games.get(self._found[key])
            base = YEAR_HINT.sub("", title).strip()
            hint = title[len(base):]
            queries = [base]
            expanded = titles.expand(base)
            if expanded and expanded != titles.normalize(base):
                queries.insert(0, expanded)     # "ведьмак 3" -> "the witcher 3"
            for q in queries:
                body = (f'search "{_clean_query(q)}"; fields {FIELDS}; '
                        f'where version_parent = null; limit 25;')
                results = await self._query("games", body)
                if results is None:
                    return None             # an error, logged; not "not found"
                g = pick(q + hint, results) or pick(title, results)
                if g:
                    out = normalise(g)
                    self._remember(key, out)
                    return out
            return None
        except Exception:
            log.exception("igdb find failed for %r", title)
            return None

    async def game(self, igdb_id: int) -> dict | None:
        if not self.enabled:
            return None
        try:
            igdb_id = int(igdb_id)
            if igdb_id in self._games:
                return self._games[igdb_id]
            results = await self._query("games", f"fields {FIELDS}; where id = {igdb_id}; limit 1;")
            if not results:
                return None
            out = normalise(results[0])
            self._remember(None, out)
            return out
        except Exception:
            log.exception("igdb game %r failed", igdb_id)
            return None

    # --- internals
    def _remember(self, key: str | None, g: dict) -> None:
        if len(self._games) >= CACHE_SIZE:
            self._games.clear()
            self._found.clear()
        if g.get("igdb_id") is not None:
            self._games[g["igdb_id"]] = g
            if key:
                self._found[key] = g["igdb_id"]

    async def _wait_turn(self) -> None:
        async with self._turn_lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_at = max(now, self._next_at) + self.interval

    async def _get_token(self) -> str | None:
        async with self._token_lock:
            now = time.monotonic()
            if self._token and now < self._token_until:
                return self._token
            if now < self._auth_failed_until:
                return None
            s = await self.http.session()
            params = {"client_id": self.client_id, "client_secret": self.client_secret,
                      "grant_type": "client_credentials"}
            async with s.post(TOKEN_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
                body = await r.json(content_type=None) if r.status == 200 else None
                if r.status != 200 or not isinstance(body, dict) or not body.get("access_token"):
                    log.error("igdb: Twitch token request failed (HTTP %s); check IGDB_CLIENT_ID/SECRET",
                              r.status)
                    self._auth_failed_until = now + AUTH_RETRY_AFTER
                    return None
            ttl = int(body.get("expires_in") or 3600)
            self._token = body["access_token"]
            self._token_until = now + max(60, ttl - 600)    # renew ten minutes early
            return self._token

    async def _query(self, endpoint: str, body: str) -> list | None:
        """POST an Apicalypse body; the JSON list, or None after logging what went wrong."""
        for attempt in range(3):
            token = await self._get_token()
            if not token:
                return None
            await self._wait_turn()
            headers = {"Client-ID": self.client_id, "Authorization": f"Bearer {token}",
                       "Accept": "application/json", "Content-Type": "text/plain"}
            s = await self.http.session()
            try:
                async with self._open:
                    async with s.post(f"{API}/{endpoint}", data=body.encode("utf-8"), headers=headers,
                                      timeout=aiohttp.ClientTimeout(total=30)) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            return data if isinstance(data, list) else None
                        text = (await r.text())[:300]
                        status = r.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("igdb %s: %s", endpoint, e)
                await asyncio.sleep(1 + attempt)
                continue
            if status == 401:               # revoked or expired early: one fresh token
                log.info("igdb: token rejected, renewing")
                self._token, self._token_until = None, 0.0
                continue
            if status == 429 or status >= 500:
                await asyncio.sleep(1 + attempt)
                continue
            log.warning("igdb %s: HTTP %s %s", endpoint, status, text)
            return None
        log.warning("igdb %s: giving up after retries", endpoint)
        return None
