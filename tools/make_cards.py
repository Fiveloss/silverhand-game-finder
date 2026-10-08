"""Card previews for the README: assets/brand/cards/*.jpg, drawn by the bot's own renderer.

    python tools/make_cards.py

Fetches four Steam covers (cached in data/covers). The numbers on the cards are illustrative.
Drawn in English, the bot's default language.
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gamefinder import i18n, render  # noqa: E402
from gamefinder.analyst import AXES, axis_ends  # noqa: E402
from gamefinder.http import Http  # noqa: E402
from gamefinder.texts import axis_label  # noqa: E402

OUT = ROOT / "assets" / "brand" / "cards"


def feel(values: dict) -> list:
    return [(axis_label(k).capitalize(), v, *axis_ends(k)) for k, v in values.items()]


async def main() -> None:
    i18n.set_lang("en")
    OUT.mkdir(parents=True, exist_ok=True)
    http = Http()
    try:
        covers = {a: await render.cover(http, a, cache_dir=str(ROOT / "data" / "covers"))
                  for a in (753640, 367520, 1145360, 632470)}
    finally:
        await http.close()

    pick1 = {"rank": 1, "name": "Outer Wilds", "match": 0.91, "genres": ["Exploration", "Puzzle", "Space"],
             "feel": feel({"pace": 3, "story": 9, "exploration": 10, "difficulty": 4}), "recent": "96%",
             "reviews_total": 112834, "hours": "~22 h", "price": "$14.99 (−40%)",
             "deck": "Steam Deck: verified", "judge": True, "warning": ""}
    pick2 = {"rank": 2, "name": "Hades", "match": 0.84, "genres": ["Roguelike", "Action", "Mythology"],
             "feel": feel({"pace": 9, "difficulty": 6, "story": 6, "replay": 9}), "recent": "98%",
             "reviews_total": 256000, "hours": "~25 h", "price": "$24.99",
             "deck": "Steam Deck: verified", "judge": False, "warning": ""}
    game = {**pick1, "name": "Disco Elysium - The Final Cut", "match": None, "rank": 0, "judge": False,
            "genres": ["Detective RPG", "Text-heavy", "RPG"], "recent": "92%", "reviews_total": 98765,
            "hours": "~30 h", "price": "$9.99 (−75%)",
            "feel": feel({k: v for k, v in zip(AXES, [3, 4, 10, 8, 6, 1, 4, 1, 10, 0, 6, 3])}),
            "praise": [("The best writing in the genre: dialogue, humour, characters", "almost everyone"),
                       ("Role-playing freedom: failures are as fun as successes", "many"),
                       ("Full voice acting and painterly art", "some")],
            "complaints": [("A lot of reading, almost no combat", "many", True),
                           ("The story sags towards the end", "some", True),
                           ("Crashes on older graphics cards", "some", False)],
            "store_genres": ["RPG"], "engaged": "98% of players with 10+ h are happy",
            "state_now": "Now: reviews steadily high, no patches needed",
            "summary": "A detective game where conversations with your own thoughts replace combat. You "
                       "investigate a murder in a port city, arguing with twenty-four skills that talk in your head."}
    (OUT / "pick-1.jpg").write_bytes(render.pick_card(pick1, covers[753640]))
    (OUT / "pick-2.jpg").write_bytes(render.pick_card(pick2, covers[1145360]))
    (OUT / "game.jpg").write_bytes(render.game_card(game, covers[632470]))
    (OUT / "banner.jpg").write_bytes(render.selection_banner(
        "PICKS", "like Hollow Knight · exploration, atmosphere · easier",
        [covers[1145360], covers[367520], covers[753640]]))
    (OUT / "placeholder.jpg").write_bytes(render.pick_card({**pick2, "name": "A game without a cover"}, None))
    print("written to", OUT)


if __name__ == "__main__":
    asyncio.run(main())
