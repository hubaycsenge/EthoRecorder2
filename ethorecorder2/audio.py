"""Audio capture (Zoom recorder used as a USB audio interface).

The sound card is read in a small separate process.  Capture has to be
serviced every few tens of milliseconds or samples are lost, and inside the
main program that cannot be guaranteed while large images are being copied
and drawn.  The helper process does nothing but stamp each block with the
time it arrived (``time.perf_counter_ns`` is system-wide, so the stamps are on
the same clock as everything else) and pass it on.

The stream runs for as long as the app is connected, so that the level meter
works and, more importantly, so that the relationship between the audio
sample counter and the host clock is already known when a recording starts.
"""

from __future__ import annotations

import multiprocessing as mp
import queue
import re
import sys
import threading
import time

import numpy as np
import sounddevice as sd
import soundfile as sf

from .clock import ClockMap, now_ns

_ZOOM = re.compile(r"zoom|\bH[1-8]n?\b", re.IGNORECASE)
STANDARD_RATES = (44100, 48000, 88200, 96000)
BLOCK = 1024


def list_inputs() -> list[dict]:
    """Input devices of the preferred host API (WASAPI on Windows, ALSA on Linux)."""
    try:
        apis = sd.query_hostapis()
        devices = sd.query_devices()
    except Exception:
        return []
    want = "WASAPI" if sys.platform == "win32" else "ALSA"
    api_ids = [i for i, a in enumerate(apis) if want in a["name"]] or list(range(len(apis)))
    out = []
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0 and d["hostapi"] in api_ids:
            out.append(
                {
                    "index": i,
                    "name": d["name"],
                    "channels": d["max_input_channels"],
                    "rate": int(d["default_samplerate"]),
                    "zoom": bool(_ZOOM.search(d["name"])),
                }
            )
    return out


def pick_input(name: str) -> dict | None:
    """The saved device if present, else a Zoom recorder, else nothing."""
    devs = list_inputs()
    for d in devs:
        if name and d["name"] == name:
            return d
    for d in devs:
        if d["zoom"]:
            return d
    return None


def _capture_process(device_index: int, rates: list[int], channels: int, out) -> None:
    """Body of the helper process: open the device at the first rate it
    accepts and forward every block as (end sample index, arrival ns,
    overflowed, raw bytes)."""
    n = 0

    def callback(indata, frames, time_info, status):
        nonlocal n
        arrival = time.perf_counter_ns()
        n += frames
        out.put((n, arrival, bool(status.input_overflow), bytes(indata)))

    last = "no sample rate to try"
    for rate in rates:
        extras = [None]
        if sys.platform == "win32":
            extras.append(sd.WasapiSettings(auto_convert=True))
        for extra in extras:
            try:
                stream = sd.RawInputStream(
                    device=device_index, samplerate=rate, channels=channels, dtype="int16",
                    blocksize=BLOCK, latency="high", callback=callback, extra_settings=extra,
                )  # fmt: skip
            except Exception as e:  # PortAudioError, ValueError
                last = str(e)
                continue
            with stream:
                out.put(("open", rate))
                threading.Event().wait()  # capture until the process is terminated
    out.put(("error", last))


class AudioInput:
    def __init__(self, device: dict, rate: int, channels: int, log):
        self.device = device
        self.log = log
        self.channels = max(1, min(channels, device["channels"]))
        self.rate = rate
        self.overflows = 0  # driver buffer overruns (lost samples) since opening
        self.error: str | None = None
        self._peak = np.zeros(self.channels)
        self._clipped = False
        self._rec = None
        self._rec_lock = threading.Lock()
        self._run = True
        self._last_block_ns = now_ns()
        self._proc = None
        self._q = None
        self._thread = None
        # sample index <-> host ns
        self.clock = ClockMap(nominal_slope=1e9 / rate, window_s=60.0, min_span_s=15.0)
        try:
            self._spawn([rate] + [r for r in (device["rate"],) if r != rate])
            self._thread = threading.Thread(target=self._consume, daemon=True, name="audio")
            self._thread.start()
            self._verify_rate()
        except Exception:
            self.close()
            raise

    def _spawn(self, rates: list[int]) -> None:
        """Start the helper process and wait until the device is open."""
        self._q = None  # the reader thread idles until the new stream is up
        self._kill()
        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        self._proc = ctx.Process(
            target=_capture_process, args=(self.device["index"], rates, self.channels, q),
            daemon=True, name="ethorecorder2-audio",
        )  # fmt: skip
        self._proc.start()
        try:
            kind, value = q.get(timeout=20.0)
        except queue.Empty:
            raise RuntimeError(f"audio device '{self.device['name']}' did not open") from None
        if kind == "error":
            raise RuntimeError(f"cannot open audio device '{self.device['name']}': {value}")
        self.rate = int(value)
        self.clock = ClockMap(nominal_slope=1e9 / self.rate, window_s=60.0, min_span_s=15.0)
        self._last_block_ns = now_ns()
        self._q = q

    def _kill(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            self._proc.join(timeout=3.0)
            self._proc = None

    def _verify_rate(self) -> None:
        """Some interfaces (Zoom H4 on Linux) run at the rate chosen on the
        device itself, whatever the computer asks for.  Count the samples that
        really arrive and reopen at the true rate if it differs."""
        time.sleep(2.5)
        pairs = np.array(self.clock.samples(), dtype=np.float64)
        if len(pairs) < 10:
            raise RuntimeError(f"audio device '{self.device['name']}' delivers no samples")
        measured = np.polyfit((pairs[:, 0] - pairs[0, 0]) / 1e9, pairs[:, 1], 1)[0]
        true_rate = min(STANDARD_RATES, key=lambda r: abs(r - measured))
        if true_rate == self.rate:
            return
        self.log(
            f"Audio: the device runs at {true_rate} Hz (its own setting), not the requested {self.rate} Hz "
            f"- recording at {true_rate} Hz"
        )
        self._spawn([true_rate])
        time.sleep(1.0)

    def _consume(self) -> None:
        while self._run:
            q = self._q
            if q is None:
                time.sleep(0.05)
                continue
            try:
                item = q.get(timeout=0.2)
            except queue.Empty:
                if now_ns() - self._last_block_ns > 3e9 and not self.error:
                    self.error = "the audio device stopped delivering sound"
                    self.log(f"ERROR: {self.error} (unplugged or switched off?)")
                continue
            except (OSError, ValueError, EOFError):
                continue
            if q is not self._q or len(item) != 4:
                continue  # left over from a helper process that was replaced
            end, arrival, overflowed, raw = item
            data = np.frombuffer(raw, dtype=np.int16).reshape(-1, self.channels)
            n = end - len(data)
            self._last_block_ns = now_ns()
            self.overflows += overflowed
            self.clock.add(arrival, end)
            peak = np.abs(data.astype(np.int32)).max(axis=0) / 32768.0
            self._peak = np.maximum(self._peak, peak)
            if peak.max() >= 0.999:
                self._clipped = True
            with self._rec_lock:
                rec = self._rec
                if rec is None or rec["done"].is_set():
                    continue
                rec["blocks"].append((end, arrival))
                stop = rec["stop_idx"]
                if end > rec["start_idx"]:
                    a = max(0, rec["start_idx"] - n)
                    b = len(data) if stop is None else max(a, min(len(data), stop - n))
                    try:
                        rec["file"].write(data[a:b])
                    except Exception as e:
                        self.error = f"audio write failed: {e}"
                        stop = end
                if stop is not None and end >= stop:
                    rec["file"].close()
                    rec["done"].set()

    def levels(self) -> tuple[np.ndarray, bool]:
        """Peak level per channel (0..1) and clip flag since the last call."""
        peak, clipped = self._peak, self._clipped
        self._peak = np.zeros(self.channels)
        self._clipped = False
        return peak, clipped

    def start_recording(self, path: str, t0_host_ns: int) -> int:
        """Start writing ``path`` about 0.3 s before host time ``t0_host_ns``
        (which must be in the future).  Returns the sample index of the first
        sample in the file.  The margin absorbs the block-arrival jitter of
        this quick estimate; the exact alignment is computed afterwards."""
        start_idx = int(round(self.clock.to_dev(t0_host_ns))) - int(0.3 * self.rate)
        f = sf.SoundFile(path, "w", samplerate=self.rate, channels=self.channels, subtype="PCM_16", format="WAV")
        with self._rec_lock:
            self._rec = {"file": f, "start_idx": start_idx, "stop_idx": None, "blocks": [], "done": threading.Event()}
        return start_idx

    def stop_recording(self, t_end_host_ns: int) -> list[tuple[int, int]]:
        """Stop 0.5 s after host time ``t_end_host_ns``.  Returns the
        (end sample index, host arrival ns) pair of every block captured
        meanwhile."""
        with self._rec_lock:
            rec = self._rec
            if rec is None:
                return []
            rec["stop_idx"] = int(round(self.clock.to_dev(t_end_host_ns))) + self.rate // 2
        if not rec["done"].wait(timeout=5.0):
            with self._rec_lock:
                rec["file"].close()
                rec["done"].set()
            self.log("WARNING: audio stream stalled at the end of the recording")
        with self._rec_lock:
            self._rec = None
        return rec["blocks"]

    def close(self) -> None:
        self._run = False
        self._kill()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        with self._rec_lock:
            if self._rec and not self._rec["done"].is_set():
                self._rec["file"].close()
