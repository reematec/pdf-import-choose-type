#!/bin/sh
# Installs (or updates) the "JFIF image" input extension for Inkscape on Linux.
# Run it with:  sh jfif-import/install.sh     Then restart Inkscape.
set -eu
SRC=$(cd "$(dirname "$0")" && pwd)
if [ ! -f "$SRC/jfif_import.inx" ]; then
    echo 'Could not find the extension files. Keep this installer in the "jfif-import" folder of the download.'
    exit 1
fi
CONFIG=${XDG_CONFIG_HOME:-$HOME/.config}
installed=0
for dir in "$CONFIG/inkscape/extensions" \
           "$HOME/.var/app/org.inkscape.Inkscape/config/inkscape/extensions" \
           "$HOME/snap/inkscape/current/.config/inkscape/extensions"; do
    case "$dir" in
        "$CONFIG"/*) ;;                               # always install for distro / AppImage
        *) [ -d "$(dirname "$(dirname "$dir")")" ] || continue ;;  # Flatpak / Snap only if present
    esac
    mkdir -p "$dir/jfif_import"
    cp "$SRC/jfif_import.inx" "$SRC/jfif_import.py" "$dir/jfif_import/"
    echo "Installed to $dir/jfif_import"
    installed=$((installed + 1))
done
if ! python3 -c 'import PIL' >/dev/null 2>&1; then
    echo 'Python is missing Pillow. On Ubuntu or Debian: sudo apt install python3-pil'
fi
echo 'Restart Inkscape to use it.'
