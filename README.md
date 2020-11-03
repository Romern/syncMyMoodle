# syncMyMoodle
Synchronization client for RWTH Moodle
Downloads all lecture material including embedded YouTube and OpenCast videos, but not E-Tests or forum threads.

# How to use
Intially you need to install the requirements (bs4, requests, tqdm and youtube-dl):
```bash
pip3 install -r requirements.txt
```

Copy ``config.json.example`` to ``config.json`` and adjust the settings:

```js
{
    "selected_courses": [], //Only these courses will be synced, of the form "https://moodle.rwth-aachen.de/course/view.php?id=XXXXX"
    "only_sync_semester": [], //Only these semesters will be synced, of the form 20ws (only used if selected_courses is empty)
    "user": "", //Your RWTH SSO username
    "password": "", //Your RWTH SSO password
    "basedir": "./", //The base directory where all files will be synced to
    "cookie_file": "./session" //The location of the cookie file
}
```

Now you just need to run
```bash
./syncMyMoodle.py
```

And your courses will be synced into the ``basedir`` you specified (default is the current directory). Your cookies will be stored in a session file.
