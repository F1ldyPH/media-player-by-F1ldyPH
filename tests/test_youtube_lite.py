"""Тесты для youtube_lite: сборка команд, выбор плеера, понятные ошибки.

Сеть и реальный mpv не нужны — проверяется только логика формирования плана
воспроизведения (именно здесь легко сломать ограничение кэша или формат).
"""

from __future__ import annotations

import youtube_lite as yl


def test_is_probably_url():
    assert yl.is_probably_url("https://youtu.be/abc")
    assert yl.is_probably_url("www.youtube.com/watch?v=abc")
    assert yl.is_probably_url("youtube.com/watch?v=abc")
    assert not yl.is_probably_url("lofi hip hop radio")
    assert not yl.is_probably_url("deep house mix 2024")


def test_build_target_search():
    assert yl.build_target("lofi hip hop") == "ytsearch1:lofi hip hop"
    assert yl.build_target("  spaced query  ") == "ytsearch1:spaced query"


def test_build_target_strips_playlist_params():
    url = "https://www.youtube.com/watch?v=abc&list=PL123&index=4"
    assert yl.build_target(url, single=True) == "https://www.youtube.com/watch?v=abc"
    # Если плейлист нужен — ссылка не меняется.
    assert yl.build_target(url, single=False) == url


def test_mpv_command_limits_cache():
    cmd = yl.build_mpv_command("mpv", "https://youtu.be/abc", quality="hd", cache_mb=64)
    assert cmd[0] == "mpv"
    assert "--demuxer-max-bytes=64MiB" in cmd
    assert "--cache=yes" in cmd
    assert "--hwdec=auto-safe" in cmd
    assert "--ytdl-format=" + yl.QUALITY_FORMATS["hd"] in cmd
    assert cmd[-1] == "https://youtu.be/abc"


def test_mpv_command_audio_mode():
    cmd = yl.build_mpv_command("mpv", "url", audio_only=True, quality="fhd")
    assert "--no-video" in cmd
    assert "--ytdl-format=" + yl.QUALITY_FORMATS["audio"] in cmd


def test_mpv_raw_options_are_single_argument():
    """Все yt-dlp-опции должны уйти одним --ytdl-raw-options (иначе mpv их теряет)."""
    cmd = yl.build_mpv_command("mpv", "url", playlist_items="1-10")
    raw = [c for c in cmd if c.startswith("--ytdl-raw-options=")]
    assert len(raw) == 1
    assert "playlist-items=1-10" in raw[0]


def test_quality_presets_cover_all_titles():
    assert set(yl.QUALITY_FORMATS) == set(yl.QUALITY_TITLES)
    assert yl.DEFAULT_QUALITY in yl.QUALITY_FORMATS


def test_make_plan_without_player(monkeypatch):
    monkeypatch.setattr(yl, "pick_player", lambda requested=None: None)
    assert yl.make_plan("https://youtu.be/abc") is None


def test_make_plan_mpv(monkeypatch):
    monkeypatch.setattr(yl, "pick_player", lambda requested=None: ("mpv", "/usr/bin/mpv"))
    plan = yl.make_plan("https://youtu.be/abc", quality="low")
    assert plan is not None
    assert plan.player_name == "mpv"
    assert "360p" in plan.description
    assert plan.command[0] == "/usr/bin/mpv"


def test_make_plan_vlc_uses_resolver(monkeypatch):
    monkeypatch.setattr(yl, "pick_player", lambda requested=None: ("vlc", "/usr/bin/vlc"))
    monkeypatch.setattr(yl, "resolve_stream", lambda target, quality, audio_only=False: "http://stream")
    plan = yl.make_plan("https://youtu.be/abc")
    assert plan is not None
    assert plan.command[0] == "/usr/bin/vlc"
    assert plan.command[-1] == "http://stream"


def test_format_diagnostics_mentions_missing_parts(monkeypatch):
    diag = {"python": "3.11", "yt-dlp": "", "mpv": "", "vlc": "", "ffplay": "", "ffmpeg": ""}
    text = yl.format_diagnostics(diag)
    assert "mpv не найден" in text
    assert "yt-dlp не найден" in text
    assert "docs/YOUTUBE_RAM_RU.md" in text


def test_parser_defaults():
    args = yl.build_parser().parse_args(["https://youtu.be/abc"])
    assert args.quality == yl.DEFAULT_QUALITY
    assert args.audio is False
    assert args.playlist is False
