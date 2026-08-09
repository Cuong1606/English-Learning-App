ENGLISH LEARNING APP V2.4.3
===========================

MỞ APP
1) Double-click: start_app.bat
2) Không cần tạo icon Desktop.

Nếu app không mở, chạy: Kiem tra loi.bat
Log server trên Windows:
%LOCALAPPDATA%\EnglishLocal\launcher_server.log

NỘI DUNG
- Learn: 8 Topics / 54 Islands
- Vocabulary: A1 / A2 / B1 / B2 / C1 / Khác
- Courses:
  + 4000 Essential English Words: 6 Books / 180 Units / 3.600 câu / 3.600 audio
  + Common English Phrases: 852 câu / 852 audio Charon
- My Islands: tạo tay hoặc import XLSX + Bulk Audio theo Audio Key

IMPORT XLSX / BULK AUDIO
- Vào My Islands > Quản lý Island > Tải file XLSX mẫu.
- Điền Audio Key / English / Vietnamese / Audio file / Note.
- Có thể import câu trước rồi bổ sung audio sau.
- Bulk Audio nhận ZIP/folder và tự ghép theo Audio Key hoặc tên file audio.
- Ví dụ Audio Key T001 nên dùng audio T001.mp3.
- App báo số file đã ghép / còn thiếu / không khớp / xung đột.

HỌC & ÔN TẬP
- Learn: học theo danh sách, nghe từng câu hoặc Auto Next.
- Shadowing: nghe -> nghỉ -> lặp lại, không dùng microphone.
- Active Recall: tự nhớ rồi chấm Quên / Khó / Nhớ / Dễ.
- Chỉ Active Recall có chấm điểm mới cập nhật lịch FSRS.
- Daily Study ưu tiên câu đến hạn; Free Study không tự thay đổi SRS nếu chỉ đọc/nghe.
- Mỗi câu có menu Ôn tập: Ôn lại ngay / Reset về Chưa học / Tạm dừng-Tiếp tục / Thông tin SRS.
- Mỗi Island, Unit, My Island và Course 4000 Essential có thao tác ôn tập hàng loạt; câu canonical trùng chỉ xử lý một lần.
- Reset về Chưa học giữ lại review history; Tạm dừng loại câu khỏi Daily Study nhưng giữ lịch FSRS để tiếp tục sau.
- Settings > FSRS: Desired Retention mặc định 90%; Reschedule existing cards mặc định OFF.

DỮ LIỆU CÁ NHÂN TRÊN WINDOWS
%LOCALAPPDATA%\EnglishLocal\user_data
%LOCALAPPDATA%\EnglishLocal\user_audio
Cập nhật app không làm mất tiến độ, My Islands hoặc lịch SRS nếu giữ thư mục trên.

YÊU CẦU
- Windows có Python 3.
- Microsoft Edge hoặc Google Chrome.

V2.4.3
- Chỉ mở bằng start_app.bat.
- Đã bỏ toàn bộ chức năng tạo icon Desktop và file launcher VBS bên ngoài.
- Tự chọn port local còn trống, ưu tiên 8767.
- Launcher không phụ thuộc PowerShell.
- Chỉ tái sử dụng server đúng cùng phiên bản để tránh mở nhầm bản cũ.
- Common English Phrases đủ 852/852 audio.
