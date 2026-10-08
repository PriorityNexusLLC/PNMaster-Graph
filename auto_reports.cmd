@echo off
rem AUTO-REPORTS: rank everything by number of flags (most first), then draft AND independently review a report
rem for the top 10. Runs minimized in the background; results land in Obsidian under HOOT/Reports/Auto reports.
rem Progress is written to data\auto_reports.log. Change 10 to report on more or fewer.
cd /d "%~dp0"
if "%1"=="run" goto run
start "Priority Nexus auto-reports" /min cmd /c "%~f0" run
echo Auto-reports started in the background (minimized). Results: Obsidian - HOOT/Reports/Auto reports.
exit /b
:run
python -I src\ingest\review_reports.py > data\auto_reports.log 2>&1
python -I src\ingest\auto_reports.py --top 10 >> data\auto_reports.log 2>&1
