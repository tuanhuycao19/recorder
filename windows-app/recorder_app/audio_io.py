"""Đọc / ghi file âm thanh. Dữ liệu trong bộ nhớ: numpy float32 dạng (số mẫu, số kênh), giá trị -1..1."""
from __future__ import annotations

from pathlib import Path

import lameenc
import numpy as np
import soundfile as sf

IMPORT_FILTER = "Âm thanh (*.wav *.mp3 *.flac *.ogg *.aif *.aiff);;Tất cả (*.*)"


def load_audio(path: str | Path) -> tuple[np.ndarray, int]:
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return np.ascontiguousarray(data), int(sr)


def resample(data: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    if sr_from == sr_to or len(data) == 0:
        return data
    try:
        import soxr
        return soxr.resample(data, sr_from, sr_to, quality="HQ").astype(np.float32)
    except ImportError:  # dự phòng: nội suy tuyến tính
        n_out = int(round(len(data) * sr_to / sr_from))
        x_old = np.arange(len(data)) / sr_from
        x_new = np.arange(n_out) / sr_to
        return np.stack([np.interp(x_new, x_old, data[:, c]) for c in range(data.shape[1])],
                        axis=1).astype(np.float32)


def to_int16(data: np.ndarray) -> np.ndarray:
    return (np.clip(data, -1.0, 1.0) * 32767.0).round().astype("<i2")


def save_mp3(path: str | Path, data: np.ndarray, sr: int, bitrate: int = 320) -> None:
    ch = data.shape[1]
    if ch > 2:
        data = data[:, :2]
        ch = 2
    enc = lameenc.Encoder()
    enc.set_bit_rate(int(bitrate))
    enc.set_in_sample_rate(int(sr))
    enc.set_channels(ch)
    enc.set_quality(2)
    pcm = to_int16(data)
    path = Path(path)
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as f:
        step = 1 << 16
        for i in range(0, len(pcm), step):
            f.write(enc.encode(pcm[i:i + step].tobytes()))
        f.write(enc.flush())
    tmp.replace(path)


def save_audio(path: str | Path, data: np.ndarray, sr: int, bitrate: int = 320) -> None:
    """Định dạng theo đuôi file: .mp3 (LAME), .wav (16-bit), .flac (24-bit), .ogg."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    if ext == ".mp3":
        save_mp3(path, data, sr, bitrate)
    elif ext == ".wav":
        sf.write(str(path), np.clip(data, -1, 1), sr, subtype="PCM_16")
    elif ext == ".flac":
        sf.write(str(path), np.clip(data, -1, 1), sr, subtype="PCM_24")
    elif ext == ".ogg":
        sf.write(str(path), np.clip(data, -1, 1), sr, format="OGG", subtype="VORBIS")
    else:
        raise ValueError(f"Không hỗ trợ định dạng {ext}")
