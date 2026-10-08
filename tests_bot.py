"""Offline test of the Telegram layer: python tests_bot.py.

The real aiogram dispatcher and router get updates through Dispatcher.feed_update; a fake
Telegram session records every outgoing call and checks it the way Telegram would (HTML,
length limits, callback data size, editing or deleting a message that exists, media edits). The
player's own messages are stored too, so the bot can delete them. The service is a fake over a
temporary SQLite db exposing exactly what bot.py calls. render.cover and intent.parse are
replaced (no Steam art, no LLM: parse_heuristic), the image cards are drawn for real, and any
outgoing connection fails the test: nothing here touches the network.

The UI under test: ONE panel message per player (an animation with a caption and buttons) that
is edited in place; cards are separate photos, after which the panel moves below them; typed
messages are deleted and the panel is re-posted at the bottom.
"""

import asyncio
import html
import inspect
import json
import logging
import os
import re
import socket
import sys
import tempfile
import traceback
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio.base_events  # noqa: E402

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError  # noqa: E402
from aiogram.types import (Animation, BufferedInputFile, CallbackQuery, Chat, FSInputFile,  # noqa: E402
                           InaccessibleMessage, InlineKeyboardMarkup, InputMediaAnimation, Message, MessageEntity,
                           PhotoSize, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, User)

from gamefinder import aspects, bot as botmod, intent, render  # noqa: E402
from gamefinder.analyst import AXES  # noqa: E402
from gamefinder.bot import AVOID, MEDIA, QUICK, TIME, App, _split, build_router  # noqa: E402
from gamefinder.config import Config  # noqa: E402
from gamefinder.db import Db  # noqa: E402
from gamefinder.intent import Request  # noqa: E402
from gamefinder.recommender import Catalog, Pick  # noqa: E402

OWNER, FRIEND, STRANGER, BUDDY = 100, 200, 300, 555
BOT_ID = 42
HK = 6
GAMES = [
    (1, "Hades", {"Roguelike": 900, "Action": 800, "Hack and Slash": 700, "Mythology": 500}),
    (2, "Celeste", {"Precision Platformer": 900, "Difficult": 800, "Pixel Graphics": 600}),
    (3, "Call of Duty", {"FPS": 1000, "Shooter": 900, "War": 700}),
    (4, "Ratchet & Clank <Rift Apart>", {"Action": 900, "3D Platformer": 800, "Sci-fi": 600}),
    (5, "Dead Cells", {"Roguelike": 1000, "Metroidvania": 900, "Action": 800}),
    (6, "Hollow Knight", {"Metroidvania": 1000, "Difficult": 900, "Exploration": 800, "Atmospheric": 700,
                          "Hand-drawn": 600}),
    (7, "Dead Space", {"Horror": 1000, "Sci-fi": 900, "Shooter": 800}),
    (8, "Stardew Valley", {"Farming Sim": 1000, "Relaxing": 900, "Pixel Graphics": 700}),
    (9, "Portal 2", {"Puzzle": 1000, "Co-op": 900, "First-Person": 700}),
    (10, "Disco Elysium", {"RPG": 1000, "Story Rich": 900, "Detective": 800}),
]
NAMES = {a: n for a, n, _ in GAMES}
POOL = [5, 4, 7, 2, 8, 9, 10, 1, 3, 6]          # the fake recommender's order
STATS = {"total": 12000, "all_share": 0.9, "recent_n": 100, "recent_share": 0.85, "recent_span_days": 30,
         "engaged_n": 40, "engaged_share": 0.92, "quick_negative_share": 0.3}
TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "tg-spoiler", "span",
        "tg-emoji"}   # Telegram's HTML; <blockquote> is left out on purpose (house style)
STALE = "Кнопка устарела, жми /start"
OUTDATED = "Вопрос устарел"

HOME = ["q:like", "q:evening", "q:long", "q:coop", "q:chill", "q:challenge", "q:story", "q:gems", "q:surprise",
        "game", "help", "region"]
RESULTS = [f"rf:{k}" for k in intent.REFINES] + ["more", "home"]
LOOSEN = ["rf:noavoid", "rf:anylen", "rf:different", "home"]          # under «Ничего не нашлось»
AVOID_ORDER = ["horror", "shooter", "hard", "grind", "mtx", "text", "pvp", "noru", "early"]   # as on screen
AVOID_KEYS = [f"av:{k}" for k in AVOID_ORDER]
TIME_KEYS = [f"tm:{k}" for k, *_ in TIME]
HK_ASPECTS = ["asp:explore", "asp:combat", "asp:atmos", "asp:challenge", "asp:visual"]


def now():
    return datetime.now(timezone.utc)


def passport(name: str) -> dict:
    p = {
        "summary": f"Быстрый & злой <рогалик> про {name}", "real_genres": ["рогалик", "экшен"],
        "core_loop": "Бежишь & рубишь", "feel": {**{k: 5 for k in AXES}, "pace": 8}, "moods": [],
        "praise": [{"point": "боёвка <огонь>", "share": "most"}],
        "complaints": [{"point": "гринд & повторы", "share": "some", "kind": "taste", "axis": "grind",
                        "direction": "high"}],
        "quality": {}, "state_now": "Лучше, чем на релизе", "best_for": "Любишь темп", "avoid_if": "Не любишь <спешку>",
        "compared_to": ["Dead Cells"], "hours_typical": "20-30 ч",
    }
    if name == "Hollow Knight":
        p["feel"] = {**{k: 5 for k in AXES}, "exploration": 9, "difficulty": 8, "combat": 7}
        p["praise"] = [{"point": "исследование мира", "share": "most"}, {"point": "атмосфера", "share": "many"},
                       {"point": "боёвка <огонь>", "share": "many"}]
        p["complaints"] = [{"point": "сложность & боссы", "share": "some", "kind": "taste", "axis": "difficulty",
                            "direction": "high"}]
        p["real_genres"] = ["метроидвания"]
    return p


def html_problems(text: str) -> list[str]:
    out, stack = [], []
    for m in re.finditer(r"<[^<>]*>|[<&]", text):
        tok = m.group(0)
        if tok == "&":
            if not re.match(r"&(lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);", text[m.start():]):
                out.append(f"bare & at {m.start()}")
        elif tok == "<":
            out.append(f"bare < at {m.start()}")
        else:
            t = re.fullmatch(r"<(/?)([a-z][a-z-]*)(?:\s[^>]*)?>", tok)
            if not t or t.group(2) not in TAGS:
                out.append(f"unsupported tag {tok}")
            elif t.group(1):
                if not stack or stack.pop() != t.group(2):
                    out.append(f"unbalanced {tok}")
            else:
                stack.append(t.group(2))
    if stack:
        out.append(f"unclosed {stack}")
    return out


def visible_len(text: str) -> int:
    """Length as Telegram counts it: after the markup, in UTF-16 code units."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)).encode("utf-16-le")) // 2


def datas_of(kb) -> list:
    return [b.callback_data for row in (kb.inline_keyboard if isinstance(kb, InlineKeyboardMarkup) else [])
            for b in row]


def labels_of(kb) -> list:
    return [b.text for row in (kb.inline_keyboard if isinstance(kb, InlineKeyboardMarkup) else []) for b in row]


class FakeTelegram(BaseSession):
    """Answers the Bot API like Telegram would, keeps the chats' messages and records what is wrong."""

    def __init__(self):
        super().__init__()
        self.card_ids: set[tuple[int, int]] = set()   # cards edited into animations
        self.calls = []
        self.messages: dict[tuple[int, int], Message] = {}
        self.deleted: set[tuple[int, int]] = set()
        self.problems: list[str] = []
        self.next_id = 1000
        self.anim_ids: set[str] = set()              # animation file_ids Telegram has handed out
        self.fail_next: dict[str, Exception] = {}    # method name -> error raised once

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError
        yield b""

    def bad(self, method, text: str, record: bool = True):
        if record:
            self.problems.append(f"{type(method).__name__}: {text}")
        raise TelegramBadRequest(method=method, message=f"Bad Request: {text}")

    def check_text(self, method, text, limit: int, what: str = "message text"):
        if not text:
            self.bad(method, f"{what} is empty")
        problems = html_problems(text)
        if problems:
            self.bad(method, f"can't parse entities: {problems} in {text!r}")
        if visible_len(text) > limit:
            self.bad(method, f"{what} is too long: {visible_len(text)} > {limit}")

    def check_markup(self, method, kb):
        if isinstance(kb, ReplyKeyboardMarkup):
            self.problems.append(f"{type(method).__name__}: a reply keyboard (the bot has none any more)")
        if not isinstance(kb, InlineKeyboardMarkup):
            return
        for row in kb.inline_keyboard:
            if not row:
                self.bad(method, "an empty keyboard row")
            for b in row:
                if not b.text:
                    self.bad(method, "button text is empty")
                if (b.callback_data is None) == (b.url is None):
                    self.bad(method, f"button {b.text!r} needs exactly one action")
                if b.callback_data is not None and not 1 <= len(b.callback_data.encode()) <= 64:
                    self.bad(method, f"BUTTON_DATA_INVALID: {b.callback_data!r}")

    def check_animation(self, method, media):
        if isinstance(media, str):
            if media not in self.anim_ids:
                self.bad(method, f"wrong file identifier {media!r}")
        elif isinstance(media, BufferedInputFile):
            # A rendered card loop: must be a real MP4 (ftyp box first)
            if not str(media.filename).endswith(".mp4") or media.data[4:8] != b"ftyp":
                self.problems.append(f"{type(method).__name__}: not an MP4 card loop {media.filename!r}")
        elif not isinstance(media, FSInputFile) or not str(media.path).endswith(".mp4"):
            self.problems.append(f"{type(method).__name__}: unexpected animation {media!r}")

    def animation(self, media) -> Animation:
        file_id = media if isinstance(media, str) else f"anim{len(self.anim_ids) + 1}"
        self.anim_ids.add(file_id)
        return Animation(file_id=file_id, file_unique_id="u" + file_id, width=1, height=1, duration=1)

    def target(self, method) -> Message:
        key = (method.chat_id, method.message_id)
        if key not in self.messages or key in self.deleted:
            self.bad(method, "message to edit not found", record=False)
        return self.messages[key]

    def keep(self, bot, msg: Message) -> Message:
        self.messages[(msg.chat.id, msg.message_id)] = msg
        return msg.as_(bot)

    def new(self, bot, method, **fields) -> Message:
        self.next_id += 1
        kb = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
        return self.keep(bot, Message(message_id=self.next_id, date=now(), chat=Chat(id=method.chat_id, type="private"),
                                      from_user=User(id=BOT_ID, is_bot=True, first_name="Finder"),
                                      reply_markup=kb, **fields))

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = type(method).__name__
        if name in self.fail_next:
            raise self.fail_next.pop(name)
        if name == "GetMe":
            return User(id=BOT_ID, is_bot=True, first_name="Finder", username="testfinderbot")
        if name == "SendMessage":
            self.check_text(method, method.text, 4096)
            self.check_markup(method, method.reply_markup)
            return self.new(bot, method, text=method.text)
        if name == "SendAnimation":
            self.check_text(method, method.caption, 1024, "caption")
            self.check_markup(method, method.reply_markup)
            self.check_animation(method, method.animation)
            return self.new(bot, method, caption=method.caption, animation=self.animation(method.animation))
        if name == "SendPhoto":
            self.check_text(method, method.caption, 1024, "caption")
            self.check_markup(method, method.reply_markup)
            photo = method.photo
            if not isinstance(photo, BufferedInputFile) or not photo.data.startswith(b"\xff\xd8"):
                self.bad(method, f"photo is not a JPEG BufferedInputFile: {type(photo).__name__}")
            return self.new(bot, method, caption=method.caption, photo=[PhotoSize(
                file_id=f"photo{self.next_id + 1}", file_unique_id="p", width=1080, height=1350)])
        if name == "EditMessageText":
            msg = self.target(method)
            if msg.text is None:
                self.bad(method, "there is no text in the message to edit")
            self.check_text(method, method.text, 4096)
            self.check_markup(method, method.reply_markup)
            if msg.text == method.text and msg.reply_markup == method.reply_markup:
                self.bad(method, "message is not modified", record=False)
            return self.keep(bot, msg.model_copy(update={"text": method.text, "reply_markup": method.reply_markup}))
        if name == "EditMessageCaption":
            msg = self.target(method)
            if msg.text is not None:
                self.bad(method, "there is no caption in the message to edit")
            if msg.animation is None:
                self.problems.append(f"EditMessageCaption: message {method.message_id} is not the panel animation")
            self.check_text(method, method.caption, 1024, "caption")
            self.check_markup(method, method.reply_markup)
            if msg.caption == method.caption and msg.reply_markup == method.reply_markup:
                self.bad(method, "message is not modified", record=False)
            return self.keep(bot, msg.model_copy(update={"caption": method.caption,
                                                         "reply_markup": method.reply_markup}))
        if name == "EditMessageMedia":
            msg = self.target(method)
            media = method.media
            if not isinstance(media, InputMediaAnimation):
                self.bad(method, f"expected InputMediaAnimation, got {type(media).__name__}")
            if msg.photo:
                # A card picture becoming its animated loop: allowed, and it stays a card.
                self.card_ids.add((method.chat_id, method.message_id))
            elif msg.animation is None:
                self.bad(method, "there is no media in the message to edit", record=False)
            self.check_text(method, media.caption, 1024, "caption")
            self.check_markup(method, method.reply_markup)
            self.check_animation(method, media.media)
            return self.keep(bot, msg.model_copy(update={"caption": media.caption, "reply_markup": method.reply_markup,
                                                         "animation": self.animation(media.media), "photo": None}))
        if name == "EditMessageReplyMarkup":
            msg = self.target(method)
            self.check_markup(method, method.reply_markup)
            kb = method.reply_markup if method.reply_markup and method.reply_markup.inline_keyboard else None
            if msg.reply_markup == kb:
                self.bad(method, "message is not modified", record=False)
            return self.keep(bot, msg.model_copy(update={"reply_markup": kb}))
        if name == "DeleteMessage":
            key = (method.chat_id, method.message_id)
            if key not in self.messages or key in self.deleted:
                self.bad(method, "message to delete not found", record=False)
            self.deleted.add(key)
            return True
        if name == "AnswerCallbackQuery":
            if method.text and len(method.text) > 200:
                self.bad(method, "answer text is too long")
            return True
        if name == "SetMyCommands":
            for c in method.commands:
                if not re.fullmatch(r"[a-z0-9_]{1,32}", c.command) or not 1 <= len(c.description) <= 256:
                    self.bad(method, f"bad command {c.command!r}")
            return True
        self.problems.append(f"unexpected Bot API call {name}")
        return True

    def sent(self, name: str | None = None) -> list:
        return [c for c in self.calls if name is None or type(c).__name__ == name]


class FakeService:
    """Exactly what bot.py uses of gamefinder.service.Service, over the test db and without the network."""

    def __init__(self, cfg: Config, db: Db):
        self.cfg, self.db = cfg, db
        self.http = object()          # handed to render.cover only (patched)
        self.busy = 0
        self.catalog = Catalog()
        self.analyst = type("Analyst", (), {"describe": lambda self: "Gemini → Groq", "providers": []})()
        self.fail: set[str] = set()   # "recommend", "empty", "analyze", "resolve", "aspects"
        self.hold: asyncio.Event | None = None
        self.resolved: list[tuple[str, bool]] = []
        self.analyzed: list[int] = []
        self.asked: list[str] = []
        self.requests: list[tuple[int, Request, set[int]]] = []
        self.price_calls: list[tuple[tuple[int, ...], str]] = []

    def refresh_catalog(self) -> None:
        self.catalog.build(self.db.catalog(200))

    async def prices(self, appids: list[int], cc: str) -> dict[int, dict]:
        self.price_calls.append((tuple(appids), cc))
        return {a: {"state": "paid", "final_formatted": "450 руб.", "discount": 75} for a in appids}

    def passport_fresh(self, appid: int) -> bool:
        return self.db.passport(appid) is not None

    async def resolve(self, text: str, limit: int = 5, strict: bool = False) -> list[dict]:
        self.resolved.append((text, strict))
        if "resolve" in self.fail:
            raise OSError("network is down")
        q = text.lower().strip()
        rows = self.db.find_by_name(q, limit) if q else []
        exact = [g for g in rows if g["name_lc"] == q][:1]
        return exact if strict else exact or rows

    async def analyze(self, appid: int, force: bool = False) -> dict | None:
        self.analyzed.append(appid)
        if "analyze" in self.fail:
            raise OSError("network is down")
        g = self.db.game(appid)
        if not g:
            return None
        self.db.set_review_stats(appid, STATS)
        self.db.set_passport(appid, passport(g["name"]), "llm", "fake", 50)
        return self.db.passport(appid)

    async def aspect_options(self, title: str) -> tuple[dict | None, dict | None, list]:
        self.asked.append(title)
        if "aspects" in self.fail:
            raise OSError("network is down")
        found = await self.resolve(title, limit=5, strict=True)
        if not found:
            return None, None, []
        g = found[0]
        p = self.db.passport(g["appid"]) or await self.analyze(g["appid"])
        return g, p, aspects.options(g, p, (p or {}).get("aspects"))

    async def recommend_now(self, user_id: int, req, seen: set[int] = frozenset(), limit: int = 3,
                            progress=None, stage=None) -> tuple[list[Pick], list[dict]]:
        self.requests.append((user_id, Request.from_json(req.to_json()), set(seen)))
        if self.hold is not None:
            await self.hold.wait()
        if "recommend" in self.fail:
            raise OSError("network is down")
        self.refresh_catalog()
        seeds = []
        for title in req.seeds[:4]:
            found = await self.resolve(title, limit=5, strict=True)
            if found:
                seeds.append(found[0])
        seed_ids = {g["appid"] for g in seeds}
        ids = [] if "empty" in self.fail else [a for a in POOL if a not in seen and a not in seed_ids][:limit]
        todo = [a for a in ids if not self.passport_fresh(a)]
        for i, appid in enumerate(todo):
            if progress:
                await progress(i, len(todo))
            await self.analyze(appid)
        return [Pick(appid=a, score=0.8, parts={}, passport=self.db.passport(a), passport_real=True,
                     because=seeds[0]["appid"] if seeds else None, feel_matches=["pace"],
                     taste_notes=[("слишком <быстро>", True)], warnings=["баги & вылеты"],
                     evidence=[{"text": "Залип & не жалею <3", "hours": 42.5}]) for a in ids], seeds


class Harness:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Db(os.path.join(self.dir.name, "t.db"))
        for appid, name, tags in GAMES:
            self.db.upsert_game(appid, name=name, tags=tags, genres=["Action"], positive=10000 - appid * 100,
                                negative=1000, price_cents=1999, currency="USD", ru_text=1, store_ok=1, spy_ok=1)
        self.cfg = Config(bot_token="42:TEST", owner_ids=frozenset({OWNER}), allowed_ids=frozenset({FRIEND}),
                          animate_cards=False)
        for uid in (OWNER, FRIEND):         # both told the bot their Steam region already
            self.db.user(uid)
            self.db.update_user(uid, region="ru")
        self.s = FakeService(self.cfg, self.db)
        self.tg = FakeTelegram()
        self.bot = Bot("42:TEST", session=self.tg, default=DefaultBotProperties(parse_mode="HTML"))
        self.app = App(self.s, self.bot)
        self.dp = Dispatcher()
        self.dp.include_router(build_router(self.app))
        self.n = 0
        self.typed: dict[int, list[int]] = {}   # chat -> ids of the player's own messages
        self.covers: list[int] = []
        self.parsed: list[str] = []
        self.errors: list[str] = []     # ERROR records the bot logged (log.exception)

    def close(self):
        self.db.close()
        self.dir.cleanup()

    async def send(self, uid: int, text: str, first_name: str = "", username: str | None = None) -> int:
        """The player types a message; it is kept in the chat (the bot may delete it). Returns its id."""
        self.n += 1
        self.tg.next_id += 1
        mid = self.tg.next_id
        entities = ([MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
                    if text.startswith("/") else None)
        msg = Message(message_id=mid, date=now(), chat=Chat(id=uid, type="private"), text=text,
                      entities=entities, from_user=User(id=uid, is_bot=False, first_name=first_name or f"u{uid}",
                                                        username=username))
        self.tg.messages[(uid, mid)] = msg
        self.typed.setdefault(uid, []).append(mid)
        await self.dp.feed_update(self.bot, Update(update_id=self.n, message=msg))
        return mid

    async def press(self, uid: int, data: str, message=None, stale: bool = False):
        """Taps the newest button with this data in the user's chat (or under the given message)."""
        if message is None:
            message = self.with_button(uid, data)
        self.n += 1
        await self.dp.feed_update(self.bot, Update(update_id=self.n, callback_query=CallbackQuery(
            id=f"cb{self.n}", from_user=User(id=uid, is_bot=False, first_name=f"u{uid}"),
            chat_instance="ci", data=data, message=message)))
        if not stale:
            assert self.answers()[-1] != STALE, f"no handler for {data!r}"

    def live(self, chat_id: int) -> list[Message]:
        return sorted((m for k, m in self.tg.messages.items() if k[0] == chat_id and k not in self.tg.deleted),
                      key=lambda m: m.message_id)

    def with_button(self, chat_id: int, data: str) -> Message:
        for m in reversed(self.live(chat_id)):
            if data in datas_of(m.reply_markup):
                return m
        raise AssertionError(f"no button {data!r} in chat {chat_id}: {self.buttons(chat_id)}")

    def buttons(self, chat_id: int, labels: bool = False, msg: Message | None = None) -> list:
        """Buttons under the newest message in the chat, or under msg (callback data, or labels)."""
        if msg is None:
            msgs = self.live(chat_id)
            msg = msgs[-1] if msgs else None
        else:
            msg = self.tg.messages[(msg.chat.id, msg.message_id)]
        kb = msg.reply_markup if msg else None
        return labels_of(kb) if labels else datas_of(kb)

    # --- the panel
    def panel_id(self, uid: int) -> int | None:
        return self.session(uid).get("panel")

    def panel(self, uid: int) -> Message:
        pid = self.panel_id(uid)
        assert pid, "no panel"
        assert (uid, pid) in self.tg.messages and (uid, pid) not in self.tg.deleted, f"panel {pid} is gone"
        return self.tg.messages[(uid, pid)]

    def caption(self, uid: int) -> str:
        p = self.panel(uid)
        return p.caption or p.text or ""

    def pbuttons(self, uid: int, labels: bool = False) -> list:
        kb = self.panel(uid).reply_markup
        return labels_of(kb) if labels else datas_of(kb)

    def plabel(self, uid: int, data: str) -> str:
        """The label of the panel's button with this callback data."""
        return dict(zip(self.pbuttons(uid), self.pbuttons(uid, labels=True)))[data]

    def check_panel(self, uid: int) -> Message:
        """The panel is an animation, the newest message in the chat and the only panel-like message there."""
        p = self.panel(uid)
        assert p.animation is not None, "the panel is not an animation"
        live = self.live(uid)
        assert live[-1].message_id == p.message_id, (
            f"the panel {p.message_id} is not at the bottom: {[(m.message_id, (m.caption or m.text or '')[:30]) for m in live]}")
        others = [m.message_id for m in live if m.animation and m.message_id != p.message_id]
        assert not others, f"old panels left in the chat: {others}"
        return p

    # --- what was said
    def mark(self) -> int:
        return len(self.tg.calls)

    def since(self, mark: int, name: str | None = None, chat_id: int | None = None) -> list:
        return [c for c in self.tg.calls[mark:] if (name is None or type(c).__name__ == name)
                and (chat_id is None or getattr(c, "chat_id", None) == chat_id)]

    def screens(self, uid: int, mark: int = 0) -> list[str]:
        """Every text the player saw since mark: new messages and panel edits, in order."""
        out = []
        for c in self.since(mark, chat_id=uid):
            name = type(c).__name__
            if name == "SendMessage":
                out.append(c.text)
            elif name in ("SendAnimation", "SendPhoto", "EditMessageCaption"):
                out.append(c.caption)
            elif name == "EditMessageMedia":
                out.append(c.media.caption)
        return out

    def saw(self, uid: int, fragment: str, mark: int = 0) -> bool:
        return any(fragment in (t or "") for t in self.screens(uid, mark))

    def out(self, chat_id: int) -> list[str]:
        """Texts and captions of the messages the bot sent to the chat, in order."""
        return [c.text if type(c).__name__ == "SendMessage" else c.caption
                for c in self.tg.sent() if type(c).__name__ in ("SendMessage", "SendAnimation", "SendPhoto")
                and c.chat_id == chat_id]

    def last(self, chat_id: int) -> str:
        return self.out(chat_id)[-1]

    def photos(self, chat_id: int, mark: int = 0) -> list:
        return self.since(mark, "SendPhoto", chat_id)

    def answers(self) -> list[str | None]:
        return [c.text for c in self.tg.sent("AnswerCallbackQuery")]

    def is_deleted(self, uid: int, mid: int) -> bool:
        return (uid, mid) in self.tg.deleted

    def state(self, uid: int) -> str:
        return self.db.user(uid)["state"]

    def session(self, uid: int) -> dict:
        return json.loads(self.db.user(uid)["session"] or "{}")

    def req(self, uid: int) -> Request:
        return Request.from_json(self.session(uid)["req"])

    def last_request(self) -> tuple[int, Request, set[int]]:
        assert self.s.requests, "recommend_now was not called"
        return self.s.requests[-1]


class _Errors(logging.Handler):
    def __init__(self, h: Harness):
        super().__init__(logging.ERROR)
        self.h = h

    def emit(self, record):
        self.h.errors.append(record.getMessage())


def _no_network(*args, **kwargs):
    raise AssertionError(f"a test tried to open a network connection: {args[1:]!r}")


def offline(h: Harness):
    """Patches for one scenario; returns the undo function."""
    real_connect = socket.socket.connect

    def connect(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):   # asyncio's own self-pipe on Windows is local
            _no_network(sock, address)
        return real_connect(sock, address)

    async def cover(http, appid, cache_dir="data/covers"):
        assert http is h.s.http
        h.covers.append(appid)
        return None

    async def parse(analyst, text, *, usage=None):
        assert analyst is h.s.analyst
        h.parsed.append(text)
        return intent.parse_heuristic(text)

    async def create_connection(self, *args, **kwargs):
        _no_network(self, *args)

    patches = [(render, "cover", cover), (intent, "parse", parse), (socket.socket, "connect", connect),
               (asyncio.base_events.BaseEventLoop, "create_connection", create_connection)]
    saved = [(obj, name, getattr(obj, name)) for obj, name, _ in patches]
    for obj, name, new in patches:
        setattr(obj, name, new)
    handler = _Errors(h)
    lg = logging.getLogger("gamefinder")
    old_level, old_prop = lg.level, lg.propagate
    lg.setLevel(logging.ERROR)
    lg.propagate = False
    lg.addHandler(handler)

    def undo():
        for obj, name, old in saved:
            setattr(obj, name, old)
        lg.removeHandler(handler)
        lg.setLevel(old_level)
        lg.propagate = old_prop
    return undo


def scenario(fn):
    async def run():
        h = Harness()
        undo = offline(h)
        try:
            await fn(h)
            assert not h.tg.problems, "\n".join(h.tg.problems)
            assert not h.errors, f"the bot logged errors: {h.errors}"
            assert h.s.busy == 0 and not h.app.running, (h.s.busy, h.app.running)
            # whoever has a panel has exactly one live panel message
            for uid in {k[0] for k in h.tg.messages}:
                panels = [m.message_id for m in h.live(uid)
                          if m.animation and (uid, m.message_id) not in h.tg.card_ids]
                assert len(panels) <= 1, f"chat {uid} has several panels: {panels}"
                if h.db.user(uid).get("session") and h.panel_id(uid) and panels:
                    assert panels == [h.panel_id(uid)], (uid, panels, h.panel_id(uid))
        finally:
            undo()
            h.close()
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


def old_message(uid: int) -> Message:
    return Message(message_id=1, date=now(), chat=Chat(id=uid, type="private"), text="old")


async def until(cond, steps: int = 500):
    for _ in range(steps):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


async def results_for(h: Harness, uid: int, quick: str = "evening"):
    """/start and a quick request: three cards and the results panel under them."""
    await h.send(uid, "/start")
    await h.press(uid, f"q:{quick}")
    assert "Подкрутить?" in h.caption(uid), h.caption(uid)


# --- static checks

def test_fake_service_matches_real():
    """Everything bot.py takes from the service exists on the real Service and on the fake,
    and the fake's methods take the same arguments as the real ones."""
    from gamefinder.service import Service
    src = inspect.getsource(botmod)
    used = set(re.findall(r"\bs\.(\w+)", src)) | set(re.findall(r"\bapp\.s\.(\w+)", src))
    assert {"recommend_now", "aspect_options", "analyze", "resolve", "passport_fresh", "busy", "http"} <= used, used
    init = inspect.getsource(Service.__init__)
    fake = FakeService(Config(bot_token="1:x", owner_ids=frozenset()), None)
    for name in sorted(used):
        assert hasattr(Service, name) or f"self.{name} =" in init, f"bot.py uses s.{name}, Service has none"
        assert hasattr(fake, name), f"FakeService lacks {name}"
        real = getattr(Service, name, None)
        if callable(real):
            rp = inspect.signature(real).parameters
            fp = inspect.signature(getattr(FakeService, name)).parameters
            assert list(rp) == list(fp), f"Service.{name}{tuple(rp)} vs fake {tuple(fp)}"
            for k in rp:
                assert rp[k].default == fp[k].default, f"Service.{name}: default of {k}"
    # and nothing more: the fake has no public method bot.py does not call
    extra = {n for n, v in vars(FakeService).items() if callable(v) and not n.startswith("_")} - used
    assert not extra, f"FakeService has methods bot.py does not use: {extra}"
    assert callable(fake.analyst.describe)


def test_split_keeps_paragraphs():
    text = "\n\n".join(f"<b>Блок {i}</b>\n" + "х" * 900 for i in range(10))
    chunks = _split(text)
    assert len(chunks) > 1 and all(len(c) <= 4000 for c in chunks)
    assert "\n\n".join(chunks) == text and all(not html_problems(c) for c in chunks)
    assert _split("short") == ["short"]


def test_keyboards_fit_telegram():
    """Every keyboard's callback data fits 64 bytes, rows are not empty, and the quick requests and the
    exclusions are well-formed."""
    opts = aspects.load([{"key": "explore", "label": "Исследование мира"}, {"key": "atmos", "label": "Атмосфера"},
                         {"key": "x" * 12, "label": "Очень длинный пункт про всё"}])
    kbs = [botmod.home_kb(), botmod.back_kb(), botmod.avoid_kb([]), botmod.avoid_kb([k for k, _, _ in AVOID]),
           botmod.time_kb(), botmod.results_kb(), botmod.pick_kb(2 ** 31, "skip"), botmod.game_kb(2 ** 31),
           botmod.aspect_kb(opts, []), botmod.aspect_kb(opts, ["explore"])]
    for kb in kbs:
        assert all(kb.inline_keyboard), kb
        assert all(d is None or 1 <= len(d.encode()) <= 64 for d in datas_of(kb))
    assert datas_of(botmod.home_kb()) == HOME
    assert datas_of(botmod.results_kb()) == RESULTS
    assert datas_of(botmod.time_kb()) == TIME_KEYS + ["home"]
    assert datas_of(botmod.avoid_kb([])) == AVOID_KEYS + ["av:done", "go", "home"]
    assert sorted(AVOID_ORDER) == sorted(k for k, _, _ in AVOID)            # every exclusion has its button
    rows = [[b.callback_data for b in row] for row in botmod.avoid_kb([]).inline_keyboard]
    assert rows == [["av:horror", "av:shooter", "av:hard"], ["av:grind", "av:mtx", "av:text"],
                    ["av:pvp", "av:noru"], ["av:early"], ["av:done"], ["go", "home"]], rows
    names = dict(zip(datas_of(botmod.avoid_kb([])), labels_of(botmod.avoid_kb([]))))
    assert names["av:text"] == "Чтение" and names["av:pvp"] == "Онлайн и PvP"
    assert labels_of(botmod.avoid_kb([]))[-3] == "Ничего, дальше ▸"
    assert labels_of(botmod.avoid_kb(["horror"]))[0] == "✓ Хоррор"
    assert labels_of(botmod.avoid_kb(["horror"]))[-3] == "Дальше ▸"
    assert datas_of(botmod.aspect_kb(opts, [])) == ["asp:explore", "asp:atmos", f"asp:{'x' * 12}", "asp:all",
                                                    "go", "home"]
    picked = botmod.aspect_kb(opts, ["explore"])
    assert datas_of(picked)[-3:] == ["asp:done", "go", "home"] and "✓ Исследование мира" in labels_of(picked)
    assert labels_of(picked)[-3] == "Дальше ▸" and labels_of(botmod.aspect_kb(opts, []))[-3] == "Всё сразу ▸"
    assert labels_of(botmod.time_kb()) == ["🌙 Вечер · до 4 ч", "Пара вечеров · до 10 ч", "Неделя · до 25 ч",
                                           "♾ Надолго · 30+ ч", "Неважно", "◂ В начало"]
    assert labels_of(botmod.results_kb()) == ["Покороче", "Попроще", "Посложнее", "Сюжетнее", "Спокойнее",
                                              "Совсем другое", "🔄 Ещё 3", "◂ В начало"]
    assert datas_of(botmod.loosen_kb()) == LOOSEN
    assert labels_of(botmod.loosen_kb()) == ["Снять исключения", "Любая длина", "Совсем другое", "◂ В начало"]
    region = botmod.region_kb().inline_keyboard
    assert [b.callback_data for b in region[-1]] == ["rg:other", "home"]
    assert [b.text for b in region[-1]] == ["Другая страна", "◂ В начало"]
    assert all(b.callback_data.startswith("rg:") for row in region[:-1] for b in row)
    for kb in (botmod.loosen_kb(), botmod.region_kb()):
        assert all(kb.inline_keyboard) and all(1 <= len(d.encode()) <= 64 for d in datas_of(kb))
    for key, _, req in QUICK:
        assert req is None or Request.from_json(req.to_json()) == req, key
    fields = Request.__dataclass_fields__
    for key, _, eff in AVOID:
        for f, v in eff.items():
            assert f in fields and isinstance(v, type(getattr(Request(), f))), (key, f)
    assert len({k for k, *_ in TIME}) == len(TIME) and len({k for k, *_ in AVOID}) == len(AVOID)


def test_config_empty_values_are_defaults():
    """An empty KEY= in .env means the default; the test harness keeps animate_cards off."""
    from gamefinder.config import load_config
    keys = {"BOT_TOKEN": "1:x", "LLM_DAILY_GAMES": "", "ANIMATE_CARDS": "", "STORE_CC": " ", "DB_PATH": "",
            "PASSPORT_MAX_AGE_DAYS": "", "OWNER_IDS": ""}
    saved = {k: os.environ.get(k) for k in keys}
    nowhere = os.path.join(tempfile.gettempdir(), "no-such-gamefinder.env")
    try:
        os.environ.update(keys)
        cfg = load_config(nowhere)
        assert cfg.llm_daily_games == 400 and cfg.animate_cards is True and cfg.store_cc == "kz"
        assert cfg.db_path == "data/gamefinder.db" and cfg.passport_max_age_days == 30 and cfg.owner_ids == frozenset()
        os.environ["ANIMATE_CARDS"] = "0"
        assert load_config(nowhere).animate_cards is False
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    h = Harness()
    try:
        assert h.cfg.animate_cards is False
    finally:
        h.close()


def test_panel_media_exist():
    """Every panel screen has its animation; without one the panel would fall back to a text message."""
    src = inspect.getsource(botmod)
    screens = set(re.findall(r"panel\(\w+(?:\.from_user\.id)?, \"(\w+)\"", src))
    assert screens == {"start", "help", "game", "pick"}, screens
    for screen in screens:
        assert (MEDIA / f"{screen}.mp4").is_file(), screen


# --- scenarios: the panel

@scenario
async def test_start_posts_the_home_panel(h):
    mid = await h.send(FRIEND, "/start", first_name="Ann <b>")
    assert h.is_deleted(FRIEND, mid)                                  # the command does not stay in the chat
    removal = [c for c in h.tg.sent("SendMessage") if isinstance(c.reply_markup, ReplyKeyboardRemove)]
    assert len(removal) == 1 and all(m.text != removal[0].text for m in h.live(FRIEND))   # sent and deleted
    p = h.check_panel(FRIEND)
    welcome = h.tg.sent("SendAnimation")[-1]
    assert isinstance(welcome.animation, FSInputFile) and welcome.animation.path.name == "start.mp4"
    assert "Во что поиграть сейчас?" in p.caption and "Привет, Ann &lt;b&gt;." in p.caption
    assert h.pbuttons(FRIEND) == HOME and h.state(FRIEND) == ""
    assert h.session(FRIEND)["panel_screen"] == "start" and h.session(FRIEND)["step"] == "home"
    assert h.tg.sent("SetMyCommands") == []                            # not an owner
    assert [m.message_id for m in h.live(FRIEND)] == [p.message_id]    # nothing else is left

    # /start again: the old panel goes, a new one comes, with the cached file_id
    old = p.message_id
    await h.send(FRIEND, "/start")
    again = h.tg.sent("SendAnimation")[-1]
    assert isinstance(again.animation, str) and again.animation in h.tg.anim_ids
    assert h.is_deleted(FRIEND, old) and h.check_panel(FRIEND).message_id != old
    assert len(h.tg.sent("SendMessage")) == 1                          # the reply keyboard goes once per player
    assert h.session(FRIEND)["kb_removed"] is True

    await h.send(OWNER, "/start")
    cmds = h.tg.sent("SetMyCommands")
    assert len(cmds) == 1 and cmds[0].scope.chat_id == OWNER
    assert {c.command for c in cmds[0].commands} >= {"stats", "allow", "find", "game", "help", "start"}
    h.check_panel(OWNER)
    removal = [c for c in h.tg.sent("SendMessage") if isinstance(c.reply_markup, ReplyKeyboardRemove)]
    assert [c.chat_id for c in removal] == [FRIEND, OWNER]              # another player gets their own, once
    assert not [m for m in h.live(OWNER) if m.text]                      # and it is deleted right away

    # the commands are panels too, posted fresh at the bottom
    for cmd, fragment, buttons, state in (("/help", "Как я подбираю", ["home"], ""),
                                          ("/game", "Разбор игры", ["home"], "game"),
                                          ("/find", "Во что поиграть сейчас?", HOME, "")):
        old = h.panel_id(FRIEND)
        mid = await h.send(FRIEND, cmd)
        assert h.is_deleted(FRIEND, mid) and h.is_deleted(FRIEND, old)
        assert fragment in h.check_panel(FRIEND).caption and h.pbuttons(FRIEND) == buttons, cmd
        assert h.state(FRIEND) == state, cmd


@scenario
async def test_panel_is_one_message_edited_in_place(h):
    """home -> help -> back -> help -> back -> a quick request: one message all along, until the cards."""
    await h.send(FRIEND, "/start")
    pid = h.panel_id(FRIEND)
    m = h.mark()
    await h.press(FRIEND, "help")
    edits = h.since(m, "EditMessageMedia")
    assert len(edits) == 1 and edits[0].message_id == pid and isinstance(edits[0].media.media, FSInputFile)
    assert edits[0].media.media.path.name == "help.mp4"
    assert "Как я подбираю" in h.caption(FRIEND) and h.pbuttons(FRIEND) == ["home"]
    assert h.session(FRIEND)["panel_screen"] == "help"

    await h.press(FRIEND, "home")
    back = h.since(m, "EditMessageMedia")[-1]
    assert back.message_id == pid and isinstance(back.media.media, str)    # start.mp4 is a file_id by now
    assert "Во что поиграть сейчас?" in h.caption(FRIEND) and h.pbuttons(FRIEND) == HOME
    await h.press(FRIEND, "help")
    assert isinstance(h.since(m, "EditMessageMedia")[-1].media.media, str)  # help.mp4 cached from the edit
    await h.press(FRIEND, "home")
    assert h.since(m, "SendAnimation") == [] and h.since(m, "DeleteMessage") == []
    assert h.since(m, "SendMessage") == []

    m = h.mark()
    await h.press(FRIEND, "q:evening")
    uid, req, seen = h.last_request()
    assert uid == FRIEND and req.mood == "evening" and req.max_hours == 4 and seen == set()
    # «reading reviews» is the same panel: the media changes once, then the caption counts candidates
    before = h.tg.calls[m:h.tg.calls.index(h.photos(FRIEND, m)[0])]
    before = [c for c in before if type(c).__name__ != "AnswerCallbackQuery"]
    names = [type(c).__name__ for c in before]
    assert names == ["EditMessageMedia", "EditMessageCaption", "EditMessageCaption", "EditMessageCaption"], names
    assert all(c.message_id == pid for c in before if hasattr(c, "message_id"))
    progress = [c.caption for c in before if type(c).__name__ == "EditMessageCaption"]
    assert ["Кандидат 1 из 3" in progress[0], "Кандидат 2 из 3" in progress[1], "Кандидат 3 из 3" in progress[2]] \
        == [True] * 3 and all("Читаю свежие отзывы" in c for c in progress)
    first = before[0].media.caption                                       # the first status: what is being looked for
    assert first.startswith("🟠 <b>Понимаю, что искать</b>\n<i>на вечер</i>") and before[0].reply_markup is None

    # the cards: a banner and three photos, then the old panel goes and a new one comes after them
    photos = h.photos(FRIEND, m)
    assert len(photos) == 4
    banner, cards = photos[0], photos[1:]
    assert banner.photo.filename == "selection.jpg" and banner.reply_markup is None
    assert banner.caption.startswith("🟠 <b>Подборка</b>\n<i>на вечер</i>"), banner.caption
    assert "Нашёл <b>3</b>, самое точное совпадение первым. Подкрутить можно в самом низу." in banner.caption
    assert "Отталкиваюсь от" not in banner.caption                        # no game named
    assert all(c.caption.split("\n")[1] == "🟢 <i>Разбор по 50 свежим отзывам</i>" for c in cards)
    assert all("<i>Игрок, " in c.caption and " ч в игре:</i>" in c.caption for c in cards)
    assert [c.photo.filename for c in cards] == ["5.jpg", "4.jpg", "7.jpg"]
    assert [c.caption.split("\n")[0] for c in cards] == [
        "🟠 <b>Dead Cells</b>", "🟠 <b>Ratchet &amp; Clank &lt;Rift Apart&gt;</b>", "🟠 <b>Dead Space</b>"]
    assert all("Залип &amp; не жалею &lt;3" in c.caption and "баги &amp; вылеты" in c.caption for c in cards)
    assert h.buttons(FRIEND, msg=h.with_button(FRIEND, "pp:5")) == ["pp:5", None, "fb:played:5", "fb:skip:5"]
    assert sorted(h.covers) == [4, 5, 7]
    assert h.is_deleted(FRIEND, pid)
    after = h.tg.calls[h.tg.calls.index(cards[-1]):]
    assert [type(c).__name__ for c in after] == ["SendPhoto", "DeleteMessage", "SendAnimation"]
    p = h.check_panel(FRIEND)
    assert p.message_id > max(m.message_id for m in h.live(FRIEND) if m.photo)
    assert "Подкрутить?" in p.caption and "Запрос: на вечер" in p.caption and h.pbuttons(FRIEND) == RESULTS
    sess = h.session(FRIEND)
    assert sess["seen"] == [5, 4, 7] and sess["shown"] == [5, 4, 7] and sess["step"] == "results"
    assert h.req(FRIEND).mood == "evening" and h.state(FRIEND) == "fix"
    assert len([x for x in h.live(FRIEND)]) == 5                          # banner, 3 cards, the panel


@scenario
async def test_typed_message_is_deleted_and_the_panel_reposted(h):
    await h.send(FRIEND, "/start")
    pid = h.panel_id(FRIEND)
    mid = await h.send(FRIEND, "кооп с другом, не шутер")
    assert h.parsed == ["кооп с другом, не шутер"]
    assert h.is_deleted(FRIEND, mid) and h.is_deleted(FRIEND, pid)
    p = h.check_panel(FRIEND)
    assert p.message_id > mid
    # «Понимаю запрос» is posted fresh, then becomes the first question in place
    texts = h.screens(FRIEND)
    understood = [t for t in texts if "Понимаю запрос" in t]
    assert len(understood) == 1 and "кооп с другом, не шутер" in understood[0]
    assert "Что точно не надо?" in p.caption and "Запрос: с друзьями · не шутер" in p.caption
    assert h.plabel(FRIEND, "av:shooter") == "✓ Шутеры"                # understood from the text: ticked
    assert h.session(FRIEND)["avoid"] == ["shooter"] and h.state(FRIEND) == "wizard"
    assert h.s.asked == [] and h.s.requests == []                      # no game named: no «Чем зацепила?»
    # the player's typing keeps nothing but the panel in the chat
    assert [m.message_id for m in h.live(FRIEND)] == [p.message_id]

    # text that is escaped on its way into the panel
    pid = p.message_id
    mid = await h.send(FRIEND, "/find")
    mid = await h.send(FRIEND, "хочу <b>что-то</b> & без доната")
    assert h.saw(FRIEND, "хочу &lt;b&gt;что-то&lt;/b&gt; &amp; без доната")
    assert h.is_deleted(FRIEND, mid) and h.check_panel(FRIEND).message_id > mid
    assert h.plabel(FRIEND, "av:mtx") == "✓ Донат"


# --- the wizard

@scenario
async def test_wizard_with_buttons(h):
    await h.send(FRIEND, "/start")
    m = h.mark()
    await h.send(FRIEND, "как Hollow Knight, но проще")
    pid = h.panel_id(FRIEND)
    seq = h.screens(FRIEND, m)
    assert "Понимаю запрос" in seq[0] and "Вспоминаю игру" in seq[1] and "Hollow Knight" in seq[1]
    assert "Чем именно зацепила Hollow Knight?" in seq[2] and len(seq) == 3
    assert h.s.asked == ["Hollow Knight"] and h.s.requests == [] and h.state(FRIEND) == "wizard"
    assert "Запрос: как Hollow Knight · проще" in h.caption(FRIEND)
    datas = h.pbuttons(FRIEND)
    assert datas == HK_ASPECTS + ["asp:all", "go", "home"], datas
    labels = h.pbuttons(FRIEND, labels=True)
    assert labels[-3] == "Всё сразу ▸"
    # the example answer is made of this game's own options: the first and the last
    example = f"«{labels[0].lower()}, а {labels[len(HK_ASPECTS) - 1].lower()} не главное»"
    assert f"Отметь, что зацепило, или ответь словами: <i>{example}</i>." in h.caption(FRIEND), h.caption(FRIEND)
    sess = h.session(FRIEND)
    assert sess["step"] == "aspects" and sess["ask"]["seed"]["appid"] == HK and sess["ask"]["picked"] == []
    assert len(sess["ask"]["opts"]) == 5 and h.req(FRIEND).asked

    await h.press(FRIEND, "asp:explore")
    await h.press(FRIEND, "asp:atmos")
    await h.press(FRIEND, "asp:combat")
    await h.press(FRIEND, "asp:combat")                        # toggled off again
    labels = h.pbuttons(FRIEND, labels=True)
    assert "✓ Исследование мира" in labels and "✓ Атмосфера" in labels and "Боевая система" in labels
    assert h.pbuttons(FRIEND)[-3:] == ["asp:done", "go", "home"] and labels[-3] == "Дальше ▸"
    assert h.session(FRIEND)["ask"]["picked"] == ["explore", "atmos"] and h.s.requests == []

    await h.press(FRIEND, "asp:done")
    assert "Что точно не надо?" in h.caption(FRIEND)
    assert "исследование мира, атмосфера" in h.caption(FRIEND)          # the lead shows what was picked
    assert h.pbuttons(FRIEND) == AVOID_KEYS + ["av:done", "go", "home"] and h.session(FRIEND)["avoid"] == []
    assert h.pbuttons(FRIEND, labels=True)[-3] == "Ничего, дальше ▸"
    await h.press(FRIEND, "asp:explore", message=h.panel(FRIEND), stale=True)   # the old question: outdated
    assert h.answers()[-1] == OUTDATED

    await h.press(FRIEND, "av:shooter")
    await h.press(FRIEND, "av:grind")
    await h.press(FRIEND, "av:grind")
    await h.press(FRIEND, "av:nosuchkey", message=h.panel(FRIEND))                      # ignored
    assert h.plabel(FRIEND, "av:shooter") == "✓ Шутеры" and h.plabel(FRIEND, "av:grind") == "Гринд"
    assert h.plabel(FRIEND, "av:done") == "Дальше ▸"
    assert h.session(FRIEND)["avoid"] == ["shooter"]

    await h.press(FRIEND, "av:done")
    assert "Сколько есть времени?" in h.caption(FRIEND) and h.pbuttons(FRIEND) == TIME_KEYS + ["home"]
    assert "Примерно, на всю игру целиком." in h.caption(FRIEND)
    assert h.plabel(FRIEND, "tm:evening") == "🌙 Вечер · до 4 ч" and h.plabel(FRIEND, "tm:long") == "♾ Надолго · 30+ ч"
    assert h.s.requests == [] and h.session(FRIEND)["step"] == "time"
    assert "FPS" in h.req(FRIEND).tags_avoid                  # the exclusions are in the request now

    await h.press(FRIEND, "tm:couple")
    _, req, seen = h.last_request()
    assert req.asked and req.focus_axes == ["exploration"] and req.seeds == ["Hollow Knight"]
    assert req.focus_labels == ["Исследование мира", "Атмосфера"]
    assert req.axes["difficulty"] == 3 and req.axes["exploration"] == 9   # the typed «проще» still wins
    assert "Exploration" in req.tags_want and "Atmospheric" in req.tags_want and "Difficult" in req.mute_tags
    assert {"FPS", "Shooter"} <= set(req.tags_avoid) and req.max_hours == 10 and req.min_hours is None
    assert seen == set() and h.state(FRIEND) == "fix" and len(h.s.requests) == 1
    # every question was the same message, edited
    assert h.since(m, "SendAnimation", FRIEND)[1:] == [h.tg.sent("SendAnimation")[-1]]
    assert all(c.message_id == pid for c in h.since(m, "EditMessageCaption", FRIEND)
               + h.since(m, "EditMessageMedia", FRIEND))
    cards = h.photos(FRIEND, m)[1:]
    assert len(cards) == 3 and all("По ощущениям близко к <b>Hollow Knight</b>" in c.caption for c in cards)
    assert "Отталкиваюсь от: <b>Hollow Knight</b>." in h.photos(FRIEND, m)[0].caption
    h.check_panel(FRIEND)

    # a double tap on the answered questions does nothing
    for data in ("asp:done", "av:done", "tm:couple"):
        await h.press(FRIEND, data, message=old_message(FRIEND), stale=True)
        assert h.answers()[-1] == OUTDATED, data
    assert len(h.s.requests) == 1

    # «Ещё 3» keeps the focused request and does not ask again
    await h.press(FRIEND, "more")
    _, req2, seen2 = h.last_request()
    assert req2.focus_axes == ["exploration"] and req2.max_hours == 10 and h.s.asked == ["Hollow Knight"]
    assert seen2 == {5, 4, 7}


@scenario
async def test_wizard_answered_in_words(h):
    await h.send(FRIEND, "как Hollow Knight, но проще")
    mid = await h.send(FRIEND, "атмосфера и исследование, а сложность бесила")
    assert h.parsed == ["как Hollow Knight, но проще"]          # the answer is not a new request
    assert h.is_deleted(FRIEND, mid) and h.check_panel(FRIEND).message_id > mid
    assert "Что точно не надо?" in h.caption(FRIEND) and h.s.requests == []
    req = h.req(FRIEND)
    assert req.asked and req.focus_axes == ["exploration"]
    assert "Difficult" in req.tags_avoid and req.axes["difficulty"] <= 3

    # a correction typed at «Что точно не надо?» joins the request and moves on
    await h.send(FRIEND, "без хоррора")
    assert h.parsed[-1] == "без хоррора" and "Сколько есть времени?" in h.caption(FRIEND)
    req = h.req(FRIEND)
    assert "horror" in req.dealbreakers and req.focus_axes == ["exploration"] and req.seeds == ["Hollow Knight"]
    await h.press(FRIEND, "tm:evening")
    _, req, _ = h.last_request()
    assert req.max_hours == 4 and req.mood == "evening" and "horror" in req.dealbreakers
    assert h.state(FRIEND) == "fix" and "Подкрутить?" in h.check_panel(FRIEND).caption

    # «всё» in words: the seed as a whole
    await h.send(FRIEND, "/find")
    await h.send(FRIEND, "что-нибудь как Hollow Knight")
    await h.send(FRIEND, "всё")
    req = h.req(FRIEND)
    assert req.asked and req.focus_axes is None and "Что точно не надо?" in h.caption(FRIEND)
    # a correction typed at «Сколько есть времени?» starts the search with it
    await h.press(FRIEND, "av:done")
    assert "Сколько есть времени?" in h.caption(FRIEND)
    n = len(h.s.requests)
    await h.send(FRIEND, "а если с кооперативом?")
    _, req, seen = h.last_request()
    assert len(h.s.requests) == n + 1 and req.coop and req.seeds == ["Hollow Knight"] and seen == set()
    h.check_panel(FRIEND)


@scenario
async def test_wizard_ticks_exclusions_and_skips_known_time(h):
    await h.send(FRIEND, "/start")
    m = h.mark()
    await h.send(FRIEND, "что-нибудь как Hollow Knight на вечер без хоррора")
    await h.press(FRIEND, "asp:all")
    assert "Что точно не надо?" in h.caption(FRIEND)
    assert h.pbuttons(FRIEND, labels=True)[0] == "✓ Хоррор" and h.session(FRIEND)["avoid"] == ["horror"]
    assert h.pbuttons(FRIEND, labels=True)[-3] == "Дальше ▸"
    await h.press(FRIEND, "av:noru")
    await h.press(FRIEND, "av:done")                          # hours are known: no time question
    assert not h.saw(FRIEND, "Сколько есть времени?", m)
    _, req, _ = h.last_request()
    assert req.max_hours == 4 and req.focus_axes is None and req.asked
    assert set(req.dealbreakers) >= {"horror", "no_ru"} and "Horror" in req.tags_avoid
    assert "Подкрутить?" in h.check_panel(FRIEND).caption

    # a mood that implies the time («на вечер» button) skips it as well
    await h.send(FRIEND, "/find")
    await h.send(FRIEND, "кооп с другом, не шутер")
    await h.press(FRIEND, "av:done")
    assert "Сколько есть времени?" in h.caption(FRIEND)       # coop says nothing about hours
    await h.press(FRIEND, "tm:long")
    _, req, _ = h.last_request()
    assert req.min_hours == 30 and req.max_hours is None and req.mood == "coop" and req.coop
    await h.send(FRIEND, "/find")
    await h.send(FRIEND, "что-нибудь спокойное")
    await h.press(FRIEND, "av:done")
    await h.press(FRIEND, "tm:evening")                        # the time button sets the mood when there is none
    _, req, _ = h.last_request()
    assert req.max_hours == 4 and req.mood in ("evening", "chill")


@scenario
async def test_go_skips_the_remaining_questions(h):
    # «Подобрать сейчас» at the aspects question: the ticks so far count
    await h.send(FRIEND, "как Hollow Knight")
    await h.press(FRIEND, "asp:explore")
    m = h.mark()
    await h.press(FRIEND, "go")
    _, req, seen = h.last_request()
    assert req.focus_axes == ["exploration"] and req.focus_labels == ["Исследование мира"] and req.asked
    assert not h.saw(FRIEND, "Что точно не надо?", m) and not h.saw(FRIEND, "Сколько есть времени?", m)
    assert "Подкрутить?" in h.check_panel(FRIEND).caption and seen == set()

    # at «Что точно не надо?»: the ticks count, the time question is skipped
    await h.send(FRIEND, "/find")
    await h.send(FRIEND, "что-нибудь как Hollow Knight")
    await h.press(FRIEND, "asp:all")
    await h.press(FRIEND, "av:horror")
    await h.press(FRIEND, "av:pvp")
    m = h.mark()
    await h.press(FRIEND, "go")
    _, req, _ = h.last_request()
    assert set(req.dealbreakers) >= {"horror", "online_only"} and "PvP" in req.tags_avoid
    assert req.focus_axes is None and not h.saw(FRIEND, "Сколько есть времени?", m)

    # with nothing to ask about: straight to the search
    await h.send(FRIEND, "/find")
    await h.send(FRIEND, "что-нибудь спокойное")
    await h.press(FRIEND, "go")
    assert len(h.s.requests) == 3 and "Подкрутить?" in h.check_panel(FRIEND).caption

    # «Подобрать сейчас» with no request at all (an old button)
    h.db.user(BUDDY)
    h.db.update_user(BUDDY, allowed=1)
    await h.press(BUDDY, "go", message=old_message(BUDDY))
    assert h.answers()[-1] == "Начни заново" and len(h.s.requests) == 3


@scenario
async def test_like_button_unknown_reference_and_failed_question(h):
    await h.send(FRIEND, "/start")
    pid = h.panel_id(FRIEND)
    await h.press(FRIEND, "q:like")
    assert h.panel_id(FRIEND) == pid and "Похожее на игру" in h.caption(FRIEND) and h.state(FRIEND) == "like"
    assert h.pbuttons(FRIEND) == ["home"]
    mid = await h.send(FRIEND, "Hollow Knight и Celeste")
    assert h.parsed == [] and h.s.asked == ["Hollow Knight"] and h.is_deleted(FRIEND, mid)
    assert "Чем именно зацепила Hollow Knight?" in h.check_panel(FRIEND).caption and h.state(FRIEND) == "wizard"
    assert h.req(FRIEND).seeds == ["Hollow Knight", "Celeste"]
    await h.press(FRIEND, "asp:visual")
    await h.press(FRIEND, "asp:done")
    await h.press(FRIEND, "go")
    _, req, _ = h.last_request()
    assert req.seeds == ["Hollow Knight", "Celeste"] and req.focus_labels == ["Рисованная графика"]
    assert req.focus_axes == [] and "визуальный стиль" in req.words and "Exploration" in req.mute_tags
    assert "Hand-drawn" in req.tags_want

    # a reference that is not on Steam: nothing to ask about it, straight to the exclusions
    await h.send(FRIEND, "/find")
    await h.send(FRIEND, "что-то как Zelda и Hades")
    assert h.s.asked[-1] == "Zelda" and "Что точно не надо?" in h.caption(FRIEND)
    note = h.caption(FRIEND).split("\n\n", 1)[1]
    assert note.startswith("Не нашёл «Zelda» в Steam"), note
    assert "Отметь, что исключить" in note
    await h.press(FRIEND, "av:horror")                          # the note goes once the question is answered
    assert "Не нашёл" not in h.caption(FRIEND) and h.session(FRIEND).get("missing_note") is None
    await h.press(FRIEND, "go")
    _, req, _ = h.last_request()
    assert req.asked and req.seeds == ["Zelda", "Hades"] and req.focus_axes is None
    banner = h.photos(FRIEND)[-4].caption
    assert banner.startswith("🟠 <b>Подборка</b>\n<i>как Zelda и Hades"), banner
    assert "Отталкиваюсь от: <b>Hades</b>." in banner
    assert "Не нашёл <b>Zelda</b> в Steam, поэтому подбираю по остальному запросу" in banner
    assert all("близко к <b>Hades</b>" in c.caption for c in h.photos(FRIEND)[-3:])

    # the question failing must not stop the request
    h.s.fail = {"aspects"}
    await h.send(FRIEND, "/find")
    await h.send(FRIEND, "как Hollow Knight, но короче")
    assert "Что точно не надо?" in h.caption(FRIEND) and h.req(FRIEND).asked
    assert h.errors == ["aspect options failed"]
    h.errors.clear()
    await h.press(FRIEND, "av:done")                          # «короче» gave the hours: no time question
    assert h.last_request()[1].max_hours == 8 and "Подкрутить?" in h.check_panel(FRIEND).caption


# --- results and corrections

@scenario
async def test_refine_more_and_nothing_left(h):
    await results_for(h, FRIEND)
    pid = h.panel_id(FRIEND)
    m1 = h.mark()
    await h.press(FRIEND, "rf:shorter")
    _, req2, seen2 = h.last_request()
    assert seen2 == {5, 4, 7} and req2.max_hours < 4 and "length" in req2.axes and req2.mood == "evening"
    # a refinement round: three cards, no second banner
    assert [c.photo.filename for c in h.photos(FRIEND, m1)] == ["2.jpg", "8.jpg", "9.jpg"]
    assert f"Запрос: до {req2.max_hours} ч" in h.caption(FRIEND)          # under an evening: the exact limit
    assert h.session(FRIEND)["seen"] == [5, 4, 7, 2, 8, 9] and h.session(FRIEND)["shown"] == [2, 8, 9]
    assert h.is_deleted(FRIEND, pid) and "Подкрутить?" in h.check_panel(FRIEND).caption
    # the statuses were the results panel, edited in place, before it moved down
    status = h.since(m1, "EditMessageCaption", FRIEND)
    assert all(c.message_id == pid for c in status)
    assert "Понимаю, что искать" in status[0].caption and f"до {req2.max_hours} ч" in status[0].caption
    assert all("Читаю свежие отзывы" in c.caption for c in status[1:]) and len(status) == 4

    await h.press(FRIEND, "more")
    _, req3, seen3 = h.last_request()
    assert seen3 == {5, 4, 7, 2, 8, 9} and req3 == req2       # the refined request is kept
    m2 = h.mark()
    await h.press(FRIEND, "rf:harder")                         # one game left in the catalog
    _, req4, seen4 = h.last_request()
    assert req4.axes["difficulty"] >= 7 and len(seen4) == 9
    assert [c.photo.filename for c in h.photos(FRIEND, m2)] == ["6.jpg"]
    pid = h.panel_id(FRIEND)
    m3 = h.mark()
    await h.press(FRIEND, "more")
    p = h.check_panel(FRIEND)
    assert "Ничего не нашлось" in p.caption and "Под все условия сразу ничего не подошло" in p.caption
    assert h.pbuttons(FRIEND) == LOOSEN                                 # buttons that loosen the request
    assert h.panel_id(FRIEND) == pid and h.since(m3, "SendAnimation") == []   # edited, nothing posted
    assert len(h.session(FRIEND)["seen"]) == 10 and h.session(FRIEND)["shown"] == []
    assert h.state(FRIEND) == "fix" and h.db.user_games(FRIEND) == []
    for how in ("easier", "story", "chill"):                    # the results buttons from an older panel
        await h.press(FRIEND, f"rf:{how}", message=old_message(FRIEND))
        assert "Ничего не нашлось" in h.check_panel(FRIEND).caption and h.pbuttons(FRIEND) == LOOSEN, how
    for how in ("noavoid", "different"):
        await h.press(FRIEND, f"rf:{how}")
        assert "Ничего не нашлось" in h.check_panel(FRIEND).caption, how
    _, req5, seen5 = h.last_request()
    assert req5.diversify and req5.axes.get("story", 0) >= 7 and req5.mood == "evening" and len(seen5) == 10
    await h.press(FRIEND, "rf:anylen")
    _, req6, _ = h.last_request()
    assert req6.max_hours is None and req6.min_hours is None and "length" not in req6.axes and req6.mood == "any"
    assert h.photos(FRIEND, m3) == []


@scenario
async def test_written_correction_after_results(h):
    await results_for(h, FRIEND)
    pid = h.panel_id(FRIEND)
    m = h.mark()
    mid = await h.send(FRIEND, "без хоррора")
    assert h.is_deleted(FRIEND, mid) and h.is_deleted(FRIEND, pid)
    assert h.parsed == ["без хоррора"]
    seq = h.screens(FRIEND, m)
    assert "Учитываю поправку" in seq[0] and "без хоррора" in seq[0]
    _, req, seen = h.last_request()
    assert seen == {5, 4, 7} and req.mood == "evening" and req.max_hours == 4
    assert "horror" in req.dealbreakers and "Horror" in req.tags_avoid
    assert [c.photo.filename for c in h.photos(FRIEND, m)] == ["2.jpg", "8.jpg", "9.jpg"]     # no banner
    assert h.session(FRIEND)["seen"] == [5, 4, 7, 2, 8, 9] and h.state(FRIEND) == "fix"
    p = h.check_panel(FRIEND)
    assert "Подкрутить?" in p.caption and "не хоррор" in p.caption and not h.s.asked
    assert not h.saw(FRIEND, "Что точно не надо?", m)          # a correction of results asks nothing

    await h.send(FRIEND, "а если с кооперативом?")
    _, req, seen = h.last_request()
    assert seen == {5, 4, 7, 2, 8, 9} and req.coop and "horror" in req.dealbreakers and req.max_hours == 4
    assert h.session(FRIEND)["seen"] == [5, 4, 7, 2, 8, 9, 10, 1, 3]
    assert len(h.s.requests) == 3 and h.parsed[-1] == "а если с кооперативом?"
    h.check_panel(FRIEND)

    # a correction on «Ничего не нашлось» goes on with the same seen
    m = h.mark()
    await h.press(FRIEND, "more")
    assert [c.photo.filename for c in h.photos(FRIEND, m)] == ["6.jpg"]
    await h.press(FRIEND, "more")
    assert "Ничего не нашлось" in h.caption(FRIEND) and h.pbuttons(FRIEND) == LOOSEN
    await h.send(FRIEND, "можно и подлиннее")
    assert len(h.last_request()[2]) == 10 and "Ничего не нашлось" in h.check_panel(FRIEND).caption


@scenario
async def test_correction_naming_a_game_restarts_the_wizard(h):
    await results_for(h, FRIEND)
    await h.send(FRIEND, "а как Hollow Knight?")
    assert h.s.asked == ["Hollow Knight"] and len(h.s.requests) == 1
    assert "Чем именно зацепила Hollow Knight?" in h.check_panel(FRIEND).caption and h.state(FRIEND) == "wizard"
    req = h.req(FRIEND)
    assert req.seeds == ["Hollow Knight"] and req.mood == "evening" and req.max_hours == 4 and req.asked
    await h.press(FRIEND, "asp:explore")
    await h.press(FRIEND, "asp:done")
    m = h.mark()
    await h.press(FRIEND, "av:done")                           # the hours are still known
    assert not h.saw(FRIEND, "Сколько есть времени?", m)
    _, req, _ = h.last_request()
    assert req.seeds == ["Hollow Knight"] and req.focus_axes == ["exploration"] and req.max_hours == 4
    assert len(h.s.requests) == 2 and "Подкрутить?" in h.check_panel(FRIEND).caption

    # naming another game later forgets what was loved in the first
    await h.send(FRIEND, "а как Celeste")
    req = h.req(FRIEND)
    assert req.seeds == ["Celeste"] and not req.focus_labels and h.s.asked[-1] == "Celeste"
    assert h.state(FRIEND) == "wizard"


@scenario
async def test_feedback_buttons_on_cards(h):
    await results_for(h, FRIEND, "chill")
    assert h.last_request()[1].mood == "chill"
    pid = h.panel_id(FRIEND)
    card = h.with_button(FRIEND, "fb:skip:4")
    await h.press(FRIEND, "fb:skip:4")
    assert h.answers()[-1] == "Понял, в этом поиске больше не покажу"
    assert h.buttons(FRIEND, labels=True, msg=card)[2:] == ["✅ Уже играл", "✓ 👎 Не то"]
    assert h.session(FRIEND)["seen"] == [5, 4, 7] and h.session(FRIEND)["shown"] == [5, 4, 7]
    await h.press(FRIEND, "fb:played:4", message=h.tg.messages[(FRIEND, card.message_id)])
    assert h.buttons(FRIEND, labels=True, msg=card)[2:] == ["✓ ✅ Уже играл", "👎 Не то"]
    await h.press(FRIEND, "fb:played:4", message=h.tg.messages[(FRIEND, card.message_id)])   # unchanged: fine
    assert h.db.user_games(FRIEND) == []                       # nothing is remembered about the player
    assert h.panel_id(FRIEND) == pid and h.check_panel(FRIEND)  # the panel is not touched

    await h.press(FRIEND, "more")
    assert h.last_request()[2] == {5, 4, 7}
    # a game turned down on an old card joins this request's seen too
    await h.press(FRIEND, "fb:skip:10", message=old_message(FRIEND))
    assert 10 in h.session(FRIEND)["seen"] and h.answers()[-1] == "Понял, в этом поиске больше не покажу"
    gone = InaccessibleMessage(chat=Chat(id=FRIEND, type="private"), message_id=7)   # older than 48 h
    await h.press(FRIEND, "fb:skip:1", message=gone)
    assert 1 in h.session(FRIEND)["seen"]
    await h.press(FRIEND, "more")
    assert {10, 1} <= h.last_request()[2]
    assert h.db.user_games(FRIEND) == []

    # without a request (a card from before a restart): just an acknowledgement
    h.db.update_user(FRIEND, session="{}")
    await h.press(FRIEND, "fb:skip:3", message=old_message(FRIEND))
    assert h.answers()[-1] == "Понял" and h.session(FRIEND) == {}


# --- breakdowns

@scenario
async def test_breakdown_via_the_game_button(h):
    await h.send(FRIEND, "/start")
    pid = h.panel_id(FRIEND)
    await h.press(FRIEND, "game")
    assert h.panel_id(FRIEND) == pid and "Разбор игры" in h.caption(FRIEND) and h.state(FRIEND) == "game"
    assert h.pbuttons(FRIEND) == ["home"]
    m = h.mark()
    mid = await h.send(FRIEND, "ratchet")
    assert h.is_deleted(FRIEND, mid) and h.parsed == [] and h.s.analyzed == [4]
    seq = h.screens(FRIEND, m)
    assert "Ищу игру" in seq[0] and "Читаю свежие отзывы" in seq[1]
    shot = h.photos(FRIEND, m)
    assert len(shot) == 1 and shot[0].photo.filename == "4.jpg" and h.covers == [4]
    cap = shot[0].caption
    assert cap.startswith("🟠 <b>Ratchet &amp; Clank &lt;Rift Apart&gt;</b>") and "Бежишь &amp; рубишь" in cap
    assert "Разбор по 50 свежим отзывам" in cap and "<b>Не бери, если</b> не любишь &lt;спешку&gt;" in cap
    card = [x for x in h.live(FRIEND) if x.photo][-1]
    assert h.buttons(FRIEND, msg=card) == ["sim:4", None] and card.caption == cap
    # the card, then the panel below it
    p = h.check_panel(FRIEND)
    names = [type(c).__name__ for c in h.tg.calls[h.tg.calls.index(shot[0]):]]
    assert names == ["SendPhoto", "DeleteMessage", "SendAnimation"], names
    assert "Ещё разбор?" in p.caption and h.pbuttons(FRIEND) == ["sim:4", "home"] and h.state(FRIEND) == "game"

    m = h.mark()
    await h.send(FRIEND, "ratchet & clank <rift apart>")       # exact, already read: no «reading» status
    assert len(h.photos(FRIEND, m)) == 1 and not h.saw(FRIEND, "Читаю свежие отзывы", m)
    assert h.s.analyzed == [4, 4]

    await h.send(FRIEND, "<b>zzz")                             # the player's text is escaped too
    assert "Не нашёл" in h.check_panel(FRIEND).caption and h.saw(FRIEND, "&lt;b&gt;zzz")
    assert h.state(FRIEND) == "game" and h.pbuttons(FRIEND) == ["home"]
    await h.send(FRIEND, "dead")
    assert "Какую из них?" in h.caption(FRIEND) and h.pbuttons(FRIEND) == ["pp:5", "pp:7", "home"]
    await h.press(FRIEND, "pp:7")
    assert h.photos(FRIEND)[-1].photo.filename == "7.jpg" and "Ещё разбор?" in h.check_panel(FRIEND).caption
    assert h.pbuttons(FRIEND)[0] == "sim:7"

    h.s.fail = {"resolve"}
    await h.send(FRIEND, "Hades")
    assert "Что-то пошло не так" in h.check_panel(FRIEND).caption and h.pbuttons(FRIEND) == ["home"]
    assert h.errors == ["text handler failed"]
    h.errors.clear()
    h.s.fail = {"analyze"}
    await h.send(FRIEND, "Celeste")
    assert "Steam не отдал отзывы" in h.check_panel(FRIEND).caption and h.pbuttons(FRIEND) == ["home"]
    assert h.errors == ["analyze failed"] and h.s.busy == 0 and h.state(FRIEND) == "game"
    h.errors.clear()
    h.s.fail = set()
    await h.press(FRIEND, "home")
    assert h.state(FRIEND) == ""
    await h.send(FRIEND, "Celeste")                            # after «В начало» a title is a request
    assert h.parsed == ["Celeste"]


@scenario
async def test_breakdown_from_a_card_keeps_the_results_panel(h):
    await results_for(h, FRIEND)
    m = h.mark()
    await h.press(FRIEND, "pp:5")                              # read during the search: no status
    assert not h.saw(FRIEND, "Читаю свежие отзывы", m) and len(h.photos(FRIEND, m)) == 1
    p = h.check_panel(FRIEND)
    assert "Подкрутить?" in p.caption and h.pbuttons(FRIEND) == RESULTS and h.state(FRIEND) == "fix"
    assert h.session(FRIEND)["step"] == "results"

    m = h.mark()
    await h.press(FRIEND, "pp:10", message=old_message(FRIEND))  # not read yet: the panel says so first
    seq = h.screens(FRIEND, m)
    assert "Читаю свежие отзывы" in seq[0] and "До минуты" in seq[0]
    assert h.since(m, "EditMessageMedia")[0].message_id == p.message_id   # the panel switched to «game»
    assert h.photos(FRIEND, m)[0].photo.filename == "10.jpg"
    assert "Подкрутить?" in h.check_panel(FRIEND).caption and h.pbuttons(FRIEND) == RESULTS
    await h.press(FRIEND, "more")
    assert h.last_request()[2] == {5, 4, 7}


@scenario
async def test_similar_from_a_breakdown(h):
    await h.send(FRIEND, "/game")
    await h.send(FRIEND, "hollow knight")
    card = [x for x in h.live(FRIEND) if x.photo][-1]
    assert h.buttons(FRIEND, msg=card)[0] == f"sim:{HK}" and h.s.asked == []
    m = h.mark()
    await h.press(FRIEND, f"sim:{HK}", message=card)           # under the card
    assert h.s.asked == ["Hollow Knight"] and h.state(FRIEND) == "wizard"
    seq = h.screens(FRIEND, m)
    assert "Вспоминаю игру" in seq[0] and "Чем именно зацепила Hollow Knight?" in seq[-1]
    assert h.since(m, "SendAnimation")                         # posted fresh under the card
    assert h.check_panel(FRIEND).message_id > card.message_id
    req = h.req(FRIEND)
    assert req.seeds == ["Hollow Knight"] and req.text == "как Hollow Knight"
    await h.press(FRIEND, "asp:challenge")
    await h.press(FRIEND, "go")
    _, req, _ = h.last_request()
    assert req.seeds == ["Hollow Knight"] and req.focus_axes == ["difficulty"] and req.axes["difficulty"] == 8

    # the same from the panel under the breakdown (another player, who has not searched yet)
    await h.send(OWNER, "/game")
    await h.send(OWNER, "hollow knight")
    assert h.pbuttons(OWNER) == [f"sim:{HK}", "home"]
    await h.press(OWNER, f"sim:{HK}")
    assert "Чем именно зацепила Hollow Knight?" in h.check_panel(OWNER).caption

    n = len(h.s.asked)
    await h.press(FRIEND, "sim:999", message=old_message(FRIEND))   # a game that is gone: nothing happens
    assert len(h.s.asked) == n


# --- access

@scenario
async def test_stranger_is_gated_and_owner_asked_once(h):
    mid = await h.send(STRANGER, "/start", first_name="Eve <script>", username="eve")
    assert "Бот приватный" in h.last(STRANGER) and f"<code>{STRANGER}</code>" in h.last(STRANGER)
    assert "Я уже написал владельцу" in h.last(STRANGER)
    assert not h.is_deleted(STRANGER, mid)
    ask = h.last(OWNER)
    assert "Просится в бота" in ask and "Eve &lt;script&gt; @eve" in ask
    assert h.buttons(OWNER) == [f"allow:{STRANGER}"]
    await h.send(STRANGER, "Hollow Knight")
    await h.press(STRANGER, "q:evening", message=old_message(STRANGER))
    assert h.answers()[-1] == "Бот приватный"
    assert len(h.out(OWNER)) == 1                              # the owner is told once
    assert len(h.out(STRANGER)) == 2 and h.s.resolved == [] and h.parsed == [] and h.s.requests == []
    assert h.tg.sent("SendAnimation") == []                    # no panel for a stranger
    await h.press(FRIEND, f"allow:{STRANGER}", message=old_message(FRIEND))
    assert STRANGER not in h.db.allowed_users()               # only an owner lets people in

    await h.press(OWNER, f"allow:{STRANGER}")
    assert STRANGER in h.db.allowed_users() and h.answers()[-1] == "Пустил"
    assert "Тебя пустили" in h.last(STRANGER) and "/start" in h.last(STRANGER)
    # the request is settled: one inert «✓ Пустил» button, answered silently
    assert h.buttons(OWNER) == ["noop"] and h.buttons(OWNER, labels=True) == ["✓ Пустил"]
    n = len(h.out(STRANGER))
    await h.press(OWNER, "noop")
    assert h.answers()[-1] is None and len(h.out(STRANGER)) == n and h.buttons(OWNER) == ["noop"]
    await h.send(STRANGER, "/start", first_name="Eve")
    assert "Привет, Eve" in h.check_panel(STRANGER).caption and h.pbuttons(STRANGER) == HOME


@scenario
async def test_owner_allow_and_stats(h):
    await h.send(OWNER, "/allow")
    assert "Пустить друга" in h.last(OWNER) and "<code>/allow 123456789</code>" in h.last(OWNER)
    await h.send(OWNER, "/allow 555")
    assert BUDDY in h.db.allowed_users()
    assert "Тебя пустили" in h.last(BUDDY) and "Доступ открыт" in h.last(OWNER) and "id — <b>555</b>" in h.last(OWNER)
    await h.send(BUDDY, "/start")
    assert "Во что поиграть сейчас?" in h.check_panel(BUDDY).caption

    h.db.add_llm_usage(12_500, 3_400)
    h.db.enqueue_analysis([1, 2])
    await h.send(OWNER, "/stats")
    stats = h.last(OWNER)
    assert "Статистика" in stats and f"Игр в базе — <b>{len(GAMES)}</b>" in stats
    assert f"Готовы к подбору — <b>{len(GAMES)}</b>" in stats and "Очередь анализа — <b>2</b>" in stats
    assert f"Разборов отзывов сегодня — <b>1 из {h.cfg.llm_daily_games}</b>" in stats
    assert "1 разборов, 12k / 3k токенов" in stats and "Аналитик: Gemini → Groq" in stats

    sent = len(h.tg.sent("SendMessage"))
    await h.send(FRIEND, "/stats")
    await h.send(FRIEND, "/allow 777")
    assert len(h.tg.sent("SendMessage")) == sent and 777 not in h.db.allowed_users()


# --- robustness

@scenario
async def test_stale_callbacks(h):
    await h.send(FRIEND, "/start")
    for data in ("mood:any", "next:disliked", "new"):         # buttons of older versions of the bot
        await h.press(FRIEND, data, message=old_message(FRIEND), stale=True)
        assert h.answers()[-1] == STALE, data
    for data in ("asp:explore", "asp:done", "av:horror", "av:done", "tm:evening"):
        await h.press(FRIEND, data, message=old_message(FRIEND))
        assert h.answers()[-1] == OUTDATED, data
    await h.press(FRIEND, "go", message=old_message(FRIEND))
    assert h.answers()[-1] == "Начни заново" and h.s.requests == []
    # refining with no request: back to the home panel, edited in place
    pid = h.panel_id(FRIEND)
    for data in ("rf:shorter", "more", "retry"):
        await h.press(FRIEND, "help")
        await h.press(FRIEND, data, message=old_message(FRIEND))
        assert "Во что поиграть сейчас?" in h.caption(FRIEND) and h.pbuttons(FRIEND) == HOME, data
    assert h.panel_id(FRIEND) == pid and h.s.requests == []
    await h.press(FRIEND, "q:nosuchkey", message=old_message(FRIEND))
    assert h.s.requests == []
    # a message too old to touch still works
    gone = InaccessibleMessage(chat=Chat(id=FRIEND, type="private"), message_id=7)
    await h.press(FRIEND, "q:gems", message=gone)
    assert h.last_request()[1].mood == "gems" and "Подкрутить?" in h.check_panel(FRIEND).caption
    await h.press(FRIEND, "rf:unknown", message=old_message(FRIEND))   # an unknown refinement: same request
    assert h.last_request()[1].mood == "gems" and len(h.s.requests) == 2 and h.last_request()[2] == {5, 4, 7}
    # the old question buttons after a search are outdated too
    await h.press(FRIEND, "av:done", message=old_message(FRIEND))
    assert h.answers()[-1] == OUTDATED and len(h.s.requests) == 2


@scenario
async def test_recommend_failure_offers_retry(h):
    await h.send(FRIEND, "/start")
    pid = h.panel_id(FRIEND)
    h.s.fail = {"recommend"}
    await h.press(FRIEND, "q:story")
    assert "Не получилось собрать подборку: один из сервисов не ответил" in h.caption(FRIEND)
    assert h.pbuttons(FRIEND) == ["retry", "home"]
    assert h.panel_id(FRIEND) == pid and h.photos(FRIEND) == []          # edited in place, no cards
    assert h.errors == ["request failed"] and not h.app.running and h.session(FRIEND)["step"] == "failed"
    h.errors.clear()

    h.s.fail = set()
    await h.press(FRIEND, "retry")
    _, req, seen = h.last_request()
    assert req.mood == "story" and seen == set() and len(h.photos(FRIEND)) == 4
    assert "Подкрутить?" in h.check_panel(FRIEND).caption

    # a failure of «Ещё 3» keeps what was seen for the retry
    h.s.fail = {"recommend"}
    await h.press(FRIEND, "more")
    h.errors.clear()
    assert h.pbuttons(FRIEND) == ["retry", "home"] and h.session(FRIEND)["seen"] == [5, 4, 7]
    h.s.fail = set()
    m = h.mark()
    await h.press(FRIEND, "retry")
    assert h.last_request()[2] == {5, 4, 7} and h.session(FRIEND)["seen"] == [5, 4, 7, 2, 8, 9]
    assert len(h.photos(FRIEND, m)) == 3                         # a second round: no banner


@scenario
async def test_nothing_found(h):
    h.s.fail = {"empty"}
    await h.send(FRIEND, "хочу <b>что-то</b> & без доната")
    await h.press(FRIEND, "go")
    _, req, _ = h.last_request()
    assert req.dealbreakers == ["mtx"]
    p = h.check_panel(FRIEND)
    assert "Ничего не нашлось" in p.caption and h.pbuttons(FRIEND) == LOOSEN and h.photos(FRIEND) == []
    assert "Под все условия сразу ничего не подошло. Сними часть ограничений кнопкой" in p.caption
    assert "без доната" in p.caption and h.state(FRIEND) == "fix"
    assert h.session(FRIEND)["shown"] == [] and h.session(FRIEND)["seen"] == []
    await h.press(FRIEND, "rf:different")                     # a refinement with nothing shown is fine
    assert h.last_request()[1].diversify and h.pbuttons(FRIEND) == LOOSEN
    h.s.fail = set()
    await h.send(FRIEND, "можно и донат")                      # a correction from «Ничего не нашлось»
    assert len(h.photos(FRIEND)) == 4 and "Подкрутить?" in h.check_panel(FRIEND).caption
    assert h.pbuttons(FRIEND) == RESULTS


@scenario
async def test_loosen_buttons_after_nothing_found(h):
    """«Снять исключения» drops what was excluded (the language stays), «Любая длина» the hours; both keep
    the rest of the request and what was seen."""
    h.s.fail = {"empty"}
    await h.send(FRIEND, "что-нибудь без хоррора и без доната на вечер")
    assert h.plabel(FRIEND, "av:horror") == "✓ Хоррор" and h.plabel(FRIEND, "av:mtx") == "✓ Донат"
    await h.press(FRIEND, "av:shooter")
    await h.press(FRIEND, "av:noru")
    await h.press(FRIEND, "av:done")                           # the hours are known: the search starts
    _, req, _ = h.last_request()
    assert {"horror", "mtx", "no_ru"} <= set(req.dealbreakers) and "Shooter" in req.tags_avoid
    assert req.max_hours == 4
    assert "Ничего не нашлось" in h.check_panel(FRIEND).caption and h.pbuttons(FRIEND) == LOOSEN
    assert h.plabel(FRIEND, "rf:noavoid") == "Снять исключения" and h.plabel(FRIEND, "rf:anylen") == "Любая длина"

    await h.press(FRIEND, "rf:noavoid")
    _, req, seen = h.last_request()
    assert req.dealbreakers == ["no_ru"] and req.tags_avoid == [] and req.avoid == []
    assert req.max_hours == 4 and seen == set() and len(h.s.requests) == 2
    assert "Ничего не нашлось" in h.check_panel(FRIEND).caption and h.pbuttons(FRIEND) == LOOSEN
    assert h.req(FRIEND).dealbreakers == ["no_ru"]                # the loosened request is the session's now

    await h.press(FRIEND, "rf:anylen")
    _, req, _ = h.last_request()
    assert req.max_hours is None and req.min_hours is None and "length" not in req.axes
    assert req.mood == "any" and req.dealbreakers == ["no_ru"]   # what was loosened before stays loosened
    assert h.pbuttons(FRIEND) == LOOSEN

    h.s.fail = set()
    await h.press(FRIEND, "rf:noavoid")                         # something now: the cards and the results panel
    assert len(h.photos(FRIEND)) == 4 and "Подкрутить?" in h.check_panel(FRIEND).caption
    assert h.pbuttons(FRIEND) == RESULTS
    await h.press(FRIEND, "home")
    assert "Во что поиграть сейчас?" in h.caption(FRIEND)


@scenario
async def test_request_while_another_is_running(h):
    h.s.hold = asyncio.Event()
    await h.send(FRIEND, "/start")
    first = asyncio.create_task(h.press(FRIEND, "q:evening"))
    await until(lambda: h.s.requests)
    assert FRIEND in h.app.running and "Понимаю, что искать" in h.caption(FRIEND)
    assert h.pbuttons(FRIEND) == []
    await h.press(FRIEND, "q:chill", message=old_message(FRIEND))
    assert "Уже ищу" in h.caption(FRIEND) and len(h.s.requests) == 1
    await h.press(FRIEND, "more", message=old_message(FRIEND))
    assert "Уже ищу" in h.caption(FRIEND) and len(h.s.requests) == 1
    # another player is not blocked by it
    other = asyncio.create_task(h.press(OWNER, "q:story", message=old_message(OWNER)))
    await until(lambda: len(h.s.requests) == 2)
    h.s.hold.set()
    await asyncio.gather(first, other)
    assert not h.app.running and len(h.photos(FRIEND)) == 4 and len(h.photos(OWNER)) == 4
    assert h.req(FRIEND).mood == "evening" and "Подкрутить?" in h.check_panel(FRIEND).caption
    h.check_panel(OWNER)
    await h.press(FRIEND, "q:chill", message=old_message(FRIEND))
    assert h.last_request()[1].mood == "chill" and "Подкрутить?" in h.check_panel(FRIEND).caption


@scenario
async def test_panel_edit_failure_posts_a_new_panel(h):
    await h.send(FRIEND, "/start")
    pid = h.panel_id(FRIEND)
    stale_panel = h.panel(FRIEND)
    h.tg.deleted.add((FRIEND, pid))                            # the player deleted the panel
    m = h.mark()
    await h.press(FRIEND, "help", message=stale_panel)         # a button still on their other device
    tried = h.since(m, "EditMessageMedia")
    assert len(tried) == 1 and tried[0].message_id == pid
    p = h.check_panel(FRIEND)
    assert p.message_id != pid and "Как я подбираю" in p.caption and h.pbuttons(FRIEND) == ["home"]
    assert len(h.since(m, "SendAnimation")) == 1 and h.session(FRIEND)["panel_screen"] == "help"

    # the same screen: the caption edit fails, a new panel is posted
    pid = p.message_id
    h.tg.deleted.add((FRIEND, pid))
    m = h.mark()
    await h.press(FRIEND, "help", message=old_message(FRIEND))
    assert [c.message_id for c in h.since(m, "EditMessageCaption")] == [pid]
    assert h.check_panel(FRIEND).message_id != pid and "Как я подбираю" in h.caption(FRIEND)

    # «message is not modified» is not a failure: nothing new is posted
    pid = h.panel_id(FRIEND)
    m = h.mark()
    await h.press(FRIEND, "help", message=h.panel(FRIEND))
    assert h.panel_id(FRIEND) == pid and h.since(m, "SendAnimation") == [] and h.since(m, "DeleteMessage") == []

    # a network error on the edit: the old panel is removed and a new one posted
    h.tg.fail_next["EditMessageMedia"] = TelegramNetworkError(method=None, message="timeout")
    await h.press(FRIEND, "home")
    assert h.is_deleted(FRIEND, pid) and "Во что поиграть сейчас?" in h.check_panel(FRIEND).caption

    # the animation can't be sent: the panel becomes a text message, and later an animation again
    pid = h.panel_id(FRIEND)
    h.tg.fail_next["SendAnimation"] = TelegramBadRequest(method=None, message="Bad Request: wrong file")
    await h.send(FRIEND, "/help")
    text_panel = h.panel(FRIEND)
    assert text_panel.text and "Как я подбираю" in text_panel.text and h.is_deleted(FRIEND, pid)
    assert h.session(FRIEND)["panel_screen"] == "" and h.pbuttons(FRIEND) == ["home"]
    await h.press(FRIEND, "home")
    assert h.is_deleted(FRIEND, text_panel.message_id)
    assert "Во что поиграть сейчас?" in h.check_panel(FRIEND).caption

    # the panel disappears during a search: the cards and a new panel still arrive
    await h.press(FRIEND, "q:evening")
    h.tg.deleted.add((FRIEND, h.panel_id(FRIEND)))
    await h.press(FRIEND, "more", message=old_message(FRIEND))
    assert len(h.photos(FRIEND)) == 4 + 3 and "Подкрутить?" in h.check_panel(FRIEND).caption


@scenario
async def test_render_failures_fall_back_to_text(h):
    def broken(*args, **kwargs):
        raise OSError("font missing")

    saved = render.pick_card, render.game_card, render.selection_banner
    render.pick_card = render.game_card = render.selection_banner = broken
    try:
        await h.press(FRIEND, "q:evening", message=old_message(FRIEND))
        assert h.photos(FRIEND) == []
        texts = [c.text for c in h.tg.sent("SendMessage") if c.chat_id == FRIEND]
        assert [t.split("\n")[0] for t in texts[:3]] == [
            "🟠 <b>Dead Cells</b>", "🟠 <b>Ratchet &amp; Clank &lt;Rift Apart&gt;</b>", "🟠 <b>Dead Space</b>"]
        assert h.buttons(FRIEND, msg=h.with_button(FRIEND, "pp:5"))[0] == "pp:5"
        assert "Подкрутить?" in h.check_panel(FRIEND).caption
        await h.press(FRIEND, "pp:5")
        assert "Быстрый &amp; злой &lt;рогалик&gt; про Dead Cells" in "\n".join(h.out(FRIEND))
        assert "Подкрутить?" in h.check_panel(FRIEND).caption and h.pbuttons(FRIEND) == RESULTS
        assert h.errors == ["banner failed", "card failed for 5", "card failed for 4", "card failed for 7",
                            "game card failed for 5"], h.errors
        h.errors.clear()
    finally:
        render.pick_card, render.game_card, render.selection_banner = saved


@scenario
async def test_failed_status_does_not_lock_the_player(h):
    """A Telegram network error while showing the «reading» status must not leave the player in app.running."""
    h.tg.fail_next["SendAnimation"] = TelegramNetworkError(method=None, message="timeout")
    h.tg.fail_next["SendMessage"] = TelegramNetworkError(method=None, message="timeout")
    h.db.user(FRIEND)
    try:
        await h.press(FRIEND, "q:evening", message=old_message(FRIEND))
    except TelegramNetworkError:
        pass
    assert FRIEND not in h.app.running, "the player is stuck: every next request answers «Уже ищу»"
    await h.press(FRIEND, "q:evening", message=old_message(FRIEND))
    assert "Подкрутить?" in h.check_panel(FRIEND).caption


@scenario
async def test_leaving_a_question_through_a_button_resets_the_state(h):
    """After «В начало» typed text is a new request, not an answer to the question that was on screen."""
    await h.send(FRIEND, "/start")
    await h.send(FRIEND, "как Hollow Knight, но проще")
    assert h.state(FRIEND) == "wizard"
    await h.press(FRIEND, "home")
    assert h.state(FRIEND) == "" and "Во что поиграть сейчас?" in h.caption(FRIEND)
    await h.send(FRIEND, "кооп с другом, не шутер")
    assert h.parsed[-1] == "кооп с другом, не шутер" and not h.req(FRIEND).seeds
    assert "Что точно не надо?" in h.caption(FRIEND)

    await h.press(FRIEND, "home")
    await h.press(FRIEND, "q:evening")                         # a quick request from the wizard's home
    assert h.state(FRIEND) == "fix"
    await h.press(FRIEND, "game", message=old_message(FRIEND))
    assert h.state(FRIEND) == "game"
    await h.press(FRIEND, "home")
    n = len(h.s.requests)
    await h.send(FRIEND, "что-нибудь спокойное")
    assert h.parsed[-1] == "что-нибудь спокойное" and len(h.s.requests) == n and h.state(FRIEND) == "wizard"


# --- regressions: bugs found while writing these tests, fixed since

@scenario
async def test_typed_correction_keeps_ticked_exclusions(h):
    """Ticks on «Что точно не надо?» must survive a correction typed on that screen
    (bot.correct() at step "avoid" goes to ask_time with the session's request, without sess["avoid"])."""
    await h.send(FRIEND, "что-нибудь как Hollow Knight")
    await h.press(FRIEND, "asp:all")
    await h.press(FRIEND, "av:shooter")
    await h.send(FRIEND, "и покороче")              # the hours are known now: the search starts
    _, req, _ = h.last_request()
    assert "Shooter" in req.tags_avoid, req.tags_avoid


@scenario
async def test_unticking_an_exclusion_from_the_text(h):
    """«Хоррор» is ticked because the text said «без хоррора»; unticking it must let horror back in."""
    await h.send(FRIEND, "что-нибудь без хоррора")
    assert h.pbuttons(FRIEND, labels=True)[0] == "✓ Хоррор"
    await h.press(FRIEND, "av:horror")
    assert h.pbuttons(FRIEND, labels=True)[0] == "Хоррор"
    await h.press(FRIEND, "go")
    _, req, _ = h.last_request()
    assert "horror" not in req.dealbreakers and "Horror" not in req.tags_avoid, (req.dealbreakers, req.tags_avoid)


@scenario
async def test_typed_request_while_a_search_runs(h):
    """A request typed while a search runs must not be overwritten by that search: its question stays on
    screen (or it waits), and the results that arrive keep their own request in the session."""
    h.s.hold = asyncio.Event()
    await h.send(FRIEND, "/start")
    first = asyncio.create_task(h.press(FRIEND, "q:evening"))
    await until(lambda: h.s.requests)
    mid = await h.send(FRIEND, "кооп с другом, не шутер")
    # the panel says the text is taken, for after the search; nothing is parsed or searched yet
    p = h.panel(FRIEND)
    assert p.caption.startswith("🟠 <b>Ищу</b>\n<i>Потом учту: «кооп с другом, не шутер»</i>"), p.caption
    assert p.reply_markup is None and h.is_deleted(FRIEND, mid)
    assert h.parsed == [] and len(h.s.requests) == 1 and h.session(FRIEND)["pending"] == "кооп с другом, не шутер"
    h.s.hold.set()
    await first
    # after the cards, the text is applied as a correction of the request that was running
    assert h.parsed == ["кооп с другом, не шутер"] and len(h.s.requests) == 2
    _, req, seen = h.last_request()
    assert req.coop and "Shooter" in req.tags_avoid and req.max_hours == 4 and seen == {5, 4, 7}
    assert "pending" not in h.session(FRIEND) or h.session(FRIEND)["pending"] is None
    req = h.req(FRIEND)
    assert req.to_json() == h.last_request()[1].to_json(), (
        f"the session holds {req.label()!r} while the results on screen are for {h.last_request()[1].label()!r}")
    assert "Подкрутить?" in h.check_panel(FRIEND).caption and h.state(FRIEND) == "fix"


@scenario
async def test_breakdown_after_a_search(h):
    """/game after a search: show_game() picks the panel by session["step"], which ask_game() leaves
    at "results", so the player gets «Подкрутить?» while the state is "game" (text = another title)."""
    await results_for(h, FRIEND)
    await h.send(FRIEND, "/game")                    # the command, straight from the results
    await h.send(FRIEND, "hollow knight")
    assert h.state(FRIEND) == "game"
    assert "Ещё разбор?" in h.check_panel(FRIEND).caption, h.caption(FRIEND)
    assert h.pbuttons(FRIEND) == [f"sim:{HK}", "home"]


@scenario
async def test_shorter_correction_never_lengthens(h):
    """«покороче» typed under a «на вечер» (≤ 4 h) selection must not raise the limit
    (intent.merge takes parse_heuristic's absolute 8 h for «покороче»)."""
    await results_for(h, FRIEND)
    await h.send(FRIEND, "покороче")
    _, req, _ = h.last_request()
    assert req.max_hours <= 4, req.max_hours


# --- the region, the banner, new request or correction

def _lead(caption: str) -> str:
    """The italic line under a panel's title."""
    return caption.split("\n")[1]


@scenario
async def test_region_asked_before_the_first_search(h):
    """Prices are the player's own: with no region yet, the first search asks for it, then goes on."""
    h.db.update_user(FRIEND, region="")
    await h.send(FRIEND, "/start")
    await h.press(FRIEND, "q:evening", message=h.panel(FRIEND))
    assert "Регион аккаунта Steam" in h.caption(FRIEND) and h.s.requests == []
    assert _lead(h.caption(FRIEND)) == "<i>Один вопрос перед первой подборкой</i>"   # a request is waiting
    assert h.session(FRIEND)["pending_req"]
    assert "rg:ru" in h.pbuttons(FRIEND) and h.pbuttons(FRIEND)[-2:] == ["rg:other", "home"]
    await h.press(FRIEND, "rg:kz", message=h.panel(FRIEND))
    assert h.db.user(FRIEND)["region"] == "kz" and not h.session(FRIEND).get("pending_req")
    assert len(h.s.requests) == 1 and h.s.requests[0][1].mood == "evening"       # the search went on
    assert h.s.price_calls and h.s.price_calls[-1][1] == "kz"
    # changing it later from the home screen, with a typed code
    await h.press(FRIEND, "home", message=h.panel(FRIEND))
    await h.press(FRIEND, "region", message=h.panel(FRIEND))
    assert _lead(h.caption(FRIEND)) == "<i>Сейчас: 🇰🇿 Казахстан</i>"
    await h.press(FRIEND, "rg:other", message=h.panel(FRIEND))
    await h.send(FRIEND, "pl")
    assert h.db.user(FRIEND)["region"] == "pl" and "Во что поиграть сейчас?" in h.caption(FRIEND)
    assert len(h.s.requests) == 1                               # no request was waiting: nothing ran


@scenario
async def test_home_forgets_the_request_waiting_for_the_region(h):
    """«В начало» on the region question drops the waiting request: picking a region later from the home
    screen just sets it, and does not start that old search."""
    h.db.update_user(FRIEND, region="")
    await h.send(FRIEND, "/start")
    await h.press(FRIEND, "q:story")
    assert h.session(FRIEND)["pending_req"] and "Один вопрос перед первой подборкой" in h.caption(FRIEND)
    await h.press(FRIEND, "home")                                # the button in the region keyboard
    assert h.session(FRIEND)["pending_req"] is None and "Во что поиграть сейчас?" in h.caption(FRIEND)
    await h.press(FRIEND, "region")
    assert _lead(h.caption(FRIEND)) == "<i>Чтобы цены были как в твоём Steam</i>"     # no region, no request
    await h.press(FRIEND, "rg:de")
    assert h.db.user(FRIEND)["region"] == "de" and h.s.requests == []
    assert "Во что поиграть сейчас?" in h.check_panel(FRIEND).caption and h.pbuttons(FRIEND) == HOME
    # /find clears it as well
    h.db.update_user(FRIEND, region="")
    await h.press(FRIEND, "q:chill")
    assert h.session(FRIEND)["pending_req"]
    await h.send(FRIEND, "/find")
    assert h.session(FRIEND)["pending_req"] is None
    await h.send(FRIEND, "кооп с другом, не шутер")             # a new request asks for the region again
    await h.press(FRIEND, "av:done")
    await h.press(FRIEND, "tm:any")
    assert h.s.requests == [] and Request.from_json(h.session(FRIEND)["pending_req"]).coop
    await h.press(FRIEND, "rg:ru")
    assert len(h.s.requests) == 1 and h.last_request()[1].coop and h.last_request()[1].mood == "coop"


@scenario
async def test_banner_only_on_the_first_round(h):
    """The selection banner opens a request; «Ещё 3», a refinement or a written correction post just three
    cards and the panel under them."""
    m = h.mark()
    await results_for(h, FRIEND)
    names = [c.photo.filename for c in h.photos(FRIEND, m)]
    assert names == ["selection.jpg", "5.jpg", "4.jpg", "7.jpg"], names
    for step in ("more", "rf:story", "text"):
        m = h.mark()
        if step == "text":
            await h.send(FRIEND, "без хоррора")
        else:
            await h.press(FRIEND, step)
        photos = h.photos(FRIEND, m)
        assert len(photos) in (1, 3) and "selection.jpg" not in [c.photo.filename for c in photos], step
        p = h.check_panel(FRIEND)                                  # the panel is below the new cards
        assert "Подкрутить?" in p.caption and h.pbuttons(FRIEND) == RESULTS, step
    assert len(h.session(FRIEND)["seen"]) == 10
    # a new request opens with a banner again
    await h.send(FRIEND, "/find")
    m = h.mark()
    await h.press(FRIEND, "q:gems")
    assert [c.photo.filename for c in h.photos(FRIEND, m)][0] == "selection.jpg"
    assert len(h.photos(FRIEND, m)) == 4 and h.last_request()[2] == set()


@scenario
async def test_typed_game_under_results_is_a_new_request(h):
    """Under results, a message naming a game is a NEW request unless it starts with a relative word
    («а», «и», «но», «только», «лучше», «можно», «без», «по», «чуть», «ещё», «не»): then it corrects."""
    await results_for(h, FRIEND)                                # «на вечер», seen 5, 4, 7
    m = h.mark()
    await h.send(FRIEND, "что-нибудь как Hollow Knight")
    assert h.saw(FRIEND, "Учитываю поправку", m)      # read once: the same reading starts the new request
    req = h.req(FRIEND)
    assert req.seeds == ["Hollow Knight"] and req.mood == "any" and req.max_hours is None   # nothing kept
    assert h.session(FRIEND)["seen"] == [] and h.session(FRIEND)["shown"] == []
    assert "Чем именно зацепила Hollow Knight?" in h.check_panel(FRIEND).caption and h.state(FRIEND) == "wizard"
    assert len(h.s.requests) == 1

    for said in ("а как Hollow Knight?", "лучше как Celeste", "и как Hollow Knight", "можно как Celeste",
                 "Но как Celeste", "ещё как Hollow Knight"):
        await results_for(h, FRIEND)
        m = h.mark()
        await h.send(FRIEND, said)
        assert h.session(FRIEND)["seen"], said                                  # not a fresh start
        req = h.req(FRIEND)
        assert req.mood == "evening" and req.max_hours == 4 and req.seeds, (said, req)   # the request goes on
        assert h.state(FRIEND) == "wizard", said

    # a word that only starts like a relative one is not one («игры» is not «и»)
    await results_for(h, FRIEND)
    m = h.mark()
    await h.send(FRIEND, "игры типа Hollow Knight")
    assert h.req(FRIEND).mood == "any" and h.session(FRIEND)["seen"] == []
    # with no game named it is a correction as before
    await results_for(h, FRIEND)
    m = h.mark()
    await h.send(FRIEND, "поспокойнее")
    assert h.saw(FRIEND, "Учитываю поправку", m) and h.last_request()[2] == {5, 4, 7}


def test_card_caption_texts():
    """A card shows how sure it is (⚪ while only tags were read) and quotes a player with their hours."""
    from gamefinder import views
    g = {"appid": 5, "name": "Dead Cells"}
    tags_only = passport("Dead Cells")                          # no "_source": built from tags
    assert views.confidence_line(tags_only) == "⚪ <i>Предварительно: по тегам, отзывы ещё не читал</i>"
    assert views.confidence_line({"_source": "llm", "_reviews_used": 30}) == "🟢 <i>Разбор по 30 свежим отзывам</i>"
    pick = Pick(appid=5, score=0.8, parts={}, passport=tags_only, passport_real=False,
                evidence=[{"text": "Залип & не жалею", "hours": 43.2}])
    cap = views.pick_caption(g, pick, None)
    assert cap.split("\n")[1].startswith("⚪") and "<i>Игрок, 43 ч в игре:</i> «Залип &amp; не жалею»" in cap, cap
    assert not html_problems(cap)
    pick.evidence = [{"text": "без часов"}]
    assert "<i>Игрок:</i> «без часов»" in views.pick_caption(g, pick, None)


def test_player_hours_round_half_up():
    """42.5 h in the game reads «43 ч» (the scenarios' fake evidence has 42.5 h)."""
    from gamefinder import views
    pick = Pick(appid=5, score=0.8, parts={}, passport=passport("Dead Cells"), passport_real=False,
                evidence=[{"text": "Залип", "hours": 42.5}])
    cap = views.pick_caption({"appid": 5, "name": "Dead Cells"}, pick, None)
    assert "<i>Игрок, 43 ч в игре:</i>" in cap, cap



@scenario
async def test_cards_become_animations(h):
    """Each card picture is swapped for its animated loop once encoded; buttons and caption stay."""
    import dataclasses
    h.app.cfg = dataclasses.replace(h.app.cfg, animate_cards=True)
    await h.send(FRIEND, "/start")
    await h.press(FRIEND, "q:evening", message=h.panel(FRIEND))
    await asyncio.gather(*list(h.app.tasks))
    edits = [c for c in h.tg.sent("EditMessageMedia") if (c.chat_id, c.message_id) in h.tg.card_ids]
    assert len(edits) == 3, len(edits)
    for c in edits:
        assert c.media.width == 960 and c.media.height == 540 and c.reply_markup.inline_keyboard[0][0].callback_data.startswith("pp:")


@scenario
async def test_different_avoids_what_the_shown_games_share(h):
    """«Совсем другое» must move away from the games on screen: their shared tags are avoided."""
    await results_for(h, FRIEND)                                # Dead Cells, Ratchet & Clank, Dead Space
    await h.press(FRIEND, "rf:different")
    _, req, seen = h.last_request()
    assert req.diversify and "Sci-fi" in req.tags_avoid, req.tags_avoid
    assert seen == {5, 4, 7}


@scenario
async def test_failure_while_sending_cards_offers_retry(h):
    """A search that worked but whose cards could not go out ends on the retry panel, not mid-way."""
    await h.send(FRIEND, "/start")

    async def broken(appids, cc):
        raise OSError("store is down")

    h.s.prices = broken
    await h.press(FRIEND, "q:story")
    h.errors.clear()
    assert h.pbuttons(FRIEND) == ["retry", "home"] and h.session(FRIEND)["step"] == "failed"
    assert not h.app.running and not h.app.busy


@scenario
async def test_text_while_the_previous_one_is_read_waits_its_turn(h):
    """A second message typed while the first is still being looked up is applied after it, on top
    of where the first one led (here: the answer to «Чем именно зацепила»), not to an older request."""
    gate = asyncio.Event()
    real = h.s.aspect_options

    async def slow(title):
        await gate.wait()
        return await real(title)

    h.s.aspect_options = slow
    await h.send(FRIEND, "/start")
    first = asyncio.create_task(h.send(FRIEND, "как Hollow Knight"))
    await until(lambda: FRIEND in h.app.busy and h.s.asked == [])
    await h.send(FRIEND, "атмосфера")
    assert h.session(FRIEND)["pending"] == "атмосфера" and not h.s.requests
    gate.set()
    await first
    assert h.session(FRIEND).get("pending") is None and not h.app.busy
    assert h.session(FRIEND)["step"] == "avoid", h.session(FRIEND)["step"]   # «атмосфера» answered the question
    assert h.s.asked == ["Hollow Knight"] and not h.s.requests


@scenario
async def test_home_drops_a_correction_typed_during_a_search(h):
    h.s.hold = asyncio.Event()
    await h.send(FRIEND, "/start")
    first = asyncio.create_task(h.press(FRIEND, "q:evening"))
    await until(lambda: h.s.requests)
    h.s.fail = {"empty"}
    await h.send(FRIEND, "без хоррора")
    h.s.hold.set()
    await first
    # nothing found: the correction still applies to it (a second search, with the exclusion)
    assert len(h.s.requests) == 2 and "Horror" in h.s.requests[-1][1].tags_avoid
    h.s.fail = set()
    h.s.hold = asyncio.Event()
    first = asyncio.create_task(h.press(FRIEND, "q:chill", message=old_message(FRIEND)))
    await until(lambda: len(h.s.requests) == 3)
    h.s.fail = {"recommend"}
    await h.send(FRIEND, "покороче")
    h.s.hold.set()
    await first
    h.errors.clear()
    # failed: the correction is dropped with the search and never fires later
    assert len(h.s.requests) == 3 and h.session(FRIEND).get("pending") is None
    h.s.fail = set()
    await h.press(FRIEND, "home")
    await h.press(FRIEND, "q:evening")
    assert len(h.s.requests) == 4


def main():
    logging.basicConfig(level=logging.CRITICAL)
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            result = fn()
            if inspect.iscoroutine(result):
                asyncio.run(result)
            print(f"ok   {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
