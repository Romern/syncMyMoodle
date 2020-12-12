# syncMyMoodle
Synchronization client for RWTH Moodle

Downloads the following materials:
* Assignment files, submissions and feedback
* Resource files
* Urls: OpenCast, Youtube and Sciebo videos/files, and all other non HTML files
* Folders
* Pages and Labels: Embedded Opencast and Youtube Videos

# How to use
Intially you need to install the requirements:
```bash
pip3 install -r requirements.txt
```

Copy ``config.json.example`` to ``config.json`` and adjust the settings:

```js
{
    "selected_courses": [], //Only these courses will be synced, of the form "https://moodle.rwth-aachen.de/course/view.php?id=XXXXX" (if empty, all courses will be synced)
    "only_sync_semester": [], //Only these semesters will be synced, of the form 20ws (only used if selected_courses is empty, if empty all semesters will be synced)
    "user": "", //Your RWTH SSO username
    "password": "", //Your RWTH SSO password
    "basedir": "./", //The base directory where all files will be synced to
    "cookie_file": "./session", //The location of the cookie file,
    "login_at_start": false, //Login automatically when starting the GUI
    "synchronize_at_start": false, //Synchronize automatically when starting the GUI
    "close_after_synchronization": false //Close automatically after synchronizing when starting the GUI
}
```



Now you just need to run
```bash
./syncMyMoodle.py
```

And your courses will be synced into the ``basedir`` you specified (default is the current directory). Your cookies will be stored in a session file.

# How to use GUI
<p float="left">
	<img src="https://user-images.githubusercontent.com/8593000/100927817-ae381c00-34e5-11eb-9ee8-9a1042b05760.png" width="50%" />
	<img src="https://user-images.githubusercontent.com/8593000/100927819-af694900-34e5-11eb-9219-3ba0ded57ad4.png" width="50%" />
</p>

You need to install the requirements as before:
```bash
pip3 install -r requirements.txt
```
Now run
```bash
./gui.py
```

Before syncing, you have to edit the Settings. You have to set your RWTH Login and maybe change your Download Directory. To choose the Semester you have to edit the ``config.json`` manually.
When you are logged in you have to press ``Update`` in the File Browser Tab and then ``Download`` to Download the Files.

Choosing which Files you want to download, is currently not implemented yet.

# CLI
You can override the fields in the config file by using command line arguments:

```
usage: syncMyMoodle.py [-h] [--secretservice] [--user USER] [--password PASSWORD] [--config CONFIG] [--cookiefile COOKIEFILE] [--courses COURSES] [--semester SEMESTER] [--basedir BASEDIR]

Synchronization client for RWTH Moodle. All optional arguments override those in config.json.

optional arguments:
  -h, --help            show this help message and exit
  --secretservice       Use FreeDesktop.org Secret Service as storage/retrival for username/passwords.
  --user USER           Your RWTH SSO username
  --password PASSWORD   Your RWTH SSO password
  --config CONFIG       The path to the config file
  --cookiefile COOKIEFILE
                        The location of the cookie file
  --courses COURSES     Only these courses will be synced (comma seperated links) (if empty, all courses will be synced)
  --semester SEMESTER   Only these semesters will be synced, of the form 20ws (comma seperated) (only used if [courses] is empty, if empty all semesters will be synced)
  --basedir BASEDIR     The base directory where all files will be synced to
```

# FreeDesktop.org Secret Service integration
If you have a FreeDesktop.org Secret Service integration compatible keyring installed, you can save you RWTH SSO credentials in it.
You need to have the python package ``secretstorage`` installed:
```bash
pip3 install secretstorage
```
After you removed your password from the config file (delete the whole line in config.json), you need to specify your password once, either using ``--password``, or you will be promted.
In subsequent runs, the credentials will be obtained automatically.
