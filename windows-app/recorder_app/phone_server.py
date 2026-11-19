"""Máy chủ TCP nhận kết nối từ app điện thoại + broadcast UDP để điện thoại tự tìm PC.

Luồng một phiên ghi âm:
  PC  -> START {session, sample_rate, channels}
  ĐT  -> STATE {recording: true, ...}   (sau đó liên tục gửi AUDIO = PCM trực tiếp)
  PC  -> STOP
  ĐT  -> STATE {recording: false}
  ĐT  -> FILE_BEGIN / FILE_DATA... / FILE_END {crc32}   (file WAV gốc ghi trên điện thoại)

File WAV gửi sau khi dừng là bản chuẩn (không mất gói do WiFi). Luồng AUDIO trực tiếp
chỉ để hiển thị mức âm và làm bản dự phòng nếu truyền file thất bại.
"""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import uuid
import wave
import zlib
from pathlib import Path
from typing import Callable

from . import protocol as P
from .levels import peak_level

EventCb = Callable[..., None]

FILE_BEGIN_TIMEOUT = 15.0   # giây chờ điện thoại bắt đầu gửi file sau khi dừng
PING_INTERVAL = 2.0
READ_TIMEOUT = 15.0


def local_ipv4_addresses() -> list[str]:
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    # Cách chắc chắn lấy IP của card mạng đang có route ra ngoài
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


class PhoneConnection:
    def __init__(self, server: "PhoneServer", sock: socket.socket, addr):
        self.server = server
        self.sock = sock
        self.addr = addr
        self.id = uuid.uuid4().hex[:8]
        self.name = f"Điện thoại {addr[0]}"
        self.alive = True
        self.ready = False  # đã nhận HELLO
        self.recording = False
        self.session: str | None = None
        self.sample_rate = 48000
        self.channels = 1
        self._send_lock = threading.Lock()
        self._live_wav: wave.Wave_write | None = None
        self._live_path: Path | None = None
        self._live_bytes = 0
        self._file_fp = None
        self._file_path: Path | None = None
        self._file_crc = 0
        self._file_session: str | None = None
        self._waiting_file_for: str | None = None
        self._done_sessions: set[str] = set()
        self._state_lock = threading.RLock()

    # ------------------------------------------------------------ gửi lệnh
    def send(self, msg_type: int, payload=None) -> None:
        data = P.encode_frame(msg_type, payload)
        with self._send_lock:
            self.sock.sendall(data)

    def start_recording(self, session: str, sample_rate: int, channels: int) -> None:
        self.send(P.MSG_START, {"session": session, "sample_rate": sample_rate, "channels": channels})

    def stop_recording(self) -> None:
        self.send(P.MSG_STOP, {})

    def close(self) -> None:
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    @property
    def label(self) -> str:
        return f"📱 {self.name} ({self.addr[0]})"

    # ------------------------------------------------------------ vòng đọc
    def run(self) -> None:
        self.sock.settimeout(READ_TIMEOUT)
        pinger = threading.Thread(target=self._ping_loop, daemon=True)
        pinger.start()
        try:
            while self.alive:
                msg_type, payload = P.read_frame(self.sock)
                self._handle(msg_type, payload)
        except (OSError, ConnectionError, P.ProtocolError) as e:
            if self.alive:
                self.server.emit("log", message=f"{self.name}: mất kết nối ({e})")
        finally:
            self.alive = False
            self._on_disconnect()
            self.server._remove(self)

    def _ping_loop(self) -> None:
        while self.alive:
            time.sleep(PING_INTERVAL)
            try:
                self.send(P.MSG_PING, {})
            except OSError:
                break

    def _handle(self, msg_type: int, payload: bytes) -> None:
        if msg_type == P.MSG_AUDIO:
            self._on_audio(payload)
        elif msg_type == P.MSG_FILE_DATA:
            self._on_file_data(payload)
        elif msg_type == P.MSG_HELLO:
            info = P.decode_json(payload)
            self.name = str(info.get("name") or self.name)
            self.ready = True
            self.send(P.MSG_HELLO, {"name": socket.gethostname(), "proto": P.PROTO_VERSION})
            self.server.emit("phone_connected", conn=self)
        elif msg_type == P.MSG_STATE:
            self._on_state(P.decode_json(payload))
        elif msg_type == P.MSG_FILE_BEGIN:
            self._on_file_begin(P.decode_json(payload))
        elif msg_type == P.MSG_FILE_END:
            self._on_file_end(P.decode_json(payload))
        elif msg_type == P.MSG_ERROR:
            msg = P.decode_json(payload).get("message", "lỗi không rõ")
            self.server.emit("log", message=f"{self.name}: {msg}")
            self.server.emit("phone_error", conn=self, message=msg)
        elif msg_type == P.MSG_PING:
            pass

    # ------------------------------------------------------------ trạng thái
    def _on_state(self, st: dict) -> None:
        with self._state_lock:
            if st.get("recording"):
                self.recording = True
                self.session = str(st.get("session") or time.strftime("%Y%m%d_%H%M%S"))
                self.sample_rate = int(st.get("sample_rate", 48000))
                self.channels = int(st.get("channels", 1))
                self._open_live()
                self.server.emit("phone_recording_started", conn=self, session=self.session,
                                 sample_rate=self.sample_rate, channels=self.channels)
            else:
                session = str(st.get("session") or self.session or "")
                self.recording = False
                self._close_live()
                self._waiting_file_for = session
                self.server.emit("phone_recording_stopped", conn=self, session=session)
                threading.Thread(target=self._file_watchdog, args=(session,), daemon=True).start()

    def _open_live(self) -> None:
        self._close_live()
        self._live_path = self.server.temp_dir / f"{self.session}_{self.id}_live.wav"
        w = wave.open(str(self._live_path), "wb")
        w.setnchannels(self.channels)
        w.setsampwidth(2)
        w.setframerate(self.sample_rate)
        self._live_wav = w
        self._live_bytes = 0

    def _close_live(self) -> None:
        if self._live_wav is not None:
            try:
                self._live_wav.close()
            except Exception:
                pass
            self._live_wav = None

    def _on_audio(self, pcm: bytes) -> None:
        with self._state_lock:
            if self._live_wav is not None:
                self._live_wav.writeframesraw(pcm)
                self._live_bytes += len(pcm)
        self.server.emit("phone_level", conn=self, level=peak_level(pcm))

    # ------------------------------------------------------------ nhận file
    def _on_file_begin(self, info: dict) -> None:
        with self._state_lock:
            self._abort_file()
            self._file_session = str(info.get("session") or self.session or time.strftime("%Y%m%d_%H%M%S"))
            self._file_path = self.server.temp_dir / f"{self._file_session}_{self.id}_phone.wav.part"
            self._file_fp = open(self._file_path, "wb")
            self._file_crc = 0
            self._file_expected = int(info.get("size", 0))
            self._file_received = 0
            self._waiting_file_for = None
        self.server.emit("phone_file_begin", conn=self, session=self._file_session, size=self._file_expected)

    def _on_file_data(self, data: bytes) -> None:
        if self._file_fp is None:
            return
        self._file_fp.write(data)
        self._file_crc = zlib.crc32(data, self._file_crc)
        self._file_received += len(data)
        self.server.emit("phone_file_progress", conn=self, received=self._file_received,
                         total=self._file_expected)

    def _on_file_end(self, info: dict) -> None:
        with self._state_lock:
            if self._file_fp is None:
                return
            self._file_fp.close()
            self._file_fp = None
            session = self._file_session
            expected_crc = info.get("crc32")
            ok = expected_crc is None or (int(expected_crc) & 0xFFFFFFFF) == (self._file_crc & 0xFFFFFFFF)
            self._send_ack(session, ok)
            if ok:
                final = self._file_path.with_suffix("")  # bỏ đuôi .part
                self._file_path.replace(final)
                self._done_sessions.discard(session)  # bản gốc luôn thay thế bản trực tiếp
                self._finish(session, final, "file")
            else:
                self.server.emit("log", message=f"{self.name}: file lỗi CRC, dùng bản thu trực tiếp")
                self._file_path.unlink(missing_ok=True)
                self._finish_with_live(session)

    def _send_ack(self, session: str, ok: bool) -> None:
        try:
            self.send(P.MSG_FILE_ACK, {"session": session, "ok": ok})
        except OSError:
            pass

    def _abort_file(self) -> None:
        if self._file_fp is not None:
            self._file_fp.close()
            self._file_fp = None
            if self._file_path:
                self._file_path.unlink(missing_ok=True)

    def _file_watchdog(self, session: str) -> None:
        time.sleep(FILE_BEGIN_TIMEOUT)
        with self._state_lock:
            if self._waiting_file_for == session:
                self.server.emit("log", message=f"{self.name}: không nhận được file gốc, dùng bản thu trực tiếp")
                self._finish_with_live(session)

    def _finish_with_live(self, session: str) -> None:
        self._waiting_file_for = None
        if self._live_path and self._live_path.exists() and self._live_bytes > 0:
            self._finish(session, self._live_path, "live")
        else:
            self.server.emit("phone_recording_failed", conn=self, session=session)

    def _finish(self, session: str, wav_path: Path, source: str) -> None:
        if session in self._done_sessions:
            return
        self._done_sessions.add(session)
        self.server.emit("phone_recording_done", conn=self, session=session, wav_path=wav_path, source=source)

    def _on_disconnect(self) -> None:
        with self._state_lock:
            self._close_live()
            session = self._file_session if self._file_fp is not None else (
                self._waiting_file_for or (self.session if self.recording else None))
            self._abort_file()
            if session and session not in self._done_sessions:
                self._finish_with_live(session)
        self.server.emit("phone_disconnected", conn=self)


class PhoneServer:
    def __init__(self, on_event: EventCb, port: int = P.TCP_PORT,
                 discovery_port: int = P.DISCOVERY_PORT, temp_dir: Path | None = None):
        self.on_event = on_event
        self.port = port
        self.discovery_port = discovery_port
        self.temp_dir = Path(temp_dir or tempfile.mkdtemp(prefix="wifirec_"))
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.connections: dict[str, PhoneConnection] = {}
        self._lock = threading.Lock()
        self._running = False
        self._srv: socket.socket | None = None

    def emit(self, name: str, **kw) -> None:
        try:
            self.on_event(name, **kw)
        except Exception:
            pass

    def start(self) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform.startswith("win"):
            # Trên Windows SO_REUSEADDR cho phép 2 tiến trình cùng chiếm cổng -> dùng chế độ độc quyền
            self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("0.0.0.0", self.port))
        self.port = self._srv.getsockname()[1]
        self._srv.listen(8)
        self._running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()
        if self.discovery_port:
            threading.Thread(target=self._beacon_loop, daemon=True).start()

    def stop(self) -> None:
        self._running = False
        if self._srv:
            self._srv.close()
        for c in list(self.connections.values()):
            c.close()

    def _accept_loop(self) -> None:
        while self._running:
            try:
                sock, addr = self._srv.accept()
            except OSError:
                break
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn = PhoneConnection(self, sock, addr)
            with self._lock:
                self.connections[conn.id] = conn
            threading.Thread(target=conn.run, daemon=True).start()

    def _remove(self, conn: PhoneConnection) -> None:
        with self._lock:
            self.connections.pop(conn.id, None)

    def _beacon_loop(self) -> None:
        msg = json.dumps({"app": P.DISCOVERY_MAGIC, "name": socket.gethostname(),
                          "port": self.port, "proto": P.PROTO_VERSION}).encode()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while self._running:
            targets = {"255.255.255.255"}
            for ip in local_ipv4_addresses():
                parts = ip.split(".")
                targets.add(".".join(parts[:3] + ["255"]))
            for t in targets:
                try:
                    s.sendto(msg, (t, self.discovery_port))
                except OSError:
                    pass
            time.sleep(1.0)
        s.close()
