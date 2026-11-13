#!/usr/bin/env python3
"""Opens a .jfif file (an ordinary JPEG) in Inkscape, the same way Inkscape opens a .jpg.

The image is embedded at its real size: its DPI from the file, or 96 DPI (one pixel
per px) if the file has none. Photos marked as rotated (EXIF orientation) are turned
upright and embedded as PNG, as Inkscape does for .jpg. Otherwise the JPEG data is
embedded unchanged.

Command line: python jfif_import.py in.jfif > out.svg
"""
import base64
import io
import sys

try:
    from PIL import Image, ImageOps
except ImportError:
    raise SystemExit('This needs Python with Pillow. On Ubuntu or Debian: sudo apt install python3-pil')


def fmt(v):
    return ('%.4f' % v).rstrip('0').rstrip('.')


def convert(path):
    with open(path, 'rb') as f:
        data = f.read()
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:
        raise SystemExit('Could not read this image: %s' % e)

    # DPI from the JFIF header only (unit 1 = per inch, 2 = per cm), as Inkscape does;
    # Pillow's 'dpi' can come from EXIF instead, which Inkscape ignores.
    unit, density = im.info.get('jfif_unit'), im.info.get('jfif_density') or (0, 0)
    scale = {1: 1.0, 2: 2.54}.get(unit)
    dx, dy = (float(d) * scale if scale and d and float(d) * scale > 1 else 96.0 for d in density[:2])

    mime = 'image/jpeg' if im.format == 'JPEG' else 'image/png'
    orientation = im.getexif().get(0x0112, 1)
    if orientation not in (1, None) or im.format != 'JPEG':
        im = ImageOps.exif_transpose(im)
        if orientation in (5, 6, 7, 8):
            dx, dy = dy, dx
        buf = io.BytesIO()
        im.save(buf, 'PNG')
        data, mime = buf.getvalue(), 'image/png'

    w, h = im.size[0] * 96.0 / dx, im.size[1] * 96.0 / dy
    href = 'data:%s;base64,%s' % (mime, base64.b64encode(data).decode('ascii'))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
            '<svg version="1.1" width="{w}" height="{h}" viewBox="0 0 {w} {h}"\n'
            '   xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"\n'
            '   xmlns:xlink="http://www.w3.org/1999/xlink"\n'
            '   xmlns="http://www.w3.org/2000/svg">\n'
            '  <g inkscape:groupmode="layer" inkscape:label="Image" id="g1">\n'
            '    <image width="{w}" height="{h}" preserveAspectRatio="none"\n'
            '       xlink:href="{href}" id="image1" />\n'
            '  </g>\n'
            '</svg>\n').format(w=fmt(w), h=fmt(h), href=href)


def main():
    files = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not files:
        raise SystemExit('Usage: jfif_import.py in.jfif')
    out = sys.stdout.buffer if hasattr(sys.stdout, 'buffer') else sys.stdout
    out.write(convert(files[-1]).encode('utf-8'))


if __name__ == '__main__':
    main()
