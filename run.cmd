@echo off
cd /d "%~dp0"
python -I src\server.py %*
