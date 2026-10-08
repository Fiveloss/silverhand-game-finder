"""Card previews for the README: assets/brand/cards/*.jpg, drawn by the bot's own renderer.

    python tools/make_cards.py

Fetches four Steam covers (cached in data/covers). The numbers on the cards are illustrative.
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gamefinder import render  # noqa: E402
from gamefinder.analyst import AXES  # noqa: E402
from gamefinder.http import Http  # noqa: E402
from gamefinder.texts import AXIS_LABEL  # noqa: E402

OUT = ROOT / "assets" / "brand" / "cards"


def feel(values: dict) -> list:
    return [(AXIS_LABEL[k].capitalize(), v, *AXES[k]) for k, v in values.items()]


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    http = Http()
    try:
        covers = {a: await render.cover(http, a, cache_dir=str(ROOT / "data" / "covers"))
                  for a in (753640, 367520, 1145360, 632470)}
    finally:
        await http.close()

    pick1 = {"rank": 1, "name": "Outer Wilds", "match": 0.91, "genres": ["Исследование", "Головоломка", "Космос"],
             "feel": feel({"pace": 3, "story": 9, "exploration": 10, "difficulty": 4}), "recent": "96%",
             "reviews_total": 112834, "hours": "~22 ч", "price": "2 590 KZT (−40%)",
             "deck": "Steam Deck: проверено", "judge": True, "warning": ""}
    pick2 = {"rank": 2, "name": "Hades", "match": 0.84, "genres": ["Рогалик", "Экшен", "Мифология"],
             "feel": feel({"pace": 9, "difficulty": 6, "story": 6, "replay": 9}), "recent": "98%",
             "reviews_total": 256000, "hours": "~25 ч", "price": "1 990 KZT",
             "deck": "Steam Deck: проверено", "judge": False, "warning": ""}
    game = {**pick1, "name": "Disco Elysium - The Final Cut", "match": None, "rank": 0, "judge": False,
            "genres": ["Детективная RPG", "Текстовая", "RPG"], "recent": "92%", "reviews_total": 98765,
            "hours": "~30 ч", "price": "4 390 KZT (−75%)",
            "feel": feel({k: v for k, v in zip(AXES, [3, 4, 10, 8, 6, 1, 4, 1, 10, 0, 6, 3])}),
            "praise": [("Лучший текст в жанре: диалоги, юмор и персонажи", "почти все"),
                       ("Свобода отыгрыша: провалы так же интересны, как успехи", "многие"),
                       ("Полная озвучка и живописная графика", "некоторые")],
            "complaints": [("Очень много чтения, почти нет боёв", "многие", True),
                           ("Под конец сюжет проседает", "некоторые", True),
                           ("Вылеты на старых видеокартах", "некоторые", False)],
            "store_genres": ["RPG"], "engaged": "98% наигравших 10+ ч довольны",
            "state_now": "Сейчас: отзывы стабильно высокие, патчи не нужны",
            "summary": "Детектив, где вместо боёв — разговоры с собственными мыслями. Вы расследуете убийство "
                       "в портовом городе, споря с двадцатью четырьмя навыками-голосами в голове."}
    (OUT / "pick-1.jpg").write_bytes(render.pick_card(pick1, covers[753640]))
    (OUT / "pick-2.jpg").write_bytes(render.pick_card(pick2, covers[1145360]))
    (OUT / "game.jpg").write_bytes(render.game_card(game, covers[632470]))
    (OUT / "banner.jpg").write_bytes(render.selection_banner(
        "ПОДБОРКА", "как Hollow Knight · исследование, атмосфера · проще",
        [covers[1145360], covers[367520], covers[753640]]))
    (OUT / "placeholder.jpg").write_bytes(render.pick_card({**pick2, "name": "Игра без обложки"}, None))
    print("written to", OUT)


if __name__ == "__main__":
    asyncio.run(main())
