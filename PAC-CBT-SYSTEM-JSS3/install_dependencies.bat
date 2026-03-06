@echo off
echo Installing CBT System Dependencies...
python -m ensurepip --upgrade
python -m pip install --upgrade pip
python -m pip install flask python-docx reportlab werkzeug flask-login flask-sqlalchemy
echo.
echo Installation Complete.
pause