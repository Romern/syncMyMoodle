# syncMyMoodle
Synchronization client for RWTH Moodle  
Downloads all lecture material including embedded YouTube and OpenCast videos, but not E-Tests or forum threads.

# How to use
Intially you need to install the requirements (bs4, requests and youtube-dl):
```
pip3 install -r requirements.txt
```
You also need to install tkinter, on arch this can be done by installing the ``tk`` package.  
  
Copy ``config.json.example`` to ``config.json`` and adjust the settings, most notably the ``user`` and ``password`` (the credentials you use in the RWTH SSO).  

Now you just need to run
```
python3 syncMyMoodle.py
```

And your courses will be synced into the ``basedir`` you specified (default is the current directory). Your cookies will be stored in a session file.

# GUI
Run 
```
python3 syncMyMoodleGUI.py
```
for the GUI.
