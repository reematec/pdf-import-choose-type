#!/usr/bin/env python3
"""PDF -> SVG importer that keeps clip groups (CorelDRAW PowerClips, Illustrator
clipping masks) as ONE clip on a group holding the clipped objects.

Inkscape runs it as an input extension (see pdf_import_choose_type.inx). It also works
on its own:

    python pdf_import_choose_type.py [--type=vector|standard] [--text=outlines|editable]
                               [--pages=all|1,3] [--output=out.svg] in.pdf

--type picks the converter for this PDF:
  vector    Vector artwork (keep clips): this converter (default).
  standard  Standard PDF: Inkscape's own PDF import, run unchanged.

Without --output the SVG is written to stdout (what Inkscape reads). Anything the
PDF uses that cannot be converted exactly is reported on stderr, which Inkscape
shows in a message after the import.
"""
import argparse
import base64
import io
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, 'vendor'))

from PIL import Image, ImageChops, ImageCms  # noqa: E402  (bundled with Inkscape)
from pypdf import PdfReader  # noqa: E402
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, StreamObject  # noqa: E402
from pypdf._codecs import adobe_glyphs, charset_encoding  # noqa: E402

PX = 96 / 72  # PDF points -> SVG px
WARNINGS = {}


def warn(msg):
    WARNINGS[msg] = WARNINGS.get(msg, 0) + 1


def R(o):
    """Resolve an indirect object."""
    while isinstance(o, IndirectObject):
        o = o.get_object()
    return o


def num(o, default=0.0):
    o = R(o)
    try:
        return float(o)
    except (TypeError, ValueError):
        return default


def nums(o):
    return [num(x) for x in R(o)]


def name(o):
    o = R(o)
    return str(o)[1:] if isinstance(o, str) and str(o).startswith('/') else str(o)


def obj_key(o):
    if isinstance(o, IndirectObject):
        return ('ref', o.idnum, o.generation)
    o2 = getattr(o, 'indirect_reference', None)
    if o2 is not None:
        return ('ref', o2.idnum, o2.generation)
    return ('id', id(o))


# ---------------------------------------------------------------- matrices
def mul(m1, m2):
    """m1 applied first, then m2 (PDF row-vector convention: m1 x m2)."""
    a, b, c, d, e, f = m1
    A, B, C, D, E, F = m2
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D,
            e * A + f * C + E, e * B + f * D + F)


def inv(m):
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        return (1, 0, 0, 1, 0, 0)
    return (d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det)


def apply(m, x, y):
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def is_similarity(m):
    a, b, c, d = m[:4]
    s = a * a + b * b + c * c + d * d
    return s > 0 and abs((a * a + b * b) - (c * c + d * d)) < 1e-6 * s and abs(a * c + b * d) < 1e-6 * s


def scale_of(m):
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))


def fmt(x):
    if abs(x) < 5e-4:
        return '0'
    s = '%.3f' % x
    s = s.rstrip('0').rstrip('.')
    return s


def fmt_m(m):
    return 'matrix(%s)' % ','.join(('%.6g' % v) for v in m)


def path_d(segs, m=None):
    out = []
    for op, pts in segs:
        if op == 'Z':
            out.append('Z')
            continue
        if m is not None:
            pts = [apply(m, x, y) for x, y in pts]
        out.append(op + ' '.join(fmt(x) + ',' + fmt(y) for x, y in pts))
    return ' '.join(out)


def bbox_of(segs, m):
    xs, ys = [], []
    for op, pts in segs:
        for x, y in pts:
            X, Y = apply(m, x, y)
            xs.append(X)
            ys.append(Y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def esc(t):
    return (t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;'))


def clean_xml_text(t):
    return ''.join(ch for ch in t if ch in '\t\n\r' or ord(ch) >= 32 and not 0xD800 <= ord(ch) <= 0xDFFF and ord(ch) not in (0xFFFE, 0xFFFF))


# ---------------------------------------------------------------- SVG tree
class Node:
    __slots__ = ('tag', 'attrs', 'children', 'text')

    def __init__(self, tag, attrs=None, text=None):
        self.tag = tag
        self.attrs = attrs or []
        self.children = []
        self.text = text

    def add(self, child):
        self.children.append(child)
        return child

    def drawable(self):
        if self.tag in ('path', 'image', 'text', 'use', 'rect'):
            return True
        return any(c.drawable() for c in self.children)

    def write(self, out, depth=0):
        pad = '  ' * depth
        attrs = ''.join(' %s="%s"' % (k, v) for k, v in self.attrs)
        if self.text is not None:
            out.append('%s<%s%s>%s</%s>' % (pad, self.tag, attrs, self.text, self.tag))
        elif not self.children:
            out.append('%s<%s%s/>' % (pad, self.tag, attrs))
        else:
            out.append('%s<%s%s>' % (pad, self.tag, attrs))
            for c in self.children:
                c.write(out, depth + 1)
            out.append('%s</%s>' % (pad, self.tag))


def prune(node):
    """Drop groups that contain nothing visible (e.g. empty PowerClips)."""
    keep = []
    for c in node.children:
        if c.tag == 'g' and not c.drawable() and not any(k == 'inkscape:groupmode' for k, _ in c.attrs):
            continue
        prune(c)
        keep.append(c)
    node.children = keep


# ---------------------------------------------------------------- color
SRGB = ImageCms.createProfile('sRGB')
_xforms = {}
# Pillow 10.3+ has ImageCms.Flags; older versions (e.g. Ubuntu 24.04's 10.2) have a FLAGS dict.
if hasattr(ImageCms, 'Flags'):
    BPC_FLAG = ImageCms.Flags.BLACKPOINTCOMPENSATION
else:
    BPC_FLAG = getattr(ImageCms, 'FLAGS', {}).get('BLACKPOINTCOMPENSATION', 0x2000)
RELATIVE_INTENT = getattr(getattr(ImageCms, 'Intent', None), 'RELATIVE_COLORIMETRIC',
                          getattr(ImageCms, 'INTENT_RELATIVE_COLORIMETRIC', 1))


SYSTEM_CMYK_PROFILES = [
    # Windows: Microsoft's SWOP profile, part of every Windows install.
    os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'spool', 'drivers', 'color', 'RSWOP.icm'),
    # Linux: Ghostscript's default CMYK profile, installed with Ghostscript.
    '/usr/share/color/icc/ghostscript/default_cmyk.icc',
    '/usr/share/ghostscript/iccprofiles/default_cmyk.icc',
]
NO_CMYK_PROFILE = ('No CMYK color profile was found, so CMYK colors were converted with a simple formula. '
                   "To match print colors, put a CMYK profile (.icc) in the extension's profiles folder.")


def cmyk_profile_default():
    """CMYK profile for PDFs that do not name one.

    A profile in the extension's profiles/ folder wins (any .icc or .icm; none is
    shipped), then a profile that comes with the system. Without one, CMYK is
    converted with a simple formula and the import says so.
    """
    folder = os.path.join(HERE, 'profiles')
    own = sorted(f for f in os.listdir(folder) if f.lower().endswith(('.icc', '.icm'))) if os.path.isdir(folder) else []
    for path in [os.path.join(folder, f) for f in own] + SYSTEM_CMYK_PROFILES:
        if os.path.isfile(path):
            with open(path, 'rb') as f:
                return f.read()
    return None


def icc_transform(profile_bytes, mode):
    key = (hash(profile_bytes), mode)
    if key not in _xforms:
        try:
            prof = ImageCms.ImageCmsProfile(io.BytesIO(profile_bytes))
            _xforms[key] = ImageCms.buildTransform(
                prof, SRGB, mode, 'RGB',
                renderingIntent=RELATIVE_INTENT, flags=BPC_FLAG)
        except Exception as e:  # broken or unusual profile
            warn('A color profile in the PDF could not be read (%s); colors in it were converted without it.' % e)
            _xforms[key] = None
    return _xforms[key]


def clamp01(v):
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def lab_to_rgb(L, a, b, wp=(0.9642, 1.0, 0.8249)):
    fy = (L + 16) / 116
    fx = fy + a / 500
    fz = fy - b / 200

    def finv(t):
        return t ** 3 if t > 6 / 29 else 3 * (6 / 29) ** 2 * (t - 4 / 29)
    X, Y, Z = wp[0] * finv(fx), wp[1] * finv(fy), wp[2] * finv(fz)
    # Bradford adaptation from the Lab white point to D65
    M = ((0.8951, 0.2664, -0.1614), (-0.7502, 1.7135, 0.0367), (0.0389, -0.0685, 1.0296))
    Mi = ((0.9869929, -0.1470543, 0.1599627), (0.4323053, 0.5183603, 0.0492912), (-0.0085287, 0.0400428, 0.9684867))

    def dot(m, v):
        return tuple(m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3))
    src = dot(M, wp)
    dst = dot(M, (0.95047, 1.0, 1.08883))
    lms = dot(M, (X, Y, Z))
    lms = tuple(lms[i] * dst[i] / src[i] for i in range(3))
    X, Y, Z = dot(Mi, lms)
    r = 3.2404542 * X - 1.5371385 * Y - 0.4985314 * Z
    g = -0.9692660 * X + 1.8760108 * Y + 0.0415560 * Z
    bb = 0.0556434 * X - 0.2040259 * Y + 1.0572252 * Z

    def gam(c):
        c = clamp01(c)
        return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return gam(r), gam(g), gam(bb)


def to_hex(rgb):
    return '#%02x%02x%02x' % tuple(int(round(clamp01(c) * 255)) for c in rgb)


# ---------------------------------------------------------------- functions
class PSCalc:
    """PostScript calculator functions (FunctionType 4)."""
    def __init__(self, src):
        toks = re.findall(rb'[{}]|[^\s{}]+', src)
        self.prog, _ = self._parse(toks, 1)

    def _parse(self, toks, i):
        out = []
        while i < len(toks):
            t = toks[i]
            if t == b'{':
                sub, i = self._parse(toks, i + 1)
                out.append(sub)
                continue
            if t == b'}':
                return out, i + 1
            try:
                out.append(float(t))
            except ValueError:
                out.append(t.decode())
            i += 1
        return out, i

    def run(self, stack):
        self._exec(self.prog, stack)
        return stack

    def _exec(self, prog, s):
        i = 0
        while i < len(prog):
            t = prog[i]
            if isinstance(t, (float, list)):
                s.append(t)
            elif t in ('if', 'ifelse'):
                if t == 'if':
                    p, c = s.pop(), s.pop()
                    if c:
                        self._exec(p, s)
                else:
                    p2, p1, c = s.pop(), s.pop(), s.pop()
                    self._exec(p1 if c else p2, s)
            else:
                self._op(t, s)
            i += 1

    def _op(self, t, s):
        if t == 'true': s.append(True)
        elif t == 'false': s.append(False)
        elif t == 'add': b = s.pop(); s.append(s.pop() + b)
        elif t == 'sub': b = s.pop(); s.append(s.pop() - b)
        elif t == 'mul': b = s.pop(); s.append(s.pop() * b)
        elif t == 'div': b = s.pop(); a = s.pop(); s.append(a / b if b else 0.0)
        elif t == 'idiv': b = s.pop(); a = s.pop(); s.append(float(int(a) // int(b)) if b else 0.0)
        elif t == 'mod': b = s.pop(); a = s.pop(); s.append(float(int(a) % int(b)) if b else 0.0)
        elif t == 'neg': s.append(-s.pop())
        elif t == 'abs': s.append(abs(s.pop()))
        elif t == 'ceiling': s.append(float(math.ceil(s.pop())))
        elif t == 'floor': s.append(float(math.floor(s.pop())))
        elif t == 'round': s.append(float(math.floor(s.pop() + 0.5)))
        elif t == 'truncate': s.append(float(int(s.pop())))
        elif t == 'sqrt': s.append(math.sqrt(max(0.0, s.pop())))
        elif t == 'sin': s.append(math.sin(math.radians(s.pop())))
        elif t == 'cos': s.append(math.cos(math.radians(s.pop())))
        elif t == 'atan': b = s.pop(); a = s.pop(); s.append(math.degrees(math.atan2(a, b)) % 360)
        elif t == 'exp': b = s.pop(); a = s.pop(); s.append(a ** b)
        elif t == 'ln': s.append(math.log(max(1e-30, s.pop())))
        elif t == 'log': s.append(math.log10(max(1e-30, s.pop())))
        elif t in ('cvi',): s.append(float(int(s.pop())))
        elif t in ('cvr',): pass
        elif t == 'eq': b = s.pop(); s.append(s.pop() == b)
        elif t == 'ne': b = s.pop(); s.append(s.pop() != b)
        elif t == 'gt': b = s.pop(); s.append(s.pop() > b)
        elif t == 'ge': b = s.pop(); s.append(s.pop() >= b)
        elif t == 'lt': b = s.pop(); s.append(s.pop() < b)
        elif t == 'le': b = s.pop(); s.append(s.pop() <= b)
        elif t == 'and': b = s.pop(); a = s.pop(); s.append(a and b if isinstance(a, bool) else float(int(a) & int(b)))
        elif t == 'or': b = s.pop(); a = s.pop(); s.append(a or b if isinstance(a, bool) else float(int(a) | int(b)))
        elif t == 'xor': b = s.pop(); a = s.pop(); s.append(a != b if isinstance(a, bool) else float(int(a) ^ int(b)))
        elif t == 'not': a = s.pop(); s.append(not a if isinstance(a, bool) else float(~int(a)))
        elif t == 'bitshift': b = int(s.pop()); a = int(s.pop()); s.append(float(a << b if b >= 0 else a >> -b))
        elif t == 'pop': s.pop()
        elif t == 'dup': s.append(s[-1])
        elif t == 'exch': s[-1], s[-2] = s[-2], s[-1]
        elif t == 'copy':
            n = int(s.pop())
            if n: s.extend(s[-n:])
        elif t == 'index': n = int(s.pop()); s.append(s[-1 - n])
        elif t == 'roll':
            j = int(s.pop()); n = int(s.pop())
            if n:
                j %= n
                part = s[-n:]
                del s[-n:]
                s.extend(part[-j:] + part[:-j] if j else part)
        else:
            raise ValueError('unknown PostScript operator %s' % t)


class PdfFunction:
    def __init__(self, obj):
        obj = R(obj)
        if isinstance(obj, list):  # array of 1-out functions
            self.parts = [PdfFunction(f) for f in obj]
            self.kind = 'array'
            return
        self.kind = int(obj['/FunctionType'])
        self.domain = nums(obj['/Domain']) if '/Domain' in obj else [0.0, 1.0]
        self.range = nums(obj['/Range']) if '/Range' in obj else None
        if self.kind == 2:
            self.c0 = nums(obj['/C0']) if '/C0' in obj else [0.0]
            self.c1 = nums(obj['/C1']) if '/C1' in obj else [1.0]
            self.n = num(obj['/N'], 1.0)
        elif self.kind == 3:
            self.funcs = [PdfFunction(f) for f in R(obj['/Functions'])]
            self.bounds = nums(obj['/Bounds'])
            self.encode = nums(obj['/Encode'])
        elif self.kind == 0:
            self.size = [int(num(x)) for x in R(obj['/Size'])]
            self.bps = int(obj['/BitsPerSample'])
            self.encode = nums(obj['/Encode']) if '/Encode' in obj else sum(([0.0, s - 1.0] for s in self.size), [])
            self.decode = nums(obj['/Decode']) if '/Decode' in obj else list(self.range)
            data = obj.get_data()
            self.nout = len(self.range) // 2
            total = 1
            for s in self.size:
                total *= s
            vals = []
            if self.bps == 8:
                vals = list(data[:total * self.nout])
            elif self.bps == 16:
                vals = [int.from_bytes(data[i:i + 2], 'big') for i in range(0, total * self.nout * 2, 2)]
            else:
                bits = ''.join('{:08b}'.format(x) for x in data)
                vals = [int(bits[i:i + self.bps], 2) for i in range(0, total * self.nout * self.bps, self.bps)]
            self.samples = vals
        elif self.kind == 4:
            self.calc = PSCalc(obj.get_data())
        else:
            raise ValueError('function type %s' % self.kind)

    def __call__(self, xs):
        if self.kind == 'array':
            return [p(xs)[0] for p in self.parts]
        xs = [min(max(x, self.domain[2 * i]), self.domain[2 * i + 1]) for i, x in enumerate(xs)]
        if self.kind == 2:
            x = xs[0]
            out = [c0 + (x ** self.n if x > 0 or self.n == int(self.n) else 0) * (c1 - c0) for c0, c1 in zip(self.c0, self.c1)]
        elif self.kind == 3:
            x = xs[0]
            k = 0
            while k < len(self.bounds) and x >= self.bounds[k]:
                k += 1
            lo = self.domain[0] if k == 0 else self.bounds[k - 1]
            hi = self.domain[1] if k == len(self.bounds) else self.bounds[k]
            e0, e1 = self.encode[2 * k], self.encode[2 * k + 1]
            t = e0 + (x - lo) * (e1 - e0) / (hi - lo) if hi != lo else e0
            out = self.funcs[k]([t])
        elif self.kind == 0:
            x = xs[0]
            d0, d1 = self.domain[0], self.domain[1]
            e = self.encode[0] + (x - d0) * (self.encode[1] - self.encode[0]) / (d1 - d0 if d1 != d0 else 1)
            e = min(max(e, 0), self.size[0] - 1)
            i0 = int(math.floor(e))
            i1 = min(i0 + 1, self.size[0] - 1)
            f = e - i0
            mx = (1 << self.bps) - 1
            out = []
            for j in range(self.nout):
                v = self.samples[i0 * self.nout + j] * (1 - f) + self.samples[i1 * self.nout + j] * f
                out.append(self.decode[2 * j] + v * (self.decode[2 * j + 1] - self.decode[2 * j]) / mx)
        else:
            st = self.calc.run([float(x) for x in xs])
            out = [float(v) for v in st]
        if self.range:
            out = [min(max(v, self.range[2 * i]), self.range[2 * i + 1]) if 2 * i + 1 < len(self.range) else v
                   for i, v in enumerate(out)]
        return out


# ---------------------------------------------------------------- color spaces
class ColorSpace:
    """Converts component values to sRGB (floats 0..1)."""
    n = 1
    is_pattern = False
    none = False  # Separation /None: paints nothing
    spot = None

    def initial(self):
        return [0.0] * self.n

    def rgb(self, comps):
        raise NotImplementedError


class DeviceGray(ColorSpace):
    n = 1

    def rgb(self, c):
        v = c[0] if c else 0
        return (v, v, v)


class DeviceRGB(ColorSpace):
    n = 3

    def rgb(self, c):
        return tuple(c[:3])


class DeviceCMYK(ColorSpace):
    n = 4

    def __init__(self, profile=None):
        self.profile = profile
        self.cache = {}

    def initial(self):
        return [0.0, 0.0, 0.0, 1.0]

    def rgb(self, c):
        key = tuple(round(v * 255) for v in c[:4])
        if key in self.cache:
            return self.cache[key]
        xf = icc_transform(self.profile, 'CMYK') if self.profile else None
        if xf:
            px = ImageCms.applyTransform(Image.new('CMYK', (1, 1), key), xf).getpixel((0, 0))
            out = tuple(v / 255 for v in px)
        else:
            if not self.profile and NO_CMYK_PROFILE not in WARNINGS:
                warn(NO_CMYK_PROFILE)
            cc, m, y, k = (v / 255 for v in key)
            out = ((1 - cc) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
        self.cache[key] = out
        return out


class LabCS(ColorSpace):
    n = 3

    def __init__(self, d):
        d = R(d)
        self.wp = tuple(nums(d['/WhitePoint'])) if '/WhitePoint' in d else (0.9642, 1, 0.8249)
        self.rng = nums(d['/Range']) if '/Range' in d else [-100, 100, -100, 100]

    def initial(self):
        return [0.0, 0.0, 0.0]

    def rgb(self, c):
        return lab_to_rgb(c[0], c[1], c[2], self.wp)


class ICCRGB(ColorSpace):
    n = 3

    def __init__(self, profile):
        self.profile = profile
        self.cache = {}

    def rgb(self, c):
        key = tuple(round(clamp01(v) * 255) for v in c[:3])
        if key not in self.cache:
            xf = icc_transform(self.profile, 'RGB')
            if xf:
                px = ImageCms.applyTransform(Image.new('RGB', (1, 1), key), xf).getpixel((0, 0))
                self.cache[key] = tuple(v / 255 for v in px)
            else:
                self.cache[key] = tuple(v / 255 for v in key)
        return self.cache[key]


class TintCS(ColorSpace):
    """Separation and DeviceN."""
    def __init__(self, names, alt, fn):
        self.names = names
        self.n = len(names)
        self.alt = alt
        self.fn = fn
        self.cache = {}
        self.none = all(nm == 'None' for nm in names)
        self.spot = names[0] if self.n == 1 and names[0] not in ('All', 'None') else None

    def initial(self):
        return [1.0] * self.n

    def rgb(self, c):
        key = tuple(round(v, 4) for v in c[:self.n])
        if key not in self.cache:
            self.cache[key] = self.alt.rgb(self.fn(list(key)))
        return self.cache[key]


class IndexedCS(ColorSpace):
    n = 1

    def __init__(self, base, hival, lookup):
        self.base = base
        self.hival = hival
        self.lookup = lookup

    def rgb(self, c):
        i = int(min(max(round(c[0]), 0), self.hival))
        nb = self.base.n
        comps = [self.lookup[i * nb + j] / 255 for j in range(nb)]
        if isinstance(self.base, LabCS):
            r = self.base.rng
            comps = [comps[0] * 100, r[0] + comps[1] * (r[1] - r[0]), r[2] + comps[2] * (r[3] - r[2])]
        return self.base.rgb(comps)


class PatternCS(ColorSpace):
    is_pattern = True

    def __init__(self, under=None):
        self.under = under
        self.n = under.n if under else 0

    def rgb(self, c):
        return self.under.rgb(c) if self.under else (0, 0, 0)


class ColorSpaces:
    def __init__(self, reader):
        self.default_cmyk_profile = None
        try:
            for oi in R(R(reader.trailer['/Root']).get('/OutputIntents', [])) or []:
                prof = R(R(oi).get('/DestOutputProfile'))
                if prof is not None:
                    self.default_cmyk_profile = prof.get_data()
                    break
        except Exception:
            pass
        if self.default_cmyk_profile is None:
            self.default_cmyk_profile = cmyk_profile_default()
        self.cache = {}

    def device(self, nm, resources):
        defaults = R(R(resources).get('/ColorSpace', {})) if resources is not None else {}
        if nm in ('DeviceGray', 'G'):
            if defaults and '/DefaultGray' in defaults:
                return self.get(defaults['/DefaultGray'], resources)
            return DeviceGray()
        if nm in ('DeviceRGB', 'RGB'):
            if defaults and '/DefaultRGB' in defaults:
                return self.get(defaults['/DefaultRGB'], resources)
            return DeviceRGB()
        if nm in ('DeviceCMYK', 'CMYK'):
            if defaults and '/DefaultCMYK' in defaults:
                return self.get(defaults['/DefaultCMYK'], resources)
            return self._cmyk(self.default_cmyk_profile)
        if nm == 'Pattern':
            return PatternCS()
        return None

    def _cmyk(self, profile):
        key = ('cmyk', hash(profile))
        if key not in self.cache:
            self.cache[key] = DeviceCMYK(profile)
        return self.cache[key]

    def by_name(self, nm, resources):
        cs = self.device(nm, resources)
        if cs:
            return cs
        csd = R(R(resources).get('/ColorSpace', {})) if resources is not None else {}
        if csd and '/' + nm in csd:
            return self.get(csd['/' + nm], resources)
        warn('Unknown color space %s; painted black.' % nm)
        return DeviceGray()

    def get(self, obj, resources):
        key = obj_key(obj)
        if key in self.cache:
            return self.cache[key]
        cs = self._make(R(obj), resources)
        self.cache[key] = cs
        return cs

    def _make(self, o, resources):
        if isinstance(o, str):
            nm = name(o)
            cs = self.device(nm, None)
            if cs:
                return cs
            return self.by_name(nm, resources)
        fam = name(o[0])
        if fam in ('DeviceGray', 'DeviceRGB', 'DeviceCMYK', 'Pattern') and len(o) == 1:
            return self.device(fam, None)
        if fam == 'CalGray':
            return DeviceGray()
        if fam == 'CalRGB':
            return DeviceRGB()
        if fam == 'Lab':
            return LabCS(o[1])
        if fam == 'ICCBased':
            st = R(o[1])
            n = int(st.get('/N', 3))
            data = st.get_data()
            if n == 4:
                return self._cmyk(data)
            if n == 3:
                desc = ''
                try:
                    desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(data)))
                except Exception:
                    pass
                return DeviceRGB() if 'sRGB' in desc or not desc else ICCRGB(data)
            return DeviceGray()
        if fam in ('Separation', 'DeviceN'):
            names = [name(o[1])] if fam == 'Separation' else [name(x) for x in R(o[1])]
            alt = self._make(R(o[2]), resources)
            return TintCS(names, alt, PdfFunction(o[3]))
        if fam in ('Indexed', 'I'):
            base = self._make(R(o[1]), resources)
            hival = int(num(o[2]))
            lk = R(o[3])
            data = lk.get_data() if isinstance(lk, StreamObject) else (lk.original_bytes if hasattr(lk, 'original_bytes') else bytes(str(lk), 'latin1'))
            return IndexedCS(base, hival, data)
        if fam == 'Pattern':
            return PatternCS(self._make(R(o[1]), resources))
        warn('Color space %s is not supported; painted black.' % fam)
        return DeviceGray()


# ---------------------------------------------------------------- content stream tokenizer


TOKRE = re.compile(rb'[ \t\r\n\x0c\x00]+|%[^\r\n]*|/[^ \t\r\n\x0c\x00/\[\]()<>{}%]*|<<|>>|\[|\]|<[0-9A-Fa-f \t\r\n]*>|\(|[{}]|[^ \t\r\n\x0c\x00/\[\]()<>{}%]+')
ESC = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}


def _lit_string(data, i):
    """Parses a (literal string) starting after '('; returns (bytes, end index)."""
    n = len(data)
    depth = 1
    out = bytearray()
    j = i
    while j < n:
        c = data[j]
        if c == 0x5C:
            j += 1
            if j >= n:
                break
            c = data[j]
            if c in ESC:
                out.append(ESC[c])
            elif 0x30 <= c <= 0x37:
                k = j
                while k < j + 3 and k < n and 0x30 <= data[k] <= 0x37:
                    k += 1
                out.append(int(data[j:k], 8) & 255)
                j = k - 1
            elif c in (10, 13):
                if c == 13 and j + 1 < n and data[j + 1] == 10:
                    j += 1
            else:
                out.append(c)
        elif c == 0x28:
            depth += 1
            out.append(c)
        elif c == 0x29:
            depth -= 1
            if not depth:
                return bytes(out), j + 1
            out.append(c)
        else:
            out.append(c)
        j += 1
    return bytes(out), j


def tokenize(data):
    """Yields (operator, operands) for a content stream, and ('BI', params, data) for inline images."""
    i, n = 0, len(data)
    stack = []
    marks = []
    match = TOKRE.match
    while i < n:
        m = match(data, i)
        if m is None:
            i += 1
            continue
        tok = m.group()
        i = m.end()
        c = tok[0]
        if c in b' \t\r\n\x0c\x00%':
            continue
        if c == 0x2F:
            nm = tok[1:]
            if b'#' in nm:
                nm = re.sub(rb'#([0-9A-Fa-f]{2})', lambda mm: bytes([int(mm.group(1), 16)]), nm)
            stack.append(nm.decode('latin1'))
            continue
        if c == 0x28:
            sv, i = _lit_string(data, i)
            stack.append(sv)
            continue
        if tok == b'<<' or tok == b'[':
            marks.append(len(stack))
            stack.append(None)
            continue
        if tok == b'>>':
            k = marks.pop() if marks else 0
            items = stack[k + 1:]
            del stack[k:]
            d = {}
            for a in range(0, len(items) - 1, 2):
                if isinstance(items[a], str):
                    d[items[a]] = items[a + 1]
            stack.append(d)
            continue
        if tok == b']':
            k = marks.pop() if marks else 0
            items = stack[k + 1:]
            del stack[k:]
            stack.append(items)
            continue
        if c == 0x3C:
            hx = re.sub(rb'\s', b'', tok[1:-1])
            if len(hx) % 2:
                hx += b'0'
            stack.append(bytes.fromhex(hx.decode()))
            continue
        if c in b'{}':
            continue
        try:
            stack.append(float(tok))
            continue
        except ValueError:
            pass
        if tok in (b'true', b'false'):
            stack.append(tok == b'true')
            continue
        if tok == b'null':
            stack.append(None)
            continue
        if marks:  # an operator inside [ ] or << >> is malformed; keep as a name
            stack.append(tok.decode('latin1'))
            continue
        op = tok.decode('latin1')
        if op == 'BI':
            k = data.find(b'ID', i)
            params = {}
            sub = []
            for _op, vals in tokenize(data[i:k] + b' __end__'):
                sub.extend(vals)
            for a in range(0, len(sub) - 1, 2):
                if isinstance(sub[a], str):
                    params[sub[a]] = sub[a + 1]
            start = k + 2
            if start < n and data[start] in b' \t\r\n\x0c\x00':
                start += 1
            mm = re.compile(rb'\sEI(?=\s|$)').search(data, start)
            end = mm.start() if mm else n
            yield ('BI', params, data[start:end])
            i = mm.end() if mm else n
            stack = []
            continue
        yield (op, stack)
        stack = []


# ---------------------------------------------------------------- fonts
STD_ENC_NAMES = None


def _std_names(which):
    from fontTools.encodings.StandardEncoding import StandardEncoding
    from fontTools.encodings.MacRoman import MacRoman
    from fontTools.agl import UV2AGL
    if which == 'StandardEncoding':
        return list(StandardEncoding)
    if which == 'MacRomanEncoding':
        return list(MacRoman)
    chars = charset_encoding.get('/' + which) or charset_encoding['/StandardEncoding']
    names = []
    for ch in chars:
        if isinstance(ch, str) and len(ch) == 1 and ord(ch) in UV2AGL:
            names.append(UV2AGL[ord(ch)])
        else:
            names.append('.notdef')
    return names


def glyph_unicode(gname):
    ch = adobe_glyphs.get('/' + gname)
    if ch:
        return ch
    m = re.match(r'uni([0-9A-Fa-f]{4})', gname) or re.match(r'u([0-9A-Fa-f]{4,6})$', gname)
    if m:
        return chr(int(m.group(1), 16))
    return ''


def parse_cmap(data):
    """ToUnicode / embedded CMap -> (codespace byte lengths, code->str, code->cid)."""
    text = data.decode('latin1')
    lengths = set()
    for blk in re.findall(r'begincodespacerange(.*?)endcodespacerange', text, re.S):
        for a, _b in re.findall(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', blk):
            lengths.add(len(a) // 2)
    uni, cid = {}, {}

    def u16(h):
        try:
            return bytes.fromhex(h).decode('utf-16-be', 'replace')
        except ValueError:
            return ''
    for blk in re.findall(r'beginbfchar(.*?)endbfchar', text, re.S):
        for a, b in re.findall(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>', blk):
            uni[(len(a) // 2, int(a, 16))] = u16(b)
    for blk in re.findall(r'beginbfrange(.*?)endbfrange', text, re.S):
        for a, b, rest in re.findall(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]*>)', blk):
            lo, hi, nb = int(a, 16), int(b, 16), len(a) // 2
            if rest.startswith('['):
                for i, h in enumerate(re.findall(r'<([0-9A-Fa-f]*)>', rest)):
                    uni[(nb, lo + i)] = u16(h)
            else:
                h = rest[1:-1]
                base = bytes.fromhex(h) if len(h) % 2 == 0 else b''
                for i, code in enumerate(range(lo, hi + 1)):
                    if not base:
                        break
                    v = int.from_bytes(base, 'big') + i
                    uni[(nb, code)] = u16('%0*x' % (len(h), v))
    for blk in re.findall(r'begincidchar(.*?)endcidchar', text, re.S):
        for a, c in re.findall(r'<([0-9A-Fa-f]+)>\s*(\d+)', blk):
            cid[(len(a) // 2, int(a, 16))] = int(c)
    for blk in re.findall(r'begincidrange(.*?)endcidrange', text, re.S):
        for a, b, c in re.findall(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\d+)', blk):
            for i, code in enumerate(range(int(a, 16), int(b, 16) + 1)):
                cid[(len(a) // 2, code)] = int(c) + i
    return sorted(lengths) or [2], uni, cid


class GlyphPen:
    """fontTools pen that records segments in glyph units (cubic curves only)."""
    def __init__(self, glyphset=None):
        from fontTools.pens.basePen import BasePen

        class _P(BasePen):
            def __init__(s, gs):
                BasePen.__init__(s, gs)
                s.segs = []

            def _moveTo(s, p):
                s.segs.append(('M', [p]))

            def _lineTo(s, p):
                s.segs.append(('L', [p]))

            def _curveToOne(s, p1, p2, p3):
                s.segs.append(('C', [p1, p2, p3]))

            def _qCurveToOne(s, p1, p2):
                p0 = s._getCurrentPoint()
                c1 = (p0[0] + 2 / 3 * (p1[0] - p0[0]), p0[1] + 2 / 3 * (p1[1] - p0[1]))
                c2 = (p2[0] + 2 / 3 * (p1[0] - p2[0]), p2[1] + 2 / 3 * (p1[1] - p2[1]))
                s.segs.append(('C', [c1, c2, p2]))

            def _closePath(s):
                s.segs.append(('Z', []))

            def _endPath(s):
                pass
        self.pen = _P(glyphset)


FAMILY_OVERRIDES = {
    'AvantGardeITCbyBT': 'ITC Avant Garde Gothic', 'ArialMT': 'Arial', 'Arial': 'Arial',
    'TimesNewRomanPS': 'Times New Roman', 'CourierNewPS': 'Courier New', 'Square721': 'Square721 BT',
    'Square721BT': 'Square721 BT', 'ArialNarrow': 'Arial Narrow',
}
WEIGHTS = [('Thin', 100), ('ExtraLight', 200), ('UltraLight', 200), ('Light', 300), ('Book', 400),
           ('Regular', 400), ('Normal', 400), ('Roman', 400), ('Medium', 500), ('SemiBold', 600),
           ('Semibold', 600), ('Demi', 600), ('ExtraBold', 800), ('Bold', 700), ('Heavy', 900), ('Black', 900)]


def font_family_style(basename, desc):
    base = re.sub(r'^[A-Z]{6}\+', '', basename)
    fam_hint = name(desc.get('/FontFamily')) if desc is not None and '/FontFamily' in desc else None
    if fam_hint and fam_hint.startswith(('\xfe\xff', 'þÿ')):
        fam_hint = None
    parts = re.split(r'[-,]', base, 1)
    fam = parts[0]
    style = parts[1] if len(parts) > 1 else ''
    for suffix, _w in WEIGHTS + [('Italic', 0), ('Oblique', 0)]:
        if fam.endswith(suffix) and len(fam) > len(suffix) + 2:
            fam, style = fam[:-len(suffix)], suffix + style
    fam = re.sub(r'(MT|PS|PSMT)$', '', fam) if fam not in FAMILY_OVERRIDES else fam
    family = fam_hint or FAMILY_OVERRIDES.get(fam) or FAMILY_OVERRIDES.get(fam + 'MT') or \
        re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', fam)
    weight = 400
    for token, w in WEIGHTS:
        if token.lower() in style.lower():
            weight = w
            break
    if desc is not None and '/FontWeight' in desc and weight == 400:
        weight = int(num(desc['/FontWeight'], 400))
    italic = 'italic' in style.lower() or 'oblique' in style.lower()
    condensed = 'cond' in style.lower() or 'narrow' in base.lower()
    return family, weight, italic, condensed


class Font:
    def __init__(self, obj):
        self.obj = obj = R(obj)
        self.subtype = name(obj.get('/Subtype'))
        self.basename = name(obj.get('/BaseFont', '/Unknown'))
        self.type0 = self.subtype == 'Type0'
        self.type3 = self.subtype == 'Type3'
        self.glyphs = {}  # code -> segs (text space, 1 unit = font size) or None
        self.outline_src = None
        self.font_matrix = (0.001, 0, 0, 0.001, 0, 0)
        desc = None
        if self.type0:
            df = R(R(obj['/DescendantFonts'])[0])
            self.cidfont = df
            desc = R(df.get('/FontDescriptor'))
            self.dw = num(df.get('/DW'), 1000)
            self.widths = {}
            w = R(df.get('/W')) or []
            i = 0
            while i < len(w):
                first = int(num(w[i]))
                nxt = R(w[i + 1])
                if isinstance(nxt, list):
                    for k, v in enumerate(nxt):
                        self.widths[first + k] = num(v)
                    i += 2
                else:
                    last = int(num(nxt))
                    for c in range(first, last + 1):
                        self.widths[c] = num(w[i + 2])
                    i += 3
            enc = R(obj.get('/Encoding'))
            self.code_lengths = [2]
            self.cid_map = None
            if isinstance(enc, StreamObject):
                self.code_lengths, _u, self.cid_map = parse_cmap(enc.get_data())
            elif name(enc) not in ('Identity-H', 'Identity-V'):
                warn('Font %s uses the CMap %s, which is read as 2-byte codes.' % (self.basename, name(enc)))
            if name(enc).endswith('-V'):
                warn('Vertical text (font %s) was placed horizontally.' % self.basename)
            c2g = R(df.get('/CIDToGIDMap'))
            self.cid2gid = None
            if isinstance(c2g, StreamObject):
                data = c2g.get_data()
                self.cid2gid = [int.from_bytes(data[i:i + 2], 'big') for i in range(0, len(data), 2)]
        else:
            desc = R(obj.get('/FontDescriptor'))
            self.code_lengths = [1]
            fc = int(num(obj.get('/FirstChar'), 0))
            ws = R(obj.get('/Widths')) or []
            missing = num(desc.get('/MissingWidth'), 0) if desc is not None else 0
            self.simple_widths = {fc + i: num(v) for i, v in enumerate(ws)}
            self.missing_width = missing
            if self.type3:
                self.font_matrix = tuple(nums(obj['/FontMatrix']))
            self._simple_encoding(obj)
            if not ws and not self.type3:
                self._std14_widths()
        self.desc = desc
        self.family, self.weight, self.italic, self.condensed = font_family_style(self.basename, desc)
        self.to_unicode = {}
        tu = R(obj.get('/ToUnicode'))
        if isinstance(tu, StreamObject):
            try:
                _l, self.to_unicode, _c = parse_cmap(tu.get_data())
            except Exception:
                pass
        self._load_program(desc)

    # --- simple font encoding
    def _simple_encoding(self, obj):
        enc = R(obj.get('/Encoding'))
        base = None
        diffs = {}
        if isinstance(enc, str):
            base = name(enc)
        elif isinstance(enc, dict):
            if '/BaseEncoding' in enc:
                base = name(enc['/BaseEncoding'])
            code = 0
            for v in R(enc.get('/Differences', [])) or []:
                v = R(v)
                if isinstance(v, str) and str(v).startswith('/'):
                    diffs[code] = name(v)
                    code += 1
                else:
                    code = int(num(v))
        self.base_encoding = base
        self.enc_names = _std_names(base) if base else None
        self.diffs = diffs

    def _std14_widths(self):
        try:
            from pypdf._codecs import core_font_metrics
            key = re.sub(r'^[A-Z]{6}\+', '', self.basename)
            m = core_font_metrics.get(key)
            if m is None:
                return
            cw = m.character_widths
            for code in range(256):
                ch = self.unicode_simple(code)
                if ch and ch in cw:
                    self.simple_widths[code] = cw[ch]
        except Exception:
            pass

    def gname(self, code):
        if code in self.diffs:
            return self.diffs[code]
        if self.enc_names:
            return self.enc_names[code] if code < len(self.enc_names) else None
        return None

    def unicode_simple(self, code):
        gn = self.gname(code)
        if gn:
            return glyph_unicode(gn)
        if self.base_encoding is None and self.outline_src and self.outline_src[0] == 'cff' and self.builtin_enc:
            gn = self.builtin_enc[code] if code < len(self.builtin_enc) else None
            if gn:
                return glyph_unicode(gn)
        chars = charset_encoding['/StandardEncoding']
        ch = chars[code] if code < len(chars) else ''
        return ch if isinstance(ch, str) else ''

    # --- decoding
    def codes(self, s):
        """Split a string into (code, nbytes)."""
        if not self.type0:
            return [(b, 1) for b in s]
        out = []
        i = 0
        lens = self.code_lengths
        while i < len(s):
            nb = lens[-1]
            if len(lens) > 1 and self.cid_map is not None:
                for L in lens:
                    if (L, int.from_bytes(s[i:i + L], 'big')) in self.cid_map:
                        nb = L
                        break
            out.append((int.from_bytes(s[i:i + nb], 'big'), nb))
            i += nb
        return out

    def cid(self, code, nb):
        if self.cid_map is not None:
            return self.cid_map.get((nb, code), code)
        return code

    def width(self, code, nb):
        if self.type0:
            return self.widths.get(self.cid(code, nb), self.dw)
        w = self.simple_widths.get(code, self.missing_width)
        if self.type3:
            return w * self.font_matrix[0] * 1000
        return w

    def unicode(self, code, nb):
        u = self.to_unicode.get((nb, code))
        if u:
            return u
        if not self.type0:
            return self.unicode_simple(code)
        return ''

    # --- outlines
    def _load_program(self, desc):
        self.builtin_enc = None
        if desc is None or self.type3:
            return
        try:
            if '/FontFile2' in desc:
                self._load_tt(R(desc['/FontFile2']).get_data())
            elif '/FontFile3' in desc:
                ff = R(desc['/FontFile3'])
                sub = name(ff.get('/Subtype'))
                data = ff.get_data()
                if sub == 'OpenType':
                    self._load_tt(data)
                else:
                    self._load_cff(data)
            elif '/FontFile' in desc:
                self._load_t1(R(desc['/FontFile']))
        except Exception as e:
            warn('Font %s could not be read (%s); its text was kept as editable text.' % (self.basename, e))
            self.outline_src = None

    def _load_tt(self, data):
        from fontTools.ttLib import TTFont
        tt = TTFont(io.BytesIO(data), lazy=True)
        if 'CFF ' in tt:
            cff = tt['CFF '].cff
            self._set_cff(cff)
            return
        self.tt = tt
        self.gs = tt.getGlyphSet()
        upm = tt['head'].unitsPerEm
        self.outline_src = ('tt', 1.0 / upm)
        self.tt_cmaps = {}
        if 'cmap' in tt:
            for t in tt['cmap'].tables:
                self.tt_cmaps[(t.platformID, t.platEncID)] = t.cmap

    def _load_cff(self, data):
        from fontTools.cffLib import CFFFontSet
        cff = CFFFontSet()
        cff.decompile(io.BytesIO(data), None)
        self._set_cff(cff)

    def _set_cff(self, cff):
        top = cff[cff.fontNames[0]]
        self.cff_top = top
        self.cff_cs = top.CharStrings
        self.cff_charset = list(top.charset)
        self.cff_is_cid = hasattr(top, 'ROS') or 'ROS' in getattr(top, 'rawDict', {})
        fm = list(getattr(top, 'FontMatrix', [0.001, 0, 0, 0.001, 0, 0]))
        self.cff_fm = tuple(fm)
        self.cff_fd_fm = None
        if self.cff_is_cid and hasattr(top, 'FDArray'):
            self.cff_fd_fm = []
            for fd in top.FDArray:
                raw = getattr(fd, 'rawDict', {})
                self.cff_fd_fm.append(tuple(raw['FontMatrix']) if 'FontMatrix' in raw else None)
            if all(m is None for m in self.cff_fd_fm):
                self.cff_fd_fm = None
        enc = getattr(top, 'Encoding', None)
        if isinstance(enc, list):
            self.builtin_enc = enc
        elif enc == 'StandardEncoding':
            self.builtin_enc = _std_names('StandardEncoding')
        self.outline_src = ('cff', None)

    def _load_t1(self, st):
        from fontTools.t1Lib import T1Font
        import tempfile
        data = st.get_data()
        l1 = int(num(st.get('/Length1')))
        l2 = int(num(st.get('/Length2')))
        segs = [(1, data[:l1]), (2, data[l1:l1 + l2]), (1, data[l1 + l2:])]
        pfb = b''.join(b'\x80' + bytes([t]) + len(d).to_bytes(4, 'little') + d for t, d in segs if d) + b'\x80\x03'
        fd, path = tempfile.mkstemp(suffix='.pfb')
        with os.fdopen(fd, 'wb') as f:
            f.write(pfb)
        try:
            t1 = T1Font(path)
            t1.parse()
            self.t1_gs = t1.getGlyphSet()
            fm = t1.font.get('FontMatrix', [0.001, 0, 0, 0.001, 0, 0])
            self.t1_fm = tuple(fm)
            enc = t1.font.get('Encoding')
            if isinstance(enc, list):
                self.builtin_enc = enc
            elif enc == 'StandardEncoding' or enc is None:
                self.builtin_enc = _std_names('StandardEncoding')
            self.outline_src = ('t1', None)
        finally:
            os.unlink(path)

    def glyph(self, code, nb):
        """Glyph outline in text space (1 unit = font size), or None."""
        key = (code, nb)
        if key in self.glyphs:
            return self.glyphs[key]
        segs = None
        if self.outline_src:
            try:
                segs = self._glyph(code, nb)
            except Exception as e:
                warn('A glyph in font %s could not be drawn (%s).' % (self.basename, e))
        self.glyphs[key] = segs
        return segs

    def _draw(self, drawable, fm, glyphset=None):
        p = GlyphPen(glyphset).pen
        drawable.draw(p)
        return [(op, [apply(fm, x, y) for x, y in pts]) for op, pts in p.segs]

    def _glyph(self, code, nb):
        kind = self.outline_src[0]
        if kind == 'cff':
            gname = None
            gid = None
            if self.type0:
                cid = self.cid(code, nb)
                if self.cff_is_cid:
                    gname = 'cid%05d' % cid
                    if gname not in self.cff_cs:
                        gname = None
                else:
                    gid = cid
            else:
                gname = self.gname(code)
                if (gname is None or gname not in self.cff_cs) and self.builtin_enc and code < len(self.builtin_enc):
                    gname = self.builtin_enc[code]
            if gname is None and gid is not None and gid < len(self.cff_charset):
                gname = self.cff_charset[gid]
            if gname is None or gname not in self.cff_cs:
                return []
            fm = self.cff_fm
            if self.cff_fd_fm:
                cs = self.cff_cs[gname]
                fdi = getattr(cs, 'fdSelectIndex', None)
                if fdi is not None and fdi < len(self.cff_fd_fm) and self.cff_fd_fm[fdi]:
                    fm = mul(self.cff_fd_fm[fdi], fm)
            return self._draw(self.cff_cs[gname], fm)
        if kind == 't1':
            gname = self.gname(code)
            if (gname is None or gname not in self.t1_gs) and self.builtin_enc and code < len(self.builtin_enc):
                gname = self.builtin_enc[code]
            if gname is None or gname not in self.t1_gs:
                return []
            return self._draw(self.t1_gs[gname], self.t1_fm)
        # TrueType
        s = self.outline_src[1]
        fm = (s, 0, 0, s, 0, 0)
        gid = None
        if self.type0:
            cid = self.cid(code, nb)
            gid = self.cid2gid[cid] if self.cid2gid is not None and cid < len(self.cid2gid) else cid
        else:
            symbolic = self.desc is not None and int(num(self.desc.get('/Flags'), 0)) & 4
            cm = self.tt_cmaps
            if (3, 0) in cm and (symbolic or (3, 1) not in cm):
                gn = cm[(3, 0)].get(0xF000 + code) or cm[(3, 0)].get(code)
                if gn:
                    return self._draw(self.gs[gn], fm, self.gs)
            if (3, 1) in cm:
                u = self.unicode_simple(code)
                gn = cm[(3, 1)].get(ord(u)) if u else None
                if gn:
                    return self._draw(self.gs[gn], fm, self.gs)
            if (1, 0) in cm:
                gn = cm[(1, 0)].get(code)
                if gn:
                    return self._draw(self.gs[gn], fm, self.gs)
            gname = self.gname(code)
            if gname and gname in self.gs:
                return self._draw(self.gs[gname], fm, self.gs)
            gid = code
        try:
            gn = self.tt.getGlyphName(gid)
        except Exception:
            return []
        if gn not in self.gs:
            return []
        return self._draw(self.gs[gn], fm, self.gs)


# ---------------------------------------------------------------- graphics state
class GState:
    __slots__ = ('ctm', 'fill_cs', 'fill', 'stroke_cs', 'stroke', 'fill_alpha', 'stroke_alpha',
                 'blend', 'smask', 'lw', 'cap', 'join', 'miter', 'dash', 'opened',
                 'font', 'fs', 'tc', 'tw', 'th', 'tl', 'ts', 'tr')

    def __init__(self):
        self.ctm = (1, 0, 0, 1, 0, 0)
        self.fill_cs = self.stroke_cs = DeviceGray()
        self.fill = self.stroke = [0.0]
        self.fill_alpha = self.stroke_alpha = 1.0
        self.blend = None
        self.smask = None
        self.lw, self.cap, self.join, self.miter, self.dash = 1.0, 0, 0, 10.0, ([], 0)
        self.opened = 0
        self.font, self.fs, self.tc, self.tw, self.th, self.tl, self.ts, self.tr = None, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0

    def copy(self):
        g = GState.__new__(GState)
        for k in GState.__slots__:
            setattr(g, k, getattr(self, k))
        g.opened = 0
        return g


CAPS = ['butt', 'round', 'square']
JOINS = ['miter', 'round', 'bevel']
BLEND = {'Multiply': 'multiply', 'Screen': 'screen', 'Overlay': 'overlay', 'Darken': 'darken',
         'Lighten': 'lighten', 'ColorDodge': 'color-dodge', 'ColorBurn': 'color-burn',
         'HardLight': 'hard-light', 'SoftLight': 'soft-light', 'Difference': 'difference',
         'Exclusion': 'exclusion', 'Hue': 'hue', 'Saturation': 'saturation', 'Color': 'color',
         'Luminosity': 'luminosity'}


# ---------------------------------------------------------------- converter
class Converter:
    def __init__(self, reader, text_mode='outlines'):
        self.reader = reader
        self.text_mode = text_mode
        self.cs = ColorSpaces(reader)
        self.defs = Node('defs')
        self.ids = {}
        self.fonts = {}
        self.pattern_ids = {}
        self.image_ids = {}
        self.ocg_off = set()
        try:
            ocp = R(R(reader.trailer['/Root']).get('/OCProperties'))
            if ocp is not None:
                for ref in R(R(ocp.get('/D', {})).get('/OFF', [])) or []:
                    self.ocg_off.add(obj_key(ref))
        except Exception:
            pass
        self.stats = {'paths': 0, 'clips': 0, 'texts': 0, 'images': 0, 'gradients': 0}

    def new_id(self, prefix):
        n = self.ids.get(prefix, 0) + 1
        self.ids[prefix] = n
        return '%s%d' % (prefix, n)

    # ---- page
    def page(self, page, offset_x, root):
        box = [num(v) for v in (page.cropbox if '/CropBox' in page else page.mediabox)]
        x0, y0, x1, y1 = min(box[0], box[2]), min(box[1], box[3]), max(box[0], box[2]), max(box[1], box[3])
        rot = int(num(page.get('/Rotate'), 0)) % 360
        w, h = (x1 - x0), (y1 - y0)
        # PDF user space -> page px (y down), including /Rotate
        if rot == 0:
            m = (PX, 0, 0, -PX, -x0 * PX, y1 * PX)
            pw, ph = w, h
        elif rot == 90:
            m = (0, PX, PX, 0, -y0 * PX, -x0 * PX)
            pw, ph = h, w
        elif rot == 180:
            m = (-PX, 0, 0, PX, x1 * PX, -y0 * PX)
            pw, ph = w, h
        else:
            m = (0, -PX, -PX, 0, y1 * PX, x1 * PX)
            pw, ph = h, w
        m = mul(m, (1, 0, 0, 1, offset_x, 0))
        res = R(page.get('/Resources')) or DictionaryObject()
        cont = R(page.get('/Contents'))
        if cont is None:
            data = b''
        elif isinstance(cont, list):
            data = b'\n'.join(R(c).get_data() for c in cont)
        else:
            data = cont.get_data()
        self.run(data, res, m, root, GState())
        return pw * PX, ph * PX

    # ---- interpreter
    def run(self, data, res, base, parent, gs, depth=0):
        if depth > 12:
            warn('Forms nested too deeply were skipped.')
            return
        stack = []
        nodes = [parent]           # open SVG groups
        owners = [None]            # GState that opened each group (None for marked content)
        mc = []                    # marked-content stack: node or None
        segs = []
        cur = start = (0.0, 0.0)
        clip = None
        tm = tlm = (1, 0, 0, 1, 0, 0)
        text = None                # current BT..ET collector
        page_res = res
        cs_db = self.cs

        def top():
            return nodes[-1]

        def close_state(state):
            n = state.opened
            while n and len(nodes) > 1:
                owner = owners.pop()
                nodes.pop()
                if owner is state:
                    n -= 1

        def end_path():
            nonlocal segs, clip
            if clip and segs:
                cid = self.new_id('clip')
                cp = self.defs.add(Node('clipPath', [('id', cid), ('clipPathUnits', 'userSpaceOnUse')]))
                attrs = [('d', path_d(segs, mul(gs.ctm, base)))]
                if clip == 'evenodd':
                    attrs.append(('clip-rule', 'evenodd'))
                cp.add(Node('path', attrs))
                g = top().add(Node('g', [('clip-path', 'url(#%s)' % cid)]))
                nodes.append(g)
                owners.append(gs)
                gs.opened += 1
                self.stats['clips'] += 1
            clip = None
            segs = []

        for tok in tokenize(data):
            if tok[0] == 'BI':
                self.inline_image(tok[1], tok[2], gs, base, top(), res)
                continue
            op, a = tok
            try:
                if op in ('m', 'l', 'c', 'v', 'y', 'h', 're'):
                    if op == 'm':
                        cur = start = (a[0], a[1])
                        segs.append(('M', [cur]))
                    elif op == 'l':
                        cur = (a[0], a[1])
                        segs.append(('L', [cur]))
                    elif op == 'c':
                        segs.append(('C', [(a[0], a[1]), (a[2], a[3]), (a[4], a[5])]))
                        cur = (a[4], a[5])
                    elif op == 'v':
                        segs.append(('C', [cur, (a[0], a[1]), (a[2], a[3])]))
                        cur = (a[2], a[3])
                    elif op == 'y':
                        segs.append(('C', [(a[0], a[1]), (a[2], a[3]), (a[2], a[3])]))
                        cur = (a[2], a[3])
                    elif op == 'h':
                        segs.append(('Z', []))
                        cur = start
                    else:
                        x, y, w, h = a[:4]
                        segs += [('M', [(x, y)]), ('L', [(x + w, y)]), ('L', [(x + w, y + h)]), ('L', [(x, y + h)]), ('Z', [])]
                        cur = start = (x, y)
                elif op in ('S', 's', 'f', 'F', 'f*', 'B', 'B*', 'b', 'b*', 'n'):
                    if op in ('s', 'b', 'b*'):
                        segs.append(('Z', []))
                    if op != 'n' and segs:
                        self.paint(segs, gs, base, top(), res,
                                   fill=op not in ('S', 's'),
                                   stroke=op in ('S', 's', 'B', 'B*', 'b', 'b*'),
                                   evenodd='*' in op)
                    end_path()
                elif op == 'W':
                    clip = 'nonzero'
                elif op == 'W*':
                    clip = 'evenodd'
                elif op == 'q':
                    stack.append(gs)
                    gs = gs.copy()
                elif op == 'Q':
                    if stack:
                        close_state(gs)
                        gs = stack.pop()
                elif op == 'cm':
                    gs.ctm = mul(tuple(a[:6]), gs.ctm)
                elif op == 'w':
                    gs.lw = a[0]
                elif op == 'J':
                    gs.cap = int(a[0])
                elif op == 'j':
                    gs.join = int(a[0])
                elif op == 'M':
                    gs.miter = a[0]
                elif op == 'd':
                    gs.dash = (list(a[0]), a[1])
                elif op == 'gs':
                    self.ext_gstate(a[0], gs, res, base)
                elif op in ('CS', 'cs'):
                    c = cs_db.by_name(a[0], res)
                    if op == 'CS':
                        gs.stroke_cs, gs.stroke = c, c.initial()
                    else:
                        gs.fill_cs, gs.fill = c, c.initial()
                elif op in ('SC', 'SCN', 'sc', 'scn'):
                    vals = [v for v in a if isinstance(v, float)]
                    pat = a[-1] if a and isinstance(a[-1], str) else None
                    val = (vals, pat) if pat else vals
                    if op in ('SC', 'SCN'):
                        gs.stroke = val
                    else:
                        gs.fill = val
                elif op in ('G', 'g', 'RG', 'rg', 'K', 'k'):
                    c = cs_db.device({'G': 'DeviceGray', 'RG': 'DeviceRGB', 'K': 'DeviceCMYK'}[op.upper()], res)
                    if op.isupper():
                        gs.stroke_cs, gs.stroke = c, list(a)
                    else:
                        gs.fill_cs, gs.fill = c, list(a)
                elif op == 'sh':
                    self.shading_fill(a[0], gs, base, top(), res)
                elif op == 'Do':
                    self.do_xobject(a[0], gs, base, top(), res, depth)
                elif op == 'BT':
                    tm = tlm = (1, 0, 0, 1, 0, 0)
                    text = TextBlock()
                elif op == 'ET':
                    if text is not None:
                        self.flush_text(text, top())
                    text = None
                elif op == 'Tf':
                    gs.font = self.font(a[0], res)
                    gs.fs = a[1]
                elif op == 'Tc':
                    gs.tc = a[0]
                elif op == 'Tw':
                    gs.tw = a[0]
                elif op == 'Tz':
                    gs.th = a[0] / 100.0
                elif op == 'TL':
                    gs.tl = a[0]
                elif op == 'Ts':
                    gs.ts = a[0]
                elif op == 'Tr':
                    gs.tr = int(a[0])
                elif op == 'Tm':
                    tm = tlm = tuple(a[:6])
                elif op in ('Td', 'TD'):
                    if op == 'TD':
                        gs.tl = -a[1]
                    tm = tlm = mul((1, 0, 0, 1, a[0], a[1]), tlm)
                elif op == 'T*':
                    tm = tlm = mul((1, 0, 0, 1, 0, -gs.tl), tlm)
                elif op in ('Tj', 'TJ', "'", '"'):
                    if op == "'":
                        tm = tlm = mul((1, 0, 0, 1, 0, -gs.tl), tlm)
                    elif op == '"':
                        gs.tw, gs.tc = a[0], a[1]
                        tm = tlm = mul((1, 0, 0, 1, 0, -gs.tl), tlm)
                    items = a[-1] if op == 'TJ' else [a[-1]]
                    if text is None:
                        text = TextBlock()
                    tm = self.show_text(items, gs, tm, base, text, res, depth)
                elif op == 'BDC' or op == 'BMC':
                    node = None
                    if op == 'BDC' and len(a) >= 2:
                        props = a[1]
                        if isinstance(props, str):
                            pd = R(R(res.get('/Properties', {})).get('/' + props)) if res is not None else None
                        else:
                            pd = props
                        label = None
                        hidden = False
                        if pd is not None and hasattr(pd, 'get'):
                            t = pd.get('/Type')
                            if t is not None and name(t) == 'OCG':
                                label = str(R(pd.get('/Name', 'Layer')))
                                ref = res.get('/Properties', {})
                                ref = R(ref).get('/' + props) if isinstance(props, str) else None
                                hidden = ref is not None and obj_key(ref) in self.ocg_off
                            elif t is not None and name(t) == 'OCMD':
                                label = None
                            elif '/Title' in pd:
                                label = str(R(pd['/Title']))
                        if label is not None and a[0] in ('OC', 'Layer'):
                            is_layer = all(n2.tag in ('svg', 'root') or any(k == 'inkscape:groupmode' for k, _ in n2.attrs) for n2 in nodes)
                            attrs = [('inkscape:label', esc(clean_xml_text(label)))]
                            if is_layer:
                                attrs.insert(0, ('inkscape:groupmode', 'layer'))
                            if hidden:
                                attrs.append(('style', 'display:none'))
                            node = top().add(Node('g', attrs))
                            nodes.append(node)
                            owners.append(None)
                    mc.append(node)
                elif op == 'EMC':
                    node = mc.pop() if mc else None
                    if node is not None and node in nodes:
                        while nodes[-1] is not node:
                            nodes.pop()
                            owners.pop()
                        nodes.pop()
                        owners.pop()
                elif op in ('d0', 'd1', 'ri', 'i', 'MP', 'DP', 'BX', 'EX', '__end__'):
                    pass
                else:
                    warn('Unknown PDF operator %s was skipped.' % op)
            except (IndexError, TypeError, ValueError, KeyError, ZeroDivisionError) as e:
                warn('A malformed "%s" instruction was skipped (%s).' % (op, e))
        while len(stack):
            close_state(gs)
            gs = stack.pop()

    # ---- painting
    def paint_attrs(self, gs, base, which, segs_bbox=None, res=None):
        """Returns (value, opacity) for fill/stroke."""
        cs = gs.fill_cs if which == 'fill' else gs.stroke_cs
        val = gs.fill if which == 'fill' else gs.stroke
        alpha = gs.fill_alpha if which == 'fill' else gs.stroke_alpha
        if cs.is_pattern:
            comps, pat = val if isinstance(val, tuple) else ([], None)
            if pat is None:
                return 'none', alpha
            ref = self.pattern(pat, comps, cs, gs, base, res)
            return ref or 'none', alpha
        if isinstance(val, tuple):
            val = val[0]
        if cs.none:
            return 'none', alpha
        if len(val) < cs.n:
            val = list(val) + cs.initial()[len(val):]
        return to_hex(cs.rgb(val)), alpha

    def paint(self, segs, gs, base, parent, res, fill, stroke, evenodd):
        m = mul(gs.ctm, base)
        attrs = []
        keep_transform = stroke and not is_similarity(m)
        if keep_transform:
            d = path_d(segs)
            sc = 1.0
        else:
            d = path_d(segs, m)
            sc = scale_of(m)
        attrs.append(('d', d))
        if keep_transform:
            attrs.append(('transform', fmt_m(m)))
        if fill:
            col, alpha = self.paint_attrs(gs, base, 'fill', res=res)
            attrs.append(('fill', col))
            if alpha < 1:
                attrs.append(('fill-opacity', fmt(alpha)))
            if evenodd:
                attrs.append(('fill-rule', 'evenodd'))
        else:
            attrs.append(('fill', 'none'))
        if stroke:
            col, alpha = self.paint_attrs(gs, base, 'stroke', res=res)
            lw = gs.lw * sc if gs.lw > 0 else 0.288 * (1.0 if not keep_transform else 1 / max(scale_of(m), 1e-9))
            attrs.append(('stroke', col))
            attrs.append(('stroke-width', fmt(lw) if lw >= 0.0005 else '%.3g' % lw))
            if alpha < 1:
                attrs.append(('stroke-opacity', fmt(alpha)))
            if gs.cap:
                attrs.append(('stroke-linecap', CAPS[min(gs.cap, 2)]))
            if gs.join:
                attrs.append(('stroke-linejoin', JOINS[min(gs.join, 2)]))
            elif abs(gs.miter - 4) > 1e-3:
                attrs.append(('stroke-miterlimit', fmt(max(gs.miter, 1))))
            dash = [v for v in gs.dash[0] if isinstance(v, float)]
            if dash and any(v > 0 for v in dash):
                attrs.append(('stroke-dasharray', ','.join(fmt(v * sc) for v in dash)))
                if gs.dash[1]:
                    attrs.append(('stroke-dashoffset', fmt(gs.dash[1] * sc)))
        self.add_effects(attrs, gs)
        parent.add(Node('path', attrs))
        self.stats['paths'] += 1

    def add_effects(self, attrs, gs):
        if gs.blend:
            attrs.append(('style', 'mix-blend-mode:%s' % gs.blend))
        if gs.smask:
            attrs.append(('mask', 'url(#%s)' % gs.smask))

    # ---- extended graphics state
    def ext_gstate(self, nm, gs, res, base):
        d = R(R(res.get('/ExtGState', {})).get('/' + nm))
        if d is None:
            return
        for k, v in d.items():
            v = R(v)
            if k == '/LW':
                gs.lw = num(v)
            elif k == '/LC':
                gs.cap = int(num(v))
            elif k == '/LJ':
                gs.join = int(num(v))
            elif k == '/ML':
                gs.miter = num(v)
            elif k == '/D':
                gs.dash = (nums(v[0]), num(v[1]))
            elif k == '/ca':
                gs.fill_alpha = num(v, 1)
            elif k == '/CA':
                gs.stroke_alpha = num(v, 1)
            elif k == '/BM':
                bm = name(v[0]) if isinstance(v, list) else name(v)
                gs.blend = BLEND.get(bm)
            elif k == '/SMask':
                if isinstance(v, str) and name(v) == 'None':
                    gs.smask = None
                else:
                    gs.smask = self.soft_mask(v, gs, base)
            elif k == '/Font':
                gs.font = self.font_obj(v[0])
                gs.fs = num(v[1])

    def soft_mask(self, d, gs, base):
        try:
            kind = name(d.get('/S', '/Luminosity'))
            form = R(d['/G'])
            mid = self.new_id('mask')
            mask = self.defs.add(Node('mask', [('id', mid), ('maskUnits', 'userSpaceOnUse'),
                                               ('x', '-100000'), ('y', '-100000'), ('width', '200000'), ('height', '200000')]))
            if kind == 'Alpha':
                mask.attrs.append(('style', 'mask-type:alpha'))
            else:
                bc = d.get('/BC')
                if bc is not None:
                    grp = R(form.get('/Group'))
                    gcs = self.cs.get(grp['/CS'], None) if grp is not None and '/CS' in grp else DeviceGray()
                    col = to_hex(gcs.rgb(nums(bc)))
                    mask.add(Node('rect', [('x', '-100000'), ('y', '-100000'), ('width', '200000'), ('height', '200000'), ('fill', col)]))
            inner = GState()
            inner.ctm = gs.ctm
            fm = tuple(nums(form['/Matrix'])) if '/Matrix' in form else (1, 0, 0, 1, 0, 0)
            inner.ctm = mul(fm, inner.ctm)
            self.run(form.get_data(), R(form.get('/Resources')) or DictionaryObject(), base, mask, inner, 1)
            return mid
        except Exception as e:
            warn('A transparency mask could not be converted (%s) and was left out.' % e)
            return None

    # ---- patterns and shadings
    def pattern(self, pname, comps, pcs, gs, base, res):
        pobj = R(R(res.get('/Pattern', {})).get('/' + pname)) if res is not None else None
        if pobj is None:
            warn('Missing pattern %s; painted nothing.' % pname)
            return None
        ptype = int(num(pobj.get('/PatternType'), 1))
        pm = tuple(nums(pobj['/Matrix'])) if '/Matrix' in pobj else (1, 0, 0, 1, 0, 0)
        key = (obj_key(pobj), tuple(base), tuple(comps) if comps else None)
        if key in self.pattern_ids:
            return 'url(#%s)' % self.pattern_ids[key]
        if ptype == 2:
            gid = self.gradient(R(pobj['/Shading']), mul(pm, base))
        else:
            gid = self.tiling(pobj, pm, base, comps, pcs)
        if gid is None:
            return None
        self.pattern_ids[key] = gid
        return 'url(#%s)' % gid

    def tiling(self, pobj, pm, base, comps, pcs):
        pid = self.new_id('pattern')
        bb = nums(pobj['/BBox'])
        xs, ys = num(pobj.get('/XStep'), bb[2] - bb[0]), num(pobj.get('/YStep'), bb[3] - bb[1])
        node = self.defs.add(Node('pattern', [('id', pid), ('patternUnits', 'userSpaceOnUse'),
                                              ('x', fmt(bb[0])), ('y', fmt(bb[1])), ('width', fmt(abs(xs))), ('height', fmt(abs(ys))),
                                              ('patternTransform', fmt_m(mul(pm, base)))]))
        inner = GState()
        if int(num(pobj.get('/PaintType'), 1)) == 2 and pcs.under is not None:
            inner.fill_cs = inner.stroke_cs = pcs.under
            inner.fill = inner.stroke = comps
        self.run(pobj.get_data(), R(pobj.get('/Resources')) or DictionaryObject(), (1, 0, 0, 1, 0, 0), node, inner, 1)
        return pid

    def shading_colors(self, sh, t0, t1, samples=65):
        fn = PdfFunction(sh['/Function'])
        cs = self.cs.get(sh['/ColorSpace'], None)
        out = []
        for i in range(samples):
            t = t0 + (t1 - t0) * i / (samples - 1)
            out.append((i / (samples - 1), cs.rgb(fn([t]))))
        # drop stops that linear interpolation reproduces
        keep = [out[0]]
        for i in range(1, len(out) - 1):
            p, c, n = keep[-1], out[i], out[i + 1]
            f = (c[0] - p[0]) / (n[0] - p[0])
            if any(abs(p[1][k] + f * (n[1][k] - p[1][k]) - c[1][k]) > 0.6 / 255 for k in range(3)):
                keep.append(c)
        keep.append(out[-1])
        return keep

    def gradient(self, sh, m):
        st = int(num(sh.get('/ShadingType')))
        if st not in (2, 3):
            warn('A mesh gradient (shading type %d) was replaced by a flat color.' % st)
            try:
                cs = self.cs.get(sh['/ColorSpace'], None)
                return None if cs is None else None
            except Exception:
                return None
        coords = nums(sh['/Coords'])
        dom = nums(sh['/Domain']) if '/Domain' in sh else [0.0, 1.0]
        ext = [bool(R(x)) for x in R(sh['/Extend'])] if '/Extend' in sh else [False, False]
        stops = self.shading_colors(sh, dom[0], dom[1])
        gid = self.new_id('gradient')
        if st == 2:
            x0, y0, x1, y1 = coords
            lo, hi = 0.0, 1.0
            if not ext[0] or not ext[1]:
                # widen the vector a little so the unextended ends become transparent
                lo, hi = (-0.002 if not ext[0] else 0.0), (1.002 if not ext[1] else 1.0)
            dx, dy = x1 - x0, y1 - y0
            node = Node('linearGradient', [('id', gid), ('gradientUnits', 'userSpaceOnUse'),
                                           ('x1', fmt(x0 + lo * dx)), ('y1', fmt(y0 + lo * dy)),
                                           ('x2', fmt(x0 + hi * dx)), ('y2', fmt(y0 + hi * dy)),
                                           ('gradientTransform', fmt_m(m))])
            span = hi - lo
            if lo < 0:
                node.add(Node('stop', [('offset', '0'), ('stop-color', to_hex(stops[0][1])), ('stop-opacity', '0')]))
            for off, rgb in stops:
                node.add(Node('stop', [('offset', '%.5f' % ((off - lo) / span)), ('stop-color', to_hex(rgb))]))
            if hi > 1:
                node.add(Node('stop', [('offset', '1'), ('stop-color', to_hex(stops[-1][1])), ('stop-opacity', '0')]))
        else:
            x0, y0, r0, x1, y1, r1 = coords
            if r1 <= 0:
                return None
            # focal point = start circle center; inner radius folded into the stops
            f0 = r0 / r1 if r1 else 0
            if abs(x0 - x1) > 1e-6 or abs(y0 - y1) > 1e-6:
                if r0 > 0:
                    warn('A radial gradient with two different circles was approximated.')
            hi = 1.0 if ext[1] else 1.002
            node = Node('radialGradient', [('id', gid), ('gradientUnits', 'userSpaceOnUse'),
                                           ('cx', fmt(x1)), ('cy', fmt(y1)), ('r', fmt(r1 * hi)),
                                           ('fx', fmt(x0)), ('fy', fmt(y0)),
                                           ('gradientTransform', fmt_m(m))])
            if not ext[0] and f0 > 0:
                node.add(Node('stop', [('offset', '%.5f' % (f0 / hi)), ('stop-color', to_hex(stops[0][1])), ('stop-opacity', '0')]))
            for off, rgb in stops:
                node.add(Node('stop', [('offset', '%.5f' % ((f0 + off * (1 - f0)) / hi)), ('stop-color', to_hex(rgb))]))
            if hi > 1:
                node.add(Node('stop', [('offset', '1'), ('stop-color', to_hex(stops[-1][1])), ('stop-opacity', '0')]))
        self.defs.add(node)
        self.stats['gradients'] += 1
        return gid

    def shading_fill(self, nm, gs, base, parent, res):
        sh = R(R(res.get('/Shading', {})).get('/' + nm))
        if sh is None:
            return
        m = mul(gs.ctm, base)
        gid = self.gradient(sh, m)
        if gid is None:
            return
        # cover the whole area; the clip that encloses `sh` limits it
        if '/BBox' in sh:
            b = nums(sh['/BBox'])
            segs = [('M', [(b[0], b[1])]), ('L', [(b[2], b[1])]), ('L', [(b[2], b[3])]), ('L', [(b[0], b[3])]), ('Z', [])]
            d = path_d(segs, m)
        else:
            d = 'M-50000,-50000 L50000,-50000 L50000,50000 L-50000,50000 Z'
        attrs = [('d', d), ('fill', 'url(#%s)' % gid)]
        if gs.fill_alpha < 1:
            attrs.append(('fill-opacity', fmt(gs.fill_alpha)))
        self.add_effects(attrs, gs)
        parent.add(Node('path', attrs))
        self.stats['paths'] += 1

    # ---- xobjects and images
    def do_xobject(self, nm, gs, base, parent, res, depth):
        xo = R(R(res.get('/XObject', {})).get('/' + nm))
        if xo is None:
            warn('Missing object %s was skipped.' % nm)
            return
        sub = name(xo.get('/Subtype'))
        if sub == 'Image':
            self.image(xo, gs, base, parent, res)
        elif sub == 'Form':
            inner = gs.copy()
            fm = tuple(nums(xo['/Matrix'])) if '/Matrix' in xo else (1, 0, 0, 1, 0, 0)
            inner.ctm = mul(fm, gs.ctm)
            target = parent
            grp = R(xo.get('/Group'))
            if grp is not None and name(grp.get('/S')) == 'Transparency' and (gs.fill_alpha < 1 or gs.blend or gs.smask):
                attrs = []
                if gs.fill_alpha < 1:
                    attrs.append(('opacity', fmt(gs.fill_alpha)))
                self.add_effects(attrs, gs)
                target = parent.add(Node('g', attrs))
                inner.fill_alpha = inner.stroke_alpha = 1.0
                inner.blend = inner.smask = None
            fres = R(xo.get('/Resources')) or res
            self.run(xo.get_data(), fres, base, target, inner, depth + 1)
        else:
            warn('Object type %s was skipped.' % sub)

    def image(self, xo, gs, base, parent, res, inline_params=None, inline_data=None):
        try:
            href, w, h = self.image_href(xo, gs, res, inline_params, inline_data)
        except Exception as e:
            warn('An image could not be converted (%s) and was left out.' % e)
            return
        if href is None:
            return
        m = mul(mul((1.0 / w, 0, 0, -1.0 / h, 0, 1), gs.ctm), base)
        attrs = [('x', '0'), ('y', '0'), ('width', str(w)), ('height', str(h)),
                 ('preserveAspectRatio', 'none'), ('transform', fmt_m(m)), ('xlink:href', href)]
        if gs.fill_alpha < 1:
            attrs.append(('opacity', fmt(gs.fill_alpha)))
        self.add_effects(attrs, gs)
        parent.add(Node('image', attrs))
        self.stats['images'] += 1

    def inline_image(self, params, data, gs, base, parent, res):
        self.image(None, gs, base, parent, res, params, data)

    def image_href(self, xo, gs, res, ip=None, idata=None):
        if xo is not None:
            key = obj_key(xo)
            stencil = bool(R(xo.get('/ImageMask', False)))
            if key in self.image_ids and not stencil:
                return self.image_ids[key]
        else:
            key = None
        abbrev = {'W': '/Width', 'H': '/Height', 'BPC': '/BitsPerComponent', 'CS': '/ColorSpace', 'F': '/Filter',
                  'D': '/Decode', 'IM': '/ImageMask', 'DP': '/DecodeParms', 'I': '/Interpolate'}
        if ip is not None:
            d = {}
            for k, v in ip.items():
                d[abbrev.get(k, '/' + k)] = v
            get = d.get
        else:
            get = xo.get
        w, h = int(num(get('/Width'))), int(num(get('/Height')))
        filters = R(get('/Filter'))
        filters = [name(f) for f in (filters if isinstance(filters, list) else [filters] if filters else [])]
        stencil = bool(R(get('/ImageMask', False)))
        bpc = int(num(get('/BitsPerComponent'), 1 if stencil else 8))
        decode = R(get('/Decode'))
        decode = [num(v) for v in decode] if decode else None

        if ip is not None:
            raw = idata
            fl = [{'Fl': 'FlateDecode', 'AHx': 'ASCIIHexDecode', 'A85': 'ASCII85Decode', 'DCT': 'DCTDecode', 'LZW': 'LZWDecode', 'RL': 'RunLengthDecode'}.get(f, f) for f in filters]
            if fl and fl[-1] != 'DCTDecode':
                from pypdf import filters as pf
                for f in fl:
                    if f == 'FlateDecode':
                        raw = zlib.decompress(raw)
                    elif f == 'ASCIIHexDecode':
                        raw = pf.ASCIIHexDecode.decode(raw)
                    elif f == 'ASCII85Decode':
                        raw = pf.ASCII85Decode.decode(raw)
                    elif f == 'RunLengthDecode':
                        raw = pf.RunLengthDecode.decode(raw)
                    elif f == 'LZWDecode':
                        raw = pf.LZWDecode.decode(raw)
            filters = fl
            data = raw
            csobj = get('/ColorSpace')
            if isinstance(csobj, str):
                csn = {'G': 'DeviceGray', 'RGB': 'DeviceRGB', 'CMYK': 'DeviceCMYK', 'I': 'Indexed'}.get(csobj, csobj)
                cs = self.cs.device(csn, res) or self.cs.by_name(csn, res)
            elif csobj is not None:
                cs = self.cs._make(csobj, res)
            else:
                cs = DeviceGray()
        else:
            cs = None if stencil else (self.cs.get(xo['/ColorSpace'], res) if '/ColorSpace' in xo else None)
            if filters and filters[-1] in ('DCTDecode', 'JPXDecode'):
                data = xo._data
                if len(filters) > 1:
                    # earlier filters (e.g. Flate around a JPEG) are rare; decode them
                    from pypdf.filters import decode_stream_data
                    tmp = StreamObject()
                    tmp._data = xo._data
                    tmp[NameObj('/Filter')] = ArrayObject([NameObj('/' + f) for f in filters[:-1]])
                    data = decode_stream_data(tmp)
            else:
                data = xo.get_data()

        if stencil:
            img = self.decode_samples(data, w, h, 1, bpc, None)
            invert = decode is not None and decode[0] > decode[1]
            alpha = img if invert else ImageChops.invert(img)
            col, a = self.paint_attrs(gs, None, 'fill', res=res)
            if not col.startswith('#'):
                col = '#000000'
            rgb = Image.new('RGB', (w, h), col)
            rgb.putalpha(alpha)
            return self.encode(rgb, None, None), w, h

        jpeg_pass = False
        if filters and filters[-1] == 'DCTDecode':
            img = Image.open(io.BytesIO(data))
            img.load()
            if img.mode == 'CMYK':
                if 'adobe' in img.info:
                    img = ImageChops.invert(img)
                if decode and decode[0] > decode[1]:
                    img = ImageChops.invert(img)
                img = self.cmyk_image(img, cs)
            elif isinstance(cs, (DeviceRGB, DeviceGray)) and not decode:
                jpeg_pass = True
            else:
                img = self.convert_image(img, cs, decode)
        elif filters and filters[-1] == 'JPXDecode':
            img = Image.open(io.BytesIO(data))
            img.load()
            if img.mode == 'CMYK':
                img = self.cmyk_image(img, cs)
        else:
            if cs is None:
                cs = DeviceGray()
            ncomp = 1 if isinstance(cs, (IndexedCS,)) else cs.n
            img = self.decode_samples(data, w, h, ncomp, bpc, cs)
            img = self.convert_image(img, cs, decode, bpc)

        # transparency
        alpha = None
        if xo is not None:
            sm = R(xo.get('/SMask'))
            mk = R(xo.get('/Mask'))
            if isinstance(sm, StreamObject):
                sw, sh_ = int(num(sm['/Width'])), int(num(sm['/Height']))
                sf = R(sm.get('/Filter'))
                sf = [name(f) for f in (sf if isinstance(sf, list) else [sf] if sf else [])]
                if sf and sf[-1] == 'DCTDecode':
                    alpha = Image.open(io.BytesIO(sm._data)).convert('L')
                else:
                    alpha = self.decode_samples(sm.get_data(), sw, sh_, 1, int(num(sm.get('/BitsPerComponent'), 8)), None)
                sd = R(sm.get('/Decode'))
                if sd and num(sd[0]) > num(sd[1]):
                    alpha = ImageChops.invert(alpha)
            elif isinstance(mk, StreamObject):
                mw, mh = int(num(mk['/Width'])), int(num(mk['/Height']))
                st = self.decode_samples(mk.get_data(), mw, mh, 1, int(num(mk.get('/BitsPerComponent'), 1)), None)
                md = R(mk.get('/Decode'))
                alpha = st if (md and num(md[0]) > num(md[1])) else ImageChops.invert(st)
            elif isinstance(mk, list):
                rng = [int(num(v)) for v in mk]
                if not jpeg_pass and img.mode in ('L', 'RGB'):
                    raw = self.decode_samples(data, w, h, 1 if isinstance(cs, IndexedCS) else cs.n, bpc, cs, raw=True)
                    import numpy as np
                    arr = np.asarray(raw).reshape(h, w, -1) if raw.mode != 'L' else np.asarray(raw).reshape(h, w, 1)
                    inside = np.ones((h, w), bool)
                    for c in range(arr.shape[2]):
                        inside &= (arr[:, :, c] >= rng[2 * c]) & (arr[:, :, c] <= rng[2 * c + 1])
                    alpha = Image.fromarray(np.where(inside, 0, 255).astype('uint8'), 'L')
        if alpha is not None:
            if jpeg_pass:
                img = Image.open(io.BytesIO(data))
                jpeg_pass = False
            img = img.convert('RGB')
            if alpha.size != img.size:
                alpha = alpha.resize(img.size, Image.BILINEAR)
            img.putalpha(alpha)
        href = self.encode(img, data if jpeg_pass else None, None)
        if key is not None:
            self.image_ids[key] = (href, w, h)
        return href, w, h

    def decode_samples(self, data, w, h, ncomp, bpc, cs, raw=False):
        import numpy as np
        row_bits = w * ncomp * bpc
        row_bytes = (row_bits + 7) // 8
        need = row_bytes * h
        if len(data) < need:
            data = bytes(data) + b'\x00' * (need - len(data))
        buf = np.frombuffer(bytes(data[:need]), dtype=np.uint8).reshape(h, row_bytes)
        if bpc == 8:
            arr = buf[:, :w * ncomp]
        elif bpc == 16:
            arr = buf.reshape(h, -1)[:, :w * ncomp * 2][:, ::2]
        else:
            bits = np.unpackbits(buf, axis=1)[:, :row_bits]
            bits = bits.reshape(h, w * ncomp, bpc)
            weights = (1 << np.arange(bpc - 1, -1, -1)).astype(np.uint16)
            vals = (bits * weights).sum(axis=2)
            if raw or isinstance(cs, IndexedCS):
                arr = vals.astype(np.uint8)
            else:
                arr = (vals * 255 // ((1 << bpc) - 1)).astype(np.uint8)
        arr = np.ascontiguousarray(arr.reshape(h, w, ncomp))
        if ncomp == 1:
            return Image.fromarray(arr[:, :, 0], 'L')
        if ncomp == 3:
            return Image.fromarray(arr, 'RGB')
        if ncomp == 4:
            return Image.fromarray(arr, 'CMYK')
        return ('N', arr)

    def cmyk_image(self, img, cs):
        prof = cs.profile if isinstance(cs, DeviceCMYK) else self.cs.default_cmyk_profile
        xf = icc_transform(prof, 'CMYK') if prof else None
        return ImageCms.applyTransform(img, xf) if xf else img.convert('RGB')

    def convert_image(self, img, cs, decode, bpc=8):
        import numpy as np
        if isinstance(img, tuple):  # DeviceN with many channels
            arr = img[1]
            h, w, n = arr.shape
            flat = arr.reshape(-1, n)
            uniq, inv_idx = np.unique(flat, axis=0, return_inverse=True)
            cols = np.array([[int(round(c * 255)) for c in cs.rgb([v / 255 for v in u])] for u in uniq], dtype=np.uint8)
            return Image.fromarray(cols[inv_idx.reshape(-1)].reshape(h, w, 3), 'RGB')
        if decode and not isinstance(cs, IndexedCS):
            arr = np.asarray(img).astype(np.float32)
            if arr.ndim == 2:
                arr = arr[:, :, None]
            for c in range(arr.shape[2]):
                if 2 * c + 1 < len(decode):
                    d0, d1 = decode[2 * c], decode[2 * c + 1]
                    if (d0, d1) != (0.0, 1.0):
                        arr[:, :, c] = np.clip((d0 + arr[:, :, c] / 255 * (d1 - d0)) * 255, 0, 255)
            arr = arr.astype(np.uint8)
            img = Image.fromarray(arr[:, :, 0] if arr.shape[2] == 1 else arr, img.mode)
        if isinstance(cs, IndexedCS):
            pal = []
            for i in range(256):
                pal += [int(round(c * 255)) for c in cs.rgb([min(i, cs.hival)])]
            p = img.convert('L') if img.mode != 'L' else img
            p = p.copy()
            p = Image.frombytes('P', p.size, p.tobytes())
            p.putpalette(pal)
            return p.convert('RGB')
        if isinstance(cs, DeviceCMYK):
            return self.cmyk_image(img, cs)
        if isinstance(cs, (DeviceRGB, DeviceGray)) or cs is None:
            return img
        if isinstance(cs, ICCRGB):
            xf = icc_transform(cs.profile, 'RGB')
            return ImageCms.applyTransform(img, xf) if xf else img
        # Separation, Lab, other: per unique value lookup
        arr = np.asarray(img)
        if arr.ndim == 2:
            lut = np.array([[int(round(c * 255)) for c in cs.rgb([i / 255])] for i in range(256)], dtype=np.uint8)
            return Image.fromarray(lut[arr], 'RGB')
        h, w = arr.shape[:2]
        flat = arr.reshape(-1, arr.shape[2])
        uniq, inv_idx = np.unique(flat, axis=0, return_inverse=True)
        if isinstance(cs, LabCS):
            r = cs.rng
            conv = lambda u: cs.rgb([u[0] / 255 * 100, r[0] + u[1] / 255 * (r[1] - r[0]), r[2] + u[2] / 255 * (r[3] - r[2])])
        else:
            conv = lambda u: cs.rgb([v / 255 for v in u])
        cols = np.array([[int(round(c * 255)) for c in conv(u)] for u in uniq], dtype=np.uint8)
        return Image.fromarray(cols[inv_idx.reshape(-1)].reshape(h, w, 3), 'RGB')

    def encode(self, img, jpeg_bytes, _):
        if jpeg_bytes is not None:
            return 'data:image/jpeg;base64,' + base64.b64encode(jpeg_bytes).decode()
        buf = io.BytesIO()
        if img.mode not in ('RGB', 'RGBA', 'L', 'LA', 'P'):
            img = img.convert('RGB')
        img.save(buf, 'PNG', optimize=True)
        return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()

    # ---- text
    def font(self, nm, res):
        fobj = R(R(res.get('/Font', {})).get('/' + nm)) if res is not None else None
        if fobj is None:
            warn('Missing font %s; its text was skipped.' % nm)
            return None
        return self.font_obj(fobj)

    def font_obj(self, fobj):
        key = obj_key(fobj)
        if key not in self.fonts:
            try:
                self.fonts[key] = Font(fobj)
            except Exception as e:
                warn('A font could not be read (%s); its text was skipped.' % e)
                self.fonts[key] = None
        return self.fonts[key]

    def show_text(self, items, gs, tm, base, block, res, depth):
        f = gs.font
        if f is None:
            return tm
        mode = gs.tr
        th = gs.th
        for it in items:
            if isinstance(it, float):
                tm = mul((1, 0, 0, 1, -it / 1000 * gs.fs * th, 0), tm)
                continue
            if not isinstance(it, bytes):
                continue
            for code, nb in f.codes(it):
                w0 = f.width(code, nb) / 1000.0
                if mode != 3 and mode != 7:
                    trm = mul((gs.fs * th, 0, 0, gs.fs, 0, gs.ts), tm)
                    if f.type3:
                        self.type3_glyph(f, code, gs, trm, base, block, res, depth)
                    else:
                        block.add(self, f, code, nb, gs, trm, tm, base)
                tx = (w0 * gs.fs + gs.tc + (gs.tw if (nb == 1 and code == 32) else 0)) * th
                tm = mul((1, 0, 0, 1, tx, 0), tm)
        if mode >= 4:
            warn('Text used as a clipping path was drawn as normal text.')
        return tm

    def type3_glyph(self, f, code, gs, trm, base, block, res, depth):
        procs = R(f.obj.get('/CharProcs'))
        gn = f.gname(code)
        if procs is None or gn is None or '/' + gn not in procs:
            return
        inner = gs.copy()
        inner.ctm = mul(mul(f.font_matrix, trm), gs.ctm)
        fres = R(f.obj.get('/Resources')) or res
        block.flush_into = True
        self.run(R(procs['/' + gn]).get_data(), fres, base, block.type3_parent(), inner, depth + 1)

    def flush_text(self, block, parent):
        block.emit(self, parent)


class TextBlock:
    """Collects the glyphs of one BT..ET block."""
    def __init__(self):
        self.runs = []   # list of dicts: key -> {segs | chars}
        self.extra = None
        self.flush_into = False

    def type3_parent(self):
        if self.extra is None:
            self.extra = Node('g')
        return self.extra

    def add(self, conv, f, code, nb, gs, trm, tm, base):
        fill = stroke = None
        mode = gs.tr % 4
        if mode in (0, 2):
            fill = conv.paint_attrs(gs, base, 'fill')
        if mode in (1, 2):
            stroke = conv.paint_attrs(gs, base, 'stroke')
        uni = f.unicode(code, nb)
        outline = conv.text_mode == 'outlines' and f.outline_src is not None
        if conv.text_mode == 'outlines' and f.outline_src is None and not f.type3:
            warn('Font "%s" is not embedded in the PDF, so its text was kept as editable text. Install the font to see it as designed.' % re.sub(r'^[A-Z]{6}\+', '', f.basename))
        m = mul(trm, mul(gs.ctm, base))
        key = (outline, id(f), fill, stroke, round(gs.fs, 4), gs.lw, gs.smask, gs.blend)
        if not self.runs or self.runs[-1]['key'] != key:
            self.runs.append({'key': key, 'font': f, 'fill': fill, 'stroke': stroke, 'outline': outline,
                              'segs': [], 'glyphs': [], 'text': '', 'gs': gs.copy(), 'lw': gs.lw * scale_of(mul(gs.ctm, base)),
                              'fs': gs.fs, 'th': gs.th, 'ctm': mul(gs.ctm, base)})
        run = self.runs[-1]
        run['text'] += uni
        if outline:
            g = f.glyph(code, nb)
            if g:
                run['segs'].extend((op, [apply(m, x, y) for x, y in pts]) for op, pts in g)
        else:
            run['glyphs'].append((uni or '?', tm, trm))

    def emit(self, conv, parent):
        if self.extra is not None and self.extra.children:
            parent.add(self.extra)
        nodes = []
        for run in self.runs:
            if run['outline']:
                if not run['segs']:
                    continue
                attrs = [('d', path_d(run['segs']))]
                self._paint(attrs, run)
                nodes.append(Node('path', attrs))
            else:
                nodes.extend(self._text_nodes(conv, run))
        label = clean_xml_text(''.join(r['text'] for r in self.runs)).strip()
        if not nodes:
            return
        conv.stats['texts'] += 1
        if len(nodes) == 1:
            if nodes[0].tag == 'path' and label:
                nodes[0].attrs.append(('inkscape:label', esc(label[:60])))
            parent.add(nodes[0])
        else:
            g = parent.add(Node('g', [('inkscape:label', esc(label[:60]))] if label else []))
            for n in nodes:
                g.add(n)

    def _paint(self, attrs, run):
        if run['fill']:
            attrs.append(('fill', run['fill'][0]))
            if run['fill'][1] < 1:
                attrs.append(('fill-opacity', fmt(run['fill'][1])))
        else:
            attrs.append(('fill', 'none'))
        if run['stroke']:
            attrs.append(('stroke', run['stroke'][0]))
            attrs.append(('stroke-width', fmt(max(run['lw'], 0.001))))
            if run['stroke'][1] < 1:
                attrs.append(('stroke-opacity', fmt(run['stroke'][1])))
        gs = run['gs']
        if gs.blend:
            attrs.append(('style', 'mix-blend-mode:%s' % gs.blend))
        if gs.smask:
            attrs.append(('mask', 'url(#%s)' % gs.smask))

    def _text_nodes(self, conv, run):
        """Editable text: one <text> per run of glyphs sharing a baseline orientation."""
        out = []
        f = run['font']
        family = f.family.replace('"', "'")
        style = ['font-family:%s' % ("'%s'" % family if ' ' in family else family)]
        if f.weight != 400:
            style.append('font-weight:%d' % f.weight)
        if f.italic:
            style.append('font-style:italic')
        if f.condensed:
            style.append('font-stretch:condensed')
        current = None
        for uni, tm, trm in run['glyphs']:
            lin = tuple(round(v, 5) for v in trm[:4])
            if current is None or current['lin'] != lin:
                base = mul(mul((1, 0, 0, -1, 0, 0), trm), run['ctm'])
                current = {'lin': lin, 'base': base, 'inv': inv(trm), 'xs': [], 'ys': [], 'chars': ''}
                out.append(current)
            u, v = apply(current['inv'], trm[4], trm[5])
            for k, ch in enumerate(uni):
                current['xs'].append(u + k * 0.3)
                current['ys'].append(-v)
                current['chars'] += ch
        nodes = []
        for t in out:
            chars = esc(clean_xml_text(t['chars']))
            if not chars:
                continue
            same_y = all(abs(y - t['ys'][0]) < 1e-6 for y in t['ys'])
            ys = fmt(t['ys'][0]) if same_y else ' '.join(fmt(y) for y in t['ys'])
            b0 = t['base']
            sc = math.sqrt(abs(b0[0] * b0[3] - b0[1] * b0[2])) or 1.0
            b1 = (b0[0] / sc, b0[1] / sc, b0[2] / sc, b0[3] / sc, b0[4], b0[5])
            t['xs'] = [x * sc for x in t['xs']]
            ys = fmt(t['ys'][0] * sc) if same_y else ' '.join(fmt(y * sc) for y in t['ys'])
            attrs = [('xml:space', 'preserve')]
            if any(abs(v - w) > 1e-6 for v, w in zip(b1, (1, 0, 0, 1, 0, 0))):
                attrs.append(('transform', fmt_m(b1)))
            attrs.append(('style', ';'.join(style + ['font-size:%spx' % fmt(sc)])))
            self._paint(attrs, run)
            # merge style attribute
            st = [v for k, v in attrs if k == 'style']
            attrs = [(k, v) for k, v in attrs if k != 'style'] + [('style', ';'.join(st))]
            node = Node('text', attrs)
            node.add(Node('tspan', [('x', ' '.join(fmt(x) if abs(x) >= 5e-4 else '0' for x in t['xs'])), ('y', ys)], text=chars))
            nodes.append(node)
        return nodes


class NameObj(str):
    pass


try:
    from pypdf.generic import NameObject as NameObj  # noqa: F811
except Exception:
    pass


# ---------------------------------------------------------------- document
def parse_pages(spec, count):
    if not spec or spec.strip().lower() in ('all', '0', ''):
        return list(range(count))
    out = []
    for part in spec.split(','):
        part = part.strip()
        if '-' in part:
            a, b = part.split('-', 1)
            out.extend(range(int(a) - 1, min(int(b), count)))
        elif part:
            out.append(int(part) - 1)
    return [p for p in out if 0 <= p < count]


def convert(path, text_mode='outlines', pages='all'):
    reader = PdfReader(path, strict=False)
    if reader.is_encrypted:
        reader.decrypt('')
    conv = Converter(reader, text_mode)
    idx = parse_pages(pages, len(reader.pages))
    if not idx:
        raise SystemExit('No such page in this PDF (it has %d).' % len(reader.pages))
    root = Node('root')
    page_boxes = []
    x = 0.0
    gap = 40.0
    for n, i in enumerate(idx):
        holder = root
        if len(idx) > 1:
            holder = root.add(Node('g', [('inkscape:groupmode', 'layer'), ('inkscape:label', 'Page %d' % (i + 1))]))
        w, h = conv.page(reader.pages[i], x, holder)
        page_boxes.append((x, 0.0, w, h))
        x += w + gap
    prune(root)
    W, H = page_boxes[0][2], page_boxes[0][3]
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
           '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'
           ' xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"'
           ' xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"'
           ' version="1.1" width="%s" height="%s" viewBox="0 0 %s %s">' % (fmt(W), fmt(H), fmt(W), fmt(H))]
    nv = Node('sodipodi:namedview', [('id', 'namedview1'), ('inkscape:document-units', 'px')])
    if len(page_boxes) > 1:
        for k, (px_, py_, pw, ph) in enumerate(page_boxes):
            nv.add(Node('inkscape:page', [('id', 'page%d' % (k + 1)), ('x', fmt(px_)), ('y', fmt(py_)),
                                          ('width', fmt(pw)), ('height', fmt(ph))]))
    nv.write(out, 1)
    if conv.defs.children:
        conv.defs.write(out, 1)
    for c in root.children:
        c.write(out, 1)
    out.append('</svg>')
    return '\n'.join(out) + '\n', conv.stats, len(reader.pages)


# Set while the standard import runs. If this script is started with it set,
# Inkscape's own importer was bypassed and the run must stop, not convert.
STANDARD_GUARD = 'PDF_IMPORT_CHOOSE_TYPE_STANDARD'


def inkscape_exe():
    """The Inkscape program that is running this extension."""
    cmd = os.environ.get('INKSCAPE_COMMAND')
    if cmd:
        return cmd
    # Inkscape runs extensions with its own Python, in the same bin folder.
    for exe in ('inkscape.exe', 'inkscape.com', 'inkscape'):
        cand = os.path.join(os.path.dirname(sys.executable), exe)
        if os.path.isfile(cand):
            return cand
    found = shutil.which('inkscape')
    if found:
        return found
    raise SystemExit('Could not find Inkscape to run its standard PDF import.')


def convert_standard(path, pages='all'):
    """Standard PDF: Inkscape's built-in PDF import, unchanged.

    Runs a second Inkscape with an empty, temporary profile folder, so it loads
    no user extensions (this one included) and only its own PDF importer exists.
    """
    reader = PdfReader(path, strict=False)
    if reader.is_encrypted:
        reader.decrypt('')
    idx = parse_pages(pages, len(reader.pages))
    if not idx:
        raise SystemExit('No such page in this PDF (it has %d).' % len(reader.pages))
    exe = inkscape_exe()
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    with tempfile.TemporaryDirectory(prefix='pdf-import-standard-') as profile:
        env = dict(os.environ, INKSCAPE_PROFILE_DIR=profile, **{STANDARD_GUARD: '1'})

        def run(page_arg):
            return subprocess.run([exe, path, page_arg, '--export-type=svg', '--export-filename=-'],
                                  capture_output=True, env=env, creationflags=flags)

        res = run('--pages=' + ','.join(str(i + 1) for i in idx))
        if res.returncode != 0 and b'Unknown option --pages' in res.stderr:
            # Inkscape 1.2 and older have no --pages; they import one page with --pdf-page.
            # Trying --pages first costs nothing on 1.3+, and about 0.1 s on 1.2.
            if len(idx) > 1:
                warn('This version of Inkscape imports one page at a time with Standard PDF. '
                     'Imported page %d. Inkscape 1.3 or later can import several pages.' % (idx[0] + 1))
            res = run('--pdf-page=%d' % (idx[0] + 1))
    if res.returncode != 0 or not res.stdout.strip():
        err = res.stderr.decode('utf-8', 'replace').strip()
        raise SystemExit('The standard PDF import failed.' + ('\n' + err if err else ''))
    return res.stdout


def write_out(data, output):
    if output:
        with open(output, 'wb') as f:
            f.write(data)
    else:
        sys.stdout.buffer.write(data)
        sys.stdout.flush()


def main():
    if os.environ.get(STANDARD_GUARD):
        raise SystemExit("The standard import was sent back to this extension instead of Inkscape's "
                         "own PDF importer, so it was stopped. Nothing was converted.")
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('input')
    ap.add_argument('--type', default='vector', choices=['vector', 'standard'])
    ap.add_argument('--text', default='outlines', choices=['outlines', 'editable'])
    ap.add_argument('--pages', default='all')
    ap.add_argument('--output', '-o')
    ap.add_argument('--stats', action='store_true')
    args, _unknown = ap.parse_known_args()
    if args.type == 'standard':
        write_out(convert_standard(args.input, args.pages), args.output)
    else:
        svg, stats, npages = convert(args.input, args.text, args.pages)
        write_out(svg.encode('utf-8'), args.output)
        if args.stats:
            sys.stderr.write('%s pages=%d\n' % (stats, npages))
    if WARNINGS:
        sys.stderr.write('Imported with notes:\n')
        for msg, n in WARNINGS.items():
            sys.stderr.write('- %s%s\n' % (msg, ' (%d times)' % n if n > 1 else ''))


if __name__ == '__main__':
    main()
