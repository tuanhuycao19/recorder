"""Giả lập app điện thoại để kiểm tra toàn bộ luồng: kết nối → ghi → dừng → nhận file → MP3."""
import io
import math
import queue
import socket
import struct
import sys
import tempfile
import threading
import wave
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recorder_app import protocol as P  # noqa: E402
from recorder_app.encoder import wav_to_mp3  # noqa: E402
from recorder_app.phone_server import PhoneServer  # noqa: E402


def sine_pcm(seconds: float, sr: int = 48000, freq: float = 440.0) -> bytes:
    n = int(seconds * sr)
    return b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / sr))) for i in range(n))


def make_wav(pcm: bytes, sr: int) -> bytes:
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return bio.getvalue()


class FakePhone:
    def __init__(self, port, send_file=True, corrupt=False):
        self.sock = socket.create_connection(("127.0.0.1", port))
        self.send_file = send_file
        self.corrupt = corrupt
        self.send(P.MSG_HELLO, {"name": "Pixel Test"})

    def send(self, t, payload=None):
        self.sock.sendall(P.encode_frame(t, payload))

    def serve_one_session(self):
        session = None
        pcm = sine_pcm(1.0)
        while True:
            t, payload = P.read_frame(self.sock)
            if t == P.MSG_START:
                st = P.decode_json(payload)
                session = st["session"]
                self.send(P.MSG_STATE, {"recording": True, "session": session,
                                        "sample_rate": 48000, "channels": 1})
                for i in range(0, len(pcm), 3840):
                    self.send(P.MSG_AUDIO, pcm[i:i + 3840])
            elif t == P.MSG_STOP:
                self.send(P.MSG_STATE, {"recording": False, "session": session})
                if self.send_file:
                    data = make_wav(pcm, 48000)
                    self.send(P.MSG_FILE_BEGIN, {"session": session, "size": len(data)})
                    for i in range(0, len(data), 65536):
                        self.send(P.MSG_FILE_DATA, data[i:i + 65536])
                    crc = zlib.crc32(data) ^ (1 if self.corrupt else 0)
                    self.send(P.MSG_FILE_END, {"session": session, "crc32": crc})
                else:
                    self.sock.close()
                return


def run_case(send_file=True, corrupt=False):
    events = queue.Queue()
    srv = PhoneServer(lambda n, **kw: events.put((n, kw)), port=0, discovery_port=0,
                      temp_dir=Path(tempfile.mkdtemp()))
    srv.start()
    phone = FakePhone(srv.port, send_file=send_file, corrupt=corrupt)
    th = threading.Thread(target=phone.serve_one_session, daemon=True)
    th.start()

    def wait(name, timeout=20):
        while True:
            n, kw = events.get(timeout=timeout)
            if n == name:
                return kw

    conn = wait("phone_connected")["conn"]
    assert conn.name == "Pixel Test"
    conn.start_recording("20260101_000000", 48000, 1)
    wait("phone_recording_started")
    conn.stop_recording()
    done = wait("phone_recording_done")
    srv.stop()
    return done


def check_mp3(wav_path):
    out = Path(tempfile.mkdtemp()) / "out.mp3"
    wav_to_mp3(wav_path, out, 320)
    data = out.read_bytes()
    assert len(data) > 30000, len(data)  # ~1 s @320 kbps ≈ 40 KB
    assert data[:3] == b"ID3" or data[0] == 0xFF


def test_file_transfer():
    done = run_case()
    assert done["source"] == "file"
    with wave.open(str(done["wav_path"])) as w:
        assert w.getnframes() == 48000
    check_mp3(done["wav_path"])


def test_crc_mismatch_falls_back_to_live():
    done = run_case(corrupt=True)
    assert done["source"] == "live"
    check_mp3(done["wav_path"])


def test_disconnect_falls_back_to_live():
    done = run_case(send_file=False)
    assert done["source"] == "live"
    with wave.open(str(done["wav_path"])) as w:
        assert w.getnframes() == 48000
    check_mp3(done["wav_path"])


if __name__ == "__main__":
    test_file_transfer()
    test_crc_mismatch_falls_back_to_live()
    test_disconnect_falls_back_to_live()
    print("OK")
