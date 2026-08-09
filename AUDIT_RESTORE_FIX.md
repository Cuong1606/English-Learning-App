# Restore audit fix (2026-08-10)

Restore bình thường không còn chạy background helper. ZIP được validate và staging khi app đang mở; pending restore được áp dụng đồng bộ ở lần mở app thủ công tiếp theo, trước mọi kết nối user DB, HTTP server và WebView.

Nếu SQLite/WAL vẫn bị process ngoài giữ khóa, startup chờ tối đa khoảng 5 giây, giữ nguyên dữ liệu cũ, ghi kết quả lỗi an toàn, dọn pending và tiếp tục mở app.

Xem gói audit gốc `english_learning_app_v1_1_restore_startup_fix_for_codex.zip` để biết phân tích và checklist Windows đầy đủ.
