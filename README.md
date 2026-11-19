# WiFi Recorder

Ứng dụng thu âm trên Windows, dùng được **điện thoại Android làm micro qua WiFi**.

- **App Windows** (`windows-app/`): chọn nguồn thu (micro máy tính hoặc điện thoại đã kết nối), bấm Ghi/Dừng, lưu **MP3** (128–320 kbps), xem mức âm, quản lý danh sách bản ghi.
- **App Android** (`android-app/`, file APK): biến điện thoại thành micro không dây. Tự tìm máy tính trong mạng WiFi, nhận lệnh ghi/dừng từ máy tính, hoặc tự ghi ngay trên điện thoại.

## Vì sao chất lượng tốt

```
Điện thoại: micro ──► WAV gốc 48 kHz/16-bit (lưu trên máy) ──► sau khi dừng: gửi nguyên file (kiểm tra CRC32) ──┐
            └──► luồng trực tiếp (để xem mức âm + bản dự phòng) ──────────────────────────────────────────────────┤
Máy tính:                                                                          WAV gốc ──► LAME ──► MP3 320 kbps
```

- Điện thoại ghi **không nén** rồi gửi nguyên file sau khi dừng, nên WiFi chập chờn **không làm rè/mất tiếng** trong bản ghi.
- Chỉ mã hoá MP3 **một lần** trên máy tính bằng LAME (chất lượng cao, mặc định 320 kbps) → không bị nén chồng nén.
- Mất WiFi giữa chừng: điện thoại **vẫn ghi tiếp**, tự kết nối lại, và gửi file khi có mạng. Nếu không nhận được file gốc, máy tính dùng bản trực tiếp làm dự phòng.
- Chọn nguồn micro trên điện thoại: *Không xử lý (thô)* — tắt lọc ồn/AGC, hợp ghi nhạc; *Micro quay phim* — chịu âm lượng lớn tốt; *Micro thường*…

## Tải về

Mỗi lần push, GitHub Actions tự build (tab **Actions** → lần chạy mới nhất → mục *Artifacts*):

- `WifiRecorder-windows` → `WifiRecorder.exe` (chạy luôn, không cần cài Python)
- `WifiRecorder-android` → `WifiRecorder.apk`

Tạo tag `v1.0.0` và push thì hai file được đính kèm vào một GitHub Release.

## Cách dùng

1. Máy tính và điện thoại **cùng một mạng WiFi**.
2. Chạy `WifiRecorder.exe`. Lần đầu Windows Firewall hỏi → chọn **Allow** (cho mạng Private) để điện thoại kết nối được.
3. Cài `WifiRecorder.apk` lên điện thoại (cho phép "cài từ nguồn không xác định"), mở app, cấp quyền **Ghi âm** và **Thông báo**.
4. Trên điện thoại, máy tính sẽ tự hiện trong danh sách → bấm vào để kết nối. Nếu không thấy, nhập IP hiển thị trên app Windows (cổng mặc định `50510`).
5. Trên máy tính chọn nguồn `📱 <tên điện thoại>` → bấm **GHI ÂM** / **DỪNG**. Cũng có thể bấm ghi/dừng ngay trên điện thoại.
6. File MP3 lưu ở `Music\WifiRecorder` (đổi được trong app). Tuỳ chọn *Giữ thêm file WAV gốc* nếu cần bản không nén.

Mẹo:
- App điện thoại chạy dịch vụ nền (có thông báo) nên vẫn ghi khi tắt màn hình. Nên tắt *tối ưu pin* cho app nếu ghi lâu.
- Bản ghi chưa gửi được nằm trong `Android/data/com.wifirecorder.app/files/Music` và sẽ tự gửi lần kết nối sau.

## Chạy / build từ mã nguồn

**Windows app** (Python 3.10+):

```bat
cd windows-app
run.bat          :: chạy trực tiếp
build.bat        :: đóng gói dist\WifiRecorder.exe
```

Test: `python -m pytest windows-app/tests` (giả lập điện thoại, kiểm tra cả trường hợp lỗi CRC và mất kết nối).

**Android app** (JDK 17 + Android SDK, hoặc mở thư mục `android-app` bằng Android Studio):

```bash
cd android-app
./gradlew assembleRelease   # app/build/outputs/apk/release/app-release.apk
```

## Giao thức (TCP cổng 50510, UDP 50511 để dò tìm)

Mỗi gói: `[1 byte loại][4 byte độ dài big-endian][payload]`. Payload điều khiển là JSON.

| Loại | Hướng | Nội dung |
|---|---|---|
| 1 HELLO | hai chiều | `{name, proto}` |
| 2 START | PC → ĐT | `{session, sample_rate, channels}` |
| 3 STOP | PC → ĐT | `{}` |
| 4 AUDIO | ĐT → PC | PCM 16-bit LE (trực tiếp) |
| 5/6/7 FILE_BEGIN / DATA / END | ĐT → PC | file WAV gốc, END kèm `crc32` |
| 8 STATE | ĐT → PC | `{recording, session, sample_rate, channels}` |
| 9 PING | hai chiều | giữ kết nối |
| 10 ERROR | ĐT → PC | `{message}` |
| 11 FILE_ACK | PC → ĐT | `{session, ok}` — điện thoại xoá/đánh dấu file đã gửi |

Máy tính phát broadcast UDP `{"app":"wifirec","name":...,"port":50510}` mỗi giây trên cổng 50511.
