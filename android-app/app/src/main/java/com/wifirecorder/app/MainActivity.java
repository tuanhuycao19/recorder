package com.wifirecorder.app;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.WindowManager;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetSocketAddress;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

public class MainActivity extends Activity implements RecorderService.Listener {
    private static final int REQ_PERMS = 1;
    private static final String[] MIC_LABELS = {
            "Tự động (ưu tiên không xử lý)", "Micro thường", "Micro quay phim (to, rõ)",
            "Nhận dạng giọng nói (ít xử lý)", "Không xử lý (thô)"};
    private static final Integer[] RATES = {48000, 44100};
    private static final String[] CHANNELS = {"Mono", "Stereo"};

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Map<String, Pc> pcs = new LinkedHashMap<>();
    private TextView statusView, timerView, infoView, pcEmpty;
    private LinearLayout pcList;
    private EditText hostInput;
    private Button recordBtn, disconnectBtn;
    private ProgressBar levelBar;
    private Thread discoveryThread;
    private volatile boolean discovering;
    private WifiManager.MulticastLock multicastLock;
    private boolean autoConnectTried;
    private Runnable pendingAction;
    private float level;

    private static final class Pc {
        String name;
        String host;
        int port;
        long seen;
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        statusView = findViewById(R.id.status);
        timerView = findViewById(R.id.timer);
        infoView = findViewById(R.id.info);
        pcEmpty = findViewById(R.id.pcEmpty);
        pcList = findViewById(R.id.pcList);
        hostInput = findViewById(R.id.hostInput);
        recordBtn = findViewById(R.id.recordBtn);
        disconnectBtn = findViewById(R.id.disconnectBtn);
        levelBar = findViewById(R.id.level);

        SharedPreferences p = prefs();
        hostInput.setText(p.getString(RecorderService.PREF_LAST_HOST, ""));

        Spinner mic = findViewById(R.id.micSpinner);
        mic.setAdapter(adapter(MIC_LABELS));
        mic.setSelection(p.getInt(RecorderService.PREF_MIC_SOURCE, 0));
        mic.setOnItemSelectedListener(onSelect(pos -> prefs().edit().putInt(RecorderService.PREF_MIC_SOURCE, pos).apply()));

        Spinner rate = findViewById(R.id.rateSpinner);
        rate.setAdapter(adapter(new String[]{"48000 Hz", "44100 Hz"}));
        rate.setSelection(p.getInt(RecorderService.PREF_SAMPLE_RATE, 48000) == 44100 ? 1 : 0);
        rate.setOnItemSelectedListener(onSelect(pos -> prefs().edit().putInt(RecorderService.PREF_SAMPLE_RATE, RATES[pos]).apply()));

        Spinner ch = findViewById(R.id.channelSpinner);
        ch.setAdapter(adapter(CHANNELS));
        ch.setSelection(p.getInt(RecorderService.PREF_CHANNELS, 1) == 2 ? 1 : 0);
        ch.setOnItemSelectedListener(onSelect(pos -> prefs().edit().putInt(RecorderService.PREF_CHANNELS, pos + 1).apply()));

        CheckBox keep = findViewById(R.id.keepFiles);
        keep.setChecked(p.getBoolean(RecorderService.PREF_KEEP_FILES, false));
        keep.setOnCheckedChangeListener((b, checked) -> prefs().edit().putBoolean(RecorderService.PREF_KEEP_FILES, checked).apply());

        findViewById(R.id.connectBtn).setOnClickListener(v -> {
            String host = hostInput.getText().toString().trim();
            if (host.isEmpty()) {
                Toast.makeText(this, "Nhập IP máy tính", Toast.LENGTH_SHORT).show();
                return;
            }
            int port = Protocol.TCP_PORT;
            int colon = host.lastIndexOf(':');
            if (colon > 0) {
                try {
                    port = Integer.parseInt(host.substring(colon + 1));
                    host = host.substring(0, colon);
                } catch (NumberFormatException ignored) {
                }
            }
            connect(host, port);
        });
        disconnectBtn.setOnClickListener(v -> sendToService(RecorderService.ACTION_DISCONNECT, null));
        recordBtn.setOnClickListener(v -> {
            RecorderService s = RecorderService.get();
            if (s != null && s.recording) {
                sendToService(RecorderService.ACTION_STOP, null);
            } else {
                sendToService(RecorderService.ACTION_RECORD, null);
            }
        });

        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        ensurePermissions(null);
    }

    private ArrayAdapter<String> adapter(String[] items) {
        ArrayAdapter<String> a = new ArrayAdapter<>(this, android.R.layout.simple_spinner_item, items);
        a.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        return a;
    }

    private interface IntCb {
        void call(int v);
    }

    private static AdapterView.OnItemSelectedListener onSelect(IntCb cb) {
        return new AdapterView.OnItemSelectedListener() {
            @Override
            public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                cb.call(position);
            }

            @Override
            public void onNothingSelected(AdapterView<?> parent) {
            }
        };
    }

    private SharedPreferences prefs() {
        return getSharedPreferences(RecorderService.PREFS, MODE_PRIVATE);
    }

    // ------------------------------------------------------------------ quyền
    private boolean hasMicPermission() {
        return Build.VERSION.SDK_INT < 23
                || checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
    }

    private void ensurePermissions(Runnable then) {
        if (Build.VERSION.SDK_INT < 23) {
            if (then != null) then.run();
            return;
        }
        List<String> need = new ArrayList<>();
        if (!hasMicPermission()) need.add(Manifest.permission.RECORD_AUDIO);
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            need.add(Manifest.permission.POST_NOTIFICATIONS);
        }
        if (need.isEmpty()) {
            if (then != null) then.run();
        } else {
            pendingAction = then;
            requestPermissions(need.toArray(new String[0]), REQ_PERMS);
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        Runnable r = pendingAction;
        pendingAction = null;
        if (!hasMicPermission()) {
            Toast.makeText(this, "Cần quyền ghi âm để dùng điện thoại làm micro", Toast.LENGTH_LONG).show();
        } else if (r != null) {
            r.run();
        }
    }

    // ------------------------------------------------------------------ dịch vụ
    private void sendToService(String action, Intent extras) {
        ensurePermissions(() -> {
            Intent i = new Intent(this, RecorderService.class).setAction(action);
            if (extras != null) i.putExtras(extras);
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(i);
            else startService(i);
        });
    }

    private void connect(String host, int port) {
        Intent e = new Intent();
        e.putExtra(RecorderService.EXTRA_HOST, host);
        e.putExtra(RecorderService.EXTRA_PORT, port);
        hostInput.setText(port == Protocol.TCP_PORT ? host : host + ":" + port);
        sendToService(RecorderService.ACTION_CONNECT, e);
    }

    @Override
    protected void onResume() {
        super.onResume();
        RecorderService.setListener(this);
        startDiscovery();
        handler.post(ticker);
        onStateChanged();
    }

    @Override
    protected void onPause() {
        RecorderService.setListener(null);
        stopDiscovery();
        handler.removeCallbacks(ticker);
        super.onPause();
    }

    private final Runnable ticker = new Runnable() {
        @Override
        public void run() {
            RecorderService s = RecorderService.get();
            long el = (s != null && s.recording && s.recordStartMs > 0)
                    ? (System.currentTimeMillis() - s.recordStartMs) / 1000 : 0;
            timerView.setText(String.format(Locale.US, "%02d:%02d:%02d", el / 3600, el / 60 % 60, el % 60));
            level *= 0.85f;
            levelBar.setProgress((int) (level * 1000));
            refreshPcList();
            handler.postDelayed(this, 100);
        }
    };

    @Override
    public void onStateChanged() {
        RecorderService s = RecorderService.get();
        boolean rec = s != null && s.recording;
        boolean conn = s != null && s.connected;
        statusView.setText(s != null ? s.status : "Chưa kết nối");
        statusView.setBackgroundColor(rec ? 0xFFFFE0E0 : (conn ? 0xFFE0F5E0 : 0xFFEEF2F7));
        recordBtn.setText(rec ? "■ DỪNG" : "● GHI ÂM");
        disconnectBtn.setVisibility(s != null && (conn || s.connecting || !s.pcHost.isEmpty())
                ? View.VISIBLE : View.GONE);
        int pending = RecorderService.pendingFiles(this).size() - (rec ? 1 : 0);
        StringBuilder info = new StringBuilder();
        if (s != null && !s.transferInfo.isEmpty()) info.append(s.transferInfo).append('\n');
        if (pending > 0) {
            info.append(pending).append(" bản ghi chưa gửi về máy tính")
                    .append(conn ? "" : " (sẽ tự gửi khi kết nối)").append('\n');
        }
        info.append("Thư mục trên điện thoại: ").append(RecorderService.recordingsDir(this).getAbsolutePath());
        infoView.setText(info.toString());
    }

    @Override
    public void onLevel(float l) {
        if (l > level) level = l;
    }

    // ------------------------------------------------------------------ tìm PC (UDP broadcast)
    private void startDiscovery() {
        if (discovering) return;
        discovering = true;
        WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        multicastLock = wm.createMulticastLock("WifiRecorder:discovery");
        multicastLock.setReferenceCounted(false);
        multicastLock.acquire();
        discoveryThread = new Thread(this::discoveryLoop, "discovery");
        discoveryThread.start();
    }

    private void stopDiscovery() {
        discovering = false;
        if (discoveryThread != null) discoveryThread.interrupt();
        if (multicastLock != null && multicastLock.isHeld()) multicastLock.release();
    }

    private void discoveryLoop() {
        try (DatagramSocket sock = new DatagramSocket(null)) {
            sock.setReuseAddress(true);
            sock.setBroadcast(true);
            sock.bind(new InetSocketAddress(Protocol.DISCOVERY_PORT));
            sock.setSoTimeout(1000);
            byte[] buf = new byte[2048];
            while (discovering) {
                DatagramPacket pkt = new DatagramPacket(buf, buf.length);
                try {
                    sock.receive(pkt);
                } catch (SocketTimeoutException e) {
                    continue;
                }
                try {
                    JSONObject j = new JSONObject(new String(pkt.getData(), 0, pkt.getLength(), StandardCharsets.UTF_8));
                    if (!Protocol.DISCOVERY_MAGIC.equals(j.optString("app"))) continue;
                    Pc pc = new Pc();
                    pc.host = pkt.getAddress().getHostAddress();
                    pc.name = j.optString("name", pc.host);
                    pc.port = j.optInt("port", Protocol.TCP_PORT);
                    pc.seen = System.currentTimeMillis();
                    handler.post(() -> onPcSeen(pc));
                } catch (Exception ignored) {
                }
            }
        } catch (Exception e) {
            handler.post(() -> pcEmpty.setText("Không dò tìm được (" + e.getMessage() + "). Hãy nhập IP máy tính."));
        }
    }

    private void onPcSeen(Pc pc) {
        boolean isNew = !pcs.containsKey(pc.host);
        pcs.put(pc.host, pc);
        if (isNew) rebuildPcButtons();
        // Tự kết nối lại máy tính dùng lần trước
        RecorderService s = RecorderService.get();
        boolean idle = s == null || (!s.connected && !s.connecting && s.pcHost.isEmpty());
        if (!autoConnectTried && idle && hasMicPermission()
                && pc.host.equals(prefs().getString(RecorderService.PREF_LAST_HOST, ""))) {
            autoConnectTried = true;
            connect(pc.host, pc.port);
        }
    }

    private void refreshPcList() {
        long now = System.currentTimeMillis();
        boolean changed = pcs.values().removeIf(p -> now - p.seen > 6000);
        if (changed) rebuildPcButtons();
    }

    private void rebuildPcButtons() {
        pcList.removeAllViews();
        for (Pc pc : pcs.values()) {
            Button b = new Button(this);
            b.setAllCaps(false);
            b.setText("💻  " + pc.name + "  (" + pc.host + ")");
            b.setOnClickListener(v -> connect(pc.host, pc.port));
            pcList.addView(b);
        }
        pcEmpty.setVisibility(pcs.isEmpty() ? View.VISIBLE : View.GONE);
    }
}
