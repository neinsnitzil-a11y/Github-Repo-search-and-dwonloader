@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 "src\github_gui.py"
) else (
  where python >nul 2>&1
  if %errorlevel%==0 (
    python "src\github_gui.py"
  ) else (
    echo Python 3.10 or newer is required.
    echo Download Python from https://www.python.org/downloads/windows/
    pause
    exit /b 1
  )
)
if errorlevel 1 (
  echo.
  echo The application exited with an error. Make sure Tkinter is installed.
  pause
)
