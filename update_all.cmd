@echo off
rem ONE CLICK: pull new SEC filings (and 10 new frontier companies), find closed loops,
rem check your documents, then let the autopilot add the SEC-backed connections to the graph.
rem Runs on this computer only. Can take an hour or more; you can leave it running.
cd /d "%~dp0"
for /f "delims=" %%v in ('python -I src\engine\paths.py') do set VAULT=%%v
set HOOT=%VAULT%\.hoot\hoot.cmd
set R=%VAULT%\HOOT\Reports
set D=%USERPROFILE%\Downloads

echo [1/6] Pulling SEC filings for everything on the Watchlist...
call "%HOOT%" refresh
echo [2/6] Career sweep: every company each person has ever filed for...
call "%HOOT%" careers
echo       People sweep: an official record for everyone named in your files, then all their companies...
call "%HOOT%" people --limit 300
echo       Same-person check: fold name variants (Margo = Margaret) into one record, two facts required...
call "%HOOT%" dedupe
echo       Helper Inbox (HOOT/Inbox): new or changed board lists...
python -I src\ingest\inbox.py
echo [3/6] Checking your documents...
echo       Private companies (SEC Form D) and nonprofits (IRS Form 990) your documents name...
python -I src\ingest\private_sweep.py
echo [4/6] Finding closed loops and conflict signals...
call "%HOOT%" loops
call "%HOOT%" conflicts
echo [5/6] Autopilot: adding SEC-backed connections to the graph...
python -I src\ingest\autopilot.py
echo       U.S. Senate: official rosters (senators, committee seats) and former senators named in your files...
python -I src\ingest\senate.py
python -I src\ingest\house.py
python -I src\ingest\identity_sync.py
python -I src\ingest\claims_check.py data\staging\claims_senate_119.json --title "Senate 119th Congress"
echo       Lobbying (Senate LDA filings), foreign agents (DOJ FARA), campaign finance (FEC)...
python -I src\ingest\lobbying.py --limit 200
python -I src\ingest\fara.py
python -I src\ingest\campaign_finance.py
python -I src\ingest\former_officials.py
python -I src\ingest\inbox.py --report
echo       Public enforcement records: every name checked against DOJ releases and the HHS-OIG exclusion list (dated stamps)...
python -I src\ingest\enforcement.py
echo [6/6] Governance checks: audit-log fingerprints, today's outside anchor, balance check...
python -I src\ingest\governance_checks.py
echo       Reports: re-check any you edited...
python -I src\ingest\review_reports.py
rem Auto-reports are PAUSED while the report format is being finalized. To turn them back on, remove "rem " below.
rem python -I src\ingest\auto_reports.py --top 10
echo       Rulebook: re-check every official law/regulation link...
python -I src\maintenance\rulebook.py
echo       Backup to the USB drive (skipped if it isn't plugged in)...
python -I src\maintenance\backup_to_usb.py
echo.
echo Done. Reports are in Obsidian under HOOT/Reports. Open the app with run.cmd.
if not "%NOPAUSE%"=="1" pause
