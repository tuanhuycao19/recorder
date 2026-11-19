"""Chuyển WAV (PCM 16-bit) sang MP3 bằng LAME (thư viện lameenc)."""
from __future__ import annotations

import wave
from pathlib import Path

import lameenc

CHUNK_FRAMES = 32768


def wav_to_mp3(wav_path: str | Path, mp3_path: str | Path, bitrate_kbps: int = 320) -> Path:
    wav_path, mp3_path = Path(wav_path), Path(mp3_path)
    with wave.open(str(wav_path), "rb") as wf:
        channels = wf.getnchannels()
        if wf.getsampwidth() != 2:
            raise ValueError("Chỉ hỗ trợ WAV PCM 16-bit")
        if channels not in (1, 2):
            raise ValueError("Chỉ hỗ trợ mono hoặc stereo")
        enc = lameenc.Encoder()
        enc.set_bit_rate(int(bitrate_kbps))
        enc.set_in_sample_rate(wf.getframerate())
        enc.set_channels(channels)
        enc.set_quality(2)  # 2 = chất lượng cao (0 tốt nhất, 9 nhanh nhất)
        mp3_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = mp3_path.with_suffix(".mp3.part")
        with open(tmp, "wb") as out:
            while True:
                data = wf.readframes(CHUNK_FRAMES)
                if not data:
                    break
                out.write(enc.encode(data))
            out.write(enc.flush())
    tmp.replace(mp3_path)
    return mp3_path


def wav_duration(wav_path: str | Path) -> float:
    with wave.open(str(wav_path), "rb") as wf:
        rate = wf.getframerate()
        return wf.getnframes() / rate if rate else 0.0
