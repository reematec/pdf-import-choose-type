"""Make Inkscape's docked dialogs show icon-only tabs, and optionally set the dock width.

Usage (close Inkscape first):
    python dock_icons_only.py            icon-only tabs
    python dock_icons_only.py 340        icon-only tabs and a 340 px wide dock
    python dock_icons_only.py --profile DIR [340]   use this Inkscape profile folder

Works on Windows and Linux (Inkscape 1.3 / 1.4). Backups are saved next to the
changed files with the ending ".before-dock".
"""
import os
import re
import shutil
import subprocess
import sys

LABELS_OFF = '2'  # /options/notebooklabels: 0 Automatic, 1 Active, 2 Off (icons only)


def inkscape_running():
    try:
        if sys.platform == 'win32':
            out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq inkscape.exe'],
                                 capture_output=True, text=True).stdout
            return 'inkscape.exe' in out.lower()
        return subprocess.run(['pgrep', '-x', 'inkscape'], capture_output=True).returncode == 0
    except OSError:
        return False


def profile_dir():
    try:
        out = subprocess.run(['inkscape', '--user-data-directory'],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        if out and os.path.isdir(out):
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    if sys.platform == 'win32':
        return os.path.join(os.environ['APPDATA'], 'inkscape')
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'inkscape')


def backup(path):
    bak = path + '.before-dock'
    if not os.path.exists(bak):
        shutil.copy2(path, bak)


def set_labels(prefs):
    with open(prefs, encoding='utf-8') as f:
        s = f.read()
    m = re.search(r'(<group\s+id="notebooklabels"\s+value=")([^"]*)(")', s)
    if m:
        if m.group(2) == LABELS_OFF:
            return False
        s = s[:m.start(2)] + LABELS_OFF + s[m.end(2):]
    else:
        opt = re.search(r'<group\s+id="options"[^>]*>', s)
        if not opt:
            raise SystemExit('Could not find the options section in %s.' % prefs)
        s = (s[:opt.end()] + '\n    <group\n       id="notebooklabels"\n       value="%s" />'
             % LABELS_OFF + s[opt.end():])
    backup(prefs)
    with open(prefs, 'w', encoding='utf-8', newline='') as f:
        f.write(s)
    return True


def set_width(state, width):
    with open(state, encoding='utf-8') as f:
        s = f.read()
    new, n = re.subn(r'(?m)^ColumnWidth=\d+', 'ColumnWidth=%d' % width, s)
    if n == 0:
        return False
    backup(state)
    with open(state, 'w', encoding='utf-8', newline='') as f:
        f.write(new)
    return True


def main():
    args = sys.argv[1:]
    prof = None
    if '--profile' in args:
        i = args.index('--profile')
        prof = args[i + 1]
        del args[i:i + 2]
    width = int(args[0]) if args else None

    if inkscape_running():
        raise SystemExit('Inkscape is open. Close it (File > Quit) and run this again.')
    prof = prof or profile_dir()
    prefs = os.path.join(prof, 'preferences.xml')
    if not os.path.isfile(prefs):
        raise SystemExit('No preferences in %s. Start Inkscape once, close it, and run this again.' % prof)

    print('Inkscape profile: %s' % prof)
    print('Icon-only tabs: %s' % ('set' if set_labels(prefs) else 'already set'))
    if width:
        state = os.path.join(prof, 'dialogs-state-ex.ini')
        if os.path.isfile(state) and set_width(state, width):
            print('Dock width: %d px' % width)
        else:
            print('Dock width: no docked dialogs saved yet. Open some, close Inkscape, and run this again.')
    print('Done. Start Inkscape.')


if __name__ == '__main__':
    main()
