"""Mô hình dự án: track → các đoạn (clip) đặt ở vị trí bất kỳ, vùng chọn, clipboard, undo/redo,
trộn âm, lưu/mở dự án.

Giống Audacity: mỗi track gồm nhiều clip, giữa các clip có thể có khoảng trống (im lặng).
Clip kéo được sang vị trí khác / track khác, tách (split) và gộp (join) được.

Quy ước: mảng âm thanh của clip KHÔNG bao giờ bị sửa tại chỗ (trừ clip đang ghi) — mỗi thao tác
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


def _f32(x: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(x, dtype=np.float32)


# ====================================================================== clip
@dataclass(eq=False)
class Clip:
    offset: int                     # vị trí bắt đầu (mẫu) trong track
    data: np.ndarray                # (n, ch) float32
    name: str = ""
    id: int = field(default_factory=lambda: next(_ids))
    version: int = 0
    recording: bool = False
    _buf: np.ndarray | None = None
    _len: int = 0

    @property
    def length(self) -> int:
        return len(self.data)

    @property
    def end(self) -> int:
        return self.offset + len(self.data)

    @property
    def channels(self) -> int:
        return self.data.shape[1]

    def set_data(self, data: np.ndarray) -> None:
        self.data = _f32(data)
        self.version += 1

    # ---- ghi trực tiếp (bộ đệm tăng dần, data = view)
    def begin_recording(self) -> None:
        self.recording = True
        self._buf = np.zeros((48000 * 10, self.channels), np.float32)
        self._len = 0
        self.data = self._buf[:0]
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


# ====================================================================== track
@dataclass(eq=False)
class Track:
    name: str
    channels: int
    clips: list[Clip] = field(default_factory=list)
    gain_db: float = 0.0
    pan: float = 0.0
    mute: bool = False
    solo: bool = False
    selected: bool = False
    note: str = ""
    id: int = field(default_factory=lambda: next(_ids))

    @classmethod
    def from_data(cls, name: str, data: np.ndarray, offset: int = 0, **kw) -> "Track":
        t = cls(name=name, channels=data.shape[1], **kw)
        if len(data):
            t.clips.append(Clip(offset, _f32(data), name=name))
        return t

    @property
    def recording(self) -> bool:
        return any(c.recording for c in self.clips)

    @property
    def length(self) -> int:
        return max((c.end for c in self.clips), default=0)

    @property
    def data(self) -> np.ndarray:
        """Toàn bộ track từ 0 (khoảng trống = im lặng)."""
        return self.render(0, self.length)

    def sort(self) -> None:
        self.clips.sort(key=lambda c: c.offset)

    def clip_at(self, pos: int) -> Clip | None:
        for c in self.clips:
            if c.offset <= pos < c.end:
                return c
        return None

    def overlapping(self, a: int, b: int) -> list[Clip]:
        return [c for c in self.clips if c.offset < b and c.end > a]

    def render(self, a: int, b: int) -> np.ndarray:
        out = np.zeros((max(0, b - a), self.channels), np.float32)
        for c in self.clips:
            lo, hi = max(a, c.offset), min(b, c.end)
            if hi > lo:
                out[lo - a:hi - a] += c.data[lo - c.offset:hi - c.offset]
        return out

    def free_range(self, clip: Clip, exclude_self: bool = True) -> tuple[int, int]:
        """Khoảng [min_offset, max_end] mà clip có thể nằm mà không đè clip khác (dùng khi kéo)."""
        lo, hi = 0, 1 << 62
        for c in self.clips:
            if exclude_self and c is clip:
                continue
            if c.end <= clip.offset:
                lo = max(lo, c.end)
            elif c.offset >= clip.offset:
                hi = min(hi, c.offset)
        return lo, hi

    def fits(self, offset: int, length: int, ignore: Clip | None = None) -> bool:
        return all(c is ignore or c.end <= offset or c.offset >= offset + length for c in self.clips)

    # ------------------------------------------------------------ thao tác cơ bản trên track
    def split_at(self, pos: int) -> bool:
        c = self.clip_at(pos)
        if c is None or pos <= c.offset or c.recording:
            return False
        k = pos - c.offset
        right = Clip(pos, c.data[k:], name=c.name)
        c.set_data(c.data[:k])
        self.clips.insert(self.clips.index(c) + 1, right)
        return True

    def delete_range(self, a: int, b: int, ripple: bool = True) -> None:
        """Xoá [a, b). ripple=True: các clip phía sau dồn sang trái; False: để lại khoảng trống."""
        n = b - a
        new: list[Clip] = []
        for c in self.clips:
            if c.end <= a:
                new.append(c)
            elif c.offset >= b:
                if ripple:
                    c.offset -= n
                new.append(c)
            else:
                left = c.data[:max(0, a - c.offset)]
                right = c.data[max(0, b - c.offset):]
                if ripple:
                    if len(left) + len(right):
                        c.set_data(np.concatenate([left, right]))
                        c.offset = min(c.offset, a)
                        new.append(c)
                else:
                    if len(left):
                        c.set_data(left)
                        new.append(c)
                    if len(right):
                        new.append(Clip(b, right, name=c.name))
        self.clips = new
        self.sort()

    def insert(self, pos: int, seg: np.ndarray) -> None:
        """Chèn đoạn vào vị trí pos, các clip phía sau dịch phải."""
        seg = convert_channels(_f32(seg), self.channels)
        n = len(seg)
        if not n:
            return
        target = next((c for c in self.clips if c.offset < pos < c.end), None) or \
            next((c for c in self.clips if c.end == pos), None) or \
            next((c for c in self.clips if c.offset == pos), None)
        for c in self.clips:
            if c is not target and c.offset >= pos:
                c.offset += n
        if target is None:
            self.clips.append(Clip(pos, seg))
        else:
            k = pos - target.offset
            target.set_data(np.concatenate([target.data[:k], seg, target.data[k:]]))
        self.sort()

    def write(self, a: int, seg: np.ndarray) -> None:
        """Ghi đè nội dung clip trong [a, a+len) (bỏ qua phần rơi vào khoảng trống)."""
        b = a + len(seg)
        for c in self.overlapping(a, b):
            lo, hi = max(a, c.offset), min(b, c.end)
            d = c.data.copy()
            d[lo - c.offset:hi - c.offset] = seg[lo - a:hi - a]
            c.set_data(d)

    def trim_to(self, a: int, b: int) -> None:
        new = []
        for c in self.clips:
            lo, hi = max(a, c.offset), min(b, c.end)
            if hi > lo:
                c.set_data(c.data[lo - c.offset:hi - c.offset])
                c.offset = lo
                new.append(c)
        self.clips = new

    def join(self, a: int, b: int) -> bool:
        group = self.overlapping(a, b) if b > a else list(self.clips)
        if len(group) < 2:
            return False
        start, end = min(c.offset for c in group), max(c.end for c in group)
        merged = Clip(start, np.zeros((0, self.channels), np.float32), name=group[0].name)
        data = np.zeros((end - start, self.channels), np.float32)
        for c in group:
            data[c.offset - start:c.end - start] += c.data
        merged.set_data(data)
        self.clips = [c for c in self.clips if c not in group] + [merged]
        self.sort()
        return True


# ====================================================================== undo
@dataclass
class _ClipState:
    clip: Clip
    offset: int
    data: np.ndarray
    name: str


@dataclass
class _TrackState:
    track: Track
    name: str
    gain_db: float
    pan: float
    mute: bool
    solo: bool
    clips: list[_ClipState]


@dataclass
class _Snapshot:
    description: str
    tracks: list[_TrackState]
    sel: tuple[int, int]


# ====================================================================== dự án
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

    def changed(self, what: str = "data") -> None:
        for cb in list(self.listeners):
            cb(what)

    # ------------------------------------------------------------ kích thước / chọn
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
        return [t for t in (self.selected_tracks() or self.tracks) if not t.recording]

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
        return self.sel_start, self.sel_end

    # ------------------------------------------------------------ undo / redo
    def snapshot(self, description: str) -> _Snapshot:
        return _Snapshot(description, [
            _TrackState(t, t.name, t.gain_db, t.pan, t.mute, t.solo,
                        [_ClipState(c, c.offset, c.data, c.name) for c in t.clips])
            for t in self.tracks if not t.recording], (self.sel_start, self.sel_end))

    def push_snapshot(self, snap: _Snapshot) -> None:
        self.undo_stack.append(snap)
        if len(self.undo_stack) > MAX_UNDO:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        self.modified = True

    def push_undo(self, description: str) -> None:
        self.push_snapshot(self.snapshot(description))

    def _restore(self, snap: _Snapshot) -> None:
        recording = [t for t in self.tracks if t.recording]
        self.tracks = []
        for st in snap.tracks:
            t = st.track
            t.name, t.gain_db, t.pan, t.mute, t.solo = st.name, st.gain_db, st.pan, st.mute, st.solo
            t.clips = []
            for cs in st.clips:
                c = cs.clip
                c.offset, c.name = cs.offset, cs.name
                if c.data is not cs.data:
                    c.set_data(cs.data)
                t.clips.append(c)
            self.tracks.append(t)
        self.tracks.extend(recording)
        self.sel_start, self.sel_end = snap.sel
        self.modified = True
        self.changed("tracks")

    def undo(self) -> str | None:
        if not self.undo_stack:
            return None
        snap = self.undo_stack.pop()
        self.redo_stack.append(self.snapshot(snap.description))
        self._restore(snap)
        return snap.description

    def redo(self) -> str | None:
        if not self.redo_stack:
            return None
        snap = self.redo_stack.pop()
        self.undo_stack.append(self.snapshot(snap.description))
        self._restore(snap)
        return snap.description

    # ------------------------------------------------------------ track
    def add_track(self, name: str, data: np.ndarray, rate: int | None = None, undo: bool = True,
                  select: bool = True, offset: int = 0) -> Track:
        if undo:
            self.push_undo(f"Thêm track {name}")
        if rate and rate != self.rate:
            data = resample(data, rate, self.rate)
        t = Track.from_data(name, _f32(data), offset=offset)
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
        desc = "Thuộc tính track" + str(t.id)
        if not (self.undo_stack and self.undo_stack[-1].description == desc):
            self.push_undo(desc)
        for k, v in props.items():
            setattr(t, k, v)
        self.modified = True
        self.changed("props")

    # ------------------------------------------------------------ chỉnh sửa theo vùng chọn
    def _edit(self, description: str, fn: Callable[[Track, int, int], None]) -> bool:
        targets = self.target_tracks()
        if not targets:
            return False
        self.push_undo(description)
        for t in targets:
            a, b = self.range_for(t)
            fn(t, a, b)
        self.changed("data")
        return True

    def apply_effect(self, description: str, effect: Callable[[np.ndarray], np.ndarray]) -> bool:
        def fn(t: Track, a: int, b: int):
            if b <= a:
                return
            group = t.overlapping(a, b)
            if not group:
                return
            if len(group) == 1:
                self._effect_on_clip(t, group[0], a, b, effect)
                return
            seg = t.render(a, b)
            out = convert_channels(_f32(effect(seg)), t.channels)
            if len(out) == len(seg):
                t.write(a, out)          # hiệu ứng giữ độ dài → áp cho cả vùng liền mạch qua các clip
            else:
                for c in sorted(group, key=lambda c: -c.offset):   # đổi độ dài → áp từng clip
                    self._effect_on_clip(t, c, a, b, effect)
        return self._edit(description, fn)

    @staticmethod
    def _effect_on_clip(t: Track, c: Clip, a: int, b: int, effect) -> None:
        lo, hi = max(a, c.offset) - c.offset, min(b, c.end) - c.offset
        old_end = c.end
        seg = convert_channels(_f32(effect(c.data[lo:hi])), t.channels)
        c.set_data(np.concatenate([c.data[:lo], seg, c.data[hi:]]))
        delta = c.end - old_end
        if delta:  # clip dài/ngắn đi → dịch các clip phía sau để không đè nhau
            for o in t.clips:
                if o is not c and o.offset >= old_end:
                    o.offset = max(0, o.offset + delta)

    def delete(self) -> bool:
        if not self.has_selection:
            return False
        ok = self._edit("Xoá", lambda t, a, b: t.delete_range(a, b, ripple=True))
        self.sel_end = self.sel_start
        self.changed("selection")
        return ok

    def copy(self) -> bool:
        if not self.has_selection:
            return False
        self.clipboard = [t.render(*self.range_for(t)) for t in self.target_tracks()]
        self.clipboard_rate = self.rate
        return bool(self.clipboard)

    def cut(self) -> bool:
        return self.copy() and self.delete()

    def silence(self) -> bool:
        if not self.has_selection:
            return False
        return self._edit("Làm im lặng", lambda t, a, b: t.write(a, np.zeros((b - a, t.channels), np.float32)))

    def trim(self) -> bool:
        """Chỉ giữ lại phần trong vùng chọn (giữ nguyên vị trí như Audacity)."""
        if not self.has_selection:
            return False
        return self._edit("Giữ lại vùng chọn", lambda t, a, b: t.trim_to(a, b))

    def paste(self) -> bool:
        if not self.clipboard:
            return False
        clips = [resample(c, self.clipboard_rate, self.rate) for c in self.clipboard]
        targets = [t for t in self.selected_tracks() if not t.recording]
        self.push_undo("Dán")
        a, b = self.sel_start, self.sel_end
        if not targets:
            for i, c in enumerate(clips):
                self.tracks.append(Track.from_data(f"Đã dán {i + 1}", c, offset=a, selected=True))
            self.sel_end = a + len(clips[0])
            self.changed("tracks")
            return True
        for i, t in enumerate(targets):
            if b > a:
                t.delete_range(a, b, ripple=True)
            t.insert(a, clips[min(i, len(clips) - 1)])
        self.sel_start, self.sel_end = a, a + len(clips[0])
        self.changed("data")
        return True

    def duplicate(self) -> bool:
        src = self.target_tracks()
        if not src:
            return False
        self.push_undo("Nhân bản")
        for t in src:
            a, b = self.range_for(t)
            nt = Track(name=t.name + " (bản sao)", channels=t.channels, gain_db=t.gain_db, pan=t.pan)
            for c in t.overlapping(a, b):
                lo, hi = max(a, c.offset), min(b, c.end)
                nt.clips.append(Clip(lo, c.data[lo - c.offset:hi - c.offset], name=c.name))
            self.tracks.append(nt)
        self.changed("tracks")
        return True

    def insert_generated(self, description: str, gen: Callable[[int], np.ndarray]) -> None:
        """Chèn âm thanh tạo ra tại con trỏ / thay vùng chọn. Không có track nào chọn → tạo track mới."""
        targets = [t for t in self.selected_tracks() if not t.recording]
        self.push_undo(description)
        a, b = self.sel_start, self.sel_end
        if not targets:
            data = gen(1)
            self.tracks.append(Track.from_data(description, data, offset=a, selected=True))
            self.sel_end = a + len(data)
            self.changed("tracks")
            return
        n = 0
        for t in targets:
            seg = gen(t.channels)
            n = len(seg)
            if b > a:
                t.delete_range(a, b, ripple=True)
            t.insert(a, seg)
        self.sel_end = a + n
        self.changed("data")

    # ------------------------------------------------------------ split / join / kéo clip
    def split(self) -> bool:
        """Tách clip tại con trỏ, hoặc tại 2 mép vùng chọn."""
        points = [self.sel_start, self.sel_end] if self.has_selection else [self.sel_start]
        targets = self.target_tracks()
        if not any(t.clip_at(p) and t.clip_at(p).offset < p for t in targets for p in points):
            return False
        self.push_undo("Tách (Split)")
        for t in targets:
            for p in points:
                t.split_at(p)
        self.changed("data")
        return True

    def split_delete(self) -> bool:
        """Xoá vùng chọn nhưng KHÔNG dồn phần sau lại (để lại khoảng trống)."""
        if not self.has_selection:
            return False
        return self._edit("Tách và xoá", lambda t, a, b: t.delete_range(a, b, ripple=False))

    def split_new(self) -> bool:
        """Chuyển vùng chọn sang track mới (giữ nguyên vị trí)."""
        if not self.has_selection:
            return False
        src = self.target_tracks()
        if not src:
            return False
        self.push_undo("Tách ra track mới")
        a, b = self.sel_start, self.sel_end
        for t in src:
            nt = Track(name=t.name + " (tách)", channels=t.channels, gain_db=t.gain_db, pan=t.pan)
            for c in t.overlapping(a, b):
                lo, hi = max(a, c.offset), min(b, c.end)
                nt.clips.append(Clip(lo, c.data[lo - c.offset:hi - c.offset], name=c.name))
            t.delete_range(a, b, ripple=False)
            if nt.clips:
                self.tracks.insert(self.tracks.index(t) + 1, nt)
        self.changed("tracks")
        return True

    def join(self) -> bool:
        """Gộp các clip trong vùng chọn (hoặc cả track) thành một, khoảng trống thành im lặng."""
        targets = self.target_tracks()
        a, b = (self.sel_start, self.sel_end) if self.has_selection else (0, 0)
        if not any(len(t.overlapping(a, b) if b > a else t.clips) > 1 for t in targets):
            return False
        self.push_undo("Gộp (Join)")
        for t in targets:
            t.join(a, b)
        self.changed("data")
        return True

    def split_at_silences(self, threshold_db: float = -40.0, min_silence_s: float = 0.5,
                          keep_s: float = 0.1) -> int:
        """Tách clip tại các khoảng lặng dài và bỏ phần lặng (như 'Detach at Silences'). Trả về số clip mới."""
        thr = 10 ** (threshold_db / 20)
        win = max(1, int(self.rate * 0.01))
        min_win = max(1, int(min_silence_s / 0.01))
        keep = int(keep_s * self.rate)
        targets = self.target_tracks()
        cuts: dict[Track, list[tuple[int, int]]] = {}
        for t in targets:
            a, b = self.range_for(t)
            for c in t.overlapping(a, b):
                lo, hi = max(a, c.offset) - c.offset, min(b, c.end) - c.offset
                d = np.abs(c.data[lo:hi]).max(axis=1)
                nwin = len(d) // win
                if nwin < min_win:
                    continue
                env = d[:nwin * win].reshape(nwin, win).max(axis=1)
                silent = np.concatenate([[False], env < thr, [False]])
                edges = np.flatnonzero(np.diff(silent.astype(np.int8)))
                for s, e in zip(edges[::2], edges[1::2]):
                    if e - s >= min_win:
                        s_abs = c.offset + lo + s * win + (keep if s > 0 else 0)
                        e_abs = c.offset + lo + e * win - (keep if e < nwin else 0)
                        if e_abs > s_abs:
                            cuts.setdefault(t, []).append((s_abs, e_abs))
        if not cuts:
            return 0
        self.push_undo("Tách tại khoảng lặng")
        before = sum(len(t.clips) for t in cuts)
        for t, ranges in cuts.items():
            for s, e in sorted(ranges, reverse=True):
                t.delete_range(s, e, ripple=False)
        self.changed("data")
        return sum(len(t.clips) for t in cuts) - before

    def move_clip(self, clip: Clip, src: Track, dst: Track, offset: int) -> bool:
        """Đặt clip vào track dst tại offset (không được đè clip khác). Không tự đẩy undo."""
        offset = max(0, int(offset))
        if dst.channels != clip.channels or dst.recording:
            return False
        if not dst.fits(offset, clip.length, ignore=clip):
            return False
        if src is not dst:
            src.clips.remove(clip)
            dst.clips.append(clip)
        clip.offset = offset
        dst.sort()
        return True

    def remove_clip(self, t: Track, clip: Clip) -> None:
        if clip in t.clips:
            self.push_undo("Xoá đoạn")
            t.clips.remove(clip)
            self.changed("data")

    def mix_to_new_track(self) -> None:
        src = self.target_tracks()
        if not src:
            return
        data = self.mix(0, self.length, src)
        self.push_undo("Trộn thành track mới")
        self.tracks.append(Track.from_data("Bản trộn", data))
        self.changed("tracks")

    def stereo_to_mono(self) -> None:
        targets = [t for t in self.target_tracks() if t.channels == 2]
        if targets:
            self.push_undo("Stereo → Mono")
            for t in targets:
                t.channels = 1
                for c in t.clips:
                    c.set_data(c.data.mean(axis=1, keepdims=True))
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
            if not t.overlapping(a, b):
                continue
            seg = t.render(a, b)
            gl, gr = self.track_gains(t)
            out[:, 0] += seg[:, 0] * gl
            out[:, 1] += seg[:, -1] * gr
        return out if channels == 2 else out.mean(axis=1, keepdims=True)

    # ------------------------------------------------------------ lưu / mở dự án
    def save(self, path: Path) -> None:
        meta = {"version": 2, "rate": self.rate, "tracks": []}
        tmp = path.with_name(path.name + ".part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            for i, t in enumerate(self.tracks):
                if t.recording:
                    continue
                tm = {"name": t.name, "channels": t.channels, "gain_db": t.gain_db, "pan": t.pan,
                      "mute": t.mute, "solo": t.solo, "clips": []}
                for j, c in enumerate(t.clips):
                    fname = f"t{i}_c{j}.npy"
                    bio = io.BytesIO()
                    np.save(bio, c.data)
                    z.writestr(fname, bio.getvalue())
                    tm["clips"].append({"file": fname, "offset": c.offset, "name": c.name})
                meta["tracks"].append(tm)
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
                if "clips" not in tm:  # định dạng v1: mỗi track một mảng
                    data = np.load(io.BytesIO(z.read(tm["file"]))).astype(np.float32)
                    clips_meta = [(0, data, tm["name"])]
                    channels = data.shape[1]
                else:
                    clips_meta = [(cm["offset"], np.load(io.BytesIO(z.read(cm["file"]))).astype(np.float32),
                                   cm.get("name", "")) for cm in tm["clips"]]
                    channels = tm["channels"]
                t = Track(name=tm["name"], channels=channels, gain_db=tm.get("gain_db", 0.0),
                          pan=tm.get("pan", 0.0), mute=tm.get("mute", False), solo=tm.get("solo", False))
                t.clips = [Clip(int(off), d, name=n) for off, d, n in clips_meta]
                p.tracks.append(t)
        p.path = path
        return p
