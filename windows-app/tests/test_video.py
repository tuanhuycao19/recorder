"""Kiểm tra nhập / xem trước / xuất video (cần FFmpeg từ gói imageio-ffmpeg)."""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recorder_app import video as V  # noqa: E402
from recorder_app.editor.model import Project  # noqa: E402

try:
    FF = V.ffmpeg_exe()
except V.VideoError:
    FF = None
pytestmark = pytest.mark.skipif(FF is None, reason="không có FFmpeg")


@pytest.fixture(scope="module")
def sample_video():
    d = Path(tempfile.mkdtemp())
    out = d / "in.mp4"
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=4",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(out)], check=True)
    return out


def test_probe_and_audio(sample_video):
    info = V.probe(sample_video)
    assert (info.width, info.height) == (320, 240) and abs(info.duration - 4) < 0.1 and info.has_audio
    a = V.extract_audio(info, 48000)
    assert abs(len(a) / 48000 - 4) < 0.1 and np.abs(a).max() > 0.1


def test_frames_and_thumbs(sample_video):
    info = V.probe(sample_video)
    got = []
    g = V.FrameGrabber(info, (160, 120), lambda t, b: got.append(len(b)))
    g.request(2.0)
    for _ in range(100):
        if got:
            break
        time.sleep(0.05)
    g.close()
    assert got == [160 * 120 * 3]
    s = V.FrameStreamer(info, (160, 120))
    s.start(1.0)
    frame = None
    for _ in range(100):
        frame = s.frame_for(1.5)
        if frame:
            break
        time.sleep(0.05)
    s.stop()
    assert frame is not None and len(frame) == 160 * 120 * 3
    thumbs = []
    V.thumbnails(info, 30, lambda t, b: thumbs.append(t))
    assert len(thumbs) == 4


def test_export_video_with_new_audio(sample_video, tmp_path):
    info = V.probe(sample_video)
    audio = np.zeros((4 * 48000, 2), np.float32)
    out = tmp_path / "out.mp4"
    V.export_video(info, audio, 48000, out)
    o = V.probe(out)
    assert o.has_audio and abs(o.duration - 4) < 0.15
    assert np.abs(V.extract_audio(o, 48000)).max() < 0.01        # đã thay bằng âm thanh mới (im lặng)
    part = tmp_path / "part.mkv"
    V.export_video(info, audio[:48000], 48000, part, start=1.0, end=2.0)
    assert abs(V.probe(part).duration - 1.0) < 0.15


def test_project_keeps_video_path(sample_video, tmp_path):
    p = Project(48000)
    p.add_track("A", np.zeros((10, 1), np.float32))
    p.video = V.probe(sample_video)
    p.save(tmp_path / "v.wrproj")
    assert Project.load(tmp_path / "v.wrproj").video_path == str(sample_video)
