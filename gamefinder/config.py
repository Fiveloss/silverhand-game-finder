"""Settings from the environment, optionally loaded from a .env file next to the code."""

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | Path) -> None:
    """KEY=VALUE lines; # comments; existing environment variables win."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _ids(value: str) -> frozenset[int]:
    return frozenset(int(x) for x in value.replace(" ", "").split(",") if x)


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_ids: frozenset[int]
    allowed_ids: frozenset[int] = frozenset()
    open_access: bool = False
    db_path: str = "data/gamefinder.db"
    # Free LLM APIs that read the reviews and write each game's passport, tried in this order.
    # No keys = heuristics only.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    gemini_lite_model: str = "gemini-flash-lite-latest"
    gemini_embed_model: str = "gemini-embedding-001"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    llm_daily_games: int = 400
    # Optional sources.
    steam_api_key: str = ""
    igdb_client_id: str = ""
    igdb_client_secret: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    # Store region for prices and availability.
    store_cc: str = "kz"
    animate_cards: bool = True      # swap each card for a 2-second loop once it is rendered
    passport_max_age_days: int = 30
    catalog_pages: int = 10
    log_level: str = "INFO"


def load_config(env_file: str | Path = ".env") -> Config:
    load_dotenv(env_file)
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        print("BOT_TOKEN is not set (see .env.example)")
        raise SystemExit(78)

    def e(key: str, default: str = "") -> str:
        # An empty KEY= in .env means "use the default", as .env.example promises.
        return (os.environ.get(key) or "").strip() or default

    try:
        return _build(token, e)
    except ValueError as err:
        print(f"Bad setting in .env: {err}")
        raise SystemExit(78)


def _build(token: str, e) -> Config:
    return Config(
        bot_token=token,
        owner_ids=_ids(e("OWNER_IDS", "")),
        allowed_ids=_ids(e("ALLOWED_IDS", "")),
        open_access=e("OPEN_ACCESS", "0").strip().lower() in ("1", "true", "yes"),
        db_path=e("DB_PATH", "data/gamefinder.db"),
        gemini_api_key=e("GEMINI_API_KEY", "").strip(),
        gemini_model=e("GEMINI_MODEL", "gemini-flash-latest").strip(),
        gemini_lite_model=e("GEMINI_LITE_MODEL", "gemini-flash-lite-latest").strip(),
        gemini_embed_model=e("GEMINI_EMBED_MODEL", "gemini-embedding-001").strip(),
        groq_api_key=e("GROQ_API_KEY", "").strip(),
        groq_model=e("GROQ_MODEL", "openai/gpt-oss-120b").strip(),
        llm_daily_games=int(e("LLM_DAILY_GAMES", "400")),
        steam_api_key=e("STEAM_API_KEY", "").strip(),
        igdb_client_id=e("IGDB_CLIENT_ID", "").strip(),
        igdb_client_secret=e("IGDB_CLIENT_SECRET", "").strip(),
        reddit_client_id=e("REDDIT_CLIENT_ID", "").strip(),
        reddit_client_secret=e("REDDIT_CLIENT_SECRET", "").strip(),
        store_cc=e("STORE_CC", "kz").strip().lower(),
        animate_cards=e("ANIMATE_CARDS", "1").strip().lower() in ("1", "true", "yes"),
        passport_max_age_days=int(e("PASSPORT_MAX_AGE_DAYS", "30")),
        catalog_pages=int(e("CATALOG_PAGES", "10")),
        log_level=e("LOG_LEVEL", "INFO"),
    )
