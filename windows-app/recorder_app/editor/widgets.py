"""Các widget vẽ: thước thời gian, track (bảng điều khiển + sóng âm), đồng hồ mức âm."""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QInputDialog, QLabel, QMenu, QScrollArea, QSizePolicy,
                               QSlider, QToolButton, QVBoxLayout, QWidget)

from .model import Track
from .summary import Summary

if TYPE_CHECKING:
    from .main_window import EditorWindow

CONTROL_W = 176
DEFAULT_TRACK_H = 150

C_WAVE = QColor(50, 50, 200)
C_RMS = QColor(100, 100, 220)
C_BG = QColor(255, 255, 255)
C_BG_SEL = QColor(200, 210, 242)
C_BEYOND = QColor(214, 218, 226)
C_BEYOND_SEL = QColor(176, 186, 220)
C_CLIP = QColor(220, 0, 0)
C_PLAYHEAD = QColor(0, 160, 0)
C_PANEL = QColor(226, 228, 233)
C_PANEL_SEL = QColor(196, 206, 236)
C_EMPTY = QColor(160, 164, 172)


def fmt_time(seconds: float, ms: bool = True) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int(seconds // 60 % 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}" if ms else f"{h:02d}:{m:02d}:{int(s):02d}"


def parse_time(text: str) -> float:
    parts = [float(p) for p in text.replace(",", ".").split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


# ====================================================================== thước thời gian
class Ruler(QWidget):
    def __init__(self, editor: "EditorWindow"):
        super().__init__()
        self.editor = editor
        self.setFixedHeight(28)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Bấm để phát từ vị trí này (Quick-Play)")

    def paintEvent(self, ev):
        ed = self.editor
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(236, 237, 240))
        p.fillRect(0, 0, CONTROL_W, self.height(), QColor(222, 224, 230))
        rate = ed.project.rate
        spp = ed.spp
        px_per_sec = rate / spp
        # chọn bước vạch sao cho cách nhau >= 90 px
        steps = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300,
                 600, 900, 1800, 3600]
        major = next((s for s in steps if s * px_per_sec >= 90), 3600)
        minor = major / 5
        t0 = ed.h0 / rate
        t1 = (ed.h0 + self.width() * spp) / rate
        p.setClipRect(CONTROL_W, 0, self.width() - CONTROL_W, self.height())
        p.setPen(QColor(90, 90, 100))
        k = math.floor(t0 / minor)
        while k * minor <= t1:
            t = k * minor
            x = CONTROL_W + (t * rate - ed.h0) / spp
            is_major = abs(t / major - round(t / major)) < 1e-6
            p.drawLine(QLineF(x, self.height() - (12 if is_major else 5), x, self.height()))
            if is_major:
                if major >= 1:
                    lbl = fmt_time(t, ms=False)
                    lbl = lbl[3:] if t < 3600 else lbl
                else:
                    lbl = f"{int(t // 60):d}:{t % 60:06.3f}".rstrip("0").rstrip(".")
                p.drawText(QPointF(x + 3, 13), lbl)
            k += 1
        # vùng chọn
        if ed.project.has_selection:
            x1 = CONTROL_W + (ed.project.sel_start - ed.h0) / spp
            x2 = CONTROL_W + (ed.project.sel_end - ed.h0) / spp
            p.fillRect(QRectF(x1, self.height() - 4, x2 - x1, 4), QColor(70, 90, 200))
        # đầu phát
        ph = ed.playhead
        if ph is not None:
            x = CONTROL_W + (ph - ed.h0) / spp
            p.setPen(Qt.NoPen)
            p.setBrush(C_PLAYHEAD)
            p.drawPolygon(QPolygonF([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x, 10)]))
        p.setClipping(False)
        p.setPen(QColor(160, 160, 170))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)

    def mousePressEvent(self, ev):
        x = ev.position().x() - CONTROL_W
        if x >= 0:
            self.editor.quick_play(int(self.editor.h0 + x * self.editor.spp))


# ====================================================================== sóng âm
class WaveView(QWidget):
    def __init__(self, editor: "EditorWindow", track: Track):
        super().__init__()
        self.editor = editor
        self.track = track
        self.summary = Summary()
        self.setMouseTracking(False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.IBeamCursor)
        self._drag_anchor: int | None = None

    def sample_at(self, x: float) -> int:
        return max(0, int(round(self.editor.h0 + x * self.editor.spp)))

    def x_of(self, sample: float) -> float:
        return (sample - self.editor.h0) / self.editor.spp

    # ------------------------------------------------------------ vẽ
    def paintEvent(self, ev):
        ed = self.editor
        t = self.track
        proj = ed.project
        p = QPainter(self)
        w, h = self.width(), self.height()
        end_x = self.x_of(t.length)
        sel = t.selected and proj.has_selection
        sx1 = self.x_of(proj.sel_start) if sel else 0
        sx2 = self.x_of(proj.sel_end) if sel else 0

        p.fillRect(0, 0, w, h, C_BG)
        if end_x < w:
            p.fillRect(QRectF(max(0, end_x), 0, w, h), C_BEYOND)
        if sel:
            p.fillRect(QRectF(sx1, 0, sx2 - sx1, h), C_BG_SEL)
            if end_x < sx2:
                p.fillRect(QRectF(max(end_x, sx1), 0, sx2 - max(end_x, sx1), h), C_BEYOND_SEL)

        self.summary.update(t)
        chs = t.channels
        lane_h = h / chs
        gain = 1.0
        for ch in range(chs):
            top = ch * lane_h
            mid = top + lane_h / 2
            half = lane_h / 2 - 2
            p.setPen(QColor(200, 200, 215))
            p.drawLine(QLineF(0, mid, min(w, end_x), mid))
            self._draw_channel(p, ch, mid, half * gain, w)
            if ch > 0:
                p.setPen(QColor(120, 120, 130))
                p.drawLine(QLineF(0, top, w, top))
            # vạch thang biên độ bên trái giống Audacity
            p.setPen(QColor(150, 150, 160))
            f = p.font()
            f.setPointSize(7)
            p.setFont(f)
            p.drawText(QPointF(2, top + 10), "1.0")
            p.drawText(QPointF(2, top + lane_h - 2), "-1.0")

        # con trỏ (khi không có vùng chọn)
        if t.selected and not proj.has_selection:
            cx = self.x_of(proj.sel_start)
            p.setPen(QPen(QColor(0, 0, 0), 1))
            p.drawLine(QLineF(cx, 0, cx, h))
        # đầu phát
        if ed.playhead is not None:
            x = self.x_of(ed.playhead)
            p.setPen(QPen(C_PLAYHEAD, 1))
            p.drawLine(QLineF(x, 0, x, h))
        if t.recording or t.note:
            p.setPen(QColor(200, 0, 0))
            f = QFont(p.font())
            f.setPointSize(9)
            f.setBold(True)
            p.setFont(f)
            p.drawText(QPointF(30, 14), "● ĐANG GHI" if t.recording else t.note)
        p.setPen(QColor(110, 110, 120))
        p.drawLine(0, h - 1, w, h - 1)

    def _draw_channel(self, p: QPainter, ch: int, mid: float, half: float, w: int):
        ed = self.editor
        data = self.track.data
        if not len(data):
            return
        spp = ed.spp
        if spp < 1.0:  # phóng to sát: vẽ từng mẫu
            first = max(0, int(ed.h0) - 1)
            last = min(len(data), int(ed.h0 + w * spp) + 2)
            if last <= first:
                return
            ys = mid - np.clip(data[first:last, ch], -1, 1) * half
            xs = (np.arange(first, last) - ed.h0) / spp
            p.setPen(QPen(C_WAVE, 1))
            p.drawPolyline(QPolygonF([QPointF(float(x), float(y)) for x, y in zip(xs, ys)]))
            if spp < 0.2:
                p.setBrush(C_WAVE)
                for x, y in zip(xs, ys):
                    p.drawEllipse(QPointF(float(x), float(y)), 2.5, 2.5)
            return
        r = self.summary.columns(data, ch, ed.h0, spp, w)
        if r is None:
            return
        xs, mn, mx, rms = r
        y_top = mid - np.clip(mx, -1, 1) * half
        y_bot = mid - np.clip(mn, -1, 1) * half
        y_bot = np.maximum(y_bot, y_top + 1)
        p.setPen(QPen(C_WAVE, 1))
        p.drawLines([QLineF(float(x) + .5, float(a), float(x) + .5, float(b)) for x, a, b in zip(xs, y_top, y_bot)])
        rr = np.minimum(rms, np.minimum(mx, -mn).clip(min=0))
        r_top = mid - rr * half
        r_bot = mid + rr * half
        mask = rr * half >= 1
        if mask.any():
            p.setPen(QPen(C_RMS, 1))
            p.drawLines([QLineF(float(x) + .5, float(a), float(x) + .5, float(b))
                         for x, a, b in zip(xs[mask], r_top[mask], r_bot[mask])])
        clip = (mx >= 0.999) | (mn <= -0.999)
        if clip.any():
            p.setPen(QPen(C_CLIP, 1))
            p.drawLines([QLineF(float(x) + .5, mid - half, float(x) + .5, mid + half) for x in xs[clip]])

    # ------------------------------------------------------------ chuột
    def mousePressEvent(self, ev):
        ed = self.editor
        if ev.button() != Qt.LeftButton or ed.is_recording:
            return
        s = self.sample_at(ev.position().x())
        proj = ed.project
        if ev.modifiers() & Qt.ShiftModifier:
            anchor = proj.sel_start if abs(s - proj.sel_end) < abs(s - proj.sel_start) else proj.sel_end
            self._drag_anchor = anchor
            self.track.selected = True
            proj.set_selection(anchor, s)
        else:
            if ev.modifiers() & Qt.ControlModifier:
                self.track.selected = not self.track.selected
            else:
                for t in proj.tracks:
                    t.selected = t is self.track
            self._drag_anchor = s
            self._anchor_track = self.track
            proj.set_selection(s, s)
        ed.on_cursor_moved()

    def mouseMoveEvent(self, ev):
        if self._drag_anchor is None or not (ev.buttons() & Qt.LeftButton):
            return
        ed = self.editor
        x = ev.position().x()
        if x > self.width():  # tự cuộn khi kéo ra ngoài
            ed.scroll_by_pixels(min(40, x - self.width()))
        elif x < 0:
            ed.scroll_by_pixels(max(-40, x))
        s = self.sample_at(x)
        ed.project.set_selection(self._drag_anchor, s)
        # kéo dọc sang track khác → chọn thêm các track ở giữa
        gy = self.mapToGlobal(ev.position().toPoint()).y()
        ed.select_tracks_between(getattr(self, "_anchor_track", self.track), gy)

    def mouseReleaseEvent(self, ev):
        self._drag_anchor = None

    def mouseDoubleClickEvent(self, ev):
        for t in self.editor.project.tracks:
            t.selected = t is self.track
        self.editor.project.set_selection(0, self.track.length)

    def wheelEvent(self, ev):
        mods = ev.modifiers()
        dy = ev.angleDelta().y() or ev.angleDelta().x()
        if mods & Qt.ControlModifier:
            self.editor.zoom(1.25 if dy > 0 else 0.8, anchor_x=ev.position().x())
            ev.accept()
        elif mods & Qt.ShiftModifier or ev.angleDelta().x():
            self.editor.scroll_by_pixels(-dy / 2)
            ev.accept()
        else:
            ev.ignore()


# ====================================================================== bảng điều khiển track
class TrackControl(QFrame):
    def __init__(self, editor: "EditorWindow", track: Track):
        super().__init__()
        self.editor = editor
        self.track = track
        self.setFixedWidth(CONTROL_W)
        self.setAutoFillBackground(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 3, 4, 3)
        lay.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(2)
        close = QToolButton(text="✕")
        close.setToolTip("Xoá track")
        close.setAutoRaise(True)
        close.clicked.connect(lambda: editor.remove_track(track))
        self.name_btn = QToolButton()
        self.name_btn.setPopupMode(QToolButton.InstantPopup)
        self.name_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.name_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        menu = QMenu(self)
        menu.addAction("Đổi tên...", self._rename)
        menu.addAction("Lên trên", lambda: editor.project.move_track(track, -1))
        menu.addAction("Xuống dưới", lambda: editor.project.move_track(track, 1))
        menu.addSeparator()
        menu.addAction("Stereo → Mono", self._to_mono)
        menu.addAction("Xuất track này...", lambda: editor.export_tracks([track]))
        self.name_btn.setMenu(menu)
        top.addWidget(close)
        top.addWidget(self.name_btn)
        lay.addLayout(top)

        ms = QHBoxLayout()
        self.mute = QToolButton(text="Tắt tiếng", checkable=True)
        self.solo = QToolButton(text="Solo", checkable=True)
        for b in (self.mute, self.solo):
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            ms.addWidget(b)
        self.mute.toggled.connect(lambda v: editor.project.set_track_prop(track, mute=v))
        self.solo.toggled.connect(lambda v: editor.project.set_track_prop(track, solo=v))
        lay.addLayout(ms)

        self.gain = QSlider(Qt.Horizontal)
        self.gain.setRange(-360, 360)
        self.gain.valueChanged.connect(self._gain_changed)
        self.pan = QSlider(Qt.Horizontal)
        self.pan.setRange(-100, 100)
        self.pan.valueChanged.connect(lambda v: editor.project.set_track_prop(track, pan=v / 100))
        g = QHBoxLayout()
        g.addWidget(QLabel("−"))
        g.addWidget(self.gain)
        g.addWidget(QLabel("+"))
        pn = QHBoxLayout()
        pn.addWidget(QLabel("T"))
        pn.addWidget(self.pan)
        pn.addWidget(QLabel("P"))
        lay.addLayout(g)
        lay.addLayout(pn)
        self.info = QLabel()
        self.info.setStyleSheet("color:#555; font-size:8pt")
        lay.addWidget(self.info)
        lay.addStretch(1)
        self.refresh()

    def _gain_changed(self, v: int):
        self.gain.setToolTip(f"Âm lượng: {v / 10:+.1f} dB")
        self.editor.project.set_track_prop(self.track, gain_db=v / 10)

    def _rename(self):
        name, ok = QInputDialog.getText(self, "Đổi tên track", "Tên:", text=self.track.name)
        if ok and name.strip():
            self.editor.project.set_track_prop(self.track, name=name.strip())

    def _to_mono(self):
        for t in self.editor.project.tracks:
            t.selected = t is self.track
        self.editor.project.stereo_to_mono()

    def refresh(self):
        t = self.track
        self.name_btn.setText(t.name + "  ▾")
        self.name_btn.setToolTip(t.name)
        for w, v in ((self.mute, t.mute), (self.solo, t.solo), (self.gain, int(round(t.gain_db * 10))),
                     (self.pan, int(round(t.pan * 100)))):
            w.blockSignals(True)
            if isinstance(w, QToolButton):
                w.setChecked(v)
            else:
                w.setValue(v)
            w.blockSignals(False)
        self.gain.setToolTip(f"Âm lượng: {t.gain_db:+.1f} dB")
        self.pan.setToolTip(f"Cân bằng trái/phải: {t.pan * 100:+.0f}%")
        self.info.setText(f"{'Stereo' if t.channels == 2 else 'Mono'}, {self.editor.project.rate} Hz\n"
                          f"32-bit float · {fmt_time(t.length / self.editor.project.rate, ms=False)}")
        pal = self.palette()
        pal.setColor(self.backgroundRole(), C_PANEL_SEL if t.selected else C_PANEL)
        self.setPalette(pal)

    def mousePressEvent(self, ev):
        proj = self.editor.project
        if ev.modifiers() & Qt.ControlModifier:
            self.track.selected = not self.track.selected
        else:
            for t in proj.tracks:
                t.selected = t is self.track
        proj.changed("selection")


class _ResizeHandle(QWidget):
    def __init__(self, row: "TrackRow"):
        super().__init__()
        self.row = row
        self.setFixedHeight(5)
        self.setCursor(Qt.SizeVerCursor)
        self._y0 = None

    def mousePressEvent(self, ev):
        self._y0 = ev.globalPosition().y()
        self._h0 = self.row.wave.height()

    def mouseMoveEvent(self, ev):
        if self._y0 is not None:
            self.row.set_track_height(int(self._h0 + ev.globalPosition().y() - self._y0))

    def mouseReleaseEvent(self, ev):
        self._y0 = None


class TrackRow(QWidget):
    def __init__(self, editor: "EditorWindow", track: Track):
        super().__init__()
        self.track = track
        self.control = TrackControl(editor, track)
        self.wave = WaveView(editor, track)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(self.control)
        h.addWidget(self.wave, 1)
        v.addLayout(h)
        v.addWidget(_ResizeHandle(self))
        self.set_track_height(DEFAULT_TRACK_H + (40 if track.channels == 2 else 0))

    def set_track_height(self, hgt: int):
        hgt = max(70, min(800, hgt))
        self.control.setFixedHeight(hgt)
        self.wave.setFixedHeight(hgt)


class TracksPanel(QScrollArea):
    def __init__(self, editor: "EditorWindow"):
        super().__init__()
        self.editor = editor
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.container = _Container(editor)
        self.vbox = QVBoxLayout(self.container)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(0)
        self.vbox.addStretch(1)
        self.setWidget(self.container)
        self.rows: dict[int, TrackRow] = {}

    def sync(self):
        """Đồng bộ các hàng với danh sách track của dự án (giữ lại hàng cũ nếu track còn)."""
        tracks = self.editor.project.tracks
        ids = {t.id for t in tracks}
        for tid in list(self.rows):
            if tid not in ids:
                row = self.rows.pop(tid)
                self.vbox.removeWidget(row)
                row.deleteLater()
        for i, t in enumerate(tracks):
            row = self.rows.get(t.id)
            if row is None:
                row = TrackRow(self.editor, t)
                self.rows[t.id] = row
            self.vbox.removeWidget(row)
            self.vbox.insertWidget(i, row)
            row.show()
        self.refresh()

    def refresh(self):
        for row in self.rows.values():
            row.control.refresh()
            row.wave.update()
        self.container.update()

    def repaint_waves(self):
        for row in self.rows.values():
            row.wave.update()

    def wave_width(self) -> int:
        return max(100, self.viewport().width() - CONTROL_W)

    def track_at_global_y(self, gy: int) -> Track | None:
        for t in self.editor.project.tracks:
            row = self.rows.get(t.id)
            if row is None:
                continue
            top = row.mapToGlobal(row.rect().topLeft()).y()
            if top <= gy < top + row.height():
                return t
        return None

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.editor.on_view_resized()


class _Container(QWidget):
    def __init__(self, editor: "EditorWindow"):
        super().__init__()
        self.editor = editor
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), C_EMPTY)
        self.setPalette(pal)

    def paintEvent(self, ev):
        if not self.editor.project.tracks:
            p = QPainter(self)
            p.setPen(QColor(250, 250, 250))
            f = p.font()
            f.setPointSize(12)
            p.setFont(f)
            p.drawText(self.rect().adjusted(0, 60, 0, 0), Qt.AlignHCenter | Qt.AlignTop,
                       "Bấm ● (R) để ghi âm từ micro hoặc điện thoại\n"
                       "hoặc Tệp → Nhập âm thanh (Ctrl+Shift+I), hay kéo thả file vào đây")

    def mousePressEvent(self, ev):
        if not self.editor.is_recording:
            self.editor.project.select_none()


# ====================================================================== đồng hồ mức âm
class Meter(QWidget):
    def __init__(self, label: str, tooltip: str):
        super().__init__()
        self.label = label
        self.setToolTip(tooltip)
        self.setFixedSize(220, 30)
        self.levels = [0.0, 0.0]
        self.peaks = [0.0, 0.0]
        self.clipped = False

    def set_levels(self, l: float, r: float):
        for i, v in enumerate((l, r)):
            self.levels[i] = max(v, self.levels[i] * 0.8)
            self.peaks[i] = max(v, self.peaks[i] * 0.985)
            if v >= 0.999:
                self.clipped = True
        self.update()

    def reset(self):
        self.levels = [0.0, 0.0]
        self.peaks = [0.0, 0.0]
        self.clipped = False
        self.update()

    @staticmethod
    def _pos(v: float) -> float:
        if v <= 0:
            return 0.0
        db = 20 * math.log10(v)
        return max(0.0, min(1.0, (db + 60) / 60))   # thang -60..0 dB

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(236, 237, 240))
        p.setPen(QColor(60, 60, 60))
        f = p.font()
        f.setPointSize(8)
        p.setFont(f)
        p.drawText(QRectF(0, 0, 26, 30), Qt.AlignCenter, self.label)
        x0, w = 28, self.width() - 34
        grad = QLinearGradient(x0, 0, x0 + w, 0)
        grad.setColorAt(0.0, QColor(40, 180, 60))
        grad.setColorAt(0.75, QColor(230, 210, 40))
        grad.setColorAt(1.0, QColor(220, 40, 30))
        for i in range(2):
            y = 3 + i * 12
            p.fillRect(QRectF(x0, y, w, 10), QColor(255, 255, 255))
            p.fillRect(QRectF(x0, y, w * self._pos(self.levels[i]), 10), grad)
            px = x0 + w * self._pos(self.peaks[i])
            p.setPen(QColor(30, 30, 30))
            p.drawLine(QLineF(px, y, px, y + 10))
        p.fillRect(QRectF(x0 + w + 1, 3, 5, 22), QColor(220, 0, 0) if self.clipped else QColor(200, 200, 200))
        p.setPen(QColor(130, 130, 130))
        for db in (-48, -36, -24, -12, -6):
            x = x0 + w * (db + 60) / 60
            p.drawLine(QLineF(x, 26, x, 29))

    def mousePressEvent(self, ev):
        self.clipped = False
        self.update()
