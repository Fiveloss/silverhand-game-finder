"""Shared message helpers in the SILVERHAND house style, and the text fallback of a game breakdown.

Same rules as the other SILVERHAND bots: a bold title after the accent mark (🟠 here, the
bot's amber), an italic lead, data as "Name — <b>value</b>" lines without bullet markers,
no <blockquote> (Telegram paints its bar in the profile colour, which a bot cannot change).
Every label is in the player's language (i18n).
"""

from html import escape

from .i18n import is_ru, pick, thousands, tr
from .sources.deck import deck_label
from .taste import describe_axis

MARK = "🟠"
SHARE_RU = {"most": "почти все", "many": "многие", "some": "некоторые"}
SHARE_EN = {"most": "almost everyone", "many": "many", "some": "some"}


def share_word(share: str | None) -> str:
    """How common a praise or a complaint is, in the player's language."""
    return (SHARE_RU if is_ru() else SHARE_EN).get(share or "", "")


def card(title: str, rows=None, note: str = "", foot: str = "", lead: str = "") -> str:
    out = [f"{MARK} <b>{title}</b>"]
    if lead:
        out.append(f"<i>{lead}</i>")
    body = []
    for item in rows or []:
        if isinstance(item, (tuple, list)):
            body.append(f"{item[0]} — <b>{item[1]}</b>")
        elif item:
            body.append(str(item))
    if body:
        out += ["", "\n".join(body)]
    if note:
        out += ["", note]
    if foot:
        out += ["", f"<i>{foot}</i>"]
    return "\n".join(out)


def pct(v) -> str:
    return "—" if v is None else f"{round(v * 100)}%"


def price(g: dict) -> str:
    if g.get("price_cents") == 0:
        return tr("free", "бесплатно")
    if g.get("price_cents") is None or not g.get("currency"):
        return ""
    value = g["price_cents"] / 100
    s = thousands(round(value)) if value >= 100 else f"{value:.2f}"
    sale = f" (−{g['discount']}%)" if g.get("discount") else ""
    return f"{s} {g['currency']}{sale}"


def store_url(appid: int) -> str:
    return f"https://store.steampowered.com/app/{appid}/"


# (English, Russian)
AXIS_LABEL = {
    "pace": ("pace", "темп"), "difficulty": ("difficulty", "сложность"), "story": ("story", "сюжет"),
    "freedom": ("freedom", "свобода"), "complexity": ("depth of mechanics", "глубина механик"),
    "grind": ("grind", "гринд"), "tension": ("tension", "напряжение"), "combat": ("combat", "бои"),
    "exploration": ("exploration", "исследование"), "social": ("co-op", "кооператив"),
    "length": ("length", "длина"), "replay": ("replay value", "реиграбельность"),
}


def axis_label(axis: str) -> str:
    return pick(AXIS_LABEL[axis])


def passport_card(g: dict, p: dict, stats: dict | None) -> str:
    rows = []
    if p["real_genres"]:
        rows.append((tr("Actual genre", "Жанр на деле"), escape(", ".join(p["real_genres"]))))
    if g.get("genres"):
        rows.append((tr("Store genre", "Жанр в магазине"), escape(", ".join(g["genres"]))))
    if stats:
        rows.append((tr("Reviews, all time", "Отзывы за всё время"),
                     tr(f"{pct(stats.get('all_share'))} of {thousands(stats.get('total', 0))}",
                        f"{pct(stats.get('all_share'))} из {thousands(stats.get('total', 0))}")))
        if stats.get("recent_share") is not None:
            days = stats.get("recent_span_days")
            span = tr(f" over {days} days", f" за {days} дн.") if days else ""
            rows.append((tr("Recent reviews", "Свежие отзывы"), f"{pct(stats['recent_share'])}{span}"))
        if stats.get("engaged_share") is not None and stats.get("engaged_n", 0) >= 10:
            rows.append((tr("Players with 10+ h", "Кто наиграл 10+ ч"),
                         tr(f"{pct(stats['engaged_share'])} happy", f"{pct(stats['engaged_share'])} довольны")))
        if stats.get("quick_negative_share") is not None:
            rows.append((tr("Negative within 2 h", "Негатив до 2 ч игры"), pct(stats["quick_negative_share"])))
    if p.get("hours_typical"):
        rows.append((tr("Length", "Длина"), escape(p["hours_typical"])))
    if is_ru() and g.get("store_ok") == 1:
        # The store's localisation facts matter to a Russian player only.
        rows.append(("Русский", "озвучка и текст" if g.get("ru_audio") else "текст" if g.get("ru_text") else "нет"))
    deck = deck_label(g.get("deck", 0), g.get("proton", ""))
    if deck:
        rows.append((tr("Compatibility", "Совместимость"), escape(deck)))
    if price(g):
        rows.append((tr("Price", "Цена"), price(g)))

    blocks = []
    if p.get("summary"):
        blocks.append(escape(p["summary"]))
    if p.get("core_loop"):
        blocks.append(f"<b>{tr('What you do:', 'Что делаешь:')}</b> {escape(p['core_loop'])}")
    feel = "\n".join(f"{axis_label(k).capitalize()} — <b>{describe_axis(k, v)}</b> ({v}/10)"
                     for k, v in p["feel"].items())
    blocks.append(f"<b>{tr('How it feels, by the reviews', 'Ощущения по отзывам')}</b>\n{feel}")
    if p["praise"]:
        blocks.append(f"<b>{tr('Praised for', 'Хвалят')}</b>\n" + "\n".join(
            f"{escape(x['point'])} — <i>{share_word(x['share'])}</i>" for x in p["praise"]))
    if p["complaints"]:
        blocks.append(f"<b>{tr('Criticised for', 'Ругают')}</b>\n" + "\n".join(
            f"{escape(x['point'])} — <i>{share_word(x['share'])}</i>"
            + (tr(" (a matter of taste)", " (дело вкуса)") if x["kind"] == "taste" else "") for x in p["complaints"]))
    if p.get("state_now"):
        blocks.append(f"<b>{tr('Now:', 'Сейчас:')}</b> {escape(p['state_now'])}")
    if p.get("best_for"):
        blocks.append(f"<b>{tr('Get it if', 'Зайдёт, если')}</b> {escape(_lower_first(p['best_for']))}")
    if p.get("avoid_if"):
        blocks.append(f"<b>{tr('Skip it if', 'Не бери, если')}</b> {escape(_lower_first(p['avoid_if']))}")
    if p.get("compared_to"):
        blocks.append(f"<b>{tr('Compared to:', 'Сравнивают с:')}</b> " + escape(", ".join(p["compared_to"][:5])))
    n = p.get("_reviews_used", 0)
    src = (tr(f"Read from {n} reviews", f"Разбор по {n} отзывам") if p.get("_source") == "llm"
           else tr("A quick estimate from tags and review keywords",
                   "Быстрая оценка по тегам и ключевым словам в отзывах"))
    return card(escape(g["name"]), rows, note="\n\n".join(blocks), foot=src)


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s
