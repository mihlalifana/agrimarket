@echo off
call .venv\Scripts\activate
if exist email_settings.bat call email_settings.bat
python manage.py run_daily_jobs
python manage.py runserver
