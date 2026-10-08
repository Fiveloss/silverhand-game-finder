"""Entry point: python -m gamefinder.main (run from the project directory)."""

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat

from . import profile
from .bot import App, build_router, commands
from .config import load_config
from .db import Db
from .http import Http
from .service import Service

log = logging.getLogger("gamefinder")

async def run() -> None:
    os.chdir(Path(__file__).resolve().parent.parent)
    cfg = load_config()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)

    db = Db(cfg.db_path)
    http = Http()
    service = Service(cfg, db, http)
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    app = App(service, bot)
    dp = Dispatcher()
    dp.include_router(build_router(app))

    # English by default; Telegram shows the Russian menu to people whose app is in Russian.
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in commands("en")])
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in commands("ru")], language_code="ru")
    await profile.apply(bot, cfg.open_access)       # the description and the about text, in both languages
    for owner in cfg.owner_ids:
        try:
            await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in commands("en", owner=True)],
                                      scope=BotCommandScopeChat(chat_id=owner))
        except Exception as e:
            log.warning("owner commands for %s: %s", owner, e)

    log.info("analyst: %s, up to %d games a day", service.analyst.describe(), cfg.llm_daily_games)
    stop = asyncio.Event()
    worker = asyncio.create_task(service.worker(stop))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        stop.set()
        # The worker may be inside a slow Steam or model call: give it a moment, then cut it off.
        try:
            await asyncio.wait_for(worker, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        for task in list(app.tasks):
            task.cancel()
        await http.close()
        await bot.session.close()
        db.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
