@echo off
setlocal

cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHON_EXE=.venv\Scripts\python.exe

if not exist "%PYTHON_EXE%" (
    echo Creating local Python environment...
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        where python >nul 2>nul
        if errorlevel 1 (
            echo Python is not installed or is not in PATH.
            echo Install Python 3, then run this file again.
            pause
            exit /b 1
        )
        python -m venv .venv
    )
)

if not exist "%PYTHON_EXE%" (
    echo Could not create .venv.
    pause
    exit /b 1
)

echo Installing requirements...
"%PYTHON_EXE%" -m pip install --upgrade pip
if errorlevel 1 (
    pause
    exit /b 1
)

"%PYTHON_EXE%" -m pip install -r requirements.txt
if errorlevel 1 (
    pause
    exit /b 1
)

"%PYTHON_EXE%" local_server.py

pause
