@echo off
REM =========================================================================
REM MCA Document Agent - launcher for Windows Task Scheduler.
REM Point a Task Scheduler action at THIS file (see README.md "Scheduling").
REM =========================================================================

REM Move to the folder this .bat file lives in, so it works regardless of
REM whatever "Start in" directory Task Scheduler uses.
cd /d "%~dp0"

REM Activate the virtual environment (created during setup — see README.md)
call venv\Scripts\activate.bat

REM Run one full pass of the agent
python -m src.main

REM Exit code is preserved for Task Scheduler to see success/failure
exit /b %ERRORLEVEL%
