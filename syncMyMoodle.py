import requests, pickle
from bs4 import BeautifulSoup as bs
import os
import unicodedata
import string
import re
from contextlib import closing
import urllib.parse
import youtube_dl
import json
from config import *
### Config
params = {
	'lang': 'en' #Titles for some pages differ
}

valid_filename_chars = "-_.() %s%süöäßÖÄÜ" % (string.ascii_letters, string.digits)
char_limit = 255


basedir = "./"
cookie_file = "./session"
if not os.path.exists(basedir):
	os.makedirs(basedir)

### Helper functions

def login(username, password):
	s = requests.Session()
	if os.path.exists(cookie_file):
		with open(cookie_file, 'rb') as f:
			s.cookies.update(pickle.load(f))
	resp = s.get("https://moodle.rwth-aachen.de/")
	resp = s.get("https://moodle.rwth-aachen.de/auth/shibboleth/index.php")
	if resp.url == "https://moodle.rwth-aachen.de/my/":
		return s
	soup = bs(resp.text, features="html.parser")
	if soup.find("input",{"name": "RelayState"}) is None:
		data = {'j_username': username,
				'j_password': password,
				'_eventId_proceed': ''}
		resp2 = s.post(resp.url,data=data)
		soup = bs(resp2.text, features="html.parser")		
	data = {"RelayState": soup.find("input",{"name": "RelayState"})["value"], 
			"SAMLResponse": soup.find("input",{"name": "SAMLResponse"})["value"]}
	resp = s.post("https://moodle.rwth-aachen.de/Shibboleth.sso/SAML2/POST", data=data)
	with open(cookie_file, 'wb') as f:
		pickle.dump(s.cookies, f)
	return s

def clean_filename(filename, whitelist=valid_filename_chars, replace=' '):
	filename = urllib.parse.unquote(filename)
	for r in replace:
		filename = filename.replace(r,'_')
	cleaned_filename = unicodedata.normalize('NFKD', filename)
	cleaned_filename = ''.join(c for c in cleaned_filename if c in whitelist)
	return cleaned_filename[:char_limit]

def download_file(url, path, session, filename=None, content=None):
	if filename != None and os.path.exists(path + clean_filename(filename)):
		return True
	with closing(session.get(url, stream=True)) as response:
		if filename == None:
			if "Content-Disposition" in response.headers:
				filename = re.findall("filename=\"(.*)\"", response.headers["Content-Disposition"])[0]
			elif "Location" in response.headers:
				filename = response.headers["Location"].split("/")[-1]
			elif "Content-Type" in response.headers and "text/html" not in response.headers["Content-Type"]: #if not html page, get the filename from the url
				filename = urllib.parse.urlsplit(url).path.split("/")[-1]
			else:
				print(f"Could not get filename from {url} ...")
				return False
		downloadpath = path + clean_filename(filename)
		if not os.path.exists(downloadpath):
			if not os.path.exists(path):
				os.makedirs(path)
			with open(downloadpath,"wb") as file:
				file.write(response.content)
			print(f"Downloaded {downloadpath}")
			return True
	return False	

### Main program

session = login(user, password)
response = session.get('https://moodle.rwth-aachen.de/my/', params=params)
soup = bs(response.text, features="html.parser")

## Get Courses
categories = [(c["value"], c.text) for c in soup.find("select", {"name": "coc-category"}).findAll("option")]
categories.remove(('all', 'All'))
selected_categories = [c for c in categories if c == max(categories,key=lambda item:int(item[0])) ] if onlyfetchcurrentsemester else categories
courses = [(c.find("h3").find("a")["href"], semestername) for (sid, semestername) in selected_categories for c in soup.select(f".coc-category-{sid}")]

##DEBUG
#courses=[courses[-1]]

for cid, semestername in courses:
	response = session.get(cid, params=params)
	soup = bs(response.text, features="html.parser")

	coursename = clean_filename(soup.select(".page-header-headings")[0].text)
	if not os.path.exists(basedir + semestername + "/" + coursename):
		os.makedirs(basedir + semestername + "/" + coursename)
	print(f"Syncing {coursename}...")

	# Get Sections. Some courses have them on one page, others on multiple, then we need to crawl all of them
	sectionpages = re.findall("&section=[0-9]+",response.text)
	if sectionpages:
		sections = []
		for s in sectionpages:
			response = session.get(cid+s, params=params)
			tempsoup = bs(response.text, features="html.parser")
			opendatalti = tempsoup.find("form", {"name": "ltiLaunchForm"})
			sections.extend([(c,opendatalti) for c in tempsoup.select(".topics")[0].children])
	else:
		sections = [(c,None) for c in soup.select(".topics")[0].children]

	for s,opendatalti in sections:
		sectionname = clean_filename(s.attrs["aria-label"])

		## Get Resources
		resources = s.findAll("li", {"class": "resource"})
		resources = [r.find('a', href=True)["href"] for r in resources if r.find('a', href=True)]
		for r in resources:
			# First check if the file is a video:
			if not download_file(r,basedir + semestername + "/" + coursename + "/" + sectionname + "/", session): #no filenames here unfortunately
				response = session.get(r, params=params)
				if "Content-Type" in response.headers and "text/html" in response.headers["Content-Type"]:
					tempsoup = bs(response.text, features="html.parser")
					videojs = tempsoup.select(".video-js")
					if videojs:
						videojs = tempsoup.select(".video-js")[0].find("source")
						if videojs and "src" in videojs.attrs.keys():
							download_file(videojs["src"],basedir + semestername + "/" + coursename + "/" + sectionname + "/", session, videojs["src"].split("/")[-1])

		## Get Resources in URLs
		url_resources = s.findAll("li", {"class": "url"})
		url_resources = [r.find('a', href=True)["href"] for r in url_resources if r.find('a', href=True)]
		for r in url_resources:
			response = session.head(r, params=params)
			if "Location" in response.headers:
				url = response.headers["Location"]
				response = session.head(url, params=params)
				if "Content-Type" in response.headers and "text/html" not in response.headers["Content-Type"]: # don't download html pages
					download_file(url,basedir + semestername + "/" + coursename + "/" + sectionname + "/", session)

		## Get Folders
		folders = s.findAll("li", {"class": "folder"})
		folders = [r.find('a', href=True)["href"] for r in folders if r.find('a', href=True)]

		for f in folders:
			response = session.get(f, params=params)
			soup = bs(response.text, features="html.parser")
			
			foldername = clean_filename(soup.find("a",{"title": "Folder"}).text)

			filemanager = soup.select(".filemanager")[0].findAll('a', href=True)
			#scheiß auf folder, das mach ich 1 andernmal
			for file in filemanager:
				link = file["href"]
				filename = file.select(".fp-filename")[0].text
				download_file(link, basedir + semestername + "/" + coursename + "/" + sectionname + "/" + foldername + "/", session, filename)

		## Get Assignments
		assignments = s.findAll("li", {"class": "assign"})
		assignments = [r.find('a', href=True)["href"] for r in assignments if r.find('a', href=True)]
		for a in assignments:
			response = session.get(a, params=params)
			soup = bs(response.text, features="html.parser")

			files = soup.select(".fileuploadsubmission")

			foldername = clean_filename(soup.find("a",{"title": "Assignment"}).text)
			
			for file in files:
				link = file.find('a', href=True)["href"]
				filename = file.text
				if len(filename) > 2: #remove space around the file
					filename = filename[1:-1]
				download_file(link, basedir + semestername + "/" + coursename + "/" + sectionname + "/" + foldername + "/", session, filename)

		## Get embedded videos in pages
		pages = s.findAll("li", {"class": "page"})
		pages = [r.find('a', href=True)["href"] for r in pages if r.find('a', href=True)]
		for p in pages:
			response = session.get(p, params=params)
			soup = bs(response.text, features="html.parser")

			pagename = soup.find("a",{"title": "Page"}).text

			links = re.findall("https://www.youtube.com/embed/.{11}", response.text)
			path = basedir + semestername + "/" + coursename + "/" + sectionname + "/" + pagename + "/"
			ydl_opts = {
							"outtmpl": "{}%(title)s-%(id)s.%(ext)s".format(path),
							"ignoreerrors": True,
							"nooverwrites": True,
							"retries": 15
						}
			with youtube_dl.YoutubeDL(ydl_opts) as ydl:
				ydl.download(links)
		
		if opendatalti:
			## Get Opencast Videos
			
			#Login
			engageData = dict([(i["name"], i["value"]) for i in opendatalti.findAll("input")])
			response = session.post('https://engage.streaming.rwth-aachen.de/lti', data=engageData)

			opencastembedds = s.findAll("iframe")
			for o in opencastembedds:
				link = o["data-framesrc"]
				linkid = re.match("https://engage.streaming.rwth-aachen.de/play/([a-z0-9\-]{36})$", link)
				if linkid:
					episodejson = f'https://engage.streaming.rwth-aachen.de/search/episode.json?id={linkid.groups()[0]}'
					episodejson = json.loads(session.get(episodejson).text)
					tracks = episodejson["search-results"]["result"]["mediapackage"]["media"]["track"]

					# get mp4 1080p (is not always 1080p, but the one with the highest quality)
					finaltrack = [t for t in tracks if (t["mimetype"] == 'video/mp4') and ("1080" in str(t["tags"])) ][0]
					download_file(finaltrack["url"], basedir + semestername + "/" + coursename + "/" + sectionname + "/", session, finaltrack["url"].split("/")[-1])
