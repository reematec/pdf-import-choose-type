#!/usr/bin/env python3
"""Isolate: dim and lock everything except the selection, and restore it afterwards.

--mode=isolate  every visible object that is not the selection, inside it, or a group it
                sits in, is dimmed to a quarter of its opacity and locked, so clicks only
                reach the selection. Its previous opacity and lock state are kept in marks.
--mode=end      restores each marked object's exact opacity and lock state and removes
                the marks. Hidden objects are never touched.
"""
import inkex

DIM = 0.25
MARK = 'data-isolate'              # previous inline opacity ('' if none)
MARK_LOCK = 'data-isolate-lock'    # previous sodipodi:insensitive ('' if none)
OLD_MARK = 'data-isolate-hidden'   # first version hid objects; still restored by End
LOCK = '{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd}insensitive'
SKIP_TAGS = {'defs', 'namedview', 'metadata', 'title', 'desc', 'style', 'script'}


def local(el):
    tag = el.tag
    return tag.rsplit('}', 1)[-1] if isinstance(tag, str) else ''


def is_hidden(el):
    return el.style.get('display') == 'none' or el.get('display') == 'none'


def set_style(el, prop, value):
    style = el.style
    if value:
        style[prop] = value
    elif prop in style:
        del style[prop]
    el.style = style


def restore_attr(el, name, value):
    if value:
        el.set(name, value)
    else:
        el.attrib.pop(name, None)


def number(value, default=1.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class Isolate(inkex.EffectExtension):
    def add_arguments(self, pars):
        pars.add_argument('--mode', default='isolate', choices=['isolate', 'end'])

    def effect(self):
        if self.options.mode == 'end':
            self.end()
        else:
            self.isolate()

    def end(self):
        dimmed = self.svg.xpath('//*[@%s]' % MARK)
        for el in dimmed:
            set_style(el, 'opacity', el.get(MARK))
            restore_attr(el, LOCK, el.get(MARK_LOCK))
            el.attrib.pop(MARK, None)
            el.attrib.pop(MARK_LOCK, None)
        hidden = self.svg.xpath('//*[@%s]' % OLD_MARK)
        for el in hidden:
            set_style(el, 'display', el.get(OLD_MARK))
            el.attrib.pop(OLD_MARK, None)
        if not dimmed and not hidden:
            self.msg('Nothing is isolated.')

    def isolate(self):
        selected = list(self.svg.selection.values())
        if not selected:
            raise inkex.AbortExtension('Select an object or group first, then run Isolate selection.')
        if self.svg.xpath('//*[@%s]' % MARK):
            raise inkex.AbortExtension('Something is already isolated. Run End isolation first.')
        # The selection and every group (or layer) it sits in stay as they are.
        path_to_root = set()
        for el in selected:
            node = el
            while node is not None:
                path_to_root.add(node)
                node = node.getparent()
        keep = set(selected)
        # Dim and lock the visible siblings at each level between the root and the selection.
        for parent in path_to_root:
            if parent in keep:
                continue  # inside the selection: leave untouched
            for child in parent:
                if child in path_to_root or local(child) in SKIP_TAGS:
                    continue
                if not isinstance(child.tag, str) or is_hidden(child):
                    continue
                prev = child.style.get('opacity') or ''
                child.set(MARK, prev)
                child.set(MARK_LOCK, child.get(LOCK) or '')
                base = number(prev, number(child.get('opacity')))
                set_style(child, 'opacity', ('%.4f' % (base * DIM)).rstrip('0').rstrip('.') or '0')
                child.set(LOCK, 'true')


if __name__ == '__main__':
    Isolate().run()
