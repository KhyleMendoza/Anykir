@echo off
cd /d "%~dp0"
pip install -r requirements.txt pyinstaller || exit /b 1
python build_exe.py || exit /b 1
