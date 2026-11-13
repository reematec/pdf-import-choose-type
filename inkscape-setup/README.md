# Inkscape setup: icon-only dock tabs

Makes the tabs in Inkscape's right-side dock show only icons, so the dock stays narrow.
Works with Inkscape 1.3 and 1.4 on Windows and Linux.

## With the script

1. Close Inkscape (File > Quit).
2. Run one of these in this folder:
   - Windows: `"C:\Program Files\Inkscape\bin\python.exe" dock_icons_only.py 340`
   - Linux: `python3 dock_icons_only.py 340`

   `340` sets the dock width in pixels. Leave it out to keep the current width.
3. Start Inkscape.

The script stops if Inkscape is open, and keeps backups ending in `.before-dock`.
For Flatpak or Snap Inkscape, add `--profile` with its folder, for example
`--profile ~/snap/inkscape/current/.config/inkscape`.

## By hand

1. Edit > Preferences > Interface > Windows.
2. Labels behavior: the option whose tooltip says "Only show dialog icons".
3. Drag the dock narrower.
4. Close Inkscape with **File > Quit**. Inkscape saves settings only when it closes normally.

## Don't

Don't turn off "Save and restore dialogs' status". Inkscape then starts without your
panels, and you have to open each one from the menus every time.

## Undo

Close Inkscape and copy `preferences.xml.before-dock` back over `preferences.xml`
(and `dialogs-state-ex.ini.before-dock` over `dialogs-state-ex.ini`) in the profile folder:
`%APPDATA%\inkscape` on Windows, `~/.config/inkscape` on Linux.
