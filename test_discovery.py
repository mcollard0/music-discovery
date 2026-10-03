import json
import sqlite3
import tempfile
import argparse
import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from discover import Catalog, DiskCatalog, Network, State, StopRun, candidates, identity, metadata, recording_matches, run, safe_name
from lyrics_bridge import has_sidecar


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_limits_survive_restart_and_do_not_count_failed_reservations(self):
        first = State(self.root, "America/Chicago")
        first.reserve("requests", 2)
        first.db.close()
        second = State(self.root, "America/Chicago")
        second.reserve("requests", 2)
        with self.assertRaises(StopRun):
            second.reserve("requests", 2)
        self.assertEqual(second.counts()["requests"], 2)
        self.assertEqual(second.counts()["downloads"], 0)
        second.db.close()

    def test_artist_title_history_is_indexed_and_survives_restart(self):
        first = State(self.root, "America/Chicago")
        track = {"artist": "Björk", "title": "It's Oh So Quiet", "category": "90s"}
        first.remember(track, "error", detail="Temporary failure", retry=86400)
        first.db.close()
        second = State(self.root, "America/Chicago")
        matched = second.track({"artist": "BJORK", "title": "IT’S OH SO QUIET"})
        self.assertEqual(matched["status"], "error")
        history = second.db.execute("SELECT status,detail FROM attempt_history WHERE artist_key=? AND title_key=?",
                                    ("bjork", "it s oh so quiet")).fetchall()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["detail"], "Temporary failure")
        plan = second.db.execute("EXPLAIN QUERY PLAN SELECT * FROM tracks WHERE artist_key=? AND title_key=?",
                                 ("bjork", "it s oh so quiet")).fetchone()[3]
        self.assertIn("track_artist_title", plan)
        second.db.close()

    def test_discovery_expands_to_five_pages_in_all_three_categories(self):
        state = State(self.root, "America/Chicago")
        network = Mock()
        network.state = state
        network.config = {"tags": {"edm": ["edm"], "80s": ["80s"], "90s": ["90s"]}}
        pages = []
        def source(_network, category, tag, page):
            pages.append((category, page))
            return [{"artist": "Artist", "title": f"{category} page {page}", "category": category}]
        with patch("discover.chart", side_effect=source):
            tracks = list(candidates(network))
        self.assertEqual(len(tracks), 15)
        self.assertEqual(set(pages), {(category, page) for category in ("edm", "80s", "90s") for page in range(1, 6)})
        self.assertEqual([t["category"] for t in tracks[:3]], ["edm", "80s", "90s"])
        state.close()

    def test_request_ceiling_resets_per_run_but_daily_statistics_persist(self):
        state = State(self.root, "America/Chicago")
        config = {"max_run_requests": 2, "daily_requests": 100, "request_delay": 0, "user_agent": "test"}
        first = Network(config, state)
        first.pace()
        first.pace()
        with self.assertRaisesRegex(StopRun, "Run request ceiling"):
            first.pace()
        second = Network(config, state)
        second.pace()
        second.pace()
        self.assertEqual(state.counts()["requests"], 4)
        state.close()

    def test_skips_and_failed_downloads_do_not_use_success_goal(self):
        state_dir = self.root / "state"
        config = {"state_dir": str(state_dir), "timezone": "America/Chicago", "songs_per_run": 2,
                  "max_candidate_checks": 100, "media_root": str(self.root), "jellyfin_database": "unused"}
        initial = State(state_dir, config["timezone"])
        # Old candidate/attempt totals must not prevent reaching the goal.
        initial.db.execute("UPDATE daily SET downloads=7,candidates=20,attempts=5")
        initial.db.commit()
        initial.db.close()
        tracks = [{"artist": "Artist", "title": title, "category": "edm"}
                  for title in ("Owned", "Rejected", "Failed download", "Good A", "Good B", "Extra")]
        catalog = Mock()
        catalog.contains.side_effect = lambda t: t["title"] == "Owned"
        catalog.keys = set()
        disk = Mock()
        disk.contains.return_value = False
        attempted = []
        def validate(network, track):
            if track["title"] == "Rejected":
                raise ValueError("No matching recording")
            track["url"] = "https://example.invalid/song"
        def fetch(network, track):
            network.state.increment("attempts")
            attempted.append(track["title"])
            if track["title"] == "Failed download":
                raise RuntimeError("Download failed")
            with network.state.db:
                network.state.db.execute("UPDATE daily SET downloads=downloads+1")
            path = self.root / (track["title"] + ".mp3")
            path.touch()
            network.state.remember(track, "downloaded", path=str(path))
            return path
        args = argparse.Namespace(dry_run=False, artist=None, title=None, category="edm", url=None,
                                  retry=False, candidate_limit=None)
        with patch("discover.ensure_media"), patch("discover.Catalog", return_value=catalog), \
             patch("discover.DiskCatalog", return_value=disk), patch("discover.Network", side_effect=lambda c,s: argparse.Namespace(state=s)), \
             patch("discover.candidates", return_value=iter(tracks)), patch("discover.metadata"), \
             patch("discover.find_video", side_effect=validate), patch("discover.download", side_effect=fetch), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run(config, args), 0)
        self.assertEqual(attempted, ["Failed download", "Good A", "Good B"])
        report = json.loads((state_dir / "last-run.json").read_text())
        self.assertEqual(report["stop_reason"], "goal_reached")
        self.assertEqual(report["daily"]["downloads"], 9)
        self.assertEqual(report["daily"]["attempts"], 8)
        self.assertEqual([t["title"] for t in report["processed"]], ["Good A", "Good B"])
        restarted = State(state_dir, config["timezone"])
        # A new invocation has a fresh goal even after nine successes today.
        self.assertEqual(restarted.remaining(2), 2)
        restarted.db.close()

    def test_active_jellyfin_wal_is_visible_and_remains_unmodified(self):
        database = self.root / "jellyfin.db"
        writer = sqlite3.connect(database)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.executescript("""CREATE TABLE BaseItems(Id TEXT,Name TEXT,Artists TEXT,
            AlbumArtists TEXT,Path TEXT,MediaType TEXT);
            CREATE TABLE BaseItemProviders(ItemId TEXT,ProviderId TEXT,ProviderValue TEXT);""")
        writer.execute("INSERT INTO BaseItems VALUES(?,?,?,?,?,?)", ("one", "Take On Me", "a-ha", "a-ha", str(self.root / "a-ha/song.mp3"), "Audio"))
        writer.execute("INSERT INTO BaseItemProviders VALUES(?,?,?)", ("one", "MusicBrainzTrack", "recording-one"))
        writer.commit()
        before = writer.total_changes
        catalog = Catalog(database, self.root)
        self.assertTrue(catalog.contains({"artist": "A-HA", "title": "Take on me"}))
        self.assertTrue(catalog.contains({"artist": "Alias", "title": "Alternate", "recording": "recording-one"}))
        self.assertFalse(catalog.contains({"artist": "a-ha", "title": "Take On Me (Live)"}))
        self.assertEqual(writer.total_changes, before)
        writer.close()

    def test_unindexed_filename_duplicate_is_found(self):
        album = self.root / "A-ha" / "Hunting High and Low"
        album.mkdir(parents=True)
        (album / "01 Take On Me.mp3").write_bytes(b"")
        disk = DiskCatalog(self.root)
        self.assertTrue(disk.contains({"artist": "a-ha", "title": "Take On Me"}))
        self.assertFalse(disk.contains({"artist": "a-ha", "title": "Take On Me (Live)"}))

    def test_song_identity_preserves_versions(self):
        self.assertEqual(identity("Björk", "It's Oh So Quiet"), identity("bjork", "IT’S OH SO QUIET"))
        self.assertNotEqual(identity("Artist", "Song"), identity("Artist", "Song (Remix)"))

    def test_only_matching_official_recordings_are_accepted(self):
        track = {"artist": "MIMIFY", "title": "I Waited too Long (Original Mix)"}
        info = {"title": "MIMIFY - I Waited too Long (Original Mix)", "channel": "MIMIFY", "duration": 470}
        self.assertTrue(recording_matches(info, track))
        self.assertFalse(recording_matches({**info, "channel": "Random uploads"}, track))
        self.assertFalse(recording_matches({**info, "is_live": True}, track))
        self.assertFalse(recording_matches({**info, "title": info["title"] + " (Slowed)"}, track))
        self.assertFalse(recording_matches(info, {**track, "title": "I Waited too Long"}))
        self.assertTrue(recording_matches({"title": "a-ha - Take On Me (Official Video)", "channel": "a-ha", "duration": 240},
                                          {"artist": "a-ha", "title": "Take On Me"}))

    def test_decade_uses_first_release_not_video_upload(self):
        state = State(self.root, "America/Chicago")
        network = Mock()
        network.state = state
        network.get.return_value.json.return_value = {"recordings": [{"id": "one", "title": "Song",
            "artist-credit": [{"artist": {"name": "Artist"}}], "first-release-date": "1985-01-01"}]}
        track = {"artist": "Artist", "title": "Song", "category": "80s"}
        metadata(network, track)
        self.assertEqual(track["year"], 1985)
        with self.assertRaises(ValueError):
            metadata(network, {"artist": "Artist", "title": "Song", "category": "90s"})
        state.db.close()

    def test_unknown_dates_fail_closed_for_decades(self):
        state = State(self.root, "America/Chicago")
        network = Mock()
        network.state = state
        network.get.return_value.json.return_value = {"recordings": []}
        with self.assertRaises(ValueError):
            metadata(network, {"artist": "Artist", "title": "Unknown", "category": "80s"})
        state.db.close()

    def test_lyrics_bridge_preserves_timed_sidecars(self):
        track = self.root / "Song.mp3"
        track.touch()
        self.assertFalse(has_sidecar(track))
        track.with_suffix(".lrc").write_text("[00:01.00]Existing timed lyric")
        self.assertTrue(has_sidecar(track))

    def test_filename_cannot_escape_artist_directory(self):
        self.assertNotIn("/", safe_name("../../Artist/Song"))
        self.assertNotIn("\\", safe_name("..\\Song"))
        self.assertEqual(safe_name("..."), "Unknown")


if __name__ == "__main__":
    unittest.main()
