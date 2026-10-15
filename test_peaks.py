"""Self-check for the loudness spike detector: two known bursts in quiet noise must be the only peaks found."""
import tempfile
from pathlib import Path
import numpy as np
import clipsniper as cs

rng = np.random.default_rng(1)
audio = rng.normal(0, 200, cs.SR * 600).astype(np.float32)          # 10 min of quiet room noise
for t in (250, 400):                                                 # two 2s bursts, 30x louder
    audio[t * cs.SR:(t + 2) * cs.SR] += rng.normal(0, 6000, 2 * cs.SR)
pcm = Path(tempfile.mkdtemp()) / "audio.pcm"
audio.astype(np.int16).tofile(pcm)

found = cs.peaks(pcm, top=10)
times = sorted(round(t) for t, _ in found)
assert times and all(any(abs(t - want) <= 1 for want in (250, 400)) for t in times), times
assert any(abs(t - 250) <= 1 for t in times) and any(abs(t - 400) <= 1 for t in times), times
assert len(times) == 2, times
print("ok", found)
