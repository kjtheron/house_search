# To-do

- [ ] **Test on the Pi 5.** Full `housebot run --dry-run` with all towns, check run time (< 15 min target) and memory.
- [ ] **First real run.** Decide: send all current matches, or seed the DB silently and alert only on changes after.
- [ ] Phase 10: install PicoClaw skill, restrict its exec tool to `housebot`.
- [ ] Phase 11: email-alert adapter (IMAP).
- [ ] Phase 12: nightly SQLite backup, README setup steps (Pi, PicoClaw, Ollama, Telegram).
- [ ] Use rates/levies from details as a same-house signal when a pair was missed at card time.
- [ ] Distance/commute filter from Private Property GPS coordinates.
