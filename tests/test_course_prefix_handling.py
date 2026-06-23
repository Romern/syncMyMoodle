import unittest

from syncmymoodle.__main__ import Node, SyncMyMoodle


class CoursePrefixHandlingTest(unittest.TestCase):
    def format_course_name(self, handling, name):
        smm = SyncMyMoodle({"course_prefix_handling": handling})
        return smm._format_course_name(name)

    def test_keep_preserves_course_name(self):
        self.assertEqual(
            self.format_course_name("keep", "(VO) Analysis"),
            "(VO) Analysis",
        )

    def test_remove_strips_two_character_prefix(self):
        self.assertEqual(
            self.format_course_name("remove", "(VO) Analysis"),
            "Analysis",
        )

    def test_suffix_moves_two_character_prefix_to_end(self):
        self.assertEqual(
            self.format_course_name("suffix", "(VU) Software Quality Assurance"),
            "Software Quality Assurance (VU)",
        )

    def test_other_two_character_prefixes_are_supported(self):
        self.assertEqual(
            self.format_course_name("suffix", "(RE) Exercise Session"),
            "Exercise Session (RE)",
        )

    def test_non_matching_names_are_preserved(self):
        self.assertEqual(self.format_course_name("remove", "Analysis"), "Analysis")
        self.assertEqual(
            self.format_course_name("remove", "(VO)Analysis"), "(VO)Analysis"
        )
        self.assertEqual(
            self.format_course_name("remove", "(V) Analysis"), "(V) Analysis"
        )
        self.assertEqual(
            self.format_course_name("remove", "(ABC) Analysis"),
            "(ABC) Analysis",
        )

    def test_invalid_mode_preserves_course_name(self):
        with self.assertLogs("syncmymoodle.__main__", level="WARNING"):
            self.assertEqual(
                self.format_course_name("invalid", "(VO) Analysis"),
                "(VO) Analysis",
            )


class CourseNameClashTest(unittest.TestCase):
    def test_same_course_folder_name_without_url_gets_stable_suffixes(self):
        root = Node("", -1, "Root", None)
        semester = root.add_child("26ss", None, "Semester")
        semester.add_child("Software Quality Assurance", 101, "Course")
        semester.add_child("Software Quality Assurance", 102, "Course")

        root.remove_children_nameclashes()

        names = [course.name for course in semester.children]
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names)), 2)
        self.assertNotIn("Software Quality Assurance", names)
        for name in names:
            self.assertTrue(name.startswith("Software Quality Assurance_"))

    def test_same_name_with_different_urls_still_gets_stable_suffixes(self):
        root = Node("", -1, "Root", None)
        section = root.add_child("General", None, "Section")
        section.add_child("Slides", 201, "URL", url="https://example.com/slides-a")
        section.add_child("Slides", 202, "URL", url="https://example.com/slides-b")

        root.remove_children_nameclashes()

        names = [link.name for link in section.children]
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names)), 2)
        self.assertNotIn("Slides", names)
        for name in names:
            self.assertTrue(name.startswith("Slides_"))


if __name__ == "__main__":
    unittest.main()
