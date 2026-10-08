"""The taste of one request: tag and feel targets built from what the player asked for right now.

Nothing is stored between requests. The games the request names as references give the tag
profile (player-voted tags) and the feel (the analyst's axes: pace, difficulty, story...);
what the player said outright, and what they loved in the reference («Чем зацепила?»), overrides it.
"""

import math
from dataclasses import dataclass, field

from .analyst import AXES
from .genres import defining as defining_genres, format_of
from .titles import series_key

# Tags that say almost nothing about taste.
GENERIC_TAGS = {"Singleplayer", "Indie", "Great Soundtrack", "3D", "2D", "Colorful", "Atmospheric",
                "Free to Play", "Early Access", "Multiplayer", "Classic", "Cult Classic", "Masterpiece",
                "Addictive", "Beautiful", "Good Soundtrack", "Funny", "Family Friendly", "Controller",
                "Full controller support", "Steam Achievements", "Steam Trading Cards", "Moddable",
                "Remake", "Remaster", "Sequel", "Female Protagonist", "Male Protagonist"}


@dataclass
class Taste:
    like_vec: dict[str, float] = field(default_factory=dict)
    dislike_vec: dict[str, float] = field(default_factory=dict)
    feel: dict[str, float] = field(default_factory=dict)        # axis -> 0..10
    confidence: dict[str, float] = field(default_factory=dict)  # axis -> 0..1
    explicit: dict[str, int] = field(default_factory=dict)
    dealbreakers: list[str] = field(default_factory=list)
    liked: list[int] = field(default_factory=list)              # appids, best first
    disliked: list[int] = field(default_factory=list)
    played: set[int] = field(default_factory=set)
    n_signals: int = 0
    weights: dict[str, float] = field(default_factory=dict)    # learned multipliers per score part
    vec: list[float] | None = None      # meaning of what they love (semantic.py): their words + liked games
    core: set[str] = field(default_factory=set)   # the references' defining tags: a candidate shares one
    genres: set[str] = field(default_factory=set)  # the references' defining genre families (genres.py)
    anchors: list[list[str]] = field(default_factory=list)  # per reference: its defining genres, main first
    seed_names: list[str] = field(default_factory=list)    # the references' titles
    formats: list[dict] = field(default_factory=list)       # per reference: genres.format_of(its tags)
    seed_series: set[str] = field(default_factory=set)      # their series (titles.series_key)
    hooks: list[str] = field(default_factory=list)          # what the reference is loved for (aspects groups)

    @property
    def ready(self) -> bool:
        return bool(self.like_vec) or bool(self.explicit)


def tag_vector(tags: dict[str, int], idf: dict[str, float]) -> dict[str, float]:
    """Unit vector over a game's tags: share of the top tag's votes, square-rooted, times IDF."""
    if not tags:
        return {}
    top = max(tags.values()) or 1
    vec = {t: math.sqrt(v / top) * idf.get(t, 1.0) for t, v in tags.items()
           if t not in GENERIC_TAGS and v > 0}
    norm = math.sqrt(sum(x * x for x in vec.values())) or 1
    return {t: x / norm for t, x in vec.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    dot = sum(x * b.get(t, 0.0) for t, x in a.items())
    na = math.sqrt(sum(x * x for x in a.values())) or 1
    nb = math.sqrt(sum(x * x for x in b.values())) or 1
    return dot / (na * nb)


def for_request(req, seeds: list[dict], avoid: list[dict], passports: dict[int, dict],
                idf: dict[str, float], played: set[int]) -> Taste:
    """The taste of one request: games it names as references, tags and feel it asks for.

    Nothing here is stored: the next request starts from scratch. `played` keeps the reference games
    themselves (and anything the caller wants out) from being recommended back."""
    t = Taste(dealbreakers=list(req.dealbreakers), played=set(played))
    diversify = getattr(req, "diversify", False)     # "something else entirely": keep only asked-for tags
    muted = set(getattr(req, "mute_tags", None) or [])  # parts of the seed they did not care about
    for g in seeds:
        if not diversify:
            top = [x for x, _ in sorted((g.get("tags") or {}).items(), key=lambda kv: -kv[1])
                   if x not in GENERIC_TAGS and x not in muted][:5]
            t.core.update(top)
            fmt = format_of(g.get("tags"))
            if fmt:
                t.formats.append(fmt)
            anchor = defining_genres(g.get("tags"))
            if anchor:
                t.anchors.append(anchor)
                t.genres.update(anchor)
            for tag, x in tag_vector(g.get("tags") or {}, idf).items():
                t.like_vec[tag] = t.like_vec.get(tag, 0.0) + x * (0.25 if tag in muted else 1.0)
        t.played.add(g["appid"])
        if g.get("name"):
            t.seed_names.append(g["name"])
            if series_key(g["name"]):
                t.seed_series.add(series_key(g["name"]))
    for g in avoid:
        for tag, x in tag_vector(g.get("tags") or {}, idf).items():
            t.dislike_vec[tag] = t.dislike_vec.get(tag, 0.0) + x
        t.played.add(g["appid"])
    # Asked-for tags: on their own they pull like one reference game; next to a reference they only
    # lean inside its genre ("building" in Subnautica is not a pull towards pottery sims).
    pull = 0.35 if seeds and not diversify else 1.0
    for tag in req.tags_want:
        t.like_vec[tag] = t.like_vec.get(tag, 0.0) + pull * idf.get(tag, 1.0) / max(1, len(req.tags_want)) ** 0.5
    for tag in req.tags_avoid:
        t.dislike_vec[tag] = t.dislike_vec.get(tag, 0.0) + idf.get(tag, 1.0)
    feels = [passports[g["appid"]]["feel"] for g in seeds if g["appid"] in passports]
    focus = getattr(req, "focus_axes", None)
    for k in AXES:
        if k in req.axes:
            t.feel[k], t.confidence[k] = float(req.axes[k]), 1.0
        elif feels:
            t.feel[k] = sum(f[k] for f in feels) / len(feels)
            # When they said what they loved in the seed, its other traits barely count.
            t.confidence[k] = 0.6 if focus is None or k in focus else 0.15
    t.liked = [g["appid"] for g in seeds]
    t.disliked = [g["appid"] for g in avoid]
    t.n_signals = len(seeds) + len(req.tags_want)
    return t


def describe_axis(axis: str, value: float) -> str:
    from .analyst import axis_ends
    from .i18n import tr
    low, high = axis_ends(axis)
    if value <= 3.5:
        return low
    if value >= 6.5:
        return high
    return tr(f"in between ({low} ↔ {high})", f"в меру ({low} ↔ {high})")
