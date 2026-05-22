@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    set "PIP=.venv\Scripts\pip.exe"
) else (
    set "PY=python"
    set "PIP=pip"
)

"%PIP%" install -q pyinstaller pyperclip uiautomation keyboard
"%PY%" -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name "Caption Copier" ^
    --hidden-import uiautomation ^
    --hidden-import comtypes ^
    --hidden-import comtypes.client ^
    --hidden-import keyboard ^
    --hidden-import pyperclip ^
    simple_caption_tool.py

if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo.
echo Built: dist\Caption Copier.exe
endlocal
