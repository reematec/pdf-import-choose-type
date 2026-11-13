@echo off
rem Installs (or updates) the "JFIF image" input extension for Inkscape.
rem Double-click this file, then restart Inkscape.
set "DEST=%APPDATA%\inkscape\extensions\jfif_import"
set "SRC=%~dp0"
if not exist "%SRC%jfif_import.inx" (
  echo Could not find the extension files. Keep this installer in the "jfif-import" folder of the download.
  pause
  exit /b 1
)
if not exist "%DEST%" mkdir "%DEST%"
copy /y "%SRC%jfif_import.inx" "%DEST%\" >nul
copy /y "%SRC%jfif_import.py" "%DEST%\" >nul
echo Installed to %DEST%
echo Restart Inkscape to use it.
pause
