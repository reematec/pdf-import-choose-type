"""Converts PDFs with pdf_import_choose_type.py and compares each page with a render
of the PDF itself (Inkscape's poppler/cairo import), pixel by pixel.

    Windows: "C:/Program Files/Inkscape/bin/python.exe" tests/test_compare.py <out_dir> <pdf>...
    Linux:   python3 tests/test_compare.py <out_dir> <pdf>...

Set INKSCAPE to the Inkscape program if it is not the default for your system.
Test PDFs are not part of this repository; pass your own.

Writes <out_dir>/<name>[-pN].svg, the two renders and a diff image (changed
pixels in red), and prints one line per page.
"""
import os
import subprocess
import sys
import time

EXT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'extension')
sys.path.insert(0, os.path.join(EXT, 'vendor'))
from PIL import Image, ImageChops  # noqa: E402
from pypdf import PdfReader  # noqa: E402

INKSCAPE = os.environ.get('INKSCAPE') or (r'C:\Program Files\Inkscape\bin\inkscape.com'
                                          if sys.platform == 'win32' else 'inkscape')
PY = sys.executable
WIDTH = 1400


def render(src, png, extra=()):
    def run(args):
        return subprocess.run([INKSCAPE, *args, '--export-type=png', '--export-width=%d' % WIDTH,
                               '--export-background=white', '--export-background-opacity=1',
                               '--export-filename=' + png, src], capture_output=True)
    r = run(extra)
    if r.returncode and b'Unknown option --pages' in r.stderr:
        # Inkscape 1.2 and older: one page with --pdf-page instead of --pages.
        r = run([a.replace('--pages=', '--pdf-page=') for a in extra])
    r.check_returncode()


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    for pdf in sys.argv[2:]:
        base = os.path.splitext(os.path.basename(pdf))[0].strip().replace(' ', '_')
        n = len(PdfReader(pdf).pages)
        for p in range(1, n + 1):
            tag = base + ('-p%d' % p if n > 1 else '')
            svg = os.path.join(out, tag + '.svg')
            t = time.time()
            r = subprocess.run([PY, os.path.join(EXT, 'pdf_import_choose_type.py'), '--pages=%d' % p, '--stats',
                                '--output=' + svg, pdf], capture_output=True, text=True)
            secs = time.time() - t
            if r.returncode:
                print('%-45s FAILED\n%s' % (tag, r.stderr[-1500:]))
                continue
            ref = os.path.join(out, tag + '.ref.png')
            got = os.path.join(out, tag + '.got.png')
            render(pdf, ref, ['--pdf-poppler', '--pages=%d' % p])
            render(svg, got)
            a = Image.open(ref).convert('RGB')
            b = Image.open(got).convert('RGB').resize(a.size)
            d = ImageChops.difference(a, b).convert('L')
            hist = d.histogram()
            bad = sum(hist[48:]) / float(sum(hist)) * 100
            mask = d.point(lambda v: 255 if v >= 48 else 0)
            vis = Image.blend(a, Image.new('RGB', a.size, 'white'), 0.75)
            vis.paste((255, 0, 0), mask=mask)
            vis.save(os.path.join(out, tag + '.diff.png'))
            stats = r.stderr.strip().splitlines()
            print('%-45s diff %6.3f%%  %5.1fs  %6.0f KB  %s' % (tag, bad, secs, os.path.getsize(svg) / 1024, stats[0] if stats else ''))
            for line in stats[1:]:
                print('    ' + line)


if __name__ == '__main__':
    main()
