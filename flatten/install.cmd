@echo off
rem Installs (or updates) the "Flatten clips and overlaps" extension for Inkscape.
rem Double-click this file, then restart Inkscape.
set "DEST=%APPDATA%\inkscape\extensions\flatten"
set "SRC=%~dp0"
if not exist "%SRC%flatten.py" (
  echo Could not find the extension files. Keep this installer in the "flatten" folder of the download.
  pause
  exit /b 1
)
if exist "%DEST%" rmdir /s /q "%DEST%"
mkdir "%DEST%"
copy /y "%SRC%flatten.py" "%DEST%\" >nul
copy /y "%SRC%flatten_worker.py" "%DEST%\" >nul
copy /y "%SRC%gradients.py" "%DEST%\" >nul
copy /y "%SRC%flatten.inx" "%DEST%\" >nul
xcopy /e /i /q /y "%SRC%vendor" "%DEST%\vendor" >nul
rem The Linux library (.so) is not needed on Windows.
del /q "%DEST%\vendor\pathops\*.so" >nul 2>nul
echo Installed to %DEST%
where py >nul 2>nul && (py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && goto ok)
where python >nul 2>nul && (python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && goto ok)
echo.
echo This extension also needs Python 3.10 or later from python.org.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
:ok
echo Restart Inkscape to use it.
pause
