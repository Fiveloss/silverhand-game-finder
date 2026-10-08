"""The genres that define a game, from its player tags, and whether two games share one.

A reference game is anchored by its most pronounced genres (the genre tags with the most player
votes). A candidate must share at least one of them, whatever proposed it: a game everybody plays
(or one the model liked) is no match for Resident Evil unless it is a horror game too.
Close genres count as one family: "Survival Horror" and "Psychological Horror" are both horror.
"""

from .analyst import GENRE_TAGS as _GENRE_TAGS

GENRE_TAGS = set(_GENRE_TAGS) | {"Open World Survival Craft"}

# Genres too broad to define a game when it has a more specific one (most games are "Action").
BROAD = {"Action", "Adventure", "Simulation", "Strategy", "RPG", "Casual", "Open World", "Exploration",
         "Arcade", "Narrative", "Physics", "Tactical", "Management", "Economy"}

FAMILIES = {
    "horror": {"Horror", "Survival Horror", "Psychological Horror"},
    "roguelike": {"Roguelike", "Roguelite", "Rogue-like", "Rogue-lite", "Action Roguelike"},
    "shooter": {"Shooter", "FPS", "Third-Person Shooter", "Boomer Shooter", "Arena Shooter",
                "Looter Shooter", "Hero Shooter", "Extraction Shooter", "Twin Stick Shooter"},
    "shmup": {"Bullet Hell", "Shoot 'Em Up"},
    "soulslike": {"Souls-like"},
    "metroidvania": {"Metroidvania"},
    "platformer": {"Platformer", "Precision Platformer", "Puzzle Platformer"},
    "crpg": {"CRPG", "Party-Based RPG", "Tactical RPG"},
    "action rpg": {"Action RPG", "Hack and Slash", "Character Action Game"},
    "strategy": {"Turn-Based Strategy", "Grand Strategy", "4X", "RTS", "Real Time Tactics",
                 "Turn-Based Tactics"},
    "builder": {"City Builder", "Colony Sim", "Base Building", "Automation"},
    "cards": {"Card Game", "Deckbuilding", "Card Battler"},
    "narrative": {"Visual Novel", "Interactive Fiction", "Choose Your Own Adventure", "Walking Simulator",
                  "Point & Click"},
    "detective": {"Detective", "Investigation", "Mystery"},
    "life": {"Life Sim", "Farming Sim", "Dating Sim"},
    "survival": {"Survival", "Open World Survival Craft"},
}
_FAMILY_OF = {t: fam for fam, tags in FAMILIES.items() for t in tags}

RU = {
    "horror": "хоррор", "roguelike": "рогалик", "shooter": "шутер", "shmup": "shoot 'em up",
    "soulslike": "соулслайк", "metroidvania": "метроидвания", "platformer": "платформер", "crpg": "партийная RPG",
    "action rpg": "экшен-RPG", "strategy": "стратегия", "builder": "строительство", "cards": "карточная",
    "narrative": "сюжетная адвенчура", "detective": "детектив", "life": "симулятор жизни", "survival": "выживание",
    "Action": "экшен", "Adventure": "приключение", "RPG": "RPG", "JRPG": "JRPG", "Simulation": "симулятор",
    "Puzzle": "головоломка", "Stealth": "стелс", "Racing": "гонки", "Sports": "спорт", "Fighting": "файтинг",
    "Open World": "открытый мир", "Immersive Sim": "иммерсив-сим", "Rhythm": "ритм-игра",
    "Sandbox": "песочница", "Strategy": "стратегия", "Casual": "казуальная",
}


def family(tag: str) -> str:
    return _FAMILY_OF.get(tag, tag)


def defining(tags: dict | None, n: int = 3, share: float = 0.4) -> list[str]:
    """The game's most pronounced genre families, best first: genre tags with at least `share` of the
    top genre tag's votes. Broad genres count only when the game has no specific one."""
    genre = sorted(((t, v) for t, v in (tags or {}).items() if t in GENRE_TAGS and v > 0), key=lambda kv: -kv[1])
    if not genre:
        return []
    specific = [(t, v) for t, v in genre if t not in BROAD]
    pool = specific or genre
    top = pool[0][1]
    out: list[str] = []
    for t, v in pool:
        if v >= share * top and family(t) not in out:
            out.append(family(t))
        if len(out) >= n:
            break
    return out


def present(tags: dict | None, share: float = 0.2) -> set[str]:
    """Genre families a game really has: tags with at least `share` of its top tag's votes."""
    tags = tags or {}
    top = max(tags.values(), default=0) or 1
    return {family(t) for t, v in tags.items() if t in GENRE_TAGS and v >= share * top}


def matches(anchors: list[list[str]], have: set[str]) -> bool:
    """A candidate with genre families `have` fits when, for some reference game, it shares that
    game's main genre, or two of its defining genres (Resident Evil 4: horror, or shooter + survival)."""
    return any(a and (a[0] in have or len(set(a) & have) >= 2) for a in anchors)


EN = {"crpg": "party RPG", "builder": "building", "cards": "card game", "narrative": "story adventure",
      "life": "life sim", "shmup": "shoot 'em up", "soulslike": "soulslike", "action rpg": "action RPG"}


def ru(fam: str) -> str:
    """A genre family or tag as the player reads it: Russian for Russian players, else English."""
    from .i18n import is_ru
    return RU.get(fam, fam) if is_ru() else EN.get(fam, fam)


# --- format: how the game is played on screen (dimension, combat timing, camera)
_FORMAT_TAGS = {
    "dim": {"2d": ("2D", "2D Platformer", "Side Scroller", "Pixel Graphics", "2.5D", "Hand-drawn"),
            "3d": ("3D", "First-Person", "Third Person", "3D Platformer", "FPS", "Third-Person Shooter")},
    "combat": {"turn": ("Turn-Based Combat", "Turn-Based", "Turn-Based Tactics", "Turn-Based Strategy",
                        "Turn-Based RPG", "Card Battler", "Deckbuilding", "JRPG"),
               "real": ("Action", "Real-Time", "Hack and Slash", "Souls-like", "FPS", "Shooter",
                        "Third-Person Shooter", "Fast-Paced", "Character Action Game", "Action RPG",
                        "Bullet Hell", "Real Time Tactics", "Beat 'em up", "Fighting")},
    "view": {"first": ("First-Person", "FPS"), "third": ("Third Person", "Third-Person Shooter"),
             "top": ("Isometric", "Top-Down", "Top-Down Shooter"), "side": ("Side Scroller", "2D Platformer")},
}
FORMAT_RU = {"2d": "2D", "3d": "3D", "turn": "пошаговые бои", "real": "бои в реальном времени",
             "first": "от первого лица", "third": "от третьего лица", "top": "вид сверху", "side": "вид сбоку"}
FORMAT_EN = {"2d": "2D", "3d": "3D", "turn": "turn-based combat", "real": "real-time combat",
             "first": "first-person", "third": "third-person", "top": "top-down", "side": "side view"}


def format_of(tags: dict | None) -> dict[str, str]:
    """{"dim": "2d"|"3d", "combat": "turn"|"real", "view": ...}: only what the player tags say clearly
    (a tag with at least 30% of the top tag's votes, ahead of the other side)."""
    tags = tags or {}
    top = max(tags.values(), default=0) or 1
    out = {}
    for aspect, sides in _FORMAT_TAGS.items():
        share = {side: max((tags.get(t, 0) / top for t in names), default=0) for side, names in sides.items()}
        best = max(share, key=share.get)
        rest = max((v for k, v in share.items() if k != best), default=0)
        need = 0.35 if aspect == "combat" else 0.3
        if share[best] >= need and share[best] >= rest + 0.15:
            out[aspect] = best
    return out


def format_clash(ref: dict[str, str], cand: dict[str, str]) -> tuple[bool, bool]:
    """(hard, soft): a different dimension or combat timing rules a game out; another camera only
    costs it some score."""
    hard = any(ref.get(k) and cand.get(k) and ref[k] != cand[k] for k in ("dim", "combat"))
    soft = bool(ref.get("view") and cand.get("view") and ref["view"] != cand["view"])
    return hard, soft


def format_ru(fmt: dict[str, str]) -> str:
    """The format in the player's language (the name stays from when it was Russian only)."""
    from .i18n import is_ru
    names = FORMAT_RU if is_ru() else FORMAT_EN
    return ", ".join(names[fmt[k]] for k in ("dim", "view", "combat") if fmt.get(k))
