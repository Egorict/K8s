@echo off
rem Find a real Python: py launcher -> standard install -> python from PATH.
rem The Microsoft Store stub in WindowsApps is skipped on purpose.
set "PYEXE="
where py >nul 2>&1 && (py -3 -c "import sys" >nul 2>&1 && set "PYEXE=py -3")
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE (
  for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PYEXE if not "%%~dpP"=="%LOCALAPPDATA%\Microsoft\WindowsApps\" set "PYEXE=%%P"
  )
)
if not defined PYEXE (
  echo Python not found. Install it:  winget install Python.Python.3.12
  pause
  exit /b 1
)
exit /b 0
