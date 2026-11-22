@echo off
rem Double-click: start the private terminal on http://127.0.0.1:8811 and open it in the browser.
cd /d "%~dp0"
start "" http://127.0.0.1:8811/
".venv\Scripts\python.exe" -X utf8 -W ignore -m rtshf serve
