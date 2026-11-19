import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recorder_app.audio_io import load_audio, save_audio  # noqa: E402
from recorder_app.editor import effects as fx  # noqa: E402
from recorder_app.editor.model import Clip, Project  # noqa: E402
from recorder_app.editor.player import Player  # noqa: E402
from recorder_app.editor.summary import Summary  # noqa: E402

SR = 48000


def ramp(n, ch=1):
    return np.repeat(np.linspace(-1, 1, n, dtype=np.float32)[:, None], ch, axis=1)


def make_project():
    p = Project(SR)
    p.add_track("A", ramp(SR))
    return p


def test_cut_paste_undo_redo():
    p = make_project()
    t = p.tracks[0]
    orig = t.data.copy()
    p.set_selection(1000, 3000)
    assert p.cut()
    assert t.length == SR - 2000
    assert p.clipboard[0].shape == (2000, 1)
    p.set_selection(0, 0)
    assert p.paste()
    assert t.length == SR
    assert np.array_equal(t.data[:2000], orig[1000:3000])
    p.undo()  # bỏ dán
    p.undo()  # bỏ cắt
    assert np.array_equal(t.data, orig)
    p.redo()
    assert t.length == SR - 2000


def test_trim_silence_delete():
    p = make_project()
    t = p.tracks[0]
    p.set_selection(100, 600)
    p.silence()
    assert not t.data[100:600].any() and t.data[99] != 0
    p.trim()  # giữ nguyên vị trí như Audacity
    assert len(t.clips) == 1 and t.clips[0].offset == 100 and t.clips[0].length == 500
    p.set_selection(0, 200)
    p.delete()  # xoá 100 mẫu trống + 100 mẫu đầu clip, phần sau dồn lại
    assert t.clips[0].offset == 0 and t.clips[0].length == 400


def test_effect_on_selection_only():
    p = make_project()
    t = p.tracks[0]
    p.set_selection(0, SR // 2)
    p.apply_effect("x", lambda x: fx.amplify(x, -6))
    assert np.isclose(t.data[0, 0], -1 * fx.db_to_gain(-6))
    assert np.isclose(t.data[-1, 0], 1.0)


def test_effects_shapes():
    x = (0.5 * np.sin(np.arange(SR) / SR * 2 * np.pi * 440)).astype(np.float32)[:, None]
    assert np.isclose(fx.peak(fx.normalize(x, -1.0)), fx.db_to_gain(-1.0), atol=1e-4)
    assert fx.fade_in(x)[0, 0] == 0 and fx.fade_out(x)[-1, 0] == 0
    assert len(fx.change_speed(x, SR, 100)) in range(SR // 2 - 10, SR // 2 + 10)
    assert fx.echo(x, SR, 0.1, 0.5).shape == x.shape
    c = fx.compressor(x, SR, -20, 4)
    assert c.shape == x.shape and fx.peak(c) <= 1.0


def test_noise_reduction_lowers_noise():
    rng = np.random.default_rng(0)
    noise = (0.05 * rng.standard_normal((SR * 2, 1))).astype(np.float32)
    tone = (0.5 * np.sin(np.arange(SR * 2) / SR * 2 * np.pi * 440)).astype(np.float32)[:, None]
    sig = noise.copy()
    sig[SR:] += tone[SR:]
    prof = fx.noise_profile(sig[:SR // 2])
    out = fx.noise_reduce(sig, prof, reduction_db=18)
    rms = lambda a: float(np.sqrt(np.mean(a ** 2)))  # noqa: E731
    assert rms(out[SR // 4:SR // 2]) < rms(sig[SR // 4:SR // 2]) * 0.3   # nhiễu giảm mạnh
    assert rms(out[SR + SR // 4:]) > rms(tone[SR + SR // 4:]) * 0.8       # tiếng chính giữ nguyên


def test_mix_mute_solo_pan():
    p = Project(SR)
    a = p.add_track("A", np.full((100, 1), 0.25, np.float32))
    b = p.add_track("B", np.full((50, 1), 0.25, np.float32))
    assert np.allclose(p.mix(0, 100)[:50], 0.5) and np.allclose(p.mix(0, 100)[50:], 0.25)
    b.mute = True
    assert np.allclose(p.mix(0, 100), 0.25)
    b.mute, b.solo = False, True
    assert np.allclose(p.mix(0, 50), 0.25)
    b.solo = False
    a.pan = -1.0
    m = p.mix(0, 50)
    assert m.shape[1] == 2 and np.allclose(m[:, 1], 0.25) and np.allclose(m[:, 0], 0.5)


def test_player_render_and_loop():
    p = make_project()
    pl = Player(p)
    pl._tracks = list(p.tracks)
    pl.start_pos, pl.end_pos, pl.position, pl.loop = 0, 1000, 0, True
    out = pl.render(2500)
    assert out.shape == (2500, 2)
    assert np.allclose(out[:1000, 0], out[1000:2000, 0])
    assert pl.position == 500


def test_recording_track_and_summary():
    p = Project(SR)
    t = p.add_track("rec", np.zeros((0, 1), np.float32))
    c = Clip(1000, np.zeros((0, 1), np.float32))
    c.begin_recording()
    t.clips.append(c)
    s = Summary()
    for _ in range(50):
        c.append(np.full((960, 1), 0.5, np.float32))
        s.update(c)
    assert t.recording and t.length == 1000 + 50 * 960
    xs, mn, mx, rms = s.columns(c.data, 0, 0.0, 1024.0, 100)
    assert mx.max() == np.float32(0.5) and mn.min() == 0.5
    c.end_recording()
    assert not t.recording and t.length == 1000 + 50 * 960


def test_project_save_load_and_audio_io():
    p = make_project()
    p.tracks[0].gain_db = -3
    p.add_track("B", ramp(500, 2))
    d = Path(tempfile.mkdtemp())
    p.save(d / "x.wrproj")
    q = Project.load(d / "x.wrproj")
    assert [t.name for t in q.tracks] == ["A", "B"] and q.tracks[0].gain_db == -3
    assert np.array_equal(q.tracks[1].data, p.tracks[1].data)
    for ext in (".wav", ".flac", ".mp3"):
        save_audio(d / f"o{ext}", p.mix(0, p.length), SR)
        data, sr = load_audio(d / f"o{ext}")
        assert sr == SR and len(data) >= SR - 10


# ---------------------------------------------------------------- clip: tách / gộp / kéo
def test_split_and_join():
    p = make_project()
    t = p.tracks[0]
    orig = t.data.copy()
    p.set_selection(10000, 20000)
    assert p.split()
    assert [(c.offset, c.length) for c in t.clips] == [(0, 10000), (10000, 10000), (20000, SR - 20000)]
    assert np.array_equal(t.data, orig)          # tách không làm đổi âm thanh
    p.set_selection(0, 0)
    assert p.join()
    assert len(t.clips) == 1 and np.array_equal(t.data, orig)
    p.undo()
    assert len(t.clips) == 3


def test_split_delete_leaves_gap_and_split_new():
    p = make_project()
    t = p.tracks[0]
    p.set_selection(10000, 20000)
    assert p.split_delete()
    assert [(c.offset, c.end) for c in t.clips] == [(0, 10000), (20000, SR)]
    assert t.length == SR and not t.data[10000:20000].any()
    p.undo()
    t.selected = True
    p.set_selection(10000, 20000)
    assert p.split_new()
    assert len(p.tracks) == 2
    nt = p.tracks[1]
    assert [(c.offset, c.end) for c in nt.clips] == [(10000, 20000)]
    assert [(c.offset, c.end) for c in t.clips] == [(0, 10000), (20000, SR)]


def test_move_clip_no_overlap_and_undo():
    p = make_project()
    t = p.tracks[0]
    p.set_selection(24000, 24000)
    p.split()
    left, right = t.clips
    snap = p.snapshot("Di chuyển đoạn")
    assert not p.move_clip(right, t, t, 1000)            # đè lên clip trái → không cho
    assert p.move_clip(right, t, t, 60000)               # dời sang phải, để lại khoảng trống
    p.push_snapshot(snap)
    assert t.length == 60000 + 24000 and not t.data[24000:60000].any()
    other = p.add_track("B", np.zeros((10, 1), np.float32))
    assert p.move_clip(right, t, other, 100)              # chuyển sang track khác
    assert right in other.clips and right not in t.clips
    p.undo()  # bỏ thêm track B
    p.undo()  # bỏ di chuyển
    assert right in t.clips and right.offset == 24000


def test_ripple_delete_and_paste_across_clips():
    p = Project(SR)
    t = p.add_track("A", np.ones((100, 1), np.float32))
    t.clips.append(Clip(200, np.full((100, 1), 0.5, np.float32)))
    p.set_selection(50, 250)             # cắt qua khoảng trống
    assert p.cut()
    assert [(c.offset, c.end) for c in t.clips] == [(0, 50), (50, 100)]
    clip = p.clipboard[0]
    assert len(clip) == 200 and not clip[50:150].any()
    p.set_selection(50, 50)
    p.paste()
    assert t.length == 300 and np.allclose(t.data[250:300], 0.5)


def test_effect_across_clips_and_gap():
    p = Project(SR)
    t = p.add_track("A", np.full((100, 1), 0.5, np.float32))
    t.clips.append(Clip(200, np.full((100, 1), 0.5, np.float32)))
    p.apply_effect("fade", fx.fade_out)           # fade liền mạch qua cả 2 clip
    d = t.data
    assert d[0, 0] > d[99, 0] > d[200, 0] > d[299, 0]
    p.apply_effect("speed", lambda x: fx.change_speed(x, SR, 100))   # đổi độ dài → từng clip
    assert all(abs(c.length - 50) <= 2 for c in t.clips)
    assert t.fits(t.clips[1].offset, t.clips[1].length, ignore=t.clips[1])


def test_split_at_silences():
    p = Project(SR)
    tone = (0.5 * np.sin(np.arange(SR) / SR * 2 * np.pi * 300)).astype(np.float32)[:, None]
    gap = np.zeros((SR, 1), np.float32)
    p.add_track("A", np.concatenate([tone, gap, tone, gap, tone]))
    n = p.split_at_silences(-40, 0.5, 0.1)
    t = p.tracks[0]
    assert n == 2 and len(t.clips) == 3
    assert abs(t.clips[1].offset - int(2.0 * SR - 0.1 * SR)) < SR * 0.02


def test_project_v2_roundtrip_with_clips():
    p = make_project()
    p.set_selection(10000, 20000)
    p.split_delete()
    d = Path(tempfile.mkdtemp())
    p.save(d / "c.wrproj")
    q = Project.load(d / "c.wrproj")
    assert [(c.offset, c.end) for c in q.tracks[0].clips] == [(0, 10000), (20000, SR)]
