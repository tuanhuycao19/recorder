"""Hiệu ứng âm thanh. Mỗi hàm nhận mảng float32 (n, ch) và trả về mảng mới."""
from __future__ import annotations

import numpy as np

from ..audio_io import resample


def db_to_gain(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def peak(x: np.ndarray) -> float:
    return float(np.max(np.abs(x))) if x.size else 0.0


def amplify(x: np.ndarray, gain_db: float) -> np.ndarray:
    return (x * db_to_gain(gain_db)).astype(np.float32)


def normalize(x: np.ndarray, peak_db: float = -1.0, remove_dc: bool = True,
              independent: bool = False) -> np.ndarray:
    y = x.astype(np.float32, copy=True)
    if remove_dc and len(y):
        y -= y.mean(axis=0, keepdims=True)
    target = db_to_gain(peak_db)
    if independent:
        for c in range(y.shape[1]):
            p = peak(y[:, c])
            if p > 0:
                y[:, c] *= target / p
    else:
        p = peak(y)
        if p > 0:
            y *= target / p
    return y


def fade_in(x: np.ndarray) -> np.ndarray:
    ramp = np.linspace(0.0, 1.0, len(x), dtype=np.float32)[:, None]
    return x * ramp


def fade_out(x: np.ndarray) -> np.ndarray:
    ramp = np.linspace(1.0, 0.0, len(x), dtype=np.float32)[:, None]
    return x * ramp


def reverse(x: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(x[::-1])


def invert(x: np.ndarray) -> np.ndarray:
    return -x


def silence_like(x: np.ndarray) -> np.ndarray:
    return np.zeros_like(x)


def change_speed(x: np.ndarray, sr: int, percent: float) -> np.ndarray:
    """Đổi tốc độ (và cao độ) như băng cassette. percent=+50 → nhanh gấp 1.5."""
    factor = 1.0 + percent / 100.0
    if factor <= 0:
        raise ValueError("Tốc độ phải lớn hơn -100%")
    return resample(x, int(round(sr * factor)), sr)


def echo(x: np.ndarray, sr: int, delay_s: float, decay: float, repeats: int = 6) -> np.ndarray:
    d = max(1, int(delay_s * sr))
    y = x.astype(np.float32, copy=True)
    g = 1.0
    for k in range(1, repeats + 1):
        g *= decay
        if g < 0.001 or k * d >= len(x):
            break
        y[k * d:] += x[: len(x) - k * d] * g
    return y


def compressor(x: np.ndarray, sr: int, threshold_db: float = -18.0, ratio: float = 3.0,
               attack_ms: float = 10.0, release_ms: float = 150.0, makeup: bool = True) -> np.ndarray:
    """Nén dải động đơn giản (dò theo đường bao đỉnh, mỗi khối 1 ms)."""
    if not len(x):
        return x
    block = max(1, sr // 1000)
    n_blocks = int(np.ceil(len(x) / block))
    pad = np.zeros((n_blocks * block - len(x), x.shape[1]), dtype=np.float32)
    env_in = np.abs(np.concatenate([x, pad])).max(axis=1).reshape(n_blocks, block).max(axis=1)
    a_att = np.exp(-1.0 / max(1e-3, attack_ms))
    a_rel = np.exp(-1.0 / max(1e-3, release_ms))
    env = np.empty(n_blocks, dtype=np.float32)
    e = 0.0
    for i, v in enumerate(env_in):  # n_blocks = số ms → vòng lặp Python chấp nhận được
        a = a_att if v > e else a_rel
        e = a * e + (1 - a) * v
        env[i] = e
    env_db = 20 * np.log10(np.maximum(env, 1e-9))
    over = np.maximum(env_db - threshold_db, 0.0)
    gain_db = -over * (1.0 - 1.0 / ratio)
    gains = np.repeat(10 ** (gain_db / 20.0), block)[: len(x)].astype(np.float32)
    y = x * gains[:, None]
    if makeup:
        p = peak(y)
        if p > 0:
            y *= min(db_to_gain(-1.0) / p, db_to_gain(24))
    return y.astype(np.float32)


# ---------------------------------------------------------------- giảm nhiễu (spectral gating)
N_FFT = 2048
HOP = N_FFT // 4


def _stft(sig: np.ndarray) -> np.ndarray:
    win = np.hanning(N_FFT).astype(np.float32)
    padded = np.concatenate([np.zeros(N_FFT, np.float32), sig.astype(np.float32), np.zeros(N_FFT, np.float32)])
    n_frames = 1 + (len(padded) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    return np.fft.rfft(padded[idx] * win, axis=1)


def _istft(spec: np.ndarray, length: int) -> np.ndarray:
    win = np.hanning(N_FFT).astype(np.float32)
    frames = np.fft.irfft(spec, n=N_FFT, axis=1).astype(np.float32) * win
    total = N_FFT + HOP * (len(frames) - 1)
    out = np.zeros(total, np.float32)
    norm = np.zeros(total, np.float32)
    for i, f in enumerate(frames):
        s = i * HOP
        out[s:s + N_FFT] += f
        norm[s:s + N_FFT] += win * win
    out /= np.maximum(norm, 1e-8)
    return out[N_FFT:N_FFT + length]


def noise_profile(noise: np.ndarray) -> np.ndarray:
    """Phổ trung bình của đoạn chỉ có tiếng ồn → (số bin,)."""
    mono = noise.mean(axis=1)
    if len(mono) < N_FFT:
        raise ValueError("Đoạn tiếng ồn quá ngắn (cần ít nhất ~0.05 giây)")
    mag = np.abs(_stft(mono))
    return mag.mean(axis=0) + 2.0 * mag.std(axis=0)


def noise_reduce(x: np.ndarray, profile: np.ndarray, reduction_db: float = 12.0,
                 sensitivity_db: float = 6.0, smoothing_bands: int = 3) -> np.ndarray:
    floor = db_to_gain(-abs(reduction_db))
    thresh = profile * db_to_gain(sensitivity_db - 6.0)
    out = np.empty_like(x, dtype=np.float32)
    kernel = np.ones(2 * smoothing_bands + 1, np.float32) / (2 * smoothing_bands + 1)
    for c in range(x.shape[1]):
        spec = _stft(x[:, c])
        mag = np.abs(spec)
        mask = np.where(mag > thresh[None, :], 1.0, floor).astype(np.float32)
        if smoothing_bands > 0:  # làm mượt theo tần số để tránh tiếng "lóc chóc"
            mask = np.apply_along_axis(lambda m: np.convolve(m, kernel, mode="same"), 1, mask)
        # làm mượt theo thời gian (attack/decay ngắn)
        for i in range(1, len(mask)):
            mask[i] = np.maximum(mask[i], mask[i - 1] * 0.5)
        out[:, c] = _istft(spec * mask, len(x))
    return out


# ---------------------------------------------------------------- tạo âm thanh
def tone(sr: int, seconds: float, freq: float, amplitude: float, channels: int,
         waveform: str = "sine") -> np.ndarray:
    t = np.arange(int(sr * seconds), dtype=np.float64) / sr
    ph = 2 * np.pi * freq * t
    if waveform == "square":
        y = np.sign(np.sin(ph))
    elif waveform == "sawtooth":
        y = 2 * (t * freq - np.floor(0.5 + t * freq))
    else:
        y = np.sin(ph)
    y = (amplitude * y).astype(np.float32)
    return np.repeat(y[:, None], channels, axis=1)


def white_noise(sr: int, seconds: float, amplitude: float, channels: int) -> np.ndarray:
    rng = np.random.default_rng()
    return (amplitude * rng.uniform(-1, 1, (int(sr * seconds), channels))).astype(np.float32)
