# Music Discovery Architecture
## Overview
`discover.py` selects popular EDM, 80s, and 90s candidates, validates metadata and official recordings, and atomically publishes accepted audio into the Jellyfin music library. `lyrics_bridge.py` separately hands indexed additions to the existing Finamp lyrics worker.
## State schema
`history.sqlite3` contains `daily` aggregate counters, current `tracks`, expiring `cache` rows, persistent `settings`, and 90-day `attempt_history`. `last-run.json` stores the most recent report. Each execution also builds an in-memory summary with `searched`, `already_present`, `failures`, and `added` lists; its count map and added-track list are written to the final run report.
## External interfaces
- Last.fm supplies popularity candidates.
- MusicBrainz validates recordings and decade release years.
- yt-dlp fetches verified official recordings.
- Jellyfin's SQLite database is read-only for duplicate detection; the Jellyfin HTTP API is used only by the lyrics bridge.
- Telegram `sendMessage` receives one scheduled-run summary through `TELEGRAM_API_KEY` and `TELEGRAM_CHAT_ID` loaded by systemd from `/etc/music-discovery/telegram.env`.
## Scheduled flow
`music-discovery.timer` starts `music-discovery.service` daily. The service executes `discover.py --telegram-summary`, then starts the independent lyrics service. The summary is generated from current-run outcomes before Jellyfin needs to index files. Telegram delivery is best-effort: missing credentials or API failure is reported in the log and `last-run.json`, but does not change the acquisition result.
## Business rules
Official artist/title matches from artist, Topic, VEVO, or configured trusted-publisher sources are accepted after ignoring parenthesized title text; non-parenthesized title differences remain distinct. Already-owned recordings, non-parenthesized altered versions, invalid decade metadata, and failed downloads do not count toward the per-run success goal. Failed candidates have a seven-day retry window; provider rate-limit cooldowns remain separate. Published files are never overwritten.
## Constraints
The Telegram report describes one automated run, normally the daily run; it is not a query of all Jellyfin additions in the last 24 hours. Telegram credentials never appear in repository configuration, output, or logs. The protected environment file must be root-owned and mode `0600`.
