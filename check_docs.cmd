@echo off
rem CHECK FILES: reads the helper Inbox (Obsidian: HOOT/Inbox) and the investigation documents, looks everyone up in
rem official records, adds what's confirmed to the master graph, and writes check reports (Obsidian: HOOT/Reports).
rem Safe to run any time; files that haven't changed since last time are skipped.
cd /d "%~dp0"
for /f "delims=" %%v in ('python -I src\engine\paths.py') do set VAULT=%%v
set R=%VAULT%\HOOT\Reports
set D=%USERPROFILE%\Downloads

echo [1/4] Helper Inbox: new or changed board lists...
python -I src\ingest\inbox.py
echo [2/4] Investigation documents...
echo [3/4] Adding confirmed connections to the master graph...
python -I src\ingest\autopilot.py
python -I src\ingest\identity_sync.py
echo [4/4] Check reports...
python -I src\ingest\inbox.py --report
call "%VAULT%\.hoot\hoot.cmd" loops
echo.
echo Done. Results: Obsidian - HOOT/Reports ("Claims check - Inbox - ..." for each Inbox file).
if not "%NOPAUSE%"=="1" pause
