#!/usr/bin/env sh
# One-time setup on macOS/Linux. Run from the project folder.
set -e
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
echo "Setup complete. Start with: . .venv/bin/activate && python manage.py runserver"
