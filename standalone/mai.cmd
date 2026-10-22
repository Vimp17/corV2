@echo off
rem MAI Corrosion Control - standalone demo build (no Docker). Usage: mai.cmd help
rem All the work is done by standalone\manage.py with the bundled Python (runtime\python.exe).
setlocal
set "MAI_ROOT=%~dp0"
set "MAI_PYTHON=%MAI_ROOT%runtime\python.exe"
if not exist "%MAI_PYTHON%" (
  echo [MAI] runtime\python.exe not found. Unpack the whole archive and run mai.cmd from its folder.
  exit /b 1
)
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%MAI_PYTHON%" -X utf8 "%MAI_ROOT%standalone\manage.py" %*
exit /b %ERRORLEVEL%
