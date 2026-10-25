"""
audio_engine.py
---------------
Проигрывание аудио через QAudioSink (pull-устройство) с собственным DSP:
10-полосный эквалайзер, спатиализация/реверберация, спектроанализатор.

Потоковая модель: readData() вызывается из аудио-потока Qt, поэтому всё
состояние, которое читает _produce_bytes(), защищено self._lock (RLock).
Тяжёлые вычисления (time_stretch, DSP-цепочка) выполняются ВНЕ блокировки:
под замком снимается только КОПИЯ блока и продвигаются позиции. Состояние
эквалайзера/спектроанализатора принадлежит аудио-потоку, GUI влияет на него
через request_reset() — без прямой записи из GUI-потока.
"""

import logging
import os
import subprocess
import threading
from collections import OrderedDict

import numpy as np
from PySide6.QtCore import QIODevice, QObject, QThread, QTimer, Signal
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaPlayer
from scipy.signal import lfilter

logger = logging.getLogger(__name__)

try:
    from pydub import AudioSegment
    # Скрытие черных окон CMD при вызове ffmpeg в Windows
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        AudioSegment.converter_kwargs = {
            "startupinfo": startupinfo,
            "creationflags": subprocess.CREATE_NO_WINDOW,
        }
except ImportError:
    AudioSegment = None

try:
    import librosa
except ImportError:
    librosa = None


SAMPLE_RATE = 44100
CHANNELS = 2

# Ограничение кэша декодированных треков (float32, 2 канала ≈ 352 КБ/сек).
# Без лимита кэш рос до сотен МБ и никогда не отдавал память обратно.
CACHE_MAX_BYTES = 192 * 1024 * 1024

EQ_BANDS_HZ = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]

EQ_PRESETS = {
    "Flat":           [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "Rock":           [5, 4, -2, -4, -2, 2, 5, 6, 6, 5],
    "Pop":            [-1, 2, 4, 4, 1, -1, -2, -2, 1, 2],
    "Bass Boost":     [8, 7, 5, 3, 1, 0, 0, 0, 0, 0],
    "Jazz":           [3, 2, 1, 2, -1, -1, 0, 1, 2, 3],
    "Classical":      [4, 3, 0, 0, 0, 0, -2, -2, 3, 4],
    "Electronic":     [6, 5, 2, 0, -2, 2, 4, 5, 6, 5],
    "Vocal":         [-2, -1, 0, 2, 5, 5, 4, 2, 0, -1],
    "Hip-Hop":       [7, 6, 3, 1, -1, 0, 2, 3, 4, 3],
    "Metal":         [6, 5, 2, -2, -4, -2, 2, 5, 6, 6],
    "Acoustic":      [3, 2, 1, 2, 3, 3, 2, 1, 2, 3],
    "Piano":         [2, 1, 0, 1, 3, 4, 3, 2, 1, 2],
    "Custom":         [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
}


def _peaking_coeffs(freq_hz: float, gain_db: float, samplerate: int, q: float = 1.0):
    freq_hz = min(freq_hz, samplerate * 0.49)
    a_amp = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * freq_hz / samplerate
    alpha = np.sin(w0) / (2 * q)
    cos_w0 = np.cos(w0)

    b0 = 1 + alpha * a_amp
    b1 = -2 * cos_w0
    b2 = 1 - alpha * a_amp
    a0 = 1 + alpha / a_amp
    a1 = -2 * cos_w0
    a2 = 1 - alpha / a_amp

    b = np.array([b0, b1, b2], dtype=np.float64) / a0
    a = np.array([1.0, a1 / a0, a2 / a0], dtype=np.float64)
    return b, a


class TenBandEqualizer:
    """10-полосный эквалайзер (peaking-биквады).

    Потоковая модель: process() вызывается ТОЛЬКО из аудио-потока Qt,
    set_band()/set_preset() — из GUI-потока. Поэтому «уровни + коэффициенты»
    хранятся одним кортежем и подменяются ОДНОЙ ссылкой: аудио-поток всегда
    берёт согласованную пару (нет состояния «новые коэффициенты, старые gains»).
    Сброс состояния фильтров GUI только запрашивает (request_reset()), а
    выполняет его сам аудио-поток — иначе гонка по self._zi.
    """

    def __init__(self, samplerate: int = SAMPLE_RATE, channels: int = CHANNELS):
        self.samplerate = samplerate
        self.channels = channels
        self._state = (list(EQ_PRESETS["Flat"]), self._build_coeffs(EQ_PRESETS["Flat"]))
        self._reset_pending = False
        self._zi = []
        self.reset_state()

    def _build_coeffs(self, gains):
        return [
            _peaking_coeffs(freq, gain, self.samplerate)
            for freq, gain in zip(EQ_BANDS_HZ, gains, strict=True)
        ]

    @property
    def gains(self):
        """Текущие уровни полос (снимок состояния, безопасен для чтения)."""
        return self._state[0]

    def reset_state(self):
        # zi для lfilter(b, a, x, axis=0): длина по оси axis = max(len(a), len(b)) - 1 = 2,
        # остальные размерности — как у x. Для 1...N каналов форма (2, channels).
        # При channels != 2 прежняя форма (channels, 2) падала с ValueError.
        # Вызывать только из аудио-потока (или до старта); из GUI — request_reset().
        self._zi = [np.zeros((2, self.channels), dtype=np.float64) for _ in range(len(EQ_BANDS_HZ))]

    def request_reset(self):
        """Попросить аудио-поток сбросить состояния фильтров (потокобезопасно)."""
        self._reset_pending = True

    def set_band(self, index: int, gain_db: float):
        gains = list(self.gains)
        if not (0 <= index < len(gains)):
            return
        gains[index] = float(gain_db)
        # Сначала готовим новую пару, потом подменяем ссылку целиком.
        self._state = (gains, self._build_coeffs(gains))

    def set_preset(self, name: str):
        preset = EQ_PRESETS.get(name)
        if preset is not None:
            self._state = (list(preset), self._build_coeffs(preset))
            self._reset_pending = True

    def is_flat(self) -> bool:
        return all(abs(g) < 0.01 for g in self.gains)

    def process(self, block: np.ndarray) -> np.ndarray:
        if self._reset_pending:
            self._reset_pending = False
            self.reset_state()
        gains, coeffs = self._state
        if all(abs(g) < 0.01 for g in gains):
            return block.astype(np.float32, copy=False)
        out = block.astype(np.float64, copy=True)
        zi = self._zi
        for band_idx, (b, a) in enumerate(coeffs):
            if abs(gains[band_idx]) < 0.01:
                continue
            filtered, zf = lfilter(b, a, out, axis=0, zi=zi[band_idx])
            out = filtered
            zi[band_idx] = zf
        return out.astype(np.float32)


class SpectrumAnalyzer:
    """FFT-спектрометр с цикличным кольцевым буфером для исключения аллокаций памяти.

    push() идёт из аудио-потока, get_bars() — из GUI (таймер визуализатора),
    поэтому буфер защищён собственным (очень лёгким) замком: без него GUI мог
    прочитать буфер наполовину перезаписанным и увидеть «рваный» спектр.
    """

    def __init__(self, samplerate: int = SAMPLE_RATE, fft_size: int = 2048):
        self.samplerate = samplerate
        self.fft_size = fft_size
        self._lock = threading.Lock()
        self._ring_buffer = np.zeros(fft_size, dtype=np.float32)
        self._write_pos = 0
        self._window = np.hanning(fft_size).astype(np.float32)

        self._cached_num_bars = -1
        self._cached_edges = None
        self._freqs = np.fft.rfftfreq(self.fft_size, d=1.0 / self.samplerate)

    def push(self, mono_block: np.ndarray):
        n = len(mono_block)
        if n <= 0:
            return
        with self._lock:
            if n >= self.fft_size:
                self._ring_buffer[:] = mono_block[-self.fft_size:]
                self._write_pos = 0
            else:
                p1 = self.fft_size - self._write_pos
                if n <= p1:
                    self._ring_buffer[self._write_pos:self._write_pos + n] = mono_block
                else:
                    self._ring_buffer[self._write_pos:] = mono_block[:p1]
                    self._ring_buffer[:n - p1] = mono_block[p1:]
                self._write_pos = (self._write_pos + n) % self.fft_size

    def get_bars(self, num_bars: int = 30):
        with self._lock:
            buf = np.concatenate((self._ring_buffer[self._write_pos:], self._ring_buffer[:self._write_pos]))
        windowed = buf * self._window
        spectrum = np.abs(np.fft.rfft(windowed))

        if self._cached_num_bars != num_bars:
            self._cached_edges = np.geomspace(30.0, self.samplerate / 2.0, num_bars + 1)
            self._cached_num_bars = num_bars

        edges = self._cached_edges
        bars = np.zeros(num_bars, dtype=np.float32)
        for i in range(num_bars):
            mask = (self._freqs >= edges[i]) & (self._freqs < edges[i + 1])
            if np.any(mask):
                bars[i] = spectrum[mask].mean()

        bars = np.log1p(bars * 4.0)
        peak = bars.max()
        if peak > 1e-6:
            bars /= peak
        return bars.tolist()


class SpaceDSPProcessor:
    """DSP-микшер: 3D Spatializer (расширение стерео) и Space Reverb (космическая реверберация)."""
    def __init__(self, samplerate=SAMPLE_RATE, channels=CHANNELS):
        self.samplerate = samplerate
        self.channels = channels
        self.spatial_enabled = False
        self.spatial_width = 1.4

        self.reverb_enabled = False
        self.reverb_mix = 0.25
        self.delay_samples = int(samplerate * 0.08)
        self._delay_buf = np.zeros((self.delay_samples, channels), dtype=np.float32)
        self._delay_pos = 0

    def process(self, block: np.ndarray) -> np.ndarray:
        if not self.spatial_enabled and not self.reverb_enabled:
            return block

        out = block.copy()

        if self.spatial_enabled and self.channels == 2:
            left = out[:, 0]
            right = out[:, 1]
            mid = (left + right) * 0.5
            side = (left - right) * 0.5 * self.spatial_width
            out[:, 0] = mid + side
            out[:, 1] = mid - side

        if self.reverb_enabled:
            # Векторизованный эквивалент покадрового цикла: позиции чтения и
            # записи внутри блока не пересекаются (буфер длиннее блока), поэтому
            # prev можно вычислить для всего сегмента разом.
            n = len(out)
            pos = self._delay_pos
            x_start = 0
            remaining = n
            while remaining > 0:
                seg = min(remaining, self.delay_samples - pos)
                prev = self._delay_buf[pos:pos + seg]
                mixed = out[x_start:x_start + seg] * (1.0 - self.reverb_mix) + prev * self.reverb_mix
                out[x_start:x_start + seg] = mixed
                self._delay_buf[pos:pos + seg] = mixed + prev * 0.3
                pos = (pos + seg) % self.delay_samples
                x_start += seg
                remaining -= seg
            self._delay_pos = pos

        return out


class AudioLoaderThread(QThread):
    loaded = Signal(str, object)
    error = Signal(str, str)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.path = path

    def run(self):
        if AudioSegment is None:
            self.error.emit(self.path, "Модуль pydub не установлен.")
            return
        try:
            segment = AudioSegment.from_file(self.path)
            segment = segment.set_frame_rate(SAMPLE_RATE).set_channels(CHANNELS).set_sample_width(2)
            raw = np.array(segment.get_array_of_samples(), dtype=np.int16).astype(np.float32) / 32768.0
            raw = raw.reshape(-1, CHANNELS)
            self.loaded.emit(self.path, raw)
        except Exception as e:
            message = str(e)
            if "ffmpeg" in message.lower() or "ffprobe" in message.lower():
                message = ("Не найден ffmpeg — установите его (winget install ffmpeg) "
                           "и добавьте в PATH, затем повторите попытку.")
            self.error.emit(self.path, message)


class _PullAudioDevice(QIODevice):
    def __init__(self, engine: "AudioEngine"):
        super().__init__()
        self._engine = engine

    def isSequential(self) -> bool:
        return True

    def readData(self, maxlen: int):
        return self._engine._produce_bytes(maxlen)

    def writeData(self, data):
        return -1

    def bytesAvailable(self):
        return (1 << 20) + super().bytesAvailable()


class AudioEngine(QObject):
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    mediaStatusChanged = Signal(object)
    playbackStateChanged = Signal(object)
    errorOccurred = Signal(str, str)

    PlaybackState = QMediaPlayer.PlaybackState
    MediaStatus = QMediaPlayer.MediaStatus

    def __init__(self, parent=None):
        super().__init__(parent)
        # Защита состояния воспроизведения: readData() работает в аудио-потоке Qt,
        # остальные методы — в GUI-потоке. RLock: вложенные вызовы в одном потоке
        # (load -> _finish_loading -> play) допустимы, взаимоблокировки нет,
        # т.к. замок никогда не удерживается во время ожидания других замков.
        self._lock = threading.RLock()

        self.equalizer = TenBandEqualizer()
        self.spectrum = SpectrumAnalyzer()
        self.spatial_dsp = SpaceDSPProcessor()

        self._raw_samples = None
        self._samples = None
        self._frame_pos = 0
        self._volume = 0.7
        self._speed = 1.0
        self._current_path = None

        # LRU-кэш декодированных треков с ограничением по байтам
        # (см. CACHE_MAX_BYTES): OrderedDict, последний используемый — в конце.
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._loader_threads = []

        self._state = QMediaPlayer.PlaybackState.StoppedState
        self._status = QMediaPlayer.MediaStatus.NoMedia
        self._duration_ms = 0

        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(CHANNELS)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self._format = fmt

        self._sink = None
        self._device = _PullAudioDevice(self)

        self._pos_timer = QTimer(self)
        self._pos_timer.setInterval(100)
        self._pos_timer.timeout.connect(self._emit_position)

    def preload_track(self, path: str):
        if not path or path in self._cache:
            return
        thread = AudioLoaderThread(path, self)
        thread.loaded.connect(self._on_preloaded)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        self._loader_threads.append(thread)
        thread.start()

    def _cache_put(self, path, samples):
        """Положить/обновить запись в LRU-кэше, вытесняя старые по бюджету байт."""
        with self._lock:
            if path in self._cache:
                self._cache_bytes -= self._cache[path].nbytes
                del self._cache[path]
            self._cache[path] = samples
            self._cache_bytes += samples.nbytes
            while self._cache_bytes > CACHE_MAX_BYTES and len(self._cache) > 1:
                # Не вытесняем текущий трек — он нужен здесь и сейчас.
                oldest = next(k for k in self._cache if k != self._current_path)
                self._cache_bytes -= self._cache[oldest].nbytes
                del self._cache[oldest]

    def _cache_get(self, path):
        with self._lock:
            samples = self._cache.get(path)
            if samples is not None:
                self._cache.move_to_end(path)
            return samples

    def _on_preloaded(self, path, raw_samples):
        self._cache_put(path, raw_samples)

    def _cleanup_thread(self, thread):
        if thread in self._loader_threads:
            self._loader_threads.remove(thread)
        if thread.parent() is self:
            thread.deleteLater()

    def setSource(self, url_or_path):
        path = url_or_path.toLocalFile() if hasattr(url_or_path, "toLocalFile") else str(url_or_path)
        self.load(path)

    def load(self, path: str):
        with self._lock:
            self._current_path = path
            cached = self._cache_get(path)

        if cached is not None:
            with self._lock:
                self._raw_samples = cached
            self._finish_loading()
        else:
            with self._lock:
                self._status = QMediaPlayer.MediaStatus.LoadingMedia
            self.mediaStatusChanged.emit(self._status)
            thread = AudioLoaderThread(path, self)
            thread.loaded.connect(self._on_loader_success)
            thread.error.connect(self._on_loader_error)
            thread.finished.connect(lambda: self._cleanup_thread(thread))
            self._loader_threads.append(thread)
            thread.start()

    def _on_loader_success(self, path, raw_samples):
        with self._lock:
            if path != self._current_path:
                return
            self._raw_samples = raw_samples
        self._cache_put(path, raw_samples)
        self._finish_loading()

    def _on_loader_error(self, path, error_text):
        with self._lock:
            if path != self._current_path:
                return
            self._raw_samples = None
            self._samples = None
            self._duration_ms = 0
            self._status = QMediaPlayer.MediaStatus.InvalidMedia
        logger.warning("Не удалось загрузить %s: %s", path, error_text)
        self.durationChanged.emit(0)
        self.mediaStatusChanged.emit(self._status)
        self.errorOccurred.emit(path, error_text)

    def _finish_loading(self):
        self._apply_speed(self._speed, reset_position=True)
        with self._lock:
            self._status = QMediaPlayer.MediaStatus.LoadedMedia
            playing = self._state == QMediaPlayer.PlaybackState.PlayingState
        self.mediaStatusChanged.emit(self._status)
        if playing:
            self.play()

    def set_speed(self, speed: float):
        speed = max(0.5, min(2.0, float(speed)))
        if abs(speed - self._speed) < 1e-3:
            return
        self._apply_speed(speed, reset_position=False)

    def get_speed(self) -> float:
        return self._speed

    def _apply_speed(self, speed: float, reset_position: bool):
        old_ratio = 0.0
        with self._lock:
            if not reset_position and self._samples is not None and len(self._samples) > 0:
                old_ratio = self._frame_pos / len(self._samples)
            self._speed = speed
            raw_samples = self._raw_samples

        if raw_samples is None:
            return

        # Тяжёлое вычисление (librosa может занять секунды) — ВНЕ блокировки,
        # иначе аудио-поток замокается на всё время и пойдут клики.
        if abs(speed - 1.0) < 1e-3:
            new_samples = raw_samples
        elif librosa is not None:
            stretched = [
                librosa.effects.time_stretch(np.ascontiguousarray(raw_samples[:, ch]), rate=speed)
                for ch in range(CHANNELS)
            ]
            min_len = min(len(s) for s in stretched)
            new_samples = np.stack([s[:min_len] for s in stretched], axis=1).astype(np.float32)
        else:
            n_out = max(1, int(len(raw_samples) / speed))
            src_idx = np.linspace(0, len(raw_samples) - 1, n_out)
            base_idx = np.arange(len(raw_samples))
            new_samples = np.stack([
                np.interp(src_idx, base_idx, raw_samples[:, ch])
                for ch in range(CHANNELS)
            ], axis=1).astype(np.float32)

        with self._lock:
            self._samples = new_samples
            # Сброс состояний фильтров выполняет сам аудио-поток.
            self.equalizer.request_reset()
            self._frame_pos = 0 if reset_position else int(old_ratio * len(new_samples))
            self._duration_ms = int(len(new_samples) / SAMPLE_RATE * 1000)
        self.durationChanged.emit(self._duration_ms)

    def play(self):
        with self._lock:
            if self._samples is None:
                # Трек ещё грузится: состояние всё равно сообщаем, иначе кнопка
                # Play не отреагирует, а по завершении загрузки _finish_loading
                # снова вызовет play() и доведёт до конца.
                self._state = QMediaPlayer.PlaybackState.PlayingState
                started = False
            else:
                if self._sink is None:
                    self._sink = QAudioSink(self._format)
                    self._sink.setVolume(self._volume)
                    if not self._device.isOpen():
                        self._device.open(QIODevice.OpenModeFlag.ReadOnly)
                    self._sink.start(self._device)
                else:
                    self._sink.resume()
                self._state = QMediaPlayer.PlaybackState.PlayingState
                started = True
            self.playbackStateChanged.emit(self._state)
        if started:
            self._pos_timer.start()

    def pause(self):
        with self._lock:
            if self._sink is not None:
                self._sink.suspend()
            self._state = QMediaPlayer.PlaybackState.PausedState
            self.playbackStateChanged.emit(self._state)
        self._pos_timer.stop()

    def stop(self):
        with self._lock:
            if self._sink is not None:
                self._sink.stop()
                self._sink = None
            self._frame_pos = 0
            self._state = QMediaPlayer.PlaybackState.StoppedState
            self.playbackStateChanged.emit(self._state)
        self._pos_timer.stop()

    def playbackState(self):
        return self._state

    def setVolume(self, value: float):
        self._volume = max(0.0, min(1.0, float(value)))
        if self._sink is not None:
            self._sink.setVolume(self._volume)

    def volume(self) -> float:
        return self._volume

    def position(self) -> int:
        with self._lock:
            if self._samples is None:
                return 0
            return int(self._frame_pos / SAMPLE_RATE * 1000)

    def duration(self) -> int:
        with self._lock:
            return self._duration_ms

    def setPosition(self, ms: int):
        with self._lock:
            if self._samples is None:
                return
            self._frame_pos = max(0, min(max(0, len(self._samples) - 1), int(ms / 1000 * SAMPLE_RATE)))
            self.equalizer.request_reset()
            position = int(self._frame_pos / SAMPLE_RATE * 1000)
        self.positionChanged.emit(position)

    def _emit_position(self):
        self.positionChanged.emit(self.position())

    def _produce_bytes(self, maxlen: int) -> bytes:
        # Шаг 1 — строго под замком: снять копию блока и продвинуть позицию.
        # Всё, что мутирует общее состояние (_frame_pos, ссылки на сэмплы), — здесь.
        finished_empty = False
        reached_end = False

        with self._lock:
            if self._samples is None or self._state != QMediaPlayer.PlaybackState.PlayingState:
                return bytes(maxlen)

            bytes_per_frame = 2 * CHANNELS
            n_frames = max(1, maxlen // bytes_per_frame)
            start = self._frame_pos
            end = start + n_frames
            block = self._samples[start:end]

            if len(block) == 0:
                finished_empty = True
            else:
                reached_end = len(block) < n_frames
                if reached_end:
                    pad = np.zeros((n_frames - len(block), CHANNELS), dtype=np.float32)
                    block = np.concatenate([block, pad])
                else:
                    # Копия: дальше блок обрабатывается БЕЗ замка, а сам
                    # self._samples может быть подменён (смена трека/скорости).
                    block = block.copy()

                self._frame_pos = min(end, len(self._samples))

        # Сигнал конца трека шлём ПОСЛЕ освобождения замка: раньше здесь стоял
        # QTimer.singleShot(0, ...) из аудио-потока, где у Qt может не быть
        # рабочего цикла событий — и трек «не заканчивался».
        if finished_empty:
            self._on_track_finished()
            return bytes(0)

        # Шаг 2 — вне замка: вся DSP-цепочка (ЭК, спатиализация, FFT).
        # Состояние эквалайзера и спектроанализатора принадлежит аудио-потоку,
        # GUI лишь ставит флаг request_reset() — гонок нет. Блок уже скопирован,
        # поэтому pause()/stop()/setPosition() из GUI-потока не ждут ЭК.
        processed = self.equalizer.process(block)
        processed = self.spatial_dsp.process(processed)

        mono = processed.mean(axis=1).astype(np.float32)
        self.spectrum.push(mono)

        pcm16 = np.clip(processed * 32767.0, -32768.0, 32767.0).astype(np.int16)
        data = pcm16.tobytes()

        if reached_end:
            self._on_track_finished()
        return data

    def _on_track_finished(self):
        with self._lock:
            if self._status == QMediaPlayer.MediaStatus.EndOfMedia:
                return
            self._status = QMediaPlayer.MediaStatus.EndOfMedia
        # Сигнал — уже вне замка: слоты GUI вправе звать engine.play()/stop().
        self.mediaStatusChanged.emit(self._status)

    def shutdown(self):
        """Остановить движок перед закрытием приложения: таймер позиции,
        звуковой sink и фоновые загрузчики (иначе QThread падает
        с «Destroyed while running»)."""
        self._pos_timer.stop()
        self.stop()
        for thread in list(self._loader_threads):
            if thread.wait(3000):
                self._cleanup_thread(thread)
            else:
                logger.warning("Загрузчик не завершился за 3 с: %s", thread.path)
