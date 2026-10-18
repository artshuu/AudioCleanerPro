import os

os.environ.setdefault('VOSK_LOG_LEVEL', 'ERROR')
import sys
import json
import time
import logging
import warnings
from logging.handlers import RotatingFileHandler
from pathlib import Path
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import numpy as np
import soundfile as sf
import torch
import torchaudio.transforms as T
from scipy.ndimage import maximum_filter1d, uniform_filter1d
from scipy.signal import butter, filtfilt
from vosk import Model, KaldiRecognizer, SetLogLevel

from config_utils import get_app_data_dir

warnings.filterwarnings('ignore')
torch.set_grad_enabled(False)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError, ValueError):
        pass

try:
    SetLogLevel(-1)
except Exception:
    pass

try:
    from silero_vad import load_silero_vad, get_speech_timestamps
    SILERO_AVAILABLE = True
except ImportError:
    SILERO_AVAILABLE = False


def _resource_bases() -> list[Path]:
    bases: list[Path] = []
    if hasattr(sys, '_MEIPASS'):
        bases.append(Path(sys._MEIPASS))
    if getattr(sys, 'frozen', False):
        bases.append(Path(sys.executable).resolve().parent)
    bases.append(Path(__file__).resolve().parent)
    return bases


def get_resource_path(relative_path: str) -> str:
    """Ищет ресурс и рядом с EXE, и внутри bundle (_internal у PyInstaller 6)."""
    bases = _resource_bases()
    for base in bases:
        full_path = base / relative_path
        if full_path.exists():
            return str(full_path.absolute())

    searched = ", ".join(str(base / relative_path) for base in bases)
    raise FileNotFoundError(f"Ресурс не найден: {relative_path}. Проверены пути: {searched}")


def find_vosk_model_dir() -> Path | None:
    """Возвращает лучшую доступную модель Vosk.

    Приоритет — большие модели (точнее), затем small. Достаточно положить папку
    модели рядом с EXE/скриптом, переименовывать код не нужно.
    """
    preferred = ("vosk-model-ru-0.42", "vosk-model-ru-0.22", "vosk-model-small-ru-0.22")
    for base in _resource_bases():
        for name in preferred:
            candidate = base / name
            if (candidate / "am").exists() and (candidate / "conf").exists() and (candidate / "graph").exists():
                return candidate
        for candidate in sorted(base.glob("vosk-model*")):
            if candidate.is_dir() and (candidate / "am").exists():
                return candidate
    return None


def _format_duration(seconds: float) -> str:
    total_sec = int(seconds)
    m, s = divmod(total_sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _chunked_mean(data: np.ndarray, block: int = 1_048_576) -> float:
    """Среднее без создания float64-копии всего файла."""
    total = 0.0
    for start in range(0, len(data), block):
        total += float(np.sum(data[start:start + block], dtype=np.float64))
    return total / max(1, len(data))


def _chunked_rms(data: np.ndarray, block: int = 1_048_576) -> float:
    total = 0.0
    for start in range(0, len(data), block):
        chunk = data[start:start + block]
        total += float(np.sum(np.square(chunk, dtype=np.float64)))
    return float(np.sqrt(total / max(1, len(data))))


def _configure_logger() -> logging.Logger:
    app_logger = logging.getLogger('audio_cleaner')
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False
    if app_logger.handlers:
        return app_logger

    preferred_dir = get_app_data_dir() if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
    candidates = [preferred_dir, get_app_data_dir()]
    for log_dir in candidates:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            handler: logging.Handler = RotatingFileHandler(
                log_dir / 'audio_cleaner.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
            )
        except OSError:
            continue
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        app_logger.addHandler(handler)
        return app_logger

    app_logger.addHandler(logging.NullHandler())
    return app_logger


logger = _configure_logger()

# Корни мата: ловят словоформы, которых может не быть в words.txt (блять, нахуй, заебал...).
PROFANITY_ROOTS = (
    "бля", "бляд",
    "хуй", "хуя", "хую", "хуе", "хуйн",
    "пизд",
    "ебал", "ебат", "ебан", "ебет", "ебн", "ебу", "ебуч", "ебли", "ебл",
    "заеб", "наеб", "уеб", "отъеб", "въеб", "выеб", "доеб", "проеб", "разъеб", "съеб", "подъеб",
    "охуе", "ахуе", "охуи",
    "сука", "суки", "суке", "суку", "сукой", "суч", "сучк",
    "мудак", "мудач", "мудил", "мудозвон",
    "наху", "нахе", "нахр",
    "пидор", "пидар", "пидр",
    "долбо", "шлюх", "говн", "жоп",
)

# Омографы и безобидные слова: даже если совпали с корнем/словарём — не цензурируем.
FALSE_POSITIVE_WORDS = {
    "сукно", "сукин", "сучок", "рука", "наука",
    "хутор", "хутора", "худой", "худо", "куй",
    "пистон", "писта", "быстро", "висит",
    "блин", "блеск", "близ", "блюд", "боля",
    "бляха", "бляхи", "бляху", "бляхой", "бляшка", "бляшки",
    "мутки", "мутка", "мутко", "мудрый", "мудрость",
    "батя", "бати", "бадя",
}

FALSE_POSITIVE_PREFIXES = (
    "бляха", "бляхи", "бляху", "бляхо", "бляшк", "блямб",
    "сучковат", "сучкоруб", "сучконож",
)

# Слишком неуверенное распознавание не цензурируем, чтобы не «мяукать» на обычные слова.
MIN_WORD_CONFIDENCE = 0.25

# Распознавание идёт блоками: это даёт прогресс и параллелизм на длинных файлах.
RECOGNITION_BLOCK_SEC = 20.0
RECOGNITION_OVERLAP_SEC = 1.0
RECOGNITION_DEDUP_SEC = 0.2
RECOGNITION_WORKERS = 4
VOSK_FEED_CHUNK_BYTES = 8000


def resolve_processing_config(preset: str, noise_reduction: float, padding_ms: int) -> dict:
    """Приводит пользовательские параметры к диапазонам выбранного пресета."""
    preset = preset or "balanced"
    if preset == "fast":
        return {
            "noise_reduction": max(0.2, min(0.45, float(noise_reduction))),
            "padding_ms": max(40, min(80, int(padding_ms))),
            "chunk_sec": 20,
            "overlap_sec": 0.5,
        }
    if preset == "quality":
        return {
            "noise_reduction": max(0.55, min(0.9, float(noise_reduction))),
            "padding_ms": max(120, int(padding_ms)),
            "chunk_sec": 40,
            "overlap_sec": 1.5,
        }
    return {
        "noise_reduction": max(0.35, min(0.7, float(noise_reduction))),
        "padding_ms": max(80, min(140, int(padding_ms))),
        "chunk_sec": 30,
        "overlap_sec": 1.0,
    }


class AudioProcessor:
    FALLBACK_PROFANITY = {'бля', 'хуй', 'пизд', 'еба', 'сука', 'нах', 'мудак'}

    def __init__(self):
        self.dict_warning = None
        self.profanity_set = set(self.FALLBACK_PROFANITY)
        try:
            dict_path = get_resource_path("words.txt")
            with open(dict_path, "r", encoding="utf-8") as f:
                lines = [line.strip().lower().replace("ё", "е") for line in f if line.strip()]
            if lines:
                self.profanity_set = set(lines)
            else:
                self.dict_warning = "⚠ words.txt пуст! Цензура работает в ограниченном режиме (7 базовых слов). "
        except (FileNotFoundError, OSError):
            self.dict_warning = "⚠ words.txt не найден! Цензура работает в ограниченном режиме (7 базовых слов). "

        self.vosk_model = None
        self.vosk_model_name = None
        self.vosk_warning = None
        model_dir = find_vosk_model_dir()
        if model_dir is None:
            self.vosk_warning = (
                "⚠ Модель Vosk не найдена (ожидается папка vosk-model-small-ru-0.22). Цензура отключена. "
            )
        else:
            try:
                self.vosk_model = Model(str(model_dir))
                self.vosk_model_name = model_dir.name
            except Exception as e:
                self.vosk_warning = f"⚠ Vosk модель недоступна: {e}. Цензура отключена. "

        self.vad_model = None
        self.vad_warning = None
        if SILERO_AVAILABLE:
            try:
                self.vad_model = load_silero_vad()
            except Exception as e:
                self.vad_warning = f"⚠ Silero VAD не загружен: {e}. Замена тишины отключена. "
        else:
            self.vad_warning = "⚠ Silero VAD не установлен. Замена тишины отключена. (pip install silero-vad)"

        self.last_entries = []

    def _log(self, message: str, log_cb=None):
        logger.info(message)
        if log_cb:
            log_cb(message)
        try:
            print(message)
        except (UnicodeEncodeError, OSError, ValueError):
            pass

    def _check_cancel(self, cancel_event):
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Обработка прервана пользователем")

    def _format_srt_time(self, seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds - int(seconds)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    @staticmethod
    def _crossfade_ramp(length: int) -> np.ndarray:
        """Рампа 0→1 без достижения единицы: сумма с (1 - ramp) всегда равна 1."""
        if length <= 0:
            return np.empty(0, dtype=np.float32)
        return np.linspace(0.0, 1.0, length, endpoint=False, dtype=np.float32)

    def _preprocess_audio(self, data: np.ndarray, sr: int, log_cb=None,
                          cancel_event=None) -> np.ndarray:
        self._log("🎛 Предобработка (сохранение тембра)... ", log_cb)
        data = np.asarray(data, dtype=np.float32)
        if data.size == 0:
            return data

        data = data - np.float32(_chunked_mean(data))

        max_val = float(np.max(np.abs(data)))
        if max_val > 0.95:
            self._log("   ✂ Исправление клиппинга... ", log_cb)
            data = (np.tanh(data * 1.5) / np.tanh(1.5)).astype(np.float32)

        rms = _chunked_rms(data)
        if rms > 0:
            target_rms = 10 ** (-18.0 / 20)
            gain = min(target_rms / rms, 6.0)
            data = (data * np.float32(gain)).astype(np.float32, copy=False)
            self._log(f"   🔊 Нормализация: RMS={rms:.4f} → {target_rms:.4f}", log_cb)

        nyq = sr / 2.0
        cutoff_norm = 80.0 / nyq
        if 0 < cutoff_norm < 1.0 and len(data) > 32:
            b, a = butter(4, cutoff_norm, btype='high')
            chunk_len = max(32, int(60 * sr))
            overlap_len = min(int(1.0 * sr), chunk_len // 4)

            if len(data) <= chunk_len:
                result = filtfilt(b, a, data.astype(np.float64)).astype(np.float32)
            else:
                step = max(1, chunk_len - overlap_len)
                ramp = self._crossfade_ramp(overlap_len)
                result = np.zeros(len(data), dtype=np.float32)
                deferred_tail = None
                prev_end = None

                for start in range(0, len(data), step):
                    self._check_cancel(cancel_event)
                    end = min(start + chunk_len, len(data))
                    filtered = filtfilt(b, a, data[start:end].astype(np.float64)).astype(np.float32)

                    head = min(len(filtered), overlap_len, (prev_end - start) if prev_end is not None else 0)
                    if head > 0 and deferred_tail is not None:
                        fade = ramp[:head]
                        result[start:start + head] = (
                            deferred_tail[:head] * (1.0 - fade) + filtered[:head] * fade
                        )

                    next_start = start + step if start + step < len(data) else None
                    tail = min(
                        len(filtered) - head, overlap_len,
                        (end - next_start) if next_start is not None else 0,
                    )
                    body_end = end - tail
                    if body_end > start + head:
                        result[start + head:body_end] = filtered[head:len(filtered) - tail if tail else len(filtered)]
                    deferred_tail = filtered[-tail:].copy() if tail > 0 else None
                    prev_end = end

                data = result

        peak = float(np.max(np.abs(data))) if data.size else 0.0
        if peak > 0.99:
            data = (data * np.float32(0.99 / peak)).astype(np.float32, copy=False)

        self._log("✅ Предобработка завершена ", log_cb)
        return data


    def _restore_envelope(self, original: np.ndarray, processed: np.ndarray,
                          sr: int, window_ms: int = 20, block_sec: float = 30.0,
                          overlap_sec: float = 0.5, cancel_event=None) -> np.ndarray:
        """Восстанавливает динамику блоками, не выделяя массивы на весь файл."""
        processed = np.asarray(processed, dtype=np.float32)
        n = min(len(original), len(processed))
        if n == 0:
            return processed.copy()

        win_len = max(1, int(sr * window_ms / 1000))
        smooth_size = max(1, win_len // 4)
        block_len = max(win_len * 4, int(sr * block_sec))
        overlap_len = min(max(win_len, int(sr * overlap_sec)), block_len // 2)
        step = max(1, block_len - overlap_len)
        ramp = self._crossfade_ramp(overlap_len)

        restored = np.zeros(n, dtype=np.float32)
        eps = np.float32(1e-8)
        deferred_tail = None
        prev_end = None

        for start in range(0, n, step):
            self._check_cancel(cancel_event)
            end = min(start + block_len, n)
            orig_seg = np.asarray(original[start:end], dtype=np.float32)
            proc_seg = processed[start:end]

            if end - start < win_len * 2:
                seg = proc_seg
            else:
                orig_env = uniform_filter1d(
                    maximum_filter1d(np.abs(orig_seg), size=win_len, mode='nearest'),
                    size=smooth_size, mode='nearest')
                proc_env = uniform_filter1d(
                    maximum_filter1d(np.abs(proc_seg), size=win_len, mode='nearest'),
                    size=smooth_size, mode='nearest')

                ratio = np.ones(end - start, dtype=np.float32)
                mask = proc_env > eps
                ratio[mask] = orig_env[mask] / proc_env[mask]
                np.clip(ratio, np.float32(0.5), np.float32(2.0), out=ratio)
                seg = proc_seg * ratio

            head = min(len(seg), overlap_len, (prev_end - start) if prev_end is not None else 0)
            if head > 0 and deferred_tail is not None:
                fade = ramp[:head]
                restored[start:start + head] = deferred_tail[:head] * (1.0 - fade) + seg[:head] * fade

            next_start = start + step if start + step < n else None
            tail = min(
                len(seg) - head, overlap_len,
                (end - next_start) if next_start is not None else 0,
            )
            body_end = end - tail
            if body_end > start + head:
                restored[start + head:body_end] = seg[head:len(seg) - tail if tail else len(seg)]
            deferred_tail = seg[-tail:].copy() if tail > 0 else None
            prev_end = end

        peak = float(np.max(np.abs(restored))) if restored.size else 0.0
        if peak > 0.99:
            restored *= np.float32(0.99 / peak)
        return restored

    def _soft_limit(self, data: np.ndarray, peak: float = 0.99, threshold: float = 0.8) -> np.ndarray:
        """Мягкое ограничение пиков: тихие сэмплы не трогаем, громкие поджимаем по tanh."""
        limited = np.asarray(data, dtype=np.float32).copy()
        if limited.size == 0:
            return limited

        span = max(peak - threshold, 1e-6)
        block = 1_048_576
        for start in range(0, limited.size, block):
            segment = limited[start:start + block]
            abs_segment = np.abs(segment)
            mask = abs_segment > threshold
            if np.any(mask):
                over = (abs_segment[mask] - threshold) / span
                segment[mask] = np.sign(segment[mask]) * (threshold + span * np.tanh(over)).astype(np.float32)

        np.clip(limited, -peak, peak, out=limited)
        return limited


    def _estimate_denoise_strength(self, speech_ratio: float, noise_ratio: float, base_strength: float) -> float:
        speech_ratio = float(np.clip(speech_ratio, 0.0, 1.0))
        noise_ratio = float(np.clip(noise_ratio, 0.0, 1.0))
        base_strength = float(np.clip(base_strength, 0.05, 0.95))
        strength = base_strength - 0.35 * speech_ratio + 0.2 * noise_ratio + 0.1
        return float(np.clip(strength, 0.1, 0.95))

    def _should_skip_denoise(self, chunk_data: np.ndarray, speech_ratio: float, noise_ratio: float) -> bool:
        if len(chunk_data) == 0:
            return True
        rms = float(np.sqrt(np.mean(np.square(chunk_data))))
        if rms < 1e-4:
            return True
        if rms < 2e-3 and speech_ratio < 0.1 and noise_ratio > 0.8:
            return True
        return False

    def _get_speech_segments(self, data: np.ndarray, sr: int, log_cb=None) -> list[tuple[int, int]]:
        if self.vad_model is None:
            return []

        target_sr = 16000
        if sr != target_sr:
            resampler = T.Resample(sr, target_sr)
            resampled = resampler(torch.from_numpy(data).float().unsqueeze(0)).squeeze(0)
        else:
            resampled = torch.from_numpy(data).float()

        timestamps = get_speech_timestamps(resampled, self.vad_model, sampling_rate=target_sr,
                                           min_speech_duration_ms=200, speech_pad_ms=80)
        ratio = sr / target_sr
        segments = []
        for ts in timestamps:
            s = max(0, min(int(ts['start'] * ratio), len(data)))
            e = max(0, min(int(ts['end'] * ratio), len(data)))
            if e > s:
                segments.append((s, e))
        speech_sec = sum(e - s for s, e in segments) / sr if sr else 0.0
        self._log(f"   🗣 VAD: {len(segments)} сегментов, речь {_format_duration(speech_sec)}", log_cb)
        return segments

    def _get_worker_count(self, sample_count: int, sr: int) -> int:
        cpu_count = max(1, os.cpu_count() or 1)
        if sample_count <= max(1, sr * 15):
            return 1
        if sample_count <= max(1, sr * 120):
            return min(2, cpu_count)
        return min(4, cpu_count)

    def _denoise_chunked(self, data: np.ndarray, sr: int, prop_decrease: float,
                         chunk_sec: int = 30, overlap_sec: float = 1.0,
                         progress_start=0.0, progress_end=0.0,
                         progress_cb=None, log_cb=None, cancel_event=None,
                         speech_segments=None) -> np.ndarray:
        import noisereduce as nr

        data = np.asarray(data, dtype=np.float32)
        n = len(data)
        if n == 0:
            return data.copy()

        self._log("   🔇 VAD-guided noisereduce: адаптивное шумоподавление... ", log_cb)

        use_vad = self.vad_model is not None
        speech_mask = None
        if use_vad:
            if speech_segments is None:
                speech_segments = self._get_speech_segments(data, sr, log_cb)
            speech_mask = np.zeros(n, dtype=bool)
            for s, e in speech_segments:
                speech_mask[max(0, s):min(n, e)] = True
            speech_pct = float(np.mean(speech_mask)) * 100
            self._log(f"   🗣 Речь: {speech_pct:.1f}% | Тишина: {100 - speech_pct:.1f}%", log_cb)
        else:
            self._log("   ℹ VAD недоступен, применяется равномерное шумоподавление ", log_cb)

        chunk_len = max(1, int(chunk_sec * sr))
        overlap_len = int(overlap_sec * sr)
        if overlap_len >= chunk_len:
            overlap_len = chunk_len // 4
        step = max(1, chunk_len - overlap_len)
        spans = [(i, min(i + chunk_len, n)) for i in range(0, n, step)]
        total = len(spans)
        num_workers = self._get_worker_count(n, sr)
        self._log(f"   Обработка: {total} чанков ({num_workers} потоков)... ", log_cb)

        def _process_chunk(idx: int, start: int, end: int):
            chunk_data = data[start:end]
            if use_vad:
                chunk_speech = speech_mask[start:end]
                speech_ratio = float(np.mean(chunk_speech)) if len(chunk_speech) else 0.0
                chunk_rms = float(np.sqrt(np.mean(np.square(chunk_data), dtype=np.float32))) if len(chunk_data) else 0.0
                noise_ratio = float(np.clip(1.0 - min(1.0, chunk_rms / 0.03), 0.0, 1.0))
                if self._should_skip_denoise(chunk_data, speech_ratio, noise_ratio):
                    return idx, chunk_data.copy()
                adaptive_prop = self._estimate_denoise_strength(speech_ratio, noise_ratio, prop_decrease)
                clean = nr.reduce_noise(y=chunk_data, sr=sr, prop_decrease=adaptive_prop)
            else:
                clean = nr.reduce_noise(y=chunk_data, sr=sr, prop_decrease=prop_decrease)
            return idx, np.asarray(clean, dtype=np.float32)

        output = np.zeros(n, dtype=np.float32)
        ramp = self._crossfade_ramp(overlap_len)
        completed = 0
        pending: dict[int, object] = {}
        submitted = 0
        window = max(1, num_workers * 2)
        deferred_tail: np.ndarray | None = None
        prev_end: int | None = None

        executor = ThreadPoolExecutor(max_workers=num_workers)
        try:
            for idx in range(total):
                self._check_cancel(cancel_event)
                while submitted < total and submitted < idx + window:
                    start, end = spans[submitted]
                    pending[submitted] = executor.submit(_process_chunk, submitted, start, end)
                    submitted += 1

                _, seg = pending.pop(idx).result()
                start, end = spans[idx]

                head = min(overlap_len, len(seg), (prev_end - start) if prev_end is not None else 0)
                if head > 0 and deferred_tail is not None:
                    fade = ramp[:head]
                    output[start:start + head] = deferred_tail[:head] * (1.0 - fade) + seg[:head] * fade
                deferred_tail = None

                next_start = spans[idx + 1][0] if idx + 1 < total else None
                tail = min(overlap_len, len(seg) - head, (end - next_start) if next_start is not None else 0)
                body_end = end - tail
                if body_end > start + head:
                    output[start + head:body_end] = seg[head:len(seg) - tail if tail else len(seg)]
                if tail > 0:
                    deferred_tail = seg[-tail:].copy()

                prev_end = end
                completed += 1
                if progress_cb and total:
                    pct = progress_start + (progress_end - progress_start) * (completed / total)
                    progress_cb(pct)
        finally:
            for future in pending.values():
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)

        return output

    @staticmethod
    def _normalize_word(word: str) -> str:
        return ''.join(c for c in (word or '').lower() if c.isalpha()).replace('ё', 'е')

    @staticmethod
    def _is_whitelisted(clean_word: str) -> bool:
        if not clean_word:
            return False
        if clean_word in FALSE_POSITIVE_WORDS:
            return True
        return any(clean_word.startswith(prefix) for prefix in FALSE_POSITIVE_PREFIXES)

    def _is_profanity(self, word_text: str) -> bool:
        clean = self._normalize_word(word_text)
        if not clean or self._is_whitelisted(clean):
            return False
        if clean in self.profanity_set:
            return True
        return any(clean.startswith(root) for root in PROFANITY_ROOTS)

    def _detect_non_speech(self, data, sr, log_cb=None, cancel_event=None, speech_segments=None):
        if self.vad_model is None:
            self._log("ℹ VAD отключён, тишина не удаляется ", log_cb)
            return data, 0.0

        self._log("🔍 Поиск неречевых звуков (Silero VAD)... ", log_cb)
        self._check_cancel(cancel_event)

        if speech_segments is None:
            speech_segments = self._get_speech_segments(data, sr, log_cb)

        if not speech_segments:
            self._log("⚠ VAD не нашёл речи — аудио оставлено без изменений ", log_cb)
            return data, 0.0

        speech_total = sum(max(0, e - s) for s, e in speech_segments)
        if speech_total < max(1, int(0.02 * len(data))):
            self._log("⚠ VAD нашёл менее 2% речи — тишина не заменялась (защита от сбоя VAD) ", log_cb)
            return data, 0.0

        result = np.asarray(data, dtype=np.float32)
        fade_ms = 50
        fade_samples = int(sr * fade_ms / 1000)

        muted = 0
        last_end = 0

        for s, e in speech_segments:
            self._check_cancel(cancel_event)
            s = max(0, min(s, len(result)))
            e = max(0, min(e, len(result)))

            if s > last_end:
                gap_len = s - last_end
                safe_fade = min(fade_samples, gap_len // 3)

                if safe_fade > 0:
                    result[last_end:last_end + safe_fade] *= np.linspace(1, 0, safe_fade, dtype=np.float32)
                    result[s - safe_fade:s] *= np.linspace(0, 1, safe_fade, dtype=np.float32)
                    if gap_len > 2 * safe_fade:
                        result[last_end + safe_fade:s - safe_fade] = 0
                else:
                    result[last_end:s] *= np.linspace(1, 0, gap_len, dtype=np.float32)

                muted += gap_len

            last_end = e

        if last_end < len(result):
            tail_len = len(result) - last_end
            safe_fade = min(fade_samples, tail_len)

            if safe_fade > 0:
                result[last_end:last_end + safe_fade] *= np.linspace(1, 0, safe_fade, dtype=np.float32)
            if tail_len > safe_fade:
                result[last_end + safe_fade:] = 0

            muted += tail_len

        muted_sec = muted / sr
        self._log(f"✅ Заменено тишиной: {_format_duration(muted_sec)} (длительность сохранена)", log_cb)
        return result, muted_sec

    def _generate_meow(self, duration, sr):
        if duration <= 0:
            return np.empty(0, dtype=np.float32)
        t = np.linspace(0, duration / sr, duration, endpoint=False)
        freq = 800.0 - 400.0 * (t / (duration / sr))
        phase = 2 * np.pi * np.cumsum(freq) / sr
        env = np.sin(np.pi * t / (duration / sr)) ** 2
        return (env * (0.7 * np.sin(phase) + 0.3 * np.sin(2 * phase)) * 0.9).astype(np.float32)

    def _generate_beep(self, duration, sr):
        if duration <= 0:
            return np.empty(0, dtype=np.float32)
        t = np.linspace(0, duration / sr, duration, endpoint=False)
        return (0.8 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)

    def _apply_censor_with_fade(self, data: np.ndarray, start_idx: int, end_idx: int,
                                 censor_audio: np.ndarray, sr: int, fade_ms: int = 15) -> np.ndarray:
        length = end_idx - start_idx
        if length <= 0 or len(censor_audio) == 0:
            return data

        if len(censor_audio) > length:
            censor_audio = censor_audio[:length]
        elif len(censor_audio) < length:
            censor_audio = np.pad(censor_audio, (0, length - len(censor_audio)), mode='constant')

        fade_samples = int(sr * fade_ms / 1000)
        fade_samples = min(fade_samples, length // 3)

        if fade_samples > 0:
            fade_in = np.linspace(0, 1, fade_samples, dtype=np.float32)
            fade_out = np.linspace(1, 0, fade_samples, dtype=np.float32)

            data[start_idx:start_idx + fade_samples] *= fade_out
            censor_audio[:fade_samples] *= fade_in

            data[end_idx - fade_samples:end_idx] *= fade_in
            censor_audio[-fade_samples:] *= fade_out

        data[start_idx:end_idx] += censor_audio

        return data


    @staticmethod
    def _dedupe_recognized_words(words: list[dict]) -> list[dict]:
        """Убирает слова, дважды распознанные в перекрывающихся блоках.

        Предпочитаем вариант из блока, где слово дальше от границы (больше контекста),
        и только потом — с большей уверенностью.
        """
        ordered = sorted(words, key=lambda item: (item.get('start', 0.0), item.get('end', 0.0)))
        deduped: list[dict] = []
        for word in ordered:
            duplicate = None
            for previous in reversed(deduped[-3:]):
                if (previous['word'].lower() == word['word'].lower()
                        and abs(previous['start'] - word['start']) <= RECOGNITION_DEDUP_SEC):
                    duplicate = previous
                    break
            if duplicate is None:
                deduped.append(word)
                continue
            new_score = (float(word.get('edge_distance', 0.0)), float(word.get('confidence', 0.0)))
            old_score = (float(duplicate.get('edge_distance', 0.0)), float(duplicate.get('confidence', 0.0)))
            if new_score > old_score:
                duplicate.update(word)
        for word in deduped:
            word.pop('edge_distance', None)
        return deduped

    def _recognize_words(self, data, sr, log_cb=None, cancel_event=None,
                         progress_cb=None, progress_start=0.0, progress_end=1.0):
        """Распознаёт весь файл блоками по 20 с.

        VAD-нарезка и шумодав для распознавания не используются: замеры на речи
        с шумом дали WER 15.8% на сыром аудио против 17.4–19.4% после
        предобработки/денойза и 19.4–23.0% с нарезкой по VAD.
        """
        if self.vosk_model is None or len(data) == 0:
            return []

        target_sr = 16000
        block_samples = max(1, int(RECOGNITION_BLOCK_SEC * sr))
        overlap_samples = max(0, int(RECOGNITION_OVERLAP_SEC * sr))
        blocks = []
        start = 0
        while start < len(data):
            end = min(start + block_samples, len(data))
            blocks.append((start, end))
            if end >= len(data):
                break
            start = max(start + 1, end - overlap_samples)

        total_samples = sum(end - start for start, end in blocks)
        workers = max(1, min(RECOGNITION_WORKERS, os.cpu_count() or 1, len(blocks)))
        self._log(
            f"   🎧 Распознавание: {_format_duration(total_samples / sr)}, "
            f"{len(blocks)} блоков, {workers} потоков, модель {self.vosk_model_name or 'Vosk'}...",
            log_cb,
        )

        def recognize_block(start: int, end: int):
            segment = data[start:end]
            if sr != target_sr:
                resampler = T.Resample(sr, target_sr)
                segment = resampler(torch.from_numpy(segment).float().unsqueeze(0)).squeeze(0).numpy()
            pcm = np.clip(segment * 32767.0, -32768, 32767).astype(np.int16)
            payload = memoryview(pcm).cast('b')

            rec = KaldiRecognizer(self.vosk_model, target_sr)
            rec.SetWords(True)
            words = []
            for offset in range(0, len(payload), VOSK_FEED_CHUNK_BYTES):
                self._check_cancel(cancel_event)
                chunk = bytes(payload[offset:offset + VOSK_FEED_CHUNK_BYTES])
                if rec.AcceptWaveform(chunk):
                    result = rec.Result()
                elif offset + VOSK_FEED_CHUNK_BYTES >= len(payload):
                    result = rec.FinalResult()
                else:
                    result = None
                if not result:
                    continue
                try:
                    parsed = json.loads(result)
                except Exception:
                    continue
                for item in parsed.get("result", []):
                    wt = item.get("word", "").strip()
                    if not wt:
                        continue
                    word_start = start / sr + item.get('start', 0.0)
                    word_end = start / sr + item.get('end', 0.0)
                    words.append({
                        'word': wt,
                        'start': word_start,
                        'end': word_end,
                        'confidence': item.get('conf', 0),
                        'edge_distance': min(word_start - start / sr, end / sr - word_end),
                    })
            return words

        results = [None] * len(blocks)
        futures = {}
        completed = 0
        next_submit = 0
        done_samples = 0
        next_report = max(1, int(total_samples * 0.05))
        started_at = time.time()
        window = max(1, workers * 2)

        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            while next_submit < len(blocks) or futures:
                self._check_cancel(cancel_event)
                while next_submit < len(blocks) and next_submit < completed + window:
                    start, end = blocks[next_submit]
                    futures[executor.submit(recognize_block, start, end)] = next_submit
                    next_submit += 1

                finished, _ = wait(set(futures), timeout=1.0, return_when=FIRST_COMPLETED)
                for future in finished:
                    idx = futures.pop(future)
                    results[idx] = future.result()
                    start, end = blocks[idx]
                    done_samples += end - start
                    completed += 1
                    if progress_cb:
                        progress_cb(progress_start + (progress_end - progress_start) * completed / len(blocks))
                    if done_samples >= next_report or completed == len(blocks):
                        pct = 100.0 * done_samples / max(1, total_samples)
                        elapsed = max(1e-6, time.time() - started_at)
                        speed = (done_samples / sr) / elapsed
                        eta = max(0.0, (total_samples - done_samples) / sr / speed) if speed > 0 else 0.0
                        self._log(
                            f"   🎧 Распознано: {_format_duration(done_samples / sr)} / "
                            f"{_format_duration(total_samples / sr)} ({pct:.0f}%), "
                            f"осталось ~{_format_duration(eta)}",
                            log_cb,
                        )
                        next_report += max(1, int(total_samples * 0.05))
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)

        all_words = [w for words in results if words for w in words]
        all_words = self._dedupe_recognized_words(all_words)
        for index, word in enumerate(all_words):
            word['index'] = index
        return all_words

    def _apply_censorship(self, data, sr, words, censor_mode, output_markers_path,
                          padding_ms=100, log_cb=None):
        self.last_entries = []
        if censor_mode == "off":
            self._log("🚫 Цензура отключена — слова не заменяются ", log_cb)
            if output_markers_path:
                self._remove_file_quietly(output_markers_path)
            return data, []

        if not words:
            self._log("ℹ Мат не найден — SRT не создан ", log_cb)
            if output_markers_path:
                self._remove_file_quietly(output_markers_path)
            return data, []

        ml = "МЯУ" if censor_mode == "meow" else "БИП"
        mi = "🐱" if censor_mode == "meow" else "📟"
        padding_ms = max(0, int(padding_ms))
        self._log(f"{mi} Цензура ({ml}, паддинг={padding_ms}мс)... ", log_cb)

        segs = []
        entries = []
        pad = int(sr * padding_ms / 1000)

        for w in words:
            wt = w['word'].strip()
            clean_wt = self._normalize_word(wt)
            if not self._is_profanity(clean_wt):
                continue

            confidence = float(w.get('confidence', 1.0) or 1.0)
            if 0.0 < confidence < MIN_WORD_CONFIDENCE:
                self._log(f"   ⚠ Пропущено (уверенность {confidence:.2f}): \"{wt}\" @ {w['start']:.2f}с", log_cb)
                continue

            s = max(0, int(w['start'] * sr) - pad)
            e = min(len(data), int(w['end'] * sr) + pad)

            if e > s:
                segs.append((s, e))
                entries.append({
                    'start': w['start'],
                    'end': w['end'],
                    'word': wt.lower(),
                    'precise': True
                })
                self._log(f"   {mi} {ml}: \"{wt}\" @ {w['start']:.2f}с", log_cb)

        if not entries:
            self._log("ℹ Мат не найден — SRT не создан ", log_cb)
            if output_markers_path:
                self._remove_file_quietly(output_markers_path)
            return data, []

        segs.sort()
        merged = []
        cs, ce = segs[0]
        for s, e in segs[1:]:
            if s <= ce:
                ce = max(ce, e)
            else:
                merged.append((cs, ce))
                cs, ce = s, e
        merged.append((cs, ce))

        generate = self._generate_meow if censor_mode == "meow" else self._generate_beep
        for s, e in merged:
            if e > s:
                data = self._apply_censor_with_fade(data, s, e, generate(e - s, sr), sr, fade_ms=15)

        self.last_entries = entries

        if output_markers_path:
            self._write_srt(output_markers_path, entries, ml, log_cb)
        unique = list(dict.fromkeys(e['word'] for e in entries))
        self._log(f"✅ Зацензурено: {len(entries)} ({', '.join(unique)})", log_cb)
        return data, entries

    @staticmethod
    def _remove_file_quietly(path) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    def _write_srt(self, path, entries, mode_lbl, log_cb):
        ec = sorted(entries, key=lambda x: x['start'])
        try:
            with open(path, 'w', encoding='utf-8') as f:
                for i, en in enumerate(ec, 1):
                    pt = "" if en['precise'] else " ≈ "
                    f.write(f"{i}\n{self._format_srt_time(en['start'])} --> {self._format_srt_time(en['end'])}\n[{mode_lbl}{pt}] {en['word']}\n\n")
            pc = sum(1 for e in ec if e['precise'])
            self._log(f"📍 Маркеры: {Path(path).name} ({pc} точных, {len(ec) - pc} ~)", log_cb)
        except Exception as ex:
            self._log(f"⚠ SRT Error: {ex}", log_cb)

    def export_srt(self, output_path: str, censor_mode: str = "meow", log_cb=None) -> bool:
        if not self.last_entries:
            self._log("ℹ Нет маркеров для экспорта ", log_cb)
            return False
        ml = "МЯУ" if censor_mode == "meow" else "БИП"
        self._write_srt(output_path, self.last_entries, ml, log_cb)
        return True

    def process(self, input_path, output_path, censor_mode="meow",
                noise_reduction=0.5, padding_ms=100,
                progress_cb=None, log_cb=None, cancel_event=None,
                processing_preset="balanced"):
        start_time = time.time()
        stats = {'profanity_count': 0, 'unique_words': [], 'elapsed': 0, 'muted_sec': 0.0}
        temp_path = None
        try:
            input_abs = Path(input_path).resolve()
            output_abs = Path(output_path).resolve()
            if input_abs == output_abs:
                message = "Входной и выходной файлы совпадают — выберите другой путь"
                self._log(f"❌ {message}", log_cb)
                return False, message, stats

            if self.dict_warning and censor_mode != "off":
                self._log(self.dict_warning, log_cb)
            if self.vosk_warning and censor_mode != "off":
                self._log(self.vosk_warning, log_cb)
            if self.vad_warning:
                self._log(self.vad_warning, log_cb)

            config = resolve_processing_config(processing_preset, noise_reduction, padding_ms)
            self._log(
                f"⚙ Пресет: {processing_preset} | шумоподавление={config['noise_reduction']:.2f} | "
                f"паддинг={config['padding_ms']}мс",
                log_cb,
            )
            self._log(f"📂 Чтение: {input_abs.name}", log_cb)
            data, sr = sf.read(str(input_abs), dtype='float32')

            if data.ndim > 1:
                data = np.mean(data, axis=1, dtype=np.float32)
                self._log("   Stereo → Mono ", log_cb)

            orig_len = len(data)
            if orig_len == 0:
                message = "Файл не содержит аудиоданных"
                self._log(f"❌ {message}", log_cb)
                return False, message, stats
            self._log(f"   Длительность: {_format_duration(orig_len / sr)} | SR: {sr} Гц ", log_cb)
            if progress_cb:
                progress_cb(0.02)

            # Распознаём ДО шумодава и без VAD-нарезки: так точнее (проверено по WER).
            words = []
            if censor_mode != "off" and self.vosk_model is not None:
                words = self._recognize_words(
                    data, sr, log_cb=log_cb, cancel_event=cancel_event,
                    progress_cb=progress_cb, progress_start=0.02, progress_end=0.45,
                )
                self._log(f"   🧠 Распознано слов: {len(words)}", log_cb)
            self._check_cancel(cancel_event)

            data = self._preprocess_audio(data, sr, log_cb, cancel_event)
            if progress_cb:
                progress_cb(0.50)

            self._log("🔇 Шумоподавление... ", log_cb)
            stage_start = time.time()
            speech_segments = self._get_speech_segments(data, sr, log_cb)
            cleaned = self._denoise_chunked(
                data, sr, config['noise_reduction'], chunk_sec=config['chunk_sec'], overlap_sec=config['overlap_sec'],
                progress_start=0.50, progress_end=0.68,
                progress_cb=progress_cb, log_cb=log_cb, cancel_event=cancel_event,
                speech_segments=speech_segments,
            )
            self._log("🔧 Восстановление динамики... ", log_cb)
            cleaned = self._restore_envelope(data, cleaned, sr, cancel_event=cancel_event)
            del data
            cleaned = self._soft_limit(cleaned)
            self._log(f"⏱ Шумоподавление заняло {_format_duration(time.time() - stage_start)}", log_cb)
            if progress_cb:
                progress_cb(0.75)
            self._check_cancel(cancel_event)

            markers_path = str(output_abs.with_suffix('.srt'))
            cleaned, entries = self._apply_censorship(
                cleaned, sr, words, censor_mode, markers_path,
                config['padding_ms'], log_cb,
            )
            cleaned = self._soft_limit(cleaned)
            if progress_cb:
                progress_cb(0.85)
            self._check_cancel(cancel_event)

            cleaned, muted_sec = self._detect_non_speech(
                cleaned, sr, log_cb, cancel_event, speech_segments=speech_segments
            )
            stats['muted_sec'] = muted_sec
            if progress_cb:
                progress_cb(0.95)

            if len(cleaned) < orig_len:
                cleaned = np.pad(cleaned, (0, orig_len - len(cleaned)))
            elif len(cleaned) > orig_len:
                cleaned = cleaned[:orig_len]

            output_abs.parent.mkdir(parents=True, exist_ok=True)
            temp_path = Path(str(output_abs) + ".tmp")
            sf.write(str(temp_path), cleaned, sr, format='WAV')
            temp_path.replace(output_abs)
            temp_path = None
            if progress_cb:
                progress_cb(1.0)

            elapsed = time.time() - start_time
            stats['profanity_count'] = len(entries)
            stats['unique_words'] = list(dict.fromkeys(e['word'] for e in entries))
            stats['elapsed'] = elapsed
            self._log(f"💾 Готово за {_format_duration(elapsed)}: {output_abs.name}", log_cb)
            return True, "Обработка завершена успешно", stats
        except InterruptedError as e:
            self._log(f"⛔ {e}", log_cb)
            return False, str(e), stats
        except Exception as e:
            self._log(f"❌ ОШИБКА: {e}", log_cb)
            logger.exception("Unhandled error in process()")
            return False, str(e), stats
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
