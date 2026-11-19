"""Phát âm thanh của dự án qua loa (sounddevice), trộn các track theo thời gian thực."""
from __future__ import annotations

import threading

import numpy as np

from .model import Project


class Player:
    def __init__(self, project: Project):
        self.project = project
        self._stream = None
        self._lock = threading.Lock()
        self.position = 0          # mẫu đang phát (theo dự án)
        self.start_pos = 0
        self.end_pos = 0
        self.loop = False
        self.paused = False
        self.playing = False
        self.level = (0.0, 0.0)
        self.device = None         # None = thiết bị mặc định
        self.error: str | None = None

    def set_project(self, project: Project) -> None:
        self.stop()
        self.project = project

    # ------------------------------------------------------------ điều khiển
    def play(self, start: int, end: int | None = None, loop: bool = False) -> bool:
        self.stop()
        p = self.project
        end = p.length if end is None or end <= start else end
        if start >= end:
            return False
        self.start_pos, self.end_pos, self.position = start, end, start
        self.loop = loop
        self.paused = False
        self._tracks = list(p.tracks)
        try:
            import sounddevice as sd
            self._stream = sd.OutputStream(samplerate=p.rate, channels=2, dtype="float32",
                                           callback=self._callback, finished_callback=self._finished,
                                           device=self.device, blocksize=1024, latency="low")
            self._stream.start()
        except Exception as e:
            self.error = str(e)
            self._stream = None
            return False
        self.playing = True
        return True

    def pause(self, paused: bool | None = None) -> None:
        if not self.playing:
            return
        self.paused = (not self.paused) if paused is None else paused

    def stop(self) -> None:
        s, self._stream = self._stream, None
        self.playing = False
        self.paused = False
        self.level = (0.0, 0.0)
        if s is not None:
            try:
                s.abort()
                s.close()
            except Exception:
                pass

    # ------------------------------------------------------------ luồng âm thanh
    def render(self, frames: int) -> np.ndarray:
        """Lấy `frames` mẫu stereo tiếp theo (cũng được dùng trong test)."""
        out = np.zeros((frames, 2), np.float32)
        if self.paused:
            return out
        filled = 0
        while filled < frames:
            a = self.position
            b = min(self.end_pos, a + frames - filled)
            if b <= a:
                if self.loop and self.end_pos > self.start_pos:
                    self.position = self.start_pos
                    continue
                break
            out[filled:filled + (b - a)] = self.project.mix(a, b, self._tracks, channels=2)
            filled += b - a
            self.position = b
        if len(out):
            self.level = (float(np.abs(out[:, 0]).max()), float(np.abs(out[:, 1]).max()))
        return out

    def _callback(self, outdata, frames, time_info, status):
        import sounddevice as sd
        data = self.render(frames)
        np.clip(data, -1.0, 1.0, out=data)
        outdata[:] = data
        if not self.paused and self.position >= self.end_pos and not self.loop:
            raise sd.CallbackStop

    def _finished(self):
        self.playing = False
        self.level = (0.0, 0.0)
