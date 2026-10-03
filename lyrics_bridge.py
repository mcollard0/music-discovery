#!/usr/bin/env python3
"""Hand imported tracks to the installed Finamp Genius worker, as jellyfin."""
import argparse
import contextlib
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

from discover import atomic_json, ensure_media

LOG = logging.getLogger("music-discovery-lyrics")


def has_sidecar(path):
    return any(path.with_suffix(suffix).is_file() for suffix in (".lrc", ".elrc", ".txt"))


def bridge(config):
    ensure_media(config)
    directory = Path(config["state_dir"])
    done = directory / "lyrics-done"
    done.mkdir(exist_ok=True)
    with (done / "bridge.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        pending = [p for p in sorted((directory / "handoff").glob("*.json")) if not (done / p.name).exists()]
        if not pending:
            atomic_json(done / "last-bridge.json", {"queued": 0, "time": time.time()})
            return 0
        installed = Path(config.get("lyrics_install_dir", "/var/lib/jellyfin/finamp-lyrics"))
        credentials = json.loads((installed / "credentials.json").read_text())
        session = requests.Session()
        session.headers.update({"X-Emby-Token": credentials["JELLYFIN_API_KEY"],
                                "User-Agent": "MusicDiscoveryLyrics/1.0"})
        base = config.get("jellyfin_url", "http://localhost:8096").rstrip("/")
        os.environ["JELLYFIN_API_KEY"] = credentials["JELLYFIN_API_KEY"]
        # Reuse the installed implementation and its existing cache/pacing/lock.
        sys.path.insert(0, str(installed))
        import lyrics_fetcher
        jf = lyrics_fetcher.Jellyfin(base, credentials["JELLYFIN_API_KEY"], [], [])
        queued = 0
        for source in pending[:config["songs_per_run"]]:
            track = json.loads(source.read_text())
            path = Path(track["path"])
            if not path.resolve().is_relative_to(Path(config["media_root"]).resolve()) or not path.is_file():
                LOG.warning("Waiting for media file: %s", path)
                continue
            if has_sidecar(path):
                atomic_json(done / source.name, {"status": "existing_lyrics", "path": str(path)})
                LOG.info("Kept existing lyric sidecar: %s", path)
                continue
            response = session.get(base + "/Items", params={"Recursive": "true", "IncludeItemTypes": "Audio",
                "SearchTerm": track["title"], "Fields": "Path,MediaStreams", "Limit": 100}, timeout=30)
            response.raise_for_status()
            items = [item for item in response.json().get("Items", []) if item.get("Path") == str(path)]
            if len(items) != 1:
                # Notify Jellyfin of exactly this completed file; do not trigger a full library scan.
                notice_marker = done / (source.stem + ".notice.json")
                notice = json.loads(notice_marker.read_text()) if notice_marker.exists() else {}
                if time.time() - notice.get("time", 0) > 6 * 3600:
                    update = session.post(base + "/Library/Media/Updated", json={"Updates": [
                        {"Path": str(path), "UpdateType": "Created"}]}, timeout=30)
                    update.raise_for_status()
                    atomic_json(notice_marker, {"time": time.time()})
                LOG.info("Notified Jellyfin; waiting for indexing: %s", path)
                continue
            item = items[0]
            resolved = jf.priority(item["Id"])
            if not resolved.title or not resolved.artists:
                refresh_marker = done / (source.stem + ".refresh.json")
                last_refresh = json.loads(refresh_marker.read_text()) if refresh_marker.exists() else {}
                if time.time() - last_refresh.get("time", 0) > 3600:
                    refresh = session.post(base + f'/Items/{item["Id"]}/Refresh', params={
                        "MetadataRefreshMode": "FullRefresh", "ImageRefreshMode": "None",
                        "ReplaceAllMetadata": "false", "ReplaceAllImages": "false"}, timeout=30)
                    refresh.raise_for_status()
                    atomic_json(refresh_marker, {"time": time.time(), "item_id": item["Id"]})
                LOG.info("Waiting for artist/title metadata: %s", path)
                continue
            lyric_response = session.get(base + f'/Audio/{item["Id"]}/Lyrics', timeout=30)
            if lyric_response.status_code not in (200, 404):
                lyric_response.raise_for_status()
            existing = lyric_response.status_code == 200 and any(
                line.get("Text", "").strip() for line in lyric_response.json().get("Lyrics", []))
            if existing or has_sidecar(path):
                status = "existing_lyrics"
            else:
                # Recheck sidecars immediately before enqueueing, preserving timed lyrics.
                result = lyrics_fetcher.main(["--server-url", base, "--state-dir", str(installed / "state"),
                    "--priority", item["Id"], "--top", "0", "--upload", "--enqueue-only"])
                if result:
                    LOG.warning("Lyric enqueue failed for %s", item["Id"])
                    continue
                status = "queued"
                queued += 1
            atomic_json(done / source.name, {"status": status, "item_id": item["Id"], "path": str(path), "time": time.time()})
            LOG.info("Lyrics %s for %s", status, path)
        atomic_json(done / "last-bridge.json", {"queued": queued, "time": time.time()})
        return 0


def kick(config):
    # Called by systemd as root after the restricted bridge exits. No credentials are read here.
    path = Path(config["state_dir"]) / "lyrics-done" / "last-bridge.json"
    report = json.loads(path.read_text()) if path.exists() else {}
    if report.get("queued") and time.time() - report.get("time", 0) < 120:
        active = subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", "finamp-lyrics-continuous.service"])
        if active.returncode:
            subprocess.run(["/usr/bin/systemctl", "start", "--no-block", "finamp-lyrics-batch.service"], check=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--kick", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = json.loads(args.config.read_text())
    try:
        return kick(config) if args.kick else bridge(config)
    except (OSError, ValueError, requests.RequestException) as exc:
        # Do not log request headers or credential-file content.
        LOG.error("Lyric handoff failed: %s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
