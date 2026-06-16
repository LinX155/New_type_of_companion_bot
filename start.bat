@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting Companion Bot...
echo Backend will run on http://localhost:8000
echo WebUI will be available at http://localhost:8000
uvicorn app.main:app --host 0.0.0.0 --port 8000
