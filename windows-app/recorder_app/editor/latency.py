"""Căn thời gian cho ghi đè (overdub) và hiệu chỉnh độ trễ bằng tiếng "tách".

Vị trí bản ghi trên dòng thời gian = vị trí mẫu đang ra loa tại thời điểm micro thu mẫu đầu tiên:
    pos = play_pos0 + (t_mic0 - t_dac0) * rate - bù_trễ
Bù trễ (ms) đo bằng công cụ hiệu chỉnh: phát chuỗi tiếng tách, ghi lại, so khớp xem lệch bao nhiêu.
"""
from __future__ import annotations

import numpy as np

CLICK_GAPS = [0.7, 0.9, 0.6, 1.0, 0.8, 0.65, 0.95]   # khoảng cách không đều → không bị nhầm chu kỳ
CLICK_START = 0.6


def overdub_position(play_pos0: int, dac_ns0: int, mic_ns0: int, rate: int, correction_ms: float = 0.0) -> int:
    return int(round(play_pos0 + (mic_ns0 - dac_ns0) * rate / 1e9 - correction_ms * rate / 1000.0))


def click_track(rate: int) -> np.ndarray:
    """Chuỗi tiếng tách (1.5 kHz, 10 ms) → mảng (n, 1)."""
    times = [CLICK_START]
    for g in CLICK_GAPS:
        times.append(times[-1] + g)
    n = int((times[-1] + 0.8) * rate)
    out = np.zeros((n, 1), np.float32)
    m = int(0.010 * rate)
    burst = (0.9 * np.sin(2 * np.pi * 1500 * np.arange(m) / rate) * np.hanning(m)).astype(np.float32)
    for t in times:
        k = int(t * rate)
        out[k:k + m, 0] += burst
    return out


def measure_lag(reference: np.ndarray, recorded: np.ndarray, rate: int, max_lag_s: float = 0.5) -> tuple[int, float]:
    """Tìm độ lệch (mẫu) của `recorded` so với `reference`: dương = bản ghi bị trễ.
    Trả về (lag, độ rõ = đỉnh cao nhất / đỉnh cao thứ hai; > 1.5 là tin được)."""
    ref = reference.mean(axis=1) if reference.ndim == 2 else reference
    rec = recorded.mean(axis=1) if recorded.ndim == 2 else recorded
    n = max(len(ref), len(rec))
    ref = np.pad(ref, (0, n - len(ref))).astype(np.float64)
    rec = np.pad(rec, (0, n - len(rec))).astype(np.float64)
    # lọc thông cao đơn giản (bỏ ù / tiếng trầm) rồi tương quan chéo qua FFT
    ref = np.diff(ref, prepend=0.0)
    rec = np.diff(rec, prepend=0.0)
    size = 1 << int(np.ceil(np.log2(2 * n)))
    corr = np.fft.irfft(np.fft.rfft(rec, size) * np.conj(np.fft.rfft(ref, size)), size)
    max_lag = int(max_lag_s * rate)
    lags = np.concatenate([np.arange(0, max_lag + 1), np.arange(-max_lag, 0)])
    vals = np.concatenate([corr[:max_lag + 1], corr[-max_lag:]])
    a = np.abs(vals)
    k = int(np.argmax(a))
    guard = int(0.02 * rate)                     # bỏ vùng ±20 ms quanh đỉnh rồi tìm đỉnh thứ hai
    mask = np.abs(lags - lags[k]) > guard
    second = float(a[mask].max()) if mask.any() else 0.0
    ratio = float(a[k] / second) if second > 0 else float("inf")
    return int(lags[k]), ratio
