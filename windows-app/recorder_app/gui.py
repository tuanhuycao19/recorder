"""Giao diện chính (Tkinter) của app thu âm trên Windows."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .encoder import wav_duration, wav_to_mp3
from .local_input import LocalRecorder, list_input_devices
from .phone_server import PhoneServer, local_ipv4_addresses

APP_NAME = "WiFi Recorder"
SAMPLE_RATES = [44100, 48000]
BITRATES = [128, 192, 256, 320]


def config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "WifiRecorder" / "settings.json"


def default_output_dir() -> Path:
    music = Path.home() / "Music"
    return (music if music.exists() else Path.home()) / "WifiRecorder"


def open_path(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.cfg = self._load_cfg()
        self.sources: list[tuple[str, object]] = []  # (nhãn, ("local", idx) | ("phone", conn_id))
        self.local_rec: LocalRecorder | None = None
        self.active: dict | None = None  # phiên đang ghi
        self.pending_phone_sessions: set[str] = set()
        self.level = 0.0
        self.workers: list[threading.Thread] = []

        self.server = PhoneServer(on_event=lambda name, **kw: self.events.put((name, kw)))
        try:
            self.server.start()
            server_ok = True
        except OSError as e:
            server_ok = False
            messagebox.showwarning(APP_NAME, f"Không mở được cổng {self.server.port} cho điện thoại:\n{e}")

        self._build_ui(server_ok)
        self.refresh_sources()
        self._load_recordings()
        self.root.after(50, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ cấu hình
    def _load_cfg(self) -> dict:
        cfg = {"output_dir": str(default_output_dir()), "sample_rate": 48000, "channels": 1,
               "bitrate": 320, "keep_wav": False}
        try:
            cfg.update(json.loads(config_path().read_text(encoding="utf-8")))
        except Exception:
            pass
        return cfg

    def _save_cfg(self) -> None:
        self.cfg.update(output_dir=self.out_var.get(), sample_rate=int(self.sr_var.get()),
                        channels=2 if self.ch_var.get() == "Stereo" else 1,
                        bitrate=int(self.br_var.get()), keep_wav=bool(self.keepwav_var.get()))
        try:
            p = config_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ giao diện
    def _build_ui(self, server_ok: bool) -> None:
        r = self.root
        r.title(f"{APP_NAME} {__version__}")
        r.geometry("760x700")
        r.minsize(620, 520)
        style = ttk.Style()
        try:
            style.theme_use("vista" if sys.platform.startswith("win") else "clam")
        except tk.TclError:
            pass
        style.configure("Big.TButton", font=("Segoe UI", 14, "bold"), padding=10)
        style.configure("Timer.TLabel", font=("Consolas", 28, "bold"))

        main = ttk.Frame(r, padding=12)
        main.pack(fill="both", expand=True)

        # --- nguồn thu
        src = ttk.LabelFrame(main, text="Nguồn thu âm", padding=8)
        src.pack(fill="x")
        self.src_var = tk.StringVar()
        self.src_combo = ttk.Combobox(src, textvariable=self.src_var, state="readonly")
        self.src_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(src, text="Làm mới", command=self.refresh_sources).pack(side="left", padx=(8, 0))

        ips = ", ".join(local_ipv4_addresses()) or "không xác định"
        info = (f"Điện thoại: mở app WiFi Recorder (cùng mạng WiFi) → chọn máy này, "
                f"hoặc nhập IP {ips}  cổng {self.server.port}") if server_ok else \
            "Không thể nhận kết nối từ điện thoại (cổng đang bị chiếm)."
        ttk.Label(main, text=info, foreground="#555", wraplength=680).pack(fill="x", pady=(4, 8))

        # --- cài đặt
        st = ttk.LabelFrame(main, text="Cài đặt", padding=8)
        st.pack(fill="x")
        self.sr_var = tk.StringVar(value=str(self.cfg["sample_rate"]))
        self.ch_var = tk.StringVar(value="Stereo" if self.cfg["channels"] == 2 else "Mono")
        self.br_var = tk.StringVar(value=str(self.cfg["bitrate"]))
        self.keepwav_var = tk.BooleanVar(value=self.cfg["keep_wav"])
        self.out_var = tk.StringVar(value=self.cfg["output_dir"])

        ttk.Label(st, text="Tần số mẫu (Hz)").grid(row=0, column=0, sticky="w")
        ttk.Combobox(st, textvariable=self.sr_var, values=SAMPLE_RATES, width=8,
                     state="readonly").grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(st, text="Kênh").grid(row=0, column=2, sticky="w", padx=(12, 0))
        ttk.Combobox(st, textvariable=self.ch_var, values=["Mono", "Stereo"], width=8,
                     state="readonly").grid(row=0, column=3, sticky="w", padx=4)
        ttk.Label(st, text="MP3 (kbps)").grid(row=0, column=4, sticky="w", padx=(12, 0))
        ttk.Combobox(st, textvariable=self.br_var, values=BITRATES, width=7,
                     state="readonly").grid(row=0, column=5, sticky="w", padx=4)
        ttk.Checkbutton(st, text="Giữ thêm file WAV gốc", variable=self.keepwav_var).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(st, text="Lưu vào").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(st, textvariable=self.out_var).grid(row=1, column=1, columnspan=6, sticky="ew",
                                                      padx=4, pady=(8, 0))
        ttk.Button(st, text="Chọn...", command=self._choose_dir).grid(row=1, column=7, sticky="w",
                                                                      padx=(8, 0), pady=(8, 0))
        st.columnconfigure(6, weight=1)

        # --- điều khiển ghi
        ctl = ttk.Frame(main, padding=(0, 12))
        ctl.pack(fill="x")
        self.rec_btn = ttk.Button(ctl, text="⏺  GHI ÂM", style="Big.TButton", command=self.toggle_record)
        self.rec_btn.pack(side="left")
        self.timer_lbl = ttk.Label(ctl, text="00:00:00", style="Timer.TLabel")
        self.timer_lbl.pack(side="left", padx=20)
        self.meter = tk.Canvas(ctl, height=26, bg="#222", highlightthickness=0)
        self.meter.pack(side="left", fill="x", expand=True)

        self.status_var = tk.StringVar(value="Sẵn sàng")
        ttk.Label(main, textvariable=self.status_var).pack(fill="x")
        self.progress = ttk.Progressbar(main, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(4, 8))

        # --- danh sách bản ghi
        lst = ttk.LabelFrame(main, text="Bản ghi", padding=8)
        lst.pack(fill="both", expand=True)
        cols = ("time", "duration", "source")
        self.tree = ttk.Treeview(lst, columns=cols, show="tree headings", height=6)
        self.tree.heading("#0", text="Tên file")
        self.tree.heading("time", text="Thời gian")
        self.tree.heading("duration", text="Độ dài")
        self.tree.heading("source", text="Nguồn")
        self.tree.column("#0", width=260)
        self.tree.column("time", width=140)
        self.tree.column("duration", width=70, anchor="e")
        self.tree.column("source", width=180)
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self._play_selected())
        sb = ttk.Scrollbar(lst, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(main, padding=(0, 8, 0, 0))
        btns.pack(fill="x")
        ttk.Button(btns, text="▶ Phát", command=self._play_selected).pack(side="left")
        ttk.Button(btns, text="Mở thư mục", command=self._open_dir).pack(side="left", padx=6)
        ttk.Button(btns, text="Xoá", command=self._delete_selected).pack(side="left")

    def _choose_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=self.out_var.get())
        if d:
            self.out_var.set(d)
            self._save_cfg()
            self._load_recordings()

    def _out_dir(self) -> Path:
        p = Path(self.out_var.get() or default_output_dir())
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ------------------------------------------------------------ nguồn
    def refresh_sources(self) -> None:
        current = self.src_var.get()
        self.sources = []
        for conn in list(self.server.connections.values()):
            if conn.ready and conn.alive:
                self.sources.append((conn.label, ("phone", conn.id)))
        for idx, name in list_input_devices():
            self.sources.append((name, ("local", idx)))
        labels = [s[0] for s in self.sources]
        self.src_combo["values"] = labels
        if current in labels:
            self.src_var.set(current)
        elif labels:
            self.src_var.set(labels[0])
        else:
            self.src_var.set("")

    def _selected_source(self):
        label = self.src_var.get()
        for l, s in self.sources:
            if l == label:
                return s
        return None

    # ------------------------------------------------------------ ghi âm
    def toggle_record(self) -> None:
        if self.active:
            self.stop_record()
        else:
            self.start_record()

    def start_record(self) -> None:
        src = self._selected_source()
        if not src:
            messagebox.showinfo(APP_NAME, "Chưa có nguồn thu. Hãy kết nối điện thoại hoặc cắm micro.")
            return
        self._save_cfg()
        session = time.strftime("%Y%m%d_%H%M%S")
        sr, ch = int(self.sr_var.get()), (2 if self.ch_var.get() == "Stereo" else 1)
        kind, ref = src
        try:
            if kind == "local":
                wav = self.server.temp_dir / f"{session}_local.wav"
                self.local_rec = LocalRecorder(ref, sr, ch, wav, on_level=self._set_level)
                self.local_rec.start()
                self.active = {"kind": "local", "session": session, "t0": time.time(),
                               "label": self.src_var.get()}
            else:
                conn = self.server.connections.get(ref)
                if not conn:
                    raise RuntimeError("Điện thoại đã ngắt kết nối")
                conn.start_recording(session, sr, ch)
                self.pending_phone_sessions.add(session)
                self.active = {"kind": "phone", "session": session, "t0": time.time(),
                               "conn_id": conn.id, "label": conn.name}
        except Exception as e:
            self.local_rec = None
            self.active = None
            messagebox.showerror(APP_NAME, f"Không bắt đầu ghi được:\n{e}")
            return
        self.rec_btn.configure(text="⏹  DỪNG")
        self.src_combo.configure(state="disabled")
        self.status_var.set(f"Đang ghi từ {self.active['label']}...")

    def stop_record(self) -> None:
        act = self.active
        if not act:
            return
        self.active = None
        self.rec_btn.configure(text="⏺  GHI ÂM")
        self.src_combo.configure(state="readonly")
        self._set_level(0)
        if act["kind"] == "local":
            rec, self.local_rec = self.local_rec, None
            wav = rec.stop()
            self._encode_async(wav, act["session"], act["label"], "máy tính")
        else:
            conn = self.server.connections.get(act["conn_id"])
            if conn:
                try:
                    conn.stop_recording()
                except OSError:
                    pass
            self.status_var.set("Đang nhận file gốc từ điện thoại...")

    def _set_level(self, level: float) -> None:
        self.level = max(level, self.level * 0.85)

    # ------------------------------------------------------------ mã hoá MP3
    def _encode_async(self, wav: Path, session: str, label: str, src_note: str) -> None:
        out_dir = self._out_dir()
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:30].strip("_") or "rec"
        mp3 = out_dir / f"{session}_{safe}.mp3"
        bitrate = int(self.br_var.get())
        keep_wav = bool(self.keepwav_var.get())
        self.status_var.set("Đang chuyển sang MP3...")

        def work():
            try:
                wav_to_mp3(wav, mp3, bitrate)
                if keep_wav:
                    shutil.copy2(wav, mp3.with_suffix(".wav"))
                dur = wav_duration(wav)
                try:
                    wav.unlink()
                except OSError:
                    pass
                self.events.put(("encoded", {"mp3": mp3, "duration": dur, "source": f"{label} ({src_note})"}))
            except Exception as e:
                self.events.put(("encode_failed", {"error": str(e), "wav": wav}))

        t = threading.Thread(target=work, daemon=True)
        self.workers.append(t)
        t.start()

    # ------------------------------------------------------------ vòng sự kiện
    def _poll(self) -> None:
        try:
            while True:
                name, kw = self.events.get_nowait()
                self._handle_event(name, kw)
        except queue.Empty:
            pass
        self._draw_meter()
        if self.active:
            el = int(time.time() - self.active["t0"])
            self.timer_lbl.configure(text=f"{el // 3600:02d}:{el // 60 % 60:02d}:{el % 60:02d}")
        self.root.after(50, self._poll)

    def _handle_event(self, name: str, kw: dict) -> None:
        conn = kw.get("conn")
        if name in ("phone_connected", "phone_disconnected"):
            verb = "đã kết nối" if name == "phone_connected" else "đã ngắt kết nối"
            self.status_var.set(f"{conn.name} {verb}")
            self.refresh_sources()
            if name == "phone_connected" and not self.active:
                self.src_var.set(conn.label)
            if name == "phone_disconnected" and self.active and self.active.get("conn_id") == conn.id:
                self.active = None
                self.rec_btn.configure(text="⏺  GHI ÂM")
                self.src_combo.configure(state="readonly")
        elif name == "phone_level":
            if self.active and self.active.get("conn_id") == conn.id:
                self._set_level(kw["level"])
        elif name == "phone_recording_started":
            if kw["session"] not in self.pending_phone_sessions and not self.active:
                # Người dùng bấm ghi trực tiếp trên điện thoại
                self.pending_phone_sessions.add(kw["session"])
                self.src_var.set(conn.label)
                self.active = {"kind": "phone", "session": kw["session"], "t0": time.time(),
                               "conn_id": conn.id, "label": conn.name}
                self.rec_btn.configure(text="⏹  DỪNG")
                self.src_combo.configure(state="disabled")
            self.status_var.set(f"Đang ghi từ {conn.name} — {kw['sample_rate']} Hz, "
                                f"{'stereo' if kw['channels'] == 2 else 'mono'}")
        elif name == "phone_recording_stopped":
            if self.active and self.active.get("conn_id") == conn.id:
                self.active = None
                self.rec_btn.configure(text="⏺  GHI ÂM")
                self.src_combo.configure(state="readonly")
                self._set_level(0)
            self.status_var.set("Đang nhận file gốc từ điện thoại...")
        elif name == "phone_file_progress":
            total = kw["total"] or 1
            self.progress["value"] = min(100, kw["received"] * 100 / total)
        elif name == "phone_recording_done":
            self.progress["value"] = 0
            note = "điện thoại" if kw["source"] == "file" else "điện thoại, bản trực tiếp"
            self.pending_phone_sessions.discard(kw["session"])
            self._encode_async(kw["wav_path"], kw["session"], conn.name, note)
        elif name == "phone_recording_failed":
            self.progress["value"] = 0
            self.status_var.set(f"Không nhận được âm thanh từ {conn.name}")
        elif name == "phone_error":
            self.status_var.set(f"{conn.name}: {kw['message']}")
            if self.active and self.active.get("conn_id") == conn.id:
                self.active = None
                self.rec_btn.configure(text="⏺  GHI ÂM")
                self.src_combo.configure(state="readonly")
        elif name == "encoded":
            mp3 = kw["mp3"]
            self.status_var.set(f"Đã lưu: {mp3}")
            self._add_recording(mp3, kw["duration"], kw["source"], top=True)
        elif name == "encode_failed":
            self.status_var.set(f"Lỗi chuyển MP3: {kw['error']}")
            messagebox.showerror(APP_NAME, f"Lỗi chuyển MP3:\n{kw['error']}\nFile WAV: {kw['wav']}")
        elif name == "log":
            self.status_var.set(kw["message"])

    def _draw_meter(self) -> None:
        c = self.meter
        w, h = c.winfo_width(), c.winfo_height()
        c.delete("all")
        lvl = self.level if self.active else 0
        self.level *= 0.92
        filled = int(w * min(1.0, lvl))
        color = "#3c3" if lvl < 0.7 else ("#ec3" if lvl < 0.95 else "#e33")
        c.create_rectangle(0, 0, filled, h, fill=color, width=0)
        for frac in (0.25, 0.5, 0.75):
            c.create_line(int(w * frac), 0, int(w * frac), h, fill="#444")

    # ------------------------------------------------------------ danh sách bản ghi
    def _add_recording(self, path: Path, duration: float | None, source: str, top: bool = False) -> None:
        t = time.strftime("%d/%m/%Y %H:%M", time.localtime(path.stat().st_mtime))
        dur = "" if duration is None else f"{int(duration) // 60}:{int(duration) % 60:02d}"
        self.tree.insert("", 0 if top else "end", iid=str(path), text=path.name,
                         values=(t, dur, source))

    def _load_recordings(self) -> None:
        self.tree.delete(*self.tree.get_children())
        try:
            files = sorted(Path(self.out_var.get()).glob("*.mp3"), key=lambda p: p.stat().st_mtime,
                           reverse=True)
        except OSError:
            files = []
        for f in files[:500]:
            self._add_recording(f, None, "")

    def _selected_path(self) -> Path | None:
        sel = self.tree.selection()
        return Path(sel[0]) if sel else None

    def _play_selected(self) -> None:
        p = self._selected_path()
        if p and p.exists():
            open_path(p)

    def _open_dir(self) -> None:
        open_path(self._out_dir())

    def _delete_selected(self) -> None:
        p = self._selected_path()
        if p and messagebox.askyesno(APP_NAME, f"Xoá {p.name}?"):
            p.unlink(missing_ok=True)
            p.with_suffix(".wav").unlink(missing_ok=True)
            self.tree.delete(str(p))

    def _on_close(self) -> None:
        if self.active and not messagebox.askyesno(APP_NAME, "Đang ghi âm. Dừng và thoát?"):
            return
        if self.active:
            self.stop_record()
        self._save_cfg()
        for t in self.workers:  # chờ các file MP3 đang mã hoá xong
            t.join(timeout=60)
        self.server.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()
