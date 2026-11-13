@echo off
rem Installs (or updates) the "PDF Import (Choose type)" extension for Inkscape.
rem Double-click this file, then restart Inkscape.
set "DEST=%APPDATA%\inkscape\extensions\pdf_import_choose_type"
set "SRC=%~dp0..\extension"
if not exist "%SRC%\pdf_import_choose_type.inx" (
  echo Could not find the extension files. Keep this installer in the "install" folder of the download.
  pause
  exit /b 1
)
if exist "%DEST%" rmdir /s /q "%DEST%"
xcopy /e /i /q /y "%SRC%" "%DEST%" >nul
echo Installed to %DEST%
echo Restart Inkscape to use it.
pause
