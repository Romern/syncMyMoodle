# syncMyMoodle

Synchronization client for RWTH Moodle

Downloads the following materials:

* Assignment files, submissions and feedback
* Resource files
* URLs: OpenCast, Youtube and Sciebo videos/files, and all other non HTML files
* Folders
* Quizzes: Downloads offline HTML of quiz attempts, with opt-in PDF generation
* Pages and Labels: Embedded Opencast and Youtube Videos

On subsequent runs, *syncMyMoodle* can also update existing files when the
content on Moodle or Sciebo (Nextcloud) changed, while optionally protecting
local edits through configurable conflict handling.

## Installation

This software requires **Python 3.11 or higher**.

### Using `pip` (recommended)

The simplest way to install *syncMyMoodle* is using pip.

You're advised to use a virtual environment to make sure that
its dependencies can't do anything evil on your machine.

Please consult
[the guide from the Python website](https://packaging.python.org/guides/installing-using-pip-and-virtual-environments/#creating-a-virtual-environment)
for more information.

If you just want to get the job done, just use the following commands:

```bash
python3 -m venv .venv
source .venv/bin/activate  # bash/zsh, for other shells view the docs
pip3 install syncmymoodle
```

After installation you can run the CLI directly as:

```bash
syncmymoodle
```

You can also install it as an isolated tool, for example using
[pipx](https://pipx.pypa.io) or [uv](https://github.com/astral-sh/uv):

```bash
pipx install syncmymoodle
# or
uv tool install syncmymoodle
```

### Manual installation

If you are living on the bleeding edge, you can also download the source
code directly and build everything by yourself.

*syncMyMoodle*'s dependencies can be installed using `pip`
or your distro's package manager (`apt`, `dnf`, `pacman`, etc.).

To install the requirements using pip execute the following command from the repository root.

```bash
# It is best to run this in a virtual environment.
# For more information see the section above.
pip3 install .
```

## Configuration

You can use *syncMyMoodle* with command line arguments or using a configuration
file. Which one is the best? Well, the answer mostly depends on how and how
often you are using it.

If you use it often, it may be best to set up a configuration file so that you
won't have to keep entering the same settings options over and over again.
If you are on Windows, want to automatically conduct backups, or use the tool
irregularly, you may want to use the command line arguments for the sake
of simplicity.

### Command line arguments

#### Using pip / tool install

Use `syncmymoodle` and pass the command line arguments directly.

#### Manual installation

```bash
source .venv/bin/activate  # if you installed using virtual environment
syncmymoodle
deactivate  # leave virtual environment
```

#### Arguments

The following command line arguments are available:

```bash
usage: python3 -m syncmymoodle [-h] [--secretservice] [--secretservicetotpsecret]
                               [--user USER] [--password PASSWORD]
                               [--totp TOTP] [--totpsecret TOTPSECRET]
                               [--config CONFIG] [--cookiefile COOKIEFILE]
                               [--courses COURSES] [--skipcourses SKIPCOURSES]
                               [--semester SEMESTER] [--basedir BASEDIR]
                               [--chromiumpath CHROMIUMPATH]
                               [--courseprefix {keep,remove,suffix}] [--nolinks]
                               [--excludefiletypes EXCLUDEFILETYPES]
                               [--excludefiles EXCLUDEFILES]
                               [--excludelinks EXCLUDELINKS]
                               [--alloweddomains ALLOWEDDOMAINS]
                               [--excludesections EXCLUDESECTIONS]
                               [--excludemodules EXCLUDEMODULES]
                               [--quiz {off,html,pdf,both}] [--updatefiles]
                               [--updatefilesconflict {rename,keep,overwrite}]
                               [-v]
                               {config} ...

Synchronization client for RWTH Moodle. All optional arguments override those
in config.toml/config.json.

positional arguments:
  {config}
    config              manage configuration files

options:
  -h, --help            show this help message and exit
  --secretservice       use system's secret service integration for storing and
                        retrieving account credentials
  --secretservicetotpsecret
                        Save TOTP secret in keyring
  --user USER           set your RWTH Single Sign-On username
  --password PASSWORD   set your RWTH Single Sign-On password
  --totp TOTP           set your RWTH Single Sign-On TOTP provider's serial
                        number (see
                        https://idm.rwth-aachen.de/selfservice/MFATokenManager)
  --totpsecret TOTPSECRET
                        (optional) set your RWTH Single Sign-On TOTP provider Secret
  --config CONFIG       set your configuration file
  --cookiefile COOKIEFILE
                        set the location of a cookie file
  --courses COURSES     specify the courses that should be synced using comma-
                        separated links. Defaults to all courses, if no
                        additional restrictions e.g. semester are defined.
  --skipcourses SKIPCOURSES
                        exclude specific courses using comma-separated links.
                        Defaults to None.
  --semester SEMESTER   specify semesters to be synced e.g. `22s`, comma-
                        separated. Defaults to all semesters, if no additional
                        restrictions e.g. courses are defined.
  --basedir BASEDIR     specify the directory where all files will be synced
  --chromiumpath CHROMIUMPATH
                        set the path to a Chrome/Chromium/Edge binary for quiz
                        PDF rendering
  --courseprefix {keep,remove,suffix}
                        handle leading two-character course prefixes in local
                        folder names: 'keep' (default), 'remove', or 'suffix'
  --nolinks             define whether various links in moodle pages should
                        also be inspected e.g. youtube videos, wikipedia
                        articles
  --excludefiletypes EXCLUDEFILETYPES
                        specify whether specific file types should be
                        excluded, comma-separated e.g. "mp4,mkv"
  --excludefiles EXCLUDEFILES
                        exclude specific files using comma-separated patterns
                        e.g. "*.bak,*.tmp"
  --excludelinks EXCLUDELINKS
                        exclude discovered links using comma-separated URL
                        patterns
  --alloweddomains ALLOWEDDOMAINS
                        only keep discovered links on these comma-separated
                        domains
  --excludesections EXCLUDESECTIONS
                        exclude Moodle sections by comma-separated names, ids
                        or patterns
  --excludemodules EXCLUDEMODULES
                        exclude Moodle modules by comma-separated names, ids,
                        types, URLs or patterns
  --quiz {off,html,pdf,both}
                        save quiz review attempts as 'off', 'html', 'pdf', or
                        'both'
  --updatefiles         define whether modified files with the same name/path
                        should be redownloaded
  --updatefilesconflict {rename,keep,overwrite}
                        define how to handle locally modified files when
                        updating: 'rename' (default) moves the old file aside,
                        'keep' skips the update, 'overwrite' replaces the
                        local file
  -v, --verbose         show information useful for debugging
```

Configuration helpers are available as subcommands:

```bash
syncmymoodle config check --config config.toml
syncmymoodle config migrate --input config.json
```

`config check` validates a configuration file and reports invalid values or
likely misspelled keys. `config migrate` converts a legacy JSON configuration
file to TOML. Use `--output` to choose a target path and `--force` to overwrite
an existing TOML file.

### Configuration file

Copy `config.toml.example` to `config.toml` in your current directory or to
`~/.config/syncmymoodle/config.toml` if you wish to configure `syncmymoodle`
user-wide. The TOML example groups related settings into tables such as
`[auth]`, `[paths]`, `[courses]`, `[downloads]`, `[links]`, `[skip_rules]` and
`[modules]`.

Legacy `config.json` files are still supported, but `syncmymoodle` will warn
when loading one and point to `syncmymoodle config migrate`.

Here's an overview of the available options with some additional remarks as to
what each configuration does. The JSON-like shape below mirrors the same
individual keys accepted in TOML tables and legacy JSON configs:

```js
{
    "selected_courses": [], // Only the specified courses (e.g. ["https://moodle.rwth-aachen.de/course/view.php?id=XXXXX"], separated using commas) will be synced
    "skip_courses": [], // Exclude the specified courses. `selected_courses` overrides this option.
    "only_sync_semester": [], // Only the specified semesters (e.g. ["23ss", "22ws"]) will be synced. `selected_courses` overrides this option.
    "user": "", // RWTH SSO username
    "password": "", // RWTH SSO password
    "totp": "", // RWTH SSO TOTP "Serial Number", format: TOTP0000000A, see https://idm.rwth-aachen.de/selfservice/MFATokenManager
    "totpsecret": "", // The TOTP Secret for your TOTP generator (optional)
    "basedir": "./", // The base directory where all your files will be synced to
    "course_prefix_handling": "suffix", // How to handle local course folders starting with a two-character prefix like "(VO) ": "keep" (backwards-compatible default), "remove", or "suffix" (recommended)
    "cookie_file": "./session", // The location of the session/cookie file, which can be used instead of a password.
    "chromium_path": "", // Optional path to a Chrome/Chromium/Edge binary for quiz PDF rendering. Leave empty to auto-detect.
    "use_secret_service": false, // Use the system keyring (see README), instead of a password.
    "secret_service_store_totp_secret": false, // Store the TOTP secret in the system keyring.
    "no_links": false, // Skip links embedded in pages. Warning: This *will* prevent Onlycast videos from being downloaded.
    "used_modules": { // Disable downloading certain modules.
        "assign": true, // Assignments
        "resource": true, // Resources
        "url": {
            "youtube": true, // Include YouTube Links/Embeds
            "opencast": true, // Include Opencast Links/Embeds
            "sciebo": true, // Include Sciebo Links/Embeds
            "quiz": "html" // Save quiz review attempts: "off", "html" (self-contained snapshot, default), "pdf" (browser-rendered PDF), or "both"
        },
        "folder": true // Include folders
    },
    "exclude_filetypes": [], // Exclude specific filetypes (e.g. ["mp4", "mkv"]) to disable downloading most videos
    "exclude_files": [], // Exclude specific files using UNIX filename pattern matching (e.g. "Lecture{video,zoom}*.{mp4,mkv}")
    "exclude_links": [], // Exclude specific links using UNIX pattern matching (e.g. ["*tooltask.igm.rwth-aachen.de/hinge*"])
    "allowed_domains": [], // Optional allowlist for discovered http(s) links. If set, links outside these domains are skipped (e.g. ["moodle.rwth-aachen.de", "rwth-aachen.sciebo.de"])
    "exclude_sections": [], // Exclude sections by name or id using pattern matching. Can also be an object keyed by course id, e.g. {"13489": ["Week 1"]}
    "exclude_modules": [], // Exclude modules by name, type, id or Moodle module URL. Can also be an object keyed by course id
    "update_files": true, // If true, existing files are redownloaded only when Moodle/Sciebo report that they were modified (based on timemodified and checksums).
    "update_files_conflict": "rename" // How to handle locally modified files when a newer version is available on Moodle/Sciebo: "rename" (default, move to <name>.syncconflict.<hash>), "keep" (skip update), or "overwrite" (!!DANGEROUS!! replaces the local file, you may lose any files you edited/changed!).
}
```

`course_prefix_handling` controls local course folder names that start with a
prefix of exactly two characters in parentheses, followed by a space. For
example, `(VO) Analysis` stays unchanged with `keep`, becomes `Analysis` with
`remove`, and becomes `Analysis (VO)` with `suffix`. If not set, the default
is `keep` for backwards compatibility, however `suffix` is recommended.
`remove` can create folder-name conflicts when multiple course types share
the same title; syncMyMoodle resolves those by adding a stable suffix to the
conflicting folders.

`exclude_sections` skips complete Moodle course sections, i.e. top-level
topic/week blocks such as `General`, `Week 1` or `Exercise Sheets`. Matching a
section skips all modules, files and links inside it.

`exclude_modules` skips individual Moodle activities/resources inside a
section, such as one file resource, folder, assignment, URL, page, quiz or
Opencast/LTI item. It can match the module name, Moodle type (`resource`,
`folder`, `assign`, `url`, `page`, `lti`, ...), id or Moodle module URL.

`exclude_sections` and `exclude_modules` can be either a global list or an
object keyed by Moodle course id. In per-course objects, `*` can be used for
rules that apply to every course.

Command line arguments have a higher priority than configuration files.
You can override any of the options that you have configured in the file
using command line arguments.

syncMyMoodle stores per-course metadata in a hidden `.syncmymoodle_cache` file
inside each synced course directory. Delete that file to force a fresh metadata
cache for a course.

Quiz review attempts are saved as self-contained HTML snapshots by default
(`url.quiz = "html"`). The snapshot inlines same-origin Moodle assets and strips
network-bearing content so it remains readable offline and does not contact
Moodle when opened later. Set `url.quiz` to `"off"` to disable quiz snapshots.

PDF rendering is separate and opt-in: set `url.quiz` to `"pdf"` or `"both"` to
render snapshots with a locally installed Chrome, Chromium or Edge browser. This
uses the browser's built-in headless PDF output, which avoids the old
`pdfkit`/`wkhtmltopdf` renderer and its security issues, but syncMyMoodle does
not launch a browser unless you explicitly choose one of the PDF modes. The
browser is auto-detected on PATH and in the usual macOS/Windows install
locations, or you can point `chromium_path` at a specific binary. When no
browser is found, the HTML snapshot is kept as a fallback so nothing is lost.

### TOTP

From the RWTH IDM service you will get a TOTP secret which will be used to
generate OTP tokens. The serial number of the TOTP, which can be seen in the 
[RWTH IDM Token Manager](https://idm.rwth-aachen.de/selfservice/MFATokenManager),
has to be provided using the `--totp` option or the JSON entry of the same name.
It usually has the format `TOTP12345678`.

The TOTP secret can be specified using the `--totpsecret` option or the JSON 
entry of the same name. It can be found in the `otpauth://` link in the secret
argument.

## Keyring Integration

You are advised to install and use the optional Keyring integration
to store your password securely if your system supports it, see the 
[projects page](https://github.com/jaraco/keyring) for all supported systems.

If you have a compatible keyring installed, you can store your RWTH SSO 
credentials in it and use it with *syncMyMoodle*, which can be particularly 
useful if you do not like storing your passwords in plain text files.

To do that, you will have to install *syncMyMoodle* with an extra `keyring`
argument:

```bash
pip3 install syncmymoodle[keyring]  # when installing from PyPi
# or
pip3 install .[keyring]  # when installing manually
```

You will be asked for your password and TOTP secret when using 
*syncMyMoodle* for the first time, which you can supply as a parameter or 
in the configuration file.

If everything went alright, you won't need to enter your password again
in the future, as it will be obtained automatically and securely from
the Secret Service Integration.


## Maintenance

Information to create releases and publish them can be found in `docs/releasing.md`.
