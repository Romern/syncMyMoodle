import requests, pickle
from bs4 import BeautifulSoup as bs
import os
import unicodedata
import re
from contextlib import closing
import urllib.parse
import json
from config import *

### Helper functions

def login(username, password):
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
				print(f"Could not get filename from {url} ...")
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

def downloadOpenCastVideos(section, opendatalti, path, session, loginOnce):
	if not loginOnce:
		engageData = dict([(i["name"], i["value"]) for i in opendatalti.findAll("input")])
		response = session.post('https://engage.streaming.rwth-aachen.de/lti', data=engageData)

	opencastembedds = section.findAll("iframe")
	for o in opencastembedds:
		link = o["data-framesrc"]
		linkid = re.match("https://engage.streaming.rwth-aachen.de/play/([a-z0-9\-]{36})$", link)
		if linkid:
			episodejson = f'https://engage.streaming.rwth-aachen.de/search/episode.json?id={linkid.groups()[0]}'
			episodejson = json.loads(session.get(episodejson).text)
			tracks = episodejson["search-results"]["result"]["mediapackage"]["media"]["track"]
			# get mp4 1080p (is not always 1080p, but the one with the highest quality)
			finaltrack = [t for t in tracks if (t["mimetype"] == 'video/mp4') and ("1080" in str(t["tags"])) ][0]
			download_file(finaltrack["url"], 
				path, 
				session, 
				finaltrack["url"].split("/")[-1])
