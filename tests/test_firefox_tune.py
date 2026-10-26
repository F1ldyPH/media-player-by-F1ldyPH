"""Тесты firefox_tune: генерация prefs, идемпотентность, откат, выбор профиля."""

from __future__ import annotations

from typing import TYPE_CHECKING

import firefox_tune as ft

if TYPE_CHECKING:
    from pathlib import Path


def test_render_prefs_has_markers_and_valid_syntax():
    text = ft.render_prefs("normal")
    assert ft.BEGIN_MARK in text
    assert ft.END_MARK in text
    assert 'user_pref("media.cache_size", 102400);' in text
    assert "user_pref(" in text
    # никаких boolean-строк в кавычках
    assert 'user_pref("browser.tabs.unloadOnLowMemory", true);' in text


def test_preset_8gb_is_stricter_than_normal():
    normal = ft.render_prefs("normal")
    low = ft.render_prefs("8gb")
    assert 'user_pref("dom.ipc.processCount", 4);' in normal
    assert 'user_pref("dom.ipc.processCount", 2);' in low
    assert 'user_pref("media.cache_size", 65536);' in low


def test_optional_av1_pref_only_when_requested():
    assert "media.av1.enabled" not in ft.render_prefs("normal")
    assert "media.av1.enabled" in ft.render_prefs("normal", include_optional=True)
    assert "media.av1.enabled" in ft.render_prefs("aggressive")


def test_merge_prefs_last_value_wins():
    prefs = ft.merge_prefs(
        [("a", "1", "x"), ("b", "2", "y")],
        [("a", "9", "z")],
    )
    assert [(n, v) for n, v, _ in prefs] == [("a", "9"), ("b", "2")]


def test_strip_block_keeps_foreign_lines():
    foreign = 'user_pref("general.smoothScroll", false);\n'
    text = foreign + "\n" + ft.render_prefs("normal")
    cleaned = ft.strip_block(text)
    assert "general.smoothScroll" in cleaned
    assert "media.cache_size" not in cleaned


def test_apply_is_idempotent_and_backs_up(tmp_path: Path):
    profile = tmp_path / "abc.default-release"
    profile.mkdir()
    user_js = profile / "user.js"
    user_js.write_text('user_pref("browser.startup.page", 3);\n', encoding="utf-8")

    first, backup1 = ft.apply_prefs(profile, "normal")
    assert backup1 is not None and backup1.is_file()
    second, backup2 = ft.apply_prefs(profile, "8gb")
    assert backup2 is not None

    # Второй прогон не должен дублировать блок.
    assert second.count(ft.BEGIN_MARK) == 1
    assert "browser.startup.page" in second          # чужая настройка сохранена
    assert 'user_pref("media.cache_size", 65536);' in second  # пресет обновился
    assert 'user_pref("media.cache_size", 102400);' not in second
    assert first != second


def test_apply_dry_run_does_not_touch_disk(tmp_path: Path):
    profile = tmp_path / "p.default"
    profile.mkdir()
    text, backup = ft.apply_prefs(profile, "normal", dry_run=True)
    assert ft.BEGIN_MARK in text
    assert backup is None
    assert not (profile / "user.js").exists()


def test_read_profiles_relative_and_absolute(tmp_path: Path):
    root = tmp_path / "firefox"
    root.mkdir()
    (root / "profiles.ini").write_text(
        "[Profile0]\nName=default\nIsRelative=1\nPath=abc.default\nDefault=1\n\n"
        "[Profile1]\nName=work\nIsRelative=0\nPath=/opt/ff/work\n\n"
        "[Install4F96D1932A9F858E]\nDefault=abc.default\nLocked=1\n",
        encoding="utf-8",
    )
    profiles = ft.read_profiles(root)
    assert len(profiles) == 2
    assert profiles[0]["path"] == str(root / "abc.default")
    assert profiles[1]["path"] == "/opt/ff/work"
    assert ft.pick_profile(profiles, None)["name"] == "default"
    assert ft.pick_profile(profiles, "work")["name"] == "work"
    assert ft.pick_profile(profiles, "nope") is None


def test_cli_print_runs(capsys):
    assert ft.main(["--print", "--preset", "8gb"]) == 0
    out = capsys.readouterr().out
    assert "media.cache_size" in out
