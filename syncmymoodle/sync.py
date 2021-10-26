import base64
import http.client
import json
import logging
import pickle
import re
import sys
import urllib.parse
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Optional

import pdfkit
import requests
import youtube_dl
from bs4 import BeautifulSoup as bs
from requests.cookies import RequestsCookieJar
from tqdm import tqdm

from syncmymoodle.filetree import Node

YOUTUBE_ID_LENGTH = 11
logger = logging.getLogger(__name__)


class SyncMyMoodle:
    params = {"lang": "en"}  # Titles for some pages differ
    block_size = 1024

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.session = requests.Session()
        self.wstoken: Optional[str] = None
        self.opencast_wstoken: Optional[str] = None
        self.user_id: Optional[int] = None
        self.root_node = Node("", -1, "Root")

    # RWTH SSO Login

    def login(self) -> None:
        cookie_file = Path(self.config.get("cookie_file", "./session"))
        if cookie_file.exists():
            with cookie_file.open("rb") as f:
                self.session.cookies.update(pickle.load(f))  # type: ignore
        resp = self.session.get("https://moodle.rwth-aachen.de/")
        resp = self.session.get(
            "https://moodle.rwth-aachen.de/auth/shibboleth/index.php"
        )
        if resp.url == "https://moodle.rwth-aachen.de/my/":
            soup = bs(resp.text, features="html.parser")
            with cookie_file.open("wb") as f:
                pickle.dump(self.session.cookies, f)
            return
        soup = bs(resp.text, features="html.parser")
        if soup.find("input", {"name": "RelayState"}) is None:
            data = {
                "j_username": self.config["user"],
                "j_password": self.config["password"],
                "_eventId_proceed": "",
            }
            resp2 = self.session.post(resp.url, data=data)
            soup = bs(resp2.text, features="html.parser")
        if soup.find("input", {"name": "RelayState"}) is None:
            logger.critical(
                "Failed to login! Maybe your login-info was wrong or the RWTH-Servers have difficulties, see https://maintenance.rz.rwth-aachen.de/ticket/status/messages . For more info use the --verbose argument."
            )
            logger.debug("-------Login-Error-Soup--------")
            logger.debug(soup)
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
            pickle.dump(self.session.cookies, f)

    # Moodle Web Services API

    def get_moodle_wstoken(self) -> str:
        self.wstoken = self.get_wstoken("moodle_mobile_app")
        return self.wstoken

    def get_opencast_wstoken(self) -> str:
        self.opencast_wstoken = self.get_wstoken("filter_opencast_authentication")
        return self.opencast_wstoken

    def get_wstoken(self, service: str) -> str:
        if not self.session:
            raise Exception("You need to login() first.")
        params = {
            "service": service,
            "passport": 1,
            "urlscheme": "moodlemobile",
        }
        # response = self.session.head("https://moodle.rwth-aachen.de/admin/tool/mobile/launch.php", params=params, allow_redirects=False)

        def getCookies(cookie_jar: RequestsCookieJar, domain: str) -> str:
            # workaround for macos
            cookie_dict = cookie_jar.get_dict(domain=domain)  # type: ignore
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
        token_base64d = response.headers["Location"].split("token=")[1]
        return base64.b64decode(token_base64d).decode().split(":::")[1]

    def get_all_courses(self) -> Any:
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

    def get_course(self, course_id: int) -> Any:
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

    def get_userid(self) -> int:
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
        userid = resp.json()["userid"]
        if not isinstance(userid, int):
            raise RuntimeError("Unexpected response from webservice")
        self.user_id = userid
        return self.user_id

    def get_assignment(self, course_id: int) -> Any:
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

    def get_assignment_submission_files(self, assignment_id: int) -> Any:
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

        logger.debug(f"------ASSIGNMENT-{assignment_id}-DATA------")
        logger.debug(response.text)

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

    def get_folders_by_courses(self, course_id: int) -> Any:
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

    def sync(self) -> None:
        """Retrives the file tree for all courses"""
        if not self.session:
            raise Exception("You need to login() first.")
        if not self.wstoken:
            raise Exception("You need to get_moodle_wstoken() first.")
        if not self.wstoken:
            raise Exception("You need to get_opencast_wstoken() first.")
        if not self.user_id:
            raise Exception("You need to get_userid() first.")

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

            semester_nodes = [
                s for s in self.root_node.children if s.name == semestername
            ]
            if len(semester_nodes) == 0:
                semester_node = self.root_node.add_child(semestername, None, "Semester")
            else:
                [semester_node] = semester_nodes

            course_node = semester_node.add_child(course_name, course_id, "Course")

            logger.info(f"Syncing {course_name}...")
            assignments = self.get_assignment(course_id)
            folders = self.get_folders_by_courses(course_id)

            logger.debug("-----------------------")
            logger.debug(f"------{semestername} - {course_name}------")
            logger.debug("------COURSE-DATA------")
            logger.debug(json.dumps(course))
            logger.debug("------ASSIGNMENT-DATA------")
            logger.debug(json.dumps(assignments))
            logger.debug("------FOLDER-DATA------")
            logger.debug(json.dumps(folders))

            for section in self.get_course(course_id):
                if isinstance(section, str):
                    logger.error(f"Error syncing section in {course_name}: {section}")
                    continue
                logger.debug("------SECTION-DATA------")
                logger.debug(json.dumps(section))
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
                            matching_ass = [
                                a
                                for a in assignments.get("assignments")
                                if a["cmid"] == module["id"]
                            ]
                            try:
                                [ass] = matching_ass
                            except ValueError:
                                continue
                            assignment_id = ass["id"]
                            assignment_name = module["name"]
                            assignment_node = section_node.add_child(
                                assignment_name, assignment_id, "Assignment"
                            )

                            ass_contents = ass[
                                "introattachments"
                            ] + self.get_assignment_submission_files(assignment_id)
                            for c in ass_contents:
                                if c["filepath"] != "/":
                                    assignment_node.add_child(
                                        str(
                                            Path(
                                                urllib.parse.unquote(c["filepath"]),
                                                urllib.parse.unquote(c["filename"]),
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
                                                urllib.parse.unquote(c["filepath"]),
                                                urllib.parse.unquote(c["filename"]),
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
                        if module["modname"] in ["page", "label"] and self.config.get(
                            "used_modules", {}
                        ).get("url", {}):
                            if module["modname"] == "page":
                                self.scanForLinks(
                                    module["url"],
                                    section_node,
                                    course_id,
                                    module_title=module["name"],
                                    single=True,
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
                                logger.debug("------LTI-ERROR-HTML------")
                                logger.debug(f"url: {info_url}")
                                logger.debug(info_res)
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
                                    urllib.parse.unquote(name),
                                    urllib.parse.urlparse(review_url)[1],
                                    "Quiz",
                                    url=review_url,
                                )

                    except Exception:
                        logger.exception(f"Failed to download the module {module}")

        self.root_node.remove_children_nameclashes()

    def download_all_files(self) -> None:
        if not self.session:
            raise Exception("You need to login() first.")
        if not self.wstoken:
            raise Exception("You need to get_moodle_wstoken() first.")
        if not self.wstoken:
            raise Exception("You need to get_opencast_wstoken() first.")
        if not self.user_id:
            raise Exception("You need to get_userid() first.")
        if not self.root_node:
            raise Exception("You need to sync() first.")

        self._download_all_files(
            self.root_node, Path(self.config.get("basedir", Path.cwd())).expanduser()
        )

    def _download_all_files(self, cur_node: Node, dest: Path) -> None:
        if not cur_node.children:
            targetfile = dest / cur_node.sanitized_name
            # We are in a leaf not which represents a downloadable node
            if cur_node.url and not cur_node.is_downloaded:
                if cur_node.type == "Youtube":
                    try:
                        self.scanAndDownloadYouTube(cur_node, targetfile)
                        cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
                        logger.error(
                            "This could be caused by an out of date youtube-dl version. Try upgrading youtube-dl through pip or your package manager."
                        )
                elif cur_node.type == "Opencast":
                    try:
                        self.downloadOpenCastVideos(cur_node, targetfile)
                        cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
                elif cur_node.type == "Quiz":
                    try:
                        self.downloadQuiz(cur_node, targetfile)
                        cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
                        logger.warning("Is wkhtmltopdf correctly installed?")
                else:
                    try:
                        self.download_file(cur_node, targetfile)
                        cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
            return

        for child in cur_node.children:
            targetdir = dest / cur_node.sanitized_name
            targetdir.mkdir(exist_ok=True)
            self._download_all_files(child, targetdir)

    def download_file(self, node: Node, dest: Path) -> bool:
        """Download file with progress bar if it isn't already downloaded"""
        if dest.exists():
            return True

        if dest.suffix in self.config.get("exclude_filetypes", []):
            return True

        tmp_dest = dest.with_suffix(dest.suffix + ".temp")
        if tmp_dest.exists():
            resume_size = tmp_dest.stat().st_size
            header = {"Range": f"bytes= {resume_size}-"}
        else:
            resume_size = 0
            header = dict()

        with closing(
            self.session.get(node.url or "", headers=header, stream=True)
        ) as response:
            logger.info(f"Downloading {dest} [{node.type}]")
            total_size_in_bytes = (
                int(response.headers.get("content-length", 0)) + resume_size
            )
            progress_bar = tqdm(total=total_size_in_bytes, unit="iB", unit_scale=True)
            if resume_size:
                progress_bar.update(resume_size)
            with tmp_dest.open("ab") as file:
                for data in response.iter_content(self.block_size):
                    progress_bar.update(len(data))
                    file.write(data)
            progress_bar.close()
            tmp_dest.rename(dest)
            return True

    def getOpenCastRealURL(self, course_id: int, url: str) -> str:
        """Download Opencast videos by using the engage API"""
        # get engage authentication form
        response = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            data={
                "moodlewsrestformat": "json",
                "wsfunction": "filter_opencast_get_lti_form",
                "courseid": course_id,
                "wstoken": self.opencast_wstoken,
            },
        )

        # submit engage authentication info
        try:
            engageDataSoup = bs(response.json(), features="html.parser")
        except Exception as e:
            logger.exception("Failed to parse Opencast response!")
            logger.debug("------Opencast-Error------")
            logger.debug(response.text)
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
            return ""
        episodejson_url = f"https://engage.streaming.rwth-aachen.de/search/episode.json?id={linkid.groups()[0]}"
        episodejson = json.loads(self.session.get(episodejson_url).text)

        all_tracks = episodejson["search-results"]["result"]["mediapackage"]["media"][
            "track"
        ]
        filtered_tracks = sorted(
            [
                (t["url"], t["video"]["resolution"])
                for t in all_tracks
                if t["mimetype"] == "video/mp4" and "transport" not in t
            ],
            key=(lambda x: int(x[1].split("x")[0])),
        )
        # only choose mp4s provided with plain https (no transport key), and use the one with the highest resolution (sorted by width) (could also use bitrate)
        finaltrack = filtered_tracks[-1]
        trackurl = finaltrack[0]
        if not isinstance(trackurl, str):
            raise RuntimeError("Unexpected response from engage")
        return trackurl

    def downloadOpenCastVideos(self, node: Node, dest: Path) -> bool:
        if ".mp4" not in node.name:
            if node.name:
                node.name += ".mp4"
            else:
                node.name = urllib.parse.unquote((node.url or "").split("/")[-1])
        return self.download_file(node, dest.with_name(node.name))

    def scanAndDownloadYouTube(self, node: Node, dest: Path) -> bool:
        """Download Youtube-Videos using youtube_dl"""
        # TODO double check dest handling
        parent_dir = dest.parent
        if any(
            (node.url or "")[-YOUTUBE_ID_LENGTH:] in f.name
            for f in parent_dir.iterdir()
        ):
            return False
        parent_dir.mkdir(parents=True, exist_ok=True)
        ydl_opts = {
            "outtmpl": "{}/%(title)s-%(id)s.%(ext)s".format(parent_dir),
            "ignoreerrors": True,
            "nooverwrites": True,
            "retries": 15,
        }
        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            ydl.download([node.url])
        return True

    def downloadQuiz(self, node: Node, dest: Path) -> bool:
        # TODO double check dest handling
        pdf_dest = dest.with_suffix(".pdf")

        if pdf_dest.exists():
            return True

        quiz_res = bs(self.session.get(node.url or "").text, features="html.parser")

        # i need to hide the left nav element because its obscuring the quiz in the resulting pdf
        for nav in quiz_res.findAll("div", {"id": "nav-drawer"}):
            nav["style"] = "visibility: hidden;"

        quiz_html = str(quiz_res)
        logger.info("Generating quiz-PDF for " + node.name + "... [Quiz]")

        pdfkit.from_string(
            quiz_html,
            pdf_dest,
            options={
                "quiet": "",
                "javascript-delay": "30000",
                "disable-smart-shrinking": "",
                "run-script": 'MathJax.Hub.Config({"CommonHTML": {minScaleAdjust: 100},"HTML-CSS": {scale: 200}}); MathJax.Hub.Queue(["Rerender", MathJax.Hub], function () {window.status="finished"})',
            },
        )

        logger.info("...done!")
        return True

    def scanForLinks(
        self,
        text: str,
        parent_node: Node,
        course_id: int,
        module_title: str = None,
        single: bool = False,
    ) -> None:
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
                        urllib.parse.unquote(filename),
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
                                urllib.parse.unquote(videojs["src"].split("/")[-1]),
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
            if single and "youtube.com" in text or "youtu.be" in text:
                youtube_links = [
                    u[0]
                    for u in re.findall(
                        r"(https?://(www\.)?(youtube\.com/(watch\?[a-zA-Z0-9_=&-]*v=|embed/)|youtu.be/).{11})",
                        text,
                    )
                ]
            else:
                youtube_links = re.findall(
                    "https://www.youtube.com/embed/[a-zA-Z0-9_-]{11}", text
                )
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
                    module_title or vid.split("/")[-1], vid, "Opencast", url=vid
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
                filename_input = soup.find("input", {"name": "filename"})
                if url and filename_input:
                    parent_node.add_child(
                        filename_input["value"],
                        url["value"],
                        "Sciebo file",
                        url=url["value"],
                    )
