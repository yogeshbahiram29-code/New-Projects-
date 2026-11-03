@echo off
REM FREE signal mode on a Windows PC/laptop.
REM Windows Task Scheduler -> Create Basic Task -> Weekly, Mon-Fri, 4:45 PM -> Start a program: this file.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
python -m swingbot signals >> state\signals.log 2>&1
