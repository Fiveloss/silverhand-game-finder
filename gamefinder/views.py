"""What the image cards show (render.py draws them) and the short captions under them.

The picture carries the numbers: match, real genres, feel, review freshness, length, price.
The caption carries the words: why this game for this request, a player's quote, warnings.
Captions stay short (Telegram cuts photo captions at 1024 characters). Everything is in the
player's language (i18n); the passports shown here are already localized (service.localize).
"""

import re
from html import escape

from .analyst import AXES, axis_ends
from .i18n import hours as hours_text, is_ru, thousands, tr
from .recommender import Pick
from .sources.deck import deck_label
from .texts import MARK, axis_label, card, pct, price, share_word

# Axes worth showing on a recommendation, in this order, when nothing in the request stands out.
DEFAULT_FEEL = ["pace", "difficulty", "story", "exploration", "combat", "length"]
_CYRILLIC = re.compile(r"[а-яё]", re.I)


def _feel(p: dict, axes: list[str]) -> list[tuple[str, int, str, str]]:
    return [(axis_label(k).capitalize(), int(p["feel"][k]), *axis_ends(k)) for k in axes]


def ru_tag(tag: str) -> str:
    """A Steam tag as the player reads it: the Russian word when the vocabulary knows it, the tag
    itself (Steam tags are English) for English players."""
    if not is_ru():
        return tag
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
        return tr("Free", "Бесплатно")
    if p["state"] == "unavailable":
        return tr("Not sold in your region", "Нет в продаже в регионе")
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
        return (tr(f"🟢 <i>Read from {n} recent reviews</i>", f"🟢 <i>Разбор по {n} свежим отзывам</i>") if n
                else tr("🟢 <i>Read from recent reviews</i>", "🟢 <i>Разбор по свежим отзывам</i>"))
    return tr("⚪ <i>Preliminary: from tags, reviews not read yet</i>",
              "⚪ <i>Предварительно: по тегам, отзывы ещё не читал</i>")


def pick_view(g: dict, pick: Pick, stats: dict | None, rank: int, req_axes: dict | None = None,
              price_now: dict | None = None) -> dict:
    p = pick.passport
    recent = None
    if stats and stats.get("recent_share") is not None:
        recent = pct(stats["recent_share"])
    elif g.get("positive"):
        recent = pct(g["positive"] / max(1, g["positive"] + g["negative"]))
    if stats and stats.get("median_hours_positive"):
        hours = "~" + hours_text(max(1, round(stats["median_hours_positive"])))
    else:
        hours = p.get("hours_typical") or ""
    if len(hours) > 14:
        hours = re.split(r" у | for |,", hours)[0][:14]
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
        "praise": [(x["point"], share_word(x.get("share"))) for x in p["praise"][:5]],
        "complaints": [(x["point"], share_word(x.get("share")), x["kind"] == "taste")
                       for x in p["complaints"][:6]],
        "store_genres": list(g.get("genres") or [])[:4],
        "engaged": (tr(f"{pct(stats['engaged_share'])} of players with 10+ h are happy",
                       f"{pct(stats['engaged_share'])} наигравших 10+ ч довольны")
                    if stats and stats.get("engaged_share") is not None and stats.get("engaged_n", 0) >= 10 else ""),
        "state_now": p.get("state_now", ""),
        "summary": p.get("summary", ""),
    })
    return view


def evidence_for_player(evidence: list[dict]) -> list[dict]:
    """Player quotes an English player can read: Russian reviews are left out for them."""
    if is_ru():
        return list(evidence or [])
    return [e for e in evidence or [] if not _CYRILLIC.search(e.get("text") or "")]


def pick_caption(g: dict, pick: Pick, liked_name: str | None) -> str:
    lines = [f"{MARK} <b>{escape(g['name'])}</b>", confidence_line(pick.passport)]
    if pick.relaxed:
        lines.append(tr("<i>A compromise: few exact matches, this is the closest</i>",
                        "<i>Компромисс: точных совпадений мало, это ближайшее</i>"))
    why = []
    if pick.judge_reason:
        why.append(escape(pick.judge_reason))
        if pick.judge_risk:
            why.append(f"<i>{tr('Mind:', 'Учти:')}</i> {escape(pick.judge_risk)}")
    elif pick.suggest_why:
        why.append(escape(pick.suggest_why))
    else:
        if liked_name:
            why.append(tr(f"Feels close to <b>{escape(liked_name)}</b>.",
                          f"По ощущениям близко к <b>{escape(liked_name)}</b>."))
        for point, good in pick.taste_notes[:1]:
            why.append(tr(f"Criticised for “{escape(point)}”, but for your request that is rather a plus.",
                          f"Ругают за «{escape(point)}», но под твой запрос это скорее плюс.") if good
                       else tr(f"<i>Mind:</i> criticised for “{escape(point)}”.",
                               f"<i>Учти:</i> ругают за «{escape(point)}»."))
        praise = [escape(x["point"]) for x in pick.passport["praise"][:2]]
        if praise:
            why.append(tr("Praised for: ", "Хвалят: ") + ", ".join(praise) + ".")
    reasons = why_lines(pick, liked_name)
    quotes = evidence_for_player(pick.evidence)
    if reasons:
        why += ["", tr("🧠 <b>Why it is here</b>", "🧠 <b>Почему в подборке</b>")] + reasons + ([""] if quotes else [])
    for e in quotes[:1]:
        h = int(e["hours"] + 0.5) if e.get("hours") else 0
        who = tr(f"A player, {h} h in the game", f"Игрок, {h} ч в игре") if h else tr("A player", "Игрок")
        why.append(f"<i>{who}:</i> " + tr(f"“{escape(e['text'][:220])}”", f"«{escape(e['text'][:220])}»"))
    if pick.warnings:
        why.append("⚠️ " + "; ".join(escape(w) for w in pick.warnings[:2]))
    if why:
        lines += [""] + why
    return _fit("\n".join(lines))


def why_lines(pick: Pick, liked_name: str | None) -> list[str]:
    """How the game got here, signal by signal: the shared genre, the model that proposed it,
    the players who play both, how close the tags are."""
    from . import genres
    out = []
    ref = tr(f" with <b>{escape(liked_name)}</b>", f" с <b>{escape(liked_name)}</b>") if liked_name else ""
    if pick.genres:
        out.append(tr(f"• Same genre{ref}: ", f"• Общий жанр{ref}: ")
                   + ", ".join(escape(genres.ru(f)) for f in pick.genres[:3]))
    if pick.hooks_hit:
        from .aspects import aspect_name
        names = [aspect_name(h).lower() for h in pick.hooks_hit if aspect_name(h)]
        model = f"<b>{escape(liked_name)}</b>" if liked_name else tr("the reference", "образец")
        out.append(tr(f"• Strong at what people love {model} for: ", f"• Сильна в том же, за что любят {model}: ")
                   + ", ".join(escape(n) for n in names))
    if pick.format:
        out.append(tr("• Same format: ", "• Тот же формат: ") + escape(genres.format_ru(pick.format)))
    tags = pick.parts.get("tags")
    if tags is not None and tags >= 0.5:
        out.append(tr(f"• Player tags match by {round(tags * 100)}%", f"• Теги игроков совпадают на {round(tags * 100)}%"))
    if "scout" in pick.parts:
        line = tr("• Proposed by the AI for your request", "• Предложила нейросеть по твоему запросу")
        if pick.suggest_why and pick.judge_reason:
            line += tr(f": “{escape(pick.suggest_why[:160])}”", f": «{escape(pick.suggest_why[:160])}»")
        out.append(line)
    co = pick.parts.get("coplay")
    if co is not None and co >= 0.3:
        who = (tr(f"fans of <b>{escape(liked_name)}</b>", f"фанаты <b>{escape(liked_name)}</b>") if liked_name
               else tr("players with similar taste", "игроки с похожим вкусом"))
        out.append(tr(f"• Played a lot by {who} (public Steam libraries)",
                      f"• В неё много играют {who} (по открытым библиотекам Steam)"))
    exp = pick.parts.get("experience")
    if exp is not None and exp >= 0.6:
        out.append(tr("• Player reviews are close to your request in meaning",
                      "• Отзывы игроков по смыслу близки к твоему запросу"))
    if pick.judge_reason:
        sure = tr(f": confidence {pick.judge_fit}/10", f": уверенность {pick.judge_fit}/10") if pick.judge_fit else ""
        out.append(tr(f"• The AI judge picked it out of 12 candidates{sure}",
                      f"• Нейросеть-судья выбрала её из 12 кандидатов{sure}"))
    return out


def game_caption(g: dict, p: dict, stats: dict | None = None) -> str:
    lines = [f"{MARK} <b>{escape(g['name'])}</b>", confidence_line(p)]
    gog = (stats or {}).get("gog") or {}
    if gog.get("rating"):
        count = tr(f" from {thousands(gog['count'])}", f" из {thousands(gog['count'])}") if gog.get("count") else ""
        link = f'<a href="{escape(gog["url"])}">GOG</a>' if gog.get("url") else "GOG"
        lines.append(tr(f"On {link}: <b>{gog['rating']}★</b>{count} ratings",
                        f"На {link}: <b>{gog['rating']}★</b>{count} оценок"))
    if p.get("core_loop"):
        lines.append(f"<i>{escape(p['core_loop'])}</i>")
    if p.get("best_for"):
        lines += ["", f"<b>{tr('Get it if', 'Зайдёт, если')}</b> {escape(_lower_first(p['best_for']))}"]
    if p.get("avoid_if"):
        lines.append(f"<b>{tr('Skip it if', 'Не бери, если')}</b> {escape(_lower_first(p['avoid_if']))}")
    if p.get("compared_to"):
        lines.append(f"<b>{tr('Compared to:', 'Сравнивают с:')}</b> " + escape(", ".join(p["compared_to"][:4])))
    return _fit("\n".join(lines))


def selection_caption(label: str, n: int, seeds: list[str], missing: list[str] | None = None) -> str:
    lead = escape(label) if label else tr("By recent player reviews", "По свежим отзывам игроков")
    note = tr(f"Found <b>{n}</b>, the closest match first. You can fine-tune it at the very bottom.",
              f"Нашёл <b>{n}</b>, самое точное совпадение первым. Подкрутить можно в самом низу.")
    if seeds:
        note = tr(f"Starting from: <b>{escape(', '.join(seeds))}</b>.\n",
                  f"Отталкиваюсь от: <b>{escape(', '.join(seeds))}</b>.\n") + note
    if missing:
        note = tr(f"Couldn't find <b>{escape(', '.join(missing))}</b> on Steam, so I'm picking by the rest of the "
                  "request. If it's a typo, send the title again.\n\n",
                  f"Не нашёл <b>{escape(', '.join(missing))}</b> в Steam, поэтому подбираю по остальному запросу. "
                  "Если это опечатка, напиши название ещё раз.\n\n") + note
    return card(tr("Your picks", "Подборка"), lead=lead, note=note)


def stage_text(what: str, label: str) -> str:
    return card(escape(what), lead=escape(label) if label else "",
                note=tr("Searching by player tags, by what people with similar taste play, and by recent "
                        "reviews. Usually under a minute.",
                        "Ищу по тегам игроков, по тому, во что залипают люди с похожим вкусом, и по свежим "
                        "отзывам. Обычно это меньше минуты."))


def reading(i: int, n: int, label: str) -> str:
    return card(tr("Reading recent reviews", "Читаю свежие отзывы"), lead=escape(label) if label else "",
                note=tr(f"Candidate {min(i + 1, n)} of {n}: checking what players say now, not at launch.",
                        f"Кандидат {min(i + 1, n)} из {n}: проверяю, что игроки пишут сейчас, а не на релизе."))


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


def _fit(text: str, limit: int = 1000) -> str:
    """Photo captions are cut at 1024 characters of text; drop whole lines from the end."""
    while len(_plain(text)) > limit and "\n" in text:
        text = text.rsplit("\n", 1)[0]
    return text


def _plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)
