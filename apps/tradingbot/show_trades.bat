@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
call "%~dp0_bootstrap.bat"
if not defined PYEXE exit /b 1
%PYEXE% run.py report
echo.
pause
