package com.wifirecorder.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTimestamp;
import android.media.MediaRecorder;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Environment;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.DataInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.zip.CRC32;

/**
 * Dịch vụ chạy nền (foreground) giữ kết nối tới PC và thu âm, để vẫn hoạt động khi tắt màn hình.
 *
 * Bản ghi luôn được lưu thành WAV gốc trên điện thoại; khi dừng, file được gửi nguyên vẹn về PC
 * (PC chuyển sang MP3). Âm thanh trực tiếp gửi kèm trong lúc ghi chỉ để PC hiển thị mức âm và
 * làm bản dự phòng. Nếu mất WiFi, điện thoại vẫn ghi tiếp và gửi lại file khi kết nối lại.
 */
public class RecorderService extends Service {
    static final String ACTION_CONNECT = "com.wifirecorder.CONNECT";
    static final String ACTION_DISCONNECT = "com.wifirecorder.DISCONNECT";
    static final String ACTION_RECORD = "com.wifirecorder.RECORD";
    static final String ACTION_STOP = "com.wifirecorder.STOP";
    static final String EXTRA_HOST = "host";
    static final String EXTRA_PORT = "port";

    static final String PREFS = "settings";
    static final String PREF_LAST_HOST = "last_host";
    static final String PREF_LAST_PORT = "last_port";
    static final String PREF_MIC_SOURCE = "mic_source";
    static final String PREF_SAMPLE_RATE = "sample_rate";
    static final String PREF_CHANNELS = "channels";
    static final String PREF_KEEP_FILES = "keep_files";

    static final String APP_VERSION = "1.0.0";
    private static final String CHANNEL_ID = "recorder";
    private static final int NOTIF_ID = 1;
    private static final int READ_TIMEOUT_MS = 15000;
    private static final int RECONNECT_DELAY_MS = 3000;

    interface Listener {
        void onStateChanged();

        void onLevel(float level);
    }

    private static volatile RecorderService instance;
    private static volatile Listener listener;
    private static final Handler main = new Handler(Looper.getMainLooper());

    static RecorderService get() {
        return instance;
    }

    static void setListener(Listener l) {
        listener = l;
    }

    // ---- trạng thái đọc từ giao diện
    volatile boolean connected;
    volatile boolean connecting;
    volatile boolean recording;
    volatile String pcName = "";
    volatile String pcHost = "";
    volatile String status = "Chưa kết nối";
    volatile String transferInfo = "";
    volatile long recordStartMs;
    volatile int recSampleRate;
    volatile int recChannels;

    private final Object lock = new Object();
    private final LinkedBlockingQueue<byte[]> sendQueue = new LinkedBlockingQueue<>(3000);
    private final ExecutorService fileSender = Executors.newSingleThreadExecutor();
    private final Set<String> queuedFiles = Collections.synchronizedSet(new HashSet<>());
    private Socket socket;
    private int generation;
    private boolean userDisconnect;
    private int reconnectPort;
    private volatile boolean stopRequested;
    private volatile File currentFile;
    private Thread recThread;
    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;
    private long lastLevelPost;

    // ------------------------------------------------------------------ vòng đời
    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "WifiRecorder:rec");
        wakeLock.setReferenceCounted(false);
        WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        int mode = Build.VERSION.SDK_INT >= 29 ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY
                : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
        wifiLock = wm.createWifiLock(mode, "WifiRecorder:wifi");
        wifiLock.setReferenceCounted(false);
        createChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startInForeground();
        String action = intent != null ? intent.getAction() : null;
        if (ACTION_CONNECT.equals(action)) {
            connect(intent.getStringExtra(EXTRA_HOST), intent.getIntExtra(EXTRA_PORT, Protocol.TCP_PORT));
        } else if (ACTION_DISCONNECT.equals(action)) {
            disconnect();
        } else if (ACTION_RECORD.equals(action)) {
            SharedPreferences p = prefs();
            startRecording(newSessionId(), p.getInt(PREF_SAMPLE_RATE, 48000), p.getInt(PREF_CHANNELS, 1));
        } else if (ACTION_STOP.equals(action)) {
            stopRecording();
        }
        maybeStopSelf();
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        instance = null;
        stopRequested = true;
        synchronized (lock) {
            userDisconnect = true;
            generation++;
            closeSocket();
        }
        fileSender.shutdownNow();
        if (wakeLock.isHeld()) wakeLock.release();
        if (wifiLock.isHeld()) wifiLock.release();
        notifyState();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    SharedPreferences prefs() {
        return getSharedPreferences(PREFS, MODE_PRIVATE);
    }

    static File recordingsDir(Context ctx) {
        File d = ctx.getExternalFilesDir(Environment.DIRECTORY_MUSIC);
        if (d == null) d = new File(ctx.getFilesDir(), "recordings");
        //noinspection ResultOfMethodCallIgnored
        d.mkdirs();
        return d;
    }

    /** Các file đã ghi nhưng chưa gửi thành công về PC. */
    static List<File> pendingFiles(Context ctx) {
        List<File> out = new ArrayList<>();
        File[] files = recordingsDir(ctx).listFiles();
        if (files == null) return out;
        for (File f : files) {
            String n = f.getName();
            if (n.startsWith("REC_") && n.endsWith(".wav") && !n.endsWith(".sent.wav")) out.add(f);
        }
        Collections.sort(out);
        return out;
    }

    private static String newSessionId() {
        return new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new java.util.Date());
    }

    // ------------------------------------------------------------------ thông báo
    private void createChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL_ID, "Thu âm",
                    NotificationManager.IMPORTANCE_LOW);
            getSystemService(NotificationManager.class).createNotificationChannel(ch);
        }
    }

    private Notification buildNotification() {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        String title = recording ? "Đang ghi âm" : (connected ? "Đã kết nối " + pcName : "WiFi Recorder");
        return b.setSmallIcon(R.drawable.ic_mic)
                .setContentTitle(title)
                .setContentText(status)
                .setContentIntent(pi)
                .setOngoing(true)
                .build();
    }

    private void startInForeground() {
        Notification n = buildNotification();
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
        } else {
            startForeground(NOTIF_ID, n);
        }
    }

    private void notifyState() {
        main.post(() -> {
            if (instance == this) {
                NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                nm.notify(NOTIF_ID, buildNotification());
            }
            Listener l = listener;
            if (l != null) l.onStateChanged();
        });
    }

    private void setStatus(String s) {
        status = s;
        notifyState();
    }

    private void maybeStopSelf() {
        main.post(() -> {
            synchronized (lock) {
                boolean reconnectPending = !userDisconnect && !pcHost.isEmpty() && !connected;
                if (!connected && !connecting && !recording && !reconnectPending && queuedFiles.isEmpty()) {
                    stopForeground(true);
                    stopSelf();
                }
            }
        });
    }

    // ------------------------------------------------------------------ kết nối
    void connect(String host, int port) {
        if (host == null || host.trim().isEmpty()) return;
        final String h = host.trim();
        final int gen;
        synchronized (lock) {
            generation++;
            gen = generation;
            closeSocket();
            connected = false;
            connecting = true;
            userDisconnect = false;
            pcHost = h;
            reconnectPort = port;
        }
        prefs().edit().putString(PREF_LAST_HOST, h).putInt(PREF_LAST_PORT, port).apply();
        setStatus("Đang kết nối tới " + h + "...");
        new Thread(() -> connectionLoop(gen, h, port), "conn").start();
    }

    void disconnect() {
        synchronized (lock) {
            userDisconnect = true;
            generation++;
            closeSocket();
            connected = false;
            connecting = false;
            pcHost = "";
            pcName = "";
        }
        if (wifiLock.isHeld()) wifiLock.release();
        setStatus("Đã ngắt kết nối");
        maybeStopSelf();
    }

    private void closeSocket() {
        if (socket != null) {
            try {
                socket.close();
            } catch (IOException ignored) {
            }
            socket = null;
        }
        sendQueue.clear();
    }

    private boolean isCurrent(int gen) {
        synchronized (lock) {
            return gen == generation;
        }
    }

    private void connectionLoop(int gen, String host, int port) {
        Socket s = new Socket();
        String error = null;
        try {
            s.connect(new InetSocketAddress(host, port), 5000);
            s.setTcpNoDelay(true);
            s.setSoTimeout(READ_TIMEOUT_MS);
            s.setSendBufferSize(512 * 1024);
            OutputStream out = new BufferedOutputStream(s.getOutputStream(), 64 * 1024);
            synchronized (lock) {
                if (gen != generation) {
                    s.close();
                    return;
                }
                socket = s;
                sendQueue.clear();
                connected = true;
                connecting = false;
            }
            wifiLock.acquire();
            new Thread(() -> senderLoop(gen, out), "sender").start();
            new Thread(() -> pingLoop(gen), "ping").start();

            JSONObject hello = new JSONObject();
            hello.put("name", deviceName());
            hello.put("proto", Protocol.PROTO_VERSION);
            hello.put("app_version", APP_VERSION);
            sendQueue.offer(Protocol.frame(Protocol.MSG_HELLO, hello));
            if (recording) sendState(true, currentSession());
            setStatus("Đã kết nối, chờ máy tính phản hồi...");

            DataInputStream in = new DataInputStream(new BufferedInputStream(s.getInputStream()));
            while (isCurrent(gen)) {
                int type = in.readUnsignedByte();
                int len = in.readInt();
                if (len < 0 || len > Protocol.MAX_PAYLOAD) throw new IOException("Gói tin không hợp lệ");
                byte[] payload = new byte[len];
                in.readFully(payload);
                handleMessage(type, payload);
            }
        } catch (Exception e) {
            error = e.getMessage();
        } finally {
            onConnectionEnded(gen, host, port, error);
        }
    }

    private void onConnectionEnded(int gen, String host, int port, String error) {
        boolean reconnect;
        synchronized (lock) {
            if (gen != generation) return;
            closeSocket();
            boolean wasConnected = connected;
            connected = false;
            connecting = false;
            reconnect = !userDisconnect;
            status = (wasConnected ? "Mất kết nối" : "Không kết nối được tới " + host)
                    + (error != null ? " (" + error + ")" : "")
                    + (reconnect ? " — đang thử lại..." : "");
        }
        if (wifiLock.isHeld()) wifiLock.release();
        notifyState();
        if (reconnect) {
            main.postDelayed(() -> {
                synchronized (lock) {
                    if (gen != generation || userDisconnect || instance != this) return;
                }
                connect(host, port);
            }, RECONNECT_DELAY_MS);
        }
    }

    private void senderLoop(int gen, OutputStream out) {
        try {
            while (isCurrent(gen)) {
                byte[] f = sendQueue.poll(500, TimeUnit.MILLISECONDS);
                if (f == null) continue;
                out.write(f);
                if (sendQueue.isEmpty()) out.flush();
            }
        } catch (Exception e) {
            synchronized (lock) {
                if (gen == generation && socket != null) {
                    try {
                        socket.close(); // vòng đọc sẽ báo lỗi và xử lý mất kết nối
                    } catch (IOException ignored) {
                    }
                }
            }
        }
    }

    private void pingLoop(int gen) {
        while (isCurrent(gen)) {
            try {
                Thread.sleep(2000);
            } catch (InterruptedException e) {
                return;
            }
            if (isCurrent(gen)) sendQueue.offer(Protocol.frame(Protocol.MSG_PING, (JSONObject) null));
        }
    }

    /** Đưa gói tin vào hàng đợi, chờ nếu đầy (dùng cho gửi file). Trả false nếu mất kết nối. */
    private boolean enqueueBlocking(byte[] frame) throws InterruptedException {
        while (connected) {
            if (sendQueue.offer(frame, 500, TimeUnit.MILLISECONDS)) return true;
        }
        return false;
    }

    private static String deviceName() {
        String m = Build.MODEL == null ? "" : Build.MODEL;
        String b = Build.MANUFACTURER == null ? "" : Build.MANUFACTURER;
        if (m.toLowerCase(Locale.ROOT).startsWith(b.toLowerCase(Locale.ROOT))) return m;
        return (b.isEmpty() ? "" : Character.toUpperCase(b.charAt(0)) + b.substring(1) + " ") + m;
    }

    private void handleMessage(int type, byte[] payload) {
        JSONObject j;
        switch (type) {
            case Protocol.MSG_HELLO:
                j = Protocol.parse(payload);
                pcName = j.optString("name", pcHost);
                setStatus("Đã kết nối tới " + pcName);
                for (File f : pendingFiles(this)) {
                    if (!f.equals(currentFile)) queueFileSend(f);
                }
                break;
            case Protocol.MSG_START:
                j = Protocol.parse(payload);
                if (recording) {
                    sendState(true, currentSession());
                } else {
                    startRecording(j.optString("session", newSessionId()),
                            j.optInt("sample_rate", 48000), j.optInt("channels", 1));
                }
                break;
            case Protocol.MSG_STOP:
                stopRecording();
                break;
            case Protocol.MSG_PING:
                // Trả lời ngay để PC đồng bộ đồng hồ (đo thời gian khứ hồi)
                j = Protocol.parse(payload);
                if (j.has("pc")) {
                    try {
                        JSONObject pong = new JSONObject();
                        pong.put("pc", j.optLong("pc"));
                        pong.put("ph", System.nanoTime());
                        sendQueue.offer(Protocol.frame(Protocol.MSG_PONG, pong));
                    } catch (Exception ignored) {
                    }
                }
                break;
            case Protocol.MSG_FILE_ACK:
                j = Protocol.parse(payload);
                onFileAck(j.optString("session", ""), j.optBoolean("ok", false));
                break;
            default:
                break; // PING, ...
        }
    }

    // ------------------------------------------------------------------ ghi âm
    private String currentSession() {
        File f = currentFile;
        if (f == null) return "";
        String n = f.getName();
        return n.substring(4, n.length() - 4);
    }

    private void sendState(boolean rec, String session) {
        if (!connected) return;
        try {
            JSONObject st = new JSONObject();
            st.put("recording", rec);
            st.put("session", session);
            st.put("sample_rate", recSampleRate);
            st.put("channels", recChannels);
            sendQueue.offer(Protocol.frame(Protocol.MSG_STATE, st));
        } catch (Exception ignored) {
        }
    }

    private void sendTiming(String session, long t0, boolean approx) {
        if (!connected) return;
        try {
            JSONObject t = new JSONObject();
            t.put("session", session);
            t.put("t0_ns", t0);
            t.put("approx", approx);
            sendQueue.offer(Protocol.frame(Protocol.MSG_TIMING, t));
        } catch (Exception ignored) {
        }
    }

    private void sendError(String message) {
        if (!connected) return;
        try {
            JSONObject e = new JSONObject();
            e.put("message", message);
            sendQueue.offer(Protocol.frame(Protocol.MSG_ERROR, e));
        } catch (Exception ignored) {
        }
    }

    void startRecording(String session, int sampleRate, int channels) {
        synchronized (lock) {
            if (recording) return;
            recording = true;
            stopRequested = false;
        }
        wakeLock.acquire(24 * 60 * 60 * 1000L);
        recThread = new Thread(() -> recordLoop(session, sampleRate, channels), "record");
        recThread.setPriority(Thread.MAX_PRIORITY);
        recThread.start();
        notifyState();
    }

    void stopRecording() {
        stopRequested = true;
    }

    private int resolveMicSource() {
        int pref = prefs().getInt(PREF_MIC_SOURCE, 0);
        switch (pref) {
            case 1:
                return MediaRecorder.AudioSource.MIC;
            case 2:
                return MediaRecorder.AudioSource.CAMCORDER;
            case 3:
                return MediaRecorder.AudioSource.VOICE_RECOGNITION;
            case 4:
                return MediaRecorder.AudioSource.UNPROCESSED;
            default:
                AudioManager am = (AudioManager) getSystemService(AUDIO_SERVICE);
                String unp = am.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED);
                return "true".equals(unp) ? MediaRecorder.AudioSource.UNPROCESSED
                        : MediaRecorder.AudioSource.MIC;
        }
    }

    /** Mở AudioRecord, thử lần lượt các cấu hình nếu máy không hỗ trợ cấu hình yêu cầu. */
    private AudioRecord openAudioRecord(int sampleRate, int channels) {
        int[] sources = {resolveMicSource(), MediaRecorder.AudioSource.MIC};
        int[] rates = {sampleRate, 48000, 44100};
        int[] chans = {channels, 1};
        for (int src : sources) {
            for (int sr : rates) {
                for (int ch : chans) {
                    int mask = ch == 2 ? AudioFormat.CHANNEL_IN_STEREO : AudioFormat.CHANNEL_IN_MONO;
                    int min = AudioRecord.getMinBufferSize(sr, mask, AudioFormat.ENCODING_PCM_16BIT);
                    if (min <= 0) continue;
                    AudioRecord r = null;
                    try {
                        r = new AudioRecord(src, sr, mask, AudioFormat.ENCODING_PCM_16BIT,
                                Math.max(min * 4, sr * ch * 2));
                        if (r.getState() == AudioRecord.STATE_INITIALIZED) {
                            recSampleRate = sr;
                            recChannels = ch;
                            return r;
                        }
                    } catch (SecurityException e) {
                        return null;
                    } catch (Exception ignored) {
                    }
                    if (r != null) r.release();
                }
            }
        }
        return null;
    }

    private void recordLoop(String session, int sampleRate, int channels) {
        File file = new File(recordingsDir(this), "REC_" + session + ".wav");
        AudioRecord rec = openAudioRecord(sampleRate, channels);
        WavWriter wav = null;
        String error = null;
        try {
            if (rec == null) throw new IOException("Không mở được micro (kiểm tra quyền ghi âm)");
            wav = new WavWriter(file, recSampleRate, recChannels);
            currentFile = file;
            rec.startRecording();
            if (rec.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
                throw new IOException("Micro đang bị ứng dụng khác sử dụng");
            }
            recordStartMs = System.currentTimeMillis();
            sendState(true, session);
            setStatus("Đang ghi " + recSampleRate + " Hz " + (recChannels == 2 ? "stereo" : "mono"));
            byte[] buf = new byte[recSampleRate * recChannels * 2 / 50]; // 20 ms
            long framesRead = 0;
            boolean timingSent = false;
            long approxT0 = 0;
            AudioTimestamp ats = new AudioTimestamp();
            while (!stopRequested) {
                int n = rec.read(buf, 0, buf.length);
                if (n < 0) throw new IOException("Lỗi đọc micro (" + n + ")");
                if (n == 0) continue;
                if (framesRead == 0) {
                    approxT0 = System.nanoTime() - (long) (n / (2 * recChannels)) * 1_000_000_000L / recSampleRate;
                }
                framesRead += n / (2 * recChannels);
                if (!timingSent) {
                    // Thời điểm (đồng hồ monotonic) của mẫu đầu tiên — để PC căn đúng với nhạc đang phát
                    if (rec.getTimestamp(ats, AudioTimestamp.TIMEBASE_MONOTONIC) == AudioRecord.SUCCESS) {
                        long t0 = ats.nanoTime - ats.framePosition * 1_000_000_000L / recSampleRate;
                        sendTiming(session, t0, false);
                        timingSent = true;
                    } else if (framesRead > recSampleRate / 2) {
                        sendTiming(session, approxT0, true);   // máy không hỗ trợ: dùng ước lượng
                        timingSent = true;
                    }
                }
                wav.write(buf, 0, n);
                if (connected) sendQueue.offer(Protocol.frame(Protocol.MSG_AUDIO, buf, 0, n)); // bỏ nếu mạng chậm
                postLevel(buf, n);
            }
        } catch (IOException e) {
            error = e.getMessage();
        } finally {
            if (rec != null) {
                try {
                    rec.stop();
                } catch (Exception ignored) {
                }
                rec.release();
            }
            if (wav != null) {
                try {
                    wav.close();
                } catch (IOException e) {
                    if (error == null) error = e.getMessage();
                }
            }
            currentFile = null;
            synchronized (lock) {
                recording = false;
            }
            if (wakeLock.isHeld()) wakeLock.release();
        }
        if (error != null) sendError(error);
        sendState(false, session);
        if (wav != null && wav.dataBytes() > 0) {
            setStatus(error != null ? error : (connected ? "Đã dừng, đang gửi file về máy tính..."
                    : "Đã lưu trên điện thoại, sẽ gửi khi kết nối máy tính"));
            if (connected) queueFileSend(file);
        } else {
            //noinspection ResultOfMethodCallIgnored
            file.delete();
            setStatus(error != null ? error : "Đã dừng");
        }
        maybeStopSelf();
    }

    private void postLevel(byte[] buf, int n) {
        long now = System.currentTimeMillis();
        if (now - lastLevelPost < 50) return;
        lastLevelPost = now;
        int peak = 0;
        for (int i = 0; i + 1 < n; i += 2) {
            int s = (short) ((buf[i] & 0xff) | (buf[i + 1] << 8));
            if (s < 0) s = -s;
            if (s > peak) peak = s;
        }
        final float level = peak / 32768f;
        main.post(() -> {
            Listener l = listener;
            if (l != null) l.onLevel(level);
        });
    }

    // ------------------------------------------------------------------ gửi file
    private void queueFileSend(File f) {
        if (!queuedFiles.add(f.getAbsolutePath())) return;
        fileSender.execute(() -> {
            try {
                sendFile(f);
            } finally {
                queuedFiles.remove(f.getAbsolutePath());
                maybeStopSelf();
            }
        });
    }

    private void sendFile(File f) {
        if (!connected || !f.exists()) return;
        String name = f.getName();
        String session = name.substring(4, name.length() - 4);
        long size = f.length();
        CRC32 crc = new CRC32();
        try (InputStream in = new BufferedInputStream(new FileInputStream(f), 256 * 1024)) {
            JSONObject begin = new JSONObject();
            begin.put("session", session);
            begin.put("size", size);
            if (!enqueueBlocking(Protocol.frame(Protocol.MSG_FILE_BEGIN, begin))) return;
            byte[] buf = new byte[64 * 1024];
            long sent = 0;
            long lastUi = 0;
            int n;
            while ((n = in.read(buf)) > 0) {
                crc.update(buf, 0, n);
                if (!enqueueBlocking(Protocol.frame(Protocol.MSG_FILE_DATA, buf, 0, n))) {
                    transferInfo = "";
                    return;
                }
                sent += n;
                long now = System.currentTimeMillis();
                if (now - lastUi > 300) {
                    lastUi = now;
                    transferInfo = String.format(Locale.US, "Đang gửi %s: %d%%", name, sent * 100 / Math.max(1, size));
                    notifyState();
                }
            }
            JSONObject end = new JSONObject();
            end.put("session", session);
            end.put("crc32", crc.getValue());
            enqueueBlocking(Protocol.frame(Protocol.MSG_FILE_END, end));
            transferInfo = "Đã gửi " + name + ", máy tính đang chuyển sang MP3";
        } catch (Exception e) {
            transferInfo = "Lỗi gửi file: " + e.getMessage();
        }
        notifyState();
    }

    private void onFileAck(String session, boolean ok) {
        File f = new File(recordingsDir(this), "REC_" + session + ".wav");
        if (!ok) {
            transferInfo = "Máy tính nhận file lỗi, sẽ gửi lại lần kết nối sau";
        } else if (f.exists()) {
            if (prefs().getBoolean(PREF_KEEP_FILES, false)) {
                //noinspection ResultOfMethodCallIgnored
                f.renameTo(new File(f.getParentFile(), "REC_" + session + ".sent.wav"));
            } else {
                //noinspection ResultOfMethodCallIgnored
                f.delete();
            }
            transferInfo = "Máy tính đã lưu bản ghi " + session;
        }
        notifyState();
    }
}
