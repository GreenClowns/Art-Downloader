#!/usr/bin/env python3
"""
Art Downloader — простое окно поверх gallery-dl.

Умеет:
  * скачивать по ссылкам на папки, галереи, отдельные работы (DeviantArt
    и любые другие сайты, которые поддерживает gallery-dl);
  * для DeviantArt — скачивать ВСЕ картинки внутри многостраничных работ;
  * сохранять по порядку: каждая папка автора — своя папка у тебя,
    файлы пронумерованы 001, 002, … (по дате, как в галерее или по названию);
  * входить в DeviantArt по логину и паролю или брать вход из браузера;
  * качать в несколько потоков, сам повторять неудачное;
  * продолжать с места остановки (уже скачанное пропускается).

Запуск:  python3 art_downloader.py
Нужно:   gallery-dl  и  python3-tkinter  (sudo dnf install python3-tkinter)
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
BROWSERS = ["firefox", "chrome", "chromium", "не брать из браузера"]
NO_BROWSER = BROWSERS[-1]
ORDER_MODES = ["по дате публикации", "как в галерее", "по названию"]

LINKS_FILE = "links.txt"
ARCHIVE_FILE = ".downloaded.sqlite3"
ORDER_FILE = ".order.json"
INCOMING = ".incoming"          # сюда качается, потом раскладывается
SINGLE_GROUP = "Отдельные работы"
MAX_THREADS = 5

# Имя, под которым gallery-dl сохраняет файлы DeviantArt (до переименования)
DA_FILENAME = "da{index}_{num|'1':>02}_{title}.{extension}"
DA_FILE_RE = re.compile(r"^da(\d+)_(\d+)_(.*?)(\.[^.]+)?$")
# Строка, которую печатает gallery-dl при сборе ссылок
COLLECT_FMT = "@@{index}\t{published_time}\t{folder[title]}\t{title}"
COLLECT_RE = re.compile(r"^@@(\d+)\t([^\t]*)\t([^\t]*)\t(.*)$")
CHAPTER_RE = re.compile(
    r"(?<![^\W_])(?:chapter|chap|ch|глава|episode|ep)\.?[\s_\-]*(\d+)",
    re.IGNORECASE,
)
CHAPTER_DIR_RE = re.compile(r"^Chapter \d+$")


# --------------------------------------------------------------------------
# Логика без окна (её можно проверять отдельно)
# --------------------------------------------------------------------------

def find_gallery_dl():
    """Путь к gallery-dl: из PATH или из ~/.local/bin (установка через pipx)."""
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
    'expand' — ссылка DeviantArt на папку/галерею/профиль: сначала
               собираем список работ внутри (и их порядок);
    'direct' — всё остальное (отдельная работа DA или другой сайт).
    """
    if not is_deviantart(url):
        return "direct"
    u = url.lower()
    if "/art/" in u or "/view/" in u or "fav.me" in u or "/journal/" in u:
        return "direct"
    return "expand"


def deviation_id(url):
    """Номер работы DeviantArt из прямой ссылки (или None)."""
    if not is_deviantart(url):
        return None
    u = url.split("?")[0].split("#")[0].rstrip("/")
    m = re.search(r"/view/(\d+)$", u) or re.search(r"/art/(?:.*-)?(\d+)$", u)
    return m.group(1) if m else None


def parse_urls(text):
    """Ссылки из текста: по одной на строку, без дублей, порядок сохраняется."""
    seen, result = set(), []
    for line in text.splitlines():
        for part in line.split():
            if part.startswith(("http://", "https://")) and part not in seen:
                seen.add(part)
                result.append(part)
    return result


def safe_name(name):
    """Название папки без запрещённых символов."""
    name = re.sub(r'[/\\\x00-\x1f]', "-", name or "").strip(" .")
    return name[:120] or "Без названия"


def group_name(folder_title, url):
    """Название папки: как на сайте, иначе из ссылки."""
    if folder_title and folder_title != "None":
        return safe_name(folder_title)
    tail = url.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
    if tail.isdigit() or tail in ("all", "gallery", ""):
        tail = url.split("deviantart.com/")[-1].split("/")[0]
    return safe_name(tail.replace("---", " - ").replace("-", " "))


def parse_collect_line(line):
    """'@@index<TAB>время<TAB>папка<TAB>название' → кортеж или None."""
    m = COLLECT_RE.match(line.strip("\r\n"))
    if not m:
        return None
    try:
        published = int(m.group(2))
    except ValueError:
        published = 0
    return m.group(1), published, m.group(3), m.group(4)


def natural_key(s):
    """Сортировка «по-человечески»: Part 2 раньше Part 10."""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", s or "")]


def build_order(entries, mode, existing=None):
    """
    entries:  [(index, published, group, title), …] в порядке, как их
              отдал сайт (порядок галереи).
    mode:     один из ORDER_MODES.
    existing: прошлый результат {index: [group, pos]} — уже выданные
              номера не меняются, новые работы дописываются в конец.
    Возвращает {index: [group, pos]}.
    """
    order = {k: list(v) for k, v in (existing or {}).items()}
    groups = {}
    for seq, (idx, published, group, title) in enumerate(entries):
        if idx in order or any(idx == e[0] for e in groups.get(group, ())):
            continue
        groups.setdefault(group, []).append((idx, published, title, seq))

    for group, items in groups.items():
        if mode == ORDER_MODES[0]:      # по дате
            items.sort(key=lambda e: (e[1] or 0, e[3]))
        elif mode == ORDER_MODES[2]:    # по названию
            items.sort(key=lambda e: (natural_key(e[2]), e[3]))
        else:                           # как в галерее
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
    Переносит скачанное из .incoming: файлы DeviantArt — в папку своей
    галереи под номером «NNN - Название», остальное — в основную папку.
    Возвращает {папка: сколько файлов}.
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
        elif m:      # DeviantArt, но порядок неизвестен
            num, title, ext = int(m.group(2)), m.group(3), m.group(4) or ""
            name = title + (f" ({num})" if num > 1 else "") + ext
            group, target_dir = ".", dest
        else:        # другие сайты — как есть
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
    """Название папки главы для файла или None, если главы в имени нет."""
    m = CHAPTER_RE.search(filename)
    if not m:
        return None
    return f"Chapter {int(m.group(1)):02d}"


def sort_into_chapters(folder):
    """
    Если в одной папке лежат разные главы — разносит их по Chapter NN.
    Файлы без главы в названии остаются на месте. Ничего не удаляет.
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
    """Разбивка по главам в основной папке и в каждой папке галереи."""
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
    """Делит список на n частей по очереди (1-я ссылка → 1-й поток, …)."""
    n = max(1, min(n, len(items)))
    return [items[i::n] for i in range(n)]


def retry_links(failed_names, all_links):
    """
    Какие ссылки повторить. Для DeviantArt номер работы есть в имени файла,
    так что повторяем только нужные. Иначе — весь список (скачанное
    пропустится по архиву).
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
    Временный файл настроек gallery-dl: имена файлов DeviantArt с номером
    работы (для нумерации) и, если задан, логин/пароль. Права 600,
    удаляется после работы — пароль нигде не остаётся.
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
    if browser and browser != NO_BROWSER:
        args += ["--cookies-from-browser", browser]
    return args


def pretty_name(path):
    """Имя скачанного файла для журнала, без служебной приставки."""
    name = os.path.basename(path.strip())
    return re.sub(r"^da\d+_\d+_", "", name)


# --------------------------------------------------------------------------
# Окно
# --------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("840x800")
        self.root.minsize(660, 620)

        self.procs = []
        self.procs_lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.events = queue.Queue()
        self.worker = None
        self.stats = {"done": 0, "skip": 0, "err": 0}
        self.failed = []
        self.counting_skips = True

        cfg = self.load_config()
        self.var_base = tk.StringVar(value=cfg.get("base", str(DEFAULT_BASE)))
        self.var_name = tk.StringVar(value="")
        self.var_browser = tk.StringVar(value=cfg.get("browser", "firefox"))
        if self.var_browser.get() not in BROWSERS:
            self.var_browser.set(NO_BROWSER)
        self.var_user = tk.StringVar(value=cfg.get("username", ""))
        self.var_pass = tk.StringVar(value="")
        self.var_threads = tk.IntVar(value=cfg.get("threads", 3))
        self.var_order = tk.StringVar(value=cfg.get("order", ORDER_MODES[0]))
        if self.var_order.get() not in ORDER_MODES:
            self.var_order.set(ORDER_MODES[0])
        self.var_sort = tk.BooleanVar(value=cfg.get("sort", True))
        self.var_retry = tk.BooleanVar(value=cfg.get("retry", True))
        self.da_connected = bool(cfg.get("da_connected", False))
        self.var_connected = tk.StringVar()
        self.update_connected()
        self.var_status = tk.StringVar(value="Готово к работе")
        self.var_stats = tk.StringVar(value="")

        self.build_ui()
        self.check_gallery_dl()
        self.root.after(100, self.poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- интерфейс ----------

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

        # Ссылки
        frm_links = ttk.LabelFrame(self.root, text=" Ссылки (по одной на строку) ")
        frm_links.pack(fill="x", **pad)
        self.txt_links = tk.Text(frm_links, height=6, wrap="none",
                                 font=("monospace", 10), undo=True)
        self.txt_links.pack(fill="x", padx=6, pady=6)
        ttk.Label(frm_links, text="Папка, глава, вся серия, галерея, профиль или "
                  "отдельная работа. Другие сайты тоже можно.",
                  **grey).pack(anchor="w", padx=6, pady=(0, 6))

        # Куда сохранять
        frm_dest = ttk.LabelFrame(self.root, text=" Куда сохранять ")
        frm_dest.pack(fill="x", **pad)
        frm_dest.columnconfigure(1, weight=1)
        ttk.Label(frm_dest, text="Основная папка:").grid(
            row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(frm_dest, textvariable=self.var_base).grid(
            row=0, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(frm_dest, text="Выбрать…", command=self.choose_base).grid(
            row=0, column=2, padx=6, pady=4)
        ttk.Label(frm_dest, text="Название работы:").grid(
            row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(frm_dest, textvariable=self.var_name).grid(
            row=1, column=1, sticky="ew", padx=6, pady=4)
        ttk.Label(frm_dest, text="(станет подпапкой)", **grey).grid(
            row=1, column=2, sticky="w", padx=6)

        # Вход
        frm_auth = ttk.LabelFrame(self.root, text=" Вход в DeviantArt (для 18+) ")
        frm_auth.pack(fill="x", **pad)
        frm_auth.columnconfigure(1, weight=1)
        frm_auth.columnconfigure(3, weight=1)
        ttk.Label(frm_auth, text="Логин:").grid(
            row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(frm_auth, textvariable=self.var_user).grid(
            row=0, column=1, sticky="ew", padx=6, pady=4)
        ttk.Label(frm_auth, text="Пароль:").grid(
            row=0, column=2, sticky="w", padx=6, pady=4)
        ttk.Entry(frm_auth, textvariable=self.var_pass, show="•").grid(
            row=0, column=3, sticky="ew", padx=6, pady=4)
        ttk.Label(frm_auth, text="Или вход из браузера:").grid(
            row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Combobox(frm_auth, textvariable=self.var_browser, values=BROWSERS,
                     state="readonly", width=20).grid(
            row=1, column=1, sticky="w", padx=6, pady=4)
        self.btn_connect = ttk.Button(frm_auth, text="🔑 Подключить DeviantArt",
                                      command=self.connect_deviantart)
        self.btn_connect.grid(row=1, column=2, columnspan=2, sticky="e",
                              padx=6, pady=4)
        ttk.Label(frm_auth, textvariable=self.var_connected, **grey).grid(
            row=2, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 6))

        # Настройки
        frm_opt = ttk.LabelFrame(self.root, text=" Настройки ")
        frm_opt.pack(fill="x", **pad)
        ttk.Label(frm_opt, text="Порядок номеров:").grid(
            row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Combobox(frm_opt, textvariable=self.var_order, values=ORDER_MODES,
                     state="readonly", width=20).grid(
            row=0, column=1, sticky="w", padx=6, pady=4)
        ttk.Label(frm_opt, text="файлы: 001 - …, 002 - …",
                  **grey).grid(row=0, column=2, sticky="w")
        ttk.Label(frm_opt, text="Скачивать одновременно:").grid(
            row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Spinbox(frm_opt, from_=1, to=MAX_THREADS, width=4, state="readonly",
                    textvariable=self.var_threads).grid(
            row=1, column=1, sticky="w", padx=6, pady=4)
        ttk.Label(frm_opt, text="потоков (3 — оптимально)", **grey).grid(
            row=1, column=2, sticky="w")
        ttk.Checkbutton(
            frm_opt, variable=self.var_retry,
            text="Повторить то, что не скачалось с первого раза"
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=2)
        ttk.Checkbutton(
            frm_opt, variable=self.var_sort,
            text="Если в одной папке несколько глав — разнести по Chapter 01, 02 …"
        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(2, 6))

        # Кнопки
        frm_btn = ttk.Frame(self.root)
        frm_btn.pack(fill="x", **pad)
        self.btn_start = ttk.Button(frm_btn, text="⬇  Скачать",
                                    style="Big.TButton", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(frm_btn, text="■  Стоп",
                                   command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Button(frm_btn, text="Упорядочить файлы",
                   command=self.organize_now).pack(side="left")
        ttk.Button(frm_btn, text="Открыть папку",
                   command=self.open_folder).pack(side="right")

        # Статус
        frm_status = ttk.Frame(self.root)
        frm_status.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(frm_status, mode="indeterminate")
        self.progress.pack(fill="x")
        row = ttk.Frame(frm_status)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, textvariable=self.var_status).pack(side="left")
        ttk.Label(row, textvariable=self.var_stats,
                  font=("TkDefaultFont", 10, "bold")).pack(side="right")

        # Журнал
        frm_log = ttk.LabelFrame(self.root, text=" Журнал ")
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

    # ---------- настройки ----------

    def load_config(self):
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_config(self):
        """Сохраняет настройки. Пароль — никогда."""
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps({
                "base": self.var_base.get(),
                "browser": self.var_browser.get(),
                "username": self.var_user.get().strip(),
                "threads": self.threads(),
                "order": self.var_order.get(),
                "retry": self.var_retry.get(),
                "sort": self.var_sort.get(),
                "da_connected": self.da_connected,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ---------- вспомогательное ----------

    def update_connected(self):
        if self.da_connected:
            self.var_connected.set("✔ DeviantArt подключён — работы 18+ "
                                   "скачиваются в полном качестве.")
        else:
            self.var_connected.set("Для 18+ один раз нажми «Подключить "
                                   "DeviantArt». Пароль не сохраняется.")

    def threads(self):
        try:
            return max(1, min(MAX_THREADS, int(self.var_threads.get())))
        except (tk.TclError, ValueError):
            return 3

    def check_gallery_dl(self):
        self.gdl = find_gallery_dl()
        if self.gdl:
            self.log(f"gallery-dl найден: {self.gdl}", "info")
        else:
            self.log("gallery-dl не найден! Установи: pipx install gallery-dl",
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
        self.var_stats.set(f"Скачано: {s['done']}   Пропущено: {s['skip']}   "
                           f"Ошибок: {s['err']}")

    def set_running(self, running):
        self.btn_connect.configure(state="disabled" if running else "normal")
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        if running:
            self.progress.start(12)
        else:
            self.progress.stop()

    # ---------- кнопки ----------

    def start(self):
        if not self.gdl:
            messagebox.showerror(APP_TITLE, "gallery-dl не найден.\n"
                                 "Установи его: pipx install gallery-dl")
            return
        urls = parse_urls(self.txt_links.get("1.0", "end"))
        if not urls:
            messagebox.showinfo(APP_TITLE, "Вставь хотя бы одну ссылку.")
            return
        user, pwd = self.var_user.get().strip(), self.var_pass.get()
        if bool(user) != bool(pwd):
            messagebox.showinfo(APP_TITLE, "Для входа нужны и логин, и пароль.\n"
                                "Или оставь оба поля пустыми.")
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
        self.log(f"— Начинаю: {len(urls)} ссылок → {dest}", "info")

        self.worker = threading.Thread(
            target=self.run_job,
            args=(urls, dest, self.var_browser.get(), user, pwd,
                  self.threads(), self.var_order.get(),
                  self.var_retry.get(), self.var_sort.get()),
            daemon=True)
        self.worker.start()

    def connect_deviantart(self):
        """Разрешение для gallery-dl от имени аккаунта (OAuth), один раз."""
        if not self.gdl:
            messagebox.showerror(APP_TITLE, "gallery-dl не найден.")
            return
        self.stop_flag.clear()
        self.set_running(True)
        self.var_status.set("Жду разрешения в браузере…")
        self.log("— Подключаю DeviantArt. В браузере откроется страница "
                 "DeviantArt: войди и нажми «Authorize».", "info")
        self.worker = threading.Thread(target=self.run_connect, daemon=True)
        self.worker.start()

    def run_connect(self):
        ok = [False]

        def on_line(line):
            low = line.lower()
            if "been cached" in low:          # токен сохранён
                ok[0] = True
            elif line.startswith("https://www.deviantart.com/oauth2"):
                self.emit("log", ("Если браузер не открылся, открой ссылку "
                                  "вручную:", "info"))
                self.emit("log", (line, "info"))
            elif "[error]" in line or "[warning]" in line:
                self.emit("log", (line, "err"))
            # остальное (в т.ч. сам токен) в журнал не выводим

        try:
            self.run_process(["oauth:deviantart"], on_line)
        except Exception as e:
            self.emit("log", (f"Ошибка: {e}", "err"))
        self.emit("connected", ok[0])

    def stop(self):
        self.stop_flag.set()
        with self.procs_lock:
            for p in self.procs:
                if p.poll() is None:
                    p.terminate()
        self.var_status.set("Останавливаю…")

    def organize_now(self):
        folder = self.dest_folder()
        if not folder.is_dir():
            messagebox.showinfo(APP_TITLE, f"Папки пока нет:\n{folder}")
            return
        self.report_organized(organize(folder, load_order(folder)))
        if self.var_sort.get():
            self.report_sorted(sort_all_chapters(folder))

    def report_organized(self, moved):
        if not moved:
            return
        self.log(f"Разложено по папкам: {sum(moved.values())} файлов", "ok")
        for name in sorted(moved):
            label = "основная папка" if name == "." else name
            self.log(f"   {label}: {moved[name]}", "ok")

    def report_sorted(self, moved):
        if not moved:
            return
        self.log(f"Разнесено по главам: {sum(moved.values())} файлов", "ok")
        for name in sorted(moved):
            self.log(f"   {name}: {moved[name]}", "ok")

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_TITLE,
                                       "Скачивание ещё идёт. Остановить и выйти?"):
                return
            self.stop()
        self.save_config()
        self.root.destroy()

    # ---------- фоновая работа ----------

    def emit(self, kind, payload=None):
        self.events.put((kind, payload))

    def run_process(self, args, on_line):
        """Запускает gallery-dl и построчно передаёт вывод в on_line."""
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
        """Выполняет функции из jobs, не больше n одновременно."""
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
        """Список работ в каждой ссылке: (index, дата, папка, название)."""
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
                self.emit("log", (f"   {group_name(None, url)}: работ "
                                  f"{len(found)}", "info"))
            return job

        self.run_parallel([make_job(i, u) for i, u in enumerate(urls)], n)
        entries = []
        for i in range(len(urls)):
            entries += results.get(i, [])
        return entries

    def download_parallel(self, links, inc, archive, args, n):
        """Делит ссылки на n частей и качает их одновременно."""
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
                self.emit("log", (f"Вход в DeviantArt как {user}", "info"))
            inc.mkdir(parents=True, exist_ok=True)

            # 1. Узнаём, какие работы в каждой ссылке и в каком порядке
            to_expand = [u for u in urls if classify_url(u) == "expand"]
            direct = [u for u in urls if u not in to_expand]
            entries = []
            if to_expand:
                self.emit("status", "Собираю список работ…")
                entries = self.collect(to_expand, args, n_threads)
                if self.stop_flag.is_set():
                    result = "stopped"
                    return
            for u in direct:
                idx = deviation_id(u)
                if idx:
                    entries.append((idx, 0, SINGLE_GROUP, ""))

            order = build_order(entries, order_mode, load_order(dest))
            save_order(dest, order)
            seen = set()
            final = []
            for idx, *_ in entries:
                if idx not in seen:
                    seen.add(idx)
                    final.append(f"https://www.deviantart.com/view/{idx}")
            final += [u for u in direct if not deviation_id(u)]
            self.emit("log", (f"Всего работ: {len(final)}", "info"))
            if not final:
                self.emit("log", ("Нечего скачивать — ссылки не найдены.", "err"))
                result = "error"
                return
            (dest / LINKS_FILE).write_text("\n".join(final) + "\n",
                                           encoding="utf-8")

            # 2. Скачиваем в несколько потоков
            archive = dest / ARCHIVE_FILE
            n = min(n_threads, len(final))
            self.emit("status", f"Скачиваю: {len(final)} ссылок, потоков: {n}…")
            self.download_parallel(final, inc, archive, args, n)
            if self.stop_flag.is_set():
                result = "stopped"
                return

            # 3. Повтор неудачных (один поток, спокойно)
            if retry:
                failed = self.take_failed()
                if failed:
                    again = retry_links(failed, final)
                    self.emit("retry", len(failed))
                    self.emit("status", f"Повторяю: {len(again)} ссылок…")
                    self.download_parallel(again, inc, archive, args, 1)
                    if self.stop_flag.is_set():
                        result = "stopped"
                        return
        except Exception as e:  # чтобы окно не зависло на ошибке
            self.emit("log", (f"Ошибка: {e}", "err"))
            result = "error"
        finally:
            # 4. Раскладываем по порядку — даже после «Стоп»
            try:
                self.emit("organized", organize(dest, load_order(dest)))
                if do_sort:
                    self.emit("sorted", sort_all_chapters(dest))
            except Exception as e:
                self.emit("log", (f"Не удалось упорядочить: {e}", "err"))
            try:
                os.remove(cfg_path)
            except OSError:
                pass
            self.emit("finish", result)

    def take_failed(self):
        """Забирает список неудачных (из фонового потока, через очередь)."""
        box, done = [], threading.Event()
        self.emit("take_failed", (box, done))
        done.wait(5)
        return box

    def handle_line(self, line):
        """Разбор строки вывода gallery-dl и подсчёт статистики."""
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
                self.log("   ↳ Не удалось войти. Проверь логин и пароль. Если "
                         "DeviantArt просит капчу — войди в браузере и выбери "
                         "его в поле «Или вход из браузера».", "err")
        elif "[warning]" in line:
            self.log(line if len(line) < 220 else line[:220] + " …", "err")
            if "cookies" in low and "unable to find" in low:
                self.log("   ↳ В этом браузере нет входа. Выбери другой браузер "
                         "или введи логин и пароль.", "err")
            elif "403" in line:
                self.log("   ↳ Сайт не отдаёт полную картинку. Для 18+ нажми "
                         "«Подключить DeviantArt», потом «Скачать» ещё раз.",
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
                    self.log(f"— Не скачалось {payload}. Пробую ещё раз…", "info")
                    self.failed = []
                    self.stats["err"] = 0
                    self.counting_skips = False
                    self.update_stats()
                elif kind == "finish":
                    self.on_finish(payload)
                elif kind == "connected":
                    self.set_running(False)
                    if payload:
                        self.da_connected = True
                        self.save_config()
                        self.update_connected()
                        self.var_status.set("DeviantArt подключён!")
                        self.log("— Готово! DeviantArt подключён. Теперь можно "
                                 "скачивать, в том числе 18+.", "ok")
                    else:
                        self.var_status.set("Не удалось подключить DeviantArt")
                        self.log("— Подключение не завершилось. Попробуй ещё "
                                 "раз и нажми «Authorize» в браузере.", "err")
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def on_finish(self, result):
        self.set_running(False)
        if result == "stopped":
            self.var_status.set("Остановлено. Нажми «Скачать» — продолжит с места.")
            self.log("— Остановлено. Повторный запуск докачает недостающее.", "info")
            return
        if result == "error":
            self.var_status.set("Ошибка — смотри журнал")
            return
        if self.failed:
            self.var_status.set(f"Готово, но не скачалось: {len(self.failed)}")
            self.log(f"Не скачались ({len(self.failed)}). Обычно это работы "
                     "по подписке или 18+ без входа в аккаунт:", "err")
            for name in self.failed:
                self.log("   " + pretty_name(name), "err")
        else:
            self.var_status.set("Готово! Всё скачалось.")
            self.log("— Готово! Всё скачалось.", "ok")


def main():
    root = tk.Tk(className="art-downloader")   # для значка в панели задач
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
