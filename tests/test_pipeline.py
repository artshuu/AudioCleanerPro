import json
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import numpy as np

from config_utils import load_settings, normalize_settings, save_settings
from processor import AudioProcessor, find_vosk_model_dir, resolve_processing_config


def make_processor(**overrides):
    processor = AudioProcessor.__new__(AudioProcessor)
    processor._log = lambda *args, **kwargs: None
    processor.vad_model = None
    processor.vosk_model = None
    processor.vosk_model_name = None
    processor.profanity_set = {"бля", "хуй", "пизд", "еба", "сука", "мудак"}
    processor.last_entries = []
    for key, value in overrides.items():
        setattr(processor, key, value)
    return processor


def rms_profile(signal, sr, window_sec=0.1):
    window = int(sr * window_sec)
    usable = len(signal) - len(signal) % window
    frames = signal[:usable].reshape(-1, window)
    return np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))


class ChunkStitchingTests(unittest.TestCase):
    def test_denoise_chunk_stitching_keeps_level(self):
        processor = make_processor()
        sr = 1000
        seconds = 25
        t = np.arange(sr * seconds) / sr
        signal = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        fake_nr = types.SimpleNamespace(
            reduce_noise=lambda y=None, sr=None, prop_decrease=1.0, **kwargs: np.asarray(y)
        )
        with patch.dict(sys.modules, {"noisereduce": fake_nr}):
            output = processor._denoise_chunked(
                signal, sr, 0.5, chunk_sec=10, overlap_sec=1.0, progress_start=0.0, progress_end=1.0
            )

        profile = rms_profile(output, sr)
        self.assertEqual(output.shape, signal.shape)
        self.assertGreater(float(np.min(profile) / np.max(profile)), 0.999)

    def test_denoise_chunk_stitching_covers_any_length(self):
        processor = make_processor()
        fake_nr = types.SimpleNamespace(
            reduce_noise=lambda y=None, sr=None, prop_decrease=1.0, **kwargs: np.asarray(y)
        )
        with patch.dict(sys.modules, {"noisereduce": fake_nr}):
            for length in (1, 999, 1001, 2999, 3001, 5500):
                signal = np.ones(length, dtype=np.float32)
                output = processor._denoise_chunked(
                    signal, 1000, 0.5, chunk_sec=3, overlap_sec=0.5
                )
                self.assertEqual(output.shape, (length,))
                self.assertTrue(np.allclose(output, 1.0), f"length={length}")

    def test_preprocess_highpass_keeps_level_across_chunks(self):
        processor = make_processor()
        sr = 1000
        seconds = 130
        t = np.arange(sr * seconds) / sr
        tone = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

        output = processor._preprocess_audio(tone, sr)
        profile = rms_profile(output, sr, window_sec=0.25)[2:-2]
        self.assertGreater(float(np.min(profile) / np.max(profile)), 0.97)

    def test_restore_envelope_keeps_level_across_blocks(self):
        processor = make_processor()
        sr = 1000
        seconds = 65
        t = np.arange(sr * seconds) / sr
        signal = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

        output = processor._restore_envelope(signal, signal.copy(), sr, block_sec=10, overlap_sec=0.5)
        profile = rms_profile(output, sr, window_sec=0.25)[2:-2]
        self.assertGreater(float(np.min(profile) / np.max(profile)), 0.97)


class ProfanityDetectionTests(unittest.TestCase):
    def test_inflected_forms_are_detected(self):
        processor = make_processor()
        for word in ("блять", "блядь", "блядский", "нахуй", "нахер", "заебал",
                     "охуеть", "пиздец", "сучара", "мудачьё"):
            self.assertTrue(processor._is_profanity(word), word)

    def test_whitelist_words_are_not_detected(self):
        processor = make_processor()
        for word in ("блин", "бляха", "бляшками", "блямба", "сукно", "наука", "хутор",
                     "худой", "мудрый", "пистон", "сучковатый"):
            self.assertFalse(processor._is_profanity(word), word)

    def test_yo_is_normalized_to_e(self):
        processor = make_processor()
        self.assertTrue(processor._is_profanity("хуёвый"))
        self.assertTrue(processor._is_profanity("ёбнуть"))

    def test_vosk_model_is_found(self):
        model_dir = find_vosk_model_dir()
        self.assertIsNotNone(model_dir)
        self.assertTrue(model_dir.name.startswith("vosk-model"))

    def test_empty_vad_segments_leave_audio_untouched(self):
        processor = make_processor(vad_model=object())
        data = np.full(1000, 0.1, dtype=np.float32)
        output, muted = processor._detect_non_speech(data, 1000, speech_segments=[])
        np.testing.assert_array_equal(output, data)
        self.assertEqual(muted, 0.0)

    def test_vad_failure_with_tiny_speech_fraction_does_not_mute_file(self):
        processor = make_processor(vad_model=object())
        data = np.full(10_000, 0.1, dtype=np.float32)
        output, muted = processor._detect_non_speech(data, 1000, speech_segments=[(0, 100)])
        np.testing.assert_array_equal(output, data)
        self.assertEqual(muted, 0.0)


class CensorshipFlowTests(unittest.TestCase):
    def test_common_neighbors_are_censored_and_low_confidence_is_skipped(self):
        processor = make_processor(vosk_model=object())
        sr = 16000
        seconds = 2
        t = np.arange(sr * seconds) / sr
        data = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        transcript = {
            "result": [
                {"word": "это", "start": 0.10, "end": 0.30, "conf": 0.90},
                {"word": "блять", "start": 0.35, "end": 0.55, "conf": 0.80},
                {"word": "бля", "start": 0.60, "end": 0.80, "conf": 0.75},
                {"word": "блин", "start": 0.85, "end": 1.05, "conf": 0.95},
                {"word": "нахуй", "start": 1.10, "end": 1.30, "conf": 0.05},
            ]
        }

        class FakeRecognizer:
            def __init__(self, model, sample_rate):
                self.payload = json.dumps(transcript)

            def SetWords(self, flag):
                pass

            def AcceptWaveform(self, chunk):
                return False

            def FinalResult(self):
                return self.payload

        with patch("processor.KaldiRecognizer", FakeRecognizer):
            words = processor._recognize_words(data, sr, None, None)
        output, entries = processor._apply_censorship(
            data.copy(), sr, words, "beep", None, 100, None
        )

        self.assertEqual([entry["word"] for entry in entries], ["блять", "бля"])
        self.assertGreater(float(np.max(np.abs(output - data))), 0.05)


class RecognitionConcurrencyTests(unittest.TestCase):
    def test_overlapping_block_words_are_deduplicated(self):
        processor = make_processor()
        words = [
            {"word": "блять", "start": 19.50, "end": 19.80, "confidence": 0.4},
            {"word": "блять", "start": 19.55, "end": 19.85, "confidence": 0.9},
            {"word": "блять", "start": 40.00, "end": 40.30, "confidence": 0.9},
        ]

        deduped = processor._dedupe_recognized_words(words)

        self.assertEqual(len(deduped), 2)
        self.assertAlmostEqual(float(deduped[0]["confidence"]), 0.9)

    def test_long_speech_is_split_into_blocks_with_progress(self):
        processor = make_processor(vosk_model=object())
        sr = 16000
        seconds = 50
        t = np.arange(sr * seconds) / sr
        data = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        transcript = {"result": [{"word": "блять", "start": 0.2, "end": 0.5, "conf": 0.8}]}

        class FakeRecognizer:
            def __init__(self, model, sample_rate):
                self.payload = json.dumps(transcript)

            def SetWords(self, flag):
                pass

            def AcceptWaveform(self, chunk):
                return False

            def FinalResult(self):
                return self.payload

        progress = []
        with patch("processor.KaldiRecognizer", FakeRecognizer):
            words = processor._recognize_words(
                data, sr, progress_cb=progress.append, progress_start=0.0, progress_end=1.0
            )
        output, entries = processor._apply_censorship(
            data.copy(), sr, words, "beep", None, 100, None
        )

        self.assertEqual([round(word["start"], 1) for word in words], [0.2, 19.2, 38.2])
        self.assertAlmostEqual(progress[-1], 1.0)
        self.assertEqual(progress, sorted(progress))
        self.assertGreater(len(progress), 1)
        self.assertGreater(float(np.max(np.abs(output - data))), 0.05)

    def test_cancelled_recognition_raises_immediately(self):
        processor = make_processor(vosk_model=object())
        data = np.zeros(16000, dtype=np.float32)
        cancel_event = threading.Event()
        cancel_event.set()

        class FakeRecognizer:
            def __init__(self, model, sample_rate):
                pass

            def SetWords(self, flag):
                pass

            def AcceptWaveform(self, chunk):
                return False

            def FinalResult(self):
                return '{"result": []}'

        with patch("processor.KaldiRecognizer", FakeRecognizer):
            with self.assertRaises(InterruptedError):
                processor._recognize_words(data, 16000, cancel_event=cancel_event)


class SrtAndConfigTests(unittest.TestCase):
    def test_write_srt_formats_timecodes(self):
        processor = make_processor()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "markers.srt")
            processor._write_srt(
                path, [{"start": 1.5, "end": 2.0, "word": "бля", "precise": True}], "БИП", None
            )
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
        self.assertIn("00:00:01,500 --> 00:00:02,000", content)
        self.assertIn("[БИП] бля", content)

    def test_preset_ranges_are_applied(self):
        fast = resolve_processing_config("fast", 0.9, 200)
        quality = resolve_processing_config("quality", 0.1, 50)
        self.assertEqual(fast["noise_reduction"], 0.45)
        self.assertEqual(fast["padding_ms"], 80)
        self.assertEqual(quality["noise_reduction"], 0.55)

    def test_invalid_theme_falls_back_to_system(self):
        self.assertEqual(normalize_settings({"theme": "neon"})["theme"], "system")

    def test_settings_roundtrip_uses_explicit_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            save_settings({"theme": "light", "noise_reduction": 0.7, "processing_preset": "quality"}, path)
            loaded = load_settings(path)
        self.assertEqual(loaded["theme"], "light")
        self.assertEqual(loaded["noise_reduction"], 0.7)
        self.assertEqual(loaded["processing_preset"], "quality")


if __name__ == "__main__":
    unittest.main()
