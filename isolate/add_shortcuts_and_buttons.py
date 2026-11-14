"""Add keyboard shortcuts and Selector-toolbar buttons for Isolate selection / End isolation.

Close Inkscape first, then run (Windows):
    "C:\\Program Files\\Inkscape\\bin\\python.exe" add_shortcuts_and_buttons.py
Linux:
    python3 add_shortcuts_and_buttons.py [path to Inkscape's ui/toolbar-select.ui]
The optional path picks another Inkscape's toolbar file (for example an extracted AppImage:
squashfs-root/usr/share/inkscape/ui/toolbar-select.ui). Inkscape 1.2 and older build the
Select toolbar in code, so only the shortcuts are added there.

- Shortcuts: Ctrl+Alt+I = Isolate selection, Ctrl+Alt+Shift+I = End isolation, written to
  your personal shortcut file (keys/default.xml in the Inkscape profile). Change them later
  in Edit > Preferences > Interface > Keyboard.
- Buttons: two buttons on the Select tool's controls bar (top), after "touch select".
  The toolbar file is read from your installed Inkscape and saved with the buttons in your
  profile (ui/toolbar-select.ui), which Inkscape uses instead of its own. Run this again
  after upgrading Inkscape. Delete that file to get Inkscape's toolbar back.

Existing files are backed up with the ending ".before-isolate".
"""
import os
import re
import shutil
import subprocess
import sys

ACTIONS = [
    ('app.isolate.selection', '&lt;ctrl&gt;&lt;alt&gt;i', 'object-hidden',
     'Isolate selection (Ctrl+Alt+I): dim and lock everything else'),
    ('app.isolate.end', '&lt;ctrl&gt;&lt;alt&gt;&lt;shift&gt;i', 'object-visible',
     'End isolation (Ctrl+Alt+Shift+I)'),
]
MARKER = '<!-- isolate buttons -->'


def inkscape_running():
    if sys.platform != 'win32':
        return subprocess.run(['pgrep', '-x', 'inkscape'], capture_output=True).returncode == 0
    out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq inkscape.exe'], capture_output=True, text=True).stdout
    return 'inkscape.exe' in out.lower()


def profile_dir():
    if sys.platform == 'win32':
        return os.path.join(os.environ['APPDATA'], 'inkscape')
    return os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'inkscape')


def system_ui_file():
    exe_dir = os.path.dirname(sys.executable)  # Inkscape's own Python lives in Inkscape's bin folder
    cands = [os.path.join(exe_dir, '..', 'share', 'inkscape', 'ui', 'toolbar-select.ui'),
             r'C:\Program Files\Inkscape\share\inkscape\ui\toolbar-select.ui',
             '/usr/share/inkscape/ui/toolbar-select.ui']
    for c in cands:
        if os.path.isfile(c):
            return os.path.normpath(c)
    raise SystemExit("Could not find Inkscape's toolbar-select.ui. Run this with Inkscape's Python.")


def backup(path):
    if os.path.exists(path) and not os.path.exists(path + '.before-isolate'):
        shutil.copy2(path, path + '.before-isolate')


def add_shortcuts(prof):
    path = os.path.join(prof, 'keys', 'default.xml')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            s = f.read()
    else:
        s = '<?xml version="1.0"?>\n<keys name="My shortcuts">\n</keys>\n'
    added = 0
    for action, keys, _icon, _tip in ACTIONS:
        if 'gaction="%s"' % action in s:
            continue
        s = s.replace('</keys>', '  <bind gaction="%s" keys="%s" />\n</keys>' % (action, keys), 1)
        added += 1
    if added:
        backup(path)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(s)
    return path, added


def button(action, icon, tip):
    return ('    <child>\n'
            '      <object class="GtkButton">\n'
            '        <property name="visible">True</property>\n'
            '        <property name="can-focus">True</property>\n'
            '        <property name="tooltip-text">%s</property>\n'
            '        <property name="action-name">%s</property>\n'
            '        <property name="relief">none</property>\n'
            '        <child>\n'
            '          <object class="GtkImage">\n'
            '            <property name="visible">True</property>\n'
            '            <property name="icon-name">%s</property>\n'
            '          </object>\n'
            '        </child>\n'
            '      </object>\n'
            '    </child>\n') % (tip, action, icon)


def add_buttons(prof, src=None):
    src = src or system_ui_file()
    with open(src, encoding='utf-8') as f:
        s = f.read()
    if not s.strip():
        # Inkscape 1.2 and older build the Select toolbar in code; their file is empty.
        print('Toolbar buttons: not added. This Inkscape (%s) builds the Select toolbar itself '
              '(Inkscape 1.2 or older); buttons need Inkscape 1.3 or later. The shortcuts work.' % src)
        return None
    # Insert after the "touch select" toggle, i.e. before the first separator after it.
    # (its icon's </object></child>, then the button's own </object></child>)
    m = re.search(r'id="_select_touch_btn".*?</object>\s*</child>\s*</object>\s*</child>\s*', s, re.S)
    if not m:
        raise SystemExit("This Inkscape's toolbar-select.ui has an unexpected layout; buttons not added.")
    sep = ('    <child>\n      <object class="GtkSeparator">\n'
           '        <property name="visible">True</property>\n      </object>\n    </child>\n')
    block = '%s\n%s%s' % (MARKER, sep, ''.join(button(a, i, t) for a, _k, i, t in ACTIONS))
    s = s[:m.end()] + block + s[m.end():]
    dest = os.path.join(prof, 'ui', 'toolbar-select.ui')
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    backup(dest)
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(s)
    return dest


def main():
    if inkscape_running():
        raise SystemExit('Inkscape is open. Close it (File > Quit) and run this again.')
    prof = profile_dir()
    path, added = add_shortcuts(prof)
    print('Shortcuts: %s (%s)' % ('added' if added else 'already set', path))
    dest = add_buttons(prof, sys.argv[1] if len(sys.argv) > 1 else None)
    if dest:
        print('Toolbar buttons: added (%s)' % dest)
    print('Done. Start Inkscape and pick the Select tool (S).')


if __name__ == '__main__':
    main()
