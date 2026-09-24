# house_search

A daily house-hunting bot for the Western Cape, South Africa. Runs on a Raspberry Pi.

- Collects for-sale listings from Property24, Private Property, Pam Golding, Seeff, Harcourts and RE/MAX, newest first, across the whole province or a list of towns. The same house on several sites is alerted once.
- Filters them against your search criteria (towns, excluded towns, price, beds, size, keywords).
- Stores everything in SQLite and sends **new matches and price changes** to Telegram, once per house per price.
- Lets you chat with a [PicoClaw](https://github.com/sipeed/picoclaw) agent (Ollama Cloud LLM) to favourite, hide and search listings.

The scraping and notifying core is plain Python on a systemd timer. The LLM only sits on top as a chat interface.

> **Status:** work in progress. See [docs/plan.md](docs/plan.md) for the build spec and [docs/todo.md](docs/todo.md) for what's next.

Personal use only. Collection is polite and low-volume; respect each site's Terms of Use.

## Quick start

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/kjtheron/house_search.git && cd house_search
uv sync
cp .env.example .env              # add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
cp config.example.yaml config.yaml && $EDITOR config.yaml   # what you're looking for
uv run housebot config show       # check the config
uv run housebot run --dry-run     # scrape + print the alerts, send nothing
```

Each site searches either a list of towns (`locations: {Town: site ID}`, best for a few towns) or the
whole province (`locations: {}`, best for "anywhere"), newest first, so after the first run it reads
only a few pages a day. The sites pre-filter by price and beds in the URL; `config.yaml` filters the rest. `towns` filters what you get alerted about. `housebot towns add/rm` keeps
`towns` and the town-mode `locations` in step, looking up each site's ID for you. Each run also re-checks a few favourites and matches (`check.per_run`) and tells you if a
favourite is sold or under offer.

New matches get their listing page fetched once (sizes, storeys, rates, levies, features, listing date)
before you're alerted, so filters like `storeys_max` and `require_features` apply. Up to `details.per_run`
pages per run; a backlog (e.g. after a backfill) drains over the next runs.

`--dry-run` prints the Telegram messages instead of sending them and records nothing as sent, so you can repeat it.
It still fetches pages from the sites. Drop `--dry-run` to send for real. Delete `data/` to start from an empty database.

## Sources

| Site | `sources:` name | Province search | Town search (`locations`) |
|---|---|---|---|
| Property24 | `property24` | `province_id: 9` | town ID from the search URL |
| Private Property | `privateproperty` | `province_id: 4` | town ID from the search URL |
| Pam Golding | `pamgolding` | `province_id: 2108` | town ID from the search URL |
| Seeff | `seeff` | national feed, Western Cape areas kept | area name, ID `0` |
| Harcourts | `harcourts` | national feed, Western Cape areas kept | area name, ID `0` |
| RE/MAX | `remax` | newest 240 listings in the province | town name, ID `0` |

Seeff and Harcourts run on the same platform (Propdata), which has no province search. A province
search reads their national feed (all types, price and beds filtered by the site) and keeps the areas
listed in `PROVINCE_AREAS` in `housebot/adapters/propdata.py`. Only the Western Cape is listed.

RE/MAX shows only its newest 240 listings per page (all types, about two months) and ignores URL
filters, so it costs one request per run. To reach older RE/MAX listings, backfill a town: that
page holds the town's newest 240. RE/MAX listings are never marked gone just because they drop off the page.

The same house on several sites (same type, beds, baths, similar suburb, and the same price, rates or
size within 2%) is grouped, so you get one alert. So is one house listed by several agencies on the same
site (same price, size within 2%, different agency). Its details are fetched once and copied to the others.
Set `enabled: false` to switch a site off.

All requests go through one polite client: a random 15–30 s wait between requests to the same site
(`http.*`), and a site that answers 403/429/503 or a bot-check page is stopped for the rest of the run.

### Adding a site to an existing database

Backfill only the new site, then run as normal:

```bash
uv run housebot backfill --source pamgolding --source seeff --source harcourts --pages 20
uv run housebot backfill --source remax              # newest 240 in the province
uv run housebot backfill Stellenbosch --source remax # older RE/MAX listings, one town at a time
uv run housebot run
uv run housebot details --limit 10 # Clear detailed backlog
```

## Using it from the command line

```bash
uv run housebot search --matching                    # latest listings that pass config.yaml
uv run housebot search --since today --matching
uv run housebot search --town Paarl --price-max 3500000 --beds-min 4
uv run housebot search --since 7d --text pool        # new this week, "pool" in the text
uv run housebot search --source remax --matching     # one site only
uv run housebot show 142                             # details, price history, same house on other sites
uv run housebot fav add 142 --rating 4 --note "big garden"
uv run housebot fav list
uv run housebot hide 142                             # never alert about this house again
uv run housebot stats                                # counts per town, median price, last runs
uv run housebot sources                              # is each scraper healthy?
uv run housebot towns list                           # town filter (empty = whole province)
uv run housebot towns add Paarl                      # alert for Paarl too (+ its site IDs in town mode)
uv run housebot towns add Paarl --history            # ...and read all of Paarl's current listings once
uv run housebot backfill --pages 20                  # one-off: go ~20 pages further back (no early stop)
uv run housebot backfill --source seeff --pages 20   # ...on one site only (repeat --source for more)
uv run housebot towns rm Paarl                       # stop, and delete Paarl's listings (favourites kept)
uv run housebot check 142                            # still for sale? (sold / under offer / gone)
uv run housebot check --favs --bg                    # --bg: run in background, Telegram message when done
uv run housebot details --limit 10                   # fetch waiting listing pages now (normally details.per_run)
uv run housebot features                             # feature names seen, for require/exclude_features
uv run housebot rematch                              # after editing config.yaml: re-check stored listings now
```

Add `--json` to any command for machine-readable output. `uv run housebot --help` lists everything.

## Telegram alerts

housebot sends alerts through the Bot API using **the same bot as PicoClaw**. It only sends, it never reads
updates, so it doesn't interfere with PicoClaw. Replies you type ("fav 142") go to PicoClaw.

In `.env`:

- `TELEGRAM_BOT_TOKEN`: the `token` from PicoClaw's `~/.picoclaw/config.json` (`channel_list.telegram`).
- `TELEGRAM_CHAT_ID`: your Telegram user ID, the number in PicoClaw's `allow_from` (or ask `@userinfobot`).

## Connecting an existing PicoClaw

Assumes PicoClaw already chats with you on Telegram. Replace `~/house_search` with where you cloned the repo.

1. Install the skill and the wrapper into PicoClaw's workspace:
   ```bash
   mkdir -p ~/.picoclaw/workspace/skills ~/.picoclaw/workspace/bin
   cp -r picoclaw/skills/housebot ~/.picoclaw/workspace/skills/
   cp picoclaw/bin/housebot ~/.picoclaw/workspace/bin/
   ```
   The wrapper exists because PicoClaw's `restrict_to_workspace` only allows commands inside the workspace.
   If the clone isn't at `~/house_search`, edit `HOUSEBOT_DIR` in the copied wrapper.
2. Check the wrapper works: `~/.picoclaw/workspace/bin/housebot stats`
3. Restart PicoClaw (`picoclaw gateway`, or its systemd service) so it loads the skill.
4. Optional: in `~/.picoclaw/config.json`, limit `tools.exec.custom_allow_patterns` to the housebot wrapper.
   Key names change between PicoClaw releases, so check your version's `config.example.json`.

Run PicoClaw and housebot as the same user so both can read `data/housebot.db`.

## Chatting on Telegram

Each alert ends with the listing number, e.g. `#142`. Message the bot in plain language:

| You type | PicoClaw runs |
|---|---|
| `fav 142` or "I like 142, great garden" | `housebot fav add 142 --note "great garden"` |
| `hide 142` or "not interested in 142" | `housebot hide 142` |
| "show my favourites" | `housebot fav list` |
| "3-bed houses in Stellenbosch under R4m" | `housebot search --town Stellenbosch --beds-min 3 --price-max 4000000` |
| "tell me about 142" | `housebot show 142` |
| "what's new this week?" | `housebot search --since 7d --matching` |
| "is the scraper working?" | `housebot sources` |
| "also look in Paarl" / "add Paarl with full history" | `housebot towns add Paarl [--history]` |
| "stop looking in Paarl" | `housebot towns rm Paarl` |
| "is 142 still available?" | `housebot check 142` |

PicoClaw never starts the daily run (only `--history` backfills when you ask); that comes from the systemd timer in [deploy/](deploy/).

## Daily schedule (Pi)

```bash
sudo cp deploy/housebot.service deploy/housebot.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now housebot.timer
systemctl list-timers housebot.timer      # next run
journalctl -u housebot                    # logs
```

Edit the paths and `User=` in `housebot.service` to match your setup first.
Full Pi setup (OS, user, Ollama, PicoClaw install) comes later.

### Moving the database from a laptop to the Pi

1. On the Pi, update the code first (`git pull && uv sync`).
2. Stop the timer on the Pi (`sudo systemctl stop housebot.timer`). Make sure no housebot command runs on the laptop.
3. Copy `data/housebot.db` and `config.yaml` to the Pi. `config.yaml` is not in git.
4. Start the timer again (`sudo systemctl start housebot.timer`).

## License

MIT
