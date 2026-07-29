@echo off
setlocal
cd /d "%~dp0"

set "URL=http://127.0.0.1:8501"
set "PY="

rem Prefer the validated project runtime. Do not use the Codex bundled Python.
call :check_python "%~dp0runtime\python\python.exe"
call :check_python "%~dp0.venv\Scripts\python.exe"
call :check_python "%~dp0venv\Scripts\python.exe"
call :check_python "D:\Anaconda3\python.exe"
for /f "delims=" %%P in ('where python 2^>nul') do call :check_python "%%P"

if not defined PY (
  echo [ERROR] No Python environment with Streamlit was found.
  echo.
  echo Install dependencies with:
  echo     D:\Anaconda3\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

if exist "%~dp0portable_data\databackup" set "DATA_ROOT=%~dp0portable_data\databackup"
if not defined DATA_ROOT if exist "%~dp0databackup" set "DATA_ROOT=%~dp0databackup"

rem If the platform is already healthy, only open the page.
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { if ((Invoke-WebRequest -UseBasicParsing -Uri '%URL%/_stcore/health' -TimeoutSec 2).StatusCode -eq 200) { Start-Process '%URL%'; exit 0 } } catch {}; exit 1"
if not errorlevel 1 (
  echo [OK] AI Platform is already running: %URL%
  timeout /t 2 /nobreak >nul
  exit /b 0
)

echo [INFO] Python: %PY%
echo [INFO] Starting AI Platform...

rem Wait for Streamlit to become healthy, then open the default browser.
if not defined PLATFORM_NO_BROWSER start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "$deadline=(Get-Date).AddSeconds(180); do { try { $ready=(Invoke-WebRequest -UseBasicParsing -Uri '%URL%/_stcore/health' -TimeoutSec 2).StatusCode -eq 200 } catch { $ready=$false }; if (-not $ready) { Start-Sleep -Milliseconds 500 } } while (-not $ready -and (Get-Date) -lt $deadline); if ($ready) { Start-Process '%URL%' }"

"%PY%" -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false

echo.
echo [INFO] AI Platform has stopped.
pause
endlocal
exit /b 0

:check_python
if defined PY exit /b 0
if not exist "%~1" exit /b 0
"%~1" -c "import streamlit, pandas, numpy" >nul 2>&1
if errorlevel 1 exit /b 0
set "PY=%~1"
exit /b 0
