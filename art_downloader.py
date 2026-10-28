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
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

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
# Tk on Windows can't always draw emoji outside the basic plane
GLOBE = "" if WINDOWS else "🌐"
KEY = "" if WINDOWS else "🔑 "
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
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
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


def resolve_mode(mode, titles):
    """
    'auto' becomes 'name' when nearly every title carries its own number
    (typical for comics: "Part 19", "Chapter 3"), otherwise 'date'.
    """
    if mode != "auto":
        return mode
    titles = [t for t in titles if t]
    if len(titles) < 2:
        return "date"
    numbers = [n for n in map(title_number, titles) if n is not None]
    if (len(numbers) >= 0.8 * len(titles)
            and len(set(numbers)) >= 0.8 * len(numbers)):
        return "name"
    return "date"


def order_meta(value):
    """Order entry as (group, pos, published, title, seq), old format too."""
    group, pos = value[0], value[1]
    published = value[2] if len(value) > 2 else 0
    title = value[3] if len(value) > 3 else None
    seq = value[4] if len(value) > 4 else pos
    return group, pos, published, title, seq


def build_order(entries, mode, existing=None):
    """
    entries:  [(index, published, group, title), …] in the order the site
              returned them (gallery order).
    mode:     one of ORDER_KEYS.
    existing: previous result — works not in `entries` are kept and ranked
              together with the new ones.
    Returns {index: [group, pos, published, title, seq]} with positions
    1, 2, 3, … in each group, in the requested order.
    """
    items = {}
    for idx, value in (existing or {}).items():
        items[idx] = list(order_meta(value))
    seen = set()
    for seq, (idx, published, group, title) in enumerate(entries):
        if idx in seen:
            continue
        seen.add(idx)
        old = items.get(idx)
        items[idx] = [group, old[1] if old else 10**9, published or 0,
                      clean_title(title) if title else (old[3] if old else None),
                      seq]

    groups = {}
    for idx, (group, pos, published, title, seq) in items.items():
        groups.setdefault(group, []).append((idx, pos, published, title, seq))

    order = {}
    for group, members in groups.items():
        mode_here = resolve_mode(mode, [m[3] for m in members])
        if mode_here == "date":
            members.sort(key=lambda m: (m[2] or 0, m[4], m[1]))
        elif mode_here == "name":
            members.sort(key=lambda m: (natural_key(m[3]), m[4], m[1]))
        else:  # gallery order
            members.sort(key=lambda m: (m[4], m[1]))
        for pos, (idx, _, published, title, seq) in enumerate(members, 1):
            order[idx] = [group, pos, published, title, seq]
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
    Renames files already on disk from the old numbers to the new ones
    (two passes, so no name ever collides). Also tidies spacing in names.
    Returns how many files were renamed.
    """
    dest = Path(dest)
    moves = []
    groups = {v[0] for v in new.values()}
    for group in groups:
        if not is_safe_component(group) or not inside(dest, dest / group):
            continue
        mapping = {}
        for idx, value in new.items():
            if value[0] == group and idx in old and old[idx][0] == group:
                mapping[old[idx][1]] = value[1]
        width = group_width(new, group)
        for path, pos, num, title, ext in scan_group(dest / group):
            if pos not in mapping:
                continue
            target = path.with_name(numbered_name(mapping[pos], width, num,
                                                  title, ext))
            if target != path:
                moves.append((path, target))
    temps = []
    for i, (path, target) in enumerate(moves):
        tmp = path.with_name(f".renumber-{i}-{path.name}")
        path.rename(tmp)
        temps.append((tmp, target))
    for tmp, target in temps:
        tmp.rename(unique_path(target))   # never overwrite another file
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
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        if target.exists():
            if target.is_file() and target.stat().st_size == f.stat().st_size:
                f.unlink()                    # same file downloaded again
                continue
            target = unique_path(target)      # never overwrite another file
        f.replace(target)
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


def sort_into_chapters(folder):
    """
    If one folder holds several chapters, move them into Chapter NN.
    Files without a chapter in the name stay put. Nothing is deleted.
    """
    folder = Path(folder)
    files = [f for f in sorted(folder.iterdir())
             if f.is_file() and not f.name.startswith(".")
             and not (f.name.startswith("links") and f.suffix == ".txt")]
    chapters = {f: chapter_folder(f.name) for f in files}
    present = {c for c in chapters.values() if c}
    present |= {d.name for d in folder.iterdir()
                if d.is_dir() and CHAPTER_DIR_RE.match(d.name)}
    if len(present) < 2:
        return {}
    moved = {}
    for f, ch in chapters.items():
        if not ch:
            continue
        (folder / ch).mkdir(exist_ok=True)
        if not (folder / ch / f.name).exists():
            f.rename(folder / ch / f.name)
            moved[ch] = moved.get(ch, 0) + 1
    return moved


def sort_all_chapters(dest):
    """Chapter split in the main folder and in every gallery folder."""
    dest = Path(dest)
    result = {}
    folders = [dest] + [d for d in sorted(dest.iterdir())
                        if d.is_dir() and not d.name.startswith(".")
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
# Window
# --------------------------------------------------------------------------

class KeyedCombo:
    """Read-only combobox that shows translated labels but stores keys."""

    def __init__(self, parent, keys, label_fn, key, width=20):
        self.keys, self.label_fn, self.key = keys, label_fn, key
        self.var = tk.StringVar(value=label_fn(key))
        self.widget = ttk.Combobox(parent, textvariable=self.var, width=width,
                                   values=[label_fn(k) for k in keys],
                                   state="readonly")
        self.widget.bind("<<ComboboxSelected>>", self._changed)

    def _changed(self, _event=None):
        labels = [self.label_fn(k) for k in self.keys]
        if self.var.get() in labels:
            self.key = self.keys[labels.index(self.var.get())]

    def get(self):
        return self.key


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        scale = max(1.0, root.winfo_fpixels("1i") / 96) if WINDOWS else 1.0
        self.root.geometry(f"{int(860 * scale)}x{int(830 * scale)}")
        self.root.minsize(int(680 * scale), int(640 * scale))
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

        cfg = migrate_config(self.load_config())
        set_lang(cfg.get("lang") or detect_lang())
        self.browser_key = cfg["browser"]
        self.order_key = cfg["order"]
        self.var_base = tk.StringVar(value=cfg.get("base", str(DEFAULT_BASE)))
        self.var_name = tk.StringVar(value="")
        self.var_user = tk.StringVar(value=cfg.get("username", ""))
        self.var_pass = tk.StringVar(value="")
        self.var_threads = tk.IntVar(value=cfg.get("threads", 3))
        self.var_sort = tk.BooleanVar(value=cfg.get("sort", True))
        self.var_retry = tk.BooleanVar(value=cfg.get("retry", True))
        self.da_connected = bool(cfg.get("da_connected", False))
        self.var_connected = tk.StringVar()
        self.var_status = tk.StringVar()
        self.var_stats = tk.StringVar(value="")

        self.build_ui()
        self.refresh_texts()
        self.check_gallery_dl()
        self.root.after(100, self.poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- interface ----------

    def set_window_icon(self):
        icon = resource_path("icon.png")
        if not icon:
            return
        try:
            self._icon = tk.PhotoImage(file=str(icon))
            self.root.iconphoto(True, self._icon)
        except tk.TclError:
            pass

    def build_ui(self):
        pad = {"padx": 10, "pady": 4}
        style = ttk.Style()
        for theme in (("vista", "clam") if WINDOWS else ("clam",)):
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue
        style.configure("Big.TButton", font=("TkDefaultFont", 11, "bold"),
                        padding=(14, 6))
        grey = {"foreground": "#666"}

        # Language
        frm_top = ttk.Frame(self.root)
        frm_top.pack(fill="x", padx=10, pady=(6, 0))
        self.cmb_lang = KeyedCombo(frm_top, list(LANGUAGES), LANGUAGES.get,
                                   get_lang(), width=10)
        self.cmb_lang.widget.pack(side="right")
        self.cmb_lang.widget.bind("<<ComboboxSelected>>", self.change_language,
                                  add="+")
        ttk.Label(frm_top, text=GLOBE or "Language / Язык:").pack(
            side="right", padx=(0, 4))

        # Links
        frm_links = ttk.LabelFrame(
            self.root, text=tr(" Ссылки (по одной на строку) ",
                               " Links (one per line) "))
        frm_links.pack(fill="x", **pad)
        self.txt_links = tk.Text(frm_links, height=6, wrap="none",
                                 font=(MONO_FONT, 10), undo=True)
        self.txt_links.pack(fill="x", padx=6, pady=6)
        ttk.Label(frm_links, text=tr(
            "Папка, глава, вся серия, галерея, профиль или отдельная работа. "
            "Другие сайты тоже можно.",
            "A folder, chapter, whole series, gallery, profile or single work. "
            "Other sites work too."), **grey).pack(anchor="w", padx=6, pady=(0, 6))

        # Destination
        frm_dest = ttk.LabelFrame(self.root, text=tr(" Куда сохранять ",
                                                     " Save to "))
        frm_dest.pack(fill="x", **pad)
        frm_dest.columnconfigure(1, weight=1)
        ttk.Label(frm_dest, text=tr("Основная папка:", "Main folder:")).grid(
            row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(frm_dest, textvariable=self.var_base).grid(
            row=0, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(frm_dest, text=tr("Выбрать…", "Browse…"),
                   command=self.choose_base).grid(row=0, column=2, padx=6, pady=4)
        ttk.Label(frm_dest, text=tr("Название работы:", "Project name:")).grid(
            row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(frm_dest, textvariable=self.var_name).grid(
            row=1, column=1, sticky="ew", padx=6, pady=4)
        ttk.Label(frm_dest, text=tr("(станет подпапкой)", "(becomes a subfolder)"),
                  **grey).grid(row=1, column=2, sticky="w", padx=6)

        # Sign-in
        frm_auth = ttk.LabelFrame(self.root, text=tr(
            " Вход в DeviantArt (для 18+) ", " DeviantArt sign-in (for 18+) "))
        frm_auth.pack(fill="x", **pad)
        frm_auth.columnconfigure(1, weight=1)
        frm_auth.columnconfigure(3, weight=1)
        ttk.Label(frm_auth, text=tr("Логин:", "Username:")).grid(
            row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(frm_auth, textvariable=self.var_user).grid(
            row=0, column=1, sticky="ew", padx=6, pady=4)
        ttk.Label(frm_auth, text=tr("Пароль:", "Password:")).grid(
            row=0, column=2, sticky="w", padx=6, pady=4)
        ttk.Entry(frm_auth, textvariable=self.var_pass, show="•").grid(
            row=0, column=3, sticky="ew", padx=6, pady=4)
        ttk.Label(frm_auth, text=tr("Или вход из браузера:",
                                    "Or cookies from browser:")).grid(
            row=1, column=0, sticky="w", padx=6, pady=4)
        self.cmb_browser = KeyedCombo(frm_auth, BROWSER_KEYS, browser_label,
                                      self.browser_key)
        self.cmb_browser.widget.grid(row=1, column=1, sticky="w", padx=6, pady=4)
        self.btn_connect = ttk.Button(
            frm_auth, text=KEY + tr("Подключить DeviantArt", "Connect DeviantArt"),
            command=self.connect_deviantart)
        self.btn_connect.grid(row=1, column=2, columnspan=2, sticky="e",
                              padx=6, pady=4)
        ttk.Label(frm_auth, textvariable=self.var_connected, **grey).grid(
            row=2, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 6))

        # Settings
        frm_opt = ttk.LabelFrame(self.root, text=tr(" Настройки ", " Settings "))
        frm_opt.pack(fill="x", **pad)
        ttk.Label(frm_opt, text=tr("Порядок номеров:", "Numbering order:")).grid(
            row=0, column=0, sticky="w", padx=6, pady=4)
        self.cmb_order = KeyedCombo(frm_opt, ORDER_KEYS, order_label,
                                    self.order_key)
        self.cmb_order.widget.grid(row=0, column=1, sticky="w", padx=6, pady=4)
        ttk.Label(frm_opt, text=tr("авто: по номеру в названии, если он есть",
                                   "auto: by the number in the title, if any"),
                  **grey).grid(row=0, column=2, sticky="w")
        ttk.Label(frm_opt, text=tr("Скачивать одновременно:",
                                   "Parallel downloads:")).grid(
            row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Spinbox(frm_opt, from_=1, to=MAX_THREADS, width=4, state="readonly",
                    textvariable=self.var_threads).grid(
            row=1, column=1, sticky="w", padx=6, pady=4)
        ttk.Label(frm_opt, text=tr("потоков (3 — оптимально)",
                                   "threads (3 is optimal)"),
                  **grey).grid(row=1, column=2, sticky="w")
        ttk.Checkbutton(
            frm_opt, variable=self.var_retry,
            text=tr("Повторить то, что не скачалось с первого раза",
                    "Retry anything that failed the first time")
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=2)
        ttk.Checkbutton(
            frm_opt, variable=self.var_sort,
            text=tr("Если в одной папке несколько глав — разнести по "
                    "Chapter 01, 02 …",
                    "If one folder has several chapters, split into "
                    "Chapter 01, 02 …")
        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(2, 6))

        # Buttons
        frm_btn = ttk.Frame(self.root)
        frm_btn.pack(fill="x", **pad)
        self.btn_start = ttk.Button(frm_btn, text=tr("⬇  Скачать", "⬇  Download"),
                                    style="Big.TButton", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(frm_btn, text=tr("■  Стоп", "■  Stop"),
                                   command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Button(frm_btn, text=tr("Упорядочить файлы", "Organize files"),
                   command=self.organize_now).pack(side="left")
        ttk.Button(frm_btn, text=tr("Открыть папку", "Open folder"),
                   command=self.open_folder).pack(side="right")

        # Status
        frm_status = ttk.Frame(self.root)
        frm_status.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(frm_status, mode="indeterminate")
        self.progress.pack(fill="x")
        row = ttk.Frame(frm_status)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, textvariable=self.var_status).pack(side="left")
        ttk.Label(row, textvariable=self.var_stats,
                  font=("TkDefaultFont", 10, "bold")).pack(side="right")

        # Log
        frm_log = ttk.LabelFrame(self.root, text=tr(" Журнал ", " Log "))
        frm_log.pack(fill="both", expand=True, **pad)
        self.txt_log = tk.Text(frm_log, height=8, wrap="none", state="disabled",
                               font=(MONO_FONT, 9), background="#1e1e1e",
                               foreground="#d4d4d4")
        scroll = ttk.Scrollbar(frm_log, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt_log.pack(fill="both", expand=True, padx=(6, 0), pady=6)
        self.txt_log.tag_configure("err", foreground="#f48771")
        self.txt_log.tag_configure("ok", foreground="#89d185")
        self.txt_log.tag_configure("skip", foreground="#808080")
        self.txt_log.tag_configure("info", foreground="#75beff")

    def refresh_texts(self):
        """Texts that live in variables (status, counters, sign-in hint)."""
        self.update_connected()
        if self.stats != {"done": 0, "skip": 0, "err": 0}:
            self.update_stats()
        if not self.running:
            self.var_status.set(tr("Готово к работе", "Ready"))

    def change_language(self, _event=None):
        new = self.cmb_lang.get()
        if new == get_lang():
            return
        if self.running:
            messagebox.showinfo(APP_TITLE, tr(
                "Язык можно сменить, когда скачивание закончится.",
                "You can change the language after the download finishes."))
            self.cmb_lang.key = get_lang()
            self.cmb_lang.var.set(LANGUAGES[get_lang()])
            return
        self.browser_key = self.cmb_browser.get()
        self.order_key = self.cmb_order.get()
        links = self.txt_links.get("1.0", "end-1c")
        log_dump = self.txt_log.dump("1.0", "end-1c", text=True, tag=True)
        set_lang(new)
        for w in self.root.winfo_children():
            w.destroy()
        self.build_ui()
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
                "base": self.var_base.get(),
                "browser": self.cmb_browser.get(),
                "username": self.var_user.get().strip(),
                "threads": self.threads(),
                "order": self.cmb_order.get(),
                "settings_version": SETTINGS_VERSION,
                "retry": self.var_retry.get(),
                "sort": self.var_sort.get(),
                "da_connected": self.da_connected,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ---------- helpers ----------

    def update_connected(self):
        if self.da_connected:
            self.var_connected.set(tr(
                "✔ DeviantArt подключён — работы 18+ скачиваются в полном "
                "качестве.",
                "✔ DeviantArt connected — 18+ works download in full quality."))
        else:
            self.var_connected.set(tr(
                "Для 18+ один раз нажми «Подключить DeviantArt». Пароль не "
                "сохраняется.",
                "For 18+ works click “Connect DeviantArt” once. No password "
                "is stored."))

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
            f"Скачано: {s['done']}   Пропущено: {s['skip']}   Ошибок: {s['err']}",
            f"Downloaded: {s['done']}   Skipped: {s['skip']}   Errors: {s['err']}"))

    def set_running(self, running):
        self.running = running
        state = "disabled" if running else "normal"
        self.btn_connect.configure(state=state)
        self.btn_start.configure(state=state)
        self.btn_stop.configure(state="normal" if running else "disabled")
        self.cmb_lang.widget.configure(state="disabled" if running else "readonly")
        if running:
            self.progress.start(12)
        else:
            self.progress.stop()

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
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
                   PYTHONUTF8="1")
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
                                      group_name(ftitle, url), title))
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
                elif kind == "retry":
                    self.log(tr(f"— Не скачалось {payload}. Пробую ещё раз…",
                                f"— {payload} failed. Trying again…"), "info")
                    self.failed = []
                    self.stats["err"] = 0
                    self.counting_skips = False
                    self.update_stats()
                elif kind == "finish":
                    self.on_finish(payload)
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

    def on_finish(self, result):
        self.set_running(False)
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
    if WINDOWS:
        try:  # sharp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk(className="art-downloader")   # matches the menu icon
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
