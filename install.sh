#!/bin/bash
# ============================================================
#  Установщик Art Downloader для Linux (проверено на Fedora)
#
#  Установка:  bash install.sh
#  Удаление:   bash install.sh удалить
#              (или правый клик по значку → «Удалить Art Downloader»)
# ============================================================

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/art-downloader"
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP="$DESKTOP_DIR/art-downloader.desktop"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
ICON="$ICON_DIR/art-downloader.svg"

remove_app() {
    rm -rf "$APP_DIR"
    rm -f "$DESKTOP" "$ICON"
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
}

if [[ "$1" == "удалить" || "$1" == "--uninstall" ]]; then
    remove_app
    echo "Art Downloader удалён. Скачанные картинки и gallery-dl не тронуты."
    exit 0
fi

if [[ ! -f "$SRC_DIR/art_downloader.py" || ! -f "$SRC_DIR/assets/icon.svg" ]]; then
    echo "✘ Не нашёл файлы программы рядом с install.sh."
    echo "  Запускай install.sh из распакованной папки проекта."
    exit 1
fi

# Установщик пакетов: dnf (Fedora) или apt (Ubuntu/Debian)
if command -v dnf >/dev/null 2>&1; then
    PKG_INSTALL="sudo dnf install -y"; TK_PKG="python3-tkinter"
elif command -v apt-get >/dev/null 2>&1; then
    PKG_INSTALL="sudo apt-get install -y"; TK_PKG="python3-tk"
else
    PKG_INSTALL=""; TK_PKG="python3-tkinter"
fi

need_pkg() {
    if [[ -z "$PKG_INSTALL" ]]; then
        echo "✘ Установи пакет $1 вручную и запусти install.sh ещё раз."
        exit 1
    fi
    $PKG_INSTALL "$1" || { echo "✘ Не удалось установить $1"; exit 1; }
}

echo
echo "=== Установка Art Downloader ==="
echo

# 1. Библиотека для окон
if python3 -c "import tkinter" 2>/dev/null; then
    echo "✔ tkinter уже установлен"
elif [[ -n "$ARTDL_SKIP_DEPS" ]]; then
    echo "• tkinter пропущен (тест)"
else
    echo "• Ставлю $TK_PKG (может спросить пароль от компьютера)…"
    need_pkg "$TK_PKG"
fi

# 2. gallery-dl
if command -v gallery-dl >/dev/null 2>&1 || [[ -x "$HOME/.local/bin/gallery-dl" ]]; then
    echo "✔ gallery-dl уже установлен"
elif [[ -n "$ARTDL_SKIP_DEPS" ]]; then
    echo "• gallery-dl пропущен (тест)"
else
    echo "• Ставлю gallery-dl…"
    command -v pipx >/dev/null 2>&1 || need_pkg pipx
    pipx install gallery-dl || { echo "✘ Не удалось установить gallery-dl"; exit 1; }
    pipx ensurepath >/dev/null 2>&1 || true
fi

# 3. Программа, значок, ярлык в меню
mkdir -p "$APP_DIR" "$DESKTOP_DIR" "$ICON_DIR"
cp "$SRC_DIR/art_downloader.py" "$APP_DIR/art_downloader.py"
cp "$SRC_DIR/assets/icon.svg" "$ICON"

cat > "$APP_DIR/uninstall.sh" <<EOF
#!/bin/bash
rm -rf "$APP_DIR"
rm -f "$DESKTOP" "$ICON"
update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
EOF

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Art Downloader
Comment=Скачивание артов и комиксов с DeviantArt и других сайтов
Exec=python3 $APP_DIR/art_downloader.py
Icon=$ICON
Terminal=false
Categories=Network;Graphics;
StartupWMClass=art-downloader
Actions=uninstall;

[Desktop Action uninstall]
Name=Удалить Art Downloader
Exec=bash $APP_DIR/uninstall.sh
EOF

chmod +x "$DESKTOP"
update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true

echo "✔ Программа установлена"
echo
echo "Открывай «Art Downloader» из меню приложений:"
echo "нажми клавишу Super (со значком Windows) и начни вводить «Art»."
echo

if [[ -z "$ARTDL_SKIP_DEPS" ]]; then
    nohup python3 "$APP_DIR/art_downloader.py" >/dev/null 2>&1 &
    disown 2>/dev/null || true
    echo "Сейчас программа откроется сама."
fi
