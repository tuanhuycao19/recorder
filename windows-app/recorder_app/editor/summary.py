"""Tính min/max/RMS theo từng cột pixel để vẽ sóng âm nhanh, kể cả file dài hàng giờ.

Giống Audacity: giữ bản tóm tắt theo khối 256 mẫu; khi thu nhỏ thì vẽ từ bản tóm tắt,
khi phóng to thì tính trực tiếp từ mẫu.
"""
from __future__ import annotations

import numpy as np

BLOCK = 256


class Summary:
    def __init__(self):
        self.version = -1
        self.track_id = -1
        self.n_full = 0      # số khối đã tính
        self.mins = np.zeros((0, 1), np.float32)
        self.maxs = np.zeros((0, 1), np.float32)
        self.sqs = np.zeros((0, 1), np.float32)

    def update(self, track) -> None:
        data = track.data
        if track.id == self.track_id and track.version == self.version:
            return
        # track đang ghi: chỉ tính thêm phần mới (dữ liệu cũ không đổi)
        incremental = track.recording and track.id == self.track_id and self.mins.shape[1] == data.shape[1]
        start_block = self.n_full if incremental else 0
        n_blocks = len(data) // BLOCK
        if n_blocks > start_block:
            seg = data[start_block * BLOCK:n_blocks * BLOCK].reshape(-1, BLOCK, data.shape[1])
            mins, maxs = seg.min(axis=1), seg.max(axis=1)
            sqs = np.square(seg, dtype=np.float32).sum(axis=1)
        else:
            mins = maxs = sqs = np.zeros((0, data.shape[1]), np.float32)
        if incremental:
            self.mins = np.concatenate([self.mins[:start_block], mins])
            self.maxs = np.concatenate([self.maxs[:start_block], maxs])
            self.sqs = np.concatenate([self.sqs[:start_block], sqs])
        else:
            self.mins, self.maxs, self.sqs = mins, maxs, sqs
        self.n_full = max(n_blocks, 0)
        self.track_id = track.id
        self.version = track.version

    def columns(self, data: np.ndarray, ch: int, h0: float, spp: float, width: int):
        """Trả về (x, min, max, rms) cho các cột có dữ liệu."""
        n = len(data)
        edges = np.floor(h0 + np.arange(width + 1) * spp).astype(np.int64)
        valid = (edges[:-1] < n) & (edges[1:] > 0)
        if not valid.any():
            return None
        xs = np.nonzero(valid)[0]
        a = np.clip(edges[xs], 0, n - 1)
        b = np.clip(edges[xs + 1], 1, n)
        b = np.maximum(b, a + 1)
        if spp >= BLOCK * 2 and self.n_full > 0:
            ba = np.minimum(a // BLOCK, self.n_full - 1)
            bb = np.maximum(np.minimum(b // BLOCK, self.n_full), ba + 1)
            lo, hi = ba[0], bb[-1]
            idx = ba - lo
            mn = np.minimum.reduceat(self.mins[lo:hi, ch], idx)
            mx = np.maximum.reduceat(self.maxs[lo:hi, ch], idx)
            sq = np.add.reduceat(self.sqs[lo:hi, ch], idx)
            cnt = (bb - ba) * BLOCK
            rms = np.sqrt(np.maximum(sq, 0) / np.maximum(cnt, 1))
            return xs, mn, mx, np.minimum(rms, np.maximum(np.abs(mn), np.abs(mx)))
        seg = data[a[0]:b[-1], ch]
        idx = a - a[0]
        mn = np.minimum.reduceat(seg, idx)
        mx = np.maximum.reduceat(seg, idx)
        sq = np.add.reduceat(np.square(seg, dtype=np.float32), idx)
        rms = np.sqrt(sq / np.maximum(b - a, 1))
        return xs, mn, mx, rms
