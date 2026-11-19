"""Thu âm từ thiết bị vào trên chính máy Windows (micro, sound card...)."""
from __future__ import annotations

import queue
import threading
import wave
from pathlib import Path
from typing import Callable

from .levels import peak_level


def _sd():
    import sounddevice as sd  # import trễ để phần còn lại chạy được khi thiếu PortAudio
    return sd


def list_input_devices() -> list[tuple[int, str]]:
    try:
        sd = _sd()
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        return []
    result = []
    for idx, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0:
            api = hostapis[d["hostapi"]]["name"]
            # Chỉ hiện MME/WASAPI để danh sách gọn (Windows liệt kê 1 thiết bị nhiều lần)
            if api in ("MME", "Windows WASAPI") or not api.startswith(("Windows", "MME")):
                result.append((idx, f"🎤 {d['name']} [{api}]"))
    return result


class LocalRecorder:
    def __init__(self, device: int, sample_rate: int, channels: int, wav_path: Path,
                 on_level: Callable[[float], None], on_chunk: Callable[[bytes], None] | None = None):
        self.device = device
        self.sample_rate = sample_rate
        self.channels = channels
        self.wav_path = Path(wav_path)
        self.on_level = on_level
        self.on_chunk = on_chunk
        self._q: queue.Queue[bytes | None] = queue.Queue()
        self._stream = None
        self._writer: threading.Thread | None = None

    def start(self) -> None:
        sd = _sd()
        dev_info = sd.query_devices(self.device)
        self.channels = max(1, min(self.channels, int(dev_info["max_input_channels"])))
        try:
            sd.check_input_settings(device=self.device, channels=self.channels,
                                    samplerate=self.sample_rate, dtype="int16")
        except Exception:
            self.sample_rate = int(dev_info["default_samplerate"])

        def callback(indata, frames, time_info, status):
            self._q.put(bytes(indata))

        self._stream = sd.RawInputStream(device=self.device, channels=self.channels,
                                         samplerate=self.sample_rate, dtype="int16",
                                         blocksize=0, callback=callback)
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()
        self._stream.start()

    def _write_loop(self) -> None:
        with wave.open(str(self.wav_path), "wb") as w:
            w.setnchannels(self.channels)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            while True:
                data = self._q.get()
                if data is None:
                    break
                w.writeframesraw(data)
                self.on_level(peak_level(data))
                if self.on_chunk:
                    self.on_chunk(data)

    def stop(self) -> Path:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        self._q.put(None)
        if self._writer:
            self._writer.join()
        return self.wav_path


def list_output_devices() -> list[tuple[int, str]]:
    try:
        sd = _sd()
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        return []
    out = []
    for idx, d in enumerate(devices):
        if d.get("max_output_channels", 0) > 0:
            api = hostapis[d["hostapi"]]["name"]
            if api in ("MME", "Windows WASAPI") or not api.startswith(("Windows", "MME")):
                out.append((idx, f"{d['name']} [{api}]"))
    return out
