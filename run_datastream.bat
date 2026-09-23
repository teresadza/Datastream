@echo off
REM Run the Datastream extraction on demand. Double-click this file, or run it
REM from a command prompt. It uses config.ini in this same folder.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py datastream_extract.py
) else (
    python datastream_extract.py
)
echo.
echo Done. Output is in the "output" subfolder. Press any key to close.
pause >nul
