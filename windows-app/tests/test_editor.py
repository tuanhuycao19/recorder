import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recorder_app.audio_io import load_audio, save_audio  # noqa: E402
from recorder_app.editor import effects as fx  # noqa: E402
from recorder_app.editor.model import Project  # noqa: E402
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
    orig = t.data
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
    assert t.data is orig
    p.redo()
    assert t.length == SR - 2000


def test_trim_silence_delete():
    p = make_project()
    t = p.tracks[0]
    p.set_selection(100, 600)
    p.silence()
    assert not t.data[100:600].any() and t.data[99] != 0
    p.trim()
    assert t.length == 500 and (p.sel_start, p.sel_end) == (0, 500)
    p.set_selection(0, 100)
    p.delete()
    assert t.length == 400


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
    t.begin_recording(prefix=1000)
    s = Summary()
    for _ in range(50):
        t.append(np.full((960, 1), 0.5, np.float32))
        s.update(t)
    assert t.length == 1000 + 50 * 960
    xs, mn, mx, rms = s.columns(t.data, 0, 0.0, 1024.0, 100)
    assert mx.max() == np.float32(0.5) and mn.min() == 0.0
    t.end_recording()
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
