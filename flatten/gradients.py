"""Gradient fills for "Flatten clips and overlaps" (runs in Inkscape's Python, 3.8+).

Used only for objects whose fill is a gradient (url(...)). Solid colors never come here.

Each object with a gradient stays one path with a gradient (it is not split by color).

A linear gradient that, over the part of it that is visible, goes from one color to
another (for example gold to black, with in-between stops along the way) becomes a
two-color fade that links to a shared gradient holding only those two colors. Every fade
between the same two colors links to the same shared gradient, so changing gold to red
there once changes every object with that fade. The object's own gradient keeps only
where the fade starts and ends; beyond those ends the end colors continue (SVG "pad").
The fade is placed so its middle and its width match the original (where the original
reaches 10%, 50% and 90% of the way between the two colors). The in-between stops are
dropped, so the fade's curve can differ a little from the original.

Kept as they are, on one path with their own gradient: radial gradients, gradients that
repeat or reflect, and gradients with more than two colors in the visible part. A gradient
that shows only one color becomes a solid fill.

In the geometry worker, a gradient travels as a color key ("url(#__flatgrad3)"), so the
worker needs no changes; the key is replaced by a real gradient when the result is
written (make_paint).
"""
import re

import inkex

GRADIENTS = (inkex.LinearGradient, inkex.RadialGradient)
NUM = re.compile(r'^\s*(-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(%?)')
XLINK = '{http://www.w3.org/1999/xlink}href'
INKSCAPE_NS = '{http://www.inkscape.org/namespaces/inkscape}'

SNAP = 12.0      # colors this close (0..255 per channel) count as the same in a solid
PALETTE = 16.0   # fade end colors this close across the design become one color
OFF_LINE = 16.0  # stops this far from the straight line between the two colors: not a fade
SAME = 6.0       # two colors this close are one color (the gradient is solid)


class Unsupported(Exception):
    pass


# ------------------------------------------------------------------ reading a gradient
def href_of(g):
    return g.get(XLINK) or g.get('href')


def chain(svg, g):
    """The gradient and the ones it links to (href), nearest first."""
    out, seen = [], set()
    while g is not None and isinstance(g, GRADIENTS) and id(g) not in seen:
        seen.add(id(g))
        out.append(g)
        ref = href_of(g)
        g = svg.getElementById(ref.strip()[1:]) if ref and ref.strip().startswith('#') else None
    return out


def attr(gs, name, default=None):
    for g in gs:
        if g.get(name) is not None:
            return g.get(name)
    return default


def number(value, units_bbox):
    """A gradient coordinate: plain number, or a percentage (fraction in bbox units)."""
    m = NUM.match(str(value))
    if not m:
        raise Unsupported('bad gradient coordinate')
    v = float(m.group(1))
    if m.group(2):
        if not units_bbox:
            raise Unsupported('percentage in user-space gradient')
        v /= 100.0
    return v


def rgb(hexcolor):
    return tuple(int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))


def stop_list(gs):
    """[(offset, '#rrggbb', opacity)] from the first gradient in the chain that has stops,
    offsets clamped to 0..1 and never going back (as SVG renders them)."""
    for g in gs:
        stops = [s for s in g if isinstance(s, inkex.Stop)]
        if stops:
            break
    else:
        return []
    out, last = [], 0.0
    for s in stops:
        st = s.specified_style() if hasattr(s, 'specified_style') else s.style
        m = NUM.match(str(s.get('offset', '0')))
        off = float(m.group(1)) / (100.0 if m.group(2) else 1.0) if m else 0.0
        off = min(max(off, 0.0, last), 1.0)
        last = off
        col = st.get('stop-color') or s.get('stop-color') or '#000000'
        try:
            col = str(inkex.Color(col).to_rgb())
        except Exception:
            raise Unsupported('stop color %s' % col)
        op = st.get('stop-opacity') or s.get('stop-opacity') or '1'
        try:
            op = float(op)
        except ValueError:
            op = 1.0
        out.append((off, col.lower(), op))
    return out


# ------------------------------------------------------------------ geometry helpers
def bbox_of(d):
    bb = inkex.Path(d).bounding_box()
    return None if bb is None else (bb.left, bb.top, bb.right, bb.bottom)


def box_and(a, b):
    if a is None:
        return b
    if b is None:
        return a
    r = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return r if r[0] < r[2] and r[1] < r[3] else None


def polygon(path):
    """Corner points when the path is one straight-edged outline, else None."""
    pts, moves = [], 0
    for seg in path.to_absolute():
        c = seg.letter.upper()
        if c == 'M':
            moves += 1
        elif c not in 'LHVZ':
            return None
    if moves != 1:
        return None
    for p in path.end_points:
        if not pts or abs(p.x - pts[-1][0]) > 1e-9 or abs(p.y - pts[-1][1]) > 1e-9:
            pts.append((p.x, p.y))
    if len(pts) > 1 and abs(pts[0][0] - pts[-1][0]) < 1e-9 and abs(pts[0][1] - pts[-1][1]) < 1e-9:
        pts.pop()
    return pts if len(pts) >= 3 else None


def convex_contains(pts, q):
    sign = 0
    n = len(pts)
    for i in range(n):
        (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % n]
        cr = (x1 - x0) * (q[1] - y0) - (y1 - y0) * (q[0] - x0)
        if abs(cr) < 1e-12:
            continue
        s = 1 if cr > 0 else -1
        if sign and s != sign:
            return False
        sign = s
    return True


def is_convex(pts):
    sign, n = 0, len(pts)
    for i in range(n):
        (x0, y0), (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n], pts[(i + 2) % n]
        cr = (x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1)
        if abs(cr) < 1e-12:
            continue
        s = 1 if cr > 0 else -1
        if sign and s != sign:
            return False
        sign = s
    return sign != 0


def rect_d(b):
    x0, y0, x1, y1 = b
    return 'M %r %r L %r %r L %r %r L %r %r Z' % (x0, y0, x1, y0, x1, y1, x0, y1)


def shrink(page_path, box):
    """A PDF shading is drawn as a huge rectangle (about +-50000) cut by clips. When the
    shape is a convex polygon that covers the whole clip area 'box', it is replaced by a
    rectangle just around that area: the same visible result, but the geometry worker
    keeps its precision (it scales by the biggest coordinate). Else None."""
    pts = polygon(page_path)
    if box is None or pts is None or not is_convex(pts):
        return None
    m = 0.01 * max(box[2] - box[0], box[3] - box[1], 1e-6)
    big = (box[0] - m, box[1] - m, box[2] + m, box[3] + m)
    corners = [(big[0], big[1]), (big[2], big[1]), (big[2], big[3]), (big[0], big[3])]
    if all(convex_contains(pts, c) for c in corners):
        return big
    return None


# ------------------------------------------------------------------ two-color fades
def color_at(stops, t):
    """RGB of the gradient at t (pad beyond the ends)."""
    if t <= stops[0][0]:
        return rgb(stops[0][1])
    for (o0, c0, _), (o1, c1, _) in zip(stops, stops[1:]):
        if o0 <= t <= o1 and o1 > o0:
            k = (t - o0) / (o1 - o0)
            a, b = rgb(c0), rgb(c1)
            return tuple(a[i] + (b[i] - a[i]) * k for i in range(3))
    return rgb(stops[-1][1])


def span(stops, tmin, tmax):
    """The stops that cover the visible part tmin..tmax: from the last stop at or before
    tmin to the first at or after tmax (the padding colors beyond the ends). When the
    visible part starts or ends inside a fade, the fade's own end color is used, not the
    in-between color at the edge. [(t, '#rrggbb')]"""
    n = len(stops)
    i0 = max([i for i in range(n) if stops[i][0] <= tmin] or [0])
    i1 = min([i for i in range(n) if stops[i][0] >= tmax] or [n - 1])
    i1 = max(i1, i0)
    pts = [(min(stops[i0][0], tmin), stops[i0][1])]
    pts += [(o, c) for o, c, _ in stops[i0 + 1:i1]]
    pts.append((max(stops[i1][0], tmax), stops[i1][1]))
    return pts


def dist(a, b):
    return sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5


def two_color_fade(stops, tmin, tmax):
    """('solid', color) / ('fade', A, B, t0, t1) / None (not a two-color fade).

    A at t0 and B at t1, linear in between, A before and B after, matching where the
    original crosses 10%, 50% and 90% of the way from A to B."""
    sp = span(stops, tmin, tmax)
    pts = [(t, rgb(c)) for t, c in sp]
    A, B = sp[0][1], sp[-1][1]
    a, b = rgb(A), rgb(B)
    ab = tuple(b[i] - a[i] for i in range(3))
    L2 = sum(v * v for v in ab)
    if L2 ** 0.5 <= SAME:
        # Both ends the same color: solid only if nothing else shows in between.
        if all(dist(c, a) <= SNAP for _, c in pts):
            return ('solid', A)
        return None
    fs, last = [], -1.0
    for t, c in pts:
        f = sum((c[i] - a[i]) * ab[i] for i in range(3)) / L2
        near = tuple(a[i] + ab[i] * f for i in range(3))
        if dist(c, near) > OFF_LINE or f < last - 0.03:
            return None  # a third color, or it goes back: not one fade
        last = max(last, f)
        fs.append((t, min(max(f, 0.0), 1.0)))

    def cross(level):
        for (t0, f0), (t1, f1) in zip(fs, fs[1:]):
            if f0 < level <= f1:
                return t0 + (t1 - t0) * (level - f0) / (f1 - f0)
        return fs[0][0] if fs[0][1] >= level else fs[-1][0]

    t10, t50, t90 = cross(0.1), cross(0.5), cross(0.9)
    w = max((t90 - t10) / 0.8, 1e-6 * (tmax - tmin), 1e-9)
    t0, t1 = closest_fit(fs, tmin, tmax, t50 - w / 2, t50 + w / 2)
    return ('fade', A, B, t0, t1)


def closest_fit(fs, tmin, tmax, t0, t1):
    """Start / end of the straight two-color fade whose largest difference from the
    original (fs: [(t, 0..1 of the way from A to B)], straight between) over the visible
    part tmin..tmax is smallest. Starts from the 10/50/90% estimate and moves the ends
    in shrinking steps while that improves."""
    def orig(t):
        if t <= fs[0][0]:
            return fs[0][1]
        for (a, fa), (b, fb) in zip(fs, fs[1:]):
            if a <= t <= b:
                return fa if b <= a else fa + (fb - fa) * (t - a) / (b - a)
        return fs[-1][1]

    n = 240
    ts = sorted(set([tmin + (tmax - tmin) * k / n for k in range(n + 1)] +
                    [t for t, _ in fs if tmin <= t <= tmax]))
    ref = [orig(t) for t in ts]

    def err(a, b):
        if b - a <= 1e-12:
            return 2.0
        return max(abs(min(max((t - a) / (b - a), 0.0), 1.0) - f) for t, f in zip(ts, ref))

    best = err(t0, t1)
    step = (t1 - t0) / 4.0
    while step > (t1 - t0) * 1e-4:
        moved = False
        for d0, d1 in ((step, 0), (-step, 0), (0, step), (0, -step), (step, step), (-step, -step),
                       (step, -step), (-step, step)):
            e = err(t0 + d0, t1 + d1)
            if e < best - 1e-12:
                best, t0, t1, moved = e, t0 + d0, t1 + d1, True
                break
        if not moved:
            step /= 2.0
    return t0, t1


class GradientFills(object):
    """Collects gradient keys while reading, and writes real gradients for them."""

    def __init__(self, svg, note):
        self.svg = svg
        self.note = note
        self.keys = {}      # 'url(#__flatgradN)' -> spec
        self.written = {}   # (key, to_parent) -> id
        self.shared = {}    # (A, B) -> id of the shared two-color gradient
        self.finished = False

    def new_key(self, spec):
        key = 'url(#__flatgrad%d)' % (len(self.keys) + 1)
        self.keys[key] = spec
        return key

    def is_key(self, color):
        return color in self.keys

    def shade(self, paint):
        """What the geometry worker needs to compare gradient parts of one object group
        (see join_shades there): a solid color, or a two-color fade in page coordinates.
        None for a gradient kept with its own stops (never joined)."""
        if not self.is_key(paint):
            c = rgb(paint)
            return {'A': c, 'B': c}
        spec = self.keys[paint]
        if spec['kind'] != 'shared':
            return None
        c = spec['coords']
        p0 = spec['M'].apply_to_point((c[0], c[1]))
        p1 = spec['M'].apply_to_point((c[2], c[3]))
        return {'A': rgb(spec['ends'][0]), 'B': rgb(spec['ends'][1]),
                'p0': [p0[0], p0[1]], 'p1': [p1[0], p1[1]]}

    # -------------------------------------------------------------- one object
    def split(self, el, T, page_d, clip_box, clip_ds=()):
        """(d, [(paint, None, label suffix)]) for a gradient-filled object: one entry (a
        solid color or a gradient key), or none when nothing is painted. d is the object's
        outline in page coordinates (maybe replaced by a smaller rectangle, see shrink).
        clip_ds: the outlines (page coordinates) of the clips the object is shown through.
        None when the fill is not a gradient this module handles (left out, as before)."""
        ref = (el.specified_style().get('fill') or '').strip()
        m = re.match(r"url\(\s*['\"]?#([^'\")]+)['\"]?\s*\)", ref)
        g = self.svg.getElementById(m.group(1)) if m else None
        if not isinstance(g, GRADIENTS):
            return None
        gs = chain(self.svg, g)
        try:
            stops = stop_list(gs)
            if not stops:
                return page_d, []  # no stops: the fill is not painted
            if any(op < 0.999 for _, _, op in stops):
                self.note('transparency was ignored (treated as fully opaque)')
            # gradient coordinates -> page
            ubox = attr(gs, 'gradientUnits', 'objectBoundingBox') == 'objectBoundingBox'
            G = inkex.Transform(attr(gs, 'gradientTransform', None))
            if ubox:
                bb = inkex.Path(el.path).bounding_box()
                if bb is None or bb.width <= 0 or bb.height <= 0:
                    return page_d, []  # SVG doesn't paint a bbox gradient on a flat box
                G = inkex.Transform((bb.width, 0, 0, bb.height, bb.left, bb.top)) @ G
            M = T @ G
            linear = isinstance(g, inkex.LinearGradient)
            if linear:
                coords = [number(attr(gs, n, dflt), ubox) for n, dflt in
                          (('x1', '0%'), ('y1', '0%'), ('x2', '100%'), ('y2', '0%'))]
            else:
                cx = number(attr(gs, 'cx', '50%'), ubox)
                cy = number(attr(gs, 'cy', '50%'), ubox)
                coords = {'cx': cx, 'cy': cy, 'r': number(attr(gs, 'r', '50%'), ubox),
                          'fx': number(attr(gs, 'fx', None) or cx, ubox),
                          'fy': number(attr(gs, 'fy', None) or cy, ubox)}
                if attr(gs, 'fr') is not None:
                    coords['fr'] = number(attr(gs, 'fr'), ubox)
        except Unsupported as exc:
            self.note('a gradient was left out (%s)' % exc)
            return None
        spread = attr(gs, 'spreadMethod', 'pad')

        page_path = inkex.Path(el.path).to_absolute().transform(T)
        box = box_and(clip_box, bbox_of(page_d))
        small = shrink(page_path, box)
        if small is not None:
            page_d = rect_d(small)
            box = box_and(box, small)

        if len(set(c for _, c, _ in stops)) == 1:
            return page_d, [(stops[0][1], None, '')]
        whole = {'kind': 'linear' if linear else 'radial', 'coords': coords,
                 'stops': stops, 'M': M, 'spread': spread}
        if linear and spread in (None, 'pad') and box is not None:
            res = self.linear(coords, stops, M, [page_d] + list(clip_ds))
            if res is not None:
                return page_d, [res]
        self.note('a gradient with more than two colors (or radial / repeating) was kept '
                  'as it is, with its own colors')
        return page_d, [(self.new_key(whole), None, ' gradient')]

    def linear(self, coords, stops, M, outlines):
        """A solid color or a shared two-color fade key for the visible part, or None.

        The visible part along the gradient: what lies inside every outline (the object
        and its clips). Each outline's points, curve handles included, contain its curves,
        so the range found can only be a little wider than the real one, never narrower."""
        x1, y1, x2, y2 = coords
        vx, vy = x2 - x1, y2 - y1
        L2 = vx * vx + vy * vy
        if L2 < 1e-18:
            return (stops[-1][1], None, '')  # zero-length vector: the last stop's color
        try:
            Minv = -M
        except Exception:
            return None
        lo, hi = float('-inf'), float('inf')
        for d in outlines:
            ts = []
            for p in inkex.Path(d).control_points:
                q = Minv.apply_to_point((p.x, p.y))
                ts.append(((q[0] - x1) * vx + (q[1] - y1) * vy) / L2)
            if ts:
                lo, hi = max(lo, min(ts)), min(hi, max(ts))
        if not lo < hi:
            return None
        res = two_color_fade(stops, lo, hi)
        if res is None:
            return None
        if res[0] == 'solid':
            return (res[1], None, '')
        _, A, B, t0, t1 = res
        p0 = (x1 + t0 * vx, y1 + t0 * vy)
        p1 = (x1 + t1 * vx, y1 + t1 * vy)
        key = self.new_key({'kind': 'shared', 'ends': (A, B), 'coords': [p0[0], p0[1], p1[0], p1[1]],
                            'M': M})
        return (key, None, ' fade')

    def finish(self):
        """Before writing: fade end colors that are nearly the same across the design
        become one color (the one used most), so one change recolors all of them. Example:
        gold #d2ae6d, and #caa769 at the end of a few fades -> both #d2ae6d. Then each
        fade gets its shared gradient for its pair of colors, either way round."""
        if self.finished:
            return
        self.finished = True
        count = {}
        for spec in self.keys.values():
            for c in spec.get('ends', ()):
                count[c] = count.get(c, 0) + 1
        anchors, to = [], {}
        for c in sorted(count, key=lambda c: (-count[c], c)):
            a = next((a for a in anchors if dist(rgb(a), rgb(c)) <= PALETTE), None)
            if a is None:
                anchors.append(c)
                a = c
            to[c] = a
        merged = sorted(set(c for c in to if to[c] != c))
        if merged:
            self.note('fade colors merged with a nearly equal color: %s' % ', '.join(
                '%s -> %s' % (c, to[c]) for c in merged))
        for spec in self.keys.values():
            if spec['kind'] != 'shared':
                continue
            A, B = to[spec['ends'][0]], to[spec['ends'][1]]
            if A == B:
                spec['kind'], spec['color'] = 'solid', A
                continue
            if B < A:
                A, B = B, A
                c = spec['coords']
                spec['coords'] = [c[2], c[3], c[0], c[1]]
            spec['pair'] = (A, B)

    # -------------------------------------------------------------- writing
    def shared_gradient(self, pair):
        """The shared gradient for a color pair: only the two stops, no position. Every
        fade between these colors links to it; change a color here to change them all."""
        if self.shared.get(pair):
            return self.shared[pair]
        g = inkex.LinearGradient()
        for off, col in ((0, pair[0]), (1, pair[1])):
            s = inkex.Stop()
            s.set('offset', str(off))
            s.style = inkex.Style({'stop-color': col, 'stop-opacity': '1'})
            g.append(s)
        self.svg.defs.append(g)
        g.set_random_id('fade')
        g.set(INKSCAPE_NS + 'label', 'fade %s %s' % pair)
        for s in g:
            s.set_random_id('fadestop')
        self.shared[pair] = g.get_id()
        return g.get_id()

    def make_paint(self, key, to_parent):
        """A gradient in the defs for a key, placed for paths written with 'to_parent'
        (page -> the new paths' parent). Returns 'url(#id)'."""
        self.finish()
        if self.keys[key]['kind'] == 'solid':
            return self.keys[key]['color']
        wkey = (key, tuple(round(v, 12) for v in to_parent.to_hexad()))
        if wkey in self.written:
            return 'url(#%s)' % self.written[wkey]
        spec = self.keys[key]
        if spec['kind'] in ('linear', 'shared'):
            g = inkex.LinearGradient()
            for n, v in zip(('x1', 'y1', 'x2', 'y2'), spec['coords']):
                g.set(n, repr(float(v)))
        else:
            g = inkex.RadialGradient()
            for n, v in spec['coords'].items():
                g.set(n, repr(float(v)))
        g.set('gradientUnits', 'userSpaceOnUse')
        t = to_parent @ spec['M']
        if not t == inkex.Transform():
            g.set('gradientTransform', str(t))
        if spec.get('spread') not in (None, 'pad'):
            g.set('spreadMethod', spec['spread'])
        if spec['kind'] == 'shared':
            g.set(XLINK, '#' + self.shared_gradient(spec['pair']))
            g.set(INKSCAPE_NS + 'collect', 'always')
        else:
            for off, col, op in spec['stops']:
                s = inkex.Stop()
                s.set('offset', repr(float(off)))
                # opaque, as everywhere in Flatten: what lies under it has been cut away
                s.style = inkex.Style({'stop-color': col, 'stop-opacity': '1'})
                g.append(s)
        self.svg.defs.append(g)
        g.set_random_id('flatgradient')
        for s in g:
            s.set_random_id('flatstop')
        self.written[wkey] = g.get_id()
        return 'url(#%s)' % g.get_id()
