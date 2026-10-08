"""Offline tests for gamefinder.maintenance. Run: python tests_maintenance.py

Each test builds a throwaway database in a temporary folder and runs one daily step on it.
"""

import gzip
import logging
import os
import sqlite3
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gamefinder import external, maintenance, semantic  # noqa: E402
from gamefinder.db import Db  # noqa: E402


def make_db(folder: str) -> tuple[Db, SimpleNamespace]:
    path = os.path.join(folder, "gamefinder.db")
    db = Db(path)
    semantic.ensure_schema(db.conn)
    external.ensure_schema(db.conn)
    return db, SimpleNamespace(db_path=path)


def add_game(db: Db, appid: int, dlc: int = 0) -> None:
    db.conn.execute("INSERT INTO games (appid, name, name_lc, is_dlc, positive) VALUES (?, ?, ?, ?, 500)",
                    (appid, f"Game {appid}", f"game {appid}", dlc))
    db.conn.execute("INSERT INTO game_vectors (appid, kind, vec) VALUES (?, 'experience', x'00')", (appid,))
    db.conn.commit()


def test_backup_is_a_readable_copy_and_keeps_three():
    with tempfile.TemporaryDirectory() as tmp:
        db, cfg = make_db(tmp)
        add_game(db, 10)
        folder = os.path.join(tmp, "backups")
        os.makedirs(folder)
        for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
            with open(os.path.join(folder, f"gamefinder-{day}.db.gz"), "wb") as f:
                f.write(b"old")
        out = maintenance.backup(db, cfg)
        assert out is not None and out.is_file()
        raw = os.path.join(tmp, "check.db")
        with gzip.open(out, "rb") as g, open(raw, "wb") as f:
            f.write(g.read())
        con = sqlite3.connect(raw)
        assert con.execute("SELECT name FROM games").fetchone()[0] == "Game 10"
        con.close()
        names = sorted(os.listdir(folder))
        assert len(names) == 3 and "gamefinder-2026-01-01.db.gz" not in names, names
        assert not any(n.endswith(".tmp") for n in names)
        assert maintenance.backup(db, cfg) == out          # once a day
        db.conn.close()


def test_prune_covers_drops_old_and_oversized():
    with tempfile.TemporaryDirectory() as tmp:
        db, cfg = make_db(tmp)
        covers = os.path.join(tmp, "covers")
        os.makedirs(covers)
        now = time.time()
        for name, age in (("old.jpg", 90), ("a.jpg", 3), ("b.jpg", 2), ("c.jpg", 1)):
            p = os.path.join(covers, name)
            with open(p, "wb") as f:
                f.write(b"x" * 1000)
            os.utime(p, (now - age * 86400, now - age * 86400))
        removed = maintenance.prune_covers(db, cfg, max_bytes=2000)
        assert removed == 2, removed
        assert sorted(os.listdir(covers)) == ["b.jpg", "c.jpg"]
        db.conn.close()


def test_prune_data_keeps_small_games_and_drops_gone_ones():
    with tempfile.TemporaryDirectory() as tmp:
        db, cfg = make_db(tmp)
        add_game(db, 1)
        add_game(db, 2, dlc=1)
        db.conn.execute("UPDATE games SET positive = 5 WHERE appid = 1")     # small, but asked about
        db.conn.execute("INSERT INTO game_vectors (appid, kind, vec) VALUES (3, 'experience', x'00')")   # gone
        db.conn.execute("INSERT INTO game_vectors (appid, kind, vec) VALUES (-7, 'experience', x'00')")  # external
        old = int(time.time()) - 100 * 86400
        db.conn.execute("INSERT INTO external_games VALUES ('old', '{}', ?)", (old,))
        db.conn.execute("INSERT INTO external_games VALUES ('new', '{}', ?)", (int(time.time()),))
        db.conn.execute("INSERT INTO analysis_queue VALUES (50, 1, ?)", (old,))
        db.conn.execute("INSERT INTO analysis_queue VALUES (51, 9, ?)", (old,))
        db.conn.commit()
        maintenance.prune_data(db, cfg)
        left = sorted(r[0] for r in db.conn.execute("SELECT appid FROM game_vectors"))
        assert left == [-7, 1], left
        assert [r[0] for r in db.conn.execute("SELECT title_key FROM external_games")] == ["new"]
        assert [r[0] for r in db.conn.execute("SELECT appid FROM analysis_queue")] == [51]
        db.conn.close()


def test_purge_profiles_runs_once_and_keeps_region():
    with tempfile.TemporaryDirectory() as tmp:
        db, cfg = make_db(tmp)
        db.conn.execute("INSERT INTO users (user_id, prefs, steam_id, region, created_at) "
                        "VALUES (1, '{\"pace\": 3}', '765', 'ru', 0)")
        db.conn.execute("INSERT INTO user_games VALUES (1, 10, 'love', 0, 'chat', '', 0)")
        db.conn.commit()
        maintenance.purge_profiles(db, cfg)
        row = db.conn.execute("SELECT prefs, steam_id, region FROM users").fetchone()
        assert tuple(row) == ("{}", "", "ru"), tuple(row)
        assert db.conn.execute("SELECT COUNT(*) FROM user_games").fetchone()[0] == 0
        db.conn.execute("INSERT INTO user_games VALUES (1, 11, 'love', 0, 'chat', '', 0)")
        db.conn.commit()
        maintenance.purge_profiles(db, cfg)             # already done: nothing touched
        assert db.conn.execute("SELECT COUNT(*) FROM user_games").fetchone()[0] == 1
        db.conn.close()


def test_run_survives_a_failing_step():
    with tempfile.TemporaryDirectory() as tmp:
        db, cfg = make_db(tmp)
        add_game(db, 2, dlc=1)
        orig = maintenance.backup
        maintenance.backup = lambda db, cfg: 1 / 0
        try:
            maintenance.run(db, cfg)
        finally:
            maintenance.backup = orig
        assert db.conn.execute("SELECT COUNT(*) FROM game_vectors").fetchone()[0] == 0   # later steps ran
        assert db.get_meta("profiles_purged") == "1"
        db.conn.close()


def test_daily_run_from_the_worker_thread_layout():
    """The worker runs the file steps in a thread and the database steps on its own thread (SQLite
    refuses a connection from another thread): nothing may fail that way."""
    import asyncio
    with tempfile.TemporaryDirectory() as tmp:
        db, cfg = make_db(tmp)
        add_game(db, 2, dlc=1)
        errors = []
        handler = logging.Handler()
        handler.emit = lambda record: errors.append(record.getMessage())
        maintenance.log.addHandler(handler)
        try:
            async def go():
                await asyncio.to_thread(maintenance.run_files, db, cfg)
                maintenance.run_db(db, cfg)
            asyncio.run(go())
        finally:
            maintenance.log.removeHandler(handler)
        assert not [e for e in errors if "failed" in e], errors
        assert db.get_meta("profiles_purged") == "1"
        assert os.listdir(os.path.join(tmp, "backups"))
        db.conn.close()


def main():
    logging.basicConfig(level=logging.CRITICAL)
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
