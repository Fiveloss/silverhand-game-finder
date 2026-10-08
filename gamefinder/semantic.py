"""Matching on the experience players describe, with their own words as proof.

Each game gets two kinds of vectors from Google's free embedding API (OpenAI-compatible
endpoint, same key as the analyst):

- one "experience" vector: the passport's summary, core loop, praise and best_for in one text;
- up to ~20 review snippets (60-300 characters, about how it feels to play) cut from the review
  sample the analyst read, each with its own vector.

A player's taste vector comes from the words they write ("what hooks you in games?") or from the
experience vectors of games they liked. experience_score() compares it with a game; evidence()
returns the snippets closest to the player's words, so a card can quote a real player.

Storage is pure Python: every vector is unit length, quantised to int8 with one float32 scale,
4 + DIM bytes (772 for 768 dims). No numpy; a 768-dim dot product is a few tens of microseconds.

Nothing here raises to the caller on API trouble: a 429, an error body or a network failure
returns None / False and the caller tries again later.
"""

import asyncio
import logging
import math
import re
import struct
import time
from array import array

log = logging.getLogger(__name__)

URL = "https://generativelanguage.googleapis.com/v1beta/openai/embeddings"
# gemini-embedding-001: on a free key gemini-embedding-2 runs out after a handful of calls (Oct 2026).
DEFAULT_MODEL = "gemini-embedding-001"
DIM = 768                                # Google's recommended reduced size (128-3072 allowed)
MAX_BATCH = 100                          # inputs per request (the batch limit of the native API)

SNIPPET_MIN, SNIPPET_MAX = 60, 300
MAX_SNIPPETS = 20
POSITIVE_SHARE = 0.7                     # of the snippets kept, how many come from positive reviews
POSITIVE_BONUS = 0.08                    # evidence(): a positive snippet wins a near tie
# Cosine between two texts about games rarely leaves 0.3..0.9 with these models: stretch that band
# to 0..1 so the score moves like the recommender's other parts. Tune on real data.
COS_FLOOR, COS_CEIL = 0.35, 0.85

# Models that rejected the `dimensions` field: we truncate and renormalise on our side instead
# (Gemini embeddings are Matryoshka-trained, so the first DIM components are a valid embedding).
_NO_DIMENSIONS: set[str] = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS game_vectors (
    appid INTEGER NOT NULL,
    kind TEXT NOT NULL,               -- 'experience'
    vec BLOB NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (appid, kind)
);
CREATE TABLE IF NOT EXISTS review_snippets (
    appid INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    up INTEGER NOT NULL,
    hours REAL NOT NULL DEFAULT 0,
    lang TEXT NOT NULL DEFAULT '',
    vec BLOB NOT NULL,
    PRIMARY KEY (appid, idx)
);
CREATE TABLE IF NOT EXISTS user_vectors (
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,               -- 'words': the player's own description of what hooks them
    text TEXT NOT NULL DEFAULT '',
    vec BLOB NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, kind)
);
"""


def drop_other_models(conn, model: str) -> int:
    """Vectors of different models cannot be compared: forget games indexed with another model
    (they are indexed again when their reviews are next read). Returns how many games were dropped."""
    ids = [r[0] for r in conn.execute("SELECT appid FROM game_vectors WHERE model != ?", (model,))]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM game_vectors WHERE appid IN ({marks})", chunk)
        conn.execute(f"DELETE FROM review_snippets WHERE appid IN ({marks})", chunk)
    conn.execute("DELETE FROM user_vectors")
    conn.commit()
    return len(ids)


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# --- vectors

def normalize(vec) -> list[float] | None:
    n = math.sqrt(sum(x * x for x in vec))
    if not n or math.isnan(n):
        return None
    return [x / n for x in vec]


def pack(vec) -> bytes:
    """int8 with one float32 scale: 4 + len(vec) bytes."""
    peak = max((abs(x) for x in vec), default=0.0)
    scale = peak / 127 if peak else 1.0
    q = array("b", (max(-127, min(127, round(x / scale))) for x in vec))
    return struct.pack("<f", scale) + q.tobytes()


def unpack(blob: bytes | None) -> list[float] | None:
    q = _ints(blob)
    if q is None:
        return None
    scale = struct.unpack_from("<f", blob)[0]
    return [x * scale for x in q]


def _ints(blob: bytes | None) -> array | None:
    if not blob or len(blob) < 5:
        return None
    q = array("b")
    q.frombytes(blob[4:])
    return q


def cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _blob_cosine(blob: bytes, unit: list[float]) -> float | None:
    """Cosine between a stored vector and a unit-length one, without dequantising (the scale cancels)."""
    q = _ints(blob)
    if q is None or len(q) != len(unit):
        return None
    nq = math.sqrt(sum(x * x for x in q))
    if not nq:
        return None
    return sum(map(float.__mul__, unit, map(float, q))) / nq


def score_from_cosine(c: float) -> float:
    return max(0.0, min(1.0, (c - COS_FLOOR) / (COS_CEIL - COS_FLOOR)))


def combine(weighted: list[tuple[list[float] | None, float]]) -> list[float] | None:
    """Weighted mean of unit vectors, renormalised: e.g. the player's words 0.6 + liked games 0.4."""
    acc = None
    for vec, w in weighted:
        if not vec or not w:
            continue
        if acc is None:
            acc = [0.0] * len(vec)
        if len(vec) != len(acc):
            continue
        for i, x in enumerate(vec):
            acc[i] += w * x
    return normalize(acc) if acc else None


# --- texts to embed

def _is_v2(model: str) -> bool:
    return "embedding-2" in model


def as_document(text: str, model: str = DEFAULT_MODEL) -> str:
    """gemini-embedding-2 takes the task in the text (no task_type field): documents this way."""
    return f"title: none | text: {text}" if _is_v2(model) else text


def as_query(text: str, model: str = DEFAULT_MODEL) -> str:
    return f"task: search result | query: {text}" if _is_v2(model) else text


def experience_text(passport: dict) -> str:
    """What playing it is like, in the passport's words: summary, core loop, praise, best_for."""
    parts = [str(passport.get("summary") or "").strip(), str(passport.get("core_loop") or "").strip()]
    praise = []
    for x in passport.get("praise") or []:
        point = x.get("point") if isinstance(x, dict) else x
        if point:
            praise.append(str(point).strip())
    if praise:
        parts.append("Хвалят: " + "; ".join(praise) + ".")
    best = str(passport.get("best_for") or "").strip()
    if best:
        parts.append("Для кого: " + best)
    return "\n".join(p for p in parts if p)


# Words that describe playing the game, not buying it.
_EXPERIENCE = re.compile(
    r"\bfeel|\bfelt\b|feeling|atmospher|immers|tension|tense\b|satisf|addict|hooked|relax|chill|cozy|"
    r"\bexplor|discover|every run|each run|each time|every time|moment|\bloop\b|\bflow\b|makes you|"
    r"you (?:can|will|get|feel|have|need|spend)|hours? fl[eyi]|lost track|immersive|rewarding|"
    r"punishing|frustrat|tedious|grind|boring|exciting|thrill|scary|creepy|calm|meditat|"
    r"ощущ|чувств|атмосфер|затягива|залип|погруж|напряж|расслаб|исследова|каждый раз|каждом забеге|"
    r"момент|хочется|ты (?:можешь|будешь|чувствуешь)|тебе|процесс|медитатив|уютн|азарт|страшн|"
    r"нудн|скучн|гринд|уныл|бесит|вознагра|кайф|драйв|интересно|не оторваться|оторваться",
    re.I)
_OFF_TOPIC = re.compile(
    r"\bprice|\bsale\b|discount|refund|\$\d|\d+\s?(?:usd|rub|₽|руб)|worth (?:the|every|it)|\bbuy\b|"
    r"\d+\s?/\s?10\b|this review|developer|\bdevs?\b|patch|update|dlc|cheat|"
    r"цен[аеуы]|скидк|рефанд|возврат|купи|покупа|разраб|патч|обновлен|обзор|длс|оценк",
    re.I)
_TEMPLATE = re.compile(r"[☐☑☒✓✔✅❌□■▪►●★☆]|-{3,}|={3,}|_{3,}|\|\s*\|")
_SENTENCE = re.compile(r"(?<=[.!?…])\s+|\n+")


def _letters_share(text: str) -> float:
    return sum(ch.isalpha() or ch.isspace() for ch in text) / max(1, len(text))


def _snippet_score(text: str, review: dict) -> float:
    hits = len(_EXPERIENCE.findall(text))
    s = min(hits, 3) * 1.0
    s -= 1.5 * len(_OFF_TOPIC.findall(text))
    if _TEMPLATE.search(text) or _letters_share(text) < 0.85:
        s -= 3
    caps = sum(ch.isupper() for ch in text) / max(1, sum(ch.isalpha() for ch in text))
    if caps > 0.3:
        s -= 2
    if not hits:
        s -= 1
    hours = float(review.get("hours") or 0)
    s += min(1.0, math.log1p(hours) / math.log1p(50))       # someone who played it
    s += min(0.5, math.log1p(int(review.get("helpful") or 0)) / 8)
    s += 0.3 if 100 <= len(text) <= 220 else 0.0             # reads well on a card
    return s


def _cut(text: str) -> list[str]:
    """Consecutive sentences glued into 60-300 character pieces; an overlong sentence is trimmed."""
    out, buf = [], ""
    for sent in (x.strip() for x in _SENTENCE.split(text)):
        if not sent:
            continue
        if len(sent) > SNIPPET_MAX:
            if SNIPPET_MIN <= len(buf):
                out.append(buf)
            buf = ""
            cut = sent[:SNIPPET_MAX - 1]
            cut = cut[:cut.rfind(" ")] if " " in cut[SNIPPET_MIN:] else cut
            out.append(cut.rstrip(",;:—- ") + "…")
            continue
        joined = f"{buf} {sent}".strip()
        if len(joined) <= SNIPPET_MAX:
            buf = joined
            if len(buf) >= 140:          # long enough to stand alone: do not keep gluing
                out.append(buf)
                buf = ""
        else:
            if len(buf) >= SNIPPET_MIN:
                out.append(buf)
            buf = sent
    if len(buf) >= SNIPPET_MIN:
        out.append(buf)
    return [x for x in out if SNIPPET_MIN <= len(x) <= SNIPPET_MAX]


def pick_snippets(sample: list[dict], limit: int = MAX_SNIPPETS, per_review: int = 2) -> list[dict]:
    """The most informative pieces of the review sample: {text, up, hours, lang}, best first.
    About 70% from positive reviews; at most `per_review` pieces from one review."""
    scored = []
    seen = set()
    for n, r in enumerate(sample):
        pieces = []
        for text in _cut(str(r.get("text") or "")):
            key = re.sub(r"\W+", "", text.lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            pieces.append((_snippet_score(text, r), text))
        pieces.sort(key=lambda x: -x[0])
        for s, text in pieces[:per_review]:
            if s > 0:
                scored.append((s, n, {"text": text, "up": bool(r.get("up")),
                                      "hours": float(r.get("hours") or 0), "lang": r.get("lang", "")}))
    scored.sort(key=lambda x: (-x[0], x[1]))
    pos = [x for x in scored if x[2]["up"]]
    neg = [x for x in scored if not x[2]["up"]]
    n_pos = min(len(pos), max(round(limit * POSITIVE_SHARE), limit - len(neg)))
    chosen = pos[:n_pos] + neg[:limit - n_pos]
    chosen.sort(key=lambda x: (-x[0], x[1]))
    return [x[2] for x in chosen]


# --- the API

async def embed_texts(http, key: str, model: str, texts: list[str], *,
                      dims: int = DIM) -> list[list[float]] | None:
    """Unit vectors of `dims` components, one per text, in order. None on any failure
    (429, error body, network): the caller retries later. Never raises."""
    if not texts:
        return []
    if not key:
        return None
    out: list[list[float]] = []
    try:
        for i in range(0, len(texts), MAX_BATCH):
            chunk = [t if t.strip() else "-" for t in texts[i:i + MAX_BATCH]]
            vecs = await _embed_chunk(http, key, model, chunk, dims)
            if vecs is None:
                return None
            out += vecs
    except asyncio.CancelledError:
        raise
    except Exception as e:      # aiohttp errors, timeouts, odd bodies
        log.warning("embeddings failed: %s", e)
        return None
    return out


async def _embed_chunk(http, key, model, texts, dims):
    payload = {"model": model, "input": texts}
    if model not in _NO_DIMENSIONS:
        payload["dimensions"] = dims
    headers = {"Authorization": f"Bearer {key}"}
    status, body = await http.post_json(URL, payload, headers=headers, timeout=60)
    if status == 400 and "dimensions" in payload and "dimension" in str(body).lower():
        _NO_DIMENSIONS.add(model)
        payload = {"model": model, "input": texts}
        status, body = await http.post_json(URL, payload, headers=headers, timeout=60)
    if status == 429:
        log.info("embeddings: rate limited")
        return None
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("data"), list):
        log.warning("embeddings: HTTP %s %s", status, str(body)[:200])
        return None
    data = sorted(body["data"], key=lambda d: d.get("index", 0))
    if len(data) != len(texts):
        log.warning("embeddings: %d vectors for %d texts", len(data), len(texts))
        return None
    out = []
    for d in data:
        emb = d.get("embedding")
        if not isinstance(emb, list) or len(emb) < dims:
            return None
        v = normalize([float(x) for x in emb[:dims]])
        if v is None:
            return None
        out.append(v)
    return out


async def embed_query(http, key: str, model: str, text: str) -> list[float] | None:
    """The player's own words as a taste vector (e.g. the onboarding answer)."""
    vecs = await embed_texts(http, key, model, [as_query(text.strip()[:2000], model)])
    return vecs[0] if vecs else None


async def index_game(http, key: str, model: str, conn, appid: int, passport: dict,
                     sample: list[dict]) -> bool:
    """Embeds the passport's experience text and the best review snippets, replacing what was
    stored for the game. One request. False when there is nothing to embed or the API failed."""
    exp = experience_text(passport or {})
    snippets = pick_snippets(sample or [])
    texts = ([exp] if exp else []) + [s["text"] for s in snippets]
    if not texts:
        return False
    vecs = await embed_texts(http, key, model, [as_document(t, model) for t in texts])
    if not vecs:
        return False
    now = int(time.time())
    try:
        with conn:
            if exp:
                conn.execute("INSERT OR REPLACE INTO game_vectors(appid, kind, vec, model, updated_at) "
                             "VALUES(?, 'experience', ?, ?, ?)", (appid, pack(vecs[0]), model, now))
            conn.execute("DELETE FROM review_snippets WHERE appid=?", (appid,))
            conn.executemany(
                "INSERT INTO review_snippets(appid, idx, text, up, hours, lang, vec) VALUES(?, ?, ?, ?, ?, ?, ?)",
                [(appid, i, s["text"], int(s["up"]), s["hours"], s["lang"], pack(v))
                 for i, (s, v) in enumerate(zip(snippets, vecs[1 if exp else 0:]))])
    except Exception as e:
        log.warning("index_game %s: %s", appid, e)
        return False
    return True


# --- reading

def is_indexed(conn, appid: int) -> bool:
    return conn.execute("SELECT 1 FROM game_vectors WHERE appid=? AND kind='experience'",
                        (appid,)).fetchone() is not None


def game_vector(conn, appid: int, kind: str = "experience") -> list[float] | None:
    row = conn.execute("SELECT vec FROM game_vectors WHERE appid=? AND kind=?", (appid, kind)).fetchone()
    return unpack(row[0]) if row else None


def experience_score(conn, taste_vec: list[float] | None, appid: int) -> float | None:
    """0..1: how close the game's described experience is to the player's taste. None = not indexed."""
    unit = normalize(taste_vec) if taste_vec else None
    if not unit:
        return None
    row = conn.execute("SELECT vec FROM game_vectors WHERE appid=? AND kind='experience'", (appid,)).fetchone()
    c = _blob_cosine(row[0], unit) if row else None
    return None if c is None else score_from_cosine(c)


def experience_scores(conn, taste_vec: list[float] | None, appids) -> dict[int, float]:
    """experience_score for many games in one query; games without vectors are left out."""
    unit = normalize(taste_vec) if taste_vec else None
    ids = list(appids)
    out: dict[int, float] = {}
    if not unit:
        return out
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for appid, blob in conn.execute(
                f"SELECT appid, vec FROM game_vectors WHERE kind='experience' AND appid IN ({marks})", chunk):
            c = _blob_cosine(blob, unit)
            if c is not None:
                out[appid] = score_from_cosine(c)
    return out


def evidence(conn, taste_vec: list[float] | None, appid: int, k: int = 2) -> list[dict]:
    """The k snippets closest to the player's taste, positive reviews preferred:
    [{text, hours, up, lang, sim}], best first."""
    unit = normalize(taste_vec) if taste_vec else None
    if not unit or k <= 0:
        return []
    ranked = []
    for idx, text, up, hours, lang, blob in conn.execute(
            "SELECT idx, text, up, hours, lang, vec FROM review_snippets WHERE appid=?", (appid,)):
        c = _blob_cosine(blob, unit)
        if c is None:
            continue
        ranked.append((c + (POSITIVE_BONUS if up else 0.0), idx,
                       {"text": text, "hours": hours, "up": bool(up), "lang": lang, "sim": round(c, 4)}))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return [x[2] for x in ranked[:k]]


def taste_vector_from_games(conn, liked: list[tuple[int, float]]) -> list[float] | None:
    """Weighted mean of the liked games' experience vectors (taste.game_weight works as weights;
    negative weights push away). None when no positive-weight game is indexed."""
    acc, positive = None, 0.0
    for appid, w in liked:
        if not w:
            continue
        v = game_vector(conn, appid)
        if not v:
            continue
        if acc is None:
            acc = [0.0] * len(v)
        if len(v) != len(acc):
            continue
        for i, x in enumerate(v):
            acc[i] += w * x
        positive += max(0.0, w)
    return normalize(acc) if acc and positive > 0 else None


def set_user_vector(conn, user_id: int, vec: list[float], text: str = "", kind: str = "words") -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO user_vectors(user_id, kind, text, vec, updated_at) "
                     "VALUES(?, ?, ?, ?, ?)", (user_id, kind, text, pack(vec), int(time.time())))


def user_vector(conn, user_id: int, kind: str = "words") -> list[float] | None:
    row = conn.execute("SELECT vec FROM user_vectors WHERE user_id=? AND kind=?", (user_id, kind)).fetchone()
    return unpack(row[0]) if row else None
