# PDF Import (Choose type)

An Inkscape 1.x input extension for PDFs. The import dialog has a **PDF type** dropdown, and each type runs its own converter. It will be released publicly for everyone, under the MIT license.

The owner develops on Windows and tests Linux on an Ubuntu machine over SSH. Keep it generic: no company names or branding in the UI, IDs or file names.

## PDF types

| Value | Label in the dialog | What it does |
|---|---|---|
| `vector` | Vector artwork (keep clips) | Our converter in `pdf_import_choose_type.py`. It turns each CorelDRAW PowerClip or Illustrator clipping mask into one clipped group. Inkscape's own import copies the clip onto every object, and drops nested clips and some embedded-font text. Also handles colors through ICC profiles, and text as outlines or editable. |
| `standard` | Standard PDF (Inkscape default) | Inkscape's own PDF import, unchanged, with default settings. It must stay byte-identical to Inkscape's import, with no bugs. |

- **Labels are generic** because the converter is for any artwork (balls, gloves, boxing, garments), not one product. Don't name a type after a product.
- **More types come from the owner.** They will say which PDFs need which converter. To add one: add an `<option>` to the `type` param in the `.inx`, add its settings under its own header label, and add a branch in `main()`.
- **Inkscape can't hide dialog settings depending on the dropdown.** That's why each type's settings sit under a header label with the type's name.

## Layout

```
extension/   what gets installed (the whole folder is copied)
  pdf_import_choose_type.inx   menu/dialog definition; id pdf_import_choose_type.input
  pdf_import_choose_type.py    converter + main(); also runs from the command line
  vendor/                      pypdf 5.9.0, fontTools 4.57.0, typing_extensions 4.13.2 (pure Python, with licenses)
  profiles/                    optional CMYK profile; none is shipped (see below)
install/install.cmd            Windows: copies extension/ to %APPDATA%\inkscape\extensions\pdf_import_choose_type
tests/test_compare.py          converts PDFs and pixel-compares against Inkscape's render
inkscape-setup/                separate from the extension: dock_icons_only.py sets icon-only dock tabs
                               and dock width in an Inkscape profile (Windows and Linux); not installed
jfif-import/                   separate input extension: opens .jfif (a JPEG) like Inkscape opens .jpg
                               (JFIF-header DPI else 96, EXIF rotation -> PNG); own install.cmd / install.sh
```

The extension runs on Inkscape's bundled Python on Windows (`C:\Program Files\Inkscape\bin\python.exe`, which has Pillow with ImageCms, and numpy). The owner has Inkscape 1.4.0. Upgrading needs admin rights, and the winget upgrade failed.

## Standard import: how it works and what must not break

- **How it runs:** a child process, `inkscape <pdf> --pages=… --export-type=svg --export-filename=-`, with `INKSCAPE_PROFILE_DIR` set to an empty temp folder. The empty folder means the child loads no user extensions, so it can't pick ours for the PDF.
- **What happened without it:** the child picked ours, read the saved dialog settings and silently produced a Vector artwork result.
- **The guard:** the child runs with the env var `PDF_IMPORT_CHOOSE_TYPE_STANDARD` set. `main()` refuses to run when it's set, so a bypassed import stops with an error instead of converting.
- **How to check it:** compare against Inkscape's own import with an empty profile folder. The outputs must be byte-identical (`cmp`).
- **Import order:** Inkscape seems to pick between input extensions for the same file type by ID order. An earlier ID starting with `com.` sorted before `org.inkscape.input.pdf` and took over every PDF. The current ID sorts after it. Keep that in mind before changing the ID.

## Colors

- **Order the converter tries:** spot colors (Lab alternates) go straight to sRGB. CMYK uses the PDF's own profile, then its OutputIntent, then the default profile. Rendering intent is relative colorimetric with black-point compensation.
- **Default CMYK profile:** the first `.icc` or `.icm` in `extension/profiles/`, then the system profile (Windows `RSWOP.icm`, Ghostscript's `default_cmyk.icc` on Linux), then a simple formula plus a warning in the import message.
- **Adobe's `USWebCoatedSWOP.icc` is never committed or shipped.** Debian treats Adobe's ICC profiles as non-free, and `.gitignore` excludes `extension/profiles/*.icc`. The owner's local copy has it, so their results match CorelDRAW and Illustrator.

## Testing (important)

- **The owner tests layouts in Inkscape themselves.** Don't run `tests/test_compare.py` over all PDFs unless asked. A full run once made Inkscape render a 43 MB SVG, Windows grew its swap file on a nearly full C: drive, and the owner was alarmed. Test one PDF, page 1, unless told otherwise.
- **Test PDFs are client artwork and never go into this repo.** The samples are in `design/`, which `.gitignore` excludes along with `*.pdf`, `*.png` and `*.svg`. Don't name sample files in anything that gets committed. The owner will say which file is the main check (0.000% pixel difference, 54 clip groups, and Standard byte-identical to Inkscape's own import) and which ones depend on the default CMYK profile.
- **After changing the extension,** run `install\install.cmd`, or copy into the installed folder, and tell the owner to restart Inkscape.

## Linux

- **`install/install.sh`** installs for each Inkscape it finds: distro package or AppImage (`~/.config/inkscape/extensions`), Flatpak (`~/.var/app/org.inkscape.Inkscape/config/inkscape/extensions`) and Snap (`~/snap/inkscape/current/.config/inkscape/extensions`). It checks that Python has Pillow with ImageCms and numpy, and prints the install command for the distro if not.
- **Tested** on Ubuntu 24.04 with its Inkscape 1.2.2 and Pillow 10.2 (no desktop, over SSH): Vector 0.000% on the main sample, Standard byte-identical, installer cases, Inkscape's own import still wins over ours.
- **Inkscape 1.2 has no `--pages`**; it imports one page with `--pdf-page=N`. Standard tries `--pages` first (no cost on 1.3+) and falls back to `--pdf-page` with the first requested page, plus a note if more were asked for. The owner chose this. `tests/test_compare.py` does the same.
- **Pillow before 10.3 has no `ImageCms.Flags`.** The code reads the flag and intent from whichever API exists. Don't use `ImageCms.Flags` or `ImageCms.Intent` directly.
- **Python 3.8 is the floor.** The official Inkscape 1.4 AppImage runs extensions with Python 3.8. The vendored libraries are the newest that support it; pypdf 7 / fontTools 4.68 crashed there. Don't upgrade them past 3.8 support, and don't use Python 3.9+ features in our code. Changing them gave byte-identical output on every sample.
- **AppImage tested** (Inkscape 1.4, extracted in `~/inkscape-test` on the test machine): Vector and Standard both pass.
- **Not tested yet:** the dialog in a Linux desktop, Flatpak and Snap.

## Working with the owner

- **Talk about changes to the import dialog or behavior first,** with options, before building them.
- **Ask one question at a time,** each with its own background and an example.
- **Don't commit or push unless asked.** Publishing to the inkscape.org gallery is the owner's decision.
- **Copy** is US English, plain and specific.
