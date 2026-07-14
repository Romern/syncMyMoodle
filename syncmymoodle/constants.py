import re
import urllib.parse

# Characters removed from any path segment derived from Moodle names/URLs.
INVALID_CHARS = '~"#%*:<>?/\\{|}'

# Chunk size for streamed HTTP reads.
DEFAULT_BLOCK_SIZE = 1024

# Bound every direct HTTP request so an unavailable service cannot hang a run.
HTTP_TIMEOUT_SECONDS = 15

# Hidden per-course metadata cache filename.
COURSE_CACHE_FILENAME = ".syncmymoodle_cache"

YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
HASH_ALGOS_BY_LENGTH = {32: "md5", 40: "sha1", 64: "sha256"}
CHECKSUM_LENGTHS_BY_ALGO = {
    algo: length for length, algo in HASH_ALGOS_BY_LENGTH.items()
}
YOUTUBE_LINK_RE = re.compile(
    r"(https?://(www\.)?(youtube\.com/(watch\?[a-zA-Z0-9_=&-]*v=|embed/)|youtu.be/).{11})"
)
OPENCAST_URL = "https://engage.streaming.rwth-aachen.de"
OPENCAST_LINK_RE = re.compile(rf"{re.escape(OPENCAST_URL)}/play/[a-zA-Z0-9-]+")
OPENCAST_EPISODE_URL_RE = re.compile(
    rf"^{re.escape(OPENCAST_URL)}/play/([a-zA-Z0-9-]{{36}})(?:[/?#].*)?$"
)
SCIEBO_URL = "https://rwth-aachen.sciebo.de"
SCIEBO_LINK_RE = re.compile(rf"{re.escape(SCIEBO_URL)}/s/[a-zA-Z0-9-]+")
MOODLE_URL = "https://moodle.rwth-aachen.de/"
# Canonical host for same-origin checks (no port, lowercase).
MOODLE_NETLOC = urllib.parse.urlparse(MOODLE_URL).netloc.lower()
RWTH_HOMEPAGE_URL = "https://www.rwth-aachen.de/"
RWTH_TOTP_MANAGER_URL = "https://idm.rwth-aachen.de/selfservice/MFATokenManager"
RWTH_STATUS_URL = "https://maintenance.itc.rwth-aachen.de/ticket/status/messages"
RWTH_MOODLE_STATUS_URL = f"{RWTH_STATUS_URL}/499?locale=en"
RWTH_SCIEBO_STATUS_URL = f"{RWTH_STATUS_URL}/484?locale=en"
RWTH_SSO_STATUS_URL = f"{RWTH_STATUS_URL}/462?locale=en"
RWTH_DISRUPTIVE_STATUS_CLASSES = {
    "statuslabel_stoerung",
    "statuslabel_teilstoerung",
    "statuslabel_wartung",
    "statuslabel_warnung",
}
COURSE_PREFIX_RE = re.compile(r"^\((?P<prefix>[^()]{2})\) +(?P<course_name>.+)$")
COURSE_PREFIX_HANDLING_OPTIONS = ("keep", "remove", "suffix")
SECRET_PROVIDER_OPTIONS = ("1password", "bitwarden", "pass", "rbw", "gopass", "command")

# Quiz review pages can be saved as a self-contained HTML snapshot ("html"),
# rendered to PDF via a headless Chromium-family browser ("pdf"), both, or not
# at all ("off"). Legacy boolean config values map to "both"/"off".
QUIZ_MODES = ("off", "html", "pdf", "both")

# Per-resource and total budgets for inlining quiz snapshot assets. This keeps
# quiz snapshots offline-safe without accidentally pulling huge media files.
QUIZ_ASSET_MAX_BYTES = 10 * 1024 * 1024
QUIZ_SNAPSHOT_MAX_ASSET_BYTES = 50 * 1024 * 1024

# Chromium-family binaries used to render quiz snapshots to PDF, in preference
# order. Looked up on PATH via shutil.which; CHROMIUM_KNOWN_PATHS covers the
# platform-specific install locations that are usually not on PATH.
CHROMIUM_BINARY_NAMES = (
    "chromium",
    "chromium-browser",
    "chrome",
    "google-chrome",
    "google-chrome-stable",
    "msedge",
    "microsoft-edge",
    "microsoft-edge-stable",
)

# Absolute locations checked when none of CHROMIUM_BINARY_NAMES is on PATH
# (macOS app bundles and Windows Program Files installs).
CHROMIUM_KNOWN_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

# Headless render budget (ms). The snapshot has no active JavaScript, but
# Chromium's virtual time gives local assets and layout a bounded window to
# settle before printing.
CHROMIUM_PDF_TIMEOUT_MS = 30000
CHROMIUM_PROCESS_TIMEOUT_SECONDS = 90
