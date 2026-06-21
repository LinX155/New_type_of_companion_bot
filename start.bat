@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "ROOT=%CD%"
set "REQ_FILE=%ROOT%\requirements.txt"

echo Starting Companion Bot...
echo Project root: %ROOT%
echo.

call :find_venv
if not defined VENV_PY (
    echo [1/3] No project virtual environment found. Creating .venv...
    call :find_python
    if errorlevel 1 goto :no_python
    !BOOTSTRAP_PYTHON! -m venv "%ROOT%\.venv"
    if errorlevel 1 goto :venv_create_failed
    set "VENV_DIR=%ROOT%\.venv"
    set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
) else (
    echo [1/3] Using virtual environment: %VENV_DIR%
)

if not exist "%VENV_PY%" goto :venv_missing
call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 goto :venv_activate_failed

if not exist "%REQ_FILE%" goto :requirements_missing
set "REQ_MARKER=%VENV_DIR%\.requirements.sha256"
set "REQ_HASH="
for /f "usebackq delims=" %%H in (`python -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest().upper())" "%REQ_FILE%"`) do set "REQ_HASH=%%H"
if not defined REQ_HASH goto :requirements_hash_failed

set "INSTALLED_HASH="
if exist "%REQ_MARKER%" set /p INSTALLED_HASH=<"%REQ_MARKER%"
if /I "%INSTALLED_HASH%"=="%REQ_HASH%" (
    echo [2/3] requirements.txt unchanged. Skipping dependency install.
) else (
    echo [2/3] Installing Python dependencies from requirements.txt...
    "%VENV_PY%" -m pip install --upgrade pip
    if errorlevel 1 goto :pip_install_failed
    "%VENV_PY%" -m pip install -r "%REQ_FILE%"
    if errorlevel 1 goto :pip_install_failed
    >"%REQ_MARKER%" echo %REQ_HASH%
)

echo [3/3] Starting backend...
echo Backend will run on http://localhost:8000
echo WebUI will be available at http://localhost:8000
if /I "%COMPANION_BOT_SETUP_ONLY%"=="1" (
    echo Setup-only mode enabled. Backend start skipped.
    exit /b 0
)
call :release_backend_port
if errorlevel 1 goto :port_in_use
"%VENV_PY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Backend exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%

:release_backend_port
set "BACKEND_PORT=8000"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "$port = [int]$env:BACKEND_PORT;" ^
  "$root = [System.IO.Path]::GetFullPath($env:ROOT).TrimEnd('\');" ^
  "$listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique);" ^
  "foreach ($processId in $listeners) {" ^
  "  if (-not $processId) { continue }" ^
  "  $proc = Get-CimInstance Win32_Process -Filter \"ProcessId=$processId\" -ErrorAction SilentlyContinue;" ^
  "  $cmd = if ($proc) { [string]$proc.CommandLine } else { '' };" ^
  "  $name = if ($proc) { [string]$proc.Name } else { '' };" ^
  "  $belongsToProject = $cmd.Contains($root) -or ($cmd.Contains('uvicorn') -and $cmd.Contains('app.main:app'));" ^
  "  $isPythonBackend = ($name -match '^(python|pythonw|py)\.exe$') -and $belongsToProject;" ^
  "  if (-not $isPythonBackend) {" ^
  "    Write-Host ('ERROR: Port {0} is already used by PID {1}: {2}' -f $port, $processId, $cmd);" ^
  "    exit 2;" ^
  "  }" ^
  "  Write-Host ('Port {0} is still held by previous backend PID {1}. Stopping it...' -f $port, $processId);" ^
  "  Stop-Process -Id $processId -Force -ErrorAction Stop;" ^
  "}" ^
  "for ($i = 0; $i -lt 30; $i++) {" ^
  "  $stillListening = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue);" ^
  "  if ($stillListening.Count -eq 0) { exit 0 }" ^
  "  Start-Sleep -Milliseconds 200;" ^
  "}" ^
  "Write-Host ('ERROR: Port {0} is still not released after waiting.' -f $port);" ^
  "exit 3;"
exit /b %ERRORLEVEL%

:find_venv
set "VENV_DIR="
set "VENV_PY="
if exist "%ROOT%\.venv\Scripts\python.exe" (
    set "VENV_DIR=%ROOT%\.venv"
    set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
    exit /b 0
)
if exist "%ROOT%\venv\Scripts\python.exe" (
    set "VENV_DIR=%ROOT%\venv"
    set "VENV_PY=%ROOT%\venv\Scripts\python.exe"
    exit /b 0
)
for /f "usebackq delims=" %%D in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath $env:ROOT -Directory -Force | Where-Object { (Test-Path (Join-Path $_.FullName 'pyvenv.cfg')) -and (Test-Path (Join-Path $_.FullName 'Scripts\python.exe')) } | Select-Object -First 1 -ExpandProperty FullName"`) do (
    set "VENV_DIR=%%D"
    set "VENV_PY=%%D\Scripts\python.exe"
    exit /b 0
)
exit /b 0

:find_python
set "BOOTSTRAP_PYTHON="
where py >nul 2>nul
if not errorlevel 1 (
    py -3.12 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "BOOTSTRAP_PYTHON=py -3.12"
        exit /b 0
    )
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "BOOTSTRAP_PYTHON=py -3"
        exit /b 0
    )
)
where python >nul 2>nul
if not errorlevel 1 (
    set "BOOTSTRAP_PYTHON=python"
    exit /b 0
)
exit /b 1

:no_python
echo ERROR: Python was not found. Install Python 3.12+ and reopen this script.
pause
exit /b 1

:venv_create_failed
echo ERROR: Failed to create .venv.
pause
exit /b 1

:venv_missing
echo ERROR: Virtual environment python.exe was not found: %VENV_PY%
pause
exit /b 1

:venv_activate_failed
echo ERROR: Failed to activate virtual environment: %VENV_DIR%
pause
exit /b 1

:requirements_missing
echo ERROR: requirements.txt was not found: %REQ_FILE%
pause
exit /b 1

:requirements_hash_failed
echo ERROR: Failed to calculate requirements.txt hash.
pause
exit /b 1

:pip_install_failed
echo ERROR: Failed to install Python dependencies.
pause
exit /b 1

:port_in_use
echo ERROR: Port 8000 is already in use and could not be released safely.
echo Close the program using port 8000, then run start.bat again.
pause
exit /b 1
