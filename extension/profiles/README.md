# CMYK color profile (optional)

Put one CMYK profile (`.icc` or `.icm`) in this folder to choose how CMYK colors
are converted when a PDF does not name its own profile. If there are several,
the first by file name is used.

No profile is shipped. Without one, the extension uses the system's profile:
Microsoft's SWOP profile on Windows (`RSWOP.icm`), Ghostscript's default CMYK
profile on Linux. If neither is found, CMYK is converted with a simple formula
and the import says so.

For the colors CorelDRAW and Illustrator show by default, use Adobe's
"U.S. Web Coated (SWOP) v2" (`USWebCoatedSWOP.icc`) from Adobe's free ICC
profile download, under Adobe's license.
