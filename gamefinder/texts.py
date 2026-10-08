"""Shared message helpers in the SILVERHAND house style, and the text fallback of a game breakdown.

Same rules as the other SILVERHAND bots: a bold title after the accent mark (🟠 here, the
bot's amber), an italic lead, data as "Name — <b>value</b>" lines without bullet markers,
no <blockquote> (Telegram paints its bar in the profile colour, which a bot cannot change).
"""

from html import escape

from .sources.deck import deck_label
from .taste import describe_axis

MARK = "🟠"
SHARE_RU = {"most": "почти все", "many": "многие", "some": "некоторые"}


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
        return "бесплатно"
    if g.get("price_cents") is None or not g.get("currency"):
        return ""
    value = g["price_cents"] / 100
    s = f"{value:,.0f}".replace(",", " ") if value >= 100 else f"{value:.2f}"
    sale = f" (−{g['discount']}%)" if g.get("discount") else ""
    return f"{s} {g['currency']}{sale}"


def store_url(appid: int) -> str:
    return f"https://store.steampowered.com/app/{appid}/"


AXIS_LABEL = {
    "pace": "темп", "difficulty": "сложность", "story": "сюжет", "freedom": "свобода",
    "complexity": "глубина механик", "grind": "гринд", "tension": "напряжение", "combat": "бои",
    "exploration": "исследование", "social": "кооператив", "length": "длина", "replay": "реиграбельность",
}


def passport_card(g: dict, p: dict, stats: dict | None) -> str:
    rows = []
    if p["real_genres"]:
        rows.append(("Жанр на деле", escape(", ".join(p["real_genres"]))))
    if g.get("genres"):
        rows.append(("Жанр в магазине", escape(", ".join(g["genres"]))))
    if stats:
        rows.append(("Отзывы за всё время", f"{pct(stats.get('all_share'))} из {stats.get('total', 0):,}".replace(",", " ")))
        if stats.get("recent_share") is not None:
            span = f" за {stats['recent_span_days']} дн." if stats.get("recent_span_days") else ""
            rows.append(("Свежие отзывы", f"{pct(stats['recent_share'])}{span}"))
        if stats.get("engaged_share") is not None and stats.get("engaged_n", 0) >= 10:
            rows.append(("Кто наиграл 10+ ч", f"{pct(stats['engaged_share'])} довольны"))
        if stats.get("quick_negative_share") is not None:
            rows.append(("Негатив до 2 ч игры", pct(stats["quick_negative_share"])))
    if p.get("hours_typical"):
        rows.append(("Длина", escape(p["hours_typical"])))
    lang = "озвучка и текст" if g.get("ru_audio") else "текст" if g.get("ru_text") else "нет"
    if g.get("store_ok") == 1:
        rows.append(("Русский", lang))
    deck = deck_label(g.get("deck", 0), g.get("proton", ""))
    if deck:
        rows.append(("Совместимость", escape(deck)))
    if price(g):
        rows.append(("Цена", price(g)))

    blocks = []
    if p.get("summary"):
        blocks.append(escape(p["summary"]))
    if p.get("core_loop"):
        blocks.append(f"<b>Что делаешь:</b> {escape(p['core_loop'])}")
    feel = "\n".join(f"{AXIS_LABEL[k].capitalize()} — <b>{describe_axis(k, v)}</b> ({v}/10)"
                     for k, v in p["feel"].items())
    blocks.append(f"<b>Ощущения по отзывам</b>\n{feel}")
    if p["praise"]:
        blocks.append("<b>Хвалят</b>\n" + "\n".join(
            f"{escape(x['point'])} — <i>{SHARE_RU.get(x['share'], '')}</i>" for x in p["praise"]))
    if p["complaints"]:
        blocks.append("<b>Ругают</b>\n" + "\n".join(
            f"{escape(x['point'])} — <i>{SHARE_RU.get(x['share'], '')}</i>"
            + (" (дело вкуса)" if x["kind"] == "taste" else "") for x in p["complaints"]))
    if p.get("state_now"):
        blocks.append(f"<b>Сейчас:</b> {escape(p['state_now'])}")
    if p.get("best_for"):
        blocks.append(f"<b>Зайдёт, если</b> {escape(_lower_first(p['best_for']))}")
    if p.get("avoid_if"):
        blocks.append(f"<b>Не бери, если</b> {escape(_lower_first(p['avoid_if']))}")
    if p.get("compared_to"):
        blocks.append("<b>Сравнивают с:</b> " + escape(", ".join(p["compared_to"][:5])))
    src = (f"Разбор по {p.get('_reviews_used', 0)} отзывам" if p.get("_source") == "llm"
           else "Быстрая оценка по тегам и ключевым словам в отзывах")
    return card(escape(g["name"]), rows, note="\n\n".join(blocks), foot=src)


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


