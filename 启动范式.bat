@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "EMOTION_PSYCHOPY_PY=C:\Program Files\PsychoPy\python.exe"
if exist "%EMOTION_PSYCHOPY_PY%" (
    "%EMOTION_PSYCHOPY_PY%" emotion_paradigm.py
) else (
    python emotion_paradigm.py
)
if errorlevel 1 pause
