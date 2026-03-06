# PAC COLLEGE CBT — Multi-Port Ultimate UI

This build runs **Student**, **Teacher**, and **Admin** portals on **different ports**, sharing one database.

## Ports (default)
- Student Portal:  http://SERVER_IP:5000   (student_app.py)
- Teacher Portal:  http://SERVER_IP:5001   (teacher_app.py)
- Admin Portal:    http://SERVER_IP:5002   (admin_app.py)

## Install
pip install flask werkzeug

## Run
Open 3 terminals:
python student_app.py
python teacher_app.py
python admin_app.py

Or run on Windows:
run_all_windows.bat

## Default Admin
admin / admin123

## LAN Access (local host for other systems)
Run on the server PC. Students connect using:
http://<SERVER_IP>:5000

## UI Features
- Premium blue/gold/pink theme
- Dark mode toggle (🌓) with localStorage memory
- Animated CTA buttons
- Exam progress bar + question navigator (offcanvas)
- Autosave toast feedback
- Responsive, modern layout

## Proctoring
Screen capture + fullscreen required before starting. Tab switching is detected and logged.


## Teacher: Upload Questions from MS Word (.docx)
- Install dependency: `pip install python-docx`
- In Teacher Portal → Questions, use **Upload Questions (Microsoft Word)**.
- If any question is wrongly formatted, the system will **notify the teacher** and skip only that question so the teacher can **add it manually**.
- Template file: `static/Question_Upload_Template.docx`
"# PAC-CBT-SYSTEM" 
