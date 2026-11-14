#!/bin/sh
# Installs (or updates) the "Flatten clips and overlaps" extension for Inkscape on Linux.
# Run it with:  sh flatten/install.sh     Then restart Inkscape.
set -eu
SRC=$(cd "$(dirname "$0")" && pwd)
if [ ! -f "$SRC/flatten.py" ]; then
    echo 'Could not find the extension files. Keep this installer in the "flatten" folder of the download.'
    exit 1
fi
case "$(uname -m)" in
    x86_64|amd64) ;;
    *) echo "Note: the bundled geometry library is for x86-64 Linux; this computer is $(uname -m)."
       echo "Install it yourself (python3 -m pip install skia-pathops==0.9.2) for Flatten to work." ;;
esac
CONFIG=${XDG_CONFIG_HOME:-$HOME/.config}
for dir in "$CONFIG/inkscape/extensions" \
           "$HOME/.var/app/org.inkscape.Inkscape/config/inkscape/extensions" \
           "$HOME/snap/inkscape/current/.config/inkscape/extensions"; do
    case "$dir" in
        "$CONFIG"/*) ;;                               # always install for distro / AppImage
        *) [ -d "$(dirname "$(dirname "$dir")")" ] || continue ;;  # Flatpak / Snap only if present
    esac
    rm -rf "$dir/flatten"
    mkdir -p "$dir/flatten/vendor/pathops"
    cp "$SRC/flatten.py" "$SRC/flatten_worker.py" "$SRC/flatten.inx" "$dir/flatten/"
    # Linux needs only the .so; the Windows files (.pyd, .dll) are left out.
    for f in "$SRC"/vendor/pathops/*.py "$SRC"/vendor/pathops/*.so; do
        cp "$f" "$dir/flatten/vendor/pathops/"
    done
    cp -r "$SRC/vendor/skia_pathops-0.9.2.dist-info" "$dir/flatten/vendor/"
    echo "Installed to $dir/flatten"
done
if python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    :
else
    echo
    echo 'Flatten also needs Python 3.10 or later as python3 (Ubuntu 22.04 and later have it).'
    echo 'Install it with your package manager, for example: sudo apt install python3'
fi
echo 'Restart Inkscape to use it.'
