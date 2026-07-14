from syncmymoodle.config import Config
from syncmymoodle.filters import format_course_name as format_course_name_impl
from syncmymoodle.node import Node
from syncmymoodle.pathing import sanitize_path_part


def format_course_name(handling, name):
    config = Config.from_dict({"courses.prefix_handling": handling})
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
        "https://moodle.rwth-aachen.de/pluginfile.php/104/mod_page/content/legacy.pdf"
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


def build_merged_section_files(
    first_url: str,
    second_url: str,
) -> tuple[Node, Node, Node, Node, Node]:
    root = Node("", -1, "Root", None)
    course = root.add_child("Course", 100, "Course")
    first_section = course.add_child("Case Study", 201, "Section")
    second_section = course.add_child("Case Study", 202, "Section")
    first_file = first_section.add_child(
        "slides.pdf",
        301,
        "Linked file [application/pdf]",
        url=first_url,
    )
    second_file = second_section.add_child(
        "slides.pdf",
        302,
        "Linked file [application/pdf]",
        url=second_url,
    )
    return root, first_section, second_section, first_file, second_file


def test_different_files_in_merged_sections_get_distinct_names():
    root, first_section, second_section, first_file, second_file = (
        build_merged_section_files(
            "https://example.test/first/slides.pdf",
            "https://example.test/second/slides.pdf",
        )
    )

    root.remove_children_nameclashes()

    assert first_section.name == second_section.name == "Case Study"
    materialized_names = {
        sanitize_path_part(first_file.name).casefold(),
        sanitize_path_part(second_file.name).casefold(),
    }
    assert len(materialized_names) == 2
    assert "slides.pdf" not in materialized_names


def test_same_file_in_merged_sections_keeps_shared_name():
    root, _, _, first_file, second_file = build_merged_section_files(
        "https://example.test/slides.pdf",
        "https://example.test/slides.pdf",
    )

    root.remove_children_nameclashes()

    assert first_file.name == second_file.name == "slides.pdf"


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


def test_generated_file_name_cannot_collide_with_existing_file():
    probe = Node("", -1, "Root", None)
    probe_section = probe.add_child("General", None, "Section")
    probe_first = probe_section.add_child(
        "slides.pdf",
        "first",
        "Linked file [application/pdf]",
        url="https://example.test/first.pdf",
    )
    probe_section.add_child(
        "slides.pdf",
        "second",
        "Linked file [application/pdf]",
        url="https://example.test/second.pdf",
    )
    probe.remove_children_nameclashes()

    root = Node("", -1, "Root", None)
    section = root.add_child("General", None, "Section")
    section.add_child(
        "slides.pdf",
        "first",
        "Linked file [application/pdf]",
        url="https://example.test/first.pdf",
    )
    section.add_child(
        "slides.pdf",
        "second",
        "Linked file [application/pdf]",
        url="https://example.test/second.pdf",
    )
    section.add_child(
        probe_first.name,
        "reserved",
        "Linked file [application/pdf]",
        url="https://example.test/reserved.pdf",
    )

    root.remove_children_nameclashes()

    materialized_names = [
        sanitize_path_part(child.name).casefold() for child in section.children
    ]
    assert len(materialized_names) == len(set(materialized_names))


def test_names_that_sanitize_to_same_windows_path_get_distinct_suffixes():
    root = Node("", -1, "Root", None)
    section = root.add_child("General", None, "Section")
    section.add_child(
        "CON.pdf",
        None,
        "Linked file [application/pdf]",
        url="https://a.example/con.pdf",
        name_clash_id=None,
    )
    section.add_child(
        "_CON.pdf",
        None,
        "Linked file [application/pdf]",
        url="https://b.example/con.pdf",
        name_clash_id=None,
    )

    root.remove_children_nameclashes()

    materialized_names = [
        sanitize_path_part(child.name).casefold() for child in section.children
    ]
    assert len(set(materialized_names)) == 2


def test_case_only_file_name_clashes_get_distinct_suffixes():
    root = Node("", -1, "Root", None)
    section = root.add_child("General", None, "Section")
    section.add_child(
        "Notes.pdf",
        None,
        "Linked file [application/pdf]",
        url="https://a.example/notes.pdf",
        name_clash_id=None,
    )
    section.add_child(
        "notes.pdf",
        None,
        "Linked file [application/pdf]",
        url="https://b.example/notes.pdf",
        name_clash_id=None,
    )

    root.remove_children_nameclashes()

    materialized_names = [
        sanitize_path_part(child.name).casefold() for child in section.children
    ]
    assert len(set(materialized_names)) == 2


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


def test_case_only_opencast_name_clashes_use_uploaded_filename_suffix():
    root = Node("", -1, "Root", None)
    section = root.add_child("General", None, "Section")
    section.add_child(
        "Recording",
        "episode-a",
        "Opencast",
        url="https://video.example.test/opencast/high.mp4",
    )
    section.add_child(
        "recording",
        "episode-b",
        "Opencast",
        url="https://video.example.test/opencast/low.mp4",
    )

    root.remove_children_nameclashes()

    materialized_names = [
        sanitize_path_part(child.name).casefold() for child in section.children
    ]
    assert len(set(materialized_names)) == 2
