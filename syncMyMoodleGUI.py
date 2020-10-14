from tkinter import Label, Entry, Button, Tk, Text, mainloop, BooleanVar, Checkbutton, filedialog, Frame, Listbox, Scrollbar
import sys
import json
import os.path
from concurrent import futures
import syncMyMoodle

thread_pool_executor = futures.ThreadPoolExecutor(max_workers=1)

master = Tk()

# Getting the config

if not os.path.exists("config.json"):
	config = {
		"selected_courses": [],
		"onlyfetchcurrentsemester": True,
		"enableExperimentalCategories": False,
		"user": "",
		"password": "",
		"basedir": "./",
		"cookie_file": "./session",
		"replace_spaces_by_underscores": True
	}
else:
	config = json.load(open("config.json"))

# Creating the input objects

username_entry = Entry(master)
password_entry = Entry(master, show="*")
base_folder_frame = Frame(master)
base_folder_entry = Entry(base_folder_frame)
textbox = Text(master)
only_cur_semester_checkbox = BooleanVar()
replace_spaces_by_underscores_checkbox = BooleanVar()
enable_experimental_categories_checkbox = BooleanVar()
course_list_box = Listbox(master, selectmode="multiple")

# Inserting the config

username_entry.insert(0,config["user"])
password_entry.insert(0,config["password"])
base_folder_entry.insert(0,config["basedir"])
only_cur_semester_checkbox.set(config["onlyfetchcurrentsemester"])
replace_spaces_by_underscores_checkbox.set(config["replace_spaces_by_underscores"])
enable_experimental_categories_checkbox.set(config["enableExperimentalCategories"])

# Configure the grid

username_entry.grid(row=0, column=1,sticky="E")
password_entry.grid(row=1, column=1,sticky="E")
base_folder_frame.grid(row=2, column=1, sticky="E")
base_folder_entry.grid(column=0, row=0)
textbox.grid(row=6, column=0, columnspan=2, sticky="WE")
course_list_box.grid(row=3, column=1, rowspan=2, sticky="WE")

scrollbar = Scrollbar(master)
scrollbar.grid(row=3, column=2, rowspan=2, sticky="NS")
course_list_box.config(yscrollcommand = scrollbar.set)
scrollbar.config(command = course_list_box.yview)

# Redirect Stdout to the textbox for simple feedback from syncMyMoodle

class StdoutRedirector(object):
	def __init__(self,text_widget):
		self.text_space = text_widget

	def write(self,string):
		self.text_space.insert('end', string)
		self.text_space.see('end')

	def flush(self):
		pass

sys.stdout = StdoutRedirector(textbox)


sync_button = None
get_courses_button = None
courses = []
smm = None

# Labels

Label(master, text="Username").grid(row=0,sticky="w")
Label(master, text="Password").grid(row=1,sticky="w")
Label(master, text="Base directory").grid(row=2,sticky="w")

# Browse Button

def ask_base_directory():
	foldername = filedialog.askdirectory()
	if foldername:
		base_folder_entry.delete(0, "end")
		base_folder_entry.insert(0, foldername)

Button(base_folder_frame, text='Browse', command=ask_base_directory).grid(column=1, row=0, sticky="E")

# Retrive courses button

def get_courses():
	global smm
	global courses
	config_new = {
		"selected_courses": config["selected_courses"],
		"onlyfetchcurrentsemester": only_cur_semester_checkbox.get(),
		"enableExperimentalCategories": enable_experimental_categories_checkbox.get(),
		"user": username_entry.get(),
		"password": password_entry.get(),
		"basedir": base_folder_entry.get(),
		"cookie_file": config["cookie_file"],
		"replace_spaces_by_underscores": replace_spaces_by_underscores_checkbox.get()
	}
	get_courses_button['state'] = 'disabled'

	smm = syncMyMoodle.SyncMyMoodle(config_new)
	smm.login()
	smm.get_courses(getAllCourses=True)
	courses = smm.courses

	for i,c in enumerate(smm.courses):
		course_list_box.insert(i, f"{c[2]} ({c[1]})")
		if not config["onlyfetchcurrentsemester"] or c[0] in config["selected_courses"] or (config["onlyfetchcurrentsemester"] and c[1] == smm.max_semester[1]):
			course_list_box.selection_set(i)

get_courses_button = Button(master, text='Retrive courses', command=get_courses)
get_courses_button.grid(column=1, row=5, sticky="E")

# Config checkboxes

Checkbutton(master, text="Only fetch current semester", variable=only_cur_semester_checkbox).grid(row=3,sticky="w")
Checkbutton(master, text="Replace spaces by _", variable=replace_spaces_by_underscores_checkbox).grid(row=4,sticky="w")
Checkbutton(master, text="Enable experimental categories", variable=enable_experimental_categories_checkbox).grid(row=5,sticky="w")

# Quit button

def quit():
	global config
	config = {
		"selected_courses": config["selected_courses"],
		"onlyfetchcurrentsemester": only_cur_semester_checkbox.get(),
		"enableExperimentalCategories": enable_experimental_categories_checkbox.get(),
		"user": username_entry.get(),
		"password": password_entry.get(),
		"basedir": base_folder_entry.get(),
		"cookie_file": config["cookie_file"],
		"replace_spaces_by_underscores": replace_spaces_by_underscores_checkbox.get()
	}
	json.dump(config,open("config.json","w"))
	master.quit()

Button(master, text='Quit', command=quit).grid(row=7, column=0,sticky="w")

# Sync Button

def sync():
	global config
	# Write config to file
	config = {
		"selected_courses": config["selected_courses"],
		"onlyfetchcurrentsemester": only_cur_semester_checkbox.get(),
		"enableExperimentalCategories": enable_experimental_categories_checkbox.get(),
		"user": username_entry.get(),
		"password": password_entry.get(),
		"basedir": base_folder_entry.get(),
		"cookie_file": config["cookie_file"],
		"replace_spaces_by_underscores": replace_spaces_by_underscores_checkbox.get()
	}
	json.dump(config,open("config.json","w"))
	# Start syncMyMoodle and disable button
	sync_button['state'] = 'disabled'
	get_courses_button['state'] = 'disabled'
	thread_pool_executor.submit(syncMyMoodle_task)

def syncMyMoodle_task():
	global smm
	if not smm:
		smm = syncMyMoodle.SyncMyMoodle(config)
		smm.login()
		smm.get_courses()
	else:
		smm.config = config
		smm.courses = [courses[i] for i in course_list_box.curselection()]
	smm.sync()
	print("Syncing finished!")
	sync_button['state'] = 'normal'

sync_button = Button(master, text='Sync My Moodle!', command=sync)
sync_button.grid(row=7, column=1,sticky="E")

#

master.columnconfigure(1, weight=1)

mainloop()
