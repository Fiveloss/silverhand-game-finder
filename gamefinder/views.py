"""What the image cards show (render.py draws them) and the short captions under them.

The picture carries the numbers: match, real genres, feel, review freshness, length, price.
The caption carries the words: why this game for this request, a player's quote, warnings.
Captions stay short (Telegram cuts photo captions at 1024 characters).
"""

import re
from html import escape

from .analyst import AXES
from .recommender import Pick
from .sources.deck import deck_label
from .texts import AXIS_LABEL, MARK, SHARE_RU, card, pct, price

# Axes worth showing on a recommendation, in this order, when nothing in the request stands out.
DEFAULT_FEEL = ["pace", "difficulty", "story", "exploration", "combat", "length"]


def _feel(p: dict, axes: list[str]) -> list[tuple[str, int, str, str]]:
    return [(AXIS_LABEL[k].capitalize(), int(p["feel"][k]), *AXES[k]) for k in axes]


def ru_tag(tag: str) -> str:
    """A Steam tag in Russian when the vocabulary knows it (heuristic passports carry English tags)."""
    from .intent import TAG_RU
    return TAG_RU.get(tag, tag)


def pick_axes(pick: Pick, req_axes: dict | None = None) -> list[str]:
    """Four axes for the card: the ones the request asked about, then the ones that match, then defaults."""
    out: list[str] = []
    for k in list(req_axes or {}) + list(pick.feel_matches) + DEFAULT_FEEL:
        if k in AXES and k not in out:
            out.append(k)
    return out[:4]


def region_price(p: dict | None, g: dict) -> str:
    """The price text for the player's region; the catalog's own price only when Steam did not answer."""
    if not p:
        return price(g)
    if p["state"] == "free":
        return "Бесплатно"
    if p["state"] == "unavailable":
        return "Нет в продаже в регионе"
    text = (p.get("final_formatted") or "").replace(" USD", "").strip()
    if p.get("discount"):
        text += f" (−{p['discount']}%)"
    return text


def confidence(passport: dict) -> tuple[str, int]:
    """("reviews", N) when a model read N reviews for the passport, else ("tags", 0)."""
    if passport.get("_source") == "llm":
        return "reviews", int(passport.get("_reviews_used") or 0)
    return "tags", 0


def confidence_line(passport: dict) -> str:
    kind, n = confidence(passport)
    if kind == "reviews":
        return f"🟢 <i>Разбор по {n} свежим отзывам</i>" if n else "🟢 <i>Разбор по свежим отзывам</i>"
    return "⚪ <i>Предварительно: по тегам, отзывы ещё не читал</i>"


def pick_view(g: dict, pick: Pick, stats: dict | None, rank: int, req_axes: dict | None = None,
              price_now: dict | None = None) -> dict:
    p = pick.passport
    recent = None
    if stats and stats.get("recent_share") is not None:
        recent = pct(stats["recent_share"])
    elif g.get("positive"):
        recent = pct(g["positive"] / max(1, g["positive"] + g["negative"]))
    if stats and stats.get("median_hours_positive"):
        hours = f"~{max(1, round(stats['median_hours_positive']))} ч"
    else:
        hours = p.get("hours_typical") or ""
    if len(hours) > 14:
        hours = hours.split(" у ")[0].split(",")[0][:14]
    return {
        "rank": rank,
        "name": g["name"],
        "match": max(0.0, min(1.0, pick.score)),
        "genres": [ru_tag(x) for x in (p.get("real_genres") or g.get("genres") or [])][:3],
        "feel": _feel(p, pick_axes(pick, req_axes)),
        "recent": recent,
        "reviews_total": (stats or {}).get("total") or g.get("positive", 0) + g.get("negative", 0),
        "hours": hours,
        "price": region_price(price_now, g),
        "confidence": confidence(p)[0],
        "reviews_used": confidence(p)[1],
        "deck": deck_label(g.get("deck", 0), g.get("proton", "")),
        "judge": bool(pick.judge_reason),
        "warning": pick.warnings[0] if pick.warnings else "",
    }


def game_view(g: dict, p: dict, stats: dict | None, price_now: dict | None = None) -> dict:
    view = pick_view(g, Pick(g["appid"], 0.0, {}, p, p.get("_source") == "llm"), stats, 0, price_now=price_now)
    view.update({
        "match": None,
        "feel": _feel(p, list(AXES)),
        "praise": [(x["point"], SHARE_RU.get(x.get("share"), "")) for x in p["praise"][:5]],
        "complaints": [(x["point"], SHARE_RU.get(x.get("share"), ""), x["kind"] == "taste")
                       for x in p["complaints"][:6]],
        "store_genres": list(g.get("genres") or [])[:4],
        "engaged": (f"{pct(stats['engaged_share'])} наигравших 10+ ч довольны"
                    if stats and stats.get("engaged_share") is not None and stats.get("engaged_n", 0) >= 10 else ""),
        "state_now": p.get("state_now", ""),
        "summary": p.get("summary", ""),
    })
    return view


def pick_caption(g: dict, pick: Pick, liked_name: str | None) -> str:
    lines = [f"{MARK} <b>{escape(g['name'])}</b>", confidence_line(pick.passport)]
    if pick.relaxed:
        lines.append("<i>Компромисс: точных совпадений мало, это ближайшее</i>")
    why = []
    if pick.judge_reason:
        why.append(escape(pick.judge_reason))
        if pick.judge_risk:
            why.append(f"<i>Учти:</i> {escape(pick.judge_risk)}")
    elif pick.suggest_why:
        why.append(escape(pick.suggest_why))
    else:
        if liked_name:
            why.append(f"По ощущениям близко к <b>{escape(liked_name)}</b>.")
        for point, good in pick.taste_notes[:1]:
            why.append(f"Ругают за «{escape(point)}», но под твой запрос это скорее плюс." if good
                       else f"<i>Учти:</i> ругают за «{escape(point)}».")
        praise = [escape(x["point"]) for x in pick.passport["praise"][:2]]
        if praise:
            why.append("Хвалят: " + ", ".join(praise) + ".")
    for e in pick.evidence[:1]:
        hours = f", {int(e['hours'] + 0.5)} ч в игре" if e.get("hours") else ""
        why.append(f"<i>Игрок{hours}:</i> «{escape(e['text'][:220])}»")
    if pick.warnings:
        why.append("⚠️ " + "; ".join(escape(w) for w in pick.warnings[:2]))
    if why:
        lines += [""] + why
    return _fit("\n".join(lines))


def game_caption(g: dict, p: dict, stats: dict | None = None) -> str:
    lines = [f"{MARK} <b>{escape(g['name'])}</b>", confidence_line(p)]
    gog = (stats or {}).get("gog") or {}
    if gog.get("rating"):
        count = f" из {gog['count']:,}".replace(",", " ") if gog.get("count") else ""
        link = f'<a href="{escape(gog["url"])}">GOG</a>' if gog.get("url") else "GOG"
        lines.append(f"На {link}: <b>{gog['rating']}★</b>{count} оценок")
    if p.get("core_loop"):
        lines.append(f"<i>{escape(p['core_loop'])}</i>")
    if p.get("best_for"):
        lines += ["", f"<b>Зайдёт, если</b> {escape(_lower_first(p['best_for']))}"]
    if p.get("avoid_if"):
        lines.append(f"<b>Не бери, если</b> {escape(_lower_first(p['avoid_if']))}")
    if p.get("compared_to"):
        lines.append("<b>Сравнивают с:</b> " + escape(", ".join(p["compared_to"][:4])))
    return _fit("\n".join(lines))


def selection_caption(label: str, n: int, seeds: list[str], missing: list[str] | None = None) -> str:
    lead = escape(label) if label else "По свежим отзывам игроков"
    note = f"Нашёл <b>{n}</b>, самое точное совпадение первым. Подкрутить можно в самом низу."
    if seeds:
        note = f"Отталкиваюсь от: <b>{escape(', '.join(seeds))}</b>.\n" + note
    if missing:
        note = (f"Не нашёл <b>{escape(', '.join(missing))}</b> в Steam, поэтому подбираю по остальному запросу. "
                "Если это опечатка, напиши название ещё раз.\n\n" + note)
    return card("Подборка", lead=lead, note=note)

def stage_text(what: str, label: str) -> str:
    return card(escape(what), lead=escape(label) if label else "",
                note="Ищу по тегам игроков, по тому, во что залипают люди с похожим вкусом, и по свежим "
                     "отзывам. Обычно это меньше минуты.")


def reading(i: int, n: int, label: str) -> str:
    return card("Читаю свежие отзывы", lead=escape(label) if label else "",
                note=f"Кандидат {min(i + 1, n)} из {n}: проверяю, что игроки пишут сейчас, а не на релизе.")


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


def _fit(text: str, limit: int = 1000) -> str:
    """Photo captions are cut at 1024 characters of text; drop whole lines from the end."""
    while len(_plain(text)) > limit and "\n" in text:
        text = text.rsplit("\n", 1)[0]
    return text


def _plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)
