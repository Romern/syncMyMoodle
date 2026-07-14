import hashlib
import ssl
from importlib import resources
from pathlib import Path

import pytest

from syncmymoodle import downloader, emedia, links
from syncmymoodle.constants import EMEDIA_API_URL, EMEDIA_URL
from syncmymoodle.node import Node, RemoteMarkerKind

from .helpers import FakeResponse, FakeSession, make_context, node_path

PLAYLIST_URL = (
    "https://wms01-avmz.germanywestcentral.cloudapp.azure.com/veira/"
    "_definst/mp4:Clinical Lecture.mp4/playlist.m3u8"
)
MANIFEST_URL = PLAYLIST_URL.rsplit("/", 1)[0] + "/manifest.mpd"


def dash_manifest(session_id: int = 123, duration: int = 90000) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"
     type="static"
     publishTime="2026-07-14T15:29:09.627Z"
     mediaPresentationDuration="PT1S">
  <Location>{MANIFEST_URL.replace("manifest.mpd", f"manifest_w{session_id}.mpd")}</Location>
  <Period id="0">
    <AdaptationSet mimeType="video/mp4" width="1280" height="720">
      <SegmentTemplate timescale="90000"
                       media="segment_$Time$_w{session_id}_mpd.m4s"
                       initialization="init_w{session_id}_mpd.m4s">
        <SegmentTimeline><S t="0" d="{duration}"/></SegmentTimeline>
      </SegmentTemplate>
      <Representation id="1" bandwidth="282569" codecs="avc1.42001f"/>
    </AdaptationSet>
  </Period>
</MPD>""".encode()


@pytest.mark.parametrize(
    "link",
    [
        "https://emedia-medizin.rwth-aachen.de/web/veira_fe/#/watch/540",
        "https://emedia-medizin.rwth-aachen.de/app/veira_fe/#/watch/540",
    ],
)
def test_extract_video_id_accepts_legacy_and_current_links(link):
    assert emedia.extract_video_id(link) == 540


@pytest.mark.parametrize(
    "link",
    [
        "https://emedia-medizin.rwth-aachen.de/app/veira_fe/",
        "https://emedia-medizin.rwth-aachen.de/app/veira_fe/#/lecturer/540",
        "https://example.test/app/veira_fe/#/watch/540",
    ],
)
def test_extract_video_id_rejects_non_video_links(link):
    assert emedia.extract_video_id(link) is None


def test_single_emedia_link_resolves_public_api_without_login(monkeypatch):
    link = "https://emedia-medizin.rwth-aachen.de/web/veira_fe/#/watch/540"
    api_session = FakeSession()
    posted = []

    def metadata_response(url, kwargs):
        del url
        posted.append(kwargs)
        return FakeResponse(
            json_payload={
                "records": [
                    {
                        "id": "540",
                        "title": "API title",
                        "wowza_url": PLAYLIST_URL,
                    }
                ]
            }
        )

    api_session.add("POST", EMEDIA_API_URL, metadata_response)
    api_session.add("GET", MANIFEST_URL, FakeResponse(content=dash_manifest()))
    monkeypatch.setattr(emedia.shutil, "which", lambda executable: f"/{executable}")
    ctx = make_context()
    ctx.session = FakeSession()
    ctx.emedia_api_session = api_session
    parent = Node("Section", 1, "Section", None)

    links.scan_for_links(
        ctx,
        link,
        parent,
        course_id=101,
        module_title="Clinical Lecture 1.2",
        single=True,
    )
    assert emedia.resolve_video(ctx, 540) == emedia.EmediaVideo(
        540, "API title", PLAYLIST_URL
    )

    assert ctx.session.calls == []
    assert api_session.calls == [
        ("POST", EMEDIA_API_URL),
        ("GET", MANIFEST_URL),
    ]
    assert posted[0]["json"] == {"id": 540}
    assert posted[0]["headers"] == {
        "Origin": EMEDIA_URL.rstrip("/"),
        "Referer": EMEDIA_URL,
    }
    assert len(parent.children) == 1
    video = parent.children[0]
    assert (video.name, video.id, video.type, video.url) == (
        "Clinical Lecture 1.2.mp4",
        540,
        "Emedia",
        PLAYLIST_URL,
    )
    assert video.download_headers == {
        "Origin": EMEDIA_URL.rstrip("/"),
        "Referer": EMEDIA_URL,
    }
    assert video.etag == emedia.manifest_revision_marker(PLAYLIST_URL, dash_manifest())
    assert video.etag_kind is RemoteMarkerKind.OPAQUE


def test_manifest_revision_ignores_generated_session_values():
    first = emedia.manifest_revision_marker(PLAYLIST_URL, dash_manifest(123))
    second = emedia.manifest_revision_marker(PLAYLIST_URL, dash_manifest(456))
    changed = emedia.manifest_revision_marker(
        PLAYLIST_URL, dash_manifest(456, duration=180000)
    )

    assert first == second
    assert first != changed


def test_emedia_without_ffmpeg_warns_once_and_uses_ts_extension(monkeypatch, caplog):
    monkeypatch.setattr(emedia.shutil, "which", lambda executable: None)
    ctx = make_context()
    ctx.emedia_video_cache[540] = emedia.EmediaVideo(540, "API title", PLAYLIST_URL)
    ctx.emedia_revision_cache[PLAYLIST_URL] = "revision"

    first_parent = Node("First", 1, "Section", None)
    second_parent = Node("Second", 2, "Section", None)
    link = "https://emedia-medizin.rwth-aachen.de/web/veira_fe/#/watch/540"
    emedia.add_video_node(ctx, first_parent, link, "Clinical Lecture.mp4")
    emedia.add_video_node(ctx, second_parent, link, "Clinical Lecture.mp4")

    assert first_parent.children[0].name == "Clinical Lecture.ts"
    assert second_parent.children[0].name == "Clinical Lecture.ts"
    assert caplog.text.count("FFmpeg is unavailable") == 1


def test_emedia_api_outage_stops_after_shared_threshold(caplog):
    api_session = FakeSession()
    api_session.add("POST", EMEDIA_API_URL, FakeResponse(status_code=503))
    ctx = make_context()
    ctx.emedia_api_session = api_session

    for video_id in (540, 541, 542, 543):
        assert emedia.resolve_video(ctx, video_id) is None

    assert api_session.count("POST", EMEDIA_API_URL) == 3
    assert ctx.service_outages.should_skip(EMEDIA_API_URL)
    assert "skipping remaining requests for this sync" in caplog.text


def test_emedia_rejects_non_https_metadata_and_caches_failure(caplog):
    api_session = FakeSession()
    api_session.add(
        "POST",
        EMEDIA_API_URL,
        FakeResponse(
            json_payload={
                "records": [
                    {
                        "id": "540",
                        "title": "Internal file",
                        "wowza_url": "http://127.0.0.1/private.m3u8",
                    }
                ]
            }
        ),
    )
    ctx = make_context()
    ctx.emedia_api_session = api_session

    assert emedia.resolve_video(ctx, 540) is None
    assert emedia.resolve_video(ctx, 540) is None

    assert api_session.calls == [("POST", EMEDIA_API_URL)]
    assert "no usable metadata" in caplog.text


@pytest.mark.parametrize(
    ("filename", "temporary_filename", "expected_fixup"),
    [
        ("Clinical Lecture.mp4", ".Clinical Lecture.smmpart.mp4", None),
        ("Clinical Lecture.ts", ".Clinical Lecture.smmpart.ts", "never"),
    ],
)
def test_emedia_download_uses_best_stream_and_exact_node_name(
    tmp_path,
    monkeypatch,
    filename,
    temporary_filename,
    expected_fixup,
):
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, opts):
            self.opts = opts
            captured["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, link, download):
            pytest.fail("size metadata should not be requested without size filters")

        def download(self, urls):
            captured["urls"] = urls
            output = Path(self.opts["outtmpl"])
            output.write_bytes(b"video bytes")
            return 0

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    ctx = make_context({"paths.sync_directory": str(tmp_path)})
    root = Node("", -1, "Root", None)
    section = root.add_child("Section", 1, "Section")
    assert section is not None
    video = section.add_child(
        filename,
        540,
        "Emedia",
        url=PLAYLIST_URL,
        download_headers={"Referer": EMEDIA_URL},
    )
    assert video is not None

    downloader.download_node_tree(ctx, root)

    output = node_path(ctx, video)
    assert output.read_bytes() == b"video bytes"
    assert captured["urls"] == [PLAYLIST_URL]
    assert captured["opts"]["format"] == "best"
    assert captured["opts"]["noplaylist"] is True
    assert captured["opts"]["http_headers"] == {"Referer": EMEDIA_URL}
    assert Path(captured["opts"]["outtmpl"]).name == temporary_filename
    assert captured["opts"].get("fixup") == expected_fixup
    assert video.is_handled
    assert ctx.stats.downloaded == 1
    # Generated file size is not a substitute for bytes observed on the network.
    assert ctx.stats.transferred_bytes == 0


def test_emedia_size_limit_uses_hls_duration_and_bitrate(tmp_path, monkeypatch):
    class FakeYoutubeDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, link, download):
            assert link == PLAYLIST_URL
            assert download is False
            return {"duration": 1049, "tbr": 282.569}

        def download(self, urls):
            pytest.fail("oversized HLS video must not be downloaded")

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    ctx = make_context(
        {
            "paths.sync_directory": str(tmp_path),
            "filters.max_file_size": "1M",
        }
    )
    root = Node("", -1, "Root", None)
    video = root.add_child(
        "Clinical Lecture.mp4",
        540,
        "Emedia",
        url=PLAYLIST_URL,
    )
    assert video is not None

    downloader.download_node_tree(ctx, root)

    assert video.remote_size == round(1049 * 282.569 * 1000 / 8)
    assert video.is_handled
    assert {item.config_key for item in ctx.filtered_items} == {"filters.max_file_size"}
    assert not node_path(ctx, video).exists()


def test_packaged_cellia_intermediate_has_expected_fingerprint():
    certificate = resources.files("syncmymoodle").joinpath(
        *Path(emedia.INTERMEDIATE_CERTIFICATE).parts
    )
    der = ssl.PEM_cert_to_DER_cert(certificate.read_text(encoding="ascii"))

    assert hashlib.sha256(der).hexdigest() == (
        "5b678dc44095a52895b63b31f27227f4b36c3e347491bf2bfa691837a5fb8c79"
    )
