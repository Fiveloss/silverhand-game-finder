"""«Чем именно зацепила <игра>?»: what the player loved in a reference game, for this one request.

A reference game is a bundle: Hollow Knight is exploration, atmosphere, hand-drawn art, music,
tight combat AND punishing difficulty. Someone who names it may want only some of that. The bot
offers a few options specific to THAT game (`options`), the player toggles them or answers in
words (`from_text`), and `apply` steers the request towards what they picked:

- the picked aspects' axes become the request's axis targets (only those axes);
- their tags go to `tags_want`, their phrases to `words` (semantic matching);
- `req.focus_axes` lists the axes they care about; the seed's other axes should then barely count
  (taste.for_request: confidence 0.6 for focus axes, ~0.15 for the rest, instead of 0.6 for all);
- `req.mute_tags` lists the seed's tags tied to aspects they did NOT pick ("Difficult",
  "Souls-like" when they only loved the exploration), so the seed's tag profile stops pulling
  towards them (for_request: scale those tags of the seed vector by ~0.25);
- a disliked aspect ("сложность бесила") becomes a LOW axis target and its tags go to `tags_avoid`;
  a lukewarm one ("бои так себе") is simply left out of the focus.

Nothing here touches the network or the database. The request is duck-typed (gamefinder.intent.Request
or anything with the same attributes); `apply` works on a deep copy.
"""

import copy
import hashlib
import re
from dataclasses import asdict, dataclass, fields, replace

from .analyst import AXES

KEY_RE = re.compile(r"^[a-z0-9_]{1,12}$")
MAX_LABEL = 28


@dataclass
class Aspect:
    key: str                    # short stable id for callback data (≤ 12 chars, [a-z0-9_])
    label: str                  # Russian button text ≤ 28 chars
    axes: dict[str, int]        # feel targets this aspect means, e.g. {"exploration": 9}
    tags: list[str]             # Steam tags it pulls
    words: str                  # Russian phrase for semantic matching
    group: str = ""             # canonical group (dedupe, text matching); "" for free-form ones
    polarity: int = 1           # 1 liked, 0 "не в этом дело" (left out), -1 disliked (steer away)


# --- canonical aspects
# group: (label, axes, core tags, alt tags pulled only when the game has them, words, regex over
# normalized Russian/English text used for praise points and free-text answers)
_C = {
    "story": ("Сюжет", {"story": 8}, ["Story Rich"], [],
              "сильный сюжет, история, которая держит до самого конца",
              r"сюжет|истори[яиюей]|\bstory|\bplot|narrat|повествов"),
    "writing": ("Тексты и диалоги", {"story": 9}, [], ["Text-Based", "Visual Novel", "Interactive Fiction"],
                "блестящие тексты и диалоги, которые хочется читать",
                r"текст|диалог|написан|writing|сценари|реплик|\bчитать"),
    "choices": ("Выборы и последствия", {"story": 8, "freedom": 6}, ["Choices Matter"],
                ["Multiple Endings", "Nonlinear"], "выборы, которые по-настоящему меняют историю",
                r"\bвыбор|последстви|концовк|choices|решени\w* (влия|мен)|ветвлен"),
    "lore": ("Лор и тайны", {"story": 6, "exploration": 7}, ["Lore-Rich"], ["Mystery"],
             "загадочный мир с глубоким лором, который собираешь по кусочкам",
             r"\bлор\w*|\blore|\bтайн|загадочн|мифолог|\bmystery|предыстори"),
    "detective": ("Расследование", {"story": 7}, ["Detective"], ["Investigation", "Mystery"],
                  "распутывать дело, собирать улики и делать выводы",
                  r"детектив|расследов|\bулик|detective|investigat|дедукц"),
    "chars": ("Персонажи", {"story": 7}, [], ["Great Characters", "Story Rich"],
              "живые запоминающиеся персонажи", r"персонаж|компаньон|напарник|character|харизм"),
    "emotion": ("Эмоции и драма", {"story": 8}, ["Emotional"], ["Sad", "Drama"],
                "история, которая трогает до глубины души",
                r"эмоци|трогат|до слез|грустн|\bдрам|плакал|emotional|душевн"),
    "ideas": ("Глубокие темы", {"story": 7}, [], ["Philosophical", "Psychological", "Political"],
              "заставляет думать о серьёзных вещах", r"философ|политик|психологи|о жизни|задума|philosoph"),
    "humor": ("Юмор", {}, ["Comedy"], ["Funny", "Dark Humor", "Satire"], "смешно, остроумный юмор",
              r"юмор|смешн|\bшутк|\bшути|ржач|\bугар|funny|humou?r|комеди|сатир|абсурд"),
    "romance": ("Отношения и романы", {"story": 6}, ["Romance"], ["Dating Sim"],
                "отношения с персонажами, романы и дружба",
                r"\bроман(?!ов)|отношени|свидани|romance|dating|\bженит|свадьб|влюб"),
    "explore": ("Исследование мира", {"exploration": 9}, ["Exploration"], ["Metroidvania", "Open World"],
                "исследовать огромный мир, находить тайные места и срезки",
                r"исслед|explor|изуча|секрет|тайник|\bкарт[аыуе]\b|локаци|новые мест|облазить|шорткат|срезк"),
    "atmos": ("Атмосфера", {}, ["Atmospheric"], ["Dark"], "густая атмосфера, в которую погружаешься с головой",
              r"атмосфер|atmospher|погружени|immersi|\bвайб|\bvibe"),
    "visual": ("Визуальный стиль", {}, [],
               ["Beautiful", "Stylized", "Hand-drawn", "Pixel Graphics", "Colorful", "Anime", "Cartoony"],
               "красивый узнаваемый визуальный стиль",
               r"визуал|графи|\bстиль|рисов|красив|\bарт\b|\bart\b|visual|graphic|пиксел|художеств|дизайн"),
    "music": ("Музыка", {}, ["Great Soundtrack"], ["Soundtrack"], "потрясающая музыка и звук",
              r"музык|саундтрек|soundtrack|music|\bost\b|\bзвук|композитор"),
    "setting": ("Сеттинг", {}, [], [], "сам мир и сеттинг игры", r"сеттинг|setting|вселенн"),
    "tension": ("Напряжение и страх", {"tension": 8}, [],
                ["Horror", "Psychological Horror", "Survival Horror", "Thriller"],
                "держит в напряжении, страшно и тревожно",
                r"напряжен|\bстрах|страшн|жутк|\bужас|хоррор|horror|scary|tension|пуга|скример|тревожн"),
    "cozy": ("Уют и спокойствие", {"tension": 2}, ["Relaxing"], ["Cozy", "Wholesome", "Cute", "Casual"],
             "уютно и спокойно, можно расслабиться",
             r"\bуют|спокой|расслаб|релакс|cozy|relax|chill|медитатив|ламповн|умиротвор|без стресса"),
    "combat": ("Боевая система", {"combat": 8}, [],
               ["Combat", "Hack and Slash", "Character Action Game", "Souls-like", "Action"],
               "отточенные бои, в которых приятно драться",
               r"\bбо[йиею]\b|\bбоя\b|\bбоев|сражен|\bдрак|драть|combat|\bfight|\bбитв|\bбосс|стрельб|перестрелк"),
    "challenge": ("Сложность и вызов", {"difficulty": 8}, ["Difficult"],
                  ["Souls-like", "Precision Platformer", "Masocore"],
                  "сложно, но честно: преодолевать настоящий вызов",
                  r"сложн|хардкор|челлендж|\bвызов|difficult|\bhard|challeng|\bsouls|соулс|\bумира|преодол"),
    "easy": ("Без лишней сложности", {"difficulty": 3}, [], ["Casual"], "можно просто играть, не застревая",
             r"\bлегк\w*|\bлегко|казуальн|\beasy|\bcasual"),
    "speed": ("Скорость и драйв", {"pace": 9}, ["Fast-Paced"], ["Action", "Bullet Hell"],
              "быстрый драйвовый темп", r"динамичн|\bдрайв|скорост|\bбыстр|fast|адреналин"),
    "slow": ("Неторопливость", {"pace": 2}, [], ["Turn-Based", "Walking Simulator"],
             "можно никуда не спешить", r"неторопл|медленн|не спеш|размеренн|\bslow"),
    "depth": ("Глубокие механики", {"complexity": 8}, [],
              ["Strategy", "Crafting", "Automation", "RPG", "CRPG", "Grand Strategy"],
              "глубокие системы, в которых интересно разбираться",
              r"механик|систем[аыуе]|глубин|\bбилд|прокачк|навык|ролев\w* систем|mechanic|depth|комбинац"),
    "tactics": ("Тактика", {"complexity": 7, "pace": 3}, ["Tactical"],
                ["Turn-Based Tactics", "Turn-Based Strategy", "Strategy", "Turn-Based Combat"],
                "продумывать каждый ход", r"тактик|tactic|пошагов|turn.based|продумыва|каждый ход"),
    "deck": ("Колодостроение", {"complexity": 7, "replay": 8}, ["Deckbuilding"], ["Card Battler", "Card Game"],
             "собирать колоду и ломать игру синергиями", r"колод|\bdeck|\bcard|карточн|синерги"),
    "puzzle": ("Головоломки", {"complexity": 6}, ["Puzzle"], ["Puzzle Platformer", "Logic"],
               "головоломки, над которыми ломаешь голову", r"головолом|\bпазл|puzzle|загадк|\bлогик"),
    "platform": ("Платформинг", {}, ["Platformer"], ["Precision Platformer", "2D Platformer", "3D Platformer"],
                 "точный приятный платформинг", r"платформ|прыж|прыга|platform|паркур"),
    "stealth": ("Стелс", {}, ["Stealth"], [], "красться и проходить незамеченным",
                r"стелс|stealth|скрытн|незамет|красться"),
    "freedom": ("Свобода", {"freedom": 8}, [], ["Open World", "Sandbox", "Nonlinear", "Immersive Sim"],
                "свобода делать что хочешь и идти куда хочешь",
                r"свобод|песочниц|sandbox|открыт\w* мир|open.world|нелинейн|куда хочешь|как хочешь|freedom"),
    "build": ("Строить и крафтить", {"complexity": 6}, ["Crafting"],
              ["Base Building", "Building", "City Builder", "Automation"],
              "строить своё и крафтить из добытого",
              r"\bстро[июя]|постро|стройк|крафт|craft|\bbuild|\bбаз[ауы]\b|\bзавод|автоматиз"),
    "survival": ("Выживание", {"tension": 6}, ["Survival"], ["Open World Survival Craft"],
                 "выживать, добывать ресурсы и держаться до последнего", r"выжива|survival|ресурс|\bголод"),
    "farm": ("Своя ферма", {"tension": 3}, ["Farming Sim"], ["Agriculture", "Life Sim"],
             "растить свою ферму в своём темпе", r"\bферм|урожа|грядк|огород|\bfarm|выращива"),
    "friends": ("Игра с друзьями", {"social": 8}, ["Co-op"], ["Online Co-Op", "Local Co-Op", "Multiplayer"],
                "весело играть вместе с друзьями",
                r"\bдруз|\bдруг[оау]|кооп|co-?op|вместе|компани[яюей]|multiplayer|мультиплеер"),
    "pvp": ("Соревнование с людьми", {"social": 9, "replay": 7}, ["PvP"], ["Competitive", "Multiplayer"],
            "соревноваться с живыми людьми", r"\bpvp|\bпвп|соревн|competitive|ранкед"),
    "replay": ("Реиграбельность", {"replay": 8}, ["Replay Value"],
               ["Roguelike", "Roguelite", "Action Roguelike", "Procedural Generation"],
               "каждый забег разный, хочется ещё и ещё",
               r"реиграб|replay|\bзабег|рогалик|roguel|каждый раз по.?разному|перепроход"),
    "long": ("Хватает надолго", {"length": 9}, [], ["Open World", "Sandbox"],
             "огромная игра на десятки и сотни часов", r"надолго|сотни час|длинн|много контента|залипнуть"),
    "short": ("Короткая и ёмкая", {"length": 2}, ["Short"], [], "короткая и ёмкая, без воды",
              r"коротк|за вечер|пару вечеров|\bshort"),
}
CANON = {k: re.compile(v[5]) for k, v in _C.items()}

# Player tag -> (group, weight, label override). Weight: how much the tag says about why people love it.
TAG_MAP: dict[str, tuple[str, float, str | None]] = {
    "Atmospheric": ("atmos", 1.0, None), "Dark": ("atmos", 0.6, "Мрачная атмосфера"),
    "Great Soundtrack": ("music", 1.0, None), "Soundtrack": ("music", 0.8, None),
    "Beautiful": ("visual", 0.9, None), "Stylized": ("visual", 0.9, None),
    "Hand-drawn": ("visual", 1.0, "Рисованная графика"), "Pixel Graphics": ("visual", 0.9, "Пиксель-арт"),
    "Colorful": ("visual", 0.5, None), "Anime": ("visual", 0.6, "Аниме-стиль"), "Cartoony": ("visual", 0.5, None),
    "Story Rich": ("story", 1.0, None), "Lore-Rich": ("lore", 1.0, None), "Mystery": ("lore", 0.7, None),
    "Choices Matter": ("choices", 1.0, None), "Multiple Endings": ("choices", 0.8, None),
    "Detective": ("detective", 1.0, None), "Investigation": ("detective", 0.9, None),
    "Text-Based": ("writing", 0.9, None), "Visual Novel": ("writing", 0.8, None),
    "Interactive Fiction": ("writing", 0.8, None),
    "Philosophical": ("ideas", 1.0, None), "Psychological": ("ideas", 0.8, None),
    "Political": ("ideas", 0.8, None),
    "Dark Humor": ("humor", 0.9, "Чёрный юмор"), "Comedy": ("humor", 0.9, None), "Funny": ("humor", 0.7, None),
    "Emotional": ("emotion", 1.0, None), "Sad": ("emotion", 0.8, None),
    "Romance": ("romance", 1.0, None), "Dating Sim": ("romance", 0.8, None),
    "Exploration": ("explore", 1.0, None), "Metroidvania": ("explore", 1.0, None),
    "Open World": ("freedom", 0.9, "Открытый мир"), "Sandbox": ("freedom", 0.9, "Песочница и свобода"),
    "Immersive Sim": ("freedom", 1.0, "Свобода решений"), "Nonlinear": ("freedom", 0.7, None),
    "Difficult": ("challenge", 1.0, None), "Souls-like": ("challenge", 0.9, None),
    "Masocore": ("challenge", 1.0, None),
    "Hack and Slash": ("combat", 0.9, None), "Character Action Game": ("combat", 1.0, None),
    "Combat": ("combat", 0.7, None), "Shooter": ("combat", 0.6, "Стрельба"), "FPS": ("combat", 0.6, "Стрельба"),
    "Horror": ("tension", 1.0, None), "Psychological Horror": ("tension", 1.0, "Психологический ужас"),
    "Survival Horror": ("tension", 1.0, None), "Thriller": ("tension", 0.7, None),
    "Relaxing": ("cozy", 1.0, None), "Cozy": ("cozy", 1.0, None), "Wholesome": ("cozy", 0.8, None),
    "Cute": ("cozy", 0.5, None),
    "Farming Sim": ("farm", 1.0, None), "Agriculture": ("farm", 0.8, None), "Life Sim": ("farm", 0.5, None),
    "Crafting": ("build", 0.8, None), "Base Building": ("build", 0.9, None), "Building": ("build", 0.7, None),
    "City Builder": ("build", 0.9, "Строить свой город"), "Automation": ("build", 1.0, "Автоматизация"),
    "Survival": ("survival", 0.9, None), "Open World Survival Craft": ("survival", 1.0, None),
    "Co-op": ("friends", 1.0, None), "Online Co-Op": ("friends", 0.9, None),
    "Local Co-Op": ("friends", 0.8, None), "Multiplayer": ("friends", 0.4, None),
    "PvP": ("pvp", 1.0, None), "Competitive": ("pvp", 0.9, None),
    "Roguelike": ("replay", 0.9, None), "Roguelite": ("replay", 0.9, None),
    "Action Roguelike": ("replay", 0.9, None), "Procedural Generation": ("replay", 0.6, None),
    "Replay Value": ("replay", 1.0, None),
    "Deckbuilding": ("deck", 1.0, None), "Card Battler": ("deck", 0.9, None),
    "Turn-Based Tactics": ("tactics", 1.0, None), "Tactical": ("tactics", 0.8, None),
    "Turn-Based Strategy": ("tactics", 0.9, None),
    "Grand Strategy": ("depth", 1.0, "Глубокая стратегия"), "Strategy": ("depth", 0.5, None),
    "CRPG": ("depth", 0.7, "Ролевая система"), "RPG": ("depth", 0.4, "Ролевая система"),
    "Puzzle": ("puzzle", 1.0, None), "Puzzle Platformer": ("puzzle", 0.9, None),
    "Platformer": ("platform", 0.6, None), "Precision Platformer": ("platform", 0.9, None),
    "Stealth": ("stealth", 1.0, None),
    "Fast-Paced": ("speed", 1.0, None), "Bullet Hell": ("speed", 0.8, None),
    "Walking Simulator": ("slow", 0.6, None), "Short": ("short", 0.8, None),
    "Cyberpunk": ("setting", 0.9, "Мир киберпанка"), "Dark Fantasy": ("setting", 0.9, "Тёмное фэнтези"),
    "Post-apocalyptic": ("setting", 0.9, "Постапокалипсис"), "Space": ("setting", 0.8, "Космос"),
    "Lovecraftian": ("setting", 0.9, "Лавкрафтовский ужас"), "Sci-fi": ("setting", 0.6, "Научная фантастика"),
    "Steampunk": ("setting", 0.9, "Стимпанк"), "Medieval": ("setting", 0.6, "Средневековье"),
    "Noir": ("setting", 0.9, "Нуар"), "Western": ("setting", 0.9, "Дикий Запад"),
    "Underwater": ("setting", 0.9, "Подводный мир"), "Mythology": ("setting", 0.8, "Мифология"),
    "Vampire": ("setting", 0.8, "Вампиры"), "Historical": ("setting", 0.6, "История"),
    "Zombies": ("setting", 0.5, "Зомби"),
}

# Strong feel axes -> group: (axis, high side?, threshold).
FEEL_RULES = [
    ("story", True, 7, "story"), ("exploration", True, 7, "explore"), ("tension", True, 7, "tension"),
    ("combat", True, 7, "combat"), ("complexity", True, 7, "depth"), ("difficulty", True, 7, "challenge"),
    ("tension", False, 2, "cozy"), ("freedom", True, 7, "freedom"), ("social", True, 7, "friends"),
    ("replay", True, 7, "replay"), ("pace", True, 8, "speed"), ("pace", False, 2, "slow"),
    ("length", True, 9, "long"),
]
# A taste complaint ("слишком сложно") means the trait is real and divisive: worth asking about.
COMPLAINT_GROUPS = {("difficulty", "high"): "challenge", ("tension", "high"): "tension",
                    ("pace", "low"): "slow", ("story", "high"): "writing", ("length", "high"): "long",
                    ("combat", "high"): "combat", ("complexity", "high"): "depth"}
SHARE = {"most": 1.0, "many": 0.6, "some": 0.3}
# Labels that say nothing about why someone loves a game.
JUNK = re.compile(r"^(геймплей|игровой процесс|графика|всё|все|игра|gameplay|fun|весело|интересно|"
                  r"цена|оптимизаци\w*|контент|качество|хорошая игра|разное|другое)$")
MIN_SCORE = 1.0


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def _short(label: str, limit: int = MAX_LABEL) -> str:
    label = re.sub(r"\s+", " ", str(label or "")).strip(" .,;:—-")
    if len(label) <= limit:
        return label[:1].upper() + label[1:]
    cut = label[:limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rfind(" ")]
    cut = cut.rstrip(" .,;:—-") + "…"
    return cut[:1].upper() + cut[1:]


def _uniq(items) -> list:
    out = []
    for x in items:
        if x and x not in out:
            out.append(x)
    return out


def _clean_axes(axes) -> dict[str, int]:
    out = {}
    for k, v in (axes or {}).items() if isinstance(axes, dict) else []:
        if k in AXES:
            try:
                out[k] = max(0, min(10, int(round(float(v)))))
            except (TypeError, ValueError):
                continue
    return out


def _game_tags(game: dict | None) -> dict[str, float]:
    tags = (game or {}).get("tags") or {}
    if isinstance(tags, (list, tuple)):        # a plain list, most voted first
        return {t: float(len(tags) - i) for i, t in enumerate(tags)}
    return {t: float(v or 0) for t, v in tags.items()}


def canonical(group: str, game_tags=None, *, label: str | None = None, extra_tags=()) -> Aspect:
    """The canonical aspect of a group, with the alt tags the game actually has."""
    lab, axes, core, alt, words, _ = _C[group]
    have = set(game_tags or ())
    tags = _uniq([*core, *extra_tags, *(t for t in alt if t in have)])[:5]
    return Aspect(group, _short(label or lab), dict(axes), tags, words, group)


def group_of(text: str) -> str:
    """The canonical group a phrase talks about ("" if none): the earliest match in the text wins."""
    low = _norm(text)
    best, pos = "", len(low) + 1
    for g, rx in CANON.items():
        m = rx.search(low)
        if m and m.start() < pos:
            best, pos = g, m.start()
    return best


# --- options

def options(game: dict, passport: dict | None, card_aspects: list[dict] | None = None,
            limit: int = 6) -> list[Aspect]:
    """Up to `limit` distinct, concrete things one might love in this game, most characteristic first.

    LLM reference-card aspects come first, in their order; the rest is filled from the passport's praise,
    its strongest feel axes and the game's distinctive player tags, one option per group. Returns [] when
    nothing specific is known: then the bot should skip the question and use the seed as a whole."""
    tags = _game_tags(game)
    out: list[Aspect] = []
    used_groups: set[str] = set()
    for i, c in enumerate(card_aspects or []):
        a = _from_card(c, i, tags)
        if a is None or any(_norm(a.label) == _norm(b.label) for b in out):
            continue
        if a.group in used_groups:
            a.group = ""          # two card aspects in one group are both kept: the model meant them apart
        if a.key in {b.key for b in out}:
            a.key = f"c{i}"
        used_groups.add(a.group)
        out.append(a)
        if len(out) >= limit:
            return out

    scores: dict[str, list[tuple[float, str | None, str | None]]] = {}    # group -> [(score, label, tag)]

    def add(group, score, label=None, tag=None):
        if group in _C:
            scores.setdefault(group, []).append((score, label, tag))

    top = max(tags.values(), default=0) or 1
    ranked = sorted(tags.items(), key=lambda kv: -kv[1])[:20]
    for tag, votes in ranked:
        if tag in TAG_MAP:
            group, w, label = TAG_MAP[tag]
            add(group, w * (0.6 + 1.6 * votes / top), label, tag)
    p = passport or {}
    for pr in p.get("praise") or []:
        point = str((pr or {}).get("point") or "")
        group = group_of(point)
        if group:
            add(group, 2.5 + 1.5 * SHARE.get(pr.get("share"), 0.3))
    src = p.get("_source")
    trust = 1.0 if src == "llm" else 0.6 if src == "heuristic" else 0.85
    feel = p.get("feel") or {}
    for axis, high, thr, group in FEEL_RULES:
        v = feel.get(axis)
        if v is None:
            continue
        if high and v >= thr:
            add(group, trust * (1.5 + 0.5 * (v - thr + 1)))
        elif not high and v <= thr:
            add(group, trust * (1.5 + 0.5 * (thr - v + 1)))
    for c in p.get("complaints") or []:
        g = COMPLAINT_GROUPS.get(((c or {}).get("axis"), (c or {}).get("direction")))
        if g and c.get("kind") == "taste":
            add(g, 0.8 * SHARE.get(c.get("share"), 0.3) + 0.4)
    for genre in p.get("real_genres") or []:
        g = group_of(str(genre))
        if g:
            add(g, 0.6)

    groups = []
    for group, items in scores.items():
        if group in used_groups:
            continue
        items.sort(key=lambda x: -x[0])
        total = items[0][0] + 0.5 * sum(s for s, _, _ in items[1:])
        if total < MIN_SCORE:
            continue
        label = next((lab for _, lab, _ in items if lab), None) if items[0][1] is None and \
            all(t is None for _, _, t in items[:1]) else items[0][1]
        if group == "setting":
            label = next((lab for _, lab, _ in items if lab), None)
            if not label:
                continue
        a = canonical(group, tags, label=label, extra_tags=[t for _, _, t in items if t])
        if group == "atmos" and "Dark" in tags and label is None:
            a.label = "Мрачная атмосфера"
        groups.append((total, a))
    groups.sort(key=lambda x: -x[0])
    for _, a in groups:
        if len(out) >= limit:
            break
        if any(_same(a, b) for b in out) or a.key in {b.key for b in out}:
            continue
        out.append(a)
    return out


# Loved, but no reason to play ANOTHER game: the same music or setting alone makes no good match.
NOT_A_HOOK = {"music", "visual", "setting"}


def hooks(game: dict, passport: dict | None, n: int = 4) -> list[Aspect]:
    """What players love this game for, most praised first (its reviews' praise, then its strongest
    feel axes and tags), without music, looks and setting. The bot uses them when the player did not
    say what hooked them: a Cyberpunk 2077 fan is matched on story, builds and exploration, not on neon."""
    return [a for a in options(game, passport, limit=8) if a.group and a.group not in NOT_A_HOOK][:n]


def strengths(game: dict, passport: dict | None) -> dict[str, float]:
    """group -> 0..1, how surely this game is strong at it: praised in its reviews (most sure), a strong
    feel axis in its passport, or only its player tags (least sure)."""
    out: dict[str, float] = {}

    def put(group: str, v: float) -> None:
        if group:
            out[group] = max(out.get(group, 0.0), min(1.0, v))

    p = passport or {}
    real = p.get("_source") == "llm"
    for pr in p.get("praise") or [] if real else []:
        put(group_of(str((pr or {}).get("point") or "")), {"most": 1.0, "many": 0.85}.get(pr.get("share"), 0.7))
    trust = 0.75 if real else 0.4
    feel = p.get("feel") or {}
    for axis, high, thr, group in FEEL_RULES:
        v = feel.get(axis)
        if v is not None and (v >= thr if high else v <= thr):
            put(group, trust)
    tags = _game_tags(game)
    top = max(tags.values(), default=0) or 1
    for tag, votes in tags.items():
        if tag in TAG_MAP:
            group, w, _ = TAG_MAP[tag]
            put(group, 0.55 * w * min(1.0, votes / top + 0.2))
    return out


def _same(a: Aspect, b: Aspect) -> bool:
    return _norm(a.label) == _norm(b.label) or (a.group and a.group == b.group)


def _from_card(c: dict, i: int, game_tags: dict) -> Aspect | None:
    if not isinstance(c, dict):
        return None
    raw = str(c.get("label_ru") or c.get("label") or "").strip()
    words = str(c.get("words_ru") or c.get("words") or raw).strip()
    if not raw or JUNK.match(_norm(raw).strip(" .!")):
        return None
    group = group_of(raw) or group_of(words)
    axes = _clean_axes(c.get("axes")) if c.get("axes") is not None else (dict(_C[group][1]) if group else {})
    tags = c.get("tags")
    if tags is None:
        tags = canonical(group, game_tags).tags if group else []
    tags = _uniq(str(t) for t in tags if isinstance(t, str))[:5]
    return Aspect(group or f"c{i}", _short(raw), axes, tags, words[:300], group)


# --- the answer

def apply(req, seed: dict, seed_feel: dict | None, chosen: list[Aspect], *, all_of_it: bool = False):
    """A copy of the request steered by what they liked in `seed` (see the module docstring).

    Sets on the copy: axes (picked aspects' targets, seed's own value when it is on the same side; the
    request's own typed axes still win), tags_want / tags_avoid, words, and:
      req.focus_axes: list[str]  axes the player cares about in the seed; None = take the seed whole
      req.mute_tags: list[str]   seed tags tied to aspects they did not pick, to pull ~4x weaker
      req.focus_labels: list[str] what they picked, for the banner ("как Hollow Knight: исследование")
    all_of_it=True (or nothing chosen) returns the request unchanged."""
    r = copy.deepcopy(req)
    if all_of_it or not chosen:
        return r
    sf = seed_feel or {}
    want = [a for a in chosen if a.polarity > 0]
    away = [a for a in chosen if a.polarity < 0]
    skip = [a for a in chosen if a.polarity == 0]

    axes: dict[str, int] = {}
    for a in want:
        for k, v in a.axes.items():
            v = _toward_seed(v, sf.get(k))
            if k not in axes or abs(v - 5) > abs(axes[k] - 5):
                axes[k] = v
    for a in away:
        for k, v in a.axes.items():
            axes[k] = min(3, 10 - v) if v >= 5 else max(7, 10 - v)
    for a in skip:
        for k in a.axes:
            if not any(k in w.axes for w in want):
                axes.pop(k, None)
    r.axes = {**axes, **dict(getattr(r, "axes", None) or {})}

    avoid_tags = _uniq(t for a in away for t in a.tags)
    r.tags_want = [t for t in _uniq([*(getattr(r, "tags_want", None) or []), *(t for a in want for t in a.tags)])
                   if t not in avoid_tags]
    r.tags_avoid = _uniq([*(getattr(r, "tags_avoid", None) or []), *avoid_tags])
    phrases = [a.words for a in want if a.words]
    r.words = "; ".join(_uniq([getattr(r, "words", "") or "", *phrases]))[:1000]

    r.focus_axes = sorted({k for a in want for k in a.axes})
    want_groups = {a.group for a in want if a.group}
    seed_tags = _game_tags(seed)
    mute = [t for t in seed_tags if t in TAG_MAP and TAG_MAP[t][0] not in want_groups]
    mute += [t for a in skip for t in a.tags]
    r.mute_tags = [t for t in _uniq(mute) if t not in r.tags_want]
    r.focus_labels = [a.label for a in want]
    return r


def _toward_seed(v: int, seed_v) -> int:
    """The aspect's target, or the seed's own value when it is on the same side (Hollow Knight's 9, not 8)."""
    if seed_v is None:
        return v
    if v >= 6 and seed_v >= 6 or v <= 4 and seed_v <= 4:
        return int(round(seed_v))
    return v


# --- free-text answer: «атмосфера и сюжет, а бои так себе»

_ALL = re.compile(r"^\W*(все|всё|всего|вс[её] сразу|вс[её] вместе|вс[её] понравилось|вс[её] целиком|"
                  r"целиком|в целом)\W*$")
_CLAUSE = re.compile(r"[,.;!?()\n—–]|\s-\s|\bа\b|\bно\b|\bзато\b|\bоднако\b|\bхотя\b|\bbut\b|(?=\bкроме\b)")
_POS_PHRASES = re.compile(r"\bне мог\w* оторваться|\bне оторваться|\bне отпуска\w*|\bне только|\bне надоеда\w*|"
                          r"\bне устаешь|\bне скучн\w*")
_STRONG = re.compile(r"\bне понрав|\bне нрав|\bне любл|\bне зашл|\bне зашел|\bне хочу|\bне надо|бесил|бесит|бесят|"
                     r"раздраж|напряга|ненавиж|надоел|утомл|задолбал|достал|устал|слишком|перебор|\bдушн|"
                     r"\bбез\b|\bминус|\bне для меня|мешал|\bhate|\bхуже|\bужасн\w* (был|бои|сложн)")
_MILD = re.compile(r"так себе|\bне особо|\bне очень|неважн|\bне важн|\bне главн|\bне главное|пофиг|вс[её] равно|"
                   r"без разницы|\bне сильно|средне|\bкроме\b|^\s*не\b|\bне в\b|\bнормальн")
_STOP = ("понрав", "нрав", "зацеп", "любл", "обожа", "очень", "больше", "всего", "особенн", "сильн", "именно",
         "было", "была", "были", "игра", "игре", "игры", "игру", "мне", "меня", "это", "как", "что", "все",
         "тоже", "еще", "просто", "вообще", "прям", "там", "тут", "его", "она", "они", "самое", "самый",
         "главн", "конечно", "наверн", "кажется", "думаю", "весь", "вся", "всем", "чем", "зашл", "зашел",
         "круто", "крут", "класс", "отличн", "хорош", "шикарн", "прикольн", "нравит", "лучш")
_WORD = re.compile(r"[a-zа-я0-9]+")


def wants_all(text: str) -> bool:
    """«всё», «всё сразу», «в целом» — the seed as a whole (apply(..., all_of_it=True))."""
    return bool(_ALL.match(_norm(text)))


def from_text(text: str, opts: list[Aspect]) -> list[Aspect]:
    """Options the player named in words, with polarity: 1 liked, 0 «не в этом дело», -1 disliked.

    Matches the offered options (by their group's keywords and by the stems of their labels), then
    canonical aspects that were not offered («ещё музыка»), then turns leftover words of a positive
    clause into one free-form aspect (key "own"). «Всё, кроме сложности» = every option, the named
    one left out. Liked ones come first."""
    low = _norm(text)
    if not low.strip():
        return []
    if wants_all(text):
        return [replace(o, polarity=1) for o in opts]
    found: dict[str, Aspect] = {}           # key -> aspect, first verdict on a key wins unless negative
    leftovers: list[str] = []
    everything = False
    for start, end in _spans(low):
        clause = low[start:end]
        if not clause.strip():
            continue
        cleaned = _POS_PHRASES.sub(" ", clause)
        pol = -1 if _STRONG.search(cleaned) else 0 if _MILD.search(cleaned) else 1
        if pol == 1 and re.search(r"(^|\s)(все|всё)(\s|$)", clause) and not _content(clause, []):
            everything = True
        hits: list[tuple[int, int]] = []
        groups_hit = set()
        for o in opts:
            m = _match(o, clause)
            if m:
                hits.append(m)
                groups_hit.add(o.group)
                _put(found, replace(o, polarity=pol))
        for g, rx in CANON.items():
            if g in groups_hit:
                continue
            m = rx.search(clause)
            if m and not any(o.group == g for o in opts):
                hits.append(m.span())
                _put(found, replace(canonical(g), polarity=pol))
        if pol == 1:
            rest = _content(clause, hits)
            if rest:
                leftovers.append(text[start:end].strip(" ,.;!?"))
    if everything:
        for o in opts:
            if o.key not in found:
                found[o.key] = replace(o, polarity=1)
    if leftovers:
        own = " ".join(leftovers)
        found.setdefault("own", Aspect("own", _short(own), _infer_axes(own), [], own[:300], "", 1))
    return sorted(found.values(), key=lambda a: -a.polarity)


def _spans(low: str) -> list[tuple[int, int]]:
    out, pos = [], 0
    for m in _CLAUSE.finditer(low):
        if m.start() > pos:
            out.append((pos, m.start()))
        pos = max(pos, m.end())
    out.append((pos, len(low)))
    return out


def _match(o: Aspect, clause: str) -> tuple[int, int] | None:
    if o.group in CANON:
        m = CANON[o.group].search(clause)
        if m:
            return m.span()
    for stem in _stems(o.label):
        m = re.search(r"\b" + re.escape(stem), clause)
        if m:
            return m.start(), m.end()
    return None


def _stems(label: str) -> list[str]:
    out = []
    for w in _WORD.findall(_norm(label)):
        if len(w) >= 4 and not w.startswith(_STOP) and w not in ("мира", "стиль", "система"):
            out.append(w[:max(4, len(w) - 4)])
    return out


def _put(found: dict[str, Aspect], a: Aspect) -> None:
    old = found.get(a.key)
    if old is None or a.polarity < old.polarity:
        found[a.key] = a


def _content(clause: str, hits: list[tuple[int, int]]) -> list[str]:
    """Meaningful words of a clause outside the matched keywords."""
    chars = list(clause)
    for s, e in hits:
        # blank the whole word around each hit
        while s > 0 and chars[s - 1].isalnum():
            s -= 1
        while e < len(chars) and chars[e].isalnum():
            e += 1
        chars[s:e] = " " * (e - s)
    words = [w for w in _WORD.findall("".join(chars)) if len(w) >= 4 and not w.startswith(_STOP)]
    return words if len(words) >= 2 or any(len(w) >= 6 for w in words) else []


_AXIS_WORDS = [
    (r"\bбыстр|динамичн|драйв", "pace", 8), (r"медленн|неторопл|спокойн\w* темп", "pace", 2),
    (r"сложн|хардкор", "difficulty", 8), (r"\bлегк|казуал", "difficulty", 3),
    (r"сюжет|истори", "story", 8), (r"исслед|секрет|тайн|шорткат|срезк|связан", "exploration", 8),
    (r"страшн|жутк|напряж", "tension", 8), (r"спокой|уют|расслаб", "tension", 2),
    (r"\bбо[йиею]\b|\bбоев|драк|\bбосс", "combat", 8), (r"свобод|куда хочешь", "freedom", 8),
    (r"\bдруз|кооп|вместе", "social", 8), (r"реиграб|каждый раз", "replay", 8),
    (r"глубок|механик|систем", "complexity", 8), (r"гринд|фарм", "grind", 7),
    (r"надолго|длинн|сотни час", "length", 8), (r"коротк|за вечер", "length", 2),
]


def _infer_axes(text: str) -> dict[str, int]:
    low = _norm(text)
    return {axis: v for rx, axis, v in _AXIS_WORDS if re.search(rx, low)}


# --- bot helpers: question, keyboard, session state

def question(name: str) -> str:
    return f"Чем именно зацепила {name}?"


def keyboard(opts: list[Aspect], picked=()) -> list[list[tuple[str, str]]]:
    """Rows of (button text, callback data): toggles "asp:<key>" (✓ when picked), "asp:all", "asp:done"."""
    picked = set(picked)
    rows, row = [], []
    for o in opts:
        btn = (("✓ " if o.key in picked else "") + o.label, f"asp:{o.key}")
        if len(o.label) > 16:
            if row:
                rows.append(row)
                row = []
            rows.append([btn])
            continue
        row.append(btn)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("Всё сразу", "asp:all")] + ([("Готово", "asp:done")] if picked else []))
    return rows


def dump(opts: list[Aspect]) -> list[dict]:
    """JSON-ready, for the session."""
    return [asdict(o) for o in opts]


def load(rows) -> list[Aspect]:
    """Back from the session JSON; tolerant of missing or unknown keys."""
    names = {f.name for f in fields(Aspect)}
    out = []
    for d in rows or []:
        if not isinstance(d, dict) or not d.get("key") or not d.get("label"):
            continue
        d = {k: v for k, v in d.items() if k in names}
        d.setdefault("axes", {})
        d.setdefault("tags", [])
        d.setdefault("words", d["label"])
        a = Aspect(**d)
        a.axes = _clean_axes(a.axes)
        out.append(a)
    return out


def pick(opts: list[Aspect], keys) -> list[Aspect]:
    """The options with these keys (toggled buttons), in the options' order."""
    keys = set(keys)
    return [o for o in opts if o.key in keys]


def valid_key(key: str) -> bool:
    return bool(KEY_RE.match(key or ""))


def _hash_key(text: str) -> str:
    return "x" + hashlib.md5(text.encode()).hexdigest()[:7]
