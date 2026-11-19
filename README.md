# WiFi Recorder

Ứng dụng thu âm trên Windows, dùng được **điện thoại Android làm micro qua WiFi**.

- **App Windows** (`windows-app/`): giao diện chỉnh sửa kiểu Audacity — ghi âm từ micro máy tính hoặc điện thoại thẳng vào track, sửa nhiều track, áp hiệu ứng, xuất **MP3** (128–320 kbps) / WAV / FLAC.
- **App Android** (`android-app/`, file APK): biến điện thoại thành micro không dây. Tự tìm máy tính trong mạng WiFi, nhận lệnh ghi/dừng từ máy tính, hoặc tự ghi ngay trên điện thoại.

## Trình chỉnh sửa (app Windows)

- **Nhiều track**: mỗi lần ghi tạo track mới tại vị trí con trỏ; nhập thêm file MP3/WAV/FLAC/OGG (kéo thả được). Mỗi track có Tắt tiếng / Solo / âm lượng / cân bằng trái-phải, kéo mép dưới để đổi chiều cao.
- **Sóng âm** giống Audacity (đỉnh + RMS, báo vỡ tiếng màu đỏ), phóng to tới từng mẫu, thước thời gian (bấm để phát từ đó), đồng hồ mức âm Ghi/Phát.
- **Sửa**: Cắt / Chép / Dán / Xoá, Trim, Làm im lặng, Nhân bản, Hoàn tác/Làm lại (tối đa 50 bước).
- **Hiệu ứng**: Khuếch đại, Chuẩn hoá, Fade In/Out, Giảm nhiễu (2 bước: lấy mẫu nhiễu → lọc), Nén dải động, Tiếng vang, Đổi tốc độ, Đảo ngược, Đảo pha. **Tạo**: Im lặng, Âm (tone), Tiếng ồn trắng.
- **Dự án**: lưu/mở `.wrproj` (giữ nguyên các track để sửa tiếp); xuất bản trộn hoặc vùng chọn ra MP3/WAV/FLAC.
- Mỗi lần ghi xong vẫn tự lưu MP3 vào thư mục bản ghi (tắt được trong *Tệp → Tuỳ chọn*).

Phím tắt chính (giống Audacity): `Space` phát/dừng, `R` ghi, `P` tạm dừng, `Ctrl+Z/Y`, `Ctrl+X/C/V`, `Delete`, `Ctrl+T` trim, `Ctrl+L` im lặng, `Ctrl+1/3` phóng to/thu nhỏ, `Ctrl+F` vừa cửa sổ, `Ctrl + lăn chuột` phóng to tại chuột. Xem đủ trong *Trợ giúp → Phím tắt* (F1).

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

**Windows app** (Python 3.10+, giao diện PySide6/Qt):

```bat
cd windows-app
run.bat          :: chạy trực tiếp
build.bat        :: đóng gói dist\WifiRecorder.exe
```

Test: `python -m pytest windows-app/tests` (giả lập điện thoại + kiểm tra cắt/dán/undo, hiệu ứng, trộn, lưu dự án).

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
