"""Once a day, in the background worker: a database backup and keeping the data folder small.

- backup: data/backups/gamefinder-YYYY-MM-DD.db.gz, a consistent copy (VACUUM INTO, safe with WAL),
  the last 3 kept, skipped when the disk is short;
- covers: data/covers keeps pictures used in the last 60 days and at most ~150 MB;
- vectors and snippets of games that are gone or turned out to be DLC, stale cards of non-Steam games, old queue rows;
- a one-off purge of the profile-era tables (the bot no longer keeps profiles);
- the WAL is checkpointed and SQLite re-plans its indexes.
"""

import gzip
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

KEEP_BACKUPS = 3
COVERS_MAX_BYTES = 150 * 1024 * 1024
COVERS_MAX_AGE = 60 * 86400


def run(db, cfg) -> None:
    run_files(db, cfg)
    run_db(db, cfg)


def run_files(db, cfg) -> None:
    """The slow part, safe in a worker thread: it opens its own connections and never uses db.conn."""
    _steps((backup, prune_covers), db, cfg)


def run_db(db, cfg) -> None:
    """Quick statements on the bot's own connection: call from the thread that opened it."""
    _steps((prune_data, purge_profiles, compact), db, cfg)


def _steps(steps, db, cfg) -> None:
    for step in steps:
        try:
            step(db, cfg)
        except Exception:
            log.exception("maintenance step %s failed", step.__name__)


def backup(db, cfg, keep: int = KEEP_BACKUPS) -> Path | None:
    src = Path(cfg.db_path)
    if not src.is_file():
        return None
    folder = src.parent / "backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    out = folder / f"gamefinder-{time.strftime('%Y-%m-%d')}.db.gz"
    if out.exists():
        return out
    if shutil.disk_usage(folder).free < 3 * src.stat().st_size + 300_000_000:
        log.warning("backup skipped: not enough free disk")
        return None
    raw = folder / "backup.tmp"
    raw.unlink(missing_ok=True)
    con = sqlite3.connect(str(src))
    try:
        con.execute("VACUUM INTO ?", (str(raw),))
    finally:
        con.close()
    part = folder / "backup.gz.tmp"
    with open(raw, "rb") as f, gzip.open(part, "wb", 6) as g:
        shutil.copyfileobj(f, g)
    os.replace(part, out)
    raw.unlink(missing_ok=True)
    os.chmod(out, 0o600)
    for old in sorted(folder.glob("gamefinder-*.db.gz"))[:-keep]:
        old.unlink()
    log.info("backup written: %s (%d KB)", out.name, out.stat().st_size // 1024)
    return out


def prune_covers(db, cfg, max_bytes: int = COVERS_MAX_BYTES, max_age: float = COVERS_MAX_AGE) -> int:
    folder = Path(cfg.db_path).parent / "covers"
    if not folder.is_dir():
        return 0
    now = time.time()
    files = sorted((p.stat().st_mtime, p.stat().st_size, p) for p in folder.iterdir() if p.is_file())
    removed, total = 0, sum(size for _, size, _ in files)
    for mtime, size, p in files:          # oldest first
        if now - mtime > max_age or total > max_bytes:
            p.unlink(missing_ok=True)
            total -= size
            removed += 1
    if removed:
        log.info("covers: removed %d old pictures", removed)
    return removed


def prune_data(db, cfg) -> None:
    c = db.conn
    # Only games that are gone or turned out to be DLC: small games a user asked about stay indexed
    # (otherwise the worker would rebuild their vectors every night).
    gone = "appid NOT IN (SELECT appid FROM games) OR appid IN (SELECT appid FROM games WHERE is_dlc = 1)"
    for table in ("game_vectors", "review_snippets"):
        if _has(c, table):
            c.execute(f"DELETE FROM {table} WHERE appid > 0 AND ({gone})")
    if _has(c, "external_games"):
        c.execute("DELETE FROM external_games WHERE updated_at < ?", (int(time.time()) - 90 * 86400,))
    c.execute("DELETE FROM analysis_queue WHERE added_at < ? AND priority < 8", (int(time.time()) - 14 * 86400,))
    c.commit()


def purge_profiles(db, cfg) -> None:
    """The bot keeps no profiles since 0.2: clear what older versions stored, once."""
    if db.get_meta("profiles_purged") == "1":
        return
    c = db.conn
    c.execute("DELETE FROM user_games")
    c.execute("DELETE FROM shown")
    c.execute("UPDATE users SET prefs='{}', dealbreakers='[]', steam_id='', own_words=''")
    if _has(c, "user_vectors"):
        c.execute("DELETE FROM user_vectors")
    c.commit()
    db.set_meta("profiles_purged", "1")
    log.info("old profile data purged")


def compact(db, cfg) -> None:
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.conn.execute("PRAGMA optimize")


def _has(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
