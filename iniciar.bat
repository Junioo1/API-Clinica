@echo off
cd /d "%~dp0"
if not exist venv python -m venv venv
call venv\Scripts\activate
pip install -q -r requirements.txt
start "" http://127.0.0.1:8080/
uvicorn main:app --port 8080
pause
