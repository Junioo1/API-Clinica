#!/bin/sh
cd "$(dirname "$0")"
[ -d venv ] || python3 -m venv venv
. venv/bin/activate
pip install -q -r requirements.txt
echo "Abra http://127.0.0.1:8080/"
uvicorn main:app --port 8080
