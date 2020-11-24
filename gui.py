import wx

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

		# flags = wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.BOTTOM

		sidebarBoxSizer.AddStretchSpacer()
		sidebarBoxSizer.Add(syncButton, 0, wx.ALL | wx.EXPAND, 10)

		return sidebarBoxSizer

class SettingsTab(wx.Panel):
	def __init__(self, parent):
		super(SettingsTab, self).__init__(parent)
		self.InitGui()

	def InitGui(self):
		settingSizer = wx.BoxSizer(wx.VERTICAL)

		loginBoxSizer = self.InitLoginPanel()
		downloadBoxSizer = self.InitDownloadPanel()
		downloadTrackerBoxSizer = self.InitDownloadTrackerPanel()
		saveSettings = wx.Button(self, wx.ID_ANY, "Save")

		settingSizer.Add(loginBoxSizer, 0, wx.EXPAND | wx.ALL, 25)
		settingSizer.Add(downloadBoxSizer, 0, wx.EXPAND | wx.ALL, 25)
		settingSizer.Add(downloadTrackerBoxSizer, 0, wx.EXPAND | wx.ALL, 25)
		settingSizer.Add(saveSettings, 0, wx.EXPAND | wx.ALL, 25)

		self.SetSizer(settingSizer)

	def InitLoginPanel(self):
		loginBox = wx.StaticBox(self, wx.ID_ANY, "Login")
		loginBoxSizer = wx.StaticBoxSizer(loginBox)

		loginStatus = wx.StaticText(loginBox, wx.ID_ANY, "Status: not available")
		username = wx.StaticText(loginBox, wx.ID_ANY, "Username")
		clearLogin = wx.Button(loginBox, wx.ID_ANY, "Delete")
		setLogin = wx.Button(loginBox, wx.ID_ANY, "Login")

		flags = wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.BOTTOM

		loginBoxSizer.Add(loginStatus, 0, wx.ALIGN_LEFT | flags, 10)
		loginBoxSizer.AddStretchSpacer()
		loginBoxSizer.Add(username, 0, wx.ALIGN_LEFT | flags, 10)
		loginBoxSizer.Add(clearLogin, 0, flags, 10)
		loginBoxSizer.Add(setLogin, 0, flags | wx.RIGHT, 10)

		return loginBoxSizer


	def InitDownloadPanel(self):
		downloadBox = wx.StaticBox(self, wx.ID_ANY, "Download")
		downloadBoxSizer = wx.StaticBoxSizer(downloadBox)

		downloadPathInput = wx.TextCtrl(downloadBox, wx.ID_ANY, "Download Path")
		searchDowloadPath = wx.Button(downloadBox, wx.ID_ANY, "Search")

		flags = wx.LEFT | wx.BOTTOM

		downloadBoxSizer.Add(downloadPathInput, 1, wx.ALIGN_LEFT | flags | wx.EXPAND, 10)
		downloadBoxSizer.Add(searchDowloadPath, 0, flags | wx.RIGHT, 10)

		return downloadBoxSizer

	def InitDownloadTrackerPanel(self):
		downloadTrackerBox = wx.StaticBox(self, wx.ID_ANY, "Download Tracker")
		downloadTrackerBoxSizer = wx.StaticBoxSizer(downloadTrackerBox)

		enableDownloadTracker = wx.CheckBox(downloadTrackerBox, wx.ID_ANY, "Enable")

		flags = wx.LEFT | wx.BOTTOM

		downloadTrackerBoxSizer.Add(enableDownloadTracker, 0, wx.ALIGN_LEFT | flags | wx.EXPAND, 10)

		return downloadTrackerBoxSizer

class MainFrame(wx.Frame):
	def __init__(self):
		super(MainFrame, self).__init__(None, wx.ID_ANY, "SyncMyMoodle", (30,30), (800,600))
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
