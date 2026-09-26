---
name: housebot
description: Query and manage the owner's Western Cape house-listing database. Use for anything about houses, listings, favourites, prices, towns, the scraper, or "#<number>" references, and for short commands like "stats", "fav 142", "hide 142", "show 142", "search".
---
Run commands with the exec tool, using the wrapper `bin/housebot` in the workspace.
Always pass --json and summarise the result briefly.

- "fav 142" / "I like 142" → bin/housebot fav add 142 [--note "..."] [--rating 1-5] --json
- "unfav 142" → bin/housebot fav rm 142 --json
- "hide 142" / "not interested in 142" → bin/housebot hide 142 --json
- "unhide 142" → bin/housebot unhide 142 --json
- "show my favourites" → bin/housebot fav list --json
- "houses in Paarl under 3.5m with 4 beds" → bin/housebot search --town Paarl --price-max 3500000 --beds-min 4 --matching --json
- "anything with a pool?" → bin/housebot search --text pool --matching --json
- "tell me about 142" → bin/housebot show 142 --json
- "what's new this week" → bin/housebot search --since 7d --matching --json
- "stats" / "how's the market" → bin/housebot stats --json
- "is the scraper working" → bin/housebot sources --json
- "which towns am I watching" → bin/housebot towns list --json
- "add Paarl" / "also look in Paarl" → bin/housebot towns add Paarl --bg --json
- "add Paarl with full history" → bin/housebot towns add Paarl --history --bg --json
- "remove Paarl" / "stop looking in Paarl" → bin/housebot towns rm Paarl --json
  (deletes that town's listings except favourites; if the town list becomes empty, warn the owner that
  alerts now cover the whole province)
- "is 142 still available?" → bin/housebot check 142 --json   (one or two IDs: run directly)
- "check my favourites" → bin/housebot check --favs --bg --json
- "what features can I filter on" → bin/housebot features --json
- "fetch the waiting details" → bin/housebot details --bg --json

`--bg` runs slow commands in the background; the owner gets a Telegram message when done.
Tell the owner it has started. Use --bg for --history, backfill, and checks of more than 2 listings.

`towns add` always runs with --bg (a new town may need a slow site lookup); if the Telegram result says the town is unknown, offer the suggested spellings.

Never edit the database file directly. Never run `housebot run` unless the owner explicitly asks.
Always include the listing URL and #id in answers.
