"""Drift-correcting audio resampler.

The audio interface and the cameras run on different crystals, so "48000 Hz"
audio is really 48000 Hz +/- a few tens of ppm on the camera timeline (tens of
milliseconds per hour).  After a recording the measured ratio is used here to
re-time the audio so that sample ``n`` sits exactly at ``n / rate`` seconds
after video frame 0.  Interpolation is windowed-sinc (fractional delay), which
is transparent for ratios this close to 1.
"""

from __future__ import annotations

import numpy as np
import soundfile as sf

TAPS = 24
PHASES = 2048
BLOCK = 1 << 16


def _filter_bank() -> np.ndarray:
    k = np.arange(TAPS) - (TAPS // 2 - 1)  # tap offsets relative to floor(position)
    frac = np.arange(PHASES + 1) / PHASES
    x = k[None, :] - frac[:, None]
    beta = 9.0
    window = np.i0(beta * np.sqrt(np.clip(1.0 - (x / (TAPS / 2)) ** 2, 0.0, None))) / np.i0(beta)
    h = np.sinc(x) * window
    h /= h.sum(axis=1, keepdims=True)
    return h.astype(np.float32)


def resample_file(src: str, dst: str, start_pos: float, ratio: float, n_out: int, progress=None) -> None:
    """Write ``n_out`` frames to ``dst``; output frame m is read from ``src``
    at fractional position ``start_pos + m * ratio``.  Positions outside the
    source are silence."""
    with sf.SoundFile(src) as fin:
        n_in, ch, rate = fin.frames, fin.channels, fin.samplerate
        with sf.SoundFile(dst, "w", samplerate=rate, channels=ch, subtype="PCM_16", format="WAV") as fout:
            if abs(ratio - 1.0) < 1e-12 and abs(start_pos - round(start_pos)) < 1e-6:
                _copy(fin, fout, int(round(start_pos)), n_out, n_in, ch, progress)
                return
            bank = _filter_bank()
            offs = np.arange(TAPS) - (TAPS // 2 - 1)
            for m0 in range(0, n_out, BLOCK):
                m = np.arange(m0, min(m0 + BLOCK, n_out), dtype=np.float64)
                pos = start_pos + m * ratio
                base = np.floor(pos)
                phase = np.round((pos - base) * PHASES).astype(np.intp)
                base = base.astype(np.int64)
                lo = int(base[0]) + int(offs[0])
                hi = int(base[-1]) + int(offs[-1]) + 1
                chunk = np.zeros((hi - lo, ch), dtype=np.float32)
                a, b = max(lo, 0), min(hi, n_in)
                if b > a:
                    fin.seek(a)
                    chunk[a - lo : b - lo] = fin.read(b - a, dtype="float32", always_2d=True)
                idx = (base - lo)[:, None] + offs[None, :]
                out = np.einsum("nt,ntc->nc", bank[phase], chunk[idx])
                fout.write(np.clip(out, -1.0, 32767.0 / 32768.0))
                if progress:
                    progress(min(1.0, (m0 + BLOCK) / max(1, n_out)))


def _copy(fin, fout, start: int, n_out: int, n_in: int, ch: int, progress) -> None:
    done = 0
    if start < 0:
        pad = min(-start, n_out)
        fout.write(np.zeros((pad, ch), dtype=np.int16))
        done, start = pad, 0
    fin.seek(min(start, n_in))
    while done < n_out:
        data = fin.read(min(BLOCK, n_out - done), dtype="int16", always_2d=True)
        if len(data) == 0:
            fout.write(np.zeros((n_out - done, ch), dtype=np.int16))
            break
        fout.write(data)
        done += len(data)
        if progress:
            progress(done / max(1, n_out))
