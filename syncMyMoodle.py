#!/usr/bin/env python3

import requests, pickle
from bs4 import BeautifulSoup as bs
import os
import re
from contextlib import closing
import json
import base64
import youtube_dl
import traceback

import http.client
import html
import urllib.parse

from tqdm import tqdm

class SyncMyMoodle:
	params = {
		'lang': 'en' #Titles for some pages differ
	}
	block_size = 1024
	invalid_chars = '~"#%&*:<>?/\\{|}'

	def __init__(self, config, downloaded_modules=None, dryrun=False):
		self.config = config
		self.session = None
		self.courses = None
		self.session_key = None
		self.wstoken = None
		self.user_private_access_key = None
		self.user_id = None
		self.sections = dict()
		self.max_semester = -1
		self.dryrun = dryrun
		self.downloaded_modules = downloaded_modules if downloaded_modules else dict()

	# RWTH SSO Login

	def login(self):
		def get_session_key(soup):
			session_key = soup.find("a", {"data-title": "logout,moodle"})["href"]
			return re.findall("sesskey=([a-zA-Z0-9]*)", session_key)[0]

		self.session = requests.Session()
		if os.path.exists(self.config["cookie_file"]):
			with open(self.config["cookie_file"], 'rb') as f:
				self.session.cookies.update(pickle.load(f))
		resp = self.session.get("https://moodle.rwth-aachen.de/")
		resp = self.session.get("https://moodle.rwth-aachen.de/auth/shibboleth/index.php")
		if resp.url == "https://moodle.rwth-aachen.de/my/":
			soup = bs(resp.text, features="html.parser")
			self.session_key = get_session_key(soup)
			with open(self.config["cookie_file"], 'wb') as f:
				pickle.dump(self.session.cookies, f)
			return
		soup = bs(resp.text, features="html.parser")
		if soup.find("input",{"name": "RelayState"}) is None:
			data = {'j_username': self.config["user"],
					'j_password': self.config["password"],
					'_eventId_proceed': ''}
			resp2 = self.session.post(resp.url,data=data)
			soup = bs(resp2.text, features="html.parser")
		data = {"RelayState": soup.find("input",{"name": "RelayState"})["value"], 
				"SAMLResponse": soup.find("input",{"name": "SAMLResponse"})["value"]}
		resp = self.session.post("https://moodle.rwth-aachen.de/Shibboleth.sso/SAML2/POST", data=data)
		with open(self.config["cookie_file"], 'wb') as f:
			soup = bs(resp.text, features="html.parser")
			self.session_key = get_session_key(soup)
			pickle.dump(self.session.cookies, f)

	### Moodle Web Services API

	def get_moodle_wstoken(self):
		if not self.session:
			raise Exception("You need to login() first.")
		params = {
			"service": "moodle_mobile_app",
			"passport" :1,
			"urlscheme": "moodlemobile"
		}
		#response = self.session.head("https://moodle.rwth-aachen.de/admin/tool/mobile/launch.php", params=params, allow_redirects=False)
		#workaround for macos
		def getCookies(cookie_jar, domain):
			cookie_dict = cookie_jar.get_dict(domain=domain)
			found = ['%s=%s' % (name, value) for (name, value) in cookie_dict.items()]
			return ';'.join(found)
		conn = http.client.HTTPSConnection("moodle.rwth-aachen.de")
		conn.request("GET", "/admin/tool/mobile/launch.php?" + urllib.parse.urlencode(params), headers={"Cookie":  getCookies(self.session.cookies, "moodle.rwth-aachen.de")})
		response = conn.getresponse()

		# token is in an app schema, which contains the wstoken base64-encoded along with some other token
		token_base64d = response.getheader("Location").split("token=")[1]
		self.wstoken = base64.b64decode(token_base64d).decode().split(":::")[1]
		return self.wstoken

	def get_all_courses(self):
		data = {
			"requests[0][function]": "core_enrol_get_users_courses",
			"requests[0][arguments]": json.dumps({"userid": str(self.user_id), "returnusercount": "0"}),
			"requests[0][settingfilter]": 1,
			"requests[0][settingfileurl]": 1,
			"wsfunction": "tool_mobile_call_external_functions",
			"wstoken": self.wstoken
		}
		params = {
			"moodlewsrestformat": "json",
			"wsfunction": "tool_mobile_call_external_functions"
		}
		resp = self.session.post(f"https://moodle.rwth-aachen.de/webservice/rest/server.php", params=params, data=data)
		return json.loads(resp.json()["responses"][0]["data"])

	def get_course(self, course_id):
		data = {
			"courseid": int(course_id),
			"moodlewssettingfilter": True,
			"moodlewssettingfileurl": True,
			"wsfunction": "core_course_get_contents",
			"wstoken": self.wstoken,
		}
		params = {
			"moodlewsrestformat": "json",
			"wsfunction": "core_course_get_contents"
		}
		resp = self.session.post(f"https://moodle.rwth-aachen.de/webservice/rest/server.php", params=params, data=data)
		return resp.json()

	def get_userid(self):
		data = {
			"moodlewssettingfilter": True,
			"moodlewssettingfileurl": True,
			"wsfunction": "core_webservice_get_site_info",
			"wstoken": self.wstoken,
		}
		params = {
			"moodlewsrestformat": "json",
			"wsfunction": "core_webservice_get_site_info"
		}
		resp = self.session.post(f"https://moodle.rwth-aachen.de/webservice/rest/server.php", params=params, data=data)
		self.user_id = resp.json()["userid"]
		self.user_private_access_key = resp.json()["userprivateaccesskey"]
		return self.user_id, self.user_private_access_key

	def get_assignment(self, course_id):
		data = {
			"courseids[0]": int(course_id),
			"includenotenrolledcourses": 1,
			"moodlewssettingfilter": True,
			"moodlewssettingfileurl": True,
			"wsfunction": "mod_assign_get_assignments",
			"wstoken": self.wstoken
		}
		params = {
			"moodlewsrestformat": "json",
			"wsfunction": "mod_assign_get_assignments"
		}
		resp = self.session.post(f"https://moodle.rwth-aachen.de/webservice/rest/server.php", params=params, data=data)
		return resp.json()["courses"][0]

	def get_assignment_submission_files(self, assignment_id):
		data = {
			'assignid': assignment_id,
			'userid': self.user_id,
			'moodlewssettingfilter': True,
			'moodlewssettingfileurl': True,
			'wsfunction': 'mod_assign_get_submission_status',
			'wstoken': self.wstoken
		}

		params = {
			"moodlewsrestformat": "json",
			"wsfunction": "mod_assign_get_submission_status"
		}

		response = self.session.post('https://moodle.rwth-aachen.de/webservice/rest/server.php', params=params, data=data)

		files = response.json().get("lastattempt",{}).get("submission",{}).get("plugins",[])
		files += response.json().get("lastattempt",{}).get("teamsubmission",{}).get("plugins",[])
		files += response.json().get("feedback",{}).get("plugins",[])

		files = [f.get("files",[]) for p in files for f in p.get("fileareas",[]) if f["area"] in ["download","submission_files"]]
		files = [f for folder in files for f in folder]
		return files

	def get_folders_by_courses(self, course_id):
		data = {
			'courseids[0]': str(course_id),
			'moodlewssettingfilter': True,
			'moodlewssettingfileurl': True,
			'wsfunction': 'mod_folder_get_folders_by_courses',
			'wstoken': self.wstoken
		}

		params = {
			'moodlewsrestformat': 'json',
			'wsfunction': 'mod_folder_get_folders_by_courses',
		}

		response = self.session.post('https://moodle.rwth-aachen.de/webservice/rest/server.php', params=params, data=data)
		folder = response.json()["folders"]
		return folder


	### The main syncing part

	def sync(self):
		if not self.session:
			raise Exception("You need to login() first.")
		if not self.wstoken:
			raise Exception("You need to get_moodle_wstoken() first.")
		if not self.user_id:
			raise Exception("You need to get_userid() first.")

		### Syncing all courses
		for course in self.get_all_courses():
			# Skip not selected courses
			if len(self.config["selected_courses"])>0 and len([c for c in self.config["selected_courses"] if str(course["id"]) in c])==0:
				continue

			semestername = course["idnumber"][:4]
			# Skip not selected semesters
			if len(self.config["selected_courses"])==0 and self.config["only_sync_semester"] and semestername not in self.config["only_sync_semester"]:
				continue

			coursename = course["shortname"]
			print(f"Syncing {coursename}...")
			assignments = self.get_assignment(course["id"])
			folders = self.get_folders_by_courses(course["id"])
			for section in self.get_course(course["id"]):
				sectionname = section["name"]
				#print(f"[{datetime.now()}] Section {sectionname}")
				sectionpath = os.path.join(os.path.expanduser(self.config["basedir"]),self.sanitize(semestername),self.sanitize(coursename),self.sanitize(sectionname))

				for module in section["modules"]:
					try:
						## Get Assignments
						if module["modname"] == "assign":
							ass = [a for a in assignments["assignments"] if a["cmid"] == module["id"]]
							if len(ass) == 0:
								continue
							ass = ass[0]
							assignment_id = ass["id"]
							ass = ass["introattachments"] + self.get_assignment_submission_files(assignment_id)
							for c in ass:
								if c["filepath"] != "/":
									filepath = os.path.join(sectionpath, self.sanitize(module["name"]), self.sanitize(c["filepath"]))
								else:
									filepath = os.path.join(sectionpath, self.sanitize(module["name"]))
								self.download_file(c["fileurl"], filepath, c["filename"])

						## Get Resources
						if module["modname"] == "resource":
							if not module.get("contents"):
								continue

							if self.downloaded_modules != None and self.downloaded_modules.get(str(module["id"])) and int(self.downloaded_modules[str(module["id"])]) >= int(module["contentsinfo"]["lastmodified"]):
								continue

							for c in module["contents"]:
								path = sectionpath
								if len(module["contents"])>0:
									path = os.path.join(path,self.sanitize(module["name"]))
								# First check if the file is directly accessible:
								if self.download_file(c["fileurl"], sectionpath, c["filename"]):
									continue
								# If no file was found, then it could be an html page with an enbedded video
								response = self.session.get(c["fileurl"])
								if "Content-Type" in response.headers and "text/html" in response.headers["Content-Type"]:
									tempsoup = bs(response.text, features="html.parser")
									videojs = tempsoup.select_one(".video-js")
									if videojs:
										videojs = videojs.select_one("source")
										if videojs and videojs.get("src"):
											self.download_file(videojs["src"], sectionpath, videojs["src"].split("/")[-1])
									elif "engage.streaming.rwth-aachen.de" in response.text:
										engage_videos = soup.select('iframe[data-framesrc*="engage.streaming.rwth-aachen.de"]')
										for vid in engage_videos:
											self.downloadOpenCastVideos(vid.get("data-framesrc"), course["id"], path)

							if self.downloaded_modules != None and module["contentsinfo"]:
								self.downloaded_modules[str(module["id"])] = int(module["contentsinfo"]["lastmodified"])

						## Get Resources in URLs
						if module["modname"] == "url":
							if not module.get("contents"):
								continue

							if self.downloaded_modules != None and self.downloaded_modules.get(str(module["id"])):
								continue

							failed = False
							for c in module["contents"]:
								try:
									self.scanForLinks(c["fileurl"], sectionpath, course["id"], single=True)
								except Exception as e:
									# Maybe the url is down?
									traceback.print_exc()
									failed = True
									print(f'Error while downloading url {c["fileurl"]}: {e}')

							if not failed:
								if self.downloaded_modules != None:
									self.downloaded_modules[str(module["id"])] = "downloaded"

						## Get Folders
						if module["modname"] == "folder":
							if not module.get("contents"):
								continue

							if self.downloaded_modules != None and self.downloaded_modules.get(str(module["id"])) and int(self.downloaded_modules[str(module["id"])]) >= int(module["contentsinfo"]["lastmodified"]):
								continue

							rel_folder = [f["intro"] for f in folders if f["coursemodule"] == module["id"]]
							if rel_folder:
								self.scanForLinks(rel_folder[0], sectionpath, course["id"])

							for c in module["contents"]:
								if c["filepath"] != "/":
									while c["filepath"][-1] == "/":
										c["filepath"] = c["filepath"][:-1]
									while c["filepath"][0] == "/":
										c["filepath"] = c["filepath"][1:]
									filepath = os.path.join(sectionpath, self.sanitize(module["name"]), self.sanitize(c["filepath"]))
								else:
									filepath = os.path.join(sectionpath, self.sanitize(module["name"]))
								self.download_file(c["fileurl"], filepath,  c["filename"])

							if self.downloaded_modules != None and module["contentsinfo"]:
								self.downloaded_modules[str(module["id"])] = int(module["contentsinfo"]["lastmodified"])

						## Get embedded videos in pages or labels
						if module["modname"] in ["page","label"]:
							if module["modname"] == "page":
								if self.downloaded_modules != None and self.downloaded_modules.get(str(module["id"])) and int(self.downloaded_modules[str(module["id"])]) >= int(module["contentsinfo"]["lastmodified"]):
									continue
								response = self.session.get(module["url"], params=self.params)
								soup = response.text
							else:
								soup = module.get("description","")

							self.scanForLinks(soup, sectionpath, course["id"])

							if module["modname"] == "page" and self.downloaded_modules != None and "contentsinfo" in module:
								self.downloaded_modules[str(module["id"])] = int(module["contentsinfo"]["lastmodified"])

#						if module["modname"] not in ["page", "folder", "url", "resource", "assign", "label"]:
#						print(json.dumps(module, indent=4))
					except Exception as e:
						traceback.print_exc()
						print(f"Failed to download the module {module}: {e}")

	def sanitize(self, path):
		path = html.unescape(path)
		path = "".join([s for s in path if s not in self.invalid_chars])
		while path and path[-1] == " ":
			path = path[:-1]
		while path and path[0] == " ":
			path = path[1:]
		return path

	# Downloads file with progress bar if it isn't already downloaded

	def download_file(self, url, path, filename):
		filename = self.sanitize(filename)
		downloadpath = os.path.join(path,filename)

		if os.path.exists(downloadpath):
			return True

		url = url.replace("webservice/pluginfile.php","tokenpluginfile.php/" + self.user_private_access_key)

		if os.path.exists(downloadpath + ".temp"):
			resume_size = os.stat(downloadpath + ".temp").st_size
			header = {'Range':f'bytes= {resume_size}-'}
		else:
			resume_size = 0
			header = dict()

		with closing(self.session.get(url, headers=header, stream=True)) as response:
			print(f"Downloading {downloadpath}")
			if self.dryrun:
				return True
			total_size_in_bytes = int(response.headers.get('content-length', 0)) + resume_size
			progress_bar = tqdm(total=total_size_in_bytes, unit='iB', unit_scale=True)
			if resume_size:
				progress_bar.update(resume_size)
			os.makedirs(path, exist_ok=True)
			with open(downloadpath + ".temp","ab") as file:
				for data in response.iter_content(self.block_size):
					progress_bar.update(len(data))
					file.write(data)
			progress_bar.close()
			os.rename(downloadpath + ".temp", downloadpath)
			return True
		return False

	# Downloads Opencast videos by using the engage API

	def downloadOpenCastVideos(self, engageLink, courseid, path):
		# get engage authentication form
		course_info = [{"index":0,"methodname":"filter_opencast_get_lti_form","args":{"courseid":str(courseid)}}]
		response = self.session.post(f'https://moodle.rwth-aachen.de/lib/ajax/service.php?sesskey={self.session_key}&info=filter_opencast_get_lti_form', data=json.dumps(course_info))

		# submit engage authentication info
		engageDataSoup = bs(response.json()[0]["data"], features="html.parser")
		engageData = dict([(i["name"], i["value"]) for i in engageDataSoup.findAll("input")])
		response = self.session.post('https://engage.streaming.rwth-aachen.de/lti', data=engageData)

		linkid = re.match("https://engage.streaming.rwth-aachen.de/play/([a-z0-9\-]{36})$", engageLink)
		if not linkid:
			return False
		episodejson = f'https://engage.streaming.rwth-aachen.de/search/episode.json?id={linkid.groups()[0]}'
		episodejson = json.loads(self.session.get(episodejson).text)

		tracks = episodejson["search-results"]["result"]["mediapackage"]["media"]["track"]
		tracks = sorted([(t["url"],t["video"]["resolution"]) for t in tracks if t["mimetype"] == 'video/mp4' and "transport" not in t], key=(lambda x: int(x[1].split("x")[0]) ))
		# only choose mp4s provided with plain https (no transport key), and use the one with the highest resolution (sorted by width) (could also use bitrate)
		finaltrack = tracks[-1]
		return self.download_file(finaltrack[0], path, finaltrack[0].split("/")[-1])

	# Downloads Youtube-Videos using youtube_dl

	def scanAndDownloadYouTube(self, link, path):
		if os.path.exists(path):
			if len([f for f in os.listdir(path) if link[-11:] in f])!=0:
				return False
		ydl_opts = {
			"outtmpl": "{}/%(title)s-%(id)s.%(ext)s".format(path),
			"ignoreerrors": True,
			"nooverwrites": True,
			"retries": 15
		}
		if self.dryrun:
			return True
		os.makedirs(path, exist_ok=True)
		with youtube_dl.YoutubeDL(ydl_opts) as ydl:
			ydl.download([link])
		return True

	def scanForLinks(self, text, path, course_id, single=False):
		if single:
			try:
				response = self.session.head(text)
				if "Content-Type" in response.headers and "text/html" not in response.headers["Content-Type"]:
					# non html links
					self.download_file(text, path, text.split("/")[-1])
			except:
				# Maybe the url is down?
				traceback.print_exc()
				print(f'Error while downloading url {text}: {e}')


		# Youtube videos
		youtube_links = re.findall("https://www.youtube.com/embed/.{11}", text)
		for l in youtube_links:
			self.scanAndDownloadYouTube(l, path)

		# OpenCast videos
		opencast_links = re.findall("https://engage.streaming.rwth-aachen.de/play/[a-f0-9\-]+", text)
		for vid in opencast_links:
			self.downloadOpenCastVideos(vid, course_id, path)

		#https://rwth-aachen.sciebo.de/s/XXX
		sciebo_links = re.findall("https://rwth-aachen.sciebo.de/s/[a-f0-9\-]+", text)
		for vid in sciebo_links:
			response = self.session.get(vid)
			soup = bs(response.text, features="html.parser")
			url = soup.find("input",{"name": "downloadURL"})
			filename = soup.find("input",{"name": "filename"})
			if url and filename:
				self.download_file(url["value"], path, filename["value"])

if __name__ == '__main__':
	if not os.path.exists("config.json"):
		print("You need to copy config.json.example to config.json and adjust the settings!")
		exit(1)
	config = json.load(open("config.json"))
	if not config.get("enable_download_tracker",True) or not os.path.exists("downloaded_modules.json"):
		downloaded_modules = dict()
	else:
		downloaded_modules = json.load(open("downloaded_modules.json"))
	smm = SyncMyMoodle(config, downloaded_modules, dryrun=config.get("dryrun"))

	print(f"Logging in...")
	smm.login()
	smm.get_moodle_wstoken()
	smm.get_userid()
	smm.sync()
	with open("config.json","w") as file:
		file.write(json.dumps(smm.config, indent=4))
	if config.get("enable_download_tracker",True):
		with open("downloaded_modules.json","w") as file:
			file.write(json.dumps(smm.downloaded_modules, indent=4))
