"""Logic tests — no window, no internet: python3 -m pytest"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import art_downloader as a  # noqa: E402

BY_AUTO, BY_DATE, BY_GALLERY, BY_NAME = a.ORDER_KEYS


@pytest.fixture(autouse=True)
def english():
    a.set_lang("en")
    yield
    a.set_lang("en")


def test_detect_lang():
    assert a.detect_lang({"LANG": "ru_RU.UTF-8"}) == "ru"
    assert a.detect_lang({"LANG": "uk_UA.UTF-8"}) == "ru"
    assert a.detect_lang({"LANG": "sv_SE.UTF-8"}) == "en"
    assert a.detect_lang({"LANGUAGE": "en_US", "LANG": "ru_RU.UTF-8"}) == "en"
    assert a.detect_lang({}, windows_lang_id=0) == "en"
    # Windows UI language ids: 0x0419 ru-RU, 0x0422 uk-UA, 0x0409 en-US
    assert a.detect_lang({}, windows_lang_id=0x0419) == "ru"
    assert a.detect_lang({}, windows_lang_id=0x0422) == "ru"
    assert a.detect_lang({}, windows_lang_id=0x0409) == "en"


def test_safe_name_windows_characters():
    assert a.safe_name('Chapter 1: "Start"?', windows=True) == "Chapter 1- -Start--"
    assert a.safe_name("Chapter 1: Start", windows=False) == "Chapter 1: Start"
    assert a.safe_name("name. ", windows=True) == "name"


def test_find_gallery_dl_in_exe(monkeypatch):
    monkeypatch.setattr(a, "FROZEN", True)
    assert a.find_gallery_dl() == [sys.executable, a.GALLERY_DL_FLAG]


def test_tr_and_labels():
    assert a.tr("Привет", "Hello") == "Hello"
    assert a.order_label("date") == "by publication date"
    a.set_lang("ru")
    assert a.tr("Привет", "Hello") == "Привет"
    assert a.order_label("date") == "по дате публикации"
    assert a.browser_label("none") == "не брать из браузера"
    assert a.browser_label("firefox") == "firefox"


def test_files_word():
    assert [a.files_word(n) for n in (1, 2, 5)] == ["1 file", "2 files", "5 files"]
    a.set_lang("ru")
    assert [a.files_word(n) for n in (1, 3, 5, 11, 21, 22, 112)] == [
        "1 файл", "3 файла", "5 файлов", "11 файлов", "21 файл", "22 файла",
        "112 файлов"]


def test_migrate_old_russian_settings():
    old = {"order": "по названию", "browser": "не брать из браузера", "threads": 4}
    new = a.migrate_config(old)
    assert new["order"] == "name"
    assert new["browser"] == "none"
    assert new["threads"] == 4
    assert a.migrate_config({})["order"] == "auto"
    # the old default "date" becomes "auto" once; a later choice is kept
    assert a.migrate_config({"order": "date"})["order"] == "auto"
    assert a.migrate_config({"order": "date", "settings_version": 2})["order"] == "date"


def test_classify_url():
    assert a.classify_url(
        "https://www.deviantart.com/al3ksgts/gallery/96690616/chapter-1---kanojo"
    ) == "expand"
    assert a.classify_url("https://www.deviantart.com/al3ksgts") == "expand"
    assert a.classify_url("https://www.deviantart.com/view/1197685682") == "direct"
    assert a.classify_url("https://www.pixiv.net/en/artworks/1") == "direct"


def test_deviation_id():
    assert a.deviation_id("https://www.deviantart.com/view/1381263216") == "1381263216"
    assert a.deviation_id(
        "https://www.deviantart.com/vangelix/art/A-Special-Tea---228-1381263216"
    ) == "1381263216"
    assert a.deviation_id("https://www.pixiv.net/en/artworks/1") is None


def test_parse_urls_dedup_and_order():
    text = "https://b x\n  https://a \nhttps://b\nне ссылка"
    assert a.parse_urls(text) == ["https://b", "https://a"]


def test_group_name():
    assert a.group_name("Chapter 1 - Kanojo", "https://x") == "Chapter 1 - Kanojo"
    assert a.group_name(
        "None", "https://www.deviantart.com/vangelix/gallery/77/a-special-tea"
    ) == "a special tea"
    assert a.group_name(
        "None", "https://www.deviantart.com/vangelix/gallery/all"
    ) == "vangelix"
    assert "/" not in a.group_name("A/B", "https://x")


def test_parse_collect_line():
    line = "@@123\t1700000000\tChapter 1 - Kanojo\tTitle\twith tab"
    assert a.parse_collect_line(line) == (
        "123", 1700000000, "Chapter 1 - Kanojo", "Title\twith tab")
    assert a.parse_collect_line("[deviantart][info] something") is None


def positions(order):
    return {idx: v[1] for idx, v in order.items()}


def test_build_order_modes():
    entries = [  # as the site returned them (newest first)
        ("3", 30, "G", "Part 10"),
        ("2", 20, "G", "Part 9"),
        ("1", 10, "G", "Part 2"),
    ]
    assert positions(a.build_order(entries, BY_DATE)) == {"1": 1, "2": 2, "3": 3}
    assert positions(a.build_order(entries, BY_GALLERY)) == {"3": 1, "2": 2, "1": 3}
    assert positions(a.build_order(entries, BY_NAME)) == {"1": 1, "2": 2, "3": 3}


def test_auto_mode_uses_title_numbers_for_comics():
    # parts published out of order, like "A Special Tea"
    entries = [("a", 100, "Tea", " A Special Tea - 19"),
               ("b", 101, "Tea", " A Special Tea - 20"),
               ("c", 50, "Tea", " A Special Tea - 64"),
               ("d", 300, "Tea", " A Special Tea - 21")]
    order = a.build_order(entries, BY_AUTO)
    assert positions(order) == {"a": 1, "b": 2, "d": 3, "c": 4}
    assert order["a"][3] == "A Special Tea - 19"          # spaces tidied


def test_auto_mode_falls_back_to_date_without_numbers():
    entries = [("x", 2, "G", "Sunset"), ("y", 1, "G", "Morning"),
               ("z", 3, "G", "Night")]
    assert positions(a.build_order(entries, BY_AUTO)) == {"y": 1, "x": 2, "z": 3}


def test_new_work_is_inserted_in_the_middle():
    old = a.build_order([("p1", 1, "G", "Part 1"), ("p3", 3, "G", "Part 3")], BY_AUTO)
    new = a.build_order([("p2", 9, "G", "Part 2")], BY_AUTO, old)
    assert positions(new) == {"p1": 1, "p2": 2, "p3": 3}


def test_reorder_renames_existing_files(tmp_path):
    """The user's case: downloaded by date, then re-sorted by title number."""
    tea = tmp_path / "Tea"
    tea.mkdir()
    # old-format .order.json (no titles) and files numbered by date
    old = {"i19": ["Tea", 1], "i20": ["Tea", 2], "i64": ["Tea", 3],
           "i65": ["Tea", 4], "i21": ["Tea", 5]}
    a.save_order(tmp_path, old)
    for name in ["001 -  A Special Tea - 19.jpg", "002 -  A Special Tea - 20.jpg",
                 "003 -  A Special Tea - 64.jpg", "003-02 -  A Special Tea - 64.jpg",
                 "004 -  A Special Tea - 65.jpg", "005 -  A Special Tea - 21.jpg"]:
        (tea / name).write_text(name)

    assert a.reorder(tmp_path, BY_AUTO) == 6
    assert sorted(f.name for f in tea.iterdir()) == [
        "001 - A Special Tea - 19.jpg", "002 - A Special Tea - 20.jpg",
        "003 - A Special Tea - 21.jpg", "004 - A Special Tea - 64.jpg",
        "004-02 - A Special Tea - 64.jpg", "005 - A Special Tea - 65.jpg"]
    # content followed its file
    assert (tea / "004-02 - A Special Tea - 64.jpg").read_text().startswith("003-02")
    saved = a.load_order(tmp_path)
    assert saved["i21"][1] == 3 and saved["i64"][1] == 4
    assert a.reorder(tmp_path, BY_AUTO) == 0             # already in order


def test_renumber_inside_chapter_folders(tmp_path):
    g = tmp_path / "Series"
    (g / "Chapter 01").mkdir(parents=True)
    (g / "Chapter 01" / "002 - X - Chapter 1 - Part 1.png").touch()
    (g / "Chapter 01" / "001 - X - Chapter 1 - Part 2.png").touch()
    a.save_order(tmp_path, {"p2": ["Series", 1, 0, "X - Chapter 1 - Part 2", 0],
                            "p1": ["Series", 2, 0, "X - Chapter 1 - Part 1", 1]})
    a.reorder(tmp_path, BY_NAME)
    assert sorted(f.name for f in (g / "Chapter 01").iterdir()) == [
        "001 - X - Chapter 1 - Part 1.png", "002 - X - Chapter 1 - Part 2.png"]


def test_organize_numbers_and_multi_images(tmp_path):
    inc = tmp_path / a.INCOMING
    inc.mkdir()
    for name in ["da1_01_Part 1.png", "da2_01_Part 2.png", "da2_02_Part 2.png",
                 "da7_01_Unknown.jpg", "pixiv_55.jpg", "da1_01_half.png.part"]:
        (inc / name).touch()
    order = {"1": ["Ch 1", 1], "2": ["Ch 1", 2, 0, "Part 2", 1]}
    moved = a.organize(tmp_path, order)
    names = sorted(p.name for p in (tmp_path / "Ch 1").iterdir())
    assert names == ["001 - Part 1.png", "002 - Part 2.png", "002-02 - Part 2.png"]
    assert (tmp_path / "Unknown.jpg").exists()
    assert (tmp_path / "pixiv_55.jpg").exists()
    assert (inc / "da1_01_half.png.part").exists()   # недокачанное не трогаем
    assert moved == {"Ch 1": 3, ".": 2}


def test_chapter_split_only_when_mixed(tmp_path):
    mixed = tmp_path / "All"
    mixed.mkdir()
    for n in ["001 - X - Chapter 1 - Part 1.png", "002 - X - Chapter 1 - Part 2.png",
              "003 - X - Chapter 2 - Part 1.png", "004 - X - Chapter 2 - Part 2.png",
              "005 - Cover.png"]:
        (mixed / n).touch()
    single = tmp_path / "Tea"
    single.mkdir()
    (single / "001 - Tea - Chapter 3.png").touch()

    result = a.sort_all_chapters(tmp_path)
    assert result == {"All/Chapter 01": 2, "All/Chapter 02": 2}
    assert (mixed / "005 - Cover.png").exists()            # no chapter: stays
    assert (single / "001 - Tea - Chapter 3.png").exists()  # one chapter: no split


def test_one_file_per_chapter_is_not_split(tmp_path):
    hc = tmp_path / "Height Charts"
    hc.mkdir()
    for c in range(1, 5):
        (hc / f"00{c} - Height Chart - Chapter {c}.png").touch()
    assert a.sort_all_chapters(tmp_path) == {}
    assert len(list(hc.glob("*.png"))) == 4


def test_new_file_joins_existing_chapter_folder_and_duplicates_vanish(tmp_path):
    ch = tmp_path / "Mei" / "Chapter 02"
    ch.mkdir(parents=True)
    (ch / "006 - Mei Chapter 2 - Page 1.png").write_text("same")
    inc = tmp_path / a.INCOMING
    inc.mkdir()
    (inc / "da6_01_Mei Chapter 2 - Page 1.png").write_text("same")   # re-download
    (inc / "da7_01_Mei Chapter 2 - Page 2.png").write_text("new")
    order = {"6": ["Mei", 6], "7": ["Mei", 7]}
    a.organize(tmp_path, order)
    assert sorted(p.name for p in ch.iterdir()) == [
        "006 - Mei Chapter 2 - Page 1.png", "007 - Mei Chapter 2 - Page 2.png"]
    assert not list((tmp_path / "Mei").glob("*.png"))       # nothing left outside


def test_chapter_folder_regex():
    assert a.chapter_folder("x - Chapter 3 - Part 1.png") == "Chapter 03"
    assert a.chapter_folder("my_kanojo_chapter_12.png") == "Chapter 12"
    assert a.chapter_folder("Глава 5.jpg") == "Chapter 05"
    assert a.chapter_folder("Deep 3.png") is None
    assert a.chapter_folder("Christmas 3.png") is None


def test_retry_links():
    assert a.retry_links(["da333_01_x.png"], ["u"]) == [
        "https://www.deviantart.com/view/333"]
    assert a.retry_links(["pixiv_1.png"], ["u1", "u2"]) == ["u1", "u2"]


def test_run_config_has_no_password_without_login():
    import json
    import os
    path = a.make_run_config()
    try:
        data = json.loads(Path(path).read_text())
        assert "password" not in data["extractor"]["deviantart"]
        assert data["output"]["stdout"] == "utf-8"
        if not a.WINDOWS:   # Windows has no Unix permission bits
            assert oct(os.stat(path).st_mode)[-3:] == "600"
    finally:
        os.remove(path)


# ---------------------------------------------------------------- security

def test_site_folder_names_cannot_escape():
    for evil in ["..", "../../etc", "a/../b", "/abs", "..\\..\\win", "C:\\x"]:
        for windows in (True, False):
            name = a.safe_name(evil, windows=windows)
            assert a.is_safe_component(name, windows=windows), (evil, windows, name)
            assert "/" not in name and "\\" not in name and name != ".."


def test_windows_reserved_names():
    assert a.safe_name("CON", windows=True) == "CON_"
    assert a.safe_name("nul.txt", windows=True) == "nul.txt_"
    assert a.safe_name("com1", windows=False) == "com1_"
    assert a.safe_name("Console", windows=True) == "Console"


def test_tampered_order_file_cannot_write_outside(tmp_path):
    dest = tmp_path / "dest"
    (dest / a.INCOMING).mkdir(parents=True)
    (dest / a.INCOMING / "da5_01_evil.png").write_text("x")
    (dest / a.ORDER_FILE).write_text(
        '{"5": ["../../outside", 1, 0, "evil", 0], "6": ["ok", 1]}')
    order = a.load_order(dest)
    assert "5" not in order and order["6"] == ["ok", 1]
    # even if a bad entry is passed directly, nothing lands outside
    a.organize(dest, {"5": ["../../outside", 1, 0, "evil", 0]})
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path.parent / "outside").exists()
    assert (dest / "evil.png").exists()


def test_order_file_garbage_is_ignored(tmp_path):
    for junk in ['[1, 2]', '{"": ["G", 1]}', '{"1": ["G", "1"]}',
                 '{"1": [null, 1]}', 'not json']:
        (tmp_path / a.ORDER_FILE).write_text(junk)
        assert a.load_order(tmp_path) == {}


def test_organize_never_overwrites_a_different_file(tmp_path):
    inc = tmp_path / a.INCOMING
    inc.mkdir()
    (tmp_path / "G").mkdir()
    (tmp_path / "G" / "001 - Part 1.png").write_text("old, different")
    (inc / "da1_01_Part 1.png").write_text("new")
    a.organize(tmp_path, {"1": ["G", 1]})
    assert (tmp_path / "G" / "001 - Part 1.png").read_text() == "old, different"
    assert (tmp_path / "G" / "001 - Part 1 (2).png").read_text() == "new"


def test_drive_letter_names_unsafe_on_windows():
    assert not a.is_safe_component("C:x", windows=True)
    assert a.is_safe_component("C:x", windows=False)


def test_risky_downloads_are_reported(tmp_path):
    inc = tmp_path / a.INCOMING
    inc.mkdir()
    (inc / "setup.exe").write_text("x")
    (inc / "picture.png").write_text("x")
    risky = []
    a.organize(tmp_path, {}, risky)
    assert risky == ["setup.exe"]


# ------------------------------------------------- reading order, folders

def test_auto_handles_chapter_and_page_numbers():
    # page 3 of chapter 2 was re-uploaded much later; covers named differently
    entries = [("c1", 10, "M", "Mei Chapter 1 Cover"),
               ("p11", 11, "M", "Mei Chapter 1 - Page 1"),
               ("p12", 12, "M", "Mei Chapter 1 - Page 2"),
               ("c2", 20, "M", "Mei Chapter 2 Cover"),
               ("p21", 21, "M", "Mei Chapter 2 - Page 1"),
               ("p22", 999, "M", "Mei Chapter 2 - Page 2"),
               ("p210", 30, "M", "Mei Chapter 2 - Page 10")]
    order = a.build_order(entries, BY_AUTO)
    assert sorted(order, key=lambda i: order[i][1]) == [
        "c1", "p11", "p12", "c2", "p21", "p22", "p210"]


def test_auto_keeps_date_order_when_numbers_do_not_differ():
    entries = [("m%d" % i, i, "Cal", f"Calendar - Month {m} 2025")
               for i, m in enumerate(["Jan", "Feb", "Mar"])]
    order = a.build_order(entries, BY_AUTO)
    assert [order[f"m{i}"][1] for i in range(3)] == [1, 2, 3]


def test_work_goes_to_the_most_specific_folder():
    entries = [("1", 1, "al3ksgts", "Part 1", False),      # whole gallery
               ("1", 1, "Chapter 1", "Part 1", True)]      # its chapter
    assert a.build_order(entries, BY_AUTO)["1"][0] == "Chapter 1"
    # once in its chapter folder, a later "whole gallery" link keeps it there
    first = a.build_order([("1", 1, "Chapter 1", "Part 1", True)], BY_AUTO)
    again = a.build_order([("1", 1, "al3ksgts", "Part 1", False)], BY_AUTO, first)
    assert again["1"][0] == "Chapter 1"


def test_renumber_moves_files_to_their_new_folder(tmp_path):
    (tmp_path / "al3ksgts").mkdir()
    (tmp_path / "al3ksgts" / "001 - Part 1.png").write_text("p1")
    (tmp_path / "al3ksgts" / "002 - Other.png").write_text("o")
    old = {"1": ["al3ksgts", 1, 0, "Part 1", 0, False],
           "2": ["al3ksgts", 2, 0, "Other", 1, False]}
    new = a.build_order([("1", 0, "Chapter 1", "Part 1", True)], BY_AUTO, old)
    a.renumber(tmp_path, old, new)
    assert (tmp_path / "Chapter 1" / "001 - Part 1.png").read_text() == "p1"
    assert (tmp_path / "al3ksgts" / "001 - Other.png").read_text() == "o"


def test_progress_lines_are_recognized():
    assert a.PROGRESS_RE.match("[3/10] https://www.deviantart.com/view/1")
    assert not a.PROGRESS_RE.match("[download][error] Failed to download x")


# ------------------------------------- notifications, export, updates

def test_notify_commands():
    argv, env = a.notify_command("T", 'say "hi"', platform="mac")
    assert argv[0] == "osascript" and '\\"hi\\"' in argv[2]
    argv, env = a.notify_command("Title", "<b>&", platform="windows")
    assert argv[0] == "powershell" and env == {"ARTDL_TITLE": "Title", "ARTDL_BODY": "<b>&"}
    assert "<b>&" not in " ".join(argv)            # text never inside the script
    linux = a.notify_command("T", "-rf body", platform="linux")
    if linux:                                       # only if notify-send exists
        assert linux[0][-3:] == ["--", "T", "-rf body"]


def test_versions():
    assert a.parse_version("v1.32.18") == (1, 32, 18)
    assert a.is_newer("1.33.0", "1.32.18") and not a.is_newer("v1.0.0", "1.0.0")
    assert a.is_newer("1.10.0", "1.9.9")
    assert a.parse_version("garbage") == ()


def test_update_methods():
    venv = [sys.executable, "-m", "gallery_dl"]
    assert a.update_method(venv, frozen=True) == "app"
    assert a.update_method(venv, frozen=False, in_venv=True) == "pip"
    assert a.update_command("pip")[-2:] == ["--upgrade", "gallery-dl"]
    pipx_path = [str(Path.home() / ".local" / "bin" / "gallery-dl")]
    assert a.update_method(pipx_path, frozen=False, in_venv=False, pipx="/usr/bin/pipx") == "pipx"
    assert a.update_method(["/usr/bin/gallery-dl"], frozen=False, in_venv=False,
                           pipx=None) == "manual"
    assert a.update_command("manual") is None


def _pages(folder, names, fmt="PNG"):
    from PIL import Image
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        Image.new("RGB", (40, 60), (200, 30, 30)).save(folder / n, fmt)


def test_export_plan_and_books(tmp_path):
    import zipfile
    _pages(tmp_path / "Tea", ["010 - Tea - 10.png", "002 - Tea - 2.png"])
    _pages(tmp_path / "Mei" / "Chapter 02", ["003 - Mei 2 - Page 1.png"])
    _pages(tmp_path / "Mei" / "Chapter 01", ["001 - Mei 1 Cover.png",
                                             "002 - Mei 1 - Page 1.jpg"], "JPEG")
    (tmp_path / "Mei" / "notes.txt").write_text("x")
    plan = a.export_plan(tmp_path)
    assert [(n, [p.name for p in ps]) for n, ps in plan] == [
        ("Mei - Chapter 01", ["001 - Mei 1 Cover.png", "002 - Mei 1 - Page 1.jpg"]),
        ("Mei - Chapter 02", ["003 - Mei 2 - Page 1.png"]),
        ("Tea", ["002 - Tea - 2.png", "010 - Tea - 10.png"])]
    made = a.export_books(tmp_path, "cbz")
    assert [m.name for m in made] == ["Mei - Chapter 01.cbz", "Mei - Chapter 02.cbz", "Tea.cbz"]
    with zipfile.ZipFile(made[2]) as z:
        assert z.namelist() == ["001.png", "002.png", "ComicInfo.xml"]
        assert z.read("002.png") == (tmp_path / "Tea" / "010 - Tea - 10.png").read_bytes()
    pdf = a.export_books(tmp_path, "pdf")[0].read_bytes()
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    assert pdf.count(b"/Type /Page ") == 2
    jpg = (tmp_path / "Mei" / "Chapter 01" / "002 - Mei 1 - Page 1.jpg").read_bytes()
    assert jpg in pdf                               # JPEG copied without loss
    # the export folder is never treated as a gallery
    assert a.sort_all_chapters(tmp_path) == {}
    assert [n for n, _ in a.export_plan(tmp_path)][-1] == "Tea"
    assert a.safe_name("_Export") == "_Export_"


def test_latest_app_release(monkeypatch):
    monkeypatch.setattr(a, "fetch_json", lambda url, timeout=10: {
        "tag_name": "v1.1.0", "html_url": "https://github.com/x/releases/tag/v1.1.0"})
    assert a.latest_app_release() == ("v1.1.0", "https://github.com/x/releases/tag/v1.1.0")

    def offline(url, timeout=10):
        raise OSError("no network")
    monkeypatch.setattr(a, "fetch_json", offline)
    assert a.latest_app_release() is None


# ------------------------------------------------ Linux AppImage menu entry

def test_desktop_quote():
    assert a.desktop_quote("/home/u/Apps/Art.AppImage") == "/home/u/Apps/Art.AppImage"
    q = a.desktop_quote('/home/u/My Apps/A "b" $X 50%.AppImage')
    assert q == '"/home/u/My Apps/A \\\\"b\\\\" \\\\$X 50%%.AppImage"'


def test_menu_entry_install_sync_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    first = str(tmp_path / "Загрузки" / "My Apps" / "ArtDownloader.AppImage")
    desktop = a.install_menu_entry(first)
    text = desktop.read_text(encoding="utf-8")
    assert a.menu_entry_target() == first
    assert f'Exec="{first}"' in text and f'"{first}" {a.REMOVE_MENU_FLAG}' in text
    assert (tmp_path / "share/icons/hicolor/256x256/apps/art-downloader.png").exists()
    assert a.sync_menu_entry(first) is False             # same place: nothing to do
    moved = str(tmp_path / "Apps" / "Art.AppImage")
    assert a.sync_menu_entry(moved) is True and a.menu_entry_target() == moved
    a.remove_menu_entry()
    assert a.menu_entry_target() is None and not desktop.exists()
    assert a.sync_menu_entry(moved) is False             # never re-adds by itself


def test_entry_from_install_sh_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    d = tmp_path / "applications"
    d.mkdir()
    (d / "art-downloader.desktop").write_text("[Desktop Entry]\nExec=python3 x.py\n")
    assert a.sync_menu_entry("/somewhere/Art.AppImage") is False
    assert "python3 x.py" in (d / "art-downloader.desktop").read_text()


def test_host_env_is_restored(monkeypatch):
    monkeypatch.setattr(a, "FROZEN", True)
    monkeypatch.setattr(a, "WINDOWS", False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundle/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib")
    a.restore_host_env()
    assert os.environ["LD_LIBRARY_PATH"] == "/usr/local/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in os.environ
    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundle/lib")
    a.restore_host_env()                                  # no original → removed
    assert "LD_LIBRARY_PATH" not in os.environ


def test_bundled_gallery_dl_starts_as_independent_program(monkeypatch):
    monkeypatch.setattr(a, "FROZEN", True)
    assert a.gdl_env()["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    monkeypatch.setattr(a, "FROZEN", False)
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in a.gdl_env()
