# syncMyMoodle
Synchronization client for RWTH Moodle  
Downloads the following materials:  
* Assignment files, submissions and feedback  
* Resource files  
* Urls: OpenCast, Youtube and Sciebo videos/files, and all other non HTML files  
* Folders  
* Pages and Labels: Embedded Opencast and Youtube Videos  

# How to use
Intially you need to install the requirements (bs4, requests, tqdm and youtube-dl):
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
    "enable_download_tracker": true //Enable the download tracker, if enabled files won't be checked on a subsequent sync
}
```

Now you just need to run
```bash
./syncMyMoodle.py
```

And your courses will be synced into the ``basedir`` you specified (default is the current directory). Your cookies will be stored in a session file.  
  
Downloaded files are tracked in ``downloaded_modules.json`` to speed up syncing, so if you need to redownload some files you might want to delete it or disable it by setting ``enable_download_tracker`` to ``false``.
