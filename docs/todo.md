# To-do

## Done
- [x] **Test on the Pi 5.** Runs on the Pi each day; a run takes 5 to 7 minutes.
- [x] **First real run.** Live since 2026-09-27; alerts go to Telegram each night.
- [x] **PicoClaw skill + wrapper (phase 10).** Installed; `hide` from Telegram works.

## Open (Pi)
- [ ] **PicoClaw exec limit.** Exec is limited to the workspace, not to `housebot` only (`custom_allow_patterns` is empty). Test one `--bg` job.
- [ ] **Hardening (phase 12).** Nightly SQLite backup; README setup steps (Pi, PicoClaw, Ollama, Telegram).
- [ ] **Ethernet cable.** The cable links at 100 Mb/s only. After a new cable: remove `/etc/cron.d/eth0-100mbit` and the speed limit in `Wired connection 1`.
- [ ] **NVMe freeze watch.** No freeze since PCIe Gen 2 and no drive deep sleep (2026-10-06). If it comes back: add `nvme.max_host_mem_size_mb=32` to `cmdline.txt`.

## Later
- [ ] Email-alert adapter (phase 11, IMAP) as a fallback if a site blocks scraping.
- [ ] Distance/commute filter from Private Property GPS coordinates.
- [ ] Duplicate alerts that can still occur: no erf size on one site; two agencies with clearly different prices.
- [ ] Some old same-house groups hold listings from different towns (e.g. Sandbaai and Darling); find the cause.
