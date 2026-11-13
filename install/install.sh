#!/bin/sh
# Installs (or updates) the "PDF Import (Choose type)" extension for Inkscape on Linux.
# Run it with:  sh install/install.sh
# Then restart Inkscape.
#
# It installs for each Inkscape it finds: the distribution package or AppImage,
# Flatpak, and Snap. Nothing outside your home folder is changed.

set -eu

NAME=pdf_import_choose_type
FLATPAK_ID=org.inkscape.Inkscape
CHECK_PY='from PIL import ImageCms; import numpy'

SRC=$(cd "$(dirname "$0")/../extension" 2>/dev/null && pwd) || SRC=
if [ -z "$SRC" ] || [ ! -f "$SRC/$NAME.inx" ]; then
    echo 'Could not find the extension files. Keep this installer in the "install" folder of the download.'
    exit 1
fi

CONFIG=${XDG_CONFIG_HOME:-$HOME/.config}
NATIVE_DIR=$CONFIG/inkscape/extensions
FLATPAK_DIR=$HOME/.var/app/$FLATPAK_ID/config/inkscape/extensions
SNAP_DIR=$HOME/snap/inkscape/current/.config/inkscape/extensions

installed=0
problems=0

copy_to() {
    dest=$1/$NAME
    mkdir -p "$1"
    rm -rf "$dest"
    cp -R "$SRC" "$dest"
    find "$dest" -name __pycache__ -type d -prune -exec rm -rf {} +
    echo "Installed to $dest"
    installed=$((installed + 1))
}

# The command that installs Pillow and numpy on this distribution.
python_packages_hint() {
    ids=
    if [ -r /etc/os-release ]; then
        ids=$(. /etc/os-release; echo "${ID:-} ${ID_LIKE:-}")
    fi
    case " $ids " in
        *" debian "*|*" ubuntu "*) echo 'sudo apt install python3-pil python3-numpy' ;;
        *" fedora "*|*" rhel "*)   echo 'sudo dnf install python3-pillow python3-numpy' ;;
        *" arch "*)                echo 'sudo pacman -S python-pillow python-numpy' ;;
        *" suse "*|*" opensuse "*) echo 'sudo zypper install python3-Pillow python3-numpy' ;;
        *)                         echo 'python3 -m pip install --user pillow numpy' ;;
    esac
}

# --- Distribution package or AppImage: uses ~/.config/inkscape -------------------
native=
INKSCAPE_PATH=$(command -v inkscape 2>/dev/null || true)
case "$INKSCAPE_PATH" in
    ''|/snap/*|*/flatpak/exports/*) ;;
    *) native=yes ;;
esac
if [ -d "$CONFIG/inkscape" ]; then native=yes; fi

if [ -n "$native" ]; then
    copy_to "$NATIVE_DIR"
    if ! python3 -c "$CHECK_PY" >/dev/null 2>&1; then
        problems=$((problems + 1))
        echo
        echo 'Python is missing Pillow (with ImageCms) or numpy, which this extension needs.'
        echo 'Install them with:'
        echo "    $(python_packages_hint)"
        echo 'If you only use the Inkscape AppImage, you can ignore this: it brings its own Python.'
    fi
fi

# --- Flatpak ----------------------------------------------------------------------
if command -v flatpak >/dev/null 2>&1 && flatpak info "$FLATPAK_ID" >/dev/null 2>&1; then
    copy_to "$FLATPAK_DIR"
    if ! flatpak run --command=python3 "$FLATPAK_ID" -c "$CHECK_PY" >/dev/null 2>&1; then
        problems=$((problems + 1))
        echo
        echo 'The Flatpak version of Inkscape is missing Pillow (with ImageCms) or numpy.'
        echo 'Flatpak apps cannot use system Python packages. Use the distribution package'
        echo 'or the AppImage of Inkscape for this extension instead.'
    fi
fi

# --- Snap -------------------------------------------------------------------------
if command -v snap >/dev/null 2>&1 && snap list inkscape >/dev/null 2>&1; then
    copy_to "$SNAP_DIR"
fi

# --- Nothing found: install for a regular Inkscape -----------------------------------
if [ "$installed" -eq 0 ]; then
    echo 'No Inkscape was found. Installing for a regular (distribution or AppImage) Inkscape.'
    copy_to "$NATIVE_DIR"
fi

echo
if [ "$problems" -eq 0 ]; then
    echo 'Restart Inkscape to use it.'
else
    echo 'Fix the problem above, then restart Inkscape.'
fi
