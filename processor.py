import os
os.environ['VOSK_LOG_LEVEL'] = 'ERROR'
import sys
import json
import time
import numpy as np
import soundfile as sf
import torch
import torchaudio.transforms as T
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from vosk import Model, KaldiRecognizer
from scipy.ndimage import maximum_filter1d, uniform_filter1d
from scipy.signal import butter, filtfilt
from logging.handlers import RotatingFileHandler
import warnings
import logging

warnings.filterwarnings('ignore')
torch.set_grad_enabled(False)

try:
    from silero_vad import load_silero_vad, get_speech_timestamps
    SILERO_AVAILABLE = True
except ImportError:
    SILERO_AVAILABLE = False


def get_resource_path(relative_path: str) -> str:
    if hasattr(sys, '_MEIPASS'):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).parent.resolve()
    full_path = base / relative_path
    if not full_path.exists():
        raise FileNotFoundError(f"Ресурс не найден: {full_path}")
    return str(full_path.absolute())


def _format_duration(seconds: float) -> str:
    total_sec = int(seconds)
    m, s = divmod(total_sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


logger = logging.getLogger('audio_cleaner')
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = RotatingFileHandler(
        'audio_cleaner.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
    )
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)

CONTEXT_WORDS = {
    "ну", "вот", "это", "же", "совсем", "просто", "опять", "блин",
    "да", "нет", "ой", "эх", "типа", "как", "что", "так", "то",
    "ведь", "прямо", "очень", "уже", "еще", "бы", "ли", "разве"
}

PHONETIC_EXCEPTIONS = {
    "ебать": ["и батя", "батя", "и бати", "бати", "бадя"],
    "мудак": ["мутки", "мутка", "мутко", "мудрый", "мудрость"],
    "хуй": ["хутор", "хутора", "худой", "худо", "куй"],
    "пизд": ["пистон", "писта", "быстро", "висит"],
    "сука": ["сукно", "сукин", "сучок", "сучка", "рука", "наука"],
    "бля": ["блин", "блеск", "близ", "блюд", "боля"],
}


class AudioProcessor:
    def __init__(self):
        self.dict_warning = None
        dict_path = get_resource_path("words.txt")
        try:
            with open(dict_path, "r", encoding="utf-8") as f:
                lines = [line.strip().lower() for line in f if line.strip()]
            if len(lines) == 0:
                self.profanity_set = {'бля', 'хуй', 'пизд', 'еба', 'сука', 'нах', 'мудак'}
                self.dict_warning = "⚠ words.txt пуст! Цензура работает в ограниченном режиме (7 базовых слов). "
            else:
                self.profanity_set = set(lines)
        except FileNotFoundError:
            self.profanity_set = {'бля', 'хуй', 'пизд', 'еба', 'сука', 'нах', 'мудак'}
            self.dict_warning = "⚠ words.txt не найден! Цензура работает в ограниченном режиме (7 базовых слов). "

        model_path = get_resource_path("vosk-model-small-ru-0.22")
        required_dirs = ['am', 'conf', 'graph']
        missing = [d for d in required_dirs if not (Path(model_path) / d).exists()]
        if missing:
            raise ValueError(f"Модель Vosk неполная. Нет папок: {missing}")

        self.vosk_model = Model(model_path)

        self.vad_model = None
        self.vad_warning = None
        if SILERO_AVAILABLE:
            try:
                self.vad_model = load_silero_vad()
            except Exception as e:
                self.vad_warning = f"⚠ Silero VAD не загружен: {e}. Удаление тишины отключено. "
        else:
            self.vad_warning = "⚠ Silero VAD не установлен. Удаление тишины отключено. (pip install silero-vad)"

        self._buf_original = None
        self._buf_processed = None
        self._resampler_cache = {}
        self.last_entries = []
        self.last_sr = 44100

    def _log(self, message: str, log_cb=None):
        logger.info(message)
        if log_cb:
            log_cb(message)
        print(message)

    def _check_cancel(self, cancel_event):
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Обработка прервана пользователем")

    def _format_srt_time(self, seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds - int(seconds)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def _preprocess_audio(self, data: np.ndarray, sr: int, log_cb=None) -> np.ndarray:
        self._log("🎛 Предобработка (сохранение тембра)... ", log_cb)
        data = data - np.mean(data)

        max_val = np.max(np.abs(data))
        if max_val > 0.95:
            self._log("   ✂ Исправление клиппинга... ", log_cb)
            data = np.tanh(data * 1.5) / np.tanh(1.5)

        rms = np.sqrt(np.mean(data ** 2))
        if rms > 0:
            target_rms = 10 ** (-18.0 / 20)
            gain = min(target_rms / rms, 6.0)
            data = data * gain
            self._log(f"   🔊 Нормализация: RMS={rms:.4f} → {target_rms:.4f}", log_cb)

        nyq = sr / 2.0
        cutoff_norm = 80.0 / nyq
        if 0 < cutoff_norm < 1.0:
            b, a = butter(4, cutoff_norm, btype='high')
            hp_chunk_sec = 60
            hp_overlap_sec = 1
            hp_chunk_len = int(hp_chunk_sec * sr)
            hp_overlap_len = int(hp_overlap_sec * sr)
            hp_step = hp_chunk_len - hp_overlap_len
            fade = np.linspace(0, 1, hp_overlap_len, dtype=np.float32) if hp_overlap_len > 0 else np.array([])

            result = np.zeros(len(data), dtype=np.float32)
            weight = np.zeros(len(data), dtype=np.float32)

            for i in range(0, len(data), hp_step):
                end = min(i + hp_chunk_len, len(data))
                chunk_f64 = data[i:end].astype(np.float64)
                filtered = filtfilt(b, a, chunk_f64).astype(np.float32)

                if i > 0 and hp_overlap_len > 0:
                    ao = min(hp_overlap_len, end - i)
                    filtered[:ao] *= fade[:ao]
                    result[i:i + ao] += filtered[:ao] * (1 - fade[:ao])
                    result[i + ao:end] += filtered[ao:]
                else:
                    result[i:end] += filtered
                weight[i:end] += 1.0

            data = result / np.maximum(weight, 1.0)

        peak = np.max(np.abs(data))
        if peak > 0.99:
            data = data * (0.99 / peak)

        self._log("✅ Предобработка завершена ", log_cb)
        return data

    def _restore_envelope(self, original: np.ndarray, processed: np.ndarray,
                          sr: int, window_ms: int = 20) -> np.ndarray:
        n = len(original)
        win_len = max(1, int(sr * window_ms / 1000))
        smooth_size = max(1, win_len // 4)

        if self._buf_original is None or len(self._buf_original) < n:
            self._buf_original = np.empty(n, dtype=np.float32)
            self._buf_processed = np.empty(n, dtype=np.float32)

        np.abs(original, out=self._buf_original[:n])
        np.abs(processed, out=self._buf_processed[:n])

        orig_env = maximum_filter1d(self._buf_original[:n], size=win_len, mode='nearest')
        proc_env = maximum_filter1d(self._buf_processed[:n], size=win_len, mode='nearest')

        orig_env = uniform_filter1d(orig_env, size=smooth_size, mode='nearest')
        proc_env = uniform_filter1d(proc_env, size=smooth_size, mode='nearest')

        eps = np.float32(1e-8)
        ratio = np.where(proc_env > eps, orig_env / proc_env, np.float32(1.0))
        np.clip(ratio, 0.5, 2.0, out=ratio)

        restored = processed * ratio
        peak = np.max(np.abs(restored))
        if peak > 0.99:
            restored *= np.float32(0.99 / peak)
        return restored

    def _soft_limit(self, data: np.ndarray, peak: float = 0.99) -> np.ndarray:
        clipped = np.asarray(data, dtype=np.float32).copy()
        peak_abs = np.max(np.abs(clipped))
        if peak_abs <= peak:
            return clipped
        gain = peak / peak_abs
        clipped *= gain
        return clipped

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

    def _get_resampler(self, src_sr: int, tgt_sr: int):
        key = (src_sr, tgt_sr)
        if key not in self._resampler_cache:
            self._resampler_cache[key] = T.Resample(src_sr, tgt_sr)
        return self._resampler_cache[key]

    def _get_speech_segments(self, data: np.ndarray, sr: int, log_cb=None) -> list[tuple[int, int]]:
        if self.vad_model is None:
            return []

        target_sr = 16000
        if sr != target_sr:
            resampler = self._get_resampler(sr, target_sr)
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

        self._log(f"   🔇 VAD-guided noisereduce: адаптивное шумоподавление... ", log_cb)

        use_vad = self.vad_model is not None
        speech_mask = np.zeros(len(data), dtype=bool)

        if use_vad:
            if speech_segments is None:
                speech_segments = self._get_speech_segments(data, sr, log_cb)
            for s, e in speech_segments:
                speech_mask[s:e] = True
            speech_pct = np.sum(speech_mask) / len(speech_mask) * 100 if len(speech_mask) > 0 else 0.0
            self._log(f"   🗣 Речь: {speech_pct:.1f}% | Тишина: {100 - speech_pct:.1f}%", log_cb)
        else:
            self._log("   ℹ VAD недоступен, применяется равномерное шумоподавление ", log_cb)

        chunk_len = int(chunk_sec * sr)
        overlap_len = int(overlap_sec * sr)
        step = chunk_len - overlap_len

        chunks = []
        for i in range(0, len(data), step):
            end = min(i + chunk_len, len(data))
            chunks.append((i, end, data[i:end].astype(np.float32)))

        total = len(chunks)
        num_workers = self._get_worker_count(len(data), sr)
        self._log(f"   Обработка: {total} чанков ({num_workers} потоков)... ", log_cb)

        results = [None] * total
        completed = 0

        def _process_chunk(args):
            idx, start, end, chunk_data = args
            if use_vad:
                chunk_speech = speech_mask[start:end]
                speech_ratio = np.sum(chunk_speech) / len(chunk_speech) if len(chunk_speech) > 0 else 0.0
                chunk_rms = float(np.sqrt(np.mean(np.square(chunk_data)))) if len(chunk_data) > 0 else 0.0
                noise_ratio = float(np.clip(1.0 - min(1.0, chunk_rms / 0.03), 0.0, 1.0))
                if self._should_skip_denoise(chunk_data, speech_ratio, noise_ratio):
                    clean = chunk_data.astype(np.float32, copy=True)
                else:
                    adaptive_prop = self._estimate_denoise_strength(speech_ratio, noise_ratio, prop_decrease)
                    clean = nr.reduce_noise(y=chunk_data, sr=sr, prop_decrease=adaptive_prop)
            else:
                clean = nr.reduce_noise(y=chunk_data, sr=sr, prop_decrease=prop_decrease)
            return idx, clean.astype(np.float32)

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = []
            for idx, (start, end, chunk) in enumerate(chunks):
                self._check_cancel(cancel_event)
                futures.append(executor.submit(_process_chunk, (idx, start, end, chunk)))

            for future in as_completed(futures):
                idx, result = future.result()
                results[idx] = result
                completed += 1
                if progress_cb:
                    pct = progress_start + (progress_end - progress_start) * (completed / total)
                    progress_cb(pct)

        output = np.zeros(len(data), dtype=np.float32)
        weight = np.zeros(len(data), dtype=np.float32)
        fade = np.linspace(0, 1, overlap_len, dtype=np.float32) if overlap_len > 0 else np.array([])

        for idx, (start, end, _) in enumerate(chunks):
            denoised = results[idx]
            if idx > 0 and overlap_len > 0:
                ao = min(overlap_len, end - start)
                denoised[:ao] *= fade[:ao]
                output[start:start + ao] += denoised[:ao] * (1 - fade[:ao])
                output[start + ao:end] += denoised[ao:]
            else:
                output[start:end] += denoised
            weight[start:end] += 1.0

        return output / np.maximum(weight, 1.0)

    def _is_profanity(self, word_text: str) -> bool:
        clean = ''.join(c for c in word_text.lower() if c.isalpha() or c == 'ё')
        return clean in self.profanity_set

    def _has_l_resonance(self, data: np.ndarray, sr: int, start_sec: float, end_sec: float) -> bool:
        s = max(0, int(start_sec * sr))
        e = min(len(data), int(end_sec * sr))
        if e - s < sr * 0.05:
            return False
        segment = data[s:e]
        window = np.hanning(len(segment))
        spectrum = np.abs(np.fft.rfft(segment * window))
        freqs = np.fft.rfftfreq(len(segment), 1.0 / sr)
        l_mask = (freqs >= 200) & (freqs <= 800)
        l_energy = np.mean(spectrum[l_mask] ** 2) if np.any(l_mask) else 0
        rest_mask = (freqs > 800) & (freqs <= 4000)
        rest_energy = np.mean(spectrum[rest_mask] ** 2) if np.any(rest_mask) else 1e-10
        ratio = l_energy / (rest_energy + 1e-10)
        return ratio > 0.3

    def _check_context_for_false_positive(self, all_words: list, current_word: str, word_index: int) -> tuple:
        neighbors_before = []
        neighbors_after = []

        for i in range(max(0, word_index - 2), word_index):
            neighbors_before.append(all_words[i].get('word', '').strip().lower())
        for i in range(word_index + 1, min(len(all_words), word_index + 3)):
            neighbors_after.append(all_words[i].get('word', '').strip().lower())

        all_neighbors = neighbors_before + neighbors_after

        matched_ctx = [n for n in all_neighbors if n in CONTEXT_WORDS]
        if matched_ctx:
            return True, f"контекст ({', '.join(matched_ctx[:2])})"

        if word_index < len(all_words):
            w = all_words[word_index]
            duration = w.get('end', 0) - w.get('start', 0)
            if duration > 0.5:
                return True, f"длительность ({duration:.2f}с)"

        clean_word = ''.join(c for c in current_word.lower() if c.isalpha() or c == 'ё')
        if clean_word in ["нах", "нахер", "нахрен"]:
            if word_index < len(all_words) - 1:
                next_word = all_words[word_index + 1].get('word', '').strip().lower()
                if next_word in ["рен", "ер", "уй"]:
                    return True, "составное слово"

        return False, ""

    def _detect_non_speech(self, data, sr, log_cb=None, cancel_event=None, speech_segments=None):
        if self.vad_model is None:
            self._log("ℹ VAD отключён, тишина не удаляется ", log_cb)
            return data, 0.0

        self._log("🔍 Поиск неречевых звуков (Silero VAD)... ", log_cb)
        self._check_cancel(cancel_event)

        if speech_segments is None:
            speech_segments = self._get_speech_segments(data, sr, log_cb)

        result = data.copy()
        fade_ms = 50
        fade_samples = int(sr * fade_ms / 1000)

        muted = 0
        last_end = 0

        for s, e in speech_segments:
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
        self._log(f"✅ Заменено на тишину: {_format_duration(muted_sec)} (длительность сохранена)", log_cb)
        return result, muted_sec

    def _generate_meow(self, duration, sr):
        t = np.linspace(0, duration / sr, duration, endpoint=False)
        freq = 800.0 - 400.0 * (t / (duration / sr))
        phase = 2 * np.pi * np.cumsum(freq) / sr
        env = np.sin(np.pi * t / (duration / sr)) ** 2
        return env * (0.7 * np.sin(phase) + 0.3 * np.sin(2 * phase)) * 0.9

    def _generate_beep(self, duration, sr):
        t = np.linspace(0, duration / sr, duration, endpoint=False)
        return 0.8 * np.sin(2 * np.pi * 1000 * t)

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


    def _censor_profanity(self, data, sr, censor_mode, output_markers_path,
                          chunk_size, padding_ms, log_cb, cancel_event,
                          speech_segments=None, transcript=None):
        # === РЕЖИМ OFF: пропускаем цензуру полностью ===
        if censor_mode == "off":
            self._log("🚫 Цензура отключена — пропуск этапа распознавания мата ", log_cb)
            self.last_entries = []
            self.last_sr = sr
            if output_markers_path:
                self._log("ℹ SRT не создан (цензура отключена) ", log_cb)
            return data, []

        ml = "МЯУ" if censor_mode == "meow" else "БИП"
        mi = "🐱" if censor_mode == "meow" else "📟"
        self._log(f"{mi} Цензура ({ml}, чанк={chunk_size}, паддинг={padding_ms}мс)... ", log_cb)
        self._check_cancel(cancel_event)

        if self.vosk_model is not None:
            if not speech_segments:
                self._log("ℹ Нет сегментов речи — распознавание выполняется по всему файлу", log_cb)
                speech_segments = [(0, len(data))]
            target_sr = 16000
            all_words = []
            for s, e in speech_segments:
                segment = data[s:e]
                if segment.size == 0:
                    continue
                if sr != target_sr:
                    resampler = self._get_resampler(sr, target_sr)
                    segment = resampler(torch.from_numpy(segment).float().unsqueeze(0)).squeeze(0).numpy()
                audio_int16 = (segment * 32768).astype(np.int16)
                audio_bytes = memoryview(audio_int16).cast('b')

                rec = KaldiRecognizer(self.vosk_model, target_sr)
                rec.SetWords(True)
                for i in range(0, len(audio_bytes), chunk_size):
                    if i % (chunk_size * 10) == 0:
                        self._check_cancel(cancel_event)
                    chunk = bytes(audio_bytes[i:i + chunk_size])
                    if rec.AcceptWaveform(chunk):
                        rj = rec.Result()
                    elif i + chunk_size >= len(audio_bytes):
                        rj = rec.FinalResult()
                    else:
                        rj = None
                    if not rj:
                        continue
                    try:
                        res = json.loads(rj)
                    except Exception:
                        continue
                    wr = res.get("result", [])
                    if wr:
                        for w in wr:
                            wt = w.get("word", "").strip()
                            if wt:
                                all_words.append({
                                    'word': wt,
                                    'start': s / sr + w.get('start', 0),
                                    'end': s / sr + w.get('end', 0),
                                    'confidence': w.get('conf', 0),
                                    'index': len(all_words)
                                })
        else:
            all_words = []

        segs = []
        entries = []
        pad = int(sr * padding_ms / 1000)

        for w in all_words:
            wt = w['word'].strip()
            clean_wt = ''.join(c for c in wt.lower() if c.isalpha() or c == 'ё')

            if not self._is_profanity(clean_wt):
                continue

            duration = w.get('end', 0) - w.get('start', 0)

            if clean_wt in PHONETIC_EXCEPTIONS:
                if clean_wt == "ебать" and duration > 0.45:
                    self._log(f"   ⚠ Пропущено (фонетика/длит.): \"{wt}\" @ {w['start']:.2f}с ({duration:.2f}с)", log_cb)
                    continue
                if clean_wt == "мудак" and duration > 0.45:
                    self._log(f"   ⚠ Пропущено (фонетика/длит.): \"{wt}\" @ {w['start']:.2f}с ({duration:.2f}с)", log_cb)
                    continue

                is_false, reason = self._check_context_for_false_positive(all_words, wt, w['index'])
                if is_false:
                    self._log(f"   ⚠ Пропущено ({reason}): \"{wt}\" @ {w['start']:.2f}с", log_cb)
                    continue

                s_idx = max(0, int(w['start'] * sr))
                e_idx = min(len(data), int(w['end'] * sr))
                segment = data[s_idx:e_idx]
                if len(segment) > 0:
                    first_10pct = segment[:max(1, len(segment) // 10)]
                    rms_start = np.sqrt(np.mean(first_10pct ** 2))
                    if clean_wt == "ебать" and rms_start > 0.05:
                        self._log(f"   ⚠ Пропущено (фонетика/энергия): \"{wt}\" @ {w['start']:.2f}с", log_cb)
                        continue

            is_false, reason = self._check_context_for_false_positive(all_words, wt, w['index'])
            if is_false:
                self._log(f"   ⚠ Пропущено ({reason}): \"{wt}\" @ {w['start']:.2f}с", log_cb)
                continue

            if clean_wt == "бля":
                if self._has_l_resonance(data, sr, w['start'], w['end']):
                    self._log(f"   ⚠ Пропущено (резонанс Л): \"{wt}\" @ {w['start']:.2f}с", log_cb)
                    continue
                if duration > 0.4:
                    self._log(f"   ⚠ Пропущено (длительность): \"{wt}\" @ {w['start']:.2f}с ({duration:.2f}с)", log_cb)
                    continue

            if clean_wt in ("сука", "нах") and duration > 0.5:
                self._log(f"   ⚠ Пропущено (длительность): \"{wt}\" @ {w['start']:.2f}с ({duration:.2f}с)", log_cb)
                continue

            s = int(w['start'] * sr)
            e = int(w['end'] * sr)
            s = max(0, s - pad)
            e = min(len(data), e + pad)

            if e > s:
                segs.append((s, e))
                entries.append({
                    'start': w['start'],
                    'end': w['end'],
                    'word': wt.lower(),
                    'precise': True
                })
                self._log(f"   {mi} {ml}: \"{wt}\" @ {w['start']:.2f}с", log_cb)

        merged = []
        if segs:
            segs.sort()
            cs, ce = segs[0]
            for s, e in segs[1:]:
                if s <= ce:
                    ce = max(ce, e)
                else:
                    merged.append((cs, ce))
                    cs, ce = s, e
            merged.append((cs, ce))

        gen = self._generate_meow if censor_mode == "meow" else self._generate_beep
        for s, e in merged:
            if e > s:
                censor_audio = gen(e - s, sr)
                data = self._apply_censor_with_fade(data, s, e, censor_audio, sr, fade_ms=15)

        self.last_entries = entries
        self.last_sr = sr

        if output_markers_path and entries:
            self._write_srt(output_markers_path, entries, ml, log_cb)
        elif output_markers_path:
            self._log("ℹ Мат не найден — SRT не создан ", log_cb)

        uw = list(dict.fromkeys(e['word'] for e in entries))
        self._log(f"✅ Зацензурено: {len(entries)} ({', '.join(uw)})" if entries else "ℹ Мата нет ", log_cb)
        return data, entries

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

    def _resolve_processing_config(self, preset: str, noise_reduction: float, censor_chunk_size: int, padding_ms: int):
        preset = preset or "balanced"
        if preset == "fast":
            return {
                "noise_reduction": max(0.2, min(0.45, float(noise_reduction))),
                "censor_chunk_size": max(4000, min(6000, int(censor_chunk_size))),
                "padding_ms": max(40, min(80, int(padding_ms))),
                "chunk_sec": 20,
                "overlap_sec": 0.5,
            }
        if preset == "quality":
            return {
                "noise_reduction": max(0.55, min(0.9, float(noise_reduction))),
                "censor_chunk_size": max(8000, int(censor_chunk_size)),
                "padding_ms": max(120, int(padding_ms)),
                "chunk_sec": 40,
                "overlap_sec": 1.5,
            }
        return {
            "noise_reduction": max(0.35, min(0.7, float(noise_reduction))),
            "censor_chunk_size": max(6000, min(8000, int(censor_chunk_size))),
            "padding_ms": max(80, min(140, int(padding_ms))),
            "chunk_sec": 30,
            "overlap_sec": 1.0,
        }

    def process(self, input_path, output_path, censor_mode="meow",
                noise_reduction=0.5, censor_chunk_size=8000, padding_ms=100,
                progress_cb=None, log_cb=None, cancel_event=None,
                processing_preset="balanced"):
        start_time = time.time()
        stats = {'profanity_count': 0, 'unique_words': [], 'elapsed': 0, 'muted_sec': 0.0}
        try:
            if self.dict_warning and censor_mode != "off":
                self._log(self.dict_warning, log_cb)
            if self.vad_warning:
                self._log(self.vad_warning, log_cb)
            self._log(f"📂 Чтение: {Path(input_path).name}", log_cb)
            config = self._resolve_processing_config(processing_preset, noise_reduction, censor_chunk_size, padding_ms)
            self._log(f"⚙ Пресет: {processing_preset} | шумоподавление={config['noise_reduction']:.2f} | чанки={config['censor_chunk_size']}", log_cb)
            data, sr = sf.read(input_path)

            if len(data.shape) > 1:
                data = np.mean(data, axis=1)
                self._log("   Stereo → Mono ", log_cb)
            orig_len = len(data)
            dur_str = _format_duration(orig_len / sr)
            self._log(f"   Длительность: {dur_str} | SR: {sr} Гц ", log_cb)
            if progress_cb:
                progress_cb(0.02)
            data_before = data.copy()
            data = self._preprocess_audio(data, sr, log_cb)
            self._check_cancel(cancel_event)
            if progress_cb:
                progress_cb(0.10)
            self._log("🔇 Шумоподавление... ", log_cb)
            stage_start = time.time()
            speech_segments = self._get_speech_segments(data, sr, log_cb)
            cleaned = self._denoise_chunked(
                data, sr, config['noise_reduction'], chunk_sec=config['chunk_sec'], overlap_sec=config['overlap_sec'],
                progress_start=0.10, progress_end=0.45,
                progress_cb=progress_cb, log_cb=log_cb, cancel_event=cancel_event,
                speech_segments=speech_segments
            )
            self._log("🔧 Восстановление динамики... ", log_cb)
            cleaned = self._restore_envelope(data_before, cleaned, sr)
            cleaned = self._soft_limit(cleaned)
            noise_time = time.time() - stage_start
            eta = max(0, noise_time * 1.8)
            eta_m, eta_s = divmod(int(eta), 60)
            self._log(f"⏱ Осталось ~{eta_m:02d}:{eta_s:02d}", log_cb)
            if progress_cb:
                progress_cb(0.55)
            self._check_cancel(cancel_event)
            cleaned, muted_sec = self._detect_non_speech(cleaned, sr, log_cb, cancel_event, speech_segments=speech_segments)
            stats['muted_sec'] = muted_sec
            if progress_cb:
                progress_cb(0.70)
            markers_path = str(Path(output_path).with_suffix('.srt'))
            cleaned, entries = self._censor_profanity(
                cleaned, sr, censor_mode, markers_path,
                config['censor_chunk_size'], config['padding_ms'], log_cb, cancel_event,
                speech_segments=speech_segments
            )
            if progress_cb:
                progress_cb(0.95)
            if len(cleaned) < orig_len:
                cleaned = np.pad(cleaned, (0, orig_len - len(cleaned)))
            elif len(cleaned) > orig_len:
                cleaned = cleaned[:orig_len]
            temp_output = Path(output_path)
            temp_output.parent.mkdir(parents=True, exist_ok=True)
            temp_path = Path(str(temp_output) + ".tmp")
            sf.write(str(temp_path), cleaned, sr, format='WAV')
            if temp_path.exists():
                temp_path.replace(output_path)
            if progress_cb:
                progress_cb(1.0)
            elapsed = time.time() - start_time
            stats['profanity_count'] = len(entries)
            stats['unique_words'] = list(dict.fromkeys(e['word'] for e in entries))
            stats['elapsed'] = elapsed
            el_str = _format_duration(elapsed)
            self._log(f"💾 Готово за {el_str}: {Path(output_path).name}", log_cb)
            return True, "Обработка завершена успешно", stats
        except InterruptedError as e:
            self._log(f"⛔ {e}", log_cb)
            return False, str(e), stats
        except Exception as e:
            self._log(f"❌ ОШИБКА: {e}", log_cb)
            logger.exception("Unhandled error in process()")
            return False, str(e), stats