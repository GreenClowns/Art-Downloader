"""Logic tests — no window, no internet: python3 -m pytest"""

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
    for n in ["001 - X - Chapter 1 - Part 1.png", "002 - X - Chapter 2 - Part 1.png",
              "003 - Cover.png"]:
        (mixed / n).touch()
    single = tmp_path / "Tea"
    single.mkdir()
    (single / "001 - Tea - Chapter 3.png").touch()

    result = a.sort_all_chapters(tmp_path)
    assert result == {"All/Chapter 01": 1, "All/Chapter 02": 1}
    assert (mixed / "003 - Cover.png").exists()            # без главы — на месте
    assert (single / "001 - Tea - Chapter 3.png").exists()  # одна глава — не делим


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
