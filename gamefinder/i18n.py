"""The player's language: English (the default) or Russian.

The language of the update being handled lives in a context variable: the bot's middleware sets it
from the player's profile, and everything below (texts, cards, prompts asking for a reason "in
English") reads it with tr() instead of taking a parameter through every call. asyncio tasks and
asyncio.to_thread copy the context, so card animations and renders started from a handler keep it.

Background work (the catalog worker reading reviews) runs outside any update and sees the default.
Shared caches are written in Russian on purpose (game passports), whatever the context says, and
translated for English players when shown (service.localize).
"""

from contextvars import ContextVar

LANGS = ("en", "ru")
DEFAULT = "en"
LANG_NAMES = {"en": "🇬🇧 English", "ru": "🇷🇺 Русский"}
PROMPT_NAMES = {"en": "English", "ru": "Russian"}

_lang: ContextVar[str] = ContextVar("lang", default=DEFAULT)


def norm(code: str | None) -> str:
    return code if code in LANGS else DEFAULT


def set_lang(code: str | None) -> None:
    _lang.set(norm(code))


def lang() -> str:
    return _lang.get()


def is_ru() -> bool:
    return _lang.get() == "ru"


def tr(en: str, ru: str) -> str:
    """The text in the current language."""
    return ru if _lang.get() == "ru" else en


def pick(pair) -> str:
    """A label kept as (en, ru), or a plain string (the same in both)."""
    if isinstance(pair, (tuple, list)) and len(pair) == 2:
        return tr(pair[0], pair[1])
    return pair


def prompt_lang() -> str:
    """The language name for an LLM prompt: "Write the reason in {prompt_lang()}"."""
    return PROMPT_NAMES[_lang.get()]


def hours(n) -> str:
    """'~22 h' / '~22 ч'."""
    return f"{n} {tr('h', 'ч')}"


def plural(n: int, en: tuple[str, str], ru: tuple[str, str, str]) -> str:
    """The word form for n: ('review', 'reviews') or ('отзыв', 'отзыва', 'отзывов')."""
    if _lang.get() == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return ru[0]
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return ru[1]
        return ru[2]
    return en[0] if n == 1 else en[1]


def thousands(n: int) -> str:
    """'112 834' in Russian, '112,834' in English."""
    s = f"{int(n):,}"
    return s.replace(",", " ") if _lang.get() == "ru" else s


class using:
    """with using("ru"): ... -- another language for a moment (a message to someone else, like the
    owner told about a newcomer), the current one back afterwards."""

    def __init__(self, code: str | None):
        self.code = norm(code)

    def __enter__(self):
        self.token = _lang.set(self.code)
        return self

    def __exit__(self, *exc):
        _lang.reset(self.token)
        return False
