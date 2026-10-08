"""SQLite storage. One file, WAL mode, used from the event loop's thread only.

games      - the catalog: store facts plus SteamSpy's player-voted tags (the "real genre").
reviews    - review statistics per game (recent vs all time, by playtime).
passports  - what players actually say a game feels like, written by the analyst from reviews.
users      - taste answers and dealbreakers; user_games - what each user played and how it went.
"""

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS games (
    appid INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_lc TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '{}',        -- {"Exploration": 1255, ...} player votes
    genres TEXT NOT NULL DEFAULT '[]',      -- store genres (marketing)
    categories TEXT NOT NULL DEFAULT '[]',
    short_desc TEXT NOT NULL DEFAULT '',
    release_date TEXT NOT NULL DEFAULT '',
    release_year INTEGER,
    price_cents INTEGER,                    -- in the store region's currency; 0 = free
    currency TEXT NOT NULL DEFAULT '',
    discount INTEGER NOT NULL DEFAULT 0,
    ru_text INTEGER NOT NULL DEFAULT 0,
    ru_audio INTEGER NOT NULL DEFAULT 0,
    early_access INTEGER NOT NULL DEFAULT 0,
    mtx INTEGER NOT NULL DEFAULT 0,         -- "In-App Purchases"
    online_only INTEGER NOT NULL DEFAULT 0, -- multiplayer with no single-player
    single INTEGER NOT NULL DEFAULT 0,
    coop INTEGER NOT NULL DEFAULT 0,
    pvp INTEGER NOT NULL DEFAULT 0,
    drm_notice TEXT NOT NULL DEFAULT '',
    owners INTEGER NOT NULL DEFAULT 0,      -- SteamSpy lower bound
    positive INTEGER NOT NULL DEFAULT 0,
    negative INTEGER NOT NULL DEFAULT 0,
    is_dlc INTEGER NOT NULL DEFAULT 0,
    adult INTEGER NOT NULL DEFAULT 0,
    store_ok INTEGER NOT NULL DEFAULT 0,    -- store details: 1 fetched, -1 not in the store
    spy_ok INTEGER NOT NULL DEFAULT 0,      -- SteamSpy asked (tags may still be empty)
    deck INTEGER NOT NULL DEFAULT 0,        -- Steam Deck: 0 unknown, 1 unsupported, 2 playable, 3 verified
    deck_notes TEXT NOT NULL DEFAULT '',
    proton TEXT NOT NULL DEFAULT '',        -- ProtonDB tier
    deck_ok INTEGER NOT NULL DEFAULT 0,     -- Deck report fetched
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS games_name ON games(name_lc);
CREATE TABLE IF NOT EXISTS reviews (
    appid INTEGER PRIMARY KEY,
    stats TEXT NOT NULL,                    -- reviews.ReviewStats as JSON
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS passports (
    appid INTEGER PRIMARY KEY,
    data TEXT NOT NULL,                     -- analyst.Passport as JSON
    source TEXT NOT NULL,                   -- 'llm' | 'heuristic'
    model TEXT NOT NULL DEFAULT '',
    reviews_used INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_queue (
    appid INTEGER PRIMARY KEY,
    priority INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_usage (
    day TEXT PRIMARY KEY,
    games INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    prefs TEXT NOT NULL DEFAULT '{}',       -- explicit axis answers {"pace": 3, ...}
    dealbreakers TEXT NOT NULL DEFAULT '[]',
    steam_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',         -- onboarding step
    own_words TEXT NOT NULL DEFAULT '',     -- what hooks them in games, in their words
    session TEXT NOT NULL DEFAULT '{}',     -- the current request and the games shown for it
    region TEXT NOT NULL DEFAULT '',        -- their Steam account's store region, for prices
    allowed INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS user_games (
    user_id INTEGER NOT NULL,
    appid INTEGER NOT NULL,
    verdict TEXT NOT NULL,       -- love | like | meh | dislike | dropped | skip (not interested) | want
    playtime_min INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'chat',    -- chat | steam | feedback
    note TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, appid)
);
CREATE TABLE IF NOT EXISTS shown (
    user_id INTEGER NOT NULL,
    appid INTEGER NOT NULL,
    shown_at INTEGER NOT NULL,
    parts TEXT NOT NULL DEFAULT '{}',       -- the score's parts when shown, to learn from feedback
    PRIMARY KEY (user_id, appid)
);
"""

GAME_FIELDS = (
    "name", "tags", "genres", "categories", "short_desc", "release_date", "release_year",
    "price_cents", "currency", "discount", "ru_text", "ru_audio", "early_access", "mtx",
    "online_only", "single", "coop", "pvp", "drm_notice", "owners", "positive", "negative",
    "is_dlc", "adult", "store_ok", "spy_ok", "deck", "deck_notes", "proton", "deck_ok",
)
# Columns added after the first release: (name, definition) for ALTER TABLE on older files.
ADDED_COLUMNS = [
    ("spy_ok", "INTEGER NOT NULL DEFAULT 0"),
    ("deck", "INTEGER NOT NULL DEFAULT 0"),
    ("deck_notes", "TEXT NOT NULL DEFAULT ''"),
    ("proton", "TEXT NOT NULL DEFAULT ''"),
    ("deck_ok", "INTEGER NOT NULL DEFAULT 0"),
]
JSON_FIELDS = ("tags", "genres", "categories")


class Db:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA journal_size_limit=67108864")   # the WAL shrinks back after a big write
        self.conn.executescript(SCHEMA)
        for table, columns in (("games", ADDED_COLUMNS), ("shown", [("parts", "TEXT NOT NULL DEFAULT '{}'")]),
                               ("users", [("own_words", "TEXT NOT NULL DEFAULT ''"),
                                         ("session", "TEXT NOT NULL DEFAULT '{}'"),
                                         ("region", "TEXT NOT NULL DEFAULT ''")])):
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- meta
    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
        self.conn.commit()

    # --- games
    def upsert_game(self, appid: int, **fields) -> None:
        """Insert or update only the given fields; JSON fields may be passed as Python objects."""
        fields = {k: v for k, v in fields.items() if k in GAME_FIELDS}
        for k in JSON_FIELDS:
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        if "name" in fields:
            fields["name_lc"] = fields["name"].lower()
        fields["updated_at"] = int(time.time())
        exists = self.conn.execute("SELECT 1 FROM games WHERE appid=?", (appid,)).fetchone()
        if exists:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(f"UPDATE games SET {sets} WHERE appid=?", (*fields.values(), appid))
        else:
            fields.setdefault("name", f"App {appid}")
            fields.setdefault("name_lc", fields["name"].lower())
            cols = ", ".join(["appid", *fields])
            marks = ", ".join("?" * (len(fields) + 1))
            self.conn.execute(f"INSERT INTO games({cols}) VALUES({marks})", (appid, *fields.values()))
        self.conn.commit()

    def game(self, appid: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM games WHERE appid=?", (appid,)).fetchone()
        return _game(row) if row else None

    def games(self, appids) -> dict[int, dict]:
        ids = list(appids)
        out = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.conn.execute(f"SELECT * FROM games WHERE appid IN ({marks})", chunk):
                out[row["appid"]] = _game(row)
        return out

    def catalog(self, min_reviews: int = 200) -> list[dict]:
        """Games worth recommending: real games with tags and enough reviews to judge."""
        rows = self.conn.execute(
            "SELECT * FROM games WHERE is_dlc=0 AND tags != '{}' AND positive + negative >= ?",
            (min_reviews,))
        return [_game(r) for r in rows]

    def find_by_name(self, name: str, limit: int = 5) -> list[dict]:
        q = name.lower().strip()
        rows = self.conn.execute(
            "SELECT * FROM games WHERE name_lc = ? OR name_lc LIKE ? "
            "ORDER BY (name_lc = ?) DESC, positive + negative DESC LIMIT ?",
            (q, f"%{q}%", q, limit)).fetchall()
        return [_game(r) for r in rows]

    def games_missing_store(self, limit: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT appid FROM games WHERE store_ok=0 ORDER BY positive + negative DESC LIMIT ?", (limit,))
        return [r[0] for r in rows]

    def games_missing_tags(self, limit: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT appid FROM games WHERE spy_ok=0 ORDER BY positive + negative DESC LIMIT ?", (limit,))
        return [r[0] for r in rows]

    def games_without_llm_passport(self, limit: int, skip=()) -> list[int]:
        skip = list(skip)[:500]
        extra = f" AND g.appid NOT IN ({','.join('?' * len(skip))})" if skip else ""
        rows = self.conn.execute(
            "SELECT g.appid FROM games g LEFT JOIN passports p ON p.appid = g.appid "
            "WHERE g.is_dlc = 0 AND g.tags != '{}' AND g.positive + g.negative >= 200 "
            f"AND (p.appid IS NULL OR p.source != 'llm'){extra} "
            "ORDER BY g.positive + g.negative DESC LIMIT ?", (*skip, limit))
        return [r[0] for r in rows]

    def games_missing_deck(self, limit: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT appid FROM games WHERE deck_ok=0 AND store_ok=1 AND is_dlc=0 "
            "ORDER BY positive + negative DESC LIMIT ?", (limit,))
        return [r[0] for r in rows]

    def game_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]

    # --- reviews and passports
    def set_review_stats(self, appid: int, stats: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO reviews(appid, stats, updated_at) VALUES(?, ?, ?)",
                          (appid, json.dumps(stats, ensure_ascii=False), int(time.time())))
        self.conn.commit()

    def review_stats(self, appid: int) -> tuple[dict, int] | None:
        row = self.conn.execute("SELECT stats, updated_at FROM reviews WHERE appid=?", (appid,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def set_passport(self, appid: int, data: dict, source: str, model: str, reviews_used: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO passports(appid, data, source, model, reviews_used, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (appid, json.dumps(data, ensure_ascii=False), source, model, reviews_used, int(time.time())))
        self.conn.execute("DELETE FROM analysis_queue WHERE appid=?", (appid,))
        self.conn.commit()

    def passport(self, appid: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM passports WHERE appid=?", (appid,)).fetchone()
        return _passport(row) if row else None

    def passports(self, appids) -> dict[int, dict]:
        ids = list(appids)
        out = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.conn.execute(f"SELECT * FROM passports WHERE appid IN ({marks})", chunk):
                out[row["appid"]] = _passport(row)
        return out

    def enqueue_analysis(self, appids, priority: int = 0) -> None:
        now = int(time.time())
        for a in appids:
            self.conn.execute(
                "INSERT INTO analysis_queue(appid, priority, added_at) VALUES(?, ?, ?) "
                "ON CONFLICT(appid) DO UPDATE SET priority=MAX(priority, excluded.priority)",
                (a, priority, now))
        self.conn.commit()

    def next_in_queue(self, limit: int = 1) -> list[int]:
        rows = self.conn.execute(
            "SELECT appid FROM analysis_queue ORDER BY priority DESC, added_at LIMIT ?", (limit,))
        return [r[0] for r in rows]

    def dequeue(self, appid: int) -> None:
        self.conn.execute("DELETE FROM analysis_queue WHERE appid=?", (appid,))
        self.conn.commit()

    def queue_size(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM analysis_queue").fetchone()[0]

    def llm_games_today(self) -> int:
        row = self.conn.execute("SELECT games FROM llm_usage WHERE day=?", (_today(),)).fetchone()
        return row[0] if row else 0

    def add_llm_usage(self, input_tokens: int, output_tokens: int, games: int = 1) -> None:
        """games=1 for a review analysis (LLM_DAILY_GAMES counts those); 0 for other calls, which
        only add their tokens."""
        self.conn.execute(
            "INSERT INTO llm_usage(day, games, input_tokens, output_tokens) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET games=games+excluded.games, "
            "input_tokens=input_tokens+excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens",
            (_today(), games, input_tokens, output_tokens))
        self.conn.commit()

    def llm_usage(self, days: int = 30) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM llm_usage ORDER BY day DESC LIMIT ?", (days,))
        return [dict(r) for r in rows]

    # --- users
    def user(self, user_id: int, name: str = "") -> dict:
        row = self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            self.conn.execute("INSERT INTO users(user_id, name, created_at) VALUES(?, ?, ?)",
                              (user_id, name, int(time.time())))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        u = dict(row)
        u["prefs"] = json.loads(u["prefs"])
        u["dealbreakers"] = json.loads(u["dealbreakers"])
        return u

    def update_user(self, user_id: int, **fields) -> None:
        for k in ("prefs", "dealbreakers"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE users SET {sets} WHERE user_id=?", (*fields.values(), user_id))
        self.conn.commit()

    def allowed_users(self) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT user_id FROM users WHERE allowed=1")}

    def set_user_game(self, user_id: int, appid: int, verdict: str, source: str = "chat",
                      playtime_min: int | None = None, note: str = "") -> None:
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO user_games(user_id, appid, verdict, playtime_min, source, note, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, appid) DO UPDATE SET "
            "verdict=excluded.verdict, source=excluded.source, note=excluded.note, "
            "playtime_min=MAX(user_games.playtime_min, excluded.playtime_min), updated_at=excluded.updated_at",
            (user_id, appid, verdict, playtime_min or 0, source, note, now))
        self.conn.commit()

    def remove_user_game(self, user_id: int, appid: int) -> None:
        self.conn.execute("DELETE FROM user_games WHERE user_id=? AND appid=?", (user_id, appid))
        self.conn.commit()

    def user_games(self, user_id: int) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM user_games WHERE user_id=? ORDER BY updated_at DESC", (user_id,))
        return [dict(r) for r in rows]

    def mark_shown(self, user_id: int, appids, parts: dict[int, dict] | None = None) -> None:
        now = int(time.time())
        parts = parts or {}
        self.conn.executemany(
            "INSERT OR REPLACE INTO shown(user_id, appid, shown_at, parts) VALUES(?, ?, ?, ?)",
            [(user_id, a, now, json.dumps(parts.get(a, {}))) for a in appids])
        self.conn.commit()

    def feedback(self, user_id: int) -> list[tuple[str, dict]]:
        """(verdict, score parts when shown) for every recommendation the player rated."""
        rows = self.conn.execute(
            "SELECT g.verdict, s.parts FROM shown s JOIN user_games g "
            "ON g.user_id = s.user_id AND g.appid = s.appid "
            "WHERE s.user_id = ? AND g.source = 'feedback' AND s.parts != '{}'", (user_id,))
        return [(r[0], json.loads(r[1])) for r in rows]

    def shown(self, user_id: int, since_days: int = 14) -> set[int]:
        since = int(time.time()) - since_days * 86400
        rows = self.conn.execute("SELECT appid FROM shown WHERE user_id=? AND shown_at>=?", (user_id, since))
        return {r[0] for r in rows}

    def reset_user(self, user_id: int) -> None:
        for table in ("user_games", "shown", "users"):
            self.conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        self.conn.commit()


def _game(row: sqlite3.Row) -> dict:
    g = dict(row)
    for k in JSON_FIELDS:
        g[k] = json.loads(g[k])
    return g


def _passport(row: sqlite3.Row) -> dict:
    from .analyst import normalize       # rows written by older versions get the current shape too
    p = normalize(json.loads(row["data"]))
    p["_source"] = row["source"]
    p["_model"] = row["model"]
    p["_reviews_used"] = row["reviews_used"]
    p["_updated_at"] = row["updated_at"]
    return p


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())
