# syncMyMoodle

Synchronization client for RWTH Moodle

Downloads the following materials:

* Assignment files, submissions and feedback
* Resource files
* URLs: OpenCast, Youtube and Sciebo videos/files, and all other non HTML files
* Folders
* Quizzes (**Disabled by default**)
* Pages and Labels: Embedded Opencast and Youtube Videos

## Installation

This software requires **Python 3.6 or higher**.

### Using `pip` (recommended)

The simplest way to install *syncMyMoodle* is using pip.

You're advised to use a virtual environment to make sure that
its dependencies can't do anything evil on your machine.

Please consult
[the guide from the Python website](https://packaging.python.org/guides/installing-using-pip-and-virtual-environments/#creating-a-virtual-environment):
for more information.

Or, you can just use the following commands:

```bash
python3 -m venv .venv
source .venv/bin/activate  # bash/zsh, for other shells view the docs
pip3 install syncmymoodle
```

### Manual installation

If you are living on the bleeding edge, you can also download the source
code directly and build everything by yourself.

*syncMyMoodle* [dependencies] can be installed using `pip`
or your distro's package manager (`apt`, `dnf`, `pacman`, etc.).

To install the requirements using pip execute the following command from the repository root.

```bash
# It is best to run this in a virtual environment.
# For more information see the section above.
pip3 install .
```

## Configuration

Copy `config.json.example` or the following text (minus the comments) to `config.json` in your current directory
or to `~/.config/syncmymoodle/config.json` if you wish to configure `syncmymoodle` user-wide.

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

You can also use CLI parameters instead. This is covered extensively
in the following section.

Your courses will be synced in the `basedir` path that you specified.
If you haven't done that, the current directory that you
are running the script from will also be the directory where
your files will be downloaded *by default*.

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

You are advised to install and use the optional
[FreeDesktop.org Secret Service integration](#freedesktoporg-secret-service-integration)
to store your password securely if your system supports it - if you're on a modern
Linux desktop-oriented distribution, it most probably does!

If you have a FreeDesktop.org Secret Service integration compatible keyring
installed, you can store your RWTH SSO credentials in it and use it with
*syncMyMoodle*, which can be particularly useful if you do not like storing
your passwords in plain text files.

To do that, you will have to install *syncMyMoodle* with an extra `keyring`
argument:

```bash
pip3 install syncmymoodle[keyring]  # when installing from PyPi
# or
pip3 install .[keyring]  # when installing manually
```

You will be asked for your password when using *syncMyMoodle* for the first
time, which you can supply as a parameter or in the configuration file.

Your password will be stored and obtained automatically in the future.

