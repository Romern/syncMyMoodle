import re

# Characters removed from any path segment derived from Moodle names/URLs.
INVALID_CHARS = '~"#%&*:<>?/\\{|}'

YOUTUBE_ID_LENGTH = 11
HASH_ALGOS_BY_LENGTH = {32: "md5", 40: "sha1", 64: "sha256"}
CHECKSUM_LENGTHS_BY_ALGO = {
    algo: length for length, algo in HASH_ALGOS_BY_LENGTH.items()
}
YOUTUBE_LINK_RE = re.compile(
    r"(https?://(www\.)?(youtube\.com/(watch\?[a-zA-Z0-9_=&-]*v=|embed/)|youtu.be/).{11})"
)
OPENCAST_LINK_RE = re.compile(
    r"https://engage\.streaming\.rwth-aachen\.de/play/[a-zA-Z0-9-]+"
)
SCIEBO_LINK_RE = re.compile(r"https://rwth-aachen\.sciebo\.de/s/[a-zA-Z0-9-]+")
MOODLE_URL = "https://moodle.rwth-aachen.de/"
RWTH_HOMEPAGE_URL = "https://www.rwth-aachen.de/"
RWTH_STATUS_URL = "https://maintenance.itc.rwth-aachen.de/ticket/status/messages"
RWTH_MOODLE_STATUS_URL = (
    "https://maintenance.itc.rwth-aachen.de/ticket/status/messages/499?locale=en"
)
RWTH_SSO_STATUS_URL = (
    "https://maintenance.itc.rwth-aachen.de/ticket/status/messages/462?locale=en"
)
RWTH_DISRUPTIVE_STATUS_CLASSES = {
    "statuslabel_stoerung",
    "statuslabel_teilstoerung",
    "statuslabel_wartung",
    "statuslabel_warnung",
}
COURSE_PREFIX_RE = re.compile(r"^\((?P<prefix>[^()]{2})\) +(?P<course_name>.+)$")
COURSE_PREFIX_HANDLING_OPTIONS = ("keep", "remove", "suffix")
