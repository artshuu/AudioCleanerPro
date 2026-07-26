import unittest

import numpy as np

from config_utils import normalize_settings
from processor import AudioProcessor


class ConfigUtilsTests(unittest.TestCase):
    def test_normalize_settings_keeps_processing_profile(self):
        settings = normalize_settings({
            "processing_preset": "quality",
        })

        self.assertEqual(settings["processing_preset"], "quality")

    def test_censor_profanity_accepts_empty_transcript_context(self):
        processor = AudioProcessor.__new__(AudioProcessor)
        processor._log = lambda *args, **kwargs: None
        processor._check_cancel = lambda *args, **kwargs: None
        processor._is_profanity = lambda word: word in {"бля", "сука"}
        processor._check_context_for_false_positive = lambda *args, **kwargs: (False, "")
        processor._has_l_resonance = lambda *args, **kwargs: False
        processor.last_entries = []
        processor.last_sr = 0
        processor.profanity_set = {"бля", "сука"}
        processor.vosk_model = None

        data = [0.0, 0.0, 0.0]
        result, entries = processor._censor_profanity(
            data,
            16000,
            "meow",
            None,
            8000,
            100,
            None,
            None,
        )

        self.assertEqual(result, data)
        self.assertEqual(entries, [])

    def test_estimate_denoise_strength_is_tighter_for_speechy_chunks(self):
        processor = AudioProcessor.__new__(AudioProcessor)

        speechy_strength = processor._estimate_denoise_strength(0.85, 0.15, 0.5)
        quiet_strength = processor._estimate_denoise_strength(0.05, 0.01, 0.5)
        moderate_strength = processor._estimate_denoise_strength(0.35, 0.08, 0.5)

        self.assertLess(speechy_strength, 0.5)
        self.assertGreater(quiet_strength, 0.5)
        self.assertGreater(moderate_strength, speechy_strength)
        self.assertLess(moderate_strength, quiet_strength)

    def test_soft_limit_caps_peak_and_preserves_shape(self):
        processor = AudioProcessor.__new__(AudioProcessor)
        data = np.array([0.2, 1.5, -1.2, -0.3], dtype=np.float32)

        limited = processor._soft_limit(data)

        self.assertEqual(limited.shape, data.shape)
        self.assertLessEqual(np.max(np.abs(limited)), 0.99 + 1e-6)
        self.assertGreater(limited[0], 0.0)

    def test_should_skip_denoise_for_near_silent_chunks(self):
        processor = AudioProcessor.__new__(AudioProcessor)
        silent_chunk = np.zeros(1024, dtype=np.float32)
        speech_chunk = np.ones(1024, dtype=np.float32) * 0.1

        self.assertTrue(processor._should_skip_denoise(silent_chunk, speech_ratio=0.0, noise_ratio=1.0))
        self.assertFalse(processor._should_skip_denoise(speech_chunk, speech_ratio=0.9, noise_ratio=0.1))


if __name__ == "__main__":
    unittest.main()
