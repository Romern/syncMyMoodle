import requests, pickle
from bs4 import BeautifulSoup as bs
import os
import unicodedata
import string
import re
from contextlib import closing
import urllib.parse
import json
import helper
#from datetime import datetime

class SyncMyMoodle:
	params = {
		'lang': 'en' #Titles for some pages differ
	}
	courses = []

	def __init__(self, config):
		self.config = config
		helper.replace_spaces_by_underscores = self.config["replace_spaces_by_underscores"]
		self.session = None
		self.courses = None
		self.sections = dict()
		self.max_semester = -1

	def login(self):
		self.session = helper.login(self.config["user"], self.config["password"], self.config["cookie_file"])

	def get_courses(self, getAllCourses=False):
		if not self.session:
			raise Exception("You need to login() first.")
		response = self.session.get('https://moodle.rwth-aachen.de/my/', params=self.params)
		soup = bs(response.text, features="html.parser")
		categories = [(c["value"], c.text) for c in soup.find("select", {"name": "coc-category"}).findAll("option")]
		categories.remove(('all', 'All'))
		self.max_semester = max(categories, key=lambda item:int(item[0]))
		self.selected_categories = [c for c in categories if c == self.max_semester] if (not getAllCourses and self.config["onlyfetchcurrentsemester"]) else categories

		self.courses = [(c.find("h3").find("a")["href"], helper.clean_filename(semestername), c.get_text().replace("\n","")) for (sid, semestername) in self.selected_categories for c in soup.select(f".coc-category-{sid}")]

		if self.config["selected_courses"] and not getAllCourses:
			self.courses = [(cid, semestername, title) for (cid, semestername, title) in self.courses if cid in self.config["selected_courses"]]

	def get_sections(self):
		if not self.session:
			raise Exception("You need to login() first.")
		if not self.courses:
			raise Exception("You need to get_courses() first.")

		for cid, semestername, _ in self.courses:
			response = self.session.get(cid, params=self.params)
			soup = bs(response.text, features="html.parser")

			# needed for OpenCast
			session_key = soup.find("a", {"data-title": "logout,moodle"})["href"]
			session_key = re.findall("sesskey=([a-zA-Z0-9]*)", session_key)[0]
			course_id = re.findall("id=([0-9]*)", cid)[0]

			coursename = helper.clean_filename(soup.select_one(".page-header-headings").text)

			# Get Sections. Some courses have them on one page, others on multiple, then we need to crawl all of them
			sectionpages = re.findall("&section=[0-9]+", response.text)
			if sectionpages:
				self.sections[course_id,session_key,semestername,coursename] = []
				for s in sectionpages:
					response = self.session.get(cid+s, params=self.params)
					tempsoup = bs(response.text, features="html.parser")
					self.sections[course_id,session_key,semestername,coursename].extend(tempsoup.select_one(".topics").children)
			else:
				self.sections[course_id,session_key,semestername,coursename] = soup.select_one(".topics").children

	def sync(self):
		if not self.session:
			raise Exception("You need to login() first.")
		if not self.courses:
			raise Exception("You need to get_courses() first.")
		if not self.sections:
			raise Exception("You need to get_sections() first.")

		### Syncing all courses

		for course_id, session_key, semestername, coursename in self.sections.keys():
			print(f"Syncing {coursename}...")
			for sec in self.sections[course_id, session_key, semestername, coursename]:
				sectionname = helper.clean_filename(sec.select_one(".sectionname").get_text())
				#print(f"[{datetime.now()}] Section {sectionname}")
				mainsectionpath = os.path.join(self.config["basedir"],semestername,coursename,sectionname)

				# Categories can be multiple levels deep like folders, see https://moodle.rwth-aachen.de/course/view.php?id=7053&section=1

				label_categories = sec.findAll("li", {"class": [
					"modtype_label",
					"modtype_resource",
					"modtype_url",
					"modtype_folder",
					"modtype_assign",
					"modtype_page",
				]})

				categories = []
				category = None
				for l in label_categories:
					# Create a category for all labels if enableExperimentalCategories is set
					if "modtype_label" in l['class'] and self.config["enableExperimentalCategories"]:
						category = (helper.clean_filename(l.findAll(text=True)[-1]), [])
						categories.append(category)
					else:
						if category == None:
							category = (None, [])
							categories.append(category)
						category[1].append(l)

				## Download Opencast Videos directly embedded in section
				helper.scan_for_opencast(sec, course_id, session_key, mainsectionpath, self.session)

				for category_name, category_soups in categories:
					if category_name == None:
						sectionpath = mainsectionpath
					else:
						sectionpath = os.path.join(mainsectionpath, category_name)
					for s in category_soups:
						mod_link = s.find('a', href=True)
						if not mod_link:
							continue
						mod_link = mod_link["href"]

						## Get Resources
						if "modtype_resource" in s["class"]:
							# First check if the file is directly accessible:
							if helper.download_file(mod_link,sectionpath, self.session):
								continue
							# If no file was found, then it could be an html page with an enbedded video
							response = self.session.get(mod_link, params=self.params)
							if "Content-Type" in response.headers and "text/html" in response.headers["Content-Type"]:
								tempsoup = bs(response.text, features="html.parser")
								videojs = tempsoup.select_one(".video-js")
								if videojs:
									videojs = videojs.select_one("source")
									if videojs and videojs.get("src"):
										helper.download_file(videojs["src"],sectionpath, self.session, videojs["src"].split("/")[-1])

						## Get Resources in URLs
						if "modtype_url" in s["class"]:
							url = None
							try:
								response = self.session.head(mod_link, params=self.params)
								if "Location" in response.headers:
									url = response.headers["Location"]
									response = self.session.head(url, params=self.params)
									if "Content-Type" in response.headers and "text/html" not in response.headers["Content-Type"]:
										# Don't download html pages
										helper.download_file(url, sectionpath, self.session)
									elif "engage.streaming.rwth-aachen.de" in url:
										# Maybe its a link to an OpenCast video
										helper.downloadOpenCastVideos(url, course_id, session_key, sectionpath, self.session)
							except:
								# Maybe the url is down?
								print(f"Error while downloading url {url}")

						## Get Folders
						if "modtype_folder" in s["class"]:
							response = self.session.get(mod_link, params=self.params)
							soup = bs(response.text, features="html.parser")
							soup_results = soup.find("a", {"title": "Folder"})

							if not soup_results:
								# page has no title?
								continue

							foldername = helper.clean_filename(soup_results.text)
							filemanager = soup.select_one(".filemanager").findAll('a', href=True)
							# Scheiß auf folder, das mach ich 1 andernmal
							for file in filemanager:
								link = file["href"]
								filename = file.select_one(".fp-filename").text
								helper.download_file(link, os.path.join(sectionpath, foldername), self.session, filename)

						## Get Assignments
						if "modtype_assign" in s["class"]:
							response = self.session.get(mod_link, params=self.params)
							soup = bs(response.text, features="html.parser")
							soup_results = soup.find("a", {"title": "Assignment"})

							if not soup_results:
								# page has no title?
								continue

							foldername = helper.clean_filename(soup_results.text)
							files = soup.select(".fileuploadsubmission")
							for file in files:
								link = file.find('a', href=True)["href"]
								filename = file.text
								helper.download_file(link, os.path.join(sectionpath, foldername), self.session, filename)

						## Get embedded videos in pages
						if "modtype_page" in s["class"]:
							response = self.session.get(mod_link, params=self.params)
							soup = bs(response.text, features="html.parser")
							soup_results = soup.find("a", {"title": "Page"})

							if not soup_results:
								# page has no title?
								continue

							pagename = helper.clean_filename(soup_results.text)
							path = os.path.join(sectionpath, pagename)

							# Youtube videos
							helper.scanAndDownloadYouTube(soup, path)

							# OpenCast videos
							helper.scan_for_opencast(soup, course_id, session_key, path, self.session)

if __name__ == '__main__':
	if not os.path.exists("config.json"):
		config = {
			"selected_courses": [],
			"onlyfetchcurrentsemester": True,
			"enableExperimentalCategories": False,
			"user": "",
			"password": "",
			"basedir": "./",
			"cookie_file": "./self.session",
			"replace_spaces_by_underscores": True
		}
	else:
		config = json.load(open("config.json"))
	smm = SyncMyMoodle(config)

	print(f"Logging in...")
	smm.login()
	print(f"Getting course info...")
	smm.get_courses()
	smm.get_sections()
	smm.sync()
