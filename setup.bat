@echo off
REM One-time setup on Windows. Run from the project folder.
python -m venv .venv
call .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
echo.
echo Setup complete. Start the system with run.bat
