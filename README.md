# syncMyMoodle

Synchronization client for RWTH Moodle

Downloads the following materials:

* Assignment files, submissions and feedback
* Resource files
* Urls: OpenCast, Youtube and Sciebo videos/files, and all other non HTML files
* Folders
* Quizzes (**Disabled by default**)
* Pages and Labels: Embedded Opencast and Youtube Videos

## Installation

This software requires **Python version >= 3.6**.

### Installation using `pip` (recommended)

The simples way to setup this app is to install it using pip.
To isolate dependencies it is advised to use a virtual environment.
For more information take a look at
[the guide from the Python website](https://packaging.python.org/guides/installing-using-pip-and-virtual-environments/#creating-a-virtual-environment):

```bash
python3 -m venv .venv
source .venv/bin/activate  # bash/zsh, for other shells view the docs
pip3 install syncmymoodle
```

### Manual installation

If you like living on the bleeding edge you can also clone or download the source directly
and setup everything manually.
syncMyMoodle requires some dependencies which can be installed using `pip`
or your distro's package manager (`apt`, `dnf`, `pacman`, etc.).

To install the requirements using pip execute the following command from the repository root.

```bash
# It is best to run this in a virtual environment.
# For more information see the section above.
pip3 install .
```

### Optional steps

It is recommended to also install and use the optional
[FreeDesktop.org Secret Service integration](#freedesktoporg-secret-service-integration)
to store your password securely if your system supports it - if you're on Linux, it probably does!

## Configuration

Copy `config.json.example` or the following text (minus the comments) to `config.json` in your current directory
or alternatively to `~/.config/syncmymoodle/config.json` for configuring `syncmymoodle` user-wide.
Afterwards you can adjust the settings:

```js
{
    "selected_courses": [], //Only these courses will be synced, of the form "https://moodle.rwth-aachen.de/course/view.php?id=XXXXX" (if empty, all courses will be synced)
    "skip_courses": [], //Skip these courses
    "only_sync_semester": [], //Only these semesters will be synced, of the form 20ws (only used if selected_courses is empty, if empty all semesters will be synced)
    "user": "", //Your RWTH SSO username
    "password": "", //Your RWTH SSO password (not needed if you use secret service)
    "basedir": "./", //The base directory where all files will be synced to
    "cookie_file": "./session", //The location of the cookie file,
    "use_secret_service": false, //Use the secret service integration (requires the secretstorage pip module)
    "no_links": false, //Skip links embedded in pages. This would disable OpenCast links for example
    "used_modules": { //Disable downloading certain modules
        "assign": true, //Assignments
        "resource": true, //Resources
        "url": {
            "youtube": true, //Youtube Links/Embeds
            "opencast": true, //Opencast Links/Embeds
            "sciebo": true, //Sciebo Links/Embeds
            "quiz": false //Quiz Links
        },
        "folder": true, //Folders
    },
    "exclude_filetypes": [] //Exclude specific filetypes, e.g. ["mp4","mkv"] do disable downloading most videos
}
```

And your courses will be synced into the `basedir` you specified (default is the current directory).
Your cookies will be stored in a session file.

## CLI usage

Run:

```bash
source .venv/bin/activate  # if you installed using virtual environment
python3 -m syncmymoodle
deactivate  # leave virtual environment
```

You can override the fields in the config file by using command line arguments:

```bash
usage: syncMyMoodle.py [-h] [--secretservice] [--user USER] [--password PASSWORD] [--config CONFIG]
                       [--cookiefile COOKIEFILE] [--courses COURSES] [--skipcourses SKIPCOURSES]
                       [--semester SEMESTER] [--basedir BASEDIR] [--nolinks]

Synchronization client for RWTH Moodle. All optional arguments override those in config.json.

optional arguments:
  -h, --help            show this help message and exit
  --secretservice       Use FreeDesktop.org Secret Service as storage/retrival for username/passwords.
  --user USER           Your RWTH SSO username
  --password PASSWORD   Your RWTH SSO password
  --config CONFIG       The path to the config file
  --cookiefile COOKIEFILE
                        The location of the cookie file
  --courses COURSES     Only these courses will be synced (comma seperated links) (if empty, all courses will be
                        synced)
  --skipcourses SKIPCOURSES
                        These courses will NOT be synced (comma seperated links)
  --semester SEMESTER   Only these semesters will be synced, of the form 20ws (comma seperated) (only used if
                        [courses] is empty, if empty all semesters will be synced)
  --basedir BASEDIR     The base directory where all files will be synced to
  --nolinks             Wether to not inspect links embedded in pages
```

## FreeDesktop.org Secret Service integration

If you have a FreeDesktop.org Secret Service integration compatible keyring installed,
you can save your RWTH SSO credentials in it.
You need to install syncMyMoodle with the `keyring` extra installed:

```bash
pip3 install syncmymoodle[keyring]  # when installing from PyPi
# or
pip3 install .[keyring]  # when installing manually
```

After you removed your password from the config file (delete the whole line in config.json),
you will be prompted for your password when syncing for the first time.
In subsequent runs, the credentials will be obtained automatically.
