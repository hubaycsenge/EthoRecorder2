"""User settings, stored as JSON in the per-user configuration folder."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import APP_NAME

# (label, width, height); 0 x 0 means "largest the camera offers".
RESOLUTION_PRESETS = [
    ("Full sensor 2048 x 1536", 2048, 1536),
    ("1920 x 1440", 1920, 1440),
    ("1600 x 1200", 1600, 1200),
    ("1440 x 1080", 1440, 1080),
    ("1280 x 960", 1280, 960),
    ("1024 x 768", 1024, 768),
    ("800 x 600", 800, 600),
]

# (label, x264 CRF)
QUALITY_PRESETS = [
    ("Maximum (CRF 14, very large files)", 14),
    ("Very high (CRF 17)", 17),
    ("High (CRF 20)", 20),
    ("Standard (CRF 23)", 23),
    ("Compact (CRF 27)", 27),
]


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME


@dataclass
class Settings:
    # --- output
    output_dir: str = str(Path.home() / "EthoRecorder2_recordings")
    session_name: str = "session"
    crf: int = 20
    x264_preset: str = "veryfast"
    embed_audio: bool = True
    keep_raw_audio: bool = True
    duration_limit_min: float = 0.0  # 0 = record until Stop is pressed

    # --- image
    width: int = 2048
    height: int = 1536
    color: bool = True
    fps: float = 0.0  # 0 = highest rate the link allows

    # --- exposure
    exposure_auto: bool = True
    exposure_ms: float = 5.0
    exposure_auto_max_ms: float = 30.0
    gain_auto: bool = True
    gain_db: float = 0.0
    gain_auto_max_db: float = 12.0  # high gain = noise = much larger files
    white_balance_auto: bool = True

    # --- audio
    audio_device: str = ""  # device name; empty = pick a Zoom recorder if present
    audio_rate: int = 48000
    audio_channels: int = 2
    audio_latency_ms: float = 0.0  # calibration: how late sound is stamped vs. video

    # --- network (advanced, edit the JSON file)
    link_budget_mbytes: float = 122.0  # total bytes/s shared by all cameras on one GigE link
    bandwidth_reserve_pct: int = 10
    packet_size: int = 1500  # set 8192 only when jumbo frames are enabled end to end
    ptp_timeout_s: float = 90.0
    ptp_max_offset_ns: int = 10000

    @classmethod
    def path(cls) -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return s
        for f in fields(cls):
            if f.name in data:
                try:
                    setattr(s, f.name, type(getattr(s, f.name))(data[f.name]))
                except (TypeError, ValueError):
                    pass
        return s

    def save(self) -> None:
        try:
            self.path().parent.mkdir(parents=True, exist_ok=True)
            self.path().write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except OSError:
            pass
