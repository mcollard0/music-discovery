#!/usr/bin/env python3
"""Bounded daily music discovery, read-only Jellyfin deduplication and downloads."""
import argparse
import contextlib
import fcntl
import hashlib
import html
import itertools
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from mutagen import MutagenError
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3

LOG = logging.getLogger("music-discovery")
CATEGORIES = ("edm", "80s", "90s")
TELEGRAM_MESSAGE_LIMIT = 4096
FAILED_CANDIDATE_RETRY_SECONDS = 7 * 86400
DEFAULT_YOUTUBE_SEARCH_RESULTS = 10
DEFAULT_TRUSTED_OFFICIAL_CHANNELS = ("UKF Dubstep",)
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
REQUEST_MAX_BYTES = 4096
REQUEST_FIELD_MAX = 200
REQUEST_RESULT_RETENTION_SECONDS = 7 * 86400


class StopRun(RuntimeError):
    pass


def normalize(value):
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", value))

def matching_title(value):
    return normalize(re.sub(r"\([^)]*\)", " ", value))



def identity(artist, title):
    # Version words survive normalization: live/remix are different recordings.
    return hashlib.sha256((normalize(artist) + "\0" + normalize(title)).encode()).hexdigest()


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value[:160] or "Unknown"


def atomic_json(path, value, mode=0o640):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

def summary_track(track, detail=None):
    item = {"artist": track.get("artist", "Unknown"), "title": track.get("title", "Unknown"),
            "category": track.get("category", "unknown")}
    if detail:
        item["detail"] = str(detail)
    return item


def new_summary():
    return {"searched": [], "already_present": [], "failures": [], "added": []}


def summary_counts(summary):
    return {key: len(summary[key]) for key in ("searched", "already_present", "failures", "added")}


def telegram_message(summary, heading="Music discovery daily summary"):
    counts = summary_counts(summary)
    lines = [f"<b>{html.escape(heading)}</b>",
             f"Searched: {counts['searched']}",
             f"Already present: {counts['already_present']}",
             f"Failures: {counts['failures']}",
             f"Added: {counts['added']}"]
    if summary["added"]:
        lines.append("")
        lines.append("<b>Added tracks</b>")
        for index, track in enumerate(summary["added"]):
            text = f"• {html.escape(track['artist'])} — {html.escape(track['title'])}"
            if track.get("category"):
                text += f" <i>({html.escape(track['category'])})</i>"
            remaining = len(summary["added"]) - index - 1
            suffix = f"\n… {remaining} additional track(s) omitted" if remaining else ""
            if len("\n".join(lines + [text]) + suffix) > TELEGRAM_MESSAGE_LIMIT:
                if remaining:
                    lines.append(f"… {remaining + 1} additional track(s) omitted")
                break
            lines.append(text)
    return "\n".join(lines)


def send_telegram_summary(config, summary, heading="Music discovery daily summary"):
    settings = config.get("telegram_summary", {})
    token = os.environ.get(settings.get("bot_token_env", "TELEGRAM_API_KEY"))
    chat_id = os.environ.get(settings.get("chat_id_env", "TELEGRAM_CHAT_ID"))
    if not token or not chat_id:
        LOG.warning("Telegram summary not sent: configured environment variables are unavailable")
        return False
    try:
        response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data={"chat_id": chat_id, "text": telegram_message(summary, heading),
                                       "parse_mode": "HTML", "disable_web_page_preview": "true"},
                                 timeout=(10, 30))
        response.raise_for_status()
        if not response.json().get("ok"):
            LOG.warning("Telegram summary was rejected by the API")
            return False
    except (requests.RequestException, ValueError):
        LOG.warning("Telegram summary failed: %s", sys.exc_info()[0].__name__)
        return False
    LOG.info("Telegram summary sent")
    return True


class State:
    def __init__(self, directory, timezone, mode="download"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.today = datetime.now(ZoneInfo(timezone)).date().isoformat()
        self.mode, self.run_id = mode, uuid.uuid4().hex
        self.db = sqlite3.connect(self.directory / "history.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS daily(day TEXT PRIMARY KEY, downloads INTEGER DEFAULT 0,
                attempts INTEGER DEFAULT 0, candidates INTEGER DEFAULT 0, requests INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS tracks(key TEXT PRIMARY KEY, artist TEXT, title TEXT,
                category TEXT, status TEXT, path TEXT, url TEXT, recording TEXT,
                retry_at REAL DEFAULT 0, detail TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, expires REAL, body TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS attempt_history(id INTEGER PRIMARY KEY, run_id TEXT,
                artist TEXT NOT NULL, title TEXT NOT NULL, artist_key TEXT NOT NULL,
                title_key TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
                attempted_at REAL NOT NULL, detail TEXT, url TEXT);
            CREATE INDEX IF NOT EXISTS attempt_artist_title_time
                ON attempt_history(artist_key,title_key,attempted_at DESC);
            CREATE INDEX IF NOT EXISTS attempt_status_time ON attempt_history(status,attempted_at DESC);
            CREATE INDEX IF NOT EXISTS attempt_run_status ON attempt_history(run_id,status);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(tracks)")}
        for column in ("artist_key", "title_key", "payload"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE tracks ADD COLUMN {column} TEXT")
        for row in self.db.execute("SELECT * FROM tracks WHERE artist_key IS NULL").fetchall():
            self.db.execute("UPDATE tracks SET artist_key=?,title_key=? WHERE key=?",
                            (normalize(row["artist"]), normalize(row["title"]), row["key"]))
            self.db.execute("""INSERT INTO attempt_history(run_id,artist,title,artist_key,title_key,
                mode,status,attempted_at,detail,url) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("migration", row["artist"], row["title"], normalize(row["artist"]),
                 normalize(row["title"]), "legacy", row["status"], row["updated"], row["detail"], row["url"]))
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS track_artist_title ON tracks(artist_key,title_key)")
        self.db.execute("UPDATE tracks SET retry_at=MAX(retry_at,updated+?) WHERE status='error'",
                        (FAILED_CANDIDATE_RETRY_SECONDS,))
        self.db.execute("INSERT OR IGNORE INTO daily(day) VALUES(?)", (self.today,))
        self.db.execute("DELETE FROM attempt_history WHERE attempted_at<?", (time.time() - 90 * 86400,))
        self.db.commit()
        # Import the last preview once, retaining its verified download candidates.
        previous = self.directory / "last-run.json"
        if not self.setting("preview_imported") == "1":
            if previous.exists():
                report = json.loads(previous.read_text())
                if report.get("mode") == "preview":
                    age = time.time() - datetime.fromisoformat(report["time"]).timestamp()
                    if age < 86400:
                        for track in report.get("processed", []):
                            if not self.track(track):
                                self.remember(track, "eligible", detail="Imported verified dry-run candidate")
            self.set_setting("preview_imported", "1")

    def counts(self):
        return dict(self.db.execute("SELECT * FROM daily WHERE day=?", (self.today,)).fetchone())

    def close(self):
        self.db.close()

    def reserve(self, field, limit):
        if field not in ("attempts", "candidates", "requests"):
            raise ValueError(field)
        with self.db:
            changed = self.db.execute(
                f"UPDATE daily SET {field}={field}+1 WHERE day=? AND {field}<?",
                (self.today, limit)).rowcount
        if not changed:
            raise StopRun(f"Daily {field} limit reached")

    def increment(self, field):
        if field not in ("attempts", "candidates"):
            raise ValueError(field)
        with self.db:
            self.db.execute(f"UPDATE daily SET {field}={field}+1 WHERE day=?", (self.today,))

    def remaining(self, goal):
        completed = self.db.execute("SELECT COUNT(*) FROM attempt_history WHERE run_id=? AND status='downloaded'",
                                    (self.run_id,)).fetchone()[0]
        return max(0, goal - completed)

    def track(self, track):
        return self.db.execute("SELECT * FROM tracks WHERE artist_key=? AND title_key=?",
                               (normalize(track["artist"]), normalize(track["title"]))).fetchone()

    def record_attempt(self, track, status, detail=None):
        with self.db:
            self.db.execute("""INSERT INTO attempt_history(run_id,artist,title,artist_key,title_key,
                mode,status,attempted_at,detail,url) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (self.run_id, track["artist"], track["title"], normalize(track["artist"]),
                 normalize(track["title"]), self.mode, status, time.time(), detail, track.get("url")))

    def ready_tracks(self):
        rows = self.db.execute("""SELECT payload FROM tracks WHERE status='eligible'
            AND updated>? AND payload IS NOT NULL ORDER BY updated""", (time.time() - 86400,))
        return [json.loads(r[0]) for r in rows]

    def remember(self, track, status, detail=None, retry=0, path=None):
        with self.db:
            self.db.execute("""INSERT INTO tracks(key,artist,title,category,status,path,url,recording,
                retry_at,detail,updated,artist_key,title_key,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET status=excluded.status,path=excluded.path,
                url=excluded.url,recording=excluded.recording,retry_at=excluded.retry_at,
                detail=excluded.detail,updated=excluded.updated,payload=excluded.payload""",
                (identity(track["artist"], track["title"]), track["artist"], track["title"],
                 track["category"], status, path, track.get("url"), track.get("recording"),
                 time.time() + retry if retry else 0, detail, time.time(),
                 normalize(track["artist"]), normalize(track["title"]), json.dumps(track)))
        self.record_attempt(track, status, detail)

    def cached(self, key):
        row = self.db.execute("SELECT body FROM cache WHERE key=? AND expires>?", (key, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, key, body, seconds):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (key, time.time() + seconds, json.dumps(body)))

    def setting(self, key, default="0"):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, str(value)))


class Catalog:
    def __init__(self, database, root):
        self.keys, self.recordings = set(), set()
        # mode=ro, not immutable: Jellyfin's active WAL must remain visible.
        with contextlib.closing(sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(BaseItems)")}
            if not {"Name", "Artists", "AlbumArtists", "Path", "MediaType"} <= columns:
                raise StopRun("Jellyfin database schema changed; refusing unchecked downloads")
            rows = db.execute("SELECT Name,Artists,AlbumArtists,Path FROM BaseItems WHERE MediaType='Audio'")
            for title, artists, album_artists, path in rows:
                if not path or not Path(path).is_relative_to(root):
                    continue
                for artist in re.split(r"[|;\\]", artists or album_artists or ""):
                    if artist.strip():
                        self.keys.add(identity(artist.strip(), title or ""))
                # Also keep the full artist credit, for collaborations.
                self.keys.add(identity(artists or album_artists or "", title or ""))
            self.recordings = {r[0] for r in db.execute("""SELECT p.ProviderValue FROM BaseItemProviders p
                JOIN BaseItems b ON b.Id=p.ItemId WHERE p.ProviderId='MusicBrainzTrack'
                AND b.MediaType='Audio'""")}

    def contains(self, track):
        return identity(track["artist"], track["title"]) in self.keys or (
            track.get("recording") and track["recording"] in self.recordings)


class DiskCatalog:
    """Inspect only matching artist folders, including files not yet indexed by Jellyfin."""
    def __init__(self, root):
        self.root = Path(root)
        self.folders = {}
        for folder in self.root.iterdir():
            if folder.is_dir() and not folder.name.startswith("."):
                self.folders.setdefault(normalize(folder.name), []).append(folder)
        self.cache = {}

    def contains(self, track):
        artist = normalize(track["artist"])
        if artist not in self.cache:
            keys = set()
            for folder in self.folders.get(artist, []):
                if not folder.resolve().is_relative_to(self.root.resolve()):
                    continue
                for path in folder.rglob("*"):
                    if path.suffix.lower() not in (".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac") or not path.is_file():
                        continue
                    if not path.resolve().is_relative_to(self.root.resolve()):
                        continue
                    from mutagen import File
                    try:
                        audio = File(path, easy=True)
                        tags = audio.tags if audio else None
                        titles = tags.get("title", []) if tags else []
                        artists = tags.get("artist", [track["artist"]]) if tags else []
                        for title in titles:
                            for credit in artists:
                                keys.add(identity(credit, title))
                    except (OSError, ValueError, MutagenError):
                        pass
                    # Filename fallback protects untagged, unindexed songs too.
                    stem = re.sub(r"^\d{1,3}[ ._-]+", "", path.stem)
                    keys.add(identity(track["artist"], stem))
            self.cache[artist] = keys
        return identity(track["artist"], track["title"]) in self.cache[artist]


class Network:
    def __init__(self, config, state):
        self.config, self.state = config, state
        self.requests = 0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config["user_agent"]

    def pace(self):
        until = float(self.state.setting("cooldown"))
        if until > time.time():
            raise StopRun("Provider cooldown is still active")
        if self.requests >= self.config["max_run_requests"]:
            raise StopRun("Run request ceiling reached")
        self.state.reserve("requests", self.config["daily_requests"])
        self.requests += 1
        delay = float(self.state.setting("next_request")) - time.time()
        if delay > 0:
            time.sleep(delay)
        self.state.set_setting("next_request", time.time() + self.config["request_delay"])

    def get(self, url, **kwargs):
        self.pace()
        response = self.session.get(url, timeout=(15, 45), **kwargs)
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "3600")
            try:
                seconds = max(3600, float(retry))
            except ValueError:
                from email.utils import parsedate_to_datetime
                try:
                    seconds = max(3600, parsedate_to_datetime(retry).timestamp() - time.time())
                except (TypeError, ValueError):
                    seconds = 3600
            self.state.set_setting("cooldown", time.time() + seconds)
            raise StopRun("HTTP 429; recorded provider cooldown")
        response.raise_for_status()
        return response

    def yt(self, arguments, timeout=300):
        self.pace()
        command = [self.config["yt_dlp"], "--ignore-config", "--no-playlist", "--no-progress",
                   "--sleep-requests", str(self.config["request_delay"]), "--socket-timeout", "30",
                   "--retries", "1", "--fragment-retries", "1", *arguments]
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            if "429" in result.stderr or "Too Many Requests" in result.stderr:
                self.state.set_setting("cooldown", time.time() + 3600)
                raise StopRun("yt-dlp rate limited; stopped for one hour")
            raise RuntimeError(result.stderr[-1800:])
        return result.stdout


def chart(network, category, tag, page):
    key = f"chart:{category}:{tag}:{page}"
    cached = network.state.cached(key)
    if cached is not None:
        return cached
    html = network.get(f"https://www.last.fm/tag/{quote(tag, safe='')}/tracks", params={"page": page}).text
    soup = BeautifulSoup(html, "html.parser")
    tracks = []
    for row in soup.select("tr.chartlist-row"):
        artist, title = row.select_one(".chartlist-artist a"), row.select_one(".chartlist-name a")
        if artist and title:
            tracks.append({"artist": artist.get_text(strip=True), "title": title.get_text(strip=True),
                           "category": category, "source_tag": tag})
    if not tracks:
        raise RuntimeError(f"No chart rows found for {tag}; page layout may have changed")
    network.state.cache(key, tracks, 86400)
    return tracks


def candidates(network):
    config, state = network.config, network.state
    day = int(state.setting("chart_round"))
    # Expand through five popular chart pages if skips leave the success goal unmet.
    for batch in range(5):
        groups = []
        for category in CATEGORIES:
            tags = config["tags"][category]
            tag = tags[day % len(tags)]
            page = 1 + (day // len(tags) + batch) % 5
            tracks = chart(network, category, tag, page)
            start = int(state.setting(f"cursor:{category}:{tag}:{page}")) % len(tracks)
            groups.append((category, tag, page, tracks[start:] + tracks[:start], start, len(tracks)))
        offset = day % 3
        groups = groups[offset:] + groups[:offset]
        for position in range(max(len(g[3]) for g in groups)):
            for category, tag, page, tracks, start, total in groups:
                if position < len(tracks):
                    state.set_setting(f"cursor:{category}:{tag}:{page}", (start + position + 1) % total)
                    yield tracks[position]


def metadata(network, track):
    key = "metadata:" + identity(track["artist"], track["title"])
    cached = network.state.cached(key)
    if cached is None:
        def escape(text):
            return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r'\\\1', text)
        query = f'recording:"{escape(track["title"])}" AND artist:"{escape(track["artist"])}"'
        data = network.get("https://musicbrainz.org/ws/2/recording/",
                           params={"query": query, "fmt": "json", "limit": 10}).json()
        matches = []
        for recording in data.get("recordings", []):
            artists = [a.get("artist", {}).get("name", "") for a in recording.get("artist-credit", []) if isinstance(a, dict)]
            if normalize(recording.get("title", "")) == normalize(track["title"]) and any(
                    normalize(a) == normalize(track["artist"]) for a in artists):
                matches.append(recording)
        # Metadata can describe several releases of the same song; use a dated exact recording.
        dated = [r for r in matches if re.match(r"^\d{4}", r.get("first-release-date", ""))]
        dated.sort(key=lambda r: r["first-release-date"])
        chosen = dated[0] if dated else (matches[0] if len(matches) == 1 else None)
        cached = {"recording": chosen["id"]} if chosen else {}
        if chosen:
            date = chosen.get("first-release-date", "")
            if re.match(r"^\d{4}", date):
                cached["year"] = int(date[:4])
        network.state.cache(key, cached, 30 * 86400)
    track.update(cached)
    if track.get("category") in ("80s", "90s"):
        lower = 1980 if track["category"] == "80s" else 1990
        if not lower <= track.get("year", 0) <= lower + 9:
            raise ValueError("Original release year is missing or outside the selected decade")


def recording_matches(info, track, trusted_channels=DEFAULT_TRUSTED_OFFICIAL_CHANNELS):
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        return False
    duration = info.get("duration")
    if not isinstance(duration, (int, float)):
        return False
    raw_title = info.get("title", "")
    title = matching_title(raw_title)
    wanted = matching_title(track["title"])
    artist = normalize(track["artist"])
    excluded = ("cover", "karaoke", "nightcore", "sped up", "slowed", "reaction", "tutorial", "dj set", "full album")
    if any(word in title and word not in wanted for word in excluded):
        return False
    for version in ("remix", "live", "extended", "acoustic", "instrumental", "radio edit", "original mix"):
        if (version in title) != (version in wanted):
            return False
    if info.get("track") and matching_title(info["track"]) == wanted:
        artists = normalize(info.get("artist") or info.get("artists") and " ".join(info["artists"]) or "")
        if artist in artists:
            return True
    # Explicit artist/title credit with only conventional video suffixes is acceptable.
    title_without_featured_artist = re.sub(r"[\[(]\s*(?:ft\.?|feat\.?|featuring)\s+[^)\]]*[\])]", " ", raw_title, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(official music video|official video|official audio|music video|lyric video|lyrics|audio|hd|hq|4k|visualizer)\b",
                     " ", matching_title(title_without_featured_artist))
    cleaned = " ".join(cleaned.split())
    featured_suffixes = (" ft ", " feat ", " featuring ")
    expected_credits = (artist + " " + wanted, wanted + " " + artist, wanted)
    if cleaned not in expected_credits and not any(cleaned.startswith(credit + suffix) for credit in expected_credits for suffix in featured_suffixes):
        return False
    channel = normalize(info.get("channel") or info.get("uploader") or "")
    trusted = {normalize(channel_name) for channel_name in trusted_channels}
    return channel in trusted or artist in channel and ("topic" in channel or "vevo" in channel or channel in (artist, artist + " official"))


def find_video(network, track):
    if track.get("url"):
        info = json.loads(network.yt(["--skip-download", "--dump-single-json", track["url"]]))
        if not recording_matches(info, track, network.config.get("trusted_official_channels", DEFAULT_TRUSTED_OFFICIAL_CHANNELS)):
            raise ValueError("Explicit URL does not match artist/title/version and official channel")
    else:
        search_results = network.config.get("youtube_search_results", DEFAULT_YOUTUBE_SEARCH_RESULTS)
        if not isinstance(search_results, int) or search_results < 1:
            raise ValueError("youtube_search_results must be a positive integer")
        query = f'ytsearch{search_results}:{track["artist"]} {track["title"]} official audio'
        result = json.loads(network.yt(["--flat-playlist", "--skip-download", "--dump-single-json", query]))
        info = None
        for entry in result.get("entries", []):
            duration = entry.get("duration")
            if duration and not network.config["minimum_duration"] <= duration <= network.config["maximum_duration"]:
                continue
            if not recording_matches(entry, track, network.config.get("trusted_official_channels", DEFAULT_TRUSTED_OFFICIAL_CHANNELS)):
                continue
            url = entry.get("webpage_url") or entry.get("url") or f'https://www.youtube.com/watch?v={entry["id"]}'
            candidate = json.loads(network.yt(["--skip-download", "--dump-single-json", url]))
            if recording_matches(candidate, track, network.config.get("trusted_official_channels", DEFAULT_TRUSTED_OFFICIAL_CHANNELS)):
                info = candidate
                break
        if info is None:
            # Fallback: try without "official audio" suffix if the first search failed
            query = f'ytsearch{search_results}:{track["artist"]} {track["title"]}'
            result = json.loads(network.yt(["--flat-playlist", "--skip-download", "--dump-single-json", query]))
            for entry in result.get("entries", []):
                duration = entry.get("duration")
                if duration and not network.config["minimum_duration"] <= duration <= network.config["maximum_duration"]:
                    continue
                if not recording_matches(entry, track, network.config.get("trusted_official_channels", DEFAULT_TRUSTED_OFFICIAL_CHANNELS)):
                    continue
                url = entry.get("webpage_url") or entry.get("url") or f'https://www.youtube.com/watch?v={entry["id"]}'
                candidate = json.loads(network.yt(["--skip-download", "--dump-single-json", url]))
                if recording_matches(candidate, track, network.config.get("trusted_official_channels", DEFAULT_TRUSTED_OFFICIAL_CHANNELS)):
                    info = candidate
                    break
        if info is None:
            raise ValueError(f"No unambiguous official recording in the first {search_results} search results")
    if not network.config["minimum_duration"] <= info["duration"] <= network.config["maximum_duration"]:
        raise ValueError("Recording duration is outside song limits")
    track["url"] = info["webpage_url"]
    track["video_id"] = info["id"]
    return info


def ensure_media(config):
    root, mount = Path(config["media_root"]), Path(config["media_mount"])
    if not os.path.ismount(mount) or not root.is_dir() or not root.resolve().is_relative_to(mount.resolve()):
        raise StopRun("Music drive is not mounted at the configured location")
    if shutil.disk_usage(root).free < config["minimum_free_gib"] * 1024 ** 3:
        raise StopRun("Insufficient free space on music drive")


def handoff(state, track, path):
    directory = state.directory / "handoff"
    directory.mkdir(exist_ok=True)
    atomic_json(directory / (identity(track["artist"], track["title"]) + ".json"),
                {"path": str(path), "artist": track["artist"], "title": track["title"], "created": time.time()})


def download(network, track):
    config, state = network.config, network.state
    ensure_media(config)
    if not state.remaining(config["songs_per_run"]):
        raise StopRun("Run successful-download goal already reached")
    state.increment("attempts")
    state.remember(track, "downloading", retry=3600)
    root = Path(config["media_root"])
    album = track.get("album") or track["title"] + " (Single)"
    destination = root / safe_name(track["artist"]) / safe_name(album)
    target = destination / (safe_name(track["title"]) + ".mp3")
    if target.exists():
        raise ValueError("Destination already exists; refusing to overwrite")
    # Stage on the music filesystem for an atomic no-overwrite publication.
    staging = root / ".music-discovery-staging"
    staging.mkdir(exist_ok=True)
    staging.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix="song-", dir=staging) as temporary:
        network.yt(["--format", "bestaudio/best", "--extract-audio", "--audio-format", "mp3",
                    "--audio-quality", "0", "--max-filesize", f'{config["maximum_file_mib"]}M',
                    "--sleep-interval", str(config["download_delay_min"]),
                    "--max-sleep-interval", str(config["download_delay_max"]),
                    "--output", str(Path(temporary) / "audio.%(ext)s"), track["url"]], timeout=900)
        audio = Path(temporary) / "audio.mp3"
        if not audio.is_file() or not 0 < audio.stat().st_size <= config["maximum_file_mib"] * 1024 ** 2:
            raise ValueError("Downloaded MP3 is missing or exceeds size limit")
        media = MP3(audio, ID3=EasyID3)
        if not config["minimum_duration"] <= media.info.length <= config["maximum_duration"]:
            raise ValueError("Decoded audio duration is outside song limits")
        if media.tags is None:
            media.add_tags()
        media.tags["title"], media.tags["artist"] = [track["title"]], [track["artist"]]
        media.tags["albumartist"], media.tags["album"] = [track["artist"]], [album]
        genre = track.get("source_tag") or track.get("category")
        if genre:  # explicit requests carry no category: leave genre untagged rather than guess "edm"
            media.tags["genre"] = [genre]
        if track.get("year"):
            media.tags["date"] = [str(track["year"])]
            media.tags["originaldate"] = [str(track["year"])]
        if track.get("recording"):
            media.tags["musicbrainz_trackid"] = [track["recording"]]
        media.save()
        subprocess.run(["/usr/bin/ffmpeg", "-v", "error", "-i", str(audio), "-f", "null", "-"],
                       check=True, capture_output=True, timeout=180)
        ensure_media(config)
        destination.mkdir(parents=True, exist_ok=True)
        if not destination.resolve().is_relative_to(root.resolve()):
            raise StopRun("Destination escapes the media directory")
        audio.chmod(0o644)
        with audio.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(audio, target)  # Fails rather than replacing an existing file.
        with state.db:
            state.db.execute("UPDATE daily SET downloads=downloads+1 WHERE day=?", (state.today,))
        state.remember(track, "downloaded", path=str(target))
        handoff(state, track, target)
        LOG.info("Downloaded %s — %s to %s", track["artist"], track["title"], target)
        return target


def run(config, args):
    song_limit = getattr(args, "song_limit", None)
    if song_limit is not None:
        config = {**config, "songs_per_run": song_limit}
    with contextlib.closing(State(config["state_dir"], config["timezone"], "preview" if args.dry_run else "download")) as state, \
         (Path(config["state_dir"]) / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not getattr(args, "wait_for_lock", False):
                LOG.info("Another run is active; exiting")
                return 0
            LOG.info("Another run is active; waiting for it to finish")  # a queued request must not be dropped
            fcntl.flock(lock, fcntl.LOCK_EX)
        ensure_media(config)
        catalog = Catalog(config["jellyfin_database"], Path(config["media_root"]))
        disk = DiskCatalog(config["media_root"])
        # Restore any handoff lost between publication and queue-file creation in live mode.
        for row in ([] if args.dry_run else state.db.execute("SELECT * FROM tracks WHERE status='downloaded'").fetchall()):
            if row["path"] and Path(row["path"]).is_file():
                marker = state.directory / "lyrics-done" / (row["key"] + ".json")
                pending = state.directory / "handoff" / (row["key"] + ".json")
                if not marker.exists() and not pending.exists():
                    handoff(state, dict(row), row["path"])
        network = Network(config, state)
        source = [{"artist": args.artist, "title": args.title, "category": args.category, "url": args.url}] if args.artist else itertools.chain(state.ready_tracks(), candidates(network))
        processed = []
        summary = new_summary()
        checked = 0
        seen = set()
        target = state.remaining(config["songs_per_run"])
        stop_reason = "sources_exhausted"
        try:
            for track in (() if target == 0 else source):
                if len(processed) >= target:
                    stop_reason = "goal_reached"
                    break
                key = identity(track["artist"], track["title"])
                if key in seen:
                    continue
                seen.add(key)
                previous = state.track(track)
                if catalog.contains(track) or disk.contains(track) or (previous and previous["status"] == "downloaded" and previous["path"] and Path(previous["path"]).is_file()):
                    state.record_attempt(track, "owned")
                    summary["already_present"].append(summary_track(track))
                    LOG.info("Already owned: %s — %s", track["artist"], track["title"])
                    continue
                if previous and previous["retry_at"] > time.time() and not args.retry:
                    state.record_attempt(track, "deferred", "Recent attempt is still in its retry window")
                    continue
                ceiling = min(args.candidate_limit or config["max_candidate_checks"], config["max_candidate_checks"])
                if checked >= ceiling:
                    stop_reason = "candidate_work_ceiling"
                    break
                state.increment("candidates")
                checked += 1
                summary["searched"].append(summary_track(track))
                try:
                    verified = not args.url and previous and previous["status"] == "eligible" and previous["payload"] and previous["updated"] > time.time() - 86400
                    if verified:
                        # Keep this invocation's category: a cached payload may carry a chart or older default category.
                        track.update({k: v for k, v in json.loads(previous["payload"]).items() if k not in ("category", "source_tag")})
                        LOG.info("Reusing verified candidate: %s — %s", track["artist"], track["title"])
                    else:
                        try:
                            metadata(network, track)
                        except requests.RequestException:
                            if track.get("category") in ("80s", "90s"):  # only decade checks need MusicBrainz
                                raise
                            LOG.warning("MusicBrainz unavailable; continuing without optional recording/year tags")
                    if catalog.contains(track):
                        state.remember(track, "owned", retry=30 * 86400)
                        summary["already_present"].append(summary_track(track))
                        LOG.info("Existing MusicBrainz recording: %s — %s", track["artist"], track["title"])
                        continue
                    if not verified:
                        find_video(network, track)
                    state.remember(track, "eligible")
                    if args.dry_run:
                        processed.append(track)
                        LOG.info("Would download %s — %s", track["artist"], track["title"])
                    else:
                        download(network, track)
                        catalog.keys.add(key)
                        summary["added"].append(summary_track(track))
                        processed.append(track)
                    if len(processed) >= target:
                        stop_reason = "goal_reached"
                        break
                except StopRun:
                    raise
                except (requests.RequestException, subprocess.SubprocessError, RuntimeError, ValueError, OSError, MutagenError) as exc:
                    state.remember(track, "error", detail=str(exc), retry=FAILED_CANDIDATE_RETRY_SECONDS)
                    summary["failures"].append(summary_track(track, type(exc).__name__))
                    LOG.warning("Skipped %s — %s: %s", track["artist"], track["title"], exc)
            if len(processed) >= target:
                stop_reason = "goal_reached"
            if not args.artist and not args.dry_run:
                state.set_setting("chart_round", int(state.setting("chart_round")) + 1)
        except StopRun as exc:
            stop_reason = str(exc)
            LOG.warning("Stopped: %s", exc)
        report = {"time": datetime.now(ZoneInfo(config["timezone"])).isoformat(),
                  "mode": state.mode, "run_id": state.run_id, "goal": config["songs_per_run"],
                  "target_this_run": target, "checked_this_run": checked, "stop_reason": stop_reason,
                  "daily": state.counts(), "processed": processed,
                  "summary": {"counts": summary_counts(summary), "added": summary["added"]}}
        atomic_json(state.directory / "last-run.json", report)
        if args.telegram_summary:
            report["telegram_summary"] = {"requested": True,
                                          "sent": send_telegram_summary(config, summary)}
            atomic_json(state.directory / "last-run.json", report)
        print(json.dumps(report, ensure_ascii=False))
        if isinstance(getattr(args, "report", None), dict):
            args.report.update(report)
        if args.artist and not processed and not catalog.contains(source[0]) and not disk.contains(source[0]):
            row = state.track(source[0])
            if not row or row["status"] != "downloaded" or not row["path"] or not Path(row["path"]).is_file():
                return 1
        return 0


def request_dir(config):
    return Path(config.get("request_dir") or Path(config["state_dir"]) / "requests")


def read_request(path):
    """Parse one queued request without following links (the directory is shared with a container)."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("request is not a regular file")
        body = stream.read(REQUEST_MAX_BYTES + 1)
    if len(body) > REQUEST_MAX_BYTES:
        raise ValueError("request is too large")
    request = json.loads(body)
    if not isinstance(request, dict):
        raise ValueError("request is not a JSON object")
    for field in ("artist", "title"):
        value = request.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > REQUEST_FIELD_MAX:
            raise ValueError(f"request {field} must be 1-{REQUEST_FIELD_MAX} characters")
    if request.get("category") not in (None, *CATEGORIES):
        raise ValueError("request category is not supported")
    if not isinstance(request.get("dry_run", False), bool):
        raise ValueError("request dry_run must be true or false")
    return request


def request_outcome(config, track, code, report):
    """Summarize a single-song run as outcome + detail for the requester."""
    counts = (report.get("summary") or {}).get("counts") or {}
    row = None
    database = Path(config["state_dir"]) / "history.sqlite3"
    if database.exists():
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT status,detail,path,url,retry_at FROM tracks WHERE artist_key=? AND title_key=?",
                             (normalize(track["artist"]), normalize(track["title"]))).fetchone()
    result = {"returncode": code, "stop_reason": report.get("stop_reason"),
              "path": row["path"] if row else None, "url": (report.get("processed") or [{}])[0].get("url") or (row["url"] if row else None)}
    if not report:
        return {**result, "outcome": "error", "detail": "music discovery produced no report"}
    if counts.get("added"):
        return {**result, "outcome": "added", "detail": ""}
    if counts.get("already_present"):
        return {**result, "outcome": "already_present", "detail": ""}
    if report.get("mode") == "preview" and report.get("processed"):
        return {**result, "outcome": "found", "detail": ""}
    if report.get("stop_reason") not in ("goal_reached", "sources_exhausted"):
        return {**result, "outcome": "stopped", "detail": str(report.get("stop_reason"))}
    if not report.get("checked_this_run") and row and row["retry_at"] > time.time():
        return {**result, "outcome": "deferred", "detail": f"tried recently ({row['detail'] or row['status']}); retry after {datetime.fromtimestamp(row['retry_at'], ZoneInfo(config['timezone'])).strftime('%Y-%m-%d %H:%M')}"}
    return {**result, "outcome": "failed", "detail": (row["detail"] if row and row["detail"] else "no unambiguous official recording found")}


def process_requests(config):
    """Run every queued single-song request (<id>.json) in arrival order; write results/<id>.json and remove the request."""
    directory = request_dir(config)
    results = directory / "results"
    results.mkdir(exist_ok=True)
    for old in results.glob("*.json"):
        with contextlib.suppress(OSError):
            if old.stat().st_mtime < time.time() - REQUEST_RESULT_RETENTION_SECONDS:
                old.unlink()
    processed = 0
    while True:
        pending = sorted((p for p in directory.glob("*.json") if REQUEST_ID_PATTERN.match(p.stem)),
                         key=lambda p: p.lstat().st_mtime)
        if not pending:
            return processed
        path = pending[0]
        started = time.time()
        result = {"id": path.stem, "started_at": started}
        try:
            request = read_request(path)
            result.update({"artist": request["artist"].strip(), "title": request["title"].strip(),
                           "requested_at": request.get("requested_at"), "dry_run": request.get("dry_run", False)})
            args = argparse.Namespace(dry_run=result["dry_run"], artist=result["artist"], title=result["title"],
                                      category=request.get("category"), url=None, retry=False, candidate_limit=None,
                                      song_limit=1, telegram_summary=False, wait_for_lock=True, report={})
            LOG.info("Processing song request %s: %s — %s", path.stem, result["artist"], result["title"])
            try:
                code = run(config, args)
            except (OSError, sqlite3.Error, StopRun) as exc:
                LOG.error("Song request %s failed: %s", path.stem, exc)
                result.update({"outcome": "error", "detail": str(exc), "returncode": 1})
            else:
                result.update(request_outcome(config, result, code, args.report))
        except (OSError, ValueError) as exc:  # unreadable or malformed: answer it so the requester is not left waiting
            LOG.warning("Rejected song request %s: %s", path.stem, exc)
            result.update({"outcome": "invalid", "detail": str(exc)})
        result["finished_at"] = time.time()
        atomic_json(results / path.name, result, mode=0o644)
        # Always remove the request, even after a failure: a lingering file would retrigger the path unit forever.
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        processed += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--dry-run", action="store_true", help="Discover and validate; do not download or enqueue lyrics (counts requests)")
    parser.add_argument("--artist")
    parser.add_argument("--title")
    parser.add_argument("--url")
    parser.add_argument("--category", choices=CATEGORIES, default=None,
                        help="With --artist/--title: tag and validate as this category (80s/90s require a matching MusicBrainz year). Default: no category or genre tag")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--retry", action="store_true", help="Retry one explicitly selected failed candidate; daily limits still apply")
    parser.add_argument("--candidate-limit", type=int, help="Lower the number of candidate checks for this invocation")
    parser.add_argument("--song-limit", type=int, help="Override this invocation's successful-download goal")
    parser.add_argument("--telegram-summary", action="store_true", help="Send a Telegram summary after this run")
    parser.add_argument("--test-telegram-summary", action="store_true", help="Send a no-download Telegram delivery test")
    parser.add_argument("--process-requests", action="store_true", help="Run queued single-song requests from the request directory (used by music-discovery-request.path)")
    args = parser.parse_args()
    if bool(args.artist) != bool(args.title) or (args.url and not args.artist):
        parser.error("artist/title must be supplied together; URL requires both")
    if args.retry and not args.artist:
        parser.error("--retry requires an explicit artist/title")
    if args.candidate_limit is not None and args.candidate_limit < 1:
        parser.error("--candidate-limit must be positive")
    if args.song_limit is not None and args.song_limit < 1:
        parser.error("--song-limit must be positive")
    config = json.loads(args.config.read_text())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.process_requests:
        try:
            LOG.info("Processed %d song request(s)", process_requests(config))
            return 0
        except (OSError, sqlite3.Error) as exc:
            LOG.error("Request processing failed: %s", exc)
            return 1
    if args.test_telegram_summary:
        sent = send_telegram_summary(config, new_summary(), "Music discovery Telegram delivery test")
        print(json.dumps({"mode": "telegram-test", "telegram_summary": {"sent": sent}}))
        return 0 if sent else 1
    if args.status:
        database = Path(config["state_dir"]) / "history.sqlite3"
        if not database.exists():
            print(json.dumps({"status": "not yet run"}))
            return 0
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            has_history = db.execute("SELECT 1 FROM sqlite_master WHERE name='attempt_history'").fetchone()
            print(json.dumps({"daily": [dict(r) for r in db.execute("SELECT * FROM daily ORDER BY day DESC LIMIT 7")],
                              "tracks": [dict(r) for r in db.execute("SELECT artist,title,status,path,detail FROM tracks ORDER BY updated DESC LIMIT 10")],
                              "recent_attempts": [dict(r) for r in db.execute("SELECT artist,title,mode,status,attempted_at,detail FROM attempt_history ORDER BY attempted_at DESC LIMIT 10")] if has_history else []}))
        return 0
    try:
        return run(config, args)
    except (OSError, sqlite3.Error, StopRun) as exc:
        LOG.error("Run failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
