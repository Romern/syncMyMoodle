import gzip
import json
import stat

from syncmymoodle.__main__ import Node

from .helpers import FakeSession, make_syncer


def test_sanitized_node_path_stays_inside_basedir(tmp_path):
    syncer = make_syncer({"basedir": str(tmp_path)})
    root = Node("", -1, "Root", None)
    bad_node = root.add_child("%2e%2e", 1, "Section")

    target_path = syncer.get_sanitized_node_path(bad_node)

    assert target_path == tmp_path / "_"
    assert target_path.resolve(strict=False).is_relative_to(tmp_path)


def test_private_gzip_json_roundtrip_uses_private_permissions(tmp_path):
    syncer = make_syncer()
    target = tmp_path / "session"

    syncer._write_private_gzip_json(target, {"format": "test", "value": 1})

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with target.open("rb") as handle:
        assert json.loads(gzip.decompress(handle.read()).decode("utf-8")) == {
            "format": "test",
            "value": 1,
        }
    assert syncer._read_private_gzip_json(target, "test data") == {
        "format": "test",
        "value": 1,
    }


def test_download_uses_course_cache_to_skip_unchanged_file(tmp_path):
    config = {"basedir": str(tmp_path), "updatefiles": True}
    cached_syncer = make_syncer(config)
    cached_root = Node("", -1, "Root", None)
    semester = cached_root.add_child("26ss", None, "Semester")
    course = semester.add_child("Cache Behavior", 301, "Course")
    section = course.add_child("General", 401, "Section")
    cached_file = section.add_child(
        "slides.pdf",
        "https://moodle.rwth-aachen.de/pluginfile.php/301/slides.pdf",
        "Linked file [application/pdf]",
        url="https://moodle.rwth-aachen.de/pluginfile.php/301/slides.pdf",
        timemodified=1710000300,
    )
    cached_syncer.root_node = cached_root
    cached_syncer.cache_root_node()

    download_path = cached_syncer.get_sanitized_node_path(cached_file)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(b"already downloaded")

    syncer = make_syncer(config)
    syncer.session = FakeSession()
    current_root = Node("", -1, "Root", None)
    current_semester = current_root.add_child("26ss", None, "Semester")
    current_course = current_semester.add_child("Cache Behavior", 301, "Course")
    current_section = current_course.add_child("General", 401, "Section")
    current_file = current_section.add_child(
        "slides.pdf",
        "https://moodle.rwth-aachen.de/pluginfile.php/301/slides.pdf",
        "Linked file [application/pdf]",
        url="https://moodle.rwth-aachen.de/pluginfile.php/301/slides.pdf",
        timemodified=1710000300,
    )

    assert syncer.download_file(current_file) is True
    assert syncer.session.calls == []
