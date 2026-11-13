# JFIF image (Inkscape input extension)

Lets Inkscape open and import `.jfif` files. A `.jfif` is an ordinary JPEG (Windows makes
browsers save JPEGs with this ending), but Inkscape only recognizes `.jpg`, `.jpeg` and `.jpe`.

The image is embedded at the same size Inkscape uses for a `.jpg`: the DPI from the file, or
96 DPI if it has none. Photos marked as rotated are turned upright. Unlike Inkscape's own JPEG
import, no dialog is shown and the image is always embedded, never linked.

Separate from the PDF import extension; install it on its own.

## Install

- Windows: double-click `install.cmd`, then restart Inkscape.
- Linux: run `sh jfif-import/install.sh`, then restart Inkscape. Needs Python with Pillow
  (`sudo apt install python3-pil` on Ubuntu or Debian).

Manual: copy `jfif_import.inx` and `jfif_import.py` into a folder named `jfif_import` in your
Inkscape user extensions folder (Edit > Preferences > System > User extensions).

## Use

File > Open or File > Import, and pick the `.jfif` file.
