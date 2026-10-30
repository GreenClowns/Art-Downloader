#!/bin/bash
# ============================================================
#  Art Downloader installer for Linux (tested on Fedora)
#
#  Install:    bash install.sh
#  Uninstall:  bash install.sh uninstall   (or: bash install.sh удалить)
#              or right-click the menu icon → "Uninstall Art Downloader"
# ============================================================

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$HOME" || "$HOME" == "/" || ! -d "$HOME" ]]; then
    echo "✘ HOME is not set correctly; refusing to continue."
    exit 1
fi
if [[ $EUID -eq 0 && -z "$ARTDL_ALLOW_ROOT" ]]; then
    echo "✘ Don't run this installer with sudo — run it as your normal user."
    echo "  (It asks for your password itself only if a system package is missing.)"
    exit 1
fi
APP_DIR="$HOME/.local/share/art-downloader"
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP="$DESKTOP_DIR/art-downloader.desktop"
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
ICON="$ICON_DIR/art-downloader.png"
OLD_ICON="$HOME/.local/share/icons/hicolor/scalable/apps/art-downloader.svg"

# Messages in Russian for ru/uk/be/kk locales, English otherwise
LOCALE="${LANGUAGE:-${LC_ALL:-${LC_MESSAGES:-${LANG:-en}}}}"
case "${LOCALE:0:2}" in ru|uk|be|kk) RU=1 ;; *) RU= ;; esac
t() { if [[ -n "$RU" ]]; then echo "$1"; else echo "$2"; fi; }

remove_app() {
    rm -rf "$APP_DIR"
    rm -f "$DESKTOP" "$ICON" "$OLD_ICON"
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
}

case "$1" in
    uninstall|--uninstall|удалить)
        remove_app
        t "Art Downloader удалён. Скачанные картинки и gallery-dl не тронуты." \
          "Art Downloader uninstalled. Your downloads and gallery-dl were not touched."
        exit 0 ;;
esac

if [[ ! -f "$SRC_DIR/art_downloader.py" || ! -f "$SRC_DIR/assets/icon.png" \
      || ! -f "$SRC_DIR/requirements.txt" ]]; then
    t "✘ Не нашёл файлы программы рядом с install.sh." \
      "✘ Program files not found next to install.sh."
    t "  Запускай install.sh из распакованной папки проекта." \
      "  Run install.sh from the unpacked project folder."
    exit 1
fi

# Package manager: dnf (Fedora) or apt (Ubuntu/Debian)
if command -v dnf >/dev/null 2>&1; then
    PKG_INSTALL="sudo dnf install -y"; TK_PKG="python3-tkinter"
elif command -v apt-get >/dev/null 2>&1; then
    PKG_INSTALL="sudo apt-get install -y"; TK_PKG="python3-tk"
else
    PKG_INSTALL=""; TK_PKG="python3-tkinter"
fi

need_pkg() {
    if [[ -z "$PKG_INSTALL" ]]; then
        t "✘ Установи пакет $1 вручную и запусти install.sh ещё раз." \
          "✘ Please install the $1 package manually and run install.sh again."
        exit 1
    fi
    $PKG_INSTALL "$1" || { t "✘ Не удалось установить $1" "✘ Could not install $1"; exit 1; }
}

echo
t "=== Установка Art Downloader ===" "=== Installing Art Downloader ==="
echo

# 1. GUI library
if python3 -c "import tkinter" 2>/dev/null; then
    t "✔ tkinter уже установлен" "✔ tkinter is already installed"
elif [[ -n "$ARTDL_SKIP_DEPS" ]]; then
    echo "• tkinter skipped (test)"
else
    t "• Ставлю $TK_PKG (может спросить пароль от компьютера)…" \
      "• Installing $TK_PKG (may ask for your computer password)…"
    need_pkg "$TK_PKG"
fi

# 2. The app's own Python environment with its libraries
#    (separate from system Python, so system updates can't break it)
VENV="$APP_DIR/venv"
mkdir -p "$APP_DIR"
if [[ ! -x "$VENV/bin/python" ]]; then
    if ! python3 -m venv "$VENV" 2>/dev/null; then
        rm -rf "$VENV"
        [[ "$TK_PKG" == "python3-tk" ]] && need_pkg python3-venv
        python3 -m venv "$VENV" || { t "✘ Не удалось создать окружение Python" \
                                       "✘ Could not create the Python environment"; exit 1; }
    fi
fi
t "• Ставлю библиотеки (customtkinter, gallery-dl)…" \
  "• Installing libraries (customtkinter, gallery-dl)…"
"$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
"$VENV/bin/python" -m pip install --quiet --upgrade -r "$SRC_DIR/requirements.txt" \
    || { t "✘ Не удалось установить библиотеки. Проверь интернет и запусти ещё раз." \
           "✘ Could not install the libraries. Check your internet and try again."
         exit 1; }
t "✔ Библиотеки установлены" "✔ Libraries installed"

# 3. App, icon, menu entry
mkdir -p "$APP_DIR" "$DESKTOP_DIR" "$ICON_DIR"
cp "$SRC_DIR/art_downloader.py" "$APP_DIR/art_downloader.py"
cp "$SRC_DIR/assets/icon.png" "$ICON"
cp "$SRC_DIR/assets/icon.png" "$APP_DIR/icon.png"   # window icon
rm -f "$OLD_ICON"

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
Comment=Download art and comics from DeviantArt and other sites
Comment[ru]=Скачивание артов и комиксов с DeviantArt и других сайтов
Comment[uk]=Завантаження артів і коміксів з DeviantArt та інших сайтів
Comment[es]=Descarga arte y cómics de DeviantArt y otros sitios
Comment[ja]=DeviantArt などからイラストや漫画をダウンロード
Comment[zh_CN]=从 DeviantArt 等网站下载插画和漫画
Exec=$VENV/bin/python $APP_DIR/art_downloader.py
Icon=$ICON
Terminal=false
Categories=Network;FileTransfer;
StartupWMClass=art-downloader
Actions=uninstall;

[Desktop Action uninstall]
Name=Uninstall Art Downloader
Name[ru]=Удалить Art Downloader
Exec=bash $APP_DIR/uninstall.sh
EOF

chmod +x "$DESKTOP"
update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true

t "✔ Программа установлена" "✔ App installed"
echo
t "Открывай «Art Downloader» из меню приложений:" \
  "Open “Art Downloader” from your applications menu:"
t "нажми клавишу Super (со значком Windows) и начни вводить «Art»." \
  "press the Super key (Windows logo) and start typing “Art”."
echo

if [[ -z "$ARTDL_SKIP_DEPS" ]]; then
    nohup "$VENV/bin/python" "$APP_DIR/art_downloader.py" >/dev/null 2>&1 &
    disown 2>/dev/null || true
    t "Сейчас программа откроется сама." "The app will open now."
fi
