"""Video qua FFmpeg (đi kèm gói imageio-ffmpeg): dò thông tin, tách âm thanh, khung hình xem trước,
ảnh thu nhỏ cho dòng thời gian, và xuất video với âm thanh đã chỉnh sửa."""
from __future__ import annotations

import math
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".3gp", ".mts", ".ts"}
VIDEO_FILTER = "Video (" + " ".join("*" + e for e in sorted(VIDEO_EXTS)) + ");;Tất cả (*.*)"
EXPORT_FORMATS = {".mp4": "MP4 (*.mp4)", ".mov": "MOV (*.mov)", ".mkv": "MKV (*.mkv)"}

_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0   # không hiện cửa sổ console trên Windows


class VideoError(Exception):
    pass


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
    raise VideoError("Không tìm thấy FFmpeg")


def _popen(args: list[str], **kw) -> subprocess.Popen:
    return subprocess.Popen([ffmpeg_exe(), "-hide_banner", "-nostdin", *args], creationflags=_NO_WINDOW,
                            stdin=subprocess.DEVNULL, **kw)


def _run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", *args], creationflags=_NO_WINDOW,
                          stdin=subprocess.DEVNULL, capture_output=True, **kw)


def is_video(path: Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTS


@dataclass
class VideoInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    audio_channels: int = 2
    audio_rate: int = 48000

    @property
    def name(self) -> str:
        return self.path.name

    def preview_size(self, max_w: int = 640, max_h: int = 480) -> tuple[int, int]:
        s = min(max_w / self.width, max_h / self.height, 1.0)
        return max(2, int(self.width * s) // 2 * 2), max(2, int(self.height * s) // 2 * 2)

    def thumb_size(self, height: int) -> tuple[int, int]:
        return max(2, int(round(height * self.width / self.height / 2)) * 2), height


def probe(path: str | Path) -> VideoInfo:
    path = Path(path)
    r = _run(["-i", str(path)])
    txt = r.stderr.decode("utf-8", "replace")
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", txt)
    vline = next((l for l in txt.splitlines() if "Stream #" in l and "Video:" in l and "attached pic" not in l), None)
    if not m or not vline:
        raise VideoError(f"{path.name} không phải video hợp lệ")
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    wh = re.search(r", (\d{2,5})x(\d{2,5})", vline)
    if not wh:
        raise VideoError("Không đọc được kích thước video")
    w, h = int(wh[1]), int(wh[2])
    fm = re.search(r"(\d+(?:\.\d+)?) fps", vline) or re.search(r"(\d+(?:\.\d+)?) tbr", vline)
    fps = float(fm[1]) if fm else 25.0
    rot = re.search(r"rotat\w* of (-?\d+(?:\.\d+)?) degrees", txt) or re.search(r"rotate\s*:\s*(-?\d+)", txt)
    if rot and abs(round(float(rot[1]))) % 180 == 90:   # video quay dọc (điện thoại)
        w, h = h, w
    aline = next((l for l in txt.splitlines() if "Stream #" in l and "Audio:" in l), None)
    ach, arate = 2, 48000
    if aline:
        rm = re.search(r"(\d+) Hz", aline)
        arate = int(rm[1]) if rm else 48000
        ach = 1 if "mono" in aline else 2
    return VideoInfo(path, duration, w, h, fps, aline is not None, ach, arate)


def extract_audio(info: VideoInfo, rate: int) -> np.ndarray:
    """Âm thanh của video → float32 (n, ch) ở tần số `rate`."""
    ch = min(2, info.audio_channels)
    r = _run(["-v", "error", "-i", str(info.path), "-map", "0:a:0", "-vn", "-ac", str(ch), "-ar", str(rate),
              "-f", "f32le", "-"])
    if r.returncode != 0:
        raise VideoError(r.stderr.decode("utf-8", "replace").strip() or "Lỗi tách âm thanh")
    data = np.frombuffer(r.stdout, dtype="<f4")
    return data[: len(data) // ch * ch].reshape(-1, ch).copy()


# ====================================================================== khung hình xem trước
class FrameGrabber:
    """Lấy 1 khung hình tại thời điểm t (khi kéo con trỏ). Luôn xử lý yêu cầu MỚI NHẤT."""

    def __init__(self, info: VideoInfo, size: tuple[int, int], on_frame: Callable[[float, bytes], None]):
        self.info = info
        self.w, self.h = size
        self.on_frame = on_frame
        self._cv = threading.Condition()
        self._req: float | None = None
        self._alive = True
        threading.Thread(target=self._loop, daemon=True).start()

    def request(self, t: float) -> None:
        with self._cv:
            self._req = t
            self._cv.notify()

    def close(self) -> None:
        with self._cv:
            self._alive = False
            self._cv.notify()

    def _loop(self):
        while True:
            with self._cv:
                while self._req is None and self._alive:
                    self._cv.wait()
                if not self._alive:
                    return
                t, self._req = self._req, None
            t = max(0.0, min(t, self.info.duration - 1.0 / max(self.info.fps, 1)))
            try:
                r = _run(["-v", "error", "-ss", f"{t:.3f}", "-i", str(self.info.path), "-frames:v", "1", "-an",
                          "-vf", f"scale={self.w}:{self.h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         timeout=20)
                if len(r.stdout) >= self.w * self.h * 3:
                    self.on_frame(t, r.stdout[: self.w * self.h * 3])
            except Exception:
                pass


class FrameStreamer:
    """Giải mã liên tục từ t0 khi đang phát; giao diện lấy khung hình theo đồng hồ âm thanh."""

    def __init__(self, info: VideoInfo, size: tuple[int, int]):
        self.info = info
        self.w, self.h = size
        self.fps = min(info.fps or 25.0, 30.0)
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue = queue.Queue(maxsize=6)
        self._pending = None

    def start(self, t0: float) -> None:
        self.stop()
        self.q = queue.Queue(maxsize=6)
        self._pending = None
        if t0 >= self.info.duration:
            return
        self.proc = _popen(["-v", "error", "-ss", f"{max(0.0, t0):.3f}", "-i", str(self.info.path), "-an",
                            "-vf", f"fps={self.fps},scale={self.w}:{self.h}", "-f", "rawvideo",
                            "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._reader, args=(self.proc, self.q, t0), daemon=True).start()

    def _reader(self, proc: subprocess.Popen, q: queue.Queue, t0: float):
        size = self.w * self.h * 3
        k = 0
        while True:
            buf = proc.stdout.read(size)
            if not buf or len(buf) < size:
                break
            item = (t0 + k / self.fps, buf)
            k += 1
            while True:
                try:
                    q.put(item, timeout=0.5)
                    break
                except queue.Full:
                    if proc.poll() is not None or self.proc is not proc:
                        return

    def frame_for(self, t: float) -> bytes | None:
        """Khung hình mới nhất có thời điểm <= t (bỏ qua các khung đã trễ)."""
        latest = None
        half = 0.5 / self.fps
        while True:
            item = self._pending
            if item is None:
                try:
                    item = self.q.get_nowait()
                except queue.Empty:
                    break
            if item[0] <= t + half:
                latest = item[1]
                self._pending = None
            else:
                self._pending = item
                break
        return latest

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.kill()
                p.wait(timeout=2)
            except Exception:
                pass


def thumbnails(info: VideoInfo, height: int, on_thumb: Callable[[float, bytes], None],
               max_count: int = 300, cancel: threading.Event | None = None) -> None:
    """Tạo ảnh thu nhỏ rải đều theo thời gian, gọi on_thumb(t, rgb_bytes) cho từng ảnh."""
    count = max(1, min(max_count, int(math.ceil(info.duration))))
    rate = count / max(info.duration, 0.001)
    tw, th = info.thumb_size(height)
    p = _popen(["-v", "error", "-i", str(info.path), "-an", "-vf", f"fps={rate:.6f},scale={tw}:{th}",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = tw * th * 3
    k = 0
    try:
        while True:
            if cancel is not None and cancel.is_set():
                break
            buf = p.stdout.read(size)
            if not buf or len(buf) < size:
                break
            on_thumb((k + 0.5) / rate, buf)
            k += 1
    finally:
        p.kill()
        p.wait()


# ====================================================================== xuất video
def export_video(info: VideoInfo, audio: np.ndarray, rate: int, out: Path, start: float = 0.0,
                 end: float | None = None, reencode: bool = False, audio_bitrate: int = 320,
                 progress: Callable[[float], None] | None = None,
                 cancel: threading.Event | None = None) -> None:
    """Ghép hình của video gốc với âm thanh `audio` (đã trộn, bắt đầu tại `start`) → file video mới.

    Mặc định giữ nguyên hình (copy, không giảm chất lượng, rất nhanh). Khi xuất một đoạn (start>0)
    hoặc copy thất bại thì mã hoá lại H.264."""
    end = info.duration if end is None else min(end, info.duration)
    dur = max(0.01, end - start)
    out = Path(out)
    tmpdir = Path(tempfile.mkdtemp(prefix="wifirec_vid_"))
    wav = tmpdir / "audio.wav"
    sf.write(str(wav), np.clip(audio, -1, 1), rate, subtype="PCM_24")
    tmp_out = out.with_name(out.stem + ".part" + out.suffix)

    def attempt(copy_video: bool) -> tuple[bool, str]:
        args = ["-y", "-v", "error", "-nostats", "-progress", "pipe:1"]
        if start > 0:
            args += ["-ss", f"{start:.3f}"]
        args += ["-i", str(info.path), "-i", str(wav), "-map", "0:v:0", "-map", "1:a:0", "-t", f"{dur:.3f}"]
        if copy_video:
            args += ["-c:v", "copy"]
        else:
            args += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
        args += ["-c:a", "aac", "-b:a", f"{audio_bitrate}k"]
        if out.suffix.lower() in (".mp4", ".mov", ".m4v"):
            args += ["-movflags", "+faststart"]
        args.append(str(tmp_out))
        p = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        err_lines: list[bytes] = []
        threading.Thread(target=lambda: err_lines.extend(p.stderr), daemon=True).start()
        for line in p.stdout:
            if cancel is not None and cancel.is_set():
                p.kill()
                p.wait()
                return False, "Đã huỷ"
            m = re.match(rb"out_time_(?:us|ms)=(\d+)", line)
            if m and progress:
                progress(min(1.0, int(m[1]) / 1e6 / dur))
        p.wait()
        return p.returncode == 0, b"".join(err_lines).decode("utf-8", "replace").strip()

    try:
        ok, err = attempt(copy_video=not reencode and start == 0)
        if not ok and err != "Đã huỷ" and not reencode and start == 0:
            ok, err = attempt(copy_video=False)   # định dạng hình không hợp với file đích → mã hoá lại
        if not ok:
            tmp_out.unlink(missing_ok=True)
            raise VideoError(err or "FFmpeg lỗi")
        tmp_out.replace(out)
        if progress:
            progress(1.0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
