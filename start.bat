@echo off
setlocal
title Recall

REM ===========================================================================
REM  Recall - start the program
REM
REM  Double-click this file. It opens Recall in your web browser.
REM
REM  Leave this black window open while you use Recall. Closing it stops the
REM  program. Nothing is lost when you close it - everything is saved as it
REM  goes, and it picks up where it left off next time.
REM ===========================================================================

cd /d "%~dp0"

set "VPY=.venv\Scripts\python.exe"

if not exist "%VPY%" (
    REM No private environment: fall back to the system Python, which is fine
    REM if the libraries were installed there.
    where python >nul 2>&1
    if errorlevel 1 (
        echo.
        echo   Recall is not set up yet on this computer.
        echo.
        echo   Double-click  setup.bat  first. It only has to be done once.
        echo.
        pause
        exit /b 1
    )
    set "VPY=python"
    echo   Note: using the system Python, because .venv was not found.
    echo   If anything misbehaves, run setup.bat once.
    echo.
)

echo.
echo ===========================================================================
echo   Recall is starting.
echo.
echo   Your browser will open in a moment. If it does not, open it yourself
echo   and go to:   http://127.0.0.1:8765
echo.
echo   Keep this window open while you use Recall.
echo   To stop:  close this window, or press Ctrl and C together.
echo ===========================================================================
echo.

"%VPY%" -m recall serve --open
set "RESULT=%errorlevel%"

if not "%RESULT%"=="0" (
    echo.
    echo ===========================================================================
    echo   Recall stopped with a problem. The message above says what happened.
    echo.
    echo   A full record is in:  workdir\logs\
    echo   To check what this computer is missing, run:  setup.bat
    echo ===========================================================================
    echo.
    pause
)
endlocal
