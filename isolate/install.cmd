@echo off
rem Installs (or updates) the "Isolate" extension for Inkscape.
rem Double-click this file, then restart Inkscape.
set "DEST=%APPDATA%\inkscape\extensions\isolate"
set "SRC=%~dp0"
if not exist "%SRC%isolate.py" (
  echo Could not find the extension files. Keep this installer in the "isolate" folder of the download.
  pause
  exit /b 1
)
if not exist "%DEST%" mkdir "%DEST%"
copy /y "%SRC%isolate.py" "%DEST%\" >nul
copy /y "%SRC%isolate_selection.inx" "%DEST%\" >nul
copy /y "%SRC%isolate_end.inx" "%DEST%\" >nul
echo Installed to %DEST%
echo Restart Inkscape to use it.
pause
