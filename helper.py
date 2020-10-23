import requests, pickle
from bs4 import BeautifulSoup as bs
import youtube_dl
import string
import os
import unicodedata
import re
from contextlib import closing
import urllib.parse
import json

valid_filename_chars = "-_.() %s%süöäßÖÄÜ" % (string.ascii_letters, string.digits)
char_limit = 255
replace_spaces_by_underscores = True

### Helper functions

def login(username, password, cookie_file):
	s = requests.Session()
	if os.path.exists(cookie_file):
		with open(cookie_file, 'rb') as f:
			s.cookies.update(pickle.load(f))
	resp = s.get("https://moodle.rwth-aachen.de/")
	resp = s.get("https://moodle.rwth-aachen.de/auth/shibboleth/index.php")
	if resp.url == "https://moodle.rwth-aachen.de/my/":
		with open(cookie_file, 'wb') as f:
			pickle.dump(s.cookies, f)
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
	if replace_spaces_by_underscores:
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
				#print(f"Could not get filename from {url} ...")
				return False
		downloadpath = os.path.join(path,clean_filename(filename))
		if not os.path.exists(downloadpath):
			if not os.path.exists(path):
				os.makedirs(path)
			with open(downloadpath,"wb") as file:
				file.write(response.content)
			print(f"Downloaded {downloadpath}")
			return True
	return False

def scanAndDownloadYouTube(soup, path):
	links = re.findall("https://www.youtube.com/embed/.{11}", str(soup))
	finallinks = []
	if os.path.exists(path):
		for l in links:
			if len([f for f in os.listdir(path) if l[-11:] in f])==0:
				finallinks.append(l)
	else:
		finallinks = links
	ydl_opts = {
		"outtmpl": "{}/%(title)s-%(id)s.%(ext)s".format(path),
		"ignoreerrors": True,
		"nooverwrites": True,
		"retries": 15
	}
	if not finallinks:
		return
	if not os.path.exists(path):
		os.makedirs(path)
	with youtube_dl.YoutubeDL(ydl_opts) as ydl:
		ydl.download(finallinks)

def downloadOpenCastVideos(engageLink, courseid, session_key, path, session):

	# get engage authentication form
	course_info = [{"index":0,"methodname":"filter_opencast_get_lti_form","args":{"courseid":str(courseid)}}]
	response = session.post(f'https://moodle.rwth-aachen.de/lib/ajax/service.php?sesskey={session_key}&info=filter_opencast_get_lti_form', data=json.dumps(course_info))

	# submit engage authentication info
	engageDataSoup = bs(response.json()[0]["data"], features="html.parser")
	engageData = dict([(i["name"], i["value"]) for i in engageDataSoup.findAll("input")])
	response = session.post('https://engage.streaming.rwth-aachen.de/lti', data=engageData)

	linkid = re.match("https://engage.streaming.rwth-aachen.de/play/([a-z0-9\-]{36})$", engageLink)
	if not linkid:
		return
	episodejson = f'https://engage.streaming.rwth-aachen.de/search/episode.json?id={linkid.groups()[0]}'
	episodejson = json.loads(session.get(episodejson).text)
	#print(episodejson)
	tracks = episodejson["search-results"]["result"]["mediapackage"]["media"]["track"]
	tracks = sorted([(t["url"],t["video"]["resolution"]) for t in tracks if t["mimetype"] == 'video/mp4' and "transport" not in t], key=(lambda x: int(x[1].split("x")[0]) ))
	# only choose mp4s provided with plain https (no transport key), and use the one with the highest resolution (sorted by width) (could also use bitrate)
	finaltrack = tracks[-1]
	download_file(finaltrack[0],
		path, 
		session, 
		finaltrack[0].split("/")[-1])
