#!/usr/bin/env python3

import wx
from datetime import datetime
import os
import json
from syncMyMoodle import SyncMyMoodle

# Dialogs


class LoginDialog (wx.Dialog):

	def __init__(self, parent):
		super(LoginDialog, self).__init__(parent, wx.ID_ANY, u"Login", wx.DefaultPosition, wx.Size(400, 200), wx.DEFAULT_DIALOG_STYLE)

		self.SetSizeHints(wx.DefaultSize, wx.DefaultSize)

		sizer = wx.BoxSizer(wx.VERTICAL)

		username_sizer = wx.BoxSizer(wx.HORIZONTAL)

		self.username_text = wx.StaticText(self, wx.ID_ANY, u"Username:", wx.DefaultPosition, wx.DefaultSize, 0)
		self.username_text.Wrap(-1)

		username_sizer.Add(self.username_text, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 10)

		self.username_input = wx.TextCtrl(self, wx.ID_ANY, wx.EmptyString, wx.DefaultPosition, wx.DefaultSize, 0)
		username_sizer.Add(self.username_input, 3, wx.ALIGN_CENTER_VERTICAL | wx.ALL | wx.EXPAND, 10)

		sizer.Add(username_sizer, 1, wx.EXPAND, 5)

		password_sizer = wx.BoxSizer(wx.HORIZONTAL)

		self.password_text = wx.StaticText(self, wx.ID_ANY, u"Password:", wx.DefaultPosition, wx.DefaultSize, 0)
		self.password_text.Wrap(-1)

		password_sizer.Add(self.password_text, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 10)

		self.password_input = wx.TextCtrl(self, wx.ID_ANY, wx.EmptyString, wx.DefaultPosition, wx.DefaultSize, wx.TE_PASSWORD)
		password_sizer.Add(self.password_input, 3, wx.ALIGN_CENTER_VERTICAL | wx.ALL | wx.EXPAND, 10)

		sizer.Add(password_sizer, 1, wx.EXPAND, 5)

		button_sizer = wx.StdDialogButtonSizer()
		self.button_sizerSave = wx.Button(self, wx.ID_OK)
		button_sizer.AddButton(self.button_sizerSave)
		self.button_sizerCancel = wx.Button(self, wx.ID_CANCEL)
		button_sizer.AddButton(self.button_sizerCancel)
		button_sizer.Realize()

		sizer.Add(button_sizer, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALL, 10)

		self.SetSizer(sizer)
		self.Layout()

		self.Centre(wx.BOTH)

	def __del__(self):
		pass

# Main Frame


class FileTab(wx.Panel):
	class TreeView(wx.TreeCtrl):
		def __init__(self, parent, smm):
			super(FileTab.TreeView, self).__init__(
				parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize,
				wx.TR_HAS_BUTTONS | wx.TR_HIDE_ROOT | wx.TR_MULTIPLE
			)

			self.smm = smm

		def on_resize(self, _):
			self.update_gui()

		def update_gui(self):
			if self.smm.root_node is None:
				return

			self.DeleteAllItems()

			root = self.AddRoot(self.smm.sanitize(self.smm.root_node.name))
			self.update_node(self.GetSize()[0], wx.WindowDC(self), root, self.smm.root_node, 1)

			self.ExpandAll()

			parent = self.GetParent()
			parent.Layout()

			parent = parent.GetParent()
			parent.Layout()

		def update_node(self, width, dc, tree, node, depth):
			if node.children is not None:
				for child_node in node.children:
					label = self.Ellipsize(self.smm.sanitize(child_node.name), dc, wx.ELLIPSIZE_END, width - (depth+1) * self.GetIndent())
					child_tree = self.AppendItem(tree, label)
					self.update_node(width, dc, child_tree, child_node, depth + 1)

	class DataPanel(wx.Panel):
		def __init__(self, parent, smm):
			super(FileTab.DataPanel, self).__init__(
				parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, wx.BORDER_SUNKEN | wx.TAB_TRAVERSAL
			)

			self.smm = smm

			self.SetFont(
				wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL, False, wx.EmptyString)
			)
			self.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW))
			self.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DLIGHT))

			data_panel_sizer = wx.BoxSizer(wx.VERTICAL)

			self.update_button = wx.Button(self, wx.ID_ANY, u"Update", wx.DefaultPosition, wx.DefaultSize, 0)
			self.update_button.Bind(wx.EVT_BUTTON, self.on_click_update)
			self.update_button.Enable(False)

			data_panel_sizer.Add(self.update_button, 0, wx.ALL | wx.EXPAND, 10)

			self.SetSizer(data_panel_sizer)
			self.Layout()
			data_panel_sizer.Fit(self)

		def on_click_update(self, _):
			if self.smm.session is None:
				return

			if self.smm.wstoken is None:
				self.smm.get_moodle_wstoken()
			if self.smm.user_id is None:
				self.smm.get_userid()
			self.smm.sync()

			wx.MessageDialog(self, "Updated Moodle Data", "Success", wx.OK | wx.ICON_INFORMATION).ShowModal()

			self.GetParent().update_gui()

		def update_gui(self):
			self.update_button.Enable(self.smm.session is not None)

		def startup(self):
			if (self.smm.config.get("synchronize_at_start")) and (self.smm.session is not None):
				print("Updating ...")
				# Wait for 100ms to ensure Message Dialog is closed
				wx.CallLater(100, self.on_click_update, None)

	class PresentationPanel(wx.Panel):
		def __init__(self, parent):
			super(FileTab.PresentationPanel, self).__init__(
				parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, wx.BORDER_SUNKEN | wx.TAB_TRAVERSAL
			)

			self.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL, False, wx.EmptyString))
			self.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DLIGHT))

			presentation_panel_sizer = wx.BoxSizer(wx.VERTICAL)

			self.show_new_files_button = wx.Button(self, wx.ID_ANY, u"Show new Files", wx.DefaultPosition, wx.DefaultSize, 0)
			self.show_new_files_button.Bind(wx.EVT_BUTTON, self.on_click_show_new_files)
			self.show_new_files_button.Enable(False)

			presentation_panel_sizer.Add(self.show_new_files_button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

			self.expand_all_button = wx.Button(self, wx.ID_ANY, u"Expand all", wx.DefaultPosition, wx.DefaultSize, 0)
			self.expand_all_button.Bind(wx.EVT_BUTTON, self.on_click_expand_all)
			presentation_panel_sizer.Add(self.expand_all_button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

			self.collapse_all_button = wx.Button(self, wx.ID_ANY, u"Collapse all", wx.DefaultPosition, wx.DefaultSize, 0)
			self.collapse_all_button.Bind(wx.EVT_BUTTON, self.one_click_collapse_all)
			presentation_panel_sizer.Add(self.collapse_all_button, 0, wx.BOTTOM | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

			self.SetSizer(presentation_panel_sizer)
			self.Layout()
			presentation_panel_sizer.Fit(self)

		def on_click_show_new_files(self, event):
			# TODO: implement show new files
			event.Skip()

		def on_click_expand_all(self, _):
			self.GetParent().tree_view.ExpandAll()

		def one_click_collapse_all(self, _):
			self.GetParent().tree_view.CollapseAll()

	# TODO implemt search Panel
	class SearchPanel(wx.Panel):
		def __init__(self, parent):
			super(FileTab.SearchPanel, self).__init__(
				parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, wx.BORDER_SUNKEN | wx.TAB_TRAVERSAL
			)

			self.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL, False, wx.EmptyString))
			self.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DLIGHT))

			search_panel_sizer = wx.BoxSizer(wx.VERTICAL)

			self.search_input = wx.TextCtrl(self, wx.ID_ANY, wx.EmptyString, wx.DefaultPosition, wx.DefaultSize, 0)
			self.search_input.Enable(False)

			search_panel_sizer.Add(self.search_input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

			self.search_button = wx.Button(self, wx.ID_ANY, u"Search", wx.DefaultPosition, wx.DefaultSize, 0)
			self.search_button.Bind(wx.EVT_BUTTON, self.on_click_search)
			self.search_button.Enable(False)

			search_panel_sizer.Add(self.search_button, 0, wx.BOTTOM | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

			self.SetSizer(search_panel_sizer)
			self.Layout()
			search_panel_sizer.Fit(self)

		def on_click_search(self, event):
			event.Skip()

	class SynchronizationPanel(wx.Panel):
		def __init__(self, parent, smm):
			super(FileTab.SynchronizationPanel, self).__init__(
				parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, wx.BORDER_SUNKEN | wx.TAB_TRAVERSAL
			)

			self.smm = smm

			self.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL, False, wx.EmptyString))
			self.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DLIGHT))

			synchronization_panel_sizer = wx.BoxSizer(wx.VERTICAL)

			self.add_selection_button = wx.Button(self, wx.ID_ANY, u"Add Selection", wx.DefaultPosition, wx.DefaultSize, 0)
			self.add_selection_button.Bind(wx.EVT_BUTTON, self.on_click_add_selection)
			self.add_selection_button.Enable(False)

			synchronization_panel_sizer.Add(self.add_selection_button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

			self.remove_selection_button = wx.Button(
				self, wx.ID_ANY, u"Remove Selection", wx.DefaultPosition, wx.DefaultSize, 0
			)
			self.remove_selection_button.Bind(wx.EVT_BUTTON, self.on_click_remove_selection)
			self.remove_selection_button.Enable(False)

			synchronization_panel_sizer.Add(self.remove_selection_button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

			self.open_download_folder_button = wx.Button(
				self, wx.ID_ANY, u"Open Download Folder", wx.DefaultPosition, wx.DefaultSize, 0
			)
			self.open_download_folder_button.Bind(wx.EVT_BUTTON, self.on_click_open_download_folder)
			self.open_download_folder_button.Enable(False)

			synchronization_panel_sizer.Add(self.open_download_folder_button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

			self.download_button = wx.Button(self, wx.ID_ANY, u"Download", wx.DefaultPosition, wx.DefaultSize, 0)
			self.download_button.SetFont(
				wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL, False, wx.EmptyString)
			)
			self.download_button.Bind(wx.EVT_BUTTON, self.on_click_download)
			self.download_button.Enable(False)

			synchronization_panel_sizer.Add(self.download_button, 0, wx.BOTTOM | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

			self.SetSizer(synchronization_panel_sizer)
			self.Layout()
			synchronization_panel_sizer.Fit(self)

		def on_click_add_selection(self, event):
			event.Skip()

		def on_click_remove_selection(self, event):
			event.Skip()

		def on_click_open_download_folder(self, event):
			event.Skip()

		def on_click_download(self, _):
			if self.smm.root_node is not None:
				print("Downloading ...")
				self.smm.download_all_files()
				wx.MessageDialog(self, "Downloaded Moodle Data", "Success", wx.OK | wx.ICON_INFORMATION).ShowModal()

				if self.smm.config.get("close_after_synchronization"):
					# Wait for 500ms so that the closing looks smooth
					wx.CallLater(500, wx.Exit)

		def update_gui(self):
			self.download_button.Enable(self.smm.root_node is not None)

		def startup(self):
			if (self.smm.config.get("synchronize_at_start")) and (self.smm.session is not None):
				# Wait for 100ms to ensure Message Dialog is closed
				wx.CallLater(100, self.on_click_download, None)

	def __init__(self, parent, smm):
		super(FileTab, self).__init__(parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, wx.TAB_TRAVERSAL)

		file_browser_sizer = wx.BoxSizer(wx.HORIZONTAL)

		tree_view_sizer = wx.StaticBoxSizer(wx.StaticBox(self, wx.ID_ANY, wx.EmptyString), wx.VERTICAL)
		self.tree_view = FileTab.TreeView(tree_view_sizer.GetStaticBox(), smm)
		tree_view_sizer.Add(self.tree_view, 0, wx.ALL | wx.EXPAND, 5)

		file_browser_sizer.Add(tree_view_sizer, 7, wx.EXPAND | wx.LEFT, 5)

		sidebar_sizer = wx.BoxSizer(wx.VERTICAL)

		# Data Panel

		self.data_text = wx.StaticText(self, wx.ID_ANY, u"Data", wx.DefaultPosition, wx.DefaultSize, 0)
		self.data_text.Wrap(-1)
		sidebar_sizer.Add(self.data_text, 0, wx.LEFT | wx.TOP, 10)

		self.data_panel = FileTab.DataPanel(self, smm)
		sidebar_sizer.Add(self.data_panel, 0, wx.EXPAND | wx.ALL, 5)

		# Presentation Panel

		self.presentation_text = wx.StaticText(self, wx.ID_ANY, u"Presentation", wx.DefaultPosition, wx.DefaultSize, 0)
		self.presentation_text.Wrap(-1)
		sidebar_sizer.Add(self.presentation_text, 0, wx.LEFT | wx.TOP, 10)

		self.presentation_panel = FileTab.PresentationPanel(self)
		sidebar_sizer.Add(self.presentation_panel, 0, wx.EXPAND | wx.ALL, 5)

		# Search Panel

		# self.search_text = wx.StaticText(self, wx.ID_ANY, u"Search", wx.DefaultPosition, wx.DefaultSize, 0)
		# self.search_text.Wrap(-1)
		# sidebar_sizer.Add(self.search_text, 0, wx.LEFT | wx.TOP, 10)

		# self.search_panel = FileTab.SearchPanel(self)
		# sidebar_sizer.Add(self.search_panel, 0, wx.EXPAND | wx.ALL, 5)

		# Synchronization Panel

		sidebar_sizer.Add((0, 0), 1, wx.EXPAND, 5)

		self.synchronization_text = wx.StaticText(self, wx.ID_ANY, u"Synchronization", wx.DefaultPosition, wx.DefaultSize, 0)
		self.synchronization_text.Wrap(-1)
		sidebar_sizer.Add(self.synchronization_text, 0, wx.LEFT | wx.TOP, 10)

		self.synchronization_panel = FileTab.SynchronizationPanel(self, smm)
		sidebar_sizer.Add(self.synchronization_panel, 0, wx.EXPAND | wx.ALL, 5)

		file_browser_sizer.Add(sidebar_sizer, 3, wx.EXPAND | wx.FIXED_MINSIZE | wx.LEFT | wx.RIGHT, 5)

		self.SetSizer(file_browser_sizer)
		self.Layout()
		file_browser_sizer.Fit(self)

	def update_gui(self):
		self.tree_view.update_gui()
		self.data_panel.update_gui()
		self.synchronization_panel.update_gui()

	def startup(self):
		self.data_panel.startup()

		self.update_gui()

		self.synchronization_panel.startup()


class SettingsTab(wx.Panel):
	class LoginPanel(wx.Panel):
		def __init__(self, parent, smm):
			super(SettingsTab.LoginPanel, self).__init__(
				parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, wx.BORDER_SUNKEN | wx.TAB_TRAVERSAL
			)

			self.smm = smm

			self.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DLIGHT))

			login_sizer = wx.BoxSizer(wx.HORIZONTAL)

			self.login_status_text = wx.StaticText(
				self, wx.ID_ANY, u"Status: not logged in", wx.DefaultPosition, wx.DefaultSize, 0
			)
			self.login_status_text.Wrap(-1)
			login_sizer.Add(
				self.login_status_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALIGN_LEFT | wx.BOTTOM | wx.LEFT | wx.TOP, 10
			)

			login_sizer.Add((0, 0), 1, wx.EXPAND, 5)

			self.username_text = wx.StaticText(
				self, wx.ID_ANY, self.smm.config.get("user"), wx.DefaultPosition, wx.DefaultSize, 0
			)
			self.username_text.Wrap(-1)
			login_sizer.Add(self.username_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.BOTTOM | wx.LEFT | wx.TOP, 10)

			self.delete_button = wx.Button(self, wx.ID_ANY, u"Delete", wx.DefaultPosition, wx.DefaultSize, 0)
			self.delete_button.Bind(wx.EVT_BUTTON, self.on_click_delete)
			self.delete_button.Enable(self.smm.config.get("user") != "")
			login_sizer.Add(
				self.delete_button, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALIGN_RIGHT | wx.BOTTOM | wx.LEFT | wx.TOP, 10
			)

			self.login_button = wx.Button(self, wx.ID_ANY, u"Login", wx.DefaultPosition, wx.DefaultSize, 0)
			self.login_button.Bind(wx.EVT_BUTTON, self.on_click_login)
			self.login_button.Enable(self.smm.session is None)
			login_sizer.Add(
				self.login_button, 0,
				wx.ALIGN_CENTER_VERTICAL | wx.ALIGN_RIGHT | wx.BOTTOM | wx.LEFT | wx.RIGHT | wx.TOP, 10
			)

			self.SetSizer(login_sizer)
			self.Layout()
			login_sizer.Fit(self)

		def on_click_delete(self, _):
			self.smm.config.update({"user": ""})
			self.smm.config.update({"password": ""})
			self.smm.session = None

			# Delete Cookie File, to prevent Login from last valid Data
			if os.path.exists(self.smm.config.get("cookie_file")):
				os.remove(self.smm.config.get("cookie_file"))

			self.update_gui()

		def on_click_login(self, _):
			if (self.smm.config.get("user") == "") or (self.smm.config.get("password") == ""):
				login_dialog = LoginDialog(self)
				if login_dialog.ShowModal() == wx.ID_CANCEL:
					return
				self.smm.config.update({"user": login_dialog.username_input.GetValue()})
				self.smm.config.update({"password": login_dialog.password_input.GetValue()})
				login_dialog.Destroy()

			try:
				print("Logging in ....")
				self.smm.login()
			except Exception as _:
				print("Login failed")
				wx.MessageDialog(self, "Login Failed", "Error", wx.OK | wx.ICON_ERROR).ShowModal()
				return

			wx.MessageDialog(self, "Login successful", "Success", wx.OK | wx.ICON_INFORMATION).ShowModal()
			self.update_gui()

		def update_gui(self):
			if self.smm.session is None:
				self.login_status_text.SetLabel(u"Status: not logged in")
			else:
				self.login_status_text.SetLabel(f"Status: logged in as {self.smm.config.get('user')}")
			self.username_text.SetLabel(self.smm.config.get("user"))
			self.delete_button.Enable(self.smm.config.get("user") != "")
			self.login_button.Enable(self.smm.session is None)

			self.Layout()
			self.GetSizer().Fit(self)

			parent = self.GetParent()
			parent.Layout()
			parent.GetSizer().Fit(parent)

		def startup(self):
			if self.smm.config.get("login_at_start"):
				self.on_click_login(None)

	class DownloadPanel(wx.Panel):
		def __init__(self, parent, smm):
			super(SettingsTab.DownloadPanel, self).__init__(
				parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, wx.BORDER_SUNKEN | wx.TAB_TRAVERSAL
			)

			self.smm = smm

			self.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DLIGHT))

			download_sizer = wx.BoxSizer(wx.VERTICAL)

			self.download_dir_picker = wx.DirPickerCtrl(
				self, wx.ID_ANY, self.smm.config.get("basedir"), u"Select a folder", wx.DefaultPosition,
				wx.DefaultSize, wx.DIRP_DIR_MUST_EXIST | wx.DIRP_USE_TEXTCTRL
			)
			self.download_dir_picker.Bind(wx.EVT_DIRPICKER_CHANGED, self.on_dir_changed_download)
			download_sizer.Add(self.download_dir_picker, 0, wx.BOTTOM | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

			self.SetSizer(download_sizer)
			self.Layout()
			download_sizer.Fit(self)

		def on_dir_changed_download(self, _):
			self.smm.config.update({"basedir": self.download_dir_picker.GetPath()})

	class CookiePanel(wx.Panel):
		def __init__(self, parent, smm):
			super(SettingsTab.CookiePanel, self).__init__(
				parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, wx.BORDER_SUNKEN | wx.TAB_TRAVERSAL
			)

			self.smm = smm

			self.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DLIGHT))

			cookie_sizer = wx.BoxSizer(wx.VERTICAL)

			self.cookie_dir_picker = wx.DirPickerCtrl(
				self, wx.ID_ANY, self.smm.config.get("cookie_file"), u"Select a folder", wx.DefaultPosition,
				wx.DefaultSize, wx.DIRP_DIR_MUST_EXIST | wx.DIRP_USE_TEXTCTRL
			)
			self.cookie_dir_picker.Bind(wx.EVT_DIRPICKER_CHANGED, self.on_dir_changed_cookie)
			cookie_sizer.Add(self.cookie_dir_picker, 0, wx.BOTTOM | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

			self.SetSizer(cookie_sizer)
			self.Layout()
			cookie_sizer.Fit(self)

		def on_dir_changed_cookie(self, _):
			self.smm.config.update({"cookie_file": self.cookie_dir_picker.GetPath()})

	class DownloadTrackerPanel(wx.Panel):
		def __init__(self, parent, smm):
			super(SettingsTab.DownloadTrackerPanel, self).__init__(
				parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, wx.BORDER_SUNKEN | wx.TAB_TRAVERSAL
			)

			self.smm = smm

			self.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DLIGHT))

			download_tracker_sizer = wx.BoxSizer(wx.HORIZONTAL)

			self.download_tracker_checkbox = wx.CheckBox(
				self, wx.ID_ANY, u"Enable Download Tracker", wx.DefaultPosition, wx.DefaultSize, 0
			)
			self.download_tracker_checkbox.SetValue(self.smm.config.get("enable_download_tracker"))
			self.download_tracker_checkbox.Bind(wx.EVT_CHECKBOX, self.on_check_download_tracker)
			download_tracker_sizer.Add(self.download_tracker_checkbox, 0, wx.BOTTOM | wx.LEFT | wx.TOP, 10)

			self.SetSizer(download_tracker_sizer)
			self.Layout()
			download_tracker_sizer.Fit(self)

		def on_check_download_tracker(self, _):
			self.smm.config.update({"enable_download_tracker": self.download_tracker_checkbox.GetValue()})

	class AutomationPanel(wx.Panel):
		def __init__(self, parent, smm):
			super(SettingsTab.AutomationPanel, self).__init__(
				parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, wx.BORDER_SUNKEN | wx.TAB_TRAVERSAL
			)

			self.smm = smm

			self.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DLIGHT))

			automation_sizer = wx.GridSizer(0, 2, 0, 0)

			self.automation_login_checkbox = wx.CheckBox(
				self, wx.ID_ANY, u"Login at Start", wx.DefaultPosition, wx.DefaultSize, 0
			)
			self.automation_login_checkbox.Bind(wx.EVT_CHECKBOX, self.on_check_automation_login)
			self.automation_login_checkbox.Enable(
				(self.smm.config.get("user") != "") and (self.smm.config.get("password") != "")
			)
			self.automation_login_checkbox.SetValue(self.smm.config.get("login_at_start"))

			automation_sizer.Add(self.automation_login_checkbox, 0, wx.ALL, 5)

			self.automation_synchronize_checkbox = wx.CheckBox(
				self, wx.ID_ANY, u"Synchronize at Start", wx.DefaultPosition, wx.DefaultSize, 0
			)
			self.automation_synchronize_checkbox.Bind(wx.EVT_CHECKBOX, self.on_check_automation_synchronize)
			self.automation_synchronize_checkbox.Enable(self.automation_login_checkbox.GetValue())
			self.automation_synchronize_checkbox.SetValue(self.smm.config.get("synchronize_at_start"))

			automation_sizer.Add(self.automation_synchronize_checkbox, 0, wx.ALL, 5)

			automation_sizer.Add((0, 0), 1, wx.EXPAND, 5)

			self.automation_close_checkbox = wx.CheckBox(
				self, wx.ID_ANY, u"Close after Synchronization", wx.DefaultPosition, wx.DefaultSize, 0
			)
			self.automation_close_checkbox.Bind(wx.EVT_CHECKBOX, self.on_check_automation_close)
			self.automation_close_checkbox.SetValue(self.smm.config.get("close_after_synchronization"))

			automation_sizer.Add(self.automation_close_checkbox, 0, wx.ALL, 5)

			self.SetSizer(automation_sizer)
			self.Layout()
			automation_sizer.Fit(self)

		def on_check_automation_login(self, _):
			self.smm.config.update({"login_at_start": self.automation_login_checkbox.GetValue()})
			if not self.smm.config.get("login_at_start"):
				self.smm.config.update({"synchronize_at_start": False})

				self.automation_synchronize_checkbox.Enable(False)
				self.automation_synchronize_checkbox.SetValue(False)
			else:
				self.automation_synchronize_checkbox.Enable(True)

		def on_check_automation_synchronize(self, _):
			self.smm.config.update({"synchronize_at_start": self.automation_synchronize_checkbox.GetValue()})

		def on_check_automation_close(self, _):
			self.smm.config.update({"close_after_synchronization": self.automation_close_checkbox.GetValue()})

	# TODO: implement MiscellaneousPanel
	class MiscellaneousPanel(wx.Panel):
		def __init__(self, parent, smm):
			super(SettingsTab.MiscellaneousPanel, self).__init__(
				parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, wx.BORDER_SUNKEN | wx.TAB_TRAVERSAL
			)

			self.smm = smm

			self.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DLIGHT))

			miscellaneous_sizer = wx.GridSizer(0, 2, 0, 0)

			self.overwrite_files_checkbox = wx.CheckBox(
				self, wx.ID_ANY, u"Overwrite Files", wx.DefaultPosition, wx.DefaultSize, 0
			)
			self.overwrite_files_checkbox.Bind(wx.EVT_CHECKBOX, self.on_check_overwrite_files)
			self.overwrite_files_checkbox.Enable(False)
			miscellaneous_sizer.Add(self.overwrite_files_checkbox, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)

			language_sizer = wx.BoxSizer(wx.HORIZONTAL)

			language_text = wx.StaticText(self, wx.ID_ANY, u"Language:", wx.DefaultPosition, wx.DefaultSize, 0)
			language_text.Wrap(-1)

			language_sizer.Add(language_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)

			language_sizer.Add((0, 0), 1, wx.EXPAND, 5)

			language_choice_choices = [u"Systemlanguage", u"Deutsch", u"English"]
			self.language_choice = wx.Choice(
				self, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, language_choice_choices, 0
			)
			self.language_choice.Bind(wx.EVT_CHOICE, self.on_choice_language)
			self.language_choice.SetSelection(2)
			self.language_choice.Enable(False)

			language_sizer.Add(self.language_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)

			miscellaneous_sizer.Add(language_sizer, 1, wx.EXPAND, 5)

			self.SetSizer(miscellaneous_sizer)
			self.Layout()
			miscellaneous_sizer.Fit(self)

		def on_check_overwrite_files(self, event):
			event.Skip()

		def on_choice_language(self, event):
			event.Skip()

	def __init__(self, parent, smm):
		super(SettingsTab, self).__init__(parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, wx.TAB_TRAVERSAL)

		self.smm = smm

		settings_sizer = wx.BoxSizer(wx.VERTICAL)

		# Login Box

		login_text = wx.StaticText(self, wx.ID_ANY, u"Login", wx.DefaultPosition, wx.DefaultSize, 0)
		login_text.Wrap(-1)
		settings_sizer.Add(login_text, 0, wx.LEFT | wx.TOP, 10)

		self.login_panel = SettingsTab.LoginPanel(self, self.smm)
		settings_sizer.Add(self.login_panel, 0, wx.EXPAND | wx.ALL, 10)

		# Download Box

		download_text = wx.StaticText(self, wx.ID_ANY, u"Download", wx.DefaultPosition, wx.DefaultSize, 0)
		download_text.Wrap(-1)
		settings_sizer.Add(download_text, 0, wx.LEFT | wx.TOP, 10)

		download_panel = SettingsTab.DownloadPanel(self, self.smm)
		settings_sizer.Add(download_panel, 0, wx.EXPAND | wx.ALL, 10)

		# Cookie Box

		cookie_text = wx.StaticText(self, wx.ID_ANY, u"Cookie", wx.DefaultPosition, wx.DefaultSize, 0)
		cookie_text.Wrap(-1)
		settings_sizer.Add(cookie_text, 0, wx.LEFT | wx.TOP, 10)

		cookie_panel = SettingsTab.CookiePanel(self, self.smm)
		settings_sizer.Add(cookie_panel, 0, wx.EXPAND | wx.ALL, 10)

		# Download Tracker Box

		download_tracker_text = wx.StaticText(self, wx.ID_ANY, u"Download Tracker", wx.DefaultPosition, wx.DefaultSize, 0)
		download_tracker_text.Wrap(-1)
		settings_sizer.Add(download_tracker_text, 0, wx.LEFT | wx.TOP, 10)

		download_tracker_panel = SettingsTab.DownloadTrackerPanel(self, self.smm)
		settings_sizer.Add(download_tracker_panel, 0, wx.ALL | wx.EXPAND, 10)

		# Automation Box

		automation_text = wx.StaticText(self, wx.ID_ANY, u"Automation", wx.DefaultPosition, wx.DefaultSize, 0)
		automation_text.Wrap(-1)
		settings_sizer.Add(automation_text, 0, wx.LEFT | wx.TOP, 10)

		automation_panel = SettingsTab.AutomationPanel(self, self.smm)
		settings_sizer.Add(automation_panel, 0, wx.EXPAND | wx.ALL, 10)

		# Miscellaneous Box

		# miscellaneous_text = wx.StaticText(self, wx.ID_ANY, u"Miscellaneous", wx.DefaultPosition, wx.DefaultSize, 0)
		# miscellaneous_text.Wrap(-1)
		# settings_sizer.Add(miscellaneous_text, 0, wx.LEFT | wx.TOP, 10)

		# miscellaneous_panel = SettingsTab.MiscellaneousPanel(self, self.smm)
		# settings_sizer.Add(miscellaneous_panel, 0, wx.EXPAND | wx.ALL, 10)

		self.about_button = wx.Button(self, wx.ID_ANY, u"About Sync-my-Moodle", wx.DefaultPosition, wx.DefaultSize, 0)
		self.about_button.Bind(wx.EVT_BUTTON, self.on_click_about)
		settings_sizer.Add(self.about_button, 0, wx.ALL | wx.EXPAND, 10)

		self.SetSizer(settings_sizer)
		self.Layout()
		settings_sizer.Fit(self)

	def on_click_about(self, event):
		event.Skip()

	def startup(self):
		self.login_panel.startup()


class LogTab(wx.Panel):
	class LogScrollPanel(wx.ScrolledWindow):
		def __init__(self, parent):
			super(LogTab.LogScrollPanel, self).__init__(
				parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, wx.BORDER_SIMPLE | wx.HSCROLL | wx.VSCROLL
			)

			self.debug_mode = False

			self.SetScrollRate(5, 5)
			log_panel_sizer = wx.BoxSizer(wx.VERTICAL)

			self.SetSizer(log_panel_sizer)
			self.Layout()
			log_panel_sizer.Fit(self)

		def add_log(self, log_type, message):
			if log_type == "DEBUG" and not self.debug_mode:
				return

			sizer = self.GetSizer()

			time = datetime.now().strftime('%H:%M:%S.%f')[:-3]
			log_message_text = wx.StaticText(
				self, wx.ID_ANY, f"{log_type}\t{time}   {message}", wx.DefaultPosition, wx.DefaultSize, 0
			)
			log_message_text.Wrap(-1)

			if log_type == "ERROR":
				log_message_text.SetForegroundColour(wx.Colour(233, 32, 39))
			elif log_type == "DEBUG":
				log_message_text.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))

			sizer.Add(log_message_text, 0, wx.ALL | wx.EXPAND, 5)

			self.Layout()
			sizer.Fit(self)

			parent = self.GetParent()
			parent.Layout()
			parent.GetSizer().Fit(parent)

		def set_log_mode(self, debug_mode):
			self.debug_mode = debug_mode

		def get_complete_log(self):
			log = ""
			for item in self.GetChildren():
				log += item.GetLabel() + "\n"
			return log

	def __init__(self, parent):
		super(LogTab, self).__init__(parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, wx.TAB_TRAVERSAL)

		log_sizer = wx.BoxSizer(wx.HORIZONTAL)

		self.log_scroll_panel = LogTab.LogScrollPanel(self)
		log_sizer.Add(self.log_scroll_panel, 5, wx.EXPAND | wx.ALL, 10)

		log_sidebar_sizer = wx.BoxSizer(wx.VERTICAL)

		log_sidebar_sizer.Add((0, 0), 1, wx.EXPAND, 5)

		self.log_choice_text = wx.StaticText(self, wx.ID_ANY, u"Log Mode:", wx.DefaultPosition, wx.DefaultSize, 0)
		self.log_choice_text.Wrap(-1)
		log_sidebar_sizer.Add(self.log_choice_text, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

		log_choice_choices = [u"Standard", u"Extended"]
		self.log_choice = wx.Choice(self, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, log_choice_choices, 0)
		self.log_choice.SetSelection(0)
		self.log_choice.Bind(wx.EVT_CHOICE, self.on_choice_log_mode)
		log_sidebar_sizer.Add(self.log_choice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

		copy_log_button = wx.Button(self, wx.ID_ANY, u"Copy", wx.DefaultPosition, wx.DefaultSize, 0)
		copy_log_button.Bind(wx.EVT_BUTTON, self.on_click_log_copy)
		log_sidebar_sizer.Add(copy_log_button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

		save_log_button = wx.Button(self, wx.ID_ANY, u"Save", wx.DefaultPosition, wx.DefaultSize, 0)
		save_log_button.Bind(wx.EVT_BUTTON, self.on_click_log_save)
		save_log_button.Disable()
		log_sidebar_sizer.Add(save_log_button, 0, wx.BOTTOM | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

		log_sizer.Add(log_sidebar_sizer, 1, wx.EXPAND, 5)

		self.SetSizer(log_sizer)
		self.Layout()
		log_sizer.Fit(self)

		self.log_scroll_panel.add_log("INFO", "Log is currently not implemented")

	def on_choice_log_mode(self, _):
		selected = self.log_choice.GetSelection()
		self.log_scroll_panel.set_log_mode(selected == 1)
		self.log_scroll_panel.add_log("INFO", f"Set Log Mode to {self.log_choice.GetString(selected)}")

	def on_click_log_copy(self, _):
		if wx.TheClipboard.Open():
			wx.TheClipboard.AddData(wx.TextDataObject(self.log_scroll_panel.get_complete_log()))
			wx.TheClipboard.Close()

	# Demo Log
	def on_click_log_save(self, event):
		# TODO Save Log
		event.Skip()


class ManualTab(wx.Panel):
	def __init__(self, parent, manual):
		super(ManualTab, self).__init__(parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, wx.TAB_TRAVERSAL)

		self.manual = manual

		manual_sizer = wx.BoxSizer(wx.VERTICAL)

		self.SetSizer(manual_sizer)
		self.Layout()
		manual_sizer.Fit(self)

	def add_text(self, text, bold, italic, underlined):
		text = wx.StaticText(self, wx.ID_ANY, text, wx.DefaultPosition, wx.DefaultSize, 0)

		style = wx.FONTSTYLE_ITALIC if italic else wx.FONTSTYLE_NORMAL
		weight = wx.FONTWEIGHT_BOLD if bold else wx.FONTWEIGHT_NORMAL

		text.SetFont(
			wx.Font(
				wx.NORMAL_FONT.GetPointSize(), wx.FONTFAMILY_DEFAULT, style, weight, underlined, wx.EmptyString
			)
		)

		text.Wrap(self.GetParent().GetSize()[0])

		self.GetSizer().Add(text, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

	def startup(self):
		for text in self.manual:
			self.add_text(text.get("text"), text.get("bold"), text.get("italic"), text.get("underlined"))

		self.Layout()
		self.GetSizer().Fit(self)


class MainFrame(wx.Frame):

	def __init__(self, parent):
		super(MainFrame, self).__init__(
			parent, wx.ID_ANY, u"SyncMyMoodle", wx.DefaultPosition, wx.Size(700, 750),
			wx.DEFAULT_FRAME_STYLE | wx.TAB_TRAVERSAL
		)

		self.SetSizeHints(wx.Size(700, 750), wx.DefaultSize)

		config = self.load_config()
		self.smm = SyncMyMoodle(config)

		main_sizer = wx.BoxSizer(wx.VERTICAL)

		notebook = wx.Notebook(self, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, 0)

		self.file_browser_tab = FileTab(notebook, self.smm)
		notebook.AddPage(self.file_browser_tab, u"File Browser", True)

		self.settings_tab = SettingsTab(notebook, self.smm)
		notebook.AddPage(self.settings_tab, u"Settings", False)

		log_tab = LogTab(notebook)
		notebook.AddPage(log_tab, u"Log", False)

		manual = self.load_manual()
		self.manual_tab = ManualTab(notebook, manual)
		notebook.AddPage(self.manual_tab, u"Manual", False)

		notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_tab_changed)

		main_sizer.Add(notebook, 1, wx.EXPAND | wx.ALL, 5)

		self.SetSizer(main_sizer)
		self.Layout()

		self.Centre(wx.BOTH)

	def __del__(self):
		self.save_config(self.smm.config)

	def load_config(self):
		config = None
		if os.path.exists("config.json"):
			config = json.load(open("config.json"))
		else:
			if os.path.exists("config.json.example"):
				config = json.load(open("config.json.example"))
			else:
				print("You need config.json.example or config.json to use the GUI!")
				exit(1)
		return config

	def save_config(self, config):
		with open("config.json", "w") as file:
			file.write(json.dumps(config, indent=4))

	def load_manual(self):
		manual = json.loads("[]")
		if os.path.exists("manual.json"):
			manual = json.load(open("manual.json"))
			pass
		return manual

	def on_tab_changed(self, event):
		if event.GetSelection() == 0:
			self.file_browser_tab.update_gui()

	def startup(self):
		self.manual_tab.startup()
		self.settings_tab.startup()
		self.file_browser_tab.startup()


class SyncMyMoodleApp(wx.App):
	def __init__(self):
		super(SyncMyMoodleApp, self).__init__()
		frame = MainFrame(None)
		frame.Show()
		# Wait 100ms before running startup function to ensure Window is visible
		wx.CallLater(100, frame.startup)


if __name__ == '__main__':
	app = SyncMyMoodleApp()
	app.MainLoop()
