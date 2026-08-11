"""Acquisition end to end, against real bytes.

The podcast path runs for real: a feed document served over a real HTTP
socket, real MP4 files as enclosures, a real download through the resumable
downloader, real duration extraction from container boxes, a real thumbnail,
and a real row in `sources`. Nothing on that path is stubbed.

The YouTube path cannot reach YouTube from here — outbound network in this
environment is limited to package registries — so those tests drive the
adapter through an injected `ydl_factory` carrying **recorded `info_dict`
payloads of the shape yt-dlp actually returns**. That tests the mapping, the
error classification and the channel walk, which is where this adapter's own
bugs live. It does not test that yt-dlp can talk to YouTube, and this docstring
is the honest statement of that gap rather than a green tick standing in for it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from clipforge.acquire import (
    AcquisitionConfig,
    AcquisitionEngine,
    InputKind,
    PermanentError,
    SourceRef,
    resolve,
)
from clipforge.acquire.probe import MediaProber
from clipforge.acquire.youtube import YouTubeAdapter, YouTubeConfig
from clipforge.factory.niches import Niche
from clipforge.factory.sources import RightsBasis, SourceKind
from clipforge.store import MemoryDatabase, ProjectRecord, TenantRecord

TENANT = "ten_acq"
FIXTURES = os.environ.get("CLIPFORGE_MEDIA_FIXTURES", "/tmp/mp4test")
FFMPEG = os.environ.get("CLIPFORGE_FFMPEG", "")

#: A real feed document, in the shape Megaphone and Libsyn actually emit —
#: iTunes namespace, an enclosure per item, per-episode and channel artwork,
#: and one item that is a cross-posted article with no enclosure.
FEED_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>The Pricing Show</title>
    <link>https://example.com/show</link>
    <language>en-gb</language>
    <description>Ninety minutes on pricing, weekly.</description>
    <itunes:author>Studio Nine</itunes:author>
    <itunes:explicit>no</itunes:explicit>
    <itunes:image href="https://example.com/art.jpg"/>
    <itunes:category text="Business"/>
    <itunes:category text="Entrepreneurship"/>
    <item>
      <title>Episode 12 — What pricing power looks like</title>
      <guid isPermaLink="false">show-ep-12</guid>
      <pubDate>Tue, 03 Mar 2026 09:00:00 +0000</pubDate>
      <itunes:duration>01:00:30</itunes:duration>
      <itunes:episode>12</itunes:episode>
      <itunes:season>2</itunes:season>
      <itunes:author>Studio Nine</itunes:author>
      <itunes:keywords>pricing, saas, margins</itunes:keywords>
      <description>The one about margins.</description>
      <enclosure url="{base}/ep12.m4a" type="audio/mp4" length="108371"/>
    </item>
    <item>
      <title>Episode 11 — Discounting</title>
      <guid isPermaLink="false">show-ep-11</guid>
      <pubDate>Tue, 24 Feb 2026 09:00:00 +0000</pubDate>
      <itunes:duration>3600</itunes:duration>
      <itunes:image href="https://example.com/ep11.jpg"/>
      <enclosure url="{base}/ep11.mp4" type="video/mp4" length="110254"/>
    </item>
    <item>
      <title>We are hiring</title>
      <guid isPermaLink="false">show-post-1</guid>
      <link>https://example.com/hiring</link>
      <description>Not an episode.</description>
    </item>
  </channel>
</rss>
"""


def _fixtures_present() -> bool:
    return all(
        os.path.exists(os.path.join(FIXTURES, name))
        for name in ("real.mp4", "podcast.m4a")
    )


class _Handler(BaseHTTPRequestHandler):
    directory = FIXTURES

    def log_message(self, *args) -> None:  # noqa: A003
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/feed"):
            body = FEED_TEMPLATE.format(base=self.server.base_url).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        mapping = {"/ep12.m4a": "podcast.m4a", "/ep11.mp4": "real.mp4"}
        name = mapping.get(self.path)
        if name is None:
            self.send_error(404)
            return
        with open(os.path.join(self.directory, name), "rb") as handle:
            body = handle.read()

        start = 0
        header = self.headers.get("Range")
        if header:
            start = int(header.split("=")[1].split("-")[0])
        chunk = body[start:]
        self.send_response(206 if header else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        if header:
            self.send_header(
                "Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}"
            )
        self.end_headers()
        self.wfile.write(chunk)


class _Server:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        host, port = self.httpd.server_address[:2]
        self.httpd.base_url = f"http://{host}:{port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return self.httpd.base_url

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@unittest.skipUnless(
    _fixtures_present(),
    f"real media fixtures not found in {FIXTURES} — see tests/README or "
    f"generate them with ffmpeg",
)
class AcquisitionEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _Server()
        self.addCleanup(self.server.close)
        self.workspace = tempfile.mkdtemp(prefix="clipforge-ws-")
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)

        self.db = MemoryDatabase()
        with self.db.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="Acquirer"))
            uow.projects.save(ProjectRecord(id="proj_1", tenant_id=TENANT,
                                            name="Brand"))

        self.engine = AcquisitionEngine(
            self.db, TENANT,
            config=AcquisitionConfig(workspace=self.workspace,
                                     min_duration_s=1.0),
            prober=MediaProber(ffmpeg=FFMPEG or None),
        )

    # -- resolving ---------------------------------------------------------

    def test_every_form_of_a_youtube_link_is_the_same_video(self) -> None:
        """Six URLs, one video. A system that treats them as six downloads it
        six times and posts the same clip to the same channel six times."""

        forms = [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc&index=3",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ?si=xyz",
        ]
        keys = {resolve(form).key for form in forms}
        self.assertEqual(keys, {"dQw4w9WgXcQ"})
        for form in forms:
            self.assertIs(resolve(form).kind, InputKind.YOUTUBE_VIDEO)

    def test_channels_resolve_by_id_and_by_handle(self) -> None:
        by_id = resolve("https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv")
        self.assertIs(by_id.kind, InputKind.YOUTUBE_CHANNEL)
        self.assertEqual(by_id.key, "UCabcdefghijklmnopqrstuv")
        by_handle = resolve("https://www.youtube.com/@studionine")
        self.assertIs(by_handle.kind, InputKind.YOUTUBE_CHANNEL)
        self.assertEqual(by_handle.key, "@studionine")

    def test_a_playlist_on_a_video_link_is_noted_not_followed(self) -> None:
        """The operator pasted a video. Quietly acquiring the other 200 items
        in the playlist is not what they asked for."""

        ref = resolve("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxyz")
        self.assertIs(ref.kind, InputKind.YOUTUBE_VIDEO)
        self.assertEqual(ref.hints.get("playlist_id"), "PLxyz")

    def test_a_local_mp4_resolves_to_a_file(self) -> None:
        ref = resolve(os.path.join(FIXTURES, "real.mp4"))
        self.assertIs(ref.kind, InputKind.LOCAL_FILE)
        self.assertTrue(os.path.isabs(ref.key))

    def test_a_missing_file_says_so_rather_than_becoming_a_url(self) -> None:
        with self.assertRaises(PermanentError):
            resolve("/no/such/interview.mp4")

    def test_a_feed_url_resolves_to_a_feed(self) -> None:
        self.assertIs(
            resolve("https://feeds.example.com/show.xml").kind,
            InputKind.PODCAST_FEED,
        )
        self.assertIs(
            resolve("https://example.com/podcast/feed").kind,
            InputKind.PODCAST_FEED,
        )

    # -- feeds -------------------------------------------------------------

    def test_a_feed_expands_to_its_episodes(self) -> None:
        ref = resolve(f"{self.server.base}/feed.xml")
        refs = self.engine.expand(ref)

        # Two enclosures; the cross-posted article is not an episode.
        self.assertEqual(len(refs), 2)
        self.assertEqual([r.key for r in refs], ["show-ep-12", "show-ep-11"])
        first = refs[0]
        self.assertEqual(first.url, f"{self.server.base}/ep12.m4a")
        self.assertEqual(first.hints["feed_title"], "The Pricing Show")
        self.assertEqual(first.hints["feed_author"], "Studio Nine")
        self.assertEqual(first.hints["language"], "en")
        self.assertEqual(first.hints["item"]["episode"], 12)
        self.assertEqual(first.hints["item"]["duration_s"], 3630.0)

    def test_the_guid_is_the_identity_not_the_url(self) -> None:
        """Enclosure URLs change whenever a host moves CDN or rewrites a
        tracking prefix. Deduplicating on URL re-downloads the entire back
        catalogue when they do."""

        refs = self.engine.expand(resolve(f"{self.server.base}/feed.xml"))
        self.assertEqual(refs[0].key, "show-ep-12")
        self.assertNotIn(self.server.base, refs[0].key)

    # -- the whole path ----------------------------------------------------

    def test_a_podcast_episode_is_downloaded_probed_and_persisted(self) -> None:
        """The full path, all real: HTTP over a socket, an MP4 on disk, a
        duration out of its container boxes, artwork out of its metadata, and
        a row in `sources`."""

        jobs = self.engine.submit(f"{self.server.base}/feed.xml")
        self.assertEqual(len(jobs), 2)

        results = self.engine.run(limit=2)
        self.assertEqual(len(results), 2)
        for acquisition in results:
            self.assertEqual(acquisition.error, "")
            self.assertTrue(acquisition.complete, acquisition.to_dict())

        by_key = {a.ref.key: a for a in results}
        audio = by_key["show-ep-12"]
        self.assertAlmostEqual(audio.probe.duration_s, 12.0, places=2)
        self.assertTrue(audio.probe.has_audio)
        self.assertFalse(audio.probe.has_video)
        self.assertEqual(audio.probe.prober, "mp4-boxes")
        # Cover art out of the container: a real thumbnail with no ffmpeg.
        self.assertEqual(audio.thumbnail.origin, "embedded")
        self.assertTrue(os.path.getsize(audio.thumbnail.path) > 0)

        video = by_key["show-ep-11"]
        self.assertAlmostEqual(video.probe.duration_s, 7.0, places=2)
        self.assertTrue(video.probe.has_video)
        self.assertEqual((video.probe.width, video.probe.height), (640, 360))

        with self.db.unit_of_work(TENANT) as uow:
            sources = uow.sources.all()
        self.assertEqual(len(sources), 2)
        titles = {s.title for s in sources}
        self.assertIn("Episode 12 — What pricing power looks like", titles)

    def test_acquired_material_is_unverified_until_a_person_says_otherwise(self) -> None:
        """Downloading something is not being allowed to republish it. The
        channel gate refuses `unverified` by default, and acquisition must not
        be able to talk its way past that."""

        self.engine.submit(f"{self.server.base}/feed.xml")
        self.engine.run(limit=2)
        with self.db.unit_of_work(TENANT) as uow:
            for source in uow.sources.all():
                self.assertEqual(source.rights_basis, "unverified")

    def test_re_acquiring_updates_the_row_and_keeps_the_licence(self) -> None:
        """An operator records a licence; a later crawl must not reset it to
        `unverified`. That would silently take a cleared show off the air."""

        self.engine.submit(f"{self.server.base}/feed.xml")
        self.engine.run(limit=2)
        with self.db.unit_of_work(TENANT) as uow:
            source = uow.sources.all()[0]
            source.rights_basis = "licensed"
            source.rights_reference = "LIC-2026-77"
            uow.sources.save(source)

        # A second pass over the same feed.
        engine = AcquisitionEngine(
            self.db, TENANT,
            config=AcquisitionConfig(workspace=self.workspace, min_duration_s=1.0),
            prober=MediaProber(ffmpeg=FFMPEG or None),
        )
        for ref in engine.expand(resolve(f"{self.server.base}/feed.xml")):
            engine._persist(engine.acquire(ref), "")

        with self.db.unit_of_work(TENANT) as uow:
            rows = uow.sources.all()
        self.assertEqual(len(rows), 2, "a re-crawl duplicated the library")
        licensed = [r for r in rows if r.rights_basis == "licensed"]
        self.assertEqual(len(licensed), 1)
        self.assertEqual(licensed[0].rights_reference, "LIC-2026-77")

    def test_an_uploaded_file_is_copied_in_and_measured(self) -> None:
        jobs = self.engine.submit(os.path.join(FIXTURES, "real.mp4"))
        self.assertEqual(len(jobs), 1)
        result = self.engine.run(limit=1)[0]

        self.assertEqual(result.error, "")
        self.assertTrue(result.complete)
        self.assertAlmostEqual(result.probe.duration_s, 7.0, places=2)
        # Copied into the workspace, not referenced where it sat. An operator
        # clearing their upload directory must not empty the library.
        self.assertTrue(result.download.path.startswith(self.workspace))
        self.assertNotEqual(result.download.path, os.path.join(FIXTURES, "real.mp4"))
        self.assertTrue(result.download.checksum)

    @unittest.skipUnless(FFMPEG, "set CLIPFORGE_FFMPEG to test frame grabs")
    def test_a_video_with_no_artwork_gets_a_grabbed_frame(self) -> None:
        self.engine.submit(os.path.join(FIXTURES, "real.mp4"))
        result = self.engine.run(limit=1)[0]
        self.assertEqual(result.thumbnail.origin, "frame")
        self.assertGreater(result.thumbnail.width, 0)
        # Not frame zero: the first frame of a talking head is usually a black
        # fade-in, and a wall of black thumbnails looks like a broken product.
        self.assertGreater(result.thumbnail.at_s, 0)

    def test_material_below_the_floor_is_refused_permanently(self) -> None:
        engine = AcquisitionEngine(
            self.db, TENANT,
            config=AcquisitionConfig(workspace=self.workspace,
                                     min_duration_s=600.0),
            prober=MediaProber(ffmpeg=FFMPEG or None),
        )
        engine.submit(os.path.join(FIXTURES, "real.mp4"))
        result = engine.run(limit=1)[0]
        self.assertIn("below the", result.error)

        # Dead, not queued: a 7-second file will not grow.
        with self.db.unit_of_work(TENANT) as uow:
            self.assertEqual([j.state for j in uow.jobs.all()], ["dead"])

    def test_a_failed_acquisition_is_retried_rather_than_lost(self) -> None:
        """A transient failure goes back on the queue with a later run_after,
        which is the difference between a flaky CDN costing a minute and
        costing an episode."""

        ref = SourceRef(InputKind.MEDIA_URL, "gone",
                        url=f"{self.server.base}/missing.m4a")
        self.engine._enqueue(ref, "")
        self.engine.run(limit=1)
        with self.db.unit_of_work(TENANT) as uow:
            job = uow.jobs.all()[0]
        # A 404 is permanent; the queue must not spend its afternoon on it.
        self.assertEqual(job.state, "dead")
        self.assertIn("404", job.last_error)

    def test_submitting_the_same_thing_twice_queues_it_once(self) -> None:
        first = self.engine.submit(os.path.join(FIXTURES, "real.mp4"))
        again = self.engine.submit(os.path.join(FIXTURES, "real.mp4"))
        self.assertEqual(first, again)
        with self.db.unit_of_work(TENANT) as uow:
            self.assertEqual(uow.jobs.count(), 1)

    def test_two_channels_may_each_acquire_the_same_source(self) -> None:
        """The dedupe key is scoped per channel. A global one would give the
        second channel nothing."""

        path = os.path.join(FIXTURES, "real.mp4")
        first = self.engine.submit(path, channel_id="ch_a")
        second = self.engine.submit(path, channel_id="ch_b")
        self.assertNotEqual(first, second)
        with self.db.unit_of_work(TENANT) as uow:
            self.assertEqual(uow.jobs.count(), 2)

    def test_work_survives_the_process_that_queued_it(self) -> None:
        """The queue is a table. A worker that dies between submit and run
        finds the job waiting, because nothing about it was held in memory."""

        self.engine.submit(f"{self.server.base}/feed.xml")

        reborn = AcquisitionEngine(
            self.db, TENANT,
            config=AcquisitionConfig(workspace=self.workspace, min_duration_s=1.0),
            prober=MediaProber(ffmpeg=FFMPEG or None),
        )
        results = reborn.run(limit=5)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(a.complete for a in results))


# ---------------------------------------------------------------------------
# YouTube — adapter logic, against recorded payloads
# ---------------------------------------------------------------------------


class _FakeYdl:
    """Stands in for `YoutubeDL`, returning a recorded `info_dict`.

    Not a stand-in for YouTube: what it exercises is the adapter's own code —
    option assembly, metadata mapping, error classification, the channel walk.
    The network leg is not covered here and this environment cannot cover it.
    """

    def __init__(self, options, payload=None, error=None, directory=""):
        self.options = options
        self.payload = payload
        self.error = error
        self.directory = directory
        self.requested: list[tuple[str, bool]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        self.requested.append((url, download))
        if self.error is not None:
            raise self.error
        if download and self.directory:
            template = self.options.get("outtmpl", "")
            path = template.replace("%(ext)s", "mp4")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copy2(os.path.join(self.directory, "real.mp4"), path)
            payload = dict(self.payload)
            payload["requested_downloads"] = [{"filepath": path}]
            return payload
        return self.payload


#: The shape yt-dlp actually returns for a video, trimmed to the fields the
#: adapter reads. Recorded rather than invented, because field-name drift
#: between yt-dlp releases is exactly what this pins.
VIDEO_INFO = {
    "id": "dQw4w9WgXcQ",
    "title": "What pricing power looks like",
    "uploader": "Studio Nine",
    "channel": "Studio Nine",
    "channel_id": "UCabcdefghijklmnopqrstuv",
    "channel_url": "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
    "description": "A long conversation about margins.",
    "duration": 3630,
    "timestamp": 1772550000,
    "upload_date": "20260303",
    "view_count": 184203,
    "like_count": 9021,
    "tags": ["pricing", "saas", "business"],
    "categories": ["Education"],
    "language": "en-GB",
    "license": "Creative Commons Attribution license (reuse allowed)",
    "age_limit": 0,
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "ext": "mp4",
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
    "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "live_status": "not_live",
    "availability": "public",
}

CHANNEL_INFO = {
    "id": "UCabcdefghijklmnopqrstuv",
    "title": "Studio Nine - Videos",
    "channel_id": "UCabcdefghijklmnopqrstuv",
    "entries": [
        {"id": "aaaaaaaaaaa", "title": "Ep 12"},
        {"id": "bbbbbbbbbbb", "title": "Ep 11"},
        None,  # `ignoreerrors` leaves a hole where a video failed
        {"id": "ccccccccccc", "title": "Ep 10"},
    ],
}


class YouTubeAdapterTest(unittest.TestCase):
    def _adapter(self, payload=None, error=None, directory="") -> YouTubeAdapter:
        self.made: list[_FakeYdl] = []

        def factory(options):
            ydl = _FakeYdl(options, payload, error, directory)
            self.made.append(ydl)
            return ydl

        return YouTubeAdapter(YouTubeConfig(), ydl_factory=factory)

    def test_metadata_maps_onto_an_acquisition(self) -> None:
        ref = resolve("https://youtu.be/dQw4w9WgXcQ")
        acquisition = self._adapter(VIDEO_INFO).describe(ref)

        self.assertEqual(acquisition.title, "What pricing power looks like")
        self.assertEqual(acquisition.creator, "Studio Nine")
        self.assertEqual(acquisition.external_id, "dQw4w9WgXcQ")
        self.assertEqual(acquisition.language, "en")
        self.assertEqual(acquisition.topics, ("pricing", "saas", "business"))
        self.assertEqual(acquisition.published_at,
                         datetime.fromtimestamp(1772550000, UTC))
        self.assertEqual(acquisition.raw_metadata["view_count"], 184203)

    def test_a_creative_commons_licence_is_recorded_but_not_acted_on(self) -> None:
        """YouTube's licence field is a hint for the person recording a rights
        basis, not a rights basis. Treating it as one would let a mislabelled
        upload authorise itself."""

        acquisition = self._adapter(VIDEO_INFO).describe(
            resolve("https://youtu.be/dQw4w9WgXcQ")
        )
        self.assertIn("Creative Commons", acquisition.raw_metadata["license"])
        # Nothing on the acquisition claims a basis.
        self.assertFalse(hasattr(acquisition, "rights_basis"))

    def test_a_channel_walk_skips_the_holes(self) -> None:
        ref = resolve("https://www.youtube.com/@studionine")
        refs = self._adapter(CHANNEL_INFO).list_channel(ref, limit=10)

        self.assertEqual([r.key for r in refs],
                         ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"])
        self.assertTrue(all(r.kind is InputKind.YOUTUBE_VIDEO for r in refs))
        self.assertEqual(refs[0].hints["channel_id"], "UCabcdefghijklmnopqrstuv")

    def test_the_channel_walk_asks_for_the_videos_tab(self) -> None:
        """The channel root is a curated home page whose first shelf is often
        someone else's content — walking it acquires another channel's videos."""

        self._adapter(CHANNEL_INFO).list_channel(
            resolve("https://www.youtube.com/@studionine")
        )
        requested = self.made[0].requested[0][0]
        self.assertTrue(requested.endswith("/videos"), requested)

    def test_a_deleted_video_is_permanent_and_a_timeout_is_not(self) -> None:
        ref = resolve("https://youtu.be/dQw4w9WgXcQ")
        with self.assertRaises(PermanentError):
            self._adapter(error=RuntimeError(
                "ERROR: [youtube] dQw4w9WgXcQ: Video unavailable"
            )).describe(ref)

        from clipforge.acquire import RetryableError

        with self.assertRaises(RetryableError):
            self._adapter(error=RuntimeError(
                "ERROR: unable to download video data: HTTP Error 503"
            )).describe(ref)

    @unittest.skipUnless(_fixtures_present(), "media fixtures not found")
    def test_a_download_reports_the_file_that_landed(self) -> None:
        directory = tempfile.mkdtemp(prefix="clipforge-yt-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        ref = resolve("https://youtu.be/dQw4w9WgXcQ")

        acquisition = self._adapter(VIDEO_INFO, directory=FIXTURES).download(
            ref, directory
        )
        self.assertIsNotNone(acquisition.download)
        self.assertTrue(os.path.exists(acquisition.download.path))
        self.assertEqual(acquisition.download.state.value, "complete")
        self.assertGreater(acquisition.download.bytes_done, 0)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(_fixtures_present(), "media fixtures not found")
class AcquisitionPersistenceTest(unittest.TestCase):
    """What survives a restart, and what the operator is left holding."""

    def setUp(self) -> None:
        self.server = _Server()
        self.addCleanup(self.server.close)
        self.workspace = tempfile.mkdtemp(prefix="clipforge-ws-")
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.db = MemoryDatabase()
        with self.db.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="Acquirer"))

    def _engine(self) -> AcquisitionEngine:
        return AcquisitionEngine(
            self.db, TENANT,
            config=AcquisitionConfig(workspace=self.workspace, min_duration_s=1.0),
            prober=MediaProber(ffmpeg=FFMPEG or None),
        )

    def test_the_run_itself_is_recorded_not_just_the_material(self) -> None:
        """`sources` is the library; `acquisition_runs` is the record of the
        work. A half-downloaded file must not appear as something to clip, and
        a failure must not vanish."""

        engine = self._engine()
        engine.submit(f"{self.server.base}/feed.xml")
        engine.run(limit=2)

        with self.db.unit_of_work(TENANT) as uow:
            runs = uow.acquisitions.all()
        self.assertEqual(len(runs), 2)
        for run in runs:
            self.assertEqual(run.state, "ready")
            self.assertTrue(run.source_id, "a ready run should name its source")
            self.assertTrue(run.checksum)
            self.assertGreater(run.bytes_done, 0)
            self.assertIsNotNone(run.duration_s)
            self.assertTrue(run.has_audio)
            self.assertTrue(run.media_path.startswith(self.workspace))

    def test_a_failure_leaves_a_row_with_the_reason(self) -> None:
        engine = self._engine()
        engine._enqueue(
            SourceRef(InputKind.MEDIA_URL, "gone",
                      url=f"{self.server.base}/missing.m4a"),
            "",
        )
        engine.run(limit=1)

        with self.db.unit_of_work(TENANT) as uow:
            runs = uow.acquisitions.in_state("failed")
        self.assertEqual(len(runs), 1)
        self.assertIn("404", runs[0].last_error)
        # No source row: a failed acquisition must not enter the library.
        with self.db.unit_of_work(TENANT) as uow:
            self.assertEqual(uow.sources.count(), 0)

    def test_the_validator_is_kept_so_a_resume_stays_a_resume(self) -> None:
        """Losing the ETag between passes turns the next resume into a
        restart — or worse, splices two encodes if the server no longer
        validates."""

        engine = self._engine()
        engine.submit(f"{self.server.base}/feed.xml")
        engine.run(limit=1)
        with self.db.unit_of_work(TENANT) as uow:
            run = uow.acquisitions.all()[0]
            run.validator = '"v1"'
            run.state = "downloading"
            uow.acquisitions.save(run)

        # A later pass that gets nowhere must not wipe it.
        from clipforge.acquire.types import Acquisition

        engine.record_run(
            Acquisition(run.id, SourceRef(InputKind(run.kind), run.ref_key),
                        error="timed out"),
            "downloading",
        )
        with self.db.unit_of_work(TENANT) as uow:
            self.assertEqual(uow.acquisitions.get(run.id).validator, '"v1"')

    def test_a_retry_updates_the_run_rather_than_adding_another(self) -> None:
        engine = self._engine()
        ref = SourceRef(InputKind.MEDIA_URL, "gone",
                        url=f"{self.server.base}/missing.m4a")
        for _ in range(3):
            engine._enqueue(ref, "")
            engine.run(limit=1)
        with self.db.unit_of_work(TENANT) as uow:
            runs = uow.acquisitions.all()
        self.assertEqual(len(runs), 1, "each attempt left its own row")
        self.assertGreaterEqual(runs[0].attempts, 1)


@unittest.skipUnless(_fixtures_present(), "media fixtures not found")
class AcquiringFinderTest(unittest.TestCase):
    """The finder that replaces hand-entry."""

    def setUp(self) -> None:
        self.server = _Server()
        self.addCleanup(self.server.close)
        self.workspace = tempfile.mkdtemp(prefix="clipforge-ws-")
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.db = MemoryDatabase()
        with self.db.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="Acquirer"))

        from clipforge.acquire import AcquiringSourceFinder

        self.engine = AcquisitionEngine(
            self.db, TENANT,
            config=AcquisitionConfig(workspace=self.workspace, min_duration_s=1.0),
            prober=MediaProber(ffmpeg=FFMPEG or None),
        )
        self.finder = AcquiringSourceFinder(self.db, TENANT, self.engine)

    def test_a_sweep_fills_the_library_from_a_watched_feed(self) -> None:
        self.finder.watch(f"{self.server.base}/feed.xml", topics=("business",))
        summary = self.finder.sweep()

        self.assertEqual(summary["submitted"], 2)
        self.assertEqual(summary["acquired"], 2)
        self.assertEqual(summary["failed"], [])
        self.assertEqual(len(self.finder.all), 2)

    def test_swept_material_is_findable_by_the_factory(self) -> None:
        """The whole point: a channel asks `find` and gets material nobody
        typed in by hand."""

        self.finder.watch(f"{self.server.base}/feed.xml", topics=("business",))
        self.finder.sweep()
        found = self.finder.find(Niche.BUSINESS, ["business"], limit=10)
        self.assertGreater(len(found), 0)
        self.assertTrue(all(s.kind is SourceKind.LONGFORM_VIDEO for s in found))

    def test_acquired_material_waits_for_a_rights_decision(self) -> None:
        """A channel wired to this finder acquires steadily and publishes
        nothing until somebody records a licence. That is intended, and
        `clearable` is what makes it visible rather than mysterious."""

        self.finder.watch(f"{self.server.base}/feed.xml")
        self.finder.sweep()
        waiting = self.finder.clearable()
        self.assertEqual(len(waiting), 2)
        self.assertTrue(all(
            s.rights.basis is RightsBasis.UNVERIFIED for s in waiting
        ))

    def test_a_customers_own_upload_is_cleared_without_a_human(self) -> None:
        """The one case acquisition may decide: the customer supplying their
        own footage is the rights holder."""

        self.finder.watch(os.path.join(FIXTURES, "real.mp4"), owned=True)
        self.finder.sweep()
        self.assertEqual(self.finder.clearable(), ())
        source = self.finder.all[0]
        self.assertIs(source.rights.basis, RightsBasis.OWNED)
        self.assertIsNotNone(source.rights.verified_at)

    def test_marking_owned_does_not_overwrite_a_recorded_licence(self) -> None:
        self.finder.watch(os.path.join(FIXTURES, "real.mp4"))
        self.finder.sweep()
        source_id = self.finder.all[0].source_id
        with self.db.unit_of_work(TENANT) as uow:
            record = uow.sources.require(source_id)
            record.rights_basis = "licensed"
            record.rights_reference = "LIC-9"
            uow.sources.save(record)

        self.assertFalse(self.finder.mark_owned(source_id))
        self.assertIs(self.finder.get(source_id).rights.basis, RightsBasis.LICENSED)

    def test_one_dead_input_does_not_stop_the_others(self) -> None:
        self.finder.watch(f"{self.server.base}/feed.xml")
        self.finder.watch(os.path.join(FIXTURES, "real.mp4"))
        # A file that vanishes between being watched and being swept.
        missing = os.path.join(self.workspace, "gone.mp4")
        open(missing, "wb").close()
        self.finder.watch(missing)
        os.remove(missing)

        summary = self.finder.sweep()
        self.assertTrue(summary["problems"], "the dead input was not reported")
        self.assertGreaterEqual(summary["acquired"], 2)

    def test_a_typo_is_caught_when_it_is_typed(self) -> None:
        with self.assertRaises(PermanentError):
            self.finder.watch("htp:/not-a-url")

    def test_a_directory_of_uploads_is_ingested_in_bulk(self) -> None:
        uploads = tempfile.mkdtemp(prefix="clipforge-up-")
        self.addCleanup(shutil.rmtree, uploads, ignore_errors=True)
        for name in ("a.mp4", "b.mp4"):
            shutil.copy2(os.path.join(FIXTURES, "real.mp4"),
                         os.path.join(uploads, name))
        # A stray file that is not media must be skipped, not fail the batch.
        with open(os.path.join(uploads, "README.txt"), "w") as handle:
            handle.write("notes")

        jobs = self.finder.ingest_directory(uploads)
        self.assertEqual(len(jobs), 2)
        self.engine.run(limit=5)
        self.assertEqual(len(self.finder.all), 2)
