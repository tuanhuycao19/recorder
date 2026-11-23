"""Ghi đè (overdub): đồng bộ đồng hồ với điện thoại, căn vị trí bản ghi, đo độ trễ bằng tiếng tách."""
import queue
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recorder_app import protocol as P  # noqa: E402
from recorder_app.editor.latency import click_track, measure_lag, overdub_position  # noqa: E402
from recorder_app.phone_server import PhoneServer  # noqa: E402

PHONE_CLOCK_OFFSET = 123_456_789_000      # đồng hồ điện thoại lệch PC ~123 giây
NET_DELAY = 0.004                         # 4 ms mỗi chiều


class ClockPhone:
    """Điện thoại giả: trả lời PING với đồng hồ riêng, mạng có độ trễ không đều."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port))
        self.sock.sendall(P.encode_frame(P.MSG_HELLO, {"name": "Clock Phone"}))
        self.lock = threading.Lock()
        threading.Thread(target=self.loop, daemon=True).start()

    def now(self):
        return time.perf_counter_ns() + PHONE_CLOCK_OFFSET

    def send(self, t, payload):
        with self.lock:
            self.sock.sendall(P.encode_frame(t, payload))

    def loop(self):
        rng = np.random.default_rng(0)
        try:
            while True:
                t, payload = P.read_frame(self.sock)
                if t == P.MSG_PING:
                    j = P.decode_json(payload)
                    if "pc" in j:
                        d1 = NET_DELAY + rng.uniform(0, 0.02)     # chiều đi (thêm nhiễu)
                        time.sleep(d1)
                        ph = self.now()
                        time.sleep(NET_DELAY + rng.uniform(0, 0.02))
                        self.send(P.MSG_PONG, {"pc": j["pc"], "ph": ph})
        except (OSError, ConnectionError):
            pass


def test_clock_sync_and_timing():
    events = queue.Queue()
    srv = PhoneServer(lambda n, **kw: events.put((n, kw)), port=0, discovery_port=0,
                      temp_dir=Path(tempfile.mkdtemp()))
    srv.start()
    phone = ClockPhone(srv.port)
    deadline = time.time() + 10
    conn = None
    while time.time() < deadline:
        n, kw = events.get(timeout=10)
        if n == "phone_connected":
            conn = kw["conn"]
            break
    time.sleep(1.2)   # đợi đợt PING đồng bộ
    assert conn.clock_offset_ns is not None
    err_ms = abs(conn.clock_offset_ns - PHONE_CLOCK_OFFSET) / 1e6
    assert err_ms < 8, err_ms          # sai số chỉ cỡ vài ms dù mạng trễ không đều tới 20 ms
    # điện thoại báo thời điểm mẫu đầu tiên → PC quy đổi sang đồng hồ của mình
    t0_pc = time.perf_counter_ns()
    phone.send(P.MSG_TIMING, {"session": "s1", "t0_ns": t0_pc + PHONE_CLOCK_OFFSET})
    while True:
        n, kw = events.get(timeout=5)
        if n == "phone_timing":
            break
    assert kw["session"] == "s1" and abs(kw["t0_pc_ns"] - t0_pc) / 1e6 < 8
    srv.stop()


def test_overdub_position_math():
    rate = 48000
    dac0 = 10_000_000_000
    # micro bắt đầu thu 250 ms sau khi mẫu 96000 (2 s) ra loa → bản ghi nằm ở 2.25 s
    assert overdub_position(96000, dac0, dac0 + 250_000_000, rate) == 96000 + 12000
    # bù độ trễ 20 ms → kéo sớm lên 960 mẫu
    assert overdub_position(96000, dac0, dac0 + 250_000_000, rate, 20.0) == 96000 + 12000 - 960
    # micro bắt đầu trước khi nhạc phát → vị trí sớm hơn điểm bắt đầu
    assert overdub_position(0, dac0, dac0 - 100_000_000, rate) == -4800


def test_measure_lag_with_room_and_noise():
    rate = 48000
    c = click_track(rate)
    rng = np.random.default_rng(3)
    for lag in (0, 2400, -720, 7000):
        rec = np.zeros_like(c)
        rec[max(0, lag):len(c) + min(0, lag)] = c[max(0, -lag):len(c) - max(0, lag)]
        rec = 0.15 * rec + 0.02 * rng.standard_normal(rec.shape).astype(np.float32)
        rec[1800:, 0] += 0.3 * rec[:-1800, 0]      # vang phòng
        got, clarity = measure_lag(c, rec, rate)
        assert got == lag and clarity > 3
    _, clarity = measure_lag(c, 0.05 * rng.standard_normal(c.shape), rate)
    assert clarity < 2       # không có tiếng tách → báo không rõ
