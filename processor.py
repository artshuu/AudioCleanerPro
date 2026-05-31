import os
os.environ['VOSK_LOG_LEVEL'] = 'ERROR'

import sys
import json
import time
import threading
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


class AudioProcessor:
    def __init__(self):
        self.dict_warning = None

        dict_path = get_resource_path("words.txt")
        try:
            with open(dict_path, "r", encoding="utf-8") as f:
                lines = [line.strip().lower() for line in f if line.strip()]
            if len(lines) == 0:
                self.profanity_set = {'бля', 'хуй', 'пизд', 'еба', 'сука', 'нах', 'мудак'}
                self.dict_warning = "⚠ words.txt пуст! Цензура работает в ограниченном режиме (7 базовых слов)."
            else:
                self.profanity_set = set(lines)
        except FileNotFoundError:
            self.profanity_set = {'бля', 'хуй', 'пизд', 'еба', 'сука', 'нах', 'мудак'}
            self.dict_warning = "⚠ words.txt не найден! Цензура работает в ограниченном режиме (7 базовых слов)."

        model_path = get_resource_path("vosk-model-small-ru-0.22")
        required_dirs = ['am', 'conf', 'graph']
        missing = [d for d in required_dirs if not (Path(model_path) / d).exists()]
        if missing:
            raise ValueError(f"Модель Vosk неполная. Нет папок: {missing}")

        self.vosk_model = Model(model_path)

        # Silero VAD
        self.vad_model = None
        self.vad_warning = None
        if SILERO_AVAILABLE:
            try:
                self.vad_model = load_silero_vad()
            except Exception as e:
                self.vad_warning = f"⚠ Silero VAD не загружен: {e}. Удаление тишины отключено."
        else:
            self.vad_warning = "⚠ Silero VAD не установлен. Удаление тишины отключено. (pip install silero-vad)"

        self._buf_original = None
        self._buf_processed = None
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
        self._log("🎛 Предобработка (сохранение тембра)...", log_cb)
        data = data - np.mean(data)

        max_val = np.max(np.abs(data))
        if max_val > 0.95:
            self._log("   ✂ Исправление клиппинга...", log_cb)
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

        self._log("✅ Предобработка завершена", log_cb)
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

    def _denoise_chunked(self, data: np.ndarray, sr: int, prop_decrease: float,
                         chunk_sec: int = 30, overlap_sec: float = 1.0,
                         progress_start=0.0, progress_end=0.0,
                         progress_cb=None, log_cb=None, cancel_event=None) -> np.ndarray:
        """
        VAD-guided noise reduction: мягкая очистка на участках с речью,
        агрессивная на тишине. Гарантированно сохраняет голос.
        """
        import noisereduce as nr

        self._log(f"   🔇 VAD-guided noisereduce: адаптивное шумоподавление...", log_cb)

        use_vad = self.vad_model is not None
        speech_mask = np.zeros(len(data), dtype=bool)

        if use_vad:
            target_sr = 16000
            if sr != target_sr:
                resampled = T.Resample(sr, target_sr)(torch.from_numpy(data).float().unsqueeze(0)).squeeze(0)
            else:
                resampled = torch.from_numpy(data).float()

            timestamps = get_speech_timestamps(resampled, self.vad_model, sampling_rate=target_sr,
                                               min_speech_duration_ms=200, speech_pad_ms=80)
            ratio = sr / target_sr
            for ts in timestamps:
                s = max(0, min(int(ts['start'] * ratio), len(speech_mask)))
                e = max(0, min(int(ts['end'] * ratio), len(speech_mask)))
                speech_mask[s:e] = True
            speech_pct = np.sum(speech_mask) / len(speech_mask) * 100
            self._log(f"   🗣 Речь: {speech_pct:.1f}% | Тишина: {100 - speech_pct:.1f}%", log_cb)
        else:
            self._log("   ℹ VAD недоступен, применяется равномерное шумоподавление", log_cb)

        chunk_len = int(chunk_sec * sr)
        overlap_len = int(overlap_sec * sr)
        step = chunk_len - overlap_len

        chunks = []
        for i in range(0, len(data), step):
            end = min(i + chunk_len, len(data))
            chunks.append((i, end, data[i:end].astype(np.float32)))

        total = len(chunks)
        num_workers = min(4, os.cpu_count() or 2)
        self._log(f"   Обработка: {total} чанков ({num_workers} потоков)...", log_cb)

        results = [None] * total
        completed = 0

        def _process_chunk(args):
            idx, start, end, chunk_data = args
            if use_vad:
                chunk_speech = speech_mask[start:end]
                speech_ratio = np.sum(chunk_speech) / len(chunk_speech) if len(chunk_speech) > 0 else 0

                if speech_ratio > 0.7:
                    clean = nr.reduce_noise(y=chunk_data, sr=sr, prop_decrease=min(prop_decrease, 0.15))
                elif speech_ratio < 0.1:
                    clean = nr.reduce_noise(y=chunk_data, sr=sr, prop_decrease=0.9)
                else:
                    adaptive_prop = 0.15 + (0.9 - 0.15) * (1 - speech_ratio)
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

    def _check_blin_context(self, wr: list, current_word: str) -> bool:
        context_words = [ww.get("word", "").strip().lower() for ww in wr]
        idx = next((j for j, ww in enumerate(context_words) if ww == current_word), -1)
        if idx < 0:
            return False
        neighbors = []
        if idx > 0:
            neighbors.append(context_words[idx - 1])
        if idx < len(context_words) - 1:
            neighbors.append(context_words[idx + 1])
        blin_context = {"ну", "вот", "это", "же", "совсем", "просто", "опять", "блин", "да", "нет", "ой", "эх"}
        return any(n in blin_context for n in neighbors)

    def _detect_non_speech(self, data, sr, log_cb=None, cancel_event=None):
        if self.vad_model is None:
            self._log("ℹ VAD отключён, тишина не удаляется", log_cb)
            return data, 0.0
        self._log("🔍 Поиск неречевых звуков (Silero VAD)...", log_cb)
        self._check_cancel(cancel_event)
        target_sr = 16000
        if sr != target_sr:
            resampled = T.Resample(sr, target_sr)(torch.from_numpy(data).float().unsqueeze(0)).squeeze(0)
        else:
            resampled = torch.from_numpy(data).float()
        timestamps = get_speech_timestamps(resampled, self.vad_model, sampling_rate=target_sr,
                                           min_speech_duration_ms=200, speech_pad_ms=80)
        result = data.copy()
        ratio = sr / target_sr
        muted = 0
        last_end = 0
        for ts in timestamps:
            seg_rms = torch.sqrt(torch.mean(resampled[ts['start']:ts['end']] ** 2)).item()
            if seg_rms >= 0.008:
                s = max(0, min(int(ts['start'] * ratio), len(result)))
                e = max(0, min(int(ts['end'] * ratio), len(result)))
                if s > last_end:
                    result[last_end:s] = 0
                    muted += s - last_end
                last_end = e
        if last_end < len(result):
            result[last_end:] = 0
            muted += len(result) - last_end
        muted_sec = muted / sr
        self._log(f"✅ Убрано тишины: {_format_duration(muted_sec)}", log_cb)
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

    def _censor_profanity(self, data, sr, censor_mode, output_markers_path,
                          chunk_size, padding_ms, log_cb, cancel_event):
        ml = "МЯУ" if censor_mode == "meow" else "БИП"
        mi = "🐱" if censor_mode == "meow" else "📟"
        self._log(f"{mi} Цензура ({ml}, чанк={chunk_size}, паддинг={padding_ms}мс)...", log_cb)
        self._check_cancel(cancel_event)
        target_sr = 16000
        ratio = sr / target_sr
        if sr != target_sr:
            resampled = T.Resample(sr, target_sr)(torch.from_numpy(data).float().unsqueeze(0)).squeeze(0).numpy()
        else:
            resampled = data
        rec = KaldiRecognizer(self.vosk_model, target_sr)
        rec.SetWords(True)
        audio_int16 = (resampled * 32768).astype(np.int16)
        audio_bytes = memoryview(audio_int16).cast('b')
        segs, entries = [], []
        pad = int(sr * padding_ms / 1000)
        for i in range(0, len(audio_bytes), chunk_size):
            if i % (chunk_size * 10) == 0:
                self._check_cancel(cancel_event)
            chunk = bytes(audio_bytes[i:i + chunk_size])
            rj = rec.Result() if rec.AcceptWaveform(chunk) else (rec.FinalResult() if i + chunk_size >= len(audio_bytes) else None)
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
                    if not wt or not self._is_profanity(wt):
                        continue
                    duration = w["end"] - w["start"]
                    if wt == "бля":
                        if self._has_l_resonance(data, sr, w["start"], w["end"]):
                            self._log(f"   ⚠ Пропущено (резонанс Л): \"{wt}\" @ {w['start']:.2f}с (dur={duration:.2f}с, вероятно \"блин\")", log_cb)
                            continue
                        if self._check_blin_context(wr, wt):
                            self._log(f"   ⚠ Пропущено (контекст): \"{wt}\" @ {w['start']:.2f}с (dur={duration:.2f}с, вероятно \"блин\")", log_cb)
                            continue
                        if duration > 0.4:
                            self._log(f"   ⚠ Пропущено (фонетика): \"{wt}\" @ {w['start']:.2f}с (dur={duration:.2f}с, вероятно не мат)", log_cb)
                            continue
                    if wt in ("сука", "нах") and duration > 0.5:
                        self._log(f"   ⚠ Пропущено (фонетика): \"{wt}\" @ {w['start']:.2f}с (dur={duration:.2f}с, вероятно не мат)", log_cb)
                        continue
                    s, e = int(w["start"] * sr), int(w["end"] * sr)
                    s, e = max(0, s - pad), min(len(data), e + pad)
                    if e > s:
                        segs.append((s, e))
                        entries.append({'start': s / sr, 'end': e / sr, 'word': wt.lower(), 'precise': True})
                        self._log(f"   {mi} {ml}: \"{wt}\" @ {w['start']:.2f}с", log_cb)
            else:
                txt = res.get("text", "").lower().strip()
                if txt:
                    mw = next((''.join(c for c in w if c.isalpha() or c == 'ё') for w in txt.split() if self._is_profanity(w)), "")
                    if mw:
                        sp = i // 2
                        cs = min(chunk_size // 2, len(resampled) - sp)
                        s, e = int(sp * ratio), int((sp + cs) * ratio)
                        s, e = max(0, min(s, len(data))), max(s, min(e, len(data)))
                        if e > s and not any(x <= s <= y for x, y in segs):
                            segs.append((s, e))
                            entries.append({'start': s / sr, 'end': e / sr, 'word': mw, 'precise': False})
                            self._log(f"   {mi} {ml}(seg~): \"{mw}\" @ ~{s / sr:.1f}с", log_cb)
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
                data[s:e] = gen(e - s, sr)
        self.last_entries = entries
        self.last_sr = sr
        if output_markers_path and entries:
            self._write_srt(output_markers_path, entries, ml, log_cb)
        elif output_markers_path:
            self._log("ℹ Мат не найден — SRT не создан", log_cb)
        uw = list(dict.fromkeys(e['word'] for e in entries))
        self._log(f"✅ Зацензурено: {len(entries)} ({', '.join(uw)})" if entries else "ℹ Мата нет", log_cb)
        return data, entries

    def _write_srt(self, path, entries, mode_lbl, log_cb):
        ec = sorted(entries, key=lambda x: x['start'])
        try:
            with open(path, 'w', encoding='utf-8') as f:
                for i, en in enumerate(ec, 1):
                    pt = "" if en['precise'] else " ≈"
                    f.write(f"{i}\n{self._format_srt_time(en['start'])} --> {self._format_srt_time(en['end'])}\n[{mode_lbl}{pt}] {en['word']}\n\n")
            pc = sum(1 for e in ec if e['precise'])
            self._log(f"📍 Маркеры: {Path(path).name} ({pc} точных, {len(ec) - pc} ~)", log_cb)
        except Exception as ex:
            self._log(f"⚠ SRT Error: {ex}", log_cb)

    def export_srt(self, output_path: str, censor_mode: str = "meow", log_cb=None) -> bool:
        if not self.last_entries:
            self._log("ℹ Нет маркеров для экспорта", log_cb)
            return False
        ml = "МЯУ" if censor_mode == "meow" else "БИП"
        self._write_srt(output_path, self.last_entries, ml, log_cb)
        return True

    def process(self, input_path, output_path, censor_mode="meow",
                noise_reduction=0.5, censor_chunk_size=8000, padding_ms=100,
                progress_cb=None, log_cb=None, cancel_event=None):
        start_time = time.time()
        stats = {'profanity_count': 0, 'unique_words': [], 'elapsed': 0, 'muted_sec': 0.0}
        try:
            if self.dict_warning:
                self._log(self.dict_warning, log_cb)
            if self.vad_warning:
                self._log(self.vad_warning, log_cb)
            self._log(f"📂 Чтение: {Path(input_path).name}", log_cb)
            data, sr = sf.read(input_path)
            if len(data.shape) > 1:
                data = np.mean(data, axis=1)
                self._log("   Stereo → Mono", log_cb)
            orig_len = len(data)
            dur_str = _format_duration(orig_len / sr)
            self._log(f"   Длительность: {dur_str} | SR: {sr} Гц", log_cb)
            if progress_cb:
                progress_cb(0.02)
            data_before = data.copy()
            data = self._preprocess_audio(data, sr, log_cb)
            self._check_cancel(cancel_event)
            if progress_cb:
                progress_cb(0.10)
            self._log("🔇 Шумоподавление...", log_cb)
            stage_start = time.time()
            cleaned = self._denoise_chunked(
                data, sr, noise_reduction, chunk_sec=30, overlap_sec=1.0,
                progress_start=0.10, progress_end=0.45,
                progress_cb=progress_cb, log_cb=log_cb, cancel_event=cancel_event
            )
            self._log("🔧 Восстановление динамики...", log_cb)
            cleaned = self._restore_envelope(data_before, cleaned, sr)
            noise_time = time.time() - stage_start
            eta = max(0, noise_time * 1.8)
            eta_m, eta_s = divmod(int(eta), 60)
            self._log(f"⏱ Осталось ~{eta_m:02d}:{eta_s:02d}", log_cb)
            if progress_cb:
                progress_cb(0.55)
            self._check_cancel(cancel_event)
            cleaned, muted_sec = self._detect_non_speech(cleaned, sr, log_cb, cancel_event)
            stats['muted_sec'] = muted_sec
            if progress_cb:
                progress_cb(0.70)
            markers_path = str(Path(output_path).with_suffix('.srt'))
            cleaned, entries = self._censor_profanity(
                cleaned, sr, censor_mode, markers_path,
                censor_chunk_size, padding_ms, log_cb, cancel_event
            )
            if progress_cb:
                progress_cb(0.95)
            if len(cleaned) < orig_len:
                cleaned = np.pad(cleaned, (0, orig_len - len(cleaned)))
            elif len(cleaned) > orig_len:
                cleaned = cleaned[:orig_len]
            sf.write(output_path, cleaned, sr)
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