# Профиль в @BotFather

Готовые тексты и картинки для профиля бота. Команды отправляются в
[@BotFather](https://t.me/BotFather), после каждой он просит выбрать бота из списка.

| Команда BotFather | Что отправить |
|---|---|
| `/setname` | `Silverhand Game Finder` |
| `/setdescription` | текст из раздела [ниже](#setdescription) |
| `/setabouttext` | текст из раздела [ниже](#setabouttext) |
| `/setcommands` | список из раздела [ниже](#setcommands) (необязательно: бот ставит его сам) |
| `/setuserpic` | [`assets/brand/avatar.png`](../assets/brand/avatar.png) |
| `/setdescriptionpic` | [`assets/brand/botfather-description.mp4`](../assets/brand/botfather-description.mp4) или [`.gif`](../assets/brand/botfather-description.gif) |
| `/setjoingroups` | **Disable**: бот отвечает только в личке, в группах ему делать нечего |

## /setdescription

Видно в пустом чате с ботом, под картинкой, до первого `/start`. Лимит Telegram —
512 символов. Этот текст — **488 из 512** (переводы строк считаются, эмодзи нет, так что
символы совпадают с единицами UTF-16, которыми считает Telegram).

```text
Помогаю выбрать, во что поиграть прямо сейчас, по свежим отзывам игроков, а не по рекламе.

Напиши своими словами: «как Hollow Knight, но проще, на пару вечеров» или «кооп с другом, не шутер». Спрошу, чем зацепила игра, прочитаю отзывы в Steam и GOG и пришлю три игры карточками: жанр на деле, длина, цена, за что хвалят и ругают сейчас.

Подкрутишь кнопками: короче, проще, ещё. Анкет нет, каждый запрос с нуля.

Бот приватный: после /start владелец получит запрос и сможет тебя пустить.
```

Последний абзац верен, пока `OPEN_ACCESS=0`. Если бот открыт всем, его можно убрать.

## /setabouttext

Видно в профиле бота и в превью ссылок на него. Лимит — 120 символов. Этот текст —
**100 из 120**.

```text
Во что поиграть прямо сейчас: напиши своими словами, подберу три игры по свежим отзывам Steam и GOG.
```

## /setcommands

Тот же список, что `COMMANDS` в [`gamefinder/bot.py`](../gamefinder/bot.py):

```text
find - Подобрать игру
game - Разбор игры по отзывам
help - Как это работает
start - Начало
```

Отправлять его не обязательно: при каждом запуске бот сам ставит этот список всем, а
владельцам из `OWNER_IDS` — расширенный, с `/stats` и `/allow` (`OWNER_COMMANDS`).
Правки, сделанные через BotFather, перезапишутся при следующем перезапуске, поэтому
меняй список в `gamefinder/bot.py`.

## Картинки

- **`/setuserpic`** — [`assets/brand/avatar.png`](../assets/brand/avatar.png), 640×640.
  [`avatar-96.png`](../assets/brand/avatar-96.png) показывает, как аватар выглядит в
  маленьком размере; загружать его не нужно.
- **`/setdescriptionpic`** — картинка 640×360 над описанием. Лучше
  [`botfather-description.mp4`](../assets/brand/botfather-description.mp4) (172 КБ,
  без звука: Telegram проигрывает его как GIF). Запасной вариант —
  [`botfather-description.gif`](../assets/brand/botfather-description.gif) (1,2 МБ,
  60 кадров, петля 3 секунды). Нужна неподвижная картинка —
  [`botfather-description.png`](../assets/brand/botfather-description.png).

Анимацию, анимации экранов меню и баннер для README рисует `tools/make_intro.py`
(текст меняют `--title`, `--sub` и `--eyebrow`). Как выглядят карточки подборки и
разбора игры — в [`assets/brand/cards/`](../assets/brand/cards/).
