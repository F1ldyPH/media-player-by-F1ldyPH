#!/usr/bin/env python3
"""
firefox_tune.py
---------------
Настройка Firefox под мало оперативной памяти (в первую очередь — под видео
на YouTube, где вкладка разрастается до 1.5–3 ГБ).

Что меняется и почему
---------------------
Firefox держит медиа-кэш и кэш декодированных кадров в RAM, и их размеры по
умолчанию рассчитаны на «нормальные» машины:

* ``media.cache_size`` — буфер видео, по умолчанию 512 000 КБ (≈500 МБ);
* ``media.memory_caches_combined_limit_kb`` — общий лимит медиа-кэшей,
  524 288 КБ (512 МБ), плюс ``..._pc_sysmem`` = 5 (% от всей RAM);
* ``image.mem.surfacecache.max_size_kb`` — кэш декодированных картинок.

Плюс разумные значения для числа процессов контента и выгрузки неактивных
вкладок. Итог: браузер не «съедает всё», а при перемотке просто чуть чаще
перечитывает поток (это дешевле, чем своп).

Использование
-------------
    python firefox_tune.py --list                 # найти профили Firefox
    python firefox_tune.py --print                # показать prefs, ничего не менять
    python firefox_tune.py --apply                # записать в профиль по умолчанию
    python firefox_tune.py --apply --preset 8gb   # агрессивнее (ПК с 8 ГБ)
    python firefox_tune.py --apply --profile "abc.default-release"
    python firefox_tune.py --remove               # убрать наши настройки
    python firefox_tune.py --apply --dry-run      # показать, что будет записано

Настройки пишутся в файл ``user.js`` профиля (Firefox применяет его при
старте). Существующий ``user.js`` сохраняется в резервную копию с меткой
времени, чужие строки не удаляются — наш блок добавляется между маркерами.
Откат: ``--remove`` (или удалить блок руками / вернуть бэкап).

Важно: скрипт СОЗНАТЕЛЬНО не трогает изоляцию сайтов (Fission) и не отключает
аппаратное ускорение — советы «выключить всё это» из интернета дают прирост
памяти ценой безопасности и плавности.
"""

from __future__ import annotations

import argparse
import configparser
import os
import shutil
import sys
import time
from pathlib import Path

APP_NAME = "Firefox RAM tune"
BEGIN_MARK = "// >>> firefox_tune: автоматические настройки памяти (см. firefox_tune.py)"
END_MARK = "// <<< firefox_tune"

# Базовый набор: безопасно для любой машины, ощутимо снижает «потолок» памяти.
BASE_PREFS: list[tuple[str, str, str]] = [
    (
        "browser.tabs.unloadOnLowMemory",
        "true",
        "при критической нехватке RAM Firefox выгружает неактивные вкладки, а не падает",
    ),
    (
        "browser.tabs.min_inactive_duration_before_unload",
        "1800000",
        "выгружать только вкладки, к которым не обращались 30 минут",
    ),
    (
        "dom.ipc.processCount",
        "4",
        "максимум 4 процесса контента (по умолчанию — до 8, каждый десятки-сотни МБ)",
    ),
    (
        "media.cache_size",
        "102400",
        "буфер видео 100 МБ вместо 500 МБ (перемотка чуть чаще докачивает поток)",
    ),
    (
        "media.memory_caches_combined_limit_kb",
        "131072",
        "общий лимит медиа-кэшей 128 МБ вместо 512 МБ",
    ),
    (
        "media.memory_caches_combined_limit_pc_sysmem",
        "3",
        "медиа-кэши не могут занять больше 3% всей RAM (было 5%)",
    ),
    (
        "image.mem.surfacecache.max_size_kb",
        "262144",
        "кэш декодированных картинок 256 МБ вместо ~2 ГБ",
    ),
    (
        "browser.sessionhistory.max_total_viewers",
        "4",
        "не держать в памяти «снимки» всех страниц истории для кнопки Назад",
    ),
    (
        "media.hardware-video-decoding.enabled",
        "true",
        "аппаратное декодирование видео — экономит и RAM, и CPU",
    ),
]

# Пресет для 8 ГБ и меньше: меньше процессов, короче медиа-буферы.
LOW_RAM_PREFS: list[tuple[str, str, str]] = [
    ("dom.ipc.processCount", "2", "всего 2 процесса контента — минимум накладных расходов"),
    ("media.cache_size", "65536", "буфер видео 64 МБ"),
    ("media.memory_caches_combined_limit_kb", "65536", "общий лимит медиа-кэшей 64 МБ"),
    ("image.mem.surfacecache.max_size_kb", "131072", "кэш картинок 128 МБ"),
    ("dom.ipc.processCount.webIsolated", "1", "меньше процессов под изолированные фреймы (чуть слабее изоляция сайтов)"),
]

# Необязательное: помогает только на старых CPU без аппаратного AV1.
OPTIONAL_PREFS: list[tuple[str, str, str]] = [
    (
        "media.av1.enabled",
        "false",
        "YouTube перестанет отдавать AV1 (его часто декодирует CPU в софте: до 1 ГБ+ RAM и 100% загрузки). Минус: нет 4K/8K в AV1.",
    ),
]

def merge_prefs(*groups: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Склеить наборы prefs: при совпадении имени побеждает последнее значение."""
    merged: dict[str, tuple[str, str, str]] = {}
    for group in groups:
        for name, value, comment in group:
            merged[name] = (name, value, comment)
    return list(merged.values())


PRESETS: dict[str, list[tuple[str, str, str]]] = {
    "normal": BASE_PREFS,
    "8gb": merge_prefs(BASE_PREFS, LOW_RAM_PREFS),
    "aggressive": merge_prefs(BASE_PREFS, LOW_RAM_PREFS, OPTIONAL_PREFS),
}


# --------------------------------------------------------------------------- #
#  Поиск профилей
# --------------------------------------------------------------------------- #
def firefox_root() -> Path | None:
    """Каталог с профилями Firefox для текущей ОС."""
    home = Path.home()
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox",  # Windows
        home / ".mozilla" / "firefox",                                # Linux
        home / "Library" / "Application Support" / "Firefox",         # macOS
        home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
        home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
    ]
    for path in candidates:
        if path and (path / "profiles.ini").is_file():
            return path
    return None


def read_profiles(root: Path) -> list[dict[str, str]]:
    """Разобрать profiles.ini → список профилей с абсолютными путями."""
    ini = configparser.ConfigParser()
    ini.read_string((root / "profiles.ini").read_text(encoding="utf-8", errors="replace"))
    profiles: list[dict[str, str]] = []
    for section in ini.sections():
        if not section.lower().startswith("profile"):
            continue
        path = ini.get(section, "Path", fallback="")
        if not path:
            continue
        relative = ini.get(section, "IsRelative", fallback="1")
        full = (root / path) if relative == "1" else Path(path)
        profiles.append(
            {
                "name": ini.get(section, "Name", fallback=Path(path).name),
                "path": str(full),
                "default": ini.get(section, "Default", fallback="0"),
                "section": section,
            }
        )
    return profiles


def pick_profile(profiles: list[dict[str, str]], wanted: str | None) -> dict[str, str] | None:
    """Профиль по имени/пути, иначе профиль по умолчанию."""
    if wanted:
        w = wanted.lower()
        for p in profiles:
            if w in (p["name"].lower(), Path(p["path"]).name.lower(), p["path"].lower()):
                return p
        return None
    for p in profiles:
        if p["default"] == "1":
            return p
    for p in profiles:
        if "default-release" in Path(p["path"]).name:
            return p
    # installs.ini не разбираем: если профилей несколько, нужен явный выбор.
    return profiles[0] if len(profiles) == 1 else None


# --------------------------------------------------------------------------- #
#  Генерация и запись user.js
# --------------------------------------------------------------------------- #
def render_prefs(preset: str = "normal", include_optional: bool = False) -> str:
    """Собрать текст user.js с комментариями."""
    prefs = list(PRESETS.get(preset, BASE_PREFS))
    if include_optional and preset != "aggressive":
        prefs = merge_prefs(prefs, OPTIONAL_PREFS)
    lines = [
        BEGIN_MARK,
        "// Сгенерировано firefox_tune.py. Блок можно удалить целиком —",
        "// остальные ваши настройки в user.js не тронуты.",
        f"// Пресет: {preset}. Применяется при следующем запуске Firefox.",
        "",
    ]
    for name, value, comment in prefs:
        lines.append(f"// {comment}")
        lines.append(f'user_pref("{name}", {_js_value(value)});')
        lines.append("")
    lines.append(END_MARK)
    return "\n".join(lines).rstrip() + "\n"


def _js_value(value: str) -> str:
    """true/false/число — как есть; иначе строка в кавычках."""
    if value in ("true", "false"):
        return value
    try:
        int(value)
    except ValueError:
        return '"' + value.replace('"', '\\"') + '"'
    return value


def strip_block(text: str) -> str:
    """Убрать ранее добавленный блок (идемпотентность при повторном --apply)."""
    out: list[str] = []
    inside = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == BEGIN_MARK:
            inside = True
            continue
        if inside:
            if stripped == END_MARK:
                inside = False
            continue
        out.append(line)
    return "".join(out).rstrip() + ("\n" if out else "")


def apply_prefs(
    profile_dir: Path, preset: str = "normal", include_optional: bool = False, dry_run: bool = False
) -> tuple[str, Path | None]:
    """Записать блок в user.js профиля. Возвращает (итоговый текст, путь бэкапа)."""
    user_js = profile_dir / "user.js"
    original = user_js.read_text(encoding="utf-8", errors="replace") if user_js.is_file() else ""
    block = render_prefs(preset, include_optional)
    cleaned = strip_block(original) if original else ""
    final = (cleaned.rstrip() + "\n\n" if cleaned.strip() else "") + block

    if dry_run:
        return final, None

    backup: Path | None = None
    if user_js.is_file():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = user_js.with_name(f"user.js.bak-{stamp}")
        shutil.copy2(user_js, backup)
    user_js.write_text(final, encoding="utf-8")
    return final, backup


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="firefox_tune",
        description="Настройки Firefox, снижающие расход памяти (профиль через user.js).",
        epilog=(
            "Примеры:\n"
            "  python firefox_tune.py --list\n"
            "  python firefox_tune.py --print --preset 8gb\n"
            "  python firefox_tune.py --apply --preset 8gb\n"
            "  python firefox_tune.py --remove\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--list", action="store_true", help="показать найденные профили Firefox")
    parser.add_argument("--print", action="store_true", help="напечатать prefs (ничего не пишем)")
    parser.add_argument("--apply", action="store_true", help="записать настройки в профиль")
    parser.add_argument("--remove", action="store_true", help="удалить наш блок из user.js")
    parser.add_argument("--profile", help="имя или путь профиля (иначе профиль по умолчанию)")
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        default="normal",
        help="normal (по умолчанию), 8gb (мало памяти), aggressive (8gb + отключить AV1)",
    )
    parser.add_argument("--optional", action="store_true", help="добавить необязательные prefs (AV1)")
    parser.add_argument("--dry-run", action="store_true", help="показать результат, но не писать файл")
    parser.add_argument("--profile-dir", help="путь к папке профиля напрямую (если поиск не сработал)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.print:
        print(render_prefs(args.preset, args.optional))
        return 0

    if args.profile_dir:
        profile_dir = Path(args.profile_dir).expanduser()
        name = profile_dir.name
    else:
        root = firefox_root()
        if root is None:
            print("Профили Firefox не найдены.")
            print("Укажите папку профиля вручную: --profile-dir \"...\\profiles\\xxxx.default-release\"")
            return 2
        profiles = read_profiles(root)
        if not profiles:
            print(f"В {root} нет ни одного профиля.")
            return 2
        if args.list:
            print(f"Firefox: {root}\n")
            for p in profiles:
                mark = "по умолчанию" if p["default"] == "1" else ""
                print(f"  {p['name']:<32} {p['path']}  {mark}")
            print("\nПрименить: python firefox_tune.py --apply --profile \"<имя>\"")
            return 0
        chosen = pick_profile(profiles, args.profile)
        if chosen is None:
            print("Не удалось однозначно выбрать профиль. Доступные:\n")
            for p in profiles:
                print(f"  - {p['name']}")
            print("\nУкажите нужный через --profile.")
            return 2
        profile_dir = Path(chosen["path"])
        name = chosen["name"]

    if not profile_dir.is_dir():
        print(f"Папка профиля не найдена: {profile_dir}")
        return 2

    user_js = profile_dir / "user.js"
    if args.remove:
        if not user_js.is_file():
            print("user.js отсутствует — удалять нечего.")
            return 0
        text = user_js.read_text(encoding="utf-8", errors="replace")
        cleaned = strip_block(text)
        if args.dry_run:
            print(cleaned)
            return 0
        backup = user_js.with_name(f"user.js.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(user_js, backup)
        if cleaned.strip():
            user_js.write_text(cleaned, encoding="utf-8")
        else:
            user_js.unlink()  # файл был только с нашим блоком
        print(f"Настройки удалены из {user_js}\nБэкап: {backup}")
        return 0

    if not (args.apply or args.dry_run):
        build_parser().print_help()
        return 0

    text, backup = apply_prefs(profile_dir, args.preset, args.optional, dry_run=args.dry_run)
    if args.dry_run:
        print(f"# Профиль: {name} ({profile_dir})\n")
        print(text)
        print("# --dry-run: файл не изменён. Запустите с --apply, чтобы записать.")
        return 0

    print(f"Профиль: {name}")
    print(f"Записано: {user_js}")
    if backup:
        print(f"Бэкап прежнего user.js: {backup}")
    print("\nГотово. Полностью закройте Firefox и запустите снова — настройки применятся.")
    cache_kb = next(
        (v for n, v, _ in PRESETS.get(args.preset, BASE_PREFS) if n == "media.cache_size"),
        "102400",
    )
    print(f"Проверить можно в about:config: media.cache_size должно быть {cache_kb}.")
    print("Откат: python firefox_tune.py --remove")
    return 0


if __name__ == "__main__":
    sys.exit(main())
