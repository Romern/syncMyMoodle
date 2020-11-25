#!/usr/bin/env python3

import wx
import os
import json
import syncMyMoodle

class LoginDialog(wx.Dialog):

	def __init__(self, parent, config):
		super(LoginDialog, self).__init__(parent, title="Login")
		self.config = config
		self.InitGui()

	def InitGui(self):
		panel = wx.Panel(self)

		sizer = wx.BoxSizer(wx.VERTICAL)

		self.username = wx.TextCtrl(panel, wx.ID_ANY, self.config.get("user"))
		self.password = wx.TextCtrl(panel, wx.ID_ANY, self.config.get("password"), style=wx.TE_PASSWORD)
		loginButton = wx.Button(panel, wx.ID_ANY, "Save Login")

		loginButton.Bind(wx.EVT_BUTTON, self.OnClickSaveLogin)

		sizer.Add(self.username, 0, wx.EXPAND | wx.ALL, 10)
		sizer.Add(self.password, 0, wx.EXPAND | wx.ALL, 10)
		sizer.Add(loginButton, 0, wx.EXPAND | wx.ALL, 10)

		panel.SetSizer(sizer)

	def GetConfig(self):
		return self.config

	def OnClickSaveLogin(self, event):
		self.config.update({"user": self.username.GetValue()})
		self.config.update({"password": self.password.GetValue()})

		self.EndModal(wx.ID_OK)

class SyncFinishedDialog(wx.MessageDialog):
	def __init__(self, parent):
		super(SyncFinishedDialog, self).__init__(parent, "Moodle Sync has finished", "SYNC")

class FileTab(wx.Panel):
	def __init__(self, parent):
		super(FileTab, self).__init__(parent)
		self.InitGui()

	def InitGui(self):
		fileSizer = wx.BoxSizer(wx.HORIZONTAL)

		browserBoxSizer = self.InitFileBrowser()
		sidebarBoxSizer = self.InitSidebar()

		flags = wx.EXPAND | wx.BOTTOM | wx.LEFT

		fileSizer.Add(browserBoxSizer, 3, flags, 10)
		fileSizer.Add(sidebarBoxSizer, 1, flags | wx.RIGHT, 10)

		self.SetSizer(fileSizer)

	def InitFileBrowser(self):
		browserBox = wx.StaticBox(self, wx.ID_ANY, "")
		browserBoxSizer = wx.StaticBoxSizer(browserBox, wx.VERTICAL)

		browserStatus = wx.StaticText(browserBox, wx.ID_ANY, "Work in Progress")

		#flags = wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.BOTTOM

		browserBoxSizer.AddStretchSpacer()
		browserBoxSizer.Add(browserStatus, 0, wx.ALL | wx.ALIGN_CENTER, 10)
		browserBoxSizer.AddStretchSpacer()

		return browserBoxSizer

	def InitSidebar(self):
		sidebarBox = wx.StaticBox(self, wx.ID_ANY, "")
		sidebarBoxSizer = wx.StaticBoxSizer(sidebarBox, wx.VERTICAL)

		syncButton = wx.Button(sidebarBox, wx.ID_ANY, "SYNC")

		syncButton.Bind(wx.EVT_BUTTON, self.OnClickSync)

		# flags = wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.BOTTOM

		sidebarBoxSizer.AddStretchSpacer()
		sidebarBoxSizer.Add(syncButton, 0, wx.ALL | wx.EXPAND, 10)

		return sidebarBoxSizer

	#Just Copied Main of syncMyMoodle
	def OnClickSync(self,event):
		if not os.path.exists("config.json"):
			print("You need to copy config.json.example to config.json and adjust the settings!")
			exit(1)
		config = json.load(open("config.json"))
		if not config.get("enable_download_tracker", True) or not os.path.exists("downloaded_modules.json"):
			downloaded_modules = dict()
		else:
			downloaded_modules = json.load(open("downloaded_modules.json"))
		smm = syncMyMoodle.SyncMyMoodle(config, downloaded_modules, dryrun=config.get("dryrun"))

		print(f"Logging in...")
		smm.login()
		smm.get_moodle_wstoken()
		smm.get_userid()
		smm.sync()
		with open("config.json", "w") as file:
			file.write(json.dumps(smm.config, indent=4))
		if config.get("enable_download_tracker", True):
			with open("downloaded_modules.json", "w") as file:
				file.write(json.dumps(smm.downloaded_modules, indent=4))
		syncDialog = SyncFinishedDialog(self)
		syncDialog.ShowModal()

class SettingsTab(wx.Panel):
	def __init__(self, parent):
		super(SettingsTab, self).__init__(parent)

		self.loadSettings()
		self.InitGui()

	def loadSettings(self):
		if os.path.exists("config.json"):
			self.config = json.load(open("config.json"))
		else:
			if os.path.exists("config.json.example"):
				self.config = json.load(open("config.json.example"))

	def saveSettings(self):
		with open("config.json", "w") as file:
			file.write(json.dumps(self.config, indent=4))

	def InitGui(self):
		settingSizer = wx.BoxSizer(wx.VERTICAL)

		loginBoxSizer = self.InitLoginPanel()
		downloadBoxSizer = self.InitDownloadPanel()
		cookieBoxSizer = self.InitCookiePanel()
		downloadTrackerBoxSizer = self.InitDownloadTrackerPanel()
		semesterBoxSizer = self.InitSemesterPanel()
		saveSettings = wx.Button(self, wx.ID_ANY, "Save to Config File")

		saveSettings.Bind(wx.EVT_BUTTON, self.OnClickSave)

		settingSizer.Add(loginBoxSizer, 0, wx.EXPAND | wx.ALL, 25)
		settingSizer.Add(downloadBoxSizer, 0, wx.EXPAND | wx.ALL, 25)
		settingSizer.Add(cookieBoxSizer, 0, wx.EXPAND | wx.ALL, 25)
		settingSizer.Add(downloadTrackerBoxSizer, 0, wx.EXPAND | wx.ALL, 25)
		settingSizer.Add(semesterBoxSizer, 0, wx.EXPAND | wx.ALL, 25)
		settingSizer.Add(saveSettings, 0, wx.EXPAND | wx.ALL, 25)

		self.SetSizer(settingSizer)

	def InitLoginPanel(self):
		loginBox = wx.StaticBox(self, wx.ID_ANY, "Login")
		loginBoxSizer = wx.StaticBoxSizer(loginBox)

		loginStatus = wx.StaticText(loginBox, wx.ID_ANY, "Status: not available")
		self.username = wx.StaticText(loginBox, wx.ID_ANY, "Username")
		self.clearLogin = wx.Button(loginBox, wx.ID_ANY, "Delete")
		setLogin = wx.Button(loginBox, wx.ID_ANY, "Login")

		self.username.SetLabel(self.config.get("user"))
		if(self.config.get("user") == ""):
			self.clearLogin.Disable()

		self.clearLogin.Bind(wx.EVT_BUTTON, self.OnClickDelete)
		setLogin.Bind(wx.EVT_BUTTON, self.OnClickLogin)

		flags = wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.BOTTOM

		loginBoxSizer.Add(loginStatus, 0, wx.ALIGN_LEFT | flags, 10)
		loginBoxSizer.AddStretchSpacer()
		loginBoxSizer.Add(self.username, 0, wx.ALIGN_LEFT | flags, 10)
		loginBoxSizer.Add(self.clearLogin, 0, flags, 10)
		loginBoxSizer.Add(setLogin, 0, flags | wx.RIGHT, 10)

		return loginBoxSizer

	def InitDownloadPanel(self):
		downloadBox = wx.StaticBox(self, wx.ID_ANY, "Download")
		downloadBoxSizer = wx.StaticBoxSizer(downloadBox)

		self.downloadPathInput = wx.TextCtrl(downloadBox, wx.ID_ANY)
		searchDowloadPath = wx.Button(downloadBox, wx.ID_ANY, "Search")

		self.downloadPathInput.SetValue(self.config.get("basedir"))
		searchDowloadPath.Disable()

		self.downloadPathInput.Bind(wx.EVT_TEXT, self.OnDownloadPathChange)

		flags = wx.LEFT | wx.BOTTOM

		downloadBoxSizer.Add(self.downloadPathInput, 1, wx.ALIGN_LEFT | flags | wx.EXPAND, 10)
		downloadBoxSizer.Add(searchDowloadPath, 0, flags | wx.RIGHT, 10)

		return downloadBoxSizer

	def InitCookiePanel(self):
		cookieBox = wx.StaticBox(self, wx.ID_ANY, "Cookie")
		cookieBoxSizer = wx.StaticBoxSizer(cookieBox)

		self.cookiePathInput = wx.TextCtrl(cookieBox, wx.ID_ANY)
		searchCookiePath = wx.Button(cookieBox, wx.ID_ANY, "Search")

		self.cookiePathInput.SetValue(self.config.get("cookie_file"))
		searchCookiePath.Disable()

		self.cookiePathInput.Bind(wx.EVT_TEXT, self.OnCookiePathChange)

		flags = wx.LEFT | wx.BOTTOM

		cookieBoxSizer.Add(self.cookiePathInput, 1, wx.ALIGN_LEFT | flags | wx.EXPAND, 10)
		cookieBoxSizer.Add(searchCookiePath, 0, flags | wx.RIGHT, 10)

		return cookieBoxSizer

	def InitDownloadTrackerPanel(self):
		downloadTrackerBox = wx.StaticBox(self, wx.ID_ANY, "Download Tracker")
		downloadTrackerBoxSizer = wx.StaticBoxSizer(downloadTrackerBox)

		self.enableDownloadTracker = wx.CheckBox(downloadTrackerBox, wx.ID_ANY, "Enable")
		self.enableDownloadTracker.SetValue(self.config.get("enable_download_tracker"))

		self.enableDownloadTracker.Bind(wx.EVT_CHECKBOX, self.OnDonwloadTrackerChanged)

		flags = wx.LEFT | wx.BOTTOM

		downloadTrackerBoxSizer.Add(self.enableDownloadTracker, 0, wx.ALIGN_LEFT | flags | wx.EXPAND, 10)

		return downloadTrackerBoxSizer

	def InitSemesterPanel(self):
		semesterBox = wx.StaticBox(self, wx.ID_ANY, "Download Tracker")
		semesterBoxSizer = wx.StaticBoxSizer(semesterBox, wx.HORIZONTAL)

		self.semesterCheckboxes = []

		self.semesterCheckboxes.append(wx.CheckBox(semesterBox, wx.ID_ANY, "18ws"))
		self.semesterCheckboxes.append(wx.CheckBox(semesterBox, wx.ID_ANY, "19ss"))
		self.semesterCheckboxes.append(wx.CheckBox(semesterBox, wx.ID_ANY, "19ws"))
		self.semesterCheckboxes.append(wx.CheckBox(semesterBox, wx.ID_ANY, "20ss"))
		self.semesterCheckboxes.append(wx.CheckBox(semesterBox, wx.ID_ANY, "20ws"))

		for s in self.semesterCheckboxes:
			s.SetValue(s.GetLabel() in self.config.get("only_sync_semester"))
			s.Bind(wx.EVT_CHECKBOX, self.OnSemesterChanged)

		flags = wx.LEFT | wx.BOTTOM

		list = iter(self.semesterCheckboxes)
		semesterBoxSizer.Add(next(list), 0, wx.ALIGN_LEFT | flags | wx.EXPAND, 10)
		for s in list:
			semesterBoxSizer.Add(s, 0, flags | wx.EXPAND, 10)

		return semesterBoxSizer

	def OnClickSave(self, event):
		self.saveSettings()

	def OnClickDelete(self, event):
		self.config.update({"user": ""})
		self.config.update({"password": ""})
		self.updateGui()

	def OnClickLogin(self, event):
		login = LoginDialog(self, self.config)
		if(login.ShowModal() != wx.ID_OK):
			self.config = login.GetConfig()
		login.Destroy()
		self.updateGui()

	def OnDownloadPathChange(self, event):
		self.config.update({"basedir": self.downloadPathInput.GetValue()})

	def OnCookiePathChange(self, event):
		self.config.update({"cookie_file": self.cookiePathInput.GetValue()})

	def OnDonwloadTrackerChanged(self, event):
		self.config.update({"enable_download_tracker": self.enableDownloadTracker.GetValue()})

	def OnSemesterChanged(self, event):
		semester = []
		for s in self.semesterCheckboxes:
			if s.GetValue():
				semester.append(s.GetLabel())
		self.config.update({"only_sync_semester": semester})

	def updateGui(self):
		self.username.SetLabel(self.config.get("user"))
		self.clearLogin.Enable((self.config.get("user") != ""))
		self.Fit()

class MainFrame(wx.Frame):
	def __init__(self):
		super(MainFrame, self).__init__(None, wx.ID_ANY, "SyncMyMoodle", (30, 30), (800, 800))
		self.InitGui()

	def InitGui(self):
		self.nb = wx.Notebook(self)
		self.nb.AddPage(FileTab(self.nb), "Filebrowser")
		self.nb.AddPage(SettingsTab(self.nb), "Settings")
		return True

class SyncMyMoodleApp(wx.App):
	def __init__(self):
		super(SyncMyMoodleApp, self).__init__()
		self.InitGui()

	def InitGui(self):
		self.frame = MainFrame()
		self.frame.Show()

	def Show(self):
		self.MainLoop()

if __name__ == '__main__':
	app = SyncMyMoodleApp()
	app.Show()
