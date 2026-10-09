"""Main window (PySide6)."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QRect, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QVBoxLayout, QWidget,
)  # fmt: skip

from . import APP_NAME, __version__
from .audio import list_inputs, pick_input
from .config import QUALITY_PRESETS, RESOLUTION_PRESETS, Settings
from .engine import Engine

REC_RED = "#d93025"


class Bridge(QObject):
    """Carries results of background threads back to the GUI thread."""

    log = Signal(str)
    done = Signal(object, object)  # (callback, result or exception)
    finished = Signal(dict)


class ImageView(QWidget):
    """Shows one camera image, scaled to fit, with a caption."""

    double_clicked = Signal(int)

    def __init__(self, index: int):
        super().__init__()
        self.index = index
        self.caption = ""
        self.recording = False
        self._image: QImage | None = None
        self._buf = None
        self._seq = -1
        self.setMinimumSize(240, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_frame(self, rgb: np.ndarray | None, seq: int) -> None:
        if rgb is None or seq == self._seq:
            return
        self._seq = seq
        h, w, _ = rgb.shape
        self._buf = rgb  # keep the pixel data alive while QImage points at it
        self._image = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888)
        self.update()

    def clear(self) -> None:
        self._image = self._buf = None
        self._seq = -1
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:
        self.double_clicked.emit(self.index)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#101214"))
        if self._image is not None:
            iw, ih = self._image.width(), self._image.height()
            scale = min(self.width() / iw, self.height() / ih)
            w, h = int(iw * scale), int(ih * scale)
            x, y = (self.width() - w) // 2, (self.height() - h) // 2
            p.setRenderHint(QPainter.SmoothPixmapTransform, scale < 1.0)
            p.drawImage(QRect(x, y, w, h), self._image)
        else:
            p.setPen(QColor("#7a7f85"))
            p.drawText(self.rect(), Qt.AlignCenter, "no image")
        font = QFont(self.font())
        font.setBold(True)
        p.setFont(font)
        fm = p.fontMetrics()
        text_w = fm.horizontalAdvance(self.caption) + 16 + (18 if self.recording else 0)
        p.fillRect(8, 8, text_w, fm.height() + 8, QColor(0, 0, 0, 170))
        x = 16
        if self.recording:
            p.setBrush(QColor(REC_RED))
            p.setPen(Qt.NoPen)
            p.drawEllipse(x, 8 + (fm.height() + 8 - 12) // 2, 12, 12)
            x += 18
        p.setPen(QColor("#ffffff"))
        p.drawText(x, 8 + 4 + fm.ascent(), self.caption)
        p.end()


class LevelMeter(QWidget):
    """Horizontal peak meters, one bar per audio channel, -60..0 dBFS."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(34)
        self._db = [-90.0, -90.0]
        self._hold = [-90.0, -90.0]
        self._clip = 0
        self.active = False

    def set_levels(self, peaks, clipped: bool) -> None:
        db = [20 * np.log10(max(float(p), 1e-5)) for p in peaks] or [-90.0]
        self._db = db
        self._hold = [max(d, h - 0.6) for d, h in zip(db, self._hold + [-90.0] * len(db))]
        self._clip = 25 if clipped else max(0, self._clip - 1)
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#101214"))
        n = max(1, len(self._db))
        bar_h = (self.height() - 6) // n - 2
        x0, w = 28, self.width() - 28 - 44
        p.setPen(QColor("#c9cdd2"))
        for i in range(n):
            y = 4 + i * (bar_h + 2)
            p.drawText(6, y + bar_h - 1, "LR"[i] if n <= 2 else str(i + 1))
            p.fillRect(x0, y, w, bar_h, QColor("#24282d"))
            if not self.active:
                continue
            frac = min(1.0, max(0.0, (self._db[i] + 60) / 60))
            color = "#d93025" if self._db[i] > -3 else "#e8a317" if self._db[i] > -12 else "#2e9e5b"
            p.fillRect(x0, y, int(w * frac), bar_h, QColor(color))
            hold = min(1.0, max(0.0, (self._hold[i] + 60) / 60))
            p.fillRect(x0 + int(w * hold) - 2, y, 2, bar_h, QColor("#ffffff"))
        if self._clip:
            p.fillRect(self.width() - 40, 4, 36, self.height() - 8, QColor(REC_RED))
            p.setPen(QColor("#ffffff"))
            p.drawText(self.width() - 40, 4, 36, self.height() - 8, Qt.AlignCenter, "CLIP")
        elif self.active:
            p.setPen(QColor("#7a7f85"))
            p.drawText(self.width() - 42, 4, 40, self.height() - 8, Qt.AlignCenter, f"{max(self._db):.0f} dB")
        p.end()


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, demo: bool):
        super().__init__()
        self.s = settings
        self.bridge = Bridge()
        self.bridge.log.connect(self._append_log)
        self.bridge.done.connect(lambda cb, result: cb(result))
        self.bridge.finished.connect(self._on_finished)
        self.engine = Engine(settings, log=self.bridge.log.emit, demo=demo)
        self.engine.on_finished = self.bridge.finished.emit
        self.busy = False
        self.selected = -1  # -1 = all cameras, else index of the enlarged camera
        self.views: list[ImageView] = []
        self._close_when_done = False

        self.setWindowTitle(f"{APP_NAME} {__version__}" + ("  [DEMO - emulated cameras]" if demo else ""))
        self.resize(1500, 900)
        self._build_ui()
        self._load_settings_into_ui()
        self._refresh_audio_devices()
        self._update_enabled()

        for key, fn in (
            ("F11", self.toggle_fullscreen), ("F", self.toggle_fullscreen), ("Escape", self.exit_fullscreen),
            ("0", lambda: self.select_view(-1)), ("1", lambda: self.select_view(0)), ("2", lambda: self.select_view(1)),
            ("3", lambda: self.select_view(2)), ("4", lambda: self.select_view(3)),
        ):  # fmt: skip
            QShortcut(QKeySequence(key), self, activated=fn)

        self.frame_timer = QTimer(self, interval=40, timeout=self._update_frames)
        self.frame_timer.start()
        self.status_timer = QTimer(self, interval=500, timeout=self._update_status)
        self.status_timer.start()

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        # ---- left: live view
        self.view_combo = QComboBox()
        self.view_combo.addItem("All cameras", -1)
        self.view_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.view_combo.setMinimumWidth(190)
        self.view_combo.currentIndexChanged.connect(lambda i: self.select_view(self.view_combo.itemData(i)))
        self.fullscreen_btn = QPushButton("Full screen (F11)")
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        hint = QLabel("Double-click an image to enlarge it; double-click again to go back.")
        hint.setStyleSheet("color: #7a7f85;")
        self.view_bar = QWidget()
        bar = QHBoxLayout(self.view_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(QLabel("View:"))
        bar.addWidget(self.view_combo)
        bar.addWidget(self.fullscreen_btn)
        bar.addWidget(hint, 1)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)
        self.placeholder = QLabel(
            "Not connected.\n\nSwitch on the PoE switch and the Zoom recorder (USB audio interface mode),\n"
            "wait about 30 seconds, then press Connect."
        )
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet("background: #101214; color: #9aa0a6; font-size: 15px;")
        self.grid.addWidget(self.placeholder, 0, 0)

        self.meter = LevelMeter()
        self.audio_label = QLabel("Audio: -")
        self.meter_row = QWidget()
        mrow = QHBoxLayout(self.meter_row)
        mrow.setContentsMargins(0, 0, 0, 0)
        mrow.addWidget(self.audio_label)
        mrow.addWidget(self.meter, 1)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(6, 6, 6, 6)
        lv.addWidget(self.view_bar)
        lv.addWidget(self.grid_host, 1)
        lv.addWidget(self.meter_row)

        # ---- right: controls
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setMinimumHeight(36)
        self.connect_btn.clicked.connect(self.on_connect_clicked)
        self.device_label = QLabel("No cameras connected.")
        self.device_label.setWordWrap(True)
        self.device_label.setTextFormat(Qt.RichText)
        self.audio_combo = QComboBox()
        self.audio_combo.activated.connect(self.on_audio_changed)
        self.audio_refresh = QPushButton("Rescan")
        self.audio_refresh.clicked.connect(self._refresh_audio_devices)
        arow = QHBoxLayout()
        arow.addWidget(self.audio_combo, 1)
        arow.addWidget(self.audio_refresh)
        dev_box = QGroupBox("1. Devices")
        dv = QVBoxLayout(dev_box)
        dv.addWidget(self.connect_btn)
        dv.addWidget(self.device_label)
        dv.addWidget(QLabel("Audio input:"))
        dv.addLayout(arow)

        self.res_combo = QComboBox()
        for label, w, h in RESOLUTION_PRESETS:
            self.res_combo.addItem(label, f"{w}x{h}")
        self.color_check = QCheckBox("Colour (off = greyscale)")
        self.fps_max_check = QCheckBox("Highest possible")
        self.fps_spin = QDoubleSpinBox(decimals=2, minimum=1.0, maximum=500.0, value=15.0, suffix=" fps")
        self.fps_max_check.toggled.connect(lambda on: self.fps_spin.setEnabled(not on))
        frow = QHBoxLayout()
        frow.addWidget(self.fps_max_check)
        frow.addWidget(self.fps_spin)
        self.apply_btn = QPushButton("Apply image settings")
        self.apply_btn.clicked.connect(self.on_apply_clicked)
        self.rate_label = QLabel("")
        self.rate_label.setWordWrap(True)
        img_box = QGroupBox("2. Image size and frame rate")
        iform = QFormLayout(img_box)
        iform.addRow("Resolution:", self.res_combo)
        iform.addRow("", self.color_check)
        iform.addRow("Frame rate:", frow)
        iform.addRow(self.apply_btn)
        iform.addRow(self.rate_label)

        self.exp_auto = QCheckBox("Automatic")
        self.exp_spin = QDoubleSpinBox(decimals=2, minimum=0.01, maximum=1000.0, suffix=" ms")
        self.gain_auto = QCheckBox("Automatic")
        self.gain_spin = QDoubleSpinBox(decimals=1, minimum=0.0, maximum=48.0, suffix=" dB")
        self.wb_auto = QCheckBox("Automatic")
        self.wb_once = QPushButton("Set once now")
        self.wb_once.clicked.connect(lambda: self.engine.white_balance_once())
        for wdg in (self.exp_auto, self.gain_auto, self.wb_auto):
            wdg.toggled.connect(self.on_exposure_changed)
        for wdg in (self.exp_spin, self.gain_spin):
            wdg.editingFinished.connect(self.on_exposure_changed)
        exp_box = QGroupBox("3. Brightness (both cameras)")
        eform = QFormLayout(exp_box)
        for label, a, b in (("Exposure:", self.exp_auto, self.exp_spin), ("Gain:", self.gain_auto, self.gain_spin),
                            ("White balance:", self.wb_auto, self.wb_once)):  # fmt: skip
            row = QHBoxLayout()
            row.addWidget(a)
            row.addWidget(b)
            eform.addRow(label, row)

        self.dir_edit = QLineEdit()
        browse = QPushButton("...")
        browse.setFixedWidth(34)
        browse.clicked.connect(self.on_browse)
        self.open_btn = QPushButton("Open")
        self.open_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.dir_edit.text()).expanduser())))
        )
        drow = QHBoxLayout()
        drow.addWidget(self.dir_edit, 1)
        drow.addWidget(browse)
        drow.addWidget(self.open_btn)
        self.name_edit = QLineEdit()
        self.quality_combo = QComboBox()
        for label, crf in QUALITY_PRESETS:
            self.quality_combo.addItem(label, crf)
        self.embed_check = QCheckBox("Put the sound into the video files too")
        self.limit_spin = QDoubleSpinBox(decimals=1, minimum=0.0, maximum=100000.0, suffix=" min")
        self.limit_spin.setSpecialValueText("until I press Stop")
        self.record_btn = QPushButton("●  Start recording")
        self.record_btn.setMinimumHeight(54)
        self.record_btn.clicked.connect(self.on_record_clicked)
        self.rec_label = QLabel("")
        self.rec_label.setWordWrap(True)
        self.rec_label.setTextFormat(Qt.RichText)
        rec_box = QGroupBox("4. Recording")
        rform = QFormLayout(rec_box)
        rform.addRow("Save in:", drow)
        rform.addRow("Name:", self.name_edit)
        rform.addRow("Quality:", self.quality_combo)
        rform.addRow("", self.embed_check)
        rform.addRow("Stop after:", self.limit_spin)
        rform.addRow(self.record_btn)
        rform.addRow(self.rec_label)

        self.log_view = QPlainTextEdit(readOnly=True, maximumBlockCount=2000)
        self.log_view.setMinimumHeight(110)
        log_box = QGroupBox("Messages")
        QVBoxLayout(log_box).addWidget(self.log_view)

        panel = QWidget()
        pv = QVBoxLayout(panel)
        for box in (dev_box, img_box, exp_box, rec_box):
            pv.addWidget(box)
        pv.addWidget(log_box, 1)
        self.side = QScrollArea()
        self.side.setWidgetResizable(True)
        self.side.setWidget(panel)
        self.side.setMinimumWidth(400)
        self.side.setMaximumWidth(520)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.side)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        self.setCentralWidget(split)
        self._style_record_button(False)

    def _style_record_button(self, recording: bool) -> None:
        color = "#3c4043" if recording else REC_RED
        self.record_btn.setText("■  Stop recording" if recording else "●  Start recording")
        self.record_btn.setStyleSheet(
            f"QPushButton {{ background: {color}; color: white; font-size: 17px; font-weight: bold;"
            " border-radius: 6px; } QPushButton:disabled { background: #9aa0a6; }"
        )

    # ------------------------------------------------------------- settings
    def _load_settings_into_ui(self) -> None:
        s = self.s
        sizes = [(w, h) for _, w, h in RESOLUTION_PRESETS]
        if (s.width, s.height) not in sizes:
            self.res_combo.addItem(f"Custom {s.width} x {s.height}", f"{s.width}x{s.height}")
        self.res_combo.setCurrentIndex(self.res_combo.findData(f"{s.width}x{s.height}"))
        self.color_check.setChecked(s.color)
        self.fps_max_check.setChecked(s.fps <= 0)
        self.fps_spin.setEnabled(s.fps > 0)
        if s.fps > 0:
            self.fps_spin.setValue(s.fps)
        for wdg, value in ((self.exp_auto, s.exposure_auto), (self.gain_auto, s.gain_auto),
                           (self.wb_auto, s.white_balance_auto), (self.embed_check, s.embed_audio)):  # fmt: skip
            wdg.blockSignals(True)
            wdg.setChecked(value)
            wdg.blockSignals(False)
        self.exp_spin.setValue(s.exposure_ms)
        self.gain_spin.setValue(s.gain_db)
        self.exp_spin.setEnabled(not s.exposure_auto)
        self.gain_spin.setEnabled(not s.gain_auto)
        self.dir_edit.setText(s.output_dir)
        self.name_edit.setText(s.session_name)
        i = self.quality_combo.findData(s.crf)
        if i < 0:
            self.quality_combo.addItem(f"Custom (CRF {s.crf})", s.crf)
            i = self.quality_combo.count() - 1
        self.quality_combo.setCurrentIndex(i)
        self.limit_spin.setValue(s.duration_limit_min)

    def _read_ui_into_settings(self) -> None:
        s = self.s
        s.width, s.height = (int(v) for v in self.res_combo.currentData().split("x"))
        s.color = self.color_check.isChecked()
        s.fps = 0.0 if self.fps_max_check.isChecked() else self.fps_spin.value()
        s.exposure_auto = self.exp_auto.isChecked()
        s.exposure_ms = self.exp_spin.value()
        s.gain_auto = self.gain_auto.isChecked()
        s.gain_db = self.gain_spin.value()
        s.white_balance_auto = self.wb_auto.isChecked()
        s.output_dir = self.dir_edit.text().strip() or s.output_dir
        s.session_name = self.name_edit.text().strip() or "session"
        s.crf = self.quality_combo.currentData()
        s.embed_audio = self.embed_check.isChecked()
        s.duration_limit_min = self.limit_spin.value()
        if self.audio_combo.currentData() is not None:
            s.audio_device = self.audio_combo.currentData()
        s.save()

    def _refresh_audio_devices(self) -> None:
        self.audio_combo.clear()
        self.audio_combo.addItem("(no audio)", "-")
        for d in list_inputs():
            self.audio_combo.addItem(d["name"], d["name"])
        chosen = pick_input(self.s.audio_device)
        want = "-" if self.s.audio_device == "-" else (chosen["name"] if chosen else "-")
        self.audio_combo.setCurrentIndex(max(0, self.audio_combo.findData(want)))

    # --------------------------------------------------------------- actions
    def run_async(self, fn, on_done) -> None:
        """Run ``fn`` off the GUI thread; ``on_done(result_or_exception)``
        is called back on the GUI thread."""
        self.busy = True
        self._update_enabled()

        def work():
            try:
                result = fn()
            except Exception as e:
                result = e
            self.bridge.done.emit(on_done, result)

        threading.Thread(target=work, daemon=True, name="worker").start()

    def _finish_async(self, result, error_title: str) -> bool:
        self.busy = False
        self._update_enabled()
        if isinstance(result, Exception):
            self._append_log(f"ERROR: {result}")
            QMessageBox.critical(self, error_title, str(result))
            return False
        return True

    def on_connect_clicked(self) -> None:
        if self.engine.state == "idle":
            self._read_ui_into_settings()
            self.placeholder.setText("Connecting ...\n\nSynchronising the camera clocks can take up to a minute.")
            self.run_async(self.engine.connect, self._connected)
        elif self.engine.state == "preview":
            self.run_async(self.engine.disconnect, self._disconnected)

    def _connected(self, result) -> None:
        if not self._finish_async(result, "Could not connect"):
            self.placeholder.setText("Not connected.")
            return
        self._build_views()
        self._refresh_after_stream_change()

    def _disconnected(self, result) -> None:
        self._finish_async(result, "Disconnect")
        self._build_views()
        self.device_label.setText("No cameras connected.")
        self.rate_label.setText("")

    def on_apply_clicked(self) -> None:
        self._read_ui_into_settings()
        self.run_async(self.engine.apply_settings, self._applied)

    def _applied(self, result) -> None:
        if self._finish_async(result, "Could not apply the settings"):
            self._refresh_after_stream_change()

    def _refresh_after_stream_change(self) -> None:
        e = self.engine
        for v in self.views:
            v.clear()
        if not e.cameras:
            return
        c = e.cameras[0]
        gb_h = c.width * c.height * e.fps * 3600 / 1e9
        self.rate_label.setText(
            f"Running {c.width} x {c.height} at <b>{e.fps:.2f} fps</b> "
            f"(the network link allows up to {e.max_fps:.2f} fps at this size)."
        )
        self.rate_label.setToolTip(f"Uncompressed this would be {gb_h:.0f} GB per hour and camera.")
        if e.sync_mode != "ptp" and len(e.cameras) > 1:
            QMessageBox.warning(
                self, "Cameras not hardware-synchronised",
                "The camera clocks could not be synchronised (PTP).\n\nRecording still works and frames are matched "
                "by time, but the two cameras do not expose at exactly the same instant (up to half a frame apart).\n\n"
                "Check that both cameras are on the same switch, then Disconnect and Connect again.",
            )  # fmt: skip

    def on_audio_changed(self) -> None:
        self._read_ui_into_settings()
        if self.engine.state == "preview":
            self.run_async(self.engine.open_audio, lambda r: self._finish_async(r, "Audio"))

    def on_exposure_changed(self) -> None:
        self.exp_spin.setEnabled(not self.exp_auto.isChecked())
        self.gain_spin.setEnabled(not self.gain_auto.isChecked())
        self._read_ui_into_settings()
        if self.engine.state in ("preview", "recording"):
            self.engine.apply_exposure()

    def on_browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder for recordings", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def on_record_clicked(self) -> None:
        e = self.engine
        if e.state == "preview":
            self._read_ui_into_settings()
            if e.audio is None and self.s.audio_device != "-":
                if QMessageBox.question(self, "No audio", "No audio input is open. Record video only?") != QMessageBox.Yes:
                    return
            try:
                e.start_recording()
            except Exception as ex:
                QMessageBox.critical(self, "Could not start recording", str(ex))
                return
        elif e.state == "recording":
            e.stop_recording()
        self._update_enabled()

    def _on_finished(self, summary: dict) -> None:
        self._update_enabled()
        if summary.get("ok"):
            filled = sum(c["frames_repeated_to_fill_gaps"] for c in summary["cameras"])
            self.rec_label.setText(
                f"Saved {summary['frames']} frames per camera ({summary['duration_s']:.1f} s)"
                + (f", <b>{filled} missing frame(s)</b> filled" if filled else ", no missing frames")
                + f".<br>{summary['folder']}"
            )
        else:
            self.rec_label.setText("<b>The recording finished with errors - see Messages.</b>")
        if self._close_when_done:
            self.close()

    # -------------------------------------------------------------- live view
    def _build_views(self) -> None:
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w is not self.placeholder:
                w.deleteLater()
            else:
                w.hide()
        self.views = []
        self.view_combo.blockSignals(True)
        self.view_combo.clear()
        self.view_combo.addItem("All cameras", -1)
        cams = self.engine.cameras
        if not cams:
            self.placeholder.setText("Not connected.")
            self.placeholder.show()
            self.grid.addWidget(self.placeholder, 0, 0)
        cols = 1 if len(cams) == 1 else 2
        for cam in cams:
            v = ImageView(cam.index)
            v.double_clicked.connect(lambda i: self.select_view(-1 if self.selected == i else i))
            self.views.append(v)
            self.grid.addWidget(v, cam.index // cols, cam.index % cols)
            self.view_combo.addItem(f"{cam.name}  ({cam.serial})", cam.index)
        self.view_combo.blockSignals(False)
        self.selected = -1
        self.select_view(-1)

    def select_view(self, index: int) -> None:
        """Show all cameras (-1) or enlarge one of them."""
        if index is None or index >= len(self.views):
            return
        self.selected = index
        for v, cam in zip(self.views, self.engine.cameras):
            v.setVisible(index < 0 or v.index == index)
            cam.preview_full = v.index == index
        i = self.view_combo.findData(index)
        if i >= 0 and i != self.view_combo.currentIndex():
            self.view_combo.blockSignals(True)
            self.view_combo.setCurrentIndex(i)
            self.view_combo.blockSignals(False)

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self.side.hide()
            self.view_bar.hide()
            self.showFullScreen()

    def exit_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.side.show()
            self.view_bar.show()

    def _update_frames(self) -> None:
        e = self.engine
        recording = e.state == "recording"
        for v, cam in zip(self.views, e.cameras):
            if not v.isVisible():
                continue
            rgb, seq = cam.preview()
            caption = f"{cam.name}   {cam.measured_fps():.1f} fps"
            if v.caption != caption or v.recording != recording:
                v.caption, v.recording = caption, recording
                v.update()
            v.set_frame(rgb, seq)
        audio = e.audio
        self.meter.active = audio is not None
        if audio is not None:
            self.meter.set_levels(*audio.levels())
        else:
            self.meter.update()

    # ----------------------------------------------------------------- status
    def _update_status(self) -> None:
        e = self.engine
        if e.state == "idle":
            return
        lines = []
        for i, cam in enumerate(e.cameras):
            parts = [f"<b>{cam.name}</b>: {cam.model}, serial {cam.serial}"]
            if i < len(e.ptp_state) and e.sync_mode == "ptp":
                role, off = e.ptp_state[i]
                parts.append(f"clock {role.lower()}" + (f" ({off / 1000:+.2f} µs)" if role == "Slave" else ""))
            if i < len(e.temps) and e.temps[i][0] is not None:
                temp, tstate = e.temps[i]
                warn = tstate not in ("", "Ok")
                parts.append(f"<span style='color:{REC_RED if warn else 'inherit'}'>{temp:.0f} °C{' ' + tstate if warn else ''}</span>")
            if cam.n_failed:
                parts.append(f"<span style='color:{REC_RED}'>{cam.n_failed} damaged image(s)</span>")
            lines.append(", ".join(parts))
        sync = "PTP - simultaneous exposure" if e.sync_mode == "ptp" else "free-run (not exposure-synchronised)"
        if len(e.cameras) > 1:
            lines.append(f"Synchronisation: <b>{sync}</b>")
        self.device_label.setText("<br>".join(lines))
        a = e.audio
        self.audio_label.setText(f"Audio: {a.rate / 1000:g} kHz, {a.channels} ch" if a else "Audio: none")

        if e.state == "recording":
            st = e.recording_status()
            t = int(st["elapsed_s"])
            text = f"<span style='color:{REC_RED}; font-size:16px'><b>● REC {t // 3600:02d}:{t // 60 % 60:02d}:{t % 60:02d}</b></span>"
            text += f" &nbsp; {st['frames']} frames"
            filled, drops = sum(st["filled"]), sum(st["queue_drops"])
            if filled or drops:
                text += f"<br><span style='color:{REC_RED}'>{filled} missing frame(s), {drops} dropped (PC too slow)</span>"
            for err in st["errors"]:
                text += f"<br><span style='color:{REC_RED}'>{err}</span>"
            text += f"<br>Free disk space: {e.free_disk_gb():.0f} GB"
            self.rec_label.setText(text)
            limit = self.s.duration_limit_min
            if limit > 0 and st["elapsed_s"] >= limit * 60:
                e.stop_recording()
                self._update_enabled()
            elif e.free_disk_gb() < 2.0:
                self._append_log("Disk almost full - stopping the recording")
                e.stop_recording()
                self._update_enabled()
        elif e.state == "finalizing":
            self.rec_label.setText(f"Finishing files: {e.progress} ... please wait")

    def _update_enabled(self) -> None:
        state = self.engine.state
        idle, preview, recording = state == "idle", state == "preview", state == "recording"
        free = not self.busy
        self.connect_btn.setEnabled(free and (idle or preview))
        self.connect_btn.setText("Connect" if idle else "Disconnect")
        for w in (self.res_combo, self.color_check, self.fps_max_check, self.apply_btn):
            w.setEnabled(free and preview)
        self.fps_spin.setEnabled(free and preview and not self.fps_max_check.isChecked())
        for w in (self.audio_combo, self.audio_refresh):
            w.setEnabled(free and (idle or preview))
        for w in (self.dir_edit, self.name_edit, self.quality_combo, self.embed_check, self.limit_spin):
            w.setEnabled(not recording and state != "finalizing")
        self.wb_once.setEnabled(preview or recording)
        self.record_btn.setEnabled(free and (preview or recording))
        self._style_record_button(recording)

    def _append_log(self, msg: str) -> None:
        self.log_view.appendPlainText(msg)

    def closeEvent(self, event) -> None:
        e = self.engine
        if e.state == "recording":
            if QMessageBox.question(self, APP_NAME, "A recording is running. Stop it, save the files and quit?") != QMessageBox.Yes:
                event.ignore()
                return
            self._close_when_done = True
            e.stop_recording()
            self._update_enabled()
            event.ignore()
            return
        if e.state == "finalizing" or self.busy:
            self._close_when_done = True
            event.ignore()
            return
        self._read_ui_into_settings()
        self.frame_timer.stop()
        self.status_timer.stop()
        e.disconnect()
        event.accept()


def run_gui(settings: Settings, demo: bool = False) -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    win = MainWindow(settings, demo)
    win.show()
    return app.exec()
