"""Telegram handlers. Private chats only; access for the owners, ALLOWED_IDS and /allow-ed friends.

The bot helps in the moment. Everything except the game cards happens in ONE panel message that
is edited in place (an animation with a caption and buttons): the home screen, the questions,
the "reading reviews" progress and the controls under a selection. When cards are posted, or the
player types, the old panel is deleted and a fresh one appears at the bottom: the menu follows
the player instead of piling up messages.

A request goes: words or a mood button -> «Чем именно зацепила X?» (when a game is named) ->
«Что точно не надо?» -> «Сколько есть времени?» -> three cards. Every question can be skipped
("Подобрать сейчас"), and anything typed while the questions or the results are on screen is
taken as a correction of the current request («без хоррора, покороче»). Nothing about the player
is remembered between requests.

users.state: "" (text = a new request) | "wizard" (a correction or an answer to the question on
screen) | "fix" (a correction of the results) | "game" (a title to break down) | "like" (titles
to find similar games to).
users.session: {"req", "seen", "shown", "step", "ask", "avoid", "panel", "panel_screen"}.
"""

import asyncio
import json
import logging
import re
from html import escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BotCommandScopeChat, BufferedInputFile, CallbackQuery, FSInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup, InputMediaAnimation, LinkPreviewOptions,
                           Message, ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import animate, aspects, intent, render, texts, titles, views
from .intent import Request
from .service import Service

log = logging.getLogger(__name__)

MEDIA = Path(__file__).resolve().parent.parent / "assets" / "menu"
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Quick requests on the home screen: (key, button, the request it stands for)
QUICK = [
    ("like", "🎯 Похожее на игру", None),
    ("evening", "🌙 На вечер", Request(text="на вечер", mood="evening", max_hours=4)),
    ("long", "♾ Залипнуть надолго", Request(text="залипнуть надолго", min_hours=30)),
    ("coop", "🤝 С другом", Request(text="с другом", mood="coop", coop=True)),
    ("chill", "😌 Расслабиться", Request(text="расслабиться", mood="chill")),
    ("challenge", "🔥 Челлендж", Request(text="челлендж", mood="challenge")),
    ("story", "📖 Сюжет", Request(text="сильный сюжет", mood="story")),
    ("gems", "💎 Жемчужины", Request(text="скрытые жемчужины", mood="gems")),
    ("surprise", "🎲 Удиви меня", Request(text="удиви меня", surprise=True)),
]
QUICK_REQ = {k: req for k, _, req in QUICK}

# «Что точно не надо?»: (key, button, what it adds to the request)
AVOID = [
    ("horror", "Хоррор", {"dealbreakers": ["horror"], "tags_avoid": ["Horror"]}),
    ("shooter", "Шутеры", {"tags_avoid": ["FPS", "Shooter"]}),
    ("text", "Чтение", {"tags_avoid": ["Visual Novel", "Text-Based"]}),
    ("hard", "Сложно", {"axes": {"difficulty": 3}}),
    ("grind", "Гринд", {"axes": {"grind": 2}}),
    ("pvp", "Онлайн и PvP", {"dealbreakers": ["online_only"], "tags_avoid": ["PvP"]}),
    ("mtx", "Донат", {"dealbreakers": ["mtx"]}),
    ("early", "Ранний доступ", {"dealbreakers": ["early_access"]}),
    ("noru", "Без русского", {"dealbreakers": ["no_ru"]}),
]
AVOID_MAP = {k: (label, eff) for k, label, eff in AVOID}

# «Сколько есть времени?»: (key, button, max_hours, min_hours, mood)
TIME = [
    ("evening", "🌙 Вечер · до 4 ч", 4, None, "evening"),
    ("couple", "Пара вечеров · до 10 ч", 10, None, None),
    ("week", "Неделя · до 25 ч", 25, None, None),
    ("long", "♾ Надолго · 30+ ч", None, 30, None),
    ("any", "Неважно", None, None, None),
]
TIME_MAP = {k: (mx, mn, mood) for k, _, mx, mn, mood in TIME}

# Steam store regions: (country code for the store API, button)
REGIONS = [
    ("ru", "🇷🇺 Россия"), ("kz", "🇰🇿 Казахстан"), ("ua", "🇺🇦 Украина"), ("by", "🇧🇾 Беларусь"),
    ("us", "🇺🇸 США"), ("de", "🇪🇺 Европа"), ("gb", "🇬🇧 Великобритания"), ("tr", "🇹🇷 Турция"),
]
REGION_NAMES = dict(REGIONS)

COMMANDS = [
    ("find", "Подобрать игру"),
    ("game", "Разбор игры по отзывам"),
    ("help", "Как это работает"),
    ("start", "Начало"),
]
OWNER_COMMANDS = COMMANDS + [("stats", "Каталог и лимиты"), ("allow", "Пустить друга: /allow id")]


class App:
    def __init__(self, service: Service, bot: Bot):
        self.s = service
        self.db = service.db
        self.cfg = service.cfg
        self.bot = bot
        self._file_ids: dict[str, str] = {}
        self.knocked: set[int] = set()   # strangers the owners have already been told about
        self.running: set[int] = set()   # players whose request is being worked on
        self.busy: set[int] = set()       # players whose message is being handled
        self.tasks: set[asyncio.Task] = set()   # card animations running after the cards are out

    def allowed(self, user_id: int) -> bool:
        return (self.cfg.open_access or user_id in self.cfg.owner_ids or user_id in self.cfg.allowed_ids
                or user_id in self.db.allowed_users())

    # --- session
    def session(self, user_id: int) -> dict:
        try:
            data = json.loads(self.db.user(user_id)["session"] or "{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, KeyError):
            return {}

    def update_session(self, user_id: int, **fields) -> dict:
        sess = self.session(user_id)
        for k, v in fields.items():
            if isinstance(v, Request):
                v = v.to_json()
            sess[k] = v
        if isinstance(sess.get("seen"), list):
            sess["seen"] = sess["seen"][-60:]
        self.db.update_user(user_id, session=json.dumps(sess, ensure_ascii=False))
        return sess

    def request(self, user_id: int) -> Request | None:
        raw = self.session(user_id).get("req")
        return Request.from_json(raw) if raw else None

    # --- the panel: one message per player, edited in place
    def _media(self, screen: str):
        path = MEDIA / f"{screen}.mp4"
        if not path.is_file():
            return None
        return self._file_ids.get(screen) or FSInputFile(path)

    def _remember(self, screen: str, msg) -> None:
        if isinstance(msg, Message) and msg.animation:
            self._file_ids[screen] = msg.animation.file_id

    async def panel(self, user_id: int, screen: str, text: str, kb: InlineKeyboardMarkup | None = None,
                    fresh: bool = False) -> None:
        """Show the panel. fresh=True moves it to the bottom (after cards or a typed message)."""
        sess = self.session(user_id)
        pid = sess.get("panel")
        if pid and not fresh:
            try:
                if sess.get("panel_screen") == screen or self._media(screen) is None:
                    await self.bot.edit_message_caption(chat_id=user_id, message_id=pid, caption=text,
                                                        reply_markup=kb)
                else:
                    msg = await self.bot.edit_message_media(
                        chat_id=user_id, message_id=pid, reply_markup=kb,
                        media=InputMediaAnimation(media=self._media(screen), caption=text))
                    self._remember(screen, msg)
                self.update_session(user_id, panel_screen=screen)
                return
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    return
                log.info("panel edit failed, posting a new one: %s", e)
            except Exception as e:
                log.warning("panel edit failed: %s", e)
        if pid:
            await _delete(self.bot, user_id, pid)
        msg = None
        media = self._media(screen)
        if media is not None:
            try:
                msg = await self.bot.send_animation(user_id, media, caption=text, reply_markup=kb)
                self._remember(screen, msg)
            except Exception as e:
                self._file_ids.pop(screen, None)
                log.warning("panel animation %s failed: %s", screen, e)
        if msg is None:
            msg = await self.bot.send_message(user_id, text, reply_markup=kb, link_preview_options=NO_PREVIEW)
        self.update_session(user_id, panel=msg.message_id, panel_screen=screen if msg.animation else "")


# --- keyboards

def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
                                                 for row in rows if row])


def home_kb() -> InlineKeyboardMarkup:
    q = {k: label for k, label, _ in QUICK}
    return _kb([
        [(q["like"], "q:like")],
        [(q["evening"], "q:evening"), (q["long"], "q:long")],
        [(q["coop"], "q:coop"), (q["chill"], "q:chill")],
        [(q["challenge"], "q:challenge"), (q["story"], "q:story")],
        [(q["gems"], "q:gems"), (q["surprise"], "q:surprise")],
        [("🔎 Разбор игры", "game"), ("❔ Как это работает", "help")],
        [("🌍 Регион Steam", "region")],
    ])


def region_kb() -> InlineKeyboardMarkup:
    btns = [(label, f"rg:{cc}") for cc, label in REGIONS]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([("Другая страна", "rg:other"), ("◂ В начало", "home")])
    return _kb(rows)


def back_kb() -> InlineKeyboardMarkup:
    return _kb([[("◂ В начало", "home")]])


def aspect_kb(opts, picked) -> InlineKeyboardMarkup:
    rows = aspects.keyboard(opts, picked)
    rows = [[(t, d) for t, d in row if d not in ("asp:all", "asp:done")] for row in rows]
    rows.append([("Дальше ▸" if picked else "Всё сразу ▸", "asp:done" if picked else "asp:all")])
    rows.append([("🤔 Не та игра", "wrong"), ("⚡ Подобрать сейчас", "go")])
    rows.append([("◂ В начало", "home")])
    return _kb(rows)


def avoid_kb(picked) -> InlineKeyboardMarkup:
    btn = {k: (("✓ " if k in picked else "") + label, f"av:{k}") for k, label, _ in AVOID}
    rows = [[btn["horror"], btn["shooter"], btn["hard"]], [btn["grind"], btn["mtx"], btn["text"]],
            [btn["pvp"], btn["noru"]], [btn["early"]]]
    rows.append([("Дальше ▸" if picked else "Ничего, дальше ▸", "av:done")])
    rows.append([("⚡ Подобрать сейчас", "go"), ("◂ В начало", "home")])
    return _kb(rows)


def time_kb() -> InlineKeyboardMarkup:
    btns = [(label, f"tm:{k}") for k, label, *_ in TIME]
    return _kb([btns[:2], btns[2:4], btns[4:], [("◂ В начало", "home")]])


def results_kb() -> InlineKeyboardMarkup:
    r = intent.REFINES
    return _kb([
        [(r["shorter"], "rf:shorter"), (r["easier"], "rf:easier"), (r["harder"], "rf:harder")],
        [(r["story"], "rf:story"), (r["chill"], "rf:chill"), (r["different"], "rf:different")],
        [("🔄 Ещё 3", "more"), ("◂ В начало", "home")],
    ])


def loosen_kb() -> InlineKeyboardMarkup:
    l = intent.LOOSEN  # noqa: E741
    return _kb([[(l["noavoid"], "rf:noavoid"), (l["anylen"], "rf:anylen")],
                [(intent.REFINES["different"], "rf:different"), ("◂ В начало", "home")]])


def pick_kb(appid: int, marked: str = "") -> InlineKeyboardMarkup:
    def b(verdict: str, label: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=("✓ " if marked == verdict else "") + label,
                                    callback_data=f"fb:{verdict}:{appid}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Подробно", callback_data=f"pp:{appid}"),
         InlineKeyboardButton(text="Steam ↗", url=texts.store_url(appid))],
        [b("played", "✅ Уже играл"), b("skip", "👎 Не то")],
    ])


def game_kb(appid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Найти похожие", callback_data=f"sim:{appid}"),
         InlineKeyboardButton(text="Steam ↗", url=texts.store_url(appid))],
    ])


# --- panel texts

def home_text(name: str = "") -> str:
    hello = f"Привет, {escape(name)}. " if name else ""
    return texts.card(
        "Во что поиграть сейчас?", lead="Silverhand Game Finder · по честным отзывам игроков",
        note=(f"{hello}Напиши своими словами, например:\n"
              "<i>«как Hollow Knight, но попроще, на пару вечеров»</i>\n"
              "<i>«кооп с другом на выходные, не шутер»</i>\n\n"
              "Или выбери настроение кнопкой."))


def help_text() -> str:
    return texts.card(
        "Как я подбираю", lead="Без анкет: только то, что хочется сейчас",
        note=("<b>1.</b> Пишешь, во что хочется, или жмёшь настроение.\n"
              "<b>2.</b> Если назвал игру, спрошу, чем она зацепила, чего не надо и сколько есть времени. "
              "Любой вопрос можно пропустить.\n"
              "<b>3.</b> Ищу по тегам игроков, по тому, во что залипают люди с похожим вкусом, и по "
              "советам нейросети, а потом сверяю со свежими отзывами Steam и GOG.\n"
              "<b>4.</b> Показываю три игры: 🟢 — отзывы разобраны, ⚪ — пока только по тегам.\n"
              "<b>5.</b> Не то? Напиши поправку: «без хоррора», «покороче». Начать заново — «◂ В начало».\n\n"
              "Помню только регион Steam, чтобы цены были твои."))


def wizard_lead(req: Request) -> str:
    label = req.label()
    return f"Запрос: {escape(label)}" if label and label != "что угодно" else ""


def build_router(app: App) -> Router:
    r = Router()
    r.message.filter(F.chat.type == "private")
    db, s = app.db, app.s

    @r.callback_query.outer_middleware()
    async def callback_gate(handler, c: CallbackQuery, data: dict):
        if not app.allowed(c.from_user.id):
            await c.answer("Бот приватный")
            return
        db.user(c.from_user.id, c.from_user.first_name or "")     # a row for state and session
        return await handler(c, data)

    async def gate(m: Message) -> bool:
        uid = m.from_user.id
        if app.allowed(uid):
            db.user(uid, m.from_user.first_name or "")
            return True
        await m.answer(texts.card("Бот приватный", lead="Пока только для своих",
                                  note=("Я уже написал владельцу. Если он тебя пустит, придёт сообщение. "
                                        f"На всякий случай твой id: <code>{uid}</code>.")))
        if uid in app.knocked:
            return False
        app.knocked.add(uid)
        name = escape(m.from_user.full_name + (f" @{m.from_user.username}" if m.from_user.username else ""))
        kb = _kb([[("Пустить", f"allow:{uid}")]])
        for owner in app.cfg.owner_ids:
            try:
                await app.bot.send_message(owner, texts.card("Просится в бота", [("Кто", name), ("id", uid)]),
                                           reply_markup=kb)
            except Exception as e:
                log.warning("could not notify owner %s: %s", owner, e)
        return False

    async def home(uid: int, fresh: bool = False, name: str = "") -> None:
        db.update_user(uid, state="")
        app.update_session(uid, step="home", pending_req=None, pending=None)
        await app.panel(uid, "start", home_text(name), home_kb(), fresh=fresh)

    # --- commands
    @r.message(CommandStart())
    async def start(m: Message):
        if not await gate(m):
            return
        uid = m.from_user.id
        if uid in app.cfg.owner_ids:
            # At launch Telegram refuses a scope for a chat the owner never opened; set it now.
            try:
                await app.bot.set_my_commands([BotCommand(command=c, description=d) for c, d in OWNER_COMMANDS],
                                              scope=BotCommandScopeChat(chat_id=uid))
            except Exception as e:
                log.warning("owner commands: %s", e)
        # The old version left a reply keyboard under the input field: take it away once.
        if not app.session(uid).get("kb_removed"):
            try:
                tmp = await m.answer("🟠", reply_markup=ReplyKeyboardRemove())
                await _delete(app.bot, uid, tmp.message_id)
            except Exception:
                pass
            app.update_session(uid, kb_removed=True)
        await _delete(app.bot, uid, m.message_id)
        await home(uid, fresh=True, name=m.from_user.first_name or "")

    @r.message(Command("find"))
    async def find(m: Message):
        if not await gate(m):
            return
        await _delete(app.bot, m.from_user.id, m.message_id)
        await home(m.from_user.id, fresh=True)

    @r.message(Command("game"))
    async def game_cmd(m: Message):
        if not await gate(m):
            return
        await _delete(app.bot, m.from_user.id, m.message_id)
        await ask_game(m.from_user.id, fresh=True)

    @r.message(Command("help"))
    async def help_cmd(m: Message):
        if not await gate(m):
            return
        await _delete(app.bot, m.from_user.id, m.message_id)
        await app.panel(m.from_user.id, "help", help_text(), back_kb(), fresh=True)

    async def ask_game(uid: int, fresh: bool = False) -> None:
        db.update_user(uid, state="game")
        app.update_session(uid, step="game")
        await app.panel(uid, "game", texts.card("Разбор игры", note=(
            "Напиши название, и я покажу, что о ней говорят игроки <b>сейчас</b>: настоящий жанр, "
            "ощущения, за что хвалят и за что ругают.")), back_kb(), fresh=fresh)

    @r.callback_query(F.data == "home")
    async def on_home(c: CallbackQuery):
        await c.answer()
        await home(c.from_user.id)

    @r.callback_query(F.data == "help")
    async def on_help(c: CallbackQuery):
        await c.answer()
        await app.panel(c.from_user.id, "help", help_text(), back_kb())

    @r.callback_query(F.data == "game")
    async def on_game(c: CallbackQuery):
        await c.answer()
        await ask_game(c.from_user.id)

    # --- the questions before a search
    async def wizard(uid: int, req: Request, fresh: bool = False) -> None:
        """Ask what is still unknown, one question at a time, in the panel."""
        db.update_user(uid, state="wizard")
        if req.seeds and not req.asked:
            await app.panel(uid, "pick", texts.card("Вспоминаю игру", lead=escape(req.seeds[0])), None,
                            fresh=fresh)
            fresh = False
            try:
                g, p, opts = await s.aspect_options(req.seeds[0])
            except Exception:
                log.exception("aspect options failed")
                g, p, opts = None, None, []
            req.asked = True
            if g and len(opts) >= 2:
                ask = {"seed": g, "feel": (p or {}).get("feel"), "opts": aspects.dump(opts), "picked": []}
                app.update_session(uid, req=req, step="aspects", ask=ask)
                example = f"«{opts[0].label.lower()}, а {opts[-1].label.lower()} не главное»"
                await app.panel(uid, "pick", texts.card(
                    escape(aspects.question(g["name"])), lead=wizard_lead(req),
                    note=f"Отметь, что зацепило, или ответь словами: <i>{escape(example)}</i>."),
                    aspect_kb(opts, []))
                return
            if not g:
                app.update_session(uid, missing_note=req.seeds[0])
        await ask_avoid(uid, req, fresh)

    async def ask_avoid(uid: int, req: Request, fresh: bool = False) -> None:
        # Exclusions already understood from the text start ticked.
        picked = [k for k, _, eff in AVOID
                  if (eff.get("dealbreakers") and set(eff["dealbreakers"]) <= set(req.dealbreakers))
                  or (eff.get("tags_avoid") and set(eff["tags_avoid"]) <= set(req.tags_avoid))]
        missing = app.session(uid).get("missing_note")
        app.update_session(uid, req=req, step="avoid", avoid=picked, missing_note=None)
        db.update_user(uid, state="wizard")
        note = "Отметь, что исключить, или напиши: <i>«без пиксельной графики и без выживания»</i>."
        if missing:
            note = (f"Не нашёл «{escape(missing)}» в Steam: подберу по остальному. Опечатка? Напиши название "
                    "ещё раз.\n\n" + note)
        await app.panel(uid, "pick", texts.card("Что точно не надо?", lead=wizard_lead(req), note=note),
                        avoid_kb(picked), fresh=fresh)

    async def ask_time(uid: int, req: Request) -> None:
        if req.max_hours or req.min_hours or req.mood in ("evening",):
            await run(uid, req)
            return
        app.update_session(uid, req=req, step="time")
        await app.panel(uid, "pick", texts.card("Сколько есть времени?", lead=wizard_lead(req),
                                                note="Примерно, на всю игру целиком."), time_kb())

    @r.callback_query(F.data.startswith("asp:"))
    async def on_aspect(c: CallbackQuery):
        uid, key = c.from_user.id, c.data[4:]
        sess = app.session(uid)
        ask, req = sess.get("ask"), app.request(uid)
        if not ask or not req or sess.get("step") != "aspects":
            return await c.answer("Вопрос устарел")
        await c.answer()
        sess = app.session(uid)                     # another tap may have landed during the await
        ask = sess.get("ask")
        if not ask or sess.get("step") != "aspects":
            return
        opts = aspects.load(ask["opts"])
        if key in ("all", "done"):
            req = _apply_aspects(req, ask, aspects.pick(opts, ask.get("picked", [])), all_of_it=key == "all")
            await ask_avoid(uid, req)
            return
        picked = list(ask.get("picked", []))
        if key in picked:
            picked.remove(key)
        else:
            picked.append(key)
        ask["picked"] = picked
        app.update_session(uid, ask=ask)
        await app.panel(uid, "pick", texts.card(
            escape(aspects.question(ask["seed"]["name"])), lead=wizard_lead(req),
            note="Отметь, что зацепило. Можно ответить своими словами."), aspect_kb(opts, picked))

    @r.callback_query(F.data.startswith("av:"))
    async def on_avoid(c: CallbackQuery):
        uid, key = c.from_user.id, c.data[3:]
        sess = app.session(uid)
        req = app.request(uid)
        if not req or sess.get("step") != "avoid":
            return await c.answer("Вопрос устарел")
        await c.answer()
        sess = app.session(uid)                     # another tap may have landed during the await
        if sess.get("step") != "avoid":
            return
        picked = list(sess.get("avoid", []))
        if key == "done":
            await ask_time(uid, _apply_avoid(req, picked))
            return
        if key in picked:
            picked.remove(key)
        elif key in AVOID_MAP:
            picked.append(key)
        app.update_session(uid, avoid=picked)
        await app.panel(uid, "pick", texts.card(
            "Что точно не надо?", lead=wizard_lead(req),
            note="Отметь, что исключить, или напиши своими словами."), avoid_kb(picked))

    @r.callback_query(F.data.startswith("tm:"))
    async def on_time(c: CallbackQuery):
        uid = c.from_user.id
        req = app.request(uid)
        if not req or app.session(uid).get("step") != "time":
            return await c.answer("Вопрос устарел")
        await c.answer()
        mx, mn, mood = TIME_MAP.get(c.data[3:], (None, None, None))
        req.max_hours, req.min_hours = mx, mn
        if mood and req.mood == "any":
            req.mood = mood
        await run(uid, req)

    @r.callback_query(F.data == "go")
    async def on_go(c: CallbackQuery):
        uid = c.from_user.id
        sess = app.session(uid)
        req = app.request(uid)
        if not req:
            return await c.answer("Начни заново")
        await c.answer()
        if sess.get("step") == "aspects" and sess.get("ask"):
            opts = aspects.load(sess["ask"]["opts"])
            req = _apply_aspects(req, sess["ask"], aspects.pick(opts, sess["ask"].get("picked", [])))
        elif sess.get("step") == "avoid":
            req = _apply_avoid(req, sess.get("avoid", []))
        await run(uid, req)

    # --- the Steam region, for prices (asked once, changeable from the home screen)
    def region_of(uid: int) -> str:
        return (db.user(uid).get("region") or "").lower()

    async def ask_region(uid: int, fresh: bool = False) -> None:
        cur = region_of(uid)
        if app.session(uid).get("pending_req"):
            lead = "Один вопрос перед первой подборкой"
            note = ("Покажу цены и скидки как в твоём магазине Steam. Спрошу один раз, поменять можно "
                    "на главном экране.")
        else:
            lead = f"Сейчас: {REGION_NAMES.get(cur, cur.upper())}" if cur else "Чтобы цены были как в твоём Steam"
            note = ("Цены и скидки беру прямо из магазина Steam твоего региона перед каждой подборкой. "
                    "Если игра в регионе не продаётся, так и напишу.")
        await app.panel(uid, "start", texts.card("Регион аккаунта Steam", lead=lead, note=note),
                        region_kb(), fresh=fresh)

    @r.callback_query(F.data == "region")
    async def on_region(c: CallbackQuery):
        await c.answer()
        await ask_region(c.from_user.id)

    @r.callback_query(F.data.startswith("rg:"))
    async def on_region_pick(c: CallbackQuery):
        uid, cc = c.from_user.id, c.data[3:]
        if cc == "other":
            await c.answer()
            db.update_user(uid, state="region")
            await app.panel(uid, "start", texts.card("Другая страна", note=(
                "Напиши двухбуквенный код страны аккаунта, например <code>PL</code>, <code>AM</code>, "
                "<code>GE</code>, <code>UZ</code>.")), back_kb())
            return
        await c.answer(f"Регион: {REGION_NAMES.get(cc, cc.upper())}")
        await set_region(uid, cc)

    async def set_region(uid: int, cc: str) -> None:
        db.update_user(uid, region=cc.lower(), state="")
        pending = app.session(uid).get("pending_req")
        if pending:
            app.update_session(uid, pending_req=None)
            await run(uid, Request.from_json(pending), app.session(uid).get("seen", []))
        else:
            await home(uid)

    # --- search
    async def run(uid: int, req: Request, seen: list[int] | None = None) -> None:
        if not region_of(uid):
            # Prices must be the player's own: ask the region before the first search, then go on.
            app.update_session(uid, pending_req=req.to_json(), seen=list(seen or []))
            await ask_region(uid)
            return
        if uid in app.running:
            await app.panel(uid, "pick", texts.card("Уже ищу", note="Дождись текущей подборки, это меньше минуты."))
            return
        app.running.add(uid)
        try:
            db.update_user(uid, state="")
            seen = list(seen or [])
            label = req.label()
            app.update_session(uid, req=req, step="running")

            async def failed(fresh: bool = False) -> None:
                try:
                    await failed_panel(uid, seen, fresh)
                except Exception:
                    log.exception("failure panel")
            await app.panel(uid, "pick", views.stage_text("Понимаю, что искать", label), None)

            async def progress(i: int, n: int) -> None:
                try:
                    await app.panel(uid, "pick", views.reading(i, n, label), None)
                except Exception:
                    pass

            try:
                async def stage(what: str) -> None:
                    await app.panel(uid, "pick", views.stage_text(what, label), None)

                picks, seeds = await s.recommend_now(uid, req, seen=set(seen), progress=progress, stage=stage)
            except Exception:
                log.exception("request failed")
                await failed()
                return
            if not picks:
                db.update_user(uid, state="fix")
                app.update_session(uid, step="results", seen=seen, shown=[])
                await app.panel(uid, "pick", texts.card("Ничего не нашлось", lead=wizard_lead(req), note=(
                    "Под все условия сразу ничего не подошло. Сними часть ограничений кнопкой или напиши, "
                    "что можно: <i>«хоррор можно»</i>, <i>«можно подлиннее»</i>.")), loosen_kb())
            else:
                try:
                    picks, spare = picks[:3], picks[3:]
                    await send_cards(uid, req, picks, seeds, banner=not seen)
                    shown = [p.appid for p in picks]
                    app.update_session(uid, spare=[_pick_json(p) for p in spare])
                    db.update_user(uid, state="fix")
                    app.update_session(uid, step="results", seen=seen + shown, shown=shown)
                    few = ""
                    if len(picks) < 3 and all(p.judge_fit for p in picks):
                        few = (f"Уверенно подошли только {len(picks)} — остальных кандидатов нейросеть "
                               "отбраковала, чтобы не подсовывать «почти то». «Ещё 3» покажет следующих.\n\n")
                    await app.panel(uid, "pick", texts.card(
                        "Подкрутить?", lead=wizard_lead(req),
                        note=few + "Нажми кнопку или просто напиши поправку: <i>«без хоррора»</i>, "
                                   "<i>«покороче»</i>, <i>«а что-нибудь с кооперативом?»</i>"), results_kb(), fresh=True)
                except Exception:
                    log.exception("sending the picks failed")
                    await failed(fresh=True)            # under the cards that did go out
                    return
        finally:
            app.running.discard(uid)
        # Text typed during the search (the panel promised to use it): a correction of these results.
        # After a failure there is nothing to correct, and it is dropped with the search.
        pending = app.session(uid).get("pending")
        if pending:
            app.update_session(uid, pending=None)
            if app.session(uid).get("step") == "results":
                await correct(uid, pending, "fix")

    async def failed_panel(uid: int, seen: list[int], fresh: bool = False) -> None:
        app.update_session(uid, step="failed", seen=seen, pending=None)
        await app.panel(uid, "pick", texts.card("Не получилось", note=(
            "Не получилось собрать подборку: один из сервисов не ответил. Попробуй ещё раз через минуту.")),
                        _kb([[("🔄 Повторить", "retry"), ("◂ В начало", "home")]]), fresh=fresh)

    async def send_cards(uid: int, req: Request, picks, seeds, banner: bool = True) -> None:
        games = db.games([p.appid for p in picks] + [p.because for p in picks if p.because])
        covers, prices = await asyncio.gather(
            asyncio.gather(*(render.cover(s.http, p.appid) for p in picks)),
            s.prices([p.appid for p in picks], region_of(uid)))
        seed_names = [g["name"] for g in seeds]
        missing = [t for t in req.seeds[:4] if not any(titles.score(t, n) >= 0.75 for n in seed_names)]
        label = req.label()
        if banner:
            try:
                img = await asyncio.to_thread(render.selection_banner, "ПОДБОРКА", label or "по свежим отзывам",
                                              list(covers))
                await app.bot.send_photo(uid, BufferedInputFile(img, "selection.jpg"),
                                         caption=views.selection_caption(label, len(picks), seed_names, missing))
            except Exception:
                log.exception("banner failed")
        sent = []
        for rank, (p, cov) in enumerate(zip(picks, covers), 1):
            g = games.get(p.appid) or db.game(p.appid)
            stats = (db.review_stats(p.appid) or (None,))[0]
            liked = games.get(p.because, {}).get("name") if p.because else None
            caption = f"<b>{escape((g or {}).get('name') or str(p.appid))}</b>"
            try:
                caption = views.pick_caption(g, p, liked)
                view = views.pick_view(g, p, stats, rank, req.axes, prices.get(p.appid))
                img = await asyncio.to_thread(render.pick_card, view, cov)
                msg = await app.bot.send_photo(uid, BufferedInputFile(img, f"{p.appid}.jpg"), caption=caption,
                                               reply_markup=pick_kb(p.appid))
                sent.append((msg.message_id, view, cov, caption, p.appid))
            except Exception:
                log.exception("card failed for %s", p.appid)
                await app.bot.send_message(uid, caption, reply_markup=pick_kb(p.appid),
                                           link_preview_options=NO_PREVIEW)
        if sent and app.cfg.animate_cards:
            # The pictures are out at once; each becomes a short loop as soon as it is encoded.
            task = asyncio.create_task(animate_cards(uid, sent))
            app.tasks.add(task)
            task.add_done_callback(app.tasks.discard)

    async def animate_cards(uid: int, sent) -> None:
        for message_id, view, cov, caption, appid in sent:
            try:
                mp4 = await asyncio.to_thread(animate.pick_card_mp4, view, cov)
                if not mp4:
                    continue
                await app.bot.edit_message_media(
                    chat_id=uid, message_id=message_id, reply_markup=pick_kb(appid),
                    media=InputMediaAnimation(media=BufferedInputFile(mp4, f"{appid}.mp4"), caption=caption,
                                              width=960, height=540, duration=2))
            except TelegramBadRequest as e:
                log.info("card %s stays a picture: %s", appid, e)
            except Exception as e:
                log.warning("card animation %s failed: %s", appid, e)

    @r.callback_query(F.data.startswith("q:"))
    async def on_quick(c: CallbackQuery):
        await c.answer()
        uid, key = c.from_user.id, c.data[2:]
        if key == "like":
            db.update_user(uid, state="like")
            await app.panel(uid, "pick", texts.card("Похожее на игру", note=(
                "Напиши игру, на которую хочется похожего. Можно несколько через запятую, можно не из "
                "Steam: <i>«Alan Wake 2»</i>, <i>«Zelda»</i>.")), back_kb())
            return
        req = QUICK_REQ.get(key)
        if req:
            await run(uid, Request.from_json(req.to_json()))

    @r.callback_query(F.data.startswith("rf:"))
    async def on_refine(c: CallbackQuery):
        await c.answer()
        uid = c.from_user.id
        req = app.request(uid)
        if not req:
            return await home(uid)
        sess = app.session(uid)
        shown = [{"passport": db.passport(a) or {}, "game": db.game(a) or {}} for a in sess.get("shown", [])]
        await run(uid, intent.refine(req, c.data[3:], shown), sess.get("seen", []))

    @r.callback_query(F.data.in_({"more", "retry"}))
    async def on_more(c: CallbackQuery):
        await c.answer()
        uid = c.from_user.id
        req = app.request(uid)
        if not req:
            return await home(uid)
        await run(uid, req, app.session(uid).get("seen", []))

    @r.callback_query(F.data.startswith("sim:"))
    async def on_similar(c: CallbackQuery):
        await c.answer()
        g = db.game(int(c.data[4:]))
        if g:
            await wizard(c.from_user.id, Request(text=f"как {g['name']}", seeds=[g["name"]]), fresh=True)

    @r.callback_query(F.data.startswith("fb:"))
    async def on_feedback(c: CallbackQuery):
        _, verdict, appid = c.data.split(":")
        uid = c.from_user.id
        # Only for this request: "more" and refinements won't bring it back. Nothing is remembered after.
        sess = app.session(uid)
        if sess.get("req"):
            seen = sess.get("seen", [])
            if int(appid) not in seen:
                app.update_session(uid, seen=seen + [int(appid)])
        spare = list(sess.get("spare") or []) if sess.get("req") and int(appid) in (sess.get("shown") or []) else []
        nxt = _pick_from(spare.pop(0)) if spare else None
        if nxt:
            await c.answer("Понял, вот замена")
        else:
            await c.answer("Понял, в этом поиске больше не покажу" if sess.get("req") else "Понял")
        msg = c.message if isinstance(c.message, Message) else None    # older than 48 h: inaccessible
        rows = msg.reply_markup.inline_keyboard if msg and msg.reply_markup else []
        first = rows[0][0].callback_data if rows and rows[0] else None
        if first and first.startswith("pp:"):
            await _set_markup(msg, pick_kb(int(appid), verdict))
        if nxt:
            # The judge's next sure pick, kept back for exactly this: no new search, no wait.
            sess = app.session(uid)
            app.update_session(uid, spare=spare, shown=list(sess.get("shown") or []) + [nxt.appid],
                               seen=list(sess.get("seen") or []) + [nxt.appid])
            try:
                await send_cards(uid, app.request(uid) or Request(), [nxt], [], banner=False)
            except Exception:
                log.exception("replacement card failed")

    # --- a game's breakdown
    @r.callback_query(F.data.startswith("pp:"))
    async def on_passport(c: CallbackQuery):
        await c.answer()
        await show_game(c.from_user.id, int(c.data[3:]))

    async def show_game(uid: int, appid: int) -> None:
        if not s.passport_fresh(appid):
            await app.panel(uid, "game", texts.card("Читаю свежие отзывы", lead="До минуты"), None)
        s.busy += 1
        try:
            p = await s.analyze(appid)
        except Exception:
            log.exception("analyze failed")
            p = None
        finally:
            s.busy -= 1
        g = db.game(appid)
        if not p or not g:
            if app.session(uid).get("step") == "results":
                await app.panel(uid, "pick", texts.card("Не получилось", note=(
                    "Steam не отдал отзывы этой игры. Подборка на месте: подкрути её или напиши поправку.")),
                    results_kb())
            else:
                await app.panel(uid, "game", texts.card("Не получилось",
                                                        note="Steam не отдал отзывы. Попробуй позже."), back_kb())
            return
        stats = (db.review_stats(appid) or (None,))[0]
        caption = views.game_caption(g, p, stats)
        try:
            cov, prices = await asyncio.gather(render.cover(s.http, appid),
                                               s.prices([appid], region_of(uid) or s.cfg.store_cc))
            img = await asyncio.to_thread(render.game_card, views.game_view(g, p, stats, prices.get(appid)), cov)
            await app.bot.send_photo(uid, BufferedInputFile(img, f"{appid}.jpg"), caption=caption,
                                     reply_markup=game_kb(appid))
        except Exception:
            log.exception("game card failed for %s", appid)
            for chunk in _split(texts.passport_card(g, p, stats)):
                await app.bot.send_message(uid, chunk, link_preview_options=NO_PREVIEW)
        # The panel goes back under the card, as it was before the breakdown.
        step = app.session(uid).get("step")
        if step == "results":
            req = app.request(uid)
            await app.panel(uid, "pick", texts.card("Подкрутить?", lead=wizard_lead(req) if req else "",
                                                    note="Нажми кнопку или напиши поправку."),
                            results_kb(), fresh=True)
        else:
            await app.panel(uid, "game", texts.card("Ещё разбор?", note="Напиши следующее название."),
                            _kb([[("🎯 Найти похожие", f"sim:{appid}"), ("◂ В начало", "home")]]), fresh=True)
            db.update_user(uid, state="game")

    async def breakdown_from_text(uid: int, text: str) -> None:
        await app.panel(uid, "game", texts.card("Ищу игру", lead=escape(text[:60])), None, fresh=True)
        games = await s.resolve(text, limit=5)
        if not games:
            await app.panel(uid, "game", texts.card("Не нашёл", note=(
                "В Steam такой игры нет или название другое. Попробуй по-английски.")), back_kb())
            db.update_user(uid, state="game")
            return
        if len(games) == 1 or games[0]["name_lc"] == text.lower().strip():
            await show_game(uid, games[0]["appid"])
            return
        kb = InlineKeyboardBuilder()
        for g in games:
            kb.button(text=g["name"][:60], callback_data=f"pp:{g['appid']}")
        kb.button(text="◂ В начало", callback_data="home")
        kb.adjust(1)
        await app.panel(uid, "game", texts.card("Какую из них?"), kb.as_markup())

    # --- owners
    async def grant(uid: int) -> None:
        db.user(uid)
        db.update_user(uid, allowed=1)
        try:
            await app.bot.send_message(uid, texts.card("Тебя пустили", note=(
                "Жми /start: подберу, во что поиграть прямо сейчас.")))
        except Exception as e:
            log.info("could not tell %s about access: %s", uid, e)

    @r.callback_query(F.data.startswith("allow:"))
    async def on_allow(c: CallbackQuery):
        if c.from_user.id not in app.cfg.owner_ids:
            return await c.answer()
        await c.answer("Пустил")
        await _set_markup(c.message, _kb([[("✓ Пустил", "noop")]]))
        await grant(int(c.data[6:]))

    @r.message(Command("allow"))
    async def allow(m: Message, command: CommandObject):
        if m.from_user.id not in app.cfg.owner_ids:
            return
        if not command.args or not command.args.strip().isdigit():
            await m.answer(texts.card("Пустить друга", note="Пришли его id: <code>/allow 123456789</code>"))
            return
        uid = int(command.args.strip())
        await grant(uid)
        await m.answer(texts.card("Доступ открыт", [("id", uid)]))

    @r.message(Command("stats"))
    async def stats(m: Message):
        if m.from_user.id not in app.cfg.owner_ids:
            return
        s.refresh_catalog()
        rows = [("Игр в базе", db.game_count()), ("Готовы к подбору", len(s.catalog.vecs)),
                ("Очередь анализа", db.queue_size()),
                ("Разборов отзывов сегодня", f"{db.llm_games_today()} из {app.cfg.llm_daily_games}")]
        for u in db.llm_usage(7):
            rows.append((u["day"], f"{u['games']} разборов, {u['input_tokens'] // 1000}k / "
                                   f"{u['output_tokens'] // 1000}k токенов"))
        await m.answer(texts.card("Статистика", rows, lead=f"Аналитик: {escape(s.analyst.describe())}"))

    # --- free text
    @r.message(F.text & ~F.text.startswith("/"))
    async def text(m: Message):
        if not await gate(m):
            return
        uid, said = m.from_user.id, m.text.strip()
        state = db.user(uid)["state"]
        await _delete(app.bot, uid, m.message_id)      # keep the chat clean: the panel shows what was understood
        if uid in app.running:
            app.update_session(uid, pending=said)       # a search is on: take it as a correction afterwards
            await app.panel(uid, "pick", texts.card("Ищу", lead=f"Потом учту: «{escape(said[:60])}»",
                                                    note="Дождись подборки, поправку применю сразу после."), None)
            return
        if uid in app.busy:
            # The previous message is still being read (a game looked up, a request understood):
            # this one goes next, on top of what that one leads to.
            app.update_session(uid, pending=said)
            return
        app.busy.add(uid)
        try:
            await handle_text(uid, said, state)
            for _ in range(3):
                nxt = app.session(uid).get("pending")
                if not nxt or uid in app.running:
                    break
                app.update_session(uid, pending=None)
                await handle_text(uid, nxt, db.user(uid)["state"])
        finally:
            app.busy.discard(uid)

    async def handle_text(uid: int, said: str, state: str) -> None:
        try:
            if state == "region":
                code = re.sub(r"[^a-z]", "", said.lower())
                if len(code) == 2:
                    await set_region(uid, code)
                else:
                    await app.panel(uid, "start", texts.card("Нужен код из двух букв", note=(
                        "Например <code>PL</code> или <code>AM</code>.")), back_kb(), fresh=True)
            elif state == "game":
                await breakdown_from_text(uid, said)
            elif state == "like":
                names = [t.strip() for t in re.split(r"[,\n;]+| и ", said) if t.strip()][:4]
                await wizard(uid, Request(text="как " + ", ".join(names), seeds=names), fresh=True)
            elif state == "wizard" and app.session(uid).get("step") == "aspects":
                await answer_aspects_text(uid, said)
            elif state in ("wizard", "fix") and app.request(uid):
                await correct(uid, said, state)
            else:
                await new_request(uid, said)
        except Exception:
            log.exception("text handler failed")
            await app.panel(uid, "pick", texts.card("Не получилось", note="Что-то пошло не так. Попробуй ещё раз."),
                            back_kb(), fresh=True)

    async def new_request(uid: int, said: str, req: Request | None = None) -> None:
        if req is None:                                 # correct() passes the text it has already read
            await app.panel(uid, "pick", texts.card("Понимаю запрос", lead=escape(said[:80])), None, fresh=True)
            try:
                req = await intent.parse(s.analyst, said)
            except Exception:
                log.exception("intent parse failed")
                req = intent.parse_heuristic(said)
        app.update_session(uid, seen=[], shown=[])
        await wizard(uid, req)

    async def answer_aspects_text(uid: int, said: str) -> None:
        sess = app.session(uid)
        req, ask = app.request(uid), sess.get("ask")
        opts = aspects.load(ask["opts"])
        chosen = aspects.from_text(said, opts)
        req = _apply_aspects(req, ask, chosen, all_of_it=aspects.wants_all(said))
        await ask_avoid(uid, req, fresh=True)

    async def correct(uid: int, said: str, state: str) -> None:
        """A written correction of the current request: «без хоррора», «покороче», «а с кооперативом?»."""
        await app.panel(uid, "pick", texts.card("Учитываю поправку", lead=escape(said[:80])), None, fresh=True)
        try:
            delta = await intent.parse(s.analyst, said)
        except Exception:
            delta = intent.parse_heuristic(said)
        sess = app.session(uid)
        base = app.request(uid)
        if sess.get("step") == "avoid":
            base = _apply_avoid(base, sess.get("avoid", []))     # the ticks made before typing count
        relative = re.match(r"\s*(а|и|но|только|лучше|можно|без|по|чуть|ещё|еще|не)\b", said.lower())
        if state == "fix" and delta.seeds and not relative:
            await new_request(uid, said, delta)         # «что-нибудь как Hollow Knight» — a fresh search
            return
        req = intent.merge(base, delta)
        if delta.seeds:
            await wizard(uid, req)                      # a new reference game: ask about it first
        elif state == "fix":
            await run(uid, req, app.session(uid).get("seen", []))
        else:
            step = app.session(uid).get("step")
            if step == "avoid":
                await ask_time(uid, req)
            else:
                await run(uid, req)

    @r.callback_query(F.data == "wrong")
    async def on_wrong_game(c: CallbackQuery):
        """The reference was found as another game («резик» as Rez): ask for the exact title."""
        await c.answer()
        uid = c.from_user.id
        found = ((app.session(uid).get("ask") or {}).get("seed") or {}).get("name", "")
        db.update_user(uid, state="like")
        app.update_session(uid, step="home")
        await app.panel(uid, "pick", texts.card("Какую игру ты имел в виду?", lead=(
            f"Я нашёл «{escape(found)}» — видимо, не то." if found else ""), note=(
            "Напиши название точнее, лучше как в Steam: <i>«Resident Evil 4»</i>, <i>«Alan Wake 2»</i>. "
            "Можно с годом или номером части.")), back_kb())

    @r.callback_query(F.data == "noop")
    async def noop(c: CallbackQuery):
        await c.answer()

    @r.callback_query()
    async def stale(c: CallbackQuery):
        """A button from an older version of the bot: stop the spinner instead of leaving it hanging."""
        await c.answer("Кнопка устарела, жми /start")

    return r


def _pick_json(p) -> dict:
    """A Pick as plain data for the session (a spare pick waits there for «Уже играл»)."""
    from dataclasses import asdict
    return asdict(p)


def _pick_from(d: dict):
    from .recommender import Pick
    try:
        d = dict(d)
        d["taste_notes"] = [tuple(x) for x in d.get("taste_notes") or []]
        return Pick(**d)
    except (TypeError, ValueError):
        return None


def _apply_aspects(req: Request, ask: dict, chosen, all_of_it: bool = False) -> Request:
    out = aspects.apply(req, ask["seed"], ask.get("feel"), chosen, all_of_it=all_of_it)
    from dataclasses import asdict, is_dataclass
    out = Request.from_json(json.dumps(asdict(out) if is_dataclass(out) else dict(vars(out))))
    out.asked = True
    out.whole = bool(all_of_it)
    return out


def _apply_avoid(req: Request, picked: list[str]) -> Request:
    """The exclusions as ticked: ticked ones are added, unticked ones removed (also when they had been
    understood from the text and shown ticked). Axes from the text («проще») are never shown as ticks,
    so an unticked button leaves them alone."""
    for key, (_, eff) in AVOID_MAP.items():
        on = key in picked
        for f, v in eff.items():
            if f == "axes":
                if on:
                    req.axes.update(v)
            else:
                cur = getattr(req, f)
                setattr(req, f, cur + [x for x in v if x not in cur] if on else [x for x in cur if x not in v])
    req.tags_want = [t for t in req.tags_want if t not in req.tags_avoid]
    return req


async def _delete(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def _set_markup(msg, kb: InlineKeyboardMarkup | None) -> None:
    """Edits the buttons under a message; an old (inaccessible) message or an unchanged keyboard is skipped."""
    if not isinstance(msg, Message):
        return
    try:
        await msg.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest:
        pass


def _split(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > limit and cur:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out
