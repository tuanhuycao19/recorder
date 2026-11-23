"""Giao thức TCP giữa app Windows và app điện thoại.

Mỗi gói tin: [1 byte loại][4 byte độ dài big-endian][payload].
Payload của các gói điều khiển là JSON UTF-8; AUDIO và FILE_DATA là byte thô.
"""
from __future__ import annotations

import json
import socket
import struct

PROTO_VERSION = 1
TCP_PORT = 50510          # PC lắng nghe kết nối từ điện thoại
DISCOVERY_PORT = 50511    # PC phát broadcast UDP để điện thoại tự tìm
DISCOVERY_MAGIC = "wifirec"

MSG_HELLO = 1
MSG_START = 2
MSG_STOP = 3
MSG_AUDIO = 4
MSG_FILE_BEGIN = 5
MSG_FILE_DATA = 6
MSG_FILE_END = 7
MSG_STATE = 8
MSG_PING = 9
MSG_ERROR = 10
MSG_FILE_ACK = 11
MSG_PONG = 12      # ĐT → PC: trả lời PING {pc, ph} để đồng bộ đồng hồ
MSG_TIMING = 13    # ĐT → PC: {session, t0_ns} thời điểm (đồng hồ ĐT) thu mẫu đầu tiên

MAX_PAYLOAD = 16 * 1024 * 1024
_HEADER = struct.Struct(">BI")


class ProtocolError(Exception):
    pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Kết nối đã đóng")
        buf.extend(chunk)
    return bytes(buf)


def read_frame(sock: socket.socket) -> tuple[int, bytes]:
    msg_type, length = _HEADER.unpack(_recv_exact(sock, _HEADER.size))
    if length > MAX_PAYLOAD:
        raise ProtocolError(f"Gói tin quá lớn: {length}")
    payload = _recv_exact(sock, length) if length else b""
    return msg_type, payload


def encode_frame(msg_type: int, payload: bytes | dict | None = None) -> bytes:
    if payload is None:
        payload = b""
    elif isinstance(payload, dict):
        payload = json.dumps(payload).encode("utf-8")
    return _HEADER.pack(msg_type, len(payload)) + payload


def decode_json(payload: bytes) -> dict:
    if not payload:
        return {}
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError(f"JSON không hợp lệ: {e}") from e
