@echo off
REM Manual cycle launcher. Double-click or invoke from CLI.
REM Automatic scheduling is intentionally NOT registered — see README.

cd /d "%~dp0"
if not exist ".venv\Scripts\activate.bat" (
    echo .venv not found. Run: python -m venv .venv ^&^& pip install -r requirements.txt
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python -m src cycle %*
set EXIT=%errorlevel%

if not "%EXIT%"=="0" (
    echo.
    echo Cycle failed with exit code %EXIT%
)
echo.
pause
exit /b %EXIT%
