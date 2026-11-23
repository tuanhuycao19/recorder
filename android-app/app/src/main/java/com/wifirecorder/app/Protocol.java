package com.wifirecorder.app;

import org.json.JSONObject;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;

/** Giao thức giống hệt windows-app/recorder_app/protocol.py */
final class Protocol {
    static final int PROTO_VERSION = 1;
    static final int TCP_PORT = 50510;
    static final int DISCOVERY_PORT = 50511;
    static final String DISCOVERY_MAGIC = "wifirec";

    static final int MSG_HELLO = 1;
    static final int MSG_START = 2;
    static final int MSG_STOP = 3;
    static final int MSG_AUDIO = 4;
    static final int MSG_FILE_BEGIN = 5;
    static final int MSG_FILE_DATA = 6;
    static final int MSG_FILE_END = 7;
    static final int MSG_STATE = 8;
    static final int MSG_PING = 9;
    static final int MSG_ERROR = 10;
    static final int MSG_FILE_ACK = 11;
    static final int MSG_PONG = 12;
    static final int MSG_TIMING = 13;

    static final int MAX_PAYLOAD = 16 * 1024 * 1024;

    private Protocol() {}

    static byte[] frame(int type, byte[] payload, int offset, int length) {
        ByteBuffer bb = ByteBuffer.allocate(5 + length);
        bb.put((byte) type);
        bb.putInt(length);
        bb.put(payload, offset, length);
        return bb.array();
    }

    static byte[] frame(int type, JSONObject json) {
        byte[] p = (json == null ? "{}" : json.toString()).getBytes(StandardCharsets.UTF_8);
        return frame(type, p, 0, p.length);
    }

    static JSONObject parse(byte[] payload) {
        try {
            if (payload.length == 0) return new JSONObject();
            return new JSONObject(new String(payload, StandardCharsets.UTF_8));
        } catch (Exception e) {
            return new JSONObject();
        }
    }
}
