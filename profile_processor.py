import os
import tempfile
import numpy as np
import soundfile as sf
import cProfile
import pstats
from processor import AudioProcessor

proc = AudioProcessor()
sr = 16000
seconds = 15
frames = int(sr * seconds)
t = np.linspace(0, seconds, frames, endpoint=False)
signal = 0.12 * np.sin(2 * np.pi * 140 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 2 * t))
signal[:sr//2] = 0.0
signal[3*sr//2:4*sr//2] = 0.0
with tempfile.TemporaryDirectory() as td:
    inp = os.path.join(td, 'input.wav')
    out = os.path.join(td, 'output.wav')
    sf.write(inp, signal.astype(np.float32), sr)
    prof_path = os.path.join(td, 'profile.stats')
    cProfile.runctx('proc.process(inp, out, censor_mode="meow", noise_reduction=0.5, censor_chunk_size=8000, padding_ms=100, processing_preset="balanced")', globals(), locals(), filename=prof_path)
    p = pstats.Stats(prof_path)
    p.sort_stats('cumtime').print_stats(30)
