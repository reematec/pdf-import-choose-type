"""Geometry helper for "Flatten clips and overlaps".

Runs in a standard Python 3.10+ (not Inkscape's own), with skia-pathops from ./vendor.
Reads JSON on stdin, writes JSON on stdout:

in:  {"clips": {key: {"d": str, "rule": "nonzero"|"evenodd"}},
      "units": [{"items": [{"d", "rule", "fill", "stroke", "local_d", "matrix", "width",
                            "cap", "join", "miter", "clips": [key, ...]}, ...]}, ...]}
     "d" is in page coordinates; a stroke uses "local_d" (the object's own coordinates),
     "width" (in those coordinates) and "matrix" [a, b, c, d, e, f] to the page.
     Items are in paint order (bottom first). Paths are absolute M/L/C/Z, all in one
     coordinate system.
out: {"units": [{"pieces": [{"color": str, "d": str}], "errors": int}], "warnings": [str]}

For each unit: every line becomes a filled shape, every shape is cut by all its clips,
hidden parts are cut away (top to bottom), and the visible parts are merged into paths:
one per object, one per color for an inner group, outlines joined with a same-color
surface under them when "join" is on (see path_keys). Pieces carry a "label".
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, 'vendor')
sys.path.insert(0, VENDOR)
if hasattr(os, 'add_dll_directory'):
    os.add_dll_directory(os.path.join(VENDOR, 'pathops'))
import pathops  # noqa: E402

# Skia's stroker and curve approximations assume pixel-sized units, so the drawing is
# scaled to about 100000 units while working, and scaled back on output.
TARGET_SIZE = 100000.0
TOL = 0.05  # tolerance for round caps/joins, in scaled units
SCALE = 1.0

CAPS = {'butt': pathops.LineCap.BUTT_CAP, 'round': pathops.LineCap.ROUND_CAP,
        'square': pathops.LineCap.SQUARE_CAP}
JOINS = {'miter': pathops.LineJoin.MITER_JOIN, 'round': pathops.LineJoin.ROUND_JOIN,
         'bevel': pathops.LineJoin.BEVEL_JOIN}
TOKEN = re.compile(r'[MLCQZmlcqz]|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?')
INTERSECTION, DIFFERENCE, UNION = (pathops.PathOp.INTERSECTION, pathops.PathOp.DIFFERENCE,
                                   pathops.PathOp.UNION)


def parse(d, rule='nonzero', scale=None, closed=False):
    """Absolute M/L/C/Q/Z path data -> scaled pathops.Path.

    closed=True closes every outline, as a fill does in SVG. Skia's boolean operations
    treat an unclosed outline differently and dropped parts of such shapes."""
    k = SCALE if scale is None else scale
    p = pathops.Path()
    p.fillType = pathops.FillType.EVEN_ODD if rule == 'evenodd' else pathops.FillType.WINDING
    toks = TOKEN.findall(d)
    i, cmd, open_ = 0, None, False
    cur = start = (0.0, 0.0)
    while i < len(toks):
        t = toks[i]
        if t in 'MLCQZmlcqz':
            cmd = t.upper()
            i += 1
            if cmd == 'Z' and open_:
                p.close()
                open_ = False
                cur = start
            continue
        if cmd == 'M':
            if closed and open_:
                p.close()
            cur = start = (float(toks[i]) * k, float(toks[i + 1]) * k)
            p.moveTo(*cur)
            open_ = True
            cmd = 'L'
            i += 2
            continue
        elif cmd == 'L':
            p.lineTo(float(toks[i]) * k, float(toks[i + 1]) * k)
            i += 2
        elif cmd == 'C':
            c = [float(v) * k for v in toks[i:i + 6]]
            if closed and flat_curve(cur, c):
                # All points on one line: the curve covers no area. As a curve that folds
                # back on itself it made Skia's simplify fill a hole (a logo in the 04
                # sample); as a line (or nothing, when it ends where it started) it's exact.
                if (c[4], c[5]) != cur:
                    p.lineTo(c[4], c[5])
            else:
                p.cubicTo(*c)
            i += 6
        elif cmd == 'Q':
            p.quadTo(*[float(v) * k for v in toks[i:i + 4]])
            i += 4
        else:
            raise ValueError('unexpected path data')
        cur = (float(toks[i - 2]) * k, float(toks[i - 1]) * k)
    if closed and open_:
        p.close()
    return p


def flat_curve(p0, c):
    """True when a cubic's points all lie on one straight line (within a tiny tolerance)."""
    pts = [p0, (c[0], c[1]), (c[2], c[3]), (c[4], c[5])]
    a, b = max(((u, v) for u in pts for v in pts), key=lambda uv: (uv[0][0] - uv[1][0]) ** 2 + (uv[0][1] - uv[1][1]) ** 2)
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = (dx * dx + dy * dy) ** 0.5
    if n == 0:
        return True  # all four points are the same point
    return all(abs((q[0] - a[0]) * dy - (q[1] - a[1]) * dx) <= n * n * 1e-9 for q in pts)


def to_d(p):
    out = []

    def f(v):
        return ('%.6f' % (v / SCALE)).rstrip('0').rstrip('.')

    for verb, pts in p.segments:
        if verb == 'moveTo':
            out.append('M%s,%s' % (f(pts[0][0]), f(pts[0][1])))
        elif verb == 'lineTo':
            out.append('L%s,%s' % (f(pts[0][0]), f(pts[0][1])))
        elif verb == 'curveTo':
            out.append('C' + ' '.join('%s,%s' % (f(x), f(y)) for x, y in pts))
        elif verb == 'qCurveTo':
            # quadratic spline: on-curve points lie between consecutive control points
            for k in range(len(pts) - 1):
                c = pts[k]
                e = pts[k + 1] if k == len(pts) - 2 else (
                    (pts[k][0] + pts[k + 1][0]) / 2, (pts[k][1] + pts[k + 1][1]) / 2)
                out.append('Q%s,%s %s,%s' % (f(c[0]), f(c[1]), f(e[0]), f(e[1])))
        elif verb == 'closePath':
            out.append('Z')
    return ' '.join(out)


def _op(a, b, kind):
    return pathops.op(a, b, kind, fix_winding=True, keep_starting_points=False)


def raw_op(a, b, kind):
    """Skia boolean operation, with retries when Skia reports a failure."""
    try:
        return _op(a, b, kind)
    except pathops.PathOpsError:
        pass
    try:  # 1: clean both shapes (removes self-overlaps and degenerate parts)
        return _op(pathops.simplify(a, fix_winding=True), pathops.simplify(b, fix_winding=True), kind)
    except pathops.PathOpsError:
        pass
    eps = TARGET_SIZE * 1e-5  # 2: nudge the second shape by 0.00001 of the drawing size
    return _op(pathops.simplify(a, fix_winding=True),
               pathops.simplify(b.transform(1, 0, 0, 1, eps, eps), fix_winding=True), kind)


def empty(p):
    return not list(p.segments)


def apart(a, b):
    """True when the bounding boxes don't overlap (then the result is obvious)."""
    x0, y0, x1, y1 = a.bounds
    u0, v0, u1, v1 = b.bounds
    return u0 >= x1 or u1 <= x0 or v0 >= y1 or v1 <= y0


def joined(a, b):
    """Two shapes that don't overlap, as one path (no Skia operation needed)."""
    p = pathops.Path()
    p.fillType = pathops.FillType.WINDING
    p.addPath(a)
    p.addPath(b)
    return p


UNSURE = [0]  # operations whose result could not be confirmed
SAMPLES = 48


def halton(i, base):
    f, r = 1.0, 0.0
    while i > 0:
        f /= base
        r += f * (i % base)
        i //= base
    return r


def sample_points(box):
    x0, y0, x1, y1 = box
    return [(x0 + (x1 - x0) * halton(i, 2), y0 + (y1 - y0) * halton(i, 3)) for i in range(1, SAMPLES + 1)]


def mismatches(result, a, b, kind):
    """Sample points where the result disagrees with the rule of the operation.

    Testing whether a point is inside a shape needs no boolean operation, so this
    check is independent of the Skia failures it is meant to catch."""
    # Points over each shape's own box, so a small shape next to a big one is checked too.
    bad = 0
    for pt in sample_points(a.bounds) + sample_points(b.bounds):
        ina, inb = a.contains(pt), b.contains(pt)
        want = (ina and not inb) if kind == DIFFERENCE else (ina and inb) if kind == INTERSECTION else (ina or inb)
        if result.contains(pt) != want:
            bad += 1
    return bad


def op(a, b, kind):
    """Boolean operation on clean shapes, checked by point sampling.

    Skia sometimes returns a wrong result without an error (seen: a difference 13 times
    larger than the shape it was cut from, a white area cut away entirely). Every result
    is checked; if it fails, it is computed another way.
    """
    if empty(a) or empty(b) or apart(a, b):
        if kind == UNION:
            return b if empty(a) else a if empty(b) else joined(a, b)
        if kind == INTERSECTION:
            return pathops.Path()
        return a  # DIFFERENCE
    def direct():
        return raw_op(a, b, kind)

    def swapped():
        # The same operation with the shapes in the other order. Skia refused a
        # difference that it computed fine as the reverse difference (04 sample).
        if kind == DIFFERENCE:
            return _op(b, a, pathops.PathOp.REVERSE_DIFFERENCE)
        return _op(b, a, kind)

    def cleaned():
        return raw_op(pathops.simplify(a, fix_winding=True), pathops.simplify(b, fix_winding=True), kind)

    def by_formula():
        if kind == DIFFERENCE:   # a - (a & b)
            return raw_op(a, raw_op(a, b, INTERSECTION), DIFFERENCE)
        if kind == INTERSECTION:  # a - (a - b)
            return raw_op(a, raw_op(a, b, DIFFERENCE), DIFFERENCE)
        return joined(a, raw_op(b, a, DIFFERENCE))  # a + (b - a)

    def shifted():
        e = TARGET_SIZE * 1.37e-5
        r = raw_op(a.transform(1, 0, 0, 1, e, e * 0.61), b.transform(1, 0, 0, 1, e, e * 0.61), kind)
        return r.transform(1, 0, 0, 1, -e, -e * 0.61)

    best, best_bad = None, None
    for make in (direct, swapped, cleaned, by_formula, shifted):
        try:
            cand = make()
        except pathops.PathOpsError:
            continue
        bad = mismatches(cand, a, b, kind)
        # Two disagreeing points are allowed: a sample can land exactly on an edge.
        if bad <= 2:
            return cand
        if best_bad is None or bad < best_bad:
            best, best_bad = cand, bad
    UNSURE[0] += 1
    if best is None:  # every way failed: keep the first shape unchanged (reported as unsure)
        return a if kind != INTERSECTION else pathops.Path()
    return best


def region_mismatches(shape, raw, subs):
    """Sample points where a cleaned shape disagrees with the original path.

    Points cover the whole shape and each outline's own box, so a small hole is checked."""
    bad = 0
    for box in [raw.bounds] + [s.bounds for s in subs]:
        for pt in sample_points(box):
            if shape.contains(pt) != raw.contains(pt):
                bad += 1
    return bad


def region(d, rule='nonzero'):
    """A filled area (fill or clip): closed outlines, cleaned into a well-formed shape.

    Skia's simplify filled a hole whose edge lies almost on the outer edge (a logo in the
    04 sample), so the result is checked against the original path; if it's wrong, the
    shape is built outline by outline with checked operations."""
    raw = parse(d, rule, closed=True)
    first = pathops.simplify(raw, fix_winding=True)
    subs = [parse('M' + s, 'nonzero', closed=True) for s in d.split('M') if s.strip()]
    if len(subs) <= 1 and rule != 'evenodd':
        subs = []  # one outline, nonzero: the whole-shape samples cover it
    if region_mismatches(first, raw, subs) <= 2:
        return first
    best, best_bad = first, region_mismatches(first, raw, subs)
    cands = [lambda: pathops.simplify(raw, fix_winding=False)]
    if rule == 'evenodd' or len(subs) > 1:
        def by_outlines():
            # even-odd: each outline toggles the area it covers (a xor b = (a-b) + (b-a))
            acc = pathops.Path()
            for s in subs:
                s = pathops.simplify(s, fix_winding=True)
                acc = op(op(acc, s, DIFFERENCE), op(s, acc, DIFFERENCE), UNION)
            return acc
        cands.append(by_outlines)
    for make in cands:
        try:
            cand = make()
        except pathops.PathOpsError:
            continue
        bad = region_mismatches(cand, raw, subs)
        if bad <= 2:
            return cand
        if bad < best_bad:
            best, best_bad = cand, bad
    UNSURE[0] += 1
    return best


def outline(it):
    """The filled shapes of one item: (color, path, is outline) for fill, then for stroke."""
    shapes = []
    if it.get('fill'):
        shapes.append((it['fill'], region(it['d'], it.get('rule', 'nonzero')), False))
    if it.get('stroke') and it.get('width', 0) > 0:
        # Stroke in the object's own coordinates, then transform: exact also when the
        # object is scaled unevenly (the pen is then an ellipse, as Inkscape draws it).
        nums = [abs(float(v)) for v in TOKEN.findall(it['local_d']) if v not in 'MLCQZmlcqz']
        k = TARGET_SIZE / max(max(nums or [1.0]), 1e-12)
        s = parse(it['local_d'], scale=k)
        s.stroke(it['width'] * k, CAPS.get(it.get('cap'), CAPS['butt']),
                 JOINS.get(it.get('join'), JOINS['miter']), max(float(it.get('miter', 4)), 1.0))
        s.convertConicsToQuads(TOL)
        s = pathops.simplify(s, fix_winding=True)
        a, b, c, d, e, f = it['matrix']
        s = s.transform(SCALE * a / k, SCALE * b / k, SCALE * c / k, SCALE * d / k, SCALE * e, SCALE * f)
        shapes.append((it['stroke'], s, True))
    return shapes


def flatten_unit(items, clip_defs, join=True):
    global SCALE
    biggest = 0.0
    for it in items:
        for v in TOKEN.findall(it['d']):
            if v not in 'MLCQZmlcqz':
                biggest = max(biggest, abs(float(v)))
    SCALE = TARGET_SIZE / biggest if biggest > 0 else 1.0
    UNSURE[0] = 0
    clips = {k: region(v['d'], v.get('rule', 'nonzero')) for k, v in clip_defs.items()}
    pieces, owners, errors = [], [], 0  # owners[i] = (item index, is outline) of pieces[i]
    for n, it in enumerate(items):
        try:
            for color, shape, is_line in outline(it):
                for key in it.get('clips', []):
                    shape = op(shape, clips[key], INTERSECTION)
                if not empty(shape):
                    pieces.append((color, shape))
                    owners.append((n, is_line))
        except Exception as exc:  # degenerate or unsupported geometry: report, keep going
            errors += 1
            if os.environ.get('FLATTEN_DEBUG'):
                sys.stderr.write('item %s: %s: %s\n' % (it.get('id'), type(exc).__name__, exc))
    # Cut away what lies under other pieces: each piece loses the pieces above it, one
    # at a time. Cutting a union of many pieces at once made Skia drop small holes
    # (dots in a large base) without an error, and the point check can't see one missing
    # hole in a big shape; with one small piece per cut, its samples cover that piece.
    visible = []
    for i, (color, shape) in enumerate(pieces):
        vis = shape
        for j in range(i + 1, len(pieces)):
            if empty(vis):
                break
            if not apart(vis, pieces[j][1]):
                vis = op(vis, pieces[j][1], DIFFERENCE)
        visible.append(vis)
    # Which output path each piece goes to (see path_keys), then merge per path in order
    # of first appearance.
    keys, labels, joined = path_keys(items, pieces, owners, join)
    merged, order = {}, []
    for i, vis in enumerate(visible):
        if empty(vis):
            continue
        k = keys[i]
        if k not in merged:
            merged[k] = pathops.Path()
            order.append(k)
        merged[k] = op(merged[k], vis, UNION)
    pieces_out = [{'color': k[2], 'label': labels[k], 'd': to_d(merged[k])} for k in order]
    return {'pieces': [p for p in pieces_out if p['d']], 'errors': errors, 'unsure': UNSURE[0],
            'joined': joined}


JOIN_SAMPLES = 32


def under(i, pt, pieces, owners):
    """Index of the piece drawn directly under piece i at pt (not i's own fill), or None."""
    x, y = pt
    for j in range(i - 1, -1, -1):
        if owners[j][0] == owners[i][0]:
            continue  # the outline's own fill is not a surface it was drawn on
        x0, y0, x1, y1 = pieces[j][1].bounds
        if x0 <= x <= x1 and y0 <= y <= y1 and pieces[j][1].contains(pt):
            return j
    return None


def path_keys(items, pieces, owners, join):
    """The output path of each piece, and a label for each path.

    - A panel's own objects each get their own path (one per color, when an object's
      fill and outline differ): two elements with the same color stay separately
      recolorable (e.g. white stripes next to a white base).
    - Objects inside one inner group (a pattern such as dots) are merged per color.
    - With join: an outline drawn on a surface of its own color (white rings around dots
      on a white base) goes to that surface's path, so they recolor together. "Drawn on"
      is decided by sampling: more than half of the outline lies directly on that color.
    """
    keys, labels, joined = [], {}, 0
    for i, (color, shape) in enumerate(pieces):
        n, is_line = owners[i]
        it = items[n]
        if it.get('group'):
            k = ('g', it['group'], color)
            lab = '%s %s' % (it.get('group_label') or it['group'], color)
        else:
            k = ('o', it['id'], color)
            lab = it.get('label') or it['id']
            if is_line and it.get('fill') and it['fill'] != color:
                lab += ' outline'
        if join and is_line:
            votes, inside, tries = {}, 0, 0
            x0, y0, x1, y1 = shape.bounds
            while inside < JOIN_SAMPLES and tries < JOIN_SAMPLES * 20:
                tries += 1
                pt = (x0 + (x1 - x0) * halton(tries, 2), y0 + (y1 - y0) * halton(tries, 3))
                if not shape.contains(pt):
                    continue
                inside += 1
                j = under(i, pt, pieces, owners)
                if j is not None and pieces[j][0] == color:
                    votes[j] = votes.get(j, 0) + 1
            if inside and sum(votes.values()) * 2 > inside:
                j = max(votes, key=votes.get)
                k = keys[j]
                joined += 1
        keys.append(k)
        labels.setdefault(k, lab)
    return keys, labels, joined


def main():
    data = json.load(sys.stdin)
    join = data.get('join', True)
    result = {'units': [flatten_unit(u['items'], data['clips'], join) for u in data['units']]}
    json.dump(result, sys.stdout)


if __name__ == '__main__':
    main()
