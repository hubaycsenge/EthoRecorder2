"""Recording engine: owns the cameras and the audio input, keeps them on one
timeline, and turns a recording into finished files.  No GUI code here, so it
also runs headless (see ``__main__``).

Timeline
--------
* PTP mode (two or more ace 2 GigE cameras): the cameras synchronise their
  clocks with each other over the network (IEEE 1588) and each one exposes on
  a periodic signal derived from that shared clock.  The shared camera clock
  is the reference timeline; frame timestamps of all cameras fall on one grid.
* Free-run mode (fallback: one camera, emulated cameras, or PTP failed): every
  camera runs at the same nominal rate on its own clock.  The host clock is
  the reference timeline and frames are matched to the nearest grid slot.

Audio samples are placed on the reference timeline through host-clock
timestamps of the audio blocks.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import APP_NAME, __version__
from .audio import AudioInput, pick_input
from .cameras import Camera, discover
from .clock import ClockMap, fit_arrivals, fit_line, now_ns
from .config import MAX_CAMERAS, Settings
from .encoder import VideoWriter, run_ffmpeg
from .resample import resample_file

FPS_MARGIN = 0.985  # run slightly below the theoretical maximum
START_LEAD_S = 0.8  # recordings start this far in the future, on a grid tick
MIN_FIT_SPAN_NS = 30e9  # clock-rate fits need at least this much data


@dataclass
class Recording:
    folder: Path
    name: str
    t0_ref: int  # reference-timeline time of frame 0 (ns)
    period_ns: int
    writers: list[VideoWriter]
    started: datetime
    stop_slot: int | None = None
    audio_start_idx: int = 0
    ref_samples: list = field(default_factory=list)  # (host ns, reference ns)
    audio_overflows_before: int = 0
    t_start_host: int = 0


class Engine:
    def __init__(self, settings: Settings, log=print, demo: bool = False):
        self.s = settings
        self.demo = demo
        self._log = log
        self.state = "idle"  # idle | preview | recording | finalizing
        self.cameras: list[Camera] = []
        self.audio: AudioInput | None = None
        self.sync_mode = "freerun"  # ptp | freerun
        self.period_us = 0
        self.max_fps = 0.0
        self.ref_clock = ClockMap()
        self.rec: Recording | None = None
        self.ptp_state: list[tuple[str, int]] = []
        self.temps: list[tuple[float | None, str]] = []
        self.progress = ""  # what the finalising step is doing
        self.on_finished = None  # callback(summary dict) after finalising
        self._calib_run = False
        self._calib_thread: threading.Thread | None = None
        self._warned: set[str] = set()

    def log(self, msg: str) -> None:
        self._log(f"{datetime.now():%H:%M:%S}  {msg}")

    @property
    def fps(self) -> float:
        return 1e6 / self.period_us if self.period_us else 0.0

    # ------------------------------------------------------------ connecting
    def connect(self) -> None:
        if self.demo:
            os.environ.setdefault("PYLON_CAMEMU", "2")
        infos = discover(self.demo)
        if not infos:
            raise RuntimeError(
                "No Basler camera found. Check that the switch is powered, the network cable is "
                "plugged into the PC, and that no other program (pylon Viewer) is using the cameras."
            )
        if len(infos) > MAX_CAMERAS:
            self.log(
                f"WARNING: {len(infos)} cameras found - using the {MAX_CAMERAS} with the lowest serial numbers, "
                "ignoring " + ", ".join(d.GetSerialNumber() for d in infos[MAX_CAMERAS:])
            )
            infos = infos[:MAX_CAMERAS]
        try:
            for i, info in enumerate(infos):
                cam = Camera(info, i, self.log)
                self.cameras.append(cam)
                self.log(f"{cam.name}: {cam.model}  serial {cam.serial}  {cam.ip}")
            self.open_audio()
            self._start_stream()
        except Exception:
            self.disconnect()
            raise
        self.state = "preview"

    def disconnect(self) -> None:
        self._stop_stream()
        for cam in self.cameras:
            cam.close()
        self.cameras = []
        if self.audio:
            self.audio.close()
            self.audio = None
        self.state = "idle"

    def open_audio(self) -> None:
        """(Re)open the audio input named in the settings."""
        if self.audio:
            self.audio.close()
            self.audio = None
        if self.s.audio_device == "-":  # the user chose "no audio"
            self.log("Audio: switched off - recording video only")
            return
        dev = pick_input(self.s.audio_device)
        if dev is None:
            self.log("WARNING: no audio input found - recording video only")
            return
        try:
            self.audio = AudioInput(dev, self.s.audio_rate, self.s.audio_channels, self.log)
            self.log(f"Audio: {dev['name']}  {self.audio.rate} Hz  {self.audio.channels} ch")
        except Exception as e:
            self.log(f"WARNING: {e} - recording video only")

    def apply_settings(self) -> None:
        """Re-configure resolution / frame rate (stops the live view briefly)."""
        if self.state != "preview":
            raise RuntimeError("settings can only be changed while not recording")
        self._stop_stream()
        self._start_stream()

    def apply_exposure(self) -> None:
        for cam in self.cameras:
            cam.apply_exposure(self.s, self.period_us)

    def white_balance_once(self) -> None:
        for cam in self.cameras:
            cam.set("BalanceWhiteAuto", "Once")

    def _start_stream(self) -> None:
        s = self.s
        share = int(s.link_budget_mbytes * 1e6 / len(self.cameras))
        for cam in self.cameras:
            cam.configure(s, share)
        self.sync_mode = self._sync_clocks()
        self.max_fps = min(cam.max_fps(s) for cam in self.cameras) * FPS_MARGIN
        fps = min(s.fps, self.max_fps) if s.fps > 0 else self.max_fps
        self.period_us = math.ceil(1e6 / fps)
        for cam in self.cameras:
            if self.sync_mode == "ptp":
                cam.trigger_periodic(self.period_us)
            else:
                cam.trigger_freerun(self.fps)
            cam.apply_exposure(s, self.period_us)
            cam.clock = ClockMap()
            cam.on_frame = self._on_frame
        self.ref_clock = ClockMap()
        self._warned.clear()
        for _ in range(3):
            self._calibrate_once()
        for cam in self.cameras:
            cam.start()
        c0 = self.cameras[0]
        self.log(
            f"Streaming {c0.width} x {c0.height} {c0.pixel_format} at {self.fps:.3f} fps "
            f"(maximum {self.max_fps:.2f}); sync: "
            + ("PTP hardware-timed, simultaneous exposure" if self.sync_mode == "ptp" else "free-run (software matched)")
        )
        self._calib_run = True
        self._calib_thread = threading.Thread(target=self._calib_loop, daemon=True, name="calib")
        self._calib_thread.start()

    def _stop_stream(self) -> None:
        self._calib_run = False
        if self._calib_thread:
            self._calib_thread.join(timeout=5.0)
            self._calib_thread = None
        for cam in self.cameras:
            cam.stop()

    def _sync_clocks(self) -> str:
        cams = self.cameras
        if len(cams) < 2:
            return "freerun"
        if not all(c.supports_ptp for c in cams):
            self.log("WARNING: cameras without PTP - falling back to free-run")
            return "freerun"
        for c in cams:
            c.set("PtpEnable", True)
        self.log("Synchronising camera clocks (PTP) - the first time takes about 30 s ...")
        deadline = time.monotonic() + self.s.ptp_timeout_s
        good, last_report = 0, 0.0
        while time.monotonic() < deadline:
            st = [c.ptp_status() for c in cams]
            self.ptp_state = st
            roles = [r for r, _ in st]
            ok = (
                all(r in ("Master", "Slave") for r in roles)
                and roles.count("Master") <= 1
                and all(abs(off) <= self.s.ptp_max_offset_ns for r, off in st if r == "Slave")
            )
            good = good + 1 if ok else 0
            if good >= 3:
                if all(c.get("BslPeriodicSignalSource") == "PtpClock" for c in cams):
                    offs = [off for r, off in st if r == "Slave"]
                    self.log(f"PTP locked: {', '.join(roles)}; offset {max(map(abs, offs), default=0)} ns")
                    return "ptp"
                good = 0
            if time.monotonic() - last_report > 5:
                last_report = time.monotonic()
                self.log("  PTP: " + ", ".join(f"{r} ({off} ns)" for r, off in st))
            time.sleep(0.5 if ok else 1.0)
        self.log("WARNING: PTP did not lock - falling back to free-run (cameras NOT exposure-synchronised)")
        return "freerun"

    # ------------------------------------------------------- clock tracking
    def _calibrate_once(self) -> None:
        for cam in self.cameras:
            if cam.is_emu:
                continue
            pair = cam.latch_clock()
            if pair and self.sync_mode == "ptp":
                self.ref_clock.add(*pair)
                rec = self.rec
                if rec is not None:
                    rec.ref_samples.append(pair)

    def _calib_loop(self) -> None:
        n = 0
        while self._calib_run:
            time.sleep(0.25 if n < 20 else 1.0)
            n += 1
            try:
                self._calibrate_once()
                if n % 3 == 0:
                    self.temps = [c.temperature() for c in self.cameras]
                    if self.sync_mode == "ptp":
                        self.ptp_state = [c.ptp_status() for c in self.cameras]
                        bad = any(
                            r not in ("Master", "Slave") or abs(off) > 10 * self.s.ptp_max_offset_ns
                            for r, off in self.ptp_state
                        )
                        self._warn_once("ptp", bad, "WARNING: PTP synchronisation degraded: " + str(self.ptp_state))
                    for c in self.cameras:
                        self._warn_once(
                            f"stall{c.index}", n > 6 and c.measured_fps() == 0.0,
                            f"ERROR: {c.name} stopped delivering images - check its network cable and the switch",
                        )  # fmt: skip
                    for c, (_, tstate) in zip(self.cameras, self.temps):
                        self._warn_once(
                            f"temp{c.index}", tstate not in ("", "Ok"),
                            f"WARNING: {c.name} temperature state is '{tstate}' - improve cooling",
                        )  # fmt: skip
            except Exception as e:
                self._warn_once("calib", True, f"WARNING: clock tracking: {e}")

    def _warn_once(self, key: str, condition: bool, msg: str) -> None:
        if condition and key not in self._warned:
            self._warned.add(key)
            self.log(msg)
        elif not condition:
            self._warned.discard(key)

    def _ref_to_host(self, ref_ns: int) -> int:
        return self.ref_clock.to_host(ref_ns) if self.sync_mode == "ptp" else ref_ns

    # ------------------------------------------------------------ frame path
    def _on_frame(self, cam: Camera, frame, ts_ns: int, host_ns: int, block_id: int) -> None:
        """Runs in each camera's grab thread."""
        if self.sync_mode == "ptp":
            ref = ts_ns
        elif len(cam.clock):
            ref = cam.clock.to_host(ts_ns)
        else:
            ref = host_ns
        cam.last_ref = ref
        rec = self.rec
        if rec is None:
            return
        slot = (ref - rec.t0_ref + rec.period_ns // 2) // rec.period_ns
        if slot < 0 or (rec.stop_slot is not None and slot >= rec.stop_slot):
            return
        resid = ref - (rec.t0_ref + slot * rec.period_ns)
        rec.writers[cam.index].put(slot, frame, ts_ns, host_ns, block_id, resid)

    # ------------------------------------------------------------- recording
    def start_recording(self) -> Path:
        if self.state != "preview":
            raise RuntimeError("not ready to record")
        s = self.s
        for cam in self.cameras:
            if cam.measured_fps() == 0.0:
                raise RuntimeError(f"{cam.name} is not delivering images - cannot start recording")
        name = re.sub(r"[^\w\-]+", "_", s.session_name).strip("_") or "session"
        name = f"{name}_{datetime.now():%Y%m%d_%H%M%S}"
        folder = Path(s.output_dir).expanduser() / name
        folder.mkdir(parents=True, exist_ok=False)

        period_ns = self.period_us * 1000
        lead = math.ceil(START_LEAD_S * 1e9 / period_ns)
        t0 = max(c.last_ref for c in self.cameras) + lead * period_ns
        queue_len = max(8, int(3 * self.fps))
        writers = [
            VideoWriter(
                folder / f"{name}_cam{c.index + 1}.rec.mp4", folder / f"{name}_cam{c.index + 1}_frames.csv",
                c.width, c.height, c.pixel_format, self.period_us, s.crf, s.x264_preset, queue_len, self.log,
            )
            for c in self.cameras
        ]  # fmt: skip
        t0_host = self._ref_to_host(t0)
        rec = Recording(
            folder, name, t0, period_ns, writers,
            started=datetime.now() + timedelta(seconds=(t0_host - now_ns()) / 1e9),
            ref_samples=self.ref_clock.samples(), t_start_host=t0_host,
        )  # fmt: skip
        if self.audio:
            latency_ns = int(s.audio_latency_ms * 1e6)
            rec.audio_overflows_before = self.audio.overflows
            rec.audio_start_idx = self.audio.start_recording(str(folder / f"{name}_audio_raw.wav"), t0_host + latency_ns)
        self.rec = rec
        self.state = "recording"
        self.log(f"RECORDING to {folder}")
        return folder

    def stop_recording(self) -> None:
        """Returns immediately; files are finished in a background thread and
        ``on_finished`` is called when they are ready."""
        rec = self.rec
        if self.state != "recording" or rec is None:
            return
        rec.stop_slot = max(1, max(w.last_slot_seen for w in rec.writers) + 1)
        self.state = "finalizing"
        self.progress = "finishing video files"
        self.log("Stopping - finishing files ...")
        threading.Thread(target=self._finalize, args=(rec,), daemon=True, name="finalize").start()

    def recording_status(self) -> dict:
        rec = self.rec
        if rec is None:
            return {}
        frames = max((w.last_slot_seen + 1 for w in rec.writers), default=0)
        return {
            "elapsed_s": max(0.0, (now_ns() - rec.t_start_host) / 1e9),
            "frames": frames,
            "filled": [w.n_filled for w in rec.writers],
            "queue_drops": [w.n_queue_drops for w in rec.writers],
            "errors": [w.error for w in rec.writers if w.error],
            "folder": str(rec.folder),
        }

    def _finalize(self, rec: Recording) -> None:
        summary: dict = {"folder": str(rec.folder), "ok": False}
        try:
            summary = self._finalize_inner(rec)
        except Exception as e:
            self.log(f"ERROR while finishing the recording: {e}")
            summary["error"] = str(e)
        finally:
            self.rec = None
            self.progress = ""
            self.state = "preview" if self.cameras else "idle"
            if self.on_finished:
                self.on_finished(summary)

    def _finalize_inner(self, rec: Recording) -> dict:
        s = self.s
        time.sleep(0.3 + 3 * rec.period_ns / 1e9)  # let the last frames arrive
        n_frames = rec.stop_slot
        self.rec = None
        for w in rec.writers:
            w.finish(n_frames)
        duration_ns = n_frames * rec.period_ns
        latency_ns = int(s.audio_latency_ms * 1e6)
        blocks = []
        audio = self.audio
        if audio:
            blocks = audio.stop_recording(self._ref_to_host(rec.t0_ref + duration_ns) + latency_ns)
        for w in rec.writers:
            w.join()

        base = rec.folder / rec.name
        raw_wav = Path(f"{base}_audio_raw.wav")
        wav = Path(f"{base}_audio.wav")
        audio_info = None
        if audio and blocks:
            self.progress = "aligning audio"
            audio_info = self._align_audio(rec, audio, blocks, raw_wav, wav, duration_ns, latency_ns)
            if not s.keep_raw_audio:
                raw_wav.unlink(missing_ok=True)

        files = []
        for cam, w in zip(self.cameras, rec.writers):
            self.progress = f"writing final video {cam.index + 1} of {len(rec.writers)}"
            tmp = Path(w.path)
            final = Path(f"{base}_cam{cam.index + 1}.mp4")
            args = ["-i", str(tmp)]
            if audio_info and s.embed_audio:
                args += ["-i", str(wav), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k"]
            else:
                args += ["-c", "copy"]
            try:
                run_ffmpeg(args + ["-movflags", "+faststart", str(final)])
                tmp.unlink()
                files.append(final.name)
            except Exception as e:
                self.log(f"ERROR: {cam.name}: {e} - the unprocessed video is kept as {tmp.name}")
                files.append(tmp.name)
            errlog = Path(w.path + ".ffmpeg.log")
            if errlog.exists() and errlog.stat().st_size == 0:
                errlog.unlink()

        cam_info = []
        for cam, w in zip(self.cameras, rec.writers):
            cam_info.append(
                {
                    "name": cam.name, "model": cam.model, "serial": cam.serial, "ip": cam.ip,
                    "width": cam.width, "height": cam.height, "pixel_format": cam.pixel_format,
                    "exposure_us": cam.get("ExposureTime", "ExposureTimeAbs"), "gain_db": cam.get("Gain"),
                    "frames_in_file": w.n_written, "frames_repeated_to_fill_gaps": w.n_filled,
                    "frames_dropped_encoder_busy": w.n_queue_drops, "frames_late": w.n_late,
                    "failed_buffers_since_stream_start": cam.n_failed,
                    "grid_error_mean_us": round(w.mean_resid_ns / 1000, 3),
                    "grid_error_max_us": round(w.max_abs_resid_ns / 1000, 3),
                    "error": w.error,
                }
            )  # fmt: skip
        meta = {
            "app": f"{APP_NAME} {__version__}",
            "recording_started": rec.started.isoformat(timespec="milliseconds"),
            "duration_s": round(duration_ns / 1e9, 6),
            "frames": n_frames,
            "frame_period_us": self.period_us,
            "fps": 1e6 / self.period_us,
            "sync_mode": self.sync_mode,
            "ptp_state_at_end": [{"role": r, "offset_from_master_ns": o} for r, o in self.ptp_state]
            if self.sync_mode == "ptp" else None,
            "cameras": cam_info,
            "audio": audio_info,
            "files": files + ([wav.name] if audio_info else []),
            "settings": asdict(s),
        }  # fmt: skip
        Path(f"{base}_session.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

        filled = sum(w.n_filled for w in rec.writers)
        self.log(
            f"Saved {n_frames} frames per camera ({duration_ns / 1e9:.1f} s) in {rec.folder}"
            + (f"; {filled} frame(s) missing and filled by repeats" if filled else "; no missing frames")
        )
        if self.sync_mode == "ptp" and len(rec.writers) > 1:
            spread = max(w.mean_resid_ns for w in rec.writers) - min(w.mean_resid_ns for w in rec.writers)
            self.log(f"Camera-to-camera exposure offset: {spread / 1000:.2f} us (mean)")
        if audio_info and audio_info["drift_corrected"]:
            self.log(f"Audio clock ran {audio_info['clock_offset_ppm']:+.1f} ppm vs. the cameras - corrected")
        meta["folder"] = str(rec.folder)
        meta["ok"] = not any(w.error for w in rec.writers)
        return meta

    def _align_audio(self, rec, audio, blocks, raw_wav, wav, duration_ns, latency_ns) -> dict:
        """Write ``wav`` so that its sample n lies n/rate seconds after video
        frame 0, correcting the audio interface's clock rate."""
        fs = audio.rate
        ia, ha, sa = fit_arrivals([b[1] for b in blocks], [b[0] for b in blocks], 1e9 / fs, MIN_FIT_SPAN_NS)
        if self.sync_mode == "ptp" and rec.ref_samples:
            t_lo = rec.t_start_host - 60e9
            pairs = [p for p in rec.ref_samples if p[0] >= t_lo]
            rr, hr, sr = fit_line([p[0] for p in pairs], [p[1] for p in pairs], 1.0, MIN_FIT_SPAN_NS)
            host0 = hr + sr * (rec.t0_ref - rr)
        else:
            sr, host0 = 1.0, float(rec.t0_ref)
        ratio = sr * 1e9 / (fs * sa)
        rate_measured = sa != 1e9 / fs  # False for recordings too short to measure drift
        ppm = (ratio - 1.0) * 1e6
        if abs(ppm) > 500:
            self.log(f"WARNING: implausible audio clock rate ({ppm:+.0f} ppm) - not correcting drift")
            ratio = 1.0
        start_pos = ia + (host0 + latency_ns - ha) / sa - rec.audio_start_idx
        n_out = int(round(duration_ns * fs / 1e9))
        resample_file(str(raw_wav), str(wav), start_pos, ratio, n_out)
        overflows = audio.overflows - rec.audio_overflows_before
        if overflows:
            self.log(f"WARNING: the audio driver lost samples {overflows} time(s) during this recording - "
                     "sound may be slightly out of step after each gap")  # fmt: skip
        return {
            "device": audio.device["name"], "sample_rate": fs, "channels": audio.channels,
            "clock_offset_ppm": round(ppm, 2) if rate_measured else None,
            "drift_corrected": rate_measured and ratio != 1.0,
            "latency_correction_ms": self.s.audio_latency_ms,
            "raw_file_position_of_frame0_samples": round(start_pos, 2),
            "overflows": overflows,
            "write_error": audio.error,
        }  # fmt: skip

    # ---------------------------------------------------------------- status
    def free_disk_gb(self) -> float:
        p = Path(self.s.output_dir).expanduser()
        while not p.exists() and p != p.parent:
            p = p.parent
        try:
            return shutil.disk_usage(p).free / 1e9
        except OSError:
            return 0.0
