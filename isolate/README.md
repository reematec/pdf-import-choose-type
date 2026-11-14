# Isolate (Inkscape extension)

Work on one object or clip group without the rest of the drawing in the way.

- **Extensions > Arrange > Isolate > Isolate selection** fades everything except the
  selected objects to a quarter of their opacity and locks it, so clicks only reach the
  selection, and you still see the rest of the design for reference.
- **Extensions > Arrange > Isolate > End isolation** restores each object's exact opacity
  and lock state.

Both can be undone with Ctrl+Z. The faded objects are marked in the file
(`data-isolate`), so End isolation also works after saving and reopening.
Hidden objects are left alone.

Separate from the PDF import extension; install it on its own.

## Install

- Windows: double-click `install.cmd`, then restart Inkscape.
- Linux: run `sh isolate/install.sh`, then restart Inkscape.

## Use

1. Select the clip group (or any objects).
2. Isolate selection.
3. Double-click into the group and edit. Double-click an empty spot to leave the group.
4. End isolation.

## Keyboard shortcuts and toolbar buttons

With Inkscape closed, `add_shortcuts_and_buttons.py` adds Ctrl+Alt+I (Isolate selection),
Ctrl+Alt+Shift+I (End isolation) and two buttons on the Select tool's bar:

- Windows: `"C:\Program Files\Inkscape\bin\python.exe" isolate\add_shortcuts_and_buttons.py`
- Linux: `python3 isolate/add_shortcuts_and_buttons.py`. Inkscape 1.2 and older build that
  toolbar in code, so only the shortcuts are added there. For an extracted AppImage, pass
  its toolbar file: `python3 isolate/add_shortcuts_and_buttons.py squashfs-root/usr/share/inkscape/ui/toolbar-select.ui`

Changed files are backed up with the ending `.before-isolate`. Or set shortcuts by hand:
Edit > Preferences > Interface > Keyboard, search for "Isolate selection" and "End isolation",
click the Shortcut column and press the keys you want.

Tested on Windows (Inkscape 1.4) and Linux (Ubuntu 24.04: Inkscape 1.2 and the 1.4 AppImage).
