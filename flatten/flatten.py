#!/usr/bin/env python3
"""Flatten clips and overlaps.

Each selected object (for example one panel) is replaced by flat filled paths: every line
becomes a filled shape, every clip (also nested PowerClips) is applied as a real cut, and
parts hidden by objects on top are cut away, so every spot belongs to exactly one path.
Each of the panel's own objects becomes its own path (labeled with the object's name);
objects in an inner group (a pattern such as dots) are merged per color; with the
join_outlines option, an outline drawn on a surface of its own color joins that surface.
Curves stay curves.

Kept as they are: panel outlines (lines that follow the selection's outermost clip),
and objects that can't be converted exactly (text, images, ...), which are put back on
top and listed in a message.

The geometry runs in a standard Python 3.10+ with skia-pathops (bundled in ./vendor for
Windows and Linux x86-64), because Inkscape's boolean operations leave small gaps. On
Windows Inkscape's own Python can't load it, so python.org Python is used; on Linux,
Inkscape's Python if it is 3.10+, else the system's python3.
"""
import glob
import json
import os
import shutil
import subprocess
import sys

import inkex

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gradients  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, 'flatten_worker.py')
SHAPES = (inkex.PathElement, inkex.Rectangle, inkex.Circle, inkex.Ellipse, inkex.Polygon,
          inkex.Polyline, inkex.Line)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


# ------------------------------------------------------------------ finding Python
def clean_env(exe):
    """Environment for another Python: without the variables an AppImage sets for its own
    bundled Python and libraries, which would break the system's python3."""
    env = dict(os.environ)
    if os.path.realpath(exe) != os.path.realpath(sys.executable):
        for k in ('PYTHONHOME', 'PYTHONPATH', 'LD_LIBRARY_PATH', 'PYTHONNOUSERSITE'):
            env.pop(k, None)
    return env


def python_ok(exe):
    try:
        out = subprocess.run([exe, '-c', 'import sys; print(sys.version_info[0], sys.version_info[1])'],
                             capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW,
                             env=clean_env(exe))
        major, minor = map(int, out.stdout.split())
        return (major, minor) >= (3, 10)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def find_python():
    """A standard Python 3.10+ that can load the bundled skia-pathops.

    Windows: not Inkscape's own (MinGW) Python, which can't load it. Linux: Inkscape's own
    Python works when it is 3.10+ (distro packages); the AppImage's Python 3.8 is too old,
    so the system's python3 is used then."""
    cands = [os.environ.get('FLATTEN_PYTHON')]
    if sys.platform == 'win32':
        local = os.environ.get('LOCALAPPDATA', '')
        cands += sorted(glob.glob(os.path.join(local, 'Programs', 'Python', 'Python3*', 'python.exe')), reverse=True)
        cands += sorted(glob.glob(r'C:\Python3*\python.exe'), reverse=True)
        cands += sorted(glob.glob(r'C:\Program Files\Python3*\python.exe'), reverse=True)
        cands += [shutil.which('python'), shutil.which('python3')]
        own = os.path.normcase(os.path.dirname(sys.executable))
    else:
        cands += [sys.executable, '/usr/bin/python3', '/usr/local/bin/python3', shutil.which('python3')]
        own = None
    for exe in cands:
        if exe and os.path.isfile(exe) and os.path.normcase(os.path.dirname(exe)) != own and python_ok(exe):
            return exe
    if sys.platform == 'win32' and shutil.which('py'):
        try:
            out = subprocess.run(['py', '-3', '-c', 'import sys; print(sys.executable)'], capture_output=True,
                                 text=True, timeout=30, creationflags=NO_WINDOW).stdout.strip()
            if out and python_ok(out):
                return out
        except (OSError, subprocess.SubprocessError):
            pass
    return None


# ------------------------------------------------------------------ geometry helpers
def abs_d(path, transform):
    """Path in absolute M/L/C/Z, with the transform applied."""
    p = inkex.Path(path).to_absolute().transform(transform)
    return str(inkex.Path(inkex.CubicSuperPath(p)).to_absolute())


def length(value, default=1.0):
    try:
        return float(str(value).replace('px', '').strip())
    except (TypeError, ValueError):
        return default


def paint(value):
    """Solid color string, or None for no paint; (color, problem) for anything else."""
    if value is None or str(value).strip() in ('none', ''):
        return None, None
    s = str(value).strip()
    if s.startswith('url('):
        return None, 'gradient or pattern fill'
    return s, None


class Flatten(inkex.EffectExtension):
    def add_arguments(self, pars):
        pars.add_argument('--join_outlines', type=inkex.Boolean, default=True,
                          help='Outlines in the color of the surface under them join that surface')

    def effect(self):
        selected = [e for e in self.svg.selection.values() if isinstance(e, inkex.ShapeElement)]
        # An object inside another selected object is handled with that one.
        chosen = set(selected)
        selected = [e for e in selected if not any(a in chosen for a in e.ancestors())]
        if not selected:
            raise inkex.AbortExtension('Select one or more objects (for example panels) first.')
        self.clips = {}
        self.notes = {}
        self.grads = gradients.GradientFills(self.svg, self.note)
        self.fades_joined = 0
        units, plans = [], []
        outer_boxes = [b for b in (self.outer_clip_box(e) for e in selected) if b is not None]
        for el in selected:
            if self.is_outline(el, outer_boxes):
                continue  # panel outline line: stays as it is
            items, keep = self.collect(el)
            units.append({'items': items})
            plans.append((el, keep))
        if not units:
            raise inkex.AbortExtension('Nothing to flatten: the selection only has panel outlines.')

        exe = find_python()
        if not exe:
            if sys.platform == 'win32':
                raise inkex.AbortExtension(
                    'Flatten needs Python 3.10 or later installed (from python.org), next to Inkscape. '
                    'Install it, or set the environment variable FLATTEN_PYTHON to its python.exe.')
            raise inkex.AbortExtension(
                'Flatten needs Python 3.10 or later as python3 (for example: sudo apt install python3). '
                'Or set the environment variable FLATTEN_PYTHON to a Python 3.10+ program.')
        payload = json.dumps({'clips': self.clips, 'units': units, 'join': bool(self.options.join_outlines)})
        if os.environ.get('FLATTEN_DUMP'):
            with open(os.environ['FLATTEN_DUMP'], 'w') as f:
                f.write(payload)
        res = subprocess.run([exe, WORKER], input=payload.encode(),
                             capture_output=True, creationflags=NO_WINDOW, env=clean_env(exe))
        if res.returncode:
            raise inkex.AbortExtension('The geometry step failed:\n' + res.stderr.decode('utf-8', 'replace')[-1500:])
        result = json.loads(res.stdout)
        if os.environ.get('FLATTEN_DEBUG'):
            sys.stderr.write(res.stderr.decode('utf-8', 'replace'))

        for (el, keep), unit in zip(plans, result['units']):
            if el in keep:
                continue  # the selected object itself can't be flattened (text, image...)
            label = el.label or el.get_id()
            self.replace(el, unit['pieces'], keep)
            if unit['errors']:
                self.note('%d object(s) had shapes that could not be processed and were left out' % unit['errors'])
            if unit.get('joined'):
                self.note('%s: %d outline(s) in the color of the surface under them were joined with that '
                          'surface' % (label, unit['joined']))
            if unit.get('fades_joined'):
                self.fades_joined += unit['fades_joined']
            if unit.get('unsure'):
                self.note('%s: %d cut(s) could not be confirmed by the geometry check; '
                          'please check this part' % (label, unit['unsure']))
        if self.grads.written:
            # Declare xlink once on the root, so the links read "xlink:href" (what a web
            # page with the SVG inline understands), not "ns1:href" on every gradient.
            from lxml import etree
            etree.cleanup_namespaces(self.document.getroot(),
                                     top_nsmap={'xlink': 'http://www.w3.org/1999/xlink'})
        if self.fades_joined:
            self.note('%d part(s) with their own fade in another direction were joined with the main '
                      'fade of their shape, so each shape is one path; their own small fade (for '
                      'example a glow at a tip) is not kept' % self.fades_joined)
        if self.notes:
            self.msg('Flattened with notes:\n' + '\n'.join(
                '- %s%s' % (k, ' (%d times)' % n if n > 1 else '') for k, n in self.notes.items()))

    def note(self, text):
        self.notes[text] = self.notes.get(text, 0) + 1

    # -------------------------------------------------------------- reading the selection
    def clip_of(self, owner):
        ref = owner.get('clip-path')
        if not ref or ref.strip() == 'none':
            return None
        cid = ref.strip()[5:-1].strip('\'"')
        cp = self.svg.getElementById(cid)
        return cp if isinstance(cp, inkex.ClipPath) else None

    def clip_key(self, owner, cp):
        """Clip outline in page coordinates (clipPathUnits=userSpaceOnUse)."""
        if cp.get('clipPathUnits') == 'objectBoundingBox':
            self.note('a clip with objectBoundingBox units was ignored')
            return None
        key = '%s@%s' % (cp.get_id(), owner.get_id())
        if key not in self.clips:
            base = owner.composed_transform() @ cp.transform
            ds, rule = [], 'nonzero'
            for c in cp.iterdescendants():
                if not isinstance(c, SHAPES):
                    continue
                t = inkex.Transform()
                node = c
                while node is not cp:
                    t = node.transform @ t
                    node = node.getparent()
                ds.append(abs_d(c.path, base @ t))
                rule = c.specified_style().get('clip-rule', 'nonzero')
            self.clips[key] = {'d': ' '.join(ds), 'rule': rule}
        return key

    def outer_clip_box(self, el):
        cp = self.clip_of(el)
        if cp is None:
            return None
        key = self.clip_key(el, cp)
        return inkex.Path(self.clips[key]['d']).bounding_box() if key else None

    def is_outline(self, el, boxes):
        """A line with no fill that follows a selected object's outermost clip."""
        if not isinstance(el, SHAPES):
            return False
        st = el.specified_style()
        if paint(st.get('fill'))[0] or not paint(st.get('stroke'))[0]:
            return False
        bb = inkex.Path(abs_d(el.path, el.composed_transform())).bounding_box()
        for b in boxes:
            tol = 0.01 * max(b.width, b.height)
            if all(abs(x - y) <= tol for x, y in zip((bb.left, bb.top, bb.right, bb.bottom),
                                                    (b.left, b.top, b.right, b.bottom))):
                return True
        return False

    @staticmethod
    def panel_top(unit):
        """The group whose own children are the panel's objects: the selected object, or
        the inner group when the selection only wraps one group."""
        top = unit
        while isinstance(top, inkex.Group):
            kids = [c for c in top if isinstance(c, inkex.ShapeElement)]
            if len(kids) == 1 and isinstance(kids[0], inkex.Group):
                top = kids[0]
            else:
                break
        return top

    @staticmethod
    def name(el):
        return el.label or el.get_id()

    def branch(self, el, top):
        """The inner group (a direct child of the panel's top) that holds el, or None
        when el is one of the panel's own objects."""
        node = el
        while node is not None and node.getparent() is not top:
            node = node.getparent()
        return node if node is not None and node is not el else None

    def collect(self, unit):
        items, keep = [], []
        stop = unit.getparent()
        top = self.panel_top(unit)
        outer = self.outer_clip_box(unit)
        for el in [unit] + list(unit.iterdescendants()):
            if not isinstance(el, inkex.ShapeElement) or isinstance(el, (inkex.Group, inkex.ClipPath, inkex.Defs)):
                continue
            if any(isinstance(a, (inkex.ClipPath, inkex.Defs, inkex.Marker, inkex.Pattern)) for a in el.ancestors()):
                continue
            if any(k in el.ancestors() for k in keep):
                continue  # inside an object that is kept whole (e.g. a text line inside a text)
            st = el.specified_style()
            if st.get('display') == 'none' or st.get('visibility') == 'hidden':
                continue
            if not isinstance(el, SHAPES):
                keep.append(el)
                self.note('%s objects were left as they are' % el.TAG)
                continue
            if outer is not None and self.is_outline(el, [outer]):
                keep.append(el)
                continue
            chain = []
            node = el
            while node is not None and node is not stop:
                cp = self.clip_of(node)
                if cp is not None:
                    key = self.clip_key(node, cp)
                    if key:
                        chain.append(key)
                if node.get('mask'):
                    self.note('masks are not supported; masked objects were flattened without the mask')
                node = node.getparent()
            fill, p1 = paint(st.get('fill'))
            stroke, p2 = paint(st.get('stroke'))
            if p1:  # a gradient fill: split into solid parts and fades (see gradients.py)
                done = self.gradient_items(el, st, stroke, chain, top, items)
                if done:
                    p1 = None
                    if p2:
                        self.note('%s was left out (only solid colors are supported)' % p2)
                    continue
            for problem in (p1, p2):
                if problem:
                    self.note('%s was left out (only solid colors are supported)' % problem)
            opac = length(st.get('opacity', 1)) * length(st.get('fill-opacity', 1))
            if opac < 0.999 or length(st.get('stroke-opacity', 1)) < 0.999:
                self.note('transparency was ignored (treated as fully opaque)')
            if st.get('stroke-dasharray') not in (None, 'none') and stroke:
                self.note('dashed lines were converted as solid lines')
            if not fill and not stroke:
                continue
            T = el.composed_transform()
            grp = self.branch(el, top) if el is not top else None
            items.append({
                'id': el.get_id(),
                'label': self.name(el),
                'group': grp.get_id() if grp is not None else None,
                'group_label': self.name(grp) if grp is not None else None,
                'd': abs_d(el.path, T),
                'local_d': abs_d(el.path, inkex.Transform()) if stroke else '',
                'matrix': [T.a, T.b, T.c, T.d, T.e, T.f],
                'rule': st.get('fill-rule', 'nonzero'),
                'fill': fill,
                'stroke': stroke,
                'width': length(st.get('stroke-width', 1)),
                'cap': st.get('stroke-linecap', 'butt'),
                'join': st.get('stroke-linejoin', 'miter'),
                'miter': length(st.get('stroke-miterlimit', 4), 4.0),
                'clips': chain,
            })
        return items, keep

    def gradient_items(self, el, st, stroke, chain, top, items):
        """Items for an object with a gradient fill: one per solid part or fade, each cut
        by its band (an extra clip); the outline, if any, as its own item on top.
        False when the gradient can't be handled (it is then left out, as before)."""
        clip_box = None
        for key in chain:
            clip_box = gradients.box_and(clip_box, gradients.bbox_of(self.clips[key]['d']))
            if clip_box is None:
                return True  # the clips don't overlap: nothing of it is visible
        T = el.composed_transform()
        res = self.grads.split(el, T, abs_d(el.path, T), clip_box, [self.clips[k]['d'] for k in chain])
        if res is None:
            return False
        d, parts = res
        opac = length(st.get('opacity', 1)) * length(st.get('fill-opacity', 1))
        if opac < 0.999 or (stroke and length(st.get('stroke-opacity', 1)) < 0.999):
            self.note('transparency was ignored (treated as fully opaque)')
        if st.get('stroke-dasharray') not in (None, 'none') and stroke:
            self.note('dashed lines were converted as solid lines')
        grp = self.branch(el, top) if el is not top else None
        name = self.name(el)

        def item(fill, stroke_, clips, label):
            return {
                'id': el.get_id(),
                'label': label,
                'group': grp.get_id() if grp is not None else None,
                'group_label': self.name(grp) if grp is not None else None,
                'd': d,
                'local_d': abs_d(el.path, inkex.Transform()) if stroke_ else '',
                'matrix': [T.a, T.b, T.c, T.d, T.e, T.f],
                'rule': st.get('fill-rule', 'nonzero'),
                'fill': fill,
                'stroke': stroke_,
                'width': length(st.get('stroke-width', 1)),
                'cap': st.get('stroke-linecap', 'butt'),
                'join': st.get('stroke-linejoin', 'miter'),
                'miter': length(st.get('stroke-miterlimit', 4), 4.0),
                'clips': clips,
            }

        if len(parts) == 1 and parts[0][1] is None:
            color, _, suffix = parts[0]
            keep_suffix = suffix if self.grads.is_key(color) else ''
            it = item(color, stroke, list(chain), name + keep_suffix)
            sh = self.grads.shade(color)
            if sh:
                it['shade'] = sh  # lets the worker join the parts of one blade (join_shades)
                # Parts of one blade: each has its own clip, inside the blade's clips.
                it['shade_group'] = '|'.join(chain[1:]) if len(chain) > 1 else None
            items.append(it)
            return True
        for color, band, suffix in parts:
            items.append(item(color, None, list(chain) + ([band] if band else []), name + suffix))
        if stroke:
            items.append(item(None, stroke, list(chain), name + ' outline'))
        return True

    # -------------------------------------------------------------- writing the result
    def replace(self, unit, pieces, keep):
        if unit in keep:
            return  # the selected object itself can't be flattened (text, image...): leave it
        parent = unit.getparent()
        to_parent = -parent.composed_transform()
        idx = parent.index(unit)
        new = []
        for p in pieces:
            e = inkex.PathElement()
            e.path = inkex.Path(p['d']).transform(to_parent)
            color, label = p['color'], p.get('label')
            if self.grads.is_key(color):  # a fade: a real gradient, placed for this parent
                if label:
                    label = label.replace(color, 'fade')
                color = self.grads.make_paint(color, to_parent)
            e.style = inkex.Style({'fill': color, 'stroke': 'none'})
            if label:
                e.label = label
            new.append(e)
        for el in keep:  # unchanged, on top, at the same place on the page
            t = el.composed_transform()
            el.getparent().remove(el)
            el.transform = to_parent @ t
            new.append(el)
        parent.remove(unit)
        for i, e in enumerate(new):
            parent.insert(idx + i, e)
            if not e.get('id'):
                e.set_random_id('flat')


if __name__ == '__main__':
    Flatten().run()
