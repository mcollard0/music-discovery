import json
import sqlite3
import tempfile
import argparse
import contextlib
import io
import requests
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from discover import Catalog, DiskCatalog, FAILED_CANDIDATE_RETRY_SECONDS, Network, State, StopRun, candidates, download, find_video, identity, metadata, new_summary, process_requests, recording_matches, run, safe_name, send_telegram_summary, telegram_message
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
        self.assertAlmostEqual(matched["retry_at"] - time.time(), FAILED_CANDIDATE_RETRY_SECONDS, delta=5)
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

    def test_song_limit_overrides_config_and_skips_do_not_use_success_goal(self):
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
                                  retry=False, candidate_limit=None, song_limit=3, telegram_summary=False)
        with patch("discover.ensure_media"), patch("discover.Catalog", return_value=catalog), \
             patch("discover.DiskCatalog", return_value=disk), patch("discover.Network", side_effect=lambda c,s: argparse.Namespace(state=s)), \
             patch("discover.candidates", return_value=iter(tracks)), patch("discover.metadata"), \
             patch("discover.find_video", side_effect=validate), patch("discover.download", side_effect=fetch), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run(config, args), 0)
        self.assertEqual(attempted, ["Failed download", "Good A", "Good B", "Extra"])
        report = json.loads((state_dir / "last-run.json").read_text())
        self.assertEqual(report["stop_reason"], "goal_reached")
        self.assertEqual(report["goal"], 3)
        self.assertEqual(report["target_this_run"], 3)
        self.assertEqual(report["daily"]["downloads"], 10)
        self.assertEqual(report["daily"]["attempts"], 9)
        self.assertEqual([t["title"] for t in report["processed"]], ["Good A", "Good B", "Extra"])
        self.assertEqual(report["summary"]["counts"], {"searched": 5, "already_present": 1, "failures": 2, "added": 3})
        self.assertEqual([t["title"] for t in report["summary"]["added"]], ["Good A", "Good B", "Extra"])
        restarted = State(state_dir, config["timezone"])
        # A new invocation has a fresh goal even after nine successes today.
        self.assertEqual(restarted.remaining(2), 2)
        failed = restarted.track({"artist": "Artist", "title": "Failed download"})
        self.assertAlmostEqual(failed["retry_at"] - time.time(), FAILED_CANDIDATE_RETRY_SECONDS, delta=5)
        restarted.db.close()
    def test_explicit_url_bypasses_cached_eligible_candidate(self):
        state_dir = self.root / "state"
        config = {"state_dir": str(state_dir), "timezone": "America/Chicago", "songs_per_run": 1,
                  "max_candidate_checks": 1, "media_root": str(self.root), "jellyfin_database": "unused"}
        track = {"artist": "Artist", "title": "Song", "category": "edm", "url": "https://example.invalid/cached", "video_id": "cached"}
        state = State(state_dir, config["timezone"])
        state.remember(track, "eligible")
        state.close()
        args = argparse.Namespace(dry_run=True, artist="Artist", title="Song", category="edm",
                                  url="https://example.invalid/explicit", retry=True, candidate_limit=None,
                                  song_limit=None, telegram_summary=False)
        catalog = Mock()
        catalog.contains.return_value = False
        catalog.keys = set()
        disk = Mock()
        disk.contains.return_value = False
        def validate(_network, candidate):
            self.assertEqual(candidate["url"], args.url)
            candidate["video_id"] = "explicit"
        with patch("discover.ensure_media"), patch("discover.Catalog", return_value=catalog), \
             patch("discover.DiskCatalog", return_value=disk), patch("discover.Network", side_effect=lambda c,s: argparse.Namespace(state=s)), \
             patch("discover.metadata"), patch("discover.find_video", side_effect=validate) as find, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run(config, args), 0)
        find.assert_called_once()
        report = json.loads((state_dir / "last-run.json").read_text())
        self.assertEqual(report["processed"][0]["url"], args.url)

    def test_explicit_request_without_category_ignores_cached_edm_category(self):
        state_dir = self.root / "state"
        config = {"state_dir": str(state_dir), "timezone": "America/Chicago", "songs_per_run": 1,
                  "max_candidate_checks": 1, "media_root": str(self.root), "jellyfin_database": "unused"}
        state = State(state_dir, config["timezone"])  # an earlier run cached this song under the old "edm" default
        state.remember({"artist": "Jaden", "title": "Again (feat. SYRE)", "category": "edm", "url": "https://example.invalid/v", "video_id": "v"}, "eligible")
        state.close()
        args = argparse.Namespace(dry_run=True, artist="Jaden", title="Again (feat. SYRE)", category=None, url=None, retry=False,
                                  candidate_limit=None, song_limit=None, telegram_summary=False)
        catalog = Mock()
        catalog.contains.return_value = False
        catalog.keys = set()
        disk = Mock()
        disk.contains.return_value = False
        with patch("discover.ensure_media"), patch("discover.Catalog", return_value=catalog), \
             patch("discover.DiskCatalog", return_value=disk), patch("discover.Network", side_effect=lambda c, s: argparse.Namespace(state=s)), \
             patch("discover.metadata") as lookup, patch("discover.find_video") as find, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run(config, args), 0)
        lookup.assert_not_called()
        find.assert_not_called()  # the verified candidate is still reused
        processed = json.loads((state_dir / "last-run.json").read_text())["processed"][0]
        self.assertIsNone(processed["category"])
        self.assertEqual(processed["url"], "https://example.invalid/v")

    def test_download_tags_genre_only_from_a_real_source(self):
        config = {"state_dir": str(self.root / "state"), "timezone": "America/Chicago", "songs_per_run": 5, "media_root": str(self.root / "music"),
                  "maximum_file_mib": 100, "download_delay_min": 0, "download_delay_max": 0, "minimum_duration": 60, "maximum_duration": 900}
        (self.root / "music").mkdir()
        state = State(config["state_dir"], config["timezone"])
        def fake_yt(arguments, timeout=300):
            Path(arguments[arguments.index("--output") + 1].replace("%(ext)s", "mp3")).write_bytes(b"x")
        saved = {}
        def fake_mp3(path, ID3=None):
            media = Mock()
            media.info.length = 200
            media.tags = {}
            media.save.side_effect = lambda: saved.setdefault(media.tags["title"][0], dict(media.tags))
            return media
        network = argparse.Namespace(config=config, state=state, yt=fake_yt)
        with patch("discover.ensure_media"), patch("discover.MP3", side_effect=fake_mp3), patch("discover.subprocess.run"):
            download(network, {"artist": "Jaden", "title": "Again (feat. SYRE)", "category": None, "url": "u"})
            download(network, {"artist": "Artist", "title": "Chart Song", "category": "edm", "source_tag": "deep house", "url": "u"})
            download(network, {"artist": "Artist", "title": "Decade Song", "category": "90s", "url": "u"})
        state.close()
        self.assertNotIn("genre", saved["Again (feat. SYRE)"])
        self.assertEqual(saved["Chart Song"]["genre"], ["deep house"])
        self.assertEqual(saved["Decade Song"]["genre"], ["90s"])

    def request_config(self):
        state_dir = self.root / "state"
        (state_dir / "requests").mkdir(parents=True)
        return {"state_dir": str(state_dir), "timezone": "America/Chicago"}, state_dir / "requests"

    def test_process_requests_runs_each_song_and_answers_every_request(self):
        config, requests_dir = self.request_config()
        (requests_dir / "gridshift-tg-1.json").write_text(json.dumps({"artist": "Jaden", "title": "Again (feat. SYRE)", "requested_at": 1}))
        (requests_dir / "gridshift-tg-2.json").write_text(json.dumps({"artist": "", "title": "No artist"}))
        (requests_dir / "notes.txt").write_text("ignored")
        calls = []
        def fake_run(_config, args):
            calls.append(args)
            args.report.update({"mode": "download", "stop_reason": "goal_reached", "checked_this_run": 1,
                                "processed": [{"url": "https://example.invalid/v"}], "summary": {"counts": {"added": 1}}})
            return 0
        with patch("discover.run", side_effect=fake_run):
            self.assertEqual(process_requests(config), 2)
        self.assertEqual(len(calls), 1)
        args = calls[0]
        self.assertEqual((args.artist, args.title, args.category, args.song_limit, args.wait_for_lock, args.dry_run),
                         ("Jaden", "Again (feat. SYRE)", None, 1, True, False))
        added = json.loads((requests_dir / "results" / "gridshift-tg-1.json").read_text())
        self.assertEqual((added["outcome"], added["artist"], added["url"]), ("added", "Jaden", "https://example.invalid/v"))
        invalid = json.loads((requests_dir / "results" / "gridshift-tg-2.json").read_text())
        self.assertEqual(invalid["outcome"], "invalid")
        self.assertIn("artist", invalid["detail"])
        self.assertEqual(sorted(p.name for p in requests_dir.iterdir()), ["notes.txt", "results"])  # requests removed, junk untouched

    def test_process_requests_reports_failures_and_never_leaves_a_request_behind(self):
        config, requests_dir = self.request_config()
        (requests_dir / "a.json").write_text(json.dumps({"artist": "A", "title": "Song", "dry_run": True}))
        (requests_dir / "b.json").write_text(json.dumps({"artist": "B", "title": "Song"}))
        (requests_dir / "c.json").symlink_to(self.root / "secret.json")
        (self.root / "secret.json").write_text(json.dumps({"artist": "X", "title": "Y"}))
        def fake_run(_config, args):
            if args.artist == "A":
                args.report.update({"mode": "preview", "stop_reason": "goal_reached", "checked_this_run": 1, "processed": [{"url": "u"}], "summary": {"counts": {}}})
                return 0
            raise StopRun("Music drive is not mounted at the configured location")
        with patch("discover.run", side_effect=fake_run):
            self.assertEqual(process_requests(config), 3)
        results = {p.stem: json.loads(p.read_text()) for p in (requests_dir / "results").glob("*.json")}
        self.assertEqual(results["a"]["outcome"], "found")
        self.assertEqual((results["b"]["outcome"], results["b"]["detail"]), ("error", "Music drive is not mounted at the configured location"))
        self.assertEqual(results["c"]["outcome"], "invalid")  # links are not followed out of the shared directory
        self.assertEqual(list(requests_dir.glob("*.json")), [])

    def test_explicit_run_waits_for_an_active_run_when_queued(self):
        state_dir = self.root / "state"
        state_dir.mkdir()
        config = {"state_dir": str(state_dir), "timezone": "America/Chicago", "songs_per_run": 1}
        args = argparse.Namespace(dry_run=True, artist="A", title="B", category=None, url=None, retry=False,
                                  candidate_limit=None, song_limit=None, telegram_summary=False, wait_for_lock=True, report={})
        modes = []
        def flock(_stream, mode):
            modes.append(mode)
            if mode & 4:  # LOCK_NB: another run holds it
                raise BlockingIOError
        with patch("discover.fcntl.flock", side_effect=flock), patch("discover.ensure_media", side_effect=StopRun("stop here")):
            with self.assertRaises(StopRun):
                run(config, args)
        self.assertEqual(len(modes), 2)  # tried non-blocking, then waited

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
        self.assertTrue(recording_matches({**info, "title": info["title"] + " (Slowed)"}, track))
        self.assertTrue(recording_matches(info, {**track, "title": "I Waited too Long"}))
        self.assertFalse(recording_matches({**info, "title": "MIMIFY - I Waited too Long Slowed"}, track))
        self.assertTrue(recording_matches({"title": "a-ha - Take On Me (Official Video)", "channel": "a-ha", "duration": 240},
                                          {"artist": "a-ha", "title": "Take On Me"}))
    def test_video_search_accepts_featured_artists_and_uses_configured_depth(self):
        track = {"artist": "Modestep", "title": "Another Day", "category": "edm"}
        entry = {"id": "s53hi2NrOik", "title": "Modestep - Another Day ft. Popeska", "channel": "ModestepVEVO", "duration": 264}
        network = Mock()
        network.config = {"minimum_duration": 60, "maximum_duration": 900, "youtube_search_results": 10}
        network.yt.side_effect = [
            json.dumps({"entries": [entry]}),
            json.dumps({**entry, "webpage_url": "https://www.youtube.com/watch?v=s53hi2NrOik"}),
        ]
        info = find_video(network, track)
        self.assertEqual(info["id"], "s53hi2NrOik")
        self.assertEqual(track["url"], "https://www.youtube.com/watch?v=s53hi2NrOik")
        self.assertIn("ytsearch10:Modestep Another Day official audio", network.yt.call_args_list[0].args[0])

    def test_trusted_official_channel_accepts_requested_remix(self):
        info = {"title": "Modestep - Another Day (Ft. Popeska) (xKore Remix) (Official Video)",
                "channel": "UKF Dubstep", "duration": 259}
        track = {"artist": "Modestep", "title": "Another Day (xKore Remix)"}
        self.assertFalse(recording_matches(info, track, ()))
        self.assertTrue(recording_matches(info, track, ("UKF Dubstep",)))

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

    def test_telegram_summary_escapes_track_metadata_and_keeps_added_list(self):
        summary = new_summary()
        summary["searched"].append({"artist": "Artist", "title": "Song", "category": "edm"})
        summary["already_present"].append({"artist": "Owned", "title": "Track", "category": "90s"})
        summary["failures"].append({"artist": "Failed", "title": "Track", "category": "80s"})
        summary["added"].append({"artist": "A < B", "title": "Song & Title", "category": "edm"})
        message = telegram_message(summary)
        self.assertIn("Searched: 1", message)
        self.assertIn("Already present: 1", message)
        self.assertIn("Failures: 1", message)
        self.assertIn("Added: 1", message)
        self.assertIn("A &lt; B — Song &amp; Title", message)

    def test_telegram_summary_sends_without_exposing_credentials(self):
        config = {"telegram_summary": {"bot_token_env": "TEST_TELEGRAM_TOKEN", "chat_id_env": "TEST_TELEGRAM_CHAT"}}
        response = Mock()
        response.json.return_value = {"ok": True}
        with patch.dict("os.environ", {"TEST_TELEGRAM_TOKEN": "test-token", "TEST_TELEGRAM_CHAT": "12345"}), \
             patch("discover.requests.post", return_value=response) as post:
            self.assertTrue(send_telegram_summary(config, new_summary()))
        self.assertIn("/bottest-token/sendMessage", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["data"]["chat_id"], "12345")

    def test_telegram_summary_does_not_send_when_credentials_are_unavailable(self):
        config = {"telegram_summary": {"bot_token_env": "MISSING_TOKEN", "chat_id_env": "MISSING_CHAT"}}
        with patch.dict("os.environ", {}, clear=True), patch("discover.requests.post") as post:
            self.assertFalse(send_telegram_summary(config, new_summary()))
        post.assert_not_called()
    def test_telegram_summary_tolerates_delivery_failure(self):
        config = {"telegram_summary": {"bot_token_env": "TEST_TELEGRAM_TOKEN", "chat_id_env": "TEST_TELEGRAM_CHAT"}}
        with patch.dict("os.environ", {"TEST_TELEGRAM_TOKEN": "test-token", "TEST_TELEGRAM_CHAT": "12345"}), \
             patch("discover.requests.post", side_effect=requests.ConnectionError):
            self.assertFalse(send_telegram_summary(config, new_summary()))


if __name__ == "__main__":
    unittest.main()
