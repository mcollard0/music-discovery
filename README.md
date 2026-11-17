# Music discovery

Discovers popular songs missing from Jellyfin's Music library, downloads individually matched official recordings, and hands new items to the existing Finamp Genius lyrics program. Intended for a personal library; configure sources you are authorized to download.

The source is this directory. Installed code is `/opt/music-discovery`; persistent state is `/var/lib/music-discovery`. Jellyfin's active database is opened **read-only**, with WAL visibility. The existing server and Finamp plugin are not reinstalled.

## Daily operation

`music-discovery.timer` runs around the time and timezone in your private configuration, following daylight saving time. It performs one catch-up after missed execution. The downloader runs as the configured download user, with Jellyfin group access. Systemd and a process lock prevent overlapping runs. The drive must be mounted and have at least 5 GiB free.

The goal is **20 successful downloads per run**, for both manual invocations and the daily timer. A manual invocation can override this with `--song-limit N`. Earlier successes today do not reduce a new run's goal. Existing songs, invalid candidates and failed downloads do not count toward it; discovery continues looking for replacements. Daily counters remain as statistics. Separate work ceilings allow up to 200 fresh candidate checks and 400 application requests per invocation, and 800 application requests per local calendar day. Those ceilings, a provider cooldown or exhausted sources can leave the goal unmet; the report records the stopping reason. Each application request is spaced by three seconds; yt-dlp also spaces its internal extractor requests by three seconds. Downloads wait a randomized 30–60 seconds. HTTP 429 ends discovery and persists a cooldown honoring Retry-After. Network retries are bounded. Failed candidates wait seven days before another lookup.
The scheduled service sends one best-effort Telegram summary after each run. It reports tracks searched, already present, failed, and added, then lists the added tracks from its in-memory run summary. It does not wait for Jellyfin indexing or query the library afterward. A failed Telegram delivery is logged but does not affect downloads or the service result. Manual and preview commands do not send a summary unless `--telegram-summary` is explicitly passed.

Candidates come from public Last.fm tag popularity lists, rotating EDM subgenres and the 80s/90s lists. Discovery expands through the first five popular chart pages when needed, with per-list cursors and category rotation. EDM membership is based on the source tag, which is community supplied; this is not an independent classifier. Decade tracks require an exact MusicBrainz artist/title match with a first release date in 1980–1989 or 1990–1999. Unknown dates are skipped. Popularity reflects these lists, not a universal chart.

Video-title matching ignores case, punctuation, and parenthesized text; other title words still distinguish versions. MusicBrainz recording identifiers and successful local download history provide additional duplicate checks. The top 10 YouTube results are checked by default and can be changed with `youtube_search_results`. Unambiguous official artist, Topic, VEVO, or configured trusted-publisher sources are accepted; covers, non-parenthesized altered-speed recordings, remixes with different titles, sets and compilations are skipped. This conservative policy can skip legitimate songs.

The default format is MP3 V0, converted from the best available audio; conversion cannot improve source quality. Files receive artist/title/album/genre and known year/recording tags. Unknown album metadata uses a separate `Title (Single)` folder per song; no track numbers or album dates are invented. Filenames are sanitized. Audio is staged privately on the music drive, fully decoded by ffmpeg, and published atomically without overwriting an existing file. Artwork is not fetched by this version.

## Genius lyrics

`music-discovery-lyrics.service` runs as `jellyfin`, reads the existing protected credential file and uses `/var/lib/jellyfin/finamp-lyrics/lyrics_fetcher.py` with its existing shared cache, queue and pacing. No Genius implementation or tokens are copied into the downloader.

After an item is indexed, it queues `--priority ITEM_ID --top 0 --upload --enqueue-only`. Existing `.lrc`, `.elrc`, `.txt` and server lyrics are preserved by skipping the handoff. The existing worker can replace lyrics if another writer creates them after enqueueing; this integration does not change that worker's publication policy. Genius provides untimed text through the existing worker's song-page extraction. Site access/layout changes can prevent retrieval.

The already running continuous worker processes new priorities. If it is inactive, the bridge starts the existing Finamp batch service. A separate 15-minute bridge timer retries items awaiting Jellyfin indexing. It notifies Jellyfin of individual completed files rather than scanning the entire library. Lyric failure does not redownload audio. Handoff markers record queued/skipped items separately from audio history; queued means accepted for processing, not proof that Genius found lyrics.

## Commands

First copy `config.example.json` to ignored `config.json` and configure your paths,
accounts, mount, timezone and integration. Install Python dependencies from
`requirements.txt` and install yt-dlp/ffmpeg separately. The configured accounts
must already exist. Run a preview before enabling automatic downloads.

Installation requires root: it links /opt/music-discovery to this checkout, sets ownership on state
directories, writes systemd units, enables the daily timer without starting it,
and enables/starts the lyrics retry timer. Starting the daily timer can trigger
catch-up downloads. Review configuration and installers before running:


```bash
sudo python3 install.py
sudo systemctl start music-discovery.timer
```

Download one explicitly selected official recording:

```bash
python3 /opt/music-discovery/discover.py \
  --artist MIMIFY --title 'I Waited too Long (Original Mix)' \
  --url 'https://www.youtube.com/watch?v=F2Iga1V4cqo' --category edm
sudo systemctl start music-discovery-lyrics.service
```

Without `--category`, an explicit `--artist/--title` request has no category: it skips the decade year check and the MP3 gets no genre tag (chart discoveries are still tagged with their source tag). Pass `--category 80s|90s|edm` to validate and tag it as one.

### Song requests from other services (GridShift)

A containerized service cannot run `discover.py` on the host, so it queues requests instead. It writes `<id>.json` (`{"artist", "title", "dry_run"?, "category"?}`, id `[A-Za-z0-9_-]{1,80}`) atomically into `request_dir` (default `/var/lib/music-discovery/requests`, `michael:jellyfin` 2770). `music-discovery-request.path` notices it and starts `music-discovery-request.service`, which runs `discover.py --process-requests` as the download user with the same sandbox as the daily run. Each request is a one-song run that waits for an active daily run instead of skipping. The answer is written to `results/<id>.json` with `outcome` = `added | already_present | found (dry run) | failed | deferred | stopped | invalid | error`, plus `detail`, `url` and `path`. The request file is always removed, even when it is rejected, so the path unit cannot loop. Links are not followed. Unread results are pruned after 7 days. `install.py` creates the folders and enables the path unit.

Preview discovery (looks for 20 eligible songs; requests count toward the network ceiling, while previews never count as downloads or enqueue lyrics):

```bash
python3 /opt/music-discovery/discover.py --dry-run
python3 /opt/music-discovery/discover.py --song-limit 50
python3 /opt/music-discovery/discover.py --status
systemctl list-timers 'music-discovery*'
journalctl -u music-discovery.service -u music-discovery-lyrics.service --since today
```

Copy `config.example.json` to `config.json` and set your media paths, service accounts, timezone, schedule, and integration locations. `config.json` is private and ignored by Git; only the example is shared. Configuration lives in `config.json`. `/opt/music-discovery` is a symlink to this checkout, so code and configuration edits take effect on the next run. Rerun the installer only to change systemd units or folders. The installer makes `config.json` `michael:jellyfin` 0640 so the lyrics service can read it. `last-run.json` contains the result, success goal, daily counters and stopping reason. `history.sqlite3` keeps the latest song state in `tracks`, with a unique index on normalized artist and title. `attempt_history` records individual outcomes (including skips) by artist/title, mode, timestamp, reason and URL; compound indexes cover artist/title/time and status/time. Attempt records are retained for 90 days. Existing history is migrated without resetting counters or completed downloads. Verified preview candidates are retained for 24 hours and considered first by a live run, avoiding repeated metadata searches. Failed attempts honor their stored retry windows. Handoff files and lyric markers are separate; no audio or lyric bodies are stored in SQLite. Systemd manages log retention.

Telegram credentials stay outside the project in the environment file named by `telegram_summary.environment_file`, normally `/etc/music-discovery/telegram.env`. It must be owned by `root`, mode `0600`, and contain `TELEGRAM_API_KEY` and `TELEGRAM_CHAT_ID`; the key names are configurable in `config.json`. The installer refuses to install a unit without that protected file. Verify delivery without downloading music:

```bash
python3 /opt/music-discovery/discover.py --test-telegram-summary
```

Disable daily discovery with `sudo systemctl disable --now music-discovery.timer`. Stop the active downloader with `sudo systemctl stop music-discovery.service`. Previously published music remains. The separate lyric timer can be disabled similarly without stopping existing Finamp services.

## Validation

Run `python3 -m unittest -v` in this directory. Tests exercise persisted limits, official recording matching, decade validation, read-only live-WAL catalog access, path handling, and preservation of timed lyric sidecars.
