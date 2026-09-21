# To-do

- [ ] **Test on the Pi 5.** Full `housebot run --dry-run` with all towns, check run time (< 15 min target) and memory.
- [ ] **Detail-page fetch.** Cards often lack floor size and full description. Fetch the listing page only for
      new / price-changed matches to fill `floor_m2`, `description`, `agency`. Decide after the Pi test,
      because it adds requests (5-10 s each).
- [ ] **First real run.** Decide: send all current matches, or seed the DB silently and alert only on changes after.
- [ ] Phase 10: install PicoClaw skill, restrict its exec tool to `housebot`.
- [ ] Phase 11: email-alert adapter (IMAP).
- [ ] Phase 12: nightly SQLite backup, README setup steps (Pi, PicoClaw, Ollama, Telegram).
