ENGLISH LEARNING APP V1.1.0 - DONG GOI WINDOWS

MUC TIEU
- Tao app Windows co cua so rieng.
- Khong mo thanh tab Chrome/Brave/Coc Coc.
- File chay sau khi build: English Learning App.exe
- App van chay local/offline; du lieu hoc ca nhan nam trong LocalAppData\EnglishLocal.

CACH LAM
1. Chep cac file nay vao THU MUC GOC cua app V1.1.0 dang chay tot:
   app_version.py
   desktop_app.py
   restore_helper.py
   server.py
   EnglishLearningApp.spec
   requirements-build.txt
   build_windows.bat
   app.ico

2. Thu muc goc do phai co san:
   web\
   data\
   audio\
   course_audio\
   templates\

3. Double-click build_windows.bat.
4. Lan dau script se tai PyInstaller + pywebview, sau do dong goi.
5. Khi hien HOAN TAT, mo:
   dist\English Learning App\English Learning App.exe

LUU Y
- Build theo kieu ONEDIR de 16.090 MP3 khong bi giai nen lai moi lan mo app.
- Khong can start_app.bat sau khi da build.
- Khong can mo Chrome.
- Windows can Microsoft Edge WebView2 Runtime. Windows 10/11 thong thuong da co san.
- Khong nen chuyen rieng file EXE ra khoi thu muc dist\English Learning App; EXE can cac file trong thu muc do.

PHAT HANH
Sau khi test EXE on, nen zip NGUYEN thu muc:
  dist\English Learning App\
va upload file ZIP do len GitHub Release.
