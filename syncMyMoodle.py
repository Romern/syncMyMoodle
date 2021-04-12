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
from argparse import ArgumentParser
import getpass

class Node:
	def __init__(self, name, id, type, parent, url=None, additional_info=None, is_downloaded=False):
		self.name = name
		self.id = id
		self.url = url
		self.type = type
		self.parent = parent
		self.children = []
		self.additional_info = additional_info # Currently only used for course_id in opencast
		self.is_downloaded = is_downloaded # Can also be used to exclude files from being downloaded

	def __repr__(self):
		return f'Node(name={self.name}, id={self.id}, url={self.url}, type={self.type})'

	def add_child(self, name, id, type, url=None, additional_info=None):
		temp = Node(name, id, type, self, url=url, additional_info=additional_info)
		self.children.append(temp)
		return temp

	def get_path(self):
		ret = []
		cur = self
		while cur != None:
			ret.insert(0, cur.name)
			cur = cur.parent
		return ret

class SyncMyMoodle:
	params = {
		'lang': 'en' #Titles for some pages differ
	}
	block_size = 1024
	invalid_chars = '~"#%&*:<>?/\\{|}'

	def __init__(self, config):
		self.config = config
		self.session = None
		self.session_key = None
		self.wstoken = None
		self.user_private_access_key = None
		self.user_id = None
		self.root_node = None

	# RWTH SSO Login

	def login(self):
		def get_session_key(soup):
			session_key = soup.find("a", {"data-title": "logout,moodle"})["href"]
			return re.findall("sesskey=([a-zA-Z0-9]*)", session_key)[0]

		self.session = requests.Session()
		if os.path.exists(self.config.get("cookie_file", "./session")):
			with open(self.config.get("cookie_file", "./session"), 'rb') as f:
				self.session.cookies.update(pickle.load(f))
		resp = self.session.get("https://moodle.rwth-aachen.de/")
		resp = self.session.get("https://moodle.rwth-aachen.de/auth/shibboleth/index.php")
		if resp.url == "https://moodle.rwth-aachen.de/my/":
			soup = bs(resp.text, features="html.parser")
			self.session_key = get_session_key(soup)
			with open(self.config.get("cookie_file", "./session"), 'wb') as f:
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
		with open(self.config.get("cookie_file", "./session"), 'wb') as f:
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
		if not resp.json().get("userid") or not resp.json()["userprivateaccesskey"]:
			print(f"Error while getting userid and access key: {json.dumps(resp.json(), indent=4)}")
			exit(1)
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
		return resp.json()["courses"][0] if len(resp.json()["courses"])>0 else None

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

	### Retrives the file tree for all courses

	def sync(self):
		if not self.session:
			raise Exception("You need to login() first.")
		if not self.wstoken:
			raise Exception("You need to get_moodle_wstoken() first.")
		if not self.user_id:
			raise Exception("You need to get_userid() first.")
		self.root_node = Node("", -1, "Root", None)

		### Syncing all courses
		for course in self.get_all_courses():
			course_name = course["shortname"]
			course_id = course["id"]

			if len([c for c in self.config.get("skip_courses",[]) if str(course_id) in c])>0:
				continue

			# Skip not selected courses
			if len(self.config.get("selected_courses",[]))>0 and len([c for c in self.config.get("selected_courses",[]) if str(course["id"]) in c])==0:
				continue

			semestername = course["idnumber"][:4]
			# Skip not selected semesters
			if len(self.config.get("selected_courses",[]))==0 and self.config.get("only_sync_semester",[]) and semestername not in self.config.get("only_sync_semester",[]):
				continue

			semester_node = [s for s in self.root_node.children if s.name==semestername]
			if len(semester_node) == 0:
				semester_node = self.root_node.add_child(semestername, None, "Semester")
			else:
				semester_node = semester_node[0]

			course_node = semester_node.add_child(course_name, course_id, "Course")

			print(f"Syncing {course_name}...")
			assignments = self.get_assignment(course_id)
			folders = self.get_folders_by_courses(course_id)
			for section in self.get_course(course_id):
				if isinstance(section, str):
					print(f"Error syncing section in {course_name}: {section}")
					continue
				section_node = course_node.add_child(section["name"], section["id"], "Section")

				for module in section["modules"]:
					try:
						## Get Assignments
						if module["modname"] == "assign" and config.get("used_modules",{}).get("assign",{}):
							if assignments == None:
								continue
							ass = [a for a in assignments.get("assignments") if a["cmid"] == module["id"]]
							if len(ass) == 0:
								continue
							ass = ass[0]
							assignment_id = ass["id"]
							assignment_name = module["name"]
							assignment_node = section_node.add_child(assignment_name, assignment_id, "Assignment")

							ass = ass["introattachments"] + self.get_assignment_submission_files(assignment_id)
							for c in ass:
								if c["filepath"] != "/":
									file_node = assignment_node.add_child(os.path.join(self.sanitize(c["filepath"]),self.sanitize(c["filename"])), c["fileurl"], "Assignment File", url=c["fileurl"])
								else:
									file_node = assignment_node.add_child(c["filename"], c["fileurl"], "Assignment File", url=c["fileurl"])

						## Get Resources or URLs
						if module["modname"] in ["resource", "url", "book"]:
							if module["modname"] == "resource" and not config.get("used_modules",{}).get("resource",{}):
								continue
							for c in module.get("contents",[]):
								if c["fileurl"]:
									if module["modname"] == "book":
										c["fileurl"] = c["fileurl"].replace("webservice/pluginfile.php","tokenpluginfile.php/" + self.user_private_access_key)
									self.scanForLinks(c["fileurl"], section_node, course_id, single=True)

						## Get Folders
						if module["modname"] == "folder" and config.get("used_modules",{}).get("folder",{}):
							folder_node = section_node.add_child(module["name"], module["id"], "Folder")

							# Scan intro for links
							rel_folder = [f["intro"] for f in folders if f["coursemodule"] == module["id"]]
							if rel_folder:
								self.scanForLinks(rel_folder[0], folder_node, course_id)

							for c in module.get("contents",[]):
								if c["filepath"] != "/":
									while c["filepath"][-1] == "/":
										c["filepath"] = c["filepath"][:-1]
									while c["filepath"][0] == "/":
										c["filepath"] = c["filepath"][1:]
									file_node = folder_node.add_child(os.path.join(self.sanitize(c["filepath"]),self.sanitize(c["filename"])), c["fileurl"], "Folder File", url=c["fileurl"])
								else:
									file_node = folder_node.add_child(c["filename"], c["fileurl"], "Folder File", url=c["fileurl"])

						## Get embedded videos in pages or labels
						if module["modname"] in ["page","label"] and config.get("used_modules",{}).get("url",{}):
							if module["modname"] == "page":
								self.scanForLinks(module["url"], section_node, course_id, module_title=module["name"], single=True)
							else:
								self.scanForLinks(module.get("description",""), section_node, course_id, module_title=module["name"])
					except Exception as e:
						traceback.print_exc()
						print(f"Failed to download the module {module}: {e}")

	def download_all_files(self):
		if not self.session:
			raise Exception("You need to login() first.")
		if not self.wstoken:
			raise Exception("You need to get_moodle_wstoken() first.")
		if not self.user_id:
			raise Exception("You need to get_userid() first.")
		if not self.root_node:
			raise Exception("You need to sync() first.")

		self._download_all_files(self.root_node)

	def _download_all_files(self, cur_node):
		if len(cur_node.children) == 0:
			try:
				if cur_node.url and not cur_node.is_downloaded:
					if cur_node.type == "Youtube":
						self.scanAndDownloadYouTube(cur_node)
						cur_node.is_downloaded = True
					elif cur_node.type == "Opencast":
						self.downloadOpenCastVideos(cur_node)
						cur_node.is_downloaded = True
					else:
						self.download_file(cur_node)
						cur_node.is_downloaded = True
				return
			except Exception as e:
				traceback.print_exc()
				print(f"Failed to download the module {cur_node}: {e}")

		for child in cur_node.children:
			self._download_all_files(child)

	def get_sanitized_node_path(self, node):
		path_temp = [os.path.expanduser(self.config.get("basedir","./"))] + [self.sanitize(p) for p in node.get_path()]
		return os.path.join(*path_temp)

	def sanitize(self, path):
		path = urllib.parse.unquote(path)
		path = "".join([s for s in path if s not in self.invalid_chars])
		while path and path[-1] == " ":
			path = path[:-1]
		while path and path[0] == " ":
			path = path[1:]
		return path

	# Downloads file with progress bar if it isn't already downloaded

	def download_file(self, node):
		downloadpath = self.get_sanitized_node_path(node)
		downloadpathparent = self.get_sanitized_node_path(node.parent)

		if os.path.exists(downloadpath):
			return True

		if len(node.name.split("."))>0 and node.name.split(".")[-1] in config.get("exclude_filetypes",[]):
			return True

		url = node.url.replace("webservice/pluginfile.php","tokenpluginfile.php/" + self.user_private_access_key)

		if os.path.exists(downloadpath + ".temp"):
			resume_size = os.stat(downloadpath + ".temp").st_size
			header = {'Range':f'bytes= {resume_size}-'}
		else:
			resume_size = 0
			header = dict()

		with closing(self.session.get(url, headers=header, stream=True)) as response:
			print(f"Downloading {downloadpath}")
			total_size_in_bytes = int(response.headers.get('content-length', 0)) + resume_size
			progress_bar = tqdm(total=total_size_in_bytes, unit='iB', unit_scale=True)
			if resume_size:
				progress_bar.update(resume_size)
			os.makedirs(downloadpathparent, exist_ok=True)
			with open(downloadpath + ".temp","ab") as file:
				for data in response.iter_content(self.block_size):
					progress_bar.update(len(data))
					file.write(data)
			progress_bar.close()
			os.rename(downloadpath + ".temp", downloadpath)
			return True
		return False

	# Downloads Opencast videos by using the engage API

	def downloadOpenCastVideos(self, node):
		# get engage authentication form
		course_info = [{"index":0,"methodname":"filter_opencast_get_lti_form","args":{"courseid":str(node.additional_info)}}]
		response = self.session.post(f'https://moodle.rwth-aachen.de/lib/ajax/service.php?sesskey={self.session_key}&info=filter_opencast_get_lti_form', data=json.dumps(course_info))

		# submit engage authentication info
		engageDataSoup = bs(response.json()[0]["data"], features="html.parser")
		engageData = dict([(i["name"], i["value"]) for i in engageDataSoup.findAll("input")])
		response = self.session.post('https://engage.streaming.rwth-aachen.de/lti', data=engageData)

		linkid = re.match("https://engage.streaming.rwth-aachen.de/play/([a-z0-9\-]{36})$", node.url)
		if not linkid:
			return False
		episodejson = f'https://engage.streaming.rwth-aachen.de/search/episode.json?id={linkid.groups()[0]}'
		episodejson = json.loads(self.session.get(episodejson).text)

		tracks = episodejson["search-results"]["result"]["mediapackage"]["media"]["track"]
		tracks = sorted([(t["url"],t["video"]["resolution"]) for t in tracks if t["mimetype"] == 'video/mp4' and "transport" not in t], key=(lambda x: int(x[1].split("x")[0]) ))
		# only choose mp4s provided with plain https (no transport key), and use the one with the highest resolution (sorted by width) (could also use bitrate)
		finaltrack = tracks[-1]
		node.url = finaltrack[0]
		node.name = finaltrack[0].split("/")[-1]
		return self.download_file(node)

	# Downloads Youtube-Videos using youtube_dl

	def scanAndDownloadYouTube(self, node):
		path = self.get_sanitized_node_path(node.parent)
		link = node.url
		if os.path.exists(path):
			if len([f for f in os.listdir(path) if link[-11:] in f])!=0:
				return False
		ydl_opts = {
			"outtmpl": "{}/%(title)s-%(id)s.%(ext)s".format(path),
			"ignoreerrors": True,
			"nooverwrites": True,
			"retries": 15
		}
		os.makedirs(path, exist_ok=True)
		with youtube_dl.YoutubeDL(ydl_opts) as ydl:
			ydl.download([link])
		return True

	def scanForLinks(self, text, parent_node, course_id, module_title=None, single=False):
		# A single link is supplied and the contents of it are checked
		if single:
			try:
				response = self.session.head(text)
				if "Content-Type" in response.headers and "text/html" not in response.headers["Content-Type"]:
					# non html links, assume the filename is in the path
					filename = urllib.parse.urlsplit(text).path.split("/")[-1]
					parent_node.add_child(filename, None, "Linked file", url=text)
					# instantly return as it was a direct link
					return
				elif not self.config.get("nolinks"):
					response = self.session.get(text)
					tempsoup = bs(response.text, features="html.parser")
					videojs = tempsoup.select_one(".video-js")
					if videojs:
						videojs = videojs.select_one("source")
						if videojs and videojs.get("src"):
							parsed = urllib.parse.urlparse(response.url)
							link = urllib.parse.urljoin(f'{parsed.scheme}://{parsed.netloc}/{parsed.path}', videojs["src"])
							parent_node.add_child(videojs["src"].split("/")[-1], None, "Embedded videojs", url=link)
					# further inspect the response for other links
					text = response.text
			except Exception as e:
				# Maybe the url is down?
				traceback.print_exc()
				print(f'Error while downloading url {text}: {e}')
		if self.config.get("nolinks"):
			return

		# Youtube videos
		if config.get("used_modules",{}).get("url",{}).get("youtube",{}):
			youtube_links = re.findall("https://www.youtube.com/embed/.{11}", text)
			for l in youtube_links:
				parent_node.add_child(f"Youtube: {module_title or l}", l, "Youtube", url=l)

		# OpenCast videos
		if config.get("used_modules",{}).get("url",{}).get("opencast",{}):
			opencast_links = re.findall("https://engage.streaming.rwth-aachen.de/play/[a-zA-Z0-9\-]+", text)
			for vid in opencast_links:
				parent_node.add_child(f"Opencast: {module_title or vid}", vid, "Opencast", url=vid, additional_info=course_id)

		#https://rwth-aachen.sciebo.de/s/XXX
		if config.get("used_modules",{}).get("url",{}).get("sciebo",{}):
			sciebo_links = re.findall("https://rwth-aachen.sciebo.de/s/[a-zA-Z0-9\-]+", text)
			for vid in sciebo_links:
				response = self.session.get(vid)
				soup = bs(response.text, features="html.parser")
				url = soup.find("input",{"name": "downloadURL"})
				filename = soup.find("input",{"name": "filename"})
				if url and filename:
					parent_node.add_child(filename["value"], url["value"], "Sciebo file", url=url["value"])

if __name__ == '__main__':
	try:
		import secretstorage
		has_secretstorage = True
	except:
		has_secretstorage = False

	parser = ArgumentParser(description="Synchronization client for RWTH Moodle. All optional arguments override those in config.json.")
	if has_secretstorage:
		parser.add_argument('--secretservice',action='store_true', help="Use FreeDesktop.org Secret Service as storage/retrival for username/passwords.")
	parser.add_argument('--user', default=None, help="Your RWTH SSO username")
	parser.add_argument('--password', default=None, help="Your RWTH SSO password")
	parser.add_argument('--config', default="config.json", help="The path to the config file")
	parser.add_argument('--cookiefile', default=None, help="The location of the cookie file")
	parser.add_argument('--courses', default=None, help="Only these courses will be synced (comma seperated links) (if empty, all courses will be synced)")
	parser.add_argument('--skipcourses', default=None, help="These courses will NOT be synced (comma seperated links)")
	parser.add_argument('--semester', default=None, help="Only these semesters will be synced, of the form 20ws (comma seperated) (only used if [courses] is empty, if empty all semesters will be synced)")
	parser.add_argument('--basedir', default=None, help="The base directory where all files will be synced to")
	parser.add_argument('--nolinks', action='store_true', help="Wether to not inspect links embedded in pages")
	parser.add_argument('--excludefiletypes', default=None, help="Exclude downloading files from urls with these extensions (comma seperated types, e.g. \"mp4,mkv\")")
	args = parser.parse_args()

	if os.path.exists(args.config):
		config = json.load(open(args.config))

	config["user"] = args.user or config.get("user")
	config["password"] = args.password or config.get("password")
	config["cookie_file"] = args.cookiefile or config.get("cookie_file","./session")
	config["selected_courses"] = args.courses.split(",") if args.courses else config.get("selected_courses",[])
	config["only_sync_semester"] = args.semester.split(",") if args.semester else config.get("only_sync_semester",[])
	config["basedir"] = args.basedir or config.get("basedir","./")
	config["use_secret_service"] = (args.secretservice if has_secretstorage else None) or config.get("use_secret_service")
	config["skip_courses"] = args.skipcourses.split(",") if args.skipcourses else config.get("skip_courses",[])
	config["nolinks"] = args.nolinks or config.get("no_links")
	config["used_modules"] = config.get("used_modules") or {
        "assign": True,
        "resource": True,
        "url": {
            "youtube": True,
            "opencast": True,
            "sciebo": True
        },
        "folder": True
    }
	config["exclude_filetypes"] = args.excludefiletypes.split(",") if args.excludefiletypes else config.get("exclude_filetypes")

	if has_secretstorage and config.get("use_secret_service"):
		if not args.user and not config.get("user"):
			print("You need to provide your username in the config file or through --user!")
			exit(1)
		if config.get("password"):
			print("You need to remove your password from your config file!")
			exit(1)

		connection = secretstorage.dbus_init()
		collection = secretstorage.get_default_collection(connection)
		attributes = {"application": "syncMyMoodle", "username": config["user"]}
		results = list(collection.search_items(attributes))
		if len(results) == 0:
			if args.password:
				password = args.password
			else:
				password = getpass.getpass("Password:")
			item = collection.create_item(f'{config["user"]}@rwth-aachen.de', attributes, password)
		else:
			item = results[0]
		config["password"] = item.get_secret().decode("utf-8")

	if not config.get("user") or not config.get("password"):
		print("You need to specify your username and password in the config file or as an argument!")
		exit(1)

	smm = SyncMyMoodle(config)

	print(f"Logging in...")
	smm.login()
	smm.get_moodle_wstoken()
	smm.get_userid()
	print(f"Syncing file tree...")
	smm.sync()
	print(f"Downloading files...")
	smm.download_all_files()
