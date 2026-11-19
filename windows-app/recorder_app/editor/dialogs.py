"""Hộp thoại tham số cho hiệu ứng, giảm nhiễu và tuỳ chọn."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)


def ask_params(parent, title: str, fields: list[tuple], note: str = "") -> dict | None:
    """fields: (key, nhãn, mặc định, kiểu/tuỳ chọn)
       kiểu: (min, max, decimals, suffix) cho số; list[str] cho combobox; bool cho checkbox."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    lay = QVBoxLayout(dlg)
    if note:
        lbl = QLabel(note)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
    form = QFormLayout()
    lay.addLayout(form)
    widgets = {}
    for key, label, default, spec in fields:
        if isinstance(default, bool):
            w = QCheckBox()
            w.setChecked(default)
        elif isinstance(spec, list):
            w = QComboBox()
            w.addItems(spec)
            w.setCurrentText(str(default))
        else:
            lo, hi, dec, suffix = spec
            w = QDoubleSpinBox() if dec else QSpinBox()
            if dec:
                w.setDecimals(dec)
            w.setRange(lo, hi)
            w.setValue(default)
            if suffix:
                w.setSuffix(" " + suffix)
        widgets[key] = w
        form.addRow(label, w)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    if dlg.exec() != QDialog.Accepted:
        return None
    out = {}
    for key, w in widgets.items():
        if isinstance(w, QCheckBox):
            out[key] = w.isChecked()
        elif isinstance(w, QComboBox):
            out[key] = w.currentText()
        else:
            out[key] = w.value()
    return out


class NoiseReductionDialog(QDialog):
    """Hai bước như Audacity: 1) lấy mẫu nhiễu, 2) chọn vùng cần lọc rồi bấm OK."""

    def __init__(self, parent, has_profile: bool, get_profile_cb, params: dict):
        super().__init__(parent)
        self.setWindowTitle("Giảm nhiễu")
        lay = QVBoxLayout(self)
        s1 = QLabel("<b>Bước 1</b>: chọn vài giây <i>chỉ có tiếng ồn</i> (không có giọng/nhạc), "
                    "rồi bấm nút dưới để lấy mẫu nhiễu.")
        s1.setWordWrap(True)
        lay.addWidget(s1)
        self.profile_btn = QPushButton("Lấy mẫu nhiễu")
        self.profile_lbl = QLabel("✓ Đã có mẫu nhiễu" if has_profile else "Chưa có mẫu nhiễu")
        row = QHBoxLayout()
        row.addWidget(self.profile_btn)
        row.addWidget(self.profile_lbl, 1)
        lay.addLayout(row)
        self.profile_btn.clicked.connect(lambda: (get_profile_cb(), self.accept_profile()))
        s2 = QLabel("<b>Bước 2</b>: chọn vùng cần lọc (bỏ trống = cả track), mở lại hộp thoại này "
                    "và bấm OK.")
        s2.setWordWrap(True)
        lay.addWidget(s2)
        form = QFormLayout()
        self.reduction = QDoubleSpinBox(suffix=" dB", decimals=0)
        self.reduction.setRange(0, 48)
        self.reduction.setValue(params.get("reduction", 12))
        self.sensitivity = QDoubleSpinBox(decimals=1)
        self.sensitivity.setRange(0, 24)
        self.sensitivity.setValue(params.get("sensitivity", 6))
        self.smoothing = QSpinBox()
        self.smoothing.setRange(0, 12)
        self.smoothing.setValue(params.get("smoothing", 3))
        form.addRow("Mức giảm nhiễu", self.reduction)
        form.addRow("Độ nhạy", self.sensitivity)
        form.addRow("Làm mượt tần số (dải)", self.smoothing)
        lay.addLayout(form)
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        self.bb.button(QDialogButtonBox.Ok).setEnabled(has_profile)
        lay.addWidget(self.bb)
        self.profile_taken = False

    def accept_profile(self):
        self.profile_taken = True
        self.reject()

    def params(self) -> dict:
        return {"reduction": self.reduction.value(), "sensitivity": self.sensitivity.value(),
                "smoothing": self.smoothing.value()}


class PreferencesDialog(QDialog):
    def __init__(self, parent, cfg: dict, output_devices: list[tuple[int, str]]):
        super().__init__(parent)
        self.setWindowTitle("Tuỳ chọn")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)
        dir_row = QWidget()
        h = QHBoxLayout(dir_row)
        h.setContentsMargins(0, 0, 0, 0)
        self.out_dir = QLineEdit(cfg["output_dir"])
        browse = QPushButton("Chọn...")
        browse.clicked.connect(self._browse)
        h.addWidget(self.out_dir, 1)
        h.addWidget(browse)
        form.addRow("Thư mục lưu bản ghi", dir_row)
        self.bitrate = QComboBox()
        self.bitrate.addItems(["128", "192", "256", "320"])
        self.bitrate.setCurrentText(str(cfg["bitrate"]))
        form.addRow("Chất lượng MP3 (kbps)", self.bitrate)
        self.autosave = QCheckBox("Tự lưu MP3 vào thư mục sau mỗi lần ghi")
        self.autosave.setChecked(cfg["autosave"])
        form.addRow("", self.autosave)
        self.out_dev = QComboBox()
        self.out_dev.addItem("Mặc định của Windows", None)
        for idx, name in output_devices:
            self.out_dev.addItem(name, idx)
            if cfg.get("output_device") == idx:
                self.out_dev.setCurrentIndex(self.out_dev.count() - 1)
        form.addRow("Loa phát", self.out_dev)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Thư mục lưu", self.out_dir.text())
        if d:
            self.out_dir.setText(d)

    def values(self) -> dict:
        return {"output_dir": self.out_dir.text() or str(Path.home()), "bitrate": int(self.bitrate.currentText()),
                "autosave": self.autosave.isChecked(), "output_device": self.out_dev.currentData()}
