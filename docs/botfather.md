# The @BotFather profile

What to set in [@BotFather](https://t.me/BotFather) for the bot's profile, and what the bot
sets itself. Each command asks you to pick the bot from a list.

| BotFather command | What to send |
|---|---|
| `/setname` | `Silverhand Game Finder` |
| `/setuserpic` | [`assets/brand/avatar.png`](../assets/brand/avatar.png) |
| `/setdescriptionpic` | [`assets/brand/botfather-description.mp4`](../assets/brand/botfather-description.mp4) or [`.gif`](../assets/brand/botfather-description.gif) |
| `/setjoingroups` | **Disable**: the bot only works in private chats |

## Set by the bot itself

On every start the bot sets, in English for everyone and in each of its other languages for
people whose Telegram app is in that language:

- **the description**: shown in an empty chat with the bot, under the picture, before the
  first `/start` (the paragraph about the bot being private is added while `OPEN_ACCESS=0`);
- **the about text**: shown in the bot's profile and in link previews;
- **the command menu**: `/find`, `/game`, `/lang`, `/help`, `/start`, and for the owners from
  `OWNER_IDS` also `/stats` and `/allow`.

So `/setdescription`, `/setabouttext` and `/setcommands` are not needed: edits made there are
overwritten on the next restart. Change the texts in
[`gamefinder/profile.py`](../gamefinder/profile.py) and the commands in
[`gamefinder/bot.py`](../gamefinder/bot.py) (`commands()`). The tests check the texts against
Telegram's limits (512 for the description, 120 for the about text, in UTF-16 units).

## Pictures

- **`/setuserpic`**: [`assets/brand/avatar.png`](../assets/brand/avatar.png), 640×640.
  [`avatar-96.png`](../assets/brand/avatar-96.png) shows how the avatar looks small; no need to
  upload it.
- **`/setdescriptionpic`**: a 640×360 picture above the description. Best is
  [`botfather-description.mp4`](../assets/brand/botfather-description.mp4) (172 KB, no sound:
  Telegram plays it as a GIF). A fallback is
  [`botfather-description.gif`](../assets/brand/botfather-description.gif) (1.2 MB, 60 frames,
  a 3-second loop). For a still picture:
  [`botfather-description.png`](../assets/brand/botfather-description.png).

`tools/make_intro.py` draws the animation, the menu screens' animations (`--menu`, one set per
language in `assets/menu/<lang>/`) and the README banner (`--banner`); `--title`, `--sub` and
`--eyebrow` change the text. The selection and breakdown cards are in
[`assets/brand/cards/`](../assets/brand/cards/).
