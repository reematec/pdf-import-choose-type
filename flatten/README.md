# Flatten clips and overlaps (Inkscape extension)

Turns artwork with clips (CorelDRAW PowerClips, Illustrator clipping masks, also nested)
into flat filled paths that don't overlap, so every spot belongs to exactly one path.
Made for preparing artwork for web configurators and other tools that need clean,
non-overlapping color areas.

**Extensions > Modify Path > Flatten clips and overlaps** replaces each selected object
(for example one panel) with:

- every line converted to a filled shape,
- every clip applied as a real cut,
- the parts hidden under other objects cut away,
- one path per object, labeled with the object's name, so elements with the same color
  can still be recolored separately,
- one path per color for objects inside an inner group (a pattern, such as dots),
- with **Outlines in the color of the surface under them join that surface** (on by
  default): an outline drawn on a surface of its own color becomes part of that surface.
  Example: white rings around dots on a white base recolor together with the base. Turn
  it off to give such outlines their own path. The message lists where it happened.

Curves stay curves.

Gradient fills are kept, and each object with a gradient stays one path. A gradient that
fades from one color to another (in-between stops included) becomes a two-color fade
linked to a shared gradient that holds only those two colors (named, for example,
"fade #201e1e #d2ae6d"). All fades between the same two colors link to the same shared
gradient, so changing gold to red there once recolors every fade, in Inkscape and in a
web page. Fade end colors that are nearly the same across the design become one color;
the message lists them. The fade keeps its position and width; its curve can differ
slightly from the original, by a few color levels out of 255. Radial gradients, gradients
that repeat or reflect, and fades through more than two colors keep their own colors.
A gradient that shows only one color becomes a solid color.

When a PDF draws one shape (for example a blade) as a fade plus a solid end in the fade's
end color, both clipped to the shape, the solid end joins the fade's path, so the shape is
one path. Where the PDF's fade stops a little short of its end color, the small step there
becomes smooth (up to 20 color levels in a strip next to the fade). A second fade between
the same two colors in a different direction (a curved blade drawn as two straight fades)
also joins, so each shape is one path; its own small fade, for example a glow at a tip,
is not kept. The message says how many parts this happened to.

Panel outlines (lines with no fill that follow the selection's outermost clip) stay
lines. Text, images and other objects that can't be converted exactly are put back
unchanged and listed in a message. Undo with Ctrl+Z.

Accuracy: the geometry is computed with [skia-pathops](https://github.com/fonttools/skia-pathops)
(Google Skia's path engine). On test designs the result matches the original drawing
to within a rendering pixel; Inkscape's own Path > Flatten left gaps of up to 0.2 mm.

Separate from the PDF import extension; install it on its own.

Tested on Windows 10 (Inkscape 1.4) and Ubuntu 24.04 (Inkscape 1.2 from apt, and the
Inkscape 1.4 AppImage).

## Requirements

- Windows (64-bit) or Linux (x86-64).
- Windows: Python 3.10 or later from [python.org](https://www.python.org/downloads/),
  installed next to Inkscape. Inkscape's own Python can't load skia-pathops, so the
  geometry step runs in this Python.
- Linux: Python 3.10 or later as `python3` (Ubuntu 22.04 and later have it). Inkscape from
  your distribution uses it directly; with the AppImage, whose own Python is older, the
  geometry step runs in the system's `python3`.
- If the right Python isn't found automatically, set the environment variable
  `FLATTEN_PYTHON` to it.

## Install

- Windows: double-click `install.cmd`, then restart Inkscape.
- Linux: run `sh flatten/install.sh`, then restart Inkscape. It installs for Inkscape from
  your distribution or the AppImage, and for Flatpak and Snap when they are present.

## Limits

- Patterns, and gradients on lines (strokes), are left out (only solid colors are supported).
- Transparency is ignored (treated as fully opaque).
- Dashed lines are converted as solid lines.
- Masks are ignored.

Each of these is listed in the message after flattening when it happens.

## Bundled software

[skia-pathops](https://github.com/fonttools/skia-pathops) 0.9.2 for Windows (64-bit,
`_pathops.pyd` and `skia.dll`) and Linux (x86-64, `_pathops.abi3.so`), BSD license, see
`vendor/skia_pathops-0.9.2.dist-info/licenses/LICENSE`. Each installer copies only the
files for its system.
