"""
online_services.py
------------------
Фоновые службы для интеграции Discord Rich Presence, поиска обложек и текстов.
"""

import contextlib
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request

from PySide6.QtCore import QObject, QThread, Signal

logger = logging.getLogger(__name__)

# Попытка импортировать pypresence для Discord
try:
    from pypresence import Presence
    PYPRESENCE_AVAILABLE = True
except ImportError:
    PYPRESENCE_AVAILABLE = False

# Заглушка Application ID — настоящее ID приходит из настроек.
PLACEHOLDER_CLIENT_ID = "1234567890123456789"

# Ограничения сетевых ответов: защита от OOM на битых/враждебных ответах.
MAX_JSON_BYTES = 2 * 1024 * 1024      # 2 МБ на JSON-ответ
MAX_IMAGE_BYTES = 20 * 1024 * 1024    # 20 МБ на изображение обложки
MAX_READ_ATTEMPTS = 2                 # 1 повтор при сетевой ошибке

# Обложки iTunes приходят только с CDN Apple — прочие хосты игнорируем (SSRF).
_ALLOWED_COVER_HOSTS = (".mzstatic.com",)


def _read_capped(response, limit: int) -> bytes:
    """Прочитать не больше limit байт, чтобы битый сервер не занял всю память."""
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"Ответ больше {limit} байт")
    return data


def _fetch_json(url: str, timeout: float = 5) -> dict:
    """GET -> JSON-объект с проверкой статуса, размера и типа."""
    req = urllib.request.Request(url, headers={"User-Agent": "DeepSpacePlayer/4.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if status != 200:
            raise ValueError(f"HTTP {status}")
        data = _read_capped(response, MAX_JSON_BYTES)
    parsed = json.loads(data.decode("utf-8", errors="replace"))
    if not isinstance(parsed, dict):
        raise ValueError("Ожидался JSON-объект")
    return parsed


def _fetch_bytes(url: str, timeout: float = 8, limit: int = MAX_IMAGE_BYTES) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "DeepSpacePlayer/4.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if status != 200:
            raise ValueError(f"HTTP {status}")
        return _read_capped(response, limit)


def _with_retry(fn, attempts: int = MAX_READ_ATTEMPTS):
    """Выполнить fn, при сетевой ошибке повторить один раз."""
    last_error = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — повторяем любые сетевые сбои
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
    raise last_error


class DiscordRPCWorker(QThread):
    def __init__(self, client_id=PLACEHOLDER_CLIENT_ID, parent=None):
        super().__init__(parent)
        self.client_id = client_id
        self.rpc = None
        self._is_connected = False
        self._current_data = {}
        self._running = True
        self.enabled = True

    def set_enabled(self, value: bool):
        self.enabled = bool(value)
        if not self.enabled:
            self._current_data = {}

    def run(self):
        if not PYPRESENCE_AVAILABLE:
            return
        if not self.client_id or self.client_id == PLACEHOLDER_CLIENT_ID:
            # Настоящий Application ID ещё не задан — подключаться нечего.
            return
        try:
            self.rpc = Presence(self.client_id)
            self.rpc.connect()
            self._is_connected = True
        except Exception as exc:
            logger.warning("Discord RPC: не удалось подключиться: %s", exc)
            return

        try:
            while self._running:
                if self.enabled and self._current_data and self._is_connected:
                    try:
                        self.rpc.update(**self._current_data)
                    except Exception as exc:
                        logger.debug("Discord RPC: update failed: %s", exc)
                # Спим короткими интервалами и перепроверяем self._running,
                # чтобы stop() отрабатывал почти мгновенно, а не ждал до 2с
                # (иначе закрытие приложения ощутимо подвисает).
                for _ in range(20):
                    if not self._running:
                        break
                    self.msleep(100)
        finally:
            # Закрываем соединение в СВОЁМ потоке — rpc.close() из GUI-потока
            # гонялся бы с rpc.update() по одному сокету.
            if self.rpc is not None:
                try:
                    self.rpc.close()
                except Exception as exc:
                    logger.debug("Discord RPC: close failed: %s", exc)
            self.rpc = None
            self._is_connected = False

    def update_presence(self, title, artist, state="Play", elapsed=0):
        if not PYPRESENCE_AVAILABLE:
            return
        details = f"{title[:128]}" if title else "Без названия"
        state_str = f"by {artist[:128]}" if artist else "Deep Space Player"
        self._current_data = {
            "details": details,
            "state": state_str,
            "large_image": "space_logo",
            "large_text": "Deep Space Music Player",
            "small_image": "play" if state == "Play" else "pause",
            "small_text": state
        }

    def stop(self):
        self._running = False
        self.quit()
        # Ждём, пока run() сам закроет rpc в своём потоке.
        if self.isRunning():
            self.wait(3000)


class CoverLyricsFetcher(QObject):
    cover_found = Signal(str, str)   # track_path, cover_path
    lyrics_found = Signal(str, str)  # track_path, lyrics_text
    finished = Signal()

    def __init__(self, track_path, title, artist):
        super().__init__()
        self.track_path = track_path
        self.title = title
        self.artist = artist
        self._cache_dir = None

    @property
    def cache_dir(self):
        """Ленивое создание директории кэша — только при первом обращении."""
        if self._cache_dir is None:
            self._cache_dir = os.path.expanduser("~/.cache/covers")
            os.makedirs(self._cache_dir, exist_ok=True)
        return self._cache_dir

    def process(self):
        if self.title and self.artist:
            self._fetch_cover()
            self._fetch_lyrics()
        self.finished.emit()

    @staticmethod
    def _cache_file_name(artist: str, title: str) -> str:
        """Имя кэш-файла, безопасное для любой ФС (в т.ч. Windows):
        запрещённые символы заменяются, длина ограничивается."""
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", f"{artist}_{title}")
        safe = safe.replace(" ", "_").strip("._") or "cover"
        return safe[:120] + ".jpg"

    def _fetch_cover(self):
        cached_path = os.path.join(self.cache_dir, self._cache_file_name(self.artist, self.title))

        if os.path.exists(cached_path):
            with open(cached_path, "rb") as f:
                existing = f.read()
            if self._is_valid_image_bytes(existing):
                self.cover_found.emit(self.track_path, cached_path)
                return
            else:
                # Битый/недокачанный файл из прошлого запуска — удаляем и качаем заново.
                with contextlib.suppress(OSError):
                    os.remove(cached_path)

        try:
            # Запрос к iTunes Search API (публичный, без ключа)
            query = urllib.parse.quote(f"{self.artist} {self.title}", safe="")
            url = f"https://itunes.apple.com/search?term={query}&entity=song&limit=1"

            def _download():
                data = _fetch_json(url, timeout=5)
                if data.get("resultCount", 0) <= 0:
                    return None
                results = data.get("results")
                if not isinstance(results, list) or not results:
                    return None
                first = results[0]
                if not isinstance(first, dict):
                    return None
                img_url = str(first.get("artworkUrl100", "")).replace("100x100bb", "600x600bb")
                if not img_url:
                    return None
                # URL берём из ответа API — проверяем, что это наш CDN Apple.
                parsed = urllib.parse.urlparse(img_url)
                if parsed.scheme != "https" or not (
                    parsed.hostname == "mzstatic.com" or parsed.hostname.endswith(_ALLOWED_COVER_HOSTS)
                ):
                    logger.warning("Cover Fetcher: отклонён неожиданный хост: %s", parsed.hostname)
                    return None
                return _fetch_bytes(img_url, timeout=8, limit=MAX_IMAGE_BYTES)

            img_bytes = _with_retry(_download)
            if not img_bytes:
                return

            if not self._is_valid_image_bytes(img_bytes):
                # Докачалось не полностью / повреждено — не сохраняем и не показываем,
                # чтобы не выводить "битую" обложку с артефактами.
                return

            # Пишем во временный файл и атомарно переименовываем — это исключает
            # ситуацию, когда в кэше остаётся наполовину записанный (битый) файл.
            tmp_path = cached_path + ".tmp"
            with open(tmp_path, "wb") as f:
                f.write(img_bytes)
            os.replace(tmp_path, cached_path)
            self.cover_found.emit(self.track_path, cached_path)
        except Exception as exc:
            logger.warning("Cover Fetcher: ошибка поиска обложки: %s", exc)

    @staticmethod
    def _is_valid_image_bytes(data: bytes) -> bool:
        """Грубая, но надёжная проверка целостности изображения: минимальный
        размер + корректная сигнатура формата (JPEG/PNG). Отсекает обрывы
        соединения и наполовину докачанные файлы, которые декодер Qt может
        отрисовать как «шум»/цветные полосы вместо явной ошибки."""
        if not data or len(data) < 3000:
            return False
        if data[:2] == b"\xff\xd8":  # JPEG
            # Корректный JPEG обязан заканчиваться маркером конца изображения.
            return data[-2:] == b"\xff\xd9"
        if data[:8] == b"\x89PNG\r\n\x1a\n":  # PNG
            # Последний чанк PNG обязан быть IEND: длина(4) + "IEND" + CRC(4).
            return data.rstrip().endswith(b"\x00\x00\x00\x00IEND\xae\x42\x60\x82")
        return False

    def _fetch_lyrics(self):
        try:
            # Запрос к публичному API LRCLIB
            artist = urllib.parse.quote(self.artist, safe="")
            title = urllib.parse.quote(self.title, safe="")
            url = f"https://lrclib.net/api/get?artist_name={artist}&track_name={title}"

            def _download():
                return _fetch_json(url, timeout=5)

            data = _with_retry(_download)
            lyrics = data.get("syncedLyrics") or data.get("plainLyrics")
            if isinstance(lyrics, str) and lyrics:
                self.lyrics_found.emit(self.track_path, lyrics)
        except Exception as exc:
            logger.warning("Lyrics Fetcher: ошибка поиска текста: %s", exc)

# ------------------------------------------------------------------
# Интернет-радио: проверенные публичные потоки.
# Каждый URL проверен HTTP-запросом (200/редирект) — сомнительные
# и сгенерированные шаблоном станции удалены.
# ------------------------------------------------------------------
RADIO_STATIONS = [
    ("Lounge / Chillout — SomaFM Groove Salad", "https://ice1.somafm.com/groovesalad-128-mp3"),
    ("Ambient — SomaFM Deep Space One", "https://ice1.somafm.com/deepspaceone-128-mp3"),
    ("Ambient — SomaFM Fluid", "https://ice1.somafm.com/fluid-128-mp3"),
    ("Space Ambient — SomaFM Mission Control", "https://ice1.somafm.com/missioncontrol-128-mp3"),
    ("Synthwave — SomaFM Synphaera", "https://ice1.somafm.com/synphaera-128-mp3"),
    ("Drum & Bass — SomaFM Drone Zone", "https://ice1.somafm.com/dronezone-128-mp3"),
    ("Indie Pop — SomaFM Indie Pop Rocks!", "https://ice1.somafm.com/indiepop-128-mp3"),
    ("Vocal Chill — SomaFM Lush", "https://ice1.somafm.com/lush-128-mp3"),
    ("Electronic / Games — SomaFM DEF CON Radio", "https://ice1.somafm.com/defcon-128-mp3"),
    ("80-е — SomaFM U80s", "https://ice1.somafm.com/u80s-128-mp3"),
    ("70-е — SomaFM Left Coast 70s", "https://ice1.somafm.com/seventies-128-mp3"),
    ("Deep House — SomaFM Beat Blender", "https://ice1.somafm.com/beatblender-128-mp3"),
    ("Lounge / Spy — SomaFM Secret Agent", "https://ice1.somafm.com/secretagent-128-mp3"),
    ("Psytrance — SomaFM The Trip", "https://ice1.somafm.com/thetrip-128-mp3"),
    ("Rock / Mix — Radio Paradise", "https://stream.radioparadise.com/mp3-128"),
    ("Alternative / Indie — KEXP", "https://kexp-mp3-128.streamguys1.com/kexp128.mp3"),
    ("Pop — NPO 3FM", "https://icecast.omroep.nl/3fm-bb-mp3"),
    ("Классика — Radio Swiss Classic", "https://stream.srg-ssr.ch/m/rsc_de/mp3_128"),
]
