"""Các widget vẽ: thước thời gian, track (bảng điều khiển + sóng âm), đồng hồ mức âm."""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QInputDialog, QLabel, QMenu, QScrollArea, QSizePolicy,
                               QSlider, QToolButton, QVBoxLayout, QWidget)

from .model import Clip, Track
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
HEADER_H = 16
C_HEADER = QColor(170, 186, 228)
C_HEADER_SEL = QColor(120, 142, 210)
C_HEADER_DRAG = QColor(90, 170, 110)
C_CLIP_EDGE = QColor(80, 90, 120)


class WaveView(QWidget):
    def __init__(self, editor: "EditorWindow", track: Track):
        super().__init__()
        self.editor = editor
        self.track = track
        self.summaries: dict[int, Summary] = {}
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.IBeamCursor)
        self._sel_anchor: int | None = None
        self._drag: dict | None = None

    def sample_at(self, x: float) -> int:
        return max(0, int(round(self.editor.h0 + x * self.editor.spp)))

    def x_of(self, sample: float) -> float:
        return (sample - self.editor.h0) / self.editor.spp

    def header_clip(self, pos) -> Clip | None:
        if pos.y() > HEADER_H:
            return None
        return self.track.clip_at(int(self.editor.h0 + pos.x() * self.editor.spp))

    # ------------------------------------------------------------ vẽ
    def paintEvent(self, ev):
        ed = self.editor
        t = self.track
        proj = ed.project
        p = QPainter(self)
        w, h = self.width(), self.height()
        sel = t.selected and proj.has_selection
        sx1 = self.x_of(proj.sel_start) if sel else 0
        sx2 = self.x_of(proj.sel_end) if sel else 0

        p.fillRect(0, 0, w, h, C_BEYOND)
        if sel:
            p.fillRect(QRectF(sx1, 0, sx2 - sx1, h), C_BEYOND_SEL)

        body_top = HEADER_H
        body_h = h - HEADER_H
        dragging = self._drag["clip"] if self._drag else None
        alive = set()
        for c in t.clips:
            x1, x2 = self.x_of(c.offset), self.x_of(c.end)
            if x2 < 0 or x1 > w:
                continue
            alive.add(c.id)
            cx1, cx2 = max(0.0, x1), min(float(w), x2)
            # thân clip
            p.fillRect(QRectF(cx1, body_top, cx2 - cx1, body_h), C_BG)
            if sel and sx2 > cx1 and sx1 < cx2:
                p.fillRect(QRectF(max(sx1, cx1), body_top, min(sx2, cx2) - max(sx1, cx1), body_h), C_BG_SEL)
            # thanh tiêu đề (kéo để di chuyển)
            hc = C_HEADER_DRAG if c is dragging else (C_HEADER_SEL if t.selected else C_HEADER)
            p.fillRect(QRectF(cx1, 0, cx2 - cx1, HEADER_H), hc)
            p.setPen(QColor(30, 30, 60))
            f = QFont(p.font())
            f.setPointSize(8)
            p.setFont(f)
            if cx2 - cx1 > 30:
                p.drawText(QRectF(cx1 + 4, 0, cx2 - cx1 - 8, HEADER_H), Qt.AlignVCenter | Qt.AlignLeft,
                           c.name or t.name)
            # sóng âm
            summ = self.summaries.get(c.id)
            if summ is None:
                summ = self.summaries[c.id] = Summary()
            summ.update(c)
            p.save()
            p.setClipRect(QRectF(cx1, body_top, cx2 - cx1, body_h))
            chs = t.channels
            lane_h = body_h / chs
            for ch in range(chs):
                top = body_top + ch * lane_h
                mid = top + lane_h / 2
                half = lane_h / 2 - 2
                p.setPen(QColor(200, 200, 215))
                p.drawLine(QLineF(cx1, mid, cx2, mid))
                self._draw_channel(p, c, summ, ch, mid, half, w)
                if ch > 0:
                    p.setPen(QColor(150, 150, 165))
                    p.drawLine(QLineF(cx1, top, cx2, top))
            p.restore()
            # viền clip
            p.setPen(QPen(C_CLIP_EDGE, 1))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(x1, 0, x2 - x1 - 1, h - 2))
        for k in list(self.summaries):
            if k not in alive and not any(c.id == k for c in t.clips):
                del self.summaries[k]

        # thang biên độ
        p.setPen(QColor(110, 110, 125))
        f = QFont(p.font())
        f.setPointSize(7)
        p.setFont(f)
        lane_h = body_h / t.channels
        for ch in range(t.channels):
            top = body_top + ch * lane_h
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
            p.drawText(QPointF(30, HEADER_H + 14), "● ĐANG GHI" if t.recording else t.note)
        p.setPen(QColor(110, 110, 120))
        p.drawLine(0, h - 1, w, h - 1)

    def _draw_channel(self, p: QPainter, clip: Clip, summ: Summary, ch: int, mid: float, half: float, w: int):
        ed = self.editor
        data = clip.data
        if not len(data):
            return
        spp = ed.spp
        h0 = ed.h0 - clip.offset            # toạ độ trong clip
        if spp < 1.0:  # phóng to sát: vẽ từng mẫu
            first = max(0, int(h0) - 1)
            last = min(len(data), int(h0 + w * spp) + 2)
            if last <= first:
                return
            ys = mid - np.clip(data[first:last, ch], -1, 1) * half
            xs = (np.arange(first, last) - h0) / spp
            p.setPen(QPen(C_WAVE, 1))
            p.drawPolyline(QPolygonF([QPointF(float(x), float(y)) for x, y in zip(xs, ys)]))
            if spp < 0.2:
                p.setBrush(C_WAVE)
                for x, y in zip(xs, ys):
                    p.drawEllipse(QPointF(float(x), float(y)), 2.5, 2.5)
            return
        r = summ.columns(data, ch, h0, spp, w)
        if r is None:
            return
        xs, mn, mx, rms = r
        y_top = mid - np.clip(mx, -1, 1) * half
        y_bot = mid - np.clip(mn, -1, 1) * half
        y_bot = np.maximum(y_bot, y_top + 1)
        p.setPen(QPen(C_WAVE, 1))
        p.drawLines([QLineF(float(x) + .5, float(a), float(x) + .5, float(b)) for x, a, b in zip(xs, y_top, y_bot)])
        rr = np.minimum(rms, np.minimum(mx, -mn).clip(min=0))
        mask = rr * half >= 1
        if mask.any():
            p.setPen(QPen(C_RMS, 1))
            p.drawLines([QLineF(float(x) + .5, float(mid - r_ * half), float(x) + .5, float(mid + r_ * half))
                         for x, r_ in zip(xs[mask], rr[mask])])
        clip_px = (mx >= 0.999) | (mn <= -0.999)
        if clip_px.any():
            p.setPen(QPen(C_CLIP, 1))
            p.drawLines([QLineF(float(x) + .5, mid - half, float(x) + .5, mid + half) for x in xs[clip_px]])

    # ------------------------------------------------------------ chuột
    def mousePressEvent(self, ev):
        ed = self.editor
        if ed.is_recording:
            return
        pos = ev.position()
        if ev.button() == Qt.RightButton:
            self._context_menu(ev)
            return
        if ev.button() != Qt.LeftButton:
            return
        clip = self.header_clip(pos)
        if clip is not None:   # bắt đầu kéo clip (Time Shift)
            ed.player.stop()
            self._drag = {"clip": clip, "orig": clip.offset, "gx": ev.globalPosition().x(),
                          "snap": ed.project.snapshot("Di chuyển đoạn"), "moved": False}
            for t in ed.project.tracks:
                t.selected = t is self.track
            ed.project.changed("selection")
            self.setCursor(Qt.ClosedHandCursor)
            return
        s = self.sample_at(pos.x())
        proj = ed.project
        if ev.modifiers() & Qt.ShiftModifier:
            anchor = proj.sel_start if abs(s - proj.sel_end) < abs(s - proj.sel_start) else proj.sel_end
            self._sel_anchor = anchor
            self.track.selected = True
            proj.set_selection(anchor, s)
        else:
            if ev.modifiers() & Qt.ControlModifier:
                self.track.selected = not self.track.selected
            else:
                for t in proj.tracks:
                    t.selected = t is self.track
            self._sel_anchor = s
            self._anchor_track = self.track
            proj.set_selection(s, s)
        ed.on_cursor_moved()

    def mouseMoveEvent(self, ev):
        ed = self.editor
        pos = ev.position()
        if not (ev.buttons() & Qt.LeftButton):
            self.setCursor(Qt.OpenHandCursor if self.header_clip(pos) else Qt.IBeamCursor)
            return
        if self._drag is not None:
            self._drag_to(ev)
            return
        if self._sel_anchor is None:
            return
        x = pos.x()
        if x > self.width():  # tự cuộn khi kéo ra ngoài
            ed.scroll_by_pixels(min(40, x - self.width()))
        elif x < 0:
            ed.scroll_by_pixels(max(-40, x))
        ed.project.set_selection(self._sel_anchor, self.sample_at(x))
        gy = self.mapToGlobal(pos.toPoint()).y()
        ed.select_tracks_between(getattr(self, "_anchor_track", self.track), gy)

    def _drag_to(self, ev):
        ed = self.editor
        d = self._drag
        clip = d["clip"]
        proj = ed.project
        src = next((t for t in proj.tracks if clip in t.clips), None)
        if src is None:
            return
        new_off = d["orig"] + (ev.globalPosition().x() - d["gx"]) * ed.spp
        # hít (snap) vào mép các clip khác, đầu dự án và vùng chọn
        tol = 8 * ed.spp
        edges = [0, proj.sel_start, proj.sel_end]
        for t in proj.tracks:
            for c in t.clips:
                if c is not clip:
                    edges += [c.offset, c.end]
        best = None
        for e in edges:
            for cand in (e, e - clip.length):
                dist = abs(new_off - cand)
                if dist < tol and (best is None or dist < best[0]):
                    best = (dist, cand)
        if best:
            new_off = best[1]
        new_off = max(0, int(round(new_off)))
        gy = int(ev.globalPosition().y())
        dst = ed.panel.track_at_global_y(gy) or src
        moved = proj.move_clip(clip, src, dst, new_off) if dst is not src else False
        if not moved:
            lo, hi = src.free_range(clip)   # cùng track: không vượt qua clip bên cạnh
            new_off = min(max(new_off, lo), max(lo, hi - clip.length))
            moved = proj.move_clip(clip, src, src, new_off)
        if moved:
            d["moved"] = d["moved"] or clip.offset != d["orig"] or dst is not src
            if dst is not src:
                for t in proj.tracks:
                    t.selected = t is dst
            ed.panel.repaint_waves()
            ed.ruler.update()

    def mouseReleaseEvent(self, ev):
        if self._drag is not None:
            d, self._drag = self._drag, None
            if d["moved"]:
                self.editor.project.push_snapshot(d["snap"])
                self.editor.project.changed("data")
                self.editor.status("Đã di chuyển đoạn (Ctrl+Z để hoàn tác)")
            self.setCursor(Qt.OpenHandCursor)
            self.editor.panel.repaint_waves()
        self._sel_anchor = None

    def mouseDoubleClickEvent(self, ev):
        proj = self.editor.project
        c = self.track.clip_at(self.sample_at(ev.position().x()))
        for t in proj.tracks:
            t.selected = t is self.track
        if c is not None:
            proj.set_selection(c.offset, c.end)     # nhấp đúp = chọn cả đoạn
        else:
            proj.set_selection(0, self.track.length)

    def _context_menu(self, ev):
        ed = self.editor
        proj = ed.project
        s = self.sample_at(ev.position().x())
        clip = self.track.clip_at(s)
        menu = QMenu(self)

        def only_this():
            for t in proj.tracks:
                t.selected = t is self.track

        def split_here():
            only_this()
            proj.set_selection(s, s)
            ed.do_edit(proj.split, "Tách")

        def select_clip():
            only_this()
            proj.set_selection(clip.offset, clip.end)

        def rename_clip():
            name, ok = QInputDialog.getText(self, "Đổi tên đoạn", "Tên:", text=clip.name or self.track.name)
            if ok:
                proj.push_undo("Đổi tên đoạn")
                clip.name = name.strip()
                proj.changed("data")

        def join_all():
            only_this()
            proj.set_selection(s, s)
            ed.do_edit(proj.join, "Gộp")

        if clip is not None and clip.offset < s:
            menu.addAction("Tách tại đây (Split)", split_here)
        if clip is not None:
            menu.addAction("Chọn cả đoạn", select_clip)
            menu.addAction("Đổi tên đoạn...", rename_clip)
            menu.addAction("Xoá đoạn này", lambda: proj.remove_clip(self.track, clip))
        if len(self.track.clips) > 1:
            menu.addAction("Gộp mọi đoạn của track (Join)", join_all)
        menu.addSeparator()
        menu.addAction("Tách tại khoảng lặng...", lambda: (only_this(), ed.split_at_silences()))
        menu.exec(ev.globalPosition().toPoint())

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
                          f"{len(t.clips)} đoạn · {fmt_time(t.length / self.editor.project.rate, ms=False)}")
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
