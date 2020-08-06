# syncMyMoodle
Synchronization client for RWTH Moodle  
Downloads all lecture material including embedded YouTube and OpenCast videos, but not E-Tests or forum threads.

# How to use
Intially you need to install the requirements (bs4, requests and youtube-dl):
```
pip install -r requirements.txt
```
Copy ``config.py.example`` to ``config.py`` and adjust the settings, most notably the ``user`` and ``password`` (the credentials you use in the RWTH SSO).  

Now you just need to run
```
python3 syncMyMoodle.py
```
And your courses will be synced into the ``basedir`` you specified (default is the current directory). Your cookies will be stored in a session file.
