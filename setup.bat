@echo off
setlocal enabledelayedexpansion
title Recall - first-time setup

REM ===========================================================================
REM  Recall - first-time setup
REM
REM  Run this once. It creates a private Python environment inside this folder
REM  and installs the pieces Recall needs. It changes nothing else on your
REM  computer, and it never touches your Outlook files.
REM
REM  If something cannot be installed, this script SAYS SO and carries on, then
REM  the check at the end tells you exactly what will and will not work.
REM ===========================================================================

cd /d "%~dp0"

echo.
echo ===========================================================================
echo   Recall - first-time setup
echo ===========================================================================
echo.
echo   This folder:  %CD%
echo.

REM --- 1. Find Python -------------------------------------------------------

set "PY_CMD="
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=py -3"
)
if not defined PY_CMD (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY_CMD=python"
    )
)

if not defined PY_CMD (
    echo   [STOP] Python 3.11 or newer was not found on this computer.
    echo.
    echo   What to do:
    echo     1. Go to  https://www.python.org/downloads/windows/
    echo     2. Download the latest "Windows installer (64-bit)".
    echo     3. Run it. On the FIRST screen, tick "Add python.exe to PATH".
    echo     4. Finish the install, then run this setup file again.
    echo.
    pause
    exit /b 1
)

echo   [1 of 5] Found Python:
%PY_CMD% --version
echo.

REM --- 2. Create the private environment -------------------------------------

if exist ".venv\Scripts\python.exe" (
    echo   [2 of 5] Private Python environment already exists - reusing it.
) else (
    echo   [2 of 5] Creating a private Python environment in .venv ...
    %PY_CMD% -m venv .venv
    if errorlevel 1 (
        echo.
        echo   [STOP] The environment could not be created.
        echo   This usually means Python was installed for "all users" and this
        echo   window does not have permission. Try again from a Command Prompt
        echo   opened with "Run as administrator".
        echo.
        pause
        exit /b 1
    )
)
echo.

set "VPY=.venv\Scripts\python.exe"

REM --- 3. Update pip ---------------------------------------------------------

echo   [3 of 5] Updating the installer ...
"%VPY%" -m pip install --quiet --upgrade pip setuptools wheel
if errorlevel 1 (
    echo   ...could not update the installer. Carrying on anyway.
)
echo.

REM --- 4. Install the libraries ---------------------------------------------

echo   [4 of 5] Installing the libraries Recall needs.
echo            This can take several minutes. Please wait.
echo.
"%VPY%" -m pip install -r requirements.txt
set "PIP_RESULT=%errorlevel%"

if not "%PIP_RESULT%"=="0" (
    echo.
    echo   ---------------------------------------------------------------
    echo   Not everything installed cleanly.
    echo.
    echo   The usual cause is libpff-python, which reads .pst and .ost files
    echo   quickly. It is published as source code rather than a ready-made
    echo   package, so it needs a C compiler that this computer may not have.
    echo.
    echo   Recall still works without it: Microsoft Outlook itself is used to
    echo   read .pst and .ost files instead. That is slower, but it reads more
    echo   file types correctly, and it needs no compiler.
    echo.
    echo   Installing the rest, one by one, so a single failure does not stop
    echo   everything...
    echo   ---------------------------------------------------------------
    echo.
    for /f "usebackq tokens=* delims=" %%P in (`"%VPY%" -c "import re,sys;[print(l.split('#')[0].strip()) for l in open('requirements.txt',encoding='utf-8') if l.split('#')[0].strip()]"`) do (
        echo   installing %%P ...
        "%VPY%" -m pip install --quiet "%%P" || echo     ^>^> %%P could not be installed.
    )
)
echo.

REM --- 5. Check the result --------------------------------------------------

echo   [5 of 5] Checking what works ...
echo.
"%VPY%" -m recall doctor
set "DOCTOR_RESULT=%errorlevel%"

echo.
echo ===========================================================================
if "%DOCTOR_RESULT%"=="0" (
    echo   Setup finished. Everything Recall needs is present.
    echo.
    echo   Next: double-click  start.bat  to open Recall.
) else (
    echo   Setup finished, but some checks failed.
    echo.
    echo   Read the FAIL lines above - each one says what to do. Fix those,
    echo   then run this setup file again.
)
echo ===========================================================================
echo.
pause
endlocal
