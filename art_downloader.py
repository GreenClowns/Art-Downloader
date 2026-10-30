#!/usr/bin/env python3
"""
Art Downloader — a small GUI on top of gallery-dl.

Features:
  * downloads folders, series, galleries, profiles and single works from
    DeviantArt (and any other site gallery-dl supports);
  * downloads EVERY image inside multi-image DeviantArt posts;
  * saves in order: one folder per gallery, files numbered 001, 002, …
    (by publication date, gallery order or title);
  * DeviantArt sign-in (OAuth button, login/password or browser cookies);
  * parallel downloads, automatic retry, resume after stop;
  * interface in English and Russian;
  * runs on Linux and Windows.

Run:       python3 art_downloader.py
Requires:  gallery-dl and Tkinter (Fedora: sudo dnf install python3-tkinter)
           The Windows .exe has everything built in.
"""

import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

if os.name != "nt" and sys.platform != "darwin":
    # CustomTkinter copies its shape font into ~/.fonts on Linux and draws
    # rounded corners poorly if that folder doesn't exist yet
    try:
        os.makedirs(os.path.expanduser("~/.fonts"), exist_ok=True)
    except OSError:
        pass

import customtkinter as ctk  # noqa: E402

APP_TITLE = "Art Downloader"
WINDOWS = os.name == "nt"
FROZEN = getattr(sys, "frozen", False)      # running as a PyInstaller .exe
GALLERY_DL_FLAG = "--gallery-dl"            # exe runs its built-in gallery-dl

if WINDOWS:
    CONFIG_PATH = (Path(os.environ.get("APPDATA") or Path.home())
                   / "ArtDownloader" / "settings.json")
else:
    CONFIG_PATH = Path.home() / ".config" / "art_downloader.json"
DEFAULT_BASE = Path.home() / "ArtDownloads"
MAX_THREADS = 5
MONO_FONT = "Consolas" if WINDOWS else "monospace"
CREATE_NO_WINDOW = 0x08000000               # hide console windows on Windows

BROWSER_KEYS = ["firefox", "chrome", "chromium", "none"]
ORDER_KEYS = ["auto", "date", "gallery", "name"]
SETTINGS_VERSION = 2

LINKS_FILE = "links.txt"
ARCHIVE_FILE = ".downloaded.sqlite3"
ORDER_FILE = ".order.json"
INCOMING = ".incoming"          # downloads land here, then get organized

# How gallery-dl names DeviantArt files before we rename them
DA_FILENAME = "da{index}_{num|'1':>02}_{title}.{extension}"
DA_FILE_RE = re.compile(r"^da(\d+)_(\d+)_(.*?)(\.[^.]+)?$")
# Line printed by gallery-dl while collecting the list of works
COLLECT_FMT = "@@{index}\t{published_time}\t{folder[title]}\t{title}"
COLLECT_RE = re.compile(r"^@@(\d+)\t([^\t]*)\t([^\t]*)\t(.*)$")
# gallery-dl prints "[3/10] https://…" before each link from an input file
PROGRESS_RE = re.compile(r"^\[(\d+)/(\d+)\] \S")
CHAPTER_RE = re.compile(
    r"(?<![^\W_])(?:chapter|chap|ch|глава|episode|ep)\.?[\s_\-]*(\d+)",
    re.IGNORECASE,
)
CHAPTER_DIR_RE = re.compile(r"^Chapter \d+$")


# --------------------------------------------------------------------------
# Language
# --------------------------------------------------------------------------

LANGUAGES = {"en": "English", "ru": "Русский"}
_lang = "en"


RU_LANG_IDS = {0x19, 0x22, 0x23, 0x3F}       # Windows: ru, uk, be, kk


def detect_lang(env=None, windows_lang_id=None):
    """Interface language from the system locale: Russian for ru/uk/be/kk."""
    env = os.environ if env is None else env
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = (env.get(var) or "").lower()
        if value:
            return "ru" if value[:2] in ("ru", "uk", "be", "kk") else "en"
    if windows_lang_id is None and WINDOWS:
        try:
            import ctypes
            windows_lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        except Exception:
            windows_lang_id = 0
    if windows_lang_id:
        return "ru" if (windows_lang_id & 0x3FF) in RU_LANG_IDS else "en"
    return "en"


def set_lang(code):
    global _lang
    _lang = code if code in LANGUAGES else "en"


def get_lang():
    return _lang


def files_word(n):
    """'3 файла' / '5 файлов' / '1 file' / '2 files'."""
    if _lang != "ru":
        return f"{n} file" + ("" if n == 1 else "s")
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} файл"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} файла"
    return f"{n} файлов"


def tr(ru, en):
    """Pick the string for the current interface language."""
    return ru if _lang == "ru" else en


def browser_label(key):
    if key == "none":
        return tr("не брать из браузера", "don't use a browser")
    return key


def order_label(key):
    return {"auto": tr("автоматически", "automatic"),
            "date": tr("по дате публикации", "by publication date"),
            "gallery": tr("как в галерее", "as in the gallery"),
            "name": tr("по названию", "by title")}[key]


def migrate_config(cfg):
    """Settings from older versions stored Russian labels instead of keys."""
    cfg = dict(cfg)
    old_order = {"по дате публикации": "date", "как в галерее": "gallery",
                 "по названию": "name"}
    if cfg.get("order") in old_order:
        cfg["order"] = old_order[cfg["order"]]
    if cfg.get("order") not in ORDER_KEYS:
        cfg["order"] = "auto"
    # "date" was the old default; "auto" gets comics right, so switch once
    if cfg.get("settings_version", 1) < SETTINGS_VERSION:
        if cfg["order"] == "date":
            cfg["order"] = "auto"
        cfg["settings_version"] = SETTINGS_VERSION
    if cfg.get("browser") in ("не брать из браузера", "без входа"):
        cfg["browser"] = "none"
    if cfg.get("browser") not in BROWSER_KEYS:
        cfg["browser"] = "firefox"
    return cfg


# --------------------------------------------------------------------------
# Logic without the window (tested separately)
# --------------------------------------------------------------------------

def find_gallery_dl():
    """
    Command that runs gallery-dl, as a list (or None if it isn't found):
    the .exe's built-in copy, gallery-dl from PATH, ~/.local/bin (pipx),
    or the Python module.
    """
    if FROZEN:
        return [sys.executable, GALLERY_DL_FLAG]
    if importlib.util.find_spec("gallery_dl"):   # e.g. the app's own venv
        return [sys.executable, "-m", "gallery_dl"]
    found = shutil.which("gallery-dl")
    if found:
        return [found]
    local = Path.home() / ".local" / "bin" / "gallery-dl"
    if local.exists():
        return [str(local)]
    if importlib.util.find_spec("gallery_dl"):
        return [sys.executable, "-m", "gallery_dl"]
    return None


def resource_path(name):
    """A file shipped with the app (next to the script or inside the exe)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for candidate in (base / name, base / "assets" / name):
        if candidate.exists():
            return candidate
    return None


# --------------------------------------------------------------------------
# Linux AppImage: menu entry and system programs
# --------------------------------------------------------------------------

APPIMAGE = os.environ.get("APPIMAGE")       # set by the AppImage runtime
DESKTOP_ID = "art-downloader"
REMOVE_MENU_FLAG = "--remove-menu-entry"
SELF_TEST_FLAG = "--self-test"
APPIMAGE_KEY = "X-ArtDownloader-AppImage"


def restore_host_env():
    """
    A bundled Linux app runs with LD_LIBRARY_PATH pointing at its own
    libraries. Programs of the user's system (browser, file manager,
    notify-send) must not inherit that, or they may fail to start. glibc
    reads LD_LIBRARY_PATH only at start-up, so this process isn't affected,
    and the app's own helper processes get it again from the launcher.
    """
    if not FROZEN or WINDOWS:
        return
    orig = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig:
        os.environ["LD_LIBRARY_PATH"] = orig
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)


def gdl_env(extra=None):
    """
    Environment for running gallery-dl. When it's the app's own bundled copy,
    PYINSTALLER_RESET_ENVIRONMENT makes it start as an independent program
    that finds its own libraries (otherwise it looks for them in the
    parent's setup and fails, e.g. "libBLT…: cannot open shared object").
    """
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
               PYTHONUTF8="1", **(extra or {}))
    if FROZEN:
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def desktop_quote(arg):
    """One argument for the Exec= line of a .desktop file (freedesktop spec)."""
    if re.fullmatch(r"[\w/.+,:@=-]+", arg):
        return arg.replace("%", "%%")
    quoted = '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'
    return quoted.replace("\\", "\\\\").replace("%", "%%")


def menu_paths():
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return (data / "applications" / f"{DESKTOP_ID}.desktop",
            data / "icons" / "hicolor" / "256x256" / "apps" / f"{DESKTOP_ID}.png")


def desktop_entry(exec_path, icon_path):
    run = desktop_quote(str(exec_path))
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Art Downloader\n"
        "Comment=Download art and comics from DeviantArt and other sites\n"
        "Comment[ru]=Скачивание артов и комиксов с DeviantArt и других сайтов\n"
        f"Exec={run}\n"
        f"Icon={icon_path}\n"
        "Terminal=false\n"
        "Categories=Network;FileTransfer;\n"
        "StartupWMClass=art-downloader\n"
        "Actions=remove;\n"
        f"{APPIMAGE_KEY}={exec_path}\n"
        "\n"
        "[Desktop Action remove]\n"
        "Name=Remove from the applications menu\n"
        "Name[ru]=Убрать из меню приложений\n"
        f"Exec={run} {REMOVE_MENU_FLAG}\n")


def _refresh_menus(folder):
    if shutil.which("update-desktop-database"):
        try:
            subprocess.run(["update-desktop-database", str(folder)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass


def install_menu_entry(exec_path):
    """Adds the app to the applications menu (only for the current user)."""
    desktop, icon = menu_paths()
    src = resource_path("icon.png")
    icon.parent.mkdir(parents=True, exist_ok=True)
    if src:
        shutil.copyfile(src, icon)
    desktop.parent.mkdir(parents=True, exist_ok=True)
    tmp = desktop.with_name(f".{desktop.name}.tmp")
    tmp.write_text(desktop_entry(exec_path, icon), encoding="utf-8")
    tmp.chmod(0o755)
    tmp.replace(desktop)
    _refresh_menus(desktop.parent)
    return desktop


def menu_entry_target():
    """The AppImage the menu entry starts (None if there's no such entry)."""
    desktop, _ = menu_paths()
    try:
        for line in desktop.read_text(encoding="utf-8").splitlines():
            if line.startswith(APPIMAGE_KEY + "="):
                return line.split("=", 1)[1]
    except OSError:
        pass
    return None


def remove_menu_entry():
    desktop, icon = menu_paths()
    for f in (desktop, icon):
        try:
            f.unlink()
        except OSError:
            pass
    _refresh_menus(desktop.parent)


def sync_menu_entry(appimage=None):
    """If the AppImage was moved or renamed, point the menu entry at it."""
    appimage = appimage or APPIMAGE
    target = menu_entry_target()
    if appimage and target and target != appimage:
        install_menu_entry(appimage)
        return True
    return False


def run_builtin_gallery_dl(args):
    """
    Entry point for `ArtDownloader.exe --gallery-dl …`: the windowed .exe
    has no console, so attach the pipes the parent gave us, then hand over
    to gallery-dl.
    """
    if WINDOWS:
        import ctypes
        import io
        import msvcrt
        kernel32 = ctypes.windll.kernel32
        for name, handle_id in (("stdout", -11), ("stderr", -12)):
            if getattr(sys, name) is not None:
                continue
            stream = None
            handle = kernel32.GetStdHandle(handle_id)
            if handle not in (0, -1, None):
                try:
                    fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
                    stream = io.TextIOWrapper(
                        io.FileIO(fd, "w", closefd=False),
                        encoding="utf-8", errors="replace", line_buffering=True)
                except OSError:
                    stream = None
            setattr(sys, name, stream or open(os.devnull, "w"))
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    restore_host_env()          # so the OAuth step can open the browser
    from gallery_dl import main as gallery_dl_main
    sys.argv = ["gallery-dl"] + list(args)
    sys.exit(gallery_dl_main())


def is_deviantart(url):
    u = url.lower()
    return "deviantart.com" in u or "fav.me" in u


def classify_url(url):
    """
    'expand' — a DeviantArt folder/gallery/profile link: first collect the
               list of works inside (and their order);
    'direct' — anything else (a single DA work or another site).
    """
    if not is_deviantart(url):
        return "direct"
    u = url.lower()
    if "/art/" in u or "/view/" in u or "fav.me" in u or "/journal/" in u:
        return "direct"
    return "expand"


def deviation_id(url):
    """DeviantArt work number from a direct link (or None)."""
    if not is_deviantart(url):
        return None
    u = url.split("?")[0].split("#")[0].rstrip("/")
    m = re.search(r"/view/(\d+)$", u) or re.search(r"/art/(?:.*-)?(\d+)$", u)
    return m.group(1) if m else None


def parse_urls(text):
    """Links from text: one per line, no duplicates, order kept."""
    seen, result = set(), []
    for line in text.splitlines():
        for part in line.split():
            if part.startswith(("http://", "https://")) and part not in seen:
                seen.add(part)
                result.append(part)
    return result


# Names Windows refuses for files and folders (also with an extension)
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL",
                    *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}

# Downloads with these extensions could run code if opened — warn about them
RISKY_EXTENSIONS = {
    ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".pif", ".cpl", ".dll",
    ".js", ".jse", ".vbs", ".vbe", ".wsf", ".wsh", ".hta", ".ps1", ".psm1",
    ".lnk", ".url", ".reg", ".jar", ".sh", ".bash", ".run", ".desktop",
    ".appimage", ".deb", ".rpm", ".py", ".pyw", ".apk", ".dmg", ".pkg",
}


def safe_name(name, windows=WINDOWS):
    """
    Folder name from site data, safe to use as ONE path component: no
    separators, no "..", nothing the file system forbids. Windows-reserved
    names get a "_" so the folder also works if moved to Windows later.
    """
    bad = r'[<>:"/\\|?*\x00-\x1f]' if windows else r'[/\\\x00-\x1f]'
    name = re.sub(bad, "-", name or "").strip(" .")[:120].strip(" .")
    if name.split(".")[0].upper() in WINDOWS_RESERVED or name == "_Export":
        name += "_"
    return name or tr("Без названия", "Untitled")


def is_safe_component(name, windows=WINDOWS):
    """True if `name` is a single, harmless folder name inside a folder."""
    # on Windows "C:x" would mean "x on drive C:", so ":" is never allowed
    bad = r'[/\\:\x00]' if windows else r'[/\\\x00]'
    return (isinstance(name, str) and name not in ("", ".", "..")
            and not re.search(bad, name) and name == name.strip())


def inside(base, path):
    """True if `path` stays inside `base` (after resolving .. and links)."""
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except ValueError:
        return False


def unique_path(path):
    """`path`, or "name (2).ext", "name (3).ext", … if it already exists."""
    path = Path(path)
    n = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


def group_name(folder_title, url):
    """Folder name: as on the site, otherwise derived from the link."""
    if folder_title and folder_title != "None":
        return safe_name(folder_title)
    tail = url.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
    if tail.isdigit() or tail in ("all", "gallery", ""):
        tail = url.split("deviantart.com/")[-1].split("/")[0]
    return safe_name(tail.replace("---", " - ").replace("-", " "))


def parse_collect_line(line):
    """'@@index<TAB>time<TAB>folder<TAB>title' → tuple or None."""
    m = COLLECT_RE.match(line.strip("\r\n"))
    if not m:
        return None
    try:
        published = int(m.group(2))
    except ValueError:
        published = 0
    return m.group(1), published, m.group(3), m.group(4)


def natural_key(s):
    """Human sorting: Part 2 before Part 10."""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", s or "")]


def clean_title(title):
    """Title without doubled or leading/trailing spaces."""
    return re.sub(r"\s+", " ", title or "").strip()


def title_number(title):
    """The last number in a title ("A Special Tea - 19" → 19) or None."""
    numbers = re.findall(r"\d+", title or "")
    return int(numbers[-1]) if numbers else None


def title_numbers(title):
    """
    All numbers in a title, in order: "Chapter 2 - Part 4" → (2, 4),
    "Chapter 2 Cover" → (2,), "A Special Tea - 19" → (19,). Comparing these
    tuples puts a cover before its pages and chapter 2 before chapter 10,
    whatever the punctuation.
    """
    return tuple(int(n) for n in re.findall(r"\d+", title or ""))


def resolve_mode(mode, titles):
    """
    'auto' becomes 'numbers' when nearly every title carries numbers that
    tell the works apart (comics: "Part 19", "Chapter 3 - Page 2"),
    otherwise 'date'.
    """
    if mode != "auto":
        return mode
    titles = [t for t in titles if t]
    if len(titles) < 2:
        return "date"
    numbered = [n for n in map(title_numbers, titles) if n]
    if (len(numbered) >= 0.8 * len(titles)
            and len(set(numbered)) >= 0.8 * len(numbered)):
        return "numbers"
    return "date"


def order_meta(value):
    """
    Order entry as (group, pos, published, title, seq, specific); older
    files have fewer fields. `specific` is False when the folder name had
    to be guessed from the link (e.g. the author's whole gallery).
    """
    group, pos = value[0], value[1]
    published = value[2] if len(value) > 2 else 0
    title = value[3] if len(value) > 3 else None
    seq = value[4] if len(value) > 4 else pos
    specific = bool(value[5]) if len(value) > 5 else True
    return group, pos, published, title, seq, specific


def build_order(entries, mode, existing=None):
    """
    entries:  [(index, published, group, title[, specific]), …] in the
              order the site returned them (gallery order).
    mode:     one of ORDER_KEYS.
    existing: previous result — works not in `entries` are kept and ranked
              together with the new ones.
    A work found in several links goes to the most specific folder (a
    chapter folder rather than the author's whole gallery).
    Returns {index: [group, pos, published, title, seq, specific]} with
    positions 1, 2, 3, … in each group, in the requested order.
    """
    items = {}
    for idx, value in (existing or {}).items():
        items[idx] = list(order_meta(value))
    taken = {}                       # idx -> specific flag chosen this run
    for seq, entry in enumerate(entries):
        idx, published, group, title = entry[:4]
        specific = bool(entry[4]) if len(entry) > 4 else True
        if idx in taken and (taken[idx] or not specific):
            continue                 # keep the first, unless this is better
        old = items.get(idx)
        if idx not in taken and old and old[5] and not specific:
            group = old[0]           # don't pull it back to a vaguer folder
            specific = True
        taken[idx] = specific
        items[idx] = [group, old[1] if old and old[0] == group else 10**9,
                      published or 0,
                      clean_title(title) if title else (old[3] if old else None),
                      seq, specific]

    groups = {}
    for idx, value in items.items():
        groups.setdefault(value[0], []).append((idx, *value[1:]))

    order = {}
    for group, members in groups.items():
        mode_here = resolve_mode(mode, [m[3] for m in members])
        if mode_here == "date":
            members.sort(key=lambda m: (m[2] or 0, m[4], m[1]))
        elif mode_here == "numbers":
            members.sort(key=lambda m: (title_numbers(m[3]), m[2] or 0, m[4], m[1]))
        elif mode_here == "name":
            members.sort(key=lambda m: (natural_key(m[3]), m[4], m[1]))
        else:  # gallery order
            members.sort(key=lambda m: (m[4], m[1]))
        for pos, (idx, _, published, title, seq, specific) in enumerate(members, 1):
            order[idx] = [group, pos, published, title, seq, specific]
    return order


DISK_NAME_RE = re.compile(r"^(\d+)(?:-(\d+))? - (.*?)(\.[^.]+)?$")


def group_width(order, group):
    top = max((v[1] for v in order.values() if v[0] == group), default=0)
    return max(3, len(str(top)))


def numbered_name(pos, width, num, title, ext):
    name = f"{pos:0{width}d}"
    if num > 1:
        name += f"-{num:02d}"
    return f"{name} - {clean_title(title)}{ext or ''}"


def scan_group(folder):
    """Numbered files of a gallery folder (and its Chapter NN subfolders)."""
    folder = Path(folder)
    found = []
    if not folder.is_dir():
        return found
    dirs = [folder] + [d for d in sorted(folder.iterdir())
                       if d.is_dir() and CHAPTER_DIR_RE.match(d.name)]
    for d in dirs:
        for f in sorted(d.iterdir()):
            m = DISK_NAME_RE.match(f.name) if f.is_file() else None
            if m:
                found.append((f, int(m.group(1)), int(m.group(2) or 1),
                              m.group(3), m.group(4) or ""))
    return found


def titles_from_disk(dest, order):
    """Fills in titles missing from older .order.json files."""
    order = {k: list(order_meta(v)) for k, v in order.items()}
    by_group = {}
    for idx, v in order.items():
        if not v[3]:
            by_group.setdefault(v[0], {})[v[1]] = idx
    for group, by_pos in by_group.items():
        if not is_safe_component(group):
            continue
        for _path, pos, num, title, _ext in scan_group(Path(dest) / group):
            if pos in by_pos and num == 1:
                order[by_pos[pos]][3] = clean_title(title)
    return order


def renumber(dest, old, new):
    """
    Renames files already on disk from their old numbers to the new ones,
    and moves them if their work now belongs to another folder (two passes,
    so no name ever collides). Also tidies spacing in names.
    Returns how many files were renamed or moved.
    """
    dest = Path(dest)
    safe = lambda g: is_safe_component(g) and inside(dest, dest / g)
    on_disk = {}
    for group in {v[0] for v in old.values()}:
        if safe(group):
            for path, pos, num, title, ext in scan_group(dest / group):
                on_disk.setdefault((group, pos), []).append(
                    (path, num, title, ext))
    moves, emptied = [], set()
    for idx, value in new.items():
        if idx not in old:
            continue
        old_group, old_pos = old[idx][0], old[idx][1]
        group, pos = value[0], value[1]
        if not safe(group):
            continue
        width = group_width(new, group)
        for path, num, title, ext in on_disk.get((old_group, old_pos), ()):
            name = numbered_name(pos, width, num, title, ext)
            if group == old_group:
                target = path.with_name(name)     # stays (also in Chapter NN)
            else:
                target = dest / group / name
                emptied.add(path.parent)
            if target != path:
                moves.append((path, target))
    temps = []
    for i, (path, target) in enumerate(moves):
        tmp = path.with_name(f".renumber-{i}-{path.name}")
        path.rename(tmp)
        temps.append((tmp, target))
    for tmp, target in temps:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.rename(unique_path(target))   # never overwrite another file
    for folder in sorted(emptied, key=lambda p: -len(p.parts)):
        for d in (folder, folder.parent):  # Chapter NN, then the group folder
            try:
                if d != dest and inside(dest, d) and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass
    return len(moves)


def reorder(dest, mode):
    """Re-sorts what's already downloaded, without the internet."""
    old = load_order(dest)
    if not old:
        return 0
    filled = titles_from_disk(dest, old)
    new = build_order([], mode, filled)
    count = renumber(dest, old, new)
    save_order(dest, new)
    return count


def load_order(dest):
    """
    Reads .order.json. Entries that don't look right (a folder name that
    could point outside the download folder, a bad number) are dropped,
    so a damaged or tampered file can't make the app write elsewhere.
    """
    try:
        data = json.loads((Path(dest) / ORDER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    clean = {}
    for idx, value in data.items():
        if (isinstance(idx, str) and idx
                and isinstance(value, list) and len(value) >= 2
                and is_safe_component(value[0])
                and isinstance(value[1], int) and 0 < value[1] < 10**7):
            clean[idx] = value
    return clean


def save_order(dest, order):
    (Path(dest) / ORDER_FILE).write_text(
        json.dumps(order, ensure_ascii=False, indent=1), encoding="utf-8")


def organize(dest, order, risky=None):
    """
    Moves downloads out of .incoming: DeviantArt files go to their gallery
    folder as "NNN - Title", everything else to the main folder. Never
    overwrites a different file and never writes outside `dest`.
    Names of files that could run code are appended to `risky`.
    Returns {folder: number of files}.
    """
    dest = Path(dest)
    inc = dest / INCOMING
    if not inc.is_dir():
        return {}
    moved = {}
    for f in sorted(inc.iterdir()):
        if not f.is_file() or f.suffix == ".part" or f.name.startswith("."):
            continue
        m = DA_FILE_RE.match(f.name)
        if (m and m.group(1) in order
                and is_safe_component(order[m.group(1)][0])):
            group, pos = order[m.group(1)][0], order[m.group(1)][1]
            num, title, ext = int(m.group(2)), m.group(3), m.group(4) or ""
            name = numbered_name(pos, group_width(order, group), num, title, ext)
            target_dir = dest / group
        elif m:      # DeviantArt, but order unknown
            num, title, ext = int(m.group(2)), clean_title(m.group(3)), m.group(4) or ""
            name = title + (f" ({num})" if num > 1 else "") + ext
            group, target_dir = ".", dest
        else:        # other sites — keep as is
            name, group, target_dir = f.name, ".", dest
        if not inside(dest, target_dir / name):
            group, target_dir, name = ".", dest, Path(f.name).name
        chapter = chapter_folder(name)
        if group != "." and chapter and (target_dir / chapter).is_dir():
            target_dir = target_dir / chapter  # folder already split
        target = place(f, target_dir / name)   # drops exact duplicates
        if target is None:
            continue
        moved[group] = moved.get(group, 0) + 1
        if risky is not None and target.suffix.lower() in RISKY_EXTENSIONS:
            risky.append(str(target.relative_to(dest)))
    try:
        inc.rmdir()
    except OSError:
        pass
    return moved


def chapter_folder(filename):
    """Chapter folder name for a file, or None if the name has no chapter."""
    m = CHAPTER_RE.search(filename)
    if not m:
        return None
    return f"Chapter {int(m.group(1)):02d}"


def same_file(a, b):
    """Two files that are the same download (same size and same bytes)."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                x, y = fa.read(1 << 20), fb.read(1 << 20)
                if x != y:
                    return False
                if not x:
                    return True
    except OSError:
        return False


def place(src, target):
    """
    Moves `src` to `target`. If an identical file is already there, the
    extra copy is removed; a different file is never overwritten.
    Returns the final path, or None if `src` was a duplicate.
    """
    if target.exists():
        if same_file(src, target):
            src.unlink()
            return None
        target = unique_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    src.replace(target)
    return target


def sort_into_chapters(folder):
    """
    If one folder holds several chapters with several files each, move them
    into Chapter NN. A folder where every "chapter" is a single file (e.g.
    height charts per chapter) stays as it is. Files without a chapter in
    the name stay put. Nothing is deleted except exact duplicates.
    """
    folder = Path(folder)
    files = [f for f in sorted(folder.iterdir())
             if f.is_file() and not f.name.startswith(".")
             and not (f.name.startswith("links") and f.suffix == ".txt")]
    chapters = {f: chapter_folder(f.name) for f in files}
    counts = {}
    for c in chapters.values():
        if c:
            counts[c] = counts.get(c, 0) + 1
    for d in folder.iterdir():
        if d.is_dir() and CHAPTER_DIR_RE.match(d.name):
            counts[d.name] = counts.get(d.name, 0) + sum(
                1 for f in d.iterdir() if f.is_file())
    if len(counts) < 2 or sum(counts.values()) < 2 * len(counts):
        return {}
    moved = {}
    for f, ch in chapters.items():
        if not ch:
            continue
        if place(f, folder / ch / f.name):
            moved[ch] = moved.get(ch, 0) + 1
    return moved


def sort_all_chapters(dest):
    """Chapter split in the main folder and in every gallery folder."""
    dest = Path(dest)
    result = {}
    folders = [dest] + [d for d in sorted(dest.iterdir())
                        if d.is_dir() and not d.name.startswith(".")
                        and d.name != "_Export"
                        and not CHAPTER_DIR_RE.match(d.name)]
    for folder in folders:
        for ch, n in sort_into_chapters(folder).items():
            key = ch if folder == dest else f"{folder.name}/{ch}"
            result[key] = n
    return result


def split_round_robin(items, n):
    """Splits a list into n parts in turn (1st link → 1st thread, …)."""
    n = max(1, min(n, len(items)))
    return [items[i::n] for i in range(n)]


def retry_links(failed_names, all_links):
    """
    Which links to retry. DeviantArt file names contain the work number, so
    only those are retried. Otherwise the whole list (finished files are
    skipped thanks to the archive).
    """
    ids = []
    for name in failed_names:
        m = DA_FILE_RE.match(os.path.basename(name.strip()))
        if not m:
            return list(all_links)
        ids.append(m.group(1))
    return [f"https://www.deviantart.com/view/{i}" for i in dict.fromkeys(ids)]


def make_run_config(username=None, password=None):
    """
    Temporary gallery-dl config: DeviantArt file names with the work number
    (for numbering) and, if given, login/password. Mode 600, deleted after
    the run — the password is never kept.
    """
    da = {"filename": DA_FILENAME}
    if username:
        da.update(username=username, password=password)
    fd, path = tempfile.mkstemp(prefix="artdl-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({
            "extractor": {"deviantart": da},
            # UTF-8 output so non-Latin titles survive on Windows too
            "output": {"stdout": "utf-8", "stderr": "utf-8"},
        }, f)
    os.chmod(path, 0o600)
    return path


def base_args(browser, config_path):
    args = ["-c", config_path]
    if browser and browser != "none":
        args += ["--cookies-from-browser", browser]
    return args


def pretty_name(path):
    """Downloaded file name for the log, without the internal prefix."""
    name = os.path.basename(path.strip())
    return re.sub(r"^da\d+_\d+_", "", name)


# --------------------------------------------------------------------------
# Notifications, export to CBZ/PDF, updates
# --------------------------------------------------------------------------

def notify_command(title, body, platform=None, icon=None):
    """
    How to show a desktop notification on this system:
    (argv, extra environment) or None if there is no way.
    Text goes through the environment on Windows so it can't break the script.
    """
    platform = platform or ("windows" if WINDOWS else
                            "mac" if sys.platform == "darwin" else "linux")
    if platform == "linux":
        if not shutil.which("notify-send"):
            return None
        argv = ["notify-send", "--app-name=" + APP_TITLE]
        if icon:
            argv.append(f"--icon={icon}")
        return argv + ["--", title, body], {}
    if platform == "mac":
        quote = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return ["osascript", "-e",
                f"display notification {quote(body)} with title {quote(title)}"], {}
    if platform == "windows":
        return (["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", WINDOWS_TOAST],
                {"ARTDL_TITLE": title, "ARTDL_BODY": body})
    return None


WINDOWS_TOAST = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$t = [Security.SecurityElement]::Escape($env:ARTDL_TITLE)
$b = [Security.SecurityElement]::Escape($env:ARTDL_BODY)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual></toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($toast)
"""


def send_notification(title, body):
    """Shows a desktop notification; never raises."""
    icon = resource_path("icon.png")
    cmd = notify_command(title, body, icon=str(icon) if icon else None)
    if not cmd:
        return False
    argv, extra = cmd
    try:
        subprocess.Popen(argv, env=dict(os.environ, **extra),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=CREATE_NO_WINDOW if WINDOWS else 0)
        return True
    except OSError:
        return False


def flash_taskbar(root):
    """Makes the taskbar button blink on Windows until the window is focused."""
    if not WINDOWS:
        return
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                        ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                        ("dwTimeout", wintypes.DWORD)]

        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), int(root.wm_frame(), 16),
                          0x0F, 0, 0)    # FLASHW_ALL | FLASHW_TIMERNOFG
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


EXPORT_DIR = "_Export"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def export_plan(dest):
    """
    Books to make from a download folder: one per Chapter NN folder, or one
    per gallery folder when it isn't split. Pages in file-name order, which
    is the reading order. Returns [(book name, [page paths]), …].
    """
    dest = Path(dest)
    images = lambda d: sorted((p for p in d.iterdir()
                               if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
                              key=lambda p: natural_key(p.name))
    books = []
    if not dest.is_dir():
        return books
    for group in sorted((d for d in dest.iterdir() if d.is_dir()),
                        key=lambda d: natural_key(d.name)):
        if group.name.startswith(".") or group.name == EXPORT_DIR:
            continue
        chapters = sorted((d for d in group.iterdir()
                           if d.is_dir() and CHAPTER_DIR_RE.match(d.name)),
                          key=lambda d: natural_key(d.name))
        loose = images(group)
        if loose:
            books.append((group.name, loose))
        for ch in chapters:
            pages = images(ch)
            if pages:
                books.append((f"{group.name} - {ch.name}", pages))
    return books


def _xml(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def write_cbz(path, pages, series, title):
    """Comic book archive: the original images, no quality loss."""
    import zipfile
    width = max(3, len(str(len(pages))))
    info = ('<?xml version="1.0" encoding="utf-8"?>\n<ComicInfo>'
            f"<Series>{_xml(series)}</Series><Title>{_xml(title)}</Title>"
            f"<PageCount>{len(pages)}</PageCount></ComicInfo>\n")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for i, page in enumerate(pages, 1):
            z.write(page, f"{i:0{width}d}{page.suffix.lower()}")
        z.writestr("ComicInfo.xml", info)


def _jpeg_page(page):
    """(jpeg bytes, width, height, colour space) for one page."""
    import io
    from PIL import Image
    with Image.open(page) as im:
        if im.format == "JPEG" and im.mode in ("RGB", "L"):
            data = Path(page).read_bytes()          # embed as is, no loss
            return data, im.width, im.height, "DeviceGray" if im.mode == "L" else "DeviceRGB"
        im.seek(0)
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, (255, 255, 255))
            rgb.paste(rgba, mask=rgba.getchannel("A"))
        else:
            rgb = im.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=92, optimize=True)
        return buf.getvalue(), rgb.width, rgb.height, "DeviceRGB"


def write_pdf(path, pages, title):
    """
    PDF with one image per page. Pages are processed one at a time, so even
    huge chapters need little memory; JPEG pages are copied without loss.
    """
    offsets, n_pages = [], len(pages)
    page_ids = [3 + 3 * i for i in range(n_pages)]
    with open(path, "wb") as f:
        def obj(num, body, stream=None):
            offsets.append((num, f.tell()))
            f.write(f"{num} 0 obj\n".encode())
            f.write(body.encode("latin-1") if isinstance(body, str) else body)
            if stream is not None:
                f.write(b"\nstream\n" + stream + b"\nendstream")
            f.write(b"\nendobj\n")

        f.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        obj(1, "<< /Type /Catalog /Pages 2 0 R >>")
        kids = " ".join(f"{p} 0 R" for p in page_ids)
        obj(2, f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>")
        for i, page in enumerate(pages):
            data, w, h, space = _jpeg_page(page)
            pw, ph = w * 0.75, h * 0.75                 # 96 dpi → points
            pid, img, content = page_ids[i], page_ids[i] + 1, page_ids[i] + 2
            obj(pid, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw:.2f} {ph:.2f}] "
                     f"/Resources << /XObject << /Im0 {img} 0 R >> >> "
                     f"/Contents {content} 0 R >>")
            obj(img, f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                     f"/ColorSpace /{space} /BitsPerComponent 8 "
                     f"/Filter /DCTDecode /Length {len(data)} >>", data)
            draw = f"q {pw:.2f} 0 0 {ph:.2f} 0 0 cm /Im0 Do Q".encode()
            obj(content, f"<< /Length {len(draw)} >>", draw)
        info_id = 3 + 3 * n_pages
        safe_title = "".join(c for c in title if c.isascii() and c.isprintable())
        safe_title = safe_title.replace("\\", "").replace("(", "").replace(")", "")
        obj(info_id, f"<< /Title ({safe_title}) /Producer ({APP_TITLE}) >>")
        xref = f.tell()
        total = info_id + 1
        table = {num: off for num, off in offsets}
        f.write(f"xref\n0 {total}\n0000000000 65535 f \n".encode())
        for num in range(1, total):
            f.write(f"{table[num]:010d} 00000 n \n".encode())
        f.write(f"trailer\n<< /Size {total} /Root 1 0 R /Info {info_id} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n".encode())


def export_books(dest, fmt, on_book=None):
    """
    Makes CBZ or PDF books from a download folder into dest/_Export.
    on_book(index, total, name) is called before each book.
    Returns the paths created.
    """
    dest = Path(dest)
    out_dir = dest / EXPORT_DIR
    plan = export_plan(dest)
    made = []
    for i, (name, pages) in enumerate(plan, 1):
        if on_book:
            on_book(i, len(plan), name)
        out_dir.mkdir(exist_ok=True)
        target = out_dir / f"{safe_name(name)}.{fmt}"
        tmp = target.with_name(f".{target.name}.tmp")
        series = name.split(" - Chapter ")[0]
        if fmt == "cbz":
            write_cbz(tmp, pages, series, name)
        else:
            write_pdf(tmp, pages, name)
        tmp.replace(target)          # replaces an older export of the same book
        made.append(target)
    return made


APP_VERSION = "1.0.0"                       # set from the release tag at build
REPO = "GreenClowns/Art-Downloader"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"


def parse_version(text):
    """'v1.32.18' → (1, 32, 18); anything odd → ()."""
    m = re.match(r"^\D*(\d+(?:\.\d+)*)", (text or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def is_newer(candidate, current):
    return parse_version(candidate) > parse_version(current)


def fetch_json(url, timeout=10):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": f"{APP_TITLE}/{APP_VERSION}",
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https URLs)
        return json.loads(r.read().decode("utf-8"))


def latest_gallery_dl():
    return fetch_json("https://pypi.org/pypi/gallery-dl/json")["info"]["version"]


def latest_app_release():
    """(version, page url) of the newest published release, or None."""
    try:
        data = fetch_json(f"https://api.github.com/repos/{REPO}/releases/latest")
    except Exception:
        return None
    return data.get("tag_name", ""), data.get("html_url") or RELEASES_URL


def gallery_dl_version(gdl):
    if not gdl:
        return None
    try:
        out = subprocess.run(list(gdl) + ["--version"], capture_output=True,
                             text=True, timeout=60, stdin=subprocess.DEVNULL,
                             env=gdl_env(),
                             creationflags=CREATE_NO_WINDOW if WINDOWS else 0)
        return out.stdout.strip().splitlines()[0] if out.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def update_method(gdl, frozen=None, in_venv=None, pipx=None):
    """
    How gallery-dl can be updated here:
      'app'    — it's built into the .exe: update the whole app
      'pip'    — the app's own Python environment (Linux install)
      'pipx'   — installed with pipx
      'manual' — somewhere else; tell the user
    """
    frozen = FROZEN if frozen is None else frozen
    in_venv = (sys.prefix != sys.base_prefix) if in_venv is None else in_venv
    pipx = shutil.which("pipx") if pipx is None else pipx
    if frozen:
        return "app"
    if gdl and gdl[:3] == [sys.executable, "-m", "gallery_dl"] and in_venv:
        return "pip"
    if gdl and pipx and Path(gdl[0]).name.startswith("gallery-dl") \
            and ".local" in Path(gdl[0]).parts:
        return "pipx"
    return "manual"


def update_command(method):
    if method == "pip":
        return [sys.executable, "-m", "pip", "install", "--upgrade", "gallery-dl"]
    if method == "pipx":
        return ["pipx", "upgrade", "gallery-dl"]
    return None

# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------

THEME_KEYS = ["system", "light", "dark"]
ACCENT = ("#16a34a", "#22c55e")            # green from the logo (light, dark)
ACCENT_HOVER = ("#15803d", "#16a34a")
DANGER = ("#dc2626", "#ef4444")
DANGER_HOVER = ("#b91c1c", "#dc2626")
MUTED = ("#6b7280", "#9ca3af")
CARD = ("#ffffff", "#1f2124")
WINDOW_BG = ("#f3f4f6", "#141517")
LOG_COLORS = {"ok": "#22a35a", "err": "#e5484d", "info": "#3b82f6",
              "skip": "#8a8f98"}


def theme_label(key):
    return {"system": tr("Как в системе", "System"),
            "light": tr("Светлая", "Light"),
            "dark": tr("Тёмная", "Dark")}[key]


class KeyedOption:
    """Option menu that shows translated labels but stores keys."""

    def __init__(self, parent, keys, label_fn, key, width=220):
        self.keys, self.label_fn, self.key = keys, label_fn, key
        self.widget = ctk.CTkOptionMenu(
            parent, width=width, values=[label_fn(k) for k in keys],
            command=self._changed, fg_color=WINDOW_BG, button_color=WINDOW_BG,
            button_hover_color=("#e5e7eb", "#2a2c30"),
            text_color=("#111827", "#f3f4f6"), dynamic_resizing=False)
        self.widget.set(label_fn(key))

    def _changed(self, label):
        labels = [self.label_fn(k) for k in self.keys]
        if label in labels:
            self.key = self.keys[labels.index(label)]

    def get(self):
        return self.key


class KeyedSegments:
    """Segmented button with translated labels that stores keys."""

    def __init__(self, parent, keys, label_fn, key, command=None):
        self.keys, self.label_fn, self.key = keys, label_fn, key
        self.command = command
        self.widget = ctk.CTkSegmentedButton(
            parent, values=[label_fn(k) for k in keys], command=self._changed,
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER)
        self.widget.set(label_fn(key))

    def _changed(self, label):
        labels = [self.label_fn(k) for k in self.keys]
        if label in labels:
            self.key = self.keys[labels.index(label)]
            if self.command:
                self.command(self.key)

    def get(self):
        return self.key


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("900x760")
        self.root.minsize(720, 620)
        self.root.configure(fg_color=WINDOW_BG)
        self.set_window_icon()

        self.procs = []
        self.procs_lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.events = queue.Queue()
        self.worker = None
        self.running = False
        self.stats = {"done": 0, "skip": 0, "err": 0}
        self.failed = []
        self.counting_skips = True
        self.total_works = 0
        self.works_started = 0
        self.seen_warnings = set()

        cfg = migrate_config(self.load_config())
        set_lang(cfg.get("lang") or detect_lang())
        self.browser_key = cfg["browser"]
        self.order_key = cfg["order"]
        self.theme_key = cfg.get("theme") if cfg.get("theme") in THEME_KEYS \
            else "system"
        ctk.set_appearance_mode(self.theme_key)
        self.var_base = tk.StringVar(value=cfg.get("base", str(DEFAULT_BASE)))
        self.var_name = tk.StringVar(value="")
        self.var_user = tk.StringVar(value=cfg.get("username", ""))
        self.var_pass = tk.StringVar(value="")
        self.var_threads = tk.StringVar(value=str(cfg.get("threads", 3)))
        self.var_sort = tk.BooleanVar(value=cfg.get("sort", True))
        self.var_retry = tk.BooleanVar(value=cfg.get("retry", True))
        self.var_notify = tk.BooleanVar(value=cfg.get("notify", True))
        self.var_autoupdate = tk.BooleanVar(value=cfg.get("auto_update", True))
        self.last_update_check = float(cfg.get("last_update_check", 0) or 0)
        self.update_action = None          # what the update button does
        self.var_versions = tk.StringVar(value=f"{APP_TITLE} {APP_VERSION}")
        self.var_update = tk.StringVar(value="")
        self.menu_asked = bool(cfg.get("appimage_menu_asked", False))
        self.var_menu = tk.StringVar()
        self.da_connected = bool(cfg.get("da_connected", False))
        self.var_connected = tk.StringVar()
        self.var_status = tk.StringVar()
        self.var_stats = tk.StringVar(value="")
        self.var_dest = tk.StringVar()
        self.var_base.trace_add("write", lambda *_: self.update_dest())
        self.var_name.trace_add("write", lambda *_: self.update_dest())

        self.build_ui()
        self.refresh_texts()
        self.check_gallery_dl()
        self.root.after(100, self.poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(2500, self.auto_check_updates)
        if APPIMAGE:
            try:
                sync_menu_entry()
            except OSError:
                pass
            self.root.after(1200, self.offer_menu_entry)

    # ---------- interface ----------

    def set_window_icon(self):
        if WINDOWS:
            ico = resource_path("icon.ico")
            if ico:   # CustomTkinter sets its own icon first; replace it after
                self.root.after(300, lambda: self.root.iconbitmap(str(ico)))
            return
        icon = resource_path("icon.png")
        if not icon:
            return
        try:
            self._icon = tk.PhotoImage(file=str(icon))
            self.root.iconphoto(True, self._icon)
        except tk.TclError:
            pass

    def font(self, size=13, weight="normal", mono=False):
        if mono:
            return ctk.CTkFont(family=MONO_FONT, size=size)
        return ctk.CTkFont(size=size, weight=weight)

    def card(self, parent, **pack):
        frame = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12)
        frame.pack(fill="x", **({"pady": (0, 12)} | pack))
        return frame

    def heading(self, parent, text, hint=None, wrap=760):
        ctk.CTkLabel(parent, text=text, font=self.font(15, "bold"),
                     anchor="w").pack(fill="x", padx=16, pady=(14, 0))
        if hint:
            ctk.CTkLabel(parent, text=hint, font=self.font(12),
                         text_color=MUTED, anchor="w", justify="left",
                         wraplength=wrap).pack(fill="x", padx=16, pady=(2, 0))

    def build_ui(self):
        # forget widgets of a previous build (language change rebuilds all)
        self.btn_connect = self.btn_da_status = self.cmb_lang = None
        self.btn_export = self.btn_check = self.btn_update = None
        self.btn_menu = None
        # Header: logo, title, DeviantArt status
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(16, 4))
        logo = resource_path("icon.png")
        if logo:
            try:
                from PIL import Image
                self._logo = ctk.CTkImage(Image.open(logo), size=(36, 36))
                ctk.CTkLabel(header, image=self._logo, text="").pack(side="left")
            except Exception:
                pass
        ctk.CTkLabel(header, text=APP_TITLE, font=self.font(22, "bold")).pack(
            side="left", padx=(10, 0))
        self.btn_da_status = ctk.CTkButton(
            header, text="", height=30, corner_radius=15,
            command=self.connect_deviantart, font=self.font(12, "bold"))
        self.btn_da_status.pack(side="right")

        self.tabs = ctk.CTkTabview(
            self.root, fg_color="transparent", corner_radius=12,
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOVER)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.tab_main_name = tr("Загрузка", "Download")
        self.tab_settings_name = tr("Настройки", "Settings")
        self.build_main_tab(self.tabs.add(self.tab_main_name))
        self.build_settings_tab(self.tabs.add(self.tab_settings_name))
        self.update_dest()
        self.update_connected()
        self.set_running(self.running)

    def build_main_tab(self, tab):
        # Links
        links = self.card(tab)
        self.heading(links, tr("Ссылки", "Links"), tr(
            "По одной на строку: папка, глава, вся серия, галерея, профиль или "
            "отдельная работа. Другие сайты тоже можно.",
            "One per line: a folder, chapter, whole series, gallery, profile or "
            "single work. Other sites work too."))
        self.txt_links = ctk.CTkTextbox(links, height=110, wrap="none",
                                        font=self.font(13, mono=True),
                                        fg_color=WINDOW_BG, corner_radius=8)
        self.txt_links.pack(fill="x", padx=16, pady=(8, 16))

        # Where to save
        dest = self.card(tab)
        self.heading(dest, tr("Куда сохранить", "Save to"))
        row = ctk.CTkFrame(dest, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(8, 0))
        row.columnconfigure(0, weight=2)
        row.columnconfigure(1, weight=3)
        for col, text in ((0, tr("Название работы", "Project name")),
                          (1, tr("Основная папка", "Main folder"))):
            ctk.CTkLabel(row, text=text, font=self.font(12), text_color=MUTED,
                         anchor="w").grid(row=0, column=col, sticky="w")
        ctk.CTkEntry(row, textvariable=self.var_name, height=36).grid(
            row=1, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkEntry(row, textvariable=self.var_base, height=36).grid(
            row=1, column=1, sticky="ew", padx=(0, 8))
        ctk.CTkButton(row, text=tr("Выбрать…", "Browse…"), width=100,
                      height=36, fg_color=WINDOW_BG,
                      text_color=("#111827", "#f3f4f6"),
                      hover_color=("#e5e7eb", "#2a2c30"),
                      command=self.choose_base).grid(row=1, column=2)
        ctk.CTkLabel(dest, textvariable=self.var_dest, font=self.font(12),
                     text_color=MUTED, anchor="w").pack(
            fill="x", padx=16, pady=(6, 14))

        # Big button + progress
        self.btn_start = ctk.CTkButton(
            tab, text="", height=50, corner_radius=12,
            font=self.font(17, "bold"), command=self.toggle_start)
        self.btn_start.pack(fill="x", pady=(2, 10))
        self.progress = ctk.CTkProgressBar(tab, mode="indeterminate", height=6,
                                           progress_color=ACCENT,
                                           fg_color=("#e5e7eb", "#2a2c30"))
        self.progress.pack(fill="x", padx=2)
        self.progress.set(0)

        status = ctk.CTkFrame(tab, fg_color="transparent")
        status.pack(fill="x", pady=(8, 6))
        ctk.CTkLabel(status, textvariable=self.var_status,
                     font=self.font(13, "bold")).pack(side="left")
        ctk.CTkLabel(status, textvariable=self.var_stats, font=self.font(12),
                     text_color=MUTED).pack(side="left", padx=(12, 0))
        small = {"height": 28, "fg_color": "transparent", "border_width": 1,
                 "border_color": ("#d1d5db", "#3a3d42"),
                 "text_color": ("#111827", "#f3f4f6"),
                 "hover_color": ("#e5e7eb", "#2a2c30"),
                 "font": self.font(12)}
        ctk.CTkButton(status, text=tr("Открыть папку", "Open folder"),
                      width=120, command=self.open_folder, **small).pack(
            side="right")
        ctk.CTkButton(status, text=tr("Упорядочить файлы", "Organize files"),
                      width=140, command=self.organize_now, **small).pack(
            side="right", padx=(0, 8))
        self.btn_export = ctk.CTkButton(
            status, text=tr("В CBZ / PDF…", "To CBZ / PDF…"), width=110,
            command=self.open_export, **small)
        self.btn_export.pack(side="right", padx=(0, 8))

        # Log
        self.txt_log = ctk.CTkTextbox(tab, height=150, wrap="none",
                                      font=self.font(12, mono=True),
                                      fg_color=CARD, corner_radius=12,
                                      state="disabled")
        self.txt_log.pack(fill="both", expand=True)
        for tag, color in LOG_COLORS.items():
            self.txt_log.tag_config(tag, foreground=color)

    def build_settings_tab(self, tab):
        scroll = ctk.CTkScrollableFrame(tab, fg_color=WINDOW_BG)
        scroll.pack(fill="both", expand=True)

        # DeviantArt
        da = self.card(scroll)
        self.heading(da, "DeviantArt", None)
        ctk.CTkLabel(da, textvariable=self.var_connected, font=self.font(12),
                     text_color=MUTED, anchor="w", justify="left",
                     wraplength=760).pack(fill="x", padx=16, pady=(2, 8))
        self.btn_connect = ctk.CTkButton(
            da, text=tr("Подключить DeviantArt", "Connect DeviantArt"),
            height=36, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=self.connect_deviantart)
        self.btn_connect.pack(anchor="w", padx=16, pady=(0, 10))
        ctk.CTkLabel(da, text=tr("Другие способы входа (необязательно)",
                                 "Other ways to sign in (optional)"),
                     font=self.font(12, "bold"), anchor="w").pack(
            fill="x", padx=16, pady=(4, 4))
        grid = ctk.CTkFrame(da, fg_color="transparent")
        grid.pack(fill="x", padx=16, pady=(0, 14))
        grid.columnconfigure((0, 1), weight=1)
        for col, text in ((0, tr("Логин", "Username")),
                          (1, tr("Пароль (не сохраняется)",
                                 "Password (not stored)"))):
            ctk.CTkLabel(grid, text=text, font=self.font(12), text_color=MUTED,
                         anchor="w").grid(row=0, column=col, sticky="w")
        ctk.CTkEntry(grid, textvariable=self.var_user, height=34).grid(
            row=1, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkEntry(grid, textvariable=self.var_pass, height=34, show="•").grid(
            row=1, column=1, sticky="ew")
        browser_row = ctk.CTkFrame(da, fg_color="transparent")
        browser_row.pack(fill="x", padx=16, pady=(0, 16))
        ctk.CTkLabel(browser_row, text=tr("Вход из браузера:",
                                          "Cookies from browser:"),
                     font=self.font(13)).pack(side="left")
        self.cmb_browser = KeyedOption(browser_row, BROWSER_KEYS, browser_label,
                                       self.browser_key)
        self.cmb_browser.widget.pack(side="left", padx=(10, 0))

        # Order and folders
        order = self.card(scroll)
        self.heading(order, tr("Порядок файлов", "File order"), tr(
            "Автоматически: по номеру в названии (Part 2 раньше Part 10), если он "
            "есть у работ, иначе по дате.",
            "Automatic: by the number in the title (Part 2 before Part 10) when "
            "works have one, otherwise by date."))
        self.cmb_order = KeyedOption(order, ORDER_KEYS, order_label,
                                     self.order_key)
        self.cmb_order.widget.pack(anchor="w", padx=16, pady=(8, 8))
        ctk.CTkSwitch(order, variable=self.var_sort, progress_color=ACCENT,
                      text=tr("Если в одной папке несколько глав — разнести по "
                              "Chapter 01, 02 …",
                              "If one folder has several chapters, split into "
                              "Chapter 01, 02 …")).pack(
            anchor="w", padx=16, pady=(0, 16))

        # Downloading
        dl = self.card(scroll)
        self.heading(dl, tr("Скачивание", "Downloading"))
        row = ctk.CTkFrame(dl, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(8, 8))
        ctk.CTkLabel(row, text=tr("Одновременно:", "At the same time:"),
                     font=self.font(13)).pack(side="left")
        ctk.CTkSegmentedButton(row, values=[str(i) for i in range(1, MAX_THREADS + 1)],
                               variable=self.var_threads,
                               selected_color=ACCENT,
                               selected_hover_color=ACCENT_HOVER).pack(
            side="left", padx=(10, 8))
        ctk.CTkLabel(row, text=tr("3 — оптимально", "3 is optimal"),
                     font=self.font(12), text_color=MUTED).pack(side="left")
        ctk.CTkSwitch(dl, variable=self.var_retry, progress_color=ACCENT,
                      text=tr("Повторять то, что не скачалось с первого раза",
                              "Retry anything that failed the first time")).pack(
            anchor="w", padx=16, pady=(0, 16))

        # Appearance
        look = self.card(scroll)
        self.heading(look, tr("Внешний вид", "Appearance"))
        row = ctk.CTkFrame(look, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(8, 8))
        ctk.CTkLabel(row, text=tr("Язык:", "Language:"), width=90, anchor="w",
                     font=self.font(13)).pack(side="left")
        self.cmb_lang = KeyedSegments(row, list(LANGUAGES), LANGUAGES.get,
                                      get_lang(), command=self.change_language)
        self.cmb_lang.widget.pack(side="left")
        row = ctk.CTkFrame(look, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 16))
        ctk.CTkLabel(row, text=tr("Тема:", "Theme:"), width=90, anchor="w",
                     font=self.font(13)).pack(side="left")
        self.seg_theme = KeyedSegments(row, THEME_KEYS, theme_label,
                                       self.theme_key, command=self.change_theme)
        self.seg_theme.widget.pack(side="left")

        # Linux AppImage: applications menu
        if APPIMAGE:
            menu = self.card(scroll)
            self.heading(menu, tr("Меню приложений", "Applications menu"))
            ctk.CTkLabel(menu, textvariable=self.var_menu, font=self.font(12),
                         text_color=MUTED, anchor="w", justify="left",
                         wraplength=760).pack(fill="x", padx=16, pady=(2, 8))
            self.btn_menu = ctk.CTkButton(
                menu, text="", height=34, fg_color=WINDOW_BG,
                text_color=("#111827", "#f3f4f6"),
                hover_color=("#e5e7eb", "#2a2c30"), command=self.toggle_menu_entry)
            self.btn_menu.pack(anchor="w", padx=16, pady=(0, 16))
            self.update_menu_card()

        # Notifications and updates
        upd = self.card(scroll)
        self.heading(upd, tr("Уведомления и обновления", "Notifications and updates"))
        ctk.CTkSwitch(upd, variable=self.var_notify, progress_color=ACCENT,
                      text=tr("Уведомлять, когда скачивание закончится (если окно "
                              "в фоне)",
                              "Notify me when a download finishes (if the window "
                              "is in the background)"),
                      command=self.save_config).pack(anchor="w", padx=16, pady=(8, 4))
        ctk.CTkSwitch(upd, variable=self.var_autoupdate, progress_color=ACCENT,
                      text=tr("Проверять обновления раз в день",
                              "Check for updates once a day"),
                      command=self.save_config).pack(anchor="w", padx=16, pady=(0, 10))
        ctk.CTkLabel(upd, textvariable=self.var_versions, font=self.font(12),
                     text_color=MUTED, anchor="w").pack(fill="x", padx=16)
        row = ctk.CTkFrame(upd, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 4))
        self.btn_check = ctk.CTkButton(
            row, text=tr("Проверить обновления", "Check for updates"), height=34,
            fg_color=WINDOW_BG, text_color=("#111827", "#f3f4f6"),
            hover_color=("#e5e7eb", "#2a2c30"), command=self.check_updates)
        self.btn_check.pack(side="left")
        self.btn_update = ctk.CTkButton(row, text="", height=34, fg_color=ACCENT,
                                        hover_color=ACCENT_HOVER,
                                        command=self.do_update)
        ctk.CTkLabel(upd, textvariable=self.var_update, font=self.font(12),
                     text_color=MUTED, anchor="w", justify="left",
                     wraplength=760).pack(fill="x", padx=16, pady=(0, 16))
        self.show_update_button()

    def update_dest(self):
        if hasattr(self, "var_dest"):
            self.var_dest.set(tr("Сохранится в: ", "Will be saved to: ")
                              + str(self.dest_folder()))

    def refresh_texts(self):
        """Texts that live in variables (status, counters, sign-in hint)."""
        self.update_connected()
        if self.stats != {"done": 0, "skip": 0, "err": 0}:
            self.update_stats()
        if not self.running:
            self.var_status.set(tr("Готово к работе", "Ready"))

    def change_theme(self, key):
        self.theme_key = key
        ctk.set_appearance_mode(key)
        self.save_config()

    def change_language(self, new=None):
        new = new or self.cmb_lang.get()
        if new == get_lang():
            return
        if self.running:
            messagebox.showinfo(APP_TITLE, tr(
                "Язык можно сменить, когда скачивание закончится.",
                "You can change the language after the download finishes."))
            self.cmb_lang.key = get_lang()
            self.cmb_lang.widget.set(LANGUAGES[get_lang()])
            return
        self.browser_key = self.cmb_browser.get()
        self.order_key = self.cmb_order.get()
        links = self.txt_links.get("1.0", "end-1c")
        log_dump = self.txt_log._textbox.dump("1.0", "end-1c", text=True, tag=True)
        set_lang(new)
        for w in self.root.winfo_children():
            w.destroy()
        self.build_ui()
        self.tabs.set(self.tab_settings_name)
        self.txt_links.insert("1.0", links)
        self.restore_log(log_dump)
        self.refresh_texts()
        self.save_config()

    def restore_log(self, dump):
        """Puts the old log back with its colors after rebuilding the UI."""
        self.txt_log.configure(state="normal")
        tags = []
        for kind, value, _index in dump:
            if kind == "tagon":
                tags.append(value)
            elif kind == "tagoff" and value in tags:
                tags.remove(value)
            elif kind == "text":
                self.txt_log.insert("end", value, tuple(tags))
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    # ---------- settings ----------

    def load_config(self):
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_config(self):
        """Saves settings. Never the password."""
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps({
                "lang": get_lang(),
                "theme": self.theme_key,
                "base": self.var_base.get(),
                "browser": self.cmb_browser.get(),
                "username": self.var_user.get().strip(),
                "threads": self.threads(),
                "order": self.cmb_order.get(),
                "settings_version": SETTINGS_VERSION,
                "retry": self.var_retry.get(),
                "sort": self.var_sort.get(),
                "da_connected": self.da_connected,
                "notify": self.var_notify.get(),
                "auto_update": self.var_autoupdate.get(),
                "last_update_check": self.last_update_check,
                "appimage_menu_asked": self.menu_asked,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ---------- helpers ----------

    def update_connected(self):
        if self.da_connected:
            self.var_connected.set(tr(
                "✔ Подключено — работы 18+ скачиваются в полном качестве.",
                "✔ Connected — 18+ works download in full quality."))
            chip = {"text": "● DeviantArt", "fg_color": ("#dcfce7", "#14532d"),
                    "hover_color": ("#bbf7d0", "#166534"),
                    "text_color": ("#166534", "#bbf7d0")}
        else:
            self.var_connected.set(tr(
                "Нужно один раз для работ 18+: откроется DeviantArt, нажми "
                "«Authorize». Пароль программа не хранит.",
                "Needed once for 18+ works: DeviantArt opens, click "
                "“Authorize”. The app never stores your password."))
            chip = {"text": tr("Подключить DeviantArt", "Connect DeviantArt"),
                    "fg_color": ACCENT, "hover_color": ACCENT_HOVER,
                    "text_color": "#ffffff"}
        if getattr(self, "btn_da_status", None) is not None:
            self.btn_da_status.configure(**chip)

    def threads(self):
        try:
            return max(1, min(MAX_THREADS, int(self.var_threads.get())))
        except (tk.TclError, ValueError):
            return 3

    def install_hint(self):
        cmd = "pip install gallery-dl" if WINDOWS else "pipx install gallery-dl"
        return tr(f"Установи его: {cmd}", f"Install it: {cmd}")

    def check_gallery_dl(self):
        self.gdl = find_gallery_dl()
        if self.gdl:
            gdl = self.gdl
            threading.Thread(target=lambda: self.emit(
                "gdl_version", gallery_dl_version(gdl)), daemon=True).start()
        if FROZEN:
            self.log(tr("gallery-dl встроен в программу",
                        "gallery-dl is built into the app"), "info")
        elif self.gdl:
            where = " ".join(self.gdl)
            self.log(tr(f"gallery-dl найден: {where}",
                        f"gallery-dl found: {where}"), "info")
        else:
            self.log(tr("gallery-dl не найден! ", "gallery-dl not found! ")
                     + self.install_hint(), "err")

    def dest_folder(self):
        base = Path(os.path.expanduser(self.var_base.get().strip()
                                       or str(DEFAULT_BASE)))
        name = self.var_name.get().strip().replace("/", "-")
        return base / name if name else base

    def choose_base(self):
        d = filedialog.askdirectory(initialdir=self.var_base.get() or str(Path.home()))
        if d:
            self.var_base.set(d)

    def open_folder(self):
        folder = self.dest_folder()
        folder.mkdir(parents=True, exist_ok=True)
        if WINDOWS:
            os.startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    def log(self, text, tag=None):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", text + "\n", tag)
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def update_stats(self):
        s = self.stats
        self.var_stats.set(tr(
            f"скачано {s['done']} · пропущено {s['skip']} · ошибок {s['err']}",
            f"{s['done']} downloaded · {s['skip']} skipped · {s['err']} errors"))

    def show_progress(self, fraction):
        """Real progress once gallery-dl reports "k of N"."""
        self.progress.stop()
        self.progress.configure(mode="determinate", progress_color=ACCENT)
        self.progress.set(fraction)

    def set_running(self, running):
        self.running = running
        if running:
            self.btn_start.configure(text="■  " + tr("Стоп", "Stop"),
                                     fg_color=DANGER, hover_color=DANGER_HOVER)
            self.progress.configure(mode="indeterminate", progress_color=ACCENT)
            self.progress.start()
        else:
            self.btn_start.configure(text="⬇  " + tr("Скачать", "Download"),
                                     fg_color=ACCENT, hover_color=ACCENT_HOVER)
            self.progress.stop()
            self.progress.configure(mode="determinate",
                                    progress_color=("#e5e7eb", "#2a2c30"))
            self.progress.set(0)
        state = "disabled" if running else "normal"
        for widget in (self.btn_connect, self.btn_da_status,
                       self.cmb_lang.widget if self.cmb_lang else None,
                       getattr(self, "btn_export", None),
                       getattr(self, "btn_check", None),
                       getattr(self, "btn_update", None)):
            if widget is not None:
                widget.configure(state=state)

    def toggle_start(self):
        if self.running:
            self.stop()
        else:
            self.start()

    # ---------- buttons ----------

    def start(self):
        if not self.gdl:
            messagebox.showerror(APP_TITLE, tr("gallery-dl не найден.\n",
                                               "gallery-dl not found.\n")
                                 + self.install_hint())
            return
        urls = parse_urls(self.txt_links.get("1.0", "end"))
        if not urls:
            messagebox.showinfo(APP_TITLE, tr("Вставь хотя бы одну ссылку.",
                                              "Paste at least one link."))
            return
        user, pwd = self.var_user.get().strip(), self.var_pass.get()
        if bool(user) != bool(pwd):
            messagebox.showinfo(APP_TITLE, tr(
                "Для входа нужны и логин, и пароль.\nИли оставь оба поля пустыми.",
                "Sign-in needs both username and password.\n"
                "Or leave both fields empty."))
            return
        dest = self.dest_folder()
        dest.mkdir(parents=True, exist_ok=True)
        self.save_config()

        self.stats = {"done": 0, "skip": 0, "err": 0}
        self.failed = []
        self.counting_skips = True
        self.total_works = 0
        self.works_started = 0
        self.seen_warnings = set()
        self.update_stats()
        self.stop_flag.clear()
        self.set_running(True)
        self.log(tr(f"— Начинаю: {len(urls)} ссылок → {dest}",
                    f"— Starting: {len(urls)} links → {dest}"), "info")

        self.worker = threading.Thread(
            target=self.run_job,
            args=(urls, dest, self.cmb_browser.get(), user, pwd,
                  self.threads(), self.cmb_order.get(),
                  self.var_retry.get(), self.var_sort.get()),
            daemon=True)
        self.worker.start()

    def connect_deviantart(self):
        """Grants gallery-dl access on behalf of the account (OAuth), once."""
        if not self.gdl:
            messagebox.showerror(APP_TITLE, tr("gallery-dl не найден.",
                                               "gallery-dl not found."))
            return
        self.stop_flag.clear()
        self.set_running(True)
        self.var_status.set(tr("Жду разрешения в браузере…",
                               "Waiting for approval in the browser…"))
        self.log(tr("— Подключаю DeviantArt. В браузере откроется страница "
                    "DeviantArt: войди и нажми «Authorize».",
                    "— Connecting DeviantArt. A DeviantArt page will open in "
                    "your browser: sign in and click “Authorize”."), "info")
        self.worker = threading.Thread(target=self.run_connect, daemon=True)
        self.worker.start()

    def run_connect(self):
        ok = [False]

        def on_line(line):
            low = line.lower()
            if "been cached" in low:          # token saved
                ok[0] = True
            elif line.startswith("https://www.deviantart.com/oauth2"):
                self.emit("log", (tr("Если браузер не открылся, открой ссылку "
                                     "вручную:",
                                     "If the browser didn't open, open this "
                                     "link manually:"), "info"))
                self.emit("log", (line, "info"))
            elif "[error]" in line or "[warning]" in line:
                self.emit("log", (line, "err"))
            # everything else (including the token itself) stays out of the log

        try:
            self.run_process(["oauth:deviantart"], on_line)
        except Exception as e:
            self.emit("log", (tr(f"Ошибка: {e}", f"Error: {e}"), "err"))
        self.emit("connected", ok[0])

    def stop(self):
        self.stop_flag.set()
        with self.procs_lock:
            for p in self.procs:
                if p.poll() is None:
                    p.terminate()
        self.var_status.set(tr("Останавливаю…", "Stopping…"))

    def organize_now(self):
        folder = self.dest_folder()
        if not folder.is_dir():
            messagebox.showinfo(APP_TITLE, tr(f"Папки пока нет:\n{folder}",
                                              f"Folder doesn't exist yet:\n{folder}"))
            return
        self.report_organized(organize(folder, load_order(folder)))
        self.report_renumbered(reorder(folder, self.cmb_order.get()))
        if self.var_sort.get():
            self.report_sorted(sort_all_chapters(folder))

    # ---------- Linux AppImage menu entry ----------

    def update_menu_card(self):
        if not getattr(self, "btn_menu", None):
            return
        if menu_entry_target():
            self.var_menu.set(tr(
                "Art Downloader есть в меню приложений. Если переместишь файл "
                "программы, значок обновится сам при следующем запуске.",
                "Art Downloader is in your applications menu. If you move the "
                "app file, the entry updates itself on the next start."))
            self.btn_menu.configure(text=tr("Убрать из меню", "Remove from the menu"))
        else:
            self.var_menu.set(tr(
                "Добавь программу в меню, чтобы запускать её как обычное "
                "приложение (клавиша Super → «Art»).",
                "Add the app to your menu to start it like any other app "
                "(Super key → “Art”)."))
            self.btn_menu.configure(text=tr("Добавить в меню приложений",
                                            "Add to the applications menu"))

    def toggle_menu_entry(self):
        try:
            if menu_entry_target():
                remove_menu_entry()
                self.log(tr("Убрано из меню приложений.",
                            "Removed from the applications menu."), "info")
            else:
                install_menu_entry(APPIMAGE)
                self.log(tr("Добавлено в меню приложений.",
                            "Added to the applications menu."), "ok")
        except OSError as e:
            self.log(tr(f"Не получилось: {e}", f"That didn't work: {e}"), "err")
        self.update_menu_card()

    def offer_menu_entry(self):
        if self.menu_asked or menu_entry_target():
            return
        self.menu_asked = True
        self.save_config()
        if messagebox.askyesno(APP_TITLE, tr(
                "Добавить Art Downloader в меню приложений?\n\n"
                "Тогда его можно будет запускать как обычную программу. "
                "Это можно сделать и позже в Настройках.",
                "Add Art Downloader to your applications menu?\n\n"
                "Then you can start it like any other app. You can also do "
                "this later in Settings.")):
            try:
                install_menu_entry(APPIMAGE)
                self.log(tr("Добавлено в меню приложений.",
                            "Added to the applications menu."), "ok")
            except OSError as e:
                self.log(tr(f"Не получилось: {e}", f"That didn't work: {e}"), "err")
            self.update_menu_card()

    # ---------- export to CBZ / PDF ----------

    def open_export(self):
        folder = self.dest_folder()
        plan = export_plan(folder) if folder.is_dir() else []
        if not plan:
            messagebox.showinfo(APP_TITLE, tr(
                f"В папке пока нет скачанных глав:\n{folder}",
                f"No downloaded chapters in this folder yet:\n{folder}"))
            return
        win = ctk.CTkToplevel(self.root)
        win.title(tr("Экспорт в CBZ / PDF", "Export to CBZ / PDF"))
        win.geometry("580x420")
        win.transient(self.root)
        win.after(100, win.grab_set)
        win.configure(fg_color=WINDOW_BG)
        body = ctk.CTkFrame(win, fg_color=CARD, corner_radius=12)
        body.pack(fill="both", expand=True, padx=16, pady=16)
        self.heading(body, tr(f"Книг: {len(plan)}", f"Books: {len(plan)}"), tr(
            "По одной книге на главу (папку Chapter NN) или на папку. "
            "Страницы — в том же порядке, что и файлы.",
            "One book per chapter (Chapter NN folder) or per folder. "
            "Pages follow the file order."), wrap=500)
        names = "\n".join("• " + n for n, _ in plan[:6])
        if len(plan) > 6:
            names += "\n" + tr(f"…и ещё {len(plan) - 6}", f"…and {len(plan) - 6} more")
        ctk.CTkLabel(body, text=names, font=self.font(12), anchor="w",
                     justify="left").pack(fill="x", padx=16, pady=(8, 4))
        fmt = KeyedSegments(body, ["cbz", "pdf"], lambda k: {
            "cbz": tr("CBZ — без потерь", "CBZ — lossless"),
            "pdf": tr("PDF — открывается везде", "PDF — opens anywhere")}[k], "cbz")
        fmt.widget.pack(anchor="w", padx=16, pady=(8, 8))
        ctk.CTkLabel(body, text=tr("Сохранится в: ", "Will be saved to: ")
                     + str(folder / EXPORT_DIR), font=self.font(12),
                     text_color=MUTED, anchor="w", justify="left",
                     wraplength=500).pack(fill="x", padx=16)

        def go():
            win.destroy()
            self.start_export(folder, fmt.get())

        ctk.CTkButton(body, text=tr("Создать", "Create"), height=38,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=go).pack(fill="x", padx=16, pady=(12, 16))
        # fit the window to its content (CustomTkinter scales sizes itself)
        win.update_idletasks()
        try:
            scale = win._get_window_scaling()
        except AttributeError:
            scale = 1.0
        height = int((body.winfo_reqheight() + 34) / scale)
        win.geometry(f"580x{height}")
        win.resizable(False, False)

    def start_export(self, folder, fmt):
        self.stop_flag.clear()
        self.set_running(True)
        self.btn_start.configure(state="disabled")
        self.log(tr(f"— Экспорт в {fmt.upper()}…", f"— Exporting to {fmt.upper()}…"),
                 "info")

        def work():
            try:
                made = export_books(folder, fmt, lambda i, n, name: self.emit(
                    "export_progress", (i, n, name)))
                self.emit("export_done", (True, [str(m) for m in made]))
            except Exception as e:
                self.emit("export_done", (False, str(e)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def on_export_progress(self, payload):
        i, n, name = payload
        self.var_status.set(tr(f"Экспорт: {i} из {n}", f"Exporting: {i} of {n}"))
        self.show_progress((i - 1) / n)
        self.log(f"   {name}", "info")

    def on_export_done(self, payload):
        ok, result = payload
        self.set_running(False)
        self.btn_start.configure(state="normal")
        if ok:
            self.var_status.set(tr(f"Готово: книг {len(result)}",
                                   f"Done: {len(result)} books"))
            self.log(tr(f"— Готово! Книги в папке {EXPORT_DIR}.",
                        f"— Done! The books are in the {EXPORT_DIR} folder."), "ok")
            self.notify_finished(True, tr(f"Экспорт готов: книг {len(result)}.",
                                          f"Export finished: {len(result)} books."))
        else:
            self.var_status.set(tr("Ошибка экспорта — смотри журнал",
                                   "Export failed — see the log"))
            self.log(tr(f"Не удалось сделать книгу: {result}",
                        f"Could not make a book: {result}"), "err")

    # ---------- updates ----------

    def auto_check_updates(self):
        if (self.var_autoupdate.get() and not self.running
                and time.time() - self.last_update_check > 24 * 3600):
            self.check_updates(quiet=True)

    def check_updates(self, quiet=False):
        if getattr(self, "_checking", False):
            return
        self._checking = True
        if not quiet:
            self.var_update.set(tr("Проверяю…", "Checking…"))
        gdl = self.gdl

        def work():
            info = {"quiet": quiet, "current_gdl": gallery_dl_version(gdl),
                    "method": update_method(gdl)}
            try:
                info["latest_gdl"] = latest_gallery_dl()
            except Exception as e:
                info["error"] = str(e)
            info["app"] = latest_app_release()
            self.emit("updates", info)

        threading.Thread(target=work, daemon=True).start()

    def on_updates(self, info):
        self._checking = False
        self.last_update_check = time.time()
        self.save_config()
        cur, latest, method = info["current_gdl"], info.get("latest_gdl"), info["method"]
        self.var_versions.set(f"{APP_TITLE} {APP_VERSION} · gallery-dl {cur or '?'}")
        app = info.get("app")
        new_app = app if app and is_newer(app[0], APP_VERSION) else None
        new_gdl = latest if latest and cur and is_newer(latest, cur) else None
        lines, self.update_action = [], None
        if new_app:
            lines.append(tr(f"Вышла новая версия Art Downloader: {app[0]}.",
                            f"A new Art Downloader version is out: {app[0]}."))
            self.update_action = ("app", app[1])
        if new_gdl:
            if method in ("pip", "pipx"):
                lines.append(tr(f"Доступен gallery-dl {latest} (сейчас {cur}).",
                                f"gallery-dl {latest} is available (you have {cur})."))
                self.update_action = self.update_action or ("gdl", latest)
            elif method == "app":
                lines.append(tr(f"Вышел gallery-dl {latest}. Он придёт с новой "
                                "версией программы.",
                                f"gallery-dl {latest} is out. It will come with "
                                "the next app version."))
            else:
                lines.append(tr(f"Доступен gallery-dl {latest}. Обнови его так же, "
                                "как устанавливал.",
                                f"gallery-dl {latest} is available. Update it the "
                                "way you installed it."))
        if "error" in info and not lines:
            lines.append(tr("Не удалось проверить: нет связи с интернетом?",
                            "Couldn't check: no internet connection?"))
        if not lines:
            lines.append(tr("Всё обновлено.", "Everything is up to date."))
        self.var_update.set(" ".join(lines))
        self.show_update_button()
        if self.update_action and info["quiet"]:
            self.log(tr("Доступно обновление — Настройки → Уведомления и "
                        "обновления.",
                        "An update is available — Settings → Notifications and "
                        "updates."), "info")

    def show_update_button(self):
        btn = getattr(self, "btn_update", None)
        if btn is None:
            return
        if not self.update_action:
            btn.pack_forget()
            return
        kind, value = self.update_action
        btn.configure(text=tr(f"Обновить gallery-dl до {value}",
                              f"Update gallery-dl to {value}") if kind == "gdl"
                      else tr("Скачать новую версию", "Download the new version"))
        btn.pack(side="left", padx=(8, 0))

    def do_update(self):
        if not self.update_action:
            return
        kind, value = self.update_action
        if kind == "app":
            import webbrowser
            webbrowser.open(value)
            return
        cmd = update_command(update_method(self.gdl))
        if not cmd:
            return
        self.stop_flag.clear()
        self.set_running(True)
        self.btn_start.configure(state="disabled")
        self.var_update.set(tr("Обновляю gallery-dl…", "Updating gallery-dl…"))
        self.log(tr("— Обновляю gallery-dl…", "— Updating gallery-dl…"), "info")

        def work():
            code = self.run_process_cmd(cmd)
            self.emit("update_done", (code == 0, gallery_dl_version(self.gdl)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def run_process_cmd(self, cmd):
        """Runs any command, its output goes to the log (used for updates)."""
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", creationflags=CREATE_NO_WINDOW if WINDOWS else 0)
            for line in proc.stdout:
                if line.strip() and not line.startswith(("  ", "Requirement already")):
                    self.emit("log", (line.rstrip()[:200], "skip"))
            return proc.wait()
        except OSError as e:
            self.emit("log", (str(e), "err"))
            return 1

    def on_update_done(self, payload):
        ok, version = payload
        self.set_running(False)
        self.btn_start.configure(state="normal")
        if ok:
            self.update_action = None
            self.show_update_button()
            self.var_versions.set(f"{APP_TITLE} {APP_VERSION} · gallery-dl {version}")
            self.var_update.set(tr(f"gallery-dl обновлён до {version}.",
                                   f"gallery-dl updated to {version}."))
            self.log(tr(f"— Готово! gallery-dl {version}.",
                        f"— Done! gallery-dl {version}."), "ok")
            self.var_status.set(tr("Готово к работе", "Ready"))
        else:
            self.var_update.set(tr("Не удалось обновить — смотри журнал.",
                                   "Update failed — see the log."))

    def report_risky(self, names):
        if not names:
            return
        self.log(tr(f"⚠ Среди скачанного есть файлы, которые могут запускать "
                    f"программы ({len(names)}). Не открывай их, если не "
                    f"уверен в источнике:",
                    f"⚠ Some downloads can run programs ({len(names)}). "
                    f"Don't open them unless you trust the source:"), "err")
        for name in names:
            self.log("   " + name, "err")

    def report_organized(self, moved):
        if not moved:
            return
        total = sum(moved.values())
        self.log(tr("Разложено по папкам: ", "Organized into folders: ")
                 + files_word(total), "ok")
        for name in sorted(moved):
            label = tr("основная папка", "main folder") if name == "." else name
            self.log(f"   {label}: {moved[name]}", "ok")

    def report_renumbered(self, count):
        if count:
            self.log(tr("Перенумеровано по порядку: ", "Renumbered into order: ")
                     + files_word(count), "ok")

    def report_sorted(self, moved):
        if not moved:
            return
        total = sum(moved.values())
        self.log(tr("Разнесено по главам: ", "Split into chapters: ")
                 + files_word(total), "ok")
        for name in sorted(moved):
            self.log(f"   {name}: {moved[name]}", "ok")

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_TITLE, tr(
                    "Скачивание ещё идёт. Остановить и выйти?",
                    "A download is still running. Stop and quit?")):
                return
            self.stop()
        self.save_config()
        self.root.destroy()

    # ---------- background work ----------

    def emit(self, kind, payload=None):
        self.events.put((kind, payload))

    def run_process(self, args, on_line):
        """Runs gallery-dl and passes its output to on_line line by line."""
        env = gdl_env()
        proc = subprocess.Popen(
            list(self.gdl) + args, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace", env=env, bufsize=1,
            creationflags=CREATE_NO_WINDOW if WINDOWS else 0)
        with self.procs_lock:
            self.procs.append(proc)
        if self.stop_flag.is_set():
            proc.terminate()
        try:
            for line in proc.stdout:
                on_line(line.rstrip("\n"))
            return proc.wait()
        finally:
            with self.procs_lock:
                self.procs.remove(proc)

    def run_parallel(self, jobs, n):
        """Runs the functions in jobs, at most n at a time."""
        jobs = list(jobs)
        lock = threading.Lock()

        def worker():
            while not self.stop_flag.is_set():
                with lock:
                    if not jobs:
                        return
                    job = jobs.pop(0)
                job()

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(max(1, min(n, len(jobs))))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def collect(self, urls, args, n):
        """Works in each link: (index, date, folder, title)."""
        results = {}

        def make_job(i, url):
            def job():
                found = []

                def on_line(line):
                    item = parse_collect_line(line)
                    if item:
                        idx, published, ftitle, title = item
                        found.append((idx, published,
                                      group_name(ftitle, url), title,
                                      ftitle not in ("", "None")))
                    elif line.strip():
                        self.emit("line", line)

                self.run_process(args + ["--print", COLLECT_FMT, url], on_line)
                results[i] = found
                self.emit("log", (tr(f"   {group_name(None, url)}: работ "
                                     f"{len(found)}",
                                     f"   {group_name(None, url)}: "
                                     f"{len(found)} works"), "info"))
            return job

        self.run_parallel([make_job(i, u) for i, u in enumerate(urls)], n)
        entries = []
        for i in range(len(urls)):
            entries += results.get(i, [])
        return entries

    def download_parallel(self, links, inc, archive, args, n):
        """Splits links into n parts and downloads them at the same time."""
        parts = split_round_robin(links, n)
        jobs = []
        for i, part in enumerate(parts, 1):
            lf = inc / f"links_{i}.txt"
            lf.write_text("\n".join(part) + "\n", encoding="utf-8")
            jobs.append(lambda lf=lf: self.run_process(
                args + ["-D", str(inc), "--download-archive", str(archive),
                        "-i", str(lf)],
                lambda line: self.emit("line", line)))
        self.run_parallel(jobs, len(jobs))
        for i in range(1, len(parts) + 1):
            (inc / f"links_{i}.txt").unlink(missing_ok=True)

    def run_job(self, urls, dest, browser, user, pwd, n_threads,
                order_mode, retry, do_sort):
        cfg_path = make_run_config(user, pwd)
        inc = dest / INCOMING
        result = "ok"
        try:
            args = base_args(browser, cfg_path)
            if user:
                self.emit("log", (tr(f"Вход в DeviantArt как {user}",
                                     f"Signing in to DeviantArt as {user}"),
                                  "info"))
            inc.mkdir(parents=True, exist_ok=True)

            # 1. Find out which works each link holds, and in which order
            to_expand = [u for u in urls if classify_url(u) == "expand"]
            direct = [u for u in urls if u not in to_expand]
            entries = []
            if to_expand:
                self.emit("status", tr("Собираю список работ…",
                                       "Collecting the list of works…"))
                entries = self.collect(to_expand, args, n_threads)
                if self.stop_flag.is_set():
                    result = "stopped"
                    return
            single_group = tr("Отдельные работы", "Single works")
            for u in direct:
                idx = deviation_id(u)
                if idx:
                    entries.append((idx, 0, single_group, ""))

            old_order = load_order(dest)
            order = build_order(entries, order_mode,
                                titles_from_disk(dest, old_order))
            # files from earlier runs move to their new numbers first, so
            # new downloads land next to them in the right place
            self.emit("renumbered", renumber(dest, old_order, order))
            save_order(dest, order)
            seen = set()
            final = []
            for idx, *_ in entries:
                if idx not in seen:
                    seen.add(idx)
                    final.append(f"https://www.deviantart.com/view/{idx}")
            final += [u for u in direct if not deviation_id(u)]
            self.emit("log", (tr(f"Всего работ: {len(final)}",
                                 f"Total works: {len(final)}"), "info"))
            if not final:
                self.emit("log", (tr("Нечего скачивать — ссылки не найдены.",
                                     "Nothing to download — no links found."),
                                  "err"))
                result = "error"
                return
            (dest / LINKS_FILE).write_text("\n".join(final) + "\n",
                                           encoding="utf-8")

            # 2. Download in several threads
            archive = dest / ARCHIVE_FILE
            n = min(n_threads, len(final))
            self.emit("status", tr(f"Скачиваю: {len(final)} ссылок, потоков: {n}…",
                                   f"Downloading: {len(final)} links, "
                                   f"{n} threads…"))
            self.emit("total", len(final))
            self.download_parallel(final, inc, archive, args, n)
            if self.stop_flag.is_set():
                result = "stopped"
                return

            # 3. Retry failures (one thread, gently)
            if retry:
                failed = self.take_failed()
                if failed:
                    again = retry_links(failed, final)
                    self.emit("retry", len(failed))
                    self.emit("status", tr(f"Повторяю: {len(again)} ссылок…",
                                           f"Retrying: {len(again)} links…"))
                    self.emit("total", len(again))
                    self.download_parallel(again, inc, archive, args, 1)
                    if self.stop_flag.is_set():
                        result = "stopped"
                        return
        except Exception as e:  # keep the window alive on any error
            self.emit("log", (tr(f"Ошибка: {e}", f"Error: {e}"), "err"))
            result = "error"
        finally:
            # 4. Put files in order — even after Stop
            try:
                risky = []
                self.emit("organized", organize(dest, load_order(dest), risky))
                self.emit("risky", risky)
                if do_sort:
                    self.emit("sorted", sort_all_chapters(dest))
            except Exception as e:
                self.emit("log", (tr(f"Не удалось упорядочить: {e}",
                                     f"Could not organize files: {e}"), "err"))
            try:
                os.remove(cfg_path)
            except OSError:
                pass
            self.emit("finish", result)

    def take_failed(self):
        """Fetches the failure list (from the worker thread, via the queue)."""
        box, done = [], threading.Event()
        self.emit("take_failed", (box, done))
        done.wait(5)
        return box

    def handle_line(self, line):
        """Parses one line of gallery-dl output and updates the counters."""
        if not line.strip():
            return
        if PROGRESS_RE.match(line):          # progress, not worth a log line
            self.works_started += 1
            self.update_progress()
            return
        low = line.lower()
        if line.startswith("# "):
            if self.counting_skips:
                self.stats["skip"] += 1
            self.log("· " + pretty_name(line[2:]), "skip")
        elif "[error]" in line:
            self.stats["err"] += 1
            if "Failed to download" in line:
                self.failed.append(line.split("Failed to download", 1)[1].strip())
            self.log(line, "err")
            if "authentication" in low or "password" in low or "login" in low:
                self.log(tr(
                    "   ↳ Не удалось войти. Проверь логин и пароль. Если "
                    "DeviantArt просит капчу — нажми «Подключить DeviantArt».",
                    "   ↳ Sign-in failed. Check username and password. If "
                    "DeviantArt asks for a captcha, click “Connect DeviantArt”."),
                    "err")
        elif "[warning]" in line:
            # the same warning from every thread is shown only once per run
            key = re.sub(r"'[^']*'|\d+", "", line)
            if key in self.seen_warnings:
                return
            self.seen_warnings.add(key)
            self.log(line if len(line) < 220 else line[:220] + " …", "err")
            if "cookies" in low and "unable to find" in low:
                self.log(tr("   ↳ В этом браузере нет входа. Выбери другой "
                            "браузер или нажми «Подключить DeviantArt».",
                            "   ↳ Not signed in with this browser. Pick another "
                            "browser or click “Connect DeviantArt”."), "err")
            elif "403" in line:
                self.log(tr("   ↳ Сайт не отдаёт полную картинку. Для 18+ нажми "
                            "«Подключить DeviantArt», потом «Скачать» ещё раз.",
                            "   ↳ The site refuses the full image. For 18+ click "
                            "“Connect DeviantArt”, then “Download” again."),
                         "err")
        elif line.startswith(("/", "✔ /")):
            self.stats["done"] += 1
            self.log("✔ " + pretty_name(line), "ok")
        else:
            self.log(line, "info")
        self.update_stats()

    def update_progress(self):
        total = self.total_works
        if not total:
            return
        done = min(self.works_started, total)
        self.var_status.set(tr(f"Скачиваю: {done} из {total}",
                               f"Downloading: {done} of {total}"))
        self.show_progress(done / total)

    def poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "line":
                    self.handle_line(payload)
                elif kind == "log":
                    self.log(*payload)
                elif kind == "status":
                    self.var_status.set(payload)
                elif kind == "risky":
                    self.report_risky(payload)
                elif kind == "organized":
                    self.report_organized(payload)
                elif kind == "sorted":
                    self.report_sorted(payload)
                elif kind == "renumbered":
                    self.report_renumbered(payload)
                elif kind == "take_failed":
                    box, done = payload
                    box.extend(self.failed)
                    done.set()
                elif kind == "total":
                    self.total_works, self.works_started = payload, 0
                elif kind == "retry":
                    self.log(tr(f"— Не скачалось {payload}. Пробую ещё раз…",
                                f"— {payload} failed. Trying again…"), "info")
                    self.failed = []
                    self.stats["err"] = 0
                    self.counting_skips = False
                    self.update_stats()
                elif kind == "finish":
                    self.on_finish(payload)
                elif kind == "export_progress":
                    self.on_export_progress(payload)
                elif kind == "export_done":
                    self.on_export_done(payload)
                elif kind == "gdl_version":
                    self.var_versions.set(f"{APP_TITLE} {APP_VERSION} · "
                                          f"gallery-dl {payload or '?'}")
                elif kind == "updates":
                    self.on_updates(payload)
                elif kind == "update_done":
                    self.on_update_done(payload)
                elif kind == "connected":
                    self.on_connected(payload)
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def on_connected(self, ok):
        self.set_running(False)
        if ok:
            self.da_connected = True
            self.save_config()
            self.update_connected()
            self.var_status.set(tr("DeviantArt подключён!",
                                   "DeviantArt connected!"))
            self.log(tr("— Готово! DeviantArt подключён. Теперь можно "
                        "скачивать, в том числе 18+.",
                        "— Done! DeviantArt is connected. You can now "
                        "download, including 18+ works."), "ok")
        else:
            self.var_status.set(tr("Не удалось подключить DeviantArt",
                                   "Could not connect DeviantArt"))
            self.log(tr("— Подключение не завершилось. Попробуй ещё раз и "
                        "нажми «Authorize» в браузере.",
                        "— Connection didn't finish. Try again and click "
                        "“Authorize” in the browser."), "err")

    def notify_finished(self, ok, body):
        """Desktop notification + blinking taskbar, only if the app is in the
        background (no point interrupting someone who is looking at it)."""
        if not self.var_notify.get():
            return
        try:
            focused = self.root.focus_displayof() is not None
        except (KeyError, tk.TclError):
            focused = False
        if focused and self.root.state() != "iconic":
            return
        title = APP_TITLE + (" — " + tr("готово", "done") if ok else "")
        send_notification(title, body)
        flash_taskbar(self.root)

    def on_finish(self, result):
        self.set_running(False)
        if result == "ok":
            done, n = self.stats["done"], len(self.failed)
            if n:
                body = tr(f"Скачано: {files_word(done)}. Не скачалось: {n}.",
                          f"Downloaded {files_word(done)}. Failed: {n}.")
            else:
                body = tr(f"Всё скачалось: {files_word(done)}.",
                          f"Everything downloaded: {files_word(done)}.")
            self.notify_finished(not n, body)
        elif result == "error":
            self.notify_finished(False, tr("Скачивание прервалось с ошибкой.",
                                           "The download stopped with an error."))
        if result == "stopped":
            self.var_status.set(tr(
                "Остановлено. Нажми «Скачать» — продолжит с места.",
                "Stopped. Click “Download” to resume."))
            self.log(tr("— Остановлено. Повторный запуск докачает недостающее.",
                        "— Stopped. Running again downloads only what's "
                        "missing."), "info")
            return
        if result == "error":
            self.var_status.set(tr("Ошибка — смотри журнал",
                                   "Error — see the log"))
            return
        if self.failed:
            n = len(self.failed)
            self.var_status.set(tr(f"Готово, но не скачалось: {n}",
                                   f"Done, but {n} failed"))
            self.log(tr(f"Не скачались ({n}). Обычно это работы по подписке "
                        "или 18+ без входа в аккаунт:",
                        f"Not downloaded ({n}). Usually subscription-only "
                        "works or 18+ without signing in:"), "err")
            for name in self.failed:
                self.log("   " + pretty_name(name), "err")
        else:
            self.var_status.set(tr("Готово! Всё скачалось.",
                                   "Done! Everything downloaded."))
            self.log(tr("— Готово! Всё скачалось.",
                        "— Done! Everything downloaded."), "ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == GALLERY_DL_FLAG:
        run_builtin_gallery_dl(sys.argv[2:])
        return
    restore_host_env()
    if len(sys.argv) > 1 and sys.argv[1] == REMOVE_MENU_FLAG:
        remove_menu_entry()
        return
    if WINDOWS:
        try:  # sharp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    ctk.set_default_color_theme("green")
    root = ctk.CTk(className="art-downloader")   # matches the menu icon
    app = App(root)
    if SELF_TEST_FLAG in sys.argv[1:]:          # used by the release build
        for _ in range(20):
            root.update()
        ok = app.gdl is not None and gallery_dl_version(app.gdl) is not None
        print("self-test:", "ok" if ok else "gallery-dl not working", flush=True)
        root.destroy()
        sys.exit(0 if ok else 1)
    root.mainloop()


if __name__ == "__main__":
    main()
