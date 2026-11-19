"""Mô hình dự án: các track, vùng chọn, clipboard, undo/redo, trộn âm, lưu/mở dự án.

Quy ước: mảng âm thanh của track KHÔNG bao giờ bị sửa tại chỗ (trừ track đang ghi) — mỗi thao tác
tạo mảng mới, nhờ vậy ảnh chụp undo chỉ cần giữ tham chiếu, rất nhẹ.
"""
from __future__ import annotations

import io
import itertools
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from ..audio_io import resample

_ids = itertools.count(1)
MAX_UNDO = 50
PROJECT_EXT = ".wrproj"


def convert_channels(x: np.ndarray, channels: int) -> np.ndarray:
    if x.shape[1] == channels:
        return x
    if channels == 1:
        return x.mean(axis=1, keepdims=True).astype(np.float32)
    return np.repeat(x[:, :1], channels, axis=1) if x.shape[1] == 1 else x[:, :channels]


@dataclass(eq=False)
class Track:
    name: str
    data: np.ndarray                       # (n, ch) float32
    gain_db: float = 0.0
    pan: float = 0.0                       # -1 trái .. +1 phải
    mute: bool = False
    solo: bool = False
    selected: bool = False
    id: int = field(default_factory=lambda: next(_ids))
    # --- khi đang ghi: bộ đệm tăng dần, data là view data = _buf[:_len]
    recording: bool = False
    note: str = ""
    _buf: np.ndarray | None = None
    _len: int = 0
    version: int = 0                       # tăng mỗi khi dữ liệu đổi (để làm mới bộ nhớ đệm sóng âm)

    @property
    def channels(self) -> int:
        return self.data.shape[1]

    @property
    def length(self) -> int:
        return len(self.data)

    def set_data(self, data: np.ndarray) -> None:
        self.data = np.ascontiguousarray(data, dtype=np.float32)
        self.version += 1

    # ---- ghi trực tiếp
    def begin_recording(self, prefix: int) -> None:
        self.recording = True
        cap = max(prefix + 48000 * 10, 1 << 16)
        self._buf = np.zeros((cap, self.channels), np.float32)
        self._len = prefix
        self.data = self._buf[:self._len]
        self.version += 1

    def append(self, chunk: np.ndarray) -> None:
        chunk = convert_channels(chunk, self.channels)
        need = self._len + len(chunk)
        if need > len(self._buf):
            nb = np.zeros((max(need, len(self._buf) * 2), self.channels), np.float32)
            nb[:self._len] = self._buf[:self._len]
            self._buf = nb
        self._buf[self._len:need] = chunk
        self._len = need
        self.data = self._buf[:self._len]
        self.version += 1

    def end_recording(self) -> None:
        self.recording = False
        self.set_data(self._buf[:self._len].copy())
        self._buf = None


@dataclass
class _TrackState:
    track: Track
    name: str
    data: np.ndarray
    gain_db: float
    pan: float
    mute: bool
    solo: bool


@dataclass
class _Snapshot:
    description: str
    tracks: list[_TrackState]
    sel: tuple[int, int]


class Project:
    def __init__(self, rate: int = 48000):
        self.rate = rate
        self.tracks: list[Track] = []
        self.sel_start = 0
        self.sel_end = 0
        self.undo_stack: list[_Snapshot] = []
        self.redo_stack: list[_Snapshot] = []
        self.clipboard: list[np.ndarray] = []
        self.clipboard_rate = rate
        self.path: Path | None = None
        self.modified = False
        self.listeners: list[Callable[[str], None]] = []

    # ------------------------------------------------------------ thông báo
    def changed(self, what: str = "data") -> None:
        for cb in list(self.listeners):
            cb(what)

    # ------------------------------------------------------------ kích thước
    @property
    def length(self) -> int:
        return max((t.length for t in self.tracks), default=0)

    @property
    def duration(self) -> float:
        return self.length / self.rate

    def selected_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.selected]

    def target_tracks(self) -> list[Track]:
        """Track chịu tác động: các track được chọn, nếu không chọn track nào thì tất cả."""
        return self.selected_tracks() or list(self.tracks)

    @property
    def has_selection(self) -> bool:
        return self.sel_end > self.sel_start

    def set_selection(self, a: int, b: int) -> None:
        a, b = sorted((max(0, int(a)), max(0, int(b))))
        self.sel_start, self.sel_end = a, b
        self.changed("selection")

    def select_all(self) -> None:
        for t in self.tracks:
            t.selected = True
        self.sel_start, self.sel_end = 0, self.length
        self.changed("selection")

    def select_none(self) -> None:
        for t in self.tracks:
            t.selected = False
        self.sel_end = self.sel_start
        self.changed("selection")

    def range_for(self, t: Track) -> tuple[int, int]:
        """Vùng tác động trên track: vùng chọn, hoặc cả track nếu chưa chọn vùng."""
        if not self.has_selection:
            return 0, t.length
        return min(self.sel_start, t.length), min(self.sel_end, t.length)

    # ------------------------------------------------------------ undo / redo
    def _snapshot(self, description: str) -> _Snapshot:
        return _Snapshot(description,
                         [_TrackState(t, t.name, t.data, t.gain_db, t.pan, t.mute, t.solo)
                          for t in self.tracks if not t.recording],
                         (self.sel_start, self.sel_end))

    def push_undo(self, description: str) -> None:
        self.undo_stack.append(self._snapshot(description))
        if len(self.undo_stack) > MAX_UNDO:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        self.modified = True

    def _restore(self, snap: _Snapshot) -> None:
        recording = [t for t in self.tracks if t.recording]
        self.tracks = []
        for st in snap.tracks:
            t = st.track
            t.name, t.gain_db, t.pan, t.mute, t.solo = st.name, st.gain_db, st.pan, st.mute, st.solo
            if t.data is not st.data:
                t.set_data(st.data)
            self.tracks.append(t)
        self.tracks.extend(recording)
        self.sel_start, self.sel_end = snap.sel
        self.modified = True
        self.changed("tracks")

    def undo(self) -> str | None:
        if not self.undo_stack:
            return None
        snap = self.undo_stack.pop()
        self.redo_stack.append(self._snapshot(snap.description))
        self._restore(snap)
        return snap.description

    def redo(self) -> str | None:
        if not self.redo_stack:
            return None
        snap = self.redo_stack.pop()
        self.undo_stack.append(self._snapshot(snap.description))
        self._restore(snap)
        return snap.description

    # ------------------------------------------------------------ track
    def add_track(self, name: str, data: np.ndarray, rate: int | None = None, undo: bool = True,
                  select: bool = True) -> Track:
        if undo:
            self.push_undo(f"Thêm track {name}")
        if rate and rate != self.rate:
            data = resample(data, rate, self.rate)
        t = Track(name=name, data=np.ascontiguousarray(data, dtype=np.float32))
        if select:
            for o in self.tracks:
                o.selected = False
            t.selected = True
        self.tracks.append(t)
        self.modified = True
        self.changed("tracks")
        return t

    def remove_track(self, t: Track) -> None:
        if t in self.tracks:
            self.push_undo(f"Xoá track {t.name}")
            self.tracks.remove(t)
            self.changed("tracks")

    def move_track(self, t: Track, delta: int) -> None:
        i = self.tracks.index(t)
        j = max(0, min(len(self.tracks) - 1, i + delta))
        if i != j:
            self.push_undo("Di chuyển track")
            self.tracks.insert(j, self.tracks.pop(i))
            self.changed("tracks")

    def set_track_prop(self, t: Track, **props) -> None:
        """Đổi tên / gain / pan / mute / solo (gộp các thay đổi liên tiếp thành một bước undo)."""
        desc = "Thuộc tính track"
        if not (self.undo_stack and self.undo_stack[-1].description == desc + str(t.id)):
            self.push_undo(desc + str(t.id))
        for k, v in props.items():
            setattr(t, k, v)
        self.modified = True
        self.changed("props")

    # ------------------------------------------------------------ chỉnh sửa
    def _edit(self, description: str, fn: Callable[[Track, int, int], np.ndarray | None]) -> bool:
        targets = [t for t in self.target_tracks() if not t.recording]
        if not targets:
            return False
        self.push_undo(description)
        for t in targets:
            a, b = self.range_for(t)
            new = fn(t, a, b)
            if new is not None:
                t.set_data(new)
        self.changed("data")
        return True

    def apply_effect(self, description: str, effect: Callable[[np.ndarray], np.ndarray]) -> bool:
        def fn(t: Track, a: int, b: int):
            if b <= a:
                return None
            seg = effect(t.data[a:b]).astype(np.float32)
            seg = convert_channels(seg, t.channels)
            return np.concatenate([t.data[:a], seg, t.data[b:]])
        ok = self._edit(description, fn)
        return ok

    def delete(self) -> bool:
        if not self.has_selection:
            return False
        ok = self._edit("Xoá", lambda t, a, b: np.concatenate([t.data[:a], t.data[b:]]))
        self.sel_end = self.sel_start
        self.changed("selection")
        return ok

    def copy(self) -> bool:
        if not self.has_selection:
            return False
        self.clipboard = []
        for t in self.target_tracks():
            a, b = self.range_for(t)
            self.clipboard.append(t.data[a:b].copy())
        self.clipboard_rate = self.rate
        return bool(self.clipboard)

    def cut(self) -> bool:
        return self.copy() and self.delete()

    def silence(self) -> bool:
        if not self.has_selection:
            return False
        return self._edit("Làm im lặng", lambda t, a, b: np.concatenate(
            [t.data[:a], np.zeros((b - a, t.channels), np.float32), t.data[b:]]))

    def trim(self) -> bool:
        """Chỉ giữ lại vùng chọn (đưa về đầu track)."""
        if not self.has_selection:
            return False
        ok = self._edit("Giữ lại vùng chọn", lambda t, a, b: t.data[a:b].copy())
        self.sel_start, self.sel_end = 0, self.sel_end - self.sel_start
        self.changed("selection")
        return ok

    def paste(self) -> bool:
        if not self.clipboard:
            return False
        clips = [resample(c, self.clipboard_rate, self.rate) for c in self.clipboard]
        targets = [t for t in self.selected_tracks() if not t.recording]
        if not targets:
            self.push_undo("Dán")
            start = self.sel_start
            for i, c in enumerate(clips):
                data = np.concatenate([np.zeros((start, c.shape[1]), np.float32), c])
                self.tracks.append(Track(name=f"Đã dán {i + 1}", data=data, selected=True))
            self.sel_end = start + len(clips[0])
            self.changed("tracks")
            return True
        self.push_undo("Dán")
        a, b = self.sel_start, self.sel_end
        for i, t in enumerate(targets):
            c = convert_channels(clips[min(i, len(clips) - 1)], t.channels)
            pre = t.data[:min(a, t.length)]
            if len(pre) < a:  # dán sau điểm cuối → chèn im lặng
                pre = np.concatenate([pre, np.zeros((a - len(pre), t.channels), np.float32)])
            t.set_data(np.concatenate([pre, c, t.data[min(b, t.length):]]))
        self.sel_start, self.sel_end = a, a + len(clips[0])
        self.changed("data")
        return True

    def duplicate(self) -> bool:
        src = [t for t in self.target_tracks() if not t.recording]
        if not src:
            return False
        self.push_undo("Nhân bản")
        for t in src:
            a, b = self.range_for(t)
            data = np.concatenate([np.zeros((a, t.channels), np.float32), t.data[a:b]])
            self.tracks.append(Track(name=t.name + " (bản sao)", data=data, gain_db=t.gain_db, pan=t.pan))
        self.changed("tracks")
        return True

    def insert_generated(self, description: str, gen: Callable[[int], np.ndarray]) -> None:
        """Chèn âm thanh tạo ra (tone, im lặng...) tại con trỏ / thay vùng chọn. Không có track → tạo mới."""
        targets = [t for t in self.selected_tracks() if not t.recording]
        if not targets:
            self.push_undo(description)
            data = gen(1)
            pad = np.zeros((self.sel_start, 1), np.float32)
            self.tracks.append(Track(name=description, data=np.concatenate([pad, data]), selected=True))
            self.sel_end = self.sel_start + len(data)
            self.changed("tracks")
            return
        self.push_undo(description)
        n = 0
        for t in targets:
            a, b = min(self.sel_start, t.length), min(self.sel_end, t.length)
            seg = gen(t.channels)
            n = len(seg)
            pre = t.data[:a]
            if a < self.sel_start:
                pre = np.concatenate([pre, np.zeros((self.sel_start - a, t.channels), np.float32)])
            t.set_data(np.concatenate([pre, seg, t.data[b:]]))
        self.sel_end = self.sel_start + n
        self.changed("data")

    def mix_to_new_track(self) -> None:
        src = self.target_tracks()
        if not src:
            return
        data = self.mix(0, self.length, src)
        self.push_undo("Trộn thành track mới")
        self.tracks.append(Track(name="Bản trộn", data=data))
        self.changed("tracks")

    def stereo_to_mono(self) -> None:
        targets = [t for t in self.target_tracks() if t.channels == 2 and not t.recording]
        if targets:
            self.push_undo("Stereo → Mono")
            for t in targets:
                t.set_data(t.data.mean(axis=1, keepdims=True))
            self.changed("tracks")

    # ------------------------------------------------------------ trộn âm
    @staticmethod
    def track_gains(t: Track) -> tuple[float, float]:
        g = 10 ** (t.gain_db / 20)
        return g * min(1.0, 1.0 - t.pan), g * min(1.0, 1.0 + t.pan)

    def audible_tracks(self, tracks: list[Track] | None = None) -> list[Track]:
        tracks = self.tracks if tracks is None else tracks
        solo = [t for t in tracks if t.solo]
        return [t for t in (solo or tracks) if not t.mute]

    def mix_channels(self, tracks: list[Track] | None = None) -> int:
        aud = self.audible_tracks(tracks)
        return 2 if any(t.channels == 2 or t.pan != 0 for t in aud) else 1

    def mix(self, a: int, b: int, tracks: list[Track] | None = None, channels: int | None = None) -> np.ndarray:
        channels = channels or self.mix_channels(tracks)
        out = np.zeros((max(0, b - a), 2), np.float32)
        for t in self.audible_tracks(tracks):
            seg = t.data[a:b]
            if not len(seg):
                continue
            gl, gr = self.track_gains(t)
            out[:len(seg), 0] += seg[:, 0] * gl
            out[:len(seg), 1] += seg[:, -1] * gr
        return out if channels == 2 else out.mean(axis=1, keepdims=True)

    # ------------------------------------------------------------ lưu / mở dự án
    def save(self, path: Path) -> None:
        meta = {"version": 1, "rate": self.rate, "tracks": []}
        tmp = path.with_name(path.name + ".part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            for i, t in enumerate(self.tracks):
                if t.recording:
                    continue
                fname = f"track{i}.npy"
                bio = io.BytesIO()
                np.save(bio, t.data)
                z.writestr(fname, bio.getvalue())
                meta["tracks"].append({"name": t.name, "file": fname, "gain_db": t.gain_db, "pan": t.pan,
                                       "mute": t.mute, "solo": t.solo})
            z.writestr("project.json", json.dumps(meta, ensure_ascii=False, indent=1))
        tmp.replace(path)
        self.path = path
        self.modified = False

    @classmethod
    def load(cls, path: Path) -> "Project":
        with zipfile.ZipFile(path) as z:
            meta = json.loads(z.read("project.json"))
            p = cls(int(meta["rate"]))
            for tm in meta["tracks"]:
                data = np.load(io.BytesIO(z.read(tm["file"])))
                p.tracks.append(Track(name=tm["name"], data=data.astype(np.float32),
                                      gain_db=tm.get("gain_db", 0.0), pan=tm.get("pan", 0.0),
                                      mute=tm.get("mute", False), solo=tm.get("solo", False)))
        p.path = path
        return p
