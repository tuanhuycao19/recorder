"""Cửa sổ chính kiểu Audacity: thanh công cụ phát/ghi, đồng hồ mức âm, nhiều track, menu hiệu ứng."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QDockWidget, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressBar,
                               QProgressDialog,
                               QPushButton, QScrollBar, QStyle, QToolBar, QVBoxLayout, QWidget)

from .. import __version__
from ..audio_io import IMPORT_FILTER, load_audio, resample, save_audio
from ..local_input import LocalRecorder, list_input_devices, list_output_devices
from ..phone_server import PhoneServer, local_ipv4_addresses
from .. import video as vid
from . import effects as fx
from .dialogs import NoiseReductionDialog, PreferencesDialog, ask_params
from .latency import click_track, measure_lag, overdub_position
from .model import PROJECT_EXT, Clip, Project
from .player import Player
from .widgets import CONTROL_W, Meter, Ruler, TracksPanel, VideoPreview, fmt_time, parse_time

APP_NAME = "WiFi Recorder"
RATES = [8000, 16000, 22050, 32000, 44100, 48000, 88200, 96000]


# ---------------------------------------------------------------- cấu hình
def config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "WifiRecorder" / "settings.json"


def default_output_dir() -> Path:
    music = Path.home() / "Music"
    return (music if music.exists() else Path.home()) / "WifiRecorder"


def load_cfg() -> dict:
    cfg = {"output_dir": str(default_output_dir()), "sample_rate": 48000, "channels": 1, "bitrate": 320,
           "autosave": True, "output_device": None, "source": "", "overdub": True,
           "latency": {}, "extra_latency_ms": 0.0}
    try:
        cfg.update(json.loads(config_path().read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def open_path(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def record_icon(size: int = 32) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(210, 30, 30))
    p.setPen(QColor(140, 0, 0))
    m = size * 0.18
    p.drawEllipse(int(m), int(m), int(size - 2 * m), int(size - 2 * m))
    p.end()
    return QIcon(pm)


def safe_name(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:40].strip("_") or "rec"


class EditorWindow(QMainWindow):
    def __init__(self, open_file: str | None = None):
        super().__init__()
        self.cfg = load_cfg()
        self.project = Project(int(self.cfg["sample_rate"]))
        self.project.listeners.append(self._on_project_changed)
        self.player = Player(self.project)
        self.player.device = self.cfg.get("output_device")
        self.spp = 512.0          # số mẫu trên mỗi pixel
        self.h0 = 0.0             # mẫu ở mép trái vùng sóng âm
        self.events: queue.Queue = queue.Queue()
        self.rec: dict | None = None
        self.pending_phone: dict[str, dict] = {}
        self._timings: dict[str, tuple[int | None, bool]] = {}   # session → (t0 mẫu đầu theo đồng hồ PC, ước lượng?)
        self.noise_profile = None
        self.noise_params: dict = {}
        self.last_effect = None
        self._hbar_guard = False
        self._last_play_pos = None
        # video
        self.video_thumbs: list[tuple[float, QImage]] = []
        self._grabber = None
        self._streamer = None
        self._streaming = False
        self._stream_last = -1
        self._thumb_cancel = None
        self._export_dlg = None

        self.server = PhoneServer(on_event=lambda n, **kw: self.events.put((n, kw)))
        try:
            self.server.start()
            self.server_ok = True
        except OSError:
            self.server_ok = False

        self._build_ui()
        self.refresh_sources()
        self.refresh_recordings()
        self._on_project_changed("tracks")
        self.resize(1280, 760)
        self.setAcceptDrops(True)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(30)
        if open_file:
            QTimer.singleShot(0, lambda: self.open_any(Path(open_file)))

    # ================================================================== giao diện
    def _build_ui(self):
        st = self.style()
        self.setWindowIcon(record_icon(64))
        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.ruler = Ruler(self)
        self.panel = TracksPanel(self)
        self.panel.setFrameShape(QFrame.NoFrame)
        hrow = QHBoxLayout()
        hrow.setContentsMargins(CONTROL_W, 0, 0, 0)
        self.hbar = QScrollBar(Qt.Horizontal)
        self.hbar.valueChanged.connect(self._on_hbar)
        hrow.addWidget(self.hbar)
        v.addWidget(self.ruler)
        v.addWidget(self.panel, 1)
        v.addLayout(hrow)
        self.setCentralWidget(central)

        A = self._action
        # ---------- hành động phát / ghi
        self.a_start = A("Về đầu", self.go_start, "Home", st.standardIcon(QStyle.SP_MediaSkipBackward))
        self.a_play = A("Phát", self.play_pressed, "Space", st.standardIcon(QStyle.SP_MediaPlay))
        self.a_loop_play = A("Phát lặp", lambda: self.play(loop=True), "Shift+Space")
        self.a_pause = A("Tạm dừng", self.pause, "P", st.standardIcon(QStyle.SP_MediaPause), checkable=True)
        self.a_stop = A("Dừng", self.stop_all, None, st.standardIcon(QStyle.SP_MediaStop))
        self.a_end = A("Về cuối", self.go_end, "End", st.standardIcon(QStyle.SP_MediaSkipForward))
        self.a_record = A("Ghi âm", self.record_pressed, "R", record_icon())
        self.a_loop = A("Lặp", None, None, None, checkable=True)
        self.a_overdub = A("Phát các track khác khi ghi (Overdub)", None, None, None, checkable=True)
        self.a_overdub.setIconText("🎧 Phát kèm khi ghi")
        self.a_overdub.setToolTip("Bật: khi ghi âm, các track đã có vẫn phát (thu nhạc, hát theo beat, lồng tiếng).\n"
                                  "Nên đeo tai nghe để micro không thu lại tiếng loa.")
        self.a_overdub.setChecked(bool(self.cfg.get("overdub", True)))
        self.a_overdub.toggled.connect(lambda v: self.cfg.__setitem__("overdub", v))
        self.a_calib = A("Hiệu chỉnh độ trễ ghi âm...", self.calibrate_latency)
        self.a_loop.setToolTip("Phát lặp lại vùng chọn")

        # ---------- chỉnh sửa
        def P_():
            return self.project
        self.a_undo = A("Hoàn tác", self.undo, QKeySequence.Undo)
        self.a_redo = A("Làm lại", self.redo, "Ctrl+Y")
        self.a_cut = A("Cắt", lambda: self._edit(P_().cut, "Cắt"), QKeySequence.Cut)
        self.a_copy = A("Sao chép", lambda: self._edit(P_().copy, "Đã sao chép"), QKeySequence.Copy)
        self.a_paste = A("Dán", lambda: self._edit(P_().paste, "Dán"), QKeySequence.Paste)
        self.a_delete = A("Xoá", lambda: self._edit(P_().delete, "Xoá"), QKeySequence.Delete)
        self.a_dup = A("Nhân bản thành track mới", lambda: self._edit(P_().duplicate, "Nhân bản"), "Ctrl+D")
        self.a_trim = A("Giữ lại vùng chọn (Trim)", lambda: self._edit(P_().trim, "Trim"), "Ctrl+T")
        self.a_silence = A("Làm im lặng vùng chọn", lambda: self._edit(P_().silence, "Im lặng"), "Ctrl+L")
        self.a_split = A("Tách (Split)", lambda: self._edit(P_().split, "Tách"), "Ctrl+I")
        self.a_split_del = A("Tách và xoá (để lại khoảng trống)", lambda: self._edit(P_().split_delete, "Tách và xoá"),
                             "Ctrl+Alt+K")
        self.a_split_new = A("Tách ra track mới", lambda: self._edit(P_().split_new, "Tách ra track mới"), "Ctrl+Alt+I")
        self.a_join = A("Gộp (Join)", lambda: self._edit(P_().join, "Gộp"), "Ctrl+J")
        self.a_silences = A("Tách tại khoảng lặng...", self.split_at_silences)
        self.a_selall = A("Chọn tất cả", lambda: self.project.select_all(), QKeySequence.SelectAll)
        self.a_selnone = A("Bỏ chọn", lambda: self.project.select_none(), "Ctrl+Shift+A")
        # ---------- xem
        self.a_zin = A("Phóng to", lambda: self.zoom(2.0), "Ctrl+1")
        self.a_zout = A("Thu nhỏ", lambda: self.zoom(0.5), "Ctrl+3")
        self.a_zsel = A("Vừa vùng chọn", self.zoom_selection, "Ctrl+E")
        self.a_zfit = A("Vừa cửa sổ", self.zoom_fit, "Ctrl+F")

        # ---------- thanh công cụ phát/ghi
        tb = QToolBar("Phát / Ghi")
        tb.setObjectName("transport")
        tb.setIconSize(QSize(30, 30))
        for a in (self.a_pause, self.a_play, self.a_stop, self.a_start, self.a_end, self.a_record):
            tb.addAction(a)
        tb.addSeparator()
        tb.addAction(self.a_loop)
        tb.addAction(self.a_overdub)
        self.addToolBar(tb)

        mt = QToolBar("Đồng hồ mức âm")
        mt.setObjectName("meters")
        self.rec_meter = Meter("Ghi", "Mức âm thu vào (bấm để xoá báo vỡ tiếng)")
        self.play_meter = Meter("Phát", "Mức âm phát ra")
        mt.addWidget(self.rec_meter)
        mt.addWidget(self.play_meter)
        self.addToolBar(mt)

        et = QToolBar("Chỉnh sửa")
        et.setObjectName("edit")
        et.setToolButtonStyle(Qt.ToolButtonTextOnly)
        for a, txt in ((self.a_cut, "✂ Cắt"), (self.a_copy, "⧉ Chép"), (self.a_paste, "📋 Dán"),
                       (self.a_trim, "⇤⇥ Trim"), (self.a_silence, "🔇 Im lặng"), (self.a_split, "⫶ Tách"),
                       (self.a_join, "⇔ Gộp"), (self.a_undo, "↶"),
                       (self.a_redo, "↷"), (self.a_zin, "🔍+"), (self.a_zout, "🔍−"), (self.a_zsel, "⤡ Vùng chọn"),
                       (self.a_zfit, "⤢ Vừa")):
            btn_action = QAction(txt, self)
            btn_action.setToolTip(f"{a.text()} ({a.shortcut().toString()})" if a.shortcut().toString() else a.text())
            btn_action.triggered.connect(a.trigger)
            a.changed.connect(lambda a=a, b=btn_action: b.setEnabled(a.isEnabled()))
            et.addAction(btn_action)
        self.addToolBarBreak()
        self.addToolBar(et)

        dt = QToolBar("Thiết bị")
        dt.setObjectName("device")
        dt.addWidget(QLabel(" 🎤 Nguồn thu: "))
        self.source_combo = QComboBox()
        self.source_combo.setMinimumWidth(300)
        dt.addWidget(self.source_combo)
        self.rec_channels = QComboBox()
        self.rec_channels.addItems(["Mono", "Stereo"])
        self.rec_channels.setCurrentIndex(1 if self.cfg["channels"] == 2 else 0)
        dt.addWidget(self.rec_channels)
        refresh = QAction("⟳", self)
        refresh.setToolTip("Làm mới danh sách thiết bị")
        refresh.triggered.connect(self.refresh_sources)
        dt.addAction(refresh)
        self.addToolBar(dt)

        # ---------- thanh vùng chọn (dưới)
        sb = QToolBar("Vùng chọn")
        sb.setObjectName("selection")
        sb.addWidget(QLabel(" Tần số dự án (Hz): "))
        self.rate_combo = QComboBox()
        self.rate_combo.addItems([str(r) for r in RATES])
        self.rate_combo.setCurrentText(str(self.project.rate))
        self.rate_combo.currentTextChanged.connect(self._rate_changed)
        sb.addWidget(self.rate_combo)
        sb.addSeparator()
        sb.addWidget(QLabel(" Bắt đầu: "))
        self.sel_start_edit = QLineEdit()
        self.sel_end_edit = QLineEdit()
        for e in (self.sel_start_edit, self.sel_end_edit):
            e.setFixedWidth(110)
            e.setToolTip("hh:mm:ss.mmm — sửa rồi Enter")
            e.editingFinished.connect(self._sel_edited)
        sb.addWidget(self.sel_start_edit)
        sb.addWidget(QLabel(" Kết thúc: "))
        sb.addWidget(self.sel_end_edit)
        self.sel_len_lbl = QLabel()
        sb.addWidget(self.sel_len_lbl)
        sb.addSeparator()
        sb.addWidget(QLabel(" Vị trí: "))
        self.pos_lbl = QLabel("00:00:00.000")
        self.pos_lbl.setStyleSheet("font-family: Consolas, monospace; font-size: 13pt; font-weight: bold;")
        sb.addWidget(self.pos_lbl)
        self.addToolBar(Qt.BottomToolBarArea, sb)

        # ---------- dock khung phát video
        vdock = QDockWidget("Video", self)
        vdock.setObjectName("video")
        self.video_view = VideoPreview()
        vdock.setWidget(self.video_view)
        self.addDockWidget(Qt.RightDockWidgetArea, vdock)
        vdock.hide()
        self.video_dock = vdock

        # ---------- dock danh sách bản ghi
        dock = QDockWidget("Bản ghi đã lưu", self)
        dock.setObjectName("recordings")
        w = QWidget()
        dl = QVBoxLayout(w)
        dl.setContentsMargins(4, 4, 4, 4)
        self.rec_list = QListWidget()
        self.rec_list.itemDoubleClicked.connect(lambda it: self.import_files([Path(it.data(Qt.UserRole))]))
        dl.addWidget(self.rec_list)
        row = QHBoxLayout()
        for txt, fn in (("Mở vào dự án", self._import_selected_recording), ("Thư mục", self._open_output_dir),
                        ("Xoá", self._delete_recording), ("⟳", self.refresh_recordings)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            row.addWidget(b)
        dl.addLayout(row)
        dock.setWidget(w)
        dock.setMinimumWidth(240)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        QTimer.singleShot(0, lambda: self.resizeDocks([dock], [260], Qt.Horizontal))
        self.rec_dock = dock

        # ---------- thanh trạng thái
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(180)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)
        ips = ", ".join(local_ipv4_addresses()) or "?"
        phone_info = (f"📱 Điện thoại kết nối tới: {ips}  cổng {self.server.port}" if self.server_ok
                      else "📱 Không mở được cổng cho điện thoại")
        self.statusBar().addPermanentWidget(QLabel(phone_info))

        self._build_menus()

    def _action(self, text, slot, shortcut=None, icon=None, checkable=False) -> QAction:
        a = QAction(text, self)
        if icon is not None:
            a.setIcon(icon)
        if shortcut is not None:
            a.setShortcut(QKeySequence(shortcut))
            a.setToolTip(f"{text} ({QKeySequence(shortcut).toString()})")
        a.setCheckable(checkable)
        if slot is not None:
            a.triggered.connect(lambda checked=False: slot())
        self.addAction(a)
        return a

    def _build_menus(self):
        mb = self.menuBar()
        A = self._action
        m = mb.addMenu("&Tệp")
        m.addAction(A("Dự án mới", self.new_project, QKeySequence.New))
        m.addAction(A("Mở...", self.open_dialog, QKeySequence.Open))
        m.addAction(A("Nhập âm thanh (thêm track)...", self.import_dialog, "Ctrl+Shift+I"))
        m.addAction(A("Nhập video...", self.import_video_dialog, "Ctrl+Shift+V"))
        m.addSeparator()
        m.addAction(A("Lưu dự án", self.save_project, QKeySequence.Save))
        m.addAction(A("Lưu dự án thành...", lambda: self.save_project(ask=True), "Ctrl+Shift+S"))
        m.addSeparator()
        m.addAction(A("Xuất MP3...", lambda: self.export_mix(".mp3"), "Ctrl+Shift+E"))
        m.addAction(A("Xuất WAV...", lambda: self.export_mix(".wav")))
        m.addAction(A("Xuất FLAC...", lambda: self.export_mix(".flac")))
        m.addAction(A("Xuất vùng chọn...", lambda: self.export_mix(".mp3", selection=True)))
        self.a_export_video = A("Xuất video...", self.export_video_dialog, "Ctrl+Alt+E")
        m.addAction(self.a_export_video)
        m.addSeparator()
        m.addAction(A("Tuỳ chọn...", self.preferences, "Ctrl+P"))
        m.addSeparator()
        m.addAction(A("Thoát", self.close, "Ctrl+Q"))

        m = mb.addMenu("&Sửa")
        for a in (self.a_undo, self.a_redo, None, self.a_cut, self.a_copy, self.a_paste, self.a_delete,
                  self.a_dup, None, self.a_trim, self.a_silence, None, self.a_split, self.a_split_del,
                  self.a_split_new, self.a_join, self.a_silences, None, self.a_selall, self.a_selnone):
            m.addSeparator() if a is None else m.addAction(a)

        m = mb.addMenu("&Xem")
        for a in (self.a_zin, self.a_zout, self.a_zsel, self.a_zfit):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.video_dock.toggleViewAction())
        m.addAction(self.rec_dock.toggleViewAction())

        m = mb.addMenu("&Phát/Ghi")
        for a in (self.a_play, self.a_loop_play, self.a_pause, self.a_stop, None, self.a_record,
                  self.a_overdub, self.a_calib, None, self.a_start, self.a_end):
            m.addSeparator() if a is None else m.addAction(a)

        m = mb.addMenu("T&rack")
        m.addAction(A("Track mono mới", lambda: self._new_track(1)))
        m.addAction(A("Track stereo mới", lambda: self._new_track(2)))
        m.addAction(A("Xoá các track đang chọn", self.remove_selected_tracks))
        m.addSeparator()
        m.addAction(A("Trộn thành track mới", lambda: self._edit(self._mix_new, "Trộn")))
        m.addAction(A("Stereo → Mono", lambda: self._edit(self._to_mono, "Stereo → Mono")))

        m = mb.addMenu("&Tạo")
        m.addAction(A("Im lặng...", self.gen_silence))
        m.addAction(A("Âm (tone)...", self.gen_tone))
        m.addAction(A("Tiếng ồn trắng...", self.gen_noise))

        m = mb.addMenu("&Hiệu ứng")
        self.a_repeat = A("Lặp lại hiệu ứng cuối", self.repeat_effect, "Ctrl+R")
        m.addAction(self.a_repeat)
        m.addSeparator()
        m.addAction(A("Khuếch đại (Amplify)...", self.fx_amplify))
        m.addAction(A("Chuẩn hoá (Normalize)...", self.fx_normalize))
        m.addAction(A("Fade In", lambda: self.run_effect("Fade In", fx.fade_in)))
        m.addAction(A("Fade Out", lambda: self.run_effect("Fade Out", fx.fade_out)))
        m.addSeparator()
        m.addAction(A("Giảm nhiễu (Noise Reduction)...", self.fx_noise))
        m.addAction(A("Nén dải động (Compressor)...", self.fx_compressor))
        m.addAction(A("Tiếng vang (Echo)...", self.fx_echo))
        m.addAction(A("Đổi tốc độ...", self.fx_speed))
        m.addSeparator()
        m.addAction(A("Đảo ngược (Reverse)", lambda: self.run_effect("Đảo ngược", fx.reverse)))
        m.addAction(A("Đảo pha (Invert)", lambda: self.run_effect("Đảo pha", fx.invert)))

        m = mb.addMenu("Trợ &giúp")
        m.addAction(A("Phím tắt", self.show_shortcuts, "F1"))
        m.addAction(A("Giới thiệu", lambda: QMessageBox.about(
            self, APP_NAME, f"<b>{APP_NAME} {__version__}</b><br>Ghi âm từ micro hoặc điện thoại qua WiFi "
                            "và chỉnh sửa nhiều track.")))

        self.edit_actions = [self.a_undo, self.a_redo, self.a_cut, self.a_copy, self.a_paste, self.a_delete,
                             self.a_dup, self.a_trim, self.a_silence, self.a_repeat, self.a_split,
                             self.a_split_del, self.a_split_new, self.a_join, self.a_silences]

    # ================================================================== trạng thái chung
    @property
    def is_recording(self) -> bool:
        return self.rec is not None

    @property
    def playhead(self) -> int | None:
        if self.player.playing:
            return self.player.position
        return None

    def status(self, msg: str, ms: int = 6000):
        self.statusBar().showMessage(msg, ms)

    def _on_project_changed(self, what: str):
        if what == "tracks":
            self.panel.sync()
        elif what == "selection":
            self.panel.refresh()
        else:
            self.panel.refresh()
        self.ruler.update()
        self._update_selection_bar()
        self._update_hbar()
        self._update_actions()

    def _update_actions(self):
        rec = self.is_recording
        p = self.project
        for a in self.edit_actions:
            a.setEnabled(not rec)
        if not rec:
            self.a_undo.setEnabled(bool(p.undo_stack))
            self.a_redo.setEnabled(bool(p.redo_stack))
            self.a_paste.setEnabled(bool(p.clipboard))
            self.a_repeat.setEnabled(self.last_effect is not None)
        self.rate_combo.setEnabled(not p.tracks and not rec)
        self.a_export_video.setEnabled(p.video is not None and not rec)
        self.source_combo.setEnabled(not rec)
        self.rec_channels.setEnabled(not rec)
        name = p.path.stem if p.path else "Chưa đặt tên"
        self.setWindowTitle(f"{'*' if p.modified else ''}{name} — {APP_NAME}")

    def _update_selection_bar(self):
        p = self.project
        r = p.rate
        if not self.sel_start_edit.hasFocus():
            self.sel_start_edit.setText(fmt_time(p.sel_start / r))
        if not self.sel_end_edit.hasFocus():
            self.sel_end_edit.setText(fmt_time(p.sel_end / r))
        self.sel_len_lbl.setText(f"  Độ dài: {fmt_time((p.sel_end - p.sel_start) / r)} ")

    def _sel_edited(self):
        try:
            a = parse_time(self.sel_start_edit.text())
            b = parse_time(self.sel_end_edit.text())
        except ValueError:
            self._update_selection_bar()
            return
        self.project.set_selection(a * self.project.rate, b * self.project.rate)

    def _rate_changed(self, txt: str):
        if not self.project.tracks:
            self.project.rate = int(txt)
            self.cfg["sample_rate"] = int(txt)
            self.ruler.update()

    def on_cursor_moved(self):
        if not self.player.playing:
            self.pos_lbl.setText(fmt_time(self.project.sel_start / self.project.rate))
            if self._grabber is not None and not self.is_recording:
                self._grabber.request(self.project.sel_start / self.project.rate)

    def select_tracks_between(self, anchor_track, global_y: int):
        cur = self.panel.track_at_global_y(global_y)
        if cur is None:
            return
        tr = self.project.tracks
        if anchor_track not in tr:
            return
        i, j = sorted((tr.index(anchor_track), tr.index(cur)))
        changed = False
        for k, t in enumerate(tr):
            want = i <= k <= j
            if t.selected != want:
                t.selected = want
                changed = True
        if changed:
            self.project.changed("selection")

    # ================================================================== phóng to / cuộn
    def on_view_resized(self):
        self._update_hbar()
        self.ruler.update()

    def zoom(self, factor: float, anchor_x: float | None = None):
        w = self.panel.wave_width()
        if anchor_x is None:
            ph = self.playhead if self.playhead is not None else self.project.sel_start
            ax = (ph - self.h0) / self.spp
            anchor_x = ax if 0 <= ax <= w else w / 2
        anchor_sample = self.h0 + anchor_x * self.spp
        max_spp = max(self.project.length, self.project.rate * 60) / w * 1.5
        self.spp = float(min(max(self.spp / factor, 1 / 64), max_spp))
        self.h0 = max(0.0, anchor_sample - anchor_x * self.spp)
        self._view_changed()

    def zoom_fit(self):
        w = self.panel.wave_width()
        n = self.project.length or self.project.rate * 10
        self.spp = max(1 / 64, n / (w * 0.98))
        self.h0 = 0.0
        self._view_changed()

    def zoom_selection(self):
        p = self.project
        if not p.has_selection:
            return
        w = self.panel.wave_width()
        span = p.sel_end - p.sel_start
        self.spp = max(1 / 64, span / (w * 0.9))
        self.h0 = max(0.0, p.sel_start - span * 0.05)
        self._view_changed()

    def scroll_by_pixels(self, dx: float):
        self.h0 = max(0.0, self.h0 + dx * self.spp)
        self._view_changed()

    def _view_changed(self):
        self._update_hbar()
        self.ruler.update()
        self.panel.repaint_waves()

    def _update_hbar(self):
        w = self.panel.wave_width()
        total = max(self.project.length * 1.05, self.h0 + w * self.spp)
        self._hbar_guard = True
        self.hbar.setRange(0, max(0, int(total / self.spp) - w))
        self.hbar.setPageStep(w)
        self.hbar.setSingleStep(max(1, w // 20))
        self.hbar.setValue(int(self.h0 / self.spp))
        self._hbar_guard = False

    def _on_hbar(self, v: int):
        if self._hbar_guard:
            return
        self.h0 = v * self.spp
        self.ruler.update()
        self.panel.repaint_waves()

    def ensure_visible(self, sample: int):
        w = self.panel.wave_width()
        x = (sample - self.h0) / self.spp
        if x > w or x < 0:
            self.h0 = max(0.0, sample - w * self.spp * 0.1)
            self._view_changed()

    # ================================================================== phát
    def play_pressed(self):
        if self.player.playing:
            self.stop_all()
        else:
            self.play(loop=self.a_loop.isChecked())

    def play(self, loop: bool = False):
        if self.is_recording:
            return
        p = self.project
        if p.has_selection:
            ok = self.player.play(p.sel_start, p.sel_end, loop=loop)
        else:
            ok = self.player.play(p.sel_start)
        if not ok and self.player.error:
            QMessageBox.warning(self, APP_NAME, f"Không phát được âm thanh:\n{self.player.error}")
            self.player.error = None

    def quick_play(self, sample: int):
        if self.is_recording:
            return
        self.player.play(sample, loop=False)

    def pause(self):
        if self.player.playing:
            self.player.pause()
        self.a_pause.setChecked(self.player.paused)

    def stop_all(self):
        if self.is_recording:
            self.stop_record()
        self.player.stop()
        self.a_pause.setChecked(False)
        self.play_meter.reset()
        self.panel.repaint_waves()
        self.ruler.update()

    def go_start(self):
        self.project.set_selection(0, 0)
        self.h0 = 0
        self._view_changed()
        self.on_cursor_moved()

    def go_end(self):
        n = self.project.length
        self.project.set_selection(n, n)
        self.ensure_visible(n)
        self.on_cursor_moved()

    # ================================================================== nguồn thu
    def refresh_sources(self):
        current = self.source_combo.currentText() or self.cfg.get("source", "")
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for conn in list(self.server.connections.values()):
            if conn.ready and conn.alive:
                self.source_combo.addItem(conn.label, ("phone", conn.id))
        for idx, name in list_input_devices():
            self.source_combo.addItem(name, ("local", idx))
        if self.source_combo.count() == 0:
            self.source_combo.addItem("(chưa có nguồn — kết nối điện thoại hoặc cắm micro)", None)
        i = self.source_combo.findText(current)
        self.source_combo.setCurrentIndex(max(0, i))
        self.source_combo.blockSignals(False)

    # ================================================================== ghi âm
    def record_pressed(self):
        if self.is_recording:
            self.stop_record()
        else:
            self.start_record()

    def _new_record_track(self, label: str, channels: int):
        p = self.project
        prefix = p.sel_start
        t = p.add_track(f"{label} {time.strftime('%H:%M:%S')}", np.zeros((0, channels), np.float32))
        clip = Clip(prefix, np.zeros((0, channels), np.float32), name=t.name)
        clip.begin_recording()
        t.clips.append(clip)
        p.set_selection(prefix, prefix)
        return t, prefix

    def start_record(self, phone_initiated: dict | None = None, calibrate: dict | None = None):
        if self.is_recording:
            return
        self.player.stop()
        p = self.project
        ch = 2 if self.rec_channels.currentIndex() == 1 else 1
        session = time.strftime("%Y%m%d_%H%M%S")
        if phone_initiated:
            conn = phone_initiated["conn"]
            t, prefix = self._new_record_track(conn.name, phone_initiated["channels"])
            self.rec = {"kind": "phone", "conn_id": conn.id, "session": phone_initiated["session"], "track": t,
                        "prefix": prefix, "rate_in": phone_initiated["sample_rate"],
                        "ch_in": phone_initiated["channels"], "label": conn.name, "src_key": "phone:" + conn.name}
        else:
            src = self.source_combo.currentData()
            if not src:
                QMessageBox.information(self, APP_NAME, "Chưa có nguồn thu. Hãy kết nối điện thoại hoặc cắm micro.")
                return
            kind, ref = src
            self.cfg["source"] = self.source_combo.currentText()
            try:
                if kind == "local":
                    t, prefix = self._new_record_track("Micro", ch)
                    wav = self.server.temp_dir / f"{session}_local.wav"
                    rec = LocalRecorder(ref, p.rate, ch, wav,
                                        on_level=lambda v: self.events.put(("local_level", {"level": v})),
                                        on_chunk=lambda b: self.events.put(("local_chunk", {"pcm": b})))
                    self.rec = {"kind": "local", "recorder": rec, "session": session, "track": t, "prefix": prefix,
                                "label": "May_tinh", "src_key": "local:" + self.source_combo.currentText()}
                    rec.start()
                    self.rec["rate_in"], self.rec["ch_in"] = rec.sample_rate, rec.channels
                else:
                    conn = self.server.connections.get(ref)
                    if not conn:
                        raise RuntimeError("Điện thoại đã ngắt kết nối")
                    t, prefix = self._new_record_track(conn.name, ch)
                    self.rec = {"kind": "phone", "conn_id": conn.id, "session": session, "track": t,
                                "prefix": prefix, "rate_in": p.rate, "ch_in": ch, "label": conn.name,
                                "src_key": "phone:" + conn.name}
                    conn.start_recording(session, p.rate, ch)
            except Exception as e:
                self.rec = None
                p.undo()
                p.redo_stack.clear()
                QMessageBox.critical(self, APP_NAME, f"Không bắt đầu ghi được:\n{e}")
                return
        self.rec["calibrate"] = calibrate
        overdub = self._start_overdub(self.rec["prefix"], force=calibrate is not None)
        self.rec_meter.reset()
        self.a_record.setText("Dừng ghi")
        self.status(f"Đang ghi từ {self.rec['label']}" + (" — đang phát kèm các track khác" if overdub else "") + "...",
                    0)
        self._update_actions()

    def _start_overdub(self, prefix: int, force: bool = False) -> bool:
        """Phát các track đã có từ vị trí ghi (overdub)."""
        r = self.rec
        if not (force or self.a_overdub.isChecked()):
            return False
        if not any(t.length > prefix for t in self.project.tracks if not t.recording):
            return False
        ok = self.player.play(prefix)
        r["overdub"] = ok
        if not ok and self.player.error:
            self.status(f"Không phát kèm được: {self.player.error}")
            self.player.error = None
        return ok

    def _latency_ms(self, src_key: str) -> float:
        return float(self.cfg.get("latency", {}).get(src_key, 0.0)) + float(self.cfg.get("extra_latency_ms", 0.0))

    def _overdub_place(self, r: dict) -> int | None:
        """Vị trí đúng của bản ghi trên dòng thời gian (bù độ trễ), hoặc None nếu không ghi kèm phát."""
        if not r.get("overdub"):
            return None
        if "place" in r:
            return r["place"]
        dac = r.get("dac_ref")
        if r["kind"] == "local":
            t0 = r["recorder"].t0_ns
        else:
            t0 = self._timings.get(r["session"], (None, False))[0]
        if dac is None or t0 is None:
            return None   # chưa đủ thông tin (thử lại khi nhận file)
        pos = overdub_position(dac[0], dac[1], t0, self.project.rate, self._latency_ms(r["src_key"]))
        if abs(pos - r["prefix"]) > self.project.rate * 5:   # lệch vô lý → đồng hồ chưa đồng bộ
            self.status("⚠ Không đo được độ trễ chính xác, đặt bản ghi tại vị trí bắt đầu ghi")
            pos = r["prefix"]
        r["place"] = pos
        return pos

    def _place_clip(self, t, clip, r: dict) -> None:
        pos = self._overdub_place(r)
        if pos is None or clip not in t.clips:
            return
        if pos < 0:
            clip.set_data(clip.data[min(len(clip.data), -pos):])
            pos = 0
        clip.offset = pos

    def stop_record(self):
        r, self.rec = self.rec, None
        if not r:
            return
        t, clip = r["track"], self._rec_clip(r)
        if r.get("overdub"):
            r["dac_ref"] = self.player.dac_ref
            self.player.stop()
            self.play_meter.reset()
        if r["kind"] == "local":
            wav = r["recorder"].stop()
            clip.end_recording()
            self._apply_final(t, clip, wav, r["session"], "May_tinh", replace=True, r=r)
        else:
            conn = self.server.connections.get(r["conn_id"])
            if conn:
                try:
                    conn.stop_recording()
                except OSError:
                    pass
            self._finish_live(r)
        self.a_record.setText("Ghi âm")
        self.rec_meter.reset()
        self._update_actions()
        self.project.changed("data")

    @staticmethod
    def _rec_clip(r: dict) -> Clip:
        return r.setdefault("clip", r["track"].clips[0])

    def _finish_live(self, r: dict):
        t, clip = r["track"], self._rec_clip(r)
        if clip.recording:
            clip.end_recording()
        if r.get("overdub") and "dac_ref" not in r:
            r["dac_ref"] = self.player.dac_ref
            self.player.stop()
        self._place_clip(t, clip, r)
        t.note = "⏳ Đang nhận file gốc từ điện thoại..."
        self.pending_phone[r["session"]] = {"track": t, "clip": clip, "live": clip.data, "label": r["label"], "r": r}
        self.status("Đã dừng — đang nhận file gốc (chất lượng đầy đủ) từ điện thoại...", 0)

    def _append_pcm(self, pcm: bytes):
        r = self.rec
        if not r or not self._rec_clip(r).recording:
            return
        ch = r.get("ch_in", 1)
        n = len(pcm) // (2 * ch)
        if n <= 0:
            return
        x = np.frombuffer(pcm[: n * 2 * ch], dtype="<i2").astype(np.float32).reshape(n, ch) / 32768.0
        rate_in = r.get("rate_in", self.project.rate)
        if rate_in != self.project.rate:
            x = resample(x, rate_in, self.project.rate)
        self._rec_clip(r).append(x)

    def _apply_final(self, t, clip, wav: Path, session: str, label: str, replace: bool, r: dict | None = None):
        """Thay dữ liệu clip vừa ghi bằng file gốc (WAV không nén) và tự lưu MP3."""
        try:
            data, sr = load_audio(wav)
        except Exception as e:
            self.status(f"Không đọc được file ghi âm: {e}")
            return
        data = resample(data, sr, self.project.rate)
        try:
            wav.unlink()
        except OSError:
            pass
        if t is not None and t in self.project.tracks and replace and clip in t.clips:
            if data.shape[1] != t.channels:
                t.channels = data.shape[1]
            clip.set_data(data)
            if r is not None:
                self._place_clip(t, clip, r)
        elif t is None:
            self.project.add_track(f"{label} {session}", data)
        if t is not None:
            t.note = ""
        self.project.modified = True
        self.project.changed("data")
        self.status(f"Đã ghi xong {fmt_time(len(data) / self.project.rate)}")
        if r is not None and r.get("calibrate"):
            self._finish_calibration(t, r)
            return
        if self.cfg.get("autosave", True):
            out = Path(self.cfg["output_dir"]) / f"{session}_{safe_name(label)}.mp3"
            self._save_async(data, self.project.rate, out)

    def _save_async(self, data: np.ndarray, rate: int, out: Path):
        bitrate = int(self.cfg["bitrate"])

        def work():
            try:
                save_audio(out, data, rate, bitrate)
                self.events.put(("saved", {"path": out}))
            except Exception as e:
                self.events.put(("save_failed", {"error": str(e)}))
        threading.Thread(target=work, daemon=True).start()

    # ================================================================== hiệu chỉnh độ trễ
    def calibrate_latency(self):
        if self.is_recording or not self._can_edit():
            return
        src = self.source_combo.currentData()
        if not src:
            QMessageBox.information(self, APP_NAME, "Hãy chọn nguồn thu (micro hoặc điện thoại) trước.")
            return
        if QMessageBox.question(
                self, "Hiệu chỉnh độ trễ",
                f"Nguồn thu: {self.source_combo.currentText()}\n\n"
                "App sẽ phát khoảng 7 giây tiếng \"tách\" qua loa và ghi lại bằng nguồn thu này để đo độ trễ.\n\n"
                "• Tạm thời KHÔNG dùng tai nghe — để micro/điện thoại gần loa (30–50 cm)\n"
                "• Bật loa vừa đủ nghe, giữ yên lặng\n\nBắt đầu?",
                QMessageBox.Ok | QMessageBox.Cancel) != QMessageBox.Ok:
            return
        p = self.project
        info = {"snap": p.snapshot("Hiệu chỉnh"), "undo_len": len(p.undo_stack), "redo": list(p.redo_stack),
                "modified": p.modified}
        clicks = click_track(p.rate)
        ct = p.add_track("Tiếng tách (hiệu chỉnh)", clicks, undo=False, select=False)
        ct.solo = True
        p.set_selection(0, 0)
        info["clicks"] = clicks
        self.start_record(calibrate=info)
        if not self.rec:
            self._end_calibration(info)
            return
        QTimer.singleShot(int(len(clicks) / p.rate * 1000) + 800,
                          lambda: self.rec and self.rec.get("calibrate") is info and self.stop_record())

    def _end_calibration(self, info: dict):
        p = self.project
        p._restore(info["snap"])
        del p.undo_stack[info["undo_len"]:]
        p.redo_stack = info["redo"]
        p.modified = info["modified"]
        p.changed("tracks")

    def _finish_calibration(self, t, r: dict):
        info = r["calibrate"]
        rate = self.project.rate
        clicks = info["clicks"]
        recorded = t.render(0, len(clicks)) if t is not None else np.zeros_like(clicks)
        lag, clarity = measure_lag(clicks, recorded, rate)
        self._end_calibration(info)
        key = r["src_key"]
        name = key.split(":", 1)[1]
        if not r.get("overdub") or r.get("dac_ref") is None:
            QMessageBox.warning(self, "Hiệu chỉnh độ trễ", "Không phát được tiếng tách qua loa — kiểm tra loa/thiết bị phát.")
            return
        if clarity < 2.0:
            QMessageBox.warning(self, "Hiệu chỉnh độ trễ",
                                f"Không nghe rõ tiếng tách trong bản ghi từ {name}.\n"
                                "Hãy tăng âm lượng loa, đặt micro/điện thoại gần loa hơn và thử lại.")
            return
        old = float(self.cfg.setdefault("latency", {}).get(key, 0.0))
        new = old + lag * 1000.0 / rate
        self.cfg["latency"][key] = round(new, 1)
        self._save_cfg()
        approx = self._timings.get(r["session"], (None, False))[1] if r["kind"] == "phone" else False
        QMessageBox.information(
            self, "Hiệu chỉnh độ trễ",
            f"Đã đo xong cho {name}.\n\nĐộ lệch đo được: {lag * 1000.0 / rate:+.1f} ms\n"
            f"Bù độ trễ đã lưu: {new:.1f} ms\n\n"
            "Từ giờ bản ghi khi phát kèm sẽ tự khớp nhịp với các track khác."
            + ("\n\n⚠ Điện thoại này không báo thời điểm thu chính xác, độ lệch có thể dao động vài chục ms."
               if approx else ""))

    # ================================================================== sự kiện (luồng nền → giao diện)
    def _tick(self):
        appended = False
        for _ in range(2000):
            try:
                name, kw = self.events.get_nowait()
            except queue.Empty:
                break
            if name in ("local_chunk", "phone_audio"):
                r = self.rec
                if r and (name == "local_chunk" and r["kind"] == "local"
                          or name == "phone_audio" and r["kind"] == "phone" and kw["conn"].id == r["conn_id"]):
                    self._append_pcm(kw["pcm"])
                    appended = True
            else:
                self._handle_event(name, kw)

        if self.rec:
            t = self.rec["track"]
            if appended:
                self.ensure_visible(self._rec_clip(self.rec).end)
                self.panel.repaint_waves()
                self.ruler.update()
            self.pos_lbl.setText(fmt_time(self._rec_clip(self.rec).end / self.project.rate))
            now = time.monotonic()
            if now - getattr(self, "_last_info", 0) > 0.5:
                self._last_info = now
                row = self.panel.rows.get(t.id)
                if row:
                    row.control.refresh()
            self.rec_meter.set_levels(self._rec_level, self._rec_level)
            self._rec_level *= 0.7
        self._sync_video()
        if self.player.playing:
            pos = self.player.position
            if pos != self._last_play_pos:
                self._last_play_pos = pos
                self.ensure_visible(pos)
                self.pos_lbl.setText(fmt_time(pos / self.project.rate))
                self.panel.repaint_waves()
                self.ruler.update()
            self.play_meter.set_levels(*self.player.level)
        elif self._last_play_pos is not None:
            self._last_play_pos = None
            self.a_pause.setChecked(False)
            self.play_meter.reset()
            self.panel.repaint_waves()
            self.ruler.update()
            self.on_cursor_moved()

    _rec_level = 0.0

    def _handle_event(self, name: str, kw: dict):
        conn = kw.get("conn")
        r = self.rec
        same_conn = bool(r and r["kind"] == "phone" and conn is not None and conn.id == r["conn_id"])
        if name in ("local_level", "phone_level"):
            if r and (name == "local_level" or same_conn):
                self._rec_level = max(self._rec_level, kw["level"])
        elif name == "phone_connected":
            self.refresh_sources()
            if not self.is_recording:
                i = self.source_combo.findText(conn.label)
                if i >= 0:
                    self.source_combo.setCurrentIndex(i)
            self.status(f"📱 {conn.name} đã kết nối")
        elif name == "phone_disconnected":
            self.refresh_sources()
            self.status(f"📱 {conn.name} đã ngắt kết nối")
            if same_conn:
                self.rec = None
                self._finish_live(r)
                self.a_record.setText("Ghi âm")
                self._update_actions()
        elif name == "phone_recording_started":
            if same_conn:
                r["rate_in"], r["ch_in"] = kw["sample_rate"], kw["channels"]
                self.status(f"Đang ghi từ {conn.name} — {kw['sample_rate']} Hz "
                            f"{'stereo' if kw['channels'] == 2 else 'mono'}", 0)
            elif not r and kw["session"] not in self.pending_phone:
                self.start_record(phone_initiated=kw)       # bấm ghi ngay trên điện thoại
        elif name == "phone_recording_stopped":
            if same_conn and kw["session"] == r["session"]:
                self.rec = None
                self._finish_live(r)
                self.a_record.setText("Ghi âm")
                self.rec_meter.reset()
                self._update_actions()
                self.project.changed("data")
        elif name == "phone_file_begin":
            self.progress.setValue(0)
            self.progress.show()
        elif name == "phone_file_progress":
            self.progress.setValue(int(kw["received"] * 100 / max(1, kw["total"])))
        elif name == "phone_timing":
            self._timings[kw["session"]] = (kw["t0_pc_ns"], kw["approx"])
        elif name == "phone_recording_done":
            self.progress.hide()
            pend = self.pending_phone.pop(kw["session"], None)
            if pend is None:  # file gửi lại từ lần ghi offline trên điện thoại
                self._apply_final(None, None, kw["wav_path"], kw["session"], conn.name, replace=False)
                return
            unchanged = pend["clip"].data is pend["live"]
            self._apply_final(pend["track"], pend["clip"], kw["wav_path"], kw["session"], pend["label"],
                              replace=unchanged, r=pend.get("r"))
            if kw["source"] != "file":
                self.status("⚠ Không nhận được file gốc, đã dùng bản truyền trực tiếp")
        elif name == "phone_recording_failed":
            self.progress.hide()
            pend = self.pending_phone.pop(kw["session"], None)
            if pend:
                pend["track"].note = ""
                self.panel.repaint_waves()
            self.status("Không nhận được âm thanh từ điện thoại")
        elif name == "phone_error":
            self.status(f"📱 {conn.name}: {kw['message']}")
        elif name == "saved":
            self.status(f"Đã lưu {kw['path']}")
            self.refresh_recordings()
        elif name == "save_failed":
            self.status(f"Lỗi lưu MP3: {kw['error']}")
        elif name == "log":
            self.status(kw["message"])
        elif name == "video_frame":
            if not self._streaming and self.project.video is not None and kw["info"] is self.project.video:
                self._show_frame(kw["data"], kw["t"])
        elif name == "video_thumb":
            if kw["info"] is self.project.video:
                w, h = kw["size"]
                img = QImage(kw["data"], w, h, 3 * w, QImage.Format_RGB888).copy()
                self.video_thumbs.append((kw["t"], img))
                if self.panel.video_row is not None:
                    self.panel.video_row.strip.update()
        elif name == "export_progress":
            if self._export_dlg is not None:
                self._export_dlg.setValue(int(kw["v"] * 1000))
        elif name == "export_done":
            if self._export_dlg is not None:
                self._export_dlg.close()
                self._export_dlg = None
            self.status(f"Đã xuất video {kw['path']}", 15000)
            if QMessageBox.question(self, APP_NAME, f"Đã xuất video:\n{kw['path']}\n\nMở thư mục chứa file?") \
                    == QMessageBox.Yes:
                open_path(Path(kw["path"]).parent)
        elif name == "export_failed":
            if self._export_dlg is not None:
                self._export_dlg.close()
                self._export_dlg = None
            if kw["error"] != "Đã huỷ":
                QMessageBox.critical(self, APP_NAME, f"Lỗi xuất video:\n{kw['error']}")
            else:
                self.status("Đã huỷ xuất video")

    # ================================================================== chỉnh sửa
    def _can_edit(self) -> bool:
        if self.is_recording:
            self.status("Đang ghi âm — dừng ghi trước khi chỉnh sửa")
            return False
        self.player.stop()
        return True

    def _edit(self, fn, label: str):
        if not self._can_edit():
            return
        if not fn():
            self.status(f"{label}: hãy chọn một vùng trên track trước")

    do_edit = _edit

    def split_at_silences(self):
        if not self._can_edit():
            return
        v = ask_params(self, "Tách tại khoảng lặng", [
            ("thr", "Ngưỡng im lặng", -40.0, (-80, -10, 0, "dB")),
            ("min", "Khoảng lặng tối thiểu", 0.5, (0.05, 30, 2, "giây")),
            ("keep", "Giữ lại ở mỗi mép", 0.1, (0, 5, 2, "giây"))],
            note="Tách các đoạn có tiếng thành clip riêng và bỏ phần im lặng ở giữa "
                 "(áp cho vùng chọn, hoặc cả track nếu chưa chọn vùng).")
        if v is None:
            return
        n = self.project.split_at_silences(v["thr"], v["min"], v["keep"])
        self.status(f"Đã tách thêm {n} đoạn" if n else "Không tìm thấy khoảng lặng nào đủ dài")

    def undo(self):
        if self._can_edit():
            d = self.project.undo()
            self.status(f"Hoàn tác: {d}" if d else "Không còn gì để hoàn tác")

    def redo(self):
        if self._can_edit():
            d = self.project.redo()
            self.status(f"Làm lại: {d}" if d else "Không còn gì để làm lại")

    def _mix_new(self):
        self.project.mix_to_new_track()
        return True

    def _to_mono(self):
        self.project.stereo_to_mono()
        return True

    def _new_track(self, ch: int):
        if self._can_edit():
            self.project.add_track(f"Track {len(self.project.tracks) + 1}", np.zeros((0, ch), np.float32))

    def remove_track(self, t):
        if self._can_edit() and not t.recording:
            self.project.remove_track(t)

    def remove_selected_tracks(self):
        if not self._can_edit():
            return
        sel = [t for t in self.project.selected_tracks() if not t.recording]
        if sel:
            self.project.push_undo("Xoá track")
            self.project.tracks = [t for t in self.project.tracks if t not in sel]
            self.project.changed("tracks")

    # ------------------------------------------------------------ hiệu ứng
    def run_effect(self, name: str, fn):
        if not self._can_edit():
            return
        if not self.project.tracks:
            self.status("Chưa có track nào")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.project.apply_effect(name, fn)
            self.last_effect = (name, fn)
            self.a_repeat.setText(f"Lặp lại: {name}")
            self.status(f"Đã áp dụng: {name}")
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Lỗi khi áp dụng {name}:\n{e}")
        finally:
            QApplication.restoreOverrideCursor()
        self._update_actions()

    def repeat_effect(self):
        if self.last_effect:
            self.run_effect(*self.last_effect)

    def _target_peak(self) -> float:
        p = self.project
        pk = 0.0
        for t in p.target_tracks():
            a, b = p.range_for(t)
            pk = max(pk, fx.peak(t.render(a, b)))
        return pk

    def fx_amplify(self):
        pk = self._target_peak()
        default = round(-20 * np.log10(pk), 1) if pk > 0 else 0.0
        v = ask_params(self, "Khuếch đại", [("gain", "Tăng/giảm", default, (-50, 50, 1, "dB")),
                                            ("clip", "Cho phép vỡ tiếng (clipping)", False, None)],
                       note=f"Đỉnh hiện tại: {20 * np.log10(pk):.1f} dB" if pk > 0 else "")
        if v is None:
            return
        gain = v["gain"]
        if not v["clip"] and pk > 0 and pk * fx.db_to_gain(gain) > 1.0:
            gain = -20 * np.log10(pk)
        self.run_effect(f"Khuếch đại {gain:+.1f} dB", lambda x: fx.amplify(x, gain))

    def fx_normalize(self):
        v = ask_params(self, "Chuẩn hoá", [("peak", "Đỉnh tối đa", -1.0, (-30, 0, 1, "dB")),
                                           ("dc", "Loại bỏ lệch DC", True, None),
                                           ("indep", "Chuẩn hoá từng kênh riêng", False, None)])
        if v:
            self.run_effect("Chuẩn hoá", lambda x: fx.normalize(x, v["peak"], v["dc"], v["indep"]))

    def fx_noise(self):
        if not self._can_edit():
            return
        dlg = NoiseReductionDialog(self, self.noise_profile is not None, self._take_noise_profile, self.noise_params)
        if dlg.exec() and not dlg.profile_taken and self.noise_profile is not None:
            self.noise_params = dlg.params()
            prof, prm = self.noise_profile, dict(self.noise_params)
            self.run_effect("Giảm nhiễu", lambda x: fx.noise_reduce(x, prof, prm["reduction"], prm["sensitivity"],
                                                                     prm["smoothing"]))

    def _take_noise_profile(self):
        p = self.project
        if not p.has_selection or not p.target_tracks():
            QMessageBox.information(self, APP_NAME, "Hãy chọn một đoạn chỉ có tiếng ồn trước.")
            return
        t = p.target_tracks()[0]
        a, b = p.range_for(t)
        try:
            self.noise_profile = fx.noise_profile(t.render(a, b))
            self.status("Đã lấy mẫu nhiễu. Giờ chọn vùng cần lọc (hoặc bỏ chọn = cả track) rồi mở lại Giảm nhiễu.")
        except ValueError as e:
            QMessageBox.information(self, APP_NAME, str(e))

    def fx_compressor(self):
        v = ask_params(self, "Nén dải động", [
            ("thr", "Ngưỡng", -18.0, (-60, 0, 1, "dB")), ("ratio", "Tỉ lệ", 3.0, (1.1, 20, 1, ": 1")),
            ("att", "Attack", 10.0, (1, 500, 0, "ms")), ("rel", "Release", 150.0, (10, 3000, 0, "ms")),
            ("makeup", "Tăng âm lượng bù sau khi nén", True, None)])
        if v:
            r = self.project.rate
            self.run_effect("Nén dải động", lambda x: fx.compressor(x, r, v["thr"], v["ratio"], v["att"], v["rel"],
                                                                    v["makeup"]))

    def fx_echo(self):
        v = ask_params(self, "Tiếng vang", [("delay", "Độ trễ", 0.3, (0.01, 5, 2, "giây")),
                                            ("decay", "Độ suy giảm", 0.5, (0.05, 0.95, 2, ""))])
        if v:
            r = self.project.rate
            self.run_effect("Tiếng vang", lambda x: fx.echo(x, r, v["delay"], v["decay"]))

    def fx_speed(self):
        v = ask_params(self, "Đổi tốc độ", [("pct", "Thay đổi", 10.0, (-90, 400, 1, "%"))],
                       note="Đổi cả tốc độ và cao độ (như tua băng). Dương = nhanh hơn.")
        if v:
            r = self.project.rate
            self.run_effect(f"Đổi tốc độ {v['pct']:+.0f}%", lambda x: fx.change_speed(x, r, v["pct"]))

    # ------------------------------------------------------------ tạo âm
    def _gen(self, name: str, make):
        if not self._can_edit():
            return
        self.project.insert_generated(name, make)

    def gen_silence(self):
        v = ask_params(self, "Im lặng", [("dur", "Độ dài", 5.0, (0.01, 3600, 2, "giây"))])
        if v:
            r = self.project.rate
            self._gen("Im lặng", lambda ch: np.zeros((int(r * v["dur"]), ch), np.float32))

    def gen_tone(self):
        v = ask_params(self, "Tạo âm", [("wave", "Dạng sóng", "sine", ["sine", "square", "sawtooth"]),
                                        ("freq", "Tần số", 440.0, (1, 20000, 1, "Hz")),
                                        ("amp", "Biên độ", 0.8, (0, 1, 2, "")),
                                        ("dur", "Độ dài", 5.0, (0.01, 3600, 2, "giây"))])
        if v:
            r = self.project.rate
            self._gen("Âm " + v["wave"], lambda ch: fx.tone(r, v["dur"], v["freq"], v["amp"], ch, v["wave"]))

    def gen_noise(self):
        v = ask_params(self, "Tiếng ồn trắng", [("amp", "Biên độ", 0.5, (0, 1, 2, "")),
                                               ("dur", "Độ dài", 5.0, (0.01, 3600, 2, "giây"))])
        if v:
            r = self.project.rate
            self._gen("Tiếng ồn", lambda ch: fx.white_noise(r, v["dur"], v["amp"], ch))

    # ================================================================== tệp
    def _confirm_discard(self) -> bool:
        if not self.project.modified or not self.project.tracks:
            return True
        r = QMessageBox.question(self, APP_NAME, "Lưu thay đổi của dự án hiện tại?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save_project()
        return r == QMessageBox.Discard

    def _set_project(self, p: Project):
        self.player.stop()
        self.project.listeners.clear()
        self.project = p
        p.listeners.append(self._on_project_changed)
        self.player.set_project(p)
        self.rate_combo.blockSignals(True)
        self.rate_combo.setCurrentText(str(p.rate))
        self.rate_combo.blockSignals(False)
        self.pending_phone.clear()
        self._detach_video()
        if p.video_path:
            try:
                self._attach_video(vid.probe(p.video_path))
            except Exception as e:
                QMessageBox.warning(self, APP_NAME, f"Không mở được video của dự án:\n{p.video_path}\n{e}")
        self._on_project_changed("tracks")
        self.zoom_fit()

    def new_project(self):
        if self.is_recording or not self._confirm_discard():
            return
        self._set_project(Project(int(self.rate_combo.currentText())))

    def open_dialog(self):
        f, _ = QFileDialog.getOpenFileName(self, "Mở", self.cfg["output_dir"],
                                           f"Dự án, âm thanh, video (*{PROJECT_EXT} *.wav *.mp3 *.flac *.ogg *.aif *.aiff "
                                           f"{' '.join('*' + e for e in sorted(vid.VIDEO_EXTS))});;"
                                           f"Dự án WiFi Recorder (*{PROJECT_EXT});;{IMPORT_FILTER}")
        if f:
            self.open_any(Path(f))

    def open_any(self, path: Path):
        if self.is_recording:
            return
        if path.suffix.lower() == PROJECT_EXT:
            if not self._confirm_discard():
                return
            try:
                self._set_project(Project.load(path))
            except Exception as e:
                QMessageBox.critical(self, APP_NAME, f"Không mở được dự án:\n{e}")
        elif vid.is_video(path):
            self.import_video(path)
        else:
            self.import_files([path])

    # ================================================================== video
    def import_video_dialog(self):
        f, _ = QFileDialog.getOpenFileName(self, "Nhập video", self.cfg.get("video_dir", self.cfg["output_dir"]),
                                           vid.VIDEO_FILTER)
        if f:
            self.cfg["video_dir"] = str(Path(f).parent)
            self.import_video(Path(f))

    def import_video(self, path: Path):
        if not self._can_edit():
            return
        if self.project.video is not None and QMessageBox.question(
                self, APP_NAME, "Dự án đã có video. Thay bằng video mới?") != QMessageBox.Yes:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            info = vid.probe(path)
            data = vid.extract_audio(info, self.project.rate) if info.has_audio else None
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP_NAME, f"Không mở được video:\n{e}")
            return
        QApplication.restoreOverrideCursor()
        if data is not None and len(data):
            self.project.add_track(f"🎬 {path.stem}", data)
        self._attach_video(info)
        self.project.modified = True
        self._on_project_changed("tracks")
        self.zoom_fit()
        self.status(f"Đã nhập video {info.name} ({info.width}×{info.height}, {fmt_time(info.duration)})"
                    + ("" if info.has_audio else " — video không có âm thanh"))

    def _attach_video(self, info):
        self._detach_video()
        self.project.video = info
        size = info.preview_size()
        self._grabber = vid.FrameGrabber(info, size, lambda t, b: self.events.put(
            ("video_frame", {"info": info, "t": t, "data": b})))
        self._streamer = vid.FrameStreamer(info, size)
        self.video_thumbs = []
        cancel = threading.Event()
        self._thumb_cancel = cancel
        tsize = info.thumb_size(54)
        threading.Thread(target=lambda: vid.thumbnails(info, 54, lambda t, b: self.events.put(
            ("video_thumb", {"info": info, "t": t, "data": b, "size": tsize})), cancel=cancel), daemon=True).start()
        self.video_dock.show()
        QTimer.singleShot(0, lambda: self.resizeDocks([self.video_dock, self.rec_dock], [380, 240], Qt.Vertical))
        self._grabber.request(self.project.sel_start / self.project.rate)

    def _detach_video(self):
        if self._thumb_cancel is not None:
            self._thumb_cancel.set()
        if self._grabber is not None:
            self._grabber.close()
        if self._streamer is not None:
            self._streamer.stop()
        self._grabber = self._streamer = self._thumb_cancel = None
        self._streaming = False
        self.video_thumbs = []
        self.project.video = None
        self.video_view.set_frame(None)

    def remove_video(self):
        if self.project.video is None:
            return
        self._detach_video()
        self.project.modified = True
        self._on_project_changed("tracks")
        self.status("Đã bỏ video khỏi dự án (track âm thanh vẫn giữ)")

    def _show_frame(self, data: bytes, t: float):
        st = self._streamer
        if st is None:
            return
        img = QImage(data, st.w, st.h, 3 * st.w, QImage.Format_RGB888).copy()
        self.video_view.set_frame(img, fmt_time(t))

    def _sync_video(self):
        """Khi đang phát/ghi: lấy khung hình theo đồng hồ âm thanh. Khi dừng: khung tại con trỏ."""
        st = self._streamer
        if st is None:
            return
        rate = self.project.rate
        if self.player.playing:
            pos = self.player.position
        elif self.rec:
            pos = self._rec_clip(self.rec).end
        else:
            pos = None
        if pos is None:
            if self._streaming:
                self._streaming = False
                st.stop()
                self._grabber.request(self.project.sel_start / rate)
            return
        if not self._streaming or pos < self._stream_last - rate // 10:   # bắt đầu phát / lặp lại
            st.start(pos / rate)
            self._streaming = True
        self._stream_last = pos
        frame = st.frame_for(pos / rate)
        if frame is not None:
            self._show_frame(frame, pos / rate)
        elif pos / rate >= self.project.video.duration:
            self.video_view.set_frame(self.video_view.image, fmt_time(pos / rate) + " (hết video)")

    def export_video_dialog(self):
        info = self.project.video
        if info is None or not self._can_edit():
            return
        p = self.project
        fields = [("fmt", "Định dạng", "mp4", [e[1:] for e in vid.EXPORT_FORMATS])]
        if p.has_selection:
            fields.append(("sel", "Chỉ xuất vùng chọn", False, None))
        fields.append(("reenc", "Mã hoá lại hình (chậm hơn; chỉ cần khi file xuất bị lỗi)", False, None))
        v = ask_params(self, "Xuất video", fields,
                       note="Hình lấy từ video gốc (giữ nguyên chất lượng), âm thanh là bản trộn của tất cả "
                            "track đang bật. Muốn bỏ tiếng gốc của video: bấm 'Tắt tiếng' track 🎬.\n"
                            f"Độ dài video: {fmt_time(info.duration)}")
        if v is None:
            return
        ext = "." + v["fmt"]
        f, _ = QFileDialog.getSaveFileName(self, "Xuất video",
                                           str(Path(self.cfg["output_dir"]) / (info.path.stem + "_da_sua" + ext)),
                                           vid.EXPORT_FORMATS[ext])
        if not f:
            return
        out = Path(f)
        if out.suffix.lower() not in vid.EXPORT_FORMATS:
            out = out.with_suffix(ext)
        if out.resolve() == info.path.resolve():
            QMessageBox.warning(self, APP_NAME, "Không thể ghi đè lên chính video gốc. Hãy chọn tên khác.")
            return
        rate = p.rate
        if v.get("sel"):
            start, end = p.sel_start / rate, min(p.sel_end / rate, info.duration)
        else:
            start, end = 0.0, info.duration
        if end <= start:
            QMessageBox.information(self, APP_NAME, "Vùng chọn nằm ngoài độ dài video.")
            return
        audio = p.mix(int(start * rate), int(round(end * rate)), channels=2)
        cancel = threading.Event()
        dlg = QProgressDialog("Đang xuất video...", "Huỷ", 0, 1000, self)
        dlg.setWindowTitle("Xuất video")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.canceled.connect(cancel.set)
        dlg.setValue(0)
        self._export_dlg = dlg
        bitrate = max(192, int(self.cfg["bitrate"]))

        def work():
            try:
                vid.export_video(info, audio, rate, out, start=start, end=end, reencode=v["reenc"],
                                 audio_bitrate=bitrate,
                                 progress=lambda x: self.events.put(("export_progress", {"v": x})), cancel=cancel)
                self.events.put(("export_done", {"path": str(out)}))
            except Exception as e:
                self.events.put(("export_failed", {"error": str(e)}))
        threading.Thread(target=work, daemon=True).start()

    def import_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Nhập âm thanh", self.cfg["output_dir"], IMPORT_FILTER)
        if files:
            self.import_files([Path(f) for f in files])

    def import_files(self, paths: list[Path]):
        if not self._can_edit():
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for path in paths:
                try:
                    data, sr = load_audio(path)
                except Exception as e:
                    QMessageBox.critical(self, APP_NAME, f"Không mở được {path.name}:\n{e}")
                    continue
                if not self.project.tracks:
                    self.project.rate = sr  # dự án trống → lấy tần số của file
                    self.rate_combo.blockSignals(True)
                    self.rate_combo.setCurrentText(str(sr))
                    self.rate_combo.blockSignals(False)
                self.project.add_track(path.stem, data, rate=sr)
        finally:
            QApplication.restoreOverrideCursor()
        self.zoom_fit()

    def save_project(self, ask: bool = False) -> bool:
        p = self.project
        path = p.path
        if ask or path is None:
            f, _ = QFileDialog.getSaveFileName(self, "Lưu dự án", str(Path(self.cfg["output_dir"]) / "du_an"),
                                               f"Dự án WiFi Recorder (*{PROJECT_EXT})")
            if not f:
                return False
            path = Path(f).with_suffix(PROJECT_EXT)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            p.save(path)
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Lỗi lưu dự án:\n{e}")
            return False
        finally:
            QApplication.restoreOverrideCursor()
        self.status(f"Đã lưu dự án {path}")
        self._update_actions()
        return True

    def export_mix(self, ext: str, selection: bool = False, tracks=None):
        p = self.project
        if not p.tracks:
            self.status("Chưa có gì để xuất")
            return
        if selection and not p.has_selection:
            self.status("Chưa chọn vùng")
            return
        base = p.path.stem if p.path else time.strftime("ban_tron_%Y%m%d_%H%M%S")
        filters = {".mp3": "MP3 (*.mp3)", ".wav": "WAV (*.wav)", ".flac": "FLAC (*.flac)", ".ogg": "OGG (*.ogg)"}
        f, _ = QFileDialog.getSaveFileName(self, "Xuất âm thanh", str(Path(self.cfg["output_dir"]) / (base + ext)),
                                           ";;".join([filters[ext]] + [v for k, v in filters.items() if k != ext]))
        if not f:
            return
        out = Path(f)
        if not out.suffix:
            out = out.with_suffix(ext)
        a, b = (p.sel_start, p.sel_end) if selection else (0, p.length)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            data = p.mix(a, b, tracks)
            peak = fx.peak(data)
            save_audio(out, data, p.rate, int(self.cfg["bitrate"]))
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Lỗi xuất file:\n{e}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        msg = f"Đã xuất {out}"
        if peak > 1.0:
            msg += " (⚠ bản trộn bị vỡ tiếng — hãy giảm âm lượng track)"
        self.status(msg, 10000)
        self.refresh_recordings()

    def export_tracks(self, tracks):
        self.export_mix(".mp3", tracks=tracks)

    def preferences(self):
        dlg = PreferencesDialog(self, self.cfg, list_output_devices())
        if dlg.exec():
            vals = dlg.values()
            if vals.pop("reset_latency"):
                self.cfg["latency"] = {}
            self.cfg.update(vals)
            self.player.device = self.cfg["output_device"]
            self._save_cfg()
            self.refresh_recordings()

    def _save_cfg(self):
        self.cfg["channels"] = 2 if self.rec_channels.currentIndex() == 1 else 1
        try:
            path = config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ danh sách bản ghi
    def refresh_recordings(self):
        self.rec_list.clear()
        d = Path(self.cfg["output_dir"])
        try:
            files = sorted([f for f in d.iterdir() if f.suffix.lower() in (".mp3", ".wav", ".flac", ".ogg")],
                           key=lambda f: f.stat().st_mtime, reverse=True)
        except OSError:
            files = []
        for f in files[:500]:
            it = QListWidgetItem(f"{f.name}\n   {time.strftime('%d/%m/%Y %H:%M', time.localtime(f.stat().st_mtime))}"
                                 f" · {f.stat().st_size / 1e6:.1f} MB")
            it.setData(Qt.UserRole, str(f))
            it.setToolTip(str(f))
            self.rec_list.addItem(it)

    def _import_selected_recording(self):
        it = self.rec_list.currentItem()
        if it:
            self.import_files([Path(it.data(Qt.UserRole))])

    def _open_output_dir(self):
        d = Path(self.cfg["output_dir"])
        d.mkdir(parents=True, exist_ok=True)
        open_path(d)

    def _delete_recording(self):
        it = self.rec_list.currentItem()
        if not it:
            return
        f = Path(it.data(Qt.UserRole))
        if QMessageBox.question(self, APP_NAME, f"Xoá {f.name}?") == QMessageBox.Yes:
            f.unlink(missing_ok=True)
            self.refresh_recordings()

    # ------------------------------------------------------------ khác
    def show_shortcuts(self):
        QMessageBox.information(self, "Phím tắt", """
<table>
<tr><td><b>Space</b></td><td>Phát / Dừng</td></tr>
<tr><td><b>Shift+Space</b></td><td>Phát lặp vùng chọn</td></tr>
<tr><td><b>P</b></td><td>Tạm dừng</td></tr>
<tr><td><b>R</b></td><td>Ghi âm / Dừng ghi</td></tr>
<tr><td><b>Home / End</b></td><td>Về đầu / về cuối</td></tr>
<tr><td><b>Ctrl+Z / Ctrl+Y</b></td><td>Hoàn tác / Làm lại</td></tr>
<tr><td><b>Ctrl+X / C / V</b></td><td>Cắt / Chép / Dán</td></tr>
<tr><td><b>Delete</b></td><td>Xoá vùng chọn</td></tr>
<tr><td><b>Ctrl+T</b></td><td>Chỉ giữ vùng chọn (Trim)</td></tr>
<tr><td><b>Ctrl+L</b></td><td>Làm im lặng vùng chọn</td></tr>
<tr><td><b>Ctrl+D</b></td><td>Nhân bản vùng chọn thành track mới</td></tr>
<tr><td><b>Ctrl+A</b></td><td>Chọn tất cả</td></tr>
<tr><td><b>Ctrl+1 / Ctrl+3</b></td><td>Phóng to / Thu nhỏ</td></tr>
<tr><td><b>Ctrl+E / Ctrl+F</b></td><td>Vừa vùng chọn / Vừa cửa sổ</td></tr>
<tr><td><b>Ctrl+R</b></td><td>Lặp lại hiệu ứng cuối</td></tr>
<tr><td><b>Ctrl + lăn chuột</b></td><td>Phóng to/thu nhỏ tại con trỏ chuột</td></tr>
<tr><td><b>Shift + lăn chuột</b></td><td>Cuộn ngang</td></tr>
<tr><td><b>Shift + bấm</b></td><td>Mở rộng vùng chọn</td></tr>
<tr><td><b>Nhấp đúp</b></td><td>Chọn cả track</td></tr>
</table>""")

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [Path(u.toLocalFile()) for u in ev.mimeData().urls() if u.isLocalFile()]
        proj = [p for p in paths if p.suffix.lower() == PROJECT_EXT]
        videos = [p for p in paths if vid.is_video(p)]
        if proj:
            self.open_any(proj[0])
        elif videos:
            self.import_video(videos[0])
        else:
            self.import_files(paths)

    def closeEvent(self, ev):
        if self.is_recording:
            if QMessageBox.question(self, APP_NAME, "Đang ghi âm. Dừng ghi và thoát?") != QMessageBox.Yes:
                ev.ignore()
                return
            self.stop_record()
        if not self._confirm_discard():
            ev.ignore()
            return
        self._save_cfg()
        self.player.stop()
        self._detach_video()
        self.server.stop()
        ev.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    w = EditorWindow(sys.argv[1] if len(sys.argv) > 1 else None)
    w.show()
    sys.exit(app.exec())
