from unittest import TestCase

from syncmymoodle.__main__ import Node


class NodeTest(TestCase):
	root: Node

	def setUp(self) -> None:
		super().setUp()
		self.root = Node("Root Node", -1, "Root", None)

	def test_root(self):
		assert self.root.name == "Root Node"
		assert self.root.id == -1
		assert self.root.type == "Root"
		assert self.root.parent == None

	def test_add_child(self):
		new_child = self.root.add_child("Child", 0, "semester")
		[child] = self.root.children

		self.assertIs(new_child, child)
		self.assertEqual(new_child.name, "Child")
		self.assertEqual(new_child.id, 0)
		self.assertEqual(new_child.type, "semester")

	def test_duplicate_urls(self):
		child_1 = self.root.add_child("File A", 0, "file", "example.com/a")
		child_2 = self.root.add_child("File B", 0, "file", "example.com/b")
		child_3 = self.root.add_child("File A", 0, "file", "example.com/a")

		self.assertEqual(child_1.url, "example.com/a")
		self.assertEqual(child_2.url, "example.com/b")
		self.assertIsNone(child_3)

	def test_get_path(self):
		semester = self.root.add_child("Child", 0, "semester")
		course = semester.add_child("Grandchild", 1, "course")
		assert course.get_path() == ["Root Node", "Child", "Grandchild"]

	def test_dot_export(self):
		child_a = self.root.add_child("Child A", 0, "Child")
		child_a.add_child("Grandchild", 0, "Child")
		self.root.add_child("Child B", 0, "Child")

		self.assertEqual(
			self.root.export_as_dot(),
			[
				"strict digraph {",
				'node_0 [label="Root Node"];',
				"node_0 -> node_00;",
				'node_00 [label="Child A"];',
				"node_00 -> node_000;",
				'node_000 [label="Grandchild"];',
				"node_0 -> node_01;",
				'node_01 [label="Child B"];',
				"}",
			]
		)
