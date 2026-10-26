#!/usr/bin/env python3
"""
youtube_lite.py
---------------
Лёгкий просмотр YouTube без браузера — специально для слабых/забитых ПК.

Зачем это нужно
---------------
Одна вкладка YouTube в Chrome/Edge/Firefox — это 1–4 ГБ оперативной памяти:
браузер держит отдельный процесс под вкладку, отдельные процессы под рекламу и
трекеры, плюс кэш декодированного видео (при 1080p это десятки-сотни мегабайт
на секунду буфера). mpv с yt-dlp играет тот же ролик за ~150–400 МБ и не тянет
за собой ни рекламу, ни скрипты, ни фоновые вкладки.

Что делает скрипт
-----------------
1. Берёт ссылку на видео, плейлист или просто поисковый запрос.
2. Отдаёт её в mpv (или VLC/ffplay, если mpv нет) — поток вытаскивает yt-dlp.
3. Ограничивает буфер плеера (по умолчанию 96 МБ вместо «сколько дадут»),
   включает аппаратное декодирование и не даёт плееру кэшировать лишнее.

Использование (кратко)
----------------------
    python youtube_lite.py https://youtu.be/VIDEO_ID
    python youtube_lite.py "lofi hip hop"              # поиск и первое совпадение
    python youtube_lite.py URL --quality low           # 360p — минимум памяти
    python youtube_lite.py URL --audio                 # только звук (≈100 МБ)
    python youtube_lite.py --gui                       # маленькое окно-пульт
    python youtube_lite.py --check                     # что установлено в системе
    python youtube_lite.py URL --print-command         # показать команду, не запускать

Зависимости
-----------
* обязательная: ``yt-dlp``  →  ``pip install yt-dlp``  (или ``winget install yt-dlp``)
* плеер: ``mpv`` (лучший вариант, ~200 МБ RAM) → ``winget install mpv``
  либо уже установленный VLC / ffplay.
* ``ffmpeg`` нужен yt-dlp для склейки видео+звука и для ffplay.

Если ничего не установлено, скрипт не падает, а печатает точные команды
установки для Windows (winget) — см. также ``--check``.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

APP_NAME = "YouTube Lite"

# Пресеты качества. Ключ — то, что пишет пользователь, значение — селектор
# формата yt-dlp. Чем меньше разрешение, тем меньше и буфер, и декодер: на
# 360p mpv спокойно живёт в 150–250 МБ.
QUALITY_FORMATS: dict[str, str] = {
    "audio": "bestaudio/best",
    "low": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
    "sd": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "hd": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "fhd": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
}

QUALITY_TITLES: dict[str, str] = {
    "audio": "только звук (минимум RAM)",
    "low": "360p (минимум RAM для видео)",
    "sd": "480p",
    "hd": "720p (рекомендуется)",
    "fhd": "1080p",
}

DEFAULT_QUALITY = "hd"

# Сколько мегабайт разрешено держать в буфере плеера. Значения намеренно
# скромные: mpv по умолчанию может занять под кэш куда больше, а на слабой
# машине именно кэш и «съедает» память при перемотке длинного ролика.
DEFAULT_CACHE_MB = 96
DEFAULT_BACK_CACHE_MB = 24

# Где искать плееры, если их нет в PATH (типичные места Windows).
PLAYER_HINTS: dict[str, tuple[str, ...]] = {
    "mpv": (
        r"C:\Program Files\mpv\mpv.exe",
        r"C:\Program Files\MPV Player\mpv.exe",
        r"C:\Program Files (x86)\mpv\mpv.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\mpv\mpv.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\mpv.exe"),
    ),
    "vlc": (
        r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    ),
    "ffplay": (
        r"C:\ffmpeg\bin\ffplay.exe",
        r"C:\Program Files\ffmpeg\bin\ffplay.exe",
    ),
}

INSTALL_HINTS = {
    "mpv": "winget install --id=mpv.net  (или скачайте mpv с mpv.io и добавьте в PATH)",
    "yt-dlp": "pip install yt-dlp   или   winget install yt-dlp",
    "vlc": "winget install VideoLAN.VLC",
    "ffmpeg": "winget install Gyan.FFmpeg",
}


# --------------------------------------------------------------------------- #
#  Поиск плееров и yt-dlp
# --------------------------------------------------------------------------- #
def find_executable(name: str) -> str | None:
    """Найти исполняемый файл: сначала PATH, потом типичные папки Windows."""
    found = shutil.which(name)
    if found:
        return found
    for candidate in PLAYER_HINTS.get(name, ()):  # Windows-подсказки
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def has_ytdlp_module() -> bool:
    """Есть ли yt-dlp как Python-модуль (нужен для резолва ссылок без mpv)."""
    try:
        import yt_dlp  # noqa: F401
    except Exception:
        return False
    return True


def has_ytdlp() -> bool:
    return bool(find_executable("yt-dlp")) or has_ytdlp_module()


def pick_player(requested: str | None = None) -> tuple[str, str] | None:
    """Вернуть (имя, путь) лучшего доступного плеера."""
    if requested:
        path = find_executable(requested)
        return (requested, path) if path else None
    for name in ("mpv", "vlc", "ffplay"):
        path = find_executable(name)
        if path:
            return name, path
    return None


# --------------------------------------------------------------------------- #
#  Формирование команды
# --------------------------------------------------------------------------- #
def is_probably_url(target: str) -> bool:
    """Ссылка это или поисковый запрос."""
    t = target.strip().lower()
    return t.startswith(("http://", "https://", "www.", "youtu.be/")) or (
        "." in t.split("/")[0] and " " not in t
    )


def build_target(target: str, single: bool = False) -> str:
    """Ссылка как есть; свободный текст превращаем в поиск yt-dlp."""
    target = target.strip()
    if is_probably_url(target):
        if single and "list=" in target and "watch?" in target:
            # Убираем &list=... чтобы не тянуть весь плейлист по клику на видео.
            head, _, _ = target.partition("&list=")
            return head
        return target
    return f"ytsearch1:{target}"


def build_mpv_command(
    player_path: str,
    target: str,
    quality: str = DEFAULT_QUALITY,
    audio_only: bool = False,
    cache_mb: int = DEFAULT_CACHE_MB,
    back_cache_mb: int = DEFAULT_BACK_CACHE_MB,
    playlist_items: str | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Собрать argv для mpv с жёстко ограниченным кэшем."""
    fmt = QUALITY_FORMATS["audio"] if audio_only else QUALITY_FORMATS[quality]
    # yt-dlp-опции копим в одну строку: mpv принимает --ytdl-raw-options
    # списком key=value через запятую.
    raw_options = ["no-warnings="]
    if playlist_items:
        raw_options.append(f"playlist-items={playlist_items}")
    cmd = [
        player_path,
        f"--title={APP_NAME}",
        "--cache=yes",
        f"--demuxer-max-bytes={cache_mb}MiB",
        f"--demuxer-max-back-bytes={back_cache_mb}MiB",
        "--cache-pause=yes",
        "--cache-pause-wait=1",
        "--hwdec=auto-safe",
        f"--ytdl-format={fmt}",
        f"--ytdl-raw-options={','.join(raw_options)}",
        "--save-position-on-quit",
        "--osc=yes",
        "--volume=100",
        "--autofit=1280x760",
        "--geometry=1280x720",
    ]
    if audio_only:
        cmd += ["--no-video", "--force-window=yes", "--geometry=620x160"]
    if extra_args:
        cmd += extra_args
    cmd.append(target)
    return cmd


def build_external_command(
    player_name: str,
    player_path: str,
    stream_url: str,
    audio_only: bool = False,
    cache_mb: int = DEFAULT_CACHE_MB,
) -> list[str]:
    """argv для VLC/ffplay — им нужен уже готовый прямой URL от yt-dlp."""
    if player_name == "vlc":
        cmd = [player_path, "--quiet", "--no-video-title-show"]
        if audio_only:
            cmd.append("--no-video")
        cmd.append(stream_url)
        return cmd
    # ffplay
    cmd = [player_path, "-hide_banner", "-loglevel", "warning", "-infbuf"]
    if audio_only:
        cmd.append("-vn")
    cmd += ["-window_title", APP_NAME, stream_url]
    return cmd


def resolve_stream(target: str, quality: str, audio_only: bool = False) -> str:
    """
    Получить прямой URL потока через yt-dlp (для VLC/ffplay).

    Берём прогрессивный формат (видео+звук в одном файле) — так плееру не
    нужен ffmpeg для склейки, а памяти тратится меньше всего.
    """
    try:
        import yt_dlp
    except Exception as exc:  # pragma: no cover - зависит от окружения
        raise RuntimeError(
            "Для VLC/ffplay нужен Python-модуль yt-dlp: pip install yt-dlp"
        ) from exc

    if audio_only:
        fmt = "bestaudio/best"
    else:
        height = {"low": 360, "sd": 480, "hd": 720, "fhd": 1080}.get(quality, 720)
        # [acodec!=none] — только прогрессивные форматы, где звук уже внутри.
        fmt = (
            f"best[height<={height}][acodec!=none][vcodec!=none]/"
            f"best[height<={height}]/best"
        )
    opts = {
        "format": fmt,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
    if not info:
        raise RuntimeError("yt-dlp не смог разобрать ссылку")
    if info.get("entries"):
        info = next(iter(info["entries"]), None) or {}
    url = info.get("url")
    if not url:
        raise RuntimeError("yt-dlp не вернул прямой URL потока")
    return url


# --------------------------------------------------------------------------- #
#  Диагностика
# --------------------------------------------------------------------------- #
def collect_diagnostics() -> dict[str, str]:
    """Что установлено в системе — для `--check` и понятных ошибок."""
    result: dict[str, str] = {}
    result["python"] = sys.version.split()[0]
    for name in ("yt-dlp", "mpv", "vlc", "ffplay", "ffmpeg"):
        path = find_executable(name)
        result[name] = path or ""
    ytdlp_module = has_ytdlp_module()
    result["yt-dlp (python)"] = "есть" if ytdlp_module else ""
    if ytdlp_module or result["yt-dlp"]:
        try:
            import yt_dlp

            result["yt-dlp version"] = yt_dlp.version.__version__
        except Exception:
            result["yt-dlp version"] = "?"
    return result


def format_diagnostics(diag: dict[str, str]) -> str:
    lines = [f"{APP_NAME} — проверка окружения", ""]
    for key, value in diag.items():
        mark = "OK  " if value else "--  "
        lines.append(f"  {mark}{key:<16}{value}")
    lines.append("")
    if not diag.get("mpv"):
        lines.append(f"  mpv не найден — поставьте: {INSTALL_HINTS['mpv']}")
    if not (diag.get("yt-dlp") or diag.get("yt-dlp (python)")):
        lines.append(f"  yt-dlp не найден — поставьте: {INSTALL_HINTS['yt-dlp']}")
    if not diag.get("ffmpeg"):
        lines.append(f"  ffmpeg не найден — без него не будет склейки HD-потоков: {INSTALL_HINTS['ffmpeg']}")
    if diag.get("mpv") and (diag.get("yt-dlp") or diag.get("yt-dlp (python)")):
        lines.append("")
        lines.append("  Всё готово: можно запускать видео.")
    lines.append("")
    lines.append("  Полный гайд по памяти — docs/YOUTUBE_RAM_RU.md")
    return "\n".join(lines)


def print_missing_dependencies() -> None:
    print("Не хватает компонентов для воспроизведения.\n")
    print(format_diagnostics(collect_diagnostics()))
    print("\nБыстрая установка (Windows, PowerShell):")
    print("  winget install yt-dlp")
    print("  winget install mpv")
    print("  winget install Gyan.FFmpeg")
    print("\nИли через pip:  pip install yt-dlp")


# --------------------------------------------------------------------------- #
#  Запуск
# --------------------------------------------------------------------------- #
@dataclass
class PlaybackPlan:
    command: list[str]
    player_name: str
    description: str


def make_plan(
    target: str,
    quality: str = DEFAULT_QUALITY,
    audio_only: bool = False,
    player_choice: str | None = None,
    cache_mb: int = DEFAULT_CACHE_MB,
    single_video: bool = True,
    playlist_items: str | None = None,
    extra_args: list[str] | None = None,
    resolve: bool = True,
) -> PlaybackPlan | None:
    """Решить, чем и как играть. None — если ничего не установлено."""
    chosen = pick_player(player_choice)
    if not chosen:
        return None
    player_name, player_path = chosen
    url = build_target(target, single=single_video)
    quality = quality if quality in QUALITY_FORMATS else DEFAULT_QUALITY

    if player_name == "mpv":
        # mpv сам вызывает yt-dlp, поэтому никаких промежуточных ссылок.
        cmd = build_mpv_command(
            player_path,
            url,
            quality=quality,
            audio_only=audio_only,
            cache_mb=cache_mb,
            playlist_items=playlist_items,
            extra_args=extra_args,
        )
        desc = f"mpv · {QUALITY_TITLES['audio'] if audio_only else QUALITY_TITLES[quality]}"
        return PlaybackPlan(cmd, player_name, desc)

    stream_url = (
        resolve_stream(url, quality, audio_only=audio_only) if resolve else url
    )
    cmd = build_external_command(
        player_name, player_path, stream_url, audio_only=audio_only, cache_mb=cache_mb
    )
    if extra_args:
        cmd[1:1] = extra_args
    return PlaybackPlan(cmd, player_name, f"{player_name} · прямой поток")


def run_plan(plan: PlaybackPlan) -> int:
    print(f"Плеер: {plan.description}")
    print("Команда:", subprocess.list2cmdline(plan.command))
    print("Выход из плеера — закрыть окно mpv. Ctrl+C здесь тоже остановит видео.\n")
    started = time.time()
    try:
        proc = subprocess.Popen(plan.command)
    except OSError as exc:
        print(f"Не удалось запустить плеер: {exc}")
        return 1
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        code = proc.wait()
    elapsed = int(time.time() - started)
    print(f"Готово. Время воспроизведения: {elapsed // 60} мин {elapsed % 60} c.")
    return code


# --------------------------------------------------------------------------- #
#  Мини-графический пульт (tkinter — есть в стандартной поставке Python)
# --------------------------------------------------------------------------- #
def run_gui() -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("600x330")
    root.minsize(520, 300)
    root.configure(bg="#14161c")

    style = ttk.Style(root)
    with contextlib.suppress(tk.TclError):
        style.theme_use("clam")
    style.configure("TFrame", background="#14161c")
    style.configure("TLabel", background="#14161c", foreground="#e6e8ef")
    style.configure("Hint.TLabel", foreground="#8d93a6")
    style.configure("TButton", padding=6)
    style.configure("TCheckbutton", background="#14161c", foreground="#e6e8ef")

    header = ttk.Label(
        root, text="YouTube без браузера", font=("Segoe UI", 15, "bold")
    )
    header.pack(anchor="w", padx=16, pady=(14, 2))
    ttk.Label(
        root,
        text="Ссылка или поисковый запрос. Буфер плеера ограничен — RAM не разрастается.",
        style="Hint.TLabel",
        wraplength=560,
    ).pack(anchor="w", padx=16, pady=(0, 10))

    frame = ttk.Frame(root)
    frame.pack(fill="x", padx=16)

    url_var = tk.StringVar()
    entry = ttk.Entry(frame, textvariable=url_var)
    entry.pack(fill="x", pady=(0, 8))
    entry.focus_set()

    row = ttk.Frame(frame)
    row.pack(fill="x")
    quality_var = tk.StringVar(value=QUALITY_TITLES[DEFAULT_QUALITY])
    combo = ttk.Combobox(
        row,
        textvariable=quality_var,
        values=[QUALITY_TITLES[k] for k in QUALITY_FORMATS],
        state="readonly",
        width=32,
    )
    combo.pack(side="left")
    audio_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(row, text="только звук", variable=audio_var).pack(
        side="left", padx=10
    )
    single_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(row, text="не весь плейлист", variable=single_var).pack(side="left")

    log = tk.Text(
        root,
        height=8,
        bg="#0d0f14",
        fg="#b9c0d4",
        insertbackground="#b9c0d4",
        relief="flat",
        wrap="word",
    )
    log.pack(fill="both", expand=True, padx=16, pady=10)

    def log_line(text: str) -> None:
        log.insert("end", text.rstrip() + "\n")
        log.see("end")

    def play() -> None:
        target = url_var.get().strip()
        if not target:
            messagebox.showinfo(APP_NAME, "Вставьте ссылку или введите запрос.")
            return
        title = quality_var.get()
        quality = next(
            (k for k, v in QUALITY_TITLES.items() if v == title), DEFAULT_QUALITY
        )
        try:
            plan = make_plan(
                target,
                quality=quality,
                audio_only=audio_var.get(),
                single_video=single_var.get(),
            )
        except RuntimeError as exc:
            log_line(f"Ошибка: {exc}")
            return
        if plan is None:
            log_line("Плеер не найден. Установите mpv: winget install mpv")
            print_missing_dependencies()
            return
        log_line(f"Запуск: {plan.description}")
        threading.Thread(target=_spawn, args=(plan, log_line), daemon=True).start()

    def _spawn(plan: PlaybackPlan, log_fn) -> None:
        try:
            proc = subprocess.Popen(
                plan.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
        except OSError as exc:
            log_fn(f"Не удалось запустить плеер: {exc}")
            return
        assert proc.stdout is not None
        lines = 0
        for line in proc.stdout:
            line = line.strip()
            if line and lines < 40:
                log_fn(line)
                lines += 1
        code = proc.wait()
        log_fn(f"Плеер закрыт (код {code}).")

    def check() -> None:
        log_line(format_diagnostics(collect_diagnostics()))

    buttons = ttk.Frame(root)
    buttons.pack(fill="x", padx=16, pady=(0, 14))
    ttk.Button(buttons, text="▶ Смотреть", command=play).pack(side="left")
    ttk.Button(buttons, text="Проверить систему", command=check).pack(
        side="left", padx=8
    )
    ttk.Button(buttons, text="Выход", command=root.destroy).pack(side="right")

    root.bind("<Return>", lambda _event: play())
    log_line("Готово к работе. Вставьте ссылку и нажмите Enter.")
    root.mainloop()
    return 0


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youtube_lite",
        description="Лёгкий просмотр YouTube через mpv/yt-dlp — без браузера и лишней памяти.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python youtube_lite.py https://youtu.be/dQw4w9WgXcQ\n"
            "  python youtube_lite.py 'deep house mix' --quality low\n"
            "  python youtube_lite.py URL --audio\n"
            "  python youtube_lite.py --gui\n"
        ),
    )
    parser.add_argument("target", nargs="?", help="ссылка на видео/плейлист или поисковый запрос")
    parser.add_argument(
        "-q",
        "--quality",
        choices=sorted(QUALITY_FORMATS),
        default=DEFAULT_QUALITY,
        help="качество потока (по умолчанию hd = 720p)",
    )
    parser.add_argument("-a", "--audio", action="store_true", help="только звук (музыка, подкасты)")
    parser.add_argument("--player", choices=["mpv", "vlc", "ffplay"], help="какой плеер использовать")
    parser.add_argument("--cache-mb", type=int, default=DEFAULT_CACHE_MB, help="размер буфера плеера, МБ")
    parser.add_argument("--playlist", action="store_true", help="играть весь плейлист, а не первое видео")
    parser.add_argument("--playlist-items", help='диапазон плейлиста, например "1-20"')
    parser.add_argument("--gui", action="store_true", help="открыть маленькое окно-пульт")
    parser.add_argument("--check", action="store_true", help="показать, что установлено в системе")
    parser.add_argument("--print-command", action="store_true", help="только показать команду запуска")
    parser.add_argument("--player-arg", action="append", default=[], help="доп. аргумент плееру (можно несколько раз)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.gui:
        return run_gui()
    if args.check:
        print(format_diagnostics(collect_diagnostics()))
        return 0
    if not args.target:
        parser.print_help()
        print("\nПодсказка: --gui открывает окно-пульт, --check проверяет окружение.")
        return 0

    try:
        plan = make_plan(
            args.target,
            quality=args.quality,
            audio_only=args.audio,
            player_choice=args.player,
            cache_mb=max(16, args.cache_mb),
            single_video=not args.playlist,
            playlist_items=args.playlist_items,
            extra_args=args.player_arg,
        )
    except RuntimeError as exc:
        print(f"Ошибка: {exc}")
        print(f"Установите yt-dlp: {INSTALL_HINTS['yt-dlp']}")
        return 2

    if plan is None:
        print_missing_dependencies()
        return 2
    if args.print_command:
        print(subprocess.list2cmdline(plan.command))
        return 0
    return run_plan(plan)


if __name__ == "__main__":
    sys.exit(main())
