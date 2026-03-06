@echo off
start "Student Portal" cmd /k py student_app.py
start "Teacher Portal" cmd /k py teacher_app.py
start "Admin Portal" cmd /k py admin_app.py
echo Started all portals on ports 5000/5001/5002
