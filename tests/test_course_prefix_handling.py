import logging

from syncmymoodle.config import Config
from syncmymoodle.filters import format_course_name as format_course_name_impl
from syncmymoodle.node import Node


def format_course_name(handling, name):
    config = Config.from_dict({"course_prefix_handling": handling})
    return format_course_name_impl(name, config)


def test_keep_preserves_course_name():
    assert format_course_name("keep", "(VO) Analysis") == "(VO) Analysis"


def test_remove_strips_two_character_prefix():
    assert format_course_name("remove", "(VO) Analysis") == "Analysis"


def test_suffix_moves_two_character_prefix_to_end():
    assert (
        format_course_name("suffix", "(VU) Software Quality Assurance")
        == "Software Quality Assurance (VU)"
    )


def test_other_two_character_prefixes_are_supported():
    assert (
        format_course_name("suffix", "(RE) Exercise Session") == "Exercise Session (RE)"
    )


def test_non_matching_names_are_preserved():
    assert format_course_name("remove", "Analysis") == "Analysis"
    assert format_course_name("remove", "(VO)Analysis") == "(VO)Analysis"
    assert format_course_name("remove", "(V) Analysis") == "(V) Analysis"
    assert format_course_name("remove", "(ABC) Analysis") == "(ABC) Analysis"


def test_invalid_mode_preserves_course_name(caplog):
    with caplog.at_level(logging.WARNING, logger="syncmymoodle.filters"):
        assert format_course_name("invalid", "(VO) Analysis") == "(VO) Analysis"
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_page_content_url_normalization_preserves_larger_content_ids():
    root = Node("", -1, "Root", None)

    child = root.add_child(
        "Video",
        101,
        "Embedded videojs",
        url=(
            "https://moodle.rwth-aachen.de/pluginfile.php/104/"
            "mod_page/content/315/page-video.mp4"
        ),
    )
    normalized_child = root.add_child(
        "Legacy page file",
        102,
        "Linked file [application/pdf]",
        url=(
            "https://moodle.rwth-aachen.de/pluginfile.php/104/"
            "mod_page/content/3/legacy.pdf?forcedownload=1"
        ),
    )

    assert child.url == (
        "https://moodle.rwth-aachen.de/pluginfile.php/104/"
        "mod_page/content/315/page-video.mp4"
    )
    assert normalized_child.url == (
        "https://moodle.rwth-aachen.de/pluginfile.php/104/"
        "mod_page/content/legacy.pdf"
    )


def test_same_course_folder_name_without_url_gets_stable_suffixes():
    root = Node("", -1, "Root", None)
    semester = root.add_child("26ss", None, "Semester")
    semester.add_child("Software Quality Assurance", 101, "Course")
    semester.add_child("Software Quality Assurance", 102, "Course")

    root.remove_children_nameclashes()

    names = [course.name for course in semester.children]
    assert len(names) == 2
    assert len(set(names)) == 2
    assert "Software Quality Assurance" not in names
    for name in names:
        assert name.startswith("Software Quality Assurance_")


def test_same_section_name_without_url_keeps_legacy_merged_path():
    root = Node("", -1, "Root", None)
    course = root.add_child("Course", 100, "Course")
    course.add_child("Case Study", 201, "Section")
    course.add_child("Case Study", 202, "Section")

    root.remove_children_nameclashes()

    names = [section.name for section in course.children]
    assert names == ["Case Study", "Case Study"]


def test_same_name_with_different_urls_still_gets_stable_suffixes():
    root = Node("", -1, "Root", None)
    section = root.add_child("General", None, "Section")
    section.add_child("Slides", 201, "URL", url="https://example.com/slides-a")
    section.add_child("Slides", 202, "URL", url="https://example.com/slides-b")

    root.remove_children_nameclashes()

    names = [link.name for link in section.children]
    assert len(names) == 2
    assert len(set(names)) == 2
    assert "Slides" not in names
    for name in names:
        assert name.startswith("Slides_")


def test_clashing_files_without_name_clash_id_use_url_for_distinct_names():
    # Direct-link / direct-content / embedded nodes pass name_clash_id=None.
    # Two such same-named files with different URLs must still get distinct
    # names (falling back to the URL) instead of both hashing md5("None").
    root = Node("", -1, "Root", None)
    section = root.add_child("General", None, "Section")
    section.add_child(
        "slides.pdf",
        None,
        "Linked file [application/pdf]",
        url="https://a.example/slides.pdf",
        name_clash_id=None,
    )
    section.add_child(
        "slides.pdf",
        None,
        "Linked file [application/pdf]",
        url="https://b.example/slides.pdf",
        name_clash_id=None,
    )

    root.remove_children_nameclashes()

    names = [child.name for child in section.children]
    assert len(names) == 2
    assert len(set(names)) == 2
    assert "slides.pdf" not in names


def test_opencast_name_clashes_use_uploaded_filename_suffix():
    root = Node("", -1, "Root", None)
    section = root.add_child("General", None, "Section")
    section.add_child(
        "Recording",
        "episode-a",
        "Opencast",
        url="https://video.example.test/opencast/high.mp4",
    )
    section.add_child(
        "Recording",
        "episode-b",
        "Opencast",
        url="https://video.example.test/opencast/low.mp4",
    )

    root.remove_children_nameclashes()

    assert [child.name for child in section.children] == [
        "Recording_high.mp4",
        "Recording_low.mp4",
    ]
