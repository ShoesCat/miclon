@echo off
setlocal
cd /d "%~dp0"

py -m PyInstaller --clean --noconfirm V6.spec
if errorlevel 1 exit /b %ERRORLEVEL%

echo Executable created: dist\Miclon.exe
