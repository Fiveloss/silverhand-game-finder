"""Does a store's search result really name the game the player typed?

Store search is fuzzy: "Alan Wake 2" returns "Alan Wake" (the sequel is not on Steam), "Hades 2"
may return "Hades". Using the wrong game as a reference quietly ruins a request, so a result
must carry the same sequel number and, for Latin-script queries, read close to the query.
Cyrillic queries are trusted to the store's localized search (it knows «Ведьмак» is The Witcher).
"""

import re
from difflib import SequenceMatcher

ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}
EDITION_WORDS = {
    "the", "edition", "remastered", "remaster", "definitive", "complete", "goty", "game", "of", "year",
    "enhanced", "directors", "director's", "cut", "final", "deluxe", "ultimate", "anniversary", "hd",
    "collection", "редакция", "издание", "полное",
}


def normalize(title: str) -> str:
    t = title.lower().replace("&", " and ")
    t = re.sub(r"[™®©]", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def numbers(title: str) -> set[int]:
    """Sequel numbers: arabic (except years) and roman numerals as separate words."""
    out = set()
    for w in normalize(title).split():
        if w.isdigit() and len(w) <= 2:
            out.add(int(w))
        elif w in ROMAN and w != "i":       # a lone "i" is usually the pronoun
            out.add(ROMAN[w])
    return out


def core(title: str) -> str:
    """Normalised title without edition words, roman sequel numbers as digits."""
    out = []
    for w in normalize(title).split():
        if w in EDITION_WORDS:
            continue
        out.append(str(ROMAN[w]) if w in ROMAN and w != "i" else w)
    return " ".join(out)


# Store search mixes these in with the game itself.
NOT_A_GAME = re.compile(r"soundtrack|\bost\b|artbook|art book|season pass|redkit|sdk|dedicated server|"
                        r"саундтрек|артбук|сезонный абонемент|\bdlc\b|bonus content|wallpaper", re.I)


# Shorthands players type, expanded before matching (and before searching the store).
ALIASES = {
    "gta": "grand theft auto", "rdr": "red dead redemption", "bg": "baldur's gate", "bg3": "baldur's gate 3",
    "kcd": "kingdom come deliverance", "kcd2": "kingdom come deliverance ii", "tes": "the elder scrolls",
    "cs": "counter-strike", "cs2": "counter-strike 2", "re": "resident evil", "mgs": "metal gear solid",
    "ds": "dark souls", "ds3": "dark souls iii", "er": "elden ring", "hk": "hollow knight", "poe": "path of exile",
    "rdr2": "red dead redemption 2", "ff": "final fantasy", "dbd": "dead by daylight", "ac": "assassin's creed",
    "skyrim": "the elder scrolls v skyrim", "ведьмак": "the witcher", "киберпанк": "cyberpunk 2077",
    "гта": "grand theft auto", "рдр": "red dead redemption", "скайрим": "the elder scrolls v skyrim",
}


def expand(query: str) -> str:
    words = normalize(query).split()
    out = []
    for w in words:
        m = re.fullmatch(r"([a-zа-яё]+)(\d+)", w)
        if w in ALIASES:
            out.append(ALIASES[w])
        elif m and m.group(1) in ALIASES and w not in ALIASES:
            out.append(f"{ALIASES[m.group(1)]} {m.group(2)}")
        else:
            out.append(w)
    return " ".join(out)


def score(query: str, name: str) -> float:
    """0..1 how surely `name` is the game `query` means; 0 when the sequel numbers differ
    or the result is a soundtrack, an artbook or the like."""
    if NOT_A_GAME.search(name):
        return 0.0
    expanded = expand(query)
    return max(_score(query, name), _score(expanded, name) if expanded != normalize(query) else 0.0)


def _score(query: str, name: str) -> float:
    q, n = numbers(query), numbers(name)
    if q != n and (q or len(n - {1}) > 0):
        return 0.0
    cq, cn = core(query), core(name)
    if not cq or not cn:
        return 0.0
    if cq == cn:
        return 1.0
    main = core(re.split(r":| - | — ", name)[0])
    if main == cq:
        return 0.9          # "The Witcher 3" for "The Witcher 3: Wild Hunt" (an exact title still wins)
    extra = cn[len(cq):].split() if cn.startswith(cq + " ") else None
    if extra and all(w.isdigit() and len(w) >= 3 for w in extra):
        return 0.85         # "Cyberpunk" for "Cyberpunk 2077"
    if re.search(r"[а-яё]", cq) and set(cq.split()) <= set(cn.split()):
        return 0.86         # a translated title with every word the player typed
    # Squared, so near-misses with a different word ("Outer Wilds" / "Outer Worlds") fall short.
    return SequenceMatcher(None, cq, cn).ratio() ** 2


def confident(query: str, name: str, threshold: float = 0.85) -> bool:
    return score(query, name) >= threshold
