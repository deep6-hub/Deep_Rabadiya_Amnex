@echo off
setlocal
cd /d "%~dp0"

REM Each team member selects their own service-account JSON path in the web UI.
REM The key's project_id is used by default; no shared project/key is hard-coded.

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Python virtual environment not found.
    echo Create it with: py -3.11 -m venv .venv
    echo Then run: .venv\Scripts\python -m pip install -r requirements.txt
    pause
    exit /b 1
)

REM Local Processing works without Earth Engine credentials. S1/S2 require a valid key selected in the web UI.

call ".venv\Scripts\activate.bat"
echo ==============================================
echo Sentinel-1 VH - Direct Earth Engine to Local
echo Authentication: Google Cloud service account
echo CRS: EPSG:32642
echo Google Drive is not required.
echo ==============================================
echo.
python -m uvicorn app:app --host 127.0.0.1 --port 8000
pause
