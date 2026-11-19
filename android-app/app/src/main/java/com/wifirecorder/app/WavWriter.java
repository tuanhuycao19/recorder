package com.wifirecorder.app;

import java.io.BufferedOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.RandomAccessFile;

/** Ghi file WAV PCM 16-bit; cập nhật header khi đóng. */
final class WavWriter {
    private final File file;
    private final int sampleRate;
    private final int channels;
    private final BufferedOutputStream out;
    private long dataBytes;

    WavWriter(File file, int sampleRate, int channels) throws IOException {
        this.file = file;
        this.sampleRate = sampleRate;
        this.channels = channels;
        this.out = new BufferedOutputStream(new FileOutputStream(file), 256 * 1024);
        out.write(header(0));
    }

    void write(byte[] buf, int off, int len) throws IOException {
        out.write(buf, off, len);
        dataBytes += len;
    }

    long dataBytes() {
        return dataBytes;
    }

    void close() throws IOException {
        out.close();
        try (RandomAccessFile raf = new RandomAccessFile(file, "rw")) {
            raf.seek(0);
            raf.write(header(dataBytes));
        }
    }

    private byte[] header(long dataLen) {
        long riffLen = Math.min(0xFFFFFFFFL, dataLen + 36);
        long dLen = Math.min(0xFFFFFFFFL, dataLen);
        int byteRate = sampleRate * channels * 2;
        byte[] h = new byte[44];
        putStr(h, 0, "RIFF");
        putInt(h, 4, riffLen);
        putStr(h, 8, "WAVE");
        putStr(h, 12, "fmt ");
        putInt(h, 16, 16);
        putShort(h, 20, 1); // PCM
        putShort(h, 22, channels);
        putInt(h, 24, sampleRate);
        putInt(h, 28, byteRate);
        putShort(h, 32, channels * 2);
        putShort(h, 34, 16);
        putStr(h, 36, "data");
        putInt(h, 40, dLen);
        return h;
    }

    private static void putStr(byte[] b, int off, String s) {
        for (int i = 0; i < 4; i++) b[off + i] = (byte) s.charAt(i);
    }

    private static void putInt(byte[] b, int off, long v) {
        b[off] = (byte) v;
        b[off + 1] = (byte) (v >> 8);
        b[off + 2] = (byte) (v >> 16);
        b[off + 3] = (byte) (v >> 24);
    }

    private static void putShort(byte[] b, int off, int v) {
        b[off] = (byte) v;
        b[off + 1] = (byte) (v >> 8);
    }
}
