#!/usr/bin/env python3

import base64
import getpass
import hashlib
import http.client
import json
import logging
import os
import pickle
import re
import shutil
import sys
import urllib.parse
from argparse import ArgumentParser
from contextlib import closing
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, List

import pdfkit
import requests
import yt_dlp
from bs4 import BeautifulSoup as bs
from tqdm import tqdm

try:
    import secretstorage
except ImportError:
    if not TYPE_CHECKING:
        # An ignore hint does not work as it would be marked as superfluous
        # by mypy if secretstorage is installed.
        # Therefore we result to the TYPE_CHECKING constant
        secretstorage = None

YOUTUBE_ID_LENGTH = 11

logger = logging.getLogger(__name__)


class Node:
    def __init__(
        self,
        name,
        id,
        type,
        parent,
        url=None,
        additional_info=None,
        is_downloaded=False,
    ):
        self.name = name
        self.id = id
        self.url = url
        self.type = type
        self.parent = parent
        self.children: List[Node] = []
        self.additional_info = (
            additional_info  # Currently only used for course_id in opencast
        )
        self.is_downloaded = (
            is_downloaded  # Can also be used to exclude files from being downloaded
        )

    def __repr__(self):
        return f"Node(name={self.name}, id={self.id}, url={self.url}, type={self.type})"

    def add_child(self, name, id, type, url=None, additional_info=None):
        if url:
            url = url.replace("?forcedownload=1", "").replace(
                "mod_page/content/3", "mod_page/content"
            )
            url = url.replace("webservice/pluginfile.php", "pluginfile.php")

        # Check for duplicate urls and just ignore those nodes:
        if url and any([True for c in self.children if c.url == url]):
            return None

        temp = Node(name, id, type, self, url=url, additional_info=additional_info)
        self.children.append(temp)
        return temp

    def get_path(self):
        ret = []
        cur = self
        while cur is not None:
            ret.insert(0, cur.name)
            cur = cur.parent
        return ret

    def remove_children_nameclashes(self):
        # Check for duplicate filenames

        unclashed_children = []
        # work on copy since deleting from the iterated list breaks stuff
        copy_children = self.children.copy()
        for child in copy_children:
            if child not in self.children:
                continue
            self.children.remove(child)
            unclashed_children.append(child)
            if child.type == "Opencast":
                siblings = [
                    c
                    for c in self.children
                    if c.name == child.name and c.url != child.url
                ]
                if len(siblings) > 0:
                    # if an Opencast filename is duplicate in its directory, we append the filename as it was uploaded
                    tmp_name = Path(child.name).name
                    child.name = f"{tmp_name}_{child.url.split('/')[-1]}"
                    for s in siblings:
                        tmp_name = Path(s.name).name
                        s.name = f"{s.name}_{s.url.split('/')[-1]}"
                        self.children.remove(s)
                    unclashed_children.extend(siblings)

        self.children = unclashed_children

        unclashed_children = []
        copy_children = self.children.copy()
        for child in copy_children:
            if child not in self.children:
                continue
            self.children.remove(child)
            unclashed_children.append(child)
            siblings = [
                c for c in self.children if c.name == child.name and c.url != child.url
            ]
            if len(siblings) > 0:
                # if a filename is still duplicate in its directory, we rename it by appending its id (urlsafe base64 so it also works for urls).
                filename = Path(child.name)
                child.name = (
                    filename.stem
                    + "_"
                    + base64.urlsafe_b64encode(
                        hashlib.md5(str(child.id).encode("utf-8"))
                        .hexdigest()
                        .encode("utf-8")
                    ).decode()[:10]
                    + filename.suffix
                )
                for s in siblings:
                    filename = Path(s.name)
                    s.name = (
                        filename.stem
                        + "_"
                        + base64.urlsafe_b64encode(
                            hashlib.md5(str(s.id).encode("utf-8"))
                            .hexdigest()
                            .encode("utf-8")
                        ).decode()[:10]
                        + filename.suffix
                    )
                    self.children.remove(s)
                unclashed_children.extend(siblings)

        self.children = unclashed_children

        for child in self.children:
            # recurse whole tree
            child.remove_children_nameclashes()


class SyncMyMoodle:
    params = {"lang": "en"}  # Titles for some pages differ
    block_size = 1024
    invalid_chars = '~"#%&*:<>?/\\{|}'

    def __init__(self, config):
        self.config = config
        self.session = None
        self.session_key = None
        self.wstoken = None
        self.user_id = None
        self.root_node = None

    # RWTH SSO Login

    def login(self):
        def get_session_key(soup):
            script = soup.find("script", string=lambda text: text and "sesskey" in text)
            js_text = script.text
            match = re.search(r'"sesskey":"(.*?)"', js_text)
            if match:
                return match.group(1)
            else:
                logger.critical("Can't retrieve session key from JavaScript config")
                exit(1)

        self.session = requests.Session()
        cookie_file = Path(self.config.get("cookie_file", "./session"))
        if cookie_file.exists():
            with cookie_file.open("rb") as f:
                self.session.cookies.update(pickle.load(f))
        resp = self.session.get("https://moodle.rwth-aachen.de/")
        resp = self.session.get(
            "https://moodle.rwth-aachen.de/auth/shibboleth/index.php"
        )
        if resp.url == "https://moodle.rwth-aachen.de/my/":
            soup = bs(resp.text, features="html.parser")
            self.session_key = get_session_key(soup)
            with cookie_file.open("wb") as f:
                pickle.dump(self.session.cookies, f)
            return
        soup = bs(resp.text, features="html.parser")
        if soup.find("input", {"name": "RelayState"}) is None:
            csrf_token = soup.find("input", {"name": "csrf_token"})["value"]
            data = {
                "j_username": self.config["user"],
                "j_password": self.config["password"],
                "_eventId_proceed": "",
                "csrf_token": csrf_token,
            }
            resp2 = self.session.post(resp.url, data=data)
            soup = bs(resp2.text, features="html.parser")
        if soup.find("input", {"name": "RelayState"}) is None:
            logger.critical(
                "Failed to login! Maybe your login-info was wrong or the RWTH-Servers have difficulties, see https://maintenance.rz.rwth-aachen.de/ticket/status/messages . For more info use the --verbose argument."
            )
            logger.info("-------Login-Error-Soup--------")
            logger.info(soup)
            sys.exit(1)
        data = {
            "RelayState": soup.find("input", {"name": "RelayState"})["value"],
            "SAMLResponse": soup.find("input", {"name": "SAMLResponse"})["value"],
        }
        resp = self.session.post(
            "https://moodle.rwth-aachen.de/Shibboleth.sso/SAML2/POST", data=data
        )
        with cookie_file.open("wb") as f:
            soup = bs(resp.text, features="html.parser")
            self.session_key = get_session_key(soup)
            pickle.dump(self.session.cookies, f)

    # Moodle Web Services API

    def get_moodle_wstoken(self):
        if not self.session:
            raise Exception("You need to login() first.")
        params = {
            "service": "moodle_mobile_app",
            "passport": 1,
            "urlscheme": "moodlemobile",
        }
        # response = self.session.head("https://moodle.rwth-aachen.de/admin/tool/mobile/launch.php", params=params, allow_redirects=False)

        def getCookies(cookie_jar, domain):
            # workaround for macos
            cookie_dict = cookie_jar.get_dict(domain=domain)
            found = ["%s=%s" % (name, value) for (name, value) in cookie_dict.items()]
            return ";".join(found)

        conn = http.client.HTTPSConnection("moodle.rwth-aachen.de")
        conn.request(
            "GET",
            "/admin/tool/mobile/launch.php?" + urllib.parse.urlencode(params),
            headers={
                "Cookie": getCookies(self.session.cookies, "moodle.rwth-aachen.de")
            },
        )
        response = conn.getresponse()

        # token is in an app schema, which contains the wstoken base64-encoded along with some other token
        token_base64d = response.getheader("Location").split("token=")[1]
        self.wstoken = base64.b64decode(token_base64d).decode().split(":::")[1]
        return self.wstoken

    def get_all_courses(self):
        data = {
            "requests[0][function]": "core_enrol_get_users_courses",
            "requests[0][arguments]": json.dumps(
                {"userid": str(self.user_id), "returnusercount": "0"}
            ),
            "requests[0][settingfilter]": 1,
            "requests[0][settingfileurl]": 1,
            "wsfunction": "tool_mobile_call_external_functions",
            "wstoken": self.wstoken,
        }
        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "tool_mobile_call_external_functions",
        }
        resp = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )
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
            "wsfunction": "core_course_get_contents",
        }
        resp = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )
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
            "wsfunction": "core_webservice_get_site_info",
        }
        resp = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )
        if not resp.json().get("userid") or not resp.json()["userprivateaccesskey"]:
            logger.critical(
                f"Error while getting userid and access key: {json.dumps(resp.json(), indent=4)}"
            )
            sys.exit(1)
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
            "wstoken": self.wstoken,
        }
        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "mod_assign_get_assignments",
        }
        resp = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )
        return resp.json()["courses"][0] if len(resp.json()["courses"]) > 0 else None

    def get_assignment_submission_files(self, assignment_id):
        data = {
            "assignid": assignment_id,
            "userid": self.user_id,
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
            "wsfunction": "mod_assign_get_submission_status",
            "wstoken": self.wstoken,
        }

        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "mod_assign_get_submission_status",
        }

        response = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )

        logger.info(f"------ASSIGNMENT-{assignment_id}-DATA------")
        logger.info(response.text)

        files = (
            response.json()
            .get("lastattempt", {})
            .get("submission", {})
            .get("plugins", [])
        )
        files += (
            response.json()
            .get("lastattempt", {})
            .get("teamsubmission", {})
            .get("plugins", [])
        )
        files += response.json().get("feedback", {}).get("plugins", [])

        files = [
            f.get("files", [])
            for p in files
            for f in p.get("fileareas", [])
            if f["area"] in ["download", "submission_files", "feedback_files"]
        ]
        files = [f for folder in files for f in folder]
        return files

    def get_folders_by_courses(self, course_id):
        data = {
            "courseids[0]": str(course_id),
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
            "wsfunction": "mod_folder_get_folders_by_courses",
            "wstoken": self.wstoken,
        }

        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "mod_folder_get_folders_by_courses",
        }

        response = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )
        folder = response.json()["folders"]
        return folder

    def sync(self):
        """Retrives the file tree for all courses"""
        if not self.session:
            raise Exception("You need to login() first.")
        if not self.wstoken:
            raise Exception("You need to get_moodle_wstoken() first.")
        if not self.user_id:
            raise Exception("You need to get_userid() first.")
        self.root_node = Node("", -1, "Root", None)

        # Syncing all courses
        for course in self.get_all_courses():
            course_name = course["shortname"]
            course_id = course["id"]

            if (
                len(
                    [
                        c
                        for c in self.config.get("skip_courses", [])
                        if str(course_id) in c
                    ]
                )
                > 0
            ):
                continue

            # Skip not selected courses
            if (
                len(self.config.get("selected_courses", [])) > 0
                and len(
                    [
                        c
                        for c in self.config.get("selected_courses", [])
                        if str(course["id"]) in c
                    ]
                )
                == 0
            ):
                continue

            semestername = course["idnumber"][:4]
            # Skip not selected semesters
            if (
                len(self.config.get("selected_courses", [])) == 0
                and self.config.get("only_sync_semester", [])
                and semestername not in self.config.get("only_sync_semester", [])
            ):
                continue

            semester_node = [
                s for s in self.root_node.children if s.name == semestername
            ]
            if len(semester_node) == 0:
                semester_node = self.root_node.add_child(semestername, None, "Semester")
            else:
                semester_node = semester_node[0]

            course_node = semester_node.add_child(course_name, course_id, "Course")

            print(f"Syncing {course_name}...")
            assignments = self.get_assignment(course_id)
            folders = self.get_folders_by_courses(course_id)

            logger.info("-----------------------")
            logger.info(f"------{semestername} - {course_name}------")
            logger.info("------COURSE-DATA------")
            logger.info(json.dumps(course))
            logger.info("------ASSIGNMENT-DATA------")
            logger.info(json.dumps(assignments))
            logger.info("------FOLDER-DATA------")
            logger.info(json.dumps(folders))

            for section in self.get_course(course_id):
                if isinstance(section, str):
                    logger.error(f"Error syncing section in {course_name}: {section}")
                    continue
                logger.info("------SECTION-DATA------")
                logger.info(json.dumps(section))
                section_node = course_node.add_child(
                    section["name"], section["id"], "Section"
                )
                for module in section["modules"]:
                    try:
                        # Get Assignments
                        if module["modname"] == "assign" and self.config.get(
                            "used_modules", {}
                        ).get("assign", {}):
                            if assignments is None:
                                continue
                            ass = [
                                a
                                for a in assignments.get("assignments")
                                if a["cmid"] == module["id"]
                            ]
                            if len(ass) == 0:
                                continue
                            ass = ass[0]
                            assignment_id = ass["id"]
                            assignment_name = module["name"]
                            assignment_node = section_node.add_child(
                                assignment_name, assignment_id, "Assignment"
                            )

                            ass = ass[
                                "introattachments"
                            ] + self.get_assignment_submission_files(assignment_id)
                            for c in ass:
                                if c["filepath"] != "/":
                                    assignment_node.add_child(
                                        str(
                                            Path(
                                                self.sanitize(c["filepath"]),
                                                self.sanitize(c["filename"]),
                                            )
                                        ),
                                        c["fileurl"],
                                        "Assignment File",
                                        url=c["fileurl"],
                                    )
                                else:
                                    assignment_node.add_child(
                                        c["filename"],
                                        c["fileurl"],
                                        "Assignment File",
                                        url=c["fileurl"],
                                    )

                        # Get Resources or URLs
                        if module["modname"] in [
                            "resource",
                            "url",
                            "book",
                            "page",
                            "pdfannotator",
                        ]:
                            if module["modname"] == "resource" and not self.config.get(
                                "used_modules", {}
                            ).get("resource", {}):
                                continue
                            for c in module.get("contents", []):
                                if c["fileurl"]:
                                    self.scanForLinks(
                                        c["fileurl"],
                                        section_node,
                                        course_id,
                                        single=True,
                                        module_title=module["name"],
                                    )

                        # Get Folders
                        if module["modname"] == "folder" and self.config.get(
                            "used_modules", {}
                        ).get("folder", {}):
                            folder_node = section_node.add_child(
                                module["name"], module["id"], "Folder"
                            )

                            # Scan intro for links
                            rel_folder = [
                                f["intro"]
                                for f in folders
                                if f["coursemodule"] == module["id"]
                            ]
                            if rel_folder:
                                self.scanForLinks(rel_folder[0], folder_node, course_id)

                            for c in module.get("contents", []):
                                if c["filepath"] != "/":
                                    while c["filepath"][-1] == "/":
                                        c["filepath"] = c["filepath"][:-1]
                                    while c["filepath"][0] == "/":
                                        c["filepath"] = c["filepath"][1:]
                                    folder_node.add_child(
                                        str(
                                            Path(
                                                self.sanitize(c["filepath"]),
                                                self.sanitize(c["filename"]),
                                            )
                                        ),
                                        c["fileurl"],
                                        "Folder File",
                                        url=c["fileurl"],
                                    )
                                else:
                                    folder_node.add_child(
                                        c["filename"],
                                        c["fileurl"],
                                        "Folder File",
                                        url=c["fileurl"],
                                    )

                        # Get embedded videos in pages or labels
                        if module["modname"] in [
                            "page",
                            "label",
                            "h5pactivity",
                        ] and self.config.get("used_modules", {}).get("url", {}):
                            if module["modname"] == "page":
                                self.scanForLinks(
                                    module["url"],
                                    section_node,
                                    course_id,
                                    module_title=module["name"],
                                    single=True,
                                )
                            # "Interactive" h5p videos
                            elif module["modname"] == "h5pactivity":
                                html_url = f'https://moodle.rwth-aachen.de/mod/h5pactivity/view.php?id={module["id"]}'
                                html = bs(
                                    self.session.get(html_url).text,
                                    features="html.parser",
                                )
                                # Get h5p iframe
                                iframe = html.find("iframe")
                                if iframe is not None:
                                    iframe_html = str(
                                        bs(
                                            self.session.get(iframe.attrs["src"]).text,
                                            features="html.parser",
                                        )
                                    )
                                    # Moodle devs dont know how to use CDATA correctly, so we need to remove all backslashes
                                    sanitized_html = iframe_html.replace("\\", "")
                                else:
                                    # H5P outside iframes
                                    sanitized_html = str(html).replace("\\", "")

                                self.scanForLinks(
                                    sanitized_html,
                                    section_node,
                                    course_id,
                                    module_title=module["modname"],
                                    single=False,
                                )
                            else:
                                self.scanForLinks(
                                    module.get("description", ""),
                                    section_node,
                                    course_id,
                                    module_title=module["name"],
                                )

                        # New OpenCast integration
                        if module["modname"] == "lti" and self.config.get(
                            "used_modules", {}
                        ).get("url", {}).get("opencast", {}):
                            info_url = f'https://moodle.rwth-aachen.de/mod/lti/launch.php?id={module["id"]}&triggerview=0'
                            info_res = bs(
                                self.session.get(info_url).text, features="html.parser"
                            )
                            # FIXME: For now we assume that all lti modules will lead to an opencast video
                            engage_id = info_res.find("input", {"name": "custom_id"})
                            name = info_res.find(
                                "input", {"name": "resource_link_title"}
                            )
                            if not engage_id:
                                logger.error("Failed to find custom_id on lti page.")
                                logger.info("------LTI-ERROR-HTML------")
                                logger.info(f"url: {info_url}")
                                logger.info(info_res)
                            else:
                                engage_id = engage_id.get("value")
                                name = name.get("value")
                                vid = self.getOpenCastRealURL(
                                    course_id,
                                    f"https://engage.streaming.rwth-aachen.de/play/{engage_id}",
                                )
                                section_node.add_child(
                                    name,
                                    engage_id,
                                    "Opencast",
                                    url=vid,
                                    additional_info=course_id,
                                )
                        # Integration for Quizzes
                        if module["modname"] == "quiz" and self.config.get(
                            "used_modules", {}
                        ).get("url", {}).get("quiz", {}):
                            info_url = f'https://moodle.rwth-aachen.de/mod/quiz/view.php?id={module["id"]}'
                            info_res = bs(
                                self.session.get(info_url).text, features="html.parser"
                            )
                            attempts = info_res.findAll(
                                "a",
                                {
                                    "title": "Überprüfung der eigenen Antworten dieses Versuchs"
                                },
                            )
                            attempt_cnt = 0
                            for attempt in attempts:
                                attempt_cnt += 1
                                review_url = attempt.get("href")
                                quiz_res = bs(
                                    self.session.get(review_url).text,
                                    features="html.parser",
                                )
                                name = (
                                    quiz_res.find("title")
                                    .get_text()
                                    .replace(": Überprüfung des Testversuchs", "")
                                    + ", Versuch "
                                    + str(attempt_cnt)
                                )
                                section_node.add_child(
                                    self.sanitize(name),
                                    urllib.parse.urlparse(review_url)[1],
                                    "Quiz",
                                    url=review_url,
                                )

                    except Exception:
                        logger.exception(f"Failed to download the module {module}")

        self.root_node.remove_children_nameclashes()

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
            if cur_node.url and not cur_node.is_downloaded:
                if cur_node.type == "Youtube":
                    try:
                        self.scanAndDownloadYouTube(cur_node)
                        cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
                        logger.error(
                            "This could be caused by an out of date yt-dlp version. Try upgrading yt-dlp through pip or your package manager."
                        )
                elif cur_node.type == "Opencast":
                    try:
                        self.downloadOpenCastVideos(cur_node)
                        cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
                elif cur_node.type == "Quiz":
                    try:
                        self.downloadQuiz(cur_node)
                        cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
                        logger.warning("Is wkhtmltopdf correctly installed?")
                else:
                    try:
                        self.download_file(cur_node)
                        cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
            return

        for child in cur_node.children:
            self._download_all_files(child)

    def get_sanitized_node_path(self, node: Node) -> Path:
        basedir = Path(self.config.get("basedir", "./")).expanduser()
        return basedir.joinpath(*(self.sanitize(p) for p in node.get_path()))

    def sanitize(self, path):
        path = urllib.parse.unquote(path)
        path = "".join([s for s in path if s not in self.invalid_chars])
        while path and path[-1] == " ":
            path = path[:-1]
        while path and path[0] == " ":
            path = path[1:]

        # Folders downloaded from Moodle display amp; in places where an
        # ampersand should be displayed instead. In the web UI, however, the
        # ampersand is shown correctly, and we're trying to emulate that here.
        path = path.replace("amp;", "&")

        return path

    def download_file(self, node):
        """Download file with progress bar if it isn't already downloaded"""
        downloadpath = self.get_sanitized_node_path(node)

        if downloadpath.exists():
            return True

        if len(node.name.split(".")) > 0 and node.name.split(".")[
            -1
        ] in self.config.get("exclude_filetypes", []):
            return True

        if any(
            fnmatchcase(node.name, pattern)
            for pattern in self.config.get("exclude_files")
        ):
            return True

        tmp_downloadpath = downloadpath.with_suffix(downloadpath.suffix + ".temp")
        if tmp_downloadpath.exists():
            resume_size = tmp_downloadpath.stat().st_size
            header = {"Range": f"bytes= {resume_size}-"}
        else:
            resume_size = 0
            header = dict()

        with closing(
            self.session.get(node.url, headers=header, stream=True)
        ) as response:
            print(f"Downloading {downloadpath} [{node.type}]")
            total_size_in_bytes = (
                int(response.headers.get("content-length", 0)) + resume_size
            )
            progress_bar = tqdm(total=total_size_in_bytes, unit="iB", unit_scale=True)
            if resume_size:
                progress_bar.update(resume_size)
            downloadpath.parent.mkdir(parents=True, exist_ok=True)
            with tmp_downloadpath.open("ab") as file:
                for data in response.iter_content(self.block_size):
                    progress_bar.update(len(data))
                    file.write(data)
            progress_bar.close()
            tmp_downloadpath.rename(downloadpath)
            return True

    def getOpenCastRealURL(self, additional_info, url):
        """Download Opencast videos by using the engage API"""
        # get engage authentication form
        course_info = [
            {
                "index": 0,
                "methodname": "filter_opencast_get_lti_form",
                "args": {"courseid": str(additional_info)},
            }
        ]
        response = self.session.post(
            f"https://moodle.rwth-aachen.de/lib/ajax/service.php?sesskey={self.session_key}&info=filter_opencast_get_lti_form",
            data=json.dumps(course_info),
        )

        # submit engage authentication info
        try:
            engageDataSoup = bs(response.json()[0]["data"], features="html.parser")
        except Exception as e:
            logger.exception("Failed to parse Opencast response!")
            logger.info("------Opencast-Error------")
            logger.info(response.text)
            raise e

        engageData = dict(
            [(i["name"], i["value"]) for i in engageDataSoup.findAll("input")]
        )
        response = self.session.post(
            "https://engage.streaming.rwth-aachen.de/lti", data=engageData
        )

        linkid = re.match(
            "https://engage.streaming.rwth-aachen.de/play/([a-z0-9-]{36})$", url
        )
        if not linkid:
            return False
        episodejson = f"https://engage.streaming.rwth-aachen.de/search/episode.json?id={linkid.groups()[0]}"
        episodejson = json.loads(self.session.get(episodejson).text)

        tracks = episodejson["search-results"]["result"]["mediapackage"]["media"][
            "track"
        ]
        tracks = sorted(
            [
                (t["url"], t["video"]["resolution"])
                for t in tracks
                if t["mimetype"] == "video/mp4" and "transport" not in t
            ],
            key=(lambda x: int(x[1].split("x")[0])),
        )
        # only choose mp4s provided with plain https (no transport key), and use the one with the highest resolution (sorted by width) (could also use bitrate)
        finaltrack = tracks[-1]

        return finaltrack[0]

    def downloadOpenCastVideos(self, node):
        if ".mp4" not in node.name:
            if node.name is not None and node.name != "":
                node.name += ".mp4"
            else:
                node.name = node.url.split("/")[-1]
        return self.download_file(node)

    def scanAndDownloadYouTube(self, node):
        """Download Youtube-Videos using yt_dlp"""
        path = self.get_sanitized_node_path(node.parent)
        link = node.url
        if path.exists():
            if any(link[-YOUTUBE_ID_LENGTH:] in f.name for f in path.iterdir()):
                return False
        ydl_opts = {
            "outtmpl": "{}/%(title)s-%(id)s.%(ext)s".format(path),
            "ignoreerrors": True,
            "nooverwrites": True,
            "retries": 15,
        }
        path.mkdir(parents=True, exist_ok=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([link])
        return True

    def downloadQuiz(self, node):
        path = self.get_sanitized_node_path(node.parent)
        path.mkdir(parents=True, exist_ok=True)

        if (path / f"{node.name}.pdf").exists():
            return True

        quiz_res = bs(self.session.get(node.url).text, features="html.parser")

        # i need to hide the left nav element because its obscuring the quiz in the resulting pdf
        for nav in quiz_res.findAll("div", {"id": "nav-drawer"}):
            nav["style"] = "visibility: hidden;"

        quiz_html = str(quiz_res)
        print("Generating quiz-PDF for " + node.name + "... [Quiz]")

        pdfkit.from_string(
            quiz_html,
            path / f"{node.name}.pdf",
            options={
                "quiet": "",
                "javascript-delay": "30000",
                "disable-smart-shrinking": "",
                "run-script": 'MathJax.Hub.Config({"CommonHTML": {minScaleAdjust: 100},"HTML-CSS": {scale: 200}}); MathJax.Hub.Queue(["Rerender", MathJax.Hub], function () {window.status="finished"})',
            },
        )

        print("...done!")
        return True

    def scanForLinks(
        self, text, parent_node, course_id, module_title=None, single=False
    ):
        # A single link is supplied and the contents of it are checked
        if single:
            try:
                text = text.replace("webservice/pluginfile.php", "pluginfile.php")
                response = self.session.head(text)
                if "youtube.com" in text or "youtu.be" in text:
                    # workaround for youtube providing bad headers when using HEAD
                    pass
                elif (
                    "Content-Type" in response.headers
                    and "text/html" not in response.headers["Content-Type"]
                ):
                    # non html links, assume the filename is in the path
                    filename = urllib.parse.urlsplit(text).path.split("/")[-1]
                    parent_node.add_child(
                        filename,
                        None,
                        f'Linked file [{response.headers["Content-Type"]}]',
                        url=text,
                    )
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
                            link = urllib.parse.urljoin(
                                f"{parsed.scheme}://{parsed.netloc}/{parsed.path}",
                                videojs["src"],
                            )
                            parent_node.add_child(
                                videojs["src"].split("/")[-1],
                                None,
                                "Embedded videojs",
                                url=link,
                            )
                    # further inspect the response for other links
                    self.scanForLinks(
                        response.text,
                        parent_node,
                        course_id,
                        module_title=module_title,
                        single=False,
                    )
            except Exception:
                # Maybe the url is down?
                logger.exception(f"Error while downloading url {text}")
        if self.config.get("nolinks"):
            return

        # Youtube videos
        if self.config.get("used_modules", {}).get("url", {}).get("youtube", {}):
            youtube_links = [
                u[0]
                # finds youtube.com, youtu.be and embed links
                for u in re.findall(
                    r"(https?://(www\.)?(youtube\.com/(watch\?[a-zA-Z0-9_=&-]*v=|embed/)|youtu.be/).{11})",
                    text,
                )
            ]
            for link in youtube_links:
                parent_node.add_child(
                    f"Youtube: {module_title or link}", link, "Youtube", url=link
                )

        # OpenCast videos
        if self.config.get("used_modules", {}).get("url", {}).get("opencast", {}):
            opencast_links = re.findall(
                "https://engage.streaming.rwth-aachen.de/play/[a-zA-Z0-9-]+", text
            )
            for vid in opencast_links:
                vid = self.getOpenCastRealURL(course_id, vid)
                parent_node.add_child(
                    module_title or vid.split("/")[-1],
                    vid,
                    "Opencast",
                    url=vid,
                    additional_info=course_id,
                )

        # https://rwth-aachen.sciebo.de/s/XXX
        if self.config.get("used_modules", {}).get("url", {}).get("sciebo", {}):
            sciebo_links = re.findall(
                "https://rwth-aachen.sciebo.de/s/[a-zA-Z0-9-]+", text
            )
            for vid in sciebo_links:
                response = self.session.get(vid)
                soup = bs(response.text, features="html.parser")
                url = soup.find("input", {"name": "downloadURL"})
                filename = soup.find("input", {"name": "filename"})
                if url and filename:
                    parent_node.add_child(
                        filename["value"], url["value"], "Sciebo file", url=url["value"]
                    )


def main():
    parser = ArgumentParser(
        prog="python3 -m syncmymoodle",
        description="Synchronization client for RWTH Moodle. All optional arguments override those in config.json.",
    )

    if secretstorage:
        parser.add_argument(
            "--secretservice",
            action="store_true",
            help="use freedesktop.org's secret service integration for storing and retrieving account credentials",
        )

    parser.add_argument(
        "--user", default=None, help="set your RWTH Single Sign-On username"
    )
    parser.add_argument(
        "--password", default=None, help="set your RWTH Single Sign-On password"
    )
    parser.add_argument("--config", default=None, help="set your configuration file")
    parser.add_argument(
        "--cookiefile", default=None, help="set the location of a cookie file"
    )
    parser.add_argument(
        "--courses",
        default=None,
        help="specify the courses that should be synced using comma-separated links. Defaults to all courses, if no additional restrictions e.g. semester are defined.",
    )
    parser.add_argument(
        "--skipcourses",
        default=None,
        help="exclude specific courses using comma-separated links. Defaults to None.",
    )
    parser.add_argument(
        "--semester",
        default=None,
        help="specify semesters to be synced e.g. `22s`, comma-separated. Defaults to all semesters, if no additional restrictions e.g. courses are defined.",
    )
    parser.add_argument(
        "--basedir",
        default=None,
        help="specify the directory where all files will be synced",
    )
    parser.add_argument(
        "--nolinks",
        action="store_true",
        help="define whether various links in moodle pages should also be inspected e.g. youtube videos, wikipedia articles",
    )
    parser.add_argument(
        "--excludefiletypes",
        default=None,
        help='specify whether specific file types should be excluded, comma-separated e.g. "mp4,mkv"',
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_const",
        dest="loglevel",
        const=logging.INFO,
        default=logging.WARNING,
        help="show information useful for debugging",
    )
    args = parser.parse_args()

    if args.config:
        overwrite_config = Path(args.config)
        if overwrite_config.is_file():
            with overwrite_config.open() as f:
                config = json.load(f)
    else:
        config = {}

        global_config = (
            Path(os.environ.get("XDG_CONFIG_HOME", Path("~/.config").expanduser()))
            / "syncmymoodle"
            / "config.json"
        )
        if global_config.is_file():
            with global_config.open() as f:
                config.update(json.load(f))

        local_config = Path("config.json")
        if local_config.is_file():
            with local_config.open() as f:
                config.update(json.load(f))

    config["user"] = args.user or config.get("user")
    config["password"] = args.password or config.get("password")
    config["cookie_file"] = args.cookiefile or config.get("cookie_file", "./session")
    config["selected_courses"] = (
        args.courses.split(",") if args.courses else config.get("selected_courses", [])
    )
    config["only_sync_semester"] = (
        args.semester.split(",")
        if args.semester
        else config.get("only_sync_semester", [])
    )
    config["basedir"] = args.basedir or config.get("basedir", "./")
    config["use_secret_service"] = (
        args.secretservice if secretstorage else None
    ) or config.get("use_secret_service")
    config["skip_courses"] = (
        args.skipcourses.split(",")
        if args.skipcourses
        else config.get("skip_courses", [])
    )
    config["nolinks"] = args.nolinks or config.get("no_links")
    config["used_modules"] = config.get("used_modules") or {
        "assign": True,
        "resource": True,
        "url": {"youtube": True, "opencast": True, "sciebo": True, "quiz": False},
        "folder": True,
    }
    config["exclude_filetypes"] = (
        args.excludefiletypes.split(",")
        if args.excludefiletypes
        else config.get("exclude_filetypes", [])
    )

    config["exclude_files"] = config.get("exclude_files", [])

    logging.basicConfig(level=args.loglevel)

    if not shutil.which("wkhtmltopdf") and config["used_modules"]["url"]["quiz"]:
        config["used_modules"]["url"]["quiz"] = False
        logger.warning(
            "You do not have wkhtmltopdf in your path. Quiz-PDFs are NOT generated"
        )

    if secretstorage and config.get("use_secret_service"):
        if config.get("password"):
            logger.critical("You need to remove your password from your config file!")
            sys.exit(1)

        connection = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(connection)
        if collection.is_locked():
            collection.unlock()
        attributes = {"application": "syncMyMoodle"}
        results = list(collection.search_items(attributes))
        if len(results) == 0:
            if not args.user and not config.get("user"):
                print(
                    "You need to provide your username in the config file or through --user!"
                )
                sys.exit(1)
            if args.password:
                password = args.password
            else:
                password = getpass.getpass("Password:")
            attributes["username"] = config["user"]
            item = collection.create_item(
                f'{config["user"]}@rwth-aachen.de', attributes, password
            )
        else:
            item = results[0]
            if item.is_locked():
                """
                item.unlock() returns true if the promt has been dismissed, therefore we
                'busy-wait' for false.
                """
                while item.unlock():
                    print("Please confirm to unlock the password if prompted!")
                    pass
        if not config.get("user"):
            config["user"] = item.get_attributes().get("username")
        config["password"] = item.get_secret().decode("utf-8")

    if not config.get("user") or not config.get("password"):
        logger.critical(
            "You need to specify your username and password in the config file or as an argument!"
        )
        sys.exit(1)

    smm = SyncMyMoodle(config)

    print("Logging in...")
    smm.login()
    smm.get_moodle_wstoken()
    smm.get_userid()
    print("Syncing file tree...")
    smm.sync()
    print("Downloading files...")
    smm.download_all_files()


if __name__ == "__main__":
    main()
