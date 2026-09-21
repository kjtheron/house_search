# house_search

A daily house-hunting bot for the Western Cape, South Africa. Runs on a Raspberry Pi.

- Collects for-sale listings from Property24 and Private Property (or their email alerts).
- Filters them against your search criteria (towns, price, beds, size, keywords).
- Stores everything in SQLite and sends **new matches and price changes** to Telegram, once per house per price.
- Lets you chat with a [PicoClaw](https://github.com/sipeed/picoclaw) agent (Ollama Cloud LLM) to favourite, hide and search listings.

The scraping and notifying core is plain Python on a systemd timer. The LLM only sits on top as a chat interface.

> **Status:** work in progress. See [plan.md](plan.md) for the build spec. Setup instructions come later.

Personal use only. Collection is polite and low-volume; respect each site's Terms of Use.

## License

MIT
