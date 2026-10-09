"""Basler camera handling (pylon / pypylon)."""

from __future__ import annotations

import threading
from collections import deque

import numpy as np
from pypylon import pylon

from .clock import ClockMap, now_ns
from .config import Settings
from .encoder import PIX_FMT

EMU_CLASS = "BaslerCamEmu"


def discover(demo: bool = False) -> list:
    """Device infos of the cameras to use: GigE cameras, or pylon's emulated
    cameras in demo mode."""
    devices = pylon.TlFactory.GetInstance().EnumerateDevices()
    if demo:
        return [d for d in devices if d.GetDeviceClass() == EMU_CLASS]
    real = [d for d in devices if d.GetDeviceClass() != EMU_CLASS]
    return sorted(real, key=lambda d: d.GetSerialNumber())


class Camera:
    PREVIEW_INTERVAL_NS = 45_000_000  # ~22 previews per second

    def __init__(self, info, index: int, log):
        self.index = index
        self.log = log
        self.serial = info.GetSerialNumber()
        self.model = info.GetModelName()
        self.is_emu = info.GetDeviceClass() == EMU_CLASS
        try:
            self.ip = info.GetIpAddress()
        except Exception:
            self.ip = ""
        self.name = f"Camera {index + 1}"
        self.cam = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateDevice(info))
        self.cam.Open()
        self.lock = threading.RLock()  # serialises parameter access
        self.clock = ClockMap(nominal_slope=1.0)  # camera ns <-> host ns (free-run mode)
        self.width = self.height = 0
        self.pixel_format = "Mono8"
        self.preview_full = False  # full-resolution preview (enlarged view)
        self.on_frame = None  # callback(camera, frame, ts_ns, host_ns, block_id)
        self.last_ref = 0  # time of the newest frame on the shared timeline
        self.n_ok = 0
        self.n_failed = 0
        self.last_error = ""
        self._preview = None
        self._preview_seq = 0
        self._last_preview_ns = 0
        self._arrivals: deque[int] = deque(maxlen=60)
        self._run = False
        self._thread: threading.Thread | None = None
        self._converter = pylon.ImageFormatConverter()
        self._converter.OutputPixelFormat = pylon.PixelType_RGB8packed

    # ------------------------------------------------------------ parameters
    def has(self, name: str) -> bool:
        try:
            return getattr(self.cam, name).IsReadable() or getattr(self.cam, name).IsWritable()
        except Exception:
            return False

    def get(self, *names, default=None):
        """Value of the first of ``names`` this camera can read."""
        with self.lock:
            for name in names:
                try:
                    return getattr(self.cam, name).GetValue()
                except Exception:
                    continue
        return default

    def set(self, names, value) -> bool:
        """Set the first of ``names`` this camera accepts; numbers are clamped
        to the allowed range and increment."""
        if isinstance(names, str):
            names = (names,)
        with self.lock:
            for name in names:
                try:
                    node = getattr(self.cam, name)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        lo, hi = node.GetMin(), node.GetMax()
                        value = type(lo)(min(max(value, lo), hi))
                        if isinstance(lo, int):
                            inc = node.GetInc() or 1
                            value = lo + (value - lo) // inc * inc
                    node.SetValue(value)
                    return True
                except Exception:
                    continue
        return False

    def execute(self, name: str) -> bool:
        with self.lock:
            try:
                getattr(self.cam, name).Execute()
                return True
            except Exception:
                return False

    # ------------------------------------------------------------- configure
    def configure(self, s: Settings, link_share_bytes: int) -> None:
        """Image format, region of interest and network settings.  Must be
        called while not grabbing."""
        with self.lock:
            self.set("TriggerMode", "Off")
            self.set("AcquisitionFrameRateEnable", False)
            formats = list(self.cam.PixelFormat.GetSymbolics())
            wanted = [f for f in formats if f.startswith("Bayer") and f in PIX_FMT] if s.color else []
            fmt = (wanted or ["Mono8"])[0]
            self.set("PixelFormat", fmt)

            wmax = self.get("WidthMax", "SensorWidth", default=s.width)
            hmax = self.get("HeightMax", "SensorHeight", default=s.height)
            self.set("OffsetX", 0)
            self.set("OffsetY", 0)
            self.set("Width", min(s.width, wmax) // 2 * 2)
            self.set("Height", min(s.height, hmax) // 2 * 2)
            self.width = int(self.cam.Width.GetValue())
            self.height = int(self.cam.Height.GetValue())
            # centre the region; even offsets keep the Bayer pattern unchanged
            self.set("OffsetX", (wmax - self.width) // 4 * 2)
            self.set("OffsetY", (hmax - self.height) // 4 * 2)
            self.pixel_format = self.cam.PixelFormat.GetValue()
            if self.pixel_format not in PIX_FMT:
                raise RuntimeError(f"{self.name}: unsupported pixel format {self.pixel_format}")

            self.set("GevSCPSPacketSize", s.packet_size)
            self.set("GevSCBWR", s.bandwidth_reserve_pct)
            self.set("DeviceLinkThroughputLimitMode", "On")
            self.set("DeviceLinkThroughputLimit", int(link_share_bytes))
            try:
                self.cam.MaxNumBuffer.SetValue(48)
            except Exception:
                pass

    def apply_exposure(self, s: Settings, period_us: int | None = None) -> None:
        """Exposure, gain and white balance; safe while grabbing."""
        limit_us = 0.9 * period_us if period_us else 1e9
        with self.lock:
            if s.exposure_auto:
                self.set(("AutoExposureTimeUpperLimit", "AutoExposureTimeAbsUpperLimit"),
                         float(min(s.exposure_auto_max_ms * 1000.0, limit_us)))  # fmt: skip
                self.set("ExposureAuto", "Continuous")
            else:
                self.set("ExposureAuto", "Off")
                self.set(("ExposureTime", "ExposureTimeAbs"), float(min(s.exposure_ms * 1000.0, limit_us)))
            if s.gain_auto:
                self.set(("AutoGainUpperLimit", "AutoGainRawUpperLimit"), float(s.gain_auto_max_db))
                self.set("GainAuto", "Continuous")
            else:
                self.set("GainAuto", "Off")
                if not self.set("Gain", float(s.gain_db)):
                    self.set("GainRaw", int(s.gain_db))
            if self.pixel_format != "Mono8":
                self.set("BalanceWhiteAuto", "Continuous" if s.white_balance_auto else "Off")

    def max_fps(self, s: Settings) -> float:
        """Highest frame rate for the current region, exposure and link share."""
        with self.lock:
            self.set("ExposureAuto", "Off")
            # auto exposure adapts to the frame period, so it must not limit it
            self.set(("ExposureTime", "ExposureTimeAbs"), 1000.0 if s.exposure_auto else s.exposure_ms * 1000.0)
            fps = self.get("BslResultingAcquisitionFrameRate", "ResultingFrameRate", "ResultingFrameRateAbs")
        if self.is_emu or not fps:
            return 30.0  # emulated cameras report rates the PC cannot actually render
        return float(fps)

    # ------------------------------------------------------------------ sync
    @property
    def supports_ptp(self) -> bool:
        return self.has("PtpEnable") and self.has("BslPeriodicSignalPeriod")

    def ptp_status(self) -> tuple[str, int]:
        """(role, offset from master in ns)."""
        with self.lock:
            try:
                self.cam.PtpDataSetLatch.Execute()
                return self.cam.PtpStatus.GetValue(), int(self.cam.PtpOffsetFromMaster.GetValue())
            except Exception:
                return "Unknown", 0

    def trigger_periodic(self, period_us: int) -> None:
        """Expose on a periodic signal derived from the PTP-synchronised clock:
        every camera fires at the same instants."""
        with self.lock:
            self.cam.BslPeriodicSignalPeriod.SetValue(float(period_us))
            self.cam.BslPeriodicSignalDelay.SetValue(0.0)
            self.cam.TriggerSelector.SetValue("FrameStart")
            self.cam.TriggerSource.SetValue("PeriodicSignal1")
            self.cam.TriggerMode.SetValue("On")

    def trigger_freerun(self, fps: float) -> None:
        with self.lock:
            self.set("TriggerMode", "Off")
            self.set("AcquisitionFrameRateEnable", True)
            self.set(("AcquisitionFrameRate", "AcquisitionFrameRateAbs"), float(fps))

    def latch_clock(self) -> tuple[int, int] | None:
        """Read the camera clock (best of 3 tries).  The (host ns, camera ns)
        pair is added to ``self.clock`` and returned."""
        best = None
        with self.lock:
            for _ in range(3):
                try:
                    a = now_ns()
                    self.cam.TimestampLatch.Execute()
                    b = now_ns()
                    v = int(self.cam.TimestampLatchValue.GetValue())
                except Exception:
                    return None
                if best is None or b - a < best[0]:
                    best = (b - a, (a + b) // 2, v)
        if best is None or best[0] > 5_000_000:
            return None
        self.clock.add(best[1], best[2])
        return best[1], best[2]

    def temperature(self) -> tuple[float | None, str]:
        return self.get("DeviceTemperature"), str(self.get("BslTemperatureStatus", "TemperatureState", default=""))

    # ------------------------------------------------------------- streaming
    def start(self) -> None:
        self._run = True
        self.n_ok = self.n_failed = 0
        self.cam.StartGrabbing(pylon.GrabStrategy_OneByOne)
        self._thread = threading.Thread(target=self._grab_loop, daemon=True, name=f"grab{self.index}")
        self._thread.start()

    def stop(self) -> None:
        self._run = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        try:
            self.cam.StopGrabbing()
        except Exception:
            pass

    def close(self) -> None:
        self.stop()
        self.set("TriggerMode", "Off")  # leave the camera usable in pylon Viewer
        try:
            self.cam.Close()
        except Exception:
            pass

    def _grab_loop(self) -> None:
        while self._run:
            try:
                res = self.cam.RetrieveResult(1000, pylon.TimeoutHandling_Return)
            except Exception as e:
                if self._run:
                    self.last_error = str(e)
                    self.log(f"ERROR: {self.name}: {e}")
                break
            host = now_ns()
            if res is None or not res.IsValid():
                continue
            try:
                if not res.GrabSucceeded():
                    self.n_failed += 1
                    self.last_error = res.GetErrorDescription()
                    continue
                frame = res.GetArray()  # copy: the pylon buffer is reused
                ts = res.GetTimeStamp() or host
                self.n_ok += 1
                self._arrivals.append(host)
                if self.on_frame:
                    self.on_frame(self, frame, ts, host, res.GetBlockID())
                if host - self._last_preview_ns >= self.PREVIEW_INTERVAL_NS:
                    self._last_preview_ns = host
                    self._preview = self._make_preview(res, frame)
                    self._preview_seq += 1
            except Exception as e:
                self.last_error = str(e)
            finally:
                res.Release()

    def _make_preview(self, res, frame: np.ndarray) -> np.ndarray:
        """RGB image for the screen: full resolution when this camera is the
        enlarged view, otherwise a fast half-resolution demosaic."""
        if self.preview_full:
            return np.ascontiguousarray(self._converter.Convert(res).GetArray())
        if self.pixel_format == "Mono8":
            g = frame[::2, ::2]
            return np.ascontiguousarray(np.dstack((g, g, g)))
        a, b = frame[0::2, 0::2], frame[0::2, 1::2]
        c, d = frame[1::2, 0::2], frame[1::2, 1::2]
        h, w = d.shape
        a, b, c = a[:h, :w], b[:h, :w], c[:h, :w]
        rgb = np.empty((h, w, 3), dtype=np.uint8)

        def avg(x, y):
            return ((x.astype(np.uint16) + y) >> 1).astype(np.uint8)

        if self.pixel_format == "BayerRG8":
            rgb[..., 0], rgb[..., 1], rgb[..., 2] = a, avg(b, c), d
        elif self.pixel_format == "BayerBG8":
            rgb[..., 0], rgb[..., 1], rgb[..., 2] = d, avg(b, c), a
        elif self.pixel_format == "BayerGR8":
            rgb[..., 0], rgb[..., 1], rgb[..., 2] = b, avg(a, d), c
        else:  # BayerGB8
            rgb[..., 0], rgb[..., 1], rgb[..., 2] = c, avg(a, d), b
        return rgb

    def preview(self) -> tuple[np.ndarray | None, int]:
        return self._preview, self._preview_seq

    def measured_fps(self) -> float:
        a = self._arrivals
        if len(a) < 2 or now_ns() - a[-1] > 2e9:
            return 0.0
        return (len(a) - 1) * 1e9 / (a[-1] - a[0])
