"""Video writing: raw camera frames are piped into ffmpeg (H.264).

Each writer owns one ffmpeg process and one thread.  Frames arrive tagged
with their *slot* (index on the shared trigger grid, slot 0 = first frame of
the recording).  The writer keeps the file strictly one-frame-per-slot: a
missing slot is filled by repeating the previous frame, so frame N of every
camera file was exposed at the same instant.
"""

from __future__ import annotations

import csv
import queue
import shutil
import subprocess
import sys
import threading

import numpy as np

# Basler pixel format -> ffmpeg rawvideo pixel format
PIX_FMT = {
    "Mono8": "gray",
    "BayerRG8": "bayer_rggb8",
    "BayerBG8": "bayer_bggr8",
    "BayerGR8": "bayer_grbg8",
    "BayerGB8": "bayer_gbrg8",
}

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def ffmpeg_exe() -> str:
    """The ffmpeg shipped with imageio-ffmpeg, else one on PATH."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
        if not exe:
            raise RuntimeError("ffmpeg not found: pip install imageio-ffmpeg")
        return exe


def run_ffmpeg(args: list[str]) -> None:
    p = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    if p.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + p.stderr.decode(errors="replace").strip()[-500:])


class VideoWriter(threading.Thread):
    def __init__(self, path, csv_path, width, height, pixel_format, period_us, crf, preset, max_queue, log):
        super().__init__(daemon=True, name="writer")
        self.path = str(path)
        self.width, self.height = width, height
        self.log = log
        self.error: str | None = None
        self.n_written = 0  # frames in the file
        self.n_filled = 0  # slots filled by repeating a frame
        self.n_late = 0  # frames that arrived for an already written slot
        self.n_queue_drops = 0  # frames dropped because the encoder fell behind
        self.last_slot_seen = -1
        self.max_abs_resid_ns = 0
        self._sum_resid = 0.0
        self._n_resid = 0
        self._q: queue.Queue = queue.Queue(maxsize=max_queue)
        self._total: int | None = None
        self._errlog = open(self.path + ".ffmpeg.log", "wb")
        gop = max(1, round(1e6 / period_us))  # one key frame per second
        cmd = [
            ffmpeg_exe(), "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "rawvideo", "-pix_fmt", PIX_FMT[pixel_format],
            "-s", f"{width}x{height}", "-framerate", f"1000000/{period_us}",
            "-i", "pipe:0", "-an",
            "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-g", str(gop),
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            # fragmented MP4: stays readable if the PC crashes mid-recording
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", self.path,
        ]  # fmt: skip
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._errlog, creationflags=_NO_WINDOW
        )
        self._csv_file = open(csv_path, "w", newline="", encoding="utf-8")
        self._csv = csv.writer(self._csv_file)
        self._csv.writerow(["frame", "status", "camera_timestamp_ns", "host_time_ns", "block_id", "grid_error_us"])
        self.start()

    # -- called from the camera grab thread
    def put(self, slot: int, frame: np.ndarray, ts_ns: int, host_ns: int, block_id: int, resid_ns: int) -> None:
        self.last_slot_seen = max(self.last_slot_seen, slot)
        try:
            self._q.put_nowait((slot, frame, ts_ns, host_ns, block_id, resid_ns))
        except queue.Full:
            self.n_queue_drops += 1

    def finish(self, total_slots: int) -> None:
        """No more frames will come; pad the file to ``total_slots`` frames."""
        self._total = total_slots
        self._q.put(None)

    @property
    def mean_resid_ns(self) -> float:
        return self._sum_resid / self._n_resid if self._n_resid else 0.0

    def _write(self, frame: np.ndarray) -> None:
        if self.error:
            return
        try:
            self._proc.stdin.write(frame.data)
            self.n_written += 1
        except (BrokenPipeError, OSError, ValueError) as e:
            self.error = f"ffmpeg stopped accepting frames ({e}); see {self.path}.ffmpeg.log"
            self.log(f"ERROR: {self.error}")

    def run(self) -> None:
        next_slot = 0
        last = None
        blank = np.zeros((self.height, self.width), dtype=np.uint8)

        def fill_to(slot: int) -> None:
            nonlocal next_slot
            while next_slot < slot:
                self._write(last if last is not None else blank)
                self._csv.writerow([next_slot, "filled", "", "", "", ""])
                self.n_filled += 1
                next_slot += 1

        while True:
            item = self._q.get()
            if item is None:
                break
            slot, frame, ts_ns, host_ns, block_id, resid_ns = item
            if slot < next_slot:
                self.n_late += 1
                continue
            fill_to(slot)
            self._write(frame)
            self._csv.writerow([slot, "ok", ts_ns, host_ns, block_id, f"{resid_ns / 1000:.1f}"])
            self.max_abs_resid_ns = max(self.max_abs_resid_ns, abs(resid_ns))
            self._sum_resid += resid_ns
            self._n_resid += 1
            last = frame
            next_slot += 1
        fill_to(self._total or 0)
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        self._proc.wait()
        self._csv_file.close()
        self._errlog.close()
        if self._proc.returncode != 0 and not self.error:
            self.error = f"ffmpeg exited with code {self._proc.returncode}; see {self.path}.ffmpeg.log"
