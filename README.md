# PDF Import (Choose type)

An Inkscape 1.x extension that lets you choose how a PDF is imported.

| PDF type | Use it for | What it does |
|---|---|---|
| **Vector artwork (keep clips)** | Artwork from CorelDRAW or Illustrator | Keeps each PowerClip or clipping mask as one clipped group. Colors are converted with the PDF's own color profile. Text can be imported as outlines or as editable text. |
| **Standard PDF (Inkscape default)** | Documents, spreadsheets, anything that is not artwork | Runs Inkscape's own PDF import with its default settings. The result is the same as Inkscape's import without this extension. |

Inkscape's own PDF import stays available as well: in File > Open or File > Import, choose "Portable Document Format" in the file-type list.

## Install

### Windows

1. Download or clone this repository.
2. Double-click `install\install.cmd`.
3. Restart Inkscape.

The extension is copied to `%APPDATA%\inkscape\extensions\pdf_import_choose_type`.

### Linux

1. Download or clone this repository.
2. In a terminal, run `sh install/install.sh` from the download folder.
3. Restart Inkscape.

The installer copies the extension for each Inkscape it finds (distribution package, AppImage, Flatpak or Snap), into its extensions folder in your home folder. It also checks that Python has Pillow and numpy, and if not, shows the command to install them (on Ubuntu or Debian: `sudo apt install python3-pil python3-numpy`).

With Inkscape 1.2 (for example on Ubuntu 24.04), **Standard PDF** imports one page at a time, the first page you enter. Inkscape 1.3 or later imports several pages. **Vector artwork** imports several pages on any version.

### Manual install (any system)

Copy the contents of the `extension` folder into a folder named `pdf_import_choose_type` inside your Inkscape user extensions folder. Inkscape shows where that is under Edit > Preferences > System > User extensions. Then restart Inkscape.

## Use

Open or import a PDF and choose **PDF Import (Choose type)**. In the dialog, pick the **PDF type**, and the pages to import (`all`, or for example `1,3` or `2-4`).

The **Text** setting applies only to Vector artwork:

- **Outlines** looks exactly like the PDF.
- **Editable text** keeps text as text. The fonts must be installed.

Anything in the PDF that could not be converted exactly is listed in a message after the import.

## Requirements

- Inkscape 1.x (developed on Inkscape 1.4).
- Python with Pillow, including its color management module (`PIL.ImageCms`), and numpy. Inkscape for Windows includes them. On Linux, install them from your distribution (see above).
- pypdf, fontTools and typing_extensions come with the extension, in `extension/vendor`. Python 3.8 or later.

## Run from the command line

```
python extension/pdf_import_choose_type.py [--type=vector|standard] [--text=outlines|editable]
                                           [--pages=all|1,3] [--output=out.svg] in.pdf
```

On Windows, use Inkscape's Python: `"C:\Program Files\Inkscape\bin\python.exe"`.

## Tests

`tests/test_compare.py` converts PDFs with Vector artwork, renders both the PDF and the result, and reports how many pixels differ. It writes a diff image for each page. Test PDFs are not included; pass your own.

```
python tests/test_compare.py <out_dir> <pdf>...
```

## Bundled software

- [pypdf](https://github.com/py-pdf/pypdf) 5.9.0, BSD license, see `extension/vendor/pypdf-5.9.0.dist-info/licenses`.
- [fontTools](https://github.com/fonttools/fonttools) 4.57.0, MIT license, see `extension/vendor/fonttools-4.57.0.dist-info/licenses`.
- [typing_extensions](https://github.com/python/typing_extensions) 4.13.2, PSF license, see `extension/vendor/typing_extensions-4.13.2.dist-info/licenses`. pypdf needs it on Python older than 3.11.

These are the newest versions that still run on Python 3.8, which the official Inkscape 1.4 AppImage uses.

No color profile is bundled. For PDFs that do not name their own CMYK profile, the extension uses the system's profile (Microsoft's SWOP profile on Windows, Ghostscript's on Linux), or one you put in `extension/profiles/`. See [extension/profiles/README.md](extension/profiles/README.md).

## Extra: icon-only dock tabs

`inkscape-setup/` has a small script, separate from the PDF import, that makes the tabs in Inkscape's dock show only icons so the dock stays narrow. See [inkscape-setup/README.md](inkscape-setup/README.md).

## Extra: open .jfif images

`jfif-import/` is a separate small extension that lets Inkscape open `.jfif` files (JPEGs that Windows browsers save with that ending). See [jfif-import/README.md](jfif-import/README.md).

## License

MIT, see [LICENSE](LICENSE).
