"""The bot's profile texts, set by the bot itself on every start: the description (an empty chat,
under the picture, before the first /start) and the about text (the profile and link previews).

English is the default Telegram shows everyone; the Russian texts go to people whose Telegram app
is in Russian. Limits are counted in UTF-16 units: 512 for the description, 120 for the about text.
"""

import logging

log = logging.getLogger(__name__)

DESCRIPTION = {
    "en": ("I help you pick a game to play right now, by fresh player reviews, not ads.\n\n"
           "Tell me in your own words: “like Hollow Knight, but easier, for a couple of evenings” or "
           "“co-op with a friend, not a shooter”. I'll read the Steam and GOG reviews "
           "and send three games as cards: the actual genre, length, price, what players praise and "
           "criticise now.\n\n"
           "Fine-tune with buttons: shorter, easier, more. No questionnaires."),
    "ru": ("Помогаю выбрать, во что поиграть прямо сейчас, по свежим отзывам игроков, а не по рекламе.\n\n"
           "Напиши своими словами: «как Hollow Knight, но проще, на пару вечеров» или «кооп с другом, не шутер». "
           "Спрошу, чем зацепила игра, прочитаю отзывы в Steam и GOG и пришлю три игры карточками: жанр на деле, "
           "длина, цена, за что хвалят и ругают сейчас.\n\n"
           "Подкрутишь кнопками: короче, проще, ещё. Анкет нет, каждый запрос с нуля."),
}
PRIVATE = {
    "en": "\n\nThe bot is private: after /start the owner gets a request and can let you in.",
    "ru": "\n\nБот приватный: после /start владелец получит запрос и сможет тебя пустить.",
}
ABOUT = {
    "en": "What to play right now: tell me in your own words, I'll pick three games by fresh Steam and GOG reviews.",
    "ru": "Во что поиграть прямо сейчас: напиши своими словами, подберу три игры по свежим отзывам Steam и GOG.",
}


def utf16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def description(lang: str, open_access: bool) -> str:
    return DESCRIPTION[lang] + ("" if open_access else PRIVATE[lang])


async def apply(bot, open_access: bool) -> None:
    """Sets the description and the about text: English for everyone, Russian for Russian apps."""
    for lang, code in (("en", None), ("ru", "ru")):
        try:
            await bot.set_my_description(description(lang, open_access), language_code=code)
            await bot.set_my_short_description(ABOUT[lang], language_code=code)
        except Exception as e:
            log.warning("profile texts (%s): %s", lang, e)
