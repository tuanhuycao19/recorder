from __future__ import annotations

import numpy as np


def peak_level(pcm16: bytes) -> float:
    """Mức đỉnh 0..1 của một đoạn PCM 16-bit little-endian."""
    if len(pcm16) < 2:
        return 0.0
    samples = np.frombuffer(pcm16[: len(pcm16) // 2 * 2], dtype="<i2")
    return float(np.max(np.abs(samples.astype(np.int32)))) / 32768.0
