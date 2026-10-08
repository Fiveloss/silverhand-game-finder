# Changelog

## 0.5.0 — 2026-10-08

English first.

- The first `/start` asks for the language, English first; `/lang` and **🌐 Language** on the
  home screen change it. Every button, panel, card, caption and game breakdown comes in it,
  and so do the AI judge's reasons and risks and the scout's "why".
- Game passports are still read once for everyone; for English players their text is
  translated by a free model once per passport version and kept (`passport_tr`). Heuristic
  passports map their fixed phrases without a model.
- Free-text answers in English are understood too ("the atmosphere and exploration, but the
  combat was annoying", "everything", "not really").
- The bot sets its own description, about text and command menu in each language
  (`gamefinder/profile.py`), English by default.
- Menu animations per language (`assets/menu/<lang>/`), and English README cards, banner,
  BotFather picture and promo.

## 0.4.0 — 2026-10-08

Genre and format first: picks a player can just play in order.

- Each reference game is anchored by its most pronounced genres (`genres.py`: genre tags
  with the most player votes, close ones as one family). A candidate shares its main genre
  or two of its defining ones; the co-play graph and the scout no longer get around it
  (Black Myth: Wukong for Resident Evil was the case).
- Format from player tags: 2D vs 3D and turn-based vs real-time combat are hard rules,
  another camera costs score.
- The reference in another edition never comes up (Resident Evil 4 (2005) for the remake);
  one game of its series at most, always last. One game per series in any selection.
- Every card says why the game is there: the shared genre, the same format, how close the
  player tags are, who proposed it (the scout, players of the reference, the judge).
- Player slang and short names for titles ("RE" for Resident Evil, "BG3"…), in the lookup and the model's
  reading of the request.
- Reviews of the whole catalog: the background reads them on Gemini Flash Lite only (Groq's
  small daily quota stays for the calls a player waits for). Flash Lite's free tier is about
  500 calls a day: `LLM_DAILY_GAMES=500` lets the background take 400 and keeps the rest for players.
- A second Groq model (`openai/gpt-oss-20b`, its own daily quota) answers when the main one is
  out for the day.
- What the reference is loved for: when the player does not say what hooked them, the bot takes
  the reference's most praised strengths from its reviews (Cyberpunk 2077: story, builds,
  atmosphere, exploration; never music, looks or setting) and steers by them. A new score part,
  «strong at the same things» (30%), checks each candidate's own praise, feel and tags, and the
  reference's setting tags are muted: a neon city alone (Cloudpunk) no longer makes a match.
- The judge rates every candidate 0-10 (it used to choose a few and was too shy to name more than
  one); the bot shows the best rated, 6 and up, in order. It sees the reference's main genres,
  format and strengths, and may use what it surely knows about well-known games not read yet.
  Fewer, but sure: no near-miss filler after the judge ran, and the panel says so.
- Up to two more sure picks are kept back: "✅ Played it" or "👎 Not this" on a card brings the
  next one at once, with no new search.
- "🤔 Wrong game" under "What exactly hooked you in X?": when the reference was found as another game, the
  bot asks for the exact title.
- `tools/golden.json` and `eval_picks.py --golden`: 30 requests with games that fit and games
  that must never come up.

## 0.3.1 — 2026-10-08

Faster picks: a request took 2–3 minutes once Gemini Flash's free quota (now about 20 calls
a day) was used up; checked on 12 real requests, it now takes about 20–30 s.

- Each job has its own order of providers: reviews on Gemini Flash Lite (then Groq), quick
  calls on Groq then Flash Lite with little thinking, the judge on Flash while its quota lasts.
- A daily 429 rests a provider until its quota resets (Gemini says when), instead of
  being asked again every 10 minutes.
- Short timeouts for calls a player waits for, and one retry after a hang.
- The scout waits at most 6 s for Steam lookups (player tags only; the store page comes later); late ones finish in the background and
  reach the catalog. Similar games of a non-Steam reference are looked up at once.
- Groq's default model is `openai/gpt-oss-120b` (Llama 3.3 70B left Groq's free tier); quick
  calls on it answer in about a second.
- One game per series in a selection: a sequel of the reference is fine, two more parts of
  one series are not.
- A bigger catalog by default (`CATALOG_PAGES=10`): more of the scout's ideas are found locally.
- `tools/eval_picks.py`: runs real requests through the whole pipeline and prints the
  picks, the score parts, the judge's reasons and where the time went.

## 0.3.0 — 2026-10-08

Help in the moment, with one panel instead of a stream of messages.

- One panel message per player, edited in place: the home screen, the questions, the
  search progress and "Fine-tune?" live in it, and it moves under the newest cards.
  The player's own messages are deleted once read. The persistent reply keyboard is gone.
- Questions before a search, each skippable with "⚡ Pick now": "What exactly hooked
  you in X?" (when a game is named), "Anything to avoid?" (toggle exclusions, the ones
  already in the request ticked) and "How much time do you have?" (skipped when the request
  says). Mood buttons search at once.
- Written corrections: anything typed while the questions or the results are on screen
  is merged into the current request ("no horror, shorter"); a new reference game
  starts a new search. Typing during a search is applied right after it.
- Refine buttons renamed ("Shorter", "Easier", "Harder", "More story", "Calmer",
  "Something else", "🔄 Three more"). When nothing fits, "Drop the exclusions", "Any length" and
  "Something else" loosen the request.
- Prices from the player's own Steam region: asked once before the first search
  (changeable from the home screen), fetched from the Steam store for each selection
  and cached for an hour; a game not sold in the region says so. `STORE_CC` now only
  sets the catalog's region.
- A confidence mark on every card: 🟢 analysed from N recent reviews, ⚪ preliminary,
  from player tags.
- Animated cards: the picture is sent at once and then swapped for a 2-second MP4 loop
  (`ANIMATE_CARDS`, on by default; ffmpeg comes with `imageio-ffmpeg`). Any failure
  leaves the picture.
- The scout: one LLM call proposes up to 15 games for what the player loved; each is
  matched strictly on Steam and scored like any other candidate. The judge now always
  runs when a key and quota are there, and sees tag-only candidates too.
- Stricter matching: IDF of tags is capped, broad tags alone no longer make games
  alike, candidates must share a defining tag of the reference unless the co-play graph
  or the scout backs them. Only difficulty, tension, length, grind and social (with
  hours and co-op) are hard limits; story, exploration and the rest are wishes.
- Reviews are never read while a player waits for picks: unread candidates work from
  player tags and go first in the background queue, which reads the catalog ahead
  within 80% of `LLM_DAILY_GAMES` and rebuilds missing meaning vectors.
- Gemini Flash Lite (`GEMINI_LITE_MODEL`, same key) joins the providers. Each job has its
  own order: reviews on Flash Lite, quick calls on Groq then Flash Lite with little
  thinking and short timeouts, the judge on Flash (its free quota is about 20 calls a day).
  A daily 429 rests a provider until its quota resets. Embeddings default to `gemini-embedding-001` (the free quota of
  `gemini-embedding-2` runs out at once); changing the model wipes the vectors.
  `LLM_DAILY_GAMES` defaults to 400 and counts review analyses only (the scout and
  the judge add their tokens, not games).
- Nothing about the player is kept but the Steam region and the current session; the
  profile data of older versions is purged once.
- Daily upkeep in the background worker: a gzipped database backup in `data/backups`
  (the last 3 kept), the cover cache trimmed to 60 days and 150 MB, vectors of games
  that are gone, stale non-Steam cards and old queue rows pruned, the SQLite log
  checkpointed.
- Deploy: the systemd unit is sandboxed (`ProtectSystem=strict`, only `data/` writable,
  no new privileges, private `/tmp`); a bad `.env` exits with code 78 and is not
  restarted in a loop; the bot stops within 15 seconds. `install.sh` runs the offline
  tests with `GF_TIMING=0`, snapshots the database before the restart and waits for
  the bot to start polling.

## 0.2.0 — 2026-10-07

The bot now helps pick a game for right now instead of learning a player's taste.

- No onboarding and no stored profile. The player writes what they want in their own
  words ("like Hollow Knight, but easier, for a couple of evenings", "co-op with a friend, not a shooter") or
  taps a mood button (like a game, for an evening, something long, with a friend, chill,
  challenge, story, hidden gems, surprise me). One free-LLM call reads the message into
  a request: reference games and games to avoid, mood, feel targets, hours, co-op,
  dealbreakers, tags to pull towards or push away from, and the experience in the
  player's words; keyword rules read it when no model can, and always add dealbreakers
  and co-op they find.
- "What exactly hooked you in <game>?": when a reference game is named, the bot offers up to six
  options specific to that game (from what its reviewers praise, its strongest feel axes
  and player tags, or from the LLM card), as toggle buttons or a free-text answer. The
  picked aspects steer the request; the reference's other traits barely count, and what
  the player disliked counts against.
- Results come as image cards: a selection banner and three cards with the cover, match
  with the request, review freshness, feel bars, real genres, length, price and Steam
  Deck status; the caption gives the reason, a real player's quote and warnings. A game
  breakdown is a larger card with all 12 axes, praise and complaints.
- Refining in the moment: "Shorter", "Easier", "Harder", "More story", "Calmer",
  "Something else", "Three more". What the request asks for outright is a filter; when exact
  matches run short, the closest games are added and marked as a compromise.
  "Played it" and "Not this" only drop a game from the current selection.
- Nothing about the player is remembered between requests: only the current request and
  the games shown for it are kept, and the next request replaces them. Removed: the
  questionnaire, the taste profile, learning from ratings, the two-week rest for shown
  games, the Steam library import, and the `/profile`, `/add`, `/steam` and `/reset`
  commands. New: `/game` for a breakdown. The menu is "🎮 Find", "🔎 Game breakdown",
  "❔ How it works".
- Reference titles are matched strictly (Alan Wake 2 never becomes Alan Wake). Games
  that are not on Steam (Epic or console exclusives, old games) work as references
  through IGDB (`IGDB_CLIENT_ID`, `IGDB_CLIENT_SECRET`, a free Twitch app) or a card from
  the free LLM with tags, feel axes, loved aspects and the Steam games they are compared
  to. Picks still come from the Steam catalog.
- GOG reviews: when a game is on GOG, up to 20 of its reviews join the sample the model
  reads, and its GOG rating is shown next to Steam's. No key needed.
- Meaning-based matching: free Gemini embeddings (`GEMINI_EMBED_MODEL`, default
  `gemini-embedding-2`) of each game's experience and of short review snippets; the
  player's words are matched against them and the closest snippet is quoted on the card.
- A co-play graph from real Steam players: with `STEAM_API_KEY`, the background worker
  reads the public libraries of reviewers who loved a game and keeps only per-game
  totals, corrected for popularity. No Steam ids or libraries are stored.
- An LLM judge reads the passports of the top dozen candidates next to the request and
  picks the final three, with a reason and a risk for each.

## 0.1.0 — 2026-10-07

First release.

- A passport for every game, from what players say in recent reviews: real genres as
  players name them, 12 feel axes from 0 to 10 (pace, difficulty, story, freedom,
  complexity, grind, tension, combat, exploration, social, length, replay), what is
  praised, what is criticised, quality flags, and the game now versus at launch.
- Every complaint is tagged as quality (bugs, performance, monetization, abandoned
  development, servers) or taste, with an axis and a direction: "too slow" counts for
  a game when the player likes slow games. Quality complaints always count against.
- Review numbers split the ways that matter: recent and all time, players with 10+
  hours, negatives from players who quit within 2 hours. Rankings use the Wilson lower
  bound.
- Free LLM APIs read up to 60 reviews (negatives over-sampled, the real share passed
  separately) and fill a fixed schema in Russian: Google Gemini first, Groq as the
  fallback (25 reviews, for its small per-minute token quota). A provider that hits a
  rate limit rests for 90 seconds, then 10 minutes, while the other one answers. Both
  keys are optional (`GEMINI_API_KEY`, `GROQ_API_KEY`; models in `GEMINI_MODEL` and
  `GROQ_MODEL`). Without keys, past the daily cap (`LLM_DAILY_GAMES`, 100 by default)
  or when neither answers, passports come from player tags and review keywords.
- Sources: Steam reviews and store facts, SteamSpy player tags, Steam Deck Verified
  and ProtonDB, and Reddit discussions when a Reddit app is configured.
- Taste profile from loved, disliked and dropped games, five quick questions,
  dealbreakers (MTX, online only, early access, no Russian, no Russian audio, Denuvo,
  horror, 18+, doesn't run on Steam Deck: unsupported per Steam or borked on ProtonDB)
  and an optional Steam library import weighted by playtime.
- Picks by mood (any, evening, story, chill, challenge, co-op, hidden gems, new), three
  at a time and unlike each other, each with why it fits. Feedback buttons under every
  pick refine the profile; shown games rest for two weeks.
- Learning from ratings: after a few 👍 and 👎 on picks, each part of the score (tags,
  feel, quality, complaints) gets a per-player weight from 0.5× to 1.5×, depending on
  how well it predicted what the player liked.
- A background worker grows the catalog once a day from Steam's top sellers, top rated
  and popular new lists, and reads the next likely candidates ahead. It steps aside
  while a player is waiting.
- Private by default: owners, `ALLOWED_IDS`, `/allow` and a one-tap button when someone
  new writes. `/stats` for owners: catalog, analysis queue, model usage per day.
- Russian interface in the SILVERHAND style with an animation per menu screen; the
  avatar, banner and @BotFather description animation in `assets/brand/`.
- `deploy/install.sh` and a systemd unit capped at 300 MB of RAM and 30% of a CPU.
- Offline test suites, run in CI on Python 3.10 and 3.13.
