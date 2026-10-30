#!/bin/bash
# Builds dist/ArtDownloader-<version>-x86_64.AppImage — one file that runs
# with a double click on any modern Linux (Python, libraries and gallery-dl
# are inside).
#
#   bash linux/build_appimage.sh [version]
#
# Needs: Python 3.10+ with Tkinter, pip, curl. Used by GitHub Actions; can
# also be run by hand.
set -euo pipefail

VERSION="${1:-0.0.0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$ROOT/build/appimage"
DIST="$ROOT/dist"
PYTHON="${PYTHON:-python3}"

# Pinned tools (official GitHub releases), verified by SHA-256
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64"
RUNTIME_SHA256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Version must look like 1.2.3, got '$VERSION'" >&2
    exit 1
fi

rm -rf "$WORK"
mkdir -p "$WORK/tools" "$DIST"

fetch() {  # url sha256 destination
    curl -sSfL --retry 3 -o "$3" "$1"
    echo "$2  $3" | sha256sum -c --quiet - || { echo "Checksum mismatch: $1" >&2; exit 1; }
}
fetch "$APPIMAGETOOL_URL" "$APPIMAGETOOL_SHA256" "$WORK/tools/appimagetool"
fetch "$RUNTIME_URL" "$RUNTIME_SHA256" "$WORK/tools/runtime"
chmod +x "$WORK/tools/appimagetool"

# 1. The app with everything it needs, as a folder (PyInstaller)
SRC="$WORK/src"
mkdir -p "$SRC"
cp "$ROOT/art_downloader.py" "$SRC/"
"$PYTHON" - "$SRC/art_downloader.py" "$VERSION" <<'EOF'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1])
s, n = re.subn(r'^APP_VERSION = "[^"]*"', f'APP_VERSION = "{sys.argv[2]}"',
               p.read_text(encoding="utf-8"), flags=re.M)
assert n == 1
p.write_text(s, encoding="utf-8")
EOF
"$PYTHON" -m PyInstaller --noconfirm --clean --windowed \
    --name ArtDownloader \
    --add-data "$ROOT/assets/icon.png:assets" \
    --collect-submodules gallery_dl \
    --collect-data customtkinter \
    --hidden-import PIL.ImageTk \
    --hidden-import PIL._tkinter_finder \
    --distpath "$WORK/pyinstaller" --workpath "$WORK/pyi-build" \
    --specpath "$WORK" "$SRC/art_downloader.py"

# 2. AppDir: launcher, menu entry, icon
APPDIR="$WORK/ArtDownloader.AppDir"
mkdir -p "$APPDIR/usr/lib"
cp -a "$WORK/pyinstaller/ArtDownloader" "$APPDIR/usr/lib/art-downloader"
cp "$ROOT/assets/icon.png" "$APPDIR/art-downloader.png"
cp "$ROOT/assets/icon.png" "$APPDIR/.DirIcon"
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/art-downloader/ArtDownloader" "$@"
EOF
chmod +x "$APPDIR/AppRun"
cat > "$APPDIR/art-downloader.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Art Downloader
Comment=Download art and comics from DeviantArt and other sites
Comment[ru]=Скачивание артов и комиксов с DeviantArt и других сайтов
Exec=ArtDownloader
Icon=art-downloader
Terminal=false
Categories=Network;FileTransfer;
StartupWMClass=art-downloader
EOF

# 3. Pack into one file
OUT="$DIST/ArtDownloader-$VERSION-x86_64.AppImage"
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$WORK/tools/appimagetool" \
    --no-appstream --runtime-file "$WORK/tools/runtime" "$APPDIR" "$OUT"
chmod +x "$OUT"
echo "Built $OUT"
