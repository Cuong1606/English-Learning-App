ENGLISH LEARNING APP V1.3.0
===========================

MỞ BẢN WINDOWS RELEASE
1) Giải nén nguyên thư mục Release.
2) Double-click: English Learning App.exe
3) Người dùng bản Release không cần cài Python và không cần Edge/Chrome.
4) App desktop dùng Microsoft Edge WebView2 Runtime; Windows 10/11 thường đã có sẵn.

CHẠY TỪ SOURCE (DÀNH CHO PHÁT TRIỂN)
- Cài Python 3 rồi chạy start_app.bat.
- Kiem tra loi.bat và launcher_server.log chỉ dành cho luồng chạy source.

NỘI DUNG
- Learn: 8 Topics / 54 Islands
- Vocabulary: A1 / A2 / B1 / B2 / C1 / Khác
- Courses:
  + 4000 Essential English Words: 6 Books / 180 Units / 3.600 câu / 3.600 audio
  + Common English Phrases: 852 câu / 852 audio Charon
  + English by Topic: 30 Units / 990 câu / 990 audio
- My Islands: tạo tay hoặc import XLSX + Bulk Audio theo Audio Key

IMPORT XLSX / BULK AUDIO
- Vào My Islands > Quản lý Island > Tải file XLSX mẫu.
- Điền Audio Key / English / Vietnamese / Audio file / Note.
- Có thể import câu trước rồi bổ sung audio sau.
- Bulk Audio nhận ZIP/folder và tự ghép theo Audio Key hoặc tên file audio.
- Ví dụ Audio Key T001 nên dùng audio T001.mp3.
- App báo số file đã ghép / còn thiếu / không khớp / xung đột.
- ZIP/folder được stream hoặc xử lý trực tiếp ở backend; báo cáo được hiển thị trước khi xác nhận import.

HỌC & ÔN TẬP
- Learn: học theo danh sách, nghe từng câu hoặc Auto Next.
- Shadowing: nghe -> nghỉ -> lặp lại, không dùng microphone.
- Active Recall: tự nhớ rồi chấm Quên / Khó / Nhớ / Dễ.
- Chỉ Active Recall có chấm điểm mới cập nhật lịch FSRS.
- Daily Study ưu tiên câu đến hạn; Free Study không tự thay đổi SRS nếu chỉ đọc/nghe.
- Mỗi câu có menu Ôn tập: Ôn lại ngay / Reset về Chưa học / Tạm dừng-Tiếp tục / Thông tin SRS.
- Mỗi Island, Unit, My Island và Course có thao tác ôn tập hàng loạt; câu canonical trùng chỉ xử lý một lần.
- Reset về Chưa học giữ lại review history; Tạm dừng loại câu khỏi Daily Study nhưng giữ lịch FSRS để tiếp tục sau.
- Settings > FSRS: Desired Retention mặc định 90%; Reschedule existing cards mặc định OFF.

DỮ LIỆU CÁ NHÂN TRÊN WINDOWS
%LOCALAPPDATA%\EnglishLocal\user_data
%LOCALAPPDATA%\EnglishLocal\user_audio
Cập nhật app không làm mất tiến độ, My Islands hoặc lịch SRS nếu giữ thư mục trên.
- Settings > Dữ liệu cho phép Reset, xóa sạch profile, sao lưu và khôi phục trực tiếp từ ZIP.
- Khi khôi phục, app tự kiểm tra backup, tạo safety snapshot rồi đóng hoàn toàn. Ở lần mở app thủ công tiếp theo, dữ liệu được khôi phục đồng bộ trước khi database/server/UI khởi động; không cần giải nén backup và không chạy helper nền trong luồng bình thường.

V1.3.0
- Đổi nguồn câu mới ngay tại Home/Review, giữ nguyên progress và fallback an toàn.
- Courses hiển thị 3 card gọn và render từ metadata.
- Active Recall cập nhật queue/counters cục bộ; bootstrap dùng audio index runtime.
- Bulk Audio bỏ payload Base64 lớn, có staging, pre-import report và rollback.
- Search có filter và gộp canonical sentence ở nhiều vị trí.

V1.2.0
- Thêm English by Topic: 30 Units / 990 câu / 990 audio.
- Dùng chung Learn / Shadowing / Active Recall / FSRS / Saved và ôn tập hàng loạt theo Unit hoặc toàn Course.
- Tổng tài nguyên bundled audio: 17.080 MP3.

V1.1.0
- Chỉ mở bằng start_app.bat.
- Đã bỏ toàn bộ chức năng tạo icon Desktop và file launcher VBS bên ngoài.
- Tự chọn port local còn trống, ưu tiên 8767.
- Launcher không phụ thuộc PowerShell.
- Chỉ tái sử dụng server đúng cùng phiên bản để tránh mở nhầm bản cũ.
- Common English Phrases đủ 852/852 audio.

Tài nguyên audio khi khôi phục/build source: xem AUDIO_ASSETS.md.
