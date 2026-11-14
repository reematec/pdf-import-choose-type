#!/bin/sh
# Installs (or updates) the "Isolate" extension for Inkscape on Linux.
# Run it with:  sh isolate/install.sh     Then restart Inkscape.
set -eu
SRC=$(cd "$(dirname "$0")" && pwd)
if [ ! -f "$SRC/isolate.py" ]; then
    echo 'Could not find the extension files. Keep this installer in the "isolate" folder of the download.'
    exit 1
fi
CONFIG=${XDG_CONFIG_HOME:-$HOME/.config}
for dir in "$CONFIG/inkscape/extensions" \
           "$HOME/.var/app/org.inkscape.Inkscape/config/inkscape/extensions" \
           "$HOME/snap/inkscape/current/.config/inkscape/extensions"; do
    case "$dir" in
        "$CONFIG"/*) ;;                               # always install for distro / AppImage
        *) [ -d "$(dirname "$(dirname "$dir")")" ] || continue ;;  # Flatpak / Snap only if present
    esac
    mkdir -p "$dir/isolate"
    cp "$SRC/isolate.py" "$SRC/isolate_selection.inx" "$SRC/isolate_end.inx" "$dir/isolate/"
    echo "Installed to $dir/isolate"
done
echo 'Restart Inkscape to use it.'
