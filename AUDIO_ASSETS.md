# Tài nguyên audio bên ngoài

Audio không được lưu trong Git repository.

Project cần hai thư mục audio bên ngoài:

- `audio/` — 11.638 MP3
- `course_audio/` — 5.442 MP3
- Tổng: 17.080 MP3

Đây là snapshot tài nguyên đi kèm v1.2.0. Không dùng các số trên làm hằng số runtime.
Đếm trực tiếp tài nguyên của checkout/build hiện tại bằng:

```powershell
python scripts/audit_audio_assets.py
```

Khi khôi phục project trên máy mới:

1. Clone repository.
2. Copy backup `audio/` vào root project.
3. Copy backup `course_audio/` vào root project.
4. Chạy script audit và đối chiếu snapshot của bản phát hành cần build.

Bản Windows trong GitHub Release đã đóng gói đầy đủ audio để sử dụng app, nhưng source repository không chứa hai thư mục audio này.
