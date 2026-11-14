"""Accuracy test for flatten/ ("Flatten clips and overlaps").

Runs flatten.py on an SVG (all top-level objects of the first layer, or the given ids),
renders the original and the result with Inkscape without anti-aliasing, and reports:
  - differing pixels (1-pixel edge noise is normal: Inkscape draws strokes and fills
    with slightly different pixel rules),
  - pixels in differences wider than 3 pixels (should be 0: real geometry errors),
  - the same per top-level clip group (panel).

Usage (Windows, Inkscape's Python; Inkscape closed or not, it doesn't matter):
  set PYTHONPATH=C:\\Program Files\\Inkscape\\share\\inkscape\\extensions
  "C:\\Program Files\\Inkscape\\bin\\python.exe" tests\\test_flatten.py <out_dir> <file.svg> [id ...]

Writes <out_dir>/<name>.flat.svg and the renders. Use one file at a time.
"""
import os
import subprocess
import sys
import time

import inkex
import numpy as np
from PIL import Image, ImageFilter

Image.MAX_IMAGE_PIXELS = None
HERE = os.path.dirname(os.path.abspath(__file__))
FLATTEN = os.path.join(HERE, '..', 'flatten', 'flatten.py')
INKSCAPE = os.environ.get('INKSCAPE') or (r'C:\Program Files\Inkscape\bin\inkscape.com'
                                          if sys.platform == 'win32' else 'inkscape')
WIDTH = 8000


def render(svg, png):
    subprocess.run([INKSCAPE, svg, '--export-type=png', '--export-area-page', '--export-width=%d' % WIDTH,
                    '--export-png-antialias=0', '--export-background=white', '--export-filename=' + png],
                   check=True, capture_output=True)


def main():
    out, src, ids = sys.argv[1], sys.argv[2], sys.argv[3:]
    os.makedirs(out, exist_ok=True)
    base = os.path.splitext(os.path.basename(src))[0].replace(' ', '_')
    doc = inkex.load_svg(src).getroot()
    layer = next(e for e in doc if isinstance(e, inkex.Group))
    if not ids:
        ids = [c.get_id() for c in layer if isinstance(c, inkex.ShapeElement)]
        src_ids = os.path.join(out, base + '.ids.svg')  # get_id() may have added ids
        with open(src_ids, 'wb') as f:
            f.write(doc.tostring())
        src = src_ids
    flat = os.path.join(out, base + '.flat.svg')
    t = time.time()
    with open(flat, 'wb') as f:
        res = subprocess.run([sys.executable, FLATTEN] + ['--id=' + i for i in ids] + [src],
                             stdout=f, stderr=subprocess.PIPE)
    print('flatten: %d objects, %.1f s, exit %d' % (len(ids), time.time() - t, res.returncode))
    if res.stderr.strip():
        print(res.stderr.decode('utf-8', 'replace').strip())
    ref_png, flat_png = os.path.join(out, base + '.ref.png'), os.path.join(out, base + '.flat.png')
    render(src, ref_png)
    render(flat, flat_png)
    a = np.asarray(Image.open(ref_png).convert('RGB')).astype(np.int16)
    b = np.asarray(Image.open(flat_png).convert('RGB')).astype(np.int16)
    diff = Image.fromarray(((np.abs(a - b).max(axis=2) > 0) * 255).astype('uint8'))
    wide = np.asarray(diff.filter(ImageFilter.MinFilter(3))) > 0
    print('page: differing %d px, wider than 3 px: %d' % ((np.asarray(diff) > 0).sum(), wide.sum()))
    k = WIDTH / doc.viewbox_width if hasattr(doc, 'viewbox_width') else WIDTH / float(doc.get_viewbox()[2])
    for c in layer:
        if c.get('clip-path'):
            bb = c.bounding_box(layer.composed_transform())
            x0, y0, x1, y1 = [max(int(v * k), 0) for v in (bb.left, bb.top, bb.right, bb.bottom)]
            print('  %-10s wider than 3 px: %d' % (c.get_id(), wide[y0:y1, x0:x1].sum()))


if __name__ == '__main__':
    main()
