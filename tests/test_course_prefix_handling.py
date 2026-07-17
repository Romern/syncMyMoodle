import pytest

from syncmymoodle import moodle_files
from syncmymoodle.config import Config
from syncmymoodle.filters import format_course_name as format_course_name_impl
from syncmymoodle.node import DownloadKind, Node, NodeKind, RemoteMarkerKind
from syncmymoodle.pathing import resolve_node_path_clashes, sanitize_path_part


def format_course_name(handling, name):
    config = Config.from_dict({"courses.prefix_handling": handling})
    return format_course_name_impl(name, config)


@pytest.mark.parametrize(
    ("handling", "name", "expected"),
    [
        ("keep", "(VO) Analysis", "(VO) Analysis"),
        ("remove", "(VO) Analysis", "Analysis"),
        (
            "suffix",
            "(VU) Software Quality Assurance",
            "Software Quality Assurance (VU)",
        ),
        ("suffix", "(RE) Exercise Session", "Exercise Session (RE)"),
    ],
)
def test_course_prefix_handling(
    handling: str,
    name: str,
    expected: str,
):
    assert format_course_name(handling, name) == expected


def test_non_matching_names_are_preserved():
    assert format_course_name("remove", "Analysis") == "Analysis"
    assert format_course_name("remove", "(VO)Analysis") == "(VO)Analysis"
    assert format_course_name("remove", "(V) Analysis") == "(V) Analysis"
    assert format_course_name("remove", "(ABC) Analysis") == "(ABC) Analysis"


def test_moodle_file_url_normalization_is_origin_aware_and_query_safe():
    current_url = (
        "https://moodle.rwth-aachen.de/pluginfile.php/104/"
        "mod_page/content/315/page-video.mp4"
    )
    assert moodle_files.canonicalize_moodle_file_url(current_url) == current_url
    assert moodle_files.canonicalize_moodle_file_url(
        "https://moodle.rwth-aachen.de/webservice/pluginfile.php/104/"
        "mod_page/content/3/legacy.pdf?forcedownload=1&sig=abc#page"
    ) == (
        "https://moodle.rwth-aachen.de/pluginfile.php/104/"
        "mod_page/content/legacy.pdf?sig=abc#page"
    )

    external_url = "https://example.test/file?forcedownload=1&sig=abc"
    assert moodle_files.canonicalize_moodle_file_url(external_url) == external_url
    child = Node("", -1, "Root", None).add_child(
        "External file",
        101,
        "File",
        url=external_url,
    )
    assert child.url == external_url


def test_structural_children_are_unconditional_and_have_typed_ancestors():
    root = Node("", -1, NodeKind.ROOT, None)
    section = root.add_child("General", 1, NodeKind.SECTION)
    first = section.add_child("First", 2, "File", url="https://example.test/file")
    second = section.add_child("Second", 3, "File", url="https://example.test/file")

    assert section.children == [first, second]
    assert second.ancestor(NodeKind.SECTION) is section
    assert second.ancestor(NodeKind.ROOT) is root
    assert second.ancestor(NodeKind.COURSE) is None


def test_explicit_download_dedup_strengthens_missing_metadata():
    parent = Node("Section", 1, NodeKind.SECTION, None)
    first = parent.add_download_child(
        "slides.pdf",
        2,
        "File",
        url="https://example.test/slides.pdf",
    )
    second = parent.add_download_child(
        "slides.pdf",
        2,
        "File",
        url="https://example.test/slides.pdf",
        etag="a" * 40,
        etag_kind=RemoteMarkerKind.CONTENT_HASH,
        remote_size=123,
    )

    assert second is first
    assert parent.children == [first]
    assert first.etag == "a" * 40
    assert first.etag_kind is RemoteMarkerKind.CONTENT_HASH
    assert first.remote_size == 123


def test_explicit_download_dedup_uses_materialized_name_and_url():
    parent = Node("Section", 1, NodeKind.SECTION, None)
    first = parent.add_download_child(
        "slides.pdf",
        2,
        "File",
        url="https://example.test/slides.pdf",
    )
    second = parent.add_download_child(
        "slides.pdf",
        999,
        "File",
        url="https://example.test/slides.pdf",
    )

    assert second is first
    assert parent.children == [first]


def test_explicit_download_dedup_rejects_conflicting_processors():
    parent = Node("Section", 1, NodeKind.SECTION, None)
    parent.add_download_child(
        "video.mp4",
        2,
        "File",
        url="https://example.test/video.mp4",
    )

    with pytest.raises(ValueError, match="conflicting download semantics"):
        parent.add_download_child(
            "video.mp4",
            2,
            "File",
            url="https://example.test/video.mp4",
            download_kind=DownloadKind.OPENCAST,
        )


@pytest.mark.parametrize("etags", [("a", "b"), ("b", "a")])
def test_explicit_download_dedup_invalidates_conflicting_metadata(etags):
    parent = Node("Section", 1, NodeKind.SECTION, None)
    first = parent.add_download_child(
        "slides.pdf",
        2,
        "File",
        url="https://example.test/slides.pdf",
        etag=etags[0] * 40,
        etag_kind=RemoteMarkerKind.CONTENT_HASH,
    )
    second = parent.add_download_child(
        "slides.pdf",
        2,
        "File",
        url="https://example.test/slides.pdf",
        etag=etags[1] * 40,
        etag_kind=RemoteMarkerKind.CONTENT_HASH,
    )

    assert second is first
    assert parent.children == [first]
    assert first.etag is None
    assert first.etag_kind is None
    assert first.has_remote_marker_conflict


def test_node_repr_does_not_expose_download_url_secrets():
    node = Node(
        "https://example.test/private.pdf?token=name-secret",
        "https://example.test/resource?id=id-secret",
        "File",
        None,
        url="https://user:password@example.test/file?token=secret",
    )

    assert "password" not in repr(node)
    assert "secret" not in repr(node)


def test_unicode_equivalent_names_share_a_normalized_path_key():
    assert sanitize_path_part("Cafe\N{COMBINING ACUTE ACCENT}.pdf") == "Café.pdf"
    root = Node("", -1, NodeKind.ROOT, None)
    section = root.add_child("General", 1, NodeKind.SECTION)
    first = section.add_child("Café.pdf", 2, "File", url="https://example.test/first")
    second = section.add_child(
        "Cafe\N{COMBINING ACUTE ACCENT}.pdf",
        3,
        "File",
        url="https://example.test/second",
    )

    resolve_node_path_clashes(root)

    assert sanitize_path_part(first.name) != sanitize_path_part(second.name)


def test_same_url_aliases_get_distinct_materialized_names():
    root = Node("", -1, NodeKind.ROOT, None)
    section = root.add_child("General", 1, NodeKind.SECTION)
    first = section.add_download_child(
        "a:b.pdf",
        2,
        "File",
        url="https://example.test/slides.pdf",
    )
    second = section.add_download_child(
        "ab.pdf",
        3,
        "File",
        url="https://example.test/slides.pdf",
    )

    resolve_node_path_clashes(root)

    assert len(section.children) == 2
    assert (
        sanitize_path_part(first.name).casefold()
        != sanitize_path_part(second.name).casefold()
    )


@pytest.mark.parametrize("course_ids", [(101, 102), (102, 101)])
def test_same_course_folder_name_without_url_gets_stable_suffixes(course_ids):
    root = Node("", -1, "Root", None)
    semester = root.add_child("26ss", None, "Semester")
    for course_id in course_ids:
        semester.add_child("Software Quality Assurance", course_id, "Course")

    resolve_node_path_clashes(root)

    assert {course.id: course.name for course in semester.children} == {
        101: "Software Quality Assurance_MzhiM2VmZj",
        102: "Software Quality Assurance_ZWM4OTU2Nj",
    }


def test_same_section_name_without_url_keeps_legacy_merged_path():
    root = Node("", -1, "Root", None)
    course = root.add_child("Course", 100, "Course")
    course.add_child("Case Study", 201, "Section")
    course.add_child("Case Study", 202, "Section")

    resolve_node_path_clashes(root)

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

    resolve_node_path_clashes(root)

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

    resolve_node_path_clashes(root)

    assert first_file.name == second_file.name == "slides.pdf"


@pytest.mark.parametrize(
    "urls",
    [
        ("https://example.com/slides-a", "https://example.com/slides-b"),
        ("https://example.com/slides-b", "https://example.com/slides-a"),
    ],
)
def test_same_name_with_different_urls_still_gets_stable_suffixes(urls):
    root = Node("", -1, "Root", None)
    section = root.add_child("General", None, "Section")
    for index, url in enumerate(urls, 201):
        section.add_child("Slides", index, "URL", url=url)

    resolve_node_path_clashes(root)

    assert {link.url: link.name for link in section.children} == {
        "https://example.com/slides-a": "Slides_NDQyZjVlND",
        "https://example.com/slides-b": "Slides_M2I1MDA3Nm",
    }


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

    resolve_node_path_clashes(root)

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
    resolve_node_path_clashes(probe)

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

    resolve_node_path_clashes(root)

    materialized_names = [
        sanitize_path_part(child.name).casefold() for child in section.children
    ]
    assert len(materialized_names) == len(set(materialized_names))


@pytest.mark.parametrize(
    ("names", "urls", "expected"),
    [
        (
            ("CON.pdf", "_CON.pdf"),
            ("https://a.example/con.pdf", "https://b.example/con.pdf"),
            {
                "https://a.example/con.pdf": "CON_YzY2ZWUxZT.pdf",
                "https://b.example/con.pdf": "_CON_NzE1MGU5NT.pdf",
            },
        ),
        (
            ("Notes.pdf", "notes.pdf"),
            ("https://a.example/notes.pdf", "https://b.example/notes.pdf"),
            {
                "https://a.example/notes.pdf": "Notes_NDZiMGU3Yz.pdf",
                "https://b.example/notes.pdf": "notes_NTk3NTA3MD.pdf",
            },
        ),
    ],
)
def test_filesystem_equivalent_names_get_stable_suffixes(names, urls, expected):
    root = Node("", -1, "Root", None)
    section = root.add_child("General", None, "Section")
    for name, url in zip(names, urls, strict=True):
        section.add_child(
            name,
            None,
            "Linked file [application/pdf]",
            url=url,
            name_clash_id=None,
        )

    resolve_node_path_clashes(root)

    assert {child.url: child.name for child in section.children} == expected


def test_opencast_name_clashes_use_uploaded_filename_suffix():
    root = Node("", -1, "Root", None)
    section = root.add_child("General", None, "Section")
    section.add_child(
        "Recording",
        "episode-a",
        "Opencast",
        url="https://video.example.test/opencast/high.mp4",
        download_kind=DownloadKind.OPENCAST,
    )
    section.add_child(
        "Recording",
        "episode-b",
        "Opencast",
        url="https://video.example.test/opencast/low.mp4",
        download_kind=DownloadKind.OPENCAST,
    )

    resolve_node_path_clashes(root)

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
        download_kind=DownloadKind.OPENCAST,
    )
    section.add_child(
        "recording",
        "episode-b",
        "Opencast",
        url="https://video.example.test/opencast/low.mp4",
        download_kind=DownloadKind.OPENCAST,
    )

    resolve_node_path_clashes(root)

    materialized_names = [
        sanitize_path_part(child.name).casefold() for child in section.children
    ]
    assert len(set(materialized_names)) == 2
