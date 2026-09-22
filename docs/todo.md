# To-do

## Before going live (Pi)
- [ ] **Test on the Pi 5.** Copy `data/` over, run `housebot run --dry-run`; check run time and memory.
- [ ] **First real run.** Decide: send all current matches, or mark them as sent and alert only on new ones.
- [ ] **PicoClaw (phase 10).** Install the skill + wrapper, restrict its exec tool to `housebot`, check `--bg` jobs.
- [ ] **Hardening (phase 12).** Nightly SQLite backup; README setup steps (Pi, PicoClaw, Ollama, Telegram).

## Later
- [ ] Email-alert adapter (phase 11, IMAP) as a fallback if a site blocks scraping.
- [ ] Distance/commute filter from Private Property GPS coordinates.
