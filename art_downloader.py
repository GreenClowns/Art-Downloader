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
  * interface in English and Russian.

Run:       python3 art_downloader.py
Requires:  gallery-dl and Tkinter (Fedora: sudo dnf install python3-tkinter)
"""

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "Art Downloader"
CONFIG_PATH = Path.home() / ".config" / "art_downloader.json"
DEFAULT_BASE = Path.home() / "ArtDownloads"
MAX_THREADS = 5

BROWSER_KEYS = ["firefox", "chrome", "chromium", "none"]
ORDER_KEYS = ["date", "gallery", "name"]

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


def detect_lang(env=None):
    """Interface language from the system locale: Russian for ru/uk/be/kk."""
    env = os.environ if env is None else env
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = (env.get(var) or "").lower()
        if value:
            return "ru" if value[:2] in ("ru", "uk", "be", "kk") else "en"
    return "en"


def set_lang(code):
    global _lang
    _lang = code if code in LANGUAGES else "en"


def get_lang():
    return _lang


def tr(ru, en):
    """Pick the string for the current interface language."""
    return ru if _lang == "ru" else en


def browser_label(key):
    if key == "none":
        return tr("не брать из браузера", "don't use a browser")
    return key


def order_label(key):
    return {"date": tr("по дате публикации", "by publication date"),
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
        cfg["order"] = "date"
    if cfg.get("browser") in ("не брать из браузера", "без входа"):
        cfg["browser"] = "none"
    if cfg.get("browser") not in BROWSER_KEYS:
        cfg["browser"] = "firefox"
    return cfg


# --------------------------------------------------------------------------
# Logic without the window (tested separately)
# --------------------------------------------------------------------------

def find_gallery_dl():
    """Path to gallery-dl: from PATH or ~/.local/bin (pipx install)."""
    found = shutil.which("gallery-dl")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "gallery-dl"
    return str(local) if local.exists() else None


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


def safe_name(name):
    """Folder name without forbidden characters."""
    name = re.sub(r'[/\\\x00-\x1f]', "-", name or "").strip(" .")
    return name[:120] or tr("Без названия", "Untitled")


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


def build_order(entries, mode, existing=None):
    """
    entries:  [(index, published, group, title), …] in the order the site
              returned them (gallery order).
    mode:     one of ORDER_KEYS.
    existing: previous result {index: [group, pos]} — numbers already given
              never change, new works are appended at the end.
    Returns {index: [group, pos]}.
    """
    order = {k: list(v) for k, v in (existing or {}).items()}
    groups = {}
    for seq, (idx, published, group, title) in enumerate(entries):
        if idx in order or any(idx == e[0] for e in groups.get(group, ())):
            continue
        groups.setdefault(group, []).append((idx, published, title, seq))

    for group, items in groups.items():
        if mode == "date":
            items.sort(key=lambda e: (e[1] or 0, e[3]))
        elif mode == "name":
            items.sort(key=lambda e: (natural_key(e[2]), e[3]))
        else:  # gallery order
            items.sort(key=lambda e: e[3])
        pos = max([p for g, p in order.values() if g == group], default=0)
        for idx, _, _, _ in items:
            pos += 1
            order[idx] = [group, pos]
    return order


def load_order(dest):
    try:
        return json.loads((Path(dest) / ORDER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_order(dest, order):
    (Path(dest) / ORDER_FILE).write_text(
        json.dumps(order, ensure_ascii=False, indent=1), encoding="utf-8")


def organize(dest, order):
    """
    Moves downloads out of .incoming: DeviantArt files go to their gallery
    folder as "NNN - Title", everything else to the main folder.
    Returns {folder: number of files}.
    """
    dest = Path(dest)
    inc = dest / INCOMING
    if not inc.is_dir():
        return {}
    widths = {}
    for group, pos in order.values():
        widths[group] = max(widths.get(group, 3), len(str(pos)))

    moved = {}
    for f in sorted(inc.iterdir()):
        if not f.is_file() or f.suffix == ".part" or f.name.startswith("."):
            continue
        m = DA_FILE_RE.match(f.name)
        if m and m.group(1) in order:
            group, pos = order[m.group(1)]
            num, title, ext = int(m.group(2)), m.group(3), m.group(4) or ""
            name = f"{pos:0{widths[group]}d}"
            if num > 1:
                name += f"-{num:02d}"
            name += f" - {title}{ext}"
            target_dir = dest / group
        elif m:      # DeviantArt, but order unknown
            num, title, ext = int(m.group(2)), m.group(3), m.group(4) or ""
            name = title + (f" ({num})" if num > 1 else "") + ext
            group, target_dir = ".", dest
        else:        # other sites — keep as is
            name, group, target_dir = f.name, ".", dest
        target_dir.mkdir(parents=True, exist_ok=True)
        f.replace(target_dir / name)
        moved[group] = moved.get(group, 0) + 1
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
        json.dump({"extractor": {"deviantart": da}}, f)
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
        self.root.geometry("860x830")
        self.root.minsize(680, 640)

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

    def build_ui(self):
        pad = {"padx": 10, "pady": 4}
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
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
        ttk.Label(frm_top, text="🌐").pack(side="right", padx=(0, 4))

        # Links
        frm_links = ttk.LabelFrame(
            self.root, text=tr(" Ссылки (по одной на строку) ",
                               " Links (one per line) "))
        frm_links.pack(fill="x", **pad)
        self.txt_links = tk.Text(frm_links, height=6, wrap="none",
                                 font=("monospace", 10), undo=True)
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
            frm_auth, text=tr("🔑 Подключить DeviantArt", "🔑 Connect DeviantArt"),
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
        ttk.Label(frm_opt, text=tr("файлы: 001 - …, 002 - …",
                                   "files: 001 - …, 002 - …"),
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
                               font=("monospace", 9), background="#1e1e1e",
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

    def check_gallery_dl(self):
        self.gdl = find_gallery_dl()
        if self.gdl:
            self.log(tr(f"gallery-dl найден: {self.gdl}",
                        f"gallery-dl found: {self.gdl}"), "info")
        else:
            self.log(tr("gallery-dl не найден! Установи: pipx install gallery-dl",
                        "gallery-dl not found! Install it: pipx install gallery-dl"),
                     "err")

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
            messagebox.showerror(APP_TITLE, tr(
                "gallery-dl не найден.\nУстанови его: pipx install gallery-dl",
                "gallery-dl not found.\nInstall it: pipx install gallery-dl"))
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
        if self.var_sort.get():
            self.report_sorted(sort_all_chapters(folder))

    def report_organized(self, moved):
        if not moved:
            return
        total = sum(moved.values())
        self.log(tr(f"Разложено по папкам: {total} файлов",
                    f"Organized into folders: {total} files"), "ok")
        for name in sorted(moved):
            label = tr("основная папка", "main folder") if name == "." else name
            self.log(f"   {label}: {moved[name]}", "ok")

    def report_sorted(self, moved):
        if not moved:
            return
        total = sum(moved.values())
        self.log(tr(f"Разнесено по главам: {total} файлов",
                    f"Split into chapters: {total} files"), "ok")
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
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        proc = subprocess.Popen(
            [self.gdl] + args, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", env=env, bufsize=1)
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

            order = build_order(entries, order_mode, load_order(dest))
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
                self.emit("organized", organize(dest, load_order(dest)))
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
                elif kind == "organized":
                    self.report_organized(payload)
                elif kind == "sorted":
                    self.report_sorted(payload)
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
    root = tk.Tk(className="art-downloader")   # matches the menu icon
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
