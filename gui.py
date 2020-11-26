#!/usr/bin/env python3

import wx
import os
import json


class LoginDialog(wx.Dialog):

	def __init__(self, parent, config):
		super(LoginDialog, self).__init__(parent, title="Login")

		self.config = config
		self.username_field = None
		self.password_field = None

		self.init_gui()

	def init_gui(self):
		panel = wx.Panel(self)

		sizer = wx.BoxSizer(wx.VERTICAL)

		self.username_field = wx.TextCtrl(panel, wx.ID_ANY, self.config.get("user"))
		self.password_field = wx.TextCtrl(panel, wx.ID_ANY, self.config.get("password"), style=wx.TE_PASSWORD)
		login_button = wx.Button(panel, wx.ID_ANY, "Save Login")

		login_button.Bind(wx.EVT_BUTTON, self.on_click_save_login)

		sizer.Add(self.username_field, 0, wx.EXPAND | wx.ALL, 10)
		sizer.Add(self.password_field, 0, wx.EXPAND | wx.ALL, 10)
		sizer.Add(login_button, 0, wx.EXPAND | wx.ALL, 10)

		panel.SetSizer(sizer)

	def get_config(self):
		return self.config

	def on_click_save_login(self, _):
		self.config.update({"user": self.username_field.GetValue()})
		self.config.update({"password": self.password_field.GetValue()})

		self.EndModal(wx.ID_OK)


class SyncFinishedDialog(wx.MessageDialog):
	def __init__(self, parent):
		super(SyncFinishedDialog, self).__init__(parent, "Moodle Sync has finished", "SYNC")


class FileTab(wx.Panel):
	def __init__(self, parent):
		super(FileTab, self).__init__(parent)
		self.init_gui()

	def init_gui(self):
		file_sizer = wx.BoxSizer(wx.HORIZONTAL)

		browser_box_sizer = self.init_file_browser()
		sidebar_box_sizer = self.init_sidebar()

		flags = wx.EXPAND | wx.BOTTOM | wx.LEFT

		file_sizer.Add(browser_box_sizer, 3, flags, 10)
		file_sizer.Add(sidebar_box_sizer, 1, flags | wx.RIGHT, 10)

		self.SetSizer(file_sizer)

	def init_file_browser(self):
		browser_box = wx.StaticBox(self, wx.ID_ANY, "")
		browser_box_sizer = wx.StaticBoxSizer(browser_box, wx.VERTICAL)

		browser_status = wx.StaticText(browser_box, wx.ID_ANY, "Work in Progress")

		# flags = wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.BOTTOM

		browser_box_sizer.AddStretchSpacer()
		browser_box_sizer.Add(browser_status, 0, wx.ALL | wx.ALIGN_CENTER, 10)
		browser_box_sizer.AddStretchSpacer()

		return browser_box_sizer

	def init_sidebar(self):
		sidebar_box = wx.StaticBox(self, wx.ID_ANY, "")
		sidebar_box_sizer = wx.StaticBoxSizer(sidebar_box, wx.VERTICAL)

		sync_button = wx.Button(sidebar_box, wx.ID_ANY, "SYNC")

		sync_button.Bind(wx.EVT_BUTTON, self.on_click_sync)

		# flags = wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.BOTTOM

		sidebar_box_sizer.AddStretchSpacer()
		sidebar_box_sizer.Add(sync_button, 0, wx.ALL | wx.EXPAND, 10)

		return sidebar_box_sizer

	# Just Copied Main of syncMyMoodle
	def on_click_sync(self, _):
		os.system('python3 syncMyMoodle.py')
		sync_dialog = SyncFinishedDialog(self)
		sync_dialog.ShowModal()


class SettingsTab(wx.Panel):
	def __init__(self, parent, config):
		super(SettingsTab, self).__init__(parent)

		self.config = config
		self.semesterCheckboxes = []

		self.init_gui()

	def save_settings(self):
		with open("config.json", "w") as file:
			file.write(json.dumps(self.config, indent=4))

	def init_gui(self):
		setting_sizer = wx.BoxSizer(wx.VERTICAL)

		login_box_sizer = self.init_login_panel()
		download_box_sizer = self.init_download_panel()
		cookie_box_sizer = self.init_cookie_panel()
		download_tracker_box_sizer = self.init_download_tracker_panel()
		semester_box_sizer = self.init_semester_panel()
		save_settings = wx.Button(self, wx.ID_ANY, "Save to Config File")

		save_settings.Bind(wx.EVT_BUTTON, self.on_click_save)

		setting_sizer.Add(login_box_sizer, 0, wx.EXPAND | wx.ALL, 25)
		setting_sizer.Add(download_box_sizer, 0, wx.EXPAND | wx.ALL, 25)
		setting_sizer.Add(cookie_box_sizer, 0, wx.EXPAND | wx.ALL, 25)
		setting_sizer.Add(download_tracker_box_sizer, 0, wx.EXPAND | wx.ALL, 25)
		setting_sizer.Add(semester_box_sizer, 0, wx.EXPAND | wx.ALL, 25)
		setting_sizer.Add(save_settings, 0, wx.EXPAND | wx.ALL, 25)

		self.SetSizer(setting_sizer)

	def init_login_panel(self):
		login_box = wx.StaticBox(self, wx.ID_ANY, "Login")
		login_box_sizer = wx.StaticBoxSizer(login_box)

		login_status = wx.StaticText(login_box, wx.ID_ANY, "Status: not available")
		username = wx.StaticText(login_box, wx.ID_ANY, "Username", name="username")
		clear_login_btn = wx.Button(login_box, wx.ID_ANY, "Delete", name="clear_login_btn")
		set_login_btn = wx.Button(login_box, wx.ID_ANY, "Login")

		username.SetLabel(self.config.get("user"))
		if self.config.get("user") == "":
			clear_login_btn.Disable()

		clear_login_btn.Bind(wx.EVT_BUTTON, self.on_click_delete)
		set_login_btn.Bind(wx.EVT_BUTTON, self.on_click_login)

		flags = wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.BOTTOM

		login_box_sizer.Add(login_status, 0, wx.ALIGN_LEFT | flags, 10)
		login_box_sizer.AddStretchSpacer()
		login_box_sizer.Add(username, 0, wx.ALIGN_LEFT | flags, 10)
		login_box_sizer.Add(clear_login_btn, 0, flags, 10)
		login_box_sizer.Add(set_login_btn, 0, flags | wx.RIGHT, 10)

		return login_box_sizer

	def init_download_panel(self):
		return self.init_path_panel("Download", "download_path_input", "basedir", self.on_download_path_change)

	def init_cookie_panel(self):
		return self.init_path_panel("Cookie", "cookie_path_input", "cookie_file", self.on_cookie_path_change)

	def init_path_panel(self, box_name, path_name, input_name, event_handler):
		box = wx.StaticBox(self, wx.ID_ANY, box_name)
		box_sizer = wx.StaticBoxSizer(box)

		path_input = wx.TextCtrl(box, wx.ID_ANY, name=path_name)
		search_path = wx.Button(box, wx.ID_ANY, "Search")

		path_input.SetValue(self.config.get(input_name))
		search_path.Disable()

		path_input.Bind(wx.EVT_TEXT, event_handler)

		flags = wx.LEFT | wx.BOTTOM

		box_sizer.Add(path_input, 1, wx.ALIGN_LEFT | flags | wx.EXPAND, 10)
		box_sizer.Add(search_path, 0, flags | wx.RIGHT, 10)

		return box_sizer

	def init_download_tracker_panel(self):
		download_tracker_box = wx.StaticBox(self, wx.ID_ANY, "Download Tracker")
		download_tracker_box_sizer = wx.StaticBoxSizer(download_tracker_box)

		enable_download_tracker = wx.CheckBox(download_tracker_box, wx.ID_ANY, "Enable", name="enable_download_tracker")
		enable_download_tracker.SetValue(self.config.get("enable_download_tracker"))

		enable_download_tracker.Bind(wx.EVT_CHECKBOX, self.on_download_tracker_changed)

		flags = wx.LEFT | wx.BOTTOM

		download_tracker_box_sizer.Add(enable_download_tracker, 0, wx.ALIGN_LEFT | flags | wx.EXPAND, 10)

		return download_tracker_box_sizer

	def init_semester_panel(self):
		semester_box = wx.StaticBox(self, wx.ID_ANY, "Download Tracker")
		semester_box_sizer = wx.StaticBoxSizer(semester_box, wx.HORIZONTAL)

		self.semesterCheckboxes.append(wx.CheckBox(semester_box, wx.ID_ANY, "18ws"))
		self.semesterCheckboxes.append(wx.CheckBox(semester_box, wx.ID_ANY, "19ss"))
		self.semesterCheckboxes.append(wx.CheckBox(semester_box, wx.ID_ANY, "19ws"))
		self.semesterCheckboxes.append(wx.CheckBox(semester_box, wx.ID_ANY, "20ss"))
		self.semesterCheckboxes.append(wx.CheckBox(semester_box, wx.ID_ANY, "20ws"))

		for s in self.semesterCheckboxes:
			s.SetValue(s.GetLabel() in self.config.get("only_sync_semester"))
			s.Bind(wx.EVT_CHECKBOX, self.on_semester_changed)

		flags = wx.LEFT | wx.BOTTOM

		boxes_iter = iter(self.semesterCheckboxes)
		semester_box_sizer.Add(next(boxes_iter), 0, wx.ALIGN_LEFT | flags | wx.EXPAND, 10)
		for s in boxes_iter:
			semester_box_sizer.Add(s, 0, flags | wx.EXPAND, 10)

		return semester_box_sizer

	def on_click_save(self, _):
		self.save_settings()

	def on_click_delete(self, _):
		self.config.update({"user": ""})
		self.config.update({"password": ""})
		self.update_gui()

	def on_click_login(self, _):
		login = LoginDialog(self, self.config)
		if login.ShowModal() != wx.ID_OK:
			self.config = login.get_config()
		login.Destroy()
		self.update_gui()

	def on_download_path_change(self, _):
		self.config.update({"basedir": self.get_item_by_name("download_path_input").GetValue()})

	def on_cookie_path_change(self, _):
		self.config.update({"cookie_file": self.get_item_by_name("cookie_path_input").GetValue()})

	def on_download_tracker_changed(self, _):
		self.config.update({"enable_download_tracker": self.get_item_by_name("enable_download_tracker").GetValue()})

	def on_semester_changed(self, _):
		semester = []
		for s in self.semesterCheckboxes:
			if s.GetValue():
				semester.append(s.GetLabel())
		self.config.update({"only_sync_semester": semester})

	def update_gui(self):
		self.get_item_by_name("username").SetLabel(self.config.get("user"))
		self.get_item_by_name("clear_login_btn").Enable((self.config.get("user") != ""))
		self.Fit()

	# Returns the First Element with the given Name
	def get_item_by_name(self, item_name):
		def get_by_name(sizer, name):
			for item in sizer.GetChildren():
				if item.IsSizer():
					res = get_by_name(item.GetSizer(), name)
					if (res is not None) and (res.GetName() == name):
						return res
				elif item.IsWindow():
					if item.GetWindow().GetName() == name:
						return item.GetWindow()
			return None

		return get_by_name(self.GetSizer(), item_name)


class MainFrame(wx.Frame):
	def __init__(self):
		super(MainFrame, self).__init__(None, wx.ID_ANY, "SyncMyMoodle", (30, 30), (800, 800))
		self.init_gui()

	def init_gui(self):
		config = None
		if os.path.exists("config.json"):
			config = json.load(open("config.json"))
		else:
			if os.path.exists("config.json.example"):
				config = json.load(open("config.json.example"))
			else:
				print("You need config.json.example or config.json to use the GUI!")
				exit(1)

		nb = wx.Notebook(self)
		nb.AddPage(FileTab(nb), "File Browser")
		nb.AddPage(SettingsTab(nb, config), "Settings")


class SyncMyMoodleApp(wx.App):
	def __init__(self):
		super(SyncMyMoodleApp, self).__init__()
		frame = MainFrame()
		frame.Show()

	def show(self):
		self.MainLoop()


if __name__ == '__main__':
	app = SyncMyMoodleApp()
	app.show()
